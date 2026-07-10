from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
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


def _paid_store_dir(store_dir: Path | None) -> Path:
    if store_dir is not None:
        return store_dir
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise PaidConfirmationRequired("LOCALAPPDATA is required for paid previews")
    return Path(local_app_data) / "QuantResearchLab" / "joinquant-playwright"


def _paid_secret(store_dir: Path | None) -> bytes:
    directory = _paid_store_dir(store_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "paid-preview.key"
    try:
        with path.open("xb") as stream:
            stream.write(secrets.token_bytes(32))
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except FileExistsError:
        pass
    secret = path.read_bytes()
    if len(secret) != 32:
        raise PaidConfirmationRequired("paid preview key is invalid")
    return secret


def _preview_payload(preview: dict[str, object]) -> bytes:
    signed = {
        name: preview.get(name)
        for name in (
            "preview_id",
            "run_id",
            "log_type",
            "range",
            "quote",
            "quote_sha256",
        )
    }
    return json.dumps(
        signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _preview_signature(preview: dict[str, object], store_dir: Path | None) -> str:
    return hmac.new(
        _paid_secret(store_dir), _preview_payload(preview), hashlib.sha256
    ).hexdigest()


def _consume_preview_id(preview_id: str, store_dir: Path | None) -> None:
    database = _paid_store_dir(store_dir) / "paid-preview.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS used_preview "
            "(preview_id TEXT PRIMARY KEY, used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        try:
            connection.execute(
                "INSERT INTO used_preview(preview_id) VALUES (?)", (preview_id,)
            )
            connection.commit()
        except sqlite3.IntegrityError as error:
            raise PaidConfirmationRequired("paid preview was already consumed") from error


def create_paid_preview(
    run_id: str,
    log_type: str,
    range_: str,
    quote: dict[str, object],
    *,
    store_dir: Path | None = None,
) -> dict[str, object]:
    preview: dict[str, object] = {
        "preview_id": uuid.uuid4().hex,
        "run_id": run_id,
        "log_type": log_type,
        "range": range_,
        "quote": quote,
        "quote_sha256": _quote_sha256(quote),
    }
    preview["signature"] = _preview_signature(preview, store_dir)
    return preview


def consume_paid_preview(
    preview: dict[str, object],
    run_id: str,
    log_type: str,
    range_: str,
    quote: dict[str, object],
    confirm: bool,
    used_preview_ids: set[str],
    *,
    store_dir: Path | None = None,
) -> dict[str, object]:
    preview_id = str(preview.get("preview_id") or "")
    matches = (
        preview.get("run_id") == run_id
        and preview.get("log_type") == log_type
        and preview.get("range") == range_
        and preview.get("quote_sha256") == _quote_sha256(quote)
        and preview.get("quote") == quote
    )
    signature = str(preview.get("signature") or "")
    valid_signature = hmac.compare_digest(
        signature, _preview_signature(preview, store_dir)
    )
    if (
        not confirm
        or not preview_id
        or not matches
        or not valid_signature
        or preview_id in used_preview_ids
    ):
        raise PaidConfirmationRequired("paid preview confirmation is invalid")
    _consume_preview_id(preview_id, store_dir)
    used_preview_ids.add(preview_id)
    return preview
