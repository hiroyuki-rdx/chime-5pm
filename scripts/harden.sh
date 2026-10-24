#!/usr/bin/env bash
#
# 運用中の Raspberry Pi の安全対策を、手順（STEP）ごとに、元に戻せる形で行う。
# scripts/security_check.sh が指摘した点の直し方で、どの手順も次の順に進む。
#   1. 変える前に、必要な条件をすべて確かめる（満たさないものがあれば、理由をすべて表示し、何も変えずに止まる）
#   2. 変えて、効いたことを確かめる（確かめられなければ、その手順だけ元に戻して止まる）
#   3. 何度実行しても同じ結果になる（すでに適用済みなら何もしない）
# 変える前の姿は /var/lib/campus-chime-harden に控えてあり、--rollback でいつでも戻せる。
#
#   sudo bash scripts/harden.sh                       # 手順の一覧と、いまの状態
#   sudo bash scripts/harden.sh ssh                   # SSH を鍵だけにする（パスワードログインを止める）
#   sudo bash scripts/harden.sh firewall [--ssh-from CIDR]...
#                                                     # 入ってくる通信を SSH（管理用の網から）・mDNS・DHCP・ICMP だけにする
#   sudo bash scripts/harden.sh sudo                  # sudo にパスワードを求める（端末での確認つき）
#   sudo bash scripts/harden.sh services              # bluetooth・hciuart・ModemManager を止めて無効にする
#   sudo bash scripts/harden.sh journal               # ログを再起動後も残す（上限 200M）
#   sudo bash scripts/harden.sh updates               # セキュリティ更新を自動で入れる（再起動はしない）
#   sudo bash scripts/harden.sh service [--test-sound]
#                                                     # 放送サービス（campus_chime.service）に保護をかける
#   sudo bash scripts/harden.sh ntp [--ntp-server HOST]...
#                                                     # 時刻同期を chrony にする（起動後の大きな時刻の飛びを受け付けない）
#   sudo bash scripts/harden.sh recommended           # services journal ssh firewall をまとめて
#   sudo bash scripts/harden.sh --confirm             # 確定待ちの ssh・firewall の変更を確定する（自動で戻るのを止める）
#   sudo bash scripts/harden.sh --rollback [STEP...]  # 元に戻す（STEP なしは、適用したものを新しい順にすべて）
#   sudo bash scripts/harden.sh --dry-run STEP...     # 何をするかを表示するだけ（何も変えない。sudo なしでも試せる）
#   sudo bash scripts/harden.sh --test-sound          # service の手順の保護をかけたまま、12 時の時報を 1 回鳴らして確かめる
#
# 共通のオプション: --user NAME（サービスの利用者。既定 pi）、--revert-after MINUTES（1〜60。既定 10）、
# --force（確認の一部を飛ばす。使いどころは手順ごとに表示します。いま SSH でつないでいる自分の
# 接続が防火壁で切れる場合は、--force でも止めます）、-h|--help。
# 複数の手順は一度に指定できる（例: services journal）。条件はすべての手順の分を先に確かめ、
# 実行は services journal updates ntp service sudo ssh firewall の順に行う。終了コードは 0 = 成功、
# 1 = 中止・失敗、2 = 使い方の誤り。
#
# ssh と firewall は、締め出される恐れのある変更なので、適用すると「確定待ち」になる。
# 別の端末で入り直して確かめ、sudo bash scripts/harden.sh --confirm で確定する。確定しないと、
# --revert-after の分数で自動で元に戻る（再起動しても戻る）。すでに確定してある設定を変えて確定
# しなかったときは、harden.sh を使う前ではなく、最後に確定した設定に戻る。元に戻す処理は、
# リポジトリの版ではなく root だけが書ける /var/lib/campus-chime-harden/harden.sh の写しを実行する。
# sudo の手順も、パスワードの確認が終わるまでの 5 分だけ「確定待ち」にする（--confirm は要らない）。
# Ctrl-C・回線切れ・強制終了などでパスワードを確かめられなかったときは、NOPASSWD の設定を元に戻す
# （止められたときはその場で、プロセスが消えたときは 5 分後の自動復元で）。
# どの手順も、途中で止められたら（Ctrl-C など）、その手順を元に戻してから終わる。
# 放送の最中にサービスを止めないよう、service・updates・ntp（と nftables を入れる firewall）は、
# 先に campus_chime.py --wait-idle で放送の終わりを待つ。apt を動かす手順と時計を変える手順
# （updates・ntp・nftables を入れる firewall）は、さらに次の放送まで 10 分以上あることを確かめる。
#
set -euo pipefail

umask 022

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# テスト用の入れ物。CHIME_SYSROOT が空（本番）でなければ、絶対パスの前に付ける。外のコマンドは
# 名前だけで呼ぶ（PATH で探す）。ユニットファイルの中身に書くパスには付けない。
SYSROOT="${CHIME_SYSROOT:-}"
PROC_DIR="${CHIME_PROC:-/proc}"
if [ -z "${SYSROOT}" ]; then
  # 普通の利用者の PATH には /usr/sbin が無く、sshd や nft が見つからない。末尾に足す（先頭には足さない）。
  PATH="${PATH}:/usr/local/sbin:/usr/sbin:/sbin"
fi
export PATH
export DEBIAN_FRONTEND=noninteractive

# 実システムのパス（SYSROOT を付けて使う）。
STATE_REAL=/var/lib/campus-chime-harden
STATE_DIR="${SYSROOT}${STATE_REAL}"
PENDING_FILE="${STATE_DIR}/pending"
SSHD_MAIN=/etc/ssh/sshd_config
SSHD_DROPIN=/etc/ssh/sshd_config.d/01-campus-chime-harden.conf
FW_CONF_DIR=/etc/campus-chime-harden
FW_NFT_FILE=/etc/campus-chime-harden/firewall.nft
FW_UNIT=/etc/systemd/system/campus-chime-firewall.service
FW_FAMILY=inet
FW_TABLE_NAME=campus_chime_filter
REVERT_SERVICE=/etc/systemd/system/campus-chime-harden-revert.service
REVERT_TIMER=/etc/systemd/system/campus-chime-harden-revert.timer
JOURNAL_DROPIN=/etc/systemd/journald.conf.d/50-campus-chime-harden.conf
JOURNAL_DIR=/var/log/journal
APT_UPGRADES_CONF=/etc/apt/apt.conf.d/52campus-chime-unattended-upgrades
APT_PERIODIC_CONF=/etc/apt/apt.conf.d/21campus-chime-auto-upgrades
APT_DAILY_DROPIN=/etc/systemd/system/apt-daily.timer.d/50-campus-chime-harden.conf
APT_UPGRADE_DROPIN=/etc/systemd/system/apt-daily-upgrade.timer.d/50-campus-chime-harden.conf
SERVICE_NAME=campus_chime.service
SERVICE_DROPIN=/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf
CHRONY_SOURCES=/etc/chrony/sources.d/campus-chime-harden.sources
# 自動復元が元に戻した手順の記録（--confirm が、確定できなかった理由を正直に伝えるため）。
REVERTED_FILE="${STATE_DIR}/reverted"
# sudo の手順が NOPASSWD を外している間の安全網（確定待ち）の長さ（分）。
SUDO_WINDOW=5
# apt を動かす手順・時計を変える手順の前に、次の放送まで空けておく分数。
LOOKAHEAD_MINUTES=10

ALL_STEPS=(ssh firewall sudo services journal updates service ntp)
# 実行する順。止めても害の少ないものから、締め出される恐れのある ssh・firewall を最後に行う。
APPLY_ORDER=(services journal updates ntp service sudo ssh firewall)
RECOMMENDED_STEPS=(services journal ssh firewall)
SERVICES_UNITS=(bluetooth.service hciuart.service ModemManager.service)

TARGET_USER=pi
REVERT_AFTER=10
DRY_RUN=0
FORCE=0
TEST_SOUND=0
MODE=apply
STEPS=()
SSH_FROM=()
NTP_SERVERS=()

IS_ROOT=0
USER_UID=""
USER_HOME=""
OS_CODENAME=""
OWN_BAD=()
ROLLBACK_LIST=()
PRE_FAIL=0
NOW=""
CURRENT_STEP=""
SSH_PORTS=()
SSH_PORTS_NOTE=""
CUR_PEERS=()
CUR_PEERS_WHY=""
CHIME_CONFIG_JSON=""
CHIME_CONFIG_STATE=""
declare -A STEP_SKIP=()

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m警告: %s\033[0m\n' "$*" >&2; }
fail() { printf '\033[31m中止: %s\033[0m\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
ok() { printf '\033[32m%s\033[0m\n' "$*"; }
# 事前の確認で見つかった問題。表示だけして数え、あとでまとめて止める（理由をすべて見せるため）。
refuse() { printf '\033[31m  NG: %s\033[0m\n' "$*" >&2; PRE_FAIL=$((PRE_FAIL + 1)); }
# --dry-run で、変えるはずのことを表示する。
plan() { printf '  [予定] %s\n' "$*"; }
plan_file() {
  plan "書き込み: $1（mode $2）"
  printf '%s\n' "$3" | sed 's/^/        | /'
}

usage_error() {
  printf '%s\n' "$*" >&2
  exit 2
}

quote_cmd() {
  local out
  out="$(printf '%q ' "$@")"
  printf '%s' "${out% }"
}

# 画面に出す「次に打つコマンド」。新しい SSH の画面は ホームのフォルダから始まるので、
# 相対パス（scripts/harden.sh）ではなく、このスクリプトの絶対パスで書く。
harden_cmd() { printf 'sudo bash %q' "${SCRIPT_PATH}"; }

join_comma() {
  local out="" item
  for item in "$@"; do
    out+="${out:+, }${item}"
  done
  printf '%s' "${out}"
}

# 状態の置き場の log に 1 行残す（残せなくても続ける）。
state_log() {
  [ "${DRY_RUN}" -eq 0 ] || return 0
  [ -d "${STATE_DIR}" ] || return 0
  printf '%s %s\n' "$(date '+%F %T')" "$*" >> "${STATE_DIR}/log" 2>/dev/null || true
}

# 変更を伴うコマンドは、これを通して呼ぶ。--dry-run では表示するだけ。
sys() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "実行: $(quote_cmd "$@")"
    return 0
  fi
  state_log "run $(quote_cmd "$@")"
  "$@"
}

# ---------------------------------------------------------------------------
# ファイルの書き込み・控え・復元
# ---------------------------------------------------------------------------

# 同じ場所の一時ファイルに書いてから置き換える（読む側に書きかけを見せない）。mode は明示する。
write_atomic() {
  local path="$1" mode="$2" content="$3" tmp
  tmp="${path}.harden-tmp.$$"
  if ! { printf '%s\n' "${content}" > "${tmp}" && chmod "${mode}" "${tmp}" && mv -f "${tmp}" "${path}"; } 2>/dev/null; then
    rm -f "${tmp}"
    return 1
  fi
}

# 実システムのパス $1 の中身と mode が、$3・$2 と同じか。
file_is() {
  local path="${SYSROOT}$1" actual
  [ -f "${path}" ] || return 1
  { actual="$(<"${path}")"; } 2>/dev/null || return 1
  [ "${actual}" = "$3" ] || return 1
  [ "$(stat -c %a "${path}" 2>/dev/null)" = "$2" ]
}

# いまの時刻（エポック秒）を NOW に入れる。
now() { NOW="$(date +%s)"; }

# 手順 $1 が作ったもの（f = ファイル、d = ディレクトリ、r = SYSROOT の外の実パスのディレクトリ）を $3 として記録する。
# 作った直後に呼ぶ。
record_created() {
  mkdir -p "${STATE_DIR}/created" || return 1
  printf '%s %s\n' "$2" "$3" >> "${STATE_DIR}/created/$1"
}

created_has() {
  [ -f "${STATE_DIR}/created/$1" ] && grep -Fxq -- "$2 $3" "${STATE_DIR}/created/$1"
}

# 控え $2 が、元のファイル $1 の完全な写しか（中身が同じか。シンボリックリンクなら行き先が同じか）。
backup_is_complete() {
  if [ -L "$1" ]; then
    [ -L "$2" ] && [ "$(readlink "$1")" = "$(readlink "$2")" ]
  else
    [ -f "$2" ] && cmp -s "$1" "$2"
  fi
}

# ファイル $2 を書き換える・消す前に呼ぶ。元のファイルがあれば一度だけ控え（2 回目以降は最初の
# 控えを残す）、無ければ「この手順が作った」と記録する。控えは、別名に最後まで写し、中身が同じと
# 確かめてから本来の名前に置く（途中で失敗・停止した写しを、元のファイルとして戻さないため）。
# 控えを完全に作れないときは 1 を返す（呼ぶ側は、そのファイルを変更しない）。
claim_path() {
  local step="$1" path="$2" real backup tmp
  real="${SYSROOT}${path}"
  backup="${STATE_DIR}/backup/${step}${path}"
  if [ -e "${backup}" ] || [ -L "${backup}" ]; then
    return 0
  fi
  if created_has "${step}" f "${path}"; then
    return 0
  fi
  if [ -e "${real}" ] || [ -L "${real}" ]; then
    mkdir -p "${backup%/*}" || return 1
    tmp="${backup}.harden-tmp.$$"
    rm -rf -- "${tmp}"
    if ! cp -a "${real}" "${tmp}" || ! backup_is_complete "${real}" "${tmp}" || ! mv -f "${tmp}" "${backup}"; then
      rm -rf -- "${tmp}"
      return 1
    fi
    state_log "backup ${step} ${path}"
  else
    record_created "${step}" f "${path}" || return 1
  fi
}

# ディレクトリ $2 が無ければ、親から順に作る。作ったものは作った直後に記録する（戻すときに消すため）。
ensure_dir() {
  local step="$1" path="$2" mode="$3" parent
  if [ "${DRY_RUN}" -eq 1 ]; then
    [ -d "${SYSROOT}${path}" ] || plan "ディレクトリを作成: ${path}"
    return 0
  fi
  [ -d "${SYSROOT}${path}" ] && return 0
  parent="${path%/*}"
  if [ -n "${parent}" ]; then
    ensure_dir "${step}" "${parent}" "${mode}" || return 1
  fi
  # 作る前に記録する（作った直後に止められても、戻すときに消せるように。無ければ戻すとき何もしない）。
  record_created "${step}" d "${path}" || return 1
  mkdir "${SYSROOT}${path}" || return 1
  chmod "${mode}" "${SYSROOT}${path}" || return 1
  state_log "mkdir ${step} ${path}"
}

# 締め出しにつながる変更（ssh・sudo）を始める直前に、ここまでに書いたもの（元のファイルの控え・確定待ちの
# 記録・自動復元の時計の単位ファイルと root の写し）をディスクへ出す。別名に書いて mv で置いただけだと、
# 電源が落ちたとき、変更そのもの（消す・置き換える）だけがディスクに残り、安全網が空のファイルになりうる。
flush_before_risky_change() {
  case "$1" in
    ssh | sudo) ;;
    *) return 0 ;;
  esac
  command -v sync > /dev/null 2>&1 || return 0
  sync || warn "sync に失敗しました。電源が落ちたときの備えが弱くなります（強制終了や回線切れには、影響しません）。"
}

# 手順 $1 のファイル $2 を、内容 $4・mode $3 で書く（元の控えを取ってから、一時ファイル経由で）。
put_file() {
  local step="$1" path="$2" mode="$3" content="$4"
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan_file "${path}" "${mode}" "${content}"
    return 0
  fi
  claim_path "${step}" "${path}" || { warn "元のファイルの控えを作れません: ${path}"; return 1; }
  flush_before_risky_change "${step}"
  write_atomic "${SYSROOT}${path}" "${mode}" "${content}" || { warn "書き込めません: ${path}"; return 1; }
  state_log "write ${step} ${path}"
}

# 手順 $1 のファイル $2 を消す（元の控えを取ってから）。
del_file() {
  local step="$1" path="$2"
  if [ "${DRY_RUN}" -eq 1 ]; then
    [ -e "${SYSROOT}${path}" ] && plan "削除: ${path}"
    return 0
  fi
  claim_path "${step}" "${path}" || { warn "元のファイルの控えを作れません: ${path}"; return 1; }
  flush_before_risky_change "${step}"
  rm -f "${SYSROOT}${path}" || { warn "削除できません: ${path}"; return 1; }
  state_log "remove ${step} ${path}"
}

# 控え $1 を $2 へ、一時ファイル経由で戻す（持ち主・mode・時刻は控えのまま）。
restore_one() {
  local src="$1" dest="$2" tmp
  tmp="${dest}.harden-tmp.$$"
  mkdir -p "${dest%/*}" || return 1
  if ! cp -a "${src}" "${tmp}" 2>/dev/null; then
    rm -f "${tmp}"
    return 1
  fi
  if ! mv -f "${tmp}" "${dest}"; then
    rm -f "${tmp}"
    return 1
  fi
}

# パス $1 の書きかけの一時ファイル（write_atomic・restore_one が、止められて残したもの）を消す。
sweep_tmp() {
  local f
  for f in "${SYSROOT}$1".harden-tmp.*; do
    if [ -e "${f}" ] || [ -L "${f}" ]; then
      rm -f -- "${f}" || true
    fi
  done
}

# 手順 $1 が変えたファイルを元に戻す: 控えを戻し、自分で作ったものを（作った順の逆に）消す。
# 1 つでも失敗したら 1 を返すが、残りは続ける。成功するまで控えと記録は消さない。
# 書きかけの控え（*.harden-tmp.*）は戻さない。
restore_step_files() {
  local step="$1" rc=0 base files file entries=() i line kind path
  base="${STATE_DIR}/backup/${step}"
  if [ -d "${base}" ]; then
    if ! files="$(find "${base}" \( -type f -o -type l \) ! -name '*.harden-tmp.*' -print)"; then
      warn "控えの一覧を読めませんでした: ${base}"
      rc=1
      files=""
    fi
    while IFS= read -r file; do
      [ -n "${file}" ] || continue
      path="${file#"${base}"}"
      sweep_tmp "${path}"
      if restore_one "${file}" "${SYSROOT}${path}"; then
        state_log "restore ${step} ${path}"
      else
        warn "元に戻せません: ${path}"
        rc=1
      fi
    done <<< "${files}"
  fi
  if [ -f "${STATE_DIR}/created/${step}" ]; then
    mapfile -t entries < "${STATE_DIR}/created/${step}"
    for ((i = ${#entries[@]} - 1; i >= 0; i--)); do
      line="${entries[i]}"
      kind="${line%% *}"
      path="${line#* }"
      case "${kind}" in
        f)
          sweep_tmp "${path}"
          if ! rm -f -- "${SYSROOT}${path}"; then
            warn "削除できません: ${path}"
            rc=1
          fi
          ;;
        d)
          if [ -d "${SYSROOT}${path}" ] && ! rmdir "${SYSROOT}${path}" 2>/dev/null; then
            warn "空でないので残しました: ${path}"
          fi
          ;;
        r)
          # SYSROOT の外の実パス（設定が指すリポジトリの中の書き込み先）。
          if [ -d "${path}" ] && ! rmdir "${path}" 2>/dev/null; then
            warn "空でないので残しました: ${path}"
          fi
          ;;
      esac
    done
  fi
  return "${rc}"
}

cleanup_step_state() {
  rm -rf "${STATE_DIR}/backup/${1:?}" "${STATE_DIR}/created/${1:?}" "${STATE_DIR}/applied/${1:?}" \
    "${STATE_DIR}/confirmed/${1:?}" "${STATE_DIR}/confirmed/${1:?}.prev" || return 1
  reverted_forget "$1"
}

# ---------------------------------------------------------------------------
# 確定した設定の控え（ssh・firewall）
# ---------------------------------------------------------------------------
# --confirm のときに、確定した設定のファイルを confirmed/<手順>/<絶対パス> に写す。すでに確定した
# 手順を変えて、確定しないまま自動復元が働くときは、harden.sh を使う前ではなく、ここへ戻す。

confirmed_has() { [ -f "${STATE_DIR}/confirmed/$1/.complete" ]; }

# 手順 $1 の確定した設定として、実システムのファイル $2... を控える（別名に写し、最後に置き換える）。
# 古い控えは、新しい控えを置くまで消さない（消してから置くと、その間に止められたとき、どちらも無くなって、
# 自動復元が確定した設定ではなく元の状態へ戻してしまう）。古い控えは .prev に退け、新しい控えを置けたら消す。
# 止められて .prev が残ったときは、confirmed_recover がロックを取るときに直す。
snapshot_confirmed() {
  local step="$1" dir tmp prev path
  shift
  dir="${STATE_DIR}/confirmed/${step}"
  tmp="${dir}.harden-tmp.$$"
  prev="${dir}.prev"
  rm -rf -- "${tmp}"
  mkdir -p "${tmp}" || return 1
  for path in "$@"; do
    if [ ! -f "${SYSROOT}${path}" ]; then
      warn "確定した設定のファイルがありません: ${path}"
      rm -rf -- "${tmp}"
      return 1
    fi
    if ! { mkdir -p "${tmp}${path%/*}" && cp -a "${SYSROOT}${path}" "${tmp}${path}" \
      && backup_is_complete "${SYSROOT}${path}" "${tmp}${path}"; }; then
      warn "確定した設定を控えられません: ${path}"
      rm -rf -- "${tmp}"
      return 1
    fi
  done
  : > "${tmp}/.complete" || { rm -rf -- "${tmp}"; return 1; }
  rm -rf -- "${prev}" || { rm -rf -- "${tmp}"; return 1; }
  if [ -e "${dir}" ] && ! mv "${dir}" "${prev}"; then
    rm -rf -- "${tmp}"
    return 1
  fi
  if ! mv "${tmp}" "${dir}"; then
    if [ -e "${prev}" ] && [ ! -e "${dir}" ]; then
      mv "${prev}" "${dir}" || true
    fi
    rm -rf -- "${tmp}"
    return 1
  fi
  rm -rf -- "${prev}" || true
  state_log "confirmed ${step}"
}

