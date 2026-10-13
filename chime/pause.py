"""「今日だけ止める」の記録（``cache/pause.json``）。

行事や点検のあいだだけ、時報と閉館放送をまとめて止めるための記録。休館日の
設定（``schedule.closures``）とは別に、現地で ``--pause-today`` などから一時的に
足し、期限が来たら自然に効かなくなる。

記録は今日から ``last_day`` まで（両端を含む）で、長くても :data:`MAX_PAUSE_DAYS`
日まで。消し忘れて何週間も鳴らないままにならないよう、上限と期限を設けている。

放送の安全のため、ここは「失敗しても鳴らす側に倒す」。ファイルが無い・読めない・
壊れている・知らない版・期限切れのときは、どれも「止めていない」として扱い、
壊れているときだけ WARNING を 1 度残す。読む側（:meth:`PauseStore.current`・
:meth:`PauseStore.covers`・:meth:`PauseStore.changed`）は例外を出さない。

``set_at``（止めた日時）は、人に見せるための参考の項目。無い・読めない書き方のときは「不明」
（``None``）として扱い、そのせいで記録を使えなくはしない（手で書き直した記録の、止めるかどうかに
関わらない欄の間違いで、休止が効かなくなって鳴り出すことのないように）。

記録のファイルは、読む前に ``os.stat`` で種類と大きさを確かめる。普通のファイルでない
もの（名前付きパイプ・デバイス・フォルダ）と :data:`MAX_PAUSE_BYTES` を超えるものは、
開かずに「止めていない」として扱う（パイプを開くと書き手が現れるまで固まり、常駐ループ
ごと止まってしまう。巨大なファイルは、時報の直前の確認を遅らせる）。

どの日を止めるかは、イベントの **鳴る予定の日**（``Event.at`` の日付）で決める。ポーンを
先行させるので、0 時の時報は前日の 23:59:57 に鳴り始めるが、それは「次の日の放送」として
扱う（前日を止めても、翌日 0 時の時報は止めない。翌日を止めれば、その時報も止まる）。
この対応づけは :class:`Pause` ではなく、呼ぶ側（``chime.app``）が ``Pause.covers`` に
``event.at.date()`` を渡して決める。
"""

from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional, Tuple

from .jsonfile import JsonFileError, read_json, write_json_atomic

logger = logging.getLogger(__name__)

#: 記録の形式の版（形式を変えたら上げる。読むときは知らない版を無視する）。
PAUSE_VERSION = 1

#: 止められる最長の日数（開始日と終了日を含む）。
MAX_PAUSE_DAYS = 31

#: 日付の書き方（ASCII の数字だけ。``date.fromisoformat`` は版によって受け付ける形が違う）。
_DAY_PATTERN = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")

#: 問題の説明に添える、ファイルに書かれていた値の最大文字数。
_SHOW_LIMIT = 40

#: 記録のファイルの大きさの上限（バイト）。数十バイトの記録に対して十分に大きく、これを超える
#: ものは読まない（手で書き足した長い理由が入っても、100 KB 程度までは読める）。
MAX_PAUSE_BYTES = 1024 * 1024

#: ファイルの状態の目印（更新時刻・大きさ・inode）。ファイルが見えないときは ``None``。
_Signature = Optional[Tuple[int, int, int]]

#: まだ 1 度も :meth:`PauseStore.changed` を呼んでいない、という目印。
_NO_BASELINE: Any = object()


class _Invalid(ValueError):
    """記録の中身が使えない理由（日本語。``str()`` がそのまま説明になる）。"""


@dataclass(frozen=True)
class Pause:
    """止めている期間（``first_day``〜``last_day`` の両端を含む）。

    ``set_at`` は止めた日時（UTC からのずれ付き。記録に無い・読めない書き方のときは ``None`` =
    不明）、``reason`` は止めた理由（空でもよい）。
    """

    first_day: date
    last_day: date
    set_at: Optional[datetime]
    reason: str

    def covers(self, day: date) -> bool:
        return self.first_day <= day <= self.last_day


