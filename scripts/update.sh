#!/usr/bin/env bash
#
# 運用中の Raspberry Pi を最新版へ更新する。
# git pull → 放送の最中なら終わるまで待つ → 導入スクリプトの再実行（時報音の生成・点検・
# サービスの再起動）の順に進み、最後に新しい版と状態を表示する。
#
#   bash scripts/update.sh         # 最新版へ更新する（pi ユーザーで実行。sudo は付けない）
#   bash scripts/update.sh --help  # この説明を表示する
#
# 手元で書き換えたファイルがある、ブランチではなく特定のコミットを見ている、GitHub の
# 履歴と食い違っている、というときは、何も変えずに止まって理由を表示する。取り込み
# （git pull）が、権限（持ち主の違うファイルが残っている）で失敗したときは sudo chown の
# 直し方を、手元の Git 管理外のファイルが邪魔をしているときはその旨を、ディスクの問題
# （空き容量の不足・読み取り専用・入出力エラー・容量の割り当て超過）のときは、ディスクの
# 調べ方（df -h・dmesg）を表示する。
# 元の版への戻し方は表示するだけで、このスクリプトは実行しない。
#
# すでに最新版でも、サービスに反映した版の記録（cache/deployed_commit。導入スクリプトが、
# サービスの再起動に成功したときに書く）が、いまの版と違うか無いときは、前回の更新が
# 途中で止まったとみて、続きの反映（放送の確認と導入スクリプトの再実行）を行う。
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# サービスに反映した版（導入スクリプトがサービスの再起動に成功したときのコミット）の記録。
DEPLOYED_FILE="${REPO_DIR}/cache/deployed_commit"

for arg in "$@"; do
  case "$arg" in
    # 先頭のコメントブロック（2 行目から、最初のコメントでない行の手前まで）を表示する。
    -h|--help) sed -n '2,/^[^#]/{/^[^#]/!p}' "$0"; exit 0 ;;
    *) echo "不明なオプション: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m警告: %s\033[0m\n' "$*" >&2; }
fail() { printf '\033[31m中止: %s\033[0m\n' "$*" >&2; exit 1; }

# 元の版へ戻す手順を表示する（表示するだけ。実行しない）。
print_rollback() {
  cat <<MESSAGE

元の版（${old_head:0:7}）へ戻す場合は、次の 2 行を実行してください（このスクリプトは実行しません）。

  git reset --hard ${old_head}
  bash scripts/setup.sh --no-apt
MESSAGE
}

# root で実行すると、git が作るファイルの持ち主が root になり、サービス（pi）が
# 書き込めなくなる。
if [ "$(id -u)" -eq 0 ]; then
  fail "root では実行できません。pi ユーザーで『bash scripts/update.sh』と実行してください（sudo は付けません。必要な箇所は自分で sudo します）。"
fi

cd "${REPO_DIR}"

log "いまの版"
old_version="$(python3 campus_chime.py --version)" \
  || fail "python3 campus_chime.py --version を実行できません。リポジトリの場所（${REPO_DIR}）と python3 を確認してください。"
echo "${old_version}"

work_dir="$(mktemp -d)" || fail "作業用の一時フォルダを作れません。ディスクの空きを確認してください。何も変更していません。"
trap 'rm -rf "${work_dir}"' EXIT

# config.json は Git 管理外なので、ここには出てこない（--untracked-files=no）。
# 名前は -z（NUL 区切り）で受け取る。空白・引用符・日本語・改行を含む名前も、git の
# 引用（"\343\201..."）を介さず、そのまま扱える。
log "手元の変更の確認"
if ! git status --porcelain -z --untracked-files=no > "${work_dir}/status" 2> "${work_dir}/status.err"; then
  fail "git が手元の変更を調べられませんでした: $(tr '\n\t' '  ' < "${work_dir}/status.err" | tr -s ' ')（Git のリポジトリでないか、このフォルダの持ち主が実行している利用者と違うと、git は安全のために開きません）。何も変更していません。"
fi

list_text=""
restore_text=""
while IFS= read -r -d '' entry; do
  code="${entry:0:2}"
  path="$(printf '%q' "${entry:3}")"
  case "${code}" in
    R?|C?|?R|?C)
      # 名前の変更・コピーは、新しい名前に続けて、元の名前が別の項目で来る。
      IFS= read -r -d '' original || original=""
      original="$(printf '%q' "${original}")"
      list_text+="  ${code} ${original} -> ${path}"$'\n'
      restore_text+="  git restore --source=HEAD --staged --worktree -- ${path} ${original}"$'\n'
      ;;
    *)
      list_text+="  ${code} ${path}"$'\n'
      restore_text+="  git restore --source=HEAD --staged --worktree -- ${path}"$'\n'
      ;;
  esac