# snapshot_confirmed が入れ替えの途中で止められて、古い控え（<手順>.prev）が残っていれば直す。
# 新しい控えが置かれていれば古い控えは要らない。置かれていなければ、古い控えを元の名前に戻す
# （確定の記録は終わっていないので、自動復元は、前に確定した設定へ戻る）。
confirmed_recover() {
  local prev cur
  for prev in "${STATE_DIR}"/confirmed/*.prev; do
    [ -d "${prev}" ] || continue
    cur="${prev%.prev}"
    if [ -f "${cur}/.complete" ] || [ ! -f "${prev}/.complete" ]; then
      rm -rf -- "${prev}" || true
    else
      rm -rf -- "${cur}" || true
      mv "${prev}" "${cur}" || true
    fi
  done
}

# 手順 $1 の確定した設定のファイルを、実システムへ戻す。1 つでも失敗したら 1。
restore_confirmed_files() {
  local base="${STATE_DIR}/confirmed/$1" files file path rc=0
  if ! files="$(find "${base}" -type f ! -name '.complete' ! -name '*.harden-tmp.*' -print)"; then
    warn "確定した設定の一覧を読めませんでした: ${base}"
    return 1
  fi
  while IFS= read -r file; do
    [ -n "${file}" ] || continue
    path="${file#"${base}"}"
    sweep_tmp "${path}"
    if restore_one "${file}" "${SYSROOT}${path}"; then
      state_log "restore-confirmed $1 ${path}"
    else
      warn "確定した設定へ戻せません: ${path}"
      rc=1
    fi
  done <<< "${files}"
  return "${rc}"
}

# ---------------------------------------------------------------------------
# 状態の記録（key=value の小さなファイル）
# ---------------------------------------------------------------------------

# meta_get FILE KEY: 最後に書かれた値を表示する。無ければ 1。
meta_get() {
  local file="$1" key="$2" line value="" found=1
  [ -f "${file}" ] || return 1
  while IFS= read -r line || [ -n "${line}" ]; do
    if [[ "${line}" == "${key}="* ]]; then
      value="${line#*=}"
      found=0
    fi
  done < "${file}"
  [ "${found}" -eq 0 ] || return 1
  printf '%s' "${value}"
}

meta_set() {
  local file="$1" key="$2" value="$3" line content=""
  if [ -f "${file}" ]; then
    while IFS= read -r line || [ -n "${line}" ]; do
      if [[ "${line}" != "${key}="* ]]; then
        content+="${line}"$'\n'
      fi
    done < "${file}"
  fi
  content+="${key}=${value}"
  write_atomic "${file}" 600 "${content}"
}

meta_del() {
  local file="$1" key="$2" line content="" kept=0
  [ -f "${file}" ] || return 0
  while IFS= read -r line || [ -n "${line}" ]; do
    if [[ "${line}" != "${key}="* ]]; then
      content+="${line}"$'\n'
      kept=1
    fi
  done < "${file}"
  if [ "${kept}" -eq 1 ]; then
    write_atomic "${file}" 600 "${content%$'\n'}"
  else
    rm -f "${file}"
  fi
}

# 手順 $1 の適用を始める。控えの値（元の状態）を applied/<step> に書く前に呼ぶ。done を外すので、
# 途中で止まれば「途中」と分かる。
begin_apply() {
  local file
  file="${STATE_DIR}/applied/$1"
  mkdir -p "${STATE_DIR}/applied" || return 1
  if ! meta_get "${file}" first_epoch > /dev/null; then
    now
    meta_set "${file}" first_epoch "${NOW}" || return 1
  fi
  meta_del "${file}" "done"
}

# 手順 $1 の適用を終えた印（以降の KEY=VALUE も記録する）。
finish_apply() {
  local step="$1" file kv
  shift
  file="${STATE_DIR}/applied/${step}"
  for kv in "$@"; do
    meta_set "${file}" "${kv%%=*}" "${kv#*=}" || return 1
  done
  now
  meta_set "${file}" epoch "${NOW}" || return 1
  meta_set "${file}" "done" 1
}

# 手順の元の状態を、最初の 1 回だけ記録する。
record_first() {
  local file
  file="${STATE_DIR}/applied/$1"
  if ! meta_get "${file}" "$2" > /dev/null; then
    meta_set "${file}" "$2" "$3"
  fi
}

step_done() { [ "$(meta_get "${STATE_DIR}/applied/$1" "done" 2>/dev/null || true)" = 1 ]; }
step_touched() {
  [ -f "${STATE_DIR}/applied/$1" ] || [ -d "${STATE_DIR}/backup/$1" ] || [ -f "${STATE_DIR}/created/$1" ]
}

# 確定待ちの記録: STEP=適用した時刻（エポック秒）と window=自動で戻るまでの分数。
pending_has() { meta_get "${PENDING_FILE}" "$1" > /dev/null 2>&1; }
pending_epoch() { meta_get "${PENDING_FILE}" "$1" 2>/dev/null || true; }
# 窓の分数。確定待ちの手順が 1 つも無いときの window だけの記録（止められて残ったもの）は、無いものとして扱う。
pending_window() {
  [ -n "$(pending_list)" ] || return 0
  meta_get "${PENDING_FILE}" window 2>/dev/null || true
}

# 確定待ちの手順を、戻す順の逆（古い→新しい）に表示する。sudo は NOPASSWD を外している間だけ載る
# （パスワードを確かめたら外す。確定の操作は要らない）。
pending_list() {
  local s
  for s in ssh firewall sudo; do
    if pending_has "${s}"; then
      printf '%s\n' "${s}"
    fi
  done
}

pending_set() {
  now
  meta_set "${PENDING_FILE}" "$1" "${NOW}" || return 1
  meta_set "${PENDING_FILE}" window "$2"
}

pending_remove() {
  local file s others=0
  file="${PENDING_FILE}"
  for s in ssh firewall sudo; do
    if [ "${s}" != "$1" ] && pending_has "${s}"; then
      others=1
    fi
  done
  if [ "${others}" -eq 0 ]; then
    # 最後の 1 つは、キーを外す前に、ファイルごと消す（途中で止められても window だけのファイルを残さない）。
    rm -f "${file}" || return 1
  else
    meta_del "${file}" "$1" || return 1
  fi
}

# 自動復元が元に戻した手順の記録（REVERTED_FILE）: epoch=いつ、<手順>=original（harden.sh を使う前）
# または confirmed（最後に確定した設定）。--confirm が「もう戻っている」と正直に伝えるために使う。
reverted_steps() {
  local s
  for s in ssh firewall sudo; do
    if meta_get "${REVERTED_FILE}" "${s}" > /dev/null 2>&1; then
      printf '%s\n' "${s}"
    fi
  done
}

reverted_record() {
  now
  meta_set "${REVERTED_FILE}" epoch "${NOW}" || return 1
  meta_set "${REVERTED_FILE}" "$1" "$2"
}

# 手順 $1 の記録を消す（手で戻した・もう一度適用した、など。記録が空になったらファイルも消す）。
reverted_forget() {
  [ -f "${REVERTED_FILE}" ] || return 0
  meta_del "${REVERTED_FILE}" "$1" || return 1
  if [ -z "$(reverted_steps)" ]; then
    rm -f "${REVERTED_FILE}" || return 1
  fi
}

# 手順の状態の表示用: applied（適用済み）、pending（確定待ち）、partial（途中）、none（未適用）。
step_state() {
  if pending_has "$1"; then
    printf 'pending'
  elif step_done "$1"; then
    printf 'applied'
  elif step_touched "$1"; then
    printf 'partial'
  else
    printf 'none'
  fi
}

# ---------------------------------------------------------------------------
# 排他・利用者・環境の確認
# ---------------------------------------------------------------------------

ensure_state_dir() {
  local sub
  for sub in "" /applied /backup /created; do
    [ -d "${STATE_DIR}${sub}" ] || mkdir -p "${STATE_DIR}${sub}" || return 1
  done
  chmod 700 "${STATE_DIR}" "${STATE_DIR}/applied" "${STATE_DIR}/backup" "${STATE_DIR}/created"
}

# 同時に 2 つ動かさない（fd 9 の flock）。$1 秒より大きければその秒数まで待ち、0 なら待たずに失敗する。
acquire_lock() {
  local wait="$1"
  ensure_state_dir || fail "状態の置き場（${STATE_REAL}）を作れません。何も変更していません。"
  if ! { exec 9> "${STATE_DIR}/lock"; } 2>/dev/null; then
    fail "排他用のファイル（${STATE_REAL}/lock）を開けません。何も変更していません。"
  fi
  if [ "${wait}" -gt 0 ]; then
    flock -w "${wait}" 9 || fail "ほかの harden.sh が ${wait} 秒たっても終わりませんでした。"
  else
    flock -n 9 || fail "ほかの harden.sh が実行中です。終わってからもう一度実行してください。何も変更していません。"
  fi
  # 前の実行が止められて残した書きかけの一時ファイル（ロックを持っている間だけ消せる）。
  find "${STATE_DIR}" -name '*.harden-tmp.*' -exec rm -rf {} + 2> /dev/null || true
  confirmed_recover
}

# 元に戻すときは、利用者がいなくても進める（放送の確認が要る手順だけが、確認できずに失敗する）。
load_user_for_rollback() {
  if getent passwd "${TARGET_USER}" > /dev/null 2>&1; then
    load_user
  else
    warn "ユーザー ${TARGET_USER} がありません。放送の確認が要る手順（service・updates・ntp）の戻しは、確認できないため失敗します。--user で指定し直してください。"
    USER_HOME="/home/${TARGET_USER}"
  fi
}

load_user() {
  local entry
  entry="$(getent passwd "${TARGET_USER}" 2>/dev/null)" \
    || fail "ユーザー ${TARGET_USER} がありません。--user で、サービスを動かしている利用者を指定してください。何も変更していません。"
  IFS=: read -r _ _ USER_UID _ _ USER_HOME _ <<< "${entry}"
  [ -n "${USER_HOME}" ] || USER_HOME="/home/${TARGET_USER}"
}

# /etc/os-release を読み、Debian / Raspberry Pi OS の bookworm か trixie でなければ、事前の確認の問題にする。
check_os() {
  local file="${SYSROOT}/etc/os-release" line id="" like="" codename=""
  OS_CODENAME=""
  if [ -r "${file}" ]; then
    while IFS= read -r line || [ -n "${line}" ]; do
      case "${line}" in
        ID=*) id="${line#ID=}" ;;
        ID_LIKE=*) like="${line#ID_LIKE=}" ;;
        VERSION_CODENAME=*) codename="${line#VERSION_CODENAME=}" ;;
      esac
    done < "${file}"
  fi
  id="${id//[\"\']/}"
  like="${like//[\"\']/}"
  codename="${codename//[\"\']/}"
  OS_CODENAME="${codename}"
  case " ${id} ${like} " in
    *" debian "* | *" raspbian "*) ;;
    *) os_problem "このスクリプトは Debian / Raspberry Pi OS 用です（ID=${id:-不明}）。"; return 0 ;;
  esac
  case "${codename}" in
    bookworm | trixie) ;;
    *) os_problem "確かめてあるのは Debian 12（bookworm）と 13（trixie）だけです（VERSION_CODENAME=${codename:-不明}）。" ;;
  esac
}

os_problem() {
  if [ "${FORCE}" -eq 1 ]; then
    warn "$1 --force が付いているので、このまま進めます。"
  else
    refuse "$1 ほかの OS で試すなら --force を付けてください。"
  fi
}

# 次の放送の再生開始までの秒数を表示する。campus_chime.py --schedule 1 の出力（標準入力）の
# 「現在時刻: 日付 時刻」と、最初の「時報 日付 時刻（再生開始 時刻）」の行を読む（どちらも放送の時間帯の
# 時刻）。「現在時刻」の行が無ければ、引数 1 の時間帯で今を求める。予定が 1 つもなければ none。
# 読み取れなければ終了コード 3。
NEXT_PY='
import re
import sys
import datetime

text = sys.stdin.read()
now = None
m = re.search(r"現在時刻: (\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)", text)
if m:
    now = datetime.datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
elif len(sys.argv) > 1 and sys.argv[1]:
    try:
        import zoneinfo
        now = datetime.datetime.now(zoneinfo.ZoneInfo(sys.argv[1])).replace(tzinfo=None)
    except Exception:
        sys.exit(3)
else:
    sys.exit(3)
m = re.search(r"^\s*- \S+ (\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)（再生開始 (\d\d:\d\d:\d\d)）", text, re.M)
if not m:
    if "予定されたイベントはありません" in text:
        print("none")
        sys.exit(0)
    sys.exit(3)
at = datetime.datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
play = datetime.datetime.strptime(m.group(1) + " " + m.group(3), "%Y-%m-%d %H:%M:%S")
if play > at:
    play -= datetime.timedelta(days=1)
print(int((play - now).total_seconds()))
'

# 次の放送まで $1 分以上あることを確かめる。近ければ、放送が終わるまで待てる範囲（--wait-idle と同じ
# 6 分まで）なら待ち、待てなければ断る。読み取れなければ、--force がなければ断る。
ensure_lookahead() {
  local ahead="$1" tries=0 out secs wait_s tz
  tz="$(chime_timezone 2> /dev/null || true)"
  while :; do
    secs=""
    if out="$(as_user python3 "${REPO_DIR}/campus_chime.py" --schedule 1 2> /dev/null)"; then
      secs="$(printf '%s\n' "${out}" | python3 -I -c "${NEXT_PY}" "${tz}" 2> /dev/null)" || secs=""
    fi
    if [ -z "${secs}" ]; then
      if [ "${FORCE}" -eq 1 ]; then
        warn "次の放送の時刻を読み取れません（campus_chime.py --schedule 1）。--force が付いているので進めます。"
        return 0
      fi
      warn "次の放送の時刻を読み取れません（campus_chime.py --schedule 1）。放送の直前でないと確かめられないので止めます。承知のうえなら --force を付けてください。"
      return 1
    fi
    if [ "${secs}" = none ] || [ "${secs}" -ge $((ahead * 60)) ]; then
      return 0
    fi
    wait_s=$((secs + 180))
    if [ "${tries}" -ge 2 ] || [ "${wait_s}" -gt 360 ]; then
      warn "次の放送まで約 $(((secs < 0 ? 0 : secs) / 60 + 1)) 分です。apt を動かす手順と時計を変える手順は、次の放送まで ${ahead} 分以上あるときだけ行います。放送のない時間に、もう一度実行してください。"
      return 1
    fi
    say "次の放送が近いので、終わるまで約 $((wait_s / 60 + 1)) 分待ちます。"
    sleep "${wait_s}"
    tries=$((tries + 1))
    runuser -u "${TARGET_USER}" -- python3 "${REPO_DIR}/campus_chime.py" --wait-idle || {
      warn "放送の時間帯が終わりませんでした。放送のない時間に、もう一度実行してください。"
      return 1
    }
  done
}

# 放送の最中にサービスを止めたり apt を動かしたりしないよう、放送の終わりを待つ（最大 6 分）。
# $1（分）が 1 以上なら、次の放送までそれだけあることも確かめる（apt や時計の変更は長引くため）。
# campus_chime.py が無いときは警告して続ける。
ensure_idle() {
  local ahead="${1:-0}" rc=0
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "放送の最中でないことを確認: runuser -u ${TARGET_USER} -- python3 ${REPO_DIR}/campus_chime.py --wait-idle"
    if [ "${ahead}" -gt 0 ]; then
      plan "次の放送まで ${ahead} 分以上あることを確認: runuser -u ${TARGET_USER} -- python3 ${REPO_DIR}/campus_chime.py --schedule 1"
    fi
    return 0
  fi
  if [ ! -f "${REPO_DIR}/campus_chime.py" ]; then
    warn "campus_chime.py が見つからないため、放送の時間帯の確認を省略します（${REPO_DIR}）。"
    return 0
  fi
  say "放送の最中でないことを確認します（放送の最中なら、終わるまで最大 6 分待ちます）。"
  runuser -u "${TARGET_USER}" -- python3 "${REPO_DIR}/campus_chime.py" --wait-idle || rc=$?
  case "${rc}" in
    0) ;;
    1) warn "放送の時間帯が終わりませんでした。放送のない時間に、もう一度実行してください。"; return 1 ;;
    2) warn "設定ファイル（config.json）を読めないため、放送の時間帯かどうか確認できませんでした。上の表示のとおり直してください。"; return 1 ;;
    *) warn "放送の時間帯の確認に失敗しました（終了コード ${rc}）。"; return 1 ;;
  esac
  if [ "${ahead}" -gt 0 ]; then
    ensure_lookahead "${ahead}" || return 1
  fi
}

# 読むだけの apt の呼び出し（apt-cache policy・apt-get -s）は、これを通す: apt_read 道具名 引数...
# root で apt を動かすと、索引の写し（/var/cache/apt の pkgcache.bin・srcpkgcache.bin）を作り直して
# しまう。--dry-run は何も変えないと約束しているので、その保存先を空にして何も書かせない（索引はそのつど
# メモリの中に作る）。scripts/security_check.sh の apt_read と同じ。
apt_read() {
  local tool="$1"
  shift
  "${tool}" -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= "$@"
}

# apt-get install の前に一覧を新しくする（古い一覧のままだと、入れるときに 404 になる）。失敗は警告にとどめる
# （一覧が新しければ入れられるため）。入れられなかったときの案内は apt_install_hint。
apt_refresh() {
  if ! sys apt-get update; then
    warn "apt-get update が失敗しました。ネットワーク（学内の網につながっているか、名前が引けるか）を確かめてください。古い一覧のまま続けます。"
  fi
}

apt_install_hint() {
  warn "ネットワークにつながっているか確かめ、sudo apt update を実行してから、もう一度実行してください。"
}

unit_exists() {
  local out first
  out="$(systemctl list-unit-files "$1" 2>/dev/null)" || true
  while read -r first _; do
    [ "${first}" = "$1" ] && return 0
  done <<< "${out}"
  return 1
}

unit_state() { systemctl is-active "$1" 2>/dev/null || true; }
unit_active() { [ "$(unit_state "$1")" = active ]; }
unit_enabled_state() { systemctl is-enabled "$1" 2>/dev/null || true; }

pkg_installed() {
  [ "$(dpkg-query -W -f "\${Status}" "$1" 2>/dev/null || true)" = "install ok installed" ]
}

# ---------------------------------------------------------------------------
# 自動復元（ssh・firewall の変更が確定されないときの安全網）
# ---------------------------------------------------------------------------

revert_service_content() {
  printf '%s\n' \
    '[Unit]' \
    'Description=campus-chime harden.sh の自動復元（確定されなかった変更を元に戻す）' \
    '' \
    '[Service]' \
    'Type=oneshot' \
    "ExecStart=/bin/bash ${STATE_REAL}/harden.sh --rollback-pending" \
    'Restart=on-failure' \
    'RestartSec=60' \
    'TimeoutStartSec=2h'
}

revert_timer_content() {
  printf '%s\n' \
    '[Unit]' \
    'Description=campus-chime harden.sh の自動復元の時計（--confirm で止める）' \
    '' \
    '[Timer]' \
    "OnActiveSec=$1min" \
    'AccuracySec=1s' \
    '' \
    '[Install]' \
    'WantedBy=timers.target'
}

# 自動復元の時計を張り直した印（番号）。時計が起こした --rollback-pending は、起きたときの番号を覚えておき、
# ほかの harden.sh の終わりを待ったあとに番号が変わっていれば、その間に時計が張り直されている
# （新しい窓の分だけ待つと案内した変更を、古い時計の実行が、すぐ戻してしまわないため）。
REVERT_GEN_FILE="${STATE_DIR}/revert_gen"

revert_gen() {
  local gen=""
  { gen="$(<"${REVERT_GEN_FILE}")"; } 2> /dev/null || true
  [[ "${gen}" =~ ^[0-9]{1,15}$ ]] || gen=""
  printf '%s' "${gen}"
}

revert_gen_bump() {
  local gen
  gen="$(revert_gen)"
  write_atomic "${REVERT_GEN_FILE}" 600 "$(( 10#${gen:-0} + 1 ))" || warn "自動復元の時計の番号を書けません（${STATE_REAL}/revert_gen）。"
}

# 自動復元の時計を張る。root だけが書ける写し（状態の置き場の harden.sh）を実行するので、リポジトリ
# （pi が書ける）の版は使わない。enable してあるので、確定前に再起動しても、起動の N 分後に戻る。
# restart で、N 分を数え直す。
arm_revert() {
  local minutes="$1" tmp
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "root の写しを作る: ${SCRIPT_PATH} -> ${STATE_REAL}/harden.sh（mode 700）"
    plan_file "${REVERT_SERVICE}" 644 "$(revert_service_content)"
    plan_file "${REVERT_TIMER}" 644 "$(revert_timer_content "${minutes}")"
    plan "実行: systemctl daemon-reload"
    plan "実行: systemctl enable campus-chime-harden-revert.timer"
    plan "実行: systemctl restart campus-chime-harden-revert.timer（${minutes} 分後に自動で元に戻す）"
    return 0
  fi
  tmp="${STATE_DIR}/harden.sh.harden-tmp.$$"
  if ! { cp "${SCRIPT_PATH}" "${tmp}" && chmod 700 "${tmp}" && mv -f "${tmp}" "${STATE_DIR}/harden.sh"; } 2>/dev/null; then
    rm -f "${tmp}"
    warn "自動復元用の写し（${STATE_REAL}/harden.sh）を作れません。"
    return 1
  fi
  write_atomic "${SYSROOT}${REVERT_SERVICE}" 644 "$(revert_service_content)" \
    || { warn "書き込めません: ${REVERT_SERVICE}"; return 1; }
  write_atomic "${SYSROOT}${REVERT_TIMER}" 644 "$(revert_timer_content "${minutes}")" \
    || { warn "書き込めません: ${REVERT_TIMER}"; return 1; }
  state_log "arm revert ${minutes}min"
  systemctl daemon-reload || return 1
  systemctl enable campus-chime-harden-revert.timer || return 1
  systemctl restart campus-chime-harden-revert.timer || return 1
  # 時計を数え直せてから番号を進める（張れなかったときに、待っている古い実行を止めてしまわないため）。
  revert_gen_bump
}

# 確定待ちの手順 $1 を記録して、自動復元の時計を張る。変更を有効にする前に呼ぶ。2 つ目の手順が
# 加わるときは、前の窓との大きいほうを使う。窓の分数は $2（無ければ --revert-after）。
arm_for() {
  local step="$1" window old
  window="${2:-${REVERT_AFTER}}"
  old="$(pending_window)"
  if [ -n "${old}" ] && [ "${old}" -gt "${window}" ]; then
    window="${old}"
  fi
  if [ "${DRY_RUN}" -eq 1 ]; then
    arm_revert "${window}"
    return 0
  fi
  # 時計を先に張り、確定待ちの記録はそのあとに書く。逆だと、記録だけがあって時計が無い状態で止められたとき
  # （kill -9・電源断）、案内が約束する自動復元を、誰もしない。時計だけがあって記録が無い状態は害がない
  # （時計が起きても、戻すものが無いだけ）。
  arm_revert "${window}" || { warn "自動復元の時計を張れませんでした。"; return 1; }
  pending_set "${step}" "${window}" || { warn "確定待ちの記録を書けません。"; return 1; }
}

# 時計と単位ファイル、root の写しを片づける。1 つでも失敗したら 1（その場合は単位ファイルを残す）。
disarm_revert() {
  local had_units=0
  if [ -e "${SYSROOT}${REVERT_TIMER}" ] || [ -e "${SYSROOT}${REVERT_SERVICE}" ]; then
    had_units=1
  fi
  if [ "${had_units}" -eq 1 ]; then
    if ! systemctl disable --now campus-chime-harden-revert.timer; then
      warn "自動復元の時計を止められませんでした。"
      return 1
    fi
    rm -f "${SYSROOT}${REVERT_TIMER}" "${SYSROOT}${REVERT_SERVICE}" || return 1
    systemctl daemon-reload || return 1
  fi
  rm -f "${STATE_DIR}/harden.sh" || return 1
  state_log "disarm revert"
}

# ---------------------------------------------------------------------------
# CIDR の計算（python3 の ipaddress）
# ---------------------------------------------------------------------------

# -I: 作業フォルダ（pi が書けるリポジトリのことがある）にあるファイルを import しない。
#   norm CIDR              正規化したネットワークを表示する（読めなければ 1、IPv4 は /8・IPv6 は /32 より広いと 2）
#   collapse CIDR...       重なりをまとめて 1 行ずつ表示する（nft の interval 集合は重なりを許さない）
#   uncovered CSV ADDR...  CSV のネットワークのどれにも入らない ADDR を表示する
#   net24 ADDR             ADDR を含む /24（IPv6 は /64）を表示する
#   first CIDR             CIDR の中の代表のアドレス（ネットワークアドレスの次。狭すぎれば先頭）を表示する
CIDR_PY='
import ipaddress
import sys

op, args = sys.argv[1], sys.argv[2:]
if op == "norm":
    try:
        net = ipaddress.ip_network(args[0], strict=False)
    except ValueError:
        sys.exit(1)
    if net.prefixlen < (8 if net.version == 4 else 32):
        sys.exit(2)
    print(net)
elif op == "collapse":
    nets = [ipaddress.ip_network(a) for a in args]
    for version in (4, 6):
        for net in ipaddress.collapse_addresses([n for n in nets if n.version == version]):
            print(net)
elif op == "uncovered":
    nets = [ipaddress.ip_network(n) for n in args[0].split(",") if n]
    for peer in args[1:]:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            print(peer)
            continue
        if not any(addr.version == n.version and addr in n for n in nets):
            print(peer)
elif op == "net24":
    addr = ipaddress.ip_address(args[0])
    print(ipaddress.ip_network("%s/%d" % (addr, 24 if addr.version == 4 else 64), strict=False))
elif op == "first":
    net = ipaddress.ip_network(args[0], strict=False)
    narrow = 31 if net.version == 4 else 127
    print(net.network_address + 1 if net.prefixlen < narrow else net.network_address)
else:
    sys.exit(3)
'

cidr_tool() { python3 -I -c "${CIDR_PY}" "$@"; }

# ---------------------------------------------------------------------------
# 放送の設定（campus_chime.py --print-config）
# ---------------------------------------------------------------------------

# 利用者 TARGET_USER として実行する（root なら runuser。その利用者本人なら、そのまま）。
as_user() {
  if [ "${IS_ROOT}" -eq 1 ]; then
    runuser -u "${TARGET_USER}" -- "$@"
  else
    "$@"
  fi
}

# 設定の JSON（--print-config）を CHIME_CONFIG_JSON に読む（1 回だけ）。戻り値: 0 = 読めた、
# 1 = 読めない（campus_chime.py が無い・設定が壊れている）、2 = 読めない立場（root でも
# ${TARGET_USER} 本人でもない --dry-run）。
load_chime_config() {
  case "${CHIME_CONFIG_STATE}" in
    ok) return 0 ;;
    fail) return 1 ;;
    skip) return 2 ;;
  esac
  if [ ! -f "${REPO_DIR}/campus_chime.py" ]; then
    CHIME_CONFIG_STATE=fail
    return 1
  fi
  if [ "${IS_ROOT}" -eq 0 ] && [ "$(id -un 2>/dev/null)" != "${TARGET_USER}" ]; then
    CHIME_CONFIG_STATE=skip
    return 2
  fi
  if ! CHIME_CONFIG_JSON="$(as_user python3 "${REPO_DIR}/campus_chime.py" --print-config 2>/dev/null)"; then
    CHIME_CONFIG_STATE=fail
    return 1
  fi
  CHIME_CONFIG_STATE=ok
}

# 設定のうち、時間帯（timezone）と、apt のタイマーと重なる放送の時刻を調べる。
#   tz                  設定の timezone を表示する（標準入力は設定の JSON）
#   quiet               04:30〜06:30 に鳴る放送（時報・閉館放送）を 1 行ずつ表示する（標準入力は設定の JSON）
#   same A B            時間帯 A と B が（名前が違っても）同じ UTC との差かを、終了コードで答える
CFG_PY='
import datetime
import json
import sys

op = sys.argv[1]
if op == "tz":
    value = json.load(sys.stdin).get("timezone")
    print(value if isinstance(value, str) else "")
elif op == "quiet":
    sched = json.load(sys.stdin).get("schedule") or {}
    low, high = 4 * 60 + 30, 6 * 60 + 30
    found = []
    hourly = sched.get("hourly") or {}
    if hourly.get("enabled", True):
        minute = int(hourly.get("minute", 0))
        skip = {int(h) for h in hourly.get("skip_hours") or []}
        for hour in range(int(hourly.get("start_hour", 10)), int(hourly.get("end_hour", 16)) + 1):
            if hour not in skip and 0 <= hour <= 23 and low <= hour * 60 + minute <= high:
                found.append("%02d:%02d の時報" % (hour, minute))
    closing = sched.get("closing") or {}
    if closing.get("enabled", True):
        hour, minute = int(closing.get("hour", 16)), int(closing.get("minute", 57))
        if low <= hour * 60 + minute <= high:
            found.append("%02d:%02d の閉館放送" % (hour, minute))
    for line in found:
        print(line)
elif op == "same":
    import zoneinfo
    first, second = zoneinfo.ZoneInfo(sys.argv[2]), zoneinfo.ZoneInfo(sys.argv[3])
    start = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone.utc)
    for step in range(0, 366 * 2, 7):
        moment = start + datetime.timedelta(days=step / 2)
        if moment.astimezone(first).utcoffset() != moment.astimezone(second).utcoffset():
            sys.exit(1)
else:
    sys.exit(3)
'

# 放送の時間帯（設定の timezone）を表示する。読めなければ 1。
chime_timezone() {
  local tz
  load_chime_config || return 1
  tz="$(printf '%s' "${CHIME_CONFIG_JSON}" | python3 -I -c "${CFG_PY}" tz 2> /dev/null)" || return 1
  # 設定は ${TARGET_USER} が書き換えられる。画面に出すコマンド（timedatectl set-timezone ...）に混ざらないよう、
  # 時間帯の名前として普通の文字だけを受け付ける。
  [[ "${tz}" =~ ^[A-Za-z0-9_+./-]+$ ]] || return 1
  printf '%s' "${tz}"
}

# ---------------------------------------------------------------------------
# 手順 ssh: SSH を鍵だけにする
# ---------------------------------------------------------------------------

SSH_ALLOW_USER=""
SSH_KEY_COUNT=0
SSH_ACCEPTED_ALGOS=""
PATH_PROBLEM=""
PROBE_WHY=""
SSH_PORTS_RC=""

ssh_dropin_content() {
  printf '%s\n' \
    '# scripts/harden.sh ssh が作成（元に戻す: sudo bash scripts/harden.sh --rollback ssh）' \
    'PasswordAuthentication no' \
    'KbdInteractiveAuthentication no' \
    'PermitRootLogin no' \
    'PubkeyAuthentication yes' \
    'AuthenticationMethods publickey' \
    'X11Forwarding no'
  if [ -n "${SSH_ALLOW_USER}" ]; then
    printf 'AllowUsers %s\n' "${SSH_ALLOW_USER}"
  fi
}

# sshd が、接続元 $1 から TARGET_USER が入るときに使う設定（小文字のキーと値）。
sshd_effective() { sshd -T -C "user=${TARGET_USER},host=chime-check,addr=$1" 2>&1; }

# 「キー 値」の行の並びから、キー $2 の値を表示する。
config_value() {
  local k v
  while read -r k v; do
    if [ "${k}" = "$2" ]; then
      printf '%s' "${v}"
      return 0
    fi
  done <<< "$1"
  return 1
}

# 「キー 値」の行の並びから、キー $2 の値を（複数あれば全部）1 行ずつ表示する。
config_values() {
  local k v
  while read -r k v; do
    if [ "${k}" = "$2" ]; then
      printf '%s\n' "${v}"
    fi
  done <<< "$1"
}

# sshd が待ち受けるポートを SSH_PORTS に入れる（sshd -T の port の行と、listenaddress の行の host:port・
# [v6]:port のポート、ssh.socket 経由なら ListenStream）。調べた結果は覚えておく。調べられなければ 1
# （SSH_PORTS は空）。
# SSH が動いていない・sshd がない・root でない、のときは 22 とみなして 0（理由は SSH_PORTS_NOTE）。
# listenaddress の値 $1（host:port・[v6]:port）のポートを表示する。ポートの無い形（port の無い IPv4・
# [::1]・:: のようなかっこの無い IPv6）は何も表示しない（その待ち受けのポートは port の行が示す）。
listen_port_of() {
  case "$1" in
    \[*\]) ;;
    \[*\]:*) printf '%s' "${1##*\]:}" ;;
    *:*:*) ;;
    *:*) printf '%s' "${1##*:}" ;;
  esac
}

detect_ssh_ports() {
  if [ -z "${SSH_PORTS_RC}" ]; then
    SSH_PORTS_RC=0
    read_ssh_ports || SSH_PORTS_RC=1
  fi
  return "${SSH_PORTS_RC}"
}

read_ssh_ports() {
  local cfg value listen line got=0 ports=() port seen=" " i
  SSH_PORTS=()
  SSH_PORTS_NOTE=""
  if ! command -v sshd > /dev/null 2>&1; then
    SSH_PORTS=(22)
    SSH_PORTS_NOTE="sshd が入っていないので、SSH のポートは 22 とみなします。"
    return 0
  fi
  if ! unit_active ssh.service && ! unit_active ssh.socket; then
    SSH_PORTS=(22)
    SSH_PORTS_NOTE="SSH が動いていないので、SSH のポートは 22 とみなします。"
    return 0
  fi
  if [ "${IS_ROOT}" -eq 0 ]; then
    SSH_PORTS=(22)
    SSH_PORTS_NOTE="root でないので sshd の設定を読めません。SSH のポートは 22 とみなします（実際の実行では sshd -T で調べます）。"
    return 0
  fi
  cfg="$(sshd_effective 203.0.113.1)" || return 1
  while IFS= read -r value; do
    [ -z "${value}" ] || ports+=("${value}")
  done < <(config_values "${cfg}" port)
  # ListenAddress host:port で指したポートは、port の行（既定の 22）に出ないことがある。
  while IFS= read -r value; do
    value="$(listen_port_of "${value%% *}")"
    [ -z "${value}" ] || ports+=("${value}")
  done < <(config_values "${cfg}" listenaddress)
  if unit_active ssh.socket; then
    listen="$(systemctl show -p Listen ssh.socket 2> /dev/null)" || return 1
    while IFS= read -r line; do
      [[ "${line}" == Listen=* ]] || continue
      line="${line#Listen=}"
      line="${line%% *}"
      ports+=("${line##*:}")
      got=1
    done <<< "${listen}"
    [ "${got}" -eq 1 ] || return 1
  fi
  for port in "${ports[@]}"; do
    if [[ ! "${port}" =~ ^[0-9]{1,5}$ ]] || [ "$((10#${port}))" -lt 1 ] || [ "$((10#${port}))" -gt 65535 ]; then
      return 1
    fi
    port="$((10#${port}))"
    if [[ "${seen}" != *" ${port} "* ]]; then
      seen+="${port} "
      # 小さい順に並べて入れる（sort に頼らない）。
      for ((i = 0; i < ${#SSH_PORTS[@]}; i++)); do
        if [ "${port}" -lt "${SSH_PORTS[i]}" ]; then
          break
        fi
      done
      SSH_PORTS=("${SSH_PORTS[@]:0:i}" "${port}" "${SSH_PORTS[@]:i}")
    fi
  done
  [ "${#SSH_PORTS[@]}" -gt 0 ] || return 1
}

# 画面に出す SSH の接続のコマンド（ポートが 22 でなければ -p を付ける）。引数は ssh への追加の指定。
ssh_login_cmd() {
  local port=22 p has22=0
  detect_ssh_ports || true
  if [ "${#SSH_PORTS[@]}" -gt 0 ]; then
    port="${SSH_PORTS[0]}"
    for p in "${SSH_PORTS[@]}"; do
      [ "${p}" != 22 ] || has22=1
    done
    [ "${has22}" -eq 0 ] || port=22
  fi
  if [ "${port}" = 22 ]; then
    printf 'ssh %s%s@campus-chime.local' "${*:+$* }" "${TARGET_USER}"
  else
    printf 'ssh -p %s %s%s@campus-chime.local' "${port}" "${*:+$* }" "${TARGET_USER}"
  fi
}

# いま SSH でつながっている相手を CUR_PEERS に入れる（ループバックは除く）。調べられなければ、理由を
# CUR_PEERS_WHY に入れて 1。SSH_PORTS が空なら 22。
collect_ssh_peers() {
  local out line peer cols=() filter p ports=("${SSH_PORTS[@]}")
  CUR_PEERS=()
  CUR_PEERS_WHY=""
  [ "${#ports[@]}" -gt 0 ] || ports=(22)
  if ! command -v ss > /dev/null 2>&1; then
    CUR_PEERS_WHY="ss が見つかりません"
    return 1
  fi
  filter="( sport = :${ports[0]}"
  for p in "${ports[@]:1}"; do
    filter+=" or sport = :${p}"
  done
  filter+=" )"
  if ! out="$(ss -H -t -n state established "${filter}" 2>&1)"; then
    CUR_PEERS_WHY="ss が失敗しました: ${out}"
    return 1
  fi
  while IFS= read -r line; do
    [ -n "${line}" ] || continue
    read -r -a cols <<< "${line}"
    peer="${cols[${#cols[@]} - 1]}"
    peer="${peer%:*}"
    peer="${peer#\[}"
    peer="${peer%\]}"
    peer="${peer%%\%*}"
    peer="${peer#::ffff:}"
    case "${peer}" in
      127.* | ::1) continue ;;
    esac
    CUR_PEERS+=("${peer}")
  done <<< "${out}"
}

# この画面を動かしているのが SSH 経由か（sshd の下にいる、または SSH の環境変数がある）。
operator_on_ssh() {
  if [ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-}" ]; then
    return 0
  fi
  session_start_epoch > /dev/null 2>&1
}

# AuthorizedKeysFile の書き方（%h・%u・%U・%%）を展開する。相対パスは利用者のホームから。
expand_key_path() {
  local template="$1" out="" i ch next
  for ((i = 0; i < ${#template}; i++)); do
    ch="${template:i:1}"
    if [ "${ch}" = "%" ]; then
      i=$((i + 1))
      next="${template:i:1}"
      case "${next}" in
        h) out+="${USER_HOME}" ;;
        u) out+="${TARGET_USER}" ;;
        U) out+="${USER_UID}" ;;
        %) out+="%" ;;
        *) return 1 ;;
      esac
    else
      out+="${ch}"
    fi
  done
  case "${out}" in
    /*) ;;
    *) out="${USER_HOME}/${out}" ;;
  esac
  printf '%s' "${out}"
}

# パス $1 の持ち主が root か利用者で、グループ・その他に書き込まれていないか（sshd の StrictModes と同じ見方。
# シンボリックリンクはたどった先を見る）。問題があれば PATH_PROBLEM に理由を入れて 1。
path_is_safe() {
  local info owner mode
  PATH_PROBLEM=""
  if ! info="$(stat -L -c '%u %a' "${SYSROOT}$1" 2>/dev/null)"; then
    PATH_PROBLEM="$1 を調べられません"
    return 1
  fi
  read -r owner mode <<< "${info}"
  if [ "${owner}" != 0 ] && [ "${owner}" != "${USER_UID}" ]; then
    PATH_PROBLEM="$1 の持ち主が root でも ${TARGET_USER} でもありません"
    return 1
  fi
  if (( (8#${mode} & 8#022) != 0 )); then
    PATH_PROBLEM="$1 がグループ・その他に書き込める設定（mode ${mode}）です"
    return 1
  fi
}

# ssh-keygen -l が括弧で示す鍵の種類 $1（ED25519・RSA など）を、sshd が受け付けるか。
# 受け付ける署名の一覧（sshd -T の pubkeyacceptedalgorithms）が分からないとき（root でない）は、
# OpenSSH 9.2 が既定で受け付けない DSA だけを除く。知らない種類は、sshd に任せて数える。
key_type_usable() {
  local want alg
  case "$1" in
    ED25519) want="ssh-ed25519" ;;
    ED25519-SK) want="sk-ssh-ed25519@openssh.com" ;;
    ECDSA) want="ecdsa-sha2-nistp256 ecdsa-sha2-nistp384 ecdsa-sha2-nistp521" ;;
    ECDSA-SK) want="sk-ecdsa-sha2-nistp256@openssh.com" ;;
    RSA) want="rsa-sha2-512 rsa-sha2-256 ssh-rsa" ;;
    DSA) want="ssh-dss" ;;
    *) return 0 ;;
  esac
  if [ -z "${SSH_ACCEPTED_ALGOS}" ]; then
    [ "$1" != DSA ]
    return
  fi
  for alg in ${want}; do
    case ",${SSH_ACCEPTED_ALGOS}," in
      *",${alg},"*) return 0 ;;
    esac
  done
  return 1
}

# 鍵のファイル $1 を調べる（$2 が yes なら、/ からの各ディレクトリも）。使える鍵の数を SSH_KEY_COUNT に足す。
check_key_file() {
  local file="$1" strict="$2" dir out line count=0 info owner mode type
  if ! path_is_safe "${file}"; then
    refuse "${PATH_PROBLEM}。sshd はこの鍵を使いません（chmod go-w と chown で直してください）。"
    return 0
  fi
  if [ "${strict}" = yes ]; then
    dir="${file%/*}"
    while [ -n "${dir}" ]; do
      if ! path_is_safe "${dir}"; then
        refuse "${PATH_PROBLEM}。sshd（StrictModes）は鍵を使いません。"
      fi
      dir="${dir%/*}"
    done
    if ! path_is_safe "/"; then
      refuse "${PATH_PROBLEM}。sshd（StrictModes）は鍵を使いません。"
    fi
  fi
  if [ "${file%/*}" = "${USER_HOME}/.ssh" ]; then
    info="$(stat -L -c '%u %a' "${SYSROOT}${file%/*}" 2>/dev/null || true)"
    read -r owner mode <<< "${info}"
    if [ "${owner:-}" != "${USER_UID}" ]; then
      refuse "${file%/*} の持ち主が ${TARGET_USER} ではありません。"
    elif (( (8#${mode:-777} & 8#077) != 0 )); then
      refuse "${file%/*} の mode が 700 より緩いです（chmod 700 ${file%/*}）。"
    fi
  fi
  if ! out="$(ssh-keygen -l -f "${SYSROOT}${file}" 2>&1)"; then
    refuse "${file} に使える鍵がありません: ${out}"
    return 0
  fi
  while read -r line; do
    [ -n "${line}" ] || continue
    type=""
    if [[ "${line}" =~ \(([A-Za-z0-9-]+)\)[[:space:]]*$ ]]; then
      type="${BASH_REMATCH[1]}"
    fi
    if key_type_usable "${type}"; then
      count=$((count + 1))
    else
      say "  ${file}: ${type} の鍵は、この sshd が受け付けないので数に入れません（鍵だけにすると、この鍵では入れません）。"
    fi
  done <<< "${out}"
  SSH_KEY_COUNT=$((SSH_KEY_COUNT + count))
  say "  ${file}: 使える鍵が ${count} 本あります。"
}

print_key_howto() {
  cat >&2 <<MESSAGE
  鍵はまだ登録されていません。自分の PC で鍵を作り、この Pi に登録してから、もう一度実行してください。
    Windows（PowerShell）:
      ssh-keygen -t ed25519
      type \$env:USERPROFILE\\.ssh\\id_ed25519.pub | ssh ${TARGET_USER}@campus-chime.local "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    macOS / Linux:
      ssh-keygen -t ed25519
      ssh-copy-id ${TARGET_USER}@campus-chime.local
    登録できたら、パスワードなしで ssh ${TARGET_USER}@campus-chime.local と入れることを確かめます。
MESSAGE
}

# 鍵だけにすると、パスワードで入っていた他のアカウントが入れなくなる。他にログインできる
# アカウント（UID 1000〜59999・ログインシェルあり）を探し、鍵のないものを数える。
check_other_accounts() {
  local all name uid home shell file others=() lacking=()
  SSH_ALLOW_USER="${TARGET_USER}"
  # 一覧を取れないと、ほかのアカウントを見落として AllowUsers で締め出しかねない。
  if ! all="$(getent passwd 2>/dev/null)" || [ -z "${all}" ]; then
    if [ "${FORCE}" -eq 1 ]; then
      warn "アカウントの一覧（getent passwd）を取れません。--force が付いているので、AllowUsers は書かずに進めます。"
      SSH_ALLOW_USER=""
    else
      refuse "アカウントの一覧（getent passwd）を取れません。ほかにログインできるアカウントを見落とすと締め出すので止めます。承知のうえなら --force を付けてください。"
    fi
    return 0
  fi
  while IFS=: read -r name _ uid _ _ home shell; do
    [ -n "${name}" ] || continue
    [ "${name}" != "${TARGET_USER}" ] || continue
    if [[ ! "${uid}" =~ ^[0-9]+$ ]] || [ "${uid}" -lt 1000 ] || [ "${uid}" -gt 59999 ]; then
      continue
    fi
    case "${shell}" in
      "" | */nologin | */false) continue ;;
    esac
    others+=("${name}")
    file="${home}/.ssh/authorized_keys"
    if [ ! -s "${SYSROOT}${file}" ] || ! ssh-keygen -l -f "${SYSROOT}${file}" > /dev/null 2>&1; then
      lacking+=("${name}")
    fi
  done <<< "${all}"
  if [ "${#others[@]}" -eq 0 ]; then
    return 0
  fi
  SSH_ALLOW_USER=""
  say "  ほかにもログインできるアカウントがあります: ${others[*]}（AllowUsers は書きません。書くと、そのアカウントが入れなくなるため）"
  if [ "${#lacking[@]}" -gt 0 ]; then
    if [ "${FORCE}" -eq 1 ]; then
      warn "鍵の無いアカウント（${lacking[*]}）は、鍵だけにすると SSH で入れなくなります。--force が付いているので進めます。"
    else
      refuse "鍵の無いアカウント（${lacking[*]}）は、鍵だけにすると SSH で入れなくなります。先に鍵を登録するか、承知のうえなら --force を付けてください。"
    fi
  fi
}

