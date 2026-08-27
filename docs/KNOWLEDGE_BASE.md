# 運用ナレッジ（KNOWLEDGE BASE）

日々の運用で使うコマンドと、起きたトラブルへの対処をまとめる。
**初回導入は [SETUP.md](SETUP.md)、設定項目の一覧は [SPECIFICATION.md](SPECIFICATION.md) を参照。**

---

## 1. システム概要（要約）

| 項目 | 内容 |
|---|---|
| 何をするか | 平日 10:00〜16:00 の毎正時に時報、16:57 に閉館放送 |
| どこにあるか | `/home/pi/campus-chime`（Raspberry Pi 3B / Raspberry Pi OS Lite 32bit） |
| どう動くか | `campus_chime.service`（systemd）が常駐プロセスを維持 |
| ログ | `journalctl -u campus_chime.service` |
| 設定 | `/home/pi/campus-chime/config.json`（Git 管理外） |
| ネットワーク依存 | 天気予報のみ（既定では無効の任意機能）。有効時も、切れれば時報・蛍の光は鳴る |

放送の流れ（時報）:

```
09:59:57  ポ、ポ、ポ、        ← 440Hz 短音 × 3
10:00:00              ポーン  ← 880Hz 長音（ここが正時）
10:00:01  「午前10時をお知らせしました。」
10:00:04  「ひとこと」（天気予報は任意機能。既定では無効）
```

---

## 2. よく使うコマンド

```bash
cd /home/pi/campus-chime

# 状態
sudo systemctl status campus_chime.service
sudo systemctl is-enabled campus_chime.service

# ログ
journalctl -u campus_chime.service -f                 # 追尾
journalctl -u campus_chime.service --since today      # 本日分
journalctl -u campus_chime.service -p err --since -7d # 直近 1 週間のエラー

# 予定
python3 campus_chime.py --schedule
python3 campus_chime.py --schedule 20

# 試し鳴らし
python3 campus_chime.py --test-hourly      # 時報（現在時刻）
python3 campus_chime.py --test-hourly 12   # 12 時の時報
python3 campus_chime.py --test             # 閉館放送
python3 campus_chime.py --test-all         # 両方
python3 campus_chime.py --weather          # 天気予報（既定では無効。有効化は SETUP.md 7 章「天気予報を有効にする」参照）
python3 campus_chime.py --say "テストです"

# 音を出さずに内容だけ見る
python3 campus_chime.py --test-all --dry-run

# 制御
sudo systemctl restart campus_chime.service
sudo systemctl stop campus_chime.service
sudo systemctl start campus_chime.service
```

---

## 3. 運用作業

### 3-1. 設定を変える

```bash
nano /home/pi/campus-chime/config.json
sudo systemctl restart campus_chime.service
python3 /home/pi/campus-chime/campus_chime.py --schedule
```

