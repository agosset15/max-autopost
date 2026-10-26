"""Concurrency guards shared by both handlers: serialisation and pacing."""

import asyncio
from collections.abc import Hashable
from typing import ClassVar


class keyed_lock:
    """Async context manager serializing work on one key.

    Edits of the same message must not overlap: both directions rebuild the
    destination from the state recorded for it, so two updates running at once
    would start from the same stale state — and, on the MAX side, could re-send
    a post twice and leave a duplicate behind.

    Locks are created on demand and dropped once nobody holds or awaits them, so
    a long-running bot does not accumulate one per message it ever saw.
    """

    _locks: ClassVar[dict[Hashable, asyncio.Lock]] = {}
    _users: ClassVar[dict[Hashable, int]] = {}

    def __init__(self, key: Hashable) -> None:
        self._key = key

    async def __aenter__(self) -> None:
        lock = self._locks.setdefault(self._key, asyncio.Lock())
        self._users[self._key] = self._users.get(self._key, 0) + 1
        await lock.acquire()

    async def __aexit__(self, *exc_info) -> None:
        self._locks[self._key].release()
        left = self._users[self._key] - 1
        if left:
            self._users[self._key] = left
        else:
            del self._users[self._key]
            del self._locks[self._key]


class keyed_throttle:
    """Keep at least `interval` seconds between operations sharing a key.

    MAX accepts no more than two message edits per second in one chat, and an
    album edited item by item in Telegram arrives as one update per item — so
    the edits that follow have to be paced rather than fired as they come.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._next: dict[Hashable, float] = {}

    async def wait(self, key: Hashable) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        # The slot is reserved before awaiting, so concurrent callers queue up
        # behind each other instead of all reading the same free moment.
        due = max(self._next.get(key, 0.0), now)
        self._next[key] = due + self._interval
        if due > now:
            await asyncio.sleep(due - now)