class PauseStore:
    """``cache/pause.json`` の読み書き。

    ``today`` は「今日」を返す関数（時計やタイムゾーンはここから切り離しておく）。
    読み込みは :meth:`current`・:meth:`covers`・:meth:`changed` のどれも例外を出さない。
    書き込み（:meth:`set`）と削除（:meth:`clear`）は、できなかったときに例外を
    出す。呼び出し側が利用者に伝えること。
    """

    def __init__(self, path: str, today: Callable[[], date]) -> None:
        self.path = path
        self._today = today
        self._reported: Optional[Tuple[_Signature, str]] = None
        self._baseline: _Signature = _NO_BASELINE

    # -- 読み出し ---------------------------------------------------------
    def current(self) -> Optional[Pause]:
        """今効いている記録を返す。無い・読めない・壊れている・知らない版・期限切れは ``None``。

        例外は出さない。壊れているときは WARNING を残す（同じ状態のあいだは
        1 度だけ。ファイルが書き換わるとまた 1 度残す）。
        """
        try:
            pause = self._read()
            if pause is not None:
                today = self._today()
                self._reported = None  # 読めた（期限が切れていてもよい）
                if pause.last_day >= today:
                    return pause
        except Exception as exc:  # 休止の記録のせいで放送を止めない
            self._report(None, "{0}: 想定外のエラーです: {1!r}".format(self.path, exc))
        return None

    def covers(self, day: date) -> bool:
        """``day`` が止めている期間に入っているか。"""
        pause = self.current()
        return pause is not None and pause.covers(day)

    def _read(self) -> Optional[Pause]:
        """ファイルを読んで記録にする（期限は見ない）。使えなければ ``None``。

        開く前に ``os.stat`` で、普通のファイルで :data:`MAX_PAUSE_BYTES` 以下かを確かめる。
        そうでなければ開かずに、1 度だけ報告して「止めていない」にする（名前付きパイプを
        開いて固まらないように）。
        """
        signature = _signature(self.path)
        problem = _unreadable_kind(self.path)
        if problem is not None:
            self._report(signature, "{0}: {1}".format(self.path, problem))
            return None
        try:
            data = read_json(self.path)
        except JsonFileError as exc:
            if exc.kind == "missing":
                self._reported = None
            else:
                self._report(signature, str(exc))
            return None
        except RecursionError:  # 入れ子が深すぎる JSON（JSON の読み込みは捕まえない）
            self._report(signature, "{0}: JSON の入れ子が深すぎて読めません".format(self.path))
            return None
        try:
            pause = _parse_pause(data)
        except _Invalid as exc:
            self._report(signature, "{0}: {1}".format(self.path, exc))
            return None
        return pause

    def _report(self, signature: _Signature, problem: str) -> None:
        """使えない記録を WARNING で残す（同じファイルの同じ問題は 1 度だけ）。"""
        key = (signature, problem)
        if key == self._reported:
            return
        self._reported = key
        logger.warning("休止の記録を使えません（放送は通常どおり行います）: %s", problem)

    # -- 書き込み ---------------------------------------------------------
    def set(self, last_day: date, reason: str = "",
            now: Optional[datetime] = None) -> Pause:
        """今日から ``last_day`` まで（両端を含む）止める記録を書いて返す。

        すでに記録があれば置き換える。``last_day`` が過去、または今日から数えて
        :data:`MAX_PAUSE_DAYS` 日を超えるときは、何も書かずに :class:`ValueError`
        （利用者向けの日本語の説明）。書けなかったときは :class:`OSError`。
        ``now`` は止めた日時で、省くと現在の時刻。UTC からのずれが無いものは
        この機械の時刻として扱い、秒未満は捨てる。
        """
        first_day = self._today()
        _check_period(first_day, last_day)
        _check_reason(reason)
        set_at = _aware_seconds(now)
        pause = Pause(first_day=first_day, last_day=last_day, set_at=set_at, reason=reason)
        write_json_atomic(self.path, {
            "v": PAUSE_VERSION,
            "first_day": pause.first_day.isoformat(),
            "last_day": pause.last_day.isoformat(),
            "set_at": set_at.isoformat(),
            "reason": pause.reason,
        })
        return pause

    def clear(self) -> bool:
        """記録のファイルを消す。消せたら ``True``、元から無ければ ``False``。

        壊れていた・期限が切れていたファイルも消す（``True``）。無いこと以外の
        理由で消せなかったときは :class:`OSError`。
        """
        try:
            os.remove(self.path)
        except FileNotFoundError:
            return False
        return True

    # -- 変化の検出 -------------------------------------------------------
    def changed(self) -> bool:
        """前に呼んだときからファイルが変わった（作られた・消えた・置き換わった）か。

        更新時刻・大きさ・inode・有無を比べる。最初の呼び出しは基準を覚えるだけで
        ``False``。状態を調べられなかった（``os.stat`` の失敗。場所に NUL 文字が混ざって
        いる、など）ときは、無いものとして扱う。例外は出さない。
        """
        signature = _signature(self.path)
        if self._baseline is _NO_BASELINE:
            self._baseline = signature
            return False
        changed = signature != self._baseline
        self._baseline = signature
        return changed


