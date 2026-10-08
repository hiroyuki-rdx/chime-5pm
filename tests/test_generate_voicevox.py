"""作り置き音声の生成スクリプト（``scripts/generate_voicevox.py``）のテスト。

VOICEVOX ENGINE は使わない（``VoicevoxEngine`` をモックに差し替え、ネットワークには
出ない）。文言の集め方、``--config`` の扱い、``--prune`` の判定、合成に失敗した
文言を manifest に書かないことを確かめる。
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

from tests.support import REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import generate_voicevox  # noqa: E402

from chime import weather  # noqa: E402
from chime.config import DEFAULT_CONFIG, Config, deep_merge  # noqa: E402
from chime.quotes import load_quotes  # noqa: E402
from chime.tts import TTSError, _digest  # noqa: E402

MANIFEST_PATH = os.path.join(REPO_ROOT, "assets", "voice", "manifest.json")

#: 現地（Pi）の config.json で地点を京都に差し替えた、という想定の設定。
#: 配列は丸ごと置き換わるので、既定の大津は外れる。
KYOTO_ONLY = {
    "weather": {
        "open_meteo": {
            "locations": [{"label": "京都", "latitude": 35.0116, "longitude": 135.7681}],
        },
    },
}


def load_manifest() -> dict:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


class PruneTest(unittest.TestCase):
    def setUp(self):
        # ローカルの config.json を読まないよう、既定値から直接作る
        self.config = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        self.manifest = load_manifest()

    def test_every_default_phrase_is_in_the_shipped_manifest(self):
        """既定設定の全文言（ひとことを含む）が、同梱の manifest にあること。

        現地で足した文言（地点など）の作り置きを同梱してもよいので、
        manifest の側に余りが無いことまでは求めない。
        """
        self.assertTrue(self.manifest)
        missing = [phrase for phrase in generate_voicevox.phrases_in_use(self.config)
                   if phrase not in self.manifest]
        self.assertEqual(missing, [])

    def test_quotes_are_in_use_even_when_not_regenerated(self):
        quotes = load_quotes(self.config.path("quotes.file"))["general"]
        self.assertTrue(quotes)
        without_quotes = generate_voicevox.collect_phrases(self.config, include_quotes=False)
        in_use = generate_voicevox.phrases_in_use(self.config)
        for quote in quotes:
            self.assertNotIn(quote, without_quotes)
            self.assertIn(quote, in_use)

    def test_only_an_unused_phrase_is_stale(self):
        manifest = dict(self.manifest)
        manifest["どこにも使われていない文言なのだ。"] = "unused.wav"
        stale = generate_voicevox.find_stale_entries(
            manifest, generate_voicevox.phrases_in_use(self.config))
        self.assertEqual(stale, [("どこにも使われていない文言なのだ。", "unused.wav")])


class PhraseUnionTest(unittest.TestCase):
    """``--config`` の設定の文言は、既定設定の文言に足される（和集合）。"""

    def setUp(self):
        self.default = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        self.local = Config(deep_merge(DEFAULT_CONFIG, KYOTO_ONLY), base_dir=REPO_ROOT)
        self.kyoto = [phrase for phrase in weather.prerecord_phrases(self.local.section("weather"))
                      if "京都" in phrase]
        self.otsu = [phrase for phrase in weather.prerecord_phrases(self.default.section("weather"))
                     if "大津" in phrase]

    def test_the_fixture_replaces_the_default_location(self):
        # 前提の確認: 配列が丸ごと置き換わり、指定設定だけでは大津が外れる
        self.assertTrue(self.kyoto)
        self.assertTrue(self.otsu)
        local_only = generate_voicevox.collect_phrases(self.local, include_quotes=False)
        for phrase in self.otsu:
            self.assertNotIn(phrase, local_only)

    def test_phrases_to_generate_adds_the_defaults_to_the_config(self):
        phrases = generate_voicevox.phrases_to_generate(self.local, include_quotes=False)
        local_only = generate_voicevox.collect_phrases(self.local, include_quotes=False)
        default_only = generate_voicevox.collect_phrases(self.default, include_quotes=False)
        self.assertEqual(set(phrases), set(local_only) | set(default_only))
        for phrase in self.kyoto + self.otsu:
            self.assertIn(phrase, phrases)

    def test_phrases_to_generate_has_no_duplicates_and_a_stable_order(self):
        phrases = generate_voicevox.phrases_to_generate(self.local, include_quotes=True)
        self.assertEqual(len(phrases), len(set(phrases)))
        # 指定設定の文言が先頭から順に並び、既定設定にだけある文言があとに続く
        local_only = generate_voicevox.collect_phrases(self.local, include_quotes=True)
        self.assertEqual(phrases[:len(local_only)], local_only)
        self.assertEqual(
            phrases, generate_voicevox.phrases_to_generate(self.local, include_quotes=True))

    def test_the_defaults_alone_are_unchanged(self):
        self.assertEqual(
            generate_voicevox.phrases_to_generate(self.default, include_quotes=True),
            generate_voicevox.collect_phrases(self.default, include_quotes=True))

    def test_phrases_to_keep_covers_the_config_and_the_defaults_including_quotes(self):
        keep = generate_voicevox.phrases_to_keep(self.local)
        for phrase in self.kyoto + self.otsu:
            self.assertIn(phrase, keep)
        for quote in load_quotes(self.default.path("quotes.file"))["general"]:
            self.assertIn(quote, keep)
        self.assertEqual(len(keep), len(set(keep)))


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
            filename = "{0}.wav".format(_digest(phrase))
            with open(os.path.join(self.out, filename), "wb") as handle:
                handle.write(b"RIFF")
            manifest[phrase] = filename
        with open(os.path.join(self.out, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False)
        return manifest

    def phrase_file(self, phrase):
        return os.path.join(self.out, "{0}.wav".format(_digest(phrase)))

    # -- --config ------------------------------------------------------------
    def test_config_phrases_are_generated_in_addition_to_the_defaults(self):
        config_path = self.write_config(KYOTO_ONLY)
        code, _, _ = self.run_main("--config", config_path)
        self.assertEqual(code, 0)

        local = Config(deep_merge(DEFAULT_CONFIG, KYOTO_ONLY), base_dir=REPO_ROOT)
        expected = generate_voicevox.phrases_to_generate(local, include_quotes=False)
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
        self.assertIn(kyoto, generate_voicevox.phrases_in_use(local))
        self.assertNotIn(otsu, generate_voicevox.phrases_in_use(local))
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
                         len(generate_voicevox.collect_phrases(self.default, False)))

    def test_a_phrase_that_already_has_a_wav_is_registered_without_synthesizing(self):
        phrase = self.otsu
        self.seed([])
        with open(self.phrase_file(phrase), "wb") as handle:
            handle.write(b"RIFF")
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn(phrase, self.synthesized)
        self.assertIn(phrase, self.read_manifest())


if __name__ == "__main__":
    unittest.main()
