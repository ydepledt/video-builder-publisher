"""Shared environment-to-platform configuration for generated-video applications."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .analytics_instagram import InstagramAnalyticsClient
from .analytics_platforms import (
    InstagramAnalyticsConfig,
    TikTokAnalyticsClient,
    TikTokAnalyticsConfig,
    YouTubeAnalyticsClient,
    YouTubeAnalyticsConfig,
)
from .publishing import (
    InstagramConfig,
    TikTokConfig,
    YouTubeConfig,
    build_publisher,
)
from .security import require_env

_SUPPORTED = ("youtube", "tiktok", "instagram")


def _normalize_prefix(prefix: str) -> str:
    value = prefix.strip().upper().replace("-", "_")
    if not value:
        raise ValueError("environment prefix cannot be empty")
    return value


def _file_env_exists(name: str) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return False
    path = Path(raw).expanduser()
    return path.is_file() and not path.is_symlink()


def _secret_env_exists(name: str) -> bool:
    direct = os.environ.get(name, "").strip()
    file_name = os.environ.get(f"{name}_FILE", "").strip()
    if direct and file_name:
        return False
    if direct:
        return True
    return _file_env_exists(f"{name}_FILE") if file_name else False


@dataclass(frozen=True, slots=True)
class PlatformEnvironment:
    """Map one application-specific env prefix onto shared platform adapters."""

    prefix: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "prefix", _normalize_prefix(self.prefix))

    def name(self, suffix: str) -> str:
        return f"{self.prefix}_{suffix}"

    def publisher_configured(self, platform: str) -> bool:
        self._validate_platform(platform)
        if platform == "youtube":
            return _file_env_exists(self.name("YOUTUBE_TOKEN_FILE"))
        if platform == "tiktok":
            return _secret_env_exists(self.name("TIKTOK_ACCESS_TOKEN"))
        return (
            _secret_env_exists(self.name("INSTAGRAM_ACCESS_TOKEN"))
            and bool(os.environ.get(self.name("INSTAGRAM_USER_ID"), "").strip())
        )

    def build_publisher(self, platform: str):
        self._validate_platform(platform)
        if platform == "youtube":
            token_path = self._required_path(self.name("YOUTUBE_TOKEN_FILE"))
            privacy = os.environ.get(self.name("YOUTUBE_PRIVACY"), "private").strip() or "private"
            if privacy == "public" and os.environ.get(self.name("ALLOW_PUBLIC_YOUTUBE")) != "1":
                raise RuntimeError(
                    f"Public YouTube upload is disabled; set {self.name('ALLOW_PUBLIC_YOUTUBE')}=1 "
                    "only after deliberate validation"
                )
            return build_publisher(
                "youtube",
                youtube=YouTubeConfig(token_file=token_path, privacy=privacy),
            )
        if platform == "tiktok":
            return build_publisher(
                "tiktok",
                tiktok=TikTokConfig(
                    access_token=require_env(self.name("TIKTOK_ACCESS_TOKEN")),
                    mode="draft",
                ),
            )
        return build_publisher(
            "instagram",
            instagram=InstagramConfig(
                access_token=require_env(self.name("INSTAGRAM_ACCESS_TOKEN")),
                user_id=self._required_text(self.name("INSTAGRAM_USER_ID")),
                api_version=(
                    os.environ.get(self.name("INSTAGRAM_API_VERSION"), "v25.0").strip()
                    or "v25.0"
                ),
                share_to_feed=False,
            ),
        )

    def analytics_configured(self, platform: str) -> bool:
        self._validate_platform(platform)
        if platform == "youtube":
            dedicated = self.name("YOUTUBE_ANALYTICS_TOKEN_FILE")
            return _file_env_exists(dedicated) or _file_env_exists(self.name("YOUTUBE_TOKEN_FILE"))
        if platform == "tiktok":
            dedicated = self.name("TIKTOK_ANALYTICS_ACCESS_TOKEN")
            return _secret_env_exists(dedicated) or _secret_env_exists(
                self.name("TIKTOK_ACCESS_TOKEN")
            )
        dedicated = self.name("INSTAGRAM_ANALYTICS_ACCESS_TOKEN")
        return _secret_env_exists(dedicated) or _secret_env_exists(
            self.name("INSTAGRAM_ACCESS_TOKEN")
        )

    def build_analytics_client(self, platform: str):
        self._validate_platform(platform)
        if platform == "youtube":
            dedicated = self.name("YOUTUBE_ANALYTICS_TOKEN_FILE")
            token_path = (
                self._required_path(dedicated)
                if os.environ.get(dedicated, "").strip()
                else self._required_path(self.name("YOUTUBE_TOKEN_FILE"))
            )
            return YouTubeAnalyticsClient(YouTubeAnalyticsConfig(token_file=token_path))
        if platform == "tiktok":
            token = self._analytics_secret(
                self.name("TIKTOK_ANALYTICS_ACCESS_TOKEN"),
                self.name("TIKTOK_ACCESS_TOKEN"),
            )
            return TikTokAnalyticsClient(TikTokAnalyticsConfig(access_token=token))
        token = self._analytics_secret(
            self.name("INSTAGRAM_ANALYTICS_ACCESS_TOKEN"),
            self.name("INSTAGRAM_ACCESS_TOKEN"),
        )
        api_version = (
            os.environ.get(
                self.name("INSTAGRAM_ANALYTICS_API_VERSION"),
                os.environ.get(self.name("INSTAGRAM_API_VERSION"), "v25.0"),
            ).strip()
            or "v25.0"
        )
        return InstagramAnalyticsClient(
            InstagramAnalyticsConfig(access_token=token, api_version=api_version)
        )

    @staticmethod
    def _validate_platform(platform: str) -> None:
        if platform not in _SUPPORTED:
            raise ValueError(f"Unsupported platform: {platform}")

    @staticmethod
    def _required_path(name: str) -> Path:
        raw = os.environ.get(name, "").strip()
        if not raw:
            raise RuntimeError(f"Missing required environment variable: {name}")
        path = Path(raw).expanduser()
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Credential file is missing or not a regular file: {path}")
        return path

    @staticmethod
    def _required_text(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return value

    @staticmethod
    def _analytics_secret(dedicated: str, fallback: str) -> str:
        if os.environ.get(dedicated, "").strip() or os.environ.get(
            f"{dedicated}_FILE", ""
        ).strip():
            return require_env(dedicated)
        return require_env(fallback)
