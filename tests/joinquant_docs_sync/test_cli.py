from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


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


def _prepare_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    fixture_dir = tmp_path / "html"
    fixture_dir.mkdir()
    common = "这是聚宽官方帮助正文。" * 20
    (fixture_dir / "stock.html").write_text(
        """
        <html><body><div class="js-app">
        <h1>股票数据</h1>
        <h2>获取股票概况</h2>
        <p>调用方法</p><pre><code>get_security_info(code)</code></pre>
        <h3>参数</h3><p>code：证券代码</p>
        <h3>返回</h3><p>Security 对象</p>
        <h2>股票数据表</h2>
        <p>表名：finance.STOCK_INFO</p>
        <table><tr><th>字段</th><th>含义</th></tr><tr><td>code</td><td>证券代码</td></tr></table>
        <p>__COMMON__</p>
        </div></body></html>
        """.replace("__COMMON__", common),
        encoding="utf-8",
    )
    (fixture_dir / "fund.html").write_text(
        """
        <html><body><div class="js-app">
        <h1>基金数据</h1>
        <h2>获取基金概况</h2>
        <p>调用方法</p><pre><code>get_security_info(code)</code></pre>
        <h3>输入</h3><p>code：基金代码</p>
        <h3>输出</h3><p>Security 对象</p>
        <p>__COMMON__</p>
        </div></body></html>
        """.replace("__COMMON__", common),
        encoding="utf-8",
    )
    sources = tmp_path / "sources.json"
    sources.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "source_id": "stock",
                        "url": "https://www.joinquant.com/help/api/help?name=Stock",
                        "output": "Stock.md",
                        "selector": ".js-app",
                        "min_chars": 100,
                    },
                    {
                        "source_id": "fund",
                        "url": "https://www.joinquant.com/help/api/help?name=fund",
                        "output": "Fund.md",
                        "selector": ".js-app",
                        "min_chars": 100,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return sources, fixture_dir, tmp_path / "output"


def test_preview_reports_changes_without_writing(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)

    result = _run(
        "preview", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["changed"] == ["Fund.md", "Stock.md", "api-index.json"]
    assert payload["documents"] == {
        "added": ["Fund.md", "Stock.md"],
        "modified": [],
        "removed": [],
    }
    assert payload["api_changes"] == {
        "added": [
            "fund:function:get_security_info",
            "stock:function:get_security_info",
            "stock:table:finance.STOCK_INFO",
        ],
        "modified": [],
        "removed": [],
    }
    assert not output.exists()


def test_preview_distinguishes_modified_and_removed_documents(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)
    initial = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )
    assert initial.returncode == 0, initial.stderr
    source_payload = json.loads(sources.read_text(encoding="utf-8"))
    source_payload["sources"] = [source_payload["sources"][0]]
    sources.write_text(json.dumps(source_payload, ensure_ascii=False), encoding="utf-8")
    stock = fixture_dir / "stock.html"
    stock.write_text(
        stock.read_text(encoding="utf-8").replace("股票数据表", "股票基础数据表"),
        encoding="utf-8",
    )

    result = _run(
        "preview", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["documents"] == {
        "added": [],
        "modified": ["Stock.md"],
        "removed": ["Fund.md"],
    }
    assert payload["api_changes"] == {
        "added": [],
        "modified": ["stock:table:finance.STOCK_INFO"],
        "removed": ["fund:function:get_security_info"],
    }


def test_sync_is_idempotent_and_verify_recomputes_hashes(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)

    first = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert first_payload["status"] == "ok"
    assert {
        entry["key"]
        for entry in json.loads(
            (output / "api-index.json").read_text(encoding="utf-8")
        )["entries"]
    } == {
        "fund:function:get_security_info",
        "stock:function:get_security_info",
        "stock:table:finance.STOCK_INFO",
    }
    before = {path.name: path.stat().st_mtime_ns for path in output.iterdir()}

    second = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["changed"] == []
    assert before == {path.name: path.stat().st_mtime_ns for path in output.iterdir()}

    verified = _run("verify", "--sources", sources, "--output", output)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["status"] == "ok"

    (output / "Stock.md").write_text("tampered", encoding="utf-8")
    tampered = _run("verify", "--sources", sources, "--output", output)
    assert tampered.returncode != 0
    assert json.loads(tampered.stdout)["status"] == "failed"


def test_failed_source_preserves_last_complete_version(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)
    initial = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )
    assert initial.returncode == 0, initial.stderr
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    (fixture_dir / "stock.html").write_text(
        '<html><body><div class="js-app">请登录</div></body></html>',
        encoding="utf-8",
    )

    failed = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert failed.returncode != 0
    assert json.loads(failed.stdout)["status"] == "failed"
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


def test_sync_supports_output_on_repository_drive() -> None:
    repo_local = SCRIPT.parents[4] / ".local"
    repo_local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="joinquant-docs-sync-test-", dir=repo_local
    ) as temp:
        sources, fixture_dir, output = _prepare_fixture(Path(temp))

        result = _run(
            "sync",
            "--sources",
            sources,
            "--output",
            output,
            "--html-dir",
            fixture_dir,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert json.loads(result.stdout)["status"] == "ok"
        assert (output / "manifest.json").is_file()


def test_cli_forces_utf8_output_under_legacy_windows_encoding(
    tmp_path: Path,
) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)
    payload = json.loads(sources.read_text(encoding="utf-8"))
    payload["sources"][0]["required_markers"] = ["末页接口"]
    sources.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "preview",
            "--sources",
            str(sources),
            "--output",
            str(output),
            "--html-dir",
            str(fixture_dir),
        ],
        check=False,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 2
    error = json.loads(result.stdout.decode("utf-8"))
    assert error["status"] == "failed"
    assert "缺少完整性标记" in error["error"]


