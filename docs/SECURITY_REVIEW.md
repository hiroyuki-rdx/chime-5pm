# セキュリティレビューの記録（SECURITY_REVIEW）

2026 年 10 月に行ったセキュリティレビューの結果と、その後の対応の状況の記録です。**公開版**です。

- **日付:** 2026-10-24（v6.2.0 の作成時）
- **この文書の役目:** 運用の手引き（手順・復旧・毎月の点検・脆弱性の報告の窓口）は [SECURITY.md](SECURITY.md) です。この文書は、その手順を「なぜ勧めるのか」の根拠と、
  まだ直していない点の記録です。
- **公開版に載せていないもの:** 悪用の手順になりうる細部（具体的な数値や再現の手順）と、個人に関わる情報です。詳細版は運用者が手元で保管しており、リポジトリには入っていません。
- **脆弱性を見つけたとき:** SECURITY.md 1 節に従ってください。公開の Issue には詳細を書かないでください。

---

## 1. 結論

1. 時報機のプログラム自体は小さく堅い造りです（外から待ち受ける口がなく、危険な関数も使っていません）。弱いのは、その**周り**（SSH・更新の経路・時計・OS の更新）です。
2. 「中」は 4 件です: SSH のパスワードログイン、`main` をそのまま取り込む更新の経路、同じネットワークの人から影響を受けうる時計、OS の更新の遅れ（実機が古い場合だけ）。
   いまの前提では「高」はありません。ただし **Pi が学外から届くなら、SSH のパスワードログインは「高」** です。
3. 先にやること: OS の更新と再起動 → SSH を鍵だけに（`harden.sh ssh`）→ 防火壁（`harden.sh firewall`）→ GitHub の二要素認証（パスキー）と `main` の保護ルール。
4. **実機の Pi は一度も見ていません。** バージョン、学外から届くか、`sudo` の設定など、実機しだいの判断は、`security_check.sh` と 7 章のコマンドで確かめてください。
5. コードの修正（時計の見張り、天気取得の時間制限ほか）は今後の候補です。急ぎではなく、相談のうえで決めます（6 章）。

---

## 2. 範囲と前提

### 2.1 運用者と決めた前提

| 項目 | 内容 |
|---|---|
| 調べた範囲 | 5 つの観点: 機械と OS、ネットワーク、アプリのコード、運用スクリプトと CI、Python と供給網。**実機の Pi では何も試していません**（作業用のコピーと偽の環境だけ） |
| 届く範囲 | Pi は学内ネットワークからだけ届く（ポート転送はしていない）。IPv6 は使われない見込み |
| ただし | 大学がクラス B／C の IPv4 アドレスの範囲を持つので、Pi が**グローバル IPv4 アドレス**を持つ可能性がある（2.2 節） |
| 機械 | Raspberry Pi 3B、Raspberry Pi OS Lite 32-bit（Bookworm、Debian 12）、利用者 `pi`、Wi-Fi のみ |
| 動き方 | `campus_chime.service`（`User=pi`）が `/home/pi/campus-chime` から動く。更新は人が `bash scripts/update.sh` で行う |
| 通信 | 出ていく通信は Open-Meteo（HTTPS）・NTP・apt・GitHub。入ってくる口は SSH（22）と mDNS（5353/udp）が主 |
| 体制 | GitHub の公開リポジトリ、所有者 1 人。変更の多くは AI が作る Pull Request を所有者がマージする |

### 2.2 グローバル IPv4 アドレスとは何か、点検スクリプトはどう判定するか

大学がクラス B（/16）やクラス C（/24）の IPv4 アドレスの範囲を持っていると、学内の機器にも「世界で唯一のアドレス」（グローバルアドレス）が配られることがあります。
その場合、Pi がインターネットから届くかどうかは**大学の入口の防火壁の設定だけ**で決まり、Pi の中からは見えません。

`security_check.sh` は Pi 自身のアドレスを `ip -4 -o addr show scope global`（IPv6 は `ip -6 ...`）で読み、1 つずつ分類します（項目名 `NET-ADDR4`・`NET-ADDR6`）。

| アドレス | 判定 |
|---|---|
| `10.`・`172.16.`〜`172.31.`・`192.168.` で始まる | プライベート。`[OK]`（学外から直接は届かない） |
| `100.64.`〜`100.127.` | 共有アドレス空間（キャリアグレード NAT）。`[OK]` |
| `169.254.` | DHCP に失敗した仮のアドレス。`[注意]` |
| 上のどれでもない | **グローバル**。`[注意]`。学外から確かめる方法を表示する |
| IPv6 | `fc00::/7`（学内専用）とアドレスなしは `[OK]`。それ以外はグローバルとみなして `[注意]` |

グローバルが 1 つでも見つかると、SSH のパスワードログインの行は「危険度は『高』」と表示され、最後のまとめに学外から確かめる手順（スマートフォンのテザリングにつないだ PC で
`nc -vz <Pi のアドレス> 22`）が出ます。これは「届く」と断定するのではなく、**「届くなら高とみなす」安全側の表示**です。実際に届くかは、学外から試すか、
大学の情報基盤の担当者に聞くしかありません。

なお、プライベートアドレスでも、大学が静的 NAT などで外へ公開していれば届きます。「ポート転送はしていない」という運用者の言葉を前提にしており、スクリプトには見えません。

### 2.3 この記録の根拠と限界

- 5 つの観点で調べ、各指摘を**別の目で再検証**し（反証も含む）、最後に抜けを調べる批評を通しました。この記録の重大度は、再検証のあとの**最終の値**です。
- 公式情報（CISA の KEV カタログ、Debian、GitHub の勧告）は別の調査で確かめました。原文を開けたものと、検索結果の要約だけのものがあります（8 章に区別して書きました）。
- 再現の実験は、すべて作業用のコピーと偽の環境で行いました。本物の Pi・本物のネットワーク・本物の GitHub は使っていません。この記録の作成中はネットワークにつないでいません。
- `security_check.sh` と `harden.sh` は、偽の環境（`CHIME_SYSROOT` と偽のコマンド）でテストしています。生成する防火壁のルールは、本物の `nft -c -f`（検査モード）で文法を確かめました。
  **本物の Pi の sshd・nft・systemd では一度も動かしていません**（7.1 節）。

### 2.4 重大度の目安

