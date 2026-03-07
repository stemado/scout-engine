# Hot Takeover: Interactive Browser Session Attachment

**Date:** 2026-02-28
**Status:** Approved design, pending implementation plan

## Problem

Scout-engine runs browser automation workflows unattended — step-by-step with no way to intervene except full cancellation. The Scout plugin runs interactively — Claude in the loop on every action. There is no bridge between these two modes.

When building or debugging a workflow and a step fails, the developer wants the experience they already have when authoring with Scout: Claude inspecting the live DOM, trying selectors, scouting the page, course-correcting in real time. That requires pausing the engine's executor, exposing the browser via CDP, and letting the Scout plugin attach to the live session.

## Architecture Decision: Hot Takeover vs Cold Diagnosis

Two distinct failure recovery patterns were identified. They require different architectures and should be built as separate features.

**Hot Takeover (this feature):** The engine pauses execution and holds the browser open. The Scout plugin connects to the paused browser via CDP. Claude (or the developer) inspects the page interactively, fixes the issue, and signals how to resume. This is the irreducible core for interactive debugging — no substitute exists.

**Cold Diagnosis (separate, future feature):** When a step fails during unsupervised execution, the engine captures rich error telemetry (DOM snapshot, screenshot, exception trace, step definition), sends it to a configurable webhook endpoint, receives a structured remediation (corrected selector, alternative strategy), and retries. No session takeover required. The error payload becomes the contract between engine and intelligence layer. This pattern is superior for automated recovery because it's simpler, more secure, more testable, and doesn't require an external agent to be available and responsive during execution.

The architectural split: Hot Takeover for interactive debugging where exploratory access is essential. Cold Diagnosis for production recovery where failure modes are structurally predictable. This design covers Hot Takeover only.

## Pause Mechanism: Threading Queue

Mirrors the existing cancellation pattern (`threading.Event` in `cancellation.py`) but uses `threading.Queue` to combine the wake signal and instruction payload into a single atomic operation.

### Why Queue over Event + shared variable

`threading.Event` is a pure signal — it says "something happened" but carries no data. `threading.Queue.get()` blocks like `event.wait()`, but when it unblocks, the instruction is already in hand. This eliminates the race condition where the event fires before the instruction variable is set.

### New module: `app/services/pause.py`

```python
_registry: dict[str, queue.Queue[ResumeInstruction]] = {}

@dataclass
class ResumeInstruction:
    action: str          # "retry" | "continue" | "abort" | "jump"
    step_index: int | None = None  # required only for "jump"
```

### Lifecycle

1. Executor registers a `Queue` for its `execution_id` at start
2. When a pause trigger fires, the executor thread calls `queue.get(timeout=1800)` — blocks until instruction arrives or 30-minute timeout
3. The resume API puts a `ResumeInstruction` into the queue — thread wakes with the payload
4. Executor reads the instruction and acts accordingly
5. Queue is unregistered in the finally block

### Pause triggers

Two paths into the paused state:

- **Explicit handoff:** A workflow step with `action: "handoff"`. Engine pauses, sets browser session state to `paused_handoff`.
- **Pause on error:** When `pause_on_error: true` in workflow settings and a step fails. Engine pauses instead of applying `on_error` policy, sets state to `paused_error`.
- **External request:** `POST /api/executions/{id}/pause` sets a `pause_requested` flag checked between steps. Engine pauses after the current step finishes.

### Resume dispatch

| Instruction | Behavior |
|---|---|
| `retry` | Re-execute the paused step with current page state |
| `continue` | Mark paused step as resolved, advance to next step |
| `abort` | Terminate the execution entirely |
| `jump <N>` | Skip to step N (0-indexed). Covers cases where intermediate steps are now irrelevant |

### Retry-then-fail state machine

When `pause_on_error` is enabled and a step fails:

```
Step fails → pause → resume with retry → step fails again → on_error policy applies (no second pause)
```

The "no infinite pause loop" guarantee is enforced by an `already_paused: bool` flag on the step execution context. The pause logic checks this flag before pausing — a step that has already been paused and retried will not pause a second time.

### Timeout

If no resume instruction arrives within 30 minutes (configurable later), the engine auto-aborts. Prevents orphaned browsers consuming resources.

## API Surface

### `POST /api/executions/{id}/pause`

Manually request a pause. Takes effect after the current step finishes.

```json
{"status": "pause_requested"}
```

### `POST /api/executions/{id}/resume`

Send a resume instruction to a paused execution. Returns 409 if not paused.

```json
// Request
{"action": "retry"}
{"action": "continue"}
{"action": "abort"}
{"action": "jump", "step_index": 6}

// Response
{"status": "resumed", "action": "retry"}
```

### `GET /api/executions/{id}/browser` (enhanced)

The existing endpoint, enhanced with pause state:

