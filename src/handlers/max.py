import html
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram.enums import ParseMode as TgParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message as TgMessage,
    ReplyParameters,
)
from maxapi import F, Router
from maxapi.enums.attachment import AttachmentType
from maxapi.enums.message_link_type import MessageLinkType
from maxapi.types import BotStarted, MessageCreated, MessageEdited, MessageRemoved

from src.config import MAX_ROUTES, MIRRORED_PAIRS, Route
from src.loader import max_bot, message_map, tg_bot
from src.locks import keyed_lock
from src.storage.message_map import MaxRef, TgItem, TgPost, TgRef

logger = logging.getLogger(__name__)

max_router = Router(router_id="main")


@max_router.bot_started()
async def bot_started(event: BotStarted):
    await max_bot.send_message(chat_id=event.chat_id, text="Привет! Отправь мне /start")


# ====== SIGN UTILS ======


def build_sign_prefix(sender) -> str:
    """Build HTML signature prefix `<b>Name</b>:\\n` for MAX -> TG sign_names mode."""
    if not sender:
        return ""
    name = getattr(sender, "full_name", None) or getattr(sender, "first_name", None)
    if not name:
        username = getattr(sender, "username", None)
        name = f"@{username}" if username else ""
    if not name:
        return ""
    return f"<b>{html.escape(name)}</b>:\n"


def _apply_prefix(prefix: str, text: str | None) -> str | None:
    if not prefix:
        return text
    if not text:
        return prefix.rstrip(":\n")
    return prefix + text


# ====== TEMP UTILS ======


def _cleanup(path: str) -> None:
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


async def _download(url: str) -> str:
    tmp_dir = tempfile.mkdtemp()
    path = await max_bot.download_file(url, tmp_dir)
    return str(path)


async def _download_cached(url: str, cache: dict[str, str]) -> str:
    """Download once per url; a MAX message fanned out to several TG chats reuses the file."""
    path = cache.get(url)
    if path is None:
        path = await _download(url)
        cache[url] = path
    return path


def cleanup_cache(cache: dict[str, str]) -> None:
    for path in cache.values():
        _cleanup(path)


# ====== ATTACHMENT URL ======


def _attachment_url(att) -> str | None:
    if att.type == AttachmentType.VIDEO:
        urls = getattr(att, "urls", None)
        if urls:
            for res in (
                urls.mp4_1080,
                urls.mp4_720,
                urls.mp4_480,
                urls.mp4_360,
                urls.mp4_240,
                urls.mp4_144,
            ):
                if res:
                    return res
    payload = getattr(att, "payload", None)
    return getattr(payload, "url", None) if payload else None


def _attachment_sig(att) -> str:
    """Stable identity of a MAX attachment, to tell a replaced file from a kept one.

    The MAX token survives edits of the surrounding message, while urls are
    signed and may be re-issued, so the token is preferred.
    """
    token = getattr(att, "token", None)
    if not token:
        payload = getattr(att, "payload", None)
        token = getattr(payload, "token", None) if payload else None
    if token:
        return f"t:{token}"
    return f"u:{_attachment_url(att) or ''}"


# ====== SAFE SEND (HTML -> plain fallback) ======


async def _safe(build):
    """build(parse_mode) -> coroutine returning a Message (or list).

    Tries HTML, falls back to plain text. Returns whatever the build returns
    so the caller can capture message ids.
    """
    try:
        return await build(TgParseMode.HTML)
    except TelegramBadRequest as e:
        logger.warning("TG send failed (%s); retrying as plain text", e)
        return await build(None)


def _not_modified(e: TelegramAPIError) -> bool:
    return "message is not modified" in str(e).lower()


async def _safe_edit(build: Callable[[Any], Awaitable[Any]]) -> bool:
    """Same HTML -> plain fallback as :func:`_safe`, but for edits.

    Returns True when the destination now holds the new content — including the
    "message is not modified" rejection, which means it already did. False means
    Telegram refused the edit itself and the caller should rebuild the post.
    """
    try:
        await build(TgParseMode.HTML)
        return True
    except TelegramBadRequest as e:
        if _not_modified(e):
            return True
        logger.warning("TG edit (HTML) failed (%s); retrying as plain text", e)
    try:
        await build(None)
        return True
    except TelegramAPIError as e:
        if _not_modified(e):
            return True
        logger.warning("TG edit failed (%s)", e)
        return False


