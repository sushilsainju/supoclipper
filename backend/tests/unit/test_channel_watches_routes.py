"""Route-level tests for the channel-watches API (Task 6).

Builds a FastAPI app with only this router mounted, overrides ``get_db``
with an inert stub, and monkeypatches everything the route module calls
into (auth resolution, the feature flag, channel resolution, and the two
repositories) so these tests exercise only the routing/validation/ownership
logic the router itself is responsible for.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from src.api.routes import channel_watches
from src.channel_watch_detection import ResolvedChannel
from src.database import get_db


@pytest.fixture
def db_stub():
    return SimpleNamespace(close=AsyncMock())


@pytest.fixture
async def client(db_stub, monkeypatch):
    monkeypatch.setattr(
        channel_watches, "resolve_authenticated_user_id", AsyncMock(return_value="user-1")
    )
    monkeypatch.setattr(channel_watches.feature_flags, "is_enabled", AsyncMock(return_value=True))

    app = FastAPI()
    app.include_router(channel_watches.router)
    app.dependency_overrides[get_db] = lambda: db_stub
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


# ----------------------------------------------------------------------
# POST /channel-watches
# ----------------------------------------------------------------------
async def test_create_watch_returns_403_when_flag_disabled(client, monkeypatch):
    monkeypatch.setattr(channel_watches.feature_flags, "is_enabled", AsyncMock(return_value=False))
    response = await client.post("/channel-watches/", json={"channel_input": "@example"})
    assert response.status_code == 403


async def test_create_watch_returns_400_for_unresolvable_channel(client, monkeypatch):
    monkeypatch.setattr(channel_watches, "resolve_channel", AsyncMock(return_value=None))
    response = await client.post("/channel-watches/", json={"channel_input": "@nope"})
    assert response.status_code == 400


async def test_create_watch_returns_201_and_stores_resolved_channel(client, monkeypatch):
    monkeypatch.setattr(
        channel_watches,
        "resolve_channel",
        AsyncMock(return_value=ResolvedChannel(channel_id="UC123", title="Example Channel")),
    )
    created = AsyncMock(
        return_value={
            "id": "watch-1",
            "user_id": "user-1",
            "platform": "youtube",
            "channel_input": "@example",
            "resolved_channel_id": "UC123",
            "resolved_channel_title": "Example Channel",
            "enabled": True,
            "state": "idle",
        }
    )
    monkeypatch.setattr(channel_watches.ChannelWatchRepository, "create", created)

    response = await client.post("/channel-watches/", json={"channel_input": "@example"})

    assert response.status_code == 201
    body = response.json()
    assert body["watch"]["resolved_channel_id"] == "UC123"
    assert body["watch"]["resolved_channel_title"] == "Example Channel"
    created.assert_awaited_once()
    assert created.await_args.kwargs == {
        "user_id": "user-1",
        "platform": "youtube",
        "channel_input": "@example",
        "resolved_channel_id": "UC123",
        "resolved_channel_title": "Example Channel",
    }


async def test_create_watch_requires_channel_input(client):
    response = await client.post("/channel-watches/", json={})
    assert response.status_code == 422


# ----------------------------------------------------------------------
# GET /channel-watches, DELETE /channel-watches/{id}
# ----------------------------------------------------------------------
async def test_list_watches_returns_403_when_flag_disabled(client, monkeypatch):
    monkeypatch.setattr(channel_watches.feature_flags, "is_enabled", AsyncMock(return_value=False))
    response = await client.get("/channel-watches/")
    assert response.status_code == 403


async def test_list_watches_returns_only_the_callers_watches(client, monkeypatch):
    watches = [{"id": "watch-1", "user_id": "user-1"}]
    list_for_user = AsyncMock(return_value=watches)
    monkeypatch.setattr(channel_watches.ChannelWatchRepository, "list_for_user", list_for_user)

    response = await client.get("/channel-watches/")

    assert response.status_code == 200
    assert response.json() == {"watches": watches, "total": 1}
    assert list_for_user.await_args.args[1] == "user-1"


async def test_delete_watch_disables_a_watch_the_caller_owns(client, monkeypatch):
    disable = AsyncMock(return_value=True)
    monkeypatch.setattr(channel_watches.ChannelWatchRepository, "disable", disable)

    response = await client.delete("/channel-watches/watch-1")

    assert response.status_code == 200
    assert disable.await_args.args[1:] == ("watch-1", "user-1")


async def test_delete_watch_returns_404_for_another_users_watch(client, monkeypatch):
    disable = AsyncMock(return_value=False)
    monkeypatch.setattr(channel_watches.ChannelWatchRepository, "disable", disable)

    response = await client.delete("/channel-watches/watch-1")

    assert response.status_code == 404


# ----------------------------------------------------------------------
# GET /channel-watches/suggestions, .../dismiss
# ----------------------------------------------------------------------
async def test_list_suggestions_returns_403_when_flag_disabled(client, monkeypatch):
    monkeypatch.setattr(channel_watches.feature_flags, "is_enabled", AsyncMock(return_value=False))
    response = await client.get("/channel-watches/suggestions")
    assert response.status_code == 403


async def test_list_suggestions_returns_only_the_callers_pending_suggestions(client, monkeypatch):
    suggestions = [{"id": "sugg-1", "user_id": "user-1", "status": "pending"}]
    list_pending = AsyncMock(return_value=suggestions)
    monkeypatch.setattr(
        channel_watches.ChannelWatchSuggestionRepository, "list_pending_for_user", list_pending
    )

    response = await client.get("/channel-watches/suggestions")

    assert response.status_code == 200
    assert response.json() == {"suggestions": suggestions, "total": 1}


async def test_dismiss_suggestion_marks_it_dismissed(client, monkeypatch):
    get_by_id = AsyncMock(return_value={"id": "sugg-1", "user_id": "user-1", "status": "pending"})
    mark_dismissed = AsyncMock(return_value=None)
    monkeypatch.setattr(channel_watches.ChannelWatchSuggestionRepository, "get_by_id", get_by_id)
    monkeypatch.setattr(
        channel_watches.ChannelWatchSuggestionRepository, "mark_dismissed", mark_dismissed
    )

    response = await client.post("/channel-watches/suggestions/sugg-1/dismiss")

    assert response.status_code == 200
    assert mark_dismissed.await_args.args[1] == "sugg-1"


async def test_dismiss_suggestion_returns_404_for_another_users_suggestion(client, monkeypatch):
    get_by_id = AsyncMock(return_value={"id": "sugg-1", "user_id": "someone-else", "status": "pending"})
    mark_dismissed = AsyncMock(return_value=None)
    monkeypatch.setattr(channel_watches.ChannelWatchSuggestionRepository, "get_by_id", get_by_id)
    monkeypatch.setattr(
        channel_watches.ChannelWatchSuggestionRepository, "mark_dismissed", mark_dismissed
    )

    response = await client.post("/channel-watches/suggestions/sugg-1/dismiss")

    assert response.status_code == 404
    mark_dismissed.assert_not_awaited()


async def test_dismiss_suggestion_returns_404_when_missing(client, monkeypatch):
    get_by_id = AsyncMock(return_value=None)
    monkeypatch.setattr(channel_watches.ChannelWatchSuggestionRepository, "get_by_id", get_by_id)

    response = await client.post("/channel-watches/suggestions/nope/dismiss")

    assert response.status_code == 404


# ----------------------------------------------------------------------
# POST /channel-watches/suggestions/{id}/link-task
# ----------------------------------------------------------------------
async def test_link_task_marks_suggestion_created_when_task_belongs_to_caller(client, monkeypatch):
    get_by_id = AsyncMock(return_value={"id": "sugg-1", "user_id": "user-1", "status": "pending"})
    get_task_by_id = AsyncMock(return_value={"id": "task-1", "user_id": "user-1"})
    mark_created = AsyncMock(return_value=None)
    monkeypatch.setattr(channel_watches.ChannelWatchSuggestionRepository, "get_by_id", get_by_id)
    monkeypatch.setattr(channel_watches.TaskRepository, "get_task_by_id", get_task_by_id)
    monkeypatch.setattr(channel_watches.ChannelWatchSuggestionRepository, "mark_created", mark_created)

    response = await client.post(
        "/channel-watches/suggestions/sugg-1/link-task", json={"task_id": "task-1"}
    )

    assert response.status_code == 200
    assert mark_created.await_args.args[1] == "sugg-1"
    assert mark_created.await_args.kwargs == {"task_id": "task-1"}


async def test_link_task_returns_404_when_task_belongs_to_another_user(client, monkeypatch):
    get_by_id = AsyncMock(return_value={"id": "sugg-1", "user_id": "user-1", "status": "pending"})
    get_task_by_id = AsyncMock(return_value={"id": "task-1", "user_id": "someone-else"})
    mark_created = AsyncMock(return_value=None)
    monkeypatch.setattr(channel_watches.ChannelWatchSuggestionRepository, "get_by_id", get_by_id)
    monkeypatch.setattr(channel_watches.TaskRepository, "get_task_by_id", get_task_by_id)
    monkeypatch.setattr(channel_watches.ChannelWatchSuggestionRepository, "mark_created", mark_created)

    response = await client.post(
        "/channel-watches/suggestions/sugg-1/link-task", json={"task_id": "task-1"}
    )

    assert response.status_code == 404
    mark_created.assert_not_awaited()


async def test_link_task_returns_404_when_suggestion_belongs_to_another_user(client, monkeypatch):
    get_by_id = AsyncMock(return_value={"id": "sugg-1", "user_id": "someone-else", "status": "pending"})
    get_task_by_id = AsyncMock(return_value={"id": "task-1", "user_id": "user-1"})
    monkeypatch.setattr(channel_watches.ChannelWatchSuggestionRepository, "get_by_id", get_by_id)
    monkeypatch.setattr(channel_watches.TaskRepository, "get_task_by_id", get_task_by_id)

    response = await client.post(
        "/channel-watches/suggestions/sugg-1/link-task", json={"task_id": "task-1"}
    )

    assert response.status_code == 404


async def test_link_task_requires_task_id(client, monkeypatch):
    get_by_id = AsyncMock(return_value={"id": "sugg-1", "user_id": "user-1", "status": "pending"})
    monkeypatch.setattr(channel_watches.ChannelWatchSuggestionRepository, "get_by_id", get_by_id)

    response = await client.post("/channel-watches/suggestions/sugg-1/link-task", json={})

    assert response.status_code == 422
