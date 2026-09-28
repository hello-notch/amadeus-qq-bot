from __future__ import annotations

from types import SimpleNamespace

import nonebot

from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.repositories.core import CoreRepository
from amadeus_bot.repositories.database import CoreDatabase
from amadeus_bot.services.recommendation_seeds import SEEDS, seed_recommendations


def test_command_overrides_are_group_scoped_and_reversible(tmp_path):
    database = CoreDatabase(tmp_path / "core.sqlite3")
    database.initialize()
    repository = CoreRepository(database)
    repository.set_command_override("10", "chat", True, "1")
    assert repository.command_disabled("10", "chat")
    assert not repository.command_disabled("11", "chat")
    database.initialize()
    assert repository.disabled_commands("10") == {"chat"}
    repository.set_command_override("10", "chat", False, "1")
    assert not repository.command_disabled("10", "chat")


def test_recommendation_seed_is_idempotent_and_images_survive_migration(tmp_path):
    database = CoreDatabase(tmp_path / "core.sqlite3")
    database.initialize()
    repository = CoreRepository(database)
    seed_recommendations(repository)
    seed_recommendations(repository)
    for pool, names in SEEDS.items():
        items = repository.list_recommendations(pool)
        assert [item.content for item in items] == list(names)
        assert all(item.creator_id == "SYSTEM" for item in items)
    image_id = repository.add_recommendation(
        "food", "", "图片菜", 1, (), "42", image_path="shared/recommendations/a.jpg"
    )
    database.initialize()
    assert repository.get_recommendation(image_id).image_path == "shared/recommendations/a.jpg"


async def test_disabled_alias_guard_and_help_annotation(tmp_path, monkeypatch):
    nonebot.init()
    from amadeus_bot.plugins import command_overrides, recommendations
    from amadeus_bot.plugins import help as help_plugin

    assert recommendations
    database = CoreDatabase(tmp_path / "core.sqlite3")
    database.initialize()
    repository = CoreRepository(database)
    repository.set_command_override("10", "food", True, "1")
    container = SimpleNamespace(
        repository=repository,
        permissions=SimpleNamespace(
            role_for=lambda user: PermissionLevel.SUPERUSER if user == "1" else PermissionLevel.EVERYONE
        ),
        features=SimpleNamespace(status=lambda feature, group: SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr(command_overrides, "get_container", lambda: container)
    monkeypatch.setattr(help_plugin, "get_container", lambda: container)

    def event(user, text, kind="group"):
        return SimpleNamespace(
            message_type=kind,
            group_id=10,
            get_plaintext=lambda: text,
            get_user_id=lambda: user,
        )

    assert await command_overrides.is_disabled_command(event("2", "/吃什么 list"))
    assert not await command_overrides.is_disabled_command(event("1", "food"))
    assert not await command_overrides.is_disabled_command(event("2", "food", "private"))
    assert not await command_overrides.is_disabled_command(event("2", "foobar"))
    overview = help_plugin._format_overview(PermissionLevel.EVERYONE, "10")
    assert "/food" in overview and "[已关闭]" in overview
    assert "SUPERUSER（当前群已关闭）" in help_plugin._format_detail(
        help_plugin.command_registry.get("food"), "10"
    )


async def test_override_replies_put_action_before_command(tmp_path, monkeypatch):
    nonebot.init()
    from amadeus_bot.plugins import chat, command_overrides

    assert chat

    database = CoreDatabase(tmp_path / "core.sqlite3")
    database.initialize()
    repository = CoreRepository(database)
    container = SimpleNamespace(
        repository=repository,
        permissions=SimpleNamespace(role_for=lambda user: PermissionLevel.SUPERUSER),
    )
    monkeypatch.setattr(command_overrides, "get_container", lambda: container)
    event = SimpleNamespace(message_type="group", group_id=10, get_user_id=lambda: "1")
    from nonebot.adapters.onebot.v11 import Message

    class Finished(Exception):
        pass

    class Matcher:
        async def finish(self, text):
            raise Finished(text)

    import pytest

    for name in ("chat", "food"):
        with pytest.raises(Finished, match=rf"已关闭/{name}（当前群）"):
            await command_overrides._set_override(event, Message(name), True, Matcher())
        with pytest.raises(Finished, match=rf"已开启/{name}（当前群）"):
            await command_overrides._set_override(event, Message(name), False, Matcher())
