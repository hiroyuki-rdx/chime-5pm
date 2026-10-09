"""「ひとこと」選択のテスト。"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import unittest

from tests.support import SHIPPED_QUOTES, load_manifest, logs_enabled, write_quotes

from chime.quotes import FALLBACK_QUOTES, QuoteError, QuotePicker, _normalize_quotes, load_quotes


class _Records(logging.Handler):
    """ログを集めるだけのハンドラ（``assertLogs`` は 0 件だと失敗するため、無音の確認に使う）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record) -> None:
        self.records.append(record)


def normalize_capturing_logs(data, path="quotes.json"):
    """``_normalize_quotes`` を呼び、結果と ``[(レベル, メッセージ)]`` を返す。

    tests/__init__.py がログを抑制しているため、この間だけ一時的に解除する。
    """
    target = logging.getLogger("chime.quotes")
    handler = _Records()
    old_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        with logs_enabled():
            result = _normalize_quotes(data, path)
    finally:
        target.setLevel(old_level)
        target.removeHandler(handler)
    return result, [(record.levelno, record.getMessage()) for record in handler.records]


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

    def test_shift_jis_file_falls_back(self):
        # メモ帳の既定（ANSI＝Shift_JIS）で保存し直された quotes.json。
        # 放送は止めず、内蔵の予備に切り替わること。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quotes.json")
            with open(path, "wb") as handle:
                handle.write(json.dumps({"general": ["おつかれさま"]},
                                        ensure_ascii=False).encode("shift_jis"))
            self.assertEqual(load_quotes(path)["general"], FALLBACK_QUOTES)

    def test_utf8_bom_file_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quotes.json")
            with open(path, "wb") as handle:
                handle.write(b"\xef\xbb\xbf" + json.dumps(
                    {"general": ["おつかれさま"]}, ensure_ascii=False).encode("utf-8"))
            self.assertEqual(load_quotes(path)["general"], ["おつかれさま"])

    def _load_capturing_logs(self, path, level):
        """``load_quotes`` を呼び、``level`` 以上のログを集めて返す。

        tests/__init__.py がログを抑制しているため、tests/support.py の
        ``logs_enabled()`` でこの間だけ一時的に解除する。
        """
        with logs_enabled(), self.assertLogs("chime.quotes", level=level) as captured:
            data = load_quotes(path)
        return data, captured.records

    def test_shift_jis_file_is_an_error_with_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quotes.json")
            with open(path, "wb") as handle:
                handle.write(json.dumps({"general": ["おつかれさま"]},
                                        ensure_ascii=False).encode("shift_jis"))
            data, records = self._load_capturing_logs(path, "ERROR")
        self.assertEqual(data["general"], FALLBACK_QUOTES)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].levelno, logging.ERROR)
        message = records[0].getMessage()
        self.assertIn(path, message)
        self.assertIn("UTF-8", message)
        self.assertIn("内蔵の予備", message)

    def test_broken_json_is_an_error_with_a_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quotes.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{\n  "general": [\n    "A",\n  ]\n}')
            data, records = self._load_capturing_logs(path, "ERROR")
        self.assertEqual(data["general"], FALLBACK_QUOTES)
        message = records[0].getMessage()
        self.assertIn(path, message)
        self.assertIn("最後の項目のあとにカンマは付けられません", message)

    def test_missing_file_stays_a_warning(self):
        data, records = self._load_capturing_logs("/nonexistent/quotes.json", "WARNING")
        self.assertEqual(data["general"], FALLBACK_QUOTES)
        self.assertEqual([record.levelno for record in records], [logging.WARNING])
        self.assertIn("内蔵の予備", records[0].getMessage())

    def test_picker_keeps_working_on_a_shift_jis_file(self):
        # 放送は止めない。予備のひとことから選べる。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "quotes.json")
            with open(path, "wb") as handle:
                handle.write("{\"general\": [\"おつかれさま\"]}".encode("shift_jis"))
            picker = QuotePicker(path, rng=random.Random(0))
            self.assertIn(picker.pick(10), FALLBACK_QUOTES)

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

    def test_every_fallback_is_a_fresh_copy(self):
        # 予備を使う 3 つの場合（ファイルなし・形式不正・何も定義なし）のどれでも、
        # 呼び出し側がリストを書き換えても FALLBACK_QUOTES や次の読み込みに響かない。
        with tempfile.TemporaryDirectory() as tmp:
            cases = {
                "missing": "/nonexistent/quotes.json",
                "not_a_mapping": write_quotes(tmp, "文字列"),
                "nothing_defined": write_quotes(tmp, {"general": [], "by_hour": {}}),
            }
            for name, path in cases.items():
                with self.subTest(case=name):
                    first = load_quotes(path)
                    first["general"].append("書き換え")
                    first["by_hour"]["9"] = ["朝"]
                    self.assertNotIn("書き換え", FALLBACK_QUOTES)
                    self.assertEqual(load_quotes(path),
                                     {"general": list(FALLBACK_QUOTES), "by_hour": {}})

    def test_the_nothing_defined_fallback_keeps_by_hour_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_quotes(tmp, {"general": [], "by_hour": {}})
            self.assertEqual(load_quotes(path),
                             {"general": list(FALLBACK_QUOTES), "by_hour": {}})

    def test_by_hour_only_definition_is_not_replaced_by_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_quotes(tmp, {"by_hour": {"9": ["朝"]}})
            self.assertEqual(load_quotes(path), {"general": [], "by_hour": {"9": ["朝"]}})

    def test_non_string_items_are_converted_to_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_quotes(tmp, {"general": ["A", 1, None], "by_hour": {"9": [2.5, True]}})
            self.assertEqual(load_quotes(path), {"general": ["A", "1", "None"],
                                                 "by_hour": {"9": ["2.5", "True"]}})


