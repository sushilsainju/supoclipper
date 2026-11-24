"""Unit tests for the YouTube channel-watch detection module.

Covers the hybrid (Data-API-first, yt-dlp-fallback) resolution and
tracking calls, plus the hard quota constraint that live *discovery*
(`probe_channel_live`) must never use the Data API.
"""
import pytest

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
        return {"items": [{"id": "UCabc123", "snippet": {"title": "Some Channel"}}]}

    def fake_ytdlp_extract_info(url, opts):
        raise AssertionError("yt-dlp must not be used when the Data API succeeds")

    monkeypatch.setattr(detection, "_data_api_request", fake_data_api_request)
    monkeypatch.setattr(detection, "_ytdlp_extract_info", fake_ytdlp_extract_info)

    result = await detection.resolve_channel("@SomeChannel")

    assert result == detection.ResolvedChannel(channel_id="UCabc123", title="Some Channel")


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
