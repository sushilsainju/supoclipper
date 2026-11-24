"""Unit tests for the channel-watch cron poller.

``poll_channel_watches`` runs three phases over a single tick, each reading
its rows from ``ChannelWatchRepository``:

1. idle sweep - discover a newly-started live broadcast on an idle watch
2. live tracking - notice an already-known-live broadcast has ended
3. VOD resolution - once the VOD's duration is resolvable, create a
   suggestion and reset the watch to idle (or fail it out after a timeout)

Every repository/detection call is monkeypatched; no real DB or network I/O
happens here.
"""
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src import channel_watch_detection as detection
from src import feature_flags
from src import youtube_utils
from src.repositories.channel_watch_repository import (
    ChannelWatchRepository,
    ChannelWatchSuggestionRepository,
)
from src.workers import tasks


def _fake_async_session_local():
    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    return session


def _watch(**overrides):
    base = {
        "id": "watch-1",
        "user_id": "user-1",
        "platform": "youtube",
        "channel_input": "@SomeChannel",
        "resolved_channel_id": "UCabc123",
        "resolved_channel_title": "Some Channel",
        "enabled": True,
        "state": "idle",
        "active_live_video_id": None,
        "active_live_started_at": None,
        "ended_at": None,
        "last_checked_at": None,
        "last_check_method": None,
    }
    base.update(overrides)
    return base


def _patch_repo_defaults(monkeypatch, *, idle=None, tracking=None):
    monkeypatch.setattr(
        ChannelWatchRepository, "list_enabled_idle", AsyncMock(return_value=idle or [])
    )
    monkeypatch.setattr(
        ChannelWatchRepository, "list_enabled_tracking", AsyncMock(return_value=tracking or [])
    )


# ---------------------------------------------------------------------------
# Phase 1: idle sweep
# ---------------------------------------------------------------------------


async def test_poll_marks_watch_live_when_channel_goes_live(monkeypatch):
    watch = _watch()
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, idle=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(detection, "probe_channel_live", AsyncMock(return_value="video-123"))
    mark_live = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "mark_live", mark_live)
    monkeypatch.setattr(ChannelWatchRepository, "touch_last_checked", AsyncMock())

    result = await tasks.poll_channel_watches({})

    mark_live.assert_awaited_once()
    _, kwargs = mark_live.call_args
    assert mark_live.call_args.args[1] == "watch-1"
    assert kwargs["video_id"] == "video-123"
    assert result["marked_live"] == 1


async def test_poll_touches_last_checked_when_channel_not_live(monkeypatch):
    watch = _watch()
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, idle=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(detection, "probe_channel_live", AsyncMock(return_value=None))
    mark_live = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "mark_live", mark_live)
    touch = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "touch_last_checked", touch)

    result = await tasks.poll_channel_watches({})

    mark_live.assert_not_awaited()
    touch.assert_awaited_once()
    assert touch.call_args.args[1] == "watch-1"
    assert result["marked_live"] == 0


async def test_poll_skips_idle_sweep_when_channel_watching_disabled(monkeypatch):
    """A disabled flag must stop polling entirely, not just hide the UI."""
    watch = _watch()
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, idle=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=False))
    probe = AsyncMock()
    monkeypatch.setattr(detection, "probe_channel_live", probe)
    touch = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "touch_last_checked", touch)

    result = await tasks.poll_channel_watches({})

    probe.assert_not_awaited()
    touch.assert_not_awaited()
    assert result["marked_live"] == 0


# ---------------------------------------------------------------------------
# Phase 2: live tracking
# ---------------------------------------------------------------------------


async def test_poll_marks_watch_ended_when_broadcast_ends(monkeypatch):
    watch = _watch(state="live", active_live_video_id="video-123")
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, tracking=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(detection, "has_broadcast_ended", AsyncMock(return_value=True))
    mark_ended = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "mark_ended", mark_ended)

    result = await tasks.poll_channel_watches({})

    mark_ended.assert_awaited_once()
    assert mark_ended.call_args.args[1] == "watch-1"
    assert result["marked_ended"] == 1


async def test_poll_leaves_watch_live_when_broadcast_still_running(monkeypatch):
    watch = _watch(state="live", active_live_video_id="video-123")
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, tracking=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(detection, "has_broadcast_ended", AsyncMock(return_value=False))
    mark_ended = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "mark_ended", mark_ended)

    result = await tasks.poll_channel_watches({})

    mark_ended.assert_not_awaited()
    assert result["marked_ended"] == 0
    assert result["errors"] == 0