# ====== SENDERS ======

MEDIA_CLS = {
    AttachmentType.IMAGE: InputMediaPhoto,
    AttachmentType.VIDEO: InputMediaVideo,
}

SINGLE_SEND = {
    AttachmentType.IMAGE: ("send_photo", "photo"),
    AttachmentType.VIDEO: ("send_video", "video"),
    AttachmentType.AUDIO: ("send_audio", "audio"),
    AttachmentType.FILE: ("send_document", "document"),
}


async def _send_text(tg_id: int, text: str, reply_to: int | None = None) -> TgMessage | None:
    rp = ReplyParameters(message_id=reply_to) if reply_to else None

    async def build(pm):
        return await tg_bot.send_message(tg_id, text, parse_mode=pm, reply_parameters=rp)

    return await _safe(build)


async def _send_single_media(
    tg_id: int,
    att,
    path: str,
    caption: str | None,
    reply_to: int | None = None,
) -> TgMessage | None:
    method_name, kw = SINGLE_SEND[att.type]
    send = getattr(tg_bot, method_name)
    filename = getattr(att, "filename", None)
    rp = ReplyParameters(message_id=reply_to) if reply_to else None

    async def build(pm):
        file = FSInputFile(path, filename=filename) if filename else FSInputFile(path)
        return await send(tg_id, **{kw: file}, caption=caption, parse_mode=pm, reply_parameters=rp)

    return await _safe(build)


async def _send_media_group(
    tg_id: int,
    items: list,
    caption: str | None,
    reply_to: int | None = None,
) -> list[TgMessage] | None:
    rp = ReplyParameters(message_id=reply_to) if reply_to else None

    async def build(pm):
        media = []
        for i, (att, path) in enumerate(items):
            cls = MEDIA_CLS[att.type]
            media.append(
                cls(
                    media=FSInputFile(path),
                    caption=caption if i == 0 else None,
                    parse_mode=pm,
                )
            )
        return await tg_bot.send_media_group(tg_id, media, reply_parameters=rp)

    return await _safe(build)


# ====== LAYOUT PLAN ======

SUPPORTED = {
    AttachmentType.IMAGE,
    AttachmentType.VIDEO,
    AttachmentType.AUDIO,
    AttachmentType.FILE,
}

TEXT = "text"

KIND_OF = {
    AttachmentType.IMAGE: "photo",
    AttachmentType.VIDEO: "video",
    AttachmentType.AUDIO: "audio",
    AttachmentType.FILE: "document",
}

# Telegram replaces an album item only with one of the same family: an audio
# album takes audio, a document album documents, any other photos and videos.
FAMILY = {"photo": "visual", "video": "visual", "audio": "audio", "document": "document"}

EDIT_MEDIA_CLS = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "audio": InputMediaAudio,
    "document": InputMediaDocument,
}


class _NeedsRebuild(Exception):
    """Telegram refused an in-place edit, so the post has to be re-sent."""


@dataclass(frozen=True)
class Planned:
    """One Telegram message a MAX message is about to be rendered as."""

    kind: str
    sig: str
    url: str | None = None
    att: Any | None = None
    album: bool = False


def plan_layout(message, sender_prefix: str = "") -> tuple[list[Planned], str | None]:
    """Telegram layout of a MAX message: one entry per message that it becomes.

    MAX keeps any number of attachments in a single message; Telegram splits
    them into an album plus one message per audio/file, so the mapping is
    1 -> N. Building it up front (before anything is downloaded) is what lets
    an edit be compared against the layout already in the chat.
    """
    body = getattr(message, "body", None)
    text = None
    attachments = []
    if body:
        text = body.html_text or body.text
        attachments = body.attachments or []
    text = _apply_prefix(sender_prefix, text)

    media: list = []
    files: list = []
    for att in attachments:
        if att.type not in SUPPORTED:
            logger.info("Skipping unsupported MAX attachment %s", att.type)
            continue
        url = _attachment_url(att)
        if not url:
            logger.info("Skipping MAX attachment %s without url", att.type)
            continue
        (media if att.type in (AttachmentType.IMAGE, AttachmentType.VIDEO) else files).append(
            (att, url)
        )

    if not media and not files:
        return ([Planned(kind=TEXT, sig="")] if text else []), text

    album = len(media) >= 2
    items = [
        Planned(kind=KIND_OF[att.type], sig=_attachment_sig(att), url=url, att=att, album=album)
        for att, url in media
    ]
    items += [
        Planned(kind=KIND_OF[att.type], sig=_attachment_sig(att), url=url, att=att, album=False)
        for att, url in files
    ]
    return items, text


