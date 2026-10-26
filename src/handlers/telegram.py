import asyncio
import html
import logging
import mimetypes
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, TypeAlias

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Audio, Document, Message as TgMessage, PhotoSize, Video
from maxapi.enums.attachment import AttachmentType
from maxapi.enums.message_link_type import MessageLinkType
from maxapi.enums.parse_mode import ParseMode
from maxapi.enums.upload_type import UploadType
from maxapi.exceptions import MaxApiError
from maxapi.methods.types.sended_message import SendedMessage
from maxapi.types import InputMedia, NewMessageLink
from maxapi.types.attachments.upload import AttachmentPayload, AttachmentUpload

from src.config import TG_CHANNEL_ROUTES, TG_GROUP_ROUTES, Route, is_local_api
from src.loader import max_bot, message_map, tg_bot
from src.locks import keyed_lock, keyed_throttle
from src.storage.message_map import MaxAtt, MaxPost, MaxRef, TgRef

logger = logging.getLogger(__name__)

media_groups: dict[str, list[TgMessage]] = {}
media_group_targets: dict[str, list["Target"]] = {}
_bg_tasks: set[asyncio.Task] = set()

AnyMedia: TypeAlias = Video | PhotoSize | Document | Audio
# One destination of a TG message: the route plus the signature prefix it wants.
Target: TypeAlias = tuple[Route, str]
# What one uploaded file was: (source TG message, file identity, MAX upload type).
Uploaded: TypeAlias = tuple[int, str, UploadType]

tg_router = Router()


async def _safe_max_send(**kwargs) -> SendedMessage | None:
    """Send to MAX as HTML; on API rejection (e.g. unsupported tag) retry as plain text."""
    try:
        return await max_bot.send_message(parse_mode=ParseMode.HTML, **kwargs)
    except MaxApiError as e:
        logger.warning("MAX send failed (%s); retrying as plain text", e)
        return await max_bot.send_message(parse_mode=None, **kwargs)


# MAX allows two edits per second per chat, and maxapi does not pace them: it
# only waits after uploading media, which an edit that just changes a caption
# never does. Editing an album item by item in Telegram produces one update per
# item, so a quick series of them would run into the limit unpaced.
MAX_EDIT_INTERVAL = 0.55
_edit_throttle = keyed_throttle(MAX_EDIT_INTERVAL)


async def _safe_max_edit(max_id: int, **kwargs) -> bool:
    """Edit in MAX as HTML; on API rejection retry as plain text."""
    await _edit_throttle.wait(max_id)
    try:
        await max_bot.edit_message(parse_mode=ParseMode.HTML, **kwargs)
        return True
    except ValueError as e:
        logger.warning("MAX edit rejected (%s)", e)
        return False
    except MaxApiError as e:
        logger.warning("MAX edit failed as HTML (%s); retrying as plain text", e)
    await _edit_throttle.wait(max_id)
    try:
        await max_bot.edit_message(parse_mode=None, **kwargs)
        return True
    except (MaxApiError, ValueError) as e:
        logger.warning("MAX edit failed (%s)", e)
        return False


def _extract_mid(sent: SendedMessage | None) -> str | None:
    if sent is None or sent.message is None or sent.message.body is None:
        return None
    return sent.message.body.mid


async def _resolve_reply_link(message: TgMessage, max_id: int) -> NewMessageLink | None:
    """If TG message is a reply, look up the mid it maps to in `max_id` and build a REPLY link."""
    if not message.reply_to_message:
        return None
    mx = await message_map.get_max_in(message.chat.id, message.reply_to_message.message_id, max_id)
    if mx is None:
        return None
    return NewMessageLink(type=MessageLinkType.REPLY, mid=mx.mid)


# ====== MAX ATTACHMENT TOKENS ======

