"""Vera Message Engine — State Management.

Handles context storage with version semantics, conversation tracking,
and suppression logic.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Dict, Optional, Tuple

_lock = threading.Lock()

# In-memory stores
_contexts: Dict[Tuple[str, str], Dict[str, Any]] = {}
_conversations: Dict[str, list] = {}
_sent_suppressions: Dict[str, float] = {}  # suppression_key -> timestamp
_start_time = time.time()


def _key(scope: str, context_id: str) -> Tuple[str, str]:
    return (scope, context_id)


def store_context(scope: str, context_id: str, version: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Store or update a context entry. Returns the stored record."""
    key = _key(scope, context_id)
    with _lock:
        existing = _contexts.get(key)
        if existing and existing["version"] > version:
            return {"stale": True, "current_version": existing["version"]}
        record = {"version": version, "payload": payload, "stored_at": time.time()}
        _contexts[key] = record
        return {"stale": False, "record": record}


def get_context(scope: str, context_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a stored context, or None."""
    key = _key(scope, context_id)
    with _lock:
        entry = _contexts.get(key)
        return entry["payload"] if entry else None


def get_context_version(scope: str, context_id: str) -> Optional[int]:
    key = _key(scope, context_id)
    with _lock:
        entry = _contexts.get(key)
        return entry["version"] if entry else None


def get_all_contexts() -> Dict[str, Dict[str, Any]]:
    """Return all stored contexts grouped by scope."""
    grouped: Dict[str, Dict[str, Any]] = defaultdict(dict)
    with _lock:
        for (scope, cid), entry in _contexts.items():
            grouped[scope][cid] = entry["payload"]
    return grouped


def get_context_counts() -> Dict[str, int]:
    with _lock:
        counts: Dict[str, int] = defaultdict(int)
        for (scope, _) in _contexts:
            counts[scope] += 1
        return dict(counts)


def store_conversation(conversation_id: str, turn: Dict[str, Any]) -> None:
    with _lock:
        _conversations.setdefault(conversation_id, []).append(turn)


def get_conversation(conversation_id: str) -> list:
    with _lock:
        return list(_conversations.get(conversation_id, []))


def is_suppressed(suppression_key: str, ttl_seconds: int = 86400) -> bool:
    """Check if a suppression key is active (within TTL)."""
    with _lock:
        ts = _sent_suppressions.get(suppression_key)
        if ts is None:
            return False
        if time.time() - ts > ttl_seconds:
            del _sent_suppressions[suppression_key]
            return False
        return True


def mark_suppressed(suppression_key: str) -> None:
    with _lock:
        _sent_suppressions[suppression_key] = time.time()


def uptime_seconds() -> int:
    return int(time.time() - _start_time)
