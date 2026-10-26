from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis, from_url

logger = logging.getLogger(__name__)


def _loads(raw: Any) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Corrupt MessageMap post state: %r", raw)
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class TgRef:
    chat_id: int
    message_id: int


@dataclass(frozen=True)
class MaxRef:
    chat_id: int
    mid: str


# ====== POST STATE ======

# A post state records what a *destination* message currently looks like, so an
# edit of the source can be diffed against it: only the parts that really
# changed are patched, and an edit the destination cannot express in place
# (media added, removed or regrouped) is recognised as such.


@dataclass(frozen=True)
class TgItem:
    """One Telegram message produced from a MAX message.

    Attributes:
        message_id: Telegram message id.
        kind: ``text`` | ``photo`` | ``video`` | ``audio`` | ``document``.
        sig: MAX attachment token (or url) this item was built from.
        album: item belongs to the ``sendMediaGroup`` call.
        caption: item carries the caption/text of the post.
    """

    message_id: int
    kind: str
    sig: str = ""
    album: bool = False
    caption: bool = False


@dataclass(frozen=True)
class TgPost:
    """The Telegram rendering of one MAX message inside one TG chat."""

    items: tuple[TgItem, ...]
    text: str | None = None

    @property
    def message_ids(self) -> list[int]:
        return [i.message_id for i in self.items]


@dataclass(frozen=True)
class MaxAtt:
    """One attachment of the MAX copy of a Telegram message.

    Attributes:
        kind: MAX attachment type (``image``/``video``/``audio``/``file``).
        sig: ``file_unique_id`` of the Telegram file it came from.
        token: MAX token, to re-attach the file without uploading it again.
        tg_message_id: album item this attachment was uploaded from.
    """

    kind: str
    sig: str = ""
    token: str = ""
    tg_message_id: int | None = None


@dataclass(frozen=True)
class MaxPost:
    """The MAX rendering of one Telegram message inside one MAX chat."""

    atts: tuple[MaxAtt, ...] = ()
    text: str | None = None
    caption_tg_id: int | None = None