```json
{
  "execution_id": "abc-123",
  "cdp_host": "127.0.0.1",
  "cdp_port": 51234,
  "cdp_websocket_url": "ws://127.0.0.1:51234/devtools/browser/guid",
  "targets_url": "http://127.0.0.1:51234/json/list",
  "devtools_frontend_url": "http://127.0.0.1:51234",
  "state": "paused_error",
  "paused_at_step": {
    "step_order": 3,
    "step_name": "Click submit",
    "action": "click",
    "reason": "Step failed",
    "error": "Element not found: #submit-btn"
  },
  "current_step": null
}
```

The `paused_at_step` object uses two distinct fields:

- `reason`: Human-readable context. For handoffs, carries the `value` from the handoff step. For errors, auto-generated (e.g., "Step failed").
- `error`: Exception detail. Present only for error pauses, null for handoffs.

This separation matters because a step that paused because the workflow asked it to is fundamentally different from a step that paused because something broke. Logs, monitoring, and future Cold Diagnosis will consume these fields differently.

The `state` field is the single source of truth for browser session status: `running`, `paused_handoff`, `paused_error`, `paused_requested`.

## Workflow Schema Changes

### New action type: `handoff`

```json
{
  "order": 5,
  "name": "Manual data entry",
  "action": "handoff",
  "value": "Complex form requires human/agent judgment"
}
```

Minimal change: add `"handoff"` to the action Literal in `app/schemas.py`.

### New workflow setting: `pause_on_error`

```json
{
  "settings": {
    "pause_on_error": false,
    "on_error": "stop"
  }
}
```

- `pause_on_error: true` — step failure triggers pause instead of `on_error` policy
- `pause_on_error: false` (default) — existing behavior unchanged
- When `pause_on_error` is true, `on_error` is ignored for the failing step (pause supersedes). On retry failure, `on_error` applies (no second pause).

## Plugin Attachment: The Session Seam

### The problem

Scout plugin tools (`scout_page_tool`, `find_elements`, `execute_action_tool`, etc.) operate through a `BrowserSession` that owns a `Driver` it launched. When attaching to the engine's paused browser, we need those same tools to target a browser the plugin didn't create and must not destroy.

### The enabling condition

While the engine is paused, its executor thread is blocked on `queue.get()`. The engine's Driver is completely idle. CDP supports multiple concurrent clients. The plugin can connect its own CDP client to the same Chrome instance without conflict.

### Solution: Subclass Browser and Driver

Botasaurus-driver has a clean architectural seam. `Browser.start()` calls `create_chrome_with_retries()` (launches Chrome) then creates a `Connection` from the websocket URL obtained by polling `/json/version`. The websocket URL acquisition works identically whether we launched Chrome or not.

```python
class AttachedBrowser(Browser):
    """Connects to an existing Chrome — does not launch or own the process."""

    def create_chrome_with_retries(self, exe, params):
        """Skip Chrome launch. Poll the already-running instance."""
        chrome_url = f"http://{self.config.host}:{self.config.port}/json/version"
        self.info = ensure_chrome_is_alive(chrome_url)
        self._process = None  # we don't own the process

    def close(self):
        """Close CDP connections only. Do NOT kill Chrome."""
        self.close_tab_connections()
        self.close_browser_connection()
        # No close_chrome(), no terminate_process(), no profile deletion
```

Two method overrides. The rest of `Browser.start()` — Connection creation, target discovery, event handlers — runs unchanged.

For the Driver, construct a real `Config` with sensible defaults and override `host`/`port`, rather than bypassing `__init__` with `__new__`. This avoids attribute errors from missing defaults that downstream code assumes are present.

### Guard tests

Write integration tests that validate the botasaurus-driver internal assumptions:

- `Browser` has a callable `create_chrome_with_retries` method
- `Browser.close` calls `close_tab_connections` and `close_browser_connection` as discrete operations
- `Browser.start()` calls `create_chrome_with_retries` before connection setup

Run against each botasaurus-driver update. Cheaper than vendoring, provides early warning if an upstream release breaks the contract.

### Ownership rules

| Concern | Engine owns | Plugin borrows |
|---|---|---|
| Chrome process | Always | Never touches it |
| CDP port | Allocated at launch | Connects as second client |
| Browser close | Engine's `finally` block | Plugin only drops connection |
| Page state | Resumes from whatever state plugin left | May navigate, click, change DOM |
| Element cache | N/A | Builds fresh cache on attach (scout) |

## Scout Plugin Commands

### `/attach [execution_id]`

If no argument: query for paused executions, auto-attach if exactly one, present a list if multiple. This is the expected primary invocation — developers typically have one workflow running and know it just failed.

If execution_id provided: attach to that specific execution.

**Flow:**

