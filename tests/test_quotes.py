"""「ひとこと」選択のテスト。"""

from __future__ import annotations

import json
import os
import random
import tempfile
import unittest

from tests.support import REPO_ROOT

from chime.quotes import FALLBACK_QUOTES, QuoteError, QuotePicker, load_quotes

SHIPPED_QUOTES = os.path.join(REPO_ROOT, "assets", "quotes.json")


def write_quotes(directory: str, data) -> str:
    path = os.path.join(directory, "quotes.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    return path


class LoadQuotesTest(unittest.TestCase):
    def test_missing_file_falls_back(self):
        data = load_quotes("/nonexistent/quotes.json")
        self.assertEqual(data["general"], FALLBACK_QUOTES)

    def test_broken_json_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quotes.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{{{")
            self.assertEqual(load_quotes(path)["general"], FALLBACK_QUOTES)

    def test_plain_list_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_quotes(tmp, ["ひとつめ", "ふたつめ"])
            self.assertEqual(load_quotes(path)["general"], ["ひとつめ", "ふたつめ"])

    def test_by_hour_keys_are_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_quotes(tmp, {"general": ["共通"], "by_hour": {12: ["お昼"]}})
            self.assertEqual(load_quotes(path)["by_hour"]["12"], ["お昼"])

    def test_non_list_by_hour_entry_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_quotes(tmp, {
                "general": ["共通"],
                "by_hour": {"10": {"a": 1, "b": 2}, "11": "文字列も配列ではない"},
            })
            data = load_quotes(path)
            self.assertNotIn("10", data["by_hour"])
            self.assertNotIn("11", data["by_hour"])

    def test_non_list_general_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_quotes(tmp, {"general": {"a": 1}, "by_hour": {}})
            data = load_quotes(path)
            self.assertEqual(data["general"], FALLBACK_QUOTES)


class PickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_quotes(self.tmp.name, {
            "general": ["A", "B", "C"],
            "by_hour": {"12": ["ランチ"]},
        })
        self.picker = QuotePicker(self.path, avoid_recent=2, rng=random.Random(0))

    def tearDown(self):
        self.tmp.cleanup()

    def test_hour_specific_quotes_are_added(self):
        self.assertEqual(sorted(self.picker.candidates(12)), ["A", "B", "C", "ランチ"])
        self.assertEqual(sorted(self.picker.candidates(10)), ["A", "B", "C"])

    def test_pick_returns_a_candidate(self):
        self.assertIn(self.picker.pick(10), ["A", "B", "C"])

    def test_recent_quotes_are_avoided(self):
        for _ in range(20):
            self.assertEqual(self.picker.pick(10, recent=["A", "B"]), "C")

    def test_avoid_recent_window_is_limited(self):
        # avoid_recent=2 なので、直近 2 件だけが除外対象
        for _ in range(20):
            self.assertEqual(self.picker.pick(10, recent=["C", "A", "B"]), "C")

    def test_falls_back_when_everything_is_recent(self):
        picker = QuotePicker(self.path, avoid_recent=10, rng=random.Random(0))
        self.assertIn(picker.pick(10, recent=["A", "B", "C"]), ["A", "B", "C"])

    def test_duplicates_are_removed(self):
        path = write_quotes(self.tmp.name, {"general": ["A", "A", "B"], "by_hour": {}})
        picker = QuotePicker(path)
        self.assertEqual(picker.candidates(), ["A", "B"])

    def test_empty_definition_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_quotes(tmp, {"general": [], "by_hour": {"9": ["朝"]}})
            picker = QuotePicker(path)
            with self.assertRaises(QuoteError):
                picker.pick(10)
            self.assertEqual(picker.pick(9), "朝")

    def test_reload_picks_up_changes(self):
        write_quotes(self.tmp.name, {"general": ["新しい"], "by_hour": {}})
        self.picker.reload()
        self.assertEqual(self.picker.pick(10), "新しい")


class MisreadWordsTest(unittest.TestCase):
    """特定の誤読を招く表記が紛れ込んでいないことを確認する（回帰防止）。

    実際の読み（カタカナ）は Open JTalk の ``-ot`` トレースで別途確認済み。
    かな書きへ置き換えれば正しく読めることを確認している。
    """

    def setUp(self):
        self.quotes = load_quotes(SHIPPED_QUOTES)["general"]

    def test_bunbetsu_is_not_written_with_kanji(self):
        # 「分別」を漢字で書くと「思慮分別」の意味のフンベツと誤読される。
        # 正しくは「ぶんべつ」（ゴミの分別）。
        for quote in self.quotes:
            self.assertNotIn("分別", quote)

    def test_ippo_is_not_written_with_kanji(self):
        # 「一歩」は 一(イチ) + 歩(ホ) に分割され、イチホと誤読される。
        # 正しくは「いっぽ」。
        for quote in self.quotes:
            self.assertNotIn("一歩", quote)


class ShippedQuotesTest(unittest.TestCase):
    """同梱の ``assets/quotes.json`` の健全性。"""

    def setUp(self):
        self.data = load_quotes(SHIPPED_QUOTES)

    def test_has_enough_general_quotes(self):
        self.assertGreaterEqual(len(self.data["general"]), 20)

    def test_every_scheduled_hour_has_entries(self):
        for hour in range(10, 17):
            self.assertIn(str(hour), self.data["by_hour"])

    def test_no_duplicates(self):
        quotes = self.data["general"]
        self.assertEqual(len(quotes), len(set(quotes)))

    def test_quotes_are_not_empty(self):
        for quote in self.data["general"]:
            self.assertTrue(quote.strip())


if __name__ == "__main__":
    unittest.main()
