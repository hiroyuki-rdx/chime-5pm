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

本体を導入したあとは、`python3 campus_chime.py --status` の「時刻の同期」でも確かめられます（6-2）。

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
   （`python3-pygame` / `alsa-utils` / `mpg123`）
3. タイムゾーンと NTP 同期の確認
4. 空の `config.json`（変えたい項目だけを書くための上書きファイル）を作成（既にあれば触りません）
5. 時報音（`assets/generated/time_signal.wav`）の生成と、時刻アナウンスの音声（作り置き）の確認
6. 設置状態の点検（`--check`。音は鳴らず、何も書きません。NG があれば「直し方」を表示して先へ進みます）
7. 予定表の表示
8. systemd への登録・有効化・起動。再起動に成功したら、そのときのコミットを `cache/deployed_commit` に記録します（`update.sh` が、サービスが最新のコードで動いているかを見分けるのに使います）

6 で `config.json` を読めなかったとき（`--check` の終了コードが 2）だけは、7 を省き、8 でサービスを再起動しません（読めない設定で再起動すると、起動できないまま再起動を繰り返すため）。このときは最後に「導入は途中です」と表示して**終了コード 1** で終わります。サービスの登録を省く `--no-service` を付けても、同じく終了コード 1 で終わります（「完了」とは表示しません）。`config.json` を直して、もう一度実行してください。

**このスクリプトは何度実行しても安全です**（`config.json` を上書きしません）。

---

## 6. 動作を確認する

### 6-1. その場で鳴らしてみる

```bash
cd /home/pi/campus-chime

# 時報（ポ・ポ・ポ・ポーン → 時刻 → ひとこと）
python3 campus_chime.py --test-hourly

# 12 時の時報（「正午をお知らせしたのだ。」のあと、天気も流れる）
python3 campus_chime.py --test-hourly 12

# 閉館放送（アナウンス → 蛍の光）
python3 campus_chime.py --test
```

チェックポイント:

- [ ] 短音が 3 回、続いて長めの音が 1 回鳴る
- [ ] 「午前◯時をお知らせしたのだ。」と読み上げられる
- [ ] 12 時は、そのあと大津の**現在の**天気と気温が流れる（それ以外の時刻は流れない）
- [ ] そのあと「ひとこと」が流れる（天気の有無に関わらず毎回）
- [ ] **すべての文言が聞こえる**（無音になる文言があれば、その文言の作り置きが外れている。8 章参照）
- [ ] 蛍の光がだんだん大きくなる（2 秒フェードイン）
- [ ] **音飛び・ぶつ切れがない**

### 6-2. 状態を見る・設置状態を点検する

```bash
python3 campus_chime.py --status
```

```
campus-chime 6.1.0 (a1b2c3d)

現在時刻        2026-08-26 20:07:15 JST
時刻の同期      同期済み（NTP）
サービス        動作中（active）
自動起動        有効（再起動後も自動で始まります）（enabled）
再生方法        pygame
作り置きの音声  138 件すべてそろっています

直近の放送（新しい順）
  （記録はまだありません）

次の予定
  - 時報 2026-08-27 10:00:00（再生開始 09:59:57）
  - 時報 2026-08-27 11:00:00（再生開始 10:59:57）
  - 時報 2026-08-27 12:00:00（再生開始 11:59:57）

気になる点は見つかりませんでした。
```

末尾が「気になる点は見つかりませんでした。」なら、常駐していて、時刻が同期していて、作り置きの声もそろっています（終了コード 0）。「要確認:」と出たら、その行のとおりに確かめてください（終了コード 1）。「自動起動」は、「無効」でも「要確認」にならないので、「有効」になっていることを自分の目で確かめてください（電源を入れ直したときに自動で始まるかどうか）。1 行目の版とコミットは、手元のコードのものです（更新が途中で止まった Pi では、サービスがその版で動いているとは限りません。9 章 A を参照）。「直近の放送」は、常駐が実際に放送した結果で、放送が済むと並びます（`--test-hourly` などの試し鳴らしは残りません）。

続けて、設置状態を点検します（音は鳴らず、何も書きません）。

```bash
python3 campus_chime.py --check
```

```
== 設定 ==
  OK    読み込んだ設定  既定値 → /home/pi/campus-chime/config.json
  OK    設定の書き方    問題は見つかりませんでした

== 作り置きの音声 ==
  OK    作り置き  138 件すべてそろっています（時刻アナウンス 7/7・ひとこと 57/57・天気 74/74）

== 音源 ==
  OK    閉館アナウンス  assets/announce.wav
  OK    蛍の光          assets/hotaru.mp3
  OK    時報音          assets/generated/time_signal.wav

== 書き込み ==
  OK    cache/      書き込めます
  OK    cache/tts/  まだありません（合成した音声を保存するときに作ります）

結果: すべて OK です。
```

