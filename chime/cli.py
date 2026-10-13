"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import sys
from typing import List, Optional, Tuple

from . import buildinfo, check, configcheck, phrases, status, timesignal
from .app import ChimeApp
from .config import DEFAULT_CONFIG, Config, ConfigError, load_config
from .logsetup import emit_early_error, setup_logging
from .scheduler import format_events
from .weather import WeatherError

logger = logging.getLogger("chime")

#: ``--test-hourly`` を値なしで指定したときに argparse が渡す値。「いまの時刻」を
#: 表す。0〜23 に収まらない負の値も同じ意味に扱う（:func:`play_tests`）。
CURRENT_HOUR = -1

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
  campus_chime.py --check              設置状態を点検する（設定・作り置きの音声・音源・書き込み。鳴らさず、何も書かない）
  campus_chime.py --status             いまの状態（版・時刻の同期・サービス・直近の放送・次の予定）を表示する
  campus_chime.py --wait-idle          放送の時間帯なら、終わるまで待つ（最大 360 秒。更新の前に使う）
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="campus_chime.py",
        description="キャンパス時報システム（時報 ＋ 閉館放送）",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=buildinfo.version_string())
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
    actions.add_argument("--test-hourly", nargs="?", const=CURRENT_HOUR, type=int,
                         metavar="HOUR",
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
    actions.add_argument("--check", action="store_true",
                         help="設置状態を点検して終了（設定・作り置きの音声・音源・書き込み先。"
                              "鳴らさず、何も書かない。NG があれば終了コード 1）")
    actions.add_argument("--status", action="store_true",
                         help="いまの状態（版・時刻の同期・サービス・直近の放送・次の予定）を表示して終了"
                              "（何も書かない。気になる点があれば終了コード 1）")
    actions.add_argument("--wait-idle", nargs="?", const=status.WAIT_IDLE_MAX, type=int,
                         metavar="SECONDS",
                         help="放送の時間帯なら、終わるまで待って終了（既定・最大 {0} 秒。"
                              "待ちきれなければ終了コード 1）".format(status.WAIT_IDLE_MAX))
    return parser


def logging_settings(config: Config) -> Tuple[str, str]:
    """ログの ``(水準, 書式)``。設定に無い（または ``null`` の）ときは既定設定の値。

    戻り先を別の文字列で持たない（既定設定を変えたときに食い違い、書式から
    ``%(name)s`` が抜けるなどしていた）。
    """
    defaults = DEFAULT_CONFIG["logging"]
    level = config.get("logging.level")
    log_format = config.get("logging.format")
    return (defaults["level"] if level is None else level,
            defaults["format"] if log_format is None else log_format)


def log_config_errors(config: Config, errors: List[configcheck.Finding]) -> None:
    """既定値に置き換えた設定の誤りを、1 件ずつ ERROR で残す（キー・内容・直し方・出どころ）。

    ``errors`` は :func:`chime.configcheck.sanitized` が返した誤り。それには値の出どころの
    設定ファイルが無いので、誤りがあるときだけ、出どころ付きで同じ誤りを求め直して残す
    （どのファイルを直せばよいかが分かる）。
    """
    if not errors:
        return
    for finding in configcheck.check_config(config):
        if finding.level == configcheck.ERROR:
            logger.error("設定の誤り: %s", finding.describe())


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

    # 動かす設定は、危ない値（範囲外・型違いなど）だけを既定値に戻したもの。異常終了
    # させても systemd の Restart=always が再起動を繰り返すだけで、時報は鳴らないため。
    # --print-config は読んだとおりを見せるので、そのまま。
    effective, errors = (config, []) if args.print_config else configcheck.sanitized(config)
    level, log_format = logging_settings(effective)
    setup_logging(args.log_level or level, log_format,
                  str(effective.get("timezone", "")), stream=log_stream)

    if args.print_config:
        print(json.dumps(config.data, ensure_ascii=False, indent=2))
        return 0

    # --check は、書かれたとおりの設定を調べて誤りを自分で表示する（ここで残すと二重になる）。
    if args.check:
        return check.run_check(config)

    log_config_errors(config, errors)
    # --status と --wait-idle は見るだけ。状態ファイルを書き換えないよう read-only にする。
    look_only = args.status or args.wait_idle is not None
    app = ChimeApp(effective, backend=args.backend, dry_run=args.dry_run or look_only)
    return dispatch(app, args)


def dispatch(app: ChimeApp, args: argparse.Namespace) -> int:
    """動作モードに応じた処理を行う（どれも未指定なら常駐）。"""
    if args.status:
        return status.run_status(app)

    if args.wait_idle is not None:
        return status.wait_idle(app.scheduler, args.wait_idle)

    if args.schedule is not None:
        return show_schedule(app, args.schedule)

    if args.generate_assets:
        return generate_assets(app)

    if args.weather:
        return show_weather(app)

    if args.say is not None:
        return say_text(app, args.say)

    if args.test_hourly is not None or args.test or args.test_all:
        return play_tests(app,
                          hourly=args.test_hourly is not None or args.test_all,
                          closing=args.test or args.test_all,
                          hour_arg=args.test_hourly)

    app.install_signal_handlers()
    return app.run_forever()


def show_schedule(app: ChimeApp, count: int) -> int:
    """次回以降の予定を ``count`` 件表示する。"""
    print("現在時刻: {0}".format(app.now().strftime("%Y-%m-%d %H:%M:%S %Z")))
    print("次回以降の予定:")
    # 0 や負の値は 1 件に丸める（upcoming の limit の解釈に頼らない）。
    print(format_events(app.scheduler.upcoming(limit=max(1, count))))
    return 0


def say_text(app: ChimeApp, text: str) -> int:
    """任意の文言を読み上げる（``--say``）。再生できなければ 1、空の文言は 2。"""
    # 空の文言を「指定なし」と見なすと常駐ループに落ちてしまうため、引数エラーにする。
    if not text.strip():
        print("読み上げる文言が空です。", file=sys.stderr)
        return 2
    warn_if_not_prerecorded(app, text)
    app.log_environment()
    plan = app.builder.build_text(text)
    return 0 if app.play(plan) else 1


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

    Pi では音声を新たに作らない（作り置き ``assets/voice/`` を引くだけ）。確かめるのは
    作り置きの有無だけ（``prerecorded_lookup``）で、合成エンジンには問い合わせない。
    VOICEVOX が動いている PC で合成まで試すと、作り置きの欠けをその場で埋めて
    しまい、Pi では無音になる文言を「用意できた」と答えてしまうため。
    """
    settings = app.config.section("time_signal")
    path = timesignal.generate_time_signal(
        app.time_signal_path, settings, app.config.section("audio.mixer"))
    print("時報音を生成しました: {0}".format(path))

    failures = 0
    # 作り置きの列挙（--prune や CI と同じ）と同じ文言を、1 件ずつ確かめる。
    # ジェネレーターなので、テンプレートが壊れていても、その時刻の前までは
    # OK / NG を出してから例外になる。
    for text in phrases.announcement_phrases(app.config):
        found = app.tts.prerecorded_lookup(text)
        if found is None:
            print("  NG {0}: {1}".format(text, missing_voice_reason(app)), file=sys.stderr)
            failures += 1
            continue
        print("  OK {0} -> {1}".format(text, found))

    if failures:
        print("{0} 件の時刻アナウンスの音声を用意できませんでした。".format(failures),
              file=sys.stderr)
        print("  - 作り置き（assets/voice/）に無い場合: PC で作り直す（docs/SETUP.md 9 章 B）",
              file=sys.stderr)
        print("  - config.json が古く文言を上書きしている場合: docs/SETUP.md 10-7",
              file=sys.stderr)
        return 1
    return 0


def missing_voice_reason(app: ChimeApp) -> str:
    """作り置きに無い文言について、その理由（フォルダそのものが無いのか、文言が無いのか）。"""
    if not os.path.isdir(app.tts.prerecorded_dir):
        return "作り置きのフォルダ（assets/voice/）が見つかりません"
    return "作り置き（assets/voice/）にこの文言がありません"


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


def play_tests(app: ChimeApp, hourly: bool, closing: bool, hour_arg: Optional[int]) -> int:
    """時報・閉館放送をその場で再生する（``--test-hourly`` / ``--test`` / ``--test-all``）。

    ``hour_arg`` が ``None`` か負（:data:`CURRENT_HOUR`）なら、いまの時刻の時報を鳴らす。
    実行環境のログは時刻の検証より先に出す（指定が誤りでも、どの環境で試したかが残る）。
    時報が鳴らなくても閉館放送は続けて試し、どちらも鳴らなかったときだけ 1 を返す。
    """
    app.log_environment()
    succeeded = False
    if hourly:
        hour = app.now().hour if hour_arg is None or hour_arg < 0 else hour_arg
        if not 0 <= hour <= 23:
            print("--test-hourly は 0〜23 で指定してください。", file=sys.stderr)
            return 2
        logger.info("テストモード: %d 時の時報を再生します。", hour)
        plan = app.builder.build_hourly(hour)
        if app.play(plan):
            succeeded = True
    if closing:
        logger.info("テストモード: 閉館放送を再生します。")
        plan = app.builder.build_closing()
        if app.play(plan):
            succeeded = True
    logger.info("テストを終了します。")
    if not succeeded:
        logger.error("再生できるセグメントがありませんでした。")
        return 1
    return 0
