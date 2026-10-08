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

from tests.support import REPO_ROOT  # noqa: F401

from chime import env
from chime.audio import Segment
from chime.cli import EPILOG, build_parser, run
from chime.sequence import PlaybackPlan
from chime.tts import TTSError
from chime.weather import WeatherError


def call(argv):
    """CLI を実行し、(終了コード, 出力) を返す。

    ``print`` 出力とログ出力の両方を 1 つのバッファへ集める
    （``tests/support.py`` がログを抑制しているため、ここだけ一時的に戻す）。
    """
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.INFO)
    logging.disable(logging.NOTSET)
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            code = run(argv)
    finally:
        logging.disable(logging.CRITICAL)
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
    logging.disable(logging.NOTSET)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = run(argv)
    finally:
        logging.disable(logging.CRITICAL)
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

    def test_test_hourly_with_value(self):
        self.assertEqual(self.parser.parse_args(["--test-hourly", "14"]).test_hourly, 14)

    def test_schedule_default_count(self):
        self.assertEqual(self.parser.parse_args(["--schedule"]).schedule, 10)

    def test_backend_choices_are_enforced(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(["--backend", "gramophone"])


class RunTest(unittest.TestCase):
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
        wav = os.path.join(REPO_ROOT, "assets", "announce.wav")
        with mock.patch("chime.tts.TTSService.synthesize", return_value=wav):
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
        fixture = os.path.join(REPO_ROOT, "tests", "fixtures", "open_meteo.json")
        with open(fixture, encoding="utf-8") as handle:
            payload = json.load(handle)
        with mock.patch("chime.weather.fetch_json", return_value=payload):
            code, output = call(["--weather", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("の天気は", output)
        self.assertIn("なのだ。", output)

    def test_weather_prints_one_line_per_sentence(self):
        """読み上げは 1 文ずつ別のセグメントとして鳴らすため、確認用の出力も
        同じ単位にする。連結した文字列では作り置き音声との照合が外れる。"""
        fixture = os.path.join(REPO_ROOT, "tests", "fixtures", "open_meteo.json")
        with open(fixture, encoding="utf-8") as handle:
            payload = json.load(handle)
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

        既定（mode="both"）では 12 時に天気予報が流れる。その取得に失敗しても
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
        wav = os.path.join(REPO_ROOT, "assets", "announce.wav")
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
        wav = os.path.join(REPO_ROOT, "assets", "announce.wav")

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
        """設定を読み込むときの警告（廃止予定のキーなど）にも、時刻とレベルが付くこと。

        ログ設定は設定を読んだあとに確定するため、読み込み中の警告は
        仮の設定（既定値）で出す。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"extra_segment": {"mode": "both"}}, handle)
            code, stdout, stderr = call_split(["--config", path, "--schedule", "1"])
        self.assertEqual(code, 0)
        lines = [line for line in stdout.splitlines() if "廃止予定" in line]
        self.assertEqual(len(lines), 1, stdout)
        self.assertRegex(lines[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.* - WARNING - ")

    def test_print_config_keeps_stdout_pure_json_even_with_warnings(self):
        """``--print-config`` の出力をファイルへ保存しても JSON のままであること
        （設定を読むときの警告は標準エラー出力へ出す）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"extra_segment": {"mode": "both"}}, handle)
            code, stdout, stderr = call_split(["--config", path, "--print-config"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["extra_segment"]["mode"], "both")
        self.assertIn("廃止予定", stderr)

    def test_log_level_option_applies_to_config_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"extra_segment": {"mode": "both"}}, handle)
            code, stdout, stderr = call_split(["--config", path, "--schedule", "1",
                                               "--log-level", "ERROR"])
        self.assertEqual(code, 0)
        self.assertNotIn("廃止予定", stdout + stderr)

    # -- --say の案内 ----------------------------------------------------
    def test_epilog_say_example_is_prerecorded(self):
        """ヘルプの ``--say`` の例は、そのまま試して鳴らせる文言（作り置きにある文）であること。"""
        example = re.search(r"--say (\S+)", EPILOG).group(1)
        manifest = os.path.join(REPO_ROOT, "assets", "voice", "manifest.json")
        with open(manifest, encoding="utf-8") as handle:
            phrases = json.load(handle)
        self.assertEqual(example, "正午をお知らせしたのだ。")
        self.assertIn(example, phrases)

    def test_say_prerecorded_phrase_has_no_warning(self):
        code, output = call(["--say", "正午をお知らせしたのだ。", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertNotIn("作り置き（assets/voice/）にこの文言がありません", output)

    def test_say_unknown_phrase_shows_candidates_and_guidance(self):
        """作り置きに無い文言は、Pi では無音になることと、近い文言・作り直しの案内を出す。
        そのうえで従来どおり再生を試みる（PC で VOICEVOX が動いていれば鳴る）。"""
        wav = os.path.join(REPO_ROOT, "assets", "announce.wav")
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
        wav = os.path.join(REPO_ROOT, "assets", "announce.wav")
        with mock.patch("chime.tts.TTSService.synthesize", return_value=wav):
            code, stdout, stderr = call_split(["--say", "Qwerty", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("作り置き（assets/voice/）にこの文言がありません", stderr)
        self.assertNotIn("近い文言", stderr)
        self.assertIn("docs/SETUP.md 8 章", stderr)

    def test_say_reports_missing_voice_folder_instead_of_missing_phrase(self):
        # assets/voice/ が丸ごと無いときは「この文言がありません」ではなく、
        # 作り置きそのものが見つからないことを案内する。
        wav = os.path.join(REPO_ROOT, "assets", "announce.wav")
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
        offline = os.path.join(REPO_ROOT, "tests", "fixtures", "offline_config.json")
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
