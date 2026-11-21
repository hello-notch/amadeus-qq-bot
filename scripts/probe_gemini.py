"""Probe the configured Gemini gateway without copying keys or touching production context."""

from __future__ import annotations

import asyncio

from amadeus_bot.adapters.ai_provider import CredentialStore
from amadeus_bot.adapters.gemini_provider import GeminiProvider
from amadeus_bot.paths import AppPaths
from amadeus_bot.services.ai_router import AIRouteCatalog


async def main() -> None:
    paths = AppPaths.discover()
    catalog = AIRouteCatalog.load(paths.ai_routes_file)
    config = catalog.providers["gemini"]
    credentials = CredentialStore.load(paths.api_key_file)
    credential = CredentialStore.for_provider(credentials, config)
    if credential is None:
        raise SystemExit("Gemini 凭据未匹配")
    provider = GeminiProvider(config, credential)

    try:
        result = await provider.complete(
            model="gemini-3.8-flash-high",
            messages=[{"role": "user", "content": "Reply only OK."}],
            temperature=0.2,
            max_tokens=8192,
        )
        print(f"chat model={result.model} nonempty={bool(result.content)}")
        tool = {
            "type": "function",
            "function": {
                "name": "calculate",
                "description": "Use to calculate arithmetic.",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            },
        }
        messages = [{"role": "user", "content": "Use the calculate tool to compute 123*456."}]
        result = await provider.complete(
            model="gemini-3.8-flash-high",
            messages=messages,
            tools=[tool],
            temperature=0.2,
            max_tokens=8192,
        )
        print(f"tools calls={len(result.tool_calls)}")
        if not result.tool_calls:
            raise RuntimeError("Gateway did not exercise tool calling")
        call = result.tool_calls[0]
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "_gemini_parts": list(result.output_items),
                    "tool_calls": [{"id": call.call_id, "function": {"name": call.name}}],
                },
                {"role": "tool", "tool_call_id": call.call_id, "content": '{"value":56088}'},
            ]
        )
        result = await provider.complete(
            model="gemini-3.8-flash-high", messages=messages, tools=[tool], temperature=0.2, max_tokens=8192
        )
        correct = "56088" in result.content.replace(",", "")
        print(f"tool replay nonempty={bool(result.content)} correct={correct}")
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
