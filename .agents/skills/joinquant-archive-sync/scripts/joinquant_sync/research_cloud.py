from __future__ import annotations

import json
import uuid
from urllib.parse import urlsplit

from playwright.sync_api import Frame, Page

from .browser import ensure_authenticated


class ResearchCloudError(RuntimeError):
    """Raised when the authenticated JoinQuant research kernel cannot export data."""


def build_research_export_script(
    backtest_id: str,
    export_path: str,
    *,
    after_times: dict[str, str] | None = None,
) -> str:
    return f"""
import datetime
import decimal
import json
import math
import os

try:
    import numpy as _np
except Exception:
    _np = None
try:
    import pandas as _pd
except Exception:
    _pd = None

def _jsonable(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if _np is not None and isinstance(value, _np.generic):
        return _jsonable(value.item())
    if _pd is not None and isinstance(value, (_pd.DataFrame, _pd.Series)):
        return [_jsonable(row) for row in value.reset_index().to_dict(orient="records")]
    if isinstance(value, dict):
        return {{str(key): _jsonable(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)

def _safe(name, call):
    try:
        return _jsonable(call())
    except Exception as error:
        return {{"__error__": str(error), "__source__": name}}

def _incremental(name, value):
    cursor = _after.get(name)
    if not cursor or not isinstance(value, list) or not value:
        return value, "full"
    if not all(isinstance(row, dict) and row.get("time") is not None for row in value):
        return value, "full_no_time_contract"
    return [row for row in value if str(row.get("time")) >= cursor], "after_time_overlap"

_id = {json.dumps(backtest_id, ensure_ascii=False)}
_path = {json.dumps(export_path, ensure_ascii=False)}
_after = {json.dumps(after_times or {}, ensure_ascii=False, sort_keys=True)}
gt = get_backtest(_id)
_values = {{
    "params": _safe("get_params", gt.get_params),
    "status": _safe("get_status", gt.get_status),
    "results": _safe("get_results", gt.get_results),
    "positions": _safe("get_positions", gt.get_positions),
    "orders": _safe("get_orders", gt.get_orders),
    "records": _safe("get_records", gt.get_records),
    "risk": _safe("get_risk", gt.get_risk),
    "period_risks": _safe("get_period_risks", gt.get_period_risks),
    "balances": _safe("get_balances", gt.get_balances),
}}
_modes = {{}}
for _name in ("results", "positions", "orders", "records", "balances"):
    _values[_name], _modes[_name] = _incremental(_name, _values[_name])
payload = {{
    "metadata": {{
        "schema_version": 1,
        "backtest_id": _id,
        "generated_at": datetime.datetime.now().isoformat(),
        "extraction_method": "joinquant_research_get_backtest",
        "incremental_after": _after,
        "transfer_modes": _modes,
    }},
    **_values,
}}
directory = os.path.dirname(_path)
if directory:
    os.makedirs(os.path.join(os.path.expanduser("~"), directory), exist_ok=True)
write_file(_path, json.dumps(payload, ensure_ascii=False), append=False)
print("joinquant archive export written: " + _path)
""".strip()


def _research_frame(page: Page) -> Frame:
    page.goto("https://www.joinquant.com/research", wait_until="domcontentloaded")
    ensure_authenticated(page)
    page.wait_for_selector("iframe#research, iframe[name='research']", timeout=60_000)
    last_path = ""
    for _ in range(120):
        frame = page.frame(name="research")
        if frame is not None:
            last_path = urlsplit(frame.url).path
            if last_path.startswith("/user/") and not last_path.startswith(
                "/hub/user/"
            ):
                frame.evaluate("document.readyState")
                return frame
        page.wait_for_timeout(500)
    raise ResearchCloudError(
        f"research workspace did not load: {last_path or 'unknown'}"
    )


_EXECUTE_JS = r"""
async ({ code }) => {
  const match = location.pathname.match(/^\/user\/[^/]+\//);
  if (!match) return {ok: false, error: `unexpected workspace path: ${location.pathname}`};
  const base = match[0];
  const ajax = (url, options = {}) => new Promise((resolve, reject) => {
    require(["base/js/utils"], utils => {
      utils.ajax(url, options)
        .done((data, _status, response) => resolve({data, status: response.status}))
        .fail(response => reject(new Error(`${response.status} ${url}`)));
    }, reject);
  });
  const api = async (path, options = {}) => {
    const response = await ajax(`${base}api/${path}`, {
      type: options.method || "GET",
      data: options.body,
      contentType: options.body ? "application/json" : undefined,
      processData: false,
    });
    return response.status === 204 ? null : response.data;
  };
  const session = (crypto.randomUUID && crypto.randomUUID()) || `archive-${Date.now()}`;
  let kernel;
  try {
    kernel = await api("kernels", {method: "POST", body: JSON.stringify({name: "python3"})});
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${location.host}${base}api/kernels/${kernel.id}/channels?session_id=${encodeURIComponent(session)}`;
    const result = await new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      let reply = null, idle = false, stdout = "", stderr = "";
      const timer = setTimeout(() => reject(new Error("research execution timeout")), 240000);
      const finish = () => {
        if (!reply || !idle) return;
        clearTimeout(timer); ws.close(); resolve({reply, stdout, stderr});
      };
      ws.onerror = () => reject(new Error("research websocket error"));
      ws.onopen = () => ws.send(JSON.stringify({
        header: {msg_id: `archive-${Date.now()}`, username: "archive", session,
          date: new Date().toISOString(), msg_type: "execute_request", version: "5.3"},
        parent_header: {}, metadata: {}, channel: "shell", buffers: [],
        content: {code, silent: false, store_history: false, user_expressions: {},
          allow_stdin: false, stop_on_error: true},
      }));
      ws.onmessage = event => {
        let message; try { message = JSON.parse(event.data); } catch (_) { return; }
        const type = message.header && message.header.msg_type;
        const content = message.content || {};
        if (type === "stream") {
          if (content.name === "stderr") stderr += content.text || "";
          else stdout += content.text || "";
        }
        if (type === "error") stderr += `${content.ename || "Error"}: ${content.evalue || ""}`;
        if (message.channel === "shell" && type === "execute_reply") reply = content;
        if (type === "status" && content.execution_state === "idle") idle = true;
        finish();
      };
    });
    if (!result.reply || result.reply.status !== "ok") {
      return {ok: false, error: result.stderr || "research execute failed", stdout: result.stdout};
    }
    return {ok: true, stdout: result.stdout, stderr: result.stderr};
  } catch (error) {
    return {ok: false, error: error && error.message ? error.message : String(error)};
  } finally {
    if (kernel && kernel.id) { try { await api(`kernels/${kernel.id}`, {method: "DELETE"}); } catch (_) {} }
  }
}
"""


