"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import sys
from typing import List, Optional

from . import __version__, timesignal
from .app import ChimeApp
from .config import DEFAULT_CONFIG, ConfigError, load_config
from .logsetup import emit_early_error, setup_logging
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
  campus_chime.py --say 正午をお知らせしたのだ。  任意の文言を読み上げる
  campus_chime.py --generate-assets    時報音を生成し、時刻アナウンスの音声を用意できるか確認する
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
                         help="時報音を生成し、時刻アナウンスの音声を用意できるか"
                              "（作り置きがあるか）確認して終了。Pi では音声を新たに作らない")
    actions.add_argument("--print-config", action="store_true",
                         help="読み込んだ設定を表示して終了")
    return parser


def run(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # load_config も警告を出す（既定値の丸ごとコピー・廃止したキー）。設定を
    # 読む前なので、既定値で仮のログ設定をしておき、読み込み後に上書きする。
    # --print-config は標準出力が JSON なので、警告は標準エラー出力へ出す。
    log_stream = sys.stderr if args.print_config else None
    defaults = DEFAULT_CONFIG["logging"]
    setup_logging(args.log_level or defaults["level"], defaults["format"],
                  DEFAULT_CONFIG["timezone"], stream=log_stream)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        emit_early_error("設定エラー: {0}".format(exc))
        return 2

    setup_logging(args.log_level or config.get("logging.level", "INFO"),
                  config.get("logging.format", "%(asctime)s - %(levelname)s - %(message)s"),
                  str(config.get("timezone", "")), stream=log_stream)

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

    if args.say is not None:
        if not args.say.strip():
            print("読み上げる文言が空です。", file=sys.stderr)
            return 2
        warn_if_not_prerecorded(app, args.say)
        app.log_environment()
        plan = app.builder.build_text(args.say)
        if not app.play(plan):
            return 1
        return 0

    if args.test_hourly is not None or args.test or args.test_all:
        app.log_environment()
        succeeded = False
        if args.test_hourly is not None or args.test_all:
            hour = app.now().hour if (args.test_hourly is None or args.test_hourly < 0) \
                else args.test_hourly
            if not 0 <= hour <= 23:
                print("--test-hourly は 0〜23 で指定してください。", file=sys.stderr)
                return 2
            logger.info("テストモード: %d 時の時報を再生します。", hour)
            plan = app.builder.build_hourly(hour)
            if app.play(plan):
                succeeded = True
        if args.test or args.test_all:
            logger.info("テストモード: 閉館放送を再生します。")
            plan = app.builder.build_closing()
            if app.play(plan):
                succeeded = True
        logger.info("テストを終了します。")
        if not succeeded:
            logger.error("再生できるセグメントがありませんでした。")
            return 1
        return 0

    app.install_signal_handlers()
    return app.run_forever()


def warn_if_not_prerecorded(app: ChimeApp, text: str) -> None:
    """``--say`` の文言が作り置きに無ければ、Pi では無音になることを知らせる。

    PC で VOICEVOX が動いていれば、作り置きに無い文言もその場で合成されて鳴る
    ため、``--say`` が鳴っても Pi で鳴る証拠にならない。この案内は再生の可否
    とは別で、再生は従来どおり試みる。
    """
    text = text.strip()
    if app.tts.prerecorded_lookup(text) is not None:
        return
    known = app.tts.known_phrases()
    if not known:
        # 文言の有無ではなく、作り置きそのもの（assets/voice/ と目録）が無い。
        print("作り置き（assets/voice/）が見つかりません。Pi では読み上げがすべて無音になります。"
              "git pull が届いているか、設置場所を確認してください。", file=sys.stderr)
        return
    print("作り置き（assets/voice/）にこの文言がありません。Pi では無音になります。",
          file=sys.stderr)
    close = difflib.get_close_matches(text, known, n=3, cutoff=0.5)
    if close:
        print("  近い文言: {0}".format("、".join("「{0}」".format(phrase) for phrase in close)),
              file=sys.stderr)
    print("  作り置きに加えるには、PC で作り直します（docs/SETUP.md 8 章）",
          file=sys.stderr)


def generate_assets(app: ChimeApp) -> int:
    """時報音を生成し、時刻アナウンスの音声を用意できるか（作り置きがあるか）確認する。

    Pi では音声を新たに作らない（作り置き ``assets/voice/`` を引くだけ）。
    VOICEVOX が動いている PC では、作り置きの欠けをその場で合成して埋めて
    しまうため、PC での成功は作り置きが揃っている証拠にならない。
    """
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
        print("{0} 件の時刻アナウンスの音声を用意できませんでした。".format(failures),
              file=sys.stderr)
        print("  - 作り置き（assets/voice/）に無い場合: PC で作り直す（docs/SETUP.md 9 章 B）",
              file=sys.stderr)
        print("  - config.json が古く文言を上書きしている場合: docs/SETUP.md 10-7",
              file=sys.stderr)
        return 1
    return 0


def show_weather(app: ChimeApp) -> int:
    """天気予報の読み上げ文を確認する。"""
    print("提供元: {0}".format(app.weather.provider))
    try:
        print("URL: {0}".format(app.weather.url()))
        sentences = app.weather.describe_sentences(today=app.now().date())
    except WeatherError as exc:
        print("天気予報を取得できませんでした: {0}".format(exc), file=sys.stderr)
        return 1
    # 1 文ずつ表示・再生する。放送でも 1 文ずつ別のセグメントとして鳴らして
    # おり、連結すると照合が外れてその文が無音になるため、
    # ここでも同じ単位で扱って実際の放送と食い違わないようにする。
    for index, sentence in enumerate(sentences, start=1):
        print("読み上げ文 {0}/{1}: {2}".format(index, len(sentences), sentence))
    if not app.dry_run:
        app.play(app.builder.build_texts(sentences))
    return 0
