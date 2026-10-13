"""アプリケーション本体（各サービスの組み立てと常駐ループ）。"""

from __future__ import annotations

import itertools
import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from . import buildinfo, env, timesignal
from .audio import PlaybackError, PlaybackInterrupted, Player, create_player
from .config import Config
from .history import RESULT_INTERRUPTED, RESULT_SKIPPED, History, make_entry
from .pause import Pause, PauseStore
from .phrases import CoverageTooLarge, coverage
from .quotes import QuotePicker
from .scheduler import Event, Scheduler, format_events
from .sequence import PlaybackPlan, SequenceBuilder
from .state import State
from .tts import TTSService
from .weather import WeatherService

logger = logging.getLogger(__name__)

#: 予定を求められないとき、やり直すまでの待ち時間（秒）。
RETRY_SECONDS = 60

#: 鳴らす予定が 1 つも無いとき、予定を調べ直すまでの待ち時間（秒）。休館日や休止で
#: 予定が長く空くことがあるので、60 秒ごとには調べず（警告も 1 度だけにして）1 時間おきにする。
NO_EVENTS_RECHECK_SECONDS = 3600

#: 停止要求で再生を打ち切ったときの、再生結果と履歴に残す説明。
INTERRUPTED_TEXT = "停止要求で中断"

#: 作り置きが足りないとき、警告に挙げる文言の数。
MISSING_PREVIEW = 3


def exception_text(exc: BaseException) -> str:
    """例外のメッセージ（``str(exc)``）。``str()`` が失敗する例外では、型名を返す。

    履歴・再生の結果・ログに残す説明を作るところで、説明のために例外が増えて
    放送や常駐ループを止めることのないようにする。
    """
    try:
        return str(exc)
    except Exception:  # __str__ が例外を出す（または文字列を返さない）例外
        return type(exc).__name__


def describe_exception(exc: BaseException) -> str:
    """例外を ``型名: メッセージ`` の 1 行にする。``str()`` が失敗する例外では型名だけ。"""
    try:
        return "{0}: {1}".format(type(exc).__name__, exc)
    except Exception:
        return type(exc).__name__


def pause_reason(pause: Pause) -> str:
    """履歴とログに残す、休止の理由。``休止``、止めた理由があれば ``休止: 学園祭の準備``。"""
    return "休止: " + pause.reason if pause.reason else "休止"


@dataclass(frozen=True)
class PlayOutcome:
    """再生 1 回の結果（:meth:`ChimeApp.play_with_result` の戻り値）。"""

    #: 再生できたか（``--dry-run`` は再生しないが失敗ではないので True）。
    ok: bool
    #: 実際に再生したセグメント数（``--dry-run`` や再生できなかったときは 0）。
    played: int
    #: 再生するはずだったセグメント数。
    total: int
    #: 再生が例外で終わったときの説明（例外が無ければ ``None``）。停止要求で打ち切った
    #: ときも入る（:data:`INTERRUPTED_TEXT`）。
    error: Optional[str] = None
    #: 停止要求で、再生を途中で打ち切ったか（``played`` は打ち切るまでに鳴らせた数）。
    interrupted: bool = False
    #: 音を出し始めたか。``False`` は、停止要求がデバイスを開く前（最初の部品が鳴り出す前）に
    #: 届いて、何も鳴っていないこと。``interrupted`` のときだけ意味がある（それ以外は ``True``）。
    started: bool = True


