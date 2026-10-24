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

## 2026-10-24 — v6.2.0（セキュリティの点検と強化）

要望:「運用中の Pi のセキュリティを調べ、見つかった点を点検・強化のスクリプトにして、結果は文書の更新のルールや
仕様に沿ってデータとして入れておいて」。前提は「Pi は学内のネットワークからだけ届く（ポート転送はしていない）。
ただし大学はクラス B/C の IPv4 アドレスの範囲を持つので、Pi がグローバルアドレスを持つ可能性がある」。

### 設計判断

- **報告を、非公開の詳細版と公開版に分けた。** 詳細版には、個人のメールアドレスの件数や、攻撃する側から見た説明、
  再現の数値が入る。公開リポジトリにそのまま置くと、直る前の弱点の手引きになるので、リポジトリには入れず
  運用者の手元に置く。公開版（`docs/SECURITY_REVIEW.md`）は、重さ・対策・状況・確かめられなかったことに絞り、
  弱点ごとの説明は一般的な 1〜2 文にした。運用の手引き（`docs/SECURITY.md`）も同じ考えで、攻撃のやり方は書かず、
  守り方と戻し方だけにした（`tests/test_docs.py` が、攻撃の手口を示す語が入っていないことを確かめる）
- **点検と強化を別のスクリプトにした。** `security_check.sh` は読むだけ（何も書かず、サービスにも触れない）にして、
  毎月でも、強化の前後でも、`pi` のままでも気軽に実行できるようにした。変えるのは `harden.sh` だけ
- **強化の手順は、どれも任意（opt-in）で、元に戻す仕組みをつける。**（「どれも元に戻せる」とは書かない。限界は SECURITY.md 3.2 節）実機を見ないまま作ったこと、運用者が専門家ではないこと、
  無人の機械で放送を止めたくないこと、手順ごとに起きうる困りごとが違うこと（`ssh` は締め出し、`service` は無音、
  `services` は Bluetooth のスピーカー）が理由。どの手順も、(1) 変える前に条件をすべて確かめ、満たさなければ
  理由をすべて出して何も変えずに止まる、(2) 変えたあとに効いたことを確かめ、確かめられなければ自分で戻す、
  (3) 何度実行しても同じ結果、の三つを守る。変える前の姿は `/var/lib/campus-chime-harden` に控え、`--rollback` で
  戻せる。`recommended`（`services journal ssh firewall`）はあるが、慣れるまでは 1 つずつをすすめる。
  放送を止めうる手順（`service`・`updates`・`ntp`）は、先に `--wait-idle` で放送の終わりを待つ
- **`ssh` と `firewall` は、確定しないと自動で元に戻す。** 無人の機械が締め出されると、直しに現地へ行くしかない。
  そこで適用すると「確定待ち」にして、10 分（`--revert-after`、1〜60 分）のタイマーを置き、**新しい端末**から
  `--confirm` するまで戻す。
  - 「新しい端末」でなければならないのは、すでに開いている SSH の接続は、新しい設定のあとも切れずに残るので、
    その画面が動いていても「次に入り直せる」確認にならないから。変更の前に開いた画面からの `--confirm` は断る
    （見分けは目安で、tmux や screen の中は見分けられないので、`--force` を残した）
  - 戻すタイマーは、**変える前に**張る。途中で落ちても、何も変わっていないか、タイマーが張られているかのどちらかに
    なる。タイマーは有効にしてあるので、確定前に再起動しても、起動の 10 分後に戻る
  - 戻す処理は、`pi` が書き換えられるリポジトリの中のスクリプトではなく、root だけが書ける写し
    （`/var/lib/campus-chime-harden/harden.sh`）を実行する。リポジトリが書き換えられても、root で動くのは写しのほう
  - 戻す処理は諦めない。放送の終わりを待たず（SSH と防火壁は放送に関係しない）、ほかの実行がロックを持っていれば
    長く待ち、戻せなかったものがあれば確定待ちの記録とタイマーを残して systemd に再試行させる
