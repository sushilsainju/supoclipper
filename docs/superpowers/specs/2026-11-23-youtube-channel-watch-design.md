# YouTube channel watching (wait-then-clip)

## Intent and why

Today, turning a livestream into clips requires a user to notice the stream
ended, find the resulting VOD URL themselves, and paste it into SupoClip.
This adds a self-serve way to skip that: a user registers a channel to
"watch," SupoClip notices when it goes live and then ends, and the user is
offered a one-click way to turn the finished VOD into a normal clipping
task — without ever touching a separate tool (the original trigger for this
was comparing SupoClip to `py-autovod`, which does this against an external
pipeline).

YouTube Live is the first platform because it is nearly free to support:
a YouTube live broadcast keeps the same video URL once it ends, and the
existing task-creation preflight (`VIDEO_DURATION_UNAVAILABLE` in
`api/routes/tasks.py`) already waits for a real duration to appear before
accepting a video. Twitch and Kick are an intended follow-up, using the
same watch-and-wait model, but need materially different detection logic
(the stream and the VOD are different URLs there) and are out of scope for
this doc.

## Goals

- A user can register a YouTube channel (by URL or @handle) to watch.
- A background process detects when that channel's live broadcast ends.
- Once the VOD's duration is confirmed available, the user is notified
  with a one-click "create clips" action, prefilled with the VOD and its
  cost in minutes — identical in spirit to pasting that URL in manually.
- Detection should work with zero extra setup (yt-dlp), and automatically
  use the official YouTube Data API when a key is already configured,
  mirroring the existing `youtube_metadata_provider` primary/fallback
  pattern in `youtube_utils.py`.
- This is intended to eventually ship as a hosted SaaS feature, not stay
  fork-only. Following the existing `workflows`/`tiktok_publishing`
  pattern, it ships behind a `feature_flags` row (e.g. `channel_watching`),
  default off, with `admins_bypass` set — so the cron job's query and the
  API routes both check the flag per user/platform rather than running
  unconditionally, and it can be turned on gradually rather than
  activating repo-wide the moment it merges.
- No plan/minute-cost gating *within* the feature itself in this
  iteration (e.g. no "Creator-only" restriction on who can add a watch) —
  the feature flag is the on/off switch; whether watching costs extra on
  top of a plan's existing minute allowance is a separate pricing decision
  for later, not part of this design.

## Non-goals / anti-goals

- **Not real-time clipping.** Clips are only generated after the stream
  ends and becomes a normal VOD. Live-while-streaming clipping is a much
  larger change (billing assumes a known duration up front, render-slot
  admission expires after 6h, the render autoscaler treats a job idle
  after 3h) and is explicitly deferred.
- **Not auto-processing.** A detected, ended stream never creates a billed
  task by itself. The user must confirm, exactly as they do today when
  pasting a URL manually. This is the one property that must never
  regress: no code path may call minute-metered task creation without an
  explicit user action.
- **No chat-spike / emote-based highlight boosting** (a `py-autovod`
  feature). Out of scope — this feature only automates *finding* the VOD,
  not improving clip selection.
- **No Twitch/Kick implementation** in this doc. The data model below is
  platform-aware so they can reuse it, but their detection mechanism
  (stream URL ≠ VOD URL, no official API already wired into this repo) is
  a separate follow-up design.

## Constraints

- Must not change the one-shot pipeline in `video_service.py`, the
  minute-billing model (`record_minute_usage` at task creation), render
  admission (`video_admission.py`), or host-job timeouts
  (`host_jobs.py`) — the eventual task this feature creates must be
  indistinguishable, to those systems, from one a user created by hand.
- Must fit the existing recurring-job pattern in `cron_worker.py` (a
  dispatcher job on a tick), not a new long-running daemon — there is no
  existing long-running service in `docker-compose.yml` and this
  shouldn't be the first one.
- YouTube Data API quota must be bounded. `search.list` (needed to find
  *which* video is live) costs 100 units; the default daily quota is
  10,000 units/project. Polling every watched channel this way on a tight
  interval does not scale.

## Approaches considered

**Detection mechanism** (the one real fork):

1. **Official Data API only.** Reliable, but `search.list`'s 100-unit cost
   caps frequent polling across many channels, and it is unavailable to
   self-hosters without a configured key.
2. **yt-dlp/scrape-based only** (what `py-autovod` does): probe the
   channel's `/live` URL. Free, no extra credential, but inherits the same
   scraping fragility this repo already tolerates for downloads (YouTube
   can break this without notice), and is a full subprocess per check.
3. **Hybrid (chosen), but only for the cheap half.** There is no cheap
   official way to *discover* a new live broadcast — `search.list` is the
   Data API's live-discovery call and costs 100 units, so even one watched
   channel polled that way every 5 minutes (288 checks/day) would burn
   ~28,800 units/day against a 10,000/day default quota. So: the **idle
   sweep** (discovering whether a channel just went live) always uses
   yt-dlp, regardless of whether a key is configured — there's no quota
   concern to trade off there. The Data API is used only for **live
   tracking** (polling a video already known to be live for its end time),
   where `videos.list` costs 1 unit and the "which video" question is
   already answered. This mirrors `youtube_metadata_provider`'s
   provider-selection shape for that one cheap call, not the whole
   detection pipeline.

**Confirmation model:**

1. **Auto-process.** Rejected — a user's monthly minute quota is small
   enough (600–1,500 min) that one long, unplanned stream could consume
   most or all of it with no review step, which is a different trust
   contract than every other task-creation path in this app.
