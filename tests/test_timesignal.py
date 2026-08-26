"""時報音・読み上げ文言のテスト。"""

from __future__ import annotations

import os
import tempfile
import unittest
import wave

from chime import timesignal
from chime.config import DEFAULT_CONFIG

SETTINGS = DEFAULT_CONFIG["time_signal"]
MIXER = DEFAULT_CONFIG["audio"]["mixer"]


class AnnounceTextTest(unittest.TestCase):
    def test_morning(self):
        self.assertEqual(timesignal.announce_text(10, SETTINGS), "午前10時をお知らせしました。")

    def test_before_noon(self):
        self.assertEqual(timesignal.announce_text(11, SETTINGS), "午前11時をお知らせしました。")

    def test_noon_uses_dedicated_template(self):
        self.assertEqual(timesignal.announce_text(12, SETTINGS), "正午をお知らせしました。")

    def test_afternoon_is_twelve_hour(self):
        self.assertEqual(timesignal.announce_text(13, SETTINGS), "午後1時をお知らせしました。")
        self.assertEqual(timesignal.announce_text(16, SETTINGS), "午後4時をお知らせしました。")
        self.assertEqual(timesignal.announce_text(23, SETTINGS), "午後11時をお知らせしました。")

    def test_midnight(self):
        self.assertEqual(timesignal.announce_text(0, SETTINGS), "午前0時をお知らせしました。")

    def test_noon_template_can_be_disabled(self):
        settings = dict(SETTINGS, use_noon_template=False)
        self.assertEqual(timesignal.announce_text(12, settings), "午後12時をお知らせしました。")

    def test_custom_template(self):
        settings = dict(SETTINGS, announce_template="ただいま{period}{hour}時です。")
        self.assertEqual(timesignal.announce_text(15, settings), "ただいま午後3時です。")

    def test_hour_parts(self):
        self.assertEqual(timesignal.hour_parts(14, SETTINGS),
                         {"period": "午後", "hour": 2, "hour24": 14})


class LeadTimeTest(unittest.TestCase):
    def test_lead_matches_short_pip_section(self):
        # 短音 3 回 × 1000ms = 3 秒後に「ポーン」が鳴る
        self.assertEqual(timesignal.lead_seconds(SETTINGS), 3.0)

    def test_total_includes_long_pip(self):
        self.assertEqual(timesignal.total_seconds(SETTINGS), 4.0)

    def test_lead_follows_configuration(self):
        settings = dict(SETTINGS, short_pip_count=4, pip_interval_ms=500)
        self.assertEqual(timesignal.lead_seconds(settings), 2.0)


class GenerateTest(unittest.TestCase):
    def _generate(self, settings=None, mixer=None):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "sub", "time_signal.wav")
        timesignal.generate_time_signal(path, settings or SETTINGS, mixer or MIXER)
        return path

    def test_creates_wav_with_expected_shape(self):
        path = self._generate()
        with wave.open(path, "rb") as handle:
            self.assertEqual(handle.getnchannels(), 2)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 44100)
            duration = handle.getnframes() / handle.getframerate()
        self.assertAlmostEqual(duration, 4.0, places=3)

    def test_creates_parent_directories(self):
        path = self._generate()
        self.assertTrue(os.path.exists(path))

    def test_mono_mixer_setting(self):
        path = self._generate(mixer=dict(MIXER, channels=1, frequency=22050))
        with wave.open(path, "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getframerate(), 22050)

    def test_long_pip_starts_at_lead_offset(self):
        """短音区間は無音で終わり、長音は lead 秒ちょうどから始まる。"""
        path = self._generate()
        with wave.open(path, "rb") as handle:
            rate, channels = handle.getframerate(), handle.getnchannels()
            frames = handle.readframes(handle.getnframes())

        import struct

        samples = struct.unpack("<{0}h".format(len(frames) // 2), frames)
        left = samples[::channels]
        lead_frame = int(rate * timesignal.lead_seconds(SETTINGS))
        # 長音直前（短音の間の無音部分）は 0
        self.assertEqual(max(abs(v) for v in left[lead_frame - 100:lead_frame]), 0)
        # 長音の中央付近には音がある
        middle = lead_frame + int(rate * 0.5)
        self.assertGreater(max(abs(v) for v in left[middle:middle + 100]), 1000)

    def test_ensure_does_not_regenerate(self):
        path = self._generate()
        before = os.path.getmtime(path)
        os.utime(path, (before - 100, before - 100))
        timesignal.ensure_time_signal(path, SETTINGS, MIXER)
        self.assertEqual(os.path.getmtime(path), before - 100)

    def test_ensure_regenerates_when_forced(self):
        path = self._generate()
        os.remove(path)
        timesignal.ensure_time_signal(path, SETTINGS, MIXER)
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