# ====== FORWARDING ======


async def _resolve_reply_to(message, tg_id: int) -> int | None:
    """If MAX message replies to another, look up its copy inside `tg_id`."""
    link = getattr(message, "link", None)
    if not link or link.type != MessageLinkType.REPLY:
        return None
    src_mid = link.message.mid if link.message else None
    if not src_mid:
        return None
    max_chat_id = message.recipient.chat_id
    if max_chat_id is None:
        return None
    tg_ref = await message_map.get_tg_in(max_chat_id, src_mid, tg_id)
    if tg_ref is None:
        return None
    return tg_ref.message_id


async def forward_to_tg(
    message,
    tg_id: int,
    sender_prefix: str = "",
    cache: dict[str, str] | None = None,
) -> TgPost:
    """Forward MAX message to TG. Returns the layout it actually produced.

    `cache` maps attachment url -> local path and is owned by the caller when
    given (so one message fanned out to several TG chats downloads once); the
    caller then cleans those files up via :func:`cleanup_cache`.
    """
    own_cache = cache is None
    if cache is None:
        cache = {}

    plan, text = plan_layout(message, sender_prefix)
    reply_to = await _resolve_reply_to(message, tg_id)

    sent: list[TgItem] = []
    caption = text

    try:
        album = [p for p in plan if p.album]
        singles = [p for p in plan if not p.album and p.kind != TEXT]
        has_text_item = any(p.kind == TEXT for p in plan)

        downloaded: list[tuple[Planned, str]] = []
        for p in album:
            path = await _fetch(p, cache)
            if path is not None:
                downloaded.append((p, path))

        try:
            if len(downloaded) >= 2:
                msgs = await _send_media_group(
                    tg_id,
                    [(p.att, path) for p, path in downloaded],
                    caption,
                    reply_to=reply_to,
                )
                if msgs:
                    for i, (msg, (p, _)) in enumerate(zip(msgs, downloaded)):
                        sent.append(
                            TgItem(
                                message_id=msg.message_id,
                                kind=p.kind,
                                sig=p.sig,
                                album=True,
                                caption=i == 0 and caption is not None,
                            )
                        )
                    caption = None
            elif len(downloaded) == 1:
                p, path = downloaded[0]
                msg = await _send_single_media(tg_id, p.att, path, caption, reply_to=reply_to)
                if msg is not None:
                    sent.append(
                        TgItem(
                            message_id=msg.message_id,
                            kind=p.kind,
                            sig=p.sig,
                            caption=caption is not None,
                        )
                    )
                    caption = None
        except TelegramAPIError as e:
            logger.warning("Failed sending MAX media -> TG %d: %s", tg_id, e)

        for p in singles:
            path = await _fetch(p, cache)
            if path is None:
                continue
            try:
                msg = await _send_single_media(tg_id, p.att, path, caption, reply_to=reply_to)
            except TelegramAPIError as e:
                logger.warning("Failed sending MAX attachment -> TG %d: %s", tg_id, e)
                continue
            if msg is not None:
                sent.append(
                    TgItem(
                        message_id=msg.message_id,
                        kind=p.kind,
                        sig=p.sig,
                        caption=caption is not None,
                    )
                )
                caption = None

        # text-only post, or every attachment failed — deliver the text anyway
        if caption and (has_text_item or not sent):
            msg = await _send_text(tg_id, caption, reply_to=reply_to)
            if msg is not None:
                sent.append(TgItem(message_id=msg.message_id, kind=TEXT, caption=True))

        logger.info("Forwarded MAX message -> TG %d (%d messages)", tg_id, len(sent))
    finally:
        if own_cache:
            cleanup_cache(cache)

    return TgPost(items=tuple(sent), text=text)


async def _fetch(planned: Planned, cache: dict[str, str]) -> str | None:
    if not planned.url:
        return None
    try:
        return await _download_cached(planned.url, cache)
    except Exception as e:  # noqa: BLE001 - any download failure just skips the media
        logger.warning("MAX download failed (%s); skipping", e)
        return None