- **検証者の指摘を受けた修正ラウンドで、元に戻す仕組みの穴を塞いだ。** 同じ「確定待ち＋時計」の道に乗せる形で直した:
  - `sudo` の手順は、NOPASSWD を外す前に、5 分（`SUDO_WINDOW`）の確定待ちと時計を用意する。パスワードを確かめられたら
    `sudo` の確定待ちだけを外す（`ssh`・`firewall` の確定待ちと時計は残す）。`--confirm` は `sudo` を確定しない。
    パスワードが分からないとき、コンソールから root で戻す方法は、root で入れない Pi では使えないので、
    「適用の前にパスワードを確かめる」を先に書いた
  - Ctrl-C・回線切れ・終了の信号は、どの手順でも、確認に失敗したときと同じ道（`undo_step`）で、実行中の手順を戻してから終わる
  - すでに確定した `ssh`・`firewall` を再適用して確定しないときは、使う前（原状）ではなく、最後に確定した設定へ戻す。
    そのため `--confirm` が、確定した設定のファイルを `confirmed/<手順>/` に控え、防火壁の起動時に読み込むファイルは
    `--confirm` のときにだけ書く。`--rollback` は、いつも原状まで戻す
  - 自動で戻したあとの `--confirm` は、「確定する変更はありません」と言うだけでは、強化が効いていると誤解させる。
    戻した手順を `reverted` に記録し、「確定できたからではない」と警告して終了コード 1 にする
  - 控えは、別名に写して `cmp` で完全と確かめてから置く。写し損ねた控えで、元のファイルを置き換えない
  - `firewall` は、sshd が実際に待ち受けるポートをすべて開ける。自分が SSH でつないでいるときの「接続が切れる」確認は、
    `--force` でも飛ばせない。Debian の標準の空の骨組み（policy accept で規則なし）の表は、別の防火壁と数えない
  - `updates` は、OS の時間帯が放送の時間帯と違うとき（OS が UTC のままだと、5 時のつもりが 14 時になる）と、
    設定した放送が 4:30〜6:30 にあるときに止まる。apt を動かす手順と時計を変える手順は、`--wait-idle` のあとに
    `--schedule 1` で次の放送まで 10 分以上あることも確かめ、`apt-get install` の前に `apt-get update` を実行する
  - `service` は、書き込み先を `chime/config.py` と同じ解き方（`~` の展開）で求め、ドロップインの `%` を `%%` と書き、
    入っているユニットの場所と利用者が合うか、利用者が書き込み先を作れるかを、変更の前に確かめる
  - `security_check.sh` は、`[不明]` が残るとき「点検は完全ではありません」と表示し、root で実行しても apt の索引の写しを
    作り直さない
  - `security_check.sh` の「harden.sh の保留」は、確定待ちの記録の今の書式（手順ごとの `ssh=`・`firewall=`・`sudo=` の行と
    `window=分`）に合わせた。`ssh` か `firewall` が残っていれば、手順の名前と戻るまでの分数を示し、新しい端末で確定する
    コマンド（絶対パス）を案内する。`sudo` の手順だけが止まっているときは、`--confirm` が `sudo` を確定しないので、
    「確定の操作は要りません。自動で戻るのを待つ」と案内する（以前の一律の「確定せよ」は、この場合に誤り）。手順の行が
    無い記録（`window` だけ・空）は、確定待ちなしと数える
  - `setup.sh` は、サービスの再起動の前に、サービスが書き込むフォルダ（`cache/` と `assets/generated/`）を、実行している
    利用者で作る。`service` の保護の `ReadWritePaths` は先頭の `-`（無ければ無視）で挙げるため、新しい checkout では
    フォルダが無いまま保護の下でサービスが起動し、状態も時報音も保存できない。root で実行したときは、root の持ち物に
    なってサービスが書けなくなるので作らず、警告だけを出す。作るのは固定の 2 つで、`config.json` で場所を変えている
    ときは `harden.sh service` が求めて作る
  - 文書は、始める前の注意（実機では未試験・コンソールの用意・放送のない時間）を SECURITY.md 3 節の先頭に置き、
    確認は `sudo` を付けた点検（`[要対応]` 0 件かつ `[不明]` なし）にし、「新しい端末」の確定のコマンドは絶対パスで書き、
    運用の手順には点検の画面に出る分類の名前を使う（項目の ID は画面に出ない）。「締め出されない」「どれも元に戻せる」という
    言い切りをやめ、守るものと守れないものを書いた
