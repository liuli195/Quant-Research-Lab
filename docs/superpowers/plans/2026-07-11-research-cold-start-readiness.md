# Research Cold-Start Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent JoinQuant Research cold starts from reaching export code before the Jupyter runtime is ready.

**Architecture:** Keep the existing authenticated `require + utils.ajax` transport. Strengthen the shared `_research_frame` boundary so both backtest and simulation callers wait for the frame load and RequireJS runtime before exporting.

**Tech Stack:** Python 3.12, Playwright, pytest

## Global Constraints

- Use the project `.venv` for Python.
- Do not replace Jupyter AJAX or read cookies.
- Keep the change to the shared readiness boundary and one regression test.
- Do not commit without explicit user authorization.

---

### Task 1: Guard the shared Research readiness boundary

**Files:**
- Modify: `.agents/skills/joinquant-archive-sync/scripts/joinquant_sync/research_cloud.py:110`
- Test: `tests/joinquant_sync/test_browser_research.py`

**Interfaces:**
- Consumes: Playwright `Page.frame(name="research")` and `Frame.wait_for_load_state` / `Frame.wait_for_function`.
- Produces: `_research_frame(page: Page) -> Frame` that returns only after the Jupyter RequireJS runtime is ready.

- [x] **Step 1: Write the failing test**

```python
def test_research_frame_waits_for_runtime_after_workspace_url() -> None:
    from joinquant_sync.research_cloud import _research_frame

    class Frame:
        url = "https://www.joinquant.com/user/1/tree"

        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            self.calls.append(("load", (state, timeout)))

        def wait_for_function(self, expression: str, *, timeout: int) -> None:
            self.calls.append(("runtime", (expression, timeout)))

    class Page:
        url = "https://www.joinquant.com/research"

        def __init__(self) -> None:
            self.research = Frame()

        def goto(self, *_args: object, **_kwargs: object) -> None:
            return None

        def wait_for_selector(self, *_args: object, **_kwargs: object) -> None:
            return None

        def frame(self, *, name: str) -> Frame:
            assert name == "research"
            return self.research

        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    page = Page()
    assert _research_frame(page) is page.research
    assert page.research.calls[0][0] == "load"
    assert page.research.calls[1][0] == "runtime"
    assert "document.readyState === \"complete\"" in page.research.calls[1][1][0]
    assert "typeof require === \"function\"" in page.research.calls[1][1][0]
    assert "typeof requirejs === \"function\"" in page.research.calls[1][1][0]
```

- [x] **Step 2: Run test to verify it fails**

Run: `& .\.venv\Scripts\python.exe -m pytest tests\joinquant_sync\test_browser_research.py::test_research_frame_waits_for_runtime_after_workspace_url -q`

Expected: FAIL because `_research_frame` calls `evaluate` instead of both readiness waits.

- [x] **Step 3: Write minimal implementation**

```python
frame.wait_for_load_state("load", timeout=60_000)
frame.wait_for_function(
    """() => document.readyState === "complete"
    && typeof require === "function"
    && typeof requirejs === "function""",
    timeout=60_000,
)
return frame
```

- [x] **Step 4: Run focused and complete checks**

Run the focused test, `test_browser_research.py`, `jq_sync.py self-test`, fast repository verification, then real sync and `verify` for targets 113, 114, and 115.

- [x] **Step 5: Report results without committing**

List changed files, red/green evidence, validation results, and each target's gate and dataset states.
