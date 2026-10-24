#!/usr/bin/env bash
#
# 時報機（Raspberry Pi）の安全面を、読むだけで点検する。何も変更しない。
# ネットワーク（学外から届きうるか）・SSH・sudo・防火壁・OS とカーネルの更新・時刻の同期・
# 使っていないサービス・時報サービスの隔離・リポジトリの状態を調べ、項目ごとに
# [OK]・[情報]・[注意]・[要対応]・[不明] を表示する。[注意] と [要対応] には、直し方
# （scripts/harden.sh の手順の名前）を添える。
#
#   bash scripts/security_check.sh                # pi ユーザーで実行（root が要る項目は [不明] と案内が出る）
#   sudo bash scripts/security_check.sh           # root で実行（すべての項目を調べる）
#   bash scripts/security_check.sh --user NAME    # サービスと管理に使うユーザー（既定は pi）
#   bash scripts/security_check.sh --help         # この説明を表示する
#
# ファイルは書かない（一時ファイルも作らない）。apt の索引の作り直しもしない（apt を使うときは、
# 索引の写し pkgcache.bin・srcpkgcache.bin の保存先を空にして、root でも何も書かせない）。
# サービスにも触れない。root でないときに使う sudo は、設定を一覧するだけの sudo -n -l
# （パスワードは聞かない）だけ。
# 終了コードは、[要対応] が無ければ 0、1 件以上あれば 1、使い方の間違いは 2。
#
# この点検は「Pi には学内のネットワークからだけ届く（ポート転送はしていない）」という
# 前提で危険度を決めている。大学が持つ IPv4 アドレスの範囲（クラス B/C）の中では、Pi が
# グローバルアドレスを持つことがある。その場合に学外から届くかどうかは大学の防火壁しだいで、
# この点検では確かめられない（見つけたアドレスは、学外から確かめる方法と一緒に表示する）。
#
set -euo pipefail

# 表示と判定を英語の語句に固定する（コマンドの表示を読むため）。
export LC_ALL=C

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="campus_chime.service"
# テスト用: システムの絶対パスはすべてこの接頭辞の下から読む（実機では空）。
SYSROOT="${CHIME_SYSROOT:-}"
SYSROOT="${SYSROOT%/}"
USER_NAME="pi"

# 配布されるカーネルに、CISA KEV（悪用が確認された脆弱性の一覧）に載ったカーネルの権限昇格
# （CVE-2026-31431・CVE-2026-53362・CVE-2026-53266・CVE-2025-39964・CVE-2025-39682・
# CVE-2025-38352）の修正が入った日の目安。CISA KEV カタログ 2026.10.21 版と、CVE-2026-53362 の
# GHSA の公開日（2026-07-04）から決めた。この日より前に作られたカーネルは、未修正とみなす。
# 新しい脆弱性が載ったら、この日付を新しくする。
KERNEL_FIXES_SINCE="2026-07-04"
# apt の更新がこの日数ないときは [注意]。
STALE_DAYS=45
# 再起動せずに動き続けているのがこの日数を超えたら [注意]（新しいカーネルに入れ替わらない）。
UPTIME_WARN_DAYS=60

usage_error() { printf '%s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    # 先頭のコメントブロック（2 行目から、最初のコメントでない行の手前まで）を表示する。
    -h|--help) sed -n '2,/^[^#]/{/^[^#]/!p}' "$0"; exit 0 ;;
    --user)
      [ $# -ge 2 ] || usage_error "--user にはユーザー名を続けてください（例: --user pi）"
      USER_NAME="$2"
      shift 2
      ;;
    --user=*)
      USER_NAME="${1#--user=}"
      shift
      ;;
    *) usage_error "不明なオプション: $1" ;;
  esac
done
if [[ ! "${USER_NAME}" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  usage_error "ユーザー名として使えない文字があります: ${USER_NAME}"
fi

# -- 表示 ------------------------------------------------------------------------
# 端末に出すときだけ色を付ける（パイプやファイルへは付けない）。
C_RESET=""; C_BOLD=""; C_GREEN=""; C_CYAN=""; C_YELLOW=""; C_RED=""; C_MAGENTA=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_CYAN=$'\033[36m'
  C_YELLOW=$'\033[33m'; C_RED=$'\033[1;31m'; C_MAGENTA=$'\033[35m'
fi

N_NG=0
N_WARN=0
N_UNK=0

log() { printf '\n%s== %s%s\n' "${C_BOLD}" "$*" "${C_RESET}"; }

# 1 件の結果を表示する: emit ラベル 分類 説明。ラベルは OK・情報・注意・要対応・不明。
emit() {
  local label=$1 category=$2 text=$3 color=""
  case "${label}" in
    OK) color="${C_GREEN}" ;;
    情報) color="${C_CYAN}" ;;
    注意) color="${C_YELLOW}"; N_WARN=$((N_WARN + 1)) ;;
    要対応) color="${C_RED}"; N_NG=$((N_NG + 1)) ;;
    不明) color="${C_MAGENTA}"; N_UNK=$((N_UNK + 1)) ;;
  esac
  printf '%s[%s]%s %s: %s\n' "${color}" "${label}" "${C_RESET}" "${category}" "${text}"
}
# 直し方（emit の直後に 1 回）と、その続きの説明（何行でも）。
fixline() { printf '    → 直し方: %s\n' "$*"; }
more() { printf '      %s\n' "$*"; }
# root でないと調べられない項目。
need_root() {
  emit 不明 "$1" "root でないため調べられません"
  printf '    → 確かめ方: sudo bash scripts/security_check.sh\n'
}

# -- 道具 ------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

# apt の道具（apt-get・apt-cache・apt-config）は、必ずこれを通して呼ぶ: apt_read 道具名 引数...
# root で apt を動かすと、索引の写し（/var/cache/apt の pkgcache.bin・srcpkgcache.bin）を作り直して
# しまうので、その保存先を空にして、何も書かせない（索引はそのつど、メモリの中に作る）。
apt_read() {
  local tool=$1
  shift
  "${tool}" -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= "$@"
}

trim() {
  local s=$1
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "${s}"
}

# 「、」でつないで 1 つの文字列にする: join_ja 項目...
join_ja() {
  local out="" item
  for item in "$@"; do
    if [ -z "${out}" ]; then out="${item}"; else out="${out}、${item}"; fi
  done
  printf '%s' "${out}"
}

# 日数: now_epoch から epoch を引いた日の数（切り捨て）。
days_since() { printf '%s' "$(( ($(date +%s) - $1) / 86400 ))"; }

UID_NOW=""
CUR_USER=""
USER_HOME=""
USER_UID=""
USER_GROUPS=""
USER_KNOWN=1
KERNEL_RELEASE=""
KERNEL_VERSION=""
OS_CODENAME=""
GLOBAL_FOUND=0
GLOBAL_ADDRS=()
SSH_ON=1
SSH_PW_YES=0
AVAHI_ACTIVE=0
CHRONY_ACTIVE=0
TIMESYNCD_ACTIVE=0
JOURNAL_TEXT=""

is_root() { [ "${UID_NOW}" = "0" ]; }

# 調べるサービスの動作状態を、systemctl is-active に 1 回でまとめて尋ねる（1 つに 1 行で答える）。
# 答えの行数が合わなければ、どれも分からなかったことにする。答えは UNIT_ACTIVE[ユニット名] に入る。
declare -A UNIT_ACTIVE=()
UNITS_ASKED=(ssh.service ssh.socket avahi-daemon.service chrony.service systemd-timesyncd.service
             bluetooth.service hciuart.service ModemManager.service)
load_unit_states() {
  local answers="" state index=0
  local -a states=()
  have systemctl || return 0
  answers="$(systemctl is-active "${UNITS_ASKED[@]}" 2>/dev/null)" || true
  while IFS= read -r state; do
    states+=("${state}")
  done < <(printf '%s\n' "${answers}")
  [ "${#states[@]}" -eq "${#UNITS_ASKED[@]}" ] || return 0
  for state in "${states[@]}"; do
    UNIT_ACTIVE[${UNITS_ASKED[${index}]}]="${state}"
    index=$((index + 1))
  done
  return 0
}
# 動作状態（取得できていなければ空）。
unit_state() { printf '%s' "${UNIT_ACTIVE[$1]-}"; }
unit_enabled_state() {
  local s=""
  if have systemctl; then
    s="$(systemctl is-enabled "$1" 2>/dev/null)" || true
  fi
  printf '%s' "${s}"
}
is_up() {
  case "$1" in
    active|activating|reloading) return 0 ;;
    *) return 1 ;;
  esac
}

# 利用者として git を読み専用で動かす。root のときは runuser で利用者になる（リポジトリの設定が
# root の権限で動かないように）。GIT_OPTIONAL_LOCKS=0 と core.fsmonitor=false で、索引を書かず、
# 監視用のプログラムも動かさない。
repo_git() {
  if is_root; then
    have runuser || return 127
    runuser -u "${USER_NAME}" -- env GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor=false -C "${REPO_DIR}" "$@"
  else
    GIT_OPTIONAL_LOCKS=0 git -c core.fsmonitor=false -C "${REPO_DIR}" "$@"
  fi
}

# 最初に 1 回だけ、実行している人・調べる利用者・OS の情報を集める。
init_context() {
  local passwd_line="" key value
  UID_NOW="$(id -u 2>/dev/null)" || UID_NOW=""
  CUR_USER="$(id -un 2>/dev/null)" || CUR_USER=""
  USER_HOME="/home/${USER_NAME}"
  if have getent; then
    if passwd_line="$(getent passwd "${USER_NAME}" 2>/dev/null)" && [ -n "${passwd_line}" ]; then
      IFS=: read -r _ _ USER_UID _ _ USER_HOME _ < <(printf '%s\n' "${passwd_line}") || true
      if [ -z "${USER_HOME}" ]; then USER_HOME="/home/${USER_NAME}"; fi
    else
      passwd_line=""
    fi
  fi
  if have id; then
    USER_GROUPS="$(id -nG "${USER_NAME}" 2>/dev/null)" || USER_GROUPS=""
  fi
  if [ -r "${SYSROOT}/etc/os-release" ]; then
    while IFS='=' read -r key value || [ -n "${key}" ]; do
      if [ "${key}" = "VERSION_CODENAME" ]; then
        value="${value%\"}"; value="${value#\"}"
        OS_CODENAME="${value}"
      fi
    done < "${SYSROOT}/etc/os-release"
  fi
  USER_KNOWN=1
  if have getent && [ -z "${passwd_line}" ]; then USER_KNOWN=0; fi
  KERNEL_RELEASE="$(uname -r 2>/dev/null)" || KERNEL_RELEASE=""
  KERNEL_VERSION="$(uname -v 2>/dev/null)" || KERNEL_VERSION=""
}

