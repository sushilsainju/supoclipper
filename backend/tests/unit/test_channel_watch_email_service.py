"""Unit tests for ``ChannelWatchEmailService``.

Mirrors ``tests/unit/test_social_publish_email_service.py``: build the
``EmailContent`` in isolation, then confirm ``send_stream_ready_email``
delegates to ``SesEmailService.send_email`` with it.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.services.channel_watch_email_service import ChannelWatchEmailService


def email_config(**extra):
    return SimpleNamespace(
        aws_region="eu-west-1",
        aws_access_key_id="k",
        aws_secret_access_key="s",
        ses_from_email="SupoClip <no-reply@supoclip.com>",
        app_base_url="https://app.supoclip.com",
        **extra,
    )


def test_builds_stream_ready_email_with_channel_title():
    service = ChannelWatchEmailService(email_config())
    content = service._build_stream_ready_email(
        channel_title="Ada's <Channel>",
        video_url="https://www.youtube.com/watch?v=abc123",
        duration_seconds=5400,
    )

    assert content.subject == "Your watched channel's stream is ready to clip"
    assert "<strong>Ada&#x27;s &lt;Channel&gt;</strong>" in content.html
    assert 'href="https://www.youtube.com/watch?v=abc123"' in content.html
    assert "90 min" in content.html
    assert "Ada's <Channel>" in content.text
    assert "https://www.youtube.com/watch?v=abc123" in content.text
    assert 'href="https://app.supoclip.com/list"' in content.html
    assert 'href="https://app.supoclip.com/settings"' in content.html


def test_builds_stream_ready_email_without_channel_title():
    service = ChannelWatchEmailService(email_config())
    content = service._build_stream_ready_email(
        channel_title=None,
        video_url="https://www.youtube.com/watch?v=xyz789",
        duration_seconds=125,
    )

    assert "A watched channel's livestream" in content.html
    assert "2 min" in content.html
    assert "javascript:" not in content.html


def test_builds_stream_ready_email_rejects_unsafe_video_url():
    service = ChannelWatchEmailService(email_config())
    content = service._build_stream_ready_email(
        channel_title="Some Channel",
        video_url="javascript:alert(1)",
        duration_seconds=60,
    )

    assert "javascript:" not in content.html
    assert "javascript:" not in content.text


@pytest.mark.asyncio
async def test_send_stream_ready_email_builds_expected_content():
    service = ChannelWatchEmailService(email_config())
    service.email_service.send_email = AsyncMock(return_value={"MessageId": "msg-1"})

    result = await service.send_stream_ready_email(
        recipient="ada@example.com",
        channel_title="Ada's Channel",
        video_url="https://www.youtube.com/watch?v=abc123",
        duration_seconds=3600,
    )

    assert result == {"MessageId": "msg-1"}
    service.email_service.send_email.assert_awaited_once()
    recipient, content = service.email_service.send_email.await_args.args
    assert recipient == "ada@example.com"
    assert content.subject == "Your watched channel's stream is ready to clip"
    assert "Ada&#x27;s Channel" in content.html
    assert "https://www.youtube.com/watch?v=abc123" in content.html
    assert "60 min" in content.html
