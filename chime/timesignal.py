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
from typing import Any, Dict, Mapping

logger = logging.getLogger(__name__)

_MAX_AMPLITUDE = 32767


def lead_seconds(settings: Mapping[str, Any]) -> float:
    """短音の総時間（＝長音が鳴るまでの秒数）を返す。"""
    count = int(settings.get("short_pip_count", 3))
    interval_ms = float(settings.get("pip_interval_ms", 1000))
    return count * interval_ms / 1000.0


def total_seconds(settings: Mapping[str, Any]) -> float:
    """時報音全体の長さ（秒）を返す。"""
    long_ms = float(settings.get("long_pip", {}).get("duration_ms", 1000))
    return lead_seconds(settings) + long_ms / 1000.0


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


def generate_time_signal(path: str, settings: Mapping[str, Any],
                         mixer: Mapping[str, Any]) -> str:
    """時報音の WAV を生成して保存し、そのパスを返す。"""
    sample_rate = int(mixer.get("frequency", 44100))
    channels = int(mixer.get("channels", 2))
    channels = 2 if channels >= 2 else 1

    volume = float(settings.get("volume", 0.6))
    envelope_ms = float(settings.get("envelope_ms", 5))
    count = int(settings.get("short_pip_count", 3))
    interval_ms = float(settings.get("pip_interval_ms", 1000))
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

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    packed = bytearray()
    for sample in frames:
        clipped = max(-_MAX_AMPLITUDE, min(_MAX_AMPLITUDE, sample))
        packed += struct.pack("<h", clipped) * channels

    with wave.open(path, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(packed))

    logger.info("時報音を生成しました: %s (%.2f秒 / %dHz / %dch)",
                path, len(frames) / sample_rate, sample_rate, channels)
    return path


def ensure_time_signal(path: str, settings: Mapping[str, Any],
                       mixer: Mapping[str, Any], force: bool = False) -> str:
    """時報音の WAV が無ければ生成する。"""
    if force or not os.path.exists(path):
        return generate_time_signal(path, settings, mixer)
    return path


def hour_parts(hour: int, settings: Mapping[str, Any]) -> Dict[str, Any]:
    """テンプレートへ渡す 12 時間表記の部品を返す。"""
    hour = int(hour) % 24
    if hour == 0:
        period, hour12 = settings.get("period_am", "午前"), 0
    elif hour < 12:
        period, hour12 = settings.get("period_am", "午前"), hour
    elif hour == 12:
        period, hour12 = settings.get("period_pm", "午後"), 12
    else:
        period, hour12 = settings.get("period_pm", "午後"), hour - 12

    # Open JTalk（MeCab）は「4時」を「よんじ」、「7時」を「ななじ」、
    # 「9時」を「きゅうじ」、「0時」を「ぜろじ」と誤読する
    # （正しくは よじ／しちじ／くじ／れいじ）。「午後よ時」のように数字部分
    # だけをかな化すると今度は「時」が「とき」と読まれてしまうため、
    # 誤読する時刻に限り「時」を含めて丸ごとかな書きに置き換える
    # （hour_readings、既定はこの 4 つのみ）。
    # 正しく読める時刻まで一律にかな化しないのは、TTS のアクセントが
    # かえって不自然になるのを避けるため。
    hour_readings: Mapping[str, str] = settings.get("hour_readings", {}) or {}
    hour_reading = hour_readings.get(str(hour12), "{0}時".format(hour12))

    return {"period": period, "hour": hour12, "hour24": hour, "hour_reading": hour_reading}


def announce_text(hour: int, settings: Mapping[str, Any]) -> str:
    """「午前10時をお知らせしました。」のような読み上げ文言を組み立てる。

    テンプレートの ``{hour_reading}`` は誤読対策込みの時刻表現
    （例: 16 時なら「よじ」）。後方互換のため、数値のみの ``{hour}`` も
    引き続き使える（利用者が独自にテンプレートを書き換えている場合に備える）。
    """
    parts = hour_parts(hour, settings)
    if parts["hour24"] == 12 and settings.get("use_noon_template", True):
        template = settings.get("noon_template", "正午をお知らせしました。")
    else:
        template = settings.get("announce_template", "{period}{hour_reading}をお知らせしました。")
    return template.format(**parts)
