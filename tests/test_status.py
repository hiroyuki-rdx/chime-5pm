"""状態の表示と放送の最中の待機（``chime/status.py``、``--status`` / ``--wait-idle``）のテスト。

``timedatectl`` / ``systemctl`` は本物を呼ばない（呼び出し側から差し替える）。
時刻も差し替えられる偽の時計を使い、実際には眠らない。表示は何も書かない
（履歴ファイルが無ければ作らない）ことも確かめる。
"""

from __future__ import annotations

import io
import os
import random
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from tests.support import SHIPPED_QUOTES, VOICE_DIR, block_network, logs_enabled, make_event

from chime import status, timesignal
from chime.config import DEFAULT_CONFIG, Config, deep_merge
from chime.history import History, make_entry
from chime.scheduler import Scheduler
from chime.status import (attention, broadcast_window, busy_event, collect_status, describe_entry,
                          ntp_synchronized, query_command, run_status, service_state, wait_idle)

TZ = ZoneInfo("Asia/Tokyo")
SERVICE = "campus_chime.service"

# 2026-10-09 は金曜日、2026-10-10 は土曜日。
FRIDAY = datetime(2026, 10, 9, 12, 30, 0, tzinfo=TZ)
SATURDAY = datetime(2026, 10, 10, 12, 59, 30, tzinfo=TZ)

IS_ACTIVE = ("systemctl", "is-active", SERVICE)
IS_ENABLED = ("systemctl", "is-enabled", SERVICE)
NTP = tuple(status.NTP_COMMAND)

HEALTHY_ANSWERS = {NTP: "yes\n", IS_ACTIVE: "active\n", IS_ENABLED: "enabled\n"}


class FakeRun:
    """``subprocess.run`` の代わり。コマンドごとに答える（答えが無いコマンドは「見つからない」）。"""

    def __init__(self, answers=None):
        self.answers = dict(HEALTHY_ANSWERS if answers is None else answers)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        answer = self.answers.get(tuple(command), FileNotFoundError(command[0]))
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(returncode=0, stdout=answer, stderr="")


def random_entries(count, seed=20261009):
    """型も中身もでたらめな履歴 1 件を ``count`` 個（同じ ``seed`` なら毎回同じ）。

    人が書き換えた履歴や、将来の版が足した項目を想定する。``at`` に範囲外の時刻、
    ``missing`` / ``silent`` に文字列・数・入れ子、改行や制御文字や孤立したサロゲートを混ぜる。
    """
    rng = random.Random(seed)
    texts = ["", "ok", "hourly", "closing", "失敗", "\n", "\x1b[31m", "\ud800", "x" * 5000,
             "2026-10-09T16:57:00+09:00", "2026-10-09T16:57:00", "2026-10-09",
             "9999-12-31T23:59:59+00:00", "0001-01-01T00:00:00+09:00",
             "0001-01-01T00:00:00-23:59", "2026-13-45", "20261009T165700Z", "\x00"]
    scalars = texts + [None, True, False, 0, -1, 10 ** 40, 1.5, float("nan"), float("inf")]
    containers = [[], [None], [1, "a", None], texts[:5], [texts], {}, {"a": [1]}]
    values = scalars + containers
    keys = ["at", "day", "key", "kind", "result", "played", "total", "silent", "missing",
            "warnings", "degraded", "error", "v", "extra"]
    return [{key: rng.choice(values) for key in keys if rng.random() < 0.7}
            for _ in range(count)]


def make_config(base_dir, override=None):
    """同梱の作り置きとひとことを使い、状態・履歴は ``base_dir`` に置く設定。"""
    data = deep_merge(DEFAULT_CONFIG, {
        "tts": {"prerecorded_dir": VOICE_DIR, "cache_dir": os.path.join(base_dir, "tts")},
        "quotes": {"file": SHIPPED_QUOTES},
    })
    return Config(deep_merge(data, override or {}), base_dir=base_dir)


def make_scheduler(now, settings=None):
    clock = {"now": now}
    scheduler = Scheduler(settings or DEFAULT_CONFIG["schedule"], TZ,
                          timesignal.lead_seconds(DEFAULT_CONFIG["time_signal"]),
                          clock=lambda: clock["now"])
    scheduler.clock = clock  # テストが時刻を進めるための取っ手
    return scheduler


def make_app(config, backend="pygame", now=FRIDAY):
    """``ChimeApp`` の代わり（表示が使う分だけ）。時刻は固定、スケジューラも同じ時計で動く。"""
    scheduler = make_scheduler(now, config.section("schedule"))
    return SimpleNamespace(config=config, tzinfo=TZ, now=scheduler.now, scheduler=scheduler,
                           player=SimpleNamespace(name=backend))


class StatusCase(unittest.TestCase):
    def setUp(self):
        block_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.history_path = os.path.join(self.tmp, "history.jsonl")
        self.config = make_config(self.tmp, {"state": {"history_file": self.history_path}})

    def add_history(self, **overrides):
        values = dict(at=FRIDAY - timedelta(hours=1), key="hourly:11", kind="hourly",
                      played=3, total=3)
        values.update(overrides)
        History(self.history_path).append(make_entry(**values))

    def show(self, answers=None, backend="pygame", production=True, config=None, now=FRIDAY):
        """(終了コード, 出力)。"""
        out = io.StringIO()
        app = make_app(config or self.config, backend, now)
        code = run_status(app, out=out, run=FakeRun(answers), production=production,
                          version="campus-chime 6.1.0 (abc1234)")
        return code, out.getvalue()


