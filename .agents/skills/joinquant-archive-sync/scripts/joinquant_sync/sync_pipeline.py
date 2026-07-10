from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow.parquet as pq
from playwright.sync_api import Page

from .archive import (
    AttributionIncomplete,
    IntegrityError,
    commit_manifest,
    detect_attribution_writer,
    evaluate_gate,
    expected_datasets,
    object_lock,
    recover_malformed_json,
    validate_attribution,
    verify_existing_manifest,
    verify_existing_manifest_files,
    write_code_context,
    write_raw_gzip,
)
from .browser import (
    FreeLogIncomplete,
    TargetDiscoveryError,
    discover_all_simulations,
    discover_history_targets,
    discover_active_simulations,
    fetch_backtest_browser_evidence,
    fetch_strategy_default_code,
    fetch_simulation_browser_evidence,
    inspect_simulation_status,
)
from .query import write_parquet
from .research_cloud import fetch_research_backtest


INDEX_FIELDS = [
    "strategy_id",
    "name",
    "joinquant_strategy_url",
    "status",
    "current_default_code",
    "latest_backtest_id",
    "latest_simulation_id",
    "updated_at",
]


def _load_existing_for_sync(
    object_dir: Path,
) -> tuple[dict[str, object], bool]:
    try:
        return verify_existing_manifest(object_dir), True
    except IntegrityError:
        return verify_existing_manifest_files(object_dir), False


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def persist_failure_evidence(
    repository: Path, error: BaseException, *, identity: str
) -> dict[str, object] | None:
    raw_pages = getattr(error, "raw_pages", None)
    if not isinstance(raw_pages, list) or not raw_pages:
        return None
    payload = json.dumps(
        {
            "identity": identity,
            "observed_at": _now(),
            "error": type(error).__name__,
            "message": str(error),
            "raw_pages": raw_pages,
            "recovery": [
                {
                    "page": index,
                    "rows": recovered,
                    "errors": errors,
                }
                for index, page in enumerate(raw_pages)
                for recovered, errors in [
                    recover_malformed_json(
                        str(page.get("raw_text") or "").encode("utf-8")
                    )
                ]
                if page.get("raw_text")
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    path = repository / ".local" / "joinquant-sync" / "failures" / f"{digest}.json.gz"
    evidence = write_raw_gzip(payload, path)
    return {
        "path": str(path),
        "sha256": evidence["compressed_sha256"],
        "raw_sha256": evidence["sha256"],
        "raw_pages": len(raw_pages),
    }


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _target_fingerprint(target: dict[str, object]) -> dict[str, object]:
    return {
        key: target.get(key)
        for key in ("page_ordinal", "name", "status", "created_at", "date_range")
    }


def _backtest_browser_fingerprint(browser: dict[str, object]) -> str:
    return _canonical_sha256(
        {
            "code": hashlib.sha256(str(browser["code"]).encode("utf-8")).hexdigest(),
            "normal_log": hashlib.sha256(bytes(browser["normal_log"])).hexdigest(),
            "official_summary": hashlib.sha256(
                bytes(browser["official_summary"])
            ).hexdigest(),
            "params": browser.get("params") or {},
        }
    )


def _read_strategy_index(index_path: Path) -> list[dict[str, str]]:
    if not index_path.is_file():
        return []
    with index_path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _strategy_id(index_path: Path, name: str, url: str) -> str:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with object_lock(index_path.parent):
        rows = _read_strategy_index(index_path)
        matches = [row for row in rows if row.get("name") == name]
        if len(matches) > 1:
            raise IntegrityError(f"duplicate strategy index name: {name}")
        if matches:
            row = matches[0]
            row["joinquant_strategy_url"] = url
            strategy_id = row["strategy_id"]
        else:
            used = {
                int(match.group(1))
                for row in rows
                if (
                    match := re.fullmatch(r"strategy-(\d+)", row.get("strategy_id", ""))
                )
            }
            number = next(
                value for value in range(1, len(used) + 2) if value not in used
            )
            strategy_id = f"strategy-{number:03d}"
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "name": name,
                    "joinquant_strategy_url": url,
                    "status": "active",
                    "current_default_code": f"joinquant/strategies/{strategy_id}/default_code.py",
                    "latest_backtest_id": "",
                    "latest_simulation_id": "",
                    "updated_at": _now(),
                }
            )
        for row in rows:
            if row.get("strategy_id") == strategy_id:
                row["updated_at"] = _now()
        temporary = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=INDEX_FIELDS, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(rows)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, index_path)
        finally:
            temporary.unlink(missing_ok=True)
    return strategy_id


def _update_strategy_latest(
    index_path: Path, strategy_id: str, field: str, local_id: str
) -> None:
    if field not in {"latest_backtest_id", "latest_simulation_id"}:
        raise ValueError(f"unsupported strategy index field: {field}")
    with object_lock(index_path.parent):
        rows = _read_strategy_index(index_path)
        matches = [row for row in rows if row.get("strategy_id") == strategy_id]
        if len(matches) != 1:
            raise IntegrityError(f"strategy index identity is missing: {strategy_id}")
        matches[0][field] = local_id
        matches[0]["updated_at"] = _now()
        temporary = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=INDEX_FIELDS, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(rows)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, index_path)
        finally:
            temporary.unlink(missing_ok=True)


def _write_strategy(strategy_dir: Path, identity: dict[str, object]) -> None:
    code = str(identity["code"])
    payload = code.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    with object_lock(strategy_dir):
        version = strategy_dir / "code_versions" / f"{digest}.py"
        version.parent.mkdir(parents=True, exist_ok=True)
        if version.is_file():
            if hashlib.sha256(version.read_bytes()).hexdigest() != digest:
                raise IntegrityError(
                    f"strategy code version hash mismatch: {version.name}"
                )
        else:
            temporary_version = version.with_name(
                f".{version.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with temporary_version.open("wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_version, version)
            finally:
                temporary_version.unlink(missing_ok=True)
        existing_aliases: list[dict[str, object]] = []
        existing_manifest = strategy_dir / "manifest.json"
        if existing_manifest.is_file():
            try:
                previous = json.loads(existing_manifest.read_text(encoding="utf-8"))
                aliases = previous.get("source", {}).get("aliases", [])
                if isinstance(aliases, list):
                    existing_aliases = [
                        item for item in aliases if isinstance(item, dict)
                    ]
            except (json.JSONDecodeError, AttributeError):
                raise IntegrityError("existing strategy manifest is invalid") from None
        current_alias = {"url": identity["edit_url"]}
        aliases = [
            current_alias,
            *[item for item in existing_aliases if item != current_alias],
        ]
        manifest = {
            "schema_version": 1,
            "object": {
                "kind": "strategy",
                "local_id": strategy_dir.name,
                "status": "active",
                "name": identity["name"],
            },
            "source": {
                "url": identity["edit_url"],
                "aliases": aliases,
                "observed_at": _now(),
            },
            "fence": {"before_sha256": digest, "after_sha256": digest},
            "code": {
                "path": f"code_versions/{digest}.py",
                "sha256": digest,
                "bytes": len(payload),
                "versions": [
                    {
                        "path": f"code_versions/{digest}.py",
                        "sha256": digest,
                        "bytes": len(payload),
                    }
                ],
            },
            "datasets": {
                "page_metadata": {
                    "required": True,
                    "status": "complete",
                    "rows": 0,
                    "verified_empty": True,
                }
            },
            "gate": {"status": "pass", "exceptions": []},
        }
        target = strategy_dir / "manifest.json"
        temporary = strategy_dir / f"manifest.json.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        default_code = strategy_dir / "default_code.py"
        temporary_default = strategy_dir / f"default_code.py.{uuid.uuid4().hex}.tmp"
        try:
            with temporary_default.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_default, default_code)
        finally:
            temporary_default.unlink(missing_ok=True)
        verify_existing_manifest(strategy_dir)


def _rows(value: object, dataset: str) -> list[dict[str, object]]:
    if dataset == "risk" and isinstance(value, dict):
        value = [value] if value else []
    elif dataset == "period_risks" and isinstance(value, dict):
        value = [
            {
                "metric": str(name),
                "payload_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
            }
            for name, item in value.items()
        ]
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise IntegrityError(f"{dataset} contains a non-object row")
        row = item
        normalized.append(
            {
                str(key): (
                    json.dumps(cell, ensure_ascii=False, sort_keys=True)
                    if isinstance(cell, (dict, list, tuple))
                    else cell
                )
                for key, cell in row.items()
            }
        )
    return normalized


