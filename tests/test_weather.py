"""天気予報の取得・整形のテスト（ネットワークには接続しない）。"""

from __future__ import annotations

import json
import logging
import unittest
import urllib.parse
from datetime import date
from unittest import mock

from tests.support import load_fixture  # noqa: F401

from chime.config import DEFAULT_CONFIG
from chime.weather import (WMO_CODES, WeatherError, WeatherService, build_sentences,
                           build_text, drop_after_markers, normalize_weather_text,
                           parse_jma, parse_open_meteo, prerecord_phrases,
                           truncate_weather_text)

WEATHER = DEFAULT_CONFIG["weather"]
TODAY = date(2026, 8, 26)

# 既定値は滋賀（work 1）に変わったため、東京 fixture を使う既存の後方互換テストは
# 従来の東京設定を明示的に持たせる（DEFAULT_CONFIG の変化から切り離す）。
TOKYO_JMA = {
    "area_code": "130000",
    "area_name": "東京地方",
    "temp_area_name": "",
    "label": "東京",
}

# jma は現況（current）を持たないため、既定の sentence_weather（「今の…」・
# {when} を使わない現況向けの文言）とは噛み合わない。jma 由来の parts を
# build_text / build_sentences に渡すテストでは、chime/config.py のコメントが
# 指示するとおり、予報向けの言い回し（{when} を使い、sentence_temp_max /
# sentence_pop を有効にする）を明示的に指定する。これにより DEFAULT_CONFIG
# 側の既定値（現況向け）が変わっても、jma のテストの意図はそのまま保たれる。
JMA_SENTENCES = dict(
    WEATHER,
    sentence_weather="{when}の{label}の天気は{weather}なのだ。",
    sentence_temp="",
    sentence_temp_max="最高気温は{temp_max}度なのだ。",
    sentence_pop="降水確率は{pop}パーセントなのだ。",
)


class NormalizeTest(unittest.TestCase):
    # 気象庁の全角スペースは形態素の境界を表す。単純に削除すると Open JTalk
    # （MeCab）の形態素解析が崩れて読み上げが崩壊するため（実測で 23 秒・
    # 読み崩れを確認済み）、削除ではなく読点「、」に置き換えて区切りを保つ。
    def test_converts_full_width_spaces_to_touten(self):
        self.assertEqual(normalize_weather_text("くもり　時々　晴れ"), "くもり、時々、晴れ")

    def test_converts_ascii_spaces_to_touten(self):
        self.assertEqual(normalize_weather_text("晴れ のち 雨"), "晴れ、のち、雨")

    def test_collapses_consecutive_spaces_into_one_touten(self):
        self.assertEqual(normalize_weather_text("晴れ　　のち　雨"), "晴れ、のち、雨")

    def test_strips_leading_and_trailing_spaces(self):
        self.assertEqual(normalize_weather_text("　晴れ　"), "晴れ")


class DropAfterMarkersTest(unittest.TestCase):
    def test_drops_text_from_the_first_marker(self):
        text = "晴れ、夜のはじめ頃、くもり、所により、夜のはじめ頃、まで、雨"
        self.assertEqual(drop_after_markers(text, ["所により"]), "晴れ、夜のはじめ頃、くもり")

    def test_empty_marker_list_drops_nothing(self):
        text = "晴れ、所により、くもり"
        self.assertEqual(drop_after_markers(text, []), text)
        self.assertEqual(drop_after_markers(text, None), text)

    def test_marker_not_found_keeps_text_as_is(self):
        text = "晴れ時々くもり"
        self.assertEqual(drop_after_markers(text, ["所により"]), text)

    def test_earliest_matching_marker_wins(self):
        text = "晴れ、のち、雨、時々、くもり"
        self.assertEqual(drop_after_markers(text, ["くもり", "のち"]), "晴れ")

    def test_marker_at_start_does_not_truncate_to_empty(self):
        # 予報文が「所により」で始まる境界ケース。切り捨てた結果が空文字列に
        # なる場合は、壊れた文を読み上げるより、切り捨てずに元の文をそのまま
        # 使うほうが害が小さい。
        text = "所により、雨"
        self.assertEqual(drop_after_markers(text, ["所により"]), text)

    def test_marker_matching_entire_text_does_not_truncate_to_empty(self):
        text = "所により"
        self.assertEqual(drop_after_markers(text, ["所により"]), text)

    def test_empty_input_stays_empty(self):
        # 元々空文字列なら、切り捨てても空のまま(フォールバック対象にはならない)。
        self.assertEqual(drop_after_markers("", ["所により"]), "")


