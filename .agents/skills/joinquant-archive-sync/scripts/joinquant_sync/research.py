from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from playwright.sync_api import Page

from joinquant_sync.archive import stage_external_file
from joinquant_sync.browser import capture_download, ensure_authenticated


class PaginationIncomplete(RuntimeError):
    """Raised when a page sequence has no provable end."""


class FactValidationError(RuntimeError):
    """Raised when structured rows cannot pass the dataset contract."""


class InventoryChanged(RuntimeError):
    """Raised when the remote inventory changes across both collection attempts."""


def collect_pages(
    fetch_page: Callable[[str | None], dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    cursor: str | None = None
    cursors: list[str | None] = []
    seen: set[str | None] = set()
    rows: list[dict[str, object]] = []
    declared_total: int | None = None
    for _ in range(10_000):
        if cursor in seen:
            raise PaginationIncomplete(f"repeated cursor: {cursor}")
        seen.add(cursor)
        cursors.append(cursor)
        page = fetch_page(cursor)
        page_rows = page.get("rows")
        if not isinstance(page_rows, list) or not all(
            isinstance(row, dict) for row in page_rows
        ):
            raise PaginationIncomplete("page rows must be a list of objects")
        rows.extend(page_rows)
        page_total = page.get("total")
        if isinstance(page_total, int):
            if declared_total is None:
                declared_total = page_total
            elif page_total != declared_total:
                raise PaginationIncomplete("declared total changed between pages")
            if len(rows) > declared_total:
                raise PaginationIncomplete("rows exceed declared total")
        if not page_rows:
            if declared_total is not None and len(rows) != declared_total:
                raise PaginationIncomplete("empty page before declared total")
            end = "empty_page"
            break
        next_cursor = page.get("next")
        if next_cursor is not None:
            cursor = str(next_cursor)
            continue
        if declared_total is not None and len(rows) == declared_total:
            end = "declared_total"
            break
        if page.get("end"):
            end = str(page["end"])
            break
        if page.get("page_full") is False:
            end = "short_page"
            break
        raise PaginationIncomplete("last page has no end evidence")
    else:
        raise PaginationIncomplete("pagination exceeded 10000 pages")
    pagination: dict[str, object] = {
        "complete": True,
        "end": end,
        "pages": len(cursors),
        "rows": len(rows),
        "cursors": cursors,
    }
    if declared_total is not None:
        pagination["total"] = declared_total
    return rows, pagination


def validate_fact_table(
    name: str,
    rows: list[dict[str, object]],
    run_status: str,
    pagination: dict[str, object],
) -> dict[str, object]:
    if not pagination.get("end"):
        raise FactValidationError(f"{name}: pagination has no end evidence")
    if not rows:
        if run_status not in {"failed", "cancelled"} and name not in {
            "positions",
            "orders",
            "records",
        }:
            raise FactValidationError(f"{name}: required table is empty")
        return {
            "required": True,
            "status": "complete",
            "rows": 0,
            "verified_empty": True,
            "pagination": pagination,
        }

    required_fields = {
        "results": {"time", "returns", "benchmark_returns"},
        "balances": {"time"},
        "positions": {"time", "security"},
        "orders": {"time", "security"},
        "records": {"time"},
    }.get(name, set())
    if any(not required_fields.issubset(row) for row in rows):
        raise FactValidationError(f"{name}: required field missing")
    canonical = [
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    ]
    if len(canonical) != len(set(canonical)):
        raise FactValidationError(f"{name}: duplicate row")
    times = [str(row["time"]) for row in rows if "time" in row]
    if times and times != sorted(times):
        raise FactValidationError(f"{name}: rows are not time-sorted")
    declared_total = pagination.get("total")
    if isinstance(declared_total, int) and declared_total != len(rows):
        raise FactValidationError(f"{name}: declared total does not match rows")
    trading_dates = pagination.get("trading_dates")
    if trading_dates and any(time[:10] not in trading_dates for time in times):
        raise FactValidationError(f"{name}: row is outside declared trading dates")
    return {
        "required": True,
        "status": "complete",
        "rows": len(rows),
        "time_range": [times[0], times[-1]] if times else None,
        "pagination": pagination,
    }


def sync_with_fence(
    read_inventory: Callable[[], dict[str, object]],
    collect: Callable[[], object],
) -> object:
    before = read_inventory()
    result = collect()
    after = read_inventory()
    if before == after:
        return result
    retry_before = read_inventory()
    retry_result = collect()
    retry_after = read_inventory()
    if retry_before != retry_after:
        raise InventoryChanged("remote inventory changed twice")
    return retry_result


def export_structured_backtest(
    page: Page, target_url: str, stage_dir: Path
) -> list[dict[str, object]]:
    ensure_authenticated(page)
    parsed = urlsplit(target_url)
    filename = (
        unquote(Path(parsed.path).name)
        if parsed.scheme in {"http", "https"}
        else "research-export.json"
    )
    with TemporaryDirectory() as temporary_dir:
        downloaded = Path(temporary_dir) / (filename or "research-export.json")
        capture_download(
            page,
            lambda: page.evaluate(
                """({url, filename}) => {
                    const link = document.createElement("a");
                    link.href = url;
                    link.download = filename;
                    document.body.appendChild(link);
                    link.click();
                    link.remove();
                }""",
                {"url": target_url, "filename": downloaded.name},
            ),
            downloaded,
        )
        return [stage_external_file(downloaded, stage_dir)]
