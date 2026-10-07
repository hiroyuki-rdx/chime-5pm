"""天気予報の取得・整形のテスト（ネットワークには接続しない）。"""

from __future__ import annotations

import hashlib
import http.client
import json
import unittest
import urllib.parse
from datetime import date
from unittest import mock

from tests.support import load_fixture, logs_enabled, urlopen_response

from chime.config import DEFAULT_CONFIG
from chime.weather import (WMO_CODES, WeatherError, WeatherService, build_sentences,
                           fetch_json, parse_open_meteo, prerecord_phrases)

WEATHER = DEFAULT_CONFIG["weather"]
TODAY = date(2026, 8, 26)

# 既定の地点は大津の 1 か所だけ（v5.1.0 までは大津・京都の 2 か所）。複数地点を
# 読み上げる仕組み（地点ごとの取得・キャッシュ・片方失敗時の続行・作り置きの
# 列挙）のテストは、既定値に頼らず 2 地点を明示した設定で確かめる。
WEATHER_TWO_LOCATIONS = dict(
    WEATHER,
    open_meteo=dict(WEATHER["open_meteo"], locations=[
        {"label": "大津", "latitude": 35.0045, "longitude": 135.8686},
        {"label": "京都", "latitude": 35.0116, "longitude": 135.7681},
    ]),
)

class ParseOpenMeteoTest(unittest.TestCase):
    """parse_open_meteo() のテスト。

    fixture（tests/fixtures/open_meteo.json）は current と daily に別々の
    天気コード・気温を持たせてある（current.weather_code=3「くもり」/
    daily.weather_code=61「弱い雨」、current.temperature_2m=28.3 /
    daily.temperature_2m_max=31.4）。取り違え（daily を読んでしまう）を
    機械的に検出できるようにするため。
    """

    def setUp(self):
        self.payload = load_fixture("open_meteo.json")

    def test_extracts_all_fields_from_current_and_daily(self):
        parts = parse_open_meteo(self.payload, {"label": "大津"}, TODAY)
        self.assertEqual(parts["when"], "今日")
        self.assertEqual(parts["weather"], "くもり")
        self.assertEqual(parts["temp"], 28)
        self.assertEqual(parts["temp_max"], 31)
        self.assertEqual(parts["temp_min"], 25)
        self.assertEqual(parts["pop"], 30)

    def test_weather_comes_from_current_not_daily(self):
        # 取り違えの検出: parts["weather"] は daily ではなく current の
        # 天気コード由来であること。
        daily_weather = WMO_CODES[self.payload["daily"]["weather_code"][0]]
        current_weather = WMO_CODES[self.payload["current"]["weather_code"]]
        self.assertNotEqual(current_weather, daily_weather)

        parts = parse_open_meteo(self.payload, {"label": "大津"}, TODAY)
        self.assertEqual(parts["weather"], current_weather)
        self.assertNotEqual(parts["weather"], daily_weather)

    def test_temp_comes_from_current_not_daily_max(self):
        # 取り違えの検出: parts["temp"] は current.temperature_2m 由来であり、
        # daily.temperature_2m_max（parts["temp_max"]）とは別の値であること。
        parts = parse_open_meteo(self.payload, {"label": "大津"}, TODAY)
        self.assertEqual(parts["temp"], 28)
        self.assertNotEqual(parts["temp"], parts["temp_max"])

    def test_temp_rounds_a_float_to_the_nearest_integer(self):
        payload = json.loads(json.dumps(self.payload))  # deep copy
        payload["current"]["temperature_2m"] = 28.6
        parts = parse_open_meteo(payload, {"label": "大津"}, TODAY)
        self.assertEqual(parts["temp"], 29)

    def test_negative_temp_rounds_correctly(self):
        payload = json.loads(json.dumps(self.payload))  # deep copy
        payload["current"]["temperature_2m"] = -3.6
        parts = parse_open_meteo(payload, {"label": "大津"}, TODAY)
        self.assertEqual(parts["temp"], -4)

    def test_unknown_current_weather_code_raises(self):
        payload = json.loads(json.dumps(self.payload))  # deep copy
        payload["current"]["weather_code"] = 999
        with self.assertRaises(WeatherError):
            parse_open_meteo(payload, {"label": "大津"}, TODAY)

    def test_missing_current_raises(self):
        payload = json.loads(json.dumps(self.payload))  # deep copy
        del payload["current"]
        with self.assertRaises(WeatherError):
            parse_open_meteo(payload, {"label": "大津"}, TODAY)

    def test_missing_daily_does_not_raise_and_leaves_forecast_fields_none(self):
        # daily は sentence_temp_max / sentence_pop の opt-in 用。current が
        # あれば daily が無くても現況の天気・気温は組み立てられる。
        payload = json.loads(json.dumps(self.payload))  # deep copy
        del payload["daily"]
        parts = parse_open_meteo(payload, {"label": "大津"}, TODAY)
        self.assertEqual(parts["weather"], "くもり")
        self.assertEqual(parts["temp"], 28)
        self.assertIsNone(parts["temp_max"])
        self.assertIsNone(parts["temp_min"])
        self.assertIsNone(parts["pop"])


