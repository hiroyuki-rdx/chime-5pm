"""設定まわりのテスト。"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import unittest

from chime.config import (DEFAULT_CONFIG, EXAMPLE_CONFIG_PATH, REMOVED_KEYS, Config,
                          ConfigError, deep_merge, load_config, redundant_keys,
                          strip_removed_keys)


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
        # 読み上げエンジンが誤読する 4 つの時刻（詳細は chime/timesignal.py）。
        self.assertEqual(
            DEFAULT_CONFIG["time_signal"]["hour_readings"],
            {"0": "れいじ", "4": "よじ", "7": "しちじ", "9": "くじ"})

    def test_announce_template_uses_hour_reading_placeholder(self):
        self.assertIn("{hour_reading}", DEFAULT_CONFIG["time_signal"]["announce_template"])


class DefaultExtraIsWeatherThenQuoteTest(unittest.TestCase):
    """運用方針: 時報のあとは毎回「天気予報 → ひとこと」の両方を流す。

    v6.0.0 で「どちらか一方を選ぶ」方式（mode="choice"）を廃し、この放送に一本化した。
    天気を流すには weather.enabled と extra_segment.enabled が揃っている必要があり、
    片方だけでは意図した放送にならないため、まとめて回帰確認する。
    """

    def test_weather_is_enabled_by_default(self):
        self.assertTrue(DEFAULT_CONFIG["weather"]["enabled"])

    def test_extra_segment_is_enabled_by_default(self):
        self.assertTrue(DEFAULT_CONFIG["extra_segment"]["enabled"])


class PrerecordableWeatherTest(unittest.TestCase):
    """天気の読み上げを作り置きできる状態に保つための回帰確認。

    気象庁（JMA）の予報文は自由文なので語彙が閉じず、事前生成できなかった。
    v5.0.0 で実行時合成を廃したため、事前生成が外れた文は無音になる。
    天気の提供元を Open-Meteo（天気コードで語彙が 28 語に閉じる）だけにすることが、
    放送全体をずんだもんの声で揃える前提になっている（v6.0.0 で JMA を削除）。
    """

    def test_open_meteo_is_the_only_weather_source(self):
        weather = DEFAULT_CONFIG["weather"]
        self.assertIn("open_meteo", weather)
        self.assertNotIn("provider", weather)
        self.assertNotIn("jma", weather)

    def test_location_is_otsu_only(self):
        # v5.1.0 までは大津・京都の 2 地点。京都の読み上げ音声は v5.2.0 で削除した。
        labels = [str(item.get("label"))
                  for item in DEFAULT_CONFIG["weather"]["open_meteo"]["locations"]]
        self.assertEqual(labels, ["大津"])

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

    語尾を変えると作り置き音声の照合（文言の完全一致）が外れる。v5.0.0 で
    実行時合成のフォールバックを廃したため、うっかり戻すと読み上げが
    すべて無音になる。
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
    """天気予報は 12 時の 1 回だけ流す（v5.0.0 までは 10/12/14/16 時の 2 時間おき）。"""

    def test_weather_is_only_at_noon(self):
        self.assertEqual(DEFAULT_CONFIG["extra_segment"]["weather_hours"], [12])

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
    当時あった Open JTalk のフォールバックが合成していた（v5.0.0 でその
    フォールバックは削除したため、いま同じことが起きれば無音になる）。
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


class RemovedKeysTest(unittest.TestCase):
    """v6.0.0 で廃止した設定キーは、設定ファイルに書かれていても警告して無視する。

    Pi の config.json に消し忘れた行が残っているだけで起動を止めない
    （systemd の Restart=always が再起動を繰り返すだけになる）。無視するだけでなく、
    マージ前に取り除くので、実行時の設定や ``--print-config`` にも現れない。
    """

    EXPECTED_KEYS = {
        "extra_segment.mode",
        "extra_segment.weather_probability",
        "extra_segment.always_weather_hours",
        "extra_segment.always_quote_hours",
        "extra_segment.fallback_to_quote",
        "weather.provider",
        "weather.jma",
        "weather.max_weather_chars",
    }

    #: 廃止前（v5.3.0）の config.example.json に入っていた、これらのキーの値。
    OLD_DEFAULTS = {
        "extra_segment": {
            "mode": "both",
            "weather_probability": 0.0,
            "always_weather_hours": [],
            "always_quote_hours": [],
            "fallback_to_quote": True,
        },
        "weather": {
            "provider": "open_meteo",
            "jma": {"area_code": "250000", "area_name": "南部",
                    "temp_area_name": "大津", "label": "滋賀",
                    "drop_after": ["所により"]},
            "max_weather_chars": 40,
        },
    }

    @staticmethod
    def _nested(path, value):
        """``"a.b"`` と値から ``{"a": {"b": 値}}`` を作る。"""
        result = value
        for part in reversed(path.split(".")):
            result = {part: result}
        return result

    @staticmethod
    def _expected_line(path, key):
        return "{0} の {1} は v6.0.0 で廃止しました（{2}）。この行は無視します。消してください。".format(
            path, key, REMOVED_KEYS[key])

    def _load_capturing_warnings(self, local=None, explicit=None):
        """``local``（config.json）・``explicit``（--config）を読み込み、警告ログを返す。

        tests/__init__.py がテスト全体でログを抑制しているため、tests/test_weather.py
        と同じ手順でこの間だけ解除する。``assertLogs`` は 1 件も出ないと失敗するので、
        判定用のダミーを先に出す。戻り値は ``(警告の行, 一時ディレクトリ, 設定)``。
        """
        with tempfile.TemporaryDirectory() as tmp:
            explicit_path = None
            for name, override in (("config.json", local), ("other.json", explicit)):
                if override is None:
                    continue
                path = os.path.join(tmp, name)
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(override, handle)
                if name == "other.json":
                    explicit_path = path
            logging.disable(logging.NOTSET)
            try:
                with self.assertLogs("chime.config", level="WARNING") as captured:
                    logging.getLogger("chime.config").warning("dummy")
                    config = load_config(explicit_path, base_dir=tmp)
            finally:
                logging.disable(logging.CRITICAL)
        lines = [line for line in captured.output if not line.endswith("dummy")]
        return lines, tmp, config

    # -- 一覧と案内 ------------------------------------------------------
    def test_the_removed_keys_are_the_planned_ones(self):
        self.assertEqual(set(REMOVED_KEYS), self.EXPECTED_KEYS)

    def test_defaults_contain_none_of_the_removed_keys(self):
        missing = object()
        config = Config(DEFAULT_CONFIG)
        for key in REMOVED_KEYS:
            self.assertIs(config.get(key, missing), missing, key)

    def test_example_config_contains_none_of_the_removed_keys(self):
        # 雛形を config.json にコピーした利用者に、廃止の警告が出てしまわないこと。
        with open(EXAMPLE_CONFIG_PATH, encoding="utf-8") as handle:
            config = Config(json.load(handle))
        missing = object()
        for key in REMOVED_KEYS:
            self.assertIs(config.get(key, missing), missing, key)

    def test_choice_keys_point_to_weather_hours(self):
        for key, guidance in REMOVED_KEYS.items():
            if key.startswith("extra_segment."):
                self.assertEqual(guidance, "天気を流す時刻は extra_segment.weather_hours で指定", key)

    def test_jma_keys_point_to_open_meteo(self):
        for key, guidance in REMOVED_KEYS.items():
            if key.startswith("weather."):
                self.assertEqual(guidance, "天気は Open-Meteo（weather.open_meteo）に一本化", key)

    # -- 警告と、設定に届かないこと --------------------------------------
    def test_each_removed_key_warns_once_and_never_reaches_the_config(self):
        for key in REMOVED_KEYS:
            with self.subTest(key=key):
                lines, tmp, config = self._load_capturing_warnings(
                    local=self._nested(key, 1))
                self.assertEqual(len(lines), 1, lines)
                self.assertIn(self._expected_line(os.path.join(tmp, "config.json"), key),
                              lines[0])
                missing = object()
                self.assertIs(config.get(key, missing), missing)
                # 廃止したキーだけを書いたなら、結果は既定値そのもの
                self.assertEqual(config.data, DEFAULT_CONFIG)

    def test_the_removed_value_does_not_change_the_behaviour(self):
        # 以前は mode="choice" で抽選方式になっていた。いまは書かれていても
        # 何も変わらない（既定値のまま）。
        lines, _, config = self._load_capturing_warnings(
            local={"extra_segment": {"mode": "choice", "weather_probability": 0.5,
                                     "weather_hours": [10]}})
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(config.get("extra_segment.weather_hours"), [10])
        self.assertEqual(config.section("extra_segment"),
                         {"enabled": True, "weather_hours": [10]})

    def test_a_removed_dict_is_dropped_but_its_siblings_still_take_effect(self):
        lines, _, config = self._load_capturing_warnings(local={"weather": {
            "jma": {"area_code": "260000", "area_name": "南部"},
            "timeout_seconds": 3.0,
        }})
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("weather.jma は", lines[0])
        self.assertEqual(config.get("weather.timeout_seconds"), 3.0)
        self.assertNotIn("jma", config.data["weather"])

    def test_one_warning_per_key_and_nothing_for_the_keys_under_it(self):
        lines, _, _ = self._load_capturing_warnings(
            local={"weather": {"jma": {"area_code": "260000", "area_name": "南部"}}})
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("weather.jma は", lines[0])

    def test_a_key_set_to_null_still_counts_as_written(self):
        lines, _, config = self._load_capturing_warnings(local={"weather": {"provider": None}})
        self.assertEqual(len(lines), 1, lines)
        self.assertNotIn("provider", config.data["weather"])

    def test_the_same_name_under_another_section_is_left_alone(self):
        # 廃止したのは "extra_segment.mode" であって、どこかの "mode" ではない。
        lines, _, config = self._load_capturing_warnings(
            local={"audio": {"mode": "x"}, "quotes": {"provider": "y"}})
        self.assertEqual(lines, [])
        self.assertEqual(config.get("audio.mode"), "x")
        self.assertEqual(config.get("quotes.provider"), "y")

    def test_current_keys_do_not_warn(self):
        lines, _, config = self._load_capturing_warnings(local={
            "extra_segment": {"weather_hours": [10, 12]},
            "weather": {"enabled": False, "open_meteo": {"locations": []}},
            "quotes": {"avoid_recent": 5},
        })
        self.assertEqual(lines, [])
        self.assertEqual(config.get("quotes.avoid_recent"), 5)

    def test_the_defaults_alone_do_not_warn(self):
        lines, _, _ = self._load_capturing_warnings()
        self.assertEqual(lines, [])

    def test_a_non_object_on_the_way_does_not_break_the_check(self):
        lines, _, config = self._load_capturing_warnings(
            local={"weather": "オブジェクトではない", "extra_segment": [1, 2]})
        self.assertEqual(lines, [])
        # 辞書でない値は、そのまま（既定値を置き換えて）届く。ここでは落ちないことだけ確かめる。
        self.assertEqual(config.get("weather"), "オブジェクトではない")

    def test_each_file_is_reported_under_its_own_name(self):
        lines, tmp, config = self._load_capturing_warnings(
            local={"weather": {"provider": "jma"}},
            explicit={"extra_segment": {"mode": "choice"}})
        self.assertEqual(len(lines), 2, lines)
        self.assertIn(self._expected_line(os.path.join(tmp, "config.json"),
                                          "weather.provider"), lines[0])
        self.assertIn(self._expected_line(os.path.join(tmp, "other.json"),
                                          "extra_segment.mode"), lines[1])
        self.assertEqual(config.data, DEFAULT_CONFIG)

    def test_an_old_full_copy_of_the_example_warns_and_still_loads(self):
        # 廃止前の config.example.json を丸ごと config.json にした Pi。既定値の
        # 丸ごとコピーの警告に加え、廃止したキーの警告も出るが、起動は続く。
        old_example = deep_merge(DEFAULT_CONFIG, self.OLD_DEFAULTS)
        # OLD_DEFAULTS が廃止キーを取りこぼしていると、このテストが意味を失う
        self.assertEqual({"{0}.{1}".format(section, key)
                          for section, body in self.OLD_DEFAULTS.items() for key in body},
                         set(REMOVED_KEYS))
        lines, tmp, config = self._load_capturing_warnings(local=old_example)
        removed = [line for line in lines if "v6.0.0 で廃止しました" in line]
        self.assertEqual(len(removed), len(REMOVED_KEYS), lines)
        self.assertTrue(any("既定値の丸ごとコピー" in line for line in lines), lines)
        self.assertEqual(config.data, DEFAULT_CONFIG)

    # -- strip_removed_keys 単体 ------------------------------------------
    def test_strip_does_not_mutate_the_callers_dict(self):
        original = {
            "weather": {"provider": "jma", "timeout_seconds": 3.0,
                        "jma": {"area_code": "260000"}},
            "extra_segment": {"mode": "choice", "weather_hours": [10]},
        }
        snapshot = copy.deepcopy(original)
        stripped = strip_removed_keys("config.json", original)
        self.assertEqual(original, snapshot)
        self.assertEqual(stripped, {"weather": {"timeout_seconds": 3.0},
                                    "extra_segment": {"weather_hours": [10]}})
        # 複製なので、取り除いた結果を書き換えても元には響かない
        stripped["extra_segment"]["weather_hours"].append(99)
        self.assertEqual(original, snapshot)

    def test_strip_keeps_a_parent_that_became_empty(self):
        self.assertEqual(
            strip_removed_keys("config.json", {"extra_segment": {"mode": "both"}}),
            {"extra_segment": {}})

    def test_strip_treats_a_non_mapping_parent_as_absent(self):
        override = {"weather": "x", "extra_segment": None}
        self.assertEqual(strip_removed_keys("config.json", override), override)
        self.assertEqual(strip_removed_keys("config.json", {"weather": {"jma": 1}}),
                         {"weather": {}})

    def test_strip_of_an_empty_override_is_empty(self):
        self.assertEqual(strip_removed_keys("config.json", {}), {})


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

    def test_shift_jis_config_raises_config_error_with_guidance(self):
        # Windows のメモ帳の既定（ANSI＝Shift_JIS）で保存された config.json。
        # UnicodeDecodeError の生のトレースバックではなく、直し方が分かる案内に
        # なること。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "wb") as handle:
                handle.write('{"quotes": {"file": "ひとこと.json"}}'.encode("shift_jis"))
            with self.assertRaises(ConfigError) as caught:
                load_config(path, base_dir=tmp)
        message = str(caught.exception)
        self.assertIn("UTF-8", message)
        self.assertIn("Shift_JIS", message)
        self.assertIn(path, message)

    def test_utf8_bom_config_is_accepted(self):
        # メモ帳の「UTF-8（BOM 付き）」で保存された config.json も読める。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "wb") as handle:
                handle.write(b"\xef\xbb\xbf" + json.dumps(
                    {"quotes": {"avoid_recent": 3}}, ensure_ascii=False).encode("utf-8"))
            config = load_config(path, base_dir=tmp)
        self.assertEqual(config.get("quotes.avoid_recent"), 3)

    def test_syntax_error_reports_line_and_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{\n  "timezone": "UTC"\n  "logging": {}\n}')
            with self.assertRaises(ConfigError) as caught:
                load_config(path, base_dir=tmp)
        message = str(caught.exception)
        self.assertIn(path, message)
        self.assertIn("3 行 3 文字目", message)

    def test_fullwidth_quotes_get_a_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{\n  “timezone”: “UTC”\n}')
            with self.assertRaises(ConfigError) as caught:
                load_config(path, base_dir=tmp)
        self.assertIn("全角の記号が混ざっていませんか", str(caught.exception))

    def test_top_level_must_be_an_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('["配列は想定外"]')
            with self.assertRaises(ConfigError) as caught:
                load_config(path, base_dir=tmp)
        self.assertIn("トップレベルはオブジェクト", str(caught.exception))

    def test_missing_config_message_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nope.json")
            with self.assertRaises(ConfigError) as caught:
                load_config(path, base_dir=tmp)
        self.assertEqual(str(caught.exception), "設定ファイルが見つかりません: " + path)


class NoRuntimeSynthesisFallbackTest(unittest.TestCase):
    """合成フォールバック（Open JTalk）を既定から締め出していることの回帰確認。

    作り置きに無い文言を「別人の男性音声」で鳴らしてしまう事故が繰り返された
    ため、v5.0.0 でエンジンごと削除した。既定に戻すと、壊れているのに音だけは
    鳴る状態が復活する。
    """

    def test_default_engines_are_prerecorded_then_voicevox(self):
        self.assertEqual(DEFAULT_CONFIG["tts"]["engines"],
                         ["prerecorded", "voicevox"])

    def test_defaults_carry_no_open_jtalk_settings(self):
        self.assertNotIn("open_jtalk", DEFAULT_CONFIG["tts"])

    def test_no_default_mentions_open_jtalk_anywhere(self):
        self.assertNotIn("open_jtalk", json.dumps(DEFAULT_CONFIG,
                                                  ensure_ascii=False))


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