class QueryCommandTest(unittest.TestCase):
    def test_returns_the_first_line_of_standard_output(self):
        run = FakeRun({("x",): "  active  \nsecond\n"})
        self.assertEqual(query_command(["x"], run), "active")

    def test_the_command_is_given_a_three_second_timeout_and_text_output(self):
        run = FakeRun({("x",): "yes\n"})
        query_command(["x"], run)
        [(command, kwargs)] = run.calls
        self.assertEqual(command, ("x",))
        self.assertEqual(kwargs["timeout"], 3)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])

    def test_a_missing_command_is_none(self):
        self.assertIsNone(query_command(["nope"], FakeRun({})))

    def test_a_timeout_is_none(self):
        run = FakeRun({("x",): subprocess.TimeoutExpired("x", 3)})
        self.assertIsNone(query_command(["x"], run))

    def test_other_os_errors_are_none(self):
        run = FakeRun({("x",): PermissionError("x")})
        self.assertIsNone(query_command(["x"], run))

    def test_empty_output_is_none(self):
        self.assertIsNone(query_command(["x"], FakeRun({("x",): "\n"})))
        self.assertIsNone(query_command(["x"], FakeRun({("x",): None})))

    def test_output_that_cannot_be_decoded_is_none(self):
        # text=True の出力が UTF-8 でないと UnicodeDecodeError（ValueError の子）になる。
        run = FakeRun({("x",): UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")})
        self.assertIsNone(query_command(["x"], run))

    def test_the_exit_code_is_not_what_decides(self):
        """``systemctl is-active`` は止まっていると 0 以外を返すが、状態は標準出力に出る。"""
        def run(command, **kwargs):
            return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")

        self.assertEqual(query_command(["systemctl"], run), "inactive")

    def test_it_uses_subprocess_run_looked_up_at_call_time(self):
        with mock.patch("chime.status.subprocess.run",
                        return_value=SimpleNamespace(returncode=0, stdout="yes\n")) as patched:
            self.assertTrue(ntp_synchronized())
        patched.assert_called_once()
        self.assertEqual(patched.call_args[0][0], status.NTP_COMMAND)

    def test_standard_output_that_is_not_text_does_not_raise(self):
        run = FakeRun({("x",): "��"})
        self.assertEqual(query_command(["x"], run), "��")


class ProbesTest(unittest.TestCase):
    def test_ntp_answers(self):
        self.assertIs(ntp_synchronized(FakeRun({NTP: "yes\n"})), True)
        self.assertIs(ntp_synchronized(FakeRun({NTP: "no\n"})), False)
        self.assertIsNone(ntp_synchronized(FakeRun({NTP: "maybe\n"})))
        self.assertIsNone(ntp_synchronized(FakeRun({})))

    def test_the_ntp_command_asks_timedatectl_for_ntpsynchronized(self):
        self.assertEqual(status.NTP_COMMAND,
                         ["timedatectl", "show", "-p", "NTPSynchronized", "--value"])

    def test_service_state_asks_both_questions_about_the_service(self):
        run = FakeRun({IS_ACTIVE: "active\n", IS_ENABLED: "enabled\n"})
        self.assertEqual(service_state(run), ("active", "enabled"))
        self.assertEqual([call[0] for call in run.calls], [IS_ACTIVE, IS_ENABLED])

    def test_service_state_without_systemctl_is_unknown(self):
        self.assertEqual(service_state(FakeRun({})), (None, None))

    def test_service_state_keeps_each_answer_apart(self):
        run = FakeRun({IS_ACTIVE: "inactive\n"})
        self.assertEqual(service_state(run), ("inactive", None))


class RunStatusTest(StatusCase):
    def test_a_healthy_machine_exits_0(self):
        self.add_history()
        code, output = self.show()
        self.assertEqual(code, 0, output)
        self.assertIn("気になる点は見つかりませんでした。", output)
        self.assertNotIn("要確認", output)

    def test_the_first_line_is_the_version(self):
        _, output = self.show()
        self.assertEqual(output.splitlines()[0], "campus-chime 6.1.0 (abc1234)")

    def test_the_version_defaults_to_the_build_info(self):
        out = io.StringIO()
        with mock.patch("chime.status.buildinfo.version_string", return_value="campus-chime X (y)"):
            run_status(make_app(self.config), out=out, run=FakeRun(), production=True)
        self.assertEqual(out.getvalue().splitlines()[0], "campus-chime X (y)")

    def test_it_shows_the_time_in_the_configured_zone(self):
        _, output = self.show()
        self.assertIn("2026-10-09 12:30:00 JST", output)

    def test_it_shows_each_fact_with_aligned_labels(self):
        _, output = self.show()
        labels = ("現在時刻", "時刻の同期", "サービス", "自動起動", "再生方法", "作り置きの音声")
        rows = [line for line in output.splitlines() if line.startswith(labels)]
        self.assertEqual(len(rows), 6)
        width = max(status.display_width(label) for label in labels)
        for row in rows:
            label = next(label for label in labels if row.startswith(label))
            value = row[len(label):].lstrip(" ")
            padding = len(row) - len(label) - len(value)
            # 項目名の長さはまちまちだが、値の始まる桁は全行でそろう（項目名の幅 + 空白 2 つ）。
            self.assertEqual(status.display_width(label) + padding, width + 2, row)

    def test_ntp_no_needs_attention(self):
        code, output = self.show({**HEALTHY_ANSWERS, NTP: "no\n"})
        self.assertEqual(code, 1)
        self.assertIn("同期していません", output)
        self.assertIn("時刻が NTP と同期していません", output.split("要確認:")[1])

    def test_ntp_that_cannot_be_checked_is_not_a_problem(self):
        code, output = self.show({IS_ACTIVE: "active\n", IS_ENABLED: "enabled\n"})
        self.assertEqual(code, 0, output)
        self.assertIn("時刻の同期      確認できません", output)

    def test_an_inactive_service_needs_attention(self):
        code, output = self.show({**HEALTHY_ANSWERS, IS_ACTIVE: "inactive\n"})
        self.assertEqual(code, 1)
        self.assertIn("止まっています（inactive）", output)
        self.assertIn("サービス campus_chime.service が動いていません（inactive）",
                      output.split("要確認:")[1])

    def test_a_failed_service_needs_attention(self):
        code, _ = self.show({**HEALTHY_ANSWERS, IS_ACTIVE: "failed\n"})
        self.assertEqual(code, 1)

    def test_an_unknown_service_state_is_not_a_problem(self):
        code, output = self.show({NTP: "yes\n"})
        self.assertEqual(code, 0, output)
        self.assertIn("サービス        確認できません", output)

    def test_a_disabled_service_is_shown_but_is_not_a_problem(self):
        code, output = self.show({**HEALTHY_ANSWERS, IS_ENABLED: "disabled\n"})
        self.assertEqual(code, 0, output)
        self.assertIn("無効（再起動すると始まりません）", output)

    def test_mock_on_a_production_linux_needs_attention(self):
        code, output = self.show(backend="mock", production=True)
        self.assertEqual(code, 1)
        self.assertIn("mock（音が出ません）", output)
        self.assertIn("再生方法が mock です", output.split("要確認:")[1])

    def test_mock_on_a_development_machine_is_fine(self):
        code, output = self.show(backend="mock", production=False)
        self.assertEqual(code, 0, output)
        self.assertIn("開発環境なので音は出しません", output)

    def test_other_backends_on_linux_are_fine(self):
        for backend in ("pygame", "command"):
            code, output = self.show(backend=backend)
            self.assertEqual(code, 0, output)
            self.assertIn(backend, output)

    def test_production_defaults_to_the_real_environment(self):
        out = io.StringIO()
        app = make_app(self.config, "mock")
        with mock.patch("chime.status.env.is_production_linux", return_value=True):
            self.assertEqual(run_status(app, out=out, run=FakeRun(), version="v"), 1)
        with mock.patch("chime.status.env.is_production_linux", return_value=False):
            self.assertEqual(run_status(app, out=io.StringIO(), run=FakeRun(), version="v"), 0)

    def test_a_failed_last_broadcast_needs_attention(self):
        self.add_history(played=0, total=3)
        code, output = self.show()
        self.assertEqual(code, 1)
        self.assertIn("直近の放送が失敗しています", output)

    def test_an_errored_last_broadcast_needs_attention(self):
        self.add_history(played=0, total=0, error="RuntimeError: x")
        code, output = self.show()
        self.assertEqual(code, 1)
        self.assertIn("エラー: RuntimeError: x", output)

    def test_only_the_latest_broadcast_decides(self):
        self.add_history(played=0, total=3, at=FRIDAY - timedelta(hours=2))
        self.add_history(played=3, total=3, at=FRIDAY - timedelta(hours=1))
        code, output = self.show()
        self.assertEqual(code, 0, output)

    def test_a_partial_last_broadcast_is_shown_with_its_silent_texts_but_is_not_a_failure(self):
        self.add_history(played=2, total=3, silent=["今日は晴れなのだ。"])
        code, output = self.show()
        self.assertEqual(code, 0, output)
        self.assertIn("一部のみ", output)
        self.assertIn("無音: 「今日は晴れなのだ。」", output)

    def test_an_inactive_service_row_is_marked_on_the_row_itself(self):
        _, output = self.show({NTP: "yes\n", IS_ACTIVE: "inactive\n", IS_ENABLED: "enabled\n"})
        [row] = [line for line in output.splitlines() if line.startswith("サービス")]
        self.assertIn("要確認", row)

    def test_a_last_broadcast_with_a_missing_part_shows_it_and_needs_attention(self):
        # 閉館放送の蛍の光が無いまま鳴らした（played == total でも「成功」ではない）。
        self.add_history(kind="closing", key="closing", played=1, total=1,
                         missing=["蛍の光（2000ms フェードイン）"])
        code, output = self.show()
        self.assertEqual(code, 1, output)
        self.assertIn("一部のみ", output)
        self.assertNotIn("成功", output)
        self.assertIn("欠けた音源: 「蛍の光（2000ms フェードイン）」", output)
        self.assertIn("音源ファイルが無くて鳴らせなかった部分があります",
                      output.split("要確認:")[1])

    def test_every_listed_entry_shows_its_own_missing_parts_and_silent_texts(self):
        self.add_history(at=FRIDAY - timedelta(hours=3), played=1, total=1,
                         missing=["時報音（ポ・ポ・ポ・ポーン）"], silent=["今日は晴れなのだ。"])
        self.add_history(at=FRIDAY - timedelta(hours=2), kind="closing", key="closing",
                         played=1, total=1, missing=["閉館アナウンス"])
        self.add_history(at=FRIDAY - timedelta(hours=1), played=3, total=3)
        _, output = self.show()
        lines = output.split("直近の放送（新しい順）")[1].split("次の予定")[0].splitlines()
        lines = [line for line in lines if line.strip()]
        self.assertEqual(len(lines), 3)
        self.assertNotIn("欠けた音源", lines[0])
        self.assertIn("欠けた音源: 「閉館アナウンス」", lines[1])
        self.assertNotIn("無音", lines[1])
        self.assertIn("欠けた音源: 「時報音（ポ・ポ・ポ・ポーン）」", lines[2])
        self.assertIn("無音: 「今日は晴れなのだ。」", lines[2])

    def test_a_missing_part_in_an_older_broadcast_does_not_decide(self):
        self.add_history(at=FRIDAY - timedelta(hours=2), played=1, total=1, missing=["蛍の光"])
        self.add_history(at=FRIDAY - timedelta(hours=1), played=3, total=3)
        code, output = self.show()
        self.assertEqual(code, 0, output)
        self.assertIn("欠けた音源: 「蛍の光」", output)  # 表示はする

    def test_a_failed_last_broadcast_with_missing_parts_gives_one_reason(self):
        self.add_history(kind="closing", key="closing", played=0, total=0,
                         missing=["閉館アナウンス", "蛍の光"])
        code, output = self.show()
        reasons = output.split("要確認:")[1].strip().splitlines()
        self.assertEqual(code, 1)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("直近の放送が失敗しています", reasons[0])
        self.assertIn("欠けた音源: 「閉館アナウンス」「蛍の光」", output)

    def test_a_history_line_from_before_missing_was_added_is_still_shown(self):
        old = make_entry(at=FRIDAY - timedelta(hours=1), key="hourly:11", kind="hourly",
                         played=3, total=3)
        del old["missing"]
        History(self.history_path).append(old)
        code, output = self.show()
        self.assertEqual(code, 0, output)
        self.assertIn("成功", output)

    def test_an_extreme_timestamp_in_the_history_does_not_stop_the_status(self):
        # 手で書き換えた履歴の、範囲外の時刻。表示は止めず、時刻が分からないと示す。
        for at in ("9999-12-31T23:59:59+00:00", "0001-01-01T00:00:00+09:00"):
            with self.subTest(at=at):
                if os.path.exists(self.history_path):
                    os.remove(self.history_path)
                entry = make_entry(at=FRIDAY, key="hourly:11", kind="hourly", played=3, total=3)
                entry["at"] = at
                self.assertTrue(History(self.history_path).append(entry))
                code, output = self.show()
                self.assertEqual(code, 0, output)
                self.assertIn("（時刻不明）  時報", output)

    def test_a_count_that_is_too_large_is_reported_without_listing_anything(self):
        config = make_config(self.tmp, {"state": {"history_file": self.history_path},
                                        "weather": {"prerecord": {"temp_max": 10 ** 9}}})
        with mock.patch("chime.weather.prerecord_phrases",
                        side_effect=AssertionError("列挙してはいけない")):
            code, output = self.show(config=config)
        self.assertEqual(code, 1)
        self.assertNotIn("CoverageTooLarge", output)
        self.assertIn("数えられません（気温の作り置き（weather.prerecord.temp_min〜temp_max）が ", output)
        self.assertIn("weather.prerecord の temp_min と temp_max を", output)
        self.assertIn("作り置きの音声を数えられません", output.split("要確認:")[1])

    def test_a_huge_sentence_is_reported_by_the_setting_to_fix_not_by_a_class_name(self):
        for override, key in (
                ({"weather": {"enabled": False, "sentence_weather": "{label:>200000000}"}},
                 "weather.sentence_weather"),
                ({"weather": {"open_meteo": {"locations": [
                    {"label": "あ" * 100_000, "latitude": 35.0, "longitude": 135.0}]}}},
                 "weather.open_meteo.locations")):
            with self.subTest(key=key), mock.patch(
                    "chime.weather.prerecord_phrases", side_effect=AssertionError("列挙してはいけない")):
                config = make_config(self.tmp, dict(override, state={"history_file": self.history_path}))
                code, output = self.show(config=config)
            self.assertEqual(code, 1)
            self.assertNotIn("CoverageTooLarge", output)
            row = [line for line in output.splitlines() if line.startswith("作り置きの音声")][0]
            self.assertIn("数えられません（", row)
            self.assertIn(key, row)
            self.assertIn("要確認", row)

    def test_missing_voices_need_attention(self):
        with tempfile.TemporaryDirectory() as empty:
            config = make_config(self.tmp, {"state": {"history_file": self.history_path},
                                            "tts": {"prerecorded_dir": empty}})
            code, output = self.show(config=config)
        self.assertEqual(code, 1)
        self.assertIn("138 件中 138 件の声がありません", output)
        self.assertIn("作り置きの音声が 138 件足りません", output)

    def test_without_prerecorded_in_the_engines_no_voice_is_used_and_it_needs_attention(self):
        """放送が作り置きを引かない設定は、声がディスクにそろっていても 0 件。起動のログ（0/138）と同じ。"""
        for engines in (["voicevox"], [], "", {}, ["open_jtalk"]):
            with self.subTest(engines=engines):
                config = make_config(self.tmp, {"state": {"history_file": self.history_path},
                                                "tts": {"engines": engines}})
                code, output = self.show(config=config)
                self.assertEqual(code, 1, output)
                self.assertNotIn("すべてそろっています", output)
                [row] = [line for line in output.splitlines() if line.startswith("作り置きの音声")]
                self.assertIn("138 件中 138 件の声がありません", row)
                self.assertIn("tts.engines に prerecorded が無いため", row)
                self.assertIn("要確認", row)
                reasons = output.split("要確認:")[1]
                self.assertIn('tts.engines に "prerecorded" が無いため', reasons)
                self.assertIn("Pi では読み上げがすべて無音になります", reasons)
                self.assertNotIn("件足りません", reasons)  # 同じことを 2 度言わない

    def test_prerecorded_anywhere_in_the_engines_is_enough(self):
        for engines in (["prerecorded"], ["voicevox", "prerecorded"], ["prerecorded", "open_jtalk"]):
            with self.subTest(engines=engines):
                config = make_config(self.tmp, {"state": {"history_file": self.history_path},
                                                "tts": {"engines": engines}})
                code, output = self.show(config=config)
                self.assertEqual(code, 0, output)
                self.assertIn("138 件すべてそろっています", output)

    def test_the_count_without_prerecorded_is_what_the_startup_log_counts(self):
        """``--status`` と起動のログが、同じ設定で同じ数を言う（どちらも ``TTSService.prerecorded_lookup``）。"""
        from chime import phrases
        from chime.app import ChimeApp
        config = make_config(self.tmp, {"state": {"history_file": self.history_path},
                                        "tts": {"engines": ["voicevox"]}})
        app = ChimeApp(config, backend="mock", dry_run=True)
        logged = phrases.coverage(config, app.tts.prerecorded_lookup)
        collected = collect_status(app, FakeRun(), True, "v")
        self.assertEqual((collected.coverage.total, len(collected.coverage.missing)),
                         (logged.total, len(logged.missing)))
        self.assertEqual(len(logged.missing), logged.total)
        self.assertFalse(collected.prerecorded_enabled)

    def test_the_apps_own_tts_is_used_instead_of_building_another(self):
        """作り直すと、「未知のエンジン」の警告が起動のログに続いてもう一度出る。"""
        from chime.app import ChimeApp
        config = make_config(self.tmp, {"state": {"history_file": self.history_path},
                                        "tts": {"engines": ["prerecorded", "open_jtalk"]}})
        app = ChimeApp(config, backend="mock", dry_run=True)
        with mock.patch("chime.status.TTSService", side_effect=AssertionError("作り直してはいけない")):
            collected = collect_status(app, FakeRun(), True, "v")
        self.assertTrue(collected.prerecorded_enabled)
        self.assertTrue(collected.coverage.ok)

    def test_the_disk_is_not_scanned_when_the_broadcast_would_not_look_at_it(self):
        config = make_config(self.tmp, {"state": {"history_file": self.history_path},
                                        "tts": {"engines": ["voicevox"]}})
        with mock.patch("chime.status.prerecorded_coverage", side_effect=AssertionError("読んではいけない")):
            code, _ = self.show(config=config)
        self.assertEqual(code, 1)

    def test_a_degraded_broadcast_is_noted_even_though_it_counts_as_a_success(self):
        # 時刻アナウンスもひとことも入れられず、最小の内容で鳴らした（結果は ok のまま）。
        self.add_history(played=1, total=1, degraded=True)
        code, output = self.show()
        [line] = [line for line in output.split("直近の放送（新しい順）")[1].split("次の予定")[0].splitlines()
                  if line.strip()]
        self.assertIn("成功", line)
        self.assertIn("簡易の内容で放送", line)
        self.assertEqual(code, 1, output)
        reasons = output.split("要確認:")[1]
        self.assertIn("直近の放送は、内容を組み立てられず簡易の内容で鳴りました", reasons)
        self.assertIn("journalctl -u campus_chime.service", reasons)

    def test_a_broadcast_that_was_not_degraded_has_no_note(self):
        self.add_history(played=3, total=3, degraded=False)
        code, output = self.show()
        self.assertEqual(code, 0, output)
        self.assertNotIn("簡易の内容", output)

    def test_only_the_latest_broadcast_decides_whether_degraded_needs_attention(self):
        self.add_history(at=FRIDAY - timedelta(hours=2), played=1, total=1, degraded=True)
        self.add_history(at=FRIDAY - timedelta(hours=1), played=3, total=3)
        code, output = self.show()
        self.assertEqual(code, 0, output)
        self.assertIn("簡易の内容で放送", output)  # 表示はする

    def test_a_degraded_failure_gives_one_reason(self):
        self.add_history(played=0, total=1, degraded=True)
        code, output = self.show()
        reasons = output.split("要確認:")[1].strip().splitlines()
        self.assertEqual(code, 1)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("直近の放送が失敗しています", reasons[0])
        self.assertIn("簡易の内容で放送", output)

    def test_a_real_degraded_broadcast_is_seen_in_the_status(self):
        """組み立てに失敗して最小の内容で鳴らした放送が、履歴を通して ``--status`` に出る。"""
        from chime.app import ChimeApp
        app = ChimeApp(self.config, backend="mock")
        event = app.scheduler.upcoming(FRIDAY, 3)[0]
        app.builder.build = mock.Mock(side_effect=RuntimeError("組み立ての不具合"))
        with logs_enabled(), self.assertLogs("chime.app", level="ERROR"):
            plan = app._build_plan(event)
        self.assertTrue(plan.degraded)
        with mock.patch("time.sleep"):
            outcome = app.play_with_result(plan)
        app._record_history(event, plan, outcome)
        self.assertEqual(History(self.history_path).recent(1)[0]["result"], "ok")
        out = io.StringIO()
        code = run_status(make_app(self.config), out=out, run=FakeRun(), production=True, version="v")
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("成功  簡易の内容で放送", out.getvalue())

    def test_the_voice_count_is_shown_when_all_are_there(self):
        _, output = self.show()
        self.assertIn("138 件すべてそろっています", output)

    def test_a_failure_while_counting_voices_is_reported_without_crashing(self):
        with mock.patch("chime.status.prerecorded_coverage", side_effect=ValueError("壊れた")):
            code, output = self.show()
        self.assertEqual(code, 1)
        self.assertIn("数えられません（ValueError: 壊れた）", output)

    def test_it_lists_the_next_three_events(self):
        _, output = self.show()
        events = output.split("次の予定")[1].split("要確認")[0].split("気になる点")[0]
        self.assertEqual(events.count("  - "), 3)
        self.assertIn("時報 2026-10-09 13:00:00", events)

    def test_with_no_history_it_says_so(self):
        _, output = self.show()
        self.assertIn("（記録はまだありません）", output)

    def test_it_lists_the_latest_eight_broadcasts_newest_first(self):
        for number in range(12):
            self.add_history(at=FRIDAY - timedelta(hours=12 - number), key="hourly:10")
        _, output = self.show()
        section = output.split("直近の放送（新しい順）")[1].split("次の予定")[0]
        lines = [line for line in section.splitlines() if line.strip()]
        self.assertEqual(len(lines), 8)
        self.assertTrue(lines[0].strip().startswith("10/09 11:30"), lines[0])
        self.assertTrue(lines[-1].strip().startswith("10/09 04:30"), lines[-1])

    def test_status_calls_both_service_questions_and_ntp_exactly_once(self):
        run = FakeRun()
        run_status(make_app(self.config), out=io.StringIO(), run=run, production=True, version="v")
        self.assertEqual(sorted(call[0] for call in run.calls), sorted([NTP, IS_ACTIVE, IS_ENABLED]))

    def test_the_reasons_are_listed_together(self):
        self.add_history(played=0, total=3)
        code, output = self.show({**HEALTHY_ANSWERS, NTP: "no\n", IS_ACTIVE: "inactive\n"},
                                 backend="mock")
        reasons = output.split("要確認:")[1].strip().splitlines()
        self.assertEqual(code, 1)
        self.assertEqual(len(reasons), 4)

    def test_flagged_rows_are_marked_on_the_row_itself(self):
        _, output = self.show({**HEALTHY_ANSWERS, NTP: "no\n"})
        [row] = [line for line in output.splitlines() if line.startswith("時刻の同期")]
        self.assertTrue(row.endswith("← 要確認"), row)


class NoWriteTest(StatusCase):
    def snapshot(self):
        state = {}
        for directory, names, files in os.walk(self.tmp):
            for name in names + files:
                path = os.path.join(directory, name)
                stat = os.lstat(path)
                state[os.path.relpath(path, self.tmp)] = (stat.st_mtime_ns, stat.st_size)
        return state

    def test_status_writes_nothing_when_there_is_no_history(self):
        before = self.snapshot()
        self.show()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.history_path))

    def test_status_leaves_an_existing_history_untouched(self):
        self.add_history()
        before = self.snapshot()
        self.show()
        self.assertEqual(self.snapshot(), before)

    def test_status_never_creates_the_cache_folder(self):
        config = make_config(self.tmp, {"state": {"history_file": os.path.join(self.tmp, "cache", "h.jsonl"),
                                                  "file": os.path.join(self.tmp, "cache", "s.json")}})
        self.show(config=config)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cache")))


