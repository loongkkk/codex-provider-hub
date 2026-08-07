from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.background import BackgroundTask

from local_proxy.core import (
    CONTROL_ASSET_DIR,
    RECOVERY_HISTORY_API_LIMIT,
    USAGE_WINDOWS,
    HealthStatusUrlStore,
    ProviderRouter,
    ProxyProvider,
    RecoveryHistoryStore,
    RetryPolicy,
    RetryPolicyStore,
    UsageStore,
    _empty_usage_summary,
    _forward_request,
    _public_control_status,
    _public_requests,
    _public_sessions,
    _valid_control_request,
    order_proxy_providers,
    retry_policy_from_mapping,
)


UI_CONFIG_FIELDS = frozenset(
    {
        "service_id",
        "display_name",
        "brand_mark",
        "client_name",
        "protocol_label",
        "proxy_url",
        "peer_console_label",
        "peer_console_url",
        "config_endpoint",
        "control_base_path",
        "config_button_label",
        "config_location_label",
        "config_location_hint",
        "data_directory",
        "config_location",
        "restart_config_text",
        "copy_config_success_title",
        "copy_config_success_detail",
        "shutdown_client_name",
        "provider_label",
        "theme_storage_key",
        "features",
    }
)
UI_FEATURE_FIELDS = frozenset({"usage_history", "session_routing"})


@dataclass
class ProxyProfile:
    service_id: str
    service_name: str
    router: ProviderRouter
    upstream_client: Any
    protocol_adapter: Any | None = None
    allowed_proxy_paths: frozenset[str] | None = None
    reload_providers: Callable[[], tuple[ProxyProvider, ...]] | None = None
    on_provider_selected: Callable[[str], None] | None = None
    on_session_provider_override_changed: Callable[[str, str | None], None] | None = None
    hidden_provider_ids: Iterable[str] = ()
    provider_order: Iterable[str] = ()
    on_hidden_provider_ids_changed: Callable[[tuple[str, ...]], None] | None = None
    on_provider_order_changed: Callable[[tuple[str, ...]], None] | None = None
    config_fragment: Callable[[], str] | None = None
    retry_policy_store: RetryPolicyStore | None = None
    retry_policy: RetryPolicy | None = None
    on_retry_policy_changed: Callable[[RetryPolicy], None] | None = None
    usage_store: UsageStore | None = None
    recovery_history_store: RecoveryHistoryStore | None = None
    health_status_url_store: HealthStatusUrlStore | None = None
    runtime_settings_snapshot: Callable[[], dict[str, Any]] | None = None
    on_runtime_settings_changed: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    validate_runtime_database: Callable[[str], dict[str, Any]] | None = None
    load_runtime_database: Callable[[Path], tuple[ProxyProvider, ...]] | None = None
    apply_runtime_database: Callable[[Path, tuple[ProxyProvider, ...]], None] | None = None
    database_validation_summary: Callable[[tuple[ProxyProvider, ...]], Mapping[str, Any]] | None = None
    runtime_metadata: Callable[[], Mapping[str, Any]] | None = None
    ui_config: Callable[[], Mapping[str, Any]] | None = None
    provider_selectable: Callable[[ProxyProvider], bool] | None = None
    provider_public_fields: Callable[[ProxyProvider], Mapping[str, Any]] | None = None
    session_name_resolver: Callable[[Iterable[str]], Mapping[str, str]] | None = None
    session_catalog: Callable[[float], Iterable[Mapping[str, Any]]] | None = None
    session_key_resolver: Callable[[str], str | None] | None = None
    config_endpoint_name: str = "config"
    owns_client: bool = True
    retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    preferences_lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def __post_init__(self) -> None:
        self.retry_policy_store = self.retry_policy_store or RetryPolicyStore(self.retry_policy)
        self.health_status_url_store = self.health_status_url_store or HealthStatusUrlStore()
        self.active_hidden_provider_ids = {
            provider_id
            for provider_id in self.hidden_provider_ids
            if isinstance(provider_id, str)
        }
        self.active_provider_order = [
            provider_id for provider_id in self.provider_order if isinstance(provider_id, str)
        ]


