ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_on_channel_watch BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE channel_watch_suggestions ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
