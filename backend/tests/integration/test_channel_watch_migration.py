"""Channel watching schema: the two tables, their columns, and the seeded feature flag."""

from pathlib import Path

import pytest
from sqlalchemy import text

MIGRATION = Path(__file__).parents[2] / "src/migrations/sql/20261123_0001_channel_watches.sql"

EXPECTED_WATCH_COLUMNS = {
    "id", "user_id", "platform", "channel_input", "resolved_channel_id", "resolved_channel_title",
    "enabled", "state", "active_live_video_id", "active_live_started_at", "ended_at",
    "last_checked_at", "last_check_method", "created_at", "updated_at",
}
EXPECTED_SUGGESTION_COLUMNS = {
    "id", "channel_watch_id", "user_id", "video_id", "video_url", "duration_seconds",
    "status", "task_id", "created_at", "notified_at",
}


async def table_columns(db_session, table: str) -> set[str]:
    rows = await db_session.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
        {"t": table},
    )
    return {row[0] for row in rows}


@pytest.mark.asyncio
async def test_channel_watch_tables_and_flag_exist(db_session):
    # Apply the migration file directly, the same way test_feature_flags.py does.
    await db_session.execute(text("DELETE FROM feature_flags WHERE key = 'channel_watching'"))
    for statement in MIGRATION.read_text().split(";"):
        if statement.strip():
            await db_session.execute(text(statement))

    assert await table_columns(db_session, "channel_watches") == EXPECTED_WATCH_COLUMNS
    assert await table_columns(db_session, "channel_watch_suggestions") == EXPECTED_SUGGESTION_COLUMNS

    row = (
        await db_session.execute(
            text("SELECT web_enabled, ios_enabled, admins_bypass FROM feature_flags WHERE key = 'channel_watching'")
        )
    ).one()
    assert (row.web_enabled, row.ios_enabled, row.admins_bypass) == (False, False, True)
    await db_session.rollback()
