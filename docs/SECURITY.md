# セキュリティの手引き（SECURITY）

運用中の Raspberry Pi（時報機）を、安全に保つための手引きです。**運用する人向けの文書**で、
脆弱性の報告の窓口（1 節）も兼ねています。

- 現状を調べる: `bash scripts/security_check.sh`（読むだけ。何も変えません）
- 直す: `sudo bash scripts/harden.sh 手順名`（手順ごとに選べて、どれも元に戻せます）
- 見つけた問題を報告する: 1 節

どのスクリプトも、`/home/pi/campus-chime` で実行します。

```bash
cd /home/pi/campus-chime
```

---

## 1. 脆弱性を見つけたとき（報告のしかた）

このリポジトリのコードや手順書に、安全面の問題（脆弱性）を見つけたときの連絡方法です。

- **公開の Issue・Pull Request・コメントに、詳しい内容を書かないでください。** 直る前に公開されると、
  同じ設定の機械が危険になります。
- 非公開で報告してください。リポジトリの **Security** タブに「Report a vulnerability」があれば、
  そこから送れます（所有者が Settings > Security で Private vulnerability reporting を有効にしていると表示されます）。
- ボタンが無いときは、リポジトリの所有者に、公開されない方法で連絡してください。連絡先が分からないときだけ、
  詳細を書かずに「セキュリティの報告をしたい」とだけ Issue に書いてください（連絡の取り方を決めたあと、その Issue は閉じます）。
- 再現の手順、影響を受ける版（`python3 campus_chime.py --version` の表示）、考えられる直し方を添えてもらえると助かります。
  返事までに時間がかかることがあります。

**対象にするもの:** このリポジトリのコード（`campus_chime.py`・`chime/`）、`scripts/` のスクリプト、
`campus_chime.service`、設定の雛形、この手引きを含む文書。最新の `main` の内容が対象です。

**対象にしないもの:** Raspberry Pi OS・Debian・VOICEVOX・Open-Meteo など、他者が作ったものの問題（それぞれの配布元へ）。
大学のネットワークや、他人の機器への試験。**稼働中の実機に、許可なく侵入や負荷の試験をしないでください。**
再現は、自分の手元の機械で行ってください。

---

## 2. 何を守るか・前提

### 2.1 守るもの

1. **放送が、決まった時刻に、正しく鳴ること。** 無人の機械なので、止まっても気づかれにくい。
2. **Pi の中の秘密。** `pi` のパスワード、Wi-Fi のパスワード、SSH の鍵。
3. **大学のネットワークの、ほかの機器。** 乗っ取られた Pi が、足がかりに使われないこと。
4. **GitHub のリポジトリ。** Pi は `main` の中身をそのまま取り込んで実行します（8 節）。

### 2.2 前提と、確かめること

この Pi は、**学内のネットワークからだけ届く**ことを前提にしています（ポート転送はしていません）。

ただし、大学はクラス B・クラス C の IPv4 アドレスの範囲を持っていることがあり、その場合は Pi が
**グローバルアドレス**を持っていることがあります。学外から届くかどうかは、大学の防火壁の設定しだいで、
Pi の中からは確かめられません。そこで、次の順に確かめます。

1. `bash scripts/security_check.sh` を実行する。Pi のアドレスを一つずつ、プライベート（`10.`・`172.16.`〜`172.31.`・`192.168.` で始まる）・
   共有アドレス（`100.64.`〜`100.127.`）・グローバル、に分けて表示します。グローバルなら `[注意]` が出ます。
2. グローバルだったら、**学外から届くかを実際に試す。** スマートフォンのテザリングにつないだ PC から、Pi のアドレスの 22 番（SSH）に
   つながるかを見ます。

   ```
   nc -vz Pi のアドレス 22
   ```

   Windows の PowerShell では `Test-NetConnection Pi のアドレス -Port 22` です。つながれば学外から届きます。時間切れになれば、
   少なくとも 22 番は届きません。大学の情報基盤の担当者に、この Pi のアドレスが学外から見えるかを尋ねてもよいです。
3. 学外から届くなら、SSH のパスワードログインは危険度が「高」です。**3 節の順のうち、鍵の設定（`ssh`）と防火壁（`firewall`）を先に**行ってください。

学外から届かなくても、学内のほかの人からは届きます。3 節の手順は、どちらの場合も行う価値があります。

