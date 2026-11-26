"""Optional Serein link for newly received messages from one paired WeChat friend."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import secrets
import sys
import tempfile
import time
from http.client import HTTPException
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

import channel_state as st


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


HTTP = build_opener(ProxyHandler({}), NoRedirect())
OFFLINE = (OSError, HTTPException, ValueError, KeyError, TypeError)


def window_id(peer):
    return "wx-private-" + sha256(("wechat-voice-bridge:private:" + peer).encode()).hexdigest()


def endpoint(url):
    dest = urlsplit(url)
    if (dest.scheme != "http" or dest.hostname != "127.0.0.1" or not dest.port
            or dest.netloc != f"127.0.0.1:{dest.port}" or dest.path not in ("", "/") or dest.query or dest.fragment):
        raise ValueError("请填写本机转发到 Serein 连接器的回环地址")
    return f"http://127.0.0.1:{dest.port}"


def link_for(db, peer):
    if not peer or peer.endswith("@chatroom") or peer != st.get(db, "peer"):
        return None
    url = st.profile(db)["memory_url"]
    if not url:
        return None
    try:
        link = json.loads((st.ROOT / "serein-link.json").read_text())
        if (link["url"] == endpoint(url) and link["window"] == window_id(peer)
                and isinstance(link["token"], str) and len(link["token"]) >= 32):
            return link
    except OFFLINE:
        return None


def pair(url):
    url = endpoint(url)
    with st.database() as db:
        db.execute("BEGIN IMMEDIATE")
        peer = st.get(db, "peer")
        if not peer or peer.endswith("@chatroom"):
            raise ValueError("请先在工作台绑定一位微信私聊好友")
        path = st.ROOT / "serein-link.json"
        link = {"url": url, "window": window_id(peer), "token": secrets.token_urlsafe(32)}
        if path.is_file():
            saved = json.loads(path.read_text())
            if saved.get("window") == link["window"] and len(saved.get("token", "")) >= 32:
                link["token"] = saved["token"]
        with tempfile.NamedTemporaryFile("w", dir=st.ROOT, delete=False) as out:
            json.dump(link, out)
        os.replace(out.name, path)
        st.put(db, "profile", {**st.get(db, "profile", {}), "memory_url": url, "memory_id": ""})
    return link


def request(link, route, payload, timeout):
    data = json.dumps({"window": link["window"], **payload}, ensure_ascii=False).encode()
    req = Request(link["url"] + "/" + route, data=data,
                  headers={"Authorization": "Bearer " + link["token"], "Content-Type": "application/json"})
    with HTTP.open(req, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValueError("记忆服务未确认")
    return result


def enroll(db, turn, peer):
    link = link_for(db, peer)
    if link:
        db.execute("INSERT OR IGNORE INTO serein_turns(turn_id,window_id) VALUES(?,?)", (turn, link["window"]))


def recall(turn, peer, words):
    with st.database() as db:
        link = link_for(db, peer)
        if not link or not db.execute("""SELECT 1 FROM serein_turns s JOIN turns t ON t.id=s.turn_id
                WHERE s.turn_id=? AND s.window_id=? AND t.scope='direct' AND t.peer=?""",
                (turn, link["window"], peer)).fetchone():
            return None
        rows = db.execute("""SELECT delivered_ids FROM serein_turns WHERE window_id=? AND delivered_ids IS NOT NULL
                             AND rowid < (SELECT rowid FROM serein_turns WHERE turn_id=?)
                             ORDER BY rowid DESC LIMIT 5""", (link["window"],turn)).fetchall()
    delivered = list(dict.fromkeys(i for row in rows for i in json.loads(row[0])))
    query = "\n".join(w.partition("\n〔引用〕")[0] for w in words if w.strip()).strip()
    note, ids = "", []
    if query:
        try:
            result = request(link, "recall", {"query": query[-16000:], "delivered_ids": delivered}, 11)
            note = result["note"]
            if not isinstance(note, str):
                raise ValueError("记忆材料格式无效")
            if note and isinstance(result.get("ids"), list):
                ids = [i for i in result["ids"] if isinstance(i, str)]
        except OFFLINE as exc:
            note, ids = "", []
            print("Serein private recall:", type(exc).__name__, flush=True)
    return {"window": link["window"], "note": note, "ids": ids}


def used(turn, memory):
    if memory is not None:
        with st.database() as db:
            db.execute("""UPDATE serein_turns SET delivered_ids=?
                          WHERE turn_id=? AND window_id=? AND delivered_ids IS NULL""",
                       (json.dumps(memory["ids"]), turn, memory["window"]))


def events(db, turn):
    def event(key, role, text, stamp):
        return {"source_event_id": sha256(key.encode()).hexdigest(), "role": role, "text": text,
                "created_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat()}
    result = []
    for row in db.execute("""SELECT i.id,i.kind,i.body,i.stamp,v.transcript FROM incoming i
            LEFT JOIN voice_inputs v ON v.message_id=i.id WHERE i.turn=? ORDER BY i.rowid""", (turn["id"],)):
        if row["kind"] == 34:
            text = row["transcript"] or "〔微信语音〕"
        elif row["kind"] == 3:
            text = "〔微信图片〕"
        else:
            text = row["body"]
        if text:
            result.append(event(turn["id"] + ":in:" + row["id"], "user", text, row["stamp"]))
    sent = db.execute("""SELECT id,kind,answer FROM turn_actions WHERE turn_id=? AND status='accepted'
                         AND receipt IS NOT NULL ORDER BY pos""", (turn["id"],)).fetchall()
    if not sent and turn["status"] == "accepted" and turn["receipt"]:
        sent = [turn]
    for row in sent:
        if row["answer"]:
            result.append(event(turn["id"] + ":out:" + row["id"], "assistant", row["answer"], turn["finished"]))
    return result


def flush():
    with st.database() as db:
        peer = st.get(db, "peer")
        link = link_for(db, peer)
        if not link:
            return False
        turn = db.execute("""SELECT t.*,s.finished,s.attempts AS tries,s.next_try AS retry_at
            FROM serein_turns s JOIN turns t ON t.id=s.turn_id
            WHERE s.window_id=? AND t.peer=? AND t.scope='direct' AND s.finished IS NOT NULL
                AND s.archived=0 ORDER BY s.rowid LIMIT 1""", (link["window"], peer)).fetchone()
        if not turn or turn["retry_at"] > time.time():
            return False
        batch = events(db, turn)
    try:
        for start in range(0, len(batch), 8):
            chunk = batch[start:start+8]
            receipt = request(link, "archive", {"events": chunk}, 12)
            if receipt.get("ids") != [e["source_event_id"] for e in chunk]:
                raise ValueError("原话归档回执不完整")
        with st.database() as db:
            db.execute("UPDATE serein_turns SET archived=1 WHERE turn_id=?", (turn["id"],))
    except OFFLINE as exc:
        delay = min(180, 3 * 2 ** min(turn["tries"], 6))
        with st.database() as db:
            db.execute("UPDATE serein_turns SET attempts=attempts+1,next_try=? WHERE turn_id=?",
                       (time.time()+delay, turn["id"]))
        print("Serein private archive:", type(exc).__name__, flush=True)
        return False
    return True


def worker():
    while True:
        try:
            if flush():
                continue
        except Exception as exc:
            print("Serein archive worker:", type(exc).__name__, flush=True)
        time.sleep(3)


def status(db):
    link = link_for(db, st.get(db, "peer"))
    if not link:
        return {"paired": False, "pending": 0, "archived": 0}
    pending, archived = db.execute("""SELECT count(*) FILTER(WHERE finished IS NOT NULL AND archived=0),
        count(*) FILTER(WHERE archived=1) FROM serein_turns WHERE window_id=?""", (link["window"],)).fetchone()
    return {"paired": True, "pending": pending, "archived": archived}


def check():
    with st.database() as db:
        link = link_for(db, st.get(db, "peer"))
    if not link:
        raise ValueError("请先配对本机 Serein 连接器")
    try:
        return request(link, "health", {}, 4)
    except OFFLINE:
        raise ValueError("无法连接本机 Serein，请检查电脑和记忆转发") from None


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "pair" or sys.stdout.isatty():
        raise SystemExit("用法：serein_memory.py pair http://127.0.0.1:端口 > 私有配对文件")
    print(json.dumps(pair(sys.argv[2])))
