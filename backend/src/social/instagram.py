"""
Instagram Reels publishing via the Instagram API with Unified Login.

Meta retired the standalone "API setup with Instagram login" for apps
created after mid-2026; every new app is routed to "API setup with Unified
login" instead, which is Facebook Login for Business configured with a
System-User Access Token (SUAT). Practical differences from the old flow:

- The authorize dialog is Facebook's (``facebook.com/dialog/oauth``) and
  takes a ``config_id`` instead of a ``scope`` list; the permissions are
  chosen once, in the Meta dashboard, when the configuration is created.
- The code exchange returns the final access token directly. There is no
  separate "exchange for a long-lived token" step, and the token is a
  System-User token that is configured to not expire, so ``refresh_tokens``
  has nothing to do -- it exists only to produce a clear error if it is
  ever reached.
- ``/me`` returns the system user, not an Instagram account. The connected
  Instagram professional account(s) are discovered via
  ``/me/assigned_instagram_accounts``, and that id (not ``me``) is what
  every publish/metrics call targets.
- Every Graph API call goes through ``graph.facebook.com`` instead of
  ``graph.instagram.com``; most request/response shapes are unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from ..config import Config, get_config
from .base import (
    DEFAULT_HTTP_TIMEOUT,
    AccountProfile,
    OAuthTokens,
    PostMetrics,
    PublishRequest,
    PublishResult,
    SocialProvider,
    SocialProviderError,
    ambiguous_outcome,
    build_caption,
)

logger = logging.getLogger(__name__)

API_VERSION = "v23.0"
AUTHORIZE_URL = "https://www.facebook.com/dialog/oauth"
TOKEN_URL = f"https://graph.facebook.com/{API_VERSION}/oauth/access_token"
GRAPH_API = f"https://graph.facebook.com/{API_VERSION}"

CAPTION_MAX_LENGTH = 2200
CONTAINER_POLL_INTERVAL_SECONDS = 5
CONTAINER_POLL_TIMEOUT_SECONDS = 600

INSIGHT_METRIC_SETS = (
    "views,reach,shares,saved",
    "plays,reach,shares,saved",
)


class InstagramProvider(SocialProvider):
    name = "instagram"
    display_name = "Instagram Reels"
    supported_privacy_levels = ("public",)
    needs_local_file = False

    def __init__(self, config: Optional[Config] = None, session=None, sleep=time.sleep):
        super().__init__(session)
        self.config = config or get_config()
        self._sleep = sleep

    @property
    def is_configured(self) -> bool:
        return bool(
            self.config.instagram_app_id
            and self.config.instagram_app_secret
            and self.config.instagram_config_id
        )

    # -- OAuth ----------------------------------------------------------
    def build_authorize_url(
        self, *, state: str, redirect_uri: str, code_challenge: Optional[str] = None
    ) -> str:
        # Unified Login configurations carry their own permission set; a
        # ``scope`` parameter here is not used and would be ignored.
        params = {
            "client_id": self.config.instagram_app_id,
            "config_id": self.config.instagram_config_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: Optional[str] = None
    ) -> OAuthTokens:
        response = self.http.get(
            TOKEN_URL,
            params={
                "client_id": self.config.instagram_app_id,
                "client_secret": self.config.instagram_app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        if response.status_code >= 400:
            raise SocialProviderError(
                f"Instagram token exchange failed: {self._extract_error(response)}"
            )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise SocialProviderError("Instagram did not return an access token")
        # Configured to never expire (System-User Access Token); if an
        # operator's configuration sets an expiry, Meta includes expires_in.
        return OAuthTokens(
            access_token=token,
            refresh_token=None,
            expires_at=None,
            raw={"token_type": payload.get("token_type")},
        )

    def refresh_tokens(self, refresh_token: str) -> OAuthTokens:
        # Unified Login's System-User Access Token is configured to never
        # expire and has no documented refresh grant. _get_valid_access_token
        # never calls this because exchange_code never sets expires_at /
        # refresh_token -- reaching it means the stored token was revoked.
        raise SocialProviderError(
            "Instagram's access token does not support refreshing; reconnect the account.",
            reauth=True,
        )

    # Unified Login has no token revocation endpoint (users remove the app
    # from their Instagram/Business settings), so ``revoke_tokens`` keeps the
    # base no-op; disconnecting still deletes the stored token.

    def fetch_profile(self, access_token: str) -> AccountProfile:
        accounts_response = self.http.get(
            f"{GRAPH_API}/me/assigned_instagram_accounts",
            params={"fields": "id,username"},
            headers=self._auth_headers(access_token),
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        self._raise_for_status(accounts_response, "list connected Instagram accounts")
        accounts = accounts_response.json().get("data") or []
        if not accounts:
            raise SocialProviderError(
                "No Instagram professional account was granted during login. Grant access to "
                "a Business or Creator Instagram account and try connecting again."
            )
        ig_id = str(accounts[0].get("id") or "")
        username = accounts[0].get("username")
        if not ig_id:
            raise SocialProviderError("Instagram did not return an account id")

        # No account_type check here: assigned_instagram_accounts only ever
        # lists accounts that belong to a Business Portfolio, and a personal
        # account can't be added to one, so every result is already
        # professional. (account_type also isn't a valid field on this node.)
        detail_response = self.http.get(
            f"{GRAPH_API}/{ig_id}",
            params={"fields": "name,profile_picture_url"},
            headers=self._auth_headers(access_token),
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        self._raise_for_status(detail_response, "load the Instagram profile")
        detail = detail_response.json()
        return AccountProfile(
            external_account_id=ig_id,
            display_name=detail.get("name") or username,
            username=username,
            avatar_url=detail.get("profile_picture_url"),
            metadata={"ig_user_id": ig_id},
        )

    # -- publishing -----------------------------------------------------
    def publish(self, access_token: str, request: PublishRequest) -> PublishResult:
        if not request.public_media_url:
            raise SocialProviderError(
                "Instagram needs a publicly reachable video URL. Set NEXT_PUBLIC_APP_URL "
                "(or SOCIAL_PUBLIC_MEDIA_BASE_URL) to a URL Instagram can fetch."
            )
        ig_user_id = request.account_metadata.get("ig_user_id")
        if not ig_user_id:
            raise SocialProviderError(
                "This account is missing its Instagram account id; reconnect it.", reauth=True
            )
        caption = build_caption(request.caption or request.title, request.hashtags, CAPTION_MAX_LENGTH)

        create_response = self.http.post(
            f"{GRAPH_API}/{ig_user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": request.public_media_url,
                "caption": caption,
                "share_to_feed": "true",
            },
            headers=self._auth_headers(access_token),
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        self._raise_for_status(create_response, "create the Instagram media container")
        container_id = create_response.json().get("id")
        if not container_id:
            raise SocialProviderError("Instagram did not return a media container id")

        self._wait_for_container(access_token, container_id)

        # The container is harmless until published; media_publish may create
        # the reel even if its response is lost.
        with ambiguous_outcome("publish the Instagram reel"):
            publish_response = self.http.post(
                f"{GRAPH_API}/{ig_user_id}/media_publish",
                data={"creation_id": container_id},
                headers=self._auth_headers(access_token),
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
            self._raise_for_status(publish_response, "publish the Instagram reel")
            media_id = publish_response.json().get("id")
        if not media_id:
            raise SocialProviderError("Instagram publish finished without a media id")

        permalink = None
        try:
            link_response = self.http.get(
                f"{GRAPH_API}/{media_id}",
                params={"fields": "permalink"},
                headers=self._auth_headers(access_token),
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
            if link_response.status_code < 400:
                permalink = link_response.json().get("permalink")
        except Exception:  # pragma: no cover - best effort
            logger.debug("Could not load Instagram permalink", exc_info=True)

        return PublishResult(
            external_post_id=str(media_id),
            external_url=permalink,
            raw={"container_id": container_id},
        )

    def _wait_for_container(self, access_token: str, container_id: str) -> None:
        deadline = time.monotonic() + CONTAINER_POLL_TIMEOUT_SECONDS
        while True:
            response = self.http.get(
                f"{GRAPH_API}/{container_id}",
                params={"fields": "status_code,status"},
                headers=self._auth_headers(access_token),
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
            self._raise_for_status(response, "check the Instagram media container")
            payload = response.json()
            status_code = payload.get("status_code")
            if status_code == "FINISHED":
                return
            if status_code in {"ERROR", "EXPIRED"}:
                raise SocialProviderError(
                    f"Instagram could not process the video: {payload.get('status') or status_code}"
                )
            if time.monotonic() > deadline:
                raise SocialProviderError(
                    "Timed out waiting for Instagram to process the video", retryable=True
                )
            self._sleep(CONTAINER_POLL_INTERVAL_SECONDS)

    # -- analytics ------------------------------------------------------
    def fetch_metrics(
        self, access_token: str, external_post_id: str, account_metadata: Dict[str, Any]
    ) -> PostMetrics:
        response = self.http.get(
            f"{GRAPH_API}/{external_post_id}",
            params={"fields": "like_count,comments_count,permalink"},
            headers=self._auth_headers(access_token),
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        self._raise_for_status(response, "load Instagram media statistics")
        basic = response.json()
        metrics = PostMetrics(
            likes=_to_int(basic.get("like_count")),
            comments=_to_int(basic.get("comments_count")),
            raw={"basic": basic},
        )

        for metric_set in INSIGHT_METRIC_SETS:
            insight_response = self.http.get(
                f"{GRAPH_API}/{external_post_id}/insights",
                params={"metric": metric_set},
                headers=self._auth_headers(access_token),
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
            if insight_response.status_code >= 400:
                continue
            values: Dict[str, Optional[int]] = {}
            for entry in insight_response.json().get("data") or []:
                name = entry.get("name")
                entry_values = entry.get("values") or []
                value = entry_values[0].get("value") if entry_values else entry.get("value")
                values[name] = _to_int(value)
            metrics.views = values.get("views", values.get("plays"))
            metrics.shares = values.get("shares")
            metrics.saves = values.get("saved")
            metrics.raw["insights"] = values
            break
        return metrics

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _auth_headers(access_token: str) -> Dict[str, str]:
        # Graph API calls take the token as a header so it never lands in a
        # URL (and from there in exception messages or proxy logs). The token
        # exchange endpoint only accepts query parameters.
        return {"Authorization": f"Bearer {access_token}"}

    def _raise_for_status(self, response, action: str) -> None:
        if response.status_code < 400:
            return
        message = self._extract_error(response)
        lowered = message.lower()
        if response.status_code in (401,) or "oauth" in lowered or "session has expired" in lowered or "(190)" in message:
            raise SocialProviderError(
                f"Instagram rejected the credentials while trying to {action}: {message}",
                reauth=True,
            )
        retryable = response.status_code >= 500 or "(4)" in message or "(17)" in message or "(32)" in message
        raise SocialProviderError(f"Failed to {action}: {message}", retryable=retryable)


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
