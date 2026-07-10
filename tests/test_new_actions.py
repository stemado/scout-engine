"""Tests for Phase 2: conditional, loop, fill_secret, upload_file, http_request, file_op."""

import json
import os

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from app.schemas import Workflow, WorkflowSettings, WorkflowStep
from app.services.executor import (
    ExecutionResult,
    LoopFrame,
    RuntimeContext,
    StepResult,
    _execute_step_sync,
    execute_workflow,
)


def _make_workflow(steps, **kwargs) -> Workflow:
    defaults = {
        "name": "test",
        "variables": {},
        "steps": steps,
        "settings": {"human_mode": False, "step_delay_ms": 0},
    }
    defaults.update(kwargs)
    return Workflow(**defaults)


# ============================================================
# Conditional action tests
# ============================================================


class TestConditionalTruthy:
    @pytest.mark.asyncio
    async def test_conditional_truthy_skips_steps(self):
        """When condition is truthy and skip_steps is set, skip that many steps."""
        wf = _make_workflow([
            # Step 0: extract a truthy value
            WorkflowStep(
                order=1, name="Set value", action="extract",
                value="return 'yes'", output_var="flag",
            ),
            # Step 1: conditional check -- skip 2 steps if truthy
            WorkflowStep(
                order=2, name="Check flag", action="conditional",
                value="${flag}", condition="truthy", skip_steps=2,
            ),
            # Step 2: should be SKIPPED
            WorkflowStep(
                order=3, name="Skipped click 1", action="extract",
                value="return 'should_not_run_1'", output_var="skip1",
            ),
            # Step 3: should be SKIPPED
            WorkflowStep(
                order=4, name="Skipped click 2", action="extract",
                value="return 'should_not_run_2'", output_var="skip2",
            ),
            # Step 4: should RUN (after skip)
            WorkflowStep(
                order=5, name="After skip", action="extract",
                value="return 'reached'", output_var="after",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = ["yes", "reached"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        # Steps executed: extract(0), conditional(1), extract(4)
        assert result.passed == 3
        assert result.outputs.get("after") == "reached"
        assert result.outputs.get("skip1") is None
        assert result.outputs.get("skip2") is None

    @pytest.mark.asyncio
    async def test_conditional_falsy_no_skip(self):
        """When condition is falsy (value is truthy), condition_met is False, no skip."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Set value", action="extract",
                value="return 'hello'", output_var="flag",
            ),
            WorkflowStep(
                order=2, name="Check falsy", action="conditional",
                value="${flag}", condition="falsy", skip_steps=1,
            ),
            # This should NOT be skipped because "hello" is not falsy
            WorkflowStep(
                order=3, name="Not skipped", action="extract",
                value="return 'ran'", output_var="result",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = ["hello", "ran"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.passed == 3
        assert result.outputs.get("result") == "ran"

    @pytest.mark.asyncio
    async def test_conditional_equals(self):
        """Equals condition compares value to compare_to."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Set value", action="extract",
                value="return 'active'", output_var="status",
            ),
            WorkflowStep(
                order=2, name="Check equals", action="conditional",
                value="${status}", condition="equals", compare_to="active",
                skip_steps=1,
            ),
            # Should be skipped
            WorkflowStep(
                order=3, name="Skipped", action="extract",
                value="return 'no'", output_var="skipped",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = ["active"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.outputs.get("skipped") is None

    @pytest.mark.asyncio
    async def test_conditional_contains(self):
        """Contains condition checks if compare_to is in value."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Set value", action="extract",
                value="return 'error: file not found'", output_var="msg",
            ),
            WorkflowStep(
                order=2, name="Check contains", action="conditional",
                value="${msg}", condition="contains", compare_to="error",
                skip_steps=1,
            ),
            WorkflowStep(
                order=3, name="Skipped", action="extract",
                value="return 'no'", output_var="skipped",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = ["error: file not found"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.outputs.get("skipped") is None

    @pytest.mark.asyncio
    async def test_conditional_matches(self):
        """Matches condition uses regex."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Set value", action="extract",
                value="return 'order-12345-confirmed'", output_var="text",
            ),
            WorkflowStep(
                order=2, name="Check regex", action="conditional",
                value="${text}", condition="matches", compare_to=r"order-\d+-confirmed",
                skip_steps=1,
            ),
            WorkflowStep(
                order=3, name="Skipped", action="extract",
                value="return 'no'", output_var="skipped",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = ["order-12345-confirmed"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.outputs.get("skipped") is None

    @pytest.mark.asyncio
    async def test_conditional_jump_to(self):
        """jump_to sets the step index directly."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Set value", action="extract",
                value="return 'go'", output_var="flag",
            ),
            # Step index 1: conditional -- jump to step index 3
            WorkflowStep(
                order=2, name="Jump", action="conditional",
                value="${flag}", condition="truthy", jump_to=3,
            ),
            # Step index 2: should be SKIPPED
            WorkflowStep(
                order=3, name="Skipped", action="extract",
                value="return 'no'", output_var="skipped",
            ),
            # Step index 3: should RUN (jump target)
            WorkflowStep(
                order=4, name="Jumped to", action="extract",
                value="return 'landed'", output_var="landed",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = ["go", "landed"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.outputs.get("skipped") is None
        assert result.outputs.get("landed") == "landed"


# ============================================================
# Loop action tests
# ============================================================


class TestLoop:
    @pytest.mark.asyncio
    async def test_loop_iterates_over_list(self):
        """Loop over ["a","b","c"], verify loop_var is set correctly each iteration."""
        wf = _make_workflow([
            # Step 0: loop declaration
            WorkflowStep(
                order=1, name="Loop items", action="loop",
                value='["a","b","c"]', loop_var="item",
                loop_index_var="idx", loop_steps=1,
            ),
            # Step 1: body -- extract the current item (runs 3 times)
            WorkflowStep(
                order=2, name="Use item", action="extract",
                value="return document.title", output_var="last_item",
            ),
        ])
        mock_driver = MagicMock()
        # run_js is called once per loop iteration for the extract
        mock_driver.run_js.side_effect = ["title_a", "title_b", "title_c"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        # 1 loop step + 3 body executions = 4 passed
        assert result.passed == 4
        # After the loop, item should be "c" and idx should be 2
        assert result.outputs["item"] == "c"
        assert result.outputs["idx"] == 2

    @pytest.mark.asyncio
    async def test_loop_empty_list_skips_body(self):
        """Empty list should skip the body steps entirely."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Loop empty", action="loop",
                value='[]', loop_var="item", loop_steps=1,
            ),
            # This should be SKIPPED
            WorkflowStep(
                order=2, name="Body", action="extract",
                value="return 'no'", output_var="body_ran",
            ),
            # This should RUN (after loop body)
            WorkflowStep(
                order=3, name="After loop", action="extract",
                value="return 'yes'", output_var="after",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = ["yes"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert result.outputs.get("body_ran") is None
        assert result.outputs.get("after") == "yes"

    @pytest.mark.asyncio
    async def test_loop_index_var(self):
        """Verify the index variable increments correctly."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Loop", action="loop",
                value='["x","y"]', loop_var="item",
                loop_index_var="i", loop_steps=1,
            ),
            WorkflowStep(
                order=2, name="Body", action="extract",
                value="return 'ok'", output_var="dummy",
            ),
        ])
        mock_driver = MagicMock()
        mock_driver.run_js.side_effect = ["ok", "ok"]

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        # After 2 iterations, index should be 1 (last iteration)
        assert result.outputs["i"] == 1
        assert result.outputs["item"] == "y"


# ============================================================
# fill_secret tests
# ============================================================


class TestFillSecret:
    @pytest.mark.asyncio
    async def test_fill_secret_types_value(self):
        """fill_secret should type into the element."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Enter password", action="fill_secret",
                selector="#password", value="s3cret!",
            ),
        ])
        mock_driver = MagicMock()

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        mock_driver.type.assert_called_once_with("#password", "s3cret!")

    @pytest.mark.asyncio
    async def test_fill_secret_scrubs_value(self):
        """fill_secret output_data should only contain chars_typed, not the actual value."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Enter password", action="fill_secret",
                selector="#password", value="supersecret123",
            ),
        ])
        mock_driver = MagicMock()

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        step_result = result.steps[0]
        assert step_result.output_data == {"chars_typed": 14}
        assert "supersecret123" not in str(step_result.output_data)


# ============================================================
# upload_file tests
# ============================================================


class TestUploadFile:
    @pytest.mark.asyncio
    async def test_upload_file_calls_driver(self):
        """upload_file should attempt to upload via the element."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Upload", action="upload_file",
                selector="input[type=file]", value="/path/to/file.csv",
            ),
        ])
        mock_driver = MagicMock()
        mock_elem = MagicMock()
        mock_elem.upload = MagicMock()
        mock_driver.select.return_value = mock_elem

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        mock_driver.select.assert_called_once_with("input[type=file]")
        mock_elem.upload.assert_called_once_with("/path/to/file.csv")


# ============================================================
# http_request tests
# ============================================================


class TestHttpRequest:
    @pytest.mark.asyncio
    async def test_http_request_get(self):
        """GET request should store response."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="GET", action="http_request",
                value="https://api.example.com/data",
                output_var="resp",
            ),
        ])
        mock_driver = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "ok"}
        mock_response.headers = {"Content-Type": "application/json"}

        with (
            patch("app.services.executor._create_driver", return_value=mock_driver),
            patch("app.services.executor._requests_lib.request", return_value=mock_response) as mock_req,
        ):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        mock_req.assert_called_once_with(
            "GET", "https://api.example.com/data",
            headers={}, data=None, auth=None, timeout=30.0,
        )
        assert result.outputs["resp"]["status_code"] == 200
        assert result.outputs["resp"]["body"] == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_http_request_post_with_body(self):
        """POST request with body."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="POST", action="http_request",
                value="https://api.example.com/submit",
                method="POST", body='{"key": "val"}',
                headers={"Content-Type": "application/json"},
                output_var="resp",
            ),
        ])
        mock_driver = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": 42}
        mock_response.headers = {}

        with (
            patch("app.services.executor._create_driver", return_value=mock_driver),
            patch("app.services.executor._requests_lib.request", return_value=mock_response) as mock_req,
        ):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        mock_req.assert_called_once_with(
            "POST", "https://api.example.com/submit",
            headers={"Content-Type": "application/json"},
            data='{"key": "val"}', auth=None, timeout=30.0,
        )
        assert result.outputs["resp"]["body"]["id"] == 42

    @pytest.mark.asyncio
    async def test_http_request_basic_auth(self):
        """Basic auth should be parsed from 'basic:user:pass' format."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Auth GET", action="http_request",
                value="https://api.example.com/secure",
                auth="basic:myuser:mypass",
                output_var="resp",
            ),
        ])
        mock_driver = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_response.headers = {}

        with (
            patch("app.services.executor._create_driver", return_value=mock_driver),
            patch("app.services.executor._requests_lib.request", return_value=mock_response) as mock_req,
        ):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        mock_req.assert_called_once()
        call_kwargs = mock_req.call_args
        assert call_kwargs.kwargs.get("auth") == ("myuser", "mypass")

    @pytest.mark.asyncio
    async def test_http_request_json_response(self):
        """JSON response should be parsed into dict."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="JSON GET", action="http_request",
                value="https://api.example.com/json",
                output_var="data",
            ),
        ])
        mock_driver = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [1, 2, 3]
        mock_response.headers = {}

        with (
            patch("app.services.executor._create_driver", return_value=mock_driver),
            patch("app.services.executor._requests_lib.request", return_value=mock_response),
        ):
            result = await execute_workflow(wf)

        assert result.outputs["data"]["body"] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_http_request_text_fallback(self):
        """Non-JSON response should fall back to text."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Text GET", action="http_request",
                value="https://example.com/plain",
                output_var="data",
            ),
        ])
        mock_driver = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("not json")
        mock_response.text = "plain text response"
        mock_response.headers = {}

        with (
            patch("app.services.executor._create_driver", return_value=mock_driver),
            patch("app.services.executor._requests_lib.request", return_value=mock_response),
        ):
            result = await execute_workflow(wf)

        assert result.outputs["data"]["body"] == "plain text response"


# ============================================================
# file_op tests
# ============================================================


class TestFileOp:
    @pytest.mark.asyncio
    async def test_file_op_mkdir(self, tmp_path):
        """mkdir should create the directory."""
        target_dir = str(tmp_path / "new_dir" / "sub")
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Mkdir", action="file_op",
                operation="mkdir", destination=target_dir,
                output_var="result",
            ),
        ])
        mock_driver = MagicMock()

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert os.path.isdir(target_dir)
        assert result.outputs["result"] == target_dir

    @pytest.mark.asyncio
    async def test_file_op_copy(self, tmp_path):
        """copy should copy the file."""
        src = tmp_path / "source.txt"
        src.write_text("hello world")
        dst = str(tmp_path / "dest.txt")

        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Copy", action="file_op",
                operation="copy", source=str(src), destination=dst,
                output_var="result",
            ),
        ])
        mock_driver = MagicMock()

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        assert os.path.isfile(dst)
        with open(dst) as f:
            assert f.read() == "hello world"

    @pytest.mark.asyncio
    async def test_file_op_write(self, tmp_path):
        """write should create file with content."""
        target = str(tmp_path / "output" / "file.txt")
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Write", action="file_op",
                operation="write", destination=target,
                content="line1\nline2",
                output_var="result",
            ),
        ])
        mock_driver = MagicMock()

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        with open(target) as f:
            assert f.read() == "line1\nline2"

    @pytest.mark.asyncio
    async def test_file_op_glob(self, tmp_path):
        """glob should return matching files."""
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        (tmp_path / "c.csv").write_text("c")

        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Glob", action="file_op",
                operation="glob", source=str(tmp_path),
                pattern="*.txt", output_var="files",
            ),
        ])
        mock_driver = MagicMock()

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        files = json.loads(result.outputs["files"])
        basenames = sorted(os.path.basename(f) for f in files)
        assert basenames == ["a.txt", "b.txt"]

    @pytest.mark.asyncio
    async def test_file_op_list(self, tmp_path):
        """list should return directory contents sorted."""
        (tmp_path / "zebra.txt").write_text("z")
        (tmp_path / "alpha.txt").write_text("a")
        os.makedirs(tmp_path / "mid_dir")

        wf = _make_workflow([
            WorkflowStep(
                order=1, name="List", action="file_op",
                operation="list", source=str(tmp_path),
                output_var="entries",
            ),
        ])
        mock_driver = MagicMock()

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "completed"
        entries = json.loads(result.outputs["entries"])
        basenames = [os.path.basename(e) for e in entries]
        assert basenames == ["alpha.txt", "mid_dir", "zebra.txt"]

    @pytest.mark.asyncio
    async def test_file_op_unknown_operation(self):
        """Unknown operation should fail."""
        wf = _make_workflow([
            WorkflowStep(
                order=1, name="Bad op", action="file_op",
                operation="delete", source="/tmp/x",
            ),
        ])
        mock_driver = MagicMock()

        with patch("app.services.executor._create_driver", return_value=mock_driver):
            result = await execute_workflow(wf)

        assert result.status == "failed"
        assert "Unknown file_op operation" in result.steps[0].error


# ============================================================
# Unit tests for conditional evaluation (no driver needed)
# ============================================================


class TestConditionalUnit:
    """Direct tests of _execute_step_sync for conditional logic."""

    def test_truthy_values(self):
        mock_driver = MagicMock()
        for val in ("yes", "1", "true", "hello"):
            step = WorkflowStep(
                order=1, name="test", action="conditional",
                value=val, condition="truthy", skip_steps=1,
            )
            result = _execute_step_sync(mock_driver, step, 30000)
            assert result.output_data["condition_met"] is True, f"Expected truthy for {val!r}"

    def test_falsy_values(self):
        mock_driver = MagicMock()
        for val in ("", "false", "null", "0", None):
            step = WorkflowStep(
                order=1, name="test", action="conditional",
                value=val, condition="truthy", skip_steps=1,
            )
            result = _execute_step_sync(mock_driver, step, 30000)
            assert result.output_data["condition_met"] is False, f"Expected falsy for {val!r}"

    def test_falsy_condition(self):
        mock_driver = MagicMock()
        step = WorkflowStep(
            order=1, name="test", action="conditional",
            value="", condition="falsy", skip_steps=1,
        )
        result = _execute_step_sync(mock_driver, step, 30000)
        assert result.output_data["condition_met"] is True

    def test_equals_match(self):
        mock_driver = MagicMock()
        step = WorkflowStep(
            order=1, name="test", action="conditional",
            value="active", condition="equals", compare_to="active",
        )
        result = _execute_step_sync(mock_driver, step, 30000)
        assert result.output_data["condition_met"] is True

    def test_equals_no_match(self):
        mock_driver = MagicMock()
        step = WorkflowStep(
            order=1, name="test", action="conditional",
            value="inactive", condition="equals", compare_to="active",
        )
        result = _execute_step_sync(mock_driver, step, 30000)
        assert result.output_data["condition_met"] is False

    def test_contains_match(self):
        mock_driver = MagicMock()
        step = WorkflowStep(
            order=1, name="test", action="conditional",
            value="error: timeout", condition="contains", compare_to="error",
        )
        result = _execute_step_sync(mock_driver, step, 30000)
        assert result.output_data["condition_met"] is True

    def test_matches_regex(self):
        mock_driver = MagicMock()
        step = WorkflowStep(
            order=1, name="test", action="conditional",
            value="order-99-done", condition="matches", compare_to=r"order-\d+-done",
        )
        result = _execute_step_sync(mock_driver, step, 30000)
        assert result.output_data["condition_met"] is True

    def test_matches_regex_no_match(self):
        mock_driver = MagicMock()
        step = WorkflowStep(
            order=1, name="test", action="conditional",
            value="order-abc-done", condition="matches", compare_to=r"order-\d+-done",
        )
        result = _execute_step_sync(mock_driver, step, 30000)
        assert result.output_data["condition_met"] is False
