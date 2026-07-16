from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence
from xml.etree import ElementTree

import pyarrow as pa
import pyarrow.parquet as pq


BENCHMARK_IDS = (
    "CSI300_CNY_TOTAL_RETURN",
    "NASDAQ100_CNY_TOTAL_RETURN",
)
GENERATOR_VERSION = "official-benchmark-set/1"
_SOURCE_IDENTITIES = {
    "csi300_total_return": {
        "provider": "China Securities Index Co., Ltd.",
        "source_id": "H00300",
        "url_prefix": "https://www.csindex.com.cn/",
    },
    "nasdaq100_total_return": {
        "provider": "Nasdaq, Inc.",
        "source_id": "XNDX",
        "url_prefix": "https://indexes.nasdaqomx.com/",
    },
    "usd_cny": {
        "provider": "Board of Governors of the Federal Reserve System",
        "source_id": "DEXCHUS",
        "url_prefix": "https://www.federalreserve.gov/",
    },
}
_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("time", pa.string(), nullable=False),
        pa.field("benchmark_id", pa.string(), nullable=False),
        pa.field("returns", pa.float64(), nullable=False),
    ]
)


class BenchmarkSetError(ValueError):
    """Raised when an official benchmark set is incomplete or altered."""


@dataclass(frozen=True, order=True)
class BenchmarkLevel:
    trading_date: date
    value: float

    def __post_init__(self) -> None:
        if not isinstance(self.trading_date, date):
            raise BenchmarkSetError("benchmark level date is invalid")
        if not math.isfinite(float(self.value)) or float(self.value) <= 0:
            raise BenchmarkSetError("benchmark level must be finite and positive")


@dataclass(frozen=True)
class SourcePayload:
    name: str
    filename: str
    provider: str
    source_id: str
    url: str
    content_type: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class BenchmarkSet:
    root: Path
    benchmark_set_id: str
    manifest: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        object.__setattr__(self, "manifest", MappingProxyType(dict(self.manifest)))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _validate_sources(sources: Sequence[SourcePayload]) -> None:
    if {item.name for item in sources} != set(_SOURCE_IDENTITIES) or len(sources) != 3:
        raise BenchmarkSetError("benchmark set requires exactly three official sources")
    if len({item.filename for item in sources}) != len(sources):
        raise BenchmarkSetError("benchmark source filenames must be unique")
    for source in sources:
        expected = _SOURCE_IDENTITIES[source.name]
        if (
            source.provider != expected["provider"]
            or source.source_id != expected["source_id"]
            or not source.url.startswith(expected["url_prefix"])
            or not source.filename
            or Path(source.filename).name != source.filename
            or not source.data
        ):
            raise BenchmarkSetError(f"benchmark source identity is invalid: {source.name}")


def _validated_levels(levels: Iterable[BenchmarkLevel], name: str) -> list[BenchmarkLevel]:
    ordered = sorted(levels)
    if len({item.trading_date for item in ordered}) != len(ordered):
        raise BenchmarkSetError(f"{name} contains duplicate dates")
    if len(ordered) < 2:
        raise BenchmarkSetError(f"{name} needs at least two observations")
    return ordered


