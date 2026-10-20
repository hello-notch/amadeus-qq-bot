"""Render synthetic help, course, and portal samples for visual QA."""

from __future__ import annotations

import asyncio
from pathlib import Path

from amadeus_bot.services.courses import CourseRecord
from amadeus_bot.services.renderer import RenderService


async def main() -> None:
    renderer = RenderService(Path("test-tmp/visual-check"))
    courses = [
        CourseRecord(1, "工程管理概论", 1, 3, 4, "1-16", "教学实验综合楼-N308", "王老师", 60),
        CourseRecord(2, "现代控制理论", 2, 6, 7, "1-16", "智慧教学楼-213", "李老师", 60),
        CourseRecord(3, "模式识别与机器学习", 3, 3, 4, "1-16", "智慧教学楼-205", "张老师", 60),
    ]
    try:
        timetable = await renderer.render_timetable(courses, week=4, variant="layout-check")
        portal = await renderer.render_rows(
            [
                ("01", "关于印发工作要点的通知", "党政办公室 · 2026-03-28 · wbnewsid 123456"),
                ("02", "校园服务通知", "教务处 · 2026-09-26 · wbnewsid 123457"),
            ],
            title="信息门户 · 校内通知",
            subtitle="第 1 页 / 最多 5 页",
            footer="最近更新：2026-09-26 18:06 北京时间",
            variant="layout-check",
        )
        help_image = await renderer.render_text(
            "Amadeus Bot · 可用命令\n\n【校园服务】\n/portal  🔧\n"
            "  查询通知｜EVERYONE\n\n"
            "🔧 表示该命令的部分能力可由 AI 工具调用。",
            title="Amadeus Bot 帮助",
            variant="overview:layout-check",
        )
        for image in (timetable, portal, help_image):
            print(image.resolve())
    finally:
        await renderer.close()


if __name__ == "__main__":
    asyncio.run(main())
