"""音声合成（TTS）。

複数のエンジンを順に試し、最初に成功したものを採用する。

``prerecorded``
    事前生成済み WAV（``assets/voice/``）を使う。VOICEVOX:ずんだもんの声を
    PC 側で作り置きしておくための仕組み。合成処理は発生しない。
``voicevox``
    VOICEVOX ENGINE の HTTP API を叩く。Pi 上では重いため、LAN 上の PC を
    指す想定（``base_url`` で指定）。
``open_jtalk``
    Raspberry Pi 上でオフライン合成する最終フォールバック。

合成結果は ``cache/tts/`` にキャッシュするため、同じ文言は 2 回目以降
合成されない（時報の定型文はネットワーク・CPU をほとんど使わなくなる）。
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional

from . import env

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
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        loaded = json.load(handle)
                    if isinstance(loaded, dict):
                        data = {str(k): str(v) for k, v in loaded.items()}
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("assets/voice/manifest.json を読めません: %s", exc)
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


class OpenJTalkEngine(TTSEngine):
    """Open JTalk によるオフライン合成。"""

    name = "open_jtalk"

    DICT_CANDIDATES = (
        "/var/lib/mecab/dic/open-jtalk/naist-jdic",
        "/usr/share/open_jtalk/open_jtalk_dic_utf_8-*",
        "/usr/local/dic",
    )
    VOICE_CANDIDATES = (
        "/usr/share/hts-voice/*/*.htsvoice",
        "/usr/share/hts-voice/**/*.htsvoice",
        "/usr/local/share/hts-voice/**/*.htsvoice",
    )

    def __init__(self, settings: Mapping[str, Any], base_dir: str) -> None:
        super().__init__(settings, base_dir)
        self.binary = str(self.settings.get("binary", "open_jtalk"))
        self._dictionary: Optional[str] = None
        self._voice: Optional[str] = None

    @staticmethod
    def _first_match(configured: str, patterns) -> Optional[str]:
        if configured and os.path.exists(configured):
            return configured
        for pattern in patterns:
            matches = sorted(glob.glob(pattern, recursive=True))
            if matches:
                return matches[0]
        return None

    def dictionary(self) -> Optional[str]:
        if self._dictionary is None:
            self._dictionary = self._first_match(
                str(self.settings.get("dictionary", "")), self.DICT_CANDIDATES)
        return self._dictionary

    def voice(self) -> Optional[str]:
        if self._voice is None:
            self._voice = self._first_match(
                str(self.settings.get("voice", "")), self.VOICE_CANDIDATES)
        return self._voice

    def voice_id(self) -> str:
        return "|".join([
            self.name,
            os.path.basename(self.voice() or "none"),
            str(self.settings.get("speed", 1.0)),
            str(self.settings.get("additional_half_tone", 0.0)),
            str(self.settings.get("sampling_frequency", 48000)),
        ])

    def available(self) -> bool:
        if not env.has_command(self.binary):
            return False
        return bool(self.dictionary() and self.voice())

    def synthesize(self, text: str, out_path: str) -> None:
        dictionary, voice = self.dictionary(), self.voice()
        if not dictionary or not voice:
            raise TTSError("Open JTalk の辞書または音響モデルが見つかりません。")

        command = [
            self.binary,
            "-x", dictionary,
            "-m", voice,
            "-r", str(self.settings.get("speed", 1.0)),
            "-fm", str(self.settings.get("additional_half_tone", 0.0)),
            "-g", str(self.settings.get("volume_gain_db", 0.0)),
            "-s", str(int(self.settings.get("sampling_frequency", 48000))),
            "-ow", out_path,
        ]

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt",
                                         delete=False) as handle:
            handle.write(text)
            input_path = handle.name
        try:
            result = subprocess.run(command + [input_path], check=False,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            os.unlink(input_path)

        if result.returncode != 0 or not os.path.exists(out_path):
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise TTSError("open_jtalk が失敗しました: " + (detail or str(result.returncode)))


class VoicevoxEngine(TTSEngine):
    """VOICEVOX ENGINE（HTTP API）による合成。"""

    name = "voicevox"

    def __init__(self, settings: Mapping[str, Any], base_dir: str) -> None:
        super().__init__(settings, base_dir)
        self.base_url = str(self.settings.get("base_url", "")).rstrip("/")
        self.speaker = int(self.settings.get("speaker", 3))
        self.timeout = float(self.settings.get("timeout_seconds", 20.0))

    def voice_id(self) -> str:
        return "{0}|{1}".format(self.name, self.speaker)

    def available(self) -> bool:
        if not self.base_url:
            return False
        try:
            with urllib.request.urlopen(self.base_url + "/version", timeout=2.0) as response:
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
            elif name == "open_jtalk":
                engines.append(OpenJTalkEngine(self.settings.get("open_jtalk", {}), self.base_dir))
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

    def synthesize(self, text: str) -> str:
        """文言を読み上げた WAV のパスを返す。全エンジン失敗時は :class:`TTSError`。"""
        text = (text or "").strip()
        if not text:
            raise TTSError("読み上げる文言が空です。")

        errors: List[str] = []
        for engine in self.engines:
            try:
                if not engine.available():
                    errors.append(engine.name + ": 利用不可")
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

        raise TTSError("音声合成に失敗しました（" + " / ".join(errors) + "）")

    def _cache_path(self, engine: TTSEngine, text: str) -> str:
        return os.path.join(self.cache_dir, _digest(engine.voice_id(), text) + ".wav")
