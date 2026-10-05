# キャンパス時報システム 仕様書

**プロジェクト名:** Campus Chime System
**バージョン:** 6.0.0
**対応要件定義:** `docs/REQUIREMENTS.md` v6.0.0
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
├── cache/                     # 状態・TTS キャッシュ（Git 管理外）
│   ├── state.json
│   └── tts/
├── scripts/
├── tests/
└── docs/
```

### 2.1 v2.0.0 設計案からの構成変更

| 対象 | 変更 | 理由 |
|---|---|---|
| `campus_chime.py` | 単一ファイル → `chime/` パッケージ ＋ 薄いエントリポイント | 時報・天気・TTS の追加により単一ファイルでは責務が過大になったため |
| `config.example.json` | 新規追加 | 時刻・文言・地域をコードから外出しするため（FR-10） |
| `cache/state.json` | 新規追加 | 再起動をまたぐ二重再生防止（FR-06） |
| `tests/` | 新規追加 | NFR-07 |
| `scripts/` | 新規追加 | 導入手順の自動化・音声事前生成 |
| `.github/workflows/ci.yml` | 新規追加 | NFR-07 |
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
| `max_sleep_seconds` | `30.0` | 待機 sleep の分割単位（時刻補正への追従用） |

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

`Segment`（`path` / `label` / `fade_in_ms` / `optional`）の列を順番に再生する。

| クラス | 仕様 |
|---|---|
| `Player.play(segments)` | 存在しないファイルを除外（`optional=False` なら `PlaybackError`）し、`open()` → 各 `play_one()` → `close()` を実行。`close()` は `finally` で必ず呼ぶ。戻り値は実際に再生したセグメント数（`int`）で、再生対象が 1 つも無かった場合（除外の結果 0 件になった場合を含む）は例外を出さず `0` を返す |
| `PygamePlayer` | `pygame.mixer.init(frequency, size, channels, buffer)` で初期化し、`music.load` → `play(fade_ms=...)` → `get_busy()` が `False` になるまで 0.05 秒間隔でポーリング。`close()` で **`pygame.mixer.quit()`** |
| `CommandPlayer` | 拡張子に応じて `aplay` / `mpg123` を実行。フェードは非対応 |
| `MockPlayer` | ログ出力と `time.sleep` のみ（WAV は実長、上限 `mock_max_seconds`） |
| `create_player(settings, force)` | `auto` の場合: 開発環境 → mock、実機 → pygame → command → mock の順 |

> **v1.x からの変更:** v1.x は `mixer.init()` を毎回呼びながら `quit()` を行っておらず、デバイスを掴んだままになっていた。`close()` での解放を必須とする。

### 4.4 `chime/tts.py`（責務: 音声合成）

エンジンを設定順に試し、最初に成功したものを採用する。

| エンジン | `available()` | `synthesize()` |
|---|---|---|
| `PrerecordedEngine` | ディレクトリが存在する | 合成しない。`manifest.json`（文言→ファイル名）または `sha1(文言)[:20].wav` を探し、無ければ次のエンジンへ |
| `VoicevoxEngine` | `GET /version` が `voicevox.probe_timeout_seconds`（既定 2 秒）以内に 200 | `POST /audio_query` → `POST /synthesis` |

**フォールバックは実行時合成ではなく無音（v5.0.0）。** どちらのエンジンでも合成できない
文言は `TTSError` を送出し、呼び出し側（`chime/sequence.py` の `_append_speech()`）が
これを捕捉してエラーログと `plan.warnings` を残し、そのセグメントだけを落とす
（時報音・蛍の光・他の文言は再生を続ける）。v4.x までは最終段に `OpenJTalkEngine`
（実行時にオフライン合成する最終フォールバック）を置いていたが、フォールバックが
あるせいで壊れた状態（作り置き不足・古い `config.json`）でも別人の男性音声で
「それらしく」鳴ってしまい、故障の発覚を妨げていたため v5.0.0 でコードごと削除した。

**キャッシュ:** `cache/tts/{sha1(voice_id + 文言)[:20]}.wav`。一時ファイルへ書いてから `os.replace` で原子的に置き換える。`voice_id` に話者・話速等を含めるため、設定を変えれば別キャッシュになる。キャッシュが使われるのは VOICEVOX で実際に合成したときだけで、その場合、同じ文言は 2 回目以降合成されない。Pi では全文言を作り置き（`assets/voice/`）から引くため、合成もキャッシュも発生しない。

**`voicevox.probe_timeout_seconds` について:** 放送直前（実行時）に呼ばれる `available()` は、エンジンが落ちていた場合に即座に他のエンジンへフォールバックできるよう、既定で短い（2 秒）タイムアウトを使う。一方、VOICEVOX ENGINE は Docker での起動直後、ONNX モデルの読み込みのため `/version` が数秒〜数十秒応答しないことがある。`scripts/generate_voicevox.py` はこの値を長めに設定した `VoicevoxEngine` で `available()` を数秒おきに再試行し、`--wait`（既定 90 秒）で指定した秒数まで起動を待つ。

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
| `events_for_date(day)` | 稼働曜日なら時報（`start_hour`〜`end_hour`、`skip_hours` を除く）と閉館放送を生成し、`play_at` 昇順で返す |
| `iter_events(start, days)` | 最大 30 日先まで、`play_at >= start` のイベントを時系列で列挙 |
| `upcoming(now, limit)` | 一覧表示用 |
| `next_event(now, is_fired)` | まず「過去 `catchup_grace_seconds` 以内・未再生」を探し（追いかけ再生、FR-07）、無ければ未来の最初の未再生イベントを返す |
| `sleep_until(target, stop, precise)` | `max_sleep_seconds` ごとに分割して sleep し、都度実時刻を読み直す。`precise=True` の場合、残り 0.25 秒からは 2ms 刻みで詰める |

時報の `play_at` は `at − pip_lead`、閉館放送の `play_at` は `at` と同じ。

`sleep_until` を分割する理由は、NTP による時刻補正（特に RTC 非搭載機の起動直後の大きなジャンプ）に追従するため。単一の長い `sleep` では補正を反映できない。

### 4.9 `chime/sequence.py`（責務: 再生内容の組み立て）

| メソッド | 生成される `Segment` |
|---|---|
| `build_hourly(hour)` | ①時報音 ②時刻アナウンス ③（`weather_hours` の時刻だけ）天気予報 ④ひとこと |
| `build_closing()` | ①閉館アナウンス ②（任意）追加読み上げ ③蛍の光（フェードイン） |
| `build_text(text)` / `build_texts(texts)` | 任意文言の読み上げのみ。複数文は 1 文ずつ別セグメントにする |

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
- 音声合成失敗 → 当該セグメントを落として続行（**時報音そのものは必ず鳴る**）
- 組み立て中の想定外の例外（通信の途中切断、読み上げ文のテンプレートの書き間違いなど）→ 部品ごとに縮退し、**時報音は必ず鳴る**。時刻アナウンスのテンプレートを書き間違えたときは既定の文言で読む（8 章）

### 4.10 `chime/app.py`（責務: 全体の組み立てと常駐ループ）

```
[起動]
  ↓
