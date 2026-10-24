from __future__ import annotations

import json
import io
import subprocess
import sys
from pathlib import Path

import pytest

from jq_docs_sync import SyncError, extract_api_entries, html_to_markdown


SCRIPT = (
    Path(__file__).parents[2]
    / ".agents"
    / "skills"
    / "joinquant-docs-sync"
    / "scripts"
    / "jq_docs_sync.py"
)


def _run(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(arg) for arg in args)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_cli_forces_utf8_output_under_legacy_windows_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jq_docs_sync

    data = io.BytesIO()
    stream = io.TextIOWrapper(data, encoding="cp1252")

    def fail_preview(*_args):
        raise SyncError("缺少完整性标记：末页接口")

    monkeypatch.setattr(jq_docs_sync.sys, "stdout", stream)
    monkeypatch.setattr(jq_docs_sync, "preview", fail_preview)
    assert jq_docs_sync.main(["preview", "--sources", "unused.json", "--output", "unused-output"]) == 2
    stream.flush()
    error = json.loads(data.getvalue().decode("utf-8"))
    assert error["status"] == "failed"
    assert "缺少完整性标记" in error["error"]


def _markdown(fragment: str) -> str:
    html = "<div class='js-app'>" + fragment + "<p>" + "官方完整说明。" * 30 + "</p></div>"
    return html_to_markdown(html, ".js-app", 100, [])


def test_sync_normalizes_inline_markdown_fences() -> None:
    markdown = _markdown("<p>说明 ```python print('ok') ``` 完成。</p>")
    fence_lines = [line for line in markdown.splitlines() if line.startswith("```")]
    assert len(fence_lines) % 2 == 0
    assert all(line.startswith("```") for line in markdown.splitlines() if "```" in line)


def test_sync_preserves_table_links_and_absolutizes_site_links() -> None:
    markdown = _markdown(
        "<table><tr><th>字段</th><th>含义</th></tr>"
        "<tr><td>code</td><td><a href='https://example.com/spec'>证券代码</a></td></tr></table>"
        "<p><a href='/help/api/help?name=faq'>常见问题</a></p><p>[社区](/community)</p>"
    )
    assert "[证券代码](https://example.com/spec)" in markdown
    assert "[常见问题](https://www.joinquant.com/help/api/help?name=faq)" in markdown
    assert "[社区](https://www.joinquant.com/community)" in markdown


def test_sync_preserves_tables_nested_in_list_items() -> None:
    markdown = _markdown(
        "<ul><li><strong>表结构：</strong>"
        "<table><tr><th>字段</th><th>含义</th></tr>"
        "<tr><td>code</td><td>证券代码</td></tr></table></li></ul>"
    )
    assert "- **表结构：**\n\n| 字段 | 含义 |" in markdown
    assert "| code | 证券代码 |" in markdown


def test_sync_preserves_syntax_highlighted_code_lines() -> None:
    markdown = _markdown(
        "<pre><code><span>get_security_info</span><span>(</span>"
        "<span>code</span><span>)</span></code></pre>"
    )
    assert "get_security_info(code)" in markdown


def test_index_classifies_specialized_factors_and_excludes_builtins() -> None:
    details = "<h3>参数</h3><p>官方参数说明。</p><h3>返回</h3><p>官方返回说明。</p>"
    technical = _markdown(
        "<h2>DMA-平均线差</h2><pre><code>DMA(security_list, check_date)\nlen(values)</code></pre>"
        + details
    )
    jqdata = _markdown(
        "<h2>聚宽因子库</h2><pre><code>get_all_factors()\nDataFrame(values)\ncode(value)\n"
        "append(value)\nSTD(value)\nchar(value)\nstock(value)</code></pre>"
        + details
    )
    entries = [*extract_api_entries(technical, "technical-analysis"), *extract_api_entries(jqdata, "jqdata")]
    keys = {entry["key"] for entry in entries}
    names = {entry["name"] for entry in entries}
    assert "technical-analysis:factor:DMA" in keys
    assert "jqdata:function:get_all_factors" in keys
    assert not names.intersection({"DataFrame", "STD", "append", "char", "code", "len", "stock"})


def test_index_uses_factor_catalog_headings() -> None:
    technical = _markdown(
        "<h3>ACCER-幅度涨速</h3><h3>BOLL-布林线</h3><h3>LWR-LWR威廉指标</h3><h3>SG-SMX-生命线</h3>"
    )
    alpha = _markdown(
        "<h3>公用函数说明</h3><p>scale(x, a) 用于缩放；a(value) 只是变量。</p><h4>alpha_101（未实现）</h4>"
    )
    keys = {entry["key"] for markdown, source in ((technical, "technical-analysis"), (alpha, "alpha101"))
            for entry in extract_api_entries(markdown, source)}
    assert {"technical-analysis:factor:ACCER", "technical-analysis:factor:Bollinger_Bands",
            "technical-analysis:factor:LWR", "technical-analysis:factor:SG_SMX",
            "alpha101:factor:alpha_101", "alpha101:function:scale"}.issubset(keys)
    assert "alpha101:function:a" not in keys


def test_index_uses_factor_tables_and_macro_table_labels() -> None:
    factors = _markdown(
        "<h3>风格因子</h3><table><tr><th>因子 code</th><th>因子名称</th></tr>"
        "<tr><td>beta</td><td>官方因子说明</td></tr></table>"
    )
    macro = _markdown("<h3>人口信息</h3><p>表名：MAC_TEST_TABLE</p>")
    keys = {entry["key"] for markdown, source in ((factors, "factor-values"), (macro, "macro-data"))
            for entry in extract_api_entries(markdown, source)}
    assert "factor-values:factor:beta" in keys
    assert "macro-data:table:macro.MAC_TEST_TABLE" in keys


def test_required_marker_rejects_partial_document() -> None:
    html = "<div class='js-app'>" + "官方完整说明。" * 30 + "</div>"
    with pytest.raises(SyncError, match="缺少完整性标记"):
        html_to_markdown(html, ".js-app", 100, ["末页接口"])


@pytest.mark.real
def test_public_self_test_runs_complete_offline_flow() -> None:
    result = _run("self-test")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "ok",
        "steps": ["preview", "sync", "idempotent", "verify", "tamper-detected"],
    }