def _gzip_record(raw: bytes, path: Path, root: Path, format_: str) -> dict[str, object]:
    evidence = write_raw_gzip(raw, path)
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": evidence["compressed_sha256"],
        "bytes": evidence["bytes"],
        "raw_sha256": evidence["sha256"],
        "raw_bytes": evidence["raw_bytes"],
        "format": format_,
    }


def _stage_research_response(
    stage: Path,
    research: dict[str, object],
    datasets: dict[str, dict[str, object]],
    *,
    root: Path | None = None,
) -> tuple[dict[str, object], Path]:
    raw = research.get("raw")
    if not isinstance(raw, bytes) or not raw:
        raise IntegrityError("original research response is missing")
    archive_root = root or stage
    path = stage / "raw" / "research-response.json.gz"
    record = _gzip_record(raw, path, archive_root, "json.gz")
    for name in (
        "results",
        "balances",
        "positions",
        "orders",
        "records",
        "risk",
        "period_risks",
    ):
        pagination = datasets[name].get("pagination")
        if not isinstance(pagination, dict):
            raise IntegrityError(f"structured pagination is missing: {name}")
        pagination["research_response_sha256"] = record["sha256"]
    return record, path


def _stage_structured(
    stage: Path,
    bundle: dict[str, object],
    datasets: dict[str, dict[str, object]],
    *,
    root: Path | None = None,
) -> list[Path]:
    archive_root = root or stage
    staged: list[Path] = []
    for name in (
        "results",
        "balances",
        "positions",
        "orders",
        "records",
        "risk",
        "period_risks",
    ):
        if name not in bundle or bundle[name] is None:
            datasets[name].update(
                status="failed", evidence={"missing_source_key": True}
            )
            continue
        value = bundle[name]
        if isinstance(value, dict) and value.get("__error__"):
            datasets[name].update(
                status="failed", evidence={"source_error": value["__error__"]}
            )
            continue
        expected = (dict,) if name in {"risk", "period_risks"} else (list,)
        if not isinstance(value, expected):
            raise IntegrityError(f"{name} source value has an invalid type")
        rows = _rows(value, name)
        raw_path = stage / "raw" / f"{name}.json.gz"
        raw = _gzip_record(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            raw_path,
            archive_root,
            "json.gz",
        )
        staged.append(raw_path)
        files: list[dict[str, object]] = [raw]
        if rows:
            parquet_path = stage / "data" / f"{name}.parquet"
            files.append(write_parquet(rows, parquet_path, root=archive_root))
            staged.append(parquet_path)
        evidence = _fact_evidence(name, rows)
        datasets[name].update(
            status="complete",
            rows=len(rows),
            files=files,
            verified_empty=not rows,
            evidence=evidence,
            time_range=evidence["time_range"],
            pagination={
                "mode": "single_complete_method_return",
                "source": f"get_backtest.{name}",
                "terminal": True,
            },
        )
    return staged


def _time_order(rows: list[dict[str, object]]) -> str:
    values = [str(row["time"]) for row in rows if row.get("time") not in {None, ""}]
    if not values:
        return "not_applicable"
    return "nondecreasing" if values == sorted(values) else "unsorted"


def _fact_evidence(dataset: str, rows: list[dict[str, object]]) -> dict[str, object]:
    fields = sorted({key for row in rows for key in row})
    time_values = [
        str(row["time"]) for row in rows if row.get("time") not in {None, ""}
    ]
    time_order = _time_order(rows)
    if time_order == "unsorted":
        raise IntegrityError(f"{dataset} time order is not nondecreasing")
    key_candidates = {
        "results": ("time",),
        "balances": ("time",),
        "positions": ("time", "pindex", "security", "side"),
        "orders": (),
        "records": ("time",),
        "period_risks": ("metric",),
    }
    keys = key_candidates.get(dataset, ())
    if rows and keys:
        if not set(keys).issubset(fields):
            raise IntegrityError(f"{dataset} unique key fields are missing")
        values = [tuple(row.get(key) for key in keys) for row in rows]
        if len(values) != len(set(values)):
            raise IntegrityError(f"{dataset} unique key is duplicated")
    if dataset == "risk" and len(rows) > 1:
        raise IntegrityError("risk contains more than one summary row")
    return {
        "source_contract": "JoinQuant get_backtest full object or cloud-filtered increment",
        "source_key_present": True,
        "row_shape": "objects",
        "fields": fields,
        "unique_key": (
            list(keys)
            if keys
            else (
                [
                    "entrust_time",
                    "pindex",
                    "security",
                    "side",
                    "amount",
                    "price",
                    "source_occurrence",
                ]
                if dataset == "orders"
                else ["single_summary_row"]
            )
        ),
        "unique": dataset != "orders",
        "time_order": time_order,
        "time_range": (
            {"start": min(time_values), "end": max(time_values)}
            if time_values
            else None
        ),
    }


def _validate_fact_relations(
    bundle: dict[str, object],
    datasets: dict[str, dict[str, object]],
    params: dict[str, object],
) -> None:
    def dates(name: str) -> set[str]:
        value = bundle.get(name)
        if not isinstance(value, list):
            return set()
        return {
            str(row["time"])[:10]
            for row in value
            if isinstance(row, dict) and row.get("time") not in {None, ""}
        }

    result_dates = dates("results")
    start = str(params.get("start_date") or "")[:10]
    end = str(params.get("end_date") or "")[:10]
    for name in _INCREMENTAL_TABLES:
        observed = dates(name)
        if observed and start and min(observed) < start:
            raise IntegrityError(f"{name} time starts before the configured range")
        if observed and end and max(observed) > end:
            raise IntegrityError(f"{name} time ends after the configured range")
        if (
            name != "results"
            and observed
            and result_dates
            and not observed <= result_dates
        ):
            raise IntegrityError(f"{name} time is outside results trading dates")
        dataset = datasets.get(name)
        if isinstance(dataset, dict):
            evidence = dataset.setdefault("evidence", {})
            if isinstance(evidence, dict):
                evidence["configured_range"] = {
                    "start": start or None,
                    "end": end or None,
                }
                evidence["trading_day_association"] = (
                    "results_dates" if observed and result_dates else "not_applicable"
                )


def _validate_run_semantics(
    kind: str,
    status: str,
    datasets: dict[str, dict[str, object]],
) -> None:
    if status not in {"done", "active", "closed"}:
        return
    for name in ("results", "balances", "risk"):
        dataset = datasets.get(name)
        if not isinstance(dataset, dict) or int(dataset.get("rows") or 0) < 1:
            raise IntegrityError(f"{kind} {status} core dataset {name} is empty")
        pagination = dataset.get("pagination")
        if not isinstance(pagination, dict) or pagination.get("terminal") is not True:
            raise IntegrityError(
                f"{kind} {status} dataset {name} lacks terminal evidence"
            )


_INCREMENTAL_TABLES = ("results", "balances", "positions", "orders", "records")


def _manifest_time_cursors(manifest: dict[str, object] | None) -> dict[str, str]:
    if not manifest:
        return {}
    streams = manifest.get("streams")
    data = streams.get("data") if isinstance(streams, dict) else None
    cursors = data.get("cursors") if isinstance(data, dict) else None
    if not isinstance(cursors, dict):
        return {}
    return {
        str(name): str(value)
        for name, value in cursors.items()
        if name in _INCREMENTAL_TABLES and isinstance(value, str) and value
    }


def _bundle_time_cursors(
    bundle: dict[str, object], previous: dict[str, str]
) -> dict[str, str]:
    cursors = dict(previous)
    for name in _INCREMENTAL_TABLES:
        value = bundle.get(name)
        if not isinstance(value, list):
            continue
        times = [
            str(row["time"])
            for row in value
            if isinstance(row, dict) and row.get("time") not in {None, ""}
        ]
        if times:
            newest = max(times)
            if newest > cursors.get(name, ""):
                cursors[name] = newest
    return cursors


def _fact_key_fields(dataset: str, rows: list[dict[str, object]]) -> tuple[str, ...]:
    candidates = {
        "results": ("time",),
        "balances": ("time",),
        "positions": ("time", "pindex", "security", "side"),
        "orders": ("entrust_time", "pindex", "security", "side", "amount", "price"),
        "records": ("time",),
    }
    selected = candidates[dataset]
    fields = {key for row in rows for key in row}
    if dataset == "orders" and not set(selected).issubset(fields):
        selected = ("time", "pindex", "security", "side")
    if rows and not set(selected).issubset(fields):
        raise IntegrityError(f"{dataset} incremental key fields are missing")
    return selected