[環境・バックエンド・TTS エンジンをログ出力]
  ↓
[次イベントを算出] ←──────────────────┐
  ↓ 無ければ 60 秒待って再算出         │
[prepare_at まで待機]                  │
  ↓                                    │
[待機中に予定が変わっていないか再確認] ─┘ 変わっていれば再算出
  ↓
[再生内容を組み立て]  ← 天気取得・音声合成はここで完結
  ↓
[play_at まで精密待機]
  ↓
[順次再生]
  ↓
[state.json に再生済みを記録] ─────────┘
```

- `SIGTERM` / `SIGINT` で `stop_event` を立てる。**待機中は待機を打ち切って正常終了する**（`systemctl stop` に即応）。**再生中は止まらず**、再生が終わってから終了する。閉館放送の蛍の光の途中で停止・再起動すると、systemd が約 90 秒後（`TimeoutStopSec` の既定値。ユニットでは変更していない）に強制終了する（今後の版で改善予定）
- イベント処理中の例外は捕捉し、当該回を再生済みとして記録したうえで常駐を継続する（無限リトライを避ける）
- 再生バックエンドは初回参照時に決定する（`--schedule` 等で不要な警告を出さないため）

### 4.11 `chime/cli.py`（責務: コマンドライン処理）

| 引数 | 動作 |
|---|---|
| （なし） | 常駐 |
| `--schedule [N]` | 予定を N 件表示 |
| `--test-hourly [HOUR]` | 時報を即時再生（省略時は現在時刻） |
| `--test` | 閉館放送を即時再生 |
| `--test-all` | 時報 → 閉館放送 |
| `--say TEXT` | 任意文言の読み上げ。作り置きに無い文言は「作り置きにありません。Pi では無音」と近い文言の候補を表示する |
| `--weather` | 天気予報の URL と読み上げ文を表示（`--dry-run` でなければ読み上げも） |
| `--generate-assets` | 時報音を生成し、時刻アナウンスの音声を用意できるか確認 |
| `--print-config` | 適用中の設定を JSON で表示 |
| `--config PATH` | 設定ファイルの明示指定 |
| `--backend {auto,pygame,command,mock}` | 再生バックエンドの強制 |
| `--dry-run` | 音を出さず内容のみ表示（`cache/state.json` は書き換えない） |
| `--log-level LEVEL` | ログレベル |
| `--version` | バージョン表示 |

終了コード: `0` 正常 / `1` 実行時エラー（`--weather` での天気取得失敗、`--generate-assets` で時刻アナウンスの音声を用意できなかった場合、`--say`・`--test-hourly`・`--test`・`--test-all` で実行した再生がすべて失敗した場合。ここでいう失敗とは、再生対象のセグメントを 1 つも用意できなかった場合と、音源ファイルの欠落など再生時のエラーにより 1 つも鳴らせなかった場合の両方を指す（`ChimeApp.play()` の戻り値で判定する）。`--test-hourly`・`--test-all` は時報と閉館放送を続けて再生するため、どちらか一方でも鳴れば `0`（時報音は合成不要のため必ず鳴り、おまけの読み上げのみが失敗した場合も `0` のまま）。`--dry-run` 指定時は実際の再生を行わず常に成功として扱うため、この判定の対象にならない。おまけの天気予報の取得に失敗しても、他のセグメントが鳴っていればエラー扱いしない）/ `2` 引数・設定エラー（`--say` に空文字列や空白のみを渡した場合、UTF-8 以外で保存された `config.json` を含む。8 章）。

`--dry-run` や `--test-hourly` などの試し鳴らしは、`cache/state.json` を書き換えない（ひとこと履歴が進まない）。

ログのタイムスタンプは `timezone` 設定に合わせて表示する（ハンドラのフォーマッタに変換関数を差し替える）。

---

## 5. 処理フロー（時報）

```
[systemd 起動]  ← After=time-sync.target（NTP の同期完了までは待たない）
      ↓