# MAX hands an attachment back in its *response* shape (the token sits in
# `payload` for images, audio and files, but at the top level for video), while
# both send and edit expect the *upload* shape. Normalising the token once here
# is what lets an edit keep media it is not replacing instead of re-uploading it
# — or, worse, dropping it. PUT /messages leaves attachments alone only when the
# field is absent, and maxapi always sends it, so an edit that does not restate
# them sends an empty list, which MAX reads as "remove them all".

_UPLOAD_OF_ATT = {
    AttachmentType.IMAGE: UploadType.IMAGE,
    AttachmentType.VIDEO: UploadType.VIDEO,
    AttachmentType.AUDIO: UploadType.AUDIO,
    AttachmentType.FILE: UploadType.FILE,
}


def _att_token(att) -> str:
    token = getattr(att, "token", None)
    if not token:
        payload = getattr(att, "payload", None)
        token = getattr(payload, "token", None) if payload else None
    return str(token) if token else ""


def _att_upload_type(att) -> UploadType | None:
    try:
        return _UPLOAD_OF_ATT[AttachmentType(att.type)]
    except (KeyError, ValueError):
        return None


def _keep(kind: str, token: str) -> AttachmentUpload | None:
    """Re-attach a file MAX already stores, by token — no upload needed."""
    if not token:
        return None
    try:
        upload_type = UploadType(kind)
    except ValueError:
        return None
    return AttachmentUpload(type=upload_type, payload=AttachmentPayload(token=token))


def _sent_atts(sent: SendedMessage | None, uploaded: list[Uploaded]) -> tuple[MaxAtt, ...]:
    """Pair the files we uploaded with the attachments MAX stored, keeping its tokens."""
    atts = []
    if sent is not None and sent.message is not None and sent.message.body is not None:
        atts = sent.message.body.attachments or []
    out = []
    for i, (tg_message_id, sig, upload_type) in enumerate(uploaded):
        out.append(
            MaxAtt(
                kind=upload_type.value,
                sig=sig,
                token=_att_token(atts[i]) if i < len(atts) else "",
                tg_message_id=tg_message_id,
            )
        )
    return tuple(out)


async def _remote_message(mid: str):
    """Read a MAX message back, for what our own record does not (or no longer) know."""
    try:
        return await max_bot.get_message(mid)
    except (MaxApiError, ValueError) as e:
        logger.warning("Cannot read MAX message %s: %s", mid, e)
        return None


async def _remote_atts(mid: str) -> list | None:
    """Current attachments of a MAX message, when our own record lost their tokens."""
    remote = await _remote_message(mid)
    if remote is None:
        return None
    return (remote.body.attachments or []) if remote.body else []


# ====== REGEX ======

TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.DOTALL)
TG_SPOILER_RE = re.compile(r"<tg-spoiler[^>]*>(.*?)</tg-spoiler>", re.DOTALL)
ANCHOR_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.DOTALL | re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![\w@/])@([A-Za-z][A-Za-z0-9_]{4,31})\b")


# ====== TEXT UTILS ======


def linkify_mentions(html: str) -> str:
    """Wrap @mentions in <a href="https://t.me/..."> while skipping existing anchors."""
    parts = []
    last = 0
    for m in ANCHOR_RE.finditer(html):
        parts.append(
            MENTION_RE.sub(r'<a href="https://t.me/\1">@tg:\1</a>', html[last : m.start()])
        )
        parts.append(m.group(0))
        last = m.end()
    parts.append(MENTION_RE.sub(r'<a href="https://t.me/\1">@tg:\1</a>', html[last:]))
    return "".join(parts)


def clean_html(html: str) -> str:
    """Удаляет служебные Telegram-теги, сохраняя содержимое."""
    html = TG_EMOJI_RE.sub(r"\1", html)
    html = TG_SPOILER_RE.sub(r"\1", html)
    html = linkify_mentions(html)
    return html


def extract_text(message: TgMessage) -> str:
    return clean_html(message.html_text or "")


def has_own_text(message: TgMessage) -> bool:
    """True if the message carries text of its own — an album item may not."""
    return message.text is not None or message.caption is not None