async def test_poll_skips_live_tracking_when_channel_watching_disabled(monkeypatch):
    """A disabled flag must stop polling an in-flight `live` row too, not just idle ones."""
    watch = _watch(state="live", active_live_video_id="video-123")
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, tracking=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=False))
    has_ended = AsyncMock()
    monkeypatch.setattr(detection, "has_broadcast_ended", has_ended)
    mark_ended = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "mark_ended", mark_ended)

    result = await tasks.poll_channel_watches({})

    has_ended.assert_not_awaited()
    mark_ended.assert_not_awaited()
    assert result["marked_ended"] == 0


# ---------------------------------------------------------------------------
# Phase 3: VOD resolution
# ---------------------------------------------------------------------------


async def test_poll_creates_suggestion_when_vod_duration_resolves(monkeypatch):
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    watch = _watch(state="ended_pending_vod", active_live_video_id="video-999", ended_at=ended_at)
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, tracking=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        youtube_utils, "async_get_youtube_video_info", AsyncMock(return_value={"duration": 1234})
    )
    create = AsyncMock()
    monkeypatch.setattr(ChannelWatchSuggestionRepository, "create", create)
    reset_to_idle = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "reset_to_idle", reset_to_idle)

    result = await tasks.poll_channel_watches({})

    create.assert_awaited_once()
    _, kwargs = create.call_args
    assert kwargs["channel_watch_id"] == "watch-1"
    assert kwargs["video_id"] == "video-999"
    assert kwargs["status"] == "pending"
    assert kwargs["duration_seconds"] == 1234
    reset_to_idle.assert_awaited_once_with(
        reset_to_idle.call_args.args[0], "watch-1"
    )
    assert result["suggestions_created"] == 1
    assert result["errors"] == 0


async def test_poll_leaves_pending_vod_watch_when_duration_unresolved_and_not_timed_out(monkeypatch):
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    watch = _watch(state="ended_pending_vod", active_live_video_id="video-999", ended_at=ended_at)
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, tracking=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        youtube_utils, "async_get_youtube_video_info", AsyncMock(return_value=None)
    )
    create = AsyncMock()
    monkeypatch.setattr(ChannelWatchSuggestionRepository, "create", create)
    reset_to_idle = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "reset_to_idle", reset_to_idle)

    result = await tasks.poll_channel_watches({})

    create.assert_not_awaited()
    reset_to_idle.assert_not_awaited()
    assert result["suggestions_created"] == 0
    assert result["errors"] == 0


async def test_poll_marks_suggestion_failed_after_vod_timeout(monkeypatch):
    ended_at = datetime.now(timezone.utc) - timedelta(
        seconds=tasks.CHANNEL_WATCH_VOD_TIMEOUT_SECONDS + 60
    )
    watch = _watch(state="ended_pending_vod", active_live_video_id="video-999", ended_at=ended_at)
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, tracking=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        youtube_utils, "async_get_youtube_video_info", AsyncMock(return_value=None)
    )
    create = AsyncMock()
    monkeypatch.setattr(ChannelWatchSuggestionRepository, "create", create)
    reset_to_idle = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "reset_to_idle", reset_to_idle)

    result = await tasks.poll_channel_watches({})

    create.assert_awaited_once()
    _, kwargs = create.call_args
    assert kwargs["status"] == "failed"
    assert kwargs.get("duration_seconds") is None
    reset_to_idle.assert_awaited_once()
    assert result["errors"] == 0
    assert result["suggestions_created"] == 0
    assert result["suggestions_failed"] == 1


async def test_poll_skips_vod_resolution_when_channel_watching_disabled(monkeypatch):
    """A disabled flag must stop polling an in-flight `ended_pending_vod` row too."""
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    watch = _watch(state="ended_pending_vod", active_live_video_id="video-999", ended_at=ended_at)
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, tracking=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=False))
    get_video_info = AsyncMock()
    monkeypatch.setattr(youtube_utils, "async_get_youtube_video_info", get_video_info)
    create = AsyncMock()
    monkeypatch.setattr(ChannelWatchSuggestionRepository, "create", create)
    reset_to_idle = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "reset_to_idle", reset_to_idle)

    result = await tasks.poll_channel_watches({})

    get_video_info.assert_not_awaited()
    create.assert_not_awaited()
    reset_to_idle.assert_not_awaited()
    assert result["suggestions_created"] == 0
    assert result["suggestions_failed"] == 0