---

## 3. おすすめの順

1 手順ごとに、確かめてから次へ進みます。**迷ったら、手順の名前に `--dry-run` を付けて**（例: `bash scripts/harden.sh --dry-run ssh`）
何をするかだけを見られます（何も変えません。`sudo` なしでも試せます）。

| 順 | すること | コマンド |
|---|---|---|
| 1 | 現状を点検する | `bash scripts/security_check.sh` |
| 2 | 自分の PC で SSH の鍵を作り、Pi に登録する | 3.1 節 |
| 3 | SSH を鍵だけにする | `sudo bash scripts/harden.sh ssh` |
| 4 | **新しい端末で**確かめ、確定する | 4 節 |
| 5 | 防火壁を入れる（管理に使うネットワークだけに SSH を許す） | `sudo bash scripts/harden.sh firewall --ssh-from 192.168.1.0/24` |
| 6 | **新しい端末で**確かめ、確定する | 4 節 |
| 7 | sudo にパスワードを求める | `sudo bash scripts/harden.sh sudo` |
| 8 | 使わない常駐を止め、ログを残す | `sudo bash scripts/harden.sh services journal` |
| 9 | 必要なら、自動更新・時刻同期・放送サービスの保護 | `sudo bash scripts/harden.sh updates`・`ntp`・`service`（5 節） |

最後に、もう一度 `bash scripts/security_check.sh` を実行して、`[要対応]` が 0 件になったことを確かめます。

現在の状態は、`sudo bash scripts/harden.sh`（手順を付けない）で一覧できます（適用済み・確定待ち・未適用）。
手順は一度に複数指定でき（例: `sudo bash scripts/harden.sh services journal`）、実行の前に、すべての手順の条件が確かめられます。
`sudo bash scripts/harden.sh recommended` は、`services journal ssh firewall` をまとめて行います。ただし鍵の設定の確認が要るので、
慣れるまでは 1 つずつをおすすめします。

どの手順も、次の決まりで動きます。

1. 変える前に、必要な条件をすべて確かめる。満たさないものがあれば、**理由をすべて表示して、何も変えずに止まる。**
2. 変えたあとに、効いたことを確かめる。確かめられなければ、**その手順を自分で元に戻して**止まる。
3. 何度実行しても同じ結果になる（すでに適用済みなら「すでに適用済みです」と表示して何もしない）。
4. 変える前のファイルは `/var/lib/campus-chime-harden`（root だけが読める）に控えてあり、`--rollback` でいつでも戻せる。

サービスの利用者が `pi` でないとき（通常はありません）は、`security_check.sh` にも `harden.sh` にも `--user 利用者名` を付けます。

### 3.1 SSH の鍵を作って登録する（自分の PC で）

Pi に入るための鍵は、**自分の PC で作ります**。鍵は 2 つのファイルでできていて、`id_ed25519.pub`（公開鍵。Pi に渡してよい）と
`id_ed25519`（秘密鍵。**PC の外へ出さない**）です。作るときに聞かれるパスフレーズ（鍵の合言葉）は、決めておいてください。

Windows（PowerShell）:

