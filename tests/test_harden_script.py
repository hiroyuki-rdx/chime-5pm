"""安全対策スクリプト（``scripts/harden.sh``）のテスト。

harden.sh は root で動き、SSH・sudo・防火壁・サービスを書き換える。間違えると Pi に入れなくなる
（締め出し）か、放送が鳴らなくなる。この 2 つを防ぐため、次を固定する。

* どの手順も、変える前に条件をすべて確かめ、満たさなければ **何も変えずに** 止まる。
* 変えたあとに効いたことを確かめ、確かめられなければ、その手順だけ元に戻して止まる。
  戻せなかったときは「元に戻しました」と言わない。
* 何度実行しても同じ。``--rollback`` で、変える前のファイルの内容と mode が **そのまま** 戻る。
* ssh・firewall は「確定待ち」にし、**変更を有効にする前に** 自動復元の時計を張る。時計が実行するのは、
  リポジトリ（pi が書ける）ではなく root の写し。確定されなければ、必ずやり切って戻す。

スクリプトは本物を ``bash`` で動かす。絶対パスはすべて ``CHIME_SYSROOT`` 配下の使い捨ての
ディレクトリに向け、外のコマンド（systemctl・sshd・nft・apt-get など、システムに触るもの）は、
呼ばれた記録を残して状態を真似する偽物に差し替える。``PATH`` は、偽物のディレクトリと、無害な道具
（cat・grep・python3 など）への許可リストだけにする。偽物が無いコマンドは「見つからない」で失敗する。
本物の nft が使えるとき（root で ``nft -c -f`` が通るとき）は、作ったルールを検査モードだけで検査する。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO_ROOT

HARDEN_SH = os.path.join(REPO_ROOT, "scripts", "harden.sh")
UNIT_FILE = os.path.join(REPO_ROOT, "campus_chime.service")

#: 無害な道具の許可リスト。これ以外の本物は PATH に置かない。
TOOLS = ("bash", "sh", "env", "cat", "cp", "mv", "rm", "mkdir", "rmdir", "ln", "touch", "ls", "find",
         "grep", "sed", "awk", "gawk", "mawk", "sort", "uniq", "head", "tail", "cut", "tr", "wc",
         "printf", "date", "mktemp", "dirname", "basename", "readlink", "realpath", "cmp", "diff",
         "tee", "xargs", "true", "false", "test", "getconf")

PI_UID = 1000
OTHER_UID = 4242
#: 偽の /proc/stat の起動時刻（エポック秒）。
BTIME = 1_000_000_000

#: 偽物が共有する関数。呼ばれた記録を残し、``fail`` に書かれた呼び出しを失敗させる
#: （``once:`` で始まる行は 1 回だけ失敗させる）。
STUB_LIB = r"""
SIM="$SIM_DIR"
stub_begin() {
  stub_name="$1"; shift
  echo "$stub_name $*" >> "$CALLS_LOG"
  [ -f "$SIM/fail" ] || return 0
  while IFS= read -r pat || [ -n "$pat" ]; do
    [ -n "$pat" ] || continue
    once=0
    case "$pat" in once:*) once=1; pat="${pat#once:}" ;; esac
    crash=0
    case "$pat" in crash:*) crash=1; pat="${pat#crash:}" ;; esac
    case "$stub_name $*" in
      "$pat"*)
        if [ "$crash" = 1 ]; then
          # 実行中のスクリプトを即座に殺す（電源断や kill -9 の真似。EXIT の後始末は動かない）
          kill -KILL "$(cat "$SIM/script_pid")"
          sleep 1
          exit 137
        fi
        echo "stub: forced failure of $stub_name $*" >&2
        if [ "$once" = 1 ]; then
          grep -vFx -- "once:$pat" "$SIM/fail" > "$SIM/fail.new"; mv "$SIM/fail.new" "$SIM/fail"
        fi
        exit 1 ;;
    esac
  done < "$SIM/fail"
}
rc_of() { if [ -f "$SIM/$1" ]; then cat "$SIM/$1"; else echo "${2:-0}"; fi; }
"""

STUBS = {}

STUBS["systemctl"] = r"""
. "$STUB_LIB"
stub_begin systemctl "$@"
U="$SIM/units"
norm() { case "$1" in *.*) echo "$1" ;; *) echo "$1.service" ;; esac; }
state_of() {
  if [ -f "$U/$1.state" ]; then cat "$U/$1.state"
  elif [ -f "$U/$1.exists" ]; then echo disabled
  else echo not-found; fi
}
case "$1" in
  is-active)
    n=$(norm "$2")
    if [ -f "$U/$n.active" ]; then echo active; exit 0; fi
    echo inactive; exit 3 ;;
  is-enabled)
    n=$(norm "$2"); st=$(state_of "$n"); echo "$st"
    case "$st" in enabled|static|enabled-runtime|alias|indirect|generated) exit 0 ;; *) exit 1 ;; esac ;;
  list-unit-files)
    n=$(norm "$2")
    if [ -f "$U/$n.exists" ] || [ -f "$U/$n.state" ]; then
      printf 'UNIT FILE   STATE   PRESET\n%s disabled enabled\n\n1 unit files listed.\n' "$n"
    else
      printf 'UNIT FILE   STATE   PRESET\n\n0 unit files listed.\n'
    fi
    exit 0 ;;
  daemon-reload)
    # 単位ファイルが消えたら、その単位は systemd から見えなくなる。
    for n in campus-chime-harden-revert.timer campus-chime-harden-revert.service campus-chime-firewall.service; do
      [ -f "$CHIME_SYSROOT/etc/systemd/system/$n" ] || rm -f "$U/$n".exists "$U/$n".state "$U/$n".active
    done
    exit 0 ;;
  list-timers) exit 0 ;;
esac
verb="$1"; shift
now=0
for a in "$@"; do [ "$a" = "--now" ] && now=1; done
for a in "$@"; do
  case "$a" in -*) continue ;; esac
  n=$(norm "$a")
  touch "$U/$n.exists"
  case "$verb" in
    enable) echo enabled > "$U/$n.state"; [ "$now" = 1 ] && touch "$U/$n.active" ;;
    disable) echo disabled > "$U/$n.state"; [ "$now" = 1 ] && rm -f "$U/$n.active" ;;
    mask) echo masked > "$U/$n.state" ;;
    unmask) echo disabled > "$U/$n.state" ;;
    start) if [ -f "$U/$n.nostart" ]; then rm -f "$U/$n.active"; else touch "$U/$n.active"; fi ;;
    restart)
      case "$n" in campus-chime-harden-revert.timer)
        echo "snapshot watch=$([ -n "$WATCH_PATH" ] && [ -e "$WATCH_PATH" ] && echo 1 || echo 0)" >> "$CALLS_LOG" ;;
      esac
      if [ -f "$U/$n.nostart" ]; then rm -f "$U/$n.active"; else touch "$U/$n.active"; fi ;;
    stop) rm -f "$U/$n.active" ;;
    reload)
      if [ ! -f "$U/$n.active" ]; then echo "Failed to reload $n: Unit is not active" >&2; exit 1; fi ;;
  esac
done
exit 0
"""

STUBS["id"] = r"""
. "$STUB_LIB"
stub_begin id "$@"
echo "$PATH" > "$CALLS_LOG.path"
case "$1" in
  -u) echo "${FAKE_UID:-0}" ;;
  -un) echo "${FAKE_USER:-pi}" ;;
  -nG) if [ -f "$SIM/groups" ]; then cat "$SIM/groups"; else echo "$2 sudo audio"; fi ;;
  *) echo 0 ;;
esac
"""

STUBS["getent"] = r"""
. "$STUB_LIB"
stub_begin getent "$@"
[ "$1" = passwd ] || exit 2
if [ -n "$2" ]; then grep "^$2:" "$SIM/passwd" || exit 2; else cat "$SIM/passwd"; fi
"""

STUBS["runuser"] = r"""
. "$STUB_LIB"
stub_begin runuser "$@"
[ "$1" = -u ] || exit 1
shift 2
[ "$1" = -- ] && shift
case "$*" in
  *--wait-idle*) exit "$(rc_of wait_idle_rc)" ;;
  *--print-config*) cat "$SIM/print_config.json" 2>/dev/null; exit "$(rc_of print_config_rc)" ;;
esac
case "$1" in
  mkdir)
    case "$3" in
      "$TEST_TMP"/*) shift; exec mkdir "$@" ;;
      *) echo "stub: mkdir outside the test directory refused: $3" >&2; exit 1 ;;
    esac ;;
  sudo) shift; exec sudo "$@" ;;
esac
exit 0
"""

STUBS["sudo"] = r"""
. "$STUB_LIB"
stub_begin sudo "$@"
case "$*" in
  "-l -U "*) cat "$SIM/sudo_l.out" ;;
  "-k") exit 0 ;;
  "-n -v")
    if [ -f "$SIM/sudo_nv_rc" ]; then exit "$(cat "$SIM/sudo_nv_rc")"; fi
    for f in "$CHIME_SYSROOT"/etc/sudoers.d/*; do
      [ -f "$f" ] || continue
      case "${f##*/}" in *.*|*~) continue ;; esac
      if grep -q NOPASSWD "$f"; then exit 0; fi
    done
    echo "sudo: a password is required" >&2; exit 1 ;;
  "-v") exit "$(rc_of sudo_v_rc)" ;;
esac
exit 0
"""

STUBS["visudo"] = r"""
. "$STUB_LIB"
stub_begin visudo "$@"
exit "$(rc_of visudo_rc)"
"""

STUBS["passwd"] = r"""
. "$STUB_LIB"
stub_begin passwd "$@"
if [ -f "$SIM/passwd_S" ]; then cat "$SIM/passwd_S"; else echo "$2 P 2024-01-01 0 99999 7 -1"; fi
"""

STUBS["sshd"] = r"""
. "$STUB_LIB"
stub_begin sshd "$@"
case "$1" in
  -t)
    rc=$(rc_of sshd_t_rc)
    [ "$rc" = 0 ] || echo "/etc/ssh/sshd_config: line 1: Bad configuration option" >&2
    exit "$rc" ;;
  -T) ;;
  *) exit 0 ;;
esac
addr=$(echo "$*" | sed -n 's/.*addr=\([^ ,]*\).*/\1/p')
main="$CHIME_SYSROOT/etc/ssh/sshd_config"
[ -f "$main" ] || { echo "sshd: no config" >&2; exit 255; }
{
  [ -f "$SIM/sshd_force" ] && grep "^$addr " "$SIM/sshd_force" | cut -d' ' -f2-
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      Include\ *) for f in "$CHIME_SYSROOT"/etc/ssh/sshd_config.d/*.conf; do [ -f "$f" ] && cat "$f"; done ;;
      *) echo "$line" ;;
    esac
  done < "$main"
} | "$REAL_AWK" '
  { sub(/^[ \t]+/, "") }
  /^#/ || NF == 0 { next }
  tolower($1) == "match" { inmatch = 1 }
  inmatch { next }
  { key = tolower($1); if (!(key in seen)) { seen[key] = 1; val = $0; sub(/^[^ \t]+[ \t]+/, "", val); print key, val } }
  END {
    n = split("passwordauthentication yes|kbdinteractiveauthentication yes|permitrootlogin prohibit-password|pubkeyauthentication yes|strictmodes yes|authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2", d, "|")
    for (i = 1; i <= n; i++) { k = d[i]; sub(/ .*/, "", k); if (!(k in seen)) print d[i] }
  }'
"""

STUBS["ssh-keygen"] = r"""
. "$STUB_LIB"
stub_begin ssh-keygen "$@"
file="$3"
count=$(grep -cE '^(ssh-|ecdsa-|sk-)' "$file" 2>/dev/null)
if [ "${count:-0}" -eq 0 ]; then echo "$file is not a public key file." >&2; exit 255; fi
grep -E '^(ssh-|ecdsa-|sk-)' "$file" | while read -r type rest; do echo "256 SHA256:stub (${type})"; done
"""

STUBS["nft"] = r"""
. "$STUB_LIB"
stub_begin nft "$@"
N="$SIM/nft"; mkdir -p "$N"
case "$1" in
  -c)
    mkdir -p "$SIM/checks"
    if [ "$3" = "-" ]; then cat > "$SIM/checks/last_stdin"; else cp "$3" "$SIM/checks/last_file"; fi
    exit "$(rc_of nft_check_rc)" ;;
  -f)
    cp "$2" "$N/table.nft"; touch "$N/loaded"; echo "snapshot revert-armed=$([ -f "$SIM/units/campus-chime-harden-revert.timer.active" ] && echo 1 || echo 0)" >> "$CALLS_LOG"
    exit 0 ;;
  list)
    case "$2" in
      tables)
        [ -f "$N/loaded" ] && echo "table inet campus_chime_filter"
        [ -f "$N/other_tables" ] && cat "$N/other_tables"
        exit 0 ;;
      table)
        if [ "$3 $4" = "inet campus_chime_filter" ]; then
          if [ -f "$N/loaded" ]; then cat "$N/table.nft"; exit 0; fi
          echo "Error: No such file or directory" >&2; exit 1
        fi
        if [ -f "$N/other_$4" ]; then cat "$N/other_$4"; exit 0; fi
        echo "Error: No such file or directory" >&2; exit 1 ;;
    esac ;;
  delete)
    if [ -f "$N/sticky" ]; then exit 0; fi
    if [ -f "$N/loaded" ]; then rm -f "$N/loaded" "$N/table.nft"; exit 0; fi
    rm -f "$N/table.nft"
    echo "Error: No such file or directory" >&2; exit 1 ;;
esac
exit 0
"""

STUBS["ip"] = r"""
. "$STUB_LIB"
stub_begin ip "$@"
case "$1" in
  -4) cat "$SIM/ip4.out" 2>/dev/null ;;
  -6) cat "$SIM/ip6.out" 2>/dev/null ;;
esac
exit 0
"""

STUBS["ss"] = r"""
. "$STUB_LIB"
stub_begin ss "$@"
cat "$SIM/ss.out" 2>/dev/null
exit "$(rc_of ss_rc)"
"""

STUBS["journalctl"] = r"""
. "$STUB_LIB"
stub_begin journalctl "$@"
case "$*" in
  *"-u ssh.service"*) cat "$SIM/journal_ssh" 2>/dev/null ;;
  *"-u campus_chime.service"*) cat "$SIM/journal_chime" 2>/dev/null ;;
esac
exit 0
"""

STUBS["systemd-analyze"] = r"""
. "$STUB_LIB"
stub_begin systemd-analyze "$@"
case "$1" in
  verify)
    cat "$SIM/analyze_out" 2>/dev/null
    if [ -f "$SIM/analyze_rc_after" ] && [ -f "$CHIME_SYSROOT/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf" ]; then
      exit "$(cat "$SIM/analyze_rc_after")"
    fi
    exit "$(rc_of analyze_rc)" ;;
  security)
    if [ -f "$CHIME_SYSROOT/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf" ]; then
      echo "→ Overall exposure level for campus_chime.service: 4.2 OK"
    else
      echo "→ Overall exposure level for campus_chime.service: 8.7 EXPOSED"
    fi ;;
esac
exit 0
"""

STUBS["systemd-run"] = r"""
. "$STUB_LIB"
stub_begin systemd-run "$@"
exit "$(rc_of systemd_run_rc)"
"""

STUBS["apt-get"] = r"""
. "$STUB_LIB"
stub_begin apt-get "$@"
mkdir -p "$SIM/dpkg" "$SIM/units"
case "$*" in
  "-s install "*) cat "$SIM/apt_sim.out" 2>/dev/null; exit "$(rc_of apt_sim_rc)" ;;
  "install -y "*)
    shift 2
    for p in "$@"; do
      touch "$SIM/dpkg/$p"
      case "$p" in
        nftables) [ -f "$STUBS_DISABLED/nft" ] && mv "$STUBS_DISABLED/nft" "$STUBS_DIR/nft" ;;
        chrony)
          rm -f "$SIM/dpkg/systemd-timesyncd" "$SIM/units/systemd-timesyncd.service.active"
          touch "$SIM/units/chrony.service.exists"
          [ -f "$SIM/chrony_nostart" ] || touch "$SIM/units/chrony.service.active" ;;
        systemd-timesyncd) rm -f "$SIM/dpkg/chrony" "$SIM/units/chrony.service.active" ;;
      esac
    done ;;
  "purge -y "*) shift 2; for p in "$@"; do rm -f "$SIM/dpkg/$p"; done ;;
