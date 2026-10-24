"""安全点検スクリプト（``scripts/security_check.sh``）のテスト。

security_check.sh は Pi の上で人が手で実行する、**読むだけ**の点検で、CI でも他のテストでも
走らない。運用中の機械の現状を正しく伝えないと、危険を見逃す（または、ありもしない危険で
騒ぐ）ので、次を固定する。

* 項目ごと（``NET-ADDR4`` などの ID は、スクリプトのコメントに書いてある）に、**良い状態と悪い
  状態の両方**で、ラベル（``[OK]``・``[情報]``・``[注意]``・``[要対応]``・``[不明]``）と、肝心の文言・
  直し方（``harden.sh`` の手順の名前）が出ること。表で組んである。
* IPv4 の境界: プライベート（10/8・172.16/12・192.168/16）・共有（100.64/10）・リンクローカル
  （169.254/16）・それ以外（グローバル）。大学が持つクラス B/C のアドレスを Pi が持つことが
  あるので、グローバルアドレスは、学外から届くかを確かめる方法と一緒に、はっきり警告する。
* 終了コード（``[要対応]`` が無ければ 0、あれば 1、使い方の間違いは 2）と、まとめの行。
* root と root でないときの違い（root が要る項目は ``[不明]`` と案内になる）。
* **何も変更しない**こと。偽の ``/``（``CHIME_SYSROOT``）・リポジトリ・``TMPDIR`` の、パス・大きさ・
  更新時刻・権限・中身を、実行の前後で比べる。道具は読み取りだけの本物（許可した名前の一覧）と、
  呼ばれた記録を残す偽物だけで、``cp``・``mv``・``rm``・``mkdir``・``touch``・``chmod``・``mktemp``・``tee`` などは
  PATH に無い。偽物の呼ばれ方も、読む形（``systemctl is-active`` など）に限られることを確かめる。

1 回の実行に 0.2 秒ほどかかるので、このファイルのテストは、いくつかのスレッドで並べて動かす
（``ParallelSuite``。どのテストも自分専用の偽の ``/`` を作り、何も共有しない）。

スクリプトは本物を ``bash`` で動かす。システムに触る道具（``ip``・``ss``・``systemctl``・``nft``・
``sshd``・``sudo``・``journalctl``・``apt-*``・``dpkg-query``・``git`` など）は偽物で、本物のサンプル出力
（Raspberry Pi OS の ``ip -o addr``・``ss -H -lntu``・``sshd -T``・``passwd -S``・``uname -v``・
``apt-cache policy``・``journalctl`` の行）を、用意した答えとして返す。
"""

from __future__ import annotations

import concurrent.futures
import datetime
import gzip
import hashlib
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from typing import Dict, List, NamedTuple, Optional, Tuple

from tests.support import REPO_ROOT

CHECK_SH = os.path.join(REPO_ROOT, "scripts", "security_check.sh")
SERVICE_FILE = os.path.join(REPO_ROOT, "campus_chime.service")
BASH = shutil.which("bash")
#: テストと、表の中の実行を並べる本数（CPU の数まで。多くても 4）。
WORKERS = max(1, min(4, os.cpu_count() or 1))

#: 読み取りだけの本物の道具。スクリプトの PATH にはこれ（とスタブ）しか入れない。書き込める道具
#: （cp・mv・rm・mkdir・touch・chmod・mktemp・tee・ln・dd など）は入れない。
REAL_TOOLS = ("bash", "sh", "env", "cat", "ls", "grep", "awk", "sort", "uniq", "head", "tail", "cut",
              "tr", "wc", "printf", "date", "dirname", "basename", "readlink", "realpath", "cmp",
              "diff", "python3", "getconf", "sleep", "true", "false", "test", "zcat", "gzip", "stat")

#: ``sed`` と ``find`` は書ける形（``sed -i``・``find -delete`` など）を断る入れ物に包む。
WRAPPED_TOOLS = {
    "sed": "-i*|--in-place*",
    "find": "-delete|-exec|-execdir|-ok|-okdir|-fprint|-fprint0|-fprintf|-fls",
}

#: スクリプトの項目の ID と、画面に出る分類の名前（``[ラベル] 分類: …`` の分類）。
CATEGORY = {
    "USER": "利用者",
    "NET-ADDR4": "IPv4 アドレス",
    "NET-ADDR6": "IPv6 アドレス",
    "NET-LISTEN": "待ち受けポート",
    "NET-FW": "防火壁",
    "SSH-ACTIVE": "SSH の稼働",
    "SSH-PASSWORD": "SSH パスワードログイン",
    "SSH-ROOT": "SSH の root ログイン",
    "SSH-KEYS": "SSH 公開鍵",
    "SSH-FAILED": "SSH 失敗ログイン",
    "SSH-LOGINS": "SSH ログイン履歴",
    "SUDO-NOPASSWD": "sudo のパスワード",
    "USER-PASSWORD": "利用者のパスワード",
    "OS-RELEASE": "OS の版",
    "OS-KERNEL": "カーネルの新しさ",
    "OS-REBOOT": "再起動の要否",
    "OS-UPDATES": "更新の状況",
    "OS-AUTOUPDATE": "自動更新",
    "TIME-SYNC": "時刻の同期",
    "TIME-SOURCE": "時刻の同期元",
    "SVC-BLUETOOTH": "Bluetooth",
    "SVC-MODEM": "ModemManager",
    "SVC-AVAHI": "Avahi",
    "SVC-RPICONNECT": "Raspberry Pi Connect",
    "LOG-PERSIST": "ログの保存",
    "CHIME-UNIT": "時報サービスの定義",
    "CHIME-SANDBOX": "時報サービスの隔離",
    "REPO-REMOTE": "リポジトリの取得元",
    "REPO-PERMS": "リポジトリの書き込み権限",
    "REPO-HOOKS": "Git フック",
    "REPO-STRAY": "リポジトリの管理外ファイル",
    "PY-USERSITE": "pip の pygame",
    "HARDEN-PENDING": "harden.sh の保留",
}

#: 項目の ID ごとの、スクリプトに書いてあるはずの ID（コメント）。USER はスクリプトの外の項目。
SCRIPT_IDS = sorted(item for item in CATEGORY if item != "USER")

#: 偽物の先頭。呼ばれた名前と引数を、``CALLS_LOG`` に 1 行で残す。
STUB_PRELUDE = r"""#!/bin/sh
{ printf '%s' "${0##*/}"; for a in "$@"; do printf ' %s' "$a"; done; printf '\n'; } >> "$CALLS_LOG"
refuse() { echo "${0##*/}: 読み取り以外の使い方は断ります: $*" >&2; exit 1; }
out() { [ -f "$1" ] || return 0; while IFS= read -r l || [ -n "$l" ]; do printf '%s\n' "$l"; done < "$1"; }
"""

#: 偽物の本体。用意された答えは ``STUB_DATA`` の中のファイルと、環境変数から返す。
STUB_BODIES = {
    "id": r"""
case "$*" in
  -u) echo "${FAKE_UID:-1000}" ;;
  -un) echo "${FAKE_USER:-pi}" ;;
  "-nG "*) echo "${FAKE_GROUPS:-pi adm sudo audio video}" ;;
  *) refuse "$@" ;;
esac
""",
    "getent": r"""
[ "$1" = passwd ] || refuse "$@"
[ -z "$GETENT_NO_USER" ] || exit 2
echo "$2:x:${PI_UID:-1000}:${PI_UID:-1000}:,,,:/home/$2:/bin/bash"
""",
    "ip": r"""
case "$*" in
  "-4 -o addr show scope global") out "$STUB_DATA/ip4_global" ;;
  "-4 -o addr show scope link") out "$STUB_DATA/ip4_link" ;;
  "-6 -o addr show scope global") out "$STUB_DATA/ip6_global" ;;
  *) refuse "$@" ;;
esac
exit 0
""",
    "ss": r"""
[ "$*" = "-H -lntu" ] || refuse "$@"
[ -z "$SS_FAIL" ] || exit 1
out "$STUB_DATA/ss"
exit 0
""",
    "systemctl": r"""
[ -z "$SYSTEMCTL_BROKEN" ] || exit 1
case "$1" in
  is-active)
    # 尋ねたユニットごとに 1 行で答える。1 つでも動いていれば終了コード 0。
    shift
    status=3
    for unit in "$@"; do
      case " $UNITS_ACTIVE " in *" $unit "*) echo active; status=0 ;; *) echo inactive ;; esac
    done
    exit "$status" ;;
  is-enabled)
    case " $UNITS_ENABLED " in *" $2 "*) echo enabled; exit 0 ;; esac
    case " $UNITS_MASKED " in *" $2 "*) echo masked; exit 1 ;; esac
    echo disabled; exit 1 ;;
  *) refuse "$@" ;;
esac
""",
    "nft": r"""
[ "$*" = "list ruleset" ] || refuse "$@"
[ -z "$NFT_FAIL" ] || { echo "Error: Operation not permitted" >&2; exit 1; }
out "$STUB_DATA/nft"
exit 0
""",
    "sshd": r"""
[ "$1" = -T ] && [ "$2" = -C ] || refuse "$@"
[ -z "$SSHD_FAIL" ] || { echo "Missing privilege separation directory: /run/sshd" >&2; exit 255; }
addr="${3##*addr=}"
if [ -f "$STUB_DATA/sshd_T.$addr" ]; then out "$STUB_DATA/sshd_T.$addr"; else out "$STUB_DATA/sshd_T"; fi
exit 0
""",
    "sudo": r"""
[ "$*" = "-n -l" ] || refuse "$@"
if [ -f "$STUB_DATA/sudo_l_err" ]; then out "$STUB_DATA/sudo_l_err" >&2; fi
if [ -f "$STUB_DATA/sudo_l" ]; then out "$STUB_DATA/sudo_l"; fi
exit "${SUDO_L_RC:-0}"
""",
    "passwd": r"""
[ "$1" = -S ] || refuse "$@"
[ -z "$PASSWD_FAIL" ] || exit 1
out "$STUB_DATA/passwd_S"
exit 0
""",
    "uname": r"""
case "$1" in
  -r) echo "$UNAME_R" ;;
  -v) echo "$UNAME_V" ;;
  *) refuse "$@" ;;
esac
""",
    "uptime": r"""
[ "$1" = -s ] || refuse "$@"
echo "$UPTIME_S"
""",
    "dpkg-query": r"""
case "$*" in
  *"linux-image-*") out "$STUB_DATA/dpkg_linux_image"; exit 0 ;;
  *unattended-upgrades)
    if [ -f "$STUB_DATA/uu_status" ]; then out "$STUB_DATA/uu_status"; exit 0; fi
    echo "dpkg-query: no packages found matching unattended-upgrades" >&2; exit 1 ;;
  *) refuse "$@" ;;
esac
""",
    "dpkg": r"""
[ "$1" = --compare-versions ] && [ "$3" = gt ] || refuse "$@"
[ "$2" != "$4" ] || exit 1
[ "$(printf '%s\n%s\n' "$2" "$4" | sort -V | tail -n 1)" = "$2" ]
""",
    "apt-get": r"""
[ "$*" = "-s -o Debug::NoLocking=1 upgrade" ] || refuse "$@"
out "$STUB_DATA/apt_upgrade_s"
exit 0
""",
    "apt-cache": r"""
[ "$*" = "policy" ] || refuse "$@"
out "$STUB_DATA/apt_policy"
exit 0
""",
    "apt-config": r"""
[ "$*" = "dump" ] || refuse "$@"
out "$STUB_DATA/apt_config"
exit 0
""",
    "timedatectl": r"""
case "$*" in
  "show -p NTPSynchronized --value") echo "$NTP_SYNC" ;;
  "show-timesync -p ServerName --value") echo "$TIMESYNC_SERVER" ;;
  *) refuse "$@" ;;
esac
""",
    "chronyc": r"""
[ "$*" = "-n sources" ] || refuse "$@"
out "$STUB_DATA/chronyc_sources"
exit 0
""",
    "journalctl": r"""
[ "$1" = -u ] && [ "$2" = ssh.service ] && [ "$3" = --since ] && [ "$5" = -o ] && [ "$6" = cat ] || refuse "$@"
[ -z "$JOURNAL_FAIL" ] || exit 1
case "$4" in
  "-7 days") file=journal_7d ;;
  "-30 days") file=journal_30d ;;
  *) refuse "$@" ;;
esac
if [ -f "$STUB_DATA/journal_err" ]; then out "$STUB_DATA/journal_err" >&2; fi
out "$STUB_DATA/$file"
exit 0
""",
    "systemd-analyze": r"""
[ "$*" = "security campus_chime.service --no-pager" ] || refuse "$@"
out "$STUB_DATA/analyze"
exit 0
""",
    "git": r"""
[ "$1" = -c ] && [ "$2" = core.fsmonitor=false ] && [ "$3" = -C ] || refuse "$@"
echo "${GIT_OPTIONAL_LOCKS-unset}" >> "$CALLS_LOG.git-locks"
shift 4
case "$*" in
  "status --porcelain --untracked-files=all")
    [ -z "$GIT_STATUS_FAIL" ] || { echo "fatal: detected dubious ownership in repository" >&2; exit 128; }
    out "$STUB_DATA/git_status"; exit 0 ;;
  "remote get-url origin")
    [ -f "$STUB_DATA/git_remote" ] || { echo "error: No such remote 'origin'" >&2; exit 2; }
    out "$STUB_DATA/git_remote"; exit 0 ;;
  "config --get core.hooksPath")
    [ -f "$STUB_DATA/git_hookspath" ] || exit 1
    out "$STUB_DATA/git_hookspath"; exit 0 ;;
  *) refuse "$@" ;;
esac
""",
    "runuser": r"""
[ "$1" = -u ] && [ "$3" = -- ] || refuse "$@"
shift 3
exec "$@"
""",
    "rpi-connect": r"""
[ "$*" = "status" ] || refuse "$@"
[ -z "$RPI_CONNECT_FAIL" ] || exit 1
out "$STUB_DATA/rpi_connect"
exit 0
""",
}

#: 偽物が受けてよい呼ばれ方（読む形だけ）。これ以外が記録に残ったら、読み取り専用ではない。
ALLOWED_CALLS = [re.compile(pattern) for pattern in (
    r"^id (-u|-un|-nG \S+)$",
    r"^getent passwd \S+$",
    r"^ip -[46] -o addr show scope (global|link)$",
    r"^ss -H -lntu$",
    r"^systemctl is-active ([\w.@-]+ )*[\w.@-]+$",
    r"^systemctl is-enabled [\w.@-]+$",
    r"^nft list ruleset$",
    r"^sshd -T -C user=\S+,host=chime-check,addr=\d+\.\d+\.\d+\.\d+$",
    r"^sudo -n -l$",
    r"^passwd -S \S+$",
    r"^uname -[rv]$",
    r"^uptime -s$",
    r"^dpkg-query -W -f \S+ (linux-image-\*|unattended-upgrades)$",
    r"^dpkg --compare-versions \S+ gt \S+$",
    r"^apt-get -s -o Debug::NoLocking=1 upgrade$",
    r"^apt-cache policy$",
    r"^apt-config dump$",
    r"^timedatectl show(-timesync)? -p \w+ --value( |$)",
    r"^chronyc -n sources$",
    r"^journalctl -u ssh\.service --since -(7|30) days -o cat$",
    r"^systemd-analyze security campus_chime\.service --no-pager$",
    r"^git -c core\.fsmonitor=false -C \S+ (status --porcelain --untracked-files=all|remote get-url origin|config --get core\.hooksPath)$",
    r"^runuser -u \S+ -- (env GIT_OPTIONAL_LOCKS=0 git -c core\.fsmonitor=false -C \S+ .*|rpi-connect status)$",
    r"^rpi-connect status$",
)]

LABEL_LINE = re.compile(r"^\[(OK|情報|注意|要対応|不明)\] ([^:]+): (.*)$")


class Item(NamedTuple):
    """出力の 1 項目: ``[ラベル] 分類: 説明`` と、続く行。"""

    label: str
    category: str
    text: str
    fix: str
    hint: str
    notes: Tuple[str, ...]

    @property
    def everything(self) -> str:
        return "\n".join((self.text, self.fix, self.hint) + self.notes)


def parse_items(stdout: str) -> List[Item]:
    items = []  # type: List[dict]
    for line in stdout.splitlines():
        found = LABEL_LINE.match(line)
        if found:
            items.append(dict(label=found.group(1), category=found.group(2), text=found.group(3),
                              fix="", hint="", notes=[]))
        elif items and line.startswith("    → 直し方: "):
            items[-1]["fix"] = line[len("    → 直し方: "):]
        elif items and line.startswith("    → 確かめ方: "):
            items[-1]["hint"] = line[len("    → 確かめ方: "):]
        elif items and line.startswith("      "):
            items[-1]["notes"].append(line.strip())
    return [Item(entry["label"], entry["category"], entry["text"], entry["fix"], entry["hint"],
                 tuple(entry["notes"])) for entry in items]


class Result:
    """スクリプトを 1 回動かした結果。"""

    def __init__(self, proc: subprocess.CompletedProcess, calls: List[str]):
        self.proc = proc
        self.code = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.calls = calls
        self.items = parse_items(proc.stdout)

    def all(self, item_id: str) -> List[Item]:
        return [item for item in self.items if item.category == CATEGORY[item_id]]

    def one(self, item_id: str) -> Item:
        found = self.all(item_id)
        if len(found) != 1:
            raise AssertionError("{0}（{1}）の行が {2} 個あります:\n{3}".format(
                item_id, CATEGORY[item_id], len(found), self.stdout))
        return found[0]

    def labels(self, item_id: str) -> List[str]:
        return [item.label for item in self.all(item_id)]

    def called(self, prefix: str) -> List[str]:
        return [call for call in self.calls if call.startswith(prefix)]

    def summary(self) -> Tuple[int, int, int]:
        found = re.search(r"要対応 (\d+) 件・注意 (\d+) 件・不明 (\d+) 件", self.stdout)
        if not found:
            raise AssertionError("まとめの行がありません:\n" + self.stdout)
        return int(found.group(1)), int(found.group(2)), int(found.group(3))


def snapshot(*directories: str) -> Dict[str, tuple]:
    """木の中の全部の、パス・種類・大きさ・更新時刻・権限・中身の要約。"""
    state = {}  # type: Dict[str, tuple]
    for top in directories:
        for here, names, files in os.walk(top):
            for name in names + files:
                path = os.path.join(here, name)
                info = os.lstat(path)
                digest = ""
                if os.path.isfile(path) and not os.path.islink(path):
                    with open(path, "rb") as handle:
                        digest = hashlib.sha1(handle.read()).hexdigest()
                state[path] = (info.st_mode, info.st_size, info.st_mtime_ns, digest)
        info = os.lstat(top)
        state[top] = (info.st_mode, 0, info.st_mtime_ns, "")
    return state


def makedirs(path: str) -> None:
    """フォルダを（途中のものも）作る。権限は、umask に関係なく 0755 にする（誰でも書き込める状態にしない）。"""
    if os.path.isdir(path):
        return
    makedirs(os.path.dirname(path))
    os.mkdir(path)
    os.chmod(path, 0o755)


def write(path: str, text: str, mode: int = 0o644) -> str:
    makedirs(os.path.dirname(path))
    if os.path.exists(path):
        # 前に 0440 などで置いたファイルも、置き換えられるようにする。
        os.chmod(path, 0o600)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)
    return path


def set_age(path: str, seconds: float) -> None:
    """ファイルの更新時刻を、いまから ``seconds`` 秒前にする。"""
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


DAY = 86400


def header_block() -> str:
    """security_check.sh 先頭のコメントブロック（``--help`` が表示するはずの範囲）。"""
    block = []
    with open(CHECK_SH, "r", encoding="utf-8") as handle:
        for line in handle.read().splitlines()[1:]:
            if not line.startswith("#"):
                break
            block.append(line)
    return "".join(line + "\n" for line in block)


