"""同梱の音声ファイル（``assets/``）の健全性テスト。

Pi には実行時の音声合成が無く、読み上げはリポジトリに同梱した作り置き音声
（``assets/voice/``）だけで鳴る。ファイルが欠けた・壊れた・文言と食い違った、
というミスはコミットの時点では気づきにくく、現地で「その 1 文だけ無音」という
形で初めて表面化する。ここでは VOICEVOX を使わず、ファイルそのものだけを調べる。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import wave

from tests.support import REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from generate_voicevox import collect_phrases  # noqa: E402

from chime.config import BASE_DIR, DEFAULT_CONFIG, Config  # noqa: E402
from chime.tts import _digest  # noqa: E402

VOICE_DIR = os.path.join(REPO_ROOT, "assets", "voice")
MANIFEST_PATH = os.path.join(VOICE_DIR, "manifest.json")
ANNOUNCE_PATH = os.path.join(REPO_ROOT, "assets", "announce.wav")
HOTARU_PATH = os.path.join(REPO_ROOT, "assets", "hotaru.mp3")

#: VOICEVOX ENGINE が出力する形式（scripts/generate_voicevox.py が書き出す WAV）。
EXPECTED_RATE = 24000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH = 2  # 16 bit

#: これより短い WAV は、合成の失敗で空に近いファイルができたとみなす。
#: 最も短い文でも 1 秒を超えるため、余裕を持たせた下限。
MIN_SECONDS = 0.3

#: assets/voice/ に置いてよい WAV 以外のファイル。
ALLOWED_NON_WAV = {"manifest.json", ".gitkeep"}

#: 蛍の光は 3 MB 余りある。これを下回るなら切れているか、Git LFS のポインタ
#: ファイル等に置き換わっている。
MIN_MP3_BYTES = 100 * 1024

REBUILD_HINT = ("PC で scripts/generate_voicevox.py --include-quotes --prune を実行して"
                "作り直し、assets/voice/ をコミットしてください")


def _load_manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as handle:
        return json.load(handle)


class VoiceManifestTest(unittest.TestCase):
    """``assets/voice/manifest.json``（文言 → ファイル名）と実ファイルの整合。"""

    @classmethod
    def setUpClass(cls):
        cls.manifest = _load_manifest()

    def test_manifest_is_not_empty(self):
        self.assertTrue(self.manifest,
                        "manifest.json が空です。{0}".format(REBUILD_HINT))

    def test_every_manifest_file_exists(self):
        missing = sorted(
            "{0} → {1}".format(text, name)
            for text, name in self.manifest.items()
            if not os.path.isfile(os.path.join(VOICE_DIR, name)))
        self.assertEqual(
            missing, [],
            "manifest.json にあるのに assets/voice/ に無い音声ファイルがあります"
            "（Pi ではその文だけ無音になります）。{0}".format(REBUILD_HINT))

    def test_every_wav_has_the_voicevox_format(self):
        bad = []
        for text, name in self.manifest.items():
            path = os.path.join(VOICE_DIR, name)
            if not os.path.isfile(path):
                continue  # 欠けているファイルは別のテストが報告する
            try:
                with wave.open(path, "rb") as handle:
                    rate = handle.getframerate()
                    channels = handle.getnchannels()
                    width = handle.getsampwidth()
                    seconds = handle.getnframes() / float(rate) if rate else 0.0
            except (wave.Error, EOFError) as exc:
                bad.append("{0}: WAV として開けません（{1}: {2}）".format(
                    name, type(exc).__name__, exc))
                continue
            if (rate, channels, width) != (EXPECTED_RATE, EXPECTED_CHANNELS,
                                           EXPECTED_SAMPLE_WIDTH):
                bad.append("{0}: {1} Hz・{2} ch・{3} bit（24000 Hz・モノラル・16 bit の"
                           "はず）".format(name, rate, channels, width * 8))
            elif seconds < MIN_SECONDS:
                bad.append("{0}: {1:.2f} 秒しかありません（{2} 秒以上のはず）".format(
                    name, seconds, MIN_SECONDS))
        self.assertEqual(
            bad, [],
            "形式が違う、または壊れている音声ファイルがあります。"
            "{0}。".format(REBUILD_HINT) + "\n" + "\n".join(bad))

    def test_no_orphan_files_in_the_voice_directory(self):
        referenced = set(self.manifest.values())
        orphans = sorted(
            name for name in os.listdir(VOICE_DIR)
            if name.endswith(".wav") and name not in referenced)
        self.assertEqual(
            orphans, [],
            "manifest.json から参照されていない WAV が assets/voice/ にあります"
            "（文言を変えた古い音声が残っています）。PC で "
            "scripts/generate_voicevox.py --include-quotes --prune を実行して"
            "不要なファイルを消し、assets/voice/ をコミットしてください。")

    def test_only_manifest_and_gitkeep_are_not_wav(self):
        others = sorted(
            name for name in os.listdir(VOICE_DIR)
            if not name.endswith(".wav") and name not in ALLOWED_NON_WAV)
        self.assertEqual(
            others, [],
            "assets/voice/ に WAV でも manifest.json でも .gitkeep でもないファイルが"
            "あります。誤ってコミットしたものなら削除してください。")

    def test_file_names_are_the_digest_of_the_phrase(self):
        # chime/tts.py の PrerecordedEngine は、文言から名前を計算して引く。
        # manifest 上の名前がその計算結果と違うと、ファイルがあっても引けない。
        mismatched = sorted(
            "{0}: {1}（期待 {2}）".format(text, name, _digest(text) + ".wav")
            for text, name in self.manifest.items()
            if name != _digest(text) + ".wav")
        self.assertEqual(
            mismatched, [],
            "manifest.json のファイル名が、文言から計算した名前（chime.tts._digest）と"
            "食い違っています。{0}。".format(REBUILD_HINT))

    def test_manifest_covers_every_phrase_the_chime_speaks(self):
        # 既定の設定で読み上げうる文言（天気の全語彙・時刻・閉館放送・ひとこと）が
        # すべて作り置きされていること。1 つでも欠けると、その文だけ Pi で無音になる。
        config = Config(DEFAULT_CONFIG, base_dir=BASE_DIR)
        phrases = collect_phrases(config, include_quotes=True)
        missing = [text for text in phrases if text not in self.manifest]
        self.assertEqual(
            missing, [],
            "作り置きが無い文言があります（{0} 件。Pi ではその文だけ無音になります）。"
            "{1}。\n{2}".format(len(missing), REBUILD_HINT, "\n".join(missing[:10])))


class BundledAudioTest(unittest.TestCase):
    """``assets/`` 直下の閉館放送用ファイル（アナウンスと蛍の光）。"""

    def test_announce_wav_is_a_playable_wav(self):
        try:
            with wave.open(ANNOUNCE_PATH, "rb") as handle:
                frames = handle.getnframes()
                rate = handle.getframerate()
        except (OSError, wave.Error, EOFError) as exc:
            self.fail("assets/announce.wav を WAV として開けません（{0}）。"
                      "git checkout で元のファイルへ戻すか、閉館アナウンスの音声を"
                      "作り直して assets/announce.wav に置いてください。".format(
                          "{0}: {1}".format(type(exc).__name__, exc)))
        self.assertGreater(frames, 0,
                           "assets/announce.wav に音声データがありません。"
                           "元のファイルへ戻してください。")
        self.assertGreater(
            frames / float(rate), 1.0,
            "assets/announce.wav が 1 秒以下です。途中で切れている可能性があります。"
            "元のファイルへ戻してください。")

    def test_hotaru_mp3_looks_like_an_mp3(self):
        with open(HOTARU_PATH, "rb") as handle:
            head = handle.read(4)
        has_id3 = head[:3] == b"ID3"
        has_frame_sync = len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
        self.assertTrue(
            has_id3 or has_frame_sync,
            "assets/hotaru.mp3 の先頭が MP3 の形式ではありません"
            "（ID3 タグでも MPEG フレームでもない）。元のファイルへ戻してください。")
        self.assertGreater(
            os.path.getsize(HOTARU_PATH), MIN_MP3_BYTES,
            "assets/hotaru.mp3 が小さすぎます（100 KB 以下）。途中で切れているか、"
            "Git LFS のポインタファイルなどに置き換わっている可能性があります。"
            "元のファイルへ戻してください。")


if __name__ == "__main__":
    unittest.main()
