#!/usr/bin/env python3
"""VOICEVOX ENGINE で定型文の音声を事前生成する。

VOICEVOX ENGINE は Raspberry Pi 3B 上で常時動かすには重いため、
**PC 側で本スクリプトを実行して WAV を作り、リポジトリに同梱する**という
使い方を想定している。生成物は ``assets/voice/`` に置かれ、実行時は
``prerecorded`` エンジンがこれを最優先で使う（合成処理は発生しない）。

天気予報（``chime.weather.prerecord_phrases`` が列挙する語彙）は
``--include-quotes`` の指定に関わらず常に事前生成対象になる。天気だけ
作り置きが無いと、Pi 上で天気の文だけ無音になってしまうため
（v5.0.0 で実行時合成のフォールバックを廃した）。

使い方（PC 側で VOICEVOX を起動した状態で）::

    python3 scripts/generate_voicevox.py
    python3 scripts/generate_voicevox.py --base-url http://192.168.1.10:50021
    python3 scripts/generate_voicevox.py --speaker 3 --include-quotes
    python3 scripts/generate_voicevox.py --config pi-config.json --include-quotes --prune

Docker で VOICEVOX ENGINE を起動した直後はモデル読み込みのため
``/version`` がしばらく応答しないことがある。既定では起動を最大 90 秒
待つ（``--wait 0`` で待たずに即座に判定する）。

文言（定型文の言い回しなど）を変更すると、古い文言の manifest エントリと
WAV が ``assets/voice/`` に残り続ける（マージ書き込みのため）。
``--prune`` を付けると、ひとことを含む現在の全文言に無い古いエントリと
WAV を削除する（``--include-quotes`` の有無に関わらず、ひとことは使用中
として残す）。既定は off で、誤って消さないよう明示的に指定した場合のみ動く::

    python3 scripts/generate_voicevox.py --include-quotes --prune

Pi の ``config.json`` で文言を足している（地点を増やした、ひとことのファイルを
差し替えたなど）場合は、その ``config.json`` を PC に持ってきて ``--config`` で
渡す。生成する文言は「``--config`` の設定の文言」に「既定設定の文言」を足した
もの（和集合）になる。``config.json`` の配列は既定値を丸ごと置き換えるため
（例: 地点に京都だけを書くと既定の大津が外れる）、既定設定の文言は常に含めて、
同梱の作り置きを崩さないようにしている。``--prune`` で残す文言も同じ和集合で
判定する。``--config`` なしで ``--prune`` を付けると、Pi の ``config.json`` で
足した文言の作り置きは消える（警告を出す）::

    scp pi@<Pi のホスト名>:/home/pi/campus-chime/config.json ./pi-config.json
    python3 scripts/generate_voicevox.py --config pi-config.json --include-quotes --prune

生成後は ``assets/voice/`` を git add してコミットし、Pi 側で git pull する。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chime import timesignal, weather  # noqa: E402
from chime.config import BASE_DIR, DEFAULT_CONFIG, Config, ConfigError, load_config  # noqa: E402
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


def _unique(phrases) -> list:
    """空でない文言を、最初に現れた順のまま重複なしで返す。"""
    seen, unique = set(), []
    for phrase in phrases:
        if phrase and phrase not in seen:
            seen.add(phrase)
            unique.append(phrase)
    return unique


def collect_phrases(config, include_quotes: bool) -> list:
    """事前生成する文言を集める。

    天気予報の文言（``chime.weather.prerecord_phrases``）は
    ``include_quotes`` の指定に関わらず常に含める。天気だけ作り置きが
    無いと、Pi 上で天気の文だけ無音になってしまうため。
    ``weather.enabled`` が False の場合も同様に含める
    （あとで有効化したときに作り置きが無くて困るより、常に列挙しておく
    ほうが安全という判断）。
    """
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

    phrases.extend(weather.prerecord_phrases(config.section("weather")))
    return _unique(phrases)


def phrases_in_use(config) -> list:
    """現在使われている全文言（ひとことを含む）を返す。

    ``--prune`` の判定に使う。ひとことを今回作り直さない
    （``--include-quotes`` を付けない）場合でも、ひとことは使われているので、
    その音声を消してはならない。
    """
    return collect_phrases(config, include_quotes=True)


def default_config() -> Config:
    """既定設定だけの :class:`Config`（現地の ``config.json`` は読まない）。"""
    return Config(DEFAULT_CONFIG, base_dir=BASE_DIR)


def phrases_to_generate(config, include_quotes: bool) -> list:
    """生成する文言。``config`` の文言に、既定設定の文言を足したもの（和集合）。

    ``config.json`` の配列は既定値を丸ごと置き換える（地点に京都だけを書くと
    既定の大津が外れる）。``--config`` で渡した設定の文言だけを作ると、
    同梱の作り置きにある文言が抜けてしまうため、既定設定の文言は常に含める。
    順序は ``config`` の文言が先で、重複は除く。
    """
    return _unique(collect_phrases(config, include_quotes)
                   + collect_phrases(default_config(), include_quotes))


def phrases_to_keep(config) -> list:
    """``--prune`` で残す文言。``config`` の使用中の文言に、既定設定の分を足したもの。"""
    return _unique(phrases_in_use(config) + phrases_in_use(default_config()))


def find_stale_entries(manifest: dict, keep_phrases) -> list:
    """``manifest`` のうち、``keep_phrases`` に含まれないエントリを列挙する。

    実際の削除は行わない（呼び出し側が ``--prune`` のときだけ削除に使う）。
    削除前に「何が消えるか」を確認できるよう、判定と実行を分けている。
    """
    keep = set(keep_phrases)
    return [(phrase, filename) for phrase, filename in manifest.items() if phrase not in keep]


def main(argv=None) -> int:
    # --config だけ先に読み、その設定から他のオプションの既定値を決める
    # （それ以外のオプションは、ここでは読み飛ばす）。
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config")
    pre_args, _ = pre_parser.parse_known_args(argv)

    try:
        config = load_config(pre_args.config)
    except ConfigError as exc:
        print("設定エラー: {0}".format(exc), file=sys.stderr)
        return 2
    voicevox = config.section("tts.voicevox")

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", metavar="PATH", default=pre_args.config,
                        help="設定ファイル（Pi の config.json を PC に持ってきて指定する。"
                             "その設定の文言に既定設定の文言を足して生成する。"
                             "既定: リポジトリ直下の config.json があれば読み込む）")
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
    parser.add_argument("--prune", action="store_true",
                        help="ひとことを含む現在の全文言に無い古い manifest エントリと"
                             "対応する WAV を削除する（--include-quotes の有無に"
                             "関わらず、ひとことは残す。既定 off。誤って消さないよう"
                             "明示的に指定した場合のみ動く）")
    args = parser.parse_args(argv)

    engine = VoicevoxEngine({
        "base_url": args.base_url,
        "speaker": args.speaker,
        "timeout_seconds": 60.0,
        # 起動待ちで繰り返す疎通確認は、実行時（既定 2 秒）より少し長めの
        # タイムアウトにしておく。--wait による再試行が全体の待ち時間を
        # 確保するので、ここは 1 回あたりの応答揺らぎを吸収する程度でよい。
        "probe_timeout_seconds": 5.0,
    })
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

    phrases = phrases_to_generate(config, args.include_quotes)
    print("{0} 件の文言を生成します（話者 {1}）。".format(len(phrases), args.speaker))

    stale = find_stale_entries(manifest, phrases_to_keep(config))
    if args.prune:
        if not args.config:
            print("警告: --config なしで --prune を指定しています。"
                  "Pi の config.json で足した文言（地点など）は、"
                  "--config を付けないと消えます。", file=sys.stderr)
        for phrase, filename in stale:
            path = os.path.join(args.out, filename)
            if os.path.exists(path):
                os.remove(path)
                print("  prune {0} -> {1}".format(phrase, filename))
            else:
                print("  prune {0} -> {1}（ファイルなし）".format(phrase, filename))
            del manifest[phrase]
        print("{0} 件の古いエントリを削除しました。".format(len(stale)))
    elif stale:
        print("{0} 件の古いエントリが残っています（--prune を付けると削除されます）。"
              .format(len(stale)))

    failures = 0
    for phrase in phrases:
        filename = "{0}.wav".format(_digest(phrase))
        path = os.path.join(args.out, filename)
        if os.path.exists(path) and not args.force:
            manifest[phrase] = filename
            print("  skip {0}".format(phrase))
            continue
        try:
            engine.synthesize(phrase, path)
        except TTSError as exc:
            # 合成できなかった文言は manifest に書かない（WAV が無いのに
            # エントリだけが manifest に残ってしまうため）。
            print("  NG   {0}: {1}".format(phrase, exc), file=sys.stderr)
            failures += 1
            continue
        manifest[phrase] = filename
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
