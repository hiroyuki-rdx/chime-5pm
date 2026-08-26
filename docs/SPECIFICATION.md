# キャンパス時報システム 仕様書

**プロジェクト名:** Campus Chime System
**バージョン:** 3.0.0
**対応要件定義:** `docs/REQUIREMENTS.md` v3.0.0
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
│   ├── hotaru.mp3             # 蛍の光（Public Domain / 44.1kHz stereo）
│   ├── quotes.json            # 「ひとこと」定義
│   ├── voice/                 # 事前生成音声（任意）＋ manifest.json
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
| `hourly.skip_hours` | `[]` | 除外する時（例: `[12]`） |
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
| `announce_template` | `"{period}{hour}時をお知らせしました。"` | 読み上げテンプレート |
| `use_noon_template` | `true` | 12 時に専用文言を使うか |
| `noon_template` | `"正午をお知らせしました。"` | 12 時の文言 |
| `period_am` / `period_pm` | `"午前"` / `"午後"` | テンプレートの `{period}` |

#### `extra_segment`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `enabled` | `true` | おまけ放送の有効／無効 |
| `weather_probability` | `0.4` | 天気予報が選ばれる確率 |
| `always_weather_hours` | `[10]` | 必ず天気予報にする時 |
| `always_quote_hours` | `[]` | 必ずひとことにする時 |
| `fallback_to_quote` | `true` | 天気取得失敗時にひとことへ切り替えるか |

#### `quotes`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `file` | `assets/quotes.json` | ひとこと定義ファイル |
| `avoid_recent` | `8` | 直近この件数と同じものは選ばない |

#### `weather`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `enabled` | `true` | 天気予報の有効／無効 |
| `provider` | `"jma"` | `jma`（気象庁）または `open_meteo` |
| `timeout_seconds` | `8.0` | HTTP タイムアウト |
| `cache_minutes` | `60` | 取得結果のキャッシュ時間 |
| `jma.area_code` | `"130000"` | 気象庁の地域コード（府県予報区） |
| `jma.area_name` | `"東京地方"` | timeSeries 内で優先する地域名（前方一致） |
| `jma.label` | `"東京"` | 読み上げに使う地名 |
| `open_meteo.latitude` / `longitude` | `35.6895` / `139.6917` | 座標 |
| `open_meteo.label` | `"東京"` | 読み上げに使う地名 |
| `template` | `"{when}の{label}の天気は、{weather}。{details}"` | 読み上げテンプレート |
| `details_separator` / `suffix` | `"、"` / `"です。"` | 詳細部の区切りと語尾 |

#### `tts`

| 項目 | 既定値 | 意味 |
|---|---|---|
| `engines` | `["prerecorded","voicevox","open_jtalk"]` | 上から順に試す |
| `cache_dir` | `cache/tts` | 合成結果のキャッシュ先 |
| `prerecorded_dir` | `assets/voice` | 事前生成音声の置き場 |
| `open_jtalk.binary` | `"open_jtalk"` | 実行ファイル |
| `open_jtalk.dictionary` / `voice` | `""` | 空なら既知の場所から自動検出 |
| `open_jtalk.sampling_frequency` | `48000` | 合成サンプリング周波数 |
| `open_jtalk.speed` | `1.0` | 話速（`-r`） |
| `open_jtalk.additional_half_tone` | `0.0` | 音の高さ（`-fm`） |
| `open_jtalk.volume_gain_db` | `0.0` | 音量ゲイン（`-g`） |
| `voicevox.base_url` | `http://127.0.0.1:50021` | VOICEVOX ENGINE の URL |
| `voicevox.speaker` | `3` | 話者 ID（3 = ずんだもん・ノーマル） |

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
| `total_seconds(settings)` | 時報音全体の長さ（既定 4.0 秒） |
| `hour_parts(hour, settings)` | 12 時間表記の部品（`period` / `hour` / `hour24`） |
| `announce_text(hour, settings)` | 読み上げ文言。12 時は `noon_template` |

波形は各トーンの前後に `envelope_ms` の直線フェードを掛け、クリックノイズを防ぐ。

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

`Segment`（`path` / `label` / `fade_in_ms` / `gap_after_ms` / `optional`）の列を順番に再生する。