2. **Notify-then-confirm (chosen).** Detection produces a *suggestion*,
   not a task. The user sees it (in-app, optionally email) and confirms
   through the same task-creation flow and cost preview that exists today.

## Design

### Data model

New table `channel_watches` (one row per user-registered channel):

| column | notes |
|---|---|
| `id` | uuid pk |
| `user_id` | fk `users.id` |
| `platform` | `'youtube'` for now; enum leaves room for `twitch`/`kick` |
| `channel_input` | what the user typed (URL or @handle) |
| `resolved_channel_id` | canonical YouTube channel ID, resolved once at creation |
| `enabled` | bool; disabling stops polling without deleting history |
| `state` | `idle` \| `live` \| `ended_pending_vod` |
| `active_live_video_id` | set while `state = live` or `ended_pending_vod` |
| `active_live_started_at` | set while `state = live` or `ended_pending_vod` |
| `ended_at` | set on transition into `ended_pending_vod`; clock for the resolution timeout |
| `last_checked_at` | last poll attempt, any outcome |
| `last_check_method` | `data_api` \| `yt_dlp`, for observability |
| `created_at` / `updated_at` | |

New table `channel_watch_suggestions` (one row per detected, ended stream —
plural so a channel going live twice in a day doesn't collide):

| column | notes |
|---|---|
| `id` | uuid pk |
| `channel_watch_id` | fk |
| `user_id` | fk, denormalized for direct querying |
| `video_url` / `video_id` | the resolved VOD |
| `duration_seconds` | once confirmed available |
| `status` | `pending` \| `dismissed` \| `created` \| `failed` |
| `task_id` | fk, set once the user confirms and a task exists |
| `created_at` | |

### Polling (new cron job in `cron_worker.py`)

Two-phase, to bound both subprocess load and Data API quota:

1. **Idle sweep** (e.g. every 5 minutes per channel): for each `enabled`
   watch in `state = idle`, run the hybrid liveness check. If live, record
   `active_live_video_id`/`active_live_started_at`, set `state = live`.
2. **Live tracking** (e.g. every 1–2 minutes per channel): for each watch
   in `state = live`, poll only the known video id (`videos.list`
   `liveStreamingDetails.actualEndTime`, 1 unit when a key is configured;
   an equivalent yt-dlp metadata fetch otherwise) to detect stream end.
   On end, set `ended_at = now`, move to `state = ended_pending_vod`, and
   continue tracking the same video id.
3. **VOD resolution** (same cadence as live tracking, since it's the same
   cheap per-video call): for each watch in `state = ended_pending_vod`,
   check whether a finite duration is now available — this is the exact
   gap `VIDEO_DURATION_UNAVAILABLE` already exists for, since YouTube can
   take a short while to finish processing a VOD after the broadcast ends.
   Once available, create a `channel_watch_suggestion`, notify the user,
   clear `active_live_video_id`/`active_live_started_at`/`ended_at`, and
   return the watch to `idle`. If duration hasn't resolved within a bounded
   timeout from `ended_at` (deleted, made private, members-only), do the
   same cleanup but surface an error on the watch instead of a suggestion,
   rather than polling forever.

The expensive "who's live" check (phase 1) only ever runs on the slower,
per-idle-channel interval, always via yt-dlp; a channel already known to be
live or just-ended is tracked with the cheap per-video call (phases 2–3),
optionally via the Data API — which is what keeps this affordable at any
watch count, since discovery is the only part with no cheap official path.

### API & frontend

- `POST /channel-watches`, `GET /channel-watches`, `DELETE
  /channel-watches/{id}` — manage watched channels. Channel resolution
  (handle/URL → canonical channel ID) happens once, synchronously, at
  creation time, so a typo surfaces immediately instead of silently never
  matching.
- `GET /channel-watches/suggestions`, `POST .../{id}/create-task`, `POST
  .../{id}/dismiss` — `create-task` calls the *existing* task-creation
  service path with the resolved VOD URL, so billing, admission, and the
  pipeline are untouched; it's the same call a manual paste makes.
- Frontend: a small settings surface (e.g. `/settings/channel-watch`) to
  add/remove channels, and a notification surface for pending suggestions
  — reusing the existing clip-publish-email toggle pattern for an optional
  "stream ready" email, plus an in-app badge/list entry.

### Known simplification: no cross-user dedup

Polling iterates `channel_watches` rows directly, one independent poll
schedule per row. If two users on the same instance watch the same
channel, it's checked twice on independent schedules rather than shared.
Accepted for this iteration — a single-operator or small self-hosted
deployment is unlikely to hit this, and a channel-level shared-state layer
is speculative generality until it's an actual quota/load problem. If it
becomes one, the fix is a `resolved_channel_id`-keyed liveness cache that
`channel_watches` rows read from, rather than each polling independently.

### What's left to the builder

- Exact migration syntax and where it lives relative to the existing
  `init.sql`/migrations convention.
- Exact yt-dlp invocation for the `/live` liveness probe (flags, timeout).
- Default interval constants (proposed above: 5 min idle sweep, 1–2 min
  live tracking) — tune based on real quota/behavior.
- Notification implementation details (email template, in-app component
  placement).
- Whether `channel_watches` needs a per-user cap (e.g. max N watched
  channels) to bound total polling load — not addressed here since it's a
  minor operational call, not a design fork.
