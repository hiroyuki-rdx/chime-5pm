"""音声再生。

再生バックエンドを 3 種類用意し、環境に応じて自動選択する。

``pygame``
    実機の標準。SDL2 mixer を明示的なパラメータ（特に buffer=4096）で初期化する。
``command``
    ``aplay`` / ``mpg123`` を呼ぶ簡易バックエンド。pygame が使えない実機向け。
``mock``
    音を出さずログのみ。WSL 等の開発環境向け。

どのバックエンドも、``play(segments, stop=...)`` に渡した :class:`threading.Event` が
立てば再生を打ち切り（:class:`PlaybackInterrupted`）、1 つの音源が長く鳴り続けても
上限の秒数で打ち切る（:class:`PlaybackError`）。音量は ``audio.volume`` を音源の種類
（:attr:`Segment.kind`）ごとに引く（:func:`volume_for`）。何も指定しなければ従来どおり動く。

``command`` は、再生コマンドを自分のプロセスグループ（セッション）で起動し、止めるときは
グループごと止める。``sh -c "aplay ..."`` のようなラッパーでも、孫の再生コマンドが
残って音声デバイスを握りつづけることはない。SIGTERM を送ったあと、猶予のうちにグループが
空にならなければ SIGKILL を送る（ラッパーが先に終わって、SIGTERM を無視する孫だけが
残った場合も同じ）。
"""

from __future__ import annotations

import logging
import math
import os
import signal
import subprocess
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Sequence, Set

from . import env

logger = logging.getLogger(__name__)

# pygame は import しただけで、標準出力に版数と宣伝のバナーを出す。
# ``--print-config`` などの JSON 出力に混ざって壊すので、import の前に消しておく。
# 利用者が自分で設定していれば、その値を尊重する（上書きしない）。
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

try:  # pragma: no cover - 実機以外では未導入が正常
    import pygame
except ImportError:  # pragma: no cover
    pygame = None


#: 音源の種類。``audio.volume`` のキーと同じ名前（音量は種類ごとに設定する）。
KIND_TIME_SIGNAL = "time_signal"
KIND_VOICE = "voice"
KIND_MUSIC = "music"
SEGMENT_KINDS = (KIND_TIME_SIGNAL, KIND_VOICE, KIND_MUSIC)

#: ``audio.mixer`` の初期化を試す回数と、失敗したときに次を試すまでの秒数。
MIXER_INIT_ATTEMPTS = 3
MIXER_INIT_RETRY_SECONDS = 1.0
#: 再生が終わったか・停止要求が来たかを確かめる間隔（秒）。
POLL_SECONDS = 0.05
#: 停止要求を受けたときのフェードアウト（ミリ秒）。``fadeout()`` が終わるまで戻らない版でも、
#: すぐ戻る版でも、鳴り止むまで ``get_busy()`` で待つ（途中で ``music.stop()`` が切らないように）。
STOP_FADEOUT_MS = 300
#: そのフェードアウトの待ちに足す余裕（ミリ秒）。待ちの上限は ``STOP_FADEOUT_MS`` + この値。
STOP_FADEOUT_MARGIN_MS = 100
#: WAV の長さに足す余裕（秒）。この時間を過ぎても鳴り終わらなければ打ち切る。
WAV_TIMEOUT_MARGIN_SECONDS = 10.0
#: WAV 以外の音源に使う打ち切りの既定値（``audio.max_segment_seconds``）。
DEFAULT_MAX_SEGMENT_SECONDS = 600.0
#: command 方式で、停止・打ち切りの SIGTERM から SIGKILL に切り替えるまでの猶予（秒）。
TERMINATE_GRACE_SECONDS = 2.0

#: 「プロセスで 1 回だけ」警告する項目（同じ設定ミスを毎時間ログに出さないため）。
_warned: Set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    """``key`` ごとに、プロセスで 1 回だけ WARNING を出す。"""
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


