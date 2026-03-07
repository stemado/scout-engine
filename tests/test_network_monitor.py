"""Tests for the NetworkMonitor service.

NetworkMonitor uses botasaurus-driver CDP callbacks. We test it by calling
the internal _on_response handler directly with mock objects -- no browser needed.
"""

import threading
from unittest.mock import MagicMock

from app.services.network_monitor import NetworkMonitor


def _make_mock_response(url: str, content_disposition: str = "", status: int = 200):
    """Build a minimal mock response object."""
    mock = MagicMock()
    mock.url = url
    mock.status = status
    mock.headers = {"content-disposition": content_disposition} if content_disposition else {}
    mock.mime_type = "application/json"
    return mock


def test_query_returns_empty_before_any_events():
    monitor = NetworkMonitor()
    assert monitor.query() == []


def test_on_response_captures_regular_response():
    monitor = NetworkMonitor()
    monitor._monitoring = True

    monitor._on_response("req-1", _make_mock_response("https://api.example.com/data"), MagicMock())

    events = monitor.query()
    assert len(events) == 1
    assert events[0].url == "https://api.example.com/data"
    assert events[0].triggered_download is False


def test_on_response_detects_download_via_content_disposition():
    monitor = NetworkMonitor()
    monitor._monitoring = True

    monitor._on_response(
        "req-1",
        _make_mock_response(
            "https://example.com/report",
            content_disposition='attachment; filename="report.csv"',
        ),
        MagicMock(),
    )

    events = monitor.query()
    assert len(events) == 1
    assert events[0].triggered_download is True
    assert events[0].download_filename == "report.csv"


def test_query_filters_by_url_pattern():
    monitor = NetworkMonitor()
    monitor._monitoring = True

    monitor._on_response("r1", _make_mock_response("https://api.example.com/data"), MagicMock())
    monitor._on_response("r2", _make_mock_response("https://other.com/thing"), MagicMock())

    matched = monitor.query(url_pattern="api.example.com")
    assert len(matched) == 1
    assert "api.example.com" in matched[0].url


def test_wait_for_download_times_out_with_no_download():
    monitor = NetworkMonitor()
    events = monitor.wait_for_download(timeout_ms=100)
    assert events == []


def test_wait_for_download_returns_immediately_when_event_already_set():
    """Simulate: download fires in background, wait_for_download picks it up."""
    monitor = NetworkMonitor()
    monitor._monitoring = True

    def fire():
        monitor._on_response(
            "req-dl",
            _make_mock_response(
                "https://example.com/file.csv",
                content_disposition='attachment; filename="data.csv"',
            ),
            MagicMock(),
        )

    t = threading.Thread(target=fire)
    t.start()
    t.join()

    events = monitor.wait_for_download(timeout_ms=1000)
    assert len(events) == 1
    assert events[0].download_filename == "data.csv"


def test_stop_prevents_new_events_from_being_captured():
    monitor = NetworkMonitor()
    monitor._monitoring = True
    monitor.stop()

    monitor._on_response("req-1", _make_mock_response("https://example.com"), MagicMock())

    assert monitor.query() == []


def test_internal_chrome_urls_are_filtered():
    monitor = NetworkMonitor()
    monitor._monitoring = True

    monitor._on_response("r1", _make_mock_response("chrome://settings/"), MagicMock())
    monitor._on_response("r2", _make_mock_response("devtools://inspector"), MagicMock())

    assert monitor.query() == []
