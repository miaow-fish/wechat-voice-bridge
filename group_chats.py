"""Explicit group subscriptions for the logged-in WeChat account."""
import hashlib
from functools import lru_cache
import time
from urllib.error import HTTPError, URLError

import channel_state as st
from wechat_case import esc, wx


def list_page(cursor=""):
    path = "/groups?limit=60"
    page = wx(path + ("&cursor=" + esc(cursor) if cursor else ""))
    items = [
        {"id": item["id"], "name": item.get("displayName") or item["id"]}
        for item in page.get("items", [])
        if item.get("id") and not item.get("deleted") and item.get("isInGroup") is not False
    ]
    return {"items": items, "nextCursor": page.get("nextCursor")}


def configure(peer, mode):
    if not isinstance(peer, str) or not peer.endswith("@chatroom") or len(peer)>256:
        raise ValueError("请选择微信账号已加入的群")
    if mode not in ("off", "auto", "mention"):
        raise ValueError("请选择有效的群聊发言方式")
    if mode != "off":
        if not wx().get("authenticated"):
            raise ValueError("请先登录助手微信")
        group = wx("/groups/" + esc(peer))
        if group.get("id") != peer or group.get("deleted") or group.get("isInGroup") is False:
            raise ValueError("助手号当前无法使用这个群聊")
    with st.database() as db:
        db.execute("BEGIN IMMEDIATE")
        routes = st.get(db, "group_routes", {})
        previous = routes.get(peer, {})
        if previous.get("mode", "off") != mode:
            db.execute("""UPDATE turns SET status='disabled', phase='group_setting', error=NULL
                          WHERE scope='group' AND peer=? AND status IN ('collecting','sealed','ready')""", (peer,))
            if mode == "off":
                routes.pop(peer, None)
            else:
                routes[peer] = {"name": group.get("displayName") or peer, "mode": mode, "since": time.time()}
            st.put(db, "group_routes", routes)
    return {"message": "群聊设置已保存"}


@lru_cache(maxsize=512)
def speaker_name(peer, sender):
    try:
        reply = wx("/groups/" + esc(peer) + "/members/" + esc(sender))
        member = reply.get("member") or {}
        if member.get("username", member.get("id")) == sender:
            for key in ("roomDisplayName", "nickname", "displayName"):
                name = member.get(key)
                if isinstance(name, str) and name.strip() and name != sender:
                    return " ".join(name.split())[:60]
    except (HTTPError, URLError, TimeoutError):
        pass
    return "群友 " + hashlib.sha256((peer+"\n"+sender).encode()).hexdigest()[:6]