class TruncateWeatherTextTest(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(truncate_weather_text("晴れ時々くもり", 40), "晴れ時々くもり")

    def test_truncates_at_the_touten_boundary(self):
        text = "あ、いうえお、かきくけこ、さしすせそ"
        # 10 文字目までに含まれる最後の読点で切る（語の途中では切らない）
        self.assertEqual(truncate_weather_text(text, 10), "あ、いうえお")

    def test_no_touten_available_leaves_text_unchanged(self):
        # 途中で不自然にぶつ切りにするよりは、切らないほうが安全
        self.assertEqual(truncate_weather_text("あいうえおかきくけこ", 5), "あいうえおかきくけこ")

    def test_non_positive_limit_disables_truncation(self):
        self.assertEqual(truncate_weather_text("あ、い、う", 0), "あ、い、う")


class ParseJmaTest(unittest.TestCase):
    def setUp(self):
        self.payload = load_fixture("jma_130000.json")

    def test_extracts_todays_forecast(self):
        # 「くもり　時々　晴れ」の全角スペースは読点に変換される
        # （削除すると Open JTalk の形態素解析が崩れて読み上げが崩壊するため）。
        parts = parse_jma(self.payload, TOKYO_JMA, TODAY)
        self.assertEqual(parts["when"], "今日")
        self.assertEqual(parts["weather"], "くもり、時々、晴れ")
        self.assertEqual(parts["label"], "東京")

    def test_picks_configured_area(self):
        settings = dict(TOKYO_JMA, area_name="伊豆諸島北部", label="")
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["weather"], "くもり")

    def test_falls_back_to_first_area(self):
        settings = dict(TOKYO_JMA, area_name="存在しない地域", label="")
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["weather"], "くもり、時々、晴れ")

    def test_extracts_max_temperature(self):
        parts = parse_jma(self.payload, TOKYO_JMA, TODAY)
        self.assertEqual(parts["temp_max"], 31)

    def test_missing_min_temperature_is_none(self):
        # 昼発表の予報には当日の最低気温が含まれない
        parts = parse_jma(self.payload, TOKYO_JMA, TODAY)
        self.assertIsNone(parts["temp_min"])

    def test_tomorrow_has_both_temperatures(self):
        parts = parse_jma(self.payload, TOKYO_JMA, date(2026, 8, 27))
        self.assertEqual(parts["when"], "今日")
        self.assertEqual((parts["temp_min"], parts["temp_max"]), (25, 33))

    def test_uses_max_precipitation_of_the_day(self):
        parts = parse_jma(self.payload, TOKYO_JMA, TODAY)
        self.assertEqual(parts["pop"], 30)

    def test_relative_label_for_future_date(self):
        parts = parse_jma(self.payload, TOKYO_JMA, date(2026, 8, 25))
        self.assertEqual(parts["when"], "明日")

    def test_rejects_unexpected_payloads(self):
        for payload in ({}, [], [{"timeSeries": []}], "nonsense"):
            with self.assertRaises(WeatherError):
                parse_jma(payload, TOKYO_JMA, TODAY)

    def test_does_not_depend_on_timeseries_order(self):
        # 気象庁 API は timeSeries の並び順（weathers/pops/temps）を保証しない。
        # 順序を入れ替えても同じ結果になること。
        payload = json.loads(json.dumps(self.payload))  # deep copy
        payload[0]["timeSeries"] = list(reversed(payload[0]["timeSeries"]))
        parts = parse_jma(payload, TOKYO_JMA, TODAY)
        expected = parse_jma(self.payload, TOKYO_JMA, TODAY)
        self.assertEqual(parts, expected)

    def test_temp_area_name_falls_back_to_first_when_missing_from_settings(self):
        # 東京 fixture の気温地点名は「東京」で area_name「東京地方」の前方一致に
        # 引っかかるため、temp_area_name が無くても従来どおり正しく選べる
        # （後方互換の回帰検出）。
        settings = dict(TOKYO_JMA)
        del settings["temp_area_name"]
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["temp_max"], 31)


