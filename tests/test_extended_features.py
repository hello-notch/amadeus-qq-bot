import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from amadeus_bot.repositories.group_data import GroupDataRepository
from amadeus_bot.repositories.user_data import UserDataRepository
from amadeus_bot.services import campus, campus_auth
from amadeus_bot.services.analytics import AnalyticsService
from amadeus_bot.services.campus import (
    ActivitySource,
    activity_from_mapping,
    decode_portal_html,
    format_activity_time,
    format_source_timestamp,
    load_cookie_header_file,
    normalize_activity_payload,
    parse_portal_list,
    portal_page_urls,
)
from amadeus_bot.services.courses import CourseService
from amadeus_bot.services.ddl import DDLService
from amadeus_bot.services.jwgl import JwglSource
from amadeus_bot.services.memory import MemoryService

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_wife_pair_is_symmetric_and_quote_is_deduplicated(tmp_path: Path) -> None:
    repository = GroupDataRepository(tmp_path / "groups")
    today = datetime(2026, 8, 28, tzinfo=SHANGHAI).date()
    pair = repository.assign_wife("1", "100", ["200", "300"], today)
    reverse = repository.get_wife("1", pair.partner_id, today)
    assert reverse is not None
    assert reverse.partner_id == "100"

    quote = repository.add_quote(
        "1",
        "99",
        "200",
        "100",
        "测试",
        "原文",
        source_author_name="小林",
        saved_by_name="小周",
    )
    assert repository.get_quote("1", quote.quote_id) == quote
    assert quote.source_author_name == "小林"
    assert quote.saved_by_name == "小周"
    assert repository.list_quotes("1", "小林") == [quote]
    assert repository.edit_quote("1", quote.quote_id, "新名称")
    assert repository.get_quote("1", quote.quote_id).name == "新名称"
    with pytest.raises(ValueError, match="已收藏"):
        repository.add_quote("1", "99", "200", "100", "重复", "原文")


def test_quote_migrates_legacy_table_without_discarding_records(tmp_path: Path) -> None:
    root = tmp_path / "groups" / "1"
    root.mkdir(parents=True)
    with sqlite3.connect(root / "group.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE quotes (quote_id INTEGER PRIMARY KEY, source_message_id TEXT, "
            "source_author_id TEXT, saved_by_id TEXT, name TEXT, tags TEXT, text TEXT, "
            "media_refs TEXT, created_at TEXT, updated_at TEXT, deleted_at TEXT)"
        )
        connection.execute(
            "INSERT INTO quotes VALUES (1, '99', '200', '100', '旧语录', '[\"旧标签\"]', "
            "'原文', '[]', '2026-09-01 00:00:00', NULL, NULL)"
        )
    repository = GroupDataRepository(tmp_path / "groups")
    row = repository.get_quote("1", 1)
    assert row is not None and row.name == "旧语录"
    assert row.source_author_name == row.saved_by_name == ""
    assert repository.get_quote("1", 1) == row
    assert repository.add_quote("1", "100", "200", "100", "新语录", "新内容").quote_id == 2


def test_memory_optout_blocks_new_candidates(tmp_path: Path) -> None:
    service = MemoryService(UserDataRepository(tmp_path / "users"))
    memory_id = service.add_candidate("100", "preference", "喜欢咖啡", confidence=0.9, source_group_id="1")
    assert service.list("100", memory_id)[0].content == "喜欢咖啡"
    service.set_analysis("100", False)
    with pytest.raises(ValueError, match="退出"):
        service.add_candidate("100", "preference", "喜欢茶", confidence=0.9, source_group_id="1")


def test_course_csv_preview_confirm_and_query(tmp_path: Path) -> None:
    service = CourseService(UserDataRepository(tmp_path / "users"))
    content = ("课程名,教师,教室,星期,开始节次,结束节次,周次\n高等数学,张老师,N101,一,1,2,1-16\n").encode()
    preview = service.preview_csv("100", content, "course.csv")
    batch_id, count = service.confirm("100", preview.token)
    assert batch_id and count == 1
    course = service.list("100", 1)[0]
    assert course.name == "高等数学"
    assert course.start_section == 1