# 調べる利用者が存在するか。
check_user() {
  if [ "${USER_KNOWN}" -eq 0 ]; then
    emit 不明 "利用者" "ユーザー ${USER_NAME} が見つかりません。--user で正しい名前を指定してください（以降の項目は、標準の場所 ${USER_HOME} を見ます）"
  else
    emit OK "利用者" "調べる利用者は ${USER_NAME} です（ホーム: ${USER_HOME}）"
  fi
  return 0
}

# -- ネットワーク ----------------------------------------------------------------

# IPv4 アドレス（a.b.c.d）の種類: private・cgnat・linklocal・loopback・global・invalid。
classify_ipv4() {
  local ip=$1 re='^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$' a b c d
  if [[ ! "${ip}" =~ ${re} ]]; then printf 'invalid'; return 0; fi
  a=$((10#${BASH_REMATCH[1]})); b=$((10#${BASH_REMATCH[2]}))
  c=$((10#${BASH_REMATCH[3]})); d=$((10#${BASH_REMATCH[4]}))
  if [ "${a}" -gt 255 ] || [ "${b}" -gt 255 ] || [ "${c}" -gt 255 ] || [ "${d}" -gt 255 ]; then
    printf 'invalid'; return 0
  fi
  if [ "${a}" -eq 10 ]; then printf 'private'
  elif [ "${a}" -eq 172 ] && [ "${b}" -ge 16 ] && [ "${b}" -le 31 ]; then printf 'private'
  elif [ "${a}" -eq 192 ] && [ "${b}" -eq 168 ]; then printf 'private'
  elif [ "${a}" -eq 100 ] && [ "${b}" -ge 64 ] && [ "${b}" -le 127 ]; then printf 'cgnat'
  elif [ "${a}" -eq 169 ] && [ "${b}" -eq 254 ]; then printf 'linklocal'
  elif [ "${a}" -eq 127 ]; then printf 'loopback'
  else printf 'global'
  fi
  return 0
}

# ip -o addr の出力から、inet（または inet6）の次の語（アドレス/長さ）を 1 行ずつ取り出す。
# shellcheck disable=SC2016
addresses_of() {
  awk -v want="$1" '{ for (i = 1; i < NF; i++) if ($i == want) { print $(i + 1); break } }'
}

# 学外から届くかの確かめ方（グローバルアドレスを見つけたとき）。
global_advice() {
  fixline "学外から届くかを確かめる。学外のネットワーク（スマホのテザリングなど）につないだ PC で  nc -vz $1 22  を実行する（つながれば学外から届く）。または情報基盤の担当に「この Pi のアドレスは学外から届くか」を尋ねる"
  more "届くなら、SSH のパスワードログインの危険度は「高」です。先に  sudo bash scripts/harden.sh ssh  と  sudo bash scripts/harden.sh firewall  を実行してください"
}

# NET-ADDR4
check_net_addr4() {
  local category="IPv4 アドレス" out_global="" out_link="" cidr addr kind found=0
  if ! have ip; then
    emit 不明 "${category}" "ip コマンドがないため調べられません"
    return 0
  fi
  out_global="$(ip -4 -o addr show scope global 2>/dev/null)" || out_global=""
  out_link="$(ip -4 -o addr show scope link 2>/dev/null)" || out_link=""
  while IFS= read -r cidr; do
    [ -n "${cidr}" ] || continue
    addr="${cidr%%/*}"
    kind="$(classify_ipv4 "${addr}")"
    case "${kind}" in
      private)
        found=1
        emit OK "${category}" "${cidr} はプライベートアドレス。学外から直接は届きません"
        ;;
      cgnat)
        found=1
        emit OK "${category}" "${cidr} は共有アドレス空間（100.64.0.0/10、キャリアグレード NAT）。学外から直接は届きません"
        ;;
      linklocal)
        found=1
        emit 注意 "${category}" "${cidr} は DHCP からアドレスをもらえなかったときの仮のアドレス（169.254.0.0/16）です。ネットワークにつながっていません"
        fixline "ケーブルや Wi-Fi、ルーター（DHCP）の状態を確かめる。つながるまで、時報の取得と時刻の同期が働きません"
        ;;
      loopback) ;;
      global)
        found=1
        GLOBAL_FOUND=1
        GLOBAL_ADDRS+=("${addr}")
        emit 注意 "${category}" "${cidr} はグローバルアドレスです（大学が持つクラス B/C のアドレスの可能性）。学外から届くかどうかは大学の防火壁しだいです"
        global_advice "${addr}"
        ;;
      *)
        found=1
        emit 不明 "${category}" "読み取れないアドレスです: ${cidr}"
        ;;
    esac
  done < <(printf '%s\n%s\n' "${out_global}" "${out_link}" | addresses_of inet)
  if [ "${found}" -eq 0 ]; then
    emit 注意 "${category}" "IPv4 アドレスがありません。ネットワークにつながっていないようです"
    fixline "ケーブルや Wi-Fi、ルーター（DHCP）の状態を確かめる。つながるまで、時報の取得と時刻の同期が働きません"
  fi
  return 0
}

# NET-ADDR6
check_net_addr6() {
  local category="IPv6 アドレス" out="" cidr addr group found=0
  if ! have ip; then
    emit 不明 "${category}" "ip コマンドがないため調べられません"
    return 0
  fi
  out="$(ip -6 -o addr show scope global 2>/dev/null)" || out=""
  while IFS= read -r cidr; do
    [ -n "${cidr}" ] || continue
    addr="${cidr%%/*}"
    group="${addr%%:*}"
    if [[ ! "${group}" =~ ^[0-9a-fA-F]{1,4}$ ]]; then
      found=1
      emit 不明 "${category}" "読み取れないアドレスです: ${cidr}"
    elif [ "$(( 16#${group} & 65024 ))" -eq 64512 ]; then
      # fc00::/7（ユニークローカル）。学内専用。
      found=1
      emit OK "${category}" "${cidr} はユニークローカルアドレス（fc00::/7）。学外から直接は届きません"
    else
      found=1
      GLOBAL_FOUND=1
      GLOBAL_ADDRS+=("${addr}")
      emit 注意 "${category}" "${cidr} はグローバルアドレスです。学外から届くかどうかは大学の防火壁しだいです"
      global_advice "${addr}"
    fi
  done < <(printf '%s\n' "${out}" | addresses_of inet6)
  if [ "${found}" -eq 0 ]; then
    emit OK "${category}" "IPv6 のグローバルアドレスはありません"
  fi
  return 0
}

# NET-LISTEN
check_net_listen() {
  local category="待ち受けポート" out="" proto local_addr host port key
  local expected=() unexpected=() seen=" "
  if ! have ss; then
    emit 不明 "${category}" "ss コマンドがないため調べられません"
    return 0
  fi
  if ! out="$(ss -H -lntu 2>/dev/null)"; then
    emit 不明 "${category}" "ss -H -lntu を実行できませんでした"
    return 0
  fi
  while read -r proto _ _ _ local_addr _; do
    [ -n "${local_addr}" ] || continue
    host="${local_addr%:*}"
    port="${local_addr##*:}"
    host="${host%%\%*}"; host="${host#[}"; host="${host%]}"
    case "${host}" in
      127.*|::1|localhost) continue ;;
    esac
    [[ "${port}" =~ ^[0-9]+$ ]] || continue
    key="${proto} ${port}"
    case "${seen}" in *" ${key}, "*) continue ;; esac
    seen="${seen}${key}, "
    case "${key}" in
      "tcp 22") expected+=("tcp 22（SSH）") ;;
      "udp 5353") expected+=("udp 5353（mDNS。campus-chime.local の名前に使う）") ;;
      "udp 68") expected+=("udp 68（DHCP）") ;;
      "udp 546") expected+=("udp 546（DHCPv6）") ;;
      "udp 123")
        if [ "${CHRONY_ACTIVE}" -eq 1 ]; then expected+=("udp 123（chrony の NTP）"); else unexpected+=("${key}"); fi
        ;;
      udp\ *)
        # Avahi や chrony は、返事を受けるために 32768 以上の一時的な UDP ポートも開ける。
        if [ "${port}" -ge 32768 ] && { [ "${AVAHI_ACTIVE}" -eq 1 ] || [ "${CHRONY_ACTIVE}" -eq 1 ]; }; then
          expected+=("${key}（Avahi・chrony が使う一時的なポート）")
        else
          unexpected+=("${key}")
        fi
        ;;
      *) unexpected+=("${key}") ;;
    esac
  done < <(printf '%s\n' "${out}")
  if [ "${#unexpected[@]}" -gt 0 ]; then
    emit 注意 "${category}" "想定外のポートが外に向けて開いています: $(join_ja "${unexpected[@]}")"
    fixline "何のプログラムが開けたかを  sudo ss -lntup  で調べ、要らなければ止める。防火壁（sudo bash scripts/harden.sh firewall）でも、外から届かなくできる"
    if [ "${#expected[@]}" -gt 0 ]; then more "想定内のポート: $(join_ja "${expected[@]}")"; fi
  elif [ "${#expected[@]}" -gt 0 ]; then
    emit OK "${category}" "外に向けて開いているのは想定内のポートだけです: $(join_ja "${expected[@]}")"
  else
    emit OK "${category}" "外に向けて開いているポートはありません"
  fi
  return 0
}

