from pathlib import Path
from types import SimpleNamespace

import httpx
import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from amadeus_bot.domain.commands import command_registry
from amadeus_bot.domain.permissions import PermissionLevel
from amadeus_bot.repositories.group_data import GroupDataRepository
from amadeus_bot.services.event_utils import onebot_message


def _plugins():
    nonebot.init()
    from amadeus_bot.plugins import group_content, memory, privacy
    from amadeus_bot.plugins import help as help_plugin

    assert memory and privacy

    return group_content, help_plugin


def test_help_hides_admin_commands_and_formats_quote_usage() -> None:
    _, help_plugin = _plugins()
    assert "/memory" not in help_plugin._format_overview(PermissionLevel.SUPERUSER, None)
    assert "/privacy" not in help_plugin._format_overview(PermissionLevel.EVERYONE, None)
    admin = help_plugin._format_overview(PermissionLevel.SUPERUSER, None, include_superuser=True)
    assert "/memory" in admin and "/privacy" in admin
    quote = command_registry.get("quote")
    assert quote is not None
    for alias in ("qa", "ql", "qf", "qs", "qr", "qe", "qd"):
        assert command_registry.get(alias) == quote
    detail = help_plugin._format_detail(quote)
    assert "/quote add [名称]：收藏文字、图片或语音\n" in detail
    assert "/quote delete <id>：直接删除语录（仅 SUPERUSER）\n" in detail
    assert "标签" not in detail
    assert not hasattr(PermissionLevel, "OWNER_OR_MEMBER")
    assert not hasattr(PermissionLevel, "SELF_OR_SUPERUSER")


def test_quote_edit_is_public_but_delete_is_superuser_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _plugins()
    repo = GroupDataRepository(tmp_path / "groups")
    quote = repo.add_quote("1", "99", "200", "100", "旧名称", "内容")
    root = SimpleNamespace(
        group_repository=repo,
        permissions=SimpleNamespace(
            role_for=lambda actor: PermissionLevel.SUPERUSER if actor == "999" else PermissionLevel.MEMBER
        ),
        paths=SimpleNamespace(data=tmp_path),
    )
    monkeypatch.setattr(plugin, "get_container", lambda: root)
    assert plugin._quote_execute("300", "1", "edit", [str(quote.quote_id), "新名称"]) == (
        f"已更新语录 #{quote.quote_id}。"
    )
    assert repo.get_quote("1", quote.quote_id).name == "新名称"
    with pytest.raises(ValueError, match="SUPERUSER"):
        plugin._quote_execute("100", "1", "delete", [str(quote.quote_id)])
    with pytest.raises(ValueError, match="用法"):
        plugin._quote_execute("999", "1", "delete", [str(quote.quote_id), "old-token"])
    assert repo.get_quote("1", quote.quote_id) is not None
    assert plugin._quote_execute("999", "1", "delete", [str(quote.quote_id)]) == "已删除。"
    assert repo.get_quote("1", quote.quote_id) is None
    assert plugin._quote_execute("999", "1", "delete", [str(quote.quote_id)]) == "语录不存在。"


def test_privacy_subject_is_explicit_and_validated() -> None:
    _plugins()
    from amadeus_bot.plugins.privacy import _privacy_subject

    assert _privacy_subject("999", ["analysis", "off"]) == ("999", ["analysis", "off"])
    assert _privacy_subject("999", ["analysis", "off", "--user", "100"]) == ("100", ["analysis", "off"])
    with pytest.raises(ValueError, match="QQ"):
        _privacy_subject("999", ["--user", "not-a-qq"])


def test_quote_lists_only_ids_and_names_without_reading_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _plugins()
    repo = GroupDataRepository(tmp_path / "groups")
    text = repo.add_quote("1", "10", "200", "100", "文字名称", "不应展示的原文")
    picture = repo.add_quote(
        "1",
        "11",
        "200",
        "100",
        "图片名称",
        "",
        ({"path": "missing-picture.jpg"},),
    )
    voice = repo.add_quote(
        "1",
        "12",
        "200",
        "100",
        "语音名称",
        "",
        ({"path": "missing-record.silk", "type": "record"},),
    )
    unnamed = repo.add_quote("1", "13", "200", "100", "", "其他原文")
    monkeypatch.setattr(plugin, "get_container", lambda: SimpleNamespace(group_repository=repo))

    def reject_full_format(row):
        pytest.fail("列表不能读取或格式化语录正文与媒体")

    monkeypatch.setattr(plugin, "_format_quote", reject_full_format)
    assert plugin._quote_execute("100", "1", "list", []) == (
        f"#{unnamed.quote_id} 未命名\n#{voice.quote_id} 语音名称\n"
        f"#{picture.quote_id} 图片名称\n#{text.quote_id} 文字名称"
    )
    assert plugin._quote_execute("100", "1", "find", ["不应展示"]) == f"#{text.quote_id} 文字名称"
    assert plugin._quote_execute("100", "2", "list", []) == "没有匹配语录。"


