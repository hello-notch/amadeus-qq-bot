import importlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nonebot
import pytest

from amadeus_bot.domain.commands import command_registry
from amadeus_bot.repositories.core import CoreRepository
from amadeus_bot.repositories.database import CoreDatabase
from amadeus_bot.repositories.user_data import UserDataRepository
from amadeus_bot.services import campus, campus_auth
from amadeus_bot.services.ddl import DDLService
from amadeus_bot.services.feature_flags import FeatureFlagService
from amadeus_bot.services.tools import AIToolService, ToolExecutionContext


def make_repository(tmp_path: Path) -> CoreRepository:
    database = CoreDatabase(tmp_path / "core.sqlite3")
    database.initialize()
    return CoreRepository(database)


def test_retired_data_cleanup_is_idempotent_and_preserves_other_data(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    for source in ("activity", "portal"):
        repo.set_subscription(source, "user", "100", True, "100")
        repo.set_subscription(source, "group", "200", True, "100")
        repo.upsert_source_item(source, {"item_id": "1", "title": "cached"})
        repo.set_source_health(source, success=True, item_count=1)
        repo.database.execute(
            "INSERT INTO feature_flags(feature,scope_type,scope_id,enabled,updated_by) "
            "VALUES (?, 'global', '', 1, '100')",
            (source,),
        )
        repo.database.execute(
            "INSERT INTO command_overrides(group_id,command,actor) VALUES ('200',?,'100')", (source,)
        )
        assert repo.claim_delivery(f"campus:{source}:user:100:2026-10-21")
    recommendation_id = repo.add_recommendation("activity", "", "散步", 1, (), "100")
    repo.append_conversation("private:100", "user", "keep", "100")
    repo.database.initialize()
    repo.database.initialize()
    assert repo.list_subscriptions("activity") == []
    assert repo.query_source_items("activity") == []
    assert repo.get_source_health("activity") is None
    assert repo.get_feature_rows("activity") == []
    assert not repo.command_disabled("200", "activity")
    assert (
        repo.database.fetch_one(
            "SELECT 1 FROM scheduler_deliveries WHERE delivery_key LIKE 'campus:activity:%'"
        )
        is None
    )
    assert len(repo.list_subscriptions("portal")) == 2
    assert len(repo.query_source_items("portal")) == 1
    assert repo.get_source_health("portal")["item_count"] == 1
    assert repo.get_feature_rows("portal")
    assert repo.command_disabled("200", "portal")
    assert not repo.claim_delivery("campus:portal:user:100:2026-10-21")
    assert repo.get_recommendation(recommendation_id).content == "散步"
    assert repo.recent_conversation("private:100")
    assert repo.database.fetch_one("PRAGMA quick_check")[0] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["campus_query", "campus_subscribe"])
@pytest.mark.parametrize("source", ["activity", "unknown"])
async def test_old_ai_source_is_rejected_before_repository_access(tmp_path, monkeypatch, name, source):
    repo = make_repository(tmp_path)
    tools = AIToolService(DDLService(UserDataRepository(tmp_path / "users")), repo, FeatureFlagService(repo))
    query = Mock(side_effect=AssertionError("must not read retired cache"))
    subscribe = Mock(side_effect=AssertionError("must not create retired subscriptions"))
    monkeypatch.setattr(repo, "query_source_items", query)
    monkeypatch.setattr(repo, "set_subscription", subscribe)
    result = json.loads(
        await tools.execute(
            name, {"source": source, "enabled": True}, ToolExecutionContext.for_requester("100", None)
        )
    )
    assert not result["success"]
    query.assert_not_called()
    subscribe.assert_not_called()
    schemas = {item["function"]["name"]: item["function"] for item in tools.schemas()}
    for tool_name in ("campus_query", "campus_subscribe"):
        assert schemas[tool_name]["parameters"]["properties"]["source"]["enum"] == ["portal"]


@pytest.mark.asyncio
async def test_portal_ai_query_and_subscription_still_work(tmp_path):
    repo = make_repository(tmp_path)
    tools = AIToolService(DDLService(UserDataRepository(tmp_path / "users")), repo, FeatureFlagService(repo))
    context = ToolExecutionContext.for_requester("100", None)
    assert json.loads(await tools.execute("campus_query", {"source": "portal"}, context))["success"]
    assert json.loads(
        await tools.execute("campus_subscribe", {"source": "portal", "enabled": True}, context)
    )["success"]
    assert repo.list_subscriptions("portal")[0]["scope_id"] == "100"


def test_retired_command_authentication_and_source_are_absent(tmp_path):
    nonebot.init()
    plugin = importlib.import_module("amadeus_bot.plugins.campus")
    assert command_registry.get("portal") is not None
    assert command_registry.get("activity") is None
    assert not hasattr(plugin, "activity_command")
    assert not hasattr(campus, "ActivitySource")
    assert not hasattr(campus_auth.CampusAuthenticator, "login_activity")
    features = FeatureFlagService(make_repository(tmp_path))
    with pytest.raises(ValueError, match="未知功能"):
        features.set("activity", "global", True, "100")
    root = Path(__file__).resolve().parents[1]
    for directory in ("src", "scripts"):
        for path in (root / directory).rglob("*.py"):
            assert "dekt.bupt.edu.cn" not in path.read_text(encoding="utf-8"), path


@pytest.mark.asyncio
async def test_scheduler_refresh_and_delivery_exclude_retired_source(tmp_path, monkeypatch):
    nonebot.init()
    scheduler = importlib.import_module("amadeus_bot.plugins.scheduler")
    repo = make_repository(tmp_path)
    # Even stale records inserted after initialization must never be delivered.
    for source in ("activity", "portal"):
        repo.set_subscription(source, "user", "100", True, "100")
        repo.upsert_source_item(source, {"item_id": "1", "title": source})
    bot = SimpleNamespace(send_private_msg=AsyncMock(), send_group_msg=AsyncMock())
    container = SimpleNamespace(repository=repo, permissions=SimpleNamespace(superusers=[]))
    monkeypatch.setattr(scheduler, "get_container", lambda: container)
    monkeypatch.setattr(scheduler, "get_bots", lambda: {"test": bot})
    monkeypatch.setattr(scheduler, "_last_source_refresh", 0)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 10000)
    portal_refresh = AsyncMock(return_value=(1, 0))
    jwgl_keepalive = AsyncMock()
    monkeypatch.setattr(scheduler.PortalSource, "refresh", portal_refresh)
    monkeypatch.setattr(scheduler.JwglSource, "keepalive", jwgl_keepalive)
    await scheduler.refresh_campus_sources_if_due()
    portal_refresh.assert_awaited_once()
    jwgl_keepalive.assert_awaited_once()
    assert repo.get_source_health("activity") is None
    monkeypatch.setenv("AMADEUS_CAMPUS_PUSH_HOUR", str(datetime.now().astimezone().hour))
    assert await scheduler.dispatch_daily_subscriptions() == 1
    bot.send_private_msg.assert_awaited_once()
    assert bot.send_private_msg.await_args.kwargs["message"].startswith("portal 每日推送")
