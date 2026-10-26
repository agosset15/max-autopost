"""Telegram -> MAX: editing a mirrored post.

A MAX edit replaces the whole message body, so every edit has to restate the
attachments it keeps — passing none clears them. Telegram, on the other hand,
never changes how many media a message holds, so the attachment list only ever
has one of its items swapped for another file.
"""

import asyncio
import unittest
from unittest import mock

from maxapi.types import InputMedia
from maxapi.types.attachments.upload import AttachmentUpload

from tests.support import (
    MAX_CHANNEL,
    TG_CHANNEL,
    FakeMaxBot,
    FakeTgBot,
    fake_map,
    tg_message,
)

from src.config import TG_CHANNEL_ROUTES
from src.handlers import telegram as handler
from src.locks import keyed_throttle
from src.storage.message_map import MaxPost


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tg = FakeTgBot()
        self.max = FakeMaxBot()
        self.map = fake_map()
        self._patched = []
        for name, value in (("tg_bot", self.tg), ("max_bot", self.max), ("message_map", self.map)):
            self._patched.append((name, getattr(handler, name)))
            setattr(handler, name, value)
        self.targets = [(TG_CHANNEL_ROUTES[TG_CHANNEL][0], "")]
        self._throttle = handler._edit_throttle
        handler._edit_throttle = keyed_throttle(0)

    async def asyncTearDown(self) -> None:
        for name, value in self._patched:
            setattr(handler, name, value)
        handler._edit_throttle = self._throttle
        handler.media_groups.clear()
        handler.media_group_targets.clear()

    async def send(self, message) -> str:
        await handler.forward_one_to_max(message, MAX_CHANNEL)
        return self.max.last_mid

    async def send_album(self, messages) -> str:
        group_id = messages[0].media_group_id
        handler.media_groups[group_id] = list(messages)
        handler.media_group_targets[group_id] = self.targets
        with mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()):
            await handler.send_media_group(group_id)
        return self.max.last_mid

    async def edit(self, message) -> None:
        await handler.forward_edit_to_max(message, self.targets)

    async def post(self, mid: str) -> MaxPost:
        """The MAX post state recorded for a mirrored message."""
        post = await self.map.get_max_post(MAX_CHANNEL, mid)
        assert post is not None
        return post

    def edit_kwargs(self) -> dict:
        return self.max.last("edit_message").kwargs

    def sent_attachments(self) -> list:
        return self.edit_kwargs()["attachments"]


class EditedTextTests(unittest.TestCase):
    def test_own_caption_wins(self):
        message = tg_message(message_id=5, photo="p", caption="new")
        post = MaxPost(text="old", caption_tg_id=5)
        self.assertEqual(handler._edited_text(message, post, ""), ("new", 5))

    def test_caption_removed_from_a_lone_message_clears_the_text(self):
        message = tg_message(message_id=5, photo="p")
        post = MaxPost(text="old", caption_tg_id=5)
        self.assertEqual(handler._edited_text(message, post, ""), ("", None))

    def test_caption_removed_from_the_album_item_that_owned_it(self):
        message = tg_message(message_id=5, photo="p", media_group_id="g")
        post = MaxPost(text="old", caption_tg_id=5)
        self.assertEqual(handler._edited_text(message, post, ""), ("", None))

    def test_album_item_without_a_caption_keeps_the_text(self):
        message = tg_message(message_id=6, photo="p", media_group_id="g")
        post = MaxPost(text="old", caption_tg_id=5)
        self.assertEqual(handler._edited_text(message, post, ""), ("old", 5))

    def test_album_item_without_a_caption_or_a_layout_leaves_the_text_unknown(self):
        message = tg_message(message_id=6, photo="p", media_group_id="g")
        self.assertEqual(handler._edited_text(message, None, ""), (None, None))

    def test_lone_message_without_a_layout_still_clears_its_caption(self):
        message = tg_message(message_id=6, photo="p")
        self.assertEqual(handler._edited_text(message, None, ""), ("", None))

    def test_caption_moved_to_another_album_item(self):
        message = tg_message(message_id=6, photo="p", caption="moved", media_group_id="g")
        post = MaxPost(text="old", caption_tg_id=5)
        self.assertEqual(handler._edited_text(message, post, ""), ("moved", 6))

    def test_sign_prefix_is_reapplied(self):
        message = tg_message(message_id=5, text="new")
        post = MaxPost(text="<b>N</b>:\nold", caption_tg_id=5)
        self.assertEqual(handler._edited_text(message, post, "<b>N</b>:\n"), ("<b>N</b>:\nnew", 5))


