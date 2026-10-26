"""MAX -> Telegram: editing and removing a mirrored post.

MAX keeps every attachment of a post in one message and lets an edit replace
that list wholesale; Telegram spreads the same post over several messages and
refuses to add or drop any of them. These tests pin down which edits survive as
in-place patches and which have to be re-sent.
"""

import unittest

from maxapi.types import MessageEdited, MessageRemoved

from tests.support import (
    MAX_BOTH,
    MAX_SOURCE,
    TG_BOTH,
    TG_SOURCE_MAX,
    TG_SOURCE_MAX_2,
    FakeMaxBot,
    FakeTgBot,
    fake_map,
    max_audio,
    max_file,
    max_image,
    max_message,
    max_video,
)

from src.handlers import max as handler
from src.storage.message_map import MaxPost, MaxRef, TgItem, TgPost, TgRef


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tg = FakeTgBot()
        self.max = FakeMaxBot()
        self.map = fake_map()
        self._patched = []
        for name, value in (("tg_bot", self.tg), ("max_bot", self.max), ("message_map", self.map)):
            self._patched.append((name, getattr(handler, name)))
            setattr(handler, name, value)

    async def asyncTearDown(self) -> None:
        for name, value in self._patched:
            setattr(handler, name, value)

    async def mirror(self, message, tg_id: int = TG_SOURCE_MAX) -> TgPost:
        """Forward a MAX message and remember the layout, as the handler does."""
        post = await handler.forward_to_tg(message, tg_id)
        await handler._remember(message.recipient.chat_id, message.body.mid, tg_id, post)
        return post

    async def stored(self, head_id: int, tg_id: int = TG_SOURCE_MAX) -> TgPost:
        post = await self.map.get_tg_post(tg_id, head_id)
        assert post is not None
        return post

    async def max_of(self, message_id: int, tg_id: int = TG_SOURCE_MAX) -> MaxRef:
        """The MAX message a mirrored TG message points back at."""
        mx = await self.map.get_max_in(tg_id, message_id, MAX_SOURCE)
        assert mx is not None
        return mx

    async def head_of(self, mid: str, tg_id: int = TG_SOURCE_MAX) -> TgRef:
        """The first TG message a MAX message was rendered into."""
        head = await self.map.get_tg_in(MAX_SOURCE, mid, tg_id)
        assert head is not None
        return head


class PlanLayoutTests(unittest.TestCase):
    def test_text_only(self):
        plan, text = handler.plan_layout(max_message(text="hi"))
        self.assertEqual([p.kind for p in plan], ["text"])
        self.assertEqual(text, "hi")

    def test_empty_message_plans_nothing(self):
        plan, text = handler.plan_layout(max_message())
        self.assertEqual(plan, [])
        self.assertIsNone(text)

    def test_single_photo_is_not_an_album(self):
        plan, _ = handler.plan_layout(max_message(attachments=[max_image("a")]))
        self.assertEqual([(p.kind, p.album) for p in plan], [("photo", False)])

    def test_two_media_make_an_album(self):
        message = max_message(attachments=[max_image("a"), max_video("b")])
        plan, _ = handler.plan_layout(message)
        self.assertEqual([(p.kind, p.album) for p in plan], [("photo", True), ("video", True)])

    def test_files_follow_the_album_as_single_messages(self):
        message = max_message(
            attachments=[
                max_image("a"),
                max_image("b"),
                max_audio("c"),
                max_file("d"),
            ]
        )
        plan, _ = handler.plan_layout(message)
        self.assertEqual(
            [(p.kind, p.album) for p in plan],
            [("photo", True), ("photo", True), ("audio", False), ("document", False)],
        )

    def test_sign_prefix_lands_in_the_text(self):
        plan, text = handler.plan_layout(max_message(text="hi"), "<b>Name</b>:\n")
        self.assertEqual(text, "<b>Name</b>:\nhi")
        self.assertEqual([p.kind for p in plan], ["text"])

    def test_sign_prefix_alone_still_needs_a_message(self):
        plan, text = handler.plan_layout(max_message(), "<b>Name</b>:\n")
        self.assertEqual([p.kind for p in plan], ["text"])
        self.assertEqual(text, "<b>Name</b>")

    def test_signature_is_stable_across_edits(self):
        first, _ = handler.plan_layout(max_message(attachments=[max_image("a")]))
        again, _ = handler.plan_layout(max_message(attachments=[max_image("a")], text="new"))
        self.assertEqual(first[0].sig, again[0].sig)