class ChimeApp:
    """設定から各サービスを組み立て、常駐ループを回す。"""

    def __init__(self, config: Config, backend: Optional[str] = None,
                 dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.tzinfo = self._resolve_timezone(config.get("timezone", "Asia/Tokyo"))
        self.stop_event = threading.Event()

        # dry-run は実機の記録（再生済み・直近のひとこと）を書き換えない。
        self.state = State(config.path("state.file"), read_only=dry_run)
        self.tts = TTSService(
            config.section("tts"),
            config.path("tts.cache_dir"),
            config.path("tts.prerecorded_dir"),
        )
        self.weather = WeatherService(config.section("weather"))
        self.quotes = QuotePicker(
            config.path("quotes.file"),
            int(config.get("quotes.avoid_recent", 8)),
        )
        self.time_signal_path = config.path("time_signal.output_file")
        self.builder = SequenceBuilder(
            config, self.tts, self.weather, self.quotes, self.state,
            self.time_signal_path,
            # 天気予報の「今日」も、スケジューリングと同じ設定タイムゾーン基準にする
            # （OS のローカル時刻が UTC のままでも日付がずれないように）。
            today_provider=lambda: self.now().date(),
        )
        self.scheduler = Scheduler(
            config.section("schedule"),
            self.tzinfo,
            timesignal.lead_seconds(config.section("time_signal")),
            clock=self.now,
        )
        # 「今日だけ止める」の記録。常駐のまま、現地の操作（--pause-today など）に従う。
        # 場所が空文字列なら、止める機能を使わない（読んでも「止めていない」になる）。
        self.pause = PauseStore(config.path("state.pause_file"),
                                today=lambda: self.now().date())
        self._backend = backend
        self._player: Optional[Player] = None
        #: 「予定されたイベントがありません」を、今の途切れで警告したか（1 度だけ警告する）。
        self._no_events_warned = False

    # ------------------------------------------------------------------
    @property
    def player(self) -> Player:
        """再生バックエンド（初回参照時に決定する）。"""
        if self._player is None:
            self._player = create_player(self.config.section("audio"), self._backend)
        return self._player

    @staticmethod
    def _resolve_timezone(name: str):
        try:
            return ZoneInfo(str(name))
        except Exception as exc:  # 未知の名前や tzdata 欠落でも起動は止めない
            logger.error("タイムゾーン '%s' を解決できません（OS のローカル時刻を使用します）: %s",
                         name, exc)
            return None

    def now(self) -> datetime:
        return datetime.now(self.tzinfo)

    def install_signal_handlers(self) -> None:
        """SIGTERM / SIGINT で待機を打ち切れるようにする。"""
        def _handler(signum, _frame):
            logger.info("シグナル %s を受信しました。停止します。", signum)
            self.stop_event.set()

        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handler)
                except ValueError:  # pragma: no cover - サブスレッドでは設定できない
                    pass

    def log_environment(self) -> None:
        logger.info("%s", buildinfo.version_string())
        info = env.describe()
        logger.info("実行環境: %s %s (%s) / Python %s / WSL=%s",
                    info["system"], info["release"], info["machine"],
                    info["python"], info["wsl"])
        logger.info("再生バックエンド: %s / TTS: %s", self.player.name, self._describe_tts())
        logger.info("設定ソース: %s", " → ".join(self.config.sources))
        self._log_coverage()

    def _describe_tts(self) -> str:
        """読み上げエンジンの状態（VOICEVOX ENGINE への疎通確認を含む）。

        疎通確認は通信するので、設定の値次第で例外になりうる（待ち時間が大きすぎて
        ``OverflowError`` など）。ログに残すための確認が起動を止めて、systemd が再起動を
        繰り返すだけにならないよう、失敗は WARNING にして続ける。
        """
        try:
            return self.tts.describe()
        except Exception as exc:
            logger.warning("読み上げエンジンの状態を調べられませんでした（起動は続けます）: %s",
                           describe_exception(exc))
            return "確認できません"

    def _log_coverage(self) -> None:
        """作り置きの音声が揃っているかを記録する。足りなければ WARNING。

        数えられなくても（ひとことの定義ファイルが壊れているなど）起動は止めない。
        設定の値で文言が増えすぎるとき（気温の幅など）は、数え上げを省いて WARNING
        だけ残す（``phrases.coverage`` の上限）。
        """
        try:
            result = coverage(self.config, self.tts.prerecorded_lookup)
        except CoverageTooLarge as exc:
            # 設定の値で文言が際限なく増える。数え上げで起動を遅らせない。
            logger.warning("作り置きの音声の数え上げを省きました（起動を遅くしないため）: %s",
                           exception_text(exc))
            return
        except Exception as exc:
            logger.warning("作り置きの音声を数えられませんでした: %s", describe_exception(exc))
            return
        logger.info("作り置きの音声: %d/%d 件", result.total - len(result.missing), result.total)
        if result.missing:
            logger.warning(
                "作り置きの音声が %d 件ありません（VOICEVOX ENGINE が使えない環境では、"
                "その文は無音になります）。先頭 %d 件まで: %s",
                len(result.missing), MISSING_PREVIEW,
                "、".join("「{0}」".format(text) for text in result.missing[:MISSING_PREVIEW]))

    # -- 再生 -----------------------------------------------------------
    def play(self, plan: PlaybackPlan) -> bool:
        """プランを再生する（``--dry-run`` の場合はログのみ）。

        戻り値は実際に再生できたかどうか。``--dry-run`` は再生自体を行わない
        ため失敗ではなく常に ``True``。再生対象のセグメントが 1 つもない場合
        や、再生中にエラーが発生した場合（``PlaybackError`` や予期しない例外）、
        セグメントはあってもすべて欠落した optional でスキップされ実際には
        何も再生できなかった場合は ``False`` を返す。いずれの場合も例外は
        外へ送出しない（放送失敗でプロセスを落とさないという方針は変えない）。
        件数や失敗の理由まで要るときは :meth:`play_with_result` を使う。
        """
        return self.play_with_result(plan).ok

    def play_with_result(self, plan: PlaybackPlan) -> PlayOutcome:
        """:meth:`play` と同じ再生を行い、結果を :class:`PlayOutcome` で返す。

        ログも「例外を外へ送出しない」方針も :meth:`play` と同じ。``--dry-run``
        は ``ok=True``・``played=0``。再生が例外で終わったときは ``error`` に
        その説明を入れる（途中で止まったときは、鳴らせた数が分からないので
        ``played`` は 0）。停止要求（``stop_event``）で打ち切ったときは、鳴らせた数を
        ``played`` に入れ、``interrupted=True``・``error`` は :data:`INTERRUPTED_TEXT`。
        音を出す前に打ち切ったとき（何も鳴っていない）は、``started=False``。
        """
        logger.info(plan.describe())
        total = len(plan.segments)
        if self.dry_run:
            logger.info("dry-run のため再生しません。")
            return PlayOutcome(ok=True, played=0, total=total)
        if not plan.segments:
            return PlayOutcome(ok=False, played=0, total=0)
        try:
            played = self.player.play(plan.segments, stop=self.stop_event)
        except PlaybackInterrupted as exc:  # PlaybackError の子なので、先に受ける
            logger.warning("停止要求のため再生を中断しました（%d/%d 件を鳴らしました）",
                           exc.played, total)
            # started は再生側が付ける印。無ければ（付けない再生側でも）音は出たものとして扱う。
            return PlayOutcome(ok=False, played=exc.played, total=total,
                               error=INTERRUPTED_TEXT, interrupted=True,
                               started=getattr(exc, "started", True))
        except PlaybackError as exc:
            reason = exception_text(exc)
            logger.error("再生に失敗しました: %s", reason)
            return PlayOutcome(ok=False, played=0, total=total, error=reason)
        except Exception as exc:  # 再生失敗でプロセスは落とさない
            logger.exception("再生中に予期しないエラーが発生しました: %s", exception_text(exc))
            return PlayOutcome(ok=False, played=0, total=total, error=describe_exception(exc))
        if not played:
            logger.warning("再生できたセグメントがありませんでした。")
            return PlayOutcome(ok=False, played=0, total=total)
        logger.info("再生シーケンスが完了しました。")
        return PlayOutcome(ok=True, played=played, total=total)

    def _is_fired(self, event: Event) -> bool:
        """そのイベントを再生済みとして記録しているか（二重再生の防止）。"""
        return self.state.is_fired(event.key, event.day)

    def _current_pause(self) -> Optional[Pause]:
        """今効いている休止の記録。無い・読めない・壊れているときは ``None``（鳴らす側に倒す）。

        ``PauseStore.current`` は例外を出さないが、放送の経路なので念のため受け止める。
        """
        try:
            return self.pause.current()
        except Exception as exc:  # 休止の記録のせいで放送を止めない
            logger.warning("休止の記録を確かめられませんでした（放送は通常どおり行います）: %s",
                           describe_exception(exc))
            return None

    def _pause_covering(self, event: Event) -> Optional[Pause]:
        """そのイベントの日を止めている休止の記録。止めていなければ ``None``。

        休止は、イベントの **鳴る予定の日**（``event.at`` の日付）で照らす。再生を始める
        時刻（``play_at``）の日付ではない。ポーンを先行させるので、0 時の時報は前日の
        23:59:57 に鳴り始めるが、それは「次の日の放送」として扱う（前日だけの休止では
        止まらず、翌日の休止で止まる）。休館日（``schedule.closures``）の日付の決め方と同じ。
        """
        pause = self._current_pause()
        if pause is not None and pause.covers(event.at.date()):
            return pause
        return None

    def _paused_for(self, event: Event) -> Callable[[], bool]:
        """``sleep_until(interrupt=...)`` に渡す、「そのイベントが止められたか」の確認。"""
        return lambda: self._pause_covering(event) is not None

    def _next_pending_event(self) -> Optional[Event]:
        """まだ鳴らしていない次のイベント。選ぶときも待機後の再確認も同じ基準にする。

        再生済みのイベントに加えて、休止の記録が止めている日のイベントも飛ばす。休止の
        記録は 1 回の選び直しにつき 1 度だけ読む（日をまたいで何十件も調べても、読み直さない）。
        止めているイベントは再生済みにはしないので、休止が解ければ追いかけ再生の
        猶予（``catchup_grace_seconds``）のうちは鳴る。
        """
        pause = self._current_pause()

        def settled(event: Event) -> bool:
            # 日の対応づけは _pause_covering と同じ（イベントの at の日付）。
            return self._is_fired(event) or (pause is not None and pause.covers(event.at.date()))

        return self.scheduler.next_event(is_fired=settled)

    def _build_plan(self, event: Event) -> PlaybackPlan:
        """再生内容を組み立てる。失敗したら最小のプランに落とす。

        最小のプランには ``degraded`` の印を付ける（履歴に残すため）。最小の
        プランの組み立てまで失敗した場合は、例外をそのまま送出する
        （握りつぶさない。受け止めるのは ``run_forever`` 側）。
        """
        try:
            return self.builder.build(event)
        except Exception as exc:
            logger.exception("再生内容の組み立てに失敗しました（最小の内容で鳴らします）: %s",
                             exception_text(exc))
            plan = self.builder.build_minimal(event)
            plan.degraded = True
            return plan

    def _record_history(self, event: Event, plan: PlaybackPlan, outcome: PlayOutcome) -> None:
        """再生した放送の結果を履歴に残す。

        停止要求で打ち切ったときは、結果を ``interrupted`` にする（説明は結果が言うので
        ``error`` は付けない）。
        """
        self._append_history(event, played=outcome.played, total=outcome.total,
                             silent=plan.silent, missing=plan.missing,
                             warnings=plan.warnings, degraded=plan.degraded,
                             error=None if outcome.interrupted else outcome.error,
                             result=RESULT_INTERRUPTED if outcome.interrupted else None)

    def _record_skip(self, event: Event, pause: Pause) -> None:
        """休止のために鳴らさなかったことを履歴に残す（再生していないので 0/0）。"""
        self._append_history(event, played=0, total=0, result=RESULT_SKIPPED,
                             reason=pause_reason(pause))

    def _record_failure(self, event: Event, exc: Exception) -> None:
        """放送が例外で終わったことを履歴に残す（鳴らせた数は分からないので 0）。"""
        self._append_history(event, played=0, total=0, error=describe_exception(exc))

    def _append_history(self, event: Event, **fields: Any) -> None:
        """履歴に 1 行足す。``--dry-run`` では残さない。

        書けなくても、放送も常駐ループも止めない（警告を残すだけ）。
        履歴の場所が空文字列なら「残さない」の意味。
        """
        if self.dry_run:
            return
        try:
            path = self.config.path("state.history_file")
            if path:
                History(path).append(
                    make_entry(at=event.at, key=event.key, kind=event.kind, **fields))
        except Exception as exc:  # 履歴は付け足し。放送は止めない
            logger.warning("放送の履歴を残せませんでした（放送は続けます）: %s",
                           describe_exception(exc))

    def _skip_if_paused(self, event: Event) -> bool:
        """そのイベントの日が休止中なら、鳴らさない記録（履歴）を残して ``True``。

        再生済みの記録は付けない（付けると、休止がすぐ解けても鳴らない）。休止の記録が
        壊れているときは止めていないものとして扱うので、``False``（鳴らす）。
        """
        pause = self._pause_covering(event)
        if pause is None:
            return False
        logger.info("休止中のため鳴らしません（%s）: %s", pause_reason(pause), event.describe())
        self._record_skip(event, pause)
        return True

    def _wait_for_play_time(self, event: Event) -> bool:
        """再生の時刻まで待つ。鳴らしてよければ ``True``、取りやめたら ``False``。

        取りやめるのは、停止要求があったとき（ログだけ）と、休止の記録が効いたとき
        （履歴に ``skipped``）。待ち終えたあと、再生の直前にも休止を確かめる。確かめる
        のは設定タイムゾーンの日付と小さな記録ファイルだけで、時報の「ポーン」を
        遅らせない（正時直前の最終調整のあいだは、待機の中では確かめない）。
        """
        woke = self.scheduler.sleep_until(event.play_at, self.stop_event, precise=True,
                                          interrupt=self._paused_for(event))
        if not woke and not self.stop_event.is_set():
            # 待っている途中で休止の記録が効いた。
            if self._skip_if_paused(event):
                return False
            # 確かめ直すと、もう止めていない（すぐ解かれた）。再生の時刻より前に鳴らして
            # しまわないよう、待ちの続きをする（こんどは休止では打ち切らない）。
            woke = self.scheduler.sleep_until(event.play_at, self.stop_event, precise=True)
        if not woke:
            logger.info("停止要求のため再生を中止しました: %s", event.describe())
            return False
        return not self._skip_if_paused(event)

    def run_event(self, event: Event) -> None:
        """イベント 1 件を準備・再生し、再生済みとして記録する。

        組み立てが失敗しても、最小のプラン（時報音／閉館アナウンスと蛍の光）
        で鳴らす。選んだひとことは、再生できたときだけ記録する（鳴らせな
        かった文を「使った」ことにしない）。再生済みの記録は、再生に失敗
        しても付ける（無限にやり直さない）。再生したあとは、結果を履歴にも
        残す（``--dry-run`` を除く。書けなくても放送には影響しない）。

        休止（「今日だけ止める」）は、組み立てる直前と、再生する直前に確かめる。止めて
        いたら鳴らさず、再生済みの記録も付けず、履歴に ``skipped`` を残して戻る。
        停止要求で再生を途中で止めたときは、再生済みの記録を付ける（やり直すと、
        同じ放送を頭から鳴らし直してしまう）。ただし、音を出す前に止まったとき
        （``PlayOutcome.started`` が偽。デバイスを開く前に停止要求が届いた）は、何も
        鳴っていないので、再生済みの記録も履歴も付けずに戻る（再起動したあと、追いかけ再生の
        猶予 ``catchup_grace_seconds`` のうちは、鳴らし直せる）。
        """
        logger.info("イベント準備: %s", event.describe())
        if self._skip_if_paused(event):
            return
        plan = self._build_plan(event)

        if not self._wait_for_play_time(event):
            return

        logger.info("再生開始: %s", event.describe())
        outcome = self.play_with_result(plan)
        if outcome.interrupted and not outcome.started:
            logger.info("音を出す前に停止要求を受けたため、再生済みにしません"
                        "（再起動のあと、猶予時間のうちは追いかけて鳴らします）: %s", event.describe())
            return
        if outcome.ok and plan.quote and not self.dry_run:
            self.state.remember_quote(plan.quote)
        self.state.mark_fired(event.key, event.day)
        self._record_history(event, plan, outcome)

    def _mark_fired_after_failure(self, event: Event) -> None:
        """失敗した放送を再生済みとして記録する（無限にやり直さない）。失敗しても例外は出さない。"""
        try:
            self.state.mark_fired(event.key, event.day)
        except Exception as exc:  # 記録できなくても常駐は継続する
            logger.error("失敗した放送を再生済みとして記録できませんでした（継続します）: %s",
                         describe_exception(exc))

    def _record_failure_safely(self, event: Event, exc: Exception) -> None:
        """放送の失敗を履歴に残す。残せなくても例外は出さない。"""
        try:
            self._record_failure(event, exc)
        except Exception as inner:  # 履歴は付け足し。常駐は継続する
            logger.warning("放送の履歴を残せませんでした（放送は続けます）: %s",
                           describe_exception(inner))

    def _log_upcoming(self) -> None:
        """次回以降の予定を記録する（求められなくても起動は止めない）。

        休止の記録が効いているときは、止めている日の予定は並べない（直前の「この間の放送は
        鳴らしません」と食い違い、鳴るように見えてしまう）。休止が明けたあとの予定を、
        ``--schedule`` と同じ見出しで並べる。休止の記録が無ければ、今までどおり。
        """
        try:
            pause = self._current_pause()
            if pause is None:
                heading = "次回以降の予定:"
                upcoming = self.scheduler.upcoming(limit=5)
            else:
                heading = "次回以降の予定（休止のあいだは省いています）:"
                # 休止の日付の対応づけは選ぶときと同じ（イベントの at の日付）。
                events = (event for event in self.scheduler.iter_events(self.scheduler.now())
                          if not pause.covers(event.at.date()))
                upcoming = list(itertools.islice(events, 5))
        except Exception as exc:
            logger.exception("次回以降の予定を求められませんでした（継続します）: %s",
                             exception_text(exc))
            return
        logger.info("%s\n%s", heading, format_events(upcoming))

    def _wait_without_events(self) -> None:
        """鳴らす予定が無いあいだ待つ（:data:`NO_EVENTS_RECHECK_SECONDS` 秒）。

        停止要求や休止の記録の書き換えがあれば、そこで切り上げる。警告は、予定が途切れて
        最初の 1 度だけ。次の予定が見つかると、また警告できる。
        """
        if not self._no_events_warned:
            logger.warning("予定されたイベントがありません。%d 分ごとに再確認します"
                           "（この警告は予定が見つかるまで繰り返しません）。",
                           NO_EVENTS_RECHECK_SECONDS // 60)
            self._no_events_warned = True
        self.scheduler.sleep_until(
            self.now() + timedelta(seconds=NO_EVENTS_RECHECK_SECONDS), self.stop_event,
            interrupt=self.pause.changed)

    def _log_pause(self) -> None:
        """起動時に、休止の記録が効いていれば知らせる（再起動しても休止は続くため）。"""
        pause = self._current_pause()
        if pause is not None:
            logger.info("休止の記録があります（%s〜%s。%s）。この間の放送は鳴らしません。",
                        pause.first_day.isoformat(), pause.last_day.isoformat(),
                        pause_reason(pause))

    def _next_due_event(self) -> Optional[Event]:
        """次に鳴らすイベントを、準備の時刻まで待ってから返す。

        待ったあとも予定が変わっていないイベントだけを返す。予定が無い・待機中に
        予定が変わった・停止要求があったときは ``None``（呼び出し側がループを
        回し直す。停止要求は ``while`` の条件が見る）。

        待っている途中で休止の記録が書き換わったら（現地の ``--pause-today`` や
        ``--pause-off``）待つのをやめて、予定を選び直す。変化の基準は、休止の記録を
        読む前に取り直す（読んだあとに取ると、その間の書き換えを見逃す）。
        """
        self.pause.changed()
        event = self._next_pending_event()
        if event is None:
            self._wait_without_events()
            return None
        self._no_events_warned = False

        if not self.scheduler.sleep_until(event.prepare_at, self.stop_event,
                                          interrupt=self.pause.changed):
            if not self.stop_event.is_set():
                logger.info("休止の記録が変わりました。予定を選び直します。")
            return None

        # 待機中に日付や時刻が大きく動いた場合に備え、対象イベントを再確認する。
        current = self._next_pending_event()
        if current is None or current.key != event.key or current.at != event.at:
            logger.info("待機中に予定が変わりました。再計算します。")
            return None
        return event

    def run_forever(self) -> int:
        """常駐ループ。

        予定を求められなくても（スケジューラーの例外）、ループは落とさない。
        ログに残して ``RETRY_SECONDS`` 秒後にやり直す（落ちると systemd が
        再起動を繰り返すだけになる）。
        """
        self.log_environment()
        self._log_pause()
        self._log_upcoming()

        while not self.stop_event.is_set():
            try:
                event = self._next_due_event()
            except Exception as exc:  # 常駐は継続する
                logger.exception("次の予定を求められませんでした（%d 秒後にやり直します）: %s",
                                 RETRY_SECONDS, exception_text(exc))
                self.stop_event.wait(RETRY_SECONDS)
                continue
            if event is None:
                continue

            try:
                self.run_event(event)
            except Exception as exc:  # 常駐は継続する
                # 再生済みの記録を最初に付ける（再起動後に同じ放送をやり直さないため）。
                # 履歴は付け足しなので、その後。どちらが失敗しても、ループは続ける。
                self._mark_fired_after_failure(event)
                logger.exception("イベント処理に失敗しました（継続します）: %s",
                                 exception_text(exc))
                self._record_failure_safely(event, exc)

        logger.info("システムを停止しました。")
        return 0
