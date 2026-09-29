"""再生シーケンス組み立てのテスト。"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import random
import tempfile
import unittest
from datetime import date
from unittest import mock

from tests.support import REPO_ROOT, load_fixture  # noqa: F401

from chime.config import DEFAULT_CONFIG, Config
from chime.quotes import QuotePicker
from chime.sequence import EXTRA_QUOTE, EXTRA_WEATHER, SequenceBuilder, choose_extra
from chime.state import State
from chime.tts import TTSError
from chime.weather import WeatherError, WeatherService

EXTRA = DEFAULT_CONFIG["extra_segment"]

#: StubWeather の既定の読み上げ文。複数地点（大津・京都）を設定した場合を模した
#: 4 要素（2 地点 × 2 文（現在の天気／気温））。既定（大津の 1 地点）では 2 文。
#: 番号付け（1/4〜4/4）など複数文の扱いを確かめるために 4 文のまま使う。
DEFAULT_WEATHER_SENTENCES = [
    "今の大津の天気は晴れなのだ。",
    "気温は28度なのだ。",
    "今の京都の天気はくもりなのだ。",
    "気温は29度なのだ。",
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

    def test_weather_hours_get_weather_then_quote(self):
        """既定設定（config.json を作らない場合、mode="both"）では、
        ``extra_segment.weather_hours``（既定は 12 時だけ）に含まれる
        時刻だけ、時報のあとに天気予報（複数文）→ ひとこと の順に両方流れる
        こと（天気は 12 時の 1 回だけ、という v5.1.0 の方針を回帰確認する）。

        スタブが 4 文返す場合、セグメントは
        時報音 1 + 時刻 1 + 天気 4 + ひとこと 1 = 7 個になる。
        """
        for hour in (12,):
            with self.subTest(hour=hour):
                weather = StubWeather()
                builder = self.make_builder(weather=weather)
                plan = builder.build_hourly(hour)
                self.assertEqual(weather.calls, 1)
                self.assertEqual(len(plan.segments), 2 + len(weather.sentences) + 1)
                self.assertIsNotNone(plan.quote)
                for sentence in weather.sentences:
                    self.assertIn(sentence, plan.spoken)

    def test_non_weather_hours_get_quote_only_and_never_call_weather(self):
        """``weather_hours`` に無い時刻（既定では 10/11/13/14/15/16 時）は、天気を
        まったく流さず「時報音 + 時刻 + ひとこと」の 3 個だけになること。
        また、その時刻では ``WeatherService`` を一度も呼ばないこと
        （呼ぶと毎正時に無駄な HTTP リクエストが発生してしまう）。
        """
        for hour in (10, 11, 13, 14, 15, 16):
            with self.subTest(hour=hour):
                weather = StubWeather()
                builder = self.make_builder(weather=weather)
                plan = builder.build_hourly(hour)
                self.assertEqual(weather.calls, 0)
                self.assertEqual(len(plan.segments), 3)
                self.assertIsNotNone(plan.quote)
                weather_labels = [label for label in self.labels(plan) if "天気予報" in label]
                self.assertEqual(weather_labels, [])

    def test_empty_weather_hours_never_plays_weather(self):
        # weather_hours を空リストにすると、設定で天気だけ止められる
        # （どの時刻でも天気は一度も流れない）。
        self.config.data["extra_segment"]["weather_hours"] = []
        weather = StubWeather()
        for hour in (10, 11, 12, 13, 14, 15, 16):
            with self.subTest(hour=hour):
                builder = self.make_builder(weather=weather)
                plan = builder.build_hourly(hour)
                self.assertEqual(len(plan.segments), 3)
        self.assertEqual(weather.calls, 0)

    def test_weather_hours_accepts_string_elements_and_ignores_bad_ones(self):
        # JSON 由来の設定では要素が文字列で来ることがある（例 "10"）。
        # int() で正規化して比較すること。変換できない要素があっても
        # 放送は落ちず、その要素だけ無視されること。
        self.config.data["extra_segment"]["weather_hours"] = ["10", "12", "not-a-number", None]
        weather = StubWeather()
        builder = self.make_builder(weather=weather)

        plan10 = builder.build_hourly(10)
        self.assertEqual(len(plan10.segments), 2 + len(weather.sentences) + 1)

        plan11 = builder.build_hourly(11)
        self.assertEqual(len(plan11.segments), 3)

        self.assertEqual(weather.calls, 1)

    def test_choice_mode_does_not_consult_weather_hours(self):
        # mode="choice" では weather_hours を一切参照せず、従来どおり
        # choose_extra() の抽選結果だけで天気予報／ひとことが決まること。
        # weather_hours を空にしても、choice モードでは影響しない。
        self.config.data["extra_segment"]["mode"] = "choice"
        self.config.data["extra_segment"]["weather_probability"] = 1.0
        self.config.data["extra_segment"]["weather_hours"] = []
        weather = StubWeather()
        # 11 時は weather_hours が空でも対象外だが、choice モードでは
        # weather_probability=1.0 なので天気予報が選ばれるはず。
        plan = self.make_builder(rng=FixedRandom(0.0), weather=weather).build_hourly(11)
        self.assertEqual(weather.calls, 1)
        for sentence in weather.sentences:
            self.assertIn(sentence, plan.spoken)
        self.assertIsNone(plan.quote)

    def test_weather_sentences_are_appended_as_separate_segments(self):
        # 天気の各文が 1 つの文字列に連結されず、文ごとに独立したセグメント
        # として積まれること（作り置き音声は文単位のため、連結すると
        # 照合が外れてその文が無音になってしまう）。
        # 12 時は既定の weather_hours に含まれる時刻。
        plan = self.make_builder().build_hourly(12)

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
        # 二重に落とさない）。12 時は既定の weather_hours に含まれる時刻。
        builder = self.make_builder(weather=StubWeather(fail=True))
        plan = builder.build_hourly(12)

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
                # 12 時は既定の weather_hours に含まれる時刻。
                plan = self.make_builder().build_hourly(12)
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

    def test_used_quote_is_recorded_in_the_plan_not_in_the_state(self):
        # 組み立ての段階では state を書かない（再生できたかどうかは、まだ
        # 分からないため）。選んだ文は plan.quote に残し、記録は再生後に
        # ChimeApp.run_event が行う。
        with mock.patch.object(self.state, "remember_quote") as remember:
            plan = self.make_builder().build_hourly(11)
        self.assertIsNotNone(plan.quote)
        remember.assert_not_called()
        self.assertEqual(self.state.recent_quotes(), [])

    def test_building_does_not_write_the_state_file(self):
        self.make_builder().build_hourly(11)
        self.assertFalse(os.path.exists(self.state.path))

    def test_recent_quotes_are_not_repeated(self):
        # mode="both"（既定）でも、天気とは独立に「ひとこと」が毎回別のものに
        # なること。記録は build の外（再生後）で行うので、ここでは
        # remember_quote を自分で呼ぶ。
        builder = self.make_builder(rng=FixedRandom(0.99))
        picked = set()
        for _ in range(5):
            quote = builder.build_hourly(11).quote
            picked.add(quote)
            self.state.remember_quote(quote)
        self.assertEqual(len(picked), 5, "直近のひとことが繰り返し選ばれている")

    def test_recent_quotes_in_the_state_are_still_read(self):
        # 記録済みの文は、build が state から読んで避けること。
        builder = self.make_builder(rng=FixedRandom(0.99))
        first = builder.build_hourly(11).quote
        self.state.remember_quote(first)
        for _ in range(5):
            self.assertNotEqual(builder.build_hourly(11).quote, first)


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

    def test_closing_never_includes_weather(self):
        """利用者の「17 時には流さない」という要望は、確認したところ
        時報（``build_hourly``）とは別経路の閉館放送（16:57、アナウンスが
        「午後五時をお知らせするのだ」と言う）を指していた。閉館放送は
        天気を含まない経路であり、この要望はすでに満たされている
        （コードを変える必要はない）。この事実を固定する回帰テスト。
        """
        weather = StubWeather()
        plan = self.make_builder(weather=weather).build_closing()
        self.assertEqual(len(plan.segments), 2)
        self.assertIn("閉館アナウンス", plan.segments[0].label)
        self.assertIn("蛍の光", plan.segments[1].label)
        self.assertEqual(weather.calls, 0, "閉館放送で WeatherService を呼んではいけない")


@contextlib.contextmanager
def logs_enabled(logger_name, level):
    """tests/__init__.py が止めているログを、このブロックの間だけ拾えるようにする。"""
    logging.disable(logging.NOTSET)
    try:
        with unittest.TestCase().assertLogs(logger_name, level=level) as captured:
            yield captured
    finally:
        logging.disable(logging.CRITICAL)


class ExplodingTTS(StubTTS):
    """TTSError ではない想定外の例外を出す音声合成。"""

    def synthesize(self, text):
        self.texts.append(text)
        raise RuntimeError("想定外の合成エラー")


class ExplodingWeather(StubWeather):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def describe_sentences(self, today=None, use_cache=True):
        self.calls += 1
        raise self.error


class ExplodingQuotes:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def pick(self, hour, recent):
        self.calls += 1
        raise self.error


def open_meteo_response(body=None, read_error=None):
    """``urlopen`` が返す応答のモック（``with`` で使え、``read()`` が本文か例外を返す）。"""
    response = mock.MagicMock()
    response.__enter__.return_value = response
    if read_error is not None:
        response.read.side_effect = read_error
    else:
        response.read.return_value = json.dumps(body).encode("utf-8")
    return response


class DegradeHourlyTest(BuilderTestCase):
    """時報の一部品が壊れても、残りの部品は必ず鳴ること。"""

    def assert_time_signal_first(self, plan):
        self.assertIn("時報音", self.labels(plan)[0])

    def quote_labels(self, plan):
        return [label for label in self.labels(plan) if "ひとこと" in label]

    # -- 天気 ---------------------------------------------------------------
    def test_incomplete_read_from_the_real_weather_service_keeps_every_other_part(self):
        # 実際の障害の再現: 12 時に天気 API の応答が途中で切れる
        # （http.client.IncompleteRead は OSError ではない）。
        # 本物の WeatherService と urlopen のモックで確かめる。
        weather = WeatherService(self.config.section("weather"))
        response = open_meteo_response(read_error=http.client.IncompleteRead(b"{\"cur", 300))
        with mock.patch("chime.weather.urllib.request.urlopen", return_value=response):
            plan = self.make_builder(weather=weather).build_hourly(12)

        self.assert_time_signal_first(plan)
        self.assertIn("正午をお知らせしたのだ。", plan.spoken)
        self.assertIsNotNone(plan.quote)
        self.assertEqual(len(self.quote_labels(plan)), 1)
        self.assertEqual(len(plan.segments), 3)  # 時報音 + 時刻 + ひとこと
        self.assertTrue(any("天気予報を取得できませんでした" in w for w in plan.warnings))

    def test_bad_status_line_from_the_real_weather_service_keeps_every_other_part(self):
        weather = WeatherService(self.config.section("weather"))
        with mock.patch("chime.weather.urllib.request.urlopen",
                        side_effect=http.client.BadStatusLine("")):
            plan = self.make_builder(weather=weather).build_hourly(12)
        self.assertEqual(len(plan.segments), 3)
        self.assertIsNotNone(plan.quote)

    def test_a_typo_in_a_weather_template_keeps_every_other_part(self):
        # sentence_temp の書き間違い（未知の置換名）。天気だけが飛び、
        # 時報音・時刻アナウンス・ひとことは残る。
        self.config.data["weather"]["sentence_temp"] = "気温は{degrees}度なのだ。"
        weather = WeatherService(self.config.section("weather"))
        payload = load_fixture("open_meteo.json")
        with mock.patch("chime.weather.urllib.request.urlopen",
                        return_value=open_meteo_response(payload)):
            plan = self.make_builder(weather=weather).build_hourly(12)
        self.assertEqual(len(plan.segments), 3)
        self.assertIsNotNone(plan.quote)
        self.assertTrue(any("天気予報を取得できませんでした" in w for w in plan.warnings))

    def test_unexpected_exception_in_weather_keeps_every_other_part(self):
        # WeatherError ではない例外が漏れても、_guard が受けて他の部品を残す。
        for error in (KeyError("temp"), RuntimeError("想定外"), TypeError("x")):
            with self.subTest(error=type(error).__name__):
                plan = self.make_builder(weather=ExplodingWeather(error)).build_hourly(12)
                self.assert_time_signal_first(plan)
                self.assertIn("正午をお知らせしたのだ。", plan.spoken)
                self.assertIsNotNone(plan.quote)
                self.assertEqual(len(plan.segments), 3)
                self.assertTrue(any(
                    "天気予報の組み立てに失敗しました（この部分だけ飛ばします）: {0}: ".format(
                        type(error).__name__) in w for w in plan.warnings))

    def test_a_failure_in_the_middle_of_the_weather_sentences_keeps_the_earlier_ones(self):
        # 3 文目の合成で想定外の例外が出ても、それまでの文と、他の部品は残る。
        class FailsOnThird(StubTTS):
            def synthesize(inner, text):
                if len(inner.texts) == 2:
                    inner.texts.append(text)
                    raise RuntimeError("3 文目で失敗")
                return super().synthesize(text)

        plan = self.make_builder(tts=FailsOnThird(self.tmp.name)).build_hourly(12)
        self.assert_time_signal_first(plan)
        self.assertIn(self.weather.sentences[0], plan.spoken)
        self.assertNotIn(self.weather.sentences[1], plan.spoken)
        self.assertTrue(plan.warnings)

    def test_unexpected_exception_in_weather_hours_setting_skips_only_the_weather(self):
        # weather_hours が配列でない（設定ミス）。天気だけ飛び、ひとことは残る。
        self.config.data["extra_segment"]["weather_hours"] = 12
        plan = self.make_builder().build_hourly(12)
        self.assertEqual(self.weather.calls, 0)
        self.assertEqual(len(plan.segments), 3)
        self.assertIsNotNone(plan.quote)
        self.assertTrue(plan.warnings)

    # -- ひとこと -------------------------------------------------------------
    def test_unexpected_exception_in_quote_selection_keeps_every_other_part(self):
        for error in (IndexError("empty"), ValueError("x"), RuntimeError("想定外")):
            with self.subTest(error=type(error).__name__):
                quotes = ExplodingQuotes(error)
                builder = self.make_builder()
                builder.quotes = quotes
                plan = builder.build_hourly(12)
                self.assertEqual(quotes.calls, 1)
                self.assert_time_signal_first(plan)
                self.assertIn("正午をお知らせしたのだ。", plan.spoken)
                for sentence in self.weather.sentences:
                    self.assertIn(sentence, plan.spoken)
                self.assertIsNone(plan.quote)
                self.assertEqual(self.quote_labels(plan), [])
                self.assertTrue(any(
                    "ひとこと" in w and "この部分だけ飛ばします" in w for w in plan.warnings))

    def test_unexpected_exception_in_choice_mode_keeps_the_other_parts(self):
        # mode="choice"（次の版で削除予定）の分岐も、部品ごとに守られていること。
        self.config.data["extra_segment"]["mode"] = "choice"
        builder = self.make_builder(rng=FixedRandom(0.99))
        builder.quotes = ExplodingQuotes(RuntimeError("想定外"))
        plan = builder.build_hourly(11)
        self.assert_time_signal_first(plan)
        self.assertIn("午前11時をお知らせしたのだ。", plan.spoken)
        self.assertEqual(len(plan.segments), 2)
        self.assertTrue(plan.warnings)

    def test_unexpected_exception_in_extra_selection_keeps_the_other_parts(self):
        # weather_probability が数値でない（設定ミス）と choose_extra が ValueError。
        self.config.data["extra_segment"]["mode"] = "choice"
        self.config.data["extra_segment"]["weather_probability"] = "たくさん"
        plan = self.make_builder().build_hourly(11)
        self.assert_time_signal_first(plan)
        self.assertIn("午前11時をお知らせしたのだ。", plan.spoken)
        self.assertEqual(len(plan.segments), 2)
        self.assertTrue(plan.warnings)

    def test_guarded_failures_are_logged_with_a_traceback(self):
        builder = self.make_builder(weather=ExplodingWeather(RuntimeError("想定外")))
        with logs_enabled("chime.sequence", "ERROR") as captured:
            builder.build_hourly(12)
        self.assertTrue(any("天気予報" in line for line in captured.output))
        self.assertTrue(any("RuntimeError" in line for line in captured.output))

    def test_keyboard_interrupt_is_not_swallowed(self):
        # SIGINT（Ctrl-C）・停止要求は握りつぶさない（Exception の子ではない）。
        builder = self.make_builder(weather=ExplodingWeather(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            builder.build_hourly(12)
        builder = self.make_builder(weather=ExplodingWeather(SystemExit(0)))
        with self.assertRaises(SystemExit):
            builder.build_hourly(12)

    # -- 時刻アナウンス ---------------------------------------------------------
    def test_a_typo_in_announce_template_falls_back_to_the_default_wording(self):
        # {hours} は存在しない置換名（正しくは {hour} / {hour_reading}）。
        self.config.data["time_signal"]["announce_template"] = "{period}{hours}をお知らせしました。"
        with logs_enabled("chime.sequence", "ERROR") as captured:
            plan = self.make_builder().build_hourly(11)
        self.assert_time_signal_first(plan)
        self.assertIn("午前11時をお知らせしたのだ。", plan.spoken)
        self.assertEqual(len(plan.segments), 3)  # 時報音 + 時刻 + ひとこと
        self.assertIsNotNone(plan.quote)
        self.assertTrue(any("announce_template" in w or "時刻アナウンス" in w
                            for w in plan.warnings))
        self.assertTrue(any("KeyError" in line for line in captured.output))

    def test_announce_template_errors_of_every_kind_fall_back(self):
        broken = {
            "KeyError": "{period}{hours}をお知らせしました。",
            "IndexError": "{0}をお知らせしました。",
            "ValueError": "{period}{hour_reading をお知らせしました。",
        }
        for kind, template in broken.items():
            with self.subTest(kind=kind):
                self.config.data["time_signal"]["announce_template"] = template
                plan = self.make_builder().build_hourly(11)
                self.assertIn("午前11時をお知らせしたのだ。", plan.spoken)
                self.assertTrue(any(kind in w for w in plan.warnings))

    def test_a_typo_in_noon_template_falls_back_to_the_default_wording(self):
        self.config.data["time_signal"]["noon_template"] = "{noon}をお知らせしました。"
        plan = self.make_builder().build_hourly(12)
        self.assertIn("正午をお知らせしたのだ。", plan.spoken)
        self.assertTrue(plan.warnings)

    def test_the_fallback_wording_is_read_with_the_default_hour_readings(self):
        # 既定の設定で作り直すので、4 時は「よじ」など既定の読みになる。
        self.config.data["time_signal"]["announce_template"] = "{oops}"
        self.config.data["time_signal"]["hour_readings"] = {"4": "誤読"}
        plan = self.make_builder().build_hourly(16)
        self.assertIn("午後よじをお知らせしたのだ。", plan.spoken)

    def test_a_valid_custom_announce_template_is_still_used(self):
        # フォールバックは書き間違いのときだけ。正しい独自文言はそのまま使う。
        self.config.data["time_signal"]["announce_template"] = "{period}{hour}時なのだ。"
        plan = self.make_builder().build_hourly(11)
        self.assertIn("午前11時なのだ。", plan.spoken)
        self.assertEqual(plan.warnings, [])

    def test_announce_failure_other_than_template_keeps_the_other_parts(self):
        # 合成が想定外の例外を出しても、時報音と（別の部品の）天気・ひとこと
        # の組み立ては続く。ここでは全部が同じ合成を通るので、時報音だけが残る。
        plan = self.make_builder(tts=ExplodingTTS(self.tmp.name)).build_hourly(11)
        self.assertEqual(len(plan.segments), 1)
        self.assert_time_signal_first(plan)
        self.assertTrue(plan.warnings)

    # -- 時報音 -------------------------------------------------------------
    def test_failed_generation_uses_the_existing_time_signal_file(self):
        os.makedirs(os.path.dirname(self.time_signal), exist_ok=True)
        with open(self.time_signal, "wb") as handle:
            handle.write(b"RIFF")
        with mock.patch("chime.timesignal.ensure_time_signal",
                        side_effect=OSError("ディスクがいっぱい")):
            with logs_enabled("chime.sequence", "ERROR"):
                plan = self.make_builder().build_hourly(11)
        self.assert_time_signal_first(plan)
        self.assertEqual(plan.segments[0].path, self.time_signal)
        self.assertIn("午前11時をお知らせしたのだ。", plan.spoken)
        self.assertEqual(len(plan.segments), 3)

    def test_failed_generation_without_a_file_still_speaks(self):
        # 時報音のファイルが無いまま必須セグメントを積むと、再生時に
        # PlaybackError になって放送全体が消える。積まずに、読み上げだけ鳴らす。
        self.assertFalse(os.path.exists(self.time_signal))
        with mock.patch("chime.timesignal.ensure_time_signal",
                        side_effect=OSError("書き込めません")):
            with logs_enabled("chime.sequence", "ERROR") as captured:
                plan = self.make_builder().build_hourly(11)
        self.assertFalse(any("時報音" in label for label in self.labels(plan)))
        self.assertIn("午前11時をお知らせしたのだ。", plan.spoken)
        self.assertIsNotNone(plan.quote)
        self.assertEqual(len(plan.segments), 2)
        self.assertTrue(any("音源ファイルが見つかりません: {0}".format(self.time_signal) in line
                            for line in captured.output))
        self.assertTrue(any("音源ファイルが見つかりません" in w for w in plan.warnings))

    def test_a_missing_time_signal_is_not_a_required_segment(self):
        # 上のプランが実際に PlaybackError にならず再生できること。
        from chime.audio import Player

        played = []

        class Recorder(Player):
            name = "recorder"

            def play_one(self, segment):
                played.append(segment.path)

        with mock.patch("chime.timesignal.ensure_time_signal", side_effect=OSError("x")):
            plan = self.make_builder().build_hourly(11)
        self.assertEqual(Recorder({}).play(plan.segments), 2)
        self.assertEqual(len(played), 2)

    def test_time_signal_generation_failure_is_logged_as_an_error(self):
        with mock.patch("chime.timesignal.ensure_time_signal",
                        side_effect=OSError("書き込めません")):
            with logs_enabled("chime.sequence", "ERROR") as captured:
                self.make_builder().build_hourly(11)
        self.assertTrue(any("書き込めません" in line for line in captured.output))


class DegradeClosingTest(BuilderTestCase):
    def announce_path(self):
        return self.config.path("closing.announce_file")

    def music_path(self):
        return self.config.path("closing.music_file")

    def test_music_still_plays_when_the_announcement_is_missing(self):
        os.remove(self.announce_path())
        with logs_enabled("chime.sequence", "ERROR") as captured:
            plan = self.make_builder().build_closing()
        self.assertEqual(len(plan.segments), 1)
        self.assertIn("蛍の光", plan.segments[0].label)
        self.assertEqual(plan.segments[0].fade_in_ms, 2000)
        self.assertTrue(any("音源ファイルが見つかりません: {0}".format(self.announce_path())
                            in line for line in captured.output))
        self.assertTrue(any("音源ファイルが見つかりません" in w for w in plan.warnings))

    def test_announcement_still_plays_when_the_music_is_missing(self):
        os.remove(self.music_path())
        with logs_enabled("chime.sequence", "ERROR") as captured:
            plan = self.make_builder().build_closing()
        self.assertEqual(len(plan.segments), 1)
        self.assertIn("閉館アナウンス", plan.segments[0].label)
        self.assertTrue(any("音源ファイルが見つかりません: {0}".format(self.music_path())
                            in line for line in captured.output))

    def test_both_missing_gives_an_empty_plan_without_raising(self):
        os.remove(self.announce_path())
        os.remove(self.music_path())
        plan = self.make_builder().build_closing()
        self.assertEqual(plan.segments, [])
        self.assertEqual(len(plan.warnings), 2)

    def test_an_unset_file_is_skipped_silently_as_before(self):
        # 空文字列は「設定しない」の意味。ファイルが無いのとは違い、警告しない。
        self.config.data["closing"]["announce_file"] = ""
        plan = self.make_builder().build_closing()
        self.assertEqual(len(plan.segments), 1)
        self.assertEqual(plan.warnings, [])

    def test_a_missing_file_does_not_hide_the_extra_text(self):
        os.remove(self.announce_path())
        self.config.data["closing"]["extra_text"] = "本日もご利用ありがとうございました。"
        plan = self.make_builder().build_closing()
        self.assertEqual(len(plan.segments), 2)
        self.assertIn("本日もご利用ありがとうございました。", plan.spoken)

    def test_a_broken_extra_text_keeps_the_announcement_and_the_music(self):
        self.config.data["closing"]["extra_text"] = "追加のお知らせなのだ。"
        plan = self.make_builder(tts=ExplodingTTS(self.tmp.name)).build_closing()
        self.assertEqual(len(plan.segments), 2)
        self.assertIn("閉館アナウンス", plan.segments[0].label)
        self.assertIn("蛍の光", plan.segments[1].label)
        self.assertTrue(any("追加アナウンス" in w and "この部分だけ飛ばします" in w
                            for w in plan.warnings))

    def test_a_broken_fade_in_setting_still_plays_the_music(self):
        for value in ("ゆっくり", [2000]):
            with self.subTest(value=value):
                self.config.data["audio"]["fade_in_ms"] = value
                plan = self.make_builder().build_closing()
                self.assertEqual(len(plan.segments), 2)
                self.assertIn("蛍の光", plan.segments[1].label)
                self.assertEqual(plan.segments[1].fade_in_ms, 0)
                self.assertTrue(any("フェードイン" in w for w in plan.warnings))

    def test_the_normal_closing_plan_has_no_warnings(self):
        plan = self.make_builder().build_closing()
        self.assertEqual(plan.warnings, [])


class BuildMinimalTest(BuilderTestCase):
    """組み立て全体が失敗したときの、最小のプラン。"""

    def event(self, kind):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from chime.scheduler import Event

        moment = datetime(2026, 8, 26, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        key = "hourly:12" if kind == "hourly" else "closing"
        return Event(key=key, kind=kind, hour=12, minute=0,
                     at=moment, play_at=moment, prepare_at=moment)

    def test_hourly_is_the_time_signal_only(self):
        weather = StubWeather()
        plan = self.make_builder(weather=weather).build_minimal(self.event("hourly"))
        self.assertEqual(len(plan.segments), 1)
        self.assertIn("時報音", plan.segments[0].label)
        self.assertEqual(plan.segments[0].path, self.time_signal)
        self.assertEqual(plan.spoken, [])
        self.assertIsNone(plan.quote)
        self.assertEqual(weather.calls, 0)
        self.assertEqual(self.tts.texts, [])

    def test_hourly_ignores_a_broken_announce_template(self):
        self.config.data["time_signal"]["announce_template"] = "{hours}"
        plan = self.make_builder().build_minimal(self.event("hourly"))
        self.assertEqual(len(plan.segments), 1)
        self.assertEqual(plan.warnings, [])

    def test_hourly_generates_the_time_signal_if_needed(self):
        self.assertFalse(os.path.exists(self.time_signal))
        self.make_builder().build_minimal(self.event("hourly"))
        self.assertTrue(os.path.exists(self.time_signal))

    def test_hourly_without_any_time_signal_is_an_empty_plan(self):
        with mock.patch("chime.timesignal.ensure_time_signal", side_effect=OSError("x")):
            plan = self.make_builder().build_minimal(self.event("hourly"))
        self.assertEqual(plan.segments, [])
        self.assertTrue(plan.warnings)

    def test_closing_is_the_announcement_and_the_music_only(self):
        self.config.data["closing"]["extra_text"] = "本日もご利用ありがとうございました。"
        plan = self.make_builder().build_minimal(self.event("closing"))
        self.assertEqual(len(plan.segments), 2)
        self.assertIn("閉館アナウンス", plan.segments[0].label)
        self.assertIn("蛍の光", plan.segments[1].label)
        self.assertEqual(plan.spoken, [])
        self.assertEqual(self.tts.texts, [])
        self.assertEqual(self.weather.calls, 0)

    def test_closing_skips_only_the_missing_file(self):
        os.remove(self.config.path("closing.announce_file"))
        plan = self.make_builder().build_minimal(self.event("closing"))
        self.assertEqual(len(plan.segments), 1)
        self.assertIn("蛍の光", plan.segments[0].label)

    def test_closing_plays_the_music_even_with_a_broken_fade_in(self):
        self.config.data["audio"]["fade_in_ms"] = "ゆっくり"
        plan = self.make_builder().build_minimal(self.event("closing"))
        self.assertEqual(len(plan.segments), 2)
        self.assertEqual(plan.segments[1].fade_in_ms, 0)

    def test_the_event_is_kept_on_the_plan(self):
        event = self.event("hourly")
        self.assertIs(self.make_builder().build_minimal(event).event, event)

    def test_never_touches_the_state_or_the_quotes(self):
        builder = self.make_builder()
        builder.quotes = ExplodingQuotes(RuntimeError("呼ばれてはいけない"))
        with mock.patch.object(self.state, "remember_quote") as remember:
            builder.build_minimal(self.event("hourly"))
            builder.build_minimal(self.event("closing"))
        remember.assert_not_called()
        self.assertEqual(builder.quotes.calls, 0)

    def test_an_unknown_kind_is_an_empty_plan(self):
        event = self.event("hourly")
        event = event.__class__(**dict(event.__dict__, kind="mystery"))
        plan = self.make_builder().build_minimal(event)
        self.assertEqual(plan.segments, [])


class BuildTextTest(BuilderTestCase):
    def test_single_segment(self):
        plan = self.make_builder().build_text("テストです。")
        self.assertEqual(plan.spoken, ["テストです。"])
        self.assertEqual(len(plan.segments), 1)

    def test_describe_includes_warnings(self):
        plan = self.make_builder(tts=StubTTS(self.tmp.name, fail=True)).build_text("だめ")
        self.assertIn("警告", plan.describe())

    def test_warning_names_the_silent_phrase(self):
        # どの文が無音になったかがログだけで分かること（TTSError の文言には
        # 文そのものが入らないため）。
        plan = self.make_builder(tts=StubTTS(self.tmp.name, fail=True)).build_text("だめなのだ。")
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn("を合成できませんでした（「だめなのだ。」）", plan.warnings[0])


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
