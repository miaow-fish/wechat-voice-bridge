#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
base=/opt/wechat-vps-case
unit=wechat-vps-sensevoice
[[ $(id -u) == 0 && -f $base/panel/sensevoice_local.py ]]
for target in "$base/sensevoice" "/etc/systemd/system/$unit.socket" "/etc/systemd/system/$unit@.service" "/run/$unit.sock"; do
    [[ ! -e $target && ! -L $target ]] || { echo "Already exists: $target" >&2; exit 1; }
done
getent group wechat-vps-panel >/dev/null
stage=$(mktemp -d "$base/sensevoice.XXXXXXXX")
weights_sha256=c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51
vocabulary_sha256=f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc
origin=https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main
for file in model.int8.onnx tokens.txt; do
    if [[ -n ${SENSEVOICE_FILES:-} ]]; then cp --reflink=auto -- "$SENSEVOICE_FILES/$file" "$stage/$file"
    else curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' "$origin/$file" -o "$stage/$file"
    fi
done
echo "$weights_sha256  $stage/model.int8.onnx" | sha256sum -c --status
echo "$vocabulary_sha256  $stage/tokens.txt" | sha256sum -c --status
python3 -m pip --disable-pip-version-check install --no-compile --no-cache-dir --only-binary=:all: --target "$stage/runtime" 'sherpa-onnx==2.0.3'
if ! PYTHONPATH="$stage/runtime" python3 -B -c "import numpy" 2>/dev/null; then
    python3 -m pip --disable-pip-version-check install --no-compile --no-cache-dir --only-binary=:all: --target "$stage/runtime" numpy
fi
chmod 0755 "$stage"
chmod 0644 "$stage/model.int8.onnx" "$stage/tokens.txt"
chmod -R a+rX "$stage/runtime"
ln -s "$stage" "$base/sensevoice"
install -m 0644 "$unit.socket" "$unit@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "$unit.socket"
