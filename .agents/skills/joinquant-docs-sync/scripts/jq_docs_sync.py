from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_SOURCES = SKILL_ROOT / "references" / "sources.json"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "joinquant-api"
ERROR_MARKERS = ("页面不存在", "系统错误", "访问被拒绝")
FUNCTION_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\([^()\n]{0,240}\)")
TABLE_RE = re.compile(
    r"\b(?:finance|macro|opt|bond|fund|jy|valuation)\.[A-Z][A-Z0-9_]+\b"
)
FENCE_TOKEN_RE = re.compile(r"```([A-Za-z0-9_+-]+)?")
FACTOR_SOURCE_IDS = {"alpha101", "alpha191", "technical-analysis"}
ALPHA_HELPER_NOISE_NAMES = {"a", "i"}
ALPHA_HEADING_RE = re.compile(r"^(alpha_\d{3})(?:$|[（(])")
TECHNICAL_HEADING_RE = re.compile(
    r"^([A-Z][A-Z0-9_]*(?:-[A-Z][A-Z0-9_]*)*)\s*-\s*[\u4e00-\u9fff]"
)
TECHNICAL_HEADING_OVERRIDES = {
    "BOLL-": "Bollinger_Bands",
    "CDP-STD-": "CDP_STD",
    "LWR-LWR": "LWR",
    "SG-LB-": "SG_LB",
    "SG-PF-": "SG_PF",
    "SG-SMX-": "SG_SMX",
    "TBP-STD-": "TBP_STD",
}
NON_API_CALL_NAMES = {
    "DataFrame",
    "BytesIO",
    "DECIMAL",
    "Dict",
    "INTEGER",
    "LOG",
    "MyObject",
    "Rank",
    "STD",
    "TTM",
    "Series",
    "ST",
    "StringIO",
    "TINYINT",
    "VARCHAR",
    "XDCE",
    "XSGE",
    "__init__",
    "a",
    "all",
    "any",
    "append",
    "array",
    "Aroon",
    "AVEDEV",
    "AvgQ",
    "char",
    "code",
    "conbond",
    "date",
    "decode",
    "decimal",
    "datetime",
    "dict",
    "display_name",
    "end_date",
    "enumerate",
    "etf",
    "factor",
    "filter",
    "float",
    "format",
    "func",
    "func1",
    "groupby",
    "head",
    "i",
    "in_",
    "index",
    "int",
    "isnan",
    "isoweekday",
    "items",
    "keys",
    "len",
    "like",
    "limit",
    "list",
    "loads",
    "map",
    "major_xs",
    "max",
    "mean",
    "min",
    "minor_xs",
    "now",
    "open",
    "options",
    "pcf_ratio",
    "print",
    "ps_ratio",
    "rand",
    "range",
    "read",
    "report_type",
    "repr",
    "round",
    "set",
    "sorted",
    "sort_values",
    "std",
    "stock",
    "str",
    "strftime",
    "sum",
    "tail",
    "time",
    "today",
    "to_csv",
    "tuple",
    "type",
    "values",
    "write",
    "weight",
    "xs",
    "varchar",
    "futures",
    "zip",
}
DETAIL_HEADINGS = {
    "调用方法",
    "参数",
    "输入",
    "返回",
    "返回值",
    "输出",
    "示例",
    "注意",
    "字段",
    "字段说明",
}


