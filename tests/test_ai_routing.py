import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from amadeus_bot.adapters.ai_provider import (
    ApiCredential,
    CredentialStore,
    OpenAICompatibleProvider,
    ProviderConfig,
    _parse_tool_call,
    _responses_input,
)
from amadeus_bot.domain.ai import AIResponse, AITask
from amadeus_bot.repositories.core import CoreRepository
from amadeus_bot.repositories.database import CoreDatabase
from amadeus_bot.services.ai_router import AIRouteCatalog, AIService

ROUTES_PATH = Path(__file__).resolve().parents[1] / "config" / "ai_routes.toml"


def test_route_catalog_assigns_cheap_and_quality_models() -> None:
    catalog = AIRouteCatalog.load(ROUTES_PATH)

    assert catalog.default_model.qualified_name == "deepseek/deepseek-v4-flash"
    assert catalog.routes[AITask.PROACTIVE_GATE].primary.model == "deepseek-v4-flash"
    assert catalog.routes[AITask.VISION].primary.model == "deepseek-v4-flash-vision-exp"
    assert catalog.routes[AITask.CHAT].primary.model == "deepseek-v4-flash"
    assert catalog.routes[AITask.CHAT].fallbacks[0].model == "deepseek-v4-pro"
    assert catalog.providers["deepseek"].models == (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-vision-exp",
    )
    assert set(catalog.providers) == {"deepseek"}


def test_responses_input_converts_image_url_to_input_image() -> None:
    items = _responses_input(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": "https://example.test/img.png"}},
                ],
            }
        ]
    )
    assert items[0]["content"] == [
        {"type": "input_text", "text": "describe"},
        {"type": "input_image", "image_url": "https://example.test/img.png"},
    ]


def test_credential_store_parses_pairs_without_copying_file(tmp_path: Path) -> None:
    path = tmp_path / "apikey.txt"
    path.write_text(
        "apikey=test-key-one\nurl=https://one.example\n\napikey: test-key-two\nurl: https://two.example\n",
        encoding="utf-8",
    )
    credentials = CredentialStore.load(path)
    assert set(credentials) == {"one.example", "two.example"}
    assert credentials["one.example"].api_key == "test-key-one"


def test_parse_openai_tool_call() -> None:
    call = _parse_tool_call(
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "ddl_add", "arguments": '{"deadline":"明天下午三点"}'},
        }
    )
    assert call.call_id == "call-1"
    assert call.name == "ddl_add"
    assert call.arguments == {"deadline": "明天下午三点"}


def test_responses_input_uses_typed_content_and_replays_output_items() -> None:
    raw_call = {
        "type": "function_call",
        "call_id": "call-1",
        "name": "ddl_add",
        "arguments": '{"deadline":"明天"}',
    }
    items = _responses_input(
        [
            {"role": "system", "content": "system prompt"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [],
                "_responses_output": [{"type": "reasoning", "content": []}, raw_call],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": '{"success":true}'},
        ]
    )

    assert items[0] == {
        "role": "system",
        "content": [{"type": "input_text", "text": "system prompt"}],
    }
    assert items[1:3] == [{"type": "reasoning", "content": []}, raw_call]
    assert items[3] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": '{"success":true}',
    }


