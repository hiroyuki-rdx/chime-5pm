"""設定まわりのテスト。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from chime.config import (DEFAULT_CONFIG, EXAMPLE_CONFIG_PATH, Config, ConfigError,
                          deep_merge, load_config)


class DeepMergeTest(unittest.TestCase):
    def test_nested_override(self):
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        merged = deep_merge(base, {"b": {"c": 20}, "e": 5})
        self.assertEqual(merged, {"a": 1, "b": {"c": 20, "d": 3}, "e": 5})

    def test_does_not_mutate_base(self):
        base = {"b": {"c": 2}}
        deep_merge(base, {"b": {"c": 99}})
        self.assertEqual(base["b"]["c"], 2)

    def test_list_is_replaced_not_merged(self):
        merged = deep_merge({"weekdays": [0, 1, 2]}, {"weekdays": [5]})
        self.assertEqual(merged["weekdays"], [5])

    def test_untouched_branches_are_independent_copies(self):
        # override で触れていない枝を書き換えても base に波及しないこと。
        # DEFAULT_CONFIG のような共有の既定値辞書を base に使う場合に重要。
        base = {"a": 1, "logging": {"level": "INFO"}}
        merged = deep_merge(base, {"a": 2})
        merged["logging"]["level"] = "DEBUG"
        self.assertEqual(base["logging"]["level"], "INFO")


class ConfigAccessTest(unittest.TestCase):
    def setUp(self):
        self.config = Config(DEFAULT_CONFIG, base_dir="/opt/chime")

    def test_dotted_get(self):
        self.assertEqual(self.config.get("schedule.hourly.start_hour"), 10)
        self.assertEqual(self.config.get("schedule.closing.minute"), 57)

    def test_missing_returns_default(self):
        self.assertIsNone(self.config.get("nope.nothing"))
        self.assertEqual(self.config.get("nope.nothing", "fallback"), "fallback")

    def test_section(self):
        section = self.config.section("audio.mixer")
        self.assertEqual(section["buffer"], 4096)
        self.assertEqual(self.config.section("does.not.exist"), {})

    def test_relative_paths_resolve_against_base_dir(self):
        self.assertEqual(self.config.path("closing.music_file"),
                         "/opt/chime/assets/hotaru.mp3")

    def test_absolute_paths_are_kept(self):
        config = Config({"x": "/srv/sound.wav"}, base_dir="/opt/chime")
        self.assertEqual(config.path("x"), "/srv/sound.wav")

    def test_explicit_empty_string_is_not_replaced_by_default(self):
        # 明示的な空文字列（「未設定」の意）は、default 引数があっても尊重される。
        config = Config({"x": ""}, base_dir="/opt/chime")
        self.assertEqual(config.path("x", default="fallback.wav"), "")

    def test_missing_key_uses_default(self):
        config = Config({}, base_dir="/opt/chime")
        self.assertEqual(config.path("missing", default="fallback.wav"),
                         "/opt/chime/fallback.wav")

    def test_tilde_is_expanded_to_home_directory(self):
        config = Config({"x": "~/sounds/hotaru.mp3"}, base_dir="/opt/chime")
        expected = os.path.join(os.path.expanduser("~"), "sounds", "hotaru.mp3")
        self.assertEqual(config.path("x"), expected)


class DefaultConfigMisreadFixesTest(unittest.TestCase):
    """時報・天気予報の誤読対策に関わる既定値の回帰防止。"""

    def test_hour_readings_default_covers_the_four_misread_hours(self):
        # Open JTalk が誤読する 4 つの時刻（詳細は chime/timesignal.py）。
        self.assertEqual(
            DEFAULT_CONFIG["time_signal"]["hour_readings"],
            {"0": "れいじ", "4": "よじ", "7": "しちじ", "9": "くじ"})

    def test_announce_template_uses_hour_reading_placeholder(self):
        self.assertIn("{hour_reading}", DEFAULT_CONFIG["time_signal"]["announce_template"])

    def test_weather_drops_region_specific_caveat_by_default(self):
        self.assertEqual(DEFAULT_CONFIG["weather"]["jma"]["drop_after"], ["所により"])

    def test_weather_has_a_max_character_safety_net(self):
        self.assertEqual(DEFAULT_CONFIG["weather"]["max_weather_chars"], 40)


class LoadConfigTest(unittest.TestCase):
    def test_local_config_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as handle:
                json.dump({"schedule": {"hourly": {"end_hour": 18}}}, handle)
            config = load_config(base_dir=tmp)
            self.assertEqual(config.get("schedule.hourly.end_hour"), 18)
            # 指定していない値は既定のまま
            self.assertEqual(config.get("schedule.hourly.start_hour"), 10)

    def test_explicit_config_wins_over_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as handle:
                json.dump({"timezone": "UTC"}, handle)
            explicit = os.path.join(tmp, "other.json")
            with open(explicit, "w", encoding="utf-8") as handle:
                json.dump({"timezone": "Asia/Osaka"}, handle)
            config = load_config(explicit, base_dir=tmp)
            self.assertEqual(config.get("timezone"), "Asia/Osaka")

    def test_broken_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            with self.assertRaises(ConfigError):
                load_config(path, base_dir=tmp)

    def test_missing_explicit_config_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                load_config(os.path.join(tmp, "nope.json"), base_dir=tmp)


class ExampleConfigTest(unittest.TestCase):
    """``config.example.json`` が既定値と一致していることを保証する。"""

    def test_example_matches_defaults(self):
        with open(EXAMPLE_CONFIG_PATH, "r", encoding="utf-8") as handle:
            example = json.load(handle)
        self.assertEqual(
            example, DEFAULT_CONFIG,
            "config.example.json が古くなっています。"
            "`python3 scripts/dump_example_config.py` で更新してください。")


if __name__ == "__main__":
    unittest.main()