class DescribeEntryTest(unittest.TestCase):
    def entry(self, **overrides):
        values = dict(at=datetime(2026, 10, 9, 16, 57, tzinfo=TZ), key="closing", kind="closing",
                      played=2, total=2)
        values.update(overrides)
        return make_entry(**values)

    def test_a_successful_closing_broadcast(self):
        line = describe_entry(self.entry(), TZ)
        self.assertEqual(line, "10/09 16:57  閉館放送  成功")

    def test_kinds_and_results_have_japanese_names(self):
        self.assertIn("時報", describe_entry(self.entry(kind="hourly"), TZ))
        self.assertIn("失敗", describe_entry(self.entry(played=0), TZ))
        self.assertIn("一部のみ", describe_entry(self.entry(played=1), TZ))
        self.assertIn("エラー", describe_entry(self.entry(error="x"), TZ))

    def test_columns_line_up_across_kinds_and_results(self):
        starts = set()
        for kind in ("hourly", "closing"):
            for played, label in ((0, "失敗"), (1, "一部のみ"), (2, "成功")):
                line = describe_entry(self.entry(kind=kind, played=played), TZ)
                starts.add(status.display_width(line[:line.index(label)]))
        self.assertEqual(starts, {23})

    def test_the_time_is_shown_in_the_given_zone(self):
        utc = make_entry(at=datetime(2026, 10, 9, 7, 57, tzinfo=ZoneInfo("UTC")), key="closing",
                         kind="closing", played=1, total=1)
        self.assertTrue(describe_entry(utc, TZ).startswith("10/09 16:57"))
        self.assertTrue(describe_entry(utc, None).startswith("10/09 07:57"))

    def test_at_most_three_silent_texts_are_named(self):
        entry = self.entry(played=1, total=6, silent=["あ。", "い。", "う。", "え。", "お。"])
        line = describe_entry(entry, TZ)
        self.assertIn("無音: 「あ。」「い。」「う。」（ほか 2 件）", line)
        self.assertNotIn("え。", line)

    def test_a_broken_entry_does_not_raise(self):
        for broken in ({}, {"at": 5}, {"at": "yesterday", "kind": 3, "result": None},
                       {"at": "2026-10-09T16:57:00+09:00", "silent": "not a list"},
                       {"kind": "hourly", "result": "ok", "silent": [None, 1]}):
            self.assertIsInstance(describe_entry(broken, TZ), str)

    def test_extreme_timestamps_are_marked_as_unknown_and_never_raise(self):
        # 変換すると年が範囲を外れる時刻は OverflowError になる（人が書き換えた履歴）。
        for at in ("9999-12-31T23:59:59+00:00", "0001-01-01T00:00:00+09:00",
                   "0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59-23:59"):
            for zone in (TZ, None):
                with self.subTest(at=at, zone=zone):
                    line = describe_entry({"at": at, "kind": "hourly", "result": "ok"}, zone)
                    self.assertIsInstance(line, str)
        self.assertEqual(
            describe_entry({"at": "9999-12-31T23:59:59+00:00", "kind": "hourly", "result": "ok"}, TZ),
            "（時刻不明）  時報      成功")

    def test_an_entry_that_is_not_a_mapping_is_shown_as_unreadable(self):
        for value in (None, "文字列", ["リスト"], 42):
            with self.subTest(value=value):
                self.assertEqual(describe_entry(value, TZ), "（読めない記録）")

    def test_an_entry_without_a_missing_field_is_shown_as_before(self):
        entry = self.entry()
        del entry["missing"]
        self.assertEqual(describe_entry(entry, TZ), "10/09 16:57  閉館放送  成功")

    def test_missing_parts_are_named_before_the_silent_texts(self):
        entry = self.entry(played=1, total=2, missing=["閉館アナウンス"], silent=["あ。"])
        line = describe_entry(entry, TZ)
        self.assertEqual(
            line, "10/09 16:57  閉館放送  一部のみ  欠けた音源: 「閉館アナウンス」  無音: 「あ。」")

    def test_the_error_comes_first_then_the_missing_parts(self):
        entry = self.entry(played=0, total=0, error="OSError: x", missing=["蛍の光"])
        self.assertTrue(describe_entry(entry, TZ).endswith(
            "エラー: OSError: x  欠けた音源: 「蛍の光」"))

    def test_at_most_three_missing_parts_are_named(self):
        entry = self.entry(played=1, total=1, missing=["あ", "い", "う", "え", "お"])
        line = describe_entry(entry, TZ)
        self.assertIn("欠けた音源: 「あ」「い」「う」（ほか 2 件）", line)
        self.assertNotIn("「え」", line)

    def test_exactly_three_missing_parts_have_no_more_note(self):
        line = describe_entry(self.entry(played=1, total=1, missing=["あ", "い", "う"]), TZ)
        self.assertIn("欠けた音源: 「あ」「い」「う」", line)
        self.assertNotIn("ほか", line)

    def test_a_missing_field_that_is_not_a_list_is_ignored(self):
        for value in ("閉館アナウンス", None, 5, {"a": 1}, []):
            with self.subTest(value=value):
                line = describe_entry({"at": "2026-10-09T16:57:00+09:00", "kind": "closing",
                                       "result": "ok", "missing": value}, TZ)
                self.assertNotIn("欠けた音源", line)

    def test_exactly_three_silent_texts_have_no_more_note(self):
        entry = self.entry(played=1, total=4, silent=["あ。", "い。", "う。"])
        self.assertNotIn("ほか", describe_entry(entry, TZ))

    def test_a_silent_field_that_is_a_string_is_ignored(self):
        line = describe_entry({"at": "2026-10-09T16:57:00+09:00", "kind": "hourly",
                               "result": "ok", "silent": "abc"}, TZ)
        self.assertNotIn("無音", line)

    def test_a_description_is_always_one_line_that_can_be_printed(self):
        # 改行・端末の制御文字・端末に出せない文字（孤立したサロゲート）は空白にする。
        entry = {"at": "2026-10-09T16:57:00+09:00", "kind": "hourly\nx", "result": "ok\x1b[31m",
                 "error": "1 行目\n2 行目\r\n3 行目\ud800", "silent": ["あ\n。"],
                 "missing": ["い\t。"]}
        line = describe_entry(entry, TZ)
        self.assertNotIn("\n", line)
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\t", line)
        self.assertIn("エラー: 1 行目 2 行目  3 行目 ", line)
        self.assertIn("「あ 。」", line)
        self.assertIn("「い 。」", line)
        line.encode("utf-8")  # 孤立したサロゲートが残っていれば UnicodeEncodeError

    def test_fullwidth_and_other_visible_characters_are_kept(self):
        entry = self.entry(played=1, total=2, silent=["全角　空白と記号★。"])
        self.assertIn("「全角　空白と記号★。」", describe_entry(entry, TZ))

    def test_whatever_shape_the_entry_has_it_never_raises(self):
        for entry in random_entries(3000):
            for zone in (TZ, None):
                line = describe_entry(entry, zone)
                self.assertIsInstance(line, str, entry)
                self.assertNotIn("\n", line, entry)
                line.encode("utf-8")

    def test_an_unknown_time_is_marked(self):
        self.assertIn("（時刻不明）", describe_entry({"kind": "hourly", "result": "ok"}, TZ))

    def test_unknown_kinds_and_results_are_shown_as_they_are(self):
        line = describe_entry({"at": "2026-10-09T10:00:00+09:00", "kind": "weekly", "result": "odd"}, TZ)
        self.assertIn("weekly", line)
        self.assertIn("odd", line)


