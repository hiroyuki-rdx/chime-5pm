"""設定まわりのテスト。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest

from chime.config import (DEFAULT_CONFIG, EXAMPLE_CONFIG_PATH, Config, ConfigError,
                          deep_merge, load_config, redundant_keys)


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


class DefaultExtraIsWeatherThenQuoteTest(unittest.TestCase):
    """運用方針: 時報のあとは毎回「天気予報 → ひとこと」の両方を流す。

    天気を流すには enabled と mode の両方が揃っている必要があり、片方だけでは
    意図した放送にならないため、まとめて回帰確認する。
    """

    def test_weather_is_enabled_by_default(self):
        self.assertTrue(DEFAULT_CONFIG["weather"]["enabled"])

    def test_extra_mode_is_both_by_default(self):
        self.assertEqual(DEFAULT_CONFIG["extra_segment"]["mode"], "both")


class PrerecordableWeatherTest(unittest.TestCase):
    """天気の読み上げを作り置きできる状態に保つための回帰確認。

    気象庁（jma）の予報文は自由文なので語彙が閉じず、事前生成できない。
    事前生成が外れた文は実行時に Open JTalk が合成するため、天気だけ
    別人の男性音声になる。既定を open_meteo（天気コードで語彙が 28 語に
    閉じる）に保つことが、放送全体をずんだもんの声で揃える前提になっている。
    """

    def test_provider_is_open_meteo_by_default(self):
        self.assertEqual(DEFAULT_CONFIG["weather"]["provider"], "open_meteo")

    def test_locations_are_otsu_and_kyoto(self):
        labels = [str(item.get("label"))
                  for item in DEFAULT_CONFIG["weather"]["open_meteo"]["locations"]]
        self.assertEqual(labels, ["大津", "京都"])

    def test_every_location_has_coordinates(self):
        for item in DEFAULT_CONFIG["weather"]["open_meteo"]["locations"]:
            self.assertIsInstance(item.get("latitude"), float, item)
            self.assertIsInstance(item.get("longitude"), float, item)

    def test_prerecord_range_is_defined(self):
        prerecord = DEFAULT_CONFIG["weather"]["prerecord"]
        self.assertLess(prerecord["temp_min"], prerecord["temp_max"])
        self.assertGreater(prerecord["pop_step"], 0)
        self.assertEqual(prerecord["whens"], ["今日"])


class ZundamonToneTest(unittest.TestCase):
    """読み上げの語尾をずんだもんの「のだ」調に揃えている回帰確認。

    語尾を変えると作り置き音声の照合（文言の完全一致）が外れるため、
    うっかり戻すと放送全体が Open JTalk の男性音声になる。
    """

    def _templates(self):
        """読み上げに使うテンプレートのうち、空でない（＝実際に読む）もの。

        空文字列は「その文を読まない」という意味で、語尾を問う対象にならない。
        """
        signal = DEFAULT_CONFIG["time_signal"]
        weather = DEFAULT_CONFIG["weather"]
        candidates = [
            signal["announce_template"],
            signal["noon_template"],
            weather["sentence_weather"],
            weather["sentence_temp"],
            weather["sentence_temp_max"],
            weather["sentence_pop"],
        ]
        return [template for template in candidates if template]

    def test_every_template_ends_with_noda(self):
        for template in self._templates():
            self.assertTrue(template.endswith("のだ。"), template)

    def test_no_template_keeps_the_old_desu_masu_ending(self):
        for template in self._templates():
            self.assertNotIn("しました。", template)
            self.assertNotIn("です。", template)


class WeatherIsCurrentConditionsTest(unittest.TestCase):
    """天気は「今日 1 日の予報」ではなく「現在の天気と気温」を読む。

    予報を読んでいた頃は、14 時に「最高気温は31度なのだ」と、その日の
    予想最高気温を読み上げていた。時報で知りたいのは今どうなのかなので
    現況に切り替えた。降水確率は現況が存在しないため読まない。
    """

    def test_weather_sentence_says_now_not_a_date(self):
        template = DEFAULT_CONFIG["weather"]["sentence_weather"]
        self.assertIn("今の", template)
        self.assertNotIn("{when}", template)

    def test_current_temperature_is_read(self):
        self.assertIn("{temp}", DEFAULT_CONFIG["weather"]["sentence_temp"])

    def test_forecast_sentences_are_disabled_by_default(self):
        # キーごと消さずに空文字列で残してある。あとから読みたくなったら
        # 文言を入れて作り置きを再生成すれば戻せる。
        self.assertEqual(DEFAULT_CONFIG["weather"]["sentence_temp_max"], "")
        self.assertEqual(DEFAULT_CONFIG["weather"]["sentence_pop"], "")


class WeatherHoursTest(unittest.TestCase):
    """天気予報は 2 時間おき（10/12/14/16 時）に流す。"""

    def test_weather_hours_are_every_two_hours(self):
        self.assertEqual(DEFAULT_CONFIG["extra_segment"]["weather_hours"],
                         [10, 12, 14, 16])

    def test_weather_hours_are_within_the_hourly_schedule(self):
        """時報が鳴らない時刻を指定しても天気は流れない。

        weather_hours は時報のあとのおまけを決める設定なので、
        schedule.hourly の範囲外を書いても効かない。設定ミスの検出。
        """
        hourly = DEFAULT_CONFIG["schedule"]["hourly"]
        scheduled = set(range(hourly["start_hour"], hourly["end_hour"] + 1))
        scheduled -= set(hourly["skip_hours"])
        for hour in DEFAULT_CONFIG["extra_segment"]["weather_hours"]:
            self.assertIn(hour, scheduled, hour)


class RedundantKeysTest(unittest.TestCase):
    """既定値を丸ごと写した設定ファイルを検出する。

    実機で「読み上げだけ男性音声になる」不具合が起きた。原因は、旧版の
    scripts/setup.sh が config.example.json（＝既定値の完全なコピー）を
    config.json として複製していたこと。そうして作られた設定はその時点の
    既定値を凍結するため、更新しても新しい既定値が届かない。読み上げ文言が
    古いまま上書きされ、事前生成した音声（文言との完全一致で引く）に当たらず、
    Open JTalk が合成していた。
    """

    def test_an_override_that_differs_is_not_redundant(self):
        override = {"schedule": {"hourly": {"end_hour": 18}}}
        self.assertEqual(redundant_keys(override), [])

    def test_a_value_equal_to_the_default_is_redundant(self):
        default_end = DEFAULT_CONFIG["schedule"]["hourly"]["end_hour"]
        override = {"schedule": {"hourly": {"end_hour": default_end}}}
        self.assertEqual(redundant_keys(override), ["schedule.hourly.end_hour"])

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(redundant_keys({"存在しないキー": 1}), [])

    def test_a_full_copy_of_the_defaults_is_all_redundant(self):
        # config.example.json は DEFAULT_CONFIG の完全なコピー。これをそのまま
        # config.json にすると全項目が冗長になり、以後の既定値の変更が届かない。
        with open(EXAMPLE_CONFIG_PATH, encoding="utf-8") as handle:
            example = json.load(handle)
        redundant = redundant_keys(example)
        self.assertGreater(len(redundant), 50, "完全コピーなら多数が冗長になるはず")

    def test_an_empty_override_is_not_redundant(self):
        # 新しい setup.sh が作る config.json（空の上書き）が警告を出さないこと。
        self.assertEqual(redundant_keys({}), [])
        self.assertEqual(redundant_keys({"_comment": "説明"}), [])

    def _load_capturing_warnings(self, override):
        """``override`` を config.json として読み込み、警告ログを集めて返す。

        tests/__init__.py がテスト全体でログを抑制している
        （``logging.disable(logging.CRITICAL)``）ため、assertLogs で拾えるよう
        tests/test_weather.py と同じ手順でこの間だけ一時的に解除する。
        ``assertLogs`` は 1 件も出ないと失敗するので、判定用のダミーを先に出す。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(override, handle)
            logging.disable(logging.NOTSET)
            try:
                with self.assertLogs("chime.config", level="WARNING") as captured:
                    logging.getLogger("chime.config").warning("dummy")
                    load_config(explicit_path=path, base_dir=tmp)
            finally:
                logging.disable(logging.CRITICAL)
        return [line for line in captured.output if not line.endswith("dummy")]

    def test_a_full_copy_triggers_a_warning(self):
        with open(EXAMPLE_CONFIG_PATH, encoding="utf-8") as handle:
            example = json.load(handle)
        warnings = self._load_capturing_warnings(example)
        self.assertTrue(warnings, "既定値の丸ごとコピーなら警告が出るはず")
        self.assertTrue(any("既定値" in line for line in warnings), warnings)

    def test_a_small_override_does_not_warn(self):
        warnings = self._load_capturing_warnings({"schedule": {"hourly": {"end_hour": 18}}})
        self.assertEqual(warnings, [])

    def test_an_empty_override_does_not_warn(self):
        warnings = self._load_capturing_warnings({"_comment": "変えたい項目だけを書きます。"})
        self.assertEqual(warnings, [])

    def test_the_warning_survives_a_default_change(self):
        """既定値を変えても検出が効き続けること（この仕組みの要）。"""
        override = {"timezone": DEFAULT_CONFIG["timezone"]}
        self.assertEqual(redundant_keys(override), ["timezone"])
        self.assertEqual(redundant_keys({"timezone": "UTC"}), [])


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