def test_quote_format_contains_names_time_and_real_image_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _plugins()
    repo = GroupDataRepository(tmp_path / "groups")
    media = tmp_path / "groups" / "1" / "media" / "quotes" / "picture.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"picture")
    row = repo.add_quote(
        "1",
        "99",
        "200",
        "100",
        "标题",
        "文字",
        ({"path": str(media.relative_to(tmp_path)), "sha256": "dummy"},),
        source_author_name="小林",
        saved_by_name="小周",
    )
    monkeypatch.setattr(
        plugin, "get_container", lambda: SimpleNamespace(paths=SimpleNamespace(data=tmp_path))
    )
    message = plugin._format_quote(row)
    assert f"#{row.quote_id} 标题 · 添加者 小周(100) 添加时间：" in message.extract_plain_text()
    assert "小林(200)：文字" in message.extract_plain_text()
    assert [segment.type for segment in message][-1] == "image"


@pytest.mark.asyncio
async def test_quote_saves_raw_voice_and_sends_record_from_local_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _plugins()
    data = b"raw-silk-voice"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=data))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        plugin.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )
    container = SimpleNamespace(paths=SimpleNamespace(data=tmp_path))
    monkeypatch.setattr(plugin, "get_container", lambda: container)
    bot = SimpleNamespace(get_record=None)
    message = onebot_message(
        {"type": "voice", "data": {"file": "abc.silk", "url": "https://example.test/abc.silk"}}
    )
    refs = await plugin._save_quote_media(bot, "1", message)
    assert len(refs) == 1 and refs[0]["type"] == "record"
    assert (tmp_path / refs[0]["path"]).read_bytes() == data
    repo = GroupDataRepository(tmp_path / "groups")
    row = repo.add_quote("1", "99", "200", "100", "语音", "", refs)
    output = plugin._format_quote(row)
    assert [part.type for part in output] == ["text", "record"]
    assert output[-1].data["file"].startswith("base64://")


@pytest.mark.asyncio
async def test_quote_falls_back_to_get_record_and_rejects_missing_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _plugins()
    monkeypatch.setattr(
        plugin, "get_container", lambda: SimpleNamespace(paths=SimpleNamespace(data=tmp_path))
    )

    class Bot:
        calls = 0

        async def get_record(self, *, file: str, out_format: str) -> dict[str, str]:
            assert file == "abc.silk" and out_format == "mp3"
            self.calls += 1
            return {"base64": "bXAzLWF1ZGlv"}

    bot = Bot()
    message = Message([MessageSegment.text("前缀"), MessageSegment("record", {"file": "abc.silk"})])
    refs = await plugin._save_quote_media(bot, "1", message)
    assert bot.calls == 1
    assert refs[0]["type"] == "record"
    assert (tmp_path / refs[0]["path"]).read_bytes() == b"mp3-audio"

    class BrokenBot:
        async def get_record(self, **kwargs: str) -> dict[str, str]:
            raise RuntimeError("voice unavailable")

    with pytest.raises(ValueError, match="未收藏"):
        await plugin._save_quote_media(BrokenBot(), "1", message)


@pytest.mark.asyncio
async def test_quote_get_record_local_file_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin, _ = _plugins()
    monkeypatch.setattr(
        plugin, "get_container", lambda: SimpleNamespace(paths=SimpleNamespace(data=tmp_path))
    )
    converted = tmp_path / "converted.mp3"
    converted.write_bytes(b"converted-audio")

    class Bot:
        async def get_record(self, **kwargs: str) -> dict[str, str]:
            assert kwargs == {"file": "abc.silk", "out_format": "mp3"}
            return {"file": str(converted)}

    refs = await plugin._save_quote_media(
        Bot(), "1", onebot_message({"type": "record", "data": {"file": "abc.silk"}})
    )
    assert (tmp_path / refs[0]["path"]).read_bytes() == b"converted-audio"


@pytest.mark.asyncio
async def test_quote_rejects_oversized_voice_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _plugins()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * (plugin.MAX_QUOTE_MEDIA_BYTES + 1))
    )
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        plugin.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs)
    )
    monkeypatch.setattr(
        plugin, "get_container", lambda: SimpleNamespace(paths=SimpleNamespace(data=tmp_path))
    )
    message = onebot_message({"type": "record", "data": {"url": "https://example.test/large.silk"}})
    with pytest.raises(ValueError, match="未收藏"):
        await plugin._save_quote_media(SimpleNamespace(), "1", message)
    assert not (tmp_path / "groups" / "1").exists()


@pytest.mark.asyncio
async def test_quote_voice_html_response_falls_back_instead_of_saving_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _ = _plugins()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=b"<html>login</html>", headers={"content-type": "text/html"}
        )
    )
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        plugin.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs)
    )
    monkeypatch.setattr(
        plugin, "get_container", lambda: SimpleNamespace(paths=SimpleNamespace(data=tmp_path))
    )

    class Bot:
        async def get_record(self, **kwargs: str) -> dict[str, str]:
            return {"base64": "bXAzLWF1ZGlv"}

    message = onebot_message(
        {
            "type": "record",
            "data": {"file": "abc.silk", "url": "https://example.test/login"},
        }
    )
    refs = await plugin._save_quote_media(Bot(), "1", message)
    assert (tmp_path / refs[0]["path"]).read_bytes() == b"mp3-audio"
    assert (tmp_path / refs[0]["path"]).suffix == ".mp3"
