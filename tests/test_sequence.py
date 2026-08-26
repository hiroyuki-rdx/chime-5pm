"""再生シーケンス組み立てのテスト。"""

from __future__ import annotations

import os
import random
import tempfile
import unittest
from datetime import date

from tests.support import REPO_ROOT  # noqa: F401

from chime.config import DEFAULT_CONFIG, Config
from chime.quotes import QuotePicker
from chime.sequence import EXTRA_QUOTE, EXTRA_WEATHER, SequenceBuilder, choose_extra
from chime.state import State
from chime.tts import TTSError
from chime.weather import WeatherError

EXTRA = DEFAULT_CONFIG["extra_segment"]


class FixedRandom(random.Random):
    """``random()`` が常に決まった値を返す乱数。"""

    def __init__(self, value):
        super().__init__(0)
        self.value = value

    def random(self):
        return self.value


class ChooseExtraTest(unittest.TestCase):
    def test_disabled_returns_none(self):
        self.assertIsNone(choose_extra(11, dict(EXTRA, enabled=False), FixedRandom(0.0)))

    def test_always_weather_hours_win(self):
        settings = dict(EXTRA, always_weather_hours=[10], weather_probability=0.0)
        self.assertEqual(choose_extra(10, settings, FixedRandom(0.99)), EXTRA_WEATHER)

    def test_always_quote_hours_win(self):
        settings = dict(EXTRA, always_quote_hours=[15], weather_probability=1.0)
        self.assertEqual(choose_extra(15, settings, FixedRandom(0.0)), EXTRA_QUOTE)

    def test_probability_boundary(self):
        settings = dict(EXTRA, always_weather_hours=[], weather_probability=0.4)
        self.assertEqual(choose_extra(11, settings, FixedRandom(0.39)), EXTRA_WEATHER)
        self.assertEqual(choose_extra(11, settings, FixedRandom(0.40)), EXTRA_QUOTE)

    def test_zero_probability_is_always_quote(self):
        settings = dict(EXTRA, always_weather_hours=[], weather_probability=0.0)
        for value in (0.0, 0.5, 0.999):
            self.assertEqual(choose_extra(11, settings, FixedRandom(value)), EXTRA_QUOTE)

    def test_default_settings_always_choose_a_quote(self):
        # 運用方針の変更: 時報のあとは常に「ひとこと」にし、天気予報は
        # 流さない。DEFAULT_CONFIG をそのまま使う限り、どの時刻・どの乱数値
        # でも天気予報が選ばれないことを回帰確認する。
        for hour in range(24):
            for value in (0.0, 0.4, 0.5, 0.999):
                self.assertEqual(choose_extra(hour, EXTRA, FixedRandom(value)), EXTRA_QUOTE)


class StubTTS:
    def __init__(self, tmp, fail=False):
        self.tmp = tmp
        self.fail = fail
        self.texts = []

    def synthesize(self, text):
        self.texts.append(text)
        if self.fail:
            raise TTSError("合成できません")
        path = os.path.join(self.tmp, "{0}.wav".format(len(self.texts)))
        with open(path, "wb") as handle:
            handle.write(b"RIFF")
        return path


class StubWeather:
    def __init__(self, text="今日の東京の天気は、晴れ。", fail=False):
        self.text = text
        self.fail = fail
        self.calls = 0
        self.received_today = []

    def describe(self, today=None):
        self.calls += 1
        self.received_today.append(today)
        if self.fail:
            raise WeatherError("接続できません")
        return self.text


class BuilderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        os.makedirs(os.path.join(root, "assets"), exist_ok=True)
        for name in ("announce.wav", "hotaru.mp3"):
            with open(os.path.join(root, "assets", name), "wb") as handle:
                handle.write(b"RIFF")

        self.config = Config(DEFAULT_CONFIG, base_dir=root)
        self.tts = StubTTS(root)
        self.weather = StubWeather()
        self.quotes = QuotePicker(os.path.join(REPO_ROOT, "assets", "quotes.json"))
        self.state = State(os.path.join(root, "cache", "state.json"))
        self.time_signal = os.path.join(root, "assets", "generated", "time_signal.wav")

    def tearDown(self):
        self.tmp.cleanup()

    def make_builder(self, rng=None, tts=None, weather=None, today_provider=None):
        return SequenceBuilder(self.config, tts or self.tts, weather or self.weather,
                               self.quotes, self.state, self.time_signal,
                               rng or FixedRandom(0.99),
                               today_provider=today_provider)

    def labels(self, plan):
        return [segment.label for segment in plan.segments]


class BuildHourlyTest(BuilderTestCase):
    def test_generates_the_time_signal_on_demand(self):
        self.assertFalse(os.path.exists(self.time_signal))
        self.make_builder().build_hourly(10)
        self.assertTrue(os.path.exists(self.time_signal))

    def test_pips_come_first(self):
        plan = self.make_builder().build_hourly(11)
        self.assertIn("時報音", self.labels(plan)[0])

    def test_announces_the_hour(self):
        plan = self.make_builder().build_hourly(11)
        self.assertIn("午前11時をお知らせしました。", plan.spoken)

    def test_noon_uses_the_dedicated_phrase(self):
        plan = self.make_builder().build_hourly(12)
        self.assertIn("正午をお知らせしました。", plan.spoken)

    def test_quote_is_appended(self):
        # 既定の weather_probability は 0.0 のため、rng の値によらずひとことが選ばれる
        plan = self.make_builder().build_hourly(11)
        self.assertEqual(len(plan.segments), 3)
        self.assertIsNotNone(plan.quote)
        self.assertIn(plan.quote, plan.spoken)

    def force_weather_selection(self):
        """既定は weather_probability=0.0（常にひとこと）のため、天気予報の
        経路そのものを検証するテストでは、ここで明示的に確率を 1.0 にして
        選ばれるようにする。"""
        self.config.data["extra_segment"]["weather_probability"] = 1.0

    def test_weather_is_appended(self):
        self.force_weather_selection()
        plan = self.make_builder(rng=FixedRandom(0.0)).build_hourly(11)
        self.assertEqual(self.weather.calls, 1)
        self.assertIn("今日の東京の天気は、晴れ。", plan.spoken)
        self.assertIsNone(plan.quote)

    def test_always_weather_hours_setting_forces_weather(self):
        # 既定では always_weather_hours は空だが、設定で指定すれば
        # その時刻は weather_probability に関わらず必ず天気予報になること。
        self.config.data["extra_segment"]["always_weather_hours"] = [10]
        plan = self.make_builder(rng=FixedRandom(0.99)).build_hourly(10)
        self.assertIn("今日の東京の天気は、晴れ。", plan.spoken)

    def test_weather_uses_the_configured_today_provider(self):
        # スケジューリングは設定タイムゾーン基準（ChimeApp.now().date()）で動くため、
        # 天気の「今日」判定も OS のローカル日付ではなく today_provider に従うこと。
        # OS のローカル日付とは絶対に一致しないよう、遠い未来日を注入して確認する。
        self.force_weather_selection()
        injected_today = date(2099, 1, 1)
        self.assertNotEqual(injected_today, date.today())
        builder = self.make_builder(rng=FixedRandom(0.0),
                                    today_provider=lambda: injected_today)
        builder.build_hourly(11)
        self.assertEqual(self.weather.received_today, [injected_today])

    def test_weather_defaults_to_os_local_today_without_a_provider(self):
        # today_provider を渡さない既存の呼び出し方でも壊れず、
        # 従来どおり OS のローカル日付が使われること。
        self.force_weather_selection()
        builder = self.make_builder(rng=FixedRandom(0.0), today_provider=None)
        builder.build_hourly(11)
        self.assertEqual(self.weather.received_today, [date.today()])

    def test_weather_failure_falls_back_to_a_quote(self):
        self.force_weather_selection()
        builder = self.make_builder(rng=FixedRandom(0.0),
                                    weather=StubWeather(fail=True))
        plan = builder.build_hourly(11)
        self.assertIsNotNone(plan.quote)
        self.assertTrue(any("天気予報を取得できませんでした" in w for w in plan.warnings))

    def test_extra_can_be_disabled(self):
        self.config.data["extra_segment"]["enabled"] = False
        plan = self.make_builder().build_hourly(11)
        self.assertEqual(len(plan.segments), 2)

    def test_default_config_always_appends_a_quote_never_weather(self):
        """既定設定（config.json を作らない場合）では、時報のあとは必ず
        「ひとこと」になり、天気予報は流れないこと（運用方針の変更）。この点を回帰確認する。

        rng に最も天気予報が選ばれやすい 0.0 を渡してもなお、天気予報の
        セグメントが 1 つも作られないことまで確認する。
        """
        for hour in (10, 11, 12, 14, 16):
            with self.subTest(hour=hour):
                plan = self.make_builder(rng=FixedRandom(0.0)).build_hourly(hour)
                self.assertEqual(len(plan.segments), 3)
                self.assertIsNotNone(plan.quote)
        self.assertEqual(self.weather.calls, 0, "既定設定では天気予報を取得してはいけない")

    def test_pips_still_play_when_tts_is_broken(self):
        """音声合成が壊れていても、時報音そのものは必ず鳴る。"""
        builder = self.make_builder(tts=StubTTS(self.tmp.name, fail=True))
        plan = builder.build_hourly(11)
        self.assertEqual(len(plan.segments), 1)
        self.assertIn("時報音", plan.segments[0].label)
        self.assertTrue(plan.warnings)

    def test_used_quote_is_remembered(self):
        plan = self.make_builder().build_hourly(11)
        self.assertEqual(self.state.recent_quotes(), [plan.quote])

    def test_recent_quotes_are_not_repeated(self):
        # rng=0.99 なので毎回「ひとこと」が選ばれる（天気にはならない）
        builder = self.make_builder(rng=FixedRandom(0.99))
        picked = {builder.build_hourly(11).quote for _ in range(5)}
        self.assertEqual(len(picked), 5, "直近のひとことが繰り返し選ばれている")