class ParseJmaShigaTest(unittest.TestCase):
    """滋賀（南部/北部・大津/彦根）の fixture を使った、地域選択のテスト。"""

    def setUp(self):
        self.payload = load_fixture("jma_250000.json")

    def test_temp_area_name_selects_the_specified_observation_point(self):
        # 気温の観測地点名（大津／彦根）は天気の細分区域名（南部／北部）とは
        # 体系が異なるため、temp_area_name で明示的に選べること。
        settings = dict(WEATHER["jma"], area_name="南部", temp_area_name="彦根", label="滋賀")
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["temp_max"], 24)  # 彦根の気温（大津なら27）

    def test_temp_area_name_empty_uses_area_name_for_backward_compat(self):
        # temp_area_name が空文字列なら、従来どおり area_name で気温地点を選ぼうと
        # する。area_name「北部」は気温の地点名（大津／彦根）と一致しないため、
        # 先頭（大津）にフォールバックする（安全側に倒れる既存挙動を維持）。
        settings = dict(WEATHER["jma"], area_name="北部", temp_area_name="", label="滋賀")
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["weather"], "雨")  # 北部の天気
        self.assertEqual(parts["temp_max"], 27)   # 大津（先頭）にフォールバック

    def test_area_name_hokubu_selects_hokubu_weather(self):
        settings = dict(WEATHER["jma"], area_name="北部", temp_area_name="彦根", label="滋賀")
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["weather"], "雨")

    def test_default_settings_describe_todays_shiga_weather(self):
        # jma は現況を持たないため、予報向けの言い回し（JMA_SENTENCES。
        # chime/config.py のコメント参照）で読み上げ文が意図どおり組み立つこと。
        # 「晴れ　時々　くもり」の全角スペースは読点に変換される（スペース削除
        # だと Open JTalk の形態素解析が崩れて読み上げが崩壊するため）。
        # sentence_weather / sentence_temp_max / sentence_pop の 3 文が
        # 連結される（temp_min は読み上げ対象に含まれない）。
        parts = parse_jma(self.payload, WEATHER["jma"], TODAY)
        text = build_text(parts, JMA_SENTENCES)
        self.assertEqual(
            text,
            "今日の滋賀の天気は晴れ、時々、くもりなのだ。"
            "最高気温は27度なのだ。降水確率は20パーセントなのだ。")

    def test_drops_region_specific_caveat_by_default(self):
        # 利用者が実際に受け取った予報文の再現。「所により」以降（地域限定の
        # 但し書き）を落とさないと、全角スペースを読点に変換しただけでも
        # 15 秒超になり、館内放送としては長すぎる。
        payload = json.loads(json.dumps(self.payload))  # deep copy
        long_forecast = ("晴れ　夜のはじめ頃　くもり　所により　"
                          "夜のはじめ頃　まで　雨で雷を伴い　激しく　降る")
        for series in payload[0]["timeSeries"]:
            for area in series.get("areas", []):
                if "weathers" in area and area["area"]["name"] == "南部":
                    area["weathers"][0] = long_forecast

        parts = parse_jma(payload, WEATHER["jma"], TODAY)
        self.assertEqual(parts["weather"], "晴れ、夜のはじめ頃、くもり")

        text = build_text(parts, JMA_SENTENCES)
        self.assertEqual(
            text,
            "今日の滋賀の天気は晴れ、夜のはじめ頃、くもりなのだ。"
            "最高気温は27度なのだ。降水確率は20パーセントなのだ。")

    def test_drop_after_can_be_disabled_via_settings(self):
        payload = json.loads(json.dumps(self.payload))  # deep copy
        long_forecast = "晴れ　所により　くもり"
        for series in payload[0]["timeSeries"]:
            for area in series.get("areas", []):
                if "weathers" in area and area["area"]["name"] == "南部":
                    area["weathers"][0] = long_forecast

        settings = dict(WEATHER["jma"], drop_after=[])
        parts = parse_jma(payload, settings, TODAY)
        self.assertEqual(parts["weather"], "晴れ、所により、くもり")

    def _with_south_weather(self, weather_text):
        payload = json.loads(json.dumps(self.payload))  # deep copy
        for series in payload[0]["timeSeries"]:
            for area in series.get("areas", []):
                if "weathers" in area and area["area"]["name"] == "南部":
                    area["weathers"][0] = weather_text
        return payload

    def test_forecast_starting_with_marker_is_not_truncated_to_empty(self):
        # 境界ケース: 予報文が但し書き「所により」で始まると、素直に切り捨てる
        # と結果が空文字列になり、読み上げ文が「天気はなのだ。」のように壊れる。
        # 壊れた文より多少長い文のほうが害が小さいため、切り捨てずに使う。
        payload = self._with_south_weather("所により　雨")
        parts = parse_jma(payload, WEATHER["jma"], TODAY)
        self.assertEqual(parts["weather"], "所により、雨")
        text = build_text(parts, JMA_SENTENCES)
        self.assertNotIn("の天気はなのだ。", text)
        self.assertEqual(
            text,
            "今日の滋賀の天気は所により、雨なのだ。"
            "最高気温は27度なのだ。降水確率は20パーセントなのだ。")

    def test_forecast_that_is_only_the_marker_is_not_truncated_to_empty(self):
        payload = self._with_south_weather("所により")
        parts = parse_jma(payload, WEATHER["jma"], TODAY)
        self.assertEqual(parts["weather"], "所により")
        text = build_text(parts, JMA_SENTENCES)
        self.assertNotIn("の天気はなのだ。", text)

    def test_empty_forecast_text_raises_weather_error(self):
        # 予報文そのものが空（または空白のみ）なら天気情報が実質無いということ。
        # build_sentences で壊れた文を組み立てるのではなく、ここで WeatherError を
        # 送出し、呼び出し側（chime.sequence）で「ひとこと」に切り替えてもらう。
        for empty_text in ("", "　", "  "):
            payload = self._with_south_weather(empty_text)
            with self.assertRaises(WeatherError):
                parse_jma(payload, WEATHER["jma"], TODAY)


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

    def test_jma_weather_text_is_still_truncated_at_max_weather_chars(self):
        # provider="jma" の {weather} は自由文なので、max_weather_chars による
        # 切り詰めは build_sentences でも従来どおり適用されること。jma は
        # {when} を使う予報向けの言い回しを使う（JMA_SENTENCES 参照）。
        settings = dict(JMA_SENTENCES, max_weather_chars=10)
        parts = {"when": "今日", "label": "滋賀",
                 "weather": "晴れ、夜のはじめ頃、くもり、まだまだ、続く、長い、予報、文",
                 "temp": None, "temp_max": None, "pop": None}
        self.assertEqual(build_sentences(parts, settings),
                         ["今日の滋賀の天気は晴れ、夜のはじめ頃なのだ。"])


