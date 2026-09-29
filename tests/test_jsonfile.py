"""JSON ファイルの読み書き（chime/jsonfile.py）のテスト。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from chime.jsonfile import JsonFileError, read_json, write_json_atomic


class ReadJsonTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write_bytes(self, data, name="data.json"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def read_error(self, path):
        with self.assertRaises(JsonFileError) as caught:
            read_json(path)
        return caught.exception


class ReadJsonTest(ReadJsonTestCase):
    def test_reads_utf8(self):
        path = self.write_bytes('{"あいさつ": "こんにちは"}'.encode("utf-8"))
        self.assertEqual(read_json(path), {"あいさつ": "こんにちは"})

    def test_reads_utf8_with_bom(self):
        # メモ帳の「UTF-8（BOM 付き）」。
        path = self.write_bytes(b"\xef\xbb\xbf" + '{"a": "あ"}'.encode("utf-8"))
        self.assertEqual(read_json(path), {"a": "あ"})

    def test_returns_any_top_level_type(self):
        self.assertEqual(read_json(self.write_bytes(b"[1, 2]")), [1, 2])

    def test_missing_file(self):
        path = os.path.join(self.dir, "nope.json")
        error = self.read_error(path)
        self.assertEqual(error.kind, "missing")
        self.assertEqual(error.path, path)
        self.assertIn("見つかりません", str(error))
        self.assertIn(path, str(error))

    def test_directory_is_an_io_error(self):
        error = self.read_error(self.dir)
        self.assertEqual(error.kind, "io")
        self.assertEqual(error.path, self.dir)
        self.assertIn(self.dir, str(error))

    def test_error_is_an_exception_with_the_guidance_as_its_message(self):
        error = self.read_error(os.path.join(self.dir, "nope.json"))
        self.assertIsInstance(error, Exception)
        self.assertEqual(error.args, (str(error),))


class EncodingErrorTest(ReadJsonTestCase):
    def test_shift_jis_is_reported_with_guidance(self):
        path = self.write_bytes('{"a": "あ"}'.encode("shift_jis"))
        error = self.read_error(path)
        self.assertEqual(error.kind, "encoding")
        self.assertEqual(error.path, path)
        message = str(error)
        self.assertIn(path, message)
        self.assertIn("UTF-8 として読めません", message)
        self.assertIn("『UTF-8』を選んでください", message)
        self.assertIn("Shift_JIS", message)

    def test_reports_the_byte_position_counted_from_one(self):
        # {"a": " が 7 バイトなので、Shift_JIS の「あ」（82 A0）は 8 バイト目から。
        path = self.write_bytes('{"a": "あ"}'.encode("shift_jis"))
        self.assertIn("（8 バイト目）", str(self.read_error(path)))

    def test_byte_position_counts_the_bom_too(self):
        path = self.write_bytes(b"\xef\xbb\xbf" + '{"a": "あ"}'.encode("shift_jis"))
        self.assertIn("（11 バイト目）", str(self.read_error(path)))

    def test_utf16_is_reported_as_encoding_error(self):
        # メモ帳の「Unicode」（UTF-16）も UTF-8 ではない。
        path = self.write_bytes('{"a": 1}'.encode("utf-16"))
        self.assertEqual(self.read_error(path).kind, "encoding")


class SyntaxErrorTest(ReadJsonTestCase):
    def test_reports_line_and_column(self):
        path = self.write_bytes('{\n  "a": 1\n  "b": 2\n}'.encode("utf-8"))
        error = self.read_error(path)
        self.assertEqual(error.kind, "syntax")
        self.assertEqual(error.path, path)
        self.assertIn("{0} の 3 行 3 文字目で JSON の書き方が正しくありません".format(path),
                      str(error))

    def test_includes_the_parser_message(self):
        path = self.write_bytes(b'{"a" 1}')
        self.assertIn("Expecting ':' delimiter", str(self.read_error(path)))

    def test_empty_file_is_a_syntax_error(self):
        error = self.read_error(self.write_bytes(b""))
        self.assertEqual(error.kind, "syntax")
        self.assertIn("1 行 1 文字目", str(error))

    def test_fullwidth_quotes_hint(self):
        path = self.write_bytes('{\n  “a”: 1\n}'.encode("utf-8"))
        message = str(self.read_error(path))
        self.assertIn("2 行 3 文字目", message)
        self.assertIn("全角の記号が混ざっていませんか", message)

    def test_fullwidth_comma_hint(self):
        path = self.write_bytes('{"a": 1，"b": 2}'.encode("utf-8"))
        self.assertIn("全角の記号が混ざっていませんか", str(self.read_error(path)))

    def test_fullwidth_hint_looks_only_at_the_reported_line(self):
        # 全角の記号が別の行にあるだけでは、ヒントを出さない。
        path = self.write_bytes('{\n  "a": "全角，のカンマ",\n  "b" 1\n}'.encode("utf-8"))
        message = str(self.read_error(path))
        self.assertIn("3 行", message)
        self.assertNotIn("全角の記号", message)

    def test_trailing_comma_in_an_object_hint(self):
        path = self.write_bytes(b'{\n  "a": 1,\n  "b": 2,\n}')
        # 報告される位置は Python のバージョンで違う（3.13 以降はカンマ自身）ので、
        # 位置は問わない。
        message = str(self.read_error(path))
        self.assertIn("最後の項目のあとにカンマは付けられません", message)
        self.assertNotIn("全角", message)

    def test_trailing_comma_in_an_array_hint(self):
        path = self.write_bytes(b'{"a": [1, 2,\n  ]}')
        self.assertIn("最後の項目のあとにカンマは付けられません",
                      str(self.read_error(path)))

    def test_no_hint_for_other_mistakes(self):
        path = self.write_bytes(b'{"a" 1}')
        message = str(self.read_error(path))
        self.assertNotIn("ヒント", message)


class WriteJsonAtomicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "dir", "data.json")

    def tearDown(self):
        self.tmp.cleanup()

    def read_text(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return handle.read()

    def leftovers(self):
        return [name for name in os.listdir(os.path.dirname(self.path))
                if name.endswith(".tmp")]

    def test_creates_the_directory(self):
        write_json_atomic(self.path, {"a": 1})
        self.assertEqual(json.loads(self.read_text()), {"a": 1})

    def test_format_is_indented_utf8_with_a_trailing_newline(self):
        write_json_atomic(self.path, {"あ": ["い"]})
        self.assertEqual(self.read_text(), '{\n  "あ": [\n    "い"\n  ]\n}\n')

    def test_keys_keep_their_order_by_default(self):
        write_json_atomic(self.path, {"b": 1, "a": 2})
        self.assertEqual(list(json.loads(self.read_text())), ["b", "a"])

    def test_sort_keys(self):
        write_json_atomic(self.path, {"b": 1, "a": 2}, sort_keys=True)
        self.assertEqual(list(json.loads(self.read_text())), ["a", "b"])

    def test_replaces_an_existing_file(self):
        write_json_atomic(self.path, {"v": 1})
        write_json_atomic(self.path, {"v": 2})
        self.assertEqual(json.loads(self.read_text()), {"v": 2})
        self.assertEqual(self.leftovers(), [])

    def test_round_trips_through_read_json(self):
        data = {"last_fired": {"閉館": "2026-08-26"}, "recent_quotes": ["おつかれさま"]}
        write_json_atomic(self.path, data)
        self.assertEqual(read_json(self.path), data)

    def test_writes_next_to_the_target_with_pid_in_the_temporary_name(self):
        seen = []
        real_replace = os.replace

        def spy(source, destination):
            seen.append(source)
            real_replace(source, destination)

        with mock.patch("chime.jsonfile.os.replace", side_effect=spy):
            write_json_atomic(self.path, {"a": 1})
        self.assertEqual(seen, ["{0}.{1}.tmp".format(self.path, os.getpid())])

    def test_failure_while_serializing_keeps_the_old_file_and_cleans_up(self):
        write_json_atomic(self.path, {"v": 1})
        with self.assertRaises(TypeError):
            write_json_atomic(self.path, {"v": object()})
        self.assertEqual(json.loads(self.read_text()), {"v": 1})
        self.assertEqual(self.leftovers(), [])

    def test_failure_while_replacing_cleans_up_and_reraises(self):
        with mock.patch("chime.jsonfile.os.replace", side_effect=OSError("置き換えられない")):
            with self.assertRaises(OSError) as caught:
                write_json_atomic(self.path, {"v": 1})
        self.assertIn("置き換えられない", str(caught.exception))
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
