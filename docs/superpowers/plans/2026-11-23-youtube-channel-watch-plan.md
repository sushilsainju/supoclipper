# YouTube Channel Watch (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user register a YouTube channel to watch; a cron poller detects when a live broadcast ends and its VOD is ready, and surfaces a confirmable suggestion the user can turn into a normal clipping task — without ever auto-creating a billed task.

**Architecture:** Two new tables (`channel_watches`, `channel_watch_suggestions`) behind a `channel_watching` feature flag; a single per-minute cron job runs a 3-phase state machine (discover live → track for end → wait for VOD duration) using a new YouTube detection module; a small REST surface lets the frontend manage watches and confirm/dismiss suggestions by reusing the *existing*, unmodified `POST /tasks/` endpoint rather than duplicating its billing/admission logic.

**Tech Stack:** FastAPI, SQLAlchemy `text()` raw SQL (existing repository convention), arq cron, yt-dlp, YouTube Data API v3, pytest with hand-rolled fake sessions (existing convention — see `tests/unit/test_billing_service.py`).

**Spec:** `docs/superpowers/specs/2026-11-23-youtube-channel-watch-design.md`

**Scope note:** This plan covers the backend only. The design doc also describes a frontend settings/notification surface; that is deliberately a separate follow-on plan once these endpoints exist to build against — each is independently testable and shippable (this plan via its own tests + `curl`/httpie against the running API).

**Key "how" decision not explicit in the spec:** the spec says suggestion-confirmation "calls the *existing* task-creation service path." Reading the actual route (`api/routes/tasks.py:639-896`) shows that logic is ~250 lines deeply tangled with HTTP request parsing, idempotency-key derivation from raw headers, and workflow-spec parsing — not a clean extractable function today. Extracting it risks regressing a heavily-tested, billing-critical path for a borderline benefit. Instead: the confirm flow is client-driven — the frontend reads a suggestion's `video_url` from `GET /channel-watches/suggestions`, calls the **existing, completely unmodified** `POST /tasks/` with it (literally the same call a manual paste makes — the spec's own stated goal, achieved more directly), then calls a small new `POST .../link-task` to close the loop. This touches zero lines of the existing task-creation route.

## Global Constraints

- YouTube only this iteration. `platform` is a column for future Twitch/Kick reuse, but only `'youtube'` is implemented; any other value is rejected at the API layer.
- The idle-sweep "who's live" discovery check always uses yt-dlp, never the Data API, regardless of whether a key is configured — there is no cheap official discovery call.
- The live-tracking and VOD-resolution checks prefer the Data API (`config.resolve_youtube_data_api_key()`) when configured, falling back to yt-dlp on any failure — mirrors the existing `youtube_metadata_provider` fallback shape in `youtube_utils.py`.
- No task or billing charge is ever created without an explicit user action. Detection only ever produces a `channel_watch_suggestions` row.
- Feature-gated behind a `channel_watching` row in `feature_flags`, default off on web/ios, `admins_bypass = true`.
- No plan/minute-cost gating inside the feature itself — the flag is the only on/off switch.
- One independent poll schedule per `channel_watches` row — no cross-user dedup for a channel watched by multiple users in this iteration (accepted simplification, documented in the spec).
- Polling cadence (builder's call, within the spec's suggested bands): idle-sweep discovery every 300s per channel; live-tracking and VOD-resolution checks every tick (the cron job itself runs once per minute).
- VOD-resolution bounded timeout: 2 hours from `ended_at`. Past that, the suggestion is marked `failed` and the watch returns to `idle` rather than polling forever (exact value is a builder call per the spec).

## Review Focus

