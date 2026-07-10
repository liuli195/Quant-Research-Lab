from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from playwright.sync_api import BrowserContext, Page, sync_playwright


class PageLike(Protocol):
    url: str


class AuthRequired(RuntimeError):
    """Raised when JoinQuant redirects to an authentication page."""


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