preflight_ssh() {
  local cfg="" akf=".ssh/authorized_keys .ssh/authorized_keys2" strict=yes template keyfile templates=() journal desired
  log "確認: ssh"
  if ! command -v sshd > /dev/null 2>&1; then
    refuse "sshd が見つかりません（openssh-server が入っていません）。"
    return 0
  fi
  if ! unit_active ssh.service && ! unit_active ssh.socket; then
    STEP_SKIP[ssh]="SSH が動いていない（ssh.service も ssh.socket も止まっている）ので、守るものがありません。何もしません。"
    return 0
  fi
  if ! command -v ssh-keygen > /dev/null 2>&1; then
    refuse "ssh-keygen が見つかりません（openssh-client が必要です）。"
    return 0
  fi
  if ! grep -Eiq '^[[:space:]]*Include[[:space:]]+.*sshd_config\.d' "${SYSROOT}${SSHD_MAIN}" 2>/dev/null; then
    refuse "${SSHD_MAIN} が /etc/ssh/sshd_config.d/ を読み込みません（Include がありません）。この設定ファイルは効かないので、手では ${SSHD_MAIN} を直す必要があります。"
  fi
  SSH_ACCEPTED_ALGOS=""
  if [ "${IS_ROOT}" -eq 1 ]; then
    if cfg="$(sshd_effective 203.0.113.1)"; then
      akf="$(config_value "${cfg}" authorizedkeysfile || printf '%s' "${akf}")"
      strict="$(config_value "${cfg}" strictmodes || printf yes)"
      SSH_ACCEPTED_ALGOS="$(config_value "${cfg}" pubkeyacceptedalgorithms || true)"
    else
      refuse "sshd の設定を読めません: ${cfg}"
      return 0
    fi
  else
    say "  （root でないので sshd の設定は読めません。鍵の置き場所は既定のものとして確かめます）"
  fi

  SSH_KEY_COUNT=0
  read -r -a templates <<< "${akf}"
  for template in "${templates[@]}"; do
    if ! keyfile="$(expand_key_path "${template}")"; then
      say "  AuthorizedKeysFile の ${template} は読み解けないので、飛ばします。"
      continue
    fi
    if [ -e "${SYSROOT}${keyfile}" ]; then
      check_key_file "${keyfile}" "${strict}"
    fi
  done
  if [ "${SSH_KEY_COUNT}" -eq 0 ]; then
    refuse "${TARGET_USER} の使える公開鍵がありません（ssh-dss の鍵は OpenSSH 9.2 では使えません）。鍵だけにすると、誰も入れなくなります。"
    print_key_howto
  fi
  check_other_accounts

  if command -v journalctl > /dev/null 2>&1; then
    journal="$(journalctl -u ssh.service --since "-30 days" -o cat 2>/dev/null || true)"
    if [[ "${journal}" != *"Accepted publickey for ${TARGET_USER} "* ]]; then
      warn "過去 30 日に、鍵で ${TARGET_USER} が入った記録がありません。鍵で入れることを、必ず別の画面で確かめてください（確かめないと ${REVERT_AFTER} 分後に自動で元に戻ります）。"
    fi
  fi

  desired="$(ssh_dropin_content)"
  if step_done ssh && file_is "${SSHD_DROPIN}" 644 "${desired}"; then
    if [ "${IS_ROOT}" -eq 1 ] && ! ssh_probe_all; then
      # ファイルは同じでも、ほかの設定（別のドロップインや Match）で、パスワードログインが効いてしまっている。
      warn "設定のファイルは適用済みと同じですが、sshd はまだ鍵だけになっていません: ${PROBE_WHY} もう一度適用します。"
    elif pending_has ssh; then
      STEP_SKIP[ssh]="適用済みですが、まだ確定していません。別の端末で確かめて、$(harden_cmd) --confirm を実行してください。"
    else
      STEP_SKIP[ssh]="すでに適用済みです。"
    fi
  fi
}

apply_ssh() {
  local content
  content="$(ssh_dropin_content)"
  # 先に確定待ちの記録と自動復元の時計を用意してから、設定を書く（途中で止まっても、時計が戻す）。
  arm_for ssh || return 1
  ensure_dir ssh "${SSHD_DROPIN%/*}" 755 || return 1
  put_file ssh "${SSHD_DROPIN}" 644 "${content}" || return 1
}

# 接続元 $1 から TARGET_USER が入るときの sshd の設定が、鍵だけ（パスワードなし・鍵あり）になっているか。
# なっていなければ、理由を PROBE_WHY に入れて 1。
ssh_probe_one() {
  local addr="$1" out value key want
  if ! out="$(sshd_effective "${addr}")"; then
    PROBE_WHY="sshd -T が失敗しました（接続元 ${addr}）: ${out}"
    return 1
  fi
  for key in passwordauthentication kbdinteractiveauthentication pubkeyauthentication; do
    want=no
    [ "${key}" != pubkeyauthentication ] || want=yes
    value="$(config_value "${out}" "${key}" || true)"
    if [ "${value}" != "${want}" ]; then
      PROBE_WHY="接続元 ${addr} からの設定が ${key} ${value:-（なし）} のままです（Match ブロックや、設定の読み込み順が原因かもしれません）。"
      return 1
    fi
  done
}

