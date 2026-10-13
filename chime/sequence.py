"""再生シーケンスの組み立て。

イベント（時報／閉館放送）から、実際に再生する :class:`~chime.audio.Segment`
の並びを作る。時報の放送は常に 時報音 → 時刻アナウンス → （``weather_hours``
の時刻だけ）天気予報 → ひとこと の 1 経路で、抽選は行わない。

天気取得や音声合成はここで完結させ、失敗しても本体（時報音・蛍の光）は
必ず鳴るように、おまけ部分は欠落を許容する設計とする。部品（時刻アナウンス・
天気・ひとこと・閉館の追加アナウンス）は :func:`_guard` で 1 つずつ守り、
1 つの失敗がほかの部品を巻き込まないようにする。

組み立ては state（再生済み・直近のひとこと）を書かない。選んだひとことは
``PlaybackPlan.quote`` に残し、再生できたあとに呼び出し側が記録する。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, List, Mapping, Optional, Sequence

from . import timesignal
from .audio import Segment
from .config import DEFAULT_CONFIG
from .phrases import closing_extra_text
from .quotes import QuoteError, QuotePicker
from .scheduler import Event
from .tts import TTSError, TTSService
from .weather import WeatherError, WeatherService

logger = logging.getLogger(__name__)


@dataclass
class PlaybackPlan:
    """1 回分の再生内容。"""

    event: Optional[Event]
    segments: List[Segment] = field(default_factory=list)
    spoken: List[str] = field(default_factory=list)
    quote: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    #: 読み上げるはずだったが、音声が用意できず無音になった文言（読み上げの順）。
    silent: List[str] = field(default_factory=list)
    #: 音源ファイルが無くて積めなかった、必須の部品の名前（時報音・閉館アナウンス・
    #: 蛍の光）。積めなかった部品は ``segments`` に無いので、再生の件数（played / total）
    #: には現れない。履歴が「すべて鳴った」と記録しないよう、ここに残す。
    missing: List[str] = field(default_factory=list)
    #: 組み立てに失敗して最小のプランに落としたとき True（``ChimeApp`` が立てる）。
    degraded: bool = False

    def describe(self) -> str:
        lines = ["再生内容:"]
        for segment in self.segments:
            lines.append("  - {0}".format(segment.describe()))
        for text in self.spoken:
            lines.append("  読み上げ: {0}".format(text))
        for text in self.silent:
            lines.append("  無音: {0}".format(text))
        for warning in self.warnings:
            lines.append("  警告: {0}".format(warning))
        return "\n".join(lines)


def _guard(plan: PlaybackPlan, label: str, func: Callable[..., Any], *args: Any) -> Any:
    """``func(*args)`` を呼び、想定外の例外はその部品だけ飛ばして続ける。

    例外はログ（トレースバック付き）と ``plan.warnings`` に残し、``None`` を
    返す。``KeyboardInterrupt`` / ``SystemExit`` は ``Exception`` の子では
    ないので捕まえない（停止要求は握りつぶさない）。
    """
    try:
        return func(*args)
    except Exception as exc:
        message = "{0}の組み立てに失敗しました（この部分だけ飛ばします）: {1}: {2}".format(
            label, type(exc).__name__, exc)
        logger.exception(message)
        plan.warnings.append(message)
        return None


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


class SequenceBuilder:
    """設定と各サービスから :class:`PlaybackPlan` を作る。"""

    def __init__(self, config, tts: TTSService, weather: WeatherService,
                 quotes: QuotePicker, state, time_signal_path: str,
                 today_provider: Optional[Callable[[], date]] = None) -> None:
        self.config = config
        self.tts = tts
        self.weather = weather
        self.quotes = quotes
        self.state = state
        self.time_signal_path = time_signal_path
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

        おまけ（``extra_segment``）は 1 経路だけ。``extra_segment.weather_hours``
        に含まれる時刻だけ 天気予報 → ひとこと の順に流し、含まれない時刻は
        ひとことだけを流す（``WeatherService`` は呼ばない。毎正時に無駄な
        HTTP リクエストが発生するのを避けるため）。天気を流す時刻でも、
        その成否に関わらずひとことは必ず 1 つ流す。天気の取得に失敗しても
        ひとことへ切り替えはしない（ここで切り替えると、この後の
        ひとことと二重になるため）。``extra_segment.enabled`` が false なら
        おまけは流さない。

        どの部品が壊れても、ほかの部品は残す。時報音の生成に失敗したときは
        既存のファイルを使い、ファイルも無ければ時報音だけを省く（必須
        セグメントを積むと、再生時の ``PlaybackError`` で全体が消えるため）。
        時刻アナウンスのテンプレートの書き間違いは既定の文言に戻す。
        天気・ひとことの想定外の例外は :func:`_guard` が受ける。

        v6.0.0 で、抽選方式（``mode="choice"`` とその関連キー）は廃止した。
        古い設定にそれらが残っていても読まない。
        """
        plan = PlaybackPlan(event=event)
        settings = self.config.section("time_signal")

        self._append_time_signal(plan, settings)
        _guard(plan, "時刻アナウンス", self._append_announce, plan, hour, settings)

        extra_settings = self.config.section("extra_segment")
        if extra_settings.get("enabled", True):
            _guard(plan, "天気予報", self._append_weather_if_due, plan, hour, extra_settings)
            _guard(plan, "ひとこと", self._append_quote, plan, hour)
        return plan

    def build_closing(self, event: Optional[Event] = None) -> PlaybackPlan:
        """閉館放送（アナウンス → 蛍の光）を組み立てる。

        アナウンスと音楽は、ファイルの有無を確かめてから積む。無い方だけを
        ERROR で飛ばし、残りは鳴らす。
        """
        plan = PlaybackPlan(event=event)
        self._append_closing_announce(plan)
        _guard(plan, "追加アナウンス", self._append_closing_text, plan)
        self._append_closing_music(plan)
        return plan

    def build_minimal(self, event: Event) -> PlaybackPlan:
        """組み立て全体が失敗したときに鳴らす、最小のプラン。

        ``hourly`` は時報音だけ、``closing`` は閉館アナウンスと蛍の光だけ。
        読み上げ・天気・ひとこと・テンプレートは使わない（壊れる原因を
        持ち込まないため）。音源が無ければ、その部品は積まない。
        """
        plan = PlaybackPlan(event=event)
        if event.kind == "hourly":
            self._append_time_signal(plan, self.config.section("time_signal"))
        elif event.kind == "closing":
            self._append_closing_announce(plan)
            self._append_closing_music(plan)
        else:
            message = "未知のイベント種別です: {0}".format(event.kind)
            logger.error(message)
            plan.warnings.append(message)
        return plan

    def build_text(self, text: str) -> PlaybackPlan:
        """任意の文言を読み上げるだけのプラン（``--say`` 用）。"""
        return self.build_texts([text])

    def build_texts(self, texts: Sequence[str]) -> PlaybackPlan:
        """複数の文言を、1 文ずつ別のセグメントとして読み上げるプラン。

        天気予報のように複数の文から成る読み上げは、連結せず 1 文ずつ
        セグメントにする必要がある。作り置き音声は文単位で用意されており、
        連結した文字列では照合が外れてその文が無音になるため。
        """
        plan = PlaybackPlan(event=None)
        for text in texts:
            self._append_speech(plan, text, "読み上げ")
        return plan

    # -- 部品 -----------------------------------------------------------
    def _append_audio_file(self, plan: PlaybackPlan, path: str, label: str,
                           fade_in_ms: int = 0) -> None:
        """音源ファイルがあれば積む。無ければ ERROR で飛ばす（積まない）。

        必須セグメントのファイルが欠けていると、再生時の ``PlaybackError``
        でプラン全体が鳴らなくなる。積む前に確かめ、欠けた部品だけを省く。
        省いた部品は ``plan.missing`` に名前（``label``）を残す。
        パスが空文字列なら「設定しない」の意味なので、黙って積まない
        （欠けたことにも数えない）。
        """
        if not path:
            return
        if not os.path.exists(path):
            message = "音源ファイルが見つかりません: {0}".format(path)
            logger.error(message)
            plan.warnings.append(message)
            plan.missing.append(label)
            return
        plan.segments.append(Segment(path, label=label, fade_in_ms=fade_in_ms))

    def _append_time_signal(self, plan: PlaybackPlan, settings: Mapping[str, Any]) -> None:
        """時報音（ポ・ポ・ポ・ポーン）を積む。生成に失敗しても既存のファイルを使う。"""
        try:
            timesignal.ensure_time_signal(
                self.time_signal_path, settings, self.config.section("audio.mixer"))
        except Exception as exc:
            message = "時報音を生成できませんでした（既存のファイルがあればそれを使います）: {0}: {1}".format(
                type(exc).__name__, exc)
            logger.exception(message)
            plan.warnings.append(message)
        self._append_audio_file(plan, self.time_signal_path, "時報音（ポ・ポ・ポ・ポーン）")

    def _append_announce(self, plan: PlaybackPlan, hour: int,
                         settings: Mapping[str, Any]) -> None:
        """「午前10時をお知らせしたのだ。」を積む。テンプレートの書き間違いは既定の文言に戻す。

        ``announce_template`` / ``noon_template`` の未知の置換名（例 ``{hours}``）
        などは ``announce_text`` で例外になる。既定の文言は作り置き済みなので、
        ``DEFAULT_CONFIG`` の設定で作り直せば鳴らせる。
        """
        try:
            text = timesignal.announce_text(hour, settings)
        except (KeyError, IndexError, ValueError) as exc:
            message = ("時刻アナウンスのテンプレート（announce_template / noon_template）が"
                       "不正です（既定の文言に戻します）: {0}: {1}".format(type(exc).__name__, exc))
            logger.error(message)
            plan.warnings.append(message)
            text = timesignal.announce_text(hour, DEFAULT_CONFIG["time_signal"])
        self._append_speech(plan, text, "時刻アナウンス")

    def _append_weather_if_due(self, plan: PlaybackPlan, hour: int,
                               extra_settings: Mapping[str, Any]) -> None:
        if hour in _resolve_weather_hours(extra_settings):
            self._append_weather(plan)

    def _append_closing_announce(self, plan: PlaybackPlan) -> None:
        self._append_audio_file(
            plan, self.config.path("closing.announce_file"), "閉館アナウンス")

    def _append_closing_text(self, plan: PlaybackPlan) -> None:
        # 作り置きの列挙（chime.phrases）と同じ読み出しを使う。読み方が食い違うと、
        # 作り置きに無い文言を読もうとして無音になる。
        extra_text = closing_extra_text(self.config)
        if extra_text:
            self._append_speech(plan, extra_text, "追加アナウンス")

    def _closing_fade_in_ms(self) -> int:
        return int(self.config.get("audio.fade_in_ms", 2000))

    def _append_closing_music(self, plan: PlaybackPlan) -> None:
        # フェードインの設定が壊れていても、音楽は鳴らす（フェードだけ省く）。
        fade_in_ms = _guard(plan, "フェードイン時間", self._closing_fade_in_ms) or 0
        self._append_audio_file(
            plan, self.config.path("closing.music_file"),
            "蛍の光（{0}ms フェードイン）".format(fade_in_ms), fade_in_ms)

    def _append_speech(self, plan: PlaybackPlan, text: str, label: str) -> bool:
        if not text:
            return False
        try:
            path = self.tts.synthesize(text)
        except TTSError as exc:
            message = "{0}を合成できませんでした（「{1}」）: {2}".format(label, text, exc)
            logger.error(message)
            plan.warnings.append(message)
            plan.silent.append(text)
            return False
        # 読み上げは欠けても放送全体を止めない（無音になるだけ）ので常に optional。
        # 必須なのは時報音と閉館の音源（_append_audio_file）だけ。
        plan.segments.append(Segment(path, label="{0}「{1}」".format(label, text),
                                     optional=True))
        plan.spoken.append(text)
        return True

    def _append_weather(self, plan: PlaybackPlan) -> None:
        """天気予報を、地点の順に文ごと独立したセグメントとして積む。

        作り置き音声は文単位で用意されているため、複数文を 1 つの文字列に
        連結してはならない（連結すると照合が外れ、その文が無音になる）。
        ``describe_sentences()`` が返す各文を
        そのまま ``_append_speech`` に渡し、1 文 1 セグメントにする。

        取得に失敗したら警告を積むだけで、「ひとこと」へは切り替えない。
        呼び出し側（``build_hourly``）がこの後で必ず ``_append_quote`` を
        呼ぶので、ここで流すと二重になる。``weather.enabled`` が false の
        ときも ``WeatherService`` が ``WeatherError`` を送出するので、
        同じ警告になる。
        """
        try:
            sentences = self.weather.describe_sentences(today=self.today_provider())
        except WeatherError as exc:
            message = "天気予報を取得できませんでした: {0}".format(exc)
            logger.warning(message)
            plan.warnings.append(message)
            return

        total = len(sentences)
        for index, sentence in enumerate(sentences, start=1):
            label = "天気予報（{0}/{1}）".format(index, total)
            self._append_speech(plan, sentence, label)

    def _append_quote(self, plan: PlaybackPlan, hour: int) -> None:
        recent = self.state.recent_quotes()
        try:
            quote = self.quotes.pick(hour, recent)
        except QuoteError as exc:
            message = "ひとことを選べませんでした: {0}".format(exc)
            logger.warning(message)
            plan.warnings.append(message)
            return
        if self._append_speech(plan, quote, "ひとこと"):
            # state には書かない。鳴らせたかどうかは再生後にしか分からないので、
            # 記録は呼び出し側（ChimeApp.run_event）が plan.quote を見て行う。
            plan.quote = quote