_FILE_JS = r"""
async ({ path, remove }) => {
  const match = location.pathname.match(/^\/user\/[^/]+\//);
  if (!match) return {ok: false, error: `unexpected workspace path: ${location.pathname}`};
  const base = match[0];
  const encoded = String(path).split("/").map(encodeURIComponent).join("/");
  const ajax = (url, options = {}) => new Promise((resolve, reject) => {
    require(["base/js/utils"], utils => {
      utils.ajax(url, options).done(resolve).fail(response => {
        reject(new Error(`${response.status} ${url}`));
      });
    }, reject);
  });
  let data;
  try {
    data = await ajax(`${base}api/contents/${encoded}?content=1&type=file`);
  } catch (error) {
    return {ok: false, error: error && error.message ? error.message : String(error)};
  }
  if (data.format !== "text" || typeof data.content !== "string") {
    return {ok: false, error: `unexpected file format: ${data.format}`};
  }
  if (remove) {
    await ajax(`${base}api/contents/${encoded}`, {type: "DELETE"});
  }
  return {ok: true, content: data.content};
}
"""


_FILES_JS = r"""
async ({ paths }) => {
  const match = location.pathname.match(/^\/user\/[^/]+\//);
  if (!match) return {ok: false, error: `unexpected workspace path: ${location.pathname}`};
  const base = match[0];
  let utils;
  try {
    utils = await new Promise((resolve, reject) => {
      require(["base/js/utils"], resolve, reject);
    });
  } catch (error) {
    return {ok: false, error: error && error.message ? error.message : String(error)};
  }
  const ajax = (url) => new Promise((resolve, reject) => {
    utils.ajax(url).done(resolve).fail(response => {
      reject(new Error(`${response.status} ${url}`));
    });
  });
  const contents = {};
  try {
    for (const path of paths) {
      const encoded = String(path).split("/").map(encodeURIComponent).join("/");
      const data = await ajax(`${base}api/contents/${encoded}?content=1&type=file`);
      if (data.format !== "text" || typeof data.content !== "string") {
        return {ok: false, error: `unexpected file format: ${data.format}`};
      }
      contents[path] = data.content;
    }
  } catch (error) {
    return {ok: false, error: error && error.message ? error.message : String(error)};
  }
  return {ok: true, contents};
}
"""


def _read_research_file(frame: Frame, path: str, *, remove: bool) -> str:
    result = frame.evaluate(_FILE_JS, {"path": path, "remove": remove})
    if not result.get("ok"):
        raise ResearchCloudError(
            str(result.get("error") or "research file read failed")
        )
    return str(result["content"])


def _read_research_files(frame: Frame, paths: list[str]) -> dict[str, str]:
    result = frame.evaluate(_FILES_JS, {"paths": paths})
    contents = result.get("contents") if result.get("ok") else None
    if not isinstance(contents, dict) or set(contents) != set(paths):
        raise ResearchCloudError(
            str(result.get("error") or "research files read failed")
        )
    if not all(isinstance(path, str) and isinstance(value, str) for path, value in contents.items()):
        raise ResearchCloudError("research files response is invalid")
    return {str(path): str(value) for path, value in contents.items()}


def fetch_research_backtest(
    page: Page,
    backtest_id: str,
    *,
    attribution_path: str = "",
    attribution_paths: list[str] | None = None,
    after_times: dict[str, str] | None = None,
) -> dict[str, object]:
    export_path = f"jq_auto_exports/archive_sync_{uuid.uuid4().hex}.json"
    frame = _research_frame(page)
    execution = frame.evaluate(
        _EXECUTE_JS,
        {
            "code": build_research_export_script(
                backtest_id, export_path, after_times=after_times
            )
        },
    )
    if not execution.get("ok"):
        raise ResearchCloudError(
            str(execution.get("error") or "research execution failed")
        )
    raw_text = _read_research_file(frame, export_path, remove=True)
    try:
        bundle = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ResearchCloudError("research export is invalid JSON") from error
    if not isinstance(bundle, dict):
        raise ResearchCloudError("research export root is not an object")
    if attribution_path and attribution_paths:
        raise ResearchCloudError("attribution path arguments are mutually exclusive")
    requested_paths = list(dict.fromkeys(attribution_paths or []))
    attributions = {
        path: content.encode("utf-8")
        for path, content in (
            _read_research_files(frame, requested_paths) if requested_paths else {}
        ).items()
    }
    attribution = (
        _read_research_file(frame, attribution_path, remove=False).encode("utf-8")
        if attribution_path
        else b""
    )
    return {
        "bundle": bundle,
        "raw": raw_text.encode("utf-8"),
        "attribution": attribution,
        "attributions": attributions,
        "execution": execution,
    }
