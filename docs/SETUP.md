# 再構築手順書（SETUP）

SD カードへの OS 書き込みから、放送が自動で流れる状態になるまでの全手順。
**上から順に実行すれば完了します。** 所要時間はおよそ 60〜90 分（大半は OS の書き込みと `apt upgrade` の待ち時間）。

---

## 0. 用意するもの

| 品目 | 備考 |
|---|---|
| Raspberry Pi 3 Model B | 本体 |
| microSD カード（8GB 以上） | Lite なら 8GB で足りる。16GB 推奨 |
| USB 電源（5V 2.5A 以上） | 電力不足は音の途切れや再起動の原因になる |
| スピーカー | 3.5mm ジャック接続 または USB |
| 作業用 PC | Raspberry Pi Imager を動かす。SSH クライアントも使う |
| Wi-Fi（**2.4GHz 帯**） | Pi 3B は 5GHz に非対応。SSID とパスワードを控えておく |

> **注意:** SD カードは中身が消去されます。旧環境から引き継ぐデータがないことを確認してください（引き継ぐ必要はありません）。

---

## 1. OS を書き込む

### 1-1. Raspberry Pi Imager を起動

作業用 PC に [Raspberry Pi Imager](https://www.raspberrypi.com/software/) を入れて起動します。

### 1-2. OS を選ぶ

`OS を選ぶ` → `Raspberry Pi OS (other)` → **`Raspberry Pi OS Lite (32-bit)`**

> **Lite を選ぶ理由:** デスクトップ環境を載せないことで、RAM 1GB の本機に余裕が生まれ、音飛びの原因だったリソース不足を根本から避けられます。
> **32bit を選ぶ理由:** 64bit 版はメモリ消費が相対的に大きく、RAM 1GB の本機には不利なためです。

### 1-3. ストレージを選ぶ

microSD カードを選択します。

### 1-4. OS のカスタマイズ（**最重要**）

`次へ` → `設定を編集する` を押し、次のとおり入力します。

**一般タブ**

| 項目 | 設定値 |
|---|---|
| ホスト名 | `campus-chime`（任意。あとで `ssh pi@campus-chime.local` で接続できます） |
| ユーザー名 | **`pi`** ← **必ず `pi` にしてください** |
| パスワード | 任意（忘れないもの） |
| Wi-Fi SSID / パスワード | **2.4GHz 帯の** SSID |
| Wi-Fi の国 | `JP` |
| ロケール設定 タイムゾーン | **`Asia/Tokyo`** |
| キーボードレイアウト | `jp` |

> **ユーザー名が `pi` でなければならない理由:** systemd ユニットと設置パスが `/home/pi/campus-chime` を前提にしているためです。別のユーザー名にする場合は `campus_chime.service` の `User` / `Group` / `WorkingDirectory` / `ExecStart` をすべて書き換える必要があります。

**サービスタブ**

- **`SSH を有効化する`** にチェック → `パスワード認証を使う`

**保存** → `はい`（設定を適用）→ 書き込み開始。10〜20 分ほどかかります。

---

## 2. 初回起動と接続

1. 書き込んだ microSD を Pi に挿し、スピーカーと電源をつなぐ
2. 1〜2 分待つ（初回起動はファイルシステム拡張のため時間がかかります）
3. 作業用 PC から SSH で接続

```bash
ssh pi@campus-chime.local
# 名前で引けない場合は、ルーターの管理画面などで IP を調べて
# ssh pi@192.168.x.x
```

接続できたら OS を最新にします。

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

再起動後、もう一度 SSH で接続してください。

---

## 3. 時刻を確認する

本機は **RTC（時計用電池）を搭載していません**。電源を切ると時刻を忘れ、起動のたびにネットワーク越しに時刻を取り直します。時報を出す以上、ここは必ず確認してください。

```bash
timedatectl
```

次の 2 行を確認します。

```
                Time zone: Asia/Tokyo (JST, +0900)
System clock synchronized: yes
```

- タイムゾーンが違う場合: `sudo timedatectl set-timezone Asia/Tokyo`
- `synchronized: no` の場合: ネットワーク接続を確認し、1〜2 分待って再確認

---

## 4. 音を出せるようにする

### 4-1. 出力先を選ぶ

**3.5mm ジャックのスピーカーを使う場合**

```bash
sudo raspi-config nonint do_audio 1
```

**USB スピーカーを使う場合**

まず認識されているカード番号を調べます。

```bash
aplay -l
```

```
card 0: Headphones [bcm2835 Headphones], ...
card 1: Device [USB Audio Device], ...     ← これが USB スピーカー
```

USB 側（この例では `card 1`）を既定にします。

```bash
sudo tee /etc/asound.conf >/dev/null <<'EOF'
defaults.pcm.card 1
defaults.ctl.card 1
EOF
```

### 4-2. 音量を上げる

```bash
alsamixer          # ↑↓ で音量、M でミュート解除、Esc で終了
sudo alsactl store # 再起動後も保持する
```

コマンドで済ませる場合（コントロール名は `amixer scontrols` で確認）:

```bash
amixer sset 'PCM' 90%
sudo alsactl store
```

### 4-3. 実際に鳴らして確認

```bash
speaker-test -t sine -f 440 -c 2 -l 1
```

「ピー」という音が出れば OK です（出ない場合は本書末尾の「音が鳴らないとき」へ）。

---

## 5. 本体を導入する

```bash
git clone https://github.com/hiroyuki-rdx/chime-5pm.git /home/pi/campus-chime
cd /home/pi/campus-chime
bash scripts/setup.sh
```

`scripts/setup.sh` が次をまとめて行います。

1. 設置パスの確認（`/home/pi/campus-chime` でなければ警告）
2. 依存パッケージの導入
   （`python3-pygame` / `open-jtalk` / `open-jtalk-mecab-naist-jdic` / `hts-voice-nitech-jp-atr503-m001` / `alsa-utils` / `mpg123`）
3. タイムゾーンと NTP 同期の確認
4. `config.example.json` から `config.json` を作成（既にあれば触りません）
5. 時報音（`assets/generated/time_signal.wav`）と時刻アナウンス音声の生成
6. 予定表の表示
7. systemd への登録・有効化・起動

**このスクリプトは何度実行しても安全です**（`config.json` を上書きしません）。

---

## 6. 動作を確認する

### 6-1. その場で鳴らしてみる

```bash
cd /home/pi/campus-chime

# 時報（ポ・ポ・ポ・ポーン → 時刻 → ひとこと or 天気予報）
python3 campus_chime.py --test-hourly

# 12 時の時報（「正午をお知らせしました。」）
python3 campus_chime.py --test-hourly 12

# 閉館放送（アナウンス → 蛍の光）
python3 campus_chime.py --test

# 天気予報だけ確認
python3 campus_chime.py --weather
```

チェックポイント:

- [ ] 短音が 3 回、続いて長めの音が 1 回鳴る
- [ ] 「午前◯時をお知らせしました。」と読み上げられる
- [ ] そのあと「ひとこと」か「天気予報」が流れる
- [ ] 蛍の光がだんだん大きくなる（2 秒フェードイン）
- [ ] **音飛び・ぶつ切れがない**

### 6-2. サービスの状態を見る

```bash
sudo systemctl status campus_chime.service
```

`Active: active (running)` になっていれば常駐しています。

### 6-3. 予定を確認する

```bash
python3 campus_chime.py --schedule
```

```
現在時刻: 2026-08-26 20:07:15 JST
次回以降の予定:
  - 時報 2026-08-27 10:00:00（再生開始 09:59:57）
  - 時報 2026-08-27 11:00:00（再生開始 10:59:57）
  ...
  - 閉館放送 2026-08-27 16:57:00（再生開始 16:57:00）
```

「再生開始」が正時より 3 秒前になっているのは、**長音「ポーン」が正時ちょうどに鳴る**ようにするためです。

### 6-4. ログを見る

```bash
journalctl -u campus_chime.service -f
```

`Ctrl+C` で終了します。

### 6-5. 再起動しても動くか確認する

```bash
sudo reboot
# 再接続して
sudo systemctl status campus_chime.service
```

---

## 7. 設定を変える

時刻・曜日・読み上げ文言・天気の地域は **`config.json` の編集だけ**で変更できます（コードを触る必要はありません）。

```bash
cd /home/pi/campus-chime
nano config.json
sudo systemctl restart campus_chime.service
python3 campus_chime.py --schedule   # 反映されたか確認
```

> `config.json` は Git 管理外です。`git pull` で更新しても、現地の設定は消えません。

### よくある変更

**時報の時間帯を変える（例: 9 時〜17 時）**

```json
{ "schedule": { "hourly": { "start_hour": 9, "end_hour": 17 } } }
```

**お昼の時報だけ止める**

```json
{ "schedule": { "hourly": { "skip_hours": [12] } } }
```

**閉館放送の時刻を変える（例: 17:00）**

```json
{ "schedule": { "closing": { "hour": 17, "minute": 0 } } }
```

**土曜日も鳴らす**（月=0 / 火=1 / 水=2 / 木=3 / 金=4 / 土=5 / 日=6）

```json
{ "schedule": {
    "hourly":  { "weekdays": [0, 1, 2, 3, 4, 5] },
    "closing": { "weekdays": [0, 1, 2, 3, 4, 5] } } }
```

**読み上げ文言を変える**

```json
{ "time_signal": {
    "announce_template": "ただいま{period}{hour}時です。",
    "noon_template": "ただいま正午です。" } }
```

**天気予報の地域を変える**

気象庁の地域コードは <https://www.jma.go.jp/bosai/common/const/area.json> の `offices` から探せます（例: 大阪府 `270000`、愛知県 `230000`、福岡県 `400000`）。

```json
{ "weather": {
    "jma": { "area_code": "270000", "area_name": "大阪府", "label": "大阪" } } }
```

緯度経度で細かく指定したい場合は Open-Meteo に切り替えます。

```json
{ "weather": {
    "provider": "open_meteo",
    "open_meteo": { "latitude": 34.6937, "longitude": 135.5023, "label": "大阪" } } }
```

**天気予報の頻度を変える**（`0.0` = 常にひとこと、`1.0` = 常に天気予報）

```json
{ "extra_segment": { "weather_probability": 0.6 } }
```

**おまけを止める（時報だけにする）**

```json
{ "extra_segment": { "enabled": false } }
```

**ひとことを追加・削除する**

`assets/quotes.json` を編集します。`general` は全時刻共通、`by_hour` は指定時刻のみ候補に加わります。

```bash
nano assets/quotes.json
python3 campus_chime.py --test-hourly 15   # 確認
```

---

## 8. 声を変える（任意）

既定では Open JTalk の音声（男性・機械的）で読み上げます。VOICEVOX:ずんだもんの声に揃えたい場合は、**PC 側で音声を作り置き**します（VOICEVOX ENGINE は Pi 3B 上で常時動かすには重すぎるため）。

1. PC で VOICEVOX を起動する（エンジンが `http://127.0.0.1:50021` で待ち受けます）
2. PC 側でリポジトリを clone し、次を実行

```bash
python3 scripts/generate_voicevox.py --include-quotes
```

3. 生成された `assets/voice/` を commit して push
4. Pi 側で反映

```bash
cd /home/pi/campus-chime
git pull
sudo systemctl restart campus_chime.service
python3 campus_chime.py --test-hourly
```

事前生成された文言はそのまま使われ、無い文言（天気予報など）だけ Open JTalk が合成します。

---

## 9. 更新のしかた

```bash
cd /home/pi/campus-chime
git pull
bash scripts/setup.sh --no-apt      # 設定の追加分と音源を反映
sudo systemctl restart campus_chime.service
```

---

## 10. 音が鳴らないとき

上から順に切り分けてください。

### 10-1. OS レベルで音が出るか

```bash
speaker-test -t sine -f 440 -c 2 -l 1
```

**鳴らない場合** → アプリではなく OS 側の問題です。

```bash
aplay -l                     # デバイスが見えているか
alsamixer                    # ミュート（MM 表示）になっていないか
grep dtparam=audio /boot/firmware/config.txt   # dtparam=audio=on になっているか
```

`dtparam=audio=on` が無い／コメントアウトされている場合は追記して再起動します。

### 10-2. アプリからは鳴るか

```bash
cd /home/pi/campus-chime
python3 campus_chime.py --test-hourly --log-level DEBUG
```

ログの `再生バックエンド:` を確認します。

| 表示 | 意味 | 対処 |
|---|---|---|
| `pygame` | 正常 | — |
| `command` | pygame が入っていない | `sudo apt install -y python3-pygame` |
| `mock` | 再生手段が無い／開発環境と誤判定 | 上記に加え `sudo apt install -y alsa-utils mpg123` |

### 10-3. 手動では鳴るがサービスでは鳴らない

サービスは `pi` ユーザーで動きます。音声デバイスへの権限を確認してください。

```bash
groups pi                    # audio が含まれているか
sudo usermod -aG audio pi    # 含まれていなければ追加
sudo systemctl restart campus_chime.service
```

ログも確認します。

```bash
journalctl -u campus_chime.service -n 50 --no-pager
```

### 10-4. 読み上げだけ鳴らない（時報音は鳴る）

音声合成が使えていません。

```bash
which open_jtalk
ls /var/lib/mecab/dic/open-jtalk/naist-jdic
ls /usr/share/hts-voice/*/*.htsvoice
```

いずれかが無ければ導入します。

```bash
sudo apt install -y open-jtalk open-jtalk-mecab-naist-jdic hts-voice-nitech-jp-atr503-m001
python3 campus_chime.py --generate-assets
```

### 10-5. 天気予報だけ流れない

これは**異常ではありません**。ネットワークが切れている、または気象庁側が応答しない場合、自動的に「ひとこと」へ切り替わります（時報は必ず鳴ります）。

切り分け:

```bash
python3 campus_chime.py --weather
ping -c 3 www.jma.go.jp
```

---

## 11. 音が途切れる・カクつくとき

1. **電源を疑う** — 5V 2.5A 以上の電源を使ってください。電圧不足はまず音に出ます
2. **バッファを増やす** — `config.json` に次を追加して再起動

```json
{ "audio": { "mixer": { "buffer": 8192 } } }
```

3. **余計な常駐を止める** — 本機はチャイム専用機です。他の常駐プロセスを入れないでください

---

## 12. 導入完了チェックリスト

- [ ] `timedatectl` が `Asia/Tokyo` かつ `System clock synchronized: yes`
- [ ] `speaker-test` で音が出る
- [ ] `python3 campus_chime.py --test-hourly` で時報・読み上げ・おまけが鳴る
- [ ] `python3 campus_chime.py --test` で閉館アナウンスと蛍の光が鳴る
- [ ] 音飛び・カクつきがない
- [ ] `sudo systemctl status campus_chime.service` が `active (running)`
- [ ] `sudo systemctl is-enabled campus_chime.service` が `enabled`
- [ ] 再起動後も自動で `active (running)` になる
- [ ] `python3 campus_chime.py --schedule` に翌営業日の予定が並ぶ
- [ ] スピーカーの音量が実際の運用位置で適切
- [ ] 実際の正時（例: 14:00）に立ち会って放送を確認した

---

## 13. 困ったときの参照先

- 運用中のトラブルと対処: [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)
- 設定項目の全一覧: [SPECIFICATION.md](SPECIFICATION.md) 3 章
- なぜこの構成なのか: [REQUIREMENTS.md](REQUIREMENTS.md)
