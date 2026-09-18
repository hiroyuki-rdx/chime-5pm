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
from typing import Any, Dict, Iterable, List, Mapping, Optional

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
        # 「午前10時をお知らせしました。」（{hour_reading} は下記 hour_readings 参照）
        "announce_template": "{period}{hour_reading}をお知らせしたのだ。",
        # 12 時台のみ差し替える（NHK 準拠）
        "use_noon_template": True,
        "noon_template": "正午をお知らせしたのだ。",
        "period_am": "午前",
        "period_pm": "午後",
        # 読み上げエンジンが誤読する時刻（12 時間表記の「時」）だけ、読みを
        # かな書きで上書きする。キーは文字列（JSON の都合上）。既定の 4 つ以外は
        # 正しく読めるため、意図的に上書きしていない。この文字列は作り置き音声を
        # 引く照合キーそのものなので、変えると作り置きが外れる
        # （詳細は chime/timesignal.py 参照）。
        "hour_readings": {"0": "れいじ", "4": "よじ", "7": "しちじ", "9": "くじ"},
    },
    "extra_segment": {
        # 時報のあとにおまけを流す
        "enabled": True,
        # "both"   … 天気予報 → ひとこと の順に両方流す（既定）
        # "choice" … 従来どおり、どちらか一方を選ぶ
        # "choice" のときだけ weather_probability / always_weather_hours /
        # always_quote_hours / fallback_to_quote が効く。
        "mode": "both",
        # mode="both" で天気予報を流す正時の一覧。ここに無い時刻は
        # 時報＋ひとことだけになる。空リストにすると一度も流さない。
        # 既定は 2 時間おき。毎正時に流すなら 10〜16 をすべて並べる。
        # 閉館放送（16:57）は時報とは別の経路なので、ここに何を書いても
        # 天気は付かない。
        "weather_hours": [10, 12, 14, 16],
        # 天気予報を選ぶ確率（0.0〜1.0）。残りは「ひとこと」。mode="choice" 専用。
        "weather_probability": 0.0,
        # この時刻は必ず天気予報にする。mode="choice" 専用。
        "always_weather_hours": [],
        # この時刻は必ず「ひとこと」にする。mode="choice" 専用。
        "always_quote_hours": [],
        # 天気取得に失敗したら「ひとこと」に切り替える。mode="choice" 専用。
        # mode="both" では、ひとことは天気の成否に関わらず必ず流れるため、
        # 取得に失敗した天気は黙って飛ばす（この値は参照されない）。
        "fallback_to_quote": True,
    },
    "quotes": {
        "file": "assets/quotes.json",
        # 直近この件数と同じ「ひとこと」は選ばない
        "avoid_recent": 8,
    },
    "weather": {
        # 毎正時に天気予報を流す。無効にすると時報のあとは「ひとこと」だけになる。
        "enabled": True,
        # "jma"（気象庁・キー不要）または "open_meteo"（キー不要）
        #
        # 既定が open_meteo なのは、読み上げ文を作り置きできるようにするため。
        # 気象庁の予報文は自由文なので事前生成できず、Pi には実行時の合成手段が
        # 無いため、天気の文だけ無音になる。open_meteo は
        # 天気コード（WMO_CODES・28 語）で語彙が閉じるため、全パターンを
        # VOICEVOX で作り置きでき、放送全体をずんだもんの声で揃えられる。
        # jma に戻す場合はこの点を承知しておくこと（docs/SETUP.md 参照）。
        #
        # またこの既定は「現在の」天気と気温を読む前提になっている。気象庁に
        # 現況は無いため、jma に戻すと気温の文が消えて天気 1 文だけになり、
        # かつ sentence_weather の「今の」が実態（今日 1 日の予報）と食い違う。
        # jma を使うなら sentence_weather を予報の言い回しに戻し、
        # sentence_temp_max / sentence_pop を有効にすること。
        "provider": "open_meteo",
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
            # 「所により」以降は地域限定の但し書きで、館内放送には不要かつ
            # 読み上げが長くなる原因になるため既定で切り捨てる。
            # 空リスト（[]）にすると切り捨てを無効化できる。
            "drop_after": ["所により"],
        },
        "open_meteo": {
            # 読み上げる地点。上から順に読む。1 つに減らせば放送が短くなる。
            # label はそのまま読み上げられるので、読み間違えない表記にすること。
            "locations": [
                {"label": "大津", "latitude": 35.0045, "longitude": 135.8686},
                {"label": "京都", "latitude": 35.0116, "longitude": 135.7681},
            ],
        },
        # 読み上げは 1 文ずつ独立した音声ファイルとして再生する。文を分けて
        # おくと、地名・気温のそれぞれが有限の語彙に収まり、全パターンを
        # 作り置きできる（1 文にまとめると組み合わせが爆発して作り置きできない）。
        # 空文字列にするとその文を読まない（放送を短くしたいときに使う）。
        #
        # 既定は「現在の」天気と気温を読む（provider="open_meteo" が current で
        # 返す値）。時報で知りたいのは今どうなのかであり、その日の予想最高気温を
        # 午後に読んでも実感と合わないため。
        "sentence_weather": "今の{label}の天気は{weather}なのだ。",
        "sentence_temp": "気温は{temp}度なのだ。",
        # 以下は今日 1 日の予報。既定では読まない（空文字列）。読みたくなったら
        # 文言を入れて scripts/generate_voicevox.py を再実行すること
        # （空のあいだは作り置きが生成されず、有効化しただけでは声が揃わない）。
        # 降水確率は現況が存在しないため、有効にすると現況と予報が混在する。
        "sentence_temp_max": "",
        "sentence_pop": "",
        # 作り置きする語彙の範囲（scripts/generate_voicevox.py が参照する）。
        # ここを広げるほど生成するファイルが増える。範囲外の値が来た場合は
        # その 1 文だけ作り置きが外れて無音になる（放送は止まらない）。
        "prerecord": {
            "temp_min": -5,
            "temp_max": 40,
            # 降水確率はこの刻みに丸めて読み上げる（語彙を 11 通りに閉じるため）。
            "pop_step": 10,
            # forecast_days=1 で問い合わせるため実際には常に「今日」になる。
            # 「明日」も作り置きしたい場合はここに足す（+56 件）。
            "whens": ["今日"],
        },
        # 天気の説明部分（{weather}）の文字数上限。provider="jma" のときだけ
        # 意味を持つ（気象庁の自由文が予期せず長い場合の保険）。
        "max_weather_chars": 40,
    },
    "tts": {
        # 上から順に試し、失敗したら次のエンジンへフォールバックする。
        # どのエンジンでも合成できない文言は「その 1 文だけ無音」になり、
        # 放送そのものは続く（時報音・蛍の光・他の文は鳴る）。
        "engines": ["prerecorded", "voicevox"],
        "cache_dir": "cache/tts",
        "prerecorded_dir": "assets/voice",
        "voicevox": {
            "base_url": "http://127.0.0.1:50021",
            # 3 = ずんだもん（ノーマル）
            "speaker": 3,
            "timeout_seconds": 20.0,
            # 疎通確認（GET /version）専用のタイムアウト。放送直前に呼ばれる
            # 実行時は短く保ち（既定 2 秒）、エンジンが落ちていた場合に
            # 即座に次のエンジンへフォールバックできるようにする。
            # VOICEVOX ENGINE は起動直後、モデル読み込みのため数秒〜数十秒
            # 応答しないことがあるため、事前生成スクリプト
            # （scripts/generate_voicevox.py の --wait）ではこの値を長めに
            # 設定して起動待ちに使う（詳細は chime/tts.py 参照）。
            "probe_timeout_seconds": 2.0,
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
        override = _read_json(candidate)
        _warn_if_defaults_were_copied(candidate, override)
        data = deep_merge(data, override)
        sources.append(candidate)

    return Config(data, base_dir=base_dir, sources=sources)


#: 既定値と同じ値をこれ以上明示している設定ファイルは、差分ではなく
#: 既定値の丸ごとコピーとみなして警告する。手書きの上書きファイルで
#: 偶然これだけ一致することは考えにくい。
_COPIED_DEFAULTS_THRESHOLD = 10


def redundant_keys(override: Mapping[str, Any],
                   default: Mapping[str, Any] = DEFAULT_CONFIG,
                   prefix: str = "") -> List[str]:
    """``override`` のうち、既定値と同じ値を明示しているキーの一覧を返す。

    設定ファイルは既定値への「差分」であり、書かなかった項目は既定値が使われる。
    既定値と同じ値をわざわざ書いても動作は変わらないが、**将来その既定値を
    変更したときに、古い値で上書きし続けてしまう**。
    """
    found: List[str] = []
    for key, value in override.items():
        if key not in default:
            continue
        path = "{0}.{1}".format(prefix, key) if prefix else str(key)
        base = default[key]
        if isinstance(value, Mapping) and isinstance(base, Mapping):
            found.extend(redundant_keys(value, base, path))
        elif value == base:
            found.append(path)
    return found


def _warn_if_defaults_were_copied(path: str, override: Mapping[str, Any]) -> None:
    """既定値を丸ごと写した設定ファイルを検出して警告する。

    ``scripts/setup.sh`` は以前 ``config.example.json``（＝既定値の完全な
    コピー）を ``config.json`` として複製していた。そうして作られた設定は
    その時点の既定値を凍結するため、更新しても新しい既定値が一切届かない。

    実際に、旧版で作られた ``config.json`` が読み上げ文言を古いまま上書きし、
    事前生成した音声（文言との完全一致で引く）に当たらず、当時あった
    Open JTalk のフォールバックが合成する — つまり読み上げだけ別人の男性音声に
    なる、という形で表面化した（v5.0.0 でそのフォールバックは削除したため、
    いま同じことが起きれば読み上げは無音になる）。症状から原因に辿り着くのが
    難しいため、起動時に気づけるようにする。

    動作は変えない（警告のみ）。意図して既定値と同じ値を書いている利用者の
    設定を、こちらの判断で無視するべきではないため。
    """
    redundant = redundant_keys(override)
    if len(redundant) < _COPIED_DEFAULTS_THRESHOLD:
        return
    logger.warning(
        "%s は既定値と同じ値を %d 項目書いています。既定値の丸ごとコピーの"
        "可能性があります。この状態だと、更新しても新しい既定値が届きません"
        "（読み上げ文言が古いままだと、作り置き音声に当たらず読み上げが"
        "無音になります）。変えたい項目だけを残してください。詳しくは "
        "docs/SETUP.md の「読み上げが無音になる」を参照。",
        path, len(redundant))
    logger.warning("  既定値と同じ項目の例: %s", "、".join(redundant[:5]))


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
