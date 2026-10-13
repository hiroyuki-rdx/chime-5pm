"""NHK 風時報の生成。

「ポ・ポ・ポ・ポーン」の音（440Hz の短音 3 回 ＋ 880Hz の長音 1 回）を
標準ライブラリだけで WAV として合成し、読み上げ文言を組み立てる。

長音の先頭が正時ちょうどに鳴るよう、短音は正時の 3 秒前から始まる。
すなわち WAV の先頭を「正時 - :func:`lead_seconds`」に再生開始する。
"""

from __future__ import annotations

import logging
import math
import os
import struct
import wave
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)

_MAX_AMPLITUDE = 32767


def _pip_timing(settings: Mapping[str, Any]) -> Tuple[int, float]:
    """``(短音の回数, 短音の間隔 ms)`` を返す。

    WAV の合成（長音までの長さ）と :func:`lead_seconds`（再生の前倒し）が
    別々に読むと、設定の読み違いで長音が正時からずれるため、同じ関数から読む。
    """
    count = int(settings.get("short_pip_count", 3))
    interval_ms = float(settings.get("pip_interval_ms", 1000))
    return count, interval_ms


def lead_seconds(settings: Mapping[str, Any]) -> float:
    """短音の総時間（＝長音が鳴るまでの秒数）を返す。"""
    count, interval_ms = _pip_timing(settings)
    return count * interval_ms / 1000.0