def build_sign_prefix(message: TgMessage) -> str:
    """Build HTML signature prefix `<b>Name</b>:\\n` for group sign_names mode."""
    u = message.from_user
    if not u:
        return ""
    name = u.full_name or u.first_name or (f"@{u.username}" if u.username else "")
    if not name:
        return ""
    safe = html.escape(name)
    if u.username:
        return f'<b><a href="https://t.me/{u.username}">{safe}</a></b>:\n'
    return f"<b>{safe}</b>:\n"


def _apply_prefix(prefix: str, text: str) -> str:
    if not prefix:
        return text
    return prefix + text if text else prefix.rstrip(":\n")


# ====== FILE UTILS ======

TG_FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20 MB — Telegram Bot API hard limit
if is_local_api:
    TG_FILE_SIZE_LIMIT = 2 * 1024 * 1024 * 1024  # 2 GB — local API hard limit


def _resolve_suffix(file: AnyMedia, fallback: str | None) -> str:
    mime = getattr(file, "mime_type", None)
    if mime:
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return guessed
    if fallback and fallback != "*":
        return fallback
    return ".bin"


def _resolve_filename(file: AnyMedia, fallback_suffix: str | None) -> str:
    name = getattr(file, "file_name", None)
    if name:
        name = os.path.basename(name)
        if name:
            return name
    return f"file{_resolve_suffix(file, fallback_suffix)}"