class BuildTextTest(unittest.TestCase):
    """build_text() は "".join(build_sentences(...)) の後方互換ラッパー。"""

    def test_matches_the_join_of_build_sentences(self):
        parts = {"when": "今日", "label": "大津", "weather": "くもり",
                 "temp": 28, "temp_max": 31, "pop": 30}
        self.assertEqual(build_text(parts, WEATHER), "".join(build_sentences(parts, WEATHER)))

    def test_full_sentence(self):
        settings = dict(WEATHER,
                        sentence_temp_max="最高気温は{temp_max}度なのだ。",
                        sentence_pop="降水確率は{pop}パーセントなのだ。")
        parts = {"when": "今日", "label": "東京", "weather": "晴れ",
                 "temp": 25, "temp_max": 30, "pop": 10}
        self.assertEqual(
            build_text(parts, settings),
            "今の東京の天気は晴れなのだ。気温は25度なのだ。"
            "最高気温は30度なのだ。降水確率は10パーセントなのだ。")

    def test_omits_missing_details(self):
        parts = {"when": "今日", "label": "東京", "weather": "晴れ",
                 "temp": None, "temp_max": None, "pop": None}
        self.assertEqual(build_text(parts, WEATHER), "今の東京の天気は晴れなのだ。")

    def test_custom_sentence_templates_are_respected(self):
        settings = dict(WEATHER, sentence_weather="{label}は{weather}。",
                        sentence_temp="", sentence_temp_max="", sentence_pop="降水{pop}%。")
        parts = {"when": "今日", "label": "大阪", "weather": "雨",
                 "temp": None, "temp_max": 30, "pop": 80}
        self.assertEqual(build_text(parts, settings), "大阪は雨。降水80%。")