@dataclass
class Segment:
    """再生する 1 ファイル分の指示。

    ``kind`` は音量の区分（``time_signal`` / ``voice`` / ``music``）。既定の
    ``voice`` は読み上げ（作り置きの音声と閉館アナウンス）で、時報音と蛍の光は
    組み立て側（:mod:`chime.sequence`）が明示する。
    """

    path: str
    label: str = ""
    fade_in_ms: int = 0
    optional: bool = False
    kind: str = KIND_VOICE

    def describe(self) -> str:
        return self.label or os.path.basename(self.path)


def wav_duration(path: str) -> Optional[float]:
    """WAV ファイルの長さ（秒）を返す。WAV でなければ・読めなければ ``None``（長さ不明）。

    例外は出さない。壊れたヘッダーは ``wave.Error`` のほか ``RuntimeError`` などにもなるため、
    ``Exception`` を広く受ける（長さが分からないことは、再生を止める理由にならない）。
    """
    try:
        with wave.open(path, "rb") as handle:
            rate = handle.getframerate()
            if not rate:
                return None
            return handle.getnframes() / float(rate)
    except Exception:
        return None


class PlaybackError(RuntimeError):
    """再生に失敗した場合に送出する。"""


class PlaybackInterrupted(PlaybackError):
    """停止要求（``stop``）で再生を打ち切ったときに送出する。

    失敗ではなく意図した中断だが、呼び出し側が ``PlaybackError`` だけを受けていても
    落ちないよう、その子クラスにしてある。``played`` は中断までに鳴り終えた
    セグメントの数（``Player.play`` の戻り値と同じ数え方）。

    ``started`` は、音を出し始めたか。``False`` は、何も鳴らさないうちに止まった（デバイスを
    開く前、または最初のセグメントを鳴らす呼び出し〔``music.play()`` やコマンドの起動〕の前）。
    既定は ``True``（分からないときは、音が出たものとして扱う）。再生側が、音を出す前だと
    分かっている場所だけ ``False`` にする。
    """

    def __init__(self, message: str = "再生を中断しました。", played: int = 0,
                 started: bool = True) -> None:
        super().__init__(message)
        self.played = played
        self.started = started


