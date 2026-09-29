"""作り置き音声の生成スクリプト（``scripts/generate_voicevox.py``）のテスト。

VOICEVOX ENGINE は使わない。文言の集め方と、``--prune`` の判定だけを確かめる。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

from tests.support import REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import generate_voicevox  # noqa: E402

from chime.config import DEFAULT_CONFIG, Config  # noqa: E402
from chime.quotes import load_quotes  # noqa: E402

MANIFEST_PATH = os.path.join(REPO_ROOT, "assets", "voice", "manifest.json")


def load_manifest() -> dict:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


class PruneTest(unittest.TestCase):
    def setUp(self):
        # ローカルの config.json を読まないよう、既定値から直接作る
        self.config = Config(DEFAULT_CONFIG, base_dir=REPO_ROOT)
        self.manifest = load_manifest()

    def test_nothing_is_stale_with_the_shipped_voices(self):
        """同梱の作り置きは、既定設定の全文言（ひとことを含む）に対応していること。

        ``--include-quotes`` を付けずに ``--prune`` を実行してもひとことの
        音声が消えない（削除対象が 0 件）ことの回帰確認でもある。
        """
        self.assertTrue(self.manifest)
        stale = generate_voicevox.find_stale_entries(
            self.manifest, generate_voicevox.phrases_in_use(self.config))
        self.assertEqual(stale, [])

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


if __name__ == "__main__":
    unittest.main()