esac
exit 0
"""

STUBS["apt-cache"] = r"""
. "$STUB_LIB"
stub_begin apt-cache "$@"
cat "$SIM/apt_policy.out" 2>/dev/null
exit "$(rc_of apt_policy_rc)"
"""

STUBS["apt-config"] = r"""
. "$STUB_LIB"
stub_begin apt-config "$@"
cat "$CHIME_SYSROOT"/etc/apt/apt.conf.d/* 2>/dev/null
exit "$(rc_of apt_config_rc)"
"""

STUBS["dpkg-query"] = r"""
. "$STUB_LIB"
stub_begin dpkg-query "$@"
for last; do :; done
if [ -f "$SIM/dpkg/$last" ]; then printf 'install ok installed'; exit 0; fi
echo "dpkg-query: no packages found matching $last" >&2; exit 1
"""

STUBS["timedatectl"] = r"""
. "$STUB_LIB"
stub_begin timedatectl "$@"
if [ -f "$SIM/ntp_sync" ]; then cat "$SIM/ntp_sync"; else echo yes; fi
"""

for _name in ("systemd-tmpfiles", "chronyc"):
    STUBS[_name] = ". \"$STUB_LIB\"\nstub_begin " + _name + " \"$@\"\nexit 0\n"

STUBS["sleep"] = r"""
. "$STUB_LIB"
stub_begin sleep "$@"
exit 0
"""

#: 本物の stat を呼ぶが、持ち主（%u）は ``owners`` に書かれたパスを除き PI_UID と答える
#: （テストを動かす利用者によらず、持ち主の判定を決まった形にするため）。
STUBS["stat"] = r"""
follow=""
if [ "$1" = -L ]; then follow="-L"; shift; fi
fmt="$2"; path="$3"
uid="$PI_UID"
if [ -f "$SIM_DIR/owners" ]; then
  while read -r p u; do [ "$p" = "$path" ] && uid="$u"; done < "$SIM_DIR/owners"
fi
fmt="${fmt//%u/$uid}"
exec "$REAL_STAT" $follow -c "$fmt" "$path"
"""

#: 本物の chmod を呼ぶが、``CHMOD_FAIL_SUFFIX`` で終わるパスだけ失敗させる。
STUBS["chmod"] = r"""
last=""
for a; do last="$a"; done
if [ -n "$CHMOD_FAIL_SUFFIX" ]; then
  case "$last" in *"$CHMOD_FAIL_SUFFIX") echo "chmod: stub failure" >&2; exit 1 ;; esac
fi
exec "$REAL_CHMOD" "$@"
"""

#: flock は記録してから本物に渡す（30 分待つ指定は、テストでは 1 秒に縮める）。
STUBS["flock"] = r"""
. "$STUB_LIB"
stub_begin flock "$@"
if [ "$1 $2" = "-w 1800" ]; then shift 2; exec "$REAL_FLOCK" -w 1 "$@"; fi
exec "$REAL_FLOCK" "$@"
"""

REAL_FLOCK = shutil.which("flock")
REAL_STAT = shutil.which("stat")
REAL_CHMOD = shutil.which("chmod")
REAL_AWK = shutil.which("awk")

SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTestsOnly000000000000000000000 pi@pc\n"

PASSWD = """root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
pi:x:{uid}:{uid}:,,,:/home/pi:/bin/bash
"""

OS_RELEASE = """PRETTY_NAME="Raspbian GNU/Linux 12 (bookworm)"
NAME="Raspbian GNU/Linux"
VERSION_ID="12"
VERSION="12 (bookworm)"
VERSION_CODENAME={codename}
ID={id}
ID_LIKE=debian
"""

SSHD_CONFIG = """# This is the sshd server system-wide configuration file.
Include /etc/ssh/sshd_config.d/*.conf
KbdInteractiveAuthentication no
UsePAM yes
X11Forwarding yes
PrintMotd no
AcceptEnv LANG LC_*
Subsystem\tsftp\t/usr/lib/openssh/sftp-server
"""

SUDOERS = """# This file MUST be edited with the 'visudo' command as root.
Defaults\tenv_reset
root\tALL=(ALL:ALL) ALL
%sudo\tALL=(ALL:ALL) ALL
@includedir /etc/sudoers.d
"""

SUDO_L_OUT = """Matching Defaults entries for pi on campus-chime:
    env_reset, mail_badpass

User pi may run the following commands on campus-chime:
    (ALL : ALL) ALL
    (ALL) NOPASSWD: ALL
"""

APT_POLICY = """Package files:
 100 /var/lib/dpkg/status
     release a=now
 500 http://archive.raspberrypi.com/debian bookworm/main armhf Packages
     release o=Raspberry Pi Foundation,a=stable,n=bookworm,l=Raspberry Pi Foundation,c=main,b=armhf
     origin archive.raspberrypi.com
 500 http://raspbian.raspberrypi.com/raspbian bookworm/main armhf Packages
     release o=Raspbian,a=stable,n=bookworm,l=Raspbian,c=main,b=armhf
     origin raspbian.raspberrypi.com
 500 http://deb.debian.org/debian-security bookworm-security/main armhf Packages
     release v=12,o=Debian,a=stable-security,n=bookworm-security,l=Debian-Security,c=main,b=armhf
     origin deb.debian.org
 100 http://deb.debian.org/debian bookworm-backports/main armhf Packages
     release o=Debian Backports,a=stable-backports,n=bookworm-backports,l=Debian Backports,c=main,b=armhf
     origin deb.debian.org
Pinned packages:
"""

IP4_OUT = ("2: eth0    inet 192.168.1.23/24 brd 192.168.1.255 scope global dynamic noprefixroute eth0\\"
           "       valid_lft 86380sec preferred_lft 86380sec\n")
SS_OUT = "0      0      192.168.1.23:22   192.168.1.50:51234\n"
JOURNAL_SSH = "Accepted publickey for pi from 192.168.1.50 port 51234 ssh2: ED25519 SHA256:abcdefghijklmnop\n"
PRINT_CONFIG = {"state": {"file": "cache/state.json", "history_file": "cache/history.jsonl",
                          "pause_file": "cache/pause.json"},
                "tts": {"cache_dir": "cache/tts"},
                "time_signal": {"output_file": "assets/generated/time_signal.wav"}}

#: nft -c -f が使えるか（root で、検査モードの小さなルールが通る）。使えなければ、本物での検査は飛ばす。
def real_nft_checks() -> bool:
    nft = shutil.which("nft")
    if not nft or os.geteuid() != 0:
        return False
    try:
        result = subprocess.run([nft, "-c", "-f", "-"], input="table inet campus_check_probe\n",
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


REAL_NFT = shutil.which("nft") if real_nft_checks() else None


def slurp(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def header_block() -> str:
    """harden.sh 先頭のコメントブロック（``--help`` が表示するはずの範囲）。"""
    block = []
    with open(HARDEN_SH, "r", encoding="utf-8") as handle:
        for line in handle.read().splitlines()[1:]:
            if not line.startswith("#"):
                break
            block.append(line)
    return "".join(line + "\n" for line in block)


def snapshot(root, exclude=(), times=False):
    """``root`` 以下の全ファイル・ディレクトリの、種類・mode・内容・（任意で）更新時刻。"""
    result = {}
    for directory, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root)
            if any(rel == skipped or rel.startswith(skipped + os.sep) for skipped in exclude):
                continue
            info = os.lstat(path)
            if os.path.islink(path):
                entry = ("link", os.readlink(path))
            elif os.path.isdir(path):
                entry = ("dir", info.st_mode & 0o7777)
            else:
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
                entry = ("file", info.st_mode & 0o7777, digest)
            if times:
                entry += (info.st_mtime_ns,)
            result[rel] = entry
    return result


class HardenCase(unittest.TestCase):
    """使い捨ての sysroot・proc・偽物のコマンドを作り、harden.sh を本物のまま動かす。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.root = os.path.join(self.tmp, "root")
        self.proc = os.path.join(self.tmp, "proc")
        self.repo = os.path.join(self.tmp, "repo")
        self.sim = os.path.join(self.tmp, "sim")
        self.stubs = os.path.join(self.tmp, "stubs")
        self.stubs_disabled = os.path.join(self.tmp, "stubs-disabled")
        self.tools = os.path.join(self.tmp, "tools")
        self.calls_log = os.path.join(self.tmp, "calls.log")
        for path in (self.root, self.proc, self.repo, self.sim, self.stubs, self.stubs_disabled, self.tools,
                     os.path.join(self.repo, "scripts")):
            os.makedirs(path)
        self.script = os.path.join(self.repo, "scripts", "harden.sh")
        shutil.copy(HARDEN_SH, self.script)
        self.state = os.path.join(self.root, "var", "lib", "campus-chime-harden")
        self.make_tools()
        self.make_stubs()
        self.build_system()
        self.addCleanup(self.unlock)
        self.lock_handle = None

    # -- 準備 -----------------------------------------------------------
    def make_tools(self):
        for name in TOOLS:
            found = shutil.which(name)
            if found:
                os.symlink(found, os.path.join(self.tools, name))
        os.symlink(sys.executable, os.path.join(self.tools, "python3"))

    def make_stubs(self):
        self.write_file(os.path.join(self.sim, "stub_lib.sh"), STUB_LIB, 0o644)
        os.makedirs(os.path.join(self.sim, "units"))
        for name, body in STUBS.items():
            shebang = "#!/bin/bash\n" if name == "stat" else "#!/bin/sh\n"
            self.write_file(os.path.join(self.stubs, name), shebang + body, 0o755)
        self.path = self.stubs + os.pathsep + self.tools

    def write_file(self, path, text, mode=0o644):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.lexists(path):
            os.remove(path)  # 読み取り専用（0440 など）の既存ファイルも、普通の利用者が置き換えられるように
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, mode)
        return path

    def sys_path(self, path):
        """sysroot の中の実パス（``path`` は ``/etc/...`` の形）。"""
        return self.root + path

    def put(self, path, text, mode=0o644):
        return self.write_file(self.sys_path(path), text, mode)

    def mkdir(self, path, mode=0o755):
        full = self.sys_path(path)
        os.makedirs(full, exist_ok=True)
        os.chmod(full, mode)

    def read(self, path):
        with open(self.sys_path(path), "r", encoding="utf-8") as handle:
            return handle.read()

    def exists(self, path):
        return os.path.lexists(self.sys_path(path))

    def mode(self, path):
        return os.stat(self.sys_path(path)).st_mode & 0o7777

    def sim_put(self, name, text):
        self.write_file(os.path.join(self.sim, name), text)

    def unit(self, name, state="enabled", active=True):
        """偽の systemd の単位を用意する。"""
        units = os.path.join(self.sim, "units")
        os.makedirs(units, exist_ok=True)
        self.write_file(os.path.join(units, name + ".exists"), "")
        self.write_file(os.path.join(units, name + ".state"), state + "\n")
        active_file = os.path.join(units, name + ".active")
        if active:
            self.write_file(active_file, "")
        elif os.path.exists(active_file):
            os.remove(active_file)

    def unit_state(self, name):
        units = os.path.join(self.sim, "units")
        path = os.path.join(units, name + ".state")
        state = slurp(path).strip() if os.path.exists(path) else "none"
        return state, os.path.exists(os.path.join(units, name + ".active"))

    def package(self, name, installed=True):
        path = os.path.join(self.sim, "dpkg", name)
        if installed:
            self.write_file(path, "")
        elif os.path.exists(path):
            os.remove(path)

    def build_system(self):
        """ふつうの Pi: bookworm、鍵のある pi、NOPASSWD の sudo、動いている ssh と放送サービス。"""
        for directory, mode in (("/", 0o755), ("/etc", 0o755), ("/etc/ssh", 0o755), ("/etc/ssh/sshd_config.d", 0o755),
                                ("/etc/sudoers.d", 0o750), ("/etc/systemd", 0o755), ("/etc/systemd/system", 0o755),
                                ("/etc/apt", 0o755), ("/etc/apt/apt.conf.d", 0o755), ("/home", 0o755),
                                ("/home/pi", 0o755), ("/home/pi/.ssh", 0o700), ("/var", 0o755), ("/var/lib", 0o755),
                                ("/var/log", 0o755)):
            self.mkdir(directory, mode)
        self.put("/etc/os-release", OS_RELEASE.format(codename="bookworm", id="debian"))
        self.put("/etc/ssh/sshd_config", SSHD_CONFIG)
        self.put("/home/pi/.ssh/authorized_keys", SSH_KEY, 0o600)
        self.put("/etc/sudoers", SUDOERS, 0o440)
        self.put("/etc/sudoers.d/010_pi-nopasswd", "pi ALL=(ALL) NOPASSWD: ALL\n", 0o440)
        self.put("/etc/sudoers.d/README", "# sudoers.d の説明\n", 0o440)
        with open(UNIT_FILE, "r", encoding="utf-8") as handle:
            self.put("/etc/systemd/system/campus_chime.service", handle.read())
        self.sim_put("passwd", PASSWD.format(uid=PI_UID))
        self.sim_put("sudo_l.out", SUDO_L_OUT)
        self.sim_put("ip4.out", IP4_OUT)
        self.sim_put("ss.out", SS_OUT)
        self.sim_put("journal_ssh", JOURNAL_SSH)
        self.sim_put("apt_policy.out", APT_POLICY)
        self.sim_put("apt_sim.out", "Inst chrony (4.3-2+deb12u1 Debian:12.5/stable [armhf])\n"
                                    "Remv systemd-timesyncd [252.22-1~deb12u1]\n")
        self.sim_put("print_config.json", json.dumps(PRINT_CONFIG))
        # 偽物の campus_chime.py: --print-config だけ本物と同じ形の JSON を返す（利用者本人が直接動かす場合用）。
        self.write_file(os.path.join(self.repo, "campus_chime.py"),
                        "import json, sys\nif '--print-config' in sys.argv:\n    print(json.dumps({0}))\n".format(
                            json.dumps(PRINT_CONFIG)))
        for name, state, active in (("ssh.service", "enabled", True), ("campus_chime.service", "enabled", True),
                                    ("systemd-journald.service", "static", True), ("apt-daily.timer", "enabled", True),
                                    ("apt-daily-upgrade.timer", "enabled", True),
                                    ("bluetooth.service", "enabled", True), ("hciuart.service", "enabled", False),
                                    ("ModemManager.service", "enabled", True), ("avahi-daemon.service", "enabled", True),
                                    ("systemd-timesyncd.service", "enabled", True)):
            self.unit(name, state, active)
        for name in ("openssh-server", "systemd-timesyncd"):
            self.package(name)
        self.proc_boot_id("11111111-1111-1111-1111-111111111111")

    def proc_boot_id(self, boot_id):
        self.write_file(os.path.join(self.proc, "sys", "kernel", "random", "boot_id"), boot_id + "\n")

    def fail_on(self, call, once=False):
        """偽物のコマンド ``call``（前方一致）を失敗させる。"""
        with open(os.path.join(self.sim, "fail"), "a", encoding="utf-8") as handle:
            handle.write(("once:" if once else "") + call + "\n")

    def hide_stub(self, name):
        """偽物 ``name`` を PATH から外す（そのコマンドが入っていない状況）。"""
        shutil.move(os.path.join(self.stubs, name), os.path.join(self.stubs_disabled, name))

    def owner(self, path, uid):
        """sysroot の ``path`` の持ち主が ``uid`` に見えるようにする。"""
        with open(os.path.join(self.sim, "owners"), "a", encoding="utf-8") as handle:
            handle.write("{0} {1}\n".format(self.sys_path(path), uid))

    # -- 実行 -----------------------------------------------------------
    def environment(self, **extra):
        env = {"PATH": self.path, "CHIME_SYSROOT": self.root, "CHIME_PROC": self.proc,
               "CALLS_LOG": self.calls_log, "SIM_DIR": self.sim, "STUB_LIB": os.path.join(self.sim, "stub_lib.sh"),
               "STUBS_DIR": self.stubs, "STUBS_DISABLED": self.stubs_disabled, "PI_UID": str(PI_UID),
               "TEST_TMP": self.tmp,
               "REAL_STAT": REAL_STAT, "REAL_FLOCK": REAL_FLOCK, "REAL_CHMOD": REAL_CHMOD, "REAL_AWK": REAL_AWK,
               "HOME": self.tmp, "LC_ALL": "C.UTF-8", "FAKE_UID": "0"}
        env.update(extra)
        return env

    def run_script(self, *args, stdin=None, timeout=90, **extra):
        return subprocess.run(["bash", self.script] + list(args), capture_output=True, text=True,
                              encoding="utf-8", env=self.environment(**extra), cwd=self.tmp, timeout=timeout,
                              stdin=subprocess.DEVNULL if stdin is None else stdin)

    def run_and_crash(self, call, *args, **extra):
        """``call``（偽物のコマンドの呼び出しの前方一致）に着いた瞬間に、スクリプトが kill -9 で死ぬ状況。"""
        self.fail_on("crash:" + call)
        wrapper = self.write_file(os.path.join(self.tmp, "pidwrapper.sh"),
                                  "#!/bin/bash\necho $$ > \"$SIM_DIR/script_pid\"\nexec bash \"$@\"\n", 0o755)
        result = subprocess.run(["bash", wrapper, self.script] + list(args), capture_output=True, text=True,
                                encoding="utf-8", env=self.environment(**extra), cwd=self.tmp, timeout=90,
                                stdin=subprocess.DEVNULL)
        os.remove(os.path.join(self.sim, "fail"))
        return result

    def run_tty(self, *args, **extra):
        """標準入力が端末のまま動かす（sudo の手順は端末を要求する）。"""
        master, slave = pty.openpty()
        try:
            return self.run_script(*args, stdin=slave, **extra)
        finally:
            os.close(slave)
            os.close(master)

    def fake_session(self, session_start):
        """偽の /proc に、プロセスの親子を作る。``session_start``（エポック秒）に SSH で入った画面の子孫なら、
        sshd の接続ごとの子（PID 5000）→ 特権側（4000）→ 待ち受け（900、親は PID 1）の連なり。
        ``None`` なら、sshd の下にいない画面（コンソールなど）。戻り値は、動かす側の親の PID。"""
        ticks = os.sysconf("SC_CLK_TCK")
        self.write_file(os.path.join(self.proc, "stat"), "cpu  1 2 3 4\nbtime {0}\n".format(BTIME))

        def entry(pid, name, ppid, started=0):
            self.write_file(os.path.join(self.proc, str(pid), "status"),
                            "Name:\t{0}\nState:\tS (sleeping)\nPid:\t{1}\nPPid:\t{2}\n".format(name, pid, ppid))
            self.write_file(os.path.join(self.proc, str(pid), "stat"),
                            "{0} ({1}) S {2} 1 1 0 -1 4194560 100 0 0 0 1 1 0 0 20 0 1 0 {3} 123456 789\n".format(
                                pid, name, ppid, started))

        if session_start is None:
            entry(6000, "login", 1)
            return 6000
        entry(900, "sshd", 1, 100)
        entry(4000, "sshd", 900, (session_start - BTIME) * ticks)
        entry(5000, "sshd", 4000, (session_start - BTIME) * ticks)
        return 5000

    def run_confirm(self, *args, session_start=None, **extra):
        """``--confirm`` を、``session_start`` に SSH で入った画面の下から動かす（None はコンソール）。"""
        parent = self.fake_session(session_start)
        wrapper = self.write_file(os.path.join(self.tmp, "wrapper.sh"),
                                  "#!/bin/bash\nmkdir -p \"$CHIME_PROC/$$\"\n"
                                  "printf 'Name:\\tbash\\nPPid:\\t%s\\n' \"$PARENT_PID\" > \"$CHIME_PROC/$$/status\"\n"
                                  "exec bash \"$@\"\n", 0o755)
        return subprocess.run(["bash", wrapper, self.script, "--confirm"] + list(args), capture_output=True,
                              text=True, encoding="utf-8", cwd=self.tmp, timeout=90, stdin=subprocess.DEVNULL,
                              env=self.environment(PARENT_PID=str(parent), **extra))

    def pending_epoch(self):
        """確定待ちの手順の、適用した時刻のうち最も新しいもの。"""
        epochs = [int(self.meta(os.path.join(self.state, "pending"), step) or 0) for step in self.pending()]
        return max(epochs)

    def calls(self):
        if not os.path.exists(self.calls_log):
            return []
        with open(self.calls_log, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()

    def calls_with(self, prefix):
        return [call for call in self.calls() if call.startswith(prefix)]

    def forget_calls(self):
        if os.path.exists(self.calls_log):
            os.remove(self.calls_log)

    def snap(self, times=False):
        """sysroot（状態の置き場を除く）と偽物の状態の、いまの姿。"""
        return (snapshot(self.root, exclude=("var/lib/campus-chime-harden",), times=times),
                snapshot(os.path.join(self.sim, "units")) if os.path.isdir(os.path.join(self.sim, "units")) else {},
                snapshot(os.path.join(self.sim, "nft")) if os.path.isdir(os.path.join(self.sim, "nft")) else {},
                snapshot(os.path.join(self.sim, "dpkg")) if os.path.isdir(os.path.join(self.sim, "dpkg")) else {})

    #: 変更を伴う呼び出し（これが記録に無いことで「何も変えていない」を確かめる）。
    MUTATING = ("systemctl enable", "systemctl disable", "systemctl mask", "systemctl unmask", "systemctl start",
                "systemctl stop", "systemctl restart", "systemctl reload", "systemctl daemon-reload", "nft -f",
                "nft delete", "apt-get install", "apt-get purge", "systemd-run", "systemd-tmpfiles", "chronyc reload",
                "journalctl --flush", "journalctl --relinquish-var", "runuser -u pi -- mkdir")

    def mutating_calls(self):
        return [call for call in self.calls() if call.startswith(self.MUTATING)]

    def assert_nothing_changed(self, before, result=None, code=1):
        """事前の確認で止まったとき: 終了コード、「何も変更していません」、ファイルと偽物の状態、変更を伴う呼び出し。"""
        if result is not None:
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
            self.assertIn("何も変更していません", result.stderr)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.mutating_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "pending")))

    def state_file(self, name):
        with open(os.path.join(self.state, name), "r", encoding="utf-8") as handle:
            return handle.read()

    def state_exists(self, name):
        return os.path.exists(os.path.join(self.state, name))

    def meta(self, path, key):
        """状態の置き場の key=value ファイルから、key の値（無ければ None）。"""
        value = None
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle.read().splitlines():
                if line.startswith(key + "="):
                    value = line.split("=", 1)[1]
        return value

    def pending(self):
        path = os.path.join(self.state, "pending")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        return [line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("window=")]

    def applied_meta(self, step, key):
        return self.meta(os.path.join(self.state, "applied", step), key)

    def hold_lock(self):
        """別の harden.sh が実行中、という状況（状態の置き場の lock を握る）。"""
        os.makedirs(self.state, exist_ok=True)
        self.lock_handle = open(os.path.join(self.state, "lock"), "w")
        fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(self):
        if getattr(self, "lock_handle", None):
            self.lock_handle.close()
            self.lock_handle = None

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result


class HelpAndUsageTest(HardenCase):
    def test_help_prints_exactly_the_header_block(self):
        expected = header_block()
        self.assertIn("scripts/harden.sh", expected)
        self.assertNotIn("set -euo pipefail", expected)
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_script("-h").stdout, expected)

    def test_help_works_without_root_and_before_anything_is_read(self):
        result = self.run_script("--help", FAKE_UID="1000")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.state))

    def test_an_unknown_option_or_step_exits_with_2_before_anything_runs(self):
        for args, message in ((("--bogus",), "不明なオプション: --bogus"), (("ssh", "-x"), "不明なオプション: -x"),
                              (("nothing",), "不明な手順: nothing")):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.state))

    def test_revert_after_must_be_an_integer_from_1_to_60(self):
        for value in ("0", "61", "abc", "1x", "", "-5", "100", "99999999999999999999", "1.5", " 5"):
            with self.subTest(value=value):
                result = self.run_script("--revert-after", value, "--dry-run", "services")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("--revert-after", result.stderr)
                self.assertEqual(self.calls(), [])
        for value in ("1", "05", "60"):
            with self.subTest(value=value):
                self.assertEqual(self.run_script("--revert-after", value, "--dry-run", "journal").returncode, 0)

    def test_user_root_and_odd_user_names_are_refused(self):
        for value in ("root", "Pi", "pi;ls", "-x", "a b"):
            with self.subTest(value=value):
                result = self.run_script("--user", value, "--dry-run", "ssh")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("--user", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_options_that_need_a_value_or_a_matching_step(self):
        cases = (("--user",), ("--revert-after",), ("--ssh-from",), ("--ntp-server",),
                 ("--confirm", "ssh"), ("--rollback-pending", "ssh"), ("--confirm", "--rollback"),
                 ("--dry-run",), ("--dry-run", "--confirm"), ("--dry-run", "--rollback", "ssh"),
                 ("--ssh-from", "10.0.0.0/8", "ssh"), ("--ntp-server", "ntp.example", "ssh"),
                 ("--test-sound", "ssh"), ("--test-sound", "--confirm"), ("--test-sound", "--rollback"))
        for args in cases:
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.calls(), [])

    def test_the_long_options_are_written_the_way_the_docs_test_reads_them(self):
        """tests/test_docs.py の shell_options() と同じ正規表現で、case の選択肢を読む。"""
        with open(HARDEN_SH, "r", encoding="utf-8") as handle:
            text = handle.read()
        options = set(re.findall(r"^\s+(?:-[a-z]\|)?(--[a-z][a-z-]*)\)", text, re.MULTILINE))
        self.assertEqual(options, {"--help", "--confirm", "--rollback", "--rollback-pending", "--dry-run", "--force",
                                   "--test-sound", "--user", "--revert-after", "--ssh-from", "--ntp-server"})

    def test_the_script_is_plain_bash_with_a_set_e_header_and_no_hardcoded_system_paths_in_commands(self):
        with open(HARDEN_SH, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(lines[0], "#!/usr/bin/env bash")
        self.assertIn("set -euo pipefail", lines)
        # 外のコマンドは名前だけで呼ぶ（テストで差し替えるため）。ユニットの中身に書く絶対パスは別。
        for number, line in enumerate(lines, 1):
            code = line.split("#", 1)[0]
            self.assertNotRegex(code, r"(^|[;&|(]\s*|\bsudo\s+|\bexec\s+)/(usr/)?(s?bin)/\w+",
                                "{0}: {1}".format(number, line))


class RootUserAndOsTest(HardenCase):
    def test_it_refuses_to_run_without_root_and_changes_nothing(self):
        before = self.snap(times=True)
        for args in ((), ("ssh",), ("--confirm",), ("--rollback",), ("--rollback", "ssh"), ("--rollback-pending",),
                     ("--test-sound",)):
            with self.subTest(args=args):
                result = self.run_script(*args, FAKE_UID="1000")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("sudo を付けて実行してください", result.stderr)
                self.assertIn("何も変更していません", result.stderr)
        self.assertEqual(self.snap(times=True), before)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.mutating_calls(), [])

    def test_dry_run_works_without_root_and_says_what_it_could_not_read(self):
        before = self.snap(times=True)
        result = self.run_script("--dry-run", "ssh", "firewall", "sudo", FAKE_UID="1000")
        self.assert_ok(result)
        self.assertIn("root ではない", result.stdout)
        self.assertIn("PasswordAuthentication no", result.stdout)
        self.assertEqual(self.snap(times=True), before)
        self.assertEqual(self.mutating_calls(), [])
        self.assertFalse(os.path.exists(self.state))

    def test_a_missing_user_stops_before_anything_changes(self):
        before = self.snap()
        result = self.run_script("--user", "nobody", "services")
        self.assert_nothing_changed(before, result)
        self.assertIn("ユーザー nobody がありません", result.stderr)

    def test_the_os_must_be_debian_or_raspbian_bookworm_or_trixie(self):
        for os_id, like, codename, accepted in (("debian", "", "bookworm", True), ("debian", "", "trixie", True),
                                                ("raspbian", "debian", "bookworm", True),
                                                ("debian", "", "bullseye", False), ("ubuntu", "debian", "noble", False),
                                                ("arch", "", "", False)):
            with self.subTest(os_id=os_id, codename=codename):
                self.put("/etc/os-release", "ID={0}\nID_LIKE={1}\nVERSION_CODENAME={2}\n".format(os_id, like, codename))
                result = self.run_script("--dry-run", "journal")
                if accepted:
                    self.assert_ok(result)
                else:
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn("Debian", result.stderr)
                    self.assertIn("--force", result.stderr)
                    forced = self.run_script("--dry-run", "--force", "journal")
                    self.assert_ok(forced)
                    self.assertIn("--force が付いている", forced.stderr)

    def test_a_missing_os_release_is_refused_too(self):
        os.remove(self.sys_path("/etc/os-release"))
        result = self.run_script("--dry-run", "journal")
        self.assertEqual(result.returncode, 1)
        self.assertIn("不明", result.stderr)

    def test_the_os_gate_does_not_block_getting_out(self):
        """元に戻す・確定するは、OS の確認なしで通る（締め出されたあとでも戻せるように）。"""
        self.assert_ok(self.run_script("services"))
        self.put("/etc/os-release", "ID=arch\nVERSION_CODENAME=rolling\n")
        self.assert_ok(self.run_script("--rollback", "services"))

    def test_path_gets_the_sbin_directories_appended_only_outside_test_mode(self):
        """P-04: 本番（CHIME_SYSROOT が空）のときだけ、PATH の末尾に足す。テストでは足さない。"""
        with open(HARDEN_SH, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertRegex(text, r'if \[ -z "\$\{SYSROOT\}" \]; then\n(?:  #[^\n]*\n)*  PATH="\$\{PATH\}:/usr/local/sbin:/usr/sbin:/sbin"\nfi')
        self.assertNotRegex(text, r'PATH="/usr/(local/)?s?bin:\$\{?PATH')
        self.run_script("--dry-run", "journal")
        with open(self.calls_log + ".path", "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), self.path)

    def test_a_second_run_at_the_same_time_is_refused(self):
        self.hold_lock()
        before = self.snap()
        for args in (("services",), ("--rollback", "services"), ("--confirm",)):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("実行中", result.stderr)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.mutating_calls(), [])


SSH_DROPIN = "/etc/ssh/sshd_config.d/01-campus-chime-harden.conf"
REVERT_SERVICE = "/etc/systemd/system/campus-chime-harden-revert.service"
REVERT_TIMER = "/etc/systemd/system/campus-chime-harden-revert.timer"
REVERT_UNITS = "campus-chime-harden-revert.timer"


def expected_ssh_dropin(allow_user="pi"):
    lines = ["# scripts/harden.sh ssh が作成（元に戻す: sudo bash scripts/harden.sh --rollback ssh）",
             "PasswordAuthentication no", "KbdInteractiveAuthentication no", "PermitRootLogin no",
             "PubkeyAuthentication yes", "AuthenticationMethods publickey", "X11Forwarding no"]
    if allow_user:
        lines.append("AllowUsers " + allow_user)
    return "\n".join(lines) + "\n"


class SshStepTest(HardenCase):
    def test_it_writes_the_key_only_dropin_and_waits_for_confirmation(self):
        result = self.assert_ok(self.run_script("ssh"))
        self.assertEqual(self.read(SSH_DROPIN), expected_ssh_dropin())
        self.assertEqual(self.mode(SSH_DROPIN), 0o644)
        self.assertNotIn("MaxAuthTries", self.read(SSH_DROPIN))
        self.assertEqual(self.pending(), ["ssh"])
        self.assertEqual(self.applied_meta("ssh", "done"), "1")
        self.assertTrue(self.exists(REVERT_SERVICE) and self.exists(REVERT_TIMER))
        calls = self.calls()
        self.assertIn("sshd -t", calls)
        for address in ("203.0.113.1", "10.0.0.1", "192.168.1.1", "172.16.0.1"):
            self.assertIn("sshd -T -C user=pi,host=chime-check,addr=" + address, calls)
        self.assertIn("systemctl reload ssh", calls)
        self.assertIn("sudo bash scripts/harden.sh --confirm", result.stdout)
        self.assertIn("Permission denied (publickey)", result.stdout)
        self.assertIn("ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password pi@campus-chime.local",
                      result.stdout)

    def test_the_timer_is_armed_before_anything_takes_effect(self):
        """LS-02: 時計を張ってから設定を書く。途中で止まっても、何も無いか時計が動いているかのどちらか。"""
        self.assert_ok(self.run_script("ssh", WATCH_PATH=self.sys_path(SSH_DROPIN)))
        calls = self.calls()
        armed = calls.index("systemctl restart " + REVERT_UNITS)
        self.assertEqual(calls[armed + 1], "snapshot watch=0")
        self.assertLess(armed, calls.index("sshd -t"))
        self.assertLess(armed, calls.index("systemctl reload ssh"))

    def test_when_the_timer_cannot_be_armed_nothing_is_left_behind(self):
        before = self.snap()
        self.fail_on("systemctl restart " + REVERT_UNITS)
        result = self.run_script("ssh")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(self.exists(SSH_DROPIN))
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.pending(), [])
        self.assertIn("元に戻しました", result.stderr + result.stdout)

    def test_a_crash_after_the_dropin_is_written_still_leaves_an_armed_timer(self):
        """LS-02: どこで止まっても、「何も適用されていない」か「時計が動いている」のどちらか。"""
        result = self.run_and_crash("systemctl reload ssh", "ssh")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.exists(SSH_DROPIN))
        self.assertEqual(self.pending(), ["ssh"])
        self.assertEqual(self.unit_state(REVERT_UNITS), ("enabled", True))
        self.assertTrue(self.exists(REVERT_TIMER) and self.exists(REVERT_SERVICE))
        self.assertTrue(self.state_exists("harden.sh"))
        self.assertIsNone(self.applied_meta("ssh", "done"))
        # 時計が呼ぶ処理（状態の置き場の写し）が、元に戻す。
        copy = os.path.join(self.state, "harden.sh")
        rollback = subprocess.run(["bash", copy, "--rollback-pending"], capture_output=True, text=True,
                                  encoding="utf-8", env=self.environment(), cwd=self.tmp, stdin=subprocess.DEVNULL)
        self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
        self.assertFalse(self.exists(SSH_DROPIN))
        self.assertEqual(self.pending(), [])

    def test_a_crash_before_the_timer_is_armed_leaves_the_configuration_untouched(self):
        for call in ("systemctl daemon-reload", "systemctl enable " + REVERT_UNITS):
            with self.subTest(call=call):
                self.setUp()
                result = self.run_and_crash(call, "ssh")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.exists(SSH_DROPIN))
                # 続きは、もう一度実行すれば最初から進む（適用済みとは言わない）。
                again = self.assert_ok(self.run_script("ssh"))
                self.assertNotIn("すでに適用済みです", again.stdout)
                self.assertTrue(self.exists(SSH_DROPIN))

    def test_a_crash_while_checking_the_new_config_leaves_an_armed_timer(self):
        result = self.run_and_crash("sshd -t", "ssh")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.exists(SSH_DROPIN))
        self.assertEqual(self.pending(), ["ssh"])
        self.assertEqual(self.unit_state(REVERT_UNITS), ("enabled", True))

    def test_every_reason_for_refusing_is_printed_and_nothing_changes(self):
        os.remove(self.sys_path("/home/pi/.ssh/authorized_keys"))
        os.chmod(self.sys_path("/home/pi/.ssh"), 0o755)
        self.put("/etc/ssh/sshd_config", "PasswordAuthentication yes\n")
        before = self.snap()
        result = self.run_script("ssh")
        self.assert_nothing_changed(before, result)
        self.assertIn("使える公開鍵がありません", result.stderr)
        self.assertIn("Include がありません", result.stderr)
        self.assertIn("ssh-copy-id pi@campus-chime.local", result.stderr)
        self.assertIn("ssh-keygen -t ed25519", result.stderr)
        self.assertIn("type $env:USERPROFILE\\.ssh\\id_ed25519.pub", result.stderr)

    def test_a_key_file_without_a_usable_key_is_refused(self):
        self.put("/home/pi/.ssh/authorized_keys", "# コメントだけ\n", 0o600)
        before = self.snap()
        result = self.run_script("ssh")
        self.assert_nothing_changed(before, result)
        self.assertIn("使える鍵がありません", result.stderr)

    def test_unsafe_permissions_that_sshd_would_reject_are_refused(self):
        cases = (("鍵のファイルがグループに書ける", lambda: os.chmod(self.sys_path("/home/pi/.ssh/authorized_keys"), 0o664),
                  "グループ・その他に書き込める"),
                 (".ssh が 755", lambda: os.chmod(self.sys_path("/home/pi/.ssh"), 0o755), "mode が 700 より緩い"),
                 ("ホームが誰でも書ける", lambda: os.chmod(self.sys_path("/home/pi"), 0o777), "/home/pi がグループ・その他"),
                 ("/home がグループに書ける", lambda: os.chmod(self.sys_path("/home"), 0o775), "/home がグループ・その他"),
                 ("鍵の持ち主が他人", lambda: self.owner("/home/pi/.ssh/authorized_keys", OTHER_UID),
                  "持ち主が root でも pi でもありません"),
                 (".ssh の持ち主が root", lambda: self.owner("/home/pi/.ssh", 0), "持ち主が pi ではありません"))
        for name, damage, message in cases:
            with self.subTest(name):
                self.setUp()
                damage()
                before = self.snap()
                result = self.run_script("ssh")
                self.assert_nothing_changed(before, result)
                self.assertIn(message, result.stderr)

    def test_strictmodes_no_skips_the_directory_walk(self):
        self.put("/etc/ssh/sshd_config", SSHD_CONFIG + "StrictModes no\n")
        os.chmod(self.sys_path("/home"), 0o777)
        self.assert_ok(self.run_script("ssh"))

    def test_a_symlinked_home_is_judged_by_its_target(self):
        """sshd は realpath を見る。/home がリンクでも、行き先が安全なら通す（リンク自身の mode は 777）。"""
        self.mkdir("/data", 0o755)
        os.rename(self.sys_path("/home"), self.sys_path("/data/home"))
        os.symlink("data/home", self.sys_path("/home"))
        self.assertEqual(os.lstat(self.sys_path("/home")).st_mode & 0o777, 0o777)
        self.assert_ok(self.run_script("ssh"))

    def test_the_key_files_sshd_will_really_read_are_the_ones_checked(self):
        """LS-07: AuthorizedKeysFile を sshd -T から読む（%h・%u・%%）。既定の場所に無くても、そちらにあれば通る。"""
        os.remove(self.sys_path("/home/pi/.ssh/authorized_keys"))
        self.put("/etc/ssh/sshd_config", SSHD_CONFIG + "AuthorizedKeysFile .ssh/chime_keys %h/keys/%u\n")
        before = self.snap()
        result = self.run_script("ssh")
        self.assert_nothing_changed(before, result)
        self.assertIn("使える公開鍵がありません", result.stderr)
        self.put("/home/pi/keys/pi", SSH_KEY, 0o600)
        self.mkdir("/home/pi/keys", 0o755)
        self.assert_ok(self.run_script("ssh"))

    def test_a_key_in_an_unsafe_directory_is_not_counted(self):
        os.remove(self.sys_path("/home/pi/.ssh/authorized_keys"))
        self.put("/etc/ssh/sshd_config", SSHD_CONFIG + "AuthorizedKeysFile /srv/keys/%u\n")
        self.mkdir("/srv", 0o755)
        self.mkdir("/srv/keys", 0o777)
        self.put("/srv/keys/pi", SSH_KEY, 0o600)
        before = self.snap()
        result = self.run_script("ssh")
        self.assert_nothing_changed(before, result)
        self.assertIn("/srv/keys がグループ・その他", result.stderr)

    def test_other_login_accounts_without_keys_block_the_step_unless_forced(self):
        """LS-08: 鍵の無い別のアカウントは、パスワードで入れなくなる。AllowUsers は書かない。"""
        with open(os.path.join(self.sim, "passwd"), "a", encoding="utf-8") as handle:
            handle.write("alice:x:1001:1001::/home/alice:/bin/bash\nsvc:x:999:999::/var/svc:/bin/bash\n"
                         "bob:x:1002:1002::/home/bob:/usr/sbin/nologin\n")
        before = self.snap()
        result = self.run_script("ssh")
        self.assert_nothing_changed(before, result)
        self.assertIn("alice", result.stderr)
        self.assertNotIn("bob", result.stderr)
        self.assertNotIn("svc", result.stderr)
        self.assertIn("--force", result.stderr)
        forced = self.assert_ok(self.run_script("ssh", "--force"))
        self.assertIn("alice", forced.stderr)
        self.assertEqual(self.read(SSH_DROPIN), expected_ssh_dropin(allow_user=None))

    def test_other_login_accounts_with_keys_are_fine_but_allowusers_is_left_out(self):
        with open(os.path.join(self.sim, "passwd"), "a", encoding="utf-8") as handle:
            handle.write("alice:x:1001:1001::/home/alice:/bin/bash\n")
        self.mkdir("/home/alice/.ssh", 0o700)
        self.put("/home/alice/.ssh/authorized_keys", SSH_KEY, 0o600)
        result = self.assert_ok(self.run_script("ssh"))
        self.assertEqual(self.read(SSH_DROPIN), expected_ssh_dropin(allow_user=None))
        self.assertIn("AllowUsers は書きません", result.stdout)

    def test_a_missing_sshd_is_refused(self):
        self.hide_stub("sshd")
        before = self.snap()
        result = self.run_script("ssh")
        self.assert_nothing_changed(before, result)
        self.assertIn("sshd が見つかりません", result.stderr)

    def test_without_a_running_ssh_there_is_nothing_to_protect(self):
        """P-03: sshd が動いていないと sshd -t / -T が失敗する（/run/sshd が無い）。何もしない。"""
        self.unit("ssh.service", "enabled", active=False)
        before = self.snap()
        result = self.assert_ok(self.run_script("ssh"))
        self.assertIn("守るものがありません", result.stdout)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.calls_with("sshd"), [])
        self.assertFalse(self.state_exists("pending"))

    def test_a_socket_activated_ssh_gets_the_config_but_no_reload(self):
        self.unit("ssh.service", "enabled", active=False)
        self.unit("ssh.socket", "enabled", active=True)
        self.assert_ok(self.run_script("ssh"))
        self.assertEqual(self.read(SSH_DROPIN), expected_ssh_dropin())
        self.assertNotIn("systemctl reload ssh", self.calls())

    def test_no_recorded_key_login_is_a_warning_not_a_refusal(self):
        self.sim_put("journal_ssh", "")
        result = self.assert_ok(self.run_script("ssh"))
        self.assertIn("鍵で pi が入った記録がありません", result.stderr)

    def test_a_dropin_that_does_not_win_is_undone_and_reported(self):
        """Match ブロックなどで password が yes のままなら、変更を残さない。"""
        for force, message in (("10.0.0.1 passwordauthentication yes\n", "passwordauthentication yes"),
                               ("192.168.1.1 kbdinteractiveauthentication yes\n", "kbdinteractiveauthentication yes"),
                               ("172.16.0.1 pubkeyauthentication no\n", "pubkeyauthentication no")):
            with self.subTest(force=force):
                self.setUp()
                self.sim_put("sshd_force", force)
                before = self.snap()
                result = self.run_script("ssh")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertIn("元に戻しました", result.stdout)
                self.assertEqual(self.snap(), before)
                self.assertEqual(self.pending(), [])
                self.assertFalse(self.exists(REVERT_TIMER) or self.exists(REVERT_SERVICE))
                self.assertFalse(self.state_exists("harden.sh"))

    def test_a_config_sshd_rejects_is_undone(self):
        before = self.snap()
        self.fail_on("sshd -t", once=True)
        result = self.run_script("ssh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("sshd -t が失敗", result.stderr)
        self.assertIn("元に戻しました", result.stdout)
        self.assertEqual(self.snap(), before)

    def test_a_failed_reload_is_undone(self):
        before = self.snap()
        self.fail_on("systemctl reload ssh", once=True)
        result = self.run_script("ssh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("元に戻しました", result.stdout)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.pending(), [])

    def test_when_the_undo_fails_it_says_so_and_keeps_the_timer(self):
        """LS-17: 戻せなかったのに「元に戻しました」と言わない。時計と確定待ちは残る。"""
        self.fail_on("systemctl reload ssh")
        result = self.run_script("ssh")
        self.assertEqual(result.returncode, 1)
        output = result.stdout + result.stderr
        self.assertIn("元に戻せませんでした", output)
        self.assertNotIn("元に戻しました", output)
        self.assertEqual(self.pending(), ["ssh"])
        self.assertTrue(self.exists(REVERT_TIMER))
        self.assertTrue(self.state_exists("harden.sh"))
        # 原因が消えたあとで、時計が呼ぶ処理がやり切る。
        os.remove(os.path.join(self.sim, "fail"))
        self.assert_ok(self.run_script("--rollback-pending"))
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.exists(REVERT_TIMER))

    def test_rerunning_a_confirmed_step_changes_nothing(self):
        self.assert_ok(self.run_script("ssh"))
        self.assert_ok(self.run_confirm())
        self.forget_calls()
        before = self.snap(times=True)
        result = self.assert_ok(self.run_script("ssh"))
        self.assertIn("すでに適用済みです", result.stdout)
        self.assertEqual(self.snap(times=True), before)
        self.assertEqual(self.mutating_calls(), [])

    def test_rerunning_an_unconfirmed_step_does_not_claim_it_is_applied(self):
        """LS-02: 確定待ちのものを「適用済み」と言わない。時計も張り直さない。"""
        self.assert_ok(self.run_script("ssh"))
        self.forget_calls()
        result = self.assert_ok(self.run_script("ssh"))
        self.assertIn("まだ確定していません", result.stdout)
        self.assertNotIn("すでに適用済みです", result.stdout)
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.pending(), ["ssh"])

    def test_an_interrupted_apply_is_redone_not_reported_as_applied(self):
        """適用の途中で止まった（done が無い）状態からの再実行は、最初からやり直す。"""
        self.assert_ok(self.run_script("ssh"))
        path = os.path.join(self.state, "applied", "ssh")
        with open(path, "r", encoding="utf-8") as handle:
            kept = [line for line in handle.read().splitlines() if not line.startswith("done=")]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(kept) + "\n")
        self.forget_calls()
        result = self.assert_ok(self.run_script("ssh"))
        self.assertNotIn("すでに適用済みです", result.stdout)
        self.assertNotIn("まだ確定していません", result.stdout)
        self.assertIn("systemctl reload ssh", self.calls())
        self.assertEqual(self.applied_meta("ssh", "done"), "1")

    def test_rollback_restores_a_preexisting_file_exactly(self):
        self.put(SSH_DROPIN, "# 運用者が書いたもの\nX11Forwarding yes\n", 0o600)
        before = self.snap()
        self.assert_ok(self.run_script("ssh"))
        self.assertEqual(self.read(SSH_DROPIN), expected_ssh_dropin())
        self.assert_ok(self.run_script("--rollback", "ssh"))
        self.assertEqual(self.read(SSH_DROPIN), "# 運用者が書いたもの\nX11Forwarding yes\n")
        self.assertEqual(self.mode(SSH_DROPIN), 0o600)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.exists(REVERT_TIMER))

    def test_rollback_after_confirmation_removes_the_dropin_and_reloads(self):
        before = self.snap()
        self.assert_ok(self.run_script("ssh"))
        self.assert_ok(self.run_confirm())
        self.forget_calls()
        self.assert_ok(self.run_script("--rollback", "ssh"))
        self.assertEqual(self.snap(), before)
        self.assertIn("sshd -t", self.calls())
        self.assertIn("systemctl reload ssh", self.calls())
        self.assertFalse(self.state_exists("applied/ssh"))

    def test_rolling_back_a_step_that_was_never_applied_is_a_no_op(self):
        before = self.snap()
        result = self.assert_ok(self.run_script("--rollback", "ssh"))
        self.assertIn("適用されていません", result.stdout)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.mutating_calls(), [])

    def test_dry_run_shows_the_file_and_the_commands_and_writes_nothing(self):
        before = self.snap(times=True)
        result = self.assert_ok(self.run_script("--dry-run", "ssh"))
        self.assertIn(SSH_DROPIN, result.stdout)
        self.assertIn("PasswordAuthentication no", result.stdout)
        self.assertIn("AllowUsers pi", result.stdout)
        self.assertIn("systemctl reload ssh", result.stdout)
        self.assertIn("campus-chime-harden-revert.timer", result.stdout)
        self.assertIn("/var/lib/campus-chime-harden/harden.sh --rollback-pending", result.stdout)
        self.assertEqual(self.snap(times=True), before)
        self.assertEqual(self.mutating_calls(), [])
        self.assertFalse(os.path.exists(self.state))


FW_DIR = "/etc/campus-chime-harden"
FW_FILE = "/etc/campus-chime-harden/firewall.nft"
FW_UNIT = "/etc/systemd/system/campus-chime-firewall.service"


def expected_ruleset(ssh_v4=("192.168.1.0/24",), ssh_v6=("fe80::/10",), local_v4=("192.168.1.0/24",)):
    """手で書いた、ルールの見本（スクリプトの組み立てとは別に持つ）。"""
    lines = ["table inet campus_chime_filter", "delete table inet campus_chime_filter",
             "table inet campus_chime_filter {"]
    if ssh_v4:
        lines.append("  set ssh_v4 { type ipv4_addr; flags interval; elements = { %s } }" % ", ".join(ssh_v4))
    if ssh_v6:
        lines.append("  set ssh_v6 { type ipv6_addr; flags interval; elements = { %s } }" % ", ".join(ssh_v6))
    if local_v4:
        lines.append("  set local_v4 { type ipv4_addr; flags interval; elements = { %s } }" % ", ".join(local_v4))
    lines += ["  chain input {",
              "    type filter hook input priority filter; policy drop;",
              "    iif \"lo\" accept",
              "    ct state established,related accept",
              "    ct state invalid drop",
              "    meta l4proto icmp accept",
              "    meta l4proto ipv6-icmp accept",
              "    meta l4proto igmp accept",
              "    udp sport 67 udp dport 68 accept",
              "    ip6 saddr fe80::/10 udp sport 547 udp dport 546 accept",
              "    udp dport 5353 ip daddr 224.0.0.251 accept",
              "    udp dport 5353 ip6 daddr ff02::fb accept"]
    if local_v4:
        lines.append("    udp dport 5353 ip saddr @local_v4 accept")
    if ssh_v4:
        lines.append("    tcp dport 22 ip saddr @ssh_v4 accept")
    if ssh_v6:
        lines.append("    tcp dport 22 ip6 saddr @ssh_v6 accept")
    lines += ["    fib daddr type { broadcast, multicast } drop",
              "    limit rate 6/minute burst 10 packets log prefix \"campus-chime-fw drop: \" level info",
              "  }",
              "  chain forward { type filter hook forward priority filter; policy drop; }",
              "}"]
    return "\n".join(lines) + "\n"


EXPECTED_FW_UNIT = """[Unit]
Description=campus-chime の防火壁（scripts/harden.sh firewall）
Documentation=file:/etc/campus-chime-harden/firewall.nft
DefaultDependencies=no
Wants=network-pre.target
Before=network-pre.target shutdown.target
Conflicts=shutdown.target
After=nftables.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/campus-chime-harden/firewall.nft
ExecStop=/usr/sbin/nft delete table inet campus_chime_filter

[Install]
WantedBy=sysinit.target
"""


class FirewallStepTest(HardenCase):
    def loaded_ruleset(self):
        with open(os.path.join(self.sim, "nft", "table.nft"), "r", encoding="utf-8") as handle:
            return handle.read()

    def nft_loaded(self):
        return os.path.exists(os.path.join(self.sim, "nft", "loaded"))

    def test_the_default_ruleset_allows_the_pis_own_subnet_and_link_local(self):
        result = self.assert_ok(self.run_script("firewall"))
        self.assertEqual(self.loaded_ruleset(), expected_ruleset())
        self.assertEqual(self.state_file("firewall.nft"), expected_ruleset())
        self.assertEqual(self.pending(), ["firewall"])
        self.assertTrue(self.nft_loaded())
        # 確定するまでは、再起動で消える（永続化していない）。
        self.assertFalse(self.exists(FW_FILE) or self.exists(FW_UNIT) or self.exists(FW_DIR))
        self.assertNotIn("systemctl enable campus-chime-firewall.service", self.calls())
        self.assertIn("sudo bash scripts/harden.sh --confirm", result.stdout)

    def test_the_ruleset_is_checked_then_loaded_only_after_the_timer_is_armed(self):
        """LS-02: 時計を張ってから nft -f。nft -c は先に通す。"""
        self.assert_ok(self.run_script("firewall"))
        calls = self.calls()
        armed = calls.index("systemctl restart " + REVERT_UNITS)
        checks = [i for i, call in enumerate(calls) if call.startswith("nft -c -f")]
        load = next(i for i, call in enumerate(calls) if call.startswith("nft -f"))
        self.assertTrue(checks)
        self.assertLess(max(checks), load)
        self.assertLess(armed, load)
        self.assertEqual(calls[load + 1], "snapshot revert-armed=1")

    def test_ssh_from_replaces_the_default_sources_and_ipv6_needs_its_own_range(self):
        self.sim_put("ss.out", "")
        self.assert_ok(self.run_script("firewall", "--ssh-from", "10.0.0.0/8", "--ssh-from", "192.168.5.7/24"))
        self.assertEqual(self.loaded_ruleset(),
                         expected_ruleset(ssh_v4=("10.0.0.0/8", "192.168.5.0/24"), ssh_v6=(), local_v4=("192.168.1.0/24",)))
        self.assertNotIn("ip6 saddr @ssh_v6", self.loaded_ruleset())

    def test_an_ipv6_ssh_from_adds_the_v6_set_and_overlaps_are_merged(self):
        self.sim_put("ss.out", "")
        self.assert_ok(self.run_script("firewall", "--ssh-from", "10.0.0.0/8", "--ssh-from", "10.1.0.0/16",
                                       "--ssh-from", "2001:db8:1::/48", "--ssh-from", "10.0.0.5"))
        self.assertEqual(self.loaded_ruleset(),
                         expected_ruleset(ssh_v4=("10.0.0.0/8",), ssh_v6=("2001:db8:1::/48",)))

    def test_the_pis_own_ipv6_prefixes_join_the_default_sources(self):
        self.sim_put("ip6.out", "3: eth0    inet6 2001:db8:77:5::23/64 scope global dynamic mngtmpaddr\\"
                                "       valid_lft 86380sec preferred_lft 14380sec\n")
        self.assert_ok(self.run_script("firewall"))
        self.assertEqual(self.loaded_ruleset(), expected_ruleset(ssh_v6=("2001:db8:77:5::/64", "fe80::/10")))

    def test_a_bad_ssh_from_is_refused_with_every_reason_and_nothing_changes(self):
        """どれも同じ実行で、理由がすべて表示される（理由を 1 つずつ直す手間をかけさせない）。"""
        bad = (("abc", "CIDR"), ("999.1.1.0/24", "CIDR"), ("0.0.0.0/0", "広すぎます"), ("::/0", "広すぎます"),
               ("0.0.0.0/1", "広すぎます"), ("128.0.0.0/1", "広すぎます"), ("10.0.0.0/7", "広すぎます"),
               ("2000::/16", "広すぎます"), ("::/1", "広すぎます"))
        self.sim_put("ss.out", "")
        before = self.snap()
        args = ["firewall"]
        for value, _ in bad:
            args += ["--ssh-from", value]
        result = self.run_script(*args)
        self.assert_nothing_changed(before, result)
        for value, message in bad:
            self.assertRegex(result.stderr, r"--ssh-from {0} は.*{1}".format(re.escape(value), message), value)
        self.assertFalse(self.nft_loaded())

    def test_the_narrowest_allowed_ranges_are_accepted(self):
        self.sim_put("ss.out", "")
        self.assert_ok(self.run_script("firewall", "--ssh-from", "10.0.0.0/8", "--ssh-from", "2001:db8::/32"))

    def test_a_current_ssh_peer_outside_the_allowed_range_is_refused_with_an_example(self):
        for line, address, example in (("172.16.5.9:40000", "172.16.5.9", "--ssh-from 172.16.5.0/24"),
                                       ("[2001:db8:5:6::7]:5555", "2001:db8:5:6::7", "--ssh-from 2001:db8:5:6::/64")):
            with self.subTest(peer=line):
                self.sim_put("ss.out", "0 0 192.168.1.23:22 " + line + "\n")
                before = self.snap()
                result = self.run_script("firewall")
                self.assert_nothing_changed(before, result)
                self.assertIn(address, result.stderr)
                self.assertIn(example, result.stderr)
                self.assertFalse(self.nft_loaded())

    def test_peers_that_are_covered_or_loopback_are_fine(self):
        """P-06: 127.0.0.0/8・::1・::ffff:127.x は無視。IPv4 射影アドレスは IPv4 として見る。ゾーン付きの fe80 も。"""
        self.sim_put("ss.out", "0 0 192.168.1.23:22 192.168.1.50:51234\n"
                               "0 0 127.0.0.1:22 127.0.0.1:50000\n"
                               "0 0 [::1]:22 [::1]:50001\n"
                               "0 0 [::ffff:127.0.0.1]:22 [::ffff:127.0.0.1]:50002\n"
                               "0 0 [::ffff:192.168.1.23]:22 [::ffff:192.168.1.60]:50003\n"
                               "0 0 [fe80::1]:22 [fe80::2%eth0]:50004\n")
        self.assert_ok(self.run_script("firewall"))

    def test_a_peer_only_reachable_over_ipv6_is_refused_when_only_ipv4_is_allowed(self):
        self.sim_put("ss.out", "0 0 [fe80::1]:22 [fe80::2%eth0]:50004\n")
        before = self.snap()
        result = self.run_script("firewall", "--ssh-from", "192.168.1.0/24")
        self.assert_nothing_changed(before, result)
        self.assertIn("fe80::2", result.stderr)

    def test_without_a_working_ss_it_refuses_unless_forced(self):
        """LS-09: 接続元を調べられないなら止める。"""
        for how in ("missing", "failing"):
            with self.subTest(how=how):
                self.setUp()
                if how == "missing":
                    self.hide_stub("ss")
                else:
                    self.sim_put("ss_rc", "1")
                before = self.snap()
                result = self.run_script("firewall")
                self.assert_nothing_changed(before, result)
                self.assertIn("接続元を調べられません", result.stderr)
                forced = self.assert_ok(self.run_script("firewall", "--force"))
                self.assertIn("--force が付いている", forced.stderr)
                self.assertTrue(self.nft_loaded())

    def test_a_peer_outside_the_range_goes_through_with_force_and_a_warning(self):
        self.sim_put("ss.out", "0 0 192.168.1.23:22 172.16.5.9:40000\n")
        result = self.assert_ok(self.run_script("firewall", "--force"))
        self.assertIn("172.16.5.9", result.stderr)
        self.assertIn("この接続は切れます", result.stderr)

    def test_without_any_ipv4_network_it_asks_for_ssh_from(self):
        """LS-10"""
        self.sim_put("ip4.out", "")
        before = self.snap()
        result = self.run_script("firewall")
        self.assert_nothing_changed(before, result)
        self.assertIn("--ssh-from", result.stderr)
        self.assertFalse(self.nft_loaded())
        self.sim_put("ss.out", "")
        ok = self.assert_ok(self.run_script("firewall", "--ssh-from", "192.168.1.0/24"))
        self.assertEqual(self.loaded_ruleset(), expected_ruleset(ssh_v6=(), local_v4=()))
        self.assertIn("firewall", ok.stdout)

    def test_a_too_wide_own_network_asks_for_ssh_from(self):
        self.sim_put("ip4.out", "2: eth0    inet 10.1.2.3/4 brd 10.255.255.255 scope global eth0\\       valid_lft forever\n")
        before = self.snap()
        result = self.run_script("firewall")
        self.assert_nothing_changed(before, result)
        self.assertIn("広すぎて", result.stderr)

    def test_another_firewall_on_the_input_hook_is_refused_unless_forced(self):
        os.makedirs(os.path.join(self.sim, "nft"), exist_ok=True)
        self.sim_put("nft/other_tables", "table inet filter\ntable ip nat\n")
        self.sim_put("nft/other_filter", "table inet filter {\n chain input {\n type filter hook input priority 0;\n }\n}\n")
        self.sim_put("nft/other_nat", "table ip nat {\n chain post {\n type nat hook postrouting priority 100;\n }\n}\n")
        before = self.snap()
        result = self.run_script("firewall")
        self.assert_nothing_changed(before, result)
        self.assertIn("inet/filter", result.stderr)
        self.assertNotIn("ip/nat", result.stderr)
        self.assertIn("table inet filter", result.stderr)
        forced = self.assert_ok(self.run_script("firewall", "--force"))
        self.assertIn("重ねて", forced.stderr)

    def test_a_ruleset_nft_rejects_is_refused_before_anything_changes(self):
        self.sim_put("nft_check_rc", "1")
        before = self.snap()
        result = self.run_script("firewall")
        self.assert_nothing_changed(before, result)
        self.assertIn("nft -c", result.stderr)

    def test_without_nft_it_waits_for_idle_then_installs_nftables(self):
        self.hide_stub("nft")
        self.assert_ok(self.run_script("firewall"))
        calls = self.calls()
        idle = next(i for i, call in enumerate(calls) if "--wait-idle" in call)
        install = calls.index("apt-get install -y nftables")
        self.assertLess(idle, install)
        self.assertEqual(self.applied_meta("firewall", "installed_nft"), "1")
        self.assertEqual(self.loaded_ruleset(), expected_ruleset())

    def test_rolling_back_never_purges_nftables(self):
        self.hide_stub("nft")
        self.assert_ok(self.run_script("firewall"))
        self.forget_calls()
        result = self.assert_ok(self.run_script("--rollback", "firewall"))
        self.assertEqual(self.calls_with("apt-get"), [])
        self.assertFalse(self.nft_loaded())
        self.assertIn("nftables は入れたままです", result.stdout)

    def test_a_load_that_fails_is_undone(self):
        before = self.snap()
        self.fail_on("nft -f")
        result = self.run_script("firewall")
        self.assertEqual(result.returncode, 1)
        self.assertIn("元に戻しました", result.stdout)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.nft_loaded())
        self.assertFalse(self.exists(REVERT_TIMER))

    def test_a_table_that_does_not_show_up_after_loading_is_undone(self):
        self.fail_on("nft list table inet campus_chime_filter", once=True)
        result = self.run_script("firewall")
        self.assertEqual(result.returncode, 1)
        self.assertIn("防火壁の表が見つかりません", result.stderr)
        self.assertIn("元に戻しました", result.stdout)
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.nft_loaded())

    # -- 確定 -----------------------------------------------------------
    def test_confirming_persists_the_ruleset_and_a_unit_and_stops_the_timer(self):
        self.assert_ok(self.run_script("firewall"))
        before_confirm = self.snap()
        self.forget_calls()
        result = self.assert_ok(self.run_confirm())
        self.assertEqual(self.read(FW_FILE), expected_ruleset())
        self.assertEqual(self.mode(FW_FILE), 0o644)
        self.assertEqual(self.read(FW_UNIT), EXPECTED_FW_UNIT)
        self.assertEqual(self.mode(FW_UNIT), 0o644)
        self.assertIn("systemctl enable campus-chime-firewall.service", self.calls())
        self.assertIn("systemctl daemon-reload", self.calls())
        self.assertIn("systemctl disable --now " + REVERT_UNITS, self.calls())
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.exists(REVERT_TIMER) or self.exists(REVERT_SERVICE))
        self.assertFalse(self.state_exists("harden.sh"))
        self.assertTrue(self.nft_loaded())
        self.assertEqual(self.applied_meta("firewall", "persisted"), "1")
        self.assertNotEqual(self.snap(), before_confirm)
        self.assertIn("確定しました", result.stdout)
        # nftables.conf と nftables.service には触らない。
        self.assertFalse(self.exists("/etc/nftables.conf"))
        self.assertEqual(self.calls_with("systemctl enable nftables"), [])

    def test_a_firewall_not_loaded_at_confirmation_is_not_persisted(self):
        """LS-11: いま読み込まれていないものを、永続化しない。"""
        before = self.snap()
        self.assert_ok(self.run_script("firewall"))
        os.remove(os.path.join(self.sim, "nft", "loaded"))
        result = self.run_confirm()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("入れ直して", result.stderr)
        self.assertFalse(self.exists(FW_FILE) or self.exists(FW_UNIT))
        self.assertNotIn("systemctl enable campus-chime-firewall.service", self.calls())
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.state_exists("applied/firewall"))
        self.assertEqual(self.snap(), before)

    def test_a_firewall_loaded_before_a_reboot_is_not_persisted(self):
        """LS-11: 再起動をまたいだ確定は、確かめた表ではない（boot_id が違う）。"""
        self.assert_ok(self.run_script("firewall"))
        self.proc_boot_id("22222222-2222-2222-2222-222222222222")
        result = self.run_confirm()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("再起動", result.stderr)
        self.assertFalse(self.exists(FW_FILE) or self.exists(FW_UNIT))
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.nft_loaded())

    def test_rolling_back_a_confirmed_firewall_restores_everything(self):
        before = self.snap()
        self.assert_ok(self.run_script("firewall"))
        self.assert_ok(self.run_confirm())
        self.forget_calls()
        self.assert_ok(self.run_script("--rollback", "firewall"))
        self.assertEqual(self.snap(), before)
        self.assertFalse(self.nft_loaded())
        self.assertIn("nft delete table inet campus_chime_filter", self.calls())
        self.assertIn("systemctl disable campus-chime-firewall.service", self.calls())

    def test_rolling_back_ignores_only_a_missing_table(self):
        """LS-04"""
        self.assert_ok(self.run_script("firewall"))
        os.remove(os.path.join(self.sim, "nft", "loaded"))
        self.assert_ok(self.run_script("--rollback", "firewall"))

    def test_any_other_nft_error_while_rolling_back_is_a_failure(self):
        self.assert_ok(self.run_script("firewall"))
        self.fail_on("nft delete table")
        result = self.run_script("--rollback", "firewall")
        self.assertEqual(result.returncode, 1)
        self.assertIn("防火壁の表を消せませんでした", result.stderr)
        self.assertIn("元に戻せませんでした", result.stderr)
        self.assertNotIn("元に戻しました", result.stdout)
        self.assertTrue(self.nft_loaded())
        self.assertEqual(self.pending(), ["firewall"])
        self.assertTrue(self.exists(REVERT_TIMER))

    def test_a_table_that_is_still_there_after_deleting_is_a_failure(self):
        self.assert_ok(self.run_script("firewall"))
        os.makedirs(os.path.join(self.sim, "nft"), exist_ok=True)
        self.sim_put("nft/sticky", "")
        result = self.run_script("--rollback", "firewall")
        self.assertEqual(result.returncode, 1)
        self.assertIn("まだ残っています", result.stderr)
        self.assertEqual(self.pending(), ["firewall"])

    def test_rerunning_a_confirmed_firewall_changes_nothing_but_a_new_range_reapplies_it(self):
        self.assert_ok(self.run_script("firewall"))
        self.assert_ok(self.run_confirm())
        self.forget_calls()
        result = self.assert_ok(self.run_script("firewall"))
        self.assertIn("すでに適用済みです", result.stdout)
        self.assertEqual(self.mutating_calls(), [])
        self.sim_put("ss.out", "")
        self.assert_ok(self.run_script("firewall", "--ssh-from", "192.168.1.0/24", "--ssh-from", "10.9.0.0/16"))
        self.assertEqual(self.loaded_ruleset(),
                         expected_ruleset(ssh_v4=("10.9.0.0/16", "192.168.1.0/24"), ssh_v6=()))
        self.assertEqual(self.pending(), ["firewall"])
        # 確定するまでは、以前に確定した設定が再起動後の姿。
        self.assertEqual(self.read(FW_FILE), expected_ruleset())
        self.assert_ok(self.run_confirm())
        self.assertEqual(self.read(FW_FILE), expected_ruleset(ssh_v4=("10.9.0.0/16", "192.168.1.0/24"), ssh_v6=()))
        self.assert_ok(self.run_script("--rollback", "firewall"))
        self.assertFalse(self.exists(FW_FILE) or self.exists(FW_UNIT) or self.exists(FW_DIR))

    def test_a_crash_after_the_table_is_loaded_still_leaves_an_armed_timer(self):
        result = self.run_and_crash("nft list table inet campus_chime_filter", "firewall")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.nft_loaded())
        self.assertEqual(self.pending(), ["firewall"])
        self.assertEqual(self.unit_state(REVERT_UNITS), ("enabled", True))
        copy = os.path.join(self.state, "harden.sh")
        rollback = subprocess.run(["bash", copy, "--rollback-pending"], capture_output=True, text=True,
                                  encoding="utf-8", env=self.environment(), cwd=self.tmp, stdin=subprocess.DEVNULL)
        self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
        self.assertFalse(self.nft_loaded())

    def test_a_crash_before_loading_leaves_nothing_loaded(self):
        result = self.run_and_crash("nft -f", "firewall")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.nft_loaded())

    def test_rerunning_an_unconfirmed_firewall_does_not_claim_it_is_applied(self):
        """LS-02: 確定待ちのものを「適用済み」と言わず、もう一度読み込みもしない。"""
        self.assert_ok(self.run_script("firewall"))
        self.forget_calls()
        result = self.assert_ok(self.run_script("firewall"))
        self.assertIn("まだ確定していません", result.stdout)
        self.assertNotIn("すでに適用済みです", result.stdout)
        self.assertEqual(self.calls_with("nft -f"), [])
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.pending(), ["firewall"])

    def test_dry_run_shows_the_ruleset_and_loads_nothing(self):
        before = self.snap(times=True)
        result = self.assert_ok(self.run_script("--dry-run", "firewall"))
        self.assertIn("policy drop", result.stdout)
        self.assertIn("nft -f", result.stdout)
        self.assertEqual(self.snap(times=True), before)
        self.assertEqual(self.mutating_calls(), [])
        self.assertFalse(self.nft_loaded())
        self.assertFalse(os.path.exists(self.state))


@unittest.skipUnless(REAL_NFT, "root で本物の nft -c -f が通る環境でだけ、作ったルールを検査する")
class RealNftCheckTest(HardenCase):
    """作ったルールを本物の nft で検査する（検査モード ``-c`` だけ。ルールは読み込まない）。"""

    def check(self, *args, ip4=None, ip6=None):
        self.sim_put("ss.out", "")
        if ip4 is not None:
            self.sim_put("ip4.out", ip4)
        if ip6 is not None:
            self.sim_put("ip6.out", ip6)
        self.assert_ok(self.run_script("firewall", *args))
        with open(os.path.join(self.sim, "nft", "table.nft"), "r", encoding="utf-8") as handle:
            text = handle.read()
        result = subprocess.run([REAL_NFT, "-c", "-f", "-"], input=text, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + text)

    def test_ipv4_only_with_ssh_from(self):
        self.check("--ssh-from", "192.168.1.0/24")

    def test_ipv4_and_ipv6_with_ssh_from(self):
        self.check("--ssh-from", "192.168.1.0/24", "--ssh-from", "2001:db8::/48", "--ssh-from", "10.0.0.0/8")

    def test_the_default_sources(self):
        self.check()

    def test_the_default_sources_with_own_ipv6_prefixes(self):
        self.check(ip6="3: eth0    inet6 2001:db8:77:5::23/64 scope global dynamic\\       valid_lft forever\n")

    def test_ipv6_only_ssh_from_leaves_out_the_v4_sets(self):
        self.check("--ssh-from", "2001:db8::/48", ip4="")

    def test_overlapping_ranges_are_merged_so_nft_accepts_them(self):
        self.check("--ssh-from", "10.0.0.0/8", "--ssh-from", "10.1.0.0/16", "--ssh-from", "10.1.2.3")


SUDO_FILE = "/etc/sudoers.d/010_pi-nopasswd"


class SudoStepTest(HardenCase):
    def test_it_needs_a_terminal_for_the_password_check(self):
        before = self.snap()
        result = self.run_script("sudo")
        self.assert_nothing_changed(before, result)
        self.assertIn("端末ではありません", result.stderr)
        self.assertTrue(self.exists(SUDO_FILE))

    def test_it_moves_the_nopasswd_file_away_and_checks_the_password(self):
        result = self.assert_ok(self.run_tty("sudo"))
        self.assertFalse(self.exists(SUDO_FILE))
        self.assertTrue(self.exists("/etc/sudoers.d/README"))
        calls = self.calls()
        k = calls.index("runuser -u pi -- sudo -k")
        self.assertIn("runuser -u pi -- sudo -n -v", calls[k:])
        self.assertLess(calls.index("runuser -u pi -- sudo -n -v"), calls.index("runuser -u pi -- sudo -v"))
        self.assertGreaterEqual(len(self.calls_with("visudo -c")), 2)
        self.assertIn("update.sh", result.stdout)
        self.assertIn("setup.sh", result.stdout)
        self.assertEqual(self.pending(), [])
        # 控えは状態の置き場にあり、mode も持ち主もそのまま。
        backup = os.path.join(self.state, "backup", "sudo", "etc", "sudoers.d", "010_pi-nopasswd")
        self.assertEqual(slurp(backup), "pi ALL=(ALL) NOPASSWD: ALL\n")
        self.assertEqual(os.stat(backup).st_mode & 0o7777, 0o440)

    def test_a_failed_password_check_puts_the_file_back_exactly(self):
        before = self.snap()
        self.sim_put("sudo_v_rc", "1")
        result = self.run_tty("sudo")
        self.assertEqual(result.returncode, 1)
        self.assertIn("元に戻しました", result.stdout)
        self.assertIn("パスワードで sudo を確かめられませんでした", result.stderr)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.mode(SUDO_FILE), 0o440)
        self.assertGreaterEqual(len(self.calls_with("visudo -c")), 3)

    def test_a_remaining_nopasswd_rule_is_caught_by_the_check_and_undone(self):
        """別の場所の NOPASSWD が残っていて、パスワードなしで通ってしまうなら、通ったことにしない。"""
        before = self.snap()
        self.sim_put("sudo_nv_rc", "0")
        result = self.run_tty("sudo")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("パスワードなしで sudo が通ってしまいます", result.stderr)
        self.assertIn("元に戻しました", result.stdout)
        self.assertEqual(self.snap(), before)

    def test_it_refuses_in_the_cases_where_removing_the_rule_would_lock_sudo(self):
        cases = (("sudo グループではない", lambda: self.sim_put("groups", "pi audio\n"), "sudo グループに入っていません"),
                 ("パスワードを求める規則が無い", lambda: self.sim_put("sudo_l.out", "User pi may run:\n    (ALL) NOPASSWD: ALL\n"),
                  "(ALL : ALL) ALL の規則"),
                 ("パスワードが無い", lambda: self.sim_put("passwd_S", "pi L 2024-01-01 0 99999 7 -1\n"),
                  "sudo passwd pi"),
                 ("sudoers に誤りがある", lambda: self.sim_put("visudo_rc", "1"), "visudo -c が失敗"),
                 ("主の sudoers にも NOPASSWD", lambda: self.put("/etc/sudoers", SUDOERS + "%admin ALL=(ALL) NOPASSWD: ALL\n", 0o440),
                  "/etc/sudoers に NOPASSWD"),
                 ("ファイルに他の内容がある", lambda: self.put(SUDO_FILE, "Defaults:pi !authenticate\npi ALL=(ALL) NOPASSWD: ALL\n", 0o440),
                  "1 行以外の内容"),
                 ("他のユーザーにも NOPASSWD", lambda: self.put(SUDO_FILE, "pi ALL=(ALL) NOPASSWD: ALL\nbob ALL=(ALL) NOPASSWD: ALL\n", 0o440),
                  "1 行以外の内容"),
                 ("グループに NOPASSWD", lambda: self.put(SUDO_FILE, "%sudo ALL=(ALL) NOPASSWD: ALL\n", 0o440),
                  "1 行以外の内容"))
        for name, damage, message in cases:
            with self.subTest(name):
                self.setUp()
                damage()
                before = self.snap()
                result = self.run_tty("sudo")
                self.assert_nothing_changed(before, result)
                self.assertIn(message, result.stderr)
                self.assertTrue(self.exists(SUDO_FILE))
                self.assertEqual(self.calls_with("runuser"), [])

    def test_whitespace_variants_of_the_dedicated_rule_are_recognised(self):
        for text in ("pi ALL=(ALL:ALL) NOPASSWD: ALL\n", "pi\tALL = (ALL : ALL)   NOPASSWD:   ALL\n",
                     "# 説明\n\npi   ALL=(ALL)  NOPASSWD:ALL  \n"):
            with self.subTest(text=text):
                self.setUp()
                self.put(SUDO_FILE, text, 0o440)
                self.assert_ok(self.run_tty("sudo"))
                self.assertFalse(self.exists(SUDO_FILE))

    def test_without_a_nopasswd_file_it_says_the_password_is_already_required(self):
        os.remove(self.sys_path(SUDO_FILE))
        before = self.snap()
        result = self.assert_ok(self.run_tty("sudo"))
        self.assertIn("すでにパスワードが必要です", result.stdout)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.mutating_calls(), [])

    def test_files_sudo_itself_ignores_are_left_alone(self):
        """名前に . を含むファイルや ~ で終わるファイルは、sudo が読まない。"""
        os.remove(self.sys_path(SUDO_FILE))
        self.put("/etc/sudoers.d/old.bak", "pi ALL=(ALL) NOPASSWD: ALL\nbob ALL=(ALL) NOPASSWD: ALL\n", 0o440)
        self.put("/etc/sudoers.d/x~", "pi ALL=(ALL) NOPASSWD: ALL\n", 0o440)
        result = self.assert_ok(self.run_tty("sudo"))
        self.assertIn("すでにパスワードが必要です", result.stdout)

    def test_rollback_puts_the_file_back_with_its_mode(self):
        before = self.snap()
        self.assert_ok(self.run_tty("sudo"))
        self.assert_ok(self.run_script("--rollback", "sudo"))
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.mode(SUDO_FILE), 0o440)
        self.assertEqual(len(self.calls_with("visudo -c")), 3)

    def test_a_visudo_failure_after_rollback_is_reported(self):
        self.assert_ok(self.run_tty("sudo"))
        self.sim_put("visudo_rc", "1")
        result = self.run_script("--rollback", "sudo")
        self.assertEqual(result.returncode, 1)
        self.assertIn("visudo -c が失敗しました", result.stderr)

    def test_dry_run_as_a_normal_user_says_it_cannot_read_sudoers(self):
        before = self.snap(times=True)
        result = self.assert_ok(self.run_script("--dry-run", "sudo", FAKE_UID="1000"))
        self.assertIn("root でない", result.stdout)
        self.assertEqual(self.snap(times=True), before)

    def test_dry_run_as_root_lists_the_file_it_would_move(self):
        before = self.snap(times=True)
        result = self.assert_ok(self.run_tty("--dry-run", "sudo"))
        self.assertIn("削除: " + SUDO_FILE, result.stdout)
        self.assertEqual(self.snap(times=True), before)


class ServicesStepTest(HardenCase):
    def states(self):
        return {name: self.unit_state(name) for name in ("bluetooth.service", "hciuart.service", "ModemManager.service",
                                                         "avahi-daemon.service")}

    def test_it_stops_and_masks_the_installed_units_and_records_how_they_were(self):
        before = self.states()
        result = self.assert_ok(self.run_script("services"))
        after = self.states()
        for name in ("bluetooth.service", "hciuart.service", "ModemManager.service"):
            self.assertEqual(after[name], ("masked", False), name)
        self.assertEqual(after["avahi-daemon.service"], before["avahi-daemon.service"])
        self.assertIn("avahi-daemon", result.stdout)
        self.assertEqual(self.applied_meta("services", "unit.bluetooth.service.enabled"), "enabled")
        self.assertEqual(self.applied_meta("services", "unit.bluetooth.service.active"), "active")
        self.assertEqual(self.applied_meta("services", "unit.hciuart.service.active"), "inactive")
        self.assertIn("systemctl disable --now bluetooth.service", self.calls())
        self.assertIn("systemctl mask bluetooth.service", self.calls())

    def test_rollback_restores_what_each_unit_was(self):
        before = self.states()
        snap = self.snap()
        self.assert_ok(self.run_script("services"))
        self.assert_ok(self.run_script("--rollback", "services"))
        self.assertEqual(self.states(), before)
        self.assertEqual(self.snap(), snap)
        self.assertFalse(self.state_exists("applied/services"))

    def test_units_that_are_not_installed_are_skipped_and_masked_ones_are_left_as_they_were(self):
        os.remove(os.path.join(self.sim, "units", "ModemManager.service.exists"))
        os.remove(os.path.join(self.sim, "units", "ModemManager.service.state"))
        os.remove(os.path.join(self.sim, "units", "ModemManager.service.active"))
        self.unit("hciuart.service", "masked", active=False)
        result = self.assert_ok(self.run_script("services"))
        self.assertIn("ModemManager.service: 入っていないので", result.stdout)
        self.assertNotIn("systemctl mask ModemManager.service", self.calls())
        self.assertNotIn("systemctl mask hciuart.service", self.calls())
        self.assert_ok(self.run_script("--rollback", "services"))
        self.assertEqual(self.unit_state("hciuart.service"), ("masked", False))
        self.assertEqual(self.unit_state("bluetooth.service"), ("enabled", True))

    def test_rerunning_changes_nothing(self):
        self.assert_ok(self.run_script("services"))
        self.forget_calls()
        result = self.assert_ok(self.run_script("services"))
        self.assertIn("すでに止めて無効", result.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_a_failure_halfway_puts_every_unit_back(self):
        before = self.states()
        self.fail_on("systemctl mask ModemManager.service")
        result = self.run_script("services")
        self.assertEqual(result.returncode, 1)
        self.assertIn("元に戻しました", result.stdout)
        self.assertEqual(self.states(), before)
        self.assertFalse(self.state_exists("applied/services"))

    def test_a_bluetooth_speaker_stops_the_step_because_the_chime_would_go_silent(self):
        cases = (("ペアリング済みの機器", lambda: self.mkdir("/var/lib/bluetooth/AA:BB:CC:DD:EE:FF/11:22:33:44:55:66")),
                 ("bluealsa の設定", lambda: self.put("/etc/asound.conf", "defaults.bluealsa.interface \"hci0\"\n")),
                 ("利用者の bluez の設定", lambda: self.put("/home/pi/.asoundrc", "pcm.btspeaker { type bluez }\n")))
        for name, setup in cases:
            with self.subTest(name):
                self.setUp()
                setup()
                before = self.snap()
                result = self.run_script("services")
                self.assert_nothing_changed(before, result)
                self.assertIn("Bluetooth のスピーカー", result.stderr)
                self.assertEqual(self.unit_state("bluetooth.service"), ("enabled", True))
                forced = self.assert_ok(self.run_script("services", "--force"))
                self.assertIn("--force が付いている", forced.stderr)
                self.assertEqual(self.unit_state("bluetooth.service"), ("masked", False))

    def test_a_bluetooth_adapter_without_paired_devices_is_fine(self):
        self.mkdir("/var/lib/bluetooth/AA:BB:CC:DD:EE:FF/cache")
        self.put("/var/lib/bluetooth/AA:BB:CC:DD:EE:FF/settings", "[General]\n")
        self.assert_ok(self.run_script("services"))

    def test_nothing_installed_means_nothing_to_do(self):
        for name in ("bluetooth.service", "hciuart.service", "ModemManager.service"):
            for suffix in (".exists", ".state", ".active"):
                path = os.path.join(self.sim, "units", name + suffix)
                if os.path.exists(path):
                    os.remove(path)
        result = self.assert_ok(self.run_script("services"))
        self.assertIn("対象のサービスが入っていません", result.stdout)
        self.assertEqual(self.mutating_calls(), [])


JOURNAL_DROPIN = "/etc/systemd/journald.conf.d/50-campus-chime-harden.conf"
EXPECTED_JOURNAL = """# scripts/harden.sh journal が作成（元に戻す: sudo bash scripts/harden.sh --rollback journal）
[Journal]
Storage=persistent
SystemMaxUse=200M
"""


class JournalStepTest(HardenCase):
    def test_it_writes_the_dropin_creates_the_directory_and_restarts_journald(self):
        self.assert_ok(self.run_script("journal"))
        self.assertEqual(self.read(JOURNAL_DROPIN), EXPECTED_JOURNAL)
        self.assertEqual(self.mode(JOURNAL_DROPIN), 0o644)
        self.assertTrue(os.path.isdir(self.sys_path("/var/log/journal")))
        self.assertEqual(self.mode("/var/log/journal"), 0o2755)
        calls = self.calls()
        order = [calls.index(call) for call in ("systemd-tmpfiles --create --prefix /var/log/journal",
                                                "systemctl restart systemd-journald", "journalctl --flush")]
        self.assertEqual(order, sorted(order))

    def test_rollback_removes_a_directory_it_created_and_says_the_logs_go(self):
        before = self.snap()
        self.assert_ok(self.run_script("journal"))
        self.put("/var/log/journal/abc/system.journal", "ログ", 0o640)
        self.forget_calls()
        result = self.assert_ok(self.run_script("--rollback", "journal"))
        self.assertEqual(self.snap(), before)
        self.assertIn("ログも消えます", result.stderr)
        self.assertIn("journalctl --relinquish-var", self.calls())
        self.assertIn("systemctl restart systemd-journald", self.calls())

    def test_rollback_keeps_a_directory_that_was_already_there(self):
        self.mkdir("/var/log/journal", 0o2755)
        self.put("/var/log/journal/old.journal", "古いログ", 0o640)
        before = self.snap()
        self.assert_ok(self.run_script("journal"))
        self.forget_calls()
        self.assert_ok(self.run_script("--rollback", "journal"))
        self.assertEqual(self.snap(), before)
        self.assertNotIn("journalctl --relinquish-var", self.calls())

    def test_rerunning_changes_nothing(self):
        self.assert_ok(self.run_script("journal"))
        self.forget_calls()
        result = self.assert_ok(self.run_script("journal"))
        self.assertIn("すでに適用済みです", result.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_a_failed_restart_is_undone(self):
        before = self.snap()
        self.fail_on("systemctl restart systemd-journald", once=True)
        result = self.run_script("journal")
        self.assertEqual(result.returncode, 1)
        self.assertIn("元に戻しました", result.stdout)
        self.assertEqual(self.snap(), before)

    def test_a_directory_made_before_a_chmod_failure_is_still_removed(self):
        """LS-18: 作ったディレクトリは、chmod の前に記録する。"""
        before = self.snap()
        result = self.run_script("journal", CHMOD_FAIL_SUFFIX="journald.conf.d")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("元に戻しました", result.stdout)
        self.assertFalse(self.exists("/etc/systemd/journald.conf.d"))
        self.assertEqual(self.snap(), before)


EXPECTED_UPGRADES = """// scripts/harden.sh updates が作成（元に戻す: sudo bash scripts/harden.sh --rollback updates）
Unattended-Upgrade::Origins-Pattern {
        "origin=Raspberry Pi Foundation,codename=${distro_codename},label=Raspberry Pi Foundation";
        "origin=Raspbian,codename=${distro_codename},label=Raspbian";
        "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
};
Unattended-Upgrade::Automatic-Reboot "false";
"""
EXPECTED_PERIODIC = """// scripts/harden.sh updates が作成（元に戻す: sudo bash scripts/harden.sh --rollback updates）
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
"""
APT_UPGRADES_CONF = "/etc/apt/apt.conf.d/52campus-chime-unattended-upgrades"
APT_PERIODIC_CONF = "/etc/apt/apt.conf.d/21campus-chime-auto-upgrades"
APT_DAILY_DROPIN = "/etc/systemd/system/apt-daily.timer.d/50-campus-chime-harden.conf"
APT_UPGRADE_DROPIN = "/etc/systemd/system/apt-daily-upgrade.timer.d/50-campus-chime-harden.conf"


class UpdatesStepTest(HardenCase):
    def test_it_builds_the_origins_from_the_real_apt_cache_policy(self):
        """P-09: 値に空白を含む配布元、-security の codename、バックポート（対象外）、重複を含む実物に近い出力。"""
        self.sim_put("apt_policy.out", APT_POLICY + " 500 http://archive.raspberrypi.com/debian bookworm/main all Packages\n"
                                       "     release o=Raspberry Pi Foundation,a=stable,n=bookworm,l=Raspberry Pi Foundation,c=main,b=all\n")
        result = self.assert_ok(self.run_script("updates"))
        self.assertEqual(self.read(APT_UPGRADES_CONF), EXPECTED_UPGRADES)
        self.assertEqual(self.read(APT_PERIODIC_CONF), EXPECTED_PERIODIC)
        self.assertEqual(self.mode(APT_UPGRADES_CONF), 0o644)
        self.assertNotIn("Backports", self.read(APT_UPGRADES_CONF))
        self.assertIn("Raspberry Pi Foundation", result.stdout)
        self.assertIn("apt-config dump の自動更新の対象:", result.stdout)
        self.assertIn("Origins-Pattern", result.stdout.split("apt-config dump の自動更新の対象:")[1])

    def test_the_timers_move_to_the_early_morning_and_stop_catching_up(self):
        self.assert_ok(self.run_script("updates"))
        self.assertEqual(self.read(APT_DAILY_DROPIN), """# scripts/harden.sh updates が作成（元に戻す: sudo bash scripts/harden.sh --rollback updates）
[Timer]
OnCalendar=
OnCalendar=*-*-* 05:00
RandomizedDelaySec=20min
Persistent=false
""")
        self.assertEqual(self.read(APT_UPGRADE_DROPIN), """# scripts/harden.sh updates が作成（元に戻す: sudo bash scripts/harden.sh --rollback updates）
[Timer]
OnCalendar=
OnCalendar=*-*-* 05:40
RandomizedDelaySec=10min
Persistent=false
""")
        calls = self.calls()
        self.assertIn("systemctl daemon-reload", calls)
        self.assertIn("systemctl restart apt-daily.timer apt-daily-upgrade.timer", calls)
        self.assertIn("systemctl list-timers apt-daily* --no-pager", calls)

    def test_it_waits_for_idle_then_installs_unattended_upgrades_and_remembers_it(self):
        result = self.assert_ok(self.run_script("updates"))
        calls = self.calls()
        self.assertLess(next(i for i, call in enumerate(calls) if "--wait-idle" in call),
                        calls.index("apt-get install -y unattended-upgrades"))
        self.assertEqual(self.applied_meta("updates", "installed_by_us"), "1")
        self.assertIn("再起動はしません", result.stdout)
        self.assertIn("9:55〜17:05", result.stdout)

    def test_rollback_purges_the_package_only_if_this_step_installed_it(self):
        before = self.snap()
        self.assert_ok(self.run_script("updates"))
        self.forget_calls()
        self.assert_ok(self.run_script("--rollback", "updates"))
        self.assertIn("apt-get purge -y unattended-upgrades", self.calls())
        self.assertEqual(self.snap(), before)
        self.setUp()
        self.package("unattended-upgrades")
        before = self.snap()
        self.assert_ok(self.run_script("updates"))
        self.assertIsNone(self.applied_meta("updates", "installed_by_us"))
        self.forget_calls()
        self.assert_ok(self.run_script("--rollback", "updates"))
        self.assertEqual(self.calls_with("apt-get"), [])
        self.assertEqual(self.snap(), before)

    def test_the_codename_comes_from_os_release(self):
        self.put("/etc/os-release", OS_RELEASE.format(codename="trixie", id="debian"))
        policy = APT_POLICY.replace("n=bookworm", "n=trixie")
        self.sim_put("apt_policy.out", policy.replace("bookworm", "trixie"))
        self.assert_ok(self.run_script("updates"))
        self.assertEqual(self.read(APT_UPGRADES_CONF), EXPECTED_UPGRADES)
        self.setUp()
        self.sim_put("apt_policy.out", APT_POLICY.replace("n=bookworm,", "n=bookworm-updates,"))
        self.assert_ok(self.run_script("updates"))
        self.assertIn("codename=${distro_codename}-updates,label=Raspbian", self.read(APT_UPGRADES_CONF))

    def test_origins_with_characters_that_would_break_the_apt_syntax_are_skipped(self):
        evil = ("     release o=Evil\"Origin,a=stable,n=bookworm,l=Evil,c=main\n"
                "     release o=Back\\slash,a=stable,n=bookworm,l=X,c=main\n"
                "     release o=Dollar,a=stable,n=bookworm,l=$HOME,c=main\n")
        self.sim_put("apt_policy.out", APT_POLICY + evil)
        self.assert_ok(self.run_script("updates"))
        self.assertEqual(self.read(APT_UPGRADES_CONF), EXPECTED_UPGRADES)

    def test_no_usable_origin_is_refused(self):
        for policy in ("", "Package files:\n 100 /var/lib/dpkg/status\n     release a=now\n",
                       APT_POLICY.replace("n=bookworm", "n=sid")):
            with self.subTest(policy=policy[:30]):
                self.sim_put("apt_policy.out", policy)
                before = self.snap()
                result = self.run_script("updates")
                self.assert_nothing_changed(before, result)
                self.assertIn("自動更新の対象を見つけられません", result.stderr)

    def test_a_failing_apt_cache_or_missing_timers_are_refused(self):
        self.sim_put("apt_policy_rc", "1")
        before = self.snap()
        self.assert_nothing_changed(before, self.run_script("updates"))
        self.setUp()
        os.remove(os.path.join(self.sim, "units", "apt-daily.timer.exists"))
        os.remove(os.path.join(self.sim, "units", "apt-daily.timer.state"))
        result = self.run_script("updates")
        self.assertEqual(result.returncode, 1)
        self.assertIn("apt-daily.timer がありません", result.stderr)

    def test_a_broadcast_in_progress_stops_it_before_any_change(self):
        for code, message in (("1", "放送の時間帯が終わりませんでした"), ("2", "config.json")):
            with self.subTest(code=code):
                self.sim_put("wait_idle_rc", code)
                before = self.snap()
                result = self.run_script("updates")
                self.assert_nothing_changed(before, result)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.calls_with("apt-get"), [])

    def test_an_apt_config_that_does_not_show_the_patterns_is_undone(self):
        before = self.snap()
        self.sim_put("apt_config_rc", "1")
        result = self.run_script("updates")
        self.assertEqual(result.returncode, 1)
        self.assertIn("元に戻しました", result.stdout)
        self.assertEqual(self.snap(), before)

    def test_rerunning_changes_nothing(self):
        self.assert_ok(self.run_script("updates"))
        self.forget_calls()
        result = self.assert_ok(self.run_script("updates"))
        self.assertIn("すでに適用済みです", result.stdout)
        self.assertEqual(self.mutating_calls(), [])
        self.assertEqual(self.calls_with("runuser"), [])


SERVICE_DROPIN = "/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf"
SERVICE_NAME = "campus_chime.service"


def expected_service_dropin(repo, extra_dirs=()):
    dirs = ["{0}/cache".format(repo), "{0}/cache/tts".format(repo), "{0}/assets/generated".format(repo)] + list(extra_dirs)
    lines = ["# scripts/harden.sh service が作成（元に戻す: sudo bash scripts/harden.sh --rollback service）",
             "[Service]", "ProtectSystem=strict", "ProtectHome=read-only",
             "ReadWritePaths=" + " ".join("-" + d for d in dirs),
             "PrivateTmp=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
             "ProtectControlGroups=yes", "ProtectHostname=yes", "ProtectProc=invisible", "RestrictNamespaces=yes",
             "RestrictRealtime=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes",
             "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK", "SystemCallArchitectures=native",
             "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM", "CapabilityBoundingSet=",
             "KeyringMode=private"]
    return "\n".join(lines) + "\n"


class ServiceStepTest(HardenCase):
    def set_config(self, **sections):
        config = json.loads(json.dumps(PRINT_CONFIG))
        for key, value in sections.items():
            config[key].update(value)
        self.sim_put("print_config.json", json.dumps(config))

    def test_the_dropin_has_the_exact_sandbox_and_the_writable_dirs_from_the_config(self):
        result = self.assert_ok(self.run_script("service"))
        self.assertEqual(self.read(SERVICE_DROPIN), expected_service_dropin(self.repo))
        self.assertEqual(self.mode(SERVICE_DROPIN), 0o644)
        for name in ("cache", "cache/tts", "assets/generated"):
            self.assertTrue(os.path.isdir(os.path.join(self.repo, name)), name)
        self.assertEqual(self.calls_with("runuser -u pi -- mkdir -p"),
                         ["runuser -u pi -- mkdir -p {0}/{1}".format(self.repo, name)
                          for name in ("cache", "cache/tts", "assets", "assets/generated")])
        self.assertIn("保護の点数（前）: → Overall exposure level for campus_chime.service: 8.7 EXPOSED", result.stdout)
        self.assertIn("保護の点数（後）: → Overall exposure level for campus_chime.service: 4.2 OK", result.stdout)

    def test_the_devices_clock_and_other_risky_directives_are_left_out(self):
        self.assert_ok(self.run_script("service"))
        text = self.read(SERVICE_DROPIN)
        for word in ("PrivateDevices", "DeviceAllow", "DevicePolicy", "ProtectClock", "MemoryDenyWriteExecute",
                     "ProcSubset", "RemoveIPC", "PrivateUsers"):
            self.assertNotIn(word, text)

    def test_the_script_explains_why_the_directives_are_left_out(self):
        """P-01: ProtectClock は単独なら無害。デバイスの許可リストが付く組み合わせで /dev/snd を塞ぐ。"""
        with open(HARDEN_SH, "r", encoding="utf-8") as handle:
            text = handle.read()
        comment = text[text.index("# 使わないもの（理由つき）:"):text.index("build_sandbox_props() {")]
        for word in ("PrivateDevices", "DeviceAllow", "DevicePolicy", "ProtectClock", "MemoryDenyWriteExecute",
                     "ProcSubset=pid", "/proc/cpuinfo", "RemoveIPC", "PrivateUsers", "/dev/snd"):
            self.assertIn(word, comment)
        self.assertIn("単独なら Bookworm / Trixie で無害", comment)
        self.assertNotIn("ProtectClock は /dev/snd を塞ぐ", comment)

    def test_relative_and_absolute_paths_in_the_config_are_resolved(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        self.set_config(state={"file": "data/state.json", "history_file": elsewhere + "/log/history.jsonl",
                               "pause_file": "data/../data/pause.json"},
                        tts={"cache_dir": elsewhere + "/tts/"}, time_signal={"output_file": "gen.wav"})
        result = self.assert_ok(self.run_script("service"))
        self.assertEqual(self.read(SERVICE_DROPIN).splitlines()[4],
                         "ReadWritePaths=-{0}/data -{1}/log -{1}/tts -{0}".format(self.repo, elsewhere))
        self.assertIn("リポジトリの外です", result.stderr)
        self.assertIn("リポジトリそのものです", result.stderr)
        self.assertTrue(os.path.isdir(elsewhere + "/log"))

    def test_dangerous_writable_dirs_are_refused_and_nothing_is_created(self):
        for value in ("/state.json", "/etc/state.json", "/usr/x/../state.json", "/var/state.json", "/home/state.json",
                      "/home/pi/state.json"):
            with self.subTest(value=value):
                self.set_config(state={"file": value})
                before = self.snap()
                result = self.run_script("service")
                self.assert_nothing_changed(before, result)
                self.assertIn("広すぎます", result.stderr)
                self.assertEqual(self.calls_with("runuser -u pi -- mkdir"), [])

    def test_dry_run_as_the_service_user_reads_the_config_without_runuser(self):
        before = self.snap(times=True)
        result = self.assert_ok(self.run_script("--dry-run", "service", FAKE_UID="1000", FAKE_USER="pi"))
        self.assertIn("ReadWritePaths=-{0}/cache".format(self.repo), result.stdout)
        self.assertEqual(self.calls_with("runuser"), [])
        self.assertEqual(self.snap(times=True), before)

    def test_dry_run_as_somebody_else_says_it_cannot_read_the_config(self):
        result = self.assert_ok(self.run_script("--dry-run", "service", FAKE_UID="1001", FAKE_USER="alice"))
        self.assertIn("設定を読めません", result.stdout)
        self.assertIn("ProtectSystem=strict", result.stdout)
        self.assertEqual(self.calls_with("runuser"), [])

    def test_a_path_that_would_inject_a_line_into_the_unit_is_refused(self):
        self.set_config(state={"file": "x\nExecStartPre=/bin/evil/y"})
        before = self.snap()
        result = self.run_script("service")
        self.assert_nothing_changed(before, result)
        self.assertIn("絶対パスになりません", result.stderr)
        self.assertFalse(self.exists(SERVICE_DROPIN))

    def test_a_directory_with_whitespace_cannot_be_written_into_the_unit(self):
        self.set_config(state={"file": os.path.join(self.tmp, "a b", "state.json")})
        before = self.snap()
        result = self.run_script("service")
        self.assert_nothing_changed(before, result)
        self.assertIn("空白", result.stderr)

    def test_an_unreadable_config_or_missing_files_are_refused(self):
        self.sim_put("print_config_rc", "2")
        before = self.snap()
        result = self.run_script("service")
        self.assert_nothing_changed(before, result)
        self.assertIn("config.json を直して", result.stderr)
        self.setUp()
        os.remove(self.sys_path("/etc/systemd/system/campus_chime.service"))
        result = self.run_script("service")
        self.assertEqual(result.returncode, 1)
        self.assertIn("bash scripts/setup.sh --no-apt", result.stderr)
        self.setUp()
        os.remove(os.path.join(self.repo, "campus_chime.py"))
        result = self.run_script("service")
        self.assertEqual(result.returncode, 1)
        self.assertIn("campus_chime.py が見つかりません", result.stderr)

    def test_the_user_must_be_the_one_the_unit_runs_as(self):
        text = self.read("/etc/systemd/system/campus_chime.service")
        self.put("/etc/systemd/system/campus_chime.service", text.replace("User=pi", "User=chime"))
        before = self.snap()
        result = self.run_script("service")
        self.assert_nothing_changed(before, result)
        self.assertIn("chime で動きます", result.stderr)
        self.assertIn("--user chime", result.stderr)

    def test_it_waits_for_the_broadcast_before_touching_anything(self):
        for code, message in (("1", "放送の時間帯が終わりませんでした"), ("2", "config.json")):
            with self.subTest(code=code):
                self.sim_put("wait_idle_rc", code)
                before = self.snap()
                result = self.run_script("service")
                self.assert_nothing_changed(before, result)
                self.assertIn(message, result.stderr)
                self.assertNotIn("systemctl restart " + SERVICE_NAME, self.calls())

    def test_the_restart_comes_after_the_idle_check_and_a_five_second_wait(self):
        self.assert_ok(self.run_script("service"))
        calls = self.calls()
        last_idle = max(i for i, call in enumerate(calls) if "--wait-idle" in call)
        restart = calls.index("systemctl restart " + SERVICE_NAME)
        self.assertLess(last_idle, calls.index("systemctl daemon-reload"))
        self.assertLess(calls.index("systemctl daemon-reload"), restart)
        self.assertEqual(calls[restart + 1], "sleep 5")
        self.assertIn("systemctl is-active " + SERVICE_NAME, calls[restart:])
        self.assertTrue(any(call.startswith("journalctl -u " + SERVICE_NAME + " --since @") and "-p err -o cat" in call
                            for call in calls))

    def test_unknown_directives_that_systemd_only_warns_about_fail_the_step(self):
        """P-02: systemd-analyze verify は、知らない項目があっても終了コード 0。"""
        for line in ("{0}:3: Unknown key name 'Foo' in section 'Service', ignoring.",
                     "{0}:3: Unknown lvalue 'Foo' in section 'Service'",
                     "{0}:1: Unknown section 'Bar'. Ignoring.",
                     "{0}:4: Failed to parse boolean value, ignoring: maybe",
                     "{0}:4: Invalid syscall filter, ignoring"):
            with self.subTest(line=line):
                self.setUp()
                before = self.snap()
                self.sim_put("analyze_out", line.format("/etc/systemd/system/campus_chime.service.d/50-campus-chime-harden.conf") + "\n")
                result = self.run_script("service")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("元に戻しました", result.stdout)
                self.assertEqual(self.snap(), before)
                self.assertNotIn("systemctl restart " + SERVICE_NAME, self.calls())

    def test_a_warning_about_something_else_does_not_fail_the_step(self):
        self.sim_put("analyze_out", "/etc/systemd/system/other.service:3: Unknown key name 'Foo' in section 'Service'\n")
        self.assert_ok(self.run_script("service"))

    def test_a_failing_verify_that_failed_before_the_change_too_is_only_shown(self):
        self.sim_put("analyze_rc", "1")
        self.sim_put("analyze_out", "campus_chime.service: Command /usr/bin/python3 is not executable\n")
        result = self.assert_ok(self.run_script("service"))
        self.assertIn("is not executable", result.stdout)

    def test_a_failing_verify_that_the_change_caused_fails_the_step(self):
        """変更の前は通っていたのに、変更のあとに失敗するなら、変更が原因。"""
        self.sim_put("analyze_rc_after", "1")
        before = self.snap()
        result = self.run_script("service")
        self.assertEqual(result.returncode, 1)
        self.assertIn("systemd-analyze verify が失敗しました", result.stderr)
        self.assertEqual(self.snap(), before)

    def test_a_service_that_does_not_come_back_is_undone_and_restarted_again(self):
        self.write_file(os.path.join(self.sim, "units", SERVICE_NAME + ".nostart"), "")
        result = self.run_script("service")
        self.assertEqual(result.returncode, 1)
        self.assertIn("起動していません", result.stderr)
        self.assertIn("元に戻しました", result.stdout)
        self.assertFalse(self.exists(SERVICE_DROPIN))
        self.assertFalse(self.exists("/etc/systemd/system/campus_chime.service.d"))
        self.assertEqual(len(self.calls_with("systemctl restart " + SERVICE_NAME)), 2)
        self.assertEqual(self.unit_state(SERVICE_NAME)[0], "enabled")

    def test_errors_in_the_journal_after_the_restart_undo_the_change(self):
        before = self.snap()
        self.sim_put("journal_chime", "pygame.error: ALSA: Couldn't open audio device: Permission denied\n")
        result = self.run_script("service")
        self.assertEqual(result.returncode, 1)
        self.assertIn("エラーを記録しています", result.stderr)
        self.assertIn("元に戻しました", result.stdout)
        self.assertEqual(self.snap(), before)
        self.assertEqual(len(self.calls_with("systemctl restart " + SERVICE_NAME)), 2)

    def test_the_restart_that_fails_is_undone_without_waiting_for_idle_again(self):
        self.fail_on("systemctl restart " + SERVICE_NAME, once=True)
        result = self.run_script("service")
        self.assertEqual(result.returncode, 1)
        self.assertIn("元に戻しました", result.stdout)
        self.assertFalse(self.exists(SERVICE_DROPIN))

    def test_a_directory_made_before_a_chmod_failure_is_removed_on_undo(self):
        """LS-18"""
        before = self.snap()
        result = self.run_script("service", CHMOD_FAIL_SUFFIX="campus_chime.service.d")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.snap(), before)

    def test_rerunning_changes_nothing(self):
        self.assert_ok(self.run_script("service"))
        self.forget_calls()
        result = self.assert_ok(self.run_script("service"))
        self.assertIn("すでに適用済みです", result.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_rollback_removes_the_dropin_and_its_directory_then_restarts_after_idle(self):
        self.mkdir("/etc/systemd/system/other.d")
        before = self.snap()
        self.assert_ok(self.run_script("service"))
        self.forget_calls()
        self.assert_ok(self.run_script("--rollback", "service"))
        self.assertEqual(self.snap(), before)
        calls = self.calls()
        self.assertLess(calls.index("systemctl daemon-reload"), calls.index("systemctl restart " + SERVICE_NAME))
        self.assertTrue(any("--wait-idle" in call for call in calls[:calls.index("systemctl restart " + SERVICE_NAME)]))
        # 作ったディレクトリ（cache など。無かった階層も）は、空なら消える。
        self.assertFalse(os.path.exists(os.path.join(self.repo, "cache")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "assets")))

    def test_rollback_does_not_restart_while_a_broadcast_is_running(self):
        self.assert_ok(self.run_script("service"))
        self.forget_calls()
        self.sim_put("wait_idle_rc", "1")
        result = self.run_script("--rollback", "service")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("systemctl restart " + SERVICE_NAME, self.calls())
        self.assertIn("放送の最中かもしれない", result.stderr)

    def test_the_sound_test_runs_the_noon_chime_in_the_same_sandbox(self):
        self.assert_ok(self.run_script("service"))
        self.forget_calls()
        result = self.assert_ok(self.run_script("--test-sound"))
        run = self.calls_with("systemd-run")
        self.assertEqual(len(run), 1)
        props = [line for line in self.read(SERVICE_DROPIN).splitlines()[2:]]
        expected = ("systemd-run --wait --pipe --collect --quiet --uid=pi --gid=pi -p SupplementaryGroups=audio "
                    "-p WorkingDirectory={0} -p Environment=SDL_AUDIODRIVER=alsa -p Environment=PYTHONUNBUFFERED=1 "
                    "-p NoNewPrivileges=yes {1} /usr/bin/python3 {0}/campus_chime.py --test-hourly 12").format(
                        self.repo, " ".join("-p " + prop for prop in props))
        self.assertEqual(run[0], expected)
        self.assertTrue(any("--wait-idle" in call for call in self.calls()[:self.calls().index(run[0])]))
        self.assertIn("12 時の時報を 1 回", result.stdout)

    def test_the_sound_test_can_follow_the_step_in_one_call(self):
        self.assert_ok(self.run_script("service", "--test-sound"))
        calls = self.calls()
        self.assertLess(calls.index("systemctl restart " + SERVICE_NAME),
                        next(i for i, call in enumerate(calls) if call.startswith("systemd-run")))

    def test_a_failing_sound_test_exits_nonzero_and_points_to_the_rollback(self):
        self.assert_ok(self.run_script("service"))
        self.sim_put("systemd_run_rc", "1")
        result = self.run_script("--test-sound")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--rollback service", result.stderr)

    def test_the_sound_test_alone_needs_the_dropin(self):
        before = self.snap()
        result = self.run_script("--test-sound")
        self.assertEqual(result.returncode, 1)
        self.assertIn("service", result.stderr)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.calls_with("systemd-run"), [])

    def test_dry_run_shows_the_dropin_and_changes_nothing(self):
        before = self.snap(times=True)
        result = self.assert_ok(self.run_script("--dry-run", "service"))
        self.assertIn("ProtectSystem=strict", result.stdout)
        self.assertIn("ReadWritePaths=-" + self.repo + "/cache", result.stdout)
        self.assertEqual(self.snap(times=True), before)
        self.assertEqual(self.mutating_calls(), [])
        self.assertFalse(os.path.isdir(os.path.join(self.repo, "cache")))


EXPECTED_CHRONY_SOURCES = """# scripts/harden.sh ntp が作成（元に戻す: sudo bash scripts/harden.sh --rollback ntp）
server ntp.campus.example iburst
server 10.0.0.9 iburst
"""
CHRONY_SOURCES = "/etc/chrony/sources.d/campus-chime-harden.sources"


class NtpStepTest(HardenCase):
    def chrony_installed(self):
        self.package("chrony")
        self.unit("chrony.service", "enabled", active=True)
        self.package("systemd-timesyncd", installed=False)
        self.unit("systemd-timesyncd.service", "disabled", active=False)

    def test_it_installs_chrony_after_the_idle_check_and_records_the_replacement(self):
        self.put("/etc/chrony/chrony.conf", "pool 2.debian.pool.ntp.org iburst\nmakestep 1 3\nrtcsync\n")
        result = self.assert_ok(self.run_script("ntp"))
        calls = self.calls()
        self.assertLess(next(i for i, call in enumerate(calls) if "--wait-idle" in call), calls.index("apt-get install -y chrony"))
        self.assertEqual(self.applied_meta("ntp", "replaced_timesyncd"), "1")
        self.assertIn("makestep 1 3", result.stdout)
        self.assertNotIn("makestep が", result.stderr)
        self.assertFalse(self.exists(CHRONY_SOURCES))
        self.assertEqual(self.calls_with("chronyc reload"), [])
        self.assertIn("systemctl restart chrony", result.stdout)

    def test_ntp_servers_are_written_as_a_sources_file_and_reloaded(self):
        self.put("/etc/chrony/chrony.conf", "makestep 1 3\n")
        self.assert_ok(self.run_script("ntp", "--ntp-server", "ntp.campus.example", "--ntp-server", "10.0.0.9"))
        self.assertEqual(self.read(CHRONY_SOURCES), EXPECTED_CHRONY_SOURCES)
        self.assertEqual(self.mode(CHRONY_SOURCES), 0o644)
        self.assertIn("chronyc reload sources", self.calls())

    def test_the_servers_timesyncd_used_are_carried_over(self):
        """P-08: --ntp-server が無いとき、timesyncd の NTP= を引き継ぐ（空の代入は、それまでを消す）。"""
        self.put("/etc/systemd/timesyncd.conf", "[Time]\nNTP=old.example\n#NTP=commented.example\nFallbackNTP=fallback.example\n")
        self.put("/etc/systemd/timesyncd.conf.d/10-campus.conf", "[Time]\nNTP=\nNTP=ntp.campus.example 10.0.0.9\n")
        self.put("/etc/systemd/timesyncd.conf.d/20-more.conf", "[Other]\nNTP=ignored.example\n")
        result = self.assert_ok(self.run_script("ntp"))
        self.assertEqual(self.read(CHRONY_SOURCES), EXPECTED_CHRONY_SOURCES)
        self.assertIn("引き継ぎます", result.stdout)

    def test_an_explicit_ntp_server_wins_over_the_carried_over_ones(self):
        self.put("/etc/systemd/timesyncd.conf", "[Time]\nNTP=old.example\n")
        self.assert_ok(self.run_script("ntp", "--ntp-server", "ntp.campus.example"))
        self.assertEqual(self.read(CHRONY_SOURCES).splitlines()[1:], ["server ntp.campus.example iburst"])

    def test_a_bad_server_name_is_refused(self):
        for value in ("bad host", "ntp;rm", "ntp/x", ""):
            with self.subTest(value=value):
                before = self.snap()
                result = self.run_script("ntp", "--ntp-server", value)
                self.assert_nothing_changed(before, result)
                self.assertIn("--ntp-server", result.stderr)

    def test_installing_chrony_must_not_remove_anything_but_timesyncd(self):
        self.sim_put("apt_sim.out", "Remv ntpdate [1:4.2.8]\nInst chrony (4.3)\nRemv systemd-timesyncd [252]\n")
        before = self.snap()
        result = self.run_script("ntp")
        self.assert_nothing_changed(before, result)
        self.assertIn("ntpdate", result.stderr)
        self.assertEqual(self.calls_with("apt-get install"), [])
        self.setUp()
        self.sim_put("apt_sim_rc", "100")
        result = self.run_script("ntp")
        self.assertEqual(result.returncode, 1)
        self.assertIn("試しの実行", result.stderr)

    def test_an_active_chrony_is_left_alone_unless_servers_are_given(self):
        self.chrony_installed()
        before = self.snap()
        result = self.assert_ok(self.run_script("ntp"))
        self.assertIn("chrony がすでに時刻を同期しています", result.stdout)
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.mutating_calls(), [])
        self.assert_ok(self.run_script("ntp", "--ntp-server", "ntp.campus.example", "--ntp-server", "10.0.0.9"))
        self.assertEqual(self.read(CHRONY_SOURCES), EXPECTED_CHRONY_SOURCES)
        self.assertEqual(self.calls_with("apt-get install"), [])
        self.assertNotIn("replaced_timesyncd", slurp(os.path.join(self.state, "applied", "ntp")))
        self.forget_calls()
        self.assert_ok(self.run_script("--rollback", "ntp"))
        self.assertEqual(self.snap(), before)
        self.assertEqual(self.calls_with("apt-get"), [])
        self.assertIn("chronyc reload sources", self.calls())

    def test_not_being_synchronised_yet_is_only_a_warning(self):
        self.sim_put("ntp_sync", "no\n")
        result = self.assert_ok(self.run_script("ntp"))
        self.assertIn("NTPSynchronized", result.stderr)
        self.assertIn("--rollback ntp", result.stderr)
        self.assertEqual(len(self.calls_with("sleep 3")), 30)

    def test_a_makestep_that_is_not_a_count_limit_is_warned_about(self):
        self.put("/etc/chrony/chrony.conf", "makestep 1 -1\n")
        result = self.assert_ok(self.run_script("ntp"))
        self.assertIn("makestep が", result.stderr)

    def test_rollback_reinstalls_timesyncd_after_the_idle_check(self):
        before_units = {n: self.unit_state(n) for n in ("systemd-timesyncd.service",)}
        self.assert_ok(self.run_script("ntp"))
        self.forget_calls()
        self.assert_ok(self.run_script("--rollback", "ntp"))
        calls = self.calls()
        self.assertLess(next(i for i, call in enumerate(calls) if "--wait-idle" in call),
                        calls.index("apt-get install -y systemd-timesyncd"))
        self.assertIn("systemctl enable --now systemd-timesyncd", calls)
        self.assertEqual({n: self.unit_state(n) for n in before_units}, before_units)

    def test_a_chrony_that_does_not_start_is_undone_by_reinstalling_timesyncd(self):
        self.sim_put("chrony_nostart", "")
        result = self.run_script("ntp")
        self.assertEqual(result.returncode, 1)
        self.assertIn("chrony が動いていません", result.stderr)
        self.assertIn("元に戻しました", result.stdout)
        self.assertIn("apt-get install -y systemd-timesyncd", self.calls())

    def test_rerunning_with_the_same_servers_changes_nothing(self):
        self.assert_ok(self.run_script("ntp", "--ntp-server", "ntp.campus.example", "--ntp-server", "10.0.0.9"))
        self.forget_calls()
        result = self.assert_ok(self.run_script("ntp", "--ntp-server", "ntp.campus.example", "--ntp-server", "10.0.0.9"))
        self.assertIn("すでに適用済みです", result.stdout)
        self.assertEqual(self.mutating_calls(), [])


EXPECTED_REVERT_SERVICE = """[Unit]
Description=campus-chime harden.sh の自動復元（確定されなかった変更を元に戻す）

[Service]
Type=oneshot
ExecStart=/bin/bash /var/lib/campus-chime-harden/harden.sh --rollback-pending
Restart=on-failure
RestartSec=60
TimeoutStartSec=2h
"""


def expected_revert_timer(minutes):
    return """[Unit]
Description=campus-chime harden.sh の自動復元の時計（--confirm で止める）

[Timer]
OnActiveSec={0}min
AccuracySec=1s

[Install]
WantedBy=timers.target
""".format(minutes)


class RevertTimerTest(HardenCase):
    def test_the_units_run_the_root_owned_copy_never_the_repo(self):
        """LS-01 と安全: pi が書けるリポジトリの版を、root の時計が実行しない。"""
        self.assert_ok(self.run_script("ssh"))
        self.assertEqual(self.read(REVERT_SERVICE), EXPECTED_REVERT_SERVICE)
        self.assertEqual(self.read(REVERT_TIMER), expected_revert_timer(10))
        self.assertEqual(self.mode(REVERT_SERVICE), 0o644)
        self.assertNotIn(self.repo, self.read(REVERT_SERVICE) + self.read(REVERT_TIMER))
        copy = os.path.join(self.state, "harden.sh")
        with open(copy, "rb") as a, open(self.script, "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(os.stat(copy).st_mode & 0o7777, 0o700)
        self.assertEqual(os.stat(self.state).st_mode & 0o7777, 0o700)
        calls = self.calls()
        self.assertIn("systemctl enable " + REVERT_UNITS, calls)
        self.assertLess(calls.index("systemctl enable " + REVERT_UNITS), calls.index("systemctl restart " + REVERT_UNITS))
        self.assertEqual(self.unit_state(REVERT_UNITS), ("enabled", True))

    def test_revert_after_sets_the_window(self):
        self.assert_ok(self.run_script("ssh", "--revert-after", "7"))
        self.assertEqual(self.read(REVERT_TIMER), expected_revert_timer(7))
        self.assertEqual(self.meta(os.path.join(self.state, "pending"), "window"), "7")

    def test_a_second_step_rearms_the_timer_with_the_longer_window(self):
        """LS-15: あとの手順の短い窓で、前の手順の窓を縮めない。長いほうを使う。"""
        self.assert_ok(self.run_script("ssh", "--revert-after", "30"))
        self.forget_calls()
        self.assert_ok(self.run_script("firewall", "--revert-after", "5"))
        self.assertEqual(self.read(REVERT_TIMER), expected_revert_timer(30))
        self.assertEqual(self.pending(), ["ssh", "firewall"])
        self.assertIn("systemctl restart " + REVERT_UNITS, self.calls())
        self.setUp()
        self.assert_ok(self.run_script("ssh", "--revert-after", "5"))
        self.assert_ok(self.run_script("firewall", "--revert-after", "30"))
        self.assertEqual(self.read(REVERT_TIMER), expected_revert_timer(30))

    def test_both_steps_in_one_call_share_one_timer_and_both_are_pending(self):
        self.assert_ok(self.run_script("firewall", "ssh"))
        self.assertEqual(self.pending(), ["ssh", "firewall"])
        self.assertEqual(len(self.calls_with("systemctl restart " + REVERT_UNITS)), 2)

    def test_steps_that_are_not_risky_do_not_arm_a_timer(self):
        self.assert_ok(self.run_script("services", "journal"))
        self.assertFalse(self.exists(REVERT_TIMER))
        self.assertFalse(self.state_exists("pending"))
        self.assertFalse(self.state_exists("harden.sh"))

    def test_a_failure_in_the_second_step_keeps_the_first_pending_and_says_so(self):
        self.fail_on("nft -f")
        result = self.run_script("ssh", "firewall")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.pending(), ["ssh"])
        self.assertTrue(self.exists(REVERT_TIMER))
        self.assertIn("確定待ちの変更: ssh", result.stdout)
        self.assertIn("前の手順で適用したものは、そのまま残っています", result.stderr)
        self.assertTrue(self.exists(SSH_DROPIN))

    def test_a_failure_in_the_first_step_applies_nothing_after_it(self):
        self.fail_on("sshd -t", once=True)
        result = self.run_script("ssh", "firewall")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.nft_loaded_flag())
        self.assertEqual(self.pending(), [])

    def nft_loaded_flag(self):
        return os.path.exists(os.path.join(self.sim, "nft", "loaded"))

    def test_the_bookkeeping_does_not_depend_on_sort_or_awk(self):
        """LS-06: 状態の記録を awk や sort に頼らない（失敗しても、ssh の記録を落とさない）。"""
        for name in ("awk", "sort"):
            self.write_file(os.path.join(self.stubs, name), "#!/bin/sh\nexit 1\n", 0o755)
        self.assert_ok(self.run_script("ssh"))
        self.assert_ok(self.run_script("firewall"))
        self.assertEqual(self.pending(), ["ssh", "firewall"])
        self.assert_ok(self.run_script("--rollback-pending"))
        self.assertEqual(self.pending(), [])


class ConfirmTest(HardenCase):
    def test_confirming_from_a_new_session_clears_everything(self):
        self.assert_ok(self.run_script("ssh", "firewall"))
        epoch = self.pending_epoch()
        result = self.assert_ok(self.run_confirm(session_start=epoch + 5))
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.state_exists("pending"))
        self.assertFalse(self.exists(REVERT_TIMER) or self.exists(REVERT_SERVICE))
        self.assertFalse(self.state_exists("harden.sh"))
        self.assertIn("systemctl disable --now " + REVERT_UNITS, self.calls())
        self.assertTrue(self.exists(SSH_DROPIN))
        self.assertTrue(self.exists(FW_FILE))
        self.assertIn("確定しました", result.stdout)
        self.assertEqual(self.unit_state(REVERT_UNITS), ("none", False))

    def test_a_session_opened_before_the_change_is_refused(self):
        """この画面が生きているだけでは、変更後も入れる証拠にならない。"""
        self.assert_ok(self.run_script("ssh"))
        epoch = self.pending_epoch()
        before = self.snap()
        mutations = len(self.mutating_calls())
        result = self.run_confirm(session_start=epoch - 600)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("この画面は、変更の前に開いたもの", result.stderr)
        self.assertIn("新しい画面", result.stderr)
        self.assertIn("目安", result.stderr)
        self.assertIn("tmux", result.stderr)
        self.assertEqual(self.pending(), ["ssh"])
        self.assertEqual(self.snap(), before)
        self.assertTrue(self.exists(REVERT_TIMER))
        self.assertEqual(len(self.mutating_calls()), mutations)

    def test_force_confirms_from_an_old_session(self):
        """LS-16"""
        self.assert_ok(self.run_script("ssh"))
        epoch = self.pending_epoch()
        self.assert_ok(self.run_confirm("--force", session_start=epoch - 600))
        self.assertEqual(self.pending(), [])

    def test_the_latest_change_decides_which_session_is_old(self):
        self.assert_ok(self.run_script("ssh"))
        first = self.pending_epoch()
        self.assert_ok(self.run_script("firewall"))
        second = self.pending_epoch()
        self.assertGreaterEqual(second, first)
        result = self.run_confirm(session_start=second - 1)
        self.assertEqual(result.returncode, 1)
        self.assert_ok(self.run_confirm(session_start=second))

    def test_a_session_that_is_not_under_sshd_is_accepted(self):
        """コンソール（物理の画面）や、sshd の下に見えない画面。"""
        self.assert_ok(self.run_script("ssh"))
        self.assert_ok(self.run_confirm(session_start=None))

    def test_the_listener_itself_is_not_taken_for_a_session(self):
        """待ち受けの sshd（親が PID 1）は、接続ごとの子ではないので、時刻を比べない。"""
        self.assert_ok(self.run_script("ssh"))
        self.fake_session(None)
        self.write_file(os.path.join(self.proc, "6000", "status"), "Name:\tsshd\nPPid:\t1\n")
        self.write_file(os.path.join(self.proc, "6000", "stat"), "6000 (sshd) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1 0\n")
        self.assert_ok(self.run_confirm(session_start=None))

    def test_the_login_session_of_trixie_is_recognised_too(self):
        self.assert_ok(self.run_script("ssh"))
        epoch = self.pending_epoch()
        parent = self.fake_session(epoch - 600)
        for pid in (900, 4000, 5000):
            path = os.path.join(self.proc, str(pid), "status")
            text = slurp(path).replace("Name:\tsshd", "Name:\tsshd-session")
            if pid == 900:
                text = text.replace("sshd-session", "sshd")
            self.write_file(path, text)
        del parent
        # fake_session を呼び直さずに、そのまま動かす（run_confirm は呼び直すので、直接動かす）。
        wrapper = os.path.join(self.tmp, "wrapper.sh")
        self.write_file(wrapper, "#!/bin/bash\nmkdir -p \"$CHIME_PROC/$$\"\n"
                                 "printf 'Name:\\tbash\\nPPid:\\t5000\\n' > \"$CHIME_PROC/$$/status\"\nexec bash \"$@\"\n", 0o755)
        result = subprocess.run(["bash", wrapper, self.script, "--confirm"], capture_output=True, text=True,
                                encoding="utf-8", cwd=self.tmp, env=self.environment(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("この画面は、変更の前に開いたもの", result.stderr)

    def test_nothing_pending_is_not_an_error(self):
        result = self.assert_ok(self.run_confirm())
        self.assertIn("確定する変更はありません", result.stdout)
        self.assertEqual(self.mutating_calls(), [])

    def test_a_timer_that_cannot_be_stopped_is_reported_and_the_pending_list_is_empty(self):
        self.assert_ok(self.run_script("ssh"))
        epoch = self.pending_epoch()
        self.fail_on("systemctl disable --now " + REVERT_UNITS)
        result = self.run_confirm(session_start=epoch + 5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("自動復元の時計を止められませんでした", result.stderr)
        self.assertEqual(self.pending(), [])
        self.assertTrue(self.exists(REVERT_TIMER))
        # 時計が動いても、確定待ちが空なら何も戻さない。
        os.remove(os.path.join(self.sim, "fail"))
        self.assert_ok(self.run_script("--rollback-pending"))
        self.assertTrue(self.exists(SSH_DROPIN))
        self.assertFalse(self.exists(REVERT_TIMER))

    def test_a_persist_failure_keeps_the_timer_and_the_pending_firewall(self):
        self.assert_ok(self.run_script("firewall"))
        epoch = self.pending_epoch()
        self.fail_on("systemctl enable campus-chime-firewall.service")
        result = self.run_confirm(session_start=epoch + 5)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.pending(), ["firewall"])
        self.assertTrue(self.exists(REVERT_TIMER))
        self.assertIn("時間が来ると元に戻ります", result.stderr)


class RollbackPendingTest(HardenCase):
    def test_it_undoes_only_the_pending_steps(self):
        self.assert_ok(self.run_script("services"))
        self.assert_ok(self.run_script("ssh", "firewall"))
        services_state = self.unit_state("bluetooth.service")
        self.forget_calls()
        result = self.assert_ok(self.run_script("--rollback-pending"))
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.exists(SSH_DROPIN))
        self.assertFalse(os.path.exists(os.path.join(self.sim, "nft", "loaded")))
        self.assertEqual(self.unit_state("bluetooth.service"), services_state)
        self.assertEqual(self.state_exists("applied/services"), True)
        self.assertFalse(self.exists(REVERT_TIMER) or self.exists(REVERT_SERVICE))
        self.assertFalse(self.state_exists("harden.sh"))
        self.assertIn("systemctl disable --now " + REVERT_UNITS, self.calls())
        self.assertIn("元に戻しました（手順 firewall）", result.stdout)
        self.assertIn("元に戻しました（手順 ssh）", result.stdout)
        self.assertLess(result.stdout.index("手順 firewall"), result.stdout.index("手順 ssh"))

    def test_it_never_waits_for_the_broadcast_and_waits_for_the_lock_up_to_30_minutes(self):
        """LS-01: 戻しは放送に触れない。放送の確認で足止めされず、ロックは 30 分待つ。"""
        self.assert_ok(self.run_script("ssh", "firewall"))
        self.assert_ok(self.run_script("service"))
        self.sim_put("wait_idle_rc", "1")
        self.forget_calls()
        self.assert_ok(self.run_script("--rollback-pending"))
        self.assertIn("flock -w 1800 9", self.calls())
        self.assertFalse(any("--wait-idle" in call for call in self.calls()))
        self.assertEqual(self.calls_with("systemctl restart campus_chime"), [])

    def test_a_held_lock_makes_it_fail_instead_of_giving_up_silently(self):
        self.assert_ok(self.run_script("ssh"))
        self.hold_lock()
        result = self.run_script("--rollback-pending")
        self.assertEqual(result.returncode, 1)
        self.assertIn("終わりませんでした", result.stderr)
        self.assertEqual(self.pending(), ["ssh"])
        self.assertTrue(self.exists(SSH_DROPIN))
        self.assertTrue(self.exists(REVERT_TIMER))

    def test_a_step_that_cannot_be_undone_stays_pending_with_the_timer_and_the_rest_is_undone(self):
        """LS-05: 戻せなかった手順の記録と時計を残す。時計の単位が 60 秒後にやり直す。"""
        self.assert_ok(self.run_script("ssh", "firewall"))
        self.fail_on("systemctl reload ssh")
        result = self.run_script("--rollback-pending")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.pending(), ["ssh"])
        self.assertTrue(self.exists(REVERT_TIMER) and self.exists(REVERT_SERVICE))
        self.assertTrue(self.state_exists("harden.sh"))
        self.assertFalse(os.path.exists(os.path.join(self.sim, "nft", "loaded")))
        self.assertIn("元に戻せませんでした（手順 ssh）", result.stderr)
        self.assertNotIn("元に戻しました（手順 ssh）", result.stdout)
        self.assertIn("元に戻せなかった手順があります: ssh", result.stderr)
        os.remove(os.path.join(self.sim, "fail"))
        self.assert_ok(self.run_script("--rollback-pending"))
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.exists(REVERT_TIMER))
        self.assertFalse(self.exists(SSH_DROPIN))

    def test_a_failing_sshd_check_after_removal_is_a_failure_not_a_silent_clear(self):
        """LS-03: コマンドの失敗を見逃して、確定待ちと時計を消してしまわない。"""
        self.assert_ok(self.run_script("ssh"))
        self.sim_put("sshd_t_rc", "1")
        result = self.run_script("--rollback-pending")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.pending(), ["ssh"])
        self.assertTrue(self.exists(REVERT_TIMER))
        self.assertNotIn("systemctl reload ssh", self.calls_after_last("sshd -t"))

    def calls_after_last(self, call):
        calls = self.calls()
        index = max(i for i, c in enumerate(calls) if c == call)
        return calls[index:]

    def test_the_units_retry_on_failure(self):
        self.assert_ok(self.run_script("ssh"))
        text = self.read(REVERT_SERVICE)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("RestartSec=60", text)
        self.assertIn("Type=oneshot", text)

    def test_nothing_pending_just_clears_a_stray_timer(self):
        self.assert_ok(self.run_script("ssh"))
        epoch = self.pending_epoch()
        self.assert_ok(self.run_confirm(session_start=epoch + 5))
        self.put(REVERT_TIMER, expected_revert_timer(10))
        self.put(REVERT_SERVICE, EXPECTED_REVERT_SERVICE)
        result = self.assert_ok(self.run_script("--rollback-pending"))
        self.assertIn("確定待ちの変更はありません", result.stdout)
        self.assertTrue(self.exists(SSH_DROPIN))
        self.assertFalse(self.exists(REVERT_TIMER))

    def test_it_can_run_from_the_root_copy_in_the_state_directory(self):
        """時計が実行するのは、状態の置き場の写し。写しから動かしても、同じ結果になる。"""
        self.assert_ok(self.run_script("ssh", "firewall"))
        copy = os.path.join(self.state, "harden.sh")
        result = subprocess.run(["bash", copy, "--rollback-pending"], capture_output=True, text=True,
                                encoding="utf-8", env=self.environment(), cwd=self.tmp, stdin=subprocess.DEVNULL,
                                timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.exists(SSH_DROPIN))
        self.assertFalse(os.path.exists(copy))


class RollbackAndListTest(HardenCase):
    def test_rollback_without_steps_undoes_everything_newest_first(self):
        before = self.snap()
        self.assert_ok(self.run_script("services", "journal", "ssh", "firewall"))
        self.assert_ok(self.run_confirm())
        self.forget_calls()
        result = self.assert_ok(self.run_script("--rollback"))
        self.assertEqual(self.snap(), before)
        order = [m.group(1) for m in re.finditer(r"元に戻しました（手順 (\w+)）", result.stdout)]
        self.assertEqual(order, ["firewall", "ssh", "journal", "services"])
        self.assertEqual(self.unit_state("bluetooth.service"), ("enabled", True))

    def test_rollback_with_nothing_applied_says_so(self):
        result = self.assert_ok(self.run_script("--rollback"))
        self.assertIn("元に戻すものはありません", result.stdout)

    def test_the_list_shows_each_state(self):
        result = self.assert_ok(self.run_script())
        for step in ("ssh", "firewall", "sudo", "services", "journal", "updates", "service", "ntp"):
            self.assertRegex(result.stdout, r"\n  {0}\s+未適用 ".format(step))
        self.assert_ok(self.run_script("services", "ssh"))
        result = self.assert_ok(self.run_script())
        self.assertRegex(result.stdout, r"\n  services\s+適用済み ")
        self.assertRegex(result.stdout, r"\n  ssh\s+確定待ち ")
        self.assertIn("確定待ちの変更: ssh", result.stdout)
        self.assert_ok(self.run_confirm())
        self.assertRegex(self.run_script().stdout, r"\n  ssh\s+適用済み ")

    def test_an_interrupted_step_shows_as_partial_and_can_be_rolled_back(self):
        self.assert_ok(self.run_script("journal"))
        path = os.path.join(self.state, "applied", "journal")
        with open(path, "r", encoding="utf-8") as handle:
            kept = [line for line in handle.read().splitlines() if not line.startswith("done=")]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(kept) + "\n")
        self.assertRegex(self.run_script().stdout, r"\n  journal\s+途中")
        before_files = self.exists(JOURNAL_DROPIN)
        self.assertTrue(before_files)
        self.assert_ok(self.run_script("--rollback", "journal"))
        self.assertFalse(self.exists(JOURNAL_DROPIN))

    def test_recommended_is_services_journal_ssh_firewall(self):
        self.assert_ok(self.run_script("recommended"))
        self.assertEqual(self.pending(), ["ssh", "firewall"])
        self.assertTrue(self.exists(JOURNAL_DROPIN))
        self.assertEqual(self.unit_state("bluetooth.service"), ("masked", False))
        self.assertFalse(self.exists(APT_UPGRADES_CONF))

    def test_all_preconditions_are_checked_before_the_first_change(self):
        """recommended で、後ろの手順（ssh）が条件を満たさないなら、前の手順（services）も実行しない。"""
        os.remove(self.sys_path("/home/pi/.ssh/authorized_keys"))
        before = self.snap()
        result = self.run_script("recommended")
        self.assert_nothing_changed(before, result)
        self.assertEqual(self.unit_state("bluetooth.service"), ("enabled", True))
        self.assertFalse(self.exists(JOURNAL_DROPIN))

    def test_the_log_records_the_actions(self):
        self.assert_ok(self.run_script("services"))
        log = self.state_file("log")
        self.assertIn("applied services", log)
        self.assertIn("run systemctl mask bluetooth.service", log)

    def test_a_step_listed_twice_is_applied_once(self):
        self.assert_ok(self.run_script("services", "services"))
        self.assertEqual(len(self.calls_with("systemctl mask bluetooth.service")), 1)

    def test_dry_run_of_every_step_writes_nothing_as_root_or_not(self):
        steps = ("ssh", "firewall", "sudo", "services", "journal", "updates", "service", "ntp")
        for uid in ("0", "1000"):
            with self.subTest(uid=uid):
                before = self.snap(times=True)
                result = self.run_tty("--dry-run", *steps, FAKE_UID=uid)
                self.assert_ok(result)
                self.assertEqual(self.snap(times=True), before)
                self.assertEqual(self.mutating_calls(), [])
                self.assertFalse(os.path.exists(self.state))
                self.assertFalse(os.path.isdir(os.path.join(self.repo, "cache")))


class SafetyTest(HardenCase):
    """スクリプト全体にわたる約束。"""

    def test_each_apply_leaves_the_state_directory_private(self):
        self.assert_ok(self.run_script("ssh"))
        for sub in ("", "applied", "backup", "created"):
            self.assertEqual(os.stat(os.path.join(self.state, sub)).st_mode & 0o7777, 0o700, sub)
        self.assertEqual(os.stat(os.path.join(self.state, "pending")).st_mode & 0o7777, 0o600)

    def test_no_temp_file_is_left_beside_the_files_it_writes(self):
        self.assert_ok(self.run_script("ssh", "firewall", "journal", "updates", "services"))
        self.assert_ok(self.run_confirm())
        for directory, _, files in os.walk(self.root):
            for name in files:
                self.assertNotIn("harden-tmp", name, directory)
        for directory, _, files in os.walk(self.state):
            for name in files:
                self.assertNotIn("harden-tmp", name, directory)
                self.assertFalse(name.endswith(".new"), name)

    def test_the_script_never_loads_a_ruleset_with_a_non_check_nft_outside_the_apply_path(self):
        """nft -f は、確定待ちの時計を張ったあとの 1 回だけ（rollback / confirm では呼ばない）。"""
        self.assert_ok(self.run_script("firewall"))
        self.assertEqual(len(self.calls_with("nft -f")), 1)
        self.assert_ok(self.run_confirm())
        self.assert_ok(self.run_script("--rollback"))
        self.assertEqual(len(self.calls_with("nft -f")), 1)

    def test_a_python_planted_in_the_working_directory_is_not_imported(self):
        """root の python3 は -I で動かす（作業フォルダの ipaddress.py を読まない）。"""
        self.write_file(os.path.join(self.tmp, "ipaddress.py"), "raise SystemExit(99)\n")
        self.sim_put("ss.out", "")
        self.assert_ok(self.run_script("firewall"))


if __name__ == "__main__":
    unittest.main()