class BuildSentencesTest(unittest.TestCase):
    """1 地点ぶんの parts から読み上げ文のリストを組み立てる build_sentences() のテスト。

    既定設定（WEATHER）は sentence_weather / sentence_temp（現況）だけが
    有効で、sentence_temp_max / sentence_pop は空文字列（opt-in）。
    """

    def test_default_settings_return_two_sentences_weather_and_temp(self):
        # 既定では sentence_temp_max / sentence_pop が空文字列なので、
        # temp_max / pop が来ていても文は出ない。
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": 28, "temp_max": 31, "pop": 30}
        sentences = build_sentences(parts, WEATHER)
        self.assertEqual(sentences, [
            "今の大津の天気はくもりなのだ。",
            "気温は28度なのだ。",
        ])

    def test_returns_up_to_four_sentences_in_order(self):
        # sentence_temp_max / sentence_pop を有効にすると、天気 → 気温 →
        # 最高気温 → 降水確率 の順で 4 文になること。
        settings = dict(WEATHER,
                        sentence_temp_max="最高気温は{temp_max}度なのだ。",
                        sentence_pop="降水確率は{pop}パーセントなのだ。")
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": 28, "temp_max": 31, "pop": 30}
        self.assertEqual(
            build_sentences(parts, settings),
            ["今の大津の天気はくもりなのだ。",
             "気温は28度なのだ。",
             "最高気温は31度なのだ。",
             "降水確率は30パーセントなのだ。"])

    def test_weather_sentence_is_always_present(self):
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": None, "temp_max": None, "pop": None}
        self.assertEqual(build_sentences(parts, WEATHER), ["今の大津の天気はくもりなのだ。"])

    def test_omits_optional_sentences_when_parts_are_none(self):
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": None, "temp_max": None, "pop": None}
        sentences = build_sentences(parts, WEATHER)
        self.assertEqual(sentences, ["今の大津の天気はくもりなのだ。"])

    def test_empty_template_suppresses_the_sentence(self):
        # 放送を短くしたい利用者向けに、テンプレートを空文字列にすると
        # その文自体が出ないこと。
        settings = dict(WEATHER, sentence_temp_max="最高気温は{temp_max}度なのだ。",
                        sentence_pop="")
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": 28, "temp_max": 31, "pop": 30}
        sentences = build_sentences(parts, settings)
        self.assertFalse(any("降水確率" in s for s in sentences))
        self.assertEqual(len(sentences), 3)  # 天気・気温・最高気温

    def test_all_templates_empty_yields_no_sentences(self):
        settings = dict(WEATHER, sentence_weather="", sentence_temp="",
                        sentence_temp_max="", sentence_pop="")
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": 28, "temp_max": 31, "pop": 30}
        self.assertEqual(build_sentences(parts, settings), [])

    def test_pop_is_rounded_to_the_configured_step_at_the_boundaries(self):
        # pop_step=10 の丸め境界: 23→20, 25→20（偶数丸め）, 26→30。
        settings = dict(WEATHER, sentence_pop="降水確率は{pop}パーセントなのだ。")
        for raw_pop, rounded in ((23, 20), (25, 20), (26, 30)):
            with self.subTest(raw_pop=raw_pop):
                parts = {"when": "今日", "label": "大津", "weather": "くもり",
                         "temp": None, "temp_max": None, "pop": raw_pop}
                sentences = build_sentences(parts, settings)
                self.assertEqual(sentences, ["今の大津の天気はくもりなのだ。",
                                             "降水確率は{0}パーセントなのだ。".format(rounded)])
                # 丸めは文の組み立て時だけで、parts["pop"] の生値は変更しない。
                self.assertEqual(parts["pop"], raw_pop)

    def test_non_positive_pop_step_disables_rounding(self):
        settings = dict(WEATHER, sentence_pop="降水確率は{pop}パーセントなのだ。",
                        prerecord=dict(WEATHER["prerecord"], pop_step=0))
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": None, "temp_max": None, "pop": 23}
        sentences = build_sentences(parts, settings)
        self.assertEqual(sentences[-1], "降水確率は23パーセントなのだ。")

    def test_non_numeric_pop_step_disables_rounding(self):
        settings = dict(WEATHER, sentence_pop="降水確率は{pop}パーセントなのだ。",
                        prerecord=dict(WEATHER["prerecord"], pop_step="毎"))
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": None, "temp_max": None, "pop": 23}
        sentences = build_sentences(parts, settings)
        self.assertEqual(sentences[-1], "降水確率は23パーセントなのだ。")

    def test_out_of_prerecord_range_still_builds_a_sentence(self):
        # temp_max=45 は prerecord.temp_max(40) の範囲外だが、例外にはならず
        # 文は組み立つ（作り置きが外れるだけで放送は止まらない）。
        settings = dict(WEATHER, sentence_temp_max="最高気温は{temp_max}度なのだ。")
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": None, "temp_max": 45, "pop": None}
        sentences = build_sentences(parts, settings)
        self.assertIn("最高気温は45度なのだ。", sentences)

    def test_custom_sentence_templates_are_respected(self):
        settings = dict(WEATHER, sentence_weather="{label}は{weather}。",
                        sentence_temp="", sentence_temp_max="", sentence_pop="降水{pop}%。")
        parts = {"when": "今日", "label": "大阪", "weather": "雨",
                 "temp": None, "temp_max": 30, "pop": 80}
        self.assertEqual(build_sentences(parts, settings), ["大阪は雨。", "降水80%。"])

    def test_weather_word_is_never_truncated(self):
        # {weather} は WMO_CODES の語（最長 10 文字）に限られ、長さの上限は持たない。
        # v6.0.0 までの max_weather_chars が設定に残っていても、語は切り詰めない。
        longest = max(WMO_CODES.values(), key=len)
        parts = {"when": "今日", "label": "大津", "weather": longest,
                 "temp": None, "temp_max": None, "pop": None}
        settings = dict(WEATHER, max_weather_chars=3)
        self.assertEqual(build_sentences(parts, settings),
                         ["今の大津の天気は{0}なのだ。".format(longest)])


