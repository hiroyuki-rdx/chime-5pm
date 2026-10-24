"""休館日（この日は時報も閉館放送も鳴らさない）。

設定の ``schedule.closures`` に書いた日付を読み、ある日が休館日かどうかを答える。
曜日の決まり（平日だけ鳴らす など）とは別に、「学園祭の日」「年末年始」のような
その年だけの休みを足すためのもの。祝日は自動では休まない（書いた日だけ休む）。

``dates``
    1 日ずつ。``"2026-11-02"`` か ``{"date": "2026-11-02", "name": "学園祭"}``。
``ranges``
    期間（両端を含む）。``{"from": "2026-12-28", "to": "2027-01-04", "name": "年末年始"}``。

放送の安全のため、ここは「失敗しても鳴らす側に倒す」。書き方の正しくない項目は
**その項目だけ**を無視し（その日は休まず鳴る）、理由を :attr:`ClosureCalendar.problems`
に日本語で残して ERROR で記録する。どんな JSON の値を渡しても例外にはしない。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: 日付の書き方（ASCII の数字だけ。``date.fromisoformat`` は版によって受け付ける形が違う）。
_DAY_PATTERN = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")

#: 問題の説明に添える、利用者が書いた値の最大文字数。
_SHOW_LIMIT = 40


class _Invalid(ValueError):
    """1 つの項目が使えない理由（利用者向けの日本語。``str()`` がそのまま説明になる）。"""


def _show(value: Any) -> str:
    """利用者が書いた値を、問題の説明に添える短い文字列にする。"""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:  # 想定外の型でも説明の組み立てで例外にしない
        text = repr(value)
    if len(text) > _SHOW_LIMIT:
        text = text[:_SHOW_LIMIT] + "…"
    return text


def _label(label: str) -> str:
    """項目名を、あとに続く言葉（「は」「の」）とつなぐ形にする。

    ``date``・``from`` のような英字の項目名は、あとに空白を入れて読みやすくする
    （``date は``）。「日付」のような日本語の項目名は、空白を入れずにつなぐ（``日付は``）。
    """
    return label if not label.isascii() else label + " "


@dataclass(frozen=True)
class Closure:
    """休館日 1 件（``first``〜``last`` の両端を含む期間。1 日だけなら同じ日）。"""

    first: date
    last: date
    name: str = ""

    def covers(self, day: date) -> bool:
        return self.first <= day <= self.last

    @property
    def reason(self) -> str:
        """鳴らさない理由。名前があれば ``休館: 学園祭``、なければ ``休館``。"""
        return "休館: " + self.name if self.name else "休館"


# -- 1 項目の読み取り（使えなければ _Invalid） ------------------------------------
def _parse_day(value: Any, label: str) -> date:
    """``"YYYY-MM-DD"`` の日付を読む。"""
    if not isinstance(value, str):
        raise _Invalid("{0}は \"2026-11-02\" のように文字で書きます（今は {1}）".format(
            _label(label), _show(value)))
    match = _DAY_PATTERN.fullmatch(value)
    if match is None:
        raise _Invalid("{0}の書き方が正しくありません（{1}）。YYYY-MM-DD の形で書いてください".format(
            _label(label), _show(value)))
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        raise _Invalid("{0}はカレンダーにない日付です（{1}）".format(
            _label(label), _show(value))) from None


def _read_name(entry: Mapping[str, Any]) -> str:
    """項目の ``name``。無い・null なら名前なし。空白は 1 つに詰める（ログや一覧を 1 行に保つ）。"""
    name = entry.get("name")
    if name is None:
        return ""
    if not isinstance(name, str):
        raise _Invalid("name は \"学園祭\" のように文字で書きます（今は {0}）。"
                       "名前が要らなければ name ごと省いてください".format(_show(name)))
    return " ".join(name.split())


def _parse_date_entry(entry: Any) -> Closure:
    """``dates`` の 1 項目: ``"2026-11-02"`` か ``{"date": …, "name": …}``。"""
    if isinstance(entry, str):
        day = _parse_day(entry, "日付")
        return Closure(day, day)
    if isinstance(entry, Mapping):
        if "date" not in entry:
            raise _Invalid("\"date\" がありません（{0}）".format(_show(entry)))
        day = _parse_day(entry["date"], "date")
        return Closure(day, day, _read_name(entry))
    raise _Invalid("\"2026-11-02\" か {{\"date\": \"2026-11-02\", \"name\": \"学園祭\"}} の形で"
                   "書いてください（今は {0}）".format(_show(entry)))


def _parse_range_entry(entry: Any) -> Closure:
    """``ranges`` の 1 項目: ``{"from": …, "to": …, "name": …}``（両端を含む）。"""
    if not isinstance(entry, Mapping):
        raise _Invalid("{{\"from\": \"2026-12-28\", \"to\": \"2027-01-04\", \"name\": \"年末年始\"}} の形で"
                       "書いてください（今は {0}）".format(_show(entry)))
    for key in ("from", "to"):
        if key not in entry:
            raise _Invalid("\"{0}\" がありません（{1}）".format(key, _show(entry)))
    first = _parse_day(entry["from"], "from")
    last = _parse_day(entry["to"], "to")
    if first > last:
        raise _Invalid("from（{0}）が to（{1}）より後になっています".format(
            first.isoformat(), last.isoformat()))
    return Closure(first, last, _read_name(entry))


#: 設定の節 → 項目の読み取り。順番がそのまま「最初に当たった項目」の優先順になる。
_SECTIONS: Tuple[Tuple[str, Callable[[Any], Closure]], ...] = (
    ("dates", _parse_date_entry),
    ("ranges", _parse_range_entry),
)


class ClosureCalendar:
    """``schedule.closures`` から作る、休館日の暦。

    ``closures``
        使える項目（``dates`` を先に、``ranges`` を後に、書いた順）。
    ``problems``
        無視した項目の説明（日本語。何番目の項目か・なぜ使えないかを含む）。
        作るときに 1 件ずつ ERROR で記録する。
    """

    def __init__(self, settings: Optional[Mapping[str, Any]]) -> None:
        self.closures: List[Closure] = []
        self.problems: List[str] = []
        # 1 日だけの項目は日付で引く（長い一覧でも、予定の探索で毎日全件を見ない）。
        self._single: Dict[date, Closure] = {}
        self._spans: List[Closure] = []

        if settings is None:
            pass
        elif not isinstance(settings, Mapping):
            self.problems.append(
                "schedule.closures は {{ \"dates\": [ … ], \"ranges\": [ … ] }} の形で書きます"
                "（今は {0}）。休館日は設定されていないものとして、いつもどおり鳴らします".format(
                    _show(settings)))
        else:
            for key, parse in _SECTIONS:
                self._load(key, parse, settings.get(key))
        for problem in self.problems:
            logger.error("休館日の設定に使えない項目があります: %s", problem)

    def _load(self, key: str, parse: Callable[[Any], Closure], entries: Any) -> None:
        """設定の 1 つの節（``dates`` か ``ranges``）を読む。無い・null は「なし」。"""
        if entries is None:
            return
        where = "schedule.closures." + key
        if not isinstance(entries, (list, tuple)):
            self.problems.append(
                "{0} は [ … ] の一覧で書きます（今は {1}）。この節は使わないので、いつもどおり鳴らします"
                .format(where, _show(entries)))
            return
        for number, entry in enumerate(entries, start=1):
            try:
                closure = parse(entry)
            except _Invalid as exc:
                self.problems.append(
                    "{0} の {1} 番目: {2}。この項目は使わないので、その日はいつもどおり鳴らします".format(
                        where, number, exc))
                continue
            self.closures.append(closure)
            if key == "dates":
                self._single.setdefault(closure.first, closure)
            else:
                self._spans.append(closure)

    def closure_on(self, day: date) -> Optional[Closure]:
        """``day`` が休館日なら、最初に当たった項目を返す（休館日でなければ ``None``）。"""
        if isinstance(day, datetime):
            day = day.date()
        found = self._single.get(day)
        if found is not None:
            return found
        for closure in self._spans:
            if closure.covers(day):
                return closure
        return None

    def closed_reason(self, day: date) -> Optional[str]:
        """``day`` が休館日なら ``"休館: 学園祭"``（名前なしは ``"休館"``）、そうでなければ ``None``。"""
        closure = self.closure_on(day)
        return None if closure is None else closure.reason
