from __future__ import annotations

import json
import hashlib
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def default_session_index_path() -> Path:
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    return codex_home / "session_index.jsonl"


def _session_key(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:24]


def _updated_at_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class CodexSessionNameIndex:
    """Resolve Codex thread IDs without reading conversation transcripts."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_session_index_path()
        self._lock = threading.RLock()
        self._stamp: tuple[int, int] | None = None
        self._names: dict[str, str] = {}
        self._updated_at: dict[str, float] = {}
        self._thread_ids_by_key: dict[str, str] = {}

    def resolve(self, thread_ids: Iterable[str]) -> dict[str, str]:
        requested = {
            thread_id
            for thread_id in thread_ids
            if isinstance(thread_id, str) and thread_id
        }
        if not requested:
            return {}
        with self._lock:
            self._refresh()
            return {
                thread_id: self._names[thread_id]
                for thread_id in requested
                if thread_id in self._names
            }

    def recent(self, since: float) -> tuple[dict[str, Any], ...]:
        cutoff = float(since)
        with self._lock:
            self._refresh()
            sessions = [
                {
                    "thread_id": thread_id,
                    "name": name,
                    "updated_at": self._updated_at[thread_id],
                }
                for thread_id, name in self._names.items()
                if self._updated_at.get(thread_id, 0.0) >= cutoff
            ]
        sessions.sort(key=lambda item: item["updated_at"], reverse=True)
        return tuple(sessions)

    def thread_id_for_session_key(self, session_key: str) -> str | None:
        with self._lock:
            self._refresh()
            return self._thread_ids_by_key.get(session_key)

    def _refresh(self) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            self._stamp = None
            self._names = {}
            self._updated_at = {}
            self._thread_ids_by_key = {}
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return

        names: dict[str, str] = {}
        updated_at: dict[str, float] = {}
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    thread_id = record.get("id")
                    thread_name = record.get("thread_name")
                    if not isinstance(thread_id, str) or not thread_id:
                        continue
                    if not isinstance(thread_name, str) or not thread_name.strip():
                        continue
                    names[thread_id] = thread_name.strip()
                    timestamp = _updated_at_timestamp(record.get("updated_at"))
                    if timestamp is None:
                        updated_at.pop(thread_id, None)
                    else:
                        updated_at[thread_id] = timestamp
        except OSError:
            self._stamp = None
            self._names = {}
            self._updated_at = {}
            self._thread_ids_by_key = {}
            return
        self._names = names
        self._updated_at = updated_at
        self._thread_ids_by_key = {
            _session_key(thread_id): thread_id for thread_id in names
        }
        self._stamp = stamp
