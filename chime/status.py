"""いまの状態の表示（``--status``）と、放送の最中の待機（``--wait-idle``）。

``--status``
    現地で「いま、ちゃんと動いているか」を一目で見る。版・時刻・時刻の同期
    （NTP）・サービスの状態・再生方法・直近の放送・次の予定・作り置きの声。
    何も書かない。外のコマンド（``timedatectl`` / ``systemctl``）は、呼び出し側から
    差し替えられる（テストでは本物を呼ばない）。調べられないものは「確認できません」
    と表示し、問題とは数えない。気になる点があれば終了コード 1。

``--wait-idle``
    更新（``scripts/update.sh``）のとき、放送の最中にサービスを再起動して
    放送を途切れさせないよう、放送の時間帯が終わるまで待つ。
"""

from __future__ import annotations

import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, TextIO, Tuple

from . import buildinfo, env
from .check import display_width, pad, prerecorded_coverage
from .history import History
from .phrases import Coverage, CoverageTooLarge, coverage
from .scheduler import Event, Scheduler, format_events
from .tts import TTSService

if TYPE_CHECKING:  # 型の注釈にだけ使う（実行時に pygame まで引き込まない）
    from .app import ChimeApp

SERVICE_NAME = "campus_chime.service"

#: 外のコマンドを待つ上限（秒）。応答しない systemd に、状態の表示が巻き込まれないように。
COMMAND_TIMEOUT = 3

#: 時刻が NTP と同期済みかを尋ねるコマンド（出力は ``yes`` か ``no``）。
NTP_COMMAND = ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]

#: 直近の放送を挙げる件数と、次の予定を挙げる件数。
HISTORY_LIMIT = 8
UPCOMING_LIMIT = 3

#: 無音だった文言・欠けた音源を 1 件の履歴につき挙げる数。
SILENT_LIMIT = 3
MISSING_LIMIT = 3

#: 放送の内容を組み立てられず、最小のプラン（時報音／閉館アナウンスと蛍の光だけ）で鳴らした
#: 履歴の補足。結果は「成功」のままなので、この補足で気づけるようにする。
DEGRADED_NOTE = "簡易の内容で放送"

UNKNOWN = "確認できません"

_KIND_LABELS = {"hourly": "時報", "closing": "閉館放送"}
_RESULT_LABELS = {"ok": "成功", "partial": "一部のみ", "failed": "失敗", "error": "エラー"}
#: 履歴の結果のうち、「放送が失敗した」と数えるもの。
_FAILED_RESULTS = ("failed", "error")

_ACTIVE_LABELS = {"active": "動作中", "inactive": "止まっています", "failed": "失敗しました",
                  "activating": "起動中（または再起動の待ち）"}
_ENABLED_LABELS = {"enabled": "有効（再起動後も自動で始まります）",
                   "disabled": "無効（再起動すると始まりません）",
                   "not-found": "未登録（bash scripts/setup.sh で登録します）"}
_NEEDS_CHECK = "  ← 要確認"

Runner = Callable[..., Any]


