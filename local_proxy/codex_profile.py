"""Codex settings, UI configuration, and protocol profile construction."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx

from local_proxy.codex import load_proxy_providers
from local_proxy.codex_sessions import CodexSessionNameIndex
from local_proxy.core import (
    DEFAULT_PORT,
    HealthStatusUrlStore,
    ProviderRouter,
    RecoveryHistoryStore,
    RetryPolicyStore,
    UsageStore,
    filter_self_referencing_providers,
    order_proxy_providers,
)
from local_proxy.paths import display_path
from local_proxy.server import ProxyProfile
from local_proxy.shared_settings import (
    PROTOCOL_SETTINGS_VERSION,
    data_directory,
    default_protocol_settings,
    load_protocol_settings,
    protocol_settings_path,
    protocol_usage_database_path,
    save_protocol_settings,
)


SETTINGS_VERSION = PROTOCOL_SETTINGS_VERSION


def settings_path() -> Path:
    return protocol_settings_path("codex")


def usage_database_path() -> Path:
    return protocol_usage_database_path("codex")


def default_settings() -> dict[str, Any]:
    return default_protocol_settings()


def load_settings(path: Path | None = None) -> dict[str, Any]:
    return load_protocol_settings(path or settings_path())


def save_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    save_protocol_settings(settings, path or settings_path())


def codex_config_fragment(port: int = DEFAULT_PORT) -> str:
    return (
        'model_provider = "local_cc_switch"\n'
        "\n"
        "[model_providers.local_cc_switch]\n"
        'name = "CC Switch Local Proxy"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = true\n"
    )


def codex_ui_config(port: int, root: Path | None = None) -> dict[str, Any]:
    data_root = (root or data_directory()).expanduser().resolve()
    return {
        "service_id": "codex",
        "display_name": "Codex 本地中转",
        "brand_mark": "CX",
        "client_name": "Codex",
        "protocol_label": "Responses · SSE",
        "proxy_url": f"http://127.0.0.1:{port}/v1",
        "peer_console_label": "Claude Code 控制台",
        "peer_console_url": f"http://127.0.0.1:{port}/control/claude/",
        "config_endpoint": "/control/codex/api/codex-config",
        "control_base_path": "/control/codex",
        "config_button_label": "复制 Codex 配置",
        "config_location_label": "Codex 配置文件",
        "config_location_hint": "“复制 Codex 配置”生成的片段需要合并到此文件",
        "data_directory": display_path(data_root),
        "config_location": "~/.codex/config.toml",
        "restart_config_text": "端口将在退出并重新启动本地中转后生效；届时需要重新复制 Codex 配置。",
        "copy_config_success_title": "Codex 配置已复制",
        "copy_config_success_detail": "首次配置后重启一次 Codex，后续切换不再需要重启。",
        "shutdown_client_name": "Codex",
        "provider_label": "Codex API",
        "theme_storage_key": "local-proxy-theme",
        "features": {
            "usage_history": True,
            "session_routing": True,
        },
    }


def build_codex_profile(
    *,
    database: Path,
    port: int,
    data_root: Path | None = None,
    settings_data: dict[str, Any] | None = None,
    retry_policy_store: RetryPolicyStore | None = None,
    health_status_url_store: HealthStatusUrlStore | None = None,
) -> ProxyProfile:
    root = (data_root or data_directory()).expanduser().resolve()
    active_settings_path = protocol_settings_path("codex", root)
    active_usage_path = protocol_usage_database_path("codex", root)
    settings = (
        dict(settings_data)
        if settings_data is not None
        else load_settings(active_settings_path)
    )
    settings_lock = threading.RLock()
    active_database_path = database.expanduser().resolve()

    def load_prepared_providers(source: Path) -> tuple:
        loaded = filter_self_referencing_providers(load_proxy_providers(source), port)
        with settings_lock:
            provider_order = tuple(settings.get("provider_order", ()))
        return order_proxy_providers(loaded, provider_order)

    def prepared_providers() -> tuple:
        with settings_lock:
            source = active_database_path
        return load_prepared_providers(source)

    providers = prepared_providers()
    router = ProviderRouter(
        providers,
        current_provider_id=settings.get("selected_provider_id"),
        session_provider_overrides=settings.get("session_provider_overrides", {}),
    )
    session_name_index = CodexSessionNameIndex()

    def persist(**changes: Any) -> None:
        with settings_lock:
            settings.update(changes, schema_version=SETTINGS_VERSION)
            save_settings(settings, active_settings_path)

    def persist_session_provider_override(
        thread_id: str,
        provider_id: str | None,
    ) -> None:
        with settings_lock:
            overrides = dict(settings.get("session_provider_overrides", {}))
            if provider_id is None:
                overrides.pop(thread_id, None)
            else:
                overrides.pop(thread_id, None)
                overrides[thread_id] = provider_id
                if len(overrides) > 1000:
                    overrides = dict(list(overrides.items())[-1000:])
            settings.update(
                session_provider_overrides=overrides,
                schema_version=SETTINGS_VERSION,
            )
            save_settings(settings, active_settings_path)

    def apply_database(source: Path, loaded: tuple) -> None:
        nonlocal active_database_path
        with settings_lock:
            active_database_path = source
        current = router.current_provider()
        selected = router.replace_providers(
            loaded,
            preferred_id=current.provider_id if current else None,
        )
        selected_id = selected.provider_id if selected is not None else None
        if selected_id != settings.get("selected_provider_id"):
            persist(selected_provider_id=selected_id)

    def runtime_metadata() -> dict[str, Any]:
        return {
            "data_directory": display_path(root),
            "settings_file": display_path(active_settings_path),
            "usage_database": display_path(active_usage_path),
            "codex_config_file": "~/.codex/config.toml",
        }

    return ProxyProfile(
        service_id="codex",
        service_name="codex-local-proxy",
        router=router,
        upstream_client=httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0),
            follow_redirects=False,
        ),
        reload_providers=prepared_providers,
        on_provider_selected=lambda provider_id: persist(selected_provider_id=provider_id),
        on_session_provider_override_changed=persist_session_provider_override,
        hidden_provider_ids=settings.get("hidden_provider_ids", ()),
        provider_order=settings.get("provider_order", ()),
        on_hidden_provider_ids_changed=lambda ids: persist(hidden_provider_ids=list(ids)),
        on_provider_order_changed=lambda ids: persist(provider_order=list(ids)),
        config_fragment=lambda: codex_config_fragment(port),
        retry_policy_store=retry_policy_store or RetryPolicyStore(),
        usage_store=UsageStore(active_usage_path),
        recovery_history_store=RecoveryHistoryStore(active_usage_path),
        health_status_url_store=health_status_url_store or HealthStatusUrlStore(),
        load_runtime_database=load_prepared_providers,
        apply_runtime_database=apply_database,
        database_validation_summary=lambda loaded: {
            "provider_count": len(loaded),
            "current_provider_configured": any(
                provider.is_cc_switch_current for provider in loaded
            ),
        },
        runtime_metadata=runtime_metadata,
        ui_config=lambda: codex_ui_config(port, root),
        session_name_resolver=session_name_index.resolve,
        session_catalog=session_name_index.recent,
        session_key_resolver=session_name_index.thread_id_for_session_key,
        config_endpoint_name="codex-config",
    )