[次の正時 - 3秒 - 45秒 まで待機]  ← 30 秒ごとに時刻を読み直すため、起動直後の NTP による時刻の飛びに追従できる
      ↓
[天気取得 / 読み上げ音声の用意（作り置きから引く）]  ← 失敗しても続行
      ↓
[正時 - 3秒 まで精密待機]
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
| 音源ファイル欠落（必須） | `PlaybackError`。ERROR ログを出し、当該回をスキップ（プロセスは継続）。`--say`/`--test-hourly`/`--test`/`--test-all` から実行した場合は、この失敗が `ChimeApp.play()` の戻り値に反映され、終了コードにも影響する（4.11 章） |
| 音源ファイル欠落（任意） | WARNING ログを出し、そのセグメントのみスキップ |
| 音声合成の全エンジン失敗 | ERROR ログ。当該セグメントを落として残りを再生（**時報音は鳴る**） |
| 組み立て中の想定外の例外（通信の途中切断、読み上げ文のテンプレートの書き間違いなど） | ERROR ログ。**部品ごとに縮退**する。失敗した部品（時刻アナウンス・天気・ひとことなど）だけを落として残りを組み立て、**時報音は必ず鳴る**。時刻アナウンスのテンプレートを書き間違えたときは、既定の文言で読む |
| 組み立て全体の失敗 | 最小構成で再生する（時報音は鳴る） |
| 閉館放送の一方の欠落（アナウンスか蛍の光のどちらかを用意できない） | ログに記録し、欠けた側だけを落として、残りは鳴らす |
| `config.json` の文字コード不正（UTF-8 以外。Shift_JIS など） | 「UTF-8 で保存し直して」と日本語で案内し、**終了コード 2** で終了する（常駐は systemd が 10 秒ごとに再起動を繰り返すが、journal に原因が出る。以前は Python のトレースバックだけだった）。BOM 付き UTF-8 は読める。`python3 campus_chime.py --print-config` で確認できる |
| 廃止した設定キー（v6.0.0。`extra_segment.mode` / `weather_probability` / `always_weather_hours` / `always_quote_hours` / `fallback_to_quote`、`weather.provider` / `jma` / `max_weather_chars`）が `config.json` に残っている | 起動時に WARNING ログ「`<ファイル>` の `<キー>` は v6.0.0 で廃止しました（`<案内>`）。この行は無視します。消してください。」を 1 キーにつき 1 行出す。案内は、`extra_segment` の 5 キーが「天気を流す時刻は extra_segment.weather_hours で指定」、`weather` の 3 キーが「天気は Open-Meteo（weather.open_meteo）に一本化」。**値は無視して起動を続け**（既定値が使われる）、止めない。`journalctl -u campus_chime.service -p warning` で確認できる（キーの一覧と代わりの設定は 3.4 章） |
| 天気取得失敗 | WARNING ログ。天気だけ飛ばし、ひとことは必ず流れる（ひとことへの切り替えはしない） |
| ひとこと定義ファイル欠落・破損 | WARNING ログ。内蔵の予備文言を使用 |
| 状態ファイル破損 | WARNING ログ。初期状態として扱う |
| 再生中の例外 | ERROR ログ。`finally` で mixer を解放し、プロセスは継続 |
| イベント処理中の想定外例外 | ERROR ログ（スタックトレース付き）。当該回を再生済みとして記録し常駐継続 |
| プロセス異常終了 | systemd が 10 秒後に再起動。`state.json` により二重再生しない |