def ip4_line(cidr: str, scope: str = "global", dev: str = "eth0") -> str:
    """``ip -4 -o addr show`` の本物と同じ形の 1 行。"""
    iface = ipaddress.ip_interface(cidr)
    if scope == "link":
        return ("3: {dev}    inet {cidr} brd {brd} scope link noprefixroute {dev}\\       "
                "valid_lft forever preferred_lft forever\n").format(
                    dev=dev, cidr=cidr, brd=iface.network.broadcast_address)
    return ("2: {dev}    inet {cidr} brd {brd} scope global dynamic noprefixroute {dev}\\       "
            "valid_lft 86399sec preferred_lft 86399sec\n").format(
                dev=dev, cidr=cidr, brd=iface.network.broadcast_address)


def ip6_line(cidr: str, dev: str = "eth0") -> str:
    return ("2: {dev}    inet6 {cidr} scope global dynamic mngtmpaddr noprefixroute \\       "
            "valid_lft 86399sec preferred_lft 14399sec\n").format(dev=dev, cidr=cidr)


#: ``ss -H -lntu`` の本物と同じ形（SSH・mDNS・DHCP・DHCPv6 と、loopback だけの待ち受け）。
SS_NORMAL = (
    "udp   UNCONN 0      0            0.0.0.0:68         0.0.0.0:*\n"
    "udp   UNCONN 0      0            0.0.0.0:5353       0.0.0.0:*\n"
    "udp   UNCONN 0      0               [::]:5353          [::]:*\n"
    "udp   UNCONN 0      0   [fe80::a1b2:c3d4:e5f6:1234]%eth0:546       [::]:*\n"
    "udp   UNCONN 0      0      127.0.0.54:53         0.0.0.0:*\n"
    "udp   UNCONN 0      0   127.0.0.53%lo:53         0.0.0.0:*\n"
    "tcp   LISTEN 0      128        0.0.0.0:22         0.0.0.0:*\n"
    "tcp   LISTEN 0      128           [::]:22            [::]:*\n"
    "tcp   LISTEN 0      4096     127.0.0.1:631        0.0.0.0:*\n"
    "tcp   LISTEN 0      4096         [::1]:631           [::]:*\n"
)

#: ``nft list ruleset``（harden.sh の firewall が作る表の要約）。
NFT_DROP = """table inet campus_chime_filter {
\tset ssh_v4 {
\t\ttype ipv4_addr
\t\tflags interval
\t\telements = { 192.168.1.0/24 }
\t}

\tchain input {
\t\ttype filter hook input priority filter; policy drop;
\t\tiif "lo" accept
\t\tct state established,related accept
\t\tct state invalid drop
\t\tmeta l4proto icmp accept
\t\ttcp dport 22 ip saddr @ssh_v4 accept
\t}

\tchain forward {
\t\ttype filter hook forward priority filter; policy drop;
\t}
}
"""

NFT_ACCEPT = """table inet filter {
\tchain input {
\t\ttype filter hook input priority filter; policy accept;
\t}
\tchain forward {
\t\ttype filter hook forward priority filter; policy accept;
\t}
}
"""

#: ``sshd -T`` の出力（Debian 12 の OpenSSH 9.2 の形。項目名は小文字）。
SSHD_T_TEMPLATE = """port 22
addressfamily any
listenaddress 0.0.0.0:22
listenaddress [::]:22
logingracetime 120
permitrootlogin {root}
strictmodes yes
maxauthtries 6
pubkeyauthentication yes
authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2
hostbasedauthentication no
passwordauthentication {password}
permitemptypasswords no
kbdinteractiveauthentication {kbd}
usepam yes
x11forwarding no
printmotd no
subsystem sftp /usr/lib/openssh/sftp-server
"""

OS_RELEASE_TEMPLATE = """PRETTY_NAME="{pretty}"
NAME="{name}"
VERSION_ID="{version_id}"
VERSION="{version_id} ({codename})"
VERSION_CODENAME={codename}
ID={os_id}
ID_LIKE=debian
HOME_URL="https://www.raspberrypi.com/"
"""

SUDOERS_MAIN = """Defaults\tenv_reset
Defaults\tmail_badpass
Defaults\tsecure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
root\tALL=(ALL:ALL) ALL
%sudo\tALL=(ALL:ALL) ALL
@includedir /etc/sudoers.d
"""

AUTHORIZED_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE0000 tester@pc\n"
AUTHORIZED_RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE0000 tester@laptop\n"
AUTHORIZED_DSS = "ssh-dss AAAAB3NzaC1kc3MAAACBAOEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE0000 old@pc\n"

#: ``apt-cache policy`` の本物の形（Raspberry Pi OS Bookworm。Raspbian と Raspberry Pi の更新元）。
APT_POLICY_RASPBIAN = """Package files:
 100 /var/lib/dpkg/status
     release a=now
 500 http://archive.raspberrypi.com/debian bookworm/main armhf Packages
     release o=Raspberry Pi Foundation,a=stable,n=bookworm,l=Raspberry Pi Foundation,c=main,b=armhf
     origin archive.raspberrypi.com
 500 http://raspbian.raspberrypi.com/raspbian bookworm/rpi armhf Packages
     release o=Raspbian,a=stable,n=bookworm,l=Raspbian,c=rpi,b=armhf
     origin raspbian.raspberrypi.com
 500 http://raspbian.raspberrypi.com/raspbian bookworm/non-free-firmware armhf Packages
     release o=Raspbian,a=stable,n=bookworm,l=Raspbian,c=non-free-firmware,b=armhf
     origin raspbian.raspberrypi.com
 500 http://raspbian.raspberrypi.com/raspbian bookworm/main armhf Packages
     release o=Raspbian,a=stable,n=bookworm,l=Raspbian,c=main,b=armhf
     origin raspbian.raspberrypi.com
Pinned packages:
"""

#: 64 ビット版（Debian の更新元を使う）の ``apt-cache policy``。
APT_POLICY_DEBIAN = """Package files:
 100 /var/lib/dpkg/status
     release a=now
 500 http://deb.debian.org/debian-security bookworm-security/main arm64 Packages
     release v=12,o=Debian,a=stable-security,n=bookworm-security,l=Debian-Security,c=main,b=arm64
     origin deb.debian.org
 500 http://deb.debian.org/debian bookworm/main arm64 Packages
     release v=12.11,o=Debian,a=stable,n=bookworm,l=Debian,c=main,b=arm64
     origin deb.debian.org
 500 http://archive.raspberrypi.com/debian bookworm/main arm64 Packages
     release o=Raspberry Pi Foundation,a=stable,n=bookworm,l=Raspberry Pi Foundation,c=main,b=arm64
     origin archive.raspberrypi.com
Pinned packages:
"""

APT_CONFIG_PERIODIC = """APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "{unattended}";
"""

APT_CONFIG_RASPBIAN = APT_CONFIG_PERIODIC.format(unattended="1") + (
    'Unattended-Upgrade::Origins-Pattern "";\n'
    'Unattended-Upgrade::Origins-Pattern:: "origin=Raspbian,codename=${distro_codename},label=Raspbian";\n'
    'Unattended-Upgrade::Origins-Pattern:: "origin=Raspberry Pi Foundation,codename=${distro_codename},'
    'label=Raspberry Pi Foundation";\n'
    'Unattended-Upgrade::Automatic-Reboot "false";\n')

#: Debian 既定の指定だけ（Raspbian の更新元には当たらない）。
APT_CONFIG_DEBIAN_ONLY = APT_CONFIG_PERIODIC.format(unattended="1") + (
    'Unattended-Upgrade::Origins-Pattern "";\n'
    'Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename},label=Debian";\n'
    'Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename}-security,'
    'label=Debian-Security";\n')

CHRONY_SOURCES = """MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
^* 192.0.2.10                    2   6   377    35   +123us[ +456us] +/-   20ms
^+ 192.0.2.11                    2   6   377    36    -95us[ -120us] +/-   25ms
^- 192.0.2.12                    3   6   377    35   +1100us[+1100us] +/-   40ms
"""

SYSTEMD_ANALYZE_EXPOSED = """UNIT                         EXPOSURE PREDICATE                  HAPPY
✓ PrivateNetwork=              0.5  Service has access to the host's network
✗ User=/DynamicUser=           0.4  Service runs as root user
→ Overall exposure level for campus_chime.service: 8.7 EXPOSED \U0001F625
"""

SYSTEMD_ANALYZE_SAFER = ("→ Overall exposure level for campus_chime.service: 3.1 OK \U0001F642\n")

#: 新しいカーネル（KERNEL_FIXES_SINCE より後）と、古いカーネル。
KERNEL_NEW = ("6.12.47+rpt-rpi-v8", "#1 SMP PREEMPT Debian 1:6.12.47-1+rpt1 (2026-08-12)")
KERNEL_OLD = ("6.12.25+rpt-rpi-v8", "#1 SMP PREEMPT Debian 1:6.12.25-1+rpt1 (2025-04-30)")

JOURNAL_QUIET = """Server listening on 0.0.0.0 port 22.
Server listening on :: port 22.
Accepted publickey for pi from 10.0.0.9 port 50100 ssh2: ED25519 SHA256:examplefingerprintexamplefingerprint00
pam_unix(sshd:session): session opened for user pi(uid=1000) by (uid=0)
Received disconnect from 10.0.0.9 port 50100:11: disconnected by user
"""

JOURNAL_ATTACKED = """Server listening on 0.0.0.0 port 22.
Failed password for pi from 10.0.0.5 port 50000 ssh2
Failed password for pi from 10.0.0.5 port 50002 ssh2
Invalid user admin from 198.51.100.7 port 4321
Failed password for invalid user admin from 198.51.100.7 port 4321 ssh2
Connection closed by invalid user admin 198.51.100.7 port 4321 [preauth]
Accepted publickey for pi from 10.0.0.9 port 50100 ssh2: ED25519 SHA256:examplefingerprintexamplefingerprint00
"""

HINT_NOT_SEEING = ("Hint: You are currently not seeing messages from other users and the system.\n"
                   "      Users in groups 'adm', 'systemd-journal' can see all messages.\n"
                   "      Pass -q to turn off this notice.\n")


