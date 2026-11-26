"""Per-instance control settings and durable WeChat delivery records."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3


ROOT = Path(os.environ.get("WECHAT_CASE_STATE", "/var/lib/wechat-vps-panel"))
MAX_HISTORY_TURNS = 100
DEFAULT = {
    "name": "微信私聊", "persona": "", "worldbook": "", "memory_url": "",
    "persona_id":"", "worldbook_id":"", "memory_id":"",
    "chat_service":"deepseek", "vision_service":"deepseek",
    "chat_model": "deepseek-flash", "vision_model": "deepseek-flash",
    "asr": "xfyun", "tts": "elevenlabs", "voice": "",
    "asr_model":"", "tts_model":"", "asr_language":"zh_cn", "asr_accent":"mandarin",
    "note_rate":"16000", "call_cue":"off", "call_barge_in":True,
    "sticker_vision":"off",
    "merge_messages": True, "merge_wait_seconds": 7,
    "split_bubbles": True, "history_turns": 30, "pinned_context": "", "private_context": "",
    "tone_service":"off", "tone_model":"", "tone_format":"input_audio",
}


@contextmanager
def database():
    db = sqlite3.connect(ROOT / "channel.sqlite", timeout=8)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def init():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with database() as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incoming(
                id TEXT PRIMARY KEY, peer TEXT NOT NULL, kind INTEGER NOT NULL,
                body TEXT NOT NULL, stamp REAL NOT NULL, turn TEXT);
            CREATE TABLE IF NOT EXISTS group_inputs(
                message_id TEXT PRIMARY KEY REFERENCES incoming(id),
                speaker TEXT NOT NULL, mentioned INTEGER NOT NULL DEFAULT 0, sender_key TEXT);
            CREATE TABLE IF NOT EXISTS turns(
                id TEXT PRIMARY KEY, peer TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
                kind TEXT, input TEXT, answer TEXT, wire TEXT, send_id TEXT, receipt TEXT,
                phase TEXT, error TEXT, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS voice_inputs(
                message_id TEXT PRIMARY KEY, transcript TEXT NOT NULL, provider TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS voice_tones(
                message_id TEXT PRIMARY KEY REFERENCES voice_inputs(message_id), tone TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS turn_actions(
                id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), pos INTEGER NOT NULL,
                kind TEXT NOT NULL, answer TEXT NOT NULL, requested TEXT, wire TEXT, send_id TEXT, receipt TEXT,
                phase TEXT, status TEXT NOT NULL DEFAULT 'ready', error TEXT, UNIQUE(turn_id,pos));
            CREATE TABLE IF NOT EXISTS serein_turns(
                turn_id TEXT PRIMARY KEY REFERENCES turns(id), window_id TEXT NOT NULL,
                delivered_ids TEXT, finished REAL, archived INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS serein_outbox ON serein_turns(window_id,archived);
            CREATE TRIGGER IF NOT EXISTS serein_finished AFTER UPDATE OF status ON turns
                WHEN NEW.status='accepted' OR (NEW.status='failed' AND EXISTS
                    (SELECT 1 FROM turn_actions a WHERE a.turn_id=NEW.id AND a.status='accepted'))
                BEGIN
                    UPDATE serein_turns SET finished=COALESCE(finished, (julianday('now')-2440587.5)*86400.0)
                    WHERE turn_id=NEW.id;
                END;
            CREATE TABLE IF NOT EXISTS call_lines(
                id INTEGER PRIMARY KEY, peer TEXT NOT NULL, stamp REAL NOT NULL, words TEXT NOT NULL, reply TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS call_lines_peer_stamp ON call_lines(peer,stamp);
            UPDATE turns SET status='call_unknown',error='服务重启前已请求过拨号，未重复拨打'
                WHERE status='call_requested';
            INSERT OR IGNORE INTO voice_inputs(message_id,transcript,provider)
                SELECT i.id,t.input,'' FROM incoming i JOIN turns t ON t.id=i.turn
                WHERE i.kind=34 AND t.input IS NOT NULL AND length(t.input)>0
                  AND (SELECT count(*) FROM incoming other WHERE other.turn=i.turn)=1;
        """)
        db.execute("BEGIN IMMEDIATE")
        if "scope" not in {r["name"] for r in db.execute("PRAGMA table_info(turns)")}:
            db.execute("ALTER TABLE turns ADD COLUMN scope TEXT NOT NULL DEFAULT 'direct'")
        if "sender_key" not in {r["name"] for r in db.execute("PRAGMA table_info(group_inputs)")}:
            db.execute("ALTER TABLE group_inputs ADD COLUMN sender_key TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS turns_pending ON turns(scope,peer,status)")
        db.execute("CREATE INDEX IF NOT EXISTS incoming_turn ON incoming(turn)")


def get(db, key, default=None):
    row = db.execute("SELECT v FROM config WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(db, key, value):
    db.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))


def group_sender_key(room, sender):
    """Match a WeChat account within one group without storing its raw ID there."""
    return sha256((room+"\n"+sender).encode()).hexdigest() if sender else ""


def speaker_line(row, bound_key=None):
    if "speaker" not in row.keys() or not row["speaker"]:
        return ""
    identity = "〔已绑定的私聊好友〕" if bound_key and row["sender_key"] == bound_key else ""
    return identity + "〔群成员 " + row["speaker"] + (" · @了你" if row["mentioned"] else "") + "〕"


def merge_window(settings):
    if settings.get("merge_messages", DEFAULT["merge_messages"]):
        return settings.get("merge_wait_seconds", DEFAULT["merge_wait_seconds"])
    return 0


