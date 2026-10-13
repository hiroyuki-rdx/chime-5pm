"""CLI と環境判定のテスト。"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from unittest import mock
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from tests.support import (ANNOUNCE_PATH, block_network, fixture_path, load_fixture,
                           load_manifest, logs_enabled)

from chime import buildinfo, cli, configcheck, env
from chime.app import ChimeApp
from chime.audio import Segment
from chime.cli import CURRENT_HOUR, EPILOG, build_parser, run
from chime.config import DEFAULT_CONFIG, load_config
from chime.scheduler import Scheduler
from chime.pause import MAX_PAUSE_DAYS
from chime.sequence import PlaybackPlan
from chime.state import State
from chime.tts import TTSError
from chime.weather import WeatherError

TOKYO = ZoneInfo("Asia/Tokyo")


def call(argv):
    """CLI を実行し、(終了コード, 出力) を返す。

    ``print`` 出力とログ出力の両方を 1 つのバッファへ集める
    （``tests/__init__.py`` がログを抑制しているため、ここだけ一時的に戻す）。
    """
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        with logs_enabled(), redirect_stdout(buffer), redirect_stderr(buffer):
            code = run(argv)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
    return code, buffer.getvalue()


def call_split(argv):
    """CLI を実行し、(終了コード, 標準出力, 標準エラー出力) を返す。

    ``call`` と違い、ログのハンドラを足さない。``run`` が自分でログを設定する
    ところ（出力先は、そのときの標準出力）まで含めて確かめたいときに使う。
    ルートロガーの状態は実行のあとで元に戻す。
    """
    out, err = io.StringIO(), io.StringIO()
    root = logging.getLogger()
    handlers, previous_level = root.handlers[:], root.level
    root.handlers[:] = []
    try:
        with logs_enabled(), redirect_stdout(out), redirect_stderr(err):
            code = run(argv)
    finally:
        root.handlers[:] = handlers
        root.setLevel(previous_level)
    return code, out.getvalue(), err.getvalue()


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_defaults_to_daemon_mode(self):
        args = self.parser.parse_args([])
        self.assertFalse(args.test)
        self.assertIsNone(args.test_hourly)
        self.assertIsNone(args.schedule)

    def test_test_hourly_without_value(self):
        self.assertEqual(self.parser.parse_args(["--test-hourly"]).test_hourly, -1)

    def test_test_hourly_without_value_is_the_current_hour_sentinel(self):
        self.assertEqual(CURRENT_HOUR, -1)
        self.assertEqual(self.parser.parse_args(["--test-hourly"]).test_hourly, CURRENT_HOUR)

    def test_test_hourly_with_value(self):
        self.assertEqual(self.parser.parse_args(["--test-hourly", "14"]).test_hourly, 14)

    def test_schedule_default_count(self):
        self.assertEqual(self.parser.parse_args(["--schedule"]).schedule, 10)

    def test_backend_choices_are_enforced(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(["--backend", "gramophone"])


class RunTest(unittest.TestCase):
    def setUp(self):
        block_network(self)

    def test_print_config_outputs_valid_json(self):
        code, output = call(["--print-config"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["timezone"], "Asia/Tokyo")

    def test_schedule_lists_events(self):
        code, output = call(["--schedule", "3"])
        self.assertEqual(code, 0)
        self.assertIn("次回以降の予定", output)
        self.assertEqual(output.count("  - "), 3)

    def test_missing_config_returns_error_code(self):
        code, output = call(["--config", "/nonexistent/config.json"])
        self.assertEqual(code, 2)
        self.assertIn("設定エラー", output)

    def test_dry_run_hourly_does_not_play(self):
        """12 時の時報では「正午をお知らせしたのだ。」という定型文が使われること。

        この検証は音声合成（TTS）の成否とは無関係にしたい。しかし
        時刻アナウンスの文言は再生セグメントのラベルとしてしか出力されず、
        そのラベルは合成が成功した場合にのみ付く。この文言はたまたま
        作り置き（``assets/voice/``）にあるため実際には合成が成功するが、
        それは実行環境の事情（作り置きの内容や VOICEVOX ENGINE の有無）に
        依存しており、この検証の意図ではない。``TTSService.synthesize`` を
        スタブ化して常に合成成功したことにする（``--dry-run`` のため
        実際のファイル内容や存在は問われない）。
        """
        wav = ANNOUNCE_PATH
        # 12 時は既定で天気予報を流す時刻なので、天気の取得も失敗に確定させて
        # Open-Meteo へ実際に通信しないようにする（検証の対象は定型文の方）。
        with mock.patch("chime.tts.TTSService.synthesize", return_value=wav), \
                mock.patch("chime.weather.fetch_json", side_effect=WeatherError("圏外")):
            code, output = call(["--test-hourly", "12", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("正午をお知らせしたのだ。", output)
        self.assertIn("dry-run", output)

    def test_dry_run_closing(self):
        code, output = call(["--test", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("蛍の光", output)

    def test_invalid_hour_is_rejected(self):
        code, output = call(["--test-hourly", "42", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("0〜23", output)

    def test_weather_failure_returns_error_code(self):
        # 天気予報は既定で有効だが、fetch_json の失敗を検証するというこのテストの
        # 前提を明示するために、あえて有効化しておく。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"weather": {"enabled": True}}, handle)
            with mock.patch("chime.weather.fetch_json",
                            side_effect=__import__("chime.weather", fromlist=["x"]).WeatherError("圏外")):
                code, output = call(["--config", path, "--weather", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("天気予報を取得できませんでした", output)

    def test_weather_success_prints_text(self):
        # 既定 provider は open_meteo（天気コードで語彙が閉じ、作り置きできる）。
        payload = load_fixture("open_meteo.json")
        with mock.patch("chime.weather.fetch_json", return_value=payload):
            code, output = call(["--weather", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("の天気は", output)
        self.assertIn("なのだ。", output)

    def test_weather_prints_one_line_per_sentence(self):
        """読み上げは 1 文ずつ別のセグメントとして鳴らすため、確認用の出力も
        同じ単位にする。連結した文字列では作り置き音声との照合が外れる。"""
        payload = load_fixture("open_meteo.json")
        with mock.patch("chime.weather.fetch_json", return_value=payload):
            code, output = call(["--weather", "--dry-run"])
        self.assertEqual(code, 0)
        # 既定は大津の 1 地点 × 2 文。現在の天気・気温が出る。
        lines = [line for line in output.splitlines() if line.startswith("読み上げ文 ")]
        self.assertEqual(len(lines), 2, output)
        self.assertTrue(all(line.endswith("なのだ。") for line in lines), lines)

    def test_config_file_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schedule": {"hourly": {"start_hour": 8, "end_hour": 8},
                                        "closing": {"enabled": False}}}, handle)
            code, output = call(["--config", path, "--schedule", "1"])
        self.assertEqual(code, 0)
        self.assertIn("08:00:00", output)

    # -- 不具合1: --weather が OS のローカル日付を使っていた --------------
    def test_weather_command_uses_configured_timezone_date(self):
        """``--weather`` は OS のローカル日付ではなく ``app.now().date()`` を渡すこと。

        修正前は ``app.weather.describe()`` を引数なしで呼んでいたため、
        ``describe()`` 側で ``today=None`` を受け取り OS のローカル日付
        （``date.today()``）にフォールバックしていた。天気が複数文になり
        ``describe_sentences()`` を呼ぶようになった今も、同じ不具合を
        踏まないことを確認する。
        """
        fake_today = date.today() + timedelta(days=1)
        captured = {}

        def fake_describe_sentences(self, today=None, use_cache=True):
            captured["today"] = today
            return ["テスト日和なのだ。"]

        with mock.patch("chime.app.ChimeApp.now",
                        return_value=datetime.combine(fake_today, datetime.min.time())), \
                mock.patch("chime.weather.WeatherService.describe_sentences",
                           fake_describe_sentences):
            code, output = call(["--weather", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertEqual(captured["today"], fake_today)

    # -- 不具合2: --say "" が常駐ループに落ちていた ------------------------
    def test_say_empty_string_is_argument_error(self):
        """空文字列は「指定なし」ではなく引数エラーとして扱い、常駐に落ちないこと。"""
        code, output = call(["--say", "", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 2)
        self.assertIn("読み上げる文言が空です", output)
        self.assertNotIn("次回以降の予定", output)

    def test_say_whitespace_only_is_argument_error(self):
        code, output = call(["--say", "   ", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 2)
        self.assertIn("読み上げる文言が空です", output)

    # -- 不具合3: 音声合成が全滅しても --say / --test-hourly が 0 を返していた --
    def test_say_returns_1_when_tts_completely_fails(self):
        """``--dry-run`` は再生をスキップするだけで常に成功扱いのため、
        実際に再生を試みる（＝``--dry-run`` を付けない）場合で確認する。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"tts": {"engines": []}}, handle)
            code, output = call(["--config", path, "--say", "テスト", "--backend", "mock"])
        self.assertEqual(code, 1)
        self.assertIn("合成できませんでした", output)

    def test_test_hourly_still_returns_0_when_only_speech_fails(self):
        """時報音は合成不要で必ず鳴るため、読み上げだけの失敗は 0 のままでよい。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"tts": {"engines": []},
                          "extra_segment": {"enabled": False}}, handle)
            code, output = call(["--config", path, "--test-hourly", "10",
                                 "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("合成できませんでした", output)

    def test_test_hourly_returns_1_when_no_segments_at_all(self):
        """時報音を含め、再生できるセグメントが 1 つも無ければ 1 を返す。

        ``--dry-run`` は再生自体を行わないため常に成功扱いになるので、
        ここでは付けずに確認する。
        """
        def fake_build_hourly(self, hour, event=None):
            return PlaybackPlan(event=event, segments=[],
                                warnings=["時報音すら用意できませんでした"])

        with mock.patch("chime.sequence.SequenceBuilder.build_hourly", fake_build_hourly):
            code, output = call(["--test-hourly", "10", "--backend", "mock"])
        self.assertEqual(code, 1)

    def test_test_hourly_weather_failure_is_not_an_error(self):
        """天気取得に失敗しても、天気だけ飛ばして他のセグメントは鳴るのでエラーではない。

        既定では 12 時に天気予報が流れる。その取得に失敗しても
        時報音・時刻アナウンス・ひとことは鳴るため、終了コードは 0 のまま。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"weather": {"enabled": True}}, handle)
            with mock.patch("chime.weather.fetch_json",
                            side_effect=__import__("chime.weather", fromlist=["x"]).WeatherError("圏外")):
                code, output = call(["--config", path, "--test-hourly", "12",
                                     "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("天気予報を取得できませんでした", output)

    def test_test_hourly_default_config_uses_weather_and_quote(self):
        """既定設定では、12 時の時報のあとに天気予報と「ひとこと」の両方が流れること。

        単体テストをネットワークに依存させないため、天気の取得は
        ``fetch_json`` のモックで失敗に確定させている。そのうえで、
        取得できなくても「ひとこと」は必ず流れる、という設計を確かめる。
        """
        wav = ANNOUNCE_PATH
        with mock.patch("chime.tts.TTSService.synthesize", return_value=wav), \
                mock.patch("chime.weather.fetch_json",
                           side_effect=WeatherError("圏外")):
            code, output = call(["--test-hourly", "12", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("ひとこと", output)
        self.assertIn("天気予報を取得できませんでした", output)

    # -- 不具合4: 音源ファイルが全て欠落していても --test 系が 0 を返していた --
    # （仕様書 4.11 章「再生可能なセグメントを 1 つも用意できなかった場合は 1」に
    #  実装が追いついていなかった不具合。len(plan.segments) を数えるだけで、
    #  実際にファイルが再生できたかを見ていなかった）
    def test_test_returns_1_when_closing_audio_files_are_both_missing(self):
        """再現手順そのもの: 閉館放送の音源が両方とも存在しないパスなら、
        一音も鳴らないので終了コードは 1 になること。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"closing": {"announce_file": "assets/does_not_exist.wav",
                                       "music_file": "assets/also_missing.mp3"}}, handle)
            code, output = call(["--config", path, "--test", "--backend", "mock"])
        self.assertEqual(code, 1)
        self.assertIn("音源ファイルが見つかりません", output)

    def test_test_all_returns_0_when_only_closing_audio_is_missing(self):
        """時報は鳴るが閉館放送の音源だけ欠けている場合、一部は鳴っているので 0 のまま。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"tts": {"engines": []},
                          "extra_segment": {"enabled": False},
                          "audio": {"mock_max_seconds": 0.05},
                          "closing": {"announce_file": "assets/does_not_exist.wav",
                                     "music_file": "assets/also_missing.mp3"}}, handle)
            code, output = call(["--config", path, "--test-all", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("音源ファイルが見つかりません", output)

    def test_say_returns_0_when_playback_succeeds(self):
        """再生に成功すれば 0 を返す（``build_text`` が返すプランを差し替えて確認）。"""
        wav = ANNOUNCE_PATH

        def fake_build_text(self, text):
            return PlaybackPlan(event=None, segments=[Segment(wav, label="読み上げ")])

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"audio": {"mock_max_seconds": 0.05}}, handle)
            with mock.patch("chime.sequence.SequenceBuilder.build_text", fake_build_text):
                code, output = call(["--config", path, "--say", "テストです", "--backend", "mock"])
        self.assertEqual(code, 0)

    def test_say_dry_run_returns_0_regardless_of_segments(self):
        """``--dry-run`` は再生をスキップするだけで失敗ではないため、常に 0 を返す。"""
        code, output = call(["--say", "テストです", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)

    def test_test_hourly_returns_0_when_tts_disabled_and_audio_files_present(self):
        """TTS が全滅していても時報音（同梱アセット）は鳴るので 0 のまま
        （``--dry-run`` を付けずに実際の再生成否で確認する）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"tts": {"engines": []},
                          "extra_segment": {"enabled": False},
                          "audio": {"mock_max_seconds": 0.05}}, handle)
            code, output = call(["--config", path, "--test-hourly", "10", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("合成できませんでした", output)


    # -- 設定エラー ------------------------------------------------------
    def test_shift_jis_config_returns_2_with_utf8_guidance(self):
        """メモ帳で Shift_JIS のまま保存した config.json は、生の例外ではなく
        「UTF-8 で保存し直す」案内を標準エラー出力へ出して 2 を返すこと。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "wb") as handle:
                handle.write('{"_comment": "設定"}'.encode("shift_jis"))
            code, stdout, stderr = call_split(["--config", path, "--schedule"])
        self.assertEqual(code, 2)
        self.assertIn("設定エラー", stderr)
        self.assertIn("UTF-8", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(stdout, "")

    def test_config_warnings_have_timestamp_and_level(self):
        """設定を読み込むときの警告（廃止したキーなど）にも、時刻とレベルが付くこと。

        ログ設定は設定を読んだあとに確定するため、読み込み中の警告は
        仮の設定（既定値）で出す。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"extra_segment": {"mode": "both"}}, handle)
            code, stdout, stderr = call_split(["--config", path, "--schedule", "1"])
        self.assertEqual(code, 0)
        lines = [line for line in stdout.splitlines() if "v6.0.0 で廃止しました" in line]
        self.assertEqual(len(lines), 1, stdout)
        self.assertRegex(lines[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.* - WARNING - ")

    def test_print_config_keeps_stdout_pure_json_even_with_warnings(self):
        """``--print-config`` の出力をファイルへ保存しても JSON のままであること
        （設定を読むときの警告は標準エラー出力へ出す）。

        警告を出させるため、有効なキー（上書きが出力に反映される）に、廃止した
        キーを 1 つ添えている。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"quotes": {"avoid_recent": 5},
                           "extra_segment": {"mode": "both"}}, handle)
            code, stdout, stderr = call_split(["--config", path, "--print-config"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["quotes"]["avoid_recent"], 5)
        self.assertIn("v6.0.0 で廃止しました", stderr)
        self.assertNotIn("v6.0.0 で廃止しました", stdout)

    def test_print_config_ignores_removed_keys_with_a_warning(self):
        """v6.0.0 で廃止したキーが config.json に残っていても、警告するだけで
        終了コード 0 のまま動き、出力の設定にも現れないこと。

        Pi の config.json に消し忘れが残っているだけで放送を止めない
        （systemd の Restart=always が再起動を繰り返すだけになる）ための保証。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"weather": {"provider": "jma"},
                           "extra_segment": {"mode": "choice"}}, handle)
            code, stdout, stderr = call_split(["--config", path, "--print-config"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr.count("v6.0.0 で廃止しました"), 2, stderr)
        self.assertIn("weather.provider", stderr)
        self.assertIn("extra_segment.mode", stderr)
        printed = json.loads(stdout)
        self.assertNotIn("provider", printed["weather"])
        self.assertNotIn("mode", printed["extra_segment"])

    def test_log_level_option_applies_to_config_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"extra_segment": {"mode": "both"}}, handle)
            code, stdout, stderr = call_split(["--config", path, "--schedule", "1",
                                               "--log-level", "ERROR"])
        self.assertEqual(code, 0)
        self.assertNotIn("廃止しました", stdout + stderr)

    # -- --say の案内 ----------------------------------------------------
    def test_epilog_say_example_is_prerecorded(self):
        """ヘルプの ``--say`` の例は、そのまま試して鳴らせる文言（作り置きにある文）であること。"""
        example = re.search(r"--say (\S+)", EPILOG).group(1)
        phrases = load_manifest()
        self.assertEqual(example, "正午をお知らせしたのだ。")
        self.assertIn(example, phrases)

    def test_say_prerecorded_phrase_has_no_warning(self):
        code, output = call(["--say", "正午をお知らせしたのだ。", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertNotIn("作り置き（assets/voice/）にこの文言がありません", output)

    def test_say_unknown_phrase_shows_candidates_and_guidance(self):
        """作り置きに無い文言は、Pi では無音になることと、近い文言・作り直しの案内を出す。
        そのうえで従来どおり再生を試みる（PC で VOICEVOX が動いていれば鳴る）。"""
        wav = ANNOUNCE_PATH
        with mock.patch("chime.tts.TTSService.synthesize", return_value=wav):
            code, stdout, stderr = call_split(
                ["--say", "正午をお知らせしますのだ。", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("作り置き（assets/voice/）にこの文言がありません。Pi では無音になります。", stderr)
        candidates = [line for line in stderr.splitlines() if "近い文言" in line]
        self.assertEqual(len(candidates), 1, stderr)
        self.assertIn("正午をお知らせしたのだ。", candidates[0])
        self.assertIn("docs/SETUP.md 8 章", stderr)
        # 従来どおり再生（この場合は dry-run の表示）まで進む
        self.assertIn("dry-run", stdout)

    def test_say_unknown_phrase_without_close_match_omits_candidates(self):
        wav = ANNOUNCE_PATH
        with mock.patch("chime.tts.TTSService.synthesize", return_value=wav):
            code, stdout, stderr = call_split(["--say", "Qwerty", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("作り置き（assets/voice/）にこの文言がありません", stderr)
        self.assertNotIn("近い文言", stderr)
        self.assertIn("docs/SETUP.md 8 章", stderr)

    def test_say_reports_missing_voice_folder_instead_of_missing_phrase(self):
        # assets/voice/ が丸ごと無いときは「この文言がありません」ではなく、
        # 作り置きそのものが見つからないことを案内する。
        wav = ANNOUNCE_PATH
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"tts": {"prerecorded_dir": os.path.join(tmp, "no-such-dir")}}, handle)
            with mock.patch("chime.tts.TTSService.synthesize", return_value=wav):
                code, stdout, stderr = call_split(
                    ["--config", path, "--say", "正午をお知らせしたのだ。",
                     "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("作り置き（assets/voice/）が見つかりません", stderr)
        self.assertNotIn("この文言がありません", stderr)

    def test_say_unknown_phrase_keeps_exit_code_when_synthesis_fails(self):
        """案内を出しても終了コードの決まり方は変わらない（合成も再生もできなければ 1）。"""
        with mock.patch("chime.tts.TTSService.synthesize", side_effect=TTSError("失敗")):
            code, stdout, stderr = call_split(
                ["--say", "正午をお知らせしますのだ。", "--backend", "mock"])
        self.assertEqual(code, 1)
        self.assertIn("この文言がありません", stderr)
        self.assertIn("合成できませんでした", stdout)

    # -- CI と同じ引数 ---------------------------------------------------
    def test_ci_hourly_command_does_not_call_the_weather_api(self):
        """CI の「CLI が起動すること」と同じ引数（天気を無効にした設定を渡す）では、
        12 時の時報でも天気 API へ通信しないこと。天気を無効にし忘れると、CI が
        Open-Meteo の障害や遅延の影響を受けてしまう。

        ``--test-hourly`` は起動時に VOICEVOX ENGINE の有無を同じ PC 内
        （127.0.0.1）へ問い合わせる。それは想定内なので、動いていないものとして
        失敗を返し、それ以外の通信が一切ないことを確かめる。
        """
        offline = fixture_path("offline_config.json")
        urls = []

        def fake_urlopen(request, *args, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            urls.append(url)
            if urlparse(url).hostname in ("127.0.0.1", "localhost"):
                raise urllib.error.URLError("VOICEVOX ENGINE は動いていません")
            raise AssertionError("外部へ通信しようとした: {0}".format(url))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            code, output = call(["--test-hourly", "12", "--dry-run", "--backend", "mock",
                                 "--config", offline])
        self.assertEqual(code, 0, output)
        external = [url for url in urls if urlparse(url).hostname not in ("127.0.0.1", "localhost")]
        self.assertEqual(external, [])


class LoggingSettingsTest(unittest.TestCase):
    """ログの水準と書式が設定に無い（または null の）ときは、既定設定の値に戻ること。

    戻り先を別の文字列で持っていると、既定設定を変えたときに食い違う（実際に、
    ``%(name)s`` の無い書式が残っていた）。``setup_logging`` へ渡る値を直接見る。
    """

    def setUp(self):
        block_network(self)

    def second_logging_call(self, data, *flags):
        """設定を読んだあとの ``setup_logging`` の呼び出し（位置引数）を返す。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            with mock.patch("chime.cli.setup_logging") as setup:
                code, _, stderr = call_split(["--config", path] + list(flags))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(setup.call_count, 2)
        return setup.call_args_list[1][0]

    def test_a_null_logging_section_falls_back_to_the_default_settings(self):
        level, log_format = self.second_logging_call({"logging": None}, "--print-config")[:2]
        self.assertEqual(level, DEFAULT_CONFIG["logging"]["level"])
        self.assertEqual(log_format, DEFAULT_CONFIG["logging"]["format"])

    def test_null_level_and_format_fall_back_to_the_default_settings(self):
        data = {"logging": {"level": None, "format": None}}
        level, log_format = self.second_logging_call(data, "--schedule", "1")[:2]
        self.assertEqual(level, DEFAULT_CONFIG["logging"]["level"])
        self.assertEqual(log_format, DEFAULT_CONFIG["logging"]["format"])

    def test_configured_values_are_kept(self):
        data = {"logging": {"level": "DEBUG", "format": "%(message)s"}}
        level, log_format = self.second_logging_call(data, "--schedule", "1")[:2]
        self.assertEqual((level, log_format), ("DEBUG", "%(message)s"))


class VersionTest(unittest.TestCase):
    def test_version_prints_the_version_string_with_the_commit(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(out.getvalue(), buildinfo.version_string() + "\n")
        self.assertRegex(out.getvalue(), r"^campus-chime \d+\.\d+\.\d+ \(.+\)\n$")

    def test_version_uses_build_info(self):
        out = io.StringIO()
        with mock.patch("chime.cli.buildinfo.version_string", return_value="campus-chime 9.9.9 (abc1234)"), \
                redirect_stdout(out), self.assertRaises(SystemExit):
            build_parser().parse_args(["--version"])
        self.assertEqual(out.getvalue(), "campus-chime 9.9.9 (abc1234)\n")


class ConfigSanitizingTest(unittest.TestCase):
    """危ない設定値は、読み込みのあとで既定値に戻してから動く（--print-config を除く）。"""

    def setUp(self):
        block_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def config_file(self, data):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def test_the_app_runs_with_the_default_value_in_place_of_a_dangerous_one(self):
        """タイムゾーンの綴りの誤りで OS のローカル時刻（Pi では UTC）にならない。"""
        path = self.config_file({"timezone": "Asia/Tokio"})
        code, output = call(["--config", path, "--schedule", "1"])
        self.assertEqual(code, 0, output)
        self.assertRegex(output, r"現在時刻: .* JST")

    def test_the_app_is_built_from_the_sanitized_config(self):
        path = self.config_file({"timezone": "Asia/Tokio", "quotes": {"avoid_recent": 5}})
        with mock.patch("chime.cli.ChimeApp", wraps=ChimeApp) as built:
            call(["--config", path, "--schedule", "1"])
        config = built.call_args[0][0]
        self.assertEqual(config.get("timezone"), "Asia/Tokyo")
        self.assertEqual(config.get("quotes.avoid_recent"), 5)

    def test_each_error_is_logged_at_error_with_key_message_hint_and_source(self):
        path = self.config_file({"timezone": "Asia/Tokio"})
        code, output = call(["--config", path, "--schedule", "1"])
        lines = [line for line in output.splitlines() if line.startswith("ERROR - 設定の誤り: ")]
        self.assertEqual(len(lines), 1, output)
        self.assertIn("timezone: ", lines[0])
        self.assertIn("Asia/Tokio", lines[0])
        self.assertIn("Asia/Tokyo", lines[0])  # 直し方・既定値
        self.assertIn("[{0}]".format(path), lines[0])  # 値の出どころ

    def test_one_line_per_error(self):
        path = self.config_file({"timezone": "Asia/Tokio", "schedule": {"max_sleep_seconds": 0}})
        _, output = call(["--config", path, "--schedule", "1"])
        lines = [line for line in output.splitlines() if "設定の誤り" in line]
        self.assertEqual(len(lines), 2, output)

    def test_a_config_without_errors_logs_no_error(self):
        path = self.config_file({"quotes": {"avoid_recent": 5}})
        _, output = call(["--config", path, "--schedule", "1"])
        self.assertNotIn("設定の誤り", output)

    def test_a_broken_log_format_no_longer_crashes_startup(self):
        """``logging.format`` の誤りは、以前は ``setup_logging`` が例外で落ちた。"""
        path = self.config_file({"logging": {"format": "%(asctime"}})
        code, output = call_split(["--config", path, "--schedule", "1"])[:2]
        self.assertEqual(code, 0)
        self.assertIn("次回以降の予定", output)

    def test_every_mode_except_print_config_is_sanitized(self):
        path = self.config_file({"timezone": "Asia/Tokio"})
        for flags in (["--schedule", "1"], ["--say", "正午をお知らせしたのだ。", "--dry-run"],
                      ["--test", "--dry-run", "--backend", "mock"], ["--status"], ["--wait-idle", "0"]):
            with mock.patch("chime.cli.ChimeApp", wraps=ChimeApp) as built, \
                    mock.patch("chime.cli.status.run_status", return_value=0), \
                    mock.patch("chime.cli.status.wait_idle", return_value=0):
                call(["--config", path] + flags)
            self.assertEqual(built.call_args[0][0].get("timezone"), "Asia/Tokyo", flags)

    def test_print_config_shows_the_config_as_written_and_logs_no_error(self):
        path = self.config_file({"timezone": "Asia/Tokio"})
        code, stdout, stderr = call_split(["--config", path, "--print-config"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["timezone"], "Asia/Tokio")
        self.assertNotIn("設定の誤り", stdout + stderr)

    def test_print_config_does_not_sanitize(self):
        path = self.config_file({"timezone": "Asia/Tokio"})
        with mock.patch("chime.cli.configcheck.sanitized") as sanitized:
            call_split(["--config", path, "--print-config"])
        sanitized.assert_not_called()


class LogConfigErrorsTest(unittest.TestCase):
    """``log_config_errors``：設定の誤りだけを ERROR で残す（警告・情報は残さない）。

    ``--check`` 以外のモードは、誤りのあるキーを既定値に戻して動く。その誤りを 1 件ずつログに
    残す関数で、誤りが無ければ何も読み直さない（起動のたびに設定を二度調べない）。
    """

    def setUp(self):
        block_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def config(self, data):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return load_config(path, base_dir=self.tmp)

    def test_nothing_is_reread_when_there_are_no_errors(self):
        config = self.config({"schedule": {"bogus_key": 1}})
        with mock.patch.object(configcheck, "check_config", side_effect=AssertionError("reread")):
            cli.log_config_errors(config, [])

    def test_only_errors_are_logged_at_error_level(self):
        # error 1 件（max_sleep_seconds）＋ warning（知らないキー）＋ info（既定値と同じ値）
        config = self.config({"schedule": {"max_sleep_seconds": 0, "bogus_key": 1},
                              "timezone": "Asia/Tokyo"})
        _effective, errors = configcheck.sanitized(config)
        self.assertEqual(len(errors), 1)
        findings = configcheck.check_config(config)
        self.assertEqual({finding.level for finding in findings},
                         {configcheck.ERROR, configcheck.WARNING, configcheck.INFO})
        with logs_enabled(), self.assertLogs("chime", level="ERROR") as captured:
            cli.log_config_errors(config, errors)
        self.assertEqual(len(captured.records), 1, [r.getMessage() for r in captured.records])
        self.assertEqual(captured.records[0].levelno, logging.ERROR)
        self.assertIn("schedule.max_sleep_seconds", captured.records[0].getMessage())

    def test_warnings_and_infos_are_not_logged_even_when_an_error_exists(self):
        config = self.config({"schedule": {"max_sleep_seconds": 0, "bogus_key": 1},
                              "timezone": "Asia/Tokyo"})
        _effective, errors = configcheck.sanitized(config)
        with logs_enabled(), self.assertLogs("chime", level="DEBUG") as captured:
            cli.log_config_errors(config, errors)
        messages = [record.getMessage() for record in captured.records]
        self.assertFalse([m for m in messages if "bogus_key" in m or "既定値と同じ" in m], messages)

    def test_each_error_line_carries_the_file_it_came_from(self):
        config = self.config({"schedule": {"max_sleep_seconds": 0}})
        _effective, errors = configcheck.sanitized(config)
        with logs_enabled(), self.assertLogs("chime", level="ERROR") as captured:
            cli.log_config_errors(config, errors)
        self.assertIn(os.path.join(self.tmp, "config.json"), captured.records[0].getMessage())
        self.assertTrue(captured.records[0].getMessage().startswith("設定の誤り: "))


class CheckModeTest(unittest.TestCase):
    """``--check``（本体は ``chime/check.py``。ここでは配線を見る）。"""

    def setUp(self):
        block_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def config_file(self, data=None):
        base = {"time_signal": {"output_file": os.path.join(self.tmp, "generated", "time_signal.wav")},
                "state": {"file": os.path.join(self.tmp, "cache", "state.json"),
                          "history_file": os.path.join(self.tmp, "cache", "history.jsonl")},
                "tts": {"cache_dir": os.path.join(self.tmp, "cache", "tts")}}
        base.update(data or {})
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(base, handle)
        return path

    def test_check_runs_the_check_with_the_config_as_written(self):
        path = self.config_file({"timezone": "Asia/Tokio"})
        with mock.patch("chime.cli.check.run_check", return_value=0) as run_check:
            code, _ = call(["--config", path, "--check"])
        self.assertEqual(code, 0)
        [config] = run_check.call_args[0]
        self.assertEqual(config.get("timezone"), "Asia/Tokio")  # 既定値に戻す前の設定

    def test_check_returns_what_the_check_returns(self):
        path = self.config_file()
        for expected in (0, 1):
            with mock.patch("chime.cli.check.run_check", return_value=expected):
                self.assertEqual(call(["--config", path, "--check"])[0], expected)

    def test_check_does_not_build_the_app(self):
        path = self.config_file()
        with mock.patch("chime.cli.check.run_check", return_value=0), \
                mock.patch("chime.cli.ChimeApp") as built:
            call(["--config", path, "--check"])
        built.assert_not_called()

    def test_check_shows_errors_itself_instead_of_logging_them_twice(self):
        path = self.config_file({"timezone": "Asia/Tokio"})
        code, output = call(["--config", path, "--check"])
        self.assertEqual(code, 1, output)
        self.assertNotIn("ERROR - 設定の誤り", output)
        self.assertIn("timezone", output)

    def test_check_end_to_end_passes_on_the_shipped_assets_and_writes_nothing(self):
        path = self.config_file()
        before = sorted(os.listdir(self.tmp))
        code, output = call(["--config", path, "--check"])
        self.assertEqual(code, 0, output)
        self.assertIn("== 作り置きの音声 ==", output)
        self.assertIn("138 件すべてそろっています", output)
        self.assertIn("結果:", output)
        # 時報音がまだ無くても警告にとどまり、点検は何も作らない。
        self.assertIn("警告", output)
        self.assertEqual(sorted(os.listdir(self.tmp)), before)

    def test_check_with_an_unreadable_config_returns_2(self):
        code, stdout, stderr = call_split(["--config", os.path.join(self.tmp, "none.json"), "--check"])
        self.assertEqual(code, 2)
        self.assertIn("設定エラー", stderr)
        self.assertEqual(stdout, "")

    def test_print_config_wins_over_check(self):
        path = self.config_file()
        with mock.patch("chime.cli.check.run_check") as run_check:
            code, _, _ = call_split(["--config", path, "--check", "--print-config"])
        self.assertEqual(code, 0)
        run_check.assert_not_called()


class StatusModeTest(unittest.TestCase):
    """``--status`` / ``--wait-idle``（本体は ``chime/status.py``。ここでは配線を見る）。"""

    def setUp(self):
        block_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        data = {"state": {"file": os.path.join(self.tmp, "state.json"),
                          "history_file": os.path.join(self.tmp, "history.jsonl")},
                "tts": {"cache_dir": os.path.join(self.tmp, "tts")}}
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_the_new_options_are_parsed(self):
        parser = build_parser()
        args = parser.parse_args([])
        self.assertFalse(args.check)
        self.assertFalse(args.status)
        self.assertIsNone(args.wait_idle)
        self.assertTrue(parser.parse_args(["--check"]).check)
        self.assertTrue(parser.parse_args(["--status"]).status)
        self.assertEqual(parser.parse_args(["--wait-idle"]).wait_idle, 360)
        self.assertEqual(parser.parse_args(["--wait-idle", "30"]).wait_idle, 30)

    def test_help_lists_the_new_options(self):
        text = build_parser().format_help()
        for option in ("--check", "--status", "--wait-idle"):
            self.assertIn(option, text)
        for option in ("--check", "--status", "--wait-idle"):
            self.assertIn("campus_chime.py " + option, EPILOG)

    def test_status_is_dispatched_with_a_read_only_app(self):
        with mock.patch("chime.cli.status.run_status", return_value=1) as run_status, \
                mock.patch("chime.cli.ChimeApp", wraps=ChimeApp) as built:
            code, _ = call(["--config", self.config, "--status"])
        self.assertEqual(code, 1)
        self.assertIsInstance(run_status.call_args[0][0], ChimeApp)
        self.assertTrue(built.call_args[1]["dry_run"])

    def test_wait_idle_is_dispatched_with_the_scheduler_and_the_limit(self):
        for flags, limit in ((["--wait-idle"], 360), (["--wait-idle", "20"], 20)):
            with mock.patch("chime.cli.status.wait_idle", return_value=1) as wait_idle, \
                    mock.patch("chime.cli.ChimeApp", wraps=ChimeApp) as built:
                code, _ = call(["--config", self.config] + flags)
            self.assertEqual(code, 1)
            scheduler, given = wait_idle.call_args[0]
            self.assertIsInstance(scheduler, Scheduler)
            self.assertEqual(given, limit)
            self.assertTrue(built.call_args[1]["dry_run"])

    def test_status_end_to_end_with_the_commands_faked(self):
        def fake_run(command, **kwargs):
            answers = {"timedatectl": "yes\n", "systemctl": "active\n"}
            return type("Done", (), {"returncode": 0, "stdout": answers[command[0]]})()

        with mock.patch("chime.status.subprocess.run", fake_run), \
                mock.patch("chime.status.env.is_production_linux", return_value=True):
            code, stdout, stderr = call_split(["--config", self.config, "--status", "--backend", "pygame"])
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn(buildinfo.version_string(), stdout)
        self.assertIn("同期済み（NTP）", stdout)
        self.assertIn("動作中（active）", stdout)
        self.assertIn("138 件すべてそろっています", stdout)
        self.assertIn("気になる点は見つかりませんでした。", stdout)

    def test_status_writes_nothing(self):
        with mock.patch("chime.status.subprocess.run", side_effect=FileNotFoundError):
            call_split(["--config", self.config, "--status", "--backend", "pygame"])
        self.assertEqual(sorted(os.listdir(self.tmp)), ["config.json"])

    def test_wait_idle_on_a_weekend_returns_0_without_waiting(self):
        saturday = datetime(2026, 10, 10, 12, 59, 30, tzinfo=TOKYO)
        with mock.patch("chime.cli.ChimeApp.now", lambda self: saturday):
            code, output = call(["--config", self.config, "--wait-idle", "5"])
        self.assertEqual(code, 0, output)
        self.assertIn("放送の時間帯ではありません", output)

    def test_wait_idle_during_a_broadcast_times_out_with_1(self):
        friday = datetime(2026, 10, 9, 13, 0, 30, tzinfo=TOKYO)
        with mock.patch("chime.cli.ChimeApp.now", lambda self: friday), \
                mock.patch("chime.status.time.sleep") as sleep:
            code, stdout, stderr = call_split(["--config", self.config, "--wait-idle", "10"])
        self.assertEqual(code, 1)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("放送の時間帯です", stdout)
        self.assertIn("10 秒待ちましたが", stderr)


class GenerateAssetsTest(unittest.TestCase):
    """``--generate-assets``。``scripts/setup.sh`` が終了コードで分岐する。

    設定ファイルで時報音・状態ファイル・合成キャッシュの出力先を一時フォルダへ
    振り向け、リポジトリには何も書かせない。作り置きの有無は
    ``TTSService.prerecorded_lookup`` を差し替えて、同梱の作り置きの内容に依存させない。
    合成（``synthesize``）は呼ばれてはならないので、呼ばれたら失敗する。
    """

    #: 既定設定（10〜16 時）の時刻アナウンス。時の順に並ぶ。
    ANNOUNCEMENTS = [
        "午前10時をお知らせしたのだ。",
        "午前11時をお知らせしたのだ。",
        "正午をお知らせしたのだ。",
        "午後1時をお知らせしたのだ。",
        "午後2時をお知らせしたのだ。",
        "午後3時をお知らせしたのだ。",
        "午後よじをお知らせしたのだ。",
    ]

    def setUp(self):
        block_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.signal_path = os.path.join(self.tmp, "generated", "time_signal.wav")

    def write_config(self, **extra):
        data = {"time_signal": {"output_file": self.signal_path},
                "state": {"file": os.path.join(self.tmp, "state.json")},
                "tts": {"cache_dir": os.path.join(self.tmp, "tts")}}
        data.update(extra)
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def generate(self, lookup, **extra):
        """(終了コード, 標準出力, 標準エラー出力, lookup に渡された文言の一覧)。"""
        texts = []

        def recording(text):
            texts.append(text)
            return lookup(text)

        path = self.write_config(**extra)
        with mock.patch("chime.tts.TTSService.prerecorded_lookup", side_effect=recording), \
                mock.patch("chime.tts.TTSService.synthesize",
                           side_effect=AssertionError("合成を呼んではならない")):
            code, stdout, stderr = call_split(
                ["--config", path, "--generate-assets", "--backend", "mock"])
        return code, stdout, stderr, texts

    @staticmethod
    def lines_starting_with(output, prefix):
        return [line for line in output.splitlines() if line.startswith(prefix)]

    def test_success_prints_ok_line_per_hour_in_order(self):
        code, stdout, stderr, texts = self.generate(lambda text: "/voice/" + text + ".wav")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertIn("時報音を生成しました: {0}".format(self.signal_path), stdout.splitlines())
        self.assertTrue(os.path.exists(self.signal_path))
        self.assertEqual(texts, self.ANNOUNCEMENTS)
        self.assertEqual(
            self.lines_starting_with(stdout, "  OK "),
            ["  OK {0} -> /voice/{0}.wav".format(text) for text in self.ANNOUNCEMENTS])
        self.assertEqual(self.lines_starting_with(stdout, "  NG "), [])

    def test_all_failures_return_1_with_guidance_on_stderr(self):
        code, stdout, stderr, texts = self.generate(lambda text: None)
        self.assertEqual(code, 1)
        # 時報音は合成不要なので、読み上げが全滅しても先に生成される
        self.assertIn("時報音を生成しました: {0}".format(self.signal_path), stdout.splitlines())
        self.assertTrue(os.path.exists(self.signal_path))
        self.assertEqual(self.lines_starting_with(stdout, "  OK "), [])
        self.assertEqual(
            self.lines_starting_with(stderr, "  NG "),
            ["  NG {0}: 作り置き（assets/voice/）にこの文言がありません".format(text)
             for text in self.ANNOUNCEMENTS])
        lines = stderr.splitlines()
        self.assertIn("7 件の時刻アナウンスの音声を用意できませんでした。", lines)
        self.assertIn("  - 作り置き（assets/voice/）に無い場合: PC で作り直す（docs/SETUP.md 9 章 B）",
                      lines)
        self.assertIn("  - config.json が古く文言を上書きしている場合: docs/SETUP.md 10-7", lines)
        # 見出し → 案内 2 行の順（NG の行より後ろ）
        summary = lines.index("7 件の時刻アナウンスの音声を用意できませんでした。")
        self.assertEqual(summary, 7)
        self.assertEqual(len(lines), 10)

    def test_partial_failure_returns_1_and_counts_only_the_failures(self):
        def missing_noon_only(text):
            return None if text == "正午をお知らせしたのだ。" else "/voice/ok.wav"

        code, stdout, stderr, texts = self.generate(missing_noon_only)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.lines_starting_with(stdout, "  OK ")), 6)
        self.assertEqual(self.lines_starting_with(stderr, "  NG "),
                         ["  NG 正午をお知らせしたのだ。: 作り置き（assets/voice/）にこの文言がありません"])
        self.assertIn("1 件の時刻アナウンスの音声を用意できませんでした。", stderr.splitlines())

    def test_a_phrase_missing_from_prerecorded_is_ng_even_when_a_synthesizer_could_make_it(self):
        """VOICEVOX が動いている PC でも、作り置きに無い文言は「用意できない」と答えること。

        合成エンジンが動いていると、作り置きに無い文言もその場で合成されて「OK」に
        なり、Pi では無音になることに気づけない。確かめるのは作り置きの有無だけ
        （``prerecorded_lookup``）で、合成（``synthesize``）は呼ばない。
        """
        path = self.write_config()
        with mock.patch("chime.tts.TTSService.prerecorded_lookup", return_value=None), \
                mock.patch("chime.tts.TTSService.synthesize",
                           side_effect=lambda text: "/synthesized/" + text + ".wav") as synthesize:
            code, stdout, stderr = call_split(
                ["--config", path, "--generate-assets", "--backend", "mock"])
        self.assertEqual(code, 1, stdout)
        self.assertEqual(self.lines_starting_with(stdout, "  OK "), [])
        self.assertEqual(len(self.lines_starting_with(stderr, "  NG ")), 7)
        synthesize.assert_not_called()

    def test_a_missing_voice_folder_is_reported_as_such(self):
        """作り置きのフォルダそのものが無いときは、文言ではなくフォルダが無いと伝える。"""
        with tempfile.TemporaryDirectory() as empty:
            path = self.write_config(tts={"cache_dir": os.path.join(self.tmp, "tts"),
                                          "prerecorded_dir": os.path.join(empty, "no-voice")})
            code, stdout, stderr = call_split(
                ["--config", path, "--generate-assets", "--backend", "mock"])
        self.assertEqual(code, 1, stdout)
        self.assertEqual(
            self.lines_starting_with(stderr, "  NG ")[0],
            "  NG 午前10時をお知らせしたのだ。: 作り置きのフォルダ（assets/voice/）が見つかりません")

    def test_hourly_range_narrows_the_hours(self):
        code, stdout, stderr, texts = self.generate(
            lambda text: "/voice/" + text + ".wav",
            schedule={"hourly": {"start_hour": 11, "end_hour": 12}})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(texts, ["午前11時をお知らせしたのだ。", "正午をお知らせしたのだ。"])
        self.assertEqual(len(self.lines_starting_with(stdout, "  OK ")), 2)

    def test_single_hour_range(self):
        code, stdout, stderr, texts = self.generate(
            lambda text: "/voice/" + text + ".wav",
            schedule={"hourly": {"start_hour": 16, "end_hour": 16}})
        self.assertEqual(code, 0, stderr)
        self.assertEqual(texts, ["午後よじをお知らせしたのだ。"])


class ModeHandlersTest(unittest.TestCase):
    """``--schedule`` / ``--say`` / ``--test*`` の細かい振る舞い（run の分け方を変えても保つもの）。"""

    OFFLINE = fixture_path("offline_config.json")

    def setUp(self):
        block_network(self)

    def test_schedule_count_below_one_still_shows_one_event(self):
        for count in ("0", "-4"):
            with self.subTest(count=count):
                code, output = call(["--schedule", count])
                self.assertEqual(code, 0)
                self.assertEqual(output.count("  - "), 1, output)

    def test_say_warns_before_logging_the_environment(self):
        """未録音の警告（標準エラー出力）→ 実行環境のログ → 再生、の順。"""
        wav = ANNOUNCE_PATH
        with mock.patch("chime.tts.TTSService.synthesize", return_value=wav):
            code, output = call(["--say", "Qwerty", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertLess(output.index("この文言がありません"), output.index("実行環境:"))
        self.assertLess(output.index("実行環境:"), output.index("dry-run"))

    def test_invalid_hour_is_rejected_after_logging_the_environment(self):
        code, output = call(["--test-hourly", "42", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 2)
        self.assertLess(output.index("実行環境:"), output.index("0〜23"))
        self.assertNotIn("テストモード", output)

    def assert_plays_hour(self, argv, hour, now_hour=14):
        """``argv`` を、現在時刻を ``now_hour`` 時に固定して実行し、``hour`` 時の時報が鳴ること。"""
        wav = ANNOUNCE_PATH
        fixed_now = datetime(2026, 10, 6, now_hour, 30)
        with mock.patch("chime.app.ChimeApp.now", return_value=fixed_now), \
                mock.patch("chime.tts.TTSService.synthesize", return_value=wav):
            code, output = call(argv + ["--dry-run", "--backend", "mock", "--config", self.OFFLINE])
        self.assertEqual(code, 0, output)
        self.assertIn("テストモード: {0} 時の時報を再生します。".format(hour), output)

    def test_test_hourly_without_value_means_the_current_hour(self):
        self.assert_plays_hour(["--test-hourly"], 14)

    def test_test_hourly_with_negative_value_means_the_current_hour(self):
        """負の値は「範囲外」ではなく「現在時刻」の意味になる（0〜23 の検証にかからない）。"""
        for value in ("-1", "-5"):
            with self.subTest(value=value):
                self.assert_plays_hour(["--test-hourly", value], 14)

    def test_test_hourly_with_value_ignores_the_current_hour(self):
        self.assert_plays_hour(["--test-hourly", "0"], 0)
        self.assert_plays_hour(["--test-hourly", "23"], 23)

    def test_test_all_plays_the_current_hour_then_closing(self):
        wav = ANNOUNCE_PATH
        fixed_now = datetime(2026, 10, 6, 15, 10)
        with mock.patch("chime.app.ChimeApp.now", return_value=fixed_now), \
                mock.patch("chime.tts.TTSService.synthesize", return_value=wav):
            code, output = call(["--test-all", "--dry-run", "--backend", "mock",
                                 "--config", self.OFFLINE])
        self.assertEqual(code, 0, output)
        self.assertLess(output.index("テストモード: 15 時の時報を再生します。"),
                        output.index("テストモード: 閉館放送を再生します。"))
        self.assertLess(output.index("閉館放送を再生します。"), output.index("テストを終了します。"))

    def test_test_all_plays_closing_even_when_the_hourly_plan_is_empty(self):
        """時報が再生できなくても閉館放送は続けて試し、どちらかが鳴れば 0 を返す。"""
        def empty_hourly(self, hour, event=None):
            return PlaybackPlan(event=event, segments=[], warnings=["時報音すら用意できませんでした"])

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"audio": {"mock_max_seconds": 0.05}}, handle)
            with mock.patch("chime.sequence.SequenceBuilder.build_hourly", empty_hourly):
                code, output = call(["--config", path, "--test-all", "--backend", "mock"])
        self.assertEqual(code, 0, output)
        self.assertIn("テストモード: 閉館放送を再生します。", output)
        self.assertNotIn("再生できるセグメントがありませんでした", output)

    def test_test_all_returns_1_when_neither_plays(self):
        def empty_hourly(self, hour, event=None):
            return PlaybackPlan(event=event, segments=[], warnings=["時報音すら用意できませんでした"])

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"closing": {"announce_file": "assets/does_not_exist.wav",
                                       "music_file": "assets/also_missing.mp3"}}, handle)
            with mock.patch("chime.sequence.SequenceBuilder.build_hourly", empty_hourly):
                code, output = call(["--config", path, "--test-all", "--backend", "mock"])
        self.assertEqual(code, 1)
        self.assertIn("テストモード: 閉館放送を再生します。", output)
        self.assertIn("再生できるセグメントがありませんでした", output)
        self.assertIn("テストを終了します。", output)


class PauseOptionsTest(unittest.TestCase):
    """休止のオプション（``--pause-today`` / ``--pause-until`` / ``--pause-off`` / ``--reason``）の定義。"""

    def test_defaults_do_not_pause(self):
        args = build_parser().parse_args([])
        self.assertFalse(args.pause_today)
        self.assertIsNone(args.pause_until)
        self.assertFalse(args.pause_off)
        self.assertIsNone(args.reason)

    def test_the_options_are_parsed(self):
        parser = build_parser()
        self.assertTrue(parser.parse_args(["--pause-today"]).pause_today)
        self.assertTrue(parser.parse_args(["--pause-off"]).pause_off)
        args = parser.parse_args(["--pause-until", "2026-11-05", "--reason", "学園祭"])
        self.assertEqual((args.pause_until, args.reason), ("2026-11-05", "学園祭"))

    def test_help_and_the_examples_list_the_options(self):
        text = build_parser().format_help()
        for option in ("--pause-today", "--pause-until", "--pause-off", "--reason"):
            self.assertIn(option, text)
        for example in ("campus_chime.py --pause-today", "campus_chime.py --pause-until 2026-11-05",
                        "campus_chime.py --pause-off"):
            self.assertIn(example, EPILOG)

    def test_help_tells_the_longest_pause(self):
        self.assertIn("最長 31 日", " ".join(build_parser().format_help().split()))

    def test_the_longest_pause_in_the_help_is_the_limit_of_the_code(self):
        # 最長の日数は、ヘルプの本文とオプションの説明の両方に書く。コードの上限（MAX_PAUSE_DAYS）と食い違わない。
        for text in (EPILOG, build_parser().format_help()):
            days = re.findall(r"最長 (\d+) 日", text)
            self.assertTrue(days)
            self.assertEqual({int(day) for day in days}, {MAX_PAUSE_DAYS})

    def test_the_pause_group_says_who_runs_it(self):
        help_text = " ".join(build_parser().format_help().split())
        self.assertIn("放送の一時休止（常駐中のサービスにそのまま効く。サービスの利用者で実行する）", help_text)

    def test_every_pause_option_has_an_example_line(self):
        examples = EPILOG.replace("campus_chime.py --pause-today --reason", "campus_chime.py --reason")
        for option in ("--pause-today", "--pause-until", "--pause-off", "--reason"):
            self.assertIn("campus_chime.py " + option, examples)

    def test_the_status_help_names_the_pause_row_and_the_closures(self):
        # --status は「放送の休止」の行と、近い休館日の節を出す。ヘルプも使用例もそう言う。
        [example] = [line for line in EPILOG.splitlines() if "campus_chime.py --status" in line]
        described = re.search(r"--status (いまの状態.*?) --wait-idle",
                              " ".join(build_parser().format_help().split()))
        self.assertIsNotNone(described)
        for text in (example, described.group(1)):
            for word in ("放送の休止", "休館日"):
                self.assertIn(word, text)

    def test_given_tells_a_zero_from_not_given(self):
        args = build_parser().parse_args(["--test-hourly", "0"])
        self.assertTrue(cli.given(args, "test_hourly"))
        self.assertFalse(cli.given(args, "test"))
        self.assertFalse(cli.given(args, "say"))

    def test_parse_day_accepts_only_real_dates_in_ascii(self):
        self.assertEqual(cli.parse_day("2026-11-05"), date(2026, 11, 5))
        for text in ("", "tomorrow", "2026-13-01", "2026-02-30", "2026-1-5", "2026/11/05",
                     "２０２６-11-05", " 2026-11-05", "2026-11-05 ", "20261105"):
            self.assertIsNone(cli.parse_day(text), repr(text))


class PauseCommandCase(unittest.TestCase):
    """一時フォルダの設定で、休止のコマンドを動かす土台。

    時計は水曜 2026-10-14 12:30。実行している利用者は ``pi``（root ではない）に固定する
    （root で動くテスト環境でも同じ結果にするため）。休止の記録・状態・履歴は一時フォルダ。
    """

    NOW = datetime(2026, 10, 14, 12, 30, tzinfo=TOKYO)

    def setUp(self):
        block_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.pause_path = os.path.join(self.tmp, "pause.json")
        self.write_config({})
        self.clock = {"now": self.NOW}
        for patcher in (mock.patch("os.geteuid", return_value=1000),
                        mock.patch("chime.cli.ChimeApp.now", lambda app: self.clock["now"])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_config(self, extra):
        data = {"state": {"file": os.path.join(self.tmp, "state.json"),
                          "history_file": os.path.join(self.tmp, "history.jsonl"),
                          "pause_file": self.pause_path},
                "tts": {"cache_dir": os.path.join(self.tmp, "tts")}}
        for section, values in extra.items():
            data.setdefault(section, {}).update(values)
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def run_cli(self, *argv):
        """(終了コード, 標準出力, 標準エラー出力)。"""
        return call_split(["--config", self.config] + list(argv))

    def pause_record(self):
        with open(self.pause_path, encoding="utf-8") as handle:
            return json.load(handle)

    def put_pause_file(self, **record):
        values = {"v": 1, "first_day": "2026-10-14", "last_day": "2026-10-15",
                  "set_at": "2026-10-14T09:00:00+09:00", "reason": "点検"}
        values.update(record)
        with open(self.pause_path, "w", encoding="utf-8") as handle:
            json.dump(values, handle)

    def files(self):
        return sorted(os.listdir(self.tmp))


class PauseTodayTest(PauseCommandCase):
    def test_it_pauses_today_and_says_what_stops_and_when_it_resumes(self):
        code, out, err = self.run_cli("--pause-today")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("10/14 の放送（時報 4 回・閉館 1 回）を止めます。10/15 10:00 に再開します。", out)

    def test_the_record_is_written_for_today(self):
        self.run_cli("--pause-today", "--reason", "点検")
        record = self.pause_record()
        self.assertEqual((record["v"], record["first_day"], record["last_day"], record["reason"]),
                         (1, "2026-10-14", "2026-10-14", "点検"))
        self.assertEqual(record["set_at"], "2026-10-14T12:30:00+09:00")
        self.assertNotIn("pause.json.", " ".join(self.files()))   # 一時ファイルを残さない

    def test_the_reason_is_shown_back(self):
        _, out, _ = self.run_cli("--pause-today", "--reason", "点検")
        self.assertIn("理由: 点検", out)

    def test_without_a_reason_there_is_no_reason_line(self):
        _, out, _ = self.run_cli("--pause-today")
        self.assertNotIn("理由", out)
        self.assertEqual(self.pause_record()["reason"], "")

    def test_it_tells_that_the_running_service_follows_without_a_restart(self):
        _, out, _ = self.run_cli("--pause-today")
        self.assertIn("常駐中のサービスにはそのまま効きます（再起動は要りません）", out)
        self.assertIn("--pause-off", out)

    def test_what_has_already_played_today_is_not_counted(self):
        self.clock["now"] = datetime(2026, 10, 14, 16, 30, tzinfo=TOKYO)
        _, out, _ = self.run_cli("--pause-today")
        self.assertIn("10/14 の放送（時報 0 回・閉館 1 回）を止めます。", out)   # 16:00 の時報はもう鳴った
        self.clock["now"] = datetime(2026, 10, 14, 17, 30, tzinfo=TOKYO)
        _, out, _ = self.run_cli("--pause-today")
        self.assertIn("10/14 の放送（時報 0 回・閉館 0 回）を止めます。10/15 10:00 に再開します。", out)

    def test_a_chime_whose_play_time_is_exactly_now_is_counted(self):
        # ちょうど再生開始の時刻に実行したとき、その回は「これから鳴る」に数える
        # （Scheduler.iter_events が play_at >= start を残すのと同じ）。
        self.clock["now"] = datetime(2026, 10, 14, 9, 59, 57, tzinfo=TOKYO)   # 10 時の時報の再生開始
        code, out, err = self.run_cli("--dry-run", "--pause-today")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("10/14 の放送（時報 7 回・閉館 1 回）を止めます。", out)

    def test_a_chime_one_second_past_its_play_time_is_not_counted(self):
        self.clock["now"] = datetime(2026, 10, 14, 9, 59, 58, tzinfo=TOKYO)
        _, out, _ = self.run_cli("--dry-run", "--pause-today")
        self.assertIn("（時報 6 回・閉館 1 回）", out)

    def test_a_value_error_from_the_store_is_exit_2_with_its_message(self):
        # 先に確かめたはずの誤りが、書く段階で ValueError になったときも、終了コード 2 で理由を言う。
        with mock.patch("chime.pause.PauseStore.set", side_effect=ValueError("日付の誤りなのだ")):
            code, out, err = self.run_cli("--pause-today")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "日付の誤りなのだ\n")
        self.assertFalse(os.path.exists(self.pause_path))

    def test_a_day_with_no_broadcast_says_zero(self):
        self.clock["now"] = datetime(2026, 10, 18, 12, 30, tzinfo=TOKYO)   # 日曜
        code, out, _ = self.run_cli("--pause-today")
        self.assertEqual(code, 0)
        self.assertIn("10/18 の放送（時報 0 回・閉館 0 回）を止めます。10/19 10:00 に再開します。", out)

    def test_a_closure_tomorrow_moves_the_resume_day(self):
        self.write_config({"schedule": {"closures": {"dates": ["2026-10-15"], "ranges": []}}})
        _, out, _ = self.run_cli("--pause-today")
        self.assertIn("10/16 10:00 に再開します。", out)

    def test_today_is_the_configured_zone_not_the_machine_zone(self):
        self.clock["now"] = datetime(2026, 10, 14, 0, 30, tzinfo=TOKYO)   # UTC では前日
        self.run_cli("--pause-today")
        self.assertEqual(self.pause_record()["first_day"], "2026-10-14")

    def test_pausing_again_replaces_the_record(self):
        self.run_cli("--pause-today", "--reason", "一度目")
        self.run_cli("--pause-until", "2026-10-15", "--reason", "二度目")
        record = self.pause_record()
        self.assertEqual((record["last_day"], record["reason"]), ("2026-10-15", "二度目"))

    def test_no_state_or_history_is_touched(self):
        self.run_cli("--pause-today")
        self.assertEqual(self.files(), ["config.json", "pause.json"])


class PauseUntilTest(PauseCommandCase):
    def test_it_pauses_the_whole_span_and_counts_the_weekdays(self):
        code, out, err = self.run_cli("--pause-until", "2026-10-16", "--reason", "学園祭の準備")
        self.assertEqual((code, err), (0, ""), out)
        # 水（今日の残り 4 + 1）・木・金（それぞれ 7 + 1）。土日は鳴らない。
        self.assertIn("10/14〜10/16 の放送（時報 18 回・閉館 3 回）を止めます。10/19 10:00 に再開します。", out)
        self.assertIn("理由: 学園祭の準備", out)
        self.assertEqual(self.pause_record()["last_day"], "2026-10-16")

    def test_closure_days_inside_the_span_are_not_counted(self):
        self.write_config({"schedule": {"closures": {"dates": ["2026-10-15"], "ranges": []}}})
        _, out, _ = self.run_cli("--pause-until", "2026-10-16")
        self.assertIn("10/14〜10/16 の放送（時報 11 回・閉館 2 回）を止めます。", out)

    def test_until_today_is_a_one_day_pause(self):
        _, out, _ = self.run_cli("--pause-until", "2026-10-14")
        self.assertIn("10/14 の放送（時報 4 回・閉館 1 回）を止めます。", out)
        self.assertNotIn("〜", out)

    def test_the_longest_pause_is_31_days_including_today(self):
        code, out, _ = self.run_cli("--pause-until", "2026-11-13")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.pause_record()["last_day"], "2026-11-13")

    def test_a_pause_longer_than_31_days_is_refused_and_points_to_closures(self):
        code, out, err = self.run_cli("--pause-until", "2026-11-14")
        self.assertEqual(code, 2)
        self.assertIn("最長 31 日", err)
        self.assertIn("2026-11-14 まででは 32 日", err)
        self.assertIn("schedule.closures", err)
        self.assertNotIn("止めます", out)
        self.assertEqual(self.files(), ["config.json"])

    def test_a_date_in_the_past_is_refused(self):
        code, out, err = self.run_cli("--pause-until", "2026-10-13")
        self.assertEqual(code, 2)
        self.assertIn("過去の日付", err)
        self.assertEqual(self.files(), ["config.json"])

    def test_after_the_end_of_the_schedule_the_resume_line_says_so(self):
        self.write_config({"schedule": {"hourly": {"enabled": False}, "closing": {"enabled": False}}})
        code, out, _ = self.run_cli("--pause-today")
        self.assertEqual(code, 0)
        self.assertIn("このあと鳴らす予定の放送がありません", out)

    def test_a_reason_that_cannot_be_written_as_utf8_is_refused(self):
        # 端末の文字コードが合っていないと、引数に孤立したサロゲートが混ざる。
        for extra in ([], ["--dry-run"]):
            code, out, err = self.run_cli("--pause-today", "--reason", "\udcff", *extra)
            self.assertEqual(code, 2, extra)
            self.assertIn("文字コードの正しくない文字", err)
            self.assertEqual(out, "")
            self.assertEqual(self.files(), ["config.json"])

    def test_a_past_date_is_refused_in_a_dry_run_too(self):
        code, _, err = self.run_cli("--pause-until", "2026-10-13", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("過去の日付", err)

    def test_a_pause_longer_than_31_days_is_refused_in_a_dry_run_too(self):
        code, _, err = self.run_cli("--pause-until", "2026-11-14", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("schedule.closures", err)


class PauseArgumentsTest(PauseCommandCase):
    """引数の組み合わせの誤りは、設定を読む前に断る（終了コード 2、何も書かない）。"""

    def setUp(self):
        super().setUp()
        loader = mock.patch("chime.cli.load_config", side_effect=AssertionError("設定を読んではいけない"))
        loader.start()
        self.addCleanup(loader.stop)
        built = mock.patch("chime.cli.ChimeApp", side_effect=AssertionError("組み立ててはいけない"))
        built.start()
        self.addCleanup(built.stop)

    def refused(self, *argv):
        code, out, err = call_split(list(argv))
        self.assertEqual(code, 2, (out, err))
        self.assertEqual(out, "")
        self.assertEqual(self.files(), ["config.json"])
        return err

    def test_reason_alone_is_refused(self):
        self.assertIn("--reason は --pause-today か --pause-until と一緒に", self.refused("--reason", "x"))

    def test_reason_with_pause_off_is_refused(self):
        self.assertIn("--reason は --pause-off では使えません",
                      self.refused("--pause-off", "--reason", "x"))

    def test_two_pause_options_are_refused(self):
        err = self.refused("--pause-today", "--pause-off")
        self.assertIn("--pause-today と --pause-off は同時に指定できません", err)
        self.assertIn("--pause-today と --pause-until", self.refused("--pause-today", "--pause-until", "2026-11-05"))

    def test_a_pause_with_another_mode_is_refused(self):
        for other in (["--status"], ["--schedule"], ["--test"], ["--check"], ["--wait-idle"],
                      ["--say", "x"], ["--print-config"], ["--weather"], ["--generate-assets"],
                      ["--test-all"], ["--test-hourly", "0"], ["--test-hourly"]):
            err = self.refused("--pause-today", *other)
            self.assertIn("--pause-today は {0} と同時に指定できません".format(other[0]), err)

    def test_a_date_that_is_not_a_date_is_refused(self):
        for text in ("", "tomorrow", "2026-13-01", "2026-02-30", "2026-1-5", "２０２６-11-05"):
            err = self.refused("--pause-until", text)
            self.assertIn("YYYY-MM-DD", err)

    def test_a_bad_pause_argument_is_refused_before_the_root_check(self):
        with mock.patch("os.geteuid", return_value=0):
            err = self.refused("--pause-until", "tomorrow")
        self.assertIn("YYYY-MM-DD", err)
        self.assertNotIn("root", err)


class PauseAsRootTest(PauseCommandCase):
    """root では休止を使えない（``cache/`` が root の持ち物になり、サービスが書けなくなる）。"""

    def test_each_pause_option_is_refused_for_root(self):
        built = mock.patch("chime.cli.ChimeApp", side_effect=AssertionError("組み立ててはいけない"))
        built.start()
        self.addCleanup(built.stop)
        for argv in (["--pause-today"], ["--pause-until", "2026-10-16"], ["--pause-off"],
                     ["--pause-today", "--reason", "x"]):
            with mock.patch("os.geteuid", return_value=0):
                code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("root では {0} を使えません".format(argv[0]), err)
            self.assertIn("サービスの利用者（pi など）で実行してください", err)
            self.assertEqual(out, "")
            self.assertEqual(self.files(), ["config.json"])

    def test_other_modes_are_not_affected_by_the_root_rule(self):
        with mock.patch("os.geteuid", return_value=0):
            code, out, _ = self.run_cli("--schedule", "1")
        self.assertEqual(code, 0, out)

    def test_an_os_without_uids_is_not_root(self):
        with mock.patch.object(cli.os, "geteuid", None):
            self.assertFalse(cli.running_as_root())
        with mock.patch("os.geteuid", return_value=0):
            self.assertTrue(cli.running_as_root())
        with mock.patch("os.geteuid", return_value=1000):
            self.assertFalse(cli.running_as_root())


class PauseOffTest(PauseCommandCase):
    def test_it_lifts_a_pause_and_says_what_was_lifted_and_what_is_next(self):
        self.run_cli("--pause-until", "2026-10-15", "--reason", "点検")
        code, out, err = self.run_cli("--pause-off")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("休止を解除しました: 10/14〜10/15（点検）", out)
        self.assertIn("次の放送は 10/14 13:00 です。", out)
        self.assertFalse(os.path.exists(self.pause_path))

    def test_without_a_pause_it_says_nothing_was_paused(self):
        code, out, err = self.run_cli("--pause-off")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("止めている放送はありません。", out)
        self.assertEqual(self.files(), ["config.json"])

    def test_an_expired_record_is_removed_and_said_so(self):
        self.put_pause_file(first_day="2026-10-10", last_day="2026-10-12")
        code, out, _ = self.run_cli("--pause-off")
        self.assertEqual(code, 0)
        self.assertIn("期限が切れていた（または読めなかった）休止の記録を消しました", out)
        self.assertFalse(os.path.exists(self.pause_path))

    def test_a_broken_record_is_removed_too(self):
        with open(self.pause_path, "w", encoding="utf-8") as handle:
            handle.write("{ broken")
        code, out, _ = self.run_cli("--pause-off")
        self.assertEqual(code, 0)
        self.assertIn("消しました", out)
        self.assertFalse(os.path.exists(self.pause_path))

    def test_a_record_that_cannot_be_removed_is_exit_1(self):
        self.put_pause_file()
        with mock.patch("chime.pause.os.remove", side_effect=PermissionError("拒否されました")):
            code, out, err = self.run_cli("--pause-off")
        self.assertEqual(code, 1)
        self.assertIn("休止の記録を消せませんでした", err)
        self.assertTrue(os.path.exists(self.pause_path))

    def pause_then_move_to(self, paused_at, now, *argv):
        """``paused_at`` に休止を書き、時計を ``now`` に進めて ``--pause-off`` を実行する。"""
        self.clock["now"] = paused_at
        self.assertEqual(self.run_cli("--pause-until", "2026-10-15", "--reason", "点検")[0], 0)
        self.clock["now"] = now
        return self.run_cli("--pause-off", *argv)

    def test_it_says_which_chime_the_service_will_catch_up_on(self):
        # 12 時の時報（再生開始 11:59:57）の 23 秒後に解除すると、サービスがその回を追いかけて鳴らす。
        # 「次の放送は 13:00」だけだと、すぐ鳴る回が抜けて驚かれる。
        code, out, err = self.pause_then_move_to(
            datetime(2026, 10, 14, 11, 50, tzinfo=TOKYO), datetime(2026, 10, 14, 12, 0, 20, tzinfo=TOKYO))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [
            "休止を解除しました: 10/14〜10/15（点検）",
            "開始から 90 秒未満の回は、サービスが追いかけて鳴らします:",
            "  - 時報 2026-10-14 12:00:00（再生開始 11:59:57）",
            "次の放送は 10/14 13:00 です。",
        ])

    def test_a_chime_the_service_may_notice_too_late_is_not_promised(self):
        # 12 時の時報（再生開始 11:59:57）の 108 秒後に解除する。常駐中のサービスが解除に気づくのは、待機の区切り
        # （schedule.max_sleep_seconds）の終わりで、最大 30 秒あと。猶予（120 秒）を過ぎて間に合わないことがあるので、
        # 「追いかけて鳴らします」と約束せず、「間に合えば」と言う。
        code, out, err = self.pause_then_move_to(
            datetime(2026, 10, 14, 11, 50, tzinfo=TOKYO), datetime(2026, 10, 14, 12, 1, 45, tzinfo=TOKYO))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [
            "休止を解除しました: 10/14〜10/15（点検）",
            "間に合えば追いかけて鳴ります（サービスが解除に気づくまで最大 30 秒かかり、"
            "開始から 120 秒を過ぎると鳴りません）:",
            "  - 時報 2026-10-14 12:00:00（再生開始 11:59:57）",
            "次の放送は 10/14 13:00 です。",
        ])
        self.assertNotIn("サービスが追いかけて鳴らします", out)

    def test_the_certain_chimes_are_the_ones_the_service_still_picks_when_it_notices_last(self):
        # 「確実」の境目は、実際の動きと同じ。解除の max_sleep_seconds 秒後（サービスが最後に気づく時刻）にも
        # Scheduler が選ぶ回だけを約束する。選ばないかもしれない回は、約束しない（ちょうど 90 秒の境目は確かめない）。
        self.pause_then_move_to(
            datetime(2026, 10, 14, 11, 50, tzinfo=TOKYO), datetime(2026, 10, 14, 12, 0, 20, tzinfo=TOKYO))
        app = ChimeApp(load_config(self.config), backend="mock", dry_run=True)
        play_at = datetime(2026, 10, 14, 11, 59, 57, tzinfo=TOKYO)
        for age in (1, 59, 60, 89, 91, 92, 119, 120):
            lifted = play_at + timedelta(seconds=age)
            noticed = lifted + timedelta(seconds=app.scheduler.max_sleep)   # 解除に気づく、いちばん遅い時刻
            self.clock["now"] = lifted
            with self.subTest(seconds_after_play_time=age), mock.patch.object(app, "now", lambda: lifted):
                [event] = cli.catch_up_events(app)
                chosen = app.scheduler.next_event(noticed)
                picked_even_when_noticed_last = chosen is not None and chosen.key == event.key
                sure, maybe = cli.split_catch_up(app, [event])
                self.assertEqual((bool(sure), bool(maybe)), (picked_even_when_noticed_last,
                                                             not picked_even_when_noticed_last))

    def test_a_chime_exactly_at_the_edge_of_the_certain_window_is_not_promised(self):
        # 確実なのは、経過が「猶予 − 待機の長さ」（120 − 30 = 90 秒）に満たない回だけ。ちょうど 90 秒は、約束しない。
        app = ChimeApp(load_config(self.config), backend="mock", dry_run=True)
        play_at = datetime(2026, 10, 14, 11, 59, 57, tzinfo=TOKYO)
        [event] = [e for e in app.scheduler.events_for_date(play_at.date()) if e.key == "hourly:12"]
        for age, expected in ((89.5, "certain"), (90, "maybe"), (90.5, "maybe")):
            moment = play_at + timedelta(seconds=age)
            with self.subTest(seconds_after_play_time=age), mock.patch.object(app, "now", lambda: moment):
                certain, maybe = cli.split_catch_up(app, [event])
                self.assertEqual(("certain" if certain else "maybe", len(certain) + len(maybe)), (expected, 1))

    def test_a_wait_as_long_as_the_grace_promises_nothing(self):
        # schedule.max_sleep_seconds が猶予以上だと、どの回も「間に合えば」にとどまる（確実な回は無い）
        self.write_config({"schedule": {"max_sleep_seconds": 300}})
        code, out, err = self.pause_then_move_to(
            datetime(2026, 10, 14, 11, 50, tzinfo=TOKYO), datetime(2026, 10, 14, 12, 0, 20, tzinfo=TOKYO))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("間に合えば追いかけて鳴ります（サービスが解除に気づくまで最大 300 秒かかり、"
                      "開始から 120 秒を過ぎると鳴りません）:", out)
        self.assertIn("  - 時報 2026-10-14 12:00:00（再生開始 11:59:57）", out)
        self.assertNotIn("サービスが追いかけて鳴らします", out)

    def test_certain_and_maybe_chimes_are_listed_in_their_own_groups(self):
        # 猶予 2 時間の設定で、12:59:30 に解除する。11:59:57 の回（経過 3573 秒）は確実、10:59:57 の回（7173 秒）は
        # 7170 秒（猶予 − 30 秒）を超えているので「間に合えば」。
        self.write_config({"schedule": {"catchup_grace_seconds": 7200}})
        code, out, err = self.pause_then_move_to(
            datetime(2026, 10, 14, 10, 0, tzinfo=TOKYO), datetime(2026, 10, 14, 12, 59, 30, tzinfo=TOKYO))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines(), [
            "休止を解除しました: 10/14〜10/15（点検）",
            "開始から 7170 秒未満の回は、サービスが追いかけて鳴らします:",
            "  - 時報 2026-10-14 12:00:00（再生開始 11:59:57）",
            "間に合えば追いかけて鳴ります（サービスが解除に気づくまで最大 30 秒かかり、"
            "開始から 7200 秒を過ぎると鳴りません）:",
            "  - 時報 2026-10-14 11:00:00（再生開始 10:59:57）",
            "次の放送は 10/14 13:00 です。",
        ])

    def test_the_catch_up_is_what_the_scheduler_really_picks(self):
        # 案内する回は、Scheduler.next_event の取りこぼしの拾い上げ（追いかけ再生）が選ぶ回と同じ。
        self.pause_then_move_to(
            datetime(2026, 10, 14, 11, 50, tzinfo=TOKYO), datetime(2026, 10, 14, 12, 0, 20, tzinfo=TOKYO))
        app = ChimeApp(load_config(self.config), backend="mock", dry_run=True)
        app.now = lambda: self.clock["now"]
        for offset in (0, 59, 60, 119, 120, 121, 300):
            moment = datetime(2026, 10, 14, 11, 59, 57, tzinfo=TOKYO) + timedelta(seconds=offset)
            self.clock["now"] = moment
            with self.subTest(seconds_after_play_time=offset), \
                    mock.patch.object(app, "now", lambda moment=moment: moment):
                chosen = app.scheduler.next_event(moment)
                late = chosen is not None and chosen.play_at < moment
                found = cli.catch_up_events(app)
                self.assertEqual(bool(found), late)
                if late:
                    self.assertEqual(found, [chosen])

    def test_a_chime_that_is_too_late_or_already_played_is_not_announced(self):
        # 猶予（120 秒）を過ぎた回は鳴らない。鳴らし終えた回は、追いかけない。
        late = self.pause_then_move_to(
            datetime(2026, 10, 14, 11, 50, tzinfo=TOKYO), datetime(2026, 10, 14, 12, 2, 1, tzinfo=TOKYO))
        self.assertNotIn("追いかけ", late[1])
        State(os.path.join(self.tmp, "state.json")).mark_fired("hourly:12", "2026-10-14")
        played = self.pause_then_move_to(
            datetime(2026, 10, 14, 11, 50, tzinfo=TOKYO), datetime(2026, 10, 14, 12, 0, 20, tzinfo=TOKYO))
        self.assertNotIn("追いかけ", played[1])

    def test_nothing_is_announced_when_no_pause_was_in_force(self):
        # 止めていない（期限切れ・壊れた記録）なら、サービスはもう鳴らしているので、案内しない。
        self.clock["now"] = datetime(2026, 10, 14, 12, 0, 20, tzinfo=TOKYO)
        self.put_pause_file(first_day="2026-10-10", last_day="2026-10-12")
        code, out, _ = self.run_cli("--pause-off")
        self.assertEqual(code, 0)
        self.assertNotIn("追いかけ", out)

    def test_many_late_chimes_are_listed_up_to_three_and_counted(self):
        # 猶予を長くした設定で、止めているあいだに過ぎた回が多いとき（長い猶予は設定の誤りだが、案内は崩れない）。
        self.write_config({"schedule": {"catchup_grace_seconds": 4 * 3600}})
        code, out, _ = self.pause_then_move_to(
            datetime(2026, 10, 14, 10, 0, tzinfo=TOKYO), datetime(2026, 10, 14, 15, 0, 20, tzinfo=TOKYO))
        self.assertEqual(code, 0)
        self.assertIn("開始から 14370 秒未満の回は、サービスが追いかけて鳴らします:", out)
        self.assertEqual(out.count("  - 時報"), cli.CATCH_UP_LIMIT)
        self.assertIn("  ほか 1 件", out)

    def test_exactly_as_many_chimes_as_the_limit_are_listed_without_a_remainder_line(self):
        # 3 件ちょうど（上限と同じ）のとき、「ほか 0 件」を出さない。猶予 3 時間で、15:00:20 には 3 回が該当する。
        self.write_config({"schedule": {"catchup_grace_seconds": 3 * 3600}})
        code, out, err = self.pause_then_move_to(
            datetime(2026, 10, 14, 10, 0, tzinfo=TOKYO), datetime(2026, 10, 14, 15, 0, 20, tzinfo=TOKYO))
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.count("  - 時報"), cli.CATCH_UP_LIMIT)
        self.assertNotIn("ほか", out)

    def test_a_chime_that_started_before_midnight_is_still_announced_after_midnight(self):
        # 猶予の範囲が日付をまたぐとき、前の日の予定も調べる（23:59 の閉館放送を、0:00:20 に解除）。
        self.write_config({"schedule": {"closing": {"hour": 23, "minute": 59}}})
        code, out, err = self.pause_then_move_to(
            datetime(2026, 10, 14, 23, 50, tzinfo=TOKYO), datetime(2026, 10, 15, 0, 0, 20, tzinfo=TOKYO))
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("開始から 90 秒未満の回は、サービスが追いかけて鳴らします:", out)
        self.assertIn("閉館放送 2026-10-14 23:59:00", out)

    def test_a_dry_run_announces_no_catch_up(self):
        code, out, err = self.pause_then_move_to(
            datetime(2026, 10, 14, 11, 50, tzinfo=TOKYO), datetime(2026, 10, 14, 12, 0, 20, tzinfo=TOKYO),
            "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("追いかけ", out)
        self.assertTrue(os.path.exists(self.pause_path))

    def test_a_record_that_is_a_folder_cannot_be_removed_and_the_message_says_what_to_do(self):
        # --check は「そのフォルダを移すか消す」と案内する。--pause-off では消せないので、そう言う。
        os.mkdir(self.pause_path)
        code, _, err = self.run_cli("--pause-off")
        self.assertEqual(code, 1)
        self.assertIn("休止の記録を消せませんでした", err)
        self.assertIn("がフォルダです", err)
        self.assertIn("そのフォルダを移すか消して", err)
        self.assertTrue(os.path.isdir(self.pause_path))

    def test_pause_off_then_the_schedule_has_no_pause_line(self):
        self.run_cli("--pause-today")
        self.run_cli("--pause-off")
        _, out, _ = self.run_cli("--schedule", "2")
        self.assertNotIn("止めています", out)


class PauseDryRunTest(PauseCommandCase):
    def test_a_dry_run_shows_what_would_happen_and_writes_nothing(self):
        code, out, err = self.run_cli("--pause-today", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("--dry-run のため、休止の記録は書きません", out)
        self.assertIn("10/14 の放送（時報 4 回・閉館 1 回）を止めます。", out)
        self.assertEqual(self.files(), ["config.json"])

    def test_a_dry_run_of_pause_off_keeps_the_record(self):
        self.put_pause_file()
        code, out, _ = self.run_cli("--pause-off", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("--dry-run のため、休止の記録は消しません", out)
        self.assertIn("止めています: 10/14〜10/15（点検）", out)
        self.assertTrue(os.path.exists(self.pause_path))


class PauseFailureTest(PauseCommandCase):
    def test_a_record_that_cannot_be_written_is_exit_1(self):
        with mock.patch("chime.pause.write_json_atomic", side_effect=OSError("読み取り専用です")):
            code, out, err = self.run_cli("--pause-today")
        self.assertEqual(code, 1)
        self.assertIn("休止の記録を書けませんでした: 読み取り専用です", err)
        self.assertNotIn("止めます", out)

    def test_a_failure_points_to_the_check_for_the_fix(self):
        # 書けない・消せないときの次の手がかり。--check の「書き込み」の節が直し方まで出す。
        with mock.patch("chime.pause.write_json_atomic", side_effect=PermissionError("拒否されました")):
            _, _, written = self.run_cli("--pause-today")
        self.put_pause_file()
        with mock.patch("chime.pause.os.remove", side_effect=PermissionError("拒否されました")):
            _, _, removed = self.run_cli("--pause-off")
        for err in (written, removed):
            self.assertIn("拒否されました", err)
            self.assertIn("python3 campus_chime.py --check", err)

    def test_a_record_that_is_a_folder_cannot_be_written_and_the_message_says_what_to_do(self):
        os.mkdir(self.pause_path)
        code, out, err = self.run_cli("--pause-today")
        self.assertEqual(code, 1)
        self.assertNotIn("止めます", out)
        self.assertIn("休止の記録を書けませんでした", err)
        self.assertIn("がフォルダです", err)
        self.assertIn("そのフォルダを移すか消して", err)
        self.assertEqual(self.files(), ["config.json", "pause.json"])   # 一時ファイルを残さない

    def test_an_empty_place_for_the_record_means_pausing_is_not_available(self):
        self.write_config({"state": {"pause_file": ""}})
        for argv in (["--pause-today"], ["--pause-off"]):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("state.pause_file", err)
        self.assertEqual(self.files(), ["config.json"])


class ScheduleWithPauseTest(PauseCommandCase):
    def test_a_paused_schedule_says_so_on_top_and_lists_after_the_pause(self):
        self.run_cli("--pause-until", "2026-10-15", "--reason", "点検")
        code, out, _ = self.run_cli("--schedule", "3")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("現在時刻: 2026-10-14 12:30:00"), lines[0])
        self.assertEqual(lines[1], "止めています: 10/14〜10/15（点検）")
        self.assertEqual(lines[2], "次回以降の予定（休止のあいだは省いています）:")
        listed = [line for line in lines if line.startswith("  - ")]
        self.assertEqual(len(listed), 3)
        self.assertIn("2026-10-16 10:00:00", listed[0])   # 休止が明けた金曜の 10 時から

    def test_a_pause_without_a_reason_shows_just_the_span(self):
        self.run_cli("--pause-today")
        _, out, _ = self.run_cli("--schedule", "1")
        self.assertIn("止めています: 10/14\n", out)

    def test_without_a_pause_the_schedule_is_what_it_was(self):
        _, out, _ = self.run_cli("--schedule", "3")
        lines = out.splitlines()
        self.assertEqual(lines[1], "次回以降の予定:")
        self.assertNotIn("止めています", out)
        listed = [line for line in lines if line.startswith("  - ")]
        self.assertEqual(len(listed), 3)
        self.assertIn("2026-10-14 13:00:00", listed[0])

    def test_an_expired_pause_is_not_shown(self):
        self.put_pause_file(first_day="2026-10-10", last_day="2026-10-13")
        _, out, _ = self.run_cli("--schedule", "1")
        self.assertNotIn("止めています", out)

    def test_a_broken_record_is_not_shown_as_a_pause(self):
        with open(self.pause_path, "w", encoding="utf-8") as handle:
            handle.write("nonsense")
        code, out, _ = self.run_cli("--schedule", "1")
        self.assertEqual(code, 0)
        self.assertNotIn("止めています", out)
        self.assertIn("2026-10-14 13:00:00", out)

    def test_closure_days_are_shown_in_the_list(self):
        self.write_config({"schedule": {"closures": {
            "dates": [{"date": "2026-10-16", "name": "学園祭"}], "ranges": []}}})
        _, out, _ = self.run_cli("--schedule", "20")
        self.assertIn("  休館 2026-10-16（金） 学園祭", out)
        self.assertNotIn("2026-10-16 10:00:00", out)   # その日は鳴らない
        self.assertIn("2026-10-19 10:00:00", out)

    def test_closure_days_and_a_pause_together(self):
        self.write_config({"schedule": {"closures": {
            "dates": [{"date": "2026-10-16", "name": "学園祭"}], "ranges": []}}})
        self.run_cli("--pause-until", "2026-10-15")
        _, out, _ = self.run_cli("--schedule", "3")
        self.assertIn("止めています: 10/14〜10/15", out)
        # 休止が明けた金曜は休館日。その行と、次に鳴る月曜の予定が並ぶ。
        self.assertIn("  休館 2026-10-16（金） 学園祭", out)
        self.assertIn("2026-10-19 10:00:00", out)

    def test_a_closure_line_never_uses_the_word_for_the_pause_command(self):
        # 「休止」は --pause-* の言葉。休館日の行と取り違えないよう、行頭は「休館」。
        self.write_config({"schedule": {"closures": {
            "dates": [{"date": "2026-10-16", "name": "学園祭"}], "ranges": []}}})
        self.run_cli("--pause-until", "2026-10-15")
        _, out, _ = self.run_cli("--schedule", "3")
        self.assertEqual([line for line in out.splitlines() if "休止" in line],
                         ["次回以降の予定（休止のあいだは省いています）:"])

    def test_a_count_of_zero_or_less_still_lists_one(self):
        _, out, _ = self.run_cli("--schedule", "0")
        self.assertEqual(out.count("  - "), 1)


class PauseDispatchTest(PauseCommandCase):
    def test_a_pause_never_starts_the_daemon_or_plays(self):
        with mock.patch("chime.cli.ChimeApp.run_forever") as daemon, \
                mock.patch("chime.cli.ChimeApp.play") as play:
            for argv in (["--pause-today"], ["--pause-until", "2026-10-15"], ["--pause-off"]):
                self.assertEqual(self.run_cli(*argv)[0], 0)
        daemon.assert_not_called()
        play.assert_not_called()

    def test_the_app_for_a_pause_is_a_normal_app_unless_dry_run(self):
        with mock.patch("chime.cli.ChimeApp", wraps=ChimeApp) as built:
            self.run_cli("--pause-today")
            self.assertFalse(built.call_args[1]["dry_run"])
            self.run_cli("--pause-today", "--dry-run")
            self.assertTrue(built.call_args[1]["dry_run"])

    def test_the_record_a_pause_command_writes_is_what_the_daemon_reads(self):
        self.run_cli("--pause-until", "2026-10-15", "--reason", "点検")
        config = load_config(self.config)
        app = ChimeApp(config, backend="mock")
        pause = app.pause.current()
        self.assertEqual((pause.first_day, pause.last_day, pause.reason),
                         (date(2026, 10, 14), date(2026, 10, 15), "点検"))
        event = app._next_pending_event()
        self.assertEqual(event.day, "2026-10-16")


class EnvironmentTest(unittest.TestCase):
    def test_describe_has_expected_keys(self):
        info = env.describe()
        for key in ("system", "release", "machine", "python", "wsl", "production"):
            self.assertIn(key, info)

    def test_non_linux_is_not_production(self):
        with mock.patch("chime.env.platform.uname") as uname:
            uname.return_value = mock.Mock(system="Darwin", release="23.0.0")
            self.assertFalse(env.is_production_linux())

    def test_wsl_is_detected_by_release(self):
        with mock.patch("chime.env.platform.uname") as uname:
            uname.return_value = mock.Mock(system="Linux", release="5.15.0-microsoft-standard-WSL2")
            self.assertTrue(env.is_wsl())
            self.assertFalse(env.is_production_linux())

    def test_wsl_is_detected_by_environment_variable(self):
        with mock.patch("chime.env.platform.uname") as uname, \
                mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
            uname.return_value = mock.Mock(system="Linux", release="6.1.0")
            self.assertTrue(env.is_wsl())

    def test_plain_linux_is_production(self):
        with mock.patch("chime.env.platform.uname") as uname, \
                mock.patch.dict(os.environ, {}, clear=True):
            uname.return_value = mock.Mock(system="Linux", release="6.6.20-v8+")
            self.assertTrue(env.is_production_linux())

    def test_has_command(self):
        self.assertTrue(env.has_command("python3"))
        self.assertFalse(env.has_command("definitely-not-installed-xyz"))
        self.assertFalse(env.has_command(""))


if __name__ == "__main__":
    unittest.main()
