import json
from datetime import datetime
from pathlib import Path

from amadeus_bot.repositories.core import CoreRepository
from amadeus_bot.repositories.database import CoreDatabase
from amadeus_bot.repositories.user_data import UserDataRepository
from amadeus_bot.services.ddl import DDLService
from amadeus_bot.services.feature_flags import FeatureFlagService
from amadeus_bot.services.interactions import InteractionPacer
from amadeus_bot.services.tools import AIToolService, ToolExecutionContext


def make_tools(path: Path) -> AIToolService:
    database = CoreDatabase(path / "core.sqlite3")
    database.initialize()
    core = CoreRepository(database)
    return AIToolService(
        DDLService(UserDataRepository(path / "users")),
        core,
        FeatureFlagService(core),
        interactions=InteractionPacer({"poke": 0, "stick": 0}),
    )


async def test_ai_tool_subject_must_equal_requester(tmp_path: Path) -> None:
    tools = make_tools(tmp_path)
    result = json.loads(
        await tools.execute(
            "ddl_list",
            {},
            ToolExecutionContext(requested_by="100", subject_user_id="200", group_id=None),
        )
    )
    assert result["success"] is False


async def test_ai_member_delegate_is_limited_to_registered_tool(tmp_path: Path) -> None:
    tools = make_tools(tmp_path)
    context = ToolExecutionContext.for_requester("100", "1")

    added = json.loads(
        await tools.execute(
            "recommend_add",
            {"pool": "food", "content": "咖喱饭", "weight": 1},
            context,
        )
    )
    assert added["success"] is True
    assert "delegated_capability" not in added

    rejected = json.loads(await tools.execute("feature_disable", {"feature": "chat"}, context))
    assert rejected["success"] is False


class FakeBot:
    self_id = 999

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, name: str, **arguments):
        self.calls.append((name, arguments))
        return {}

    async def get_group_member_list(self, *, group_id: int):
        assert group_id == 1
        return [
            {"user_id": 10, "nickname": "群主", "card": "", "role": "owner"},
            {"user_id": 20, "nickname": "管理员", "card": "管理名片", "role": "admin"},
            {"user_id": 30, "nickname": "群员", "card": "", "role": "member"},
        ]


async def test_ai_interaction_schemas_are_batch_capable(tmp_path: Path) -> None:
    schemas = {item["function"]["name"]: item["function"] for item in make_tools(tmp_path).schemas()}

    assert {"recent_group_messages", "group_member_list", "poke", "stick"} <= schemas.keys()
    assert "poke_once" not in schemas
    assert "stick_once" not in schemas
    assert schemas["poke"]["parameters"]["properties"]["target_user_ids"]["type"] == "array"
    assert schemas["stick"]["parameters"]["properties"]["message_ids"]["type"] == "array"


async def test_ai_can_query_group_member_roles_and_poke_multiple_targets(tmp_path: Path) -> None:
    tools = make_tools(tmp_path)
    bot = FakeBot()
    context = ToolExecutionContext.for_requester("100", "1", bot=bot, current_message_id="42")

    members = json.loads(await tools.execute("group_member_list", {"role": "admin"}, context))
    assert members["members"] == [
        {
            "user_id": "20",
            "display_name": "管理名片",
            "role": "admin",
            "title": "",
            "is_robot": False,
        }
    ]

    poked = json.loads(await tools.execute("poke", {"target_user_ids": ["20", "30"], "count": 1}, context))
    assert poked["success"] is True
    assert poked["successes"] == 2
    assert bot.calls == [
        ("group_poke", {"group_id": 1, "user_id": 20}),
        ("group_poke", {"group_id": 1, "user_id": 30}),
    ]


async def test_ai_stick_defaults_to_current_message_and_accepts_arbitrary_batch(tmp_path: Path) -> None:
    tools = make_tools(tmp_path)
    bot = FakeBot()
    context = ToolExecutionContext.for_requester("101", "1", bot=bot, current_message_id="42")

    current = json.loads(await tools.execute("stick", {"emoji_id": 387}, context))
    batch = json.loads(await tools.execute("stick", {"emoji_id": 387, "message_ids": ["-11", "12"]}, context))
    replied = json.loads(
        await tools.execute(
            "stick",
            {"emoji_id": 387},
            ToolExecutionContext.for_requester(
                "101", "1", bot=bot, current_message_id="42", replied_message_id="99"
            ),
        )
    )

    assert current["succeeded_message_ids"] == ["42"]
    assert batch["succeeded_message_ids"] == ["-11", "12"]
    assert replied["succeeded_message_ids"] == ["99"]
    assert bot.calls == [
        ("set_msg_emoji_like", {"message_id": 42, "emoji_id": 387, "set": True}),
        ("set_msg_emoji_like", {"message_id": -11, "emoji_id": 387, "set": True}),
        ("set_msg_emoji_like", {"message_id": 12, "emoji_id": 387, "set": True}),
        ("set_msg_emoji_like", {"message_id": 99, "emoji_id": 387, "set": True}),
    ]


async def test_ai_can_fetch_recent_message_ids_and_cq_mentions(tmp_path: Path) -> None:
    tools = make_tools(tmp_path)
    now = datetime.now().astimezone()
    directory = tmp_path / "logs" / "messages" / "1"
    directory.mkdir(parents=True)
    row = {
        "message_id": "590771942",
        "user_id": "1912600950",
        "group_id": "1",
        "plain_text": "戳一下 ",
        "timestamp": int(now.timestamp()),
        "segments": [
            {"type": "text", "data": {"text": "戳一下 "}},
            {"type": "at", "data": {"qq": "2323287290"}},
        ],
    }
    (directory / f"{now:%Y-%m-%d}.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = json.loads(
        await tools.execute(
            "recent_group_messages",
            {"limit": 10},
            ToolExecutionContext.for_requester("100", "1"),
        )
    )

    assert result["messages"][0]["message_id"] == "590771942"
    assert "[CQ:at,qq=2323287290]" in result["messages"][0]["cq_message"]