def profile(db):
    values = {**DEFAULT, **get(db, "profile", {})}
    library = get(db,"library",{})
    for kind,field in (("persona","persona"),("worldbook","worldbook"),("memory","memory_url")):
        item = library.get(values[kind+"_id"])
        if item and item["kind"]==kind:
            values[field]=item["body"]
    return values

def delivered_reply(db, turn, *, for_call):
    """Render accepted actions for the model independently of the operator's transcript."""
    import reply_actions as actions
    if turn["status"] == "silent":
        return "", ""
    sent = db.execute("""SELECT id,kind,answer FROM turn_actions
                         WHERE turn_id=? AND status='accepted' ORDER BY pos""",(turn["id"],)).fetchall()
    if not sent and turn["status"] != "accepted":
        return "", ""
    words, receipts = [], []
    for item in sent or [turn]:
        kind, body = item["kind"], item["answer"] or ""
        if kind == "sticker":
            sticker = get(db, "sticker:"+item["id"], {}) or {}
            name = " · ".join(str(sticker[k]) for k in ("group","label") if sticker.get(k))
            if not name and body.startswith("〔") and body.endswith("〕") and " · " in body:
                name = body[1:-1]
            name = " ".join(name.split())[:120]
            receipts.append("发出表情「"+name+"」" if name else "发出表情")
            if not for_call:
                words.append(actions.STICKER)
        elif kind == "voice":
            if not sent:
                body = body.removeprefix(actions.VOICE_LABEL).strip()
            if body:
                words.append(body if for_call else actions.VOICE+"\n"+body.replace("\n"," "))
        else:
            plain, mislabeled = actions.without_display_labels(body)
            if plain.strip():
                words.append(plain)
            if mislabeled and "媒体标注误发成了文字" not in receipts:
                receipts.append("媒体标注误发成了文字")
    return "\n".join(words), "、".join(receipts)


def recent_dialogue(peer,before=None,*,for_call=False,bound_key=None):
    """Keep each conversation's speakers, including group turns the assistant sat out."""
    import time
    with database() as db:
        limit=get(db,"profile",{}).get("history_turns",DEFAULT["history_turns"])
        if not limit: return []
        group = peer.endswith("@chatroom")
        if group and bound_key is None:
            bound_key=group_sender_key(peer,get(db,"peer"))
        until=time.time()
        if before:
            row=db.execute("SELECT MIN(stamp) FROM incoming WHERE turn=?",(before,)).fetchone()
            if row[0] is not None: until=row[0]
        rows=db.execute("""SELECT t.id,t.input,t.answer,t.kind,t.scope,t.status,MAX(i.stamp) AS stamp
            FROM turns t JOIN incoming i ON i.turn=t.id
            WHERE t.peer=? AND t.scope=? AND (t.status='accepted' OR (t.scope='group' AND t.status='silent')
                OR (t.status='failed' AND EXISTS (SELECT 1 FROM turn_actions a WHERE a.turn_id=t.id AND a.status='accepted')))
                AND (? IS NULL OR t.rowid < (SELECT rowid FROM turns WHERE id=?))
            GROUP BY t.id ORDER BY t.rowid DESC LIMIT ?""",(peer,"group" if group else "direct",before,before,limit)).fetchall()
        turns=[]
        for row in rows:
            inputs=db.execute("""SELECT i.kind,i.body,g.speaker,g.mentioned,g.sender_key,v.transcript,t.tone FROM incoming i
                LEFT JOIN group_inputs g ON g.message_id=i.id
                LEFT JOIN voice_inputs v ON v.message_id=i.id LEFT JOIN voice_tones t ON t.message_id=i.id
                WHERE i.turn=? ORDER BY i.rowid""",(row["id"],)).fetchall()
            if row["scope"]=="direct" and any(x["kind"]==34 and not x["transcript"] for x in inputs):
                words="〔历史消息含微信语音，逐条转写不可还原〕\n"+(row["input"] or "")
            else:
                messages=[]
                for x in inputs:
                    prefix=speaker_line(x,bound_key)
                    if prefix: messages.append(prefix)
                    if x["kind"]==34 and not x["transcript"]:
                        messages.append("〔微信语音〕")
                    elif x["kind"]==34:
                        messages.append("〔微信语音转写〕\n"+x["transcript"]+("〔"+x["tone"]+"〕" if x["tone"] else ""))
                    elif x["kind"]==3: messages.append("〔此前发送了图片〕")
                    else: messages.append(x["body"])
                words="\n".join(messages)
            reply,receipt=delivered_reply(db,row,for_call=for_call)
            turns.append((row["stamp"],words,reply,receipt))
        if not group:
            for stamp,words,reply in db.execute("SELECT stamp,words,reply FROM call_lines WHERE peer=? AND stamp<=? ORDER BY stamp DESC,id DESC LIMIT ?",(peer,until,limit)):
                turns.append((stamp,"〔微信电话〕\n"+words,reply,""))
    result=[]
    for _,words,reply,receipt in sorted(turns,key=lambda t:t[0])[-limit:]:
        message={"role":"user","content":words[:2000]}
        if receipt:
            message["wechat_receipt"]=receipt
        result.append(message)
        if reply: result.append({"role":"assistant","content":reply[:2000]})
    return result


def remember_call_line(peer,words,reply):
    import time
    with database() as db:
        db.execute("INSERT INTO call_lines(peer,stamp,words,reply) VALUES(?,?,?,?)",(peer,time.time(),words[:2000],reply[:2000]))
        db.execute("DELETE FROM call_lines WHERE peer=? AND id NOT IN (SELECT id FROM call_lines WHERE peer=? ORDER BY id DESC LIMIT ?)",(peer,peer,MAX_HISTORY_TURNS))