# 4 つの例の接続元と、いま SSH でつながっている相手（その人の PC に効く Match Address を見逃さないため）、
# 防火壁で SSH を許す各ネットワークの代表のアドレスで、sshd の設定を確かめる。
ssh_probe_all() {
  local addr net seen=" "
  PROBE_WHY=""
  detect_ssh_ports || true
  collect_ssh_peers || true
  for addr in 203.0.113.1 10.0.0.1 192.168.1.1 172.16.0.1 "${CUR_PEERS[@]}"; do
    [[ "${seen}" != *" ${addr} "* ]] || continue
    seen+="${addr} "
    ssh_probe_one "${addr}" || return 1
  done
  for net in "${FW_SSH_V4[@]}" "${FW_SSH_V6[@]}"; do
    addr="$(cidr_tool first "${net}" 2> /dev/null || true)"
    [ -n "${addr}" ] || continue
    [[ "${seen}" != *" ${addr} "* ]] || continue
    seen+="${addr} "
    ssh_probe_one "${addr}" || return 1
  done
}

verify_ssh() {
  local out
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "確認: sshd -t と、4 つの接続元（203.0.113.1・10.0.0.1・192.168.1.1・172.16.0.1）、いま SSH でつながっている相手、防火壁で許すネットワークでの sshd -T（passwordauthentication no など）"
    plan "実行: systemctl reload ssh（いまの接続は切れません）"
    return 0
  fi
  if ! out="$(sshd -t 2>&1)"; then
    warn "sshd -t が失敗しました: ${out}"
    return 1
  fi
  if ! ssh_probe_all; then
    warn "${PROBE_WHY}"
    return 1
  fi
  if unit_active ssh.service; then
    systemctl reload ssh || { warn "systemctl reload ssh が失敗しました。"; return 1; }
  else
    say "ssh.service は止まっていて ssh.socket が待ち受けています。次の接続から新しい設定で動きます。"
  fi
  # 「この変更のあとに開いた画面」を見分けるため、有効になった時刻で記録し直す。
  pending_set ssh "$(pending_window)" || return 1
}

# 設定のファイルを戻したあと、sshd が読み直す（sshd -t が通ったときだけ）。
ssh_reload_after_restore() {
  local out
  if command -v sshd > /dev/null 2>&1 && unit_active ssh.service; then
    if out="$(sshd -t 2>&1)"; then
      systemctl reload ssh || { warn "systemctl reload ssh が失敗しました。"; return 1; }
    else
      warn "元の設定に戻したあとの sshd -t が失敗しました（reload はしません）: ${out}"
      return 1
    fi
  fi
}

rollback_ssh() {
  local rc=0
  restore_step_files ssh || rc=1
  ssh_reload_after_restore || rc=1
  return "${rc}"
}

# 確定した設定（confirmed/ssh）へ戻す。
confirmed_restore_ssh() {
  restore_confirmed_files ssh || return 1
  ssh_reload_after_restore
}

# ---------------------------------------------------------------------------
# 手順 firewall: 入ってくる通信を SSH（管理用の網から）・mDNS・DHCP・ICMP だけにする
# ---------------------------------------------------------------------------

FW_SSH_V4=()
FW_SSH_V6=()
FW_LOCAL_V4=()
FW_RULESET=""
FW_INSTALL_NFT=0
OWN_V4=()
OWN_V6=()

# SSH を開けるポートの書き方（1 つなら 22、複数なら { 22, 2222 }）。
fw_dport_expr() {
  if [ "${#SSH_PORTS[@]}" -le 1 ]; then
    printf '%s' "${SSH_PORTS[0]:-22}"
  else
    printf '{ %s }' "$(join_comma "${SSH_PORTS[@]}")"
  fi
}

build_ruleset() {
  local dport
  dport="$(fw_dport_expr)"
  printf '%s\n' \
    "table ${FW_FAMILY} ${FW_TABLE_NAME}" \
    "delete table ${FW_FAMILY} ${FW_TABLE_NAME}" \
    "table ${FW_FAMILY} ${FW_TABLE_NAME} {"
  if [ "${#FW_SSH_V4[@]}" -gt 0 ]; then
    printf '  set ssh_v4 { type ipv4_addr; flags interval; elements = { %s } }\n' "$(join_comma "${FW_SSH_V4[@]}")"
  fi
  if [ "${#FW_SSH_V6[@]}" -gt 0 ]; then
    printf '  set ssh_v6 { type ipv6_addr; flags interval; elements = { %s } }\n' "$(join_comma "${FW_SSH_V6[@]}")"
  fi
  if [ "${#FW_LOCAL_V4[@]}" -gt 0 ]; then
    printf '  set local_v4 { type ipv4_addr; flags interval; elements = { %s } }\n' "$(join_comma "${FW_LOCAL_V4[@]}")"
  fi
  printf '%s\n' \
    '  chain input {' \
    '    type filter hook input priority filter; policy drop;' \
    '    iif "lo" accept' \
    '    ct state established,related accept' \
    '    ct state invalid drop' \
    '    meta l4proto icmp accept' \
    '    meta l4proto ipv6-icmp accept' \
    '    meta l4proto igmp accept' \
    '    udp sport 67 udp dport 68 accept' \
    '    ip6 saddr fe80::/10 udp sport 547 udp dport 546 accept' \
    '    udp dport 5353 ip daddr 224.0.0.251 accept' \
    '    udp dport 5353 ip6 daddr ff02::fb accept'
  if [ "${#FW_LOCAL_V4[@]}" -gt 0 ]; then
    printf '%s\n' '    udp dport 5353 ip saddr @local_v4 accept'
  fi
  if [ "${#FW_SSH_V4[@]}" -gt 0 ]; then
    printf '    tcp dport %s ip saddr @ssh_v4 accept\n' "${dport}"
  fi
  if [ "${#FW_SSH_V6[@]}" -gt 0 ]; then
    printf '    tcp dport %s ip6 saddr @ssh_v6 accept\n' "${dport}"
  fi
  printf '%s\n' \
    '    fib daddr type { broadcast, multicast } drop' \
    '    limit rate 6/minute burst 10 packets log prefix "campus-chime-fw drop: " level info' \
    '  }' \
    '  chain forward { type filter hook forward priority filter; policy drop; }' \
    '}'
}

# firewall を永続化するユニット。Debian の nftables.service と同じ作りで、After=nftables.service により、
# あちらの flush ruleset でこの表が消されないようにする。
fw_unit_content() {
  printf '%s\n' \
    '[Unit]' \
    'Description=campus-chime の防火壁（scripts/harden.sh firewall）' \
    "Documentation=file:${FW_NFT_FILE}" \
    'DefaultDependencies=no' \
    'Wants=network-pre.target' \
    'Before=network-pre.target shutdown.target' \
    'Conflicts=shutdown.target' \
    'After=nftables.service' \
    '' \
    '[Service]' \
    'Type=oneshot' \
    'RemainAfterExit=yes' \
    "ExecStart=/usr/sbin/nft -f ${FW_NFT_FILE}" \
    "ExecStop=/usr/sbin/nft delete table ${FW_FAMILY} ${FW_TABLE_NAME}" \
    '' \
    '[Install]' \
    'WantedBy=sysinit.target'
}

# この Pi の IPv4 / IPv6 のグローバルアドレスから、属するネットワークを OWN_V4 / OWN_V6 に集める
# （IPv6 は /64）。広すぎて使えないものは OWN_BAD に入れる。
fw_collect_own() {
  local out family addr net
  OWN_V4=()
  OWN_V6=()
  OWN_BAD=()
  command -v ip > /dev/null 2>&1 || return 0
  out="$(ip -4 -o addr show scope global 2>/dev/null || true)"
  while read -r _ _ family addr _; do
    [ "${family}" = inet ] || continue
    if net="$(cidr_tool norm "${addr}" 2>/dev/null)"; then
      OWN_V4+=("${net}")
    else
      OWN_BAD+=("${addr}")
    fi
  done <<< "${out}"
  out="$(ip -6 -o addr show scope global 2>/dev/null || true)"
  while read -r _ _ family addr _; do
    [ "${family}" = inet6 ] || continue
    if net="$(cidr_tool norm "${addr%/*}/64" 2>/dev/null)"; then
      OWN_V6+=("${net}")
    fi
  done <<< "${out}"
}

# --ssh-from の値を検証して FW_SSH_V4 / FW_SSH_V6 に入れる。
fw_parse_ssh_from() {
  local raw net rc
  for raw in "${SSH_FROM[@]}"; do
    rc=0
    net="$(cidr_tool norm "${raw}" 2>/dev/null)" || rc=$?
    case "${rc}" in
      0)
        case "${net}" in
          *:*) FW_SSH_V6+=("${net}") ;;
          *) FW_SSH_V4+=("${net}") ;;
        esac
        ;;
      2) refuse "--ssh-from ${raw} は広すぎます（IPv4 は /8、IPv6 は /32 より狭く指定してください。全世界を許すと防火壁の意味がありません）。" ;;
      *) refuse "--ssh-from ${raw} は CIDR（例: 192.168.1.0/24）として読めません。" ;;
    esac
  done
}

# いま SSH でつながっている相手（ss の peer 列。sshd の全部のポートについて）が、許す範囲に入っているか。
# 入っていないと、防火壁を入れた瞬間に切れる。--force は、自分自身が SSH でつないでいるとき
# （この画面が sshd の下にあるとき）は効かない（自分の接続を切る確認を飛ばさせないため）。
fw_check_ssh_peers() {
  local allowed uncovered example peer
  if ! collect_ssh_peers; then
    fw_peer_check_unavailable "${CUR_PEERS_WHY}"
    return 0
  fi
  [ "${#CUR_PEERS[@]}" -gt 0 ] || return 0
  allowed="$(join_comma "${FW_SSH_V4[@]}" "${FW_SSH_V6[@]}")"
  allowed="${allowed// /}"
  uncovered="$(cidr_tool uncovered "${allowed}" "${CUR_PEERS[@]}")" || { refuse "接続元の判定に失敗しました。"; return 0; }
  while IFS= read -r peer; do
    [ -n "${peer}" ] || continue
    example="$(cidr_tool net24 "${peer}" 2>/dev/null || printf '%s' "${peer}")"
    if [ "${FORCE}" -eq 1 ] && ! operator_on_ssh; then
      warn "いま SSH でつながっている ${peer} は、許す範囲に入っていません。--force が付いているので進めます（この接続は切れます）。"
    elif [ "${FORCE}" -eq 1 ]; then
      refuse "いま SSH でつながっている ${peer} は、許す範囲に入っていません。この画面も SSH でつないでいるので、このまま防火壁を入れるとこの接続が切れます（--force でも止めます）。--ssh-from ${example} を付けて、もう一度実行してください。"
    else
      refuse "いま SSH でつながっている ${peer} は、許す範囲に入っていません。このまま防火壁を入れると、この接続が切れます。--ssh-from ${example} を付けて、もう一度実行してください。"
    fi
  done <<< "${uncovered}"
}

fw_peer_check_unavailable() {
  if [ "${FORCE}" -eq 1 ]; then
    warn "いまの SSH の接続元を調べられません（$1）。--force が付いているので進めます。"
  else
    refuse "いまの SSH の接続元を調べられません（$1）。切れる接続があるかを確かめられないので止めます。承知のうえなら --force を付けてください。"
  fi
}

# 別の表の表示 $1 に、入力を実際に止める・選り分ける base chain があるか。policy accept で規則が 1 つも
# ない input の chain（Debian の標準の /etc/nftables.conf の空の骨組み）は、何も止めないので数えない。
fw_input_chain_active() {
  local line trimmed in_chain=0 policy=accept rules=0
  while IFS= read -r line; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    if [ "${in_chain}" -eq 0 ]; then
      if [[ "${trimmed}" == *"hook input"* ]]; then
        in_chain=1
        rules=0
        policy=accept
        if [[ "${trimmed}" =~ policy[[:space:]]+([a-z]+) ]]; then
          policy="${BASH_REMATCH[1]}"
        fi
      fi
    else
      case "${trimmed}" in
        "}"*)
          in_chain=0
          if [ "${policy}" != accept ] || [ "${rules}" -gt 0 ]; then
            return 0
          fi
          ;;
        "") ;;
        *) rules=$((rules + 1)) ;;
      esac
    fi
  done <<< "$1"
  # 閉じていない（読み切れない）chain は、安全のため「止める」ものとして扱う。
  [ "${in_chain}" -eq 0 ] || return 0
  return 1
}

# 同じ入力に別の防火壁（input フックの base chain に規則か drop の policy を持つ別の表）があると、
# 重なって予想外の動きになる。
fw_check_other_tables() {
  local tables family name body other_tables="" harmless=""
  if ! tables="$(nft list tables 2>&1)"; then
    refuse "nft list tables が失敗しました: ${tables}"
    return 0
  fi
  while read -r _ family name; do
    [ -n "${name}" ] || continue
    if [ "${family}" = "${FW_FAMILY}" ] && [ "${name}" = "${FW_TABLE_NAME}" ]; then
      continue
    fi
    body="$(nft list table "${family}" "${name}" 2>/dev/null || true)"
    if [[ "${body}" == *"hook input"* ]]; then
      if fw_input_chain_active "${body}"; then
        other_tables+=" ${family}/${name}"
      else
        harmless+=" ${family}/${name}"
      fi
    fi
  done <<< "${tables}"
  if [ -n "${harmless}" ]; then
    say "  ${harmless# } は入力の chain が policy accept で規則が無い空の表（Debian の標準の骨組み）なので、何も止めません。そのまま進めます。"
  fi
  [ -n "${other_tables}" ] || return 0
  say "  いまの nft の表:" >&2
  printf '%s\n' "${tables}" | sed 's/^/    /' >&2
  if [ "${FORCE}" -eq 1 ]; then
    warn "別の防火壁（${other_tables# }）が入力を見ています。--force が付いているので、重ねて入れます。"
  else
    refuse "別の防火壁（${other_tables# }）がすでに入力を見ています。重ねると予想外の動きになるので止めます。承知のうえなら --force を付けてください。"
  fi
}

preflight_firewall() {
  local default_sources=0 out
  log "確認: firewall"
  FW_SSH_V4=()
  FW_SSH_V6=()
  FW_LOCAL_V4=()
  FW_INSTALL_NFT=0
  if ! command -v nft > /dev/null 2>&1; then
    FW_INSTALL_NFT=1
    say "  nft が入っていません。適用のとき、apt-get install -y nftables で入れます（入れたあとも残ります）。"
  elif [ "${IS_ROOT}" -eq 1 ]; then
    fw_check_other_tables
  else
    say "  （root でないので、ほかの防火壁の有無は確かめられません）"
  fi

  fw_collect_own
  if [ "${#SSH_FROM[@]}" -gt 0 ]; then
    fw_parse_ssh_from
  else
    default_sources=1
    if [ "${#OWN_BAD[@]}" -gt 0 ]; then
      refuse "この Pi のネットワーク（${OWN_BAD[*]}）が広すぎて、SSH を許す範囲にできません。--ssh-from で管理用のネットワークを指定してください。"
    fi
    if [ "${#OWN_V4[@]}" -eq 0 ]; then
      refuse "この Pi の IPv4 のネットワークを見つけられません。--ssh-from で、SSH を許す管理用のネットワーク（例: --ssh-from 192.168.1.0/24）を指定してください。"
    fi
    FW_SSH_V4=("${OWN_V4[@]}")
    FW_SSH_V6=(fe80::/10 "${OWN_V6[@]}")
  fi
  FW_LOCAL_V4=("${OWN_V4[@]}")
  if [ "${#FW_SSH_V4[@]}" -gt 0 ]; then
    mapfile -t FW_SSH_V4 < <(cidr_tool collapse "${FW_SSH_V4[@]}")
  fi
  if [ "${#FW_SSH_V6[@]}" -gt 0 ]; then
    mapfile -t FW_SSH_V6 < <(cidr_tool collapse "${FW_SSH_V6[@]}")
  fi
  if [ "${#FW_LOCAL_V4[@]}" -gt 0 ]; then
    mapfile -t FW_LOCAL_V4 < <(cidr_tool collapse "${FW_LOCAL_V4[@]}")
  fi
  if [ "${#FW_SSH_V4[@]}" -eq 0 ] && [ "${#FW_SSH_V6[@]}" -eq 0 ]; then
    refuse "SSH を許す相手が 1 つもありません。--ssh-from で指定してください。"
    return 0
  fi
  if [ "${default_sources}" -eq 1 ]; then
    say "  SSH を許す範囲: この Pi と同じネットワーク（${FW_SSH_V4[*]}）と、IPv6 のリンクローカル（${FW_SSH_V6[*]}）。別の網から入るなら --ssh-from を付けます。"
  else
    say "  SSH を許す範囲（--ssh-from）: ${FW_SSH_V4[*]} ${FW_SSH_V6[*]}"
  fi
  # 開けるポートは、sshd が実際に待ち受けているもの（22 とは限らない）。
  if ! detect_ssh_ports; then
    SSH_PORTS=(22)
    if [ "${FORCE}" -eq 1 ]; then
      warn "sshd が待ち受けるポートを調べられません（sshd -T / ssh.socket）。--force が付いているので、22 だけを開けます。"
    else
      refuse "sshd が待ち受けるポートを調べられません（sshd -T / ssh.socket）。開けるポートが分からないまま防火壁を入れると、新しい SSH 接続が切れるので止めます。承知のうえなら --force を付けてください（22 だけを開けます）。"
    fi
  elif [ -n "${SSH_PORTS_NOTE}" ]; then
    say "  ${SSH_PORTS_NOTE}"
  fi
  say "  SSH を開けるポート: ${SSH_PORTS[*]}"
  fw_check_ssh_peers

  FW_RULESET="$(build_ruleset)"
  if [ "${FW_INSTALL_NFT}" -eq 0 ] && [ "${IS_ROOT}" -eq 1 ]; then
    if ! out="$(printf '%s\n' "${FW_RULESET}" | nft -c -f - 2>&1)"; then
      refuse "作ったルールを nft が受け付けません（nft -c）: ${out}"
    fi
  fi
  if [ "${PRE_FAIL}" -eq 0 ] && step_done firewall && nft list table "${FW_FAMILY}" "${FW_TABLE_NAME}" > /dev/null 2>&1; then
    if pending_has firewall; then
      if file_is "${STATE_REAL}/firewall.nft" 600 "${FW_RULESET}"; then
        STEP_SKIP[firewall]="適用済みですが、まだ確定していません。別の端末で確かめて、$(harden_cmd) --confirm を実行してください。"
      fi
    elif [ "$(meta_get "${STATE_DIR}/applied/firewall" persisted 2>/dev/null || true)" = 1 ] \
      && file_is "${FW_NFT_FILE}" 644 "${FW_RULESET}" && file_is "${FW_UNIT}" 644 "$(fw_unit_content)"; then
      # 再起動のあとも防火壁が入るには、永続化のユニットが有効でなければならない。
      if [ "$(unit_enabled_state campus-chime-firewall.service)" = enabled ]; then
        STEP_SKIP[firewall]="すでに適用済みです。"
      else
        warn "防火壁の設定は適用済みと同じですが、campus-chime-firewall.service が有効になっていません（再起動すると防火壁が消えます）。もう一度適用します。"
      fi
    fi
  fi
}

apply_firewall() {
  local work="${STATE_DIR}/firewall.nft"
  if [ "${FW_INSTALL_NFT}" -eq 1 ]; then
    ensure_idle "${LOOKAHEAD_MINUTES}" || return 1
    # 入れる前に記録する（入れた直後に止められても、入れたと分かるように）。
    if [ "${DRY_RUN}" -eq 0 ]; then
      record_first firewall installed_nft 1 || return 1
    fi
    apt_refresh
    sys apt-get install -y nftables || { warn "nftables を入れられませんでした。"; apt_install_hint; return 1; }
  fi
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan_file "${FW_NFT_FILE}（確定したときに書く。それまでは ${STATE_REAL}/firewall.nft）" 644 "${FW_RULESET}"
    plan "実行: nft -c -f ${STATE_REAL}/firewall.nft（文法の確認だけ）"
    arm_for firewall
    plan "実行: nft -f ${STATE_REAL}/firewall.nft（この表だけを入れ替える。確定するまで再起動すると消える）"
    return 0
  fi
  write_atomic "${work}" 600 "${FW_RULESET}" || { warn "ルールの下書きを書けません: ${work}"; return 1; }
  # 先に確定待ちの記録と自動復元の時計を用意してから、ルールを読み込む。
  arm_for firewall || return 1
  nft -c -f "${work}" || { warn "nft がルールを受け付けません。"; return 1; }
  nft -f "${work}" || { warn "ルールを読み込めませんでした。"; return 1; }
}

verify_firewall() {
  local out
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "確認: nft list table ${FW_FAMILY} ${FW_TABLE_NAME} に hook input と policy drop があること"
    return 0
  fi
  if ! out="$(nft list table "${FW_FAMILY}" "${FW_TABLE_NAME}" 2>&1)"; then
    warn "防火壁の表が見つかりません: ${out}"
    return 1
  fi
  if [[ "${out}" != *"hook input"* ]] || [[ "${out}" != *"policy drop"* ]]; then
    warn "防火壁の表に、入力の drop が見つかりません。"
    return 1
  fi
  meta_set "${STATE_DIR}/applied/firewall" boot_id "$(cat "${PROC_DIR}/sys/kernel/random/boot_id" 2>/dev/null || true)" || return 1
  pending_set firewall "$(pending_window)" || return 1
}

# --confirm で、確定した防火壁を再起動後も残す。この起動で本当に読み込んだ防火壁だけを残す。
persist_firewall() {
  local work="${STATE_DIR}/firewall.nft" boot_now boot_then
  if ! nft list table "${FW_FAMILY}" "${FW_TABLE_NAME}" > /dev/null 2>&1; then
    warn "防火壁の表がいま読み込まれていません。"
    return 2
  fi
  # 適用が最後まで終わっていない（途中で止められた）と、下書きは、読み込んで確かめたルールとは限らない。
  if ! step_done firewall; then
    warn "防火壁の適用が最後まで終わっていません（途中で止められたようです）。いま読み込まれている表と下書きが同じとは限らないので、確定しません。"
    return 2
  fi
  boot_now="$(cat "${PROC_DIR}/sys/kernel/random/boot_id" 2>/dev/null || true)"
  boot_then="$(meta_get "${STATE_DIR}/applied/firewall" boot_id 2>/dev/null || true)"
  if [ -z "${boot_now}" ] || [ "${boot_now}" != "${boot_then}" ]; then
    warn "防火壁を入れてから、この Pi が再起動しています。いま読み込まれている表は、確かめた表ではありません。"
    return 2
  fi
  [ -f "${work}" ] || { warn "ルールの下書き（${work}）がありません。"; return 1; }
  ensure_dir firewall "${FW_CONF_DIR}" 755 || return 1
  put_file firewall "${FW_NFT_FILE}" 644 "$(cat "${work}")" || return 1
  put_file firewall "${FW_UNIT}" 644 "$(fw_unit_content)" || return 1
  systemctl daemon-reload || return 1
  systemctl enable campus-chime-firewall.service || return 1
  meta_set "${STATE_DIR}/applied/firewall" persisted 1 || return 1
}

# 確定した防火壁（confirmed/firewall）へ戻す: ファイルとユニットを確定した版にして、その表を読み込み直す
# （この版は確定済みなので、下書きもこれにそろえる）。
confirmed_restore_firewall() {
  local out file="${SYSROOT}${FW_NFT_FILE}"
  restore_confirmed_files firewall || return 1
  systemctl daemon-reload || return 1
  systemctl enable campus-chime-firewall.service || return 1
  nft -c -f "${file}" || { warn "確定した防火壁のルールを nft が受け付けません。"; return 1; }
  nft -f "${file}" || { warn "確定した防火壁を読み込み直せませんでした。"; return 1; }
  if ! out="$(nft list table "${FW_FAMILY}" "${FW_TABLE_NAME}" 2>&1)"; then
    warn "確定した防火壁を読み込んだのに、表が見つかりません: ${out}"
    return 1
  fi
  if [[ "${out}" != *"hook input"* ]] || [[ "${out}" != *"policy drop"* ]]; then
    warn "確定した防火壁の表に、入力の drop が見つかりません。"
    return 1
  fi
  write_atomic "${STATE_DIR}/firewall.nft" 600 "$(cat "${file}")" || return 1
}

rollback_firewall() {
  local rc=0 out unit_existed=0
  if command -v nft > /dev/null 2>&1; then
    if ! out="$(nft delete table "${FW_FAMILY}" "${FW_TABLE_NAME}" 2>&1)"; then
      # 「表が無い」だけは正常（すでに消えている）。それ以外のエラーは失敗として扱う。
      if [[ "${out}" != *"No such file or directory"* ]]; then
        warn "防火壁の表を消せませんでした: ${out}"
        rc=1
      fi
    fi
    if out="$(nft list table "${FW_FAMILY}" "${FW_TABLE_NAME}" 2>&1)"; then
      warn "防火壁の表がまだ残っています。"
      rc=1
    elif [[ "${out}" != *"No such file or directory"* ]]; then
      warn "防火壁の表が消えたことを確かめられません: ${out}"
      rc=1
    fi
  fi
  if [ -e "${SYSROOT}${FW_UNIT}" ]; then
    unit_existed=1
    systemctl disable campus-chime-firewall.service || { warn "campus-chime-firewall.service を無効にできませんでした。"; rc=1; }
  fi
  restore_step_files firewall || rc=1
  rm -f "${STATE_DIR}/firewall.nft" || rc=1
  if [ "${unit_existed}" -eq 1 ]; then
    systemctl daemon-reload || rc=1
  fi
  if [ "$(meta_get "${STATE_DIR}/applied/firewall" installed_nft 2>/dev/null || true)" = 1 ]; then
    say "nftables は入れたままです（削除しません）。"
  fi
  return "${rc}"
}

# ---------------------------------------------------------------------------
# 手順 sudo: sudo にパスワードを求める
# ---------------------------------------------------------------------------

SUDO_FILES=()

# sudoers の 1 行を、空白をそろえて比べやすくする。
normalize_rule() {
  local s="$1"
  s="${s//$'\t'/ }"
  while [[ "${s}" == *"  "* ]]; do
    s="${s//  / }"
  done
  s="${s# }"
  s="${s% }"
  s="${s// = /=}"
  s="${s//= /=}"
  s="${s// =/=}"
  s="${s// : /:}"
  s="${s//: /:}"
  s="${s// :/:}"
  s="${s//( /(}"
  s="${s// )/)}"
  printf '%s' "${s}"
}

# sudoers のファイル $1 の、有効な行（空行とコメント以外。#include は有効）を 1 行ずつ表示する。
sudoers_effective_lines() {
  local line trimmed
  while IFS= read -r line || [ -n "${line}" ]; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    [ -n "${trimmed}" ] || continue
    case "${trimmed}" in
      "#include"*) ;;
      "#"*) continue ;;
    esac
    printf '%s\n' "${trimmed}"
  done < "$1"
}

preflight_sudo() {
  local sudo_out passwd_out status f name line lines rule_re groups_out file_ok has_password_rule=0 found=()
  local password_rule_re='^[[:space:]]*\(ALL[[:space:]]*:[[:space:]]*ALL\)[[:space:]]+ALL[[:space:]]*$'
  log "確認: sudo"
  SUDO_FILES=()
  if [ "${IS_ROOT}" -eq 0 ]; then
    say "  （root でないので、sudoers を読めません。実際の確認は sudo を付けて行います）"
    return 0
  fi
  if [ "${DRY_RUN}" -eq 0 ] && [ ! -t 0 ]; then
    refuse "標準入力が端末ではありません。パスワードの確認を端末で行うので、SSH や画面の端末から直接実行してください。"
  fi
  groups_out="$(id -nG "${TARGET_USER}" 2>/dev/null || true)"
  case " ${groups_out} " in
    *" sudo "*) ;;
    *) refuse "${TARGET_USER} が sudo グループに入っていません（groups: ${groups_out:-不明}）。パスワードを求めても、sudo を使えなくなります。" ;;
  esac
  # NOPASSWD を外したあとに残る、パスワードを求める規則（/etc/sudoers の %sudo）があるか。
  sudo_out="$(sudo -l -U "${TARGET_USER}" 2>&1 || true)"
  while IFS= read -r line; do
    if [[ "${line}" =~ ${password_rule_re} ]]; then
      has_password_rule=1
    fi
  done <<< "${sudo_out}"
  if [ "${has_password_rule}" -eq 0 ]; then
    refuse "パスワードを求める (ALL : ALL) ALL の規則（/etc/sudoers の %sudo）が見つかりません。NOPASSWD を外すと sudo を使えなくなるので止めます（sudo -l -U ${TARGET_USER} の結果: ${sudo_out//$'\n'/ / }）。"
  fi
  passwd_out="$(passwd -S "${TARGET_USER}" 2>/dev/null || true)"
  read -r _ status _ <<< "${passwd_out}"
  if [ "${status:-}" != P ]; then
    refuse "${TARGET_USER} にパスワードがありません（passwd -S: ${passwd_out:-不明}）。先に sudo passwd ${TARGET_USER} でパスワードを決めてください。"
  fi
  if ! visudo -c > /dev/null 2>&1; then
    refuse "visudo -c が失敗します。sudoers にすでに誤りがあるので、触りません。"
  fi

  # /etc/sudoers の NOPASSWD は、このスクリプトでは外せない。
  if [ -f "${SYSROOT}/etc/sudoers" ]; then
    lines="$(sudoers_effective_lines "${SYSROOT}/etc/sudoers")"
    while IFS= read -r line; do
      if [[ "${line}" == *NOPASSWD* ]]; then
        refuse "/etc/sudoers に NOPASSWD の規則があります（${line}）。このスクリプトは /etc/sudoers.d/ の中の専用ファイルだけを外します。手で直してください。"
      fi
    done <<< "${lines}"
  fi
  # /etc/sudoers.d/ のファイルのうち、NOPASSWD を含むものは、この利用者の 1 行だけでなければならない。
  rule_re="^${TARGET_USER} ALL=\\(ALL(:ALL)?\\) ?NOPASSWD:ALL\$"
  for f in "${SYSROOT}/etc/sudoers.d/"*; do
    [ -f "${f}" ] || continue
    name="${f##*/}"
    case "${name}" in
      *.* | *~) continue ;;
    esac
    lines="$(sudoers_effective_lines "${f}")"
    [[ "${lines}" == *NOPASSWD* ]] || continue
    file_ok=1
    while IFS= read -r line; do
      [ -n "${line}" ] || continue
      if ! [[ "$(normalize_rule "${line}")" =~ ${rule_re} ]]; then
        refuse "/etc/sudoers.d/${name} に、${TARGET_USER} だけに NOPASSWD を与える 1 行以外の内容があります（${line}）。外すと他の設定も消えてしまうので、手で直してください。"
        file_ok=0
        break
      fi
    done <<< "${lines}"
    if [ "${file_ok}" -eq 1 ]; then
      found+=("/etc/sudoers.d/${name}")
    fi
  done
  SUDO_FILES=("${found[@]}")
  if [ "${#SUDO_FILES[@]}" -eq 0 ] && [ "${PRE_FAIL}" -eq 0 ]; then
    STEP_SKIP[sudo]="すでにパスワードが必要です（NOPASSWD の専用ファイルはありません）。"
    return 0
  fi
  say "  外す対象: ${SUDO_FILES[*]:-（なし）}"
  say "  ${TARGET_USER} のパスワードを知っていることを確かめてください（知らないと、外したあと sudo を使えません）。"
  say "  外している間は ${SUDO_WINDOW} 分の安全網（確定待ち）を張ります。パスワードを確かめられなかったとき（Ctrl-C・回線切れ・強制終了を含む）は、元に戻します。"
}

