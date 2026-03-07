# Human Mode & Schema Alignment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make scout-engine's executor indistinguishable from the hand-crafted Python workflow scripts by baking human-like behavior into its core, and extend the workflow schema to express the real-world patterns that the Python scripts already handle.

**Architecture:** Two phases. Phase 1 adds human mode as a first-class engine feature — every workflow runs with Bezier-curve mouse movement, per-keystroke typing jitter, and randomized inter-step delays by default. Phase 2 extends the schema with `run_js` actions, step output capture, and cleanup steps so the JSON can express what the Python file does.

**Tech Stack:** botasaurus-driver, botasaurus-humancursor (new dependency), FastAPI, Pydantic, SQLAlchemy, pytest + MagicMock

---

## Context

The working Python workflow (`D:\Projects\caladrius-onboarding\workflows\ccm\bc-census-export\benefitsconnect-employee-export.py`) uses three anti-detection layers that the engine completely lacks:

1. **`driver.enable_human_mode()`** — Bezier-curve mouse movement for clicks (via `botasaurus-humancursor`)
2. **`human_type()`** — per-keystroke 30-120ms random delays (custom helper, not a botasaurus built-in)
3. **Randomized inter-step delays** — `human_pause()` (0.8-2s), `human_pause_long()` (2-4s), plus `driver.short_random_sleep()` (2-4s) and `driver.long_random_sleep()` (6-9s) at key transition points

The engine currently uses a fixed `time.sleep(step_delay_ms / 1000)` between steps and bare `target.type()` at machine speed. This is a detection fingerprint.

Additionally, the Python workflow uses features the JSON schema v1.0 cannot express: JavaScript execution (6 uses), conditional logic, step output capture, post-processing, and cleanup steps. Phase 2 addresses the most critical of these.

### Reference files

- **Scout export command** (defines the anti-detection contract): `D:\Projects\scout\commands\export-workflow.md`
- **Working Python workflow**: `D:\Projects\caladrius-onboarding\workflows\ccm\bc-census-export\benefitsconnect-employee-export.py`
- **Engine executor**: `D:\Projects\scout-engine\app\services\executor.py`
- **Engine schema**: `D:\Projects\scout-engine\app\schemas.py`
- **botasaurus-driver human mode source**: `.venv\Lib\site-packages\botasaurus_driver\driver.py:2127-2137`

---

## Phase 1: Human Mode as Core Engine Behavior

### Task 1: Add `botasaurus-humancursor` dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add the dependency**