done < "${work_dir}/status"

if [ -n "${list_text}" ]; then
  echo "Git 管理下のファイルが手元で書き換わっています:" >&2
  printf '%s' "${list_text}" >&2
  echo "このまま更新すると上書きされるか、取り込みに失敗します。書き換えを捨ててよいなら、ファイルごとに次で元に戻せます（git add 済みの変更も含めて、HEAD の内容に戻ります）:" >&2
  printf '%s' "${restore_text}" >&2
  echo "git が古く（2.23 より前）git restore が無いときは、git checkout HEAD -- <ファイル> で戻せます（git add した新しいファイルは git rm -f -- <ファイル>）。" >&2
  fail "手元の変更を片付けてから、もう一度実行してください。何も変更していません。"
fi
echo "手元の変更はありません。"

if ! git symbolic-ref -q HEAD > /dev/null; then
  fail "ブランチではなく、特定のコミットを見ています（detached HEAD）。更新するブランチ（通常は main）に戻してから、もう一度実行してください。何も変更していません。"
fi

old_head="$(git rev-parse HEAD)"

log "最新版の取り込み"
# git の表示（標準エラー出力）は、失敗の理由を見分けるために控えておき、終わってから表示する。
# 日本語などの環境では git の表示が翻訳されて、下で探す英語の語句に当たらなくなるので、
# この 1 回だけ言語を C に固定する。
pull_status=0
LC_ALL=C git pull --ff-only 2> "${work_dir}/pull.err" || pull_status=$?
cat "${work_dir}/pull.err" >&2
if [ "${pull_status}" -ne 0 ]; then
  # 失敗の理由を、git の表示から見分ける。見分けられないものは、ネットワークか履歴の食い違いとして案内する。
  pull_problem=network
  if grep -qiE 'No space left on device|Read-only file system|Input/output error|Disk quota exceeded' "${work_dir}/pull.err"; then
    # ディスクの問題。権限の語句（unable to unlink・could not open など）と一緒に出ることがあるが、
    # 容量の不足や読み取り専用は持ち主を直しても直らないので、権限より先に見る。
    pull_problem=disk
  elif grep -qiE 'publickey|could not read from remote|authentication failed' "${work_dir}/pull.err"; then
    : # GitHub への接続・認証の失敗（ssh の "Permission denied (publickey)" など）。ファイルの権限ではない
  elif grep -qiE 'Permission denied|insufficient permission|unable to unlink|could not open' "${work_dir}/pull.err"; then
    pull_problem=permission
  elif grep -qi 'would be overwritten' "${work_dir}/pull.err"; then
    pull_problem=untracked
  fi
  case "${pull_problem}" in
    permission)
      # 以前に sudo で git を実行した、などで、リポジトリの中に別の持ち主（root など）のファイルがある。
      owner_user="$(id -un 2>/dev/null)" || owner_user=""
      owner_group="$(id -gn 2>/dev/null)" || owner_group=""
      echo "git の表示に権限（permission）の問題が出ています。ネットワークの問題ではありません。" >&2
      echo "リポジトリの中に、いまの利用者（${owner_user:-自分}）の持ち物でないファイル（以前に sudo で git を実行して root のものになった、など）があり、書き込めないようです。持ち主を次で直してください（このリポジトリの中だけが対象です）:" >&2
      echo "  sudo chown -R ${owner_user:-<ユーザー名>}:${owner_group:-<グループ名>} $(printf '%q' "${REPO_DIR}")" >&2
      echo "直したら、もう一度 bash scripts/update.sh を実行してください。取り込みが途中で止まっていた場合は、次の実行で『手元の変更』として案内が出るので、その表示に従ってください。" >&2
      fail "最新版を取り込めませんでした。放送は今までどおり動いています。"
      ;;
    disk)
      # このリポジトリのあるディスク（Pi では SD カード）に書けない。いっぱいになった、エラーが続いて
      # 読み取り専用に切り替わった（電源断や SD カードの劣化のあとに起きる）、割り当てを超えた、など。
      echo "git の表示にディスクの問題（空き容量の不足・読み取り専用・入出力エラー・容量の割り当て超過）が出ています。ネットワークの問題ではありません。" >&2
      echo "このリポジトリのあるディスク（SD カード）がいっぱいになっているか、エラーのあとで読み取り専用に切り替わっている（SD カードが傷んでいる）ようです。次で確かめてください:" >&2
      echo "  df -h $(printf '%q' "${REPO_DIR}")" >&2
      echo "    空き容量です。Use% が 100% に近い、または Avail が 0 なら、いっぱいです。不要なファイルを消して空きを作ってください。" >&2
      echo "  dmesg | tail -n 30" >&2
      echo "    ディスクのエラーです。I/O error や Remounting filesystem read-only と出ていれば、読み取り専用に切り替わっています（権限が無いと言われたら sudo dmesg | tail -n 30）。再起動で戻ることがありますが、くり返すときは SD カードの交換を考えてください。" >&2
      echo "直したら、もう一度 bash scripts/update.sh を実行してください。取り込みが途中で止まっていた場合は、次の実行で『手元の変更』として案内が出るので、その表示に従ってください。" >&2
      fail "最新版を取り込めませんでした。放送は今までどおり動いています。"
      ;;
    untracked)
      # 手元にある Git 管理外のファイルが、新しい版にも同じ名前であり、上書きされてしまう。
      echo "手元にある Git 管理外のファイル（上の git の表示に名前が出ています）が、新しい版のファイルと同じ名前で、取り込むと上書きされてしまうため、git が止めました。" >&2
      echo "そのファイルが要るなら別の場所へ移し（mv）、要らないなら消してから、もう一度 bash scripts/update.sh を実行してください。" >&2
      fail "最新版を取り込めませんでした。何も変更していません。放送は今までどおり動いています。"
      ;;
    *)
      fail "最新版を取り込めませんでした。ネットワークにつながっていないか、この機械の履歴が GitHub の履歴と食い違っています。何も変更していません。放送は今までどおり動いています。ネットワークを確認してもう一度試し、直らなければ管理者に伝えてください。"
      ;;
  esac