class PrerecordPhrasesTest(unittest.TestCase):
    """作り置きすべき文言を全列挙する prerecord_phrases() のテスト。"""

    def test_enumerates_expected_totals(self):
        # 天気: 地点数(1) x whens(1) x WMO_CODES(28) = 28
        # 気温（現況）: temp_min(-5) 〜 temp_max(40) の 46 通り
        # 既定では sentence_temp_max / sentence_pop が空文字列のため、
        # 最高気温・降水確率は列挙されない。
        phrases = prerecord_phrases(WEATHER)
        self.assertEqual(len(phrases), 28 + 46)
        # 重複が無いこと
        self.assertEqual(len(set(phrases)), len(phrases))

    def test_enumerates_every_location_when_several_are_configured(self):
        # 天気: 地点数(2) x whens(1) x WMO_CODES(28) = 56。気温は地点によらず 46 通りのまま。
        phrases = prerecord_phrases(WEATHER_TWO_LOCATIONS)
        self.assertEqual(len(phrases), 56 + 46)
        self.assertEqual(len(set(phrases)), len(phrases))
        self.assertIn("今の大津の天気はくもりなのだ。", phrases)
        self.assertIn("今の京都の天気はくもりなのだ。", phrases)

    def test_covers_every_location_and_weather_code(self):
        phrases = set(prerecord_phrases(WEATHER))
        for location in WEATHER["open_meteo"]["locations"]:
            for when in WEATHER["prerecord"]["whens"]:
                for weather in WMO_CODES.values():
                    sentence = WEATHER["sentence_weather"].format(
                        when=when, label=location["label"], weather=weather)
                    self.assertIn(sentence, phrases)

    def test_covers_the_full_current_temp_range_inclusive(self):
        # sentence_temp（現況の気温）は既定で有効。
        phrases = set(prerecord_phrases(WEATHER))
        prerecord = WEATHER["prerecord"]
        self.assertIn(
            WEATHER["sentence_temp"].format(temp=prerecord["temp_min"]), phrases)
        self.assertIn(
            WEATHER["sentence_temp"].format(temp=prerecord["temp_max"]), phrases)

    def test_covers_pop_in_configured_steps_including_100(self):
        # sentence_pop は既定で無効（opt-in）なので、有効にして確認する。
        settings = dict(WEATHER, sentence_pop="降水確率は{pop}パーセントなのだ。")
        phrases = set(prerecord_phrases(settings))
        for pop in range(0, 101, WEATHER["prerecord"]["pop_step"]):
            self.assertIn(settings["sentence_pop"].format(pop=pop), phrases)

    def test_default_phrase_set_is_pinned(self):
        # 作り置きの音声は文言の完全一致で引く。1 文字でもずれるとその文は
        # 作り置きに無く、実行時の音声合成を持たない Pi では無音になる。
        # 天気の仕組みを整理しても語彙が変わっていないことを、件数・長さ・
        # sha1 で固定して確かめる（v6.0.0 の JMA 削除前に採取した値）。
        phrases = prerecord_phrases(DEFAULT_CONFIG["weather"])
        joined = "\n".join(phrases)
        self.assertEqual(len(phrases), 74)
        self.assertEqual(len(joined), 979)
        self.assertEqual(hashlib.sha1(joined.encode("utf-8")).hexdigest(),
                         "b521f2c84163bb1a5dec8a5b04cd3044d4d955ed")

    def test_order_is_stable_across_calls(self):
        self.assertEqual(prerecord_phrases(WEATHER), prerecord_phrases(WEATHER))

    def test_empty_template_excludes_that_kind_of_phrase(self):
        settings = dict(WEATHER, sentence_temp="")
        phrases = prerecord_phrases(settings)
        self.assertFalse(any("気温は" in p for p in phrases))
        self.assertEqual(len(phrases), 28)

    def test_non_positive_pop_step_yields_no_pop_phrases(self):
        settings = dict(WEATHER, sentence_pop="降水確率は{pop}パーセントなのだ。",
                        prerecord=dict(WEATHER["prerecord"], pop_step=0))
        phrases = prerecord_phrases(settings)
        self.assertFalse(any("パーセント" in p for p in phrases))

    def test_enabling_optional_templates_adds_their_phrases(self):
        # sentence_temp_max / sentence_pop に文言を入れると opt-in が効き、
        # prerecord_phrases() もその分を列挙すること。
        settings = dict(WEATHER,
                        sentence_temp_max="最高気温は{temp_max}度なのだ。",
                        sentence_pop="降水確率は{pop}パーセントなのだ。")
        phrases = prerecord_phrases(settings)
        # 天気28 + 気温(現況)46 + 最高気温46 + 降水確率11
        self.assertEqual(len(phrases), 28 + 46 + 46 + 11)
        self.assertTrue(any("最高気温" in p for p in phrases))
        self.assertTrue(any("パーセント" in p for p in phrases))

    def test_temp_and_temp_max_phrases_share_the_same_range(self):
        # 現況の気温と最高気温は、同じ temp_min 〜 temp_max（両端含む）を列挙する。
        settings = dict(WEATHER, sentence_temp_max="最高気温は{temp_max}度なのだ。",
                        prerecord=dict(WEATHER["prerecord"], temp_min=-1, temp_max=2))
        phrases = prerecord_phrases(settings)
        self.assertEqual([p for p in phrases if p.startswith("気温は")],
                         ["気温は{0}度なのだ。".format(v) for v in (-1, 0, 1, 2)])
        self.assertEqual([p for p in phrases if p.startswith("最高気温は")],
                         ["最高気温は{0}度なのだ。".format(v) for v in (-1, 0, 1, 2)])

    def test_numeric_strings_are_accepted_as_the_temp_range(self):
        settings = dict(WEATHER, sentence_temp_max="最高気温は{temp_max}度なのだ。",
                        prerecord=dict(WEATHER["prerecord"], temp_min="3", temp_max="5"))
        phrases = prerecord_phrases(settings)
        self.assertEqual([p for p in phrases if "気温は" in p],
                         ["気温は3度なのだ。", "気温は4度なのだ。", "気温は5度なのだ。",
                          "最高気温は3度なのだ。", "最高気温は4度なのだ。", "最高気温は5度なのだ。"])

    def test_invalid_temp_range_enumerates_no_temperature_phrases(self):
        # 数値にできない値・逆転した範囲では、気温の文を列挙しない（天気の文だけが残る）。
        # 例外にもならない（作り置きの列挙が止まると generate_voicevox が落ちる）。
        for bad in ({"temp_min": "x"}, {"temp_max": None}, {"temp_min": [1]},
                    {"temp_min": 5, "temp_max": 1}):
            with self.subTest(bad=bad):
                settings = dict(WEATHER, sentence_temp_max="最高気温は{temp_max}度なのだ。",
                                prerecord=dict(WEATHER["prerecord"], **bad))
                phrases = prerecord_phrases(settings)
                self.assertEqual(len(phrases), 28)
                self.assertFalse(any("気温" in p for p in phrases))

    def test_a_location_label_is_a_string_and_defaults_to_empty(self):
        # label が無い地点は空の地名、数値の label は文字列として扱う。
        # キャッシュのキーも、その文字列の label になる。
        locations = [{"latitude": 1, "longitude": 2},
                     {"label": 5, "latitude": 3, "longitude": 4}]
        settings = dict(WEATHER, open_meteo={"locations": locations})
        phrases = prerecord_phrases(settings)
        self.assertIn("今のの天気はくもりなのだ。", phrases)
        self.assertIn("今の5の天気はくもりなのだ。", phrases)

        service = WeatherService(settings)
        with mock.patch("chime.weather.fetch_json",
                        return_value=load_fixture("open_meteo.json")):
            service.describe_sentences(today=TODAY)
        self.assertEqual(sorted(service._cache), ["", "5"])


