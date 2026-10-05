"""再生シーケンス組み立てのテスト。"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

from tests.support import REPO_ROOT, load_fixture  # noqa: F401

from chime.config import DEFAULT_CONFIG, Config
from chime.quotes import QuotePicker
from chime.sequence import SequenceBuilder
from chime.state import State
from chime.tts import TTSError
from chime.weather import WeatherError, WeatherService

#: 6.0.0 で廃止した extra_segment のキー。古い config.json に残っていても
#: 放送の組み立てには一切影響しない（起動時に警告して無視するだけ）。
LEGACY_EXTRA_SEGMENT_KEYS = {
    "mode": "choice",
    "weather_probability": 1.0,
    "always_weather_hours": [10],
    "always_quote_hours": [12],
    "fallback_to_quote": True,
}

#: StubWeather の既定の読み上げ文。複数地点（大津・京都）を設定した場合を模した
#: 4 要素（2 地点 × 2 文（現在の天気／気温））。既定（大津の 1 地点）では 2 文。
#: 番号付け（1/4〜4/4）など複数文の扱いを確かめるために 4 文のまま使う。
DEFAULT_WEATHER_SENTENCES = [
    "今の大津の天気は晴れなのだ。",
    "気温は28度なのだ。",
    "今の京都の天気はくもりなのだ。",
    "気温は29度なのだ。",
]


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

    def make_builder(self, tts=None, weather=None, today_provider=None):
        return SequenceBuilder(self.config, tts or self.tts, weather or self.weather,
                               self.quotes, self.state, self.time_signal,
                               today_provider=today_provider)

    def labels(self, plan):
        return [segment.label for segment in plan.segments]

    def quote_labels(self, plan):
        return [label for label in self.labels(plan) if "ひとこと" in label]

    def weather_labels(self, plan):
        return [label for label in self.labels(plan) if "天気予報" in label]


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
        # 11 時は weather_hours の外。時報音 + 時刻 + ひとこと の 3 個になる。
        plan = self.make_builder().build_hourly(11)
        self.assertEqual(len(plan.segments), 3)
        self.assertIsNotNone(plan.quote)
        self.assertIn(plan.quote, plan.spoken)

    def test_weather_hours_get_weather_then_quote(self):
        """``extra_segment.weather_hours``（既定は 12 時だけ）に含まれる
        時刻だけ、時報のあとに天気予報（複数文）→ ひとこと の順に流れること
        （天気は 12 時の 1 回だけ、という v5.1.0 の方針を回帰確認する）。
        ひとことは天気の成否に関わらず、毎回ちょうど 1 つ。

        スタブが 4 文返す場合、セグメントは
        時報音 1 + 時刻 1 + 天気 4 + ひとこと 1 = 7 個になる。
        """
        weather = StubWeather()
        plan = self.make_builder(weather=weather).build_hourly(12)
        self.assertEqual(weather.calls, 1)
        self.assertEqual(len(plan.segments), 2 + len(weather.sentences) + 1)
        self.assertIsNotNone(plan.quote)
        # 読み上げの順序は 時刻 → 天気（地点の順） → ひとこと。
        self.assertEqual(plan.spoken,
                         ["正午をお知らせしたのだ。"] + weather.sentences + [plan.quote])
        self.assertEqual(len(self.quote_labels(plan)), 1)

    def test_speech_segments_are_optional_and_the_time_signal_is_required(self):
        """読み上げのセグメントは optional=True、時報音は optional=False であること。

        読み上げの WAV が 1 つ欠けても、無音になるのはその 1 文だけで済ませたい。
        ``Player.play`` は欠けた optional セグメントを飛ばすが、必須セグメントが
        欠けると ``PlaybackError`` を送出する。読み上げを必須にしてしまうと、
        音声ファイル 1 つの欠落で放送全体（時報音も含む）が鳴らなくなる。
        """
        # 12 時は天気を流す時刻。時報音 + 時刻 + 天気（複数文）+ ひとこと が揃う。
        plan = self.make_builder().build_hourly(12)
        self.assertEqual(len(plan.segments), 2 + len(self.weather.sentences) + 1)

        time_signal, speech = plan.segments[0], plan.segments[1:]
        self.assertIn("時報音", time_signal.label)
        self.assertFalse(time_signal.optional, "時報音は必須セグメントのはず")

        # 読み上げの数は plan.spoken と一致する（時刻 + 天気の各文 + ひとこと）。
        self.assertEqual(len(speech), len(plan.spoken))
        self.assertEqual(speech[0].label, "時刻アナウンス「正午をお知らせしたのだ。」")
        self.assertEqual(len(self.weather_labels(plan)), len(self.weather.sentences))
        self.assertEqual(len(self.quote_labels(plan)), 1)
        for segment in speech:
            with self.subTest(label=segment.label):
                self.assertTrue(segment.optional, "読み上げは optional のはず")

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
                self.assertEqual(self.weather_labels(plan), [])

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

    def test_weather_failure_adds_a_warning_but_no_second_quote(self):
        # 天気が全滅（WeatherError）しても、ひとことは必ず 1 つ流れること。
        # 天気の失敗を「ひとこと」で埋めると、その後の通常のひとことと
        # 二重になるので、ここでは警告を積むだけ。
        # 12 時は既定の weather_hours に含まれる時刻。
        builder = self.make_builder(weather=StubWeather(fail=True))
        plan = builder.build_hourly(12)

        self.assertEqual(len(plan.segments), 3)  # 時報音 + 時刻 + ひとこと
        self.assertIsNotNone(plan.quote)
        self.assertTrue(any("天気予報を取得できませんでした" in w for w in plan.warnings))
        self.assertEqual(len(self.quote_labels(plan)), 1, "ひとことが2つ流れてはいけない")

    def test_weather_disabled_adds_a_warning_and_keeps_the_quote(self):
        # weather.enabled=false でも weather_hours の時刻には WeatherService を
        # 呼ぶ（無効の判断は WeatherService の側）。その WeatherError が
        # 「取得できませんでした」の警告になり、ひとことは 1 つだけ残る。
        self.config.data["weather"]["enabled"] = False
        weather = WeatherService(self.config.section("weather"))
        with mock.patch("chime.weather.urllib.request.urlopen") as urlopen:
            plan = self.make_builder(weather=weather).build_hourly(12)
        urlopen.assert_not_called()
        self.assertEqual(len(plan.segments), 3)  # 時報音 + 時刻 + ひとこと
        self.assertEqual(self.weather_labels(plan), [])
        self.assertEqual(len(self.quote_labels(plan)), 1)
        self.assertTrue(any("天気予報を取得できませんでした" in w and "無効化" in w
                            for w in plan.warnings))

    def test_weather_uses_the_configured_today_provider(self):
        # スケジューリングは設定タイムゾーン基準（ChimeApp.now().date()）で動くため、
        # 天気の「今日」判定も OS のローカル日付ではなく today_provider に従うこと。
        # OS のローカル日付とは絶対に一致しないよう、遠い未来日を注入して確認する。
        injected_today = date(2099, 1, 1)
        self.assertNotEqual(injected_today, date.today())
        builder = self.make_builder(today_provider=lambda: injected_today)
        builder.build_hourly(12)
        self.assertEqual(self.weather.received_today, [injected_today])

    def test_weather_defaults_to_os_local_today_without_a_provider(self):
        # today_provider を渡さない既存の呼び出し方でも壊れず、
        # 従来どおり OS のローカル日付が使われること。
        builder = self.make_builder(today_provider=None)
        builder.build_hourly(12)
        self.assertEqual(self.weather.received_today, [date.today()])

    # -- 廃止したキー（v6.0.0） --------------------------------------------------

    def use_legacy_keys(self, legacy):
        """extra_segment を「既定 + 廃止キー」にする（変種どうしが混ざらないよう作り直す）。"""
        self.config.data["extra_segment"] = dict(DEFAULT_CONFIG["extra_segment"], **legacy)

    def legacy_variants(self):
        """古い config.json に残りうる extra_segment の廃止キーの組み合わせ。"""
        return {
            "legacy": dict(LEGACY_EXTRA_SEGMENT_KEYS),
            # 未知の mode は以前は警告つきで "both" 扱いだった。今は読まない。
            "unknown-mode": {"mode": "surprise"},
            # 読んでいたら ValueError / TypeError になる値でも、読まなければ無害。
            "garbage": {"mode": 42, "weather_probability": "たくさん",
                        "always_weather_hours": "x", "always_quote_hours": None,
                        "fallback_to_quote": "?"},
        }

    def test_legacy_extra_segment_keys_are_ignored_in_a_non_weather_hour(self):
        # always_weather_hours=[10] / weather_probability=1.0 は効かない。
        # 10 時は天気を流さず（WeatherService も呼ばず）、ひとこと 1 つだけ。
        for name, legacy in self.legacy_variants().items():
            with self.subTest(variant=name):
                self.use_legacy_keys(legacy)
                weather = StubWeather()
                with mock.patch("chime.sequence.logger") as log:
                    plan = self.make_builder(weather=weather).build_hourly(10)
                self.assertEqual(weather.calls, 0)
                self.assertEqual(len(plan.segments), 3)  # 時報音 + 時刻 + ひとこと
                self.assertEqual(self.weather_labels(plan), [])
                self.assertEqual(len(self.quote_labels(plan)), 1)
                self.assertIsNotNone(plan.quote)
                self.assertEqual(plan.warnings, [])
                log.warning.assert_not_called()

    def test_legacy_extra_segment_keys_are_ignored_in_the_weather_hour(self):
        # always_quote_hours=[12] は効かない。12 時は天気 → ひとこと のまま。
        for name, legacy in self.legacy_variants().items():
            with self.subTest(variant=name):
                self.use_legacy_keys(legacy)
                weather = StubWeather()
                plan = self.make_builder(weather=weather).build_hourly(12)
                self.assertEqual(weather.calls, 1)
                self.assertEqual(
                    plan.spoken,
                    ["正午をお知らせしたのだ。"] + weather.sentences + [plan.quote])
                self.assertEqual(len(self.quote_labels(plan)), 1)
                self.assertEqual(plan.warnings, [])

    def test_legacy_extra_segment_keys_do_not_turn_a_weather_failure_into_a_quote(self):
        # fallback_to_quote=true は効かない。天気が失敗しても、ひとことは
        # 通常の 1 つだけ（天気の代わりの 2 つめは出ない）。
        for name, legacy in self.legacy_variants().items():
            with self.subTest(variant=name):
                self.use_legacy_keys(legacy)
                weather = StubWeather(fail=True)
                plan = self.make_builder(weather=weather).build_hourly(12)
                self.assertEqual(weather.calls, 1)
                self.assertEqual(len(plan.segments), 3)  # 時報音 + 時刻 + ひとこと
                self.assertEqual(len(self.quote_labels(plan)), 1)
                self.assertTrue(any("天気予報を取得できませんでした" in w
                                    for w in plan.warnings))

    def test_extra_can_be_disabled(self):
        # 天気を流す 12 時でも、おまけ全体が止まる（時報音 + 時刻だけ）。
        for hour in (11, 12):
            with self.subTest(hour=hour):
                self.config.data["extra_segment"]["enabled"] = False
                weather = StubWeather()
                plan = self.make_builder(weather=weather).build_hourly(hour)
                self.assertEqual(len(plan.segments), 2)
                self.assertEqual(weather.calls, 0)
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
        # 天気とは独立に「ひとこと」が毎回別のものになること。記録は
        # build の外（再生後）で行うので、ここでは remember_quote を自分で呼ぶ。
        builder = self.make_builder()
        picked = set()
        for _ in range(5):
            quote = builder.build_hourly(11).quote
            picked.add(quote)
            self.state.remember_quote(quote)
        self.assertEqual(len(picked), 5, "直近のひとことが繰り返し選ばれている")

    def test_recent_quotes_in_the_state_are_still_read(self):
        # 記録済みの文は、build が state から読んで避けること。
        builder = self.make_builder()
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

    def test_closing_extra_text_is_optional_and_the_audio_files_are_required(self):
        """追加アナウンス（読み上げ）は optional=True、閉館アナウンスと蛍の光の
        音源ファイルは optional=False であること。

        追加アナウンスの WAV が欠けても、無音になるのはその 1 文だけで済ませたい。
        ``Player.play`` は欠けた optional セグメントを飛ばすが、必須セグメントが
        欠けると ``PlaybackError`` を送出する。読み上げを必須にすると、
        音声ファイル 1 つの欠落で閉館放送（蛍の光も含む）全体が鳴らなくなる。
        """
        self.config.data["closing"]["extra_text"] = "本日もご利用ありがとうございました。"
        plan = self.make_builder().build_closing()

        self.assertEqual(len(plan.segments), 3)
        announce, extra, music = plan.segments
        self.assertIn("閉館アナウンス", announce.label)
        self.assertIn("追加アナウンス", extra.label)
        self.assertIn("蛍の光", music.label)

        self.assertFalse(announce.optional, "閉館アナウンスは必須セグメントのはず")
        self.assertTrue(extra.optional, "追加アナウンス（読み上げ）は optional のはず")
        self.assertFalse(music.optional, "蛍の光は必須セグメントのはず")

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

    def test_weather_and_quote_both_failing_keep_the_time_signal_and_the_announcement(self):
        # 天気とひとこと、どちらも想定外の例外を出しても、時報音と時刻
        # アナウンスは残る。部品ごとに別々の警告が積まれる（1 つめの失敗が
        # 2 つめの組み立てを巻き込まない）。
        builder = self.make_builder(weather=ExplodingWeather(RuntimeError("想定外")))
        builder.quotes = ExplodingQuotes(RuntimeError("想定外"))
        plan = builder.build_hourly(12)
        self.assert_time_signal_first(plan)
        self.assertEqual(plan.spoken, ["正午をお知らせしたのだ。"])
        self.assertEqual(len(plan.segments), 2)
        self.assertIsNone(plan.quote)
        self.assertEqual(builder.quotes.calls, 1)
        self.assertTrue(any("天気予報の組み立てに失敗しました" in w for w in plan.warnings))
        self.assertTrue(any("ひとことの組み立てに失敗しました" in w for w in plan.warnings))

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
        return Event(key=key, kind=kind, hour=12,
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