class SyncError(RuntimeError):
    pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_sources(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"无法读取来源清单: {path}: {exc}") from exc
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, list) or not sources:
        raise SyncError("来源清单必须包含非空 sources 列表")

    seen: dict[str, set[str]] = {"source_id": set(), "url": set(), "output": set()}
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            raise SyncError(f"来源 #{index} 必须是对象")
        item = {
            "source_id": str(source.get("source_id", "")).strip(),
            "url": str(source.get("url", "")).strip(),
            "output": str(source.get("output", "")).strip(),
            "selector": str(source.get("selector", "")).strip(),
            "min_chars": source.get("min_chars"),
            "required_markers": source.get("required_markers", []),
        }
        if not all((item["source_id"], item["url"], item["output"], item["selector"])):
            raise SyncError(f"来源 #{index} 缺少必填字段")
        parsed = urlparse(item["url"])
        if parsed.scheme != "https" or not (
            parsed.hostname == "joinquant.com"
            or (parsed.hostname or "").endswith(".joinquant.com")
        ):
            raise SyncError(f"来源必须是聚宽 HTTPS 官方地址: {item['url']}")
        output = Path(item["output"])
        if output.name != item["output"] or output.suffix.lower() != ".md":
            raise SyncError(f"目标文件必须是目录内 Markdown 文件名: {item['output']}")
        try:
            item["min_chars"] = int(item["min_chars"])
        except (TypeError, ValueError) as exc:
            raise SyncError(f"来源 {item['source_id']} 的 min_chars 无效") from exc
        if item["min_chars"] < 1:
            raise SyncError(f"来源 {item['source_id']} 的 min_chars 必须大于 0")
        markers = item["required_markers"]
        if not isinstance(markers, list) or any(
            not isinstance(marker, str) or not marker.strip() for marker in markers
        ):
            raise SyncError(f"来源 {item['source_id']} 的 required_markers 无效")
        item["required_markers"] = [marker.strip() for marker in markers]
        for field in seen:
            value = str(item[field])
            if value in seen[field]:
                raise SyncError(f"来源清单 {field} 冲突: {value}")
            seen[field].add(value)
        normalized.append(item)
    return normalized


