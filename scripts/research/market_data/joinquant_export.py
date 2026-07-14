from __future__ import annotations

import hashlib
import re
import textwrap
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

from .contracts import MARKET_DATA_FIELDS, BatchRecord
from .storage import MarketDataIntegrityError, import_batch


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ExportRequest:
    securities: Sequence[str]
    fields: Sequence[str]
    snapshot_end_date: str
    fq: None = None
    skip_paused: bool = False

    def __post_init__(self) -> None:
        securities = tuple(str(value).strip() for value in self.securities)
        fields = tuple(str(value) for value in self.fields)
        if not securities or any(not value for value in securities):
            raise ValueError("securities must be non-empty")
        if len(securities) != len(set(securities)):
            raise ValueError("securities must be unique")
        if fields != MARKET_DATA_FIELDS:
            raise ValueError("fields must match the fixed daily market-data contract")
        try:
            date.fromisoformat(self.snapshot_end_date)
        except ValueError as exc:
            raise ValueError("snapshot_end_date must use YYYY-MM-DD") from exc
        if self.fq is not None:
            raise ValueError("the first export contract requires fq=None")
        if self.skip_paused is not False:
            raise ValueError("the first export contract requires skip_paused=False")
        object.__setattr__(self, "securities", securities)
        object.__setattr__(self, "fields", fields)


@dataclass(frozen=True)
class TransferEvidence:
    status: Literal["complete", "failed"]
    local_sha256: str | None
    remote_sha256: str
    remote_cleaned: bool
    reasons: tuple[str, ...]


def render_export_program(request: ExportRequest) -> str:
    securities = list(request.securities)
    fields = list(request.fields)
    return textwrap.dedent(
        f"""\
        import hashlib
        import pandas as pd

        SECURITIES = {securities!r}
        OUTPUT_FIELDS = {fields!r}
        PRICE_FIELDS = [
            field for field in OUTPUT_FIELDS if field not in ('date', 'security')
        ]
        SNAPSHOT_END_DATE = {request.snapshot_end_date!r}
        REMOTE_PATH = 'market-data.csv'


        def export_market_data():
            frames = []
            for security in SECURITIES:
                start_date = get_security_info(security).start_date
                frame = get_price(
                    security,
                    start_date=start_date,
                    end_date=SNAPSHOT_END_DATE,
                    frequency='daily',
                    fields=PRICE_FIELDS,
                    fq=None,
                    skip_paused=False,
                    panel=False,
                )
                frame.index.name = 'date'
                frame = frame.reset_index()
                frame['date'] = frame['date'].map(
                    lambda value: value.strftime('%Y-%m-%d')
                )
                frame.insert(1, 'security', security)
                frames.append(frame[OUTPUT_FIELDS])

            if not frames:
                raise ValueError('no securities were requested')
            output = pd.concat(frames, ignore_index=True)
            output = output[OUTPUT_FIELDS].sort_values(
                ['date', 'security'], kind='mergesort'
            )
            try:
                csv_text = output.to_csv(index=False, line_terminator='\\n')
            except TypeError:
                csv_text = output.to_csv(index=False, lineterminator='\\n')
            payload = csv_text.encode('utf-8')
            write_file(REMOTE_PATH, payload, append=False)
            remote_bytes = read_file(REMOTE_PATH)
            if isinstance(remote_bytes, str):
                remote_bytes = remote_bytes.encode('utf-8')
            local_sha256 = hashlib.sha256(payload).hexdigest()
            remote_sha256 = hashlib.sha256(remote_bytes).hexdigest()
            if local_sha256 != remote_sha256:
                raise ValueError('remote readback SHA256 mismatch')
            return {{
                'remote_path': REMOTE_PATH,
                'sha256': remote_sha256,
                'bytes': len(remote_bytes),
                'rows': len(output),
            }}


        def cleanup_export(delete_file):
            delete_file(REMOTE_PATH)


        export_result = export_market_data()
        """
    )


def verify_transfer(
    *,
    local_file: Path,
    remote_sha256: str,
    remote_cleaned: bool,
) -> TransferEvidence:
    reasons: list[str] = []
    local_sha256: str | None = None
    try:
        local_sha256 = hashlib.sha256(Path(local_file).read_bytes()).hexdigest()
    except OSError:
        reasons.append("local transfer file is missing")

    normalized_remote = str(remote_sha256).lower()
    if _SHA256_PATTERN.fullmatch(normalized_remote) is None:
        reasons.append("remote SHA256 is invalid")
    elif local_sha256 is not None and local_sha256 != normalized_remote:
        reasons.append("SHA256 mismatch")
    if remote_cleaned is not True:
        reasons.append("remote cleanup is not confirmed")

    return TransferEvidence(
        status="failed" if reasons else "complete",
        local_sha256=local_sha256,
        remote_sha256=normalized_remote,
        remote_cleaned=remote_cleaned is True,
        reasons=tuple(reasons),
    )


def import_verified_transfer(
    *,
    local_file: Path,
    remote_sha256: str,
    cleanup_remote: Callable[[], bool],
    manifest: Mapping[str, object],
    root: Path,
) -> BatchRecord:
    """Publish a verified transfer, then confirm remote and local cleanup."""
    transfer_path = Path(local_file)
    evidence = verify_transfer(
        local_file=transfer_path,
        remote_sha256=remote_sha256,
        remote_cleaned=True,
    )
    if evidence.status != "complete":
        byte_reasons = tuple(
            reason
            for reason in evidence.reasons
            if reason != "remote cleanup is not confirmed"
        )
        raise MarketDataIntegrityError(
            "transfer validation failed: " + "; ".join(byte_reasons)
        )

    record = import_batch(
        csv_path=transfer_path,
        manifest=manifest,
        root=Path(root),
    )
    try:
        remote_cleaned = cleanup_remote()
    except Exception as exc:
        raise MarketDataIntegrityError("remote cleanup is not confirmed") from exc
    if remote_cleaned is not True:
        raise MarketDataIntegrityError("remote cleanup is not confirmed")

    try:
        try:
            transfer_path.unlink(missing_ok=True)
        except OSError as exc:
            raise MarketDataIntegrityError(
                "local transfer cleanup is not confirmed"
            ) from exc
    finally:
        if transfer_path.exists():
            raise MarketDataIntegrityError("local transfer cleanup is not confirmed")
    return record
