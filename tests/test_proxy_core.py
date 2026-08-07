import json
import sqlite3
import socket
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from local_proxy.core import (
    HealthStatusUrlStore,
    LocalProxyServer,
    ProviderRouter,
    ProxyProvider,
    RecoveryHistoryStore,
    RetryPolicy,
    RetryPolicyStore,
    TokenUsage,
    UsageCapture,
    UsageStore,
    _codex_thread_id,
    _public_control_status,
    create_proxy_app,
    filter_self_referencing_providers,
    order_proxy_providers,
)
from local_proxy.codex import load_proxy_providers
from local_proxy.protocols.claude_messages import ClaudeMessagesProtocol


async def _empty_wait() -> None:
    return None


def provider(
    provider_id: str,
    *,
    current: bool = False,
    api_key: str | None = "test-upstream-credential",
) -> ProxyProvider:
    return ProxyProvider(
        provider_id=provider_id,
        name=provider_id.title(),
        base_url=f"https://{provider_id}.example.test/v1",
        is_cc_switch_current=current,
        api_key=api_key,
    )


class ProviderRouterTests(unittest.TestCase):
    def test_active_request_exposes_resolved_name_without_thread_id(self) -> None:
        router = ProviderRouter((provider("first", current=True),))
        thread_id = "019fa83f-2a11-73b0-a862-4d51679219ef"
        request = router.begin_request(thread_id=thread_id)

        payload = _public_control_status(
            router,
            session_name_resolver=lambda requested: {
                item: "Codex服务可用检测" for item in requested
            },
        )

        self.assertEqual(
            payload["providers"][0]["active_sessions"],
            [{"name": "Codex服务可用检测"}],
        )
        self.assertNotIn(thread_id, json.dumps(payload, ensure_ascii=False))
        router.finish_request(request, status_code=200)
        self.assertEqual(router.status().active_request_details, ())

    def test_codex_thread_id_reads_bounded_json_metadata(self) -> None:
        thread_id = "019fa83f-2a11-73b0-a862-4d51679219ef"
        headers = {
            "x-codex-turn-metadata": json.dumps(
                {"thread_id": thread_id, "turn_id": "turn-fixture"}
            )
        }

        self.assertEqual(_codex_thread_id(headers), thread_id)
        self.assertIsNone(_codex_thread_id({"x-codex-turn-metadata": "not-json"}))

    def test_switch_affects_new_requests_without_moving_active_request(self) -> None:
        router = ProviderRouter((provider("first", current=True), provider("second")))

        first_request = router.begin_request()
        router.select("second")
        second_request = router.begin_request()

        self.assertEqual(first_request.provider.provider_id, "first")
        self.assertEqual(second_request.provider.provider_id, "second")
        self.assertEqual(router.status().active_by_provider, {"first": 1, "second": 1})
        router.finish_request(first_request, status_code=200)
        self.assertEqual(router.status().active_by_provider, {"second": 1})

    def test_retry_can_move_active_request_to_current_provider(self) -> None:
        router = ProviderRouter((provider("first", current=True), provider("second")))
        request = router.begin_request()
        router.record_retry(
            request,
            attempt=2,
            max_attempts=-1,
            delay_seconds=2,
            kind="http_503",
        )

        router.select("second")
        rerouted, changed = router.route_retry_to_current(request)

        self.assertTrue(changed)
        self.assertEqual(rerouted.request_id, request.request_id)
        self.assertEqual(rerouted.provider.provider_id, "second")
        status = router.status()
        self.assertEqual(status.active_by_provider, {"second": 1})
        self.assertEqual(
            status.retrying_by_request[request.request_id].provider_id,
            "second",
        )
        router.record_retry(
            rerouted,
            attempt=3,
            max_attempts=-1,
            delay_seconds=0,
            kind="http_503",
            error_provider_id="first",
        )
        self.assertEqual(router.status().recent_retry_errors[0].provider_id, "first")
        router.finish_request(rerouted, status_code=200)
        self.assertEqual(router.status().active_by_provider, {})

    def test_session_override_routes_new_requests_and_retries_to_fixed_provider(self) -> None:
        router = ProviderRouter(
            (provider("first", current=True), provider("second")),
            session_provider_overrides={"thread-a": "second"},
        )

        request = router.begin_request(thread_id="thread-a")
        router.select("first")
        rerouted, changed = router.route_retry_to_current(request)

        self.assertEqual(request.provider.provider_id, "second")
        self.assertFalse(changed)
        self.assertEqual(rerouted.provider.provider_id, "second")
        router.finish_request(request, status_code=200)
        router.set_session_provider_override("thread-a", None)
        following = router.begin_request(thread_id="thread-a")
        self.assertEqual(following.provider.provider_id, "first")
        router.finish_request(following, status_code=200)

    def test_refresh_preserves_selection_and_falls_back_safely(self) -> None:
        router = ProviderRouter((provider("first"), provider("second", current=True)))
        router.select("first")

        router.replace_providers((provider("first"), provider("third")))
        self.assertEqual(router.current_provider().provider_id, "first")

        router.replace_providers((provider("third", current=True),))
        self.assertEqual(router.current_provider().provider_id, "third")

    def test_provider_repr_never_contains_upstream_credential(self) -> None:
        upstream = provider("private", api_key="credential-that-must-not-appear")

        self.assertNotIn("credential-that-must-not-appear", repr(upstream))

    def test_concurrent_retries_are_tracked_per_request(self) -> None:
        router = ProviderRouter((provider("same", current=True),))
        first = router.begin_request()
        second = router.begin_request()

        router.record_retry(first, attempt=2, max_attempts=-1, delay_seconds=1, kind="connection")
        router.record_retry(second, attempt=4, max_attempts=-1, delay_seconds=4, kind="http_503")

        status = router.status()
        self.assertEqual(len(status.retrying_by_request), 2)
        self.assertEqual({item.attempt for item in status.retrying_by_request.values()}, {2, 4})
        self.assertEqual(len(status.recent_retry_errors), 2)
        self.assertEqual(status.recent_retry_errors[0].attempt, 3)
        router.finish_request(first, status_code=200)
        self.assertEqual(len(router.status().retrying_by_request), 1)
        router.finish_request(second, status_code=200)

    def test_retry_history_redacts_sensitive_error_details(self) -> None:
        router = ProviderRouter((provider("selected", current=True),))
        request = router.begin_request()

        router.record_retry(
            request,
            attempt=2,
            max_attempts=4,
            delay_seconds=1,
            kind="http_503",
            error_summary='"Authorization": "Bearer fixture-private-token"',
        )

        status = router.status()
        self.assertEqual(len(status.recent_retry_errors), 1)
        self.assertIn("[已隐藏]", status.recent_retry_errors[0].summary)
        self.assertNotIn("fixture-private-token", status.recent_retry_errors[0].summary)

        for attempt in range(3, 9):
            router.record_retry(
                request,
                attempt=attempt,
                max_attempts=-1,
                delay_seconds=1,
                kind="connection",
            )
        self.assertEqual(len(router.status().recent_retry_errors), 5)

    def test_local_order_is_stable_and_self_provider_is_removed(self) -> None:
        first = provider("first")
        second = provider("second")
        loop = ProxyProvider(
            provider_id="local-loop",
            name="Codex 本地中转",
            base_url="http://localhost:17890/v1",
            is_cc_switch_current=False,
            api_key="placeholder",
        )

        filtered = filter_self_referencing_providers((first, loop, second), 17890)
        ordered = order_proxy_providers(filtered, ("second", "stale"))

        self.assertEqual([item.provider_id for item in ordered], ["second", "first"])


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_context.cleanup)
        self.store = UsageStore(Path(self.temp_context.name) / "usage.sqlite3")

    def test_creates_missing_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "missing" / "nested" / "usage.sqlite3"

            store = UsageStore(database)

            self.assertTrue(database.is_file())
            self.assertEqual(store.summary("today")["total"]["request_count"], 0)

    def test_upstream_usage_wins_over_local_estimate(self) -> None:
        capture = UsageCapture(
            b'{"model":"gpt-5","input":"this would be estimated"}',
            "responses",
        )
        capture.feed(
            b'data: {"type":"response.completed","response":{"usage":'
            b'{"input_tokens":101,"output_tokens":23,"total_tokens":124,'
            b'"input_tokens_details":{"cached_tokens":40},'
            b'"output_tokens_details":{"reasoning_tokens":7}}}}\n\n'
        )

        usage = capture.finalize(200)

        self.assertEqual(
            usage,
            TokenUsage(101, 23, 124, 40, 7, source="upstream"),
        )

    def test_missing_usage_estimates_input_and_streamed_output(self) -> None:
        capture = UsageCapture(
            b'{"model":"gpt-5","instructions":"be concise",'
            b'"input":[{"type":"message","content":'
            b'[{"type":"input_text","text":"hello world"}]}]}',
            "responses",
        )
        capture.feed(
            b'data: {"type":"response.output_text.delta","delta":"hello back"}\n\n'
        )

        usage = capture.finalize(200)

        self.assertIsNotNone(usage)
        self.assertEqual(usage.source, "estimated")
        self.assertGreater(usage.input_tokens, 0)
        self.assertGreater(usage.output_tokens, 0)
        self.assertEqual(usage.total_tokens, usage.input_tokens + usage.output_tokens)

    def test_sqlite_summary_uses_exact_168_hour_window(self) -> None:
        now = 2_000_000.0
        self.store.record(
            provider_id="inside",
            model="gpt-5",
            usage=TokenUsage(10, 5, 15, source="upstream"),
            status_code=200,
            recorded_at=now - 7 * 24 * 3600 + 1,
        )
        self.store.record(
            provider_id="outside",
            model="gpt-5",
            usage=TokenUsage(100, 50, 150, source="estimated"),
            status_code=200,
            recorded_at=now - 7 * 24 * 3600 - 1,
        )

        summary = self.store.summary("7d", now=now)

        self.assertEqual(summary["total"]["total_tokens"], 15)
        self.assertEqual(summary["total"]["request_count"], 1)
        self.assertEqual(set(summary["by_provider"]), {"inside"})
        self.assertEqual(
            summary["by_provider"]["inside"]["last_success_at"],
            round((now - 7 * 24 * 3600 + 1) * 1000),
        )

    def test_request_history_includes_failures_and_paginates(self) -> None:
        now = 2_000_000.0
        records = (
            (now - 3, 200, TokenUsage(10, 2, 12, 4, 1, source="upstream")),
            (now - 2, 503, TokenUsage(20, 3, 23, source="upstream")),
            (
                now - 1,
                200,
                TokenUsage(
                    30,
                    4,
                    34,
                    source="estimated",
                    estimate_method="fixture-estimator",
                ),
            ),
        )
        for recorded_at, status_code, usage in records:
            self.store.record(
                provider_id="provider-a",
                model="gpt-5.6-sol",
                usage=usage,
                status_code=status_code,
                recorded_at=recorded_at,
            )
        self.store.record(
            provider_id="provider-a",
            model="gpt-5.6-sol",
            usage=TokenUsage(90, 9, 99),
            status_code=200,
            successful=False,
            recorded_at=now - 0.5,
        )
        self.store.record(
            provider_id="provider-b",
            model="other-model",
            usage=TokenUsage(100, 20, 120),
            status_code=200,
            recorded_at=now,
        )

        first = self.store.history(
            provider_id="provider-a",
            window="all",
            limit=1,
            now=now,
        )
        second = self.store.history(
            provider_id="provider-a",
            window="all",
            cursor=first["next_cursor"],
            limit=2,
            now=now,
        )

        self.assertEqual(first["total_count"], 4)
        self.assertEqual(first["total"]["total_tokens"], 168)
        self.assertEqual(first["total"]["successful_requests"], 2)
        self.assertEqual(first["total"]["failed_requests"], 2)
        self.assertEqual(first["total"]["successful_tokens"], 46)
        self.assertEqual(first["total"]["failed_tokens"], 122)
        self.assertEqual(first["items"][0]["total_tokens"], 99)
        self.assertFalse(first["items"][0]["succeeded"])
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(second["items"][0]["total_tokens"], 34)
        self.assertTrue(second["items"][0]["succeeded"])
        self.assertEqual(second["items"][0]["usage_source"], "estimated")
        self.assertEqual(second["items"][0]["estimate_method"], "fixture-estimator")
        self.assertEqual(second["items"][1]["status_code"], 503)
        self.assertFalse(second["items"][1]["succeeded"])
        third = self.store.history(
            provider_id="provider-a",
            window="all",
            cursor=second["next_cursor"],
            limit=2,
            now=now,
        )
        self.assertEqual(third["items"][0]["total_tokens"], 12)
        self.assertTrue(third["items"][0]["succeeded"])
        self.assertIsNone(third["next_cursor"])
        with self.assertRaisesRegex(ValueError, "游标"):
            self.store.history(
                provider_id="provider-a",
                window="all",
                cursor="invalid",
                now=now,
            )

    def test_existing_usage_database_adds_success_marker(self) -> None:
        old_path = Path(self.temp_context.name) / "old-usage.sqlite3"
        now = time.time()
        with closing(sqlite3.connect(old_path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE request_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at REAL NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL,
                    reasoning_tokens INTEGER NOT NULL,
                    usage_source TEXT NOT NULL,
                    estimate_method TEXT,
                    status_code INTEGER NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO request_usage (
                    recorded_at, provider_id, model, input_tokens, output_tokens,
                    total_tokens, cached_tokens, reasoning_tokens, usage_source,
                    estimate_method, status_code
                ) VALUES (?, 'provider-a', 'gpt-5.6-sol', 10, 2, 12, 0, 0,
                          'upstream', NULL, 200)
                """,
                (now,),
            )

        migrated = UsageStore(old_path)
        history = migrated.history(provider_id="provider-a", window="all", now=now)

        self.assertEqual(history["total_count"], 1)
        with closing(sqlite3.connect(old_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(request_usage)")
            }
            succeeded = connection.execute(
                "SELECT succeeded FROM request_usage"
            ).fetchone()[0]
        self.assertIn("succeeded", columns)
        self.assertEqual(succeeded, 1)

    def test_usage_database_never_stores_request_or_response_content(self) -> None:
        self.store.record(
            provider_id="provider-a",
            model="gpt-5",
            usage=TokenUsage(1, 2, 3),
            status_code=200,
        )
        connection = sqlite3.connect(self.store.path)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(request_usage)")
            }
        finally:
            connection.close()

        self.assertNotIn("request_body", columns)
        self.assertNotIn("response_body", columns)
        self.assertNotIn("api_key", columns)

    def test_local_request_history_keeps_24_hours_without_duplicating_usage(self) -> None:
        now = 2_000_000.0
        usage = TokenUsage(12, 3, 15, cached_tokens=4)
        usage_id = self.store.record(
            provider_id="provider-a",
            model="gpt-5.6-sol",
            usage=usage,
            status_code=200,
            recorded_at=now,
            successful=False,
        )
        self.store.record_request(
            started_at=now - 4,
            finished_at=now,
            provider_id="provider-a",
            thread_id="thread-fixture",
            session_name="Codex 服务可用检测",
            model="gpt-5.6-sol",
            status_code=200,
            successful=False,
            outcome="failed",
            retry_count=2,
            error_kind="stream_interrupted",
            error_summary='Authorization: Bearer fixture-private-token',
            usage=usage,
            usage_id=usage_id,
        )

        history = self.store.request_history(
            window="24h",
            status="failed",
            query="Codex 服务",
            now=now + 1,
        )

        self.assertEqual(history["total_count"], 1)
        self.assertEqual(history["items"][0]["retry_count"], 2)
        self.assertEqual(history["items"][0]["total_tokens"], 15)
        self.assertNotIn("fixture-private-token", history["items"][0]["error_summary"])
        with closing(sqlite3.connect(self.store.path)) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(request_history)")
            }
        self.assertNotIn("request_body", columns)


class RecoveryHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_context.cleanup)
        self.path = Path(self.temp_context.name) / "usage.sqlite3"
        self.store = RecoveryHistoryStore(self.path)

    def test_history_persists_only_recent_24_hours_and_sanitizes_summary(self) -> None:
        now = time.time()
        self.store.record(
            request_id=1,
            provider_id="expired",
            attempt=1,
            max_attempts=4,
            delay_seconds=1,
            kind="connection",
            summary="expired",
            stage="before_output",
            outcome="retrying",
            recorded_at=now - 25 * 3600,
        )
        self.store.record(
            request_id=2,
            provider_id="provider-a",
            attempt=2,
            max_attempts=-1,
            delay_seconds=2,
            kind="model_capacity",
            summary='Authorization: Bearer fixture-private-token',
            stage="before_output",
            outcome="retrying",
            recorded_at=now - 2,
            request_started_at=now - 120,
        )
        self.store.record(
            request_id=3,
            provider_id="provider-b",
            attempt=3,
            max_attempts=4,
            delay_seconds=None,
            kind="stream_interrupted",
            summary="stream disconnected",
            stage="after_output",
            outcome="passed_through",
            recorded_at=now - 1,
        )

        reopened = RecoveryHistoryStore(self.path)
        history = reopened.history(now=now)

        self.assertEqual(history["window_hours"], 24)
        self.assertEqual(history["total_count"], 2)
        self.assertFalse(history["truncated"])
        self.assertEqual(
            [item["provider_id"] for item in history["items"]],
            ["provider-b", "provider-a"],
        )
        self.assertEqual(history["items"][0]["stage"], "after_output")
        self.assertIsNone(history["items"][0]["request_started_at"])
        self.assertEqual(
            history["items"][1]["request_started_at"],
            round((now - 120) * 1000),
        )
        self.assertIn("[已隐藏]", history["items"][1]["summary"])
        self.assertNotIn("fixture-private-token", str(history))

        with closing(sqlite3.connect(self.path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(recovery_events)")
            }
            stored_count = connection.execute(
                "SELECT COUNT(*) FROM recovery_events"
            ).fetchone()[0]
        self.assertEqual(stored_count, 2)
        self.assertIn("request_started_at", columns)
        self.assertNotIn("request_body", columns)
        self.assertNotIn("response_body", columns)
        self.assertNotIn("api_key", columns)

    def test_existing_database_is_migrated_without_inventing_start_times(self) -> None:
        old_path = Path(self.temp_context.name) / "old-recovery.sqlite3"
        now = time.time()
        with closing(sqlite3.connect(old_path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE recovery_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at REAL NOT NULL,
                    request_id INTEGER NOT NULL,
                    provider_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    delay_seconds REAL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    outcome TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO recovery_events (
                    recorded_at, request_id, provider_id, attempt, max_attempts,
                    delay_seconds, kind, summary, stage, outcome
                ) VALUES (?, 1, 'provider-a', 1, 4, 1, 'connection',
                          'temporary failure', 'before_output', 'retrying')
                """,
                (now,),
            )

        migrated = RecoveryHistoryStore(old_path)
        history = migrated.history(now=now + 1)

        self.assertEqual(history["total_count"], 1)
        self.assertIsNone(history["items"][0]["request_started_at"])
        with closing(sqlite3.connect(old_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(recovery_events)")
            }
        self.assertIn("request_started_at", columns)

    def test_history_uses_cursor_pagination(self) -> None:
        now = time.time()
        for request_id in range(1, 4):
            self.store.record(
                request_id=request_id,
                provider_id="provider-a",
                attempt=request_id,
                max_attempts=4,
                delay_seconds=1,
                kind="connection",
                summary=f"failure {request_id}",
                stage="before_output",
                outcome="retrying",
                recorded_at=now - (3 - request_id),
            )

        first = self.store.history(now=now, limit=2)
        second = self.store.history(
            now=now,
            limit=2,
            cursor=first["next_cursor"],
        )

        self.assertEqual(first["total_count"], 3)
        self.assertEqual(
            [item["request_id"] for item in first["items"]],
            [3, 2],
        )
        self.assertTrue(first["truncated"])
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(
            [item["request_id"] for item in second["items"]],
            [1],
        )
        self.assertFalse(second["truncated"])
        self.assertIsNone(second["next_cursor"])
        with self.assertRaisesRegex(ValueError, "游标"):
            self.store.history(now=now, cursor="invalid")


class CCSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_context.cleanup)
        self.database = Path(self.temp_context.name) / "cc-switch.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                is_current INTEGER NOT NULL,
                settings_config TEXT NOT NULL,
                meta TEXT,
                app_type TEXT NOT NULL,
                sort_index INTEGER,
                created_at TEXT
            );
            CREATE TABLE provider_endpoints (
                provider_id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                url TEXT
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        payload = {
            "config": """
model_provider = "custom"
[model_providers.custom]
base_url = "https://upstream.example.test/v1/"
env_key = "UPSTREAM_KEY"
wire_api = "responses"
[model_providers.custom.http_headers]
X-Client = "codex-local-proxy"
[model_providers.custom.env_http_headers]
X-Extra-Auth = "EXTRA_AUTH"
[model_providers.custom.query_params]
api-version = "2026-07-01"
""",
            "auth": {
                "UPSTREAM_KEY": "fixture-primary-credential",
                "EXTRA_AUTH": "fixture-extra-credential",
            },
        }
        connection.execute(
            """INSERT INTO providers
               (id, name, is_current, settings_config, meta, app_type, sort_index, created_at)
               VALUES (?, ?, 1, ?, '{}', 'codex', 1, '2026-07-27')""",
            ("fixture", "Fixture", json.dumps(payload)),
        )
        connection.execute(
            "INSERT INTO provider_endpoints VALUES (?, 'codex', ?)",
            ("fixture", "https://fallback.example.test/v1"),
        )
        connection.execute(
            "INSERT INTO settings VALUES ('common_config_codex', '')"
        )
        connection.commit()
        connection.close()

    def test_loads_effective_provider_from_read_only_database(self) -> None:
        providers = load_proxy_providers(self.database)

        self.assertEqual(len(providers), 1)
        loaded = providers[0]
        self.assertEqual(loaded.name, "Fixture")
        self.assertEqual(loaded.base_url, "https://upstream.example.test/v1")
        self.assertTrue(loaded.has_credentials)
        self.assertEqual(loaded.configured_headers["X-Client"], "codex-local-proxy")
        self.assertIn("X-Extra-Auth", loaded.configured_headers)
        self.assertEqual(loaded.default_query, {"api-version": "2026-07-01"})


class ProxyAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_api_hides_thread_id_and_persists_session_route(self) -> None:
        seen_hosts: list[str] = []

        async def upstream(request: httpx.Request) -> httpx.Response:
            seen_hosts.append(request.url.host)
            return httpx.Response(
                200,
                json={
                    "id": "response-fixture",
                    "output": [],
                    "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                },
            )

        thread_id = "019fa83f-2a11-73b0-a862-4d51679219ef"
        metadata = json.dumps({"thread_id": thread_id})
        with tempfile.TemporaryDirectory() as temp_dir:
            usage_store = UsageStore(Path(temp_dir) / "usage.sqlite3")
            router = ProviderRouter((provider("first", current=True), provider("second")))
            upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            app = create_proxy_app(
                router,
                client=upstream_client,
                usage_store=usage_store,
                session_name_resolver=lambda requested: {
                    item: "请求列表测试" for item in requested
                },
            )
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )
            try:
                first = await client.post(
                    "/v1/responses",
                    headers={"x-codex-turn-metadata": metadata},
                    json={"model": "gpt-test", "input": "hello"},
                )
                requests = await client.get("/control/api/requests")
                item = requests.json()["items"][0]
                routed = await client.post(
                    f"/control/api/session-routes/{item['session_key']}",
                    headers={**{"X-Local-Proxy-Control": "1"}},
                    json={"provider_id": "second"},
                )
                second = await client.post(
                    "/v1/responses",
                    headers={"x-codex-turn-metadata": metadata},
                    json={"model": "gpt-test", "input": "again"},
                )
                history = await client.get("/control/api/requests")
            finally:
                await client.aclose()
                await upstream_client.aclose()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(routed.status_code, 200)
        self.assertEqual(seen_hosts, ["first.example.test", "second.example.test"])
        self.assertEqual(item["session_name"], "请求列表测试")
        self.assertEqual(item["model"], "gpt-test")
        self.assertEqual(item["total_tokens"], 6)
        self.assertNotIn(thread_id, requests.text)
        self.assertEqual(
            [entry["provider_name"] for entry in history.json()["items"]],
            ["Second", "First"],
        )
        self.assertEqual(
            {entry["route_provider_id"] for entry in history.json()["items"]},
            {"second"},
        )

    async def test_sessions_api_lists_active_recent_sessions_without_thread_ids(self) -> None:
        active_thread = "thread-active"
        recent_thread = "thread-recent"
        catalog_since: list[float] = []
        now = time.time()

        def session_catalog(since: float):
            catalog_since.append(since)
            return (
                {
                    "thread_id": recent_thread,
                    "name": "最近会话",
                    "updated_at": now - 3600,
                },
            )

        router = ProviderRouter((provider("first", current=True), provider("second")))
        router.set_session_provider_override(active_thread, "second")
        active_request = router.begin_request(thread_id=active_thread)
        with tempfile.TemporaryDirectory() as temp_dir:
            usage_store = UsageStore(Path(temp_dir) / "usage.sqlite3")
            upstream_client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200))
            )
            app = create_proxy_app(
                router,
                client=upstream_client,
                usage_store=usage_store,
                session_name_resolver=lambda requested: {
                    item: "当前活动会话" for item in requested if item == active_thread
                },
                session_catalog=session_catalog,
            )
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )
            try:
                response = await client.get("/control/api/sessions")
            finally:
                await client.aclose()
                await upstream_client.aclose()
                router.finish_request(active_request, status_code=200)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["window_days"], 7)
        self.assertGreaterEqual(catalog_since[0], now - 7 * 24 * 3600 - 2)
        self.assertEqual(
            [item["name"] for item in payload["items"]],
            ["当前活动会话", "最近会话"],
        )
        self.assertTrue(payload["items"][0]["active"])
        self.assertEqual(payload["items"][0]["route_provider_id"], "second")
        self.assertTrue(all(len(item["session_key"]) == 24 for item in payload["items"]))
        self.assertNotIn(active_thread, response.text)
        self.assertNotIn(recent_thread, response.text)

    async def test_protocol_adapter_replaces_claude_placeholder_auth(self) -> None:
        seen: list[httpx.Request] = []

        async def upstream(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={"type": "message", "usage": {"input_tokens": 1, "output_tokens": 1}},
            )

        selected = provider("selected", current=True)
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((selected,)),
            client=upstream_client,
            protocol_adapter=ClaudeMessagesProtocol(),
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        try:
            response = await client.post(
                "/v1/messages",
                headers={
                    "x-api-key": "local-placeholder",
                    "anthropic-version": "2023-06-01",
                },
                json={"model": "claude-test", "messages": []},
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen[0].url.path, "/v1/messages")
        self.assertEqual(seen[0].headers["x-api-key"], "test-upstream-credential")
        self.assertNotIn("authorization", seen[0].headers)

    async def test_claude_protocol_retries_http_529(self) -> None:
        attempts = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(529, json={"error": {"type": "overloaded_error"}})
            return httpx.Response(200, json={"type": "message", "content": []})

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            protocol_adapter=ClaudeMessagesProtocol(),
            retry_policy=RetryPolicy(delay_seconds=0.1),
            retry_sleep=lambda _: _empty_wait(),
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        try:
            response = await client.post(
                "/v1/messages",
                json={"model": "claude-test", "messages": []},
            )
        finally:
            await client.aclose()
            await upstream_client.aclose()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 2)

    async def test_stream_usage_is_persisted_for_final_provider(self) -> None:
        class UsageStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                yield (
                    b'data: {"type":"response.completed","response":{"usage":'
                    b'{"input_tokens":12,"output_tokens":3,"total_tokens":15}}}\n\n'
                )

        async def upstream(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=UsageStream(),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            usage_store = UsageStore(Path(temp_dir) / "usage.sqlite3")
            upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            app = create_proxy_app(
                ProviderRouter((provider("selected", current=True),)),
                client=upstream_client,
                usage_store=usage_store,
            )
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            )
            try:
                response = await client.post(
                    "/v1/responses",
                    json={"model": "gpt-5", "input": "hello"},
                )
                status = (
                    await client.get("/control/api/status?usage_window=all")
                ).json()
            finally:
                await client.aclose()
                await upstream_client.aclose()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(status["usage"]["total"]["total_tokens"], 15)
        self.assertEqual(status["usage"]["total"]["estimated_requests"], 0)
        self.assertEqual(
            status["usage"]["by_provider"]["selected"]["input_tokens"], 12
        )
        self.assertIsNotNone(
            status["usage"]["by_provider"]["selected"]["last_success_at"]
        )

    async def test_usage_history_endpoint_returns_only_selected_provider(self) -> None:
        temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(temp_context.cleanup)
        usage_store = UsageStore(Path(temp_context.name) / "usage.sqlite3")
        usage_store.record(
            provider_id="selected",
            model="gpt-5.6-sol",
            usage=TokenUsage(12, 3, 15, cached_tokens=8),
            status_code=200,
        )
        usage_store.record(
            provider_id="selected",
            model="gpt-5.6-sol",
            usage=TokenUsage(18, 2, 20, cached_tokens=12),
            status_code=200,
            successful=False,
        )
        usage_store.record(
            provider_id="other",
            model="gpt-5.6-sol",
            usage=TokenUsage(20, 5, 25),
            status_code=200,
        )
        upstream_client = httpx.AsyncClient()
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True), provider("other"))),
            client=upstream_client,
            usage_store=usage_store,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.get(
            "/control/api/usage-history",
            params={"provider_id": "selected", "usage_window": "all"},
        )
        missing = await client.get(
            "/control/api/usage-history",
            params={"provider_id": "missing", "usage_window": "all"},
        )
        invalid_cursor = await client.get(
            "/control/api/usage-history",
            params={
                "provider_id": "selected",
                "usage_window": "all",
                "cursor": "invalid",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["total_count"], 2)
        self.assertEqual(response.json()["items"][0]["total_tokens"], 20)
        self.assertEqual(response.json()["items"][0]["cached_tokens"], 12)
        self.assertFalse(response.json()["items"][0]["succeeded"])
        self.assertEqual(response.json()["items"][1]["total_tokens"], 15)
        self.assertTrue(response.json()["items"][1]["succeeded"])
        self.assertEqual(response.json()["total"]["total_tokens"], 35)
        self.assertEqual(response.json()["total"]["successful_tokens"], 15)
        self.assertEqual(response.json()["total"]["failed_tokens"], 20)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(invalid_cursor.status_code, 422)

    async def test_status_summarizes_history_and_detail_endpoint_returns_all(self) -> None:
        temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(temp_context.cleanup)
        history_store = RecoveryHistoryStore(
            Path(temp_context.name) / "usage.sqlite3"
        )
        now = time.time()
        for index in range(3):
            history_store.record(
                request_id=index + 1,
                provider_id="selected",
                attempt=index + 1,
                max_attempts=4,
                delay_seconds=1,
                kind="connection",
                summary=f"failure {index + 1}",
                stage="before_output",
                outcome="retrying",
                recorded_at=now - index,
            )
        upstream_client = httpx.AsyncClient()
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            recovery_history_store=history_store,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        status = (await client.get("/control/api/status")).json()
        detail_response = await client.get(
            "/control/api/recovery-history",
            params={"limit": 2},
        )
        detail = detail_response.json()
        older = (
            await client.get(
                "/control/api/recovery-history",
                params={"limit": 2, "cursor": detail["next_cursor"]},
            )
        ).json()

        self.assertEqual(status["retry"]["history"]["total_count"], 3)
        self.assertEqual(len(status["retry"]["history"]["items"]), 1)
        self.assertTrue(status["retry"]["history"]["truncated"])
        self.assertEqual(detail["total_count"], 3)
        self.assertEqual(len(detail["items"]), 2)
        self.assertTrue(detail["truncated"])
        self.assertIsNotNone(detail["next_cursor"])
        self.assertEqual(len(older["items"]), 1)
        self.assertFalse(older["truncated"])
        self.assertIsNone(older["next_cursor"])

    async def test_provider_visibility_and_order_control_api(self) -> None:
        hidden_changes: list[tuple[str, ...]] = []
        order_changes: list[tuple[str, ...]] = []
        router = ProviderRouter((provider("first", current=True), provider("second")))
        upstream_client = httpx.AsyncClient()
        app = create_proxy_app(
            router,
            client=upstream_client,
            on_hidden_provider_ids_changed=hidden_changes.append,
            on_provider_order_changed=order_changes.append,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)
        headers = {"X-Local-Proxy-Control": "1"}

        current_hidden = await client.post(
            "/control/api/providers/first/visibility",
            headers=headers,
            json={"hidden": True},
        )
        hidden = await client.post(
            "/control/api/providers/second/visibility",
            headers=headers,
            json={"hidden": True},
        )
        reordered = await client.post(
            "/control/api/providers/order",
            headers=headers,
            json={"provider_ids": ["second", "first"]},
        )

        self.assertEqual(current_hidden.status_code, 409)
        self.assertTrue(hidden.json()["providers"][1]["hidden"])
        self.assertEqual(
            [item["provider_id"] for item in reordered.json()["providers"]],
            ["second", "first"],
        )
        self.assertEqual(hidden_changes, [("second",)])
        self.assertEqual(order_changes, [("second", "first")])

    async def test_infinite_retry_mode_recovers_without_fixed_attempt_limit(self) -> None:
        attempts = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts <= 6:
                return httpx.Response(503, content=b"temporary")
            return httpx.Response(200, content=b"recovered")

        async def no_wait(_: float) -> None:
            return None

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            retry_policy=RetryPolicy(max_attempts=-1),
            retry_sleep=no_wait,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"recovered")
        self.assertEqual(attempts, 7)

    async def test_failed_retry_is_transparently_taken_over_by_new_provider(self) -> None:
        observed: list[dict[str, object]] = []
        first = ProxyProvider(
            provider_id="first",
            name="First",
            base_url="https://first.example.test/v1",
            is_cc_switch_current=True,
            api_key="first-upstream-key",
            configured_headers={"X-Provider-Route": "first"},
            default_query={"provider": "first"},
        )
        second = ProxyProvider(
            provider_id="second",
            name="Second",
            base_url="https://second.example.test/v1",
            is_cc_switch_current=False,
            api_key="second-upstream-key",
            configured_headers={"X-Provider-Route": "second"},
            default_query={"provider": "second"},
        )
        router = ProviderRouter((first, second))

        async def upstream(request: httpx.Request) -> httpx.Response:
            observed.append(
                {
                    "url": str(request.url),
                    "authorization": request.headers.get("authorization"),
                    "route": request.headers.get("x-provider-route"),
                    "body": await request.aread(),
                }
            )
            if request.url.host == "first.example.test":
                router.select("second")
                return httpx.Response(
                    503,
                    json={"error": {"message": "no available channel"}},
                )
            return httpx.Response(200, content=b"recovered by second")

        sleeps: list[float] = []

        async def no_wait(delay: float) -> None:
            sleeps.append(delay)

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            router,
            client=upstream_client,
            retry_policy=RetryPolicy(max_attempts=-1, delay_seconds=2, strategy="fixed"),
            retry_sleep=no_wait,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post(
            "/v1/responses?client=value",
            headers={"Authorization": "Bearer local-placeholder"},
            content=b'{"model":"test"}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"recovered by second")
        self.assertEqual(sleeps, [0.0])
        self.assertEqual(
            [item["url"] for item in observed],
            [
                "https://first.example.test/v1/responses?client=value&provider=first",
                "https://second.example.test/v1/responses?client=value&provider=second",
            ],
        )
        self.assertEqual(
            [item["authorization"] for item in observed],
            ["Bearer first-upstream-key", "Bearer second-upstream-key"],
        )
        self.assertEqual([item["route"] for item in observed], ["first", "second"])
        self.assertEqual(
            [item["body"] for item in observed],
            [b'{"model":"test"}', b'{"model":"test"}'],
        )
        status = router.status()
        self.assertEqual(status.active_by_provider, {})
        self.assertEqual(status.recent_retry_errors[0].provider_id, "first")

    async def test_disabled_retry_passes_upstream_error_through(self) -> None:
        attempts = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, content=b"upstream unavailable")

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            retry_policy=RetryPolicy(enabled=False),
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b"upstream unavailable")
        self.assertEqual(attempts, 1)

    async def test_retry_policy_control_api_validates_updates_and_hides_secrets(self) -> None:
        changed: list[RetryPolicy] = []
        store = RetryPolicyStore()
        upstream_client = httpx.AsyncClient()
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True, api_key="fixture-secret"),)),
            client=upstream_client,
            retry_policy_store=store,
            on_retry_policy_changed=changed.append,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)
        payload = {
            "enabled": True,
            "max_attempts": -1,
            "delay_seconds": 2,
            "strategy": "fixed",
            "max_delay_seconds": 30,
            "circuit_failure_threshold": 5,
            "circuit_cooldown_seconds": 60,
        }

        forbidden = await client.post("/control/api/retry-policy", json=payload)
        invalid = await client.post(
            "/control/api/retry-policy",
            headers={"X-Local-Proxy-Control": "1"},
            json={**payload, "max_attempts": 0},
        )
        updated = await client.post(
            "/control/api/retry-policy",
            headers={"X-Local-Proxy-Control": "1"},
            json=payload,
        )

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["retry"]["max_attempts"], -1)
        self.assertEqual(store.get().strategy, "fixed")
        self.assertEqual(changed, [store.get()])
        self.assertNotIn("fixture-secret", updated.text)

    async def test_runtime_settings_api_validates_updates_and_refreshes_health_url(self) -> None:
        runtime = {
            "configured_port": 17890,
            "active_port": 17890,
            "restart_required": False,
            "database_path": "~/.cc-switch/cc-switch.db",
            "health_status_url": None,
            "data_directory": "~/.codex-local-proxy",
            "codex_config_file": "~/.codex/config.toml",
        }
        changed: list[dict[str, object]] = []
        health_store = HealthStatusUrlStore()

        def snapshot() -> dict[str, object]:
            return dict(runtime)

        def update(payload: dict[str, object]) -> dict[str, object]:
            if payload.get("port") == 80:
                raise ValueError("端口无效")
            changed.append(dict(payload))
            runtime["configured_port"] = payload["port"]
            runtime["restart_required"] = payload["port"] != runtime["active_port"]
            runtime["database_path"] = payload["database_path"]
            runtime["health_status_url"] = payload["health_status_url"]
            health_store.replace(payload["health_status_url"])
            return snapshot()

        def validate_database(database_path: str) -> dict[str, object]:
            if database_path == "missing.db":
                raise ValueError("未找到数据库")
            return {
                "database_path": database_path,
                "provider_count": 2,
                "current_provider_configured": True,
            }

        upstream_client = httpx.AsyncClient()
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            health_status_url_store=health_store,
            runtime_settings_snapshot=snapshot,
            on_runtime_settings_changed=update,
            validate_runtime_database=validate_database,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)
        payload = {
            "port": 18888,
            "database_path": "~/.cc-switch/alternate.db",
            "health_status_url": "https://status.example.test/api/status",
        }

        current = await client.get("/control/api/runtime-settings")
        forbidden = await client.post("/control/api/runtime-settings", json=payload)
        invalid = await client.post(
            "/control/api/runtime-settings",
            headers={"X-Local-Proxy-Control": "1"},
            json={**payload, "port": 80},
        )
        invalid_database = await client.post(
            "/control/api/runtime-settings/validate-database",
            headers={"X-Local-Proxy-Control": "1"},
            json={"database_path": "missing.db"},
        )
        valid_database = await client.post(
            "/control/api/runtime-settings/validate-database",
            headers={"X-Local-Proxy-Control": "1"},
            json={"database_path": payload["database_path"]},
        )
        updated = await client.post(
            "/control/api/runtime-settings",
            headers={"X-Local-Proxy-Control": "1"},
            json=payload,
        )
        status = await client.get("/control/api/status")

        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.headers["cache-control"], "no-store")
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid_database.status_code, 422)
        self.assertEqual(valid_database.json()["provider_count"], 2)
        self.assertTrue(updated.json()["restart_required"])
        self.assertEqual(changed, [payload])
        self.assertEqual(
            status.json()["health_status_url"],
            "https://status.example.test/api/status",
        )

    async def test_retries_retryable_status_before_returning_response(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(
                    503,
                    json={"error": {"message": "temporary upstream overload"}},
                )
            return httpx.Response(200, content=b"recovered")

        async def no_wait(delay: float) -> None:
            sleeps.append(delay)

        router = ProviderRouter((provider("selected", current=True),))
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(router, client=upstream_client, retry_sleep=no_wait)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})
        status = (await client.get("/control/api/status")).json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"recovered")
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(status["retry"]["total_retries"], 2)
        self.assertEqual(status["retry"]["active"], [])
        self.assertTrue(
            all(
                item["request_started_at"] <= item["recorded_at"]
                for item in status["retry"]["recent_errors"]
            )
        )
        self.assertEqual(
            len(
                {
                    item["request_started_at"]
                    for item in status["retry"]["recent_errors"]
                }
            ),
            1,
        )
        self.assertEqual(
            [item["attempt"] for item in status["retry"]["recent_errors"]],
            [2, 1],
        )
        self.assertIn(
            "HTTP 503：temporary upstream overload",
            status["retry"]["recent_errors"][0]["summary"],
        )

    async def test_retries_nginx_html_404_before_returning_response(self) -> None:
        attempts = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    404,
                    headers={"content-type": "text/html; charset=utf-8"},
                    content=(
                        b"<html><head><title>404 Not Found</title></head>"
                        b"<body><h1>404 Not Found</h1><hr>"
                        b"<center>nginx</center></body></html>"
                    ),
                )
            return httpx.Response(200, content=b"recovered")

        async def no_wait(_: float) -> None:
            return None

        temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(temp_context.cleanup)
        history_store = RecoveryHistoryStore(
            Path(temp_context.name) / "recovery.sqlite3"
        )
        router = ProviderRouter((provider("selected", current=True),))
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            router,
            client=upstream_client,
            retry_policy=RetryPolicy(max_attempts=2),
            retry_sleep=no_wait,
            recovery_history_store=history_store,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})
        history = history_store.history()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"recovered")
        self.assertEqual(attempts, 2)
        self.assertEqual(router.status().last_retry_kind, "http_404")
        self.assertEqual(history["total_count"], 1)
        self.assertEqual(history["items"][0]["provider_id"], "selected")
        self.assertEqual(history["items"][0]["kind"], "http_404")
        self.assertEqual(history["items"][0]["stage"], "before_output")
        self.assertEqual(history["items"][0]["outcome"], "retrying")
        self.assertLessEqual(
            history["items"][0]["request_started_at"],
            history["items"][0]["recorded_at"],
        )
        self.assertIn("HTTP 404", history["items"][0]["summary"])
        self.assertNotIn("<html", history["items"][0]["summary"])

    async def test_json_business_404_is_not_retried(self) -> None:
        attempts = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": "model_not_found",
                        "message": "requested model does not exist",
                    }
                },
            )

        async def no_wait(_: float) -> None:
            return None

        temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(temp_context.cleanup)
        history_store = RecoveryHistoryStore(
            Path(temp_context.name) / "recovery.sqlite3"
        )
        router = ProviderRouter((provider("selected", current=True),))
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            router,
            client=upstream_client,
            retry_sleep=no_wait,
            recovery_history_store=history_store,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "model_not_found")
        self.assertEqual(attempts, 1)
        self.assertEqual(router.status().total_retries, 0)
        self.assertEqual(history_store.history()["total_count"], 0)

    async def test_sniffed_nginx_404_exhausts_without_html_leak(self) -> None:
        attempts = 0

        class ChunkedNginx404(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"<html><head><title>"
                yield b"404 Not Found</title></head><body>"
                yield (
                    b"<center><h1>404 Not Found</h1></center>"
                    b"<hr><center>nginx</center></body></html>"
                )

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(404, stream=ChunkedNginx404())

        async def no_wait(_: float) -> None:
            return None

        temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(temp_context.cleanup)
        history_store = RecoveryHistoryStore(
            Path(temp_context.name) / "recovery.sqlite3"
        )
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            retry_policy=RetryPolicy(max_attempts=2),
            retry_sleep=no_wait,
            recovery_history_store=history_store,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})
        history = history_store.history()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(attempts, 2)
        self.assertNotIn(b"nginx", response.content)
        self.assertNotIn(b"<html", response.content)
        self.assertEqual(history["total_count"], 2)
        self.assertEqual(
            {item["kind"] for item in history["items"]},
            {"http_404"},
        )
        self.assertEqual(history["items"][0]["outcome"], "exhausted")

    async def test_retry_status_redacts_sensitive_upstream_message(self) -> None:
        attempts = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    503,
                    json={"error": {"message": "api_key=fixture-private-value"}},
                )
            return httpx.Response(200, content=b"recovered")

        async def no_wait(_: float) -> None:
            return None

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            retry_sleep=no_wait,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})
        status = await client.get("/control/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("[已隐藏]", status.text)
        self.assertNotIn("fixture-private-value", status.text)

    async def test_retries_connection_error_before_first_response(self) -> None:
        attempts = 0

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("fixture connection failure", request=request)
            return httpx.Response(200, content=b"ok")

        async def no_wait(_: float) -> None:
            return None

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            retry_sleep=no_wait,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 2)

    async def test_retries_rate_limit_without_retry_after(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, json={"error": {"message": "quota"}})
            return httpx.Response(200, content=b"recovered")

        async def no_wait(delay: float) -> None:
            sleeps.append(delay)

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            retry_sleep=no_wait,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"recovered")
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [1.0])

    async def test_caps_rate_limit_retry_after_to_local_delay_budget(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "120"},
                    json={"error": {"message": "try much later"}},
                )
            return httpx.Response(200, content=b"recovered")

        async def no_wait(delay: float) -> None:
            sleeps.append(delay)

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            retry_policy=RetryPolicy(max_delay_seconds=5),
            retry_sleep=no_wait,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"recovered")
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [5])

    async def test_embedded_rate_limit_before_output_retries_on_current_provider(self) -> None:
        attempts: list[str] = []
        sleeps: list[float] = []
        first = provider("first", current=True)
        second = provider("second")
        router = ProviderRouter((first, second))

        class RateLimitedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
                yield b'data: {"type":"response.failed","response":{"status":"failed","error":'
                yield b'{"message":"exceeded retry limit, last status: 429 Too Many Requests"}}}\n\n'

        class RecoveredStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"recovered"}\n\n'
                yield b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'

        async def upstream(request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.host)
            if request.url.host == "first.example.test":
                router.select("second")
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=RateLimitedStream(),
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=RecoveredStream(),
            )

        async def no_wait(delay: float) -> None:
            sleeps.append(delay)

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            router,
            client=upstream_client,
            retry_policy=RetryPolicy(max_attempts=-1),
            retry_sleep=no_wait,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"recovered", response.content)
        self.assertNotIn(b"exceeded retry limit", response.content)
        self.assertEqual(attempts, ["first.example.test", "second.example.test"])
        self.assertEqual(sleeps, [0.0])
        status = router.status()
        self.assertEqual(status.total_retries, 1)
        self.assertEqual(status.last_retry_kind, "rate_limited")
        self.assertIn("HTTP 429", status.recent_retry_errors[0].summary)

    async def test_embedded_rate_limit_after_output_is_not_replayed(self) -> None:
        attempts = 0

        class OutputThenRateLimit(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
                yield (
                    b'data: {"type":"response.failed","response":{"status":"failed",'
                    b'"error":{"message":"last status: 429 Too Many Requests"}}}\n\n'
                )

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=OutputThenRateLimit(),
            )

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        router = ProviderRouter((provider("selected", current=True),))
        app = create_proxy_app(router, client=upstream_client)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(attempts, 1)
        self.assertIn(b"partial", response.content)
        self.assertIn(b"429 Too Many Requests", response.content)
        self.assertEqual(router.status().total_retries, 0)

    async def test_embedded_model_capacity_before_output_is_retried(self) -> None:
        attempts = 0

        class AtCapacityStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
                yield b'data: {"type":"response.reasoning_text.delta","delta":"hidden"}\n\n'
                yield b'data: {"type":"response.function_call_arguments.delta","delta":"{}"}\n\n'
                yield b'data: {"type":"response.failed","response":{"status":"failed","error":'
                yield (
                    b'{"message":"Selected model is at capacity. '
                    b'Please try a different model."}}}'
                )

        class RecoveredStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"recovered"}\n\n'
                yield b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            stream = AtCapacityStream() if attempts == 1 else RecoveredStream()
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        async def no_wait(_: float) -> None:
            return None

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        router = ProviderRouter((provider("selected", current=True),))
        temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(temp_context.cleanup)
        history_store = RecoveryHistoryStore(
            Path(temp_context.name) / "usage.sqlite3"
        )
        app = create_proxy_app(
            router,
            client=upstream_client,
            retry_sleep=no_wait,
            recovery_history_store=history_store,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})
        api_history = (
            await client.get("/control/api/status")
        ).json()["retry"]["history"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 2)
        self.assertIn(b"recovered", response.content)
        self.assertNotIn(b"at capacity", response.content)
        status = router.status()
        self.assertEqual(status.total_retries, 1)
        self.assertEqual(status.last_retry_kind, "model_capacity")
        self.assertIn(
            "Selected model is at capacity",
            status.recent_retry_errors[0].summary,
        )
        history = history_store.history()
        self.assertEqual(history["total_count"], 1)
        self.assertEqual(history["items"][0]["kind"], "model_capacity")
        self.assertEqual(history["items"][0]["stage"], "before_output")
        self.assertEqual(history["items"][0]["outcome"], "retrying")
        self.assertEqual(api_history["total_count"], 1)
        self.assertEqual(api_history["items"][0]["kind"], "model_capacity")

    async def test_oversized_nested_model_capacity_before_output_is_retried(self) -> None:
        attempts = 0

        class OversizedCapacityStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (
                    b'data: {"type":"response.failed","response":'
                    b'{"status":"failed","metadata":{"padding":"'
                )
                padding = b"x" * (320 * 1024)
                for offset in range(0, len(padding), 32 * 1024):
                    yield padding[offset : offset + 32 * 1024]
                yield (
                    b'"},"diagnostic":{"nested":{"message":'
                    b'"Selected model is at cap'
                )
                yield b'acity. Please try a different model."}}}}\n\n'

        class RecoveredStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"recovered"}\n\n'
                yield b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            stream = OversizedCapacityStream() if attempts == 1 else RecoveredStream()
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        async def no_wait(_: float) -> None:
            return None

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        router = ProviderRouter((provider("selected", current=True),))
        app = create_proxy_app(router, client=upstream_client, retry_sleep=no_wait)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 2)
        self.assertIn(b"recovered", response.content)
        self.assertNotIn(b"at capacity", response.content)
        self.assertEqual(router.status().total_retries, 1)
        self.assertEqual(router.status().last_retry_kind, "model_capacity")

    async def test_capacity_words_in_visible_output_do_not_trigger_retry(self) -> None:
        attempts = 0

        class ExplanationStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                text = (
                    'Example {"type":"response.failed"}: '
                    "Selected model is at capacity. Please try a different model."
                )
                event = {
                    "type": "response.output_text.delta",
                    "delta": text,
                }
                yield b"data: " + json.dumps(event).encode() + b"\n\n"
                yield b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ExplanationStream(),
            )

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        router = ProviderRouter((provider("selected", current=True),))
        app = create_proxy_app(router, client=upstream_client)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(attempts, 1)
        self.assertIn(b"at capacity", response.content)
        self.assertEqual(router.status().total_retries, 0)

    async def test_embedded_model_capacity_after_output_is_not_replayed(self) -> None:
        attempts = 0

        class OutputThenCapacity(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
                yield (
                    b'data: {"type":"response.failed","response":{"status":"failed",'
                    b'"error":{"message":"Selected model is at capacity. '
                    b'Please try a different model."}}}\n\n'
                )

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=OutputThenCapacity(),
            )

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        router = ProviderRouter((provider("selected", current=True),))
        temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(temp_context.cleanup)
        usage_path = Path(temp_context.name) / "usage.sqlite3"
        history_store = RecoveryHistoryStore(usage_path)
        usage_store = UsageStore(usage_path)
        app = create_proxy_app(
            router,
            client=upstream_client,
            recovery_history_store=history_store,
            usage_store=usage_store,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(attempts, 1)
        self.assertIn(b"partial", response.content)
        self.assertIn(b"at capacity", response.content)
        self.assertEqual(router.status().total_retries, 0)
        history = history_store.history()
        self.assertEqual(history["total_count"], 1)
        self.assertEqual(history["items"][0]["kind"], "model_capacity")
        self.assertEqual(history["items"][0]["stage"], "after_output")
        self.assertEqual(history["items"][0]["outcome"], "passed_through")
        usage_summary = usage_store.summary("all")
        usage_history = usage_store.history(provider_id="selected", window="all")
        self.assertEqual(usage_summary["total"]["request_count"], 1)
        self.assertEqual(usage_summary["total"]["successful_requests"], 0)
        self.assertEqual(usage_summary["total"]["failed_requests"], 1)
        self.assertIsNone(usage_summary["by_provider"]["selected"]["last_success_at"])
        self.assertEqual(usage_history["total_count"], 1)
        self.assertFalse(usage_history["items"][0]["succeeded"])
        self.assertEqual(usage_history["items"][0]["status_code"], 200)

    async def test_oversized_model_capacity_after_output_is_recorded(self) -> None:
        attempts = 0

        class OutputThenOversizedCapacity(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
                yield (
                    b'data: {"type":"response.failed","response":'
                    b'{"status":"failed","metadata":{"padding":"'
                )
                padding = b"x" * (320 * 1024)
                for offset in range(0, len(padding), 32 * 1024):
                    yield padding[offset : offset + 32 * 1024]
                yield (
                    b'"},"error":{"details":{"message":'
                    b'"Selected model is at cap'
                )
                yield b'acity. Please try a different model."}}}}\n\n'

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=OutputThenOversizedCapacity(),
            )

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        router = ProviderRouter((provider("selected", current=True),))
        temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(temp_context.cleanup)
        history_store = RecoveryHistoryStore(
            Path(temp_context.name) / "usage.sqlite3"
        )
        app = create_proxy_app(
            router,
            client=upstream_client,
            recovery_history_store=history_store,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(attempts, 1)
        self.assertIn(b"partial", response.content)
        self.assertIn(b"at capacity", response.content)
        self.assertEqual(router.status().total_retries, 0)
        history = history_store.history()
        self.assertEqual(history["total_count"], 1)
        self.assertEqual(history["items"][0]["kind"], "model_capacity")
        self.assertEqual(history["items"][0]["stage"], "after_output")
        self.assertEqual(history["items"][0]["outcome"], "passed_through")

    async def test_embedded_upstream_failure_before_output_is_retried(self) -> None:
        attempts = 0

        class FailedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
                yield b'data: {"type":"response.failed","response":{"status":"failed","error":'
                yield b'{"code":"upstream_error","message":"Upstream request failed"}}}\n\n'

        class RecoveredStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"recovered"}\n\n'
                yield b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            stream = FailedStream() if attempts == 1 else RecoveredStream()
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        async def no_wait(_: float) -> None:
            return None

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        router = ProviderRouter((provider("selected", current=True),))
        app = create_proxy_app(
            router,
            client=upstream_client,
            retry_sleep=no_wait,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 2)
        self.assertIn(b"recovered", response.content)
        self.assertNotIn(b"Upstream request failed", response.content)
        status = router.status()
        self.assertEqual(status.total_retries, 1)
        self.assertEqual(status.last_retry_kind, "upstream_error")
        self.assertIn("Upstream request failed", status.recent_retry_errors[0].summary)

    async def test_embedded_permanent_failure_is_not_retried(self) -> None:
        attempts = 0

        class InvalidRequestStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield (
                    b'data: {"type":"response.failed","response":{"status":"failed",'
                    b'"error":{"code":"invalid_request_error",'
                    b'"message":"Unknown parameter"}}}\n\n'
                )

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=InvalidRequestStream(),
            )

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        router = ProviderRouter((provider("selected", current=True),))
        app = create_proxy_app(router, client=upstream_client)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(attempts, 1)
        self.assertIn(b"Unknown parameter", response.content)
        self.assertEqual(router.status().total_retries, 0)

    async def test_retries_stream_failure_before_first_chunk(self) -> None:
        attempts = 0

        class BrokenBeforeOutput(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise httpx.ReadError("fixture early disconnect")
                yield b"unreachable"

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(200, stream=BrokenBeforeOutput())
            return httpx.Response(200, content=b"recovered")

        async def no_wait(_: float) -> None:
            return None

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(
            ProviderRouter((provider("selected", current=True),)),
            client=upstream_client,
            retry_sleep=no_wait,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"recovered")
        self.assertEqual(attempts, 2)

    async def test_does_not_replay_stream_after_first_chunk(self) -> None:
        attempts = 0

        class BrokenAfterOutput(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: started\n\n"
                raise httpx.ReadError("fixture late disconnect")

        async def upstream(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, stream=BrokenAfterOutput())

        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        router = ProviderRouter((provider("selected", current=True),))
        app = create_proxy_app(router, client=upstream_client)
        route = next(
            route for route in app.routes if getattr(route, "path", "") == "/v1/{upstream_path:path}"
        )
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "raw_path": b"/v1/responses",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1),
            "root_path": "",
        }
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": b"{}", "more_body": False}

        response = await route.endpoint("responses", Request(scope, receive))
        iterator = response.body_iterator

        self.assertEqual(await anext(iterator), b"data: started\n\n")
        with self.assertRaises(httpx.ReadError):
            await anext(iterator)
        self.assertEqual(attempts, 1)
        await upstream_client.aclose()

    async def test_forwards_request_stream_headers_query_and_response(self) -> None:
        observed: dict[str, object] = {}

        class EventStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: ok\n\n"

        async def upstream(request: httpx.Request) -> httpx.Response:
            observed["url"] = str(request.url)
            observed["authorization"] = request.headers.get("authorization")
            observed["local_header"] = request.headers.get("x-local-header")
            observed["body"] = await request.aread()
            return httpx.Response(
                201,
                headers={
                    "content-type": "text/event-stream",
                    "connection": "keep-alive",
                    "x-upstream": "yes",
                },
                stream=EventStream(),
            )

        selected = ProxyProvider(
            provider_id="selected",
            name="Selected",
            base_url="https://selected.example.test/v1",
            is_cc_switch_current=True,
            api_key="fixture-upstream-key",
            configured_headers={"X-Local-Header": "configured"},
            default_query={"api-version": "1", "existing": "ignored"},
        )
        router = ProviderRouter((selected,))
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = create_proxy_app(router, client=upstream_client)
        proxy_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:17890",
        )
        self.addAsyncCleanup(proxy_client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await proxy_client.post(
            "/v1/responses?existing=request",
            headers={
                "Authorization": "Bearer local-placeholder",
                "Connection": "close",
            },
            content=b'{"model":"gpt-test"}',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.content, b"data: ok\n\n")
        self.assertEqual(response.headers["x-upstream"], "yes")
        self.assertNotIn("connection", response.headers)
        self.assertEqual(
            observed["url"],
            "https://selected.example.test/v1/responses?existing=request&api-version=1",
        )
        self.assertEqual(observed["authorization"], "Bearer fixture-upstream-key")
        self.assertEqual(observed["local_header"], "configured")
        self.assertEqual(observed["body"], b'{"model":"gpt-test"}')
        self.assertEqual(router.status().active_by_provider, {})

    async def test_missing_credentials_returns_sanitized_error(self) -> None:
        router = ProviderRouter((provider("empty", current=True, api_key=None),))
        upstream_client = httpx.AsyncClient()
        app = create_proxy_app(router, client=upstream_client)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:17890",
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("credential", response.text.casefold())

    async def test_rejects_route_back_to_same_proxy_address(self) -> None:
        selected = ProxyProvider(
            provider_id="loop",
            name="Loop",
            base_url="http://localhost:17890/v1",
            is_cc_switch_current=True,
            api_key="fixture-key",
        )
        upstream_client = httpx.AsyncClient()
        app = create_proxy_app(ProviderRouter((selected,)), client=upstream_client)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:17890",
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        response = await client.post("/v1/responses", json={"model": "test"})

        self.assertEqual(response.status_code, 508)
        self.assertNotIn("fixture-key", response.text)

    def test_server_rejects_non_loopback_binding(self) -> None:
        router = ProviderRouter((provider("selected"),))

        with self.assertRaisesRegex(ValueError, "回环地址"):
            LocalProxyServer(router, host="0.0.0.0")

    async def test_control_api_switches_without_exposing_credentials(self) -> None:
        first = provider("first", current=True, api_key="first-private-value")
        second = provider("second", api_key="second-private-value")
        selected: list[str] = []
        upstream_client = httpx.AsyncClient()
        app = create_proxy_app(
            ProviderRouter((first, second)),
            client=upstream_client,
            on_provider_selected=selected.append,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        status = await client.get("/control/api/status")
        forbidden = await client.post("/control/api/providers/second/select")
        switched = await client.post(
            "/control/api/providers/second/select",
            headers={"X-Local-Proxy-Control": "1"},
        )

        self.assertEqual(status.status_code, 200)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(switched.json()["current_provider_id"], "second")
        self.assertEqual(selected, ["second"])
        serialized = status.text + switched.text
        self.assertNotIn("first-private-value", serialized)
        self.assertNotIn("second-private-value", serialized)
        self.assertNotIn("api_key", serialized.casefold())

    async def test_control_page_refresh_config_and_shutdown(self) -> None:
        router = ProviderRouter((provider("first", current=True),))
        stopped: list[bool] = []
        upstream_client = httpx.AsyncClient()
        app = create_proxy_app(
            router,
            client=upstream_client,
            reload_providers=lambda: (provider("refreshed", current=True),),
            config_fragment=lambda: 'base_url = "http://127.0.0.1:17890/v1"\n',
            on_shutdown_requested=lambda: stopped.append(True),
            health_status_url="https://status.example.test/api/status?window=24h",
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(upstream_client.aclose)

        page = await client.get("/control/")
        script = await client.get("/control/static/app.js")
        styles = await client.get("/control/static/styles.css")
        ui_config = await client.get("/control/api/ui-config")
        refreshed = await client.post(
            "/control/api/refresh",
            headers={"X-Local-Proxy-Control": "1"},
        )
        config = await client.get("/control/api/codex-config")
        shutdown = await client.post(
            "/control/api/shutdown",
            headers={"X-Local-Proxy-Control": "1"},
        )

        self.assertEqual(page.status_code, 200)
        self.assertIn("本地中转", page.text)
        self.assertNotIn("Codex 本地中转", page.text)
        self.assertIn('id="theme-button"', page.text)
        self.assertIn('data-theme-value="dark"', page.text)
        self.assertIn('id="recovery-details-button"', page.text)
        self.assertIn('id="usage-window"', page.text)
        self.assertIn('<option value="7d">近 7 天</option>', page.text)
        self.assertIn('<option value="30d">近 30 天</option>', page.text)
        self.assertNotIn("168 小时", page.text)
        self.assertIn('id="manage-providers"', page.text)
        self.assertIn('id="runtime-view"', page.text)
        self.assertIn('id="runtime-port"', page.text)
        self.assertIn('id="runtime-database-path"', page.text)
        self.assertIn('id="runtime-health-url"', page.text)
        self.assertIn('id="runtime-data-directory"', page.text)
        self.assertIn('id="usage-total"', page.text)
        self.assertIn("Token 用量", page.text)
        self.assertIn("styles.css?v=22", page.text)
        self.assertIn("app.js?v=26", page.text)
        self.assertIn("<span>请求</span><span>服务器检测</span>", page.text)
        self.assertIn("供应商", page.text)
        self.assertIn("设置会话路由", page.text)
        self.assertIn('id="active-sessions-popover"', page.text)
        self.assertIn('id="usage-history-popover"', page.text)
        self.assertIn('id="recovery-history-meta"', page.text)
        self.assertIn("selectProvider", script.text)
        self.assertIn("setProviderHidden", script.text)
        self.assertIn("saveProviderOrder", script.text)
        self.assertIn("usage_window", script.text)
        self.assertIn('suffix: "K"', script.text)
        self.assertIn('suffix: "M"', script.text)
        self.assertIn('suffix: "B"', script.text)
        self.assertIn("provider-token-cell", script.text)
        self.assertIn("openUsageHistoryPopover", script.text)
        self.assertIn("openActiveSessionsPopover", script.text)
        self.assertIn('controlUrl("/api/usage-history")', script.text)
        self.assertIn('controlUrl("/api/ui-config")', script.text)
        self.assertNotIn("/control/api/codex-config", script.text)
        self.assertIn("请求记录", script.text)
        self.assertIn("流级失败", script.text)
        self.assertIn("healthStatusUrl", script.text)
        self.assertNotIn("HEALTH_STATUS_URL", script.text)
        self.assertIn("normalizeProviderEndpoint", script.text)
        self.assertIn("createProviderHealthDetail", script.text)
        self.assertIn("openProviderHealthPopover", script.text)
        self.assertIn("showHistoryDetail", script.text)
        self.assertNotIn("expandedHealthProviderIds", script.text)
        self.assertIn("最近 60 次", script.text)
        self.assertIn("尚未输出且再次失败的旧请求将由新供应商接管", script.text)
        self.assertIn("local-proxy-theme", script.text)
        self.assertNotIn("codex-local-proxy-theme", script.text)
        self.assertIn("recent_errors", script.text)
        self.assertIn("retry.history", script.text)
        self.assertIn('controlUrl("/api/recovery-history")', script.text)
        self.assertIn("recoveryOutcomeLabel", script.text)
        self.assertIn("输出后未重放", script.text)
        self.assertIn("positionRecoveryPopover", script.text)
        self.assertIn("formatRecoverySummary", script.text)
        self.assertIn("model_capacity", script.text)
        self.assertIn(':root[data-theme="dark"]', styles.text)
        self.assertIn(".provider-list::-webkit-scrollbar", styles.text)
        self.assertIn("flex-direction: column", styles.text)
        self.assertIn("flex: 0 0 auto", styles.text)
        self.assertIn(".recovery-popover", styles.text)
        self.assertIn(".recovery-popover ol::-webkit-scrollbar", styles.text)
        self.assertIn(".usage-summary", styles.text)
        self.assertIn(".provider-token-cell", styles.text)
        self.assertIn(".usage-history-popover", styles.text)
        self.assertIn(".active-sessions-popover", styles.text)
        self.assertIn("--provider-grid-columns", styles.text)
        self.assertIn("scrollbar-gutter: stable", styles.text)
        self.assertIn(".provider-health-cell", styles.text)
        self.assertIn(".provider-health-detail", styles.text)
        self.assertIn(".provider-health-popover", styles.text)
        self.assertIn(".history-detail-popover", styles.text)
        self.assertIn("minmax(160px, 250px)", styles.text)
        self.assertIn("minmax(260px, 1fr)", styles.text)
        self.assertIn(".drag-handle", styles.text)
        self.assertIn(".hidden-provider", styles.text)
        self.assertNotIn("-webkit-line-clamp: 2", styles.text)
        self.assertIn("overflow-y: auto; overscroll-behavior: contain", styles.text)
        self.assertIn("max-height: min(340px", styles.text)
        self.assertIn(".setting-control-with-action", styles.text)
        self.assertEqual(script.headers["cache-control"], "no-store")
        self.assertEqual(ui_config.headers["cache-control"], "no-store")
        self.assertEqual(ui_config.json()["service_id"], "codex")
        self.assertEqual(ui_config.json()["config_endpoint"], "/control/api/codex-config")
        self.assertTrue(ui_config.json()["features"]["usage_history"])
        self.assertEqual(refreshed.json()["current_provider_id"], "refreshed")
        self.assertEqual(
            refreshed.json()["health_status_url"],
            "https://status.example.test/api/status?window=24h",
        )
        self.assertIn("127.0.0.1:17890", config.text)
        self.assertEqual(shutdown.json()["status"], "stopping")
        self.assertEqual(stopped, [True])


class LiveProxyTests(unittest.TestCase):
    def test_uvicorn_proxy_forwards_to_live_streaming_upstream(self) -> None:
        observed: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0"))
                observed["path"] = self.path
                observed["authorization"] = self.headers.get("Authorization")
                observed["body"] = self.rfile.read(content_length)
                body = b"data: live-ok\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)

        proxy_port = self._free_port()
        selected = ProxyProvider(
            provider_id="live",
            name="Live",
            base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
            is_cc_switch_current=True,
            api_key="live-fixture-key",
        )
        router = ProviderRouter((selected,))
        server = LocalProxyServer(router, port=proxy_port)
        server.start()
        self.addCleanup(server.stop)

        response = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/responses?stream=true",
            headers={"Authorization": "Bearer local-placeholder"},
            json={"model": "fixture-model"},
            timeout=5,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"data: live-ok\n\n")
        self.assertEqual(observed["path"], "/v1/responses?stream=true")
        self.assertEqual(observed["authorization"], "Bearer live-fixture-key")
        self.assertIn(b"fixture-model", observed["body"])
        self.assertEqual(router.status().active_by_provider, {})

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