class BuildClosingTest(BuilderTestCase):
    def test_announcement_then_music(self):
        plan = self.make_builder().build_closing()
        self.assertEqual(len(plan.segments), 2)
        self.assertIn("閉館アナウンス", plan.segments[0].label)
        self.assertIn("蛍の光", plan.segments[1].label)

    def test_music_fades_in(self):
        plan = self.make_builder().build_closing()
        self.assertEqual(plan.segments[0].fade_in_ms, 0)
        self.assertEqual(plan.segments[1].fade_in_ms, 2000)

    def test_extra_text_is_inserted_between(self):
        self.config.data["closing"]["extra_text"] = "本日もご利用ありがとうございました。"
        plan = self.make_builder().build_closing()
        self.assertEqual(len(plan.segments), 3)
        self.assertIn("本日もご利用ありがとうございました。", plan.spoken)


class BuildTextTest(BuilderTestCase):
    def test_single_segment(self):
        plan = self.make_builder().build_text("テストです。")
        self.assertEqual(plan.spoken, ["テストです。"])
        self.assertEqual(len(plan.segments), 1)

    def test_describe_includes_warnings(self):
        plan = self.make_builder(tts=StubTTS(self.tmp.name, fail=True)).build_text("だめ")
        self.assertIn("警告", plan.describe())


class BuildDispatchTest(BuilderTestCase):
    def test_unknown_kind_raises(self):
        from chime.scheduler import Scheduler
        from zoneinfo import ZoneInfo

        scheduler = Scheduler(DEFAULT_CONFIG["schedule"], ZoneInfo("Asia/Tokyo"), 3.0)
        event = scheduler.events_for_date(__import__("datetime").date(2026, 8, 26))[0]
        broken = event.__class__(**dict(event.__dict__, kind="mystery"))
        with self.assertRaises(ValueError):
            self.make_builder().build(broken)


if __name__ == "__main__":
    unittest.main()
