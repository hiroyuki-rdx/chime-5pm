# キャンパス時報システム 仕様書

**プロジェクト名:** Campus Chime System
**バージョン:** 6.2.0
**対応要件定義:** `docs/REQUIREMENTS.md` v6.2.0
**作成日:** 2026/08/26

---

## 1. 本書の位置づけ

要件定義書が「**何を・なぜ**作るか」を定めるのに対し、本書は「**どう実装するか**」を定める。実装・改修時は本書を正とする。

---

## 2. ディレクトリ構成

設置先は `/home/pi/campus-chime` に**統一する**（v1.x に存在した `~/steam5pm` 表記は全廃）。

```
/home/pi/campus-chime/
├── campus_chime.py            # エントリポイント（chime.cli.run を呼ぶだけ）
├── campus_chime.service       # systemd ユニット定義
├── config.example.json        # 設定の雛形（DEFAULT_CONFIG と一致）
├── config.json                # 現地設定（Git 管理外・任意）
├── chime/                     # アプリケーション本体（下記 4 章）
├── assets/
│   ├── announce.wav           # 閉館アナウンス（VOICEVOX:ずんだもん / 24kHz mono）
│   │                          #   「午後五時をお知らせするのだ。とっとと帰るのだ」
│   ├── hotaru.mp3             # 蛍の光（Public Domain / 44.1kHz stereo）
│   ├── quotes.json            # 「ひとこと」定義
│   ├── voice/                 # 事前生成音声＋ manifest.json
│   └── generated/             # 実行時に生成される時報音（Git 管理外）
├── cache/                     # 状態・放送の履歴・TTS キャッシュ（Git 管理外）
│   ├── state.json
│   ├── history.jsonl          #   放送ごとの結果（4.16 章。--status で直近を表示）
│   ├── pause.json             #   「今日だけ止める」の記録（4.21 章。--pause-* が書く。無ければ止めていない）
│   ├── deployed_commit        #   サービスに反映した版のコミット（setup.sh が書き、update.sh が読む。4.19 章）
│   └── tts/
├── scripts/                   # 導入・更新・音声の作り直しなど（4.19 章）
├── tests/
└── docs/
```

### 2.1 v2.0.0 設計案からの構成変更

| 対象 | 変更 | 理由 |
|---|---|---|
| `campus_chime.py` | 単一ファイル → `chime/` パッケージ ＋ 薄いエントリポイント | 時報・天気・TTS の追加により単一ファイルでは責務が過大になったため |
| `config.example.json` | 新規追加 | 時刻・文言・地域をコードから外出しするため（FR-10） |
| `cache/state.json` | 新規追加 | 再起動をまたぐ二重再生防止（FR-06） |
| `cache/history.jsonl` | 新規追加（v6.1.0） | 放送ごとの結果を残し、`--status` で振り返れるようにするため（FR-12） |
| `chime/configcheck.py` / `check.py` / `status.py` / `history.py` / `buildinfo.py` | 新規追加（v6.1.0） | 現地で、鳴らさず・書かずに設定・設置状態・いまの状態を確かめるため（4.13〜4.17 章、FR-11・FR-12） |
| `chime/closures.py` / `chime/pause.py` / `cache/pause.json` | 新規追加（v6.2.0） | 休館日の暦と、「今日だけ止める」の記録。行事や点検の日に、サービスを止めずに放送だけを止めるため（4.20・4.21 章、FR-14・FR-15） |
| `campus_chime.service` | `TimeoutStopSec=30` を追加（v6.2.0） | 停止要求で再生が止まるようにしたため、止まらなかったときの保険の待ち時間を、既定の 90 秒から縮める（6 章、FR-17） |
| `scripts/update.sh` | 新規追加（v6.1.0） | 運用中の Pi を、放送を途切れさせずに最新版へ更新するため（4.19 章、FR-13） |
| `tests/` | 新規追加 | NFR-07 |
| `scripts/` | 新規追加 | 導入手順の自動化・音声事前生成 |
| `.github/workflows/ci.yml` | 新規追加 | NFR-07 |
| `.github/workflows/tag.yml` | 新規追加 | `main` へのマージで版のタグを自動で付けるため（10.1 章） |
| `scripts/tag_releases.py` | 新規追加 | 版のタグ付けの本体。`CHANGELOG.md` の版見出しのリンク（`releases/tag/vX.Y.Z`）の行き先を用意するため |
| `docs/LEGACY_SYSTEM_SHUTDOWN.md` | **削除** | `weather.py` は OS ごと廃止されるため、停止手順が不要になる |

---

## 3. 設定仕様

### 3.1 読み込み順序

後に読んだものが優先される（辞書は再帰的にマージ、配列は置換）。

1. `chime/config.py` の `DEFAULT_CONFIG`（仕様上の正）
2. リポジトリ直下の `config.json`（存在すれば。Git 管理外）
3. `--config PATH` で明示指定したファイル

`config.example.json` は `DEFAULT_CONFIG` をそのまま書き出したもので、`tests/test_config.py` が両者の一致を検証する。既定値を変更したら `python3 scripts/dump_example_config.py` を実行すること。

### 3.2 設定項目

#### `timezone`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `timezone` | `"Asia/Tokyo"` | 判定・ログ表示に使うタイムゾーン |

#### `schedule`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `hourly.enabled` | `true` | 時報の有効／無効 |
| `hourly.start_hour` | `10` | 時報の開始時（時） |
| `hourly.end_hour` | `16` | 時報の終了時（時。この時刻も含む） |
| `hourly.minute` | `0` | 時報を鳴らす分 |
| `hourly.weekdays` | `[0,1,2,3,4]` | 稼働曜日（月=0 〜 日=6） |
| `hourly.skip_hours` | `[]` | 除外する時（例: `[12]`）。12 時を除外すると、天気予報（既定は 12 時だけ）も流れなくなる |
| `closing.enabled` | `true` | 閉館放送の有効／無効 |
| `closing.hour` / `closing.minute` | `16` / `57` | 閉館放送の時刻 |
| `closing.weekdays` | `[0,1,2,3,4]` | 稼働曜日 |
| `pip_lead_seconds` | `null` | 正時より何秒前に再生を始めるか。`null` なら `time_signal` から自動計算 |
| `prepare_lead_seconds` | `45.0` | 天気取得・音声合成を何秒前に済ませるか |
| `catchup_grace_seconds` | `120.0` | 出遅れた場合に追いかけ再生を許す秒数 |
| `max_sleep_seconds` | `30.0` | 待機 sleep の分割単位（時刻補正への追従用）。休止の記録の書き換えに気づくまでの最長でもあり、`catchup_grace_seconds` より短くしておく（以上だと、再生開始の直前に休止を解いた回を取りこぼすことがある） |
| `closures.dates` | `[]` | 休館日（1 日ずつ）。`"2026-11-02"` または `{"date": "2026-11-02", "name": "学園祭"}`。書いた日は時報も閉館放送も鳴らさない。**祝日は自動では休まない**（4.20 章） |
| `closures.ranges` | `[]` | 休館日（期間）。`{"from": "2026-12-28", "to": "2027-01-04", "name": "年末年始"}`（両端を含む）。`name` は省ける |

休館日の項目は、書き方が正しくなければ**その項目だけ**を使わず（その日は鳴る）、ほかの項目は使う（4.20 章、3.5 章）。`dates` と `ranges` は配列なので、`config.json` では丸ごと置き換わる。

#### `audio`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `backend` | `"auto"` | `auto` / `pygame` / `command` / `mock` |
| `mixer.frequency` | `44100` | サンプリング周波数（Hz） |
| `mixer.size` | `-16` | サンプルフォーマット（符号付き 16bit） |
| `mixer.channels` | `2` | ステレオ |
| `mixer.buffer` | `4096` | **内部バッファサイズ（サンプル）** |
| `gap_ms` | `350` | セグメント間の無音（ミリ秒） |
| `fade_in_ms` | `2000` | 蛍の光のフェードイン（ミリ秒） |
| `commands` | `aplay` / `mpg123` | `command` バックエンドで使う外部プレイヤー |
| `mock_max_seconds` | `3.0` | mock が 1 ファイルに費やす最大秒数 |
| `volume.time_signal` | `1.0` | 時報音（ポ・ポ・ポ・ポーン）の再生音量（0.0〜1.0）。`time_signal.volume`（時報音を作るときの波形の大きさ）とは別 |
| `volume.voice` | `1.0` | 読み上げ（時刻・天気・ひとこと・閉館アナウンス）の再生音量（0.0〜1.0） |
| `volume.music` | `1.0` | 蛍の光の再生音量（0.0〜1.0） |
| `max_segment_seconds` | `600` | 長さの分からない音源（MP3 など）を鳴らしてよい最大の秒数。超えると打ち切る（WAV は長さから決めるので、この設定は使わない。4.3 章） |

**音量の決まり。** OS（ALSA）の音量は高めに固定し、小さくしたいときはここで絞る（SETUP.md 4-2・7 章）。キーは音源の種類（`Segment.kind`。4.3 章）。数として読めない値（文字列・`null`・真偽値・`NaN`）は 1.0（今までの音量）、範囲外は 0〜1 に丸める（負の値は 0.0 で無音）。どちらも設定の間違いなので、キーごとにプロセスで 1 回だけ WARNING を残し、放送は止めない（`--check` は警告にする。3.5 章）。外部コマンド（`command`）で再生する方式は音量を変えられない（4.3 章）。

#### `time_signal`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `short_pip.frequency` / `duration_ms` | `440.0` / `100` | 短音「ポ」 |
| `long_pip.frequency` / `duration_ms` | `880.0` / `1000` | 長音「ポーン」 |
| `short_pip_count` | `3` | 短音の回数 |
| `pip_interval_ms` | `1000` | 短音の間隔（＝ 1 拍の長さ） |
| `volume` | `0.6` | 振幅（0.0〜1.0） |
| `envelope_ms` | `5` | クリックノイズ防止のフェード |
| `output_file` | `assets/generated/time_signal.wav` | 生成先 |
| `announce_template` | `"{period}{hour_reading}をお知らせしたのだ。"` | 読み上げテンプレート。`{hour_reading}` は誤読対策込みの時刻表現、`{hour}` は後方互換の数値のみのプレースホルダ |
| `use_noon_template` | `true` | 12 時に専用文言を使うか |
| `noon_template` | `"正午をお知らせしたのだ。"` | 12 時の文言 |
| `period_am` / `period_pm` | `"午前"` / `"午後"` | テンプレートの `{period}` |
| `hour_readings` | `{"0":"れいじ","4":"よじ","7":"しちじ","9":"くじ"}` | 読み上げエンジンが誤読する時刻（12 時間表記）だけをかな書きで上書きする。キーは文字列。それ以外の時刻は正しく読めるため上書きしない（詳細は 4.2 章） |

#### `extra_segment`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `enabled` | `true` | おまけ放送（天気予報・ひとこと）の有効／無効。`false` なら時報のあとは何も流さず、時報音と時刻アナウンスだけになる |
| `weather_hours` | `[12]` | 天気予報を流す正時。既定は **12 時の 1 回だけ**（v5.0.0 までは `[10, 12, 14, 16]` の 2 時間おき）。ここに無い時刻は「ひとこと」だけになり、天気 API も呼ばない。空リストなら一度も流さない。要素は文字列でも可（`int` に正規化し、変換できない要素は警告して無視）。**閉館放送は別経路のため、ここに何を書いても天気は付かない** |

時報のあとの放送は**常に 1 経路**で、抽選はしない（4.9 章）。時報音 → 時刻アナウンス → （`weather_hours` の時刻で、かつ `weather.enabled` のときだけ）天気予報 → ひとこと 1 つ。天気を流す時刻でも流さない時刻でも、ひとことは必ず 1 つ流れる。

#### `quotes`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `file` | `assets/quotes.json` | ひとこと定義ファイル |
| `avoid_recent` | `8` | 直近この件数と同じものは選ばない |

#### `weather`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `enabled` | `true` | 天気予報の有効／無効。無効にすると、`weather_hours` の時刻でも天気は流れず、時報のあとは「ひとこと」だけになる（警告を 1 件記録する） |
| `timeout_seconds` | `8.0` | HTTP タイムアウト |
| `cache_minutes` | `60` | 取得結果のキャッシュ時間 |
| `open_meteo.locations` | 大津 の 1 件（v5.1.0 までは 大津 / 京都 の 2 件） | 読み上げる地点の配列（`label` / `latitude` / `longitude`）。**上から順に読む**。増やすと 1 か所あたり約 6 秒放送が長くなる。増やした地点の文は作り置きが無いので、PC で読み上げ音声を作り直すこと（作り直すまでその地点の文は無音になる）。配列は `config.json` で丸ごと置き換わる（deep merge されない）ため、地点を足すときは大津も含めて全地点を書く。`label` はそのまま読み上げられるので、読み間違えない表記にすること |
| `sentence_weather` | `"今の{label}の天気は{weather}なのだ。"` | 天気の文。**現在の天気**を読む |
| `sentence_temp` | `"気温は{temp}度なのだ。"` | 気温の文。**現在の気温**を読む |
| `sentence_temp_max` | `""` | 今日の最高気温の文。既定は空＝読まない |
| `sentence_pop` | `""` | 今日の降水確率の文。既定は空＝読まない |
| `prerecord.temp_min` / `temp_max` | `-5` / `40` | 事前生成する気温の範囲（両端含む） |
| `prerecord.pop_step` | `10` | 降水確率を丸める刻み（`sentence_pop` を有効にしたときに効く） |
| `prerecord.whens` | `["今日"]` | 事前生成する `{when}` の一覧（予報の文を有効にしたときに効く） |

**読み上げを 1 文ずつに分けている理由。** 地名・気温をそれぞれ別の文にすると、各文の語彙が
有限に収まり、全パターンを VOICEVOX で事前生成できる（`assets/voice/`）。1 文にまとめると
値の組み合わせが爆発して事前生成できず、**その文だけ無音になる**（他の文・時報音は鳴る）。
テンプレートを空文字列にするとその文を読まない。

**取得先が Open-Meteo だけである理由。** 天気の文面を自由文のまま読むと語彙が閉じず、事前生成
できない（Pi には実行時の音声合成が無いので、その文だけ無音になる）。Open-Meteo は天気コード
（`WMO_CODES`・28 語）で語彙が閉じるため、気温の文と合わせて全パターンを作り置きできる。
気象庁の取得先は、予報文が自由文で作り置きできなかったため、v6.0.0 で削除した（5.3.0 で
予告）。

#### `tts`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `engines` | `["prerecorded","voicevox"]` | 上から順に試す。どちらでも合成できない文言はその 1 文だけ無音になる（放送は続く） |
| `cache_dir` | `cache/tts` | 合成結果のキャッシュ先 |
| `prerecorded_dir` | `assets/voice` | 事前生成音声の置き場 |
| `voicevox.base_url` | `http://127.0.0.1:50021` | VOICEVOX ENGINE の URL |
| `voicevox.speaker` | `3` | 話者 ID（3 = ずんだもん・ノーマル） |
| `voicevox.probe_timeout_seconds` | `2.0` | 疎通確認（`GET /version`）のタイムアウト。放送直前に呼ばれる実行時は短く保つ既定値で、`scripts/generate_voicevox.py` の起動待ちでは長めの値を設定して使う（4.4 章参照） |

**v5.0.0 で `open_jtalk` エンジンをコードごと削除した。** 古い `config.json` が
`engines` に `"open_jtalk"` を残していても、`engines` に含まれる未知のエンジン名は
警告ログ（`未知の TTS エンジン '...' は無視します。`）を出して無視されるだけで、
動作には影響しない。

#### `closing` / `state` / `logging`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `closing.announce_file` | `assets/announce.wav` | 閉館アナウンス音源 |
| `closing.music_file` | `assets/hotaru.mp3` | 楽曲 |
| `closing.extra_text` | `""` | 空でなければアナウンスと楽曲の間に読み上げを挿入 |
| `state.file` | `cache/state.json` | 再生状態の保存先 |
| `state.history_file` | `cache/history.jsonl` | 放送の履歴の保存先（4.16 章）。空文字列なら履歴を残さない |
| `state.pause_file` | `cache/pause.json` | 「今日だけ止める」の記録の保存先（4.21 章）。空文字列なら止める機能を使わない（`--pause-*` は断り、読んでも「止めていない」になる） |
| `logging.level` | `"INFO"` | ログレベル |
| `logging.format` | `"%(asctime)s - %(levelname)s - %(name)s - %(message)s"` | ログ書式 |

### 3.3 `mixer.buffer` に関する設計判断

pygame 2.x の `mixer.init()` は buffer 既定値が **512 サンプル**であり、これは低速機ではバッファアンダーラン（音飛び・カクつき）の原因となる。Pi 3B では余裕を持たせ **4096** を指定する。

- バッファは 2 の累乗であること（非累乗値は切り上げられる）
- 大きくすると再生遅延が増えるが、本システムは即応性を要求しないため許容できる
- 4096 でも改善しない場合は 8192 まで引き上げてよい

なお音源のサンプリング周波数は mixer 設定と一致していなくてよい（SDL_mixer が読み込み時に変換する）。ただし変換は CPU を使うため、可能なら 44.1kHz に揃えることが望ましい。

### 3.4 廃止した設定キー（v6.0.0）

次の 8 キーは v6.0.0 で廃止した（5.3.0 から起動時に「廃止予定」と警告していた）。`config.json` に残っていても起動は止まらず、起動時に警告して値を無視する（既定値が使われる。8 章）。

- 廃止した `extra_segment.mode` / `weather_probability` / `always_weather_hours` / `always_quote_hours` / `fallback_to_quote`: 天気を流す時刻は `extra_segment.weather_hours` で指定する。「天気かひとことのどちらか一方を選ぶ」方式（`mode="choice"`）は廃止した
- 廃止した `weather.provider` / `weather.jma` / `weather.max_weather_chars`: 天気は Open-Meteo（`weather.open_meteo`）に一本化した。気象庁の取得先は廃止した

起動時のログのほか、`python3 campus_chime.py --check` の「設定」にも、残っている行が警告として出る（「直し方: この行を消してください」。4.14 章）。

### 3.5 設定の検査（`chime/configcheck.py`）

設定ファイルの書き間違いが、再起動の繰り返し（起動時の例外）・CPU の空回り（待機時間を 0 秒にした）・9 時間のずれ（タイムゾーンの綴りを間違えると OS のローカル時刻になり、Pi では UTC のことが多い）といった形で表に出る前に、起動時と `--check` で見つける。検査の結果（`Finding`）は、次の 3 段階の重大度を持つ。

| 重大度 | 意味 | 起動時の扱い |
|---|---|---|
| error | **ランタイムが、その値では動けない**。起動・予定の計算・再生で例外になる、待機ループが CPU を使い続ける、OS のローカル時刻で動く（タイムゾーンが解決できない）、ログがすべて失われる（書式や水準の名前が使えない） | そのキー**だけ**を既定値に置き換えて続ける（`sanitized()`）。ERROR ログに、キー・内容・直し方・値の出どころの設定ファイルを 1 件ずつ残す。リストの壊れた要素が原因のときは、その要素だけを取り除き、残りは使う |
| warning | **ランタイムは動く**が、書いた意図と違う動きになる、または直したほうがよい（鳴らない時刻・読み上げの欠け・既定の文言への切り替え・知らないキー・廃止したキーなど） | **置き換えない**。`--check` が警告として示し、直し方を添える（廃止したキーと既定値の丸ごとコピーは、読み込むときの WARNING ログにも出る。8・9 章） |
| info | 動作は変わらない（既定値と同じ値を書いている） | 動作は変えない |

異常終了させず既定値に置き換えるのは、異常終了させても systemd の `Restart=always` が再起動を繰り返すだけで、時報が鳴らないままになるため。一方、置き換えるのは error だけに限る。ランタイムが例外なく動かせる値を置き換えると、前の版で動いていた放送が、設定を変えていないのに変わってしまう（たとえば `schedule.hourly.weekdays` に `7` が混ざった `[0, 1, 2, 3, 4, 5, 7]` は、土曜までは鳴り続ける。リスト全体を既定値に戻すと土曜の時報が消える）。error と warning の境目は、ランタイムの読み方そのものに合わせてあり、`tests/test_configcheck.py` が実際の `ChimeApp`・`Scheduler`・再生・ログに通して確かめる。

検査は 2 段に分かれる。`walk_overrides()` は設定ファイル（既定値への「差分」）を書かれたとおりに見て、知らないキー（近い綴りがあれば「もしかして …?」）・廃止したキーを warning、既定値と同じ値を info として挙げる（値の正しさは見ない）。`validate()` はマージ後の値を見て、次の表のとおり error と warning を挙げる。