def _as_number(value: Any) -> Optional[float]:
    """JSON の数（``bool`` と NaN を除く）を ``float`` にする。数でなければ ``None``。

    ``float`` に直せない巨大な整数（``OverflowError``）は、符号を残して ±無限大にする
    （``-1`` や ``-inf`` が 0.0 になるのに、``-10**400`` だけ別の扱いにならないように）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return -math.inf if value < 0 else math.inf
    return None if math.isnan(number) else number


def volume_for(segment: Segment, audio_settings: Mapping[str, Any]) -> float:
    """``segment`` の音量（0.0〜1.0）を ``audio.volume`` から引く。例外は出さない。

    キーが無い・数でない・NaN のときは 1.0（今までの音量）。範囲外は 0〜1 に丸める。
    どちらも設定の間違いなので、キーごとにプロセスで 1 回だけ WARNING を出す。
    放送は止めない（音量の設定ミスで時報が鳴らなくなることはない）。
    """
    key = "audio.volume.{0}".format(segment.kind)
    volumes = audio_settings.get("volume") if isinstance(audio_settings, Mapping) else None
    if not isinstance(volumes, Mapping) or segment.kind not in volumes:
        _warn_once(key, "%s が設定されていません。1.0 として扱います。", key)
        return 1.0
    raw = volumes[segment.kind]
    number = _as_number(raw)
    if number is None:
        _warn_once(key, "%s の値 %r を解釈できません（0〜1 の数）。1.0 として扱います。", key, raw)
        return 1.0
    if not 0.0 <= number <= 1.0:
        clamped = max(0.0, min(1.0, number))
        _warn_once(key, "%s = %r は範囲外です（0〜1）。%s に丸めます。", key, raw, clamped)
        return clamped
    return number


def max_segment_seconds(audio_settings: Mapping[str, Any]) -> float:
    """``audio.max_segment_seconds``（WAV 以外の音源の打ち切り秒数）。例外は出さない。

    キーが無ければ既定の 600 秒。数でない・0 以下・無限大は設定の間違いなので、
    600 秒として扱い、プロセスで 1 回だけ WARNING を出す。
    """
    if "max_segment_seconds" not in audio_settings:
        return DEFAULT_MAX_SEGMENT_SECONDS
    raw = audio_settings["max_segment_seconds"]
    number = _as_number(raw)
    if number is None or number <= 0 or math.isinf(number):
        _warn_once("audio.max_segment_seconds",
                   "audio.max_segment_seconds の値 %r を解釈できません（正の数）。%d 秒として扱います。",
                   raw, DEFAULT_MAX_SEGMENT_SECONDS)
        return DEFAULT_MAX_SEGMENT_SECONDS
    return number


def segment_time_limit(segment: Segment, audio_settings: Mapping[str, Any]) -> float:
    """1 つの音源を鳴らしてよい最大の秒数。

    WAV は長さ（``wave`` で読む）に 10 秒を足した値。長さが分からない音源（mp3 など、
    また壊れた WAV）は ``audio.max_segment_seconds``。
    """
    duration = wav_duration(segment.path)
    if duration is not None:
        return duration + WAV_TIMEOUT_MARGIN_SECONDS
    return max_segment_seconds(audio_settings)


class Player:
    """再生バックエンドの基底クラス。

    ``sleep`` / ``clock`` は待ち時間と時計の差し替え口（テスト用）。指定しなければ
    ``time.sleep`` / ``time.monotonic``（呼ぶたびに引くので、差し替えにも従う）。
    """

    name = "base"

    def __init__(self, settings: Mapping[str, Any], *,
                 sleep: Optional[Callable[[float], Any]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.settings = dict(settings)
        self.gap_ms = int(self.settings.get("gap_ms", 0))
        self._sleep = sleep
        self._clock = clock
        #: ``play`` の間だけ入る停止要求。``play_one`` はこれを見て中断する。
        self._stop: Optional[threading.Event] = None
        #: この ``play`` で、音を出し始めたか（:class:`PlaybackInterrupted` の ``started``）。
        self._started = False

    # -- ライフサイクル -------------------------------------------------
    def open(self) -> None:
        """再生前の初期化。"""

    def close(self) -> None:
        """再生後の後始末。"""

    def play_one(self, segment: Segment) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    # -- 待機と停止要求 --------------------------------------------------
    def _stopped(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    def _now(self) -> float:
        return (self._clock or time.monotonic)()

    def _wait(self, seconds: float) -> bool:
        """``seconds`` 秒待つ。待つあいだに停止要求が立てば、すぐ戻って ``True``。

        停止要求を渡されていなければ、従来どおり ``time.sleep`` で待つだけ。
        """
        if self._sleep is not None:
            self._sleep(seconds)
            return self._stopped()
        if self._stop is None:
            time.sleep(seconds)
            return False
        return self._stop.wait(seconds)

    def _pause(self, seconds: float) -> None:
        """停止要求とは関係なく ``seconds`` 秒待つ（差し替えた ``sleep`` があればそれで）。

        停止要求が立ったあとの後始末の待ちに使う。:meth:`_wait` は、立っている停止要求で
        すぐ戻ってしまい、待ちにならない。
        """
        (self._sleep or time.sleep)(seconds)

    def _mark_started(self) -> None:
        """音を出し始める（その呼び出しに入る）ことを記録する。以後の中断は ``started=True``。"""
        self._started = True

    def _interrupted(self, what: str) -> PlaybackInterrupted:
        """停止要求による中断。``started`` は、ここまでに音を出し始めたか。"""
        return PlaybackInterrupted("停止要求のため再生を中断しました: {0}".format(what),
                                   started=self._started)

    def _wait_for_finish(self, segment: Segment, is_busy: Callable[[], bool], limit: float,
                         on_interrupt: Callable[[], None],
                         on_timeout: Callable[[], None],
                         nap: Optional[Callable[[float], Any]] = None) -> None:
        """``is_busy()`` が偽になる（鳴り終わる）まで待つ。

        待つあいだ、停止要求が立てば ``on_interrupt()`` で音を止めて
        :class:`PlaybackInterrupted` を、``limit`` 秒を過ぎれば ``on_timeout()`` で
        止めて :class:`PlaybackError`（音源のパス入り）を送出する。

        確認の間隔（:data:`POLL_SECONDS`）は ``nap(秒)`` で待つ。既定は :meth:`_wait`。
        """
        nap = nap or self._wait
        deadline = self._now() + limit
        while is_busy():
            if self._stopped():
                on_interrupt()
                raise self._interrupted(segment.describe())
            if self._now() >= deadline:
                on_timeout()
                raise PlaybackError(
                    "再生が終わらないため打ち切りました（上限 {0:g} 秒）: {1}".format(limit, segment.path))
            nap(POLL_SECONDS)

    # -- 共通処理 -------------------------------------------------------
    @staticmethod
    def _select_playable(segments: Sequence[Segment]) -> List[Segment]:
        """再生できるセグメントだけを残す。

        パスが空のものは無視する。音源が見つからないとき、optional は警告を
        出してスキップし、必須は ``PlaybackError`` にする。再生の前に全件を
        確かめるので、後ろに必須の欠落があれば手前も鳴らさず、デバイスも開かない。
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
        return playable

    def play(self, segments: Sequence[Segment], stop: Optional[threading.Event] = None) -> int:
        """セグメントを順番に、間に無音を挟みながら再生する。

        戻り値は実際に再生したセグメント数。欠落した optional セグメントは
        スキップされるためカウントしない。1 件も再生できなかった場合は
        （元々セグメントが無かった場合も含め）例外を送出せず ``0`` を返す。

        ``stop`` が立つと、鳴らしている音を止め（pygame は 0.3 秒でフェードアウト）、
        セグメントの間隔の待ちも打ち切って、:class:`PlaybackInterrupted` を送出する
        （``played`` に鳴り終えた数、``started`` に音を出し始めたか）。``None`` なら止めない。
        """
        playable = self._select_playable(segments)
        if not playable:
            logger.warning("再生できるセグメントがありません。")
            return 0

        self._started = False
        if stop is not None and stop.is_set():
            raise self._interrupted("再生の開始前")  # デバイスは開かない
        self._stop = stop
        completed = 0
        try:
            self.open()
            for index, segment in enumerate(playable):
                if self._stopped():
                    raise self._interrupted(segment.describe())
                logger.info("再生[%s]: %s", self.name, segment.describe())
                self.play_one(segment)
                self._mark_started()  # 自分で印を付けないバックエンドも、鳴らし終えれば出した扱い
                completed = index + 1
                if self.gap_ms and index < len(playable) - 1:
                    if self._wait(self.gap_ms / 1000.0):
                        raise self._interrupted("セグメントの間隔")
        except PlaybackInterrupted as exc:
            exc.played = completed
            raise
        finally:
            self._stop = None
            self.close()
        return len(playable)


