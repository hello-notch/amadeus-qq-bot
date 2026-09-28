from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from amadeus_bot.bootstrap import build_container
from amadeus_bot.domain.ai import AITask
from amadeus_bot.paths import AppPaths
from amadeus_bot.services.tools import ToolExecutionContext

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-tool-probe-") as temporary:
        runtime = Path(temporary)
        paths = AppPaths(
            project_root=PROJECT_ROOT,
            data=runtime / "data",
            logs=runtime / "logs",
            config=PROJECT_ROOT / "config",
            api_key_file=PROJECT_ROOT / "secrets" / "apikey.txt",
            ai_routes_file=PROJECT_ROOT / "config" / "ai_routes.toml",
        )
        container = build_container(paths=paths)
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "必须使用 ddl_add 工具完成用户明确提出的 DDL 请求；"
                        "工具成功后用一句话确认，不要重复调用。"
                    ),
                },
                {
                    "role": "user",
                    "content": "帮我设置一个DDL，在明天下午三点前完成数学作业",
                },
            ]
            response = await container.ai.complete(
                AITask.CHAT,
                messages,
                user_id="100000000",
                tools=container.ai_tools.schemas(),
            )
            print(
                f"provider={response.provider} model={response.model} tool_calls={len(response.tool_calls)}"
            )
            assistant_calls = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in response.tool_calls
            ]
            assistant_message = {
                "role": "assistant",
                "content": response.content or None,
                "tool_calls": assistant_calls,
            }
            if response.output_items:
                assistant_message["_responses_output"] = list(response.output_items)
            messages.append(assistant_message)
            for call in response.tool_calls:
                result_text = await container.ai_tools.execute(
                    call.name,
                    call.arguments,
                    ToolExecutionContext.for_requester("100000000", None),
                )
                result = json.loads(result_text)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": result_text,
                    }
                )
                print(
                    f"tool={call.name} argument_keys={sorted(call.arguments)} "
                    f"success={bool(result.get('success'))}"
                )
            final = await container.ai.complete(
                AITask.CHAT,
                messages,
                user_id="100000000",
                tools=container.ai_tools.schemas(),
            )
            print(
                f"follow_up_provider={final.provider} model={final.model} "
                f"tool_calls={len(final.tool_calls)} nonempty={bool(final.content.strip())}"
            )
        finally:
            await container.close()


if __name__ == "__main__":
    asyncio.run(main())
