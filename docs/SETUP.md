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

# 時報（ポ・ポ・ポ・ポーン → 時刻 → ひとこと）
python3 campus_chime.py --test-hourly

# 12 時の時報（「正午をお知らせしたのだ。」）
python3 campus_chime.py --test-hourly 12

# 閉館放送（アナウンス → 蛍の光）
python3 campus_chime.py --test
```

チェックポイント:

- [ ] 短音が 3 回、続いて長めの音が 1 回鳴る
- [ ] 「午前◯時をお知らせしたのだ。」と読み上げられる
- [ ] 10/12/14/16 時は、そのあと大津と京都の**現在の**天気と気温が流れる（11/13/15 時は流れない）
- [ ] そのあと「ひとこと」が流れる（天気の有無に関わらず毎回）
- [ ] **すべてずんだもんの声**である（男性の声が混ざったら、その文言の作り置きが外れている。8 章参照）
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

> **`config.json` には変えたい項目だけを書いてください。** 全項目を丸ごと書き写すと、その時点の既定値が固定され、以後の既定値の変更が届かなくなります（10 章「10-7. 読み上げが男性音声になる」で解説する不具合の原因そのものです）。全項目を見渡したいときは `config.example.json` を参照し、そこから必要な行だけ `config.json` に写してください。

### よくある変更

**時報の時間帯を変える（例: 9 時〜17 時）**

```json
{ "schedule": { "hourly": { "start_hour": 9, "end_hour": 17 } } }
```

（9 時は Open JTalk が誤読しやすい時刻の一つですが、既定の `hour_readings` で正しく読めるよう対策済みです）

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

`{hour}` は数字のみ（例: `4`）のプレースホルダです。Open JTalk は「4時」を「よんじ」、「7時」を「ななじ」、「9時」を「きゅうじ」、「0時」を「ぜろじ」と誤読します（正しくは よじ／しちじ／くじ／れいじ）。誤読対策込みの読みが欲しい場合は `{hour}` の代わりに `{hour_reading}` を使ってください（既定のテンプレートはこちらを使っています）。

```json
{ "time_signal": {
    "announce_template": "ただいま{period}{hour_reading}です。" } }
```

対象の時刻を増やしたい場合は `hour_readings` に追加します（キーは 12 時間表記の「時」、文字列）。

```json
{ "time_signal": { "hour_readings": { "0": "れいじ", "4": "よじ", "7": "しちじ", "9": "くじ", "1": "いちじ" } } }
```

**天気予報の時刻を変える**

既定では **10 / 12 / 14 / 16 時**（2 時間おき）に、大津と京都の**現在の**天気と気温を読み上げます。ここに無い時刻（11 / 13 / 15 時）は時報と「ひとこと」だけになり、天気 API も呼びません。

毎正時に流したい場合:

```json
{ "extra_segment": { "weather_hours": [10, 11, 12, 13, 14, 15, 16] } }
```

天気を一切流さない場合（「ひとこと」だけにする）:

```json
{ "extra_segment": { "weather_hours": [] } }
```

**閉館放送（16:57）には、ここに何を書いても天気は付きません。** 閉館放送は時報とは別の経路で組み立てられるためです。

**天気予報の地域を変える**

既定は大津と京都の 2 か所です。地点は上から順に読み上げられます。

```json
{ "weather": { "open_meteo": { "locations": [
    { "label": "大津", "latitude": 35.0045, "longitude": 135.8686 }
] } } }
```

1 か所に減らせば放送が約 6 秒短くなります。`label` はそのまま読み上げられるので、読み間違えない表記にしてください（漢字の読みが不安なら、ひらがなで書くのが確実です）。

**地点や読み上げ項目を変えたら、必ず音声を作り置きし直してください**（8 章）。作り置きに無い文言は実行時に Open JTalk が合成するため、**そこだけ別人の男性音声になります**。

```bash
python3 scripts/generate_voicevox.py --include-quotes --prune
```

**読み上げる項目を変える**

既定では「現在の天気」と「現在の気温」を読みます。文ごとにテンプレートが分かれており、空文字列にするとその文を読みません。

```json
{ "weather": {
    "sentence_weather":  "今の{label}の天気は{weather}なのだ。",
    "sentence_temp":     "気温は{temp}度なのだ。",
    "sentence_temp_max": "",
    "sentence_pop":      ""
} }
```

今日の最高気温や降水確率も読みたい場合は、空文字列の項目に文言を入れます。

```json
{ "weather": {
    "sentence_temp_max": "最高気温は{temp_max}度なのだ。",
    "sentence_pop":      "降水確率は{pop}パーセントなのだ。"
} }
```

**このとき必ず作り置きを再生成してください。** 空文字列のあいだは事前生成の対象にならないため、有効にしただけでは声が揃いません。

なお降水確率は現況が存在せず、今日 1 日の予報です。現在の天気・気温と混ぜて読むことになる点に注意してください。

**気象庁（jma）に切り替える場合の注意**

既定の取得先が Open-Meteo なのは、**読み上げ文を作り置きできるようにするため**です。気象庁の予報文は自由文で語彙が閉じないため事前生成できず、天気だけ Open JTalk の男性音声になります。

そのうえで気象庁に切り替える場合は、次の 3 点を承知してください。

1. **天気の読み上げだけ男性音声になります**（作り置きが効かないため）
2. 気象庁に現況が無いため `sentence_temp`（現在の気温）が出力されず、**天気 1 文だけ**になります
3. `sentence_weather` の「今の」が実態（今日 1 日の予報）と食い違います

切り替えるなら、文言も予報の言い回しに戻してください。

```json
{ "weather": {
    "provider": "jma",
    "jma": { "area_name": "北部", "temp_area_name": "彦根", "label": "滋賀" },
    "sentence_weather":  "今日の{label}の天気は{weather}なのだ。",
    "sentence_temp":     "",
    "sentence_temp_max": "最高気温は{temp_max}度なのだ。",
    "sentence_pop":      "降水確率は{pop}パーセントなのだ。"
} }
```

滋賀県の一次細分区域は「南部」（大津・草津・近江八幡など）と「北部」（彦根・長浜・米原・高島など）です。気温は細分区域ではなく観測地点名（「大津」「彦根」）で指定するため、`area_name` と `temp_area_name` を別々に書く必要があります。他の都道府県のコードは <https://www.jma.go.jp/bosai/common/const/area.json> の `offices` から探します。

いずれの設定も本リポジトリでは実通信で検証していません。変えたら必ず実機で確認してください。

```bash
python3 campus_chime.py --weather
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