def test_sync_normalizes_inline_markdown_fences(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)
    stock = fixture_dir / "stock.html"
    stock.write_text(
        stock.read_text(encoding="utf-8").replace(
            "这是聚宽官方帮助正文。",
            "说明 ```python print('ok') ``` 完成。",
            1,
        ),
        encoding="utf-8",
    )

    result = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode == 0, result.stderr or result.stdout
    markdown = (output / "Stock.md").read_text(encoding="utf-8")
    fence_lines = [line for line in markdown.splitlines() if line.startswith("```")]
    assert len(fence_lines) % 2 == 0
    assert all(
        line.startswith("```") for line in markdown.splitlines() if "```" in line
    )


def test_sync_preserves_table_links_and_absolutizes_site_links(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)
    stock = fixture_dir / "stock.html"
    stock.write_text(
        stock.read_text(encoding="utf-8").replace(
            "<td>证券代码</td>",
            "<td><a href='https://example.com/spec'>证券代码</a></td>",
            1,
        ).replace(
            "</div></body></html>",
            "<p><a href='/help/api/help?name=faq'>常见问题</a></p>"
            "<p>[社区](/community)</p>"
            "</div></body></html>",
            1,
        ),
        encoding="utf-8",
    )

    result = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode == 0, result.stderr or result.stdout
    markdown = (output / "Stock.md").read_text(encoding="utf-8")
    assert "[证券代码](https://example.com/spec)" in markdown
    assert "[常见问题](https://www.joinquant.com/help/api/help?name=faq)" in markdown
    assert "[社区](https://www.joinquant.com/community)" in markdown