| 対象 | error（既定値に置き換える） | warning（置き換えない） |
|---|---|---|
| 節（`{ … }` でまとめる項目） | 数値や文字列などに置き換わると、ランタイムが例外になるもの: `schedule.hourly`・`schedule.closing`（真の値のとき。予定を作る処理）、`audio.mixer`・`audio.commands`（`dict()` にできない値。再生の準備。`audio.commands` は、`audio.backend` が `mock` / `pygame` の間は表を読まないので warning）、`tts.voicevox`（`tts.engines` に `voicevox` があるときだけ） | 上記以外の節（`schedule`・`schedule.closures`・`audio`・`audio.volume`・`time_signal`・`extra_segment`・`quotes`・`weather`・`weather.open_meteo`・`weather.prerecord`・`tts`・`closing`・`state`・`logging` など）の置き換え。ランタイムは空の節として読むので、その節の機能が働かないだけ（例: `schedule` なら時報も閉館放送も鳴らない）。`schedule.hourly`・`schedule.closing` を `null`（や偽の値）にしたときも、その放送が鳴らないだけ（止めたいときは `enabled` を `false` にする） |
| 時・分 | `schedule.closing.hour`（0〜23）・`schedule.hourly.minute`・`schedule.closing.minute`（0〜59）が、整数として読めない、または範囲外（日時を作る処理が例外。その節の `enabled` が偽のときは warning）。`schedule.hourly.start_hour`・`schedule.hourly.end_hour` が整数として読めない。調べる範囲（`end_hour` − `start_hour` + 1）が 10,000,000 件を超える（予定を作るたびに全部を数えるので、1 日あたり約 0.6 秒、予定の探索では約 20 秒の CPU を使い続ける。10,000,000 件ちょうどまでは動かす（v6.0.0 と同じ）。超えたときは、0〜23 の外にあるほうの時（どちらも外なら両方）だけを既定値に戻す。v6.0.0 は超える範囲も動かしていたが、現実の書き間違い（`end_hour: 1600` など）はこれよりずっと小さい） | `"16"`・`16.0` のような数値の書き方の違い。`schedule.hourly.start_hour`・`schedule.hourly.end_hour` が 0〜23 の外（その時刻は鳴らない）。`start_hour` が `end_hour` より後ろ（時報は 1 回も鳴らない） |
| 曜日・休みにする時刻 | `schedule.hourly.weekdays`・`schedule.closing.weekdays`: 反復できない値（数値・`null` など）、またはハッシュできない要素（リスト・辞書）を含む。`schedule.hourly.skip_hours`: 反復できない、または `int()` で読めない要素を含む。どちらも、壊れた要素だけを取り除く（リストでなければ既定値に戻す） | `schedule.hourly.weekdays`・`schedule.closing.weekdays` の `7`・`"1"`・`null`（どの曜日にも一致しない）。`schedule.hourly.skip_hours` の範囲外・小数・文字列。`extra_segment.weather_hours` の壊れた要素（無視され、天気が流れないだけで、error にはならない） |
| 数値 | 読めない値（`int()` / `float()` で読めない。`null`・リスト・`"abc"` など）のうち、起動・予定の計算・再生が例外で止まるキー: `schedule.max_sleep_seconds`・`schedule.pip_lead_seconds`（`null` は可）・`schedule.prepare_lead_seconds`・`schedule.catchup_grace_seconds`、`audio.mixer.frequency`・`audio.mixer.size`・`audio.mixer.channels`・`audio.mixer.buffer`、`audio.gap_ms`、`audio.mock_max_seconds`、`time_signal.short_pip_count`・`time_signal.pip_interval_ms`、`quotes.avoid_recent`、`weather.timeout_seconds`・`weather.cache_minutes`、`tts.voicevox.speaker`・`tts.voicevox.timeout_seconds`・`tts.voicevox.probe_timeout_seconds`（`tts.engines` に `voicevox` があるときだけ）。範囲: `schedule.max_sleep_seconds` が 1 未満（待機ループが CPU を使い続ける）、`audio.gap_ms` が負、または 9,223,372,036,000 ミリ秒を超える（待てない）、`audio.mock_max_seconds` が負または `NaN`、前倒しや準備の秒数（`schedule.pip_lead_seconds`・`schedule.prepare_lead_seconds`・`schedule.catchup_grace_seconds`、短音の数 × 間隔）が、日時の範囲（1〜9999 年）を出る（`NaN`・無限大を含む）。`tts.voicevox.timeout_seconds`・`tts.voicevox.probe_timeout_seconds` の絶対値が 9,223,372,036 秒を超える（通信の待ち時間として設定できる範囲を出る。`socket` が `OverflowError` にして、VOICEVOX ENGINE の疎通確認と合成が毎回例外になる。`tts.engines` に `voicevox` があるときだけ。9,000,000,000 秒までは動く） | `"9"`・`9.0` のような数値の書き方の違い。読めなくても、その部品が失敗するだけのキー: `audio.fade_in_ms`（蛍の光のフェードインが省かれる）、`time_signal.short_pip.frequency`・`time_signal.short_pip.duration_ms`・`time_signal.long_pip.frequency`・`time_signal.long_pip.duration_ms`・`time_signal.volume`・`time_signal.envelope_ms`（時報音を作れず鳴らない。前に作った時報音があればそれを使う）。`tts.voicevox.speaker` などは、`tts.engines` に `voicevox` が無い間は使われないので、読めなくても warning。`weather.timeout_seconds` の絶対値が 9,223,372,036 秒を超える（`socket` が `OverflowError` にして、天気予報の取得は毎回例外になるが、放送を組み立てる側が受け止めて天気予報を飛ばすだけで、時報音・時刻アナウンス・ひとこと、放送は続くので、置き換えない。天気予報が流れない。9,000,000,000 秒までは動く。4.5 章） |
| 外部コマンドの表 | `audio.commands` の項目が、コマンド（`["aplay", "-q", "{path}"]` のような、文字列の引数のリスト）として読めない: 1 つの文字列で書いた（`"aplay -q {path}"`。リストと取り違えやすい。1 文字ずつの引数に分かれて読まれ、そのコマンドの再生が毎回失敗する）、反復できない値（数値・`null`。外部コマンドで再生する方式を作る処理が例外になる）、文字列でない要素を含むリスト（再生のたびに例外）。読めない項目だけを、その拡張子の既定の項目に置き換える（`.wav` は `aplay`、`.mp3` は `mpg123`。既定に無い拡張子の項目は取り除く）。表を読むのは外部コマンドで再生する方式だけなので、error にするのは `audio.backend` が `mock` / `pygame` 以外のとき（`command`、`auto`（pygame が無いと外部コマンドで再生する）、知らない名前） | `audio.backend` が `mock` か `pygame` の間は、表を読まないので、壊れていてもランタイムは動く。置き換えない（書いたとおりにする。「今は audio.backend が mock か pygame なので使われませんが、command にしたとき（pygame が無いときの auto を含む）に放送が鳴らなくなります」と添える）。空の文字列は `[]` と同じ「未設定」。辞書など、ほかの反復できる値はキーを並べたコマンドとして読まれ、例外にはならない |
| 文字列で選ぶもの | `timezone` が IANA のタイムゾーン名として解決できない（OS のローカル時刻で動き、Pi では 9 時間ずれる）。`logging.level` がログの水準の名前（`DEBUG`・`INFO`・`WARNING`・`ERROR`・`CRITICAL` など）でない。`logging.format` が、実際に 1 件のログを整形すると例外になる（すべてのログが失われる）。`audio.backend` が文字列でない。`tts.engines` が反復できない | `audio.backend` の知らない名前（`auto` として動く）。`tts.engines` の知らない名前（無視される。使えるものが 1 つも残らなければ、読み上げはすべて無音）。`tts.engines` に `prerecorded` が無い（`[]`・`""`・`{}`・`["voicevox"]` を含む。放送が作り置きを引かないので、声がディスクにそろっていても Pi では読み上げがすべて無音になる。PC で VOICEVOX ENGINE だけを使う開発のために外すこともあるので、置き換えない）。`tts.engines` がリストでなく文字列や辞書（1 文字・1 キーずつのエンジン名として読まれる） |
| 真偽値 | なし | `schedule.hourly.enabled`・`schedule.closing.enabled`・`extra_segment.enabled`・`weather.enabled`・`time_signal.use_noon_template` が `true` / `false` でない（ランタイムは真偽として読む。`"false"` は真になり、止めたつもりで有効のまま） |
| 地点・読み上げ文・作り置きの範囲 | なし | `weather.open_meteo.locations`（緯度 -90〜90・経度 -180〜180 の数値でない。その地点の天気が流れない）。読み上げ文のテンプレート `time_signal.announce_template`・`time_signal.noon_template`・`weather.sentence_weather`・`weather.sentence_temp`・`weather.sentence_temp_max`・`weather.sentence_pop`（使えない置換名・波括弧の誤り。時刻アナウンスは既定の文言に切り替わり、天気の文はその地点が読み上げられない）。テンプレートの書式指定の幅や桁数（`{label:>20}` の 20 など）が 1000 を超える（1 つの文が巨大になり、作り置きに無いので無音。試しの `format` には通さない）。`weather.prerecord.temp_min`・`weather.prerecord.temp_max`・`weather.prerecord.pop_step`（読めない・逆順・気温の読み上げ文が 200 件を超える） |
| 休館日・音量・再生の打ち切り・休止の記録の保存先（v6.2.0） | なし（どれも、ランタイムは例外にせず、**鳴らす側**に倒れる） | `schedule.closures.dates` / `schedule.closures.ranges` の書き方の正しくない項目（日付の形が違う・カレンダーにない日付・`from` が `to` より後・`name` が文字でない・項目の形が違う、など）: **その項目だけ**が使われず、その日はいつもどおり鳴る（ほかの項目は使う）。一覧がリストでないとき（`null` を除く）は、その一覧がまるごと使われない。`audio.volume.time_signal` / `audio.volume.voice` / `audio.volume.music`: 数として読めない（文字列・真偽値・`NaN`。1.0 で鳴る）、または 0〜1 の範囲外（負の値は 0.0 に丸められて無音、1 を超える値は 1.0）。`audio.max_segment_seconds`: 正の有限の数でない（600 秒として扱う）。`state.pause_file`: 文字列でない（`null` は保存先なしになり、「今日だけ止める」が使えない） |

表の補足:

- **節が無効のとき。** 節の `enabled` が偽の間、`Scheduler` はその節の中身を読まないので、壊れていても warning にとどめる（「今は enabled が false なので使われませんが、有効にしたときに例外になります」と添える）。`tts.engines` に `voicevox` が無い間の `tts.voicevox` も、`audio.backend` が `mock` / `pygame` の間の `audio.commands` も同じ（表を読むのは、外部コマンドで再生する方式だけ）。
- **リストの error。** ランタイムが動くのに必要な分だけ直す。`int()` で読めない `skip_hours` の要素・ハッシュできない `weekdays` の要素は、**その要素だけを取り除き**、残りはそのまま使う。コマンド（引数のリスト）として読めない `audio.commands` の項目は、その拡張子の既定の項目に置き換える（既定に無い拡張子の項目は取り除く。読める項目は書かれたとおりに残す）。リストでない値のときは、既定値に戻す。warning の要素（範囲外の時刻・存在しない曜日など）のためには、リストを作り直さない。
- **日時の範囲。** 前倒しや準備の秒数が日時の範囲（1〜9999 年）を出るかは、今の日付から予定を探す範囲で、`Scheduler` と同じ式を計算して決める。
- **内部の失敗。** 検査は 1 項目ずつ独立して動き、どれかが内部で例外を出しても、その項目を「検査できませんでした」という warning にして、ほかの検査と起動は止めない。
- **作り置きの数え上げの上限。** 設定の値で文言が増えすぎる・長すぎるもの（気温の幅、地点の `label` の長さ、テンプレートの書式指定の幅など）は、起動時のログ・`--check`・`--status` が数え上げを省き、理由を日本語で示す（4.12 章）。`validate()` は、そのうち書式指定の幅だけを先に警告にする（上の表）。
- **v6.2.0 の設定は、すべて warning。** 休館日の書き間違いで放送が止まる側に倒れないよう、ランタイム（4.20 章の `ClosureCalendar`）は項目ごとに無視して鳴らす。検査は同じ `ClosureCalendar` を使い、無視される項目の説明をそのまま警告にする（ランタイムとの食い違いを作らない。1 つの一覧につき最大 10 件を示し、残りは件数だけ示す）。
- **検査しないもの。** 時報音の合成が巨大な値（長さ・短音の数・周波数）で終わらなくなること（合成は時報音が無いときだけ行われる。前の版と同じ）。

起動時は、`sanitized()` が error のキーだけを直した設定（元の設定は変えない）でアプリを動かす。`--check` は書かれたとおりの設定を調べ、error を NG、warning を警告、info を情報として示す（4.14 章）。`--print-config` は置き換える前の、読んだとおりの値を見せる。

このモジュールは `chime.audio` / `chime.tts` を import しない（pygame を引き込まないため）。再生バックエンドと読み上げエンジンの名前はここに写して持ち、`tests/test_configcheck.py` が実装との食い違いを検出する。

---

## 4. モジュール仕様

### 4.1 `chime/env.py`（責務: 実行環境の判定）

| 関数 | 仕様 |
|---|---|
| `is_wsl()` | `platform.uname().release` に `microsoft` / `wsl` を含む、または環境変数 `WSL_DISTRO_NAME` があれば `True` |
| `is_production_linux()` | `system == 'Linux'` かつ WSL でなければ `True` |
| `has_command(name)` | `shutil.which` による外部コマンドの存在確認 |
| `describe()` | ログ出力用の環境情報 |

> **v1.x からの変更:** `kill_conflict_process()` を **削除**。`weather.py` が存在しない環境となるため、`pkill` 実行は不要かつ副作用リスクのみが残る。

### 4.2 `chime/timesignal.py`（責務: 時報音の合成と文言生成）

| 関数 | 仕様 |
|---|---|
| `generate_time_signal(path, settings, mixer)` | 標準ライブラリ（`wave` / `math` / `struct`）だけで 16bit PCM の WAV を書き出す |
| `ensure_time_signal(path, ...)` | ファイルが無ければ生成する |
| `lead_seconds(settings)` | 短音区間の長さ＝`short_pip_count × pip_interval_ms`（既定 3.0 秒） |
| `hour_parts(hour, settings)` | 12 時間表記の部品（`period` / `hour` / `hour24` / `hour_reading`） |
| `announce_text(hour, settings)` | 読み上げ文言。12 時は `noon_template` |

**設定が欠けたときの補い方。** 読み上げ文言まわりの項目（`announce_template` / `noon_template` / `use_noon_template` / `period_am` / `period_pm` / `hour_readings`）が、設定に無い、または `null` のときは、`DEFAULT_CONFIG["time_signal"]` の値で補う。作り置きの声は文言の**完全一致**で引くため、欠けたときの予備が既定値と違う言い回しだと、`"time_signal": null` のような設定でその放送だけ無音になる。予備はコードに書かず、既定設定から読む。

**`hour_readings` の読み方。** `null` のときだけでなく、辞書でない値（`[]`・`0`・`false`・`""`・文字列・数値など）のときも、既定の読みの表（`DEFAULT_CONFIG["time_signal"]["hour_readings"]`）を使う。空の辞書 `{}` は「読みの置き換えなし」の意味で、すべての時刻が「N時」と読まれる（たとえば 16 時は「午後4時をお知らせしたのだ。」になり、作り置きに無いので無音）。v6.0.0 は偽の値（`[]`・`0`・`false`・`""`）を `{}` と同じに扱い、辞書でない真の値（`"x"`・`5`・`true`・`[1]`）では例外で時刻アナウンスが落ちていた。この版はどちらも、作り置きのある既定の文言（「午後よじをお知らせしたのだ。」）で読む（読み上げの文言は増えない）。`time_signal` そのものが `null` のときも同じ（上の補い方）。

波形は各トーンの前後に `envelope_ms` の直線フェードを掛け、クリックノイズを防ぐ。

**時刻の読み誤り対策（`hour_readings`）**

読み上げエンジンは「4時」を「よんじ」、「7時」を「ななじ」、「9時」を「きゅうじ」、「0時」を「ぜろじ」と誤読する（正しくは よじ／しちじ／くじ／れいじ）。数字部分だけをかな化すると（例:「午後よ時」）今度は「時」が「とき」と読まれるため、`hour_readings` は「時」を含めて丸ごとかな書きに置き換える（例: `"4": "よじ"`）。既定でこの 4 つの時刻のみを対象にしており、正しく読める時刻まで一律にかな化しないのは、TTS のアクセントがかえって不自然になるのを避けるため。`hour_parts()` はこの読みを `hour_reading` として返し、`announce_template` の既定値はこれを使う。

**タイムライン（既定値）**

```
t=0.0  ポ（440Hz 100ms）
t=1.0  ポ
t=2.0  ポ
t=3.0  ポーン（880Hz 1000ms）  ← ここが正時
t=4.0  終了
```

WAV の先頭を「正時 − `lead_seconds()`」に再生開始することで、長音の先頭が正時に一致する。

### 4.3 `chime/audio.py`（責務: 音声再生制御）

`Segment`（`path` / `label` / `fade_in_ms` / `optional` / `kind`）の列を順番に再生する。`kind` は音量の区分で、`time_signal`（時報音）・`voice`（読み上げと閉館アナウンス。既定）・`music`（蛍の光）のどれか。組み立て側（4.9 章）が付ける。

| クラス | 仕様 |
|---|---|
| `Player.play(segments, stop=None)` | 存在しないファイルを除外（`optional=False` なら `PlaybackError`）し、`open()` → 各 `play_one()` → `close()` を実行。`close()` は `finally` で必ず呼ぶ。戻り値は実際に再生したセグメント数（`int`）で、再生対象が 1 つも無かった場合（除外の結果 0 件になった場合を含む）は例外を出さず `0` を返す。`stop`（`threading.Event`）が立つと、鳴らしている音を止め、セグメントの間隔の待ちも打ち切って、`PlaybackInterrupted`（`PlaybackError` の子。`played` に鳴り終えたセグメント数、`started` に音を出し始めたか）を送出する。再生の前から立っていれば、デバイスを開かずに（`started` は偽で）送出する。`None` なら止めない |
| `PygamePlayer` | `pygame.mixer.init(frequency, size, channels, buffer)` で初期化し、`music.load` → 音量の設定（`music.set_volume`。`load` で音量が戻るので、`load` のあと `play` の前に設定する）→ `play(fade_ms=...)`。そのあと 0.05 秒（`POLL_SECONDS`）ごとに、`get_busy()` が `False` になったか・停止要求が立ったか・上限を超えたかを確かめる。`load` と音量の設定のあと `play` の前にも停止要求を確かめ、その間に来た要求では鳴らし始めない。打ち切りの秒数は、鳴らし始める前に決める（鳴らし始めたあとで例外を出して放送を投げ出さないため）。停止要求では 300 ミリ秒（`STOP_FADEOUT_MS`）でフェードアウトし、`get_busy()` が偽になるまで待つ（`fadeout()` がすぐ戻る版でも、後始末の `music.stop()` がフェードを途中で切らないため。待つのは最長 `STOP_FADEOUT_MS` ＋ 100 ミリ秒〔`STOP_FADEOUT_MARGIN_MS`〕）。`close()` は `music.stop()` と **`pygame.mixer.quit()`** を必ず呼ぶ（どちらも失敗しても記録だけで、再生の結果〔中断・打ち切り・成功〕を隠さない） |
| `CommandPlayer` | 拡張子に応じて `aplay` / `mpg123` を、自分のセッション（`start_new_session=True`）で `subprocess.Popen` により起動し、0.05 秒ごとに、終了したか・停止要求が立ったか・上限を超えたかを確かめる（子が終われば、次の確認を待たずにその場で次へ進む）。停止要求と上限では、子のグループごと `SIGTERM` を送り（ラッパー〔`sh -c "aplay …"`〕が起こした孫の再生も止まる）、直接の子（ラッパー）を回収する。ラッパーが先に終わったときは、グループが空になる（`os.killpg(pgid, 0)` で確かめる）か猶予の 2 秒（`TERMINATE_GRACE_SECONDS`）が過ぎるまで待ち、残っていればグループごと `SIGKILL` を送る（`SIGTERM` を無視する孫がラッパーの陰に残らない。ラッパーが猶予のうちに終わらないときも `SIGKILL`）。グループへ信号を送れないとき（`killpg` が無い・失敗）は直接の子だけを相手にし、グループの有無を確かめられないときは空とみなして待たない（想定外の例外で抜けるときも子を残さない）。停止要求が立っているときに子が異常終了していたら（systemd や Ctrl+C は、本体と子に同じ `SIGTERM` を送る）、失敗ではなく中断として扱う。起動できない（`OSError`）・異常終了は `PlaybackError`。フェードは非対応。**音量も変えられない** |
| `MockPlayer` | ログ出力と待機のみ（WAV は実長、上限 `mock_max_seconds`）。待つあいだも停止要求に応じる。音量が 1.0 でなければ、ログに「音量 0.50」を添える |
| `create_player(settings, force)` | `auto` の場合: 開発環境 → mock、実機 → pygame → command → mock の順 |

> **v1.x からの変更:** v1.x は `mixer.init()` を毎回呼びながら `quit()` を行っておらず、デバイスを掴んだままになっていた。`close()` での解放を必須とする。

**音量。** `volume_for(segment, audio_settings)` が、`audio.volume` の `segment.kind` のキー（0.0〜1.0）を返す。キーが無い・数として読めない（文字列・真偽値・`NaN`）ときは 1.0（今までの音量）、範囲外は 0〜1 に丸める。例外は出さず、どちらもキーごとにプロセスで 1 回だけ WARNING を残す。音量の設定のせいで時報が鳴らなくなることは無い（`pygame` の `set_volume` が失敗しても、そのまま鳴らす）。外部コマンドで再生する方式は音量を変えられないので、`audio.volume` が 1.0 でなければ「command 方式では音量を変えられません」と WARNING を 1 度だけ残し、そのままの音量で鳴らす。`time_signal.volume`（既定 0.6）は時報音を作るときの波形の大きさで、再生の音量（`audio.volume.time_signal`）とは別のもの。