```powershell
ssh-keygen -t ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh pi@campus-chime.local "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

macOS・Linux:

```bash
ssh-keygen -t ed25519
ssh-copy-id pi@campus-chime.local
```

登録できたら、`ssh pi@campus-chime.local` で入れること（Pi のパスワードを聞かれないこと。鍵のパスフレーズは聞かれます）を確かめます。
Raspberry Pi Imager で最初から鍵を登録しておくこともできます（[SETUP.md](SETUP.md) 1-4）。

**鍵をなくすと入れなくなります。** 秘密鍵は、パスフレーズつきでバックアップを取ってください。別の PC からも入れるように、
その PC の公開鍵も同じ方法で登録しておくと安心です。

---

## 4. 変更を確定する仕組み（自動で元に戻る）と、確かめ方

`ssh` と `firewall` は、**自分が締め出される恐れのある**変更です。そのため、適用しただけでは「確定待ち」になり、
**確定しないと、10 分たったところで自動で元に戻ります。** 確定待ちの間は、`sudo bash scripts/harden.sh`（一覧）の表示が「確定待ち」です。

- 待つ時間は `--revert-after 分` で変えられます（1〜60 の整数。既定は 10）。
- 確定しないまま Pi を再起動しても、起動の 10 分後に元に戻ります。
- `firewall` は、確定するまでは再起動すると消えます。確定すると、再起動後も有効です。
- `ssh` と `firewall` を続けて適用したときは、待つ時間を数え直します。一度の `--confirm` で、確定待ちのすべてを確定します。

### 4.1 確定の手順

1. **今の画面（端末）は閉じないでください。** 何かあったときに戻すために残します。
2. **新しい端末（別の画面）を開き**、鍵で入れることを確かめます。

   ```
   ssh pi@campus-chime.local
   ```

3. （`ssh` を適用したとき）パスワードでは入れないことを確かめます。「Permission denied (publickey)」と出れば正しい動きです。

   ```
   ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password pi@campus-chime.local
   ```

4. （`firewall` を適用したとき）新しい端末の SSH の画面で `python3 campus_chime.py --status` を実行し、サービスが動いていることと、時刻が同期していることを見ます（出ていく通信は絞らないので、放送には影響しないはずですが、念のためです）。
5. **その新しい端末で**、確定します。

   ```
   sudo bash scripts/harden.sh --confirm
   ```

確定しないで、すぐ元に戻すときは `sudo bash scripts/harden.sh --rollback-pending` です。

### 4.2 なぜ「新しい端末」なのか

すでに開いている SSH の接続は、新しい設定を入れたあとも切れずに生きています（`ssh` は設定を読み込み直すだけ、`firewall` は
「すでにつながっている通信」を通す決まりのため）。つまり、**今の画面が動いていても、「次に入り直せる」ことの確認にはなりません。**
入り直せない設定のまま確定してしまうと、次の接続から誰も入れなくなります。

そのため `--confirm` は、**変更より前に開かれた画面から実行すると断ります**（「この画面は、変更の前に開いたものです」）。
この見分けは目安で、tmux や screen の中では見分けられません。入り直せたことを確かめ済みなら `--force` で確定できます。

### 4.3 元に戻す処理の置き場所

自動で元に戻す処理は、リポジトリの中のスクリプトではなく、root だけが書ける `/var/lib/campus-chime-harden/harden.sh` の写しを実行します。
リポジトリの中身が書き換えられても、root で実行されるのは写しのほうです。

---

## 5. 手順ごとの説明

どの手順にも、**何を変えるか・放送への影響・確かめ方・元に戻し方**を書きます。元に戻すときは、手順の名前を付けた
`sudo bash scripts/harden.sh --rollback 手順名` を使います（名前を付けないと、適用したものすべてを新しい順に戻します）。

### 5.1 `ssh` — SSH を鍵だけにする

`sudo bash scripts/harden.sh ssh`

- **変えること:** `/etc/ssh/sshd_config.d/01-campus-chime-harden.conf` を作ります。パスワードでのログイン、キーボードの対話での認証、
  root のログインを止め、公開鍵だけを受け付けます（X11 転送も止めます）。`pi` のほかにログインできるアカウントが無いときは、
  `pi` だけを許す設定も書きます。いまの接続は切れません。
- **止まる条件（何も変えません）:** `pi` の公開鍵が登録されていない・置き場所の持ち主や権限が sshd に拒まれる、鍵の無い別のアカウントがある、など。
  鍵が無いときは、登録の手順を表示します（3.1 節）。
- **放送への影響:** ありません。放送は SSH と無関係です。影響を受けるのは、**鍵を持たない人のログイン**だけです。
- **確かめ方:** 4.1 節の手順。点検では、`bash scripts/security_check.sh` の SSH の行が `[OK]` になります。
- **元に戻す:** `sudo bash scripts/harden.sh --rollback ssh`（設定ファイルを消して、読み込み直します）。

### 5.2 `firewall` — 入ってくる通信を絞る

`sudo bash scripts/harden.sh firewall --ssh-from 192.168.1.0/24`

- **変えること:** nftables の `inet campus_chime_filter` という表を 1 つ足します（既存の表や `/etc/nftables.conf` には触れません）。
  入ってくる通信は、**既定では捨てます。** 通すのは、Pi から始めた通信への返事、ping などの ICMP、DHCP、`campus-chime.local` の名前解決（mDNS）、
  そして **SSH（`--ssh-from` で指定したネットワークからだけ）** です。出ていく通信は絞りません。
  `nft` が入っていないときは、`apt-get install -y nftables` で入れます（放送の終わりを待ってから）。
- **`--ssh-from` について:** SSH を許すネットワークを CIDR で書きます（例: `192.168.1.0/24`。管理に使う PC が 1 台なら `192.168.1.23/32`）。
  何度でも指定できます。指定しないと、Pi と同じネットワーク全体に許します。**管理に使うネットワークだけを書くのをおすすめします。**
  IPv4 の `/8` より広い指定は断ります。
- **止まる条件（何も変えません）:** いま SSH でつながっている相手が、許す範囲に入っていないとき（入れると今の接続が切れます。表示される `--ssh-from` の例を付けて、もう一度実行します）。
  ほかの防火壁（input を見る別の表）があるとき。生成したルールを `nft` が受け付けないとき。
- **放送への影響:** ありません。天気の取得（Open-Meteo）、時刻の同期、DNS、apt、GitHub は、Pi から出ていく通信なので止まりません。
- **確かめ方:** 4.1 節の手順。`sudo nft list table inet campus_chime_filter` で表が見えること。点検の防火壁の行が `[OK]` になること。
- **元に戻す:** `sudo bash scripts/harden.sh --rollback firewall`（表を消し、確定済みなら起動時に読み込む設定も外します。入れた `nftables` のパッケージは残ります）。

### 5.3 `sudo` — sudo にパスワードを求める

`sudo bash scripts/harden.sh sudo`

- **変えること:** `/etc/sudoers.d/` の中の、`pi` にパスワードなしの `sudo` を与えるファイル（Raspberry Pi OS では `010_pi-nopasswd`）を、控えを取ってから外します。
  以後、`sudo` は `pi` のパスワードを聞きます。
- **止まる条件（何も変えません）:** 標準入力が端末でないとき（パスワードの確認を端末で行うため、SSH の画面などから直接実行します）。
  `pi` にパスワードが無いとき（先に `sudo passwd pi` で決めます）。`pi` が `sudo` グループに入っていないとき。パスワードを求める規則が見つからないとき
  （外すと `sudo` を使えなくなるため）。`/etc/sudoers` にパスワードなしの規則があるとき、または外すファイルに `pi` 専用の 1 行以外の内容があるとき
  （自動では直せないので、手で直します）。
- **放送への影響:** ありません（放送のサービスは `sudo` を使いません）。**`bash scripts/update.sh` と `bash scripts/setup.sh` は、`sudo` の行で
  `pi` のパスワードを聞くようになります。** SSH の端末（手で入力できる画面）で実行してください。
- **確かめ方:** 実行の途中で、パスワードを入力して確かめます（確かめられなければ、ファイルを元に戻して止まります）。あとから手で見るなら
  `sudo -k` のあと `sudo -v` でパスワードを聞かれること。
- **元に戻す:** `sudo bash scripts/harden.sh --rollback sudo`。

### 5.4 `services` — 使わない常駐を止める

`sudo bash scripts/harden.sh services`

- **変えること:** `bluetooth.service`・`hciuart.service`・`ModemManager.service` を止めて、無効にします（`mask`）。入っていないものは飛ばします。
  `avahi-daemon` は、`campus-chime.local` で入るために動かしたままです。
- **止まる条件:** Bluetooth のスピーカーで放送を鳴らしている**かもしれない**とき（ペアリング済みの機器や、`bluealsa` の設定が見つかったとき）。
  そのまま止めると音が出なくなるためです。使っていないと確かなときだけ `--force` を付けます。
- **放送への影響:** 3.5mm ジャックと USB のスピーカーには影響しません。Bluetooth のスピーカーを使っていると鳴らなくなります。
- **確かめ方:** `systemctl is-enabled bluetooth.service` が `masked`。`python3 campus_chime.py --test-hourly` で音が出ること。
- **元に戻す:** `sudo bash scripts/harden.sh --rollback services`（もとの有効・起動の状態に戻します）。

### 5.5 `journal` — ログを再起動のあとも残す

`sudo bash scripts/harden.sh journal`

- **変えること:** `/etc/systemd/journald.conf.d/50-campus-chime-harden.conf` を作り、ログをディスクに残します（上限 200M）。`/var/log/journal` が無ければ作ります。
  `/var/log/journal` が無い Pi では、ログは再起動すると消えるので、**停電や再起動のあとに「何が起きたか」を調べられません。**
- **放送への影響:** ありません。SD カードへの書き込みが少し増えます（上限があります）。
- **確かめ方:** `ls /var/log/journal` が見えること。再起動したあとに `journalctl --list-boots` で前回の起動が見えること。
- **元に戻す:** `sudo bash scripts/harden.sh --rollback journal`。この手順が作った `/var/log/journal` は消えるため、**保存してあったログも消えます**（警告が出ます）。

### 5.6 `updates` — セキュリティ更新を自動で入れる（任意）

`sudo bash scripts/harden.sh updates`

- **変えること:** `unattended-upgrades` を入れ（無ければ）、Raspberry Pi OS と Raspberry Pi の配布元の更新を毎朝自動で入れる設定を書きます
  （`/etc/apt/apt.conf.d/52campus-chime-unattended-upgrades` と `21campus-chime-auto-upgrades`）。**自動では再起動しません。**
  apt の自動実行の時刻も、放送の時間帯と重ならないよう、毎朝 5 時台に動かします（`apt-daily.timer`・`apt-daily-upgrade.timer` の追加設定）。
  `apt` を動かす前に、放送の終わりを待ちます。
- **放送への影響:** 更新は毎朝 5:40〜6:00 ごろで、放送のない時間です。**カーネルの更新は、再起動するまで効きません。** 月に 1 回は手で更新して再起動します（6 節）。
- **確かめ方:** `systemctl list-timers 'apt-daily*'` に、5 時台の予定が出ること。点検の自動更新の行。
- **元に戻す:** `sudo bash scripts/harden.sh --rollback updates`（設定とタイマーの追加設定を外します。この手順で入れた `unattended-upgrades` は削除します）。

### 5.7 `service` — 放送サービスに保護（sandbox）をかける（任意）

`sudo bash scripts/harden.sh service`

- **変えること:** `/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf` を足します（systemd の追加設定。**`campus_chime.service` 本体は変えません**。
  `setup.sh` でユニットを登録し直しても、追加設定は残ります）。サービスから見たファイルシステムを読み取り専用にして、書けるのは
  状態・履歴・休止の記録・音声のキャッシュ・時報音の置き場だけにし、特権の取り上げやシステムコールの制限なども足します。
  乗っ取られたとしても、サービスが自分のコードを書き換えたり、ほかの場所を荒らしたりしにくくなります。書き込みを許す場所は、`config.json` の設定から求めます。
- **放送への影響:** サービスを再起動します（放送の終わりを待ってから）。**保護が音を止めてしまわないかを、必ず確かめてください。**
- **確かめ方:** `sudo bash scripts/harden.sh --test-sound` は、保護をかけたまま 12 時の時報を 1 回、**実際にスピーカーから鳴らします**（放送の時間帯と、周りの人に気をつけてください）。
  手順と一緒に `sudo bash scripts/harden.sh service --test-sound` と指定することもできます。あわせて `python3 campus_chime.py --status` を見ます。
  この手順は、適用したあとに「動いているか」「エラーが出ていないか」を自分で確かめ、だめなら元に戻します。
- **元に戻す:** `sudo bash scripts/harden.sh --rollback service`（追加設定を消して、サービスを再起動します）。
  書き込み先を `config.json` で変えたときは、もう一度この手順を実行して、書ける場所を更新します。

### 5.8 `ntp` — 時刻の同期を chrony にする（任意）

`sudo bash scripts/harden.sh ntp`

- **変えること:** 時刻の同期を、`systemd-timesyncd` から `chrony` に替えます（`apt-get install -y chrony`。`systemd-timesyncd` は入れ替わりで外れます）。
  NTP は暗号で守られていないため、同期先から来た時刻をそのまま信じる `systemd-timesyncd` は、起動後も大きな時刻の飛びを受け入れます。
  `chrony` の Debian の既定（`makestep 1 3`）は、最初の数回の更新だけ時刻を飛ばし、そのあとの大きなずれは少しずつ寄せます。放送の時刻が急に狂いにくくなります。
  `systemd-timesyncd` に設定してあった NTP サーバーは引き継ぎます。学内の NTP サーバーを使うなら `--ntp-server ホスト名` を付けます（何度でも指定できます）。
- **放送への影響:** 時刻が正しいほど、正しい時刻に鳴ります。`timedatectl` の「同期済み」の表示（`--status` の「時刻の同期」）は、そのままです。
  適用のとき、同期するまで最大 90 秒待ちます。
- **確かめ方:** `chronyc tracking`・`chronyc -n sources`。`python3 campus_chime.py --status` の「時刻の同期」が「同期済み」。
  これ以降、時刻の再同期は `sudo systemctl restart chrony` です（[KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) 4-5）。
- **元に戻す:** `sudo bash scripts/harden.sh --rollback ntp`（`systemd-timesyncd` を入れ直して有効にします）。

---

## 6. 毎月の点検

月に 1 回、**放送のない時間（9:55〜17:05 以外）に**行います。

1. `bash scripts/security_check.sh` を実行し、`[要対応]` が 0 件であることを確かめる（`[注意]` は内容を読んで判断する）。
2. 更新して、再起動する。

   ```bash
   sudo apt update && sudo apt full-upgrade
   ```

   ```bash
   sudo reboot
   ```

   カーネルは、再起動してはじめて新しくなります（`security_check.sh` の「更新したカーネルがまだ使われていません」が出なくなります）。
3. 再起動のあと、`python3 campus_chime.py --status` の末尾が「気になる点は見つかりませんでした。」であることを確かめる。
4. `sudo bash scripts/harden.sh` で、手順が意図した状態（適用済み・確定待ちが残っていないこと）であることを見る。

`harden.sh updates` を適用していても、この月 1 回の更新と再起動は必要です（自動更新は再起動しないため）。
日常のほかの点検は [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) 5 章に書いています。

---

## 7. 閉め出されたとき

鍵が使えない・防火壁で入れない、となったときの順序です。

1. **まず待つ。** `ssh` と `firewall` は確定しなければ、10 分（`--revert-after` で変えた分）で自動で元に戻ります。
   再起動した場合も、起動のあとに元に戻ります。そのあと、もう一度 SSH で入れます。
2. 待っても入れない、待てないとき: **Pi にディスプレイ（HDMI）と USB キーボードをつなぎ**、コンソールで `pi` とそのパスワードでログインします
   （SSH の設定にかかわらず、コンソールには入れます）。そして次を実行します。

   ```
   cd /home/pi/campus-chime
   sudo bash scripts/harden.sh --rollback ssh
   ```

   防火壁が原因なら `--rollback firewall` です。確定待ちをすべて戻すなら `sudo bash scripts/harden.sh --rollback-pending`、
   適用した手順をすべて戻すなら `sudo bash scripts/harden.sh --rollback` です。
3. それでも直らないときは、SD カードを書き直します（[SETUP.md](SETUP.md)。`config.json` はあらかじめ控えておきます）。

**コンソールのためにも、`pi` のパスワードを控えておいてください**（パスワード管理ソフトに保存します）。`sudo` の手順を適用すると、`sudo` にも
このパスワードが要ります。

---

## 8. GitHub 側の守り

Pi は `bash scripts/update.sh` のたびに、`main` の最新の内容を取り込み、**そのまま実行します**（サービスは `pi` の権限で動き、
取り込んだ `scripts/setup.sh` の `sudo` の行では root の権限も使います）。つまり、**`main` に入れる人のアカウントが乗っ取られると、
全部の Pi が乗っ取られます。** GitHub 側で次を行ってください。

1. **二要素認証を有効にする。** SMS よりも、パスキーかハードウェアセキュリティキーをおすすめします。
   **リカバリーコードは、紙などオフラインで保管します**（二要素認証に使う端末と同じ場所に置きません）。
2. **`main` を保護する。** リポジトリの Settings > Rules > Rulesets で、`main`（既定のブランチ）に対するルールを作り、
   次を有効にします。
   - Require a pull request before merging（直接 push せず、Pull Request を通す）
   - Block force pushes（履歴の書き換えを禁止）
   - Restrict deletions（`main` の削除を禁止）

   Enforcement status は Active にし、例外（Bypass list）は空にします。一人で運用していて、承認してくれる人がいないときは、
   承認の必要人数を 0 にしてかまいません（Pull Request を通すこと自体が、マージ前に内容を読む機会になります）。
3. **マージの前に、Pull Request の内容を必ず自分の目で読む。** とくに `scripts/*.sh`・`campus_chime.service`・`requirements.txt`・
   `.github/workflows/` の変更は、Pi の root の権限や、全部の機械に届きます。自分以外が出した Pull Request も、
   ツールが自動で出したものも同じです。
4. **GitHub のトークン（Personal access token）やデプロイ鍵を、Pi に置かない。** Pi は公開のリポジトリを読むだけなので、書き込みの権限は要りません。
   `git remote -v` が `https://github.com/...` であることを確かめます（点検でも見ます）。
5. 使っていないトークン・SSH 鍵・連携アプリは、GitHub の設定から取り消す。

---

## 9. PC 側（VOICEVOX）

作り置き音声を作る PC では、VOICEVOX ENGINE が動きます。**この ENGINE には、ログインの仕組みがありません。**
生成のスクリプトを**同じ PC で**動かすなら、その PC の中（`127.0.0.1`）だけで待ち受けるようにして、学内のほかの人から届かないようにします。

```bash
docker run -d --name voicevox -p 127.0.0.1:50021:50021 voicevox/voicevox_engine:cpu-latest
```

WSL2 から Windows 側の ENGINE へ `--base-url` で届かせる場合など、全部のインターフェースで待ち受ける必要があるときは
[SETUP.md](SETUP.md) 8 章に書いてあります。そのときは、作業の間だけにして、終わったら `docker stop voicevox` で止めてください。

SSH の秘密鍵は、パスフレーズをかけ、ディスクの暗号化（BitLocker・FileVault など）が有効な PC に置きます。

---

## 10. 乗っ取られたかもしれないとき

覚えのないログイン、`security_check.sh` が出す「リポジトリに管理外のファイルがあります」、放送が勝手に鳴る・急に鳴らない、
などで疑ったときの、短い手順です。

1. **切り離す。** Pi の電源を抜くか、ネットワーク（LAN ケーブル・Wi-Fi）を外します。調べる必要があるときは、
   SD カードを抜いて、そのまま保管します。大学の情報基盤の担当者にも連絡してください（学内の規則に従います）。
2. **作り直す。** 疑わしい SD カードは使わず、新しい SD カードに [SETUP.md](SETUP.md) の手順で OS から作り直します。
   `config.json` の内容は、控えがあれば見直してから戻します。
3. **秘密を変える。** Wi-Fi のパスフレーズ（変更に大学の担当者の手が要ることがあります）、`pi` のパスワード、SSH の鍵（作り直して、古い公開鍵は Pi から外す）。
   PC も疑わしいときは、PC のほうも調べます。
4. **GitHub を調べる。** 自分のアカウントの Settings の「Security log」に、覚えのないログインや設定の変更がないか。
   覚えのないトークン・SSH 鍵・連携アプリがあれば取り消す。リポジトリの履歴（`git log`）と、Actions の実行履歴に、覚えのない変更がないか。
5. 原因が分かったら、3 節の手順と `bash scripts/security_check.sh` で、同じことが起きないようにします。

---

## 11. 参考: `harden.sh` が置くもの

| 場所 | 内容 |
|---|---|
| `/var/lib/campus-chime-harden/` | 状態の置き場（root だけが読める）。適用の記録、変える前のファイルの控え、確定待ちの記録、操作の記録（`log`）、元に戻す処理の写し（`harden.sh`） |
| `/etc/ssh/sshd_config.d/01-campus-chime-harden.conf` | `ssh` |
| `/etc/campus-chime-harden/firewall.nft`・`/etc/systemd/system/campus-chime-firewall.service` | `firewall`（確定したときに作る） |
| `/etc/systemd/journald.conf.d/50-campus-chime-harden.conf` | `journal` |
| `/etc/apt/apt.conf.d/52campus-chime-unattended-upgrades` ほか | `updates` |
| `/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf` | `service` |
| `/etc/chrony/sources.d/campus-chime-harden.sources` | `ntp`（NTP サーバーを指定したときだけ） |
| `/etc/systemd/system/campus-chime-harden-revert.service`・`/etc/systemd/system/campus-chime-harden-revert.timer` | 確定待ちの間だけ置く、自動で元に戻すための設定 |

`sudo` の手順は、`/etc/sudoers.d/` のファイルを外すだけで、新しいファイルは作りません（外したファイルは控えの置き場にあります）。