| クラス | 仕様 |
|---|---|
| `Player.play(segments)` | 存在しないファイルを除外（`optional=False` なら `PlaybackError`）し、`open()` → 各 `play_one()` → `close()` を実行。`close()` は `finally` で必ず呼ぶ |
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
| `VoicevoxEngine` | `GET /version` が 2 秒以内に 200 | `POST /audio_query` → `POST /synthesis` |
| `OpenJTalkEngine` | 実行ファイル・辞書・音響モデルが揃う | `open_jtalk -x DIC -m VOICE -r 話速 -fm 高さ -g 音量 -s 周波数 -ow OUT input.txt` |

辞書・音響モデルは設定が空なら次の順に自動検出する。

- 辞書: `/var/lib/mecab/dic/open-jtalk/naist-jdic` → `/usr/share/open_jtalk/open_jtalk_dic_utf_8-*` → `/usr/local/dic`
- 音響モデル: `/usr/share/hts-voice/*/*.htsvoice` ほか

**キャッシュ:** `cache/tts/{sha1(voice_id + 文言)[:20]}.wav`。一時ファイルへ書いてから `os.replace` で原子的に置き換える。`voice_id` に話者・話速等を含めるため、設定を変えれば別キャッシュになる。同じ文言は 2 回目以降合成されない（時報の定型文は初回のみ）。

### 4.5 `chime/weather.py`（責務: 天気予報の取得）

| 提供元 | エンドポイント |
|---|---|
| `jma` | `https://www.jma.go.jp/bosai/forecast/data/forecast/{area_code}.json` |
| `open_meteo` | `https://api.open-meteo.com/v1/forecast?...&timezone=Asia/Tokyo&forecast_days=1` |

**気象庁 JSON の解釈**

- 天気: `timeSeries` のうち `weathers` を持つ系列から、設定した地域（前方一致、無ければ先頭）を選ぶ。`timeDefines` が当日の要素を優先し、全角スペースを除去する
- 最高／最低気温: `temps` を持つ系列を `timeDefines` と対にし、**時刻が 6 時未満なら最低・以降なら最高**として日付ごとに畳み込む
- 降水確率: `pops` を持つ系列から当日分の**最大値**を採る
- いずれも欠落しうる前提で、取れた要素だけで文を組み立てる（当日昼発表の予報には当日の最低気温が無い、など）

**読み上げ文の組み立て**

```
{when}の{label}の天気は、{weather}。最高気温は{max}度、最低気温は{min}度、降水確率は{pop}パーセントです。
```

`when` は当日=「今日」、翌日=「明日」、それ以降は「M月D日」。詳細が 1 つも取れない場合は詳細部を丸ごと省略する。

取得結果は `cache_minutes` の間メモリ上に保持する（NFR-06）。あらゆる失敗は `WeatherError` に正規化し、呼び出し側が「ひとこと」へ切り替えられるようにする。

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

`Event` は `key` / `kind` / `hour` / `minute` / `at`（内容の時刻）/ `play_at`（再生開始）/ `prepare_at`（準備開始）を持つ。

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
| `build_hourly(hour)` | ①時報音 ②時刻アナウンス ③おまけ（ひとこと／天気予報） |
| `build_closing()` | ①閉館アナウンス ②（任意）追加読み上げ ③蛍の光（フェードイン） |
| `build_text(text)` | 任意文言の読み上げのみ |

`choose_extra(hour, settings, rng)` の判定順序:

1. `enabled` が `false` → なし
2. `hour` が `always_weather_hours` に含まれる → 天気予報
3. `hour` が `always_quote_hours` に含まれる → ひとこと
4. `rng.random() < weather_probability` → 天気予報、そうでなければひとこと

**失敗時の扱い（重要）**

- 天気取得失敗 → 警告を記録し、`fallback_to_quote` ならひとことへ
- 音声合成失敗 → 当該セグメントを落として続行（**時報音そのものは必ず鳴る**）

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

- `SIGTERM` / `SIGINT` で `stop_event` を立て、待機を打ち切って正常終了する（`systemctl stop` に即応）
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
| `--say TEXT` | 任意文言の読み上げ |
| `--weather` | 天気予報の URL と読み上げ文を表示（`--dry-run` でなければ読み上げも） |
| `--generate-assets` | 時報音と時報用定型文の音声を事前生成 |
| `--print-config` | 適用中の設定を JSON で表示 |
| `--config PATH` | 設定ファイルの明示指定 |
| `--backend {auto,pygame,command,mock}` | 再生バックエンドの強制 |
| `--dry-run` | 音を出さず内容のみ表示 |
| `--log-level LEVEL` | ログレベル |
| `--version` | バージョン表示 |