| 重大度 | この記録での意味 |
|---|---|
| 高 | 少ない条件で、Pi の乗っ取りや放送の悪用に届く（例: 学外から SSH のパスワードを試せる） |
| 中 | 学内の接続やアカウントの乗っ取りなど、現実にありうる条件がそろうと、Pi の乗っ取りや放送の長い停止・悪用に届く |
| 低 | さらに別の前提（先に Pi の中に入られている、相手が正規の通信相手になれる、など）が要る。または影響が小さい |
| 情報 | 直接の被害には届かない。衛生・将来のため・事故防止 |
| （目安） | 見落としとして指摘されたが、反証を含む再検証は通していないもの |

### 2.5 用語

| 言葉 | 平たく言うと |
|---|---|
| SSH／鍵 | 別の PC から Pi に入る仕組み／パスワードの代わりに使う、PC に置く暗号の鍵ファイル |
| `sudo` | 管理者（root）の権限で命令を実行する仕組み。Raspberry Pi OS は既定でパスワードなしで使えることが多い（この Pi では未確認） |
| 防火壁（firewall） | 入ってくる通信を、許したものだけに絞る門番。ここでは nftables |
| NTP／NTS／chrony | ネットワークで時計を合わせる仕組み／それを暗号で守る方式／時計合わせのソフト（`systemd-timesyncd` の代わり） |
| DNS | 名前（`example.com`）から相手のアドレスを引く仕組み |
| CVE／KEV | 脆弱性の通し番号／アメリカ政府機関（CISA）が「実際に悪用された」と確認した脆弱性の一覧 |
| PSK | Wi-Fi の共通のパスワード |

---

## 3. 指摘の一覧

### 3.1 一覧（重大度順）

17 のまとまり（テーマ）にしました。**状況**の言い方は次のとおりです。

- **`harden.sh ○○` で対策できる:** v6.2.0 の `scripts/harden.sh` の手順で、運用者が Pi で直せます（手順ごとの説明は SECURITY.md 5 節）。実機ではまだ動かしていません（7.1 節）。
- **GitHub の設定で対策（運用者が行う）:** GitHub の画面で設定します（5 章）。
- **コードの修正は今後の候補:** リポジトリのコードを直す案です。まだ入れていません（6 章）。
- **文書を直した:** v6.2.0 の文書に反映済みです。
- **点検で見つかる:** `security_check.sh` が見つけて知らせます。

