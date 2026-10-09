"""設定の検査（``chime/configcheck.py``）のテスト。

検査の方針は、``sanitized()`` が「前の版が例外なく動かしていた設定の動作を変えない」こと。
そのため重大度は、実際のランタイム（``ChimeApp``・``Scheduler``・放送の組み立て・再生・ログ）が
その値でどうなるかで決める。このファイルは、その分類を実ランタイムで確かめる（通信はしない）。

- error（``sanitized()`` が既定値に置き換える）: 素の値でランタイムが壊れる（例外・CPU の空回り・
  OS のローカル時刻・ログが失われる）ことを実ランタイムで示し、置き換えた後は動くことを示す。
- warning（置き換えない）: 素の値でもランタイムが動くことを示し、``sanitized()`` が設定を
  一切変えないことを示す。

実ランタイムに通す仕掛けは :func:`run_runtime`。時計は :data:`FIXED_NOW` に固定する。
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import tracemalloc
import types
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from tests.support import ASSETS_DIR, REPO_ROOT, block_network, logs_enabled

from chime import audio, configcheck, phrases, timesignal, weather
from chime.app import ChimeApp
from chime.audio import create_player
from chime.cli import logging_settings
from chime.config import (DEFAULT_CONFIG, EXAMPLE_CONFIG_PATH, REMOVED_KEYS, Config,
                          deep_merge, load_config, redundant_keys)
from chime.configcheck import (ERROR, INFO, WARNING, Finding, _TEMPLATE_FIELDS, check_config,
                               sanitized, validate, walk_overrides)
from chime.logsetup import setup_logging
from chime.scheduler import MAX_LOOKAHEAD_DAYS, Scheduler
from chime.tts import TTSError, TTSService

_JAPANESE = re.compile("[ぁ-んァ-ヶ一-龥]")
TZ = ZoneInfo("Asia/Tokyo")

#: 検査と実ランタイムの両方に使う「いま」（金曜の昼）。日付に依存する判定（日時の範囲を出るか）を、
#: いつ実行しても同じ答えにする。
FIXED_NOW = datetime(2026, 10, 9, 12, 0, 0)

def make_config(override=None, **kwargs):
    """既定値に ``override`` を重ねた設定（読み込んだファイルは無い）。"""
    return Config(deep_merge(DEFAULT_CONFIG, override or {}), base_dir="/opt/chime", **kwargs)


def levels_of(override, level):
    return [finding for finding in validate(make_config(override)) if finding.level == level]


def errors_of(override):
    return levels_of(override, ERROR)


def warnings_of(override):
    return levels_of(override, WARNING)


def keys_of(findings):
    return [finding.key for finding in findings]


def nested(dotted_key, value):
    """``"a.b"``, 1 → ``{"a": {"b": 1}}``。"""
    for part in reversed(dotted_key.split(".")):
        value = {part: value}
    return value


def default_at(dotted_key):
    return Config(DEFAULT_CONFIG).get(dotted_key)


# ---------------------------------------------------------------------------
# 実際のランタイムに通す
# ---------------------------------------------------------------------------
def _fixed_moments():
    return (FIXED_NOW - timedelta(days=1),
            FIXED_NOW + timedelta(days=MAX_LOOKAHEAD_DAYS + 1))


def _fixed_now(self):
    return FIXED_NOW.replace(tzinfo=self.tzinfo)


_SANDBOX = []


def sandbox_dir():
    """放送を組み立てて鳴らすための作業フォルダ。

    同梱の音源・声・ひとことへのリンクと、生成済みの時報音を置く（時報音は、無ければ放送の
    たびに合成するので、先に作っておく）。このモジュールのテストが全部終わったら消す。
    """
    if not _SANDBOX:
        root = tempfile.mkdtemp(prefix="configcheck-")
        unittest.addModuleCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "assets", "generated"))
        for name in ("voice", "announce.wav", "hotaru.mp3", "quotes.json"):
            os.symlink(os.path.join(ASSETS_DIR, name), os.path.join(root, "assets", name))
        timesignal.generate_time_signal(
            os.path.join(root, "assets", "generated", "time_signal.wav"),
            DEFAULT_CONFIG["time_signal"], DEFAULT_CONFIG["audio"]["mixer"])
        _SANDBOX.append(root)
    return _SANDBOX[0]


def socket_timeout_urlopen(request, *args, timeout=None, **kwargs):
    """``urlopen`` の代わり。通信はしないが、待ち時間の設定は本物のソケットと同じ拒み方をする。

    巨大な値（負も）は ``socket.settimeout`` が ``OverflowError`` にする。``urlopen`` はそれを
    通信の失敗に直さないので、VOICEVOX ENGINE の疎通確認と合成がそのまま例外になる。
    """
    with contextlib.closing(socket.socket()) as sock:
        sock.settimeout(timeout)
    raise urllib.error.URLError("テスト中は通信しません")


def fake_sleep(seconds):
    """``time.sleep`` と同じ拒み方をして、待たない。"""
    seconds = float(seconds)
    if seconds != seconds:
        raise ValueError("Invalid value NaN (not a number)")
    if seconds < 0:
        raise ValueError("sleep length must be non-negative")
    if seconds > 9223372036:
        raise OverflowError("timestamp out of range for platform time_t")


class FakeMixer(object):
    """``pygame.mixer``。引数が整数でなければ、本物と同じく例外にする。"""

    class music(object):
        @staticmethod
        def load(path):
            pass

        @staticmethod
        def play(*args, **kwargs):
            pass

        @staticmethod
        def get_busy():
            return False

        @staticmethod
        def stop():
            pass

    @staticmethod
    def init(**kwargs):
        for name, value in kwargs.items():
            if not isinstance(value, int):
                raise TypeError("{0} は整数でなければなりません".format(name))

    @staticmethod
    def quit():
        pass

    @staticmethod
    def get_init():
        return (44100, -16, 2)


FAKE_PYGAME = types.SimpleNamespace(mixer=FakeMixer)


def count_wakeups(settings, seconds=1.0, cap=200):
    """待機ループが、``seconds`` 秒の待機のあいだに、停止要求を確かめに起きる回数。

    時計は ``wait`` に渡された秒数だけ進める（実時間では待たない）。``cap`` 回を超えても
    終わらないなら（待機の秒数が 0 以下だと、時計が進まない）``RuntimeError``。
    """
    clock = {"now": FIXED_NOW.replace(tzinfo=TZ), "calls": 0}

    class Stop(threading.Event):
        def wait(self, timeout=None):
            clock["calls"] += 1
            if clock["calls"] > cap:
                raise RuntimeError("待機ループが空回りしている")
            clock["now"] += timedelta(seconds=max(timeout or 0, 0))
            return False

    scheduler = Scheduler(settings, TZ, 3.0, clock=lambda: clock["now"])
    scheduler.sleep_until(clock["now"] + timedelta(seconds=seconds), Stop())
    return clock["calls"]


@contextlib.contextmanager
def isolated_logging():
    """ルートロガーを空にして、ログを拾えるようにする（抜けると元に戻す）。"""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    root.handlers[:] = []
    try:
        with logs_enabled():
            yield root
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def logging_outcome(config):
    """起動時と同じ手順でログを設定し、1 件出してみる。``(ルートの水準, 出力の失敗, 出力)``。"""
    level, log_format = logging_settings(config)
    stream = io.StringIO()
    failures = []
    with isolated_logging() as root:
        setup_logging(level, log_format, str(config.get("timezone", "")), stream=stream)
        for handler in root.handlers:
            handler.handleError = lambda record: failures.append(sys.exc_info()[1])
        logging.getLogger("chime.truth").warning("メッセージ")
        effective = root.level
    return effective, failures, stream.getvalue()


class Run(object):
    """設定を実際のランタイムに通した結果。"""

    def __init__(self):
        #: 壊れた段階 → 例外の名前（壊れなければ空）
        self.failures = {}
        self.app = None
        #: 組み立てた放送（"hourly:10"・"hourly:12"・"hourly:16"・"closing"）
        self.plans = {}
        #: 日付（ISO）→ その日の予定のキー
        self.events = {}


def run_runtime(config):
    """``config`` を、実際の ``ChimeApp``・``Scheduler``・放送の組み立て・再生・ログに通す。

    通信は呼び出し側が ``block_network`` で塞いでおく。時計は :data:`FIXED_NOW` に固定する。
    壊れた段階を ``Run.failures`` に集める。段階は次のとおり。

    ``ログ`` ログの設定と 1 件の出力（書式・水準の名前）。 ``再生方式`` 再生方式の選択（Pi と同じく
    pygame を選べる環境として）。 ``起動`` ``ChimeApp`` の組み立て。 ``時計`` 設定のタイムゾーンで
    動いているか。 ``起動時の記録`` 起動時のログ（再生方式・作り置きの数え上げ）。
    ``VOICEVOX:疎通確認`` / ``VOICEVOX:合成`` VOICEVOX ENGINE への通信（待ち時間の設定は本物の
    ソケットと同じ拒み方で、通信はしない）。 ``予定`` 予定の計算。
    ``待機`` 待機ループが空回りしていないか。 ``放送:時報`` / ``放送:閉館`` 放送の組み立て。
    ``再生:mock`` / ``再生:pygame`` / ``再生:command`` 3 つの再生方式での再生。
    """
    run = Run()

    def attempt(stage, func):
        try:
            return func()
        except Exception as exc:
            run.failures[stage] = type(exc).__name__
            return None

    cfg = Config(copy.deepcopy(config.data), base_dir=sandbox_dir())
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(ChimeApp, "now", _fixed_now))
        stack.enter_context(mock.patch.object(audio.time, "sleep", fake_sleep))
        # 時報音の保存先が空だと ``wave.open`` が開けず、標準ライブラリの後始末が stderr に書く
        stack.enter_context(mock.patch.object(sys, "unraisablehook", lambda unraisable: None))

        def logging_step():
            effective, failures, _ = logging_outcome(cfg)
            if failures:
                raise failures[0]
            name = str(logging_settings(cfg)[0]).upper()
            if not isinstance(logging.getLevelName(name), int):
                raise ValueError("ログの水準の名前ではない（黙って INFO で動く）")
        attempt("ログ", logging_step)

        def player_choice_step():
            with mock.patch.object(audio.env, "is_production_linux", return_value=True), \
                    mock.patch.object(audio.PygamePlayer, "available", return_value=True):
                create_player(cfg.section("audio"), None)
        attempt("再生方式", player_choice_step)

        app = attempt("起動", lambda: ChimeApp(cfg, backend="mock", dry_run=True))
        if app is None:
            return run
        run.app = app
        app.quotes.rng.seed(0)  # ひとこと（乱数で選ぶ）を、同じ設定なら毎回同じにする
        if app.tzinfo is None:
            run.failures["時計"] = "OS のローカル時刻"
        attempt("起動時の記録", app.log_environment)

        def voicevox_step(operation):
            def step():
                with mock.patch("urllib.request.urlopen", socket_timeout_urlopen):
                    for engine in app.tts.engines:
                        if engine.name == "voicevox":
                            operation(engine)
            return step

        def synthesize(engine):
            try:
                engine.synthesize("テスト", os.path.join(sandbox_dir(), "voicevox-test.wav"))
            except TTSError:
                pass  # 通信できない（ふつうの失敗。放送は他のエンジンへ進む）

        attempt("VOICEVOX:疎通確認", voicevox_step(lambda engine: engine.available()))
        attempt("VOICEVOX:合成", voicevox_step(synthesize))

        def schedule_step():
            now = app.now()
            app.scheduler.upcoming(now, limit=12)
            app.scheduler.next_event(now)
            for offset in range(8):
                day = (now + timedelta(days=offset)).date()
                run.events[day.isoformat()] = [e.key for e in app.scheduler.events_for_date(day)]
        attempt("予定", schedule_step)

        def spin_step():
            count_wakeups(cfg.section("schedule"))  # 空回りなら RuntimeError
        attempt("待機", spin_step)

        for hour in (10, 12, 16):
            run.plans["hourly:{0}".format(hour)] = attempt(
                "放送:時報", lambda hour=hour: app.builder.build_hourly(hour))
        run.plans["closing"] = attempt("放送:閉館", app.builder.build_closing)

        segments = []
        for name in ("hourly:10", "closing"):
            if run.plans.get(name) is not None:
                segments.extend(run.plans[name].segments)
        section = cfg.section("audio")
        attempt("再生:mock", lambda: audio.MockPlayer(section).play(segments))

        def pygame_step():
            with mock.patch.object(audio, "pygame", FAKE_PYGAME):
                audio.PygamePlayer(section).play(segments)
        attempt("再生:pygame", pygame_step)

        def command_step():
            # 実機の ``aplay`` と ``mpg123`` だけが見つかる。コマンドの名前が違えば見つからない
            with mock.patch.object(audio.env, "has_command",
                                   side_effect=lambda name: name in ("aplay", "mpg123")), \
                    mock.patch.object(audio.subprocess, "run",
                                      return_value=types.SimpleNamespace(returncode=0)):
                try:
                    audio.CommandPlayer(section).play(segments)
                except audio.PlaybackError as exc:
                    if "未設定" not in str(exc):
                        raise
                    # 外部コマンドが未設定の拡張子（audio ごと失った設定）。pygame が無い環境だけの話
        attempt("再生:command", command_step)
    return run


def runtime_failures(config):
    return run_runtime(config).failures


class RuntimeTestCase(unittest.TestCase):
    """通信を塞ぎ、日付に依存する判定を固定した時計で行う。"""

    def setUp(self):
        super().setUp()
        block_network(self)
        patcher = mock.patch.object(configcheck, "_reference_moments", _fixed_moments)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- 分類の確かめ方 --------------------------------------------------------
    def assert_breaks(self, override, key, stage):
        """error: 素の値でランタイムが ``stage`` で壊れ、``sanitized()`` の後は動く。

        直した設定を返す。
        """
        config = make_config(override)
        before = runtime_failures(config)
        self.assertIn(stage, before, "ランタイムが壊れるはず: {0} → {1}".format(configcheck._show(override, 120), before))
        levels = [f.level for f in validate(config) if f.key == key]
        self.assertIn(ERROR, levels, "error になるはず: {0}".format(configcheck._show(override, 120)))
        fixed, errors = sanitized(config)
        self.assertIn(key, keys_of(errors))
        self.assertEqual(runtime_failures(fixed), {},
                         "置き換えた後は動くはず: {0}".format(configcheck._show(override, 120)))
        self.assertEqual(errors_of_config(fixed), [], "置き換えた後に error が残る: {0}".format(configcheck._show(override, 120)))
        return fixed

    def assert_tolerated(self, override, key=None, runtime=True):
        """warning（``key`` が ``None`` なら指摘なし）: 素の値でもランタイムが動き、``sanitized()`` は何も変えない。

        指摘（warning）を返す。
        """
        config = make_config(override)
        if runtime:
            self.assertEqual(runtime_failures(config), {},
                             "ランタイムは動くはず: {0}".format(configcheck._show(override, 120)))
        found = validate(config)
        self.assertEqual([f for f in found if f.level == ERROR], [],
                         "error ではないはず: {0}".format(configcheck._show(override, 120)))
        if key is None:
            self.assertEqual(found, [], "指摘なしのはず: {0}".format(configcheck._show(override, 120)))
        else:
            self.assertIn(key, keys_of(found), "{0} の warning が出るはず: {1}".format(key, configcheck._show(override, 120)))
        fixed, errors = sanitized(config)
        self.assertEqual(errors, [])
        self.assertEqual(fixed, config, "warning は置き換えないはず: {0}".format(configcheck._show(override, 120)))
        return [f for f in found if f.key == key]


def errors_of_config(config):
    return [finding for finding in validate(config) if finding.level == ERROR]


# ---------------------------------------------------------------------------
# 分類の表
# ---------------------------------------------------------------------------
INF = float("inf")
NAN = float("nan")
HUGE = 10 ** 400

#: error: (説明, 上書き, error になるキー, 壊れる段階)。素の値でランタイムが壊れる。
BREAKING = [
    # -- 予定を作るとき（Scheduler が int() や datetime() で例外にする） --
    ("閉館放送の時が 24", nested("schedule.closing.hour", 24), "schedule.closing.hour", "予定"),
    ("閉館放送の時が -1", nested("schedule.closing.hour", -1), "schedule.closing.hour", "予定"),
    ("閉館放送の時が文字列 x", nested("schedule.closing.hour", "x"), "schedule.closing.hour", "予定"),
    ("閉館放送の時が null", nested("schedule.closing.hour", None), "schedule.closing.hour", "予定"),
    ("閉館放送の時がリスト", nested("schedule.closing.hour", [16]), "schedule.closing.hour", "予定"),
    ("閉館放送の分が 60", nested("schedule.closing.minute", 60), "schedule.closing.minute", "予定"),
    ("閉館放送の分が -1", nested("schedule.closing.minute", -1), "schedule.closing.minute", "予定"),
    ("閉館放送の分が文字列 x", nested("schedule.closing.minute", "x"), "schedule.closing.minute", "予定"),
    ("閉館放送の分が null", nested("schedule.closing.minute", None), "schedule.closing.minute", "予定"),
    ("時報の分が 60", nested("schedule.hourly.minute", 60), "schedule.hourly.minute", "予定"),
    ("時報の分が -1", nested("schedule.hourly.minute", -1), "schedule.hourly.minute", "予定"),
    ("時報の分が null", nested("schedule.hourly.minute", None), "schedule.hourly.minute", "予定"),
    ("時報の分が文字列 x", nested("schedule.hourly.minute", "x"), "schedule.hourly.minute", "予定"),
    ("時報の開始の時が null", nested("schedule.hourly.start_hour", None), "schedule.hourly.start_hour", "予定"),
    ("時報の開始の時が文字列 x", nested("schedule.hourly.start_hour", "x"), "schedule.hourly.start_hour", "予定"),
    ("時報の開始の時がリスト", nested("schedule.hourly.start_hour", [10]), "schedule.hourly.start_hour", "予定"),
    ("時報の終了の時が null", nested("schedule.hourly.end_hour", None), "schedule.hourly.end_hour", "予定"),
    ("時報の終了の時が文字列 x", nested("schedule.hourly.end_hour", "x"), "schedule.hourly.end_hour", "予定"),
    ("休みの時刻に文字列 x", nested("schedule.hourly.skip_hours", [12, "x"]), "schedule.hourly.skip_hours", "予定"),
    ("休みの時刻に null", nested("schedule.hourly.skip_hours", [None]), "schedule.hourly.skip_hours", "予定"),
    ("休みの時刻にリスト", nested("schedule.hourly.skip_hours", [[1]]), "schedule.hourly.skip_hours", "予定"),
    ("休みの時刻が数値", nested("schedule.hourly.skip_hours", 5), "schedule.hourly.skip_hours", "予定"),
    ("休みの時刻が true", nested("schedule.hourly.skip_hours", True), "schedule.hourly.skip_hours", "予定"),
    ("休みの時刻が 1 文字ずつ読めない文字列", nested("schedule.hourly.skip_hours", "none"), "schedule.hourly.skip_hours", "予定"),
    ("時報の曜日が数値", nested("schedule.hourly.weekdays", 3), "schedule.hourly.weekdays", "予定"),
    ("時報の曜日が null", nested("schedule.hourly.weekdays", None), "schedule.hourly.weekdays", "予定"),
    ("時報の曜日が true", nested("schedule.hourly.weekdays", True), "schedule.hourly.weekdays", "予定"),
    ("時報の曜日にリスト", nested("schedule.hourly.weekdays", [0, [1]]), "schedule.hourly.weekdays", "予定"),
    ("時報の曜日に辞書", nested("schedule.hourly.weekdays", [0, {}]), "schedule.hourly.weekdays", "予定"),
    ("閉館放送の曜日が数値", nested("schedule.closing.weekdays", 3), "schedule.closing.weekdays", "予定"),
    ("閉館放送の曜日が null", nested("schedule.closing.weekdays", None), "schedule.closing.weekdays", "予定"),
    ("閉館放送の曜日にリスト", nested("schedule.closing.weekdays", [0, [1]]), "schedule.closing.weekdays", "予定"),
    ("時報の節が数値", nested("schedule.hourly", 5), "schedule.hourly", "予定"),
    ("時報の節が文字列", nested("schedule.hourly", "x"), "schedule.hourly", "予定"),
    ("時報の節が true", nested("schedule.hourly", True), "schedule.hourly", "予定"),
    ("時報の節がリスト", nested("schedule.hourly", [1]), "schedule.hourly", "予定"),
    ("閉館放送の節が数値", nested("schedule.closing", 5), "schedule.closing", "予定"),
    ("閉館放送の節が文字列", nested("schedule.closing", "x"), "schedule.closing", "予定"),
    # -- 日時の計算（今から何秒ずらすか）が範囲を出る --
    ("準備の秒数が 1e12", nested("schedule.prepare_lead_seconds", 1e12), "schedule.prepare_lead_seconds", "予定"),
    ("準備の秒数が -1e12", nested("schedule.prepare_lead_seconds", -1e12), "schedule.prepare_lead_seconds", "予定"),
    ("準備の秒数が無限大", nested("schedule.prepare_lead_seconds", INF), "schedule.prepare_lead_seconds", "予定"),
    ("準備の秒数が NaN", nested("schedule.prepare_lead_seconds", NAN), "schedule.prepare_lead_seconds", "予定"),
    ("準備の秒数が 10**400", nested("schedule.prepare_lead_seconds", HUGE), "schedule.prepare_lead_seconds", "起動"),
    ("先行の秒数が 1e12", nested("schedule.pip_lead_seconds", 1e12), "schedule.pip_lead_seconds", "予定"),
    ("先行の秒数が -1e12", nested("schedule.pip_lead_seconds", -1e12), "schedule.pip_lead_seconds", "予定"),
    ("先行の秒数が NaN", nested("schedule.pip_lead_seconds", NAN), "schedule.pip_lead_seconds", "予定"),
    ("先行の秒数が 10**400", nested("schedule.pip_lead_seconds", HUGE), "schedule.pip_lead_seconds", "起動"),
    ("遅れて鳴らす上限が 1e12", nested("schedule.catchup_grace_seconds", 1e12), "schedule.catchup_grace_seconds", "予定"),
    ("遅れて鳴らす上限が -1e12", nested("schedule.catchup_grace_seconds", -1e12), "schedule.catchup_grace_seconds", "予定"),
    ("遅れて鳴らす上限が無限大", nested("schedule.catchup_grace_seconds", INF), "schedule.catchup_grace_seconds", "予定"),
    ("短音の数が 10**20", nested("time_signal.short_pip_count", 10 ** 20), "time_signal.short_pip_count", "予定"),
    ("短音の数が 10**400", nested("time_signal.short_pip_count", HUGE), "time_signal.short_pip_count", "起動"),
    ("短音の間隔が 1e300", nested("time_signal.pip_interval_ms", 1e300), "time_signal.pip_interval_ms", "予定"),
    ("短音の間隔が無限大", nested("time_signal.pip_interval_ms", INF), "time_signal.pip_interval_ms", "予定"),
    ("短音の間隔が NaN", nested("time_signal.pip_interval_ms", NAN), "time_signal.pip_interval_ms", "予定"),
    # -- 起動時に int() / float() が例外になる --
    ("待機の秒数が文字列 x", nested("schedule.max_sleep_seconds", "x"), "schedule.max_sleep_seconds", "起動"),
    ("待機の秒数が null", nested("schedule.max_sleep_seconds", None), "schedule.max_sleep_seconds", "起動"),
    ("待機の秒数がリスト", nested("schedule.max_sleep_seconds", [30]), "schedule.max_sleep_seconds", "起動"),
    ("先行の秒数が文字列 x", nested("schedule.pip_lead_seconds", "x"), "schedule.pip_lead_seconds", "起動"),
    ("準備の秒数が文字列 x", nested("schedule.prepare_lead_seconds", "x"), "schedule.prepare_lead_seconds", "起動"),
    ("準備の秒数が null", nested("schedule.prepare_lead_seconds", None), "schedule.prepare_lead_seconds", "起動"),
    ("遅れて鳴らす上限が文字列 soon", nested("schedule.catchup_grace_seconds", "soon"), "schedule.catchup_grace_seconds", "起動"),
    ("短音の数が文字列 x", nested("time_signal.short_pip_count", "x"), "time_signal.short_pip_count", "起動"),
    ("短音の数が null", nested("time_signal.short_pip_count", None), "time_signal.short_pip_count", "起動"),
    ("短音の間隔が文字列 x", nested("time_signal.pip_interval_ms", "x"), "time_signal.pip_interval_ms", "起動"),
    ("ひとことの重複回避が文字列 x", nested("quotes.avoid_recent", "x"), "quotes.avoid_recent", "起動"),
    ("ひとことの重複回避が無限大", nested("quotes.avoid_recent", INF), "quotes.avoid_recent", "起動"),
    ("ひとことの重複回避が NaN", nested("quotes.avoid_recent", NAN), "quotes.avoid_recent", "起動"),
    ("天気の待ち時間が文字列 x", nested("weather.timeout_seconds", "x"), "weather.timeout_seconds", "起動"),
    ("天気の保存時間が文字列 x", nested("weather.cache_minutes", "x"), "weather.cache_minutes", "起動"),
    ("天気の保存時間が 10**400", nested("weather.cache_minutes", HUGE), "weather.cache_minutes", "起動"),
    ("VOICEVOX の話者が文字列 x", nested("tts.voicevox.speaker", "x"), "tts.voicevox.speaker", "起動"),
    ("VOICEVOX の待ち時間が文字列 x", nested("tts.voicevox.timeout_seconds", "x"), "tts.voicevox.timeout_seconds", "起動"),
    ("VOICEVOX の疎通確認が文字列 x", nested("tts.voicevox.probe_timeout_seconds", "x"), "tts.voicevox.probe_timeout_seconds", "起動"),
    ("VOICEVOX の疎通確認の待ち時間が 1e12", nested("tts.voicevox.probe_timeout_seconds", 1e12), "tts.voicevox.probe_timeout_seconds", "VOICEVOX:疎通確認"),
    ("VOICEVOX の疎通確認の待ち時間が -1e12", nested("tts.voicevox.probe_timeout_seconds", -1e12), "tts.voicevox.probe_timeout_seconds", "VOICEVOX:疎通確認"),
    ("VOICEVOX の疎通確認の待ち時間が無限大", nested("tts.voicevox.probe_timeout_seconds", INF), "tts.voicevox.probe_timeout_seconds", "VOICEVOX:疎通確認"),
    ("VOICEVOX の疎通確認の待ち時間が 9.3e9", nested("tts.voicevox.probe_timeout_seconds", 9.3e9), "tts.voicevox.probe_timeout_seconds", "VOICEVOX:疎通確認"),
    ("VOICEVOX の合成の待ち時間が 1e12", nested("tts.voicevox.timeout_seconds", 1e12), "tts.voicevox.timeout_seconds", "VOICEVOX:合成"),
    ("VOICEVOX の合成の待ち時間が -1e12", nested("tts.voicevox.timeout_seconds", -1e12), "tts.voicevox.timeout_seconds", "VOICEVOX:合成"),
    ("VOICEVOX の合成の待ち時間が無限大", nested("tts.voicevox.timeout_seconds", INF), "tts.voicevox.timeout_seconds", "VOICEVOX:合成"),
    ("VOICEVOX の節が null", nested("tts.voicevox", None), "tts.voicevox", "起動"),
    ("VOICEVOX の節が数値", nested("tts.voicevox", 5), "tts.voicevox", "起動"),
    ("読み上げのエンジンが null", nested("tts.engines", None), "tts.engines", "起動"),
    ("読み上げのエンジンが数値", nested("tts.engines", 5), "tts.engines", "起動"),
    ("読み上げのエンジンが true", nested("tts.engines", True), "tts.engines", "起動"),
    ("タイムゾーンの綴り違い", nested("timezone", "Asia/Tokio"), "timezone", "時計"),
    # -- 再生のとき --
    ("無音の間隔が負", nested("audio.gap_ms", -1), "audio.gap_ms", "再生:mock"),
    ("無音の間隔が待てないほど大きい", nested("audio.gap_ms", 10 ** 41), "audio.gap_ms", "再生:pygame"),
    ("無音の間隔が文字列 x", nested("audio.gap_ms", "x"), "audio.gap_ms", "起動時の記録"),
    ("無音の間隔が null", nested("audio.gap_ms", None), "audio.gap_ms", "起動時の記録"),
    ("mixer の周波数が文字列 x", nested("audio.mixer.frequency", "x"), "audio.mixer.frequency", "再生:pygame"),
    ("mixer の bit が null", nested("audio.mixer.size", None), "audio.mixer.size", "再生:pygame"),
    ("mixer のチャンネル数が文字列 x", nested("audio.mixer.channels", "x"), "audio.mixer.channels", "再生:pygame"),
    ("mixer のバッファが文字列 x", nested("audio.mixer.buffer", "x"), "audio.mixer.buffer", "再生:pygame"),
    ("mixer の節が null", nested("audio.mixer", None), "audio.mixer", "再生:pygame"),
    ("mixer の節が数値", nested("audio.mixer", 5), "audio.mixer", "再生:pygame"),
    ("外部コマンドの節が null", nested("audio.commands", None), "audio.commands", "再生:command"),
    ("外部コマンドの節が数値", nested("audio.commands", 5), "audio.commands", "再生:command"),
    ("外部コマンドの項目が数値", nested("audio.commands", {".wav": 1}), "audio.commands", "再生:command"),
    ("外部コマンドの項目が null", nested("audio.commands", {".mp3": None}), "audio.commands", "再生:command"),
    ("外部コマンドの項目が 1 つの文字列", nested("audio.commands", {".wav": "aplay -q {path}"}), "audio.commands", "再生:command"),
    ("外部コマンドの項目が波括弧の無い文字列", nested("audio.commands", {".wav": "aplay"}), "audio.commands", "再生:command"),
    ("外部コマンドの mp3 の項目が 1 つの文字列", nested("audio.commands", {".mp3": "mpg123 -q {path}"}), "audio.commands", "再生:command"),
    ("外部コマンドの項目の要素が数値", nested("audio.commands", {".wav": ["aplay", 5]}), "audio.commands", "再生:command"),
    ("外部コマンドの項目の要素が null", nested("audio.commands", {".mp3": ["mpg123", None]}), "audio.commands", "再生:command"),
    ("mock の秒数が負", nested("audio.mock_max_seconds", -1), "audio.mock_max_seconds", "再生:mock"),
    ("mock の秒数が NaN", nested("audio.mock_max_seconds", NAN), "audio.mock_max_seconds", "再生:mock"),
    ("mock の秒数が文字列 x", nested("audio.mock_max_seconds", "x"), "audio.mock_max_seconds", "再生:mock"),
    ("再生方式が数値", nested("audio.backend", 1), "audio.backend", "再生方式"),
    ("再生方式が true", nested("audio.backend", True), "audio.backend", "再生方式"),
    ("再生方式がリスト", nested("audio.backend", ["mock"]), "audio.backend", "再生方式"),
    # -- CPU の空回り --
    ("待機の秒数が 0", nested("schedule.max_sleep_seconds", 0), "schedule.max_sleep_seconds", "待機"),
    ("待機の秒数が負", nested("schedule.max_sleep_seconds", -5), "schedule.max_sleep_seconds", "待機"),
    ("待機の秒数が false", nested("schedule.max_sleep_seconds", False), "schedule.max_sleep_seconds", "待機"),
    ("待機の秒数が文字列 0", nested("schedule.max_sleep_seconds", "0"), "schedule.max_sleep_seconds", "待機"),
    # -- ログ --
    ("ログの書式に知らない項目", nested("logging.format", "%(foo)s"), "logging.format", "ログ"),
    ("ログの書式の型が合わない", nested("logging.format", "%(message)d"), "logging.format", "ログ"),
    ("ログの書式に余分な %s", nested("logging.format", "%(asctime)s %(message)s %s"), "logging.format", "ログ"),
    ("ログの書式が閉じていない", nested("logging.format", "%(asctime"), "logging.format", "ログ"),
    ("ログの書式に置換が無い", nested("logging.format", "ログ"), "logging.format", "ログ"),
    ("ログの書式が数値", nested("logging.format", 5), "logging.format", "ログ"),
    ("ログの書式がリスト", nested("logging.format", ["%(message)s"]), "logging.format", "ログ"),
    ("ログの水準の綴り違い", nested("logging.level", "LOUD"), "logging.level", "ログ"),
    ("ログの水準が数値", nested("logging.level", 10), "logging.level", "ログ"),
    ("ログの水準が空文字列", nested("logging.level", ""), "logging.level", "ログ"),
    ("ログの水準が属性名", nested("logging.level", "basicConfig"), "logging.level", "ログ"),
    ("ログの水準の前後に空白", nested("logging.level", "INFO "), "logging.level", "ログ"),
]

#: warning: (説明, 上書き, warning が出るキー)。ランタイムは動くので置き換えない。キーが ``None`` なら、指摘なし。
TOLERATED = [
    # -- 数値の書き方の違い（int() / float() が読める） --
    ("時報の開始の時が文字列 9", nested("schedule.hourly.start_hour", "9"), "schedule.hourly.start_hour"),
    ("時報の開始の時が 9.0", nested("schedule.hourly.start_hour", 9.0), "schedule.hourly.start_hour"),
    ("時報の開始の時が 9.5", nested("schedule.hourly.start_hour", 9.5), "schedule.hourly.start_hour"),
    ("時報の開始の時が true", nested("schedule.hourly.start_hour", True), "schedule.hourly.start_hour"),
    ("時報の終了の時が 16.0", nested("schedule.hourly.end_hour", 16.0), "schedule.hourly.end_hour"),
    ("時報の終了の時が文字列 16", nested("schedule.hourly.end_hour", "16"), "schedule.hourly.end_hour"),
    ("閉館放送の時が文字列 16", nested("schedule.closing.hour", "16"), "schedule.closing.hour"),
    ("閉館放送の時が 16.0", nested("schedule.closing.hour", 16.0), "schedule.closing.hour"),
    ("閉館放送の時が true", nested("schedule.closing.hour", True), "schedule.closing.hour"),
    ("閉館放送の分が 55.0", nested("schedule.closing.minute", 55.0), "schedule.closing.minute"),
    ("閉館放送の分が文字列 57", nested("schedule.closing.minute", "57"), "schedule.closing.minute"),
    ("時報の分が false", nested("schedule.hourly.minute", False), "schedule.hourly.minute"),
    ("時報の分が 0.5", nested("schedule.hourly.minute", 0.5), "schedule.hourly.minute"),
    ("準備の秒数が文字列 45", nested("schedule.prepare_lead_seconds", "45"), "schedule.prepare_lead_seconds"),
    ("先行の秒数が文字列 3", nested("schedule.pip_lead_seconds", "3"), "schedule.pip_lead_seconds"),
    ("遅れて鳴らす上限が文字列 120", nested("schedule.catchup_grace_seconds", "120"), "schedule.catchup_grace_seconds"),
    ("待機の秒数が文字列 30", nested("schedule.max_sleep_seconds", "30"), "schedule.max_sleep_seconds"),
    ("待機の秒数が NaN", nested("schedule.max_sleep_seconds", NAN), "schedule.max_sleep_seconds"),
    ("待機の秒数が無限大", nested("schedule.max_sleep_seconds", INF), "schedule.max_sleep_seconds"),
    ("短音の数が 3.0", nested("time_signal.short_pip_count", 3.0), "time_signal.short_pip_count"),
    ("短音の数が文字列 3", nested("time_signal.short_pip_count", "3"), "time_signal.short_pip_count"),
    ("短音の間隔が文字列 1000", nested("time_signal.pip_interval_ms", "1000"), "time_signal.pip_interval_ms"),
    ("無音の間隔が 350.0", nested("audio.gap_ms", 350.0), "audio.gap_ms"),
    ("無音の間隔が文字列 350", nested("audio.gap_ms", "350"), "audio.gap_ms"),
    ("mixer の周波数が 44100.0", nested("audio.mixer.frequency", 44100.0), "audio.mixer.frequency"),
    ("mixer のチャンネル数が 2.0", nested("audio.mixer.channels", 2.0), "audio.mixer.channels"),
    ("mixer のバッファが文字列 4096", nested("audio.mixer.buffer", "4096"), "audio.mixer.buffer"),
    ("mock の秒数が無限大", nested("audio.mock_max_seconds", INF), "audio.mock_max_seconds"),
    ("ひとことの重複回避が 8.0", nested("quotes.avoid_recent", 8.0), "quotes.avoid_recent"),
    ("天気の待ち時間が文字列 8", nested("weather.timeout_seconds", "8"), "weather.timeout_seconds"),
    ("天気の待ち時間が無限大", nested("weather.timeout_seconds", INF), "weather.timeout_seconds"),
    ("天気の待ち時間がソケットの上限を超える 1e12", nested("weather.timeout_seconds", 1e12), "weather.timeout_seconds"),
    ("天気の待ち時間がソケットの下限を超える -1e12", nested("weather.timeout_seconds", -1e12), "weather.timeout_seconds"),
    ("天気の保存時間が NaN", nested("weather.cache_minutes", NAN), "weather.cache_minutes"),
    ("VOICEVOX の話者が 3.0", nested("tts.voicevox.speaker", 3.0), "tts.voicevox.speaker"),
    # -- 部品ごとランタイムが受け止める値（時報音を作れない・フェードを省く） --
    ("時報音の周波数が文字列 x", nested("time_signal.short_pip.frequency", "x"), "time_signal.short_pip.frequency"),
    ("時報音の長さが null", nested("time_signal.long_pip.duration_ms", None), "time_signal.long_pip.duration_ms"),
    ("時報音の音量が文字列 x", nested("time_signal.volume", "x"), "time_signal.volume"),
    ("時報音の音量が範囲外でも NaN", nested("time_signal.volume", NAN), "time_signal.volume"),
    ("時報音のフェードが文字列 x", nested("time_signal.envelope_ms", "x"), "time_signal.envelope_ms"),
    ("蛍の光のフェードが文字列 x", nested("audio.fade_in_ms", "x"), "audio.fade_in_ms"),
    ("蛍の光のフェードが null", nested("audio.fade_in_ms", None), "audio.fade_in_ms"),
    # -- 範囲外の時刻・順序の逆転（鳴らさないだけ） --
    ("時報の開始の時が -1", nested("schedule.hourly.start_hour", -1), "schedule.hourly.start_hour"),
    ("時報の開始の時が 24", {"schedule": {"hourly": {"start_hour": 24, "end_hour": 24}}}, "schedule.hourly.start_hour"),
    ("時報の終了の時が 24", nested("schedule.hourly.end_hour", 24), "schedule.hourly.end_hour"),
    ("時報の終了の時が 1600", nested("schedule.hourly.end_hour", 1600), "schedule.hourly.end_hour"),
    ("時報の終了の時が -5", {"schedule": {"hourly": {"start_hour": -9, "end_hour": -5}}}, "schedule.hourly.end_hour"),
    ("時報の開始が終了より後ろ", nested("schedule.hourly.start_hour", 17), "schedule.hourly.start_hour"),
    ("時報の時刻の範囲が 10 万件", {"schedule": {"hourly": {"start_hour": 0, "end_hour": 100000}}}, "schedule.hourly.end_hour"),
    ("休みの時刻が範囲外を含む", nested("schedule.hourly.skip_hours", [12, 24]), "schedule.hourly.skip_hours"),
    ("休みの時刻が負", nested("schedule.hourly.skip_hours", [-1]), "schedule.hourly.skip_hours"),
    ("休みの時刻が文字列", nested("schedule.hourly.skip_hours", ["12"]), "schedule.hourly.skip_hours"),
    ("休みの時刻が小数", nested("schedule.hourly.skip_hours", [12.0]), "schedule.hourly.skip_hours"),
    ("休みの時刻が真偽値", nested("schedule.hourly.skip_hours", [True]), "schedule.hourly.skip_hours"),
    ("休みの時刻が文字列そのもの", nested("schedule.hourly.skip_hours", "12"), "schedule.hourly.skip_hours"),
    ("休みの時刻が辞書", nested("schedule.hourly.skip_hours", {"12": 1}), "schedule.hourly.skip_hours"),
    ("休みの時刻が null", nested("schedule.hourly.skip_hours", None), None),
    ("休みの時刻が空", nested("schedule.hourly.skip_hours", []), None),
    ("天気の時刻が数値", nested("extra_segment.weather_hours", 12), "extra_segment.weather_hours"),
    ("天気の時刻が文字列そのもの", nested("extra_segment.weather_hours", "12"), "extra_segment.weather_hours"),
    ("天気の時刻に読めない要素", nested("extra_segment.weather_hours", ["x", 12]), "extra_segment.weather_hours"),
    ("天気の時刻が範囲外", nested("extra_segment.weather_hours", [12, 24]), "extra_segment.weather_hours"),
    ("天気の時刻が文字列の要素", nested("extra_segment.weather_hours", ["12"]), "extra_segment.weather_hours"),
    ("天気の時刻が空", nested("extra_segment.weather_hours", []), None),
    # -- 曜日（どの日にも一致しないだけ） --
    ("時報の曜日が文字列", nested("schedule.hourly.weekdays", ["0", "1", "2", "3", "4"]), "schedule.hourly.weekdays"),
    ("時報の曜日に 7", nested("schedule.hourly.weekdays", [0, 1, 2, 3, 4, 5, 7]), "schedule.hourly.weekdays"),
    ("時報の曜日が負", nested("schedule.hourly.weekdays", [-1]), "schedule.hourly.weekdays"),
    ("時報の曜日に null", nested("schedule.hourly.weekdays", [0, None]), "schedule.hourly.weekdays"),
    ("時報の曜日が文字列そのもの", nested("schedule.hourly.weekdays", "01234"), "schedule.hourly.weekdays"),
    ("時報の曜日が辞書", nested("schedule.hourly.weekdays", {"0": 1}), "schedule.hourly.weekdays"),
    ("時報の曜日が小数", nested("schedule.hourly.weekdays", [1.0, 2.0]), "schedule.hourly.weekdays"),
    ("時報の曜日が真偽値", nested("schedule.hourly.weekdays", [True]), "schedule.hourly.weekdays"),
    ("閉館放送の曜日が文字列", nested("schedule.closing.weekdays", ["0", "1"]), "schedule.closing.weekdays"),
    ("閉館放送の曜日に 7", nested("schedule.closing.weekdays", [0, 1, 2, 3, 4, 7]), "schedule.closing.weekdays"),
    ("時報の曜日が空", nested("schedule.hourly.weekdays", []), None),
    # -- 真偽値の書き方（ランタイムは値の真偽で読む） --
    ("時報の enabled が文字列 false", nested("schedule.hourly.enabled", "false"), "schedule.hourly.enabled"),
    ("時報の enabled が 0", nested("schedule.hourly.enabled", 0), "schedule.hourly.enabled"),
    ("閉館放送の enabled が null", nested("schedule.closing.enabled", None), "schedule.closing.enabled"),
    ("おまけの enabled が文字列 no", nested("extra_segment.enabled", "no"), "extra_segment.enabled"),
    ("天気の enabled が 1", nested("weather.enabled", 1), "weather.enabled"),
    ("正午の専用文言の指定が 0", nested("time_signal.use_noon_template", 0), "time_signal.use_noon_template"),
    ("正午の専用文言の指定が null", nested("time_signal.use_noon_template", None), None),
    # -- 止めた節の中は読まれない --
    ("止めた閉館放送の時が 24", {"schedule": {"closing": {"enabled": False, "hour": 24}}}, "schedule.closing.hour"),
    ("止めた閉館放送の分が 60", {"schedule": {"closing": {"enabled": False, "minute": 60}}}, "schedule.closing.minute"),
    ("止めた閉館放送の曜日が null", {"schedule": {"closing": {"enabled": False, "weekdays": None}}}, "schedule.closing.weekdays"),
    ("止めた時報の分が 60", {"schedule": {"hourly": {"enabled": False, "minute": 60}}}, "schedule.hourly.minute"),
    ("止めた時報の曜日が null", {"schedule": {"hourly": {"enabled": False, "weekdays": None}}}, "schedule.hourly.weekdays"),
    ("止めた時報の休みの時刻が x", {"schedule": {"hourly": {"enabled": False, "skip_hours": ["x"]}}}, "schedule.hourly.skip_hours"),
    ("読まれない VOICEVOX の話者が x", {"tts": {"engines": ["prerecorded"], "voicevox": {"speaker": "x"}}}, "tts.voicevox.speaker"),
    ("読まれない VOICEVOX の節が null", {"tts": {"engines": ["prerecorded"], "voicevox": None}}, "tts.voicevox"),
    # -- null や数値にした節（ランタイムは空の節として動く） --
    ("時報の節が null", nested("schedule.hourly", None), "schedule.hourly"),
    ("時報の節が 0", nested("schedule.hourly", 0), "schedule.hourly"),
    ("時報の節が空文字列", nested("schedule.hourly", ""), "schedule.hourly"),
    ("時報の節が空リスト", nested("schedule.hourly", []), "schedule.hourly"),
    ("時報の節が false", nested("schedule.hourly", False), "schedule.hourly"),
    ("閉館放送の節が null", nested("schedule.closing", None), "schedule.closing"),
    ("予定の節が null", nested("schedule", None), "schedule"),
    ("予定の節が数値", nested("schedule", 5), "schedule"),
    ("ログの節が null", nested("logging", None), "logging"),
    ("再生の節が null", nested("audio", None), "audio"),
    ("時報の設定の節が null", nested("time_signal", None), "time_signal"),
    ("時報音の節が null", nested("time_signal.short_pip", None), "time_signal.short_pip"),
    ("長音の節が数値", nested("time_signal.long_pip", 5), "time_signal.long_pip"),
    ("時刻の読み換えが null", nested("time_signal.hour_readings", None), "time_signal.hour_readings"),
    ("時刻の読み換えが数値", nested("time_signal.hour_readings", 5), "time_signal.hour_readings"),
    ("おまけの節が null", nested("extra_segment", None), "extra_segment"),
    ("ひとことの節が null", nested("quotes", None), "quotes"),
    ("天気の節が null", nested("weather", None), "weather"),
    ("天気の地点の節が null", nested("weather.open_meteo", None), "weather.open_meteo"),
    ("天気の作り置きの節が null", nested("weather.prerecord", None), "weather.prerecord"),
    ("読み上げの節が null", nested("tts", None), "tts"),
    ("閉館放送の音源の節が null", nested("closing", None), "closing"),
    ("状態の節が null", nested("state", None), "state"),
    # -- 緯度経度・テンプレート・名前（ランタイムが受け止める） --
    ("緯度が範囲外", nested("weather.open_meteo.locations", [{"label": "x", "latitude": 135.0, "longitude": 35.0}]), "weather.open_meteo.locations"),
    ("緯度が文字列", nested("weather.open_meteo.locations", [{"label": "x", "latitude": "35.0", "longitude": 135.0}]), "weather.open_meteo.locations"),
    ("経度が無い", nested("weather.open_meteo.locations", [{"label": "x", "latitude": 35.0}]), "weather.open_meteo.locations"),
    ("地点が文字列", nested("weather.open_meteo.locations", ["大津"]), "weather.open_meteo.locations"),
    ("地点のリストが文字列", nested("weather.open_meteo.locations", "大津"), "weather.open_meteo.locations"),
    ("地点が空", nested("weather.open_meteo.locations", []), None),
    ("時刻アナウンスに知らない置換", nested("time_signal.announce_template", "{hours}"), "time_signal.announce_template"),
    ("時刻アナウンスが番号の置換", nested("time_signal.announce_template", "{}"), "time_signal.announce_template"),
    ("時刻アナウンスの波括弧が閉じていない", nested("time_signal.announce_template", "{period"), "time_signal.announce_template"),
    ("時刻アナウンスが存在しない属性", nested("time_signal.announce_template", "{hour.foo}"), "time_signal.announce_template"),
    ("時刻アナウンスの書式指定が合わない", nested("time_signal.announce_template", "{hour_reading:02d}"), "time_signal.announce_template"),
    ("時刻アナウンスが数値", nested("time_signal.announce_template", 5), "time_signal.announce_template"),
    ("時刻アナウンスが null", nested("time_signal.announce_template", None), None),
    ("正午のアナウンスに知らない置換", nested("time_signal.noon_template", "{hours}"), "time_signal.noon_template"),
    ("天気の文に知らない置換", nested("weather.sentence_weather", "{temp}度"), "weather.sentence_weather"),
    ("気温の文の波括弧が閉じていない", nested("weather.sentence_temp", "{temp"), "weather.sentence_temp"),
    ("気温の文が数値", nested("weather.sentence_temp", 5), "weather.sentence_temp"),
    ("天気の文の書式指定の幅が大きすぎる", nested("weather.sentence_weather", "{label:>5000}"), "weather.sentence_weather"),
    ("気温の文の書式指定の桁数が大きすぎる", nested("weather.sentence_temp", "{temp:.5000f}"), "weather.sentence_temp"),
    ("時刻アナウンスの書式指定の幅が大きすぎる", nested("time_signal.announce_template", "{period}{hour_reading:>5000}"), "time_signal.announce_template"),
    ("正午のアナウンスの書式指定の幅が大きすぎる", nested("time_signal.noon_template", "{hour_reading:*^5000}"), "time_signal.noon_template"),
    ("気温の文が null", nested("weather.sentence_temp", None), "weather.sentence_temp"),
    ("最高気温の文に知らない置換", nested("weather.sentence_temp_max", "{label}"), "weather.sentence_temp_max"),
    ("降水確率の文に知らない置換", nested("weather.sentence_pop", "{x}"), "weather.sentence_pop"),
    ("再生方式の綴り違い", nested("audio.backend", "pygam"), "audio.backend"),
    ("再生方式の前に空白", nested("audio.backend", " mock"), "audio.backend"),
    ("再生方式が null", nested("audio.backend", None), None),
    ("再生方式が空文字列", nested("audio.backend", ""), None),
    ("エンジン名の綴り違い", nested("tts.engines", ["prerecorded", "open_jtalk"]), "tts.engines"),
    ("エンジンが文字列", nested("tts.engines", "prerecorded"), "tts.engines"),
    ("エンジンが辞書", nested("tts.engines", {"open_jtalk": 1}), "tts.engines"),
    ("エンジンの要素が数値", nested("tts.engines", ["prerecorded", 5]), "tts.engines"),
    ("エンジンが空", nested("tts.engines", []), "tts.engines"),
    ("エンジンが空文字列", nested("tts.engines", ""), "tts.engines"),
    ("エンジンが空の辞書", nested("tts.engines", {}), "tts.engines"),
    ("エンジンが voicevox だけ", nested("tts.engines", ["voicevox"]), "tts.engines"),
    ("エンジン名が全部綴り違い", nested("tts.engines", ["open_jtalk"]), "tts.engines"),
    # -- 作り置きの範囲 --
    ("気温の範囲が広すぎる", nested("weather.prerecord.temp_max", 10 ** 9), "weather.prerecord.temp_max"),
    ("気温の範囲が逆", {"weather": {"prerecord": {"temp_min": 10, "temp_max": 0}}}, "weather.prerecord.temp_min"),
    ("気温の下限が文字列 x", nested("weather.prerecord.temp_min", "x"), "weather.prerecord.temp_min"),
    ("降水確率の刻みが 0", nested("weather.prerecord.pop_step", 0), "weather.prerecord.pop_step"),
    ("降水確率の刻みが負", nested("weather.prerecord.pop_step", -10), "weather.prerecord.pop_step"),
    ("降水確率の刻みが文字列 x", nested("weather.prerecord.pop_step", "x"), "weather.prerecord.pop_step"),
    # -- 大きいが日時の範囲に収まる値（指摘なし） --
    ("準備の秒数が 5e10", nested("schedule.prepare_lead_seconds", 5e10), None),
    ("先行の秒数が -1e10", nested("schedule.pip_lead_seconds", -1e10), None),
    ("遅れて鳴らす上限が 1e9", nested("schedule.catchup_grace_seconds", 1e9), None),
    ("遅れて鳴らす上限が -86400", nested("schedule.catchup_grace_seconds", -86400), None),
    ("短音の数が 10**6", nested("time_signal.short_pip_count", 10 ** 6), None),
    ("短音の間隔が 1e6", nested("time_signal.pip_interval_ms", 1e6), None),
    ("準備の秒数が負", nested("schedule.prepare_lead_seconds", -45), None),
    ("先行の秒数が 3", nested("schedule.pip_lead_seconds", 3), None),
    ("無音の間隔が 0", nested("audio.gap_ms", 0), None),
    ("待機の秒数が 1", nested("schedule.max_sleep_seconds", 1), None),
    ("ログの水準が小文字", nested("logging.level", "debug"), None),
    ("ログの水準が WARN", nested("logging.level", "WARN"), None),
    ("ログの水準が null", nested("logging.level", None), None),
    ("ログの書式が null", nested("logging.format", None), None),
    ("ログの書式が空文字列", nested("logging.format", ""), None),
]


class FindingTest(unittest.TestCase):
    def test_hint_and_source_are_optional(self):
        finding = Finding("error", "timezone", "メッセージ")
        self.assertEqual((finding.hint, finding.source), ("", ""))

    def test_a_finding_is_immutable(self):
        finding = Finding("info", "a.b", "m")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            finding.level = "error"

    def test_describe_puts_key_message_hint_and_source_on_one_line(self):
        finding = Finding("warning", "schedule.hourly.minite", "知らないキーです",
                          "もしかして schedule.hourly.minute?", "/etc/chime.json")
        self.assertEqual(
            finding.describe(),
            "schedule.hourly.minite: 知らないキーです（もしかして schedule.hourly.minute?）"
            " [/etc/chime.json]")

    def test_describe_without_key_hint_or_source(self):
        self.assertEqual(Finding("warning", "", "ファイル全体の問題").describe(), "ファイル全体の問題")

    def test_the_fields_are_the_documented_ones(self):
        """``Finding`` の形は変えない（``chime.check`` や ``chime.cli`` が読む）。"""
        self.assertEqual([field.name for field in dataclasses.fields(Finding)],
                         ["level", "key", "message", "hint", "source"])


class WalkOverridesTest(unittest.TestCase):
    def test_an_empty_override_has_no_findings(self):
        self.assertEqual(walk_overrides({}), [])

    def test_a_non_mapping_override_has_no_findings(self):
        self.assertEqual(walk_overrides(["配列"]), [])

    def test_unknown_key_is_a_warning_with_a_suggestion(self):
        findings = walk_overrides({"schedule": {"hourly": {"minite": 5}}})
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual((finding.level, finding.key), (WARNING, "schedule.hourly.minite"))
        self.assertEqual(finding.hint, "もしかして schedule.hourly.minute?")
        self.assertIn("schedule.hourly.minite", finding.message)

    def test_a_misspelt_section_is_suggested_too(self):
        findings = walk_overrides({"shedule": {"hourly": {"minute": 5}}})
        # 知らない節の中は、比べる相手がないので見ない（1 件だけ）
        self.assertEqual(keys_of(findings), ["shedule"])
        self.assertEqual(findings[0].hint, "もしかして schedule?")

    def test_a_key_in_the_wrong_place_points_at_where_it_belongs(self):
        findings = walk_overrides({"start_hour": 9})
        self.assertEqual(findings[0].hint, "もしかして schedule.hourly.start_hour?")

    def test_a_key_that_exists_in_two_places_suggests_both(self):
        findings = walk_overrides({"minute": 5})
        self.assertEqual(findings[0].hint,
                         "もしかして schedule.hourly.minute か schedule.closing.minute?")

    def test_a_whole_section_in_the_wrong_place_is_suggested(self):
        findings = walk_overrides({"hourly": {"start_hour": 9}})
        self.assertEqual(keys_of(findings), ["hourly"])
        self.assertEqual(findings[0].hint, "もしかして schedule.hourly?")

    def test_without_a_close_match_the_hint_points_at_the_example(self):
        findings = walk_overrides({"zzzzzzzz": 1})
        self.assertEqual(findings[0].level, WARNING)
        self.assertIn("config.example.json", findings[0].hint)

    def test_every_removed_key_is_a_warning(self):
        for dotted_key, guidance in REMOVED_KEYS.items():
            with self.subTest(key=dotted_key):
                findings = walk_overrides(nested(dotted_key, 1))
                # 廃止したキーは「不明」ではなく「廃止」として 1 件だけ報告する
                self.assertEqual(keys_of(findings), [dotted_key])
                self.assertEqual(findings[0].level, WARNING)
                self.assertIn("v6.0.0 で廃止", findings[0].message)
                self.assertIn(guidance, findings[0].message)

    def test_the_contents_of_a_removed_section_are_not_walked(self):
        findings = walk_overrides({"weather": {"jma": {"areas": [], "whatever": 1}}})
        self.assertEqual(keys_of(findings), ["weather.jma"])

    def test_a_value_equal_to_the_default_is_info(self):
        findings = walk_overrides({"schedule": {"hourly": {"start_hour": 10, "end_hour": 15}}})
        self.assertEqual([(f.level, f.key) for f in findings],
                         [(INFO, "schedule.hourly.start_hour")])
        self.assertIn("既定値（10）と同じ", findings[0].message)

    def test_a_list_equal_to_the_default_is_info(self):
        findings = walk_overrides({"schedule": {"closing": {"weekdays": [0, 1, 2, 3, 4]}}})
        self.assertEqual(keys_of(findings), ["schedule.closing.weekdays"])

    def test_true_is_not_the_default_one_and_false_is_not_the_default_zero(self):
        self.assertEqual(walk_overrides({"schedule": {"hourly": {"minute": False}}}), [])
        self.assertEqual(walk_overrides({"extra_segment": {"enabled": 1}}), [])

    def test_a_different_value_is_silent(self):
        self.assertEqual(walk_overrides({"schedule": {"hourly": {"start_hour": 9}}}), [])

    def test_info_findings_match_redundant_keys(self):
        override = {"schedule": {"hourly": {"start_hour": 10, "end_hour": 18, "skip_hours": []},
                                 "max_sleep_seconds": 30.0},
                    "timezone": "Asia/Tokyo", "tts": {"cache_dir": "elsewhere"}}
        found = {finding.key for finding in walk_overrides(override) if finding.level == INFO}
        self.assertEqual(found, set(redundant_keys(override)))

    def test_keys_starting_with_underscore_are_ignored_at_any_depth(self):
        override = {"_comment": "メモ", "schedule": {"_note": {"x": 1}, "hourly": {"_memo": 1}}}
        self.assertEqual(walk_overrides(override), [])

    def test_hour_readings_contents_are_not_walked(self):
        # 既定にない時刻のキー・値の中身は見ない（利用者が自由に足す表）
        self.assertEqual(walk_overrides({"time_signal": {"hour_readings": {"5": "ごじ"}}}), [])

    def test_hour_readings_equal_to_the_default_is_one_info_not_one_per_entry(self):
        findings = walk_overrides({"time_signal": {"hour_readings": dict(
            DEFAULT_CONFIG["time_signal"]["hour_readings"])}})
        self.assertEqual([(f.level, f.key) for f in findings],
                         [(INFO, "time_signal.hour_readings")])

    def test_audio_commands_contents_are_not_walked(self):
        override = {"audio": {"commands": {".ogg": ["ogg123", "{path}"], "_x": 1}}}
        self.assertEqual(walk_overrides(override), [])

    def test_list_elements_are_not_walked(self):
        override = {"weather": {"open_meteo": {"locations": [{"lat": 1, "名前": "x"}]}}}
        self.assertEqual(walk_overrides(override), [])

    def test_source_is_attached_to_every_finding(self):
        findings = walk_overrides(
            {"shedule": 1, "extra_segment": {"mode": "x"}, "timezone": "Asia/Tokyo"},
            "/home/pi/config.json")
        self.assertEqual({f.level for f in findings}, {WARNING, INFO})
        self.assertEqual({f.source for f in findings}, {"/home/pi/config.json"})

    def test_source_defaults_to_empty(self):
        self.assertEqual(walk_overrides({"shedule": 1})[0].source, "")

    def test_the_whole_default_is_shown_in_the_info_message(self):
        """既定値が長い（地点のリスト）ときも、省略せずに見せる（見せるのは書かれた値だけを省略する）。"""
        findings = walk_overrides(nested("weather.open_meteo.locations",
                                         default_at("weather.open_meteo.locations")))
        self.assertIn(json.dumps(default_at("weather.open_meteo.locations"), ensure_ascii=False),
                      findings[0].message)

    def test_a_copy_of_the_defaults_is_all_info(self):
        findings = walk_overrides(copy.deepcopy(DEFAULT_CONFIG))
        self.assertTrue(findings)
        self.assertEqual({finding.level for finding in findings}, {INFO})

    def test_the_input_is_not_modified(self):
        override = {"schedule": {"hourly": {"minite": 5}}, "extra_segment": {"mode": "x"}}
        snapshot = copy.deepcopy(override)
        walk_overrides(override)
        self.assertEqual(override, snapshot)


class ValidateDefaultsTest(unittest.TestCase):
    def test_the_default_config_has_no_findings(self):
        self.assertEqual(validate(Config(DEFAULT_CONFIG)), [])

    def test_the_example_config_has_no_findings(self):
        with open(EXAMPLE_CONFIG_PATH, "r", encoding="utf-8") as handle:
            self.assertEqual(validate(Config(json.load(handle))), [])

    def test_an_empty_config_has_no_findings(self):
        # キーが無い項目は見ない（既定値が使われる）
        self.assertEqual(validate(Config({})), [])

    def test_odd_values_do_not_break_the_check_itself(self):
        config = Config({"timezone": object(), "audio": {"backend": b"mock"},
                         "tts": {"engines": [object()]}})
        findings = validate(config)
        self.assertEqual(sorted(keys_of(findings)), ["audio.backend", "timezone", "tts.engines"])
        # 文字列でない再生方式と、解決できないタイムゾーンはランタイムが壊れる。
        # 知らないエンジン名は、ランタイムが警告して無視する。
        self.assertEqual({f.key: f.level for f in findings},
                         {"audio.backend": ERROR, "timezone": ERROR, "tts.engines": WARNING})


class DocumentedExamplesTest(unittest.TestCase):
    """README と docs の JSON の例が、どれも指摘なしで通ること（例に従った人を警告しない）。"""

    FILES = ["README.md", "docs/SETUP.md", "docs/KNOWLEDGE_BASE.md", "docs/SPECIFICATION.md",
             "docs/REQUIREMENTS.md"]

    def configs(self):
        found = []
        for name in self.FILES:
            path = os.path.join(REPO_ROOT, *name.split("/"))
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            for block in re.findall(r"```json\n(.*?)```", text, re.S):
                try:
                    data = json.loads(block)
                except ValueError:
                    continue
                # 設定ファイルの例だけ（ひとこと・状態ファイルなどの JSON は除く）
                if isinstance(data, dict) and set(data) - {"_comment"} <= set(DEFAULT_CONFIG):
                    found.append((name, data))
        return found

    def test_there_are_examples_to_check(self):
        self.assertGreaterEqual(len(self.configs()), 10)

    def test_every_documented_config_has_no_findings(self):
        for name, data in self.configs():
            with self.subTest(file=name, example=json.dumps(data, ensure_ascii=False)[:60]):
                self.assertEqual(validate(Config(deep_merge(DEFAULT_CONFIG, data))), [])

    def test_the_example_file_has_no_findings_even_as_a_full_copy(self):
        with open(EXAMPLE_CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        fixed, errors = sanitized(Config(data))
        self.assertEqual((errors, fixed.data), ([], data))


class BreakingValuesTest(RuntimeTestCase):
    """error: その値でランタイムが実際に壊れる。置き換えた後は動く。"""

    def test_each_error_breaks_the_real_runtime_and_is_fixed_by_sanitizing(self):
        for label, override, key, stage in BREAKING:
            with self.subTest(label):
                self.assert_breaks(override, key, stage)

    def test_the_table_covers_every_stage_of_the_runtime(self):
        stages = {stage for _, _, _, stage in BREAKING}
        self.assertEqual(stages, {"ログ", "再生方式", "起動", "時計", "起動時の記録",
                                  "VOICEVOX:疎通確認", "VOICEVOX:合成", "予定", "待機",
                                  "再生:mock", "再生:pygame", "再生:command"})

    def test_the_default_config_does_not_break_the_runtime(self):
        run = run_runtime(Config(DEFAULT_CONFIG))
        self.assertEqual(run.failures, {})
        # 放送の組み立ても、予定の計算も、既定の動作どおり（平日 10〜16 時と 16:57 の閉館放送）
        self.assertEqual(run.events["2026-10-09"], [
            "hourly:10", "hourly:11", "hourly:12", "hourly:13", "hourly:14", "hourly:15",
            "hourly:16", "closing"])
        self.assertEqual(run.events["2026-10-10"], [])
        self.assertEqual(run.plans["hourly:10"].spoken[0], "午前10時をお知らせしたのだ。")
        self.assertEqual(run.plans["hourly:16"].spoken[0], "午後よじをお知らせしたのだ。")
        self.assertEqual(run.plans["hourly:12"].spoken[0], "正午をお知らせしたのだ。")

    def test_every_error_message_names_the_key_in_japanese_and_the_default_in_the_hint(self):
        for label, override, key, _ in BREAKING:
            with self.subTest(label):
                found = [f for f in errors_of(override) if f.key == key]
                self.assertTrue(found)
                for finding in found:
                    self.assertRegex(finding.message, _JAPANESE)
                    self.assertIn(key, finding.message)
                    self.assertRegex(finding.hint, _JAPANESE)
                    self.assertIn("既定値", finding.hint)


class ToleratedValuesTest(RuntimeTestCase):
    """warning（または指摘なし）: その値でもランタイムは動く。置き換えず、設定は一切変えない。"""

    def test_each_tolerated_value_runs_and_is_left_alone_by_sanitizing(self):
        for label, override, key in TOLERATED:
            with self.subTest(label):
                found = self.assert_tolerated(override, key)
                if key is not None:
                    self.assertEqual({f.level for f in found}, {WARNING})

    def test_every_warning_message_names_the_key_and_how_to_fix_without_the_error_phrase(self):
        for label, override, key in TOLERATED:
            if key is None:
                continue
            with self.subTest(label):
                for finding in [f for f in warnings_of(override) if f.key == key]:
                    self.assertRegex(finding.message, _JAPANESE)
                    self.assertIn(key, finding.message)
                    self.assertRegex(finding.hint, _JAPANESE)
                    self.assertNotIn("直るまで", finding.hint)
                    self.assertNotIn("直るまで", finding.message)

    def test_a_list_with_a_tolerated_element_is_never_reset(self):
        """リストに warning の要素が混ざっても、リストは作り直さない（error の要素の扱いと違う）。"""
        for key, value in (("schedule.hourly.skip_hours", [12, 24, "13"]),
                           ("schedule.hourly.weekdays", [0, 1, 7, "2"]),
                           ("extra_segment.weather_hours", [12, 24, "x"]),
                           ("weather.open_meteo.locations", [
                               {"label": "大津", "latitude": 35.0045, "longitude": 135.8686},
                               {"label": "x", "latitude": 135.0, "longitude": 35.0}])):
            with self.subTest(key):
                fixed, errors = sanitized(make_config(nested(key, value)))
                self.assertEqual((errors, fixed.get(key)), ([], value))


class HoursAndMinutesTest(RuntimeTestCase):
    HOUR_KEYS = ("schedule.hourly.start_hour", "schedule.hourly.end_hour",
                 "schedule.closing.hour")
    MINUTE_KEYS = ("schedule.hourly.minute", "schedule.closing.minute")

    def test_the_edges_of_the_range_are_accepted(self):
        for key in self.HOUR_KEYS:
            for value in (0, 23):
                with self.subTest(key=key, value=value):
                    # 開始・終了の前後関係の指摘にかからないよう、ほかの値を合わせる
                    override = nested(key, value)
                    if key.endswith("start_hour") and value == 23:
                        override = deep_merge(override, nested("schedule.hourly.end_hour", 23))
                    if key.endswith("end_hour") and value == 0:
                        override = deep_merge(override, nested("schedule.hourly.start_hour", 0))
                    self.assertEqual(validate(make_config(override)), [])
        for key in self.MINUTE_KEYS:
            for value in (0, 59):
                with self.subTest(key=key, value=value):
                    self.assertEqual(validate(make_config(nested(key, value))), [])

    def test_the_closing_hour_and_the_minutes_must_fit_datetime(self):
        """``datetime()`` が受けない値は、予定を作る処理が例外になる（error）。"""
        for key, bad in (("schedule.closing.hour", (-1, 24, 25)),
                         ("schedule.hourly.minute", (-1, 60, 99)),
                         ("schedule.closing.minute", (-1, 60, 99))):
            for value in bad:
                with self.subTest(key=key, value=value):
                    found = errors_of(nested(key, value))
                    self.assertEqual(keys_of(found), [key])
                    self.assertIn(str(value), found[0].message)
                    self.assertRegex(found[0].message, "0〜(23|59)")

    def test_the_hourly_range_outside_0_to_23_is_only_skipped_by_the_scheduler(self):
        """時報の開始・終了の時は、範囲外の時刻を ``Scheduler`` が飛ばすだけ（warning）。"""
        for override, key in (
                (nested("schedule.hourly.start_hour", -1), "schedule.hourly.start_hour"),
                ({"schedule": {"hourly": {"start_hour": 24, "end_hour": 25}}}, "schedule.hourly.start_hour"),
                (nested("schedule.hourly.end_hour", 24), "schedule.hourly.end_hour"),
                (nested("schedule.hourly.end_hour", 1600), "schedule.hourly.end_hour")):
            with self.subTest(override=override):
                found = [f for f in warnings_of(override) if f.key == key]
                self.assertEqual(len(found), 1)
                self.assertIn("0〜23", found[0].message)
                self.assertEqual(errors_of(override), [])

    def test_a_reversed_range_is_explained_by_the_order_not_by_the_range(self):
        """開始が終了より後ろなら、鳴らない理由は順序。範囲外の注意まで重ねない。"""
        found = warnings_of({"schedule": {"hourly": {"start_hour": 24}}})
        self.assertEqual(len(found), 1)
        self.assertIn("1 回も鳴りません", found[0].message)

    def test_end_hour_24_still_chimes_through_23_and_never_at_24(self):
        run = run_runtime(make_config(nested("schedule.hourly.end_hour", 24)))
        self.assertEqual(run.failures, {})
        self.assertEqual(run.events["2026-10-09"][:2], ["hourly:10", "hourly:11"])
        self.assertIn("hourly:23", run.events["2026-10-09"])
        self.assertNotIn("hourly:24", run.events["2026-10-09"])

    def test_text_numbers_are_read_by_the_scheduler_so_they_are_only_warned(self):
        run = run_runtime(make_config({"schedule": {"hourly": {"start_hour": "9"},
                                                    "closing": {"minute": 55.0}}}))
        self.assertEqual(run.failures, {})
        self.assertEqual(run.events["2026-10-09"][0], "hourly:09")
        self.assertEqual(run.events["2026-10-09"][-1], "closing")
        self.assertEqual(run.app.scheduler.events_for_date(FIXED_NOW.date())[-1].at.minute, 55)
        self.assertEqual(keys_of(warnings_of({"schedule": {"hourly": {"start_hour": "9"},
                                                           "closing": {"minute": 55.0}}})),
                         ["schedule.closing.minute", "schedule.hourly.start_hour"])

    def test_values_that_int_cannot_read_are_errors_even_for_the_hourly_range(self):
        for key in ("schedule.hourly.start_hour", "schedule.hourly.end_hour"):
            for value in (None, "x", [10], 1e999, NAN):
                with self.subTest(key=key, value=value):
                    self.assertEqual(keys_of(errors_of(nested(key, value))), [key])

    def test_the_bad_value_is_in_the_message(self):
        found = errors_of({"schedule": {"closing": {"hour": 25}}})
        self.assertIn("25", found[0].message)

    def test_start_hour_after_end_hour_means_no_chime_and_is_a_warning(self):
        config = make_config({"schedule": {"hourly": {"start_hour": 17}}})
        found = [f for f in validate(config) if f.level == WARNING]
        self.assertEqual(keys_of(found), ["schedule.hourly.start_hour"])
        self.assertIn("17", found[0].message)
        self.assertIn("16", found[0].message)
        self.assertIn("1 回も鳴りません", found[0].message)
        self.assertEqual(errors_of({"schedule": {"hourly": {"start_hour": 17}}}), [])
        run = run_runtime(config)
        self.assertEqual(run.failures, {})
        self.assertEqual(run.events["2026-10-09"], ["closing"])  # 時報は 1 回も鳴らず、閉館放送だけ

    def test_start_hour_equal_to_end_hour_is_fine(self):
        override = {"schedule": {"hourly": {"start_hour": 12, "end_hour": 12}}}
        self.assertEqual(validate(make_config(override)), [])

    def test_the_order_is_not_reported_for_a_hourly_section_that_is_off(self):
        override = {"schedule": {"hourly": {"enabled": False, "start_hour": 17}}}
        self.assertEqual(validate(make_config(override)), [])

    def test_reading_order_is_reported_once_for_start_hour_only(self):
        found = validate(make_config({"schedule": {"hourly": {"start_hour": 12, "end_hour": 9}}}))
        self.assertEqual(keys_of(found), ["schedule.hourly.start_hour"])


class HourSpanTest(RuntimeTestCase):
    """時報の時刻を調べる範囲が広すぎる（予定を作るたびに CPU を使い続ける）のは error。"""

    def span_override(self, start, end):
        return {"schedule": {"hourly": {"start_hour": start, "end_hour": end}}}

    def test_the_scheduler_visits_every_hour_of_the_range_for_every_day(self):
        visited = []

        def counting(hourly):
            for hour in range(int(hourly["start_hour"]), int(hourly["end_hour"]) + 1):
                visited.append(hour)
                yield hour

        scheduler = Scheduler(make_config(self.span_override(0, 5000)).section("schedule"), TZ, 3.0)
        with mock.patch("chime.scheduler.hourly_hours", counting):
            scheduler.events_for_date(FIXED_NOW.date())
        self.assertEqual(len(visited), 5001)  # 範囲外の時刻も、飛ばすために 1 つずつ数える

    def test_a_range_at_the_limit_is_a_warning_and_beyond_it_is_an_error(self):
        limit = configcheck._MAX_HOUR_SPAN
        at_limit = make_config(self.span_override(0, limit - 1))
        beyond = make_config(self.span_override(0, limit))
        self.assertEqual([(f.level, f.key) for f in validate(at_limit)],
                         [(WARNING, "schedule.hourly.end_hour")])
        found = [f for f in validate(beyond) if f.level == ERROR]
        self.assertEqual(keys_of(found), ["schedule.hourly.end_hour"])
        self.assertIn("CPU", found[0].message)

    def test_a_large_range_that_the_scheduler_still_handles_is_left_alone(self):
        # 10 万件でも 1 日分を数えるのは数ミリ秒。調べる範囲が広いだけで、動作は変わらない
        override = self.span_override(0, 100000)
        run = run_runtime(make_config(override))
        self.assertEqual(run.failures, {})
        self.assertEqual(len(run.events["2026-10-09"]), 24 + 1)  # 0〜23 時の 24 回と閉館放送
        fixed, errors = sanitized(make_config(override))
        self.assertEqual(errors, [])

    def test_the_key_outside_0_to_23_is_blamed_and_the_good_one_is_kept(self):
        fixed, errors = sanitized(make_config(self.span_override(0, 10 ** 9)))
        self.assertEqual(keys_of(errors), ["schedule.hourly.end_hour"])
        self.assertEqual((fixed.get("schedule.hourly.start_hour"), fixed.get("schedule.hourly.end_hour")),
                         (0, 16))
        fixed, errors = sanitized(make_config(self.span_override(-10 ** 9, 12)))
        self.assertEqual(keys_of(errors), ["schedule.hourly.start_hour"])
        self.assertEqual((fixed.get("schedule.hourly.start_hour"), fixed.get("schedule.hourly.end_hour")),
                         (10, 12))

    def test_both_keys_are_blamed_when_both_are_outside(self):
        fixed, errors = sanitized(make_config(self.span_override(-10 ** 9, 10 ** 9)))
        self.assertEqual(sorted(keys_of(errors)),
                         ["schedule.hourly.end_hour", "schedule.hourly.start_hour"])
        self.assertEqual((fixed.get("schedule.hourly.start_hour"), fixed.get("schedule.hourly.end_hour")),
                         (10, 16))

    def test_a_huge_range_never_finishes_and_the_replacement_does(self):
        """置き換え前の設定では、予定を作る処理が終わらない（別プロセスで 2 秒だけ待って確かめる）。"""
        code = "\n".join([
            "import json, sys",
            "from datetime import date",
            "from chime.scheduler import Scheduler",
            "settings = json.loads(sys.argv[1])",
            "Scheduler(settings, None, 3.0).events_for_date(date(2026, 10, 9))",
        ])

        def run(config, timeout):
            return subprocess.run(
                [sys.executable, "-c", code, json.dumps(config.section("schedule"))],
                cwd=REPO_ROOT, timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        config = make_config(self.span_override(0, 10 ** 400))
        with self.assertRaises(subprocess.TimeoutExpired):
            run(config, 2)
        fixed, errors = sanitized(config)
        self.assertEqual(keys_of(errors), ["schedule.hourly.end_hour"])
        self.assertEqual(run(fixed, 30).returncode, 0)

    def test_the_span_error_applies_even_when_the_hourly_section_is_off(self):
        """作り置きの文言を数える処理は、``enabled`` に関わらず時刻の範囲を 1 つずつ数える。"""
        override = {"schedule": {"hourly": {"enabled": False, "start_hour": 0, "end_hour": 5000}}}
        with mock.patch.object(timesignal, "announce_text", return_value="文言") as announce:
            list(phrases.announcement_phrases(make_config(override)))
        self.assertEqual(announce.call_count, 5001)
        too_wide = {"schedule": {"hourly": {"enabled": False, "start_hour": 0, "end_hour": 10 ** 9}}}
        self.assertEqual(keys_of(errors_of(too_wide)), ["schedule.hourly.end_hour"])

    def test_a_non_integer_hour_breaks_the_phrase_list_even_when_the_hourly_section_is_off(self):
        override = {"schedule": {"hourly": {"enabled": False, "start_hour": "x"}}}
        self.assertEqual(keys_of(errors_of(override)), ["schedule.hourly.start_hour"])
        with self.assertRaises(ValueError):
            list(phrases.announcement_phrases(make_config(override)))


class WeekdaysTest(RuntimeTestCase):
    SECTIONS = (("schedule.hourly.weekdays", "hourly"), ("schedule.closing.weekdays", "closing"))
    #: 固定の「いま」（金曜）から 8 日間のうち、平日の日付
    WEEKDAYS = ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]

    def hourly_days(self, run):
        return [day for day, keys in sorted(run.events.items())
                if any(key.startswith("hourly:") for key in keys)]

    def closing_days(self, run):
        return [day for day, keys in sorted(run.events.items()) if "closing" in keys]

    def test_valid_lists_are_fine(self):
        for key, _ in self.SECTIONS:
            for value in ([], [0], [0, 1, 2, 3, 4, 5, 6]):
                with self.subTest(key=key, value=value):
                    self.assertEqual(validate(make_config(nested(key, value))), [])

    def test_string_weekdays_never_equal_a_weekday_number_so_those_days_do_not_fire(self):
        run = run_runtime(make_config(nested("schedule.hourly.weekdays", ["0", "1", "2", "3", "4"])))
        self.assertEqual(run.failures, {})
        self.assertEqual(self.hourly_days(run), [])  # 時報はどの日にも鳴らない
        self.assertEqual(self.closing_days(run), self.WEEKDAYS)  # 閉館放送は影響なし
        found = warnings_of(nested("schedule.hourly.weekdays", ["0", "1", "2", "3", "4"]))
        self.assertEqual(keys_of(found), ["schedule.hourly.weekdays"])
        message = found[0].message
        for name in ("月曜", "火曜", "水曜", "木曜", "金曜"):
            self.assertIn(name, message)  # 書いたつもりの曜日を挙げる
        self.assertIn("どの曜日にも時報は鳴りません", message)
        self.assertIn("0〜6 の整数", found[0].hint)

    def test_a_string_for_the_closing_weekdays_silences_only_the_closing_broadcast(self):
        run = run_runtime(make_config(nested("schedule.closing.weekdays", ["0", "4"])))
        self.assertEqual(self.closing_days(run), [])
        self.assertEqual(self.hourly_days(run), self.WEEKDAYS)
        found = warnings_of(nested("schedule.closing.weekdays", ["0", "4"]))
        self.assertIn("どの曜日にも閉館放送は鳴りません", found[0].message)

    def test_seven_is_not_a_weekday_so_sunday_is_the_day_that_does_not_fire(self):
        override = nested("schedule.hourly.weekdays", [0, 1, 2, 3, 4, 5, 7])
        run = run_runtime(make_config(override))
        self.assertEqual(self.hourly_days(run), sorted(self.WEEKDAYS + ["2026-10-10"]))  # 土曜も鳴る
        self.assertNotIn("2026-10-11", self.hourly_days(run))  # 日曜
        found = warnings_of(override)
        self.assertIn("7", found[0].message)
        self.assertIn("時報が鳴る曜日は 月・火・水・木・金・土、鳴らない曜日は 日 です", found[0].message)
        self.assertIn("6=日曜", found[0].hint)

    def test_floats_and_booleans_equal_the_numbers_so_they_still_fire(self):
        override = nested("schedule.hourly.weekdays", [1.0, True])
        run = run_runtime(make_config(override))
        self.assertEqual(self.hourly_days(run), ["2026-10-13"])  # 火曜
        found = warnings_of(override)
        self.assertIn("整数ではありません", found[0].message)
        self.assertEqual(sanitized(make_config(override))[0].get("schedule.hourly.weekdays"), [1.0, True])

    def test_a_long_list_of_unmatched_elements_is_shortened_in_the_message(self):
        found = warnings_of(nested("schedule.hourly.weekdays", [str(n) for n in range(10)] + ["x" * 100]))
        self.assertIn("ほか 6 件", found[0].message)
        self.assertLess(len(found[0].message), 400)

    def test_a_bare_string_is_read_one_character_at_a_time(self):
        found = warnings_of(nested("schedule.hourly.weekdays", "01234"))
        self.assertIn("リストではなく", found[0].message)
        self.assertEqual(self.hourly_days(run_runtime(make_config(
            nested("schedule.hourly.weekdays", "01234")))), [])

    def test_values_that_set_cannot_hold_are_errors(self):
        for key, _ in self.SECTIONS:
            for value in (3, None, True, 1.5, [0, [1]], [{}]):
                with self.subTest(key=key, value=value):
                    self.assertEqual(keys_of(errors_of(nested(key, value))), [key])

    def test_the_unusable_element_is_named(self):
        found = errors_of(nested("schedule.hourly.weekdays", [0, [1, 2]]))
        self.assertIn("[[1, 2]]", found[0].message)

    def test_an_empty_list_means_no_day_and_is_left_alone(self):
        run = run_runtime(make_config(nested("schedule.hourly.weekdays", [])))
        self.assertEqual(run.failures, {})
        self.assertEqual(self.hourly_days(run), [])


class SkipHoursTest(RuntimeTestCase):
    KEY = "schedule.hourly.skip_hours"

    def hours(self, run):
        return [key for key in run.events["2026-10-09"] if key.startswith("hourly:")]

    def test_an_out_of_range_hour_never_matches_so_the_other_skips_still_work(self):
        override = nested(self.KEY, [12, 24])
        run = run_runtime(make_config(override))
        self.assertEqual(run.failures, {})
        self.assertEqual(self.hours(run), ["hourly:10", "hourly:11", "hourly:13", "hourly:14",
                                           "hourly:15", "hourly:16"])  # 12 時だけが休み
        found = warnings_of(override)
        self.assertEqual(keys_of(found), [self.KEY])
        self.assertIn("[24]", found[0].message)
        self.assertIn("0〜23", found[0].message)
        self.assertEqual(sanitized(make_config(override))[0].get(self.KEY), [12, 24])

    def test_text_and_float_hours_are_read_by_int_so_they_still_skip(self):
        for value in (["12"], [12.0]):
            with self.subTest(value=value):
                run = run_runtime(make_config(nested(self.KEY, value)))
                self.assertNotIn("hourly:12", self.hours(run))
                found = warnings_of(nested(self.KEY, value))
                self.assertIn("整数ではありません", found[0].message)

    def test_a_bare_string_skips_the_characters_not_the_hour(self):
        override = nested(self.KEY, "12")
        run = run_runtime(make_config(override))
        self.assertIn("hourly:12", self.hours(run))  # 12 時は休みにならない
        found = warnings_of(override)
        self.assertIn("リストではなく", found[0].message)
        self.assertIn("[1, 2]", found[0].message)

    def test_null_and_empty_mean_no_skip(self):
        for value in (None, [], 0, ""):
            with self.subTest(value=value):
                self.assertEqual(validate(make_config(nested(self.KEY, value))), [])
                self.assertEqual(run_runtime(make_config(nested(self.KEY, value))).failures, {})

    def test_an_element_int_cannot_read_breaks_the_scheduler(self):
        for value in ([12, "x"], [None], [[1]], [{}], ["a", 12]):
            with self.subTest(value=value):
                self.assertEqual(keys_of(errors_of(nested(self.KEY, value))), [self.KEY])
                self.assertIn("予定", runtime_failures(make_config(nested(self.KEY, value))))

    def test_values_that_cannot_be_iterated_break_the_scheduler(self):
        for value in (5, True, 1.5):
            with self.subTest(value=value):
                self.assertEqual(keys_of(errors_of(nested(self.KEY, value))), [self.KEY])
                self.assertIn("予定", runtime_failures(make_config(nested(self.KEY, value))))


class ListRepairTest(RuntimeTestCase):
    """error の要素があるリストは、動くのに必要な分だけ（読めない要素だけ）を取り除く。

    warning の要素（範囲外・書き方の違い）は残す。リストでない値や、要素を取り除けない値は
    既定値に戻す。取り除くのは要素だけにするのは、前の版が動かしていた設定の、読める部分
    （休みにした正午や、鳴らす土曜）を失わないため。
    """

    def fixed(self, override, key):
        fixed, errors = sanitized(make_config(override))
        self.assertEqual(keys_of(errors), [key])
        return fixed.get(key)

    def test_skip_hours_keeps_every_readable_element(self):
        key = "schedule.hourly.skip_hours"
        self.assertEqual(self.fixed(nested(key, [12, "x", 24, None, [1], "13"]), key), [12, 24, "13"])
        # 実際に、正午の休みは残り、13 時も休みになる
        fixed, _ = sanitized(make_config(nested(key, [12, "x", "13"])))
        keys = run_runtime(fixed).events["2026-10-09"]
        self.assertEqual([k for k in keys if k.startswith("hourly:")],
                         ["hourly:10", "hourly:11", "hourly:14", "hourly:15", "hourly:16"])

    def test_skip_hours_that_is_not_a_list_comes_back_as_the_default(self):
        key = "schedule.hourly.skip_hours"
        for value in (5, True, "none", {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(self.fixed(nested(key, value), key), [])

    def test_weekdays_keeps_every_hashable_element(self):
        key = "schedule.hourly.weekdays"
        self.assertEqual(self.fixed(nested(key, [0, 1, [2], {}, "3", 7]), key), [0, 1, "3", 7])
        fixed, _ = sanitized(make_config(nested(key, [0, 1, 2, 3, 4, 5, [6]])))
        self.assertEqual(fixed.get(key), [0, 1, 2, 3, 4, 5])  # 土曜の時報は残る
        self.assertEqual(run_runtime(fixed).events["2026-10-10"][0], "hourly:10")

    def test_closing_weekdays_is_repaired_the_same_way(self):
        key = "schedule.closing.weekdays"
        self.assertEqual(self.fixed(nested(key, [0, [1], 5]), key), [0, 5])

    def test_weekdays_that_is_not_a_list_comes_back_as_the_default(self):
        for key in ("schedule.hourly.weekdays", "schedule.closing.weekdays"):
            for value in (3, None, True):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.fixed(nested(key, value), key), [0, 1, 2, 3, 4])

    def test_external_commands_keep_the_readable_entries(self):
        """読めない項目は、その拡張子の既定の項目に置き換え（既定に無い拡張子なら取り除き）、読める項目は残す。"""
        key = "audio.commands"
        self.assertEqual(self.fixed(nested(key, {".wav": 1, ".mp3": ["mpg123", "{path}"], ".ogg": None}), key),
                         {".wav": ["aplay", "-q", "{path}"], ".mp3": ["mpg123", "{path}"]})
        self.assertEqual(self.fixed(nested(key, None), key), default_at(key))

    def test_the_original_config_is_not_modified_and_the_result_is_a_copy(self):
        config = make_config(nested("schedule.hourly.skip_hours", [12, "x"]))
        snapshot = copy.deepcopy(config.data)
        fixed, _ = sanitized(config)
        self.assertEqual(config.data, snapshot)
        fixed.data["schedule"]["hourly"]["skip_hours"].append(99)
        self.assertEqual(config.data, snapshot)

    def test_the_hint_says_only_the_unreadable_elements_are_dropped(self):
        found = errors_of(nested("schedule.hourly.skip_hours", [12, "x"]))
        self.assertIn("読めない要素だけを取り除き", found[0].hint)
        self.assertIn("直るまでは既定値 [] で動かします", found[0].hint)  # 取り除けないときの行き先
        found = errors_of(nested("schedule.hourly.weekdays", 3))
        self.assertIn("直るまでは既定値 [0, 1, 2, 3, 4] で動かします", found[0].hint)

    def test_a_repair_that_fails_falls_back_to_the_default(self):
        def broken(value):
            raise RuntimeError("直せない")

        with mock.patch.dict(configcheck._REPAIRS, {"schedule.hourly.skip_hours": broken}):
            fixed, errors = sanitized(make_config(nested("schedule.hourly.skip_hours", [12, "x"])))
        self.assertEqual(keys_of(errors), ["schedule.hourly.skip_hours"])
        self.assertEqual(fixed.get("schedule.hourly.skip_hours"), [])


class WeatherHoursTest(RuntimeTestCase):
    KEY = "extra_segment.weather_hours"

    def test_a_scalar_means_the_weather_part_is_skipped_by_the_guard(self):
        run = run_runtime(make_config(nested(self.KEY, 12)))
        self.assertEqual(run.failures, {})
        plan = run.plans["hourly:12"]
        self.assertTrue(any("天気予報" in warning for warning in plan.warnings))
        self.assertTrue(plan.spoken)  # 時報の読み上げとひとことは鳴る
        found = warnings_of(nested(self.KEY, 12))
        self.assertIn("天気予報は流れません", found[0].message)

    def test_unreadable_elements_are_ignored_and_the_rest_still_apply(self):
        override = nested(self.KEY, ["x", 12])
        found = warnings_of(override)
        self.assertIn("無視されます", found[0].message)
        self.assertIn('["x"]', found[0].message)

    def test_an_hour_outside_0_to_23_never_matches(self):
        found = warnings_of(nested(self.KEY, [12, 24]))
        self.assertIn("[24]", found[0].message)
        self.assertIn("天気は流れません", found[0].message)

    def test_a_bare_string_is_read_one_character_at_a_time(self):
        found = warnings_of(nested(self.KEY, "12"))
        self.assertIn("[1, 2]", found[0].message)

    def test_the_default_and_the_documented_example_are_fine(self):
        for value in ([12], [10, 12, 14, 16], []):
            self.assertEqual(validate(make_config(nested(self.KEY, value))), [])


class LeadThresholdTest(RuntimeTestCase):
    """日時の計算が範囲（1〜9999 年）を出る境目を、本物の ``Scheduler`` と突き合わせる。"""

    SECONDS = (0, 1, 45, 3600, 10 ** 7, 10 ** 9, 10 ** 10, 3 * 10 ** 10, 5 * 10 ** 10,
               63 * 10 ** 9, 64 * 10 ** 9, 7 * 10 ** 10, 10 ** 11, 10 ** 12, 10 ** 14, 10 ** 18,
               10 ** 30, 1e300, INF, NAN)

    def crashes(self, override):
        """``Scheduler`` で予定を計算して、例外になるか（起動時の ``lead_seconds`` も含む）。"""
        config = make_config(override)
        now = FIXED_NOW.replace(tzinfo=TZ)
        try:
            lead = timesignal.lead_seconds(config.section("time_signal"))
            scheduler = Scheduler(config.section("schedule"), TZ, lead, clock=lambda: now)
            scheduler.upcoming(now, limit=12)
            scheduler.next_event(now)
            scheduler.events_for_date(FIXED_NOW.date())
        except Exception:
            return True
        return False

    def test_the_real_threshold_for_each_seconds_key(self):
        for key in ("schedule.pip_lead_seconds", "schedule.prepare_lead_seconds",
                    "schedule.catchup_grace_seconds"):
            for sign in (1, -1):
                for seconds in self.SECONDS:
                    value = seconds if seconds != seconds else sign * seconds  # NaN は符号なし
                    override = nested(key, value)
                    with self.subTest(key=key, seconds=value):
                        self.assertEqual(self.crashes(override), key in keys_of(errors_of(override)),
                                         "Scheduler が壊れるかと、error かが食い違う")

    def test_the_threshold_is_the_distance_to_year_1(self):
        # 2026-10-09 から 63e9 秒（約 1996 年）前は西暦 30 年ごろ。64e9 秒前は紀元前
        self.assertFalse(self.crashes(nested("schedule.prepare_lead_seconds", 63e9)))
        self.assertTrue(self.crashes(nested("schedule.prepare_lead_seconds", 64e9)))
        self.assertEqual(errors_of(nested("schedule.prepare_lead_seconds", 63e9)), [])
        self.assertEqual(keys_of(errors_of(nested("schedule.prepare_lead_seconds", 64e9))),
                         ["schedule.prepare_lead_seconds"])

    def test_the_threshold_follows_the_date(self):
        """日付が進めば、同じ値が範囲内になりうる（判定は今の日付で確かめる）。"""
        far = datetime(5000, 1, 1)
        with mock.patch.object(configcheck, "_reference_moments",
                               lambda: (far - timedelta(days=1), far + timedelta(days=31))):
            self.assertEqual(errors_of(nested("schedule.prepare_lead_seconds", 7 * 10 ** 10)), [])
        self.assertEqual(keys_of(errors_of(nested("schedule.prepare_lead_seconds", 7 * 10 ** 10))),
                         ["schedule.prepare_lead_seconds"])

    def test_each_seconds_key_alone_is_fine_but_the_sum_can_overflow(self):
        override = {"schedule": {"pip_lead_seconds": 35 * 10 ** 9, "prepare_lead_seconds": 35 * 10 ** 9}}
        self.assertTrue(self.crashes(override))
        self.assertEqual(keys_of(errors_of(override)), ["schedule.prepare_lead_seconds"])
        fixed = self.assert_breaks(override, "schedule.prepare_lead_seconds", "予定")
        self.assertEqual(fixed.get("schedule.pip_lead_seconds"), 35 * 10 ** 9)  # 先行はそのまま
        self.assertEqual(fixed.get("schedule.prepare_lead_seconds"), 45.0)

    def test_the_short_pip_count_and_interval_decide_the_lead_when_pip_lead_is_null(self):
        counts = (3, 10 ** 6, 10 ** 9, 10 ** 10, 60 * 10 ** 9, 70 * 10 ** 9, 10 ** 12, 10 ** 20, 10 ** 400)
        intervals = (1000, 10 ** 6, 10 ** 9, 15 * 10 ** 12, 25 * 10 ** 12, 1e300, INF, NAN)
        for key, values in (("time_signal.short_pip_count", counts),
                            ("time_signal.pip_interval_ms", intervals)):
            for value in values:
                override = nested(key, value)
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.crashes(override), key in keys_of(errors_of(override)))

    def test_with_an_explicit_pip_lead_only_an_unreadable_product_breaks_startup(self):
        explicit = {"schedule": {"pip_lead_seconds": 3}}
        # 短音の数が巨大でも、先行の秒数が決まっていれば予定の計算には使われない
        override = deep_merge(explicit, nested("time_signal.short_pip_count", 10 ** 20))
        self.assertFalse(self.crashes(override))
        self.assertEqual(errors_of(override), [])
        # ただし起動時の ``lead_seconds`` の掛け算は、先行の秒数に関わらず実行される
        override = deep_merge(explicit, nested("time_signal.short_pip_count", HUGE))
        self.assertTrue(self.crashes(override))
        self.assertEqual(keys_of(errors_of(override)), ["time_signal.short_pip_count"])

    def test_a_product_that_overflows_only_when_multiplied_blames_the_short_pip_count(self):
        override = {"time_signal": {"short_pip_count": 10 ** 5, "pip_interval_ms": 10 ** 9}}  # 1e14 秒
        self.assertTrue(self.crashes(override))
        self.assertEqual(keys_of(errors_of(override)), ["time_signal.short_pip_count"])
        fixed = self.assert_breaks(override, "time_signal.short_pip_count", "予定")
        self.assertEqual(fixed.get("time_signal.pip_interval_ms"), 10 ** 9)

    def test_the_interval_is_blamed_when_it_alone_is_too_big(self):
        override = nested("time_signal.pip_interval_ms", 1e300)
        self.assertEqual(keys_of(errors_of(override)), ["time_signal.pip_interval_ms"])

    def test_an_invalid_pip_lead_does_not_hide_a_product_that_then_overflows(self):
        """先行の秒数が置き換えられると、短音の数 × 間隔が予定に使われる。それも壊れるなら、まとめて直す。"""
        override = {"schedule": {"pip_lead_seconds": 1e12}, "time_signal": {"short_pip_count": 10 ** 20}}
        fixed, errors = sanitized(make_config(override))
        self.assertEqual(sorted(keys_of(errors)), ["schedule.pip_lead_seconds", "time_signal.short_pip_count"])
        self.assertFalse(self.crashes(fixed.data))

    def test_the_catchup_grace_replays_old_broadcasts_but_only_overflow_is_an_error(self):
        # 巨大でも日時が範囲内なら、ランタイムは（最も古い未再生から）さかのぼって鳴らす。動くので error にしない
        override = nested("schedule.catchup_grace_seconds", 10 ** 9)
        self.assertEqual(errors_of(override), [])
        scheduler = Scheduler(make_config(override).section("schedule"), TZ, 3.0)
        first = scheduler.next_event(FIXED_NOW.replace(tzinfo=TZ))
        self.assertLess(first.at.year, 2000)

    def test_the_message_says_what_goes_wrong(self):
        found = errors_of(nested("schedule.prepare_lead_seconds", 1e12))
        self.assertIn("日時の計算が例外", found[0].message)
        self.assertIn("1〜9999 年", found[0].message)
        self.assertIn("直るまでは既定値 45.0 で動かします", found[0].hint)


class MaxSleepTest(RuntimeTestCase):
    KEY = "schedule.max_sleep_seconds"

    def wakeups(self, max_sleep, minutes=1.0, cap=100000):
        """``minutes`` 分の待機で、停止要求を確かめに起きる回数。"""
        return count_wakeups({"max_sleep_seconds": max_sleep}, seconds=minutes * 60, cap=cap)

    def test_the_loop_wakes_once_per_max_sleep_seconds(self):
        self.assertEqual(self.wakeups(30), 2)
        self.assertEqual(self.wakeups(1), 60)
        self.assertEqual(self.wakeups(0.5), 120)  # 1 秒に 2 回起きる

    def test_zero_or_negative_never_advances_so_it_spins(self):
        for value in (0, -5, -0.0):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    self.wakeups(value, cap=1000)

    def test_one_second_or_more_is_fine(self):
        for value in (1, 1.0, 30.0, 600):
            with self.subTest(value=value):
                self.assertEqual(errors_of(nested(self.KEY, value)), [])

    def test_less_than_one_second_is_an_error(self):
        for value in (0.99, 0.5, 0, -5, False, "0", "0.5"):
            with self.subTest(value=value):
                found = errors_of(nested(self.KEY, value))
                self.assertEqual(keys_of(found), [self.KEY])
                self.assertIn("CPU", found[0].message)

    def test_a_non_number_is_an_error_but_nan_and_infinity_are_only_read_oddly(self):
        for value in ("abc", None, [30], {}):
            with self.subTest(value=value):
                self.assertEqual(keys_of(errors_of(nested(self.KEY, value))), [self.KEY])
        for value in (NAN, INF):
            with self.subTest(value=value):
                self.assertEqual(errors_of(nested(self.KEY, value)), [])
                self.assertEqual(keys_of(warnings_of(nested(self.KEY, value))), [self.KEY])
        # NaN や無限大は ``min(remaining, max_sleep)`` で残りの秒数になり、待機は 1 回で済む
        self.assertEqual(self.wakeups(NAN), 1)
        self.assertEqual(self.wakeups(INF), 1)


#: 数値のキーごとに、素の読めない値でランタイムが壊れる段階。``None`` は、ランタイムの ``_guard`` などが
#: 受け止める（その部品だけが失敗し、放送は続く）ので warning。
NUMBER_STAGES = {
    "schedule.max_sleep_seconds": "起動",
    "schedule.pip_lead_seconds": "起動",
    "schedule.prepare_lead_seconds": "起動",
    "schedule.catchup_grace_seconds": "起動",
    "audio.mixer.frequency": "再生:pygame",
    "audio.mixer.size": "再生:pygame",
    "audio.mixer.channels": "再生:pygame",
    "audio.mixer.buffer": "再生:pygame",
    "audio.gap_ms": "起動時の記録",
    "audio.fade_in_ms": None,
    "audio.mock_max_seconds": "再生:mock",
    "time_signal.short_pip.frequency": None,
    "time_signal.short_pip.duration_ms": None,
    "time_signal.long_pip.frequency": None,
    "time_signal.long_pip.duration_ms": None,
    "time_signal.short_pip_count": "起動",
    "time_signal.pip_interval_ms": "起動",
    "time_signal.volume": None,
    "time_signal.envelope_ms": None,
    "quotes.avoid_recent": "起動",
    "weather.timeout_seconds": "起動",
    "weather.cache_minutes": "起動",
    "tts.voicevox.speaker": "起動",
    "tts.voicevox.timeout_seconds": "起動",
    "tts.voicevox.probe_timeout_seconds": "起動",
}


class NumbersTest(RuntimeTestCase):
    """数値で読むキー。ランタイムの ``int()`` / ``float()`` が読めるかで、error か warning かが決まる。"""

    def test_every_numeric_default_has_a_rule(self):
        """既定設定の数値は、どれも検査の対象（新しいキーを足したときに、分類を忘れない）。"""
        covered = ({rule.key for rule in configcheck._NUMBERS}
                   | {field.key for field in configcheck._FIELDS}
                   | {configcheck._START_HOUR, configcheck._END_HOUR}
                   | {"weather.prerecord.temp_min", "weather.prerecord.temp_max",
                      "weather.prerecord.pop_step"})
        numeric = {path for path, base in configcheck._default_paths()
                   if isinstance(base, (int, float)) and not isinstance(base, bool)}
        self.assertEqual(numeric - covered, set())
        self.assertEqual(set(NUMBER_STAGES), {rule.key for rule in configcheck._NUMBERS})

    def test_fatal_matches_whether_the_runtime_breaks(self):
        for rule in configcheck._NUMBERS:
            with self.subTest(rule.key):
                self.assertEqual(rule.fatal, NUMBER_STAGES[rule.key] is not None)

    def test_text_that_is_not_a_number_breaks_where_the_runtime_reads_it(self):
        for rule in configcheck._NUMBERS:
            stage = NUMBER_STAGES[rule.key]
            if stage is None:
                continue
            for bad in ("abc", [1]):
                with self.subTest(key=rule.key, value=bad):
                    fixed = self.assert_breaks(nested(rule.key, bad), rule.key, stage)
                    self.assertEqual(fixed.get(rule.key), default_at(rule.key))
                    found = errors_of(nested(rule.key, bad))
                    self.assertIn("整数" if rule.integer else "数値", found[0].message)

    def test_numbers_written_as_text_floats_or_booleans_are_read_so_they_are_only_warned(self):
        for rule in configcheck._NUMBERS:
            default = default_at(rule.key)
            default = 3 if default is None else default
            forms = ["{0}".format(default)]
            if rule.integer:
                forms.append(float(default))
            forms.append(True)
            for value in forms:
                with self.subTest(key=rule.key, value=value):
                    found = self.assert_tolerated(nested(rule.key, value), rule.key)
                    self.assertEqual(len(found), 1)
                    self.assertIn("として読みます", found[0].message)

    def test_plain_numbers_have_no_findings(self):
        for rule in configcheck._NUMBERS:
            default = default_at(rule.key)
            for value in (default, 5, 7.5 if not rule.integer else 6):
                if value is None or (rule.minimum is not None and value < rule.minimum):
                    continue
                with self.subTest(key=rule.key, value=value):
                    self.assertEqual(validate(make_config(nested(rule.key, value))), [])

    def test_only_the_integer_keys_cut_a_fraction(self):
        found = warnings_of(nested("audio.gap_ms", 350.5))
        self.assertIn("小数部は切り捨てて 350 として読みます", found[0].message)
        found = warnings_of(nested("weather.timeout_seconds", "7.5"))
        self.assertIn("7.5 として読みます", found[0].message)

    def test_integers_are_accepted_for_number_keys(self):
        self.assertEqual(errors_of({"weather": {"timeout_seconds": 8}}), [])

    def test_pip_lead_seconds_may_be_null(self):
        self.assertEqual(validate(make_config({"schedule": {"pip_lead_seconds": None}})), [])
        self.assertEqual(validate(make_config({"schedule": {"pip_lead_seconds": 3.0}})), [])
        self.assertEqual(keys_of(errors_of({"schedule": {"pip_lead_seconds": "x"}})),
                         ["schedule.pip_lead_seconds"])

    def test_other_keys_may_not_be_null(self):
        self.assertEqual(keys_of(errors_of({"schedule": {"prepare_lead_seconds": None}})),
                         ["schedule.prepare_lead_seconds"])

    def test_the_voicevox_keys_are_only_read_when_voicevox_is_listed(self):
        for key in ("tts.voicevox.speaker", "tts.voicevox.timeout_seconds",
                    "tts.voicevox.probe_timeout_seconds"):
            with self.subTest(key):
                listed = nested(key, "x")
                self.assertEqual(keys_of(errors_of(listed)), [key])
                unlisted = deep_merge(listed, {"tts": {"engines": ["prerecorded"]}})
                self.assertEqual(errors_of(unlisted), [])
                found = self.assert_tolerated(unlisted, key)
                self.assertIn("voicevox が無い", found[0].message)

    def test_the_mock_and_gap_limits(self):
        # 負の値や、待てないほど大きい値は、``time.sleep`` が例外にする
        self.assertEqual(keys_of(errors_of(nested("audio.gap_ms", -1))), ["audio.gap_ms"])
        edge = configcheck._MAX_GAP_MS
        self.assertEqual(errors_of(nested("audio.gap_ms", edge)), [])
        self.assertEqual(keys_of(errors_of(nested("audio.gap_ms", edge + 1000))), ["audio.gap_ms"])
        with self.assertRaises(OverflowError):
            time_sleep(edge / 1000.0 + 1)
        self.assertEqual(errors_of(nested("audio.mock_max_seconds", 0)), [])

    TIMEOUT_KEYS = ("tts.voicevox.probe_timeout_seconds", "tts.voicevox.timeout_seconds")

    def test_the_voicevox_timeouts_stop_at_what_a_socket_accepts(self):
        """待ち時間が大きすぎると ``socket.settimeout`` が ``OverflowError`` にし、``urlopen`` はそれを通信の失敗に直さない。"""
        edge = configcheck._MAX_TIMEOUT_SECONDS
        # 本物のソケットの境界（約 9.223e9 秒。負の値は ValueError か、大きければ OverflowError）
        with contextlib.closing(socket.socket()) as sock:
            sock.settimeout(edge)
            sock.settimeout(9e9)
            for beyond in (edge + 1, 1e12, INF):
                with self.assertRaises(OverflowError):
                    sock.settimeout(beyond)
            with self.assertRaises(ValueError):
                sock.settimeout(-edge)
            for beyond in (-edge - 1, -1e12, -INF):
                with self.assertRaises(OverflowError):
                    sock.settimeout(beyond)
        for key in self.TIMEOUT_KEYS:
            for fine in (0, 2.0, 86400, 9e9, edge, -1, -edge, NAN):
                with self.subTest(key=key, value=fine):
                    self.assertEqual(errors_of(nested(key, fine)), [])
            for bad in (edge + 1, -edge - 1, 1e12, -1e12, 9.3e9, INF, -INF):
                with self.subTest(key=key, value=bad):
                    found = errors_of(nested(key, bad))
                    self.assertEqual(keys_of(found), [key])
                    self.assertIn("VOICEVOX ENGINE", found[0].message)
                    self.assertIn("既定値 {0}".format(default_at(key)), found[0].hint)

    def test_a_timeout_that_the_socket_rejects_is_replaced_by_the_default(self):
        for key in self.TIMEOUT_KEYS:
            with self.subTest(key=key):
                fixed, errors = sanitized(make_config(nested(key, 1e12)))
                self.assertEqual(keys_of(errors), [key])
                self.assertEqual(fixed.get(key), default_at(key))

    def test_a_large_but_working_timeout_is_not_replaced(self):
        """前の版が動かしていた値（ふつうは現実的でなくても）は、置き換えて動作を変えない。"""
        for key in self.TIMEOUT_KEYS:
            with self.subTest(key=key):
                fixed, errors = sanitized(make_config(nested(key, 9e9)))
                self.assertEqual((errors, fixed.get(key)), ([], 9e9))

    def test_the_weather_timeout_stops_at_what_a_socket_accepts_but_only_warns(self):
        """天気の待ち時間も、大きすぎると ``socket.settimeout`` が ``OverflowError`` にする。warning で、置き換えない。

        ``fetch_json`` はそれを ``WeatherError`` に直さないが、放送を組み立てる側の ``_guard`` が
        受け止めるので、放送は続く（正午の天気予報が毎日飛ばされるだけ。前の版も同じ）。
        VOICEVOX の待ち時間（error）と同じ範囲を境にして、範囲を出たら warning にする。
        """
        key = "weather.timeout_seconds"
        edge = configcheck._MAX_TIMEOUT_SECONDS
        for fine in (0, 2.0, 8, 86400, 9e9, edge, -1, -edge):
            with self.subTest(value=fine):
                self.assertEqual(validate(make_config(nested(key, fine))), [])
        for bad in (edge + 1, -edge - 1, 1e12, -1e12, 9.3e9, INF, -INF):
            with self.subTest(value=bad):
                found = self.assert_tolerated(nested(key, bad), key)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].level, WARNING)
                self.assertIn("天気予報", found[0].message)
                self.assertNotIn("VOICEVOX", found[0].message)
                self.assertNotIn("直るまで", found[0].hint)

    def test_a_weather_timeout_the_socket_rejects_only_skips_the_forecast_and_is_never_replaced(self):
        key = "weather.timeout_seconds"
        run = run_runtime(make_config(nested(key, 1e12)))
        self.assertEqual(run.failures, {})
        with mock.patch("urllib.request.urlopen", socket_timeout_urlopen):
            plan = run.app.builder.build_hourly(12)
        skipped = [warning for warning in plan.warnings if "天気予報" in warning]
        self.assertEqual(len(skipped), 1)
        self.assertIn("OverflowError", skipped[0])  # WeatherError ではない素の例外を、組み立ての守りが受ける
        self.assertTrue(plan.spoken)  # 時刻アナウンスとひとことは鳴る
        # 通る値なら、ふつうの通信失敗（WeatherError）。同じ天気予報の欠けでも、原因が違う
        run = run_runtime(make_config(nested(key, 8)))
        with mock.patch("urllib.request.urlopen", socket_timeout_urlopen):
            plan = run.app.builder.build_hourly(12)
        skipped = [warning for warning in plan.warnings if "天気予報" in warning]
        self.assertEqual(len(skipped), 1)
        self.assertNotIn("OverflowError", skipped[0])
        fixed, errors = sanitized(make_config(nested(key, 1e12)))
        self.assertEqual((errors, fixed.get(key)), ([], 1e12))

    def test_the_timeouts_are_only_read_when_voicevox_is_listed(self):
        for key in self.TIMEOUT_KEYS:
            with self.subTest(key=key):
                unlisted = deep_merge(nested(key, 1e12), {"tts": {"engines": ["prerecorded"]}})
                self.assertEqual(errors_of(unlisted), [])
                found = self.assert_tolerated(unlisted, key)
                self.assertIn("voicevox が無い", found[0].message)
                self.assertNotIn("直るまで", found[0].hint)

    def test_nan_for_the_mock_seconds_breaks_the_mock_player_only_for_unknown_lengths(self):
        """``min(長さ, NaN)`` は長さを返すが、長さが分からない音源（mp3）では ``NaN`` のまま ``sleep`` に渡る。"""
        player = audio.MockPlayer({"mock_max_seconds": NAN})
        segment = audio.Segment(os.path.join(ASSETS_DIR, "hotaru.mp3"))
        with mock.patch.object(audio.time, "sleep", fake_sleep):
            with self.assertRaises(ValueError):
                player.play_one(segment)
            player.play_one(audio.Segment(os.path.join(ASSETS_DIR, "announce.wav")))
        self.assertEqual(keys_of(errors_of(nested("audio.mock_max_seconds", NAN))),
                         ["audio.mock_max_seconds"])


def time_sleep(seconds):
    """本物の ``time.sleep``（上限を超えるときだけ呼ぶ。待たずに例外になる）。"""
    import time
    time.sleep(seconds)


class GuardedNumbersTest(RuntimeTestCase):
    """ランタイムが例外を受け止める数値は warning（その部品だけが失敗し、放送は続く）。"""

    WAVE_KEYS = ("time_signal.short_pip.frequency", "time_signal.short_pip.duration_ms",
                 "time_signal.long_pip.frequency", "time_signal.long_pip.duration_ms",
                 "time_signal.volume", "time_signal.envelope_ms")

    def fresh_app(self, override):
        """時報音がまだ無い作業フォルダで組み立てた ``ChimeApp``（放送のたびに時報音を合成する）。"""
        root = tempfile.mkdtemp(prefix="configcheck-fresh-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "assets"))
        for name in ("voice", "announce.wav", "hotaru.mp3", "quotes.json"):
            os.symlink(os.path.join(ASSETS_DIR, name), os.path.join(root, "assets", name))
        config = Config(deep_merge(DEFAULT_CONFIG, override), base_dir=root)
        return ChimeApp(config, backend="mock", dry_run=True)

    def test_an_unreadable_wave_setting_makes_the_time_signal_fail_but_the_broadcast_goes_on(self):
        for key in self.WAVE_KEYS:
            with self.subTest(key):
                override = nested(key, "abc")
                app = self.fresh_app(override)
                with self.assertRaises((ValueError, TypeError)):
                    timesignal.generate_time_signal(
                        os.path.join(app.config.base_dir, "x.wav"), app.config.section("time_signal"),
                        app.config.section("audio.mixer"))
                plan = app.builder.build_hourly(10)  # 例外にならない
                self.assertTrue(any("時報音を生成できませんでした" in w for w in plan.warnings))
                self.assertTrue(plan.spoken)  # 読み上げは鳴る（時報音だけが無い）
                self.assertNotIn("時報音", " ".join(segment.label for segment in plan.segments))
                self.assertEqual(errors_of(override), [])
                self.assertEqual(keys_of(warnings_of(override)), [key])

    def test_a_readable_wave_setting_makes_the_time_signal(self):
        app = self.fresh_app({})
        plan = app.builder.build_hourly(10)
        self.assertEqual([w for w in plan.warnings if "時報音" in w], [])
        self.assertIn("時報音", " ".join(segment.label for segment in plan.segments))

    def test_an_unreadable_fade_in_only_drops_the_fade(self):
        run = run_runtime(make_config(nested("audio.fade_in_ms", "abc")))
        self.assertEqual(run.failures, {})
        plan = run.plans["closing"]
        self.assertTrue(any("フェードイン時間" in warning for warning in plan.warnings))
        music = [segment for segment in plan.segments if segment.path.endswith("hotaru.mp3")]
        self.assertEqual([segment.fade_in_ms for segment in music], [0])  # 蛍の光は鳴る
        found = warnings_of(nested("audio.fade_in_ms", "abc"))
        self.assertIn("フェードイン", found[0].message)


class HugeValuesTest(RuntimeTestCase):
    """巨大な整数（``10**400``）や 4300 桁を超える整数・``NaN``・無限大で、検査と置き換えが落ちない。

    JSON は 309 桁以上の整数も受けるが、``float`` には直せない（``math.isfinite`` が
    ``OverflowError`` になる）。検査が例外を出すと、起動が ``ChimeApp`` に届く前に落ちる。
    """

    VALUES = (HUGE, -HUGE, 10 ** 5000, INF, -INF, NAN)

    def leaf_keys(self):
        return [path for path, base in configcheck._default_paths()
                if not isinstance(base, dict)]

    def assert_survives(self, override):
        config = make_config(override)
        findings = validate(config)  # 例外にならない
        fixed, errors = sanitized(config)
        check_config(config)
        self.assertEqual(errors, [f for f in findings if f.level == ERROR])
        self.assertEqual(errors_of_config(fixed), [], "置き換えた後に error が残る")
        for finding in findings:
            self.assertIn(finding.key, finding.message)
            self.assertLessEqual(len(finding.message), 600, "巨大な値でメッセージが埋まっている")
        return findings

    def test_every_leaf_key_survives_every_huge_value(self):
        for key in self.leaf_keys():
            for index, value in enumerate(self.VALUES):
                with self.subTest(key=key, value=type(value).__name__ + str(index)):
                    self.assert_survives(nested(key, value))

    def test_every_leaf_key_survives_a_huge_value_in_a_list_or_a_table(self):
        for key in self.leaf_keys():
            for index, value in enumerate(([HUGE], {"k": HUGE}, [[HUGE]], [NAN, INF])):
                with self.subTest(key=key, value=index):
                    self.assert_survives(nested(key, value))

    def test_a_huge_integer_for_a_number_key_is_a_finding_or_clean_never_an_exception(self):
        for rule in configcheck._NUMBERS:
            with self.subTest(rule.key):
                self.assert_survives(nested(rule.key, HUGE))

    def test_a_huge_integer_does_not_stop_the_other_keys_from_being_checked(self):
        config = make_config({"time_signal": {"volume": HUGE},
                              "schedule": {"max_sleep_seconds": 0},
                              "timezone": "Asia/Tokio"})
        self.assertEqual(sorted(keys_of(errors_of_config(config))),
                         ["schedule.max_sleep_seconds", "timezone"])
        self.assertIn("time_signal.volume", keys_of(validate(config)))

    def test_is_number_accepts_huge_integers(self):
        self.assertTrue(configcheck._is_number(HUGE))
        self.assertTrue(configcheck._is_number(-HUGE))
        for value in (True, False, NAN, INF, -INF, "1", None, [1]):
            self.assertFalse(configcheck._is_number(value))

    def test_huge_numbers_never_flood_a_message(self):
        found = validate(make_config(nested("time_signal.volume", HUGE)))
        self.assertLess(len(found[0].message), 300)
        self.assertIn("…", found[0].message)

    def test_a_value_json_cannot_dump_is_still_shown(self):
        self.assertEqual(configcheck._show(10 ** 5000), configcheck._show(10 ** 5000))
        self.assertTrue(configcheck._show(10 ** 5000))
        self.assertEqual(configcheck._show(object())[:1], '"')

    def test_a_check_file_with_a_huge_integer_is_read_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pi.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"time_signal": {"volume": %s, "short_pip_count": %s}}' % ("9" * 400, "9" * 400))
            config = load_config(path, base_dir=tmp)
            findings = check_config(config)
        self.assertEqual([(f.level, f.key) for f in findings],
                         [(ERROR, "time_signal.short_pip_count"), (WARNING, "time_signal.volume")])

    def test_huge_hours_and_spans_are_counted_without_printing_every_digit(self):
        found = errors_of({"schedule": {"hourly": {"start_hour": 0, "end_hour": HUGE}}})
        self.assertEqual(keys_of(found), ["schedule.hourly.end_hour"])
        self.assertIn("天文学的な数", found[0].message)
        self.assertLess(len(found[0].message), 300)

    def test_the_text_and_hour_messages_survive_an_integer_python_cannot_print(self):
        # 4300 桁を超える整数は、Python 3.11 以降では ``str()`` にできない
        found = validate(make_config({"schedule": {"hourly": {"start_hour": 10 ** 5000}}}))
        self.assertEqual(keys_of(found), ["schedule.hourly.start_hour"])


class RuleIsolationTest(RuntimeTestCase):
    """検査の 1 つが壊れても、ほかの検査と起動は止まらない（その項目を「検査できませんでした」にする）。"""

    def test_a_rule_that_raises_becomes_one_warning_and_the_others_still_run(self):
        config = make_config({"timezone": "Asia/Tokio", "schedule": {"max_sleep_seconds": 0}})
        with mock.patch.object(configcheck, "_check_timezone", side_effect=RuntimeError("内部の不具合")):
            findings = validate(config)
        self.assertEqual([(f.level, f.key) for f in findings],
                         [(ERROR, "schedule.max_sleep_seconds"), (WARNING, "timezone")])
        unchecked = findings[1]
        self.assertIn("この項目は検査できませんでした", unchecked.message)
        self.assertIn("timezone", unchecked.message)
        self.assertIn("RuntimeError", unchecked.message)
        self.assertNotIn("直るまで", unchecked.hint)

    def test_one_numeric_rule_failing_does_not_hide_the_other_numeric_rules(self):
        real = configcheck._check_number

        def flaky(config, rule):
            if rule.key == "time_signal.volume":
                raise ZeroDivisionError("boom")
            return real(config, rule)

        config = make_config({"time_signal": {"volume": "x"}, "weather": {"timeout_seconds": "x"}})
        with mock.patch.object(configcheck, "_check_number", flaky):
            findings = validate(config)
        self.assertEqual([(f.level, f.key) for f in findings],
                         [(WARNING, "time_signal.volume"), (ERROR, "weather.timeout_seconds")])
        self.assertIn("検査できませんでした", findings[0].message)

    def test_sanitized_and_check_config_survive_a_broken_rule(self):
        config = make_config({"timezone": "Asia/Tokio"})
        with mock.patch.object(configcheck, "_check_timezone", side_effect=RuntimeError("内部の不具合")):
            fixed, errors = sanitized(config)
            findings = check_config(config)
        self.assertEqual(errors, [])
        self.assertEqual(fixed, config)  # 検査できなかった項目は、置き換えない
        self.assertEqual([(f.level, f.key) for f in findings], [(WARNING, "timezone")])

    def test_every_rule_is_isolated(self):
        """どの検査を壊しても、検査全体は例外にならない。"""
        for key, check in configcheck._checks():
            with self.subTest(key):
                with mock.patch.object(configcheck, "_checks", return_value=[
                        (key, mock.Mock(side_effect=OverflowError("x"))),
                        ("other", lambda config: [])]):
                    found = validate(make_config())
                self.assertEqual([(f.level, f.key) for f in found], [(WARNING, key)])

    def test_a_walk_that_raises_is_a_warning_for_that_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pi.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"timezone": "UTC"}, handle)
            config = load_config(path, base_dir=tmp)
            with mock.patch.object(configcheck, "walk_overrides", side_effect=RuntimeError("x")):
                findings = check_config(config)
        self.assertEqual([(f.level, f.key, f.source) for f in findings], [(WARNING, "", path)])
        self.assertIn("検査できませんでした", findings[0].message)

    def test_a_message_that_cannot_be_built_is_still_a_warning(self):
        # 「検査できませんでした」の行き先（キー）が空でも、メッセージは作れる
        finding = configcheck._unchecked("", ValueError("x"))
        self.assertEqual((finding.level, finding.key), (WARNING, ""))
        self.assertIn("この項目は検査できませんでした", finding.message)


class FlagsTest(RuntimeTestCase):
    """真偽値のキー。ランタイムは ``bool(値)`` で読むので動くが、書いたつもりと逆になりうる。"""

    def test_true_and_false_are_fine(self):
        for key in configcheck._FLAGS:
            for value in (True, False):
                with self.subTest(key=key, value=value):
                    self.assertEqual(validate(make_config(nested(key, value))), [])

    def test_every_flag_in_the_default_config_is_covered(self):
        flags = {path for path, base in configcheck._default_paths() if isinstance(base, bool)}
        self.assertEqual(flags, set(configcheck._FLAGS))

    def test_the_string_false_is_true_so_the_chime_stays_on(self):
        run = run_runtime(make_config(nested("schedule.hourly.enabled", "false")))
        self.assertEqual(run.failures, {})
        self.assertIn("hourly:10", run.events["2026-10-09"])  # 止めたつもりが、鳴る
        found = warnings_of(nested("schedule.hourly.enabled", "false"))
        self.assertIn("真として扱われ、時報が鳴ります", found[0].message)

    def test_zero_and_null_are_false_so_the_chime_is_off(self):
        for value in (0, None, ""):
            with self.subTest(value=value):
                run = run_runtime(make_config(nested("schedule.hourly.enabled", value)))
                self.assertFalse(any(k.startswith("hourly:") for k in run.events["2026-10-09"]))
                found = warnings_of(nested("schedule.hourly.enabled", value))
                self.assertIn("偽として扱われ、時報は鳴りません", found[0].message)

    def test_the_other_flags_are_read_the_same_way(self):
        run = run_runtime(make_config(nested("extra_segment.enabled", "no")))  # 真: おまけは流れる
        self.assertEqual(len(run.plans["hourly:10"].spoken), 2)
        run = run_runtime(make_config(nested("extra_segment.enabled", 0)))  # 偽: おまけは流れない
        self.assertEqual(len(run.plans["hourly:10"].spoken), 1)
        run = run_runtime(make_config(nested("time_signal.use_noon_template", 0)))
        self.assertEqual(run.plans["hourly:12"].silent, ["午後12時をお知らせしたのだ。"])  # 専用文言を使わず、作り置きが無い
        self.assertIn("無音になります", warnings_of(nested("time_signal.use_noon_template", 0))[0].message)

    def test_a_null_noon_flag_takes_the_default_in_the_runtime_so_it_is_not_reported(self):
        run = run_runtime(make_config(nested("time_signal.use_noon_template", None)))
        self.assertEqual(run.plans["hourly:12"].spoken[0], "正午をお知らせしたのだ。")
        self.assertEqual(validate(make_config(nested("time_signal.use_noon_template", None))), [])


class TimezoneTest(RuntimeTestCase):
    def test_resolvable_names_are_fine(self):
        for name in ("Asia/Tokyo", "UTC", "America/New_York"):
            with self.subTest(name=name):
                self.assertEqual(errors_of({"timezone": name}), [])

    def test_unresolvable_values_are_errors(self):
        for value in ("Asia/Tokio", "", "Asia", "../etc/passwd", 9, None, ["Asia/Tokyo"], True):
            with self.subTest(value=value):
                self.assertEqual(keys_of(errors_of({"timezone": value})), ["timezone"])

    def test_the_runtime_falls_back_to_the_os_local_time_for_each(self):
        for value in ("Asia/Tokio", "", None, 9):
            with self.subTest(value=value):
                self.assert_breaks({"timezone": value}, "timezone", "時計")

    def test_the_message_explains_the_nine_hour_shift(self):
        found = errors_of({"timezone": "Asia/Tokio"})
        self.assertIn("Asia/Tokio", found[0].message)
        self.assertIn("9 時間", found[0].message)
        self.assertIn("Asia/Tokyo", found[0].hint)


class LoggingTest(RuntimeTestCase):
    def test_the_accepted_level_names_are_the_ones_logging_registers(self):
        self.assertEqual(set(configcheck._LOG_LEVELS), set(logging._nameToLevel))

    def test_each_level_name_gives_that_level_in_any_case(self):
        for name in configcheck._LOG_LEVELS:
            for written in (name, name.lower(), name.capitalize()):
                with self.subTest(name=written):
                    config = make_config(nested("logging.level", written))
                    self.assertEqual(errors_of_config(config), [])
                    effective, failures, _ = logging_outcome(config)
                    self.assertEqual((effective, failures), (logging._nameToLevel[name], []))

    def test_an_unknown_name_silently_becomes_info(self):
        for value in ("LOUD", "INFO ", "", "10", 10, "warning!", "ｉｎｆｏ"):
            with self.subTest(value=value):
                config = make_config(nested("logging.level", value))
                self.assertEqual([f.key for f in errors_of_config(config)], ["logging.level"])
                effective, _, _ = logging_outcome(config)
                self.assertEqual(effective, logging.INFO)  # 黙って INFO になる（利用者は気づけない）

    def test_the_replacement_is_the_default_level_and_changes_nothing_but_the_log(self):
        fixed, errors = sanitized(make_config(nested("logging.level", "LOUD")))
        self.assertEqual(keys_of(errors), ["logging.level"])
        self.assertEqual(fixed.get("logging.level"), "INFO")
        self.assertIn("直るまでは既定値 \"INFO\" で動かします", errors[0].hint)

    def test_a_null_level_means_the_default(self):
        config = make_config(nested("logging.level", None))
        self.assertEqual(validate(config), [])
        self.assertEqual(logging_outcome(config)[0], logging.INFO)

    def test_usable_formats_are_fine(self):
        for value in ("%(asctime)s - %(levelname)s - %(message)s", "%(message)s", None, "",
                      "%(levelname)s: %(message)s (%(name)s)", "100%% %(message)s"):
            with self.subTest(value=value):
                config = make_config(nested("logging.format", value))
                self.assertEqual(validate(config), [])
                self.assertEqual(logging_outcome(config)[1], [])

    def test_a_format_that_fails_when_a_record_is_formatted_is_an_error(self):
        """書式の組み立てには通っても、1 件を整形すると例外になり、ログが全部失われる書式。"""
        for value in ("%(foo)s", "%(message)d", "%(asctime)s %(message)s %s", "%(message)", "ログ",
                      "%(asctime", "100%", 5, ["%(message)s"], {"a": 1}):
            with self.subTest(value=value):
                config = make_config(nested("logging.format", value))
                self.assertEqual(keys_of(errors_of_config(config)), ["logging.format"])
                self.assertIn("ログ", runtime_failures(config))  # 設定か、1 件の出力で壊れる

    def test_a_format_logging_accepts_but_cannot_format_is_caught_by_formatting_a_record(self):
        # logging.Formatter(...) の組み立てだけを見ても、これらは見逃す
        for value in ("%(foo)s", "%(message)d", "%(asctime)s %(message)s %s"):
            with self.subTest(value=value):
                logging.Formatter(value)  # 組み立ては通る
                self.assertEqual(keys_of(errors_of(nested("logging.format", value))), ["logging.format"])

    def test_the_message_names_the_exception(self):
        found = errors_of(nested("logging.format", "%(foo)s"))
        self.assertIn("ValueError", found[0].message)
        self.assertIn("foo", found[0].message)

    def test_the_replacement_format_logs_normally(self):
        fixed, _ = sanitized(make_config(nested("logging.format", "%(foo)s")))
        effective, failures, output = logging_outcome(fixed)
        self.assertEqual(failures, [])
        self.assertIn("メッセージ", output)


class AudioBackendTest(RuntimeTestCase):
    ACCEPTED = ("auto", "pygame", "command", "mock")

    def test_the_documented_backends_are_fine_in_any_case(self):
        for name in self.ACCEPTED + ("MOCK", "Pygame"):
            with self.subTest(name=name):
                self.assertEqual(validate(make_config({"audio": {"backend": name}})), [])

    def test_an_unknown_name_falls_back_to_auto_so_it_is_only_a_warning(self):
        for value in ("pygam", "alsa", " mock", "mock "):
            with self.subTest(value=value):
                found = self.assert_tolerated({"audio": {"backend": value}}, "audio.backend")
                self.assertIn("mock", found[0].hint)
                self.assertIn("自動選択", found[0].message)

    def test_an_unknown_name_is_really_treated_as_auto_with_a_warning_log(self):
        logger = logging.getLogger("chime.audio")
        with logs_enabled(), self.assertLogs(logger, level="WARNING") as logged:
            with mock.patch.object(audio.env, "is_production_linux", return_value=False):
                player = create_player({"backend": "pygam"})
        self.assertEqual(player.name, "mock")  # 開発環境の auto
        self.assertTrue(any("未知の audio.backend" in line for line in logged.output))

    def test_a_non_string_value_breaks_the_player_selection(self):
        for value in (1, True, ["mock"], {"a": 1}, 1.5):
            with self.subTest(value=value):
                self.assert_breaks({"audio": {"backend": value}}, "audio.backend", "再生方式")
        with self.assertRaises(AttributeError):
            create_player({"backend": 1})

    def test_empty_values_mean_auto_and_are_left_alone(self):
        for value in (None, "", 0, False, []):
            with self.subTest(value=value):
                self.assertEqual(validate(make_config({"audio": {"backend": value}})), [])
                self.assertEqual(create_player({"backend": value}).name, create_player({}).name)

    def test_the_accepted_names_are_the_ones_create_player_understands(self):
        # configcheck は chime.audio を import できないので名前を写して持っている。
        # create_player が「未知の audio.backend」と言わない名前と一致していること。
        logger = logging.getLogger("chime.audio")

        def warns_unknown(name):
            with logs_enabled(), self.assertLogs(logger, level="DEBUG") as logged:
                create_player({"backend": name})
                logger.debug("assertLogs は 1 件以上のログを要求する")
            return any("未知の audio.backend" in line for line in logged.output)

        for name in self.ACCEPTED:
            with self.subTest(name=name):
                self.assertFalse(warns_unknown(name))
        self.assertTrue(warns_unknown("pygam"))
        for name in ("pygame", "command", "mock"):
            self.assertEqual(create_player({"backend": name}).name, name)
        self.assertEqual(self.ACCEPTED, configcheck._AUDIO_BACKENDS)


class TtsEnginesTest(RuntimeTestCase):
    ACCEPTED = ("prerecorded", "voicevox")

    def silent_phrases(self, override):
        run = run_runtime(make_config(override))
        self.assertEqual(run.failures, {})
        return run.plans["hourly:10"].silent

    def test_known_engines_are_fine(self):
        for value in (["prerecorded", "voicevox"], ["prerecorded"], ["voicevox", "prerecorded"]):
            with self.subTest(value=value):
                self.assertEqual(validate(make_config({"tts": {"engines": value}})), [])

    def test_the_default_engines_are_fine(self):
        self.assertEqual(validate(Config(DEFAULT_CONFIG)), [])
        self.assertEqual(DEFAULT_CONFIG["tts"]["engines"], ["prerecorded", "voicevox"])

    def test_an_unknown_engine_name_is_ignored_by_the_runtime_so_it_is_a_warning(self):
        override = {"tts": {"engines": ["prerecorded", "open_jtalk"]}}
        found = self.assert_tolerated(override, "tts.engines")
        self.assertIn("open_jtalk", found[0].message)
        self.assertNotIn("prerecorded", found[0].message)
        self.assertIn("無視されます", found[0].message)
        self.assertEqual(self.silent_phrases(override), [])  # 作り置きの声は鳴る
        service = TTSService({"engines": ["prerecorded", "open_jtalk"]}, "/nonexistent/cache", "/nonexistent/voice")
        self.assertEqual([engine.name for engine in service.engines], ["prerecorded"])

    def test_a_string_is_read_one_character_at_a_time_so_every_voice_goes_silent(self):
        override = {"tts": {"engines": "prerecorded"}}
        found = self.assert_tolerated(override, "tts.engines")
        self.assertIn("リストではなく", found[0].message)
        self.assertIn("無音", found[0].message)
        self.assertEqual(len(self.silent_phrases(override)), 2)  # 時刻アナウンスもひとことも無音
        self.assertIn("[ ] の中に prerecorded・voicevox", found[0].hint)

    def test_no_usable_engine_left_is_said(self):
        found = warnings_of({"tts": {"engines": ["a", "b"]}})
        self.assertIn("1 つも残らない", found[0].message)
        found = warnings_of({"tts": {"engines": ["prerecorded", "b"]}})
        self.assertNotIn("1 つも残らない", found[0].message)

    def test_an_element_that_is_not_a_string_is_an_unknown_name(self):
        found = warnings_of({"tts": {"engines": ["prerecorded", 5, ["voicevox"]]}})
        self.assertEqual(keys_of(found), ["tts.engines"])

    def test_a_value_that_cannot_be_iterated_breaks_startup(self):
        for value in (None, 5, True, 1.5):
            with self.subTest(value=value):
                self.assert_breaks({"tts": {"engines": value}}, "tts.engines", "起動")

    NO_PRERECORDED = (["voicevox"], [], "", {}, ["open_jtalk"], ["voicevox", "open_jtalk"], {"voicevox": 1})

    def test_without_prerecorded_every_voice_goes_silent_on_a_pi_so_it_is_a_warning(self):
        """作り置きを引かない設定は、Pi（VOICEVOX ENGINE が動かない）では読み上げがすべて無音になる。

        ランタイムは例外にならず動く（PC の開発では ``["voicevox"]`` だけにすることもある）ので、
        置き換えない warning。
        """
        for value in self.NO_PRERECORDED:
            override = {"tts": {"engines": value}}
            with self.subTest(value=value):
                found = self.assert_tolerated(override, "tts.engines")
                self.assertEqual(len(found), 1)
                self.assertIn("tts.engines", found[0].message)
                self.assertIn("Pi では読み上げがすべて無音になります", found[0].message)
                self.assertIn("prerecorded", found[0].hint)
                self.assertNotIn("直るまで", found[0].hint)
                run = run_runtime(make_config(override))
                self.assertEqual(run.failures, {})
                plan = run.plans["hourly:10"]
                self.assertEqual(plan.spoken, [])  # 時刻アナウンスもひとことも、声が出ない
                self.assertEqual(len(plan.silent), 2)

    def test_the_warning_matches_what_the_prerecorded_lookup_finds(self):
        """指摘が出る設定は、``TTSService.prerecorded_lookup`` が（声のファイルがあっても）何も引けない設定。"""
        phrase = "午前10時をお知らせしたのだ。"
        voice = os.path.join(ASSETS_DIR, "voice")
        cases = [(["prerecorded", "voicevox"], True), (["prerecorded"], True),
                 (["voicevox", "prerecorded"], True), (["prerecorded", "open_jtalk"], True)]
        cases += [(value, False) for value in self.NO_PRERECORDED]
        for value, found_by_lookup in cases:
            with self.subTest(value=value):
                service = TTSService({"engines": value}, "/nonexistent/cache", voice)
                self.assertEqual(service.prerecorded_lookup(phrase) is not None, found_by_lookup)
                flagged = "Pi では読み上げがすべて無音になります" in "".join(
                    f.message for f in warnings_of({"tts": {"engines": value}}) if f.key == "tts.engines")
                self.assertEqual(flagged, not found_by_lookup)

    def test_one_finding_per_key_even_when_several_things_are_wrong(self):
        for value in (["open_jtalk"], "voicevox", {"open_jtalk": 1}, ["a", "b", "voicevox"]):
            with self.subTest(value=value):
                found = [f for f in validate(make_config({"tts": {"engines": value}})) if f.key == "tts.engines"]
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].level, WARNING)

    def test_a_prerecorded_engine_anywhere_in_the_list_is_enough(self):
        for value in (["voicevox", "prerecorded"], ["open_jtalk", "prerecorded"], ["prerecorded"]):
            with self.subTest(value=value):
                found = [f for f in warnings_of({"tts": {"engines": value}}) if "無音" in f.message]
                self.assertEqual(found, [])

    def test_an_empty_list_is_left_alone_but_the_silence_is_said(self):
        found = warnings_of({"tts": {"engines": []}})
        self.assertEqual(keys_of(found), ["tts.engines"])
        self.assertIn("使えるエンジンが 1 つもない", found[0].message)
        self.assertIn("Pi では読み上げがすべて無音になります", found[0].message)
        fixed, errors = sanitized(make_config({"tts": {"engines": []}}))
        self.assertEqual((errors, fixed.get("tts.engines")), ([], []))

    def test_voicevox_only_says_where_the_voice_still_comes_from(self):
        found = warnings_of({"tts": {"engines": ["voicevox"]}})
        self.assertIn("VOICEVOX ENGINE が動く PC では声が出ます", found[0].message)
        self.assertIn('"prerecorded" を入れてください', found[0].hint)
        self.assertIn('["prerecorded", "voicevox"]', found[0].hint)

    def test_an_integer_python_cannot_turn_into_text_breaks_startup_like_the_runtime(self):
        """``str(整数)`` は、桁数が多すぎると Python 3.11 以降では例外になる（古い版では「知らない名前」）。"""
        override = {"tts": {"engines": ["prerecorded", 10 ** 5000]}}
        try:
            str(10 ** 5000)
        except ValueError:
            self.assert_breaks(override, "tts.engines", "起動")
        else:
            self.assert_tolerated(override, "tts.engines")

    def test_the_accepted_names_are_the_ones_tts_service_builds(self):
        service = TTSService({"engines": list(self.ACCEPTED) + ["nope"]},
                             "/nonexistent/cache", "/nonexistent/voice")
        self.assertEqual([engine.name for engine in service.engines], list(self.ACCEPTED))
        self.assertEqual(self.ACCEPTED, configcheck._TTS_ENGINES)


class LocationsTest(RuntimeTestCase):
    KEY = "weather.open_meteo.locations"

    def locations(self, *items):
        return {"weather": {"open_meteo": {"locations": list(items)}}}

    def test_valid_coordinates_are_fine(self):
        for latitude, longitude in ((35.0045, 135.8686), (90, 180), (-90.0, -180.0), (0, 0)):
            with self.subTest(latitude=latitude, longitude=longitude):
                item = {"label": "地点", "latitude": latitude, "longitude": longitude}
                self.assertEqual(validate(make_config(self.locations(item))), [])

    def test_no_locations_is_fine(self):
        self.assertEqual(validate(make_config(self.locations())), [])

    def test_latitude_out_of_range_is_a_warning(self):
        for latitude in (90.0001, -91, 135.8686):
            with self.subTest(latitude=latitude):
                override = self.locations({"label": "x", "latitude": latitude, "longitude": 135.0})
                found = self.assert_tolerated(override, self.KEY)
                self.assertEqual(keys_of(found), [self.KEY])
                self.assertIn("latitude", found[0].message)
                self.assertIn("1 番目", found[0].message)

    def test_longitude_out_of_range_is_a_warning(self):
        for longitude in (180.1, -181, 360):
            with self.subTest(longitude=longitude):
                found = warnings_of(self.locations({"label": "x", "latitude": 35.0, "longitude": longitude}))
                self.assertEqual(keys_of(found), [self.KEY])
                self.assertIn("longitude", found[0].message)

    def test_swapped_latitude_and_longitude_is_caught(self):
        found = warnings_of(self.locations({"label": "大津", "latitude": 135.8686, "longitude": 35.0045}))
        self.assertEqual(len(found), 1)
        self.assertIn("取り違え", found[0].hint)

    def test_missing_or_non_numeric_coordinates_are_warnings(self):
        for item in ({"label": "x", "latitude": 35.0},
                     {"label": "x", "longitude": 135.0},
                     {"label": "x", "latitude": "35.0", "longitude": 135.0},
                     {"label": "x", "latitude": True, "longitude": 135.0},
                     {"label": "x", "latitude": None, "longitude": 135.0},
                     {"label": "x", "latitude": 10 ** 400, "longitude": 135.0}):
            with self.subTest(item=item):
                self.assertEqual(keys_of(warnings_of(self.locations(item))), [self.KEY])
                self.assertEqual(errors_of(self.locations(item)), [])

    def test_every_bad_location_is_reported_with_its_position(self):
        good = {"label": "大津", "latitude": 35.0045, "longitude": 135.8686}
        found = warnings_of(self.locations(
            good, {"label": "a", "latitude": 99, "longitude": 135.0},
            {"label": "b", "latitude": 35.0, "longitude": 999}))
        self.assertEqual(len(found), 2)
        self.assertIn("2 番目", found[0].message)
        self.assertIn("3 番目", found[1].message)

    def test_a_non_list_or_non_object_element_is_a_warning(self):
        self.assertEqual(keys_of(warnings_of({"weather": {"open_meteo": {"locations": "大津"}}})), [self.KEY])
        self.assertEqual(keys_of(warnings_of(self.locations("大津"))), [self.KEY])

    def test_a_bad_location_costs_only_that_places_weather_the_broadcast_goes_on(self):
        """天気 API は範囲外の座標を断る（HTTP 400）。その地点の天気を取れないだけで、放送は続く。"""
        error = urllib.error.HTTPError("https://api.open-meteo.com/", 400, "Bad Request", None, None)
        good = {"label": "大津", "latitude": 35.0045, "longitude": 135.8686}
        bad = {"label": "京都", "latitude": 135.0, "longitude": 35.0}
        config = make_config(self.locations(good, bad))
        app = ChimeApp(Config(config.data, base_dir=sandbox_dir()), backend="mock", dry_run=True)

        def fetch(url, timeout):
            if "latitude=135.0" in url:
                raise weather.WeatherError("天気 API が HTTP 400 を返しました") from error
            return {"current": {"weather_code": 1, "temperature_2m": 20.0}}

        with mock.patch.object(weather, "fetch_json", fetch):
            sentences = app.weather.describe_sentences(today=FIXED_NOW.date())
        self.assertEqual(sentences[0], "今の大津の天気はおおむね晴れなのだ。")  # 取れた地点だけを読む
        self.assertTrue(all("京都" not in sentence for sentence in sentences))


class TemplatesTest(RuntimeTestCase):
    """読み上げ文のテンプレート。壊れていても、ランタイムは既定の文言に切り替えるか、その部品だけを飛ばす。"""

    def test_the_default_templates_are_fine(self):
        self.assertEqual(validate(Config(DEFAULT_CONFIG)), [])

    def test_every_allowed_name_is_accepted(self):
        for key, names in _TEMPLATE_FIELDS.items():
            with self.subTest(key=key):
                template = "".join("{" + name + "}" for name in names)
                self.assertEqual(validate(make_config(nested(key, template))), [])

    def test_an_unknown_placeholder_is_a_warning(self):
        for key in _TEMPLATE_FIELDS:
            with self.subTest(key=key):
                found = warnings_of(nested(key, "あ{hours}い"))
                self.assertEqual(keys_of(found), [key])
                self.assertIn("{hours}", found[0].message)
                self.assertEqual(errors_of(nested(key, "あ{hours}い")), [])

    def test_a_name_that_belongs_to_another_template_is_a_warning(self):
        for override, key in (({"weather": {"sentence_weather": "{temp}度"}}, "weather.sentence_weather"),
                              ({"weather": {"sentence_temp": "{label}は{temp}度"}}, "weather.sentence_temp"),
                              ({"time_signal": {"noon_template": "{weather}"}}, "time_signal.noon_template")):
            with self.subTest(key=key):
                self.assertEqual(keys_of(warnings_of(override)), [key])

    def test_positional_placeholders_are_warnings(self):
        for template in ("{}", "{0}", "{period}{}"):
            with self.subTest(template=template):
                self.assertEqual(
                    keys_of(warnings_of({"time_signal": {"announce_template": template}})),
                    ["time_signal.announce_template"])

    def test_broken_braces_are_warnings(self):
        for template in ("{period", "period}", "{", "}{"):
            with self.subTest(template=template):
                found = warnings_of({"time_signal": {"announce_template": template}})
                self.assertEqual(keys_of(found), ["time_signal.announce_template"])
                self.assertIn("波括弧", found[0].message)

    def test_escaped_braces_and_attribute_access_are_fine(self):
        for template in ("{{period}}", "{hour.real}時", "{hour:02d}"):
            with self.subTest(template=template):
                self.assertEqual(validate(make_config({"time_signal": {"announce_template": template}})), [])

    def test_a_placeholder_in_a_format_spec_is_checked_too(self):
        found = warnings_of({"weather": {"sentence_temp": "{temp:{width}}"}})
        self.assertEqual(keys_of(found), ["weather.sentence_temp"])
        self.assertIn("{width}", found[0].message)

    def test_a_usage_the_names_cannot_show_is_found_by_formatting_the_template(self):
        for template in ("{hour.foo}", "{hour_reading:02d}", "{period.x}", "{hour:abc}"):
            with self.subTest(template=template):
                found = warnings_of({"time_signal": {"announce_template": template}})
                self.assertEqual(keys_of(found), ["time_signal.announce_template"])
                self.assertIn("この書き方は使えません", found[0].message)

    def test_a_long_template_is_not_formatted_by_the_check(self):
        template = "{hour:" + "9" * 40 + "}" + "あ" * configcheck._MAX_TRIAL_TEMPLATE
        self.assertEqual(validate(make_config({"time_signal": {"announce_template": template}})), [])
        short = "{hour:" + "9" * 40 + "}"
        self.assertEqual(keys_of(warnings_of({"time_signal": {"announce_template": short}})),
                         ["time_signal.announce_template"])

    def test_an_empty_sentence_is_fine_it_means_do_not_read(self):
        self.assertEqual(validate(make_config({"weather": {"sentence_weather": "", "sentence_temp": ""}})), [])

    def test_a_non_string_weather_template_is_read_through_str_so_it_is_a_warning(self):
        for value in (None, 5, ["{temp}"]):
            with self.subTest(value=value):
                found = warnings_of({"weather": {"sentence_temp": value}})
                self.assertEqual(keys_of(found), ["weather.sentence_temp"])
                self.assertIn("文字列ではありません", found[0].message)
                self.assertIn("作り置きに無い", found[0].message)

    def test_an_unknown_placeholder_in_the_announcement_falls_back_to_the_default_phrase(self):
        """``SequenceBuilder`` が既定の文言に切り替える。作り置きの声が鳴る。"""
        for key in ("time_signal.announce_template", "time_signal.noon_template"):
            with self.subTest(key):
                override = nested(key, "あ{hours}い")
                run = run_runtime(make_config(override))
                self.assertEqual(run.failures, {})
                plan = run.plans["hourly:12" if "noon" in key else "hourly:10"]
                expected = "正午をお知らせしたのだ。" if "noon" in key else "午前10時をお知らせしたのだ。"
                self.assertEqual(plan.spoken[0], expected)
                self.assertEqual(plan.silent, [])
                self.assertTrue(any("テンプレート" in w for w in plan.warnings))
                found = warnings_of(override)
                self.assertIn("既定の文言で読み上げます", found[0].message)

    def test_an_attribute_error_drops_the_announcement_instead_of_falling_back(self):
        """``{hour.foo}`` は ``AttributeError``。切り替えの対象外で、時刻アナウンスの部品が飛ばされる。"""
        override = nested("time_signal.announce_template", "{hour.foo}")
        run = run_runtime(make_config(override))
        self.assertEqual(run.failures, {})
        plan = run.plans["hourly:10"]
        self.assertTrue(all("お知らせ" not in text for text in plan.spoken))
        self.assertTrue(plan.spoken)  # ひとことは鳴る
        found = warnings_of(override)
        self.assertIn("飛ばされ", found[0].message)

    def test_a_non_string_announcement_template_also_drops_the_announcement(self):
        override = nested("time_signal.announce_template", 5)
        run = run_runtime(make_config(override))
        self.assertTrue(all("お知らせ" not in text for text in run.plans["hourly:10"].spoken))
        self.assertIn("飛ばされ", warnings_of(override)[0].message)

    def test_a_null_announcement_template_uses_the_default_phrase(self):
        run = run_runtime(make_config(nested("time_signal.announce_template", None)))
        self.assertEqual(run.plans["hourly:10"].spoken[0], "午前10時をお知らせしたのだ。")
        self.assertEqual(validate(make_config(nested("time_signal.announce_template", None))), [])

    def test_a_broken_weather_template_costs_the_weather_only(self):
        override = nested("weather.sentence_weather", "{temp}度")
        config = make_config(override)
        app = ChimeApp(Config(config.data, base_dir=sandbox_dir()), backend="mock", dry_run=True)
        canned = {"current": {"weather_code": 1, "temperature_2m": 20.0}}
        with mock.patch.object(weather, "fetch_json", lambda url, timeout: canned):
            plan = app.builder.build_hourly(12)
        self.assertTrue(any("天気予報" in w for w in plan.warnings))
        self.assertEqual(plan.spoken[0], "正午をお知らせしたのだ。")
        self.assertEqual(len(plan.spoken), 2)  # 時刻アナウンスとひとこと（天気は無い）
        found = warnings_of(override)
        self.assertIn("天気予報は読み上げられません", found[0].message)

    def test_the_allowed_names_are_the_ones_the_real_code_passes(self):
        # 写して持つ名前表が、実装が渡す名前とずれていないこと（ずれると、使える置換を
        # 指摘したり、壊れるテンプレートを見逃したりする）。
        hour_names = _TEMPLATE_FIELDS["time_signal.announce_template"]
        actual = timesignal.hour_parts(10, DEFAULT_CONFIG["time_signal"])
        self.assertEqual(set(hour_names), set(actual))
        every_hour_name = "".join("{" + name + "}" for name in hour_names)
        timesignal.announce_text(10, {"announce_template": every_hour_name})
        with self.assertRaises(KeyError):
            timesignal.announce_text(10, {"announce_template": "{temp}"})

        parts = {"when": "今日", "label": "大津", "weather": "晴れ",
                 "temp": 20, "temp_max": 25, "pop": 50}
        settings = {"prerecord": {"pop_step": 10}}
        for key in ("sentence_weather", "sentence_temp", "sentence_temp_max", "sentence_pop"):
            names = _TEMPLATE_FIELDS["weather." + key]
            settings[key] = "".join("{" + name + "}" for name in names)
        self.assertEqual(len(weather.build_sentences(parts, settings)), 4)
        with self.assertRaises(KeyError):
            weather.build_sentences(parts, dict(settings, sentence_weather="{temp}"))

    def test_the_trial_values_are_what_the_runtime_passes(self):
        for key, sample in configcheck._TEMPLATE_SAMPLES.items():
            with self.subTest(key=key):
                self.assertEqual(set(sample), set(_TEMPLATE_FIELDS[key]))

    def test_the_fallback_errors_are_the_ones_the_builder_catches(self):
        for error in configcheck._FALLBACK_ERRORS:
            self.assertTrue(issubclass(error, (KeyError, IndexError, ValueError)))
        for template, fallback in (("{hours}", True), ("{", True), ("{}", True), ("{hour.foo}", False)):
            problem = configcheck._template_problem("time_signal.announce_template", template)
            self.assertEqual(problem[1], fallback, template)


class ExternalCommandsTest(RuntimeTestCase):
    """``audio.commands`` の項目は、引数のリストで書く。文字列で書くと 1 文字ずつの引数に分かれる。"""

    KEY = "audio.commands"
    DEFAULT = {".wav": ["aplay", "-q", "{path}"], ".mp3": ["mpg123", "-q", "{path}"]}

    def test_a_string_is_read_one_character_at_a_time_and_fails_every_playback(self):
        player = audio.CommandPlayer({"commands": {".wav": "aplay -q {path}"}})
        self.assertEqual(player.commands[".wav"], list("aplay -q {path}"))
        with self.assertRaises(ValueError):  # 波括弧 1 つだけの引数は format できない
            player.command_for("/x/y.wav")

    def test_a_string_without_braces_runs_a_command_named_after_its_first_character(self):
        player = audio.CommandPlayer({"commands": {".wav": "aplay"}})
        self.assertEqual(player.command_for("/x/y.wav"), ["a", "p", "l", "a", "y"])
        with mock.patch.object(audio.env, "has_command", side_effect=lambda name: name == "aplay"):
            with self.assertRaises(audio.PlaybackError) as caught:
                player.play_one(audio.Segment("/x/y.wav"))
        self.assertIn("a", str(caught.exception))

    def test_a_list_with_a_non_string_element_cannot_be_formatted(self):
        player = audio.CommandPlayer({"commands": {".wav": ["aplay", 5]}})
        with self.assertRaises(AttributeError):
            player.command_for("/x/y.wav")

    def test_a_string_is_an_error_and_only_that_entry_is_replaced_by_the_default(self):
        fixed = self.assert_breaks(nested(self.KEY, {".wav": "aplay -q {path}"}), self.KEY, "再生:command")
        self.assertEqual(fixed.get(self.KEY), self.DEFAULT)
        fixed = self.assert_breaks(nested(self.KEY, {".mp3": "mpg123 -q {path}"}), self.KEY, "再生:command")
        self.assertEqual(fixed.get(self.KEY), self.DEFAULT)

    def test_a_string_without_braces_is_an_error_too(self):
        fixed = self.assert_breaks(nested(self.KEY, {".wav": "aplay"}), self.KEY, "再生:command")
        self.assertEqual(fixed.get(self.KEY), self.DEFAULT)

    def test_the_other_entries_are_kept_as_they_were_written(self):
        override = nested(self.KEY, {".wav": "aplay -q {path}", ".mp3": ["mpg123", "--gapless", "{path}"]})
        fixed, errors = sanitized(make_config(override))
        self.assertEqual(keys_of(errors), [self.KEY])
        self.assertEqual(fixed.get(self.KEY), {".wav": self.DEFAULT[".wav"],
                                               ".mp3": ["mpg123", "--gapless", "{path}"]})

    def test_an_extension_without_a_default_entry_is_dropped(self):
        override = nested(self.KEY, {".ogg": "ogg123 {path}", ".flac": ["flac", "-d", "{path}"]})
        fixed, errors = sanitized(make_config(override))
        self.assertEqual(keys_of(errors), [self.KEY])
        self.assertEqual(fixed.get(self.KEY), dict(self.DEFAULT, **{".flac": ["flac", "-d", "{path}"]}))

    def test_the_extension_is_matched_in_lower_case_like_the_player(self):
        override = nested(self.KEY, {".WAV": "aplay -q {path}"})
        fixed, _ = sanitized(make_config(override))
        self.assertEqual(fixed.get(self.KEY)[".WAV"], self.DEFAULT[".wav"])
        player = audio.CommandPlayer(fixed.section("audio"))
        self.assertEqual(player.command_for("/x/y.wav"), ["aplay", "-q", "/x/y.wav"])

    def test_the_replacement_is_a_copy_of_the_default(self):
        fixed, _ = sanitized(make_config(nested(self.KEY, {".wav": "aplay"})))
        fixed.get(self.KEY)[".wav"].append("--changed")
        self.assertEqual(DEFAULT_CONFIG["audio"]["commands"][".wav"], ["aplay", "-q", "{path}"])

    def test_an_entry_that_is_not_iterable_is_replaced_the_same_way(self):
        for value in (1, None, True, 2.5):
            with self.subTest(value=value):
                fixed, errors = sanitized(make_config(nested(self.KEY, {".wav": value})))
                self.assertEqual(keys_of(errors), [self.KEY])
                self.assertEqual(fixed.get(self.KEY), self.DEFAULT)

    def test_a_table_that_is_not_a_table_comes_back_as_the_default(self):
        for value in (None, 5, "aplay", ["aplay"]):
            with self.subTest(value=value):
                fixed, _ = sanitized(make_config(nested(self.KEY, value)))
                self.assertEqual(fixed.get(self.KEY), self.DEFAULT)

    def test_readable_entries_are_left_alone(self):
        for value in ({".wav": ["aplay", "-q", "{path}"]},
                      {".ogg": ""},  # 空の文字列は空のリストと同じ「未設定」
                      {".wav": ("aplay", "{path}")},
                      {".flac": []},
                      {".wav": {"aplay": 1}}):  # 辞書はキーを並べたコマンドとして読まれる（例外にはならない）
            with self.subTest(value=value):
                self.assertEqual(errors_of(nested(self.KEY, value)), [])

    def test_the_message_names_the_entry_and_shows_how_to_write_it(self):
        found = errors_of(nested(self.KEY, {".wav": "aplay -q {path}"}))
        self.assertEqual(keys_of(found), [self.KEY])
        self.assertIn('".wav"', found[0].message)
        self.assertIn("1 文字ずつ", found[0].message)
        self.assertIn('["aplay", "-q", "{path}"]', found[0].message)
        self.assertIn('["aplay", "-q", "{path}"] のように引数のリストで書いてください', found[0].hint)
        self.assertIn("既定の項目に置き換え", found[0].hint)
        self.assertIn("取り除き", found[0].hint)
        self.assertRegex(found[0].hint, _JAPANESE)

    def test_a_non_string_message_does_not_talk_about_characters(self):
        found = errors_of(nested(self.KEY, {".wav": 1}))
        self.assertNotIn("1 文字ずつ", found[0].message)

    # -- 表を読む再生方式かどうか ------------------------------------------------
    #: コマンドとして読めない表（文字列の項目・反復できない項目・文字列でない要素）。
    BROKEN_TABLES = ({".wav": "aplay -q {path}"}, {".wav": "aplay"}, {".mp3": "mpg123 -q {path}"},
                     {".wav": 1}, {".mp3": None}, {".wav": ["aplay", 5]})
    #: 外部コマンドの再生が選ばれうる ``audio.backend``。``auto`` は pygame が無いと外部コマンドに
    #: 落ちる。空・知らない名前（綴り違い・前後の空白）も ``auto`` として動く。
    COMMAND_BACKENDS = ("command", "COMMAND", "auto", "Auto", None, "", "pygam", " mock")
    #: 外部コマンドの表を読まない ``audio.backend``（大文字小文字は問わない）。
    OTHER_BACKENDS = ("mock", "pygame", "MOCK", "Pygame")

    def override(self, backend, table):
        return {"audio": {"backend": backend, "commands": table}}

    def pi_without_pygame(self):
        """pygame の入っていない Pi（``auto`` は外部コマンドの再生に落ちる）。"""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(audio.env, "is_production_linux", return_value=True))
        stack.enter_context(mock.patch.object(audio.PygamePlayer, "available", return_value=False))
        return stack

    def test_a_broken_table_is_an_error_wherever_the_command_player_can_be_chosen(self):
        for backend in self.COMMAND_BACKENDS:
            for table in self.BROKEN_TABLES:
                with self.subTest(backend=backend, table=table):
                    self.assert_breaks(self.override(backend, table), self.KEY, "再生:command")

    def test_a_broken_table_is_only_a_warning_when_the_backend_never_reads_it(self):
        """mock と pygame は表を読まない。壊れていても動くので、置き換えない warning（command にしたときの話）。"""
        for backend in self.OTHER_BACKENDS:
            for table in self.BROKEN_TABLES + (None, 5):
                with self.subTest(backend=backend, table=table):
                    config = make_config(self.override(backend, table))
                    found = validate(config)
                    self.assertEqual([(f.level, f.key) for f in found], [(WARNING, self.KEY)])
                    self.assertIn(configcheck._COMMANDS_UNUSED, found[0].message)
                    self.assertRegex(found[0].hint, _JAPANESE)
                    self.assertNotIn("直るまで", found[0].hint)
                    self.assertNotIn("置き換え", found[0].hint)
                    fixed, errors = sanitized(config)
                    self.assertEqual((errors, fixed), ([], config))

    def test_the_unused_warning_still_says_how_to_write_the_entry(self):
        found = warnings_of(self.override("mock", {".wav": "aplay -q {path}"}))
        self.assertIn('".wav"', found[0].message)
        self.assertIn("1 文字ずつ", found[0].message)
        self.assertIn('["aplay", "-q", "{path}"] のように引数のリストで書いてください', found[0].hint)

    def test_the_backends_that_never_read_the_table_really_play_without_it(self):
        wav = os.path.join(ASSETS_DIR, "announce.wav")
        for backend in self.OTHER_BACKENDS:
            for table in self.BROKEN_TABLES + (None, 5):
                with self.subTest(backend=backend, table=table):
                    section = make_config(self.override(backend, table)).section("audio")
                    with mock.patch.object(audio, "pygame", FAKE_PYGAME), \
                            mock.patch.object(audio.time, "sleep", fake_sleep):
                        player = create_player(section)
                        played = player.play([audio.Segment(wav)])
                    self.assertEqual((player.name, played), (backend.lower(), 1))

    def test_the_check_agrees_with_create_player_about_who_reads_the_table(self):
        """表を読む（外部コマンドの再生を作る）方式だけが error。pygame の無い Pi の ``auto`` も含む。"""
        table = {".wav": 1}
        for backend in self.COMMAND_BACKENDS:
            with self.subTest(backend=backend), self.pi_without_pygame():
                section = make_config(self.override(backend, table)).section("audio")
                with self.assertRaises(TypeError):
                    create_player(section)
                self.assertEqual(keys_of(errors_of(self.override(backend, table))), [self.KEY])
        for backend in self.OTHER_BACKENDS:
            with self.subTest(backend=backend), self.pi_without_pygame():
                section = make_config(self.override(backend, table)).section("audio")
                self.assertEqual(create_player(section).name, backend.lower())
                self.assertEqual(errors_of(self.override(backend, table)), [])

    def test_a_backend_that_is_not_text_is_replaced_by_auto_so_the_table_is_read(self):
        override = self.override(5, {".wav": "aplay"})
        self.assertEqual(sorted(keys_of(errors_of(override))), ["audio.backend", self.KEY])
        fixed, _ = sanitized(make_config(override))
        self.assertEqual((fixed.get("audio.backend"), fixed.get(self.KEY)), ("auto", self.DEFAULT))


class FormatWidthTest(RuntimeTestCase):
    """書式指定の幅・桁数が大きいテンプレートは、試しの ``format`` に通さない（試すだけで 200 MB の文字列ができる）。"""

    HUGE = "{label:>200000000}"

    def peak_bytes(self, key, template):
        tracemalloc.start()
        try:
            found = warnings_of(nested(key, template))
            return found, tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_the_limit_is_the_one_phrases_uses(self):
        self.assertEqual(configcheck._MAX_FORMAT_SPEC, phrases.MAX_FORMAT_SPEC)
        for spec in ("", ">200", "*^1000", ".5f", "02d", ">{width}", ">99999999", "{x:>5000}", ",.3"):
            with self.subTest(spec=spec):
                self.assertEqual(configcheck._spec_number(spec), phrases._spec_number(spec))

    def test_a_huge_width_is_a_warning_and_is_never_formatted(self):
        cases = (("weather.sentence_weather", self.HUGE),
                 ("weather.sentence_weather", "{when}の{label:*^200000000}"),
                 ("weather.sentence_temp", "{temp:.999999999f}"),
                 ("weather.sentence_temp_max", "{temp_max:0>1001}"),
                 ("weather.sentence_pop", "{pop:{pop:>5000}}"),
                 ("time_signal.announce_template", "{hour_reading:>200000000}"),
                 ("time_signal.noon_template", "{period}{hour:.1001}"))
        for key, template in cases:
            with self.subTest(key=key, template=template):
                found, peak = self.peak_bytes(key, template)
                self.assertLess(peak, 5_000_000, "試しの format で大きな文字列を作っている")
                self.assertEqual(keys_of(found), [key])
                self.assertEqual(errors_of(nested(key, template)), [])
                self.assertIn("1000", found[0].message)
                self.assertIn("1000 以下", found[0].hint)
                self.assertNotRegex(found[0].message, r"[A-Za-z]+Error")

    def test_the_limit_itself_is_fine_and_one_more_is_not(self):
        for key, template, names in (("weather.sentence_weather", "{label:>%d}", "label"),
                                     ("weather.sentence_temp", "{temp:%d}", "temp"),
                                     ("time_signal.announce_template", "{hour_reading:*^%d}", "hour_reading")):
            with self.subTest(key=key):
                self.assertEqual(validate(make_config(nested(key, template % 1000))), [])
                self.assertEqual(keys_of(warnings_of(nested(key, template % 1001))), [key])

    def test_ordinary_specs_are_still_tried_by_formatting(self):
        # 小さな幅は、これまでどおり試す（合わなければ見つかる）。
        found = warnings_of(nested("time_signal.announce_template", "{hour_reading:02d}"))
        self.assertIn("この書き方は使えません", found[0].message)

    def test_a_width_beyond_what_format_accepts_is_a_value_error_like_any_other_typo(self):
        """9.2e18 以上は ``format`` が ``ValueError`` にする。時刻アナウンスは既定の文言に切り替わる。"""
        template = "{period}{hour_reading:>9223372036854775808}"
        with self.assertRaises(ValueError):
            template.format(**configcheck._TEMPLATE_SAMPLES["time_signal.announce_template"])
        override = nested("time_signal.announce_template", template)
        found = self.assert_tolerated(override, "time_signal.announce_template")
        self.assertIn("既定の文言で読み上げます", found[0].message)
        plan = run_runtime(make_config(override)).plans["hourly:10"]
        self.assertEqual(plan.spoken[0], "午前10時をお知らせしたのだ。")
        self.assertEqual(plan.silent, [])

    def test_the_largest_width_python_accepts_asks_for_memory_it_does_not_have(self):
        """9.2e18 未満の巨大な幅は、組み立てが ``MemoryError`` になり、時刻アナウンスの部品だけが飛ばされる。"""
        template = "{period}{hour_reading:>9223372036854775807}"
        override = nested("time_signal.announce_template", template)
        found = self.assert_tolerated(override, "time_signal.announce_template")
        self.assertIn("巨大な文", found[0].message)
        self.assertNotIn("既定の文言で読み上げます", found[0].message)
        plan = run_runtime(make_config(override)).plans["hourly:10"]
        self.assertTrue(any("時刻アナウンス" in warning for warning in plan.warnings))
        self.assertTrue(all("お知らせ" not in text for text in plan.spoken))

    def test_a_wide_but_buildable_announcement_is_silent_because_no_voice_matches(self):
        override = nested("time_signal.announce_template", "{period}{hour_reading:>5000}")
        found = self.assert_tolerated(override, "time_signal.announce_template")
        self.assertIn("巨大な文になり、作り置きに無いので、時刻アナウンスは鳴りません", found[0].message)
        plan = run_runtime(make_config(override)).plans["hourly:10"]
        self.assertTrue(all("お知らせ" not in text for text in plan.spoken))
        self.assertEqual(len(plan.silent), 1)  # 巨大な文だけ。ひとことは鳴る
        self.assertGreaterEqual(len(plan.silent[0]), 5000)
        self.assertEqual(len(plan.spoken), 1)

    def test_a_wide_weather_sentence_costs_that_places_weather_only(self):
        override = nested("weather.sentence_weather", "{label:>5000}")
        found = self.assert_tolerated(override, "weather.sentence_weather")
        self.assertIn("その地点の天気予報は読み上げられません", found[0].message)
        config = make_config(override)
        app = ChimeApp(Config(config.data, base_dir=sandbox_dir()), backend="mock", dry_run=True)
        canned = {"current": {"weather_code": 1, "temperature_2m": 20.0}}
        with mock.patch.object(weather, "fetch_json", lambda url, timeout: canned):
            plan = app.builder.build_hourly(12)
        self.assertTrue(any(len(text) >= 5000 for text in plan.silent))
        self.assertEqual(plan.spoken[0], "正午をお知らせしたのだ。")

    def test_a_long_template_is_still_not_examined(self):
        template = self.HUGE + "あ" * configcheck._MAX_TRIAL_TEMPLATE
        self.assertEqual(validate(make_config({"weather": {"sentence_weather": template}})), [])

    def test_a_broken_template_is_reported_as_broken_not_as_wide(self):
        found = warnings_of(nested("weather.sentence_weather", "{label:>200000000"))
        self.assertIn("波括弧", found[0].message)

    def test_a_width_with_hundreds_of_digits_is_shown_without_listing_them(self):
        template = "{label:>" + "9" * 900 + "}"
        found = warnings_of(nested("weather.sentence_weather", template))
        self.assertEqual(keys_of(found), ["weather.sentence_weather"])
        self.assertIn("巨大な数", found[0].message)


class PrerecordTest(RuntimeTestCase):
    """作り置きの気温・降水確率の範囲。実行時には例外にならないが、広すぎると数え上げが終わらない。"""

    LOW, HIGH, STEP = ("weather.prerecord.temp_min", "weather.prerecord.temp_max",
                       "weather.prerecord.pop_step")

    def test_the_default_range_is_fine(self):
        self.assertEqual(validate(Config(DEFAULT_CONFIG)), [])
        self.assertEqual(len(weather.prerecord_phrases(DEFAULT_CONFIG["weather"])),
                         28 + 46)  # 天気の語彙 28 と、気温 -5〜40 の 46

    def test_a_range_of_200_values_is_fine_and_201_is_a_warning(self):
        self.assertEqual(validate(make_config({"weather": {"prerecord": {"temp_min": -5, "temp_max": 194}}})), [])
        found = warnings_of({"weather": {"prerecord": {"temp_min": -5, "temp_max": 195}}})
        self.assertEqual(keys_of(found), [self.HIGH])
        self.assertIn("201 件", found[0].message)

    def test_the_phrase_count_really_grows_with_the_range(self):
        settings = make_config({"weather": {"prerecord": {"temp_min": 0, "temp_max": 5000}}}).section("weather")
        # 気温の文は temp_min〜temp_max の件数だけ増える（天気の語彙 28 件に加わる）
        self.assertEqual(len(weather.prerecord_phrases(settings)), 28 + 5001)

    def test_an_enormous_range_is_a_warning_naming_the_key_far_from_the_default(self):
        for override, key in (({"temp_max": 10 ** 9}, self.HIGH), ({"temp_min": -10 ** 9}, self.LOW),
                              ({"temp_max": HUGE}, self.HIGH), ({"temp_min": -HUGE}, self.LOW)):
            with self.subTest(override=str(override)[:30]):
                found = self.assert_tolerated({"weather": {"prerecord": override}}, key)
                self.assertEqual(len(found), 1)
                self.assertIn("巨大", found[0].message)
                self.assertIn(self.LOW, found[0].message)
                self.assertIn(self.HIGH, found[0].message)

    def test_an_empty_range_prepares_no_voice_so_the_temperature_goes_silent(self):
        override = {"weather": {"prerecord": {"temp_min": 10, "temp_max": 0}}}
        found = warnings_of(override)
        self.assertEqual(keys_of(found), [self.LOW])
        self.assertIn("0 件", found[0].message)
        settings = make_config(override).section("weather")
        self.assertEqual(len(weather.prerecord_phrases(settings)), 28)  # 気温の文は 1 つも無い

    def test_a_range_that_is_not_an_integer_prepares_nothing(self):
        override = nested(self.LOW, "x")
        found = warnings_of(override)
        self.assertEqual(keys_of(found), [self.LOW])
        self.assertEqual(len(weather.prerecord_phrases(make_config(override).section("weather"))), 28)

    def test_a_pop_step_that_is_not_positive_does_not_round_and_prepares_nothing(self):
        for value in (0, -10, "x", None):
            with self.subTest(value=value):
                override = {"weather": {"sentence_pop": "降水確率は{pop}パーセントなのだ。",
                                        "prerecord": {"pop_step": value}}}
                found = self.assert_tolerated(override, self.STEP)
                self.assertIn("1 以上の整数", found[0].message)
                settings = make_config(override).section("weather")
                self.assertEqual(len(weather.prerecord_phrases(settings)), 28 + 46)  # 降水確率の文は 0 件
                self.assertEqual(weather._round_pop_to_step(37, value), 37)  # 丸めず、生の値を読む

    def test_a_positive_pop_step_is_fine(self):
        for value in (1, 5, 10, 25, "10", 10.0):
            with self.subTest(value=value):
                self.assertEqual(errors_of({"weather": {"prerecord": {"pop_step": value}}}), [])
        self.assertEqual(validate(make_config({"weather": {"prerecord": {"pop_step": 10}}})), [])

    def test_the_enumeration_does_not_run_in_the_check(self):
        """検査は範囲の大きさを式で求める。巨大な範囲でも、文言を 1 つも作らずに終わる。"""
        with mock.patch.object(weather, "prerecord_phrases", side_effect=AssertionError("数え上げた")):
            found = warnings_of({"weather": {"prerecord": {"temp_max": HUGE}}})
        self.assertEqual(keys_of(found), [self.HIGH])


class SectionsTest(RuntimeTestCase):
    def test_a_missing_time_signal_section_loses_the_pips_but_not_the_announcement(self):
        run = run_runtime(make_config(nested("time_signal", None)))
        self.assertEqual(run.failures, {})
        plan = run.plans["hourly:10"]
        self.assertNotIn("時報音", " ".join(segment.label for segment in plan.segments))
        self.assertEqual(plan.spoken[0], "午前10時をお知らせしたのだ。")
        self.assertIn("時報音", warnings_of(nested("time_signal", None))[0].message)

    def test_every_section_of_the_default_config_is_classified(self):
        paths = {path for path, base in configcheck._default_paths() if isinstance(base, dict)}
        self.assertEqual(set(configcheck._SECTIONS), paths)

    def test_only_sections_the_runtime_reads_directly_break_when_replaced(self):
        """``Scheduler`` が節の中の節を直接読むもの・節をそのまま ``dict()`` に渡すものだけが error。"""
        broken = {path for path, rule in configcheck._SECTIONS.items() if rule.breaks is not None}
        self.assertEqual(broken, {"schedule.hourly", "schedule.closing", "audio.mixer",
                                  "audio.commands", "tts.voicevox"})

    def test_a_section_replaced_by_a_non_object_is_a_finding_on_the_section_only(self):
        for key in configcheck._SECTIONS:
            for value in (None, 5, "x", [], True):
                with self.subTest(key=key, value=value):
                    found = [f for f in validate(make_config(nested(key, value))) if f.key == key]
                    self.assertTrue(found)
                    self.assertEqual(keys_of(validate(make_config(nested(key, value)))), [key])

    def test_the_effect_of_each_section_is_in_the_message(self):
        for key, rule in configcheck._SECTIONS.items():
            with self.subTest(key):
                found = validate(make_config(nested(key, "x")))
                self.assertEqual(len(found), 1)
                breaks = rule.breaks is not None and rule.breaks("x") and (
                    rule.when is None or rule.when(make_config()))
                self.assertEqual(found[0].level, ERROR if breaks else WARNING)
                self.assertIn(rule.crash if breaks else rule.effect, found[0].message)

    def test_the_hint_for_a_disabled_section_points_at_enabled(self):
        for key in ("schedule", "schedule.hourly", "schedule.closing", "extra_segment", "weather"):
            with self.subTest(key):
                found = validate(make_config(nested(key, None)))
                self.assertIn("enabled", found[0].hint)

    def test_a_falsy_hourly_or_closing_section_means_no_chime_and_a_truthy_one_breaks_the_scheduler(self):
        for key, kind in (("schedule.hourly", "hourly:"), ("schedule.closing", "closing")):
            for value in (None, 0, "", [], False):
                with self.subTest(key=key, value=value):
                    run = run_runtime(make_config(nested(key, value)))
                    self.assertEqual(run.failures, {})
                    self.assertFalse(any(k.startswith(kind) for keys in run.events.values() for k in keys))
                    self.assertEqual(errors_of(nested(key, value)), [])
            for value in (5, "x", True, [1]):
                with self.subTest(key=key, value=value):
                    self.assertIn("予定", runtime_failures(make_config(nested(key, value))))
                    self.assertEqual(keys_of(errors_of(nested(key, value))), [key])

    def test_a_dict_like_value_is_not_an_error_for_the_sections_passed_to_dict(self):
        """``dict("")`` や ``dict([])`` は空の辞書になるので、ランタイムは動く。"""
        for key in ("audio.mixer", "audio.commands"):
            for value in ("", []):
                with self.subTest(key=key, value=value):
                    self.assertEqual(errors_of(nested(key, value)), [])
        with self.assertRaises(TypeError):
            audio.PygamePlayer({"mixer": None})

    def test_a_replaced_section_hides_its_children_from_the_other_checks(self):
        found = validate(make_config({"schedule": {"hourly": None, "closing": {"hour": 24}}}))
        self.assertEqual([(f.level, f.key) for f in found],
                         [(WARNING, "schedule.hourly"), (ERROR, "schedule.closing.hour")])


class InactiveSectionTest(RuntimeTestCase):
    """止めた（``enabled`` が偽の）時報・閉館放送の中身は、``Scheduler`` が読まない。壊れていても動く。"""

    def test_a_broken_value_in_a_disabled_section_is_a_warning_with_a_note(self):
        override = {"schedule": {"closing": {"enabled": False, "hour": 24}}}
        found = self.assert_tolerated(override, "schedule.closing.hour")
        self.assertIn("enabled が false", found[0].message)
        self.assertNotIn("直るまで", found[0].hint)

    def test_the_same_value_is_an_error_when_the_section_is_on(self):
        override = {"schedule": {"closing": {"enabled": True, "hour": 24}}}
        self.assert_breaks(override, "schedule.closing.hour", "予定")

    def test_enabled_is_read_like_the_scheduler_reads_it(self):
        # 文字列の "false" は真（有効）。0・null・空文字列は偽（無効）
        for enabled, active in (("false", True), (1, True), (0, False), (None, False), ("", False)):
            with self.subTest(enabled=enabled):
                override = {"schedule": {"closing": {"enabled": enabled, "hour": 24}}}
                self.assertEqual(bool(errors_of(override)), active)
                self.assertEqual(bool(runtime_failures(make_config(override))), active)

    def test_the_hourly_range_is_read_even_when_the_section_is_off(self):
        override = {"schedule": {"hourly": {"enabled": False, "start_hour": "x"}}}
        self.assertEqual(keys_of(errors_of(override)), ["schedule.hourly.start_hour"])


class FindingQualityTest(RuntimeTestCase):
    """全部の規則を一度に踏ませて、指摘の体裁（日本語・キー・直し方・既定値の案内）を揃えて確かめる。"""

    BAD_ERRORS = {
        "timezone": "Asia/Tokio",
        "logging": {"level": "LOUD", "format": "%(foo)s"},
        "schedule": {"max_sleep_seconds": 0, "catchup_grace_seconds": "soon",
                     "prepare_lead_seconds": 1e12,
                     "hourly": {"minute": 60, "weekdays": [7, [1]], "skip_hours": [-1, "x"]},
                     "closing": {"hour": 24, "minute": -1, "weekdays": None}},
        "audio": {"backend": 1, "gap_ms": -1, "mixer": {"frequency": "x"}},
        "tts": {"engines": None},
        "time_signal": {"short_pip_count": "x"},
    }

    BAD_WARNINGS = {
        "schedule": {"hourly": {"start_hour": 17, "weekdays": ["0"], "skip_hours": [24]},
                     "closing": {"hour": 16.5}},
        "audio": {"backend": "pygam"},
        "tts": {"engines": ["voice"]},
        "weather": {"sentence_temp": "{temp",
                    "open_meteo": {"locations": [{"label": "x", "latitude": 135.0, "longitude": 35.0}]},
                    "prerecord": {"temp_max": 10 ** 6, "pop_step": 0}},
        "time_signal": {"announce_template": "{hours}", "noon_template": 5, "volume": "x"},
        "extra_segment": {"weather_hours": [24]},
    }

    def test_there_is_an_error_for_each_broken_key(self):
        found = errors_of(self.BAD_ERRORS)
        self.assertEqual(
            set(keys_of(found)),
            {"timezone", "logging.level", "logging.format", "schedule.max_sleep_seconds",
             "schedule.catchup_grace_seconds", "schedule.prepare_lead_seconds",
             "schedule.hourly.minute", "schedule.hourly.weekdays", "schedule.hourly.skip_hours",
             "schedule.closing.hour", "schedule.closing.minute", "schedule.closing.weekdays",
             "audio.backend", "audio.gap_ms", "audio.mixer.frequency", "tts.engines",
             "time_signal.short_pip_count"})

    def test_errors_are_on_real_keys_in_japanese_and_say_which_default_is_used_meanwhile(self):
        defaults = Config(DEFAULT_CONFIG)
        for finding in errors_of(self.BAD_ERRORS):
            with self.subTest(key=finding.key):
                self.assertIsNot(defaults.get(finding.key, self), self)
                self.assertRegex(finding.message, _JAPANESE)
                self.assertIn(finding.key, finding.message)
                self.assertRegex(finding.hint, _JAPANESE)
                self.assertIn("直るまでは既定値 {0} で動かします".format(
                    json.dumps(defaults.get(finding.key), ensure_ascii=False)), finding.hint)

    def test_there_is_a_warning_for_each_tolerated_key_and_none_of_them_is_an_error(self):
        self.assertEqual(errors_of(self.BAD_WARNINGS), [])
        self.assertEqual(
            set(keys_of(warnings_of(self.BAD_WARNINGS))),
            {"schedule.hourly.start_hour", "schedule.hourly.weekdays", "schedule.hourly.skip_hours",
             "schedule.closing.hour", "audio.backend", "tts.engines", "weather.sentence_temp",
             "weather.open_meteo.locations", "weather.prerecord.temp_max",
             "weather.prerecord.pop_step", "time_signal.announce_template",
             "time_signal.noon_template", "time_signal.volume", "extra_segment.weather_hours"})

    def test_warnings_say_how_to_fix_it_in_japanese_and_never_promise_a_default(self):
        for finding in warnings_of(self.BAD_WARNINGS):
            with self.subTest(key=finding.key):
                self.assertRegex(finding.message, _JAPANESE)
                self.assertIn(finding.key, finding.message)
                self.assertRegex(finding.hint, _JAPANESE)
                self.assertNotIn("直るまで", finding.hint)
                self.assertNotIn("直るまで", finding.message)

    def test_every_finding_of_every_table_row_is_self_contained(self):
        """メッセージだけ読めば、何がどう動くかが分かる（どの指摘にも日本語の説明と、直し方がある）。"""
        for label, override, key, _ in BREAKING:
            for finding in validate(make_config(override)):
                with self.subTest(label=label, key=finding.key):
                    self.assertRegex(finding.message, _JAPANESE)
                    self.assertRegex(finding.hint, _JAPANESE)
        for label, override, key in TOLERATED:
            for finding in validate(make_config(override)):
                with self.subTest(label=label, key=finding.key):
                    self.assertRegex(finding.message, _JAPANESE)
                    self.assertRegex(finding.hint, _JAPANESE)


class CheckConfigTest(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(data, str):
                handle.write(data)
            else:
                json.dump(data, handle, ensure_ascii=False)
        return path

    def test_the_default_config_has_no_findings(self):
        self.assertEqual(check_config(load_config()), [])
        self.assertEqual(check_config(Config(DEFAULT_CONFIG)), [])

    def test_defaults_are_never_opened_as_a_file(self):
        with mock.patch("chime.configcheck.read_json") as read_json:
            check_config(load_config())
        read_json.assert_not_called()

    def test_every_source_file_is_reread(self):
        local = self.write("config.json", {"timezone": "UTC"})
        explicit = self.write("other.json", {"tts": {"cache_dir": "x"}})
        config = load_config(explicit, base_dir=self.tmp.name)
        with mock.patch("chime.configcheck.read_json", return_value={}) as read_json:
            check_config(config)
        self.assertEqual([call.args[0] for call in read_json.call_args_list], [local, explicit])

    def test_file_findings_carry_their_source(self):
        path = self.write("pi.json", {"schedule": {"hourly": {"minite": 5, "start_hour": 10}},
                                      "extra_segment": {"mode": "choice"}})
        config = load_config(path, base_dir=self.tmp.name)
        findings = check_config(config)
        self.assertEqual({f.source for f in findings}, {path})
        self.assertEqual(sorted(keys_of(findings)),
                         ["extra_segment.mode", "schedule.hourly.minite",
                          "schedule.hourly.start_hour"])

    def test_removed_keys_are_reported_even_though_load_config_strips_them(self):
        path = self.write("pi.json", {"weather": {"provider": "jma"}})
        config = load_config(path, base_dir=self.tmp.name)
        self.assertNotIn("provider", config.section("weather"))
        findings = check_config(config)
        self.assertEqual([(f.level, f.key) for f in findings], [(WARNING, "weather.provider")])

    def test_findings_are_sorted_errors_then_warnings_then_info(self):
        path = self.write("pi.json", {
            "timezone": "Asia/Tokyo",                      # info
            "shedule": {},                                 # warning
            "schedule": {"hourly": {"minute": 99}},        # error
            "weather": {"provider": "jma"},                # warning
            "audio": {"backend": "mock"}})                 # （既定と違うので何も出ない）
        findings = check_config(load_config(path, base_dir=self.tmp.name))
        self.assertEqual([f.level for f in findings], [ERROR, WARNING, WARNING, INFO])
        # 同じレベルの中では、見つけた順（ファイルに書かれた順）のまま
        self.assertEqual([f.key for f in findings if f.level == WARNING],
                         ["shedule", "weather.provider"])

    def test_a_validate_warning_sorts_between_the_errors_and_the_info(self):
        path = self.write("pi.json", {"timezone": "Asia/Tokyo",              # info
                                      "schedule": {"hourly": {"start_hour": "9", "minute": 99}}})
        findings = check_config(load_config(path, base_dir=self.tmp.name))
        self.assertEqual([(f.level, f.key) for f in findings], [
            (ERROR, "schedule.hourly.minute"), (WARNING, "schedule.hourly.start_hour"),
            (INFO, "timezone")])

    def test_a_value_error_is_attributed_to_the_file_that_set_it(self):
        self.write("config.json", {"schedule": {"hourly": {"minute": 61}}})
        explicit = self.write("other.json", {"schedule": {"hourly": {"minute": 62}}})
        config = load_config(explicit, base_dir=self.tmp.name)
        found = [f for f in check_config(config) if f.level == ERROR]
        self.assertEqual(keys_of(found), ["schedule.hourly.minute"])
        self.assertEqual(found[0].source, explicit)

    def test_a_warning_is_attributed_to_its_file_too(self):
        path = self.write("pi.json", {"schedule": {"hourly": {"weekdays": ["0"]}}})
        found = [f for f in check_config(load_config(path, base_dir=self.tmp.name)) if f.level == WARNING]
        self.assertEqual([(f.key, f.source) for f in found], [("schedule.hourly.weekdays", path)])

    def test_an_error_from_the_earlier_file_names_that_file(self):
        local = self.write("config.json", {"audio": {"backend": 1}})
        explicit = self.write("other.json", {"timezone": "UTC"})
        config = load_config(explicit, base_dir=self.tmp.name)
        found = [f for f in check_config(config) if f.level == ERROR]
        self.assertEqual([(f.key, f.source) for f in found], [("audio.backend", local)])

    def test_an_error_overridden_by_a_later_file_is_gone(self):
        self.write("config.json", {"schedule": {"hourly": {"minute": 61}}})
        explicit = self.write("other.json", {"schedule": {"hourly": {"minute": 30}}})
        config = load_config(explicit, base_dir=self.tmp.name)
        self.assertEqual([f for f in check_config(config) if f.level == ERROR], [])

    def test_a_value_not_written_in_any_file_has_no_source(self):
        config = Config(deep_merge(DEFAULT_CONFIG, {"timezone": "Asia/Tokio"}),
                        sources=["<defaults>"])
        self.assertEqual(check_config(config)[0].source, "")

    def test_comment_keys_in_the_file_are_not_reported(self):
        path = self.write("pi.json", {"_comment": "メモ", "schedule": {"_note": 1}})
        self.assertEqual(check_config(load_config(path, base_dir=self.tmp.name)), [])

    def test_a_file_that_cannot_be_reread_is_a_warning_not_a_crash(self):
        missing = os.path.join(self.tmp.name, "gone.json")
        config = Config(DEFAULT_CONFIG, sources=["<defaults>", missing])
        findings = check_config(config)
        self.assertEqual([(f.level, f.key, f.source) for f in findings],
                         [(WARNING, "", missing)])
        self.assertIn("読み直せませんでした", findings[0].message)

    def test_a_file_whose_top_level_is_not_an_object_is_a_warning(self):
        path = self.write("list.json", '["配列"]')
        config = Config(DEFAULT_CONFIG, sources=["<defaults>", path])
        findings = check_config(config)
        self.assertEqual([(f.level, f.source) for f in findings], [(WARNING, path)])

    def test_check_config_does_not_modify_the_config(self):
        path = self.write("pi.json", {"schedule": {"hourly": {"minute": 61}}})
        config = load_config(path, base_dir=self.tmp.name)
        snapshot = copy.deepcopy(config.data)
        check_config(config)
        self.assertEqual(config.data, snapshot)


class SanitizedTest(RuntimeTestCase):
    def test_the_default_config_comes_back_equal_with_no_errors(self):
        config = Config(DEFAULT_CONFIG, base_dir="/opt/chime", sources=["<defaults>"])
        fixed, errors = sanitized(config)
        self.assertEqual(errors, [])
        self.assertEqual(fixed, config)
        self.assertEqual(fixed.data, DEFAULT_CONFIG)
        self.assertIsNot(fixed, config)

    def test_only_the_broken_keys_go_back_to_the_defaults(self):
        config = make_config({"schedule": {"hourly": {"minute": 60, "end_hour": 15},
                                           "closing": {"minute": 30}},
                              "weather": {"timeout_seconds": 5.0}})
        fixed, errors = sanitized(config)
        self.assertEqual(keys_of(errors), ["schedule.hourly.minute"])
        self.assertEqual(fixed.get("schedule.hourly.minute"), 0)
        self.assertEqual(fixed.get("schedule.hourly.end_hour"), 15)
        self.assertEqual(fixed.get("schedule.closing.minute"), 30)
        self.assertEqual(fixed.get("weather.timeout_seconds"), 5.0)

    def test_the_returned_errors_are_the_error_findings_of_validate(self):
        config = make_config({"timezone": "Asia/Tokio", "tts": {"engines": None}})
        _, errors = sanitized(config)
        self.assertEqual(errors, [f for f in validate(config) if f.level == ERROR])
        self.assertEqual({f.level for f in errors}, {ERROR})

    def test_base_dir_and_sources_are_kept(self):
        config = make_config({"timezone": "Asia/Tokio"},
                             sources=["<defaults>", "/etc/chime.json"])
        fixed, _ = sanitized(config)
        self.assertEqual(fixed.base_dir, "/opt/chime")
        self.assertEqual(fixed.sources, ["<defaults>", "/etc/chime.json"])

    def test_the_original_is_not_modified(self):
        config = make_config({"timezone": "Asia/Tokio", "schedule": {"max_sleep_seconds": 0}})
        snapshot = copy.deepcopy(config.data)
        sanitized(config)
        self.assertEqual(config.data, snapshot)

    def test_the_result_shares_nothing_with_the_defaults(self):
        fixed, _ = sanitized(make_config({"schedule": {"hourly": {"weekdays": 9}}}))
        fixed.data["schedule"]["hourly"]["weekdays"].append(5)
        fixed.data["tts"]["engines"].append("x")
        self.assertEqual(DEFAULT_CONFIG["schedule"]["hourly"]["weekdays"], [0, 1, 2, 3, 4])
        self.assertEqual(DEFAULT_CONFIG["tts"]["engines"], ["prerecorded", "voicevox"])

    def test_warnings_and_unknown_keys_are_left_alone(self):
        config = make_config({"nope": 1, "schedule": {"hourly": {"minite": 5}}})
        fixed, errors = sanitized(config)
        self.assertEqual(errors, [])
        self.assertEqual(fixed, config)

    def test_a_config_with_only_warnings_comes_back_equal(self):
        for label, override, key in TOLERATED:
            with self.subTest(label):
                config = make_config(override)
                fixed, errors = sanitized(config)
                self.assertEqual((errors, fixed), ([], config))

    def test_a_section_replaced_by_a_scalar_comes_back_whole_when_it_breaks_the_runtime(self):
        fixed, errors = sanitized(make_config({"schedule": {"hourly": 5}}))
        self.assertEqual(keys_of(errors), ["schedule.hourly"])
        self.assertEqual(fixed.data["schedule"]["hourly"], DEFAULT_CONFIG["schedule"]["hourly"])

    def test_a_section_the_runtime_survives_is_left_as_written(self):
        fixed, errors = sanitized(make_config({"schedule": 5}))
        self.assertEqual((errors, fixed.data["schedule"]), ([], 5))

    def test_a_list_of_locations_with_a_bad_place_is_never_reset(self):
        config = make_config({"weather": {"open_meteo": {"locations": [
            {"label": "京都", "latitude": 35.0116, "longitude": 135.7681},
            {"label": "x", "latitude": 135.0, "longitude": 35.0}]}}})
        fixed, errors = sanitized(config)
        self.assertEqual(errors, [])
        self.assertEqual(fixed.get("weather.open_meteo.locations"),
                         config.get("weather.open_meteo.locations"))

    def test_start_after_end_is_left_as_written(self):
        fixed, errors = sanitized(make_config({"schedule": {"hourly": {"start_hour": 12, "end_hour": 9}}}))
        self.assertEqual(errors, [])
        self.assertEqual((fixed.get("schedule.hourly.start_hour"), fixed.get("schedule.hourly.end_hour")),
                         (12, 9))

    def test_a_string_hour_is_left_as_the_string_so_the_start_stays_at_nine(self):
        """前の版は ``"9"`` を 9 時として動かしていた。置き換えて 10 時に変えてはならない。"""
        config = make_config({"schedule": {"hourly": {"start_hour": "9"}}})
        fixed, errors = sanitized(config)
        self.assertEqual(errors, [])
        self.assertEqual(fixed.get("schedule.hourly.start_hour"), "9")
        self.assertEqual(run_runtime(fixed).events["2026-10-09"][0], "hourly:09")

    def test_every_kind_of_error_is_gone_after_sanitizing(self):
        for label, override, key, stage in BREAKING:
            with self.subTest(label):
                fixed, errors = sanitized(make_config(override))
                self.assertIn(key, keys_of(errors))
                self.assertEqual(errors_of_config(fixed), [])
                if key not in ("schedule.hourly.skip_hours", "schedule.hourly.weekdays",
                               "schedule.closing.weekdays", "audio.commands"):
                    self.assertEqual(fixed.get(key), Config(DEFAULT_CONFIG).get(key))

    def test_sanitizing_twice_changes_nothing_more(self):
        for label, override, key, stage in BREAKING:
            with self.subTest(label):
                once, _ = sanitized(make_config(override))
                twice, errors = sanitized(once)
                self.assertEqual((errors, twice), ([], once))


class HazardsTest(RuntimeTestCase):
    """検査の目的そのもの: タイプミスが起動時の例外・CPU の空回り・9 時間のずれにならない。"""

    def app(self, config):
        return ChimeApp(Config(config.data, base_dir=self.tmp), backend="mock", dry_run=True)

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def test_a_text_where_a_number_belongs_would_crash_startup_and_is_caught(self):
        config = make_config({"schedule": {"catchup_grace_seconds": "soon"}})
        # 直さないまま起動すると例外で落ち、systemd が再起動を繰り返す
        with self.assertRaises(ValueError):
            self.app(config)
        fixed, errors = sanitized(config)
        self.assertEqual(keys_of(errors), ["schedule.catchup_grace_seconds"])
        self.assertEqual(self.app(fixed).scheduler.grace, 120.0)

    def test_a_zero_wait_would_spin_the_cpu_and_is_caught(self):
        config = make_config({"schedule": {"max_sleep_seconds": 0}})
        self.assertEqual(self.app(config).scheduler.max_sleep, 0.0)
        fixed, errors = sanitized(config)
        self.assertEqual(keys_of(errors), ["schedule.max_sleep_seconds"])
        self.assertEqual(self.app(fixed).scheduler.max_sleep, 30.0)

    def test_a_misspelt_timezone_would_fall_back_to_local_time_and_is_caught(self):
        config = make_config({"timezone": "Asia/Tokio"})
        self.assertIsNone(self.app(config).tzinfo)
        fixed, errors = sanitized(config)
        self.assertEqual(keys_of(errors), ["timezone"])
        self.assertEqual(self.app(fixed).tzinfo, ZoneInfo("Asia/Tokyo"))

    def test_a_closing_hour_of_24_would_crash_the_schedule_and_is_caught(self):
        config = make_config({"schedule": {"closing": {"hour": 24}}})
        app = self.app(config)
        with self.assertRaises(ValueError):
            app.scheduler.upcoming(FIXED_NOW.replace(tzinfo=app.tzinfo), limit=5)
        fixed, errors = sanitized(config)
        self.assertEqual(keys_of(errors), ["schedule.closing.hour"])
        app = self.app(fixed)
        self.assertEqual(len(app.scheduler.upcoming(FIXED_NOW.replace(tzinfo=app.tzinfo), limit=5)), 5)

    def test_a_huge_lead_would_crash_the_schedule_and_is_caught(self):
        config = make_config({"schedule": {"prepare_lead_seconds": 10 ** 12}})
        app = self.app(config)
        with self.assertRaises(OverflowError):
            app.scheduler.upcoming(FIXED_NOW.replace(tzinfo=app.tzinfo), limit=5)
        fixed, errors = sanitized(config)
        self.assertEqual(keys_of(errors), ["schedule.prepare_lead_seconds"])

    def test_a_format_logging_cannot_use_would_lose_every_log_line_and_is_caught(self):
        config = make_config({"logging": {"format": "%(foo)s"}})
        _, failures, _ = logging_outcome(config)
        self.assertEqual(len(failures), 1)
        fixed, errors = sanitized(config)
        self.assertEqual(keys_of(errors), ["logging.format"])
        self.assertEqual(logging_outcome(fixed)[1], [])


class MirroredKillerTest(unittest.TestCase):
    """変異テストで見つかった、見逃されていた振る舞い（configcheck の分）。"""

    def test_set_default_creates_missing_parents(self):
        data = {}
        configcheck._set_default(data, "schedule.hourly.start_hour")
        self.assertEqual(data, {"schedule": {"hourly": {"start_hour": 10}}})

    def test_set_default_replaces_a_parent_that_is_not_a_dict(self):
        data = {"schedule": None}
        configcheck._set_default(data, "schedule.hourly.start_hour")
        self.assertEqual(data["schedule"], {"hourly": {"start_hour": 10}})

    def test_a_float_channel_count_is_read_by_int_so_it_is_a_warning_not_an_error(self):
        """``audio.mixer.channels`` は ``int()`` で読まれる。``2.0`` は動くので warning。読めない値は error。"""
        config = make_config({"audio": {"mixer": {"channels": 2.0}}})
        self.assertEqual([(f.level, f.key) for f in validate(config)], [(WARNING, "audio.mixer.channels")])
        self.assertEqual(int(2.0), 2)
        config = make_config({"audio": {"mixer": {"channels": "stereo"}}})
        self.assertEqual([(f.level, f.key) for f in validate(config)], [(ERROR, "audio.mixer.channels")])

    @staticmethod
    def findings_for(override, key):
        return [finding for finding in validate(make_config(override)) if finding.key == key]

    def engines_finding(self, names):
        (finding,) = self.findings_for({"tts": {"engines": names}}, "tts.engines")
        return finding

    def test_prerecorded_missing_and_nothing_unknown_is_worded_as_missing_not_as_unknown(self):
        """作り置きが無いだけ（知らない名前は無い）のとき、「知らないエンジン名」とは言わない。"""
        for names in (["voicevox"], []):
            finding = self.engines_finding(names)
            self.assertIn("prerecorded がありません（今は", finding.message, names)
            self.assertNotIn("知らないエンジン名", finding.message, names)

    def test_unknown_names_with_prerecorded_present_get_the_list_of_valid_names_as_the_fix(self):
        """作り置きは入っていて知らない名前がある。直し方は使える名前の一覧で、作り置きを入れろとは言わない。"""
        finding = self.engines_finding(["prerecorded", "foo"])
        self.assertIn("使えるのは", finding.hint)
        self.assertNotIn("を入れてください", finding.hint)

    def test_the_valid_names_are_added_to_the_fix_only_when_a_name_was_unknown(self):
        self.assertIn("使えるのは", self.engines_finding(["foo"]).hint)
        self.assertNotIn("使えるのは", self.engines_finding(["voicevox"]).hint)

    def test_the_unused_note_appears_only_while_voicevox_is_not_listed(self):
        """VOICEVOX の待ち時間の「使われていません」の注記は、``tts.engines`` に voicevox が無い間だけ付く。"""
        key = "tts.voicevox.probe_timeout_seconds"
        note = "今は tts.engines に voicevox が無いので"
        read = self.findings_for({"tts": {"engines": ["prerecorded", "voicevox"],
                                          "voicevox": {"probe_timeout_seconds": 1e12}}}, key)
        self.assertEqual([f.level for f in read], [ERROR])
        self.assertNotIn(note, read[0].message)
        unread = self.findings_for({"tts": {"engines": ["prerecorded"],
                                            "voicevox": {"probe_timeout_seconds": 1e12}}}, key)
        self.assertEqual([f.level for f in unread], [WARNING])
        self.assertIn(note, unread[0].message)

    def test_a_negative_number_is_too_small_and_nan_is_not_a_number_for_a_rule_that_breaks_on_nan(self):
        """下限を割った数は「小さすぎます」、``NaN`` は「数値として使えません」（言い分けを取り違えない）。"""
        key = "audio.mock_max_seconds"
        (negative,) = self.findings_for(nested(key, -1), key)
        self.assertIn("小さすぎます", negative.message)
        (not_a_number,) = self.findings_for(nested(key, NAN), key)
        self.assertIn("数値として使えません", not_a_number.message)
        self.assertNotIn("小さすぎます", not_a_number.message)

    def test_a_text_command_is_shown_as_its_first_characters_cut_at_30(self):
        """文字列で書いた外部コマンドの例示は、先頭の 12 文字を 30 文字までで示す。"""
        (finding,) = self.findings_for(nested("audio.commands", {".wav": "aplay -q {path}"}), "audio.commands")
        self.assertIn('（".wav" は ["a", "p", "l", "a", "y", " "…）', finding.message)

    def test_only_errors_would_be_logged_as_errors(self):
        """起動時に ERROR で残すのは ``sanitized()`` が返した error だけ（warning と info は含まない）。

        error 1 件（max_sleep_seconds）＋ warning（知らないキー・数値の書き方）＋ info（既定値と同じ値）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pi.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"schedule": {"max_sleep_seconds": 0, "bogus_key": 1,
                                        "hourly": {"start_hour": "9"}},
                           "timezone": "Asia/Tokyo"}, handle)
            config = load_config(path, base_dir=tmp)
            _effective, errors = sanitized(config)
            levels = [f.level for f in check_config(config)]
        self.assertEqual(keys_of(errors), ["schedule.max_sleep_seconds"])
        self.assertEqual(levels, [ERROR, WARNING, WARNING, INFO])