終了コード: `0` 正常 / `1` 実行時エラー（天気取得失敗・音声生成失敗）/ `2` 引数・設定エラー。

ログのタイムスタンプは `timezone` 設定に合わせて表示する（ハンドラのフォーマッタに変換関数を差し替える）。

---

## 5. 処理フロー（時報）

```
[systemd 起動]
      ↓
[NTP 時刻同期完了を待機]  ← time-sync.target
      ↓
[次の正時 - 3秒 - 45秒 まで待機]
      ↓
[天気取得 / 音声合成]  ← 失敗しても続行
      ↓
[正時 - 3秒 まで精密待機]
      ↓
09:59:57 ポ ─ 09:59:58 ポ ─ 09:59:59 ポ ─ 10:00:00 ポーン
      ↓
[「午前10時をお知らせしました。」]
      ↓
[ひとこと または 天気予報]
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
| `Wants` / `After=time-sync.target` | 本機は RTC 非搭載。時刻同期前に起動すると誤った時刻で判定するおそれがある（NFR-04） |
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
| 生成 | 実行時に合成 | 同梱 | 同梱 | 実行時に合成＋キャッシュ |
| 形式 | 44.1kHz / 16bit / ステレオ | 24kHz / 16bit / モノラル | MPEG-1 Layer3 128kbps / 44.1kHz | 48kHz / 16bit / モノラル |
| フェード | 各トーンに 5ms | なし | フェードイン 2000ms | なし |
| 権利 | 自作（合成） | VOICEVOX 利用規約に従いクレジット表記 | Public Domain | Open JTalk / HTS Voice |

---

## 8. エラーハンドリング仕様

| 事象 | 挙動 |
|---|---|
| `pygame` 未導入 | 外部コマンド再生へフォールバック。それも不可なら ERROR ログを出して mock 動作 |
| 音源ファイル欠落（必須） | `PlaybackError`。ERROR ログを出し、当該回をスキップ（プロセスは継続） |
| 音源ファイル欠落（任意） | WARNING ログを出し、そのセグメントのみスキップ |
| 音声合成の全エンジン失敗 | ERROR ログ。当該セグメントを落として残りを再生（**時報音は鳴る**） |
| 天気取得失敗 | WARNING ログ。ひとことへ切り替え |
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

参照方法:

```bash
journalctl -u campus_chime.service -f            # 追尾
journalctl -u campus_chime.service --since today # 本日分
journalctl -u campus_chime.service -p err        # エラーのみ
```

---

## 10. テスト仕様

### 10.1 自動テスト

```bash
python3 -m unittest discover -s tests -t . -v
```

ネットワーク・音声デバイス・外部コマンドに依存せず実行できる（気象庁／Open-Meteo の応答は `tests/fixtures/` の実データ形式で再現）。

| ファイル | 対象 |
|---|---|
| `tests/test_config.py` | 設定のマージ・解決、`config.example.json` との同期 |
| `tests/test_timesignal.py` | 読み上げ文言、WAV の形式・長さ・長音の開始位置 |
| `tests/test_scheduler.py` | 曜日・時間帯の展開、追いかけ再生、待機処理 |
| `tests/test_weather.py` | 気象庁／Open-Meteo の解析、文の組み立て、異常応答 |
| `tests/test_quotes.py` | 候補の抽出、直近除外、同梱データの健全性 |
| `tests/test_tts.py` | エンジンのフォールバック、キャッシュ、事前生成音声の参照 |
| `tests/test_state.py` | 永続化、日付をまたぐリセット、破損時の挙動 |
| `tests/test_sequence.py` | セグメント構成、おまけの抽選、失敗時の縮退 |
| `tests/test_audio.py` | 再生順序、デバイス解放、バックエンド選択 |
| `tests/test_app.py` | 常駐ループ、停止要求、例外時の継続 |
| `tests/test_cli.py` | 引数解釈、終了コード、環境判定 |

CI（`.github/workflows/ci.yml`）で Python 3.9 / 3.11 / 3.13 に対して自動実行する。

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
| T-13 | 天気取得 | `python3 campus_chime.py --weather` | 当日の予報文が表示・読み上げされる |

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
| キャッシュ再生成 | `rm -rf cache/tts && python3 campus_chime.py --generate-assets` |

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