def test_course_current_week_and_user_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMADEUS_SEMESTER_START", "2026-09-07")
    service = CourseService(UserDataRepository(tmp_path / "users"))
    service.add("100", "本周课", 1, (1, 2), "3", "N101")
    service.add("100", "下周课", 2, (3, 4), "4")
    service.add("200", "他人课", 1, (1, 2), "3")
    week = service.current_week(now=datetime(2026, 9, 26, 12, tzinfo=SHANGHAI))
    assert week == 3
    assert [row.name for row in service.week_courses("100", week)] == ["本周课"]
    assert service.week_courses("300", week) == []
    with pytest.raises(ValueError, match="周次"):
        service.week_courses("100", 31)


def test_course_import_replaces_and_deduplicates_without_losing_data_on_invalid_input(
    tmp_path: Path,
) -> None:
    service = CourseService(UserDataRepository(tmp_path / "users"))
    service.add("100", "旧课", 1, (1, 2), "1-16")
    service.add("200", "他人课", 1, (1, 2), "1-16")
    row = {
        "name": "新课",
        "weekday": 2,
        "start_section": 3,
        "end_section": 4,
        "weeks": "1-16",
        "teacher": "李老师",
        "location": "N101",
    }
    with pytest.raises(ValueError, match="节次"):
        service.preview_rows("100", [row | {"end_section": 2}], "test")
    assert [course.name for course in service.list("100")] == ["旧课"]
    preview = service.preview_rows("100", [row, row, row | {"weeks": "9-16"}], "test")
    with pytest.raises(ValueError, match="不属于"):
        service.confirm("200", preview.token)
    assert service.confirm("100", preview.token)[1] == 1
    assert [course.name for course in service.list("100")] == ["新课"]
    assert service.list("100")[0].weeks == "1-16"
    preview = service.preview_rows("100", [row, row], "test")
    service.confirm("100", preview.token)
    assert len(service.list("100")) == 1
    assert [course.name for course in service.list("200")] == ["他人课"]


def test_course_conflicts_require_persistent_choice_and_ignore_disjoint_weeks(
    tmp_path: Path,
) -> None:
    repository = UserDataRepository(tmp_path / "users")
    service = CourseService(repository)
    base = {"weekday": 1, "start_section": 1, "end_section": 2, "weeks": "1-8", "location": "N101"}
    rows = [
        base | {"name": "选修甲", "teacher": "甲老师"},
        base | {"name": "选修乙", "teacher": "乙老师"},
        base | {"name": "后半学期", "weeks": "9-16"},
        base | {"name": "下午课", "start_section": 5, "end_section": 6},
    ]
    preview = service.preview_rows("100", rows, "class")
    service.confirm("100", preview.token)
    assert {course.name for course in service.list("100")} == {"后半学期", "下午课"}
    groups = service.conflict_groups("100")
    assert len(groups) == 1
    assert {course.name for course in groups[0]} == {"选修甲", "选修乙"}
    with pytest.raises(ValueError, match="待选择"):
        service.choose("200", groups[0][0].course_id)
    chosen = next(course for course in groups[0] if course.name == "选修甲")
    assert service.choose("100", chosen.course_id) == (1, 0)
    restarted = CourseService(repository)
    assert not restarted.conflict_groups("100")
    assert {course.name for course in restarted.week_courses("100", 4)} == {"选修甲", "下午课"}
    assert {course.name for course in restarted.week_courses("100", 12)} == {"后半学期"}


def test_course_batch_choice_is_atomic_and_partial_choices_remain_pending(tmp_path: Path) -> None:
    service = CourseService(UserDataRepository(tmp_path / "users"))
    rows = [
        {"name": name, "weekday": day, "start_section": 3, "end_section": 4, "weeks": "1-16"}
        for day, name in ((1, "甲"), (1, "乙"), (2, "丙"), (2, "丁"))
    ]
    service.confirm("100", service.preview_rows("100", rows, "class").token)
    by_name = {row.name: row.course_id for row in service.pending_courses("100")}
    with pytest.raises(ValueError, match="选择冲突"):
        service.choose_many("100", [by_name["甲"], by_name["乙"]])
    assert len(service.pending_courses("100")) == 4
    with pytest.raises(ValueError, match="待选择"):
        service.choose_many("100", [by_name["甲"], 99999])
    assert len(service.pending_courses("100")) == 4
    assert service.choose_many("100", [by_name["甲"], by_name["丙"]]) == (2, 0)
    assert {row.name for row in service.list("100")} == {"甲", "丙"}
    assert service.pending_courses("100") == []


