"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from typing import List, Optional

from . import __version__, timesignal
from .app import ChimeApp
from .config import ConfigError, load_config
from .scheduler import format_events
from .tts import TTSError
from .weather import WeatherError

logger = logging.getLogger("chime")

EPILOG = """\
使用例:
  campus_chime.py                      常駐して定刻に自動再生する（systemd 用）
  campus_chime.py --schedule           次回以降の予定を表示する
  campus_chime.py --test-hourly        いまの時刻の時報をその場で再生する
  campus_chime.py --test-hourly 12     12 時の時報をその場で再生する
  campus_chime.py --test               閉館放送（アナウンス＋蛍の光）を再生する
  campus_chime.py --weather            天気予報の読み上げ文を確認する
  campus_chime.py --say こんにちは      任意の文言を読み上げる
  campus_chime.py --generate-assets    時報音と定型文の音声を事前生成する
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="campus_chime.py",
        description="キャンパス時報システム（時報 ＋ 閉館放送）",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version="campus-chime {0}".format(__version__))
    parser.add_argument("--config", metavar="PATH",
                        help="設定ファイル（既定: config.json があれば読み込む）")
    parser.add_argument("--backend", choices=["auto", "pygame", "command", "mock"],
                        help="再生バックエンドを強制する")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="ログレベル")
    parser.add_argument("--dry-run", action="store_true",
                        help="音を鳴らさず、再生内容だけを表示する")

    actions = parser.add_argument_group("動作モード（未指定なら常駐）")
    actions.add_argument("--test", action="store_true",
                         help="閉館放送（アナウンス＋蛍の光）を即時再生して終了")
    actions.add_argument("--test-hourly", nargs="?", const=-1, type=int, metavar="HOUR",
                         help="時報を即時再生して終了（時刻を省略すると現在時刻）")
    actions.add_argument("--test-all", action="store_true",
                         help="時報と閉館放送を続けて再生して終了")
    actions.add_argument("--say", metavar="TEXT",
                         help="任意の文言を読み上げて終了")
    actions.add_argument("--weather", action="store_true",
                         help="天気予報の読み上げ文を表示して終了（--dry-run 以外では読み上げも行う）")
    actions.add_argument("--schedule", nargs="?", const=10, type=int, metavar="N",
                         help="次回以降の予定を N 件表示して終了（既定 10 件）")
    actions.add_argument("--generate-assets", action="store_true",
                         help="時報音と定型文の音声を事前生成して終了")
    actions.add_argument("--print-config", action="store_true",
                         help="読み込んだ設定を表示して終了")
    return parser


def setup_logging(level_name: str, log_format: str, timezone: str = "") -> None:
    """ログ設定。タイムスタンプは設定したタイムゾーンで表示する。"""
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    logging.basicConfig(level=level, format=log_format, stream=sys.stdout)

    if not timezone:
        return
    try:
        from zoneinfo import ZoneInfo

        tzinfo = ZoneInfo(timezone)
    except Exception:  # pragma: no cover - tzdata 欠落時は OS のローカル時刻のまま
        return

    def _converter(timestamp):
        return datetime.fromtimestamp(timestamp, tzinfo).timetuple()

    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            # インスタンス属性として差し替える（クラス属性だと self が渡ってしまう）
            handler.formatter.converter = _converter


def run(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("設定エラー: {0}".format(exc), file=sys.stderr)
        return 2

    setup_logging(args.log_level or config.get("logging.level", "INFO"),
                  config.get("logging.format", "%(asctime)s - %(levelname)s - %(message)s"),
                  str(config.get("timezone", "")))

    if args.print_config:
        print(json.dumps(config.data, ensure_ascii=False, indent=2))
        return 0

    app = ChimeApp(config, backend=args.backend, dry_run=args.dry_run)

    if args.schedule is not None:
        print("現在時刻: {0}".format(app.now().strftime("%Y-%m-%d %H:%M:%S %Z")))
        print("次回以降の予定:")
        print(format_events(app.scheduler.upcoming(limit=max(1, args.schedule))))
        return 0

    if args.generate_assets:
        return generate_assets(app)

    if args.weather:
        return show_weather(app)

    if args.say:
        app.log_environment()
        app.play(app.builder.build_text(args.say))
        return 0

    if args.test_hourly is not None or args.test or args.test_all:
        app.log_environment()
        if args.test_hourly is not None or args.test_all:
            hour = app.now().hour if (args.test_hourly is None or args.test_hourly < 0) \
                else args.test_hourly
            if not 0 <= hour <= 23:
                print("--test-hourly は 0〜23 で指定してください。", file=sys.stderr)
                return 2
            logger.info("テストモード: %d 時の時報を再生します。", hour)
            app.play(app.builder.build_hourly(hour))
        if args.test or args.test_all:
            logger.info("テストモード: 閉館放送を再生します。")
            app.play(app.builder.build_closing())
        logger.info("テストを終了します。")
        return 0

    app.install_signal_handlers()
    return app.run_forever()


def generate_assets(app: ChimeApp) -> int:
    """時報音と、時報で使う定型文の音声を事前生成する。"""
    settings = app.config.section("time_signal")
    path = timesignal.generate_time_signal(
        app.time_signal_path, settings, app.config.section("audio.mixer"))
    print("時報音を生成しました: {0}".format(path))

    hourly = app.config.section("schedule.hourly")
    hours = range(int(hourly.get("start_hour", 10)), int(hourly.get("end_hour", 16)) + 1)
    failures = 0
    for hour in hours:
        text = timesignal.announce_text(hour, settings)
        try:
            generated = app.tts.synthesize(text)
        except TTSError as exc:
            print("  NG {0}: {1}".format(text, exc), file=sys.stderr)
            failures += 1
            continue
        print("  OK {0} -> {1}".format(text, generated))

    if failures:
        print("{0} 件の音声を生成できませんでした。TTS の設定を確認してください。".format(failures),
              file=sys.stderr)
        return 1
    return 0


def show_weather(app: ChimeApp) -> int:
    """天気予報の読み上げ文を確認する。"""
    print("提供元: {0}".format(app.weather.provider))
    try:
        print("URL: {0}".format(app.weather.url()))
        text = app.weather.describe()
    except WeatherError as exc:
        print("天気予報を取得できませんでした: {0}".format(exc), file=sys.stderr)
        return 1
    print("読み上げ文: {0}".format(text))
    if not app.dry_run:
        app.play(app.builder.build_text(text))
    return 0
