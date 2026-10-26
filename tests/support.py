"""Shared scaffolding for the handler tests.

Importing ``src.config`` reads the environment and the channel mapping, and
``src.loader`` builds both bots, so both have to be prepared before any handler
module is imported. Doing it here keeps every test module down to one import.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# ====== ENVIRONMENT (before importing src.*) ======

TG_CHANNEL = -1001000000001
MAX_CHANNEL = -71000000000001
TG_GROUP = -1001000000003
MAX_GROUP = -71000000000003
TG_SOURCE_MAX = -1001000000004  # fed by max_to_tg
TG_SOURCE_MAX_2 = -1001000000006  # same MAX source, second destination
MAX_SOURCE = -71000000000004
TG_BOTH = -1001000000005  # bidirectional pair
MAX_BOTH = -71000000000005

_CHANNELS = [
    {"type": "channel", "tg_id": TG_CHANNEL, "max_id": MAX_CHANNEL},
    {"type": "group", "tg_id": TG_GROUP, "max_id": MAX_GROUP},
    {"type": "channel", "tg_id": TG_SOURCE_MAX, "max_id": MAX_SOURCE, "direction": "max_to_tg"},
    {"type": "channel", "tg_id": TG_SOURCE_MAX_2, "max_id": MAX_SOURCE, "direction": "max_to_tg"},
    {"type": "group", "tg_id": TG_BOTH, "max_id": MAX_BOTH, "bidirectional": True},
]

_tmp = tempfile.mkdtemp(prefix="maxautopost-tests-")
_channels_file = os.path.join(_tmp, "channels.json")
with open(_channels_file, "w", encoding="utf-8") as fh:
    json.dump(_CHANNELS, fh)

os.environ.setdefault("CHANNELS_FILE", _channels_file)
os.environ.setdefault("WEBHOOK_URL", "https://example.test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("MAX_BOT_TOKEN", "max-test-token")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:AAEoTESTtokenTESTtokenTESTtokenTESTt")

from aiogram.types import (  # noqa: E402
    Audio,
    Chat,
    Document,
    Message as TgMessage,
    PhotoSize,
    Video,
)
from maxapi.enums.attachment import AttachmentType  # noqa: E402
from maxapi.enums.chat_type import ChatType  # noqa: E402
from maxapi.types import (  # noqa: E402
    Message as MaxMessage,
    MessageBody,
    Recipient,
    User,
)
from maxapi.types.attachments import (  # noqa: E402
    Audio as MaxAudio,
    File as MaxFile,
    Image,
    OtherAttachmentPayload,
    PhotoAttachmentPayload,
    Video as MaxVideo,
    VideoUrl,
)
from redis.asyncio import Redis  # noqa: E402

from src.storage.message_map import MessageMapService  # noqa: E402

# ====== MAX MESSAGE BUILDERS ======


def max_image(token: str) -> Image:
    return Image(
        type=AttachmentType.IMAGE,
        payload=PhotoAttachmentPayload(
            photo_id=abs(hash(token)) % 10**9, token=token, url=f"https://max.test/{token}.jpg"
        ),
    )


def max_video(token: str) -> MaxVideo:
    return MaxVideo(token=token, urls=VideoUrl(mp4_720=f"https://max.test/{token}.mp4"))


def max_audio(token: str) -> MaxAudio:
    return MaxAudio(
        type=AttachmentType.AUDIO,
        payload=OtherAttachmentPayload(url=f"https://max.test/{token}.mp3", token=token),
    )


def max_file(token: str, filename: str = "doc.pdf") -> MaxFile:
    return MaxFile(
        type=AttachmentType.FILE,
        filename=filename,
        size=10,
        payload=OtherAttachmentPayload(url=f"https://max.test/{token}.pdf", token=token),
    )


def max_message(
    *,
    chat_id: int | None = MAX_SOURCE,
    mid: str = "mid-1",
    text: str | None = None,
    attachments: list | None = None,
    sender_id: int | None = 555,
) -> MaxMessage:
    return MaxMessage(
        sender=User(user_id=sender_id, first_name="Sender", is_bot=False, last_activity_time=0)
        if sender_id is not None
        else None,
        recipient=Recipient(chat_id=chat_id, chat_type=ChatType.CHAT),
        timestamp=0,
        body=MessageBody(mid=mid, seq=1, text=text, attachments=attachments or []),
    )


# ====== TELEGRAM MESSAGE BUILDERS ======


def tg_message(
    *,
    message_id: int = 10,
    chat_id: int = TG_CHANNEL,
    text: str | None = None,
    caption: str | None = None,
    photo: str | None = None,
    video: str | None = None,
    audio: str | None = None,
    document: str | None = None,
    media_group_id: str | None = None,
) -> TgMessage:
    kwargs: dict[str, Any] = {
        "message_id": message_id,
        "date": datetime.now(tz=UTC),
        "chat": Chat(id=chat_id, type="channel"),
        "text": text,
        "caption": caption,
        "media_group_id": media_group_id,
    }
    if photo:
        kwargs["photo"] = [
            PhotoSize(
                file_id=f"id-{photo}", file_unique_id=photo, width=10, height=10, file_size=100
            )
        ]
    if video:
        kwargs["video"] = Video(
            file_id=f"id-{video}",
            file_unique_id=video,
            width=10,
            height=10,
            duration=1,
            file_size=100,
        )
    if audio:
        kwargs["audio"] = Audio(
            file_id=f"id-{audio}", file_unique_id=audio, duration=1, file_size=100
        )
    if document:
        kwargs["document"] = Document(
            file_id=f"id-{document}", file_unique_id=document, file_name="doc.pdf", file_size=100
        )
    return TgMessage.model_validate(kwargs)


_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


# ====== FAKE REDIS ======


class FakePipe:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._ops: list = []

    async def sadd(self, key, value):
        self._ops.append(("sadd", key, value))
        return self

    async def srem(self, key, value):
        self._ops.append(("srem", key, value))
        return self

    async def delete(self, *keys):
        self._ops.extend(("delete", key, None) for key in keys)
        return self

    async def expire(self, key, ttl):
        return self

    async def execute(self):
        for op, key, value in self._ops:
            if op == "sadd":
                self._redis.sets.setdefault(key, set()).add(value)
            elif op == "srem":
                self._redis.sets.get(key, set()).discard(value)
            else:
                self._redis.sets.pop(key, None)
                self._redis.values.pop(key, None)
        self._ops.clear()


class FakeRedis:
    """Just the handful of operations MessageMapService uses."""

    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.values: dict[str, str] = {}

    async def ping(self):
        return True

    def pipeline(self, transaction: bool = False):
        return FakePipe(self)

    async def smembers(self, key):
        return set(self.sets.get(key, ()))

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.sets.pop(key, None)
            self.values.pop(key, None)

    async def exists(self, key):
        return 1 if key in self.values else 0


def fake_map() -> MessageMapService:
    service = MessageMapService(url="redis://fake", ttl_seconds=60)
    service._redis = cast("Redis", FakeRedis())
    return service


def fake_redis(service: MessageMapService) -> FakeRedis:
    """The FakeRedis a `fake_map()` service writes to."""
    redis = service.redis
    assert isinstance(redis, FakeRedis)
    return redis


# ====== FAKE BOTS ======


@dataclass
class Call:
    name: str
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)


class _MaxSent:
    """What `max_bot.send_message` hands back: the stored message."""

    def __init__(self, message: MaxMessage) -> None:
        self.message = message


class _Sent:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeTgBot:
    """Records what the MAX handler asks Telegram to do."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.next_id = 100
        self.refuse: set[str] = set()
        self.files: dict[str, bytes] = {}

    def _record(self, name, *args, **kwargs) -> Call:
        call = Call(name=name, args=args, kwargs=kwargs)
        self.calls.append(call)
        return call

    def names(self) -> list[str]:
        return [c.name for c in self.calls]

    def last_delete(self) -> list[int]:
        """Message ids of the last delete, whether bulk or one by one."""
        for call in reversed(self.calls):
            if call.name == "delete_messages":
                return call.args[1]
            if call.name == "delete_message":
                return [call.args[1]]
        return []

    def _new(self) -> _Sent:
        self.next_id += 1
        return _Sent(self.next_id)

    def _maybe_refuse(self, name: str) -> None:
        if name in self.refuse:
            from aiogram.exceptions import TelegramBadRequest
            from aiogram.methods import SendMessage

            raise TelegramBadRequest(
                method=SendMessage(chat_id=1, text="x"), message="Bad Request: refused by test"
            )

    async def send_message(self, chat_id, text, **kwargs):
        self._record("send_message", chat_id, text, **kwargs)
        self._maybe_refuse("send_message")
        return self._new()

    async def send_photo(self, chat_id, **kwargs):
        self._record("send_photo", chat_id, **kwargs)
        self._maybe_refuse("send_photo")
        return self._new()

    async def send_video(self, chat_id, **kwargs):
        self._record("send_video", chat_id, **kwargs)
        self._maybe_refuse("send_video")
        return self._new()

    async def send_audio(self, chat_id, **kwargs):
        self._record("send_audio", chat_id, **kwargs)
        self._maybe_refuse("send_audio")
        return self._new()

    async def send_document(self, chat_id, **kwargs):
        self._record("send_document", chat_id, **kwargs)
        self._maybe_refuse("send_document")
        return self._new()

    async def send_media_group(self, chat_id, media, **kwargs):
        self._record("send_media_group", chat_id, media, **kwargs)
        self._maybe_refuse("send_media_group")
        return [self._new() for _ in media]

    async def edit_message_text(self, **kwargs):
        self._record("edit_message_text", **kwargs)
        self._maybe_refuse("edit_message_text")
        return True

    async def edit_message_caption(self, **kwargs):
        self._record("edit_message_caption", **kwargs)
        self._maybe_refuse("edit_message_caption")
        return True

    async def edit_message_media(self, **kwargs):
        self._record("edit_message_media", **kwargs)
        self._maybe_refuse("edit_message_media")
        return True

    async def delete_messages(self, chat_id, message_ids):
        self._record("delete_messages", chat_id, list(message_ids))
        self._maybe_refuse("delete_messages")
        return True

    async def delete_message(self, chat_id, message_id):
        self._record("delete_message", chat_id, message_id)
        self._maybe_refuse("delete_message")
        return True

    # --- TG -> MAX downloads ---

    async def get_file(self, file_id):
        self._record("get_file", file_id)

        class _Info:
            file_path = f"path/{file_id}"

        return _Info()

    async def download_file(self, file_path, destination):
        self._record("download_file", file_path, destination)
        Path(destination).write_bytes(_PNG)