| # | 重大度 | 何が起きうるか（前提） | 対策 | 状況 |
|---|---|---|---|---|
| 1 | 中（学外から届く・ゲスト用の Wi-Fi・推測されやすいパスワードなら **高**） | SSH がパスワードでのログインを受け付けたままだと、同じネットワークの人がパスワードを試し続けられる。当たると Pi の中に入られ、`sudo` がパスワードなしなら管理者の権限まで届く。放送の悪用、Wi-Fi のパスワードの流出、学内への足がかりになりうる。前提: 学内の同じネットワークから届くこと。実機の sshd の設定は未確認 | 鍵だけにする（`harden.sh ssh`）→ 管理用のネットワークだけに SSH を許す（`harden.sh firewall` の `--ssh-from`）→ `sudo` にパスワードを求める（`harden.sh sudo`）。`fail2ban` は入れない（3.3 節） | `harden.sh ssh`・`firewall`・`sudo` で対策できる。文書を直した（SETUP.md は公開鍵認証を勧める書き方に） |
| 2 | 中 | GitHub の `main` に悪意のある変更が入ると、次に `update.sh` を実行した Pi すべてで、その中身が実行される（`pi` の権限で、`sudo` の行では管理者の権限で）。Pi が学内専用でも関係ない（Pi は GitHub へ出ていくだけ）。前提: `main` に書き込める人のアカウントやトークンの乗っ取り、または中身を読まずにマージした Pull Request。そのあと運用者が `update.sh` を実行すること | GitHub 側の守りが最も安く効く（5 章）。さらに署名つきのデプロイ用タグ（6 章 8）。**`sudo` にパスワードを求めても、この経路は守れない**（取り込んだコードは運用者の画面で動くため） | GitHub の設定で対策（運用者が行う）。署名つきタグはコードの修正は今後の候補。文書を直した（SECURITY.md 8 節） |
| 3 | 中 | NTP は暗号で守られていない。同じネットワークで時刻合わせの通信に手を加えられる人がいると、時計が狂い、違う時刻に鳴る・鳴らない日ができる。ずれが戻ったあとも影響が残ることがある。そうした位置にいる人がいるときだけ起きる話で、広く起きるものではないが、学内の公共の場に鳴る機械なので「中」 | `chrony` に替える（`harden.sh ntp`。起動後の大きな時刻の飛びを受け付けない）。**NTP 自体は暗号化されず、NTS は未設定なので、部分的な対策**。アプリに時計の見張りを足す案（6 章 2） | `harden.sh ntp` で対策できる（部分的）。コードの修正は今後の候補 |
| 4 | 条件つきで中（実機が古いときだけ。そうでなければ情報） | OS・カーネル・SSH・avahi・BlueZ などの更新が遅れていると、既知の脆弱性が残る。とくに CISA の KEV に載ったカーネルの権限昇格が 6 件ある（8.1 節）。**単独で外から入る穴ではなく**、先に Pi の中に入った人が管理者になるのを助ける。以前の手順書は、初回の更新と学期ごとの `apt upgrade` だけで、再起動の決まりもなかった。前提: 実機のパッケージが修正より古い場合のみ（実機は未確認） | 今すぐ `sudo apt update && sudo apt full-upgrade` と再起動。以後は毎月（9:55〜17:05 を避ける）。使わない常駐を止める（`harden.sh services`）。自動更新（`harden.sh updates`）は任意で、再起動はしない | `harden.sh services`・`updates` で対策できる。文書を直した（SECURITY.md 6 節、KNOWLEDGE_BASE.md 5 章の毎月の更新と再起動）。古さは `security_check.sh` が見つける |
| 5 | 低 | 放送サービスの保護が薄い（`systemd-analyze security` で 8.7、EXPOSED）。サービスが乗っ取られた場合に、自分のコードやホームの中を書き換えられ、任意の通信ができる。前提: 先にサービスの中でコードが動く必要がある（いま入口は見つかっていない）。`NoNewPrivileges` が付いており、`sudo` への昇格はできない | 追加設定（`harden.sh service`。ユニット本体は変えない）。適用の前後で点を表示する。**音が出なくなる恐れがあるので `harden.sh --test-sound` で確かめる** | `harden.sh service` で対策できる |
| 6 | 低（名前解決の答えに手を加えられる人がいるなら中） | 天気の取得は、放送を回す主ループの中で行う。取得全体の時間制限がなく、通信相手のふるまいしだいで取得が長引き、その回の放送の準備が遅れる・抜けることがある。ほかに、正しい証明書を持つ相手（Open-Meteo 側の乗っ取りなど）が巨大な応答や `http` への転送を返した場合の、メモリ不足や誤った天気の一文。前提: #3 と同じ種類の人、または Open-Meteo 側の問題 | 取得全体の期限、読み取りの上限、`https` 以外への転送の拒否（6 章 1）。Pi 側の対策はない | コードの修正は今後の候補 |
| 7 | 低 | 防火壁がないと、sshd・avahi・Bluetooth・ModemManager が同じネットワークや電波の届く範囲に見えている。Bluetooth（BlueZ）と avahi に既知の脆弱性の報告がある（8.3 節）。前提: 同じネットワークまたは電波の届く範囲。実機でどれが有効かは未確認 | 防火壁（`harden.sh firewall`）と、使わない常駐の停止（`harden.sh services`）。avahi は `campus-chime.local` に使うので止めない。Bluetooth を根元から切るなら `/boot/firmware/config.txt` に `dtoverlay=disable-bt`（`harden.sh` にはない。再起動が要る。未試験） | `harden.sh firewall`・`services` で対策できる |
| 8 | 低 | SD カードに Wi-Fi のパスワード・`pi` のパスワードのハッシュ・SSH のホスト鍵が暗号化されずに入る。カードを抜かれると読まれる。共有の Wi-Fi のパスワードを知る人は、#3 の前提になる位置に立てる。前提: 設置場所に人が触れる。共有パスワードの Wi-Fi。Imager が書く設定の中身（パスワードの保存のしかた）は未確認 | 設置場所の管理、専用の SSID か有線、Wi-Fi のパスワードの変更手順（SECURITY.md 10 節）。スクリプトでは直せない | 文書を直した（運用で対処する） |
| 9 | 低（開発 PC）／情報（Pi） | 他人のブランチを取り込んだとき、`assets/voice/manifest.json` の書き方しだいで、作り置きの掃除（`generate_voicevox.py --prune`）が意図しない場所のファイルを消すことがある（再現済み）。Pi 側は、読めるファイルを再生用に開くだけで、リポジトリを書き換えられる人にはもともと可能なこと | 作り置きのフォルダの外を指すファイル名を拒む（6 章 5）。**他人のブランチを取り込んだ直後は `--prune` を実行しない** | コードの修正は今後の候補 |
| 10 | 低 | 運用の事故。`sudo` で `campus_chime.py` を動かすと管理者の持ち主のファイルが残り、サービスが書けなくなることがある。手で起動したものとサービスが並ぶと放送が 2 回鳴ることがある（コードの読みだけ。未実行）。時報音の WAV が書き込み中の停電で壊れることがある（`setup.sh` が毎回作り直すので、影響する窓は小さい）。攻撃者は要らない | 管理者で実行したときの案内の拡張、同時に 1 つしか動かさない仕組み、WAV の原子的な書き込み（6 章 4・6・7） | コードの修正は今後の候補 |
| 11 | 情報 | `config.json`・`cache/state.json` などに極端な値や特殊なファイルがあると、起動時に落ちる・止まる。書き込める人（`pi`、運用者、リポジトリを変えられる人）だけが起こせ、新しい境界は越えない | 失敗しても落ちない読み込み（6 章 3） | コードの修正は今後の候補 |
| 12 | 情報 | `requirements.txt` を `pip3 install` で入れると、`~/.local` の pygame が apt の版より優先される。版の固定・ハッシュなし。前提: `pip` を実際に使った場合のみ（文書は apt を勧める）。実機は未確認 | apt で入れる | 点検で見つかる（`security_check.sh` の `PY-USERSITE`） |
| 13 | 情報（Actions の固定は低） | GitHub Actions の外部アクションがメジャーなタグでの指定（`tag.yml` は `contents: write`）。Dependabot の設定なし。CI の Python 3.9 は寿命切れ。公開の履歴にコミットした人のメールアドレスが残る。Open-Meteo に座標と User-Agent が見える。NetworkManager の接続確認（平文の HTTP）が有効かは未確認 | Dependabot、Actions の権限を読み取りに、コミットの SHA での固定は次の Pull Request で | GitHub の設定で対策（運用者が行う）。コードの修正は今後の候補 |
| 14 | 情報（目安） | 失敗の通知がない。乗っ取られても鳴り続けるなら気づけない。止まったまま動いている（ハングした）プロセスにも気づけない（`WatchdogSec` なし）。ログが再起動で消える設定かもしれない（未確認）。他の指摘の発見が遅れる、という種類の弱点 | ログを残す（`harden.sh journal`）、毎月の `security_check.sh` と `--status`。見張りの仕組みは 6 章に候補 | `harden.sh journal` で対策できる。見張りはコードの修正は今後の候補 |
| 15 | 情報（目安） | 侵入されたときの手順がなかった（作り直しに 60〜90 分）。バックアップのイメージには Wi-Fi のパスワード・`pi` のパスワードのハッシュ・SSH のホスト鍵が入る。固有の状態は `config.json` だけ | SECURITY.md 10 節に短い手順を書いた。イメージは暗号化かオフラインで保管する。`config.json` の控えを取る | 文書を直した |
| 16 | 情報（目安。ただし #2 と直結） | 所有者が 1 人で、連絡先のメールが学内のもの。卒業・メールの失効・アカウント名の変更や削除で、回復できなくなる、または**古い名前を別人に取られ、Pi の `git pull` がその別人のものを受け入れてしまう**（リポジトリの乗っ取り）。前提: 引き継ぎの時期。Pi の origin は名前入りの URL | 卒業後も使える回復用メール、パスキー、回復コードの紙での保管、第 2 の管理者か組織への移管、名前を変えない。署名つきタグがあれば緩和される | GitHub の設定で対策（運用者が行う） |
| 17 | 低〜情報（目安） | VOICEVOX ENGINE が全部のインターフェースで待ち受けていた（以前の文書の例）ので、学内の人が使える。作り置き音声の WAV はコミットされて Pi で鳴る。開発 PC は push の資格情報（と署名鍵）を持つ。AI に Issue や Pull Request を自動で読み書きさせる運用では、入力の混入口になりうる。前提: 開発 PC が共用のネットワークにある、または AI に自動で触らせる | `-p 127.0.0.1:50021:50021`（SECURITY.md 9 節）、`.github/CODEOWNERS`、AI には Pull Request だけを出せるトークン | 文書を直した（VOICEVOX の例）。GitHub の設定で対策（運用者が行う）。コードの修正は今後の候補 |

### 3.2 中の 4 件を平たく言うと