# NET-FW
check_net_fw() {
  local category="防火壁" ruleset=""
  if ! is_root; then need_root "${category}"; return 0; fi
  if ! have nft; then
    emit 要対応 "${category}" "nft（nftables）が入っておらず、防火壁がありません。どこからでも、開いているポートに届きます"
    fixline "sudo bash scripts/harden.sh firewall"
    return 0
  fi
  if ! ruleset="$(nft list ruleset 2>/dev/null)"; then
    emit 不明 "${category}" "nft list ruleset を実行できませんでした"
    return 0
  fi
  if printf '%s\n' "${ruleset}" | awk '/hook input/ && /policy drop/ { found = 1 } END { exit found ? 0 : 1 }'; then
    emit OK "${category}" "入ってくる通信を、許したもの以外は捨てる防火壁（hook input・policy drop）があります"
  else
    emit 要対応 "${category}" "入ってくる通信を既定で捨てる防火壁（hook input・policy drop）がありません。開いているポートに、届くところからは誰でも届きます"
    fixline "sudo bash scripts/harden.sh firewall"
  fi
  return 0
}

# -- SSH -------------------------------------------------------------------------

# SSH-ACTIVE
check_ssh_active() {
  local category="SSH の稼働" service_state socket_state
  if ! have systemctl; then
    emit 不明 "${category}" "systemctl がないため、SSH が動いているか調べられません（動いているものとして続けます）"
    return 0
  fi
  service_state="$(unit_state ssh.service)"
  socket_state="$(unit_state ssh.socket)"
  if is_up "${service_state}" || is_up "${socket_state}"; then
    emit 情報 "${category}" "SSH が動いています（ポート 22）"
  elif [ -z "${service_state}${socket_state}" ]; then
    emit 不明 "${category}" "SSH が動いているか調べられません（動いているものとして続けます）"
  else
    SSH_ON=0
    emit OK "${category}" "SSH は動いていません（SSH で入られることはありません。以降の SSH の項目は省きます）"
  fi
  return 0
}

