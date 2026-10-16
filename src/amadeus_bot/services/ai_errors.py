from __future__ import annotations

import re

import httpx


class AIInvocationError(RuntimeError):
    """Contains only locally constructed, credential-free route diagnostics."""


def failure_reason(exc: Exception) -> str:
    if isinstance(exc, AIInvocationError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        phase = {
            httpx.ReadTimeout: "等待上游响应超时",
            httpx.ConnectTimeout: "连接上游超时",
            httpx.WriteTimeout: "发送请求超时",
            httpx.PoolTimeout: "本地连接池等待超时",
        }.get(type(exc), "请求超时")
        return f"{phase}；未收到完整响应，不能据此断定供应商宕机"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    else:
        match = re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(exc))
        status = int(match[1]) if match else None
    if status is not None:
        category = (
            "上游鉴权拒绝"
            if status in {401, 403}
            else "上游限流或配额不足"
            if status == 429
            else "上游服务错误"
            if status >= 500
            else "上游拒绝请求"
        )
        return f"HTTP {status}：{category}"
    if isinstance(exc, httpx.TransportError):
        return "网络传输失败；无法确认上游服务状态"
    if isinstance(exc, ValueError):
        return "响应格式或内容校验失败"
    if "empty output" in str(exc) or "no choices" in str(exc) or "no candidates" in str(exc):
        return "上游返回空结果或缺少候选"
    return f"调用失败（{type(exc).__name__}）；未记录敏感响应正文"