class DegradedNoteTest(unittest.TestCase):
    def entry(self, **overrides):
        values = dict(at=datetime(2026, 10, 9, 16, 0, tzinfo=TZ), key="hourly:16", kind="hourly",
                      played=1, total=1)
        values.update(overrides)
        return make_entry(**values)

    def test_the_note_is_shown_for_a_degraded_entry(self):
        self.assertEqual(describe_entry(self.entry(degraded=True), TZ),
                         "10/09 16:00  時報      成功  簡易の内容で放送")

    def test_no_note_otherwise(self):
        for entry in (self.entry(), self.entry(degraded=False)):
            self.assertEqual(describe_entry(entry, TZ), "10/09 16:00  時報      成功")
        old = self.entry()
        del old["degraded"]
        self.assertEqual(describe_entry(old, TZ), "10/09 16:00  時報      成功")

    def test_only_a_true_flag_counts(self):
        for flag in ("true", "yes", 1, 0, None, [], [True], {"a": 1}, "", "簡易"):
            entry = self.entry()
            entry["degraded"] = flag
            with self.subTest(flag=flag):
                self.assertNotIn("簡易の内容", describe_entry(entry, TZ))

    def test_the_note_comes_before_the_missing_parts_and_the_silent_texts(self):
        line = describe_entry(self.entry(degraded=True, missing=["時報音"], silent=["今日は晴れなのだ。"]), TZ)
        self.assertLess(line.index("簡易の内容で放送"), line.index("欠けた音源"))
        self.assertLess(line.index("欠けた音源"), line.index("無音"))

    def test_the_note_follows_the_error(self):
        line = describe_entry(self.entry(degraded=True, played=0, total=0, error="RuntimeError: x"), TZ)
        self.assertLess(line.index("エラー: RuntimeError: x"), line.index("簡易の内容で放送"))


