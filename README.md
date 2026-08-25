# Video Builder Publisher

Shared Python primitives for **building, validating, queueing, publishing and measuring generated videos**.

This repository keeps media/output infrastructure out of individual content projects such as `shazam`, `live-stats`/EarthPulse and future generators.

The package deliberately contains **no domain-specific content logic**: no Shazam chart rules, no earthquake scoring, no project-specific editorial policy.

## Package layout

```text
video_builder_publisher/
├── video.py                # profiles, FFmpeg streaming, atomic MP4, ffprobe, audio mux
├── security.py             # secrets, redaction, URL allow-lists, locks, atomic writes
├── queue.py                # immutable SHA-256 artifact queue
├── store.py                # generic SQLite run/platform publication state
├── publishing.py           # YouTube, TikTok and Instagram upload adapters
├── analytics.py            # normalized metrics, checkpoints, retention, SQLite state
└── analytics_platforms.py  # YouTube/TikTok/Instagram analytics adapters
```

The dependency direction is intentional:

```text
project-specific content
        |
        |  AnalyticsProfile / ContentContext
        v
video_builder_publisher
        |
        +--> render / validate
        +--> queue / integrity
        +--> publish / recover
        +--> collect / normalize / checkpoint performance
```

Projects remain responsible for **what** to render and which custom dimensions describe the content. This package owns the reusable mechanics for **how** media is produced, safely published and measured.

## Install

Python 3.12+ is required. FFmpeg and ffprobe are required for video encoding/validation.

```bash
python -m pip install -e .

# Development
python -m pip install -e '.[dev]'

# YouTube upload + analytics OAuth support
python -m pip install -e '.[youtube]'
```

## Video building

```python
from video_builder_publisher.video import RawVideoEncoder, resolve_video_spec

spec = resolve_video_spec("shorts")

with RawVideoEncoder("output.mp4", spec) as encoder:
    for frame in frames:  # BGR uint8, H x W x 3
        encoder.write_frame(frame)
```

Built-in profiles:

- `shorts`: 1080x1920 @ 60 fps
- `social`: 1080x1920 @ 30 fps
- `landscape`: 1920x1080 @ 60 fps

The video layer also exposes atomic targets, `ffprobe` validation and audio muxing without re-encoding the video stream.

## Immutable publication queue

```python
from datetime import date
from video_builder_publisher.queue import PublishQueue

queue = PublishQueue("publish_queue")
artifact, sha256 = queue.stage(
    "output.mp4",
    date.today(),
    run_key="my-project|2026-08-20",
    manifest={"title": "Example"},
)
```

Each logical run receives its own directory. Artifacts are SHA-256 checked before and after staging and manifests reject secret-like fields.

## Publication state

`PublicationStore` implements the fail-closed state machine used around remote publishing:

- `publishing` is written immediately before remote I/O;
- a process crash while publishing is recovered as `unknown`;
- successful/submitted/draft/unknown results are not blindly retried;
- per-platform attempts and external IDs are persisted in SQLite.

The generic term `scope_key` is used instead of a project-specific concept such as `chart_key`.

## Publishers

Credentials are **explicit configuration**, not hard-coded environment variable names.

```python
from pathlib import Path
from video_builder_publisher.publishing import (
    YouTubeConfig,
    YouTubePublisher,
    TikTokConfig,
    TikTokPublisher,
    InstagramConfig,
    InstagramPublisher,
)

YouTubePublisher(
    YouTubeConfig(token_file=Path("~/.config/app/youtube-token.json").expanduser())
)
TikTokPublisher(TikTokConfig(access_token="..."))
InstagramPublisher(
    InstagramConfig(
        access_token="...",
        user_id="123456789",
        api_version="v25.0",
    )
)
```

Application projects may read secrets from environment variables or Docker secrets in their own adaptation layer and pass the resulting values here.

TikTok unattended Direct Post remains intentionally disabled; the reusable publisher supports creator-inbox/draft upload unless a separate interactive consent flow is implemented.

## Shared analytics

The analytics layer deliberately separates **global platform metrics** from **generator-specific dimensions**.

Global normalized metrics live here:

```text
views
engaged_views
likes
comments
shares
saves
reach
total_interactions
watch_time_seconds
average_view_duration_seconds
average_view_percentage
skip_rate
retention curve
1h / 6h / 24h / 7d checkpoints
```

A generator only supplies context through an `AnalyticsProfile`:

```python
from video_builder_publisher.analytics import (
    AnalyticsProfile,
    ContentContext,
)

class MyProfile(AnalyticsProfile):
    def build_context(self, publication, manifest):
        manifest = manifest or {}
        return ContentContext(
            run_key=publication.run_key,
            scope_key=publication.scope_key,
            run_date=publication.run_date,
            title=manifest.get("title"),
            content_format=publication.content_format,
            duration_seconds=manifest.get("duration_seconds"),
            dimensions={
                "experiment": manifest.get("experiment"),
                "content_category": manifest.get("category"),
            },
        )
```

EarthPulse can therefore add earthquake/story dimensions, while Shazam can add chart/track/format dimensions, with no copy of the platform collectors or metric schema.

The persistent analytics database is separate from publication state:

```text
state/
├── publication.sqlite  # remote publication state machine
└── analytics.sqlite    # performance history and checkpoints
```

Both files belong to persistent runtime state and should **not** be committed to Git.

`PublicationAnalyticsSource` imports published/submitted targets from the generic publication schema. `AnalyticsStore` records immutable first-successful checkpoints and raw cumulative snapshots. Custom dimensions are stored as JSON so adding a project-specific field does not require a shared schema migration.

### Platform analytics adapters

- YouTube combines the Data API for fast public counters with YouTube Analytics for engaged views, watch time and retention.
- TikTok queries the final numeric video id; draft upload ids are intentionally not guessed/matched.
- Instagram normalizes Reel insights when those metrics are exposed by the account/API version.

Missing metrics remain `NULL`/`None`; the shared layer never invents cross-platform equivalence.

## Security model

- no shell interpolation for FFmpeg/ffprobe;
- HTTPS upload URL allow-lists;
- auth/query token redaction;
- optional `NAME_FILE` secret loading with private-file checks;
- atomic sensitive writes with POSIX `0600` permissions;
- private queue directories;
- ownership-aware file locks;
- immutable SHA-256-identified artifacts;
- fail-closed recovery after ambiguous remote publication outcomes.

## Development

```bash
python -m pytest
python -m ruff check .
python -m bandit -q -r src
python -m pip_audit
python -m pip check
```

CI runs the same checks on every pull request.
