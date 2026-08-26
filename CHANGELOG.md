# 変更履歴

本ファイルの記法は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に、
バージョン番号は [セマンティック バージョニング](https://semver.org/lang/ja/) に従う。

## [3.0.0] - 2026-08-26

Lite 移行による専用機化（v2.0.0 として設計されていた内容）と、時報機能の追加を同時に実施した版。

### 追加

- **時報機能**: 平日 10:00〜16:00 の毎正時に NHK 風の時報を再生する
  - 440Hz の短音 3 回 ＋ 880Hz の長音 1 回。**長音の先頭が正時ちょうど**に鳴る
  - 時報音は実行時にコードで合成する（音源ファイルの同梱・調達が不要）
  - 「午前10時をお知らせしました。」の読み上げ（12 時は「正午をお知らせしました。」）
- **おまけ機能**: 時報のあとに「ひとこと」または「天気予報」をランダム再生
  - 「ひとこと」は `assets/quotes.json` で編集可能（全時刻共通 40 件＋時刻別 17 件を同梱）
  - 直近に使ったひとことは選ばれない
  - 天気予報は気象庁 JSON（既定）または Open-Meteo から取得。いずれも API キー不要
  - 取得失敗時は自動的に「ひとこと」へ切り替わる
- **音声合成（TTS）**: Open JTalk によるオフライン合成。VOICEVOX ENGINE と事前生成音声にも対応
  - 合成結果を `cache/tts/` にキャッシュし、同じ文言は 2 回目以降合成しない
  - `scripts/generate_voicevox.py` で PC 側から定型文を作り置きできる
- **設定ファイル `config.json`**: 時刻・曜日・読み上げ文言・地域などをコード改変なしで変更可能
  - 雛形 `config.example.json` を同梱。`git pull` と衝突しないよう `config.json` は Git 管理外
- **再生状態の永続化 (`cache/state.json`)**: 再起動をまたいで二重再生を防止
- **追いかけ再生**: 起動が遅れた場合、既定 120 秒以内なら当該回を再生
- **再生バックエンドの多重化**: pygame → 外部コマンド（`aplay`/`mpg123`）→ mock の順に自動選択
- **CLI の拡充**: `--test-hourly` / `--test-all` / `--weather` / `--say` / `--schedule` / `--generate-assets` / `--print-config` / `--dry-run` / `--backend` / `--log-level` / `--config` / `--version`
- **ユニットテスト 190 件超**（ネットワーク・音声デバイス不要）と GitHub Actions による CI
- **導入スクリプト `scripts/setup.sh`**（冪等）
- **ドキュメント**: `docs/SPECIFICATION.md`、`docs/SETUP.md`、`CHANGELOG.md`、Issue テンプレート

### 変更

- OS 前提を Raspberry Pi OS Desktop → **Lite 32bit**（専用機化）
- 単一ファイル `campus_chime.py` → `chime/` パッケージ＋薄いエントリポイントに再構成
- スケジューリングを 1 秒ポーリングから「次イベントまでの分割待機」に変更
  - 待機中の CPU 使用率を低減し、NTP による時刻補正にも追従する
- `mixer.init()` に周波数・フォーマット・チャンネル・**バッファ 4096** を明示指定（音飛び対策）
- 再生終了後に `pygame.mixer.quit()` を実行し、オーディオデバイスを解放
- systemd ユニット: `time-sync.target` 待機、`SDL_AUDIODRIVER=alsa`、`PYTHONUNBUFFERED=1`、`Restart=always`、`SupplementaryGroups=audio`、最低限の保護設定を追加
- 設置パスを `/home/pi/campus-chime` に統一（README の `~/steam5pm` 表記を全廃）
- ログのタイムスタンプを設定タイムゾーンで表示
- `SIGTERM` / `SIGINT` で待機を打ち切り、`systemctl stop` に即応するように

### 削除

- `weather.py` 競合排除機能（`kill_conflict_process()` / `CONFLICT_APP`）
  - 競合元を OS ごと廃止したため不要。`pkill` の副作用リスクも解消
- systemd ユニットの `audio.target`（標準ターゲットとして存在しない）
- 未使用変数 `is_time` と、実装（16:57）と矛盾するコメント（17:00 表記）
- `docs/LEGACY_SYSTEM_SHUTDOWN.md`（OS 入れ替えにより不要）

### 修正

- ドキュメントと systemd ユニットのパス不一致により、手順どおり導入するとサービスが起動失敗した問題
- 再生のたびに `mixer.init()` を呼びながら `quit()` していなかった問題
- サービス再起動により「本日再生済み」の記録が失われ、同じ回が再度鳴りうる問題

## [1.2.0] - 2025-11-30

### 追加

- 閉館アナウンス（`assets/announce.wav`）の再生と、蛍の光への順次再生
- 平日限定稼働（月〜金）
- `--test` による即時再生

## [1.0.0] - 2025-11-30

- 初版。定刻に「蛍の光」を再生する常駐スクリプトと systemd ユニット

[3.0.0]: https://github.com/hiroyuki-rdx/chime-5pm/releases/tag/v3.0.0