# ---------------------------------------------------------------------------
# Per-row isolation
# ---------------------------------------------------------------------------


async def test_poll_continues_past_one_rows_exception(monkeypatch):
    watch_a = _watch(id="watch-a", user_id="user-a", resolved_channel_id="UCaaa")
    watch_b = _watch(id="watch-b", user_id="user-b", resolved_channel_id="UCbbb")
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, idle=[watch_a, watch_b])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))

    async def fake_probe(channel_id):
        if channel_id == "UCaaa":
            raise RuntimeError("boom")
        return "video-b"

    monkeypatch.setattr(detection, "probe_channel_live", fake_probe)
    mark_live = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "mark_live", mark_live)
    monkeypatch.setattr(ChannelWatchRepository, "touch_last_checked", AsyncMock())

    result = await tasks.poll_channel_watches({})

    mark_live.assert_awaited_once()
    assert mark_live.call_args.args[1] == "watch-b"
    assert result["marked_live"] == 1
    assert result["errors"] == 1


def _suggestion(**overrides):
    base = {
        "id": "sugg-1",
        "channel_watch_id": "watch-1",
        "user_id": "user-1",
        "video_id": "video-999",
        "video_url": "https://www.youtube.com/watch?v=video-999",
        "duration_seconds": 1234,
        "status": "pending",
        "task_id": None,
        "created_at": None,
    }
    base.update(overrides)
    return base


def _notification_context(**overrides):
    base = {
        "suggestion_id": "sugg-1",
        "channel_watch_id": "watch-1",
        "video_id": "video-999",
        "video_url": "https://www.youtube.com/watch?v=video-999",
        "duration_seconds": 1234,
        "notified_at": None,
        "notify_on_channel_watch": True,
        "user_email": "ada@example.com",
        "user_name": "Ada Lovelace",
        "user_first_name": "Ada",
        "channel_title": "Some Channel",
    }
    base.update(overrides)
    return base


def _setup_vod_resolved(monkeypatch, *, context=None):
    """Common setup for the VOD-resolution phase: one ended_pending_vod watch
    whose VOD duration has just resolved, so a suggestion is created.
    """
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    watch = _watch(state="ended_pending_vod", active_live_video_id="video-999", ended_at=ended_at)
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, tracking=[watch])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        youtube_utils, "async_get_youtube_video_info", AsyncMock(return_value={"duration": 1234})
    )
    monkeypatch.setattr(
        ChannelWatchSuggestionRepository, "create", AsyncMock(return_value=_suggestion())
    )
    monkeypatch.setattr(ChannelWatchRepository, "reset_to_idle", AsyncMock())
    monkeypatch.setattr(
        ChannelWatchSuggestionRepository,
        "get_notification_context",
        AsyncMock(return_value=context if context is not None else _notification_context()),
    )
    mark_notified = AsyncMock()
    monkeypatch.setattr(ChannelWatchSuggestionRepository, "mark_notified", mark_notified)
    return mark_notified


# ---------------------------------------------------------------------------
# Phase 3b: notify the user once a pending suggestion is created
# ---------------------------------------------------------------------------


async def test_poll_sends_channel_watch_notification_when_suggestion_created(monkeypatch):
    mark_notified = _setup_vod_resolved(monkeypatch)
    send_email = AsyncMock(return_value={"MessageId": "msg-1"})

    class FakeChannelWatchEmailService:
        def __init__(self):
            self.is_configured = True

        send_stream_ready_email = send_email

    monkeypatch.setattr(
        "src.services.channel_watch_email_service.ChannelWatchEmailService",
        FakeChannelWatchEmailService,
    )

    result = await tasks.poll_channel_watches({})

    send_email.assert_awaited_once()
    _, kwargs = send_email.call_args
    assert kwargs["recipient"] == "ada@example.com"
    assert kwargs["channel_title"] == "Some Channel"
    assert kwargs["video_url"] == "https://www.youtube.com/watch?v=video-999"
    assert kwargs["duration_seconds"] == 1234
    mark_notified.assert_awaited_once()
    assert mark_notified.call_args.args[1] == "sugg-1"
    assert result["suggestions_created"] == 1
    assert result["errors"] == 0


