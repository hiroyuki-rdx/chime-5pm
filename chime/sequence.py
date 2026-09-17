"""再生シーケンスの組み立て。

イベント（時報／閉館放送）から、実際に再生する :class:`~chime.audio.Segment`
の並びを作る。時報のあとに流す「天気予報／ひとこと」のおまけの組み立て
（既定は両方、``mode="choice"`` なら抽選）もここで行う。

天気取得や音声合成はここで完結させ、失敗しても本体（時報音・蛍の光）は
必ず鳴るように、おまけ部分は欠落を許容する設計とする。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, List, Mapping, Optional, Sequence

from .audio import Segment
from .quotes import QuoteError, QuotePicker
from .scheduler import Event
from .tts import TTSError, TTSService
from .weather import WeatherError, WeatherService

logger = logging.getLogger(__name__)

EXTRA_WEATHER = "weather"
EXTRA_QUOTE = "quote"


@dataclass
class PlaybackPlan:
    """1 回分の再生内容。"""

    event: Optional[Event]
    segments: List[Segment] = field(default_factory=list)
    spoken: List[str] = field(default_factory=list)
    quote: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = ["再生内容:"]
        for segment in self.segments:
            lines.append("  - {0}".format(segment.describe()))
        for text in self.spoken:
            lines.append("  読み上げ: {0}".format(text))
        for warning in self.warnings:
            lines.append("  警告: {0}".format(warning))
        return "\n".join(lines)


def _resolve_weather_hours(settings: Mapping[str, Any]) -> set:
    """``extra_segment.weather_hours`` を ``int`` の集合として返す。

    JSON 由来の設定では要素に文字列（例 ``"10"``）が混ざりうるため、
    ``int()`` で正規化してから比較する。変換できない要素は警告ログを
    出して無視する（設定ミス 1 件で放送全体が落ちないようにするため）。
    """
    hours = set()
    for raw in settings.get("weather_hours", []) or []:
        try:
            hours.add(int(raw))
        except (TypeError, ValueError):
            logger.warning(
                "extra_segment.weather_hours の要素を解釈できません: %r。無視します。",
                raw)
    return hours


def choose_extra(hour: int, settings: Mapping[str, Any],
                 rng: random.Random) -> Optional[str]:
    """時報のあとに流す内容（天気予報／ひとこと）を抽選する。"""
    if not settings.get("enabled", True):
        return None
    if hour in {int(h) for h in settings.get("always_weather_hours", []) or []}:
        return EXTRA_WEATHER
    if hour in {int(h) for h in settings.get("always_quote_hours", []) or []}:
        return EXTRA_QUOTE
    probability = float(settings.get("weather_probability", 0.0))
    return EXTRA_WEATHER if rng.random() < probability else EXTRA_QUOTE


class SequenceBuilder:
    """設定と各サービスから :class:`PlaybackPlan` を作る。"""

    def __init__(self, config, tts: TTSService, weather: WeatherService,
                 quotes: QuotePicker, state, time_signal_path: str,
                 rng: Optional[random.Random] = None,
                 today_provider: Optional[Callable[[], date]] = None) -> None:
        self.config = config
        self.tts = tts
        self.weather = weather
        self.quotes = quotes
        self.state = state
        self.time_signal_path = time_signal_path
        self.rng = rng or random.Random()
        # 天気予報の「今日」を決める手段。既定は OS のローカル日付だが、
        # スケジューリングは設定タイムゾーン（``ChimeApp.now()``）基準で動くため、
        # 呼び出し側（``ChimeApp``）はそちらの日付を渡すことで両者を一致させる。
        self.today_provider: Callable[[], date] = today_provider or date.today

    # ------------------------------------------------------------------
    def build(self, event: Event) -> PlaybackPlan:
        if event.kind == "hourly":
            return self.build_hourly(event.hour, event)
        if event.kind == "closing":
            return self.build_closing(event)
        raise ValueError("未知のイベント種別です: {0}".format(event.kind))

    def build_hourly(self, hour: int, event: Optional[Event] = None) -> PlaybackPlan:
        """時報（ポ・ポ・ポ・ポーン → 時刻読み上げ → おまけ）を組み立てる。

        おまけ（``extra_segment``）は ``mode`` で分岐する。

        - ``"both"``（既定） ``extra_segment.weather_hours`` に含まれる時刻
          だけ 天気予報 → ひとこと の順に両方流す。含まれない時刻は
          ひとことだけを流す（``WeatherService`` は呼ばない。毎正時に
          無駄な HTTP リクエストが発生するのを避けるため）。天気を流す
          時刻でも、その成否に関わらずひとことは必ず流す
          （``_append_weather`` に ``fallback=False`` を渡し、失敗時に
          「ひとこと」が二重に流れるのを防ぐ）。
        - ``"choice"``       従来どおり :func:`choose_extra` の抽選結果で、
          天気予報／ひとことのどちらか一方だけを流す。
        - それ以外（設定ミス） 警告ログを出したうえで ``"both"`` として
          扱う。放送そのものが設定ミスで壊れないようにするため。
        """
        from . import timesignal  # 循環参照を避けるための遅延インポート

        plan = PlaybackPlan(event=event)
        settings = self.config.section("time_signal")

        timesignal.ensure_time_signal(
            self.time_signal_path, settings, self.config.section("audio.mixer"))
        plan.segments.append(Segment(self.time_signal_path, label="時報音（ポ・ポ・ポ・ポーン）"))

        text = timesignal.announce_text(hour, settings)
        self._append_speech(plan, text, "時刻アナウンス")

        extra_settings = self.config.section("extra_segment")
        if extra_settings.get("enabled", True):
            mode = str(extra_settings.get("mode", "both") or "both")
            if mode == "choice":
                extra = choose_extra(hour, extra_settings, self.rng)
                if extra == EXTRA_WEATHER:
                    self._append_weather(plan, hour)
                elif extra == EXTRA_QUOTE:
                    self._append_quote(plan, hour)
            else:
                if mode != "both":
                    logger.warning(
                        "未知の extra_segment.mode です: %s。\"both\" として扱います。",
                        mode)
                if hour in _resolve_weather_hours(extra_settings):
                    self._append_weather(plan, hour, fallback=False)
                self._append_quote(plan, hour)
        return plan

    def build_closing(self, event: Optional[Event] = None) -> PlaybackPlan:
        """閉館放送（アナウンス → 蛍の光）を組み立てる。"""
        plan = PlaybackPlan(event=event)
        announce = self.config.path("closing.announce_file")
        music = self.config.path("closing.music_file")
        fade_in_ms = int(self.config.get("audio.fade_in_ms", 2000))

        if announce:
            plan.segments.append(Segment(announce, label="閉館アナウンス"))

        extra_text = str(self.config.get("closing.extra_text", "") or "")
        if extra_text:
            self._append_speech(plan, extra_text, "追加アナウンス")

        if music:
            plan.segments.append(
                Segment(music, label="蛍の光（{0}ms フェードイン）".format(fade_in_ms),
                        fade_in_ms=fade_in_ms))
        return plan

    def build_text(self, text: str) -> PlaybackPlan:
        """任意の文言を読み上げるだけのプラン（``--say`` 用）。"""
        return self.build_texts([text])

    def build_texts(self, texts: Sequence[str]) -> PlaybackPlan:
        """複数の文言を、1 文ずつ別のセグメントとして読み上げるプラン。

        天気予報のように複数の文から成る読み上げは、連結せず 1 文ずつ
        セグメントにする必要がある。作り置き音声は文単位で用意されており、
        連結した文字列では照合が外れて実行時合成（別人の声）に落ちるため。
        """
        plan = PlaybackPlan(event=None)
        for text in texts:
            self._append_speech(plan, text, "読み上げ")
        return plan

    # -- 部品 -----------------------------------------------------------
    def _append_speech(self, plan: PlaybackPlan, text: str, label: str,
                       optional: bool = True) -> bool:
        if not text:
            return False
        try:
            path = self.tts.synthesize(text)
        except TTSError as exc:
            message = "{0}を合成できませんでした: {1}".format(label, exc)
            logger.error(message)
            plan.warnings.append(message)
            return False
        plan.segments.append(Segment(path, label="{0}「{1}」".format(label, text),
                                     optional=optional))
        plan.spoken.append(text)
        return True

    def _append_weather(self, plan: PlaybackPlan, hour: int, fallback: bool = True) -> None:
        """天気予報を、地点の順に文ごと独立したセグメントとして積む。

        作り置き音声は文単位で用意されているため、複数文を 1 つの文字列に
        連結してはならない（連結すると照合が外れ、Open JTalk への
        フォールバックが発生する）。``describe_sentences()`` が返す各文を
        そのまま ``_append_speech`` に渡し、1 文 1 セグメントにする。

        ``fallback`` は ``extra_segment.mode == "choice"`` のときだけ効く
        （``fallback_to_quote`` 設定を見て「ひとこと」に切り替える）。
        ``mode == "both"`` では呼び出し側（``build_hourly``）がこの後で
        必ず ``_append_quote`` を呼ぶため、ここでも「ひとこと」へ落とすと
        二重に流れてしまう。そのため ``build_hourly`` は ``mode == "both"``
        のとき ``fallback=False`` を渡し、失敗時は警告を積むだけに留める。
        """
        settings = self.config.section("extra_segment")
        try:
            sentences = self.weather.describe_sentences(today=self.today_provider())
        except WeatherError as exc:
            message = "天気予報を取得できませんでした: {0}".format(exc)
            logger.warning(message)
            plan.warnings.append(message)
            if fallback and settings.get("fallback_to_quote", True):
                self._append_quote(plan, hour)
            return

        total = len(sentences)
        for index, sentence in enumerate(sentences, start=1):
            label = "天気予報（{0}/{1}）".format(index, total)
            self._append_speech(plan, sentence, label)

    def _append_quote(self, plan: PlaybackPlan, hour: int) -> None:
        recent = self.state.recent_quotes() if self.state else []
        try:
            quote = self.quotes.pick(hour, recent)
        except QuoteError as exc:
            message = "ひとことを選べませんでした: {0}".format(exc)
            logger.warning(message)
            plan.warnings.append(message)
            return
        if self._append_speech(plan, quote, "ひとこと"):
            plan.quote = quote
            if self.state:
                self.state.remember_quote(quote)
