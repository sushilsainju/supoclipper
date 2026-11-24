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