In `pyproject.toml`, add `botasaurus-humancursor` to the main dependencies list (it's a runtime dependency, not dev-only):

```toml
dependencies = [
    # ... existing deps ...
    "botasaurus-humancursor>=4.0.0",
]
```

**Step 2: Install**

Run: `uv sync`
Expected: Installs `botasaurus-humancursor`, `numpy`, `pytweening`

**Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add botasaurus-humancursor for human-mode mouse movement"
```

---

### Task 2: Add `human_mode` to WorkflowSettings schema

**Files:**
- Modify: `D:\Projects\scout-engine\app\schemas.py`
- Test: `D:\Projects\scout-engine\tests\test_schemas.py`

**Step 1: Write the failing test**

Add to `tests/test_schemas.py`:

```python
def test_workflow_settings_human_mode_defaults_true():
    """human_mode should default to True -- anti-detection by default."""
    settings = WorkflowSettings()
    assert settings.human_mode is True


def test_workflow_settings_human_mode_explicit_false():
    """Allow opting out of human mode for speed in trusted environments."""
    settings = WorkflowSettings(human_mode=False)
    assert settings.human_mode is False


def test_workflow_with_human_mode_in_settings():
    """Full workflow with human_mode in settings round-trips correctly."""
    wf_json = {
        "schema_version": "1.0",
        "name": "test",
        "steps": [
            {"order": 1, "name": "nav", "action": "navigate", "value": "https://example.com"}
        ],
        "settings": {"human_mode": False, "step_delay_ms": 200},
    }
    wf = Workflow.model_validate(wf_json)
    assert wf.settings.human_mode is False
    assert wf.settings.step_delay_ms == 200
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schemas.py -k "human_mode" -v`
Expected: FAIL — `human_mode` field doesn't exist yet

**Step 3: Add `human_mode` field to WorkflowSettings**

In `app/schemas.py`, add to `WorkflowSettings`:

```python
class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headless: bool = False
    human_mode: bool = True  # Anti-detection: Bezier mouse + typing jitter + random delays
    default_timeout_ms: int = Field(default=30000, ge=0)
    step_delay_ms: int = Field(default=500, ge=0)
    on_error: Literal["stop", "continue", "retry"] = "stop"
```

Key design decision: **`human_mode` defaults to `True`**. The whole point of this engine is to run workflows that don't get detected. Opting OUT should be the explicit choice.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_schemas.py -k "human_mode" -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/schemas.py tests/test_schemas.py
git commit -m "feat: add human_mode to WorkflowSettings (default: True)"
```

---

### Task 3: Implement human typing in the executor

This is the `human_type()` pattern from the export-workflow command. Since `enable_human_mode()` does NOT affect typing (it only changes mouse clicks), we need our own per-keystroke jitter.

**Files:**
- Modify: `D:\Projects\scout-engine\app\services\executor.py`
- Test: `D:\Projects\scout-engine\tests\test_executor.py`

**Step 1: Write the failing test**

Add to `tests/test_executor.py`:

```python
@pytest.fixture
def human_mode_workflow():
    """Workflow with human_mode enabled and a type step."""
    return Workflow.model_validate({
        "schema_version": "1.0",
        "name": "human-type-test",
        "settings": {"human_mode": True, "step_delay_ms": 0},
        "steps": [
            {"order": 1, "name": "Type username", "action": "type",
             "selector": "input#user", "value": "admin"},
        ],
    })


async def test_human_mode_enables_human_mode_on_driver(human_mode_workflow):
    """When human_mode is True, executor must call driver.enable_human_mode()."""
    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        await execute_workflow(human_mode_workflow)
    mock_driver.enable_human_mode.assert_called_once()


async def test_human_mode_types_character_by_character(human_mode_workflow):
    """When human_mode is True, type action should type one char at a time."""
    mock_driver = MagicMock()
    mock_elem = MagicMock()
    mock_driver.wait_for_element.return_value = mock_elem

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        with patch("app.services.executor.time") as mock_time:
            mock_time.perf_counter.side_effect = [0, 0, 1]  # start, step start, end
            await execute_workflow(human_mode_workflow)

    # Should have typed each character individually: 'a', 'd', 'm', 'i', 'n'
    type_calls = mock_elem.type.call_args_list
    assert len(type_calls) == 5
    typed_chars = [call.args[0] for call in type_calls]
    assert typed_chars == ["a", "d", "m", "i", "n"]


async def test_human_mode_false_types_normally():
    """When human_mode is False, type action should use standard driver.type()."""
    wf = Workflow.model_validate({
        "schema_version": "1.0",
        "name": "no-human-test",
        "settings": {"human_mode": False, "step_delay_ms": 0},
        "steps": [
            {"order": 1, "name": "Type", "action": "type",
             "selector": "input#user", "value": "admin"},
        ],
    })
    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        await execute_workflow(wf)

    # Should NOT have called enable_human_mode
    mock_driver.enable_human_mode.assert_not_called()
    # Should have typed the full string at once
    mock_driver.type.assert_called_once_with("input#user", "admin")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_executor.py -k "human_mode" -v`
Expected: FAIL

**Step 3: Implement human typing**

In `app/services/executor.py`, add a `_human_type` helper and modify the `type` case:

```python
import random

def _human_type(target, selector: str, text: str) -> None:
    """Type text with per-keystroke jitter to mimic human typing.

    Matches the human_type() pattern from Scout's export-workflow command:
    30-120ms random delay between each keystroke.
    """
    elem = target.wait_for_element(selector, wait=10)
    elem.click()
    for char in text:
        elem.type(char)
        time.sleep(random.uniform(0.03, 0.12))
```

Modify `_execute_step_sync` to accept `human_mode: bool` parameter, and change the `type` case:

```python
case "type":
    if human_mode:
        _human_type(target, step.selector, step.value)
    else:
        if step.clear_first:
            target.clear(step.selector)
        target.type(step.selector, step.value)
```

Note: `_human_type` clicks the element first (like the export template) which implicitly clears focus, but we should also handle `clear_first`:

```python
case "type":
    if human_mode:
        if step.clear_first:
            target.clear(step.selector)
        _human_type(target, step.selector, step.value)
    else:
        if step.clear_first:
            target.clear(step.selector)
        target.type(step.selector, step.value)
```

**Step 4: Wire `enable_human_mode()` in `_run_sync`**

In the `_run_sync` function, after creating the driver:

```python
def _run_sync():
    nonlocal passed, failed, cancelled

    driver = _create_driver(effective_headless)
    human_mode = workflow.settings.human_mode

    if human_mode:
        driver.enable_human_mode()

    # ... rest of _run_sync ...
```

Pass `human_mode` to `_execute_step_sync`:

```python
result = _execute_step_sync(driver, step, default_timeout, monitor=monitor, human_mode=human_mode)
```

Update `_execute_step_sync` signature:

```python
def _execute_step_sync(
    driver, step: WorkflowStep, default_timeout: int,
    monitor: NetworkMonitor | None = None,
    human_mode: bool = False,
) -> StepResult:
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_executor.py -k "human_mode" -v`
Expected: PASS

**Step 6: Commit**

```bash
git add app/services/executor.py tests/test_executor.py
git commit -m "feat: implement human typing with per-keystroke jitter"
```

---

### Task 4: Replace fixed step delays with randomized human-like timing

The export-workflow command defines specific delay patterns:
- After navigation: 2-4s (`short_random_sleep`)
- After login submission: 6-9s (`long_random_sleep`)
- Between same-page interactions: 0.3-0.8s
- After non-login form submission: 2-4s

For the engine, we simplify this to: when `human_mode` is on, randomize the step delay around `step_delay_ms` instead of using it as a fixed value.

**Files:**
- Modify: `D:\Projects\scout-engine\app\services\executor.py`
- Test: `D:\Projects\scout-engine\tests\test_executor.py`

**Step 1: Write the failing test**

```python
async def test_human_mode_randomizes_step_delays():
    """When human_mode is True, step delays should be randomized, not fixed."""
    wf = Workflow.model_validate({
        "schema_version": "1.0",
        "name": "delay-test",
        "settings": {"human_mode": True, "step_delay_ms": 500},
        "steps": [
            {"order": 1, "name": "Nav", "action": "navigate", "value": "https://a.com"},
            {"order": 2, "name": "Click", "action": "click", "selector": "#btn"},
        ],
    })
    mock_driver = MagicMock()
    sleep_durations = []

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        with patch("app.services.executor.time") as mock_time:
            mock_time.perf_counter.side_effect = [0, 0, 0.1, 0, 0.1, 1]
            original_sleep = time.sleep
            def capture_sleep(s):
                sleep_durations.append(s)
            mock_time.sleep = capture_sleep
            await execute_workflow(wf)

    # There should be exactly 1 inter-step delay (between step 1 and 2)
    assert len(sleep_durations) == 1
    delay = sleep_durations[0]
    # Should NOT be exactly 0.5 (the fixed step_delay_ms / 1000)
    # Should be in a randomized range around the base delay
    assert 0.2 <= delay <= 2.0  # generous bounds for randomization
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_executor.py::test_human_mode_randomizes_step_delays -v`
Expected: FAIL

**Step 3: Implement randomized delays**

In `executor.py`, replace the fixed sleep with a context-aware randomized delay when human mode is on:

```python
def _human_delay(step: WorkflowStep, base_delay_ms: int) -> None:
    """Apply a randomized inter-step delay based on the action type.

    Mirrors the anti-detection timing from Scout's export-workflow:
    - After navigate: 2-4s (page loads vary; fixed waits are a bot signal)
    - After click (form submit): 0.8-2.0s (general interaction pacing)
    - Between same-page interactions: 0.3-0.8s
    """
    match step.action:
        case "navigate":
            time.sleep(random.uniform(2.0, 4.0))
        case "click":
            time.sleep(random.uniform(0.8, 2.0))
        case "type":
            # human_type already includes per-keystroke delays, keep gap short
            time.sleep(random.uniform(0.2, 0.5))
        case "select":
            time.sleep(random.uniform(0.3, 0.8))
        case _:
            # Fallback: randomize around the base delay
            base_s = base_delay_ms / 1000
            time.sleep(random.uniform(base_s * 0.5, base_s * 1.5))
```

Then in `_run_sync`, replace the fixed delay block:

```python
# Apply step delay between steps (not after the last one)
if i < len(workflow.steps) - 1:
    if human_mode:
        _human_delay(step, step_delay)
    elif step_delay > 0:
        time.sleep(step_delay / 1000)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_executor.py::test_human_mode_randomizes_step_delays -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/executor.py tests/test_executor.py
git commit -m "feat: randomize inter-step delays in human mode (action-aware timing)"
```

---

### Task 5: Run full test suite and verify nothing is broken

**Step 1: Run all tests**

Run: `uv run pytest -v`
Expected: All existing tests PASS. New human_mode tests PASS.

**Step 2: Verify existing workflows still work with defaults**

Since `human_mode` defaults to `True`, all existing test workflows will now go through the human mode path. The mock driver (`MagicMock`) will accept `enable_human_mode()` calls without complaint, but verify no tests break due to the changed typing behavior (char-by-char vs full string).

If existing tests break, update their assertions to match the new char-by-char typing behavior, OR have them explicitly set `human_mode: False` in their test fixtures for speed.

**Step 3: Commit any test fixes**

```bash
git add tests/
git commit -m "test: update existing tests for human_mode default"
```

---

## Phase 2: Schema Extensions for Real-World Workflow Parity

These tasks close the gap between what the JSON schema can express and what the Python workflow actually does.

### Task 6: Add `run_js` action type

The Python workflow uses `driver.run_js()` in 6 places. This is the most critical schema gap.

**Files:**
- Modify: `D:\Projects\scout-engine\app\schemas.py` (add `run_js` to action Literal)
- Modify: `D:\Projects\scout-engine\app\services\executor.py` (add `run_js` case)
- Test: `D:\Projects\scout-engine\tests\test_executor.py`

**Step 1: Write the failing test**

```python
async def test_run_js_action():
    """run_js action should execute JavaScript via driver.run_js()."""
    wf = Workflow.model_validate({
        "schema_version": "1.0",
        "name": "js-test",
        "settings": {"human_mode": False, "step_delay_ms": 0},
        "steps": [
            {"order": 1, "name": "Run JS", "action": "run_js",
             "value": "return document.title"},
        ],
    })
    mock_driver = MagicMock()
    mock_driver.run_js.return_value = "Test Page"
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)
    assert result.status == "completed"
    mock_driver.run_js.assert_called_once_with("return document.title")
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_executor.py::test_run_js_action -v`
Expected: FAIL — validation rejects `run_js` action

**Step 3: Add `run_js` to schema**

In `app/schemas.py`, update the `WorkflowStep.action` Literal:

```python
action: Literal[
    "navigate", "click", "type", "select", "scroll", "wait",
    "wait_for_download", "wait_for_response",
    "press_key", "hover", "clear",
    "run_js",
]
```

**Step 4: Add `run_js` case to executor**

In `app/services/executor.py`, add to the `match` statement:

```python
case "run_js":
    target = _resolve_target(driver, step.frame_context)
    target.run_js(step.value)
```

**Step 5: Run test to verify pass**

Run: `uv run pytest tests/test_executor.py::test_run_js_action -v`
Expected: PASS

**Step 6: Commit**

```bash
git add app/schemas.py app/services/executor.py tests/test_executor.py
git commit -m "feat: add run_js action type for JavaScript execution"
```

---

### Task 7: Add `cleanup_steps` to the Workflow schema

The Python workflow logs out in a `finally` block. The JSON schema needs a way to express "always run these steps, even on failure."

**Files:**
- Modify: `D:\Projects\scout-engine\app\schemas.py`
- Modify: `D:\Projects\scout-engine\app\services\executor.py`
- Test: `D:\Projects\scout-engine\tests\test_executor.py`

**Step 1: Write the failing test**

```python
async def test_cleanup_steps_run_on_failure():
    """cleanup_steps should execute even when a main step fails."""
    wf = Workflow.model_validate({
        "schema_version": "1.0",
        "name": "cleanup-test",
        "settings": {"human_mode": False, "step_delay_ms": 0},
        "steps": [
            {"order": 1, "name": "Will fail", "action": "click", "selector": "#missing"},
        ],
        "cleanup_steps": [
            {"order": 1, "name": "Logout", "action": "navigate", "value": "https://example.com/logout"},
        ],
    })
    mock_driver = MagicMock()
    mock_driver.click.side_effect = Exception("Element not found")
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "failed"
    # Cleanup step should still have been called
    mock_driver.get.assert_called_once_with("https://example.com/logout")
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_executor.py::test_cleanup_steps_run_on_failure -v`
Expected: FAIL

**Step 3: Add `cleanup_steps` to Workflow model**

In `app/schemas.py`:

```python
class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    name: str
    description: str = ""
    created: str | None = None
    source: WorkflowSource | None = None
    variables: dict[str, WorkflowVariable] = Field(default_factory=dict)
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    steps: list[WorkflowStep]
    cleanup_steps: list[WorkflowStep] = Field(default_factory=list)
```

**Step 4: Execute cleanup steps in the executor's `finally` block**

In `executor.py`, inside `_run_sync`'s `finally` block, before `driver.close()`:

```python
finally:
    monitor.stop()
    # Execute cleanup steps (best-effort, errors don't change final status)
    for cleanup_step in workflow.cleanup_steps:
        try:
            _execute_step_sync(driver, cleanup_step, default_timeout, human_mode=human_mode)
        except Exception:
            pass  # best-effort — don't mask the original error
    try:
        driver.close()
    except Exception:
        pass
```

**Step 5: Run test to verify pass**

Run: `uv run pytest tests/test_executor.py::test_cleanup_steps_run_on_failure -v`
Expected: PASS

**Step 6: Commit**

```bash
git add app/schemas.py app/services/executor.py tests/test_executor.py
git commit -m "feat: add cleanup_steps for always-run teardown (e.g., logout)"
```

---

### Task 8: Generate Alembic migration for schema changes

If `human_mode` or `cleanup_steps` affect the stored JSONB workflow data, no migration is needed — JSON columns are schemaless. But verify that the `WorkflowSettings` and `Workflow` Pydantic model changes don't break existing stored workflows (they shouldn't since both new fields have defaults).

