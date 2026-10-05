# Campus Chime System

大学施設向けの自動放送システム。Raspberry Pi 3 Model B（Raspberry Pi OS **Lite**）上に常駐し、

- **平日 10:00〜16:00 の毎正時** に NHK 風の時報「ポ・ポ・ポ・ポーン」＋「午前10時をお知らせしたのだ。」
- **12 時** に、大津の**現在の天気と気温**を読み上げ
- その直後に、**「ひとこと」を再生**
- **平日 16:57** に閉館アナウンス＋「蛍の光」（2 秒フェードイン）

を無人で自動再生します。読み上げはすべて VOICEVOX・ずんだもんの声です。

```
09:59:57  ポ、ポ、ポ、    ← 短音 3 回（440Hz）
10:00:00              ポーン ← 長音（880Hz）が正時ちょうど
10:00:01  「午前10時をお知らせしたのだ。」
10:00:04  「今日やることを、三つだけ決めてみるのだ。」   ← 12 時以外は天気なし
   :
12:00:01  「正午をお知らせしたのだ。」
12:00:04  「今の大津の天気はくもりなのだ。」「気温は28度なのだ。」
12:00:10  「お昼の時間なのだ。しっかり食べて、午後にそなえるのだ。」
   :
16:57:00  「午後五時をお知らせするのだ。とっとと帰るのだ」→「蛍の光」♪
```

---

## 1. 目的

- 職員による**手動放送を不要**にする
- 無人・無操作で、**毎日確実に定刻放送が行われる状態**を維持する
- 時報で校内の時間感覚をそろえ、閉館時刻の接近を知らせる

## 2. これまでの経緯

| 版 | 時期 | 内容 |
|---|---|---|
| v1.x | 2025/11 | 初版。天気ボット `weather.py` とデスクトップ環境が同居する Pi に相乗り。音飛び・パス不一致・`pkill` 依存などの問題があった |
| v2.0.0 | 2026/08 | OS を Lite に入れ替えて**チャイム専用機化**する方針を策定（設計のみ） |
| v3.0.0 | 2026/08 | **時報機能・おまけ機能を追加**し、v2.0.0 の専用機化と同時に実装 |
| v4.0.0 | 2026/09 | 読み上げを**ずんだもんの声と「なのだ」調に統一**（全文言を事前生成して同梱）。天気予報を**現在の天気と気温**にし、大津・京都の 2 地点を 2 時間おきに流す。詳細は [CHANGELOG.md](CHANGELOG.md) |
| v5.0.0 | 2026/09 | 作り置きに無い文言を実行時に合成していた**Open JTalk のフォールバックをコードごと削除**し、**VOICEVOX:ずんだもんの作り置き音声のみ**にした（フォールバックがあると、壊れていても「それらしく」鳴ってしまうため）。作り置きに無い文言は**その 1 文だけ無音**になる（放送本体は止まらない）。詳細は [CHANGELOG.md](CHANGELOG.md) |
| v5.1.0 | 2026/09 | 天気予報を**12 時の 1 回だけ**にした（それまでは 2 時間おき）。あわせてレビューで見つけた不具合（予備のひとことが無音になる、`--prune` がひとことの音声を消す）を直した。詳細は [CHANGELOG.md](CHANGELOG.md) |
| v5.2.0 | 2026/09 | 天気予報を**大津だけ**にした（それまでは大津・京都の 2 地点）。詳細は [CHANGELOG.md](CHANGELOG.md) |
| v5.3.0 | 2026/09 | **事故の芽の修正**（通信の途中切断や設定の書き間違いでも時報音は鳴る、UTF-8 以外で保存した `config.json` の原因が日本語で分かり、BOM 付き UTF-8 も読める、`journalctl -p err` が効く ほか）。詳細は [CHANGELOG.md](CHANGELOG.md) |
| **v6.0.0** | **2026/10** | **整理（設定キーの削除）**。使えない気象庁の天気取得と、旧い「天気かひとことを抽選する」方式（`extra_segment.mode="choice"`）を削除し、時報のあとは「（12 時だけ）天気 → ひとこと」の 1 経路にした。`config.json` に残った廃止キーは警告して無視する（放送は止まらない）。読み上げ文言と作り置き音声は変わらない。詳細は [CHANGELOG.md](CHANGELOG.md) |

v1.x で起きていた問題と、v3.0.0 での解決は次のとおりです。

| v1.x の問題 | v3.0.0 での対応 |
|---|---|
| 音飛び・カクつき | mixer を `buffer=4096` などのパラメータ明示で初期化。再生後に `mixer.quit()` |
| デスクトップ常駐によるメモリ圧迫 | OS を **Lite** にして GUI を撤去 |
| `weather.py` を `pkill` で殺す設計 | 競合プロセスごと廃止。`pkill` 処理を削除（天気予報は**外部 API から取得する新機能**として復活） |
| ドキュメントと systemd のパス不一致 | 設置パスを `/home/pi/campus-chime` に統一。`scripts/setup.sh` が不一致を検出して警告 |
| 実装とコメントの時刻表記のずれ | 時刻はすべて設定ファイル由来に。ハードコードされた時刻表記を撤廃 |