def _signature(path: str) -> _Signature:
    """ファイルの状態の目印（更新時刻・大きさ・inode）。無い・調べられないときは ``None``。

    ``os.stat`` は、場所に NUL 文字や変換できない文字が混ざっていると ``OSError`` ではなく
    ``ValueError``（``UnicodeEncodeError`` を含む）を出す。それも「調べられない」に含める。
    """
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def _unreadable_kind(path: str) -> Optional[str]:
    """開いてはいけないファイルなら、その理由（日本語）。開いてよい（または無い）なら ``None``。

    普通のファイルでないもの（名前付きパイプ・デバイス・フォルダ）と、大きすぎるもの。
    ``os.stat`` で調べるだけで、開かない。無い・権限がない場合は ``None`` を返し、開く側
    （``read_json``）の既存の報告に任せる。場所そのものが使えない（NUL 文字など）ときは、
    その旨を返す。
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    except ValueError:
        return "場所の書き方が正しくありません（使えない文字が含まれています）"
    if not stat.S_ISREG(info.st_mode):
        return "普通のファイルではないので読みません（フォルダ・名前付きパイプ・デバイスなど）"
    if info.st_size > MAX_PAUSE_BYTES:
        return "大きすぎるので読みません（{0} バイト。上限は {1} バイト）".format(
            info.st_size, MAX_PAUSE_BYTES)
    return None


# -- 書き込む前の確認（使えなければ ValueError） ------------------------------------
def _check_period(first_day: date, last_day: date) -> None:
    """``first_day``〜``last_day`` が、過去でなく、上限を超えない期間かを確かめる。"""
    if last_day < first_day:
        raise ValueError("終了日 {0} は過去の日付です（今日は {1}）。今日以降の日付を指定してください".format(
            last_day.isoformat(), first_day.isoformat()))
    days = (last_day - first_day).days + 1
    if days > MAX_PAUSE_DAYS:
        raise ValueError("止められるのは今日を含めて最長 {0} 日までです"
                         "（{1} まででは {2} 日になります）".format(
                             MAX_PAUSE_DAYS, last_day.isoformat(), days))


def _check_reason(reason: str) -> None:
    """理由が UTF-8 で書ける文字かを確かめる（引数の文字コードが壊れていると書けない）。"""
    try:
        reason.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("理由に文字コードの正しくない文字が含まれています。"
                         "別の言葉で指定し直してください") from None


def _aware_seconds(now: Optional[datetime]) -> datetime:
    """止めた日時を、UTC からのずれ付き・秒単位にする（``None`` なら現在の時刻）。"""
    moment = datetime.now() if now is None else now
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.astimezone()
    return moment.replace(microsecond=0)


# -- ファイルの中身の読み取り（使えなければ _Invalid） ------------------------------
def _show(value: Any) -> str:
    """ファイルに書かれていた値を、問題の説明に添える短い文字列にする。"""
    text = repr(value)
    return text if len(text) <= _SHOW_LIMIT else text[:_SHOW_LIMIT] + "…"


def _parse_pause(data: Any) -> Pause:
    """読み込んだ JSON を記録にする。使えなければ :class:`_Invalid`。"""
    if not isinstance(data, dict):
        raise _Invalid("最上位が {{ }} で囲んだ形ではありません（今は {0}）".format(_show(data)))
    version = data.get("v")
    # bool は int の子なので、``True == 1`` で通らないよう型も見る。
    if type(version) is not int or version != PAUSE_VERSION:
        raise _Invalid("知らない版です（v={0}。読めるのは v={1}）".format(
            _show(version), PAUSE_VERSION))

    first_day = _parse_day(data.get("first_day"), "first_day")
    last_day = _parse_day(data.get("last_day"), "last_day")
    if last_day < first_day:
        raise _Invalid("last_day（{0}）が first_day（{1}）より前です".format(
            last_day.isoformat(), first_day.isoformat()))
    days = (last_day - first_day).days + 1
    if days > MAX_PAUSE_DAYS:
        raise _Invalid("止める期間が {0} 日あります（長くても {1} 日までです）".format(
            days, MAX_PAUSE_DAYS))

    reason = data.get("reason", "")
    if not isinstance(reason, str):
        raise _Invalid("reason は文字で書きます（今は {0}）".format(_show(reason)))
    return Pause(first_day=first_day, last_day=last_day,
                 set_at=_parse_moment(data.get("set_at")), reason=reason)


def _parse_day(value: Any, label: str) -> date:
    """``"YYYY-MM-DD"`` の日付を読む。"""
    match = _DAY_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise _Invalid("{0} は \"2026-11-02\" の形で書きます（今は {1}）".format(label, _show(value)))
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        raise _Invalid("{0} はカレンダーにない日付です（{1}）".format(label, _show(value))) from None


def _parse_moment(value: Any) -> Optional[datetime]:
    """``set_at``（ISO 8601 の日時）を読む。読めなければ ``None``（不明）で、記録は使える。

    参考の項目なので、間違いで記録を捨てない。末尾の ``Z``（UTC）は、``datetime.fromisoformat``
    が受け付けない版（3.10 以前）があるので ``+00:00`` に直してから読む。ずれの無いものは
    この機械の時刻として扱う。
    """
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value[-1:] in ("Z", "z") else value
    try:
        moment = datetime.fromisoformat(text)
        if moment.utcoffset() is None:
            moment = moment.astimezone()
    except Exception:  # 書き方の間違い（ValueError）のほか、どんな理由でも、参考の項目で記録を捨てない
        return None
    return moment