class PygamePlayer(Player):
    """pygame（SDL2 mixer）による再生。

    ``mixer_module`` は ``pygame.mixer`` の差し替え口（テスト用）。既定は
    ``pygame.mixer``（import 済みの ``pygame`` から呼ぶたびに引く）。
    """

    name = "pygame"

    def __init__(self, settings: Mapping[str, Any], mixer_module: Any = None, *,
                 sleep: Optional[Callable[[float], Any]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        super().__init__(settings, sleep=sleep, clock=clock)
        self.mixer_settings = dict(self.settings.get("mixer", {}))
        self._mixer_module = mixer_module
        self._opened = False

    @staticmethod
    def available() -> bool:
        return pygame is not None

    def _mixer(self) -> Any:
        if self._mixer_module is not None:
            return self._mixer_module
        if pygame is None:  # pragma: no cover
            raise PlaybackError("pygame が導入されていません。")
        return pygame.mixer

    def open(self) -> None:
        mixer = self._mixer()
        params = dict(
            frequency=int(self.mixer_settings.get("frequency", 44100)),
            size=int(self.mixer_settings.get("size", -16)),
            channels=int(self.mixer_settings.get("channels", 2)),
            buffer=int(self.mixer_settings.get("buffer", 4096)),
        )
        for attempt in range(1, MIXER_INIT_ATTEMPTS + 1):
            try:
                mixer.init(**params)
                break
            except Exception as exc:
                reason = "{0}: {1}".format(type(exc).__name__, exc)
                if attempt == MIXER_INIT_ATTEMPTS:
                    raise PlaybackError(
                        "音声デバイスを初期化できませんでした（{0} 回試しました）: {1}".format(
                            MIXER_INIT_ATTEMPTS, reason)) from exc
                logger.warning("音声デバイスの初期化に失敗しました（%d/%d 回目）: %s。%g 秒後にやり直します。",
                               attempt, MIXER_INIT_ATTEMPTS, reason, MIXER_INIT_RETRY_SECONDS)
                if self._wait(MIXER_INIT_RETRY_SECONDS):
                    raise self._interrupted("音声デバイスの初期化")
        self._opened = True
        logger.debug("mixer 初期化: %s", mixer.get_init())

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        mixer = self._mixer()
        # stop が失敗しても quit は必ず呼ぶ。どちらの失敗も送出せず記録だけにする（再生の
        # 結果〔中断・打ち切り・成功〕を、後始末の例外で隠さないため）。
        self._quietly(mixer.music.stop, "再生の停止")
        self._quietly(mixer.quit, "mixer の終了")

    @staticmethod
    def _apply_volume(music: Any, volume: float) -> None:
        """音量を設定する。設定できなくても鳴らす（音量は鳴らすことより大事ではない）。"""
        try:
            music.set_volume(volume)
        except Exception as exc:
            _warn_once("pygame-set-volume", "音量を設定できませんでした（そのまま再生します）: %s: %s",
                       type(exc).__name__, exc)

    @staticmethod
    def _quietly(action: Callable[[], Any], what: str) -> bool:
        """止める操作が失敗しても、呼び出し側の例外（中断・打ち切り）を優先する。成功したか。"""
        try:
            action()
        except Exception as exc:
            logger.warning("%sに失敗しました: %s: %s", what, type(exc).__name__, exc)
            return False
        return True

    def _fade_out(self, music: Any) -> None:
        """停止要求で、音をフェードアウトさせ、鳴り止むまで待つ。

        ``fadeout()`` が終わるまで戻る版なら、戻った時点で鳴り止んでいる。すぐ戻る版でも、
        後始末の ``music.stop()`` がフェードを途中で切らないよう、``get_busy()`` が偽になるまで
        待つ。待つのは、``fadeout()`` を呼ぶ前から数えて最長 ``STOP_FADEOUT_MS`` +
        ``STOP_FADEOUT_MARGIN_MS`` ミリ秒（停止にかかる時間を抑える）。フェードアウトできな
        かった・``get_busy()`` が失敗したときは、待たずに止める（失敗は記録するだけ）。
        """
        give_up = self._now() + (STOP_FADEOUT_MS + STOP_FADEOUT_MARGIN_MS) / 1000.0
        if not self._quietly(lambda: music.fadeout(STOP_FADEOUT_MS), "フェードアウト"):
            return
        while True:
            try:
                busy = bool(music.get_busy())
            except Exception as exc:
                logger.warning("フェードアウトの終わりを確かめられませんでした: %s: %s",
                               type(exc).__name__, exc)
                return
            remaining = give_up - self._now()
            if not busy or remaining <= 0:
                return
            self._pause(min(POLL_SECONDS, remaining))

    def play_one(self, segment: Segment) -> None:
        music = self._mixer().music
        # 打ち切りの秒数は、鳴らし始める前に決める（鳴らし始めたあとで例外を出して、
        # 放送を途中で投げ出さないため）。
        limit = segment_time_limit(segment, self.settings)
        music.load(segment.path)
        # load すると音量が戻るので、load の後・play の前に設定する。
        self._apply_volume(music, volume_for(segment, self.settings))
        if self._stopped():  # load〜play の間に来た停止要求では、鳴らし始めない
            raise self._interrupted(segment.describe())
        self._mark_started()
        if segment.fade_in_ms > 0:
            music.play(loops=0, fade_ms=segment.fade_in_ms)
        else:
            music.play()
        self._wait_for_finish(
            segment, music.get_busy, limit,
            on_interrupt=lambda: self._fade_out(music),
            on_timeout=lambda: self._quietly(music.stop, "再生の停止"))


class CommandPlayer(Player):
    """外部コマンド（aplay / mpg123）による再生。"""

    name = "command"

    def __init__(self, settings: Mapping[str, Any], *,
                 sleep: Optional[Callable[[float], Any]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        super().__init__(settings, sleep=sleep, clock=clock)
        self.commands = {
            str(key).lower(): list(value)
            for key, value in dict(self.settings.get("commands", {})).items()
        }
        #: SIGTERM を送ってから、グループが空になるのを待つ猶予（秒）。過ぎたら SIGKILL に切り替える。
        self.terminate_grace_seconds = TERMINATE_GRACE_SECONDS

    def command_for(self, path: str) -> Optional[List[str]]:
        template = self.commands.get(os.path.splitext(path)[1].lower())
        if not template:
            return None
        return [part.format(path=path) for part in template]

    def available(self) -> bool:
        return any(env.has_command(parts[0]) for parts in self.commands.values() if parts)

    @staticmethod
    def _signal(process: "subprocess.Popen[Any]", signum: int, direct: Callable[[], Any]) -> bool:
        """動いている子に ``signum`` を送る。子が起こした孫にも届くよう、グループごと送る。

        子は ``start_new_session=True`` で起動してあり、自分がグループの先頭（グループ ID は
        子の pid）。グループに送れなければ（``OSError``、``killpg`` が無い環境）、子の pid だけに
        ``direct()`` で送る。子がもう終わって回収済みなら何も送らない（その pid は別の
        プロセスに再利用されているかもしれない）。グループに送れたら ``True``。
        """
        if process.poll() is not None:
            return False
        if CommandPlayer._signal_group(process.pid, signum):
            return True
        direct()
        return False

    @staticmethod
    def _signal_group(pgid: int, signum: int) -> bool:
        """グループ ``pgid`` に ``signum`` を送る。送れたら ``True``（``killpg`` が無い・失敗は ``False``）。"""
        killpg = getattr(os, "killpg", None)
        if killpg is None:
            return False
        try:
            killpg(pgid, signum)
        except OSError:
            return False
        return True

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        """グループ ``pgid`` に、まだ生きている（回収されていないゾンビも含む）プロセスがいるか。

        信号 0 は、何も送らずに宛先があるかだけを確かめる。宛先が無ければ ``ProcessLookupError``。
        確かめられないとき（権限がない、など）は、待っても分からないので「いない」とする。
        """
        try:
            os.killpg(pgid, 0)
        except OSError:
            return False
        return True

    def _wait_for_group(self, pgid: int, deadline: float) -> bool:
        """グループが空になるか ``deadline`` になるまで待つ。``deadline`` でも残っていれば ``True``。

        ``POLL_SECONDS`` ごとに確かめる。待ち終えたあとの SIGKILL は、まだメンバーがいるグループにだけ
        送る（メンバーがいるあいだ、そのグループ ID はカーネルが別のプロセスに割り当てない）。
        """
        while self._group_alive(pgid):
            remaining = deadline - self._now()
            if remaining <= 0:
                return True
            self._pause(min(POLL_SECONDS, remaining))
        return False

    def _terminate(self, process: "subprocess.Popen[Any]") -> None:
        """子（と、子が起こした孫）を止める。

        グループに SIGTERM を送り、猶予（``terminate_grace_seconds``）のうちに、子が終わり、グループが
        空になるのを待つ。猶予が過ぎても残っていれば、グループに SIGKILL を送る。子（ラッパー）が
        SIGTERM で先に終わっても、SIGTERM を無視する孫が残っていれば同じ（孫だけが残って音声
        デバイスを握りつづけない）。最後は ``wait`` で子を回収する（ゾンビを残さない）。

        グループに送れなかったとき（``killpg`` が無い・失敗）は、子だけを相手にする。
        """
        grouped = self._signal(process, signal.SIGTERM, process.terminate)
        deadline = self._now() + self.terminate_grace_seconds
        try:
            process.wait(timeout=self.terminate_grace_seconds)  # 子を回収する
        except subprocess.TimeoutExpired:
            pass
        else:
            if not grouped or not self._wait_for_group(process.pid, deadline):
                return
        if not (grouped and self._signal_group(process.pid, signal.SIGKILL)):
            if process.poll() is None:
                process.kill()
        process.wait()

    def _wait_for_exit(self, process: "subprocess.Popen[Any]", seconds: float) -> None:
        """子が終わるか ``seconds`` 秒たつまで待つ。子が終わればその場で戻る。

        ``poll`` して ``sleep`` する作りだと、終わった子に最大 ``seconds`` 秒遅れて気づき、
        短いコマンドを並べたときに 1 つごとに無音が挟まる。``sleep`` を差し替えてあれば
        （テスト用）、それで待つ。
        """
        if self._sleep is not None:
            self._sleep(seconds)
            return
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            pass

    def play_one(self, segment: Segment) -> None:
        command = self.command_for(segment.path)
        if not command:
            raise PlaybackError(f"再生コマンドが未設定の拡張子です: {segment.path}")
        if not env.has_command(command[0]):
            raise PlaybackError(f"再生コマンドが見つかりません: {command[0]}")
        if segment.fade_in_ms:
            logger.debug("command バックエンドはフェードインに対応しません（無視します）。")
        if volume_for(segment, self.settings) != 1.0:
            _warn_once("command-volume",
                       "command 方式では音量を変えられません（audio.volume は無視して、そのままの音量で鳴らします）。")
        limit = segment_time_limit(segment, self.settings)  # 起動の前に決める
        try:
            # 自分のセッション（プロセスグループ）で起動する。ラッパー（``sh -c "aplay ..."``）の
            # 孫まで、グループごと止められる。
            process = subprocess.Popen(command, start_new_session=True)
        except OSError as exc:
            raise PlaybackError(
                f"再生コマンドを起動できませんでした（{type(exc).__name__}: {exc}）: {' '.join(command)}"
            ) from exc
        self._mark_started()
        try:
            self._wait_for_finish(
                segment, lambda: process.poll() is None, limit,
                on_interrupt=lambda: self._terminate(process),
                on_timeout=lambda: self._terminate(process),
                nap=lambda seconds: self._wait_for_exit(process, seconds))
        finally:
            if process.poll() is None:  # 想定外の例外で抜けるときも、子を鳴らしっぱなしにしない
                self._terminate(process)
        if process.returncode != 0:
            if self._stopped():
                # systemd（control-group の kill）や Ctrl+C は、python と子に同じ SIGTERM を
                # 送る。子が先に死んでも、止めようとしていたのだから失敗ではなく中断。
                raise self._interrupted(segment.describe())
            raise PlaybackError(
                f"再生コマンドが異常終了しました（終了コード {process.returncode}）: {' '.join(command)}"
            )


class MockPlayer(Player):
    """音を出さずログのみを出す開発用バックエンド。"""

    name = "mock"

    def play_one(self, segment: Segment) -> None:
        self._mark_started()  # 呼ばれた時点で、停止要求は立っていない（play が先に確かめる）
        max_seconds = float(self.settings.get("mock_max_seconds", 3.0))
        duration = wav_duration(segment.path)
        if duration is None:
            duration = min(max_seconds, 2.0)
        volume = volume_for(segment, self.settings)
        if volume == 1.0:
            logger.info("[MOCK] %s（約 %.1f 秒 / fade_in=%dms）",
                        segment.describe(), duration, segment.fade_in_ms)
        else:
            logger.info("[MOCK] %s（約 %.1f 秒 / fade_in=%dms / 音量 %.2f）",
                        segment.describe(), duration, segment.fade_in_ms, volume)
        if self._wait(min(duration, max_seconds)):
            raise self._interrupted(segment.describe())


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