**停止要求への即応。** `ChimeApp` は `stop_event`（`SIGTERM` / `SIGINT` で立つ。4.10 章）を `Player.play` に渡す。停止の要求から 1 秒ほどで再生が止まる（確かめる間隔 0.05 秒 ＋ フェードアウト 300 ミリ秒）。送出された `PlaybackInterrupted` は失敗ではなく意図した中断だが、`PlaybackError` だけを受ける呼び出し側でも落ちないよう、その子クラスにしてある。

**再生の打ち切り。** 1 つの音源が鳴り終わらないとき（デバイスの詰まりなど）に、放送の処理が戻らなくならないよう、上限の秒数で打ち切る。上限は、WAV なら `wave` で読んだ音の長さに 10 秒（`WAV_TIMEOUT_MARGIN_SECONDS`）を足した秒数、長さの分からない音源（MP3 や、壊れた WAV）なら `audio.max_segment_seconds`（既定 600）。超えると音を止めて `PlaybackError`（「再生が終わらないため打ち切りました（上限 … 秒）」にパスを添える）を送出する。呼び出し側（4.10 章）は、履歴に `error` として残して次へ進む。

**音声デバイスの初期化。** `pygame.mixer.init` が例外になったときは、3 回（`MIXER_INIT_ATTEMPTS`）まで試し、1 秒（`MIXER_INIT_RETRY_SECONDS`）おきにやり直す（起動直後などでデバイスがまだ使えないことがあるため。失敗のたびに WARNING）。待つあいだも停止要求に応じる。3 回とも失敗すると `PlaybackError`（「音声デバイスを初期化できませんでした」）。

pygame は import しただけで標準出力に版数と宣伝のバナーを出し、`--print-config` の JSON などに混ざって壊す。`chime/audio.py` は import の前に環境変数 `PYGAME_HIDE_SUPPORT_PROMPT=1` を設定してこれを消す（利用者が自分で設定していれば、その値を尊重して上書きしない）。

### 4.4 `chime/tts.py`（責務: 音声合成）

エンジンを設定順に試し、最初に成功したものを採用する。

| エンジン | `available()` | `synthesize()` |
|---|---|---|
| `PrerecordedEngine` | ディレクトリが存在する | 合成しない。`manifest.json`（文言→ファイル名）または `sha1(文言)[:20].wav` を探し、無ければ次のエンジンへ |
| `VoicevoxEngine` | `GET /version` が `voicevox.probe_timeout_seconds`（既定 2 秒）以内に 200 | `POST /audio_query` → `POST /synthesis` |

**文言の整え方。** 実行時の照合（`TTSService`）・文言の列挙（`chime/phrases.py`）・作り置きの生成（`scripts/generate_voicevox.py`）は、同じ `normalize_phrase()`（前後の空白を落とす。`None` は空文字列）を通す。整え方が食い違うと、前後に空白のある文言は manifest に空白つきのキーで載り、実行時には引けずにその文だけ永久に無音になる。

**VOICEVOX ENGINE との通信の失敗。** 疎通確認（`available()`）も合成（`synthesize()`）も、`urllib.error.URLError`・`http.client.HTTPException`・`OSError`・`ValueError` を受ける。別のサービスが居座っていて HTTP ではないもので答えたとき（`BadStatusLine` など）も、例外を外へ出さず、`available()` は `False`、`synthesize()` は `TTSError` にする。使えないと判断した理由は DEBUG ログに残す（通常は静かに「使えない」と返すだけ）。

**フォールバックは実行時合成ではなく無音（v5.0.0）。** どちらのエンジンでも合成できない
文言は `TTSError` を送出し、呼び出し側（`chime/sequence.py` の `_append_speech()`）が
これを捕捉してエラーログと `plan.warnings` を残し、そのセグメントだけを落とす
（時報音・蛍の光・他の文言は再生を続ける）。v4.x までは最終段に `OpenJTalkEngine`
（実行時にオフライン合成する最終フォールバック）を置いていたが、フォールバックが
あるせいで壊れた状態（作り置き不足・古い `config.json`）でも別人の男性音声で
「それらしく」鳴ってしまい、故障の発覚を妨げていたため v5.0.0 でコードごと削除した。

**キャッシュ:** `cache/tts/{sha1(voice_id + 文言)[:20]}.wav`。一時ファイルへ書いてから `os.replace` で原子的に置き換える。`voice_id` に話者・話速等を含めるため、設定を変えれば別キャッシュになる。キャッシュが使われるのは VOICEVOX で実際に合成したときだけで、その場合、同じ文言は 2 回目以降合成されない。Pi では全文言を作り置き（`assets/voice/`）から引くため、合成もキャッシュも発生しない。

**`voicevox.probe_timeout_seconds` について:** 放送直前（実行時）に呼ばれる `available()` は、エンジンが落ちていた場合に即座に他のエンジンへフォールバックできるよう、既定で短い（2 秒）タイムアウトを使う。一方、VOICEVOX ENGINE は Docker での起動直後、ONNX モデルの読み込みのため `/version` が数秒〜数十秒応答しないことがある。`scripts/generate_voicevox.py` はこの値を長めに設定した `VoicevoxEngine` で `available()` を数秒おきに再試行し、`--wait`（既定 90 秒）で指定した秒数まで起動を待つ。

**待ち時間の上限。** `voicevox.timeout_seconds`（合成）と `voicevox.probe_timeout_seconds`（疎通確認）は、3.5 章の上限（約 92 億秒）を超えると、`socket` が `OverflowError` にする。`urllib` はこれを通信の失敗として受け止めないので、疎通確認も合成も毎回例外になる。そのため設定の検査は error として既定値に戻す（`tts.engines` に `voicevox` があるときだけ）。起動時の疎通確認（4.10 章の `TTS: …` の行）は、万一例外になっても起動を止めない。`weather.timeout_seconds` も同じ上限を持つが、こちらは warning にとどめる（4.5 章）。

### 4.5 `chime/weather.py`（責務: 天気予報の取得）

提供元は **Open-Meteo だけ**（API キー不要）。エンドポイントは次のとおり。

| 提供元 | エンドポイント |
|---|---|
| Open-Meteo | `https://api.open-meteo.com/v1/forecast?...&current=weather_code,temperature_2m&daily=...&timezone=Asia/Tokyo&forecast_days=1` |

`weather.open_meteo.locations` の**地点ごとに個別に問い合わせる**。
複数地点の一括クエリは応答形式が変わる（配列になる）ため使わない。地点数が少ないうちは
素直に地点ごとに叩くほうが堅い。キャッシュも地点ごとに持つ。

**Open-Meteo JSON の解釈**

- 天気: **`current.weather_code`** を `WMO_CODES`（28 語）で引く。`daily.weather_code`（今日 1 日を
  代表する予報）ではなく**現況**を使う。時報で知りたいのは今どうなのかであり、その日の
  予想を午後に読んでも実感と合わないため
- 気温: **`current.temperature_2m`** を四捨五入して整数化する（現況）
- `daily` も同時に取得し、`temp_max` / `temp_min` / `pop` / `when` として保持する。
  既定では読まないが、`sentence_temp_max` / `sentence_pop` を有効にすれば使われる
- `current` が無い、または現況の天気コードを解釈できない場合は `WeatherError`。
  その地点だけ飛ばされ、**他の地点は読み上げられる**

**読み上げ文の組み立て**

読み上げは**1 文ずつ独立した音声ファイル**として再生する。`build_sentences()` が 1 地点ぶんの
文のリストを返し、`describe_sentences()` が全地点ぶんを地点の順に平坦なリストで返す。

```
今の大津の天気は晴れなのだ。      ← sentence_weather
気温は28度なのだ。                ← sentence_temp
```

既定（大津の 1 地点）ではこの 2 文になる。`locations` に地点を足した場合は、上から順に
地点ごとの 2 文が続く（例: 京都を足すと、上の 2 文のあとに「今の京都の天気は…なのだ。」
「気温は…度なのだ。」が続き、計 4 文になる）。

値が `None` の文、テンプレートが空文字列の文は出力しない。`sentence_pop` を有効にした場合、
降水確率は `prerecord.pop_step` の刻みに丸めて読み上げる（語彙を閉じるため）。丸めは文の
組み立て時のみ行い、`parts["pop"]` は生値のまま保持する。

`prerecord_phrases()` は、事前生成すべき文言（地点 × 天気コード、気温の全域、
降水確率の全段階）を全列挙する。`build_sentences()` と同じ関数を通して文を組み立てるため、
両者の文言がずれることはない。この網羅性は `tests/test_weather.py` の語彙網羅テストが
機械的に検証している（**これが「天気だけ無音になる」ことを防ぐ唯一の担保**）。

`{weather}` には `WMO_CODES` の語しか入らないため、文字数の上限や切り詰めは持たない。

取得結果は `cache_minutes` の間、**地点ごとに**メモリ上に保持する（NFR-06）。あらゆる失敗は
`WeatherError` に正規化する。全地点が失敗したときだけ `describe_sentences()` が送出し、
呼び出し側が天気を飛ばせるようにする。

**待ち時間の上限。** `weather.timeout_seconds` が 3.5 章の上限（約 92 億秒）を超えると、`socket` が `OverflowError` にする。`urllib` はこれを通信の失敗として受け止めず、`WeatherError` にもならないので、天気予報の取得は毎回例外になる。ただし、放送を組み立てる側（4.9 章）が、天気予報の部品の想定外の例外を受け止めて、その部品だけを飛ばす（時報音・時刻アナウンス・ひとことは鳴り、放送は続く）。そのため設定の検査は、起動を止める VOICEVOX ENGINE の待ち時間（4.4 章）と違い、warning にとどめて置き換えない（天気予報が流れないだけ。3.5 章）。

### 4.6 `chime/quotes.py`（責務: ひとことの選択）

`assets/quotes.json` の形式:

```json
{
  "general": ["全時刻共通のひとこと", "..."],
  "by_hour": { "12": ["お昼だけのひとこと"] }
}
```

`pick(hour, recent)` は `general` ＋ 当該時刻の `by_hour` を候補とし、`recent` の末尾 `avoid_recent` 件を除外して選ぶ。除外の結果候補が空になった場合は全候補から選ぶ。ファイルが無い・壊れている場合は内蔵の予備文言にフォールバックする（放送を止めない）。

### 4.7 `chime/state.py`（責務: 再生状態の永続化）

`cache/state.json`:

```json
{
  "last_fired": { "hourly:10": "2026-08-26", "closing": "2026-08-26" },
  "recent_quotes": ["...", "..."]
}
```

- `is_fired(key, day)` / `mark_fired(key, day)` で二重再生を防ぐ（FR-06）
- `remember_quote()` は直近 32 件まで保持
- 書き込みは一時ファイル＋`os.replace` による原子的置換
- 読み込み失敗時は初期状態として扱い、例外を投げない

### 4.8 `chime/scheduler.py`（責務: 次イベントの算出と待機）

`Event` は `key` / `kind` / `hour` / `at`（内容の時刻）/ `play_at`（再生開始）/ `prepare_at`（準備開始）を持つ。

| メソッド | 仕様 |
|---|---|
| `events_for_date(day)` | 稼働曜日なら時報（`start_hour`〜`end_hour`、`skip_hours` を除く）と閉館放送を生成し、`play_at` 昇順で返す。**休館日（`schedule.closures`。4.20 章）は空** |
| `skip_reason(day)` | 休館のために鳴らさない日なら、その理由（`休館: 学園祭`）を返す。曜日や有効／無効の決まりでもともと鳴らない日（土日など）は、休館日と重なっていても `None`（「休館がなければ鳴っていたはずの日」だけを答える） |
| `iter_events(start, days)` | 最大 400 日先まで（`MAX_LOOKAHEAD_DAYS`）、`play_at >= start` のイベントを時系列で列挙。土日・祝日の連続だけでなく、年末年始や長期休館をまたいでも、休み明けの最初の予定を見つけられる長さ |
| `upcoming(now, limit)` | 一覧表示用 |
| `next_event(now, is_fired)` | まず「過去 `catchup_grace_seconds` 以内・未再生」を探し（追いかけ再生、FR-07）、無ければ未来の最初の未再生イベントを返す |
| `sleep_until(target, stop, precise, interrupt)` | `max_sleep_seconds` ごとに分割して sleep し、都度実時刻を読み直す。`precise=True` の場合、残り 0.25 秒からは 2ms 刻みで詰める。`interrupt`（引数なしで真偽を返す関数）は、「今日は止める」のような、待つ途中で放送を取りやめる合図の確認で、1 回分の待機が終わるたびに呼び、真なら待つのをやめて `False` を返す。**正時直前の最終調整（残り 0.25 秒以内）では呼ばない**（ポーンを遅らせない）。確認で例外が出ても待機は続ける（鳴らす側に倒す。ログは 1 度だけ） |
| `format_schedule(scheduler, start, limit)`（関数） | `--schedule` の一覧。`format_events(upcoming(start, limit))` と同じ並びに、一覧が覆う期間の「休館がなければ鳴っていたはずの日」を、1 日 1 行（`  休館 2026-11-02（月） 学園祭`。名前が無ければ名前を省く。行頭は「休館」で、現地の操作で一時的に止める「休止」とは別の言葉）で時系列に入れる。休館日のない設定では `format_events` と同じ文字列 |

時報の `play_at` は `at − pip_lead`、閉館放送の `play_at` は `at` と同じ。

`sleep_until` を分割する理由は、NTP による時刻補正（特に RTC 非搭載機の起動直後の大きなジャンプ）に追従するため。単一の長い `sleep` では補正を反映できない。

### 4.9 `chime/sequence.py`（責務: 再生内容の組み立て）

| メソッド | 生成される `Segment` |
|---|---|
| `build_hourly(hour)` | ①時報音 ②時刻アナウンス ③（`weather_hours` の時刻だけ）天気予報 ④ひとこと |
| `build_closing()` | ①閉館アナウンス ②（任意）追加読み上げ ③蛍の光（フェードイン） |
| `build_text(text)` / `build_texts(texts)` | 任意文言の読み上げのみ。複数文は 1 文ずつ別セグメントにする |

積む `Segment` には音量の区分（`kind`）を付ける。時報音は `time_signal`、蛍の光は `music`、そのほか（作り置きの読み上げ・閉館アナウンス）は `voice`。音量そのものは再生側（4.3 章）が `audio.volume` から引くので、ここでは設定を読まない。

**時報の放送の決まり方（1 経路）**

`build_hourly(hour)` は次の順に積む。抽選や分岐はなく、設定で変わるのは天気を流す時刻だけ。

1. 時報音
2. 時刻アナウンス
3. `extra_segment.enabled` が `false` ならここで終わり。そうでなければ続けて:
   1. `hour` が `weather_hours`（既定 `[12]`）に含まれる → 天気予報を積む。含まれなければ**天気 API を呼ばない**。`weather.enabled` が `false` のときは天気を積まず、警告を 1 件記録する
   2. 必ずひとことを 1 つ積む（天気の成否に関わらず）

`weather_hours` の要素は `int` に正規化し、変換できない要素は警告して無視する。v6.0.0 で廃止した旧設定（3.4 章）が `config.json` に残っていても、ここでは読まない。

**閉館放送は `build_closing()` という別経路のため、`weather_hours` に何を書いても
天気は付かない**（回帰テストで固定している）。

**天気の読み上げは 1 文 = 1 セグメント**。`describe_sentences()` が返す各文を
個別に積む。連結すると作り置き音声との照合が外れてその文が無音になるため。

**失敗時の扱い（重要）**

- 天気取得失敗 → 警告を記録して**天気を黙って飛ばす**。ひとことは後続で必ず流れるため
  沈黙せず、ここでひとことへ切り替えるとひとことが 2 つ流れてしまう
- 音声合成失敗 → 当該セグメントを落として続行（**時報音そのものは必ず鳴る**）。音にならなかった文言は `PlaybackPlan.silent` に読み上げの順で残り、再生内容の表示（`describe()`）に「無音: …」の行で出て、放送の履歴にも載る（4.16 章）
- 必須の音源ファイル（時報音・閉館アナウンス・蛍の光）が無い → ERROR ログを出し、その部品だけを積まずに残りを鳴らす。積めなかった部品の名前は `PlaybackPlan.missing` に残り、放送の履歴の `missing` にも載る（4.16 章）。積めなかった部品は `segments` に無いので、再生の件数（`played` / `total`）には現れない。件数だけを見ると『すべて鳴った』ことになってしまうため、別に残す
- 組み立て中の想定外の例外（通信の途中切断、読み上げ文のテンプレートの書き間違いなど）→ 部品ごとに縮退し、**時報音は必ず鳴る**。時刻アナウンスのテンプレートを書き間違えたときは既定の文言で読む（8 章）。組み立て全体が失敗して最小構成に落としたときは、`PlaybackPlan.degraded` が真になる（`ChimeApp` が立てる。履歴に残す）

### 4.10 `chime/app.py`（責務: 全体の組み立てと常駐ループ）

```
[起動]
  ↓
[版・環境・バックエンド・TTS エンジン・作り置きの件数をログ出力。休止の記録が効いていればそれも]
  ↓
[次イベントを算出] ←──────────────────┐  ← 休止が止めている日のイベントは飛ばす
  ↓ 無ければ 60 分ごとに再確認         │
[prepare_at まで待機]                  │  ← 休止の記録が書き換わったら、待つのをやめて選び直す
  ↓                                    │
[待機中に予定が変わっていないか再確認] ─┘ 変わっていれば再算出
  ↓
[休止中なら、鳴らさず履歴に skipped を残して次へ]
  ↓
[再生内容を組み立て]  ← 天気取得・音声合成はここで完結
  ↓
[play_at まで精密待機]  ← 待つあいだも休止の記録を見る（正時直前の 0.25 秒は見ない）
  ↓
[再生の直前にもう一度、休止を確かめる]
  ↓
[順次再生]  ← 停止要求が立てば、フェードアウトして止まる
  ↓
[state.json に再生済みを記録]
  ↓
[history.jsonl に結果を追記] ──────────┘
```

- `SIGTERM` / `SIGINT` で `stop_event` を立てる。**待機中は待機を打ち切って正常終了し、再生中は再生を止めて終了する**（`systemctl stop` に即応）。`stop_event` を `Player.play(…, stop=…)` に渡し（4.3 章）、鳴らしている音はフェードアウトして、停止の要求から 1 秒ほどで止まる。止めた回（`PlaybackInterrupted`）は、鳴らせた数を添えて WARNING「停止要求のため再生を中断しました」を残し、**再生済みとして記録する**（やり直すと、同じ放送を頭から鳴らし直してしまう）。履歴の結果は `interrupted`（`error` は付けない。4.16 章）。**ただし、音を出す前に止まった回**（`PlaybackInterrupted.started` が偽。デバイスを開く前、または最初の音を鳴らす前に停止要求が届いた）は、何も鳴っていないので、**再生済みの記録も履歴も付けずに戻る**（INFO「音を出す前に停止要求を受けたため、再生済みにしません」。再起動のあと、追いかけ再生の猶予のうちに鳴らし直す）。止まらなかったときの保険として、ユニットに `TimeoutStopSec=30` を置く（6 章。以前は既定の 90 秒を待って強制終了していた）
- **休止（「今日だけ止める」。4.21 章）。** 常駐ループは、(1) 次のイベントを選ぶとき（休止が止めている日のイベントは飛ばす。1 回の選び直しにつき、記録は 1 度だけ読む）、(2) 準備の時刻までの待機（記録が書き換わったら待つのをやめて選び直す。`PauseStore.changed()` を `sleep_until(interrupt=…)` に渡す）、(3) 組み立ての直前、(4) 再生の時刻までの待機（`sleep_until(interrupt=…)`。正時直前の最終調整は確かめない）と再生の直前、に休止を確かめる。止めていたら、鳴らさず、**再生済みの記録も付けず**、履歴に `skipped`（`reason` は `休止: 点検` のように止めた理由）を残して戻る。再生済みにしないので、止めた直後に解除すれば、追いかけ再生の猶予（`catchup_grace_seconds`）のうちは鳴る。ただし、常駐ループが解除に気づくのは待機の区切り（`max_sleep_seconds`）の終わりで、解除から最大でその秒数あと。確実に鳴るのは、再生開始からの経過が `catchup_grace_seconds` − `max_sleep_seconds` に満たない回だけで、それ以降の回は、気づくのが猶予内に間に合えば鳴る。確かめるのは設定タイムゾーンの日付と小さな記録ファイルだけで、**時報の「ポーン」を遅らせない**。記録が壊れている・読めない・確かめるときの例外は、どれも止めていないものとして鳴らす（`ChimeApp._current_pause()` が例外も受け止める）。**履歴に `skipped` が残るのは、(3)(4) で止めた回（準備に入っていた回。準備は再生開始の `prepare_lead_seconds` 秒前に始まる）だけ**。準備の前に書かれた休止は、(1) と (2) で、その日のイベントを選ばない（外す）だけなので、履歴にもログにも残らない（記録の書き換えに気づいたときの INFO「休止の記録が変わりました」と、起動時の INFO「休止の記録があります」だけが残る）
- 鳴らす予定が 1 つも無いとき（時報・閉館放送とも無効、または曜日・休館日・休止で先まで空）は、WARNING「予定されたイベントがありません。60 分ごとに再確認します」を、**予定が途切れて最初の 1 度だけ**残し、60 分ごと（`NO_EVENTS_RECHECK_SECONDS`）に再確認する（休止や休館日で長く空くことがあるので、60 秒ごとに警告を出さない）。停止要求と、休止の記録の書き換えで待ちを切り上げる。次の予定が見つかると、また警告できる
- イベント処理中の例外は捕捉し、当該回を再生済みとして記録したうえで常駐を継続する（無限リトライを避ける）。この場合も、履歴には `error` として残す。再生済みの記録を先に付け、履歴はその後（履歴のために、再生済みの記録が遅れない）。`str()` が失敗する例外でも、記録も継続も同じ（`exception_text()` / `describe_exception()` が型名で代える）
- 次の予定を求められないとき（スケジューラーの例外）も、常駐は落とさない。例外をログに残し、60 秒後にやり直す（落ちると systemd が再起動を繰り返すだけになる）。これは上の「予定が無い」場合とは別で、こちらは例外のときだけ
- 再生バックエンドは初回参照時に決定する（`--schedule` 等で不要な警告を出さないため）