`NG` の行には必ず「直し方」が付きます（警告と情報にも付くことがあります）。`NG` が 1 件でもあれば終了コード 1 で、警告と情報だけなら 0 です。警告と情報は放送を止めません。`cache/tts/` が「まだありません」でも正常です（Pi では音声の合成もキャッシュも発生しません）。音源のファイルは、空でないか、最後まで読めるかまで確かめるので、電源断などで途中までしか書けなかったファイルも `NG` になります（WAV は音のデータを最後まで読み、MP3 は先頭の形と大きさを見たうえで、MPEG のフレームをファイルの終わりまで 1 つずつたどって、最後のフレームが切れていないか、途中に MP3 でないデータが挟まっていないか、ID3 タグのあとにフレームが無い（電源断で、タグまでしか書けなかった）ものでないかを確かめます）。`tts.engines` から `prerecorded` を外すと、声がそろっていても Pi は作り置きを使わず読み上げがすべて無音になるので、「作り置き」の行が `NG`、`--status` の末尾が「要確認」になります（PC で VOICEVOX ENGINE だけを使う開発用の設定です。Pi では戻してください）。状態の保存先（`state.file`）を `/tmp` のような sticky ビットのフォルダに置き、フォルダも既存の `state.json` も別の利用者の持ち物だと、置き換えて保存できないので `NG` です（既定の `cache/` には当てはまりません）。読み方は [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) 3-6 を参照してください。サービスのログまで見たいときは `sudo systemctl status campus_chime.service` も使えます。

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
cd /home/pi/campus-chime
python3 campus_chime.py --status
```

「サービス」が「動作中」、「自動起動」が「有効」なら成功です（起動の直後は、時刻の同期が済むまで 1〜2 分かかります）。

---

## 7. 設定を変える

時刻・曜日・読み上げ文言・天気の地域は **`config.json` の編集だけ**で変更できます（コードを触る必要はありません）。

```bash
cd /home/pi/campus-chime
nano config.json
python3 campus_chime.py --check      # 書き間違いの点検（鳴らさない）
sudo systemctl restart campus_chime.service
python3 campus_chime.py --schedule   # 反映されたか確認
```

`--check` の「設定」の節は、知らないキー（綴りの違い。近いキーがあれば「もしかして …?」と出します）・廃止したキー・範囲外や型違いの値・存在しないタイムゾーン名などを見つけます。結果は 2 通りです。**NG** は、サービスがその値では動けないもの（存在しないタイムゾーン名、1 秒未満の待機時間、読めない数値など）で、サービスの起動時にその項目**だけ**を既定値へ置き換えて動かします（放送は止まらず、ログに ERROR で残ります）。**警告**は、サービスが動けはするが、書いた意図とは違う動きになるもの（鳴らない時刻、曜日の書き間違い、`null` にした節など）で、**置き換えずに、書いたとおりに動きます**。警告の行は、「直し方」を見て自分で直してください（どの値が NG で、どの値が警告かは、[SPECIFICATION.md](SPECIFICATION.md) 3.5 章にあります）。`config.json` が JSON として読めないとき（引用符やカンマの書き間違い、UTF-8 以外での保存）は、原因を日本語で表示して終了コード 2 になります。読めないまま再起動するとサービスが起動できないので、直してから再起動してください。

> `config.json` は Git 管理外です。`git pull` で更新しても、現地の設定は消えません。

> **`config.json` には変えたい項目だけを書いてください。** 全項目を丸ごと書き写すと、その時点の既定値が固定され、以後の既定値の変更が届かなくなります（10 章「10-7. 読み上げが無音になる」で解説する不具合の原因そのものです）。全項目を見渡したいときは `config.example.json` を参照し、そこから必要な行だけ `config.json` に写してください。

### よくある変更

**時報の時間帯を変える（例: 9 時〜17 時）**

```json
{ "schedule": { "hourly": { "start_hour": 9, "end_hour": 17 } } }
```

（9 時は読み上げエンジンが誤読しやすい時刻の一つですが、既定の `hour_readings` で正しく読めるよう対策済みです）

**お昼の時報だけ止める**

```json
{ "schedule": { "hourly": { "skip_hours": [12] } } }
```

天気予報は既定で 12 時だけなので、**12 時を止めると天気予報も流れなくなります**。天気を残したい場合は、`weather_hours` を別の時刻にしてください（下の「天気予報の時刻を変える」参照）。

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

`{hour}` は数字のみ（例: `4`）のプレースホルダです。読み上げエンジンは「4時」を「よんじ」、「7時」を「ななじ」、「9時」を「きゅうじ」、「0時」を「ぜろじ」と誤読します（正しくは よじ／しちじ／くじ／れいじ）。誤読対策込みの読みが欲しい場合は `{hour}` の代わりに `{hour_reading}` を使ってください（既定のテンプレートはこちらを使っています）。

```json
{ "time_signal": {
    "announce_template": "ただいま{period}{hour_reading}です。" } }
