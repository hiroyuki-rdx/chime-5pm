"""スケジューリング。

「次に鳴らすべきイベント」を計算し、その時刻まで待つ。

イベントは 2 種類。

``hourly``
    毎正時の時報（既定は平日 10:00〜16:00）。``at`` は正時ちょうど、
    ``play_at`` は「ポーン」が正時に鳴るよう ``pip_lead`` 秒だけ手前。
``closing``
    閉館アナウンス＋蛍の光（既定は平日 16:57）。``play_at`` は ``at`` と同じ。

休館日（``schedule.closures``。``chime.closures``）に当たる日は、どちらも鳴らさない。

1 秒ごとに現在時刻を比較する方式ではなく、次のイベント時刻を求めて
そこまで sleep する方式とした（NFR-02: 待機中の CPU 使用率）。
NTP による時刻補正に追従できるよう、sleep は最大 ``max_sleep`` 秒ずつに
分割して都度実時刻を読み直す。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterator, List, Mapping, Optional, Sequence

from .closures import Closure, ClosureCalendar

logger = logging.getLogger(__name__)

#: イベントを探索する最大日数。土日・祝日の連続だけでなく、年末年始や長期休館
#: （``schedule.closures``）をまたいでも、休み明けの最初の予定を見つけられる長さ。
MAX_LOOKAHEAD_DAYS = 400

#: ``date.weekday()`` の値（月曜が 0）に対応する曜日の名前。
WEEKDAY_NAMES = ("月", "火", "水", "木", "金", "土", "日")


def hourly_hours(hourly: Mapping[str, Any]) -> range:
    """時報の ``start_hour``〜``end_hour``（両端を含む）を返す。

    ``skip_hours``・曜日・0〜23 の範囲外は取り除かない。作り置きの文言を
    数える側（``chime.phrases``）は、休みにしている時刻の文言も用意しておく
    ため。鳴らす時刻を決める ``Scheduler`` は、これに自分で絞り込みをかける。
    """
    return range(int(hourly.get("start_hour", 10)), int(hourly.get("end_hour", 16)) + 1)


@dataclass(frozen=True)
class Event:
    """1 回分の再生イベント。"""

    key: str
    kind: str
    hour: int
    at: datetime
    play_at: datetime
    prepare_at: datetime

    @property
    def day(self) -> str:
        """再生日（YYYY-MM-DD）。二重再生防止のキーに使う。"""
        return self.at.date().isoformat()

    def describe(self) -> str:
        label = "時報" if self.kind == "hourly" else "閉館放送"
        return "{0} {1}（再生開始 {2}）".format(
            label,
            self.at.strftime("%Y-%m-%d %H:%M:%S"),
            self.play_at.strftime("%H:%M:%S"),
        )


class Scheduler:
    """設定からイベント列を生成し、待機を行う。"""

    def __init__(self, settings: Mapping[str, Any], tzinfo, pip_lead_seconds: float,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self.settings = dict(settings)
        self.tzinfo = tzinfo
        configured_lead = self.settings.get("pip_lead_seconds")
        self.pip_lead = float(configured_lead) if configured_lead is not None else float(pip_lead_seconds)
        self.prepare_lead = float(self.settings.get("prepare_lead_seconds", 45.0))
        self.grace = float(self.settings.get("catchup_grace_seconds", 120.0))
        self.max_sleep = float(self.settings.get("max_sleep_seconds", 30.0))
        # 書き方の正しくない休館日の項目は、その項目だけ無視する（鳴らす側に倒す）。
        self.closures = ClosureCalendar(self.settings.get("closures"))
        self._clock = clock or (lambda: datetime.now(self.tzinfo))

    # ------------------------------------------------------------------
    def now(self) -> datetime:
        return self._clock()

    def _make_event(self, kind: str, key: str, moment: datetime, lead: float) -> Event:
        play_at = moment - timedelta(seconds=lead)
        return Event(
            key=key,
            kind=kind,
            hour=moment.hour,
            at=moment,
            play_at=play_at,
            prepare_at=play_at - timedelta(seconds=self.prepare_lead),
        )

    @staticmethod
    def _runs_on(section: Mapping[str, Any], day: date) -> bool:
        """設定の節（時報・閉館放送）が有効で、その日の曜日に稼働するか。

        無効な節の ``weekdays`` は読まない。無効にした節に壊れた値が残っていても
        例外にしないため（``enabled`` を先に見て、``and`` で打ち切る）。
        """
        return bool(section.get("enabled", True)
                    and day.weekday() in set(section.get("weekdays", [])))

    def _hourly_events(self, day: date) -> List[Event]:
        """指定日の時報。``skip_hours`` と 0〜23 の範囲外の時刻は鳴らさない。"""
        hourly = self.settings.get("hourly", {}) or {}
        if not self._runs_on(hourly, day):
            return []

        minute = int(hourly.get("minute", 0))
        hours = hourly_hours(hourly)
        skip = {int(h) for h in hourly.get("skip_hours", []) or []}
        events: List[Event] = []
        for hour in hours:
            if hour in skip or not 0 <= hour <= 23:
                continue
            moment = datetime(day.year, day.month, day.day, hour, minute,
                              tzinfo=self.tzinfo)
            events.append(self._make_event("hourly", "hourly:{0:02d}".format(hour),
                                           moment, self.pip_lead))
        return events

    def _closing_events(self, day: date) -> List[Event]:
        """指定日の閉館放送（先行なし: 再生開始は ``at`` と同じ）。"""
        closing = self.settings.get("closing", {}) or {}
        if not self._runs_on(closing, day):
            return []

        moment = datetime(day.year, day.month, day.day,
                          int(closing.get("hour", 16)), int(closing.get("minute", 57)),
                          tzinfo=self.tzinfo)
        return [self._make_event("closing", "closing", moment, 0.0)]

    def _rule_events(self, day: date) -> List[Event]:
        """曜日・有効／無効の決まりだけから作った、指定日のイベント（休館日は見ない）。"""
        events = self._hourly_events(day) + self._closing_events(day)
        events.sort(key=lambda event: event.play_at)
        return events

    def events_for_date(self, day: date) -> List[Event]:
        """指定日のイベントを再生開始時刻順に返す。休館日は空。"""
        if self.closures.closed_reason(day) is not None:
            return []
        return self._rule_events(day)

    def skip_closure(self, day: date) -> Optional[Closure]:
        """休館のために鳴らさない日なら、その休館日の項目を返す。

        曜日や有効／無効の決まりでもともと鳴らない日（土日など）は、休館日と
        重なっていても ``None``。「休館がなければ鳴っていたはずの日」だけを答える。
        """
        closure = self.closures.closure_on(day)
        if closure is None or not self._rule_events(day):
            return None
        return closure

    def skip_reason(self, day: date) -> Optional[str]:
        """休館のために鳴らさない日なら、その理由（``"休館: 学園祭"``）を返す。

        答える日は :meth:`skip_closure` と同じ（休館がなければ鳴っていたはずの日だけ）。
        """
        closure = self.skip_closure(day)
        return None if closure is None else closure.reason

    def iter_events(self, start: datetime, days: int = MAX_LOOKAHEAD_DAYS) -> Iterator[Event]:
        """``start`` 以降のイベントを時系列で列挙する。"""
        for offset in range(days):
            day = (start + timedelta(days=offset)).date()
            for event in self.events_for_date(day):
                if event.play_at >= start:
                    yield event

    def upcoming(self, now: Optional[datetime] = None, limit: int = 10) -> List[Event]:
        """次に来るイベントを ``limit`` 件返す（一覧表示用）。"""
        now = now or self.now()
        result: List[Event] = []
        for event in self.iter_events(now):
            result.append(event)
            if len(result) >= limit:
                break
        return result

    def next_event(self, now: Optional[datetime] = None,
                   is_fired: Optional[Callable[[Event], bool]] = None) -> Optional[Event]:
        """次に処理すべきイベントを返す。

        再起動などで少し出遅れた場合に備え、``catchup_grace_seconds`` 以内の
        取りこぼしは「今すぐ再生する」イベントとして返す。
        """
        now = now or self.now()
        is_fired = is_fired or (lambda event: False)

        # 取りこぼしの拾い上げ（過去 grace 秒以内）。
        # 走査する日付は horizon から直接決める。catchup_grace_seconds が
        # 1 日を超える設定でも取りこぼさず、grace が 0 以下なら今日だけを見る。
        horizon = now - timedelta(seconds=self.grace)
        day = min(horizon.date(), now.date())
        while day <= now.date():
            for event in self.events_for_date(day):
                if horizon <= event.play_at < now and not is_fired(event):
                    logger.warning("再生開始時刻を %.1f 秒過ぎています（追いかけ再生）: %s",
                                   (now - event.play_at).total_seconds(), event.describe())
                    return event
            day += timedelta(days=1)

        for event in self.iter_events(now):
            if not is_fired(event):
                return event
        return None

    # -- 待機 -----------------------------------------------------------
    def sleep_until(self, target: datetime, stop: Optional[threading.Event] = None,
                    precise: bool = False,
                    interrupt: Optional[Callable[[], bool]] = None) -> bool:
        """``target`` まで待つ。停止要求・割り込みで打ち切った場合は ``False`` を返す。

        ``interrupt`` は「今日は止める」のような、待つ途中で放送を取りやめる合図の
        確認。1 回分の待機（``max_sleep`` 秒以内）が終わるたびに呼び、``True`` を
        返したら待つのをやめる。正時直前の最終調整（残り 0.25 秒以内）では呼ばない
        （ポーンを遅らせない）。確認で例外が出ても待機は続ける（鳴らす側に倒す）。
        """
        interrupt_failed = False
        while True:
            if stop is not None and stop.is_set():
                return False
            remaining = (target - self.now()).total_seconds()
            if interrupt is not None and not (precise and 0 < remaining <= 0.25):
                try:
                    if interrupt():
                        return False
                except Exception:
                    if not interrupt_failed:
                        logger.exception("待機中の割り込みの確認に失敗しました（待機は続けます）")
                        interrupt_failed = True
            if remaining <= 0:
                return True
            if precise and remaining <= 0.25:
                # 正時ちょうどに鳴らすための最終調整。
                # この間も停止要求を見逃さないよう、都度確認する。
                deadline = time.monotonic() + remaining
                while time.monotonic() < deadline:
                    if stop is not None and stop.is_set():
                        return False
                    time.sleep(0.002)
                return True
            chunk = min(remaining, self.max_sleep)
            if precise:
                chunk = min(chunk, max(0.05, remaining - 0.2))
            if stop is not None:
                if stop.wait(chunk):
                    return False
            else:
                time.sleep(chunk)


def format_events(events: Sequence[Event]) -> str:
    """イベント一覧を人が読める文字列に整形する。"""
    if not events:
        return "（予定されたイベントはありません）"
    return "\n".join("  - " + event.describe() for event in events)


def _closed_day_line(day: date, closure: Closure) -> str:
    """休館で鳴らさない日の 1 行（例: ``  休館 2026-12-28（月） 年末年始``。名前なしは名前を省く）。

    行頭は「休館」。現地の操作で一時的に止める「休止」（``--pause-today`` など）とは別物なので、
    言葉を分けている。
    """
    line = "  休館 {0}（{1}）".format(day.isoformat(), WEEKDAY_NAMES[day.weekday()])
    return "{0} {1}".format(line, closure.name) if closure.name else line


def format_schedule(scheduler: Scheduler, start: Optional[datetime] = None,
                    limit: int = 10) -> str:
    """``start`` 以降の予定 ``limit`` 件を、休館で鳴らさない日の行を挟んで整形する。

    ``format_events(scheduler.upcoming(start, limit))`` と同じ並びに、一覧が覆う
    期間（``start`` の日〜最後の予定の日。予定が ``limit`` 件に満たないときは探索の
    果てまで）の「休館がなければ鳴っていたはずの日」を 1 日 1 行で時系列に入れる。
    休館日のない設定では ``format_events`` と同じ文字列になる。
    """
    start = start or scheduler.now()
    events = scheduler.upcoming(start, limit)
    if not scheduler.closures.closures:
        return format_events(events)

    first_day = start.date()
    if events and len(events) >= limit:
        last_day = max(events[-1].at.date(), events[-1].play_at.date())
    else:
        last_day = first_day + timedelta(days=MAX_LOOKAHEAD_DAYS - 1)

    # (日付, 休館の行なら 0・予定なら 1) で安定ソートすると、同じ日の中の並びは変わらない。
    rows = [(event.play_at.date(), 1, format_events([event])) for event in events]
    day = first_day
    while day <= last_day:
        closure = scheduler.skip_closure(day)
        if closure is not None:
            rows.append((day, 0, _closed_day_line(day, closure)))
        day += timedelta(days=1)
    rows.sort(key=lambda row: (row[0], row[1]))

    lines = [line for _, _, line in rows]
    if not events:
        lines.append(format_events([]))
    return "\n".join(lines)
