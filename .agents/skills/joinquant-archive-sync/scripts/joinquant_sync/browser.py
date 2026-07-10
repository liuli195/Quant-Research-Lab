from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from playwright.sync_api import BrowserContext, Page, sync_playwright


class PageLike(Protocol):
    url: str


class AuthRequired(RuntimeError):
    """Raised when JoinQuant redirects to an authentication page."""


class FreeLogIncomplete(RuntimeError):
    """Raised when free log pagination stops without an explainable boundary."""


class PaidConfirmationRequired(RuntimeError):
    """Raised when a paid download is unconfirmed, changed, or already consumed."""


def ensure_authenticated(page: PageLike) -> None:
    url = page.url.lower()
    if "/login" in url or "/user/login" in url:
        raise AuthRequired("auth_required")


@contextmanager
def open_authenticated_context(
    profile_dir: Path,
    *,
    headless: bool,
) -> Iterator[BrowserContext]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            accept_downloads=True,
            headless=headless,
        )
        try:
            state_path = profile_dir / "storage-state.json"
            if state_path.is_file():
                context.set_storage_state(state_path)
            yield context
        finally:
            context.close()


def capture_download(
    page: Page,
    trigger: Callable[[], object],
    destination: Path,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with page.expect_download() as download_info:
        trigger()
    download_info.value.save_as(destination)
    return destination


def collect_free_logs(
    fetch_page: Callable[[int], dict[str, object]],
) -> tuple[list[dict[str, object]], str]:
    rows: list[dict[str, object]] = []
    offset = 0
    for _ in range(10_000):
        page = fetch_page(offset)
        page_rows = page.get("rows")
        if not isinstance(page_rows, list) or not all(
            isinstance(row, dict) for row in page_rows
        ):
            raise FreeLogIncomplete("log page rows must be objects")
        rows.extend(page_rows)
        if page.get("end"):
            return rows, "complete"
        if page_rows:
            offset += len(page_rows)
            continue
        if page.get("blocked_free"):
            return rows, "capped_free"
        raise FreeLogIncomplete(f"free logs stopped without end evidence at {offset}")
    raise FreeLogIncomplete("free log pagination exceeded 10000 pages")


def _quote_sha256(quote: dict[str, object]) -> str:
    payload = json.dumps(
        quote, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_paid_preview(
    run_id: str,
    log_type: str,
    range_: str,
    quote: dict[str, object],
) -> dict[str, object]:
    return {
        "preview_id": uuid.uuid4().hex,
        "run_id": run_id,
        "log_type": log_type,
        "range": range_,
        "quote": quote,
        "quote_sha256": _quote_sha256(quote),
    }


def consume_paid_preview(
    preview: dict[str, object],
    run_id: str,
    log_type: str,
    range_: str,
    quote: dict[str, object],
    confirm: bool,
    used_preview_ids: set[str],
) -> dict[str, object]:
    preview_id = str(preview.get("preview_id") or "")
    matches = (
        preview.get("run_id") == run_id
        and preview.get("log_type") == log_type
        and preview.get("range") == range_
        and preview.get("quote_sha256") == _quote_sha256(quote)
    )
    if not confirm or not preview_id or not matches or preview_id in used_preview_ids:
        raise PaidConfirmationRequired("paid preview confirmation is invalid")
    used_preview_ids.add(preview_id)
    return preview
