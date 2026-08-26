# Campus Chime System

大学施設向けの自動放送システム。Raspberry Pi 3 Model B（Raspberry Pi OS **Lite**）上に常駐し、

- **平日 10:00〜16:00 の毎正時** に NHK 風の時報「ポ・ポ・ポ・ポーン」＋「午前10時をお知らせしました。」
- その直後に、**ランダムで「ひとこと」または「今日の天気予報」**
- **平日 16:57** に閉館アナウンス＋「蛍の光」（2 秒フェードイン）

を無人で自動再生します。

```
09:59:57  ポ、ポ、ポ、    ← 短音 3 回（440Hz）
10:00:00              ポーン ← 長音（880Hz）が正時ちょうど
10:00:01  「午前10時をお知らせしました。」
10:00:04  「今日の滋賀の天気は、晴れ時々くもり。最高気温は27度、降水確率は20パーセントです。」
   :
16:57:00  「（閉館アナウンス）」→「蛍の光」♪
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
| **v3.0.0** | **2026/08** | **時報機能・おまけ機能を追加**し、v2.0.0 の専用機化と同時に実装。詳細は [CHANGELOG.md](CHANGELOG.md) |

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
| 時刻の読み上げ | 「午前10時をお知らせしました。」（12:00 は「正午をお知らせしました。」） |
| おまけ | 時報のあとに「ひとこと」か「天気予報」をランダム再生（10 時は必ず天気予報） |
| 閉館放送 | 平日 16:57 にアナウンス → 「蛍の光」（2 秒フェードイン） |
| 平日限定稼働 | 土日は再生しない（※祝日判定は未対応） |
| 二重再生防止 | 同一日・同一イベントは 1 回だけ。**再起動しても状態を保持** |
| 取りこぼし防止 | 起動が数十秒遅れた場合は追いかけ再生（既定 120 秒以内） |
| 自動復旧 | systemd により電源投入後に自動起動、異常終了時も再起動 |
| オフライン動作 | 天気取得に失敗しても時報・蛍の光は必ず鳴る（ひとことへ自動切替） |
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
│   ├── tts.py                 #   音声合成（3 エンジン＋キャッシュ）
│   └── weather.py             #   天気予報の取得
├── assets/                    # [Res] 音声リソース
│   ├── announce.wav           #   閉館アナウンス（VOICEVOX:ずんだもん）
│   ├── hotaru.mp3             #   蛍の光（Auld Lang Syne / Public Domain）
│   ├── quotes.json            #   「ひとこと」定義
│   ├── voice/                 #   事前生成した音声（任意）
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
| 音声合成 | Open JTalk（apt で導入・オフライン動作） |
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
python3 campus_chime.py --say "テストです"   # 任意の文言を読み上げ
python3 campus_chime.py --generate-assets  # 時報音・定型文音声を事前生成
python3 campus_chime.py --print-config     # 適用中の設定を表示
python3 campus_chime.py --dry-run --test   # 音を出さず内容だけ確認
```

ログの確認:

```bash
journalctl -u campus_chime.service -f
```

## 8. 設定変更

時刻・曜日・読み上げ文言・天気の地域などは `config.json` で変更できます（**コードの書き換えは不要**）。

```bash
cp config.example.json config.json   # 初回のみ（setup.sh が実行済み）
nano config.json
sudo systemctl restart campus_chime.service
```

よく使う項目:

| やりたいこと | 変更する項目 |
|---|---|
| 時報の時間帯を変える | `schedule.hourly.start_hour` / `end_hour` |
| 特定の時刻だけ止める | `schedule.hourly.skip_hours`（例: `[12]`） |
| 閉館放送の時刻を変える | `schedule.closing.hour` / `minute` |
| 土曜も鳴らす | `schedule.*.weekdays` に `5` を追加（月=0〜日=6） |
| 読み上げ文言を変える | `time_signal.announce_template` |
| 天気の地域を変える | `weather.jma.area_code` / `area_name` / `temp_area_name` / `label`（既定は滋賀・南部） |
| 天気の頻度を変える | `extra_segment.weather_probability`（0.0〜1.0） |
| おまけを止める | `extra_segment.enabled` を `false` |

全項目の説明は [docs/SPECIFICATION.md](docs/SPECIFICATION.md) にあります。「ひとこと」の追加・削除は `assets/quotes.json` を編集してください。

## 9. テスト

外部依存なし（ネットワーク・音声デバイス不要）で実行できます。

```bash
python3 -m unittest discover -s tests -t . -v
```

## 10. ドキュメント

| ファイル | 内容 |
|---|---|
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | 要件定義書。目的・背景・機能要件・非機能要件・将来課題 |
| [docs/SPECIFICATION.md](docs/SPECIFICATION.md) | 仕様書。設定項目・モジュール・systemd・テスト仕様 |
| [docs/SETUP.md](docs/SETUP.md) | 再構築手順書。OS 書き込みから運用開始まで |
| [docs/KNOWLEDGE_BASE.md](docs/KNOWLEDGE_BASE.md) | 運用ナレッジ・トラブルシューティング |
| [docs/DEVELOPMENT_LOG.md](docs/DEVELOPMENT_LOG.md) | 開発履歴 |
| [CHANGELOG.md](CHANGELOG.md) | 変更履歴 |

## 11. ライセンス・クレジット

- **合成音声（同梱の `announce.wav`）:** VOICEVOX:ずんだもん
- **実行時の音声合成:** [Open JTalk](https://open-jtalk.sourceforge.net/)（修正 BSD ライセンス）/ HTS Voice "nitech_jp_atr503_m001"
- **楽曲:** Auld Lang Syne（Public Domain / Copyright Free）
- **天気予報:** [気象庁](https://www.jma.go.jp/bosai/) の防災情報 JSON、または [Open-Meteo](https://open-meteo.com/)（CC BY 4.0）
- **時報音:** 本リポジトリのコードが実行時に合成（音源ファイルの同梱なし）
