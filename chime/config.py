"""設定の既定値と読み込み処理。

設定の優先順位（後勝ち）:

1. :data:`DEFAULT_CONFIG`（本モジュール。仕様上の正）
2. リポジトリ直下の ``config.json``（現地設定。Git 管理外）
3. ``--config`` で明示指定したファイル

``config.example.json`` は :data:`DEFAULT_CONFIG` をそのまま書き出したもので、
現地設定を作る際の雛形として同梱する（``tests/test_config.py`` で同期を検証）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any, Dict, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

#: リポジトリ（インストール先）のルート。
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 現地設定ファイル（Git 管理外）。
LOCAL_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

#: 現地設定の雛形（Git 管理下）。
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")


DEFAULT_CONFIG: Dict[str, Any] = {
    "timezone": "Asia/Tokyo",
    "logging": {
        "level": "INFO",
        "format": "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    },
    "schedule": {
        # 毎正時の時報（NHK 風）
        "hourly": {
            "enabled": True,
            "start_hour": 10,
            "end_hour": 16,
            "minute": 0,
            "weekdays": [0, 1, 2, 3, 4],
            "skip_hours": [],
        },
        # 閉館アナウンス＋蛍の光
        "closing": {
            "enabled": True,
            "hour": 16,
            "minute": 57,
            "weekdays": [0, 1, 2, 3, 4],
        },
        # 「ポーン」が正時ちょうどに鳴るよう、この秒数だけ前倒しで再生を開始する。
        # null なら time_signal の設定（短音の数 × 間隔）から自動計算する。
        "pip_lead_seconds": None,
        # 天気取得・音声合成をこの秒数だけ前に済ませ、再生開始を遅らせない。
        "prepare_lead_seconds": 45.0,
        # サービス再起動等で出遅れた場合、この秒数までは遅れて再生する。
        "catchup_grace_seconds": 120.0,
        # 待機ループが一度に sleep する最大秒数（NTP による時刻補正への追従用）。
        "max_sleep_seconds": 30.0,
    },
    "audio": {
        # auto: pygame → コマンド（aplay/mpg123）→ mock の順に利用可能なものを選ぶ
        "backend": "auto",
        "mixer": {
            "frequency": 44100,
            "size": -16,
            "channels": 2,
            "buffer": 4096,
        },
        # 各セグメントの間に挟む無音（ミリ秒）
        "gap_ms": 350,
        # 楽曲（蛍の光）のフェードイン（ミリ秒）
        "fade_in_ms": 2000,
        # command バックエンドで使う外部プレイヤー
        "commands": {
            ".wav": ["aplay", "-q", "{path}"],
            ".mp3": ["mpg123", "-q", "{path}"],
        },
        # mock バックエンドが 1 ファイルあたりに費やす最大秒数
        "mock_max_seconds": 3.0,
    },
    "time_signal": {
        "short_pip": {"frequency": 440.0, "duration_ms": 100},
        "long_pip": {"frequency": 880.0, "duration_ms": 1000},
        "short_pip_count": 3,
        "pip_interval_ms": 1000,
        "volume": 0.6,
        # クリックノイズ防止のためのフェード（ミリ秒）
        "envelope_ms": 5,
        "output_file": "assets/generated/time_signal.wav",
        # 「午前10時をお知らせしました。」
        "announce_template": "{period}{hour}時をお知らせしました。",
        # 12 時台のみ差し替える（NHK 準拠）
        "use_noon_template": True,
        "noon_template": "正午をお知らせしました。",
        "period_am": "午前",
        "period_pm": "午後",
    },
    "extra_segment": {
        # 時報のあとに「ひとこと」または「天気予報」を流す
        "enabled": True,
        # 天気予報を選ぶ確率（0.0〜1.0）。残りは「ひとこと」。
        "weather_probability": 0.4,
        # この時刻は必ず天気予報にする
        "always_weather_hours": [10],
        # この時刻は必ず「ひとこと」にする
        "always_quote_hours": [],
        # 天気取得に失敗したら「ひとこと」に切り替える
        "fallback_to_quote": True,
    },
    "quotes": {
        "file": "assets/quotes.json",
        # 直近この件数と同じ「ひとこと」は選ばない
        "avoid_recent": 8,
    },
    "weather": {
        "enabled": True,
        # "jma"（気象庁・キー不要）または "open_meteo"（キー不要）
        "provider": "jma",
        "timeout_seconds": 8.0,
        "cache_minutes": 60,
        "jma": {
            # 地域コード。https://www.jma.go.jp/bosai/common/const/area.json 参照
            # 既定は滋賀県。一次細分区域は「南部」（大津・草津・近江八幡など）と
            # 「北部」（彦根・長浜・米原・高島など）の 2 つがあり、既定は南部。
            # 北部に切り替える場合は area_name を "北部"、temp_area_name を
            # "彦根" にする（docs/SETUP.md 7 章を参照）。
            "area_code": "250000",
            # timeSeries 内で優先的に使う地域名（前方一致）。空なら先頭を使う。
            # 天気・降水確率の細分区域名（"南部" / "北部"）。
            "area_name": "南部",
            # 気温の timeSeries だけは観測地点名（"大津" / "彦根" など）で
            # area_name とは体系が異なるため、別に指定する。
            # 空文字列なら従来どおり area_name で選ぶ（後方互換）。
            "temp_area_name": "大津",
            "label": "滋賀",
        },
        "open_meteo": {
            "latitude": 35.0045,
            "longitude": 135.8686,
            "label": "滋賀",
        },
        "template": "{when}の{label}の天気は、{weather}。{details}",
        "details_separator": "、",
        "suffix": "です。",
    },
    "tts": {
        # 上から順に試し、失敗したら次のエンジンへフォールバックする
        "engines": ["prerecorded", "voicevox", "open_jtalk"],
        "cache_dir": "cache/tts",
        "prerecorded_dir": "assets/voice",
        "open_jtalk": {
            "binary": "open_jtalk",
            # 空文字なら既知の場所から自動検出する
            "dictionary": "",
            "voice": "",
            "sampling_frequency": 48000,
            "speed": 1.0,
            "additional_half_tone": 0.0,
            "volume_gain_db": 0.0,
        },
        "voicevox": {
            "base_url": "http://127.0.0.1:50021",
            # 3 = ずんだもん（ノーマル）
            "speaker": 3,
            "timeout_seconds": 20.0,
        },
    },
    "closing": {
        "announce_file": "assets/announce.wav",
        "music_file": "assets/hotaru.mp3",
        # 空文字ならアナウンス音声ファイルのみ。文字列を入れると TTS で追加読み上げ。
        "extra_text": "",
    },
    "state": {
        "file": "cache/state.json",
    },
}


class ConfigError(RuntimeError):
    """設定ファイルが読めない・壊れている場合に送出する。"""


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """辞書を再帰的にマージした新しい辞書を返す（``override`` が優先）。

    戻り値は ``base``／``override`` のどの階層とも参照を共有しない。
    ``override`` で触れられなかった枝を呼び出し側が書き換えても、
    元の ``base``（ひいては :data:`DEFAULT_CONFIG`）に影響しない。
    """
    merged: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class Config:
    """ドット記法でアクセスできる設定オブジェクト。"""

    def __init__(self, data: Mapping[str, Any], base_dir: str = BASE_DIR,
                 sources: Optional[Iterable[str]] = None) -> None:
        self._data: Dict[str, Any] = copy.deepcopy(dict(data))
        self.base_dir = base_dir
        self.sources = list(sources or [])

    # ------------------------------------------------------------------
    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    def get(self, path: str, default: Any = None) -> Any:
        """``"weather.jma.area_code"`` のようなドット区切りで値を取得する。"""
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, path: str) -> Dict[str, Any]:
        """辞書セクションを取得する（存在しなければ空辞書）。"""
        value = self.get(path, {})
        return dict(value) if isinstance(value, Mapping) else {}

    def path(self, path: str, default: str = "") -> str:
        """設定値を絶対パスとして解決する（相対パスは ``base_dir`` 起点）。

        キー自体が存在しない場合のみ ``default`` を使う。空文字列が明示的に
        設定されている場合は「未設定」の意味でそのまま尊重する
        （``or default`` にすると意図的な空文字列まで ``default`` に化けてしまう）。
        """
        value = self.get(path, default)
        if value is None:
            value = default
        return self.resolve(str(value))

    def resolve(self, value: str) -> str:
        """相対パスを ``base_dir`` 起点の絶対パスへ変換する。"""
        if not value:
            return ""
        expanded = os.path.expanduser(value)
        if os.path.isabs(expanded):
            return expanded
        return os.path.normpath(os.path.join(self.base_dir, expanded))


def load_config(explicit_path: Optional[str] = None, base_dir: str = BASE_DIR) -> Config:
    """既定値・現地設定・明示指定ファイルをマージして :class:`Config` を返す。"""
    data: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
    sources = ["<defaults>"]

    candidates = []
    local_path = os.path.join(base_dir, "config.json")
    if os.path.exists(local_path):
        candidates.append(local_path)
    if explicit_path:
        candidates.append(explicit_path)

    for candidate in candidates:
        data = deep_merge(data, _read_json(candidate))
        sources.append(candidate)

    return Config(data, base_dir=base_dir, sources=sources)


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"設定ファイルが見つかりません: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"設定ファイルの JSON が不正です: {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"設定ファイルのトップレベルはオブジェクトである必要があります: {path}")
    return loaded


def dump_default_config(path: str) -> None:
    """:data:`DEFAULT_CONFIG` を JSON として書き出す（雛形生成用）。"""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(DEFAULT_CONFIG, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
