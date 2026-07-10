from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from playwright.sync_api import Page

from joinquant_sync.archive import stage_external_file
from joinquant_sync.browser import capture_download, ensure_authenticated


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