- **時刻の同期は `chrony` にした（`ntp`）。** NTP は暗号で守られておらず、`systemd-timesyncd` は起動後の大きな
  時刻の飛びも受け入れる。`chrony` の Debian の既定（`makestep 1 3`）は、起動後の最初の数回の更新だけ時刻を飛ばし、
  そのあとの大きなずれは少しずつ寄せるので、放送の時刻が急には狂いにくい。`rtcsync` があるので、`--status` と
  `setup.sh` が読む `timedatectl` の「同期済み」はそのまま。NTS（NTP を暗号で守る方式）は、認証なしの既定の行を残すと
  意味が薄れ、TCP 4460 と起動時のほぼ正しい時計も要るので入れず、**部分的な対策**と書いた。再同期の命令が
  `systemctl restart chrony` に変わるので、KNOWLEDGE_BASE.md 4-5 に並べて書いた
- **放送サービスの保護（`service`）は、`ProtectClock`・`PrivateDevices`・`DeviceAllow`・`DevicePolicy` を使わない。**
  これらは音声デバイス（`/dev/snd`）を閉じてしまい、音が出なくなる。`ProtectClock` は単独なら無害だが、デバイスの
  許可リストと組み合わさると閉じる側に働き、得られる点も約 0.1 だけ。`MemoryDenyWriteExecute`・
  `ProcSubset=pid`（SDL が `/proc/cpuinfo` を読む）・`PrivateUsers` も使わない。ユニット本体は変えず、systemd の
  追加設定（drop-in）だけを足すので、`setup.sh` が登録し直しても残る。書き込みを許す場所は `config.json` から求める。
  項目の唯一の定義は `SANDBOX_PROPS` で、`--test-sound` の試験にも同じものを使い、SPECIFICATION.md 6.3 章の表との
  一致をテストが比べる
- **Pi のアドレスは、「届く」と断定せず、安全側に表示する。** 大学の入口の防火壁は Pi の中から見えない。
  グローバルアドレスが 1 つでもあれば「届くなら高」と表示し、学外から試す方法（`nc -vz`）を添える
- **GitHub 側の守りを、Pi の強化と並べて書いた。** Pi は `main` の中身をそのまま取り込んで実行するので、`main` に
  書ける人のアカウントを守るのが一番安く効く。`sudo` にパスワードを求めても、この経路は守れない（取り込んだコードは
  運用者の画面で動く）ので、`harden.sh sudo` の説明にもそう書いた
- **コードの修正は、この版に入れなかった。** 天気取得の時間制限、時計の見張り、署名つきデプロイタグなどは、
  `SECURITY_REVIEW.md` 6 章に候補として書いた。放送の動作を変える修正は運用者との相談が先で、この版は文書と
  スクリプトだけにして、放送の時刻と内容を変えないようにした

### 実装

- `scripts/security_check.sh`（読むだけ）と `scripts/harden.sh`（手順 `ssh`・`firewall`・`sudo`・`services`・
  `journal`・`updates`・`service`・`ntp`、`recommended`）。どちらも bash で `set -euo pipefail`。システムの絶対パスは
  `CHIME_SYSROOT` の下から読み書きし、`/proc` は `CHIME_PROC`、外のコマンドは名前だけで呼ぶ（PATH で探す）ので、
  テストで差し替えられる
- `tests/test_security_check.py`（200 件）・`tests/test_harden_script.py`（353 件）: 偽のシステムと、偽のコマンドだけの
  PATH で、本物のスクリプトを動かす。実機のシステムには触れない。修正ラウンドで、検証者が再現した不具合
  （`sudo` の入力中の中断、遅い `--confirm`、再適用の取り消し、sshd のポート、次の放送までの余裕、OS の時間帯、
  控えの不完全、書き込み先の解き方、点検の「完全ではありません」の表示、apt の索引の写し、点検の確定待ちの案内）の回帰テストを足した。
  `tests/test_setup_script.py`（50 件）には、サービスが書き込むフォルダを再起動の前に作ることの試験を足した
