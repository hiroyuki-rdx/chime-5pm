"""アプリケーション本体（各サービスの組み立てと常駐ループ）。"""

from __future__ import annotations

import logging
import random
import signal
import threading
from datetime import datetime
from typing import Optional

from . import env, timesignal
from .audio import PlaybackError, Player, create_player
from .config import Config
from .quotes import QuotePicker
from .scheduler import Event, Scheduler, format_events
from .sequence import PlaybackPlan, SequenceBuilder
from .state import State
from .tts import TTSService
from .weather import WeatherService

logger = logging.getLogger(__name__)

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 3.9 未満は非対応
    ZoneInfo = None  # type: ignore


class ChimeApp:
    """設定から各サービスを組み立て、常駐ループを回す。"""

    def __init__(self, config: Config, backend: Optional[str] = None,
                 dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.tzinfo = self._resolve_timezone(config.get("timezone", "Asia/Tokyo"))
        self.stop_event = threading.Event()

        self.state = State(config.path("state.file"))
        self.tts = TTSService(
            config.section("tts"),
            config.base_dir,
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
            self.time_signal_path, random.Random(),
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
        self._backend = backend
        self._player: Optional[Player] = None

    # ------------------------------------------------------------------
    @property
    def player(self) -> Player:
        """再生バックエンド（初回参照時に決定する）。"""
        if self._player is None:
            self._player = create_player(self.config.section("audio"), self._backend)
        return self._player

    @staticmethod
    def _resolve_timezone(name: str):
        if ZoneInfo is None:  # pragma: no cover
            logger.warning("zoneinfo が使えないため、OS のローカル時刻を使用します。")
            return None
        try:
            return ZoneInfo(str(name))
        except Exception as exc:  # pragma: no cover - tzdata 欠落時のみ
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
        info = env.describe()
        logger.info("実行環境: %s %s (%s) / Python %s / WSL=%s",
                    info["system"], info["release"], info["machine"],
                    info["python"], info["wsl"])
        logger.info("再生バックエンド: %s / TTS: %s",
                    self.player.name, self.tts.describe())
        logger.info("設定ソース: %s", " → ".join(self.config.sources))

    # -- 再生 -----------------------------------------------------------
    def play(self, plan: PlaybackPlan) -> None:
        """プランを再生する（``--dry-run`` の場合はログのみ）。"""
        logger.info(plan.describe())
        if self.dry_run:
            logger.info("dry-run のため再生しません。")
            return
        try:
            self.player.play(plan.segments)
        except PlaybackError as exc:
            logger.error("再生に失敗しました: %s", exc)
        except Exception as exc:  # pragma: no cover - 再生失敗でプロセスは落とさない
            logger.exception("再生中に予期しないエラーが発生しました: %s", exc)
        else:
            logger.info("再生シーケンスが完了しました。")

    def run_event(self, event: Event) -> None:
        """イベント 1 件を準備・再生し、再生済みとして記録する。"""
        logger.info("イベント準備: %s", event.describe())
        plan = self.builder.build(event)

        if not self.scheduler.sleep_until(event.play_at, self.stop_event, precise=True):
            logger.info("停止要求のため再生を中止しました: %s", event.describe())
            return

        logger.info("再生開始: %s", event.describe())
        self.play(plan)
        self.state.mark_fired(event.key, event.day)

    def run_forever(self) -> int:
        """常駐ループ。"""
        self.log_environment()
        upcoming = self.scheduler.upcoming(limit=5)
        logger.info("次回以降の予定:\n%s", format_events(upcoming))

        while not self.stop_event.is_set():
            event = self.scheduler.next_event(
                is_fired=lambda candidate: self.state.is_fired(candidate.key, candidate.day))
            if event is None:
                logger.warning("予定されたイベントがありません。60 秒後に再確認します。")
                if self.stop_event.wait(60):
                    break
                continue

            if not self.scheduler.sleep_until(event.prepare_at, self.stop_event):
                break

            # 待機中に日付や時刻が大きく動いた場合に備え、対象イベントを再確認する。
            current = self.scheduler.next_event(
                is_fired=lambda candidate: self.state.is_fired(candidate.key, candidate.day))
            if current is None or current.key != event.key or current.at != event.at:
                logger.info("待機中に予定が変わりました。再計算します。")
                continue

            try:
                self.run_event(event)
            except Exception as exc:  # pragma: no cover - 常駐は継続する
                logger.exception("イベント処理に失敗しました（継続します）: %s", exc)
                self.state.mark_fired(event.key, event.day)

        logger.info("システムを停止しました。")
        return 0
