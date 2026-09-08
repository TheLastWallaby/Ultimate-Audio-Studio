"""Centralized task manager providing thread-pool execution, task tracking, and graceful lifecycle shutdown."""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Callable
from typing import Any

from app.config import log_error


class TaskManager:
    """Manages background worker threads via a bounded ThreadPoolExecutor.

    Provides structured error capture, cooperative cancellation tokens,
    and clean shutdown on application teardown.
    """

    def __init__(self, max_workers: int = 6) -> None:
        self.max_workers = max_workers
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="UAS_Worker")
        self._lock = threading.RLock()
        self._futures: set[concurrent.futures.Future[Any]] = set()
        self._is_shutting_down = False

    def submit_task(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        **kwargs: Any,
    ) -> concurrent.futures.Future[Any] | None:
        """Submit a background callable to the managed worker pool."""
        with self._lock:
            if self._is_shutting_down:
                return None

            future = self._executor.submit(fn, *args, **kwargs)
            self._futures.add(future)

        def _done_callback(fut: concurrent.futures.Future[Any]) -> None:
            with self._lock:
                self._futures.discard(fut)

            if fut.cancelled():
                return

            try:
                result = fut.result()
                if on_success:
                    on_success(result)
            except Exception as exc:
                log_error(f"TaskManager worker exception in {getattr(fn, '__name__', str(fn))}: {exc}")
                if on_error:
                    on_error(exc)

        future.add_done_callback(_done_callback)
        return future

    def shutdown(self, wait: bool = False, cancel_futures: bool = True) -> None:
        """Gracefully shut down task executor and cancel pending futures."""
        with self._lock:
            self._is_shutting_down = True
            pending = list(self._futures)
            self._futures.clear()

        for fut in pending:
            if not fut.done():
                try:
                    fut.cancel()
                except Exception:
                    pass

        try:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        except Exception:
            pass


# Global singleton instance
task_mgr = TaskManager(max_workers=6)