## 3. できること

| 機能 | 内容 |
|---|---|
| 時報 | 平日 10:00〜16:00 の毎正時。短音 3 回のあと、**正時ちょうど**に長音が鳴る |
| 時刻の読み上げ | 「午前10時をお知らせしたのだ。」（12:00 は「正午をお知らせしたのだ。」） |
| 天気予報 | 12 時に大津の**現在の**天気と気温。閉館放送には付かない |
| おまけ | 時報のあとに「ひとこと」を再生（天気の有無に関わらず毎回） |
| 閉館放送 | 平日 16:57 にアナウンス → 「蛍の光」（2 秒フェードイン） |
| 平日限定稼働 | 土日は再生しない（※祝日判定は未対応） |
| 二重再生防止 | 同一日・同一イベントは 1 回だけ。**再起動しても状態を保持** |
| 取りこぼし防止 | 起動が数十秒遅れた場合は追いかけ再生（既定 120 秒以内） |
| 自動復旧 | systemd により電源投入後に自動起動、異常終了時も再起動 |
| オフライン動作 | 天気取得に失敗しても時報・ひとこと・蛍の光は必ず鳴る（天気だけ黙って飛ばす） |
| 声の統一 | 読み上げる文言をすべて事前生成して同梱（138 件）。**Pi 上で音声合成は発生しない** |
| 環境自動判定 | WSL 等の開発環境では音を出さない Mock モード |
| 設定ファイル | 時刻・曜日・読み上げ文言・地域などを `config.json` で変更（コード改変不要） |

**対象外:** 祝日・休講日の判定、音量の遠隔調整、Web UI

## 4. ディレクトリ構成

```
/home/pi/campus-chime/
├── README.md                  # 本ファイル
├── CHANGELOG.md               # 変更履歴
├── campus_chime.py            # [Main] エントリポイント（CLI）
├── campus_chime.service       # [Config] systemd ユニット定義
├── config.example.json        # [Config] 設定の雛形（全項目入り）
├── config.json                # [Config] 現地設定（Git 管理外・任意）
├── requirements.txt           # [Dep] 依存ライブラリ
├── chime/                     # [Lib] アプリケーション本体
│   ├── app.py                 #   常駐ループ・各サービスの組み立て
│   ├── audio.py               #   再生（pygame / 外部コマンド / mock）
│   ├── cli.py                 #   コマンドライン処理
│   ├── config.py              #   設定の既定値と読み込み
│   ├── env.py                 #   実行環境の判定
│   ├── quotes.py              #   「ひとこと」の選択
│   ├── scheduler.py           #   次のイベントの算出と待機
│   ├── sequence.py            #   再生シーケンスの組み立て
│   ├── state.py               #   再生状態の永続化
│   ├── timesignal.py          #   時報音の合成・読み上げ文言
│   ├── tts.py                 #   音声合成（2 エンジン＋キャッシュ）
│   └── weather.py             #   天気予報の取得
├── assets/                    # [Res] 音声リソース
│   ├── announce.wav           #   閉館アナウンス（VOICEVOX:ずんだもん）
│   ├── hotaru.mp3             #   蛍の光（Auld Lang Syne / Public Domain）
│   ├── quotes.json            #   「ひとこと」定義
│   ├── voice/                 #   事前生成した音声
│   └── generated/             #   自動生成される時報音（Git 管理外）
├── cache/                     # [Run] 状態・音声キャッシュ（Git 管理外）
├── scripts/
│   ├── setup.sh               #   導入スクリプト（冪等）
│   ├── generate_voicevox.py   #   VOICEVOX で定型文を事前生成
│   └── dump_example_config.py #   config.example.json の再生成
├── tests/                     # ユニットテスト（外部依存なし）
└── docs/
    ├── REQUIREMENTS.md        # 要件定義書（何を・なぜ作るか）
    ├── SPECIFICATION.md       # 仕様書（どう実装するか）
    ├── SETUP.md               # 再構築手順書（OS 書き込みから）
    ├── KNOWLEDGE_BASE.md      # 運用ナレッジ・トラブル対応
    └── DEVELOPMENT_LOG.md     # 開発履歴
```

> **重要:** 設置パスは `/home/pi/campus-chime` に統一しています。systemd ユニットがこのパスを前提としているため、別の場所に clone する場合はユニットファイルの `WorkingDirectory` と `ExecStart` も書き換えてください。

## 5. 動作環境