**再生の結果。** `ChimeApp.play(plan)` は、鳴らせたかを `bool` で返す（`--dry-run` は再生しないが失敗ではないので `True`）。件数や失敗の理由まで要るときは `play_with_result(plan)` を使う。戻り値の `PlayOutcome` は `ok`（鳴らせたか）・`played`（実際に再生したセグメント数。`--dry-run` や失敗では 0。停止要求で打ち切ったときは、打ち切るまでに鳴らせた数）・`total`（再生するはずだったセグメント数）・`error`（再生が例外で終わったときの説明。無ければ `None`。停止要求で打ち切ったときは「停止要求で中断」）・`interrupted`（停止要求で打ち切ったか）・`started`（音を出し始めたか。`interrupted` なのに偽なら、何も鳴らさないうちに止まった）を持つ。どちらも例外は外へ送出しない。

**放送の履歴。** 常駐が放送した回（`run_event`）は、再生と再生済みの記録のあとに、結果を履歴に 1 行足す（4.16 章）。`--dry-run` と、`--test-hourly` などの試し鳴らしは残さない。履歴を書けなくても、放送も常駐ループも止めない（警告を残すだけ）。`state.history_file` が空文字列なら残さない。`silent`・`missing`・`warnings`・`degraded` は、組み立てた内容（`PlaybackPlan`）から取る。休止で鳴らさなかった回は、再生していないので `played` と `total` は 0、結果は `skipped`（4.16 章）。

**起動時のログ。** `log_environment()` が次を順に INFO で残す（9 章）。1 行目は版とコミット（`campus-chime 6.1.0 (a1b2c3d)`）、続いて実行環境、再生バックエンドと TTS エンジン、設定の出どころ、そして `作り置きの音声: N/M 件`。作り置きが足りなければ、続けて WARNING で件数と先頭 3 件の文言を残す（VOICEVOX ENGINE が使えない環境では、その文は無音になる）。数えられなくても（ひとことの定義ファイルが壊れているなど）起動は止めない。設定の値で文言が増えすぎる・長すぎるとき（4.12 章の上限を超えるとき）は、起動を遅くしないために数え上げを省き、WARNING を 1 件残す。TTS エンジンの状態の確認（VOICEVOX ENGINE への疎通確認を含む）が例外になっても（待ち時間の設定が大きすぎるなど）、WARNING「読み上げエンジンの状態を調べられませんでした（起動は続けます）」を残して、`TTS: 確認できません` と書き、起動を続ける（落ちると systemd が再起動を繰り返すだけになる）。ここまでの行（`log_environment()` が残す）のあと、休止の記録が効いていれば INFO「休止の記録があります（開始日〜終了日。休止: 理由）。この間の放送は鳴らしません。」を残し（再起動しても、止めた期間は続くため）、次回以降の予定を INFO で残す。休止の記録が効いているあいだは、止めている日の予定を並べず（直前の「この間の放送は鳴らしません」と食い違うため）、見出しを「次回以降の予定（休止のあいだは省いています）:」にする（`--schedule`・`--status` と同じ）。

### 4.11 `chime/cli.py`（責務: コマンドライン処理）

| 引数 | 動作 |
|---|---|
| （なし） | 常駐 |
| `--schedule [N]` | 予定を N 件表示。休館日（`schedule.closures`）の日は、予定の代わりに `  休館 2026-11-02（月） 学園祭` の行を時系列で入れる。休止（`--pause-*`）で止めているあいだは、先頭に `止めています: 10/30（点検）` を出し、予定は休止が明けてから数える |
| `--test-hourly [HOUR]` | 時報を即時再生（省略時は現在時刻） |
| `--test` | 閉館放送を即時再生 |
| `--test-all` | 時報 → 閉館放送 |
| `--say TEXT` | 任意文言の読み上げ。作り置きに無い文言は「作り置きにありません。Pi では無音」と近い文言の候補を表示する |
| `--weather` | 天気予報の URL と読み上げ文を表示（`--dry-run` でなければ読み上げも） |
| `--generate-assets` | 時報音を生成し、時刻アナウンスの音声を用意できるか確認 |
| `--print-config` | 読み込んだ設定を、書かれたとおりに JSON で表示（危ない値の置き換えをする前の値。ただし廃止したキーは、読み込むときに捨てるので表示されない。ログは標準エラー出力へ出す） |
| `--check` | 設置状態を点検（設定・作り置きの音声・音源・書き込み先。4.14 章）。鳴らさず、何も書かない |
| `--status` | いまの状態（版・時刻の同期・サービス・放送の休止・直近の放送・次の予定・近い休館日。4.15 章）を表示。何も書かない |
| `--wait-idle [SECONDS]` | 放送の時間帯なら、終わるまで待つ（既定・最大 360 秒。4.15 章）。更新の前に使う |
| `--pause-today` | 今日いっぱい、時報と閉館放送を止める記録を書く（4.21 章）。止める放送の回数（時報・閉館。休館日と、今日はすでに鳴らした分を除く）と、いつ再開するかを表示する。常駐中のサービスにそのまま効く（再起動は要らない） |
| `--pause-until YYYY-MM-DD` | その日（含む）まで止める。今日から最長 31 日（`MAX_PAUSE_DAYS`）。過去の日付・長すぎる日付は断り（終了コード 2）、長い休みには `schedule.closures` を案内する |
| `--pause-off` | 止めている放送を再開する（記録を消す）。何を再開したか、追いかけて鳴らしうる回（再生開始から `catchup_grace_seconds` 以内で、まだ鳴らしていないもの）があればその回、次の放送がいつかを表示する。サービスが解除に気づくまでに最大 `max_sleep_seconds` かかるので、再生開始からの経過が `catchup_grace_seconds` − `max_sleep_seconds` に満たない回は「開始から N 秒未満の回は、サービスが追いかけて鳴らします:」、それ以降の回は「間に合えば追いかけて鳴ります（…）:」の見出しで分けて並べる（鳴ると約束するのは前者だけ。後者は、気づくのが猶予内に間に合えば鳴る。`max_sleep_seconds` が `catchup_grace_seconds` 以上なら、前者は無い）。並べるのは見出しごとに最大 3 件で、超えた分は「ほか N 件」と件数だけ。止めていなければ、そう表示する（期限が切れた・壊れた記録があれば、それを消したと表示する） |
| `--reason TEXT` | 止める理由。`--pause-today` / `--pause-until` と一緒に指定する。記録に残り、`--status`・`--schedule`に出る（準備に入っていた回を止めたときは、履歴の `reason` にも付く。4.10・4.16 章） |
| `--config PATH` | 設定ファイルの明示指定 |
| `--backend {auto,pygame,command,mock}` | 再生バックエンドの強制 |
| `--dry-run` | 音を出さず内容のみ表示（`cache/state.json` と `cache/history.jsonl` は書き換えない） |
| `--log-level LEVEL` | ログレベル |
| `--version` | 版とコミットの表示（例: `campus-chime 6.1.0 (a1b2c3d)`。コミットを調べられなければ `(unknown)`。4.17 章） |

**起動の順序。** 設定を読み込んだあと、`--print-config` 以外は、危ない値だけを既定値に戻した設定（3.5 章）で動く。`--print-config` は読んだとおりを見せ（JSON を表示して終了コード 0）、`--check` は書かれたとおりの設定を自分で検査して表示する（ここでログに残すと二重になる）。それ以外のモードは、置き換えた誤りを ERROR ログに 1 件ずつ残す。`--status` と `--wait-idle` は、状態ファイルを書き換えないよう、`--dry-run` と同じ読み取り専用のアプリで動かす。

**休止のオプション。** `--pause-today` / `--pause-until` / `--pause-off` は、どれか 1 つだけを指定する（複数・ほかの動作モード（`--test`・`--say`・`--schedule`・`--check`・`--status` など）との同時指定は、設定を読む前に断る。終了コード 2）。`--reason` は `--pause-today` / `--pause-until` と一緒にだけ指定できる。`--pause-until` の日付は、半角の `YYYY-MM-DD` で、カレンダーにある日付でなければならない。**root では使えない**（`cache/` が root の持ち物になり、サービスが記録を書けなくなるため。引数の誤りのほうを先に断る）。止める記録を書く場所（`state.pause_file`）が空なら断る（終了コード 2）。`--dry-run` を付けると、記録を書かず（消さず）に、何が起きるかを表示する（過去・長すぎる日付は同じように断る）。休止のオプションは、`--status` や `--wait-idle` と違い、読み取り専用ではない通常のアプリで動かす（`--dry-run` を付けたときを除く）。書くのは休止の記録だけで、状態（`state.json`）と履歴は書かない。

終了コード: `0` 正常 / `1` 実行時エラー（休止の記録を書けない・消せない場合、`--check` で NG が 1 件でもあった場合（警告と情報だけなら `0`）、`--status` で気になる点があった場合、`--wait-idle` が上限の秒数まで待っても放送の時間帯が終わらなかった場合、`--weather` での天気取得失敗、`--generate-assets` で時刻アナウンスの音声を用意できなかった場合、`--say`・`--test-hourly`・`--test`・`--test-all` で実行した再生がすべて失敗した場合。ここでいう失敗とは、再生対象のセグメントを 1 つも用意できなかった場合と、音源ファイルの欠落など再生時のエラーにより 1 つも鳴らせなかった場合の両方を指す（`ChimeApp.play()` の戻り値で判定する）。`--test-hourly`・`--test-all` は時報と閉館放送を続けて再生するため、どちらか一方でも鳴れば `0`（時報音は合成不要のため必ず鳴り、おまけの読み上げのみが失敗した場合も `0` のまま）。`--dry-run` 指定時は実際の再生を行わず常に成功として扱うため、この判定の対象にならない。おまけの天気予報の取得に失敗しても、他のセグメントが鳴っていればエラー扱いしない）/ `2` 引数・設定エラー（休止のオプションの組み合わせの誤り・日付の誤り・root での実行、`--say` に空文字列や空白のみを渡した場合、`config.json` を読めない場合（UTF-8 以外で保存・JSON の書き間違い）を含む。読めないときは、`--version`・`--help` を除くどのモードでも（`--check`・`--status` を含む）原因を日本語で表示して `2` になる。8 章）。

`--dry-run` や `--test-hourly` などの試し鳴らしは、`cache/state.json` を書き換えない（ひとこと履歴が進まない）。放送の履歴（`cache/history.jsonl`）にも残さない。

ログのタイムスタンプは `timezone` 設定に合わせて表示する（ハンドラのフォーマッタに変換関数を差し替える）。

### 4.12 `chime/phrases.py`（責務: 読み上げうる全文言の列挙）

チャイムが読み上げうる全文言を列挙する。ここで数えた文言が、そのまま「作り置きしなければならない文言」になる。Pi には実行時の音声合成が無く、声は文言の**完全一致**で `assets/voice/manifest.json` から引くため、列挙から漏れた文言はその文だけ無音になる。

| 関数 | 仕様 |
|---|---|
| `announcement_phrases(config)` | 時報の文言を、`start_hour`〜`end_hour`（`chime.scheduler.hourly_hours()`）の時刻ごとに返すジェネレーター。重複は除かず、`skip_hours` も見ない（休みにしている時刻の文言も作り置きしておく） |
| `closing_extra_text(config)` | `closing.extra_text`。`null`・空文字列・キーなしはどれも空文字列 |
| `closing_phrases(config)` | `closing.extra_text` があれば 1 件、無ければ空 |
| `quote_phrases(config)` | ひとことの全文言（`general`、`by_hour` の順） |
| `collect_phrases(config, include_quotes)` | 時報・閉館・ひとこと（`include_quotes` のときだけ）に、天気の全語彙（`weather.prerecord_phrases()`。`include_quotes` や `weather.enabled` に関わらず常に含む）を足し、前後の空白を落として、空と重複を除いて最初に現れた順に返す |
| `coverage(config, lookup, include_quotes=True, max_phrases=…, max_chars=…)` | `collect_phrases` が挙げる文言のうち、作り置きの声がある数・無い文言を数えて `Coverage`（`total` / `missing` / `by_kind` / `ok`）で返す。`lookup` は文言から WAV のパスを返す関数（呼び出し側は `TTSService.prerecorded_lookup` を渡す）。VOICEVOX には問い合わせないので、Pi 上でも使える。種類（`PHRASE_KINDS` = 時刻アナウンス・閉館の追加文言・ひとこと・天気）ごとの `(声がある件数, 全件数)` も持ち、同じ文言が複数の種類にあれば最初の種類に数える（種類ごとの合計が `total` と一致する）。起動のたびに呼ばれるので、数える手間は、件数 `max_phrases`（既定は 2000 件（`MAX_COVERAGE_PHRASES`））と、文言の文字数の合計 `max_chars`（既定は 2,000,000 文字（`MAX_COVERAGE_CHARS`））の両方までに抑える。設定の値（気温の幅など）で文言がそれを超えるときは、列挙せずに `CoverageTooLarge`（`ValueError` の子。メッセージは、何が多すぎる・長すぎるかと、直す設定のキーを日本語で述べる）を出す。気温の幅は 200 度分（`MAX_TEMP_SPAN`）まで。件数が少なくても 1 件が巨大な設定（地点の `label` が長い、テンプレートの書式指定の幅や桁数が大きい）も同じで、書式指定の幅や桁数は 1000（`MAX_FORMAT_SPEC`）まで。文言は作る前に数式で見積もり、上限を超えるなら作らずに数え上げを省く（`{label:>200000000}` のような短い書式指定 1 つが、数百 MB の文を作ってしまうため）。それぞれ `None` なら確かめない。既定の設定（138 件、約 4,000 文字）の結果は変わらない |
| `phrases_in_use(config)` | ひとことを含む使用中の全文言（`--prune` の判定に使う） |
| `phrases_to_generate(config, include_quotes)` | `config` の文言に既定設定の文言を足した和集合（`config.json` の配列は既定値を丸ごと置き換えるため） |
| `phrases_to_keep(config)` | `--prune` で残す文言。ひとことを含めた `phrases_to_generate` と同じ |
| `find_stale_entries(manifest, keep_phrases)` | manifest のうち、残す文言に無いエントリ（判定だけで、削除はしない） |

使うのは `scripts/generate_voicevox.py`（WAV の生成と `--prune`。同スクリプトからも `collect_phrases()` などを引けるよう再公開している）、`--check`・`--status` と起動時のログ（`coverage()`）、テスト（`tests/test_phrases.py`・`tests/test_voice_assets.py`・`tests/test_docs.py`）、CI の `prerecorded-only` ジョブ。

**層の規則:** `chime.audio` / `chime.sequence` / `chime.app` を import しない（pygame を引き込まない）。生成スクリプトは PC 側で動かすため、再生系の依存が無くても使えるようにしておく。`tests/test_phrases.py` が別プロセスで import して確かめる。

**文言の整え方:** 列挙は、実行時の照合（`TTSService`）と同じ `tts.normalize_phrase()`（前後の空白を落とす）を通し、整えて空になる文言は数えない。そろえないと、前後に空白のある `closing.extra_text` やひとことが、空白つきのキーで作り置きされ、実行時には引けずに永久に無音になる。

### 4.13 `chime/configcheck.py`（責務: 設定の検査）

検査の規則（どの値が error で、どの値が warning か）と、起動時の扱い（error のキーだけ既定値に置き換えて続ける）は 3.5 章。

| 関数 | 仕様 |
|---|---|
| `check_config(config)` | 全問題を、重大な順（error → warning → info）に返す。`config.sources` の設定ファイルを読み直して書き方を見て（`walk_overrides`）、マージ後の値を検査する（`validate`）。値の出どころの設定ファイルが分かる問題には `source` を付ける。設定ファイルを読み直せなければ warning |
| `walk_overrides(override, source)` | 設定ファイル 1 つを既定値と突き合わせ、知らないキー・廃止したキー（warning）と、既定値と同じ値（info）を返す。`_` で始まるキー（`_comment` など）・`time_signal.hour_readings` と `audio.commands` の中身・リストの要素は見ない |
| `validate(config)` | マージ後の設定の値を検査する（3.5 章の表）。キーが無い項目は見ない（既定値が使われるため）。既定の設定では何も返さない。検査は 1 項目ずつ独立していて、どれかが例外を出しても、その項目を「検査できませんでした」という warning にして続ける |
| `sanitized(config)` | error の出たキーだけを直した設定と、その error の一覧を返す（元の設定は変えない）。直し方は、そのキーを既定値に戻すこと。ただし `schedule.hourly.skip_hours`・`schedule.hourly.weekdays`・`schedule.closing.weekdays` は、壊れた要素だけを取り除き、残りは使う。`audio.commands` は、コマンドとして読めない項目だけを、その拡張子の既定の項目に置き換える（既定に無い拡張子の項目は取り除く）。warning と info の項目は変えない |

`Finding` は `level` / `key`（ドット区切りの設定キー。ファイル全体の問題は空）/ `message` / `hint`（直し方）/ `source` を持ち、`describe()` が 1 行の説明にする。error の `hint` には、直るまで使う既定値を添える。

### 4.14 `chime/check.py`（責務: 設置状態の点検 — `--check`）

現地で「この機械は放送できる状態か」を、**鳴らさず、何も書かずに**確かめる（ファイルもフォルダも作らず、権限も変えない。書き込みの確認は `os.access` と読み出しだけで行う）。次の 4 つの節に、`OK` / `情報` / `警告` / `NG` の行を並べる。

| 節 | 点検すること | NG・警告 |
|---|---|---|
| 設定 | 読み込んだ設定の出どころ（`既定値 → …/config.json`）と、`configcheck.check_config()` の結果（3.5 章）。書かれたとおりの設定を調べる | error → NG、warning → 警告、info → 情報（先頭の 5 件まで。残りは件数だけ）。休館日（`schedule.closures`）の使えない項目、範囲外・読めない音量（`audio.volume.*`）、`audio.max_segment_seconds`、`state.pause_file` は、どれも警告（ランタイムは鳴らす側に倒れる。3.5 章） |
| 作り置きの音声 | 放送で読み上げる全文言に、作り置き（`assets/voice/`）の声があるか（`phrases.coverage()`）。声を数えるときに引くのは作り置きだけで、VOICEVOX ENGINE の有無に関わらない（PC で ENGINE が動いていても、Pi と同じ答えになる）。ただし、放送が作り置きを引くのは `tts.engines` に `prerecorded` があるときだけなので、その有無を先に見る（`prerecorded_listed()`。`TTSService` と同じ読み方）。ファイルがあっても、空・開けない・WAV として壊れている・途中で切れているものは「声が無い」ものとして数える。ひとこと定義（`quotes.file`）を読めるかも見る | `tts.engines` に `prerecorded` が無い → NG（声がディスクにそろっていても、放送は作り置きを引かず、Pi では読み上げがすべて無音になる。この場合は声を数えない。設定の検査にも警告が出る）。フォルダが無い、または声の無い文言がある（先頭の 10 件を一覧。ファイルがあるのに使えないものがあれば「うち N 件は…」と添え、`git checkout -- assets/voice` で戻す案内を出す）→ NG。文言が多すぎる・長すぎて数えられない（4.12 章の上限）→ NG「読み上げる文言を数えられません」（理由と直す設定のキーを添える）。ひとこと定義が無い・壊れている・空 → 警告（内蔵の予備のひとこと 3 件で動く） |
| 音源 | 閉館アナウンス・蛍の光・時報音のファイルを、最後まで読めるか。空でないこと。WAV は、開けて、1 フレーム以上あり、ヘッダーに書かれたフレーム数の音のデータが最後まで入っていること（電源断で途中までしか書けなかったファイルを見つける）。MP3 は、先頭が ID3 か MPEG のフレームであること。4096 バイトに満たない MP3 は、途中で切れたものとみなす。そのうえで MPEG のフレームをファイルの終わりまで 1 つずつたどり（フレームの長さは、版・レイヤー・ビットレート・サンプリング周波数・パディングから求める）、最後のフレームがファイルの終わりを越えていないか、フレームの途中でないところに MP3 でないデータが挟まっていないかを確かめる（先頭の ID3v2 タグは、重なっていても 4 つまで読み飛ばす。末尾の ID3v1 タグ（128 バイト）・APEv2 タグ・1024 バイトまでの 0 埋めは正常と数える。0 埋めを小さく取るのは、電源断で OS が長さだけ伸ばして中身を 0 で埋めたファイルを見逃さないため）。**ID3 タグのあとにフレームが無い**ものも NG にする（電源断で、大きさは記録されたのに先頭のブロック（タグ）しかディスクに届かなかったファイルは、先頭の形も大きさも正しいので、フレームをたどる前の確かめでは通ってしまう）。タグの大きさに数えられていない余分なデータ（0 埋めなど）が挟まる MP3 もあるので、タグの直後でなくてもよく、タグの直後から 65,536 バイトの範囲に、続けて 2 つのフレームのヘッダー（1 つ目のヘッダーが示す長さの先に、次のヘッダーがある）が見つかれば、フレームはあるとみなす（この場合は、その先のフレームをたどらず、問題なしとする）。見つからなければ NG（「ID3 タグのあとに MP3 のフレームがありません」）。読む権限が無いものも NG | 同梱の音源が無い・壊れている → NG（既定の場所なら `git checkout -- <ファイル>` を案内）。時報音がまだ無いのは警告（放送のときに自動で作る。`bash scripts/setup.sh --no-apt`）。時報音が無く、作る場所にも書けないときは NG。時報音があるのに開けないのも NG |
| 書き込み | 放送が書き込む場所（状態のフォルダ・履歴のフォルダ・TTS キャッシュ）に書けるか（まだ無いフォルダは、実在する一番近い親に書けるか）。状態ファイル・履歴ファイルがフォルダになっていないか。既にある履歴ファイルに追記できるか。状態ファイルが sticky ビットのフォルダ（`/tmp` など）にあるとき、フォルダも既存の `state.json` も別の利用者の持ち物でないか。休止の記録（`state.pause_file`。4.21 章）も同じ節で見る: 止めている最中なら情報（`止めています: 10/30（点検）。再開するには python3 campus_chime.py --pause-off を実行します`）、フォルダだった・読めない・中身が使えない（壊れている・知らない版）なら警告。無い・期限が切れている・保存先が空のときは何も出さない | **NG にするのは、再生済みの記録（`state.json`）を残せなくなるものだけ**（状態のフォルダに書けない、状態ファイルがフォルダ）。履歴のフォルダ・ファイルと TTS キャッシュは、書けなくても放送が止まらないので警告。休止の記録は、どんな状態でも放送が止まらない（使えない記録は「止めていない」ものとして鳴る）ので、NG にしない（警告にとどめる）。sticky ビットのフォルダで、フォルダも既存の `state.json` も他人の持ち物なら NG（置き換えて保存できない） |