declare -A SSHCFG=()
# sshd_config を読む（Include を辿り、Match の中は無視し、同じ項目は最初の値を使う）:
# read_sshd_file ファイル 深さ。値は SSHCFG[小文字の項目名] に入る。
read_sshd_file() {
  local file=$1 depth=$2 line key rest in_match=0 pattern included
  local -a patterns=()
  [ -r "${file}" ] || return 0
  while IFS= read -r line || [ -n "${line}" ]; do
    line="$(trim "${line}")"
    case "${line}" in ''|'#'*) continue ;; esac
    key="${line%%[[:space:]=]*}"
    rest="$(trim "${line#"${key}"}")"
    rest="${rest#=}"
    rest="$(trim "${rest}")"
    key="${key,,}"
    case "${key}" in
      match) in_match=1 ;;
      include)
        if [ "${in_match}" -eq 0 ] && [ "${depth}" -lt 5 ]; then
          read -r -a patterns < <(printf '%s\n' "${rest}") || true
          for pattern in "${patterns[@]}"; do
            case "${pattern}" in /*) ;; *) pattern="/etc/ssh/${pattern}" ;; esac
            while IFS= read -r included; do
              read_sshd_file "${included}" $((depth + 1))
            done < <(compgen -G "${SYSROOT}${pattern}" | sort || true)
          done
        fi
        ;;
      passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|permitrootlogin)
        if [ "${in_match}" -eq 0 ]; then
          if [ "${key}" = "challengeresponseauthentication" ]; then key="kbdinteractiveauthentication"; fi
          if [ -z "${SSHCFG[${key}]+set}" ]; then
            rest="${rest%%[[:space:]]*}"
            rest="${rest#\"}"; rest="${rest%\"}"
            SSHCFG[${key}]="${rest,,}"
          fi
        fi
        ;;
    esac
  done < "${file}"
  return 0
}

# PermitRootLogin の値の強さ（大きいほど危ない）。
root_login_rank() {
  case "$1" in
    yes) printf '4' ;;
    prohibit-password|without-password) printf '3' ;;
    forced-commands-only) printf '2' ;;
    no) printf '1' ;;
    *) printf '0' ;;
  esac
}

# SSH-PASSWORD / SSH-ROOT
check_ssh_auth() {
  local addr out key value pw="" kbd="" root_login="" estimated=0 failed=0
  local pw_category="SSH パスワードログイン" root_category="SSH の root ログイン"
  local level="中" note="" what=""
  if is_root && have sshd; then
    # Match ブロックが特定のアドレスだけパスワードを許していないかも見るため、学外のアドレスと
    # 学内のアドレスの両方で、sshd が実際に使う設定を出させる。
    for addr in 203.0.113.1 10.0.0.1 192.168.1.1; do
      if out="$(sshd -T -C "user=${USER_NAME},host=chime-check,addr=${addr}" 2>/dev/null)"; then
        while read -r key value _; do
          case "${key}" in
            passwordauthentication)
              if [ "${value}" = "yes" ]; then pw="yes"; elif [ -z "${pw}" ]; then pw="no"; fi ;;
            kbdinteractiveauthentication|challengeresponseauthentication)
              if [ "${value}" = "yes" ]; then kbd="yes"; elif [ -z "${kbd}" ]; then kbd="no"; fi ;;
            permitrootlogin)
              if [ "$(root_login_rank "${value}")" -gt "$(root_login_rank "${root_login}")" ]; then root_login="${value}"; fi ;;
          esac
        done < <(printf '%s\n' "${out}")
      else
        failed=1
      fi
    done
  else
    failed=1
  fi
  if [ "${failed}" -eq 1 ] || [ -z "${pw}" ]; then
    # sshd -T を使えない（root でない・sshd が見つからない・実行に失敗）。設定ファイルから推定する。
    estimated=1
    pw=""; kbd=""; root_login=""
    if [ ! -r "${SYSROOT}/etc/ssh/sshd_config" ]; then
      if is_root; then
        emit 不明 "${pw_category}" "sshd -T を実行できず、/etc/ssh/sshd_config も読めません"
      else
        emit 不明 "${pw_category}" "/etc/ssh/sshd_config を読めません（root なら sshd の実際の設定を調べられます）"
        printf '    → 確かめ方: sudo bash scripts/security_check.sh\n'
      fi
      emit 不明 "${root_category}" "/etc/ssh/sshd_config を読めないため調べられません"
      return 0
    fi
    read_sshd_file "${SYSROOT}/etc/ssh/sshd_config" 0
    # 未設定のときは OpenSSH の既定（パスワードは yes・root は prohibit-password）で考える。
    pw="${SSHCFG[passwordauthentication]:-yes}"
    kbd="${SSHCFG[kbdinteractiveauthentication]:-yes}"
    root_login="${SSHCFG[permitrootlogin]:-prohibit-password}"
  fi
  if [ "${estimated}" -eq 1 ]; then note="（設定ファイルから推定）"; fi

  if [ "${GLOBAL_FOUND}" -eq 1 ]; then level="高"; fi
  if [ "${pw}" = "yes" ] || [ "${kbd}" = "yes" ]; then
    SSH_PW_YES=1
    if [ "${pw}" = "yes" ]; then what="PasswordAuthentication"; fi
    if [ "${kbd}" = "yes" ]; then
      if [ -n "${what}" ]; then what="${what} と KbdInteractiveAuthentication"; else what="KbdInteractiveAuthentication"; fi
    fi
    emit 要対応 "${pw_category}" "パスワードでの SSH ログインが有効です（${what} が yes）。危険度は「${level}」${note}。パスワードを推測する攻撃を受けます"
    if [ "${GLOBAL_FOUND}" -eq 1 ]; then
      more "この Pi はグローバルアドレスを持つため、学外から届くなら「高」とみなしています"
    fi
    fixline "先に PC で鍵を作ってこの Pi に登録し（SSH 公開鍵の項目を参照）、そのあと  sudo bash scripts/harden.sh ssh"
  else
    emit OK "${pw_category}" "パスワードでの SSH ログインは無効です（鍵だけ）${note}"
  fi

  case "${root_login}" in
    yes)
      emit 要対応 "${root_category}" "root で SSH ログインできる設定です（PermitRootLogin yes）${note}"
      fixline "sudo bash scripts/harden.sh ssh"
      ;;
    prohibit-password|without-password)
      emit 情報 "${root_category}" "root はパスワードでは入れません（鍵なら入れる。harden.sh ssh で no にできます）${note}"
      ;;
    forced-commands-only)
      emit 情報 "${root_category}" "root は、鍵に決めたコマンドを付けたときだけ入れます（forced-commands-only）${note}"
      ;;
    no)
      emit OK "${root_category}" "root での SSH ログインは禁止されています${note}"
      ;;
    *)
      emit 不明 "${root_category}" "PermitRootLogin の値を読み取れません: ${root_login}"
      ;;
  esac
  return 0
}

# SSH-KEYS
check_ssh_keys() {
  local category="SSH 公開鍵" home="${SYSROOT}${USER_HOME}" file line type current count=0 dss=0
  local -A kinds=()
  local -a summary=()
  local re='(^|[[:space:]])(ssh-[a-z0-9]+|ecdsa-sha2-[a-z0-9]+|sk-ssh-[a-z0-9@.-]+|sk-ecdsa-sha2-[a-z0-9@.-]+)[[:space:]]+[A-Za-z0-9+/=]{20,}'
  file="${home}/.ssh/authorized_keys"
  if [ -e "${file}" ] && [ ! -r "${file}" ]; then
    emit 不明 "${category}" "${file#"${SYSROOT}"} を読めません"
    printf '    → 確かめ方: sudo bash scripts/security_check.sh\n'
    return 0
  fi
  if [ ! -e "${file}" ]; then
    if { [ -e "${home}" ] && [ ! -x "${home}" ]; } || { [ -d "${home}/.ssh" ] && [ ! -x "${home}/.ssh" ]; }; then
      emit 不明 "${category}" "${USER_HOME} の中を読めません"
      printf '    → 確かめ方: sudo bash scripts/security_check.sh\n'
      return 0
    fi
  else
    while IFS= read -r line || [ -n "${line}" ]; do
      line="${line#"${line%%[![:space:]]*}"}"
      case "${line}" in '#'*) continue ;; esac
      if [[ "${line}" =~ ${re} ]]; then
        type="${BASH_REMATCH[2]}"
        count=$((count + 1))
        current="${kinds[${type}]:-0}"
        kinds[${type}]=$((current + 1))
        if [ "${type}" = "ssh-dss" ]; then dss=$((dss + 1)); fi
      fi
    done < "${file}"
  fi
  if [ "${count}" -eq 0 ]; then
    emit 注意 "${category}" "鍵がまだありません（${USER_HOME}/.ssh/authorized_keys に公開鍵が 1 つもありません）。harden.sh ssh の前に、PC で鍵を作ってこの Pi に登録しておく必要があります"
    fixline "PC で鍵を作り、この Pi に登録する"
    # shellcheck disable=SC2016
    more 'Windows（PowerShell）: ssh-keygen -t ed25519  のあと'
    # shellcheck disable=SC2016
    more '  type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh '"${USER_NAME}"'@campus-chime.local "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"'
    more "macOS / Linux: ssh-keygen -t ed25519  のあと  ssh-copy-id ${USER_NAME}@campus-chime.local"
    more "そのあと、別の画面で  ssh ${USER_NAME}@campus-chime.local  と鍵でログインできることを確かめてから、sudo bash scripts/harden.sh ssh"
    return 0
  fi
  while IFS= read -r type; do
    summary+=("${type} が ${kinds[${type}]} 個")
  done < <(printf '%s\n' "${!kinds[@]}" | sort)
  if [ "${dss}" -gt 0 ]; then
    emit 注意 "${category}" "公開鍵が ${count} 個あります（$(join_ja "${summary[@]}")）。古い DSA 鍵（ssh-dss）が含まれます。新しい OpenSSH では使えず、強さも足りません"
    fixline "新しい鍵を作り（ssh-keygen -t ed25519）、登録し直して、ssh-dss の行を authorized_keys から消す"
  else
    emit OK "${category}" "公開鍵が ${count} 個あります（$(join_ja "${summary[@]}")）"
  fi
  return 0
}

# SSH のジャーナル（過去 N 日）を読む。読めたら 0 を返して本文を JOURNAL_TEXT に入れる。
read_ssh_journal() {
  local since=$1 rc=0
  JOURNAL_TEXT=""
  have journalctl || return 1
  JOURNAL_TEXT="$(journalctl -u ssh.service --since "${since}" -o cat 2>&1)" || rc=$?
  if [ "${rc}" -ne 0 ]; then return 1; fi
  case "${JOURNAL_TEXT}" in
    *"not seeing messages"*|*"No journal files"*) return 1 ;;
  esac
  return 0
}

# SSH-FAILED
# shellcheck disable=SC2016
check_ssh_failed() {
  local category="SSH 失敗ログイン" kind count addr rest
  local failed=0 invalid=0
  local -a sources=()
  if ! read_ssh_journal "-7 days"; then
    emit 不明 "${category}" "SSH のログ（journalctl）を読めません（root で実行するか、${USER_NAME} を systemd-journal グループに入れると読めます）"
    printf '    → 確かめ方: sudo bash scripts/security_check.sh\n'
    return 0
  fi
  while read -r kind count rest; do
    case "${kind}" in
      failed) failed="${count}" ;;
      invalid) invalid="${count}" ;;
      src) sources+=("${rest}（${count} 回）") ;;
    esac
  done < <(printf '%s\n' "${JOURNAL_TEXT}" | awk -v top=3 '
    function note(line,   i, n, w) {
      n = split(line, w, " ")
      for (i = 1; i < n; i++) if (w[i] == "from") { cnt[w[i + 1]]++; return }
    }
    /Failed password/ { failed++; note($0) }
    /Invalid user/ { invalid++; note($0) }
    END {
      printf "failed %d\n", failed
      printf "invalid %d\n", invalid
      for (k = 0; k < top; k++) {
        best = ""; bestn = 0
        for (a in cnt) if (cnt[a] > bestn || (cnt[a] == bestn && bestn > 0 && a < best)) { best = a; bestn = cnt[a] }
        if (bestn == 0) break
        printf "src %d %s\n", bestn, best
        delete cnt[best]
      }
    }')
  if [ "$((failed + invalid))" -gt 0 ]; then
    addr="$(join_ja "${sources[@]}")"
    emit 注意 "${category}" "パスワードを試されています。過去 7 日に Failed password が ${failed} 回、Invalid user が ${invalid} 回ありました（多い送信元: ${addr:-不明}）"
    fixline "sudo bash scripts/harden.sh ssh  で鍵だけにする（パスワードを試されても入れなくなる）。学外から届くなら  sudo bash scripts/harden.sh firewall  も"
  else
    emit OK "${category}" "過去 7 日に、失敗したログイン（Failed password・Invalid user）はありません"
  fi
  return 0
}

# SSH-LOGINS
# shellcheck disable=SC2016
check_ssh_logins() {
  local category="SSH ログイン履歴" kind count rest
  local pubkey=0 password=0
  local -a sources=()
  if ! read_ssh_journal "-30 days"; then
    emit 不明 "${category}" "SSH のログ（journalctl）を読めません"
    printf '    → 確かめ方: sudo bash scripts/security_check.sh\n'
    return 0
  fi
  while read -r kind count rest; do
    case "${kind}" in
      publickey) pubkey="${count}" ;;
      password) password="${count}" ;;
      src) sources+=("${rest}（${count} 回）") ;;
    esac
  done < <(printf '%s\n' "${JOURNAL_TEXT}" | awk -v user="${USER_NAME}" -v top=5 '
    $1 == "Accepted" && ($2 == "password" || $2 == "publickey") && $3 == "for" && $4 == user && $5 == "from" {
      method[$2]++; cnt[$6]++
    }
    END {
      printf "publickey %d\n", method["publickey"]
      printf "password %d\n", method["password"]
      for (k = 0; k < top; k++) {
        best = ""; bestn = 0
        for (a in cnt) if (cnt[a] > bestn || (cnt[a] == bestn && bestn > 0 && a < best)) { best = a; bestn = cnt[a] }
        if (bestn == 0) break
        printf "src %d %s\n", bestn, best
        delete cnt[best]
      }
    }')
  if [ "$((pubkey + password))" -eq 0 ]; then
    emit 情報 "${category}" "過去 30 日に、${USER_NAME} が SSH でログインした記録はありません（ログが再起動で消えていることもあります）"
  else
    emit 情報 "${category}" "過去 30 日に、${USER_NAME} のログインが成功した回数は、公開鍵が ${pubkey} 回、パスワードが ${password} 回です（送信元: $(join_ja "${sources[@]}")）"
    if [ "${password}" -gt 0 ]; then
      more "パスワードでのログインが使われています。鍵だけにする（harden.sh ssh）前に、使っている人が鍵でログインできるようにしておいてください"
    fi
  fi
  return 0
}

# -- sudo と利用者のパスワード -------------------------------------------------------

# sudoers の中の NOPASSWD の規則を探し、SUDO_FULL（NOPASSWD: ALL）と SUDO_LIMITED（一部の
# コマンドだけ）に、見つけたファイル名を入れる。
SUDO_FULL=""
SUDO_LIMITED=""
scan_sudoers() {
  local file base line lhs hosts subjects subject cmds
  local -a list=()
  for file in "${SYSROOT}/etc/sudoers" "${SYSROOT}/etc/sudoers.d"/*; do
    [ -f "${file}" ] || continue
    base="${file##*/}"
    if [ "${file%/*}" != "${SYSROOT}/etc" ]; then
      # sudo は、sudoers.d の中の、名前に . を含むファイルと ~ で終わるファイルを読まない。
      case "${base}" in *.*|*~) continue ;; esac
    fi
    while IFS= read -r line || [ -n "${line}" ]; do
      line="$(trim "${line}")"
      case "${line}" in ''|'#'*|Defaults*|User_Alias*|Runas_Alias*|Host_Alias*|Cmnd_Alias*) continue ;; esac
      case "${line}" in *NOPASSWD:*) ;; *) continue ;; esac
      lhs="$(trim "${line%%=*}")"
      hosts="${lhs##*[[:space:]]}"
      subjects="$(trim "${lhs%"${hosts}"}")"
      read -r -a list < <(printf '%s\n' "${subjects//,/ }") || true
      for subject in "${list[@]}"; do
        case "${subject}" in
          ALL|"${USER_NAME}") ;;
          %*) case " ${USER_GROUPS} " in *" ${subject#%} "*) ;; *) continue ;; esac ;;
          *) continue ;;
        esac
        cmds="$(trim "${line#*NOPASSWD:}")"
        while [[ "${cmds}" =~ ^[A-Z_]+:[[:space:]]* ]]; do cmds="${cmds#"${BASH_REMATCH[0]}"}"; done
        if [ "${cmds}" = "ALL" ]; then
          SUDO_FULL="${SUDO_FULL:+${SUDO_FULL}、}/etc/${file#"${SYSROOT}/etc/"}"
        else
          SUDO_LIMITED="${SUDO_LIMITED:+${SUDO_LIMITED}、}/etc/${file#"${SYSROOT}/etc/"}"
        fi
        break
      done
    done < "${file}"
  done
  return 0
}

