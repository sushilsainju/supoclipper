"""
Channel-watch routes.

Lets a user register a YouTube channel to watch for livestreams (Task 1-4:
schema, repositories, detection, cron poller) and manage the VOD-ready
suggestions the cron poller produces from it.

Confirming a suggestion does NOT create a task through this router: the
frontend calls the existing, unmodified ``POST /tasks/`` directly with the
suggestion's ``video_url``. ``link-task`` only records, after the fact, that
a given task id resulted from a given suggestion.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ... import feature_flags
from ...auth_headers import resolve_authenticated_user_id
from ...channel_watch_detection import resolve_channel
from ...config import get_config
from ...database import get_db
from ...feature_flags import client_platform
from ...repositories.channel_watch_repository import (
    ChannelWatchRepository,
    ChannelWatchSuggestionRepository,
)
from ...repositories.task_repository import TaskRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/channel-watches", tags=["channel-watches"])

PLATFORM = "youtube"


async def _user_id(request: Request, db: AsyncSession) -> str:
    return await resolve_authenticated_user_id(request, db, get_config())


async def _require_flag(request: Request, db: AsyncSession, user_id: str) -> None:
    enabled = await feature_flags.is_enabled(
        db, feature_flags.CHANNEL_WATCHING, platform=client_platform(request), user_id=user_id
    )
    if not enabled:
        raise HTTPException(
            status_code=403, detail="Channel watching isn't available on your account yet."
        )


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - empty or invalid body
        return {}
    return payload if isinstance(payload, dict) else {}


# ----------------------------------------------------------------------
# Watches
# ----------------------------------------------------------------------
@router.post("/", status_code=201)
async def create_watch(request: Request, db: AsyncSession = Depends(get_db)):
    """Register a channel to watch for livestreams."""
    user_id = await _user_id(request, db)
    await _require_flag(request, db, user_id)

    body = await _json_body(request)
    channel_input = body.get("channel_input")
    if not isinstance(channel_input, str) or not channel_input.strip():
        raise HTTPException(status_code=422, detail="channel_input is required")

    resolved = await resolve_channel(channel_input)
    if resolved is None:
        raise HTTPException(status_code=400, detail="Could not resolve that channel")

    watch = await ChannelWatchRepository.create(
        db,
        user_id=user_id,
        platform=PLATFORM,
        channel_input=channel_input,
        resolved_channel_id=resolved.channel_id,
        resolved_channel_title=resolved.title,
    )
    return {"watch": watch}


@router.get("/")
async def list_watches(request: Request, db: AsyncSession = Depends(get_db)):
    """List the caller's watched channels."""
    user_id = await _user_id(request, db)
    await _require_flag(request, db, user_id)

    watches = await ChannelWatchRepository.list_for_user(db, user_id)
    return {"watches": watches, "total": len(watches)}


@router.delete("/{watch_id}")
async def delete_watch(
    watch_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Disable a watch owned by the caller."""
    user_id = await _user_id(request, db)

    disabled = await ChannelWatchRepository.disable(db, watch_id, user_id)
    if not disabled:
        raise HTTPException(status_code=404, detail="Channel watch not found")
    return {"message": "Channel watch disabled"}


# ----------------------------------------------------------------------
# Suggestions
# ----------------------------------------------------------------------
@router.get("/suggestions")
async def list_suggestions(request: Request, db: AsyncSession = Depends(get_db)):
    """List the caller's pending VOD-ready suggestions."""
    user_id = await _user_id(request, db)
    await _require_flag(request, db, user_id)

    suggestions = await ChannelWatchSuggestionRepository.list_pending_for_user(db, user_id)
    return {"suggestions": suggestions, "total": len(suggestions)}


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(
    suggestion_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Dismiss a suggestion the caller owns."""
    user_id = await _user_id(request, db)

    suggestion = await ChannelWatchSuggestionRepository.get_by_id(db, suggestion_id)
    if not suggestion or suggestion["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    await ChannelWatchSuggestionRepository.mark_dismissed(db, suggestion_id)
    return {"message": "Suggestion dismissed"}


@router.post("/suggestions/{suggestion_id}/link-task")
async def link_suggestion_to_task(
    suggestion_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """
    Record that ``task_id`` (already created by the caller via the existing
    ``POST /tasks/``) resulted from this suggestion.
    """
    user_id = await _user_id(request, db)

    suggestion = await ChannelWatchSuggestionRepository.get_by_id(db, suggestion_id)
    if not suggestion or suggestion["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    body = await _json_body(request)
    task_id = body.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise HTTPException(status_code=422, detail="task_id is required")

    task = await TaskRepository.get_task_by_id(db, task_id)
    if not task or task["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Task not found")

    await ChannelWatchSuggestionRepository.mark_created(db, suggestion_id, task_id=task_id)
    return {"message": "Suggestion linked to task", "task_id": task_id}