class NullToleranceTest(unittest.TestCase):
    """設定の ``None`` の扱い。箇所によって異なり、その差も観測できる。

    - ``open_meteo`` が ``None``: 地点を読む 3 か所（url・describe_sentences の
      取得先・prerecord_phrases）がどれも AttributeError。
    - ``prerecord`` が ``None``: prerecord_phrases は許容する（``or {}``）が、
      build_sentences は許容せず AttributeError になる。describe_sentences では
      それを _describe_one が WeatherError にし、その地点の天気は読まれない。
    """

    PARTS = {"when": "今日", "label": "大津", "weather": "くもり",
             "temp": 28, "temp_max": None, "pop": None}

    def test_open_meteo_none_raises_attribute_error_at_every_site(self):
        settings = dict(WEATHER, open_meteo=None)
        with self.assertRaises(AttributeError):
            WeatherService(settings).url()
        with mock.patch("chime.weather.fetch_json") as mocked:
            with self.assertRaises(AttributeError):
                WeatherService(settings).describe_sentences(today=TODAY)
        mocked.assert_not_called()
        with self.assertRaises(AttributeError):
            prerecord_phrases(settings)

    def test_open_meteo_without_locations_means_no_locations(self):
        # locations が None・空・キー無しなら「地点なし」（AttributeError にならない）。
        for open_meteo in ({"locations": None}, {"locations": []}, {}):
            with self.subTest(open_meteo=open_meteo):
                settings = dict(WEATHER, open_meteo=open_meteo)
                with self.assertRaises(WeatherError):
                    WeatherService(settings).url()
                # 天気の文は 0 件で、気温の 46 通りだけが残る。
                self.assertEqual(len(prerecord_phrases(settings)), 46)

    def test_prerecord_none_is_tolerated_by_prerecord_phrases(self):
        # whens は空、気温の範囲は既定の 0 〜 0 に落ちる。
        settings = dict(WEATHER, prerecord=None)
        self.assertEqual(prerecord_phrases(settings), ["気温は0度なのだ。"])

    def test_prerecord_none_is_not_tolerated_by_build_sentences(self):
        settings = dict(WEATHER, prerecord=None)
        with self.assertRaises(AttributeError):
            build_sentences(self.PARTS, settings)

    def test_prerecord_none_skips_that_location_in_describe_sentences(self):
        # 1 地点なら全地点で失敗、2 地点でもどちらも読めない（どちらも同じ設定のため）。
        for settings in (WEATHER, WEATHER_TWO_LOCATIONS):
            with self.subTest(locations=len(settings["open_meteo"]["locations"])):
                service = WeatherService(dict(settings, prerecord=None))
                with mock.patch("chime.weather.fetch_json",
                                return_value=load_fixture("open_meteo.json")):
                    with self.assertRaises(WeatherError) as caught:
                        service.describe_sentences(today=TODAY)
                self.assertIn("全地点で失敗", str(caught.exception))


