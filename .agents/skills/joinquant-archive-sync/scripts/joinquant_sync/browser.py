from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from playwright.sync_api import BrowserContext, Page, sync_playwright


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_SESSION_FILE = "joinquant-session.dpapi"


def _dpapi(payload: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("Windows DPAPI is unavailable")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    operation.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p if protect else ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_DataBlob),
    ]
    operation.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    source = ctypes.create_string_buffer(payload)
    source_blob = _DataBlob(
        len(payload), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte))
    )
    destination = _DataBlob()
    description = "QuantResearchLab JoinQuant session" if protect else None
    if not operation(
        ctypes.byref(source_blob),
        description,
        None,
        None,
        None,
        0,
        ctypes.byref(destination),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel32.LocalFree(destination.data)


def persist_authenticated_session(context: BrowserContext, profile_dir: Path) -> bool:
    cookies = [
        cookie
        for cookie in context.cookies()
        if (domain := str(cookie.get("domain") or "").lstrip(".").lower())
        == "joinquant.com"
        or domain.endswith(".joinquant.com")
    ]
    if not cookies:
        return False
    profile_dir.mkdir(parents=True, exist_ok=True)
    destination = profile_dir / _SESSION_FILE
    temporary = destination.with_suffix(".tmp")
    payload = json.dumps({"cookies": cookies}, ensure_ascii=False).encode("utf-8")
    temporary.write_bytes(_dpapi(payload, protect=True))
    os.replace(temporary, destination)
    return True


def restore_authenticated_session(context: BrowserContext, profile_dir: Path) -> bool:
    source = profile_dir / _SESSION_FILE
    if not source.is_file():
        return False
    try:
        payload = json.loads(_dpapi(source.read_bytes(), protect=False).decode("utf-8"))
        cookies = payload.get("cookies")
        if not isinstance(cookies, list) or not all(
            isinstance(cookie, dict) for cookie in cookies
        ):
            return False
        context.add_cookies(cookies)
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return True


class PageLike(Protocol):
    url: str


class AuthRequired(RuntimeError):
    """Raised when JoinQuant redirects to an authentication page."""


class FreeLogIncomplete(RuntimeError):
    """Raised when free log pagination stops without an explainable boundary."""

    def __init__(
        self, message: str, *, raw_pages: list[dict[str, object]] | None = None
    ) -> None:
        super().__init__(message)
        self.raw_pages = list(raw_pages or [])


class PaidConfirmationRequired(RuntimeError):
    """Raised when a paid download is unconfirmed, changed, or already consumed."""


class SimulationDiscoveryError(RuntimeError):
    """Raised when an active simulation row lacks stable page evidence."""


class TargetDiscoveryError(RuntimeError):
    """Raised when a strategy or history target cannot be resolved exactly."""


SIMULATION_ACTIVE_STATUSES = frozenset({"0", "1", "3", "5"})
SIMULATION_CLOSED_STATUSES = frozenset({"2"})


def ensure_authenticated(page: PageLike) -> None:
    url = page.url.lower()
    if "/login" in url or "/user/login" in url:
        raise AuthRequired("auth_required")


def parse_active_simulation_rows(
    rows: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    return [item for item in parse_simulation_rows(rows) if item["status"] == "active"]


def parse_simulation_rows(
    rows: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen_spaces: set[str] = set()
    for ordinal, row in enumerate(rows, start=1):
        remote_status = str(row.get("status") or "")
        name = str(row.get("name") or "").strip()
        if not name and not row.get("detail_url"):
            continue
        if remote_status not in SIMULATION_ACTIVE_STATUSES | SIMULATION_CLOSED_STATUSES:
            raise SimulationDiscoveryError(
                f"unknown simulation status: {remote_status or '<empty>'}"
            )
        page_space_id = str(row.get("page_space_id") or "").strip()
        detail_url = urljoin(
            "https://www.joinquant.com", str(row.get("detail_url") or "").strip()
        )
        parsed = urlsplit(detail_url)
        if (
            not name
            or not page_space_id
            or page_space_id in seen_spaces
            or parsed.scheme != "https"
            or parsed.hostname not in {"joinquant.com", "www.joinquant.com"}
            or parsed.path != "/algorithm/live/index"
        ):
            raise SimulationDiscoveryError("active simulation page identity is invalid")
        seen_spaces.add(page_space_id)
        aliases = [
            parse_qs(parsed.query).get("backtestId", [""])[0],
            str(row.get("transport_id") or "").strip(),
        ]
        candidates.append(
            {
                "page_ordinal": str(ordinal),
                "name": name,
                "page_space_id": page_space_id,
                "status": (
                    "active"
                    if remote_status in SIMULATION_ACTIVE_STATUSES
                    else "closed"
                ),
                "detail_url": detail_url,
                "aliases": list(dict.fromkeys(alias for alias in aliases if alias)),
            }
        )
    return candidates


def discover_active_simulations(page: Page) -> list[dict[str, object]]:
    return [
        item for item in discover_all_simulations(page) if item["status"] == "active"
    ]


def discover_all_simulations(page: Page) -> list[dict[str, object]]:
    page.goto(
        "https://www.joinquant.com/algorithm/trade/list",
        wait_until="networkidle",
    )
    ensure_authenticated(page)
    rows = page.locator("tr[_status]").evaluate_all(
        """
        elements => elements.map(row => {
          const link = row.querySelector('td.name a[href*="/algorithm/live/index"]');
          const transport = row.querySelector('td[_backtestid]');
          return {
            status: row.getAttribute('_status') || '',
            name: link ? (link.textContent || '').trim() : '',
            page_space_id: row.getAttribute('data-backtestspaceid') || '',
            detail_url: link ? link.getAttribute('href') || '' : '',
            transport_id: transport ? transport.getAttribute('_backtestid') || '' : '',
          };
        })
        """
    )
    return parse_simulation_rows(rows)


def parse_simulation_page_status(status_value: str, page_text: str) -> str:
    value = status_value.strip()
    text = page_text.strip()
    if value in SIMULATION_ACTIVE_STATUSES:
        return "active"
    if value in SIMULATION_CLOSED_STATUSES and any(
        marker in text for marker in ("已关闭", "已结束", "已停止", "已终止")
    ):
        return "closed"
    return "unknown"


def inspect_simulation_status(page: Page, detail_url: str) -> str:
    page.goto(detail_url, wait_until="networkidle")
    ensure_authenticated(page)
    value = (
        page.locator("#status").input_value() if page.locator("#status").count() else ""
    )
    return parse_simulation_page_status(value, page.locator("body").inner_text())


def _clean_page_text(value: object) -> str:
    return " ".join(str(value or "").split())


def parse_history_rows(
    rows: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    targets: dict[str, dict[str, object]] = {}
    status_names = {
        "完成": "done",
        "失败": "failed",
        "已取消": "cancelled",
        "取消": "cancelled",
        "运行中": "running",
    }
    for row in rows:
        ordinal = _clean_page_text(row.get("page_ordinal"))
        name = _clean_page_text(row.get("name"))
        detail_id = _clean_page_text(row.get("detail_id"))
        if not re.fullmatch(r"[1-9]\d*", ordinal) or not name or not detail_id:
            raise TargetDiscoveryError("history page identity is invalid")
        status_text = _clean_page_text(row.get("status_text"))
        status = next(
            (value for label, value in status_names.items() if label in status_text),
            "unknown",
        )
        aliases = [
            detail_id,
            _clean_page_text(row.get("result_id")),
            _clean_page_text(row.get("source_id")),
        ]
        target: dict[str, object] = {
            "page_ordinal": ordinal,
            "name": name,
            "status": status,
            "created_at": _clean_page_text(row.get("created_at")),
            "date_range": _clean_page_text(row.get("date_range")),
            "detail_url": (
                "https://www.joinquant.com/algorithm/backtest/detail?backtestId="
                + detail_id
            ),
            "aliases": list(dict.fromkeys(alias for alias in aliases if alias)),
        }
        existing = targets.get(ordinal)
        if existing is not None and existing != target:
            raise TargetDiscoveryError(f"conflicting history page ordinal: {ordinal}")
        targets[ordinal] = target
    return sorted(targets.values(), key=lambda item: int(str(item["page_ordinal"])))


def discover_history_targets(page: Page, strategy_name: str) -> list[dict[str, object]]:
    selected = strategy_name.strip()
    if not selected:
        raise TargetDiscoveryError("strategy name is required")
    page.goto(
        "https://www.joinquant.com/algorithm/index/list", wait_until="networkidle"
    )
    ensure_authenticated(page)
    strategies = page.locator("tr.algorithm_list").evaluate_all(
        """
        elements => elements.map(row => {
          const name = row.querySelector('a.file_name');
          const history = row.querySelector('a[href*="/algorithm/backtest/list"]');
          return {
            name: name ? (name.textContent || '').trim() : '',
            history_url: history ? history.getAttribute('href') || '' : '',
          };
        })
        """
    )
    matches = [item for item in strategies if item.get("name") == selected]
    if len(matches) != 1 or not matches[0].get("history_url"):
        raise TargetDiscoveryError("strategy page is missing or ambiguous")
    page.goto(
        urljoin("https://www.joinquant.com", str(matches[0]["history_url"])),
        wait_until="networkidle",
    )
    ensure_authenticated(page)
    count_match = re.search(r"共有\s*(\d+)\s*个回测", page.locator("body").inner_text())
    if count_match is None:
        raise TargetDiscoveryError("history total count is missing")
    expected_count = int(count_match.group(1))
    page_urls = {
        page.url,
        *[
            urljoin("https://www.joinquant.com", href)
            for href in page.locator("ul#yw0 li.page a").evaluate_all(
                "elements => elements.map(link => link.getAttribute('href') || '')"
            )
            if href
        ],
    }
    row_script = """
        elements => elements.map(row => {
          const cells = row.querySelectorAll('td');
          const source = row.querySelector('.source-code');
          const name = row.querySelector('.backtest-name');
          const status = row.querySelector('.backtest-list__backtest-status');
          const created = row.querySelector('.backtest-list__td_create-time');
          return {
            page_ordinal: row.getAttribute('_idx') || '',
            name: name ? (name.getAttribute('title') || name.textContent || '').trim() : '',
            status_text: status ? (status.textContent || '').trim() : '',
            created_at: created ? (created.textContent || '').trim() : '',
            date_range: cells.length > 4 ? (cells[4].textContent || '').trim() : '',
            detail_id: row.getAttribute('_backtestid2') || '',
            result_id: row.getAttribute('_backtestid') || '',
            source_id: source ? source.getAttribute('_backtestid') || '' : '',
          };
        })
        """
    all_rows: list[dict[str, object]] = []
    for page_url in sorted(page_urls):
        if page.url != page_url:
            page.goto(page_url, wait_until="networkidle")
            ensure_authenticated(page)
        all_rows.extend(page.locator("tr.backtest-tr").evaluate_all(row_script))
    targets = parse_history_rows(all_rows)
    if len(targets) != expected_count:
        raise TargetDiscoveryError(
            f"history target count mismatch: expected {expected_count}, got {len(targets)}"
        )
    return targets


def fetch_strategy_default_code(page: Page, strategy_name: str) -> dict[str, object]:
    selected = strategy_name.strip()
    page.goto(
        "https://www.joinquant.com/algorithm/index/list", wait_until="networkidle"
    )
    ensure_authenticated(page)
    strategies = page.locator("tr.algorithm_list").evaluate_all(
        """
        elements => elements.map((row, index) => {
          const link = row.querySelector('a.file_name[href*="/algorithm/index/edit"]');
          return {
            page_ordinal: String(index + 1),
            name: link ? (link.textContent || '').trim() : '',
            edit_url: link ? link.getAttribute('href') || '' : '',
          };
        })
        """
    )
    matches = [item for item in strategies if item.get("name") == selected]
    if len(matches) != 1 or not matches[0].get("edit_url"):
        raise TargetDiscoveryError("strategy editor is missing or ambiguous")
    edit_url = urljoin("https://www.joinquant.com", str(matches[0]["edit_url"]))
    page.goto(edit_url, wait_until="domcontentloaded")
    ensure_authenticated(page)
    page.wait_for_selector("#ide-container, #code", state="attached", timeout=60_000)
    code = page.evaluate(
        """
        () => {
          if (window.ace && document.getElementById('ide-container')) {
            return window.ace.edit('ide-container').getValue();
          }
          const hidden = document.getElementById('code');
          return hidden ? hidden.value : '';
        }
        """
    )
    if not isinstance(code, str) or not code:
        raise TargetDiscoveryError("strategy default code is empty")
    return {
        "page_ordinal": str(matches[0]["page_ordinal"]),
        "name": selected,
        "edit_url": edit_url,
        "code": code,
    }


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
            restore_authenticated_session(context, profile_dir)
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


_CY_AJAX_JS = r"""
({ url }) => new Promise(resolve => {
  if (!window.Cy || typeof window.Cy.ajax !== "function") {
    resolve({ok: false, error: "Cy.ajax is unavailable"});
    return;
  }
  window.Cy.ajax(url, {
    success: (value, _status, xhr) => resolve({
      ok: true, value, raw_text: xhr && typeof xhr.responseText === "string" ? xhr.responseText : ""
    }),
    error: value => resolve({ok: false, raw_text: value && value.responseText || "", error: String(value && value.status || "request failed")}),
    fail: value => resolve({ok: false, raw_text: value && value.responseText || "", error: String(value && value.status || "request failed")}),
  });
})
"""


def _performance_profile_evidence(page: Page) -> dict[str, object]:
    tab = page.locator("a[href='#tab-profile']")
    surface_supported = tab.count() == 1
    payload = b""
    if surface_supported:
        page.wait_for_function(
            """() => {
                const element = document.querySelector("#profile-tab");
                return element && element.style.cursor === "pointer";
            }""",
            timeout=60_000,
        )
        tab.click()
        profile = page.locator("#profile pre")
        if profile.count() == 1:
            profile.wait_for(state="visible", timeout=60_000)
            payload = profile.inner_text().encode("utf-8")
    return {
        "performance_profile": payload,
        "performance_profile_surface_supported": surface_supported,
    }


def fetch_backtest_browser_evidence(page: Page, target_url: str) -> dict[str, object]:
    page.goto(target_url, wait_until="networkidle")
    ensure_authenticated(page)

    with page.expect_response(
        lambda response: "/algorithm/backtest/source" in response.url
    ) as source_info:
        page.locator("#code-tab").click()
    source_response = source_info.value
    source_document = source_response.json()
    source_data = source_document.get("data")
    if not isinstance(source_data, dict) or not isinstance(
        source_data.get("source"), str
    ):
        raise TargetDiscoveryError("backtest source response is invalid")
    code = str(source_data["source"])

    log_id = str(page.locator("#export-log-button").get_attribute("backtestid") or "")
    if not log_id:
        raise TargetDiscoveryError("backtest log alias is missing")

    raw_log_pages: list[dict[str, object]] = []

    def fetch_log_page(offset: int) -> dict[str, object]:
        result = page.evaluate(
            _CY_AJAX_JS,
            {"url": (f"/algorithm/backtest/log?backtestId={log_id}&offset={offset}")},
        )
        if not result.get("ok"):
            raw_log_pages.append(
                {
                    "offset": offset,
                    "raw_text": str(result.get("raw_text") or ""),
                    "transport_error": str(result.get("error") or "unknown error"),
                }
            )
            raise FreeLogIncomplete(
                f"backtest log request failed: {result.get('error') or 'unknown error'}",
                raw_pages=raw_log_pages,
            )
        response = result.get("value")
        if not isinstance(response, dict) or response.get("code") in {403, "403"}:
            raw_log_pages.append(
                {
                    "offset": offset,
                    "raw_text": str(result.get("raw_text") or ""),
                    "response": response,
                    "blocked_free": True,
                }
            )
            return {"rows": [], "blocked_free": True}
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("logArr"), list):
            raise FreeLogIncomplete("backtest log response is invalid")
        raw_log_pages.append(
            {
                "offset": offset,
                "raw_text": str(result.get("raw_text") or ""),
                "response": response,
            }
        )
        rows = [
            {"offset": offset + index, "text": str(line)}
            for index, line in enumerate(data["logArr"])
        ]
        terminal = not rows and (
            data.get("max") is True or data.get("state") in {2, 3, "2", "3"}
        )
        return {"rows": rows, "end": terminal}

    logs, log_status, normal_log_error = collect_backtest_logs(fetch_log_page)
    log_bytes = (
        "\n".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in logs
        )
        + ("\n" if logs else "")
    ).encode("utf-8")

    performance = _performance_profile_evidence(page)

    with TemporaryDirectory(prefix="joinquant-summary-") as directory:
        destination = Path(directory) / "official-summary.xls"
        page.locator("#backtest-menu-toggle").click()
        with page.expect_download(timeout=60_000) as download_info:
            page.locator("#export-csv-button").click()
        download_info.value.save_as(destination)
        official_summary = destination.read_bytes()
    if not official_summary:
        raise TargetDiscoveryError("official summary download is empty")

    return {
        "code": code,
        "source_raw": source_response.body(),
        "normal_log": log_bytes,
        "normal_log_status": log_status,
        "normal_log_rows": len(logs),
        "normal_log_records": logs,
        "normal_log_raw_pages": raw_log_pages,
        "normal_log_error": normal_log_error,
        "official_summary": official_summary,
        **performance,
        "params": {
            "start_date": page.locator("#start_date").input_value()
            if page.locator("#start_date").count()
            else "",
            "end_date": page.locator("#end_date").input_value()
            if page.locator("#end_date").count()
            else "",
        },
    }


def fetch_backtest_code_evidence(page: Page, target_url: str) -> dict[str, object]:
    page.goto(target_url, wait_until="networkidle")
    ensure_authenticated(page)
    with page.expect_response(
        lambda response: "/algorithm/backtest/source" in response.url
    ) as source_info:
        page.locator("#code-tab").click()
    document = source_info.value.json()
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("source"), str):
        raise TargetDiscoveryError("backtest source response is invalid")
    return {
        "code": data["source"],
        "params": {
            "start_date": page.locator("#start_date").input_value()
            if page.locator("#start_date").count()
            else "",
            "end_date": page.locator("#end_date").input_value()
            if page.locator("#end_date").count()
            else "",
        },
    }


def collect_simulation_logs(
    initial_offset: int,
    initial_lines: list[str],
    fetch_older: Callable[[int, int], dict[str, object]],
    *,
    stop_offset: int = 0,
) -> tuple[list[dict[str, object]], str]:
    if initial_offset < 0 or stop_offset < 0:
        raise FreeLogIncomplete("simulation log offset is invalid")
    cursor = initial_offset
    records: list[dict[str, object]] = [
        {"offset": cursor + index, "text": line}
        for index, line in enumerate(initial_lines)
    ]
    for _ in range(10_000):
        if stop_offset and cursor <= stop_offset:
            records.sort(key=lambda item: int(item["offset"]))
            return records, "incremental"
        if cursor <= 0:
            records.sort(key=lambda item: int(item["offset"]))
            return records, "complete"
        offset = max(stop_offset, 0, cursor - 100)
        limit = cursor - offset
        page = fetch_older(offset, limit)
        if page.get("blocked_free"):
            records.sort(key=lambda item: int(item["offset"]))
            return records, "capped_free"
        rows = page.get("rows")
        if not isinstance(rows, list) or not rows:
            raise FreeLogIncomplete("simulation older log ended without evidence")
        records.extend(
            {"offset": offset + index, "text": str(line)}
            for index, line in enumerate(rows)
        )
        cursor = offset
    raise FreeLogIncomplete("simulation log pagination exceeded safety limit")


def reuse_simulation_code_history(
    fresh_document: dict[str, object],
    previous_pages: object,
    history_total: int,
    known_total: object,
) -> tuple[list[object], list[dict[str, object]], bool]:
    fresh_data = fresh_document.get("data")
    fresh_items = fresh_data.get("list") if isinstance(fresh_data, dict) else None
    if not isinstance(fresh_items, list):
        raise TargetDiscoveryError("simulation code history is invalid")
    previous_history: list[object] = []
    valid_pages = (
        list(previous_pages)
        if isinstance(previous_pages, list)
        and all(isinstance(item, dict) for item in previous_pages)
        else []
    )
    for document in valid_pages:
        data = document.get("data")
        items = data.get("list") if isinstance(data, dict) else None
        if isinstance(items, list):
            previous_history.extend(items)
    reusable = (
        type(known_total) is int
        and known_total == history_total
        and len(previous_history) == history_total
        and previous_history[: len(fresh_items)] == fresh_items
    )
    if reusable:
        return (
            previous_history,
            [fresh_document, *valid_pages[1:]],
            True,
        )
    return list(fresh_items), [fresh_document], False


def simulation_history_cache_key(
    history_ordinal: int, add_time: str, mod_time: str
) -> str:
    return json.dumps(
        [history_ordinal, add_time, mod_time],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def fetch_simulation_code_versions(
    page: Page,
    history: Iterable[object],
    *,
    cached_sources: dict[str, str] | None = None,
    cached_history: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Resolve history metadata to the complete source of every code version."""
    history_rows = list(history)
    source_ids: list[str] = []
    sources = dict(cached_sources or {})
    history_cache = dict(cached_history or {})
    for ordinal, item in enumerate(history_rows, start=1):
        if not isinstance(item, dict):
            raise TargetDiscoveryError("simulation code history row is invalid")
        source_id = str(item.get("sourceBacktestId") or "").strip()
        if not source_id:
            raise TargetDiscoveryError("simulation code history source is missing")
        if source_id not in source_ids:
            source_ids.append(source_id)
        cache_key = simulation_history_cache_key(
            ordinal,
            str(item.get("addTime") or ""),
            str(item.get("modTime") or ""),
        )
        if source_id not in sources and cache_key in history_cache:
            sources[source_id] = history_cache[cache_key]
    for source_id in source_ids:
        if source_id in sources:
            if not sources[source_id].strip():
                raise TargetDiscoveryError(
                    f"simulation cached code source is invalid: {source_id}"
                )
            continue
        result = page.evaluate(
            _CY_AJAX_JS,
            {
                "url": "/algorithm/backtest/source?"
                + urlencode({"backtestId": source_id})
            },
        )
        document = result.get("value") if result.get("ok") else None
        data = document.get("data") if isinstance(document, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("source"), str):
            raise TargetDiscoveryError(
                f"simulation code history source is invalid: {source_id}"
            )
        sources[source_id] = str(data["source"])
    return [
        {
            "history_ordinal": ordinal,
            "live_history_id": str(item.get("liveHistoryId") or ""),
            "source_backtest_id": str(item["sourceBacktestId"]),
            "add_time": str(item.get("addTime") or ""),
            "mod_time": str(item.get("modTime") or ""),
            "code": sources[str(item["sourceBacktestId"])],
        }
        for ordinal, item in enumerate(history_rows, start=1)
        if isinstance(item, dict)
    ]


def fetch_simulation_browser_evidence(
    page: Page,
    candidate: dict[str, object],
    incremental: dict[str, object] | None = None,
) -> dict[str, object]:
    with page.expect_response(
        lambda response: "/algorithm/live/getLiveHistoryList" in response.url,
        timeout=60_000,
    ) as history_info:
        page.goto(str(candidate["detail_url"]), wait_until="networkidle")
    ensure_authenticated(page)
    history_document = history_info.value.json()
    history_data = history_document.get("data")
    if not isinstance(history_data, dict) or not isinstance(
        history_data.get("list"), list
    ):
        raise TargetDiscoveryError("simulation code history is invalid")
    try:
        history_total = int(history_data.get("totalCount"))
    except (TypeError, ValueError) as error:
        raise TargetDiscoveryError(
            "simulation code history total is invalid"
        ) from error
    previous_pages = (
        incremental.get("code_history_pages")
        if isinstance(incremental, dict)
        else None
    )
    known_total = (
        incremental.get("code_history_total")
        if isinstance(incremental, dict)
        else None
    )
    history, history_pages, reuse_previous = reuse_simulation_code_history(
        history_document,
        previous_pages,
        history_total,
        known_total,
    )
    history_url = urlsplit(history_info.value.url)
    history_query = parse_qs(history_url.query)
    history_alias = history_query.get("backtestId", [""])[0]
    history_limit = int(history_query.get("limit", ["20"])[0])
    if not history_alias or history_limit < 1:
        raise TargetDiscoveryError(
            "simulation code history request identity is invalid"
        )
    page_numbers = (
        []
        if reuse_previous
        else range(2, (history_total + history_limit - 1) // history_limit + 1)
    )
    for page_number in page_numbers:
        result = page.evaluate(
            _CY_AJAX_JS,
            {
                "url": history_url.path
                + "?"
                + urlencode(
                    {
                        "backtestId": history_alias,
                        "page": page_number,
                        "limit": history_limit,
                        "ajax": 1,
                    }
                )
            },
        )
        document = result.get("value") if result.get("ok") else None
        data = document.get("data") if isinstance(document, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise TargetDiscoveryError("simulation code history page is invalid")
        history.extend(data["list"])
        history_pages.append(document)
    if len(history) != history_total:
        raise TargetDiscoveryError(
            f"simulation code history count mismatch: expected {history_total}, got {len(history)}"
        )

    with page.expect_response(
        lambda response: "/algorithm/backtest/source" in response.url,
        timeout=60_000,
    ) as source_info:
        page.locator("#code-tab,#code-item").first.click()
    source_document = source_info.value.json()
    source_data = source_document.get("data")
    if not isinstance(source_data, dict) or not isinstance(
        source_data.get("source"), str
    ):
        raise TargetDiscoveryError("simulation source response is invalid")

    with page.expect_response(
        lambda response: "/algorithm/live/log" in response.url,
        timeout=60_000,
    ) as log_info:
        page.locator("#logs-tab,#log-item").first.click()
    log_url = urlsplit(log_info.value.url)
    log_id = parse_qs(log_url.query).get("backtestId", [""])[0]
    if not log_id:
        raise TargetDiscoveryError("simulation log alias is missing")

    pages: list[dict[str, object]] = []
    raw_log_pages: list[dict[str, object]] = []
    initial_raw_text = log_info.value.body().decode("utf-8")
    try:
        response = json.loads(initial_raw_text)
    except json.JSONDecodeError as error:
        raise FreeLogIncomplete(
            "simulation log response is invalid JSON",
            raw_pages=[{"cursor": 0, "raw_text": initial_raw_text}],
        ) from error
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("logArr"), list):
        raise FreeLogIncomplete("simulation log response is invalid")
    cursor = int(data.get("offset") or 0)
    initial_lines = [str(line) for line in data["logArr"]]
    raw_log_pages.append(
        {
            "cursor": cursor,
            "raw_text": initial_raw_text,
            "response": response,
        }
    )
    pages.append({"cursor": cursor, "rows": len(initial_lines), "mode": "latest"})

    def fetch_older(offset: int, limit: int) -> dict[str, object]:
        result = page.evaluate(
            _CY_AJAX_JS,
            {
                "url": (
                    "/algorithm/live/log?addLog=1&backtestId="
                    f"{log_id}&offset={offset}&limit={limit}"
                )
            },
        )
        if not result.get("ok"):
            raw_log_pages.append(
                {
                    "cursor": offset,
                    "raw_text": str(result.get("raw_text") or ""),
                    "transport_error": str(result.get("error") or "unknown error"),
                }
            )
            raise FreeLogIncomplete(
                f"simulation older log request failed: {result.get('error') or 'unknown error'}",
                raw_pages=raw_log_pages,
            )
        value = result.get("value")
        if not isinstance(value, dict):
            raise FreeLogIncomplete("simulation older log response is invalid")
        if value.get("code") in {403, "403"}:
            raw_log_pages.append(
                {
                    "cursor": offset,
                    "raw_text": str(result.get("raw_text") or ""),
                    "response": value,
                    "blocked_free": True,
                }
            )
            pages.append(
                {"cursor": offset, "rows": 0, "mode": "older", "blocked_free": True}
            )
            return {"rows": [], "blocked_free": True}
        older_data = value.get("data")
        if not isinstance(older_data, dict) or not isinstance(
            older_data.get("logArr"), list
        ):
            raise FreeLogIncomplete("simulation older log response is invalid")
        raw_log_pages.append(
            {
                "cursor": offset,
                "raw_text": str(result.get("raw_text") or ""),
                "response": value,
            }
        )
        older_lines = [str(line) for line in older_data["logArr"]]
        pages.append({"cursor": offset, "rows": len(older_lines), "mode": "older"})
        return {"rows": older_lines}

    stop_offset = (
        incremental.get("normal_log_stop_offset", 0)
        if isinstance(incremental, dict)
        else 0
    )
    if type(stop_offset) is not int:
        raise TargetDiscoveryError("simulation incremental log offset is invalid")
    records, log_status = collect_simulation_logs(
        cursor,
        initial_lines,
        fetch_older,
        stop_offset=stop_offset,
    )
    log_bytes = (
        "\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in records
        )
        + ("\n" if records else "")
    ).encode("utf-8")

    cached_sources = (
        incremental.get("code_version_cache")
        if isinstance(incremental, dict)
        else None
    )
    if cached_sources is not None and not isinstance(cached_sources, dict):
        raise TargetDiscoveryError("simulation code version cache is invalid")
    cached_history = (
        incremental.get("code_history_cache")
        if isinstance(incremental, dict)
        else None
    )
    if cached_history is not None and not isinstance(cached_history, dict):
        raise TargetDiscoveryError("simulation code history cache is invalid")
    code_history_versions = fetch_simulation_code_versions(
        page,
        history,
        cached_sources={
            str(key): str(value) for key, value in (cached_sources or {}).items()
        },
        cached_history={
            str(key): str(value) for key, value in (cached_history or {}).items()
        },
    )
    code_versions = [str(item["code"]) for item in code_history_versions]
    source_backtest = next(
        (
            str(item.get("sourceBacktestId"))
            for item in history
            if isinstance(item, dict) and item.get("sourceBacktestId")
        ),
        "",
    )
    research_id = (
        page.locator("#backtestId").input_value()
        if page.locator("#backtestId").count()
        else str((candidate.get("aliases") or [""])[0])
    )
    performance = _performance_profile_evidence(page)
    return {
        "code": str(source_data["source"]),
        "source_raw": source_info.value.body(),
        "code_versions": code_versions,
        "code_history_versions": code_history_versions,
        "code_version_cache": {
            str(item["source_backtest_id"]): str(item["code"])
            for item in code_history_versions
        },
        "code_history_cache": {
            simulation_history_cache_key(
                int(item["history_ordinal"]),
                str(item["add_time"]),
                str(item["mod_time"]),
            ): str(item["code"])
            for item in code_history_versions
        },
        "code_history": history,
        "code_history_pages": history_pages,
        "code_history_total": history_total,
        "source_backtest": source_backtest,
        "research_id": research_id,
        "normal_log": log_bytes,
        "normal_log_status": log_status,
        "normal_log_rows": len(records),
        "normal_log_records": records,
        "normal_log_raw_pages": raw_log_pages,
        "log_pages": pages,
        **performance,
        "params": {
            "start_date": page.locator("#startDate").input_value()
            if page.locator("#startDate").count()
            else "",
            "status": page.locator("#status").input_value()
            if page.locator("#status").count()
            else "",
        },
    }


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


def collect_backtest_logs(
    fetch_page: Callable[[int], dict[str, object]],
) -> tuple[list[dict[str, object]], str, dict[str, str] | None]:
    try:
        rows, status = collect_free_logs(fetch_page)
    except FreeLogIncomplete as error:
        return [], "failed", {"type": type(error).__name__, "message": str(error)}
    return rows, status, None


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
            "source_url",
            "object_path",
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
            raise PaidConfirmationRequired(
                "paid preview was already consumed"
            ) from error


def create_paid_preview(
    run_id: str,
    log_type: str,
    range_: str,
    quote: dict[str, object],
    *,
    store_dir: Path | None = None,
    source_url: str = "",
    object_path: str = "",
) -> dict[str, object]:
    preview: dict[str, object] = {
        "preview_id": uuid.uuid4().hex,
        "run_id": run_id,
        "log_type": log_type,
        "range": range_,
        "quote": quote,
        "quote_sha256": _quote_sha256(quote),
        "source_url": source_url,
        "object_path": object_path,
    }
    preview["signature"] = _preview_signature(preview, store_dir)
    directory = _paid_store_dir(store_dir) / "paid-previews"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{preview['preview_id']}.json"
    path.write_text(
        json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return preview


def load_paid_preview(
    preview_id: str, *, store_dir: Path | None = None
) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{32}", preview_id):
        raise PaidConfirmationRequired("paid preview id is invalid")
    path = _paid_store_dir(store_dir) / "paid-previews" / f"{preview_id}.json"
    try:
        preview = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise PaidConfirmationRequired("paid preview was not found") from error
    if not isinstance(preview, dict) or not hmac.compare_digest(
        str(preview.get("signature") or ""), _preview_signature(preview, store_dir)
    ):
        raise PaidConfirmationRequired("paid preview signature is invalid")
    return preview


def open_paid_log_quote(page: Page, source_url: str) -> dict[str, object]:
    page.goto(source_url, wait_until="networkidle")
    ensure_authenticated(page)
    button = page.locator("#export-log-button")
    if button.count() != 1:
        raise PaidConfirmationRequired("this run does not expose paid log export")
    with page.expect_response(
        lambda response: "/credits/index/getUserCreditsInfo" in response.url,
        timeout=60_000,
    ) as quote_info:
        button.evaluate("element => element.click()")
    document = quote_info.value.json()
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("reduce"), int):
        raise PaidConfirmationRequired("JoinQuant paid log quote is invalid")
    return {
        "credits": data["reduce"],
        "available_credits": data.get("amount"),
        "rule_key": "export_log",
        "remote_scope": "full_log",
    }


def download_confirmed_paid_log(
    page: Page,
    preview: dict[str, object],
    destination: Path,
    *,
    confirm: bool,
    store_dir: Path | None = None,
) -> dict[str, object]:
    source_url = str(preview.get("source_url") or "")
    quote = open_paid_log_quote(page, source_url)
    consume_paid_preview(
        preview,
        str(preview.get("run_id") or ""),
        str(preview.get("log_type") or ""),
        str(preview.get("range") or ""),
        quote,
        confirm,
        set(),
        store_dir=store_dir,
    )
    confirm_button = (
        page.locator(".modal:visible button, .bootstrap-dialog:visible button")
        .filter(has_text="确定")
        .last
    )
    with page.expect_download(timeout=300_000) as download_info:
        confirm_button.click()
    destination.parent.mkdir(parents=True, exist_ok=True)
    download_info.value.save_as(destination)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise PaidConfirmationRequired("paid log download is empty")
    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "quote": quote,
    }


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