async def _remember(max_chat_id: int, mid: str, tg_id: int, post: TgPost) -> None:
    """Bind every message of a rendering to the MAX message and store its layout."""
    if not post.items:
        return
    refs = [TgRef(chat_id=tg_id, message_id=item.message_id) for item in post.items]
    await message_map.bind_many(refs, MaxRef(chat_id=max_chat_id, mid=mid))
    await message_map.set_tg_post(tg_id, post.items[0].message_id, post)


async def _forget(tg_id: int, post: TgPost) -> None:
    for item in post.items:
        await message_map.forget_tg(tg_id, item.message_id)


# ====== EDIT FORWARDING ======


async def _edit_text(tg_id: int, message_id: int, text: str) -> bool:
    async def build(pm):
        return await tg_bot.edit_message_text(
            chat_id=tg_id,
            message_id=message_id,
            text=text,
            parse_mode=pm,
        )

    return await _safe_edit(build)


async def _edit_caption(tg_id: int, message_id: int, caption: str | None) -> bool:
    async def build(pm):
        return await tg_bot.edit_message_caption(
            chat_id=tg_id,
            message_id=message_id,
            caption=caption,
            parse_mode=pm,
        )

    return await _safe_edit(build)


async def _edit_media(
    tg_id: int,
    message_id: int,
    planned: Planned,
    path: str,
    caption: str | None,
) -> bool:
    cls = EDIT_MEDIA_CLS[planned.kind]
    filename = getattr(planned.att, "filename", None)

    async def build(pm):
        file = FSInputFile(path, filename=filename) if filename else FSInputFile(path)
        return await tg_bot.edit_message_media(
            chat_id=tg_id,
            message_id=message_id,
            media=cls(media=file, caption=caption, parse_mode=pm),
        )

    return await _safe_edit(build)


def _caption_index(post: TgPost) -> int | None:
    for i, item in enumerate(post.items):
        if item.caption:
            return i
    return None


def _patchable(post: TgPost, plan: list[Planned]) -> bool:
    """True if the new layout can be edited over the old one, message by message.

    Telegram cannot add or remove messages from a post, nor turn a text message
    into a media one, so anything but a position-for-position replacement of
    compatible media has to be rebuilt from scratch.
    """
    if len(post.items) != len(plan) or not plan:
        return False
    if _caption_index(post) not in (None, 0):
        return False
    for old, new in zip(post.items, plan):
        if old.kind == TEXT or new.kind == TEXT:
            if old.kind != new.kind:
                return False
            continue
        if old.album != new.album:
            return False
        if old.album and FAMILY[old.kind] != FAMILY[new.kind]:
            return False
    return True


async def _patch_in_place(
    tg_id: int,
    post: TgPost,
    plan: list[Planned],
    text: str | None,
) -> TgPost | None:
    """Apply a same-shape edit to the messages already in the chat.

    Returns the new layout. Raises :class:`_NeedsRebuild` if Telegram refused an
    edit; returns None if the new media could not be fetched, in which case the
    copy is left as it is rather than rebuilt from media we do not have.
    """
    cache: dict[str, str] = {}
    items: list[TgItem] = []
    text_changed = text != post.text
    try:
        for i, (old, new) in enumerate(zip(post.items, plan)):
            carries = i == 0
            caption = text if carries else None

            if new.kind == TEXT:
                if text_changed and text and not await _edit_text(tg_id, old.message_id, text):
                    raise _NeedsRebuild
                items.append(TgItem(message_id=old.message_id, kind=TEXT, caption=True))
                continue

            if new.sig != old.sig:
                path = await _fetch(new, cache)
                if path is None:
                    logger.warning(
                        "Cannot fetch new media for TG (%d, %d); edit skipped",
                        tg_id,
                        old.message_id,
                    )
                    return None
                if not await _edit_media(tg_id, old.message_id, new, path, caption):
                    raise _NeedsRebuild
            elif carries and text_changed:
                if not await _edit_caption(tg_id, old.message_id, text):
                    raise _NeedsRebuild

            items.append(
                TgItem(
                    message_id=old.message_id,
                    kind=new.kind,
                    sig=new.sig,
                    album=new.album,
                    caption=carries and caption is not None,
                )
            )
    finally:
        cleanup_cache(cache)

    return TgPost(items=tuple(items), text=text)


