"""Tests for the pause/resume registry service."""

import queue

from app.services.pause import (
    ResumeInstruction,
    register,
    request_pause,
    resume,
    unregister,
)


def test_register_returns_queue_and_event():
    pause_queue, pause_requested = register("exec-1")
    assert isinstance(pause_queue, queue.Queue)
    assert not pause_requested.is_set()
    unregister("exec-1")


def test_resume_puts_instruction_on_queue():
    pause_queue, _ = register("exec-2")
    instruction = ResumeInstruction(action="retry")
    result = resume("exec-2", instruction)
    assert result is True
    assert pause_queue.get_nowait() is instruction
    unregister("exec-2")


def test_resume_unknown_id_returns_false():
    result = resume("does-not-exist", ResumeInstruction(action="abort"))
    assert result is False


def test_request_pause_sets_event():
    _, pause_requested = register("exec-3")
    result = request_pause("exec-3")
    assert result is True
    assert pause_requested.is_set()
    unregister("exec-3")


def test_request_pause_unknown_id_returns_false():
    result = request_pause("does-not-exist")
    assert result is False


def test_unregister_prevents_future_resume():
    register("exec-4")
    unregister("exec-4")
    result = resume("exec-4", ResumeInstruction(action="continue"))
    assert result is False


def test_resume_instruction_with_step_index():
    pause_queue, _ = register("exec-5")
    instruction = ResumeInstruction(action="jump", step_index=6)
    resume("exec-5", instruction)
    got = pause_queue.get_nowait()
    assert got.action == "jump"
    assert got.step_index == 6
    unregister("exec-5")
