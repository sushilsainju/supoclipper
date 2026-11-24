"""Unit tests for the YouTube channel-watch detection module.

Covers the hybrid (Data-API-first, yt-dlp-fallback) resolution and
tracking calls, plus the hard quota constraint that live *discovery*
(`probe_channel_live`) must never use the Data API.
"""
import pytest
import yt_dlp

from src import channel_watch_detection as detection
from src.config import Config, set_config_override


@pytest.fixture
def config_with_key(monkeypatch):
    config = Config()
    config.youtube_data_api_key = "test-data-api-key"
    config.google_api_key = ""
    set_config_override(config)
    yield config
    set_config_override(None)


@pytest.fixture
def config_without_key(monkeypatch):
    config = Config()
    config.youtube_data_api_key = ""
    config.google_api_key = ""
    set_config_override(config)
    yield config
    set_config_override(None)


# ---------------------------------------------------------------------------
# resolve_channel
# ---------------------------------------------------------------------------


async def test_resolve_channel_returns_resolved_channel_via_data_api_when_key_configured(
    config_with_key, monkeypatch,
):
    def fake_data_api_request(url, params):
        assert "forHandle" in params
        assert params["forHandle"] == "@SomeChannel"
        return {"items": [{"id": "UCabcdefghijklmnopqrstuv", "snippet": {"title": "Some Channel"}}]}

    def fake_ytdlp_extract_info(url, opts):
        raise AssertionError("yt-dlp must not be used when the Data API succeeds")

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.resolve_channel("@SomeChannel")

    assert result == detection.ResolvedChannel(channel_id="UCabcdefghijklmnopqrstuv", title="Some Channel")


async def test_resolve_channel_falls_back_to_yt_dlp_on_data_api_failure(
    config_with_key, monkeypatch,
):
    def fake_data_api_request(url, params):
        raise RuntimeError("Data API 403")

    def fake_ytdlp_extract_info(url, opts):
        assert "@SomeChannel" in url
        return {"channel_id": "UCfallback", "channel": "Fallback Channel"}

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.resolve_channel("@SomeChannel")

    assert result == detection.ResolvedChannel(channel_id="UCfallback", title="Fallback Channel")


async def test_resolve_channel_returns_none_for_unresolvable_input(
    config_without_key, monkeypatch,
):
    def fake_data_api_request(url, params):
        raise AssertionError("Data API must not be called without a configured key")

    def fake_ytdlp_extract_info(url, opts):
        return {"id": None, "channel_id": None, "title": "Not a channel"}

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.resolve_channel("not-a-real-channel-typo")

    assert result is None


async def test_resolve_channel_returns_none_for_non_youtube_url(config_with_key, monkeypatch):
    # Review Focus case: a Twitch URL must not resolve at all — not via the
    # Data API (its path would otherwise parse as a bare username and could
    # match an unrelated YouTube channel) and not via yt-dlp's own, non-YouTube
    # extractor for that host.
    def fake_data_api_request(url, params):
        raise AssertionError("A non-YouTube URL must never reach the Data API")

    def fake_ytdlp_extract_info(url, opts):
        raise AssertionError("A non-YouTube URL must never reach yt-dlp either")

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.resolve_channel("https://twitch.tv/foo")

    assert result is None


def test_resolve_channel_via_data_api_rejects_a_malformed_channel_id(monkeypatch):
    # A forUsername/forHandle lookup that somehow returns an id not shaped
    # like a real YouTube channel id (UC + 22 chars) must be treated as
    # unresolved rather than handed back as a resolved channel.
    def fake_data_api_request(url, params):
        return {"items": [{"id": "not-a-channel-id", "snippet": {"title": "Bogus"}}]}

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)

    result = detection._resolve_channel_via_data_api("@SomeChannel", "test-data-api-key")

    assert result is None


# ---------------------------------------------------------------------------
# probe_channel_live
# ---------------------------------------------------------------------------