def _tone(frequency: float, duration_ms: float, sample_rate: int, volume: float,
          envelope_ms: float) -> list:
    """1 つのトーン（16bit モノラルサンプル列）を生成する。"""
    total = int(sample_rate * duration_ms / 1000.0)
    envelope = max(1, int(sample_rate * envelope_ms / 1000.0))
    envelope = min(envelope, max(1, total // 2))
    amplitude = _MAX_AMPLITUDE * max(0.0, min(1.0, volume))
    step = 2.0 * math.pi * frequency / sample_rate

    samples = []
    for index in range(total):
        gain = 1.0
        if index < envelope:
            gain = index / envelope
        elif index >= total - envelope:
            gain = (total - index) / envelope
        samples.append(int(amplitude * gain * math.sin(step * index)))
    return samples


def _synthesize(settings: Mapping[str, Any], sample_rate: int) -> List[int]:
    """時報音全体（短音 × 回数 ＋ 長音）のモノラルサンプル列を合成する。"""
    volume = float(settings.get("volume", 0.6))
    envelope_ms = float(settings.get("envelope_ms", 5))
    count, interval_ms = _pip_timing(settings)
    short: Dict[str, Any] = dict(settings.get("short_pip", {}))
    long: Dict[str, Any] = dict(settings.get("long_pip", {}))

    short_samples = _tone(
        float(short.get("frequency", 440.0)),
        float(short.get("duration_ms", 100)),
        sample_rate, volume, envelope_ms,
    )
    long_samples = _tone(
        float(long.get("frequency", 880.0)),
        float(long.get("duration_ms", 1000)),
        sample_rate, volume, envelope_ms,
    )

    slot = int(sample_rate * interval_ms / 1000.0)
    if len(short_samples) > slot:
        short_samples = short_samples[:slot]

    frames = []
    for _ in range(count):
        frames.extend(short_samples)
        frames.extend([0] * (slot - len(short_samples)))
    frames.extend(long_samples)
    return frames


def _write_wav(path: str, frames: List[int], sample_rate: int, channels: int) -> None:
    """モノラルのサンプル列を 16bit PCM の WAV として保存する（全チャンネル同じ音）。"""
    # abspath は必ず絶対パスを返すので、dirname が空になることは無い（相対パスの
    # ファイル名だけでも作業ディレクトリが入る）。
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    packed = bytearray()
    for sample in frames:
        clipped = max(-_MAX_AMPLITUDE, min(_MAX_AMPLITUDE, sample))
        packed += struct.pack("<h", clipped) * channels

    with wave.open(path, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(packed))


def generate_time_signal(path: str, settings: Mapping[str, Any],
                         mixer: Mapping[str, Any]) -> str:
    """時報音の WAV を生成して保存し、そのパスを返す。"""
    sample_rate = int(mixer.get("frequency", 44100))
    channels = int(mixer.get("channels", 2))
    channels = 2 if channels >= 2 else 1

    frames = _synthesize(settings, sample_rate)
    _write_wav(path, frames, sample_rate, channels)

    logger.info("時報音を生成しました: %s (%.2f秒 / %dHz / %dch)",
                path, len(frames) / sample_rate, sample_rate, channels)
    return path


def ensure_time_signal(path: str, settings: Mapping[str, Any],
                       mixer: Mapping[str, Any], force: bool = False) -> str:
    """時報音の WAV が無ければ生成する。"""
    if force or not os.path.exists(path):
        return generate_time_signal(path, settings, mixer)
    return path


def _text_setting(settings: Optional[Mapping[str, Any]], key: str) -> Any:
    """読み上げ文言まわりの設定を返す。無い（または ``null`` の）ときは既定設定の値。

    読み上げ文言は作り置きの声を**完全一致**で引くため、設定が欠けたときの予備も
    既定設定（``DEFAULT_CONFIG["time_signal"]``）と同じでなければならない。ここに
    別の言い回しを書くと、``"time_signal": null`` などで作り置きに無い文言が
    できて、その放送だけ無音になる。予備はここに書かず、既定設定から読む。
    """
    value = (settings or {}).get(key)
    if value is None:
        value = DEFAULT_CONFIG["time_signal"][key]
    return value


def hour_parts(hour: int, settings: Mapping[str, Any]) -> Dict[str, Any]:
    """テンプレートへ渡す 12 時間表記の部品を返す。"""
    hour = int(hour) % 24
    if hour < 12:
        period = _text_setting(settings, "period_am")
    else:
        period = _text_setting(settings, "period_pm")
    # 0 時は 0 のまま（12 にしない）。hour_readings の "0"（れいじ）を引くため。
    # 12 時は 12、13 時以降は 12 を引く。
    hour12 = hour if hour <= 12 else hour - 12

    # 読み上げエンジンは「4時」を「よんじ」、「7時」を「ななじ」、
    # 「9時」を「きゅうじ」、「0時」を「ぜろじ」と誤読する
    # （正しくは よじ／しちじ／くじ／れいじ）。「午後よ時」のように数字部分
    # だけをかな化すると今度は「時」が「とき」と読まれてしまうため、
    # 誤読する時刻に限り「時」を含めて丸ごとかな書きに置き換える
    # （hour_readings、既定はこの 4 つのみ）。
    # 正しく読める時刻まで一律にかな化しないのは、TTS のアクセントが
    # かえって不自然になるのを避けるため。
    hour_readings = _text_setting(settings, "hour_readings")
    if not isinstance(hour_readings, Mapping):
        hour_readings = DEFAULT_CONFIG["time_signal"]["hour_readings"]
    hour_reading = hour_readings.get(str(hour12), "{0}時".format(hour12))

    return {"period": period, "hour": hour12, "hour24": hour, "hour_reading": hour_reading}


def announce_text(hour: int, settings: Mapping[str, Any]) -> str:
    """「午前10時をお知らせしたのだ。」のような読み上げ文言を組み立てる。

    テンプレートの ``{hour_reading}`` は誤読対策込みの時刻表現
    （例: 16 時なら「よじ」）。後方互換のため、数値のみの ``{hour}`` も
    引き続き使える（利用者が独自にテンプレートを書き換えている場合に備える）。

    設定に無い（または ``null`` の）項目は、既定設定（``DEFAULT_CONFIG["time_signal"]``）
    の値で補う。``"time_signal": null`` でも、既定の文言（作り置きがある文言）になる。
    """
    parts = hour_parts(hour, settings)
    if parts["hour24"] == 12 and _text_setting(settings, "use_noon_template"):
        template = _text_setting(settings, "noon_template")
    else:
        template = _text_setting(settings, "announce_template")
    return template.format(**parts)