def test_course_batch_delete_and_clear_include_pending_and_keep_user_isolation(tmp_path: Path) -> None:
    service = CourseService(UserDataRepository(tmp_path / "users"))
    first = service.add("100", "早课", 1, (1, 2), "1-16")
    second = service.add("100", "晚课", 2, (8, 9), "1-16")
    service.add("200", "别人的课", 1, (1, 2), "1-16")
    with pytest.raises(ValueError, match="不存在"):
        service.delete_many("100", [first, 99999])
    assert len(service.list("100")) == 2
    assert service.delete_many("100", [first, second, first]) == (2, 0)
    service.confirm(
        "100",
        service.preview_rows(
            "100",
            [
                {"name": name, "weekday": 3, "start_section": 3, "end_section": 4, "weeks": "1-16"}
                for name in ("候选甲", "候选乙")
            ],
            "class",
        ).token,
    )
    ids = [row.course_id for row in service.pending_courses("100")]
    assert service.delete_many("100", [ids[0]]) == (1, 1)
    assert [row.name for row in service.list("100")] == ["候选乙"]
    service.register_conflict_prompt("100", "100", "12345", "555")
    assert service.delete_all("100") == 1
    assert not service.list("100") and not service.pending_courses("100")
    assert service.conflict_reply_subject("100", "12345", "555", is_superuser=False) is None
    assert [row.name for row in service.list("200")] == ["别人的课"]


def test_conflict_reply_is_bound_to_actor_and_conversation_across_restart(tmp_path: Path) -> None:
    repository = UserDataRepository(tmp_path / "users")
    service = CourseService(repository)
    service.register_conflict_prompt("100", "100", "123", "999")
    restarted = CourseService(repository)
    assert restarted.conflict_reply_subject("100", "123", "999", is_superuser=False) == "100"
    assert restarted.conflict_reply_subject("100", "123", "888", is_superuser=False) is None
    assert restarted.conflict_reply_subject("100", "123", None, is_superuser=False) is None
    assert restarted.conflict_reply_subject("200", "123", "999", is_superuser=False) is None
    service.register_conflict_prompt("100", "200", "456", None)
    assert restarted.conflict_reply_subject("100", "123", "999", is_superuser=False) is None
    assert restarted.conflict_reply_subject("100", "456", None, is_superuser=False) is None
    assert restarted.conflict_reply_subject("100", "456", None, is_superuser=True) == "200"
    service.register_conflict_prompt("200", "200", "789", "999")
    assert restarted.conflict_reply_subject("200", "789", "999", is_superuser=False) == "200"
    with repository.connection("100") as connection:
        assert connection.execute("SELECT COUNT(*) FROM course_conflict_prompts").fetchone()[0] == 1
        connection.execute("UPDATE course_conflict_prompts SET created_at_utc=datetime('now', '-1 day')")
    assert restarted.conflict_reply_subject("100", "456", None, is_superuser=True) is None


def test_conflict_prompt_migration_keeps_only_newest_legacy_message(tmp_path: Path) -> None:
    user_dir = tmp_path / "users" / "100"
    user_dir.mkdir(parents=True)
    with sqlite3.connect(user_dir / "user.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE course_conflict_prompts ("
            "message_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, scope_type TEXT NOT NULL, "
            "scope_id TEXT NOT NULL, created_at_utc TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO course_conflict_prompts VALUES (?, '100', 'group', '999', ?)",
            [("old", "2026-09-20 12:00:00"), ("new", "2026-09-26 12:00:00")],
        )
    repository = UserDataRepository(tmp_path / "users")
    with repository.connection("100") as connection:
        assert [row["message_id"] for row in connection.execute("SELECT * FROM course_conflict_prompts")] == [
            "new"
        ]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO course_conflict_prompts(message_id, subject_id, scope_type, scope_id) "
                "VALUES ('another', '100', 'group', '999')"
            )
    with repository.connection("100") as connection:
        assert connection.execute("SELECT COUNT(*) FROM course_conflict_prompts").fetchone()[0] == 1