_MAX_ATT_OF_UPLOAD = {
    "image": max_image,
    "video": max_video,
    "audio": max_audio,
    "file": max_file,
}


class FakeMaxBot:
    """Records what the Telegram handler asks MAX to do.

    Keeps a little server state: send and edit store the resulting message, so
    `get_message` answers with the attachments (and tokens) MAX would really
    hold afterwards.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.me = User(user_id=1, first_name="Bot", is_bot=True, last_activity_time=0)
        self.remote: dict[str, MaxMessage] = {}
        self.edit_fails = 0
        self.next_token = 0
        self.next_mid = 0
        self.last_mid = ""

    def names(self) -> list[str]:
        return [c.name for c in self.calls]

    def last(self, name: str) -> Call:
        return next(c for c in reversed(self.calls) if c.name == name)

    def _store_atts(self, attachments) -> list:
        """Turn what was sent into what MAX would hand back."""
        out = []
        for att in attachments or []:
            kind = att.type.value if hasattr(att.type, "value") else str(att.type)
            token = getattr(getattr(att, "payload", None), "token", None)
            if token is None:  # a freshly uploaded InputMedia
                self.next_token += 1
                token = f"stored{self.next_token}"
            out.append(_MAX_ATT_OF_UPLOAD[kind](token))
        return out

    async def send_message(self, **kwargs):
        self.calls.append(Call("send_message", kwargs=kwargs))
        self.next_mid += 1
        self.last_mid = f"mid-{self.next_mid}"
        message = max_message(
            chat_id=kwargs.get("chat_id"),
            mid=self.last_mid,
            text=kwargs.get("text"),
            attachments=self._store_atts(kwargs.get("attachments")),
            sender_id=self.me.user_id,
        )
        self.remote[self.last_mid] = message
        return _MaxSent(message)

    async def edit_message(self, **kwargs):
        self.calls.append(Call("edit_message", kwargs=kwargs))
        if self.edit_fails:
            self.edit_fails -= 1
            from maxapi.exceptions import MaxApiError

            raise MaxApiError(code=400, raw={"code": "test.refused"})

        mid = kwargs["message_id"]
        self.remote[mid] = max_message(
            chat_id=self.remote[mid].recipient.chat_id if mid in self.remote else None,
            mid=mid,
            text=kwargs.get("text"),
            attachments=self._store_atts(kwargs.get("attachments")),
            sender_id=self.me.user_id,
        )

        class _Result:
            success = True

        return _Result()

    async def get_message(self, message_id):
        self.calls.append(Call("get_message", args=(message_id,)))
        return self.remote[message_id]

    async def delete_message(self, message_id):
        self.calls.append(Call("delete_message", args=(message_id,)))

        class _Result:
            success = True

        return _Result()

    async def download_file(self, url, directory):
        self.calls.append(Call("download_file", args=(url, directory)))
        path = os.path.join(directory, os.path.basename(url) or "file.bin")
        Path(path).write_bytes(_PNG)
        return path