- **#1 SSH:** 玄関の鍵が「覚えやすいパスワード」だけで、玄関のドアは学内の誰にでも見えている状態です。鍵（暗号の鍵ファイル）だけにすれば、パスワードを当てる攻撃は成り立ちません。
- **#2 更新の経路:** Pi は「GitHub の `main` に書いてあること」を信じて実行します。`main` に書ける人のアカウントが乗っ取られると、その人は全部の Pi を動かせます。
  Pi の守りを固めても防げないので、**GitHub 側の守りが一番効きます**。
- **#3 時計:** 時報機は時計だけを頼りに鳴ります。時計をだまされると、違う時刻に鳴ったり、鳴らない日ができたりします。現実の相手は「同じネットワークの中で通信に手を加えられる人」で、
  広く起きる話ではありませんが、学内の公共の場に鳴る機械なので「中」です。
- **#4 更新の遅れ:** 古いままの OS は、ほかの問題（#1 など）で中に入られたときに、被害を広げる足場になります。実機のバージョンを見るまでは「中」とも「情報」とも断定できないので「条件つき」にしています。

### 3.3 再検証で訂正・却下された点（誤って対策しないために）

| 元の主張 | 結果 |
|---|---|
| 「ジャーナルが無制限に SD カードを埋める」 | 誤り。既定で上限（ディスクの 10 %、最大 4 GB）があり、空きも残す。残る論点は「再起動で消えるか」だけ（#14） |
| 「通信を遅らせるだけで、天気の取得を止め続けられる」 | 成り立たない。TLS の読み取りには全体の期限がある。注意が要るのは、名前解決の答えに手を加えられる場合（#6） |
| 「`sudo` にパスワードを求めれば、更新の経路も守れる」 | 守れない（#2）。守れるのは「SSH の足がかりから管理者へ」「サービスの乗っ取りから管理者へ」の 2 つ |
| 「`fail2ban` を入れる」 | 入れない。細工した入力で任意の IP を締め出せてしまう脆弱性の報告がある（8.3 節、**調**）。鍵だけの SSH と防火壁で足りる |
| 「`unattended-upgrades` の標準の設定でよい」 | 足りない。標準の対象は Debian の配布元の名前で、Raspbian・Raspberry Pi の配布元に当たらない。`harden.sh updates` は `apt-cache policy` から対象を作る（実機で当たるかは未確認） |
| 「ユニットに `ProtectClock=true` を足す」 | 足さない。`DevicePolicy=` などと組み合わさると `/dev/snd` を閉じて音が出なくなる恐れがある。得られる点は約 0.1 だけ。`harden.sh service` の追加設定には入れていない |
| 「`git pull --verify-signatures` を `main` に使う」 | 使えない。Pull Request のマージは GitHub 名義のコミットで、全部弾くか、GitHub の鍵を信じて意味がなくなる。署名は別の名前空間のタグで行う（6 章 8） |
| 「chrony に NTS を足す」 | 足さない。認証なしの既定の行を残すと意味が薄れ、TCP 4460 と、ほぼ正しい起動時の時計も要る。`harden.sh ntp` は NTS を設定しない（#3 は部分的な対策） |
| 「`--status` の出力に危険がある」 | 運用者自身の画面に出す診断なので、却下（指摘にしない） |

### 3.4 再検証で重大度が変わったもの

観点ごとに出た初期の重大度が、別の目での再検証で変わった主なものです。

- **天気の取得（#6）:** 初期の「中」を、検証で「低」に下げました。名前解決の答えに手を加えられる人がいるなら中です。
- **`main` に保護ルールがないこと:** 「中」から「低」へ。別の弱点ではなく、#2 への入口だからです。**ただし最も安い対策なので、優先は高く扱います**（5 章）。
- **OS の更新の遅れ（#4）:** 実機のバージョンを見るまで断定できないため、「条件つきで中」としました。
- **SSH のパスワードログイン（#1）と更新の経路（#2）、時計（#3）:** 再検証のあとも「中」のままです。
- **#14〜#17:** 最後の批評で「見落とし」として挙がったもので、反証を含む再検証は通していません（目安）。
- **却下:** 3.3 節の表のとおりです。

---

## 4. Pi でやること（順番どおり）

放送のない時間（9:55〜17:05 以外）に行います。**`ssh` と `firewall` は、確定しないと 10 分（既定。`--revert-after` で 1〜60 分）で自動で元に戻ります**（再起動しても戻ります）。
始める前に、Pi に **HDMI ディスプレイと USB キーボード**をつなげるようにして、`pi` のパスワードを控えてください（鍵が使えなくなったときの最後の入り口です）。
詳しい説明は SECURITY.md の 3 節〜7 節です。ここでは順番と、使うコマンドだけを並べます。

| 順 | すること | コマンド |
|---|---|---|
| 0 | 管理に使う PC のネットワーク（例: `192.168.1.0/24`）を調べる。手順 6 の `--ssh-from` に使う | Windows は `ipconfig`、macOS／Linux は `ip a` か `ifconfig` |
| 1 | 現状を点検する（読むだけ。何も変えない）。`[要対応]` があると終了コードが 1 になる | `bash scripts/security_check.sh` と `sudo bash scripts/security_check.sh`（root が要る項目も見る） |
| 2 | グローバルアドレスが出たら、学外（スマートフォンのテザリングなど）から届くかを確かめる（2.2 節） | `nc -vz <Pi のアドレス> 22`（Windows は `Test-NetConnection <Pi のアドレス> -Port 22`） |
| 3 | OS を更新して再起動する（カーネルの修正は再起動してはじめて効く）。そのあと点検で `OS-KERNEL`・`OS-REBOOT` が `[OK]` になることを見る | `sudo apt update && sudo apt full-upgrade`、`sudo reboot`、`python3 campus_chime.py --status`、`bash scripts/security_check.sh` |
| 4 | 自分の PC で SSH の鍵を作り、Pi に登録する（SECURITY.md 3.1 節） | `ssh-keygen -t ed25519`、`ssh-copy-id pi@campus-chime.local` |
| 5 | SSH を鍵だけにする。まず `--dry-run` で内容を見る。適用したら、**新しい端末**で鍵で入れること・パスワードでは断られることを確かめ、その新しい端末で確定する | `sudo bash scripts/harden.sh --dry-run ssh`、`sudo bash scripts/harden.sh ssh`、`sudo bash scripts/harden.sh --confirm` |
| 6 | 防火壁を入れる。手順 5 と同じ流れで確定する。`--ssh-from` は管理に使うネットワークだけにする | `sudo bash scripts/harden.sh --dry-run firewall --ssh-from 192.168.1.0/24`、`sudo bash scripts/harden.sh firewall --ssh-from 192.168.1.0/24`、`sudo bash scripts/harden.sh --confirm` |
| 7 | 使わない常駐を止め、ログを残す。Bluetooth のスピーカーを使っている疑いがあるときは、何も変えずに止まる | `sudo bash scripts/harden.sh services journal` |
| 8 | `sudo` にパスワードを求める（端末で。`pi` のパスワードを控えてあること）。以後、`update.sh` と `setup.sh` は `sudo` の行でパスワードを聞く | `sudo bash scripts/harden.sh sudo` |
| 9 | 任意の手順を、放送のない時間に 1 つずつ。`service` は適用のあとに、スピーカーから実際に鳴らして確かめる | `sudo bash scripts/harden.sh updates`、`sudo bash scripts/harden.sh ntp`、`sudo bash scripts/harden.sh service`、`sudo bash scripts/harden.sh --test-sound` |
| 10 | 最後の確認。`[要対応]` が 0 件で、各手順が「適用済み」、「確定待ち」が残っていないこと | `bash scripts/security_check.sh`、`sudo bash scripts/harden.sh`、`python3 campus_chime.py --status` |

