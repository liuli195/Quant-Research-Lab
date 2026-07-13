from __future__ import annotations

import json
import subprocess
import sys
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