def create_unified_proxy_app(
    codex: ProxyProfile,
    claude: ProxyProfile,
    *,
    control_asset_dir: Path = CONTROL_ASSET_DIR,
    on_shutdown_requested: Callable[[], None] | None = None,
) -> FastAPI:
    profiles = {"codex": codex, "claude": claude}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            closed: set[int] = set()
            for profile in profiles.values():
                if not profile.owns_client or id(profile.upstream_client) in closed:
                    continue
                closed.add(id(profile.upstream_client))
                await profile.upstream_client.aclose()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        services: dict[str, Any] = {}
        active_requests = 0
        configured = False
        for service_id, profile in profiles.items():
            current = profile.router.current_provider()
            status = profile.router.status()
            active = sum(status.active_by_provider.values())
            active_requests += active
            configured = configured or current is not None
            services[service_id] = {
                "status": "ok" if current is not None else "not_configured",
                "current_provider": current.name if current else None,
                "active_requests": active,
            }
        return {
            "service": "codex-provider-hub",
            "status": "ok" if configured else "not_configured",
            "active_requests": active_requests,
            "services": services,
        }

    @app.get("/control", include_in_schema=False)
    async def control_redirect() -> RedirectResponse:
        return RedirectResponse("/control/codex/", status_code=307)

    @app.get("/control/", include_in_schema=False)
    async def control_root_redirect() -> RedirectResponse:
        return RedirectResponse("/control/codex/", status_code=307)

    for service_id, profile in profiles.items():
        _register_control_routes(
            app,
            profile,
            prefix=f"/control/{service_id}",
            control_asset_dir=control_asset_dir,
            on_shutdown_requested=on_shutdown_requested,
        )

    @app.api_route(
        "/v1/{upstream_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy_v1(upstream_path: str, request: Request):
        normalized_path = upstream_path.strip("/")
        first_segment = normalized_path.split("/", 1)[0]
        if normalized_path in (claude.allowed_proxy_paths or frozenset()):
            profile = claude
        elif first_segment == "messages":
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        else:
            profile = codex
        current_provider = profile.router.current_provider()
        if (
            current_provider is not None
            and profile.provider_selectable is not None
            and not profile.provider_selectable(current_provider)
        ):
            return JSONResponse(
                status_code=503,
                content={"error": {"message": "当前供应商与请求协议不兼容"}},
            )
        return await _forward_request(
            profile.router,
            profile.upstream_client,
            request,
            normalized_path,
            retry_policy=profile.retry_policy_store.get(),
            retry_sleep=profile.retry_sleep,
            usage_store=profile.usage_store,
            recovery_history_store=profile.recovery_history_store,
            protocol_adapter=profile.protocol_adapter,
            session_name_resolver=profile.session_name_resolver,
        )

    return app


def _register_control_routes(
    app: FastAPI,
    profile: ProxyProfile,
    *,
    prefix: str,
    control_asset_dir: Path,
    on_shutdown_requested: Callable[[], None] | None,
) -> None:
    def public_status(window: str = "today") -> dict[str, Any]:
        with profile.preferences_lock:
            hidden = set(profile.active_hidden_provider_ids)
        usage = (
            profile.usage_store.summary(window)
            if profile.usage_store is not None
            else _empty_usage_summary(window)
        )
        recovery_history = None
        if profile.recovery_history_store is not None:
            try:
                recovery_history = profile.recovery_history_store.history(limit=1)
            except (OSError, sqlite3.Error):
                recovery_history = None
        return _public_control_status(
            profile.router,
            profile.retry_policy_store.get(),
            hidden_provider_ids=hidden,
            usage_summary=usage,
            recovery_history=recovery_history,
            health_status_url=profile.health_status_url_store.get(),
            service_name=profile.service_name,
            provider_public_fields=profile.provider_public_fields,
            session_name_resolver=profile.session_name_resolver,
        )

    def public_status_for_request(request: Request) -> dict[str, Any]:
        window = request.query_params.get("usage_window", "today").strip().lower()
        return public_status(window if window in USAGE_WINDOWS else "today")

    async def control_page() -> FileResponse:
        return FileResponse(control_asset_dir / "index.html")

    async def control_ui_config():
        configured = dict(profile.ui_config() if profile.ui_config is not None else {})
        payload = {key: value for key, value in configured.items() if key in UI_CONFIG_FIELDS}
        features = payload.get("features")
        if isinstance(features, Mapping):
            payload["features"] = {
                key: bool(features[key])
                for key in UI_FEATURE_FIELDS
                if key in features
            }
        else:
            payload.pop("features", None)
        payload["service_id"] = profile.service_id
        return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})

    async def control_asset(asset_name: str):
        if asset_name not in {"app.js", "styles.css"}:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        response = FileResponse(control_asset_dir / asset_name)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def control_status(request: Request):
        window = request.query_params.get("usage_window", "today").strip().lower()
        if window not in USAGE_WINDOWS:
            return JSONResponse(status_code=422, content={"detail": "Token 统计时间范围无效"})
        return public_status(window)

    async def control_recovery_history(request: Request):
        if profile.recovery_history_store is None:
            history = public_status()["retry"]["history"]
        else:
            try:
                limit = int(request.query_params.get("limit", str(RECOVERY_HISTORY_API_LIMIT)))
                history = profile.recovery_history_store.history(
                    limit=limit,
                    cursor=request.query_params.get("cursor"),
                )
            except ValueError as exc:
                return JSONResponse(status_code=422, content={"detail": str(exc)})
            except (OSError, sqlite3.Error):
                return JSONResponse(status_code=503, content={"detail": "无法读取本地恢复记录"})
        return JSONResponse(content=history, headers={"Cache-Control": "no-store"})

    async def control_usage_history(request: Request):
        provider_id = request.query_params.get("provider_id", "").strip()
        window = request.query_params.get("usage_window", "today").strip().lower()
        cursor = request.query_params.get("cursor")
        if window not in USAGE_WINDOWS:
            return JSONResponse(status_code=422, content={"detail": "Token 统计时间范围无效"})
        if not provider_id or not any(
            provider.provider_id == provider_id for provider in profile.router.providers()
        ):
            return JSONResponse(status_code=404, content={"detail": "供应商不存在"})
        if profile.usage_store is None:
            return JSONResponse(status_code=503, content={"detail": "Token 记录功能不可用"})
        try:
            history = profile.usage_store.history(
                provider_id=provider_id,
                window=window,
                cursor=cursor,
            )
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        except (OSError, sqlite3.Error):
            return JSONResponse(status_code=503, content={"detail": "无法读取请求记录"})
        return JSONResponse(content=history, headers={"Cache-Control": "no-store"})

    async def control_requests(request: Request):
        if profile.usage_store is None:
            return JSONResponse(status_code=503, content={"detail": "请求记录功能不可用"})
        provider_id = request.query_params.get("provider_id", "").strip() or None
        if provider_id and not any(
            provider.provider_id == provider_id for provider in profile.router.providers()
        ):
            return JSONResponse(status_code=404, content={"detail": "供应商不存在"})
        try:
            payload = _public_requests(
                profile.router,
                profile.usage_store,
                window=request.query_params.get("window", "24h"),
                status_filter=request.query_params.get("status", "all"),
                provider_id=provider_id,
                query=request.query_params.get("query", ""),
                cursor=request.query_params.get("cursor"),
                session_name_resolver=profile.session_name_resolver,
            )
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        except (OSError, sqlite3.Error):
            return JSONResponse(status_code=503, content={"detail": "无法读取本地请求记录"})
        return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})

    async def control_sessions():
        if profile.session_catalog is None:
            return JSONResponse(status_code=503, content={"detail": "会话路由功能不可用"})
        try:
            sessions = profile.session_catalog(time.time() - 7 * 24 * 3600)
            payload = _public_sessions(
                profile.router,
                sessions,
                session_name_resolver=profile.session_name_resolver,
            )
        except (OSError, TypeError, ValueError):
            return JSONResponse(status_code=503, content={"detail": "无法读取 Codex 会话列表"})
        return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})

    async def control_session_route(session_key: str, request: Request):
        if not _valid_control_request(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        if profile.usage_store is None or not re.fullmatch(r"[0-9a-f]{24}", session_key):
            return JSONResponse(status_code=404, content={"detail": "未找到该会话"})
        try:
            payload = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(status_code=422, content={"detail": "会话路由格式无效"})
        if not isinstance(payload, dict) or "provider_id" not in payload:
            return JSONResponse(status_code=422, content={"detail": "会话路由格式无效"})
        provider_id = payload["provider_id"]
        if provider_id is not None and not isinstance(provider_id, str):
            return JSONResponse(status_code=422, content={"detail": "provider_id 必须是字符串或 null"})
        thread_id = profile.router.thread_id_for_session_key(session_key)
        if thread_id is None:
            thread_id = profile.usage_store.thread_id_for_session_key(session_key)
        if thread_id is None and profile.session_key_resolver is not None:
            thread_id = profile.session_key_resolver(session_key)
        if thread_id is None:
            return JSONResponse(status_code=404, content={"detail": "未找到该会话"})
        if provider_id is not None:
            candidate = next(
                (
                    provider
                    for provider in profile.router.providers()
                    if provider.provider_id == provider_id
                ),
                None,
            )
            if candidate is None:
                return JSONResponse(status_code=404, content={"detail": "供应商不存在"})
            if profile.provider_selectable is not None and not profile.provider_selectable(candidate):
                return JSONResponse(status_code=409, content={"detail": "该供应商与当前协议不兼容"})
        previous = profile.router.session_provider_override(thread_id)
        try:
            profile.router.set_session_provider_override(thread_id, provider_id)
            if profile.on_session_provider_override_changed is not None:
                profile.on_session_provider_override_changed(thread_id, provider_id)
        except (OSError, ValueError, sqlite3.Error):
            profile.router.set_session_provider_override(thread_id, previous)
            return JSONResponse(status_code=503, content={"detail": "无法保存会话路由"})
        return JSONResponse(
            content={"session_key": session_key, "provider_id": provider_id},
            headers={"Cache-Control": "no-store"},
        )

    async def control_retry_policy(request: Request):
        if not _valid_control_request(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        try:
            policy = retry_policy_from_mapping(await request.json())
        except (ValueError, TypeError):
            return JSONResponse(status_code=422, content={"detail": "重试设置格式无效"})
        profile.retry_policy_store.replace(policy)
        if profile.on_retry_policy_changed is not None:
            profile.on_retry_policy_changed(policy)
        return public_status_for_request(request)

    async def control_runtime_settings():
        if profile.runtime_settings_snapshot is None:
            return JSONResponse(status_code=503, content={"detail": "运行设置功能不可用"})
        return JSONResponse(
            content=profile.runtime_settings_snapshot(),
            headers={"Cache-Control": "no-store"},
        )

    async def control_update_runtime_settings(request: Request):
        if not _valid_control_request(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        if profile.on_runtime_settings_changed is None:
            return JSONResponse(status_code=503, content={"detail": "运行设置功能不可用"})
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("运行设置必须是对象")
            updated = profile.on_runtime_settings_changed(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc) or "运行设置格式无效"})
        except (OSError, sqlite3.Error):
            return JSONResponse(status_code=503, content={"detail": "无法保存运行设置"})
        return JSONResponse(content=updated, headers={"Cache-Control": "no-store"})

    async def control_validate_runtime_database(request: Request):
        if not _valid_control_request(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        if profile.validate_runtime_database is None:
            return JSONResponse(status_code=503, content={"detail": "数据源验证功能不可用"})
        try:
            payload = await request.json()
            database_path = payload.get("database_path") if isinstance(payload, dict) else None
            if not isinstance(database_path, str) or not database_path.strip():
                raise ValueError("数据来源不能为空")
            result = profile.validate_runtime_database(database_path)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc) or "数据来源无效"})
        except (OSError, sqlite3.Error):
            return JSONResponse(status_code=422, content={"detail": "无法读取供应商数据库"})
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})

    async def control_select(provider_id: str, request: Request):
        if not _valid_control_request(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        candidate = next(
            (provider for provider in profile.router.providers() if provider.provider_id == provider_id),
            None,
        )
        if candidate is None:
            return JSONResponse(status_code=404, content={"detail": "未找到该供应商"})
        if profile.provider_selectable is not None and not profile.provider_selectable(candidate):
            return JSONResponse(status_code=409, content={"detail": "该供应商与当前协议不兼容"})
        try:
            selected = profile.router.select(provider_id)
        except KeyError:
            return JSONResponse(status_code=404, content={"detail": "未找到该供应商"})
        if profile.on_provider_selected is not None:
            profile.on_provider_selected(selected.provider_id)
        return public_status_for_request(request)

    async def control_visibility(provider_id: str, request: Request):
        if not _valid_control_request(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        provider_ids = {provider.provider_id for provider in profile.router.providers()}
        if provider_id not in provider_ids:
            return JSONResponse(status_code=404, content={"detail": "未找到该供应商"})
        try:
            payload = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(status_code=422, content={"detail": "显示设置格式无效"})
        hidden = payload.get("hidden") if isinstance(payload, dict) else None
        if not isinstance(hidden, bool):
            return JSONResponse(status_code=422, content={"detail": "hidden 必须是布尔值"})
        current = profile.router.current_provider()
        if hidden and current is not None and current.provider_id == provider_id:
            return JSONResponse(status_code=409, content={"detail": "当前供应商不能隐藏，请先切换"})
        with profile.preferences_lock:
            if hidden:
                profile.active_hidden_provider_ids.add(provider_id)
            else:
                profile.active_hidden_provider_ids.discard(provider_id)
            saved_hidden = tuple(
                item.provider_id
                for item in profile.router.providers()
                if item.provider_id in profile.active_hidden_provider_ids
            )
        if profile.on_hidden_provider_ids_changed is not None:
            profile.on_hidden_provider_ids_changed(saved_hidden)
        return public_status_for_request(request)

    async def control_provider_order(request: Request):
        if not _valid_control_request(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        try:
            payload = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(status_code=422, content={"detail": "排序设置格式无效"})
        requested = payload.get("provider_ids") if isinstance(payload, dict) else None
        current_ids = [provider.provider_id for provider in profile.router.providers()]
        if (
            not isinstance(requested, list)
            or any(not isinstance(item, str) for item in requested)
            or len(set(requested)) != len(requested)
            or set(requested) != set(current_ids)
        ):
            return JSONResponse(status_code=422, content={"detail": "供应商排序必须完整且不能重复"})
        current = profile.router.current_provider()
        by_id = {provider.provider_id: provider for provider in profile.router.providers()}
        profile.router.replace_providers(
            tuple(by_id[provider_id] for provider_id in requested),
            preferred_id=current.provider_id if current else None,
        )
        with profile.preferences_lock:
            profile.active_provider_order[:] = requested
            saved_order = tuple(profile.active_provider_order)
        if profile.on_provider_order_changed is not None:
            profile.on_provider_order_changed(saved_order)
        return public_status_for_request(request)

    async def control_refresh(request: Request):
        if not _valid_control_request(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        if profile.reload_providers is None:
            return JSONResponse(status_code=503, content={"detail": "刷新功能不可用"})
        current = profile.router.current_provider()
        try:
            providers = profile.reload_providers()
            with profile.preferences_lock:
                providers = order_proxy_providers(providers, profile.active_provider_order)
            profile.router.replace_providers(
                providers,
                preferred_id=current.provider_id if current else None,
            )
        except (OSError, ValueError, sqlite3.Error):
            return JSONResponse(status_code=503, content={"detail": "无法读取 CC Switch 数据库"})
        return public_status_for_request(request)

    async def control_config():
        if profile.config_fragment is None:
            return JSONResponse(status_code=503, content={"detail": "配置生成功能不可用"})
        return PlainTextResponse(profile.config_fragment(), headers={"Cache-Control": "no-store"})

    async def control_shutdown(request: Request):
        if not _valid_control_request(request):
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        if on_shutdown_requested is None:
            return JSONResponse(status_code=503, content={"detail": "退出功能不可用"})
        return JSONResponse(
            content={"status": "stopping"},
            background=BackgroundTask(on_shutdown_requested),
        )

    app.add_api_route(f"{prefix}/", control_page, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{prefix}/api/ui-config", control_ui_config, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{prefix}/static/{{asset_name}}", control_asset, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{prefix}/api/status", control_status, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{prefix}/api/recovery-history", control_recovery_history, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{prefix}/api/usage-history", control_usage_history, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{prefix}/api/requests", control_requests, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{prefix}/api/sessions", control_sessions, methods=["GET"], include_in_schema=False)
    app.add_api_route(
        f"{prefix}/api/session-routes/{{session_key}}",
        control_session_route,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(f"{prefix}/api/retry-policy", control_retry_policy, methods=["POST"], include_in_schema=False)
    app.add_api_route(f"{prefix}/api/runtime-settings", control_runtime_settings, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{prefix}/api/runtime-settings", control_update_runtime_settings, methods=["POST"], include_in_schema=False)
    app.add_api_route(
        f"{prefix}/api/runtime-settings/validate-database",
        control_validate_runtime_database,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        f"{prefix}/api/providers/{{provider_id}}/select",
        control_select,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        f"{prefix}/api/providers/{{provider_id}}/visibility",
        control_visibility,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(f"{prefix}/api/providers/order", control_provider_order, methods=["POST"], include_in_schema=False)
    app.add_api_route(f"{prefix}/api/refresh", control_refresh, methods=["POST"], include_in_schema=False)
    app.add_api_route(
        f"{prefix}/api/{profile.config_endpoint_name}",
        control_config,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(f"{prefix}/api/shutdown", control_shutdown, methods=["POST"], include_in_schema=False)