- `docs/SECURITY.md`（運用の手引き。脆弱性の連絡先を兼ねる）と `docs/SECURITY_REVIEW.md`（公開版の記録）を追加し、
  README・SETUP・KNOWLEDGE_BASE・SPECIFICATION・REQUIREMENTS・CHANGELOG を更新した。SETUP では、Imager の手順で
  公開鍵認証を勧め、`pi` のパスワードは長いランダムな文字列にし、VOICEVOX の例は `-p 127.0.0.1:50021:50021` にした。
  KNOWLEDGE_BASE では、学期ごとの `apt upgrade` を、毎月の `full-upgrade` と、放送のない時間の再起動と、
  `security_check.sh` に替えた
- `tests/test_docs.py` に、文書に書いた `harden.sh` の手順・オプション・ファイルの場所がスクリプトにあること、
  SECURITY.md が全手順を「変えること・放送への影響・確かめ方・元に戻す」で説明していること、追加設定の項目が
  `SANDBOX_PROPS` と一致することなどの検査を足した。「N 章」の参照の検査も、SECURITY.md などの新しい文書の
  見出しと照らすようにした（それまでは、5 つの文書以外に「N 章」を書くと KeyError になった）。修正ラウンドで、
  始める前の注意が手順より前にあること、確認の手順が `sudo` 付きの点検であること、言い切りが無いこと、確定のコマンドが
  絶対パスであること、運用の手順が画面に出る分類の名前で書かれていること、タイマーの時刻・待つ秒数・上限・分数が
  スクリプトの値と同じであること、`--rollback` に続く手順の名前と手引きのオプションの綴り、他の文書が指す
  SECURITY.md・SECURITY_REVIEW.md の節の実在を足した（時刻を 6:40〜7:00 に、上限を 100M に、`--rollback ntpp` に
  書き換えても通っていた、という検証者の指摘を、書き換えて落ちることを確かめて直した）。点検の確定待ちの項目（`window` の既定が `harden.sh` の `REVERT_AFTER` と同じこと、
  `sudo` だけが止まったときの案内に `--confirm` が無いこと）と、`setup.sh` が再起動の前に作るフォルダ（固定の 2 つ、
  `sudo` を使わないこと、再起動する分岐でだけ呼ぶこと）の文書の記述も、スクリプトと照らして確かめる

### つまずいた点

- **最初の実装を失った。** コンテナが作り直されて、コミット前の最初の実装（2 本のスクリプトとテスト）が消えた。
  残っていた仕様書と、検証者の指摘の記録から作り直した。検証者の指摘は、仕様書に「必ず取り込む点」として
  書き足してから作り直した。例: 戻すタイマーを変更の前に張ること、戻す処理を諦めないこと、鍵の置き場所の持ち主と
  権限（sshd が拒む置き方）を事前に確かめること、`ProtectClock` を使わない理由、`systemd-analyze verify` が知らない項目でも
  0 で終わること
- **テストの偽物の側の誤り。** 偽の `runuser` が `mkdir` に「mkdir」を余分に渡していて、CI（一般ユーザー・書き込めない
  作業フォルダ）で `service` の手順のテストが 16 件落ちた。スクリプト本体の不具合ではなく、偽物を直した。
  root で動かす環境では通っていたので、テストは一般ユーザーでも動かして確かめる必要がある
- **文書が言い過ぎていた。** 最初の文書は、「SSH と防火壁は、確定しなければ自動で元に戻るので、締め出されない」「どの手順も
  元に戻せる」と書いていた。実機で一度も動かしておらず、失敗や中断の穴も残っていたので、検証者に指摘された。
  「始める前に」の注意を足し、守るものと守れないもの（強制終了・電源断・OS が起動しない場合）を SECURITY.md 3.2 節に書いた
