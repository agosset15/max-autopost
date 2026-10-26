"""The mapping and the post states stored next to it."""

import unittest

from tests.support import (
    MAX_BOTH,
    MAX_CHANNEL,
    MAX_SOURCE,
    TG_BOTH,
    TG_CHANNEL,
    TG_SOURCE_MAX,
    fake_map,
    fake_redis,
)

from src.config import MIRRORED_PAIRS
from src.locks import keyed_throttle
from src.storage.message_map import MaxAtt, MaxPost, MaxRef, TgItem, TgPost, TgRef


class MappingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.map = fake_map()

    async def test_bind_many_points_every_item_at_the_max_message(self):
        refs = [TgRef(TG_CHANNEL, 5), TgRef(TG_CHANNEL, 6)]
        await self.map.bind_many(refs, MaxRef(MAX_CHANNEL, "m1"))
        for ref in refs:
            mx = await self.map.get_max_in(TG_CHANNEL, ref.message_id, MAX_CHANNEL)
            assert mx is not None
            self.assertEqual(mx.mid, "m1")
        head = await self.map.get_tg_in(MAX_CHANNEL, "m1", TG_CHANNEL)
        assert head is not None
        self.assertEqual(head.message_id, 5)

    async def test_fan_out_keeps_one_counterpart_per_chat(self):
        await self.map.bind(TgRef(TG_CHANNEL, 5), MaxRef(MAX_CHANNEL, "a"))
        await self.map.bind(TgRef(TG_CHANNEL, 5), MaxRef(MAX_SOURCE, "b"))
        in_channel = await self.map.get_max_in(TG_CHANNEL, 5, MAX_CHANNEL)
        in_source = await self.map.get_max_in(TG_CHANNEL, 5, MAX_SOURCE)
        assert in_channel is not None and in_source is not None
        self.assertEqual(in_channel.mid, "a")
        self.assertEqual(in_source.mid, "b")
        self.assertEqual(len(await self.map.get_max_all(TG_CHANNEL, 5)), 2)


class PostStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.map = fake_map()

    async def test_tg_post_round_trip(self):
        post = TgPost(
            items=(
                TgItem(10, "photo", "t:a", album=True, caption=True),
                TgItem(11, "photo", "t:b", album=True),
                TgItem(12, "document", "t:c"),
            ),
            text="cap",
        )
        await self.map.set_tg_post(TG_SOURCE_MAX, 10, post)
        self.assertEqual(await self.map.get_tg_post(TG_SOURCE_MAX, 10), post)
        self.assertEqual(post.message_ids, [10, 11, 12])

    async def test_max_post_round_trip(self):
        post = MaxPost(
            atts=(
                MaxAtt("image", "p1", "tok1", 5),
                MaxAtt("video", "v1", "tok2", 6),
                MaxAtt("file", "", "", None),
            ),
            text="cap",
            caption_tg_id=5,
        )
        await self.map.set_max_post(MAX_CHANNEL, "m1", post)
        self.assertEqual(await self.map.get_max_post(MAX_CHANNEL, "m1"), post)

    async def test_missing_state_reads_as_none(self):
        self.assertIsNone(await self.map.get_tg_post(TG_SOURCE_MAX, 1))
        self.assertIsNone(await self.map.get_max_post(MAX_CHANNEL, "nope"))

    async def test_corrupt_state_reads_as_none(self):
        values = fake_redis(self.map).values
        values[self.map._tg_post_key(TG_SOURCE_MAX, 1)] = "{not json"
        values[self.map._max_post_key(MAX_CHANNEL, "m")] = '{"atts": [{"x": 1}]}'
        self.assertIsNone(await self.map.get_tg_post(TG_SOURCE_MAX, 1))
        self.assertIsNone(await self.map.get_max_post(MAX_CHANNEL, "m"))

    async def test_has_max_post_marks_our_own_copies(self):
        self.assertFalse(await self.map.has_max_post(MAX_CHANNEL, "m1"))
        await self.map.set_max_post(MAX_CHANNEL, "m1", MaxPost(text="x"))
        self.assertTrue(await self.map.has_max_post(MAX_CHANNEL, "m1"))

    async def test_forgetting_a_tg_message_drops_its_state(self):
        await self.map.bind(TgRef(TG_SOURCE_MAX, 10), MaxRef(MAX_SOURCE, "m1"))
        await self.map.set_tg_post(TG_SOURCE_MAX, 10, TgPost(items=(TgItem(10, "text"),)))
        await self.map.forget_tg(TG_SOURCE_MAX, 10)
        self.assertIsNone(await self.map.get_tg_post(TG_SOURCE_MAX, 10))
        self.assertIsNone(await self.map.get_max_in(TG_SOURCE_MAX, 10, MAX_SOURCE))
        self.assertIsNone(await self.map.get_tg_in(MAX_SOURCE, "m1", TG_SOURCE_MAX))

    async def test_forgetting_a_max_message_drops_its_state(self):
        await self.map.bind(TgRef(TG_CHANNEL, 10), MaxRef(MAX_CHANNEL, "m1"))
        await self.map.set_max_post(MAX_CHANNEL, "m1", MaxPost(text="x"))
        await self.map.forget_max(MAX_CHANNEL, "m1")
        self.assertFalse(await self.map.has_max_post(MAX_CHANNEL, "m1"))
        self.assertIsNone(await self.map.get_tg_in(MAX_CHANNEL, "m1", TG_CHANNEL))
        self.assertIsNone(await self.map.get_max_in(TG_CHANNEL, 10, MAX_CHANNEL))


class ThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_second_call_on_a_key_waits(self):
        import time

        throttle = keyed_throttle(0.05)
        await throttle.wait("a")
        started = time.monotonic()
        await throttle.wait("a")
        self.assertGreaterEqual(time.monotonic() - started, 0.04)

    async def test_other_keys_are_not_held_back(self):
        import time

        throttle = keyed_throttle(0.5)
        await throttle.wait("a")
        started = time.monotonic()
        await throttle.wait("b")
        self.assertLess(time.monotonic() - started, 0.2)

    async def test_concurrent_calls_queue_up(self):
        import asyncio
        from itertools import pairwise

        throttle = keyed_throttle(0.02)
        loop = asyncio.get_running_loop()
        stamps: list[float] = []

        async def tick():
            await throttle.wait("a")
            stamps.append(loop.time())

        await asyncio.gather(*(tick() for _ in range(3)))
        gaps = [b - a for a, b in pairwise(stamps)]
        self.assertTrue(all(gap >= 0.015 for gap in gaps), gaps)


class MirroredPairTests(unittest.TestCase):
    def test_bidirectional_group_is_mirrored(self):
        self.assertIn((TG_BOTH, MAX_BOTH), MIRRORED_PAIRS)

    def test_one_way_pairs_are_not(self):
        self.assertNotIn((TG_CHANNEL, MAX_CHANNEL), MIRRORED_PAIRS)
        self.assertNotIn((TG_SOURCE_MAX, MAX_SOURCE), MIRRORED_PAIRS)


if __name__ == "__main__":
    unittest.main()