class NormalizeQuotesTest(unittest.TestCase):
    """読み込んだデータの整形（``_normalize_quotes``。ファイルは読まない）。"""

    def test_a_plain_list_becomes_general(self):
        data, records = normalize_capturing_logs(["ひとつめ", "ふたつめ"])
        self.assertEqual(data, {"general": ["ひとつめ", "ふたつめ"], "by_hour": {}})
        self.assertEqual(records, [])

    def test_a_non_mapping_is_an_error_and_uses_the_fallback(self):
        for raw in ("文字列", 42, None, 3.5, True):
            with self.subTest(raw=raw):
                data, records = normalize_capturing_logs(raw, path="q/quotes.json")
                self.assertEqual(data, {"general": list(FALLBACK_QUOTES), "by_hour": {}})
                self.assertEqual([level for level, _ in records], [logging.ERROR])
                self.assertIn("形式が不正", records[0][1])
                self.assertIn("q/quotes.json", records[0][1])

    def test_a_truthy_non_list_general_is_warned_about(self):
        data, records = normalize_capturing_logs(
            {"general": {"a": 1}, "by_hour": {"9": ["朝"]}}, path="q/quotes.json")
        # general は空になるが、by_hour があるので予備には置き換わらない。
        self.assertEqual(data, {"general": [], "by_hour": {"9": ["朝"]}})
        self.assertEqual([level for level, _ in records], [logging.WARNING])
        self.assertIn("general が配列ではありません", records[0][1])
        self.assertIn("q/quotes.json", records[0][1])

    def test_a_falsy_non_list_general_is_silently_empty(self):
        for raw in (None, {}, "", 0):
            with self.subTest(raw=raw):
                data, records = normalize_capturing_logs(
                    {"general": raw, "by_hour": {"9": ["朝"]}})
                self.assertEqual(data["general"], [])
                self.assertEqual(records, [])

    def test_non_list_by_hour_entries_are_dropped_and_truthy_ones_warned_about(self):
        data, records = normalize_capturing_logs({
            "general": ["共通"],
            "by_hour": {"10": {"a": 1}, "11": "文字列", "12": None, "13": "", "14": 0,
                        "15": {}, "16": ["夕方"]},
        })
        self.assertEqual(data, {"general": ["共通"], "by_hour": {"16": ["夕方"]}})
        self.assertEqual([level for level, _ in records], [logging.WARNING, logging.WARNING])
        self.assertIn("by_hour[10] が配列ではありません", records[0][1])
        self.assertIn("by_hour[11] が配列ではありません", records[1][1])

    def test_a_non_mapping_by_hour_is_ignored_silently(self):
        for raw in (None, [], ["x"], "text", 0):
            with self.subTest(raw=raw):
                data, records = normalize_capturing_logs({"general": ["共通"], "by_hour": raw})
                self.assertEqual(data, {"general": ["共通"], "by_hour": {}})
                self.assertEqual(records, [])

    def test_by_hour_keys_become_strings(self):
        data, _ = normalize_capturing_logs({"general": ["共通"], "by_hour": {12: ["お昼"]}})
        self.assertEqual(data["by_hour"], {"12": ["お昼"]})

    def test_nothing_defined_warns_and_uses_the_fallback(self):
        for raw in ({}, {"general": [], "by_hour": {}}, {"general": None, "by_hour": None}):
            with self.subTest(raw=raw):
                data, records = normalize_capturing_logs(raw, path="q/quotes.json")
                self.assertEqual(data, {"general": list(FALLBACK_QUOTES), "by_hour": {}})
                self.assertEqual([level for level, _ in records], [logging.WARNING])
                self.assertIn("1 件も定義されていません", records[0][1])
                self.assertIn("q/quotes.json", records[0][1])

    def test_warnings_come_in_the_order_general_then_by_hour_then_empty(self):
        # general が不正で by_hour も使えない場合、3 つの警告がこの順に出る。
        data, records = normalize_capturing_logs(
            {"general": "x", "by_hour": {"9": "y"}}, path="q/quotes.json")
        self.assertEqual(data, {"general": list(FALLBACK_QUOTES), "by_hour": {}})
        messages = [message for _, message in records]
        self.assertEqual(len(messages), 3)
        self.assertIn("general が配列ではありません", messages[0])
        self.assertIn("by_hour[9] が配列ではありません", messages[1])
        self.assertIn("1 件も定義されていません", messages[2])


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

    def test_candidates_keep_first_seen_order_across_general_and_by_hour(self):
        # general が先、時刻専用が後。重複は最初に出た位置に残り、空文字列は除く。
        path = write_quotes(self.tmp.name, {
            "general": ["C", "A", "", "C", "B"],
            "by_hour": {"12": ["B", "D", "", "A", "E", "D"]},
        })
        picker = QuotePicker(path)
        self.assertEqual(picker.candidates(12), ["C", "A", "B", "D", "E"])
        self.assertEqual(picker.candidates(10), ["C", "A", "B"])
        self.assertEqual(picker.candidates(), ["C", "A", "B"])

    def test_pick_chooses_from_the_ordered_fresh_candidates(self):
        # rng.choice に渡る並び（＝シード固定の抽選が指す位置）は、候補の順から
        # 直近のものを除いたもの。
        class RecordingRng:
            def __init__(self):
                self.seen = None

            def choice(self, seq):
                self.seen = list(seq)
                return seq[-1]

        rng = RecordingRng()
        picker = QuotePicker(self.path, avoid_recent=1, rng=rng)
        self.assertEqual(picker.pick(12, recent=["B"]), "ランチ")
        self.assertEqual(rng.seen, ["A", "C", "ランチ"])
        # 全部が直近なら、除かずに全候補から選ぶ。
        picker = QuotePicker(self.path, avoid_recent=10, rng=rng)
        picker.pick(10, recent=["A", "B", "C"])
        self.assertEqual(rng.seen, ["A", "B", "C"])

    def test_seeded_picks_are_reproducible(self):
        first = QuotePicker(self.path, rng=random.Random(42))
        second = QuotePicker(self.path, rng=random.Random(42))
        for hour in (None, 10, 12, 16, 12):
            self.assertEqual(first.pick(hour), second.pick(hour))

    def test_empty_definition_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_quotes(tmp, {"general": [], "by_hour": {"9": ["朝"]}})
            picker = QuotePicker(path)
            with self.assertRaises(QuoteError):
                picker.pick(10)
            self.assertEqual(picker.pick(9), "朝")


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


class FallbackQuotesTest(unittest.TestCase):
    """内蔵の予備のひとこと（``FALLBACK_QUOTES``）の健全性。

    予備も読み上げ音声は作り置きから引くため、作り置きの無い文だと
    ``quotes.json`` が読めないときに無音になってしまう。
    """

    def test_every_fallback_quote_has_a_prerecorded_voice(self):
        manifest = load_manifest()
        for quote in FALLBACK_QUOTES:
            self.assertIn(quote, manifest)

    def test_fallback_quotes_end_with_the_zundamon_ending(self):
        for quote in FALLBACK_QUOTES:
            self.assertTrue(quote.endswith("のだ。"), quote)


if __name__ == "__main__":
    unittest.main()