def test_sync_preserves_tables_nested_in_list_items(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)
    stock = fixture_dir / "stock.html"
    stock.write_text(
        stock.read_text(encoding="utf-8").replace(
            "<p>表名：finance.STOCK_INFO</p>",
            "<ul><li><strong>表结构：</strong>"
            "<table><tr><th>字段</th><th>含义</th></tr>"
            "<tr><td>code</td><td>证券代码</td></tr></table></li></ul>",
            1,
        ),
        encoding="utf-8",
    )

    result = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode == 0, result.stderr or result.stdout
    markdown = (output / "Stock.md").read_text(encoding="utf-8")
    assert "- **表结构：**\n\n| 字段 | 含义 |" in markdown
    assert "| code | 证券代码 |" in markdown


def test_sync_preserves_syntax_highlighted_code_lines(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)
    stock = fixture_dir / "stock.html"
    stock.write_text(
        stock.read_text(encoding="utf-8").replace(
            "<pre><code>get_security_info(code)</code></pre>",
            "<pre><code><span>get_security_info</span><span>(</span>"
            "<span>code</span><span>)</span></code></pre>",
            1,
        ),
        encoding="utf-8",
    )

    result = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode == 0, result.stderr or result.stdout
    markdown = (output / "Stock.md").read_text(encoding="utf-8")
    assert "get_security_info(code)" in markdown