class MessageMapService:
    """
    Async Redis-backed mapping between Telegram and MAX message identifiers.

    Keys (both are Redis sets):
        mm:tg:{tg_chat_id}:{tg_msg_id}  -> {"{max_chat_id}:{max_mid}", ...}
        mm:max:{max_chat_id}:{max_mid}  -> {"{tg_chat_id}:{tg_msg_id}", ...}

    Sets, not plain values: one source message may be mirrored into several
    destination chats (one tg_id fanning out to many max_id and vice versa), so
    a single message id can hold several counterparts — at most one per
    destination chat. Lookups are therefore scoped by destination chat
    (:meth:`get_max_in` / :meth:`get_tg_in`); the ``*_all`` variants return
    every counterpart, for fanning an edit out to all of them.

    Alongside the mapping, each destination message keeps a *post state*
    (:class:`TgPost` / :class:`MaxPost`) describing how it was rendered —
    which messages, which media, which text. Edits of the source are diffed
    against it, and its mere presence marks the message as a copy this bot
    created, which is what tells an original apart from a mirror in a
    bidirectional pair.

    Both directions are written with the same TTL, refreshed on every bind.
    """

    _TG_PREFIX = "mm:tg"
    _MAX_PREFIX = "mm:max"
    _TG_POST_PREFIX = "mm:st:tg"
    _MAX_POST_PREFIX = "mm:st:max"

    def __init__(self, url: str, ttl_seconds: int) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._redis: Redis | None = None

    async def connect(self) -> None:
        if self._redis is not None:
            return
        self._redis = from_url(self._url, decode_responses=True)
        await self._redis.ping()
        logger.info("MessageMap connected to Redis (%s), TTL=%ds", self._url, self._ttl)

    async def close(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.aclose()
        finally:
            self._redis = None

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            raise RuntimeError("MessageMapService is not connected. Call connect() first.")
        return self._redis

    @classmethod
    def _tg_key(cls, chat_id: int, msg_id: int) -> str:
        return f"{cls._TG_PREFIX}:{chat_id}:{msg_id}"

    @classmethod
    def _max_key(cls, chat_id: int, mid: str) -> str:
        return f"{cls._MAX_PREFIX}:{chat_id}:{mid}"

    @classmethod
    def _tg_post_key(cls, chat_id: int, msg_id: int) -> str:
        return f"{cls._TG_POST_PREFIX}:{chat_id}:{msg_id}"

    @classmethod
    def _max_post_key(cls, chat_id: int, mid: str) -> str:
        return f"{cls._MAX_POST_PREFIX}:{chat_id}:{mid}"

    # ====== WRITE ======

    async def bind(self, tg: TgRef, mx: MaxRef) -> None:
        """Store both forward (tg->max) and reverse (max->tg) mappings."""
        await self.bind_many([tg], mx)

    async def bind_many(self, tg_refs: list[TgRef], mx: MaxRef) -> None:
        """Bind several TG messages (e.g. media group) to one MAX message.

        Every TG message points at the MAX one; the reverse points back at the
        first (primary) TG ref only, so a reply to the MAX message lands on the
        head of the album rather than an arbitrary item.
        """
        if not tg_refs:
            return
        pipe = self.redis.pipeline(transaction=False)
        for tg in tg_refs:
            key = self._tg_key(tg.chat_id, tg.message_id)
            await pipe.sadd(key, f"{mx.chat_id}:{mx.mid}")
            await pipe.expire(key, self._ttl)
        primary = tg_refs[0]
        max_key = self._max_key(mx.chat_id, mx.mid)
        await pipe.sadd(max_key, f"{primary.chat_id}:{primary.message_id}")
        await pipe.expire(max_key, self._ttl)
        await pipe.execute()
        logger.debug("MessageMap bind tg=%s max=%s", tg_refs, mx)

    # ====== READ ======

    @staticmethod
    def _split(raw: str) -> tuple[str, str] | None:
        head, _, tail = raw.partition(":")
        if not head or not tail:
            return None
        return head, tail

    def _parse_max(self, members: Iterable[str]) -> list[MaxRef]:
        out: list[MaxRef] = []
        for raw in members:
            parts = self._split(raw)
            if parts is None:
                logger.warning("Corrupt MessageMap member: %r", raw)
                continue
            chat_str, mid = parts
            try:
                out.append(MaxRef(chat_id=int(chat_str), mid=mid))
            except ValueError:
                logger.warning("Corrupt MessageMap member: %r", raw)
        return out

    def _parse_tg(self, members: Iterable[str]) -> list[TgRef]:
        out: list[TgRef] = []
        for raw in members:
            parts = self._split(raw)
            if parts is None:
                logger.warning("Corrupt MessageMap member: %r", raw)
                continue
            chat_str, msg_str = parts
            try:
                out.append(TgRef(chat_id=int(chat_str), message_id=int(msg_str)))
            except ValueError:
                logger.warning("Corrupt MessageMap member: %r", raw)
        return out

    async def get_max_all(self, tg_chat_id: int, tg_msg_id: int) -> list[MaxRef]:
        """Every MAX counterpart of a TG message (one per destination MAX chat)."""
        # decode_responses=True, so members are str — the stub allows bytes too
        members = await self.redis.smembers(self._tg_key(tg_chat_id, tg_msg_id))
        return self._parse_max(members)  # ty:ignore[invalid-argument-type]

    async def get_max_in(self, tg_chat_id: int, tg_msg_id: int, max_chat_id: int) -> MaxRef | None:
        """The MAX counterpart of a TG message inside one specific MAX chat."""
        for mx in await self.get_max_all(tg_chat_id, tg_msg_id):
            if mx.chat_id == max_chat_id:
                return mx
        return None

    async def get_tg_all(self, max_chat_id: int, max_mid: str) -> list[TgRef]:
        """Every TG counterpart of a MAX message (one per destination TG chat)."""
        # decode_responses=True, so members are str — the stub allows bytes too
        members = await self.redis.smembers(self._max_key(max_chat_id, max_mid))
        return self._parse_tg(members)  # ty:ignore[invalid-argument-type]

    async def get_tg_in(self, max_chat_id: int, max_mid: str, tg_chat_id: int) -> TgRef | None:
        """The TG counterpart of a MAX message inside one specific TG chat."""
        for tg in await self.get_tg_all(max_chat_id, max_mid):
            if tg.chat_id == tg_chat_id:
                return tg
        return None

    # ====== POST STATE ======

    async def set_tg_post(self, chat_id: int, head_msg_id: int, post: TgPost) -> None:
        """Remember how a MAX message was rendered into TG chat `chat_id`.

        Keyed by the *first* TG message of the rendering, which is the one
        :meth:`get_tg_in` returns, so the state is reachable from the MAX side.
        """
        payload = {
            "items": [
                {
                    "id": item.message_id,
                    "kind": item.kind,
                    "sig": item.sig,
                    "album": item.album,
                    "caption": item.caption,
                }
                for item in post.items
            ],
            "text": post.text,
        }
        await self.redis.set(
            self._tg_post_key(chat_id, head_msg_id), json.dumps(payload), ex=self._ttl
        )

    async def get_tg_post(self, chat_id: int, head_msg_id: int) -> TgPost | None:
        raw = await self.redis.get(self._tg_post_key(chat_id, head_msg_id))
        data = _loads(raw)
        if data is None:
            return None
        items = []
        for entry in data.get("items") or ():
            try:
                items.append(
                    TgItem(
                        message_id=int(entry["id"]),
                        kind=str(entry["kind"]),
                        sig=str(entry.get("sig") or ""),
                        album=bool(entry.get("album")),
                        caption=bool(entry.get("caption")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Corrupt TgPost item: %r", entry)
                return None
        return TgPost(items=tuple(items), text=data.get("text"))

    async def drop_tg_post(self, chat_id: int, head_msg_id: int) -> None:
        await self.redis.delete(self._tg_post_key(chat_id, head_msg_id))

    async def set_max_post(self, chat_id: int, mid: str, post: MaxPost) -> None:
        """Remember how a TG message was rendered into MAX chat `chat_id`."""
        payload = {
            "atts": [
                {
                    "kind": att.kind,
                    "sig": att.sig,
                    "token": att.token,
                    "tg": att.tg_message_id,
                }
                for att in post.atts
            ],
            "text": post.text,
            "caption_tg_id": post.caption_tg_id,
        }
        await self.redis.set(self._max_post_key(chat_id, mid), json.dumps(payload), ex=self._ttl)

    async def get_max_post(self, chat_id: int, mid: str) -> MaxPost | None:
        raw = await self.redis.get(self._max_post_key(chat_id, mid))
        data = _loads(raw)
        if data is None:
            return None
        atts = []
        for entry in data.get("atts") or ():
            try:
                tg = entry.get("tg")
                atts.append(
                    MaxAtt(
                        kind=str(entry["kind"]),
                        sig=str(entry.get("sig") or ""),
                        token=str(entry.get("token") or ""),
                        tg_message_id=int(tg) if tg is not None else None,
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Corrupt MaxPost attachment: %r", entry)
                return None
        return MaxPost(
            atts=tuple(atts),
            text=data.get("text"),
            caption_tg_id=data.get("caption_tg_id"),
        )

    async def drop_max_post(self, chat_id: int, mid: str) -> None:
        await self.redis.delete(self._max_post_key(chat_id, mid))

    async def has_max_post(self, chat_id: int, mid: str) -> bool:
        """True if this bot created the MAX message (it is a copy, not a source)."""
        return bool(await self.redis.exists(self._max_post_key(chat_id, mid)))

    # ====== DELETE ======

    async def forget_tg(self, tg_chat_id: int, tg_msg_id: int) -> None:
        """Drop a TG message, its post state and every link to a MAX counterpart."""
        refs = await self.get_max_all(tg_chat_id, tg_msg_id)
        pipe = self.redis.pipeline(transaction=False)
        await pipe.delete(self._tg_key(tg_chat_id, tg_msg_id))
        await pipe.delete(self._tg_post_key(tg_chat_id, tg_msg_id))
        for mx in refs:
            await pipe.srem(self._max_key(mx.chat_id, mx.mid), f"{tg_chat_id}:{tg_msg_id}")
        await pipe.execute()

    async def forget_max(self, max_chat_id: int, max_mid: str) -> None:
        """Drop a MAX message, its post state and every link to a TG counterpart."""
        refs = await self.get_tg_all(max_chat_id, max_mid)
        pipe = self.redis.pipeline(transaction=False)
        await pipe.delete(self._max_key(max_chat_id, max_mid))
        await pipe.delete(self._max_post_key(max_chat_id, max_mid))
        for tg in refs:
            await pipe.srem(self._tg_key(tg.chat_id, tg.message_id), f"{max_chat_id}:{max_mid}")
        await pipe.execute()