- **点検の「要対応 0 件」を合格と読めた。** `sudo` なしで実行すると、防火壁などが `[不明]` になるのに、数えるのは
  `[要対応]` だけなので、`sudo` を付けない確認の手順では、調べていないのに「0 件」になりうる。確認は `sudo` 付きにして、
  `[不明]` が残るときはスクリプトが「点検は完全ではありません」と表示するようにした
- **文書が、画面に出ない名前を指していた。** 運用の手順に、項目の ID（`OS-KERNEL` など）を書いたが、ID はスクリプトの
  コメントにしかない。画面に出る分類の名前に直し、ID は開発者向けの対応表（SECURITY_REVIEW.md 8.4 節）に残した
- **新しい端末の確定のコマンドに、場所が無かった。** 新しく開いた SSH の画面はホームのフォルダから始まるので、
  `sudo bash scripts/harden.sh --confirm` は動かず、確定の 10 分を無駄にする。スクリプトが画面に出すコマンドも文書も、
  `/home/pi/campus-chime/scripts/harden.sh` の絶対パスにした

### 検証

- `python3 -m unittest discover -s tests -t .` → 3795 件 全件成功（skip 3 件。初版の 3550 件から、修正ラウンドの
  回帰テストと文書のテストで 245 件増えた）。作業環境の Python 3.11 で確認した。`tests/test_docs.py`（337 件）は
  Python 3.11・3.12・3.13 で通り、書き込めない作業フォルダの一般ユーザー（ubuntu）でも通る（CI と同じ条件。CI は 3.9・3.11・3.13。
  3.9 はこの環境にない）
- `shellcheck scripts/*.sh` → 指摘なし。`bash -n scripts/harden.sh scripts/security_check.sh` → 指摘なし。
  `python3 -m pyflakes tests/test_docs.py` → 指摘なし
- 生成する防火壁のルールを、本物の `nft -c -f`（検査モード。読み込まない）で検査した。テストは、root で
  `nft -c -f` が通る環境のときだけ実行し、通らなければ飛ばす
- 点検スクリプトが何も書かないことを、読み取りだけの道具と偽のコマンドだけの PATH で動かして確かめた
  （書き込もうとすれば失敗する）
- 文書とスクリプトの食い違い（手順の名前・オプション・ファイルの場所・追加設定の項目・確定までの時間）は、
  `tests/test_docs.py` で機械的に比べている

### 未検証のまま残したもの

- **失敗や中断を入れた試験で見つかった点は直したが、見つけていない不具合が残っているかもしれない。** 検証者が、手順の途中に
  強制終了・外部コマンドの失敗・中断の信号（Ctrl-C など）を入れる試験を、作業用の環境（偽のシステム）で行った。
  強制終了を入れた試験では、問題はほとんど見つからなかった。一方、失敗や中断を入れた試験では、元に戻す処理や確定の処理が
  実際と合わない場合が 10 項目ほど見つかった。例: `sudo` の手順のパスワード入力中に中断すると、パスワードなしの設定が
  外れたまま残る。控えのコピーに失敗したあとで元に戻すと、元のファイルを空の控えで置き換えてしまう。`--confirm` が、
  確定待ちの記録を消せなかったのに「確定しました」と表示する。これらは修正ラウンドで直し、再現を回帰テストにした
  （`tests/test_harden_script.py`）。ただし、試験は偽のシステムでのもので、実機の動きは確かめていない。
  実機で `ssh`・`firewall`・`sudo` を使うときは、これまでどおり、`--dry-run` で内容を見て、1 手順ずつ、HDMI とキーボードを
  つないだ状態で行う（SECURITY.md 3 節の「始める前に」）
- **本物の Pi では、一度も動かしていない。** `harden.sh` の `ssh`・`firewall`・`sudo`・`ntp`・`updates` は、本物の
  sshd・nft・sudoers・apt・systemd では試していない。最初は `--dry-run` で内容を見て、放送のない時間に 1 手順ずつ
- `service`（保護）の下での SDL2／ALSA の動き。32 ビット ARM で試せていない。音が出なければ
  `--rollback service`（KNOWLEDGE_BASE.md 4-12）
