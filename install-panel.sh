#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
prefix=/opt/wechat-vps-case
dest=$prefix/panel
unit=wechat-vps-panel.service
unit_file=/etc/systemd/system/$unit
if [[ $(id -u) != 0 || ! -x $prefix/bin/wx ]]; then
    echo "请先以 root 执行 install-agent.sh，再安装管理页。" >&2
    exit 1
fi
exec 9>/run/lock/wechat-voice-bridge-install.lock
flock -n 9 || { echo "已有安装正在运行。" >&2; exit 1; }
for target in "$dest" "$unit_file" /var/lib/wechat-vps-panel /var/lib/private/wechat-vps-panel; do
    if [[ -e "$target" || -L "$target" ]]; then
        echo "Already exists: $target" >&2
        exit 1
    fi
done
if [[ -n $(ss -H -lnt 'sport = :28222') ]]; then
    echo "TCP 28222 is occupied." >&2
    exit 1
fi
python=/usr/bin/python3
"$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11 或更新版本")'
stage=$(mktemp -d "$prefix/.panel-install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
"$python" -m venv "$stage/bootstrap"
install -d -m 0755 "$stage/panel"
(
    umask 022
    "$stage/bootstrap/bin/python" -m pip --disable-pip-version-check install \
        --no-input --no-cache-dir --no-compile --only-binary=:all: \
        --target "$stage/panel/deps" -r requirements.txt
)
install -m 0644 channel_state.py channel_worker.py group_chats.py wechat_case.py deepseek_case.py providers.py pcm_audio.py spoken_reply.py reply_actions.py sensevoice_local.py voice_iat.py account_stickers.py call_channel.py worldbooks.py conversation_prompt.py panel.py index.html panel.js panel.css "$stage/panel/"
(
    cd "$stage/panel"
    PYTHONPATH="$PWD/deps" "$python" -B -c 'import aiohttp, PIL.Image, websockets.asyncio.client, panel'
)
"$python" provision.py
mv -T -- "$stage/panel" "$dest"
install -m 0644 "$unit" "$unit_file"
systemctl daemon-reload
systemctl enable --now "$unit"
for attempt in {1..15}; do
    if curl --noproxy '*' -fsS --max-time 3 -o /dev/null http://127.0.0.1:28222/api/state; then
        echo "WeChat channel panel ready on 127.0.0.1:28222"
        exit 0
    fi
    sleep 1
done
echo "管理页未就绪。用 journalctl -u $unit -n 30 查看启动日志。" >&2
exit 1
