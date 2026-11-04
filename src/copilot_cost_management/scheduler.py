from __future__ import annotations

import asyncio
from threading import Event, Lock, Thread
from typing import Any, Awaitable, Callable


class DailySnapshotScheduler:
    def __init__(self) -> None:
        self._stop_event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._state: dict[str, Any] = {
            "status": "stopped",
            "interval_hours": 24,
            "last_snapshot_id": None,
            "last_completed_at": None,
            "last_error": None,
        }

    def start(self, capture: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return dict(self._state)
            self._stop_event.clear()
            self._state = {
                "status": "starting",
                "interval_hours": 24,
                "last_snapshot_id": self._state.get("last_snapshot_id"),
                "last_completed_at": self._state.get("last_completed_at"),
                "last_error": None,
            }
            self._thread = Thread(target=self._run, args=(capture,), daemon=True)
            self._thread.start()
            return dict(self._state)

    def _run(self, capture: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                self._state["status"] = "capturing"
                self._state["last_error"] = None
            try:
                result = asyncio.run(capture())
                with self._lock:
                    self._state["last_snapshot_id"] = result.get("snapshot_id")
                    self._state["status"] = result.get("status", "completed")
                    if result.get("status") in ("completed", "completed_with_errors"):
                        self._state["last_completed_at"] = result.get("completed_at")
            except Exception as error:
                with self._lock:
                    self._state["status"] = "failed"
                    self._state["last_error"] = f"{type(error).__name__}: {error}"
            if self._stop_event.wait(24 * 60 * 60):
                break
        with self._lock:
            self._state["status"] = "stopped"

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                self._state["status"] = "stopped"
            return dict(self._state)

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["process_local"] = True
            return state


daily_snapshot_scheduler = DailySnapshotScheduler()