> **設計方針:** 失敗によってプロセス全体を落とさない。翌日以降の放送を継続できることを最優先する。

---

## 9. ログ仕様

形式: `%(asctime)s - %(levelname)s - %(name)s - %(message)s`（タイムゾーンは設定に追従）

| レベル | 出力タイミング |
|---|---|
| INFO | 起動、環境情報、次回予定、イベント準備、再生開始、再生内容（読み上げ文言を含む）、再生完了、停止 |
| WARNING | 追いかけ再生、天気取得失敗、任意音源の欠落、予定なし |
| ERROR | 依存欠落、必須音源の欠落、音声合成失敗、再生例外 |

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

ネットワーク・音声デバイス・外部コマンドに依存せず実行できる（Open-Meteo の応答は `tests/fixtures/` の実データ形式で再現）。

| ファイル | 対象 |
|---|---|
| `tests/test_config.py` | 設定のマージ・解決、`config.example.json` との同期、廃止した設定キーの警告と無視 |
| `tests/test_timesignal.py` | 読み上げ文言、WAV の形式・長さ・長音の開始位置 |
| `tests/test_scheduler.py` | 曜日・時間帯の展開、追いかけ再生、待機処理 |
| `tests/test_weather.py` | Open-Meteo の解析、文の組み立て、語彙の網羅、異常応答 |
| `tests/test_quotes.py` | 候補の抽出、直近除外、同梱データの健全性 |
| `tests/test_tts.py` | エンジンのフォールバック、キャッシュ、事前生成音声の参照 |
| `tests/test_state.py` | 永続化、日付をまたぐリセット、破損時の挙動 |
| `tests/test_sequence.py` | セグメント構成（天気を流す時刻・ひとことは毎回）、失敗時の縮退 |
| `tests/test_audio.py` | 再生順序、デバイス解放、バックエンド選択 |
| `tests/test_app.py` | 常駐ループ、停止要求、例外時の継続 |
| `tests/test_cli.py` | 引数解釈、終了コード、環境判定 |
| `tests/test_jsonfile.py` | JSON の読み書き（BOM 付き UTF-8、UTF-8 以外の案内、構文エラーの行・桁とヒント、一時ファイル経由の書き込み） |
| `tests/test_logsetup.py` | journal への重大度の接頭辞（`JOURNAL_STREAM` が一致するときだけ全行に付く）、ハンドラの重複防止 |
| `tests/test_generate_voicevox.py` | 作り置き生成スクリプト（`--config` の文言と既定の文言の和集合、`--prune` で残す文言、合成に失敗した文言を manifest に書かないこと） |
| `tests/test_voice_assets.py` | 同梱の音声の健全性（`assets/voice/` の manifest と WAV の欠け・余り・形式、既定の設定が読み上げる全文言が manifest にあること、`announce.wav` と `hotaru.mp3`。VOICEVOX は使わずファイルだけを調べる） |
| `tests/test_docs.py` | 文書の記載と実装の一致（文書中の `--say` の例が作り置きにあること、README・要求定義書・仕様書の版が `chime/__init__.py` の `__version__` と一致すること、`CHANGELOG.md` の先頭の版が `__version__` と一致すること、文書に書いた作り置きの件数・天気コードの語数がコードから数えた値と一致すること、廃止したキーが「廃止」の語なしに現行の設定として書かれていないこと） |