読み上げる文言はすべて VOICEVOX:ずんだもんの声で作り置きしてリポジトリに同梱してあり、Pi 上では再生するだけです（VOICEVOX ENGINE は Pi 3B 上で常時動かすには重すぎるため）。

**文言を変えたとき**（語尾、ひとこと、天気の地点や読み上げ項目など）は、**PC 側で作り置きを作り直す必要があります**。作り直さないと、変えた文言だけ実行時に Open JTalk が合成し、そこだけ男性・機械的な音声になります。

1. PC で VOICEVOX ENGINE を起動する（エンジンが `http://127.0.0.1:50021` で待ち受けます）。Docker で起動する場合は例えば次のようにします。

```bash
docker run -d --name voicevox -p 50021:50021 voicevox/voicevox_engine:cpu-latest
```

`-d` はバックグラウンド実行の指定で、付けないとターミナルが占有され続けて `curl` や生成スクリプトを実行できません。また `--rm` は付けません。付けると停止時にコンテナごと削除され、`docker start` で再開できなくなるためです。イメージのタグ（`cpu-latest` の部分）は環境によって異なることがあるため、`docker images` で手元にあるタグを確認して読み替えてください。

次回以降は次で再開できます。

```bash
docker start voicevox
```

止めるときは次のとおりです（`--rm` を付けていないのでコンテナは消えません）。

```bash
docker stop voicevox
```

起動直後は ONNX モデルの読み込みのため、`/version` がしばらく応答しないことがあります。次のコマンドで `Application startup complete.` が表示されるまで待ってから疎通確認してください（`Ctrl+C` でログの表示を抜けてもコンテナ自体は止まりません）。

```bash
docker logs -f voicevox
```

疎通確認は次のコマンドでできます。

```bash
curl -s http://127.0.0.1:50021/version
```

2. PC 側でリポジトリを clone し、次を実行

```bash
python3 scripts/generate_voicevox.py --include-quotes --prune
```

生成されるのは **166 件**（時刻アナウンス 7 ＋ ひとこと 57 ＋ 天気 102）です。数分かかります。

`--prune` は、**現在の文言集合に無くなった古い音声と manifest のエントリを削除**します。manifest はマージ方式で書き戻すため、文言（語尾や地名、読み上げる項目）を変えると古いファイルが残り続けます。付けない場合も残存件数は表示されるので、消す前に確認できます。

