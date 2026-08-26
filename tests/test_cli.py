"""CLI と環境判定のテスト。"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests.support import REPO_ROOT  # noqa: F401

from chime import env
from chime.cli import build_parser, run


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
        code, output = call(["--test-hourly", "12", "--dry-run", "--backend", "mock"])
        self.assertEqual(code, 0)
        self.assertIn("正午をお知らせしました。", output)
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
        with mock.patch("chime.weather.fetch_json",
                        side_effect=__import__("chime.weather", fromlist=["x"]).WeatherError("圏外")):
            code, output = call(["--weather", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("天気予報を取得できませんでした", output)

    def test_weather_success_prints_text(self):
        fixture = os.path.join(REPO_ROOT, "tests", "fixtures", "jma_130000.json")
        with open(fixture, encoding="utf-8") as handle:
            payload = json.load(handle)
        with mock.patch("chime.weather.fetch_json", return_value=payload):
            code, output = call(["--weather", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("の天気は、", output)

    def test_config_file_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schedule": {"hourly": {"start_hour": 8, "end_hour": 8},
                                        "closing": {"enabled": False}}}, handle)
            code, output = call(["--config", path, "--schedule", "1"])
        self.assertEqual(code, 0)
        self.assertIn("08:00:00", output)


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