補足です。

- `OS-KERNEL` の `[OK]` は、「カーネルの作成日が 2026-07-04 より新しい」という**目安**にすぎません。Raspberry Pi のカーネルに各修正がいつ入ったかは確かめられておらず、
  `[OK]` は 6 件すべてが直った証明ではありません。古い日付なら、未修正とみなして更新します。
- `--ssh-from` を付けないと、Pi と同じネットワーク全体に SSH を許します。管理に使うネットワークだけを書くのをおすすめします。
  いまつながっている SSH の相手が許す範囲に入っていないと、何も変えずに止まります。
- 変更の前に開いた画面からは確定できません（新しい画面から）。公開鍵が未登録のときは、何も変えずに止まります。
- `updates` の自動更新の対象は `apt-cache policy` から作ります。適用のあと、`sudo unattended-upgrade --dry-run -d` で対象が出るかを確かめます（未実行）。
  カーネルは再起動するまで効かないので、毎月の更新と再起動は続けます。
- `ntp` は `systemd-timesyncd` を `chrony` に替えます。再同期は `sudo systemctl restart chrony` になります（KNOWLEDGE_BASE.md 4-5）。
- `service` は放送サービスを再起動します（放送の終わりを待ってから）。SDL2／ALSA を保護の下で動かす試験は、32 ビット ARM では行えていません。音が出なければ
  `sudo bash scripts/harden.sh --rollback service`（KNOWLEDGE_BASE.md 4-12）。

**閉め出されたら:** まず 10 分待ちます（確定しなければ自動で戻ります）。待てないときは、Pi に HDMI とキーボードをつないで `pi` でログインし、
`cd /home/pi/campus-chime && sudo bash scripts/harden.sh --rollback ssh` を実行します。防火壁なら `--rollback firewall`、確定待ちを全部戻すなら `--rollback-pending`、
適用したものを全部戻すなら `--rollback` です（SECURITY.md 7 節、KNOWLEDGE_BASE.md 4-10）。

**毎月**（放送のない時間に）: `bash scripts/security_check.sh` → `sudo apt update && sudo apt full-upgrade` → `sudo reboot` → `python3 campus_chime.py --status`（SECURITY.md 6 節）。

---

## 5. GitHub でやること

画面の名前は GitHub の英語表示のものです。この記録の作成中は GitHub につないでいないため、**現在の画面と違う場合があります**（未確認）。名前が違ったら、近い項目を探してください。

### 5.1 アカウントの守り（右上のアイコン > Settings）

| やること | どこで | ひとこと |
|---|---|---|
| 二要素認証をパスキーかセキュリティキーにする | Password and authentication > Two-factor authentication で、Passkeys／Security keys を追加 | SMS より、パスキーかハードウェアキー |
| 回復コードを紙で保管する | 同じ画面の Recovery codes を表示して、印刷するか書き写す | 二要素認証に使う端末と別の場所に置く |
| 回復用のメールを、卒業後も使えるものにする | Emails の Primary email address と Backup email address | 学内のメールは失効しうる（#16） |
| 連携アプリを確認し、使わないものを外す | Applications の Installed GitHub Apps／Authorized OAuth Apps | `Contents: write` が付いたものがないか見る |
| トークンを整理する | Developer settings > Personal access tokens | classic は削除する。fine-grained は期限つきで、このリポジトリだけにする |
| 覚えのない SSH 鍵・署名鍵を消す | SSH and GPG keys | |
| 覚えのないログインがないか見る | Archives の中の Security log | 月に 1 回、点検のついでに |

### 5.2 リポジトリの守り（リポジトリを開いて Settings）

| やること | どこで | ひとこと |
|---|---|---|
| `main` の保護ルール | Rules > Rulesets > New ruleset > New branch ruleset | 名前は `protect-main`、Enforcement status は Active、Bypass list は空、対象は既定のブランチ。Restrict deletions／Require a pull request before merging（承認の人数は 0 でよい）／Block force pushes をオンにする |
| 脆弱性の通知・秘密の混入の防止 | Code security（Advanced Security と表示されることもある）の Dependabot alerts／Dependabot security updates／Secret scanning／Push protection | Dependabot の「版の更新」（Actions の固定の更新）には、リポジトリの `.github/dependabot.yml` が要る（今後の候補） |
| 脆弱性の非公開の報告窓口 | Code security の Private vulnerability reporting | SECURITY.md 1 節が参照している |
| Actions の権限を読み取りだけに | Actions > General > Workflow permissions で Read repository contents and packages permissions。「Allow GitHub Actions to create and approve pull requests」は外す。Fork pull request workflows の項目で、外部の人の Pull Request には承認を必須にする | `ci.yml` は `contents: read`、`tag.yml` は自分で `contents: write` を宣言しているので、影響は出ない見込み（未確認） |
| 書き込める人・アプリの確認 | Collaborators（または Collaborators and teams）、GitHub Apps | 書き込み権限のあるものを数える。第 2 の管理者を検討する |
| 引き継ぎの備え | General > Danger Zone > Transfer ownership（組織への移管） | アカウント名・リポジトリ名は変えない。変えたら Pi の `git remote set-url origin ...` も直す |

保護ルールについて、正確に言っておきます。

- ルールは「トークンが `main` へ直接 push する」ことを止めます。所有者のアカウントが**丸ごと**乗っ取られた場合は止めません（設定を変えられるため）。だからアカウントの守り（5.1 節）が先です。
- 承認 0 人のままでも、Pull Request を通す手間が「マージ前に自分で読む」機会になります。**`scripts/*.sh`・`campus_chime.service`・`requirements.txt`・`.github/workflows/` の変更は、
  Pi の管理者の権限に届く**ので、必ず自分の目で読んでください（AI が出した Pull Request も同じです）。
