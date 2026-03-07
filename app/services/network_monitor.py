"""CDP network monitoring -- captures requests, responses, and download events.

Ported from Scout's NetworkMonitor (scout/src/scout/network.py).
Simplified: no body capture (not needed for workflow execution).
Public API matches Scout's CLI executor: start(), stop(), query(), wait_for_download().
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from botasaurus_driver import Driver

# Internal Chrome URL prefixes to filter out of captured events
_INTERNAL_PREFIXES = (
    "chrome://",
    "chrome-extension://",
    "chrome-untrusted://",
    "devtools://",
    "data:",
    "about:",
)


@dataclass
class NetworkEvent:
    """A single captured network response event."""

    url: str
    method: str = "GET"
    status: int | None = None
    triggered_download: bool = False
    download_filename: str | None = None


class NetworkMonitor:
    """Monitors network activity via botasaurus-driver's CDP callback system.

    Usage pattern (must start monitoring BEFORE the step that triggers the
    download/response -- the look-ahead pattern from Scout's executor):

        monitor = NetworkMonitor()
        monitor.start(driver, url_pattern="api/export")  # before trigger step
        # ... execute trigger step (e.g. click download button) ...
        events = monitor.wait_for_download(timeout_ms=30000)
        monitor.stop()
    """

    def __init__(self) -> None:
        self._events: list[NetworkEvent] = []
        self._monitoring = False
        self._url_pattern: re.Pattern | None = None
        self._driver: Driver | None = None
        self._download_event = threading.Event()
        self._lock = threading.Lock()
        self._pending_requests: dict[str, dict] = {}

    def start(self, driver: Driver, url_pattern: str | None = None) -> None:
        """Start monitoring. Registers CDP callbacks on the driver.

        Args:
            driver: Active botasaurus Driver instance.
            url_pattern: Optional regex to filter captured events by URL.
        """
        self._driver = driver
        self._url_pattern = re.compile(url_pattern) if url_pattern else None
        self._monitoring = True
        self._download_event.clear()
        with self._lock:
            self._events.clear()
            self._pending_requests.clear()

        driver.before_request_sent(self._on_request)
        driver.after_response_received(self._on_response)

    def stop(self) -> None:
        """Stop capturing new events. Existing events are preserved for query()."""
        self._monitoring = False

    @property
    def is_active(self) -> bool:
        """Whether the monitor is currently capturing events."""
        return self._monitoring

    def query(self, url_pattern: str | None = None) -> list[NetworkEvent]:
        """Return captured events, optionally filtered by URL regex pattern."""
        with self._lock:
            events = list(self._events)
        if url_pattern:
            pat = re.compile(url_pattern)
            return [e for e in events if pat.search(e.url)]
        return events

    def wait_for_download(self, timeout_ms: int = 30000) -> list[NetworkEvent]:
        """Block until a download response is detected or timeout elapses.

        Returns list of events with triggered_download=True. Empty list on timeout.
        """
        self._download_event.wait(timeout=timeout_ms / 1000)
        with self._lock:
            return [e for e in self._events if e.triggered_download]

    # -- CDP callbacks (called on the websocket thread) ------------------------

    def _on_request(self, request_id: str, request, event) -> None:
        """Handle Network.requestWillBeSent -- store request metadata."""
        if not self._monitoring:
            return

        url = request.url if hasattr(request, "url") else str(request)
        if any(url.startswith(p) for p in _INTERNAL_PREFIXES):
            return
        if self._url_pattern and not self._url_pattern.search(url):
            return

        method = request.method if hasattr(request, "method") else "GET"
        with self._lock:
            self._pending_requests[request_id] = {"url": url, "method": method}

    def _on_response(self, request_id: str, response, event) -> None:
        """Handle Network.responseReceived -- build and store a NetworkEvent."""
        if not self._monitoring:
            return

        url = response.url if hasattr(response, "url") else ""
        if any(url.startswith(p) for p in _INTERNAL_PREFIXES):
            return
        if self._url_pattern and not self._url_pattern.search(url):
            return

        with self._lock:
            req_meta = self._pending_requests.pop(request_id, {})

        status = response.status if hasattr(response, "status") else None
        headers: dict = {}
        if hasattr(response, "headers") and response.headers:
            headers = (
                dict(response.headers)
                if not isinstance(response.headers, dict)
                else response.headers
            )

        # Detect file download via Content-Disposition: attachment
        content_disposition = headers.get(
            "content-disposition", headers.get("Content-Disposition", "")
        )
        is_download = (
            "attachment" in content_disposition.lower() if content_disposition else False
        )
        download_filename = None
        if is_download and "filename=" in content_disposition:
            download_filename = content_disposition.split("filename=")[-1].strip('" ')

        net_event = NetworkEvent(
            url=url or req_meta.get("url", ""),
            method=req_meta.get("method", "GET"),
            status=status,
            triggered_download=is_download,
            download_filename=download_filename,
        )

        with self._lock:
            self._events.append(net_event)

        if is_download:
            self._download_event.set()