def build_benchmark_rows(
    *,
    csi_levels: Iterable[BenchmarkLevel],
    nasdaq_levels: Iterable[BenchmarkLevel],
    usd_cny_levels: Iterable[BenchmarkLevel],
    start_date: date,
    end_date: date,
) -> list[dict[str, object]]:
    if start_date > end_date:
        raise BenchmarkSetError("benchmark date range is invalid")
    csi = _validated_levels(csi_levels, "CSI300 total return")
    nasdaq = _validated_levels(nasdaq_levels, "NASDAQ100 total return")
    fx = _validated_levels(usd_cny_levels, "USD/CNY")
    rows: list[dict[str, object]] = []

    for previous, current in zip(csi, csi[1:], strict=False):
        if start_date <= current.trading_date <= end_date:
            rows.append(
                {
                    "time": current.trading_date.isoformat(),
                    "benchmark_id": BENCHMARK_IDS[0],
                    "returns": current.value / previous.value - 1.0,
                }
            )

    nasdaq_by_date = {item.trading_date: item.value for item in nasdaq}
    fx_by_date = {item.trading_date: item.value for item in fx}
    common = sorted(set(nasdaq_by_date) & set(fx_by_date))
    combined = [
        BenchmarkLevel(current, nasdaq_by_date[current] * fx_by_date[current])
        for current in common
    ]
    for previous, current in zip(combined, combined[1:], strict=False):
        if start_date <= current.trading_date <= end_date:
            rows.append(
                {
                    "time": current.trading_date.isoformat(),
                    "benchmark_id": BENCHMARK_IDS[1],
                    "returns": current.value / previous.value - 1.0,
                }
            )
    rows.sort(key=lambda item: (str(item["time"]), str(item["benchmark_id"])))
    return rows


def _validate_rows(
    rows: Iterable[Mapping[str, object]],
    start_date: date,
    end_date: date,
) -> list[dict[str, object]]:
    materialized: list[dict[str, object]] = []
    keys: set[tuple[str, str]] = set()
    identities: set[str] = set()
    for original in rows:
        if set(original) != {"time", "benchmark_id", "returns"}:
            raise BenchmarkSetError("benchmark row fields are invalid")
        try:
            trading_date = date.fromisoformat(str(original["time"]))
        except ValueError as exc:
            raise BenchmarkSetError("benchmark time must use YYYY-MM-DD") from exc
        benchmark_id = str(original["benchmark_id"])
        if benchmark_id not in BENCHMARK_IDS:
            raise BenchmarkSetError(f"unsupported benchmark identity: {benchmark_id}")
        value = float(original["returns"])
        if not math.isfinite(value) or value <= -1:
            raise BenchmarkSetError("benchmark return is invalid")
        if not start_date <= trading_date <= end_date:
            raise BenchmarkSetError("benchmark row is outside the declared range")
        key = (trading_date.isoformat(), benchmark_id)
        if key in keys:
            raise BenchmarkSetError(f"duplicate benchmark row: {key}")
        keys.add(key)
        identities.add(benchmark_id)
        materialized.append(
            {"time": key[0], "benchmark_id": benchmark_id, "returns": value}
        )
    if identities != set(BENCHMARK_IDS):
        raise BenchmarkSetError("benchmark set must contain exactly both required identities")
    materialized.sort(key=lambda item: (item["time"], item["benchmark_id"]))
    return materialized


def _source_manifest(root: Path, source: SourcePayload) -> dict[str, object]:
    path = root / "sources" / source.filename
    return {
        "name": source.name,
        "provider": source.provider,
        "source_id": source.source_id,
        "url": source.url,
        "content_type": source.content_type,
        "path": path.relative_to(root).as_posix(),
        "sha256": _digest(path),
        "bytes": path.stat().st_size,
    }


