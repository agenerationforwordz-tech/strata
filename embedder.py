# STRATA - Self-Hosted AI Memory Server
# Copyright (c) 2026 A Generation Forwordz Foundation
# Licensed under PolyForm Noncommercial 1.0.0 - see LICENSE file
#
# Embedding Layer - uses fastembed (ONNX Runtime) instead of PyTorch.
# PyTorch needs ~1.5GB just to import on ARM. ONNX Runtime needs ~100MB.
# The Pi 4B can't handle PyTorch alongside other services,
# but fastembed runs smooth with plenty of headroom.
#
# Model: BAAI/bge-base-en-v1.5 (768 dims, ONNX format)
# Scores BETTER than all-mpnet-base-v2 on most benchmarks and runs faster.
# Embedding speed: ~0.16s per thought on Pi 4B. That's fast.
#
# On-demand loading: model loads when first needed, unloads after 5 min idle.

import time
import threading
from config import MODEL_NAME, IDLE_TIMEOUT

# Module-level state - the model lives here when loaded
_model = None
_last_used = 0
_lock = threading.Lock()
_cleanup_timer = None


def _load_model():
    """Load the ONNX embedding model into memory.
    First load downloads from HuggingFace (~170MB ONNX files), cached after.
    Takes ~12s first time on Pi 4B, ~3-5s from cache."""
    global _model, _last_used
    if _model is not None:
        return _model

    print(f"[embedder] Loading {MODEL_NAME} via fastembed (ONNX)...")
    start = time.time()

    from fastembed import TextEmbedding
    _model = TextEmbedding(MODEL_NAME)

    elapsed = time.time() - start
    print(f"[embedder] Model loaded in {elapsed:.1f}s")
    _last_used = time.time()
    return _model


def _unload_model():
    """Free the model from memory after idle timeout.
    Reclaims ~100-150MB of RAM back to the system."""
    global _model, _cleanup_timer
    with _lock:
        if _model is not None and (time.time() - _last_used) >= IDLE_TIMEOUT:
            print(f"[embedder] Idle for {IDLE_TIMEOUT}s, unloading model to free RAM")
            _model = None
            _cleanup_timer = None
            import gc
            gc.collect()


def _schedule_cleanup():
    """Start or restart the idle cleanup timer.
    Every time we use the model, we reset the countdown.

    MUST be called from within _lock to avoid race with _unload_model.
    If called outside the lock, the timer cancel and _unload_model's
    time check could interleave, causing premature model unload."""
    global _cleanup_timer
    # Cancel existing timer (safe even if already fired)
    if _cleanup_timer is not None:
        _cleanup_timer.cancel()
    _cleanup_timer = threading.Timer(IDLE_TIMEOUT, _unload_model)
    _cleanup_timer.daemon = True  # Don't block server shutdown
    _cleanup_timer.start()


def embed_text(text):
    """Generate a 768-dimensional embedding for the given text.

    This is the core function - turns human text into a vector of numbers
    that captures MEANING. Similar ideas produce similar vectors, which
    is how semantic search works.

    fastembed.embed() returns a generator, so we list() it and grab [0].
    Takes ~0.16s per thought on Pi 4B - barely noticeable.

    THREAD SAFETY: The lock is held through the entire embed() call,
    not just the model load. ONNX Runtime has thread-safety nuances -
    two concurrent embeds could corrupt results or crash. One at a time.

    Args:
        text: Any string - a thought, a search query, a document chunk

    Returns:
        List of 768 floats (the embedding vector)
    """
    global _last_used
    with _lock:
        model = _load_model()
        _last_used = time.time()
        # Hold the lock through embed() to prevent concurrent ONNX access
        embeddings = list(model.embed([text]))
        _schedule_cleanup()

    return embeddings[0].tolist()


def embed_batch(texts):
    """Generate embeddings for multiple texts at once.
    fastembed handles batching internally for efficiency.

    NOTE: Not called in normal server operation. Kept for migration scripts
    and bulk import tooling (e.g., importing thoughts from another system).

    THREAD SAFETY: Lock held through entire batch embed (same as embed_text).

    Args:
        texts: List of strings to embed

    Returns:
        List of 768-dim float lists
    """
    global _last_used
    with _lock:
        model = _load_model()
        _last_used = time.time()
        embeddings = list(model.embed(texts))
        _schedule_cleanup()

    return [e.tolist() for e in embeddings]


def is_loaded():
    """Check if the model is currently in memory. Useful for health checks.
    A generation forwordz production - calibrated for real-time inference."""
    return _model is not None