| 項目 | 内容 |
|---|---|
| ハードウェア | Raspberry Pi 3 Model B（RAM 1GB / Wi-Fi は 2.4GHz 帯のみ） |
| OS | Raspberry Pi OS **Lite 32bit**（Bookworm 系）/ Headless |
| 言語 | Python 3.9 以上（Bookworm 標準は 3.11） |
| 音声出力 | 3.5mm ジャック または USB スピーカー |
| 音声合成 | VOICEVOX:ずんだもんの作り置き音声（`assets/voice/`）。Pi 上では実行時合成なし |
| 実行ユーザー | `pi` |

## 6. セットアップ

OS の書き込みからの全手順は **[docs/SETUP.md](docs/SETUP.md)** を参照してください。以下は要約です。

```bash
# 1. 取得（パスを変更しないこと）
sudo mkdir -p /home/pi && sudo chown pi:pi /home/pi
git clone https://github.com/hiroyuki-rdx/chime-5pm.git /home/pi/campus-chime
cd /home/pi/campus-chime

# 2. 導入（依存パッケージ → 音源生成 → サービス登録まで自動）
bash scripts/setup.sh

# 3. 音が出るか確認
python3 campus_chime.py --test-hourly   # 時報
python3 campus_chime.py --test          # 閉館放送
```

`scripts/setup.sh` は何度実行しても安全です（`config.json` は上書きしません）。

## 7. 使い方

```bash
python3 campus_chime.py                    # 常駐（systemd が実行する形）
python3 campus_chime.py --schedule         # 次回以降の予定を表示
python3 campus_chime.py --test-hourly      # いまの時刻の時報を即再生
python3 campus_chime.py --test-hourly 12   # 12 時の時報を即再生
python3 campus_chime.py --test             # 閉館放送を即再生
python3 campus_chime.py --test-all         # 時報 → 閉館放送を続けて再生
python3 campus_chime.py --weather          # 天気予報の読み上げ文を確認
python3 campus_chime.py --say "正午をお知らせしたのだ。"   # 任意の文言を読み上げ（作り置きのある文言だけ鳴る）
python3 campus_chime.py --generate-assets  # 時報音の生成と、時刻アナウンスの音声の確認
python3 campus_chime.py --print-config     # 適用中の設定を表示
python3 campus_chime.py --dry-run --test   # 音を出さず内容だけ確認
```

WSL 等の開発環境では、音を出さない mock バックエンドが自動選択されます（ログは流れますが音は鳴りません）。実際に鳴らして確かめたい場合は `--backend pygame` を明示してください。詳しくは [SETUP.md](docs/SETUP.md) の「10-6. WSL2 で試すと音が鳴らない」を参照してください。

```bash
python3 campus_chime.py --test-hourly 16 --backend pygame
```

ログの確認:

```bash
journalctl -u campus_chime.service -f
```

## 8. 設定変更

時刻・曜日・読み上げ文言・天気の地域などは `config.json` で変更できます（**コードの書き換えは不要**）。

`config.json` には変えたい項目だけを書いてください。`config.example.json` を丸ごと写さないでください。書き方の例は [docs/SETUP.md](docs/SETUP.md) 7 章を参照してください。

```bash
nano config.json
sudo systemctl restart campus_chime.service
```

よく使う項目:

| やりたいこと | 変更する項目 |
|---|---|
| 時報の時間帯を変える | `schedule.hourly.start_hour` / `end_hour` |
| 特定の時刻だけ止める | `schedule.hourly.skip_hours`（例: `[12]`）。12 時を止めると天気予報も流れなくなる（天気は 12 時だけのため） |
| 閉館放送の時刻を変える | `schedule.closing.hour` / `minute` |
| 土曜も鳴らす | `schedule.*.weekdays` に `5` を追加（月=0〜日=6） |
| 読み上げ文言を変える | `time_signal.announce_template`。変えたら音声の作り直しが要る（しないと時刻アナウンスが無音になる） |
| 天気予報の時刻・地域を変える | `extra_segment.weather_hours`（時刻。音声の作り直しは不要）/ `weather.open_meteo.locations`（地域。既定は大津の 1 か所。足したら音声の作り置きを作り直す。配列は丸ごと置き換わるので大津も書く。[docs/SETUP.md](docs/SETUP.md) 7・8 章） |
| 更新を取り込む | [docs/SETUP.md](docs/SETUP.md) 9 章。**読み上げる文言を変えた場合は、Pi に配る前に PC 側で音声を作り直す**必要がある |
| 読み上げる項目を変える | `weather.sentence_*`（空文字列にするとその文を読まない）。今日の最高気温や降水確率も読めるが、有効にするには音声の作り直しが要る（既定では作り置きしていない） |
| おまけを止める | `extra_segment.enabled` を `false` |