def _cleanup_temp(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    finally:
        parent = os.path.dirname(path)
        if parent and os.path.isdir(parent):
            try:
                os.rmdir(parent)
            except OSError:
                pass


async def download_to_temp(file: AnyMedia, suffix: str | None) -> str:
    file_size = getattr(file, "file_size", None)
    if file_size and file_size > TG_FILE_SIZE_LIMIT:
        raise ValueError(f"File too large for Bot API: {file_size} bytes")

    file_info = await tg_bot.get_file(file.file_id)
    if not file_info.file_path:
        raise ValueError("File not found")

    tmp_dir = tempfile.mkdtemp()
    path = os.path.join(tmp_dir, _resolve_filename(file, suffix))

    await tg_bot.download_file(file_info.file_path, destination=path)
    return path


async def send_media(
    *,
    file: AnyMedia,
    suffix: str | None,
    upload_type: UploadType,
    text: str,
    max_id: int,
    link: NewMessageLink | None = None,
) -> SendedMessage | None:
    """Returns SendedMessage on success, None if file too large to download or send failed."""
    try:
        temp_path = await download_to_temp(file, suffix)
    except (TelegramBadRequest, ValueError) as e:
        logger.warning("Cannot download file: %s", e)
        return None

    try:
        media = InputMedia(temp_path, upload_type)
        return await _safe_max_send(
            chat_id=max_id,
            text=text,
            attachments=[media],
            link=link,
        )
    finally:
        _cleanup_temp(temp_path)


# ====== MEDIA GROUP ======


async def send_media_group(media_group_id: str):
    """Flush one TG album: download every item once, then post it to each target."""
    await asyncio.sleep(2)

    messages = media_groups.pop(media_group_id, [])
    targets = media_group_targets.pop(media_group_id, [])
    if not messages or not targets:
        return

    messages.sort(key=lambda m: m.message_id)
    caption_msg = next((m for m in messages if m.caption), None)
    base_text = extract_text(caption_msg) if caption_msg else None

    temp_files: list[str] = []
    specs: list[tuple[str, UploadType]] = []
    uploaded: list[Uploaded] = []

    try:
        for msg in messages:
            media = _tg_media(msg)
            if media is None:
                continue

            try:
                path = await download_to_temp(media.file, media.suffix)
            except (TelegramBadRequest, ValueError) as e:
                logger.warning("Media group %s: skipping item — %s", media_group_id, e)
                continue

            temp_files.append(path)
            specs.append((path, media.upload_type))
            uploaded.append((msg.message_id, media.file.file_unique_id, media.upload_type))

        if not specs:
            return

        tg_refs = [TgRef(chat_id=m.chat.id, message_id=m.message_id) for m in messages]

        for route, prefix in targets:
            text = _apply_prefix(prefix, base_text or "") if prefix else base_text
            link = await _resolve_reply_link(messages[0], route.max_id)
            try:
                sent = await _safe_max_send(
                    chat_id=route.max_id,
                    text=text,
                    attachments=[InputMedia(path, upload_type) for path, upload_type in specs],
                    link=link,
                )
            except Exception:
                logger.exception("Media group %s -> MAX %d failed", media_group_id, route.max_id)
                continue

            logger.info(
                "Forwarded media group %s (%d items) to MAX channel %d",
                media_group_id,
                len(specs),
                route.max_id,
            )
            mid = _extract_mid(sent)
            if mid:
                await message_map.bind_many(tg_refs, MaxRef(chat_id=route.max_id, mid=mid))
                await message_map.set_max_post(
                    route.max_id,
                    mid,
                    MaxPost(
                        atts=_sent_atts(sent, uploaded),
                        text=text,
                        caption_tg_id=caption_msg.message_id if caption_msg else None,
                    ),
                )

    except Exception:
        logger.exception("Media group error")

    finally:
        for path in temp_files:
            _cleanup_temp(path)


# ====== MEDIA HANDLERS ======

MEDIA_HANDLERS = {
    "photo": (".jpg", UploadType.IMAGE),
    "video": (".mp4", UploadType.VIDEO),
    "audio": (".mp3", UploadType.AUDIO),
    "document": (None, UploadType.FILE),
}


@dataclass(frozen=True)
class TgMedia:
    """The one attachment of a TG message, with everything needed to re-upload it."""

    file: AnyMedia
    suffix: str | None
    upload_type: UploadType


def _tg_media(message: TgMessage) -> TgMedia | None:
    for attr, (suffix, upload_type) in MEDIA_HANDLERS.items():
        media_obj = getattr(message, attr, None)
        if media_obj:
            file = media_obj[-1] if isinstance(media_obj, list) else media_obj
            return TgMedia(file=file, suffix=suffix, upload_type=upload_type)
    return None


# ====== ROUTING ======


def _passes(route: Route, message: TgMessage, check_threads: bool) -> bool:
    if route.allowed_user_ids is not None and (
        not message.from_user or message.from_user.id not in route.allowed_user_ids
    ):
        return False

    return not (
        check_threads
        and route.allowed_thread_ids is not None
        and message.message_thread_id not in route.allowed_thread_ids
        and int(bool(message.message_thread_id)) not in route.allowed_thread_ids
    )


def _targets(routes: list[Route], message: TgMessage, check_threads: bool) -> list[Target]:
    """Routes this message may travel, each paired with the signature prefix it wants."""
    return [
        (route, build_sign_prefix(message) if route.sign_names else "")
        for route in routes
        if _passes(route, message, check_threads)
    ]


# ====== FORWARDING ======


async def forward_to_max(message: TgMessage, targets: list[Target]):
    """Forward one TG message to every MAX chat it is routed to."""
    if not targets:
        return

    logger.info(
        "New Telegram post %s in chat %s -> MAX ids %s",
        message.message_id,
        message.chat.id,
        [route.max_id for route, _ in targets],
    )

    # media group — buffered once, flushed to every target together
    if message.media_group_id:
        first = message.media_group_id not in media_groups
        media_groups.setdefault(message.media_group_id, []).append(message)
        if first:
            media_group_targets[message.media_group_id] = targets
            task = asyncio.create_task(send_media_group(message.media_group_id))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
        return

    for route, prefix in targets:
        await forward_one_to_max(message, route.max_id, prefix)


async def forward_one_to_max(message: TgMessage, max_id: int, sender_prefix: str = ""):
    """Forward a single (non-album) TG message to one MAX chat."""
    text = _apply_prefix(sender_prefix, extract_text(message))
    link = await _resolve_reply_link(message, max_id)
    tg_ref = TgRef(chat_id=message.chat.id, message_id=message.message_id)
    caption_tg_id = message.message_id if has_own_text(message) else None

    # single media
    media = _tg_media(message)
    if media is not None:
        sent = await send_media(
            file=media.file,
            suffix=media.suffix,
            upload_type=media.upload_type,
            text=text,
            max_id=max_id,
            link=link,
        )
        if sent is None:
            logger.warning(
                "Skipped media (message %s) in chat %s — file too large for Bot API",
                message.message_id,
                message.chat.id,
            )
            return

        logger.info("Forwarded %s to MAX channel %d", media.upload_type.value, max_id)
        mid = _extract_mid(sent)
        if mid:
            uploaded = [(message.message_id, media.file.file_unique_id, media.upload_type)]
            await message_map.bind(tg_ref, MaxRef(chat_id=max_id, mid=mid))
            await message_map.set_max_post(
                max_id,
                mid,
                MaxPost(
                    atts=_sent_atts(sent, uploaded),
                    text=text,
                    caption_tg_id=caption_tg_id,
                ),
            )
        return

    # text only
    if text:
        sent = await _safe_max_send(chat_id=max_id, text=text, link=link)
        logger.info("Forwarded text to MAX id %d", max_id)
        mid = _extract_mid(sent)
        if mid:
            await message_map.bind(tg_ref, MaxRef(chat_id=max_id, mid=mid))
            await message_map.set_max_post(
                max_id,
                mid,
                MaxPost(
                    text=text,
                    caption_tg_id=caption_tg_id,
                ),
            )


# ====== EDIT FORWARDING ======

# MAX edits replace the whole message body, so every edit has to restate the
# attachments it wants to keep. Telegram can neither add nor drop media on an
# existing message, so the attachment *list* never changes shape here — at most
# one item of it is swapped for another file.


@dataclass
class _Rebuilt:
    """The attachment list to send, and the state to remember for the next edit."""

    attachments: list[Any]
    atts: tuple[MaxAtt, ...]


async def _keep_all(post: MaxPost, mid: str) -> _Rebuilt | None:
    """Restate every attachment unchanged. None if any of them cannot be re-attached."""
    return await _rebuild_atts(post, mid, replace=None)


async def _rebuild_atts(
    post: MaxPost,
    mid: str,
    replace: tuple[int, TgMedia, str] | None,
) -> _Rebuilt | None:
    """Re-attach the stored attachments, swapping the one `replace` describes.

    `replace` is (tg_message_id, media, path): the album item whose file changed,
    already downloaded. Returns None when an attachment can neither be
    re-attached by token nor read back from MAX — dropping media silently would
    be worse than letting the edit through only partly applied.
    """
    remote: list | None = None
    attachments: list[Any] = []
    atts: list[MaxAtt] = []

    for i, att in enumerate(post.atts):
        if replace is not None and att.tg_message_id == replace[0]:
            _, media, path = replace
            attachments.append(InputMedia(path, media.upload_type))
            atts.append(
                MaxAtt(
                    kind=media.upload_type.value,
                    sig=media.file.file_unique_id,
                    token="",
                    tg_message_id=att.tg_message_id,
                )
            )
            continue

        kept = _keep(att.kind, att.token)
        if kept is None:
            if remote is None:
                remote = await _remote_atts(mid) or []
            if i < len(remote):
                upload_type = _att_upload_type(remote[i])
                token = _att_token(remote[i])
                if upload_type is not None:
                    kept = _keep(upload_type.value, token)
            if kept is None:
                logger.warning(
                    "MAX %s: attachment %d cannot be re-attached; edit skipped",
                    mid,
                    i,
                )
                return None
            att = MaxAtt(
                kind=kept.type.value,
                sig=att.sig,
                token=kept.payload.token,
                tg_message_id=att.tg_message_id,
            )
        attachments.append(kept)
        atts.append(att)

    return _Rebuilt(attachments=attachments, atts=tuple(atts))


def _edited_text(
    message: TgMessage,
    post: MaxPost | None,
    prefix: str,
) -> tuple[str | None, int | None]:
    """The text the MAX copy should carry after this TG edit, and who owns it.

    An album spreads over several TG messages but collapses into one MAX
    message, so an edit of an item that never had a caption must leave the text
    alone, while an edit of the item that did own it replaces — or clears — it.
    A text of None means "unknown": the item carries none and no layout says who
    does, so whatever MAX holds has to stand.
    """
    if has_own_text(message):
        return _apply_prefix(prefix, extract_text(message)), message.message_id

    if not message.media_group_id:
        # A lone message with no text left: its caption was removed.
        return _apply_prefix(prefix, ""), None

    if post is None:
        return None, None

    if post.caption_tg_id == message.message_id:
        return _apply_prefix(prefix, ""), None

    return post.text or "", post.caption_tg_id


async def forward_edit_to_max(message: TgMessage, targets: list[Target]):
    """Propagate a TG edit to every MAX copy of that message."""
    for route, prefix in targets:
        mx = await message_map.get_max_in(message.chat.id, message.message_id, route.max_id)
        if mx is None:
            logger.info(
                "Edit ignored: no MAX mapping for TG (%s, %s) in MAX %d",
                message.chat.id,
                message.message_id,
                route.max_id,
            )
            continue

        async with keyed_lock((route.max_id, mx.mid)):
            await edit_one_in_max(message, route.max_id, mx.mid, prefix)


async def edit_one_in_max(message: TgMessage, max_id: int, mid: str, prefix: str = "") -> None:
    post = await message_map.get_max_post(max_id, mid)
    media = _tg_media(message)
    text, caption_tg_id = _edited_text(message, post, prefix)
    link = await _resolve_reply_link(message, max_id)

    if post is None:
        await _edit_without_state(message, max_id, mid, media, text, caption_tg_id, link)
        return

    # Does this TG message own one of the MAX attachments, and did its file change?
    owns = next((a for a in post.atts if a.tg_message_id == message.message_id), None)
    replaces_media = (
        media is not None
        and (owns is None or owns.sig != media.file.file_unique_id)
        # An unmapped album item (its upload failed once) cannot be placed in the
        # list; a lone message owns the whole list, so it always can.
        and (owns is not None or not message.media_group_id)
    )

    temp_path: str | None = None
    try:
        if replaces_media and media is not None:
            try:
                temp_path = await download_to_temp(media.file, media.suffix)
            except (TelegramBadRequest, ValueError) as e:
                logger.warning("Cannot download edited file (%s); keeping MAX media", e)
                replaces_media = False

        if replaces_media and media is not None and temp_path is not None:
            if owns is not None:
                rebuilt = await _rebuild_atts(
                    post,
                    mid,
                    replace=(message.message_id, media, temp_path),
                )
            else:
                rebuilt = _Rebuilt(
                    attachments=[InputMedia(temp_path, media.upload_type)],
                    atts=(
                        MaxAtt(
                            kind=media.upload_type.value,
                            sig=media.file.file_unique_id,
                            tg_message_id=message.message_id,
                        ),
                    ),
                )
        else:
            rebuilt = await _keep_all(post, mid)

        if rebuilt is None:
            return

        if not await _safe_max_edit(
            max_id,
            message_id=mid,
            text=text,
            attachments=rebuilt.attachments,
            link=link,
        ):
            return
    finally:
        if temp_path:
            _cleanup_temp(temp_path)

    atts = rebuilt.atts
    if replaces_media:
        # The edit response carries no body, so the token of the file just
        # uploaded has to be read back before the next edit needs it.
        remote = await _remote_atts(mid)
        if remote is not None:
            atts = tuple(
                MaxAtt(
                    kind=a.kind,
                    sig=a.sig,
                    token=_att_token(remote[i]) if i < len(remote) else "",
                    tg_message_id=a.tg_message_id,
                )
                for i, a in enumerate(atts)
            )

    await message_map.set_max_post(
        max_id,
        mid,
        MaxPost(
            atts=atts,
            text=text,
            caption_tg_id=caption_tg_id,
        ),
    )
    logger.info("Edited MAX %s after TG edit", mid)


async def _edit_without_state(
    message: TgMessage,
    max_id: int,
    mid: str,
    media: TgMedia | None,
    text: str | None,
    caption_tg_id: int | None,
    link: NewMessageLink | None,
) -> None:
    """Edit a copy made before the layout was recorded (or after the record expired).

    Nothing is known about which attachment came from which TG message, so the
    MAX attachments are read back and restated as they are — along with the text,
    when the edited message carries none of its own. A lone message does own its
    single attachment, so there its new file is uploaded instead.
    """
    remote_message = await _remote_message(mid)
    if remote_message is None:
        return
    body = remote_message.body
    remote = (body.attachments or []) if body else []
    if text is None:
        text = (body.html_text or body.text or "") if body else ""

    attachments: list[Any] = []
    atts: list[MaxAtt] = []
    temp_path: str | None = None

    try:
        if media is not None and not message.media_group_id:
            try:
                temp_path = await download_to_temp(media.file, media.suffix)
            except (TelegramBadRequest, ValueError) as e:
                logger.warning("Cannot download edited file (%s); keeping MAX media", e)

        if temp_path is not None and media is not None:
            attachments = [InputMedia(temp_path, media.upload_type)]
            atts = [
                MaxAtt(
                    kind=media.upload_type.value,
                    sig=media.file.file_unique_id,
                    tg_message_id=message.message_id,
                )
            ]
        else:
            for i, att in enumerate(remote):
                upload_type = _att_upload_type(att)
                kept = _keep(upload_type.value, _att_token(att)) if upload_type else None
                if kept is None:
                    logger.warning(
                        "MAX %s: attachment %d cannot be re-attached; edit skipped",
                        mid,
                        i,
                    )
                    return
                attachments.append(kept)
                atts.append(MaxAtt(kind=kept.type.value, token=kept.payload.token))

        if not await _safe_max_edit(
            max_id,
            message_id=mid,
            text=text,
            attachments=attachments,
            link=link,
        ):
            return
    finally:
        if temp_path:
            _cleanup_temp(temp_path)

    if temp_path is not None:
        refreshed = await _remote_atts(mid)
        if refreshed:
            atts = [
                MaxAtt(
                    kind=a.kind,
                    sig=a.sig,
                    token=_att_token(refreshed[i]),
                    tg_message_id=a.tg_message_id,
                )
                for i, a in enumerate(atts)
                if i < len(refreshed)
            ]

    await message_map.set_max_post(
        max_id,
        mid,
        MaxPost(
            atts=tuple(atts),
            text=text,
            caption_tg_id=caption_tg_id,
        ),
    )
    logger.info("Edited MAX %s after TG edit (layout unknown)", mid)


# ====== MAIN HANDLERS ======


@tg_router.channel_post(F.chat.id.in_(TG_CHANNEL_ROUTES))
async def on_channel_post(message: TgMessage):
    await forward_to_max(message, _targets(TG_CHANNEL_ROUTES[message.chat.id], message, True))


@tg_router.message(F.chat.id.in_(TG_GROUP_ROUTES))
async def on_group_message(message: TgMessage):
    await forward_to_max(message, _targets(TG_GROUP_ROUTES[message.chat.id], message, True))


@tg_router.edited_channel_post(F.chat.id.in_(TG_CHANNEL_ROUTES))
async def on_channel_post_edited(message: TgMessage):
    await forward_edit_to_max(message, _targets(TG_CHANNEL_ROUTES[message.chat.id], message, False))


@tg_router.edited_message(F.chat.id.in_(TG_GROUP_ROUTES))
async def on_group_message_edited(message: TgMessage):
    await forward_edit_to_max(message, _targets(TG_GROUP_ROUTES[message.chat.id], message, False))
