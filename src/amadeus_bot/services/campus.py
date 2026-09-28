from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx

from amadeus_bot.repositories.core import CoreRepository
from amadeus_bot.services.campus_auth import CampusAuthenticator, CampusSessionExpired


@dataclass(frozen=True, slots=True)
class CampusItem:
    item_id: str
    title: str
    published_at: str | None
    department: str
    summary: str
    url: str
    metadata: dict

    def as_repository_item(self) -> dict:
        payload = {
            "item_id": self.item_id,
            "title": self.title,
            "published_at": self.published_at,
            "department": self.department,
            "summary": self.summary,
            "url": self.url,
            "metadata": self.metadata,
        }
        payload["content_hash"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return payload


def format_source_timestamp(value: str | None) -> str:
    if not value:
        return "未知"
    instant = datetime.fromisoformat(value).replace(tzinfo=UTC)
    return instant.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M 北京时间")


def format_activity_time(value: str | None) -> str:
    if not value:
        return "时间未公布"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if instant.tzinfo is not None:
        instant = instant.astimezone(ZoneInfo("Asia/Shanghai"))
    return instant.strftime("%Y-%m-%d %H:%M")


class PortalSource:
    LIST_URL = "http://my.bupt.edu.cn/list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1154"

    def __init__(self, repository: CoreRepository) -> None:
        self.repository = repository

    async def refresh(self) -> tuple[int, int]:
        try:
            return await self._refresh_once()
        except CampusSessionExpired:
            authenticator = CampusAuthenticator()
            if not authenticator.available:
                raise
            await authenticator.login_portal()
            return await self._refresh_once()

    async def _refresh_once(self) -> tuple[int, int]:
        cookie_header = load_cookie_header_file(os.getenv("AMADEUS_PORTAL_COOKIE_FILE"))
        headers = {"Cookie": cookie_header} if cookie_header else {}
        items: list[CampusItem] = []
        seen: set[str] = set()
        queue = [self.LIST_URL]
        visited: set[str] = set()
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers=headers,
        ) as client:
            while queue and len(visited) < 5 and len(items) < 50:
                page_url = queue.pop(0)
                if page_url in visited:
                    continue
                visited.add(page_url)
                response = await client.get(page_url)
                response.raise_for_status()
                html_text = decode_portal_html(response.content)
                if "authserver/login" in str(response.url) or "CAS Login" in html_text:
                    raise CampusSessionExpired("信息门户登录已失效")
                for item in parse_portal_list(html_text, str(response.url)):
                    if item.item_id not in seen:
                        items.append(item)
                        seen.add(item.item_id)
                    if len(items) == 50:
                        break
                queue.extend(
                    url
                    for url in portal_page_urls(html_text, str(response.url))
                    if url not in visited and url not in queue
                )
        if not items:
            raise RuntimeError("信息门户列表为空，可能是页面结构变化")
        new_count = sum(
            self.repository.upsert_source_item("portal", item.as_repository_item()) for item in items
        )
        self.repository.prune_source_items("portal", [item.item_id for item in items])
        self.repository.set_source_health("portal", success=True, item_count=len(items))
        return len(items), new_count


