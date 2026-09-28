from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from nonebot.log import logger

from amadeus_bot.domain.ai import AIResponse, ToolCall


@dataclass(frozen=True, slots=True)
class ApiCredential:
    base_url: str
    api_key: str

    @property
    def host(self) -> str:
        return httpx.URL(self.base_url).host


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    credential_host: str
    api_prefix: str
    api_mode: str = "chat_completions"
    responses_stream: bool = False
    thinking_enabled: bool = True
    models: tuple[str, ...] = ()


class CredentialStore:
    @staticmethod
    def load(path: Path) -> dict[str, ApiCredential]:
        if not path.is_file():
            return {}
        credentials: dict[str, ApiCredential] = {}
        pending_key: str | None = None
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^([^:=]+)\s*[:=]\s*(.*)$", line)
            if match is None:
                continue
            normalized_label = match.group(1).strip().lower()
            value = match.group(2).strip()
            if normalized_label == "apikey":
                pending_key = value
            elif normalized_label == "url" and pending_key:
                credential = ApiCredential(base_url=value.rstrip("/"), api_key=pending_key)
                credentials[credential.host] = credential
                pending_key = None
        return credentials


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: ProviderConfig,
        credential: ApiCredential,
        *,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.config = config
        self.credential = credential
        prefix = config.api_prefix.strip()
        self.api_base = credential.base_url.rstrip("/") + ("/" + prefix.strip("/") if prefix else "")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Authorization": f"Bearer {credential.api_key}"},
        )

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> AIResponse:
        if self.config.api_mode == "responses":
            return await self._complete_responses(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
            )
        return await self._complete_chat(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
        )

    async def _complete_chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
    ) -> AIResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": _chat_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not self.config.thinking_enabled:
            payload["thinking"] = {"type": "disabled"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        response = await self._client.post(f"{self.api_base}/chat/completions", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"AI provider {self.config.name} returned HTTP {response.status_code}")
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"AI provider {self.config.name} returned no choices")
        message = choices[0].get("message") or {}
        # reasoning_content is internal model reasoning and must not be shown to QQ users.
        content = message.get("content") or ""
        tool_calls = tuple(_parse_tool_call(item) for item in message.get("tool_calls") or [])
        usage = data.get("usage") or {}
        if not str(content).strip() and not tool_calls:
            details = usage.get("completion_tokens_details") or {}
            reason = (
                f"AI provider {self.config.name} returned empty output "
                f"(finish_reason={choices[0].get('finish_reason')}, "
                f"completion_tokens={usage.get('completion_tokens')}, "
                f"reasoning_tokens={details.get('reasoning_tokens')})"
            )
            logger.warning("{}", reason)
            raise RuntimeError(reason)
        prompt_details = usage.get("prompt_tokens_details") or {}
        return AIResponse(
            content=str(content),
            provider=self.config.name,
            model=model,
            response_id=str(data.get("id") or "") or None,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
            raw_usage=usage,
            tool_calls=tool_calls,
        )

    async def _complete_responses(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
    ) -> AIResponse:
        payload: dict[str, Any] = {
            "model": model,
            "input": _responses_input(messages),
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "store": False,
        }
        if tools:
            payload["tools"] = [_responses_tool(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        if self.config.responses_stream:
            payload["stream"] = True
            data = await self._complete_responses_stream(payload)
        else:
            response = await self._client.post(f"{self.api_base}/responses", json=payload)
            if response.status_code >= 400:
                raise RuntimeError(_provider_error(self.config.name, response))
            data = response.json()
        output = data.get("output") or []
        content = "".join(_responses_text(item) for item in output)
        tool_calls = tuple(
            _parse_responses_tool_call(item)
            for item in output
            if isinstance(item, dict) and item.get("type") == "function_call"
        )
        if not content and not tool_calls:
            raise RuntimeError(f"AI provider {self.config.name} returned no output")
        usage = data.get("usage") or {}
        input_details = usage.get("input_tokens_details") or {}
        return AIResponse(
            content=content,
            provider=self.config.name,
            model=str(data.get("model") or model),
            response_id=str(data.get("id") or "") or None,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cached_tokens=int(input_details.get("cached_tokens") or 0),
            raw_usage=usage,
            tool_calls=tool_calls,
            output_items=tuple(dict(item) for item in output if isinstance(item, dict)),
        )

    async def _complete_responses_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        response_data: dict[str, Any] | None = None
        metadata: dict[str, Any] = {}
        output_items: list[dict[str, Any]] = []
        async with self._client.stream(
            "POST",
            f"{self.api_base}/responses",
            json=payload,
            headers={"Accept": "text/event-stream"},
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise RuntimeError(_provider_error(self.config.name, response))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw_data = line.removeprefix("data:").strip()
                if not raw_data or raw_data == "[DONE]":
                    continue
                try:
                    event = json.loads(raw_data)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type") or "")
                if event_type in {"response.created", "response.in_progress"}:
                    current = event.get("response")
                    if isinstance(current, dict):
                        metadata.update(current)
                elif event_type == "response.output_item.done":
                    item = event.get("item")
                    if isinstance(item, dict):
                        output_items.append(item)
                elif event_type == "response.completed":
                    current = event.get("response")
                    if isinstance(current, dict):
                        response_data = current
                    break
                elif event_type in {"error", "response.failed"}:
                    raise RuntimeError(_responses_stream_error(self.config.name, event))
        if response_data is not None:
            if not response_data.get("output") and output_items:
                response_data["output"] = output_items
            return response_data
        if output_items:
            metadata["output"] = output_items
            metadata["status"] = "completed"
            return metadata
        raise RuntimeError(f"AI provider {self.config.name} returned no completed response")

    async def close(self) -> None:
        await self._client.aclose()


def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    function = raw.get("function") or {}
    arguments_raw = function.get("arguments") or "{}"
    if isinstance(arguments_raw, str):
        try:
            arguments = json.loads(arguments_raw)
        except json.JSONDecodeError:
            arguments = {}
    elif isinstance(arguments_raw, dict):
        arguments = arguments_raw
    else:
        arguments = {}
    return ToolCall(
        call_id=str(raw.get("id") or ""),
        name=str(function.get("name") or ""),
        arguments=arguments,
    )


def _chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in message.items() if not key.startswith("_")} for message in messages]


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        raw_output = message.get("_responses_output")
        if isinstance(raw_output, list):
            items.extend(dict(item) for item in raw_output if isinstance(item, dict))
            continue
        role = str(message.get("role") or "")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        content = message.get("content")
        if content:
            items.append({"role": role, "content": _responses_content(content)})
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments") or "{}"
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            items.append(
                {
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                }
            )
    return items


def _responses_content(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": str(content)}]
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
        elif isinstance(part, dict):
            converted = dict(part)
            if converted.get("type") == "text":
                converted["type"] = "input_text"
            elif converted.get("type") == "image_url":
                image = converted.get("image_url")
                converted = {
                    "type": "input_image",
                    "image_url": image.get("url") if isinstance(image, dict) else image,
                }
            parts.append(converted)
    return parts


def _responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
        return dict(tool)
    function = dict(tool["function"])
    function.setdefault("strict", False)
    return {"type": "function", **function}


def _responses_text(item: object) -> str:
    if not isinstance(item, dict) or item.get("type") != "message":
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") == "output_text"
    )


def _parse_responses_tool_call(raw: dict[str, Any]) -> ToolCall:
    arguments_raw = raw.get("arguments") or "{}"
    if isinstance(arguments_raw, str):
        try:
            arguments = json.loads(arguments_raw)
        except json.JSONDecodeError:
            arguments = {}
    elif isinstance(arguments_raw, dict):
        arguments = arguments_raw
    else:
        arguments = {}
    return ToolCall(
        call_id=str(raw.get("call_id") or raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        arguments=arguments,
    )


def _provider_error(provider_name: str, response: httpx.Response) -> str:
    code = ""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        code = str(data["error"].get("code") or data["error"].get("type") or "")
    suffix = f" ({code})" if code else ""
    return f"AI provider {provider_name} returned HTTP {response.status_code}{suffix}"


def _responses_stream_error(provider_name: str, event: dict[str, Any]) -> str:
    error = event.get("error")
    if not isinstance(error, dict):
        response = event.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    code = ""
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("type") or "")
    suffix = f" ({code})" if code else ""
    return f"AI provider {provider_name} returned a Responses stream error{suffix}"
