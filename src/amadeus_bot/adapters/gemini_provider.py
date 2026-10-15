from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from amadeus_bot.adapters.ai_provider import AIProviderRateLimited, ApiCredential, ProviderConfig
from amadeus_bot.domain.ai import AIResponse, ToolCall

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class GeminiProvider:
    def __init__(self, config: ProviderConfig, credential: ApiCredential) -> None:
        self.config = config
        self.api_base = credential.base_url.rstrip("/") + "/" + config.api_prefix.strip("/")
        self._client = httpx.AsyncClient(timeout=180.0, headers={"x-goog-api-key": credential.api_key})

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> AIResponse:
        contents, system = await self._contents(messages)
        generation: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if model in self.config.image_models:
            generation["responseModalities"] = ["TEXT", "IMAGE"]
        elif self.config.thinking_level:
            generation["thinkingConfig"] = {"thinkingLevel": self.config.thinking_level.upper()}
        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        if tools:
            payload["tools"] = [{"functionDeclarations": [tool["function"] for tool in tools]}]
        response = await self._client.post(f"{self.api_base}/models/{model}:generateContent", json=payload)
        if response.status_code == 429:
            raise AIProviderRateLimited("Gemini provider returned HTTP 429")
        if response.status_code >= 400:
            raise RuntimeError(f"AI provider {self.config.name} returned HTTP {response.status_code}")
        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"AI provider {self.config.name} returned no candidates")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text: list[str] = []
        images: list[bytes] = []
        calls: list[ToolCall] = []
        for part in parts:
            if part.get("thought"):
                continue
            if part.get("text"):
                text.append(str(part["text"]))
            if isinstance(part.get("functionCall"), dict):
                call = part["functionCall"]
                calls.append(
                    ToolCall(
                        str(call.get("id") or f"gemini-{len(calls)}"),
                        str(call.get("name") or ""),
                        dict(call.get("args") or {}),
                    )
                )
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict):
                if not str(inline.get("mimeType") or inline.get("mime_type") or "").startswith("image/"):
                    raise ValueError("Gemini returned non-image inline data")
                encoded = str(inline.get("data") or "")
                if len(encoded) > MAX_IMAGE_BYTES * 2:
                    raise ValueError("Gemini image exceeds size limit")
                image = base64.b64decode(encoded, validate=True)
                if not image or len(image) > MAX_IMAGE_BYTES:
                    raise ValueError("Gemini returned empty or oversized image")
                images.append(image)
        if not text and not calls and not images:
            raise RuntimeError(f"AI provider {self.config.name} returned empty output")
        usage = data.get("usageMetadata") or {}
        return AIResponse(
            content="".join(text),
            provider=self.config.name,
            model=model,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            cached_tokens=int(usage.get("cachedContentTokenCount") or 0),
            raw_usage=usage,
            tool_calls=tuple(calls),
            output_items=tuple(parts),
            images=tuple(images),
        )

    async def _contents(self, messages: list[dict[str, Any]]) -> tuple[list[dict], list[str]]:
        contents: list[dict] = []
        system: list[str] = []
        names: dict[str, str] = {}
        native_ids: set[str] = set()
        for message in messages:
            role = message["role"]
            if role == "system":
                system.append(str(message["content"]))
                continue
            for call in message.get("tool_calls") or []:
                names[str(call["id"])] = str(call["function"]["name"])
            for part in message.get("_gemini_parts") or []:
                native_call = part.get("functionCall") or {}
                if native_call.get("id"):
                    native_ids.add(str(native_call["id"]))
            if role == "tool":
                call_id = str(message["tool_call_id"])
                if call_id not in names:
                    raise ValueError("Gemini tool result has no matching call")
                try:
                    result = json.loads(message["content"])
                except (ValueError, TypeError):
                    result = {"result": str(message["content"])}
                function_response = {"name": names[call_id], "response": result}
                if call_id in native_ids:
                    function_response["id"] = call_id
                parts = [{"functionResponse": function_response}]
            elif message.get("_gemini_parts"):
                parts = message["_gemini_parts"]
            else:
                parts = await self._parts(message.get("content"))
                for call in message.get("tool_calls") or []:
                    function = call["function"]
                    parts.append(
                        {
                            "functionCall": {
                                "name": function["name"],
                                "args": json.loads(function["arguments"]),
                            }
                        }
                    )
            if parts:
                gemini_role = "model" if role == "assistant" else "user"
                if contents and contents[-1]["role"] == gemini_role:
                    contents[-1]["parts"].extend(parts)
                else:
                    contents.append({"role": gemini_role, "parts": list(parts)})
        return contents, system

    async def _parts(self, content: Any) -> list[dict]:
        if not content:
            return []
        if isinstance(content, str):
            return [{"text": content}]
        parts: list[dict] = []
        for part in content:
            if part.get("type") == "text":
                parts.append({"text": str(part["text"])})
            elif part.get("type") == "image_url":
                image = part["image_url"]
                url = image["url"] if isinstance(image, dict) else str(image)
                parts.append({"inlineData": await _image_data(url)})
            else:
                raise ValueError("Unsupported Gemini input part")
        return parts

    async def close(self) -> None:
        await self._client.aclose()


async def _image_data(url: str) -> dict[str, str]:
    if url.startswith("data:image/"):
        header, encoded = url.split(",", 1)
        if not header.endswith(";base64") or len(encoded) > MAX_IMAGE_BYTES * 2:
            raise ValueError("Invalid image data URI")
        data = base64.b64decode(encoded, validate=True)
        mime = header[5:].split(";", 1)[0]
    else:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not any(
            host == domain or host.endswith("." + domain) for domain in ("qq.com", "qq.com.cn", "qlogo.cn")
        ):
            raise ValueError("Gemini image URL must be a QQ HTTPS URL or data URI")
        # Never send the provider key while downloading user media.
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                mime = response.headers.get("content-type", "").split(";", 1)[0]
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_IMAGE_BYTES:
                        raise ValueError("Image exceeds size limit")
                data = bytes(chunks)
    if not mime.startswith("image/") or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Invalid image data")
    return {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}