async def _delete_items(tg_id: int, post: TgPost) -> None:
    ids = post.message_ids
    if not ids:
        return
    try:
        await tg_bot.delete_messages(tg_id, ids)
        return
    except TelegramAPIError as e:
        logger.warning("Bulk delete in TG %d failed (%s); deleting one by one", tg_id, e)
    for message_id in ids:
        try:
            await tg_bot.delete_message(tg_id, message_id)
        except TelegramAPIError as e:
            logger.warning("Cannot delete TG (%d, %d): %s", tg_id, message_id, e)


async def _rebuild(
    message,
    tg_id: int,
    sender_prefix: str,
    post: TgPost,
    max_chat_id: int,
    mid: str,
) -> None:
    """Re-send a post whose new shape Telegram cannot reach by editing.

    The new copy goes out first: if it fails, the old one is still there and
    still mapped, so the edit is merely missed instead of the post being lost.
    """
    plan, _ = plan_layout(message, sender_prefix)
    if plan:
        fresh = await forward_to_tg(message, tg_id, sender_prefix=sender_prefix)
        if [item.kind for item in fresh.items] != [p.kind for p in plan]:
            # A media file would not go through, so the re-send came out
            # degraded. Replacing a complete post with a lesser one loses
            # content for good; keeping the old copy only misses this edit.
            logger.warning(
                "Rebuild after MAX edit came out degraded for TG %d (%s instead of %s); "
                "keeping the old copy",
                tg_id,
                [item.kind for item in fresh.items],
                [p.kind for p in plan],
            )
            await _delete_items(tg_id, fresh)
            return
    else:
        # edited down to nothing — drop the copy without replacing it
        fresh = TgPost(items=())

    await _delete_items(tg_id, post)
    await _forget(tg_id, post)
    await _remember(max_chat_id, mid, tg_id, fresh)
    logger.info(
        "Rebuilt MAX %s in TG %d (%d -> %d messages)",
        mid,
        tg_id,
        len(post.items),
        len(fresh.items),
    )


async def forward_edit_to_tg(message, tg_id: int, sender_prefix: str = "") -> None:
    """Propagate a MAX edit to the copy of that message inside `tg_id`."""
    max_chat_id = message.recipient.chat_id
    body = message.body
    if max_chat_id is None or body is None:
        return

    head = await message_map.get_tg_in(max_chat_id, body.mid, tg_id)
    if head is None:
        logger.info(
            "Edit ignored: no TG mapping for MAX (%s, %s) in TG %d",
            max_chat_id,
            body.mid,
            tg_id,
        )
        return

    plan, text = plan_layout(message, sender_prefix)

    post = await message_map.get_tg_post(tg_id, head.message_id)
    if post is None:
        # Mirrored before the layout was recorded (or the record expired): the
        # only safe move is patching the text of the message we do know about.
        await _legacy_patch(tg_id, head.message_id, text, bool(plan) and plan[0].kind != TEXT)
        return

    if _patchable(post, plan):
        try:
            patched = await _patch_in_place(tg_id, post, plan, text)
        except _NeedsRebuild:
            logger.info("In-place edit of MAX %s in TG %d refused; rebuilding", body.mid, tg_id)
        else:
            if patched is not None:
                await message_map.set_tg_post(tg_id, patched.items[0].message_id, patched)
                logger.info("Edited MAX %s in TG %d in place", body.mid, tg_id)
            return

    await _rebuild(message, tg_id, sender_prefix, post, max_chat_id, body.mid)


async def _legacy_patch(tg_id: int, message_id: int, text: str | None, has_media: bool) -> None:
    if not text:
        return
    ok = (
        await _edit_caption(tg_id, message_id, text)
        if has_media
        else await _edit_text(tg_id, message_id, text)
    )
    if ok:
        logger.info("Edited TG (%d, %d) after MAX edit", tg_id, message_id)


# ====== REMOVAL FORWARDING ======


def _is_own_copy_pair(tg_id: int, max_id: int) -> bool:
    """True if `max_id` also receives copies of `tg_id`, so its messages are ambiguous."""
    return (tg_id, max_id) in MIRRORED_PAIRS