# SUDO-NOPASSWD
check_sudo() {
  local category="sudo のパスワード" out="" via="" detail_full="" detail_limited="" line cmds
  if is_root; then
    scan_sudoers
    via="/etc/sudoers と /etc/sudoers.d から確認"
  else
    if [ -n "${CUR_USER}" ] && [ "${CUR_USER}" != "${USER_NAME}" ]; then
      emit 不明 "${category}" "別のユーザー（${USER_NAME}）の sudo の設定は、root でないと調べられません"
      printf '    → 確かめ方: sudo bash scripts/security_check.sh\n'
      return 0
    fi
    if ! have sudo; then
      emit 不明 "${category}" "sudo コマンドがないため調べられません"
      return 0
    fi
    # 設定を一覧するだけ（-n: パスワードは聞かない）。
    if out="$(sudo -n -l 2>&1)"; then
      via="sudo -n -l の出力から確認"
      while IFS= read -r line; do
        case "${line}" in *NOPASSWD:*) ;; *) continue ;; esac
        cmds="$(trim "${line#*NOPASSWD:}")"
        while [[ "${cmds}" =~ ^[A-Z_]+:[[:space:]]* ]]; do cmds="${cmds#"${BASH_REMATCH[0]}"}"; done
        if [ "${cmds}" = "ALL" ]; then SUDO_FULL="sudo -l"; else SUDO_LIMITED="sudo -l"; fi
      done < <(printf '%s\n' "${out}")
    else
      case "${out}" in
        *"a password is required"*)
          emit OK "${category}" "sudo にはパスワードが必要です（sudo -n -l はパスワードを聞かれて止まりました）"
          return 0
          ;;
        *"may not run sudo"*)
          emit OK "${category}" "${USER_NAME} は sudo を使えません（sudo -n -l の出力から確認）"
          return 0
          ;;
        *)
          emit 不明 "${category}" "sudo -n -l の結果を読み取れませんでした"
          return 0
          ;;
      esac
    fi
  fi
  detail_full="${via}"
  detail_limited="${via}"
  if is_root; then
    detail_full="規則のあるファイル: ${SUDO_FULL}。${via}"
    detail_limited="規則のあるファイル: ${SUDO_LIMITED}。${via}"
  fi
  if [ -n "${SUDO_FULL}" ]; then
    if [ "${SSH_PW_YES}" -eq 1 ]; then
      emit 要対応 "${category}" "${USER_NAME} は sudo をパスワードなしで使えます（NOPASSWD: ALL。${detail_full}）。しかも SSH はパスワードで入れるので、パスワードを当てられると、そのまま root を取られます"
    else
      emit 注意 "${category}" "${USER_NAME} は sudo をパスワードなしで使えます（NOPASSWD: ALL。${detail_full}）。SSH や時報サービスを乗っ取られると、そのまま root を取られます"
    fi
    fixline "sudo bash scripts/harden.sh sudo  （${USER_NAME} のパスワードが決まっていることが前提。無ければ先に  sudo passwd ${USER_NAME}）"
  elif [ -n "${SUDO_LIMITED}" ]; then
    emit 情報 "${category}" "一部のコマンドだけ、パスワードなしで sudo を使えます（${detail_limited}）"
  else
    emit OK "${category}" "sudo にはパスワードが必要です（${via}）"
  fi
  return 0
}

# USER-PASSWORD
# shellcheck disable=SC2016
check_user_password() {
  local category="利用者のパスワード" out="" state
  if ! is_root; then need_root "${category}"; return 0; fi
  if ! have passwd; then
    emit 不明 "${category}" "passwd コマンドがないため調べられません"
    return 0
  fi
  if ! out="$(passwd -S "${USER_NAME}" 2>/dev/null)"; then
    emit 不明 "${category}" "passwd -S ${USER_NAME} を実行できませんでした"
    return 0
  fi
  state="$(printf '%s\n' "${out}" | awk 'NR == 1 { print $2 }')"
  case "${state}" in
    P) emit OK "${category}" "${USER_NAME} のパスワードが設定されています" ;;
    L|NP)
      emit 情報 "${category}" "${USER_NAME} にパスワードがありません（状態 ${state}）。harden.sh sudo の前に、sudo passwd ${USER_NAME} でパスワードを決めてください"
      ;;
    *) emit 不明 "${category}" "passwd -S の結果を読み取れませんでした: ${out}" ;;
  esac
  return 0
}

# -- OS とカーネル ---------------------------------------------------------------

# OS-RELEASE
check_os_release() {
  local category="OS の版"
  if [ ! -r "${SYSROOT}/etc/os-release" ] || [ -z "${OS_CODENAME}" ]; then
    emit 不明 "${category}" "/etc/os-release から OS の版（VERSION_CODENAME）を読めません"
    return 0
  fi
  case "${OS_CODENAME}" in
    trixie)
      emit OK "${category}" "Debian 13（trixie）です"
      ;;
    bookworm)
      emit 情報 "${category}" "Debian 12（bookworm）です。Debian 12 は 2026-07 から LTS（長期サポート。2028-06-30 まで）に入っています。Raspbian の再構築は遅れることがあります"
      more "更新が届いているかは、下の「更新の状況」「カーネルの新しさ」で確かめます"
      ;;
    bullseye|buster|stretch|jessie|wheezy|squeeze|lenny|etch|sarge)
      emit 要対応 "${category}" "Debian ${OS_CODENAME} は古く、セキュリティ更新が終わっています（Debian 11 の LTS は 2026-08-31 に終了）"
      fixline "新しい Raspberry Pi OS（Bookworm 以降）を SD カードに書き直す（docs/SETUP.md の手順）"
      ;;
    *)
      emit 不明 "${category}" "OS の版（${OS_CODENAME}）を判定できません"
      ;;
  esac
  return 0
}

# uname -v の文字列から、カーネルを作った日（YYYY-MM-DD）を取り出す。分からなければ失敗する。
# Raspberry Pi のカーネルは末尾が (2025-04-30) の形。古いものは date で読める日時の文字列。
kernel_build_date() {
  local v=$1 rest word
  if [[ "${v}" =~ \(([0-9]{4}-[0-9]{2}-[0-9]{2})\)[[:space:]]*$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return 0
  fi
  rest="${v#\#}"
  rest="${rest#*[[:space:]]}"
  while :; do
    word="${rest%%[[:space:]]*}"
    case "${word}" in
      SMP|PREEMPT|PREEMPT_DYNAMIC|PREEMPT_RT|RT)
        rest="${rest#"${word}"}"
        rest="${rest#"${rest%%[![:space:]]*}"}"
        ;;
      *) break ;;
    esac
  done
  date -d "${rest}" +%Y-%m-%d 2>/dev/null
}

# uname -v の文字列から、動いているカーネルのパッケージの版（Debian 1:6.12.25-1+rpt1 の 1:6.12.25-1+rpt1）。
kernel_running_version() {
  local re='(Debian|Raspbian)[[:space:]]+([^[:space:]]+)[[:space:]]+\('
  if [[ "$1" =~ ${re} ]]; then
    printf '%s' "${BASH_REMATCH[2]}"
    return 0
  fi
  return 1
}

# OS-KERNEL
check_os_kernel() {
  local category="カーネルの新しさ" release="${KERNEL_RELEASE}" version="${KERNEL_VERSION}" built=""
  if [ -z "${version}" ] || ! built="$(kernel_build_date "${version}")" || [ -z "${built}" ]; then
    emit 不明 "${category}" "動いているカーネル（${release:-不明}）を作った日を、uname -v（${version:-取得できず}）から読み取れません"
    return 0
  fi
  if [[ "${built}" < "${KERNEL_FIXES_SINCE}" ]]; then
    emit 要対応 "${category}" "動いているカーネル ${release}（${built} に作成）は、CISA KEV に載った権限昇格の修正を含みません（${KERNEL_FIXES_SINCE} 以降のカーネルが必要）"
    fixline "sudo apt update && sudo apt full-upgrade  のあと、放送のない時間（9:55–17:05 を避ける）に再起動する"
  else
    emit OK "${category}" "動いているカーネル ${release}（${built} に作成）は、CISA KEV に載った権限昇格の修正を含む新しさです。引き続き、月に 1 回は  sudo apt update && sudo apt full-upgrade  で更新してください"
  fi
  return 0
}

# OS-REBOOT
check_os_reboot() {
  local category="再起動の要否" release="${KERNEL_RELEASE}" version="${KERNEL_VERSION}" running="" status pkg ver
  local boot="" boot_epoch="" days="" uptime_note="" reason reason_text=""
  local -a reasons=() newer=()
  if [ -e "${SYSROOT}/run/reboot-required" ]; then
    reasons+=("/run/reboot-required があります（更新のあとに再起動が必要だという印）")
  fi
  if [ -n "${release}" ] && [ ! -d "${SYSROOT}/lib/modules/${release}" ]; then
    reasons+=("動いているカーネル（${release}）のモジュールが /lib/modules にありません（更新で入れ替わっています）")
  fi
  if running="$(kernel_running_version "${version}")" && have dpkg-query && have dpkg; then
    # shellcheck disable=SC2016
    while IFS='|' read -r status pkg ver; do
      status="$(trim "${status}")"
      [ "${status}" = "ii" ] || continue
      if dpkg --compare-versions "${ver}" gt "${running}"; then newer+=("${pkg}（${ver}）"); fi
    done < <(dpkg-query -W -f '${db:Status-Abbrev}|${Package}|${Version}\n' 'linux-image-*' 2>/dev/null || true)
    if [ "${#newer[@]}" -gt 0 ]; then
      reasons+=("動いているカーネル（${running}）より新しいカーネルが入っています: $(join_ja "${newer[@]}")")
    fi
  fi
  if have uptime; then
    boot="$(uptime -s 2>/dev/null)" || boot=""
    if [ -n "${boot}" ] && boot_epoch="$(date -d "${boot}" +%s 2>/dev/null)"; then
      days="$(days_since "${boot_epoch}")"
      uptime_note="最後の起動: ${boot}（${days} 日前）"
    fi
  fi
  for reason in "${reasons[@]}"; do reason_text="${reason_text}${reason}。"; done
  if [ "${#reasons[@]}" -gt 0 ]; then
    emit 要対応 "${category}" "更新したカーネルがまだ使われていません。${reason_text}${uptime_note}"
    fixline "放送のない時間（9:55–17:05 を避ける）に  sudo reboot"
  elif [ -n "${days}" ] && [ "${days}" -gt "${UPTIME_WARN_DAYS}" ]; then
    emit 注意 "${category}" "再起動せずに ${days} 日たっています（${uptime_note}）。カーネルの更新が入っていても、再起動するまで古いカーネルのままです"
    fixline "放送のない時間（9:55–17:05 を避ける）に  sudo reboot"
  else
    emit OK "${category}" "再起動が必要な更新はありません${uptime_note:+（${uptime_note}）}"
  fi
  return 0
}