apply_sudo() {
  local f
  if [ "${DRY_RUN}" -eq 1 ] && [ "${IS_ROOT}" -eq 0 ]; then
    plan "root で、/etc/sudoers.d/ の中の ${TARGET_USER} 専用の NOPASSWD ファイルを、控えを取ってから外す"
  fi
  # 先に安全網を張る: 確定待ち（${SUDO_WINDOW} 分）と自動復元の時計。外したあとにプロセスが消えても
  # （kill -9・電源断・回線切れ）、時計が NOPASSWD のファイルを戻す。
  arm_for sudo "${SUDO_WINDOW}" || return 1
  for f in "${SUDO_FILES[@]}"; do
    del_file sudo "${f}" || return 1
  done
  if [ "${DRY_RUN}" -eq 0 ]; then
    visudo -c > /dev/null || { warn "ファイルを外したあとの visudo -c が失敗しました。"; return 1; }
  fi
}

verify_sudo() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "確認: runuser -u ${TARGET_USER} -- sudo -k、sudo -n -v（パスワードが要ること）、sudo -v（パスワードを入力）"
    return 0
  fi
  say "sudo がパスワードを求めることを確かめます。${TARGET_USER} のパスワードを入力してください（${SUDO_WINDOW} 分以内。入力しない・間違える・Ctrl-C のときは、NOPASSWD の設定を元に戻します）。"
  runuser -u "${TARGET_USER}" -- sudo -k || true
  if runuser -u "${TARGET_USER}" -- sudo -n -v > /dev/null 2>&1; then
    warn "パスワードなしで sudo が通ってしまいます（NOPASSWD の規則がほかにも残っています）。"
    return 1
  fi
  if ! runuser -u "${TARGET_USER}" -- sudo -v; then
    warn "パスワードで sudo を確かめられませんでした。"
    return 1
  fi
  runuser -u "${TARGET_USER}" -- sudo -k || true
  # パスワードを確かめられた。安全網（sudo の確定待ち）だけを外す。ssh・firewall の確定待ちと時計は
  # そのまま残す（確定待ちが空になったときだけ、時計を片づける）。
  pending_remove sudo || { warn "sudo の確定待ちの記録を消せません。"; return 1; }
  settle_revert || true
}

rollback_sudo() {
  local rc=0
  restore_step_files sudo || rc=1
  if ! visudo -c > /dev/null; then
    warn "visudo -c が失敗しました。/etc/sudoers.d/ の中を確かめてください。"
    rc=1
  fi
  return "${rc}"
}

# ---------------------------------------------------------------------------
# 手順 services: 使わない常駐サービスを止めて無効にする
# ---------------------------------------------------------------------------

SVC_PRESENT=()
declare -A SVC_ENABLED=()
declare -A SVC_ACTIVE=()

# Bluetooth のスピーカーで放送を鳴らしているなら、bluetooth を止めると放送が鳴らなくなる（いちばん避けたいこと）。
# ペアリング済みの機器と、ALSA の設定の bluealsa / bluez を探して、あれば止める（--force で進める）。
services_check_bluetooth_audio() {
  local dir name conf bt_signs=""
  for dir in "${SYSROOT}/var/lib/bluetooth/"*/*/; do
    name="${dir%/}"
    name="${name##*/}"
    if [[ "${name}" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
      bt_signs+=" ペアリング済みの機器 ${name}"
    fi
  done
  for conf in /etc/asound.conf "${USER_HOME}/.asoundrc"; do
    if [ -f "${SYSROOT}${conf}" ] && grep -Eiq 'bluealsa|bluez' "${SYSROOT}${conf}"; then
      bt_signs+=" ${conf} の bluealsa / bluez の設定"
    fi
  done
  [ -n "${bt_signs}" ] || return 0
  if [ "${FORCE}" -eq 1 ]; then
    warn "Bluetooth のスピーカーを使っているかもしれません（${bt_signs# }）。--force が付いているので止めます。放送が鳴らなくなったら --rollback services で戻せます。"
  else
    refuse "Bluetooth のスピーカーで放送を鳴らしているかもしれません（${bt_signs# }）。bluetooth を止めると放送が鳴らなくなります。使っていないと確かなら --force を付けてください。"
  fi
}

preflight_services() {
  local unit all_done=1
  log "確認: services"
  SVC_PRESENT=()
  for unit in "${SERVICES_UNITS[@]}"; do
    if unit_exists "${unit}"; then
      SVC_PRESENT+=("${unit}")
      SVC_ENABLED["${unit}"]="$(unit_enabled_state "${unit}")"
      SVC_ACTIVE["${unit}"]="$(unit_state "${unit}")"
      if [ -z "${SVC_ENABLED[${unit}]}" ] || [ -z "${SVC_ACTIVE[${unit}]}" ]; then
        # 元の状態を調べられないまま止めると、戻すときに元どおりにできない。
        refuse "${unit} の状態を調べられません（systemctl is-enabled / is-active が答えませんでした）。元の状態を控えられないので止めます。もう一度実行してください。"
        continue
      fi
      say "  ${unit}: ${SVC_ENABLED[${unit}]}、${SVC_ACTIVE[${unit}]}"
      if [ "${SVC_ENABLED[${unit}]}" != masked ] || [ "${SVC_ACTIVE[${unit}]}" = active ]; then
        all_done=0
      fi
    else
      say "  ${unit}: 入っていないので、何もしません。"
    fi
  done
  say "  avahi-daemon は止めません（campus-chime.local の名前解決に使うため）。"
  if [ -n "${SVC_ENABLED[bluetooth.service]:-}" ]; then
    services_check_bluetooth_audio
  fi
  if [ "${#SVC_PRESENT[@]}" -eq 0 ]; then
    STEP_SKIP[services]="対象のサービスが入っていません。"
  elif [ "${all_done}" -eq 1 ]; then
    STEP_SKIP[services]="すでに止めて無効（masked）にしてあります。"
  fi
}

apply_services() {
  local unit enabled active
  for unit in "${SVC_PRESENT[@]}"; do
    enabled="${SVC_ENABLED[${unit}]}"
    active="${SVC_ACTIVE[${unit}]}"
    if [ "${enabled}" = masked ] && [ "${active}" != active ]; then
      say "  ${unit}: すでに無効（masked）です。"
      continue
    fi
    if [ "${DRY_RUN}" -eq 0 ]; then
      # 元の状態は最初の 1 回だけ記録する（戻すときに、元どおりに有効・起動に戻すため）。
      record_first services "unit.${unit}.enabled" "${enabled}" || return 1
      record_first services "unit.${unit}.active" "${active}" || return 1
    fi
    sys systemctl disable --now "${unit}" || { warn "${unit} を止められませんでした。"; return 1; }
    sys systemctl mask "${unit}" || { warn "${unit} を mask できませんでした。"; return 1; }
  done
}

verify_services() {
  local unit
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "確認: 各サービスが masked で、止まっていること"
    return 0
  fi
  for unit in "${SVC_PRESENT[@]}"; do
    if [ "$(unit_enabled_state "${unit}")" != masked ] || unit_active "${unit}"; then
      warn "${unit} が止まっていない、または mask されていません。"
      return 1
    fi
  done
}

rollback_services() {
  local rc=0 unit file was_enabled was_active
  file="${STATE_DIR}/applied/services"
  for unit in "${SERVICES_UNITS[@]}"; do
    # 控えがあれば、中身が空でも戻す（unmask だけは必ず行う）。
    meta_get "${file}" "unit.${unit}.enabled" > /dev/null 2>&1 || continue
    was_enabled="$(meta_get "${file}" "unit.${unit}.enabled" 2>/dev/null || true)"
    was_active="$(meta_get "${file}" "unit.${unit}.active" 2>/dev/null || true)"
    systemctl unmask "${unit}" || { warn "${unit} の mask を外せませんでした。"; rc=1; continue; }
    if [ "${was_enabled}" = enabled ]; then
      systemctl enable "${unit}" || { warn "${unit} を有効に戻せませんでした。"; rc=1; }
    fi
    if [ "${was_active}" = active ]; then
      systemctl start "${unit}" || { warn "${unit} を起動できませんでした。"; rc=1; }
    fi
  done
  return "${rc}"
}

# ---------------------------------------------------------------------------
# 手順 journal: ログを再起動後も残す
# ---------------------------------------------------------------------------

journal_dropin_content() {
  printf '%s\n' \
    '# scripts/harden.sh journal が作成（元に戻す: sudo bash scripts/harden.sh --rollback journal）' \
    '[Journal]' \
    'Storage=persistent' \
    'SystemMaxUse=200M'
}

preflight_journal() {
  log "確認: journal"
  if step_done journal && file_is "${JOURNAL_DROPIN}" 644 "$(journal_dropin_content)" && [ -d "${SYSROOT}${JOURNAL_DIR}" ]; then
    STEP_SKIP[journal]="すでに適用済みです。"
  fi
}

apply_journal() {
  ensure_dir journal "${JOURNAL_DROPIN%/*}" 755 || return 1
  put_file journal "${JOURNAL_DROPIN}" 644 "$(journal_dropin_content)" || return 1
  # /var/log/journal を作ったかどうかを記録する（戻すときに、作ったときだけ消すため）。
  ensure_dir journal "${JOURNAL_DIR}" 2755 || return 1
  sys systemd-tmpfiles --create --prefix /var/log/journal || { warn "systemd-tmpfiles が失敗しました。"; return 1; }
  sys systemctl restart systemd-journald || { warn "systemd-journald を再起動できませんでした。"; return 1; }
  sys journalctl --flush || { warn "journalctl --flush が失敗しました。"; return 1; }
}

verify_journal() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "確認: ${JOURNAL_DIR} があり、systemd-journald が動いていること"
    return 0
  fi
  if [ ! -d "${SYSROOT}${JOURNAL_DIR}" ]; then
    warn "${JOURNAL_DIR} がありません。"
    return 1
  fi
  if ! unit_active systemd-journald.service; then
    warn "systemd-journald が動いていません。"
    return 1
  fi
}

rollback_journal() {
  local rc=0
  if created_has journal d "${JOURNAL_DIR}"; then
    warn "${JOURNAL_DIR} はこの手順が作ったので、消します。保存されていたログも消えます。"
    if command -v journalctl > /dev/null 2>&1; then
      journalctl --relinquish-var || { warn "journalctl --relinquish-var が失敗しました。"; rc=1; }
    fi
    rm -rf "${SYSROOT}${JOURNAL_DIR:?}" || rc=1
  fi
  restore_step_files journal || rc=1
  systemctl restart systemd-journald || { warn "systemd-journald を再起動できませんでした。"; rc=1; }
  return "${rc}"
}

# ---------------------------------------------------------------------------
# 手順 updates: セキュリティ更新を自動で入れる（再起動はしない）
# ---------------------------------------------------------------------------

UPD_PATTERNS=()
UPD_INSTALL=0

updates_conf_content() {
  local pattern
  printf '%s\n' \
    '// scripts/harden.sh updates が作成（元に戻す: sudo bash scripts/harden.sh --rollback updates）' \
    'Unattended-Upgrade::Origins-Pattern {'
  for pattern in "${UPD_PATTERNS[@]}"; do
    printf '        "%s";\n' "${pattern}"
  done
  printf '%s\n' \
    '};' \
    'Unattended-Upgrade::Automatic-Reboot "false";'
}

updates_periodic_content() {
  printf '%s\n' \
    '// scripts/harden.sh updates が作成（元に戻す: sudo bash scripts/harden.sh --rollback updates）' \
    'APT::Periodic::Update-Package-Lists "1";' \
    'APT::Periodic::Unattended-Upgrade "1";'
}

# apt の標準のタイマー（06:00 と 18:00 に最大 12 時間のばらつき）だと、一覧の取得が放送の時間帯
# （10〜17 時）に入りうる。早朝に寄せ、止まっていた日は取り戻さない（Persistent=false）。
updates_timer_content() {
  printf '%s\n' \
    '# scripts/harden.sh updates が作成（元に戻す: sudo bash scripts/harden.sh --rollback updates）' \
    '[Timer]' \
    'OnCalendar=' \
    "OnCalendar=*-*-* $1" \
    "RandomizedDelaySec=$2" \
    'Persistent=false'
}

# apt-cache policy の release 行（o=・l=・n=）から、Origins-Pattern の行を作る。n（codename）が
# このシステムの codename なら ${distro_codename}、codename-security / codename-updates なら
# ${distro_codename}-security / -updates と書く（バックポートなど、ほかの n は対象にしない）。
updates_collect_patterns() {
  local policy line rest part o l n expr pattern parts=()
  local -A seen=()
  UPD_PATTERNS=()
  if ! policy="$(apt_read apt-cache policy 2>&1)"; then
    refuse "apt-cache policy が失敗しました: ${policy}"
    return 0
  fi
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "${line}" in
      "release "*) ;;
      *) continue ;;
    esac
    rest="${line#release }"
    o=""
    l=""
    n=""
    IFS=, read -r -a parts <<< "${rest}"
    for part in "${parts[@]}"; do
      case "${part}" in
        o=*) o="${part#o=}" ;;
        l=*) l="${part#l=}" ;;
        n=*) n="${part#n=}" ;;
      esac
    done
    [ -n "${o}" ] || continue
    case "${o}${l}" in
      *'"'* | *"\\"* | *'$'*) continue ;;
    esac
    if [ -n "${OS_CODENAME}" ] && [ "${n}" = "${OS_CODENAME}" ]; then
      expr="\${distro_codename}"
    elif [ -n "${OS_CODENAME}" ] && [ "${n}" = "${OS_CODENAME}-security" ]; then
      expr="\${distro_codename}-security"
    elif [ -n "${OS_CODENAME}" ] && [ "${n}" = "${OS_CODENAME}-updates" ]; then
      expr="\${distro_codename}-updates"
    else
      continue
    fi
    pattern="origin=${o},codename=${expr}"
    if [ -n "${l}" ]; then
      pattern+=",label=${l}"
    fi
    if [ -z "${seen["${pattern}"]:-}" ]; then
      seen["${pattern}"]=1
      UPD_PATTERNS+=("${pattern}")
    fi
  done <<< "${policy}"
}

# apt のタイマーは OS の時間帯で動く（OnCalendar）。放送の時間帯（設定の timezone）と OS の時間帯が違うと、
# 5:00 に動かすつもりが放送の時間帯（9:55〜17:05）に入りうる（OS が UTC なら 14:00）。時間帯が同じこと、
# 設定の放送が 04:30〜06:30（タイマーの動く 5:00〜6:00 ごろの前後）に無いことを確かめる。
updates_check_timing() {
  local chime_tz os_tz found rc=0
  load_chime_config || rc=$?
  if [ "${rc}" -eq 2 ]; then
    say "  （root でも ${TARGET_USER} でもないので、放送の設定を読めません。時間帯と放送の時刻の確認は、実際の実行のときに行います）"
    return 0
  fi
  if [ "${rc}" -ne 0 ] || ! chime_tz="$(chime_timezone)"; then
    timing_problem "放送の設定（campus_chime.py --print-config の timezone）を読めないので、apt のタイマー（5:00〜6:00 ごろ）が放送の時間帯と重ならないか確かめられません。"
    return 0
  fi
  if ! command -v timedatectl > /dev/null 2>&1 || ! os_tz="$(timedatectl show -p Timezone --value 2> /dev/null)" || [ -z "${os_tz}" ]; then
    timing_problem "OS の時間帯（timedatectl show -p Timezone）を調べられないので、apt のタイマーが放送の時間帯と重ならないか確かめられません。"
    return 0
  fi
  if [ "${os_tz}" != "${chime_tz}" ] && ! python3 -I -c "${CFG_PY}" same "${os_tz}" "${chime_tz}" 2> /dev/null; then
    timing_problem "OS の時間帯（${os_tz}）が、放送の時間帯（${chime_tz}）と違います。apt のタイマーは OS の時間帯で動くので、5:00 のつもりが放送の時間帯に入ることがあります（UTC なら 14:00 です）。sudo timedatectl set-timezone ${chime_tz} を実行してから、もう一度実行してください。"
    return 0
  fi
  if ! found="$(printf '%s' "${CHIME_CONFIG_JSON}" | python3 -I -c "${CFG_PY}" quiet 2> /dev/null)"; then
    timing_problem "放送の設定（schedule）から放送の時刻を読み取れません。"
    return 0
  fi
  if [ -n "${found}" ]; then
    timing_problem "放送の予定（$(printf '%s' "${found}" | tr '\n' ' ' | sed 's/ $//')）が、apt のタイマーの動く時間（5:00〜6:00 ごろ）の前後（4:30〜6:30）にあります。apt の更新が放送とぶつかります。"
    return 0
  fi
  say "  OS の時間帯は放送と同じ（${chime_tz}）で、4:30〜6:30 に放送はありません。タイマーの 5:00〜6:00 ごろは安全です。"
}

timing_problem() {
  if [ "${FORCE}" -eq 1 ]; then
    warn "$1 --force が付いているので、このまま進めます。"
  else
    refuse "$1 承知のうえなら --force を付けてください。"
  fi
}