class PrerecordPhrasesTest(unittest.TestCase):
    """作り置きすべき文言を全列挙する prerecord_phrases() のテスト。"""

    def test_enumerates_expected_totals(self):
        # 天気: 地点数(2) x whens(1) x WMO_CODES(28) = 56
        # 気温（現況）: temp_min(-5) 〜 temp_max(40) の 46 通り
        # 既定では sentence_temp_max / sentence_pop が空文字列のため、
        # 最高気温・降水確率は列挙されない。
        phrases = prerecord_phrases(WEATHER)
        self.assertEqual(len(phrases), 56 + 46)
        # 重複が無いこと
        self.assertEqual(len(set(phrases)), len(phrases))

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

    def test_order_is_stable_across_calls(self):
        self.assertEqual(prerecord_phrases(WEATHER), prerecord_phrases(WEATHER))

    def test_empty_template_excludes_that_kind_of_phrase(self):
        settings = dict(WEATHER, sentence_temp="")
        phrases = prerecord_phrases(settings)
        self.assertFalse(any("気温は" in p for p in phrases))
        self.assertEqual(len(phrases), 56)

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
        # 天気56 + 気温(現況)46 + 最高気温46 + 降水確率11
        self.assertEqual(len(phrases), 56 + 46 + 46 + 11)
        self.assertTrue(any("最高気温" in p for p in phrases))
        self.assertTrue(any("パーセント" in p for p in phrases))