class VocabularyCoverageTest(unittest.TestCase):
    """build_sentences() が組み立てうる全パターンが prerecord_phrases() に
    完全に含まれることを保証する（今回の改修の肝）。

    既定設定（sentence_weather + sentence_temp が有効、sentence_temp_max /
    sentence_pop は空文字列で無効）で実際に組み立てられる全パターン
    （地点(1) x whens(1) x WMO_CODES(28) x 現況気温の全値(46) = 1288 通り）を
    検証する。天気・気温の読み上げが作り置きから外れて無音にならないことの
    機械的な担保になる。sentence_temp の作り置きが 1 つ欠けると、この
    テストは落ちる（実際に確認済み）。
    """

    def test_every_combination_is_covered_by_prerecord_phrases(self):
        prerecord = WEATHER["prerecord"]
        universe = set(prerecord_phrases(WEATHER))

        locations = WEATHER["open_meteo"]["locations"]
        whens = prerecord["whens"]
        temps = range(prerecord["temp_min"], prerecord["temp_max"] + 1)

        combinations = 0
        for location in locations:
            for when in whens:
                for weather in WMO_CODES.values():
                    for temp in temps:
                        parts = {"when": when, "label": location["label"],
                                 "weather": weather, "temp": temp,
                                 "temp_max": None, "pop": None}
                        for sentence in build_sentences(parts, WEATHER):
                            self.assertIn(sentence, universe)
                        combinations += 1

        expected = len(locations) * len(whens) * len(WMO_CODES) * len(temps)
        self.assertEqual(combinations, expected)
        self.assertEqual(combinations, 1288)


class ServiceTest(unittest.TestCase):
    def test_provider_is_open_meteo(self):
        # --weather が表示するための定数。
        self.assertEqual(WeatherService(WEATHER).provider, "open_meteo")

    def test_url_defaults_to_the_first_configured_location(self):
        # 地点を指定しない呼び出し（--weather CLI の URL 表示）向け。
        service = WeatherService(WEATHER)
        url = service.url()
        self.assertTrue(url.startswith("https://api.open-meteo.com/v1/forecast?"))
        self.assertIn("latitude=35.0045", url)   # 大津（先頭）
        self.assertIn("longitude=135.8686", url)
        self.assertIn("timezone=Asia%2FTokyo", url)

    def test_url_accepts_an_explicit_location(self):
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        kyoto = WEATHER_TWO_LOCATIONS["open_meteo"]["locations"][1]
        url = service.url(kyoto)
        self.assertIn("latitude=35.0116", url)
        self.assertIn("longitude=135.7681", url)

    def test_url_includes_current_and_daily_parameters(self):
        # current（現況。読み上げの本体）と daily（opt-in 用）の両方が
        # 1 回の HTTP リクエストで問い合わせられること。
        service = WeatherService(WEATHER)
        url = service.url()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query.get("current"), ["weather_code,temperature_2m"])
        self.assertEqual(
            query.get("daily"),
            ["weather_code,temperature_2m_max,temperature_2m_min,"
             "precipitation_probability_max"])

    def test_url_without_locations_raises(self):
        service = WeatherService(dict(WEATHER, open_meteo={"locations": []}))
        with self.assertRaises(WeatherError):
            service.url()

    def test_describe_without_locations_raises(self):
        service = WeatherService(dict(WEATHER, open_meteo={"locations": []}))
        with mock.patch("chime.weather.fetch_json") as mocked:
            with self.assertRaises(WeatherError):
                service.describe_sentences(today=TODAY)
        mocked.assert_not_called()

    def test_disabled_service_raises(self):
        service = WeatherService(dict(WEATHER, enabled=False))
        with mock.patch("chime.weather.fetch_json") as mocked:
            with self.assertRaises(WeatherError):
                service.describe_sentences()
        mocked.assert_not_called()

    def test_cached_result_is_reused(self):
        service = WeatherService(dict(WEATHER, enabled=True))
        service._cache["大津"] = (float("inf"), date.today(), ["キャッシュされた予報なのだ。"])
        self.assertEqual(service.describe_sentences(), ["キャッシュされた予報なのだ。"])

    def test_cache_is_not_reused_across_a_date_change(self):
        # キャッシュ期限内でも、日付が変わっていれば前日分の文言を使い回さない。
        service = WeatherService(dict(WEATHER, enabled=True))
        service._cache["大津"] = (float("inf"), date(2026, 8, 25), ["昨日の天気なのだ。"])
        payload = load_fixture("open_meteo.json")
        with mock.patch("chime.weather.fetch_json", return_value=payload) as mocked:
            sentences = service.describe_sentences(today=TODAY)
        mocked.assert_called_once()
        self.assertEqual(sentences, ["今の大津の天気はくもりなのだ。", "気温は28度なのだ。"])
        # 取り直した文が今日の日付でキャッシュされ、次回は取りに行かない。
        with mock.patch("chime.weather.fetch_json") as mocked:
            self.assertEqual(service.describe_sentences(today=TODAY), sentences)
        mocked.assert_not_called()

    def test_cache_within_the_same_day_avoids_refetch(self):
        service = WeatherService(dict(WEATHER, enabled=True))
        service._cache["大津"] = (float("inf"), TODAY, ["本日分のキャッシュなのだ。"])
        with mock.patch("chime.weather.fetch_json") as mocked:
            sentences = service.describe_sentences(today=TODAY)
        mocked.assert_not_called()
        self.assertEqual(sentences, ["本日分のキャッシュなのだ。"])

    def test_use_cache_false_bypasses_a_fresh_cache(self):
        service = WeatherService(dict(WEATHER, enabled=True))
        service._cache["大津"] = (float("inf"), TODAY, ["本日分のキャッシュなのだ。"])
        payload = load_fixture("open_meteo.json")
        with mock.patch("chime.weather.fetch_json", return_value=payload) as mocked:
            sentences = service.describe_sentences(today=TODAY, use_cache=False)
        mocked.assert_called_once()
        self.assertNotEqual(sentences, ["本日分のキャッシュなのだ。"])

    def test_cache_is_per_location(self):
        # 大津のキャッシュが京都に流用されないこと。
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        service._cache["大津"] = (float("inf"), TODAY, ["大津のキャッシュ文なのだ。"])
        kyoto_payload = load_fixture("open_meteo_kyoto.json")
        with mock.patch("chime.weather.fetch_json", return_value=kyoto_payload) as mocked:
            sentences = service.describe_sentences(today=TODAY)
        # 大津はキャッシュを使うので、fetch_json は京都の分だけ呼ばれる。
        mocked.assert_called_once()
        self.assertEqual(sentences[0], "大津のキャッシュ文なのだ。")
        self.assertIn("京都", "".join(sentences[1:]))
        self.assertNotIn("大津のキャッシュ文なのだ。", sentences[1:])


