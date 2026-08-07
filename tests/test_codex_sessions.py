import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from local_proxy.codex_sessions import CodexSessionNameIndex


class CodexSessionNameIndexTests(unittest.TestCase):
    def test_latest_name_wins_and_malformed_rows_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session_index.jsonl"
            records = [
                {"id": "thread-one", "thread_name": "旧名称"},
                {"id": "thread-two", "thread_name": "另一个会话"},
                {"id": "thread-one", "thread_name": "当前名称"},
            ]
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in records)
                + "\n{unfinished",
                encoding="utf-8",
            )
            index = CodexSessionNameIndex(path)

            self.assertEqual(
                index.resolve(("thread-one", "missing")),
                {"thread-one": "当前名称"},
            )

    def test_missing_index_returns_no_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            index = CodexSessionNameIndex(Path(temporary_directory) / "missing.jsonl")

            self.assertEqual(index.resolve(("thread-one",)), {})

    def test_recent_filters_to_seven_day_window_and_resolves_private_key(self) -> None:
        now = time.time()

        def timestamp(offset: float) -> str:
            return datetime.fromtimestamp(now + offset, timezone.utc).isoformat()

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session_index.jsonl"
            records = [
                {"id": "thread-old", "thread_name": "旧会话", "updated_at": timestamp(-8 * 24 * 3600)},
                {"id": "thread-recent", "thread_name": "最近会话", "updated_at": timestamp(-3600)},
            ]
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
                encoding="utf-8",
            )
            index = CodexSessionNameIndex(path)

            recent = index.recent(now - 7 * 24 * 3600)
            self.assertEqual([item["name"] for item in recent], ["最近会话"])
            session_key = hashlib.sha256(b"thread-recent").hexdigest()[:24]
            self.assertEqual(index.thread_id_for_session_key(session_key), "thread-recent")
            self.assertIsNone(index.thread_id_for_session_key("not-a-session-key"))


if __name__ == "__main__":
    unittest.main()