- 「必須のステータスチェック」は、最初は付けずに始めてください。チェックの名前を間違えると、全部の Pull Request がマージできなくなります。付けるときは、成功した Pull Request の Checks 一覧に出る名前を選びます。
- `tag.yml` はタグだけを push するので、ブランチのルールには当たらない見込みです（GitHub の仕様の理解による。未確認）。Pull Request は「Create a merge commit」か「Squash and merge」でマージします（`tag.yml` の注意書き）。
- Pi の origin は `https://` のままにし、トークンや書き込みの鍵を Pi に置かないでください（`security_check.sh` の `REPO-REMOTE` が見ます）。

---

## 6. コードの修正候補（今後、要相談）

どれも**まだリポジトリには入れていません**。作業用のコピーで試作して全テストが通ることを確かめたものと、考え方だけのものがあります。優先度は A（先に）・B・C です。

| # | 優先 | 内容 | 触る所 | 注意 |
|---|---|---|---|---|
| 1 | B | **天気取得:** 取得全体の期限（放送の準備の余裕より短く）、読み取りの 1 MB の上限、`https` 以外への転送の拒否 | `chime/weather.py`（`fetch_json`）、`chime/tts.py`（同じ読み方） | 転送の拒否は `build_opener(...).open` ではなく、**`install_opener` を 1 回行い、`urllib.request.urlopen` を呼び続ける**。`build_opener` にするとテストが失敗し、テストのネットワーク遮断（`tests/support.py` の `block_network`）も効かなくなる。つまり `urlopen` はテストの接続点として残す |
| 2 | A | **時計の見張り:** 起きるたびに、壁時計の進みと単調時計の進みを比べ、大きく食い違ったら WARNING を出して「今日は放送済み」の記録（`last_fired`）を消す | `chime/app.py`（`run_forever`）、`chime/state.py` | 日付だけを比べる見張りでは足りない。副作用は、猶予の中にずれが来たとき、最大 1 回余分に鳴る程度。`tests/test_state.py` の更新が要る。電源投入直後（時刻同期の前）に古い時計で鳴る問題は、設計上の割り切り（REQUIREMENTS.md NFR-04）のまま |
| 3 | B | **JSON の読み込みを落ちないように:** 極端な値を `JsonFileError` に直す、サイズの上限、通常のファイルだけを開く | `chime/jsonfile.py`（`read_json`） | 書き込める人にしか起こせないので、緊急ではない（#11） |
| 4 | B | **単一起動のロック:** 二重に動かない（`flock`） | `campus_chime.py` の起動部分 | コードの読みだけで、未実行（#10） |
| 5 | C | **マニフェストのファイル名を閉じ込める:** 作り置きのフォルダの外を指すファイル名を拒む（実行時の検索と `generate_voicevox.py --prune` の両方） | `chime/tts.py`、`scripts/generate_voicevox.py` | #9 |
| 6 | C | **管理者で実行したときの案内を、他の CLI のモードにも:** いまは `--pause-*` だけ | `chime/cli.py` | `setup.sh` は管理者でも動き、終了コード 2 を「`config.json` を読めない」に割り当てている。ガードと衝突するので、`setup.sh` に `update.sh` と同じ早めの確認を入れるか、別の終了コードにする |
| 7 | C | **`time_signal.wav` を原子的に書く**（一時ファイルに書いて `os.replace`） | `chime/timesignal.py`（`generate_time_signal`） | `--generate-assets` が `setup.sh` から毎回走るので、実害の窓は小さい（#10） |
| 8 | A（効果大、手間も大） | **署名つきデプロイタグ（任意）:** `/etc/campus-chime/deploy-fingerprint` があるときだけ、`refs/tags/deploy/*` を取り込み、固定した指紋で署名された最新のタグへ `git merge --ff-only`。なければ従来どおり `git pull --ff-only`。「検証できる版がない」は終了コード 65 | `scripts/update.sh`、テスト（偽のリポジトリは署名なしなので**任意**にする）、文書 | 名前空間は **`deploy/vX.Y.Z`**。`tag.yml` が作る `vX.Y.Z` は署名なしの bot のタグで、使うと毎回止まる。`git verify-tag` は、信頼していない鍵でも正しい署名なら 0 を返すので、`--raw` の `VALIDSIG` で指紋を固定する。署名は**その下の履歴全部**を保証するので、署名の前に先端を読む。毎回署名が要り、鍵をなくすと更新が止まる。`update.sh` は自分を途中で更新しないので、最初の 1 回は手で `git pull`。**GPG（git 2.43）だけ試験。SSH 署名と Bookworm の git 2.39 は未試験**。試作のパッチはリポジトリに残していない（作り直しになる） |

そのほかの候補です。

- **見張り:** `campus_chime.service` の `WatchdogSec` と `sd_notify`（ハング対策）、または `/etc/systemd/system.conf.d/` の `RuntimeWatchdogSec=15`（OS のハングだけ。先に `ls /dev/watchdog*`）。止まったままの `aplay` には前者が要ります。
- **`scripts/audit.sh`:** 月 1 回の点検をまとめる読み取り専用のスクリプト。`security_check.sh` が多くを兼ねているので、足りない所だけ。
- **`.github/CODEOWNERS`**（`campus_chime.service`・`scripts/`・`.github/`・`requirements.txt`・`chime/audio.py`）、**`.github/dependabot.yml`**、Actions の**コミットの SHA での固定**。
- **ユニットの強化をリポジトリ側で行う場合:** `setup.sh` で `mkdir -p cache assets/generated` を先に行い、SPECIFICATION.md 6.1 章を `campus_chime.service` と一致させます（`tests/test_docs.py` が比べます）。
  書き込み先が無いとサービスが起動しないためです。`harden.sh service` は追加設定とフォルダの作成でこれを避けているので、急ぎません（SPECIFICATION.md 6.3 章）。
- **文書:** OS の版（Bookworm）の明記、`requirements.txt` の pip の注意、CI の Python 3.9 の扱い。

---

## 7. 確かめられなかったことと、運用者への質問

### 7.1 確かめられなかったこと