# apt の操作履歴（history.log と、回転した古い分）を続けて出す。.gz も読む。
apt_history_stream() {
  local file
  for file in "${SYSROOT}/var/log/apt"/history.log*; do
    [ -f "${file}" ] || continue
    if have zcat; then
      zcat -f -- "${file}" 2>/dev/null || true
    else
      cat -- "${file}" 2>/dev/null || true
    fi
  done
  return 0
}

# OS-UPDATES
# shellcheck disable=SC2016
check_os_updates() {
  local category="更新の状況" lists_dir="${SYSROOT}/var/lib/apt/lists"
  local newest_list="" last_upgrade="" last_epoch="" list_age="" upgrade_age="" pending="" stale=0
  local list_text upgrade_text pending_text="" history_found=0 file
  if [ -d "${lists_dir}" ]; then
    newest_list="$(find "${lists_dir}" -maxdepth 1 -name '*InRelease' -printf '%T@\n' 2>/dev/null | sort -rn | awk 'NR == 1 { printf "%d", $1 }')" || newest_list=""
  fi
  for file in "${SYSROOT}/var/log/apt"/history.log*; do
    if [ -f "${file}" ]; then history_found=1; fi
  done
  last_upgrade="$(apt_history_stream | awk '
    /^Start-Date:/ { start = $2 " " $3 }
    /^Commandline:/ && /upgrade/ && start != "" { if (start > best) best = start }
    END { print best }')" || last_upgrade=""
  if [ -z "${newest_list}" ] && [ "${history_found}" -eq 0 ]; then
    emit 不明 "${category}" "apt の更新の記録（/var/lib/apt/lists・/var/log/apt/history.log）を読めません"
    return 0
  fi
  if [ -n "${newest_list}" ]; then
    list_age="$(days_since "${newest_list}")"
    list_text="apt のリストの更新は ${list_age} 日前"
    if [ "${list_age}" -gt "${STALE_DAYS}" ]; then stale=1; fi
  else
    list_text="apt のリストの更新の記録がありません"
    stale=1
  fi
  if [ -n "${last_upgrade}" ] && last_epoch="$(date -d "${last_upgrade}" +%s 2>/dev/null)"; then
    upgrade_age="$(days_since "${last_epoch}")"
    upgrade_text="最後にパッケージを更新したのは ${upgrade_age} 日前（${last_upgrade}）"
    if [ "${upgrade_age}" -gt "${STALE_DAYS}" ]; then stale=1; fi
  else
    upgrade_text="パッケージを更新した記録（upgrade・full-upgrade・unattended-upgrade）がありません"
    stale=1
  fi
  if have apt-get; then
    pending="$(apt_read apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | awk '/^Inst / { n++ } END { print n + 0 }')" || pending=""
  fi
  if [ -n "${pending}" ]; then
    pending_text="。未適用の更新は ${pending} 件（apt の手元のリストからの見積りで、最新とは限りません）"
  fi
  if [ "${stale}" -eq 1 ]; then
    emit 注意 "${category}" "${STALE_DAYS} 日以上、更新が止まっています。${list_text}。${upgrade_text}${pending_text}"
    fixline "sudo apt update && sudo apt full-upgrade  （放送のない時間に。カーネルが入れ替わったら再起動）。毎日の自動更新にするなら  sudo bash scripts/harden.sh updates"
  elif [ -n "${pending}" ] && [ "${pending}" -gt 0 ]; then
    emit 情報 "${category}" "${list_text}。${upgrade_text}${pending_text}"
  else
    emit OK "${category}" "${list_text}。${upgrade_text}${pending_text}"
  fi
  return 0
}

# apt-cache policy の release 行（o=Raspbian,a=stable,n=bookworm,l=Raspbian,c=main,b=armhf）だけを出す。
policy_releases() {
  apt_read apt-cache policy 2>/dev/null | sed -n 's/^[[:space:]]*release[[:space:]][[:space:]]*//p'
}

# unattended-upgrades の Origins-Pattern を 1 行に 1 つ出す。
# shellcheck disable=SC2016
configured_origin_patterns() {
  apt_read apt-config dump 2>/dev/null | sed -n 's/^Unattended-Upgrade::Origins-Pattern::[[:space:]]*"\(.*\)";[[:space:]]*$/\1/p'
}

# 更新元の指定（origin=...,codename=...,label=...）が、release 行（o=...,n=...,l=...）に当てはまるか。
# shellcheck disable=SC2016
origin_pattern_matches() {
  local pattern=$1 release=$2 pair key value want have_it
  local -a pairs=()
  local -A rel=()
  IFS=, read -r -a pairs < <(printf '%s\n' "${release}") || true
  for pair in "${pairs[@]}"; do
    if [ -n "${pair%%=*}" ]; then rel[${pair%%=*}]="${pair#*=}"; fi
  done
  IFS=, read -r -a pairs < <(printf '%s\n' "${pattern}") || true
  [ "${#pairs[@]}" -gt 0 ] || return 1
  for pair in "${pairs[@]}"; do
    key="${pair%%=*}"
    value="${pair#*=}"
    value="${value//'${distro_codename}'/${OS_CODENAME}}"
    case "${key}" in
      origin|o) want="o" ;;
      label|l) want="l" ;;
      codename|n) want="n" ;;
      archive|suite|a) want="a" ;;
      component|c) want="c" ;;
      *) return 1 ;;
    esac
    have_it="${rel[${want}]-}"
    [ "${have_it}" = "${value}" ] || return 1
  done
  return 0
}

# OS-AUTOUPDATE
# shellcheck disable=SC2016
check_os_autoupdate() {
  local category="自動更新" status="" periodic="" pattern release matched=0
  local -a releases=() patterns=() origins=()
  if ! have dpkg-query || ! have apt-config || ! have apt-cache; then
    emit 不明 "${category}" "dpkg-query・apt-config・apt-cache のどれかがないため調べられません"
    return 0
  fi
  status="$(dpkg-query -W -f '${Status}' unattended-upgrades 2>/dev/null)" || status=""
  if [ "${status}" != "install ok installed" ]; then
    emit 注意 "${category}" "自動更新（unattended-upgrades）が入っていません。更新は手で  apt full-upgrade  をしたときだけです"
    fixline "sudo bash scripts/harden.sh updates"
    return 0
  fi
  periodic="$(apt_read apt-config dump 2>/dev/null | sed -n 's/^APT::Periodic::Unattended-Upgrade[[:space:]]*"\([^"]*\)";.*$/\1/p')" || periodic=""
  if [ -z "${periodic}" ] || [ "${periodic}" = "0" ]; then
    emit 注意 "${category}" "unattended-upgrades は入っていますが、毎日の実行が有効になっていません（APT::Periodic::Unattended-Upgrade が 0 か未設定）"
    fixline "sudo bash scripts/harden.sh updates"
    return 0
  fi
  while IFS= read -r release; do
    if [ -n "${release}" ]; then releases+=("${release}"); fi
  done < <(policy_releases)
  while IFS= read -r pattern; do
    if [ -n "${pattern}" ]; then patterns+=("${pattern}"); fi
  done < <(configured_origin_patterns)
  if [ "${#releases[@]}" -eq 0 ]; then
    emit 不明 "${category}" "apt-cache policy から更新元（release 行）を読み取れません"
    return 0
  fi
  for pattern in "${patterns[@]}"; do
    for release in "${releases[@]}"; do
      if origin_pattern_matches "${pattern}" "${release}"; then
        matched=$((matched + 1))
        case " ${origins[*]-} " in
          *" ${pattern%%,*} "*) ;;
          *) origins+=("${pattern%%,*}") ;;
        esac
        break
      fi
    done
  done
  if [ "${matched}" -gt 0 ]; then
    emit OK "${category}" "自動更新（unattended-upgrades）が有効で、いまの更新元に当たる指定があります: $(join_ja "${origins[@]}")"
  else
    emit 注意 "${category}" "unattended-upgrades は入っていますが、更新元の指定（Origins-Pattern）が Raspbian・Raspberry Pi の更新元に当たらないため、何も自動で更新されません（Debian 用の指定だけ）"
    fixline "sudo bash scripts/harden.sh updates"
  fi
  return 0
}

# -- 時刻 ------------------------------------------------------------------------

# TIME-SYNC / TIME-SOURCE
# shellcheck disable=SC2016
check_time() {
  local sync_category="時刻の同期" source_category="時刻の同期元" synced="" servers="" chrony_summary=""
  if ! have timedatectl; then
    emit 不明 "${sync_category}" "timedatectl がないため調べられません"
  else
    synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" || synced=""
    case "${synced}" in
      yes) emit OK "${sync_category}" "時刻は同期しています（NTPSynchronized=yes）" ;;
      no)
        emit 注意 "${sync_category}" "時刻がまだ同期していません。本機は RTC（時計）を持たないので、同期するまで時刻がずれていて、時報が違う時刻に鳴ることがあります"
        fixline "ネットワーク（NTP: UDP 123）につながっているかを確かめる。timedatectl で状態を見られる"
        ;;
      *) emit 不明 "${sync_category}" "同期の状態を読み取れません" ;;
    esac
  fi
  if ! have systemctl; then
    emit 不明 "${source_category}" "systemctl がないため調べられません"
    return 0
  fi
  if [ "${CHRONY_ACTIVE}" -eq 1 ]; then
    if have chronyc; then
      chrony_summary="$(chronyc -n sources 2>/dev/null | awk '
        /^[\^=#][*+?x~-]/ { n++; if (substr($0, 2, 1) == "*") sel = $2 }
        END { if (n > 0) { printf "同期元 %d 個", n; if (sel != "") printf "、選択中: %s", sel } }')" || chrony_summary=""
    fi
    emit OK "${source_category}" "chrony です（起動後の大きな時刻の飛びを受け付けません）${chrony_summary:+。${chrony_summary}}"
  elif [ "${TIMESYNCD_ACTIVE}" -eq 1 ]; then
    if have timedatectl; then
      servers="$(timedatectl show-timesync -p ServerName --value 2>/dev/null)" || servers=""
    fi
    emit 注意 "${source_category}" "systemd-timesyncd です。偽の時刻をそのまま受け入れます（学内で偽装されると、違う時刻に鳴る・1 日鳴らない）${servers:+。いまのサーバー: ${servers}}"
    fixline "sudo bash scripts/harden.sh ntp"
  else
    emit 注意 "${source_category}" "時刻を同期するサービス（chrony・systemd-timesyncd）が動いていません"
    fixline "sudo bash scripts/harden.sh ntp"
  fi
  return 0
}