class ActivitySource:
    BASE_URL = "https://dekt.bupt.edu.cn"

    def __init__(self, repository: CoreRepository) -> None:
        self.repository = repository

    async def refresh(self) -> tuple[int, int]:
        endpoint = os.getenv("AMADEUS_ACTIVITY_LIST_ENDPOINT", "/api/v1/activity").strip()
        if not endpoint:
            raise RuntimeError(
                "尚未配置只读活动列表接口；当前已确认平台使用 Bearer token，但未确认学生端列表路径"
            )
        if not endpoint.startswith("/api/"):
            raise RuntimeError("活动接口必须是站内 /api/... 路径")

        token_file = os.getenv("AMADEUS_ACTIVITY_TOKEN_FILE", "secrets/activity-token.txt")
        token = _load_secret_file(token_file)
        authenticator = CampusAuthenticator()
        if not token:
            if not authenticator.available:
                raise RuntimeError("第二课堂 token 缺失，且未配置可用于自动续登的密码文件")
            await authenticator.login_activity()
            token = _load_secret_file(token_file)

        try:
            return await self._refresh_once(endpoint, token)
        except CampusSessionExpired:
            if not authenticator.available:
                raise
            await authenticator.login_activity()
            refreshed_token = _load_secret_file(token_file)
            if not refreshed_token or refreshed_token == token:
                raise RuntimeError("第二课堂自动续登后未获得新 token") from None
            return await self._refresh_once(endpoint, refreshed_token)

    async def _refresh_once(self, endpoint: str, token: str) -> tuple[int, int]:
        async with httpx.AsyncClient(base_url=self.BASE_URL, timeout=20.0) as client:
            response = await client.get(
                endpoint,
                params=(
                    {
                        "college_id": "0",
                        "grade": "0",
                        "class_id": "0",
                        "role_id": "0",
                        "page": "1",
                        "page_size": "50",
                    }
                    if endpoint == "/api/v1/activity"
                    else {"act_state": 0, "page": 1, "page_size": 50}
                ),
                headers={"Authorization": f"Bearer {token}"},
            )
            real_status = int(response.headers.get("x-real-status") or response.status_code)
            if real_status in {401, 403}:
                raise CampusSessionExpired("第二课堂登录已失效")
            if real_status >= 400:
                raise RuntimeError(f"第二课堂活动列表失败（HTTP {real_status}）")
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("第二课堂活动接口返回了非 JSON 响应") from exc
            items = normalize_activity_payload(payload)
            if endpoint == "/api/v1/activity" and isinstance(payload, dict):
                rows = payload.get("data")
                if isinstance(rows, list) and len(rows) == len(items):
                    semaphore = asyncio.Semaphore(4)

                    async def enrich(row: dict, item: CampusItem) -> CampusItem:
                        if (
                            not str(row.get("id") or "").isdigit()
                            or not str(row.get("demands") or "").isdigit()
                            or not int(row["demands"]) & 1
                            or not str(row.get("attend_limit") or "").isdigit()
                            or int(row["attend_limit"]) <= 0
                            or row.get("attend_count") is not None
                        ):
                            return item
                        async with semaphore:
                            try:
                                detail_response = await client.get(
                                    f"{endpoint}/{row['id']}",
                                    headers={"Authorization": f"Bearer {token}"},
                                )
                                detail_status = int(
                                    detail_response.headers.get("x-real-status")
                                    or detail_response.status_code
                                )
                                if detail_status in {401, 403}:
                                    raise CampusSessionExpired("第二课堂登录已失效")
                                if detail_status >= 400:
                                    return item
                                envelope = detail_response.json()
                                if (
                                    not isinstance(envelope, dict)
                                    or envelope.get("success") is False
                                    or envelope.get("error") not in (None, False, "")
                                ):
                                    return item
                                detail = envelope.get("data")
                                if (
                                    not isinstance(detail, dict)
                                    or str(detail.get("id")) != str(row["id"])
                                    or detail.get("attend_count") is None
                                ):
                                    return item
                                return activity_from_mapping({**row, **detail})
                            except CampusSessionExpired:
                                raise
                            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                                return item

                    items = await asyncio.gather(
                        *(enrich(row, item) for row, item in zip(rows, items, strict=True))
                    )
        return self.import_items(items)

    def import_items(self, items: list[CampusItem]) -> tuple[int, int]:
        new_count = sum(
            self.repository.upsert_source_item("activity", item.as_repository_item()) for item in items
        )
        self.repository.prune_source_items("activity", [item.item_id for item in items])
        self.repository.set_source_health("activity", success=True, item_count=len(items))
        return len(items), new_count

    def import_file(self, content: bytes, filename: str) -> tuple[int, int]:
        if filename.lower().endswith(".json"):
            raw = json.loads(content.decode("utf-8-sig"))
            items = normalize_activity_payload(raw)
        elif filename.lower().endswith(".csv"):
            rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
            items = [activity_from_mapping(row) for row in rows]
        else:
            raise ValueError("活动导入仅支持 JSON 或 CSV")
        return self.import_items(items)


def parse_portal_list(html_text: str, base_url: str) -> list[CampusItem]:
    text = re.sub(r"<script[\s\S]*?</script>", "", html_text, flags=re.I)
    pattern = re.compile(
        r'<a[^>]+href=["\'](?P<url>[^"\']*(?:xntz_content\.jsp|wbnewsid=)[^"\']*)["\'][^>]*>'
        r"(?P<title>[\s\S]*?)</a>(?P<tail>[\s\S]{0,300})",
        re.I,
    )
    items: list[CampusItem] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        url = urljoin(base_url, unescape(match.group("url")))
        id_match = re.search(r"wbnewsid=(\d+)", url)
        item_id = id_match.group(1) if id_match else hashlib.sha256(url.encode()).hexdigest()[:20]
        if item_id in seen:
            continue
        title = _strip_html(match.group("title"))
        row_start = text.rfind("<tr", 0, match.start())
        row_end = text.find("</tr>", match.end())
        row = text[row_start : row_end + 5] if row_start >= 0 and row_end >= 0 else match.group("tail")
        date_match = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", row)
        author = re.search(
            r'<[^>]+class=["\'][^"\']*\bauthor\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
            row,
            re.I | re.S,
        )
        department = _strip_html(author.group(1)) if author else ""
        if not department:
            cells = [_strip_html(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)]
            department = next(
                (
                    cell
                    for cell in cells
                    if cell
                    and cell != title
                    and len(cell) < 100
                    and not re.search(r"20\d{2}[-/.]\d", cell)
                    and not cell.isdigit()
                ),
                "",
            )
        if not title:
            continue
        items.append(
            CampusItem(
                item_id,
                title,
                date_match.group(0).replace("/", "-") if date_match else None,
                department,
                "",
                url,
                {},
            )
        )
        seen.add(item_id)
    return items