@pytest.mark.asyncio
async def test_responses_provider_parses_stream_and_flattens_tools() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        output_item = {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call-1",
                "name": "ddl_add",
                "arguments": '{"deadline":"明天"}',
            },
        }
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp-1",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.6-luna",
                "output": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "input_tokens_details": {"cached_tokens": 2},
                },
            },
        }
        body = (
            "event: response.output_item.done\ndata: "
            + json.dumps(output_item)
            + "\n\nevent: response.completed\ndata: "
            + json.dumps(completed)
            + "\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(
        ProviderConfig(
            name="huanyan",
            credential_host="api.huanyan.ltd",
            api_prefix="",
            api_mode="responses",
            responses_stream=True,
            models=("gpt-5.6-luna",),
        ),
        ApiCredential("https://api.huanyan.ltd/v1", "test-key"),
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await provider.complete(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.2,
            max_tokens=100,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "ddl_add",
                        "description": "add ddl",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )
    finally:
        await provider.close()

    assert captured["path"] == "/v1/responses"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["input"][0]["content"][0] == {"type": "input_text", "text": "hello"}
    assert payload["tools"][0]["name"] == "ddl_add"
    assert "function" not in payload["tools"][0]
    assert result.response_id == "resp-1"
    assert result.tool_calls[0].call_id == "call-1"
    assert result.cached_tokens == 2


@pytest.mark.asyncio
async def test_ai_service_switches_and_persists_active_model(tmp_path: Path) -> None:
    catalog = AIRouteCatalog.load(ROUTES_PATH)
    database = CoreDatabase(tmp_path / "core.sqlite3")
    database.initialize()
    repository = CoreRepository(database)
    credentials = {
        "api.deepseek.com": ApiCredential("https://api.deepseek.com", "test-deepseek"),
    }
    service = AIService(catalog, credentials, repository)
    try:
        assert service.current_model().qualified_name == "deepseek/deepseek-v4-flash"
        assert len(service.available_models()) == 3
        switched = service.switch_model("deepseek-v4-pro", "999")
        assert switched.qualified_name == "deepseek/deepseek-v4-pro"
    finally:
        await service.close()

    reopened = AIService(catalog, credentials, repository)
    try:
        assert reopened.current_model().qualified_name == "deepseek/deepseek-v4-pro"
    finally:
        await reopened.close()

    repository.set_runtime_setting("ai.active_model", "huanyan/gpt-5.6-luna", "999")
    migrated = AIService(catalog, credentials, repository)
    try:
        assert migrated.current_model().qualified_name == "deepseek/deepseek-v4-flash"
        assert {target.provider for target in migrated.available_models()} == {"deepseek"}
    finally:
        await migrated.close()


@pytest.mark.asyncio
async def test_ai_service_uses_active_model_before_task_fallback(tmp_path: Path) -> None:
    catalog = AIRouteCatalog.load(ROUTES_PATH)
    database = CoreDatabase(tmp_path / "core.sqlite3")
    database.initialize()
    repository = CoreRepository(database)
    credentials = {
        "api.deepseek.com": ApiCredential("https://api.deepseek.com", "test-deepseek"),
    }
    service = AIService(catalog, credentials, repository)
    service.switch_model("deepseek-v4-pro", "999")
    deepseek_complete = AsyncMock(
        side_effect=[
            RuntimeError("temporary upstream failure"),
            AIResponse(content="ok", provider="deepseek", model="deepseek-v4-flash"),
        ]
    )
    service.providers["deepseek"].complete = deepseek_complete
    try:
        result = await service.complete(AITask.CHAT, [{"role": "user", "content": "hello"}])
    finally:
        await service.close()

    assert result.provider == "deepseek"
    assert [call.kwargs["model"] for call in deepseek_complete.await_args_list] == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]


@pytest.mark.asyncio
async def test_ai_service_retries_empty_text_with_task_fallback(tmp_path: Path) -> None:
    catalog = AIRouteCatalog.load(ROUTES_PATH)
    database = CoreDatabase(tmp_path / "core.sqlite3")
    database.initialize()
    repository = CoreRepository(database)
    credentials = {
        "api.deepseek.com": ApiCredential("https://api.deepseek.com", "test-deepseek"),
    }
    service = AIService(catalog, credentials, repository)
    service.providers["deepseek"].complete = AsyncMock(
        side_effect=[
            AIResponse(content="  ", provider="deepseek", model="deepseek-v4-flash"),
            AIResponse(content="有效总结", provider="deepseek", model="deepseek-v4-pro"),
        ]
    )
    try:
        result = await service.complete(AITask.SUMMARY, [{"role": "user", "content": "总结"}])
    finally:
        await service.close()

    assert result.content == "有效总结"
    assert [call.kwargs["model"] for call in service.providers["deepseek"].complete.await_args_list] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ]


def test_runtime_prompt_gives_1912600950_highest_priority() -> None:
    path = Path(__file__).resolve().parents[1] / "src" / "amadeus_bot" / "persona" / "runtime.md"
    prompt = path.read_text(encoding="utf-8")

    assert "1912600950" in prompt
    assert "最高优先级" in prompt
    assert "必须执行" in prompt