def _inline(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if node.name in {"script", "style", "noscript", "svg"}:
        return ""
    if node.name == "br":
        return "\n"
    text = "".join(_inline(child) for child in node.children).strip()
    if node.name == "a" and text:
        href = str(node.get("href", "")).strip()
        if href.startswith("/"):
            href = urljoin("https://www.joinquant.com", href)
        return f"[{text}]({href})" if href else text
    if node.name == "code" and text:
        return f"`{text}`"
    if node.name in {"strong", "b"} and text:
        return f"**{text}**"
    if node.name in {"em", "i"} and text:
        return f"*{text}*"
    return text


def _table_to_markdown(table: Tag) -> str:
    rows: list[list[str]] = []
    for row in table.find_all("tr"):
        cells = [
            re.sub(r"\s+", " ", _inline(cell)).strip()
            for cell in row.find_all(["th", "td"])
        ]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    rendered = ["| " + " | ".join(row) + " |" for row in rows]
    rendered.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(rendered) + "\n\n"


def _block(node: Tag | NavigableString) -> str:
    if isinstance(node, NavigableString):
        return "" if not str(node).strip() else _inline(node)
    name = node.name or ""
    if name in {"script", "style", "noscript", "svg", "button", "form"}:
        return ""
    if re.fullmatch(r"h[1-6]", name):
        level = int(name[1])
        return f"{'#' * level} {_inline(node)}\n\n"
    if name == "pre":
        code = node.get_text("", strip=False).strip().replace("```", "`` `")
        return f"```\n{code}\n```\n\n"
    if name == "table":
        return _table_to_markdown(node)
    if name in {"ul", "ol"}:
        lines: list[str] = []
        ordered = name == "ol"
        for number, item in enumerate(node.find_all("li", recursive=False), start=1):
            prefix = f"{number}. " if ordered else "- "
            inline_parts: list[str] = []
            nested_blocks: list[str] = []
            for child in item.children:
                if isinstance(child, Tag) and child.name in {
                    "blockquote",
                    "ol",
                    "pre",
                    "table",
                    "ul",
                }:
                    nested_blocks.append(_block(child).strip())
                else:
                    inline_parts.append(_inline(child))
            inline_text = re.sub(r"\s+", " ", "".join(inline_parts)).strip()
            rendered = prefix + inline_text
            if nested_blocks:
                rendered += "\n\n" + "\n\n".join(nested_blocks)
            lines.append(rendered.rstrip())
        return "\n".join(lines) + "\n\n" if lines else ""
    if name == "blockquote":
        text = node.get_text(" ", strip=True)
        return "\n".join(f"> {line}" for line in text.splitlines()) + "\n\n"
    if name in {"p", "figcaption"}:
        text = _inline(node).strip()
        return f"{text}\n\n" if text else ""
    if name == "hr":
        return "---\n\n"
    return "".join(_block(child) for child in node.children)


def _normalize_markdown_fences(markdown: str) -> str:
    parts: list[str] = []
    position = 0
    for match in FENCE_TOKEN_RE.finditer(markdown):
        before = markdown[position : match.start()].rstrip(" \t")
        parts.append(before)
        if before and not before.endswith("\n"):
            parts.append("\n")
        parts.append(f"```{match.group(1) or ''}\n")
        position = match.end()
        while position < len(markdown) and markdown[position] in " \t":
            position += 1
    parts.append(markdown[position:])
    return "".join(parts)


def _validate_markdown_fences(markdown: str) -> None:
    fence_lines = [line for line in markdown.splitlines() if "```" in line]
    if any(not re.fullmatch(r"```[A-Za-z0-9_+-]*", line) for line in fence_lines):
        raise SyncError("Markdown 包含行内代码围栏")
    if len(fence_lines) % 2:
        raise SyncError("Markdown 代码围栏未闭合")


def html_to_markdown(
    html: str, selector: str, min_chars: int, required_markers: list[str]
) -> str:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(selector)
    if container is None:
        raise SyncError(f"找不到正文选择器: {selector}")
    plain = re.sub(r"\s+", " ", container.get_text(" ", strip=True)).strip()
    if len(plain) < min_chars:
        raise SyncError(f"正文长度 {len(plain)} 低于门禁 {min_chars}")
    if any(marker in plain[:500] for marker in ERROR_MARKERS):
        raise SyncError("页面命中错误页特征")
    missing_markers = [marker for marker in required_markers if marker not in plain]
    if missing_markers:
        raise SyncError(f"正文缺少完整性标记: {', '.join(missing_markers)}")
    markdown = _normalize_markdown_fences(_block(container))
    markdown = re.sub(
        r"\]\((/[^)\s]+)\)",
        lambda match: f"]({urljoin('https://www.joinquant.com', match.group(1))})",
        markdown,
    )
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"
    _validate_markdown_fences(markdown)
    return markdown


def extract_api_entries(markdown: str, source_id: str) -> list[dict[str, str]]:
    sections: list[tuple[str, str]] = []
    heading = ""
    body: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        heading_match = None if in_fence else re.match(r"^#{1,6} (.+)$", line)
        if heading_match and heading_match.group(1).strip() not in DETAIL_HEADINGS:
            if heading or body:
                sections.append((heading, "\n".join(body)))
            heading = heading_match.group(1).strip()
            body = []
        else:
            body.append(line)
    if heading or body:
        sections.append((heading, "\n".join(body)))

    entries: dict[str, dict[str, str]] = {}
    for current_heading, section in sections:
        catalog_match = None
        catalog_name = None
        if source_id in {"alpha101", "alpha191"}:
            catalog_match = ALPHA_HEADING_RE.match(current_heading)
        elif source_id == "technical-analysis":
            catalog_name = next(
                (
                    name
                    for prefix, name in TECHNICAL_HEADING_OVERRIDES.items()
                    if current_heading.startswith(prefix)
                ),
                None,
            )
            if catalog_name is None:
                catalog_match = TECHNICAL_HEADING_RE.match(current_heading)
        if catalog_match or catalog_name:
            name = catalog_name or catalog_match.group(1)
            key = f"{source_id}:factor:{name}"
            entries[key] = {
                "key": key,
                "source_id": source_id,
                "kind": "factor",
                "name": name,
                "heading": current_heading,
                "evidence": current_heading,
            }
        if source_id in {"alpha101", "alpha191"} and current_heading.startswith(
            "alpha（"
        ):
            key = f"{source_id}:function:alpha"
            entries[key] = {
                "key": key,
                "source_id": source_id,
                "kind": "function",
                "name": "alpha",
                "heading": current_heading,
                "evidence": current_heading,
            }
        if (
            source_id in {"alpha101", "alpha191"}
            and current_heading == "公用函数说明"
        ):
            for match in FUNCTION_RE.finditer(section):
                name = match.group(1)
                if name in ALPHA_HELPER_NOISE_NAMES:
                    continue
                key = f"{source_id}:function:{name}"
                entries[key] = {
                    "key": key,
                    "source_id": source_id,
                    "kind": "function",
                    "name": name,
                    "heading": current_heading,
                    "evidence": match.group(0),
                }
        has_input = "参数" in section or "输入" in section
        has_output = "返回" in section or "输出" in section
        if has_input and has_output and source_id not in FACTOR_SOURCE_IDS:
            for match in FUNCTION_RE.finditer(section):
                name = match.group(1)
                if name in NON_API_CALL_NAMES:
                    continue
                kind = (
                    "factor"
                    if name.startswith("alpha_") or source_id in FACTOR_SOURCE_IDS
                    else "function"
                )
                key = f"{source_id}:{kind}:{name}"
                entries[key] = {
                    "key": key,
                    "source_id": source_id,
                    "kind": kind,
                    "name": name,
                    "heading": current_heading,
                    "evidence": match.group(0),
                }
        for table in TABLE_RE.findall(section):
            key = f"{source_id}:table:{table}"
            entries[key] = {
                "key": key,
                "source_id": source_id,
                "kind": "table",
                "name": table,
                "heading": current_heading,
                "evidence": table,
            }
    if source_id == "factor-values":
        in_factor_table = False
        for line in markdown.splitlines():
            if not line.startswith("|"):
                in_factor_table = False
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and cells[0].replace(" ", "").lower() == "因子code":
                in_factor_table = True
                continue
            if not in_factor_table or not cells:
                continue
            name = cells[0]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
                continue
            key = f"{source_id}:factor:{name}"
            entries[key] = {
                "key": key,
                "source_id": source_id,
                "kind": "factor",
                "name": name,
                "heading": "因子目录",
                "evidence": line,
            }
    if source_id == "macro-data":
        for match in re.finditer(r"表名[：:]\s*(MAC_[A-Z0-9_]+)", markdown):
            table = f"macro.{match.group(1)}"
            key = f"{source_id}:table:{table}"
            entries[key] = {
                "key": key,
                "source_id": source_id,
                "kind": "table",
                "name": table,
                "heading": "宏观数据表",
                "evidence": match.group(0),
            }
    return [entries[key] for key in sorted(entries)]


def _fixture_html(source: dict[str, Any], html_dir: Path) -> str:
    path = html_dir / f"{source['source_id']}.html"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SyncError(f"无法读取离线页面: {path}: {exc}") from exc


def _live_html(sources: list[dict[str, Any]]) -> dict[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SyncError("项目 .venv 缺少 Playwright，请安装仓库运行依赖") from exc
    rendered: dict[str, str] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            for source in sources:
                response = page.goto(source["url"], wait_until="domcontentloaded")
                if response is None or response.status >= 400:
                    status = "无响应" if response is None else response.status
                    raise SyncError(f"来源 {source['source_id']} 请求失败: {status}")
                page.wait_for_function(
                    "([selector, minChars, markers]) => { const text = document.querySelector(selector)?.innerText || ''; return text.length >= minChars && markers.every(marker => text.includes(marker)); }",
                    arg=[
                        source["selector"],
                        source["min_chars"],
                        source["required_markers"],
                    ],
                    timeout=30_000,
                )
                final_host = urlparse(page.url).hostname or ""
                if final_host != "joinquant.com" and not final_host.endswith(
                    ".joinquant.com"
                ):
                    raise SyncError(f"来源 {source['source_id']} 被重定向到非聚宽域名")
                rendered[source["source_id"]] = page.content()
        finally:
            browser.close()
    return rendered


def build_candidates(
    sources: list[dict[str, Any]], html_dir: Path | None
) -> tuple[dict[str, bytes], list[dict[str, str]]]:
    html_by_id = (
        {source["source_id"]: _fixture_html(source, html_dir) for source in sources}
        if html_dir is not None
        else _live_html(sources)
    )
    candidates: dict[str, bytes] = {}
    entries: list[dict[str, str]] = []
    for source in sources:
        markdown = html_to_markdown(
            html_by_id[source["source_id"]],
            source["selector"],
            source["min_chars"],
            source["required_markers"],
        )
        candidates[source["output"]] = markdown.encode("utf-8")
        entries.extend(extract_api_entries(markdown, source["source_id"]))
    keys = [entry["key"] for entry in entries]
    if len(keys) != len(set(keys)):
        raise SyncError("API 稳定键发生冲突")
    entries.sort(key=lambda entry: entry["key"])
    candidates["api-index.json"] = _json_text(
        {"version": 1, "entries": entries}
    ).encode("utf-8")
    return candidates, entries


def _changed_files(output: Path, candidates: dict[str, bytes]) -> list[str]:
    changed: list[str] = []
    for name, data in candidates.items():
        path = output / name
        if not path.is_file() or path.read_bytes() != data:
            changed.append(name)
    return sorted(changed)


def _read_json_if_present(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"无法读取现有 JSON 文件: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"现有 JSON 文件必须是对象: {path}")
    return value


def _preview_changes(
    sources: list[dict[str, Any]],
    output: Path,
    candidates: dict[str, bytes],
    entries: list[dict[str, str]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    document_names = {source["output"] for source in sources}
    added = sorted(name for name in document_names if not (output / name).is_file())
    modified = sorted(
        name
        for name in document_names
        if (output / name).is_file()
        and (output / name).read_bytes() != candidates[name]
    )
    previous_manifest = _read_json_if_present(output / "manifest.json")
    previous_outputs = {
        str(source.get("output"))
        for source in (previous_manifest or {}).get("sources", [])
        if isinstance(source, dict) and source.get("output")
    }
    removed = sorted(previous_outputs - document_names)

    previous_index = _read_json_if_present(output / "api-index.json")
    previous_entries = {
        str(entry["key"]): entry
        for entry in (previous_index or {}).get("entries", [])
        if isinstance(entry, dict) and entry.get("key")
    }
    current_entries = {entry["key"]: entry for entry in entries}
    previous_keys = set(previous_entries)
    current_keys = set(current_entries)
    api_changes = {
        "added": sorted(current_keys - previous_keys),
        "modified": sorted(
            key
            for key in current_keys & previous_keys
            if current_entries[key] != previous_entries[key]
        ),
        "removed": sorted(previous_keys - current_keys),
    }
    documents = {"added": added, "modified": modified, "removed": removed}
    return documents, api_changes


def preview(sources_path: Path, output: Path, html_dir: Path | None) -> dict[str, Any]:
    sources = load_sources(sources_path)
    candidates, entries = build_candidates(sources, html_dir)
    documents, api_changes = _preview_changes(sources, output, candidates, entries)
    return {
        "status": "ok",
        "changed": _changed_files(output, candidates),
        "api_entries": len(entries),
        "documents": documents,
        "api_changes": api_changes,
    }


def sync(sources_path: Path, output: Path, html_dir: Path | None) -> dict[str, Any]:
    sources = load_sources(sources_path)
    candidates, entries = build_candidates(sources, html_dir)
    changed = _changed_files(output, candidates)
    if not changed and (output / "manifest.json").is_file():
        return {"status": "ok", "changed": [], "api_entries": len(entries)}

    manifest = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [
            {
                "source_id": source["source_id"],
                "url": source["url"],
                "output": source["output"],
            }
            for source in sources
        ],
        "files": {
            name: {"sha256": _sha256_bytes(data), "bytes": len(data)}
            for name, data in sorted(candidates.items())
        },
        "api_entries": len(entries),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".joinquant-docs-sync-", dir=output.parent
    ) as temp:
        stage = Path(temp)
        for name, data in candidates.items():
            (stage / name).write_bytes(data)
        (stage / "manifest.json").write_text(_json_text(manifest), encoding="utf-8")
        output.mkdir(parents=True, exist_ok=True)
        for name in changed:
            os.replace(stage / name, output / name)
        os.replace(stage / "manifest.json", output / "manifest.json")
    return {"status": "ok", "changed": changed, "api_entries": len(entries)}


def verify(sources_path: Path, output: Path) -> dict[str, Any]:
    sources = load_sources(sources_path)
    manifest_path = output / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"无法读取同步清单: {manifest_path}: {exc}") from exc
    expected_names = {source["output"] for source in sources} | {"api-index.json"}
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != expected_names:
        raise SyncError("同步清单文件集合不匹配")
    for name in sorted(expected_names):
        path = output / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SyncError(f"无法读取同步文件: {path}: {exc}") from exc
        if _sha256_bytes(data) != files[name].get("sha256"):
            raise SyncError(f"文件摘要不匹配: {name}")
        if len(data) != files[name].get("bytes"):
            raise SyncError(f"文件大小不匹配: {name}")

    try:
        index = json.loads((output / "api-index.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"API 索引无效: {exc}") from exc
    actual_entries: list[dict[str, str]] = []
    for source in sources:
        markdown = (output / source["output"]).read_text(encoding="utf-8")
        actual_entries.extend(extract_api_entries(markdown, source["source_id"]))
    actual_entries.sort(key=lambda entry: entry["key"])
    if index.get("entries") != actual_entries:
        raise SyncError("API 索引与文档重算结果不一致")
    keys = [entry["key"] for entry in actual_entries]
    if len(keys) != len(set(keys)):
        raise SyncError("API 索引包含重复稳定键")
    if manifest.get("api_entries") != len(actual_entries):
        raise SyncError("API 条目数量与清单不一致")
    return {"status": "ok", "files": len(expected_names), "api_entries": len(keys)}


def self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="joinquant-docs-self-test-") as temp:
        root = Path(temp)
        html_dir = root / "html"
        html_dir.mkdir()
        common = "离线聚宽帮助正文。" * 20
        fixture = (
            '<html><body><div class="js-app"><h1>股票数据</h1>'
            "<h2>获取概况</h2><p>调用方法</p><pre><code>get_security_info(code)</code></pre>"
            f"<h3>参数</h3><p>code</p><h3>返回</h3><p>对象</p><p>{common}</p>"
            "</div></body></html>"
        )
        (html_dir / "stock.html").write_text(fixture, encoding="utf-8")
        sources_path = root / "sources.json"
        sources_path.write_text(
            _json_text(
                {
                    "version": 1,
                    "sources": [
                        {
                            "source_id": "stock",
                            "url": "https://www.joinquant.com/help/api/help?name=Stock",
                            "output": "Stock.md",
                            "selector": ".js-app",
                            "min_chars": 100,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        output = root / "output"
        preview(sources_path, output, html_dir)
        sync(sources_path, output, html_dir)
        second = sync(sources_path, output, html_dir)
        if second["changed"]:
            raise SyncError("离线重复同步不是幂等的")
        verify(sources_path, output)
        (output / "Stock.md").write_text("tampered", encoding="utf-8")
        try:
            verify(sources_path, output)
        except SyncError:
            pass
        else:
            raise SyncError("离线篡改未被校验发现")
    return {
        "status": "ok",
        "steps": ["preview", "sync", "idempotent", "verify", "tamper-detected"],
    }


def _common_arguments(parser: argparse.ArgumentParser, include_html: bool) -> None:
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    if include_html:
        parser.add_argument("--html-dir", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jq_docs_sync.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preview", "sync"):
        subparser = subparsers.add_parser(command)
        _common_arguments(subparser, include_html=True)
    verify_parser = subparsers.add_parser("verify")
    _common_arguments(verify_parser, include_html=False)
    subparsers.add_parser("self-test")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "preview":
            result = preview(args.sources, args.output, args.html_dir)
        elif args.command == "sync":
            result = sync(args.sources, args.output, args.html_dir)
        elif args.command == "verify":
            result = verify(args.sources, args.output)
        else:
            result = self_test()
    except (SyncError, OSError, ValueError) as exc:
        print(_json_text({"status": "failed", "error": str(exc)}), end="")
        return 2
    print(_json_text(result), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
