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
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chime.config import Config, ConfigError, load_config  # noqa: E402
from chime.jsonfile import JsonFileError, read_json, write_json_atomic  # noqa: E402
from chime.phrases import (  # noqa: E402
    collect_phrases, find_stale_entries, phrases_in_use, phrases_to_generate, phrases_to_keep)
from chime.tts import (  # noqa: E402
    MANIFEST_FILENAME, TTSError, VoicevoxEngine, prerecorded_filename)

# 文言の列挙は ``chime.phrases`` に移した（pygame を引き込まずに使えるよう、
# 再生系を import しないモジュールにしてある）。CI やテストが従来どおり
# ``from generate_voicevox import collect_phrases`` で引けるよう、再公開しておく。
__all__ = [
    "collect_phrases", "find_stale_entries", "main", "phrases_in_use",
    "phrases_to_generate", "phrases_to_keep", "wait_for_engine",
]

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


def _build_parser(config: Config, config_path: Optional[str]) -> argparse.ArgumentParser:
    """コマンドライン引数の定義。他のオプションの既定値は ``config`` から決める。"""
    voicevox = config.section("tts.voicevox")

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", metavar="PATH", default=config_path,
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
    return parser


def _report_engine_unreachable(args: argparse.Namespace) -> None:
    """VOICEVOX ENGINE に繋がらなかったときの案内を stderr に出す。"""
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


def _prune(manifest: Dict[str, str], stale: List[Tuple[str, str]],
           out_dir: str, has_config: bool) -> None:
    """古いエントリ（``stale``）の WAV を消し、``manifest`` からも外す。"""
    if not has_config:
        print("警告: --config なしで --prune を指定しています。"
              "Pi の config.json で足した文言（地点など）は、"
              "--config を付けないと消えます。", file=sys.stderr)
    for phrase, filename in stale:
        path = os.path.join(out_dir, filename)
        if os.path.exists(path):
            os.remove(path)
            print("  prune {0} -> {1}".format(phrase, filename))
        else:
            print("  prune {0} -> {1}（ファイルなし）".format(phrase, filename))
        del manifest[phrase]
    print("{0} 件の古いエントリを削除しました。".format(len(stale)))


def _remove_quietly(path: str) -> None:
    """``path`` を消す。無い・消せないときは何もしない（後始末用）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def _synthesize_to(engine: VoicevoxEngine, phrase: str, path: str) -> None:
    """``phrase`` の WAV を ``path`` へ作る。途中で止まっても欠けた WAV を ``path`` に残さない。

    同じディレクトリの一時ファイルに書いてから ``os.replace`` で置き換える。
    ``path`` に書きかけが残ると、次の実行で「生成済み」として飛ばされ、欠けた声が
    そのまま使われてしまうため。失敗・中断（Ctrl-C を含む）では一時ファイルを消す。
    ``--force`` で作り直すときも、置き換えるまで元の WAV は残る。
    """
    temp_path = "{0}.{1}.tmp".format(path, os.getpid())
    try:
        engine.synthesize(phrase, temp_path)
        os.replace(temp_path, path)
    except BaseException:
        _remove_quietly(temp_path)
        raise


def _synthesize_all(engine: VoicevoxEngine, phrases: List[str], out_dir: str,
                    manifest: Dict[str, str], force: bool) -> int:
    """``phrases`` を合成して ``manifest`` に登録する。失敗した件数を返す。"""
    failures = 0
    for phrase in phrases:
        filename = prerecorded_filename(phrase)
        path = os.path.join(out_dir, filename)
        if os.path.exists(path) and not force:
            manifest[phrase] = filename
            print("  skip {0}".format(phrase))
            continue
        try:
            _synthesize_to(engine, phrase, path)
        except TTSError as exc:
            # 合成できなかった文言は manifest に書かない（WAV が無いのに
            # エントリだけが manifest に残ってしまうため）。
            print("  NG   {0}: {1}".format(phrase, exc), file=sys.stderr)
            failures += 1
            continue
        manifest[phrase] = filename
        print("  OK   {0} -> {1}".format(phrase, filename))
    return failures


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

    args = _build_parser(config, pre_args.config).parse_args(argv)

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
        _report_engine_unreachable(args)
        return 1

    os.makedirs(args.out, exist_ok=True)
    manifest_path = os.path.join(args.out, MANIFEST_FILENAME)
    manifest = {}
    if os.path.exists(manifest_path):
        # 厳密に読む。実行時の PrerecordedEngine のように壊れた目録を空として
        # 扱うと、このあとの書き出しで既存の登録がすべて消えてしまうため、
        # 読めなければ何も合成せず・何も書かずに止める。
        try:
            manifest = read_json(manifest_path)
        except JsonFileError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    phrases = phrases_to_generate(config, args.include_quotes)
    print("{0} 件の文言を生成します（話者 {1}）。".format(len(phrases), args.speaker))

    stale = find_stale_entries(manifest, phrases_to_keep(config))
    if args.prune:
        _prune(manifest, stale, args.out, bool(args.config))
    elif stale:
        print("{0} 件の古いエントリが残っています（--prune を付けると削除されます）。"
              .format(len(stale)))

    failures = _synthesize_all(engine, phrases, args.out, manifest, args.force)

    # 一時ファイル経由で置き換えるので、途中で止まっても manifest が書きかけにならない。
    write_json_atomic(manifest_path, manifest, sort_keys=True)
    print("manifest を書き出しました: {0}".format(manifest_path))

    if failures:
        print("{0} 件が失敗しました。".format(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
