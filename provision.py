"""Provision one fresh local WeChat account slot and its scoped application credential on the VPS."""
import json
import os
from pathlib import Path
from urllib.request import build_opener, ProxyHandler, Request

ACCOUNT = "channel-test"
CONFIG = Path("/etc/wechat-vps-case")


def main():
    os.umask(0o077)
    owner = Path("/var/lib/wechat-vps-case/hub/api-token").read_text().strip()
    http = build_opener(ProxyHandler({}))
    def wx(path, data=None):
        req = Request("http://127.0.0.1:28220/v1"+path,headers={"Authorization":"Bearer "+owner,
            "Content-Type":"application/json"},data=json.dumps(data).encode() if data else None)
        with http.open(req,timeout=8) as result:
            return json.load(result)
    slots = wx("/accounts")
    if not any(a["id"]==ACCOUNT for a in slots["items"]):
        wx("/accounts",{"id":ACCOUNT,"alias":ACCOUNT})
    CONFIG.mkdir(mode=0o700,exist_ok=True)
    path = CONFIG/"wechat.token"
    if not path.exists():
        result=wx("/tokens",{"name":"WeChat channel case","accounts":[ACCOUNT],"scopes":["read","send","media","login","calls"]})
        with path.open("x") as f:
            f.write(result["token"]+"\n")
    print("微信测试账号槽位及其独立凭据已就绪。")


if __name__=="__main__":
    main()
