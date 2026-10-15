"""Probe the configured Gemini gateway without copying keys or touching production context."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from amadeus_bot.adapters.ai_provider import CredentialStore, OpenAICompatibleProvider
from amadeus_bot.adapters.gemini_provider import GeminiProvider
from amadeus_bot.domain.ai import AITask
from amadeus_bot.paths import AppPaths
from amadeus_bot.services.ai_router import AIRouteCatalog

PROBE_DIRECTORY = Path(__file__).resolve().parents[1] / "test-tmp" / "gemini-probe"


async def main(image: bool) -> None:
    paths = AppPaths.discover()
    catalog = AIRouteCatalog.load(paths.ai_routes_file)
    config = catalog.providers["gemini"]
    credentials = CredentialStore.load(paths.api_key_file)
    provider = GeminiProvider(config, credentials[config.credential_host])

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
        if image:
            target = catalog.routes[AITask.IMAGE_GENERATION].primary
            image_config = catalog.providers[target.provider]
            image_provider = OpenAICompatibleProvider(
                image_config, credentials[image_config.credential_host], timeout_seconds=180.0
            )
            try:
                result = await image_provider.complete(
                    model=target.model,
                    messages=[
                        {
                            "role": "user",
                            "content": "Generate a small image of a red apple on white, no text.",
                        }
                    ],
                    temperature=1.0,
                    max_tokens=8192,
                )
            finally:
                await image_provider.close()
            print(f"image model={result.model} images={len(result.images)}")
            if not result.images:
                raise RuntimeError("Gateway did not return an image")
            from amadeus_bot.services.image_generation import ImageGenerationService

            service = ImageGenerationService(None, PROBE_DIRECTORY)
            output = await asyncio.to_thread(service._save, result.images[0])
            print(f"image saved={output}")
    finally:
        await provider.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="store_true")
    asyncio.run(main(parser.parse_args().image))