def _merge_fact_rows(
    dataset: str,
    previous: list[dict[str, object]],
    current: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows = [*previous, *current]
    keys = _fact_key_fields(dataset, rows)
    previous_groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
    current_groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row in previous:
        previous_groups.setdefault(tuple(row.get(key) for key in keys), []).append(row)
    for row in current:
        current_groups.setdefault(tuple(row.get(key) for key in keys), []).append(row)
    merged: list[dict[str, object]] = []
    for key in [
        *previous_groups,
        *[item for item in current_groups if item not in previous_groups],
    ]:
        merged.extend(current_groups.get(key, previous_groups.get(key, [])))
    return sorted(
        merged,
        key=lambda row: (
            str(row.get("time") or row.get("entrust_time") or ""),
            *(str(row.get(key) or "") for key in keys),
        ),
    )


def _compact_incremental_bundle(
    bundle: dict[str, object],
    previous: dict[str, object] | None,
    object_dir: Path | None,
) -> None:
    if not previous or object_dir is None:
        return
    metadata = bundle.get("metadata")
    prior_datasets = previous.get("datasets")
    if not isinstance(metadata, dict) or not isinstance(prior_datasets, dict):
        return
    after = metadata.get("incremental_after")
    modes = metadata.get("transfer_modes")
    if not isinstance(after, dict) or not isinstance(modes, dict):
        return
    for name in _INCREMENTAL_TABLES:
        if not after.get(name) or modes.get(name) != "after_time_overlap":
            continue
        prior = prior_datasets.get(name)
        current_value = bundle.get(name)
        if not isinstance(prior, dict) or not isinstance(current_value, list):
            raise IntegrityError(f"incremental dataset is invalid: {name}")
        previous_rows: list[dict[str, object]] = []
        for item in prior.get("files") or []:
            if not isinstance(item, dict) or item.get("format") != "parquet":
                continue
            previous_rows.extend(
                pq.read_table(object_dir / str(item["path"])).to_pylist()
            )
        bundle[name] = _merge_fact_rows(name, previous_rows, _rows(current_value, name))
        modes[name] = "compacted_after_time_overlap"


def _read_log_records(raw: bytes) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(item, dict) or type(item.get("offset")) is not int:
            return []
        records.append(item)
    return records


def _merge_simulation_log(
    browser: dict[str, object],
    previous: dict[str, object] | None,
    object_dir: Path | None,
) -> None:
    current = browser.get("normal_log_records")
    if not isinstance(current, list) or not all(
        isinstance(item, dict) for item in current
    ):
        current = [
            {"offset": index, "text": line.decode("utf-8", errors="replace")}
            for index, line in enumerate(bytes(browser["normal_log"]).splitlines())
        ]
    prior: list[dict[str, object]] = []
    prior_pages: list[dict[str, object]] = []
    if previous and object_dir is not None:
        datasets = previous.get("datasets")
        normal = datasets.get("normal_log") if isinstance(datasets, dict) else None
        if isinstance(normal, dict):
            candidates = [
                item
                for item in normal.get("files") or []
                if isinstance(item, dict)
                and item.get("format") == "jsonl.gz"
                and "pages" not in str(item.get("path"))
            ]
            if candidates:
                with gzip.open(
                    object_dir / str(candidates[-1]["path"]), "rb"
                ) as stream:
                    prior = _read_log_records(stream.read())
            page_candidates = [
                item
                for item in normal.get("files") or []
                if isinstance(item, dict)
                and item.get("format") == "json.gz"
                and "normal-log-pages" in str(item.get("path"))
            ]
            if page_candidates:
                with gzip.open(
                    object_dir / str(page_candidates[-1]["path"]),
                    "rt",
                    encoding="utf-8",
                ) as stream:
                    value = json.load(stream)
                if isinstance(value, list):
                    prior_pages = [item for item in value if isinstance(item, dict)]
    merged = {
        int(item["offset"]): item
        for item in [*prior, *current]
        if type(item.get("offset")) is int
    }
    records = [merged[offset] for offset in sorted(merged)]
    offsets = list(merged)
    contiguous = bool(offsets) and sorted(offsets) == list(
        range(min(offsets), max(offsets) + 1)
    )
    complete_from_start = contiguous and min(offsets) == 0
    observed_status = str(browser["normal_log_status"])
    status = "complete" if complete_from_start else observed_status
    current_pages = [
        item
        for item in browser.get("normal_log_raw_pages") or []
        if isinstance(item, dict)
    ]
    successful_pages: dict[int, dict[str, object]] = {}
    for page in [*prior_pages, *current_pages]:
        cursor = page.get("offset", page.get("cursor"))
        if type(cursor) is int and page.get("blocked_free") is not True:
            successful_pages[int(cursor)] = page
    merged_pages = [successful_pages[key] for key in sorted(successful_pages)]
    if status == "capped_free":
        merged_pages.extend(
            page for page in current_pages if page.get("blocked_free") is True
        )
    browser["normal_log_records"] = records
    browser["normal_log_rows"] = len(records)
    browser["normal_log_status"] = status
    browser["normal_log_raw_pages"] = merged_pages
    browser["normal_log"] = (
        "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in records
        )
        + ("\n" if records else "")
    ).encode("utf-8")