```

対象の時刻を増やしたい場合は `hour_readings` に追加します（キーは 12 時間表記の「時」、文字列）。

```json
{ "time_signal": { "hour_readings": { "0": "れいじ", "4": "よじ", "7": "しちじ", "9": "くじ", "1": "いちじ" } } }
```

**天気予報の時刻を変える**

既定では **12 時の 1 回だけ**、大津の**現在の**天気と気温を読み上げます。ここに無い時刻（12 時以外）は時報と「ひとこと」だけになり、天気 API も呼びません。

以前のように 2 時間おき（10 / 12 / 14 / 16 時）に戻す場合:

```json
{ "extra_segment": { "weather_hours": [10, 12, 14, 16] } }
```

毎正時に流したい場合:

```json
{ "extra_segment": { "weather_hours": [10, 11, 12, 13, 14, 15, 16] } }
```

天気を一切流さない場合（「ひとこと」だけにする）:

```json
{ "extra_segment": { "weather_hours": [] } }
```

**時刻を変えても音声の作り直しは不要です**（天気の読み上げ文は流す時刻と関係なく同じです。作り直しが要るのは、地点や読み上げ文を変えたときです）。

**閉館放送（16:57）には、ここに何を書いても天気は付きません。** 閉館放送は時報とは別の経路で組み立てられるためです。

**天気予報の地域を変える**

既定は大津の 1 か所だけです（v5.1.0 までは大津・京都の 2 か所）。地点を増やすと上から順に読み上げられ、1 か所増やすごとに放送が約 6 秒長くなります。京都を足すには次のように書きます。

```json
{ "weather": { "open_meteo": { "locations": [
    { "label": "大津", "latitude": 35.0045, "longitude": 135.8686 },
    { "label": "京都", "latitude": 35.0116, "longitude": 135.7681 }
] } } }
```

`locations` は既定に足されるのではなく**丸ごと置き換わる**ため、大津も含めて全地点を書いてください。`label` はそのまま読み上げられるので、読み間違えない表記にしてください（漢字の読みが不安なら、ひらがなで書くのが確実です）。

**地点や読み上げ項目を変えたら、必ず音声を作り置きし直してください**（8 章）。作り置きに無い文言は**その 1 文だけ無音になります**（時報音・蛍の光・他の文言は鳴ります）。京都の音声も v5.2.0 で削除したため、京都を足す場合も作り直しが必要です。

作り直しは PC で行います。`config.json` は Git 管理外で PC には無いため、**Pi の `config.json` を PC に持ってきて `--config` で渡します**（渡さないと、足した地点の文言は生成されません）。

```bash
scp pi@<Pi のホスト名>:/home/pi/campus-chime/config.json ./pi-config.json
python3 scripts/generate_voicevox.py --config pi-config.json --include-quotes --prune
```

`--config` は、Pi の `config.json` で文言を変えたときに付けるものです。`config.json` を変えていない場合は、付けなくて構いません（8 章）。

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

天気の取得先は Open-Meteo だけです。気象庁への切り替え（`weather.provider` と `weather.jma`）は v6.0.0 で廃止しました。気象庁の予報文は自由文で作り置きできず、天気だけ無音になるためです。

地点や読み上げ項目を変えたら、実機で次のコマンドを実行し、天気の文が出ることを確認してください。

```bash
python3 campus_chime.py --weather
```

**おまけを止める（時報だけにする）**

```json
{ "extra_segment": { "enabled": false } }
```

**ひとことを追加・削除する**

`assets/quotes.json` を編集します。`general` は全時刻共通、`by_hour` は指定時刻のみ候補に加わります。

編集は Pi ではなく PC 側で行い、commit・push してから Pi で取り込みます（Pi で直接編集すると、次の `git pull` が衝突します）。

- **追加・変更したとき**は、足した文の作り置きが無いと無音になるため、PC で読み上げ音声を作り直します（9 章 B）
- **削除だけ**なら、音声の作り直しは要りません

Pi では 9 章 A の手順で取り込み、**必ず再起動**します。常駐プロセスは `assets/quotes.json` を起動時に 1 回しか読みません。`--test-hourly` は別のプロセスで動くため、再起動を忘れていても確認だけは通ってしまいます。

```bash
python3 campus_chime.py --test-hourly 15   # 確認
```

**ログに「v6.0.0 で廃止しました」と出たら**

v6.0.0 で廃止した設定が `config.json` に残っていると、起動のたびに警告が出ます。値は無視されて放送は止まりませんが、警告に出た行は `config.json` から消してください。残っている行は、次で「警告」として出ます（「直し方: この行を消してください」）。

```bash
python3 campus_chime.py --check
```

- 廃止した 5 項目 `extra_segment.mode` / `weather_probability` / `always_weather_hours` / `always_quote_hours` / `fallback_to_quote` は、天気を流す時刻を `extra_segment.weather_hours` で指定する方式に置き換わりました
- 廃止した 3 項目 `weather.provider` / `weather.jma` / `weather.max_weather_chars` は、天気の取得先が Open-Meteo だけになったため不要です

廃止した `mode: "choice"`（天気かひとことのどちらか一方を選ぶ方式）を使っていた場合は、天気を流したい時刻を `weather_hours` で指定し直してください（前掲「天気予報の時刻を変える」）。天気を流す時刻でも、ひとことは毎回流れます。

---

## 8. 読み上げ音声を作り直す（文言を変えたとき）

読み上げる文言はすべて VOICEVOX:ずんだもんの声で作り置きしてリポジトリに同梱してあり、Pi 上では再生するだけです（VOICEVOX ENGINE は Pi 3B 上で常時動かすには重すぎるため）。

**文言を変えたとき**（語尾、ひとこと、天気の地点や読み上げ項目など）は、**PC 側で作り置きを作り直す必要があります**。作り直さないと、変えた文言だけ無音になります（時報音・蛍の光・他の文言は鳴ります）。

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

Pi の `config.json` で文言を変えている（天気の地点を足した、時刻アナウンスの言い回しを変えたなど）場合は、その `config.json` を PC に持ってきて `--config` で渡します。

```bash
scp pi@<Pi のホスト名>:/home/pi/campus-chime/config.json ./pi-config.json
python3 scripts/generate_voicevox.py --config pi-config.json --include-quotes --prune
```

`config.json` を変えていない場合は、`--config` なしで構いません。

```bash
python3 scripts/generate_voicevox.py --include-quotes --prune
```

`--config` を付けたときに生成する文言は、その設定の文言に**既定の設定の文言を足したもの**です。`config.json` の配列は既定値を丸ごと置き換える（地点に京都だけを書くと、既定の大津が外れる）ため、既定の分も常に含めて、同梱の作り置きを崩さないようにしています。

既定の設定では **138 件**（時刻アナウンス 7 ＋ ひとこと 57 ＋ 天気 74。天気の内訳は、天気の文 28 ＋ 気温の文 46）が生成されます。`--config` で足した文言の分は、これに加わります。数分かかります。

`--prune` は、**現在の文言集合に無くなった古い音声と manifest のエントリを削除**します。manifest はマージ方式で書き戻すため、文言（語尾や地名、読み上げる項目）を変えると古いファイルが残り続けます。付けない場合も残存件数は表示されるので、消す前に確認できます。残す文言の判定も、`--config` の設定の文言と既定の設定の文言の両方が対象です。

**`--config` なしで `--prune` を付けると、Pi の `config.json` で足した文言（地点など）の作り置きは消えます**（警告が出ますが、処理は続きます）。Pi の `config.json` で文言を足しているなら、`--prune` のときは必ず `--config` を付けてください。

`scripts/generate_voicevox.py` は既定でエンジンの起動を最大 90 秒待つため（`--wait` で変更可能）、起動直後で `/version` が応答しない状態でもそのまま実行して構いません。90 秒待っても応答しない場合はエラーメッセージの案内に従って確認してください。

Docker Desktop（Windows）で VOICEVOX ENGINE を動かしている場合、WSL2 側から `127.0.0.1` では届かないことがあります。その場合は `--base-url` で Windows ホスト側の IP を指定してください。

```bash
python3 scripts/generate_voicevox.py --include-quotes --prune --base-url http://<ホストのIP>:50021
```

Pi の `config.json` を渡す場合は、この例にも `--config pi-config.json` を加えてください。

生成した音声を WSL2 側でその場で試聴する場合は、既定では音が鳴りません。`--backend pygame` を明示してください（詳しくは「10-6. WSL2 で試すと音が鳴らない」参照）。

3. 生成された `assets/voice/` を commit して push
4. Pi 側で反映（9 章 A）

```bash
cd /home/pi/campus-chime
bash scripts/update.sh
python3 campus_chime.py --test-hourly
```

事前生成された文言はそのまま使われます。**作り置きに無い文言だけ無音になります**（時報音・蛍の光・他の文言は鳴ります）。

天気予報（Open-Meteo）も、既定の設定であれば全パターンが作り置きされるため、**Pi 上では音声合成が一切発生しません**。読み上げが無音になったら、次のいずれかです。

1. 文言を変えたあとに作り置きを作り直していない
2. 古い `config.json` が文言を上書きしている（10 章「10-7. 読み上げが無音になる」参照）

---

## 9. 更新のしかた

**更新の前に、読み上げる文言を変えたかどうかを確認してください。** ここで経路が分かれます。

| 変えたもの | 必要な作業 |
|---|---|
| コード・設定値（時刻、曜日、音量など） | **A だけ**（Pi で `bash scripts/update.sh`） |
| **読み上げる文言** | **B → A の順**（PC で音声を作り直してから Pi へ） |

「読み上げる文言を変えた」には次が含まれます。うっかり踏みやすいのは**天気の地点**です。

- 時刻アナウンスの言い回し（`time_signal.announce_template` / `noon_template`）
- ひとこと（`assets/quotes.json`）
- 天気の文（`weather.sentence_*`）や**読み上げる地点**（`weather.open_meteo.locations`）
- 事前生成の範囲（`weather.prerecord`）

**なぜ分かれるのか。** 読み上げ音声は文言と 1 対 1 で作り置きしてあり、実行時は
**文字列の完全一致**で引いています。1 文字でも変えると照合が外れ、その文言だけ
**無音になります**（時報音・蛍の光・他の文言は鳴ります）。
`git pull` だけでは作り置きは増えないため、Pi に配る前に作り直す必要があります。

---

### A. Pi 側で反映する

> **16:55〜17:02（閉館放送の前後）は、更新も再起動もしないでください。** `update.sh` は放送の時間帯が終わるまで待ってから再起動するので、この時間帯に実行すると、`update.sh` は最大約 6 分（放送の時間帯の余裕を含む、待つ上限です。閉館放送そのものの長さではありません）待つことがあります。手で再起動した場合は、放送中でも再生は止まらず、蛍の光の途中なら systemd が約 90 秒後に強制終了します（今後の版で改善予定）。

`pi` ユーザーで（`sudo` は付けずに）実行します。

```bash
cd /home/pi/campus-chime
```

```bash
bash scripts/update.sh
```

`scripts/update.sh` は、次を順に行います（何度実行しても安全で、`config.json` は上書きしません）。

1. いまの版（`campus-chime 版 (コミット)`）を表示し、Git 管理下のファイルが手元で書き換わっていないか確認する
2. `git pull --ff-only` で最新版を取り込む。すでに最新版なら、サービスに反映した版の記録（`cache/deployed_commit`。`setup.sh` が再起動に成功したときに書く）を見る。いまの版と同じなら `--status` を表示して終わる。**合っていない・無いときは、前回の更新が途中で止まったとみて、警告して続きを行う**
3. 放送の時間帯なら、終わるまで待つ（`python3 campus_chime.py --wait-idle`）
4. `bash scripts/setup.sh --no-apt` を実行する（時報音の生成、時刻アナウンスの音声の確認、設置状態の点検、サービスの登録し直しと再起動。apt は実行しません）
5. 「旧い版 → 新しい版」と、いまの状態（`--status`）を表示し、元の版へ戻す手順を表示する（コードが更新されたときだけ）

放送の時間帯は、各放送の準備を始める少し前（10 秒前）から、時報は再生開始の 90 秒後、閉館放送は 300 秒後までです。待つのは最大 360 秒です。

次のときは、理由を表示して終了します。表示に従って直してから、もう一度実行してください。上の 8 つは、**何も変えずに**止まります（権限またはディスクの問題で `git pull` が失敗したときだけは、取り込みが途中まで進んでいることがあります。次の実行で「手元の変更」として案内が出るので、その表示に従ってください）。

| 止まる理由 | 対処 |
|---|---|
| root で実行した（`sudo` を付けた） | `pi` ユーザーで、`sudo` なしで実行する |
| Git 管理下のファイルが手元で書き換わっている | 表示された `git restore --source=HEAD --staged --worktree -- <ファイル>` で元に戻す（`git add` 済みの変更も戻る。`config.json` は Git 管理外なので対象外。2.23 より前の古い git には `git restore` が無いので、`git checkout HEAD -- <ファイル>` を使う） |
| `git` が手元の変更を調べられない（フォルダの持ち主が違うなど） | 表示された理由に従う。`pi` ユーザーで実行しているか確かめ、リポジトリが root で作られていたら `sudo chown -R pi:pi /home/pi/campus-chime` で持ち主を戻す |
| ブランチではなく特定のコミットを見ている（detached HEAD） | 更新するブランチ（通常は `main`）に戻す |
| `git pull` できない（ネットワークにつながらない、GitHub への接続・認証に失敗した（`publickey` など）、または履歴が GitHub と食い違っている。下の 3 つに当てはまらないときは、ここに入る） | ネットワークを確認して、もう一度試す。履歴が GitHub と食い違っているなら、管理者に伝える。放送は今までどおり動いている |
| `git pull` できない（`git` の表示に `Permission denied`・`insufficient permission` とある。以前に `sudo` で `git` を実行して、リポジトリの中に root のファイルが残っている） | ネットワークの問題ではない。表示される `sudo chown -R pi:pi /home/pi/campus-chime` で持ち主を直して、もう一度実行する。取り込みが途中まで進んでいることがある（次の実行で「手元の変更」として案内が出たら、その表示に従う）。放送は今までどおり動いている |
| `git pull` できない（`git` の表示に `No space left on device`・`Read-only file system`・`Input/output error`・`Disk quota exceeded` とある。このリポジトリのあるディスク（SD カード）がいっぱいか、エラーのあとで読み取り専用に切り替わっている。権限の語句と一緒に出ても、ディスクの問題として案内する） | ネットワークの問題ではない。表示される `df -h`（空き容量。Use% が 100% に近い、または Avail が 0 ならいっぱいなので、不要なファイルを消して空きを作る）と `dmesg \| tail -n 30`（ディスクのエラー。`I/O error` や `Remounting filesystem read-only` と出ていれば、読み取り専用に切り替わっている。権限が無いと言われたら `sudo dmesg \| tail -n 30`）で確かめる。読み取り専用は再起動で戻ることがあるが、くり返すときは SD カードの交換を考える。直したら、もう一度実行する。取り込みが途中まで進んでいることがある（次の実行で「手元の変更」として案内が出たら、その表示に従う）。放送は今までどおり動いている |
| `git pull` できない（`git` の表示に `would be overwritten` とある。手元の Git 管理外のファイルが、新しい版のファイルと同じ名前で、上書きされてしまう） | 表示されたファイルが要るなら別の場所へ移し（`mv`）、要らないなら消してから、もう一度実行する。放送は今までどおり動いている |
| 放送の時間帯が 360 秒待っても終わらない | コードの更新（`git pull`）は済んでいるが、サービスは再起動していない。放送のない時間に、もう一度 `bash scripts/update.sh` を実行する |
| `config.json` を読めない（`--wait-idle` が終了コード 2） | 放送の時間帯かどうかを調べられないので、サービスは再起動していない。表示された原因（JSON の書き間違い、UTF-8 以外での保存）を直して、もう一度 `bash scripts/update.sh` を実行する。急ぐときも、先に `config.json` を直す（読めないままでは、手で `bash scripts/setup.sh --no-apt` を実行しても、`setup.sh` もサービスを再起動しない） |
| `setup.sh` が失敗した | サービスは再起動されていないかもしれない。表示を確認して直し、もう一度 `bash scripts/update.sh` を実行する |

下の 3 つは、コードの取り込みが済んだあとに止まるので、サービスは古い版のまま動き続けます（`setup.sh` が失敗したときは、再起動の前に止まったことが多いのですが、再起動されたかは分かりません）。直したあとにもう一度 `bash scripts/update.sh` を実行すると、**すでに最新版でも**、`cache/deployed_commit` がいまの版と合わないので、続きから反映します（記録が無いときも同じです）。`--status` の 1 行目の版は手元のコードのものなので、サービスが新しい版で動いているかの判断には使えません。

元の版へ戻すときは、表示される 2 行（`git reset --hard <元のコミット>` と `bash scripts/setup.sh --no-apt`）を自分で実行します（`update.sh` は戻す操作を実行しません）。この 2 行は、コードを更新した回の終わり（取り込みのあとで止まった回を含む）に表示されます。続きから反映する回（コードがすでに最新のとき）には表示されないので、戻す可能性があるなら、止まった回の表示を控えておいてください。

**手で更新するとき。** 次の 2 つの場合は、手で行えます。

**v6.1.0 より前の版（v6.0.0 以前）から、初めて更新するとき**は、手元にまだ `update.sh` も `--wait-idle` も無い（実行すると `unrecognized arguments` で終了コード 2 になる）ので、一度だけ次の順に実行します。放送の時間帯を待つ仕組みが無いので、**16:55〜17:02（閉館放送の前後）を避けて**ください。

```bash
cd /home/pi/campus-chime
```

```bash
git pull
```

```bash
bash scripts/setup.sh --no-apt
```

`scripts/setup.sh --no-apt` は、時報音の生成、時刻アナウンスの音声の確認、設置状態の点検、サービスの登録し直しと再起動までを行います（`restart` の 1 行は要りません）。これで v6.1.0 以降になるので、次の更新からは `bash scripts/update.sh` で行えます。

**`update.sh` が途中で止まったとき**は、もう一度 `bash scripts/update.sh` を実行するのが簡単です（続きから反映します）。手で進めるなら、コードはもう新しい版なので `--wait-idle` が使えます。放送の時間帯が終わるのを待ってから、導入スクリプトを実行します。

```bash
cd /home/pi/campus-chime
```

```bash
python3 campus_chime.py --wait-idle
```

```bash
bash scripts/setup.sh --no-apt
```

```bash
sudo systemctl restart campus_chime.service
```

`--wait-idle` は、放送の時間帯でなければすぐ終わります。`scripts/setup.sh --no-apt` はサービスの再起動までを行うので、`restart` は念のための 1 行です。

反映されたか確認します。

```bash
python3 campus_chime.py --status
```

```bash
python3 campus_chime.py --test-hourly 12
```

- [ ] `--status` の末尾が「気になる点は見つかりませんでした。」で、1 行目の版が更新した版になっている（「自動起動」も「有効」）
- [ ] **読み上げがすべて聞こえる**（無音になっている文言があれば、B が済んでいないか、`config.json` が古い（10-7）のどちらかです。`--status` の「作り置きの音声」と、`--check` の「作り置き」にも出ます）
- [ ] 12 時の時報で天気が読み上げられる

```bash
python3 campus_chime.py --schedule
```

- [ ] 翌営業日の予定が並ぶ

自動起動も `--status` の「自動起動」が「有効」なら、電源を入れ直せば勝手に動く状態です。実際に再起動して確かめる場合は次のとおりです。

```bash
sudo reboot
```

再接続して、

```bash
cd /home/pi/campus-chime
python3 campus_chime.py --status
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

