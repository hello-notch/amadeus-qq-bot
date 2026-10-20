from __future__ import annotations

import hashlib
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


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", value))).strip()