**Step 1: Verify existing workflow JSON validates with new schema**

```python
# Quick manual check — the sample workflow has no human_mode or cleanup_steps
# fields, so it should still validate with the new defaults
wf = Workflow.model_validate(json.loads(Path("docs/plans/sample-workflow.json").read_text()))
assert wf.settings.human_mode is True  # default
assert wf.cleanup_steps == []  # default
```

**Step 2: Run full test suite**

Run: `uv run pytest -v`
Expected: ALL PASS

**Step 3: Commit if any migration needed**

```bash
git commit -m "chore: verify schema backward compatibility (no migration needed)"
```

---

### Task 9: Update the sample workflow JSON to include human mode and cleanup

**Files:**
- Modify: `D:\Projects\scout-engine\docs\plans\sample-workflow.json`

Update the sample workflow to demonstrate the new features:

1. Add `"human_mode": true` to settings (documents the default explicitly)
2. Add a `cleanup_steps` array with a logout step
3. Add a `run_js` step where the Python workflow uses one (e.g., company selection by name)

**Step 1: Update the file**

Add to settings:
```json
"settings": {
    "headless": false,
    "human_mode": true,
    "default_timeout_ms": 30000,
    "step_delay_ms": 500,
    "on_error": "stop"
}
```

Add cleanup:
```json
"cleanup_steps": [
    {
        "order": 1,
        "name": "Logout to release server session",
        "action": "navigate",
        "value": "${BC_URL}/Logout.cfm"
    }
]
```