preflight_updates() {
  local unit
  log "確認: updates"
  updates_check_timing
  updates_collect_patterns
  if [ "${#UPD_PATTERNS[@]}" -eq 0 ]; then
    refuse "apt の配布元（apt-cache policy の release 行）から、自動更新の対象を見つけられません。ネットワークと /etc/apt/sources.list を確かめてください。"
  else
    say "  自動更新の対象（apt-cache policy の配布元から作ります）:"
    printf '    %s\n' "${UPD_PATTERNS[@]}"
  fi
  for unit in apt-daily.timer apt-daily-upgrade.timer; do
    if ! unit_exists "${unit}"; then
      refuse "${unit} がありません。apt の標準のタイマーがない環境では、この手順は使えません。"
    fi
  done
  UPD_INSTALL=0
  if ! pkg_installed unattended-upgrades; then
    UPD_INSTALL=1
    say "  unattended-upgrades が入っていません。適用のとき、apt-get install -y で入れます。"
  fi
  if [ "${PRE_FAIL}" -eq 0 ] && step_done updates \
    && file_is "${APT_UPGRADES_CONF}" 644 "$(updates_conf_content)" \
    && file_is "${APT_PERIODIC_CONF}" 644 "$(updates_periodic_content)" \
    && file_is "${APT_DAILY_DROPIN}" 644 "$(updates_timer_content 05:00 20min)" \
    && file_is "${APT_UPGRADE_DROPIN}" 644 "$(updates_timer_content 05:40 10min)"; then
    STEP_SKIP[updates]="すでに適用済みです。"
  fi
}

apply_updates() {
  ensure_idle "${LOOKAHEAD_MINUTES}" || return 1
  if [ "${UPD_INSTALL}" -eq 1 ]; then
    # 入れる前に記録する（入れた直後に止められても、戻すときに入れたと分かるように）。
    if [ "${DRY_RUN}" -eq 0 ]; then
      record_first updates installed_by_us 1 || return 1
    fi
    apt_refresh
    sys apt-get install -y unattended-upgrades || { warn "unattended-upgrades を入れられませんでした。"; apt_install_hint; return 1; }
  fi
  put_file updates "${APT_UPGRADES_CONF}" 644 "$(updates_conf_content)" || return 1
  put_file updates "${APT_PERIODIC_CONF}" 644 "$(updates_periodic_content)" || return 1
  ensure_dir updates "${APT_DAILY_DROPIN%/*}" 755 || return 1
  put_file updates "${APT_DAILY_DROPIN}" 644 "$(updates_timer_content 05:00 20min)" || return 1
  ensure_dir updates "${APT_UPGRADE_DROPIN%/*}" 755 || return 1
  put_file updates "${APT_UPGRADE_DROPIN}" 644 "$(updates_timer_content 05:40 10min)" || return 1
  sys systemctl daemon-reload || return 1
  sys systemctl restart apt-daily.timer apt-daily-upgrade.timer || return 1
}

verify_updates() {
  local dump pattern line
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "確認: apt-config dump に Origins-Pattern・Automatic-Reboot \"false\"・Periodic の設定があること。systemctl list-timers 'apt-daily*' を表示"
    return 0
  fi
  if ! dump="$(apt-config dump 2>&1)"; then
    warn "apt-config dump が失敗しました: ${dump}"
    return 1
  fi
  for pattern in "${UPD_PATTERNS[@]}"; do
    if [[ "${dump}" != *"${pattern}"* ]]; then
      warn "apt の設定に、自動更新の対象 ${pattern} が見えません。"
      return 1
    fi
  done
  if [[ "${dump}" != *'Unattended-Upgrade::Automatic-Reboot "false"'* ]] || [[ "${dump}" != *'APT::Periodic::Unattended-Upgrade "1"'* ]]; then
    warn "apt の設定に、再起動しない設定または自動更新の設定が見えません。"
    return 1
  fi
  if ! unit_active apt-daily.timer || ! unit_active apt-daily-upgrade.timer; then
    warn "apt のタイマーが動いていません。"
    return 1
  fi
  say "apt-config dump の自動更新の対象:"
  while IFS= read -r line; do
    if [[ "${line}" == *Origins-Pattern* ]]; then
      say "  ${line}"
    fi
  done <<< "${dump}"
  systemctl list-timers 'apt-daily*' --no-pager || true
}

# 戻すときに放送を待つのは手で戻すときだけ（自動復元では待たない）。$1 は、次の放送まで空ける分数。
ROLLBACK_IDLE=1
rollback_idle() {
  if [ "${ROLLBACK_IDLE}" -eq 0 ]; then
    return 0
  fi
  ensure_idle "${1:-0}"
}

rollback_updates() {
  local rc=0
  restore_step_files updates || rc=1
  systemctl daemon-reload || rc=1
  systemctl restart apt-daily.timer apt-daily-upgrade.timer || rc=1
  # 入れる前に記録するので、実際には入っていないこともある（入っていなければ何もしない）。
  if [ "$(meta_get "${STATE_DIR}/applied/updates" installed_by_us 2>/dev/null || true)" = 1 ] && pkg_installed unattended-upgrades; then
    if rollback_idle "${LOOKAHEAD_MINUTES}"; then
      apt-get purge -y unattended-upgrades || { warn "unattended-upgrades を削除できませんでした。"; rc=1; }
    else
      rc=1
    fi
  fi
  return "${rc}"
}

# ---------------------------------------------------------------------------
# 手順 service: 放送サービス（campus_chime.service）に保護をかける
# ---------------------------------------------------------------------------

SVC_RW_DIRS=()
SANDBOX_PROPS=()
SERVICE_RESTARTED=0
ANALYZE_BASE_RC=0
SCORE_BEFORE=""

# 設定（--print-config の JSON）から、サービスが書く場所のディレクトリを 1 行ずつ表示する。
# chime/config.py の Config.resolve と同じ解き方: まず ~ を展開し（HOME は呼ぶ側が、サービスの利用者の
# ホームにして渡す）、相対パスはリポジトリからの相対。
DIRS_PY='
import json
import os
import sys

repo = sys.argv[1]
cfg = json.load(sys.stdin)


def pick(*keys):
    cur = cfg
    for key in keys:
        cur = cur.get(key) if isinstance(cur, dict) else None
    return cur if isinstance(cur, str) and cur else None


seen = []
for keys, is_dir in ((("state", "file"), False), (("state", "history_file"), False),
                     (("state", "pause_file"), False), (("tts", "cache_dir"), True),
                     (("time_signal", "output_file"), False)):
    value = pick(*keys)
    if value is None:
        continue
    expanded = os.path.expanduser(value)
    path = os.path.normpath(expanded if os.path.isabs(expanded) else os.path.join(repo, expanded))
    if path.startswith("//"):
        # POSIX は先頭の // を残す（normpath しても //home/pi のまま）。/ 1 つにそろえる。
        path = "/" + path.lstrip("/")
    directory = path if is_dir else os.path.dirname(path)
    if directory not in seen:
        seen.append(directory)
        print(directory)
'

# 保護の設定の唯一の定義。ドロップインにも --test-sound の systemd-run にも、これを使う。
# 使わないもの（理由つき）:
#   PrivateDevices・DeviceAllow・DevicePolicy: 音声デバイス（/dev/snd）に届かなくなり、音が出ない。
#   ProtectClock: 単独なら Bookworm / Trixie で無害だが、DevicePolicy・DeviceAllow・PrivateDevices のどれかと
#     組み合わさるとデバイスの許可リストが付いて /dev/snd を塞ぐ。得られるのは露出度の約 0.1 だけなので使わない。
#   MemoryDenyWriteExecute: pygame / SDL の実行時コード生成に差し支えうる。
#   ProcSubset=pid: SDL が /proc/cpuinfo を読む。
#   RemoveIPC・PrivateUsers: 得るものが小さく、音声まわりを壊す恐れがある。
build_sandbox_props() {
  local dir paths=""
  for dir in "${SVC_RW_DIRS[@]}"; do
    paths+="${paths:+ }-${dir}"
  done
  SANDBOX_PROPS=(
    ProtectSystem=strict
    ProtectHome=read-only
    "ReadWritePaths=${paths}"
    PrivateTmp=yes
    ProtectKernelTunables=yes
    ProtectKernelModules=yes
    ProtectKernelLogs=yes
    ProtectControlGroups=yes
    ProtectHostname=yes
    ProtectProc=invisible
    RestrictNamespaces=yes
    RestrictRealtime=yes
    RestrictSUIDSGID=yes
    LockPersonality=yes
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK"
    SystemCallArchitectures=native
    SystemCallFilter=@system-service
    SystemCallErrorNumber=EPERM
    CapabilityBoundingSet=
    KeyringMode=private
  )
}

# ドロップインの中身。ユニットファイルでは % が指定子（%t など）として展開されるので、パスの % は %% と書く
# （systemd-run -p に渡す値は展開されないので、SANDBOX_PROPS そのものは直さない）。
service_dropin_content() {
  local prop lines=()
  for prop in "${SANDBOX_PROPS[@]}"; do
    case "${prop}" in
      ReadWritePaths=*) lines+=("${prop//\%/%%}") ;;
      *) lines+=("${prop}") ;;
    esac
  done
  printf '%s\n' \
    '# scripts/harden.sh service が作成（元に戻す: sudo bash scripts/harden.sh --rollback service）' \
    '[Service]' \
    "${lines[@]}"
}

# ディレクトリ $1 を ${TARGET_USER} が作れるか。無ければ、いちばん近い既存の親に書けるかを調べる。
# 作れなければ、そのとおりに直す方法つきで事前の確認の問題にする。
service_check_creatable() {
  local dir="$1" parent="$1"
  if [ -d "${dir}" ]; then
    if ! as_user test -w "${dir}"; then
      warn "書き込み先 ${dir} には、${TARGET_USER} が書き込めません。サービスが状態を保存できません（sudo chown -R ${TARGET_USER}:${TARGET_USER} $(printf '%q' "${dir}") で直せます）。"
    fi
    return 0
  fi
  while [ -n "${parent}" ] && [ ! -d "${parent}" ]; do
    parent="${parent%/*}"
  done
  [ -n "${parent}" ] || parent=/
  if ! as_user test -w "${parent}"; then
    refuse "書き込み先 ${dir} を、${TARGET_USER} は作れません（${parent} に書き込めません）。次を実行してから、もう一度実行してください: sudo mkdir -p $(printf '%q' "${dir}") && sudo chown ${TARGET_USER}:${TARGET_USER} $(printf '%q' "${dir}")"
  fi
}

# 書き込み先のディレクトリを求める。求められなければ事前の確認の問題にする。
service_collect_dirs() {
  local dirs dir rc=0 real real_home broad cand
  SVC_RW_DIRS=()
  if [ ! -f "${REPO_DIR}/campus_chime.py" ]; then
    refuse "campus_chime.py が見つかりません（${REPO_DIR}）。設定から書き込み先を求められません。"
    return 0
  fi
  load_chime_config || rc=$?
  if [ "${rc}" -eq 2 ]; then
    # --dry-run だけ。root でも ${TARGET_USER} 本人でもないと、設定を読めない。
    say "  （root でも ${TARGET_USER} でもないので、設定を読めません。書き込み先は、実際の実行のときに求めます）"
    SVC_RW_DIRS=("<設定の書き込み先>")
    return 0
  fi
  if [ "${rc}" -ne 0 ]; then
    refuse "設定を読めません（campus_chime.py --print-config が失敗）。config.json を直してから、もう一度実行してください。"
    return 0
  fi
  if ! dirs="$(printf '%s' "${CHIME_CONFIG_JSON}" | HOME="${USER_HOME}" python3 -I -c "${DIRS_PY}" "${REPO_DIR}" 2>/dev/null)"; then
    refuse "設定の JSON から書き込み先を求められません。"
    return 0
  fi
  while IFS= read -r dir; do
    [ -n "${dir}" ] || continue
    case "${dir}" in
      /*) ;;
      *)
        refuse "設定の書き込み先 ${dir} が絶対パスになりません。config.json のパス（state・tts.cache_dir・time_signal.output_file）を確かめてください。"
        continue
        ;;
    esac
    # 広すぎる場所か。シンボリックリンクの先（realpath）も同じ基準で見る。
    real="$(realpath -m -- "${dir}" 2> /dev/null || printf '%s' "${dir}")"
    real_home="$(realpath -m -- "${USER_HOME}" 2> /dev/null || printf '%s' "${USER_HOME}")"
    broad=0
    for cand in "${dir}" "${real}"; do
      case "${cand}" in
        / | /etc | /usr | /var | /home | "${USER_HOME}" | "${real_home}") broad=1 ;;
      esac
    done
    if [ "${broad}" -eq 1 ]; then
      refuse "設定の書き込み先 ${dir} は広すぎます。サービスに書かせてはいけない場所なので、config.json のパス（state・tts.cache_dir・time_signal.output_file）を直してください。"
      continue
    fi
    case "${dir}" in
      *[[:space:]]*)
        refuse "設定の書き込み先 ${dir} に空白があります。systemd の設定に安全に書けないので、空白のないパスにしてください。"
        continue
        ;;
      *'$'* | *\\*)
        refuse "設定の書き込み先 ${dir} に \$ か \\ があります。systemd の設定に安全に書けないので、使わないパスにしてください。"
        continue
        ;;
    esac
    case "${dir}" in
      "${REPO_DIR}")
        warn "書き込み先 ${dir} はリポジトリそのものです。サービスが自分のコードを書き換えられてしまいます（state・tts.cache_dir・time_signal.output_file を cache/ などの下にすると防げます）。"
        ;;
      "${REPO_DIR}"/*) ;;
      *) warn "書き込み先 ${dir} はリポジトリの外です。サービスに書き込みを許します。" ;;
    esac
    service_check_creatable "${dir}"
    SVC_RW_DIRS+=("${dir}")
  done <<< "${dirs}"
  if [ "${#SVC_RW_DIRS[@]}" -eq 0 ]; then
    refuse "サービスの書き込み先を 1 つも求められませんでした。"
  fi
}

analyze_score() {
  local out line
  out="$(systemd-analyze security "${SERVICE_NAME}" --no-pager 2>/dev/null || true)"
  while IFS= read -r line; do
    if [[ "${line}" == *"Overall exposure level"* ]]; then
      printf '%s' "${line}"
      return 0
    fi
  done <<< "${out}"
  printf '（取得できません）'
}

# ディレクトリ $1 と $2 が同じ場所か（末尾の / や、シンボリックリンク経由の違いは同じとみなす）。
same_dir() {
  local a="${1%/}" b="${2%/}"
  [ "${a}" = "${b}" ] && return 0
  [ "$(realpath -m "${a}" 2> /dev/null)" = "$(realpath -m "${b}" 2> /dev/null)" ]
}

# 入っているユニットが動かす場所（WorkingDirectory と ExecStart の campus_chime.py）が、このスクリプトのある
# リポジトリと同じか。違うと、書き込み先を違う config.json から求めてしまい、本当の書き込み先を許せない。
service_check_unit_location() {
  local unit_file="$1" line wd="" exec_dir="" word
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in
      WorkingDirectory=*) wd="${line#WorkingDirectory=}"; wd="${wd#-}" ;;
      ExecStart=*)
        for word in ${line#ExecStart=}; do
          case "${word}" in
            */campus_chime.py) exec_dir="${word%/campus_chime.py}" ;;
          esac
        done
        ;;
    esac
  done < "${unit_file}"
  if [ -n "${wd}" ] && ! same_dir "${wd}" "${REPO_DIR}"; then
    refuse "${SERVICE_NAME} は ${wd} で動いていますが、このスクリプトは ${REPO_DIR} にあります。書き込み先は、動いているほうの config.json から求める必要があります。${wd}/scripts/harden.sh を使ってください。"
  elif [ -n "${exec_dir}" ] && ! same_dir "${exec_dir}" "${REPO_DIR}"; then
    refuse "${SERVICE_NAME} は ${exec_dir}/campus_chime.py を動かしますが、このスクリプトは ${REPO_DIR} にあります。${exec_dir}/scripts/harden.sh を使ってください。"
  fi
}

preflight_service() {
  local unit_file="${SYSROOT}/etc/systemd/system/${SERVICE_NAME}" line unit_user=""
  log "確認: service"
  if [ ! -f "${unit_file}" ]; then
    refuse "${SERVICE_NAME} が /etc/systemd/system に入っていません。先に bash scripts/setup.sh --no-apt で入れてください。"
  else
    # 書き込み先を作る利用者（--user）が、サービスを動かす利用者と違うと、サービスが書けなくなる。
    while IFS= read -r line || [ -n "${line}" ]; do
      if [[ "${line}" == User=* ]]; then
        unit_user="${line#User=}"
      fi
    done < "${unit_file}"
    if [ -n "${unit_user}" ] && [ "${unit_user}" != "${TARGET_USER}" ]; then
      refuse "${SERVICE_NAME} は ${unit_user} で動きますが、--user は ${TARGET_USER} です。書き込み先の持ち主が合わなくなるので、--user ${unit_user} を付けてください。"
    fi
    service_check_unit_location "${unit_file}"
  fi
  if ! command -v systemd-analyze > /dev/null 2>&1; then
    refuse "systemd-analyze が見つかりません。"
  fi
  service_collect_dirs
  if [ "${#SVC_RW_DIRS[@]}" -gt 0 ]; then
    build_sandbox_props
    say "  書き込みを許すディレクトリ: ${SVC_RW_DIRS[*]}"
  fi
  if [ "${PRE_FAIL}" -eq 0 ]; then
    ANALYZE_BASE_RC=0
    systemd-analyze verify "${SERVICE_NAME}" > /dev/null 2>&1 || ANALYZE_BASE_RC=$?
    SCORE_BEFORE="$(analyze_score)"
    if step_done service && file_is "${SERVICE_DROPIN}" 644 "$(service_dropin_content)" && unit_active "${SERVICE_NAME}"; then
      STEP_SKIP[service]="すでに適用済みです。"
    fi
  fi
}

# ディレクトリ $1 を、無い階層だけ利用者の持ち物として作り、作った階層を記録する（SYSROOT の外の実パス）。
make_user_dir() {
  local dir="$1" parent="${1%/*}"
  if [ -d "${dir}" ]; then
    return 0
  fi
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "実行: runuser -u ${TARGET_USER} -- mkdir -p ${dir}"
    return 0
  fi
  if [ -n "${parent}" ]; then
    make_user_dir "${parent}" || return 1
  fi
  # 作る前に記録する（作った直後に止められても、戻すときに消せるように。無ければ戻すとき何もしない）。
  record_created service r "${dir}" || return 1
  runuser -u "${TARGET_USER}" -- mkdir -p "${dir}" || { warn "${dir} を作れませんでした。"; return 1; }
}

apply_service() {
  local dir
  # 書き込み先が無ければ、サービスの利用者の持ち物として作る（root が作ると、サービスが書けない）。
  for dir in "${SVC_RW_DIRS[@]}"; do
    make_user_dir "${dir}" || return 1
  done
  ensure_dir service "${SERVICE_DROPIN%/*}" 755 || return 1
  put_file service "${SERVICE_DROPIN}" 644 "$(service_dropin_content)" || return 1
}

verify_service() {
  local out out_rc=0 line since problems="" score_after
  local dir
  local bad_re='Unknown |ignoring|Failed to parse|Failed to resolve|Invalid'
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "確認: systemd-analyze verify ${SERVICE_NAME}（知らない項目の警告があれば失敗）"
    ensure_idle
    plan "実行: systemctl daemon-reload"
    plan "実行: systemctl restart ${SERVICE_NAME}（5 秒後に active と、journal にエラーが無いことを確認）"
    return 0
  fi
  # verify は、知らない項目があっても終了コード 0 で警告を出すだけなので、出力の文面でも判定する。
  out="$(systemd-analyze verify "${SERVICE_NAME}" 2>&1)" || out_rc=$?
  while IFS= read -r line; do
    if [[ "${line}" == *campus-chime-harden* ]] && [[ "${line}" =~ ${bad_re} ]]; then
      problems+="${line}"$'\n'
    fi
  done <<< "${out}"
  [ -z "${out}" ] || printf '%s\n' "${out}"
  if [ -n "${problems}" ]; then
    warn "systemd が保護の設定を受け付けません: ${problems%$'\n'}"
    return 1
  fi
  if [ "${out_rc}" -ne 0 ] && [ "${ANALYZE_BASE_RC}" -eq 0 ]; then
    warn "systemd-analyze verify が失敗しました（終了コード ${out_rc}）。"
    return 1
  fi
  ensure_idle || return 1
  # 再起動の前に、書き込み先が全部あることを確かめる（無いディレクトリは、保護の下ではサービスが作れない）。
  for dir in "${SVC_RW_DIRS[@]}"; do
    make_user_dir "${dir}" || return 1
  done
  systemctl daemon-reload || { warn "systemctl daemon-reload が失敗しました。"; return 1; }
  now
  since="${NOW}"
  SERVICE_RESTARTED=1
  systemctl restart "${SERVICE_NAME}" || { warn "${SERVICE_NAME} を再起動できませんでした。"; return 1; }
  sleep 5
  if ! unit_active "${SERVICE_NAME}"; then
    warn "${SERVICE_NAME} が起動していません（保護の設定が合わなかったようです）。"
    return 1
  fi
  out="$(journalctl -u "${SERVICE_NAME}" --since "@${since}" -p err -o cat --quiet 2>/dev/null || true)"
  out="${out//-- No entries --/}"
  if [ -n "${out//[[:space:]]/}" ]; then
    warn "再起動のあと、サービスがエラーを記録しています: ${out}"
    return 1
  fi
  score_after="$(analyze_score)"
  say "  保護の点数（前）: ${SCORE_BEFORE}"
  say "  保護の点数（後）: ${score_after}"
}

rollback_service() {
  local rc=0
  restore_step_files service || rc=1
  systemctl daemon-reload || rc=1
  if [ "${SERVICE_RESTARTED}" -eq 1 ]; then
    if rollback_idle; then
      systemctl restart "${SERVICE_NAME}" || { warn "${SERVICE_NAME} を再起動できませんでした。"; rc=1; }
    else
      warn "放送の最中かもしれないので、再起動していません。放送のない時間に systemctl restart ${SERVICE_NAME} を実行してください。"
      rc=1
    fi
  fi
  return "${rc}"
}

# 保護をかけたまま 12 時の時報を 1 回鳴らす（本番と同じ設定の一時的な実行）。
run_test_sound() {
  local args=() prop
  if [ ! -f "${REPO_DIR}/campus_chime.py" ]; then
    fail "campus_chime.py が見つかりません（${REPO_DIR}）。"
  fi
  PRE_FAIL=0
  service_collect_dirs
  [ "${PRE_FAIL}" -eq 0 ] || fail "書き込み先を求められません。何も変更していません。"
  build_sandbox_props
  for prop in "${SANDBOX_PROPS[@]}"; do
    args+=(-p "${prop}")
  done
  ensure_idle || fail "放送の最中かもしれないので、音を鳴らしません。何も変更していません。"
  log "音の確認（12 時の時報）"
  say "保護をかけた状態で、12 時の時報を 1 回、スピーカーから鳴らします。"
  if systemd-run --wait --pipe --collect --quiet "--uid=${TARGET_USER}" "--gid=${TARGET_USER}" \
    -p SupplementaryGroups=audio -p "WorkingDirectory=${REPO_DIR}" \
    -p Environment=SDL_AUDIODRIVER=alsa -p Environment=PYTHONUNBUFFERED=1 -p NoNewPrivileges=yes \
    "${args[@]}" /usr/bin/python3 "${REPO_DIR}/campus_chime.py" --test-hourly 12; then
    say "終わりました（終了コード 0）。音が出たかを確かめてください。出なければ、$(harden_cmd) --rollback service で戻せます。"
  else
    warn "時報の再生が失敗しました（終了コード $?）。保護の設定が合わない可能性があります。$(harden_cmd) --rollback service で戻せます。"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# 手順 ntp: 時刻同期を chrony にする（起動後の大きな時刻の飛びを受け付けない）
# ---------------------------------------------------------------------------
# systemd-timesyncd は、偽の NTP 応答が求める時刻の飛びをそのまま受け入れる。学内の誰かが偽装すると、
# 違う時刻に鳴る、1 日鳴らない、が起こりうる。Debian の chrony の既定（makestep 1 3）は、最初の 3 回の
# 更新だけ時刻を飛ばし、以後の大きなずれは少しずつ寄せる（1 時間ずらすのに約 12 時間かかる）。
# rtcsync もあるので、timedatectl の NTPSynchronized（--status と setup.sh が読む）も yes になる。

NTP_SERVERS_EFFECTIVE=()
NTP_INSTALL=0
NTP_REPLACES_TIMESYNCD=0
NTP_OTHER_REMOVALS=()

# apt-get -s install chrony の出力 $1 を読む。systemd-timesyncd が削除されるなら NTP_REPLACES_TIMESYNCD=1
# （戻すときに入れ直す。入っているかを別に調べない。調べそこなって控えそこなわないため）、それ以外が
# 削除されるなら NTP_OTHER_REMOVALS に入れる。
ntp_parse_plan() {
  local word pkg
  NTP_REPLACES_TIMESYNCD=0
  NTP_OTHER_REMOVALS=()
  while read -r word pkg _; do
    if [ "${word}" = Remv ]; then
      if [ "${pkg}" = systemd-timesyncd ]; then
        NTP_REPLACES_TIMESYNCD=1
      else
        NTP_OTHER_REMOVALS+=("${pkg}")
      fi
    fi
  done <<< "$1"
}

# timesyncd の NTP= を（本体の設定と timesyncd.conf.d/*.conf の順に）読んで 1 行ずつ表示する。
# 空の代入は、それまでの分を消す。
timesyncd_servers() {
  local files=() f line section host servers=() hosts=()
  if [ -f "${SYSROOT}/etc/systemd/timesyncd.conf" ]; then
    files+=("${SYSROOT}/etc/systemd/timesyncd.conf")
  fi
  for f in "${SYSROOT}/etc/systemd/timesyncd.conf.d/"*.conf; do
    if [ -f "${f}" ]; then
      files+=("${f}")
    fi
  done
  for f in "${files[@]}"; do
    section=""
    while IFS= read -r line || [ -n "${line}" ]; do
      line="${line#"${line%%[![:space:]]*}"}"
      case "${line}" in
        "[Time]"*) section=Time ;;
        "["*) section=other ;;
        NTP=*)
          [ "${section}" = Time ] || continue
          line="${line#NTP=}"
          if [ -z "${line//[[:space:]]/}" ]; then
            servers=()
          else
            read -r -a hosts <<< "${line}"
            servers+=("${hosts[@]}")
          fi
          ;;
      esac
    done < "${f}"
  done
  for host in "${servers[@]}"; do
    printf '%s\n' "${host}"
  done
}