class PatchableTests(unittest.TestCase):
    def _plan(self, message):
        return handler.plan_layout(message)[0]

    def test_text_to_text(self):
        post = TgPost(items=(TgItem(1, "text", caption=True),), text="old")
        self.assertTrue(handler._patchable(post, self._plan(max_message(text="new"))))

    def test_text_to_media_needs_a_rebuild(self):
        post = TgPost(items=(TgItem(1, "text", caption=True),), text="old")
        plan = self._plan(max_message(text="old", attachments=[max_image("a")]))
        self.assertFalse(handler._patchable(post, plan))

    def test_media_to_text_needs_a_rebuild(self):
        post = TgPost(items=(TgItem(1, "photo", "t:a", caption=True),))
        self.assertFalse(handler._patchable(post, self._plan(max_message(text="x"))))

    def test_replacing_one_photo_is_patchable(self):
        post = TgPost(items=(TgItem(1, "photo", "t:a", caption=True),))
        self.assertTrue(
            handler._patchable(post, self._plan(max_message(attachments=[max_image("b")])))
        )

    def test_photo_to_video_in_a_lone_message_is_patchable(self):
        post = TgPost(items=(TgItem(1, "photo", "t:a", caption=True),))
        self.assertTrue(
            handler._patchable(post, self._plan(max_message(attachments=[max_video("b")])))
        )

    def test_swapping_album_items_is_patchable(self):
        post = TgPost(
            items=(
                TgItem(1, "photo", "t:a", album=True, caption=True),
                TgItem(2, "photo", "t:b", album=True),
            )
        )
        plan = self._plan(max_message(attachments=[max_image("b"), max_image("a")]))
        self.assertTrue(handler._patchable(post, plan))

    def test_adding_a_photo_needs_a_rebuild(self):
        post = TgPost(items=(TgItem(1, "photo", "t:a", caption=True),))
        plan = self._plan(max_message(attachments=[max_image("a"), max_image("b")]))
        self.assertFalse(handler._patchable(post, plan))

    def test_removing_a_photo_needs_a_rebuild(self):
        post = TgPost(
            items=(
                TgItem(1, "photo", "t:a", album=True, caption=True),
                TgItem(2, "photo", "t:b", album=True),
            )
        )
        self.assertFalse(
            handler._patchable(post, self._plan(max_message(attachments=[max_image("a")])))
        )

    def test_album_photo_cannot_become_an_album_document(self):
        post = TgPost(
            items=(
                TgItem(1, "photo", "t:a", album=True, caption=True),
                TgItem(2, "photo", "t:b", album=True),
            )
        )
        plan = [
            handler.Planned(kind="document", sig="t:c", url="u", album=True),
            handler.Planned(kind="document", sig="t:d", url="u", album=True),
        ]
        self.assertFalse(handler._patchable(post, plan))

    def test_album_grouping_must_match(self):
        post = TgPost(
            items=(
                TgItem(1, "photo", "t:a", album=True, caption=True),
                TgItem(2, "audio", "t:b"),
            )
        )
        plan = self._plan(max_message(attachments=[max_image("a"), max_image("b")]))
        self.assertFalse(handler._patchable(post, plan))

    def test_caption_on_a_later_message_needs_a_rebuild(self):
        post = TgPost(
            items=(
                TgItem(1, "photo", "t:a"),
                TgItem(2, "audio", "t:b", caption=True),
            ),
            text="old",
        )
        plan = self._plan(max_message(text="new", attachments=[max_image("a"), max_audio("b")]))
        self.assertFalse(handler._patchable(post, plan))


class ForwardTests(_Base):
    async def test_text_post_records_one_text_message(self):
        post = await self.mirror(max_message(mid="m1", text="hello"))
        self.assertEqual([(i.kind, i.caption) for i in post.items], [("text", True)])
        self.assertEqual(post.text, "hello")
        self.assertEqual(self.tg.names(), ["send_message"])

    async def test_album_plus_file_records_every_message(self):
        message = max_message(
            mid="m2",
            text="cap",
            attachments=[
                max_image("a"),
                max_image("b"),
                max_file("c"),
            ],
        )
        post = await self.mirror(message)
        self.assertEqual([i.kind for i in post.items], ["photo", "photo", "document"])
        self.assertEqual([i.album for i in post.items], [True, True, False])
        self.assertEqual([i.caption for i in post.items], [True, False, False])
        self.assertEqual(self.tg.names(), ["send_media_group", "send_document"])

    async def test_every_message_maps_back_to_the_max_post(self):
        message = max_message(mid="m3", attachments=[max_image("a"), max_image("b")])
        post = await self.mirror(message)
        for item in post.items:
            mx = await self.max_of(item.message_id)
            self.assertEqual(mx.mid, "m3")
        head = await self.head_of("m3")
        self.assertEqual(head.message_id, post.items[0].message_id)


