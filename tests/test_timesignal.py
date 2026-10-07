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
    # 読み上げエンジンは「4時」を「よんじ」、「7時」を「ななじ」、「9時」を
    # 「きゅうじ」、「0時」を「ぜろじ」と誤読する（正しくは よじ／しちじ／
    # くじ／れいじ）。既定の hour_readings はこの 4 つだけをかな書きに
    # 上書きしており、以下のテストはその読みが文言に反映されることを確認する。
    def test_morning(self):
        self.assertEqual(timesignal.announce_text(10, SETTINGS), "午前10時をお知らせしたのだ。")

    def test_before_noon(self):
        self.assertEqual(timesignal.announce_text(11, SETTINGS), "午前11時をお知らせしたのだ。")

    def test_noon_uses_dedicated_template(self):
        self.assertEqual(timesignal.announce_text(12, SETTINGS), "正午をお知らせしたのだ。")

    def test_afternoon_is_twelve_hour(self):
        self.assertEqual(timesignal.announce_text(13, SETTINGS), "午後1時をお知らせしたのだ。")
        # 16 時（午後4時）は「よんじ」と誤読するため「よじ」に上書きされる。
        self.assertEqual(timesignal.announce_text(16, SETTINGS), "午後よじをお知らせしたのだ。")
        self.assertEqual(timesignal.announce_text(23, SETTINGS), "午後11時をお知らせしたのだ。")

    def test_midnight(self):
        # 0 時は「ぜろじ」と誤読するため「れいじ」に上書きされる。
        self.assertEqual(timesignal.announce_text(0, SETTINGS), "午前れいじをお知らせしたのだ。")

    def test_hour_readings_cover_the_four_misread_hours(self):
        # 誤読する 4 つの時刻を全数確認する。範囲外の 7 時・9 時も
        # docs/SETUP.md の「時報の時間帯を変える」例で踏みうるため対象に含める。
        self.assertEqual(timesignal.announce_text(7, SETTINGS), "午前しちじをお知らせしたのだ。")
        self.assertEqual(timesignal.announce_text(9, SETTINGS), "午前くじをお知らせしたのだ。")
        self.assertEqual(timesignal.announce_text(19, SETTINGS), "午後しちじをお知らせしたのだ。")
        self.assertEqual(timesignal.announce_text(21, SETTINGS), "午後くじをお知らせしたのだ。")

    def test_correctly_read_hours_are_left_as_digits(self):
        # 正しく読める時刻をかな化しないのは意図的（TTS のアクセントが
        # 不自然になるのを避けるため）。数字表記のまま変わらないことを確認する。
        for hour in (1, 2, 3, 5, 6, 8, 10, 11):
            self.assertEqual(timesignal.announce_text(hour, SETTINGS),
                             "午前{0}時をお知らせしたのだ。".format(hour))

    def test_noon_template_can_be_disabled(self):
        settings = dict(SETTINGS, use_noon_template=False)
        self.assertEqual(timesignal.announce_text(12, settings), "午後12時をお知らせしたのだ。")

    def test_custom_template(self):
        # {hour} は後方互換のプレースホルダ（利用者が既存の config.json で
        # テンプレートを書き換えている場合に備え、引き続き数値として使える）。
        settings = dict(SETTINGS, announce_template="ただいま{period}{hour}時です。")
        self.assertEqual(timesignal.announce_text(15, settings), "ただいま午後3時です。")

    def test_hour_readings_can_be_overridden_via_settings(self):
        settings = dict(SETTINGS, hour_readings={"3": "さんじ"})
        self.assertEqual(timesignal.announce_text(15, settings), "午後さんじをお知らせしたのだ。")
        # 既定の 4 つを上書きしなければ、そちらは元のまま数字表記に戻る
        # （hour_readings を丸ごと差し替える設計のため）。
        self.assertEqual(timesignal.announce_text(16, settings), "午後4時をお知らせしたのだ。")

    def test_hour_parts(self):
        self.assertEqual(timesignal.hour_parts(14, SETTINGS),
                         {"period": "午後", "hour": 2, "hour24": 14, "hour_reading": "2時"})

    def test_hour_parts_uses_kana_reading_for_misread_hours(self):
        self.assertEqual(timesignal.hour_parts(16, SETTINGS),
                         {"period": "午後", "hour": 4, "hour24": 16, "hour_reading": "よじ"})

    def test_hour_parts_covers_every_hour_of_the_day(self):
        # 12 時間表記への変換を全 24 時で固定する。境界は 0 時（午前の 0。12 にはしない）、
        # 11 時と 12 時（午前と午後の切り替え）、13 時（午後の 1）。
        for hour in range(24):
            with self.subTest(hour=hour):
                parts = timesignal.hour_parts(hour, SETTINGS)
                self.assertEqual(parts["period"], "午前" if hour < 12 else "午後")
                expected_hour12 = hour if hour <= 12 else hour - 12
                self.assertEqual(parts["hour"], expected_hour12)
                self.assertEqual(parts["hour24"], hour)

    def test_hour_parts_wraps_hours_outside_a_day(self):
        self.assertEqual(timesignal.hour_parts(24, SETTINGS), timesignal.hour_parts(0, SETTINGS))
        self.assertEqual(timesignal.hour_parts(25, SETTINGS), timesignal.hour_parts(1, SETTINGS))
        self.assertEqual(timesignal.hour_parts(-1, SETTINGS), timesignal.hour_parts(23, SETTINGS))

    def test_period_names_can_be_overridden(self):
        settings = dict(SETTINGS, period_am="AM", period_pm="PM")
        self.assertEqual(timesignal.hour_parts(0, settings)["period"], "AM")
        self.assertEqual(timesignal.hour_parts(11, settings)["period"], "AM")
        self.assertEqual(timesignal.hour_parts(12, settings)["period"], "PM")
        self.assertEqual(timesignal.hour_parts(23, settings)["period"], "PM")

    def test_period_names_default_without_settings(self):
        self.assertEqual(timesignal.hour_parts(9, {})["period"], "午前")
        self.assertEqual(timesignal.hour_parts(15, {})["period"], "午後")
        self.assertEqual(timesignal.hour_parts(15, {})["hour_reading"], "3時")

    def test_announce_text_for_every_hour_with_the_default_settings(self):
        expected = [
            "午前れいじをお知らせしたのだ。", "午前1時をお知らせしたのだ。",
            "午前2時をお知らせしたのだ。", "午前3時をお知らせしたのだ。",
            "午前よじをお知らせしたのだ。", "午前5時をお知らせしたのだ。",
            "午前6時をお知らせしたのだ。", "午前しちじをお知らせしたのだ。",
            "午前8時をお知らせしたのだ。", "午前くじをお知らせしたのだ。",
            "午前10時をお知らせしたのだ。", "午前11時をお知らせしたのだ。",
            "正午をお知らせしたのだ。", "午後1時をお知らせしたのだ。",
            "午後2時をお知らせしたのだ。", "午後3時をお知らせしたのだ。",
            "午後よじをお知らせしたのだ。", "午後5時をお知らせしたのだ。",
            "午後6時をお知らせしたのだ。", "午後しちじをお知らせしたのだ。",
            "午後8時をお知らせしたのだ。", "午後くじをお知らせしたのだ。",
            "午後10時をお知らせしたのだ。", "午後11時をお知らせしたのだ。",
        ]
        self.assertEqual([timesignal.announce_text(hour, SETTINGS) for hour in range(24)],
                         expected)