class AttentionTest(unittest.TestCase):
    def status(self, **overrides):
        values = dict(version="v", now=FRIDAY, ntp=True, active="active", enabled="enabled",
                      backend="pygame", production=True, history=[], upcoming=[],
                      coverage=SimpleNamespace(ok=True, missing=[], total=1), zone=TZ)
        values.update(overrides)
        return status.Status(**values)

    def test_nothing_to_report(self):
        self.assertEqual(attention(self.status()), [])

    def test_unknown_facts_are_not_reported(self):
        self.assertEqual(attention(self.status(ntp=None, active=None, enabled=None)), [])

    def test_each_condition_gives_one_reason(self):
        cases = [dict(ntp=False), dict(active="inactive"), dict(backend="mock"),
                 dict(history=[{"result": "failed"}]), dict(history=[{"result": "error"}]),
                 dict(coverage=SimpleNamespace(ok=False, missing=["x"], total=2)),
                 dict(coverage=None)]
        for overrides in cases:
            self.assertEqual(len(attention(self.status(**overrides))), 1, overrides)

    def test_a_missing_part_in_the_latest_broadcast_is_one_reason(self):
        reasons = attention(self.status(history=[{"result": "partial", "missing": ["蛍の光"]}]))
        self.assertEqual(len(reasons), 1)
        self.assertIn("音源ファイルが無くて鳴らせなかった部分があります", reasons[0])

    def test_no_missing_part_is_not_a_reason(self):
        for history in ([{"result": "partial", "missing": []}], [{"result": "partial"}],
                        [{"result": "ok", "missing": "蛍の光"}], [{"result": "ok", "missing": None}],
                        [{"result": "ok"}, {"result": "partial", "missing": ["蛍の光"]}]):
            with self.subTest(history=history):
                self.assertEqual(attention(self.status(history=history)), [])

    def test_a_failed_broadcast_with_missing_parts_is_still_one_reason(self):
        reasons = attention(self.status(
            history=[{"result": "failed", "missing": ["閉館アナウンス", "蛍の光"]}]))
        self.assertEqual(len(reasons), 1)
        self.assertIn("失敗", reasons[0])

    def test_whatever_the_latest_entry_looks_like_the_status_can_be_shown(self):
        for entry in random_entries(500, seed=7):
            found = self.status(history=[entry, entry])
            reasons = attention(found)
            self.assertIsInstance(found.last_failed, bool)
            self.assertIsInstance(found.last_missing, bool)
            self.assertIsInstance(found.last_degraded, bool)
            lines = status.render_status(found, reasons)
            "\n".join(lines).encode("utf-8")

    def test_last_missing_is_false_without_history(self):
        self.assertIs(self.status().last_missing, False)

    def test_a_degraded_latest_broadcast_is_one_reason(self):
        reasons = attention(self.status(history=[{"result": "ok", "degraded": True}]))
        self.assertEqual(len(reasons), 1)
        self.assertIn("簡易の内容で鳴りました", reasons[0])

    def test_degraded_is_not_a_reason_unless_it_is_true_and_the_latest(self):
        for history in ([{"result": "ok", "degraded": False}], [{"result": "ok"}],
                        [{"result": "ok", "degraded": "true"}], [{"result": "ok", "degraded": 1}],
                        [{"result": "ok"}, {"result": "ok", "degraded": True}], []):
            with self.subTest(history=history):
                self.assertEqual(attention(self.status(history=history)), [])

    def test_a_degraded_failure_or_missing_part_is_still_one_reason(self):
        for entry in ({"result": "failed", "degraded": True},
                      {"result": "partial", "missing": ["時報音"], "degraded": True}):
            with self.subTest(entry=entry):
                self.assertEqual(len(attention(self.status(history=[entry]))), 1)

    def test_last_degraded_is_false_without_history(self):
        self.assertIs(self.status().last_degraded, False)

    def test_engines_without_prerecorded_are_one_reason_instead_of_the_missing_count(self):
        missing = SimpleNamespace(ok=False, missing=["x", "y"], total=2)
        reasons = attention(self.status(coverage=missing, prerecorded_enabled=False))
        self.assertEqual(len(reasons), 1)
        self.assertIn('tts.engines に "prerecorded" が無い', reasons[0])
        reasons = attention(self.status(coverage=missing))
        self.assertEqual(len(reasons), 1)
        self.assertIn("2 件足りません", reasons[0])

    def test_engines_without_prerecorded_and_an_uncountable_set_are_two_reasons(self):
        reasons = attention(self.status(coverage=None, prerecorded_enabled=False))
        self.assertEqual(len(reasons), 2)

    def test_prerecorded_is_enabled_unless_said_otherwise(self):
        self.assertIs(self.status().prerecorded_enabled, True)

    def test_activating_counts_as_not_running(self):
        self.assertEqual(len(attention(self.status(active="activating"))), 1)

    def test_a_non_failed_result_is_not_a_reason(self):
        for result in ("ok", "partial", "something-new"):
            self.assertEqual(attention(self.status(history=[{"result": result}])), [], result)


