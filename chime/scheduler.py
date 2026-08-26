"""スケジューリング。

「次に鳴らすべきイベント」を計算し、その時刻まで待つ。

イベントは 2 種類。

``hourly``
    毎正時の時報（既定は平日 10:00〜16:00）。``at`` は正時ちょうど、
    ``play_at`` は「ポーン」が正時に鳴るよう ``pip_lead`` 秒だけ手前。
``closing``
    閉館アナウンス＋蛍の光（既定は平日 16:57）。``play_at`` は ``at`` と同じ。

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

logger = logging.getLogger(__name__)

#: イベントを探索する最大日数（土日・祝日の連続を十分にまたげる長さ）。
MAX_LOOKAHEAD_DAYS = 30


@dataclass(frozen=True)
class Event:
    """1 回分の再生イベント。"""

    key: str
    kind: str
    hour: int
    minute: int
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
            minute=moment.minute,
            at=moment,
            play_at=play_at,
            prepare_at=play_at - timedelta(seconds=self.prepare_lead),
        )

    def events_for_date(self, day: date) -> List[Event]:
        """指定日のイベントを再生開始時刻順に返す。"""
        events: List[Event] = []

        hourly = self.settings.get("hourly", {}) or {}
        if hourly.get("enabled", True) and day.weekday() in set(hourly.get("weekdays", [])):
            minute = int(hourly.get("minute", 0))
            start = int(hourly.get("start_hour", 10))
            end = int(hourly.get("end_hour", 16))
            skip = {int(h) for h in hourly.get("skip_hours", []) or []}
            for hour in range(start, end + 1):
                if hour in skip or not 0 <= hour <= 23:
                    continue
                moment = datetime(day.year, day.month, day.day, hour, minute,
                                  tzinfo=self.tzinfo)
                events.append(self._make_event("hourly", "hourly:{0:02d}".format(hour),
                                               moment, self.pip_lead))

        closing = self.settings.get("closing", {}) or {}
        if closing.get("enabled", True) and day.weekday() in set(closing.get("weekdays", [])):
            moment = datetime(day.year, day.month, day.day,
                              int(closing.get("hour", 16)), int(closing.get("minute", 57)),
                              tzinfo=self.tzinfo)
            events.append(self._make_event("closing", "closing", moment, 0.0))

        events.sort(key=lambda event: event.play_at)
        return events

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

        # 取りこぼしの拾い上げ（過去 grace 秒以内）
        horizon = now - timedelta(seconds=self.grace)
        for offset in (-1, 0):
            day = (now + timedelta(days=offset)).date()
            for event in self.events_for_date(day):
                if horizon <= event.play_at < now and not is_fired(event):
                    logger.warning("再生開始時刻を %.1f 秒過ぎています（追いかけ再生）: %s",
                                   (now - event.play_at).total_seconds(), event.describe())
                    return event

        for event in self.iter_events(now):
            if not is_fired(event):
                return event
        return None

    # -- 待機 -----------------------------------------------------------
    def sleep_until(self, target: datetime, stop: Optional[threading.Event] = None,
                    precise: bool = False) -> bool:
        """``target`` まで待つ。停止要求で打ち切った場合は ``False`` を返す。"""
        while True:
            if stop is not None and stop.is_set():
                return False
            remaining = (target - self.now()).total_seconds()
            if remaining <= 0:
                return True
            if precise and remaining <= 0.25:
                # 正時ちょうどに鳴らすための最終調整
                deadline = time.monotonic() + remaining
                while time.monotonic() < deadline:
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
