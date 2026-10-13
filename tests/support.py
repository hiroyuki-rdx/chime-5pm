"""テスト共通のヘルパー。"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import urllib.error
import wave
from unittest import mock

from chime import config as chime_config
from chime.audio import Player
from chime.scheduler import Event

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# -- 同梱データ（リポジトリに入っている実ファイル） --------------------------------
ASSETS_DIR = os.path.join(REPO_ROOT, "assets")
VOICE_DIR = os.path.join(ASSETS_DIR, "voice")
MANIFEST_PATH = os.path.join(VOICE_DIR, "manifest.json")
ANNOUNCE_PATH = os.path.join(ASSETS_DIR, "announce.wav")
SHIPPED_QUOTES = os.path.join(ASSETS_DIR, "quotes.json")


def load_manifest() -> dict:
    """同梱の作り置き音声の ``manifest.json``（文言 → ファイル名）を読み込む。"""
    with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_quotes(directory: str, data) -> str:
    """``directory`` に ``quotes.json`` を書き、そのパスを返す。"""
    path = os.path.join(directory, "quotes.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    return path


def fixture_path(name: str) -> str:
    """``tests/fixtures/`` 配下のファイルパスを返す。"""
    return os.path.join(FIXTURES, name)


def load_fixture(name: str):
    """``tests/fixtures/`` の JSON を読み込む。"""
    with open(fixture_path(name), "r", encoding="utf-8") as handle:
        return json.load(handle)


# -- 設定の例 --------------------------------------------------------------------
#: 現地（Pi）の config.json で地点を京都に差し替えた、という想定の設定。
#: 配列は丸ごと置き換わるので、既定の大津は外れる。
#: ``tests/test_phrases.py`` と ``tests/test_generate_voicevox.py`` が共有する
#: （テストモジュール同士で import し合わないよう、ここに置く）。
KYOTO_ONLY = {
    "weather": {
        "open_meteo": {
            "locations": [{"label": "京都", "latitude": 35.0116, "longitude": 135.7681}],
        },
    },
}


# -- 開発者の手元の config.json ---------------------------------------------------
#: 差し替える前の ``chime.config.local_config_path``（本物の動作を確かめるテスト用）。
REAL_LOCAL_CONFIG_PATH = chime_config.local_config_path

#: 「現地設定は無い」ことを表す、存在しないパス。
NO_LOCAL_CONFIG = os.path.join(FIXTURES, "no-local-config", "config.json")


def _local_config_path_for_tests(base_dir: str = chime_config.BASE_DIR) -> str:
    """リポジトリ直下だけ「現地設定なし」にし、ほかのフォルダは本物の場所を返す。"""
    if os.path.abspath(base_dir) == chime_config.BASE_DIR:
        return NO_LOCAL_CONFIG
    return REAL_LOCAL_CONFIG_PATH(base_dir)


def isolate_local_config() -> None:
    """リポジトリ直下の ``config.json`` を、テストが読まないようにする。

    開発者が手元に置いた ``config.json``（Git 管理外）を ``load_config`` が自動で
    読むため、CLI や生成スクリプトを ``--config`` なしで動かすテストは、その内容に
    左右されて落ちていた（例: ``start_hour`` を 9 にしていると時刻アナウンスの
    件数が変わる）。``chime.config.local_config_path`` の差し替えで、リポジトリ
    直下だけ「無い」ことにする。``base_dir`` に一時フォルダを渡すテストは、その
    フォルダの ``config.json`` を従来どおり読む。

    このモジュールを import したときに 1 度だけ呼ぶ（プロセス全体に効く。
    ``tests/__init__.py`` のログ抑制と同じ扱い）。何度呼んでも結果は同じ。
    """
    chime_config.local_config_path = _local_config_path_for_tests


isolate_local_config()


# -- ログ ------------------------------------------------------------------------
@contextlib.contextmanager
def logs_enabled():
    """``tests/__init__.py`` が止めているログを、このブロックの間だけ拾えるようにする。

    ``assertLogs`` は ``logging.disable(logging.CRITICAL)`` で止められたログを拾えない。
    ブロックを抜けたら、``tests/__init__.py`` が設定する状態（CRITICAL）へ戻す。
    直前の値を控えて戻さないのは、テスト全体の既定が常にその状態だと決まっていて、
    途中で例外が出ても同じところへ戻るため。``assertLogs`` は呼び出し側で書く。
    """
    logging.disable(logging.NOTSET)
    try:
        yield
    finally:
        logging.disable(logging.CRITICAL)


# -- 通信 ------------------------------------------------------------------------
def block_network(testcase):
    """``testcase`` の間、``urllib.request.urlopen`` をすべて通信失敗にする。

    CLI や常駐ループをまるごと動かすテストは、VOICEVOX ENGINE の有無の確認
    （127.0.0.1:50021）などで、意図せず通信しかねない。テストは通信しない前提なので、
    どの URL でも ``URLError`` を送出する。``AssertionError`` などにしないのは、
    本番コードが ``URLError`` を想定内の通信失敗として扱い、それ以外の例外は別の経路で
    変換するため（本物の通信失敗と同じ形でないと、確かめたい経路を通らない）。

    要求された URL は、戻り値のリストへ順に追記する。テスト側で ``urlopen`` を
    さらに差し替えた場合はそちらが優先され、抜けるとこの差し替えへ戻る。
    """
    requested = []

    def refuse(request, *args, **kwargs):
        requested.append(request if isinstance(request, str) else request.full_url)
        raise urllib.error.URLError("テスト中は通信しません")

    patcher = mock.patch("urllib.request.urlopen", refuse)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return requested


def urlopen_response(body=None, read_error=None):
    """``urlopen`` が返す応答のモック（``with`` で使え、``read()`` が本文か例外を返す）。"""
    response = mock.MagicMock()
    response.__enter__.return_value = response
    if read_error is not None:
        response.read.side_effect = read_error
    else:
        response.read.return_value = json.dumps(body).encode("utf-8")
    return response


# -- 音声・再生 ------------------------------------------------------------------
def make_wav(path: str, seconds: float = 0.05) -> str:
    """無音の WAV（8kHz・モノラル・16bit）を ``path`` に書き、そのパスを返す。"""
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


class RecordingPlayer(Player):
    """鳴らさずに、再生を求められたパスと ``open``／``close`` の回数だけ記録する。"""

    name = "recording"

    def __init__(self, settings=None):
        super().__init__(settings or {})
        self.played = []
        self.opened = 0
        self.closed = 0

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def play_one(self, segment):
        self.played.append(segment.path)


# -- スケジュール ----------------------------------------------------------------
def make_event(moment, key="hourly:10", kind="hourly", hour=10):
    """``moment`` に鳴らす（準備も同時刻の）イベントを作る。"""
    return Event(key=key, kind=kind, hour=hour,
                 at=moment, play_at=moment, prepare_at=moment)
