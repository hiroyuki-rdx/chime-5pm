"""チャイムが読み上げうる全文言の列挙。

ここで数えた文言が、そのまま「作り置きしておかなければならない文言」になる。
Pi には実行時の音声合成が無く、声は文言の**完全一致**で作り置き
（``assets/voice/manifest.json``）から引くため、列挙から漏れた文言はその文だけ
無音になる。列挙の結果は ``scripts/generate_voicevox.py``（WAV の生成と
``--prune``）・テスト・CI の ``prerecorded-only`` ジョブが使う。

このモジュールは ``chime.audio`` / ``chime.sequence`` / ``chime.app`` を
import しない（pygame を引き込まないため）。生成スクリプトは PC 側で動くので、
再生系の依存が無くても使えるようにしておく。
"""

from __future__ import annotations

from typing import Iterable, Iterator, List, Mapping, Tuple

from . import scheduler, timesignal, weather
from .config import BASE_DIR, DEFAULT_CONFIG, Config
from .quotes import load_quotes

# 既知の取り決め: 実行時の照合（``TTSService``）は文言の前後の空白を除いて引くが、
# ここでの列挙は除かない。現状のまま揃えてある（テンプレートや設定が前後に空白を
# 含む文言を作らない限り食い違わない）。変えると作り置きの対象が動くので、
# 変えるときは作り置きの作り直しとセットにすること。


def _unique(phrases: Iterable[str]) -> List[str]:
    """空でない文言を、最初に現れた順のまま重複なしで返す。"""
    return list(dict.fromkeys(phrase for phrase in phrases if phrase))


def announcement_phrases(config: Config) -> Iterator[str]:
    """時報の読み上げ文言を、``start_hour``〜``end_hour`` の時刻ごとに返す。

    重複は除かず、``skip_hours`` も見ない（休みにしている時刻の文言も作り置き
    しておく）。ジェネレーターなので、テンプレートが壊れていても例外が出るのは
    その時刻まで進んだときになる（呼び出し側が 1 件ずつ処理できる）。
    """
    settings = config.section("time_signal")
    for hour in scheduler.hourly_hours(config.section("schedule.hourly")):
        yield timesignal.announce_text(hour, settings)


def closing_extra_text(config: Config) -> str:
    """閉館放送の最後に足す文言（``closing.extra_text``）。無ければ空文字列。

    実行時の組み立てと作り置きの列挙が同じ読み方をするよう、読み出しを
    ここに一本化している。
    """
    return str(config.get("closing.extra_text", "") or "")


def closing_phrases(config: Config) -> List[str]:
    """閉館放送に足す文言（設定されていれば 1 件、無ければ空）。"""
    text = closing_extra_text(config)
    return [text] if text else []


def quote_phrases(config: Config) -> List[str]:
    """ひとことの全文言を、``general``、``by_hour``（ファイルの順）の順に返す。"""
    quotes = load_quotes(config.path("quotes.file"))
    phrases = list(quotes.get("general", []))
    for values in quotes.get("by_hour", {}).values():
        phrases.extend(values)
    return phrases


def collect_phrases(config: Config, include_quotes: bool) -> List[str]:
    """事前生成する文言を集める。

    天気予報の文言（``chime.weather.prerecord_phrases``）は
    ``include_quotes`` の指定に関わらず常に含める。天気だけ作り置きが
    無いと、Pi 上で天気の文だけ無音になってしまうため。
    ``weather.enabled`` が False の場合も同様に含める
    （あとで有効化したときに作り置きが無くて困るより、常に列挙しておく
    ほうが安全という判断）。
    """
    phrases = list(announcement_phrases(config)) + closing_phrases(config)
    if include_quotes:
        phrases += quote_phrases(config)
    phrases += weather.prerecord_phrases(config.section("weather"))
    return _unique(phrases)


def phrases_in_use(config: Config) -> List[str]:
    """現在使われている全文言（ひとことを含む）を返す。

    ``--prune`` の判定に使う。ひとことを今回作り直さない
    （``--include-quotes`` を付けない）場合でも、ひとことは使われているので、
    その音声を消してはならない。
    """
    return collect_phrases(config, include_quotes=True)


def _default_config() -> Config:
    """既定設定だけの :class:`Config`（現地の ``config.json`` は読まない）。"""
    return Config(DEFAULT_CONFIG, base_dir=BASE_DIR)


def phrases_to_generate(config: Config, include_quotes: bool) -> List[str]:
    """生成する文言。``config`` の文言に、既定設定の文言を足したもの（和集合）。

    ``config.json`` の配列は既定値を丸ごと置き換える（地点に京都だけを書くと
    既定の大津が外れる）。``--config`` で渡した設定の文言だけを作ると、
    同梱の作り置きにある文言が抜けてしまうため、既定設定の文言は常に含める。
    順序は ``config`` の文言が先で、重複は除く。
    """
    return _unique(collect_phrases(config, include_quotes)
                   + collect_phrases(_default_config(), include_quotes))


def phrases_to_keep(config: Config) -> List[str]:
    """``--prune`` で残す文言。ひとことを含めて生成する文言と同じ（和集合）。"""
    return phrases_to_generate(config, include_quotes=True)


def find_stale_entries(manifest: Mapping[str, str],
                       keep_phrases: Iterable[str]) -> List[Tuple[str, str]]:
    """``manifest`` のうち、``keep_phrases`` に含まれないエントリを列挙する。

    実際の削除は行わない（呼び出し側が ``--prune`` のときだけ削除に使う）。
    削除前に「何が消えるか」を確認できるよう、判定と実行を分けている。
    """
    keep = set(keep_phrases)
    return [(phrase, filename) for phrase, filename in manifest.items() if phrase not in keep]
