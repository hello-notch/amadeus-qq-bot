"""Exercise issue fixes through an already-running isolated simulator (AI must be enabled)."""

from __future__ import annotations

import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with httpx.Client(base_url="http://127.0.0.1:18765", timeout=10.0) as client:
        for _ in range(90):
            if client.get("/api/state").json()["connected"]:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Simulator not connected")

        def send(scope, user, text="", extra=(), reply=None):
            segments = ([{"type": "text", "data": {"text": text}}] if text else []) + list(extra)
            response = client.post(
                "/api/send",
                json={
                    "scope": scope,
                    "user_id": user,
                    "segments": segments,
                    "reply_to": reply,
                },
            )
            response.raise_for_status()
            return response.json()["message_id"]

        def wait(message_id, predicate, label):
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                rows = client.get("/api/state").json()["messages"]
                for row in rows:
                    if row["bot"] and row["message_id"] > message_id and predicate(row):
                        print(f"PASS {label} input={message_id} output={row['message_id']}")
                        return row
                time.sleep(0.5)
            raise AssertionError(f"No expected simulator output: {label}")

        def text(row):
            return "".join(str(part["data"].get("text", "")) for part in row["segments"])

        def private(row, user):
            return row["scope"] == "private" and row["recipient_id"] == user

        regular, admin = 930000002, 930000001
        mid = send("private", admin, "/model switch gemini-3.8-flash-high")
        wait(mid, lambda row: private(row, admin) and "已切换首选模型" in text(row), "Gemini selected")
        mid = send("group", regular, "/history clear")
        wait(mid, lambda row: "只能由 SUPERUSER" in text(row), "group clear rejected")
        mid = send("group", admin, "/history clear")
        wait(mid, lambda row: "已清空当前会话" in text(row), "superuser clear")
        mid = send("private", regular, "/history clear")
        wait(mid, lambda row: private(row, regular) and "已清空" in text(row), "self private clear")
        target = send("group", regular, "测试贴表情目标")
        mid = send("group", regular, "/stick ", [{"type": "face", "data": {"id": "66"}}], target)
        wait(
            mid,
            lambda row: (
                "set_msg_emoji_like" in text(row)
                and f"'message_id': {target}" in text(row)
                and "'emoji_id': 66" in text(row)
            ),
            "reply face reaction",
        )
        mid = send("private", regular, "stick ", [{"type": "face", "data": {"id": "0"}}])
        wait(
            mid,
            lambda row: (
                "set_msg_emoji_like" in text(row)
                and f"'message_id': {mid}" in text(row)
                and "'emoji_id': 0" in text(row)
            ),
            "private current-message face reaction",
        )
        mid = send("group", regular, "/stick 66 ", [{"type": "face", "data": {"id": "66"}}], target)
        wait(mid, lambda row: "不要混合" in text(row), "mixed face argument rejected")
        mid = send("group", regular, "/summary 25")
        wait(mid, lambda row: "1～24" in text(row), "summary hour bound")
        mid = send("private", regular, "/summary 1")
        wait(mid, lambda row: "群聊" in text(row), "summary private rejection")
        mid = send("private", regular, "/chat 请只回复测试成功，不要使用工具。")
        wait(mid, lambda row: private(row, regular) and "测试成功" in text(row), "Gemini chat")
        mid = send("private", regular, "/chat 请务必使用calculate工具计算123乘456，然后告诉我结果。")
        wait(
            mid,
            lambda row: private(row, regular) and "56088" in text(row).replace(",", ""),
            "Gemini calculate round trip",
        )
        for line in (
            "今天讨论项目进度：周五交付功能测试。",
            "明天由小林负责核对日志，小周负责写测试。",
            "结论是先完成测试，再发布机器人更新。",
        ):
            send("group", regular, line)
        mid = send("group", regular, "/summary 1")
        row = wait(
            mid,
            lambda row: row["scope"] == "group" and any(part["type"] == "image" for part in row["segments"]),
            "summary image",
        )
        file = next(part["data"]["file"] for part in row["segments"] if part["type"] == "image")
        print(f"summary image={file}")
        mid = send("private", regular, "/draw")
        wait(mid, lambda row: private(row, regular) and text(row) == "draw不可用", "private draw disabled")
        mid = send("group", regular, "/draw apple")
        wait(mid, lambda row: text(row) == "draw不可用", "group draw disabled")
        print("Simulator issue sessions complete")


if __name__ == "__main__":
    main()
