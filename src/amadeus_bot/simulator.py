"""Local OneBot V11 peer and browser UI. Never connects to QQ or NapCat."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Literal

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from amadeus_bot.paths import AppPaths

BOT_ID = 910000000
GROUP_ID = 920000000
USERS = (
    {"user_id": 930000001, "nickname": "测试管理员", "role": "owner"},
    {"user_id": 930000002, "nickname": "小林", "role": "member"},
    {"user_id": 930000003, "nickname": "小周", "role": "member"},
)
ROOT = Path(__file__).resolve().parents[2] / ".test-tmp" / "simulator"
STATIC = Path(__file__).resolve().parent / "simulator_ui"


class SendRequest(BaseModel):
    scope: Literal["group", "private"]
    user_id: int
    segments: list[dict[str, Any]] = Field(min_length=1, max_length=30)
    reply_to: int | None = None


class Simulator:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.messages: deque[dict[str, Any]] = deque(maxlen=400)
        self.by_id: dict[int, dict[str, Any]] = {}
        self.sequence = 10000
        self.ws: Any = None
        self.process: asyncio.subprocess.Process | None = None
        self.log_file: Any = None
        self.error = ""

    def next_id(self) -> int:
        self.sequence += 1
        return self.sequence

    def record(self, scope: str, user_id: int, segments: list[dict], *, bot: bool = False) -> dict:
        message_id = self.next_id()
        row = {
            "message_id": message_id,
            "time": int(time.time()),
            "scope": scope,
            "group_id": GROUP_ID if scope == "group" else None,
            "user_id": BOT_ID if bot else user_id,
            "recipient_id": user_id if scope == "private" else None,
            "segments": segments,
            "bot": bot,
        }
        if len(self.messages) == self.messages.maxlen:
            self.by_id.pop(self.messages[0]["message_id"], None)
        self.messages.append(row)
        self.by_id[message_id] = row
        return row

    def event(self, request: SendRequest) -> dict:
        if request.user_id not in {user["user_id"] for user in USERS}:
            raise ValueError("未知模拟用户")
        segments = request.segments.copy()
        if any(
            not isinstance(item, dict)
            or item.get("type") not in {"text", "at", "face", "image"}
            or not isinstance(item.get("data"), dict)
            for item in segments
        ):
            raise ValueError("消息段仅支持 text、at、face、image")
        if request.reply_to is not None:
            target = self.by_id.get(request.reply_to)
            if target is None or target["scope"] != request.scope:
                raise ValueError("回复目标不在当前会话")
            if request.scope == "private" and target["recipient_id"] != request.user_id:
                raise ValueError("不能回复其他用户的私聊")
            segments = [{"type": "reply", "data": {"id": str(request.reply_to)}}, *segments]
        row = self.record(request.scope, request.user_id, segments)
        sender = next(user for user in USERS if user["user_id"] == request.user_id)
        payload = {
            "time": row["time"],
            "self_id": BOT_ID,
            "post_type": "message",
            "message_type": request.scope,
            "sub_type": "normal" if request.scope == "group" else "friend",
            "message_id": row["message_id"],
            "user_id": request.user_id,
            "message": segments,
            "raw_message": "".join(str(item["data"].get("text", "")) for item in segments),
            "font": 0,
            "sender": sender,
        }
        if request.scope == "group":
            payload["group_id"] = GROUP_ID
        return payload

    def handle_api(self, action: str, params: dict) -> dict:
        if action in {"send_msg", "send_group_msg", "send_private_msg"}:
            scope = (
                params.get("message_type")
                if action == "send_msg"
                else ("group" if action == "send_group_msg" else "private")
            )
            target = params.get("group_id") if scope == "group" else params.get("user_id")
            if scope not in {"group", "private"} or int(target or 0) not in (
                {GROUP_ID} if scope == "group" else {user["user_id"] for user in USERS}
            ):
                raise ValueError("模拟器拒绝向非模拟会话发送消息")
            value = params.get("message") or []
            segments = [{"type": "text", "data": {"text": value}}] if isinstance(value, str) else value
            if not isinstance(segments, list):
                raise ValueError("无效消息格式")
            row = self.record(scope, int(target) if scope == "private" else 0, segments, bot=True)
            return {"message_id": row["message_id"]}
        if action == "get_msg":
            row = self.by_id.get(int(params.get("message_id") or 0))
            if row is None:
                raise ValueError("消息不存在")
            return {
                "time": row["time"],
                "message_type": row["scope"],
                "message_id": row["message_id"],
                "real_id": row["message_id"],
                "sender": {"user_id": row["user_id"], "nickname": self.name(row["user_id"])},
                "message": row["segments"],
            }
        if action == "get_group_member_list" and int(params.get("group_id") or 0) == GROUP_ID:
            return [self.member(user) for user in USERS]
        if action == "get_group_member_info" and int(params.get("group_id") or 0) == GROUP_ID:
            user = next((u for u in USERS if u["user_id"] == int(params.get("user_id") or 0)), None)
            if user:
                return self.member(user)
        if action == "get_group_list":
            return [{"group_id": GROUP_ID, "group_name": "模拟群聊", "member_count": len(USERS)}]
        if action == "get_friend_list":
            return [{"user_id": u["user_id"], "nickname": u["nickname"]} for u in USERS]
        if action == "get_login_info":
            return {"user_id": BOT_ID, "nickname": "Amadeus（模拟）"}
        if action == "get_status":
            return {"online": True, "good": True}
        if action in {"set_msg_emoji_like", "group_poke", "friend_poke"}:
            if action == "set_msg_emoji_like" and int(params.get("message_id") or 0) not in self.by_id:
                raise ValueError("消息不存在")
            if action == "group_poke" and int(params.get("group_id") or 0) != GROUP_ID:
                raise ValueError("群不存在")
            if action != "set_msg_emoji_like" and int(params.get("user_id") or 0) not in {
                u["user_id"] for u in USERS
            }:
                raise ValueError("用户不存在")
            self.record(
                "group" if action != "friend_poke" else "private",
                int(params.get("user_id") or 0),
                [{"type": "text", "data": {"text": f"[API: {action} {params}]"}}],
                bot=True,
            )
            return {}
        raise ValueError(f"模拟器未实现 OneBot API: {action}")

    @staticmethod
    def member(user: dict) -> dict:
        return {
            **user,
            "group_id": GROUP_ID,
            "card": user["nickname"],
            "sex": "unknown",
            "age": 20,
            "join_time": 0,
            "last_sent_time": 0,
            "level": "1",
            "unfriendly": False,
            "title": "",
            "title_expire_time": 0,
            "card_changeable": False,
        }

    @staticmethod
    def name(user_id: int) -> str:
        return next((u["nickname"] for u in USERS if u["user_id"] == user_id), "Amadeus")

    async def connect(self, bot_port: int) -> None:
        url = f"ws://127.0.0.1:{bot_port}/onebot/v11/ws"
        while self.process and self.process.returncode is None:
            try:
                async with websockets.connect(url, additional_headers={"X-Self-ID": str(BOT_ID)}) as ws:
                    self.ws = ws
                    self.error = ""
                    await ws.send(
                        json.dumps(
                            {
                                "time": int(time.time()),
                                "self_id": BOT_ID,
                                "post_type": "meta_event",
                                "meta_event_type": "lifecycle",
                                "sub_type": "connect",
                            }
                        )
                    )
                    async for raw in ws:
                        call = json.loads(raw)
                        action = call.get("action", "")
                        try:
                            result = self.handle_api(action, call.get("params") or {})
                            response = {"status": "ok", "retcode": 0, "data": result}
                        except (ValueError, TypeError) as exc:
                            self.error = str(exc)
                            response = {
                                "status": "failed",
                                "retcode": 1404,
                                "data": None,
                                "message": str(exc),
                            }
                        response["echo"] = call.get("echo")
                        await ws.send(json.dumps(response, ensure_ascii=False))
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                self.error = f"等待 Bot 连接：{type(exc).__name__}"
            finally:
                self.ws = None
            await asyncio.sleep(1)
        if self.process and self.process.returncode is not None:
            self.error = f"Bot 进程已退出：{self.process.returncode}"


def isolated_environment(root: Path, bot_port: int) -> dict[str, str]:
    root = root.resolve()
    project = AppPaths.discover().project_root.resolve()
    protected = ("data", "logs", "backups", "secrets", "issues")
    if (
        root == project
        or project not in root.parents
        or any(root == project / name or project / name in root.parents for name in protected)
    ):
        raise ValueError("模拟器根目录必须是项目内独立目录")
    configured = os.environ.copy()

    def credential_path(name: str, default: str) -> str:
        path = Path(configured.get(name, default))
        return str((project / path).resolve() if not path.is_absolute() else path.resolve())

    ai_enabled = configured.get("SIMULATOR_ENABLE_AI", "").strip().lower() in {"1", "true", "yes"}
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith(("AMADEUS_", "ONEBOT_")) or key in {"SUPERUSERS", "ACCESS_TOKEN"}:
            env.pop(key)
    env.update(
        {
            "AMADEUS_SIMULATOR_ROOT": str(root),
            "AMADEUS_DATA_DIR": str(root / "data"),
            "AMADEUS_LOG_DIR": str(root / "logs"),
            "AMADEUS_API_KEY_FILE": (
                credential_path("AMADEUS_API_KEY_FILE", "secrets/apikey.txt")
                if ai_enabled
                else str(root / "missing-api-key")
            ),
            "AMADEUS_PORTAL_COOKIE_FILE": credential_path(
                "AMADEUS_PORTAL_COOKIE_FILE", "secrets/portal-cookie.txt"
            ),
            "AMADEUS_JWGL_COOKIE_FILE": credential_path(
                "AMADEUS_JWGL_COOKIE_FILE", "secrets/jwgl-cookie.txt"
            ),
            "AMADEUS_ACTIVITY_TOKEN_FILE": credential_path(
                "AMADEUS_ACTIVITY_TOKEN_FILE", "secrets/activity-token.txt"
            ),
            "AMADEUS_PASSWORD_FILE": str(root / "missing-password"),
            "AMADEUS_ACTIVITY_LIST_ENDPOINT": configured.get(
                "AMADEUS_ACTIVITY_LIST_ENDPOINT", "/api/v1/activity"
            ),
            "ONEBOT_ACCESS_TOKEN": "",
            "ONEBOT_WS_URLS": "[]",
            "SUPERUSERS": f'["{USERS[0]["user_id"]}"]',
            "HOST": "127.0.0.1",
            "PORT": str(bot_port),
            "COMMAND_START": '["/",""]',
        }
    )
    return env


def make_app(sim: Simulator, bot_port: int) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        sim.root.mkdir(parents=True, exist_ok=True)
        sim.log_file = (sim.root / "bot.log").open("a", encoding="utf-8")
        sim.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "amadeus_bot",
            cwd=AppPaths.discover().project_root,
            env=isolated_environment(sim.root, bot_port),
            stdout=sim.log_file,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        task = asyncio.create_task(sim.connect(bot_port))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            sim.process.terminate()
            try:
                await asyncio.wait_for(sim.process.wait(), 10)
            except TimeoutError:
                sim.process.kill()
                await sim.process.wait()
            sim.log_file.close()

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/style.css")
    async def style():
        return FileResponse(STATIC / "style.css")

    @app.get("/app.js")
    async def script():
        return FileResponse(STATIC / "app.js")

    @app.get("/api/state")
    async def state():
        return {
            "connected": sim.ws is not None,
            "error": sim.error,
            "users": USERS,
            "group_id": GROUP_ID,
            "bot_id": BOT_ID,
            "messages": list(sim.messages),
        }

    @app.get("/api/media/{name}")
    async def media(name: str):
        cache = (sim.root / "data" / "cache" / "render").resolve()
        path = (cache / name).resolve()
        if path.parent != cache or path.suffix.lower() != ".png" or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.post("/api/send")
    async def send(payload: SendRequest, request: Request):
        require_local_origin(request)
        if sim.ws is None:
            raise HTTPException(503, "Bot 尚未连接")
        try:
            event = sim.event(payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await sim.ws.send(json.dumps(event, ensure_ascii=False))
        return {"message_id": event["message_id"]}

    @app.post("/api/poke")
    async def poke(request: Request):
        require_local_origin(request)
        if sim.ws is None:
            raise HTTPException(503, "Bot 尚未连接")
        body = await request.json()
        user_id = int(body.get("user_id") or 0)
        if user_id not in {u["user_id"] for u in USERS}:
            raise HTTPException(400, "未知模拟用户")
        await sim.ws.send(
            json.dumps(
                {
                    "time": int(time.time()),
                    "self_id": BOT_ID,
                    "post_type": "notice",
                    "notice_type": "notify",
                    "sub_type": "poke",
                    "group_id": GROUP_ID,
                    "user_id": user_id,
                    "target_id": BOT_ID,
                }
            )
        )
        return {"ok": True}

    return app


def require_local_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers['host']}":
        raise HTTPException(403, "仅允许本地同源请求")


def main() -> None:
    ui_port = int(os.getenv("SIMULATOR_UI_PORT", "18765"))
    bot_port = int(os.getenv("SIMULATOR_BOT_PORT", "18080"))
    if ui_port == bot_port:
        raise SystemExit("网页与 Bot 端口不能相同")
    for port in (ui_port, bot_port):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise SystemExit(f"本地端口 {port} 已占用") from exc
    sim = Simulator()
    print(f"模拟群聊与私聊：http://127.0.0.1:{ui_port}", flush=True)
    uvicorn.run(make_app(sim, bot_port), host="127.0.0.1", port=ui_port, log_level="warning")


if __name__ == "__main__":
    main()