ntp_sources_content() {
  local host
  printf '%s\n' '# scripts/harden.sh ntp が作成（元に戻す: sudo bash scripts/harden.sh --rollback ntp）'
  for host in "${NTP_SERVERS_EFFECTIVE[@]}"; do
    printf 'server %s iburst\n' "${host}"
  done
}

preflight_ntp() {
  local host sim_out carried="" chrony_ready=0
  log "確認: ntp"
  NTP_SERVERS_EFFECTIVE=()
  NTP_INSTALL=0
  NTP_REPLACES_TIMESYNCD=0
  if pkg_installed chrony && unit_active chrony.service; then
    chrony_ready=1
  fi
  for host in "${NTP_SERVERS[@]}"; do
    if [[ ! "${host}" =~ ^[A-Za-z0-9.:-]+$ ]]; then
      refuse "--ntp-server ${host} は、ホスト名か IP アドレスとして読めません（英数字と . : - だけ）。"
    else
      NTP_SERVERS_EFFECTIVE+=("${host}")
    fi
  done
  # chrony を新しく入れるときだけ、timesyncd に設定されていた NTP サーバーを引き継ぐ。
  if [ "${#NTP_SERVERS[@]}" -eq 0 ] && [ "${chrony_ready}" -eq 0 ]; then
    while IFS= read -r host; do
      [ -n "${host}" ] || continue
      if [[ "${host}" =~ ^[A-Za-z0-9.:-]+$ ]]; then
        NTP_SERVERS_EFFECTIVE+=("${host}")
        carried+=" ${host}"
      else
        warn "timesyncd の NTP=${host} は読み取れないので、引き継ぎません。"
      fi
    done < <(timesyncd_servers)
    if [ -n "${carried}" ]; then
      say "  timesyncd に設定されていた NTP サーバーを引き継ぎます:${carried}"
    fi
  fi
  if [ "${chrony_ready}" -eq 1 ]; then
    say "  chrony はすでに入っていて動いています。"
    if [ "${#NTP_SERVERS_EFFECTIVE[@]}" -eq 0 ]; then
      STEP_SKIP[ntp]="chrony がすでに時刻を同期しています。何もしません。"
    elif step_done ntp && file_is "${CHRONY_SOURCES}" 644 "$(ntp_sources_content)"; then
      STEP_SKIP[ntp]="すでに適用済みです。"
    fi
    return 0
  fi
  NTP_INSTALL=1
  if ! sim_out="$(apt_read apt-get -s install chrony 2>&1)"; then
    refuse "apt-get -s install chrony（試しの実行）が失敗しました: ${sim_out}"
    return 0
  fi
  ntp_parse_plan "${sim_out}"
  if [ "${#NTP_OTHER_REMOVALS[@]}" -gt 0 ]; then
    refuse "chrony を入れると、systemd-timesyncd 以外のパッケージ（${NTP_OTHER_REMOVALS[*]}）も削除されます。apt の計画: ${sim_out//$'\n'/ ; }"
  fi
}

# 事前の確認の試しの実行は、古い一覧のままだったかもしれない。一覧を新しくしたあとにもう一度試して、
# systemd-timesyncd 以外が削除されるなら、入れずに止める。
ntp_recheck_plan() {
  local sim_out
  [ "${DRY_RUN}" -eq 0 ] || return 0
  if ! sim_out="$(apt_read apt-get -s install chrony 2>&1)"; then
    warn "一覧を新しくしたあとの apt-get -s install chrony（試しの実行）が失敗しました: ${sim_out}"
    apt_install_hint
    return 1
  fi
  ntp_parse_plan "${sim_out}"
  if [ "${#NTP_OTHER_REMOVALS[@]}" -gt 0 ]; then
    warn "一覧を新しくしたら、chrony を入れると systemd-timesyncd 以外のパッケージ（${NTP_OTHER_REMOVALS[*]}）も削除されると分かりました。入れずに止めます。"
    return 1
  fi
}

apply_ntp() {
  local i
  ensure_idle "${LOOKAHEAD_MINUTES}" || return 1
  if [ "${NTP_INSTALL}" -eq 1 ]; then
    if [ "${DRY_RUN}" -eq 0 ] && [ "${NTP_REPLACES_TIMESYNCD}" -eq 1 ]; then
      record_first ntp replaced_timesyncd 1 || return 1
    fi
    apt_refresh
    ntp_recheck_plan || return 1
    sys apt-get install -y chrony || { warn "chrony を入れられませんでした。"; apt_install_hint; return 1; }
  fi
  if [ "${#NTP_SERVERS_EFFECTIVE[@]}" -gt 0 ]; then
    ensure_dir ntp "${CHRONY_SOURCES%/*}" 755 || return 1
    put_file ntp "${CHRONY_SOURCES}" 644 "$(ntp_sources_content)" || return 1
    sys chronyc reload sources || warn "chronyc reload sources が失敗しました（chrony の再起動で読み直されます）。"
  fi
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "確認: /etc/chrony/chrony.conf の makestep を表示し、最大 90 秒、NTPSynchronized が yes になるのを待つ"
    return 0
  fi
  say "chrony.conf の makestep（時刻を飛ばす回数の制限）:"
  grep -E '^makestep' "${SYSROOT}/etc/chrony/chrony.conf" 2>/dev/null || say "  （makestep の行が見つかりません）"
  if ! grep -Eq '^makestep[[:space:]]+[0-9.]+[[:space:]]+[0-9]+[[:space:]]*$' "${SYSROOT}/etc/chrony/chrony.conf" 2>/dev/null; then
    warn "makestep が『makestep 1 3』のような回数の制限になっていません。大きな時刻の飛びを受け付けない、という狙いが効かないかもしれません。"
  fi
  for ((i = 0; i < 30; i++)); do
    if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = yes ]; then
      break
    fi
    sleep 3
  done
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" = yes ]; then
    say "時刻が同期しました。"
  else
    warn "90 秒待っても、時刻の同期（NTPSynchronized）が yes になりませんでした。時計はそのまま動き続けます。同期しないままなら、$(harden_cmd) --rollback ntp で戻せます。"
  fi
  chronyc -n sources || true
  say "docs/KNOWLEDGE_BASE.md の systemctl restart systemd-timesyncd は、今後 systemctl restart chrony になります。"
}

verify_ntp() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    plan "確認: chrony が動いていること"
    return 0
  fi
  if ! unit_active chrony.service; then
    warn "chrony が動いていません。"
    return 1
  fi
}

rollback_ntp() {
  local rc=0
  restore_step_files ntp || rc=1
  if [ "$(meta_get "${STATE_DIR}/applied/ntp" replaced_timesyncd 2>/dev/null || true)" = 1 ]; then
    if pkg_installed systemd-timesyncd; then
      # chrony を入れる前に止まったなど、timesyncd がまだ入っているなら、入れ直さない（有効にするだけ）。
      systemctl enable --now systemd-timesyncd || { warn "systemd-timesyncd を有効にできませんでした。"; rc=1; }
    elif rollback_idle "${LOOKAHEAD_MINUTES}"; then
      if ! apt-get update; then
        warn "apt-get update が失敗しました。ネットワーク（学内の網につながっているか、名前が引けるか）を確かめてください。古い一覧のまま続けます。"
      fi
      apt-get install -y systemd-timesyncd || { warn "systemd-timesyncd を入れ直せませんでした。"; apt_install_hint; rc=1; }
      systemctl enable --now systemd-timesyncd || { warn "systemd-timesyncd を有効にできませんでした。"; rc=1; }
    else
      rc=1
    fi
  elif unit_active chrony.service; then
    chronyc reload sources || warn "chronyc reload sources が失敗しました。"
  fi
  return "${rc}"
}

# ---------------------------------------------------------------------------
# 手順の振り分け（関数名を組み立てて呼ばず、case で明示する）
# ---------------------------------------------------------------------------

step_title() {
  case "$1" in
    ssh) printf 'SSH を鍵だけにする（パスワードログインを止める）' ;;
    firewall) printf '入ってくる通信を SSH（管理用の網から）・mDNS・DHCP・ICMP だけにする' ;;
    sudo) printf 'sudo にパスワードを求める' ;;
    services) printf '使わない常駐（bluetooth・hciuart・ModemManager）を止めて無効にする' ;;
    journal) printf 'ログを再起動後も残す（上限 200M）' ;;
    updates) printf 'セキュリティ更新を自動で入れる（再起動はしない）' ;;
    service) printf '放送サービスに保護（sandbox）をかける' ;;
    ntp) printf '時刻同期を chrony にする（起動後の大きな時刻の飛びを受け付けない）' ;;
  esac
}

step_preflight() {
  case "$1" in
    ssh) preflight_ssh ;;
    firewall) preflight_firewall ;;
    sudo) preflight_sudo ;;
    services) preflight_services ;;
    journal) preflight_journal ;;
    updates) preflight_updates ;;
    service) preflight_service ;;
    ntp) preflight_ntp ;;
  esac
}

step_apply() {
  case "$1" in
    ssh) apply_ssh ;;
    firewall) apply_firewall ;;
    sudo) apply_sudo ;;
    services) apply_services ;;
    journal) apply_journal ;;
    updates) apply_updates ;;
    service) apply_service ;;
    ntp) apply_ntp ;;
  esac
}

step_verify() {
  case "$1" in
    ssh) verify_ssh ;;
    firewall) verify_firewall ;;
    sudo) verify_sudo ;;
    services) verify_services ;;
    journal) verify_journal ;;
    updates) verify_updates ;;
    service) verify_service ;;
    ntp) verify_ntp ;;
  esac
}

step_rollback() {
  case "$1" in
    ssh) rollback_ssh ;;
    firewall) rollback_firewall ;;
    sudo) rollback_sudo ;;
    services) rollback_services ;;
    journal) rollback_journal ;;
    updates) rollback_updates ;;
    service) rollback_service ;;
    ntp) rollback_ntp ;;
  esac
}

# 適用できたあとに伝えること。
step_report() {
  case "$1" in
    ssh)
      say "SSH を鍵だけにしました（${SSHD_DROPIN}）。いまの接続は切れません。"
      ;;
    firewall)
      say "防火壁を入れました。確定するまでは、再起動すると消えます。この Pi と別のネットワーク（別の VLAN・VPN など）から SSH するなら、--ssh-from で足してから確定してください（確定したあとは、そこからは入れません）。"
      ;;
    sudo)
      say "sudo がパスワードを求めるようになりました。scripts/update.sh と scripts/setup.sh は、sudo の行で ${TARGET_USER} のパスワードを聞きます（SSH の端末で実行してください）。"
      ;;
    services)
      say "止めて無効にしました。avahi-daemon は、campus-chime.local のために動いたままです。"
      ;;
    journal)
      say "ログを残すようにしました。再起動のあとも journalctl で見られます。"
      ;;
    updates)
      say "セキュリティ更新は、毎朝 5:40〜6:00 ごろに自動で入ります。再起動はしません。カーネルの更新は、放送のない時間（9:55〜17:05 以外）に手で再起動します（security_check.sh が知らせます）。念のため、月に 1 回は sudo apt update && sudo apt full-upgrade と再起動を手でも行ってください。"
      ;;
    service)
      say "放送サービスに保護をかけました。音が出るかは『$(harden_cmd) --test-sound』で確かめられます。"
      ;;
    ntp)
      say "時刻同期を chrony にしました。"
      ;;
  esac
}

step_needs_idle() {
  case "$1" in
    service | updates | ntp) return 0 ;;
    firewall) [ "${FW_INSTALL_NFT}" -eq 1 ] ;;
    *) return 1 ;;
  esac
}

# apt を動かす・時計を変える手順は長引くので、次の放送まで余裕があることも確かめる。
step_needs_lookahead() {
  case "$1" in
    updates | ntp) return 0 ;;
    firewall) [ "${FW_INSTALL_NFT}" -eq 1 ] ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# 適用・巻き戻し・確定
# ---------------------------------------------------------------------------

# 手順を元に戻す。成功したら、確定待ちの記録と控えを片づける。失敗したら、何も片づけない
# （確定待ちと自動復元の時計を残し、もう一度試せるようにする）。
rollback_step() {
  local step="$1"
  if ! step_rollback "${step}"; then
    state_log "rollback ${step} FAILED"
    return 1
  fi
  if pending_has "${step}"; then
    pending_remove "${step}" || { warn "確定待ちの記録を消せません。"; return 1; }
  fi
  cleanup_step_state "${step}" || return 1
  state_log "rollback ${step}"
}

# 手順 $1 の確定した設定（confirmed/）へ戻す。ssh は確定した drop-in を戻して sshd に読み直させ、
# firewall は確定したルールとユニットを戻して、その表を読み込み直す。
confirmed_restore_step() {
  case "$1" in
    ssh) confirmed_restore_ssh ;;
    firewall) confirmed_restore_firewall ;;
    *) return 1 ;;
  esac
}

# 確定していない再適用を取り消す。すでに確定した設定があれば、harden.sh を使う前ではなく、そこへ戻す
# （鍵だけの SSH やファイアウォールを、再適用の失敗で失わないため）。無ければ（初めての適用なら）手順を
# 丸ごと元に戻す。成功したら REVERT_KIND に confirmed か original を入れる。手で --rollback するときは
# これを通さず、いつも丸ごと元に戻す。
REVERT_KIND=""
revert_step() {
  local step="$1"
  REVERT_KIND=""
  if confirmed_has "${step}"; then
    if ! confirmed_restore_step "${step}"; then
      state_log "revert ${step} FAILED"
      return 1
    fi
    if pending_has "${step}"; then
      pending_remove "${step}" || { warn "確定待ちの記録を消せません。"; return 1; }
    fi
    # 再適用の始めに外した「適用済み」の印を、確定した状態に合わせて戻す。
    meta_set "${STATE_DIR}/applied/${step}" "done" 1 || return 1
    REVERT_KIND=confirmed
    state_log "revert ${step} to confirmed"
    return 0
  fi
  rollback_step "${step}" || return 1
  REVERT_KIND=original
}

# 確定待ちが空になったら、自動復元の時計を片づける。
settle_revert() {
  if [ -n "$(pending_list)" ]; then
    return 0
  fi
  if ! disarm_revert; then
    warn "自動復元の時計を片づけられませんでした。systemctl disable --now campus-chime-harden-revert.timer を確かめてください。"
    return 1
  fi
}

# 確定待ちが空なのに、自動復元の単位ファイル・root の写しが残っているのは、時計を張る途中で止められたあと
# （時計を先に張るので、確定待ちの記録より前に止められるとこうなる）か、片づけに失敗したあと。害はないが、
# 変更を始める前に片づける。
settle_orphan_revert() {
  [ "${DRY_RUN}" -eq 0 ] || return 0
  [ -z "$(pending_list)" ] || return 0
  if [ -e "${SYSROOT}${REVERT_TIMER}" ] || [ -e "${SYSROOT}${REVERT_SERVICE}" ] || [ -e "${STATE_DIR}/harden.sh" ]; then
    state_log "settle orphan revert units"
    settle_revert || true
  fi
}

# sudo の手順で NOPASSWD を外したまま止まったときの案内。パスワードが分からないと sudo を使えない。
# （同じ案内を 2 回出さない）
SUDO_ADVICE_SHOWN=0
sudo_password_advice() {
  [ "${SUDO_ADVICE_SHOWN}" -eq 0 ] || return 0
  SUDO_ADVICE_SHOWN=1
  warn "sudo の手順では、${TARGET_USER} のパスワードが分からないと、sudo を付けた操作ができません。パスワードが分からないときは、そのまま ${SUDO_WINDOW} 分待ってください（自動復元の時計が NOPASSWD の設定を戻します）。待てないときは、この Pi のキーボードと画面（コンソール）から root として、bash $(printf '%q' "${SCRIPT_PATH}") --rollback sudo を実行してください。"
}

# 元に戻せなかった手順 $1 の案内。時計が動いていれば、それも伝える。
rollback_failure_advice() {
  local step="$1" window
  window="$(pending_window)"
  if pending_has "${step}"; then
    warn "元に戻せませんでした（手順 ${step}）。自動復元の時計は動いたままなので、${window:-${REVERT_AFTER}} 分以内に自動でもう一度戻します。手で戻すなら $(harden_cmd) --rollback ${step} です。"
  else
    warn "元に戻せませんでした（手順 ${step}）。上の表示を確かめ、$(harden_cmd) --rollback ${step} を実行してください。"
  fi
  if [ "${step}" = sudo ]; then
    sudo_password_advice
  fi
}

# 適用か確認に失敗した（または、止められた）手順を、その場で元に戻す。戻せたかどうかを、そのとおりに伝える。
undo_step() {
  local step="$1" reason="$2"
  warn "手順 ${step} は適用できませんでした（${reason}）。元に戻します。"
  ROLLBACK_IDLE=0
  if revert_step "${step}"; then
    settle_revert || true
    if [ "${REVERT_KIND}" = confirmed ]; then
      ok "元に戻しました（手順 ${step}。以前に確定した設定に戻しました）。"
    else
      ok "元に戻しました（手順 ${step}）。"
    fi
    return 0
  fi
  rollback_failure_advice "${step}"
  return 1
}

run_step() {
  local step="$1"
  CURRENT_STEP="${step}"
  log "適用: ${step} — $(step_title "${step}")"
  if [ "${DRY_RUN}" -eq 0 ]; then
    if ! begin_apply "${step}"; then
      warn "状態の記録を書けません（${STATE_REAL}）。"
      undo_step "${step}" "状態を記録できない" || true
      CURRENT_STEP=""
      return 1
    fi
  fi
  if [ "${DRY_RUN}" -eq 0 ]; then
    # もう一度適用するので、「自動で元に戻った」記録は古くなる。
    reverted_forget "${step}" || true
  fi
  if ! step_apply "${step}"; then
    undo_step "${step}" "変更の途中で失敗した" || true
    CURRENT_STEP=""
    return 1
  fi
  if ! step_verify "${step}"; then
    undo_step "${step}" "効いたことを確かめられなかった" || true
    CURRENT_STEP=""
    return 1
  fi
  if [ "${DRY_RUN}" -eq 0 ]; then
    if ! finish_apply "${step}"; then
      warn "適用済みの記録を書けません。"
      undo_step "${step}" "状態を記録できない" || true
      CURRENT_STEP=""
      return 1
    fi
    state_log "applied ${step}"
  fi
  if [ "${DRY_RUN}" -eq 0 ]; then
    step_report "${step}"
  fi
  CURRENT_STEP=""
}

print_pending_notice() {
  local list=() others=() step window
  mapfile -t list < <(pending_list)
  [ "${#list[@]}" -gt 0 ] || return 0
  window="$(pending_window)"
  window="${window:-${REVERT_AFTER}}"
  for step in "${list[@]}"; do
    if [ "${step}" != sudo ]; then
      others+=("${step}")
    fi
  done
  if [ "${#others[@]}" -lt "${#list[@]}" ]; then
    log "sudo のパスワードの確認が終わっていません"
    say "NOPASSWD を外したまま、パスワードの確認が終わっていません。${window} 分以内に、自動で元に戻ります（確定の操作は要りません）。"
    say "  ${TARGET_USER} のパスワードが分かるなら、いま戻せます: $(harden_cmd) --rollback sudo"
    say "  分からないなら、そのまま待ってください（戻るまで sudo は使えません）。"
  fi
  [ "${#others[@]}" -gt 0 ] || return 0
  log "次にすること: 別の端末で確かめてから、確定する"
  say "確定待ちの変更: ${others[*]}"
  say "このままだと ${window} 分で自動で元に戻ります（確定する前に再起動しても、起動の ${window} 分後に戻ります）。"
  say "  1. この画面は閉じずに、新しい端末（別の画面）を開く。"
  say "  2. 鍵で入れることを確かめる:"
  say "       $(ssh_login_cmd)"
  say "  3. （ssh のとき）パスワードでは入れないことを確かめる:"
  say "       $(ssh_login_cmd -o PubkeyAuthentication=no -o PreferredAuthentications=password)"
  say "     『Permission denied (publickey)』と出れば正しい動きです。"
  say "  4. その新しい端末で、確定する:"
  say "       $(harden_cmd) --confirm"
  say "確定しないで元に戻すなら: $(harden_cmd) --rollback-pending"
}

run_apply() {
  local step ordered=() s want skipped_all=1 need_idle ahead
  if [ "${DRY_RUN}" -eq 0 ]; then
    require_root
  fi
  load_user
  for s in "${APPLY_ORDER[@]}"; do
    for want in "${STEPS[@]}"; do
      if [ "${s}" = "${want}" ]; then
        ordered+=("${s}")
      fi
    done
  done
  if [ "${DRY_RUN}" -eq 0 ]; then
    acquire_lock 0
    log "事前の確認（ここでは何も変更しません）"
  else
    log "事前の確認と、実行する内容の表示（--dry-run: 何も変更しません）"
    if [ "${IS_ROOT}" -eq 0 ]; then
      say "root ではないので、root でないと読めない項目は確かめられません（実際の実行では確かめます）。"
    fi
  fi
  PRE_FAIL=0
  check_os
  for step in "${ordered[@]}"; do
    step_preflight "${step}"
  done
  if [ "${PRE_FAIL}" -gt 0 ]; then
    if [ "${DRY_RUN}" -eq 1 ]; then
      fail "条件を満たさないものが ${PRE_FAIL} 件あります（上の NG）。実際に実行しても、ここで止まります。何も変更していません。"
    fi
    fail "条件を満たさないものが ${PRE_FAIL} 件あります（上の NG）。直してから、もう一度実行してください。何も変更していません。"
  fi
  for step in "${ordered[@]}"; do
    if [ -z "${STEP_SKIP[${step}]:-}" ]; then
      skipped_all=0
    fi
  done
  if [ "${DRY_RUN}" -eq 0 ] && [ "${skipped_all}" -eq 0 ]; then
    need_idle=0
    ahead=0
    for step in "${ordered[@]}"; do
      [ -z "${STEP_SKIP[${step}]:-}" ] || continue
      if step_needs_idle "${step}"; then
        need_idle=1
      fi
      if step_needs_lookahead "${step}"; then
        ahead="${LOOKAHEAD_MINUTES}"
      fi
    done
    if [ "${need_idle}" -eq 1 ]; then
      ensure_idle "${ahead}" || fail "放送の最中、または放送の直前でないことを確認できませんでした。何も変更していません。"
    fi
  fi
  if [ "${DRY_RUN}" -eq 0 ] && [ "${skipped_all}" -eq 0 ]; then
    settle_orphan_revert
  fi
  for step in "${ordered[@]}"; do
    if [ -n "${STEP_SKIP[${step}]:-}" ]; then
      log "${step}: ${STEP_SKIP[${step}]}"
      continue
    fi
    if ! run_step "${step}"; then
      print_pending_notice
      fail "手順 ${step} を適用できませんでした。ここで止めます（前の手順で適用したものは、そのまま残っています。一覧は $(harden_cmd)）。"
    fi
  done
  if [ "${DRY_RUN}" -eq 1 ]; then
    log "以上が実行する内容です（何も変更していません）"
    return 0
  fi
  print_pending_notice
  if [ "${TEST_SOUND}" -eq 1 ]; then
    run_test_sound || fail "音の確認に失敗しました。"
  fi
  log "完了"
}

