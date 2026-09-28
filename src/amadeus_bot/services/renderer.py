from __future__ import annotations

import asyncio
import hashlib
import html
import re
from contextlib import suppress
from pathlib import Path

from amadeus_bot.services.courses import section_periods


class RenderService:
    STYLE_VERSION = "amadeus-lists-v6"

    def __init__(self, cache_dir: Path, *, max_concurrency: int = 2) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._playwright = None
        self._browser = None
        self._browser_lock = asyncio.Lock()

    def cache_key(self, content: str, *, kind: str, variant: str = "default") -> str:
        material = "\x1f".join((self.STYLE_VERSION, kind, variant, content))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def render_text(self, content: str, *, title: str = "Amadeus", variant: str = "default") -> Path:
        body = (
            self._help_overview_html(content)
            if variant.startswith("overview:")
            else f"<pre>{html.escape(content)}</pre>"
        )
        return await self._render(
            content,
            title=title,
            variant=variant,
            kind="plain-text",
            html_body=body,
        )

    async def render_rows(
        self,
        rows: list[tuple[str, str, str]],
        *,
        title: str,
        subtitle: str = "",
        footer: str = "",
        variant: str = "rows",
    ) -> Path:
        content = repr((rows, subtitle, footer))
        cards = "".join(
            f'<div class="list-row"><span class="row-index">{html.escape(index)}</span>'
            f'<div><div class="row-title">{html.escape(label)}</div>'
            f'<div class="row-meta">{html.escape(meta)}</div></div></div>'
            for index, label, meta in rows
        )
        body = (
            f'<div class="subtitle">{html.escape(subtitle)}</div>' if subtitle else ""
        ) + f'<section class="list-rows">{cards}</section>'
        if footer:
            body += f'<div class="list-footer">{html.escape(footer)}</div>'
        return await self._render(content, title=title, variant=variant, kind="structured", html_body=body)

    async def render_timetable(
        self, courses: list, *, week: int, variant: str, pending_count: int = 0
    ) -> Path:
        section_count = max(course.end_section for course in courses)
        days = 7 if any(course.weekday >= 6 for course in courses) else 5
        periods = section_periods()
        by_day: dict[int, list] = {}
        seen: set[tuple] = set()
        for course in courses:
            identity = (
                course.name,
                course.teacher,
                course.location,
                course.weekday,
                course.start_section,
                course.end_section,
            )
            if identity in seen:
                continue
            seen.add(identity)
            by_day.setdefault(course.weekday, []).append(course)
        starts: dict[tuple[int, int], tuple[int, list]] = {}
        covered: set[tuple[int, int]] = set()
        for day, day_courses in by_day.items():
            cluster: list = []
            end = 0
            for course in sorted(day_courses, key=lambda row: (row.start_section, row.end_section)):
                if cluster and course.start_section > end:
                    first = min(row.start_section for row in cluster)
                    starts[(day, first)] = (end, cluster)
                    covered.update((day, section) for section in range(first + 1, end + 1))
                    cluster = []
                cluster.append(course)
                end = max(end, course.end_section)
            if cluster:
                first = min(row.start_section for row in cluster)
                starts[(day, first)] = (end, cluster)
                covered.update((day, section) for section in range(first + 1, end + 1))
        header = (
            "<tr><th>节次</th>" + "".join(f"<th>周{day}</th>" for day in "一二三四五六日"[:days]) + "</tr>"
        )
        body: list[str] = []
        for section in range(1, section_count + 1):
            occupied = any(course.start_section <= section <= course.end_section for course in courses)
            period = periods.get(section)
            clock = f"<small>{period[0]}<br>{period[1]}</small>" if period else ""
            cells = [f'<th class="section-index">{section:02d}{clock}</th>']
            for day in range(1, days + 1):
                if (day, section) in covered:
                    continue
                anchor = starts.get((day, section))
                if anchor is None:
                    cells.append('<td class="slot"></td>')
                    continue
                end, entries = anchor
                content = "".join(
                    '<div class="course-chip">'
                    f"<strong>{html.escape(course.name)}</strong>"
                    f"<small>第{course.start_section}-{course.end_section}节 · "
                    f"{html.escape(course.location or '地点未填写')}</small>"
                    f"<small>#{course.course_id} · {html.escape(course.teacher or '教师未填写')}</small>"
                    "</div>"
                    for course in entries
                )
                cells.append(
                    f'<td class="course-block course-tone-{entries[0].course_id % 5}" '
                    f'rowspan="{end - section + 1}">{content}</td>'
                )
            body.append(f'<tr class="{"busy" if occupied else "idle"}">' + "".join(cells) + "</tr>")
        content = repr((week, courses, pending_count))
        pending_notice = (
            f'<div class="course-pending">本周有 {pending_count} 门冲突课程待选择 · '
            "使用 /course conflicts 查看候选，再发送 /course choose &lt;ID&gt;</div>"
            if pending_count
            else ""
        )
        html_body = (
            '<div class="subtitle">北京时间 · 按课程 ID 编辑或删除课程</div>'
            f"{pending_notice}"
            f'<table class="timetable"><thead>{header}</thead><tbody>{"".join(body)}</tbody></table>'
            '<div class="list-footer">/course edit &lt;ID&gt; · /course delete &lt;ID&gt;</div>'
        )
        return await self._render(
            content, title=f"第 {week} 周课表", variant=variant, kind="timetable", html_body=html_body
        )

    async def render_activities(self, activities: list[dict], *, subtitle: str, updated: str) -> Path:
        cards = []
        for index, item in enumerate(activities, 1):
            status_html = ""
            for status in item["statuses"]:
                tone = "full" if status == "人数已满" else "need" if status.startswith("需") else "neutral"
                status_html += f'<span class="activity-status {tone}">{html.escape(status)}</span>'
            registration = (
                f'<div class="registration-time">报名时间 · {html.escape(item["registration"])}</div>'
                if item["registration"]
                else ""
            )
            cards.append(
                f'<article class="activity-entry"><span class="row-index">{index:02d}</span>'
                f"<div><strong>{html.escape(item['title'])}</strong>"
                f'<div class="row-meta">{html.escape(item["when"])} · '
                f"{html.escape(item['category'])}</div>"
                f'<div class="row-meta">{html.escape(item["place"])}</div>'
                f'<div class="activity-statuses">{status_html}</div>{registration}</div></article>'
            )
        return await self._render(
            repr((activities, subtitle, updated)),
            title="第二课堂 · 活动",
            variant="activity:rich",
            kind="structured",
            html_body=(
                f'<div class="subtitle">{html.escape(subtitle)}</div>'
                f"<section>{''.join(cards)}</section>"
                f'<div class="list-footer">最近更新：{html.escape(updated)}</div>'
            ),
        )

    @staticmethod
    def _help_overview_html(content: str) -> str:
        sections: list[str] = []
        in_section = False
        in_row = False
        lines = content.splitlines()
        for line in lines:
            if line.startswith("【") and line.endswith("】"):
                if in_row:
                    sections.append("</div>")
                    in_row = False
                if in_section:
                    sections.append("</section>")
                sections.append(f'<section class="help-section"><h2>{html.escape(line[1:-1])}</h2>')
                in_section = True
            elif line.startswith("/"):
                if in_row:
                    sections.append("</div>")
                command = html.escape(line).replace(
                    "🔧", '<span class="help-tool" title="可由 AI 工具调用">🔧</span>'
                )
                sections.append(f'<div class="help-row"><strong>{command}</strong>')
                in_row = True
            elif line.startswith("  ") and in_row:
                sections.append(f"<small>{html.escape(line.strip())}</small></div>")
                in_row = False
            elif line.strip():
                if in_row:
                    sections.append("</div>")
                    in_row = False
                if in_section:
                    sections.append("</section>")
                    in_section = False
                sections.append(f'<p class="list-footer">{html.escape(line)}</p>')
        if in_row:
            sections.append("</div>")
        if in_section:
            sections.append("</section>")
        return "".join(sections)

    async def render_markdown(
        self, content: str, *, title: str = "Amadeus", variant: str = "markdown"
    ) -> Path:
        from markdown_it import MarkdownIt

        if len(content) > 30_000:
            raise ValueError("Markdown 内容超过 30000 字符")
        renderer = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
        safe_markdown = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[外链图片已阻止：\1]", content)
        rendered = renderer.render(safe_markdown)
        return await self._render(
            content,
            title=title,
            variant=variant,
            kind="markdown",
            html_body=f'<article class="markdown">{rendered}</article>',
        )

    async def _render(
        self,
        content: str,
        *,
        title: str,
        variant: str,
        kind: str,
        html_body: str,
    ) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        key = self.cache_key(content, kind=kind, variant=variant)
        output_path = self.cache_dir / f"{key}.png"
        if output_path.is_file() and output_path.stat().st_size > 0:
            return output_path
        async with self._semaphore:
            if output_path.is_file() and output_path.stat().st_size > 0:
                return output_path
            browser = await self._get_browser()
            page = await browser.new_page(
                viewport={"width": 980, "height": 800},
                device_scale_factor=2,
            )
            temporary = output_path.with_suffix(".tmp.png")
            try:
                await page.set_content(
                    self._build_html(html_body, title),
                    wait_until="load",
                    timeout=15_000,
                )
                await page.evaluate("document.fonts ? document.fonts.ready : Promise.resolve()")
                await page.locator(".card").screenshot(path=str(temporary))
                temporary.replace(output_path)
            finally:
                await page.close()
                if temporary.exists():
                    temporary.unlink(missing_ok=True)
            return output_path

    async def close(self) -> None:
        if self._browser is not None:
            # Ctrl+C may stop Playwright's child driver before NoneBot invokes
            # shutdown hooks.  A disconnected browser is already closed and
            # must not turn an otherwise clean Bot shutdown into a failure.
            with suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    async def _get_browser(self):
        if self._browser is not None:
            return self._browser
        async with self._browser_lock:
            if self._browser is not None:
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ModuleNotFoundError as exc:
                raise RuntimeError("缺少 Playwright，无法渲染帮助图片") from exc
            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(headless=True)
            except Exception as exc:
                await self._playwright.stop()
                self._playwright = None
                raise RuntimeError(
                    "无法启动 Playwright Chromium；请执行 playwright install chromium"
                ) from exc
            return self._browser

    @staticmethod
    def _build_html(body: str, title: str) -> str:
        safe_title = html.escape(title)
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
:root {{ color-scheme: light; font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 28px; background: #edf1f7; color: #202737; }}
.card {{ width: 900px; padding: 30px 34px; background: #fff; border: 1px solid #d8deea;
         border-radius: 8px; box-shadow: 0 12px 32px rgba(35, 48, 80, .10); }}
h1 {{ margin: 0 0 18px; color: #245c59; font-size: 30px; }}
.subtitle {{ color: #53656c; font-size: 17px; margin: -6px 0 18px; }}
.help-section {{ margin: 22px 0; }}
.help-section h2 {{ color: #245c59; font-size: 21px; margin: 0 0 9px; }}
.help-row,.list-row {{ background: #f2f7f6; padding: 13px 16px; overflow-wrap: anywhere; }}
.help-row:nth-of-type(even),.list-row:nth-child(even) {{ background: #f7f5ee; }}
.help-row strong {{ display: block; color: #1c4140; font-size: 19px; }}
.help-tool {{ font-size: 17px; margin-left: 7px; vertical-align: middle; }}
.help-row small,.row-meta {{
  display: block; color: #52666b; font-size: 16px; line-height: 1.5; margin-top: 4px;
}}
.list-row {{ display: grid; grid-template-columns: 68px 1fr; gap: 12px; align-items: start; }}
.row-index {{ color: #a25c35; font-size: 17px; font-weight: 700; }}
.row-title {{ font-size: 20px; font-weight: 650; line-height: 1.4; }}
.list-footer {{ color: #52666b; font-size: 16px; margin: 18px 0 0; }}
.course-pending {{ padding: 11px 14px; margin: 0 0 14px; border-left: 4px solid #a25c35;
  background: #fff4e5; color: #714426; font-size: 16px; }}
.timetable {{ width: 100%; border-collapse: separate; border-spacing: 4px; table-layout: fixed; }}
.timetable th {{ color: #1b4d7d; font-size: 17px; height: 40px; }}
.timetable th:first-child {{ width: 65px; }}
.timetable td {{ vertical-align: top; background: #f5f8fb;
  border: 1px solid #e3ebf1; border-radius: 4px; padding: 2px; }}
.timetable tbody tr {{ height: 94px; }}
.timetable tbody th.section-index {{ height: 94px; }}
.timetable td.slot {{ height: 94px; }}
.timetable tr.idle td.slot {{ background: #fafbfc; }}
.timetable td.course-block {{ border-left: 4px solid #19856b; background: #e3f4ed; padding: 8px 6px; }}
.timetable td.course-block.course-tone-1 {{ border-color: #af7021; background: #fff1d8; }}
.timetable td.course-block.course-tone-2 {{ border-color: #bd507e; background: #fbeaf1; }}
.timetable td.course-block.course-tone-3 {{ border-color: #158196; background: #e2f4f7; }}
.timetable td.course-block.course-tone-4 {{ border-color: #6269b1; background: #ececfa; }}
.section-index {{ color: #61788d !important; font-size: 15px !important; }}
.section-index small {{ display: block; font-size: 11px; font-weight: 400; line-height: 1.25; }}
.course-chip {{ overflow-wrap: anywhere; }}
.course-chip + .course-chip {{ border-top: 1px solid #99a9ae; padding-top: 8px; margin-top: 8px; }}
.course-chip strong {{ display: block; font-size: 14px; line-height: 1.25; }}
.course-chip small {{ display: block; color: #52666b; font-size: 11px; line-height: 1.3; margin-top: 4px; }}
.activity-entry {{ display: grid; grid-template-columns: 55px 1fr; gap: 12px;
  padding: 17px 15px; overflow-wrap: anywhere; }}
.activity-entry:nth-child(odd) {{ background: #f2f7f6; }}
.activity-entry:nth-child(even) {{ background: #f7f5ee; }}
.activity-entry strong {{ display: block; font-size: 20px; line-height: 1.4; }}
.activity-statuses {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }}
.activity-status {{ display: inline-block; border: 1px solid #a7b6c1; border-radius: 4px;
  padding: 3px 7px; color: #53656c; background: #fff; font-size: 13px; }}
.activity-status.need {{ color: #125c92; border-color: #78a8c8; background: #e8f3fb; }}
.activity-status.full {{ color: #9f1239; border-color: #db8da0; background: #fff0f2; }}
.registration-time {{ margin-top: 9px; padding-left: 8px; border-left: 3px solid #a25c35;
  color: #714426; font-size: 15px; }}
.course-tone-1 {{ background: #fff1d8; border-color: #af7021; }}
.course-tone-2 {{ background: #fbeaf1; border-color: #bd507e; }}
.course-tone-3 {{ background: #e2f4f7; border-color: #158196; }}
.course-tone-4 {{ background: #ececfa; border-color: #6269b1; }}
pre {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere;
       font: 19px/1.7 "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; }}
.markdown {{ font-size: 18px; line-height: 1.7; overflow-wrap: anywhere; }}
.markdown h1,.markdown h2,.markdown h3 {{ color: #374b78; margin: 1em 0 .45em; }}
.markdown table {{ border-collapse: collapse; width: 100%; }}
.markdown th,.markdown td {{ border: 1px solid #ccd4e3; padding: 8px 10px; text-align: left; }}
.markdown code {{ background: #edf1f7; border-radius: 5px; padding: 2px 5px; }}
.markdown pre code {{ display: block; padding: 14px; white-space: pre-wrap; }}
.markdown img {{ max-width: 100%; }}
</style></head><body><main class="card"><h1>{safe_title}</h1>{body}</main></body></html>"""