バージョンが返ってから、Pi の `config.json` で文言を変えている場合は、その `config.json` を PC に持ってきて `--config` で渡します。

```bash
scp pi@<Pi のホスト名>:/home/pi/campus-chime/config.json ./pi-config.json
python3 scripts/generate_voicevox.py --config pi-config.json --include-quotes --prune
```

`config.json` を変えていない場合は、`--config` なしで構いません。

```bash
python3 scripts/generate_voicevox.py --include-quotes --prune
```

`--prune` は、**使われなくなった古い音声と manifest のエントリを削除**します。manifest は
マージ方式で書き戻すため、これを付けないと古いファイルが残り続けます。付けない場合も
残存件数は表示されるので、消す前に確認できます。`--config` なしで付けると、Pi の
`config.json` で足した文言（地点など）の作り置きも消えるため、足している場合は必ず
`--config` を付けてください（8 章）。

生成できたか確認します（鳴らさず、何も書きません）。Pi の `config.json` を渡したときは、同じ `--config pi-config.json` を付けます。

```bash
python3 campus_chime.py --check
```

「作り置きの音声」が「138 件すべてそろっています」（`--config` で文言を足したときは、その分が加わった件数）になれば揃っています。声の無い文言があると NG になり、その文言が一覧で出ます。PC では、時報音が「まだありません」という警告が出ても構いません（放送のときに自動で作られます）。