変更例は [SETUP.md 7 章](SETUP.md#7-設定を変える) を参照。

### 3-2. ひとことを増やす

```bash
nano /home/pi/campus-chime/assets/quotes.json
python3 campus_chime.py --test-hourly 15   # 確認（再起動不要：毎回読み直す）
```

JSON の書式を壊すと読み込みに失敗し、内蔵の予備文言に切り替わります（放送は止まりません）。編集後は上記コマンドで確認してください。

### 3-3. 更新を取り込む

```bash
cd /home/pi/campus-chime
git pull
bash scripts/setup.sh --no-apt
sudo systemctl restart campus_chime.service
```

### 3-4. 一時的に止める（休業日など）

```bash
sudo systemctl stop campus_chime.service     # その場で停止（再起動すると復活）
sudo systemctl disable --now campus_chime.service  # 自動起動も止める
```

再開:

```bash
sudo systemctl enable --now campus_chime.service
```

### 3-5. 音量を変える

```bash
alsamixer            # ↑↓ で調整、Esc
sudo alsactl store   # 保持
```

---

## 4. トラブル対処

### 4-1. 時間になっても鳴らない

順に確認します。

```bash
# ① サービスは動いているか
sudo systemctl status campus_chime.service

# ② その時刻は予定に入っているか（曜日・時間帯・skip_hours）
python3 campus_chime.py --schedule 20

# ③ その時刻のログに何が出ているか
journalctl -u campus_chime.service --since "today 09:55" --until "today 10:05" --no-pager

# ④ そもそも音が出る状態か
speaker-test -t sine -f 440 -c 2 -l 1
```

| ログに出ていること | 意味 | 対処 |
|---|---|---|
| `再生開始` は出ているが聞こえない | アプリは鳴らしている | 音量・配線・出力先（4-2） |
| `再生開始` が無い | 予定に入っていない／サービスが落ちていた | `--schedule` と `systemctl status` |
| `再生開始時刻を … 過ぎています` | 起動が遅れて追いかけ再生した | 正常（起動直後に多い） |
| `音声合成に失敗` | 読み上げだけ不可 | 4-4 |
| `天気予報を取得できませんでした` | 天気予報が無効（既定）、またはネットワーク断 | 正常動作（ひとことに切替）。有効化する場合は SETUP.md 7 章参照 |

### 4-2. 音が出ない

[SETUP.md 10 章](SETUP.md#10-音が鳴らないとき) に切り分け手順があります。要点だけ:

```bash
speaker-test -t sine -f 440 -c 2 -l 1   # OS で鳴るか
aplay -l                                # デバイスが見えるか
alsamixer                               # ミュートでないか
groups pi                               # audio グループに入っているか
```

### 4-3. 音が途切れる・カクつく

1. 電源を 5V 2.5A 以上のものに替える（最も多い原因）
2. `config.json` で `audio.mixer.buffer` を `8192` に上げる
3. 他の常駐プロセスを入れていないか確認する（本機は専用機）

```json
{ "audio": { "mixer": { "buffer": 8192 } } }
```

### 4-4. 読み上げだけ鳴らない（時報音は鳴る）

音声合成が使えていません。時報音は合成不要なので鳴ります。

```bash
which open_jtalk
ls /var/lib/mecab/dic/open-jtalk/naist-jdic
ls /usr/share/hts-voice/*/*.htsvoice

sudo apt install -y open-jtalk open-jtalk-mecab-naist-jdic hts-voice-nitech-jp-atr503-m001
python3 campus_chime.py --generate-assets
```

### 4-5. 時刻がずれている

本機は RTC 非搭載です。

```bash
timedatectl
# Time zone: Asia/Tokyo / System clock synchronized: yes を確認

sudo timedatectl set-timezone Asia/Tokyo
sudo systemctl restart systemd-timesyncd
```

同期しない場合は Wi-Fi 接続と DNS を確認してください。

### 4-6. 同じ回が 2 回鳴った

`cache/state.json` が壊れているか、書き込めていない可能性があります。

```bash
cat /home/pi/campus-chime/cache/state.json
ls -l /home/pi/campus-chime/cache/
sudo chown -R pi:pi /home/pi/campus-chime
```

### 4-7. 天気予報が古い／いつも同じ

天気予報を有効にしている場合のみ該当します（既定では無効です。SETUP.md 7 章参照）。取得結果は 60 分キャッシュされます（気象庁への負荷を避けるため）。すぐに反映したい場合はサービスを再起動してください。

```json
{ "weather": { "cache_minutes": 30 } }
```

### 4-8. サービスが再起動を繰り返す

```bash
journalctl -u campus_chime.service -n 100 --no-pager
```

`Restart=always` のため 10 秒ごとに再起動します。設定ファイルの JSON 書式エラーが典型的な原因です。

```bash
python3 -c "import json;json.load(open('/home/pi/campus-chime/config.json'))"
# エラーが出たら書式を直す。応急処置として config.json を退避すれば既定値で動く
mv config.json config.json.bak
sudo systemctl restart campus_chime.service
```

### 4-9. Wi-Fi が切れる

Pi 3B は **2.4GHz 帯のみ**対応です。5GHz の SSID には接続できません。

```bash
iwconfig wlan0
sudo iw dev wlan0 set power_save off   # 省電力による切断を防ぐ（応急）
```

天気予報（既定では無効の任意機能）以外は Wi-Fi なしでも動作するため、放送そのものは止まりません。

---

## 5. 定期点検の目安

| 頻度 | 作業 |
|---|---|
| 毎月 | 実際の正時に立ち会って放送を聴く／`journalctl -p err --since -30d` を確認 |
| 学期ごと | `sudo apt update && sudo apt upgrade`／再起動して自動復帰を確認 |
| 年 1 回 | SD カードの健全性確認（可能ならバックアップイメージを取得） |
| 都度 | 長期休業前後にスケジュール（`weekdays` / `skip_hours`）を見直す |

---

## 6. 設計上の重要な決定（背景）

保守時に「なぜこうなっているのか」で迷わないための記録。

| 決定 | 理由 |
|---|---|
| 長音の**開始**を正時に合わせる | NHK の時報と同じ作法。短音 3 回は「正時の 3 秒前から」始まる。そのため `--schedule` の「再生開始」は正時より 3 秒早い |
| 天気取得・音声合成を再生の 45 秒前に済ませる | ネットワークや合成に時間がかかっても、再生開始時刻がずれないようにするため |
| `sleep` を 30 秒ごとに分割する | RTC 非搭載機は起動直後に NTP で時刻が大きく飛ぶ。長い `sleep` では追従できない |
| 再生済みをディスクに保存する | `Restart=always` のため、クラッシュ後の再起動で同じ回が二度鳴るのを防ぐ |
| 天気の失敗をひとことで代替する | 放送が「無音」になる事態を避けるため。時報が鳴らない方が問題が大きい |
| 音声合成結果をキャッシュする | 時報の定型文は 7 種類しかない。2 回目以降は合成不要になり、CPU も安定する |
| 時報音をコードで合成する | 音源ファイルの権利関係・調達を不要にし、周波数や長さを設定で変えられるようにするため |
| pygame 以外に外部コマンド再生を用意する | Lite 環境で pygame が壊れても放送を継続できるようにするため |
| `config.json` を Git 管理外にする | 現地で編集したファイルが `git pull` と衝突しないようにするため |

---

## 7. 廃止された仕組み（歴史）

| 廃止物 | 経緯 |
|---|---|
| `weather.py`（常駐天気ボット） | v1.x で同居していた別プロセス。音声デバイスを奪い合うため、v3.0.0 で OS ごと廃止。天気予報は本アプリが必要時に API を呼ぶ形で再実装した |
| `pkill -f weather.py` | 上記の競合を力技で回避していた処理。競合源が消えたため削除 |
| `docs/LEGACY_SYSTEM_SHUTDOWN.md` | `weather.py` の自動起動を止める手順書。OS を入れ替えるため不要になり削除 |
| `~/steam5pm` という設置パス | v1.x のドキュメント表記。systemd ユニットと食い違い、導入失敗の原因だった。`/home/pi/campus-chime` に統一 |
| 1 秒ごとの時刻ポーリング | 次イベントまでの分割待機に置き換え（CPU 負荷と時刻補正追従の両立） |
