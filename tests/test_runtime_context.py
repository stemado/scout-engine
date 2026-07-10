"""Tests for Phase 1: RuntimeContext, extract action, and runtime interpolation."""

import pytest
from unittest.mock import MagicMock, patch

from app.schemas import Workflow, WorkflowSettings, WorkflowStep
from app.services.executor import (
    ExecutionResult,
    LoopFrame,
    RuntimeContext,
    StepResult,
    execute_workflow,
)
from app.services.variables import resolve_step_runtime


def _make_workflow(steps, **kwargs) -> Workflow:
    defaults = {
        "name": "test",
        "variables": {},
        "steps": steps,
        "settings": {"human_mode": False, "step_delay_ms": 0},
    }
    defaults.update(kwargs)
    return Workflow(**defaults)


# --- RuntimeContext dataclass tests ---


class TestRuntimeContext:
    def test_defaults(self):
        ctx = RuntimeContext()
        assert ctx.step_outputs == {}
        assert ctx.loop_stack == []
        assert ctx.files == {}

    def test_store_and_retrieve(self):
        ctx = RuntimeContext()
        ctx.step_outputs["title"] = "Hello World"
        assert ctx.step_outputs["title"] == "Hello World"


class TestLoopFrame:
    def test_creation(self):
        frame = LoopFrame(
            items=["a", "b", "c"],
            current_index=0,
            loop_var="item",
            loop_index_var="idx",
            body_start=3,
            body_end=5,
        )
        assert frame.items == ["a", "b", "c"]
        assert frame.loop_var == "item"
        assert frame.loop_index_var == "idx"


# --- StepResult.output_data tests ---


class TestStepResultOutputData:
    def test_output_data_default_none(self):
        r = StepResult(step_order=1, step_name="test", action="click", status="passed")
        assert r.output_data is None

    def test_output_data_set(self):
        r = StepResult(
            step_order=1, step_name="test", action="extract",
            status="passed", output_data={"key": "value"},
        )
        assert r.output_data == {"key": "value"}


# --- ExecutionResult.outputs tests ---


class TestExecutionResultOutputs:
    def test_outputs_default_none(self):
        r = ExecutionResult(status="completed")
        assert r.outputs is None

    def test_outputs_set(self):
        r = ExecutionResult(status="completed", outputs={"title": "Hello"})
        assert r.outputs == {"title": "Hello"}


# --- resolve_step_runtime tests ---


class TestResolveStepRuntime:
    def test_no_outputs_returns_same_step(self):
        step = WorkflowStep(order=1, name="Click", action="click", selector="#btn")
        ctx = RuntimeContext()
        result = resolve_step_runtime(step, ctx)
        assert result is step  # same object, no copy needed

    def test_interpolates_value_from_step_outputs(self):
        step = WorkflowStep(
            order=2, name="Navigate", action="navigate",
            value="https://example.com/${page_url}",
        )
        ctx = RuntimeContext(step_outputs={"page_url": "dashboard"})
        result = resolve_step_runtime(step, ctx)
        assert result.value == "https://example.com/dashboard"

    def test_interpolates_selector(self):
        step = WorkflowStep(
            order=2, name="Click", action="click",
            selector="[data-id='${record_id}']",
        )
        ctx = RuntimeContext(step_outputs={"record_id": "42"})
        result = resolve_step_runtime(step, ctx)
        assert result.selector == "[data-id='42']"

    def test_unresolved_token_left_as_is(self):
        step = WorkflowStep(
            order=2, name="Nav", action="navigate",
            value="https://example.com/${unknown_var}",
        )
        ctx = RuntimeContext(step_outputs={"other": "val"})
        result = resolve_step_runtime(step, ctx)
        assert result.value == "https://example.com/${unknown_var}"

    def test_multiple_tokens_in_one_field(self):
        step = WorkflowStep(
            order=2, name="Nav", action="navigate",
            value="https://${host}/${path}",
        )
        ctx = RuntimeContext(step_outputs={"host": "example.com", "path": "api/v1"})
        result = resolve_step_runtime(step, ctx)
        assert result.value == "https://example.com/api/v1"

    def test_non_string_output_converted_to_str(self):
        step = WorkflowStep(
            order=2, name="Nav", action="navigate",
            value="Page count: ${count}",
        )
        ctx = RuntimeContext(step_outputs={"count": 42})
        result = resolve_step_runtime(step, ctx)
        assert result.value == "Page count: 42"

    def test_interpolates_body_field(self):
        step = WorkflowStep(
            order=2, name="HTTP", action="http_request",
            body='{"token": "${auth_token}"}',
        )
        ctx = RuntimeContext(step_outputs={"auth_token": "abc123"})
        result = resolve_step_runtime(step, ctx)
        assert result.body == '{"token": "abc123"}'

    def test_no_change_returns_same_step(self):
        step = WorkflowStep(
            order=1, name="Click", action="click", selector="#static",
        )
        ctx = RuntimeContext(step_outputs={"something": "val"})
        result = resolve_step_runtime(step, ctx)
        assert result is step  # no ${} in any field