class DescribeSentencesMultiLocationTest(unittest.TestCase):
    def setUp(self):
        self.otsu_payload = load_fixture("open_meteo.json")
        self.kyoto_payload = load_fixture("open_meteo_kyoto.json")

    def test_returns_four_sentences_in_otsu_then_kyoto_order(self):
        # 2 地点を設定し、sentence_weather + sentence_temp のみ有効（既定）なら、
        # 1 地点あたり 2 文（天気・気温）× 2 地点 = 4 文になる。
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        with mock.patch("chime.weather.fetch_json",
                        side_effect=[self.otsu_payload, self.kyoto_payload]):
            sentences = service.describe_sentences(today=TODAY)

        self.assertEqual(len(sentences), 4)
        self.assertEqual(sentences, [
            "今の大津の天気はくもりなのだ。",
            "気温は28度なのだ。",
            "今の京都の天気は弱い雨なのだ。",
            "気温は30度なのだ。",
        ])


class DescribeSentencesPartialFailureTest(unittest.TestCase):
    def setUp(self):
        self.kyoto_payload = load_fixture("open_meteo_kyoto.json")

    def test_one_location_failing_still_returns_the_other(self):
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        # tests/__init__.py がテスト全体でログを抑制している（logging.disable
        # (logging.CRITICAL)）ため、assertLogs で拾えるよう logs_enabled() で
        # このテストの間だけ一時的に解除する。
        with logs_enabled():
            with mock.patch("chime.weather.fetch_json",
                            side_effect=[WeatherError("圏外"), self.kyoto_payload]):
                with self.assertLogs("chime.weather", level="WARNING") as cm:
                    sentences = service.describe_sentences(today=TODAY)
        self.assertTrue(any("大津" in message for message in cm.output))
        self.assertEqual(len(sentences), 2)
        self.assertIn("京都", "".join(sentences))

    def test_one_location_missing_current_still_returns_the_other(self):
        # current が欠けた地点だけ飛ばされ、もう一方の地点の文は返ること。
        otsu_payload_without_current = json.loads(json.dumps(load_fixture("open_meteo.json")))
        del otsu_payload_without_current["current"]
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        with logs_enabled():
            with mock.patch("chime.weather.fetch_json",
                            side_effect=[otsu_payload_without_current, self.kyoto_payload]):
                with self.assertLogs("chime.weather", level="WARNING") as cm:
                    sentences = service.describe_sentences(today=TODAY)
        self.assertTrue(any("大津" in message for message in cm.output))
        self.assertEqual(len(sentences), 2)
        self.assertIn("京都", "".join(sentences))

    def test_the_other_order_also_returns_the_succeeding_location(self):
        otsu_payload = load_fixture("open_meteo.json")
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        with mock.patch("chime.weather.fetch_json",
                        side_effect=[otsu_payload, WeatherError("圏外")]):
            sentences = service.describe_sentences(today=TODAY)
        self.assertEqual(len(sentences), 2)
        self.assertIn("大津", "".join(sentences))

    def test_all_locations_failing_raises(self):
        service = WeatherService(WEATHER)
        with mock.patch("chime.weather.fetch_json", side_effect=WeatherError("圏外")):
            with self.assertRaises(WeatherError):
                service.describe_sentences(today=TODAY)