class VocabularyCoverageTest(unittest.TestCase):
    """build_sentences() が組み立てうる全パターンが prerecord_phrases() に
    完全に含まれることを保証する（今回の改修の肝）。

    既定設定（sentence_weather + sentence_temp が有効、sentence_temp_max /
    sentence_pop は空文字列で無効）で実際に組み立てられる全パターン
    （地点(2) x whens(1) x WMO_CODES(28) x 現況気温の全値(46) = 2576 通り）を
    検証する。天気・気温の読み上げが Open JTalk（男性声）へ落ちないことの
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
        self.assertEqual(combinations, 2576)


class ServiceTest(unittest.TestCase):
    def test_jma_url(self):
        service = WeatherService(dict(WEATHER, provider="jma"))
        self.assertEqual(
            service.url(),
            "https://www.jma.go.jp/bosai/forecast/data/forecast/250000.json")

    def test_open_meteo_url_defaults_to_the_first_configured_location(self):
        # 地点を指定しない呼び出し（--weather CLI など）との後方互換。
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        url = service.url()
        self.assertIn("latitude=35.0045", url)   # 大津（先頭）
        self.assertIn("longitude=135.8686", url)
        self.assertIn("timezone=Asia%2FTokyo", url)

    def test_open_meteo_url_accepts_an_explicit_location(self):
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        kyoto = WEATHER["open_meteo"]["locations"][1]
        url = service.url(kyoto)
        self.assertIn("latitude=35.0116", url)
        self.assertIn("longitude=135.7681", url)

    def test_open_meteo_url_includes_current_and_daily_parameters(self):
        # current（現況。読み上げの本体）と daily（opt-in 用）の両方が
        # 1 回の HTTP リクエストで問い合わせられること。
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        url = service.url()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query.get("current"), ["weather_code,temperature_2m"])
        self.assertEqual(
            query.get("daily"),
            ["weather_code,temperature_2m_max,temperature_2m_min,"
             "precipitation_probability_max"])

    def test_unknown_provider_raises(self):
        service = WeatherService(dict(WEATHER, provider="magic-8-ball"))
        with self.assertRaises(WeatherError):
            service.url()

    def test_disabled_service_raises(self):
        service = WeatherService(dict(WEATHER, enabled=False))
        with self.assertRaises(WeatherError):
            service.describe()

    def test_cached_result_is_reused(self):
        service = WeatherService(dict(WEATHER, provider="jma", enabled=True))
        service._cache["jma"] = (float("inf"), date.today(), ["キャッシュされた予報なのだ。"])
        self.assertEqual(service.describe(), "キャッシュされた予報なのだ。")

    def test_cache_is_not_reused_across_a_date_change(self):
        # キャッシュ期限内でも、日付が変わっていれば前日分の文言を使い回さない。
        service = WeatherService(dict(WEATHER, provider="jma", enabled=True))
        service._cache["jma"] = (float("inf"), date(2026, 8, 25), ["昨日の天気なのだ。"])
        payload = load_fixture("jma_130000.json")
        with mock.patch("chime.weather.fetch_json", return_value=payload) as mocked:
            text = service.describe(today=TODAY)
        mocked.assert_called_once()
        self.assertNotEqual(text, "昨日の天気なのだ。")

    def test_cache_within_the_same_day_avoids_refetch(self):
        service = WeatherService(dict(WEATHER, provider="jma", enabled=True))
        service._cache["jma"] = (float("inf"), TODAY, ["本日分のキャッシュなのだ。"])
        with mock.patch("chime.weather.fetch_json") as mocked:
            text = service.describe(today=TODAY)
        mocked.assert_not_called()
        self.assertEqual(text, "本日分のキャッシュなのだ。")

    def test_parse_dispatches_by_provider(self):
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        parts = service.parse(load_fixture("open_meteo.json"), TODAY)
        self.assertEqual(parts["weather"], "くもり")

    def test_cache_is_per_location(self):
        # 大津のキャッシュが京都に流用されないこと。
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
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
        # 既定設定（sentence_weather + sentence_temp のみ有効）では、
        # 1 地点あたり 2 文（天気・気温）× 2 地点 = 4 文になる。
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
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

    def test_describe_joins_all_locations_into_one_string(self):
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        with mock.patch("chime.weather.fetch_json",
                        side_effect=[self.otsu_payload, self.kyoto_payload]):
            text = service.describe(today=TODAY)
        self.assertIn("大津", text)
        self.assertIn("京都", text)


class DescribeSentencesPartialFailureTest(unittest.TestCase):
    def setUp(self):
        self.kyoto_payload = load_fixture("open_meteo_kyoto.json")

    def test_one_location_failing_still_returns_the_other(self):
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        # tests/__init__.py がテスト全体でログを抑制している（logging.disable
        # (logging.CRITICAL)）ため、assertLogs で拾えるよう tests/test_sequence.py
        # と同じ手順でこのテストの間だけ一時的に解除する。
        logging.disable(logging.NOTSET)
        try:
            with mock.patch("chime.weather.fetch_json",
                            side_effect=[WeatherError("圏外"), self.kyoto_payload]):
                with self.assertLogs("chime.weather", level="WARNING") as cm:
                    sentences = service.describe_sentences(today=TODAY)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertTrue(any("大津" in message for message in cm.output))
        self.assertEqual(len(sentences), 2)
        self.assertIn("京都", "".join(sentences))

    def test_one_location_missing_current_still_returns_the_other(self):
        # current が欠けた地点だけ飛ばされ、もう一方の地点の文は返ること。
        otsu_payload_without_current = json.loads(json.dumps(load_fixture("open_meteo.json")))
        del otsu_payload_without_current["current"]
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        logging.disable(logging.NOTSET)
        try:
            with mock.patch("chime.weather.fetch_json",
                            side_effect=[otsu_payload_without_current, self.kyoto_payload]):
                with self.assertLogs("chime.weather", level="WARNING") as cm:
                    sentences = service.describe_sentences(today=TODAY)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertTrue(any("大津" in message for message in cm.output))
        self.assertEqual(len(sentences), 2)
        self.assertIn("京都", "".join(sentences))

    def test_the_other_order_also_returns_the_succeeding_location(self):
        otsu_payload = load_fixture("open_meteo.json")
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        with mock.patch("chime.weather.fetch_json",
                        side_effect=[otsu_payload, WeatherError("圏外")]):
            sentences = service.describe_sentences(today=TODAY)
        self.assertEqual(len(sentences), 2)
        self.assertIn("大津", "".join(sentences))

    def test_all_locations_failing_raises(self):
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        with mock.patch("chime.weather.fetch_json", side_effect=WeatherError("圏外")):
            with self.assertRaises(WeatherError):
                service.describe_sentences(today=TODAY)


if __name__ == "__main__":
    unittest.main()