**直し方の `chown` は、設定が指す場所そのものにしか使わない。** 実在する祖先（`/` や `/var/lib` など、設定の持ち物でないもの）を巻き込まない。

- まだ無い場所（実在する親に書けない）: その 1 つだけを作って渡す。再帰しない。例: `sudo mkdir -p /home/pi/campus-chime/cache && sudo chown pi:pi /home/pi/campus-chime/cache`
- あるのに書けない（root など、他人の所有）: そのフォルダと中身を再帰して渡す。例: `sudo chown -R pi:pi /home/pi/campus-chime/cache`
- あるのに書けない（実行している利用者の所有）: 権限（モード）の問題なので `chmod u+rwx <フォルダ>`（ファイルなら `chmod u+w <ファイル>`）
- 既にあるファイル（履歴）が他人の所有で追記できない: そのファイル 1 つだけに、再帰なしの `sudo chown 利用者:グループ <ファイル>`
- OS の場所（`/`・`/var/lib`・`/etc` など）を設定が指しているとき: 持ち主を変えずに、設定を書ける場所に変える案内（`OS の場所 … の持ち主は変えられません。…`）

root 所有の**ファイル**は、それだけでは NG にしない。`state.json` は一時ファイルへ書いてから置き換えて保存するので、root 所有のファイルが残っていても、置き場所のフォルダに書ければ保存できる。フォルダ（既定では `cache/`）が root 所有で書けないと、再生済みの記録を保存できず、再起動のたびに同じ回が再生される（KNOWLEDGE_BASE.md 4-6）。この場合は、フォルダの行が NG になる。**例外は sticky ビットのフォルダ**（`/tmp` など）で、そこでは、ファイルを消す・置き換えられるのは、そのファイルかフォルダの持ち主だけなので、フォルダにも既存の `state.json` にも自分の持ち物でないと、一時ファイルからの置き換えを OS に断られ、保存できない。この組み合わせ（`state.file` を共有の sticky フォルダに置き、`state.json` が別の利用者のもの）は NG にして、`state.json` 1 つに再帰なしの `sudo chown 利用者:グループ <ファイル>`（または設定の `state.file` を、サービスの利用者のフォルダの中に変える）を案内する。既定の `cache/` は sticky ビットが無いので当てはまらない。root で実行しているときは、何でも書けてしまい権限を調べても意味がないので、「所有者」の行を情報で出す（`pi` ユーザーで実行すると確認できる）。

「設定」の節は書かれたとおりの設定を、ほかの節は実際に動く設定（error のキーを既定値に戻したもの。サービスはこちらで動く）を調べる。終了コードは、NG が 1 つでもあれば `1`、なければ `0`（4.11 章）。設定ファイルを読めないとき（`2`）は、設定を読み込む側（`chime.cli`）が、ここへ来る前に返す。

### 4.15 `chime/status.py`（責務: いまの状態の表示と、放送の最中の待機 — `--status` / `--wait-idle`）

**`--status`** は、現地で「いま、ちゃんと動いているか」を一目で見るための表示で、何も書かない。表示するのは、版とコミット（4.17 章）・現在時刻・時刻の同期（NTP）・サービスの動作の状態と自動起動の状態・再生方法・作り置きの音声の集計（`phrases.coverage()`。放送が実際に引く声を数える。`tts.engines` に `prerecorded` があればファイルの中身まで確かめ（`--check` と同じ）、無ければ放送は作り置きを引かないので、すべて「声が無い」と数える。起動時のログが `0/138` と言うのと同じ）・放送の休止（`止めていません` / `止めています: 10/30（点検）`。記録があるのに使えない〔壊れている・読めない・フォルダ・知らない版〕ときは `止めていません（休止の記録を使えません。--check で確かめてください）`。判定は `--check` の「書き込み」の警告と同じ（`check.pause_record_unusable()`）で、放送は止めていないものとして鳴るので「要確認」にはしない。4.21 章）・直近の放送（4.16 章の履歴の新しい順に 8 件）・次の予定（3 件）（止めているあいだは、止めているあいだを省いて休止が明けてから数え、見出しを「次の予定（休止のあいだは省いています）」にする）・近い休館日（`休館日（今後 14 日）` の節。今日から `CLOSURE_DAYS` 日以内の、休館のために鳴らさない日と理由。曜日の決まりでもともと鳴らない日は挙げず、無ければ節ごと出さない）。外のコマンド（`timedatectl show -p NTPSynchronized --value`、`systemctl is-active` / `is-enabled campus_chime.service`）は 3 秒で打ち切り、呼び出し側から差し替えられる（テストでは本物を呼ばない）。実行できない・応答しない・出力を読めないものは「確認できません」と表示し、問題とは数えない。

気になる点（終了コード `1` の理由。末尾に「要確認:」として並び、時刻の同期・サービス・再生方法・作り置きの音声の行は行末にも「← 要確認」が付く）は次のとおり。

- 時刻が NTP と同期していない
- サービスが `active` ではない（調べられたとき）
- 再生方法が `mock` で、実機の Linux で動いている（開発環境の `mock` は問題にしない）
- 直近の放送が失敗（`failed` / `error`）で終わっている
- 直近の放送が、停止要求で途中で止まった（`interrupted`。サービスの停止や再起動と重なったと思われる。次の放送が最後まで鳴れば消える。失敗とは別の理由として挙げる）
- 直近の放送で、音源ファイルが無くて積めなかった必須の部品があった（履歴の `missing`。結果が `partial` でも気にする。`--check` で確認）
- 直近の放送が、組み立てに失敗して簡易の内容（時報音だけ、または閉館アナウンスと蛍の光だけ）で鳴った（履歴の `degraded`。結果は `ok` のままなので、この項目で見分ける。時刻アナウンスやひとことが入っていない。原因は `journalctl -u campus_chime.service`）。直近の 1 件だけを見る
- `tts.engines` に `prerecorded` が無い（放送が作り置きを引かないので、声がそろっていても Pi では読み上げがすべて無音。`--check` で確認）
- 作り置きの音声が足りない、または「数えられません」（文言が多すぎる・長すぎて数え上げを省いたときは、理由と直す設定のキーを日本語で表示する。例外の型名は見せない）

自動起動の状態（`enabled` / `disabled` / `not-found`）は表示するだけで、気になる点には数えない。放送を止めていること（休止）も、利用者の意思なので数えない。直近の放送の判定（上の 3 つの項目）では、休止で鳴らさなかった記録（`skipped`）は飛ばす（止めていた日の記録が、その前の放送の失敗を隠してしまわないように）。直近の放送の 1 行は、日時・種類（時報／閉館放送）・結果（成功／一部のみ／失敗／エラー／止めていた／中断）・止めた理由（`reason`。`休止: 設備点検`）・エラーの説明・停止要求で中断した放送の `N/M 件を鳴らして停止`・「簡易の内容で放送」（履歴の `degraded` が `true` のとき）・欠けた音源（3 件まで）・音にならなかった文言（3 件まで）で、読めない項目があっても例外にしない（履歴は人が書き換えることもある）。

**`--wait-idle`** は、更新（`scripts/update.sh`）のとき、放送の最中にサービスを再起動して放送を途切れさせないよう、放送の時間帯が終わるまで待つ。放送の時間帯は、イベントの準備の開始（`prepare_at`）の 10 秒前から、再生開始（`play_at`）の 90 秒後（時報）・300 秒後（閉館放送。蛍の光が長い）まで。日付をまたぐ時間帯のために、前日と翌日のイベントも見る。5 秒ずつ眠って現在時刻を読み直し（NTP の補正で時刻が動いても追従する）、時間帯の外になれば `0`、上限まで待っても終わらなければ `1` を返す。上限は既定・最大とも 360 秒（閉館放送の時間帯を 1 回分まるごと待てる長さ。それより大きい値を指定しても 360 秒に丸める）。

### 4.16 `chime/history.py`（責務: 放送の履歴）

`cache/history.jsonl`（`state.history_file`）に、1 回の放送につき JSON を 1 行追記する。`--status` で直近の放送を見るためのもので、放送そのものには影響しない（書けなくても放送は止めない）。1 行の内容は次のとおり。

| キー | 内容 |
|---|---|
| `v` | 行の形式の版（`1`。読むときは、知らない版の行を飛ばす） |
| `at` / `day` | 放送の予定の日時（タイムゾーン付きの ISO 8601、秒まで）と、その日付 |
| `key` / `kind` | イベントの識別子（`hourly:12`・`closing`）と、種類（`hourly`・`closing`） |
| `result` | 通常は、`error`（放送が例外で終わった）→ `failed`（1 つも鳴らせなかった）→ `partial`（一部しか鳴らせなかった、音にならなかった部品がある、または音源ファイルが無くて積めなかった部品がある）→ `ok`（すべて鳴らせた）の順に判定する。呼び出し側が決める結果が 2 つある: `skipped`（休止＝「今日だけ止める」のために鳴らさなかった。再生していないので `played` / `total` は 0）と、`interrupted`（停止要求で再生の途中で止まった。`played` は止まるまでに鳴らせた数で、`error` は付けない）。読む側は、知らない `result` の値を見ても落ちない（飛ばさず、そのまま表示する）。この 2 つは後から足した値で、行の形式の版 `v` は上げていない |
| `played` / `total` | 鳴らせた部品の数と、鳴らすはずだった部品の数 |
| `silent` / `warnings` | 音にならなかった文言と、組み立ての警告 |
| `missing` | 音源ファイルが無くて、組み立てのときに積めなかった必須の部品の名前（時報音・閉館アナウンス・蛍の光）。`total` に数えていない（積めなかった部品は再生の件数に現れない）ので、`played == total` でも `result` は `partial` になる。この項目の無い古い行は、無かったものとして読む（行の形式の版 `v` は、項目を足すだけなので上げていない） |
| `degraded` | 組み立てに失敗して、最小構成で鳴らしたか。結果は `ok` のままになりうる（鳴らせた部品がすべて鳴ったため）ので、`--status` は行末に「簡易の内容で放送」と添え、直近の放送ならば気になる点に数える（4.15 章）。この項目の無い古い行は、無かったものとして読む |
| `error` | 放送が例外で終わったときの説明（あるときだけ） |
| `reason` | 結果の理由（あるときだけ）。`skipped` では `休止: 点検`（止めた理由があれば `休止: ` に続けて、無ければ `休止` だけ）。後から足した項目で、この項目の無い古い行も読める |

| メソッド | 仕様 |
|---|---|
| `History.append(entry)` | 1 行で追記する。書けたら `True`、書けなければ WARNING を残して `False`（例外は出さない）。前の書き込みが途中で切れていたら（電源断）、改行で区切ってから足し、壊れるのを切れた 1 行だけにする。600 行（`MAX_LINES`）を超えたら、新しい 500 行（`KEEP_LINES`）だけに書き直す（一時ファイルへ書いてから置き換えるので、途中で電源が落ちても履歴を失わない。書き直しに失敗しても追記はできているので `True`） |
| `History.recent(limit)` | 新しい順に、最大 `limit` 件を返す。空行・壊れた行・形が違う行・知らない版の行は飛ばす（件数にも数えない）。ファイルが無ければ空、読めなければ WARNING を残して空。例外は出さない |

### 4.17 `chime/buildinfo.py`（責務: 版とコミットの表示）

`--version`・`--status`・起動時のログが、「いま動いているのはどのコミットか」を示すために使う。`git` コマンドは呼ばず（Pi に入っていないこともあり、遅くもなるので）、`.git` の中のファイルを直接読む。

| 関数 | 仕様 |
|---|---|
| `commit_id(base_dir)` | 現在のコミットを短い形（7 桁）で返す。`.git` はディレクトリでも、`gitdir: <場所>` と書いたファイル（git worktree・サブモジュール）でもよい。`HEAD` はブランチ名でもコミット ID そのもの（detached HEAD）でもよく、ブランチ名は個別のファイル、無ければ `packed-refs` から探す。分からなければ `"unknown"`（例外は出さない。調べものが原因で起動や表示を止めないため） |
| `version_string(base_dir)` | `campus-chime 6.1.0 (a1b2c3d)` の形。コミットが分からなければ `(unknown)` |

### 4.18 `chime/jsonfile.py` / `chime/logsetup.py`（責務: JSON の読み書き・ログの設定）

| モジュール | 仕様 |
|---|---|
| `chime/jsonfile.py` | JSON の読み書きの共通処理。設定・状態・ひとこと・作り置きの目録は、人がメモ帳などで手直しすることがあるため、起きやすい失敗（Shift_JIS で保存、全角の引用符やカンマ、最後の項目のあとのカンマ）を、生の例外ではなく「どこを・どう直すか」の日本語の案内（`JsonFileError`。`kind` は `missing` / `encoding` / `syntax` / `io`）にして返す。BOM 付き UTF-8 も読める。`write_json_atomic()` は一時ファイルへ書いてから置き換える |
| `chime/logsetup.py` | ログの設定。systemd は、標準出力の各行を接頭辞が無ければ info として記録するため、`<3>` などの sd-daemon の接頭辞を全行に付け、`journalctl -p err` で絞り込めるようにする。接頭辞は、標準出力が journal に繋がっているとき（環境変数 `JOURNAL_STREAM` が一致するとき）だけ付ける。ハンドラは重複させない。タイムスタンプは `timezone` 設定に合わせる。`emit_early_error()` は、設定を読めずにログを設定できないときの、標準エラー出力へのエラー表示 |

### 4.19 `scripts/`（運用スクリプト）

| スクリプト | 役割 |
|---|---|
| `setup.sh` | 導入（冪等）。設置パスの確認 → 依存パッケージ → タイムゾーンと NTP の確認 → 空の `config.json` の用意 → 時報音の生成と、時刻アナウンスの音声の確認 → **設置状態の点検（`--check`）** → 予定表の表示 → systemd への登録・再起動。点検の終了コードが `2`（`config.json` を読めない）のときだけ、予定表の表示を省き、サービスを再起動しない（読めない設定で再起動すると、起動できないまま再起動を繰り返すため）。このとき最後に「導入は途中です」と表示して**終了コード 1** で終わる（途中で止めた導入を「完了」と言わず、`update.sh` が失敗と分かるようにする）。サービスを触らない指定の `--no-service` を付けても（再起動は元から行わない）、同じく「導入は途中です」と表示して終了コード 1 で終わる。`--generate-assets` が `2` のときは、作り置きが無いのではないので、PC で作り直す案内を出さない。点検が `1`（NG）のときは、「直し方」に従うよう警告して続ける。サービスの再起動に成功したときは、そのときのコミットを `cache/deployed_commit` に記録する（`update.sh` が、サービスが最新のコードで動いているかを見分けるのに使う。書くのは実行している利用者で、root で実行したときは書かない。書けなくても導入は止めない）。`--no-apt` / `--no-service` |
| `update.sh` | 運用中の Pi を最新版へ更新する（冪等。`pi` ユーザーで実行し、root では拒否する）。現在の版の表示 → 手元の変更（Git 管理下のファイル。`config.json` は対象外）の確認 → ブランチ上かの確認 → `git pull --ff-only` → （新しい版が届いたとき、またはサービスに反映した版が違うとき）`--wait-idle` → `setup.sh --no-apt` → 旧い版と新しい版、`--status` の表示。止まる条件（root・手元の変更・`git` が状態を読めない（フォルダの持ち主が違うなど）・detached HEAD・取り込みの失敗）では、理由を表示して終了する（取り込みが権限またはディスクの問題で失敗したときを除き、**何も変えない**）。取り込み（`git pull`）の失敗は、`git` の表示（標準エラー出力。翻訳されて語句が合わなくならないよう、この 1 回だけ `LC_ALL=C` で実行する）から原因を見分けて、直し方を案内する。(1) ネットワークにつながらない、または履歴が GitHub の履歴と食い違っている（既定の案内。見分けられないもの、`Permission denied (publickey)` など GitHub への接続・認証の失敗も、ここに入る）。(2) 権限（`Permission denied`・`insufficient permission`・`unable to unlink`・`could not open`）: 以前に `sudo` で `git` を実行したなどで、リポジトリの中に別の持ち主（root など）のファイルが残っている。ネットワークの問題ではないと伝え、`sudo chown -R 利用者:グループ <リポジトリ>` を案内する。取り込みが途中まで進んでいることがあるので「何も変更していません」とは言わず、次の実行で「手元の変更」として案内が出ることを伝える。(3) ディスクの問題（`No space left on device`・`Read-only file system`・`Input/output error`・`Disk quota exceeded`）: このリポジトリのあるディスク（Pi では SD カード）がいっぱいになった、エラーのあとで読み取り専用に切り替わった（電源断や SD カードの劣化のあとに起きる）、容量の割り当てを超えた、など。持ち主を直しても直らないので、権限の語句（`unable to unlink`・`could not open` など）と一緒に出ても、**権限より先に**ディスクの問題として見分ける。ネットワークの問題ではないと伝え、確かめ方として `df -h <リポジトリ>`（空き容量。Use% が 100% に近い、または Avail が 0 ならいっぱい）と `dmesg \| tail -n 30`（ディスクのエラー。`I/O error` や `Remounting filesystem read-only`。権限が無いと言われたら `sudo dmesg \| tail -n 30`）を案内する。取り込みが途中まで進んでいることがあるので「何も変更していません」とは言わず、次の実行で「手元の変更」として案内が出ることを伝える。(4) Git 管理外のファイルが上書きされる（`would be overwritten`）: 手元にある Git 管理外のファイルが、新しい版のファイルと同じ名前なので `git` が止めた。そのファイルを移す（`mv`）か消してから、もう一度実行するよう案内する。手元の変更には、ファイルごとに `git restore --source=HEAD --staged --worktree -- <ファイル>` を案内する（`git add` 済みの変更も HEAD の内容に戻す。`git restore` が無い 2.23 より前の git では `git checkout HEAD -- <ファイル>`）。**すでに最新版でも**、`cache/deployed_commit`（サービスに反映した版）がいまの版と同じでなければ、前回の更新が取り込みのあとで止まって、サービスが古い版のままかもしれないので、警告して続き（放送の確認と `setup.sh --no-apt`）を行う。記録がいまの版と同じなら、`--status` を見せて終わる。取り込みのあとで止まるのは、次の 3 つ（いずれも終了コード `1`。最初の 2 つはサービスを再起動しない。`setup.sh` の失敗は、再起動の前に止まったことが多いが、再起動されたかは分からない）。`--wait-idle` が上限まで待っても終わらなかったとき、`--wait-idle` が `2`（`config.json` を読めず、放送の時間帯を調べられない）を返したとき（`config.json` を直してから再実行するよう案内する。急ぐときも先に直す。読めないままでは、`setup.sh` もサービスを再起動しない）、`setup.sh` が失敗したとき。どれも、もう一度 `update.sh` を実行すれば続きから反映する（記録が合わないため）。元の版への戻し方（`git reset --hard <元のコミット>` と `setup.sh --no-apt`）は、コードが更新された回（取り込みのあとで止まった回を含む）に表示するだけで、実行しない。続きから反映する回（コードが最新のとき）には表示しないので、止まった回の表示を控えておく |
| `security_check.sh` | セキュリティの点検（読むだけ。何も書かず、何も変えない）。`pi` でも `sudo` 付きでも動き、root が要る項目は `pi` では「不明」と出す。アドレスがプライベートかグローバルか（学外から届きうるか）、SSH・sudo・防火壁・OS とカーネルの更新・時刻の同期・使っていないサービス・ログの保存・時報サービスの隔離・リポジトリを調べ、項目ごとに `[OK]`・`[情報]`・`[注意]`・`[要対応]`・`[不明]` と直し方（`harden.sh` の手順）を出す。終了コード 0（要対応なし）／1（要対応あり）／2（使い方の誤り）。`--user` |
| `harden.sh` | セキュリティの強化（root で実行）。手順 `ssh`・`firewall`・`sudo`・`services`・`journal`・`updates`・`service`・`ntp`（`recommended` は `services journal ssh firewall`）を選んで実行する。どの手順も、変える前にすべての条件を確かめ、変えたあとに確かめ、確かめられなければ自分で戻す。`--rollback` で元に戻せる。`ssh` と `firewall` は、10 分以内に新しい画面から `--confirm` しなければ自動で元に戻る（締め出し防止）。`--dry-run`・`--user`・`--revert-after`・`--ssh-from`・`--ntp-server`・`--test-sound`・`--force` |
| `generate_voicevox.py` | PC 側で作り置き（`assets/voice/`）を生成・整理する（`--config`・`--include-quotes`・`--prune`）。各 WAV は、同じフォルダの一時ファイルへ書いてから `os.replace` で置き換える（失敗・中断（Ctrl-C を含む）で欠けた WAV を残さない。残ると、次の実行で「生成済み」として飛ばされ、欠けた声がそのまま使われるため。`--force` で作り直すときも、置き換えるまで元の WAV は残る）。文言は `normalize_phrase()` で整える |
| `dump_example_config.py` | `config.example.json` を `DEFAULT_CONFIG` から書き出す（3.1 章） |
| `tag_releases.py` | 版のタグ付け（10.1 章） |


