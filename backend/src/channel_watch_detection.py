"""
YouTube detection primitives for channel watching.

Three independent calls the channel-watch cron poller (a later task) uses:

- ``resolve_channel``: handle/URL -> canonical channel id, once at creation.
- ``probe_channel_live``: "is this channel live right now, and if so which
  video id" — the idle-sweep discovery check.
- ``has_broadcast_ended``: "has this already-known-live video's broadcast
  ended" — the live-tracking check.

Quota constraint (see docs/superpowers/specs/2026-11-23-youtube-channel-watch-design.md):
there is no cheap official YouTube Data API call to *discover* a new live
broadcast — ``search.list`` costs 100 quota units against a 10,000/day
default quota, so polling even one channel that way every few minutes would
exhaust it almost immediately. ``probe_channel_live`` therefore always uses
yt-dlp and must never call the Data API, regardless of whether a key is
configured. ``resolve_channel`` and ``has_broadcast_ended`` act on a
channel/video already known (not a discovery sweep) via cheap ``~1 unit``
Data API calls (``channels.list`` / ``videos.list``), so they use the
Data-API-first-with-yt-dlp-fallback hybrid already established by
``youtube_metadata_provider`` in ``youtube_utils.py``.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
import yt_dlp

from .config import get_config

logger = logging.getLogger(__name__)

# yt-dlp error messages that mean the video itself is gone (deleted, made
# private, taken down, or never archived) rather than a transient failure —
# matched against the fallback's extraction errors so a stuck-forever
# state=live watch can be routed through the existing ended_pending_vod
# timeout instead of polling forever. See has_broadcast_ended.
_VIDEO_UNAVAILABLE_ERROR = re.compile(
    r"video (?:is )?unavailable|private video|this video is no longer available"
    r"|this video has been removed|video has been deleted"
    r"|account associated with this video has been terminated"
    r"|video does not exist",
    re.IGNORECASE,
)

YOUTUBE_CHANNELS_API_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_VIDEOS_API_URL = "https://www.googleapis.com/youtube/v3/videos"

# Hosts a channel URL is allowed to point at. A URL input pointing anywhere
# else (e.g. a Twitch channel) must be rejected outright: parsing its path as
# a handle/username can otherwise match an unrelated YouTube channel via the
# Data API, or hand yt-dlp's own (non-YouTube) extractor a result that gets
# stored as resolved_channel_id and polled forever. See resolve_channel.
_VALID_CHANNEL_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})

# A real YouTube channel id always looks like this. Guards against accepting
# a bogus Data API id (e.g. from a mismatched forUsername/forHandle lookup).
_CHANNEL_ID_SHAPE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

_DATA_API_TIMEOUT = (10, 30)

_YTDLP_BASE_OPTIONS: Dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "nocheckcertificate": True,
}


@dataclass(frozen=True)
class ResolvedChannel:
    channel_id: str
    title: Optional[str] = None


# ---------------------------------------------------------------------------
# Narrow I/O primitives (the only things tests need to monkeypatch)
# ---------------------------------------------------------------------------


def _data_api_request(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get(url, params=params, timeout=_DATA_API_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _ytdlp_extract_info(url: str, opts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# ---------------------------------------------------------------------------
# resolve_channel
# ---------------------------------------------------------------------------


def _looks_like_url(trimmed: str) -> bool:
    return "://" in trimmed or trimmed.lower().startswith("www.")


def _is_disallowed_host(channel_input: str) -> bool:
    """True when the input looks like a URL but points at a host other than
    YouTube's — e.g. a Twitch URL whose path happens to parse as a bare
    username, which would otherwise resolve against an unrelated YouTube
    channel (Data API) or get stored with whatever id a non-YouTube yt-dlp
    extractor returns. A bare ``@handle`` or bare username (no URL scheme)
    is never treated as a URL and is always accepted, as today.
    """
    trimmed = (channel_input or "").strip()
    if not _looks_like_url(trimmed):
        return False
    url = trimmed if "://" in trimmed else f"https://{trimmed}"
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host not in _VALID_CHANNEL_HOSTS


def _parse_channel_input(channel_input: str) -> tuple[str, str]:
    """Return (kind, value): kind is ``id`` | ``handle`` | ``username``."""
    trimmed = (channel_input or "").strip()
    if not trimmed:
        return ("handle", "")

    if _looks_like_url(trimmed):
        url = trimmed if "://" in trimmed else f"https://{trimmed}"
        try:
            parsed = urlparse(url)
        except ValueError:
            return ("handle", "")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "channel":
            return ("id", parts[1])
        if len(parts) >= 2 and parts[0] in ("c", "user"):
            return ("username", parts[1])
        if parts and parts[0].startswith("@"):
            return ("handle", parts[0])
        if parts:
            return ("username", parts[0])
        return ("handle", "")

    if trimmed.startswith("@"):
        return ("handle", trimmed)
    return ("username", trimmed)


def _channel_url_for_ytdlp(channel_input: str) -> str:
    trimmed = (channel_input or "").strip()
    if "://" in trimmed:
        return trimmed
    if trimmed.lower().startswith("www."):
        return f"https://{trimmed}"
    if trimmed.startswith("@"):
        return f"https://www.youtube.com/{trimmed}"
    return f"https://www.youtube.com/@{trimmed}"


def _resolve_channel_via_data_api(channel_input: str, api_key: str) -> Optional[ResolvedChannel]:
    kind, value = _parse_channel_input(channel_input)
    if not value:
        return None

    params: Dict[str, Any] = {"part": "snippet", "key": api_key}
    if kind == "id":
        params["id"] = value
    elif kind == "handle":
        params["forHandle"] = value
    else:
        params["forUsername"] = value

    payload = _data_api_request(YOUTUBE_CHANNELS_API_URL, params)
    items = payload.get("items") or []
    if not items:
        return None

    item = items[0]
    channel_id = item.get("id")
    if not channel_id or not _CHANNEL_ID_SHAPE.match(channel_id):
        # Doesn't look like a real YouTube channel id — treat as unresolved
        # rather than storing a bogus id that will never match a live probe.
        return None
    title = (item.get("snippet") or {}).get("title")
    return ResolvedChannel(channel_id=channel_id, title=title)


def _resolve_channel_via_ytdlp(channel_input: str) -> Optional[ResolvedChannel]:
    url = _channel_url_for_ytdlp(channel_input)
    opts = dict(_YTDLP_BASE_OPTIONS)
    opts["extract_flat"] = True
    opts["playlist_items"] = "0"

    info = _ytdlp_extract_info(url, opts)
    if not info:
        return None

    channel_id = info.get("channel_id") or info.get("id")
    if not channel_id:
        return None
    title = info.get("channel") or info.get("uploader") or info.get("title")
    return ResolvedChannel(channel_id=channel_id, title=title)


async def resolve_channel(channel_input: str) -> Optional[ResolvedChannel]:
    """Resolve a user-typed handle/URL to a canonical YouTube channel id.

    Cheap, one-shot, acts on a channel the caller already named — not a
    discovery sweep — so it may use the Data API (~1 unit) when a key is
    configured, falling back to yt-dlp on any failure or non-match.

    Rejects a URL input whose host isn't YouTube's outright (e.g. a Twitch
    URL): otherwise its path can still parse as a bare handle/username and
    resolve against an unrelated channel, or against a non-YouTube page via
    yt-dlp's own extractor.
    """
    if _is_disallowed_host(channel_input):
        return None

    config = get_config()
    api_key = config.resolve_youtube_data_api_key()

    resolved: Optional[ResolvedChannel] = None
    if api_key:
        try:
            resolved = await asyncio.to_thread(_resolve_channel_via_data_api, channel_input, api_key)
        except Exception as exc:
            logger.warning(
                "YouTube Data API channel resolution failed for %r: %s. Falling back to yt-dlp.",
                channel_input,
                exc,
            )

    if resolved is not None:
        return resolved

    try:
        return await asyncio.to_thread(_resolve_channel_via_ytdlp, channel_input)
    except Exception as exc:
        logger.warning("yt-dlp channel resolution failed for %r: %s", channel_input, exc)
        return None


# ---------------------------------------------------------------------------
# probe_channel_live
# ---------------------------------------------------------------------------


async def probe_channel_live(channel_id: str) -> Optional[str]:
    """Is this channel live right now, and if so, which video id.

    HARD CONSTRAINT: this is the idle-sweep discovery check — it always
    uses yt-dlp and must never call the Data API, even when a key is
    configured. ``search.list`` is the only official way to discover a new
    live broadcast and costs 100 quota units against a 10,000/day default
    quota; polling even one watched channel that way every few minutes
    would exhaust it almost immediately.
    """
    url = f"https://www.youtube.com/channel/{channel_id}/live"
    opts = dict(_YTDLP_BASE_OPTIONS)

    try:
        info = await asyncio.to_thread(_ytdlp_extract_info, url, opts)
    except Exception as exc:
        logger.info("Live probe failed for channel %s: %s", channel_id, exc)
        return None

    if not info or not info.get("is_live"):
        return None
    return info.get("id") or None


# ---------------------------------------------------------------------------
# has_broadcast_ended
# ---------------------------------------------------------------------------


def _has_broadcast_ended_via_data_api(video_id: str, api_key: str) -> Optional[bool]:
    payload = _data_api_request(
        YOUTUBE_VIDEOS_API_URL,
        {"part": "liveStreamingDetails", "id": video_id, "key": api_key},
    )
    items = payload.get("items") or []
    if not items:
        # The Data API successfully queried this id and found nothing — the
        # video was deleted, made private, or never archived. That is gone,
        # not inconclusive: treat it as ended rather than falling through to
        # yt-dlp, so the existing ended_pending_vod timeout can clean up the
        # watch instead of polling state=live forever.
        return True
    details = items[0].get("liveStreamingDetails") or {}
    return details.get("actualEndTime") is not None


def _has_broadcast_ended_via_ytdlp(video_id: str) -> bool:
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = dict(_YTDLP_BASE_OPTIONS)
    try:
        info = _ytdlp_extract_info(url, opts)
    except Exception as exc:
        if _VIDEO_UNAVAILABLE_ERROR.search(str(exc)):
            # The video is gone (deleted/private/taken down), not a transient
            # extraction failure — treat as ended for the same reason as the
            # zero-items Data API case above.
            return True
        raise
    return not bool((info or {}).get("is_live"))


async def has_broadcast_ended(video_id: str) -> bool:
    """Has this already-known-live video's broadcast ended.

    Cheap, per-video (not a discovery sweep), so it may use the Data API
    (~1 unit, ``videos.list`` + ``liveStreamingDetails.actualEndTime``)
    when a key is configured, falling back to a yt-dlp liveness check on
    any failure. Never raises — a Data API 403/429/network error falls
    back to yt-dlp and this still returns a bool either way.

    A video that is simply gone (deleted, private, taken down, or never
    archived) counts as ended rather than as a failure: zero items from the
    Data API, or yt-dlp reporting the video unavailable, both resolve to
    ``True`` so a stuck watch can proceed through ``ended_pending_vod`` and
    its existing timeout instead of being retried as ``state=live`` forever.
    """
    config = get_config()
    api_key = config.resolve_youtube_data_api_key()

    result: Optional[bool] = None
    if api_key:
        try:
            result = await asyncio.to_thread(_has_broadcast_ended_via_data_api, video_id, api_key)
        except Exception as exc:
            logger.warning(
                "YouTube Data API broadcast-end check failed for %s: %s. Falling back to yt-dlp.",
                video_id,
                exc,
            )

    if result is not None:
        return result

    try:
        return await asyncio.to_thread(_has_broadcast_ended_via_ytdlp, video_id)
    except Exception as exc:
        logger.warning("yt-dlp broadcast-end check failed for %s: %s", video_id, exc)
        return False