def _stage_attribution(
    stage: Path,
    raw: bytes,
    datasets: dict[str, dict[str, object]],
    checked: dict[str, object],
    *,
    root: Path | None = None,
) -> list[Path]:
    archive_root = root or stage
    rows: list[dict[str, object]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise AttributionIncomplete(
                f"attribution line {number} is invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise AttributionIncomplete(f"attribution line {number} is not an object")
        rows.extend(_rows([value], "attribution_log"))
    if len(rows) != int(checked.get("rows") or 0):
        raise AttributionIncomplete("attribution validation row count changed")
    raw_path = stage / "raw" / "attribution-log.jsonl.gz"
    parquet_path = stage / "data" / "attribution_log.parquet"
    files = [_gzip_record(raw, raw_path, archive_root, "jsonl.gz")]
    staged = [raw_path]
    if rows:
        files.append(write_parquet(rows, parquet_path, root=archive_root))
        staged.append(parquet_path)
    datasets["attribution_log"].update(
        status="complete",
        rows=len(rows),
        files=files,
        evidence=checked,
        verified_empty=not rows,
    )
    return staged


def _ensure_queryable_attribution(
    object_dir: Path, manifest: dict[str, object]
) -> dict[str, object]:
    datasets = manifest.get("datasets")
    attribution = (
        datasets.get("attribution_log") if isinstance(datasets, dict) else None
    )
    if not isinstance(attribution, dict) or attribution.get("status") != "complete":
        return manifest
    files = attribution.get("files")
    if not isinstance(files, list):
        raise IntegrityError("attribution file list is invalid")
    if any(
        isinstance(item, dict) and item.get("format") == "parquet" for item in files
    ):
        return manifest
    raw_items = [
        item
        for item in files
        if isinstance(item, dict) and item.get("format") == "jsonl.gz"
    ]
    if len(raw_items) != 1:
        raise AttributionIncomplete(
            "queryable attribution migration requires one raw file"
        )
    raw_path = object_dir / str(raw_items[0]["path"])
    with gzip.open(raw_path, "rb") as stream:
        raw = stream.read()
    status = str((manifest.get("object") or {}).get("status") or "active")
    code_record = manifest.get("code")
    if not isinstance(code_record, dict) or not isinstance(
        code_record.get("params"), dict
    ):
        raise AttributionIncomplete(
            "queryable attribution migration lacks code context"
        )
    code_text = (object_dir / str(code_record["path"])).read_text(encoding="utf-8")
    writer = detect_attribution_writer(code_text)
    params = json.loads(
        (object_dir / str(code_record["params"]["path"])).read_text(encoding="utf-8")
    )
    checked = validate_attribution(
        raw.splitlines(),
        status if status != "closed" else "done",
        True,
        expected_token=str((writer.get("evidence") or {}).get("token") or ""),
        expected_path=str(writer.get("path") or ""),
        expected_start=str(params.get("start_date") or ""),
        expected_end=str(params.get("end_date") or ""),
    )
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    staging_root = (
        object_dir.parent / f".{object_dir.name}-attribution-{uuid.uuid4().hex}"
    )
    staging_root.mkdir(parents=True)
    try:
        parquet_path = staging_root / "data" / "attribution_log.parquet"
        record = write_parquet(
            _rows(rows, "attribution_log"), parquet_path, root=staging_root
        )
        attribution["files"] = [*files, record]
        attribution["evidence"] = checked
        manifest["gate"] = evaluate_gate(datasets)
        commit_manifest(object_dir, manifest, [parquet_path])
    finally:
        if staging_root.exists():
            for path in sorted(staging_root.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            staging_root.rmdir()
    return verify_existing_manifest(object_dir)


def commit_paid_log_supplement(
    object_dir: Path,
    preview: dict[str, object],
    source_archive: Path,
    selected: dict[str, object],
    remote: dict[str, object],
) -> dict[str, object]:
    manifest = verify_existing_manifest(object_dir)
    datasets = manifest["datasets"]
    normal = datasets.get("normal_log")
    if not isinstance(normal, dict) or normal.get("status") != "capped_free":
        raise IntegrityError("paid log supplement requires capped_free normal_log")
    try:
        source_relative = (
            source_archive.resolve().relative_to(object_dir.resolve()).as_posix()
        )
        selected_path = Path(str(selected["path"]))
        selected_relative = (
            selected_path.resolve().relative_to(object_dir.resolve()).as_posix()
        )
    except ValueError:
        raise IntegrityError(
            "paid log files must stay inside the archive object"
        ) from None
    source_record = {
        "path": source_relative,
        "sha256": str(remote["sha256"]),
        "bytes": int(remote["bytes"]),
        "format": "zip",
    }
    selected_record = {
        "path": selected_relative,
        "sha256": str(selected["sha256"]),
        "bytes": int(selected["bytes"]),
        "rows": int(selected["rows"]),
        "format": "jsonl.gz",
    }
    files = normal.setdefault("files", [])
    if not isinstance(files, list):
        raise IntegrityError("normal_log files are invalid")
    for record in (source_record, selected_record):
        if not any(
            isinstance(item, dict) and item.get("path") == record["path"]
            for item in files
        ):
            files.append(record)
    evidence = normal.setdefault("evidence", {})
    if not isinstance(evidence, dict):
        raise IntegrityError("normal_log evidence is invalid")
    supplements = evidence.setdefault("paid_supplements", [])
    if not isinstance(supplements, list):
        raise IntegrityError("paid supplement evidence is invalid")
    supplements.append(
        {
            "preview_id": preview["preview_id"],
            "confirmed": True,
            "quote": preview["quote"],
            "requested_range": selected["requested_range"],
            "actual_range": selected["actual_range"],
            "source_sha256": remote["sha256"],
            "selected_sha256": selected["sha256"],
        }
    )
    manifest["gate"] = evaluate_gate(datasets)
    commit_manifest(object_dir, manifest, [])
    return verify_existing_manifest(object_dir)


def _stage_error_log(
    stage: Path,
    status: str,
    normal_log: bytes,
    normal_log_status: str,
    datasets: dict[str, dict[str, object]],
) -> list[Path]:
    if status not in {"failed", "cancelled"}:
        return []
    if status == "failed" and normal_log_status != "complete":
        datasets["error_log"].update(
            status="failed",
            evidence={"normal_log_status": normal_log_status},
        )
        raise IntegrityError(
            "failed backtest requires a complete normal log for error evidence"
        )
    text = normal_log.decode("utf-8", errors="replace")
    errors = max(text.count(" - ERROR - "), text.count("Traceback"))
    if errors == 0:
        if status == "failed":
            datasets["error_log"].update(
                status="failed", evidence={"error_marker_found": False}
            )
            raise IntegrityError("failed backtest error log is missing")
        datasets["error_log"].update(
            status="missing_at_source",
            evidence={"run_status": "cancelled", "error_marker_found": False},
        )
        return []
    digest = hashlib.sha256(normal_log).hexdigest()[:24]
    path = stage / "raw" / f"error-log-{digest}.jsonl.gz"
    record = _gzip_record(normal_log, path, stage, "text-log.gz")
    datasets["error_log"].update(
        status="complete",
        rows=errors,
        files=[record],
        evidence={
            "derived_from": "complete free normal log",
            "error_marker_found": True,
        },
    )
    return [path]


def _version_simulation_code_context(
    stage: Path, code: dict[str, object]
) -> list[Path]:
    staged: list[Path] = []
    mappings = (
        (code, "path", "current_code.py", "code_versions", ".py"),
        (code["params"], "path", "params.json", "params_versions", ".json"),
        (code["source"], "path", "source.json", "source_versions", ".json"),
    )
    for record, key, old_name, directory, suffix in mappings:
        assert isinstance(record, dict)
        source = stage / old_name
        digest = str(record["sha256"])
        relative = f"{directory}/{digest}{suffix}"
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        record[key] = relative
        staged.append(destination)
    for item in code.get("versions") or []:
        if not isinstance(item, dict):
            raise IntegrityError("simulation code version record is invalid")
        path = stage / str(item["path"])
        if path not in staged:
            staged.append(path)
    return staged


def _update_simulation_pointers(object_dir: Path, manifest: dict[str, object]) -> None:
    code = manifest.get("code")
    if not isinstance(code, dict):
        raise IntegrityError("simulation code manifest is invalid")
    records = (
        (code, "current_code.py"),
        (code.get("params"), "params.json"),
        (code.get("source"), "source.json"),
    )
    with object_lock(object_dir):
        for record, target_name in records:
            if not isinstance(record, dict) or not record.get("path"):
                raise IntegrityError(f"simulation {target_name} record is missing")
            source = object_dir / str(record["path"])
            payload = source.read_bytes()
            expected = record.get("sha256")
            if expected and hashlib.sha256(payload).hexdigest() != expected:
                raise IntegrityError(f"simulation {target_name} source hash mismatch")
            temporary = object_dir / f".{target_name}.{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, object_dir / target_name)
            finally:
                temporary.unlink(missing_ok=True)


def _cleanup_unreferenced_snapshots(
    object_dir: Path, manifest: dict[str, object]
) -> None:
    datasets = manifest.get("datasets")
    referenced: set[str] = set()
    if isinstance(datasets, dict):
        for dataset in datasets.values():
            if not isinstance(dataset, dict):
                continue
            for item in dataset.get("files") or []:
                if not isinstance(item, dict):
                    continue
                parts = Path(str(item.get("path") or "")).parts
                if len(parts) > 1 and parts[0] == "snapshots":
                    referenced.add(parts[1])
    for item in manifest.get("research_lineage") or []:
        if isinstance(item, dict):
            parts = Path(str(item.get("path") or "")).parts
            if len(parts) > 1 and parts[0] == "snapshots":
                referenced.add(parts[1])
    root = object_dir / "snapshots"
    if not root.is_dir():
        return
    for path in root.iterdir():
        if path.is_dir() and path.name not in referenced:
            resolved = path.resolve()
            resolved.relative_to(root.resolve())
            shutil.rmtree(resolved)


def _backtest_params(
    target: dict[str, object],
    browser: dict[str, object],
    bundle: dict[str, object],
) -> dict[str, object]:
    research_params = bundle.get("params")
    if not isinstance(research_params, dict) or research_params.get("__error__"):
        raise IntegrityError("research backtest parameters are missing")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", str(target.get("date_range") or ""))
    browser_params = dict(browser.get("params") or {})
    start = str(browser_params.get("start_date") or (dates[0] if dates else ""))
    end = str(browser_params.get("end_date") or (dates[1] if len(dates) > 1 else ""))
    if not start or not end:
        raise IntegrityError("backtest configured date range is missing")
    return {
        "start_date": start,
        "end_date": end,
        "research_params": research_params,
        "research_status": bundle.get("status"),
        "research_metadata": {
            key: value
            for key, value in (bundle.get("metadata") or {}).items()
            if key not in {"backtest_id", "generated_at"}
        },
    }


def _version_backtest_params(stage: Path, code: dict[str, object]) -> Path:
    record = code.get("params")
    if not isinstance(record, dict):
        raise IntegrityError("backtest parameter record is missing")
    source = stage / "params.json"
    digest = str(record["sha256"])
    relative = f"params_versions/{digest}.json"
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    record["path"] = relative
    code["params_path"] = relative
    return destination


def _backtest_params_complete(object_dir: Path, manifest: dict[str, object]) -> bool:
    code = manifest.get("code")
    record = code.get("params") if isinstance(code, dict) else None
    if not isinstance(record, dict) or not record.get("path"):
        return False
    try:
        params = json.loads(
            (object_dir / str(record["path"])).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(params, dict)
        and params.get("start_date")
        and params.get("end_date")
        and isinstance(params.get("research_params"), dict)
        and not {
            "backtest_id",
            "generated_at",
        }
        & set(params.get("research_metadata") or {})
    )


def _backtest_identity_matches(
    object_dir: Path,
    manifest: dict[str, object],
    target: dict[str, object],
    remote_code: str,
) -> bool:
    code = manifest.get("code")
    if (
        not isinstance(code, dict)
        or code.get("sha256") != hashlib.sha256(remote_code.encode("utf-8")).hexdigest()
    ):
        return False
    if not _backtest_params_complete(object_dir, manifest):
        return False
    record = code.get("params")
    assert isinstance(record, dict)
    params = json.loads((object_dir / str(record["path"])).read_text(encoding="utf-8"))
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", str(target.get("date_range") or ""))
    return (
        len(dates) == 2 and [params.get("start_date"), params.get("end_date")] == dates
    )


def _backtest_archive_current(object_dir: Path, manifest: dict[str, object]) -> bool:
    code = manifest.get("code")
    datasets = manifest.get("datasets")
    if not isinstance(code, dict) or not isinstance(code.get("source_response"), dict):
        return False
    if not isinstance(datasets, dict):
        return False
    for name in (
        "results",
        "balances",
        "positions",
        "orders",
        "records",
        "risk",
        "period_risks",
    ):
        dataset = datasets.get(name)
        pagination = dataset.get("pagination") if isinstance(dataset, dict) else None
        if not isinstance(pagination, dict) or pagination.get("terminal") is not True:
            return False
    summary = datasets.get("official_summary")
    results = datasets.get("results")
    if not isinstance(summary, dict) or not isinstance(results, dict):
        return False
    if summary.get("rows") != results.get("rows"):
        return False
    normal = datasets.get("normal_log")
    files = normal.get("files") if isinstance(normal, dict) else None
    if not isinstance(files, list) or not any(
        isinstance(item, dict) and "normal-log-pages" in str(item.get("path"))
        for item in files
    ):
        return False
    log_files = [
        item
        for item in files
        if isinstance(item, dict)
        and item.get("format") == "jsonl.gz"
        and "pages" not in str(item.get("path"))
    ]
    if len(log_files) != 1:
        return False
    with gzip.open(object_dir / str(log_files[0]["path"]), "rb") as stream:
        return bool(_read_log_records(stream.read()))


def _update_backtest_params_pointer(
    object_dir: Path, manifest: dict[str, object]
) -> None:
    code = manifest.get("code")
    record = code.get("params") if isinstance(code, dict) else None
    if not isinstance(record, dict) or not record.get("path"):
        raise IntegrityError("backtest parameter record is missing")
    payload = (object_dir / str(record["path"])).read_bytes()
    if hashlib.sha256(payload).hexdigest() != record.get("sha256"):
        raise IntegrityError("backtest parameter version hash mismatch")
    with object_lock(object_dir):
        temporary = object_dir / f".params.json.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, object_dir / "params.json")
        finally:
            temporary.unlink(missing_ok=True)


def _cleanup_unreferenced_backtest_files(
    object_dir: Path, manifest: dict[str, object]
) -> None:
    referenced: set[str] = set()
    code = manifest.get("code")
    if isinstance(code, dict):
        for item in [
            code,
            code.get("params"),
            code.get("source_response"),
        ]:
            if isinstance(item, dict) and item.get("path"):
                referenced.add(str(item["path"]))
    datasets = manifest.get("datasets")
    if isinstance(datasets, dict):
        for dataset in datasets.values():
            if isinstance(dataset, dict):
                referenced.update(
                    str(item["path"])
                    for item in dataset.get("files") or []
                    if isinstance(item, dict) and item.get("path")
                )
    candidates = [
        *(object_dir / "raw").glob("normal-log*.gz"),
        *(object_dir / "params_versions").glob("*.json"),
    ]
    for path in candidates:
        if path.relative_to(object_dir).as_posix() not in referenced:
            path.unlink()


def _build_backtest_batch(
    stage: Path,
    target: dict[str, object],
    browser: dict[str, object],
    research: dict[str, object],
    attribution: dict[str, object],
) -> tuple[dict[str, object], list[Path]]:
    status = str(target["status"])
    writer_present = bool(attribution["writer_present"])
    datasets = expected_datasets("backtest", status, writer_present)
    bundle = research["bundle"]
    if not isinstance(bundle, dict):
        raise IntegrityError("research bundle is invalid")
    params = _backtest_params(target, browser, bundle)
    staged = _stage_structured(stage, bundle, datasets)
    research_response, research_response_path = _stage_research_response(
        stage, research, datasets
    )
    staged.append(research_response_path)
    _validate_fact_relations(bundle, datasets, params)
    _validate_run_semantics("backtest", status, datasets)

    summary_path = stage / "reports" / "official-summary.csv"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(bytes(browser["official_summary"]))
    summary_record = {
        "path": summary_path.relative_to(stage).as_posix(),
        "sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "bytes": summary_path.stat().st_size,
        "format": "csv",
    }
    summary_payload = summary_path.read_bytes()
    summary_text = ""
    summary_encoding = ""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            summary_text = summary_payload.decode(encoding)
            summary_encoding = encoding
            break
        except UnicodeDecodeError:
            continue
    if not summary_encoding:
        raise IntegrityError("official summary encoding is unsupported")
    summary_rows = list(csv.reader(io.StringIO(summary_text)))
    summary_count = max(0, len(summary_rows) - 1)
    if status == "done" and summary_count != int(datasets["results"].get("rows") or 0):
        raise IntegrityError("official summary row count does not match results")
    datasets["official_summary"].update(
        status="complete",
        rows=summary_count,
        files=[summary_record],
        evidence={"encoding": summary_encoding, "header": summary_rows[0]},
    )
    staged.append(summary_path)

    log_digest = hashlib.sha256(bytes(browser["normal_log"])).hexdigest()[:24]
    log_path = stage / "raw" / f"normal-log-{log_digest}.jsonl.gz"
    log_record = _gzip_record(bytes(browser["normal_log"]), log_path, stage, "jsonl.gz")
    log_pages_payload = json.dumps(
        browser.get("normal_log_raw_pages") or [],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    pages_digest = hashlib.sha256(log_pages_payload).hexdigest()[:24]
    log_pages_path = stage / "raw" / f"normal-log-pages-{pages_digest}.json.gz"
    log_pages_record = _gzip_record(
        log_pages_payload,
        log_pages_path,
        stage,
        "json.gz",
    )
    log_status = str(browser["normal_log_status"])
    datasets["normal_log"].update(
        status=log_status,
        rows=int(browser["normal_log_rows"]),
        files=[log_record, log_pages_record],
        pagination={
            "pages": len(browser.get("normal_log_raw_pages") or []),
            "cumulative_rows": int(browser["normal_log_rows"]),
            "terminal": log_status == "complete",
            "capped": log_status == "capped_free",
        },
    )
    staged.append(log_path)
    staged.append(log_pages_path)
    staged.extend(
        _stage_error_log(
            stage,
            status,
            bytes(browser["normal_log"]),
            log_status,
            datasets,
        )
    )
    datasets["performance_profile"].update(
        status="unsupported_api_version",
        evidence={"source": "detail page did not expose a free profile export"},
    )

    if writer_present:
        attribution_raw = bytes(research["attribution"])
        expected_token = str((attribution.get("evidence") or {}).get("token") or "")
        expected_path = str(attribution.get("path") or "")
        checked = validate_attribution(
            attribution_raw.splitlines(),
            status,
            True,
            expected_token=expected_token,
            expected_path=expected_path,
            expected_start=str(params["start_date"]),
            expected_end=str(params["end_date"]),
        )
        staged.extend(_stage_attribution(stage, attribution_raw, datasets, checked))
    code = write_code_context(stage, "backtest", str(browser["code"]), params)
    code_source_path = stage / "raw" / "code-source-response.bin.gz"
    code["source_response"] = _gzip_record(
        bytes(browser["source_raw"]), code_source_path, stage, "binary.gz"
    )
    staged.extend(
        [
            stage / "code.py",
            _version_backtest_params(stage, code),
            code_source_path,
        ]
    )
    manifest = {
        "schema_version": 1,
        "object": {
            "kind": "backtest",
            "local_id": target["page_ordinal"],
            "status": status,
        },
        "source": {
            "url": target["detail_url"],
            "aliases": [{"remote_id": value} for value in target.get("aliases") or []],
            "observed_at": _now(),
        },
        "fence": target["fence"],
        "collection_fence": target["collection_fence"],
        "research_response": research_response,
        "research_lineage": [research_response],
        "code": code,
        "datasets": datasets,
    }
    manifest["gate"] = evaluate_gate(datasets)
    if manifest["gate"]["status"] != "pass":
        raise IntegrityError("backtest completeness gate failed")
    return manifest, staged


def sync_selected_backtest(
    page: Page,
    repository: Path,
    strategy_name: str,
    target_selector: str,
    *,
    attribution_path: str = "",
) -> dict[str, object]:
    strategy = fetch_strategy_default_code(page, strategy_name)
    targets = discover_history_targets(page, strategy_name)
    if target_selector.isdigit():
        matches = [item for item in targets if item["page_ordinal"] == target_selector]
    else:
        matches = [item for item in targets if item["detail_url"] == target_selector]
    if len(matches) != 1:
        raise TargetDiscoveryError(
            "explicit history target did not resolve exactly once"
        )
    target = matches[0]
    before = _target_fingerprint(target)
    index_path = repository / "joinquant" / "strategies" / "strategy_index.csv"
    strategy_id = _strategy_id(index_path, strategy_name, str(strategy["edit_url"]))
    strategy_dir = repository / "joinquant" / "strategies" / strategy_id
    _write_strategy(strategy_dir, strategy)
    object_dir = strategy_dir / "backtests" / str(target["page_ordinal"])
    existing: dict[str, object] | None = None
    if (object_dir / "manifest.json").is_file():
        existing, contract_current = _load_existing_for_sync(object_dir)
        if contract_current:
            existing = _ensure_queryable_attribution(object_dir, existing)
        source = existing.get("source")
        if isinstance(source, dict):
            target["aliases"] = list(
                dict.fromkeys(
                    [
                        *(target.get("aliases") or []),
                        *[
                            str(item["remote_id"])
                            for item in source.get("aliases") or []
                            if isinstance(item, dict) and item.get("remote_id")
                        ],
                    ]
                )
            )
    browser = fetch_backtest_browser_evidence(page, str(target["detail_url"]))
    browser_before_fingerprint = _backtest_browser_fingerprint(browser)
    attribution = detect_attribution_writer(str(browser["code"]))
    selected_attribution_path = attribution_path or str(attribution["path"])
    if attribution["writer_present"] and not selected_attribution_path:
        raise AttributionIncomplete("attribution writer path could not be derived")
    if (
        attribution["writer_present"]
        and attribution.get("path")
        and selected_attribution_path != attribution.get("path")
    ):
        raise AttributionIncomplete(
            "requested attribution path does not match target code"
        )
    research_before = fetch_research_backtest(
        page,
        str((target.get("aliases") or [""])[0]),
        attribution_path=selected_attribution_path,
    )
    refreshed = next(
        (
            item
            for item in discover_history_targets(page, strategy_name)
            if item["page_ordinal"] == target["page_ordinal"]
        ),
        None,
    )
    if refreshed is None or _target_fingerprint(refreshed) != before:
        raise IntegrityError("history target changed during synchronization")
    refreshed_browser = fetch_backtest_browser_evidence(
        page, str(refreshed["detail_url"])
    )
    if browser_before_fingerprint != _backtest_browser_fingerprint(refreshed_browser):
        raise IntegrityError("backtest browser evidence changed during synchronization")
    research_after = fetch_research_backtest(
        page,
        str((refreshed.get("aliases") or [""])[0]),
        attribution_path=selected_attribution_path,
    )
    if _research_remote_fingerprint(research_before) != _research_remote_fingerprint(
        research_after
    ):
        raise IntegrityError("backtest research data changed during synchronization")
    final = next(
        (
            item
            for item in discover_history_targets(page, strategy_name)
            if item["page_ordinal"] == target["page_ordinal"]
        ),
        None,
    )
    if final is None or _target_fingerprint(final) != before:
        raise IntegrityError("history target changed after research synchronization")
    final_browser = fetch_backtest_browser_evidence(page, str(final["detail_url"]))
    if _backtest_browser_fingerprint(
        refreshed_browser
    ) != _backtest_browser_fingerprint(final_browser):
        raise IntegrityError(
            "backtest browser evidence changed after research synchronization"
        )
    browser = final_browser
    research = research_after
    remote_fingerprint = _canonical_sha256(
        {
            "target": _target_fingerprint(final),
            "browser": _backtest_browser_fingerprint(browser),
            "research": _research_remote_fingerprint(research),
        }
    )
    target["fence"] = {
        "before_sha256": remote_fingerprint,
        "after_sha256": remote_fingerprint,
    }
    target["collection_fence"] = {
        "collection_before_sha256": _canonical_sha256(
            {
                "browser": browser_before_fingerprint,
                "research": _research_remote_fingerprint(research_before),
            }
        ),
        "collection_after_sha256": _canonical_sha256(
            {
                "browser": _backtest_browser_fingerprint(final_browser),
                "research": _research_remote_fingerprint(research_after),
            }
        ),
    }

    staging_root = repository / ".local" / "joinquant-sync"
    staging_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix="joinquant-backtest-", dir=staging_root
    ) as directory:
        manifest, staged = _build_backtest_batch(
            Path(directory), target, browser, research, attribution
        )
        if (
            existing is not None
            and contract_current
            and existing.get("fence") == manifest.get("fence")
            and _backtest_identity_matches(
                object_dir, existing, target, str(browser["code"])
            )
            and _backtest_archive_current(object_dir, existing)
        ):
            _update_backtest_params_pointer(object_dir, existing)
            _cleanup_unreferenced_backtest_files(object_dir, existing)
            _update_strategy_latest(
                index_path,
                strategy_id,
                "latest_backtest_id",
                str(target["page_ordinal"]),
            )
            return {
                "status": "unchanged",
                "strategy_id": strategy_id,
                "backtest_id": target["page_ordinal"],
                "manifest": existing,
            }
        commit_manifest(object_dir, manifest, staged)
        _update_backtest_params_pointer(object_dir, manifest)
        verify_existing_manifest(object_dir)
        _cleanup_unreferenced_backtest_files(object_dir, manifest)
    _update_strategy_latest(
        index_path, strategy_id, "latest_backtest_id", str(target["page_ordinal"])
    )
    return {
        "status": "committed",
        "strategy_id": strategy_id,
        "backtest_id": target["page_ordinal"],
        "manifest": manifest,
    }


def _simulation_id(index_path: Path, page_space_id: str) -> str:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with object_lock(index_path.parent):
        if index_path.is_file():
            data = json.loads(index_path.read_text(encoding="utf-8"))
        else:
            data = {"schema_version": 1, "objects": []}
        objects = data.get("objects")
        if not isinstance(objects, list):
            raise IntegrityError("simulation index is invalid")
        matches = [
            item for item in objects if item.get("page_space_id") == page_space_id
        ]
        if len(matches) > 1:
            raise IntegrityError("duplicate simulation page identity")
        if matches:
            return str(matches[0]["local_id"])
        local_id = f"simulation-{len(objects) + 1:03d}"
        objects.append({"local_id": local_id, "page_space_id": page_space_id})
        temporary = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, index_path)
        return local_id


def _validate_simulation_attribution(
    raw: bytes,
    start_date: str,
    status: str = "active",
    *,
    expected_token: str = "",
    expected_path: str = "",
    end_date: str = "",
) -> dict[str, object]:
    try:
        return validate_attribution(
            raw.splitlines(),
            "done" if status == "closed" else "active",
            True,
            expected_token=expected_token,
            expected_path=expected_path,
            expected_start=start_date,
            expected_end=end_date,
        )
    except AttributionIncomplete as error:
        if "start time" in str(error):
            raise AttributionIncomplete(
                "simulation attribution file belongs to a different run"
            ) from error
        raise


def _simulation_remote_fingerprint(browser: dict[str, object]) -> str:
    records = browser.get("normal_log_records")
    log_value: object = (
        records
        if isinstance(records, list)
        else bytes(browser["normal_log"]).decode("utf-8", errors="replace")
    )
    return _canonical_sha256(
        {
            "code_sha256": hashlib.sha256(
                str(browser["code"]).encode("utf-8")
            ).hexdigest(),
            "version_sha256": sorted(
                hashlib.sha256(str(item).encode("utf-8")).hexdigest()
                for item in browser.get("code_versions") or []
                if str(item).strip()
            ),
            "code_history_total": browser.get("code_history_total"),
            "log": log_value,
            "params": browser.get("params") or {},
        }
    )


def _research_remote_fingerprint(research: dict[str, object]) -> str:
    bundle = research.get("bundle")
    if not isinstance(bundle, dict):
        raise IntegrityError("research bundle is invalid")
    normalized = dict(bundle)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        normalized["metadata"] = {
            key: value for key, value in metadata.items() if key != "generated_at"
        }
    return _canonical_sha256(
        {
            "bundle": normalized,
            "attribution_sha256": hashlib.sha256(
                bytes(research.get("attribution") or b"")
            ).hexdigest(),
        }
    )


def _build_simulation_batch(
    stage: Path,
    candidate: dict[str, object],
    browser: dict[str, object],
    research: dict[str, object],
    attribution: dict[str, object],
    previous: dict[str, object] | None = None,
    previous_root: Path | None = None,
) -> tuple[dict[str, object], list[Path]]:
    writer_present = bool(attribution["writer_present"])
    status = str(candidate.get("status") or "active")
    datasets = expected_datasets(
        "simulation", "done" if status == "closed" else "active", writer_present
    )
    bundle = research["bundle"]
    if not isinstance(bundle, dict):
        raise IntegrityError("simulation research bundle is invalid")
    _compact_incremental_bundle(bundle, previous, previous_root)
    _merge_simulation_log(browser, previous, previous_root)
    data_payload = {
        name: bundle.get(name)
        for name in (
            "results",
            "balances",
            "positions",
            "orders",
            "records",
            "risk",
            "period_risks",
        )
    }
    data_sha = _canonical_sha256(data_payload)
    code_sha = _canonical_sha256(
        {
            "current": browser["code"],
            "version_sha256": sorted(
                {
                    hashlib.sha256(str(version).encode("utf-8")).hexdigest()
                    for version in browser.get("code_versions") or []
                    if str(version).strip()
                }
            ),
        }
    )
    snapshot_sha = hashlib.sha256(
        status.encode("utf-8")
        + data_sha.encode("ascii")
        + bytes(browser["normal_log"])
        + code_sha.encode("ascii")
        + _research_remote_fingerprint(research).encode("ascii")
    ).hexdigest()
    snapshot_id = snapshot_sha[:24]
    snapshot = stage / "snapshots" / snapshot_id
    staged = _stage_structured(snapshot, bundle, datasets, root=stage)
    research_response, research_response_path = _stage_research_response(
        snapshot, research, datasets, root=stage
    )
    staged.append(research_response_path)
    previous_lineage = (
        previous.get("research_lineage") if isinstance(previous, dict) else None
    )
    research_lineage = [
        item for item in (previous_lineage or []) if isinstance(item, dict)
    ]
    if (
        not research_lineage
        and isinstance(previous, dict)
        and isinstance(previous.get("research_response"), dict)
    ):
        research_lineage.append(previous["research_response"])
    if not research_lineage or research_lineage[-1].get(
        "sha256"
    ) != research_response.get("sha256"):
        research_lineage.append(research_response)
    _validate_fact_relations(bundle, datasets, dict(browser.get("params") or {}))
    _validate_run_semantics("simulation", status, datasets)

    report_path = snapshot / "reports" / "live-summary.json.gz"
    report_record = _gzip_record(
        json.dumps(
            {
                "extraction_method": "joinquant_research_get_backtest",
                "risk": bundle.get("risk"),
                "counts": {
                    name: len(value) if isinstance(value, (list, dict)) else 0
                    for name, value in data_payload.items()
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        report_path,
        stage,
        "json.gz",
    )
    datasets["official_summary"].update(
        status="complete", rows=1, files=[report_record]
    )
    staged.append(report_path)
    log_path = snapshot / "raw" / "normal-log.jsonl.gz"
    log_record = _gzip_record(bytes(browser["normal_log"]), log_path, stage, "jsonl.gz")
    log_pages_path = snapshot / "raw" / "normal-log-pages.json.gz"
    log_pages_record = _gzip_record(
        json.dumps(
            browser.get("normal_log_raw_pages") or [],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        log_pages_path,
        stage,
        "json.gz",
    )
    log_status = str(browser["normal_log_status"])
    datasets["normal_log"].update(
        status=log_status,
        rows=int(browser["normal_log_rows"]),
        files=[log_record, log_pages_record],
        pagination={
            "pages": len(browser.get("normal_log_raw_pages") or []),
            "cumulative_rows": int(browser["normal_log_rows"]),
            "terminal": log_status == "complete",
            "capped": log_status == "capped_free",
        },
    )
    staged.append(log_path)
    staged.append(log_pages_path)
    datasets["performance_profile"].update(
        status="unsupported_api_version",
        evidence={"source": "live page did not expose a free profile export"},
    )
    if writer_present:
        attribution_raw = bytes(research["attribution"])
        checked = _validate_simulation_attribution(
            attribution_raw,
            str((browser.get("params") or {}).get("start_date") or ""),
            status,
            expected_token=str((attribution.get("evidence") or {}).get("token") or ""),
            expected_path=str(attribution.get("path") or ""),
            end_date=str((browser.get("params") or {}).get("end_date") or ""),
        )
        staged.extend(
            _stage_attribution(snapshot, attribution_raw, datasets, checked, root=stage)
        )

    code = write_code_context(
        stage,
        "simulation",
        str(browser["code"]),
        dict(browser.get("params") or {}),
        source_backtest=str(browser["source_backtest"] or "unknown"),
        versions=list(browser.get("code_versions") or []),
    )
    history_path = stage / "raw" / "code-history.json.gz"
    code["history"] = _gzip_record(
        json.dumps(
            browser.get("code_history_pages") or [],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        history_path,
        stage,
        "json.gz",
    )
    source_response_path = stage / "raw" / "code-source-response.bin.gz"
    code["source_response"] = _gzip_record(
        bytes(browser.get("source_raw") or b""),
        source_response_path,
        stage,
        "binary.gz",
    )
    staged.extend(_version_simulation_code_context(stage, code))
    staged.extend([history_path, source_response_path])
    remote_state_sha = _canonical_sha256(
        {
            "status": status,
            "page_space_id": candidate.get("page_space_id"),
            "browser": _simulation_remote_fingerprint(browser),
            "data": data_sha,
            "attribution": hashlib.sha256(
                bytes(research.get("attribution") or b"")
            ).hexdigest(),
        }
    )
    collection_fence = candidate.get("collection_fence")
    fence = {
        "before_sha256": remote_state_sha,
        "after_sha256": remote_state_sha,
    }
    manifest = {
        "schema_version": 1,
        "object": {
            "kind": "simulation",
            "local_id": candidate["local_id"],
            "status": status,
        },
        "source": {
            "url": candidate["detail_url"],
            "aliases": [
                {"remote_id": value} for value in candidate.get("aliases") or []
            ],
            "observed_at": _now(),
            "source_backtest": browser["source_backtest"],
        },
        "fence": fence,
        "research_response": research_response,
        "research_lineage": research_lineage,
        "code": code,
        "datasets": datasets,
        "tracking": "stopped" if status == "closed" else "active",
        "streams": {
            "code": {"cursor": snapshot_id, "sha256": code_sha},
            "snapshots": {"cursor": snapshot_id, "sha256": snapshot_sha},
            "data": {
                "cursor": snapshot_id,
                "sha256": data_sha,
                "cursors": _bundle_time_cursors(
                    bundle, _manifest_time_cursors(previous)
                ),
            },
            "logs": {
                "cursor": snapshot_id,
                "sha256": hashlib.sha256(bytes(browser["normal_log"])).hexdigest(),
            },
        },
    }
    if isinstance(collection_fence, dict):
        manifest["collection_fence"] = collection_fence
    if status == "closed":
        manifest["final_sync"] = "complete"
    manifest["gate"] = evaluate_gate(datasets)
    if manifest["gate"]["status"] != "pass":
        raise IntegrityError("simulation completeness gate failed")
    return manifest, staged


def commit_simulation_evidence(
    object_dir: Path,
    stage: Path,
    candidate: dict[str, object],
    browser: dict[str, object],
    research: dict[str, object],
    attribution: dict[str, object],
    *,
    previous: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build, atomically commit, and verify one collected simulation snapshot."""
    if previous is None and (object_dir / "manifest.json").is_file():
        previous, _ = _load_existing_for_sync(object_dir)
    manifest, staged = _build_simulation_batch(
        stage,
        candidate,
        browser,
        research,
        attribution,
        previous=previous,
        previous_root=object_dir,
    )
    if (
        previous is not None
        and previous.get("fence") == manifest.get("fence")
        and previous.get("tracking") == manifest.get("tracking")
    ):
        verify_existing_manifest(object_dir)
        _cleanup_unreferenced_snapshots(object_dir, previous)
        return {"status": "unchanged", "manifest": previous}
    commit_manifest(object_dir, manifest, staged)
    _update_simulation_pointers(object_dir, manifest)
    verify_existing_manifest(object_dir)
    _cleanup_unreferenced_snapshots(object_dir, manifest)
    return {"status": "committed", "manifest": manifest}


def _tracked_simulations(
    repository: Path, active_spaces: set[str]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    closed_candidates: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    strategy_root = repository / "joinquant" / "strategies"
    strategy_names = {
        row["strategy_id"]: row["name"]
        for row in _read_strategy_index(strategy_root / "strategy_index.csv")
    }
    for index_path in strategy_root.glob("strategy-*/simulations/index.json"):
        data = json.loads(index_path.read_text(encoding="utf-8"))
        for item in data.get("objects") or []:
            if not isinstance(item, dict):
                continue
            space = str(item.get("page_space_id") or "")
            if not space or space in active_spaces:
                continue
            local_id = str(item.get("local_id") or "")
            object_dir = index_path.parent / local_id
            manifest_path = object_dir / "manifest.json"
            if not manifest_path.is_file():
                unresolved.append(
                    {
                        "name": strategy_names.get(index_path.parents[1].name, ""),
                        "message": "tracked simulation manifest is missing",
                    }
                )
                continue
            manifest, _ = _load_existing_for_sync(object_dir)
            if manifest.get("tracking") != "active":
                continue
            source = manifest.get("source")
            url = str(source.get("url") or "") if isinstance(source, dict) else ""
            candidate = {
                "name": strategy_names.get(index_path.parents[1].name, ""),
                "page_space_id": space,
                "local_id": local_id,
                "detail_url": url,
                "aliases": [
                    str(alias.get("remote_id"))
                    for alias in (source.get("aliases") or [])
                    if isinstance(alias, dict) and alias.get("remote_id")
                ]
                if isinstance(source, dict)
                else [],
                "status": "unresolved",
                "strategy_id": index_path.parents[1].name,
            }
            closed_candidates.append(candidate)
    return closed_candidates, unresolved


def sync_all_active_simulations(
    page: Page, repository: Path
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    all_remote = discover_all_simulations(page)
    candidates = [item for item in all_remote if item.get("status") == "active"]
    tracked, unresolved = _tracked_simulations(
        repository, {str(item["page_space_id"]) for item in candidates}
    )
    results.extend(
        {"status": "failed", "error": "IntegrityError", **item} for item in unresolved
    )
    for candidate in tracked:
        fresh_closed = [
            item
            for item in all_remote
            if item.get("page_space_id") == candidate.get("page_space_id")
            and item.get("status") == "closed"
        ]
        if len(fresh_closed) == 1:
            refreshed = fresh_closed[0]
            refreshed["local_id"] = candidate["local_id"]
            refreshed["strategy_id"] = candidate["strategy_id"]
            candidates.append(refreshed)
            continue
        try:
            status = inspect_simulation_status(page, str(candidate["detail_url"]))
        except Exception as error:
            failure = {
                "name": candidate.get("name"),
                "status": "failed",
                "error": type(error).__name__,
                "message": str(error),
            }
            if isinstance(error, FreeLogIncomplete):
                evidence = persist_failure_evidence(
                    repository,
                    error,
                    identity=str(
                        candidate.get("page_space_id") or candidate.get("name")
                    ),
                )
                if evidence:
                    failure["failure_evidence"] = evidence
            results.append(failure)
            continue
        if status != "closed":
            results.append(
                {
                    "name": candidate.get("name"),
                    "status": "failed",
                    "error": "SimulationStatusUnknown",
                    "message": "tracked simulation disappeared from the active list without explicit closed-page evidence",
                }
            )
            continue
        candidate["status"] = "closed"
        candidates.append(candidate)
    for candidate in candidates:
        try:
            strategy = fetch_strategy_default_code(page, str(candidate["name"]))
            strategy_id = str(candidate.get("strategy_id") or "") or _strategy_id(
                repository / "joinquant" / "strategies" / "strategy_index.csv",
                str(candidate["name"]),
                str(strategy["edit_url"]),
            )
            strategy_dir = repository / "joinquant" / "strategies" / strategy_id
            _write_strategy(strategy_dir, strategy)
            simulation_id = _simulation_id(
                strategy_dir / "simulations" / "index.json",
                str(candidate["page_space_id"]),
            )
            candidate["local_id"] = simulation_id
            object_dir = strategy_dir / "simulations" / simulation_id
            existing = None
            if (object_dir / "manifest.json").is_file():
                existing, _ = _load_existing_for_sync(object_dir)
                source = existing.get("source")
                if isinstance(source, dict):
                    candidate["aliases"] = list(
                        dict.fromkeys(
                            [
                                *(candidate.get("aliases") or []),
                                *[
                                    str(item["remote_id"])
                                    for item in source.get("aliases") or []
                                    if isinstance(item, dict) and item.get("remote_id")
                                ],
                            ]
                        )
                    )
            browser = fetch_simulation_browser_evidence(page, candidate)
            attribution = detect_attribution_writer(str(browser["code"]))
            if attribution["writer_present"] and not attribution["path"]:
                raise AttributionIncomplete("simulation attribution path is unresolved")
            after_times = _manifest_time_cursors(existing)
            research_before = fetch_research_backtest(
                page,
                str(browser["research_id"]),
                attribution_path=str(attribution["path"]),
                after_times=after_times,
            )
            browser_before_fingerprint = _simulation_remote_fingerprint(browser)
            research_before_fingerprint = _research_remote_fingerprint(research_before)
            refreshed_candidate = candidate
            if candidate.get("status") == "active":
                matches = [
                    item
                    for item in discover_active_simulations(page)
                    if item.get("page_space_id") == candidate.get("page_space_id")
                ]
                if len(matches) != 1:
                    raise IntegrityError(
                        "simulation inventory changed during synchronization"
                    )
                refreshed_candidate = matches[0]
                refreshed_candidate["local_id"] = simulation_id
            refreshed_browser = fetch_simulation_browser_evidence(
                page, refreshed_candidate
            )
            research_after = fetch_research_backtest(
                page,
                str(refreshed_browser["research_id"]),
                attribution_path=str(attribution["path"]),
                after_times=after_times,
            )
            browser_after_fingerprint = _simulation_remote_fingerprint(
                refreshed_browser
            )
            research_after_fingerprint = _research_remote_fingerprint(research_after)
            if browser_before_fingerprint != browser_after_fingerprint:
                raise IntegrityError("simulation changed during synchronization")
            if research_before_fingerprint != research_after_fingerprint:
                raise IntegrityError(
                    "simulation research data changed during synchronization"
                )
            final_candidate = refreshed_candidate
            if candidate.get("status") == "active":
                final_matches = [
                    item
                    for item in discover_active_simulations(page)
                    if item.get("page_space_id") == candidate.get("page_space_id")
                ]
                if len(final_matches) != 1:
                    raise IntegrityError(
                        "simulation inventory changed after research synchronization"
                    )
                final_candidate = final_matches[0]
                final_candidate["local_id"] = simulation_id
            final_browser = fetch_simulation_browser_evidence(page, final_candidate)
            final_browser_fingerprint = _simulation_remote_fingerprint(final_browser)
            if browser_after_fingerprint != final_browser_fingerprint:
                raise IntegrityError(
                    "simulation browser evidence changed after research synchronization"
                )
            research = research_after
            browser = final_browser
            candidate.update(
                {
                    "detail_url": final_candidate["detail_url"],
                    "aliases": list(
                        dict.fromkeys(
                            [
                                *(candidate.get("aliases") or []),
                                *(refreshed_candidate.get("aliases") or []),
                                *(final_candidate.get("aliases") or []),
                            ]
                        )
                    ),
                    "collection_fence": {
                        "collection_before_sha256": _canonical_sha256(
                            {
                                "browser": browser_before_fingerprint,
                                "research": research_before_fingerprint,
                            }
                        ),
                        "collection_after_sha256": _canonical_sha256(
                            {
                                "browser": final_browser_fingerprint,
                                "research": research_after_fingerprint,
                            }
                        ),
                    },
                }
            )
            staging_root = repository / ".local" / "joinquant-sync"
            staging_root.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                prefix="joinquant-simulation-", dir=staging_root
            ) as directory:
                synced = commit_simulation_evidence(
                    object_dir,
                    Path(directory),
                    candidate,
                    browser,
                    research,
                    attribution,
                    previous=existing,
                )
            _update_strategy_latest(
                repository / "joinquant" / "strategies" / "strategy_index.csv",
                strategy_id,
                "latest_simulation_id",
                simulation_id,
            )
            if synced["status"] == "unchanged":
                results.append({"name": candidate["name"], "status": "unchanged"})
                continue
            manifest = synced["manifest"]
            results.append(
                {
                    "name": candidate["name"],
                    "status": "committed",
                    "strategy_id": strategy_id,
                    "simulation_id": simulation_id,
                    "gate": manifest["gate"],
                }
            )
        except Exception as error:
            failure = {
                "name": candidate.get("name"),
                "status": "failed",
                "error": type(error).__name__,
                "message": str(error),
            }
            if isinstance(error, FreeLogIncomplete):
                evidence = persist_failure_evidence(
                    repository,
                    error,
                    identity=str(
                        candidate.get("page_space_id") or candidate.get("name")
                    ),
                )
                if evidence:
                    failure["failure_evidence"] = evidence
            results.append(failure)
    return results
