"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import sys
from datetime import date, timedelta
from typing import List, Optional, Tuple

from . import buildinfo, check, configcheck, phrases, status, timesignal
from .app import ChimeApp
from .config import DEFAULT_CONFIG, Config, ConfigError, load_config
from .logsetup import emit_early_error, setup_logging
from .pause import MAX_PAUSE_DAYS
from .scheduler import Event, format_events, format_schedule
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
  campus_chime.py --status             いまの状態（版・時刻の同期・サービス・放送の休止・直近の放送・次の予定・近い休館日）を表示する
  campus_chime.py --wait-idle          放送の時間帯なら、終わるまで待つ（最大 360 秒。更新の前に使う）
  campus_chime.py --pause-today        今日の時報と閉館放送を止める（常駐中のサービスにそのまま効く）
  campus_chime.py --pause-until 2026-11-05   その日まで止める（今日から最長 31 日）
  campus_chime.py --pause-today --reason 点検   理由を添えて止める（--status に出る）
  campus_chime.py --pause-off          止めている放送を再開する
"""

#: 休止の記録を書けない・消せないときに添える、次の手がかり（``--check`` は直し方まで出す）。
CHECK_HINT = "直し方は python3 campus_chime.py --check で確かめられます（「書き込み」の節）。"

#: ``--pause-off`` が、追いかけて鳴る回として挙げる最大の件数（それより多い分は件数だけ）。
CATCH_UP_LIMIT = 3

#: 日付の書き方（ASCII の数字だけ。``date.fromisoformat`` は版によって受け付ける形が違う）。
_DAY_PATTERN = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")

#: 休止の動作モード（オプション名, 引数の名前）。
PAUSE_OPTIONS = (("--pause-today", "pause_today"), ("--pause-until", "pause_until"),
                 ("--pause-off", "pause_off"))

#: 休止と一緒には指定できない動作モード（オプション名, 引数の名前）。
OTHER_MODES = (("--test", "test"), ("--test-hourly", "test_hourly"), ("--test-all", "test_all"),
               ("--say", "say"), ("--weather", "weather"), ("--schedule", "schedule"),
               ("--generate-assets", "generate_assets"), ("--print-config", "print_config"),
               ("--check", "check"), ("--status", "status"), ("--wait-idle", "wait_idle"))


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
                         help="いまの状態（版・時刻の同期・サービス・放送の休止・直近の放送・次の予定・"
                              "近い休館日）を表示して終了（何も書かない。気になる点があれば終了コード 1）")
    actions.add_argument("--wait-idle", nargs="?", const=status.WAIT_IDLE_MAX, type=int,
                         metavar="SECONDS",
                         help="放送の時間帯なら、終わるまで待って終了（既定・最大 {0} 秒。"
                              "待ちきれなければ終了コード 1）".format(status.WAIT_IDLE_MAX))

    pausing = parser.add_argument_group(
        "放送の一時休止（常駐中のサービスにそのまま効く。サービスの利用者で実行する）")
    pausing.add_argument("--pause-today", action="store_true",
                         help="今日いっぱい、時報と閉館放送を止めて終了")
    pausing.add_argument("--pause-until", metavar="YYYY-MM-DD",
                         help="その日（含む）まで放送を止めて終了（今日から最長 {0} 日。"
                              "それより長い休みは設定の schedule.closures に書く）".format(MAX_PAUSE_DAYS))
    pausing.add_argument("--pause-off", action="store_true",
                         help="止めている放送を再開して終了")
    pausing.add_argument("--reason", metavar="TEXT",
                         help="止める理由（--pause-today / --pause-until と一緒に。--status などに出る）")
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


def given(args: argparse.Namespace, name: str) -> bool:
    """引数が指定されたか（フラグは真、値を取るものは ``None`` でないとき）。

    ``--test-hourly 0`` の ``0`` を「指定なし」と取り違えないよう、真偽ではなく ``None`` と
    ``False`` そのものと比べる。
    """
    value = getattr(args, name, None)
    return value is not None and value is not False


def parse_day(text: str) -> Optional[date]:
    """``YYYY-MM-DD`` の日付。書き方が違う・カレンダーにない日付なら ``None``。"""
    match = _DAY_PATTERN.fullmatch(text)
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def pause_arguments_error(args: argparse.Namespace) -> Optional[str]:
    """休止の引数の組み合わせの誤り（利用者向けの説明）。誤りが無ければ ``None``。

    設定を読む前に確かめる（誤りなら何も始めない）。``--pause-until`` の日付が
    今日から見て長すぎる・過去であることは、今日が分かってからでないと言えないので、
    :func:`pause_until` が確かめる。
    """
    pauses = [option for option, name in PAUSE_OPTIONS if given(args, name)]
    if not pauses:
        if args.reason is not None:
            return "--reason は --pause-today か --pause-until と一緒に指定してください。"
        return None
    if len(pauses) > 1:
        return "{0} は同時に指定できません。どれか 1 つだけ指定してください。".format(
            " と ".join(pauses))
    others = [option for option, name in OTHER_MODES if given(args, name)]
    if others:
        return "{0} は {1} と同時に指定できません。".format(pauses[0], " と ".join(others))
    if args.reason is not None and args.pause_off:
        return "--reason は --pause-off では使えません（--pause-today か --pause-until と一緒に指定します）。"
    if args.pause_until is not None and parse_day(args.pause_until) is None:
        return ("--pause-until は YYYY-MM-DD の形で、カレンダーにある日付を指定してください"
                "（例: 2026-11-05）: {0}".format(args.pause_until))
    return None


def running_as_root() -> bool:
    """root（実効 uid が 0）で動いているか。uid を持たない OS では ``False``。"""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def pause_refusal(args: argparse.Namespace) -> Optional[str]:
    """休止の引数を断る理由（引数の誤り、または root での実行）。受け付けるなら ``None``。

    root で ``--pause-*`` を実行すると、``cache/`` が root の持ち物になり、サービスの
    利用者が記録を書けなくなる（履歴や再生済みの記録まで巻き込む）ので断る。
    """
    problem = pause_arguments_error(args)
    if problem is not None:
        return problem
    pauses = [option for option, name in PAUSE_OPTIONS if given(args, name)]
    if pauses and running_as_root():
        return ("root では {0} を使えません。cache/ が root の持ち物になり、サービスが"
                "記録を書けなくなるためです。サービスの利用者（pi など）で実行してください"
                "（sudo は付けません）。".format(pauses[0]))
    return None


def run(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 休止の引数の誤りと root での実行は、設定を読む前に断る（何も書かない）。
    problem = pause_refusal(args)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2

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
    if any(given(args, name) for _, name in PAUSE_OPTIONS):
        return run_pause(app, args)

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
    """次回以降の予定を ``count`` 件表示する。

    休止中なら、いちばん上に休止の期間と理由を出し、予定は休止が明けてから数える
    （止めているあいだの放送は鳴らないので、並べると鳴るように見えてしまう）。
    休館日（``schedule.closures``）のために鳴らさない日は、予定の並びの中に 1 行ずつ出る。
    """
    print("現在時刻: {0}".format(app.now().strftime("%Y-%m-%d %H:%M:%S %Z")))
    pause = app.pause.current()
    start = None
    heading = "次回以降の予定:"
    if pause is not None:
        print("止めています: {0}".format(check.describe_pause(pause)))
        start = status.start_of_day(pause.last_day + timedelta(days=1), app.tzinfo)
        heading = "次回以降の予定（休止のあいだは省いています）:"
    print(heading)
    # 0 や負の値は 1 件に丸める（upcoming の limit の解釈に頼らない）。
    print(format_schedule(app.scheduler, start, limit=max(1, count)))
    return 0


# -- 放送の一時休止 --------------------------------------------------------------
def count_broadcasts(app: ChimeApp, first_day: date, last_day: date) -> Tuple[int, int]:
    """``first_day`` から ``last_day`` までに、これから鳴る予定の ``(時報, 閉館放送)`` の回数。

    休館日と曜日の決まりを通した、スケジュールそのものの予定を数える（今日は、すでに
    鳴らした分を除く）。
    """
    now = app.now()
    hourly = closing = 0
    day = first_day
    while day <= last_day:
        for event in app.scheduler.events_for_date(day):
            if event.play_at < now:
                continue
            if event.kind == "closing":
                closing += 1
            else:
                hourly += 1
        day += timedelta(days=1)
    return hourly, closing


def resume_text(app: ChimeApp, last_day: date) -> str:
    """休止が明けて最初に鳴る放送の案内（``10/06 10:00 に再開します。``）。"""
    start = status.start_of_day(last_day + timedelta(days=1), app.tzinfo)
    for event in app.scheduler.iter_events(start):
        return "{0} に再開します。".format(event.at.strftime("%m/%d %H:%M"))
    return "このあと鳴らす予定の放送がありません（曜日や休館日の設定を確認してください）。"


def pause_unavailable(app: ChimeApp) -> bool:
    """休止の記録の場所が空（止める機能を使わない設定）なら、そのことを伝えて ``True``。"""
    if app.pause.path:
        return False
    print("休止の記録の場所（設定の state.pause_file）が空なので、放送を止められません。",
          file=sys.stderr)
    return True


def run_pause(app: ChimeApp, args: argparse.Namespace) -> int:
    """``--pause-today`` / ``--pause-until`` / ``--pause-off``。成功は 0、引数や日付の誤りは 2、記録の失敗は 1。"""
    if pause_unavailable(app):
        return 2
    if args.pause_off:
        return pause_off(app)
    last_day = app.now().date() if args.pause_today else parse_day(args.pause_until or "")
    if last_day is None:  # 引数は run() が確かめている。dispatch を直接呼ばれたときの備え
        print("--pause-until は YYYY-MM-DD の形で指定してください。", file=sys.stderr)
        return 2
    return pause_until(app, last_day, args.reason or "")


def pause_period_error(today: date, last_day: date, reason: str) -> Optional[str]:
    """止める期間と理由の誤り（過去の日付・長すぎる・文字コードの壊れた理由）。無ければ ``None``。

    ``PauseStore.set`` も同じことを確かめる。ここで先に確かめるのは、``--dry-run``（``set`` を
    呼ばない）でも同じ誤りを断るため、また長すぎるときは休館日の設定を案内するため。
    """
    days = (last_day - today).days + 1
    if days < 1:
        return "終了日 {0} は過去の日付です（今日は {1}）。今日以降の日付を指定してください".format(
            last_day.isoformat(), today.isoformat())
    if days > MAX_PAUSE_DAYS:
        return ("止められるのは今日を含めて最長 {0} 日までです（{1} まででは {2} 日になります）。"
                "それより長く休むときは、設定の schedule.closures に休館日として書いてください"
                "（config.example.json の例を参照）。".format(MAX_PAUSE_DAYS, last_day.isoformat(), days))
    try:
        reason.encode("utf-8")
    except UnicodeEncodeError:
        return "理由に文字コードの正しくない文字が含まれています。別の言葉で指定し直してください"
    return None


def pause_until(app: ChimeApp, last_day: date, reason: str) -> int:
    """今日から ``last_day`` まで放送を止める記録を書き、何を止めて、いつ再開するかを表示する。"""
    today = app.now().date()
    problem = pause_period_error(today, last_day, reason)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2
    hourly, closing = count_broadcasts(app, today, last_day)
    summary = "{0} の放送（時報 {1} 回・閉館 {2} 回）を止めます。{3}".format(
        check.pause_span(today, last_day), hourly, closing, resume_text(app, last_day))
    if app.dry_run:
        print("--dry-run のため、休止の記録は書きません。書くと次のようになります。")
    else:
        try:
            app.pause.set(last_day, reason, now=app.now())
        except ValueError as exc:  # 上で確かめたはずの誤り（別の経路で決まる条件の備え）
            print(exc, file=sys.stderr)
            return 2
        except OSError as exc:
            print("休止の記録を書けませんでした: {0}".format(exc), file=sys.stderr)
            print(pause_io_next_step(app), file=sys.stderr)
            return 1
    print(summary)
    if reason:
        print("理由: {0}".format(reason))
    print("常駐中のサービスにはそのまま効きます（再起動は要りません）。"
          "早く再開するには --pause-off を実行します。")
    return 0


def pause_io_next_step(app: ChimeApp) -> str:
    """休止の記録を書けない・消せないときの、次の手がかり（利用者向けの 1 行）。

    記録の場所がフォルダになっているときは、``--pause-off`` でも消せないので、手で移すか
    消すよう言う（``--check`` の「書き込み」の節と同じ案内）。それ以外は ``--check`` を案内する。
    """
    path = app.pause.path
    if os.path.isdir(path):
        return ("休止の記録の場所 {0} がフォルダです（ファイルでないと書けず、--pause-off でも消せません）。"
                "そのフォルダを移すか消して（中身を確かめてから）、もう一度実行してください。".format(path))
    return CHECK_HINT


def catch_up_events(app: ChimeApp) -> List[Event]:
    """休止を解いたとき、サービスが追いかけて鳴らしうる回（再生開始から猶予の秒数以内で、まだ鳴らしていないもの）。

    ``Scheduler.next_event`` の取りこぼしの拾い上げと同じ範囲を、ログを出さずに調べる。
    再生開始の時刻順。サービスが解除に気づくのが遅れて間に合わない回も含むので、確実に鳴る回は
    :func:`split_catch_up` で分ける。
    """
    now = app.now()
    horizon = now - timedelta(seconds=app.scheduler.grace)
    found = []
    day = min(horizon.date(), now.date())
    while day <= now.date():
        found.extend(event for event in app.scheduler.events_for_date(day)
                     if horizon <= event.play_at < now and not app.state.is_fired(event.key, event.day))
        day += timedelta(days=1)
    return found


def split_catch_up(app: ChimeApp, events: List[Event]) -> Tuple[List[Event], List[Event]]:
    """追いかけて鳴らす回を、``(確実な回, 間に合えば鳴る回)`` に分ける（順番は変えない）。

    常駐中のサービスが休止の解除に気づくのは、待機の区切り（``max_sleep`` 秒）の終わりで、解除から
    最大でその秒数あと。その間に猶予を過ぎる回があるので、再生開始からの経過が ``猶予 - max_sleep`` 秒に
    満たない回だけが確実。それ以降の回は、サービスが気づくのが間に合えば鳴る（待機の区切りの
    位置しだいで、鳴らないこともある）。
    """
    now = app.now()
    certain_within = app.scheduler.grace - app.scheduler.max_sleep
    certain, maybe = [], []
    for event in events:
        (certain if (now - event.play_at).total_seconds() < certain_within else maybe).append(event)
    return certain, maybe


def print_catch_up_group(heading: str, events: List[Event]) -> None:
    """見出しと、追いかけて鳴らす回（最大 :data:`CATCH_UP_LIMIT` 件。残りは件数だけ）を表示する。"""
    print(heading)
    print(format_events(events[:CATCH_UP_LIMIT]))
    if len(events) > CATCH_UP_LIMIT:
        print("  ほか {0} 件".format(len(events) - CATCH_UP_LIMIT))


def print_catch_up(app: ChimeApp) -> None:
    """休止を解いたあと、追いかけて鳴る回があれば知らせる（無ければ何も出さない）。

    止めている最中に再生開始の時刻が過ぎた回も、猶予のうちは、休止が解けたサービスが鳴らす
    （止めた回は再生済みにしないため）。「次の放送」だけを言うと、この回が抜けて驚かれる。
    ただし、サービスが解除に気づくまでには最大 ``max_sleep`` 秒かかるので、猶予の終わりに近い回
    （:func:`split_catch_up`）は、鳴ると約束せず「間に合えば」と言う。
    """
    certain, maybe = split_catch_up(app, catch_up_events(app))
    if certain:
        print_catch_up_group("開始から {0:g} 秒未満の回は、サービスが追いかけて鳴らします:".format(
            app.scheduler.grace - app.scheduler.max_sleep), certain)
    if maybe:
        print_catch_up_group("間に合えば追いかけて鳴ります（サービスが解除に気づくまで最大 {0:g} 秒かかり、"
                             "開始から {1:g} 秒を過ぎると鳴りません）:".format(
                                 app.scheduler.max_sleep, app.scheduler.grace), maybe)


def pause_off(app: ChimeApp) -> int:
    """休止の記録を消して、止めていた放送を再開する。何を再開したか（無ければ、止めていないこと）を表示する。"""
    pause = app.pause.current()
    if app.dry_run:
        print("--dry-run のため、休止の記録は消しません。")
        print("止めています: {0}".format(check.describe_pause(pause)) if pause
              else "止めている放送はありません。")
        return 0
    try:
        removed = app.pause.clear()
    except OSError as exc:
        print("休止の記録を消せませんでした: {0}".format(exc), file=sys.stderr)
        print(pause_io_next_step(app), file=sys.stderr)
        return 1
    if pause is not None:
        print("休止を解除しました: {0}".format(check.describe_pause(pause)))
        print_catch_up(app)
        upcoming = app.scheduler.upcoming(limit=1)
        if upcoming:
            print("次の放送は {0} です。".format(upcoming[0].at.strftime("%m/%d %H:%M")))
    elif removed:
        print("期限が切れていた（または読めなかった）休止の記録を消しました。止めている放送はありません。")
    else:
        print("止めている放送はありません。")
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
