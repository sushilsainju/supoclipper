"""Email notification for channel watching: tell a user that a livestream on a
channel they watch has just ended and its VOD is ready to clip.

Mirrors ``SocialPublishEmailService``'s shape exactly.
"""
from __future__ import annotations

from html import escape
from typing import Optional
from urllib.parse import urlparse

from ..config import Config
from .email_layout import email_button
from .email_service import EmailContent, SesEmailService, first_name_for


def _safe_http_url(url: Optional[str]) -> Optional[str]:
    candidate = (url or "").strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return candidate
    return None


class ChannelWatchEmailService:
    """Send a notification when a watched channel's livestream VOD is ready to clip."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.email_service = SesEmailService(self.config)
        self.app_url = self.config.app_base_url

    @property
    def is_configured(self) -> bool:
        return self.email_service.is_configured

    async def send_stream_ready_email(
        self,
        *,
        recipient: str,
        channel_title: Optional[str],
        video_url: str,
        duration_seconds: int,
    ) -> dict:
        content = self._build_stream_ready_email(
            channel_title=channel_title,
            video_url=video_url,
            duration_seconds=duration_seconds,
        )
        return await self.email_service.send_email(recipient, content)

    def _build_stream_ready_email(
        self,
        *,
        channel_title: Optional[str],
        video_url: str,
        duration_seconds: int,
    ) -> EmailContent:
        first_name = first_name_for()
        clean_title = (channel_title or "").strip() or None
        minutes = max(1, int(duration_seconds or 0) // 60)
        safe_video_url = _safe_http_url(video_url)
        make_clips_url = f"{self.app_url}/list"
        settings_url = f"{self.app_url}/settings"

        if clean_title:
            headline_html = (
                f"<strong>{escape(clean_title)}</strong>'s livestream just ended "
                f"(about {minutes} min), and it's ready to clip."
            )
            headline_text = (
                f'"{clean_title}"\'s livestream just ended (about {minutes} min), '
                "and it's ready to clip."
            )
        else:
            headline_html = (
                "A watched channel's livestream just ended "
                f"(about {minutes} min), and it's ready to clip."
            )
            headline_text = (
                "A watched channel's livestream just ended "
                f"(about {minutes} min), and it's ready to clip."
            )

        button_html = email_button(make_clips_url, "Make clips")

        if safe_video_url:
            footer_html = (
                f'<p>You can also <a href="{escape(safe_video_url, quote=True)}">'
                "watch the stream</a> or "
                f'<a href="{escape(settings_url, quote=True)}">'
                "manage your notification settings</a>.</p>"
            )
            links_text = (
                f"Make clips: {make_clips_url}\n"
                f"Watch the stream: {safe_video_url}\n"
                f"Notification settings: {settings_url}"
            )
        else:
            footer_html = (
                f'<p>You can <a href="{escape(settings_url, quote=True)}">'
                "manage your notification settings</a> anytime.</p>"
            )
            links_text = (
                f"Make clips: {make_clips_url}\n"
                f"Notification settings: {settings_url}"
            )

        return EmailContent(
            subject="Your watched channel's stream is ready to clip",
            html=(
                f"<p>Hi {escape(first_name)},</p>"
                f"<p>{headline_html}</p>"
                f"{button_html}"
                f"{footer_html}"
                "<p>Team SupoClip</p>"
            ),
            text=(
                f"Hi {first_name},\n\n"
                f"{headline_text}\n\n"
                f"{links_text}\n\n"
                "Team SupoClip"
            ),
        )