Replace the hardcoded company click (step 7) with a `run_js` step:
```json
{
    "order": 7,
    "name": "Select target company by name",
    "action": "run_js",
    "value": "return (() => { const links = document.querySelectorAll('#CompanyList a'); for (const link of links) { if (link.textContent.trim() === 'Century Care Management') { link.click(); return true; } } return false; })()"
}
```

**Step 2: Validate the updated JSON**

```python
wf = Workflow.model_validate(json.loads(Path("docs/plans/sample-workflow.json").read_text()))
assert wf.settings.human_mode is True
assert len(wf.cleanup_steps) == 1
```

**Step 3: Commit**

```bash
git add docs/plans/sample-workflow.json
git commit -m "docs: update sample workflow with human_mode, cleanup_steps, run_js"
```

---

## Out of Scope (Future Work)

These gaps exist but are deferred to keep this plan focused:

| Feature | Why deferred |
|---------|-------------|
| **Conditional steps (if/else)** | Requires a mini-DSL in JSON. Better to use `run_js` for now — JS can handle conditionals. |
| **Step output capture (`capture_as`)** | Needs runtime variable interpolation engine. `run_js` can return values but we can't pass them to later steps yet. |
| **Post-processing (CSV conversion)** | Out of band — better handled by a separate pipeline step or webhook payload processor. |
| **Credential vault integration** | Environment variables work for now. Vault is an Scout plugin feature, not an engine concern. |
| **Dynamic date range variables** | Use `run_js` with JS Date APIs, or resolve dates in the variable overrides at API call time. |