def write_benchmark_set(
    *,
    market_data_root: Path,
    rows: Iterable[Mapping[str, object]],
    sources: Sequence[SourcePayload],
    start_date: date,
    end_date: date,
) -> BenchmarkSet:
    _validate_sources(sources)
    materialized = _validate_rows(rows, start_date, end_date)
    sets_root = Path(market_data_root).resolve() / "benchmark-sets"
    sets_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".benchmark-set-", dir=sets_root))
    try:
        source_root = staging / "sources"
        source_root.mkdir()
        for source in sources:
            (source_root / source.filename).write_bytes(source.data)
        table = pa.Table.from_pylist(materialized, schema=_PARQUET_SCHEMA)
        parquet_path = staging / "benchmark-returns.parquet"
        pq.write_table(
            table,
            parquet_path,
            compression="zstd",
            use_dictionary=False,
            write_statistics=True,
        )
        source_entries = [
            _source_manifest(staging, source)
            for source in sorted(sources, key=lambda item: item.name)
        ]
        counts = {
            benchmark_id: sum(
                row["benchmark_id"] == benchmark_id for row in materialized
            )
            for benchmark_id in BENCHMARK_IDS
        }
        ranges = {
            benchmark_id: {
                "start": min(
                    row["time"]
                    for row in materialized
                    if row["benchmark_id"] == benchmark_id
                ),
                "end": max(
                    row["time"]
                    for row in materialized
                    if row["benchmark_id"] == benchmark_id
                ),
            }
            for benchmark_id in BENCHMARK_IDS
        }
        data_ref = {
            "path": "benchmark-returns.parquet",
            "sha256": _digest(parquet_path),
            "bytes": parquet_path.stat().st_size,
            "rows": table.num_rows,
            "format": "parquet",
            "compression": "zstd",
            "fields": ["time", "benchmark_id", "returns"],
            "unique_key": ["time", "benchmark_id"],
        }
        identity = {
            "schema_version": "benchmark-set/1",
            "generator_version": GENERATOR_VERSION,
            "requested_range": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
            "source_sha256": {
                item["name"]: item["sha256"] for item in source_entries
            },
            "data_sha256": data_ref["sha256"],
        }
        benchmark_set_id = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
        manifest = {
            "schema_version": "benchmark-set/1",
            "benchmark_set_id": benchmark_set_id,
            "status": "complete",
            "timezone": "Asia/Shanghai",
            "requested_range": identity["requested_range"],
            "generator": {
                "name": "official benchmark set builder",
                "version": GENERATOR_VERSION,
            },
            "benchmarks": {
                BENCHMARK_IDS[0]: {
                    "currency": "CNY",
                    "return_kind": "total_return",
                    "definition": "CSI 300 Total Return Index close-to-close return",
                    "source_id": "H00300",
                    "fx_formula": "not_applicable",
                    "rows": counts[BENCHMARK_IDS[0]],
                    "date_range": ranges[BENCHMARK_IDS[0]],
                },
                BENCHMARK_IDS[1]: {
                    "currency": "CNY",
                    "return_kind": "total_return",
                    "definition": "NASDAQ-100 Total Return Index converted to CNY",
                    "source_id": "XNDX",
                    "fx_source_id": "DEXCHUS",
                    "fx_formula": "(1 + XNDX_USD_total_return) * (1 + USD_CNY_change) - 1",
                    "rows": counts[BENCHMARK_IDS[1]],
                    "date_range": ranges[BENCHMARK_IDS[1]],
                },
            },
            "sources": source_entries,
            "normalization": {
                "csi300": "adjacent official H00300 closing levels",
                "nasdaq100": "adjacent official XNDX levels; non-positive market-closure placeholders excluded",
                "usd_cny": "Federal Reserve CNY per USD observations; ND excluded",
                "alignment": "exact-date inner join only; no zero, forward or backward fill",
            },
            "data": data_ref,
            "gate": {
                "status": "pass",
                "exceptions": [],
                "checks": [
                    "official_source_identity",
                    "source_snapshots",
                    "no_proxy",
                    "no_zero_or_forward_fill",
                    "parquet_digest",
                    "unique_key",
                ],
            },
        }
        (staging / "manifest.json").write_bytes(_canonical_bytes(manifest) + b"\n")
        target = sets_root / benchmark_set_id
        if target.exists():
            existing = open_benchmark_set(target)
            shutil.rmtree(staging)
            return existing
        staging.replace(target)
        return open_benchmark_set(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def open_benchmark_set(root: Path) -> BenchmarkSet:
    result_root = Path(root).resolve()
    try:
        manifest = json.loads((result_root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkSetError("benchmark manifest is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "benchmark-set/1"
        or manifest.get("status") != "complete"
        or manifest.get("benchmark_set_id") != result_root.name
        or set(manifest.get("benchmarks", {})) != set(BENCHMARK_IDS)
        or manifest.get("gate", {}).get("status") != "pass"
        or manifest.get("gate", {}).get("exceptions") != []
    ):
        raise BenchmarkSetError("benchmark manifest identity is invalid")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or len(sources) != 3:
        raise BenchmarkSetError("benchmark source evidence is incomplete")
    for source in sources:
        if not isinstance(source, dict):
            raise BenchmarkSetError("benchmark source evidence is invalid")
        path = (result_root / str(source.get("path", ""))).resolve()
        if result_root not in path.parents or not path.is_file():
            raise BenchmarkSetError("benchmark source snapshot is missing")
        if path.stat().st_size != source.get("bytes") or _digest(path) != source.get("sha256"):
            raise BenchmarkSetError("benchmark source digest or size mismatch")
    data_ref = manifest.get("data")
    if not isinstance(data_ref, dict) or data_ref.get("path") != "benchmark-returns.parquet":
        raise BenchmarkSetError("benchmark data reference is invalid")
    data_path = result_root / "benchmark-returns.parquet"
    if (
        not data_path.is_file()
        or data_path.stat().st_size != data_ref.get("bytes")
        or _digest(data_path) != data_ref.get("sha256")
    ):
        raise BenchmarkSetError("benchmark data digest or size mismatch")
    table = pq.read_table(data_path)
    if not table.schema.equals(_PARQUET_SCHEMA) or table.num_rows != data_ref.get("rows"):
        raise BenchmarkSetError("benchmark Parquet schema or rows are invalid")
    rows = table.to_pylist()
    requested = manifest.get("requested_range", {})
    validated = _validate_rows(
        rows,
        date.fromisoformat(str(requested.get("start"))),
        date.fromisoformat(str(requested.get("end"))),
    )
    if rows != validated:
        raise BenchmarkSetError("benchmark rows are not in canonical order")
    for benchmark_id in BENCHMARK_IDS:
        declared = manifest["benchmarks"][benchmark_id]
        current = [row for row in rows if row["benchmark_id"] == benchmark_id]
        if declared.get("rows") != len(current):
            raise BenchmarkSetError("benchmark row counts do not match the manifest")
    return BenchmarkSet(
        root=result_root,
        benchmark_set_id=result_root.name,
        manifest=manifest,
    )


def _http_request(request: urllib.request.Request | str) -> tuple[bytes, str]:
    if isinstance(request, str):
        request = urllib.request.Request(request)
    request.add_header("User-Agent", "Quant-Research-Lab/official-benchmark-set")
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise BenchmarkSetError(f"benchmark source returned HTTP {response.status}")
        return response.read(), str(response.headers.get("Content-Type", ""))


def fetch_official_sources(start_date: date, end_date: date) -> tuple[SourcePayload, ...]:
    buffered_start = start_date - timedelta(days=10)
    buffered_end = end_date + timedelta(days=1)
    csi_url = (
        "https://www.csindex.com.cn/csindex-home/exportExcel/"
        "downloadindex-perf?language=CH"
    )
    csi_body = _canonical_bytes(
        [
            {
                "startDate": buffered_start.strftime("%Y%m%d"),
                "endDate": buffered_end.strftime("%Y%m%d"),
                "indexCode": "H00300",
            }
        ]
    )
    csi_request = urllib.request.Request(
        csi_url,
        data=csi_body,
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://www.csindex.com.cn",
            "Referer": "https://www.csindex.com.cn/",
        },
    )
    csi_data, csi_type = _http_request(csi_request)

    nasdaq_url = "https://indexes.nasdaqomx.com/Index/ExportHistory/XNDX?" + urllib.parse.urlencode(
        {
            "startDate": buffered_start.isoformat() + "T00:00:00.000Z",
            "endDate": buffered_end.isoformat() + "T00:00:00.000Z",
            "timeOfDay": "EOD",
        }
    )
    nasdaq_data, nasdaq_type = _http_request(nasdaq_url)
    fed_url = "https://www.federalreserve.gov/releases/h10/Hist/dat00_ch.htm"
    fed_data, fed_type = _http_request(fed_url)
    return (
        SourcePayload(
            name="csi300_total_return",
            filename="csi300-total-return.xlsx",
            provider=_SOURCE_IDENTITIES["csi300_total_return"]["provider"],
            source_id="H00300",
            url=csi_url,
            content_type=csi_type,
            data=csi_data,
        ),
        SourcePayload(
            name="nasdaq100_total_return",
            filename="nasdaq100-total-return.xlsx",
            provider=_SOURCE_IDENTITIES["nasdaq100_total_return"]["provider"],
            source_id="XNDX",
            url=nasdaq_url,
            content_type=nasdaq_type,
            data=nasdaq_data,
        ),
        SourcePayload(
            name="usd_cny",
            filename="usd-cny.html",
            provider=_SOURCE_IDENTITIES["usd_cny"]["provider"],
            source_id="DEXCHUS",
            url=fed_url,
            content_type=fed_type,
            data=fed_data,
        ),
    )


_XML_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if letters is None:
        raise BenchmarkSetError("Excel cell reference is invalid")
    result = 0
    for character in letters.group(0):
        result = result * 26 + ord(character) - ord("A") + 1
    return result - 1


def _xlsx_rows(data: bytes) -> list[list[object | None]]:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root.findall(f"{_XML_NS}si"):
                    shared.append("".join(node.text or "" for node in item.iter(f"{_XML_NS}t")))
            worksheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise BenchmarkSetError("official Excel source is invalid") from exc
    result: list[list[object | None]] = []
    for row in worksheet.iter(f"{_XML_NS}row"):
        values: list[object | None] = []
        for cell in row.findall(f"{_XML_NS}c"):
            index = _column_index(str(cell.attrib.get("r", "")))
            while len(values) <= index:
                values.append(None)
            kind = cell.attrib.get("t")
            if kind == "inlineStr":
                value: object | None = "".join(
                    node.text or "" for node in cell.iter(f"{_XML_NS}t")
                )
            else:
                node = cell.find(f"{_XML_NS}v")
                raw = None if node is None else node.text
                if raw is None:
                    value = None
                elif kind == "s":
                    value = shared[int(raw)]
                else:
                    value = float(raw)
            values[index] = value
        result.append(values)
    return result


def parse_csi300_levels(data: bytes) -> tuple[BenchmarkLevel, ...]:
    rows = _xlsx_rows(data)
    if not rows:
        raise BenchmarkSetError("CSI source is empty")
    headers = {str(value): index for index, value in enumerate(rows[0])}
    required = {
        "日期Date",
        "指数代码Index Code",
        "指数英文全称Index English Name(Full)",
        "收盘Close",
    }
    if not required.issubset(headers):
        raise BenchmarkSetError("CSI source columns are incomplete")
    result: list[BenchmarkLevel] = []
    for row in rows[1:]:
        try:
            code = str(row[headers["指数代码Index Code"]])
            name = str(row[headers["指数英文全称Index English Name(Full)"]])
            current = datetime.strptime(
                str(row[headers["日期Date"]]), "%Y%m%d"
            ).date()
            close = float(row[headers["收盘Close"]])
        except (IndexError, TypeError, ValueError) as exc:
            raise BenchmarkSetError("CSI source row is invalid") from exc
        if code != "H00300" or name != "CSI 300 Total Return Index":
            raise BenchmarkSetError("CSI source does not prove H00300 total return")
        result.append(BenchmarkLevel(current, close))
    return tuple(_validated_levels(result, "CSI300 total return"))


def _excel_date(value: object) -> date:
    if isinstance(value, (int, float)):
        return date(1899, 12, 30) + timedelta(days=int(value))
    if isinstance(value, str):
        for format_string in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, format_string).date()
            except ValueError:
                continue
    raise BenchmarkSetError("NASDAQ source date is invalid")


def parse_nasdaq100_levels(data: bytes) -> tuple[BenchmarkLevel, ...]:
    rows = _xlsx_rows(data)
    if not rows:
        raise BenchmarkSetError("NASDAQ source is empty")
    headers = {str(value): index for index, value in enumerate(rows[0])}
    if not {"Trade Date", "Index Value"}.issubset(headers):
        raise BenchmarkSetError("NASDAQ source columns are incomplete")
    result: list[BenchmarkLevel] = []
    for row in rows[1:]:
        if not row or all(value is None for value in row):
            continue
        try:
            current = _excel_date(row[headers["Trade Date"]])
            value = float(row[headers["Index Value"]])
        except (IndexError, TypeError, ValueError) as exc:
            raise BenchmarkSetError("NASDAQ source row is invalid") from exc
        if value <= 0:
            continue
        result.append(BenchmarkLevel(current, value))
    return tuple(_validated_levels(result, "NASDAQ100 total return"))


class _FedTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def parse_usd_cny_levels(data: bytes) -> tuple[BenchmarkLevel, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BenchmarkSetError("Federal Reserve source is not UTF-8") from exc
    if "Rates in Chinese yuan per U.S. dollar" not in text:
        raise BenchmarkSetError("Federal Reserve source currency identity is invalid")
    parser = _FedTableParser()
    parser.feed(text)
    result: list[BenchmarkLevel] = []
    for row in parser.rows:
        if len(row) < 2 or row[0] == "Date" or row[1] == "ND":
            continue
        try:
            current = datetime.strptime(row[0].strip(), "%d-%b-%y").date()
            value = float(row[1])
        except ValueError:
            continue
        result.append(BenchmarkLevel(current, value))
    return tuple(_validated_levels(result, "USD/CNY"))


def build_official_benchmark_set(
    market_data_root: Path, start_date: date, end_date: date
) -> BenchmarkSet:
    sources = fetch_official_sources(start_date, end_date)
    by_name = {source.name: source for source in sources}
    rows = build_benchmark_rows(
        csi_levels=parse_csi300_levels(by_name["csi300_total_return"].data),
        nasdaq_levels=parse_nasdaq100_levels(by_name["nasdaq100_total_return"].data),
        usd_cny_levels=parse_usd_cny_levels(by_name["usd_cny"].data),
        start_date=start_date,
        end_date=end_date,
    )
    return write_benchmark_set(
        market_data_root=market_data_root,
        rows=rows,
        sources=sources,
        start_date=start_date,
        end_date=end_date,
    )


def probe_official_sources(start_date: date, end_date: date) -> dict[str, object]:
    sources = fetch_official_sources(start_date, end_date)
    by_name = {source.name: source for source in sources}
    parsed = {
        "csi300_total_return": parse_csi300_levels(
            by_name["csi300_total_return"].data
        ),
        "nasdaq100_total_return": parse_nasdaq100_levels(
            by_name["nasdaq100_total_return"].data
        ),
        "usd_cny": parse_usd_cny_levels(by_name["usd_cny"].data),
    }
    rows = build_benchmark_rows(
        csi_levels=parsed["csi300_total_return"],
        nasdaq_levels=parsed["nasdaq100_total_return"],
        usd_cny_levels=parsed["usd_cny"],
        start_date=start_date,
        end_date=end_date,
    )
    counts = {
        benchmark_id: sum(row["benchmark_id"] == benchmark_id for row in rows)
        for benchmark_id in BENCHMARK_IDS
    }
    if any(value == 0 for value in counts.values()):
        raise BenchmarkSetError("official sources have no comparable rows")
    return {
        "status": "complete",
        "requested_range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        },
        "source_rows": {name: len(values) for name, values in parsed.items()},
        "benchmark_rows": counts,
        "source_sha256": {source.name: source.sha256 for source in sources},
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Build official total-return benchmarks")
    parser.add_argument("command", choices=("probe", "build"))
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--market-data-root", type=Path, default=Path(".local/market-data")
    )
    args = parser.parse_args()
    try:
        if args.command == "probe":
            result: object = probe_official_sources(args.start_date, args.end_date)
        else:
            created = build_official_benchmark_set(
                args.market_data_root, args.start_date, args.end_date
            )
            result = {
                "status": "complete",
                "benchmark_set_id": created.benchmark_set_id,
                "root": str(created.root),
            }
    except BenchmarkSetError as exc:
        print(
            json.dumps(
                {"status": "evidence_insufficient", "reason": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
