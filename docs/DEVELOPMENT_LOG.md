# Development Log

## 2025-11-30

- **Git/GitHub Setup**:
  - Initialized Git repository (`git init`).
  - Renamed default branch to `main`.
  - Added remote origin `https://github.com/hiroyuki-rdx/chime-5pm.git`.
  - Created this log file.
  - Committed initial files.
- **GitHub Authentication Attempt**:
  - Attempted to install GitHub CLI (`gh`) via `apt`.
  - Failed: Package not found in default repositories.
  - Action: Providing official installation script for Ubuntu.
- **Project Initialization**:
  - Created Requirements Definition (`docs/REQUIREMENTS.md`).
  - Created Knowledge Base (`docs/KNOWLEDGE_BASE.md`).
  - Confirmed `docs/requirements.txt` includes `pygame`.
  - Started implementation of `main.py` (Environment detection logic).
- **Core Feature Implementation**:
  - Implemented `ChimePlayer` class in `main.py`.
  - Added `play` method with environment detection (Mock vs Production).
  - Implemented fade-in logic (2000ms) using `pygame.mixer`.
  - Implemented double playback prevention using date checking.
- **Service Configuration**:
  - Created `chime.service` for systemd integration.
  - Updated Knowledge Base with deployment instructions.
- **Refactoring**:
  - Reorganized project structure for scalability.
  - Moved source code to `src/` directory (`src/config.py`, `src/environment.py`, `src/player.py`).
  - Moved assets to `assets/` directory.
  - Renamed `main.py` to `run.py`.
  - Renamed `docs/weahtersyskill.txt` to `docs/LEGACY_SYSTEM_SHUTDOWN.md`.
  - Updated `chime.service` path.
- **v1.2.0 Update (Announcements)**:
  - Updated `docs/REQUIREMENTS.md`.
  - Renamed/Moved assets: `001_...wav` -> `assets/announce.wav`, `auld_lang_syne.mp3` -> `assets/hotaru.mp3`.
  - Updated `src/config.py` with new file paths.
  - Updated `src/player.py` to implement sequential playback (Announce -> Wait -> Chime with Fade-in).
  - Updated `run.py` to ensure weekday-only execution (Mon-Fri).
- **Technical Specifications v1 Implementation**:
  - Flattened project structure based on Technical Specifications.
  - Consolidated code into single `campus_chime.py`.
  - Renamed `chime.service` to `campus_chime.service`.
  - Moved `requirements.txt` to root.
  - Removed `src/` directory and `run.py`.

## 2026-08-26 — v3.0.0（時報機能追加・Lite 移行・専用機化）

要望:「ラズパイ 3B に軽い OS を入れて、10 時〜16 時に NHK 風の時報を鳴らし、
17 時近くに蛍の光を流したい。時報のあとにランダムで一言か天気予報も。
あわせてコード修正・GitHub 整理・セットアップの明確化を」

### 設計判断

- **時報音は合成する**: 音源ファイルを調達せず、`wave` / `math` / `struct` だけで生成。
  権利関係が発生せず、周波数・長さを設定で変えられる。
- **長音の開始を正時に合わせる**: NHK の作法に合わせ、短音 3 回は正時の 3 秒前から。
  スケジューラは「正時 − 3 秒」に再生を開始する。
- **準備と再生を分離**: 天気取得・音声合成は再生の 45 秒前に済ませ、再生開始時刻がぶれないようにした。
- **音声合成は 3 段フォールバック**: 事前生成 → VOICEVOX ENGINE → Open JTalk。
  VOICEVOX は Pi 3B には重すぎるため、PC 側で作り置きする導線（`scripts/generate_voicevox.py`）を用意。
- **天気は「おまけ」に留める**: 取得失敗時はひとことへ切り替え、放送本体は絶対に止めない。
  v1.x の `weather.py`（常駐プロセス）は復活させず、必要時に API を呼ぶだけの実装とした。
- **設定の外部化**: 時刻・文言・地域を `config.json` へ。運用担当者がコードを触らずに変更できる。
  `config.json` は Git 管理外とし、`git pull` と衝突しないようにした。
- **状態の永続化**: `Restart=always` との組み合わせで二重再生が起こりうるため、`cache/state.json` に記録。

### 実装

- `chime/` パッケージへ再構成（`app` / `audio` / `cli` / `config` / `env` / `quotes` /
  `scheduler` / `sequence` / `state` / `timesignal` / `tts` / `weather`）
- `campus_chime.py` は `chime.cli.run()` を呼ぶだけのエントリポイントに
- 1 秒ポーリング → 次イベントまでの分割待機（30 秒刻み）に変更
- 再生バックエンドを 3 種類（pygame / 外部コマンド / mock）にし自動選択
- ユニットテスト 190 件超を追加（ネットワーク・音声デバイス不要）
- GitHub Actions（Python 3.9 / 3.11 / 3.13）で CI を構成
- `scripts/setup.sh`（冪等な導入スクリプト）、`scripts/dump_example_config.py` を追加

### GitHub 整理

- `docs/LEGACY_SYSTEM_SHUTDOWN.md` を削除（OS 入れ替えにより不要）
- `CHANGELOG.md`、Issue テンプレート、CI ワークフローを追加
- `.gitignore` を整理（`config.json` / `cache/` / `assets/generated/` を除外）
- README を全面改訂し、経緯・機能・構成・設定変更の導線を 1 か所に集約
- `docs/SETUP.md` を新規作成し、OS 書き込みから運用開始までを 1 本の手順に

### 検証

- `python3 -m unittest discover -s tests -t .` → 全件成功
- Open JTalk による実際の音声合成（7 つの時刻定型文）と時報音生成を実行して確認
- 天気予報は気象庁 JSON の実形式を fixture 化して解析を検証
  （開発環境からは jma.go.jp へ到達できないため、実機での `--weather` 確認が必要）
