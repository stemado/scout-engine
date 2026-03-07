"""Tests for the CancellationRegistry service."""

import threading

from app.services.cancellation import cancel, register, unregister


def test_register_returns_unset_threading_event():
    event = register("exec-1")
    assert isinstance(event, threading.Event)
    assert not event.is_set()
    unregister("exec-1")  # cleanup


def test_cancel_sets_the_event():
    event = register("exec-2")
    result = cancel("exec-2")
    assert result is True
    assert event.is_set()
    unregister("exec-2")


def test_cancel_unknown_id_returns_false():
    result = cancel("does-not-exist")
    assert result is False


def test_unregister_prevents_future_cancel():
    register("exec-3")
    unregister("exec-3")
    result = cancel("exec-3")
    assert result is False
