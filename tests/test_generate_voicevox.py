"""作り置き音声の生成スクリプト（``scripts/generate_voicevox.py``）のテスト。

VOICEVOX ENGINE は使わない（``VoicevoxEngine`` をモックに差し替え、ネットワークには
出ない）。``--config`` の扱い、``--prune`` の実行、合成に失敗した文言を manifest に
書かないことを確かめる。文言の列挙そのもの（``chime/phrases.py``）のテストは
``tests/test_phrases.py`` にある。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests.support import KYOTO_ONLY, MANIFEST_PATH, REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import generate_voicevox  # noqa: E402

from chime import phrases, weather  # noqa: E402
from chime.config import DEFAULT_CONFIG, Config, deep_merge  # noqa: E402
from chime.quotes import load_quotes  # noqa: E402
from chime.tts import TTSError, prerecorded_filename  # noqa: E402


class ReexportTest(unittest.TestCase):
    """文言の列挙は ``chime.phrases`` にあり、スクリプトは同じ関数を再公開する。

    CI の ``prerecorded-only`` ジョブや従来のテストは、
    ``from generate_voicevox import collect_phrases`` で引く。
    """

    def test_collect_phrases_is_the_one_in_chime_phrases(self):
        self.assertIs(generate_voicevox.collect_phrases, phrases.collect_phrases)

    def test_every_re_exported_name_is_the_one_in_chime_phrases(self):
        for name in ("collect_phrases", "find_stale_entries", "phrases_in_use",
                     "phrases_to_generate", "phrases_to_keep"):
            self.assertIn(name, generate_voicevox.__all__)
            self.assertIs(getattr(generate_voicevox, name), getattr(phrases, name), name)

    def test_the_public_names_of_the_script_are_listed(self):
        self.assertIn("main", generate_voicevox.__all__)
        self.assertIn("wait_for_engine", generate_voicevox.__all__)
        for name in generate_voicevox.__all__:
            self.assertTrue(hasattr(generate_voicevox, name), name)


class FakeClock:
    """``time`` の差し替え。``sleep`` した分だけ ``monotonic`` が進む（実際には待たない）。"""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class WaitForEngineTest(unittest.TestCase):
    """``wait_for_engine``: 起動待ちの再試行と、待たない場合の 1 回判定。"""

    def setUp(self):
        self.engine = mock.Mock()
        self.clock = FakeClock()
        patcher = mock.patch.object(generate_voicevox, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait(self, wait_seconds):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = generate_voicevox.wait_for_engine(self.engine, wait_seconds)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_an_engine_that_is_already_up_is_checked_once_without_output(self):
        self.engine.available.return_value = True
        result, stdout, stderr = self.wait(90)
        self.assertTrue(result)
        self.assertEqual(self.engine.available.call_count, 1)
        self.assertEqual((stdout, stderr), ("", ""))
        self.assertEqual(self.clock.slept, [])

    def test_no_waiting_means_a_single_check(self):
        self.engine.available.return_value = False
        for wait_seconds in (0, -5):
            with self.subTest(wait=wait_seconds):
                self.engine.available.reset_mock()
                result, stdout, stderr = self.wait(wait_seconds)
                self.assertFalse(result)
                self.assertEqual(self.engine.available.call_count, 1)
                self.assertEqual((stdout, stderr), ("", ""))
                self.assertEqual(self.clock.slept, [])

    def test_retries_every_few_seconds_until_the_engine_is_up(self):
        self.engine.available.side_effect = [False, False, True]
        result, stdout, stderr = self.wait(90)
        self.assertTrue(result)
        self.assertEqual(self.engine.available.call_count, 3)
        self.assertEqual(self.clock.slept, [3.0, 3.0])
        self.assertEqual(stdout, "")
        # 待っている間は、残り時間を stderr に出す（stdout は汚さない）
        self.assertEqual(stderr.count("起動を待っています"), 2)
        self.assertEqual(stderr.splitlines(), [
            "VOICEVOX の起動を待っています…（残り 90 秒）",
            "VOICEVOX の起動を待っています…（残り 87 秒）",
        ])

    def test_gives_up_when_the_wait_is_over(self):
        self.engine.available.return_value = False
        result, stdout, stderr = self.wait(10)
        self.assertFalse(result)
        # 最初の 1 回 + 待つたびに 1 回。最後は残りの 1 秒だけ眠って打ち切る
        self.assertEqual(self.engine.available.call_count, 5)
        self.assertEqual(self.clock.slept, [3.0, 3.0, 3.0, 1.0])
        self.assertEqual(sum(self.clock.slept), 10)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr.count("起動を待っています"), 4)


class MainTest(unittest.TestCase):
    """``main(argv)`` を、VOICEVOX ENGINE をモックに差し替えて実行する。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.out = os.path.join(self.tmp, "voice")

        patcher = mock.patch.object(generate_voicevox, "VoicevoxEngine")
        self.engine_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = self.engine_class.return_value
        self.engine.available.return_value = True
        self.engine.synthesize.side_effect = self.fake_synthesize
        self.synthesized = []
        self.failing = set()

        self.default = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        local = Config(deep_merge(DEFAULT_CONFIG, KYOTO_ONLY), base_dir=REPO_ROOT)
        # 地点だけが違う天気の文（京都は現地の config.json で足した文言の代表）
        self.otsu, self.otsu_other = [
            phrase for phrase in weather.prerecord_phrases(self.default.section("weather"))
            if "大津" in phrase][:2]
        self.kyoto = [phrase for phrase in weather.prerecord_phrases(local.section("weather"))
                      if "京都" in phrase][0]

    def fake_synthesize(self, text, out_path):
        if text in self.failing:
            raise TTSError("テスト用の失敗")
        self.synthesized.append(text)
        with open(out_path, "wb") as handle:
            handle.write(b"RIFF")

    def write_config(self, data, name="pi-config.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        return path

    def run_main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = generate_voicevox.main(["--out", self.out, "--wait", "0"] + list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def read_manifest(self):
        with open(os.path.join(self.out, "manifest.json"), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def seed(self, phrases):
        """``phrases`` の作り置き（WAV と manifest）を出力先に置いておく。"""
        os.makedirs(self.out, exist_ok=True)
        manifest = {}
        for phrase in phrases:
            filename = prerecorded_filename(phrase)
            with open(os.path.join(self.out, filename), "wb") as handle:
                handle.write(b"RIFF")
            manifest[phrase] = filename
        with open(os.path.join(self.out, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False)
        return manifest

    def phrase_file(self, phrase):
        return os.path.join(self.out, prerecorded_filename(phrase))

    # -- --config ------------------------------------------------------------
    def test_config_phrases_are_generated_in_addition_to_the_defaults(self):
        config_path = self.write_config(KYOTO_ONLY)
        code, _, _ = self.run_main("--config", config_path)
        self.assertEqual(code, 0)

        local = Config(deep_merge(DEFAULT_CONFIG, KYOTO_ONLY), base_dir=REPO_ROOT)
        expected = phrases.phrases_to_generate(local, include_quotes=False)
        self.assertEqual(sorted(self.synthesized), sorted(expected))
        self.assertIn(self.kyoto, self.synthesized)
        self.assertIn(self.otsu, self.synthesized)
        self.assertEqual(set(self.read_manifest()), set(expected))

    def test_config_decides_the_defaults_of_the_other_options(self):
        out = os.path.join(self.tmp, "from-config")
        config_path = self.write_config({
            "tts": {"prerecorded_dir": out,
                    "voicevox": {"base_url": "http://192.0.2.1:50021", "speaker": 8}},
        })
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = generate_voicevox.main(["--wait", "0", "--config", config_path])
        self.assertEqual(code, 0)
        settings = self.engine_class.call_args[0][0]
        self.assertEqual(settings["base_url"], "http://192.0.2.1:50021")
        self.assertEqual(settings["speaker"], 8)
        self.assertTrue(os.path.exists(os.path.join(out, "manifest.json")))

    def test_command_line_options_win_over_the_config(self):
        config_path = self.write_config(
            {"tts": {"voicevox": {"base_url": "http://192.0.2.1:50021", "speaker": 8}}})
        code, _, _ = self.run_main("--config", config_path,
                                   "--base-url", "http://192.0.2.2:50021", "--speaker", "1")
        self.assertEqual(code, 0)
        settings = self.engine_class.call_args[0][0]
        self.assertEqual(settings["base_url"], "http://192.0.2.2:50021")
        self.assertEqual(settings["speaker"], 1)

    def test_a_missing_config_is_a_config_error(self):
        code, _, stderr = self.run_main("--config", os.path.join(self.tmp, "nothing.json"))
        self.assertEqual(code, 2)
        self.assertIn("設定エラー", stderr)
        self.assertEqual(self.synthesized, [])

    # -- --prune -------------------------------------------------------------
    def test_prune_keeps_both_the_defaults_and_the_local_phrases(self):
        local = Config(deep_merge(DEFAULT_CONFIG, KYOTO_ONLY), base_dir=REPO_ROOT)
        kyoto, otsu = self.kyoto, self.otsu
        quote = load_quotes(self.default.path("quotes.file"))["general"][0]
        unused = "どこにも使われていない文言なのだ。"
        self.assertIn(kyoto, phrases.phrases_in_use(local))
        self.assertNotIn(otsu, phrases.phrases_in_use(local))
        self.seed([kyoto, otsu, quote, unused])

        config_path = self.write_config(KYOTO_ONLY)
        code, _, stderr = self.run_main("--config", config_path, "--prune")
        self.assertEqual(code, 0)

        manifest = self.read_manifest()
        # 古い文言だけが manifest と WAV から消える
        self.assertNotIn(unused, manifest)
        self.assertFalse(os.path.exists(self.phrase_file(unused)))
        # 現地の文言・既定の文言・ひとこと（--include-quotes なしでも）は残る
        for phrase in (kyoto, otsu, quote):
            self.assertIn(phrase, manifest)
            self.assertTrue(os.path.exists(self.phrase_file(phrase)))
        # 残した文言は消してから作り直したのではなく、そのまま使われている
        self.assertNotIn(kyoto, self.synthesized)
        self.assertNotIn(otsu, self.synthesized)
        self.assertNotIn("Pi の config.json", stderr)

    def test_prune_without_config_warns_that_local_phrases_will_go(self):
        kyoto = self.kyoto
        self.seed([kyoto])
        code, _, stderr = self.run_main("--prune")
        self.assertEqual(code, 0)
        self.assertIn("Pi の config.json で足した文言（地点など）は、--config を付けないと消えます",
                      stderr)
        # 警告を出しても処理は続く
        self.assertNotIn(kyoto, self.read_manifest())
        self.assertFalse(os.path.exists(self.phrase_file(kyoto)))

    def test_no_warning_without_prune(self):
        _, _, stderr = self.run_main()
        self.assertNotIn("--config を付けないと消えます", stderr)

    # -- 合成の失敗 ----------------------------------------------------------
    def test_a_phrase_that_failed_to_synthesize_is_not_written_to_the_manifest(self):
        failed = self.otsu
        self.failing.add(failed)
        code, _, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("1 件が失敗しました", stderr)

        manifest = self.read_manifest()
        self.assertNotIn(failed, manifest)
        self.assertFalse(os.path.exists(self.phrase_file(failed)))
        # 成功した文言は書かれる
        self.assertIn(self.otsu_other, manifest)
        self.assertEqual(len(manifest) + 1,
                         len(phrases.collect_phrases(self.default, False)))

    def test_a_phrase_that_already_has_a_wav_is_registered_without_synthesizing(self):
        phrase = self.otsu
        self.seed([])
        with open(self.phrase_file(phrase), "wb") as handle:
            handle.write(b"RIFF")
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn(phrase, self.synthesized)
        self.assertIn(phrase, self.read_manifest())

    # -- 生成する文言の範囲 --------------------------------------------------
    def test_quotes_are_synthesized_only_with_include_quotes(self):
        quote = load_quotes(self.default.path("quotes.file"))["general"][0]
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn(quote, self.synthesized)
        self.assertNotIn(quote, self.read_manifest())
        # 天気の文は、指定がなくても常に作る
        self.assertIn(self.otsu, self.synthesized)

        self.synthesized.clear()
        code, _, _ = self.run_main("--include-quotes")
        self.assertEqual(code, 0)
        self.assertIn(quote, self.synthesized)
        self.assertIn(quote, self.read_manifest())

    def test_force_synthesizes_a_phrase_that_already_has_a_wav(self):
        phrase = self.otsu
        self.seed([])
        with open(self.phrase_file(phrase), "wb") as handle:
            handle.write(b"OLD")

        code, stdout, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn(phrase, self.synthesized)
        self.assertIn("  skip {0}\n".format(phrase), stdout)
        with open(self.phrase_file(phrase), "rb") as handle:
            self.assertEqual(handle.read(), b"OLD")

        code, stdout, _ = self.run_main("--force")
        self.assertEqual(code, 0)
        self.assertIn(phrase, self.synthesized)
        self.assertIn("  OK   {0} -> {1}\n".format(phrase, prerecorded_filename(phrase)), stdout)
        self.assertNotIn("  skip", stdout)
        with open(self.phrase_file(phrase), "rb") as handle:
            self.assertEqual(handle.read(), b"RIFF")
        self.assertEqual(self.read_manifest()[phrase], prerecorded_filename(phrase))

    # -- 古いエントリ（--prune なし） ----------------------------------------
    def test_a_stale_entry_without_prune_is_reported_and_kept(self):
        unused = "どこにも使われていない文言なのだ。"
        self.seed([self.otsu, unused])
        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("1 件の古いエントリが残っています（--prune を付けると削除されます）。\n",
                      stdout)
        self.assertNotIn("古いエントリを削除しました", stdout)
        self.assertNotIn("--config を付けないと消えます", stderr)
        self.assertIn(unused, self.read_manifest())
        self.assertTrue(os.path.exists(self.phrase_file(unused)))

    def test_no_notice_when_nothing_is_stale(self):
        self.seed([self.otsu])
        code, stdout, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn("古いエントリ", stdout)

    # -- ENGINE に繋がらない -------------------------------------------------
    def test_an_unreachable_engine_is_reported_without_touching_anything(self):
        self.engine.available.return_value = False
        url = "http://192.0.2.1:50021"
        code, stdout, stderr = self.run_main("--base-url", url)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        lines = stderr.splitlines()
        self.assertEqual(lines[0], "VOICEVOX ENGINE に接続できません: {0}".format(url))
        self.assertEqual(lines[1], "次を確認してください。")
        self.assertIn("  - 疎通確認: curl -s {0}/version".format(url), lines)
        self.assertTrue(any("--base-url でホストの IP を指定してください" in line
                            for line in lines), stderr)
        # --wait 0 なので「待ちましたが」は出ない
        self.assertNotIn("秒待ちましたが", stderr)
        self.assertEqual(self.synthesized, [])
        self.engine.synthesize.assert_not_called()
        self.assertFalse(os.path.exists(self.out), "接続できないのに出力先を作ってはいけない")

    def test_an_unreachable_engine_after_waiting_says_how_long_it_waited(self):
        self.engine.available.return_value = False
        with mock.patch.object(generate_voicevox, "time", FakeClock()):
            code, stdout, stderr = self.run_main("--wait", "5")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        lines = stderr.splitlines()
        self.assertEqual(lines.count("VOICEVOX の起動を待っています…（残り 5 秒）"), 1)
        # 待ったあとの案内は、接続できない旨 → 待った秒数 → 確認事項の順
        failed = lines.index("VOICEVOX ENGINE に接続できません: {0}".format(
            self.default.section("tts.voicevox")["base_url"]))
        self.assertEqual(lines[failed + 1], "5 秒待ちましたが応答がありませんでした。")
        self.assertEqual(lines[failed + 2], "次を確認してください。")
        self.assertFalse(os.path.exists(self.out))

    # -- manifest の書式 -----------------------------------------------------
    def test_the_manifest_is_written_sorted_and_indented_with_a_trailing_newline(self):
        code, _, _ = self.run_main("--include-quotes")
        self.assertEqual(code, 0)
        manifest_path = os.path.join(self.out, "manifest.json")
        with open(manifest_path, "rb") as handle:
            raw = handle.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8")
        manifest = json.loads(text)
        self.assertEqual(
            text, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        self.assertIn("大津", text, "日本語は \\u エスケープせずにそのまま書く")

    def test_the_shipped_manifest_is_in_the_format_the_script_writes(self):
        # 生成し直したときに、manifest.json の全行が差分にならないための確認。
        with open(MANIFEST_PATH, "rb") as handle:
            text = handle.read().decode("utf-8")
        self.assertEqual(
            text, json.dumps(json.loads(text), ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    # -- 既存の manifest の読み込み -------------------------------------------
    def write_manifest_bytes(self, raw):
        os.makedirs(self.out, exist_ok=True)
        path = os.path.join(self.out, "manifest.json")
        with open(path, "wb") as handle:
            handle.write(raw)
        return path

    def test_a_utf8_bom_manifest_is_accepted(self):
        # メモ帳の「UTF-8（BOM 付き）」で保存し直された manifest も読む。
        unused = "どこにも使われていない文言なのだ。"
        phrase = self.otsu
        self.seed([phrase])
        self.write_manifest_bytes(b"\xef\xbb\xbf" + json.dumps(
            {phrase: prerecorded_filename(phrase), unused: "unused.wav"},
            ensure_ascii=False).encode("utf-8"))

        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        # 空として扱われたのではなく、中身が読めている（古いエントリとして数えられる）
        self.assertIn("1 件の古いエントリが残っています", stdout)
        manifest = self.read_manifest()
        self.assertEqual(manifest[phrase], prerecorded_filename(phrase))
        self.assertIn(unused, manifest)
        with open(os.path.join(self.out, "manifest.json"), "rb") as handle:
            self.assertFalse(handle.read().startswith(b"\xef\xbb\xbf"), "書き出しは BOM なし")

    def test_an_unreadable_manifest_stops_the_run_and_is_left_untouched(self):
        # 壊れた目録を空として扱って書き直すと、既存の登録がすべて消える。
        broken = [
            ("JSON の書き方が正しくない", b"{ not json", "1 行 3 文字目"),
            ("UTF-8 ではない", json.dumps({"あ": "a.wav"}, ensure_ascii=False).encode("shift_jis"),
             "UTF-8"),
        ]
        for label, raw, expected in broken:
            with self.subTest(label):
                self.synthesized.clear()
                self.engine.synthesize.reset_mock()
                path = self.write_manifest_bytes(raw)
                before = sorted(os.listdir(self.out))

                code, stdout, stderr = self.run_main("--prune", "--include-quotes")
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith(path), stderr)
                self.assertIn(expected, stderr)
                self.assertEqual(stderr.count("\n"), 1, "案内は 1 行だけ（トレースバックは出さない）")
                self.assertEqual(self.synthesized, [])
                self.engine.synthesize.assert_not_called()
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), raw)
                self.assertEqual(sorted(os.listdir(self.out)), before)

    def test_a_failure_while_writing_the_manifest_keeps_the_previous_one(self):
        phrase = self.otsu
        self.seed([phrase])
        path = os.path.join(self.out, "manifest.json")
        with open(path, "rb") as handle:
            previous = handle.read()
        with mock.patch("chime.jsonfile.os.replace", side_effect=OSError("置き換えられない")):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(OSError):
                    generate_voicevox.main(["--out", self.out, "--wait", "0"])
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), previous)
        self.assertEqual([name for name in os.listdir(self.out) if name.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