def test_existing_course_database_migrates_selection_state(tmp_path: Path) -> None:
    repository = UserDataRepository(tmp_path / "users")
    service = CourseService(repository)
    service.add("100", "原课", 1, (1, 2), "1-16")
    database = tmp_path / "users" / "100" / "user.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE courses DROP COLUMN selection_state")
    assert [course.name for course in service.list("100")] == ["原课"]
    assert [course.name for course in service.list("100")] == ["原课"]


def test_campus_parsers_accept_portal_and_activity_shapes() -> None:
    html = (
        '<a href="xntz_content.jsp?urltype=news.NewsContentUrl&amp;wbnewsid=123">校内通知 A</a>'
        "<span>2026-08-28</span>"
    )
    portal = parse_portal_list(html, "http://my.bupt.edu.cn/")
    assert portal[0].item_id == "123"
    assert portal[0].published_at == "2026-08-28"
    activity = normalize_activity_payload({"data": [{"act_id": 7, "act_name": "讲座", "location": "西土城"}]})
    assert activity[0] == activity_from_mapping({"act_id": 7, "act_name": "讲座", "location": "西土城"})


def test_portal_department_and_pagination_are_extracted() -> None:
    base = "http://my.bupt.edu.cn/list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1154"
    html = (
        '<tr><td><a href="xntz_content.jsp?wbnewsid=123">通知 A</a></td>'
        "<td>教务处</td><td>2026-09-26</td></tr>"
        '<a href="list.jsp?wbtreeid=1154&urltype=tree.TreeTempUrl&a1154p=2">下一页</a>'
        '<a href="https://evil.example/list.jsp?wbtreeid=1154&page=2">错误域名</a>'
    )
    assert parse_portal_list(html, base)[0].department == "教务处"
    assert portal_page_urls(html, base) == [
        "http://my.bupt.edu.cn/list.jsp?wbtreeid=1154&urltype=tree.TreeTempUrl&a1154p=2"
    ]


def test_activity_business_error_is_not_empty_result() -> None:
    with pytest.raises(RuntimeError, match="业务错误"):
        normalize_activity_payload({"status": 401, "data": []})
    with pytest.raises(RuntimeError, match="形状"):
        normalize_activity_payload({"message": "login required"})
    assert normalize_activity_payload({"status": 0, "data": []}) == []


def test_activity_student_fields_and_source_timestamp() -> None:
    row = activity_from_mapping(
        {
            "id": 3,
            "name": "讲座",
            "activity_start_time": "2026-09-26 14:00:00",
            "area": 1,
            "demands": 3,
            "attend_count": 20,
            "attend_limit": 20,
        }
    )
    assert row.published_at == "2026-09-26 14:00:00"
    assert row.metadata["campus"] == "沙河校区"
    assert "人数已满" in row.metadata["status"]
    assert format_source_timestamp("2026-09-26 10:06:25") == "2026-09-26 18:06 北京时间"
    assert format_activity_time("2026-09-25T00:00:00Z") == "2026-09-25 08:00"
    assert format_activity_time("2026-09-25 08:00:00") == "2026-09-25 08:00"
    assert (
        activity_from_mapping({"id": 4, "name": "活动", "area": 1, "location": "教学楼"}).metadata["campus"]
        == "沙河校区"
    )


def test_portal_decoder_falls_back_to_gb18030() -> None:
    html = "<title>校内通知-欢迎访问信息服务门户</title>"
    assert decode_portal_html(html.encode("utf-8")) == html
    assert decode_portal_html(html.encode("gb18030")) == html


def test_cookie_header_file_preserves_duplicate_names_and_legacy_json(tmp_path: Path) -> None:
    raw = "JSESSIONID=first; route=node; JSESSIONID=second"
    raw_file = tmp_path / "cookie.txt"
    raw_file.write_text(raw, encoding="utf-8")
    assert load_cookie_header_file(str(raw_file)) == raw

    legacy_file = tmp_path / "cookie.json"
    legacy_file.write_text('{"first": "one", "second": "two"}', encoding="utf-8")
    assert load_cookie_header_file(str(legacy_file)) == "first=one; second=two"


