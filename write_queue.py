# STRATA — Write Queue
# Serializes all SQLite writes through a single dedicated thread.
# Prevents "database is locked" errors when multiple agents (Surface,
# Mini, Helios, Telegram, Codex) write simultaneously.
#
# HOW IT WORKS:
# - All DB writes go through WriteQueue.submit() or submit_fire_and_forget()
# - A single daemon thread pulls jobs off a FIFO queue and executes them one at a time
# - Callers block until their write completes (submit) or return immediately (fire_and_forget)
# - Reads bypass the queue entirely — SQLite WAL mode handles concurrent readers
#
# WHY threading.Queue and not asyncio.Queue:
# MCP tools are synchronous functions (FastMCP requirement). They can't await.
# threading.Queue works from both sync and async contexts.

import queue
import threading
import time
import logging

logger = logging.getLogger("strata.write_queue")


class WriteQueue:
    """Single-writer queue for SQLite. Guarantees one DB write at a time.
    
    Usage:
        wq = WriteQueue()
        
        # Blocking — waits for result (use for capture_thought, update_thought, etc.)
        thought_id = wq.submit(db.store_thought, content="...", embedding=emb, ...)
        
        # Fire-and-forget — returns immediately (use for record_access)
        wq.submit_fire_and_forget(db.record_access, [1, 2, 3])
    """

    def __init__(self, timeout=60):
        # FIFO queue — no size limit. Writes arrive way slower than they process
        # (embedding takes 2-3s, SQLite write takes <50ms), so this won't grow unbounded.
        self._queue = queue.Queue()
        self._timeout = timeout

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
        
        The calling thread (from Starlette's thread pool) blocks here.
        The writer thread executes fn(*args, **kwargs) and signals completion.
        
        Returns whatever fn() returns.
        Raises whatever fn() raises.
        """
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
        No result, no error propagation. If it fails, it's logged and skipped.
        """
        self._queue.put((fn, args, kwargs, None, {}, time.monotonic()))

    def _writer_loop(self):
        """The single-threaded writer. Pulls jobs off the queue and executes
        them one at a time. This is the ONLY thread that writes to SQLite.
        
        Runs forever until the process exits (daemon thread).
        """
        while True:
            try:
                job = self._queue.get()
            except Exception:
                continue

            if job is None:
                # Poison pill — clean shutdown (not used in production,
                # but useful for testing)
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
                # Log either way so it shows up in journalctl
                logger.error(f"Write failed [{fn.__name__}]: {e}")
            finally:
                # Signal the waiting caller (if any) that we're done
                if done_event is not None:
                    done_event.set()
                self._queue.task_done()

    @property
    def stats(self):
        """Queue health stats for the /health endpoint.
        
        Returns dict with:
        - queue_depth: how many writes are waiting right now
        - total_processed: lifetime count of completed writes
        - total_errors: lifetime count of failed writes
        - avg_wait_ms: average time a write waited in queue before execution
        - max_wait_ms: longest queue wait ever seen
        """
        with self._lock:
            avg = (self._total_wait_ms / self._total_processed) if self._total_processed > 0 else 0
            return {
                "queue_depth": self._queue.qsize(),
                "total_processed": self._total_processed,
                "total_errors": self._total_errors,
                "avg_wait_ms": round(avg, 1),
                "max_wait_ms": round(self._max_wait_ms, 1),
            }

    def shutdown(self):
        """Clean shutdown — drain the queue and stop the writer thread.
        Not strictly needed (daemon thread dies with process) but good for testing."""
        self._queue.put(None)  # Poison pill
        self._thread.join(timeout=10)