class SecurityCheckCase(unittest.TestCase):
    """使い捨ての偽の ``/``・リポジトリ・偽の道具を作り、スクリプトを動かす。

    はじめの状態は「すべて良い」Pi（Raspberry Pi OS Bookworm 32 ビット。プライベートアドレス、
    鍵だけの SSH、防火壁、最近の更新、新しいカーネル、chrony、隔離済みのサービスなど）。
    各テストが、見たい項目だけを悪くして動かす。
    """

    def setUp(self):
        self.build_world()

    def build_world(self):
        """偽の ``/``・リポジトリ・道具を作り、すべて良い状態にする。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "root")
        self.data = os.path.join(self.tmp, "stubdata")
        self.repo = os.path.join(self.tmp, "repo")
        self.tmpdir = os.path.join(self.tmp, "tmpdir")
        self.elsewhere = os.path.join(self.tmp, "elsewhere")
        self.bin_dir = os.path.join(self.tmp, "bin")
        self.tools_dir = os.path.join(self.tmp, "tools")
        self.calls_log = os.path.join(self.tmp, "calls.log")
        for directory in (self.root, self.data, self.repo, self.tmpdir, self.elsewhere,
                          self.bin_dir, self.tools_dir):
            makedirs(directory)
        self.env_extra = {}  # type: Dict[str, str]
        self.units_active = ["ssh", "avahi-daemon", "chrony"]
        self.units_enabled = []  # type: List[str]
        self.units_masked = []  # type: List[str]
        self.now = time.time()
        self.make_tools()
        self.make_stubs()
        self.make_repo()
        self.make_good_world()

    # -- 道具 ---------------------------------------------------------------
    def make_tools(self):
        for name in REAL_TOOLS:
            real = shutil.which(name)
            if real:
                os.symlink(real, os.path.join(self.tools_dir, name))
        for name, refused in WRAPPED_TOOLS.items():
            real = shutil.which(name)
            self.assertTrue(real, name + " が無い環境では動かせない")
            write(os.path.join(self.tools_dir, name), (
                "#!/bin/sh\nfor a in \"$@\"; do\n  case \"$a\" in\n    {refused}) "
                "echo \"{name}: 書き込める形は断ります: $a\" >&2; exit 1 ;;\n  esac\ndone\n"
                "exec {real} \"$@\"\n").format(refused=refused, name=name, real=real), 0o755)

    def make_stubs(self):
        for name, body in STUB_BODIES.items():
            write(os.path.join(self.bin_dir, name), STUB_PRELUDE + body, 0o755)

    def remove_tool(self, name: str) -> None:
        """道具を 1 つ無くす（入っていない機械の再現）。"""
        for directory in (self.bin_dir, self.tools_dir):
            path = os.path.join(directory, name)
            if os.path.lexists(path):
                os.remove(path)

    # -- 使い捨てのリポジトリと偽の / --------------------------------------
    def make_repo(self):
        shutil.copy(SERVICE_FILE, write(os.path.join(self.repo, "campus_chime.service"), ""))
        shutil.copy(CHECK_SH, write(os.path.join(self.repo, "scripts", "security_check.sh"), "", 0o755))
        write(os.path.join(self.repo, "campus_chime.py"), "# 偽物\n")
        write(os.path.join(self.repo, ".git", "hooks", "pre-commit.sample"), "#!/bin/sh\n", 0o755)
        write(os.path.join(self.repo, ".git", "config"), "[core]\n\trepositoryformatversion = 0\n")

    def sys(self, path: str) -> str:
        """偽の ``/`` の中のパス。"""
        return os.path.join(self.root, path.lstrip("/"))

    def put(self, path: str, text: str, mode: int = 0o644) -> str:
        return write(self.sys(path), text, mode)

    def stub_data(self, name: str, text: Optional[str]) -> None:
        path = os.path.join(self.data, name)
        if text is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            write(path, text)

    def set_os(self, codename: str = "bookworm", os_id: str = "raspbian", version_id: str = "12"):
        pretty = "{0} GNU/Linux {1} ({2})".format("Raspbian" if os_id == "raspbian" else "Debian",
                                                  version_id, codename)
        self.put("/etc/os-release", OS_RELEASE_TEMPLATE.format(
            pretty=pretty, name=pretty.split(" (")[0].rsplit(" ", 1)[0], version_id=version_id,
            codename=codename, os_id=os_id))

    def set_kernel(self, release: str, version: str):
        self.env_extra["UNAME_R"] = release
        self.env_extra["UNAME_V"] = version
        modules = self.sys("/lib/modules")
        shutil.rmtree(modules, ignore_errors=True)
        makedirs(os.path.join(modules, release))

    def set_history(self, upgrade_days: Optional[float], install_days: float = 3):
        """``history.log``: 最後の upgrade が ``upgrade_days`` 日前（None なら upgrade の記録なし）。"""
        def stamp(days: float) -> str:
            return datetime.datetime.fromtimestamp(self.now - days * DAY).strftime("%Y-%m-%d  %H:%M:%S")
        blocks = []
        if upgrade_days is not None:
            blocks.append("Start-Date: {0}\nCommandline: /usr/bin/unattended-upgrade\n"
                          "Upgrade: libssl3:armhf (3.0.15-1~deb12u1, 3.0.16-1~deb12u1)\n"
                          "End-Date: {0}\n".format(stamp(upgrade_days)))
        blocks.append("Start-Date: {0}\nCommandline: apt install -y vim\nRequested-By: pi (1000)\n"
                      "Install: vim:armhf (2:9.0.1378-2)\nEnd-Date: {0}\n".format(stamp(install_days)))
        self.put("/var/log/apt/history.log", "\n".join(blocks))

    def set_lists_age(self, days: float):
        directory = self.sys("/var/lib/apt/lists")
        shutil.rmtree(directory, ignore_errors=True)
        for name in ("raspbian.raspberrypi.com_raspbian_dists_bookworm_InRelease",
                     "archive.raspberrypi.com_debian_dists_bookworm_InRelease"):
            path = write(os.path.join(directory, name), "-----BEGIN PGP SIGNED MESSAGE-----\n")
            set_age(path, days * DAY)
        write(os.path.join(directory, "lock"), "")

    def make_good_world(self):
        self.set_os()
        self.set_kernel(*KERNEL_NEW)
        self.stub_data("ip4_global", ip4_line("192.168.1.20/24"))
        self.stub_data("ip4_link", "")
        self.stub_data("ip6_global", "")
        self.stub_data("ss", SS_NORMAL)
        self.stub_data("nft", NFT_DROP)
        self.stub_data("sshd_T", SSHD_T_TEMPLATE.format(root="without-password", password="no", kbd="no"))
        self.stub_data("sudo_l_err", "sudo: a password is required\n")
        self.env_extra["SUDO_L_RC"] = "1"
        self.stub_data("passwd_S", "pi P 2026-01-15 0 99999 7 -1\n")
        self.stub_data("dpkg_linux_image",
                       "ii |linux-image-6.12.47+rpt-rpi-v8|1:6.12.47-1+rpt1\n"
                       "ii |linux-image-rpi-v8|1:6.12.47-1+rpt1\n")
        self.stub_data("uu_status", "install ok installed")
        self.stub_data("apt_upgrade_s", "Reading package lists...\nBuilding dependency tree...\n"
                       "Reading state information...\nCalculating upgrade...\n"
                       "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n")
        self.stub_data("apt_policy", APT_POLICY_RASPBIAN)
        self.stub_data("apt_config", APT_CONFIG_RASPBIAN)
        self.stub_data("chronyc_sources", CHRONY_SOURCES)
        self.stub_data("journal_7d", JOURNAL_QUIET)
        self.stub_data("journal_30d", JOURNAL_QUIET)
        self.stub_data("analyze", SYSTEMD_ANALYZE_SAFER)
        self.stub_data("git_status", "")
        self.stub_data("git_remote", "https://github.com/hiroyuki-rdx/chime-5pm.git\n")
        self.env_extra["NTP_SYNC"] = "yes"
        self.env_extra["UPTIME_S"] = datetime.datetime.fromtimestamp(
            self.now - 3 * DAY).strftime("%Y-%m-%d %H:%M:%S")
        # 偽の /
        self.put("/etc/ssh/sshd_config", "Include /etc/ssh/sshd_config.d/*.conf\n\n#Port 22\n"
                 "KbdInteractiveAuthentication no\nUsePAM yes\nX11Forwarding yes\nPrintMotd no\n"
                 "AcceptEnv LANG LC_*\nSubsystem\tsftp\t/usr/lib/openssh/sftp-server\n")
        self.put("/etc/ssh/sshd_config.d/01-campus-chime-harden.conf",
                 "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n"
                 "PubkeyAuthentication yes\nAuthenticationMethods publickey\nX11Forwarding no\n")
        self.put("/etc/sudoers", SUDOERS_MAIN, 0o440)
        self.put("/etc/sudoers.d/README", "# sudoers.d の説明\n", 0o440)
        shutil.copy(SERVICE_FILE, write(self.sys("/etc/systemd/system/campus_chime.service"), ""))
        self.put("/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf",
                 "[Service]\nProtectSystem=strict\n")
        makedirs(self.sys("/var/log/journal"))
        makedirs(self.sys("/run"))
        self.set_lists_age(1)
        self.set_history(2)
        self.put("/home/pi/.ssh/authorized_keys", AUTHORIZED_KEY, 0o600)
        # rpi-connect は入っていない機械から始める（入れるテストは install_stub する）。
        self.remove_tool("rpi-connect")

    def make_bad_world(self) -> None:
        """ほぼすべての項目を悪くする（良い状態から、1 つずつ崩す）。"""
        self.stub_data("ip4_global", ip4_line("203.0.113.7/24"))
        self.stub_data("ip6_global", ip6_line("2001:db8::10/64"))
        self.stub_data("ss", SS_NORMAL + "tcp   LISTEN 0      511        0.0.0.0:8080       0.0.0.0:*\n")
        self.stub_data("nft", NFT_ACCEPT)
        self.stub_data("sshd_T", SSHD_T_TEMPLATE.format(root="yes", password="yes", kbd="yes"))
        self.set_sshd_config("")
        os.remove(self.sys("/home/pi/.ssh/authorized_keys"))
        self.stub_data("journal_7d", JOURNAL_ATTACKED)
        self.put("/etc/sudoers.d/010_pi-nopasswd", SUDO_NOPASSWD_ALL, 0o440)
        self.stub_data("sudo_l", SUDO_L_NOPASSWD_ALL)
        self.stub_data("sudo_l_err", None)
        self.env_extra["SUDO_L_RC"] = "0"
        self.stub_data("passwd_S", "pi NP 2026-01-15 0 99999 7 -1\n")
        self.set_os("bullseye", "raspbian", "11")
        self.set_kernel(*KERNEL_OLD)
        self.put("/run/reboot-required", "")
        self.set_lists_age(80)
        self.set_history(80)
        self.stub_data("uu_status", None)
        self.env_extra["NTP_SYNC"] = "no"
        self.env_extra["UPTIME_S"] = datetime.datetime.fromtimestamp(
            self.now - 90 * DAY).strftime("%Y-%m-%d %H:%M:%S")
        self.set_units(active=["ssh", "avahi-daemon", "systemd-timesyncd", "bluetooth", "ModemManager"],
                       enabled=["hciuart"])
        self.install_stub("rpi-connect")
        self.stub_data("rpi_connect", "Signed in: yes\nScreen sharing: not allowed\nRemote shell: allowed\n")
        shutil.rmtree(self.sys("/var/log/journal"))
        with open(SERVICE_FILE, "r", encoding="utf-8") as handle:
            unit = handle.read()
        self.put("/etc/systemd/system/campus_chime.service", unit.replace("User=pi", "User=root"))
        os.remove(self.sys("/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf"))
        self.stub_data("analyze", SYSTEMD_ANALYZE_EXPOSED)
        self.stub_data("git_remote", "http://github.com/hiroyuki-rdx/chime-5pm.git\n")
        os.chmod(os.path.join(self.repo, "campus_chime.py"), 0o666)
        write(os.path.join(self.repo, ".git", "hooks", "post-merge"), "#!/bin/sh\n", 0o755)
        self.stub_data("git_status", "?? evil.py\n")
        makedirs(self.sys("/home/pi/.local/lib/python3.11/site-packages/pygame"))
        self.put("/var/lib/campus-chime-harden/pending", "ssh 1\n", 0o600)

    def install_stub(self, name: str) -> None:
        write(os.path.join(self.bin_dir, name), STUB_PRELUDE + STUB_BODIES[name], 0o755)

    def set_units(self, active: Optional[List[str]] = None, enabled: Optional[List[str]] = None,
                  masked: Optional[List[str]] = None) -> None:
        if active is not None:
            self.units_active = active
        if enabled is not None:
            self.units_enabled = enabled
        if masked is not None:
            self.units_masked = masked

    def set_sshd_config(self, main: str, drop_ins: Optional[Dict[str, str]] = None) -> None:
        """root でないときの推定が読む設定ファイル（sshd_config と sshd_config.d の中）を置き直す。"""
        shutil.rmtree(self.sys("/etc/ssh"), ignore_errors=True)
        self.put("/etc/ssh/sshd_config", main)
        makedirs(self.sys("/etc/ssh/sshd_config.d"))
        for name, text in (drop_ins or {}).items():
            self.put("/etc/ssh/sshd_config.d/" + name, text)

    # -- 実行 ---------------------------------------------------------------
    def environment(self, root: bool, extra: Dict[str, str]) -> Dict[str, str]:
        env = {
            "PATH": self.bin_dir + os.pathsep + self.tools_dir,
            "HOME": self.tmp,
            "TMPDIR": self.tmpdir,
            "CHIME_SYSROOT": self.root,
            "CALLS_LOG": self.calls_log,
            "STUB_DATA": self.data,
            "FAKE_UID": "0" if root else "1000",
            "FAKE_USER": "root" if root else "pi",
            # 偽の getent が返す uid。リポジトリのファイルの持ち主（このテストを動かす人）に合わせる。
            "PI_UID": str(os.getuid()),
            "UNITS_ACTIVE": " ".join(self.unit_names(self.units_active)),
            "UNITS_ENABLED": " ".join(self.unit_names(self.units_enabled)),
            "UNITS_MASKED": " ".join(self.unit_names(self.units_masked)),
            "TIMESYNC_SERVER": "",
        }
        if "TZ" in os.environ:
            env["TZ"] = os.environ["TZ"]
        env.update(self.env_extra)
        env.update(extra)
        return env

    @staticmethod
    def unit_names(names: List[str]) -> List[str]:
        return [name if "." in name else name + ".service" for name in names]

    def run_check(self, *args: str, root: bool = True, **extra: str) -> Result:
        """リポジトリの外から、リポジトリの中の security_check.sh を動かす。"""
        if os.path.exists(self.calls_log):
            os.remove(self.calls_log)
        proc = subprocess.run(
            [BASH, os.path.join(self.repo, "scripts", "security_check.sh")] + list(args),
            capture_output=True, text=True, encoding="utf-8", cwd=self.elsewhere,
            env=self.environment(root, extra), timeout=60)
        calls = []
        if os.path.exists(self.calls_log):
            with open(self.calls_log, "r", encoding="utf-8") as handle:
                calls = handle.read().splitlines()
        return Result(proc, calls)

    def run_variants(self, variants, root: bool = True):
        """名前と変更の組ごとに、別々の新しい世界を作って変更し、スクリプトを動かす（4 本まで並列）。

        ``variants`` は ``[(名前, 変更)]``。変更は ``変更(世界)`` の形で、世界（このテストと同じ種類）の
        状態を直し、``run_check`` に渡す環境変数の辞書（なければ None）を返す。返り値は ``[(名前, 結果)]``
        （入れた順）。表で組んだテストが多く、1 回の実行に 0.25 秒ほどかかるので、並べて動かす。
        """
        def work(variant):
            name, change = variant
            world = type(self)(self._testMethodName)
            world.build_world()
            try:
                extra = change(world) or {}
                return name, world.run_check(root=root, **extra)
            finally:
                world.doCleanups()

        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            return list(pool.map(work, variants))

    def check_item(self, item_id: str, label: str, *phrases: str, root: bool = True,
                   result: Optional[Result] = None, **extra: str) -> Item:
        """項目 ``item_id`` が 1 行で、ラベルが ``label``、説明に ``phrases`` を全部含むこと。"""
        result = result or self.run_check(root=root, **extra)
        item = result.one(item_id)
        self.assertEqual(item.label, label, "{0}: {1}\n{2}".format(item_id, item.label, result.stdout))
        for phrase in phrases:
            self.assertIn(phrase, item.everything, "{0} に「{1}」がありません:\n{2}".format(
                item_id, phrase, item.everything))
        return item


class HelpAndUsageTest(SecurityCheckCase):
    def test_help_prints_exactly_the_header_block(self):
        expected = header_block()
        self.assertIn("bash scripts/security_check.sh", expected)
        self.assertNotIn("set -euo pipefail", expected)
        result = self.run_check("--help")
        self.assertEqual(result.code, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.calls, [])

    def test_short_help_is_the_same(self):
        self.assertEqual(self.run_check("-h").stdout, header_block())

    def test_an_unknown_option_stops_with_exit_2_before_anything_runs(self):
        for argument in (["--bogus"], ["--user", "pi", "--bogus"], ["extra"], ["-x"]):
            with self.subTest(arguments=argument):
                result = self.run_check(*argument)
                self.assertEqual(result.code, 2, result.stderr)
                self.assertIn("不明なオプション: {0}".format(argument[-1]), result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.calls, [])

    def test_user_needs_a_valid_name(self):
        for argument in (["--user"], ["--user="], ["--user", "bad name"], ["--user", "Pi"],
                         ["--user=a;b"], ["--user", "$(id)"], ["--user", "-x"]):
            with self.subTest(arguments=argument):
                result = self.run_check(*argument)
                self.assertEqual(result.code, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.calls, [])

    def test_user_selects_the_account_that_is_checked(self):
        self.put("/home/chime/.ssh/authorized_keys", AUTHORIZED_RSA, 0o600)
        for argument in (["--user", "chime"], ["--user=chime"]):
            with self.subTest(arguments=argument):
                result = self.run_check(*argument)
                self.assertIn("sshd -T -C user=chime,host=chime-check,addr=203.0.113.1", result.calls)
                self.assertIn("passwd -S chime", result.calls)
                self.assertIn("id -nG chime", result.calls)
                self.assertIn("ssh-rsa が 1 個", result.one("SSH-KEYS").text)
                self.assertIn("chime", result.one("USER").text)

    def test_the_case_patterns_are_readable_by_the_docs_test(self):
        # tests/test_docs.py の shell_options() と同じ読み方で、長いオプションが拾えること。
        with open(CHECK_SH, "r", encoding="utf-8") as handle:
            text = handle.read()
        found = set(re.findall(r"^\s+(?:-[a-z]\|)?(--[a-z][a-z-]*)\)", text, re.MULTILINE))
        self.assertEqual(found, {"--help", "--user"})



class AllGoodTest(SecurityCheckCase):
    """すべて良い Pi では、何も指摘せず、終了コード 0 で終わる。"""

    def test_a_healthy_pi_has_no_findings_as_root(self):
        result = self.run_check()
        self.assertEqual(result.code, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.summary(), (0, 0, 0))
        for item_id in CATEGORY:
            with self.subTest(item=item_id):
                if item_id == "SVC-RPICONNECT":
                    # rpi-connect が入っていなければ、何も表示しない。
                    self.assertEqual(result.all(item_id), [])
                    continue
                self.assertEqual(len(result.all(item_id)), 1, result.stdout)
                self.assertIn(result.one(item_id).label, ("OK", "情報"), result.stdout)
        self.assertNotIn("※", result.stdout)

    def test_a_healthy_pi_as_a_normal_user_only_misses_the_root_items(self):
        result = self.run_check(root=False)
        self.assertEqual(result.code, 0, result.stdout + result.stderr)
        self.assertEqual(result.summary(), (0, 0, 2))
        unknown = sorted(item.category for item in result.items if item.label == "不明")
        self.assertEqual(unknown, sorted([CATEGORY["NET-FW"], CATEGORY["USER-PASSWORD"]]))
        for item_id in ("NET-FW", "USER-PASSWORD"):
            self.assertEqual(result.one(item_id).hint, "sudo bash scripts/security_check.sh")
        self.assertIn("sudo bash scripts/security_check.sh", result.stdout.splitlines()[-1])
        self.assertEqual(result.called("sudo"), ["sudo -n -l"])
        self.assertEqual(result.called("nft"), [])
        self.assertEqual(result.called("passwd"), [])
        self.assertEqual(result.called("sshd"), [])

    def test_the_root_run_has_no_hint_and_never_calls_sudo(self):
        result = self.run_check()
        self.assertNotIn("root でないため", result.stdout)
        self.assertEqual(result.called("sudo"), [])

    def test_every_item_id_is_written_as_a_comment_next_to_its_check(self):
        with open(CHECK_SH, "r", encoding="utf-8") as handle:
            text = handle.read()
        for item_id in SCRIPT_IDS:
            with self.subTest(item=item_id):
                self.assertTrue(re.search(r"(?m)^\s*#.*\b{0}\b".format(re.escape(item_id)), text),
                                "コメントに {0} がありません".format(item_id))
        # 表示する分類の名前も、スクリプトにある。
        for item_id, category in CATEGORY.items():
            with self.subTest(category=item_id):
                self.assertIn('"{0}"'.format(category), text)

    def test_the_user_line_reports_an_unknown_account(self):
        result = self.run_check(GETENT_NO_USER="1")
        item = result.one("USER")
        self.assertEqual(item.label, "不明")
        self.assertIn("ユーザー pi が見つかりません", item.text)
        self.assertIn("--user", item.text)


#: IPv4 アドレスの境界: (CIDR, ip の scope, ラベル, 文言)
IPV4_CASES = [
    # プライベート（10/8・172.16/12・192.168/16）
    ("10.0.0.5/24", "global", "OK", "プライベートアドレス"),
    ("10.255.255.254/8", "global", "OK", "プライベートアドレス"),
    ("172.16.0.1/12", "global", "OK", "プライベートアドレス"),
    ("172.31.255.254/16", "global", "OK", "プライベートアドレス"),
    ("192.168.0.1/24", "global", "OK", "プライベートアドレス"),
    ("192.168.255.254/16", "global", "OK", "プライベートアドレス"),
    # 共有アドレス空間（100.64/10。CGNAT）
    ("100.64.0.1/10", "global", "OK", "共有アドレス空間"),
    ("100.127.255.254/10", "global", "OK", "共有アドレス空間"),
    # リンクローカル（169.254/16。DHCP の失敗）
    ("169.254.10.20/16", "link", "注意", "DHCP"),
    ("169.254.0.1/16", "link", "注意", "DHCP"),
    # 範囲のすぐ外は、グローバル
    ("9.255.255.254/8", "global", "注意", "グローバルアドレス"),
    ("11.0.0.1/8", "global", "注意", "グローバルアドレス"),
    ("172.15.255.254/16", "global", "注意", "グローバルアドレス"),
    ("172.32.0.1/16", "global", "注意", "グローバルアドレス"),
    ("192.167.255.254/16", "global", "注意", "グローバルアドレス"),
    ("192.169.0.1/16", "global", "注意", "グローバルアドレス"),
    ("100.63.255.254/16", "global", "注意", "グローバルアドレス"),
    ("100.128.0.1/16", "global", "注意", "グローバルアドレス"),
    ("169.253.255.254/16", "global", "注意", "グローバルアドレス"),
    ("169.255.0.1/16", "global", "注意", "グローバルアドレス"),
    # 大学が持つクラス B / C のブロックにありそうなアドレス（文書用の範囲で代用）
    ("203.0.113.7/24", "global", "注意", "グローバルアドレス"),
    ("198.51.100.9/24", "global", "注意", "グローバルアドレス"),
    ("130.54.12.34/16", "global", "注意", "グローバルアドレス"),
]


class Ipv4AddressTest(SecurityCheckCase):
    """NET-ADDR4: アドレスを分類し、グローバルなら学外から届くかの確かめ方を示す。"""

    def set_address(self, cidr: str, scope: str) -> None:
        self.stub_data("ip4_global", ip4_line(cidr) if scope == "global" else "")
        self.stub_data("ip4_link", ip4_line(cidr, "link", "wlan0") if scope == "link" else "")

    def test_each_range_and_its_boundaries(self):
        # 全部のアドレスを一度に持たせる。1 つのアドレスが 1 行になり、順番は ip の出力どおり
        # （scope global のあと scope link）。
        global_cases = [case for case in IPV4_CASES if case[1] == "global"]
        link_cases = [case for case in IPV4_CASES if case[1] == "link"]
        self.stub_data("ip4_global", "".join(ip4_line(case[0]) for case in global_cases))
        self.stub_data("ip4_link", "".join(ip4_line(case[0], "link", "wlan0") for case in link_cases))
        result = self.run_check()
        items = result.all("NET-ADDR4")
        self.assertEqual(len(items), len(IPV4_CASES), result.stdout)
        for (cidr, scope, label, phrase), item in zip(global_cases + link_cases, items):
            with self.subTest(address=cidr):
                self.assertEqual(item.label, label, item.text)
                self.assertIn(phrase, item.text)
                self.assertIn(cidr, item.text)
                if label == "OK":
                    self.assertIn("学外から直接は届きません", item.text)
                    self.assertEqual(item.fix, "")
                elif scope == "global":
                    address = cidr.split("/")[0]
                    self.assertIn("クラス B/C", item.text)
                    self.assertIn("学外から届くかどうかは大学の防火壁しだい", item.text)
                    self.assertIn("nc -vz {0} 22".format(address), item.fix)
                    self.assertIn("スマホのテザリング", item.fix)
                    self.assertIn("情報基盤", item.fix)
                    self.assertTrue(any("パスワードログインの危険度は「高」" in note for note in item.notes), item.notes)
                else:
                    self.assertIn("169.254.0.0/16", item.text)
                    self.assertIn("DHCP", item.fix)
        # 注意だけなら、終了コードは 0。まとめは、見つけたグローバルアドレスをすべて挙げる。
        self.assertEqual(result.code, 0)
        addresses = [case[0].split("/")[0] for case in global_cases if case[2] == "注意"]
        self.assertIn("グローバルアドレス（{0}）を持っています".format("、".join(addresses)), result.stdout)

    def test_the_reminder_appears_only_for_a_global_address(self):
        private = self.run_check()
        self.assertNotIn("※", private.stdout)
        self.assertNotIn("必ず学外", private.stdout)
        self.set_address("203.0.113.7/24", "global")
        result = self.run_check()
        summary = result.stdout.split("== まとめ")[1]
        self.assertIn("※ この Pi は、グローバルアドレス（203.0.113.7）を持っています。", summary)
        self.assertIn("大学の防火壁の設定しだいで、この点検では確かめられません", summary)
        self.assertIn("必ず学外（スマホのテザリングなど）から確かめてください", summary)
        self.assertIn("nc -vz 203.0.113.7 22", summary)
        self.assertIn("危険度は上の表示より一段高く", summary)
        self.assertEqual(result.code, 0)

    def test_every_address_is_reported_on_its_own_line(self):
        self.stub_data("ip4_global", ip4_line("192.168.1.20/24") + ip4_line("203.0.113.7/24", dev="wlan0"))
        self.stub_data("ip4_link", ip4_line("169.254.3.4/16", "link", "usb0"))
        result = self.run_check()
        lines = result.all("NET-ADDR4")
        self.assertEqual([item.label for item in lines], ["OK", "注意", "注意"])
        self.assertIn("192.168.1.20/24", lines[0].text)
        self.assertIn("203.0.113.7/24", lines[1].text)
        self.assertIn("169.254.3.4/16", lines[2].text)

    def test_no_ipv4_address_at_all_is_a_warning(self):
        self.stub_data("ip4_global", "")
        item = self.check_item("NET-ADDR4", "注意", "IPv4 アドレスがありません")
        self.assertIn("DHCP", item.fix)

    def test_ip_missing_or_failing_is_unknown(self):
        self.remove_tool("ip")
        result = self.run_check()
        self.assertEqual(result.labels("NET-ADDR4"), ["不明"])
        self.assertEqual(result.labels("NET-ADDR6"), ["不明"])

    def test_the_link_scope_is_read_too(self):
        self.stub_data("ip4_global", "")
        self.stub_data("ip4_link", ip4_line("169.254.7.8/16", "link"))
        result = self.run_check()
        self.assertIn("ip -4 -o addr show scope link", result.calls)
        self.assertEqual(result.labels("NET-ADDR4"), ["注意"])

    def test_a_global_address_raises_the_ssh_password_danger_to_high(self):
        self.stub_data("sshd_T", SSHD_T_TEMPLATE.format(root="no", password="yes", kbd="no"))
        private = self.check_item("SSH-PASSWORD", "要対応", "危険度は「中」")
        self.assertNotIn("高", private.text)
        self.stub_data("ip4_global", ip4_line("203.0.113.7/24"))
        public = self.check_item("SSH-PASSWORD", "要対応", "危険度は「高」", "学外から届くなら「高」")
        self.assertIn("harden.sh ssh", public.fix)


class Ipv6AddressTest(SecurityCheckCase):
    """NET-ADDR6: ULA は学内専用、それ以外のグローバルアドレスは注意。"""

    def test_no_global_ipv6_address_is_fine(self):
        self.check_item("NET-ADDR6", "OK", "IPv6 のグローバルアドレスはありません")

    def test_ranges(self):
        cases = [
            ("fd12:3456:789a:1::10/64", "OK", "ユニークローカル"),
            ("fc00::5/7", "OK", "ユニークローカル"),
            ("fdff:ffff::1/48", "OK", "ユニークローカル"),
            ("fbff:ffff::1/64", "注意", "グローバルアドレス"),
            ("fe00::1/64", "注意", "グローバルアドレス"),
            ("2001:db8:1:2::10/64", "注意", "グローバルアドレス"),
            ("2400:1234:5678::9/64", "注意", "グローバルアドレス"),
            ("2001:db8::1/128", "注意", "グローバルアドレス"),
        ]
        self.stub_data("ip6_global", "".join(ip6_line(case[0]) for case in cases))
        result = self.run_check()
        items = result.all("NET-ADDR6")
        self.assertEqual(len(items), len(cases), result.stdout)
        for (cidr, label, phrase), item in zip(cases, items):
            with self.subTest(address=cidr):
                self.assertEqual(item.label, label, item.text)
                self.assertIn(phrase, item.text)
                self.assertIn(cidr, item.text)
                if label == "注意":
                    self.assertIn("nc -vz {0} 22".format(cidr.split("/")[0]), item.fix)
        globals_found = [case[0].split("/")[0] for case in cases if case[1] == "注意"]
        self.assertIn("グローバルアドレス（{0}）を持っています".format("、".join(globals_found)), result.stdout)

    def test_several_addresses_get_a_line_each(self):
        self.stub_data("ip6_global", ip6_line("fd00::10/64") + ip6_line("2001:db8::10/64", "wlan0"))
        self.assertEqual(self.run_check().labels("NET-ADDR6"), ["OK", "注意"])

    def test_a_global_ipv6_address_raises_the_ssh_password_danger_to_high(self):
        self.stub_data("sshd_T", SSHD_T_TEMPLATE.format(root="no", password="yes", kbd="no"))
        self.stub_data("ip6_global", ip6_line("2001:db8::10/64"))
        self.check_item("SSH-PASSWORD", "要対応", "危険度は「高」")


class ListeningPortsTest(SecurityCheckCase):
    """NET-LISTEN: loopback 以外で待ち受けているポート。"""

    def test_the_expected_ports_are_fine(self):
        item = self.check_item("NET-LISTEN", "OK", "tcp 22", "udp 5353", "udp 68", "udp 546")
        # IPv4 と IPv6 で同じポートが 2 度出ても、1 回だけ。
        self.assertEqual(item.text.count("tcp 22"), 1)
        # loopback だけの待ち受け（631・53）は数えない。
        self.assertNotIn("631", item.text)
        self.assertIsNone(re.search(r"udp 53(?!\d)", item.text), item.text)

    def test_unexpected_ports_are_listed(self):
        cases = [
            ("tcp   LISTEN 0      511        0.0.0.0:8080       0.0.0.0:*\n", "tcp 8080"),
            ("tcp   LISTEN 0      128           [::]:2222          [::]:*\n", "tcp 2222"),
            ("udp   UNCONN 0      0        239.255.255.250:1900  0.0.0.0:*\n", "udp 1900"),
            ("tcp   LISTEN 0      5      192.168.1.20:5000       0.0.0.0:*\n", "tcp 5000"),
            ("tcp   LISTEN 0      5                 *:9000             *:*\n", "tcp 9000"),
        ]
        variants = [(case, lambda world, case=case: world.stub_data("ss", SS_NORMAL + case[0])) for case in cases]
        for (_line, port), result in self.run_variants(variants):
            with self.subTest(port=port):
                item = self.check_item("NET-LISTEN", "注意", port, "tcp 22", result=result)
                self.assertIn("ss -lntup", item.fix)
                self.assertIn("harden.sh firewall", item.fix)

    def test_loopback_only_listeners_are_ignored(self):
        self.stub_data("ss", SS_NORMAL + "tcp   LISTEN 0      5      127.0.0.1:9000       0.0.0.0:*\n"
                       "tcp   LISTEN 0      5          [::1]:9000          [::]:*\n"
                       "udp   UNCONN 0      0    127.0.0.1:323       0.0.0.0:*\n")
        item = self.check_item("NET-LISTEN", "OK")
        self.assertNotIn("9000", item.text)
        self.assertNotIn("323", item.text)

    def test_avahi_and_chrony_use_a_temporary_udp_port(self):
        udp = "udp   UNCONN 0      0            0.0.0.0:{0}      0.0.0.0:*\n"
        tcp = "tcp   LISTEN 0      5            0.0.0.0:41234      0.0.0.0:*\n"
        everything = ["ssh", "avahi-daemon", "chrony"]
        cases = [
            ("avahi と chrony が動いている", udp.format(41234), everything, "OK", "udp 41234（Avahi・chrony が使う一時的なポート）"),
            ("avahi だけ動いている", udp.format(41234), ["ssh", "avahi-daemon"], "OK", "一時的なポート"),
            ("chrony だけ動いている", udp.format(41234), ["ssh", "chrony"], "OK", "一時的なポート"),
            ("どちらも動いていない", udp.format(41234), ["ssh"], "注意", "udp 41234"),
            ("tcp は一時的なポートでも想定外", tcp, everything, "注意", "tcp 41234"),
            ("32768 より小さい udp ポート", udp.format(32767), everything, "注意", "udp 32767"),
            ("32768 ちょうど", udp.format(32768), everything, "OK", "udp 32768"),
        ]
        def change(world, case):
            world.stub_data("ss", SS_NORMAL + case[1])
            world.set_units(active=case[2])
        variants = [(case, lambda world, case=case: change(world, case)) for case in cases]
        for (name, _line, _units, label, phrase), result in self.run_variants(variants):
            with self.subTest(case=name):
                self.check_item("NET-LISTEN", label, phrase, result=result)

    def test_udp_123_is_expected_only_while_chrony_runs(self):
        self.stub_data("ss", SS_NORMAL + "udp   UNCONN 0      0            0.0.0.0:123        0.0.0.0:*\n")
        self.check_item("NET-LISTEN", "OK", "udp 123（chrony の NTP）")
        self.set_units(active=["ssh", "systemd-timesyncd"])
        self.check_item("NET-LISTEN", "注意", "udp 123")

    def test_nothing_listening_is_fine(self):
        self.stub_data("ss", "")
        self.check_item("NET-LISTEN", "OK", "開いているポートはありません")

    def test_ss_missing_or_failing_is_unknown(self):
        self.check_item("NET-LISTEN", "不明", SS_FAIL="1")
        self.remove_tool("ss")
        self.check_item("NET-LISTEN", "不明", "ss コマンドがない")

    def test_it_works_without_root_too(self):
        self.stub_data("ss", SS_NORMAL + "tcp   LISTEN 0      511        0.0.0.0:8080       0.0.0.0:*\n")
        self.check_item("NET-LISTEN", "注意", "tcp 8080", root=False)


class FirewallTest(SecurityCheckCase):
    """NET-FW: 入ってくる通信を既定で捨てる防火壁があるか（root のみ）。"""

    def test_a_drop_policy_on_the_input_hook_is_fine(self):
        self.check_item("NET-FW", "OK", "hook input", "policy drop")

    def test_bad_rulesets(self):
        cases = {
            "policy accept": NFT_ACCEPT,
            "nothing at all": "",
            "only a forward chain drops": "table inet t {\n\tchain forward {\n\t\ttype filter hook forward "
                                          "priority filter; policy drop;\n\t}\n}\n",
            "drop policy on an output hook": "table inet t {\n\tchain output {\n\t\ttype filter hook output "
                                             "priority filter; policy drop;\n\t}\n}\n",
        }
        variants = [(name, lambda world, ruleset=ruleset: world.stub_data("nft", ruleset))
                    for name, ruleset in cases.items()]
        for name, result in self.run_variants(variants):
            with self.subTest(case=name):
                item = self.check_item("NET-FW", "要対応", "防火壁", result=result)
                self.assertEqual(item.fix, "sudo bash scripts/harden.sh firewall")

    def test_an_iptables_nft_table_counts_too(self):
        self.stub_data("nft", "table ip filter {\n\tchain INPUT {\n\t\ttype filter hook input priority filter; "
                       "policy drop;\n\t}\n}\n")
        self.check_item("NET-FW", "OK")

    def test_a_missing_nft_means_no_firewall(self):
        self.remove_tool("nft")
        item = self.check_item("NET-FW", "要対応", "入っておらず")
        self.assertEqual(item.fix, "sudo bash scripts/harden.sh firewall")

    def test_a_failing_nft_is_unknown(self):
        self.check_item("NET-FW", "不明", NFT_FAIL="1")

    def test_without_root_it_is_unknown_and_nft_is_not_called(self):
        result = self.run_check(root=False)
        item = self.check_item("NET-FW", "不明", "root でないため", result=result)
        self.assertEqual(item.hint, "sudo bash scripts/security_check.sh")
        self.assertEqual(result.called("nft"), [])


SSH_ITEMS = ("SSH-PASSWORD", "SSH-ROOT", "SSH-KEYS", "SSH-FAILED", "SSH-LOGINS")


class SshActiveTest(SecurityCheckCase):
    """SSH-ACTIVE: SSH が止まっていれば、ほかの SSH の項目は省く。"""

    def test_running_ssh_is_reported(self):
        item = self.check_item("SSH-ACTIVE", "情報", "動いています")
        self.assertEqual(item.fix, "")

    def test_socket_activation_counts_as_running(self):
        self.set_units(active=["ssh.socket"])
        result = self.run_check()
        self.check_item("SSH-ACTIVE", "情報", "動いています", result=result)
        self.assertEqual(len(result.all("SSH-PASSWORD")), 1)

    def test_when_ssh_is_off_the_other_ssh_items_are_skipped(self):
        self.set_units(active=["avahi-daemon", "chrony"])
        result = self.run_check()
        self.check_item("SSH-ACTIVE", "OK", "動いていません", result=result)
        for item_id in SSH_ITEMS:
            self.assertEqual(result.all(item_id), [], item_id)
        self.assertEqual(result.called("sshd"), [])
        self.assertEqual(result.called("journalctl"), [])
        # ほかの項目は、そのまま調べる。
        self.assertEqual(len(result.all("SUDO-NOPASSWD")), 1)
        self.assertEqual(result.code, 0)

    def test_ssh_off_does_not_hide_other_findings(self):
        self.set_units(active=["avahi-daemon", "chrony"])
        self.stub_data("sshd_T", SSHD_T_TEMPLATE.format(root="yes", password="yes", kbd="yes"))
        self.put("/etc/sudoers.d/010_pi-nopasswd", "pi ALL=(ALL) NOPASSWD: ALL\n", 0o440)
        result = self.run_check()
        self.assertEqual(result.labels("SUDO-NOPASSWD"), ["注意"], "SSH が止まっていれば、パスワードで入られる心配は無い")

    def test_unknown_when_systemctl_is_missing_or_broken(self):
        for mode in ("missing", "broken"):
            with self.subTest(mode=mode):
                if mode == "missing":
                    self.remove_tool("systemctl")
                    result = self.run_check()
                else:
                    result = self.run_check(SYSTEMCTL_BROKEN="1")
                self.assertEqual(result.labels("SSH-ACTIVE"), ["不明"])
                # 動いているものとして、SSH の項目は調べる。
                for item_id in SSH_ITEMS:
                    self.assertEqual(len(result.all(item_id)), 1, item_id)


class SshPasswordRootTest(SecurityCheckCase):
    """SSH-PASSWORD / SSH-ROOT（root: sshd -T が実際に使う設定を見る）。"""

    def sshd(self, password="no", kbd="no", root="no"):
        self.stub_data("sshd_T", SSHD_T_TEMPLATE.format(root=root, password=password, kbd=kbd))

    def test_password_and_keyboard_interactive_combinations(self):
        cases = [
            ("no", "no", "OK", "鍵だけ"),
            ("yes", "no", "要対応", "PasswordAuthentication が yes"),
            ("no", "yes", "要対応", "KbdInteractiveAuthentication が yes"),
            ("yes", "yes", "要対応", "PasswordAuthentication と KbdInteractiveAuthentication が yes"),
        ]
        variants = [(case, lambda world, case=case: world.sshd(case[0], case[1])) for case in cases]
        for (password, kbd, label, phrase), result in self.run_variants(variants):
            with self.subTest(password=password, kbd=kbd):
                item = self.check_item("SSH-PASSWORD", label, phrase, result=result)
                self.assertNotIn("設定ファイルから推定", item.text)
                if label == "要対応":
                    self.assertEqual(item.fix[-len("sudo bash scripts/harden.sh ssh"):],
                                     "sudo bash scripts/harden.sh ssh")
                    self.assertIn("危険度は「中」", item.text)
                    self.assertIn("PC で鍵を作って", item.fix)

    def test_it_asks_sshd_as_seen_from_outside_and_from_inside(self):
        result = self.run_check()
        self.assertEqual(result.called("sshd"), [
            "sshd -T -C user=pi,host=chime-check,addr=203.0.113.1",
            "sshd -T -C user=pi,host=chime-check,addr=10.0.0.1",
            "sshd -T -C user=pi,host=chime-check,addr=192.168.1.1"])

    def test_a_match_block_that_enables_passwords_for_one_address_is_found(self):
        for address in ("203.0.113.1", "10.0.0.1", "192.168.1.1"):
            with self.subTest(address=address):
                self.stub_data("sshd_T.{0}".format(address), SSHD_T_TEMPLATE.format(
                    root="no", password="yes", kbd="no"))
                self.check_item("SSH-PASSWORD", "要対応", "PasswordAuthentication")
                self.stub_data("sshd_T.{0}".format(address), None)

    def test_when_sshd_cannot_be_asked_it_falls_back_to_the_files(self):
        # sshd -T が使えない（/run/sshd が無い）。設定ファイル（鍵だけにした状態）から推定する。
        item = self.check_item("SSH-PASSWORD", "OK", "（設定ファイルから推定）", SSHD_FAIL="1")
        self.assertIn("鍵だけ", item.text)
        self.remove_tool("sshd")
        self.check_item("SSH-PASSWORD", "OK", "（設定ファイルから推定）")
        # 推定でも、設定が緩ければ見つける。
        self.set_sshd_config("")
        self.check_item("SSH-PASSWORD", "要対応", "（設定ファイルから推定）")

    def test_permit_root_login_values(self):
        cases = [
            ("yes", "要対応", "PermitRootLogin yes"),
            ("prohibit-password", "情報", "root はパスワードでは入れません"),
            ("without-password", "情報", "root はパスワードでは入れません"),
            ("forced-commands-only", "情報", "forced-commands-only"),
            ("no", "OK", "禁止されています"),
        ]
        variants = [(case, lambda world, case=case: world.sshd(root=case[0])) for case in cases]
        for (value, label, phrase), result in self.run_variants(variants):
            with self.subTest(value=value):
                item = self.check_item("SSH-ROOT", label, phrase, result=result)
                if label == "要対応":
                    self.assertEqual(item.fix, "sudo bash scripts/harden.sh ssh")
                if label == "情報" and value != "forced-commands-only":
                    self.assertIn("harden.sh ssh", item.text)

    def test_root_login_is_the_most_permissive_value_over_the_addresses(self):
        self.sshd(root="no")
        self.stub_data("sshd_T.10.0.0.1", SSHD_T_TEMPLATE.format(root="yes", password="no", kbd="no"))
        self.check_item("SSH-ROOT", "要対応")
        self.stub_data("sshd_T.10.0.0.1", SSHD_T_TEMPLATE.format(root="prohibit-password", password="no", kbd="no"))
        self.check_item("SSH-ROOT", "情報")


#: 設定ファイルから推定する場合の表: (名前, sshd_config, {sshd_config.d の中}, パスワードのラベル, root のラベル)
KEYS_OFF = "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
CONFIG_CASES = [
    ("何も書いていない（OpenSSH の既定）", "", {}, "要対応", "情報"),
    ("どちらも no", KEYS_OFF + "PermitRootLogin no\n", {}, "OK", "OK"),
    ("パスワードだけ no（キーボード対話は既定の yes）", "PasswordAuthentication no\n", {}, "要対応", "情報"),
    ("同じ項目は最初の値が勝つ",
     "PasswordAuthentication yes\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n",
     {}, "要対応", "情報"),
    ("Include が先で、drop-in の no が勝つ",
     "Include /etc/ssh/sshd_config.d/*.conf\nPasswordAuthentication yes\n",
     {"01-x.conf": KEYS_OFF}, "OK", "情報"),
    ("Include が後なので、先の yes が勝つ",
     "PasswordAuthentication yes\nInclude /etc/ssh/sshd_config.d/*.conf\n",
     {"01-x.conf": KEYS_OFF}, "要対応", "情報"),
    ("Match の中は無視する",
     KEYS_OFF + "Match Address 10.*\n    PasswordAuthentication yes\n    PermitRootLogin yes\n",
     {}, "OK", "情報"),
    ("Match の中だけに書いてあっても、全体は既定のまま",
     "Match User bob\n    PasswordAuthentication no\n    KbdInteractiveAuthentication no\n",
     {}, "要対応", "情報"),
    ("Match の中の Include は無視する",
     "Match User bob\n    Include /etc/ssh/sshd_config.d/*.conf\n",
     {"01-x.conf": KEYS_OFF}, "要対応", "情報"),
    ("= 区切り・引用符・大文字小文字",
     'passwordauthentication="no"\nKBDINTERACTIVEAUTHENTICATION = No\nPermitRootLogin=NO\n',
     {}, "OK", "OK"),
    ("古い名前 ChallengeResponseAuthentication",
     "PasswordAuthentication no\nChallengeResponseAuthentication no\n", {}, "OK", "情報"),
    ("相対パスの Include", "Include sshd_config.d/*.conf\n", {"a.conf": KEYS_OFF}, "OK", "情報"),
    ("drop-in は名前順（先の no が勝つ）", "Include /etc/ssh/sshd_config.d/*.conf\n",
     {"10-a.conf": KEYS_OFF, "20-b.conf": "PasswordAuthentication yes\n"}, "OK", "情報"),
    ("drop-in は名前順（先の yes が勝つ）", "Include /etc/ssh/sshd_config.d/*.conf\n",
     {"10-a.conf": "PasswordAuthentication yes\n", "20-b.conf": KEYS_OFF}, "要対応", "情報"),
    ("drop-in の Match は、そのファイルの終わりで終わる",
     "Include /etc/ssh/sshd_config.d/*.conf\n" + KEYS_OFF,
     {"00-x.conf": "Match User bob\n    PasswordAuthentication yes\n"}, "OK", "情報"),
    ("コメントと空行と余分な空白",
     "# PasswordAuthentication yes\n\n   PasswordAuthentication   no   \n#KbdInteractiveAuthentication yes\n"
     "KbdInteractiveAuthentication no\n", {}, "OK", "情報"),
    ("root の直接ログインを許している", KEYS_OFF + "PermitRootLogin yes\n", {}, "OK", "要対応"),
    (".conf でない drop-in は読まない", "Include /etc/ssh/sshd_config.d/*.conf\n",
     {"01-x.conf.bak": KEYS_OFF}, "要対応", "情報"),
]


class SshConfigEstimateTest(SecurityCheckCase):
    """root でないときは、sshd_config と Include を読んで推定する（同じ項目は最初の値が勝つ）。"""

    def test_estimates_from_the_files(self):
        variants = [(case, lambda world, case=case: world.set_sshd_config(case[1], case[2]))
                    for case in CONFIG_CASES]
        for (name, main, drop_ins, password_label, root_label), result in self.run_variants(variants, root=False):
            with self.subTest(case=name):
                password = self.check_item("SSH-PASSWORD", password_label, "（設定ファイルから推定）", result=result)
                root = self.check_item("SSH-ROOT", root_label, "（設定ファイルから推定）", result=result)
                self.assertEqual(result.called("sshd"), [])
                self.assertIn("設定ファイルから推定", password.text + root.text)

    def test_an_unreadable_sshd_config_is_unknown(self):
        shutil.rmtree(self.sys("/etc/ssh"))
        result = self.run_check(root=False)
        self.assertEqual(result.labels("SSH-PASSWORD"), ["不明"])
        self.assertEqual(result.labels("SSH-ROOT"), ["不明"])
        self.assertEqual(result.one("SSH-PASSWORD").hint, "sudo bash scripts/security_check.sh")

    def test_a_missing_sshd_config_as_root_is_unknown_too_when_sshd_cannot_run(self):
        shutil.rmtree(self.sys("/etc/ssh"))
        result = self.run_check(SSHD_FAIL="1")
        self.assertEqual(result.labels("SSH-PASSWORD"), ["不明"])

    def test_include_loops_do_not_hang(self):
        self.set_sshd_config("Include /etc/ssh/sshd_config\nPasswordAuthentication no\n"
                             "KbdInteractiveAuthentication no\n")
        self.check_item("SSH-PASSWORD", "OK", root=False)


class SshKeysTest(SecurityCheckCase):
    """SSH-KEYS: authorized_keys の公開鍵。"""

    def keys(self, text: str) -> None:
        self.put("/home/pi/.ssh/authorized_keys", text, 0o600)

    def test_one_modern_key_is_fine(self):
        self.check_item("SSH-KEYS", "OK", "公開鍵が 1 個", "ssh-ed25519 が 1 個")

    def test_the_key_types_are_counted(self):
        self.keys(AUTHORIZED_KEY + AUTHORIZED_KEY.replace("EXAMPLE0000", "EXAMPLE1111") + AUTHORIZED_RSA)
        self.check_item("SSH-KEYS", "OK", "公開鍵が 3 個", "ssh-ed25519 が 2 個", "ssh-rsa が 1 個")

    def test_comments_blank_lines_and_options_are_handled(self):
        self.keys("# 管理者の鍵\n\n" + 'command="/usr/bin/true",no-pty,no-port-forwarding ' + AUTHORIZED_KEY
                  + "   \n# " + AUTHORIZED_RSA)
        self.check_item("SSH-KEYS", "OK", "公開鍵が 1 個")

    def test_a_dsa_key_is_a_warning(self):
        self.keys(AUTHORIZED_KEY + AUTHORIZED_DSS)
        item = self.check_item("SSH-KEYS", "注意", "ssh-dss", "DSA")
        self.assertIn("ssh-keygen -t ed25519", item.fix)

    def test_no_key_yet_is_a_warning_with_steps(self):
        cases = [("空のファイル", ""), ("コメントだけ", "# まだ登録していない\n"),
                 ("鍵に見えない行", "hello world\n"), ("ファイルが無い", None)]
        def change(world, text):
            if text is None:
                os.remove(world.sys("/home/pi/.ssh/authorized_keys"))
            else:
                world.keys(text)
        variants = [(case, lambda world, case=case: change(world, case[1])) for case in cases]
        for (name, _text), result in self.run_variants(variants):
            with self.subTest(case=name):
                item = self.check_item("SSH-KEYS", "注意", "鍵がまだありません", result=result)
                steps = item.fix + "\n" + "\n".join(item.notes)
                self.assertIn("harden.sh ssh の前に", item.text)
                self.assertIn("ssh-keygen -t ed25519", steps)
                self.assertIn("ssh-copy-id pi@campus-chime.local", steps)
                self.assertIn("$env:USERPROFILE\\.ssh\\id_ed25519.pub", steps)
                self.assertIn("chmod 600 ~/.ssh/authorized_keys", steps)
                self.assertIn("sudo bash scripts/harden.sh ssh", steps)

    def test_the_steps_use_the_checked_user_name(self):
        self.put("/home/chime/.ssh/authorized_keys", "")
        result = self.run_check("--user", "chime")
        item = self.check_item("SSH-KEYS", "注意", "/home/chime/.ssh/authorized_keys", result=result)
        self.assertIn("ssh-copy-id chime@campus-chime.local", "\n".join(item.notes))
        self.assertIn("ssh chime@campus-chime.local", "\n".join(item.notes))

    @unittest.skipIf(os.geteuid() == 0, "root は権限の無いファイルも読めるので、確かめられない")
    def test_an_unreadable_file_or_home_is_unknown(self):
        os.chmod(self.sys("/home/pi/.ssh/authorized_keys"), 0)
        self.check_item("SSH-KEYS", "不明", "読めません", root=False)
        os.chmod(self.sys("/home/pi/.ssh/authorized_keys"), 0o600)
        os.chmod(self.sys("/home/pi/.ssh"), 0)
        self.addCleanup(os.chmod, self.sys("/home/pi/.ssh"), 0o700)
        self.check_item("SSH-KEYS", "不明", root=False)


class SshJournalTest(SecurityCheckCase):
    """SSH-FAILED / SSH-LOGINS: ssh のジャーナルの数え上げ。"""

    def test_no_failed_logins_is_fine(self):
        self.check_item("SSH-FAILED", "OK", "失敗したログイン")

    def test_failed_passwords_and_invalid_users_are_counted_separately(self):
        self.stub_data("journal_7d", JOURNAL_ATTACKED)
        item = self.check_item("SSH-FAILED", "注意", "パスワードを試されています",
                               "Failed password が 3 回", "Invalid user が 1 回")
        # 送信元は回数の多い順。同数なら、アドレスの文字順。
        self.assertIn("10.0.0.5（2 回）、198.51.100.7（2 回）", item.text)
        self.assertIn("harden.sh ssh", item.fix)
        self.assertIn("harden.sh firewall", item.fix)

    def test_only_the_three_busiest_sources_are_shown(self):
        lines = []
        for address, count in (("203.0.113.1", 4), ("203.0.113.2", 3), ("203.0.113.3", 2), ("203.0.113.4", 1)):
            lines += ["Failed password for root from {0} port {1} ssh2".format(address, 40000 + n)
                      for n in range(count)]
        self.stub_data("journal_7d", "\n".join(lines) + "\n")
        item = self.check_item("SSH-FAILED", "注意", "Failed password が 10 回", "Invalid user が 0 回")
        self.assertIn("203.0.113.1（4 回）、203.0.113.2（3 回）、203.0.113.3（2 回）", item.text)
        self.assertNotIn("203.0.113.4", item.text)

    def test_ipv6_sources_are_read(self):
        self.stub_data("journal_7d", "Failed password for pi from 2001:db8::5 port 5000 ssh2\n")
        self.check_item("SSH-FAILED", "注意", "2001:db8::5（1 回）")

    def test_a_journal_that_cannot_be_read_is_unknown(self):
        def hint(world):
            world.stub_data("journal_err", HINT_NOT_SEEING)
            world.stub_data("journal_7d", "-- No entries --\n")
            world.stub_data("journal_30d", "-- No entries --\n")

        def no_files(world):
            world.stub_data("journal_err", "No journal files were found.\n")
            world.stub_data("journal_7d", "-- No entries --\n")
            world.stub_data("journal_30d", "-- No entries --\n")

        def failing(world):
            return {"JOURNAL_FAIL": "1"}

        def missing(world):
            world.remove_tool("journalctl")

        variants = [("他のユーザーのログを見られない", hint), ("ジャーナルが無い", no_files),
                    ("journalctl が失敗する", failing), ("journalctl が無い", missing)]
        for name, result in self.run_variants(variants, root=False):
            with self.subTest(case=name):
                self.assertEqual(result.labels("SSH-FAILED"), ["不明"])
                self.assertEqual(result.labels("SSH-LOGINS"), ["不明"])
                self.assertEqual(result.one("SSH-FAILED").hint, "sudo bash scripts/security_check.sh")

    def test_it_reads_seven_and_thirty_days(self):
        result = self.run_check()
        self.assertEqual(result.called("journalctl"), [
            "journalctl -u ssh.service --since -7 days -o cat",
            "journalctl -u ssh.service --since -30 days -o cat"])

    def test_logins_are_counted_by_method_with_their_sources(self):
        lines = ["Accepted publickey for pi from 10.0.0.9 port 50100 ssh2: ED25519 SHA256:abc",
                 "Accepted publickey for pi from 10.0.0.9 port 50101 ssh2: ED25519 SHA256:abc",
                 "Accepted publickey for pi from 10.0.0.8 port 50102 ssh2: ED25519 SHA256:abc",
                 "Accepted publickey for bob from 10.0.0.77 port 50103 ssh2: ED25519 SHA256:abc",
                 "Accepted password for pi from 10.0.0.7 port 50104 ssh2"]
        self.stub_data("journal_30d", "\n".join(lines) + "\n")
        item = self.check_item("SSH-LOGINS", "情報", "公開鍵が 3 回、パスワードが 1 回")
        self.assertIn("10.0.0.9（2 回）、10.0.0.7（1 回）、10.0.0.8（1 回）", item.text)
        self.assertNotIn("10.0.0.77", item.text)
        # パスワードでのログインが使われている、という注意書き。
        self.assertTrue(any("パスワードでのログインが使われています" in note for note in item.notes), item.notes)

    def test_key_only_logins_have_no_password_note(self):
        item = self.check_item("SSH-LOGINS", "情報", "パスワードが 0 回")
        self.assertEqual(item.notes, ())

    def test_at_most_five_login_sources_are_shown(self):
        lines = ["Accepted publickey for pi from 10.0.0.{0} port 50000 ssh2: ED25519 SHA256:abc".format(n)
                 for n in range(1, 8)]
        self.stub_data("journal_30d", "\n".join(lines) + "\n")
        item = self.check_item("SSH-LOGINS", "情報", "公開鍵が 7 回")
        self.assertEqual(item.text.count("回）"), 5)

    def test_no_logins_at_all(self):
        self.stub_data("journal_30d", "-- No entries --\n")
        self.check_item("SSH-LOGINS", "情報", "ログインした記録はありません")


SUDO_NOPASSWD_ALL = "pi ALL=(ALL) NOPASSWD: ALL\n"
SUDO_L_PASSWORD = "Matching Defaults entries for pi on raspberrypi:\n    env_reset, mail_badpass, use_pty\n\n" \
                  "User pi may run the following commands on raspberrypi:\n    (ALL : ALL) ALL\n"
SUDO_L_NOPASSWD_ALL = SUDO_L_PASSWORD + "    (ALL) NOPASSWD: ALL\n"
SUDO_L_NOPASSWD_SOME = SUDO_L_PASSWORD + "    (root) NOPASSWD: /usr/bin/apt-get update, /sbin/reboot\n"


class SudoRootTest(SecurityCheckCase):
    """SUDO-NOPASSWD（root: /etc/sudoers と /etc/sudoers.d を読む）。"""

    def test_rules_in_sudoers_files(self):
        d = "/etc/sudoers.d/"
        cases = [
            ("規則が無い", {}, {}, "OK", "パスワードが必要です"),
            ("Raspberry Pi OS 既定の 010_pi-nopasswd", {d + "010_pi-nopasswd": SUDO_NOPASSWD_ALL}, {},
             "注意", "NOPASSWD: ALL"),
            ("グループ %sudo の NOPASSWD（利用者は所属）", {d + "90-sudo": "%sudo ALL=(ALL:ALL) NOPASSWD: ALL\n"},
             {}, "注意", "/etc/sudoers.d/90-sudo"),
            ("グループ %sudo の NOPASSWD（利用者は所属しない）", {d + "90-sudo": "%sudo ALL=(ALL:ALL) NOPASSWD: ALL\n"},
             {"FAKE_GROUPS": "pi adm audio"}, "OK", "パスワードが必要です"),
            ("全員 ALL の NOPASSWD", {d + "90-all": "ALL ALL=(ALL) NOPASSWD: ALL\n"}, {}, "注意", "NOPASSWD: ALL"),
            ("利用者の並びに含まれる", {d + "90-list": "alice, pi\tALL=(ALL) NOPASSWD: ALL\n"}, {},
             "注意", "NOPASSWD: ALL"),
            ("ほかの利用者だけ", {d + "90-deploy": "deploy ALL=(ALL) NOPASSWD: ALL\n"}, {}, "OK", "パスワードが必要です"),
            ("一部のコマンドだけ", {d + "90-apt": "pi ALL=(ALL) NOPASSWD: /usr/bin/apt-get update, /sbin/reboot\n"},
             {}, "情報", "一部のコマンドだけ"),
            ("タグが続く ALL", {d + "90-env": "pi ALL=(ALL) NOPASSWD:SETENV: ALL\n"}, {}, "注意", "NOPASSWD: ALL"),
            ("コメントアウト", {d + "010_pi-nopasswd": "#" + SUDO_NOPASSWD_ALL}, {}, "OK", "パスワードが必要です"),
            ("名前に . を含むファイルは sudo も読まない", {d + "010_pi-nopasswd.dpkg-old": SUDO_NOPASSWD_ALL}, {},
             "OK", "パスワードが必要です"),
            ("名前が ~ で終わるファイルも読まない", {d + "010_pi-nopasswd~": SUDO_NOPASSWD_ALL}, {},
             "OK", "パスワードが必要です"),
            ("/etc/sudoers の本体に書いてある", {"/etc/sudoers": SUDOERS_MAIN + SUDO_NOPASSWD_ALL}, {},
             "注意", "/etc/sudoers"),
            ("一部のコマンドと ALL の両方", {d + "90-a": "pi ALL=(ALL) NOPASSWD: /sbin/reboot\n",
                                         d + "90-b": SUDO_NOPASSWD_ALL}, {}, "注意", "NOPASSWD: ALL"),
        ]
        variants = []
        for case in cases:
            def change(world, case=case):
                for path, text in case[1].items():
                    world.put(path, text, 0o440)
                return case[2]
            variants.append((case, change))
        for (name, _files, _extra, label, phrase), result in self.run_variants(variants):
            with self.subTest(case=name):
                item = self.check_item("SUDO-NOPASSWD", label, phrase, result=result)
                if label == "注意":
                    self.assertIn("sudo bash scripts/harden.sh sudo", item.fix)
                    self.assertIn("sudo passwd pi", item.fix)
                    self.assertIn("/etc/sudoers と /etc/sudoers.d から確認", item.text)

    def test_it_is_an_error_when_ssh_also_accepts_the_password(self):
        self.put("/etc/sudoers.d/010_pi-nopasswd", SUDO_NOPASSWD_ALL, 0o440)
        self.stub_data("sshd_T", SSHD_T_TEMPLATE.format(root="no", password="yes", kbd="no"))
        item = self.check_item("SUDO-NOPASSWD", "要対応", "パスワードで入れるので", "root を取られます")
        self.assertIn("harden.sh sudo", item.fix)
        # SSH が止まっていれば、パスワードで入られる心配は無い。
        self.set_units(active=["avahi-daemon", "chrony"])
        self.check_item("SUDO-NOPASSWD", "注意")

    def test_the_user_name_option_selects_the_rules(self):
        self.put("/etc/sudoers.d/90-deploy", "deploy ALL=(ALL) NOPASSWD: ALL\n", 0o440)
        self.put("/home/deploy/.ssh/authorized_keys", AUTHORIZED_KEY, 0o600)
        result = self.run_check("--user", "deploy", FAKE_GROUPS="deploy")
        self.check_item("SUDO-NOPASSWD", "注意", "deploy は sudo", result=result)


class SudoNonRootTest(SecurityCheckCase):
    """SUDO-NOPASSWD（root でない: sudo -n -l の出力を読む）。"""

    def set_sudo(self, out: str = "", err: str = "", rc: int = 0) -> None:
        self.stub_data("sudo_l", out)
        self.stub_data("sudo_l_err", err or None)
        self.env_extra["SUDO_L_RC"] = str(rc)

    def test_listings(self):
        cases = [
            ("パスワードが必要", dict(err="sudo: a password is required\n", rc=1), "OK", "パスワードが必要です"),
            ("どのコマンドもパスワードが必要（一覧は見えた）", dict(out=SUDO_L_PASSWORD), "OK", "パスワードが必要です"),
            ("NOPASSWD: ALL", dict(out=SUDO_L_NOPASSWD_ALL), "注意", "NOPASSWD: ALL"),
            ("一部のコマンドだけ", dict(out=SUDO_L_NOPASSWD_SOME), "情報", "一部のコマンドだけ"),
            ("sudo を使えない", dict(err="Sorry, user pi may not run sudo on raspberrypi.\n", rc=1), "OK",
             "sudo を使えません"),
            ("読み取れない失敗", dict(err="sudo: unable to resolve host raspberrypi\n", rc=1), "不明",
             "読み取れませんでした"),
        ]
        variants = [(case, lambda world, case=case: world.set_sudo(**case[1])) for case in cases]
        for (name, _args, label, phrase), result in self.run_variants(variants, root=False):
            with self.subTest(case=name):
                item = self.check_item("SUDO-NOPASSWD", label, phrase, result=result)
                self.assertEqual(result.called("sudo"), ["sudo -n -l"])
                if label == "注意":
                    self.assertIn("sudo -n -l の出力から確認", item.text)
                    self.assertIn("harden.sh sudo", item.fix)

    def test_it_is_an_error_when_ssh_also_accepts_the_password(self):
        self.set_sudo(out=SUDO_L_NOPASSWD_ALL)
        self.set_sshd_config("")
        self.check_item("SUDO-NOPASSWD", "要対応", "パスワードで入れるので", root=False)

    def test_another_account_cannot_be_checked_without_root(self):
        result = self.run_check("--user", "chime", root=False, FAKE_USER="alice")
        item = self.check_item("SUDO-NOPASSWD", "不明", "別のユーザー（chime）", result=result)
        self.assertEqual(item.hint, "sudo bash scripts/security_check.sh")
        self.assertEqual(result.called("sudo"), [])

    def test_a_missing_sudo_is_unknown(self):
        self.remove_tool("sudo")
        self.check_item("SUDO-NOPASSWD", "不明", "sudo コマンドがない", root=False)


class UserPasswordTest(SecurityCheckCase):
    """USER-PASSWORD（root のみ）: harden.sh sudo の前に、パスワードが決まっているか。"""

    def test_states(self):
        cases = [
            ("pi P 2026-01-15 0 99999 7 -1\n", "OK", "パスワードが設定されています"),
            ("pi L 2026-01-15 0 99999 7 -1\n", "情報", "状態 L"),
            ("pi NP 2026-01-15 0 99999 7 -1\n", "情報", "状態 NP"),
            ("something odd\n", "不明", "読み取れませんでした"),
        ]
        variants = [(case, lambda world, case=case: world.stub_data("passwd_S", case[0])) for case in cases]
        for (_out, label, phrase), result in self.run_variants(variants):
            with self.subTest(label=label, phrase=phrase):
                item = self.check_item("USER-PASSWORD", label, phrase, result=result)
                if label == "情報":
                    self.assertIn("sudo passwd pi", item.text)
                    self.assertIn("harden.sh sudo の前に", item.text)

    def test_failing_or_missing_passwd_is_unknown(self):
        self.check_item("USER-PASSWORD", "不明", PASSWD_FAIL="1")
        self.remove_tool("passwd")
        self.check_item("USER-PASSWORD", "不明", "passwd コマンドがない")

    def test_it_needs_root(self):
        result = self.run_check(root=False)
        self.check_item("USER-PASSWORD", "不明", "root でないため", result=result)
        self.assertEqual(result.called("passwd"), [])


class OsReleaseTest(SecurityCheckCase):
    """OS-RELEASE: Debian の版（VERSION_CODENAME）。"""

    def test_codenames(self):
        cases = [
            ("trixie", "debian", "13", "OK", "Debian 13"),
            ("bookworm", "raspbian", "12", "情報", "LTS"),
            ("bookworm", "debian", "12", "情報", "2028-06-30"),
            ("bullseye", "raspbian", "11", "要対応", "2026-08-31"),
            ("buster", "raspbian", "10", "要対応", "Debian buster"),
            ("stretch", "raspbian", "9", "要対応", "セキュリティ更新が終わっています"),
            ("forky", "debian", "14", "不明", "forky"),
            ("sid", "debian", "", "不明", "sid"),
        ]
        variants = [(case, lambda world, case=case: world.set_os(case[0], case[1], case[2])) for case in cases]
        for (codename, _os_id, _version, label, phrase), result in self.run_variants(variants):
            with self.subTest(codename=codename):
                item = self.check_item("OS-RELEASE", label, phrase, result=result)
                if label == "要対応":
                    self.assertIn("SD カード", item.fix)

    def test_the_bookworm_text_explains_the_support_period(self):
        self.set_os("bookworm")
        item = self.check_item("OS-RELEASE", "情報", "Debian 12（bookworm）", "2026-07", "Raspbian の再構築は遅れる")
        self.assertEqual(item.fix, "")

    def test_quoted_codename_and_missing_file(self):
        def quoted(world):
            world.put("/etc/os-release", 'ID=debian\nVERSION_CODENAME="trixie"\n')

        def missing(world):
            os.remove(world.sys("/etc/os-release"))

        (_n1, quoted_result), (_n2, missing_result) = self.run_variants([("quoted", quoted), ("missing", missing)])
        self.check_item("OS-RELEASE", "OK", result=quoted_result)
        self.check_item("OS-RELEASE", "不明", "/etc/os-release", result=missing_result)

KERNEL_CASES = [
    # (名前, uname -r, uname -v, ラベル, 文言)
    ("古い Raspberry Pi のカーネル", KERNEL_OLD[0], KERNEL_OLD[1], "要対応", "2025-04-30 に作成"),
    ("新しい Raspberry Pi のカーネル", KERNEL_NEW[0], KERNEL_NEW[1], "OK", "2026-08-12 に作成"),
    ("基準日の前日", "6.12.40+rpt-rpi-v8", "#1 SMP PREEMPT Debian 1:6.12.40-1+rpt1 (2026-07-03)", "要対応", "2026-07-03"),
    ("基準日", "6.12.41+rpt-rpi-v8", "#1 SMP PREEMPT Debian 1:6.12.41-1+rpt1 (2026-07-04)", "OK", "2026-07-04"),
    ("基準日の翌日", "6.12.42+rpt-rpi-v8", "#1 SMP PREEMPT Debian 1:6.12.42-1+rpt1 (2026-07-05)", "OK", "2026-07-05"),
    ("Debian 本家の古いカーネル", "6.1.0-30-arm64", "#1 SMP Debian 6.1.129-1 (2025-03-06)", "要対応", "2025-03-06"),
    ("Raspbian の古い表記", "5.10.103-v7+", "#1529 SMP Raspbian 1:5.10.103-1 (2022-03-11)", "要対応", "2022-03-11"),
    ("日付を date で読める古い表記", "5.4.51-v7+", "#1333 SMP Tue Aug 4 14:18:21 BST 2020", "要対応", "2020-08-04"),
    ("日付を date で読める新しい表記", "6.20.1-v8", "#1 SMP PREEMPT_DYNAMIC Mon Nov  3 10:00:00 UTC 2026", "OK", "2026-11-03"),
    ("読めない表記", "6.12.47-x", "#1 SMP something odd", "不明", "読み取れません"),
    ("uname -v が空", "6.12.47-x", "", "不明", "読み取れません"),
]


class KernelTest(SecurityCheckCase):
    """OS-KERNEL: 動いているカーネルを作った日が、CISA KEV の修正が入った日（KERNEL_FIXES_SINCE）より後か。"""

    def test_build_dates(self):
        variants = [(case, lambda world, case=case: world.set_kernel(case[1], case[2])) for case in KERNEL_CASES]
        for (name, release, _version, label, phrase), result in self.run_variants(variants):
            with self.subTest(case=name):
                item = self.check_item("OS-KERNEL", label, phrase, result=result)
                if label == "要対応":
                    self.assertIn("CISA KEV に載った権限昇格の修正を含みません", item.text)
                    self.assertIn("sudo apt update && sudo apt full-upgrade", item.fix)
                    self.assertIn("9:55–17:05", item.fix)
                if label == "OK":
                    self.assertIn("引き続き", item.text)
                    self.assertIn(release, item.text)

    def test_the_cutoff_date_is_a_documented_constant(self):
        with open(CHECK_SH, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('KERNEL_FIXES_SINCE="2026-07-04"', text)
        self.assertIn("CISA KEV", text)
        self.assertIn("2026.10.21", text)
        self.assertIn("CVE-2026-53362", text)

    def test_a_failing_uname_is_unknown(self):
        self.remove_tool("uname")
        self.check_item("OS-KERNEL", "不明", "読み取れません")


class RebootTest(SecurityCheckCase):
    """OS-REBOOT: 更新したカーネルがまだ使われていないか、長く再起動していないか。"""

    def boot_days_ago(self, days: float) -> Dict[str, str]:
        return {"UPTIME_S": datetime.datetime.fromtimestamp(self.now - days * DAY).strftime("%Y-%m-%d %H:%M:%S")}

    def test_a_fresh_system_needs_no_reboot(self):
        item = self.check_item("OS-REBOOT", "OK", "再起動が必要な更新はありません", "最後の起動")
        self.assertIn("3 日前", item.text)

    def test_reboot_required_file(self):
        self.put("/run/reboot-required", "*** System restart required ***\n")
        item = self.check_item("OS-REBOOT", "要対応", "更新したカーネルがまだ使われていません", "/run/reboot-required")
        self.assertIn("sudo reboot", item.fix)
        self.assertIn("9:55–17:05", item.fix)

    def test_missing_modules_of_the_running_kernel(self):
        shutil.rmtree(self.sys("/lib/modules"))
        self.check_item("OS-REBOOT", "要対応", "/lib/modules")

    def test_a_newer_installed_kernel_package(self):
        self.stub_data("dpkg_linux_image",
                       "ii |linux-image-6.12.47+rpt-rpi-v8|1:6.12.47-1+rpt1\n"
                       "ii |linux-image-6.12.62+rpt-rpi-v8|1:6.12.62-1+rpt1\n"
                       "ii |linux-image-rpi-v8|1:6.12.62-1+rpt1\n")
        item = self.check_item("OS-REBOOT", "要対応", "linux-image-6.12.62+rpt-rpi-v8（1:6.12.62-1+rpt1）",
                               "動いているカーネル（1:6.12.47-1+rpt1）より新しい")
        self.assertIn("dpkg-query -W -f ${db:Status-Abbrev}|${Package}|${Version}\\n linux-image-*", " ".join(
            self.run_check().calls))
        self.assertIn("sudo reboot", item.fix)

    def test_only_installed_packages_count(self):
        # 削除済み（rc: 設定だけ残っている）の新しい版は、使われる心配が無い。
        self.stub_data("dpkg_linux_image",
                       "ii |linux-image-6.12.47+rpt-rpi-v8|1:6.12.47-1+rpt1\n"
                       "rc |linux-image-6.12.62+rpt-rpi-v8|1:6.12.62-1+rpt1\n"
                       "ii |linux-image-6.6.51+rpt-rpi-v8|1:6.6.51-1+rpt3\n")
        self.check_item("OS-REBOOT", "OK")

    def test_the_comparison_is_skipped_when_the_running_version_is_unreadable(self):
        self.set_kernel("6.12.47+rpt-rpi-v8", "#1 SMP PREEMPT (2026-08-12)")
        self.stub_data("dpkg_linux_image", "ii |linux-image-9.9.9+rpt-rpi-v8|1:9.9.9-1+rpt1\n")
        result = self.run_check()
        self.check_item("OS-REBOOT", "OK", result=result)
        self.assertEqual(result.called("dpkg --compare-versions"), [])

    def test_no_kernel_packages_listed(self):
        self.stub_data("dpkg_linux_image", "")
        self.check_item("OS-REBOOT", "OK")

    def test_a_long_uptime_is_a_warning(self):
        cases = [(59, "OK"), (60.1, "OK"), (61.1, "注意"), (200, "注意")]
        variants = [(case, lambda world, case=case: world.boot_days_ago(case[0])) for case in cases]
        for (days, label), result in self.run_variants(variants):
            with self.subTest(days=days):
                item = self.check_item("OS-REBOOT", label, "最後の起動", result=result)
                if label == "注意":
                    self.assertIn("再起動せずに {0} 日たっています".format(int(days)), item.text)
                    self.assertIn("sudo reboot", item.fix)

    def test_reboot_needed_wins_over_a_long_uptime_and_shows_it(self):
        self.put("/run/reboot-required", "")
        item = self.check_item("OS-REBOOT", "要対応", UPTIME_S=self.boot_days_ago(90)["UPTIME_S"])
        self.assertIn("90 日前", item.text)

    def test_missing_uptime_still_checks_the_rest(self):
        self.remove_tool("uptime")
        item = self.check_item("OS-REBOOT", "OK")
        self.assertNotIn("最後の起動", item.text)


class UpdatesTest(SecurityCheckCase):
    """OS-UPDATES: apt のリストの新しさと、最後に更新した日（45 日を超えたら注意）。"""

    def test_a_recent_update_is_fine(self):
        item = self.check_item("OS-UPDATES", "OK", "apt のリストの更新は 1 日前", "最後にパッケージを更新したのは 2 日前",
                               "未適用の更新は 0 件")
        self.assertIn("最新とは限りません", item.text)

    def test_pending_updates_are_informational(self):
        self.stub_data("apt_upgrade_s", "Reading package lists...\nThe following packages will be upgraded:\n"
                       "  libssl3 openssh-server openssh-client\n"
                       "Inst libssl3 [3.0.15-1~deb12u1] (3.0.16-1~deb12u1 Raspbian:12.11/stable [armhf])\n"
                       "Inst openssh-client [1:9.2p1-2+deb12u3] (1:9.2p1-2+deb12u5 Raspbian:12.11/stable [armhf])\n"
                       "Inst openssh-server [1:9.2p1-2+deb12u3] (1:9.2p1-2+deb12u5 Raspbian:12.11/stable [armhf])\n"
                       "Conf libssl3 (3.0.16-1~deb12u1 Raspbian:12.11/stable [armhf])\n"
                       "3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n")
        self.check_item("OS-UPDATES", "情報", "未適用の更新は 3 件")

    def test_the_forty_five_day_boundary(self):
        cases = [
            # (apt リストの日数, 最後の upgrade の日数, ラベル, 文言)
            (44.5, 44.5, "OK", "44 日前"),
            (45.1, 2, "OK", "45 日前"),
            (2, 45.1, "OK", "45 日前"),
            (46.1, 2, "注意", "46 日前"),
            (2, 46.1, "注意", "46 日前"),
            (100, 100, "注意", "100 日前"),
        ]
        def change(world, lists_days, upgrade_days):
            world.set_lists_age(lists_days)
            world.set_history(upgrade_days)
        variants = [(case, lambda world, case=case: change(world, case[0], case[1])) for case in cases]
        for (lists_days, upgrade_days, label, phrase), result in self.run_variants(variants):
            with self.subTest(lists=lists_days, upgrade=upgrade_days):
                item = self.check_item("OS-UPDATES", label, phrase, result=result)
                if label == "注意":
                    self.assertIn("45 日以上、更新が止まっています", item.text)
                    self.assertIn("sudo apt update && sudo apt full-upgrade", item.fix)
                    self.assertIn("harden.sh updates", item.fix)

    def test_upgrade_commands_that_count(self):
        cases = [("/usr/bin/unattended-upgrade", True), ("apt upgrade -y", True), ("apt full-upgrade", True),
                 ("apt-get dist-upgrade -y", True), ("apt install -y vim", False), ("apt update", False)]
        def change(world, command):
            stamp = datetime.datetime.fromtimestamp(world.now - 3 * DAY).strftime("%Y-%m-%d  %H:%M:%S")
            old = datetime.datetime.fromtimestamp(world.now - 90 * DAY).strftime("%Y-%m-%d  %H:%M:%S")
            world.put("/var/log/apt/history.log",
                      "Start-Date: {0}\nCommandline: apt upgrade\nEnd-Date: {0}\n\n"
                      "Start-Date: {1}\nCommandline: {2}\nEnd-Date: {1}\n".format(old, stamp, command))
        variants = [(case, lambda world, case=case: change(world, case[0])) for case in cases]
        for (command, counts), result in self.run_variants(variants):
            with self.subTest(command=command):
                item = self.check_item("OS-UPDATES", "OK" if counts else "注意", result=result)
                if counts:
                    self.assertIn("最後にパッケージを更新したのは 3 日前", item.text)
                else:
                    self.assertIn("最後にパッケージを更新したのは 90 日前", item.text)

    def test_rotated_logs_are_read_too(self):
        # apt の履歴は月ごとに回転する。最近の upgrade が history.log.1.gz にあっても、止まっているとは言わない。
        self.set_history(None, install_days=1)
        stamp = datetime.datetime.fromtimestamp(self.now - 10 * DAY).strftime("%Y-%m-%d  %H:%M:%S")
        path = self.sys("/var/log/apt/history.log.1.gz")
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write("Start-Date: {0}\nCommandline: /usr/bin/unattended-upgrade\nEnd-Date: {0}\n".format(stamp))
        self.check_item("OS-UPDATES", "OK", "最後にパッケージを更新したのは 10 日前")
        # 回転した古い（平文の）ログも読む。
        os.remove(path)
        self.put("/var/log/apt/history.log.2", "Start-Date: {0}\nCommandline: apt upgrade\nEnd-Date: {0}\n".format(stamp))
        self.check_item("OS-UPDATES", "OK", "10 日前")

    def test_no_upgrade_in_the_history_is_a_warning(self):
        self.set_history(None)
        self.check_item("OS-UPDATES", "注意", "パッケージを更新した記録（upgrade・full-upgrade・unattended-upgrade）がありません")

    def test_missing_lists_are_a_warning(self):
        shutil.rmtree(self.sys("/var/lib/apt/lists"))
        self.check_item("OS-UPDATES", "注意", "apt のリストの更新の記録がありません")

    def test_without_any_record_it_is_unknown(self):
        shutil.rmtree(self.sys("/var/lib/apt/lists"))
        shutil.rmtree(self.sys("/var/log/apt"))
        self.check_item("OS-UPDATES", "不明", "読めません")

    def test_without_apt_get_there_is_no_pending_count(self):
        self.remove_tool("apt-get")
        item = self.check_item("OS-UPDATES", "OK")
        self.assertNotIn("未適用", item.text)


class AutoUpdateTest(SecurityCheckCase):
    """OS-AUTOUPDATE: unattended-upgrades が入っていて、Raspbian・Raspberry Pi の更新元に当たる指定か。"""

    def test_cases(self):
        harden_style = APT_CONFIG_PERIODIC.format(unattended="1") + (
            'Unattended-Upgrade::Origins-Pattern:: "origin=Raspbian,codename=${distro_codename},label=Raspbian";\n'
            'Unattended-Upgrade::Origins-Pattern:: "origin=Raspberry Pi Foundation,'
            'codename=${distro_codename},label=Raspberry Pi Foundation";\n')
        debian_security = APT_CONFIG_PERIODIC.format(unattended="1") + (
            'Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename}-security,'
            'label=Debian-Security";\n')
        rpi_only = APT_CONFIG_PERIODIC.format(unattended="1") + (
            'Unattended-Upgrade::Origins-Pattern:: "origin=Raspberry Pi Foundation,'
            'codename=${distro_codename},label=Raspberry Pi Foundation";\n')
        archive = APT_CONFIG_PERIODIC.format(unattended="1") + (
            'Unattended-Upgrade::Origins-Pattern:: "o=Raspbian,a=stable";\n')
        wrong_codename = APT_CONFIG_PERIODIC.format(unattended="1") + (
            'Unattended-Upgrade::Origins-Pattern:: "origin=Raspbian,codename=bullseye,label=Raspbian";\n')
        unknown_key = APT_CONFIG_PERIODIC.format(unattended="1") + (
            'Unattended-Upgrade::Origins-Pattern:: "origin=Raspbian,bogus=1";\n')
        cases = [
            ("Raspbian と Raspberry Pi の指定", APT_POLICY_RASPBIAN, APT_CONFIG_RASPBIAN, "OK",
             "origin=Raspbian、origin=Raspberry Pi Foundation"),
            ("harden.sh が書く形", APT_POLICY_RASPBIAN, harden_style, "OK", "origin=Raspbian"),
            ("Raspberry Pi の更新元だけに当たる", APT_POLICY_RASPBIAN, rpi_only, "OK",
             "origin=Raspberry Pi Foundation"),
            ("o=、a= の短い書き方", APT_POLICY_RASPBIAN, archive, "OK", "o=Raspbian"),
            ("Debian 用の指定だけ（Raspbian には当たらない）", APT_POLICY_RASPBIAN, APT_CONFIG_DEBIAN_ONLY, "注意",
             "何も自動で更新されません"),
            ("Debian の更新元（64 ビット版）には、Debian 用の指定が当たる", APT_POLICY_DEBIAN, APT_CONFIG_DEBIAN_ONLY, "OK",
             "origin=Debian"),
            ("Debian-Security だけ", APT_POLICY_DEBIAN, debian_security, "OK", "origin=Debian"),
            ("コードネームが違う", APT_POLICY_RASPBIAN, wrong_codename, "注意", "何も自動で更新されません"),
            ("知らない項目の指定", APT_POLICY_RASPBIAN, unknown_key, "注意", "何も自動で更新されません"),
            ("毎日の実行が 0", APT_POLICY_RASPBIAN, APT_CONFIG_RASPBIAN.replace(
                'Unattended-Upgrade "1"', 'Unattended-Upgrade "0"'), "注意", "毎日の実行が有効になっていません"),
            ("毎日の実行が未設定", APT_POLICY_RASPBIAN, "\n".join(
                line for line in APT_CONFIG_RASPBIAN.splitlines() if "Periodic::Unattended" not in line) + "\n",
             "注意", "毎日の実行が有効になっていません"),
            ("指定が 1 つも無い", APT_POLICY_RASPBIAN, APT_CONFIG_PERIODIC.format(unattended="1"), "注意",
             "何も自動で更新されません"),
        ]
        variants = []
        for case in cases:
            def change(world, case=case):
                world.stub_data("apt_policy", case[1])
                world.stub_data("apt_config", case[2])
            variants.append((case, change))
        for (name, _policy, _config, label, phrase), result in self.run_variants(variants):
            with self.subTest(case=name):
                item = self.check_item("OS-AUTOUPDATE", label, phrase, result=result)
                if label == "注意":
                    self.assertEqual(item.fix, "sudo bash scripts/harden.sh updates")

    def test_not_installed(self):
        variants = [(status, lambda world, status=status: world.stub_data("uu_status", status))
                    for status in (None, "deinstall ok config-files")]
        for status, result in self.run_variants(variants):
            with self.subTest(status=status):
                item = self.check_item("OS-AUTOUPDATE", "注意", "入っていません", result=result)
                self.assertEqual(item.fix, "sudo bash scripts/harden.sh updates")

    def test_a_policy_without_release_lines_is_unknown(self):
        only_now = "Package files:\n 100 /var/lib/dpkg/status\n     release a=now\nPinned packages:\n"
        variants = [("a=now の行だけ", lambda world: world.stub_data("apt_policy", only_now)),
                    ("何も無い", lambda world: world.stub_data("apt_policy", ""))]
        (_n1, only_now_result), (_n2, empty_result) = self.run_variants(variants)
        # a=now の行だけでも「読み取れた」ことになり、指定は当たらないので注意になる。
        self.check_item("OS-AUTOUPDATE", "注意", result=only_now_result)
        self.check_item("OS-AUTOUPDATE", "不明", "更新元", result=empty_result)

    def test_missing_tools_are_unknown(self):
        variants = [(tool, lambda world, tool=tool: world.remove_tool(tool))
                    for tool in ("apt-config", "apt-cache", "dpkg-query")]
        for tool, result in self.run_variants(variants):
            with self.subTest(tool=tool):
                self.check_item("OS-AUTOUPDATE", "不明", result=result)

class TimeTest(SecurityCheckCase):
    """TIME-SYNC / TIME-SOURCE: 時刻の同期と、同期元（chrony か systemd-timesyncd か）。"""

    def test_synchronised_clock(self):
        self.check_item("TIME-SYNC", "OK", "同期しています")

    def test_unsynchronised_clock(self):
        item = self.check_item("TIME-SYNC", "注意", "同期していません", "RTC", NTP_SYNC="no")
        self.assertIn("UDP 123", item.fix)

    def test_unreadable_state(self):
        self.check_item("TIME-SYNC", "不明", NTP_SYNC="")
        self.remove_tool("timedatectl")
        self.check_item("TIME-SYNC", "不明", "timedatectl がない")

    def test_chrony_is_fine_and_its_sources_are_summarised(self):
        item = self.check_item("TIME-SOURCE", "OK", "chrony", "大きな時刻の飛びを受け付けません",
                               "同期元 3 個", "選択中: 192.0.2.10")
        self.assertEqual(item.fix, "")

    def test_chrony_without_chronyc_still_ok(self):
        self.remove_tool("chronyc")
        item = self.check_item("TIME-SOURCE", "OK", "chrony")
        self.assertNotIn("同期元", item.text)

    def test_timesyncd_accepts_forged_time(self):
        self.set_units(active=["ssh", "avahi-daemon", "systemd-timesyncd"])
        item = self.check_item("TIME-SOURCE", "注意", "systemd-timesyncd", "偽の時刻をそのまま受け入れます",
                               "違う時刻に鳴る・1 日鳴らない", TIMESYNC_SERVER="ntp.example.ac.jp")
        self.assertIn("ntp.example.ac.jp", item.text)
        self.assertEqual(item.fix, "sudo bash scripts/harden.sh ntp")

    def test_timesyncd_without_a_known_server(self):
        self.set_units(active=["ssh", "avahi-daemon", "systemd-timesyncd"])
        item = self.check_item("TIME-SOURCE", "注意", "systemd-timesyncd")
        self.assertNotIn("いまのサーバー", item.text)

    def test_neither_service_running(self):
        self.set_units(active=["ssh", "avahi-daemon"])
        item = self.check_item("TIME-SOURCE", "注意", "動いていません")
        self.assertEqual(item.fix, "sudo bash scripts/harden.sh ntp")

    def test_chrony_wins_when_both_are_running(self):
        self.set_units(active=["ssh", "chrony", "systemd-timesyncd"])
        self.check_item("TIME-SOURCE", "OK", "chrony")

    def test_without_systemctl_the_source_is_unknown(self):
        self.remove_tool("systemctl")
        self.check_item("TIME-SOURCE", "不明", "systemctl がない")


class ServicesTest(SecurityCheckCase):
    """SVC-BLUETOOTH / SVC-MODEM / SVC-AVAHI / SVC-RPICONNECT: 使っていない機能。"""

    def test_bluetooth_and_modem_manager(self):
        cases = [
            # (名前, 動作中, 起動時に有効, マスク, 項目, ラベル, 文言)
            ("bluetooth が動作中", ["ssh", "bluetooth"], [], [], "SVC-BLUETOOTH", "注意", "bluetooth.service（動作中）"),
            ("bluetooth が起動時に有効", ["ssh"], ["bluetooth"], [], "SVC-BLUETOOTH", "注意",
             "bluetooth.service（起動時に有効）"),
            ("hciuart が起動時に有効", ["ssh"], ["hciuart"], [], "SVC-BLUETOOTH", "注意", "hciuart.service（起動時に有効）"),
            ("両方", ["ssh", "bluetooth"], ["hciuart"], [], "SVC-BLUETOOTH", "注意",
             "bluetooth.service（動作中）、hciuart.service（起動時に有効）"),
            ("止めてマスクした", ["ssh"], [], ["bluetooth", "hciuart"], "SVC-BLUETOOTH", "OK", "動いておらず"),
            ("何も無い", ["ssh"], [], [], "SVC-BLUETOOTH", "OK", "動いておらず"),
            ("ModemManager が動作中", ["ssh", "ModemManager"], [], [], "SVC-MODEM", "注意",
             "ModemManager.service（動作中）"),
            ("ModemManager が起動時に有効", ["ssh"], ["ModemManager"], [], "SVC-MODEM", "注意",
             "ModemManager.service（起動時に有効）"),
            ("ModemManager をマスクした", ["ssh"], [], ["ModemManager"], "SVC-MODEM", "OK", "動いておらず"),
        ]
        variants = [(case, lambda world, case=case: world.set_units(case[1], case[2], case[3])) for case in cases]
        for (name, _a, _e, _m, item_id, label, phrase), result in self.run_variants(variants):
            with self.subTest(case=name):
                item = self.check_item(item_id, label, phrase, result=result)
                if label == "注意":
                    self.assertEqual(item.fix, "sudo bash scripts/harden.sh services")

    def test_avahi_is_kept_but_explained(self):
        item = self.check_item("SVC-AVAHI", "情報", "campus-chime.local", "防火壁")
        self.assertEqual(item.fix, "")
        self.set_units(active=["ssh", "chrony"])
        self.check_item("SVC-AVAHI", "OK", "動いていません")

    def test_without_systemctl_these_items_are_unknown(self):
        self.remove_tool("systemctl")
        result = self.run_check()
        for item_id in ("SVC-BLUETOOTH", "SVC-MODEM", "SVC-AVAHI"):
            self.assertEqual(result.labels(item_id), ["不明"], item_id)

    def test_rpi_connect(self):
        self.install_stub("rpi-connect")
        signed_in = "Signed in: yes\nScreen sharing: not allowed\nRemote shell: allowed\n"
        cases = [
            ("サインイン済み", signed_in, {}, "注意", "学外から遠隔操作できる経路"),
            ("サインインしていない", "Signed in: no\n", {}, "情報", "サインインしていません"),
            ("大文字小文字の違い", "signed in: YES\n", {}, "注意", "学外から遠隔操作できる経路"),
            ("読み取れない", "Something else\n", {}, "不明", "読み取れません"),
            ("失敗する", "", {"RPI_CONNECT_FAIL": "1"}, "不明", "読み取れません"),
        ]
        def change(world, case):
            world.install_stub("rpi-connect")
            world.stub_data("rpi_connect", case[1])
            return case[2]
        variants = [(case, lambda world, case=case: change(world, case)) for case in cases]
        for (name, _out, _env, label, phrase), result in self.run_variants(variants):
            with self.subTest(case=name):
                item = self.check_item("SVC-RPICONNECT", label, phrase, result=result)
                if label == "注意":
                    self.assertIn("rpi-connect signout", item.fix)

    def test_rpi_connect_runs_as_the_user_when_root_and_directly_otherwise(self):
        self.install_stub("rpi-connect")
        self.stub_data("rpi_connect", "Signed in: no\n")
        result = self.run_check()
        self.assertEqual(result.called("runuser -u pi -- rpi-connect"), ["runuser -u pi -- rpi-connect status"])
        result = self.run_check(root=False)
        self.assertEqual(result.called("runuser"), [])
        self.assertEqual(result.called("rpi-connect"), ["rpi-connect status"])

    def test_absent_rpi_connect_prints_nothing(self):
        self.assertEqual(self.run_check().all("SVC-RPICONNECT"), [])


class LogPersistTest(SecurityCheckCase):
    def test_a_persistent_journal_is_fine(self):
        self.check_item("LOG-PERSIST", "OK", "再起動のあとも残ります")

    def test_a_volatile_journal_is_a_warning(self):
        shutil.rmtree(self.sys("/var/log/journal"))
        item = self.check_item("LOG-PERSIST", "注意", "再起動で消えます")
        self.assertEqual(item.fix, "sudo bash scripts/harden.sh journal")

    def test_a_plain_file_is_not_a_journal_directory(self):
        shutil.rmtree(self.sys("/var/log/journal"))
        self.put("/var/log/journal", "")
        self.check_item("LOG-PERSIST", "注意")


class ChimeServiceTest(SecurityCheckCase):
    """CHIME-UNIT / CHIME-SANDBOX: 時報サービスの定義と、隔離の設定。"""

    UNIT = "/etc/systemd/system/campus_chime.service"
    DROP_IN = "/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf"

    def unit_text(self) -> str:
        with open(SERVICE_FILE, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_the_repository_copy_is_fine(self):
        self.check_item("CHIME-UNIT", "OK", "pi で動き", "リポジトリの版と同じ")

    def test_the_user_must_not_be_root(self):
        cases = [("User=root", "User=root"), ("User=0", "User=0"), ("User= が無い", None)]
        def change(world, replacement):
            text = world.unit_text().replace("User=pi\n", (replacement + "\n") if replacement else "")
            world.put(world.UNIT, text)
        variants = [(case, lambda world, case=case: change(world, case[1])) for case in cases]
        for (name, _replacement), result in self.run_variants(variants):
            with self.subTest(case=name):
                item = self.check_item("CHIME-UNIT", "要対応", "root で動く設定です", result=result)
                self.assertIn("bash scripts/setup.sh --no-apt", item.fix)
                self.assertIn("User=pi", item.fix)

    def test_a_different_installed_copy_is_a_warning(self):
        self.put(self.UNIT, self.unit_text().replace("RestartSec=10", "RestartSec=99"))
        item = self.check_item("CHIME-UNIT", "注意", "リポジトリの版と違います")
        self.assertEqual(item.fix, "bash scripts/setup.sh --no-apt  で入れ直す")

    def test_a_missing_unit_is_a_warning_and_skips_the_sandbox_item(self):
        os.remove(self.sys(self.UNIT))
        result = self.run_check()
        item = self.check_item("CHIME-UNIT", "注意", "入っていません", result=result)
        self.assertIn("setup.sh --no-apt", item.fix)
        self.assertEqual(result.all("CHIME-SANDBOX"), [])

    def test_the_sandbox_drop_in(self):
        item = self.check_item("CHIME-SANDBOX", "OK", "50-campus-chime-harden.conf", "3.1 OK")
        self.assertEqual(item.fix, "")
        os.remove(self.sys(self.DROP_IN))
        self.stub_data("analyze", SYSTEMD_ANALYZE_EXPOSED)
        item = self.check_item("CHIME-SANDBOX", "注意", "隔離する設定がありません",
                               "Overall exposure level for campus_chime.service: 8.7 EXPOSED")
        self.assertEqual(item.fix, "sudo bash scripts/harden.sh service")
        # 絵文字などは出さない。
        self.assertNotIn("\U0001F625", item.text)

    def test_the_score_is_optional(self):
        os.remove(self.sys(self.DROP_IN))
        self.remove_tool("systemd-analyze")
        item = self.check_item("CHIME-SANDBOX", "注意", "隔離する設定がありません")
        self.assertNotIn("systemd-analyze", item.text)
        self.stub_data("analyze", "")
        self.install_stub("systemd-analyze")
        item = self.check_item("CHIME-SANDBOX", "注意")
        self.assertNotIn("Overall", item.text)


class RepoRemoteTest(SecurityCheckCase):
    """REPO-REMOTE: 取得元は暗号化された GitHub の URL か。"""

    def test_urls(self):
        cases = [
            ("https://github.com/hiroyuki-rdx/chime-5pm.git", "OK", "暗号化された GitHub"),
            ("https://github.com/hiroyuki-rdx/chime-5pm", "OK", "暗号化された GitHub"),
            ("http://github.com/hiroyuki-rdx/chime-5pm.git", "要対応", "暗号化されていません"),
            ("git://github.com/hiroyuki-rdx/chime-5pm.git", "要対応", "暗号化されていません"),
            ("git@github.com:hiroyuki-rdx/chime-5pm.git", "情報", "GitHub の https:// 以外"),
            ("ssh://git@github.com/hiroyuki-rdx/chime-5pm.git", "情報", "GitHub の https:// 以外"),
            ("https://gitlab.example.ac.jp/lab/chime.git", "情報", "GitHub の https:// 以外"),
            ("/srv/git/chime.git", "情報", "/srv/git/chime.git"),
        ]
        variants = [(case, lambda world, case=case: world.stub_data("git_remote", case[0] + "\n")) for case in cases]
        for (url, label, phrase), result in self.run_variants(variants):
            with self.subTest(url=url):
                item = self.check_item("REPO-REMOTE", label, phrase, result=result)
                if label == "要対応":
                    self.assertIn("git remote set-url origin https://github.com/hiroyuki-rdx/chime-5pm.git", item.fix)

    def test_credentials_in_the_url_are_never_shown(self):
        urls = ["https://hiroyuki:ghp_SECRETTOKEN123@github.com/hiroyuki-rdx/chime-5pm.git",
                "https://ghp_SECRETTOKEN123@github.com/hiroyuki-rdx/chime-5pm.git",
                "http://user:SECRETTOKEN123@github.com/hiroyuki-rdx/chime-5pm.git"]
        variants = [(url, lambda world, url=url: world.stub_data("git_remote", url + "\n")) for url in urls]
        for url, result in self.run_variants(variants):
            with self.subTest(url=url):
                self.assertNotIn("SECRETTOKEN123", result.stdout)
                self.assertIn("github.com/hiroyuki-rdx/chime-5pm.git", result.one("REPO-REMOTE").text)

    def test_unreadable_remote_or_missing_git(self):
        self.stub_data("git_remote", None)
        self.check_item("REPO-REMOTE", "不明", "origin")
        self.remove_tool("git")
        result = self.run_check()
        for item_id in ("REPO-REMOTE", "REPO-STRAY"):
            self.assertEqual(result.labels(item_id), ["不明"], item_id)
        # フックのファイルは git が無くても見られる。
        self.assertEqual(result.labels("REPO-HOOKS"), ["OK"])

    def test_git_is_run_as_the_user_when_root_and_read_only(self):
        result = self.run_check()
        runuser_calls = result.called("runuser")
        self.assertEqual(len(runuser_calls), 3, runuser_calls)
        for call in runuser_calls:
            self.assertRegex(call, r"^runuser -u pi -- env GIT_OPTIONAL_LOCKS=0 git -c core\.fsmonitor=false -C ")
        # git の呼び出しは、すべて runuser 経由（root のまま git を動かさない）。
        self.assertEqual(len(result.called("git ")), len(runuser_calls))
        with open(self.calls_log + ".git-locks", "r", encoding="utf-8") as handle:
            self.assertEqual(set(handle.read().split()), {"0"})

    def test_git_runs_directly_without_root(self):
        result = self.run_check(root=False)
        self.assertEqual(result.called("runuser"), [])
        git_calls = result.called("git ")
        self.assertEqual(len(git_calls), 3, git_calls)
        for call in git_calls:
            self.assertTrue(call.startswith("git -c core.fsmonitor=false -C {0} ".format(self.repo)), call)
        with open(self.calls_log + ".git-locks", "r", encoding="utf-8") as handle:
            self.assertEqual(set(handle.read().split()), {"0"})


class RepoPermsTest(SecurityCheckCase):
    """REPO-PERMS: 誰でも書き込める、または利用者の持ち物でないファイル。"""

    def test_a_clean_repository(self):
        self.check_item("REPO-PERMS", "OK", "書き込めるファイルや")

    def test_world_writable_files_and_directories(self):
        def change(world, relative):
            path = os.path.join(world.repo, relative)
            os.chmod(path, os.stat(path).st_mode | 0o002)
        cases = ["campus_chime.py", "scripts/security_check.sh", ".git/config", "scripts"]
        variants = [(case, lambda world, case=case: change(world, case)) for case in cases]
        for relative, result in self.run_variants(variants):
            with self.subTest(path=relative):
                item = self.check_item("REPO-PERMS", "要対応", relative.rsplit("/", 1)[-1], "1 件", result=result)
                self.assertIn("chmod -R o-w", item.fix)
                self.assertIn("sudo chown -R pi:pi", item.fix)

    def test_files_owned_by_someone_else(self):
        # 偽の getent が返す pi の uid を、ファイルの持ち主と違うものにする。
        result = self.run_check(PI_UID=str(os.getuid() + 1))
        item = self.check_item("REPO-PERMS", "要対応", "pi の持ち物でないファイル", result=result)
        self.assertRegex(item.text, r"\d+ 件あります")

    def test_at_most_five_paths_are_listed(self):
        for number in range(8):
            path = write(os.path.join(self.repo, "extra", "f{0}.txt".format(number)), "x")
            os.chmod(path, 0o666)
        item = self.check_item("REPO-PERMS", "要対応")
        self.assertIn("ほか", item.text)
        listed = item.text.split(": ", 1)[1].split("。", 1)[0]
        self.assertEqual(len(listed.replace("、ほか", "").split("、")), 5)
        self.assertIn("8 件", item.text)

    def test_symlinks_are_ignored_and_deep_files_are_not_scanned(self):
        os.symlink("campus_chime.py", os.path.join(self.repo, "link-to-code"))
        deep = write(os.path.join(self.repo, "a", "b", "c", "d", "e", "deep.txt"), "x")
        os.chmod(deep, 0o666)
        self.check_item("REPO-PERMS", "OK")

    def test_a_missing_find_is_unknown(self):
        self.remove_tool("find")
        self.check_item("REPO-PERMS", "不明", "find コマンドがない")


class RepoHooksAndStrayTest(SecurityCheckCase):
    """REPO-HOOKS / REPO-STRAY / PY-USERSITE: 仕込まれたコードの入り口。"""

    def test_sample_hooks_are_fine(self):
        self.check_item("REPO-HOOKS", "OK")

    def test_hooks(self):
        write(os.path.join(self.repo, ".git", "hooks", "post-merge"), "#!/bin/sh\n", 0o755)
        write(os.path.join(self.repo, ".git", "hooks", "post-checkout"), "#!/bin/sh\n", 0o755)
        item = self.check_item("REPO-HOOKS", "注意", "post-checkout", "post-merge")
        self.assertNotIn("pre-commit.sample", item.text)
        self.assertIn("core.hooksPath", item.fix)

    def test_hooks_path_setting(self):
        self.stub_data("git_hookspath", ".githooks\n")
        self.check_item("REPO-HOOKS", "注意", "core.hooksPath=.githooks")

    def test_stray_files(self):
        self.stub_data("git_status", "?? config.json\n M campus_chime.py\n?? evil.py\n?? scripts/run.sh\n")
        item = self.check_item("REPO-STRAY", "注意", "（2 件）", "evil.py", "scripts/run.sh")
        self.assertNotIn("config.json", item.text)
        self.assertNotIn("campus_chime.py", item.text)
        self.assertIn("git status", item.fix)

    def test_only_config_json_or_modified_tracked_files_are_fine(self):
        self.stub_data("git_status", "?? config.json\n M campus_chime.py\nA  newfile.py\n")
        self.check_item("REPO-STRAY", "OK")

    def test_at_most_five_stray_paths_are_listed(self):
        self.stub_data("git_status", "".join("?? tools/f{0}.py\n".format(n) for n in range(8)))
        item = self.check_item("REPO-STRAY", "注意", "（8 件）")
        self.assertEqual(item.text.count("tools/f"), 5)

    def test_a_git_that_cannot_open_the_repository_is_unknown(self):
        result = self.run_check(GIT_STATUS_FAIL="1")
        self.assertEqual(result.labels("REPO-STRAY"), ["不明"])
        self.assertIn("持ち主", result.one("REPO-STRAY").text)

    def test_python_user_site_pygame(self):
        self.check_item("PY-USERSITE", "OK")
        def change(world, version):
            makedirs(world.sys("/home/pi/.local/lib/{0}/site-packages/pygame".format(version)))
        variants = [(version, lambda world, version=version: change(world, version))
                    for version in ("python3.9", "python3.11")]
        for version, result in self.run_variants(variants):
            with self.subTest(version=version):
                item = self.check_item("PY-USERSITE", "注意", "pip で入れた pygame", version, result=result)
                self.assertIn("pip3 uninstall pygame", item.fix)

class HardenPendingTest(SecurityCheckCase):
    """HARDEN-PENDING: harden.sh の確定待ちの変更が残っていないか。"""

    STATE = "/var/lib/campus-chime-harden"

    def test_never_used(self):
        self.check_item("HARDEN-PENDING", "OK", "harden.sh はまだ使われていません")
        self.check_item("HARDEN-PENDING", "OK", "harden.sh はまだ使われていません", root=False)

    def test_used_but_nothing_pending(self):
        makedirs(self.sys(self.STATE + "/applied"))
        self.check_item("HARDEN-PENDING", "OK", "確定待ちの変更はありません")

    def test_a_pending_change(self):
        self.put(self.STATE + "/pending", "ssh 1760000000\n", 0o600)
        item = self.check_item("HARDEN-PENDING", "要対応", "まだ確定していません", "10 分")
        self.assertIn("sudo bash scripts/harden.sh --confirm", item.fix)
        self.assertIn("新しい画面から", item.fix)

    def test_without_root_the_state_directory_cannot_be_read(self):
        self.put(self.STATE + "/pending", "ssh 1760000000\n", 0o600)
        item = self.check_item("HARDEN-PENDING", "不明", "root でないため", root=False)
        self.assertEqual(item.hint, "sudo bash scripts/security_check.sh")


KNOWN_HARDEN_WORDS = {"ssh", "firewall", "sudo", "services", "journal", "updates", "service", "ntp", "--confirm"}


class SummaryAndExitCodeTest(SecurityCheckCase):
    """終了コード・まとめの行・直し方の書き方。"""

    def test_an_error_makes_the_exit_code_one(self):
        self.stub_data("nft", NFT_ACCEPT)
        result = self.run_check()
        self.assertEqual(result.code, 1)
        self.assertEqual(result.summary(), (1, 0, 0))
        self.assertEqual(result.stderr, "")

    def test_warnings_and_unknowns_alone_keep_the_exit_code_zero(self):
        shutil.rmtree(self.sys("/var/log/journal"))
        result = self.run_check(root=False)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.summary(), (0, 1, 2))
        # 要対応が 1 件でもあれば、注意や不明が何件あっても 1。
        self.stub_data("sshd_T", SSHD_T_TEMPLATE.format(root="no", password="yes", kbd="no"))
        result = self.run_check()
        self.assertEqual(result.code, 1)
        self.assertEqual(result.summary()[0], 1)

    def test_the_summary_counts_the_lines(self):
        self.make_bad_world()
        for root in (True, False):
            with self.subTest(root=root):
                result = self.run_check(root=root)
                counts = tuple(sum(1 for item in result.items if item.label == label)
                               for label in ("要対応", "注意", "不明"))
                self.assertEqual(result.summary(), counts)
                self.assertGreater(counts[0], 0)
                self.assertEqual(result.code, 1)
                self.assertEqual(result.stderr, "")

    def test_every_warning_and_error_names_a_known_harden_step(self):
        self.make_bad_world()
        result = self.run_check()
        steps_named = set()
        for item in result.items:
            if item.label in ("注意", "要対応"):
                self.assertTrue(item.fix, "直し方が無い: {0}".format(item))
        for word in re.findall(r"harden\.sh ([A-Za-z-]+)", result.stdout):
            self.assertIn(word, KNOWN_HARDEN_WORDS, word)
            steps_named.add(word)
        # 悪い状態をそろえたので、harden.sh のすべての手順が、どこかの直し方に出てくる。
        self.assertTrue({"ssh", "firewall", "sudo", "services", "journal", "updates", "service", "ntp"}
                        <= steps_named, steps_named)
        self.assertIn("--confirm", steps_named)

    def test_a_fix_always_follows_its_own_item(self):
        # 直し方（→）は、必ず項目の行か、その続きの行の直後に付く（見出しや空行の後ろには付かない）。
        self.make_bad_world()
        lines = self.run_check().stdout.splitlines()
        previous_is_item = False
        for line in lines:
            if line.startswith("    → "):
                self.assertTrue(previous_is_item, line)
            previous_is_item = bool(LABEL_LINE.match(line)) or line.startswith("      ") or line.startswith("    → ")

    def test_no_color_codes_when_the_output_is_not_a_terminal(self):
        self.make_bad_world()
        result = self.run_check()
        self.assertNotIn("\033", result.stdout)
        self.assertNotIn("\033", result.stderr)

    def test_the_output_does_not_depend_on_the_callers_locale(self):
        base = self.run_check()
        other = self.run_check(LANG="ja_JP.UTF-8", LC_ALL="ja_JP.UTF-8", LC_MESSAGES="ja_JP.UTF-8")
        self.assertEqual(base.stdout, other.stdout)

    def test_a_trailing_slash_in_the_sysroot_is_fine(self):
        base = self.run_check()
        other = self.run_check(CHIME_SYSROOT=self.root + "/")
        self.assertEqual(base.stdout, other.stdout)

    def test_all_good_output_is_stable_between_runs(self):
        # 時刻に依存する「N 日前」も、同じ状態で続けて動かせば同じ。
        self.assertEqual(self.run_check().stdout, self.run_check().stdout)

    def test_the_sections_come_in_order(self):
        headings = re.findall(r"^== (.+)$", self.run_check().stdout, re.MULTILINE)
        self.assertEqual(headings, ["対象", "ネットワーク（どこから届くか）", "SSH", "sudo と利用者",
                                    "OS とカーネルの更新", "時刻", "使っていないサービス", "ログ",
                                    "時報サービス", "リポジトリ", "harden.sh の状態", "まとめ"])


class ReadOnlyTest(SecurityCheckCase):
    """何も変更しない: 偽の /・リポジトリ・TMPDIR を、実行の前後で比べる。"""

    WRITERS = ("cp", "mv", "rm", "rmdir", "mkdir", "touch", "chmod", "chown", "mktemp", "tee", "ln", "dd",
               "install", "truncate", "patch", "ed", "shred", "sponge")

    def all_state(self):
        return snapshot(self.root, self.repo, self.tmpdir, self.elsewhere, self.data)

    def test_the_path_has_no_writing_tools(self):
        present = set(os.listdir(self.bin_dir)) | set(os.listdir(self.tools_dir))
        self.assertEqual(sorted(present & set(self.WRITERS)), [])
        # PATH はこの 2 つのフォルダだけ。
        self.assertEqual(self.environment(True, {})["PATH"], self.bin_dir + os.pathsep + self.tools_dir)

    def test_sed_and_find_refuse_the_writing_forms(self):
        sed = os.path.join(self.tools_dir, "sed")
        find = os.path.join(self.tools_dir, "find")
        target = write(os.path.join(self.elsewhere, "victim.txt"), "abc\n")
        self.assertNotEqual(subprocess.run([sed, "-i", "s/a/b/", target], capture_output=True).returncode, 0)
        self.assertNotEqual(subprocess.run([sed, "--in-place", "s/a/b/", target], capture_output=True).returncode, 0)
        self.assertNotEqual(subprocess.run([find, self.elsewhere, "-delete"], capture_output=True).returncode, 0)
        self.assertNotEqual(subprocess.run([find, self.elsewhere, "-exec", "true", ";"],
                                           capture_output=True).returncode, 0)
        with open(target, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "abc\n")
        # 読む形は通る。
        self.assertEqual(subprocess.run([sed, "-n", "1p", target], capture_output=True, text=True).stdout, "abc\n")

    def assert_only_reading_calls(self, result: Result, root: bool) -> None:
        for call in result.calls:
            self.assertTrue(any(pattern.match(call) for pattern in ALLOWED_CALLS), "読み取り以外の呼び出し: " + call)
        sudo_calls = result.called("sudo")
        self.assertEqual(sudo_calls, [] if root else ["sudo -n -l"])
        verbs = {call.split()[1] for call in result.called("systemctl")}
        self.assertTrue(verbs <= {"is-active", "is-enabled"}, verbs)
        for call in result.calls:
            self.assertNotIn("nft -f", call)
            self.assertNotIn("--hard", call)
            self.assertFalse(call.startswith(("apt-get install", "apt-get update", "apt-get upgrade")), call)
        with open(self.calls_log + ".git-locks", "r", encoding="utf-8") as handle:
            self.assertEqual(set(handle.read().split()), {"0"})

    def test_nothing_is_written_anywhere(self):
        scenarios = [("すべて良い・root", False, True), ("すべて良い・root でない", False, False),
                     ("悪い状態・root", True, True), ("悪い状態・root でない", True, False)]
        for name, bad, root in scenarios:
            with self.subTest(scenario=name):
                world = type(self)(self._testMethodName)
                world.build_world()
                self.addCleanup(world.doCleanups)
                if bad:
                    world.make_bad_world()
                before = world.all_state()
                result = world.run_check(root=root)
                after = world.all_state()
                self.assertEqual(result.code, 1 if bad else 0, result.stdout)
                # 実行のたびに増えるのは、呼び出しの記録（CALLS_LOG）だけ。それは tmp の外側にある。
                changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
                self.assertEqual(changed, [], "変更されたもの: " + ", ".join(changed))
                self.assertEqual(os.listdir(world.tmpdir), [], "TMPDIR に一時ファイルが残った")
                world.assert_only_reading_calls(result, root)

    def test_the_script_does_not_create_files_even_when_the_sysroot_is_read_only(self):
        # 書こうとすると失敗する状態でも、結果が変わらない（root 以外のとき。root は権限を無視できる）。
        if os.geteuid() == 0:
            self.skipTest("root は書き込み禁止を無視できる")
        base = self.run_check(root=False)
        for directory in (self.root, self.repo, self.tmpdir, self.elsewhere):
            for here, names, _files in os.walk(directory):
                os.chmod(here, 0o555)
        self.addCleanup(lambda: [os.chmod(here, 0o755) for here, _n, _f in os.walk(self.tmp)])
        self.assertEqual(self.run_check(root=False).stdout, base.stdout)


def code_of(text: str) -> str:
    """コメント行と、' ' で囲んだ文字列を除いたスクリプトの本文（命令や書き込みを探すため）。"""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return re.sub(r"'[^']*'", "''", "\n".join(lines), flags=re.DOTALL)


class StaticScriptTest(unittest.TestCase):
    """スクリプトの書き方（読み取り専用・テスト用の入口・シェルの流儀）を、本文から確かめる。"""

    def setUp(self):
        with open(CHECK_SH, "r", encoding="utf-8") as handle:
            self.text = handle.read()
        self.code = code_of(self.text)

    def test_header_and_options(self):
        self.assertEqual(self.text.splitlines()[0], "#!/usr/bin/env bash")
        self.assertIn("\nset -euo pipefail\n", self.text)
        self.assertIn('export LC_ALL=C', self.text)
        self.assertTrue(os.access(CHECK_SH, os.R_OK))

    def test_it_is_valid_bash(self):
        proc = subprocess.run([BASH, "-n", CHECK_SH], capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck が無い環境では、CI の段に任せる")
    def test_shellcheck_has_no_findings(self):
        proc = subprocess.run(["shellcheck", CHECK_SH], capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_no_temp_files_here_documents_or_here_strings(self):
        self.assertNotRegex(self.code, r"<<")
        self.assertNotRegex(self.code, r"\bmktemp\b")
        self.assertNotIn("/tmp", self.code)
        self.assertNotIn("TMPDIR", self.code)

    def test_redirections_only_discard_or_merge_output(self):
        allowed = {">/dev/null", "2>/dev/null", ">&2", "2>&1", ">&"}
        # 二重引用符の中にある $( ... ) の中の > も調べるので、引用符は外さない。
        found = set(re.findall(r"(?<![<>&\-])(\d?>>?&?[^\s)\";]*)", self.code))
        stray = sorted(token for token in found if token not in allowed)
        self.assertEqual(stray, [], "ファイルへの書き込みになりうる >: {0}".format(stray))

    def test_no_writing_commands(self):
        writers = r"(rm|cp|mv|mkdir|rmdir|touch|chmod|chown|mktemp|tee|ln|dd|install|truncate|shred)"
        position = r"(?:^|[|;&(!`]|\bthen\b|\bdo\b|\belse\b|\bif\b|\$\()\s*"
        # 画面に出す文面の行（「sudo chown … && chmod …」のような案内を含む）は、命令ではない。
        commands = "\n".join(line for line in self.code.splitlines()
                             if not re.match(r"^\s*(emit|more|fixline|need_root|usage_error|printf)\b", line))
        self.assertNotRegex(commands, r"(?m)" + position + writers + r"\b")
        self.assertNotRegex(commands, r"\bsed\s+-[a-z]*i\b")
        self.assertNotRegex(commands, r"\s-(delete|exec|execdir|fprint\w*|fls)\b")

    def test_programs_are_called_by_bare_name(self):
        self.assertNotRegex(self.code, r"(?<![\w.${}/-])/(?:usr/)?(?:local/)?s?bin/\w+")

    def test_system_paths_go_through_the_sysroot(self):
        message = re.compile(r"^\s*(emit |more |fixline |reasons\+=|via=|pattern=|USER_HOME=|if \[ -z \"\$\{USER_HOME\}\""
                             r"|case \"\$\{pattern\}\" in /\*\) ;; \*\) pattern=)")
        path = re.compile(r"/(etc|var|run|lib|home|proc)/\w")
        offenders = []
        for line in self.code.splitlines():
            if path.search(line) and "${SYSROOT}" not in line and not message.match(line):
                offenders.append(line.strip())
        self.assertEqual(offenders, [])

    def test_sudo_is_run_in_exactly_one_place_and_only_to_list(self):
        # 本文で sudo を実行するのは、sudo -n -l の 1 か所だけ（ほかの sudo は、画面に出す案内の文字列）。
        executed = [line.strip() for line in self.code.splitlines()
                    if re.search(r"=\"\$\(sudo |\bif out=\"\$\(sudo ", line)]
        self.assertEqual(executed, ['if out="$(sudo -n -l 2>&1)"; then'])

    def test_help_text_is_the_leading_comment_block_and_documents_the_options(self):
        block = header_block()
        for needle in ("--user", "--help", "sudo bash scripts/security_check.sh", "終了コード", "何も変更しない"):
            self.assertIn(needle, block)


class RobustnessTest(SecurityCheckCase):
    """道具や設定が欠けていても、途中で止まらず、まとめまで出す（set -e で落ちない）。"""

    def test_with_no_tools_at_all_it_still_finishes(self):
        for name in list(STUB_BODIES):
            self.remove_tool(name)
        result = self.run_check()
        self.assertEqual(result.code, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.summary()[0], 0)
        # 調べられなかった項目は、不明になる（良さそうに見せかけない）。
        for item_id in ("NET-ADDR4", "NET-ADDR6", "NET-LISTEN", "NET-FW", "SSH-ACTIVE", "SSH-FAILED", "SSH-LOGINS",
                        "SUDO-NOPASSWD", "USER-PASSWORD", "OS-KERNEL", "OS-AUTOUPDATE", "TIME-SYNC", "TIME-SOURCE",
                        "SVC-BLUETOOTH", "SVC-MODEM", "SVC-AVAHI", "REPO-REMOTE", "REPO-STRAY"):
            with self.subTest(item=item_id):
                self.assertEqual(result.labels(item_id), ["不明"], result.stdout)

    def test_an_empty_sysroot_finishes_and_reports_what_is_missing(self):
        shutil.rmtree(self.root)
        makedirs(self.root)
        result = self.run_check()
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.labels("SSH-KEYS"), ["注意"])
        self.assertEqual(result.labels("LOG-PERSIST"), ["注意"])
        self.assertEqual(result.labels("CHIME-UNIT"), ["注意"])
        self.assertEqual(result.labels("OS-RELEASE"), ["不明"])
        self.assertEqual(result.labels("OS-UPDATES"), ["不明"])
        result.summary()

    def test_odd_apt_lines_do_not_abort_the_script(self):
        # 空の項目（連続したカンマ）や、= の無い項目があっても落ちない。
        self.stub_data("apt_policy", APT_POLICY_RASPBIAN.replace(
            "release o=Raspbian,a=stable", "release o=Raspbian,,a=stable,junk,=x"))
        self.stub_data("apt_config", APT_CONFIG_RASPBIAN + 'Unattended-Upgrade::Origins-Pattern:: ",,=,junk";\n'
                       'Unattended-Upgrade::Origins-Pattern:: "";\n')
        result = self.run_check()
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.labels("OS-AUTOUPDATE"), ["OK"])
        self.assertEqual(len(result.all("REPO-STRAY")), 1)

    def test_other_key_types_are_recognised(self):
        keys = (AUTHORIZED_KEY
                + "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEXAMPLEEXAMPLE0000 e@pc\n"
                + "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29tAAAAIEXAMPLEEXAMPLE0000 s@pc\n")
        self.put("/home/pi/.ssh/authorized_keys", keys, 0o600)
        self.check_item("SSH-KEYS", "OK", "公開鍵が 3 個", "ecdsa-sha2-nistp256 が 1 個",
                        "sk-ssh-ed25519@openssh.com が 1 個", "ssh-ed25519 が 1 個")

    def test_stray_names_with_spaces_are_shown_as_git_prints_them(self):
        self.stub_data("git_status", '?? "docs/a b.txt"\n?? 日本語.py\n')
        item = self.check_item("REPO-STRAY", "注意", "（2 件）", '"docs/a b.txt"', "日本語.py")
        self.assertIn("git status", item.fix)

    def test_a_dash_user_name_cannot_inject_options(self):
        # 先頭が - のユーザー名は使い方の間違い（コマンドの引数に化けない）。
        result = self.run_check("--user", "-oProxyCommand=x")
        self.assertEqual(result.code, 2)
        self.assertEqual(result.calls, [])


# -- このファイルのテストを並べて動かす ----------------------------------------------------
#
# スクリプトは 1 回の実行に 0.2 秒ほどかかる（偽の道具を 50 回ほど起動する）ので、テストを 1 つずつ
# 順に動かすと 1 分近くかかる。どのテストも自分専用の偽の / を作り、ほかのテストと何も共有しない
# （環境変数・umask・カレントフォルダも変えない）ので、いくつかのスレッドで並べて動かす。
# 1 つのテストだけを名前で指定して動かすときは、並べず、ふつうに動かす。

def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for test in _flatten(item):
                yield test
        else:
            yield item


class _SerializedResult:
    """結果の記録（失敗の一覧など）を、スレッドが 1 つずつ順に書くようにする入れ物。"""

    def __init__(self, result):
        self._result = result
        self._lock = threading.RLock()

    def __getattr__(self, name):
        attribute = getattr(self._result, name)
        if not callable(attribute):
            return attribute

        def locked(*args, **kwargs):
            with self._lock:
                return attribute(*args, **kwargs)
        return locked


class ParallelSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        shared = _SerializedResult(result)
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(lambda test: test(shared), list(_flatten(self))))
        return result


def load_tests(loader, standard_tests, pattern):
    return ParallelSuite(standard_tests)
