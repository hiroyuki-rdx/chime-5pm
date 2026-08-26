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