### 4.20 `chime/closures.py`（責務: 休館日の暦）

設定の `schedule.closures` を読み、ある日が休館日かどうかを答える。曜日の決まり（平日だけ鳴らす）とは別に、「学園祭の日」「年末年始」のような、その年だけの休みを足すためのもの。**祝日は自動では休まない**（祝日も開館するため。書いた日だけ休む）。休館日は、時報も閉館放送も鳴らさない（`Scheduler.events_for_date` が空を返す。4.8 章）。

| 名前 | 仕様 |
|---|---|
| `ClosureCalendar(settings)` | `schedule.closures` から作る暦。`closures`（使える項目。`dates` を先に、`ranges` を後に、書いた順）と `problems`（使わない項目の日本語の説明。何番目の項目か・なぜ使えないかを含む）を持つ。作るときに、使わない項目を 1 件ずつ ERROR「休館日の設定に使えない項目があります」で記録する |
| `closure_on(day)` | `day` が休館日なら、最初に当たった項目（`Closure`）。休館日でなければ `None`。1 日だけの項目は日付で引く（長い一覧でも、予定の探索で毎日全件を見ない） |
| `closed_reason(day)` | 休館日なら `休館: 学園祭`（`name` が無ければ `休館`）、そうでなければ `None` |
| `Closure` | `first`〜`last`（両端を含む。1 日だけなら同じ日）と `name` |

項目の書き方（`dates` と `ranges`。どちらも配列）:

- `dates` の項目: `"2026-11-02"`、または `{"date": "2026-11-02", "name": "学園祭"}`
- `ranges` の項目: `{"from": "2026-12-28", "to": "2027-01-04", "name": "年末年始"}`（`from` と `to` の両端を含む。`from` は `to` と同じ日か前）
- 日付は半角の `YYYY-MM-DD` で、カレンダーにある日付（`date.fromisoformat` は版によって受け付ける形が違うので、使わずに自分で読む）。`name` は省ける（`null` も可）。文字でなければ使わない。空白は 1 つに詰めて 1 行にする（ログや一覧を 1 行に保つ）

**失敗は鳴らす側に倒す。** 放送の安全のため、どんな JSON の値を渡しても例外にしない。書き方の正しくない**項目だけ**を無視し（その日は休まず鳴る）、理由を `problems` に残す。無視するのは、日付が文字でない・形が違う・カレンダーにない、`date` / `from` / `to` が無い、`from` が `to` より後、`name` が文字でない、項目の形が違うとき。`dates` や `ranges` がリストでない（`null` は「なし」）ときはその一覧を、節そのものが `{ … }` でないときは節ぜんぶを使わない。ほかの項目は、これらに巻き込まれず使われる。`--check` の「設定」は同じ `ClosureCalendar` を使い、`problems` をそのまま警告にする（3.5 章）。`--status` は近い休館日を挙げ（4.15 章）、`--schedule` は休館日を「休館 …」の行で示す（4.11 章。「休止」は `--pause-*` で止めたほうの言い方で、行の頭には付けない）。

### 4.21 `chime/pause.py`（責務: 「今日だけ止める」の記録）

行事や点検のあいだだけ、時報と閉館放送をまとめて止めるための記録（`cache/pause.json`。`state.pause_file`）。休館日の設定（4.20 章）とは別に、現地で `--pause-today` などから一時的に足し、期限が来れば自然に効かなくなる。書くのは `--pause-*`（4.11 章）、読むのは常駐ループ（4.10 章）・`--schedule`・`--status`・`--check`。記録は今日から `last_day` まで（両端を含む）で、長くても 31 日（`MAX_PAUSE_DAYS`）まで。消し忘れて何週間も鳴らないままにならないよう、上限と期限を設けている。

```json
{
  "v": 1,
  "first_day": "2026-10-30",
  "last_day": "2026-11-05",
  "set_at": "2026-10-30T12:30:00+09:00",
  "reason": "設備点検"
}
```

| 名前 | 仕様 |
|---|---|
| `PauseStore(path, today)` | `today` は「今日」を返す関数（`ChimeApp` は設定タイムゾーンの今日を渡す。時計やタイムゾーンをここから切り離す） |
| `current()` | 今効いている記録（`Pause`）。無い・読めない・壊れている・知らない版（`v`）・期限切れ（`last_day` が今日より前）は `None`。**例外は出さない。** 壊れているときだけ WARNING「休止の記録を使えません（放送は通常どおり行います）」を残す（同じファイルの同じ問題につき 1 度。ファイルが書き換わるとまた 1 度）。ファイルは、読む前に `os.stat` で種類と大きさを確かめ、**普通のファイルでないもの（フォルダ・名前付きパイプ・デバイス）と 1 MiB（`MAX_PAUSE_BYTES`）を超えるものは、開かずに**同じ扱いにする（名前付きパイプを開くと書き手が現れるまで固まり、常駐ループごと止まる。巨大なファイルは、時報の直前の確認を遅らせる） |
| `covers(day)` | `day` が止めている期間に入っているか（`current()` と同じく、例外を出さない） |
| `set(last_day, reason, now)` | 今日から `last_day` まで止める記録を書いて返す（すでにあれば置き換える。一時ファイルへ書いてから置き換える）。`last_day` が過去、または今日を含めて 31 日を超える、または `reason` が UTF-8 で書けない（端末の文字コードが壊れた引数）ときは、何も書かず `ValueError`（利用者向けの日本語）。書けなかったときは `OSError`。`set_at` は UTC からのずれ付き・秒単位 |
| `clear()` | 記録のファイルを消す。消せたら `True`、元から無ければ `False`。壊れていた・期限が切れていたファイルも消す。無いこと以外の理由で消せなければ `OSError` |
| `changed()` | 前に呼んだときから、ファイルが作られた・消えた・置き換わったか（更新時刻・大きさ・inode・有無を比べる）。最初の呼び出しは基準を覚えるだけで `False`。調べられなければ（`os.stat` の失敗。場所に NUL 文字などが混ざっていて `ValueError` になる場合を含む）無いものとして扱い、例外は出さない。常駐ループが、待機を切り上げて予定を選び直す合図に使う（4.10 章） |

**止める日の決め方。** 止める日は、イベントの鳴る予定の日（`Event.at` の日付）で決める。0 時の時報は、「ポーン」を先行させるので前日の 23:59:57 に鳴り始めるが、それは翌日の放送として扱う（前日だけを止めても止まらず、翌日を止めれば止まる。休館日〔4.20 章〕も同じ決め方）。

読むときは、最上位が `{ … }`、`v` が整数の `1`（`true` は不可）、`first_day` / `last_day` が半角の `YYYY-MM-DD` でカレンダーにある日付、`last_day` が `first_day` と同じ日か後、期間が 31 日以内、`reason` が文字（省くと空）、のすべてを確かめる。どれかが欠けると、記録は「止めていない」ものとして扱う。`set_at` は人に見せる参考の項目なので確かめない（末尾の `Z` / `z` は、どの Python の版でも `+00:00` として読む。無い・文字列でない・読めない書き方のときは「不明」として扱い、記録は有効なまま）。**失敗は鳴らす側に倒す**（壊れた記録のせいで放送が止まり続けることは無い）。

---

## 5. 処理フロー（時報）

```
[systemd 起動]  ← After=time-sync.target（NTP の同期完了までは待たない）
      ↓
[次の正時 - 3秒 - 45秒 まで待機]  ← 30 秒ごとに時刻を読み直すため、起動直後の NTP による時刻の飛びに追従できる
      ↓
[休止中（--pause-*）なら取りやめ]  ← 休館日の日は、そもそも予定に入らない。休止は鳴らさず、履歴に skipped を残す
      ↓
[天気取得 / 読み上げ音声の用意（作り置きから引く）]  ← 失敗しても続行
      ↓
[正時 - 3秒 まで精密待機]  ← 休止の記録が書き換わっても、正時直前の 0.25 秒は待ちを切らない
      ↓
[再生の直前にもう一度、休止を確かめる]
      ↓
09:59:57 ポ ─ 09:59:58 ポ ─ 09:59:59 ポ ─ 10:00:00 ポーン
      ↓
[「午前10時をお知らせしたのだ。」]
      ↓
[天気予報]  ← weather_hours の時刻のみ（既定は 12 時だけ）
      ↓
[ひとこと]
      ↓
[mixer 解放 / 再生日を state.json に記録] → 次イベントへ
```

---

## 6. systemd ユニット仕様

