from pathlib import Path

from amadeus_bot.repositories.core import CoreRepository
from amadeus_bot.repositories.database import CoreDatabase


def make_repository(path: Path) -> CoreRepository:
    database = CoreDatabase(path / "core.sqlite3")
    database.initialize()
    return CoreRepository(database)


def test_members_and_recommendation_pools_are_persistent(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.add_member("10001", "99999")
    assert repository.is_member("10001")

    food_id = repository.add_recommendation("food", "午饭", "咖喱饭", 2, ("米饭",), "10001")
    music_id = repository.add_recommendation("music", "学习", "Song - Artist - URL", 1, (), "10001")

    assert [item.recommendation_id for item in repository.list_recommendations("food")] == [food_id]
    assert [item.recommendation_id for item in repository.list_recommendations("music")] == [music_id]
    assert repository.list_recommendations("activity") == []

    reopened = make_repository(tmp_path)
    assert reopened.is_member("10001")
    assert reopened.get_recommendation(food_id).content == "咖喱饭"


def test_portal_pages_and_detail_are_source_scoped(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    for index in range(1, 13):
        repository.upsert_source_item(
            "portal",
            {
                "item_id": str(index),
                "title": f"通知 {index}",
                "published_at": f"2026-09-{index:02d}",
                "department": "教务处",
                "summary": "",
                "url": f"https://example.test/{index}",
                "metadata": {},
                "content_hash": str(index),
            },
        )
    page = repository.query_source_items("portal", limit=10, offset=10)
    assert [row["item_id"] for row in page] == ["2", "1"]
    assert repository.get_source_item("portal", "12")["title"] == "通知 12"
    assert repository.get_source_item("activity", "12") is None
    assert repository.prune_source_items("portal", [str(index) for index in range(3, 13)]) == 2
    assert [row["item_id"] for row in repository.query_source_items("portal", limit=10, offset=10)] == []


def test_conversation_is_kept_after_repository_reopen(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.append_conversation("group:1", "user", "[100]: hello", "100")
    repository.append_conversation("group:1", "assistant", "hi", None)

    reopened = make_repository(tmp_path)
    assert reopened.recent_conversation("group:1") == [
        {"role": "user", "content": "[100]: hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_ai_quota_counts_successful_calls(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.set_ai_quota("chat", 2, "999")
    repository.record_ai_usage(
        task="chat",
        provider="test",
        model="test-model",
        latency_ms=1,
        success=True,
    )
    assert repository.ai_quota_for_task("chat") == (2, 1)


def test_ai_usage_summary_keeps_cached_and_peak_tokens(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.database.execute(
        """
        INSERT INTO ai_usage(
            task, provider, model, input_tokens, output_tokens, cached_tokens,
            latency_ms, success, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("chat", "deepseek", "deepseek-v4-flash", 100, 20, 40, 10, 1, "2026-09-04 01:30:00"),
    )
    repository.database.execute(
        """
        INSERT INTO ai_usage(
            task, provider, model, input_tokens, output_tokens, cached_tokens,
            latency_ms, success, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("chat", "deepseek", "deepseek-v4-flash", 200, 30, 50, 20, 1, "2026-09-04 04:30:00"),
    )

    row = repository.ai_usage_summary(3650)[0]

    assert row["input_tokens"] == 300
    assert row["output_tokens"] == 50
    assert row["cached_tokens"] == 90
    assert row["peak_input_tokens"] == 100
    assert row["peak_output_tokens"] == 20
    assert row["peak_cached_tokens"] == 40


def test_runtime_setting_is_persistent(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    assert repository.get_runtime_setting("ai.active_model") is None

    repository.set_runtime_setting("ai.active_model", "huanyan/gpt-5.6-luna", "999")

    reopened = make_repository(tmp_path)
    assert reopened.get_runtime_setting("ai.active_model") == "huanyan/gpt-5.6-luna"