# ---------------------------------------------------------------------------
# 外のコマンド
# ---------------------------------------------------------------------------
def query_command(command: List[str], run: Optional[Runner] = None) -> Optional[str]:
    """コマンドの標準出力の先頭行を返す。実行できない・時間切れ・出力が空なら ``None``。

    終了コードは見ない（``systemctl is-active`` は止まっていると 0 以外を返すが、
    状態は標準出力に出す）。``run`` は ``subprocess.run`` の代わり（省略時は本物）。
    """
    runner = run or subprocess.run
    try:
        result = runner(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
    except (OSError, subprocess.SubprocessError, ValueError):
        # ValueError: 出力を文字に直せなかった（UnicodeDecodeError）。
        return None
    lines = (getattr(result, "stdout", "") or "").strip().splitlines()
    return lines[0].strip() if lines else None


def ntp_synchronized(run: Optional[Runner] = None) -> Optional[bool]:
    """時刻が NTP と同期済みか。調べられなければ ``None``。"""
    answer = query_command(NTP_COMMAND, run)
    return {"yes": True, "no": False}.get(answer or "")


def service_state(run: Optional[Runner] = None) -> Tuple[Optional[str], Optional[str]]:
    """サービスの ``(動作の状態, 自動起動の状態)``（``active`` / ``enabled`` など）。

    ``systemctl`` が無い・応答しないときは、その項目が ``None``。
    """
    return (query_command(["systemctl", "is-active", SERVICE_NAME], run),
            query_command(["systemctl", "is-enabled", SERVICE_NAME], run))


# ---------------------------------------------------------------------------
# 状態の集め方
# ---------------------------------------------------------------------------
@dataclass
class Status:
    """表示する状態の全部（集めた結果。表示と判定の元）。"""

    version: str
    now: datetime
    ntp: Optional[bool]
    active: Optional[str]
    enabled: Optional[str]
    backend: str
    #: 実機の Linux か（mock が問題になる環境か）。
    production: bool
    history: List[Dict[str, Any]]
    upcoming: List[Event]
    #: 作り置きの集計。数えられなかったときは ``None``（理由は ``coverage_error``）。
    coverage: Optional[Coverage]
    coverage_error: str = ""
    #: 履歴の時刻を表示するタイムゾーン（``None`` なら記録のまま）。
    zone: Optional[tzinfo] = None
    #: 放送が作り置きの声を引くか（``tts.engines`` に ``prerecorded`` があるか）。無ければ、
    #: 声がディスクにそろっていても、放送は作り置きを引かない（Pi では読み上げがすべて無音）。
    prerecorded_enabled: bool = True

    @property
    def last_failed(self) -> bool:
        """直近の放送が失敗（1 つも鳴らせない、または例外）で終わったか。"""
        return bool(self.history) and self.history[0].get("result") in _FAILED_RESULTS

    @property
    def last_missing(self) -> bool:
        """直近の放送で、音源ファイルが無くて積めなかった必須の部品があったか。

        ``missing`` の無い古い行は、無かったものとして扱う。
        """
        if not self.history:
            return False
        missing = self.history[0].get("missing")
        return isinstance(missing, list) and bool(missing)

    @property
    def last_degraded(self) -> bool:
        """直近の放送が、内容を組み立てられず簡易の内容（最小のプラン）で鳴ったか。

        ``degraded`` の無い古い行は、無かったものとして扱う（真偽値の ``true`` だけを数える）。
        """
        return bool(self.history) and self.history[0].get("degraded") is True


def collect_status(app: ChimeApp, run: Optional[Runner] = None,
                   production: Optional[bool] = None,
                   version: Optional[str] = None) -> Status:
    """``app`` と外のコマンドから、表示する状態を集める。何も書かない。

    再生バックエンドの選択（ログを出す）はここで済ませる。表示の途中に
    ログが混ざらないようにするため。
    """
    backend = app.player.name
    found, error = _coverage_or_error(app)
    active, enabled = service_state(run)
    return Status(
        version=version if version is not None else buildinfo.version_string(),
        now=app.now(),
        ntp=ntp_synchronized(run),
        active=active,
        enabled=enabled,
        backend=backend,
        production=env.is_production_linux() if production is None else production,
        history=History(app.config.path("state.history_file")).recent(HISTORY_LIMIT),
        upcoming=app.scheduler.upcoming(limit=UPCOMING_LIMIT),
        coverage=found,
        coverage_error=error,
        zone=app.tzinfo,
        prerecorded_enabled=_prerecorded_enabled(app),
    )


def _runtime_tts(app: ChimeApp) -> TTSService:
    """放送が使う読み上げ（通信も書き込みもしない）。

    ``ChimeApp`` が持つものをそのまま使う（作り直すと、「未知のエンジン」の警告がもう一度
    ログに出る）。持たない代用の app には、設定から ``ChimeApp`` と同じ作り方で組み立てる。
    """
    tts = getattr(app, "tts", None)
    if tts is not None:
        return tts
    config = app.config
    return TTSService(config.section("tts"), config.path("tts.cache_dir"),
                      config.path("tts.prerecorded_dir"))


def _prerecorded_enabled(app: ChimeApp) -> bool:
    """放送が作り置きの声を引くか（``TTSService.prerecorded_lookup`` が引ける状態か）。

    組み立てられない設定（``tts.engines`` が反復できないなど）は ``ChimeApp`` が起動で
    例外にするので、ここでは「引く」として数える（別の理由を重ねない）。
    """
    try:
        return _has_prerecorded(_runtime_tts(app))
    except Exception:
        return True


def _has_prerecorded(tts: TTSService) -> bool:
    return any(engine.name == "prerecorded" for engine in tts.engines)


def _coverage_or_error(app: ChimeApp) -> Tuple[Optional[Coverage], str]:
    """作り置きの集計。数えられなければ ``(None, 理由)``。

    放送が実際に引く声を数える。``tts.engines`` に ``prerecorded`` があれば、ファイルの中身まで
    確かめる（``--check`` と同じ）。無ければ、``TTSService.prerecorded_lookup`` が何も引けない
    ので（起動のログが ``0/138`` と言うのと同じ）、すべて「声が無い」。
    """
    try:
        tts = _runtime_tts(app)
        if _has_prerecorded(tts):
            return prerecorded_coverage(app.config), ""
        return coverage(app.config, tts.prerecorded_lookup), ""
    except CoverageTooLarge as exc:  # 理由は日本語で述べてある。例外の型名は利用者に見せない
        return None, str(exc)
    except Exception as exc:  # 文言の数え上げは設定の値に左右される。表示は止めない
        return None, "{0}: {1}".format(type(exc).__name__, exc)


def attention(status: Status) -> List[str]:
    """気になる点（終了コード 1 の理由）。無ければ空のリスト。"""
    reasons = []
    if status.ntp is False:
        reasons.append("時刻が NTP と同期していません（放送の時刻がずれます）")
    if status.active is not None and status.active != "active":
        reasons.append("サービス {0} が動いていません（{1}）".format(SERVICE_NAME, status.active))
    if status.production and status.backend == "mock":
        reasons.append("再生方法が mock です（実機では音が出ません。audio.backend と音声まわりを確認）")
    if status.last_failed:
        reasons.append("直近の放送が失敗しています（下の「直近の放送」を参照）")
    elif status.last_missing:
        reasons.append("直近の放送で、音源ファイルが無くて鳴らせなかった部分があります"
                       "（下の「直近の放送」を参照。--check で確認）")
    elif status.last_degraded:
        reasons.append("直近の放送は、内容を組み立てられず簡易の内容で鳴りました"
                       "（時刻アナウンスやひとことが入っていません。下の「直近の放送」を参照。"
                       "原因は journalctl -u {0} で確認）".format(SERVICE_NAME))
    if not status.prerecorded_enabled:
        reasons.append("tts.engines に \"prerecorded\" が無いため、作り置きの声を使いません。"
                       "Pi では読み上げがすべて無音になります（--check で確認）")
    if status.coverage is None:
        reasons.append("作り置きの音声を数えられません（--check で理由を確認）")
    elif not status.coverage.ok and status.prerecorded_enabled:
        reasons.append("作り置きの音声が {0} 件足りません（--check で一覧を確認）".format(
            len(status.coverage.missing)))
    return reasons


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------
def render_status(status: Status, reasons: List[str]) -> List[str]:
    """状態を表示用の行にする。"""
    rows = _rows(status)
    width = max(display_width(label) for label, _ in rows)
    lines = [status.version, ""]
    lines.extend("{0}  {1}".format(pad(label, width), value) for label, value in rows)
    lines.extend(["", "直近の放送（新しい順）"])
    lines.extend(_history_lines(status))
    lines.extend(["", "次の予定", format_events(status.upcoming), ""])
    if reasons:
        lines.append("要確認:")
        lines.extend("  - {0}".format(reason) for reason in reasons)
    else:
        lines.append("気になる点は見つかりませんでした。")
    return lines


def _rows(status: Status) -> List[Tuple[str, str]]:
    """項目名と値の組（上から表示する順）。"""
    return [
        ("現在時刻", status.now.strftime("%Y-%m-%d %H:%M:%S %Z").strip()),
        ("時刻の同期", _ntp_text(status.ntp)),
        ("サービス", _active_text(status.active)),
        ("自動起動", _enabled_text(status.enabled)),
        ("再生方法", _backend_text(status)),
        ("作り置きの音声", _coverage_text(status)),
    ]


def _ntp_text(ntp: Optional[bool]) -> str:
    if ntp is None:
        return UNKNOWN
    return "同期済み（NTP）" if ntp else "同期していません" + _NEEDS_CHECK


def _labelled(state: str, labels: Mapping[str, str]) -> str:
    label = labels.get(state)
    return "{0}（{1}）".format(label, state) if label else state


def _active_text(state: Optional[str]) -> str:
    if state is None:
        return UNKNOWN
    return _labelled(state, _ACTIVE_LABELS) + ("" if state == "active" else _NEEDS_CHECK)


def _enabled_text(state: Optional[str]) -> str:
    return UNKNOWN if state is None else _labelled(state, _ENABLED_LABELS)


def _backend_text(status: Status) -> str:
    if status.backend != "mock":
        return status.backend
    if status.production:
        return "mock（音が出ません）" + _NEEDS_CHECK
    return "mock（開発環境なので音は出しません）"


def _coverage_text(status: Status) -> str:
    found = status.coverage
    if found is None:
        return "数えられません（{0}）".format(status.coverage_error) + _NEEDS_CHECK
    if found.ok:
        return "{0} 件すべてそろっています".format(found.total)
    text = "{0} 件中 {1} 件の声がありません".format(found.total, len(found.missing))
    if not status.prerecorded_enabled:
        text += "（tts.engines に prerecorded が無いため、作り置きを使いません）"
    return text + _NEEDS_CHECK


def _history_lines(status: Status) -> List[str]:
    if not status.history:
        return ["  （記録はまだありません）"]
    return ["  " + describe_entry(entry, status.zone) for entry in status.history]


def describe_entry(entry: Mapping[str, Any], tz: Optional[tzinfo] = None) -> str:
    """履歴 1 件を 1 行にする（時刻・種類・結果・簡易の内容で放送・欠けた音源・無音だった文言）。

    読めない項目があっても例外にしない（履歴は人が書き換えることもある）。
    辞書でないもの、時刻が範囲外のもの、型が違う項目、改行などの制御文字を含む
    文字列でも、1 行の文字列を返す。
    """
    if not isinstance(entry, Mapping):
        return "（読めない記録）"
    raw_kind = entry.get("kind", "?")
    kind = _KIND_LABELS.get(str(raw_kind), _plain(raw_kind))
    result = _plain(entry.get("result", "?"))
    label = _RESULT_LABELS.get(result, result)
    line = "{0}  {1}  {2}".format(_entry_time(entry, tz), pad(kind, 8), pad(label, 8)).rstrip()
    return line + _entry_notes(entry)


def _entry_time(entry: Mapping[str, Any], tz: Optional[tzinfo]) -> str:
    try:
        moment = datetime.fromisoformat(str(entry.get("at")))
        if tz is not None and moment.tzinfo is not None:
            moment = moment.astimezone(tz)
        return moment.strftime("%m/%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        # OverflowError: 変換すると年が範囲を外れる時刻（9999-12-31T23:59:59+00:00 など）。
        return "（時刻不明）"


def _plain(value: Any) -> str:
    """表示用の 1 行の文字列にする。改行などの制御文字や、端末に出せない文字は空白にする。"""
    try:
        text = str(value)
    except Exception:  # 文字列にできない値（JSON から来た値では起きないが、念のため）
        return "?"
    return "".join(" " if unicodedata.category(char) in ("Cc", "Cs") else char
                   for char in text)


def _listed_note(title: str, values: Any, limit: int) -> List[str]:
    """``title: 「a」「b」（ほか n 件）`` を 1 件だけ入れたリスト。空・リストでなければ空のリスト。"""
    if not isinstance(values, list) or not values:
        return []
    texts = "".join("「{0}」".format(_plain(value)) for value in values[:limit])
    more = "（ほか {0} 件）".format(len(values) - limit) if len(values) > limit else ""
    return ["{0}: {1}{2}".format(title, texts, more)]


def _entry_notes(entry: Mapping[str, Any]) -> str:
    """結果の補足（エラーの説明・簡易の内容で放送・欠けた音源・無音だった文言）。"""
    notes = []
    if entry.get("error"):
        notes.append("エラー: {0}".format(_plain(entry["error"])))
    if entry.get("degraded") is True:
        notes.append(DEGRADED_NOTE)
    notes.extend(_listed_note("欠けた音源", entry.get("missing"), MISSING_LIMIT))
    notes.extend(_listed_note("無音", entry.get("silent"), SILENT_LIMIT))
    return "  " + "  ".join(notes) if notes else ""


def run_status(app: ChimeApp, out: Optional[TextIO] = None, run: Optional[Runner] = None,
               production: Optional[bool] = None, version: Optional[str] = None) -> int:
    """いまの状態を表示し、気になる点があれば 1、なければ 0 を返す。何も書かない。

    ``out`` は出力先（省略時は標準出力）。``run`` は外のコマンドを走らせる関数
    （``subprocess.run`` の代わり）、``production`` は実機の Linux か、``version`` は
    版の表記。いずれも省略時は本物を使う。
    """
    stream = sys.stdout if out is None else out
    status = collect_status(app, run, production, version)
    reasons = attention(status)
    print("\n".join(render_status(status, reasons)), file=stream)
    return 1 if reasons else 0


# ---------------------------------------------------------------------------
# 放送の最中の待機
# ---------------------------------------------------------------------------
#: 待つ上限（秒）の既定と最大。閉館放送の時間帯（準備の開始から終わりまで約 355 秒）を
#: 1 回分まるごと待てる長さ。
WAIT_IDLE_MAX = 360
#: 待つときの 1 回の長さ（秒）。
WAIT_STEP = 5.0

#: 放送の時間帯は、準備を始める少し前から。
WINDOW_BEFORE_SECONDS = 10.0
#: 再生開始のあと、この秒数まで放送の時間帯（種類ごと。閉館放送は蛍の光が長い）。
WINDOW_AFTER_SECONDS = {"hourly": 90.0, "closing": 300.0}


def broadcast_window(event: Event) -> Tuple[datetime, datetime]:
    """イベントの放送の時間帯 ``(始まり, 終わり)``。この間はサービスを再起動しない。"""
    after = WINDOW_AFTER_SECONDS.get(event.kind, WINDOW_AFTER_SECONDS["hourly"])
    return (event.prepare_at - timedelta(seconds=WINDOW_BEFORE_SECONDS),
            event.play_at + timedelta(seconds=after))


def busy_event(scheduler: Scheduler, now: datetime) -> Optional[Tuple[Event, datetime]]:
    """``now`` が放送の時間帯の中なら ``(そのイベント, 時間帯の終わり)``、外なら ``None``。

    時間帯が重なるときは、終わりが一番遅いもの。日付をまたぐ時間帯のために、
    前日と翌日のイベントも見る。
    """
    found: Optional[Tuple[Event, datetime]] = None
    for offset in (-1, 0, 1):
        for event in scheduler.events_for_date(now.date() + timedelta(days=offset)):
            start, end = broadcast_window(event)
            if start <= now < end and (found is None or end > found[1]):
                found = (event, end)
    return found


def wait_idle(scheduler: Scheduler, limit: float = WAIT_IDLE_MAX,
              sleep: Optional[Callable[[float], None]] = None,
              out: Optional[TextIO] = None, err: Optional[TextIO] = None) -> int:
    """放送の時間帯が終わるまで待つ。終わった（または最初から外だった）ら 0、``limit`` 秒で終わらなければ 1。

    ``limit`` は 0〜:data:`WAIT_IDLE_MAX` 秒に丸める。:data:`WAIT_STEP` 秒ずつ眠って
    現在時刻を読み直す（NTP の補正で時刻が動いても、時間帯の判定に追従する）。
    時刻は ``scheduler.now()``、眠る関数は ``sleep``（省略時は ``time.sleep``）で
    差し替えられる。
    """
    stream = sys.stdout if out is None else out
    problems = sys.stderr if err is None else err
    nap = sleep or time.sleep
    limit = max(0.0, min(float(limit), float(WAIT_IDLE_MAX)))
    waited = 0.0
    while True:
        busy = busy_event(scheduler, scheduler.now())
        if busy is None:
            _report_idle(stream, waited)
            return 0
        if waited == 0.0:
            _report_busy(stream, scheduler.now(), busy, limit)
        if waited >= limit:
            print("{0:g} 秒待ちましたが、放送の時間帯が終わりませんでした: {1}".format(
                limit, busy[0].describe()), file=problems)
            return 1
        step = min(WAIT_STEP, limit - waited)
        nap(step)
        waited += step


def _report_busy(stream: TextIO, now: datetime, busy: Tuple[Event, datetime], limit: float) -> None:
    event, end = busy
    remaining = max(0, int((end - now).total_seconds()))
    print("放送の時間帯です: {0}".format(event.describe()), file=stream)
    print("  終わるまであと約 {0} 秒。最大 {1:g} 秒待ちます。".format(remaining, limit), file=stream)


def _report_idle(stream: TextIO, waited: float) -> None:
    if waited:
        print("放送の時間帯が終わりました（{0:g} 秒待ちました）。".format(waited), file=stream)
    else:
        print("いまは放送の時間帯ではありません。", file=stream)