class FetchJsonTest(unittest.TestCase):
    """``fetch_json`` は通信まわりの失敗をすべて WeatherError にそろえる。

    ``http.client.IncompleteRead`` と ``BadStatusLine`` は ``OSError`` ではない
    ため、以前は素通りして放送の組み立てまで壊していた。``urlopen`` をモックし、
    ネットワークには接続しない。
    """

    URL = "https://example.invalid/forecast.json"

    def test_incomplete_read_becomes_weather_error(self):
        response = urlopen_response(read_error=http.client.IncompleteRead(b"{\"cur", 200))
        with mock.patch("chime.weather.urllib.request.urlopen", return_value=response):
            with self.assertRaises(WeatherError):
                fetch_json(self.URL, 8.0)

    def test_bad_status_line_becomes_weather_error(self):
        with mock.patch("chime.weather.urllib.request.urlopen",
                        side_effect=http.client.BadStatusLine("")):
            with self.assertRaises(WeatherError):
                fetch_json(self.URL, 8.0)

    def test_other_http_exceptions_become_weather_error(self):
        # IncompleteRead / BadStatusLine の親クラス（HTTPException）ごと受ける。
        with mock.patch("chime.weather.urllib.request.urlopen",
                        side_effect=http.client.RemoteDisconnected("切断された")):
            with self.assertRaises(WeatherError):
                fetch_json(self.URL, 8.0)
        with mock.patch("chime.weather.urllib.request.urlopen",
                        side_effect=http.client.LineTooLong("status line")):
            with self.assertRaises(WeatherError):
                fetch_json(self.URL, 8.0)

    def test_value_error_becomes_weather_error(self):
        with mock.patch("chime.weather.urllib.request.urlopen",
                        side_effect=ValueError("unknown url type")):
            with self.assertRaises(WeatherError):
                fetch_json(self.URL, 8.0)

    def test_malformed_url_becomes_weather_error(self):
        with self.assertRaises(WeatherError):
            fetch_json("not-a-url", 8.0)

    def test_the_error_message_names_the_exception_type(self):
        # BadStatusLine の文言は空の引用符だけで、型名が無いと原因を読み取れない。
        with mock.patch("chime.weather.urllib.request.urlopen",
                        side_effect=http.client.BadStatusLine("")):
            with self.assertRaises(WeatherError) as caught:
                fetch_json(self.URL, 8.0)
        self.assertIn("BadStatusLine", str(caught.exception))

    def test_a_valid_response_is_still_parsed(self):
        with mock.patch("chime.weather.urllib.request.urlopen",
                        return_value=urlopen_response({"ok": True})):
            self.assertEqual(fetch_json(self.URL, 8.0), {"ok": True})


class LeftoverJmaSettingsTest(unittest.TestCase):
    """v6.0.0 で廃止した旧設定（provider / jma / max_weather_chars）が残っていても無視する。

    config.py が廃止キーを取り除くのは別の段階であり、天気の処理は単体でも
    それらに左右されてはならない（利用者の config.json に古い設定が残っていても
    取得先・文言・作り置きの語彙が変わらないこと）。
    """

    REMOVED_KEYS = ("provider", "jma", "max_weather_chars")
    LEFTOVER = {
        "provider": "jma",
        "jma": {"area_code": "130000", "area_name": "東京地方", "temp_area_name": "",
                "label": "東京", "drop_after": ["所により"]},
        "max_weather_chars": 10,
    }

    def settings(self, **extra):
        clean = {k: v for k, v in WEATHER.items() if k not in self.REMOVED_KEYS}
        return dict(clean, **dict(self.LEFTOVER, **extra))

    def test_provider_stays_open_meteo(self):
        for provider in ("jma", "magic-8-ball", ""):
            with self.subTest(provider=provider):
                service = WeatherService(self.settings(provider=provider))
                self.assertEqual(service.provider, "open_meteo")

    def test_url_is_the_open_meteo_url(self):
        service = WeatherService(self.settings())
        url = service.url()
        self.assertTrue(url.startswith("https://api.open-meteo.com/v1/forecast?"))
        self.assertNotIn("jma.go.jp", url)
        self.assertIn("latitude=35.0045", url)
        self.assertIn("longitude=135.8686", url)

    def test_describe_sentences_reads_open_meteo_through_urlopen(self):
        service = WeatherService(self.settings())
        payload = load_fixture("open_meteo.json")
        with mock.patch("chime.weather.urllib.request.urlopen",
                        return_value=urlopen_response(payload)) as urlopen:
            sentences = service.describe_sentences(today=TODAY)
        self.assertEqual(sentences, ["今の大津の天気はくもりなのだ。", "気温は28度なのだ。"])
        urlopen.assert_called_once()
        requested = urlopen.call_args[0][0]
        self.assertEqual(requested.full_url, service.url())

    def test_prerecord_phrases_do_not_change(self):
        clean = {k: v for k, v in WEATHER.items() if k not in self.REMOVED_KEYS}
        self.assertEqual(prerecord_phrases(self.settings()), prerecord_phrases(clean))


