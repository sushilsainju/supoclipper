from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.repositories.channel_watch_repository import (
    ChannelWatchRepository,
    ChannelWatchSuggestionRepository,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeSession:
    """Queues one result per execute() call; records every statement and its bound params."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.commits = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        rows = self.results.pop(0) if self.results else []
        return _FakeResult(rows)

    async def commit(self):
        self.commits += 1


def _watch_row(**overrides):
    values = {
        "id": "watch-1",
        "user_id": "user-1",
        "platform": "youtube",
        "channel_input": "@example",
        "resolved_channel_id": "UC_example",
        "resolved_channel_title": "Example Channel",
        "enabled": True,
        "state": "idle",
        "active_live_video_id": None,
        "active_live_started_at": None,
        "ended_at": None,
        "last_checked_at": None,
        "last_check_method": None,
        "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _suggestion_row(**overrides):
    values = {
        "id": "sugg-1",
        "channel_watch_id": "watch-1",
        "user_id": "user-1",
        "video_id": "vid_abc",
        "video_url": "https://www.youtube.com/watch?v=vid_abc",
        "duration_seconds": 3600,
        "status": "pending",
        "task_id": None,
        "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --- ChannelWatchRepository.create / get_by_id ---------------------------------


async def test_create_returns_new_watch_row():
    session = _FakeSession([[_watch_row()]])

    watch = await ChannelWatchRepository.create(
        session,
        user_id="user-1",
        platform="youtube",
        channel_input="@example",
        resolved_channel_id="UC_example",
        resolved_channel_title="Example Channel",
    )

    sql, params = session.calls[0]
    assert "INSERT INTO channel_watches" in sql
    assert params["user_id"] == "user-1"
    assert params["platform"] == "youtube"
    assert params["channel_input"] == "@example"
    assert params["resolved_channel_id"] == "UC_example"
    assert params["resolved_channel_title"] == "Example Channel"
    assert params["id"]  # a fresh uuid is generated
    assert watch["id"] == "watch-1"
    assert watch["state"] == "idle"
    assert watch["enabled"] is True
    assert session.commits == 1


async def test_create_allows_null_resolved_title():
    session = _FakeSession([[_watch_row(resolved_channel_title=None)]])

    watch = await ChannelWatchRepository.create(
        session,
        user_id="user-1",
        platform="youtube",
        channel_input="UC_example",
        resolved_channel_id="UC_example",
        resolved_channel_title=None,
    )

    assert session.calls[0][1]["resolved_channel_title"] is None
    assert watch["resolved_channel_title"] is None


async def test_get_by_id_returns_none_when_missing():
    session = _FakeSession([[]])

    assert await ChannelWatchRepository.get_by_id(session, "missing") is None
    assert session.calls[0][1] == {"watch_id": "missing"}


async def test_get_by_id_returns_dict_shape():
    session = _FakeSession([[_watch_row()]])

    watch = await ChannelWatchRepository.get_by_id(session, "watch-1")

    assert watch["id"] == "watch-1"
    assert watch["user_id"] == "user-1"
    assert watch["resolved_channel_id"] == "UC_example"
    assert set(watch) == {
        "id", "user_id", "platform", "channel_input", "resolved_channel_id",
        "resolved_channel_title", "enabled", "state", "active_live_video_id",
        "active_live_started_at", "ended_at", "last_checked_at", "last_check_method",
        "created_at", "updated_at",
    }


# --- ChannelWatchRepository list methods ---------------------------------------


async def test_list_for_user_binds_user_and_maps_rows():
    session = _FakeSession([[_watch_row(id="a"), _watch_row(id="b")]])

    watches = await ChannelWatchRepository.list_for_user(session, "user-1")

    sql, params = session.calls[0]
    assert "WHERE user_id = :user_id" in sql
    assert params == {"user_id": "user-1"}
    assert [w["id"] for w in watches] == ["a", "b"]


async def test_list_enabled_idle_binds_platform_and_stale_cutoff():
    stale_before = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    session = _FakeSession([[_watch_row()]])

    watches = await ChannelWatchRepository.list_enabled_idle(
        session, platform="youtube", stale_before=stale_before
    )

    sql, params = session.calls[0]
    assert params == {"platform": "youtube", "stale_before": stale_before}
    assert "enabled = true" in sql
    assert "state = 'idle'" in sql
    assert "last_checked_at < :stale_before" in sql
    assert "last_checked_at IS NULL" in sql
    assert len(watches) == 1


async def test_list_enabled_idle_excludes_recently_checked_rows_in_sql():
    # The fake session cannot evaluate SQL, so this pins the exclusion predicate:
    # a row checked at or after stale_before fails `last_checked_at < :stale_before`
    # and is therefore never returned. Only that bound predicate can drop it.
    stale_before = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    too_recent = _watch_row(last_checked_at=datetime(2026, 11, 23, 12, 1, tzinfo=timezone.utc))
    session = _FakeSession([[]])  # the database filters the too-recent row out

    watches = await ChannelWatchRepository.list_enabled_idle(
        session, platform="youtube", stale_before=stale_before
    )

    sql, params = session.calls[0]
    assert "last_checked_at < :stale_before" in sql
    assert params["stale_before"] == stale_before
    assert watches == []
    assert too_recent.last_checked_at > stale_before


async def test_list_enabled_tracking_selects_live_and_ended_pending_vod():
    session = _FakeSession([[_watch_row(state="live"), _watch_row(id="w2", state="ended_pending_vod")]])

    watches = await ChannelWatchRepository.list_enabled_tracking(session, platform="youtube")

    sql, params = session.calls[0]
    assert params == {"platform": "youtube"}
    assert "enabled = true" in sql
    assert "state IN ('live', 'ended_pending_vod')" in sql
    assert [w["state"] for w in watches] == ["live", "ended_pending_vod"]


# --- ChannelWatchRepository state transitions -----------------------------------


async def test_mark_live_sets_live_state_and_video_fields():
    started = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    session = _FakeSession()

    await ChannelWatchRepository.mark_live(
        session, "watch-1", video_id="vid_live", started_at=started
    )

    sql, params = session.calls[0]
    assert "state = 'live'" in sql
    assert params == {"watch_id": "watch-1", "video_id": "vid_live", "started_at": started}
    assert session.commits == 1


async def test_mark_ended_sets_ended_pending_vod_and_ended_at():
    ended = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)
    session = _FakeSession()

    await ChannelWatchRepository.mark_ended(session, "watch-1", ended_at=ended)

    sql, params = session.calls[0]
    assert "state = 'ended_pending_vod'" in sql
    assert params == {"watch_id": "watch-1", "ended_at": ended}
    assert session.commits == 1


async def test_reset_to_idle_clears_live_fields():
    session = _FakeSession()

    await ChannelWatchRepository.reset_to_idle(session, "watch-1")

    sql, params = session.calls[0]
    assert "state = 'idle'" in sql
    assert "active_live_video_id = NULL" in sql
    assert "active_live_started_at = NULL" in sql
    assert "ended_at = NULL" in sql
    assert params == {"watch_id": "watch-1"}
    assert session.commits == 1


async def test_disable_is_scoped_to_owner_and_returns_true_when_updated():
    session = _FakeSession([[SimpleNamespace(id="watch-1")]])

    updated = await ChannelWatchRepository.disable(session, "watch-1", "user-1")

    sql, params = session.calls[0]
    assert "SET enabled = false" in sql
    assert "WHERE id = :watch_id AND user_id = :user_id" in sql
    assert params == {"watch_id": "watch-1", "user_id": "user-1"}
    assert updated is True
    assert session.commits == 1


async def test_disable_returns_false_for_non_owner():
    session = _FakeSession([[]])

    updated = await ChannelWatchRepository.disable(session, "watch-1", "someone-else")

    assert session.calls[0][1] == {"watch_id": "watch-1", "user_id": "someone-else"}
    assert updated is False


async def test_touch_last_checked_records_method_and_timestamp():
    session = _FakeSession()

    await ChannelWatchRepository.touch_last_checked(session, "watch-1", method="ytdlp")

    sql, params = session.calls[0]
    assert "last_checked_at = NOW()" in sql
    assert "last_check_method = :method" in sql
    assert params == {"watch_id": "watch-1", "method": "ytdlp"}
    assert session.commits == 1


# --- ChannelWatchSuggestionRepository -------------------------------------------


async def test_suggestion_create_defaults_to_pending():
    session = _FakeSession([[_suggestion_row()]])

    suggestion = await ChannelWatchSuggestionRepository.create(
        session,
        channel_watch_id="watch-1",
        user_id="user-1",
        video_id="vid_abc",
        video_url="https://www.youtube.com/watch?v=vid_abc",
        duration_seconds=3600,
    )

    sql, params = session.calls[0]
    assert "INSERT INTO channel_watch_suggestions" in sql
    assert params["status"] == "pending"
    assert params["duration_seconds"] == 3600
    assert params["video_id"] == "vid_abc"
    assert params["id"]
    assert suggestion["status"] == "pending"
    assert session.commits == 1


async def test_suggestion_create_failed_without_duration():
    session = _FakeSession([[_suggestion_row(status="failed", duration_seconds=None)]])

    suggestion = await ChannelWatchSuggestionRepository.create(
        session,
        channel_watch_id="watch-1",
        user_id="user-1",
        video_id="vid_abc",
        video_url="https://www.youtube.com/watch?v=vid_abc",
        status="failed",
    )

    params = session.calls[0][1]
    assert params["status"] == "failed"
    assert params["duration_seconds"] is None
    assert suggestion["status"] == "failed"
    assert suggestion["duration_seconds"] is None


async def test_list_pending_for_user_filters_by_user_and_pending_status():
    session = _FakeSession([[_suggestion_row()]])

    suggestions = await ChannelWatchSuggestionRepository.list_pending_for_user(session, "user-1")

    sql, params = session.calls[0]
    assert "user_id = :user_id" in sql
    assert "status = 'pending'" in sql
    assert params == {"user_id": "user-1"}
    assert [s["id"] for s in suggestions] == ["sugg-1"]


async def test_suggestion_get_by_id_returns_none_when_missing():
    session = _FakeSession([[]])

    assert await ChannelWatchSuggestionRepository.get_by_id(session, "missing") is None
    assert session.calls[0][1] == {"suggestion_id": "missing"}


async def test_suggestion_get_by_id_returns_row():
    session = _FakeSession([[_suggestion_row()]])

    suggestion = await ChannelWatchSuggestionRepository.get_by_id(session, "sugg-1")

    assert suggestion["video_url"] == "https://www.youtube.com/watch?v=vid_abc"


async def test_mark_created_sets_status_and_task_id():
    session = _FakeSession()

    await ChannelWatchSuggestionRepository.mark_created(session, "sugg-1", task_id="task-9")

    sql, params = session.calls[0]
    assert "status = 'created'" in sql
    assert params == {"suggestion_id": "sugg-1", "task_id": "task-9"}
    assert session.commits == 1


async def test_mark_dismissed_sets_status():
    session = _FakeSession()

    await ChannelWatchSuggestionRepository.mark_dismissed(session, "sugg-1")

    sql, params = session.calls[0]
    assert "status = 'dismissed'" in sql
    assert params == {"suggestion_id": "sugg-1"}
    assert session.commits == 1