class LogConfigErrorsTest(RuntimeTestCase):
    """起動時に ERROR で残すのは、``sanitized()`` が返した error だけ（``chime.cli.log_config_errors``）。"""

    def config(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "pi.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return load_config(path, base_dir=tmp.name)

    def test_nothing_is_reread_when_there_are_no_errors(self):
        from chime import cli
        config = self.config({"schedule": {"bogus_key": 1, "hourly": {"start_hour": "9"}}})
        with mock.patch.object(configcheck, "check_config", side_effect=AssertionError("reread")):
            cli.log_config_errors(config, [])

    def test_only_errors_are_logged_at_error_level(self):
        from chime import cli
        # error 1 件（max_sleep_seconds）＋ warning（知らないキー・数値の書き方）＋ info（既定値と同じ値）
        config = self.config({"schedule": {"max_sleep_seconds": 0, "bogus_key": 1,
                                           "hourly": {"start_hour": "9"}},
                              "timezone": "Asia/Tokyo"})
        _effective, errors = sanitized(config)
        self.assertEqual(len(errors), 1)
        with logs_enabled(), self.assertLogs("chime", level="ERROR") as captured:
            cli.log_config_errors(config, errors)
        self.assertEqual(len(captured.records), 1, [r.getMessage() for r in captured.records])
        self.assertIn("schedule.max_sleep_seconds", captured.records[0].getMessage())


class LayeringTest(unittest.TestCase):
    def test_importing_configcheck_does_not_pull_in_the_playback_stack(self):
        """設定の検査に pygame（再生系）は要らない。名前の写しを持つのは、そのため。"""
        code = "\n".join([
            "import sys",
            "import chime.configcheck",
            "banned = ('chime.audio', 'chime.tts', 'chime.sequence', 'chime.app', 'pygame')",
            "loaded = [name for name in banned if name in sys.modules]",
            "print(','.join(loaded))",
            "sys.exit(1 if loaded else 0)",
        ])
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True)
        self.assertEqual(result.returncode, 0,
                         "chime.configcheck が再生系を import しています: {0}\n{1}".format(
                             result.stdout.strip(), result.stderr.strip()))


if __name__ == "__main__":
    unittest.main()