CI（`.github/workflows/ci.yml`）で Python 3.9 / 3.11 / 3.13 に対して自動実行する（`test` ジョブ。「CLI が起動すること」の `--test-hourly 12` は、実際の通信をしないよう `--config tests/fixtures/offline_config.json`（天気を無効にした設定）を渡す）。別ジョブ（`lint`。Python 3.11 のみ）で `pyflakes`（版を固定してインストールする）を `python -m pyflakes chime scripts tests campus_chime.py` で実行する。ワークフロー全体の権限は `contents: read` だけで、同じ ref の古い実行は新しい実行が始まると止める（`concurrency`）。この環境には音声合成エンジンを一切導入しないため、「合成エンジンが一つも使えない」状態がそのまま再現される。別ジョブ（`prerecorded-only`）で、時報の定型文・ひとこと・天気予報の全文言（`scripts/generate_voicevox.py` の `collect_phrases()` が列挙する語彙）を `--say ... --dry-run` で 1 件ずつ流し、無音になったことを示す警告（`を合成できませんでした`）が 1 件も出ないことを確認する。これにより、作り置き（`assets/voice/`）だけで全文言を賄えていることを回帰的に検証する（v4.x まではここで `open_jtalk` を導入し実際の音声合成を検証していたが、v5.0.0 でそのエンジンをコードごと削除したため不要になった）。

手元で `prerecorded-only` ジョブと同じ検証をするには、`.github/workflows/ci.yml` のコマンドをそのまま実行する（`cache/tts/` を消してから実行すると、キャッシュ済みの合成結果に隠れず確実に検証できる）。

### 10.2 実機での確認

| # | 項目 | 手順 | 期待結果 |
|---|---|---|---|
| T-01 | Mock 動作 | 開発機で `python3 campus_chime.py --test-all` | 音を出さずログのみ出力 |
| T-02 | 時報の即時再生 | Pi 上で `python3 campus_chime.py --test-hourly` | ポ・ポ・ポ・ポーン → 時刻 → おまけ が順に鳴る |
| T-03 | 閉館放送の即時再生 | Pi 上で `python3 campus_chime.py --test` | アナウンス → 蛍の光（フェードイン） |
| T-04 | 音声品質 | T-02・T-03 実施時に聴取 | 音飛び・カクつきがない（NFR-01） |
| T-05 | 時報の時刻精度 | 電波時計等と聴き比べ | 長音の開始が正時と一致（±100ms、NFR-04） |
| T-06 | サービス起動 | `sudo systemctl status campus_chime.service` | `active (running)` |
| T-07 | 時刻トリガー | `config.json` で `hourly.start_hour`/`end_hour` を直近の時刻に変更して待機 | 定刻に自動再生される |
| T-08 | 二重再生防止 | T-07 の再生直後に `sudo systemctl restart` | 再生が繰り返されない |
| T-09 | 追いかけ再生 | 正時の 30 秒後に `sudo systemctl restart` | 遅れて再生され、WARNING が記録される |
| T-10 | 自動復旧 | Pi を再起動 | 起動後にサービスが自動的に active になる |
| T-11 | 待機負荷 | `top` で当該プロセスを確認 | CPU 使用率 1% 未満（NFR-02） |
| T-12 | オフライン動作 | Wi-Fi を切って `--test-hourly` | 時報は鳴り、おまけはひとことになる |
| T-13 | 天気取得 | `python3 campus_chime.py --weather` | 現在の天気と気温の文が表示・読み上げされる |

> T-07 実施後は `config.json` を元に戻すこと。

---

## 11. 運用・保守

| 操作 | コマンド |
|---|---|
| 状態確認 | `sudo systemctl status campus_chime.service` |
| ログ追尾 | `journalctl -u campus_chime.service -f` |
| 予定確認 | `python3 campus_chime.py --schedule` |
| 更新反映 | `cd /home/pi/campus-chime && git pull && sudo systemctl restart campus_chime.service` |
| 設定変更 | `nano config.json` → `sudo systemctl restart campus_chime.service` |
| 一時停止 | `sudo systemctl stop campus_chime.service` |
| 自動起動解除 | `sudo systemctl disable campus_chime.service` |
| 時報音の再生成（時報音の設定を変えたとき） | `python3 campus_chime.py --generate-assets` |

> **運用上の注意:** **16:55〜17:02（閉館放送の前後）は、更新・再起動・停止をしない。** 待機中は停止要求にすぐ応じるが、再生中は止まらず、蛍の光の途中なら systemd が約 90 秒後に強制終了する（4.10 章。今後の版で改善予定）。

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