| 項目 | なぜ | 影響 |
|---|---|---|
| 実機の Pi の状態すべて（アドレス・有効なサービス・sudoers・sshd の設定・バージョン） | 実機に触れていない | #1・#4 の重さが変わる。7.2 節のコマンドで確かめる |
| 学外から Pi に届くか | 大学の防火壁は Pi の中から見えない | 届くなら #1 は「高」 |
| カーネルの KEV 修正が、Raspberry Pi の apt のカーネルに入った時期 | 公式のページが見つからなかった。パッケージ名も推測（`linux-image-rpi-v7` か `raspberrypi-kernel` か） | `OS-KERNEL` の日付（2026-07-04）は目安。CVE-2025-39682・CVE-2025-38352 の修正版は未確認 |
| Raspbian が Debian の LTS の更新（例: python3.11 の `3.11.2-6+deb12u9`）を再構築するか | Raspbian の配布元を調べていない | 「更新したのに直っていない」可能性。32 ビット版の寿命の見通しも未確認 |
| Wi-Fi の種類（共有パスワード・ゲスト・企業認証）、クライアント隔離、PMF／WPA3 | 実環境の情報 | #3・#8 の重さ。7.3 節の質問 |
| `systemd 252`（Pi）と `chrony 4.3` の実際の動き | 作業環境は systemd 255 | `ntp`・`service` の効果と副作用。1 つずつ試す |
| 保護（sandbox）の下での SDL2／ALSA の動き（armhf） | 試験できない | 音が出なくなる恐れ。`harden.sh --test-sound` と `harden.sh --rollback service` |
| `harden.sh` の `ssh`・`firewall`・`sudo`・`ntp`・`updates` の実機での動き | 偽の環境でのテストのみ | 最初は `--dry-run` で内容を確かめ、1 手順ずつ |
| `harden.sh` の途中に中断や外部コマンドの失敗が入ったときの動き | 偽の環境での試験で、元に戻す処理や確定の処理が実際と合わない場合が見つかり、直っていないものがある（DEVELOPMENT_LOG.md の 2026-10-24） | 実機では `--dry-run` で内容を確かめ、1 手順ずつ、HDMI とキーボードを手元に置いて行う（SECURITY.md 7 節）。直したらこの行を消す |
| 署名タグの SSH 署名版、Bookworm の git 2.39 | 試験環境に無い | 6 章 8 |
| GitHub の画面の名前・ルールの実際の効き方 | つないでいない | 5 章 |
| Debian の通常の支援の終了日 | 資料で 2026-07-11／07-12／（古い見込みの）06-11 とずれがある。記事の原文は開いていない | 8.2 節 |
| 個々の CISA の警告ページ | 検索結果の要約のみ。カタログの JSON は開いた | 8.1 節 |

### 7.2 実機で確かめるコマンド（読むだけ。何も変えません）

```bash
cd /home/pi/campus-chime
sudo sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|allowusers) '
sudo ls /etc/sudoers.d/ && sudo cat /etc/sudoers.d/010_pi-nopasswd     # NOPASSWD の有無
systemctl is-enabled ssh avahi-daemon bluetooth ModemManager systemd-timesyncd chrony
uname -r -v
dpkg -l openssh-server avahi-daemon bluez sudo python3 python3-pygame
apt-cache policy raspberrypi-kernel linux-image-rpi-v7             # カーネルのパッケージ名と版
apt-cache policy | grep -E '^ +release'                             # 配布元（updates の対象になる）
grep -c '^Valid-Until' /var/lib/apt/lists/*InRelease                # 0 なら、古い一覧を差し込まれうる
timedatectl show-timesync -p ServerName --value; timedatectl status
ls /var/log/journal                                                 # 無ければ再起動でログが消える
nmcli connection show                                               # IPv6 を使わない場合の確認
ls ~/.local/lib/python3*/site-packages 2>/dev/null                  # pip の pygame があるか
systemctl show campus_chime.service -p DevicePolicy -p DeviceAllow  # 空か auto であること
systemd-analyze security campus_chime.service --no-pager | tail -3
ls /dev/watchdog*
dpkg -l | grep -i connectivity                                      # NetworkManager の接続確認の設定があるか
```

### 7.3 運用者への質問

1. Pi のアドレスは何ですか（`security_check.sh` の `NET-ADDR4`／`NET-ADDR6`）。学外から 22 番に届きますか。情報基盤の担当者に聞けますか。
2. どの Wi-Fi ですか（共有パスワードなら何人が知っているか、ゲスト、企業認証）。クライアント隔離はありますか。近くに有線の口はありますか（DHCP の予約、MAC 登録）。
3. 管理は、どの PC から、どのネットワーク（固定のアドレスか）から行いますか（`--ssh-from` に書く値です）。
4. `sudo` がパスワードなしなのは、意図したものですか。更新のたびに `update.sh` でパスワードを入力しても構いませんか。
5. `main` に書き込めるのは誰ですか（所有者だけか、AI の連携、他の協力者、長期のトークン）。二要素認証はパスキーですか。リポジトリは公開ですか。卒業後の引き継ぎ先は決まっていますか。
6. デプロイ用の署名を、PC の外にある鍵（ハードウェアキー）で、毎回のリリースで行う運用を許容できますか（6 章 8）。
7. Pi は人が触れる場所にありますか。SD カードの差し込み口に手が届きますか。
8. 音の出力は 3.5 mm・USB・Bluetooth のどれですか。`~/.asoundrc` を使っていますか（`services`・`service` の可否に関わります）。
9. pygame は apt だけで入れましたか。`pip` を使ったことはありますか。
10. 時刻の源は何ですか（既定の Debian プール、学内の NTP）。UDP 123 と TCP 4460 は出られますか。TLS を検査するプロキシはありますか。
11. `extra_segment.weather_hours` は `[12]` だけですか。天気の地点は 1 か所ですか。
12. 停電（電源の抜き差し）は多いですか。放送が遅れる・鳴らないとき、通知が必要ですか（放送は緊急用途ではない、と文書にはあります）。
13. `pi` のほかのアカウントはありますか。VOICEVOX の ENGINE を学内のネットワークで動かすことはありますか。

---

## 8. 参照

出典の確かさを区別します。**確認**は、調査の検証者が勧告やカタログの原文で存在を確かめたもの、**要約**は検索結果の要約だけのもの、**調**は調査担当者の記述（勧告の番号つき）で、
別の検証は通っていないものです。この記録の作成中はネットワークにつないでいないので、記録の書き手は再確認していません。

### 8.1 CISA KEV（悪用が確認された脆弱性）のカーネルの権限昇格

カタログ: `catalogVersion 2026.10.21`（2026-10-21 公開、1754 件。調査担当者が取得して解析）。**確認**: https://raw.githubusercontent.com/cisagov/kev-data/develop/known_exploited_vulnerabilities.json
一覧の閲覧用ページ: https://www.cisa.gov/known-exploited-vulnerabilities-catalog

