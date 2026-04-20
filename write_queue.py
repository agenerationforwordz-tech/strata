# STRATA — Write Queue (SQLite concurrency workaround)
# SQLite only allows one writer at a time. This queue serializes write operations
# so multiple agents don't step on each other.
#
# NOTE: When using PostgreSQL backend (STRATA_DB_BACKEND=postgresql), the WriteQueue
# becomes a pass-through — submit() calls the function directly instead of queuing.
# PostgreSQL handles concurrency natively, so no serialization is needed.

import logging
import os
import queue
import threading
import time

logger = logging.getLogger("strata.write_queue")

_PG_MODE = os.environ.get("STRATA_DB_BACKEND", "sqlite").lower() == "postgresql"


class WriteQueue:
    """Single-writer queue for SQLite. Guarantees one DB write at a time.

    When STRATA_DB_BACKEND=postgresql, all methods become direct pass-through
    calls — no queue, no thread, no overhead. PostgreSQL handles concurrency.

    Usage:
        wq = WriteQueue()

        # Blocking — waits for result (use for capture_thought, update_thought, etc.)
        thought_id = wq.submit(db.store_thought, content="...", embedding=emb, ...)

        # Fire-and-forget — returns immediately (use for record_access)
        wq.submit_fire_and_forget(db.record_access, [1, 2, 3])
    """

    def __init__(self, timeout=60):
        self._timeout = timeout

        if _PG_MODE:
            # PostgreSQL mode — no queue needed, just pass through
            logger.info("Write queue: PASS-THROUGH mode (PostgreSQL handles concurrency)")
            self._total_processed = 0
            self._total_errors = 0
            return

        # SQLite mode — start the single-writer queue
        # FIFO queue — no size limit. Writes arrive way slower than they process
        # (embedding takes 2-3s, SQLite write takes <50ms), so this won't grow unbounded.
        self._queue = queue.Queue()

        # Stats for monitoring via /health endpoint
        self._total_processed = 0
        self._total_errors = 0
        self._total_wait_ms = 0.0
        self._max_wait_ms = 0.0
        self._lock = threading.Lock()  # Protects stats counters

        # Start the single writer thread. Daemon=True so it dies with the process —
        # no need for explicit shutdown when systemd sends SIGTERM.
        self._thread = threading.Thread(target=self._writer_loop, daemon=True, name="strata-writer")
        self._thread.start()
        logger.info("Write queue started (single-writer thread)")

    def submit(self, fn, *args, **kwargs):
        """Submit a write job and block until it completes.

        Used by capture_thought, update_thought, delete_thought, etc.
        that need to return a result to the caller.

        In PostgreSQL mode, calls fn() directly — no queue overhead.

        Returns whatever fn() returns.
        Raises whatever fn() raises.
        """
        if _PG_MODE:
            # Direct call — PostgreSQL handles concurrency
            self._total_processed += 1
            return fn(*args, **kwargs)

        result_holder = {}  # Mutable dict to pass result back between threads
        done_event = threading.Event()  # Writer thread sets this when done
        enqueue_time = time.monotonic()

        self._queue.put((fn, args, kwargs, done_event, result_holder, enqueue_time))

        # Block until the writer thread processes our job
        if not done_event.wait(timeout=self._timeout):
            raise TimeoutError(
                f"Write queue timeout after {self._timeout}s. "
                f"Queue depth: {self._queue.qsize()}. "
                f"Function: {fn.__name__}"
            )

        # Re-raise any exception from the writer thread
        if "error" in result_holder:
            raise result_holder["error"]

        return result_holder.get("result")

    def submit_fire_and_forget(self, fn, *args, **kwargs):
        """Submit a write job without waiting for completion.

        Used by record_access — the read tool returns search results
        immediately while access tracking happens in the background.
        No result, no error propagation.

        In PostgreSQL mode, calls fn() directly.
        """
        if _PG_MODE:
            try:
                fn(*args, **kwargs)
            except Exception as e:
                logger.error(f"Fire-and-forget failed [{fn.__name__}]: {e}")
            return

        self._queue.put((fn, args, kwargs, None, {}, time.monotonic()))

    def _writer_loop(self):
        """The single-threaded writer. Pulls jobs off the queue and executes
        them one at a time. This is the ONLY thread that writes to SQLite.

        Runs forever until the process exits (daemon thread).
        Not started in PostgreSQL mode.
        """
        while True:
            try:
                job = self._queue.get()
            except Exception:
                continue

            if job is None:
                # Poison pill — clean shutdown
                break

            fn, args, kwargs, done_event, result_holder, enqueue_time = job
            wait_ms = (time.monotonic() - enqueue_time) * 1000

            # Update stats (thread-safe)
            with self._lock:
                self._total_processed += 1
                self._total_wait_ms += wait_ms
                self._max_wait_ms = max(self._max_wait_ms, wait_ms)

            try:
                result = fn(*args, **kwargs)
                result_holder["result"] = result
            except Exception as e:
                with self._lock:
                    self._total_errors += 1
                if done_event is not None:
                    # Blocking caller — they need to know about the error
                    result_holder["error"] = e
                logger.error(f"Write failed [{fn.__name__}]: {e}")
            finally:
                if done_event is not None:
                    done_event.set()
                self._queue.task_done()

    @property
    def stats(self):
        """Queue health stats for the /health endpoint."""
        if _PG_MODE:
            return {
                "mode": "pass-through (PostgreSQL)",
                "total_processed": self._total_processed,
                "total_errors": self._total_errors,
            }

        with self._lock:
            avg = (self._total_wait_ms / self._total_processed) if self._total_processed > 0 else 0
            return {
                "mode": "queued (SQLite)",
                "queue_depth": self._queue.qsize(),
                "total_processed": self._total_processed,
                "total_errors": self._total_errors,
                "avg_wait_ms": round(avg, 1),
                "max_wait_ms": round(self._max_wait_ms, 1),
            }

    def shutdown(self):
        """Clean shutdown — drain the queue and stop the writer thread.
        No-op in PostgreSQL mode."""
        if _PG_MODE:
            return
        self._queue.put(None)  # Poison pill
        self._thread.join(timeout=10)