1. `GET /api/executions/{id}/browser`
2. Route on state:
   - `paused_*` — proceed to step 3
   - `running` — offer to pause, and if confirmed: fire `POST /pause`, poll `/browser` until state transitions to `paused_*`, then proceed
   - `404` — "No active browser session"
3. Construct `AttachedDriver(host, port)` via `AttachedBrowser`
4. Create `BrowserSession(driver, owns_browser=False)`
5. Auto-scout the page — show DOM structure to Claude
6. Display pause context:
   - Handoff: "Paused at step 5 'Manual data entry': Complex form requires agent judgment"
   - Error: "Paused at step 3 'Click submit': Element not found: #submit-btn"
   - Requested: "Paused after step 4 'Navigate' (developer-requested pause)"
7. Claude + user work interactively (all existing Scout tools)

### `/resume <action> [step_index]`

```
/resume retry        — re-execute the failed step
/resume continue     — skip to next step
/resume abort        — terminate execution
/resume jump 6       — skip to step 6
```

**Flow:**

1. `POST /api/executions/{id}/resume` with instruction
2. Wait for 200 acknowledgment from engine
3. Then detach: drop CDP connection (`BrowserSession.detach()`)
4. Report: "Resumed execution abc-123 with action: retry"

Ordering is critical: confirm acknowledgment before detaching. If the POST fails, the plugin keeps the session alive and reports the error.

### `/pause [execution_id]`

Standalone convenience command for pausing without attaching:

```
/pause abc-123 → POST /api/executions/abc-123/pause
```

Exists for cases where the user wants to pause without immediately attaching. The common path — `/attach` detecting a running execution and handling pause+poll+attach in one flow — does not require this command.

## Scope

### In scope (Hot Takeover v1)

| Component | Changes |
|---|---|
| `app/services/pause.py` | New module: Queue-based pause registry, `ResumeInstruction` dataclass |
| `app/services/executor.py` | Pause triggers (handoff + pause_on_error + external request), `queue.get()` blocking, resume dispatch, `already_paused` guard |
| `app/services/browser_session.py` | Add `state` field, add `reason` + `error` to paused context |
| `app/schemas.py` | Add `handoff` to action Literal, add `pause_on_error: bool = False` to WorkflowSettings |
| `app/api/executions.py` | `POST /pause`, `POST /resume`, enhance `GET /browser` response |
| Scout plugin: `attached_driver.py` | `AttachedBrowser` + `AttachedDriver` subclasses |
| Scout plugin: `/attach` command | Full flow: discover, pause-if-running, poll, connect, scout, display context |
| Scout plugin: `/resume` command | Send instruction, confirm ack, detach |
| Scout plugin: guard tests | Validate botasaurus-driver internal assumptions |

### Out of scope (separate features, build later)

| Feature | Why deferred |
|---|---|
| Cold Diagnosis (webhook + error telemetry + retry-with-patch) | Different architecture, different triggers. The engine captures context and sends to an external endpoint; intelligence sits outside. Requires its own design for the error payload contract and webhook interface. |
| Pause timeout configuration via API | Default 30-minute timeout is adequate for v1 interactive debugging. Configurable timeout adds API surface with no immediate user need. |
| Multiple concurrent paused executions | v1 supports it mechanically (registry is per execution_id), but the UX isn't optimized for managing multiple paused sessions. Revisit if concurrent debugging becomes a real workflow. |
| WebSocket push notifications for pause events | Polling from `/attach` is simple and adequate for v1. Push is an optimization that matters when Cold Diagnosis introduces automated recovery agents that need instant notification. |
| Workflow editor integration | No UI changes. This is a CLI/API feature for developers using Claude Code. |

### Invariants (must never be violated)

1. **Engine owns Chrome, plugin borrows CDP.** `AttachedBrowser.close()` never kills Chrome. The `owns_browser = False` flag on `BrowserSession` is checked defensively at every code path that could terminate the browser.

2. **No infinite pause loop.** An `already_paused: bool` flag on the step execution context prevents a retried step from pausing a second time on the same failure. State machine: step fails -> pause -> retry -> fails again -> `on_error` applies.

3. **Acknowledge before detach.** The plugin confirms a 200 from `POST /resume` before dropping its CDP connection. If the resume fails, the plugin keeps the session alive and reports the error.

4. **Pause timeout.** Orphaned paused executions auto-abort after 30 minutes. Prevents resource leaks from forgotten debugging sessions.

5. **Backward compatibility.** `pause_on_error` defaults to `false`. `handoff` is a new action type. Existing workflows behave identically.

6. **Credential safety during attachment.** Credentials injected by the engine (via variable resolution into `type` steps) are scrubbed from all plugin output during attached sessions, same as credentials injected by the plugin itself. The plugin's scrubbing mechanism operates on output, not input source — it must apply regardless of who typed the credential into the form field.