- Pi の systemd 252 と chrony 4.3 の実際の動き（作業環境は systemd 255）。点検の「カーネルの新しさ」の日付（2026-07-04）は目安で、
  Raspberry Pi のカーネルに各修正がいつ入ったかは確かめていない
- GitHub の設定画面の名前と、保護ルールの実際の効き方（GitHub につないでいない）。`SECURITY_REVIEW.md` の CVE と
  Debian の日付は、検索結果の要約や調査担当者の記述に頼るものがある（印を付けて区別した）
- 署名つきデプロイタグの試作は、GPG（git 2.43）でだけ試した。SSH 署名と、Bookworm の git 2.39 は未試験で、
  試作のパッチはリポジトリに残していない

## 2026-09-17 — v4.0.0（ずんだもん統一・現況天気）

要望:「声はずんだもんにしたのに文言が事務的なままなので、語尾を『なのだ』調にしたい。
天気予報も時報ごとに流したい」。途中で「現在の天気と気温を教えてくれないのか」
「2 時間おきにしたい」と追加。

### 設計判断

- **天気の読み上げを有限語彙の組み合わせに作り替えた**（本版の中心）。
  天気の文面は日替わりで事前生成できず、そのまま有効にすると天気だけ Open JTalk の
  男性音声になる。地名・気温をそれぞれ別の文に分割すれば各文の語彙が閉じるため、
  全パターンを作り置きできる。1 文にまとめると組み合わせが爆発して作り置きできない
- **取得先を気象庁から Open-Meteo へ**。気象庁の予報文は自由文で語彙が閉じない。
  精度より声の一貫性を優先した
- **語彙の網羅テストを置いた**。実行時に組み立てる文が作り置きの集合に完全に含まれる
  ことを機械的に検証する。これが「天気だけ男性音声になる」ことを防ぐ唯一の担保なので、
  作り置きを 1 つ欠けさせると実際に落ちることまで確認した
- **天気の取得失敗時は黙って飛ばす**。従来は「ひとこと」へ切り替えていたが、
  新方式では「ひとこと」が必ず後続するため、切り替えると 2 つ流れてしまう
- **天気を流さない時刻では天気 API を呼ばない**。毎正時に無駄な HTTP を出さない

### つまずいた点

- **読んでいたのは予報だった。** `daily` しか問い合わせておらず、14 時に
  「最高気温は31度」と読んでいたのはその日の予想最高気温だった。利用者の指摘で発覚し、
  `current` に切り替えた
- **作り置きを作り直さないまま配りかけた。** 語尾を変えたあと `assets/voice/` が
  古い 64 件のままでマージ直前まで進んでいた。そのまま配れば読み上げが 1 件残らず
  男性音声になっていた。原因は手順書に「文言を変えたら音声を作り直す」分岐が
  無かったことなので、README 9 章と `docs/SETUP.md` 9 章を書き直した
- **`--weather` が複数文を連結して合成していた。** 文ごとには作り置きがあるのに
  連結すると照合が外れ、確認コマンドの声と実際の放送の声が食い違っていた
- **ひとことの誤読**を避けるため、書き換えた 57 件すべてを `open_jtalk` の
  形態素解析にかけて確認した（過去に「分別→フンベツ」を実機で踏んでいるため）

### 未検証のまま残したもの

- Open-Meteo の `current` の応答形式。開発環境から外部に出られず実通信で確認していない
- WSL2 で閉館チャイムが 20 秒ほどで途切れる件。実機で再現するか未確認

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
- **音声合成は 2 段フォールバック**: 事前生成 → VOICEVOX ENGINE。
  VOICEVOX は Pi 3B には重すぎるため、PC 側で作り置きする導線（`scripts/generate_voicevox.py`）を用意。
  v4.x までは最終段に Open JTalk を置いていたが、作り置きが外れたときに別人の男性音声で
  鳴ってしまい、壊れていることを隠す結果になったため v5.0.0 で削除した（CHANGELOG 参照）。
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