class DescribeSentencesDegradeTest(unittest.TestCase):
    """1 地点の失敗（通信・解析・文の組み立て）が、他の地点を巻き込まないこと。"""

    def setUp(self):
        self.otsu_payload = load_fixture("open_meteo.json")
        self.kyoto_payload = load_fixture("open_meteo_kyoto.json")

    def test_incomplete_read_on_one_location_keeps_the_other(self):
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        responses = [
            urlopen_response(read_error=http.client.IncompleteRead(b"{", 500)),
            urlopen_response(self.kyoto_payload),
        ]
        with mock.patch("chime.weather.urllib.request.urlopen", side_effect=responses):
            sentences = service.describe_sentences(today=TODAY)
        self.assertEqual(len(sentences), 2)
        self.assertIn("京都", "".join(sentences))

    def test_bad_status_line_on_one_location_keeps_the_other(self):
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        responses = [urlopen_response(self.otsu_payload), http.client.BadStatusLine("")]
        with mock.patch("chime.weather.urllib.request.urlopen", side_effect=responses):
            sentences = service.describe_sentences(today=TODAY)
        self.assertEqual(len(sentences), 2)
        self.assertIn("大津", "".join(sentences))

    def test_incomplete_read_on_every_location_raises_weather_error(self):
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        response = urlopen_response(read_error=http.client.IncompleteRead(b"", 1))
        with mock.patch("chime.weather.urllib.request.urlopen", return_value=response):
            with self.assertRaises(WeatherError):
                service.describe_sentences(today=TODAY)

    def test_unknown_placeholder_in_sentence_temp_becomes_weather_error(self):
        # config.json の書き間違い（{temp} を {degrees} と書いた等）は KeyError になる。
        service = WeatherService(dict(
            WEATHER, sentence_temp="気温は{degrees}度なのだ。"))
        with mock.patch("chime.weather.fetch_json", return_value=self.otsu_payload):
            with self.assertRaises(WeatherError):
                service.describe_sentences(today=TODAY)

    def test_every_sentence_template_typo_becomes_weather_error(self):
        # 既定で空の sentence_temp_max / sentence_pop も、有効にしたうえで
        # 書き間違えた場合に同じく WeatherError になること。
        typos = {
            "sentence_weather": "今の{place}の天気は{weather}なのだ。",
            "sentence_temp": "気温は{degrees}度なのだ。",
            "sentence_temp_max": "最高気温は{max}度なのだ。",
            "sentence_pop": "降水確率は{percent}パーセントなのだ。",
        }
        for key, template in typos.items():
            with self.subTest(key=key):
                settings = dict(
                    WEATHER,
                    sentence_temp_max="最高気温は{temp_max}度なのだ。",
                    sentence_pop="降水確率は{pop}パーセントなのだ。")
                settings[key] = template
                service = WeatherService(settings)
                with mock.patch("chime.weather.fetch_json", return_value=self.otsu_payload):
                    with self.assertRaises(WeatherError):
                        service.describe_sentences(today=TODAY)

    def test_positional_or_broken_placeholder_becomes_weather_error(self):
        # {0}（IndexError）と閉じていない {（ValueError）。
        for template in ("気温は{0}度なのだ。", "気温は{temp度なのだ。"):
            with self.subTest(template=template):
                service = WeatherService(dict(
                    WEATHER, sentence_temp=template))
                with mock.patch("chime.weather.fetch_json", return_value=self.otsu_payload):
                    with self.assertRaises(WeatherError):
                        service.describe_sentences(today=TODAY)

    def test_unexpected_parse_errors_become_weather_error(self):
        # 解析の途中で出た想定外の例外も、1 地点の失敗として WeatherError にそろえる。
        for error in (KeyError("x"), IndexError("x"), ValueError("x"),
                      TypeError("x"), AttributeError("x")):
            with self.subTest(error=type(error).__name__):
                service = WeatherService(WEATHER)
                with mock.patch("chime.weather.fetch_json", return_value=self.otsu_payload), \
                        mock.patch("chime.weather.parse_open_meteo", side_effect=error):
                    with self.assertRaises(WeatherError):
                        service.describe_sentences(today=TODAY)

    def test_a_parse_error_on_one_location_keeps_the_other(self):
        service = WeatherService(WEATHER_TWO_LOCATIONS)
        real_parse = parse_open_meteo
        calls = []

        def flaky_parse(payload, settings, today):
            calls.append(settings.get("label"))
            if len(calls) == 1:
                raise KeyError("current")
            return real_parse(payload, settings, today)

        with mock.patch("chime.weather.fetch_json",
                        side_effect=[self.otsu_payload, self.kyoto_payload]), \
                mock.patch("chime.weather.parse_open_meteo", side_effect=flaky_parse):
            sentences = service.describe_sentences(today=TODAY)
        self.assertEqual(len(sentences), 2)
        self.assertIn("京都", "".join(sentences))

    def test_weather_error_from_fetch_is_not_rewrapped(self):
        # 既に WeatherError になっているものは、そのまま（メッセージを保って）通す。
        service = WeatherService(WEATHER)
        with mock.patch("chime.weather.fetch_json", side_effect=WeatherError("圏外です")):
            with self.assertRaises(WeatherError) as caught:
                service.describe_sentences(today=TODAY)
        self.assertIn("全地点で失敗", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
