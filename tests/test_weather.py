"""天気予報の取得・整形のテスト（ネットワークには接続しない）。"""

from __future__ import annotations

import unittest
from datetime import date

from tests.support import load_fixture  # noqa: F401

from chime.config import DEFAULT_CONFIG
from chime.weather import (WeatherError, WeatherService, build_text,
                           normalize_weather_text, parse_jma, parse_open_meteo)

WEATHER = DEFAULT_CONFIG["weather"]
TODAY = date(2026, 8, 26)


class NormalizeTest(unittest.TestCase):
    def test_removes_full_width_spaces(self):
        self.assertEqual(normalize_weather_text("くもり　時々　晴れ"), "くもり時々晴れ")

    def test_removes_ascii_spaces(self):
        self.assertEqual(normalize_weather_text("晴れ のち 雨"), "晴れのち雨")


class ParseJmaTest(unittest.TestCase):
    def setUp(self):
        self.payload = load_fixture("jma_130000.json")

    def test_extracts_todays_forecast(self):
        parts = parse_jma(self.payload, WEATHER["jma"], TODAY)
        self.assertEqual(parts["when"], "今日")
        self.assertEqual(parts["weather"], "くもり時々晴れ")
        self.assertEqual(parts["label"], "東京")

    def test_picks_configured_area(self):
        settings = dict(WEATHER["jma"], area_name="伊豆諸島北部", label="")
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["weather"], "くもり")

    def test_falls_back_to_first_area(self):
        settings = dict(WEATHER["jma"], area_name="存在しない地域", label="")
        parts = parse_jma(self.payload, settings, TODAY)
        self.assertEqual(parts["weather"], "くもり時々晴れ")

    def test_extracts_max_temperature(self):
        parts = parse_jma(self.payload, WEATHER["jma"], TODAY)
        self.assertEqual(parts["temp_max"], 31)

    def test_missing_min_temperature_is_none(self):
        # 昼発表の予報には当日の最低気温が含まれない
        parts = parse_jma(self.payload, WEATHER["jma"], TODAY)
        self.assertIsNone(parts["temp_min"])

    def test_tomorrow_has_both_temperatures(self):
        parts = parse_jma(self.payload, WEATHER["jma"], date(2026, 8, 27))
        self.assertEqual(parts["when"], "今日")
        self.assertEqual((parts["temp_min"], parts["temp_max"]), (25, 33))

    def test_uses_max_precipitation_of_the_day(self):
        parts = parse_jma(self.payload, WEATHER["jma"], TODAY)
        self.assertEqual(parts["pop"], 30)

    def test_relative_label_for_future_date(self):
        parts = parse_jma(self.payload, WEATHER["jma"], date(2026, 8, 25))
        self.assertEqual(parts["when"], "明日")

    def test_rejects_unexpected_payloads(self):
        for payload in ({}, [], [{"timeSeries": []}], "nonsense"):
            with self.assertRaises(WeatherError):
                parse_jma(payload, WEATHER["jma"], TODAY)


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
            "https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json")

    def test_open_meteo_url_contains_coordinates(self):
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        url = service.url()
        self.assertIn("latitude=35.6895", url)
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
        service._cache = (float("inf"), "キャッシュされた予報")
        self.assertEqual(service.describe(), "キャッシュされた予報")

    def test_parse_dispatches_by_provider(self):
        service = WeatherService(dict(WEATHER, provider="open_meteo"))
        parts = service.parse(load_fixture("open_meteo.json"), TODAY)
        self.assertEqual(parts["weather"], "くもり")


if __name__ == "__main__":
    unittest.main()