```bash
python3 campus_chime.py --test-hourly 12 --backend pygame
```

WSL2 では `--backend pygame` が必須です（付けないとログだけ流れて音が鳴りません。10-6 参照）。
**すべての読み上げが聞こえる**ことを耳で確認してください。

ただし、PC で VOICEVOX ENGINE が動いていると、作り置きの欠けをその場で合成して埋めてしまうため、PC で聞いても欠けは分かりません。push 後に GitHub の CI「作り置きだけで全文言を賄えること（音声合成エンジンなし）」が緑になれば、Pi でも全文言が鳴ります。

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

`python3 campus_chime.py --status` の「再生方法」でも確認できます（実機で `mock` になっていると「← 要確認」が付きます）。

### 10-3. 手動では鳴るがサービスでは鳴らない

サービスは `pi` ユーザーで動きます。音声デバイスへの権限を確認してください。

```bash
groups pi                    # audio が含まれているか
sudo usermod -aG audio pi    # 含まれていなければ追加
sudo systemctl restart campus_chime.service
```

サービスが放送した結果は、`--status` の「直近の放送」に残ります（失敗・エラー・無音になった文言も出ます。読み方は [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) 3-6）。

```bash
python3 campus_chime.py --status
```

ログも確認します。

```bash
journalctl -u campus_chime.service -n 50 --no-pager
```

