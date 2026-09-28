from __future__ import annotations

from amadeus_bot.repositories.core import CoreRepository

# Selected from the legacy things.json; food and music never enter the activity pool.
SEEDS = {
    "activity": (
        "做一道数学题",
        "做一道编程题",
        "背几个六级单词",
        "看一节网课",
        "读一本书",
        "复盘今天的笔记",
        "赶几个DDL",
        "清理宿舍",
        "去健身房",
        "出去散步十分钟",
        "去操场跑会步",
        "去睡觉",
        "改进一下bot",
        "写一会小说",
        "为《大邮数学集》编写答案",
        "为《大邮数学集》排版题目",
        "为byrdocs整理题目",
        "水会群",
        "问候一个朋友",
        "刷一会B站",
        "玩一局游戏",
        "去白浮泉公园",
        "去一趟国家博物馆",
    ),
    "food": (
        "冰激凌",
        "奶茶",
        "蛋挞",
        "米村拌饭",
        "自助餐",
        "醉面",
    ),
    # Playlist 2718626529, first 15 track IDs in playlist order (2026-09-28).
    "music": (
        "槃清的从塔楼一跃而下",
        "槃清/世羽繊莫的在我们成为朋友之前",
        "根本真澄的gDie Divil JIO",
        "Califair的句点",
        "北极星Polaris_D的痛苦啊胆怯啊我的梦话",
        "Newbiao好梦一场/洛天依Official/星尘infinity的沸雪煮相思",
        "花烬_Phoenix/星尘的叙梦",
        "赤羽/忘川风华录的天命周兴",
        "星尘infinity的争命",
        "海鲜面/乐正绫的对症下药",
        "砖厂浪人的临安雨",
        "ilem的告死鸟",
        "长长长安pwp/洛天依/星尘的宇宙一隅的我",
        "北极星Polaris_D的你要我如何说再见",
        "星尘/忘川风华录的日月山河",
    ),
}


def seed_recommendations(repository: CoreRepository) -> None:
    with repository.database.connection() as connection:
        for pool, names in SEEDS.items():
            for name in names:
                connection.execute(
                    """INSERT INTO recommendations(pool, path, content, weight, tags, creator_id)
                    SELECT ?, '', ?, 1, '[]', 'SYSTEM'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM recommendations
                        WHERE pool=? AND content=? AND creator_id='SYSTEM'
                    )""",
                    (pool, name, pool, name),
                )