class EditTests(_Base):
    async def test_text_change_edits_text_in_place(self):
        post = await self.mirror(max_message(mid="m1", text="old"))
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(max_message(mid="m1", text="new"), TG_SOURCE_MAX)
        self.assertEqual(self.tg.names(), ["edit_message_text"])
        self.assertEqual(self.tg.calls[0].kwargs["text"], "new")
        self.assertEqual((await self.stored(post.items[0].message_id)).text, "new")

    async def test_unchanged_text_touches_nothing(self):
        await self.mirror(max_message(mid="m1", text="same"))
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(max_message(mid="m1", text="same"), TG_SOURCE_MAX)
        self.assertEqual(self.tg.names(), [])

    async def test_caption_change_edits_only_the_caption(self):
        await self.mirror(max_message(mid="m1", text="old", attachments=[max_image("a")]))
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(
            max_message(mid="m1", text="new", attachments=[max_image("a")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(self.tg.names(), ["edit_message_caption"])
        self.assertEqual(self.tg.calls[0].kwargs["caption"], "new")

    async def test_removing_the_caption_clears_it(self):
        await self.mirror(max_message(mid="m1", text="old", attachments=[max_image("a")]))
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(
            max_message(mid="m1", attachments=[max_image("a")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(self.tg.names(), ["edit_message_caption"])
        self.assertIsNone(self.tg.calls[0].kwargs["caption"])

    async def test_replacing_media_edits_that_message(self):
        post = await self.mirror(max_message(mid="m1", text="cap", attachments=[max_image("a")]))
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(
            max_message(mid="m1", text="cap", attachments=[max_image("z")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(self.tg.names(), ["edit_message_media"])
        self.assertEqual(self.tg.calls[0].kwargs["message_id"], post.items[0].message_id)
        self.assertEqual(self.tg.calls[0].kwargs["media"].caption, "cap")
        stored = await self.stored(post.items[0].message_id)
        self.assertEqual(stored.items[0].sig, "t:z")

    async def test_swapping_album_items_edits_both_in_place(self):
        message = max_message(mid="m1", text="cap", attachments=[max_image("a"), max_image("b")])
        post = await self.mirror(message)
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(
            max_message(mid="m1", text="cap", attachments=[max_image("b"), max_image("a")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(self.tg.names(), ["edit_message_media", "edit_message_media"])
        self.assertEqual(
            [c.kwargs["message_id"] for c in self.tg.calls],
            [post.items[0].message_id, post.items[1].message_id],
        )
        # only the first message keeps the caption
        self.assertEqual(self.tg.calls[0].kwargs["media"].caption, "cap")
        self.assertIsNone(self.tg.calls[1].kwargs["media"].caption)
        stored = await self.stored(post.items[0].message_id)
        self.assertEqual([i.sig for i in stored.items], ["t:b", "t:a"])

    async def test_changing_one_album_item_leaves_the_other_alone(self):
        message = max_message(mid="m1", attachments=[max_image("a"), max_image("b")])
        post = await self.mirror(message)
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(
            max_message(mid="m1", attachments=[max_image("a"), max_image("z")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(self.tg.names(), ["edit_message_media"])
        self.assertEqual(self.tg.calls[0].kwargs["message_id"], post.items[1].message_id)

    async def test_added_media_rebuilds_the_post(self):
        post = await self.mirror(max_message(mid="m1", text="cap", attachments=[max_image("a")]))
        old_ids = post.message_ids
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(
            max_message(mid="m1", text="cap", attachments=[max_image("a"), max_image("b")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(self.tg.names(), ["send_media_group", "delete_messages"])
        self.assertEqual(self.tg.last_delete(), old_ids)

        head = await self.head_of("m1")
        self.assertNotIn(head.message_id, old_ids)
        fresh = await self.stored(head.message_id)
        self.assertEqual([i.kind for i in fresh.items], ["photo", "photo"])
        # the old messages are no longer mapped
        for message_id in old_ids:
            self.assertIsNone(await self.map.get_max_in(TG_SOURCE_MAX, message_id, MAX_SOURCE))

    async def test_removed_media_rebuilds_the_post(self):
        post = await self.mirror(
            max_message(mid="m1", text="cap", attachments=[max_image("a"), max_image("b")])
        )
        old_ids = post.message_ids
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(
            max_message(mid="m1", text="cap", attachments=[max_image("a")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(self.tg.names(), ["send_photo", "delete_messages"])
        self.assertEqual(self.tg.last_delete(), old_ids)

    async def test_media_added_to_a_text_post_rebuilds_it(self):
        post = await self.mirror(max_message(mid="m1", text="hi"))
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(
            max_message(mid="m1", text="hi", attachments=[max_image("a")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(self.tg.names(), ["send_photo", "delete_messages"])
        self.assertEqual(self.tg.last_delete(), post.message_ids)

    async def test_all_media_removed_rebuilds_as_text(self):
        post = await self.mirror(max_message(mid="m1", text="hi", attachments=[max_image("a")]))
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(max_message(mid="m1", text="hi"), TG_SOURCE_MAX)
        self.assertEqual(self.tg.names(), ["send_message", "delete_messages"])
        self.assertEqual(self.tg.last_delete(), post.message_ids)

    async def test_refused_in_place_edit_falls_back_to_a_rebuild(self):
        post = await self.mirror(max_message(mid="m1", text="old", attachments=[max_image("a")]))
        self.tg.calls.clear()
        self.tg.refuse.add("edit_message_caption")
        await handler.forward_edit_to_tg(
            max_message(mid="m1", text="new", attachments=[max_image("a")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(
            self.tg.names(),
            ["edit_message_caption", "edit_message_caption", "send_photo", "delete_messages"],
        )
        self.assertEqual(self.tg.last_delete(), post.message_ids)

    async def test_failed_rebuild_keeps_the_old_copy(self):
        post = await self.mirror(max_message(mid="m1", text="hi"))
        self.tg.calls.clear()
        self.tg.refuse.add("send_photo")
        await handler.forward_edit_to_tg(
            max_message(mid="m1", text="hi", attachments=[max_image("a")]),
            TG_SOURCE_MAX,
        )
        # the degraded re-send is rolled back, the original copy stays mapped
        self.assertEqual(self.tg.last_delete(), [self.tg.next_id])
        head = await self.head_of("m1")
        self.assertEqual(head.message_id, post.items[0].message_id)
        self.assertEqual((await self.stored(head.message_id)).text, "hi")

    async def test_edit_without_a_mapping_is_ignored(self):
        await handler.forward_edit_to_tg(max_message(mid="nope", text="x"), TG_SOURCE_MAX)
        self.assertEqual(self.tg.names(), [])

    async def test_edit_without_a_layout_patches_the_text(self):
        post = await self.mirror(max_message(mid="m1", text="old"))
        await self.map.drop_tg_post(TG_SOURCE_MAX, post.items[0].message_id)
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(max_message(mid="m1", text="new"), TG_SOURCE_MAX)
        self.assertEqual(self.tg.names(), ["edit_message_text"])

    async def test_edit_without_a_layout_patches_the_caption(self):
        post = await self.mirror(max_message(mid="m1", text="old", attachments=[max_image("a")]))
        await self.map.drop_tg_post(TG_SOURCE_MAX, post.items[0].message_id)
        self.tg.calls.clear()
        await handler.forward_edit_to_tg(
            max_message(mid="m1", text="new", attachments=[max_image("a")]),
            TG_SOURCE_MAX,
        )
        self.assertEqual(self.tg.names(), ["edit_message_caption"])


class HandlerGuardTests(_Base):
    async def test_edit_of_our_own_copy_is_not_propagated(self):
        """A MAX message this bot created from TG must not edit the TG original."""
        await self.map.bind(
            TgRef(chat_id=TG_BOTH, message_id=77),
            MaxRef(chat_id=MAX_BOTH, mid="copy"),
        )
        await self.map.set_max_post(MAX_BOTH, "copy", MaxPost(text="hi", caption_tg_id=77))

        await handler.on_max_message_edited(
            MessageEdited(
                timestamp=0,
                message=max_message(chat_id=MAX_BOTH, mid="copy", text="changed"),
            )
        )
        self.assertEqual(self.tg.names(), [])

    async def test_edit_by_the_bot_itself_is_ignored(self):
        await self.mirror(max_message(mid="m1", text="hi"))
        self.tg.calls.clear()
        await handler.on_max_message_edited(
            MessageEdited(
                timestamp=0,
                message=max_message(mid="m1", text="changed", sender_id=self.max.me.user_id),
            )
        )
        self.assertEqual(self.tg.names(), [])

    async def test_edit_handler_fans_out_to_every_target(self):
        first = await self.mirror(max_message(mid="m1", text="hi"))
        second = await self.mirror(max_message(mid="m1", text="hi"), tg_id=TG_SOURCE_MAX_2)
        self.tg.calls.clear()
        await handler.on_max_message_edited(
            MessageEdited(
                timestamp=0,
                message=max_message(mid="m1", text="changed"),
            )
        )
        self.assertEqual(self.tg.names(), ["edit_message_text", "edit_message_text"])
        self.assertEqual(
            [(c.kwargs["chat_id"], c.kwargs["message_id"]) for c in self.tg.calls],
            [
                (TG_SOURCE_MAX, first.items[0].message_id),
                (TG_SOURCE_MAX_2, second.items[0].message_id),
            ],
        )

    async def test_removal_handler_fans_out_to_every_target(self):
        first = await self.mirror(max_message(mid="m1", text="hi"))
        second = await self.mirror(max_message(mid="m1", text="hi"), tg_id=TG_SOURCE_MAX_2)
        self.tg.calls.clear()
        await handler.on_max_message_removed(
            MessageRemoved(message_id="m1", chat_id=MAX_SOURCE, user_id=555, timestamp=0)
        )
        self.assertEqual(self.tg.names(), ["delete_messages", "delete_messages"])
        self.assertEqual(
            [(c.args[0], c.args[1]) for c in self.tg.calls],
            [(TG_SOURCE_MAX, first.message_ids), (TG_SOURCE_MAX_2, second.message_ids)],
        )
        self.assertEqual(await self.map.get_tg_all(MAX_SOURCE, "m1"), [])


class RemovalTests(_Base):
    async def test_removal_deletes_every_message_of_the_post(self):
        post = await self.mirror(
            max_message(mid="m1", text="cap", attachments=[max_image("a"), max_file("b")])
        )
        self.tg.calls.clear()
        await handler.remove_from_tg(MAX_SOURCE, "m1", TG_SOURCE_MAX)
        self.assertEqual(self.tg.last_delete(), post.message_ids)
        for message_id in post.message_ids:
            self.assertIsNone(await self.map.get_max_in(TG_SOURCE_MAX, message_id, MAX_SOURCE))
        self.assertIsNone(await self.map.get_tg_post(TG_SOURCE_MAX, post.items[0].message_id))

    async def test_removal_without_a_mapping_is_ignored(self):
        await handler.remove_from_tg(MAX_SOURCE, "gone", TG_SOURCE_MAX)
        self.assertEqual(self.tg.names(), [])

    async def test_one_way_pair_deletes_even_without_a_layout(self):
        post = await self.mirror(max_message(mid="m1", text="hi"))
        await self.map.drop_tg_post(TG_SOURCE_MAX, post.items[0].message_id)
        self.tg.calls.clear()
        await handler.remove_from_tg(MAX_SOURCE, "m1", TG_SOURCE_MAX)
        self.assertEqual(self.tg.last_delete(), post.message_ids)

    async def test_bidirectional_pair_keeps_an_unidentified_message(self):
        message = max_message(chat_id=MAX_BOTH, mid="m1", text="hi")
        post = await self.mirror(message, tg_id=TG_BOTH)
        await self.map.drop_tg_post(TG_BOTH, post.items[0].message_id)
        self.tg.calls.clear()
        await handler.remove_from_tg(MAX_BOTH, "m1", TG_BOTH)
        self.assertEqual(self.tg.names(), [])

    async def test_removal_of_our_own_copy_is_not_propagated(self):
        """A MAX message this bot created from TG must not delete the TG original."""
        await self.map.bind(
            TgRef(chat_id=TG_BOTH, message_id=77),
            MaxRef(chat_id=MAX_BOTH, mid="copy"),
        )
        await self.map.set_max_post(MAX_BOTH, "copy", MaxPost(text="hi", caption_tg_id=77))

        await handler.on_max_message_removed(
            MessageRemoved(message_id="copy", chat_id=MAX_BOTH, user_id=555, timestamp=0)
        )

        self.assertEqual(self.tg.names(), [])
        self.assertIsNone(await self.map.get_tg_in(MAX_BOTH, "copy", TG_BOTH))

    async def test_removal_handler_deletes_an_original(self):
        post = await self.mirror(max_message(mid="m1", text="hi"))
        self.tg.calls.clear()
        await handler.on_max_message_removed(
            MessageRemoved(message_id="m1", chat_id=MAX_SOURCE, user_id=555, timestamp=0)
        )
        self.assertEqual(self.tg.last_delete(), post.message_ids)

    async def test_removal_by_the_bot_itself_is_ignored(self):
        await self.mirror(max_message(mid="m1", text="hi"))
        self.tg.calls.clear()
        await handler.on_max_message_removed(
            MessageRemoved(
                message_id="m1",
                chat_id=MAX_SOURCE,
                user_id=self.max.me.user_id,
                timestamp=0,
            )
        )
        self.assertEqual(self.tg.names(), [])


if __name__ == "__main__":
    unittest.main()
