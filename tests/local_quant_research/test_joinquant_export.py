from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pandas as pd


FIELDS = (
    "date",
    "security",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "money",
    "factor",
    "paused",
    "high_limit",
    "low_limit",
)


def test_rendered_program_uses_verified_joinquant_research_contract() -> None:
    from scripts.research.market_data.joinquant_export import (
        ExportRequest,
        render_export_program,
    )

    program = render_export_program(
        ExportRequest(
            securities=("510300.XSHG", "159915.XSHE"),
            fields=FIELDS,
            snapshot_end_date="2026-07-13",
        )
    )

    compile(program, "<joinquant-export>", "exec")
    assert "get_security_info(security).start_date" in program
    assert "get_price(" in program
    assert "fq=None" in program
    assert "skip_paused=False" in program
    assert "panel=False" in program
    assert repr(list(FIELDS)) in program
    assert "line_terminator='\\n'" in program
    assert "write_file(" in program
    assert "read_file(" in program
    assert "hashlib.sha256(remote_bytes).hexdigest()" in program
    assert "def cleanup_export(delete_file):" in program
    assert "from jqdata import get_price" not in program
    for credential_name in ("PASSWORD", "ACCESS_TOKEN", "COOKIE", "SECRET"):
        assert credential_name not in program.upper()


def test_rendered_program_executes_with_injected_research_apis() -> None:
    from scripts.research.market_data.joinquant_export import (
        ExportRequest,
        render_export_program,
    )

    calls: list[dict[str, object]] = []
    remote_files: dict[str, bytes] = {}

    class SecurityInfo:
        start_date = date(2026, 1, 5)

    def get_security_info(security: str) -> SecurityInfo:
        assert security == "510300.XSHG"
        return SecurityInfo()

    def get_price(security: str, **kwargs):
        calls.append({"security": security, **kwargs})
        return pd.DataFrame(
            [
                [10, 11, 9, 10.5, 10, 100, 1050, 1, 0.0, 11, 9],
                [10.5, 12, 10, 11, 10.5, 110, 1210, 1, 1.0, 11.55, 9.45],
            ],
            index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
            columns=FIELDS[2:],
        )

    def write_file(path: str, content: bytes, append: bool = False) -> None:
        assert append is False
        remote_files[path] = content

    def read_file(path: str) -> bytes:
        return remote_files[path]

    namespace = {
        "get_security_info": get_security_info,
        "get_price": get_price,
        "write_file": write_file,
        "read_file": read_file,
    }
    program = render_export_program(
        ExportRequest(
            securities=("510300.XSHG",),
            fields=FIELDS,
            snapshot_end_date="2026-01-06",
        )
    )

    exec(compile(program, "<joinquant-export>", "exec"), namespace)

    payload = remote_files["market-data.csv"]
    assert payload.decode("utf-8").splitlines()[0] == ",".join(FIELDS)
    assert namespace["export_result"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert namespace["export_result"]["rows"] == 2
    assert calls == [
        {
            "security": "510300.XSHG",
            "start_date": date(2026, 1, 5),
            "end_date": "2026-01-06",
            "frequency": "daily",
            "fields": list(FIELDS[2:]),
            "fq": None,
            "skip_paused": False,
            "panel": False,
        }
    ]

    namespace["cleanup_export"](lambda path: remote_files.pop(path))
    assert remote_files == {}


def test_export_request_contains_no_strategy_universe_or_rules() -> None:
    from scripts.research.market_data.joinquant_export import ExportRequest

    request = ExportRequest(
        securities=("510300.XSHG",),
        fields=FIELDS,
        snapshot_end_date="2026-07-13",
    )

    assert request.fq is None
    assert request.skip_paused is False
    assert not hasattr(request, "strategy")
    assert not hasattr(request, "entry_window")
    assert not hasattr(request, "risk_budget")


def test_verify_transfer_requires_matching_bytes_and_confirmed_cleanup(
    tmp_path: Path,
) -> None:
    from scripts.research.market_data.joinquant_export import verify_transfer

    local_file = tmp_path / "market-data.csv"
    local_file.write_bytes(b"date,security\n2026-01-05,510300.XSHG\n")
    digest = hashlib.sha256(local_file.read_bytes()).hexdigest()

    complete = verify_transfer(
        local_file=local_file,
        remote_sha256=digest,
        remote_cleaned=True,
    )
    digest_mismatch = verify_transfer(
        local_file=local_file,
        remote_sha256="0" * 64,
        remote_cleaned=True,
    )
    not_cleaned = verify_transfer(
        local_file=local_file,
        remote_sha256=digest,
        remote_cleaned=False,
    )

    assert complete.status == "complete"
    assert complete.local_sha256 == digest
    assert complete.reasons == ()
    assert digest_mismatch.status == "failed"
    assert "SHA256 mismatch" in digest_mismatch.reasons
    assert not_cleaned.status == "failed"
    assert "remote cleanup is not confirmed" in not_cleaned.reasons


def test_verify_transfer_fails_when_local_file_is_missing(tmp_path: Path) -> None:
    from scripts.research.market_data.joinquant_export import verify_transfer

    evidence = verify_transfer(
        local_file=tmp_path / "missing.csv",
        remote_sha256="0" * 64,
        remote_cleaned=True,
    )

    assert evidence.status == "failed"
    assert evidence.local_sha256 is None
    assert "local transfer file is missing" in evidence.reasons
