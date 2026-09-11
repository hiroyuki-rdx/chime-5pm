"""再生シーケンス組み立てのテスト。"""

from __future__ import annotations

import logging
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

#: StubWeather の既定の読み上げ文。2 地点（大津・京都）× 3 文
#: （天気／最高気温／降水確率）を模した、実運用のパターン数と揃えた 6 要素。
DEFAULT_WEATHER_SENTENCES = [
    "今日の大津の天気はおおむね晴れなのだ。",
    "最高気温は28度なのだ。",
    "降水確率は10パーセントなのだ。",
    "今日の京都の天気はくもりなのだ。",
    "最高気温は27度なのだ。",
    "降水確率は20パーセントなのだ。",
]


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

    def test_default_extra_settings_choose_quote_when_mode_is_choice(self):
        # choose_extra() 自体は mode="choice" 運用のための抽選関数で、
        # "mode" キーそのものは見ない。既定の weather_probability は 0.0 の
        # ままなので、運用者が mode="choice" に切り替えた場合の既定挙動は
        # 「常にひとこと」になることを回帰確認する。
        #
        # 実際の既定運用（mode="both"）では天気予報とひとことの両方が必ず
        # 流れる。それは BuildHourlyTest 側で確認する。
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
    """``WeatherService.describe_sentences()`` のスタブ。

    全地点ぶんの読み上げ文を地点の順に平坦なリストで返す契約
    （``chime.weather.WeatherService.describe_sentences``）を模している。
    """

    def __init__(self, sentences=None, fail=False):
        self.sentences = list(DEFAULT_WEATHER_SENTENCES if sentences is None else sentences)
        self.fail = fail
        self.calls = 0
        self.received_today = []
        self.received_use_cache = []

    def describe_sentences(self, today=None, use_cache=True):
        self.calls += 1
        self.received_today.append(today)
        self.received_use_cache.append(use_cache)
        if self.fail:
            raise WeatherError("接続できません")
        return list(self.sentences)


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
        self.assertIn("午前11時をお知らせしたのだ。", plan.spoken)

    def test_noon_uses_the_dedicated_phrase(self):
        plan = self.make_builder().build_hourly(12)
        self.assertIn("正午をお知らせしたのだ。", plan.spoken)

    def test_quote_is_appended(self):
        # mode="choice" のとき、既定の weather_probability は 0.0 のため、
        # rng の値によらず「ひとこと」が選ばれる。
        self.config.data["extra_segment"]["mode"] = "choice"
        plan = self.make_builder().build_hourly(11)
        self.assertEqual(len(plan.segments), 3)
        self.assertIsNotNone(plan.quote)
        self.assertIn(plan.quote, plan.spoken)

    def force_weather_selection(self):
        """既定は mode="both"（天気予報とひとこと両方を流す）のため、
        choose_extra による排他選択の経路そのものを検証するテストでは、
        ここで明示的に mode="choice" にしたうえで weather_probability を
        1.0 にして天気予報が選ばれるようにする。"""
        self.config.data["extra_segment"]["mode"] = "choice"
        self.config.data["extra_segment"]["weather_probability"] = 1.0

    # -- mode="both"（既定） ---------------------------------------------

    def test_default_config_appends_weather_then_quote(self):
        """既定設定（config.json を作らない場合、mode="both"）では、
        時報のあとに天気予報（複数文）→ ひとこと の順に両方流れること
        （運用方針の変更を回帰確認する）。

        スタブが 6 文返す場合、セグメントは
        時報音 1 + 時刻 1 + 天気 6 + ひとこと 1 = 9 個になる。
        """
        for hour in (10, 11, 12, 14, 16):
            with self.subTest(hour=hour):
                weather = StubWeather()
                builder = self.make_builder(weather=weather)
                plan = builder.build_hourly(hour)
                self.assertEqual(weather.calls, 1)
                self.assertEqual(len(plan.segments), 2 + len(weather.sentences) + 1)
                self.assertIsNotNone(plan.quote)
                for sentence in weather.sentences:
                    self.assertIn(sentence, plan.spoken)

    def test_weather_sentences_are_appended_as_separate_segments(self):
        # 天気の各文が 1 つの文字列に連結されず、文ごとに独立したセグメント
        # として積まれること（作り置き音声は文単位のため、連結すると
        # 照合が外れて Open JTalk にフォールバックしてしまう）。
        plan = self.make_builder().build_hourly(11)

        weather_start = 1  # plan.spoken[0] は時刻アナウンス
        weather_spoken = plan.spoken[weather_start:weather_start + len(self.weather.sentences)]
        self.assertEqual(weather_spoken, self.weather.sentences)

        # TTS には文ごとに個別に渡っている（連結された 1 つの長い文字列ではない）
        for sentence in self.weather.sentences:
            self.assertIn(sentence, self.tts.texts)
        self.assertNotIn("".join(self.weather.sentences), self.tts.texts)

        weather_labels = [label for label in self.labels(plan) if "天気予報" in label]
        self.assertEqual(len(weather_labels), len(self.weather.sentences))
        self.assertEqual(len(set(weather_labels)), len(weather_labels),
                         "天気の各セグメントのラベルは重複しないはず")

    def test_weather_failure_does_not_prevent_the_quote_in_both_mode(self):
        # 天気が全滅（WeatherError）しても、ひとことは必ず流れること。
        # かつ、ひとことが 2 つ流れないこと（mode="both" では
        # _append_weather(fallback=False) のため、ここでの失敗はひとことへ
        # 二重に落とさない）。
        builder = self.make_builder(weather=StubWeather(fail=True))
        plan = builder.build_hourly(11)

        self.assertEqual(len(plan.segments), 3)  # 時報音 + 時刻 + ひとこと
        self.assertIsNotNone(plan.quote)
        self.assertTrue(any("天気予報を取得できませんでした" in w for w in plan.warnings))

        quote_labels = [label for label in self.labels(plan) if "ひとこと" in label]
        self.assertEqual(len(quote_labels), 1, "ひとことが2つ流れてはいけない")

    def test_unknown_mode_falls_back_to_both_and_logs_a_warning(self):
        self.config.data["extra_segment"]["mode"] = "surprise"
        # tests/__init__.py がテスト全体でログを抑制している（logging.disable
        # (logging.CRITICAL)）ため、assertLogs で拾えるよう tests/test_cli.py
        # と同じ手順でこのテストの間だけ一時的に解除する。
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("chime.sequence", level="WARNING") as cm:
                plan = self.make_builder().build_hourly(11)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertTrue(any("mode" in message for message in cm.output))
        self.assertEqual(len(plan.segments), 2 + len(self.weather.sentences) + 1)
        self.assertIsNotNone(plan.quote)
        for sentence in self.weather.sentences:
            self.assertIn(sentence, plan.spoken)

    # -- mode="choice"（従来どおりの排他選択） ----------------------------

    def test_weather_is_appended(self):
        self.force_weather_selection()
        plan = self.make_builder(rng=FixedRandom(0.0)).build_hourly(11)
        self.assertEqual(self.weather.calls, 1)
        for sentence in self.weather.sentences:
            self.assertIn(sentence, plan.spoken)
        self.assertIsNone(plan.quote)

    def test_always_weather_hours_setting_forces_weather(self):
        # 既定では always_weather_hours は空だが、mode="choice" で設定すれば
        # その時刻は weather_probability に関わらず必ず天気予報になること。
        self.config.data["extra_segment"]["mode"] = "choice"
        self.config.data["extra_segment"]["always_weather_hours"] = [10]
        plan = self.make_builder(rng=FixedRandom(0.99)).build_hourly(10)
        for sentence in self.weather.sentences:
            self.assertIn(sentence, plan.spoken)
        self.assertIsNone(plan.quote)

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
        # mode="choice" のときだけ、天気の失敗が fallback_to_quote に従って
        # 「ひとこと」に切り替わる。
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
        self.assertEqual(self.weather.calls, 0)
        self.assertIsNone(plan.quote)

    def test_extra_can_be_disabled_in_choice_mode_too(self):
        # enabled=False は mode に関わらず、どちらの mode でもおまけを出さない。
        self.config.data["extra_segment"]["mode"] = "choice"
        self.config.data["extra_segment"]["enabled"] = False
        plan = self.make_builder().build_hourly(11)
        self.assertEqual(len(plan.segments), 2)
        self.assertEqual(self.weather.calls, 0)
        self.assertIsNone(plan.quote)

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
        # mode="both"（既定）でも、天気とは独立に「ひとこと」が毎回別のものに
        # なること。
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