| CVE | 内容 | KEV 追加日 | 備考 |
|---|---|---|---|
| CVE-2026-31431 | `AF_ALG`（algif_aead）のページキャッシュ書き込みで、手元の利用者が管理者に（通称 Copy Fail） | 2026-05-01 | GHSA-2274-3hgr-wxv6、High 7.8、公開 2026-04-22。**確認** |
| CVE-2026-53362 | IPv6 のページ割り当て（`fraggap`）。名前空間と UDPv6 のソケットの作成が要る | 2026-08-27 | GHSA-3x6f-vm7x-cgm7、High 7.8、公開 2026-07-04。**確認** |
| CVE-2026-53266 | netfilter ブリッジ ebtables の SNAT。ebtables が要る（Lite では通常読み込まれていない） | 2026-09-18 | GHSA-xmhg-rr34-2chj。**確認** |
| CVE-2025-39964 | `AF_ALG` の並行書き込み | 2026-09-18 | KEV 掲載は JSON で**確認**。内容の説明は調査の要約 |
| CVE-2025-39682 | カーネル（観点の調査には出なかった） | 2026-09-18 | 修正版は未確認 |
| CVE-2025-38352 | カーネル（同上） | 2025-09-04 | 修正版は未確認 |

`security_check.sh` の `KERNEL_FIXES_SINCE=2026-07-04` は、CVE-2026-53362 の公開日から決めた目安です。新しい脆弱性が載ったら、この日付を新しくします。
いずれも**手元で動けること**が前提で、外から単独では使えません（#4）。個々の CISA の警告ページは**要約**のみです。

KEV に載っていても、この Pi には当たらないもの: sudo CVE-2025-32463（Bookworm の sudo 1.9.13p3 は対象外）、git CVE-2025-48384（サブモジュールを使わず `git pull --ff-only` のみ）、
glibc CVE-2023-4911（Bookworm では修正済み）、`tj-actions` 系 CVE-2025-30066・CVE-2025-30154（使っていない）。FreeType CVE-2025-27363 は pygame の依存だがフォントを読まず、Debian の修正状況は未確認です。

### 8.2 Debian と Python

| 内容 | 出典 |
|---|---|
| Debian 12（Bookworm）の通常のセキュリティ支援は 2026-07-11〜12 に終了し、LTS チームへ。LTS は 2028-06-30 まで、armhf を含む | https://www.debian.org/News/2026/20260712（**要約**）。Debian wiki の古い見込みは 2026-06-11 で、**資料でずれがある** |
| python3.11 は LTS の対象。DLA-4827-1（2026-10-07 掲載）で `3.11.2-6+deb12u9` | https://www.debian.org/lts/security/ 、tracker.debian.org（**要約**。DLA の本文は未確認）。tracker の要約では、u7 が 2026-05-02、u8 が 2026-05-24 に受理（oldstable-proposed-updates） |
| CVE-2025-13836（PSF-2025-14、GHSA-399h-rrqc-rpgv）: `HTTPResponse.read()` が Content-Length を信じて確保する。2025-12-01 公開 | **確認**。2026 年 5 月ごろより前に最後の更新をした Pi には修正がない。放送への影響は低 |
| Python 3.11 の上流は 2027-10 にセキュリティ修正のみの期間が終わる | **要約** |
| 32 ビット版 Raspberry Pi OS の多くのパッケージは Raspbian から来て、Debian と方針が違いうる（Raspberry Pi 側の返信、フォーラム 391247） | **要約** |

### 8.3 その他の CVE（本文で触れたもの）

| CVE | 内容 | 重さ・出典 |
|---|---|---|
| CVE-2026-80186 | BlueZ。細工した EIR パケットでスタックあふれ。近くから、ペアリング不要 | GHSA-f784-6479-v89p、High 7.6。**調**。Bluetooth を止める理由（#7） |
| CVE-2026-24401 | avahi。自己参照する CNAME で avahi-daemon が落ちる | GHSA-h4vp-5m8j-f6w3、Moderate 6.5、公開 2026-01-24。v0.9-rc3 で修正。**確認** |
| CVE-2026-109786 | fail2ban。細工したユーザー名で任意の IP を締め出させられる | GHSA-xw36-3g63-5wfw、Moderate 5.3、公開 2026-10-13（0.10.5〜1.1.1、修正は commit ddd879d）。**調**。入れない理由（3.3 節） |
| CVE-2026-35189・CVE-2026-8674 | OpenSSL・glibc。CVE-2025-13836 とあわせ、近くの攻撃者が天気の取得を妨げうる、という一群 | 放送への影響は低（天気の一文が抜ける、再起動）、攻撃のしやすさは中。**調** |

KEV に載っていないことを確認したもの: CVE-2024-6387、CVE-2025-32462、CVE-2025-13836、CVE-2024-10573、CVE-2026-31976、CVE-2025-9074、CVE-2026-35535、CVE-2026-35444、CVE-2026-56109、CVE-2026-24401。
CPython・SDL・pygame・mpg123・ALSA・OpenSSH・systemd・avahi・BlueZ・wpa_supplicant・VOICEVOX・`actions/checkout`・`setup-python` に当たる KEV の項目はありませんでした（OpenSSL は 2014 年の Heartbleed のみ）。

### 8.4 リポジトリ内

- `scripts/security_check.sh`（項目名: `NET-ADDR4`／`NET-ADDR6`／`NET-LISTEN`／`NET-FW`／`SSH-PASSWORD`／`SSH-KEYS`／`SUDO-NOPASSWD`／`OS-KERNEL`／`OS-REBOOT`／`OS-UPDATES`／`OS-AUTOUPDATE`／`TIME-SYNC`／`REPO-STRAY`／`HARDEN-PENDING` ほか）
- `scripts/harden.sh`（手順 `ssh`・`firewall`・`sudo`・`services`・`journal`・`updates`・`service`・`ntp`、`recommended`、`--confirm`・`--rollback`・`--rollback-pending`・`--dry-run`・`--test-sound`・`--ssh-from`・`--ntp-server`・`--revert-after`・`--force`・`--user`）
- [SECURITY.md](SECURITY.md)（運用の手引き）
- [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) 4-5（時刻の再同期）・4-10（閉め出されたとき）・4-11・4-12、同じく 5 章（毎月の点検）
- [SETUP.md](SETUP.md)（公開鍵認証、導入の最後の安全面の手順）
- [SPECIFICATION.md](SPECIFICATION.md) 4.19 章（点検と強化のスクリプト）、SPECIFICATION.md 6.3 章（追加設定）