# -- 使っていないサービス ------------------------------------------------------------

# 起動していて有効なもの（active か enabled）の名前を出す。
units_in_use() {
  local unit state enabled
  for unit in "$@"; do
    state="$(unit_state "${unit}")"
    enabled="$(unit_enabled_state "${unit}")"
    if is_up "${state}"; then
      printf '%s（動作中）\n' "${unit}"
    elif [ "${enabled}" = "enabled" ] || [ "${enabled}" = "enabled-runtime" ]; then
      printf '%s（起動時に有効）\n' "${unit}"
    fi
  done
  return 0
}

# SVC-BLUETOOTH / SVC-MODEM / SVC-AVAHI / SVC-RPICONNECT
check_services() {
  local used signed=""
  local -a list=()
  if ! have systemctl; then
    emit 不明 "Bluetooth" "systemctl がないため調べられません"
    emit 不明 "ModemManager" "systemctl がないため調べられません"
    emit 不明 "Avahi" "systemctl がないため調べられません"
  else
    while IFS= read -r used; do
      if [ -n "${used}" ]; then list+=("${used}"); fi
    done < <(units_in_use bluetooth.service hciuart.service)
    if [ "${#list[@]}" -gt 0 ]; then
      emit 注意 "Bluetooth" "使わない機能が有効です: $(join_ja "${list[@]}")。近くから狙われる脆弱性（BlueZ）の入口になります"
      fixline "sudo bash scripts/harden.sh services"
    else
      emit OK "Bluetooth" "動いておらず、起動時にも有効になっていません"
    fi
    list=()
    while IFS= read -r used; do
      if [ -n "${used}" ]; then list+=("${used}"); fi
    done < <(units_in_use ModemManager.service)
    if [ "${#list[@]}" -gt 0 ]; then
      emit 注意 "ModemManager" "使わない機能が有効です: $(join_ja "${list[@]}")"
      fixline "sudo bash scripts/harden.sh services"
    else
      emit OK "ModemManager" "動いておらず、起動時にも有効になっていません"
    fi
    if [ "${AVAHI_ACTIVE}" -eq 1 ]; then
      emit 情報 "Avahi" "avahi-daemon が動いています。campus-chime.local の名前に使います。防火壁で、同じネットワークからだけ届くようにしておくと安心です（harden.sh firewall）"
    else
      emit OK "Avahi" "avahi-daemon は動いていません（campus-chime.local の名前ではつながりません。アドレスで接続してください）"
    fi
  fi
  # SVC-RPICONNECT
  if have rpi-connect; then
    # 利用者として動かす（rpi-connect は利用者ごとのサービス）。
    if is_root; then
      if have runuser; then signed="$(runuser -u "${USER_NAME}" -- rpi-connect status 2>&1)" || signed=""; fi
    else
      signed="$(rpi-connect status 2>&1)" || signed=""
    fi
    case "${signed,,}" in
      *"signed in: yes"*)
        emit 注意 "Raspberry Pi Connect" "サインイン済みです。学外から遠隔操作できる経路になっています"
        fixline "使わないなら  rpi-connect signout  でサインアウトし、rpi-connect off で止める"
        ;;
      *"signed in: no"*)
        emit 情報 "Raspberry Pi Connect" "入っていますが、サインインしていません"
        ;;
      *)
        emit 不明 "Raspberry Pi Connect" "入っていますが、rpi-connect status の結果を読み取れません"
        ;;
    esac
  fi
  return 0
}

# -- ログ ------------------------------------------------------------------------

# LOG-PERSIST
check_log_persist() {
  local category="ログの保存"
  if [ -d "${SYSROOT}/var/log/journal" ]; then
    emit OK "${category}" "ジャーナル（ログ）は再起動のあとも残ります（/var/log/journal がある）"
  else
    emit 注意 "${category}" "ジャーナル（ログ）が再起動で消えます（/var/log/journal がない）。侵入されたあとの調べや、SSH の失敗の記録が残りません"
    fixline "sudo bash scripts/harden.sh journal"
  fi
  return 0
}

# -- 時報サービス ----------------------------------------------------------------

# サービスの定義ファイルの、最初の User= の値（無ければ空）。
unit_user() {
  local line
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in
      User=*) printf '%s' "$(trim "${line#User=}")"; return 0 ;;
    esac
  done < "$1"
  return 0
}

# CHIME-UNIT / CHIME-SANDBOX
check_chime_service() {
  local unit_file="${SYSROOT}/etc/systemd/system/${SERVICE_NAME}"
  local drop_in="${SYSROOT}/etc/systemd/system/${SERVICE_NAME}.d/50-campus-chime-harden.conf"
  local category="時報サービスの定義" run_user score=""
  if [ ! -f "${unit_file}" ]; then
    emit 注意 "${category}" "時報サービス（${SERVICE_NAME}）が入っていません（/etc/systemd/system にありません）"
    fixline "bash scripts/setup.sh --no-apt"
    return 0
  fi
  run_user="$(unit_user "${unit_file}")"
  if [ -z "${run_user}" ] || [ "${run_user}" = "root" ] || [ "${run_user}" = "0" ]; then
    emit 要対応 "${category}" "時報サービスが root で動く設定です（User= が ${run_user:-なし}）。コードを書き換えられると、そのまま root を取られます"
    fixline "bash scripts/setup.sh --no-apt  で、リポジトリの定義（User=pi）を入れ直す"
  elif [ -f "${REPO_DIR}/${SERVICE_NAME}" ] && ! cmp -s "${unit_file}" "${REPO_DIR}/${SERVICE_NAME}"; then
    emit 注意 "${category}" "入っている定義（User=${run_user}）が、リポジトリの版と違います"
    fixline "bash scripts/setup.sh --no-apt  で入れ直す"
  else
    emit OK "${category}" "時報サービスは ${run_user} で動き、リポジトリの版と同じ定義です"
  fi
  if have systemd-analyze; then
    score="$(systemd-analyze security "${SERVICE_NAME}" --no-pager 2>/dev/null | awk 'NF { last = $0 } END { print last }' | tr -cd '[:print:]')" || score=""
    score="$(trim "${score}")"
  fi
  category="時報サービスの隔離"
  if [ -f "${drop_in}" ]; then
    emit OK "${category}" "サービスを隔離する設定（50-campus-chime-harden.conf）が入っています${score:+。systemd-analyze security: ${score}}"
  else
    emit 注意 "${category}" "サービスを隔離する設定がありません。コードの欠陥やだまされた入力で乗っ取られると、ファイルの書き換え（自分のコードも）や他への通信が自由にできます${score:+。systemd-analyze security: ${score}}"
    fixline "sudo bash scripts/harden.sh service"
  fi
  return 0
}

# -- リポジトリ ------------------------------------------------------------------

# REPO-REMOTE
check_repo_remote() {
  local category="リポジトリの取得元" url="" scheme rest authority="" path="" shown
  if ! have git; then
    emit 不明 "${category}" "git コマンドがないため調べられません"
    return 0
  fi
  if ! url="$(repo_git remote get-url origin 2>/dev/null)" || [ -z "${url}" ]; then
    emit 不明 "${category}" "取得元（git remote の origin）を読み取れません"
    return 0
  fi
  shown="${url}"
  if [[ "${url}" == *://* ]]; then
    # URL に埋め込まれた認証情報（利用者名:トークン@）は、表示に出さない。
    scheme="${url%%://*}"
    rest="${url#*://}"
    authority="${rest%%/*}"
    path="${rest#"${authority}"}"
    authority="${authority##*@}"
    shown="${scheme}://${authority}${path}"
  fi
  case "${shown}" in
    https://github.com/*)
      emit OK "${category}" "取得元は暗号化された GitHub の URL です（${shown}）"
      ;;
    http://*|git://*)
      emit 要対応 "${category}" "取得元が暗号化されていません（${shown}）。通信の途中で、取り込むコードをすり替えられます"
      fixline "git remote set-url origin https://${authority}${path}"
      ;;
    *)
      emit 情報 "${category}" "取得元は ${shown} です（GitHub の https:// 以外）"
      ;;
  esac
  return 0
}

# REPO-PERMS
check_repo_perms() {
  local category="リポジトリの書き込み権限" found="" path n=0 rest_text=""
  local -a paths=() find_args=()
  if ! have find; then
    emit 不明 "${category}" "find コマンドがないため調べられません"
    return 0
  fi
  if [ -n "${USER_UID}" ]; then
    find_args=( \( -perm -0002 -o ! -uid "${USER_UID}" \) )
  else
    find_args=( -perm -0002 )
  fi
  found="$(find "${REPO_DIR}" -xdev -maxdepth 4 "${find_args[@]}" ! -type l -print 2>/dev/null)" || true
  while IFS= read -r path; do
    [ -n "${path}" ] || continue
    n=$((n + 1))
    if [ "${n}" -le 5 ]; then paths+=("${path#"${REPO_DIR}"/}"); fi
  done < <(printf '%s\n' "${found}")
  if [ "${n}" -gt 5 ]; then rest_text="、ほか"; fi
  if [ "${n}" -gt 0 ]; then
    emit 要対応 "${category}" "誰でも書き込める、または ${USER_NAME} の持ち物でないファイルが ${n} 件あります: $(join_ja "${paths[@]}")${rest_text}。時報サービスが実行するコードを、別のユーザーに書き換えられるおそれがあります"
    fixline "sudo chown -R ${USER_NAME}:${USER_NAME} ${REPO_DIR} && chmod -R o-w ${REPO_DIR}"
  else
    emit OK "${category}" "リポジトリに、誰でも書き込めるファイルや、${USER_NAME} 以外の持ち物のファイルはありません"
  fi
  return 0
}

# REPO-HOOKS
check_repo_hooks() {
  local category="Git フック" hooks_dir="${REPO_DIR}/.git/hooks" file hooks_path=""
  local -a found=() items=()
  for file in "${hooks_dir}"/*; do
    if [ -f "${file}" ] || [ -L "${file}" ]; then
      case "${file##*/}" in *.sample) ;; *) found+=("${file##*/}") ;; esac
    fi
  done
  if have git; then
    hooks_path="$(repo_git config --get core.hooksPath 2>/dev/null)" || hooks_path=""
  fi
  items=("${found[@]}")
  if [ -n "${hooks_path}" ]; then items+=("core.hooksPath=${hooks_path}"); fi
  if [ "${#items[@]}" -gt 0 ]; then
    emit 注意 "${category}" "git の操作（pull など）のたびに実行されるプログラムがあります: $(join_ja "${items[@]}")"
    fixline "覚えがなければ、中身を確かめて消す（.git/hooks の中の、.sample でないファイル。core.hooksPath は  git config --unset core.hooksPath）"
  else
    emit OK "${category}" "git のフック（pull などで自動実行されるプログラム）はありません"
  fi
  return 0
}

# REPO-STRAY
check_repo_stray() {
  local category="リポジトリの管理外ファイル" out="" line n=0
  local -a paths=()
  if ! have git; then
    emit 不明 "${category}" "git コマンドがないため調べられません"
    return 0
  fi
  if ! out="$(repo_git status --porcelain --untracked-files=all 2>/dev/null)"; then
    emit 不明 "${category}" "git status を実行できませんでした（リポジトリの持ち主が実行した人と違うと、git は開きません）"
    return 0
  fi
  while IFS= read -r line; do
    case "${line}" in
      '?? config.json') ;;
      '?? '*)
        n=$((n + 1))
        if [ "${n}" -le 5 ]; then paths+=("${line#'?? '}"); fi
        ;;
    esac
  done < <(printf '%s\n' "${out}")
  if [ "${n}" -gt 0 ]; then
    emit 注意 "${category}" "リポジトリに管理外のファイルがあります（${n} 件）: $(join_ja "${paths[@]}")。置いた覚えのないプログラム（.py・.sh）は、誰かが仕込んだコードかもしれません"
    fixline "git status で確かめ、覚えのないファイルは中身を読んでから消す"
  else
    emit OK "${category}" "リポジトリに、管理外のファイル（config.json 以外）はありません"
  fi
  return 0
}

