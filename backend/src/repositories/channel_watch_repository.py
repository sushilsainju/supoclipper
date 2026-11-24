"""
Channel watch repositories - database operations for watched livestream channels
and the VOD suggestions they produce.

State machine for ``channel_watches.state``:
``idle`` -> ``live`` -> ``ended_pending_vod`` -> ``idle``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_WATCH_COLUMNS = """
    id, user_id, platform, channel_input, resolved_channel_id, resolved_channel_title,
    enabled, state, active_live_video_id, active_live_started_at, ended_at,
    last_checked_at, last_check_method, created_at, updated_at
"""

_SUGGESTION_COLUMNS = """
    id, channel_watch_id, user_id, video_id, video_url, duration_seconds,
    status, task_id, created_at
"""


def _watch_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "platform": row.platform,
        "channel_input": row.channel_input,
        "resolved_channel_id": row.resolved_channel_id,
        "resolved_channel_title": row.resolved_channel_title,
        "enabled": row.enabled,
        "state": row.state,
        "active_live_video_id": row.active_live_video_id,
        "active_live_started_at": row.active_live_started_at,
        "ended_at": row.ended_at,
        "last_checked_at": row.last_checked_at,
        "last_check_method": row.last_check_method,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _suggestion_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": row.id,
        "channel_watch_id": row.channel_watch_id,
        "user_id": row.user_id,
        "video_id": row.video_id,
        "video_url": row.video_url,
        "duration_seconds": row.duration_seconds,
        "status": row.status,
        "task_id": row.task_id,
        "created_at": row.created_at,
    }


class ChannelWatchRepository:
    """Repository for channel_watches rows."""

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        user_id: str,
        platform: str,
        channel_input: str,
        resolved_channel_id: str,
        resolved_channel_title: Optional[str],
    ) -> Dict[str, Any]:
        """Insert a new watch in state ``idle`` and return it."""
        result = await db.execute(
            text(
                f"""
                INSERT INTO channel_watches (
                    id, user_id, platform, channel_input, resolved_channel_id,
                    resolved_channel_title, created_at, updated_at
                )
                VALUES (
                    :id, :user_id, :platform, :channel_input, :resolved_channel_id,
                    :resolved_channel_title, NOW(), NOW()
                )
                RETURNING {_WATCH_COLUMNS}
                """
            ),
            {
                "id": str(uuid4()),
                "user_id": user_id,
                "platform": platform,
                "channel_input": channel_input,
                "resolved_channel_id": resolved_channel_id,
                "resolved_channel_title": resolved_channel_title,
            },
        )
        row = result.fetchone()
        await db.commit()
        return _watch_to_dict(row)

    @staticmethod
    async def get_by_id(db: AsyncSession, watch_id: str) -> Optional[Dict[str, Any]]:
        """Return the watch with this id, or None."""
        result = await db.execute(
            text(f"SELECT {_WATCH_COLUMNS} FROM channel_watches WHERE id = :watch_id"),
            {"watch_id": watch_id},
        )
        row = result.fetchone()
        return _watch_to_dict(row) if row else None

    @staticmethod
    async def list_for_user(db: AsyncSession, user_id: str) -> List[Dict[str, Any]]:
        """Return all watches owned by a user, newest first."""
        result = await db.execute(
            text(
                f"""
                SELECT {_WATCH_COLUMNS} FROM channel_watches
                WHERE user_id = :user_id
                ORDER BY created_at DESC
                """
            ),
            {"user_id": user_id},
        )
        return [_watch_to_dict(row) for row in result.fetchall()]

    @staticmethod
    async def list_enabled_idle(
        db: AsyncSession, *, platform: str, stale_before: datetime
    ) -> List[Dict[str, Any]]:
        """Enabled idle watches not checked since ``stale_before`` (never checked counts as stale)."""
        result = await db.execute(
            text(
                f"""
                SELECT {_WATCH_COLUMNS} FROM channel_watches
                WHERE platform = :platform
                  AND enabled = true
                  AND state = 'idle'
                  AND (last_checked_at IS NULL OR last_checked_at < :stale_before)
                ORDER BY last_checked_at NULLS FIRST
                """
            ),
            {"platform": platform, "stale_before": stale_before},
        )
        return [_watch_to_dict(row) for row in result.fetchall()]

    @staticmethod
    async def list_enabled_tracking(db: AsyncSession, *, platform: str) -> List[Dict[str, Any]]:
        """Enabled watches that are live or waiting for their VOD to resolve."""
        result = await db.execute(
            text(
                f"""
                SELECT {_WATCH_COLUMNS} FROM channel_watches
                WHERE platform = :platform
                  AND enabled = true
                  AND state IN ('live', 'ended_pending_vod')
                """
            ),
            {"platform": platform},
        )
        return [_watch_to_dict(row) for row in result.fetchall()]

    @staticmethod
    async def mark_live(
        db: AsyncSession, watch_id: str, *, video_id: str, started_at: datetime
    ) -> None:
        """Transition idle -> live for the given video."""
        await db.execute(
            text(
                """
                UPDATE channel_watches
                SET state = 'live',
                    active_live_video_id = :video_id,
                    active_live_started_at = :started_at,
                    updated_at = NOW()
                WHERE id = :watch_id
                """
            ),
            {"watch_id": watch_id, "video_id": video_id, "started_at": started_at},
        )
        await db.commit()

    @staticmethod
    async def mark_ended(db: AsyncSession, watch_id: str, *, ended_at: datetime) -> None:
        """Transition live -> ended_pending_vod; the same video id stays tracked."""
        await db.execute(
            text(
                """
                UPDATE channel_watches
                SET state = 'ended_pending_vod',
                    ended_at = :ended_at,
                    updated_at = NOW()
                WHERE id = :watch_id
                """
            ),
            {"watch_id": watch_id, "ended_at": ended_at},
        )
        await db.commit()

    @staticmethod
    async def reset_to_idle(db: AsyncSession, watch_id: str) -> None:
        """Transition ended_pending_vod -> idle and clear the live-stream fields."""
        await db.execute(
            text(
                """
                UPDATE channel_watches
                SET state = 'idle',
                    active_live_video_id = NULL,
                    active_live_started_at = NULL,
                    ended_at = NULL,
                    updated_at = NOW()
                WHERE id = :watch_id
                """
            ),
            {"watch_id": watch_id},
        )
        await db.commit()

    @staticmethod
    async def disable(db: AsyncSession, watch_id: str, user_id: str) -> bool:
        """Disable a watch owned by ``user_id``. Returns False if no such watch belongs to them."""
        result = await db.execute(
            text(
                """
                UPDATE channel_watches
                SET enabled = false, updated_at = NOW()
                WHERE id = :watch_id AND user_id = :user_id
                RETURNING id
                """
            ),
            {"watch_id": watch_id, "user_id": user_id},
        )
        updated = result.fetchone() is not None
        await db.commit()
        return updated

    @staticmethod
    async def touch_last_checked(db: AsyncSession, watch_id: str, *, method: str) -> None:
        """Record when and how the watch was last polled."""
        await db.execute(
            text(
                """
                UPDATE channel_watches
                SET last_checked_at = NOW(),
                    last_check_method = :method,
                    updated_at = NOW()
                WHERE id = :watch_id
                """
            ),
            {"watch_id": watch_id, "method": method},
        )
        await db.commit()


class ChannelWatchSuggestionRepository:
    """Repository for channel_watch_suggestions rows."""

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        channel_watch_id: str,
        user_id: str,
        video_id: str,
        video_url: str,
        status: str = "pending",
        duration_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Insert a suggestion row and return it. ``duration_seconds`` may be None (e.g. status ``failed``)."""
        result = await db.execute(
            text(
                f"""
                INSERT INTO channel_watch_suggestions (
                    id, channel_watch_id, user_id, video_id, video_url,
                    duration_seconds, status, created_at
                )
                VALUES (
                    :id, :channel_watch_id, :user_id, :video_id, :video_url,
                    :duration_seconds, :status, NOW()
                )
                RETURNING {_SUGGESTION_COLUMNS}
                """
            ),
            {
                "id": str(uuid4()),
                "channel_watch_id": channel_watch_id,
                "user_id": user_id,
                "video_id": video_id,
                "video_url": video_url,
                "duration_seconds": duration_seconds,
                "status": status,
            },
        )
        row = result.fetchone()
        await db.commit()
        return _suggestion_to_dict(row)

    @staticmethod
    async def list_pending_for_user(db: AsyncSession, user_id: str) -> List[Dict[str, Any]]:
        """Return the user's suggestions still awaiting a decision, newest first."""
        result = await db.execute(
            text(
                f"""
                SELECT {_SUGGESTION_COLUMNS} FROM channel_watch_suggestions
                WHERE user_id = :user_id AND status = 'pending'
                ORDER BY created_at DESC
                """
            ),
            {"user_id": user_id},
        )
        return [_suggestion_to_dict(row) for row in result.fetchall()]

    @staticmethod
    async def get_by_id(db: AsyncSession, suggestion_id: str) -> Optional[Dict[str, Any]]:
        """Return the suggestion with this id, or None."""
        result = await db.execute(
            text(
                f"SELECT {_SUGGESTION_COLUMNS} FROM channel_watch_suggestions WHERE id = :suggestion_id"
            ),
            {"suggestion_id": suggestion_id},
        )
        row = result.fetchone()
        return _suggestion_to_dict(row) if row else None

    @staticmethod
    async def mark_created(db: AsyncSession, suggestion_id: str, *, task_id: str) -> None:
        """Mark a suggestion as turned into a clip task."""
        await db.execute(
            text(
                """
                UPDATE channel_watch_suggestions
                SET status = 'created', task_id = :task_id
                WHERE id = :suggestion_id
                """
            ),
            {"suggestion_id": suggestion_id, "task_id": task_id},
        )
        await db.commit()

    @staticmethod
    async def mark_dismissed(db: AsyncSession, suggestion_id: str) -> None:
        """Mark a suggestion as dismissed by the user."""
        await db.execute(
            text(
                """
                UPDATE channel_watch_suggestions
                SET status = 'dismissed'
                WHERE id = :suggestion_id
                """
            ),
            {"suggestion_id": suggestion_id},
        )
        await db.commit()
