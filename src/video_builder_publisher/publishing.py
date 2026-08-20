"""Reusable publisher adapters for YouTube, TikTok and Instagram."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

import requests

from .security import (
    assert_private_file,
    atomic_write_text,
    redact_sensitive_text,
    validate_https_url,
)

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TIKTOK_UPLOAD_HOSTS = ("tiktokapis.com",)
INSTAGRAM_UPLOAD_HOSTS = ("rupload.facebook.com",)
INSTAGRAM_MAX_FILE_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PublishResult:
    status: str
    external_id: str | None = None


class PublishError(RuntimeError):
    def __init__(
        self,
        message: str,
        retryable: bool = False,
        outcome_uncertain: bool = False,
        external_id: str | None = None,
        secret_values: Iterable[str] = (),
    ) -> None:
        super().__init__(redact_sensitive_text(message, secret_values))
        self.retryable = retryable
        self.outcome_uncertain = outcome_uncertain
        self.external_id = external_id


class Publisher(Protocol):
    platform: str

    def publish(self, video: Path, title: str, description: str) -> PublishResult: ...


@dataclass(frozen=True, slots=True)
class YouTubeConfig:
    token_file: Path
    privacy: str = "private"

    def __post_init__(self) -> None:
        if self.privacy not in {"private", "unlisted", "public"}:
            raise ValueError("youtube privacy must be private, unlisted, or public")


@dataclass(frozen=True, slots=True)
class TikTokConfig:
    access_token: str
    mode: str = "draft"
    privacy: str = "SELF_ONLY"

    def __post_init__(self) -> None:
        if not self.access_token.strip():
            raise ValueError("TikTok access token cannot be empty")
        if self.mode != "draft":
            raise ValueError(
                "Unattended TikTok Direct Post is intentionally disabled. "
                "Use draft/inbox upload unless an interactive consent flow is implemented."
            )


@dataclass(frozen=True, slots=True)
class InstagramConfig:
    access_token: str
    user_id: str
    api_version: str
    share_to_feed: bool = False

    def __post_init__(self) -> None:
        if not self.access_token.strip():
            raise ValueError("Instagram access token cannot be empty")
        if not re.fullmatch(r"\d+", self.user_id):
            raise ValueError("Instagram user_id must contain digits only")
        if not re.fullmatch(r"v\d+\.\d+", self.api_version):
            raise ValueError("Instagram api_version must look like v25.0")


def _check_response(
    response: requests.Response,
    platform: str,
    *,
    secret_values: Iterable[str] = (),
) -> dict[str, Any]:
    if response.status_code >= 500 or response.status_code in {408, 429}:
        raise PublishError(
            f"{platform} transient HTTP {response.status_code}",
            retryable=True,
            secret_values=secret_values,
        )
    if response.status_code >= 400:
        raise PublishError(
            f"{platform} HTTP {response.status_code}: {response.text[:500]}",
            secret_values=secret_values,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PublishError(
            f"{platform} returned invalid JSON",
            secret_values=secret_values,
        ) from exc
    if not isinstance(payload, dict):
        raise PublishError(
            f"{platform} returned a non-object JSON payload",
            secret_values=secret_values,
        )
    return payload


def _uncertain(
    platform: str,
    message: str,
    *,
    external_id: str | None = None,
    secret_values: Iterable[str] = (),
) -> PublishError:
    return PublishError(
        f"{platform} outcome is uncertain: {message}",
        outcome_uncertain=True,
        external_id=external_id,
        secret_values=secret_values,
    )


def _validate_video_file(video: Path) -> int:
    if not video.is_file():
        raise PublishError(f"Video does not exist or is not a regular file: {video}")
    size = video.stat().st_size
    if size <= 0:
        raise PublishError(f"Video is empty: {video}")
    return size


class YouTubePublisher:
    platform = "youtube"

    def __init__(self, config: YouTubeConfig) -> None:
        self.config = config

    def publish(self, video: Path, title: str, description: str) -> PublishResult:
        _validate_video_file(video)
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise PublishError(
                "Install video-builder-publisher[youtube] to enable YouTube uploads."
            ) from exc

        token_file = assert_private_file(
            Path(self.config.token_file).expanduser(),
            "YouTube OAuth token file",
        )
        try:
            credentials = Credentials.from_authorized_user_file(
                str(token_file),
                [YOUTUBE_UPLOAD_SCOPE],
            )
        except (OSError, ValueError) as exc:
            raise PublishError("YouTube OAuth token file is invalid.") from exc

        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception as exc:
                raise PublishError(
                    f"YouTube OAuth refresh failed: {type(exc).__name__}",
                    retryable=True,
                ) from exc
            atomic_write_text(token_file, credentials.to_json())
        if not credentials.valid:
            raise PublishError("YouTube OAuth credentials are invalid or cannot be refreshed.")

        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title[:100],
                    "description": description[:5000],
                },
                "status": {"privacyStatus": self.config.privacy},
            },
            media_body=MediaFileUpload(
                str(video),
                chunksize=8 * 1024 * 1024,
                resumable=True,
            ),
        )

        response = None
        while response is None:
            try:
                _, response = request.next_chunk()
            except Exception as exc:
                raise _uncertain(
                    "YouTube",
                    f"resumable upload failed ({type(exc).__name__})",
                ) from exc

        video_id = str(response.get("id")) if response and response.get("id") else None
        if not video_id:
            raise _uncertain("YouTube", "upload completed without a video id")
        return PublishResult("published", video_id)


def authorize_youtube(
    client_secrets_file: str | Path,
    token_file: str | Path,
) -> Path:
    """Run one-time interactive OAuth and write the refresh token privately."""

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise PublishError(
            "Install video-builder-publisher[youtube] to authorize YouTube."
        ) from exc

    secrets_path = assert_private_file(
        Path(client_secrets_file).expanduser(),
        "YouTube client secrets file",
    )
    token_path = Path(token_file).expanduser()
    flow = InstalledAppFlow.from_client_secrets_file(
        str(secrets_path),
        [YOUTUBE_UPLOAD_SCOPE],
    )
    credentials = flow.run_local_server(port=0, open_browser=True)
    atomic_write_text(token_path, credentials.to_json())
    return token_path


class TikTokPublisher:
    platform = "tiktok"
    BASE = "https://open.tiktokapis.com/v2/post/publish"

    def __init__(self, config: TikTokConfig, session: requests.Session | None = None) -> None:
        self.config = config
        self.token = config.access_token
        self.session = session or requests.Session()

    def publish(self, video: Path, title: str, description: str) -> PublishResult:
        del title, description
        file_size = _validate_video_file(video)
        source_info = self._source_info(file_size)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        init = _check_response(
            self.session.post(
                f"{self.BASE}/inbox/video/init/",
                headers=headers,
                json={"source_info": source_info},
                timeout=30,
            ),
            "TikTok",
            secret_values=(self.token,),
        )
        if init.get("error", {}).get("code") not in {None, "ok"}:
            raise PublishError(
                f"TikTok init failed: {init.get('error')}",
                secret_values=(self.token,),
            )

        data = init.get("data", {})
        if not isinstance(data, dict):
            raise PublishError("TikTok init returned invalid data.")
        publish_id = str(data.get("publish_id") or "")
        upload_url = str(data.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise PublishError("TikTok did not return publish_id/upload_url")
        try:
            validate_https_url(upload_url, TIKTOK_UPLOAD_HOSTS)
        except ValueError as exc:
            raise PublishError(f"TikTok returned an unsafe upload URL: {exc}") from exc

        self._upload_file(upload_url, video, publish_id)
        status = self._poll_status(
            publish_id,
            accepted={"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"},
            timeout_seconds=90,
        )
        if status == "PUBLISH_COMPLETE":
            return PublishResult("published", publish_id)
        return PublishResult("draft_uploaded", publish_id)

    @staticmethod
    def _chunk_plan(file_size: int) -> tuple[int, int]:
        if file_size <= 0:
            raise PublishError("TikTok cannot upload an empty video")
        if file_size < 5 * 1024 * 1024:
            return file_size, 1
        chunk_size = min(32 * 1024 * 1024, file_size)
        chunk_count = max(1, math.ceil(file_size / chunk_size))
        return chunk_size, chunk_count

    def _source_info(self, file_size: int) -> dict[str, Any]:
        chunk_size, chunk_count = self._chunk_plan(file_size)
        return {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": chunk_size,
            "total_chunk_count": chunk_count,
        }

    def _upload_file(self, upload_url: str, video: Path, publish_id: str) -> None:
        total = video.stat().st_size
        chunk_size, chunk_count = self._chunk_plan(total)
        with video.open("rb") as handle:
            offset = 0
            for index in range(chunk_count):
                read_size = min(chunk_size, total - offset)
                chunk = handle.read(read_size)
                if len(chunk) != read_size:
                    raise PublishError("TikTok video read ended unexpectedly.")
                end = offset + len(chunk) - 1
                try:
                    response = self.session.put(
                        upload_url,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end}/{total}",
                        },
                        data=chunk,
                        timeout=120,
                    )
                except requests.RequestException as exc:
                    raise _uncertain(
                        "TikTok",
                        f"upload request failed ({type(exc).__name__})",
                        external_id=publish_id,
                        secret_values=(self.token,),
                    ) from exc
                if response.status_code >= 500 or response.status_code == 429:
                    raise _uncertain(
                        "TikTok",
                        f"upload HTTP {response.status_code}",
                        external_id=publish_id,
                        secret_values=(self.token,),
                    )
                if response.status_code >= 400:
                    raise PublishError(
                        f"TikTok upload HTTP {response.status_code}: {response.text[:500]}",
                        secret_values=(self.token,),
                    )
                expected_status = 201 if index == chunk_count - 1 else 206
                if response.status_code != expected_status:
                    raise _uncertain(
                        "TikTok",
                        f"unexpected chunk acknowledgement {response.status_code}; "
                        f"expected {expected_status}",
                        external_id=publish_id,
                        secret_values=(self.token,),
                    )
                offset = end + 1

        if offset != total:
            raise _uncertain(
                "TikTok",
                f"uploaded {offset} of {total} bytes",
                external_id=publish_id,
                secret_values=(self.token,),
            )

    def _poll_status(
        self,
        publish_id: str,
        accepted: set[str],
        timeout_seconds: int,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        deadline = time.monotonic() + timeout_seconds
        last_status = "UNKNOWN"
        while time.monotonic() < deadline:
            try:
                response = self.session.post(
                    f"{self.BASE}/status/fetch/",
                    headers=headers,
                    json={"publish_id": publish_id},
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise _uncertain(
                    "TikTok",
                    f"status request failed ({type(exc).__name__})",
                    external_id=publish_id,
                    secret_values=(self.token,),
                ) from exc
            try:
                payload = _check_response(
                    response,
                    "TikTok",
                    secret_values=(self.token,),
                )
            except PublishError as exc:
                if exc.retryable:
                    raise _uncertain(
                        "TikTok",
                        str(exc),
                        external_id=publish_id,
                        secret_values=(self.token,),
                    ) from exc
                raise

            if payload.get("error", {}).get("code") not in {None, "ok"}:
                raise PublishError(
                    f"TikTok status failed: {payload.get('error')}",
                    secret_values=(self.token,),
                )
            data = payload.get("data", {})
            if not isinstance(data, dict):
                raise PublishError("TikTok status returned invalid data.")
            last_status = str(data.get("status", "UNKNOWN"))
            if last_status in accepted:
                return last_status
            if last_status == "FAILED":
                raise PublishError(
                    f"TikTok processing failed: {data.get('fail_reason', 'unknown')}",
                    secret_values=(self.token,),
                )
            time.sleep(3)

        raise _uncertain(
            "TikTok",
            f"processing status remained {last_status}",
            external_id=publish_id,
            secret_values=(self.token,),
        )


class InstagramPublisher:
    platform = "instagram"

    def __init__(self, config: InstagramConfig, session: requests.Session | None = None) -> None:
        self.config = config
        self.token = config.access_token
        self.user_id = config.user_id
        self.api_version = config.api_version
        self.session = session or requests.Session()
        self.graph = f"https://graph.instagram.com/{self.api_version}"

    def publish(self, video: Path, title: str, description: str) -> PublishResult:
        del title
        file_size = _validate_video_file(video)
        if file_size > INSTAGRAM_MAX_FILE_BYTES:
            raise PublishError("Instagram Reel exceeds the 1 GB file-size limit.")

        headers = {"Authorization": f"Bearer {self.token}"}
        create = _check_response(
            self.session.post(
                f"{self.graph}/{self.user_id}/media",
                headers=headers,
                data={
                    "media_type": "REELS",
                    "upload_type": "resumable",
                    "caption": description[:2200],
                    "share_to_feed": "true" if self.config.share_to_feed else "false",
                },
                timeout=30,
            ),
            "Instagram",
            secret_values=(self.token,),
        )
        container_id = str(create.get("id") or "")
        if not container_id:
            raise PublishError("Instagram did not return a media container id")

        upload_uri = str(
            create.get("uri")
            or f"https://rupload.facebook.com/ig-api-upload/{self.api_version}/{container_id}"
        )
        try:
            validate_https_url(upload_uri, INSTAGRAM_UPLOAD_HOSTS)
        except ValueError as exc:
            raise PublishError(f"Instagram returned an unsafe upload URL: {exc}") from exc

        try:
            with video.open("rb") as handle:
                upload_response = self.session.post(
                    upload_uri,
                    headers={
                        "Authorization": f"OAuth {self.token}",
                        "offset": "0",
                        "file_size": str(file_size),
                        "Content-Type": "video/mp4",
                    },
                    data=handle,
                    timeout=180,
                )
        except requests.RequestException as exc:
            raise PublishError(
                f"Instagram upload request failed ({type(exc).__name__})",
                retryable=True,
                secret_values=(self.token,),
            ) from exc

        if upload_response.status_code >= 500 or upload_response.status_code in {408, 429}:
            raise PublishError(
                f"Instagram upload HTTP {upload_response.status_code}",
                retryable=True,
                secret_values=(self.token,),
            )
        _check_response(
            upload_response,
            "Instagram",
            secret_values=(self.token,),
        )

        self._wait_ready(container_id)
        try:
            publish_response = self.session.post(
                f"{self.graph}/{self.user_id}/media_publish",
                headers=headers,
                data={"creation_id": container_id},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise _uncertain(
                "Instagram",
                f"media_publish request failed ({type(exc).__name__})",
                external_id=container_id,
                secret_values=(self.token,),
            ) from exc
        if publish_response.status_code >= 500 or publish_response.status_code in {408, 429}:
            raise _uncertain(
                "Instagram",
                f"media_publish HTTP {publish_response.status_code}",
                external_id=container_id,
                secret_values=(self.token,),
            )
        published = _check_response(
            publish_response,
            "Instagram",
            secret_values=(self.token,),
        )
        media_id = str(published.get("id") or "")
        if not media_id:
            raise _uncertain(
                "Instagram",
                "media_publish returned no media id",
                external_id=container_id,
                secret_values=(self.token,),
            )
        return PublishResult("published", media_id)

    def _wait_ready(self, container_id: str) -> None:
        deadline = time.monotonic() + 90
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                response = self.session.get(
                    f"{self.graph}/{container_id}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    params={"fields": "status_code,status"},
                    timeout=30,
                )
                payload = _check_response(
                    response,
                    "Instagram",
                    secret_values=(self.token,),
                )
            except requests.RequestException as exc:
                last_error = type(exc).__name__
                time.sleep(3)
                continue
            except PublishError as exc:
                if exc.retryable:
                    last_error = str(exc)
                    time.sleep(3)
                    continue
                raise

            status = str(payload.get("status_code", ""))
            if status in {"FINISHED", "PUBLISHED"}:
                return
            if status in {"ERROR", "EXPIRED"}:
                raise PublishError(
                    f"Instagram container failed: {payload.get('status', status)}",
                    secret_values=(self.token,),
                )
            time.sleep(3)

        raise PublishError(
            "Instagram container processing timed out"
            + (f" after {last_error}" if last_error else ""),
            retryable=True,
            secret_values=(self.token,),
        )


def build_publisher(
    platform: str,
    *,
    youtube: YouTubeConfig | None = None,
    tiktok: TikTokConfig | None = None,
    instagram: InstagramConfig | None = None,
) -> Publisher:
    """Build one publisher from an explicit, project-agnostic credential config."""

    if platform == "youtube":
        if youtube is None:
            raise ValueError("youtube config is required")
        return YouTubePublisher(youtube)
    if platform == "tiktok":
        if tiktok is None:
            raise ValueError("tiktok config is required")
        return TikTokPublisher(tiktok)
    if platform == "instagram":
        if instagram is None:
            raise ValueError("instagram config is required")
        return InstagramPublisher(instagram)
    raise ValueError(f"Unsupported platform: {platform}")