# 起動した画面が、確定待ちの変更より前に開かれたものかを調べる。sshd のセッションの子孫なら、
# そのセッションを開いた時刻（エポック秒）を表示する。見つからなければ 1（コンソールや、
# tmux / screen の中などで、sshd の下に見えない場合を含む）。
session_start_epoch() {
  local pid=$$ depth=0 name ppid key value stat_line rest fields=() ticks btime tck
  while [ "${pid}" -gt 1 ] && [ "${depth}" -lt 64 ]; do
    [ -r "${PROC_DIR}/${pid}/status" ] || return 1
    name=""
    ppid=""
    while read -r key value _; do
      case "${key}" in
        Name:) name="${value}" ;;
        PPid:) ppid="${value}" ;;
      esac
    done < "${PROC_DIR}/${pid}/status"
    [ -n "${ppid}" ] || return 1
    # 待ち受けの親（PID 1 の子の sshd）ではなく、接続ごとの子プロセスを探す。
    if { [ "${name}" = sshd ] || [ "${name}" = sshd-session ]; } && [ "${ppid}" != 1 ]; then
      stat_line="$(cat "${PROC_DIR}/${pid}/stat" 2>/dev/null)" || return 1
      rest="${stat_line##*) }"
      read -r -a fields <<< "${rest}"
      ticks="${fields[19]:-}"
      [ -n "${ticks}" ] || return 1
      btime=""
      while read -r key value _; do
        if [ "${key}" = btime ]; then
          btime="${value}"
        fi
      done < "${PROC_DIR}/stat"
      tck="$(getconf CLK_TCK 2>/dev/null)" || return 1
      if [ -z "${btime}" ] || [ -z "${tck}" ]; then
        return 1
      fi
      printf '%s' $((btime + ticks / tck))
      return 0
    fi
    pid="${ppid}"
    depth=$((depth + 1))
  done
  return 1
}

# 確定を、変更より前に開いた画面から行おうとしていないか。目安であり、tmux / screen の中などでは
# 見分けられない。--force で飛ばせる。
check_session_age() {
  local latest="$1" start
  start="$(session_start_epoch)" || return 0
  if [ "${start}" -lt "${latest}" ]; then
    warn "この画面は、変更の前に開いたものです（SSH のログインが ${start}、変更が ${latest}）。この画面が生きているだけでは、変更後も入れることの確認になりません。新しい画面を開いて、そこから --confirm を実行してください。"
    warn "この見分けは目安です（tmux や screen の中では見分けられません）。確認済みなら --force で確定できます。"
    return 1
  fi
}

# 自動復元が元に戻した手順の記録（REVERTED_FILE）を、そのまま伝える。
report_reverted() {
  local steps=() step kind epoch when=""
  mapfile -t steps < <(reverted_steps)
  [ "${#steps[@]}" -gt 0 ] || return 0
  epoch="$(meta_get "${REVERTED_FILE}" epoch 2>/dev/null || true)"
  if [ -n "${epoch}" ]; then
    when="$(date -d "@${epoch}" '+%F %T' 2>/dev/null || true)"
  fi
  warn "確定されないまま自動復元（--rollback-pending）が働いて、次の変更はすでに元に戻されています${when:+（${when}）}。"
  for step in "${steps[@]}"; do
    kind="$(meta_get "${REVERTED_FILE}" "${step}" 2>/dev/null || true)"
    if [ "${kind}" = confirmed ]; then
      warn "  ${step}: 最後に確定した設定に戻りました（今回の変更は効いていません）。"
    else
      warn "  ${step}: harden.sh を使う前の状態に戻りました（守りは効いていません）。"
    fi
  done
  warn "もう一度適用するなら: $(harden_cmd) ${steps[*]}（ssh・firewall は、適用したあとの案内のとおり別の端末で確かめて、${REVERT_AFTER} 分以内に $(harden_cmd) --confirm）。firewall に --ssh-from を付けていたなら、同じ指定を付けます。"
}

run_confirm() {
  local step latest=0 epoch failed=0 unconfirmed=0 rc steps=()
  require_root
  acquire_lock 0
  if [ -z "$(pending_list)" ]; then
    settle_revert || exit 1
    if [ -n "$(reverted_steps)" ]; then
      warn "確定する変更はありません。ただし、これは確定できたからではありません。"
      report_reverted
      exit 1
    fi
    say "確定する変更はありません。"
    return 0
  fi
  mapfile -t steps < <(pending_list)
  for step in "${steps[@]}"; do
    epoch="$(pending_epoch "${step}")"
    if [ -n "${epoch}" ] && [ "${epoch}" -gt "${latest}" ]; then
      latest="${epoch}"
    fi
  done
  if [ "${FORCE}" -eq 0 ]; then
    check_session_age "${latest}" || fail "確定していません。確定待ちの変更は、時間が来ると自動で元に戻ります。"
  fi
  for step in "${steps[@]}"; do
    case "${step}" in
      firewall)
        rc=0
        persist_firewall || rc=$?
        if [ "${rc}" -eq 0 ]; then
          # 確定した版を控えてから、確定待ちを外す（控えられなければ、確定待ちを残す）。
          if snapshot_confirmed firewall "${FW_NFT_FILE}" "${FW_UNIT}" && pending_remove firewall; then
            say "防火壁を確定しました（再起動後も有効です: campus-chime-firewall.service）。"
          else
            warn "防火壁の確定を記録できませんでした。"
            failed=1
          fi
        elif [ "${rc}" -eq 2 ]; then
          unconfirmed=1
          ROLLBACK_IDLE=0
          if confirmed_has firewall; then
            if revert_step firewall; then
              warn "新しい防火壁は確定しませんでした。以前に確定した防火壁に戻してあります。入れ直して（$(harden_cmd) firewall）から、もう一度確定してください。"
            else
              failed=1
            fi
          else
            warn "防火壁は確定しませんでした。入れ直して（$(harden_cmd) firewall）から、もう一度確定してください。"
            rollback_step firewall || failed=1
          fi
        else
          failed=1
        fi
        ;;
      ssh)
        if ! step_done ssh; then
          warn "SSH の適用が最後まで終わっていません（途中で止められたようです）。設定を確かめ終えていないので、確定しません。"
          unconfirmed=1
          ROLLBACK_IDLE=0
          if confirmed_has ssh; then
            if revert_step ssh; then
              warn "新しい SSH の設定は確定しませんでした。以前に確定した設定に戻してあります。もう一度適用して（$(harden_cmd) ssh）から、確定してください。"
            else
              failed=1
            fi
          else
            warn "SSH の鍵だけの設定は確定しませんでした。もう一度適用して（$(harden_cmd) ssh）から、確定してください。"
            rollback_step ssh || failed=1
          fi
        elif snapshot_confirmed ssh "${SSHD_DROPIN}" && pending_remove ssh; then
          say "SSH の鍵だけの設定を確定しました。"
        else
          warn "SSH の確定を記録できませんでした。"
          failed=1
        fi
        ;;
      sudo)
        # sudo は確定の操作で確定するものではない（パスワードの確認が終わると自動で確定待ちが外れる）。
        # 残っているのは、確認が終わる前に止まったということ。
        warn "手順 sudo は、パスワードの確認が終わっていません（途中で止まったようです）。--confirm では確定できません。$(pending_window) 分以内に自動で元に戻ります。いま戻すなら $(harden_cmd) --rollback-pending です。"
        unconfirmed=1
        ;;
    esac
  done
  if [ "${failed}" -eq 1 ]; then
    fail "確定できなかったものがあります。確定待ちと自動復元の時計は残してあります（時間が来ると元に戻ります）。上の表示を確かめてください。"
  fi
  settle_revert || fail "確定しましたが、自動復元の時計を止められませんでした。時間が来ても、確定済みの変更は元に戻りません（確定待ちは空です）。"
  if [ "${unconfirmed}" -eq 1 ]; then
    fail "確定できなかった手順があります（上の警告）。確定できたものだけが確定しています。"
  fi
  if [ -n "$(reverted_steps)" ]; then
    warn "なお、別の手順がすでに自動で元に戻されています。"
    report_reverted
  fi
  log "確定しました"
  say "状態の確認: $(harden_cmd)（一覧）、bash $(printf '%q' "${REPO_DIR}/scripts/security_check.sh")（点検）"
}

# 手順を指定された順に（指定がなければ、適用したものを新しい順に）元に戻す。
run_rollback() {
  local step failed=() targets=() touched
  require_root
  load_user_for_rollback
  acquire_lock 0
  if [ "${#STEPS[@]}" -gt 0 ]; then
    targets=("${STEPS[@]}")
  else
    rollback_order
    targets=("${ROLLBACK_LIST[@]}")
    # 手順を指定しない --rollback は、すべてを手で戻す操作。もう触っていない手順の「自動で元に戻った」記録も消す。
    for step in "${ALL_STEPS[@]}"; do
      reverted_forget "${step}" || true
    done
  fi
  ROLLBACK_IDLE=1
  SERVICE_RESTARTED=1
  for step in "${targets[@]}"; do
    # 手で戻すなら、「自動で元に戻った」という記録は要らない（戻すものが無くても消す）。
    reverted_forget "${step}" || true
    touched=0
    step_touched "${step}" && touched=1
    pending_has "${step}" && touched=1
    if [ "${touched}" -eq 0 ]; then
      say "手順 ${step}: 適用されていません。何もしません。"
      continue
    fi
    log "元に戻す: ${step}"
    CURRENT_STEP="${step}"
    if rollback_step "${step}"; then
      ok "元に戻しました（手順 ${step}）。"
    else
      warn "元に戻せませんでした（手順 ${step}）。状態と控えは残してあります。上の表示を直して、もう一度 --rollback ${step} を実行してください。"
      failed+=("${step}")
    fi
    CURRENT_STEP=""
  done
  settle_revert || failed+=("revert-timer")
  if [ "${#failed[@]}" -gt 0 ]; then
    fail "元に戻せなかったものがあります: ${failed[*]}"
  fi
  if [ "${#targets[@]}" -eq 0 ]; then
    say "元に戻すものはありません。"
  fi
}

# 適用した手順を新しい順に ROLLBACK_LIST へ入れる（開始時刻の新しい順。同じ秒なら、後で適用する順の逆）。
rollback_order() {
  local i best best_epoch epoch step used=" " count=0
  ROLLBACK_LIST=()
  for step in "${ALL_STEPS[@]}"; do
    if step_touched "${step}" || pending_has "${step}"; then
      count=$((count + 1))
    fi
  done
  while [ "${#ROLLBACK_LIST[@]}" -lt "${count}" ]; do
    best=""
    best_epoch=-1
    for ((i = ${#APPLY_ORDER[@]} - 1; i >= 0; i--)); do
      step="${APPLY_ORDER[i]}"
      [[ "${used}" != *" ${step} "* ]] || continue
      step_touched "${step}" || pending_has "${step}" || continue
      epoch="$(meta_get "${STATE_DIR}/applied/${step}" first_epoch 2>/dev/null || true)"
      [ -n "${epoch}" ] || epoch=0
      if [ "${epoch}" -gt "${best_epoch}" ]; then
        best="${step}"
        best_epoch="${epoch}"
      fi
    done
    [ -n "${best}" ] || break
    ROLLBACK_LIST+=("${best}")
    used+="${best} "
  done
}

# 自動復元の時計が呼ぶ。確定待ちの手順だけを元に戻す。放送の確認はしない（ssh と firewall の
# 戻しは放送に触れない）。ほかの harden.sh が動いていても、最大 30 分待って、必ずやり切る。
# 1 つでも戻せなければ失敗で終わる（時計の単位が Restart=on-failure で 60 秒後にやり直す）。
run_rollback_pending() {
  local steps=() step i failed=() rc=0 done_steps=() item gen_start gen_now
  require_root
  # 起きたときの時計の番号を覚えてから、ロックを待つ。
  gen_start="$(revert_gen)"
  acquire_lock 1800
  gen_now="$(revert_gen)"
  if [ -n "${gen_start}" ] && [ -n "${gen_now}" ] && [ "${gen_start}" != "${gen_now}" ]; then
    # ロックを待つ間に、ほかの harden.sh が自動復元の時計を張り直した（窓を数え直した）。新しい時計が、新しい窓の
    # 終わりに戻すので、古い時計のこの実行は、確定待ちの変更に触らない。
    say "自動復元の時計は、待っている間に張り直されました。新しい時計が戻すので、この実行は何もしません。"
    state_log "rollback-pending skipped (revert timer re-armed: ${gen_start} -> ${gen_now})"
    return 0
  fi
  ROLLBACK_IDLE=0
  mapfile -t steps < <(pending_list)
  if [ "${#steps[@]}" -eq 0 ]; then
    say "確定待ちの変更はありません。"
    settle_revert || exit 1
    return 0
  fi
  log "確定されなかった変更を元に戻します: ${steps[*]}"
  state_log "rollback-pending ${steps[*]}"
  # firewall のほうが締め出しの原因になりやすいので、後ろ（新しい順）から戻す。
  for ((i = ${#steps[@]} - 1; i >= 0; i--)); do
    step="${steps[i]}"
    CURRENT_STEP="${step}"
    if revert_step "${step}"; then
      if [ "${REVERT_KIND}" = confirmed ]; then
        ok "元に戻しました（手順 ${step}。以前に確定した設定に戻しました）。"
      else
        ok "元に戻しました（手順 ${step}）。"
      fi
      done_steps+=("${step}:${REVERT_KIND}")
    else
      warn "元に戻せませんでした（手順 ${step}）。確定待ちと自動復元の時計を残して、やり直します。"
      if [ "${step}" = sudo ]; then
        sudo_password_advice
      fi
      failed+=("${step}")
      rc=1
    fi
  done
  CURRENT_STEP=""
  # 戻した手順を記録する（あとで --confirm を実行した人に、すでに戻っていると伝えるため）。
  for item in "${done_steps[@]}"; do
    reverted_record "${item%%:*}" "${item#*:}" || warn "自動で元に戻したことの記録を書けません。"
  done
  if [ "${rc}" -ne 0 ]; then
    fail "元に戻せなかった手順があります: ${failed[*]}"
  fi
  settle_revert || exit 1
  log "確定待ちの変更をすべて元に戻しました"
}

run_list() {
  local step state label
  require_root
  log "手順の一覧"
  for step in "${ALL_STEPS[@]}"; do
    state="$(step_state "${step}")"
    case "${state}" in
      applied) label="適用済み" ;;
      pending) label="確定待ち" ;;
      partial) label="途中（--rollback で戻せます）" ;;
      *) label="未適用" ;;
    esac
    printf '  %-9s %-10s %s\n' "${step}" "${label}" "$(step_title "${step}")"
  done
  print_pending_notice
  if [ -n "$(reverted_steps)" ]; then
    log "自動で元に戻された変更があります"
    report_reverted
  fi
  list_check_service_dirs
  say ""
  say "使い方: $(harden_cmd) STEP...（--dry-run で内容だけ確認、--rollback で戻す、-h で説明）"
}

# service の手順の保護の下で書き込みを許したディレクトリが、いまも全部あるか。無いディレクトリは、保護の下では
# サービスが作れない（再クローンや git clean、cache/ の削除のあとなど）。
list_check_service_dirs() {
  local dropin="${SYSROOT}${SERVICE_DROPIN}" line paths path missing=() quoted=""
  [ -f "${dropin}" ] || return 0
  while IFS= read -r line || [ -n "${line}" ]; do
    [[ "${line}" == ReadWritePaths=* ]] || continue
    paths="${line#ReadWritePaths=}"
    for path in ${paths}; do
      path="${path#-}"
      path="${path//\%\%/%}"
      [ -d "${path}" ] || missing+=("${path}")
    done
  done < "${dropin}"
  if [ "${#missing[@]}" -gt 0 ]; then
    quoted="$(quote_cmd "${missing[@]}")"
    warn "保護の下で書き込みを許したディレクトリが無くなっています: ${missing[*]}。サービスは保護の下では作れないので、状態や時報音を保存できません。${TARGET_USER} として作ってください（sudo -u ${TARGET_USER} mkdir -p ${quoted}）。"
  fi
}

require_root() {
  if [ "${IS_ROOT}" -ne 1 ]; then
    fail "sudo を付けて実行してください（例: $(harden_cmd) ssh）。何も変更していません。"
  fi
}

# 途中で止まったとき、実際に起きたことだけを伝える。
on_exit() {
  local rc=$?
  trap - EXIT
  if [ "${rc}" -ne 0 ] && [ -n "${CURRENT_STEP}" ] && [ "${DRY_RUN}" -eq 0 ]; then
    warn "手順 ${CURRENT_STEP} の途中で止まりました。状態は『$(harden_cmd)』で確かめられ、$(harden_cmd) --rollback ${CURRENT_STEP} で戻せます。"
    if [ "${CURRENT_STEP}" = sudo ]; then
      sudo_password_advice
    elif [ -n "$(pending_list 2>/dev/null || true)" ]; then
      warn "確定待ちの変更には自動復元の時計が動いています。確定しなければ、時間が来ると元に戻ります。"
    fi
  fi
  exit "${rc}"
}

# Ctrl-C・回線切れ（SIGHUP）・SIGTERM。適用の途中なら、確認に失敗したときと同じ道（undo_step）で、その手順を
# 元に戻してから終わる（sudo なら NOPASSWD の設定が戻る）。戻している間は、重ねて来る信号は無視する。
# 端末が消えていても（回線切れ）戻せるよう、画面への出力の失敗は無視する。
on_signal() {
  local name="$1" code="$2" step="${CURRENT_STEP}"
  trap '' INT TERM HUP PIPE
  set +e
  printf '\n' >&2 || true
  warn "${name} を受け取ったので、途中で止めます。" || true
  if [ -n "${step}" ] && [ "${MODE}" = apply ] && [ "${DRY_RUN}" -eq 0 ]; then
    if undo_step "${step}" "中断された"; then
      CURRENT_STEP=""
    fi
  fi
  exit "${code}"
}

# ---------------------------------------------------------------------------
# 引数
# ---------------------------------------------------------------------------

set_mode() {
  if [ "${MODE}" != apply ] && [ "${MODE}" != "$1" ]; then
    usage_error "--confirm・--rollback・--rollback-pending は同時に使えません。"
  fi
  MODE="$1"
}

parse_args() {
  local arg step seen_steps=" "
  while [ $# -gt 0 ]; do
    arg="$1"
    case "${arg}" in
      # 先頭のコメントブロック（2 行目から、最初のコメントでない行の手前まで）を表示する。
      -h|--help) sed -n '2,/^[^#]/{/^[^#]/!p}' "$0"; exit 0 ;;
      --confirm) set_mode confirm ;;
      --rollback) set_mode rollback ;;
      --rollback-pending) set_mode rollback-pending ;;
      --dry-run) DRY_RUN=1 ;;
      --force) FORCE=1 ;;
      --test-sound) TEST_SOUND=1 ;;
      --user)
        [ $# -ge 2 ] || usage_error "--user には利用者名が必要です。"
        TARGET_USER="$2"
        shift
        ;;
      --revert-after)
        [ $# -ge 2 ] || usage_error "--revert-after には分数（1〜60）が必要です。"
        REVERT_AFTER="$2"
        shift
        ;;
      --ssh-from)
        [ $# -ge 2 ] || usage_error "--ssh-from には CIDR（例: 192.168.1.0/24）が必要です。"
        SSH_FROM+=("$2")
        shift
        ;;
      --ntp-server)
        [ $# -ge 2 ] || usage_error "--ntp-server にはホスト名が必要です。"
        NTP_SERVERS+=("$2")
        shift
        ;;
      ssh | firewall | sudo | services | journal | updates | service | ntp) STEPS+=("${arg}") ;;
      recommended) STEPS+=("${RECOMMENDED_STEPS[@]}") ;;
      -*) usage_error "不明なオプション: ${arg}（使い方は bash scripts/harden.sh --help）" ;;
      *) usage_error "不明な手順: ${arg}（ssh firewall sudo services journal updates service ntp recommended のどれかです）" ;;
    esac
    shift
  done

  # 同じ手順が重なったら 1 つにする。
  local unique=()
  for step in "${STEPS[@]}"; do
    if [[ "${seen_steps}" != *" ${step} "* ]]; then
      unique+=("${step}")
      seen_steps+="${step} "
    fi
  done
  STEPS=("${unique[@]}")

  if [[ ! "${TARGET_USER}" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    usage_error "--user の値 ${TARGET_USER} は、利用者名として読めません。"
  fi
  if [ "${TARGET_USER}" = root ]; then
    usage_error "--user root は使えません。サービスと SSH の利用者（通常は pi）を指定してください。"
  fi
  if [[ ! "${REVERT_AFTER}" =~ ^[0-9]{1,2}$ ]] || [ "$((10#${REVERT_AFTER}))" -lt 1 ] || [ "$((10#${REVERT_AFTER}))" -gt 60 ]; then
    usage_error "--revert-after は 1〜60 の整数（分）で指定してください（指定: ${REVERT_AFTER}）。"
  fi
  REVERT_AFTER="$((10#${REVERT_AFTER}))"

  case "${MODE}" in
    confirm | rollback-pending)
      [ "${#STEPS[@]}" -eq 0 ] || usage_error "--${MODE} には手順を付けません。"
      [ "${DRY_RUN}" -eq 0 ] || usage_error "--dry-run は手順の適用だけに使えます。"
      [ "${TEST_SOUND}" -eq 0 ] || usage_error "--test-sound は --${MODE} と一緒には使えません。"
      ;;
    rollback)
      [ "${DRY_RUN}" -eq 0 ] || usage_error "--dry-run は手順の適用だけに使えます。"
      [ "${TEST_SOUND}" -eq 0 ] || usage_error "--test-sound は --rollback と一緒には使えません。"
      ;;
    apply)
      if [ "${#STEPS[@]}" -eq 0 ]; then
        if [ "${DRY_RUN}" -eq 1 ]; then
          # --dry-run --test-sound も同じ: 音を鳴らす確認は、何も変えない試しにはならない。
          usage_error "--dry-run には手順が必要です（例: bash scripts/harden.sh --dry-run ssh）。"
        elif [ "${TEST_SOUND}" -eq 1 ]; then
          MODE=test-sound
        else
          MODE=list
        fi
      fi
      ;;
  esac
  if [ "${TEST_SOUND}" -eq 1 ] && [ "${MODE}" = apply ]; then
    [[ " ${STEPS[*]} " == *" service "* ]] || usage_error "--test-sound は、手順 service と一緒に、または単独で使います。"
  fi
  if [ "${#SSH_FROM[@]}" -gt 0 ] && [[ " ${STEPS[*]} " != *" firewall "* ]]; then
    usage_error "--ssh-from は、手順 firewall と一緒に使います。"
  fi
  if [ "${#NTP_SERVERS[@]}" -gt 0 ] && [[ " ${STEPS[*]} " != *" ntp "* ]]; then
    usage_error "--ntp-server は、手順 ntp と一緒に使います。"
  fi
}

main() {
  parse_args "$@"
  # python3 などを動かす作業フォルダは / にする（pi が書けるリポジトリの中で、root が動かさないため）。
  cd /
  if [ "$(id -u)" -eq 0 ]; then
    IS_ROOT=1
  fi
  trap on_exit EXIT
  trap 'on_signal INT 130' INT
  trap 'on_signal TERM 143' TERM
  trap 'on_signal HUP 129' HUP
  case "${MODE}" in
    list) run_list ;;
    apply) run_apply ;;
    confirm) run_confirm ;;
    rollback) run_rollback ;;
    rollback-pending) run_rollback_pending ;;
    test-sound)
      require_root
      load_user
      if [ ! -f "${SYSROOT}${SERVICE_DROPIN}" ]; then
        fail "保護の設定（${SERVICE_DROPIN}）がありません。先に $(harden_cmd) service を実行するか、service と一緒に --test-sound を付けてください。何も変更していません。"
      fi
      acquire_lock 0
      run_test_sound
      ;;
  esac
}

main "$@"