def portal_page_urls(html_text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html_text, re.I):
        candidate = urljoin(base_url, unescape(href))
        parsed = urlparse(candidate)
        if parsed.hostname != "my.bupt.edu.cn" or not parsed.path.endswith("/list.jsp"):
            continue
        query = parse_qs(parsed.query)
        if query.get("wbtreeid") != ["1154"]:
            continue
        if any(
            re.fullmatch(r"(?:page|pagenum|pageno|pageindex|p|a\d+p)", key, re.I)
            and any(value.isdigit() and int(value) > 1 for value in values)
            for key, values in query.items()
        ):
            urls.append(candidate)
    return list(dict.fromkeys(urls))


def decode_portal_html(content: bytes) -> str:
    """Decode portal HTML without turning valid UTF-8 Chinese into mojibake."""
    # The portal has served both UTF-8 and GB18030 pages.  UTF-8 must be tried
    # first: decoding UTF-8 bytes as GB18030 often succeeds but produces the
    # characteristic ``鍖椾含`` style mojibake instead of raising an error.
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("gb18030", errors="replace")


def normalize_activity_payload(payload) -> list[CampusItem]:
    if isinstance(payload, dict):
        if (
            payload.get("success") is False
            or payload.get("error") not in (None, False, "")
            or str(payload.get("status", "ok")).lower() not in {"ok", "success", "200", "0"}
            or str(payload.get("code", "0")) not in {"0", "200"}
        ):
            raise RuntimeError("第二课堂活动列表返回业务错误")
        for key in ("data", "items", "list", "records", "activities", "result"):
            value = payload.get(key)
            if key in payload:
                return normalize_activity_payload(value)
    if not isinstance(payload, list):
        raise RuntimeError("第二课堂活动列表响应形状已变化")
    if any(not isinstance(item, dict) for item in payload):
        raise RuntimeError("第二课堂活动列表条目形状已变化")
    return [activity_from_mapping(item) for item in payload if isinstance(item, dict)]


def activity_from_mapping(item: dict) -> CampusItem:
    item_id = _first(item, "activity_id", "act_id", "id", "活动ID")
    title = _first(item, "name", "title", "activity_name", "act_name", "活动名称")
    if not item_id or not title:
        raise ValueError("活动数据必须包含活动 ID 和名称")
    published = (
        _first(item, "activity_start_time", "start_time", "activity_time", "begin_at", "活动时间") or None
    )
    area = {"0": "西土城校区", "1": "沙河校区"}.get(str(item.get("area")), "")
    campus = _first(item, "campus", "校区") or area
    demands = item.get("demands")
    status = _first(item, "status", "state", "状态")
    if demands is not None and str(demands).isdigit():
        bits = int(demands)
        status = " · ".join(
            (
                "需报名" if bits & 1 else "不报名",
                "需签到" if bits & 2 else "不签到",
                "需签退" if bits & 4 else "不签退",
            )
        )
        try:
            if (
                bits & 1
                and int(item.get("attend_limit") or 0) > 0
                and int(item.get("attend_count") or 0) >= int(item["attend_limit"])
            ):
                status += " · 人数已满"
        except (TypeError, ValueError):
            pass
    metadata = {
        "category": _first(item, "category", "class_name", "type", "类别"),
        "campus": campus,
        "location": _first(item, "location", "address", "地点"),
        "event_end": _first(item, "activity_end_time", "end_time", "finish_at"),
        "registration_start": _first(
            item, "register_start_time", "registration_start_time", "signup_start_time", "报名开始"
        ),
        "registration_end": _first(
            item, "register_end_time", "registration_end_time", "signup_end_time", "报名结束"
        ),
        "capacity": _first(item, "attend_limit", "capacity", "quota", "名额"),
        "status": status,
    }
    return CampusItem(
        str(item_id),
        str(title),
        str(published) if published else None,
        str(_first(item, "organizer", "organizer_name", "department", "sponsor", "sponsor_name", "主办方")),
        str(_first(item, "summary", "description", "简介")),
        str(_first(item, "url", "link", "详情链接")),
        metadata,
    )


def load_cookie_header_file(value: str | None) -> str:
    """Load an exact Cookie request header while accepting legacy JSON files."""
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_file():
        return ""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return "; ".join(f"{key}={item}" for key, item in payload.items())
    if "\r" in raw or "\n" in raw:
        raise RuntimeError("Cookie 文件必须只包含单行请求头")
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].lstrip()
    if not all("=" in part for part in raw.split(";") if part.strip()):
        raise RuntimeError("Cookie 请求头格式无效")
    return "; ".join(part.strip() for part in raw.split(";") if part.strip())


def _load_secret_file(value: str | None) -> str:
    if not value:
        return ""
    path = Path(value).expanduser()
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def _first(item: dict, *keys: str):
    for key in keys:
        if item.get(key) not in {None, ""}:
            return item[key]
    return ""


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", value))).strip()
