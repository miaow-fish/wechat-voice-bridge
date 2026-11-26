#!/usr/bin/env bash
# Install the verified upstream Linux client as a loopback-only systemd service.
set -euo pipefail
umask 077

prefix=/opt/wechat-vps-case
state=/var/lib/wechat-vps-case
unit=wechat-vps-case.service
unit_path=/etc/systemd/system/$unit
version=3.1.0
digest=36398c555b4c785f832f9124a1c713552396d7bccef52fa8a87c64bcdb2dad12
url="https://github.com/thisnick/agent-wechat/releases/download/v$version/wx-v$version-linux-x64-gnu"

if [[ $(id -u) != 0 || $(uname -sm) != "Linux x86_64" ]]; then
    echo "请在 x86-64 Linux VPS 上用 sudo 运行此脚本。" >&2
    exit 1
fi
for command in systemctl curl sha256sum ffmpeg ffprobe ss flock; do
    command -v "$command" >/dev/null || { echo "缺少命令 $command，请先安装 README 中的系统依赖。" >&2; exit 1; }
done
exec 9>/run/lock/wechat-voice-bridge-install.lock
flock -n 9 || { echo "已有安装正在运行。" >&2; exit 1; }
for target in "$prefix" "$state" "$unit_path" /var/lib/private/wechat-vps-case; do
    if [[ -e "$target" || -L "$target" ]]; then
        echo "Already exists: $target; refusing to replace it." >&2
        exit 1
    fi
done
if [[ -n $(ss -H -lnt 'sport = :28220') ]]; then
    echo "TCP 28220 is occupied." >&2
    exit 1
fi

scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
if [[ -n ${AGENT_WECHAT_NATIVE_BINARY:-} ]]; then
    install -m 0700 -- "$AGENT_WECHAT_NATIVE_BINARY" "$scratch/wx"
else
    curl --proto '=https' --proto-redir '=https' --fail --silent --show-error --location \
        --retry 2 --retry-all-errors --connect-timeout 10 --max-time 120 --output "$scratch/wx" "$url"
fi
echo "$digest  $scratch/wx" | sha256sum --check --status
chmod 0755 "$scratch/wx"
"$scratch/wx" --json version
cat >"$scratch/$unit" <<'UNIT'
[Unit]
Description=WeChat VPS case, native account service
Wants=network-online.target
After=network-online.target

[Service]
DynamicUser=yes
StateDirectory=wechat-vps-case
StateDirectoryMode=0700
WorkingDirectory=/var/lib/wechat-vps-case
Environment=HOME=/var/lib/wechat-vps-case
Environment=XDG_CONFIG_HOME=/var/lib/wechat-vps-case/config
ExecStartPre=/usr/bin/install -d -m 0700 /var/lib/wechat-vps-case/hub
ExecStart=/opt/wechat-vps-case/bin/wx serve --data-dir /var/lib/wechat-vps-case/hub --listen 127.0.0.1:28220
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
Nice=5
MemoryHigh=160M
MemoryMax=256M

[Install]
WantedBy=multi-user.target
UNIT
install -d -m 0755 "$prefix" "$prefix/bin"
install -m 0755 "$scratch/wx" "$prefix/bin/wx"
install -m 0644 "$scratch/$unit" "$unit_path"
systemctl daemon-reload
systemctl enable --now "$unit"
for attempt in {1..20}; do
    if curl --noproxy '*' -fsS --max-time 2 --output /dev/null http://127.0.0.1:28220/health; then
        echo "WeChat account service ready on 127.0.0.1:28220"
        exit 0
    fi
    sleep 1
done
exit 1