class ForwardTests(_Base):
    async def test_text_message_records_its_text(self):
        mid = await self.send(tg_message(message_id=5, text="hello"))
        post = await self.post(mid)
        self.assertEqual(post.text, "hello")
        self.assertEqual(post.atts, ())
        self.assertEqual(post.caption_tg_id, 5)

    async def test_photo_records_the_max_token(self):
        mid = await self.send(tg_message(message_id=5, photo="p1", caption="cap"))
        post = await self.post(mid)
        self.assertEqual(
            [(a.kind, a.sig, a.tg_message_id) for a in post.atts], [("image", "p1", 5)]
        )
        self.assertTrue(post.atts[0].token)

    async def test_album_records_one_attachment_per_item(self):
        messages = [
            tg_message(message_id=5, photo="p1", caption="cap", media_group_id="g"),
            tg_message(message_id=6, photo="p2", media_group_id="g"),
            tg_message(message_id=7, video="v1", media_group_id="g"),
        ]
        mid = await self.send_album(messages)
        post = await self.post(mid)
        self.assertEqual(
            [(a.kind, a.sig, a.tg_message_id) for a in post.atts],
            [("image", "p1", 5), ("image", "p2", 6), ("video", "v1", 7)],
        )
        self.assertEqual(post.caption_tg_id, 5)
        self.assertEqual(post.text, "cap")

    async def test_album_skips_an_item_it_cannot_download(self):
        messages = [
            tg_message(message_id=5, photo="p1", caption="cap", media_group_id="g"),
            tg_message(message_id=6, photo="p2", media_group_id="g"),
        ]
        oversized = messages[1].photo
        assert oversized is not None
        oversized[0].__dict__["file_size"] = handler.TG_FILE_SIZE_LIMIT + 1
        mid = await self.send_album(messages)
        post = await self.post(mid)
        self.assertEqual([a.tg_message_id for a in post.atts], [5])


