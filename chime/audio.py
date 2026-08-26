"""音声再生。

再生バックエンドを 3 種類用意し、環境に応じて自動選択する。

``pygame``
    実機の標準。SDL2 mixer を明示的なパラメータ（特に buffer=4096）で初期化する。
``command``
    ``aplay`` / ``mpg123`` を呼ぶ簡易バックエンド。pygame が使えない実機向け。
``mock``
    音を出さずログのみ。WSL 等の開発環境向け。
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import time
import wave
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence

from . import env

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 実機以外では未導入が正常
    import pygame
except ImportError:  # pragma: no cover
    pygame = None


@dataclass
class Segment:
    """再生する 1 ファイル分の指示。"""

    path: str
    label: str = ""
    fade_in_ms: int = 0
    gap_after_ms: int = 0
    optional: bool = False

    def describe(self) -> str:
        return self.label or os.path.basename(self.path)


def wav_duration(path: str) -> Optional[float]:
    """WAV ファイルの長さ（秒）を返す。WAV でなければ ``None``。"""
    try:
        with contextlib.closing(wave.open(path, "rb")) as handle:
            rate = handle.getframerate()
            if not rate:
                return None
            return handle.getnframes() / float(rate)
    except (wave.Error, EOFError, OSError):
        return None


class PlaybackError(RuntimeError):
    """再生に失敗した場合に送出する。"""


class Player:
    """再生バックエンドの基底クラス。"""

    name = "base"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self.settings = dict(settings)
        self.gap_ms = int(self.settings.get("gap_ms", 0))

    # -- ライフサイクル -------------------------------------------------
    def open(self) -> None:
        """再生前の初期化。"""

    def close(self) -> None:
        """再生後の後始末。"""

    def play_one(self, segment: Segment) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    # -- 共通処理 -------------------------------------------------------
    def play(self, segments: Sequence[Segment]) -> int:
        """セグメントを順番に、間に無音を挟みながら再生する。

        戻り値は実際に再生したセグメント数。欠落した optional セグメントは
        スキップされるためカウントしない。1 件も再生できなかった場合は
        （元々セグメントが無かった場合も含め）例外を送出せず ``0`` を返す。
        """
        playable: List[Segment] = []
        for segment in segments:
            if not segment.path:
                continue
            if not os.path.exists(segment.path):
                message = f"音源ファイルが見つかりません: {segment.path}"
                if segment.optional:
                    logger.warning("%s（このセグメントはスキップします）", message)
                    continue
                raise PlaybackError(message)
            playable.append(segment)

        if not playable:
            logger.warning("再生できるセグメントがありません。")
            return 0

        try:
            self.open()
            for index, segment in enumerate(playable):
                logger.info("再生[%s]: %s", self.name, segment.describe())
                self.play_one(segment)
                gap_ms = segment.gap_after_ms if segment.gap_after_ms else self.gap_ms
                if gap_ms and index < len(playable) - 1:
                    time.sleep(gap_ms / 1000.0)
        finally:
            self.close()
        return len(playable)


class PygamePlayer(Player):
    """pygame（SDL2 mixer）による再生。"""

    name = "pygame"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.mixer_settings = dict(self.settings.get("mixer", {}))
        self._opened = False

    @staticmethod
    def available() -> bool:
        return pygame is not None

    def open(self) -> None:
        if pygame is None:  # pragma: no cover
            raise PlaybackError("pygame が導入されていません。")
        pygame.mixer.init(
            frequency=int(self.mixer_settings.get("frequency", 44100)),
            size=int(self.mixer_settings.get("size", -16)),
            channels=int(self.mixer_settings.get("channels", 2)),
            buffer=int(self.mixer_settings.get("buffer", 4096)),
        )
        self._opened = True
        logger.debug("mixer 初期化: %s", pygame.mixer.get_init())

    def close(self) -> None:
        if self._opened and pygame is not None:  # pragma: no cover - 実機のみ
            try:
                pygame.mixer.music.stop()
            finally:
                pygame.mixer.quit()
        self._opened = False

    def play_one(self, segment: Segment) -> None:  # pragma: no cover - 実機のみ
        pygame.mixer.music.load(segment.path)
        if segment.fade_in_ms > 0:
            pygame.mixer.music.play(loops=0, fade_ms=segment.fade_in_ms)
        else:
            pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)


class CommandPlayer(Player):
    """外部コマンド（aplay / mpg123）による再生。"""

    name = "command"

    def __init__(self, settings: Mapping[str, Any]) -> None:
        super().__init__(settings)
        self.commands = {
            str(key).lower(): list(value)
            for key, value in dict(self.settings.get("commands", {})).items()
        }

    def command_for(self, path: str) -> Optional[List[str]]:
        template = self.commands.get(os.path.splitext(path)[1].lower())
        if not template:
            return None
        return [part.format(path=path) for part in template]

    def available(self) -> bool:
        return any(env.has_command(parts[0]) for parts in self.commands.values() if parts)

    def play_one(self, segment: Segment) -> None:
        command = self.command_for(segment.path)
        if not command:
            raise PlaybackError(f"再生コマンドが未設定の拡張子です: {segment.path}")
        if not env.has_command(command[0]):
            raise PlaybackError(f"再生コマンドが見つかりません: {command[0]}")
        if segment.fade_in_ms:
            logger.debug("command バックエンドはフェードインに対応しません（無視します）。")
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            raise PlaybackError(
                f"再生コマンドが異常終了しました（終了コード {result.returncode}）: {' '.join(command)}"
            )


class MockPlayer(Player):
    """音を出さずログのみを出す開発用バックエンド。"""

    name = "mock"

    def play_one(self, segment: Segment) -> None:
        max_seconds = float(self.settings.get("mock_max_seconds", 3.0))
        duration = wav_duration(segment.path)
        if duration is None:
            duration = min(max_seconds, 2.0)
        logger.info("[MOCK] %s（約 %.1f 秒 / fade_in=%dms）",
                    segment.describe(), duration, segment.fade_in_ms)
        time.sleep(min(duration, max_seconds))


def create_player(settings: Mapping[str, Any], force_backend: Optional[str] = None) -> Player:
    """設定と実行環境から再生バックエンドを選ぶ。"""
    backend = (force_backend or settings.get("backend") or "auto").lower()

    if backend == "pygame":
        return PygamePlayer(settings)
    if backend == "command":
        return CommandPlayer(settings)
    if backend == "mock":
        return MockPlayer(settings)
    if backend != "auto":
        logger.warning("未知の audio.backend '%s' のため auto として扱います。", backend)

    if not env.is_production_linux():
        logger.info("開発環境と判定したため mock バックエンドを使用します。")
        return MockPlayer(settings)
    if PygamePlayer.available():
        return PygamePlayer(settings)
    command_player = CommandPlayer(settings)
    if command_player.available():
        logger.warning("pygame が見つからないため外部コマンド再生にフォールバックします。")
        return command_player
    logger.error("利用可能な再生手段がありません。mock で動作します（音は鳴りません）。")
    return MockPlayer(settings)
