"""天気予報の取得・整形のテスト（ネットワークには接続しない）。"""

from __future__ import annotations

import json
import unittest
from datetime import date
from unittest import mock

from tests.support import load_fixture  # noqa: F401

from chime.config import DEFAULT_CONFIG
from chime.weather import (WeatherError, WeatherService, build_text,
                           normalize_weather_text, parse_jma, parse_open_meteo)

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


class NormalizeTest(unittest.TestCase):
    def test_removes_full_width_spaces(self):
        self.assertEqual(normalize_weather_text("くもり　時々　晴れ"), "くもり時々晴れ")

    def test_removes_ascii_spaces(self):
        self.assertEqual(normalize_weather_text("晴れ のち 雨"), "晴れのち雨")


class ParseJmaTest(unittest.TestCase):
    def setUp(self):
        self.payload = load_fixture("jma_130000.json")

    def test_extracts_todays_forecast(self):
        parts = parse_jma(self.payload, TOKYO_JMA, TODAY)
        self.assertEqual(parts["when"], "今日")
        self.assertEqual(parts["weather"], "くもり時々晴れ")
        self.assertEqual(parts["label"], "東京")

    def test_picks_configured_area(self):
        settings = dict(TOKYO_JMA, area_name="伊豆諸島北部", label="")
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["weather"], "くもり")

    def test_falls_back_to_first_area(self):
        settings = dict(TOKYO_JMA, area_name="存在しない地域", label="")
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["weather"], "くもり時々晴れ")

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
        # 既定設定（南部／大津）で、読み上げ文が意図どおり組み立つこと。
        parts = parse_jma(self.payload, WEATHER["jma"], TODAY)
        text = build_text(parts, WEATHER)
        self.assertEqual(
            text,
            "今日の滋賀の天気は、晴れ時々くもり。最高気温は27度、降水確率は20パーセントです。")


class ParseOpenMeteoTest(unittest.TestCase):
    def setUp(self):
        self.payload = load_fixture("open_meteo.json")

    def test_extracts_all_fields(self):
        parts = parse_open_meteo(self.payload, WEATHER["open_meteo"], TODAY)
        self.assertEqual(parts["when"], "今日")
        self.assertEqual(parts["weather"], "くもり")
        self.assertEqual(parts["temp_max"], 31)
        self.assertEqual(parts["temp_min"], 25)
        self.assertEqual(parts["pop"], 30)

    def test_unknown_weather_code_raises(self):
        payload = {"daily": dict(self.payload["daily"], weather_code=[999])}
        with self.assertRaises(WeatherError):
            parse_open_meteo(payload, WEATHER["open_meteo"], TODAY)

    def test_missing_daily_raises(self):
        with self.assertRaises(WeatherError):
            parse_open_meteo({"hourly": {}}, WEATHER["open_meteo"], TODAY)


class BuildTextTest(unittest.TestCase):
    def test_full_sentence(self):
        parts = {"when": "今日", "label": "東京", "weather": "晴れ",
                 "temp_max": 30, "temp_min": 22, "pop": 10}
        self.assertEqual(
            build_text(parts, WEATHER),
            "今日の東京の天気は、晴れ。最高気温は30度、最低気温は22度、降水確率は10パーセントです。")

    def test_omits_missing_details(self):
        parts = {"when": "今日", "label": "東京", "weather": "晴れ",
                 "temp_max": None, "temp_min": None, "pop": None}
        self.assertEqual(build_text(parts, WEATHER), "今日の東京の天気は、晴れ。")

    def test_custom_template(self):
        settings = dict(WEATHER, template="{label}は{weather}。{details}", suffix="。")
        parts = {"when": "今日", "label": "大阪", "weather": "雨", "pop": 80}
        self.assertEqual(build_text(parts, settings), "大阪は雨。降水確率は80パーセント。")


class ServiceTest(unittest.TestCase):
    def test_jma_url(self):
        service = WeatherService(dict(WEATHER, provider="jma"))
        self.assertEqual(
            service.url(),
            "https://www.jma.go.jp/bosai/forecast/data/forecast/250000.json")

    def test_open_meteo_url_contains_coordinates(self):
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        url = service.url()
        self.assertIn("latitude=35.0045", url)
        self.assertIn("longitude=135.8686", url)
        self.assertIn("timezone=Asia%2FTokyo", url)

    def test_unknown_provider_raises(self):
        service = WeatherService(dict(WEATHER, provider="magic-8-ball"))
        with self.assertRaises(WeatherError):
            service.url()

    def test_disabled_service_raises(self):
        service = WeatherService(dict(WEATHER, enabled=False))
        with self.assertRaises(WeatherError):
            service.describe()

    def test_cached_result_is_reused(self):
        service = WeatherService(WEATHER)
        service._cache = (float("inf"), date.today(), "キャッシュされた予報")
        self.assertEqual(service.describe(), "キャッシュされた予報")

    def test_cache_is_not_reused_across_a_date_change(self):
        # キャッシュ期限内でも、日付が変わっていれば前日分の文言を使い回さない。
        service = WeatherService(dict(WEATHER, provider="jma"))
        service._cache = (float("inf"), date(2026, 8, 25), "昨日の天気")
        payload = load_fixture("jma_130000.json")
        with mock.patch("chime.weather.fetch_json", return_value=payload) as mocked:
            text = service.describe(today=TODAY)
        mocked.assert_called_once()
        self.assertNotEqual(text, "昨日の天気")

    def test_cache_within_the_same_day_avoids_refetch(self):
        service = WeatherService(dict(WEATHER, provider="jma"))
        service._cache = (float("inf"), TODAY, "本日分のキャッシュ")
        with mock.patch("chime.weather.fetch_json") as mocked:
            text = service.describe(today=TODAY)
        mocked.assert_not_called()
        self.assertEqual(text, "本日分のキャッシュ")

    def test_parse_dispatches_by_provider(self):
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        parts = service.parse(load_fixture("open_meteo.json"), TODAY)
        self.assertEqual(parts["weather"], "くもり")


if __name__ == "__main__":
    unittest.main()