### 10-4. 読み上げだけ鳴らない（時報音は鳴る）

読み上げに使えるエンジンが 1 つもありません。時報音は合成不要なので鳴ります。

```bash
python3 campus_chime.py --test-hourly --log-level DEBUG
```

ログの `TTS:` を確認します。

```
再生バックエンド: pygame / TTS: prerecorded(利用不可), voicevox(利用不可)
```

`prerecorded(利用不可)` になっている場合、`assets/voice/` が見つかっていません。`git clone` / `git pull` が完全に終わっているか確認してください。作り置きがそろっているかは、次で分かります（鳴らさず、何も書きません）。

```bash
python3 campus_chime.py --check
```

「作り置きの音声」が「138 件すべてそろっています」になっているか確認してください。フォルダが無ければ「フォルダ assets/voice が見つかりません」と NG が出ます（9 章 A を参照）。Pi 上では VOICEVOX ENGINE を動かさない運用のため、`voicevox(利用不可)` はここでは異常ではありません。

`TTS:` の行に `prerecorded` が無い（`voicevox(利用不可)` だけ、または `（エンジンなし）`）場合は、`config.json` の `tts.engines` から `prerecorded` が外れています。Pi では作り置きだけが声の元なので、読み上げがすべて無音になります。`--check` の「作り置き」が `NG`、「設定」に警告が出て、`--status` の末尾も「要確認」になります。`tts.engines` の行を `config.json` から消す（既定は `["prerecorded", "voicevox"]`）か、`"prerecorded"` を足してください。