# --- Schema tests for new fields ---


class TestSchemaNewFields:
    def test_extract_action_accepted(self):
        step = WorkflowStep(
            order=1, name="Extract title", action="extract",
            value="return document.title", output_var="page_title",
        )
        assert step.action == "extract"
        assert step.output_var == "page_title"

    def test_new_actions_accepted(self):
        for action in ("conditional", "loop", "fill_secret", "upload_file", "http_request", "file_op"):
            step = WorkflowStep(order=1, name=f"Test {action}", action=action)
            assert step.action == action

    def test_new_optional_fields_default_none(self):
        step = WorkflowStep(order=1, name="Click", action="click", selector="#btn")
        assert step.output_var is None
        assert step.condition is None
        assert step.compare_to is None
        assert step.skip_steps is None
        assert step.jump_to is None
        assert step.loop_var is None
        assert step.loop_index_var is None
        assert step.loop_steps is None
        assert step.operation is None
        assert step.source is None
        assert step.destination is None
        assert step.content is None
        assert step.pattern is None
        assert step.headers is None
        assert step.body is None
        assert step.auth is None
        assert step.include is None

    def test_existing_workflows_still_validate(self):
        """Existing workflow JSON without new fields must still validate."""
        raw = {
            "schema_version": "1.0",
            "name": "legacy",
            "steps": [
                {"order": 1, "name": "Navigate", "action": "navigate", "value": "https://example.com"},
                {"order": 2, "name": "Click", "action": "click", "selector": "#btn"},
            ],
        }
        wf = Workflow.model_validate(raw)
        assert len(wf.steps) == 2


# --- Executor integration tests ---


class TestExtractAction:
    @pytest.mark.asyncio
    async def test_extract_stores_output_data(self):
        """extract action should run JS and store result in output_data."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Extract title", action="extract",
                value="return document.title", output_var="page_title",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.return_value = "My Page Title"

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.steps[0].status == "passed"
        assert result.steps[0].output_data == "My Page Title"
        mock_driver.run_js.assert_called_once_with("return document.title")

    @pytest.mark.asyncio
    async def test_extract_without_output_var_fails(self):
        """extract action without output_var should fail with clear error."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Bad extract", action="extract",
                value="return document.title",
            ),
        ])
        mock_driver = MagicMock()

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "failed"
        assert "output_var" in result.steps[0].error

    @pytest.mark.asyncio
    async def test_extract_populates_execution_outputs(self):
        """extract outputs should appear in ExecutionResult.outputs."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Extract title", action="extract",
                value="return document.title", output_var="page_title",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.return_value = "Hello World"

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.outputs == {"page_title": "Hello World"}


class TestRunJsWithOutputVar:
    @pytest.mark.asyncio
    async def test_run_js_captures_output_when_output_var_set(self):
        """run_js with output_var should capture the return value."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Get count", action="run_js",
                value="return document.querySelectorAll('tr').length",
                output_var="row_count",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.return_value = 15

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.steps[0].output_data == 15
        assert result.outputs == {"row_count": 15}

    @pytest.mark.asyncio
    async def test_run_js_without_output_var_no_output(self):
        """run_js without output_var should not populate outputs."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Run JS", action="run_js",
                value="console.log('hello')",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.return_value = None

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.steps[0].output_data is None
        assert result.outputs is None


class TestRuntimeInterpolationEndToEnd:
    @pytest.mark.asyncio
    async def test_extract_then_navigate_with_interpolation(self):
        """Step 1 extracts a value; step 2 uses it via ${...} interpolation."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Extract URL", action="extract",
                value="return document.querySelector('a').href",
                output_var="link_url",
            ),
            WorkflowStep(
                order=2, name="Navigate to link", action="navigate",
                value="${link_url}",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.return_value = "https://example.com/next-page"

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.passed == 2
        # The navigate call should have used the extracted URL
        mock_driver.get.assert_called_once_with("https://example.com/next-page")

    @pytest.mark.asyncio
    async def test_multiple_extracts_and_interpolation(self):
        """Multiple extract steps should all be available for interpolation."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Extract host", action="extract",
                value="return window.location.hostname",
                output_var="host",
            ),
            WorkflowStep(
                order=2, name="Extract path", action="extract",
                value="return window.location.pathname",
                output_var="path",
            ),
            WorkflowStep(
                order=3, name="Navigate", action="navigate",
                value="https://${host}/api${path}",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = ["example.com", "/users/42"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        mock_driver.get.assert_called_once_with("https://example.com/api/users/42")
        assert result.outputs == {"host": "example.com", "path": "/users/42"}

    @pytest.mark.asyncio
    async def test_failed_extract_does_not_populate_outputs(self):
        """If extract step fails, its output_var should NOT be stored."""
        wf = _make_workflow(
            [
                WorkflowStep(
                    order=1, name="Bad extract", action="extract",
                    value="throw new Error('oops')", output_var="val",
                ),
            ],
            settings={"human_mode": False, "step_delay_ms": 0, "on_error": "continue"},
        )
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = Exception("JS error")

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.outputs is None  # no successful outputs