class CollectStatusTest(StatusCase):
    def test_the_player_is_chosen_before_anything_is_printed(self):
        """バックエンドの選択はログを出す。表示の途中に混ざらないよう、集める段階で済ませる。"""
        calls = []
        app = make_app(self.config)
        app.player = SimpleNamespace(name="pygame")
        original = status.prerecorded_coverage

        def coverage(config):
            calls.append("coverage")
            return original(config)

        with mock.patch("chime.status.prerecorded_coverage", coverage):
            collected = collect_status(app, FakeRun(), True, "v")
        self.assertEqual(collected.backend, "pygame")
        self.assertEqual(calls, ["coverage"])
        self.assertEqual(collected.zone, TZ)

    def test_history_comes_from_the_configured_file(self):
        self.add_history()
        collected = collect_status(make_app(self.config), FakeRun(), True, "v")
        self.assertEqual(len(collected.history), 1)
        self.assertEqual(collected.history[0]["key"], "hourly:11")


class BroadcastWindowTest(unittest.TestCase):
    def events(self, day=FRIDAY.date()):
        scheduler = make_scheduler(FRIDAY)
        return {event.key: event for event in scheduler.events_for_date(day)}

    def test_an_hourly_window_runs_from_10_seconds_before_preparing_to_90_seconds_after_playing(self):
        event = self.events()["hourly:13"]
        start, end = broadcast_window(event)
        self.assertEqual(start, event.prepare_at - timedelta(seconds=10))
        self.assertEqual(end, event.play_at + timedelta(seconds=90))
        self.assertEqual(start, datetime(2026, 10, 9, 12, 59, 2, tzinfo=TZ))
        self.assertEqual(end, datetime(2026, 10, 9, 13, 1, 27, tzinfo=TZ))

    def test_a_closing_window_ends_300_seconds_after_playing(self):
        event = self.events()["closing"]
        start, end = broadcast_window(event)
        self.assertEqual(start, datetime(2026, 10, 9, 16, 56, 5, tzinfo=TZ))
        self.assertEqual(end, datetime(2026, 10, 9, 17, 2, 0, tzinfo=TZ))

    def test_an_unknown_kind_gets_the_hourly_window(self):
        moment = FRIDAY
        event = make_event(moment, key="x", kind="odd")
        self.assertEqual(broadcast_window(event)[1], moment + timedelta(seconds=90))