async def remove_from_tg(max_chat_id: int, mid: str, tg_id: int) -> None:
    """Delete the TG copy of a MAX message that was removed."""
    head = await message_map.get_tg_in(max_chat_id, mid, tg_id)
    if head is None:
        logger.info(
            "Removal ignored: no TG mapping for MAX (%s, %s) in TG %d",
            max_chat_id,
            mid,
            tg_id,
        )
        return

    post = await message_map.get_tg_post(tg_id, head.message_id)
    if post is None:
        # Without a layout record we cannot prove the TG message is our copy. In
        # a one-way pair it has to be; in a mirrored one it may be the original,
        # and deleting a user's own message would be worse than missing a sync.
        if _is_own_copy_pair(tg_id, max_chat_id):
            logger.warning(
                "Removal of MAX (%s, %s) not propagated: no layout for TG (%d, %d) "
                "in a bidirectional pair — cannot tell the copy from the original",
                max_chat_id,
                mid,
                tg_id,
                head.message_id,
            )
            return
        post = TgPost(items=(TgItem(message_id=head.message_id, kind=TEXT),))

    await _delete_items(tg_id, post)
    await _forget(tg_id, post)
    logger.info("Removed %d TG message(s) in %d after MAX removal", len(post.items), tg_id)


# ====== MAIN HANDLERS ======


def _targets(chat_id: int, sender) -> list[tuple[Route, str]]:
    """Routes leaving this MAX chat that accept `sender`, each with its prefix."""
    out: list[tuple[Route, str]] = []
    for route in MAX_ROUTES.get(chat_id, ()):
        if (
            route.allowed_user_ids is not None
            and sender
            and sender.user_id not in route.allowed_user_ids
        ):
            continue
        out.append((route, build_sign_prefix(sender) if route.sign_names else ""))
    return out


def _is_bot(user_id: int | None) -> bool:
    return bool(user_id and max_bot.me and user_id == max_bot.me.user_id)


@max_router.message_created(F.message.recipient.chat_id.in_(MAX_ROUTES))
async def on_max_message(event: MessageCreated):
    chat_id = event.message.recipient.chat_id
    if not chat_id:
        return

    sender = event.message.sender
    if sender and _is_bot(sender.user_id):
        return  # ignore the bot's own messages

    targets = _targets(chat_id, sender)
    if not targets:
        return

    logger.info(
        "New MAX message in chat %s -> TG ids %s",
        chat_id,
        [route.tg_id for route, _ in targets],
    )

    body = event.message.body
    cache: dict[str, str] = {}
    try:
        for route, sender_prefix in targets:
            post = await forward_to_tg(
                event.message,
                route.tg_id,
                sender_prefix=sender_prefix,
                cache=cache,
            )
            if body is not None:
                await _remember(chat_id, body.mid, route.tg_id, post)
    finally:
        cleanup_cache(cache)


@max_router.message_edited(F.message.recipient.chat_id.in_(MAX_ROUTES))
async def on_max_message_edited(event: MessageEdited):
    chat_id = event.message.recipient.chat_id
    body = event.message.body
    if not chat_id or body is None:
        return

    sender = event.message.sender
    if sender and _is_bot(sender.user_id):
        return

    # In a bidirectional pair this MAX message may itself be a copy of a TG one;
    # editing "back" would fight the TG original instead of following it.
    if await message_map.has_max_post(chat_id, body.mid):
        logger.info("MAX %s is a copy of a TG message; edit not propagated", body.mid)
        return

    async with keyed_lock((chat_id, body.mid)):
        for route, sender_prefix in _targets(chat_id, sender):
            logger.info("MAX message edited in chat %s -> TG %d", chat_id, route.tg_id)
            await forward_edit_to_tg(event.message, route.tg_id, sender_prefix=sender_prefix)


@max_router.message_removed(F.chat_id.in_(MAX_ROUTES))
async def on_max_message_removed(event: MessageRemoved):
    chat_id = event.chat_id
    mid = event.message_id
    if not chat_id or not mid:
        return

    # user_id is whoever performed the removal, so this only covers the bot
    # deleting a MAX message itself; an admin deleting one of the bot's copies is
    # caught by the post state below.
    if _is_bot(event.user_id):
        return

    if await message_map.has_max_post(chat_id, mid):
        logger.info("MAX %s is a copy of a TG message; removal not propagated", mid)
        await message_map.forget_max(chat_id, mid)
        return

    async with keyed_lock((chat_id, mid)):
        for route in MAX_ROUTES.get(chat_id, ()):
            # No user filter here: a message already mirrored should disappear
            # whoever removed it, including an admin who is not on the whitelist.
            logger.info("MAX message removed in chat %s -> TG %d", chat_id, route.tg_id)
            await remove_from_tg(chat_id, mid, route.tg_id)
        await message_map.forget_max(chat_id, mid)