一部の文言だけ聞こえない場合は「10-7. 読み上げが無音になる」を参照してください。

### 10-5. 天気予報だけ流れない

まず耳で切り分けます。

| 時刻の読み上げ | 天気 | 原因 |
|---|---|---|
| 聞こえない（ポーンのあと、いきなりひとこと） | 流れない | 古い `config.json` |
| 聞こえる | 流れない（12 時） | ネットワーク／時刻のずれ |
| 聞こえる | 流れない（12 時以外） | 仕様どおり |

次のコマンドで詳しく確認できます（音は鳴りません）。

```bash
python3 campus_chime.py --test-hourly 12 --dry-run
```

警告の文言ごとの意味は次のとおりです。

- **「天気予報機能が無効化されています」** — `weather.enabled` が `false` です。多くは古い `config.json` が原因なので、「10-7. 読み上げが無音になる」の手順で作り直してください。
- **「全地点で失敗」** — 通信できていません。`python3 campus_chime.py --weather` で地点ごとの理由を確認し、`python3 campus_chime.py --status` の「時刻の同期」（「同期済み（NTP）」になっているか）も確認してください。

古い `config.json` かどうかは、`python3 campus_chime.py --check` で分かります。「設定」に「既定値と同じ値です」という情報が並んでいれば、`config.json` が古い可能性が高いです。「10-7. 読み上げが無音になる」を参照してください。

なお 12 時以外に天気が流れないのは**異常ではありません**。既定は 12 時の 1 回だけです（7 章「天気予報の時刻を変える」参照）。

通信に失敗しても放送そのものは止まりません。時報音・時刻の読み上げ・ひとことは鳴ります（黙って飛ばされるのは天気予報だけです）。

正常なときは次のように天気予報（1/2）〜（2/2）が並び、警告は出ません。直ったかどうかの目安にしてください。

```
再生内容:
  - 時報音（ポ・ポ・ポ・ポーン）
  - 時刻アナウンス「正午をお知らせしたのだ。」
  - 天気予報（1/2）「今の大津の天気はくもりなのだ。」
  - 天気予報（2/2）「気温は28度なのだ。」
  - ひとこと「自転車は、決められた場所に停めるのだ。」
```

（天気・気温・ひとことの文言は日によって変わります）

### 10-6. WSL2 で試すと音が鳴らない

WSL2 上で動作確認をすると、ログは流れる（`[MOCK]` が並ぶ）のに音がまったく鳴らないことがあります。

原因は、WSL が開発環境と判定され、音を出さない `mock` バックエンドが自動選択されるためです。これは Pi 以外の環境で誤って音を鳴らさないための意図した仕様であり、故障ではありません。実機（Raspberry Pi）では `auto` のままで正しく `pygame` が選ばれます。

WSL2 上で実際に音を出して確認したい場合は、`--backend pygame` を明示してください。

```bash
python3 campus_chime.py --test-hourly 16 --backend pygame
python3 campus_chime.py --test-all --backend pygame
```

### 10-7. 読み上げが無音になる

時報音（ポ・ポ・ポ・ポーン）や蛍の光は鳴るのに、時刻アナウンス・天気・ひとことのうち特定の 1 文だけ**無音**になることがあります（他の文言は普通に聞こえます）。

原因が古い `config.json`（後述）の場合は、特定の 1 文だけでなく**時刻の読み上げが無音になり、天気も流れない**という出方をします。「天気が流れない」と感じた場合もこの節を確認してください。

読み上げ音声は文言と 1 対 1 で作り置きしてあり、**文字列の完全一致**で引いています。一致しない文言は VOICEVOX ENGINE（PC 側で動かしていれば）で合成しますが、Pi では動かしていないため、一致しない文言はその 1 文だけ無音になります（放送そのものは止まりません。時報音・蛍の光・他の文言は鳴ります）。

**気づき方（無音は男性音声より気づきにくい）**

耳だけで気づくのは難しいため、次でも検出できます。`--test-hourly` を `--dry-run` で実行すると、一致しなかった文言が再生内容のログに `警告:` として残ります（本来あるはずの「時刻アナウンス「…」」のような行が無くなっている点にも注目してください）。次は古い `config.json` の場合の例です（天気予報の警告も並びます）。

```bash
python3 campus_chime.py --test-hourly 12 --dry-run
```

