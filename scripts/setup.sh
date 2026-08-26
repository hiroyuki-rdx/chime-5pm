#!/usr/bin/env bash
#
# Raspberry Pi OS Lite 上でキャンパス時報システムを導入する。
# 何度実行しても同じ結果になる（冪等）よう作ってある。
#
#   bash scripts/setup.sh              # 依存導入 → 音源生成 → サービス登録
#   bash scripts/setup.sh --no-apt     # apt を実行しない
#   bash scripts/setup.sh --no-service # systemd への登録を行わない
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="campus_chime.service"
EXPECTED_DIR="/home/pi/campus-chime"

DO_APT=1
DO_SERVICE=1
for arg in "$@"; do
  case "$arg" in
    --no-apt) DO_APT=0 ;;
    --no-service) DO_SERVICE=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "不明なオプション: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m警告: %s\033[0m\n' "$*" >&2; }

log "設置パスの確認"
echo "リポジトリ: ${REPO_DIR}"
if [ "${REPO_DIR}" != "${EXPECTED_DIR}" ]; then
  warn "systemd ユニットは ${EXPECTED_DIR} を前提にしています。"
  warn "別の場所で運用する場合は ${SERVICE_NAME} の WorkingDirectory と ExecStart を書き換えてください。"
fi

if [ "${DO_APT}" -eq 1 ]; then
  log "依存パッケージの導入"
  sudo apt-get update
  sudo apt-get install -y \
    python3 \
    python3-pygame \
    open-jtalk \
    open-jtalk-mecab-naist-jdic \
    hts-voice-nitech-jp-atr503-m001 \
    alsa-utils \
    mpg123 \
    git
else
  echo "--no-apt が指定されたため、パッケージ導入を省略します。"
fi

log "タイムゾーンと時刻同期の確認"
current_tz="$(timedatectl show --property=Timezone --value 2>/dev/null || echo unknown)"
echo "タイムゾーン: ${current_tz}"
if [ "${current_tz}" != "Asia/Tokyo" ]; then
  warn "タイムゾーンが Asia/Tokyo ではありません。次のコマンドで設定してください:"
  warn "  sudo timedatectl set-timezone Asia/Tokyo"
fi
if ! timedatectl show --property=NTPSynchronized --value 2>/dev/null | grep -q '^yes$'; then
  warn "NTP 同期がまだ完了していません（本機は RTC 非搭載です）。"
  warn "  timedatectl  で 'System clock synchronized: yes' になることを確認してください。"
fi

log "現地設定ファイルの用意"
if [ -f "${REPO_DIR}/config.json" ]; then
  echo "config.json は既にあります（上書きしません）。"
else
  cp "${REPO_DIR}/config.example.json" "${REPO_DIR}/config.json"
  echo "config.example.json から config.json を作成しました。"
  echo "地域や時刻を変える場合は config.json を編集してください（Git 管理外です）。"
fi

log "時報音と定型文音声の生成"
if ! python3 "${REPO_DIR}/campus_chime.py" --generate-assets; then
  warn "音声合成に失敗しました。時報音（ポ・ポ・ポ・ポーン）は鳴りますが、読み上げが出ません。"
  warn "次を実行してから、もう一度 --generate-assets を試してください:"
  warn "  sudo apt install -y open-jtalk open-jtalk-mecab-naist-jdic hts-voice-nitech-jp-atr503-m001"
fi

log "動作確認（音は鳴りません）"
python3 "${REPO_DIR}/campus_chime.py" --schedule 5

if [ "${DO_SERVICE}" -eq 1 ]; then
  log "systemd への登録"
  sudo cp "${REPO_DIR}/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}"
  sudo systemctl daemon-reload
  sudo systemctl enable "${SERVICE_NAME}"
  sudo systemctl restart "${SERVICE_NAME}"
  sleep 2
  sudo systemctl status "${SERVICE_NAME}" --no-pager || true
else
  echo "--no-service が指定されたため、systemd への登録を省略します。"
fi

log "完了"
cat <<'MESSAGE'
次の手順で実際に音が出るか確認してください。

  python3 campus_chime.py --test-hourly    # 時報（ポ・ポ・ポ・ポーン＋読み上げ）
  python3 campus_chime.py --test           # 閉館放送（アナウンス＋蛍の光）

音が出ない場合は docs/SETUP.md の「音が鳴らないとき」を参照してください。
ログの確認:

  journalctl -u campus_chime.service -f
MESSAGE
