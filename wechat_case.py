"""Use the documented, account-scoped Agent WeChat HTTP and CLI interfaces."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener

from deepseek_case import NoRedirect


HOST = "http://127.0.0.1:28220"
ACCOUNT = "channel-test"
SCOPE = "/v1/accounts/" + ACCOUNT
WX = "/opt/wechat-vps-case/bin/wx"
TOKEN = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/etc/wechat-vps-case")) / "wechat.token"
HTTP = build_opener(ProxyHandler({}), NoRedirect())
LIMIT = 8 * 1024 * 1024


def esc(value):
    return quote(str(value), safe="")


def wx(path="", data=None, idem=None, *, binary=False):
    headers = {"Authorization": "Bearer " + TOKEN.read_text().strip(), "Content-Type": "application/json"}
    if idem:
        headers["Idempotency-Key"] = idem
    req = Request(HOST + SCOPE + path, headers=headers,
                  data=None if data is None else json.dumps(data).encode())
    with HTTP.open(req, timeout=20) as response:
        result = response.read(LIMIT + 1)
    if len(result) > LIMIT:
        raise ValueError("微信数据超过单次下载上限")
    return result if binary else json.loads(result)


def cli(*args):
    proc = subprocess.run([WX, "--server", HOST, "--account", ACCOUNT, "--json", "--token-stdin", *map(str, args)],
                          input=TOKEN.read_bytes(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=55)
    if proc.returncode:
        raise ValueError("微信媒体提交失败")
    return json.loads(proc.stdout)


def image_type(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if raw.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if raw.startswith((b"GIF87a",b"GIF89a")): return "image/gif"
    if raw[:4]==b"RIFF" and raw[8:12]==b"WEBP": return "image/webp"
    raise ValueError("微信图片格式暂不支持")


def media(message_id, qualities):
    ref = "/messages/" + esc(message_id)
    assets = [a for a in wx(ref + "/media").get("items", []) if a.get("messageId") == message_id and a.get("id")]
    for quality in qualities:
        asset = next((a for a in assets if a.get("variant") == quality
                      and a.get("state") not in ("unsupported", "expired", "recalled")), None)
        if not asset:
            continue
        if max(asset.get("byteLength") or 0, asset.get("advertisedByteLength") or 0) > LIMIT:
            continue
        path = "/media/" + esc(asset["id"])
        if asset.get("state") != "available":
            job = wx(ref + "/media-transfers", {"messageId": message_id, "assetId": asset["id"], "quality": quality})
            until = time.monotonic() + 32
            while time.monotonic() < until:
                asset = wx(path)
                if asset.get("state") == "available" or asset.get("state") in ("unsupported", "expired", "recalled"):
                    break
                if job.get("id") and wx("/jobs/" + esc(job["id"])).get("state") in ("failed","expired","cancelled"):
                    break
                time.sleep(.5)
        if asset.get("state") == "available":
            data = wx(path + "/content", binary=True)
            if not data or (asset.get("sha256") and hashlib.sha256(data).hexdigest() != asset["sha256"]):
                raise ValueError("微信媒体校验失败")
            return data
    raise ValueError("微信没有可用的原媒体")
