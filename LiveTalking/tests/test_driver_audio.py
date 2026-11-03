import unittest
from queue import Queue
from types import SimpleNamespace

import numpy as np

from avatars.base_avatar import BaseAvatar
from tts.base_tts import BaseTTS, State


class DriverAudioTestCase(unittest.TestCase):
    def make_avatar(self, chunk=4):
        avatar = BaseAvatar.__new__(BaseAvatar)
        avatar.chunk = chunk
        avatar._talk_generation = 0
        avatar.frames = []
        avatar.put_audio_frame = lambda frame, event, generation=None: avatar.frames.append(
            (np.asarray(frame), dict(event))
        ) or True
        return avatar

    def test_short_audio_keeps_start_and_end_events(self):
        avatar = self.make_avatar()

        frame_count = avatar._put_audio_stream(
            np.array([0.1, 0.2], dtype=np.float32),
            {"source": "upload"},
        )

        self.assertEqual(frame_count, 1)
        self.assertEqual(len(avatar.frames), 2)
        self.assertEqual(avatar.frames[0][0].shape, (4,))
        self.assertEqual(avatar.frames[0][1]["status"], "start")
        self.assertEqual(avatar.frames[1][1]["status"], "end")
        np.testing.assert_array_equal(avatar.frames[1][0], np.zeros(4))

    def test_trailing_audio_is_padded_and_closed(self):
        avatar = self.make_avatar()

        frame_count = avatar._put_audio_stream(
            np.arange(6, dtype=np.float32),
            {"source": "upload"},
        )

        self.assertEqual(frame_count, 2)
        self.assertEqual(len(avatar.frames), 2)
        self.assertEqual(avatar.frames[0][1]["status"], "start")
        self.assertEqual(avatar.frames[1][1]["status"], "end")
        np.testing.assert_array_equal(
            avatar.frames[1][0], np.array([4, 5, 0, 0], dtype=np.float32)
        )

    def test_flush_talk_drains_queue_without_touching_queue_internals(self):
        tts = BaseTTS.__new__(BaseTTS)
        tts.msgqueue = Queue()
        tts.msgqueue.put(("one", {}))
        tts.msgqueue.put(("two", {}))
        tts.state = State.RUNNING
        tts._fallback_tts = None
        tts._generation = 0
        tts._active_generation = 0

        tts.flush_talk()

        self.assertTrue(tts.msgqueue.empty())
        self.assertEqual(tts.state, State.PAUSE)
        self.assertEqual(tts._generation, 1)

    def test_generation_marks_inflight_synthesis_cancelled(self):
        tts = BaseTTS.__new__(BaseTTS)
        tts.msgqueue = Queue()
        tts.state = State.RUNNING
        tts._fallback_tts = None
        tts._generation = 0
        tts._active_generation = 0

        tts.flush_talk()

        self.assertTrue(tts.is_cancelled())

    def test_avatar_rejects_audio_from_previous_talk_generation(self):
        avatar = BaseAvatar.__new__(BaseAvatar)
        avatar._talk_generation = 0
        avatar.res_frame_queue = Queue()
        avatar.custom_audiotype = 0
        avatar.asr = SimpleNamespace(
            put_audio_frame=lambda frame, event, generation: generation,
        )

        old_generation = avatar._talk_generation
        avatar.flush_talk()

        self.assertFalse(
            avatar.put_audio_frame(
                np.zeros(4, dtype=np.float32),
                {},
                old_generation,
            )
        )
        self.assertEqual(
            avatar.put_audio_frame(
                np.zeros(4, dtype=np.float32),
                {},
                avatar._talk_generation,
            ),
            avatar._talk_generation,
        )

    def test_asr_flush_invalidates_queued_audio(self):
        from avatars.audio_features.base_asr import BaseASR

        asr = BaseASR.__new__(BaseASR)
        asr.chunk = 4
        asr.parent = None
        asr.queue = Queue()
        asr.output_queue = Queue()
        asr.feat_queue = Queue(maxsize=2)
        asr.frames = [np.ones(4, dtype=np.float32)]
        asr._generation = 0

        asr.put_audio_frame(np.ones(4, dtype=np.float32), {}, 0)
        asr.flush_talk()
        self.assertTrue(asr.queue.empty())
        current = asr.get_audio_frame()
        self.assertEqual(current.type, 1)
        self.assertEqual(current.generation, 1)

    def test_tts_stream_passes_talk_generation_to_avatar(self):
        calls = []
        parent = SimpleNamespace(
            _talk_generation=3,
            put_audio_frame=lambda frame, event, generation: calls.append(generation) or True,
        )
        tts = BaseTTS.__new__(BaseTTS)
        tts.parent = parent
        tts.chunk = 4
        tts.state = State.RUNNING
        tts._generation = 0
        tts._active_generation = 0

        tts._stream_to_avatar(np.ones(4, dtype=np.float32), "ok", {})

        self.assertEqual(calls, [3, 3])


if __name__ == "__main__":
    unittest.main()