fi
new_head="$(git rev-parse HEAD)"

# サービスに反映した版。記録が無い・読めないときは空。
deployed=""
if [ -f "${DEPLOYED_FILE}" ]; then
  { IFS= read -r deployed < "${DEPLOYED_FILE}"; } 2>/dev/null || true
  deployed="${deployed//[[:space:]]/}"
fi

code_changed=1
if [ "${old_head}" = "${new_head}" ]; then
  if [ "${deployed}" = "${new_head}" ]; then
    echo "すでに最新版です。更新するものはありません。"
    log "いまの状態"
    python3 campus_chime.py --status || true
    exit 0
  fi
  # コードは最新だが、サービスに反映した記録が合わない。前回の更新が、取り込みのあと
  # （放送の確認・導入スクリプト）で止まったままかもしれない。止めずに続きを行う。
  code_changed=0
  if [ -z "${deployed}" ]; then
    reason="サービスに反映した版の記録（cache/deployed_commit）がありません"
  else
    reason="サービスに反映した版（${deployed:0:7}）が、いまの版（${new_head:0:7}）と違います"
  fi
  warn "コードは最新ですが、${reason}。前回の更新が途中で止まって、サービスが古い版のままかもしれません。続けて反映します。"
fi

# 放送の最中にサービスを再起動すると、放送が途切れる。
log "放送の時間帯の確認"
wait_status=0
python3 campus_chime.py --wait-idle || wait_status=$?
if [ "${wait_status}" -ne 0 ]; then
  if [ "${wait_status}" -eq 2 ]; then
    # 設定ファイルを読めない。放送の時間帯かどうかを調べられない（放送が終わらないのではない）。
    warn "設定ファイル（config.json）を読めないため、放送の時間帯かどうかを確認できませんでした。サービスはまだ再起動していません。"
    warn "上の『設定エラー』の表示のとおり config.json を直してから、もう一度 bash scripts/update.sh を実行してください（反映が済んでいなければ、続きから行います）。"
    warn "急ぐときは、config.json を直したうえで、放送のない時間に bash scripts/setup.sh --no-apt を実行しても反映できます（設定を読めないままでは、setup.sh もサービスを再起動しません）。"
  else
    warn "放送が終わったことを確認できませんでした。サービスはまだ再起動していません。"
    warn "コードの更新（git pull）は済んでいます。放送のない時間に、次を実行すると反映されます:"
    warn "  bash scripts/setup.sh --no-apt"
  fi
  if [ "${code_changed}" -eq 1 ]; then
    print_rollback >&2
  fi
  exit 1
fi

log "導入スクリプトの再実行（時報音の生成・点検・サービスの再起動）"
if ! bash "${REPO_DIR}/scripts/setup.sh" --no-apt; then
  warn "導入スクリプトが途中で失敗しました。上の表示を確認してください。サービスは再起動されていないかもしれません。"
  warn "直したあと、もう一度 bash scripts/update.sh を実行すると、続きから反映します。"
  if [ "${code_changed}" -eq 1 ]; then
    print_rollback >&2
  fi
  exit 1
fi

log "更新の結果"
new_version="$(python3 campus_chime.py --version)" || new_version="（版を取得できません）"
echo "${old_version}  →  ${new_version}"
python3 campus_chime.py --status || true
if [ "${code_changed}" -eq 1 ]; then
  print_rollback
fi