```ini
[Unit]
Description=Campus Chime System (hourly time signal and closing announcement)
Documentation=https://github.com/hiroyuki-rdx/chime-5pm
Wants=time-sync.target
After=time-sync.target sound.target network.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/home/pi/campus-chime
Environment=SDL_AUDIODRIVER=alsa
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /home/pi/campus-chime/campus_chime.py
Restart=always
RestartSec=10
TimeoutStopSec=30
SupplementaryGroups=audio
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 6.1 各設定の根拠

| 設定 | 根拠 |
|---|---|
| `Wants` / `After=time-sync.target` | 本機は RTC 非搭載。`After=time-sync.target` だけでは NTP の同期完了までは待たない（`systemd-time-wait-sync.service` が有効な場合に限り、同期完了まで待つ）。本アプリは同期を待たずに起動しても、スケジューラが 30 秒ごとに時刻を読み直すため（4.8 章）、起動直後の NTP による時刻の飛びに追従できる。`systemd-time-wait-sync` は Wi-Fi が無いと起動しなくなるため有効にしない（NFR-04） |
| `Environment=SDL_AUDIODRIVER=alsa` | Lite 環境ではデスクトップ由来のサウンドサーバーが存在しないため、SDL の出力先を ALSA に明示する |
| `Environment=PYTHONUNBUFFERED=1` | 標準出力のバッファリングで journal へのログ到達が遅れるのを防ぐ |
| `Restart=always` / `RestartSec=10` | v1.x の `on-failure` では正常終了扱いのケースで復帰しない。専用機であり常時稼働が前提のため `always` とする（NFR-03） |
| `TimeoutStopSec=30` | 停止要求（`SIGTERM`）を受けた再生は 1 秒ほどで止まる（4.3 章）ので、30 秒は止まらなかったときの保険。以前は既定の 90 秒を待って強制終了していた。この値は `setup.sh` がユニットを登録し直すときに反映される |
| `SupplementaryGroups=audio` | `pi` が `audio` グループに属していない環境でも音声デバイスへアクセスできるようにする |
| `NoNewPrivileges` / `PrivateTmp` | 最低限の保護。`cache/` への書き込みが必要なため `ProtectHome` は設定しない |
| `WorkingDirectory` | 設置パスと一致させる。v1.x の不一致による起動失敗を防ぐ |

### 6.2 v1.x からの削除

- `After=... audio.target` の `audio.target` は systemd の標準ターゲットとして存在しないため削除。サウンド関連は `sound.target` が正しい。

---

## 7. 音声ファイル仕様

| 項目 | time_signal.wav | announce.wav | hotaru.mp3 | TTS 出力 |
|---|---|---|---|---|
| 用途 | 時報音 | 閉館アナウンス | 楽曲 | 時刻・ひとこと・天気 |
| 生成 | 無ければ実行時にコードで合成（`--generate-assets` で作り直し） | 同梱 | 同梱 | 作り置き（`assets/voice/`。PC 側で VOICEVOX により生成し同梱） |
| 形式 | 44.1kHz / 16bit / ステレオ | 24kHz / 16bit / モノラル | MPEG-1 Layer3 128kbps / 44.1kHz | 24kHz / 16bit / モノラル |
| フェード | 各トーンに 5ms | なし | フェードイン 2000ms | なし |
| 権利 | 自作（合成） | VOICEVOX 利用規約に従いクレジット表記 | Public Domain | VOICEVOX 利用規約に従いクレジット表記 |

---

## 8. エラーハンドリング仕様

| 事象 | 挙動 |
|---|---|
| `pygame` 未導入 | 外部コマンド再生へフォールバック。それも不可なら ERROR ログを出して mock 動作 |
| 音源ファイル欠落（必須。組み立てのとき） | ERROR ログを出し、その部品だけを積まずに残りを鳴らす。積めなかった部品は履歴の `missing` に残り、結果は `partial`（4.9・4.16 章）。`--check` が NG として見つける |
| 音源ファイル欠落（必須。組み立てのあと、再生の直前に消えた） | `PlaybackError`。ERROR ログを出し、当該回をスキップ（プロセスは継続）。履歴には `error` として残す。`--say`/`--test-hourly`/`--test`/`--test-all` から実行した場合は、この失敗が `ChimeApp.play()` の戻り値に反映され、終了コードにも影響する（4.11 章） |
| 音源ファイル欠落（任意） | WARNING ログを出し、そのセグメントのみスキップ |
| 音声合成の全エンジン失敗 | ERROR ログ。当該セグメントを落として残りを再生（**時報音は鳴る**） |
| 組み立て中の想定外の例外（通信の途中切断、読み上げ文のテンプレートの書き間違いなど） | ERROR ログ。**部品ごとに縮退**する。失敗した部品（時刻アナウンス・天気・ひとことなど）だけを落として残りを組み立て、**時報音は必ず鳴る**。時刻アナウンスのテンプレートを書き間違えたときは、既定の文言で読む |
| 組み立て全体の失敗 | 最小構成で再生する（時報音は鳴る） |
| 閉館放送の一方の欠落（アナウンスか蛍の光のどちらかを用意できない） | ログに記録し、欠けた側だけを落として、残りは鳴らす（欠けた側は履歴の `missing` に残り、結果は `partial`） |
| `config.json` の文字コード不正（UTF-8 以外。Shift_JIS など）・JSON の書き間違い | 「UTF-8 で保存し直して」「N 行 M 文字目で…」のように、日本語で原因を案内し、**終了コード 2** で終了する（常駐は systemd が 10 秒ごとに再起動を繰り返すが、journal に原因が出る。以前は Python のトレースバックだけだった）。BOM 付き UTF-8 は読める。`--version`・`--help` を除くどのモードでも（`--check`・`--status` を含む）同じで、`python3 campus_chime.py --check` で確認できる |
| 設定値が、ランタイムが動けない値（error。起動・予定・再生で例外になる、待機ループが CPU を使い続ける、存在しないタイムゾーン名、使えないログの書式、VOICEVOX ENGINE の待ち時間が大きすぎる、`audio.commands` の項目を 1 つの文字列で書いたもの（`audio.backend` が `mock` / `pygame` でないとき）など） | 起動時に、そのキー**だけ**を既定値に置き換えて続行する（異常終了させても systemd が再起動を繰り返すだけで、時報が鳴らないため）。リストの壊れた要素が原因なら、その要素だけを取り除く（`audio.commands` の項目は、その拡張子の既定の項目に置き換える）。ERROR ログ「設定の誤り: キー: 内容（直し方） [設定ファイル]」を 1 件ずつ残す。`--check` が先に NG として見つけ、直し方を示す（3.5 章） |
| 設定値が、ランタイムが許す値の誤り（warning。休館日の項目の書き方の違い・範囲外や数でない音量・範囲外の時刻・曜日の書き間違い・`"9"` のような書き方の違い・`null` にした節・緯度経度の範囲外・読み上げ文のテンプレートの置換名の誤り・`tts.engines` に `prerecorded` が無い・`weather.timeout_seconds` が大きすぎる（天気予報が流れないだけ）・再生方式が `mock` / `pygame` のときの壊れた `audio.commands` など） | **置き換えず**、書かれたとおりに動かす（前の版が動かしていた放送を変えない）。`--check` が警告として示す（3.5 章） |
| 知らない設定キー・既定値と同じ値 | 動作は変えない（知らないキーの値は無視される）。`--check` が「もしかして …?」の警告・情報として挙げる |
| 廃止した設定キー（v6.0.0。`extra_segment.mode` / `weather_probability` / `always_weather_hours` / `always_quote_hours` / `fallback_to_quote`、`weather.provider` / `jma` / `max_weather_chars`）が `config.json` に残っている | `--check` の「設定」に警告として出るほか、起動時に WARNING ログ「`<ファイル>` の `<キー>` は v6.0.0 で廃止しました（`<案内>`）。この行は無視します。消してください。」を 1 キーにつき 1 行出す。案内は、`extra_segment` の 5 キーが「天気を流す時刻は extra_segment.weather_hours で指定」、`weather` の 3 キーが「天気は Open-Meteo（weather.open_meteo）に一本化」。**値は無視して起動を続け**（既定値が使われる）、止めない。`journalctl -u campus_chime.service -p warning` や `--check` で確認できる（キーの一覧と代わりの設定は 3.4 章） |
| 天気取得失敗 | WARNING ログ。天気だけ飛ばし、ひとことは必ず流れる（ひとことへの切り替えはしない） |
| 作り置きの音声が足りない（起動時の集計） | 起動時に WARNING ログ（件数と先頭 3 件の文言）。数えられなくても起動は止めない。設定の値で文言が増えすぎる・長すぎるときは、数え上げを省いて WARNING を 1 件残す（4.12 章）。`--check`・`--status` が一覧と理由を示す |
| 起動時の TTS エンジンの状態の確認（VOICEVOX ENGINE への疎通確認）が例外になる（待ち時間の設定が大きすぎるなど） | WARNING ログ「読み上げエンジンの状態を調べられませんでした（起動は続けます）」を残し、`TTS: 確認できません` として起動を続ける（4.10 章）。設定の検査が先に error として既定値に戻す（3.5 章） |
| VOICEVOX ENGINE の代わりに HTTP ではないものが答える（別のサービスが居座っている） | 使えないエンジンとして扱う（`available()` が `False`。理由は DEBUG ログ）。合成中なら `TTSError` にして、その文だけ無音にする |
| ひとこと定義ファイル欠落・破損 | WARNING ログ。内蔵の予備文言を使用 |
| 状態ファイル破損 | WARNING ログ。初期状態として扱う |
| 放送の履歴を書けない・読めない | WARNING ログ（書けないときは放送も常駐ループも続ける。履歴は付け足し）。読むときは、壊れた行・形が違う行・知らない版の行を飛ばす（4.16 章） |
| 次の予定を求められない（スケジューラーの例外） | 例外をログに残し、60 秒後にやり直す（常駐は落とさない） |
| `--status` の外のコマンド（`timedatectl`・`systemctl`）が無い・応答しない | 「確認できません」と表示し、問題とは数えない（3 秒で打ち切る） |
| 再生中の例外 | ERROR ログ。`finally` で mixer を解放し、プロセスは継続 |
| 停止要求（`SIGTERM` / `SIGINT`）が再生中に届いた | 鳴らしている音をフェードアウトして止め（1 秒ほど）、WARNING「停止要求のため再生を中断しました（… 件を鳴らしました）」を残す。音を出し始めていた回は再生済みとして記録し（鳴り直さない）、履歴の結果は `interrupted`。音を出す前に止まった回は、何も記録しない（猶予のうちに鳴り直す）。止まらなければ、`TimeoutStopSec=30` で systemd が強制終了する（4.3・4.10・6 章） |
| 1 つの音源が鳴り終わらない（上限の秒数を超えた） | 音を止めて、ERROR「再生が終わらないため打ち切りました（上限 … 秒）」。`PlaybackError` として扱う（履歴は `error`）。上限は WAV なら長さ ＋ 10 秒、ほかは `audio.max_segment_seconds`（4.3 章） |
| 音声デバイスの初期化が失敗する | 1 秒おきに 3 回まで試し（WARNING）、3 回とも失敗すれば `PlaybackError`（ERROR「音声デバイスを初期化できませんでした」）。その回は鳴らず、履歴は `error` |
| 音量（`audio.volume.*`）が数でない・範囲外 | 数でなければ 1.0、範囲外は 0〜1 に丸めて鳴らす。キーごとにプロセスで 1 回だけ WARNING。放送は止めない。`--check` が警告する（3.5 章） |
| 休止の記録（`cache/pause.json`）が壊れている・読めない・知らない版・期限切れ | 止めていないものとして**鳴らす**。壊れているときだけ WARNING「休止の記録を使えません（放送は通常どおり行います）」（同じ問題は 1 度）。`--check` の「書き込み」が警告し、`--pause-off` で消せる（4.21 章） |
| 休止の記録を確かめる処理が例外になる | 止めていないものとして鳴らす。WARNING「休止の記録を確かめられませんでした（放送は通常どおり行います）」（4.10 章） |
| 休止の記録を書けない・消せない（`--pause-*`） | 原因を表示して終了コード 1。常駐には影響しない |
| 休館日の設定（`schedule.closures`）の項目の書き方が正しくない | ERROR「休館日の設定に使えない項目があります」を項目ごとに残し、**その項目だけ**使わない（その日はいつもどおり鳴る）。`--check` が警告する（4.20 章） |
| 鳴らす予定が 1 つも無い（休館日・休止・曜日で先まで空など） | WARNING「予定されたイベントがありません。60 分ごとに再確認します」を、予定が途切れて最初の 1 度だけ残し、60 分ごとに再確認する。次の予定が見つかれば、また警告できる（4.10 章） |
| イベント処理中の想定外例外 | ERROR ログ（スタックトレース付き）。当該回を再生済みとして記録し常駐継続（そのあとで履歴に `error` を残す） |
| プロセス異常終了 | systemd が 10 秒後に再起動。`state.json` により二重再生しない |

> **設計方針:** 失敗によってプロセス全体を落とさない。翌日以降の放送を継続できることを最優先する。

---

## 9. ログ仕様

形式: `%(asctime)s - %(levelname)s - %(name)s - %(message)s`（タイムゾーンは設定に追従）

| レベル | 出力タイミング |
|---|---|
| INFO | 起動（版とコミット、環境情報、再生バックエンドと TTS エンジン、設定の出どころ、作り置きの音声の件数、効いている休止の記録）、次回予定（休止中は、休止が明けたあとの予定）、イベント準備、休止の記録の書き換えに気づいたこと（「休止の記録が変わりました」）、休止のために鳴らさなかった回（「休止中のため鳴らしません」）、再生開始、再生内容（読み上げ文言と、無音になった文言を含む）、再生完了、停止 |
| WARNING | 追いかけ再生、天気取得失敗、任意音源の欠落、予定なし（最初の 1 度だけ）、停止要求による再生の中断、音声デバイスの初期化の失敗（やり直す）、使えない休止の記録、作り置きの音声が足りない（数え上げを省いたときを含む）、読み上げエンジンの状態を調べられない（起動は続ける）、放送の履歴を書けない、廃止したキー・既定値の丸ごとコピー |
| ERROR | 依存欠落、必須音源の欠落、音声合成失敗、再生例外（再生の打ち切り・音声デバイスの初期化の失敗を含む）、設定の誤り（既定値に置き換えて続行）、使えない休館日の項目（その項目だけ無視して続行） |

起動時の最初の数行は次の順に出る（`ChimeApp.log_environment()`）。何が動いているのかを、journal から読み取れるようにするため。

```
campus-chime 6.1.0 (a1b2c3d)
実行環境: Linux 6.1.0-rpi (armv7l) / Python 3.11.2 / WSL=False
再生バックエンド: pygame / TTS: prerecorded(利用可), voicevox(利用不可)
設定ソース: <defaults> → /home/pi/campus-chime/config.json
作り置きの音声: 138/138 件
```

systemd 配下で動かしているときは、ログの各行に重大度の接頭辞が付き、`journalctl -p` で重大度による絞り込みができる（v5.3.0 から。それ以前は全行が同じ重大度で、`-p err` は常に空だった）。

参照方法:

```bash
journalctl -u campus_chime.service -f               # 追尾
journalctl -u campus_chime.service --since today    # 本日分
journalctl -u campus_chime.service -p err           # エラーのみ（v5.3.0 から有効。それ以前は常に空だった）
journalctl -u campus_chime.service -p warning       # 警告も見るなら（警告以上）
```

---

## 10. テスト仕様

### 10.1 自動テスト

```bash
python3 -m unittest discover -s tests -t . -v
```

ネットワーク・音声デバイス・外部コマンドに依存せず実行できる（Open-Meteo の応答は `tests/fixtures/` の実データ形式で再現）。開発者の手元に置いた `config.json`（Git 管理外）をテストが読んで結果が変わらないよう、リポジトリ直下の `config.json` は「無い」ことにする（`tests/support.py` の `isolate_local_config()`。`chime.config.local_config_path()` を差し替える）。

| ファイル | 対象 |
|---|---|
| `tests/test_config.py` | 設定のマージ・解決、`config.example.json` との同期、廃止した設定キーの警告と無視、設定の等価比較（`Config.__eq__`）、現地設定の場所（`local_config_path()`）とテストでの差し替え |
| `tests/test_configcheck.py` | 設定の検査（3.5 章）。知らないキー・廃止したキー・既定値と同じ値、**error と warning の境目**（実際の `ChimeApp`・`Scheduler`・再生・ログに値を通して、ランタイムが壊れる値だけが error になること）、時・分・曜日・休みの時刻の範囲、数値（読めない・範囲外・日時の範囲を出る・巨大な値。通信の待ち時間がソケットの上限を超えたとき、VOICEVOX ENGINE の分は error で、天気予報の分［放送を組み立てる側が受け止める］は warning）、外部コマンドの表 `audio.commands` の重大度（再生方式が `command` / `auto` なら error、表を読まない `mock` / `pygame` なら warning で置き換えない）、タイムゾーン、ログの水準と書式（実際に 1 件を整形できること）、再生バックエンドと読み上げエンジンの名前、地点の緯度経度、テンプレートの置換名と書き方、作り置きの範囲、`check_config()` の並び順と出どころ、`sanitized()` が error のキーだけを既定値に戻し、リストは壊れた要素だけを取り除くこと、warning の値は置き換えないこと、検査が 1 項目ずつ独立していること、`chime/configcheck.py` が再生系を import しないこと。v6.2.0 の設定（休館日 `schedule.closures` の使えない項目が警告になり、置き換えられないこと［項目の説明はランタイムの `ClosureCalendar.problems` と同じ］、音量 `audio.volume.*` が数でない・範囲外のとき警告で、ランタイムが 1.0 にする／丸めるのと一致すること、`audio.max_segment_seconds`、`state.pause_file`） |
| `tests/test_timesignal.py` | 読み上げ文言、WAV の形式・長さ・長音の開始位置、設定が欠けた（空の節・`null`・キーなし）ときの既定の文言への補完 |
| `tests/test_scheduler.py` | 曜日・時間帯の展開、追いかけ再生、待機処理。休館日で `events_for_date` が空になること、既定の設定では予定が v6.1.0 と同じであること、`skip_reason`（元から鳴らない日は挙げない）、`format_schedule`（休館日のない設定では `format_events` と同じ文字列）、`sleep_until` の `interrupt`（真で待ちをやめる・例外でも待ちを続ける・正時直前の 0.25 秒は呼ばない）、400 日先までの探索が速いこと |
| `tests/test_closures.py` | 休館日の暦（4.20 章）。`dates`（文字・名前付き・うるう日）と `ranges`（両端を含む・1 日だけ・全期間）の読み方、最初に当たった項目の理由、書き方の正しくない項目だけが無視されて説明（`problems`）と ERROR ログが 1 件ずつ残り、ほかの項目は使われること、節や一覧の形が違うとき、どんな JSON の値を渡しても例外にならないこと、長い一覧でも引けること |
| `tests/test_weather.py` | Open-Meteo の解析、文の組み立て、語彙の網羅、異常応答 |
| `tests/test_quotes.py` | 候補の抽出、直近除外、同梱データの健全性 |
| `tests/test_tts.py` | エンジンのフォールバック、キャッシュ、事前生成音声の参照、VOICEVOX が HTTP ではないもので答えたときの扱い、文言の整え方（`normalize_phrase()`） |
| `tests/test_state.py` | 永続化、日付をまたぐリセット、破損時の挙動 |
| `tests/test_security_check.py` | セキュリティの点検（`scripts/security_check.sh`）。偽のシステム（`CHIME_SYSROOT`）と、読むだけの道具と偽のコマンドだけの PATH で本物のスクリプトを動かし、項目ごとのよい状態と悪い状態、IPv4（プライベート・CGNAT・リンクローカル・グローバル）と IPv6 の見分け、終了コード、root と一般ユーザー、**何も書かないこと**（前後のファイル一覧と、呼んだコマンドの許可リスト） |
| `tests/test_harden_script.py` | セキュリティの強化（`scripts/harden.sh`）。偽のシステムと偽のコマンドで本物のスクリプトを動かし、手順ごとの事前確認での中止（何も変えない）、書く中身、二度目の実行、元に戻したときに元のファイルが内容と権限まで戻ること、`--dry-run` が何も書かないこと、確かめられなかったときの自動の取り消し、自動の取り消しのタイマーと `--confirm`・`--rollback-pending`、ロック。生成した防火壁の規則は、使えるときは本物の `nft -c -f` で確かめる |
| `tests/test_pause.py` | 「今日だけ止める」の記録（4.21 章）。書いて読む、`set_at`、上限（31 日）と過去の日付の拒否、期限切れ、使えない記録（無い・壊れた・知らない版・型違い・深すぎる入れ子・フォルダ）が「止めていない」になり、警告は同じ問題につき 1 度だけであること、時計の失敗、`clear()`、`changed()`（作られた・消えた・置き換わった） |
| `tests/test_sequence.py` | セグメント構成（天気を流す時刻・ひとことは毎回）、失敗時の縮退、音にならなかった文言（`plan.silent`）の記録と表示。セグメントの音量の区分（`kind`: 時報音・読み上げ・閉館アナウンス・蛍の光）、読み上げる文言が増えていないこと（作り置きに全部あること） |
| `tests/test_audio.py` | 再生順序、デバイス解放、バックエンド選択、pygame のバナーを出さないこと（`PYGAME_HIDE_SUPPORT_PROMPT`）。音量（`volume_for()` が数でない・範囲外でも例外を出さず、キーごとに 1 度だけ警告すること）、打ち切りの秒数（WAV は長さ ＋ 10 秒、ほかは `audio.max_segment_seconds`）、停止要求（`PlaybackInterrupted`。再生の前から立っている・再生中・間隔の待ち・pygame のフェードアウト・外部コマンドの `terminate` から `kill`・mock）、pygame の初期化のやり直し（3 回）、外部コマンドの起動と終了（偽の mixer・偽の時計・偽のプロセス。本物の音も子プロセスも使わない） |
| `tests/test_app.py` | 常駐ループ、停止要求、例外時の継続、予定を求められないときの再試行（暴走しても固まらずに落ちること）、再生の結果（`PlayOutcome`）、`str()` が失敗する例外でも再生・常駐が止まらないこと、放送の履歴の追記（書けなくても放送を続けること、`--dry-run` では残さないこと、音源ファイルが無くて積めなかった部品が `partial` になること、再生済みの記録が履歴より先に付くこと）、起動時のログ（版・作り置きの件数と警告・数え上げを省くとき）。休止（次のイベントの選び方・待機中の書き換えで選び直すこと・組み立ての直前と再生の直前に確かめること・履歴の `skipped`・止めても再生済みにしないこと・壊れた記録や確かめる処理の例外で鳴ること・正時直前に確かめないこと）、停止要求で再生を止めた回（`interrupted`・再生済みの記録・ログ）、鳴らす予定が無いときの待ち（警告は最初の 1 度だけ）、起動時の休止のログ、起動時の「次回以降の予定」が、止める日を選ぶときと同じ（`Event.at` の日付）で決めること |
| `tests/test_cli.py` | 引数解釈、終了コード、環境判定、`--version`・`--check`・`--status`・`--wait-idle` の呼び分けと終了コード、危ない値の置き換えとそのログ、`--print-config` が書かれたとおりを見せること、点検と状態の表示が何も書かないこと。休止のオプション（`--pause-today` / `--pause-until` / `--pause-off` / `--reason`: 書く記録、表示する回数と再開の日時、過去・長すぎる日付と組み合わせの誤りを設定を読む前に断ること、root では断ること、`--dry-run` では書かないこと、書けない・消せないときの終了コード 1、`--schedule` の休止の表示、`--pause-off` が追いかけて鳴る回を「確実な回」と「間に合えば」の回に分けること（境目は猶予 − 待機の長さで、`Scheduler` の実際の選び方と一致すること。件数の上限、日付をまたぐ回）） |
| `tests/test_jsonfile.py` | JSON の読み書き（BOM 付き UTF-8、UTF-8 以外の案内、構文エラーの行・桁とヒント、一時ファイル経由の書き込み） |
| `tests/test_logsetup.py` | journal への重大度の接頭辞（`JOURNAL_STREAM` が一致するときだけ全行に付く）、ハンドラの重複防止 |
| `tests/test_buildinfo.py` | 版とコミットの表示（`.git` を直接読む）。ブランチ・detached HEAD・`packed-refs`・`gitdir:` と書いたファイル（worktree）、調べられないとき（`.git` が無い・壊れている・読めない）に例外ではなく `unknown` になること、`git` コマンドを呼ばないこと、このリポジトリで `git rev-parse` の答えと一致すること |
| `tests/test_check.py` | 設置状態の点検（`--check`、4.14 章）。すべて正常な設置場所（一時フォルダ）で `OK` になること、設定・作り置きの音声（空・壊れた・途中で切れた声は「無い」として数える）・音源（WAV は最後まで読めること、MP3 は先頭・大きさ・フレームをファイルの終わりまでたどること。途中で切れた・途中に壊れた所がある・ID3 タグのあとにフレームが無い（タグまでしか書けていない）ものは NG で、末尾の ID3v1・APEv2・小さな 0 埋めと、重なった ID3v2 タグは通すこと）・書き込みの各節の NG・警告・情報の分け方と直し方（まだ無い場所には `mkdir -p` と再帰なしの `chown`、OS の場所は持ち主を変えない、自分の所有なら `chmod`）、**NG にするのは再生済みの記録が残せなくなるものだけ**で履歴・TTS キャッシュは警告、root 所有のファイルは置き換えられるので挙げないこと（所有者・書き込み可否・実行している利用者は差し替える）、終了コード、**何も書かない**こと（ファイルの内容・時刻・フォルダの一覧が変わらない）、全角を含む桁のそろえ。休館日の使えない項目の警告、休止の記録の行（止めていれば情報、フォルダ・読めない・中身が使えないは警告で NG にしない、保存先が空なら出さない）、点検中に休止の記録のログを出さないこと |
| `tests/test_history.py` | 放送の履歴（4.16 章）。1 件の作り方と `result` の判定（音源ファイルが無くて積めなかった部品 `missing` があれば `partial`）、追記（途中で切れた行の区切り・書けないときの `False` と警告・深すぎる入れ子など書けない値）、行数の整理（一時ファイル経由で、失敗しても履歴を失わない）、読み出し（新しい順・壊れた行や知らない版の行を飛ばす・`missing` の無い古い行も読む・読めなくても例外にしない）。`skipped` / `interrupted` の結果と `reason`（知らない値は `ValueError`。これらの項目の無い古い行も読めること） |
| `tests/test_status.py` | 状態の表示と放送の最中の待機（4.15 章）。外のコマンド（`timedatectl`・`systemctl`）は偽物に差し替えて、答え・失敗・時間切れ・出力が読めないときの「確認できません」、気になる点の判定と終了コード（直近の放送が失敗、または音源ファイルが無くて積めなかった部品があるとき）、直近の放送の 1 行（欠けた音源・無音だった文言・範囲外の時刻でも例外にしない）、**何も書かない**こと、放送の時間帯の判定（日付をまたぐ場合を含む）、`wait_idle` の待機と上限（偽の時計で、実際には眠らない）。「放送の休止」の行、休止中の次の予定の数え方、近い休館日の節（`CLOSURE_DAYS`）、「止めていた」「中断」の行、休止で鳴らさなかった記録を「直近の放送」の判定で飛ばすこと、停止要求で止まった放送が気になる点になること、休止の日の放送が待機に入らずに止まること（偽の `sleep_until` が呼ばれたら失敗にする。時計が止まっているので、本物の待機だと戻らない） |
| `tests/test_update_script.py` | 更新スクリプト（`scripts/update.sh`）。本物の `bash` と `git` で、一時フォルダに作った使い捨ての `origin.git` と clone を更新する（通信はしない。`python3`・`id`・`sudo`・`systemctl`・`setup.sh` は呼ばれた記録だけ残す偽物）。`--help`、止まる条件（root・手元の変更・`git` が状態を読めない・detached HEAD・取り込みの失敗）では**何も変えずに**理由を表示して終了すること、取り込みの失敗の原因の見分け（ネットワーク・権限・ディスク［空き容量の不足・読み取り専用・入出力エラー・容量の割り当て超過。権限の語句と一緒に出てもディスク］・Git 管理外のファイルの上書き）と、権限とディスクのときは「何も変更していません」と言わず、次の実行で「手元の変更」として案内が出ると伝えること、手元の変更の案内（`git restore --source=HEAD --staged --worktree -- <ファイル>`）を**実際に実行すると変更が消える**こと（`git add` 済みの変更・削除・追加・名前の変更・変わった名前のファイルでも）、`config.json`（Git 管理外）は止める理由にならないこと、呼ぶ順番（`git pull` → `--wait-idle` → `setup.sh --no-apt` → `--status`）、すでに最新でも `cache/deployed_commit` がいまの版と合わなければ続きを反映すること（`--wait-idle` の失敗・`setup.sh` の失敗のあと、もう一度実行して再起動に至ること）、`--wait-idle` の終了コード 1（放送が終わらない）と 2（`config.json` を読めない）を言い分けること、元の版への戻し方は表示するだけで `git reset` などを実行しないこと |
| `tests/test_phrases.py` | 全文言の列挙（既定設定・現地設定での件数とハッシュの固定、`--config` の文言と既定の文言の和集合、`--prune` で残す文言、時報・閉館・ひとこと各列挙の振る舞い、前後の空白の整え方が実行時の照合と同じこと、`coverage()` の集計（種類ごとの件数・声の無い文言・同梱の作り置きで全件そろうこと）、`chime/phrases.py` が再生系を import しないこと） |
| `tests/test_generate_voicevox.py` | 作り置き生成スクリプトの CLI 側（`--config` の文言を既定の文言に足して生成すること、`--prune` の実行、合成に失敗した文言を manifest に書かないこと、列挙の関数を `chime/phrases.py` から再公開していること、前後に空白のある文言を実行時の引き方と同じキーで作ること、WAV を一時ファイルへ書いてから置き換えること（中断・失敗しても欠けた WAV を残さず、`--force` では元の WAV を残す）） |
| `tests/test_setup_script.py` | 導入スクリプト（`scripts/setup.sh`）。`--help` / `-h` が先頭のコメントブロックだけを表示すること（`set -euo pipefail` まで出さない）、不明なオプションが副作用（apt・systemd など）の前に終了コード 2 で止まること、新規に作る `config.json` の雛形が正しい JSON で、設定項目を持たず、既定値と同じ値を 1 つも写さない（起動時に丸ごとコピーの警告が出ない）こと、設置状態の点検（`--check`）の段（時報音の生成のあと・サービスの再起動の前に走ること、NG でも先へ進み「直し方」を案内すること、`config.json` を読めないとき（終了コード 2）だけ予定表の表示とサービスの再起動を省き、「導入は途中です」と表示して終了コード 1 で終わること、`--generate-assets` が 2 のときは PC で作り直す案内を出さないこと、`--no-service` でも点検は走ること）、サービスの再起動に成功したときだけ `cache/deployed_commit` にコミットを書くこと（再起動を控えたとき・失敗したとき・`--no-service`・root で実行したときは書かない）。副作用のあるコマンドは偽物に差し替えて実行する |
| `tests/test_ci_workflow.py` | CI の定義（`.github/workflows/ci.yml`）。`test` ジョブの Python の版に下限の 3.9 と実機の 3.11 が入っていること、`prerecorded-only` ジョブに `--check` の段があること、`scripts/*.sh` の**どれも** `bash -n` で **1 本ずつ**構文を調べ（`bash -n` は複数のファイルを渡しても 1 つ目しか調べないため）、`shellcheck` にもかけること。YAML の解析器は標準ライブラリに無いので、このファイルの書き方だけを読む最小の解析を使い、書かれたコマンドを**実際に動かして**確かめる（`scripts/` の写しの 1 本をわざと壊して、段が失敗すること）。`.github/workflows/` のどのワークフローも、`actions/checkout` と `actions/setup-python` を Node.js 24 で動く主版（v5・v6）以上で使っていること |
| `tests/test_tag_releases.py` | 版のタグ付け（`scripts/tag_releases.py` と `.github/workflows/tag.yml`）。使い捨ての git リポジトリで、first-parent 上で `__version__` がその版になった最初のコミット（版を出した PR のマージコミット）に版のタグが付くこと、`CHANGELOG.md` に `## [X.Y.Z]` の見出しが無い版には付けないこと、`--since` より古い版には付けないこと、タグが注釈付きであること、何度実行しても結果が変わらず既存のタグを動かさないこと（別のコミットを指す既存のタグは警告だけ）、GitHub Actions 上では警告が注釈として出ること、`--dry-run` が何も作らないこと、`--push` が bare リポジトリのリモートへ届くことと、`git push --porcelain` の結果の行をタグごとに読み取ること（送れた・送り先に既にあった・断られた）、GitHub が `workflows` 権限を理由に断ったタグは警告（人が手元で作って送るコマンドつき）にとどめて終了コード 0 にすること（OAuth の `workflow` スコープの断りなど、ほかの断り方や結果の読み取れない失敗は、これまでどおり終了コード 1 になり手で push するコマンドが表示されること）、タグを 1 つ作れなくても残りは作って push し終了コード 1 になること、git が読めないオブジェクトがあると終了コード 1 でタグを 1 つも付けないこと、付けられなかった版（見出しが無い・先頭の版が読めない）が警告になること、浅い clone・git のリポジトリでない場所・不正な `--since` が終了コード 2 になること、`tag.yml` の静的な検査（トリガー、ジョブ単位の `main` 限定の条件、ワークフロー全体の `permissions` が 1 つだけであること、認証情報や権限を上書きする設定が無いこと、`fetch-depth: 0`、ref ごとの `concurrency` グループ）。`tag.yml` と `ci.yml` が同じ Actions を同じ主版で使うこと |
| `tests/test_voice_assets.py` | 同梱の音声の健全性（`assets/voice/` の manifest と WAV の欠け・余り・形式、既定の設定が読み上げる全文言が manifest にあること、`announce.wav` と `hotaru.mp3`。VOICEVOX は使わずファイルだけを調べる） |
| `tests/test_docs.py` | 文書の記載と実装の一致（文書中の `--say` の例が作り置きにあること、README・要求定義書・仕様書の版が `chime/__init__.py` の `__version__` と一致すること、`CHANGELOG.md` の先頭の版が `__version__` と一致すること、文書に書いた作り置きの件数・天気コードの語数・待機の秒数・履歴の件数・点検の上限などがコードから数えた値と一致すること、廃止したキーが「廃止」の語なしに現行の設定として書かれていないこと、すべてのコマンドラインオプション・`chime/` の各モジュール・`scripts/` の各スクリプト・各テストファイルが文書に載っていること、文書に書いたコマンドのオプションが実在すること、文書が指す節（`KNOWLEDGE_BASE.md 3-6` など）が実在すること、どの表の行も見出しと同じ数のセルを持つこと、`--check`・`--status` の出力例の見出しと項目名が実装と一致すること、「直近の放送」の例が**実装に放送させて記録した行**と一致すること（手で書いた辞書ではない）、KNOWLEDGE_BASE の読み方の引用が実装に実在する文面であること、仕様書 3.5 章の「置き換える（error）」キーの一覧が `validate()` に値を入れて調べた結果と過不足なく一致し、警告にとどめる例が本当に置き換えられないこと、`--check` の直し方（`mkdir -p` と再帰なしの `chown`・`chown -R`・OS の場所）が実装の出力と同じであること、`--check`・`--status` で置き換えた手作業の確認（`ls … \| wc -l`・`grep -c`・`python3 -c`）が文書に戻っていないこと、更新の手順（v6.1.0 より前から初めて更新する手順が `--wait-idle` を使わないこと、`update.sh` が止まったときの手順は放送の時間帯を待ってから導入スクリプトを実行すること、止まる理由・手元の変更の戻し方・`cache/deployed_commit` が `update.sh` と合っていること、止まる理由の表の行数が前後の「上の N つ」「下の N つ」と合うこと、取り込みの失敗の原因（ネットワーク・権限・ディスク・Git 管理外のファイルの上書き）が表とスクリプトで同じ語で分かれていること、権限とディスクで失敗した取り込みを「何も変更していません」と言わないこと、README・SETUP・KNOWLEDGE_BASE・仕様書・要求定義書が同じ原因を挙げること、元の版へ戻す手順が「コードを更新した回だけ」と書かれていること）、`--check` の MP3 の確かめ方（途中で切れた蛍の光と、ID3 タグまでしか書けていない蛍の光を NG にし、末尾の ID3v1・0 埋めと、タグの直後から探す範囲の内の余分なデータ、重なった ID3v2 タグは通す。範囲と数は定数と一致すること）・sticky ビットの扱い・`tts.engines` から `prerecorded` を外したときの警告と NG・簡易の内容で鳴った放送の表示・`audio.commands` を 1 つの文字列で書いたときの置き換え（再生方式が `command` / `auto` のときだけ。`mock` / `pygame` では警告にとどめて置き換えない）・天気予報の待ち時間が大きすぎるときの警告・時刻の読みの表が辞書でないときの扱い・時報の時刻を調べる範囲の境目・VOICEVOX ENGINE の待ち時間の上限（天気予報の待ち時間は同じ上限で警告）・作り置きの数え上げの上限が、文書のとおりに動くこと、どのソース（`chime/`・`scripts/`・`tests/`）もコンパイルで警告（無効なエスケープなど）を出さないこと、CI の `bash -n` の説明が ci.yml の書き方（1 本ずつ）と合っていること、`--print-config` が廃止したキーを表示しないという説明、「最大約 6 分」が `update.sh` の待機の上限であること） |

CI（`.github/workflows/ci.yml`）で Python 3.9 / 3.11 / 3.13 に対して自動実行する（`test` ジョブ。「CLI が起動すること」の `--test-hourly 12` は、実際の通信をしないよう `--config tests/fixtures/offline_config.json`（天気を無効にした設定）を渡す）。別ジョブ（`lint`。Python 3.11 のみ）で `pyflakes`（版を固定してインストールする）を `python -m pyflakes chime scripts tests campus_chime.py` で実行する。別ジョブ（`shell`）で、`scripts/*.sh` のすべてについて、構文（`bash -n`）と `shellcheck` を確かめる。`bash -n` は複数のファイルを渡しても 1 つ目しか調べない（2 つ目以降は引数として渡されるだけ）ので、`for f in scripts/*.sh` で **1 本ずつ**実行する（`scripts/` に増えたスクリプトも自動で対象になる。`shellcheck` は複数のファイルをまとめて受ける）。ワークフロー全体の権限は `contents: read` だけで、同じ ref の古い実行は新しい実行が始まると止める（`concurrency`）。この環境には音声合成エンジンを一切導入しないため、「合成エンジンが一つも使えない」状態がそのまま再現される。別ジョブ（`prerecorded-only`）で、時報の定型文・ひとこと・天気予報の全文言（`chime/phrases.py` の `collect_phrases()` が列挙する語彙）を `--say ... --dry-run` で 1 件ずつ流し、無音になったことを示す警告（`を合成できませんでした`）が 1 件も出ないことを確認する。これにより、作り置き（`assets/voice/`）だけで全文言を賄えていることを回帰的に検証する（v4.x まではここで `open_jtalk` を導入し実際の音声合成を検証していたが、v5.0.0 でそのエンジンをコードごと削除したため不要になった）。同じ `prerecorded-only` ジョブの最後の手順で `python campus_chime.py --check` を実行し、設置状態の点検が、リポジトリに入っているものだけで通ることを確かめる。このジョブには `config.json` も時報音（`assets/generated/`。Git 管理外）も無いが、時報音が無いのは警告にとどまる（放送のときに自動で作る）ので終了コードは `0` のまま。設定の誤り・作り置きの欠け・同梱の音源の破損・書き込めない場所があれば NG になり、ジョブが赤くなる。

手元で `prerecorded-only` ジョブと同じ検証をするには、`.github/workflows/ci.yml` のコマンドをそのまま実行する（`cache/tts/` を消してから実行すると、キャッシュ済みの合成結果に隠れず確実に検証できる）。

版のタグは、別のワークフロー（`.github/workflows/tag.yml`、名前は「タグ付け」）が付ける。`main` への push のたびに（手動実行の `workflow_dispatch` も `main` だけ）、`github-actions[bot]` として `python scripts/tag_releases.py --since 5.2.0 --push` を実行する。タグを push するため、権限に `contents: write` を持つのはこのワークフローだけで、`ci.yml` は `contents: read` のままにしてある。履歴をたどるので、取得は `fetch-depth: 0`（全履歴）にする（浅い clone ではスクリプトが終了コード 2 で止まる）。スクリプトは `main` を first-parent でたどり、`chime/__init__.py` の `__version__` がその版になった**最初のコミット**（版を出した PR のマージコミット）に、`vX.Y.Z` の注釈付きタグ（メッセージはタグ名）を付ける。ただし `CHANGELOG.md` にその版の見出し（`## [X.Y.Z]`）が無いコミットには付けない。既存のタグは動かさず、消しもしない（別のコミットを指すタグがあっても警告するだけ）ので、何度実行しても結果は変わらない。タグを付けるのは 5.2.0 以降の版だけで、この下限はスクリプトに埋め込まず、ワークフローが `--since 5.2.0` で渡す。実行は `concurrency`（グループ `tag-${{ github.ref }}`、つまり ref ごと）で 1 つずつ順に行い、実行中のものを取り消さない。`main` への push と `main` での手動実行は同じグループに並ぶので順に実行され、別のブランチでの手動実行（`main` ではないのでタグは付けずに終わる）が、待っている `main` の実行を押しのけることはない。PR は「Create a merge commit」（または「Squash and merge」）でマージする。「Rebase and merge」やファストフォワードでは、マージコミットができず、PR の版を上げたコミットがそのまま `main` の first-parent に載るため、タグがそのコミットに付いてしまう。

push は `git push --porcelain` で 1 回にまとめて行い、タグごとの結果（送れた・送り先に既にあった・断られた）を読み取って報告する。ただし **`v5.2.0` と `v5.3.0` は、Actions からは付けられない。** GitHub は、`.github/workflows` の中身が既定ブランチと違う古いコミットを指すタグの作成を、Actions のトークン（`GITHUB_TOKEN`。`workflows` 権限を付けられない）では断る（返答は `refusing to allow a GitHub App to create or update workflow …` で始まり、`workflows` 権限が無いことを理由に挙げる）ためである。この断り方は GitHub の仕様で、実行の失敗ではないので、タグごとの警告（Actions の画面では注釈。手元で作って送るコマンドつき）で知らせ、実行は緑のまま（終了コード `0`）終わる。この 2 つは、**人が自分のマシンから 1 回だけ**作って送る。

```bash
git fetch origin && git tag -a v5.2.0 <警告に書かれたコミットの sha> -m v5.2.0 && git push origin v5.2.0
```

送ったあとの実行では、すでにあるタグとして扱われ、警告も出ない。以降の版は、マージ直後の `main` の先頭のコミット（版を上げた PR のマージコミット）に付くため、自動で付く（同じ警告が出たら、同じ手順で人が付ける）。それ以外の断り方（フックによる拒否、認証の失敗など）や、タグごとの結果が得られない失敗（送り先が無い、認証など）は、これまでどおり失敗（終了コード `1`）として報告する。

付けられなかった版（`__version__` がその版なのに `CHANGELOG.md` に `## [X.Y.Z]` の見出しが無い場合と、ref の先頭で `__version__` を読み取れない場合）は、警告として報告する（Actions の画面では注釈になる）。実行は緑のまま終わる。1 つのタグの作成に失敗しても、残りのタグは作って push し、実行は赤（終了コード 1）で終わる。git が履歴やオブジェクトを読めない（clone が壊れている・途中までしかない）ときは、何もタグを付けず、実行は赤で終わる。

### 10.2 実機での確認

| # | 項目 | 手順 | 期待結果 |
|---|---|---|---|
| T-01 | Mock 動作 | 開発機で `python3 campus_chime.py --test-all` | 音を出さずログのみ出力 |
| T-02 | 時報の即時再生 | Pi 上で `python3 campus_chime.py --test-hourly` | ポ・ポ・ポ・ポーン → 時刻 → おまけ が順に鳴る |
| T-03 | 閉館放送の即時再生 | Pi 上で `python3 campus_chime.py --test` | アナウンス → 蛍の光（フェードイン） |
| T-04 | 音声品質 | T-02・T-03 実施時に聴取 | 音飛び・カクつきがない（NFR-01） |
| T-05 | 時報の時刻精度 | 電波時計等と聴き比べ | 長音の開始が正時と一致（±100ms、NFR-04） |
| T-06 | サービス起動 | `python3 campus_chime.py --status` | 「サービス」が「動作中（active）」で、「自動起動」が「有効」、末尾が「気になる点は見つかりませんでした。」 |
| T-07 | 時刻トリガー | `config.json` で `hourly.start_hour`/`end_hour` を直近の時刻に変更して待機 | 定刻に自動再生される |
| T-08 | 二重再生防止 | T-07 の再生直後に `sudo systemctl restart` | 再生が繰り返されない |
| T-09 | 追いかけ再生 | 正時の 30 秒後に `sudo systemctl restart` | 遅れて再生され、WARNING が記録される |
| T-10 | 自動復旧 | Pi を再起動 | 起動後にサービスが自動的に active になる |
| T-11 | 待機負荷 | `top` で当該プロセスを確認 | CPU 使用率 1% 未満（NFR-02） |
| T-12 | オフライン動作 | Wi-Fi を切って `--test-hourly` | 時報は鳴り、おまけはひとことになる |
| T-13 | 天気取得 | `python3 campus_chime.py --weather` | 現在の天気と気温の文が表示・読み上げされる |
| T-14 | 設置状態の点検 | Pi 上で `python3 campus_chime.py --check` | 末尾が「結果: すべて OK です。」（終了コード 0）。何も書かれない |
| T-15 | 直近の放送の記録 | 実際の正時の放送のあとに `python3 campus_chime.py --status` | 「直近の放送」にその回が「成功」で並ぶ |
| T-16 | 更新 | 放送のない時間に `bash scripts/update.sh` | 取り込み・点検・再起動のあと、旧い版 → 新しい版と `--status` が表示される。手元に変更があるときは何も変えずに止まる |
| T-17 | 放送中の待機 | 閉館放送（16:57）の時間帯に `python3 campus_chime.py --wait-idle` | 放送の時間帯が終わるまで待ち、終わったら終了コード 0（360 秒で終わらなければ 1） |
| T-18 | 今日だけ止める | 放送のない時間に `python3 campus_chime.py --pause-today --reason 試験`（再起動はしない）。次の正時を待つ | 止める回数と再開の日時が表示される。`--status` の「放送の休止」が「止めています」になり、次の正時は鳴らない（先に止めたので、`--status` の「直近の放送」に行は付かない。正時の直前（再生開始の 45 秒前から）に止めたときだけ、「止めていた」の行が付く）。`--pause-off` で戻すと、次の正時から鳴る |
| T-19 | 休館日 | `config.json` の `schedule.closures.dates` に、直近の平日を書いて再起動し、`python3 campus_chime.py --schedule` | その日が「休館 …」の行になり、時報も閉館放送も鳴らない。確かめたら `config.json` を元に戻す |
| T-20 | 音量 | `config.json` の `audio.volume.voice` を 0.3 にして再起動し、`python3 campus_chime.py --test-hourly` | 時報音は元のまま、読み上げだけが小さくなる。確かめたら元に戻す |
| T-21 | 再生中の停止 | 放送のない時間に、サービスを止め（`sudo systemctl stop campus_chime.service`）、閉館放送の時刻を直近の分にした設定（`state.file` も本番とは別の場所にする）を `--config` に渡して、端末で常駐させる（`python3 campus_chime.py --config 試験用.json`）。蛍の光が鳴っているあいだに Ctrl-C を押す。終わったら `sudo systemctl start campus_chime.service` | 音が 1 秒ほどで止まる（蛍の光が鳴り終わるまで待たされない）。ログに WARNING「停止要求のため再生を中断しました」が出て、プロセスが終了する |

> T-07・T-19・T-20 実施後は `config.json` を元に戻すこと。T-18 実施後は `--pause-off` で戻すこと。

---

## 11. 運用・保守

| 操作 | コマンド |
|---|---|
| 状態確認 | `python3 campus_chime.py --status`（systemd 側の詳細は `sudo systemctl status campus_chime.service`） |
| 設置状態の点検 | `python3 campus_chime.py --check` |
| 版の確認 | `python3 campus_chime.py --version` |
| ログ追尾 | `journalctl -u campus_chime.service -f` |
| 予定確認 | `python3 campus_chime.py --schedule` |
| 更新反映 | `cd /home/pi/campus-chime && bash scripts/update.sh`（取り込み・放送の時間帯の待機・点検・再起動・結果の表示。手順は SETUP.md 9 章 A） |
| 設定変更 | `nano config.json` → `python3 campus_chime.py --check` → `sudo systemctl restart campus_chime.service` |
| 放送を止める（休業日・行事・点検） | `python3 campus_chime.py --pause-today [--reason 理由]`（今日だけ）・`--pause-until YYYY-MM-DD`（その日まで。最長 31 日）。戻すのは `--pause-off`。サービスは止めず、再起動も要らない（長い休みは設定の `schedule.closures`。**祝日は自動では休まない**）。pi ユーザーで実行する |
| サービスの停止（保守） | `python3 campus_chime.py --wait-idle && sudo systemctl stop campus_chime.service`（再生中の音も 1 秒ほどで止まるが、その回の放送は途中で終わる） |
| 自動起動解除 | `sudo systemctl disable campus_chime.service` |
| 時報音の再生成（時報音の設定を変えたとき） | `python3 campus_chime.py --generate-assets` |

> **v6.1.0 より前の版から初めて更新するとき**は、手元に `update.sh` も `--wait-idle` も無い。`git pull` のあと、16:55〜17:02 を避けて `bash scripts/setup.sh --no-apt` を実行する（SETUP.md 9 章 A）。次の更新からは `bash scripts/update.sh` で行える。

> **運用上の注意:** **16:55〜17:02（閉館放送の前後）は、更新・再起動・停止をしない。** 停止要求には、待機中も再生中もすぐ応じる（再生中の音は 1 秒ほどで止まる。4.10 章）が、そうして止めた回の放送は途中で終わる（再生済みとして記録するので、鳴り直さない。履歴は `interrupted`）。`update.sh` は放送の時間帯が終わるまで待ってから再起動する。手で再起動するときは、先に `python3 campus_chime.py --wait-idle` を実行する。

> **版のタグ:** `main` にマージすると、GitHub Actions が版のタグ（`v5.2.0` 以降）を自動で付ける（10.1 章）。ただし `v5.2.0` と `v5.3.0` は、Actions のトークンでは GitHub に断られるため、人が自分のマシンから 1 回だけ作って送る必要がある（実行は警告つきの緑で終わり、警告にそのまま打てるコマンドが書いてある）。`CHANGELOG.md` の版見出しのリンク（`releases/tag/vX.Y.Z`）は、v5.2.0 から先がこのタグに解決する（その 2 つは、人が送るまで解決しない）。

---

## 12. v2.0.0 設計案 → v3.0.0 変更点サマリ

| 分類 | 変更内容 |
|---|---|
| 機能 | **時報機能を新規追加**（毎正時のポ・ポ・ポ・ポーン＋時刻読み上げ） |
| 機能 | **おまけ機能を新規追加**（ひとこと／天気予報のランダム再生） |
| 機能 | 天気予報を「外部 API を必要時に呼ぶ」形で再導入（常駐ボットは復活させない） |
| 機能 | 追いかけ再生（起動遅れの取りこぼし防止）を追加 |
| 機能 | 二重再生防止をディスクへ永続化 |
| 構成 | 単一ファイル → `chime/` パッケージ化 |
| 構成 | 設定を `config.json` へ外出し（時刻・文言・地域をコード改変なしで変更可能） |
| 音声 | Open JTalk による実行時音声合成＋キャッシュを追加 |
| 音声 | 再生バックエンドを 3 種（pygame / コマンド / mock）にし、自動選択 |
| スケジューラ | 1 秒ポーリング → 次イベントまでの分割待機（CPU 負荷低減・時刻補正追従） |
| systemd | `PYTHONUNBUFFERED`、`SupplementaryGroups=audio`、最低限の保護設定を追加 |
| 品質 | ユニットテスト 190 件超と GitHub Actions による CI を追加 |
| 文書 | 本仕様書・`SETUP.md`・`CHANGELOG.md` を整備。`LEGACY_SYSTEM_SHUTDOWN.md` を削除 |

### v1.2.0 からの累積変更（v2.0.0 設計案で予定していた分）

| 分類 | 変更内容 |
|---|---|
| 環境 | Raspberry Pi OS Desktop → **Lite 32bit**（専用機化） |
| 機能 | `weather.py` 競合排除機能を **廃止**（`kill_conflict_process` / `CONFLICT_APP` 削除） |
| 音声 | `mixer.init()` に周波数・フォーマット・チャンネル・**バッファ 4096** を明示指定 |
| 音声 | 再生終了後の `mixer.quit()` を追加 |
| コード | 未使用変数 `is_time` と実装矛盾コメント（17:00 表記）を削除 |
| systemd | `time-sync.target` 待機、`SDL_AUDIODRIVER=alsa`、`Restart=always` を追加。存在しない `audio.target` を削除 |
| 構成 | 設置パスを `/home/pi/campus-chime` に統一 |