`scripts/generate_voicevox.py` は既定でエンジンの起動を最大 90 秒待つため（`--wait` で変更可能）、起動直後で `/version` が応答しない状態でもそのまま実行して構いません。90 秒待っても応答しない場合はエラーメッセージの案内に従って確認してください。

Docker Desktop（Windows）で VOICEVOX ENGINE を動かしている場合、WSL2 側から `127.0.0.1` では届かないことがあります。その場合は `--base-url` で Windows ホスト側の IP を指定してください。

```bash
python3 scripts/generate_voicevox.py --include-quotes --base-url http://<ホストのIP>:50021
```

生成した音声を WSL2 側でその場で試聴する場合は、既定では音が鳴りません。`--backend pygame` を明示してください（詳しくは「10-6. WSL2 で試すと音が鳴らない」参照）。

3. 生成された `assets/voice/` を commit して push
4. Pi 側で反映

```bash
cd /home/pi/campus-chime
git pull
sudo systemctl restart campus_chime.service
python3 campus_chime.py --test-hourly
```

事前生成された文言はそのまま使われます。**作り置きに無い文言だけ Open JTalk が合成する**ため、そこだけ別人の男性音声になります。

天気予報も既定の設定（`provider: "open_meteo"`）であれば全パターンが作り置きされるため、**Pi 上では音声合成が一切発生しません**。男性の声が混ざって聞こえたら、文言を変えたあとに作り置きを作り直していないか、`provider` を `jma` に変えたかのどちらかです（7 章「気象庁（jma）に切り替える場合の注意」参照）。

---

## 9. 更新のしかた

**更新の前に、読み上げる文言を変えたかどうかを確認してください。** ここで経路が分かれます。

| 変えたもの | 必要な作業 |
|---|---|
| コード・設定値（時刻、曜日、音量など） | **A だけ**（Pi で pull して再起動） |
| **読み上げる文言** | **B → A の順**（PC で音声を作り直してから Pi へ） |

「読み上げる文言を変えた」には次が含まれます。うっかり踏みやすいのは**天気の地点**です。

- 時刻アナウンスの言い回し（`time_signal.announce_template` / `noon_template`）
- ひとこと（`assets/quotes.json`）
- 天気の文（`weather.sentence_*`）や**読み上げる地点**（`weather.open_meteo.locations`）
- 事前生成の範囲（`weather.prerecord`）

**なぜ分かれるのか。** 読み上げ音声は文言と 1 対 1 で作り置きしてあり、実行時は
**文字列の完全一致**で引いています。1 文字でも変えると照合が外れ、その文言だけ
実行時に Open JTalk が合成します。結果、**そこだけ別人の男性音声**になります。
`git pull` だけでは作り置きは増えないため、Pi に配る前に作り直す必要があります。

---

### A. Pi 側で反映する

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

`scripts/setup.sh --no-apt` は、設定の追加分の反映と時報音の生成を行います（apt は実行しません）。
何度実行しても安全で、`config.json` は上書きしません。

反映されたか確認します。

```bash
sudo systemctl status campus_chime.service
```

```bash
python3 campus_chime.py --test-hourly 10
```

- [ ] `Active: active (running)` になっている
- [ ] **読み上げがすべてずんだもんの声**である（男性の声が混ざったら B が済んでいません）
- [ ] 天気が流れる時刻（既定 10/12/14/16 時）で天気が読み上げられる

```bash
python3 campus_chime.py --schedule
```

- [ ] 翌営業日の予定が並ぶ

自動起動の確認もしておくと安心です（電源を入れ直せば勝手に動く状態かどうか）。

```bash
sudo systemctl is-enabled campus_chime.service
```

`enabled` と出れば、電源投入時に自動起動します。実際に再起動して確かめる場合は次のとおりです。

```bash
sudo reboot
```

再接続して、

```bash
sudo systemctl status campus_chime.service
```

---

### B. 文言を変えたとき（PC 側で音声を作り直す）

**Pi ではなく、VOICEVOX を動かせる PC 側で行います。** Pi 3B では VOICEVOX を動かせません。

```bash
docker start voicevox
```

コンテナを作っていない場合は 8 章を参照してください。

```bash
curl -s http://127.0.0.1:50021/version
```

バージョンが返ってから、

```bash
python3 scripts/generate_voicevox.py --include-quotes --prune
```

`--prune` は、**使われなくなった古い音声と manifest のエントリを削除**します。manifest は
マージ方式で書き戻すため、これを付けないと古いファイルが残り続けます。付けない場合も
残存件数は表示されるので、消す前に確認できます。

生成できたか確認します。

