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
        import json
        import pandas as pd

        if 'finance' not in globals() or 'query' not in globals():
            from jqdata import finance, query

        SECURITIES = {securities!r}
        OUTPUT_FIELDS = {fields!r}
        PRICE_FIELDS = [
            field for field in OUTPUT_FIELDS if field not in ('date', 'security')
        ]
        SNAPSHOT_END_DATE = {request.snapshot_end_date!r}
        REMOTE_PATH = 'market-data.csv'
        CORPORATE_ACTIONS_PATH = 'corporate-actions.csv'
        CORPORATE_ACTION_FIELDS = [
            'source_event_id', 'security', 'event_type', 'announcement_date',
            'record_date', 'ex_date', 'effective_date', 'pay_date', 'status',
            'knowledge_cutoff_date', 'split_ratio', 'cash_per_share', 'source',
            'source_record_sha256'
        ]


        def _text(value):
            if value is None or pd.isna(value):
                return ''
            if hasattr(value, 'strftime'):
                return value.strftime('%Y-%m-%d')
            return str(value)


        def _first_text(*values):
            for value in values:
                normalized = _text(value)
                if normalized:
                    return normalized
            return ''


        def _status_at_cutoff(process_id, cancellation_date):
            cancel_date = _text(cancellation_date)
            currently_cancelled = (
                not pd.isna(process_id) and int(process_id) == 405003
            )
            if cancel_date:
                return 'cancelled' if cancel_date <= SNAPSHOT_END_DATE else 'active'
            if currently_cancelled:
                raise ValueError(
                    'cancelled FUND_DIVIDEND event has no cancellation date'
                )
            return 'active'


        def _write_verified(path, output):
            try:
                csv_text = output.to_csv(index=False, line_terminator='\\n')
            except TypeError:
                csv_text = output.to_csv(index=False, lineterminator='\\n')
            payload = csv_text.encode('utf-8')
            write_file(path, payload, append=False)
            remote_bytes = read_file(path)
            if isinstance(remote_bytes, str):
                remote_bytes = remote_bytes.encode('utf-8')
            local_sha256 = hashlib.sha256(payload).hexdigest()
            remote_sha256 = hashlib.sha256(remote_bytes).hexdigest()
            if local_sha256 != remote_sha256:
                raise ValueError('remote readback SHA256 mismatch')
            return {{
                'remote_path': path,
                'sha256': remote_sha256,
                'bytes': len(remote_bytes),
                'rows': len(output),
            }}


        def export_market_data():
            frames = []
            coverage = []
            for security in SECURITIES:
                security_info = get_security_info(security)
                start_date = security_info.start_date
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
                if frame.empty:
                    raise ValueError('security has no market rows: ' + security)
                frame.insert(1, 'security', security)
                coverage.append({{
                    'security': security,
                    'official_start_date': _text(security_info.start_date),
                    'first_market_date': frame['date'].iloc[0],
                    'last_market_date': frame['date'].iloc[-1],
                    'market_rows': len(frame),
                }})
                frames.append(frame[OUTPUT_FIELDS])

            if not frames:
                raise ValueError('no securities were requested')
            output = pd.concat(frames, ignore_index=True)
            output = output[OUTPUT_FIELDS].sort_values(
                ['date', 'security'], kind='mergesort'
            )
            result = _write_verified(REMOTE_PATH, output)
            result['securities'] = coverage
            return result


        def export_corporate_actions():
            security_by_code = {{
                security.split('.')[0]: security for security in SECURITIES
            }}
            output_rows = []
            source_fields = [
                'id', 'code', 'pub_date', 'event_id', 'event', 'process_id',
                'process', 'proportion', 'split_ratio', 'record_date', 'ex_date',
                'fund_paid_date', 'dividend_cancel_date', 'otc_ex_date', 'pay_date'
            ]
            for code, security in sorted(security_by_code.items()):
                raw = finance.run_query(
                    query(finance.FUND_DIVIDEND)
                    .filter(finance.FUND_DIVIDEND.code == code)
                    .limit(5000)
                )
                if len(raw) >= 5000:
                    raise ValueError('FUND_DIVIDEND query may be truncated')
                for _, row in raw.iterrows():
                    announcement_date = _text(row.get('pub_date'))
                    if not announcement_date or announcement_date > SNAPSHOT_END_DATE:
                        continue
                    event_code = int(row.get('event_id'))
                    if event_code == 404001:
                        event_type = 'cash_dividend'
                    elif event_code in (404002, 404003, 404004, 404005):
                        event_type = 'split'
                    else:
                        raise ValueError('unsupported FUND_DIVIDEND event type')
                    ex_date = _first_text(
                        row.get('ex_date'), row.get('otc_ex_date')
                    )
                    record_date = _text(row.get('record_date'))
                    effective_date = ex_date or record_date
                    if not effective_date:
                        raise ValueError('FUND_DIVIDEND event has no effective date')
                    cancel_date = _text(row.get('dividend_cancel_date'))
                    status = _status_at_cutoff(
                        row.get('process_id'), row.get('dividend_cancel_date')
                    )
                    raw_document = {{
                        field: _text(row.get(field)) for field in source_fields
                    }}
                    source_record_sha256 = hashlib.sha256(
                        json.dumps(
                            raw_document,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(',', ':'),
                        ).encode('utf-8')
                    ).hexdigest()
                    output_rows.append({{
                        'source_event_id': 'FUND_DIVIDEND:' + _text(row.get('id')),
                        'security': security,
                        'event_type': event_type,
                        'announcement_date': announcement_date,
                        'record_date': record_date,
                        'ex_date': ex_date,
                        'effective_date': effective_date,
                        'pay_date': _first_text(
                            row.get('pay_date'), row.get('fund_paid_date')
                        ),
                        'status': status,
                        'knowledge_cutoff_date': SNAPSHOT_END_DATE,
                        'split_ratio': (
                            _text(row.get('split_ratio')) if event_type == 'split' else ''
                        ),
                        'cash_per_share': (
                            _text(row.get('proportion'))
                            if event_type == 'cash_dividend'
                            else ''
                        ),
                        'source': 'joinquant.finance.FUND_DIVIDEND',
                        'source_record_sha256': source_record_sha256,
                    }})
            output = pd.DataFrame(output_rows, columns=CORPORATE_ACTION_FIELDS)
            if not output.empty:
                output = output.sort_values(
                    ['source_event_id'], kind='mergesort'
                )
            return _write_verified(CORPORATE_ACTIONS_PATH, output)


        def cleanup_export(delete_file):
            delete_file(REMOTE_PATH)
            delete_file(CORPORATE_ACTIONS_PATH)


        export_result = {{
            'market_data': export_market_data(),
            'corporate_actions': export_corporate_actions(),
        }}
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
    corporate_actions_file: Path,
    corporate_actions_remote_sha256: str,
    cleanup_remote: Callable[[], bool],
    manifest: Mapping[str, object],
    root: Path,
) -> BatchRecord:
    """Publish a verified transfer, then confirm remote and local cleanup."""
    transfer_path = Path(local_file)
    actions_transfer_path = Path(corporate_actions_file)
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
    actions_evidence = verify_transfer(
        local_file=actions_transfer_path,
        remote_sha256=corporate_actions_remote_sha256,
        remote_cleaned=True,
    )
    if actions_evidence.status != "complete":
        byte_reasons = tuple(
            reason
            for reason in actions_evidence.reasons
            if reason != "remote cleanup is not confirmed"
        )
        raise MarketDataIntegrityError(
            "corporate-actions transfer validation failed: "
            + "; ".join(byte_reasons)
        )

    record = import_batch(
        csv_path=transfer_path,
        corporate_actions_csv_path=actions_transfer_path,
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
            actions_transfer_path.unlink(missing_ok=True)
        except OSError as exc:
            raise MarketDataIntegrityError(
                "local transfer cleanup is not confirmed"
            ) from exc
    finally:
        if transfer_path.exists():
            raise MarketDataIntegrityError("local transfer cleanup is not confirmed")
        if actions_transfer_path.exists():
            raise MarketDataIntegrityError(
                "local corporate-actions transfer cleanup is not confirmed"
            )
    return record