@pytest.mark.asyncio
async def test_jwgl_keepalive_preserves_duplicate_cookie_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "JSESSIONID=first; route=node; JSESSIONID=second"
    cookie_file = tmp_path / "jwgl-cookie.txt"
    cookie_file.write_text(raw, encoding="utf-8")
    monkeypatch.setenv("AMADEUS_JWGL_COOKIE_FILE", str(cookie_file))

    class FakeResponse:
        url = "https://jwgl.bupt.edu.cn/jsxsd/framework/xsMain_bjyddx.jsp"
        text = "教学一体化服务平台"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, path: str):
            assert path == JwglSource.HOME_PATH
            return FakeResponse()

    source = JwglSource()
    monkeypatch.setattr(
        source,
        "_client",
        lambda cookie_header: FakeClient() if cookie_header == raw else None,
    )
    await source.keepalive()


@pytest.mark.asyncio
async def test_activity_refresh_accepts_empty_holiday_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "activity-token.txt"
    token_file.write_text("test-token", encoding="utf-8")
    monkeypatch.setenv("AMADEUS_ACTIVITY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AMADEUS_ACTIVITY_LIST_ENDPOINT", "/api/v1/activity")

    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": 0, "data": [], "digest": [], "error": None}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, endpoint: str, **kwargs):
            assert endpoint == "/api/v1/activity"
            assert kwargs["params"] == {
                "college_id": "0",
                "grade": "0",
                "class_id": "0",
                "role_id": "0",
                "page": "1",
                "page_size": "50",
            }
            assert kwargs["headers"]["Authorization"] == "Bearer test-token"
            return FakeResponse()

    class FakeRepository:
        def set_source_health(self, source: str, **values) -> None:
            assert source == "activity"
            assert values == {"success": True, "item_count": 0}

        def upsert_source_item(self, _source: str, _item: dict) -> bool:
            raise AssertionError("空列表不应写入项目")

        def prune_source_items(self, source: str, item_ids: list[str]) -> int:
            assert source == "activity"
            assert item_ids == []
            return 0

    monkeypatch.setattr(campus.httpx, "AsyncClient", FakeClient)
    assert await ActivitySource(FakeRepository()).refresh() == (0, 0)


@pytest.mark.asyncio
async def test_activity_refresh_enriches_full_registration_from_readonly_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "activity-token.txt"
    token_file.write_text("test-token", encoding="utf-8")
    monkeypatch.setenv("AMADEUS_ACTIVITY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AMADEUS_ACTIVITY_LIST_ENDPOINT", "/api/v1/activity")
    seen_paths: list[str] = []
    stored: list[dict] = []

    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, data) -> None:
            self.data = data

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"data": self.data}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, endpoint: str, **kwargs):
            seen_paths.append(endpoint)
            assert kwargs["headers"]["Authorization"] == "Bearer test-token"
            if endpoint.endswith("/7"):
                return FakeResponse({"id": 7, "attend_count": 20, "attend_limit": 20})
            return FakeResponse(
                [
                    {
                        "id": 7,
                        "name": "讲座",
                        "demands": 1,
                        "attend_limit": 20,
                        "activity_start_time": "2026-09-26 14:00:00",
                    }
                ]
            )

    class FakeRepository:
        def upsert_source_item(self, source: str, item: dict) -> bool:
            assert source == "activity"
            stored.append(item)
            return True

        def prune_source_items(self, source: str, ids: list[str]) -> int:
            assert source == "activity" and ids == ["7"]
            return 0

        def set_source_health(self, source: str, **values) -> None:
            assert source == "activity" and values["item_count"] == 1

    monkeypatch.setattr(campus.httpx, "AsyncClient", FakeClient)
    assert await ActivitySource(FakeRepository()).refresh() == (1, 1)
    assert seen_paths == ["/api/v1/activity", "/api/v1/activity/7"]
    assert "人数已满" in stored[0]["metadata"]["status"]