# PY-USERSITE
check_py_usersite() {
  local category="pip の pygame" dir pip_pygame=""
  for dir in "${SYSROOT}${USER_HOME}"/.local/lib/python3*/site-packages/pygame; do
    if [ -e "${dir}" ]; then pip_pygame="${dir#"${SYSROOT}"}"; break; fi
  done
  if [ -n "${pip_pygame}" ]; then
    emit 注意 "${category}" "${pip_pygame} があります。pip で入れた pygame が、apt の版より先に読み込まれ、更新されないまま残ります"
    fixline "pip3 uninstall pygame  （apt の python3-pygame を使う）"
  else
    emit OK "${category}" "${USER_NAME} の pip で入れた pygame はありません（apt の版が使われます）"
  fi
  return 0
}

# HARDEN-PENDING
# harden.sh の確定待ちの記録（pending ファイル）は、1 行に 1 つ: 手順ごとの「ssh=時刻」「firewall=時刻」「sudo=時刻」と、
# 自動で戻るまでの「window=分数」。手順が 1 つも無い記録（window だけ・空）は、止められて残ったもので、確定待ちではない。
check_harden_pending() {
  local category="harden.sh の保留" state_dir="${SYSROOT}/var/lib/campus-chime-harden"
  local pending_file line window="" harden steps=() has_ssh=0 has_firewall=0 has_sudo=0
  pending_file="${state_dir}/pending"
  if [ ! -d "${state_dir}" ]; then
    emit OK "${category}" "確定待ちの変更はありません（harden.sh はまだ使われていません）"
    return 0
  fi
  if ! is_root; then
    need_root "${category}"
    return 0
  fi
  if [ -f "${pending_file}" ]; then
    if [ ! -r "${pending_file}" ]; then
      emit 不明 "${category}" "確定待ちの記録（${pending_file#"${SYSROOT}"}）を読めませんでした"
      return 0
    fi
    while IFS= read -r line || [ -n "${line}" ]; do
      case "${line}" in
        ssh=*) has_ssh=1 ;;
        firewall=*) has_firewall=1 ;;
        sudo=*) has_sudo=1 ;;
        window=*) window="${line#window=}" ;;
      esac
    done < "${pending_file}"
  fi
  if [ "${has_ssh}${has_firewall}${has_sudo}" = 000 ]; then
    emit OK "${category}" "確定待ちの変更はありません"
    return 0
  fi
  if [ "${has_ssh}" -eq 1 ]; then steps+=(ssh); fi
  if [ "${has_firewall}" -eq 1 ]; then steps+=(firewall); fi
  if [ "${has_sudo}" -eq 1 ]; then steps+=(sudo); fi
  # 分数が読めないときは、harden.sh の既定（REVERT_AFTER）と同じ 10 分。
  case "${window}" in '' | *[!0-9]*) window=10 ;; esac
  # 直し方に書く harden.sh は、この点検がいるリポジトリの絶対パス（どこで実行しても、そのまま貼れる）。
  harden="$(printf '%q' "${REPO_DIR}/scripts/harden.sh")"
  if [ "${has_ssh}" -eq 1 ] || [ "${has_firewall}" -eq 1 ]; then
    emit 要対応 "${category}" "harden.sh の変更がまだ確定していません（手順: $(join_ja "${steps[@]}")）。確定しないと ${window} 分で自動で元に戻ります"
    fixline "新しい端末（別の画面）を開き、鍵でログインできることを確かめてから、その新しい端末で  sudo bash ${harden} --confirm"
    if [ "${has_sudo}" -eq 1 ]; then
      more "sudo の手順も途中で止まっています。こちらは確定の操作では確定できず、${window} 分以内に自動で元に戻ります（パスワードなしの sudo が戻る）"
    fi
  else
    emit 要対応 "${category}" "sudo の手順が途中で止まりました。数分で自動で元に戻ります（パスワードなしの sudo が戻る）"
    fixline "確定の操作は要りません。自動で戻る（最長 ${window} 分）のを待ち、パスワードなしの sudo が戻ってから、もう一度  sudo bash ${harden} sudo"
  fi
  return 0
}

# -- 実行 ------------------------------------------------------------------------

main() {
  init_context
  log "対象"
  check_user
  # 後の項目が使う、サービスの状態。
  load_unit_states
  if is_up "$(unit_state avahi-daemon.service)"; then AVAHI_ACTIVE=1; fi
  if is_up "$(unit_state chrony.service)"; then CHRONY_ACTIVE=1; fi
  if is_up "$(unit_state systemd-timesyncd.service)"; then TIMESYNCD_ACTIVE=1; fi

  log "ネットワーク（どこから届くか）"
  check_net_addr4
  check_net_addr6
  check_net_listen
  check_net_fw

  log "SSH"
  check_ssh_active
  if [ "${SSH_ON}" -eq 1 ]; then
    check_ssh_auth
    check_ssh_keys
    check_ssh_failed
    check_ssh_logins
  fi

  log "sudo と利用者"
  check_sudo
  check_user_password

  log "OS とカーネルの更新"
  check_os_release
  check_os_kernel
  check_os_reboot
  check_os_updates
  check_os_autoupdate

  log "時刻"
  check_time

  log "使っていないサービス"
  check_services

  log "ログ"
  check_log_persist

  log "時報サービス"
  check_chime_service

  log "リポジトリ"
  check_repo_remote
  check_repo_perms
  check_repo_hooks
  check_repo_stray
  check_py_usersite

  log "harden.sh の状態"
  check_harden_pending

  log "まとめ"
  printf '要対応 %d 件・注意 %d 件・不明 %d 件\n' "${N_NG}" "${N_WARN}" "${N_UNK}"
  # [不明] が 1 件でもあれば、調べ終わっていない。「要対応 0 件」を合格と読まないよう、すぐ下に書く。
  if [ "${N_UNK}" -gt 0 ]; then
    if is_root; then
      printf '\n%s※ 点検は完全ではありません。[不明] の %d 件は調べられていません（理由は上の各行にあります）。[要対応] が 0 件でも、安全と確かめられたわけではありません。%s\n' "${C_BOLD}" "${N_UNK}" "${C_RESET}"
    else
      printf '\n%s※ 点検は完全ではありません。root でないため、[不明] の %d 件を調べられていません。[要対応] が 0 件でも、安全と確かめられたわけではありません。%s\n' "${C_BOLD}" "${N_UNK}" "${C_RESET}"
      printf '  この結果は、確認の合格には使えません。すべて調べるには、sudo を付けて実行してください:\n'
      printf '    sudo bash scripts/security_check.sh\n'
    fi
  fi
  if [ "${GLOBAL_FOUND}" -eq 1 ]; then
    printf '\n%s※ この Pi は、グローバルアドレス（%s）を持っています。%s\n' "${C_BOLD}" "$(join_ja "${GLOBAL_ADDRS[@]}")" "${C_RESET}"
    printf '  学外から届くかどうかは、大学の防火壁の設定しだいで、この点検では確かめられません。\n'
    printf '  「学内からだけ届く」という前提が成り立つかを、必ず学外（スマホのテザリングなど）から確かめてください:\n'
    printf '    nc -vz %s 22\n' "${GLOBAL_ADDRS[0]}"
    printf '  つながるなら、危険度は上の表示より一段高く考えてください（SSH のパスワードログインは「高」）。\n'
  fi
  if [ "${N_NG}" -gt 0 ]; then
    return 1
  fi
  return 0
}

main
