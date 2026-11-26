"""Watches: what a gem watches for, kept, run and removed.

The database, the event sink, the connections' MCP events and the clock are stand-ins,
so a timer's hours pass at once and nothing reaches the internet.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from bson import ObjectId

from app.config import settings
from app.core.errors import InvalidError, NotFoundError
from app.entities.conversation_store import ConversationStoreClass
from app.entities.gem_ent import GemConfig, GemEntityClass
from app.entities.gem_log import LogEntityClass
from app.entities.watch_ent import Watch, WatchEntityClass
from tests.test_chat import _DocDB, _docdb  # pyright: ignore[reportPrivateUsage]

_PUBLIC = "https://hooks.example.com"


async def _until(done: Callable[[], bool]) -> None:
    """Let the tasks run until `done` holds."""
    for _ in range(10_000):
        if done():
            return
        await asyncio.sleep(0)
    raise AssertionError("it never came to hold")


class _Setup(SimpleNamespace):
    docdb: _DocDB
    gems: GemEntityClass
    log: LogEntityClass
    watches: WatchEntityClass
    one: GemConfig
    two: GemConfig
    told: list[tuple[str, str]]

    def stored(self) -> list[dict[str, Any]]:
        return self.docdb.collections.get("Watches", [])

    async def logged(self, gem: GemConfig) -> list[tuple[str, dict[str, Any]]]:
        await self.log.written(gem.id)
        return [
            (d["kind"], d["data"])
            for d in self.docdb.collections.get("GemLog", [])
            if d["gem_id"] == gem.id
        ]


@pytest.fixture
async def setup(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[_Setup]:
    monkeypatch.setattr(settings, "public_url", _PUBLIC)
    docdb, adapter = _docdb()
    gems = GemEntityClass(adapter)
    log = LogEntityClass(ConversationStoreClass(adapter))
    watches = WatchEntityClass(docdb=adapter, gems=gems, log=log)
    told: list[tuple[str, str]] = []

    async def listener(gem: str, watch: str) -> None:
        told.append((gem, watch))

    watches.listen(listener)
    yield _Setup(
        docdb=docdb,
        gems=gems,
        log=log,
        watches=watches,
        one=await gems.create(name="Gem One", model="6 Luna"),
        two=await gems.create(name="Gem Two", model="6 Luna"),
        told=told,
    )


# ---- what is kept


async def test_a_webhook_watch_is_kept_with_an_address_of_its_own(setup: _Setup) -> None:
    made = await setup.watches.add_webhook(setup.one.id, "Review each new pull request.")
    other = await setup.watches.add_webhook(setup.one.id, "Say when a build fails.")

    assert made.token is not None and len(made.token) >= 43
    assert made.token != other.token
    assert made.hook_url == f"{_PUBLIC}/v1/hooks/{made.token}"
    [stored, _] = setup.stored()
    assert Watch.model_validate(stored) == made
    assert (made.state, made.reason, made.events, made.last_event_at) == ("active", None, 0, None)


async def test_a_webhook_watch_is_refused_with_no_public_address(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "public_url", "")

    with pytest.raises(InvalidError, match="no public address"):
        await setup.watches.add_webhook(setup.one.id, "Review each new pull request.")
    assert setup.stored() == []


async def test_a_gem_s_watches_are_listed_newest_first(setup: _Setup) -> None:
    first = await setup.watches.add_webhook(setup.one.id, "first")
    await setup.watches.add_webhook(setup.two.id, "not hers")
    second = await setup.watches.add_webhook(setup.one.id, "second")

    listed = await setup.watches.list_for(setup.one.id)

    assert [watch.id for watch in listed] == [second.id, first.id]


# ---- removing


async def test_a_watch_removed_goes_is_written_down_and_told(setup: _Setup) -> None:
    made = await setup.watches.add_webhook(setup.one.id, "Review each new pull request.")

    await setup.watches.remove(setup.one.id, str(made.id), by="gem")

    assert setup.stored() == []
    assert await setup.logged(setup.one) == [
        (
            "watch.added",
            {"watch": str(made.id), "source": "webhook", "about": "Review each new pull request."},
        ),
        ("watch.removed", {"watch": str(made.id), "by": "gem"}),
    ]
    await _until(lambda: len(setup.told) == 2)
    assert setup.told == [(str(setup.one.id), str(made.id))] * 2


async def test_a_watch_of_another_gem_or_none_is_not_found(setup: _Setup) -> None:
    made = await setup.watches.add_webhook(setup.one.id, "first")

    for gem, watch in [(setup.two.id, str(made.id)), (setup.one.id, "nope"), (setup.one.id, "")]:
        with pytest.raises(NotFoundError):
            await setup.watches.remove(gem, watch, by="user")
    assert len(setup.stored()) == 1


async def test_end_for_removes_every_watch_of_the_gem_and_no_other(setup: _Setup) -> None:
    first = await setup.watches.add_webhook(setup.one.id, "first")
    second = await setup.watches.add_webhook(setup.one.id, "second")
    kept = await setup.watches.add_webhook(setup.two.id, "kept")

    await setup.watches.end_for(setup.one.id)

    assert [d["_id"] for d in setup.stored()] == [kept.id]
    removed = [data for kind, data in await setup.logged(setup.one) if kind == "watch.removed"]
    assert removed == [
        {"watch": str(second.id), "by": "user"},
        {"watch": str(first.id), "by": "user"},
    ]


async def test_a_deleted_gem_s_watches_go_with_it(setup: _Setup) -> None:
    await setup.watches.add_webhook(setup.one.id, "first")
    kept = await setup.watches.add_webhook(setup.two.id, "kept")

    await setup.gems.delete(str(setup.one.id))
    await _until(lambda: len(setup.stored()) == 1)

    assert [d["_id"] for d in setup.stored()] == [kept.id]


async def test_the_token_is_not_in_the_log(setup: _Setup) -> None:
    made = await setup.watches.add_webhook(setup.one.id, "Review each new pull request.")
    assert made.token is not None

    assert made.token not in repr(await setup.logged(setup.one))


async def test_a_watch_is_not_listed_for_an_unknown_gem(setup: _Setup) -> None:
    await setup.watches.add_webhook(setup.one.id, "first")

    assert await setup.watches.list_for(ObjectId()) == []