```
再生内容:
  - 時報音（ポ・ポ・ポ・ポーン）
  - ひとこと「…」
  警告: 時刻アナウンスを合成できませんでした: 音声合成に失敗しました（prerecorded: 事前生成済み音声にこの文言はありません。 / voicevox: 利用不可）
  警告: 天気予報を取得できませんでした: 天気予報機能が無効化されています。
```

サービスが実際に放送した回で無音になった文言は、`--status` の「直近の放送」に残ります（「一部のみ」の行の「無音: 「…」」。読み方は [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) 3-6）。

```bash
python3 campus_chime.py --status
```

放送ごとの詳しい理由（音声合成の失敗のエラー）は、journal の ERROR ログ（`journalctl -u campus_chime.service -p err`）に残っています。

**切り分け 1: 作り置きがあるか**

```bash
python3 campus_chime.py --check
```

「作り置きの音声」が「138 件すべてそろっています」であることを確認してください。声の無い文言は NG として一覧で出ます（先頭の 10 件まで）。出たのが特定の文言だけなら、その文言を変えたのに作り置きを作り直していません（9 章 B 参照）。「フォルダ … が見つかりません」なら、`git pull` が届いていません。

**切り分け 2: `config.json` が古くないか（今回の原因）**

同じ `--check` の出力で判定できます。**これが一番確実な判定です**（文言以外が古い場合も拾えます）。「設定」に「既定値と同じ値です」という情報が並び、「作り置きの音声」で時刻アナウンスがそろっていなければ該当します。次は古い `config.json` の例です（「情報」の行は多数並ぶため、先頭の 5 件のあとは「（ほか）情報があと N 件あります」とまとめられます）。

```
== 設定 ==
  OK    読み込んだ設定              既定値 → /home/pi/campus-chime/config.json
  情報  timezone                    timezone は既定値（"Asia/Tokyo"）と同じ値です  [/home/pi/campus-chime/config.json]
                                    直し方: 書かなくても同じ動作です。残すと、将来その既定値が変わっても古い値のままになります
  情報  logging.level               logging.level は既定値（"INFO"）と同じ値です  [/home/pi/campus-chime/config.json]
                                    直し方: 書かなくても同じ動作です。残すと、将来その既定値が変わっても古い値のままになります
  （同じ形の「情報」が続く）
  情報  （ほか）                    情報があと 62 件あります
  OK    設定の書き方                問題は見つかりませんでした

== 作り置きの音声 ==
  NG    作り置き  138 件中 7 件の声がありません（時刻アナウンス 0/7・ひとこと 57/57・天気 74/74）。その文は無音になります
                  「午前10時をお知らせしました。」
                  「午前11時をお知らせしました。」
                  「正午をお知らせしました。」
                  「午後1時をお知らせしました。」
                  「午後2時をお知らせしました。」
                  「午後3時をお知らせしました。」
                  「午後4時をお知らせしました。」
                  直し方: PC で作り直して commit し、この Pi で git pull する（docs/SETUP.md 8 章）
```

この場合の「直し方」（PC で作り直す）は当てはまりません。作り置きは揃っていて、`config.json` が古い文言で上書きしているためです。以下で `config.json` を作り直してください。起動時のログにも、同じ指摘が WARNING で出ます。

```
WARNING ... config.json は既定値と同じ値を 76 項目書いています。既定値の丸ごとコピーの
        可能性があります。この状態だと、更新しても新しい既定値が届きません…
```

なお、旧版の既定値を丸ごと写した `config.json` には、v6.0.0 で廃止した項目も含まれています。その場合は「設定」に「廃止しました」の警告も並びます（放送は止まりません。作り直せば消えます）。

いずれかに当てはまれば、以下で作り直してください。**現地で変えていた設定は、`config.json.old` をそのまま書き戻したり丸ごと写したりせず、その項目だけを書いた新しい `config.json` を作って戻してください**（書き方は 7 章「設定を変える」参照）。`config.json` は無くても動作し、無ければ全項目が最新の既定値になります。

```bash
mv config.json config.json.old
```

```bash
sudo systemctl restart campus_chime.service
```

そのうえで確認します。`--check` の「設定」が「問題は見つかりませんでした」、「作り置きの音声」が「すべてそろっています」になっていれば直っています。

```bash
python3 campus_chime.py --check
python3 campus_chime.py --test-hourly 12
```

**なぜ起きるのか。** 古いバージョンで作られた `config.json` は、当時の既定値を丸ごと複製したものです。設定は既定値 → `config.json` の順に deep merge されるため、この古い `config.json` が新しい既定値をすべて握りつぶします。現在の `scripts/setup.sh` は、新規に作る `config.json` の内容を、説明書き（`_comment`）だけの空の上書きにするようになったため、ここで作り直せば以後は起きません。

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
- [ ] `python3 campus_chime.py --check` の末尾が「結果: すべて OK です。」（警告だけなら内容を確かめる）
- [ ] `python3 campus_chime.py --status` の末尾が「気になる点は見つかりませんでした。」で、「サービス」が「動作中」、「自動起動」が「有効」
- [ ] 再起動後も `--status` の「サービス」が「動作中」になる
- [ ] `python3 campus_chime.py --schedule` に翌営業日の予定が並ぶ
- [ ] スピーカーの音量が実際の運用位置で適切
- [ ] 実際の正時（例: 14:00）に立ち会って放送を確認した

---

## 13. 困ったときの参照先

- 運用中のトラブルと対処: [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)
- 設定項目の全一覧: [SPECIFICATION.md](SPECIFICATION.md) 3 章
- なぜこの構成なのか: [REQUIREMENTS.md](REQUIREMENTS.md)