class BusyEventTest(unittest.TestCase):
    def busy(self, moment, settings=None):
        return busy_event(make_scheduler(moment, settings), moment)

    def test_outside_every_window_is_idle(self):
        for moment in (datetime(2026, 10, 9, 9, 0, tzinfo=TZ),
                       datetime(2026, 10, 9, 12, 59, 1, tzinfo=TZ),
                       datetime(2026, 10, 9, 13, 1, 27, tzinfo=TZ),
                       datetime(2026, 10, 9, 17, 2, 0, tzinfo=TZ),
                       datetime(2026, 10, 9, 23, 0, tzinfo=TZ)):
            self.assertIsNone(self.busy(moment), moment)

    def test_the_window_includes_its_start_and_excludes_its_end(self):
        start = datetime(2026, 10, 9, 12, 59, 2, tzinfo=TZ)
        self.assertEqual(self.busy(start)[0].key, "hourly:13")
        self.assertEqual(self.busy(start - timedelta(microseconds=1)), None)
        end = datetime(2026, 10, 9, 13, 1, 27, tzinfo=TZ)
        self.assertEqual(self.busy(end - timedelta(microseconds=1))[0].key, "hourly:13")
        self.assertIsNone(self.busy(end))

    def test_during_a_closing_broadcast(self):
        for moment in (datetime(2026, 10, 9, 16, 56, 5, tzinfo=TZ),
                       datetime(2026, 10, 9, 16, 59, 0, tzinfo=TZ),
                       datetime(2026, 10, 9, 17, 1, 59, tzinfo=TZ)):
            self.assertEqual(self.busy(moment)[0].key, "closing", moment)

    def test_a_weekend_is_idle_even_at_the_usual_time(self):
        self.assertIsNone(self.busy(SATURDAY))

    def test_overlapping_windows_end_with_the_latest(self):
        settings = deep_merge(DEFAULT_CONFIG["schedule"],
                              {"closing": {"hour": 16, "minute": 0}})
        moment = datetime(2026, 10, 9, 16, 0, 10, tzinfo=TZ)
        event, end = self.busy(moment, settings)
        self.assertEqual(event.key, "closing")
        self.assertEqual(end, datetime(2026, 10, 9, 16, 5, 0, tzinfo=TZ))

    def test_a_window_that_crosses_midnight_is_found_from_the_next_day(self):
        settings = deep_merge(DEFAULT_CONFIG["schedule"], {"closing": {"hour": 23, "minute": 59}})
        moment = datetime(2026, 10, 10, 0, 2, 0, tzinfo=TZ)  # 翌日の 0:02（金曜の 23:59 の放送の最中）
        event, end = self.busy(moment, settings)
        self.assertEqual(event.key, "closing")
        self.assertEqual(end, datetime(2026, 10, 10, 0, 4, 0, tzinfo=TZ))

    def test_a_window_that_starts_before_midnight_is_found_from_the_previous_day(self):
        settings = deep_merge(DEFAULT_CONFIG["schedule"],
                              {"hourly": {"start_hour": 0, "end_hour": 0}})
        moment = datetime(2026, 10, 8, 23, 59, 30, tzinfo=TZ)  # 翌金曜 0:00 の時報の準備中
        event, _ = self.busy(moment, settings)
        self.assertEqual(event.key, "hourly:00")


class WaitIdleTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.sleeps = []

    def wait(self, moment, limit=360, settings=None):
        scheduler = make_scheduler(moment, settings)

        def sleep(seconds):
            self.sleeps.append(seconds)
            scheduler.clock["now"] += timedelta(seconds=seconds)

        code = wait_idle(scheduler, limit, sleep=sleep, out=self.out, err=self.err)
        return code, scheduler.clock["now"]

    def test_when_not_near_a_broadcast_it_returns_0_immediately(self):
        code, _ = self.wait(datetime(2026, 10, 9, 9, 0, tzinfo=TZ))
        self.assertEqual(code, 0)
        self.assertEqual(self.sleeps, [])
        self.assertIn("いまは放送の時間帯ではありません。", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_inside_a_window_it_says_what_it_waits_for_and_sleeps_in_small_steps(self):
        code, finished = self.wait(datetime(2026, 10, 9, 13, 0, 30, tzinfo=TZ))
        self.assertEqual(code, 0)
        output = self.out.getvalue()
        self.assertIn("放送の時間帯です: 時報 2026-10-09 13:00:00（再生開始 12:59:57）", output)
        self.assertIn("終わるまであと約 57 秒。最大 360 秒待ちます。", output)
        self.assertEqual(set(self.sleeps), {5.0})
        # 13:01:27 に終わるので、5 秒ずつ眠って 13:01:30 に出る。
        self.assertEqual(len(self.sleeps), 12)
        self.assertEqual(finished, datetime(2026, 10, 9, 13, 1, 30, tzinfo=TZ))
        self.assertIn("放送の時間帯が終わりました（60 秒待ちました）。", output)

    def test_the_closing_broadcast_is_waited_out_within_the_default_limit(self):
        code, _ = self.wait(datetime(2026, 10, 9, 16, 56, 5, tzinfo=TZ))
        self.assertEqual(code, 0)
        self.assertLessEqual(sum(self.sleeps), 360)

    def test_it_times_out_with_1_when_the_window_outlasts_the_limit(self):
        code, _ = self.wait(datetime(2026, 10, 9, 16, 56, 30, tzinfo=TZ), limit=20)
        self.assertEqual(code, 1)
        self.assertEqual(self.sleeps, [5.0, 5.0, 5.0, 5.0])
        self.assertIn("20 秒待ちましたが、放送の時間帯が終わりませんでした: 閉館放送", self.err.getvalue())
        self.assertNotIn("終わりました", self.out.getvalue())

    def test_the_last_step_is_shortened_so_the_limit_is_not_exceeded(self):
        code, _ = self.wait(datetime(2026, 10, 9, 16, 56, 30, tzinfo=TZ), limit=12)
        self.assertEqual(code, 1)
        self.assertEqual(self.sleeps, [5.0, 5.0, 2.0])

    def test_a_huge_limit_still_finishes_a_closing_broadcast(self):
        code, _ = self.wait(datetime(2026, 10, 9, 16, 56, 5, tzinfo=TZ), limit=100000)
        self.assertEqual(code, 0)
        self.assertLessEqual(sum(self.sleeps), status.WAIT_IDLE_MAX)

    def test_a_clamped_limit_is_enforced_when_the_window_never_ends(self):
        scheduler = make_scheduler(datetime(2026, 10, 9, 16, 56, 30, tzinfo=TZ))
        # 時計が進まない（眠っても現在時刻が変わらない）なら、上限で諦める。
        slept = []
        code = wait_idle(scheduler, 100000, sleep=slept.append, out=self.out, err=self.err)
        self.assertEqual(code, 1)
        self.assertEqual(sum(slept), 360)

    def test_a_zero_limit_checks_once_without_sleeping(self):
        code, _ = self.wait(datetime(2026, 10, 9, 13, 0, 30, tzinfo=TZ), limit=0)
        self.assertEqual(code, 1)
        self.assertEqual(self.sleeps, [])
        code, _ = self.wait(datetime(2026, 10, 9, 9, 0, tzinfo=TZ), limit=0)
        self.assertEqual(code, 0)

    def test_a_negative_limit_is_treated_as_zero(self):
        code, _ = self.wait(datetime(2026, 10, 9, 13, 0, 30, tzinfo=TZ), limit=-5)
        self.assertEqual(code, 1)
        self.assertEqual(self.sleeps, [])

    def test_a_busy_window_is_announced_only_once(self):
        self.wait(datetime(2026, 10, 9, 13, 0, 30, tzinfo=TZ))
        self.assertEqual(self.out.getvalue().count("放送の時間帯です"), 1)

    def test_a_negative_limit_is_shown_as_zero(self):
        self.wait(datetime(2026, 10, 9, 13, 0, 30, tzinfo=TZ), limit=-5)
        self.assertIn("0 秒待ちました", self.err.getvalue())
        self.assertNotIn("-5", self.err.getvalue() + self.out.getvalue())

    def test_the_default_limit_is_360(self):
        self.assertEqual(status.WAIT_IDLE_MAX, 360)
        self.assertEqual(wait_idle.__defaults__[0], 360)

    def test_it_reads_the_clock_again_after_each_step(self):
        """眠っている間に NTP で時刻が飛んでも、その時点の時刻で判定し直す。"""
        scheduler = make_scheduler(datetime(2026, 10, 9, 13, 0, 30, tzinfo=TZ))
        jumps = iter([datetime(2026, 10, 9, 13, 0, 35, tzinfo=TZ), datetime(2026, 10, 9, 15, 30, 0, tzinfo=TZ)])

        def sleep(seconds):
            scheduler.clock["now"] = next(jumps)

        code = wait_idle(scheduler, 360, sleep=sleep, out=self.out, err=self.err)
        self.assertEqual(code, 0)
        self.assertIn("放送の時間帯が終わりました", self.out.getvalue())

    def test_it_uses_time_sleep_when_no_sleep_is_given(self):
        scheduler = make_scheduler(datetime(2026, 10, 9, 13, 1, 25, tzinfo=TZ))
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)
            scheduler.clock["now"] += timedelta(seconds=seconds)

        with mock.patch("chime.status.time.sleep", fake_sleep):
            code = wait_idle(scheduler, 360, out=self.out, err=self.err)
        self.assertEqual(code, 0)
        self.assertEqual(slept, [5.0])

    def test_it_prints_to_standard_streams_by_default(self):
        scheduler = make_scheduler(datetime(2026, 10, 9, 9, 0, tzinfo=TZ))
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(wait_idle(scheduler, 5), 0)
        self.assertIn("放送の時間帯ではありません", stdout.getvalue())

    def test_it_writes_nothing_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                self.wait(datetime(2026, 10, 9, 13, 0, 30, tzinfo=TZ))
            finally:
                os.chdir(cwd)
            self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