class EditTests(_Base):
    async def test_text_edit_updates_the_text(self):
        mid = await self.send(tg_message(message_id=5, text="old"))
        await self.edit(tg_message(message_id=5, text="new"))
        self.assertEqual(self.edit_kwargs()["message_id"], mid)
        self.assertEqual(self.edit_kwargs()["text"], "new")
        self.assertEqual(self.sent_attachments(), [])
        post = await self.post(mid)
        self.assertEqual(post.text, "new")

    async def test_caption_edit_keeps_the_media_by_token(self):
        mid = await self.send(tg_message(message_id=5, photo="p1", caption="old"))
        stored = (await self.post(mid)).atts[0].token
        self.tg.calls.clear()

        await self.edit(tg_message(message_id=5, photo="p1", caption="new"))

        attachments = self.sent_attachments()
        self.assertEqual(len(attachments), 1)
        self.assertIsInstance(attachments[0], AttachmentUpload)
        self.assertEqual(attachments[0].payload.token, stored)
        self.assertEqual(self.edit_kwargs()["text"], "new")
        # nothing was downloaded from Telegram again
        self.assertEqual(self.tg.names(), [])

    async def test_replaced_photo_is_uploaded_again(self):
        mid = await self.send(tg_message(message_id=5, photo="p1", caption="cap"))
        await self.edit(tg_message(message_id=5, photo="p2", caption="cap"))

        attachments = self.sent_attachments()
        self.assertEqual(len(attachments), 1)
        self.assertIsInstance(attachments[0], InputMedia)
        post = await self.post(mid)
        self.assertEqual(post.atts[0].sig, "p2")
        # the token of the new file was read back for the next edit
        self.assertTrue(post.atts[0].token)
        self.assertIn("get_message", self.max.names())

    async def test_photo_replaced_by_a_video_changes_the_attachment_type(self):
        mid = await self.send(tg_message(message_id=5, photo="p1", caption="cap"))
        await self.edit(tg_message(message_id=5, video="v1", caption="cap"))
        post = await self.post(mid)
        self.assertEqual([(a.kind, a.sig) for a in post.atts], [("video", "v1")])

    async def test_undownloadable_replacement_keeps_the_old_media(self):
        mid = await self.send(tg_message(message_id=5, photo="p1", caption="cap"))
        stored = (await self.post(mid)).atts[0].token

        edited = tg_message(message_id=5, photo="p2", caption="cap")
        assert edited.photo is not None
        edited.photo[0].__dict__["file_size"] = handler.TG_FILE_SIZE_LIMIT + 1
        await self.edit(edited)

        attachments = self.sent_attachments()
        self.assertIsInstance(attachments[0], AttachmentUpload)
        self.assertEqual(attachments[0].payload.token, stored)

    async def test_album_caption_edit_keeps_every_attachment(self):
        messages = [
            tg_message(message_id=5, photo="p1", caption="old", media_group_id="g"),
            tg_message(message_id=6, photo="p2", media_group_id="g"),
        ]
        mid = await self.send_album(messages)
        tokens = [a.token for a in (await self.post(mid)).atts]
        self.tg.calls.clear()

        await self.edit(tg_message(message_id=5, photo="p1", caption="new", media_group_id="g"))

        attachments = self.sent_attachments()
        self.assertEqual([a.payload.token for a in attachments], tokens)
        self.assertEqual(self.edit_kwargs()["text"], "new")
        self.assertEqual(self.tg.names(), [])

    async def test_album_item_media_edit_replaces_only_that_item(self):
        messages = [
            tg_message(message_id=5, photo="p1", caption="cap", media_group_id="g"),
            tg_message(message_id=6, photo="p2", media_group_id="g"),
            tg_message(message_id=7, photo="p3", media_group_id="g"),
        ]
        mid = await self.send_album(messages)
        tokens = [a.token for a in (await self.post(mid)).atts]

        await self.edit(tg_message(message_id=6, photo="p9", media_group_id="g"))

        attachments = self.sent_attachments()
        self.assertEqual(len(attachments), 3)
        self.assertEqual(attachments[0].payload.token, tokens[0])
        self.assertIsInstance(attachments[1], InputMedia)
        self.assertEqual(attachments[2].payload.token, tokens[2])
        # the text of the album, owned by another item, is left alone
        self.assertEqual(self.edit_kwargs()["text"], "cap")

        post = await self.post(mid)
        self.assertEqual([a.sig for a in post.atts], ["p1", "p9", "p3"])
        self.assertEqual(post.caption_tg_id, 5)
        self.assertTrue(all(a.token for a in post.atts))

    async def test_album_item_edit_without_a_caption_does_not_clear_the_text(self):
        messages = [
            tg_message(message_id=5, photo="p1", caption="cap", media_group_id="g"),
            tg_message(message_id=6, photo="p2", media_group_id="g"),
        ]
        mid = await self.send_album(messages)
        await self.edit(tg_message(message_id=6, photo="p2", media_group_id="g"))
        self.assertEqual(self.edit_kwargs()["text"], "cap")
        self.assertEqual((await self.post(mid)).text, "cap")

    async def test_album_caption_removal_clears_the_text(self):
        messages = [
            tg_message(message_id=5, photo="p1", caption="cap", media_group_id="g"),
            tg_message(message_id=6, photo="p2", media_group_id="g"),
        ]
        mid = await self.send_album(messages)
        await self.edit(tg_message(message_id=5, photo="p1", media_group_id="g"))
        self.assertEqual(self.edit_kwargs()["text"], "")
        post = await self.post(mid)
        self.assertEqual(post.text, "")
        self.assertIsNone(post.caption_tg_id)
        self.assertEqual(len(post.atts), 2)

    async def test_edit_without_a_mapping_is_ignored(self):
        await self.edit(tg_message(message_id=99, text="x"))
        self.assertNotIn("edit_message", self.max.names())

    async def test_edit_without_a_layout_restates_the_media_from_max(self):
        mid = await self.send(tg_message(message_id=5, photo="p1", caption="old"))
        stored = (await self.post(mid)).atts[0].token
        await self.map.drop_max_post(MAX_CHANNEL, mid)

        # an album item: its position in the attachment list is unknown, so the
        # media is restated as MAX holds it and only the text is updated
        await self.edit(tg_message(message_id=5, photo="p1", caption="new", media_group_id="g"))

        attachments = self.sent_attachments()
        self.assertEqual([a.payload.token for a in attachments], [stored])
        self.assertEqual(self.edit_kwargs()["text"], "new")
        post = await self.post(mid)
        self.assertEqual(post.text, "new")

    async def test_edit_without_a_layout_keeps_the_album_text(self):
        messages = [
            tg_message(message_id=5, photo="p1", caption="cap", media_group_id="g"),
            tg_message(message_id=6, photo="p2", media_group_id="g"),
        ]
        mid = await self.send_album(messages)
        await self.map.drop_max_post(MAX_CHANNEL, mid)

        await self.edit(tg_message(message_id=6, photo="p2", media_group_id="g"))

        self.assertEqual(self.edit_kwargs()["text"], "cap")
        self.assertEqual(len(self.sent_attachments()), 2)
        self.assertEqual((await self.post(mid)).text, "cap")

    async def test_edit_without_a_layout_reuploads_a_lone_message(self):
        mid = await self.send(tg_message(message_id=5, photo="p1", caption="old"))
        await self.map.drop_max_post(MAX_CHANNEL, mid)

        await self.edit(tg_message(message_id=5, photo="p2", caption="new"))

        self.assertIsInstance(self.sent_attachments()[0], InputMedia)
        post = await self.post(mid)
        self.assertEqual([(a.kind, a.sig) for a in post.atts], [("image", "p2")])

    async def test_html_rejection_retries_as_plain_text(self):
        mid = await self.send(tg_message(message_id=5, text="old"))
        self.max.edit_fails = 1
        await self.edit(tg_message(message_id=5, text="new"))
        edits = [c for c in self.max.calls if c.name == "edit_message"]
        self.assertEqual(len(edits), 2)
        self.assertIsNone(edits[1].kwargs["parse_mode"])
        self.assertEqual((await self.post(mid)).text, "new")

    async def test_failed_edit_leaves_the_recorded_state_alone(self):
        mid = await self.send(tg_message(message_id=5, text="old"))
        self.max.edit_fails = 2
        await self.edit(tg_message(message_id=5, text="new"))
        self.assertEqual((await self.post(mid)).text, "old")


if __name__ == "__main__":
    unittest.main()
