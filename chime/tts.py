"""音声合成（TTS）。

複数のエンジンを順に試し、最初に成功したものを採用する。

``prerecorded``
    事前生成済み WAV（``assets/voice/``）を使う。VOICEVOX:ずんだもんの声を
    PC 側で作り置きしておくための仕組み。合成処理は発生しない。
``voicevox``
    VOICEVOX ENGINE の HTTP API を叩く。Pi 上では重いため、LAN 上の PC を
    指す想定（``base_url`` で指定）。

``cache/tts/`` のキャッシュは、VOICEVOX で実際に合成したときだけ使われる
（PC 側など）。Pi では作り置きを使うので、合成もキャッシュも発生しない。

作り置きにも VOICEVOX にも無い文言は、合成できずそのセグメントが
無音になる（他人の声にフォールバックすることはない）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional

from .jsonfile import JsonFileError, read_json

logger = logging.getLogger(__name__)


class TTSError(RuntimeError):
    """音声合成に失敗した場合に送出する。"""


def _digest(*parts: str) -> str:
    # 空白区切りだと voice_id と text の境界がずれた組み合わせ
    # （例: ("A", "B C") と ("A B", "C")）が同じ文字列になり、
    # キャッシュキーが衝突しうる。通常のテキストに現れない制御文字で区切る。
    joined = "\x1f".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:20]


class TTSEngine:
    """音声合成エンジンの基底クラス。"""

    name = "base"

    def __init__(self, settings: Mapping[str, Any], base_dir: str) -> None:
        self.settings = dict(settings)
        self.base_dir = base_dir

    def voice_id(self) -> str:
        """キャッシュキーに含める、声色を識別する文字列。"""
        return self.name

    def available(self) -> bool:  # pragma: no cover - 抽象
        raise NotImplementedError

    def lookup(self, text: str) -> Optional[str]:
        """合成せずに使える既存ファイルがあれば、そのパスを返す。"""
        return None

    def synthesize(self, text: str, out_path: str) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class PrerecordedEngine(TTSEngine):
    """事前生成済み WAV を参照するだけのエンジン。"""

    name = "prerecorded"

    def __init__(self, settings: Mapping[str, Any], base_dir: str, directory: str) -> None:
        super().__init__(settings, base_dir)
        self.directory = directory
        self._manifest: Optional[Dict[str, str]] = None

    def manifest(self) -> Dict[str, str]:
        """``manifest.json``（文言 → ファイル名）を読み込む。"""
        if self._manifest is None:
            path = os.path.join(self.directory, "manifest.json")
            data: Dict[str, str] = {}
            try:
                loaded = read_json(path)
            except JsonFileError as exc:
                if exc.kind != "missing":
                    logger.warning("作り置きの目録を読めません（空として扱います）: %s", exc)
            else:
                if isinstance(loaded, dict):
                    data = {str(k): str(v) for k, v in loaded.items()}
            self._manifest = data
        return self._manifest

    def available(self) -> bool:
        return os.path.isdir(self.directory)

    def lookup(self, text: str) -> Optional[str]:
        if not self.available():
            return None
        filename = self.manifest().get(text)
        candidates = []
        if filename:
            candidates.append(os.path.join(self.directory, filename))
        candidates.append(os.path.join(self.directory, _digest(text) + ".wav"))
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None

    def synthesize(self, text: str, out_path: str) -> None:
        raise TTSError("事前生成済み音声にこの文言はありません。")


class VoicevoxEngine(TTSEngine):
    """VOICEVOX ENGINE（HTTP API）による合成。"""

    name = "voicevox"

    def __init__(self, settings: Mapping[str, Any], base_dir: str) -> None:
        super().__init__(settings, base_dir)
        self.base_url = str(self.settings.get("base_url", "")).rstrip("/")
        self.speaker = int(self.settings.get("speaker", 3))
        self.timeout = float(self.settings.get("timeout_seconds", 20.0))
        # 疎通確認（GET /version）専用のタイムアウト。放送直前（実行時）に
        # 呼ばれるため既定は短く（2 秒）保ち、エンジンが落ちていた場合に
        # 即座に他のエンジンへフォールバックできるようにする。一方、VOICEVOX
        # ENGINE は起動直後、モデル読み込みのため /version が数秒〜数十秒
        # 応答しないことがある。事前生成スクリプト
        # （scripts/generate_voicevox.py の --wait）は、この値をより長く
        # 設定したインスタンスで疎通確認を繰り返すことで起動待ちを行う。
        self.probe_timeout = float(self.settings.get("probe_timeout_seconds", 2.0))

    def voice_id(self) -> str:
        return "{0}|{1}".format(self.name, self.speaker)

    def available(self) -> bool:
        if not self.base_url:
            return False
        try:
            with urllib.request.urlopen(self.base_url + "/version",
                                        timeout=self.probe_timeout) as response:
                return getattr(response, "status", 200) == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def synthesize(self, text: str, out_path: str) -> None:
        query = urllib.parse.urlencode({"text": text, "speaker": self.speaker})
        try:
            request = urllib.request.Request(
                self.base_url + "/audio_query?" + query, data=b"", method="POST")
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                audio_query = response.read()

            request = urllib.request.Request(
                "{0}/synthesis?speaker={1}".format(self.base_url, self.speaker),
                data=audio_query,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                audio = response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TTSError("VOICEVOX ENGINE との通信に失敗しました: {0}".format(exc)) from exc

        if not audio:
            raise TTSError("VOICEVOX ENGINE が空の音声を返しました。")
        with open(out_path, "wb") as handle:
            handle.write(audio)


class TTSService:
    """エンジンの選択・フォールバック・キャッシュを束ねる。"""

    def __init__(self, settings: Mapping[str, Any], base_dir: str,
                 cache_dir: str, prerecorded_dir: str) -> None:
        self.settings = dict(settings)
        self.base_dir = base_dir
        self.cache_dir = cache_dir
        self.prerecorded_dir = prerecorded_dir
        self.engines: List[TTSEngine] = self._build_engines()

    def _build_engines(self) -> List[TTSEngine]:
        engines: List[TTSEngine] = []
        for name in self.settings.get("engines", []):
            name = str(name)
            if name == "prerecorded":
                engines.append(PrerecordedEngine({}, self.base_dir, self.prerecorded_dir))
            elif name == "voicevox":
                engines.append(VoicevoxEngine(self.settings.get("voicevox", {}), self.base_dir))
            else:
                logger.warning("未知の TTS エンジン '%s' は無視します。", name)
        return engines

    def describe(self) -> str:
        parts = []
        for engine in self.engines:
            state = "利用可" if engine.available() else "利用不可"
            parts.append("{0}({1})".format(engine.name, state))
        return ", ".join(parts) or "（エンジンなし）"

    def _prerecorded_engine(self) -> Optional[PrerecordedEngine]:
        for engine in self.engines:
            if isinstance(engine, PrerecordedEngine):
                return engine
        return None

    def prerecorded_lookup(self, text: str) -> Optional[str]:
        """作り置きにある文言なら、その WAV のパスを返す（無ければ ``None``）。

        prerecorded エンジンの照合だけで引く。VOICEVOX には問い合わせない
        ため、エンジンが動いていなくても（Pi 上でも）使える。放送で無音に
        なる文言かどうかを事前に確かめる用途。
        """
        engine = self._prerecorded_engine()
        text = (text or "").strip()
        if engine is None or not text:
            return None
        return engine.lookup(text)

    def known_phrases(self) -> List[str]:
        """作り置きの目録（manifest.json）に載っている文言の一覧を返す。"""
        engine = self._prerecorded_engine()
        if engine is None:
            return []
        return list(engine.manifest())

    def synthesize(self, text: str) -> str:
        """文言を読み上げた WAV のパスを返す。全エンジン失敗時は :class:`TTSError`。"""
        text = (text or "").strip()
        if not text:
            raise TTSError("読み上げる文言が空です。")

        errors: List[str] = []
        unavailable: List[str] = []
        for engine in self.engines:
            try:
                if not engine.available():
                    errors.append(engine.name + ": 利用不可")
                    unavailable.append(engine.name)
                    continue

                existing = engine.lookup(text)
                if existing:
                    logger.debug("既存音声を使用[%s]: %s", engine.name, existing)
                    return existing

                cached = self._cache_path(engine, text)
                if os.path.exists(cached):
                    logger.debug("キャッシュを使用[%s]: %s", engine.name, cached)
                    return cached

                os.makedirs(self.cache_dir, exist_ok=True)
                # pid だけでは同一プロセス内の並行呼び出しで一時ファイル名が
                # 衝突しうるため、スレッド ID も加えて一意にする。
                temp_path = "{0}.{1}.{2}.tmp".format(
                    cached, os.getpid(), threading.get_ident())
                try:
                    engine.synthesize(text, temp_path)
                    os.replace(temp_path, cached)
                except Exception:
                    # 合成が失敗した場合、書きかけの一時ファイルを残さない。
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except OSError:
                            pass
                    raise
                logger.info("音声合成[%s]: %s", engine.name, text)
                return cached
            except TTSError as exc:
                errors.append("{0}: {1}".format(engine.name, exc))
            except Exception as exc:  # pragma: no cover - 想定外は次のエンジンへ
                errors.append("{0}: 予期しないエラー: {1}".format(engine.name, exc))

        detail = " / ".join(errors)
        if self._prerecorded_engine() is None:
            raise TTSError("音声合成に失敗しました（" + detail + "）")
        # 主な原因は「作り置きに無い」こと。Pi では VOICEVOX ENGINE が動いて
        # いないのが正常なので、そちらは補足に留める。
        summary = "作り置き（assets/voice/）にこの文言がありません"
        if "voicevox" in unavailable:
            summary += "（VOICEVOX ENGINE も使えません。Pi ではこれが正常）"
        raise TTSError("{0}。エンジンごとの詳細: {1}".format(summary, detail))

    def _cache_path(self, engine: TTSEngine, text: str) -> str:
        return os.path.join(self.cache_dir, _digest(engine.voice_id(), text) + ".wav")