async def test_probe_channel_live_returns_video_id_when_live(config_with_key, monkeypatch):
    def fake_ytdlp_extract_info(url, opts):
        assert "UCabc123" in url and url.endswith("/live")
        return {"id": "liveVideo123", "is_live": True}

    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.probe_channel_live("UCabc123")

    assert result == "liveVideo123"


async def test_probe_channel_live_returns_none_when_offline(config_with_key, monkeypatch):
    def fake_ytdlp_extract_info(url, opts):
        return {"id": "someVideo", "is_live": False}

    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.probe_channel_live("UCabc123")

    assert result is None


async def test_probe_channel_live_never_calls_the_data_api(config_with_key, monkeypatch):
    # Even with a Data API key configured, this check must only ever shell
    # out to yt-dlp — assert the Data API HTTP call is never made.
    def fake_data_api_request(url, params):
        raise AssertionError("probe_channel_live must never call the Data API")

    def fake_ytdlp_extract_info(url, opts):
        return {"id": "liveVideo123", "is_live": True}

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.probe_channel_live("UCabc123")

    assert result == "liveVideo123"


# ---------------------------------------------------------------------------
# has_broadcast_ended
# ---------------------------------------------------------------------------


async def test_has_broadcast_ended_true_via_data_api_actual_end_time(config_with_key, monkeypatch):
    def fake_data_api_request(url, params):
        assert params["id"] == "liveVideo123"
        return {
            "items": [
                {"liveStreamingDetails": {"actualStartTime": "2026-11-23T10:00:00Z", "actualEndTime": "2026-11-23T11:00:00Z"}},
            ],
        }

    def fake_ytdlp_extract_info(url, opts):
        raise AssertionError("yt-dlp must not be used when the Data API succeeds")

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.has_broadcast_ended("liveVideo123")

    assert result is True


async def test_has_broadcast_ended_false_while_still_live(config_with_key, monkeypatch):
    def fake_data_api_request(url, params):
        return {
            "items": [
                {"liveStreamingDetails": {"actualStartTime": "2026-11-23T10:00:00Z"}},
            ],
        }

    def fake_ytdlp_extract_info(url, opts):
        raise AssertionError("yt-dlp must not be used when the Data API succeeds")

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.has_broadcast_ended("liveVideo123")

    assert result is False


async def test_has_broadcast_ended_true_when_video_is_gone(config_with_key, monkeypatch):
    # Review Focus case: a deleted/private/never-archived video must count as
    # ended (not stay stuck in state=live forever). The Data API successfully
    # queries the id and finds nothing — distinct from "still live" (an item
    # with no actualEndTime) and "genuinely ended" (an item with
    # actualEndTime set).
    def fake_data_api_request(url, params):
        assert params["id"] == "goneVideo123"
        return {"items": []}

    def fake_ytdlp_extract_info(url, opts):
        raise AssertionError("yt-dlp must not be used when the Data API gives a conclusive answer")

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.has_broadcast_ended("goneVideo123")

    assert result is True


async def test_has_broadcast_ended_true_when_yt_dlp_reports_video_unavailable(
    config_without_key, monkeypatch,
):
    # Same "video is gone" case, but reached via the yt-dlp fallback (no Data
    # API key configured) — yt-dlp raises instead of returning an empty
    # result for an unavailable video.
    def fake_ytdlp_extract_info(url, opts):
        assert "goneVideo123" in url
        raise yt_dlp.utils.DownloadError(
            "ERROR: [youtube] goneVideo123: Video unavailable. This video has been removed by the uploader"
        )

    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.has_broadcast_ended("goneVideo123")

    assert result is True


async def test_has_broadcast_ended_falls_back_to_yt_dlp_on_data_api_error(config_with_key, monkeypatch):
    # Review Focus case: Data API 403/429/network error must not raise —
    # it must fall back to the yt-dlp check and still return a bool.
    def fake_data_api_request(url, params):
        raise RuntimeError("Data API 403 Forbidden")

    def fake_ytdlp_extract_info(url, opts):
        assert "liveVideo123" in url
        return {"is_live": False}

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.has_broadcast_ended("liveVideo123")

    assert result is True