async def test_poll_skips_email_when_notify_toggle_is_off(monkeypatch):
    mark_notified = _setup_vod_resolved(
        monkeypatch, context=_notification_context(notify_on_channel_watch=False)
    )
    send_email = AsyncMock()

    class FakeChannelWatchEmailService:
        def __init__(self):
            self.is_configured = True

        send_stream_ready_email = send_email

    monkeypatch.setattr(
        "src.services.channel_watch_email_service.ChannelWatchEmailService",
        FakeChannelWatchEmailService,
    )

    result = await tasks.poll_channel_watches({})

    send_email.assert_not_awaited()
    mark_notified.assert_not_awaited()
    assert result["suggestions_created"] == 1
    assert result["errors"] == 0


async def test_poll_does_not_resend_email_once_notified_at_is_set(monkeypatch):
    mark_notified = _setup_vod_resolved(
        monkeypatch,
        context=_notification_context(notified_at=datetime.now(timezone.utc)),
    )
    send_email = AsyncMock()

    class FakeChannelWatchEmailService:
        def __init__(self):
            self.is_configured = True

        send_stream_ready_email = send_email

    monkeypatch.setattr(
        "src.services.channel_watch_email_service.ChannelWatchEmailService",
        FakeChannelWatchEmailService,
    )

    result = await tasks.poll_channel_watches({})

    send_email.assert_not_awaited()
    mark_notified.assert_not_awaited()
    assert result["suggestions_created"] == 1
    assert result["errors"] == 0


async def test_poll_skips_email_when_ses_not_configured(monkeypatch):
    mark_notified = _setup_vod_resolved(monkeypatch)
    send_email = AsyncMock()

    class FakeChannelWatchEmailService:
        def __init__(self):
            self.is_configured = False

        send_stream_ready_email = send_email

    monkeypatch.setattr(
        "src.services.channel_watch_email_service.ChannelWatchEmailService",
        FakeChannelWatchEmailService,
    )

    result = await tasks.poll_channel_watches({})

    send_email.assert_not_awaited()
    mark_notified.assert_not_awaited()
    assert result["suggestions_created"] == 1
    assert result["errors"] == 0


async def test_poll_tick_survives_channel_watch_email_send_failure(monkeypatch):
    """A failed notification send must never break the poll or the row's own
    accounting - matching ``_send_publish_notification_if_needed``'s pattern."""
    mark_notified = _setup_vod_resolved(monkeypatch)
    send_email = AsyncMock(side_effect=RuntimeError("SES down"))

    class FakeChannelWatchEmailService:
        def __init__(self):
            self.is_configured = True

        send_stream_ready_email = send_email

    monkeypatch.setattr(
        "src.services.channel_watch_email_service.ChannelWatchEmailService",
        FakeChannelWatchEmailService,
    )

    result = await tasks.poll_channel_watches({})

    send_email.assert_awaited_once()
    mark_notified.assert_not_awaited()
    assert result["suggestions_created"] == 1
    assert result["errors"] == 0


async def test_poll_continues_past_one_rows_exception_in_tracking_phase(monkeypatch):
    """One tracking row's (``live`` or ``ended_pending_vod``) exception must not stop the
    other row in the same loop from being checked."""
    watch_a = _watch(
        id="watch-a", user_id="user-a", state="live", active_live_video_id="video-a"
    )
    watch_b = _watch(
        id="watch-b", user_id="user-b", state="live", active_live_video_id="video-b"
    )
    monkeypatch.setattr("src.database.AsyncSessionLocal", _fake_async_session_local())
    _patch_repo_defaults(monkeypatch, tracking=[watch_a, watch_b])
    monkeypatch.setattr(feature_flags, "is_enabled", AsyncMock(return_value=True))

    async def fake_has_ended(video_id):
        if video_id == "video-a":
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(detection, "has_broadcast_ended", fake_has_ended)
    mark_ended = AsyncMock()
    monkeypatch.setattr(ChannelWatchRepository, "mark_ended", mark_ended)

    result = await tasks.poll_channel_watches({})

    mark_ended.assert_awaited_once()
    assert mark_ended.call_args.args[1] == "watch-b"
    assert result["marked_ended"] == 1
    assert result["errors"] == 1