- A typo'd, private, or deleted channel input → `POST /channel-watches` must fail synchronously with a clear 4xx, not silently create an unpollable row. (Task 6)
- A VOD that never resolves a duration (made private/members-only/deleted after the stream ends) → after the bounded timeout, the watch returns to `idle` with a `failed` suggestion, not infinite polling. (Task 4)
- One watch's external call failing (yt-dlp subprocess crash, Data API 4xx/5xx) must never abort the whole cron tick — every other channel's poll must still run that tick. (Task 4)
- A Data API failure (quota exhausted, bad key, network error) during live-tracking or VOD-resolution must fall back to yt-dlp rather than raising, exactly like `youtube_metadata_provider`'s existing fallback. (Task 3)
- A user whose `channel_watching` flag is off (and who isn't an admin) must be fully excluded from the cron tick for their own rows, not just hidden in the API/UI — otherwise turning the flag off after a watch already exists doesn't actually stop it running. (Task 4 and Task 6)

---

## File Structure

**Create:**
- `src/migrations/sql/20261123_0001_channel_watches.sql` — tables + feature flag seed
- `src/migrations/sql/20261123_0002_channel_watch_notifications.sql` — notification toggle + idempotency columns
- `src/repositories/channel_watch_repository.py` — `ChannelWatchRepository`, `ChannelWatchSuggestionRepository`
- `src/channel_watch_detection.py` — `resolve_channel`, `probe_channel_live`, `has_broadcast_ended`
- `src/services/channel_watch_email_service.py` — `ChannelWatchEmailService`
- `src/api/routes/channel_watches.py` — the REST surface
- `tests/unit/test_channel_watch_repository.py`
- `tests/unit/test_channel_watch_detection.py`
- `tests/unit/test_channel_watch_cron.py`
- `tests/unit/test_channel_watch_email_service.py`
- `tests/unit/test_channel_watches_routes.py`
- `tests/integration/test_channel_watch_migration.py`

**Modify:**
- `src/workers/tasks.py` — add `poll_channel_watches`
- `src/workers/cron_worker.py` — register it
- `src/main.py` — register the new router
- `src/feature_flags.py` — add `"channel_watching": False` to `DEFAULT_FLAGS`

---

### Task 1: Schema migration

**Files:**
- Create: `src/migrations/sql/20261123_0001_channel_watches.sql`
- Test: `tests/integration/test_channel_watch_migration.py`

**Interfaces:**
- Produces: tables `channel_watches(id, user_id, platform, channel_input, resolved_channel_id, resolved_channel_title, enabled, state, active_live_video_id, active_live_started_at, ended_at, last_checked_at, last_check_method, created_at, updated_at)` and `channel_watch_suggestions(id, channel_watch_id, user_id, video_id, video_url, duration_seconds, status, task_id, created_at)`; a `feature_flags` row keyed `'channel_watching'`.

- [ ] **Step 1: Write the migration file**

```sql
CREATE TABLE IF NOT EXISTS channel_watches (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform VARCHAR(20) NOT NULL DEFAULT 'youtube',
    channel_input TEXT NOT NULL,
    resolved_channel_id VARCHAR(64) NOT NULL,
    resolved_channel_title TEXT,
    enabled BOOLEAN NOT NULL DEFAULT true,
    state VARCHAR(20) NOT NULL DEFAULT 'idle',
    active_live_video_id VARCHAR(32),
    active_live_started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ,
    last_check_method VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_channel_watches_user ON channel_watches(user_id);
CREATE INDEX IF NOT EXISTS idx_channel_watches_poll ON channel_watches(enabled, state);

CREATE TABLE IF NOT EXISTS channel_watch_suggestions (
    id VARCHAR(36) PRIMARY KEY,
    channel_watch_id VARCHAR(36) NOT NULL REFERENCES channel_watches(id) ON DELETE CASCADE,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    video_id VARCHAR(32) NOT NULL,
    video_url TEXT NOT NULL,
    duration_seconds INTEGER,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    task_id VARCHAR(36),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_channel_watch_suggestions_user ON channel_watch_suggestions(user_id, status);

INSERT INTO feature_flags (key, description, web_enabled, ios_enabled, admins_bypass)
VALUES ('channel_watching', 'Watch a YouTube channel and auto-suggest clipping its VOD once a livestream ends', false, false, true)
ON CONFLICT (key) DO NOTHING;
```

Check `src/migrations/sql/` for any other file already dated `20261123_*` before naming this one, to avoid a filename collision (sort order only matters relative to files it doesn't depend on, but a collision would silently skip one file).

Also mirror this migration's `CREATE TABLE`/`CREATE INDEX`/`INSERT` statements into the root `init.sql`, in the section alongside the other feature-flag seed rows and table definitions — this repo's convention (per `CLAUDE.md`'s "Adding a flag" note) is to keep `init.sql` in sync with each migration so a from-scratch install's bootstrap schema matches what an existing instance ends up with after migrations run. Functionally the migration file alone is sufficient (a fresh install's data volume is empty, so every migration still applies), but `init.sql` should read as the accurate current schema.

- [ ] **Step 2: Write the migration test**

```python
async def test_channel_watch_tables_and_flag_exist(db_session):
    # Apply the migration file directly, mirroring tests/integration/test_feature_flags.py's pattern
    # Assert: channel_watches and channel_watch_suggestions tables exist with expected columns
    # Assert: a feature_flags row with key='channel_watching', web_enabled=False, admins_bypass=True exists
```

- [ ] **Step 3: Run migration against the test database and verify the test passes**

Run: `pytest --no-cov tests/integration/test_channel_watch_migration.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/migrations/sql/20261123_0001_channel_watches.sql tests/integration/test_channel_watch_migration.py
git commit -m "feat: add channel_watches schema and feature flag"
```

---

### Task 2: Repositories

**Files:**
- Create: `src/repositories/channel_watch_repository.py`
- Test: `tests/unit/test_channel_watch_repository.py`

**Interfaces:**
- Consumes: Task 1's tables.
- Produces:
  - `ChannelWatchRepository.create(db, *, user_id: str, platform: str, channel_input: str, resolved_channel_id: str, resolved_channel_title: Optional[str]) -> dict`
  - `ChannelWatchRepository.get_by_id(db, watch_id: str) -> Optional[dict]`
  - `ChannelWatchRepository.list_for_user(db, user_id: str) -> list[dict]`
  - `ChannelWatchRepository.list_enabled_idle(db, *, platform: str, stale_before: datetime) -> list[dict]`
  - `ChannelWatchRepository.list_enabled_tracking(db, *, platform: str) -> list[dict]`
  - `ChannelWatchRepository.mark_live(db, watch_id: str, *, video_id: str, started_at: datetime) -> None`
  - `ChannelWatchRepository.mark_ended(db, watch_id: str, *, ended_at: datetime) -> None`
  - `ChannelWatchRepository.reset_to_idle(db, watch_id: str) -> None`
  - `ChannelWatchRepository.disable(db, watch_id: str, user_id: str) -> bool`
  - `ChannelWatchRepository.touch_last_checked(db, watch_id: str, *, method: str) -> None`
  - `ChannelWatchSuggestionRepository.create(db, *, channel_watch_id: str, user_id: str, video_id: str, video_url: str, status: str = "pending", duration_seconds: Optional[int] = None) -> dict`
  - `ChannelWatchSuggestionRepository.list_pending_for_user(db, user_id: str) -> list[dict]`
  - `ChannelWatchSuggestionRepository.get_by_id(db, suggestion_id: str) -> Optional[dict]`
  - `ChannelWatchSuggestionRepository.mark_created(db, suggestion_id: str, *, task_id: str) -> None`
  - `ChannelWatchSuggestionRepository.mark_dismissed(db, suggestion_id: str) -> None`

Follow the existing static-method, raw-`text()`-SQL convention in `src/repositories/social_repository.py` and `src/repositories/source_repository.py` (plain classes, no `__init__`, every method takes `db: AsyncSession` as its first argument, dict-shaped returns, the method itself calls `await db.commit()`).

- [ ] **Step 1: Write failing tests for `ChannelWatchRepository.create` and `get_by_id`**

```python
# Follow the _FakeSession/_FakeResult pattern in tests/unit/test_billing_service.py —
# no real DB in unit tests, a hand-rolled session whose execute()/fetchone() return queued rows.

async def test_create_returns_new_watch_row():
    ...

async def test_get_by_id_returns_none_when_missing():
    ...
```

- [ ] **Step 2: Run tests, verify they fail** (module doesn't exist yet)

Run: `pytest --no-cov tests/unit/test_channel_watch_repository.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `ChannelWatchRepository.create` and `get_by_id`**

- [ ] **Step 4: Run tests, verify they pass**

- [ ] **Step 5: Write failing tests for the remaining `ChannelWatchRepository` methods** (`list_for_user`, `list_enabled_idle`, `list_enabled_tracking`, `mark_live`, `mark_ended`, `reset_to_idle`, `disable`, `touch_last_checked`), one assertion per state transition — `list_enabled_idle` in particular needs a test proving it excludes a row whose `last_checked_at` is newer than `stale_before`.

- [ ] **Step 6: Run tests, verify they fail, then implement, then verify they pass**

- [ ] **Step 7: Write failing tests for all `ChannelWatchSuggestionRepository` methods**, including that `create(..., status="failed")` stores a suggestion without requiring `duration_seconds`.

- [ ] **Step 8: Run tests, verify they fail, then implement, then verify they pass**

- [ ] **Step 9: Commit**

```bash
git add src/repositories/channel_watch_repository.py tests/unit/test_channel_watch_repository.py
git commit -m "feat: add channel watch repositories"
```

---

### Task 3: Detection module

**Files:**
- Create: `src/channel_watch_detection.py`
- Test: `tests/unit/test_channel_watch_detection.py`

**Interfaces:**
- Consumes: `config.resolve_youtube_data_api_key()` and the yt-dlp/Data-API primitives already used by `youtube_utils.py` (call the same underlying HTTP/subprocess mechanisms that module uses — do not import its private `_fetch_video_info_with_*` helpers; write new, narrowly-scoped calls here, since this module needs different fields — `is_live`, `liveStreamingDetails.actualEndTime`, channel resolution — than that module's duration-only metadata fetch exposes).
- Produces:
  - `@dataclass(frozen=True) class ResolvedChannel: channel_id: str; title: Optional[str]`
  - `async def resolve_channel(channel_input: str) -> Optional[ResolvedChannel]`
  - `async def probe_channel_live(channel_id: str) -> Optional[str]` (returns the live video id, or `None`)
  - `async def has_broadcast_ended(video_id: str) -> bool`

- [ ] **Step 1: Write failing tests for `resolve_channel`**

```python
# monkeypatch the yt-dlp subprocess call and the Data API HTTP call (follow the
# monkeypatch.setattr style in tests/unit/test_youtube_cache_quality.py)

async def test_resolve_channel_returns_resolved_channel_via_data_api_when_key_configured():
    ...

async def test_resolve_channel_falls_back_to_yt_dlp_on_data_api_failure():
    ...

async def test_resolve_channel_returns_none_for_unresolvable_input():
    ...
```

- [ ] **Step 2: Run, verify fail; implement `resolve_channel`; run, verify pass**

Try the Data API's `channels.list(forHandle=...)` (or `forUsername=`) first when `config.resolve_youtube_data_api_key()` is set; on any failure (including no key), fall back to a yt-dlp metadata extraction of the channel input treated as a URL. Return `None` if neither resolves a channel id — this is the Review Focus case for a typo'd channel.

- [ ] **Step 3: Write failing tests for `probe_channel_live`**

```python
async def test_probe_channel_live_returns_video_id_when_live():
    ...

async def test_probe_channel_live_returns_none_when_offline():
    ...

async def test_probe_channel_live_never_calls_the_data_api():
    # Even with a Data API key configured, this check must only ever shell
    # out to yt-dlp — assert the Data API HTTP call is never made.
    ...
```

- [ ] **Step 4: Run, verify fail; implement `probe_channel_live`; run, verify pass**

Probe `https://www.youtube.com/channel/<id>/live` via yt-dlp; a successful extraction with `is_live: true` returns that video's id, anything else returns `None`.

- [ ] **Step 5: Write failing tests for `has_broadcast_ended`**

```python
async def test_has_broadcast_ended_true_via_data_api_actual_end_time():
    ...

async def test_has_broadcast_ended_false_while_still_live():
    ...

async def test_has_broadcast_ended_falls_back_to_yt_dlp_on_data_api_error():
    # Review Focus case: Data API 403/429/network error must not raise —
    # it must fall back to the yt-dlp check and still return a bool.
    ...
```

- [ ] **Step 6: Run, verify fail; implement `has_broadcast_ended`; run, verify pass**

Prefer `videos.list` with `part=liveStreamingDetails` and check `actualEndTime` is present when a key is configured; fall back to a yt-dlp metadata check (`is_live` false / playability no longer live) on any Data API failure.

- [ ] **Step 7: Commit**

```bash
git add src/channel_watch_detection.py tests/unit/test_channel_watch_detection.py
git commit -m "feat: add YouTube channel-watch detection module"
```

---

### Task 4: Cron poller

**Files:**
- Modify: `src/workers/tasks.py` (add `poll_channel_watches`)
- Modify: `src/workers/cron_worker.py` (register it)
- Test: `tests/unit/test_channel_watch_cron.py`

**Interfaces:**
- Consumes: `ChannelWatchRepository`/`ChannelWatchSuggestionRepository` (Task 2), `resolve_channel`/`probe_channel_live`/`has_broadcast_ended` (Task 3), the existing `async_get_youtube_video_info(url: str) -> Optional[dict]` from `src/youtube_utils.py` (reused as-is for the VOD-duration check — it already returns a `duration` key via the same hybrid provider fallback; do not reimplement this), `feature_flags.is_enabled(db, key, *, platform, user_id)`.
- Produces: `async def poll_channel_watches(ctx: Dict[str, Any]) -> Dict[str, Any]`, registered as `scheduled(poll_channel_watches, second=0)` (once per minute, matching `dispatch_scheduled_social_posts`'s registration shape in `cron_worker.py`) in `CronWorkerSettings.cron_jobs`.

Module-level constants in `src/workers/tasks.py` near this function: `CHANNEL_WATCH_IDLE_SWEEP_INTERVAL_SECONDS = 300`, `CHANNEL_WATCH_VOD_TIMEOUT_SECONDS = 7200`.

- [ ] **Step 1: Write a failing test for the idle-sweep phase finding a new live broadcast**

```python
async def test_poll_marks_watch_live_when_channel_goes_live(monkeypatch):
    # One enabled, idle watch; probe_channel_live returns a video id;
    # assert ChannelWatchRepository.mark_live was called with that id.
```

- [ ] **Step 2: Run, verify fail; implement the idle-sweep phase; run, verify pass**

For each row from `list_enabled_idle(platform="youtube", stale_before=now - 300s)`: skip the row if `feature_flags.is_enabled(db, "channel_watching", platform="web", user_id=row["user_id"])` is `False` (Review Focus: a disabled flag must stop polling, not just hide the UI); otherwise call `probe_channel_live`, and on a video id, call `mark_live`. Always call `touch_last_checked` regardless of outcome. Wrap each row's work in its own `try/except` that logs and continues — one row's exception must never stop the loop (Review Focus).

- [ ] **Step 3: Write a failing test for the live-tracking phase detecting the stream ended**

```python
async def test_poll_marks_watch_ended_when_broadcast_ends(monkeypatch):
    # One watch in state=live; has_broadcast_ended returns True;
    # assert ChannelWatchRepository.mark_ended was called.
```

- [ ] **Step 4: Run, verify fail; implement the live-tracking phase; run, verify pass**

For each row from `list_enabled_tracking(platform="youtube")` with `state == "live"`: call `has_broadcast_ended(row["active_live_video_id"])`; on `True`, call `mark_ended`.

- [ ] **Step 5: Write a failing test for the VOD-resolution phase creating a suggestion**

```python
async def test_poll_creates_suggestion_when_vod_duration_resolves(monkeypatch):
    # One watch in state=ended_pending_vod; async_get_youtube_video_info
    # returns a dict with a duration; assert a suggestion was created via
    # ChannelWatchSuggestionRepository.create(..., status="pending",
    # duration_seconds=<that duration>) and the watch was reset to idle.
```

- [ ] **Step 6: Run, verify fail; implement the VOD-resolution phase; run, verify pass**

For each row from `list_enabled_tracking` with `state == "ended_pending_vod"`: call `async_get_youtube_video_info(f"https://www.youtube.com/watch?v={video_id}")`. If it returns a usable `duration`, create a suggestion and `reset_to_idle`. If not, and `now - row["ended_at"] > CHANNEL_WATCH_VOD_TIMEOUT_SECONDS`, create a `status="failed"` suggestion (no `duration_seconds`) and `reset_to_idle` (Review Focus: bounded timeout, not infinite polling). Otherwise leave the row as-is for the next tick.

- [ ] **Step 7: Write a failing test for the per-row isolation guarantee**

```python
async def test_poll_continues_past_one_rows_exception(monkeypatch):
    # Two idle watches; probe_channel_live raises for the first, returns a
    # video id for the second; assert the second watch still ends up marked
    # live despite the first raising.
```

- [ ] **Step 8: Run, verify fail; wrap each phase's per-row body in try/except; verify pass**

- [ ] **Step 9: Register the job**

```python
scheduled(poll_channel_watches, second=0)
```

appended to `CronWorkerSettings.cron_jobs` in `src/workers/cron_worker.py`.

- [ ] **Step 10: Commit**

```bash
git add src/workers/tasks.py src/workers/cron_worker.py tests/unit/test_channel_watch_cron.py
git commit -m "feat: add channel-watch cron poller"
```

---

### Task 5: Notification

**Files:**
- Create: `src/services/channel_watch_email_service.py`
- Create: `src/migrations/sql/20261123_0002_channel_watch_notifications.sql`
- Modify: `src/workers/tasks.py` (call the email service from `poll_channel_watches`'s VOD-resolution phase)
- Test: `tests/unit/test_channel_watch_email_service.py`

**Interfaces:**
- Consumes: `SesEmailService`/`EmailContent` from `src/services/email_service.py`, `email_button()`/layout helpers from `src/services/email_layout.py` (mirror `SocialPublishEmailService`'s exact usage).
- Produces: `ChannelWatchEmailService.send_stream_ready_email(*, recipient: str, channel_title: Optional[str], video_url: str, duration_seconds: int) -> dict`. Migration adds `users.notify_on_channel_watch BOOLEAN NOT NULL DEFAULT true` and `channel_watch_suggestions.notified_at TIMESTAMPTZ`. Also produces `ChannelWatchSuggestionRepository.mark_notified(db, suggestion_id: str) -> None` (extends Task 2's class).

- [ ] **Step 1: Write the migration**

```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_on_channel_watch BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE channel_watch_suggestions ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
```

Also mirror both `ALTER TABLE` statements into the root `init.sql`'s column list for `users` and `channel_watch_suggestions` (same convention as Task 1 — functionally this migration file alone is sufficient, but `init.sql` should stay an accurate schema reference).

Add one more method to `ChannelWatchSuggestionRepository` (from Task 2) to persist this column: `ChannelWatchSuggestionRepository.mark_notified(db, suggestion_id: str) -> None` — sets `notified_at = now()`. Task 2 didn't know about this column yet when it ran; this task owns adding it.

- [ ] **Step 2: Write a failing test for `send_stream_ready_email`**

```python
async def test_send_stream_ready_email_builds_expected_content(monkeypatch):
    # Stub SesEmailService.send_email; assert it was called with an
    # EmailContent whose subject/html mention channel_title and video_url.
```

- [ ] **Step 3: Run, verify fail; implement `ChannelWatchEmailService`; run, verify pass**

Mirror `SocialPublishEmailService.send_clip_published_email`'s shape exactly: a private `_build_stream_ready_email` builder using `email_button()`/`first_name_for()`, then `SesEmailService.send_email`.

- [ ] **Step 4: Write a failing test for the toggle + idempotency check**

```python
async def test_poll_skips_email_when_notify_toggle_is_off(monkeypatch):
    ...

async def test_poll_does_not_resend_email_once_notified_at_is_set():
    ...
```

- [ ] **Step 5: Run, verify fail; wire the call into `poll_channel_watches`'s VOD-resolution phase, gated by the toggle and idempotency column, wrapped in try/except (a failed email must never break the poll, matching `_send_publish_notification_if_needed`'s pattern); run, verify pass**

- [ ] **Step 6: Commit**

```bash
git add src/services/channel_watch_email_service.py src/migrations/sql/20261123_0002_channel_watch_notifications.sql src/workers/tasks.py tests/unit/test_channel_watch_email_service.py
git commit -m "feat: notify users when a watched channel's VOD is ready"
```

---

### Task 6: API routes

**Files:**
- Create: `src/api/routes/channel_watches.py`
- Modify: `src/main.py` (register the router)
- Modify: `src/feature_flags.py` (`DEFAULT_FLAGS["channel_watching"] = False`)
- Test: `tests/unit/test_channel_watches_routes.py`

**Interfaces:**
- Consumes: `ChannelWatchRepository`/`ChannelWatchSuggestionRepository` (Task 2), `resolve_channel` (Task 3), `feature_flags.is_enabled`, `TaskRepository.get_task_by_id(db, task_id) -> Optional[dict]` (existing, for the `link-task` ownership check — compare the returned row's `user_id` to the authenticated caller).
- Produces: `router = APIRouter(prefix="/channel-watches", tags=["channel-watches"])` with:
  - `POST /channel-watches` — body `{"channel_input": str}`; 403 if the flag is off for this user; 400 if `resolve_channel` returns `None`; otherwise creates the watch.
  - `GET /channel-watches` — lists the caller's watches.
  - `DELETE /channel-watches/{watch_id}` — disables a watch the caller owns; 404 otherwise.
  - `GET /channel-watches/suggestions` — lists the caller's pending suggestions.
  - `POST /channel-watches/suggestions/{suggestion_id}/dismiss`
  - `POST /channel-watches/suggestions/{suggestion_id}/link-task` — body `{"task_id": str}`; 404 if the suggestion or task isn't the caller's; otherwise `mark_created`.

- [ ] **Step 1: Write a failing test for `POST /channel-watches` behind the feature flag**

```python
async def test_create_watch_returns_403_when_flag_disabled():
    ...

async def test_create_watch_returns_400_for_unresolvable_channel(monkeypatch):
    ...

async def test_create_watch_returns_201_and_stores_resolved_channel(monkeypatch):
    ...
```

- [ ] **Step 2: Run, verify fail; implement `POST /channel-watches`; run, verify pass**

- [ ] **Step 3: Write failing tests for `GET /channel-watches` and `DELETE /channel-watches/{id}`** (including a 404 when the watch belongs to a different user)

- [ ] **Step 4: Run, verify fail; implement both; run, verify pass**

- [ ] **Step 5: Write failing tests for `GET /channel-watches/suggestions` and `.../dismiss`**

- [ ] **Step 6: Run, verify fail; implement both; run, verify pass**

- [ ] **Step 7: Write failing tests for `.../link-task`**

```python
async def test_link_task_marks_suggestion_created_when_task_belongs_to_caller(monkeypatch):
    ...

async def test_link_task_returns_404_when_task_belongs_to_another_user(monkeypatch):
    ...
```

- [ ] **Step 8: Run, verify fail; implement `link-task`; run, verify pass**

- [ ] **Step 9: Register the router and the flag default**

```python
# src/main.py — alongside the other app.include_router(...) calls:
app.include_router(channel_watches_router)
```

```python
# src/feature_flags.py DEFAULT_FLAGS:
"channel_watching": False,
```

- [ ] **Step 10: Run the full backend test suite once to confirm no regressions**

Run: `pytest --no-cov` (from `backend/`)
Expected: all pass, including the pre-existing `test_billing_service.py`/`test_social_providers.py` suites — this task touched no existing route or service code, so nothing there should change.

- [ ] **Step 11: Commit**

```bash
git add src/api/routes/channel_watches.py src/main.py src/feature_flags.py tests/unit/test_channel_watches_routes.py
git commit -m "feat: add channel-watches API routes"
```

---

## Self-Review Notes

- **Spec coverage:** every design-doc requirement maps to a task — schema (Task 1), hybrid detection with the corrected quota split (Task 3), the 3-phase poller with the `ended_pending_vod` fix and bounded timeout (Task 4), notify-then-confirm (Task 5 + 6), feature-flag gating (Task 6), no plan gating (nothing added), no cross-user dedup (explicitly left as-is, per spec's accepted simplification).
- **Deliberate deviation from a literal reading of the spec:** the spec's "calls the existing task-creation service path" is implemented as the client reusing the existing HTTP endpoint directly rather than a server-side shared function, because reading the actual route showed that extraction was far riskier than the spec anticipated. This is flagged at the top of the plan and achieves the same stated goal (indistinguishable from a manual paste) more directly.
- **Review Focus:** all five items are each pinned to a specific task's tests, not left as unverified narrative.