@pytest.mark.asyncio
async def test_activity_refresh_renews_expired_token_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "activity-token.txt"
    token_file.write_text("expired-token", encoding="utf-8")
    monkeypatch.setenv("AMADEUS_ACTIVITY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AMADEUS_ACTIVITY_LIST_ENDPOINT", "/api/v1/activity")
    used_tokens: list[str] = []

    class FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": 0, "data": []}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, _endpoint: str, **kwargs):
            token = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
            used_tokens.append(token)
            return FakeResponse(401 if token == "expired-token" else 200)

    class FakeAuthenticator:
        available = True

        async def login_activity(self) -> None:
            token_file.write_text("header.payload.signature", encoding="utf-8")

    class FakeRepository:
        def set_source_health(self, source: str, **values) -> None:
            assert source == "activity"
            assert values == {"success": True, "item_count": 0}

        def upsert_source_item(self, _source: str, _item: dict) -> bool:
            raise AssertionError("空列表不应写入项目")

        def prune_source_items(self, source: str, item_ids: list[str]) -> int:
            assert source == "activity"
            assert item_ids == []
            return 0

    monkeypatch.setattr(campus.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(campus, "CampusAuthenticator", FakeAuthenticator)
    assert await ActivitySource(FakeRepository()).refresh() == (0, 0)
    assert used_tokens == ["expired-token", "header.payload.signature"]


@pytest.mark.asyncio
async def test_activity_login_saves_returned_jwt_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    password_file = tmp_path / "password.txt"
    password_file.write_text("student\nsecret\n", encoding="utf-8")
    token_file = tmp_path / "activity-token.txt"
    monkeypatch.setenv("AMADEUS_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("AMADEUS_ACTIVITY_TOKEN_FILE", str(token_file))

    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def json(self) -> dict:
            return {"data": "header.payload.signature"}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, path: str, **kwargs):
            assert path == campus_auth.ACTIVITY_LOGIN_PATH
            assert kwargs["data"] == {
                "username": "student",
                "password": "secret",
                "code": "",
                "captcha": "",
            }
            return FakeResponse()

    monkeypatch.setattr(campus_auth.httpx, "AsyncClient", FakeClient)
    await campus_auth.CampusAuthenticator().login_activity()
    assert token_file.read_text(encoding="utf-8") == "header.payload.signature"
    assert not token_file.with_suffix(".txt.tmp").exists()


@pytest.mark.asyncio
async def test_activity_login_uses_browser_when_site_requires_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    password_file = tmp_path / "password.txt"
    password_file.write_text("student\nsecret\n", encoding="utf-8")
    token_file = tmp_path / "activity-token.txt"
    monkeypatch.setenv("AMADEUS_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("AMADEUS_ACTIVITY_TOKEN_FILE", str(token_file))

    class FakeResponse:
        status_code = 200
        headers = {"x-real-status": "419"}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, _path: str, **_kwargs):
            return FakeResponse()

    async def fake_browser_login(_self: campus_auth.CampusAuthenticator, account: str, password: str) -> str:
        assert (account, password) == ("student", "secret")
        return "newheader.newpayload.newsignature"

    monkeypatch.setattr(campus_auth.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        campus_auth.CampusAuthenticator,
        "_login_activity_browser",
        fake_browser_login,
    )
    await campus_auth.CampusAuthenticator().login_activity()
    assert token_file.read_text(encoding="utf-8") == "newheader.newpayload.newsignature"


def test_analytics_counts_deterministic_segments(tmp_path: Path) -> None:
    now = datetime.now().astimezone()
    directory = tmp_path / "logs" / "messages" / "1"
    directory.mkdir(parents=True)
    row = {
        "message_id": "1",
        "direction": "inbound",
        "scene": "group",
        "user_id": "100",
        "group_id": "1",
        "self_id": "999",
        "plain_text": "hello https://example.com",
        "timestamp": int(now.timestamp()),
        "segments": [
            {"type": "text", "data": {"text": "hello "}},
            {"type": "at", "data": {"qq": "42"}},
            {"type": "image", "data": {}},
        ],
    }
    (directory / f"{now:%Y-%m-%d}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    window = AnalyticsService(tmp_path / "logs").load_group("1", 1, now=now)
    stats = AnalyticsService.deterministic(window)
    assert stats["messages"] == 1
    assert stats["links"] == 1
    assert stats["segments"]["image"] == 1
    transcript = AnalyticsService.ai_transcript(window)
    assert "[消息 #1]" in transcript
    assert "[CQ:at,qq=42]" in transcript


def test_ddl_edit_resets_default_reminder_and_due_scan(tmp_path: Path) -> None:
    repository = UserDataRepository(tmp_path / "users")
    service = DDLService(repository)
    now = datetime(2026, 8, 28, 14, 0, tzinfo=SHANGHAI)
    created = service.add("100", "明天15:00", "作业", now=now)
    updated = service.edit(
        "100", created.record.ddl_id, deadline_text="明天16:00", content="数学作业", now=now
    )
    assert updated is not None
    assert updated.content == "数学作业"
    assert updated.reminder_at_utc is not None