def test_index_classifies_specialized_factors_and_excludes_builtins(
    tmp_path: Path,
) -> None:
    fixture_dir = tmp_path / "html"
    fixture_dir.mkdir()
    output = tmp_path / "docs"
    filler = "官方说明。" * 30
    (fixture_dir / "technical-analysis.html").write_text(
        "<div class='js-app'><h2>DMA-平均线差</h2>"
        "<pre><code>DMA(security_list, check_date)\nlen(values)</code></pre>"
        f"<h3>参数</h3><p>{filler}</p><h3>返回</h3><p>{filler}</p></div>",
        encoding="utf-8",
    )
    (fixture_dir / "jqdata.html").write_text(
        "<div class='js-app'><h2>聚宽因子库</h2>"
        "<pre><code>get_all_factors()\nDataFrame(values)\ncode(value)\n"
        "append(value)\nSTD(value)\nchar(value)\nstock(value)</code></pre>"
        f"<h3>参数</h3><p>{filler}</p><h3>返回</h3><p>{filler}</p></div>",
        encoding="utf-8",
    )
    sources = tmp_path / "sources.json"
    sources.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "technical-analysis",
                        "url": "https://www.joinquant.com/help/api/help?name=technicalanalysis",
                        "output": "Technical.md",
                        "selector": ".js-app",
                        "min_chars": 100,
                    },
                    {
                        "source_id": "jqdata",
                        "url": "https://www.joinquant.com/help/api/help?name=JQData",
                        "output": "JQData.md",
                        "selector": ".js-app",
                        "min_chars": 100,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode == 0, result.stderr or result.stdout
    entries = json.loads((output / "api-index.json").read_text(encoding="utf-8"))[
        "entries"
    ]
    keys = {entry["key"] for entry in entries}
    names = {entry["name"] for entry in entries}
    assert "technical-analysis:factor:DMA" in keys
    assert "jqdata:function:get_all_factors" in keys
    assert "DataFrame" not in names
    assert "STD" not in names
    assert "append" not in names
    assert "char" not in names
    assert "code" not in names
    assert "len" not in names
    assert "stock" not in names


def test_index_uses_factor_catalog_headings(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "html"
    fixture_dir.mkdir()
    output = tmp_path / "docs"
    filler = "官方完整说明。" * 30
    (fixture_dir / "technical-analysis.html").write_text(
        "<div class='js-app'><h3>ACCER-幅度涨速</h3>"
        "<h3>BOLL-布林线</h3><h3>LWR-LWR威廉指标</h3>"
        f"<h3>SG-SMX-生命线</h3><p>{filler}</p></div>",
        encoding="utf-8",
    )
    (fixture_dir / "alpha101.html").write_text(
        "<div class='js-app'><h3>公用函数说明</h3>"
        "<p>scale(x, a) 用于缩放；a(value) 只是变量。</p>"
        f"<h4>alpha_101（未实现）</h4><p>{filler}</p></div>",
        encoding="utf-8",
    )
    sources = tmp_path / "sources.json"
    sources.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "technical-analysis",
                        "url": "https://www.joinquant.com/help/api/help?name=technicalanalysis",
                        "output": "Technical.md",
                        "selector": ".js-app",
                        "min_chars": 50,
                    },
                    {
                        "source_id": "alpha101",
                        "url": "https://www.joinquant.com/help/api/help?name=Alpha101",
                        "output": "Alpha101.md",
                        "selector": ".js-app",
                        "min_chars": 50,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode == 0, result.stderr or result.stdout
    entries = json.loads((output / "api-index.json").read_text(encoding="utf-8"))[
        "entries"
    ]
    keys = {entry["key"] for entry in entries}
    assert "technical-analysis:factor:ACCER" in keys
    assert "technical-analysis:factor:Bollinger_Bands" in keys
    assert "technical-analysis:factor:LWR" in keys
    assert "technical-analysis:factor:SG_SMX" in keys
    assert "alpha101:factor:alpha_101" in keys
    assert "alpha101:function:scale" in keys
    assert "alpha101:function:a" not in keys


def test_index_uses_factor_tables_and_macro_table_labels(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "html"
    fixture_dir.mkdir()
    output = tmp_path / "docs"
    filler = "官方完整说明。" * 30
    (fixture_dir / "factor-values.html").write_text(
        "<div class='js-app'><h3>风格因子</h3>"
        "<table><tr><th>因子 code</th><th>因子名称</th></tr>"
        f"<tr><td>beta</td><td>{filler}</td></tr></table></div>",
        encoding="utf-8",
    )
    (fixture_dir / "macro-data.html").write_text(
        f"<div class='js-app'><h3>人口信息</h3><p>{filler}</p>"
        "<p>表名：MAC_TEST_TABLE</p></div>",
        encoding="utf-8",
    )
    sources = tmp_path / "sources.json"
    sources.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "factor-values",
                        "url": "https://www.joinquant.com/help/api/help?name=factor_values",
                        "output": "Factors.md",
                        "selector": ".js-app",
                        "min_chars": 50,
                    },
                    {
                        "source_id": "macro-data",
                        "url": "https://www.joinquant.com/help/api/help?name=macroData",
                        "output": "Macro.md",
                        "selector": ".js-app",
                        "min_chars": 50,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = _run(
        "sync", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode == 0, result.stderr or result.stdout
    entries = json.loads((output / "api-index.json").read_text(encoding="utf-8"))[
        "entries"
    ]
    keys = {entry["key"] for entry in entries}
    assert "factor-values:factor:beta" in keys
    assert "macro-data:table:macro.MAC_TEST_TABLE" in keys


def test_required_marker_rejects_partial_document(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)
    payload = json.loads(sources.read_text(encoding="utf-8"))
    payload["sources"][0]["required_markers"] = ["末页接口"]
    sources.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = _run(
        "preview", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode != 0
    assert "缺少完整性标记" in result.stdout
    assert not output.exists()


def test_duplicate_source_manifest_is_rejected_before_writing(tmp_path: Path) -> None:
    sources, fixture_dir, output = _prepare_fixture(tmp_path)
    payload = json.loads(sources.read_text(encoding="utf-8"))
    payload["sources"].append(dict(payload["sources"][0]))
    sources.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = _run(
        "preview", "--sources", sources, "--output", output, "--html-dir", fixture_dir
    )

    assert result.returncode != 0
    error = json.loads(result.stdout)
    assert error["status"] == "failed"
    assert "来源清单 source_id 冲突" in error["error"]
    assert not output.exists()


def test_public_self_test_runs_complete_offline_flow() -> None:
    result = _run("self-test")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "ok",
        "steps": ["preview", "sync", "idempotent", "verify", "tamper-detected"],
    }