```bash
ls assets/voice/*.wav | wc -l
```

```bash
python3 campus_chime.py --test-hourly 10 --backend pygame
```

WSL2 では `--backend pygame` が必須です（付けないとログだけ流れて音が鳴りません。10-6 参照）。
**読み上げがすべてずんだもんの声**であることを耳で確認してください。

問題なければコミットして push します。

```bash
git add assets/voice/
```

```bash
git status --short
```

追加・削除された音声ファイルが並ぶことを確認してから、

```bash
git commit -m "assets: 読み上げ音声を再生成"
```

```bash
git push
```

**push が通ったことを必ず確認してください。** コミットしただけ、あるいは認証で止まった
ままだと、Pi 側で `git pull` しても古い音声のままになります。

```bash
git status
```

`Your branch is up to date with 'origin/...'` と出れば届いています。`ahead of ... by N commits`
と出ていれば push できていません。

ここまで済んだら **A** に進みます。

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

まず `weather.enabled` を確認してください。

```bash
python3 campus_chime.py --print-config | python3 -c "import json,sys; print(json.load(sys.stdin)['weather']['enabled'])"
```

`False` なら天気予報が無効になっています。既定は `True` なので、`config.json` で無効にしていないか確認してください。

なお 11 / 13 / 15 時に天気が流れないのは**異常ではありません**。既定では 10 / 12 / 14 / 16 時の 2 時間おきに流す設定です（7 章「天気予報の時刻を変える」参照）。

`True` にしているのに流れない場合は、ネットワークが切れている、または気象庁側が応答しない可能性があります。この場合も**異常ではありません**。自動的に「ひとこと」へ切り替わります（時報は必ず鳴ります）。

切り分け:

```bash
python3 campus_chime.py --weather
ping -c 3 www.jma.go.jp
```

### 10-6. WSL2 で試すと音が鳴らない

WSL2 上で動作確認をすると、ログは流れる（`[MOCK]` が並ぶ）のに音がまったく鳴らないことがあります。

原因は、WSL が開発環境と判定され、音を出さない `mock` バックエンドが自動選択されるためです。これは Pi 以外の環境で誤って音を鳴らさないための意図した仕様であり、故障ではありません。実機（Raspberry Pi）では `auto` のままで正しく `pygame` が選ばれます。

WSL2 上で実際に音を出して確認したい場合は、`--backend pygame` を明示してください。

```bash
python3 campus_chime.py --test-hourly 16 --backend pygame
python3 campus_chime.py --test-all --backend pygame
```

### 10-7. 読み上げが男性音声になる

時報音（ポ・ポ・ポ・ポーン）は鳴るのに、読み上げ（時刻アナウンス・天気・ひとこと）だけ**男性の機械音声**になることがあります。

読み上げ音声は文言と 1 対 1 で作り置きしてあり、**文字列の完全一致**で引いています。一致しない文言は実行時に Open JTalk が合成するため、そこだけ別人の声になります。

**切り分け 1: 作り置きがあるか**

```bash
ls assets/voice/*.wav | wc -l
```

**166 件**あるか確認してください。少なければ `git pull` が届いていないか、作り置きを作り直していません（9 章 B 参照）。

**切り分け 2: `config.json` が古くないか（今回の原因）**

起動時のログに次の警告が出ていれば該当します。**これが一番確実な判定です**（文言以外が古い場合も拾えます）。

```bash
journalctl -u campus_chime.service -n 50 | grep 既定値
```

```
WARNING ... config.json は既定値と同じ値を 76 項目書いています。既定値の丸ごとコピーの
        可能性があります。この状態だと、更新しても新しい既定値が届きません…
```

読み上げの文言が古いかどうかだけを見るなら、次でも分かります。

```bash
grep -c "お知らせしました" config.json
```

いずれかに当てはまれば、以下で作り直してください。**現地で変更した設定があれば、`config.json.old` から書き戻すこと。**

```bash
mv config.json config.json.old
```

```bash
cp config.example.json config.json
```

```bash
sudo systemctl restart campus_chime.service
```

そのうえで確認します。

```bash
python3 campus_chime.py --test-hourly 10
```

**なぜ起きるのか。** 古いバージョンで作られた `config.json` は、当時の既定値を丸ごと複製したものです。設定は既定値 → `config.json` の順に deep merge されるため、この古い `config.json` が新しい既定値をすべて握りつぶします。現在の `scripts/setup.sh` は、新規に作る `config.json` の内容を空の上書き（`{}`）にするようになったため、ここで作り直せば以後は起きません。

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
