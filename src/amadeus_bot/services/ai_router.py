from __future__ import annotations

import time
import tomllib
from pathlib import Path
from threading import RLock
from typing import Any

from amadeus_bot.adapters.ai_provider import (
    ApiCredential,
    OpenAICompatibleProvider,
    ProviderConfig,
)
from amadeus_bot.domain.ai import AIResponse, AITask, ModelTarget, TaskRoute
from amadeus_bot.repositories.core import CoreRepository


class AIRouteCatalog:
    def __init__(
        self,
        providers: dict[str, ProviderConfig],
        routes: dict[AITask, TaskRoute],
        default_model: ModelTarget,
    ) -> None:
        self.providers = providers
        self.routes = routes
        self.default_model = default_model

    @classmethod
    def load(cls, path: Path) -> AIRouteCatalog:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
        providers = {
            name: ProviderConfig(
                name=name,
                credential_host=str(values["credential_host"]),
                api_prefix=str(values.get("api_prefix", "")),
                api_mode=str(values.get("api_mode", "chat_completions")),
                responses_stream=bool(values.get("responses_stream", False)),
                models=tuple(str(item) for item in values.get("models", [])),
            )
            for name, values in raw.get("providers", {}).items()
        }
        routes: dict[AITask, TaskRoute] = {}
        for task_name, values in raw.get("tasks", {}).items():
            task = AITask(task_name)
            routes[task] = TaskRoute(
                primary=ModelTarget.parse(str(values["primary"])),
                fallbacks=tuple(ModelTarget.parse(str(item)) for item in values.get("fallbacks", [])),
                temperature=float(values.get("temperature", 0.2)),
                max_tokens=int(values.get("max_tokens", 1000)),
            )
        missing = set(AITask) - routes.keys()
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"AI 路由缺少任务：{names}")
        runtime = raw.get("runtime", {})
        default_model = ModelTarget.parse(str(runtime["default_model"]))
        catalog = cls(providers, routes, default_model)
        catalog._validate()
        return catalog

    def model_targets(self) -> tuple[ModelTarget, ...]:
        return tuple(
            ModelTarget(provider=provider_name, model=model)
            for provider_name, provider in self.providers.items()
            for model in provider.models
        )

    def resolve_model(self, value: str) -> ModelTarget:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("模型名不能为空")
        targets = self.model_targets()
        if "/" in normalized:
            matches = [target for target in targets if target.qualified_name.casefold() == normalized]
        else:
            matches = [target for target in targets if target.model.casefold() == normalized]
        if not matches:
            raise ValueError(f"未知模型：{value.strip()}")
        if len(matches) > 1:
            choices = "、".join(target.qualified_name for target in matches)
            raise ValueError(f"模型名不唯一，请使用完整名称：{choices}")
        return matches[0]

    def _validate(self) -> None:
        for provider in self.providers.values():
            if provider.api_mode not in {"chat_completions", "responses"}:
                raise ValueError(f"未知 AI API 模式：{provider.name}/{provider.api_mode}")
        configured = set(self.model_targets())
        if self.default_model not in configured:
            raise ValueError(f"默认模型未列入 provider 模型清单：{self.default_model.qualified_name}")
        for task, route in self.routes.items():
            for target in (route.primary, *route.fallbacks):
                if target not in configured:
                    raise ValueError(
                        f"{task.value} 路由模型未列入 provider 模型清单：{target.qualified_name}"
                    )


class AIService:
    ACTIVE_MODEL_SETTING = "ai.active_model"

    def __init__(
        self,
        catalog: AIRouteCatalog,
        credentials: dict[str, ApiCredential],
        repository: CoreRepository,
    ) -> None:
        self.catalog = catalog
        self.repository = repository
        self._model_lock = RLock()
        self.providers: dict[str, OpenAICompatibleProvider] = {}
        for name, provider_config in catalog.providers.items():
            credential = credentials.get(provider_config.credential_host)
            if credential is not None:
                self.providers[name] = OpenAICompatibleProvider(provider_config, credential)
        self._active_model = self._load_active_model()

    def available(self) -> bool:
        return bool(self.providers)

    def route_description(self) -> dict[str, str]:
        current = self.current_model().qualified_name
        return {task.value: current for task in self.catalog.routes}

    def current_model(self) -> ModelTarget:
        with self._model_lock:
            return self._active_model

    def available_models(self) -> tuple[ModelTarget, ...]:
        return tuple(target for target in self.catalog.model_targets() if target.provider in self.providers)

    def switch_model(self, model_name: str, actor: str) -> ModelTarget:
        target = self.catalog.resolve_model(model_name)
        if target.provider not in self.providers:
            raise ValueError(f"模型供应商未配置凭据：{target.provider}")
        self.repository.set_runtime_setting(
            self.ACTIVE_MODEL_SETTING,
            target.qualified_name,
            actor,
        )
        with self._model_lock:
            self._active_model = target
        return target

    def _load_active_model(self) -> ModelTarget:
        stored = self.repository.get_runtime_setting(self.ACTIVE_MODEL_SETTING)
        if stored:
            try:
                return self.catalog.resolve_model(stored)
            except ValueError:
                pass
        return self.catalog.default_model

    async def complete(
        self,
        task: AITask,
        messages: list[dict[str, Any]],
        *,
        group_id: str | None = None,
        user_id: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AIResponse:
        quota = self.repository.ai_quota_for_task(task.value)
        if quota is not None and quota[1] >= quota[0]:
            raise AIQuotaExceeded(f"AI 任务 {task.value} 已达到当日调用上限 {quota[0]}")
        route = self.catalog.routes[task]
        errors: list[str] = []
        targets = dict.fromkeys(
            (route.primary, *route.fallbacks)
            if task in {AITask.VISION, AITask.SUMMARY, AITask.STATS_ANALYSIS}
            else (self.current_model(), route.primary, *route.fallbacks)
        )
        for target in targets:
            provider = self.providers.get(target.provider)
            if provider is None:
                errors.append(f"{target.provider}:credential-unavailable")
                continue
            started = time.perf_counter()
            try:
                result = await provider.complete(
                    model=target.model,
                    messages=messages,
                    temperature=route.temperature,
                    max_tokens=route.max_tokens,
                    tools=tools,
                )
                if not result.content.strip() and not result.tool_calls:
                    raise ValueError("AI response has no text or tool calls")
            except Exception as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                error_type = type(exc).__name__
                self.repository.record_ai_usage(
                    task=task.value,
                    provider=target.provider,
                    model=target.model,
                    latency_ms=latency_ms,
                    success=False,
                    group_id=group_id,
                    user_id=user_id,
                    error_type=error_type,
                )
                errors.append(f"{target.provider}/{target.model}:{error_type}")
                continue
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.repository.record_ai_usage(
                task=task.value,
                provider=result.provider,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cached_tokens=result.cached_tokens,
                latency_ms=latency_ms,
                success=True,
                group_id=group_id,
                user_id=user_id,
            )
            return result
        raise RuntimeError("所有 AI 路由均不可用：" + "; ".join(errors))

    async def close(self) -> None:
        for provider in self.providers.values():
            await provider.close()


class AIQuotaExceeded(RuntimeError):
    pass