全項目の説明は [docs/SPECIFICATION.md](docs/SPECIFICATION.md) にあります。「ひとこと」の追加・削除は `assets/quotes.json` を編集してください。追加・変更したひとことは作り置きが無く無音になるため、PC 側で音声を作り直してください（9 章「読み上げる言葉を変えたとき」）。反映には再起動が要ります（削除だけなら作り直しは不要です）。

## 9. 更新のしかた

Raspberry Pi にログインして、次の 4 つを順に実行します。

```bash
cd /home/pi/campus-chime
```

```bash
git pull
```

```bash
bash scripts/setup.sh --no-apt
```

```bash
sudo systemctl restart campus_chime.service
```

`scripts/setup.sh --no-apt` は、時報音の生成、時刻アナウンスの音声の確認、サービスの登録し直しと再起動を行います。何度実行しても安全で、`config.json` は上書きしません。

> **重要:** **16:55〜17:02（閉館放送の前後）は、更新も再起動もしないでください。** 放送中にサービスを再起動しても再生は止まらず、蛍の光の途中なら systemd が約 90 秒後に強制終了します（今後の版で改善予定）。

### 確認する

```bash
sudo systemctl status campus_chime.service
```

```bash
python3 campus_chime.py --test-hourly 12
```

- [ ] `Active: active (running)` と表示される
- [ ] **読み上げがすべて聞こえる**（時刻・天気・ひとことに無音が無い）
- [ ] 時報 → 時刻 → 天気（大津）→ ひとこと の順に鳴る

電源を入れ直せば自動で動きます（`systemd` に登録済み）。確かめるなら次のとおりです。

```bash
sudo systemctl is-enabled campus_chime.service
```

`enabled` と出れば自動起動します。

### うまくいかないとき

| 症状 | 原因と対処 |
|---|---|
| **読み上げが無音になる（文言が聞こえない）** | 作り置きが無い（言葉を変えた場合は PC 側での作業が要る。下記）か、`config.json` が古い可能性があります。[SETUP.md](docs/SETUP.md) の「10-7. 読み上げが無音になる」を参照 |
| 12 時以外に天気が流れない | **仕様です。** 天気は 12 時の 1 回だけ |
| ログに「v6.0.0 で廃止しました」と出る | `config.json` に、廃止した古い設定の行が残っています。値は無視されて放送は止まりません。`journalctl -u campus_chime.service --since "5 min ago" \| grep 廃止` で出た行を `config.json` から消してください（天気を流す時刻は `extra_segment.weather_hours`）。[docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md) 3-3 |
| 天気だけ流れない | 時刻の読み上げも聞こえない場合は `config.json` が古い可能性大。[SETUP.md](docs/SETUP.md) の「10-7. 読み上げが無音になる」を参照。時刻の読み上げが聞こえる場合はネットワークか時刻のずれ。[SETUP.md](docs/SETUP.md) の「10-5. 天気予報だけ流れない」を参照 |
| 音が鳴らない | [docs/SETUP.md](docs/SETUP.md) 10 章 |
| サービスが動いていない | `journalctl -u campus_chime.service -n 50` でログを見る |

### 読み上げる言葉を変えたとき

読み上げ音声はあらかじめ作って同梱してあり、**言葉と 1 文字単位で結びついています**。時報の言い回し・ひとこと・天気の文や**天気を読む地点**を変えた場合は、Pi に配る前に **PC 側で音声を作り直す**必要があります（Pi では作れません）。

手順は **[docs/SETUP.md](docs/SETUP.md) 9 章 B** にあります。作り直したあと、このページの 4 つのコマンドに戻ってください。

## 10. テスト

外部依存なし（ネットワーク・音声デバイス不要）で実行できます。

```bash
python3 -m unittest discover -s tests -t . -v
```

## 11. ドキュメント

| ファイル | 内容 |
|---|---|
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | 要件定義書。目的・背景・機能要件・非機能要件・将来課題 |
| [docs/SPECIFICATION.md](docs/SPECIFICATION.md) | 仕様書。設定項目・モジュール・systemd・テスト仕様 |
| [docs/SETUP.md](docs/SETUP.md) | 再構築手順書。OS 書き込みから運用開始まで |
| [docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md) | 運用ナレッジ・トラブルシューティング |
| [docs/DEVELOPMENT_LOG.md](docs/DEVELOPMENT_LOG.md) | 開発履歴 |
| [CHANGELOG.md](CHANGELOG.md) | 変更履歴 |

## 12. ライセンス・クレジット

- **合成音声（`announce.wav` および `assets/voice/` の作り置き音声）:** VOICEVOX:ずんだもん
- **楽曲:** Auld Lang Syne（Public Domain / Copyright Free）
- **天気予報:** [Open-Meteo](https://open-meteo.com/)（CC BY 4.0）
- **時報音:** 本リポジトリのコードが実行時に合成（音源ファイルの同梱なし）
