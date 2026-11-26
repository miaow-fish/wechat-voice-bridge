"""Explicit group subscriptions for the logged-in WeChat account."""
import hashlib
from functools import lru_cache
import time
from urllib.error import HTTPError, URLError

import channel_state as st
from wechat_case import esc, wx


def list_page(cursor=""):
    own = wx().get("ownUsername")
    if not own:
        raise ValueError("请先登录助手微信")
    path = "/groups?limit=10"
    page = wx(path + ("&cursor=" + esc(cursor) if cursor else ""))
    items, unverified = [], []
    for item in page.get("items", []):
        room = item.get("id")
        if not room or item.get("deleted"):
            continue
        entry = {"id": room, "name": item.get("displayName") or room}
        try:
            roster = wx("/groups/" + esc(room) + "/lookup?version=0")
            members = roster.get("items")
            if roster.get("id") == room and isinstance(members,list):
                if not members and roster.get("version") == 0:
                    # A successful empty zero-version roster has no room to display.
                    # Re-check on the next refresh in case the account rejoins.
                    continue
                if any(member.get("id") == own for member in members):
                    items.append(entry)
                    continue
        except (URLError, TimeoutError, ValueError):
            pass
        unverified.append(entry)
    return {"items": items, "unverified": unverified, "nextCursor": page.get("nextCursor")}


def refresh_page():
    # The catalog is incremental and retains old rooms. Check membership even
    # when catalog sync is partial so a stale row cannot prove membership.
    operation = wx("/group-sync", {})
    deadline = time.monotonic() + 12
    while operation.get("state") in ("pending", "queued", "running", "accepted") and time.monotonic() < deadline:
        time.sleep(.4)
        operation = wx("/operations/" + esc(operation["id"]))
    page = list_page()
    page["catalogComplete"] = operation.get("state") == "succeeded" and operation.get("result", {}).get("complete") is True
    return page


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
