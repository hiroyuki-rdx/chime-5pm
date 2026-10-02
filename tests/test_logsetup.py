"""ログ設定（journal の重大度の接頭辞）のテスト。"""

from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

from chime.logsetup import (
    JournalPriorityFormatter,
    emit_early_error,
    journal_stream_matches,
    setup_logging,
)

FORMAT = "%(levelname)s %(message)s"


def journal_stream_value(handle) -> str:
    """``handle`` を journal のストリームとみなす ``JOURNAL_STREAM`` の値。"""
    stat = os.fstat(handle.fileno())
    return "{0}:{1}".format(stat.st_dev, stat.st_ino)


def read_all(handle) -> str:
    handle.flush()
    handle.seek(0)
    return handle.read()


class LoggingTestCase(unittest.TestCase):
    """ルートロガーと ``JOURNAL_STREAM`` を、テストの前後で元に戻す。"""

    def setUp(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        root.handlers[:] = []
        # tests/__init__.py がログを抑制しているため、ここだけ戻す
        logging.disable(logging.NOTSET)

        def restore():
            logging.disable(logging.CRITICAL)
            root.handlers[:] = handlers
            root.setLevel(level)

        self.addCleanup(restore)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("JOURNAL_STREAM", None)

        self.stream = tempfile.NamedTemporaryFile("w+", encoding="utf-8")
        self.addCleanup(self.stream.close)
        self.logger = logging.getLogger("chime.test_logsetup")


class JournalPriorityTest(LoggingTestCase):
    def test_every_line_has_prefix_when_stream_is_journal(self):
        os.environ["JOURNAL_STREAM"] = journal_stream_value(self.stream)
        setup_logging("DEBUG", FORMAT, stream=self.stream)

        self.logger.debug("デバッグ")
        self.logger.info("通常")
        self.logger.warning("警告")
        self.logger.error("一行目\n二行目")
        self.logger.critical("致命的")
        try:
            raise ValueError("boom")
        except ValueError:
            self.logger.exception("例外")

        lines = read_all(self.stream).splitlines()
        self.assertEqual(lines[0], "<7>DEBUG デバッグ")
        self.assertEqual(lines[1], "<6>INFO 通常")
        self.assertEqual(lines[2], "<4>WARNING 警告")
        self.assertEqual(lines[3:5], ["<3>ERROR 一行目", "<3>二行目"])
        self.assertEqual(lines[5], "<2>CRITICAL 致命的")
        # 例外（traceback を含む）も全行が error の重大度になる
        exception_lines = lines[6:]
        self.assertEqual(exception_lines[0], "<3>ERROR 例外")
        self.assertIn("<3>Traceback (most recent call last):", exception_lines)
        self.assertEqual(exception_lines[-1], "<3>ValueError: boom")
        self.assertTrue(all(line.startswith("<3>") for line in exception_lines), exception_lines)

    def test_no_prefix_without_journal_stream_variable(self):
        setup_logging("INFO", FORMAT, stream=self.stream)

        self.logger.error("一行目\n二行目")

        self.assertEqual(read_all(self.stream), "ERROR 一行目\n二行目\n")

    def test_no_prefix_when_journal_stream_is_another_file(self):
        with tempfile.NamedTemporaryFile("w+") as other:
            os.environ["JOURNAL_STREAM"] = journal_stream_value(other)
            setup_logging("INFO", FORMAT, stream=self.stream)

            self.logger.error("エラー")

        self.assertEqual(read_all(self.stream), "ERROR エラー\n")

    def test_formatter_is_chosen_by_stream(self):
        setup_logging("INFO", FORMAT, stream=self.stream)
        handler = logging.getLogger().handlers[0]
        self.assertNotIsInstance(handler.formatter, JournalPriorityFormatter)

        os.environ["JOURNAL_STREAM"] = journal_stream_value(self.stream)
        setup_logging("INFO", FORMAT, stream=self.stream)
        self.assertIsInstance(handler.formatter, JournalPriorityFormatter)


class SetupLoggingTest(LoggingTestCase):
    def test_second_call_updates_the_same_handler(self):
        root = logging.getLogger()
        setup_logging("INFO", FORMAT, stream=self.stream)
        first = list(root.handlers)
        self.assertEqual(len(first), 1)

        setup_logging("DEBUG", "%(message)s [%(levelname)s]", stream=self.stream)

        self.assertEqual(root.handlers, first)
        self.logger.debug("更新後")
        self.assertEqual(read_all(self.stream), "更新後 [DEBUG]\n")

    def test_level_is_applied(self):
        setup_logging("WARNING", FORMAT, stream=self.stream)

        self.logger.info("出ない")
        self.logger.warning("出る")

        self.assertEqual(read_all(self.stream), "WARNING 出る\n")

    def test_unknown_level_falls_back_to_info(self):
        setup_logging("NOPE", FORMAT, stream=self.stream)

        self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_foreign_handler_is_left_alone_and_none_is_added(self):
        """すでにハンドラがあれば追加しない（``logging.basicConfig`` と同じ）。"""
        root = logging.getLogger()
        buffer = io.StringIO()
        foreign = logging.StreamHandler(buffer)
        root.addHandler(foreign)
        root.setLevel(logging.ERROR)

        setup_logging("DEBUG", FORMAT, stream=self.stream)

        self.assertEqual(root.handlers, [foreign])
        self.assertEqual(root.level, logging.ERROR)

    def test_timestamps_use_the_configured_timezone(self):
        setup_logging("INFO", "%(asctime)s %(message)s", "Asia/Tokyo", stream=self.stream)
        record = logging.LogRecord("chime", logging.INFO, __file__, 1, "確認", None, None)
        record.created = 0  # 1970-01-01 00:00:00 UTC

        formatted = logging.getLogger().handlers[0].format(record)

        self.assertTrue(formatted.startswith("1970-01-01 09:00:00"), formatted)

    def test_default_stream_is_stdout_at_call_time(self):
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdout", buffer):
            setup_logging("INFO", FORMAT)
            self.logger.info("標準出力")

        self.assertEqual(buffer.getvalue(), "INFO 標準出力\n")


class JournalStreamMatchesTest(LoggingTestCase):
    def test_matches_only_the_same_device_and_inode(self):
        os.environ["JOURNAL_STREAM"] = journal_stream_value(self.stream)
        self.assertTrue(journal_stream_matches(self.stream))

    def test_false_without_variable(self):
        self.assertFalse(journal_stream_matches(self.stream))

    def test_false_for_stream_without_file_descriptor(self):
        os.environ["JOURNAL_STREAM"] = journal_stream_value(self.stream)
        self.assertFalse(journal_stream_matches(io.StringIO()))
        self.assertFalse(journal_stream_matches(None))

    def test_false_for_malformed_variable(self):
        for value in ("", "abc", "1", "1:", ":2", "1:2:3", "x:y"):
            os.environ["JOURNAL_STREAM"] = value
            self.assertFalse(journal_stream_matches(self.stream), value)


class EmitEarlyErrorTest(LoggingTestCase):
    def test_each_line_has_error_prefix_when_stderr_is_journal(self):
        os.environ["JOURNAL_STREAM"] = journal_stream_value(self.stream)

        with mock.patch.object(sys, "stderr", self.stream):
            emit_early_error("設定エラー: 一行目\n二行目")

        self.assertEqual(read_all(self.stream), "<3>設定エラー: 一行目\n<3>二行目\n")

    def test_plain_text_when_stderr_is_not_journal(self):
        buffer = io.StringIO()

        with mock.patch.object(sys, "stderr", buffer):
            emit_early_error("設定エラー: 一行目\n二行目")

        self.assertEqual(buffer.getvalue(), "設定エラー: 一行目\n二行目\n")

    def test_no_prefix_when_journal_stream_is_another_file(self):
        with tempfile.NamedTemporaryFile("w+") as other:
            os.environ["JOURNAL_STREAM"] = journal_stream_value(other)
            with mock.patch.object(sys, "stderr", self.stream):
                emit_early_error("設定エラー")

        self.assertEqual(read_all(self.stream), "設定エラー\n")


if __name__ == "__main__":
    unittest.main()
