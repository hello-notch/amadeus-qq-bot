from pathlib import Path

import pytest

from amadeus_bot.simulator import GROUP_ID, USERS, SendRequest, Simulator, isolated_environment


def test_simulator_event_and_user_scope(tmp_path: Path) -> None:
    sim = Simulator(tmp_path)
    first = sim.event(
        SendRequest(
            scope="group",
            user_id=USERS[0]["user_id"],
            segments=[{"type": "text", "data": {"text": "/calc 1+2"}}],
        )
    )
    assert first["message_type"] == "group"
    assert first["group_id"] == GROUP_ID
    assert first["sender"]["nickname"] == USERS[0]["nickname"]
    other = sim.event(
        SendRequest(
            scope="group",
            user_id=USERS[1]["user_id"],
            segments=[
                {"type": "at", "data": {"qq": str(USERS[0]["user_id"])}},
                {"type": "face", "data": {"id": "66"}},
            ],
            reply_to=first["message_id"],
        )
    )
    assert other["user_id"] != first["user_id"]
    assert [part["type"] for part in other["message"]] == ["reply", "at", "face"]
    assert (
        sim.handle_api("get_msg", {"message_id": first["message_id"]})["message"][0]["data"]["text"]
        == "/calc 1+2"
    )

    private = sim.event(
        SendRequest(
            scope="private",
            user_id=USERS[0]["user_id"],
            segments=[{"type": "text", "data": {"text": "/ddl list"}}],
        )
    )
    assert private["message_type"] == "private"
    assert "group_id" not in private
    with pytest.raises(ValueError, match="不能回复其他用户"):
        sim.event(
            SendRequest(
                scope="private",
                user_id=USERS[1]["user_id"],
                segments=[{"type": "text", "data": {"text": "hi"}}],
                reply_to=private["message_id"],
            )
        )


def test_simulator_api_rejects_real_targets_and_unknown_calls(tmp_path: Path) -> None:
    sim = Simulator(tmp_path)
    result = sim.handle_api(
        "send_private_msg",
        {"user_id": USERS[1]["user_id"], "message": [{"type": "text", "data": {"text": "hello"}}]},
    )
    assert sim.by_id[result["message_id"]]["recipient_id"] == USERS[1]["user_id"]
    with pytest.raises(ValueError, match="非模拟会话"):
        sim.handle_api("send_group_msg", {"group_id": 123456, "message": "oops"})
    with pytest.raises(ValueError, match="未实现"):
        sim.handle_api("delete_msg", {"message_id": result["message_id"]})
    with pytest.raises(ValueError, match="未知模拟用户"):
        sim.event(
            SendRequest(scope="group", user_id=123456, segments=[{"type": "text", "data": {"text": "hi"}}])
        )


def test_simulator_environment_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMADEUS_PASSWORD_FILE", "secrets/real-password")
    monkeypatch.setenv("AMADEUS_JWGL_COOKIE_FILE", "secrets/jwgl-session")
    monkeypatch.setenv("AMADEUS_ACTIVITY_LIST_ENDPOINT", "/api/v1/test-list")
    monkeypatch.delenv("SIMULATOR_ENABLE_AI", raising=False)
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "secret")
    project = Path(__file__).resolve().parents[1]
    root = project / ".test-tmp" / "simulator-test"
    env = isolated_environment(root, 18080)
    assert env["AMADEUS_DATA_DIR"] == str(root.resolve() / "data")
    assert env["AMADEUS_LOG_DIR"] == str(root.resolve() / "logs")
    assert env["AMADEUS_PASSWORD_FILE"] == str(root.resolve() / "missing-password")
    assert env["AMADEUS_API_KEY_FILE"] == str(root.resolve() / "missing-api-key")
    assert env["AMADEUS_JWGL_COOKIE_FILE"] == str(project / "secrets" / "jwgl-session")
    assert env["AMADEUS_ACTIVITY_LIST_ENDPOINT"] == "/api/v1/test-list"
    assert env["ONEBOT_ACCESS_TOKEN"] == ""
    assert env["AMADEUS_SIMULATOR_ROOT"] == str(root.resolve())
    monkeypatch.setenv("SIMULATOR_ENABLE_AI", "true")
    monkeypatch.setenv("AMADEUS_API_KEY_FILE", "secrets/simulator-ai-key")
    assert isolated_environment(root, 18080)["AMADEUS_API_KEY_FILE"] == str(
        project / "secrets" / "simulator-ai-key"
    )
    with pytest.raises(ValueError, match="独立目录"):
        isolated_environment(project / "data", 18080)
    with pytest.raises(ValueError, match="独立目录"):
        isolated_environment(project / "logs" / "simulator", 18080)


@pytest.mark.asyncio
async def test_simulator_campus_sources_can_query_without_renewing_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amadeus_bot.services import campus, jwgl
    from amadeus_bot.services.campus_auth import CampusAuthenticator, CampusSessionExpired

    monkeypatch.setenv("AMADEUS_SIMULATOR_ROOT", str(tmp_path))
    monkeypatch.setenv("AMADEUS_PASSWORD_FILE", str(tmp_path / "missing-password"))
    assert not CampusAuthenticator().available
    with pytest.raises(RuntimeError, match="模拟器不执行校园自动续登"):
        CampusAuthenticator()._load_credentials()
    calls: list[str] = []

    async def portal_once(self):
        calls.append("portal")
        return 1, 1

    async def activity_once(self, endpoint, token):
        calls.append("activity")
        assert endpoint == "/api/v1/activity"
        assert token == "test-token"
        return 1, 1

    async def jwgl_once(self, class_number, term):
        calls.append("jwgl")
        assert class_number == "12345"
        return class_number, [{"name": "测试课"}]

    monkeypatch.setattr(campus.PortalSource, "_refresh_once", portal_once)
    monkeypatch.setattr(campus.ActivitySource, "_refresh_once", activity_once)
    monkeypatch.setattr(campus, "_load_secret_file", lambda path: "test-token")
    monkeypatch.setattr(jwgl.JwglSource, "_query_class_once", jwgl_once)
    monkeypatch.setenv("AMADEUS_ACTIVITY_LIST_ENDPOINT", "/api/v1/activity")
    assert await campus.PortalSource(None).refresh() == (1, 1)
    assert await campus.ActivitySource(None).refresh() == (1, 1)
    assert (await jwgl.JwglSource().query_class("12345"))[0] == "12345"
    assert calls == ["portal", "activity", "jwgl"]

    async def expired(self):
        raise CampusSessionExpired("expired")

    monkeypatch.setattr(campus.PortalSource, "_refresh_once", expired)
    with pytest.raises(CampusSessionExpired):
        await campus.PortalSource(None).refresh()