class LeadTimeTest(unittest.TestCase):
    def test_lead_matches_short_pip_section(self):
        # 短音 3 回 × 1000ms = 3 秒後に「ポーン」が鳴る
        self.assertEqual(timesignal.lead_seconds(SETTINGS), 3.0)

    def test_lead_follows_configuration(self):
        settings = dict(SETTINGS, short_pip_count=4, pip_interval_ms=500)
        self.assertEqual(timesignal.lead_seconds(settings), 2.0)

    def test_lead_without_settings_uses_the_defaults(self):
        # 短音 3 回 × 1000ms。generate_time_signal も同じ既定値で合成する。
        self.assertEqual(timesignal.lead_seconds({}), 3.0)


class GenerateTest(unittest.TestCase):
    def _generate(self, settings=None, mixer=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "sub", "time_signal.wav")
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

    def test_mono_8khz_has_the_expected_length_and_size(self):
        # 4 秒 × 8000Hz × 1ch × 2 バイト ＋ WAV ヘッダ 44 バイト。
        path = self._generate(mixer=dict(MIXER, channels=1, frequency=8000))
        with wave.open(path, "rb") as handle:
            self.assertEqual(handle.getnframes(), 32000)
        self.assertEqual(os.path.getsize(path), 44 + 32000 * 2)

    def test_three_or_more_channels_become_stereo(self):
        path = self._generate(mixer=dict(MIXER, channels=3))
        with wave.open(path, "rb") as handle:
            self.assertEqual(handle.getnchannels(), 2)

    def test_every_channel_carries_the_same_samples(self):
        import struct

        path = self._generate()
        with wave.open(path, "rb") as handle:
            frames = handle.readframes(handle.getnframes())
        samples = struct.unpack("<{0}h".format(len(frames) // 2), frames)
        self.assertEqual(samples[0::2], samples[1::2])

    def test_missing_mixer_and_settings_use_the_defaults(self):
        path = self._generate(settings={"volume": 0.6}, mixer={"channels": 2})
        with wave.open(path, "rb") as handle:
            self.assertEqual(handle.getframerate(), 44100)
            duration = handle.getnframes() / handle.getframerate()
        self.assertAlmostEqual(duration, 4.0, places=3)

    def test_a_bare_file_name_is_written_to_the_current_directory(self):
        # ディレクトリを含まないパス（"time_signal.wav" だけ）でも作れること。
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(directory.name)
        returned = timesignal.generate_time_signal("time_signal.wav", SETTINGS, MIXER)
        self.assertEqual(returned, "time_signal.wav")
        self.assertTrue(os.path.exists(os.path.join(directory.name, "time_signal.wav")))

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

    def test_long_pip_follows_the_configured_timing(self):
        """短音の回数・間隔を変えても、長音は lead_seconds ちょうどから始まる。

        WAV の合成と lead_seconds が同じ設定の読み方をしていないと、長音が
        正時からずれる（再生は「正時 − lead_seconds」に始めるため）。
        """
        import struct

        settings = dict(SETTINGS, short_pip_count=2, pip_interval_ms=500)
        path = self._generate(settings=settings)
        with wave.open(path, "rb") as handle:
            rate, channels = handle.getframerate(), handle.getnchannels()
            frames = handle.readframes(handle.getnframes())
            total = handle.getnframes()
        samples = struct.unpack("<{0}h".format(len(frames) // 2), frames)
        left = samples[::channels]
        lead_frame = int(rate * timesignal.lead_seconds(settings))
        self.assertEqual(lead_frame, rate)  # 2 回 × 500ms = 1 秒
        self.assertEqual(max(abs(v) for v in left[lead_frame - 100:lead_frame]), 0)
        middle = lead_frame + int(rate * 0.5)
        self.assertGreater(max(abs(v) for v in left[middle:middle + 100]), 1000)
        # 全体の長さ ＝ 短音区間 ＋ 長音（1 秒）
        self.assertEqual(total, lead_frame + rate)

    def test_ensure_does_not_regenerate(self):
        path = self._generate()
        before = os.path.getmtime(path)
        os.utime(path, (before - 100, before - 100))
        timesignal.ensure_time_signal(path, SETTINGS, MIXER)
        self.assertEqual(os.path.getmtime(path), before - 100)

    def test_ensure_generates_when_the_file_is_missing(self):
        path = self._generate()
        os.remove(path)
        timesignal.ensure_time_signal(path, SETTINGS, MIXER)
        self.assertTrue(os.path.exists(path))

    def test_ensure_force_regenerates_an_existing_file(self):
        path = self._generate()
        old = os.path.getmtime(path) - 100
        os.utime(path, (old, old))

        # force なしでは作り直さない（更新時刻が古いまま）。
        timesignal.ensure_time_signal(path, SETTINGS, MIXER)
        self.assertEqual(os.path.getmtime(path), old)

        # force=True なら、あるファイルでも作り直す（更新時刻が新しくなる）。
        returned = timesignal.ensure_time_signal(path, SETTINGS, MIXER, force=True)
        self.assertEqual(returned, path)
        self.assertGreater(os.path.getmtime(path), old)

    def test_ensure_force_replaces_the_contents(self):
        path = self._generate()
        with open(path, "wb") as handle:
            handle.write(b"not a wav")
        timesignal.ensure_time_signal(path, SETTINGS, MIXER)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"not a wav")
        timesignal.ensure_time_signal(path, SETTINGS, MIXER, force=True)
        with wave.open(path, "rb") as handle:
            self.assertEqual(handle.getframerate(), 44100)


if __name__ == "__main__":
    unittest.main()
