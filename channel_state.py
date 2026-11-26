"""Per-instance control settings and durable WeChat delivery records."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3


ROOT = Path(os.environ.get("WECHAT_CASE_STATE", "/var/lib/wechat-vps-panel"))
DEFAULT = {
    "name": "微信私聊", "persona": "", "worldbook": "", "memory_url": "",
    "persona_id":"", "worldbook_id":"", "memory_id":"",
    "chat_service":"deepseek", "vision_service":"deepseek",
    "chat_model": "deepseek-flash", "vision_model": "deepseek-flash",
    "asr": "xfyun", "tts": "elevenlabs", "voice": "",
    "asr_model":"", "tts_model":"", "asr_language":"zh_cn", "asr_accent":"mandarin",
    "note_rate":"16000", "call_cue":"off",
    "sticker_vision":"off",
    "merge_messages": True, "merge_wait_seconds": 7,
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


def get(db, key, default=None):
    row = db.execute("SELECT v FROM config WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(db, key, value):
    db.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))


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

def recent_dialogue(peer,before=None):
    """Return delivered turns in time order across this private chat and its calls."""
    import time
    with database() as db:
        until=time.time()
        if before:
            row=db.execute("SELECT MIN(stamp) FROM incoming WHERE turn=?",(before,)).fetchone()
            if row[0] is not None: until=row[0]
        rows=db.execute("""SELECT t.id,t.input,t.answer,t.kind,MAX(i.stamp) AS stamp
            FROM turns t JOIN incoming i ON i.turn=t.id
            WHERE t.peer=? AND t.status='accepted'
                AND (? IS NULL OR t.rowid < (SELECT rowid FROM turns WHERE id=?))
            GROUP BY t.id ORDER BY t.rowid DESC LIMIT 8""",(peer,before,before)).fetchall()
        turns=[]
        for row in rows:
            inputs=db.execute("""SELECT i.kind,i.body,v.transcript,t.tone FROM incoming i
                LEFT JOIN voice_inputs v ON v.message_id=i.id LEFT JOIN voice_tones t ON t.message_id=i.id
                WHERE i.turn=? ORDER BY i.rowid""",(row["id"],)).fetchall()
            if any(x["kind"]==34 and not x["transcript"] for x in inputs):
                words="〔历史消息含微信语音，逐条转写不可还原〕\n"+(row["input"] or "")
            else:
                messages=[]
                for x in inputs:
                    if x["kind"]==34:
                        messages.append("〔微信语音转写〕\n"+x["transcript"]+("〔"+x["tone"]+"〕" if x["tone"] else ""))
                    elif x["kind"]==3: messages.append("〔此前发送了图片〕")
                    else: messages.append(x["body"])
                words="\n".join(messages)
            reply=row["answer"] or ""
            if row["kind"]=="voice" and not reply.startswith("〔微信语音条〕"): reply="〔微信语音条〕\n"+reply
            turns.append((row["stamp"],words,reply))
        for stamp,words,reply in db.execute("SELECT stamp,words,reply FROM call_lines WHERE peer=? AND stamp<=? ORDER BY stamp DESC,id DESC LIMIT 8",(peer,until)):
            turns.append((stamp,"〔微信电话〕\n"+words,reply))
    result=[]
    for _,words,reply in sorted(turns,key=lambda t:t[0])[-8:]:
        result.append({"role":"user","content":words[:2000]})
        if reply: result.append({"role":"assistant","content":reply[:2000]})
    return result


def remember_call_line(peer,words,reply):
    import time
    with database() as db:
        db.execute("INSERT INTO call_lines(peer,stamp,words,reply) VALUES(?,?,?,?)",(peer,time.time(),words[:2000],reply[:2000]))
        db.execute("DELETE FROM call_lines WHERE peer=? AND id NOT IN (SELECT id FROM call_lines WHERE peer=? ORDER BY id DESC LIMIT 8)",(peer,peer))
