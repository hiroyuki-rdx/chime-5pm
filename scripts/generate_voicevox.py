#!/usr/bin/env python3
"""VOICEVOX ENGINE で定型文の音声を事前生成する。

VOICEVOX ENGINE は Raspberry Pi 3B 上で常時動かすには重いため、
**PC 側で本スクリプトを実行して WAV を作り、リポジトリに同梱する**という
使い方を想定している。生成物は ``assets/voice/`` に置かれ、実行時は
``prerecorded`` エンジンがこれを最優先で使う（合成処理は発生しない）。

使い方（PC 側で VOICEVOX を起動した状態で）::

    python3 scripts/generate_voicevox.py
    python3 scripts/generate_voicevox.py --base-url http://192.168.1.10:50021
    python3 scripts/generate_voicevox.py --speaker 3 --include-quotes

Docker で VOICEVOX ENGINE を起動した直後はモデル読み込みのため
``/version`` がしばらく応答しないことがある。既定では起動を最大 90 秒
待つ（``--wait 0`` で待たずに即座に判定する）。

生成後は ``assets/voice/`` を git add してコミットし、Pi 側で git pull する。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chime import timesignal  # noqa: E402
from chime.config import load_config  # noqa: E402
from chime.quotes import load_quotes  # noqa: E402
from chime.tts import TTSError, VoicevoxEngine, _digest  # noqa: E402

#: 起動待ち中に疎通確認を再試行する間隔（秒）。
_POLL_INTERVAL_SECONDS = 3.0


def wait_for_engine(engine: VoicevoxEngine, wait_seconds: float) -> bool:
    """VOICEVOX ENGINE が応答するようになるまで、最大 ``wait_seconds`` 秒待つ。

    Docker で起動した直後の VOICEVOX ENGINE は、ONNX モデルの読み込みに
    より数秒〜数十秒 ``/version`` に応答しないことがある。無言で固まった
    ように見えないよう、待っている間は残り時間を表示しながら数秒おきに
    :meth:`VoicevoxEngine.available` を再試行する。``wait_seconds`` が
    0 以下なら再試行せず、従来どおり 1 回だけ判定する。
    """
    if engine.available():
        return True
    if wait_seconds <= 0:
        return False

    deadline = time.monotonic() + wait_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        print("VOICEVOX の起動を待っています…（残り {0:.0f} 秒）".format(remaining),
              file=sys.stderr)
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        if engine.available():
            return True


def collect_phrases(config, include_quotes: bool) -> list:
    """事前生成する文言を集める。"""
    settings = config.section("time_signal")
    hourly = config.section("schedule.hourly")
    phrases = [
        timesignal.announce_text(hour, settings)
        for hour in range(int(hourly.get("start_hour", 10)),
                          int(hourly.get("end_hour", 16)) + 1)
    ]

    extra_text = str(config.get("closing.extra_text", "") or "")
    if extra_text:
        phrases.append(extra_text)

    if include_quotes:
        quotes = load_quotes(config.path("quotes.file"))
        phrases.extend(quotes.get("general", []))
        for values in quotes.get("by_hour", {}).values():
            phrases.extend(values)

    seen, unique = set(), []
    for phrase in phrases:
        if phrase and phrase not in seen:
            seen.add(phrase)
            unique.append(phrase)
    return unique


def main() -> int:
    config = load_config()
    voicevox = config.section("tts.voicevox")

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=voicevox.get("base_url", "http://127.0.0.1:50021"),
                        help="VOICEVOX ENGINE の URL")
    parser.add_argument("--speaker", type=int, default=int(voicevox.get("speaker", 3)),
                        help="話者 ID（3 = ずんだもん・ノーマル）")
    parser.add_argument("--out", default=config.path("tts.prerecorded_dir"),
                        help="出力先ディレクトリ")
    parser.add_argument("--include-quotes", action="store_true",
                        help="「ひとこと」もまとめて生成する")
    parser.add_argument("--force", action="store_true",
                        help="既存ファイルがあっても作り直す")
    parser.add_argument("--wait", type=float, default=90.0,
                        help="VOICEVOX ENGINE が応答するまで待つ秒数"
                             "（既定 90 秒。0 なら待たずに即座に判定する）")
    args = parser.parse_args()

    engine = VoicevoxEngine({
        "base_url": args.base_url,
        "speaker": args.speaker,
        "timeout_seconds": 60.0,
        # 起動待ちで繰り返す疎通確認は、実行時（既定 2 秒）より少し長めの
        # タイムアウトにしておく。--wait による再試行が全体の待ち時間を
        # 確保するので、ここは 1 回あたりの応答揺らぎを吸収する程度でよい。
        "probe_timeout_seconds": 5.0,
    }, config.base_dir)
    if not wait_for_engine(engine, args.wait):
        print("VOICEVOX ENGINE に接続できません: {0}".format(args.base_url), file=sys.stderr)
        if args.wait > 0:
            print("{0:.0f} 秒待ちましたが応答がありませんでした。".format(args.wait),
                  file=sys.stderr)
        print("次を確認してください。", file=sys.stderr)
        print("  - 疎通確認: curl -s {0}/version".format(args.base_url), file=sys.stderr)
        print("  - Docker Desktop（Windows）を使っている場合、WSL2 から 127.0.0.1 では"
              " VOICEVOX ENGINE に届かないことがあります。"
              "--base-url でホストの IP を指定してください"
              "（例: --base-url http://<ホストのIP>:50021）。", file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)
    manifest_path = os.path.join(args.out, "manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)

    phrases = collect_phrases(config, args.include_quotes)
    print("{0} 件の文言を生成します（話者 {1}）。".format(len(phrases), args.speaker))

    failures = 0
    for phrase in phrases:
        filename = "{0}.wav".format(_digest(phrase))
        path = os.path.join(args.out, filename)
        manifest[phrase] = filename
        if os.path.exists(path) and not args.force:
            print("  skip {0}".format(phrase))
            continue
        try:
            engine.synthesize(phrase, path)
        except TTSError as exc:
            print("  NG   {0}: {1}".format(phrase, exc), file=sys.stderr)
            failures += 1
            continue
        print("  OK   {0} -> {1}".format(phrase, filename))

    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print("manifest を書き出しました: {0}".format(manifest_path))

    if failures:
        print("{0} 件が失敗しました。".format(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
