"""Route live messages to the paired friend and explicitly enabled WeChat groups."""
from datetime import datetime
import hashlib
import hmac
import json
import re
import subprocess
import time
from urllib.error import HTTPError, URLError

import account_stickers
import call_channel
import channel_state as st
import conversation_prompt
import deepseek_case as ds
import group_chats
import pcm_audio
import providers
import reply_actions
import serein_memory
import spoken_reply
from wechat_case import cli, esc, image_type, media, wx

CALL_REQUEST = re.compile(
    r"(?:(?:你|请|现在|那|快|再|主动|能不能|可以|能|麻烦|帮我|用微信))*"
    r"(?:(?:给我)?(?:打(?:一?个|通|下)?(?:微信|语音)?电话(?:给我)?|回(?:个|一通)?电话(?:给我)?)"
    r"|打给我|打过来|回拨(?:给我)?|给我回拨|给我来(?:一?个|通)?电话)"
    r"(?:过来|来|好不好|可以吗|好吗|一下|吧|呀|嘛|呗|啦|哈|啊|哦)*"
)
CALL_CANCEL = re.compile(r"(?:算了|不用了|(?:先)?(?:别|不用|不要)(?:再)?(?:给我)?打(?:电话|了|给我|过来)?(?:了)?)")
VOICE_DENIAL = re.compile(r"(?:我.{0,8})?(?:发不了|不能发|没法发|不支持发|无法发)(?:送)?(?:微信)?语音|只能(?:打字|文字)(?:回复|聊天|回)")
VOICE = re.compile(
    r"^(?:(?:你|请|给我|能不能|能否|可不可以|可以|能|再|也|用微信|帮我)\s*)*"
    r"(?:发[一条个段下\s]*语音(?:条|消息)?|用?语音回(?:复)?(?:我)?|用语音说(?:一句|句话|几句)?)"
    r"(?:给我|让我|来)?(?:听一听|听一下|听听|试听|试试)?[吧呀嘛吗呗啦呢哈啊～~！!。？?\s]*$"
)


def mode_for(lines):
    call_cancelled=False
    for line in reversed(lines):
        cmd = line.partition("\n〔引用〕")[0].strip()
        call_text=re.sub(r"[\s，,。！!？?～~]","",cmd)
        if CALL_CANCEL.fullmatch(call_text):
            call_cancelled=True
        if not call_cancelled and len(call_text)<=100 and re.fullmatch(r"(?:"+CALL_REQUEST.pattern+r")+",call_text):
            return "call",""
        wanted = account_stickers.request_name(cmd)
        if wanted is not None:
            return "sticker", wanted
        if cmd == "/语音" or cmd.startswith("/语音 ") or VOICE.fullmatch(cmd):
            return "voice", cmd[3:].strip() if cmd.startswith("/语音 ") else ""
    return "text", ""


def valid_pair(pair, body):
    return bool(pair and pair["expires"] >= time.time() and
                hmac.compare_digest(pair["hash"],hashlib.sha256(body.encode()).hexdigest()))


def incoming_sender(msg,peer,mid):
    sender=msg.get("senderId")
    if (msg.get("direction")!="incoming" or msg.get("id")!=mid or msg.get("threadId")!=peer
            or msg.get("recall") or not isinstance(sender,str) or not sender):
        return None
    if peer.endswith("@chatroom"):
        return sender if sender!=peer else None
    return sender if sender==peer else None


def restore_group_senders():
    """Recover missing sender keys on upgrade from the original group messages."""
    with st.database() as db:
        rooms=list(st.get(db,"group_routes",{}))
    for room in rooms:
        if not room.endswith("@chatroom"):
            continue
        with st.database() as db:
            missing=db.execute("""SELECT i.id FROM incoming i JOIN group_inputs g ON g.message_id=i.id
                WHERE g.sender_key IS NULL AND i.peer=? AND i.turn IN
                    (SELECT id FROM turns WHERE peer=? AND scope='group' ORDER BY rowid DESC LIMIT ?)""",
                (room,room,st.MAX_HISTORY_TURNS)).fetchall()
        verified=[]
        for (mid,) in missing:
            try:
                sender=incoming_sender(wx("/messages/"+esc(mid)),room,mid)
            except HTTPError as exc:
                if exc.code in (404,410):
                    continue
                break
            except (OSError,ValueError):
                break
            if sender:
                verified.append((st.group_sender_key(room,sender),mid))
        if verified:
            with st.database() as db:
                db.executemany("UPDATE group_inputs SET sender_key=? WHERE message_id=? AND sender_key IS NULL",verified)


def ingest(db, event):
    data = event.get("data") or {}
    if event.get("kind") != "message.committed":
        return
    peer, mid = data.get("threadId"), data.get("messageId")
    bound = st.get(db,"peer")
    if not isinstance(peer,str) or not peer or not mid:
        return
    group = st.get(db,"group_routes",{}).get(peer) if peer.endswith("@chatroom") else None
    natural = group is not None and group["mode"] == "natural"
    if not group and (peer.endswith("@chatroom") or (bound and bound != peer)):
        return
    if data.get("direction") == "outgoing" and (bound == peer or group) and data.get("origin") in ("local_send","live"):
        if not natural:
            db.execute("UPDATE turns SET status='sealed', next_try=0 WHERE peer=? AND status='collecting'",(peer,))
        return
    if data.get("direction") != "incoming" or data.get("origin") != "live":
        return
    pair = st.get(db,"pair",{})
    if not group and not bound and pair.get("expires",0) < time.time():
        return
    try:
        stamp = datetime.fromisoformat(event["createdAt"]).timestamp()
    except (KeyError,TypeError,ValueError):
        if group: return
        stamp = time.time()
    if group and stamp < group["since"]:
        return
    if db.execute("SELECT 1 FROM incoming WHERE id=?",(mid,)).fetchone():
        return
    try:
        msg = wx("/messages/" + esc(mid))
    except HTTPError as err:
        if err.code == 404:
            return
        raise
    sender = incoming_sender(msg,peer,mid)
    if not sender:
        return
    kind = msg.get("type")
    body = (msg.get("body") if isinstance(msg.get("body"),str) else "").strip()
    if not group and not bound:
        if kind == 1 and valid_pair(pair,body) and wx("/threads/"+esc(peer)).get("kind") == "direct":
            db.execute("BEGIN IMMEDIATE")
            if not st.get(db,"peer") and valid_pair(st.get(db,"pair",{}),body):
                st.put(db,"peer",peer)
                st.put(db,"pair",{})
        return
    if body.startswith("微信配对 "):
        return
    sticker_server_id = None
    quoted_self = False
    if kind == 49 and msg.get("appMessageType") == 57:
        quote = msg.get("quote") or {}
        quoted_self = natural and quote.get("direction") == "outgoing"
        ref = quote.get("embeddedText")
        if quote.get("state") == "recalled":
            ref = "〔原消息已撤回〕"
        elif account_stickers.is_sticker(quote) or quote.get("embeddedSticker"):
            sticker_server_id = quote.get("serverId")
            ref = account_stickers.UNKNOWN
        body = body + ("\n〔引用〕" + ref[:1500] if isinstance(ref,str) and ref.strip() else "")
        kind = 1
    elif kind == 47 or (kind == 49 and msg.get("appMessageType") == 8):
        kind = 47
        sticker_server_id = msg.get("serverId")
        body = account_stickers.incoming_label(msg,body,st.get(db,"sticker_names",{}))
    elif kind not in (1,3,34):
        return
    if kind == 1 and not body:
        return
    if kind in (3,34):
        body = ""
    if group:
        lookup = group_chats.current_speaker_name if sender==bound else group_chats.speaker_name
        speaker = lookup(peer,sender)
    mentions = msg.get("mentions") or {}
    mentioned = bool(group and mentions.get("state")=="known" and mentions.get("self") is True)
    db.execute("BEGIN IMMEDIATE")
    if group:
        current_group = st.get(db,"group_routes",{}).get(peer)
        if not current_group or current_group["since"] != group["since"] or current_group["mode"] != group["mode"]:
            return
        group = current_group
    if db.execute("SELECT 1 FROM incoming WHERE id=?",(mid,)).fetchone():
        return
    addressed = mentioned or quoted_self
    if natural:
        passive_wait = group.get("passive_wait", group_chats.NATURAL_PASSIVE_WAIT)
        wait_seconds = group_chats.NATURAL_DIRECT_WAIT if addressed else passive_wait
    else:
        wait_seconds = st.merge_window(st.get(db, "profile", {}))
    active = db.execute("SELECT id,next_try FROM turns WHERE status='collecting' AND peer=? ORDER BY rowid DESC LIMIT 1",(peer,)).fetchone()
    if wait_seconds and active and stamp <= active["next_try"]:
        turn = active["id"]
        if not natural:
            db.execute("UPDATE turns SET next_try=max(next_try,?) WHERE id=?", (stamp + wait_seconds, turn))
        elif addressed and not db.execute("""SELECT 1 FROM incoming i JOIN group_inputs g ON g.message_id=i.id
                WHERE i.turn=? AND (g.mentioned OR g.quoted_self) LIMIT 1""", (turn,)).fetchone():
            # The first direct address upgrades a passive batch and starts its fixed wait.
            db.execute("UPDATE turns SET next_try=? WHERE id=?", (stamp + wait_seconds, turn))
    else:
        if active:
            db.execute("UPDATE turns SET status='sealed', next_try=0 WHERE id=?", (active["id"],))
        turn = hashlib.sha256((peer+"\n"+mid).encode()).hexdigest()
        status = "collecting" if wait_seconds else "ready"
        deadline = stamp + wait_seconds if wait_seconds else 0
        db.execute("INSERT INTO turns(id,peer,status,next_try,scope) VALUES(?,?,?,?,?)",
                   (turn, peer, status, deadline, "group" if group else "direct"))
        if not group:
            serein_memory.enroll(db, turn, peer)
    db.execute("INSERT INTO incoming(id,peer,kind,body,stamp,turn) VALUES(?,?,?,?,?,?)",
               (mid,peer,kind,body[:16000],stamp,turn))
    if isinstance(sticker_server_id,str) and re.fullmatch(r"[0-9]{1,20}",sticker_server_id):
        db.execute("INSERT INTO sticker_refs VALUES(?,?)",(mid,sticker_server_id))
    if group:
        db.execute("INSERT INTO group_inputs(message_id,speaker,mentioned,sender_key,quoted_self) VALUES(?,?,?,?,?)",
                   (mid,speaker,mentioned,st.group_sender_key(peer,sender),quoted_self))


def collect():
    while True:
        try:
            with st.database() as db:
                cursor = st.get(db,"cursor")
                if cursor is None:
                    cursor = wx("/events?newest=true&limit=1").get("nextCursor","0")
                    st.put(db,"cursor",cursor)
            try:
                page = wx("/events?after=" + esc(cursor) + "&limit=100")
            except HTTPError as exc:
                problem = json.loads(exc.read(4096)).get("error",{})
                if problem.get("code") != "event_cursor_expired":
                    raise
                page = wx("/events?newest=true&limit=100")
                with st.database() as db:
                    if not page.get("items"):
                        st.put(db,"cursor",page.get("nextCursor","0"))
                    st.put(db,"cursor_gap",True)
            for event in page.get("items",[]):
                with st.database() as db:
                    ingest(db,event)
                    st.put(db,"cursor",event["cursor"])
            time.sleep(.12 if len(page.get("items",[])) == 100 else 1.2)
        except Exception as exc:
            print("event poll:",type(exc).__name__,getattr(exc,"code",""),flush=True)
            time.sleep(5)


def sticker_description(mid, settings):
    try:
        body = account_stickers.received(mid, settings)
    except (OSError,ValueError,KeyError,IndexError) as exc:
        # The media helper only exposes fixed reason codes, never signed URLs.
        reason = getattr(exc, "media_reason", type(exc).__name__)
        print("sticker read:",reason,flush=True)
        return account_stickers.UNKNOWN
    if body != account_stickers.UNKNOWN:
        with st.database() as db:
            db.execute("UPDATE incoming SET body=? WHERE id=? AND kind=47 AND body=?",
                       (body,mid,account_stickers.UNKNOWN))
    return body


def quoted_sticker(row, settings):
    with st.database() as db:
        ref = db.execute("SELECT server_id FROM sticker_refs WHERE message_id=?", (row["id"],)).fetchone()
        if not ref: return row["body"]
        candidates = db.execute("""SELECT i.id FROM incoming i JOIN sticker_refs r ON r.message_id=i.id
            WHERE i.peer=? AND i.kind=47 AND r.server_id=? LIMIT 2""",(row["peer"],ref[0])).fetchall()
    # Confirm the quote and its target in the native account, including recalls.
    parent = wx("/messages/"+esc(row["id"]))
    quote = parent.get("quote") or {}
    if parent.get("id") != row["id"] or parent.get("threadId") != row["peer"] or parent.get("recall") or quote.get("serverId") != ref[0]:
        return row["body"]
    if quote.get("state") == "recalled":
        return row["body"].removesuffix(account_stickers.UNKNOWN)+"〔原消息已撤回〕"
    if len(candidates) == 1:
        possible = [wx("/messages/"+esc(candidates[0][0]))]
    elif not candidates:
        # Older messages and stickers sent by this account predate the local index.
        possible = wx("/threads/"+esc(row["peer"])+"/messages?limit=100").get("items",[])
    else:
        return row["body"]
    hits = [m for m in possible if m.get("id") and m.get("threadId")==row["peer"] and m.get("serverId")==ref[0]
            and account_stickers.is_sticker(m) and not m.get("recall")
            and (not quote.get("senderId") or m.get("senderId")==quote["senderId"])
            and (not quote.get("direction") or m.get("direction")==quote["direction"])]
    if len(hits) != 1: return row["body"]
    mid = hits[0]["id"]
    with st.database() as db:
        prior = db.execute("SELECT body FROM incoming WHERE id=? AND peer=? AND kind=47",(mid,row["peer"])).fetchone()
        label = prior[0] if prior else account_stickers.incoming_label(hits[0],hits[0].get("body") or "",st.get(db,"sticker_names",{}))
    if label == account_stickers.UNKNOWN:
        label = sticker_description(mid,settings)
    return row["body"].removesuffix(account_stickers.UNKNOWN) + label


def input_parts(rows, settings, *, bound_key=None):
    parts, lines, sources = [], [], []
    pixels = 0
    for row in rows:
        body = row["body"]
        speaker = st.speaker_line(row,bound_key)
        if speaker:
            parts.append({"type":"text","text":speaker})
        if row["kind"] == 3:
            data = media(row["id"],("original","standard","normal"))
            pixels += len(data)
            if pixels > 12 * 1024 * 1024:
                raise ValueError("本轮图片总量超过 12 MiB")
            parts.append(ds.image_part(data,image_type(data)))
            lines.append("〔图片〕")
            sources.append("微信图片（已附实际画面）")
            continue
        if row["kind"] == 34:
            with st.database() as db:
                prior=db.execute("""SELECT v.transcript,t.tone,v.provider FROM voice_inputs v
                            LEFT JOIN voice_tones t USING(message_id) WHERE v.message_id=?""",(row["id"],)).fetchone()
            if prior:
                body,tone,provider=prior
            else:
                pcm=pcm_audio.samples(media(row["id"],("normal","standard")),from_wechat=True)
                body,tone,provider,status=providers.hear_utterance(settings,pcm)
                with st.database() as db:
                    inserted=db.execute("INSERT OR IGNORE INTO voice_inputs(message_id,transcript,provider) VALUES(?,?,?)",
                                        (row["id"],body,provider))
                    if inserted.rowcount and status!="off":
                        db.execute("INSERT INTO voice_tones VALUES(?,?,?)",(row["id"],tone,status))
            sources.append("微信语音条（"+(provider or "通道")+"已转写）")
            parts.append({"type":"text","text":"〔语音转写〕\n"+providers.with_tone(body,tone)})
        else:
            if row["kind"]==47 and body==account_stickers.UNKNOWN:
                body=sticker_description(row["id"],settings)
            elif row["kind"]==1 and body.endswith("\n〔引用〕"+account_stickers.UNKNOWN):
                try:
                    body=quoted_sticker(row,settings)
                except (OSError,ValueError,KeyError,IndexError) as exc:
                    print("sticker quote:",type(exc).__name__,flush=True)
                if body!=row["body"]:
                    with st.database() as db:
                        db.execute("UPDATE incoming SET body=? WHERE id=? AND body=?",(body,row["id"],row["body"]))
            sources.append("微信表情包" if row["kind"]==47 else "微信文字")
            parts.append({"type":"text","text":body})
        lines.append(body)
    return parts,lines,sources


def update(turn, *, action=False, **values):
    fields = ", ".join(k+"=?" for k in values)
    with st.database() as db:
        table="turn_actions" if action else "turns"
        db.execute("UPDATE "+table+" SET "+fields+" WHERE id=?",(*values.values(),turn))


def history(peer,current,*,bound_key=None):
    return st.recent_dialogue(peer,current,bound_key=bound_key)


VOICE_REQUEST_NOTE = "这轮请用语音回复。"
VOICE_DENIAL_NOTE = "语音已就绪，用语音回刚才那句话。"
CALL_RETRY_NOTE = "这轮没有回拨邀请，直接回消息。"
MISSING_VOICE_NOTE = "这段会直接念给对方，只输出台词。"
MEDIA_FORMAT_NOTE = "重新按微信动作格式写。每条语音第一行单独写 [[voice]]，下一行是台词。表情单独一行 [[sticker]]。引用写法可用行内代码。"


def chat_instructions(settings, *, group):
    scene = "微信群聊，留意谁在跟谁说话。" if group else "微信私聊。"
    style = "多用日常口语和短句，句末标点可省，也可以只回标点或表情包。"
    if settings.get("split_bubbles", True):
        style += "想分气泡就换行。"
    lines = [scene + style]
    voice = providers.audio_route_ready(settings, "tts")
    if voice:
        audio=f"每条语音用下面两行，发几条就写几组。台词只写一遍，单发语音时省去文字气泡。\n{reply_actions.VOICE}\n要念的台词"
        lines.append(providers.voice_instructions(settings,audio))
    lines.append(f"想发表情包就单独写一行 {reply_actions.STICKER}，随后从本号的表情名中选一张。")
    if group:
        lines.append("跟标为「已绑定的私聊好友」的人说话时直接用「你」，群里不提绑定或私聊。")
        lines.append(f"想旁听时，只回 {reply_actions.SILENT}。")
    elif voice and providers.audio_route_ready(settings, "asr"):
        lines.append(f"对方请你打电话时，只回 {reply_actions.CALL}。")
    return "\n".join(lines)


def reply_messages(settings,peer,current,parts,sources,*,now=None,group=None,bound_key=None,memory=None):
    system=chat_instructions(settings, group=bool(group))
    recent=history(peer,current,bound_key=bound_key)
    notes=[]
    if group:
        notes.append("当前群聊："+group["name"])
    else:
        words=parts if isinstance(parts,str) else "\n".join(p["text"] for p in parts if p["type"]=="text")
        if re.search(r"电话|通话|来电|去电|回拨|接听|接通|没接|接了|挂断|线路",words):
            notes.append(call_channel.private_call_note(peer))
    if any(s!="微信文字" for s in sources):
        notes.append("来源（依次）："+"、".join(sources))
    if not group and memory and memory["note"]:
        notes.append("Serein 召回的往事（仅作背景）\n"+memory["note"])
    return conversation_prompt.build(settings,system,recent,parts,notes,now=now,private=not group)


def dispatch_call(ident,peer,lines):
    # Reserve before opening a live call. A replay or ambiguous outcome must
    # never ring the same person twice for one WeChat message.
    with st.database() as db:
        db.execute("BEGIN IMMEDIATE")
        claimed=db.execute("""UPDATE turns SET status='call_requested',kind='call',phase='dial',input=?
            WHERE id=? AND peer=? AND status IN ('collecting','sealed','ready')""",("\n".join(lines),ident,peer))
    if not claimed.rowcount:
        return
    try:
        call_channel.start("dial",expected_peer=peer,turn_id=ident)
    except Exception as exc:
        update(ident,status="failed",error=explain(exc),phase="dial")


def allows_model_call(lines):
    words=" ".join(x.partition("\n〔引用〕")[0] for x in lines)
    if re.search(r"举例|代号|代码|什么[意含]思|别打|不要打|不许打|不用打|别给我打|不要给我打|如果|假如|他说|她说",words):
        return False
    return bool(re.search(r"打给我|打过来|回拨|给我.{0,8}(?:电话|打)|(?:打|来|通).{0,8}电话|来.{0,4}语音通话",words))


def sticker_context(peer,ident,lines,assistant_text="",*,speakers=(),recent=None):
    if recent is None:
        recent=history(peer,ident)
    context = [("助手" if m["role"]=="assistant" else "对方")+"："+m["content"][-75:] for m in recent[-4:]]
    exchange = [f"{who}：{text}" if who else text for who,text in zip(speakers,lines)] if speakers else lines
    context.append("对方这轮："+"\n".join(exchange[-3:])[-140:])
    if assistant_text:
        context.append("助手这轮的回复："+assistant_text[:140])
    return "\n".join(context)


def explicit_sticker_for_action(db,turn):
    # A single request can name the exact sticker after the assistant elects to send.
    rows=db.execute("""SELECT COALESCE(v.transcript,i.body) FROM incoming i
        LEFT JOIN voice_inputs v ON v.message_id=i.id WHERE i.turn=? LIMIT 2""",(turn,)).fetchall()
    if len(rows)!=1:
        return ""
    details=account_stickers.request_details(rows[0][0].partition("\n〔引用〕")[0])
    return details[0] if details and details[1] else ""


def display_action(kind, value):
    label = {"voice":reply_actions.VOICE_LABEL, "sticker":reply_actions.STICKER_LABEL, "text":""}[kind]
    return label + value


def save_plan(ident,lines,parts,hint):
    rows=[]
    display=[]
    for index,(kind,value) in enumerate(parts):
        wire=None
        if kind=="voice":
            spoken=spoken_reply.parse(value)
            value=spoken.words
            wire=json.dumps({"delivery":spoken.delivery},ensure_ascii=False)
        display.append(display_action(kind,value))
        rows.append((ident+"-"+str(index),ident,index,kind,value,hint if kind=="sticker" else "",wire))
    with st.database() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM turn_actions WHERE turn_id=?",(ident,)).fetchone(): return
        db.executemany("INSERT INTO turn_actions(id,turn_id,pos,kind,answer,requested,wire) VALUES(?,?,?,?,?,?,?)",rows)
        kinds={kind for kind,_ in parts}
        db.execute("UPDATE turns SET input=?,answer=?,kind=?,phase='media' WHERE id=?",
                   ("\n".join(lines),"\n".join(display),parts[0][0] if len(kinds)==1 else "mixed",ident))


def deliver_plan(ident,peer,settings):
    with st.database() as db:
        records=db.execute("SELECT * FROM turn_actions WHERE turn_id=? ORDER BY pos",(ident,)).fetchall()
    for r in records:
        if r["kind"]=="text" and r["status"]!="accepted" and not r["send_id"]:
            reply_actions.check_media_format(r["answer"])
    for r in records:
        if r["status"]!="accepted":
            deliver_action(r,peer,settings,requested=r["requested"],action=True,descriptive=True)
    with st.database() as db:
        sent=db.execute("SELECT kind,answer,receipt FROM turn_actions WHERE turn_id=? ORDER BY pos",(ident,)).fetchall()
    display = "\n".join(display_action(r["kind"],r["answer"]) for r in sent)
    update(ident,status="accepted",phase="receipt",error=None,receipt=sent[-1]["receipt"],answer=display)


class GroupTurnClosed(Exception):
    """Group settings changed while this response was being prepared."""


def ensure_group_open(peer, ident):
    if peer.endswith("@chatroom"):
        with st.database() as db:
            pending = db.execute("""SELECT 1 FROM turns WHERE id=? AND peer=? AND scope='group'
                                    AND status IN ('collecting','sealed','ready')""", (ident,peer)).fetchone()
            if not pending or peer not in st.get(db,"group_routes",{}):
                raise GroupTurnClosed()


def later_group_address(peer, turn):
    with st.database() as db:
        return db.execute("""SELECT 1 FROM turns t JOIN incoming i ON i.turn=t.id
            JOIN group_inputs g ON g.message_id=i.id
            WHERE t.peer=? AND t.rowid>(SELECT rowid FROM turns WHERE id=?)
              AND t.status IN ('collecting','sealed','ready') AND (g.mentioned OR g.quoted_self)
            LIMIT 1""", (peer,turn)).fetchone() is not None


def process(turn):
    ident, peer = turn["id"], turn["peer"]
    with st.database() as db:
        group = None
        bound = st.get(db,"peer")
        bound_key = None
        if turn["scope"]=="group":
            ensure_group_open(peer,ident)
            group = st.get(db,"group_routes",{}).get(peer)
            if not group: raise GroupTurnClosed()
            bound_key = st.group_sender_key(peer,bound)
        elif peer != bound:
            raise ValueError("私聊绑定已改变")
        settings = st.profile(db)
        rows = db.execute("""SELECT i.*,g.speaker,g.mentioned,g.quoted_self,g.sender_key FROM incoming i
                             LEFT JOIN group_inputs g ON g.message_id=i.id WHERE i.turn=? ORDER BY i.rowid""",
                          (ident,)).fetchall()
        planned=db.execute("SELECT 1 FROM turn_actions WHERE turn_id=?",(ident,)).fetchone()
    if planned:
        deliver_plan(ident,peer,settings)
        return
    answer, kind, job_id = turn["answer"],turn["kind"],turn["send_id"]
    voice_wire = turn["wire"] if kind == "voice" else None
    chosen = None
    if not answer:
        natural = group is not None and group["mode"] == "natural"
        addressed = natural and any(row["mentioned"] or row["quoted_self"] for row in rows)
        if natural and not addressed and later_group_address(peer,ident):
            update(ident,status="silent",phase="group_yield",error=None)
            return
        if group and group["mode"]=="mention" and not any(r["mentioned"] for r in rows):
            update(ident,status="silent",phase="group_mention",error=None)
            return
        update(ident,phase="read")
        parts, lines, sources = input_parts(rows,settings,bound_key=bound_key)
        # In a group “send a voice note” may have been addressed to another member.
        # Only the assistant's own decision can trigger a group send.
        kind, literal = ("text", "") if group else mode_for(lines)
        if kind=="call":
            dispatch_call(ident,peer,lines)
            return
        if kind == "voice" and settings["tts"] == "off":
            raise ValueError("私聊配置已关闭语音合成")
        if kind == "sticker":
            requested=next((details for line in reversed(lines) if (details:=account_stickers.request_details(line.partition("\n〔引用〕")[0])) is not None),("",False))
            exact=requested[1]
            hint=literal or sticker_context(peer,ident,lines)
            chosen = account_stickers.choose(ident,hint,description_settings=settings,exact_name=bool(exact and literal))
            answer = "〔" + chosen["group"] + " · " + chosen["label"] + "〕"
        elif literal:
            answer = literal
        else:
            update(ident,phase="model")
            vision = any(p["type"] == "image_url" for p in parts)
            ensure_group_open(peer,ident)
            memory = serein_memory.recall(ident,peer,lines) if turn["scope"]=="direct" else None
            messages=reply_messages(settings,peer,ident,parts,sources,group=group,bound_key=bound_key,memory=memory)

            def draft(note=None):
                request = conversation_prompt.with_chat_note(messages, note) if note else messages
                result = providers.chat(settings, request, vision=vision)
                split = settings.get("split_bubbles", True)
                try:
                    reply_actions.check_media_format(result, split=split)
                except reply_actions.MediaFormatError:
                    ensure_group_open(peer,ident)
                    correction = (note+"\n" if note else "") + MEDIA_FORMAT_NOTE
                    request = conversation_prompt.with_chat_note(messages, correction)
                    result = providers.chat(settings, request, vision=vision)
                    reply_actions.check_media_format(result, split=split)
                return result

            note = VOICE_REQUEST_NOTE if kind=="voice" else None
            if natural:
                if addressed:
                    note = "本轮有人 @ 你或引用你的消息。自然接他的话并给出回应。"
                else:
                    note = f"这是群友之间的聊天。只有确实有话值得接时才发一句，否则只回 {reply_actions.SILENT}。"
            answer = draft(note)
            if addressed and answer.strip() == reply_actions.SILENT:
                ensure_group_open(peer,ident)
                answer = draft(note + " 对这条直接招呼请回一句。")
            serein_memory.used(ident,memory)
            ensure_group_open(peer,ident)
            if natural and not addressed and later_group_address(peer,ident):
                update(ident,status="silent",phase="group_yield",error=None)
                return
            if group and answer.strip() in (reply_actions.SILENT,reply_actions.CALL):
                update(ident,status="silent",phase="group_decision",error=None)
                return
            if kind=="voice" and VOICE_DENIAL.search(answer):
                answer=draft(VOICE_DENIAL_NOTE)
                if VOICE_DENIAL.search(answer):
                    raise ValueError("回复与当前可用的微信语音功能矛盾，正在重新生成")
            plan=reply_actions.parse(answer)
            if plan and plan[0][0]=="call":
                if allows_model_call(lines):
                    dispatch_call(ident,peer,lines)
                    return
                answer=draft(CALL_RETRY_NOTE)
                plan=reply_actions.parse(answer)
                if plan and plan[0][0]=="call":
                    raise ValueError("这轮没有明确的回拨请求")
            if settings.get("split_bubbles",True):
                if plan:
                    expanded=[]
                    for action,value in plan:
                        if action=="text":
                            expanded.extend(("text",b) for b in reply_actions.text_bubbles(value))
                        else:
                            expanded.append((action,value))
                    plan=expanded
                elif kind=="text":
                    bubbles=reply_actions.text_bubbles(answer)
                    if len(bubbles)>1:
                        plan=[("text",b) for b in bubbles]
            if plan:
                resolved=[]
                for action,value in plan:
                    if action=="voice" and settings["tts"]=="off":
                        raise ValueError("私聊配置已关闭语音合成")
                    if action=="voice" and not value:
                        value=draft(MISSING_VOICE_NOTE)
                        if value.strip()==reply_actions.SILENT or reply_actions.parse(value) or VOICE_DENIAL.search(value):
                            raise ValueError("语音台词仍未生成")
                    resolved.append((action,value))
                if kind=="voice" and not any(a=="voice" for a,_ in resolved):
                    raise ValueError("本轮请求语音，生成内容缺少语音台词")
                hint=""
                if any(a=="sticker" for a,_ in resolved):
                    spoken = " ".join(v for a,v in resolved if a!="sticker")
                    speakers=[st.speaker_line(r,bound_key) for r in rows] if group else ()
                    hint = sticker_context(peer,ident,lines,spoken,speakers=speakers,recent=messages[1:-1])
                save_plan(ident,lines,resolved,hint)
                deliver_plan(ident,peer,settings)
                return
        if kind == "voice":
            spoken = spoken_reply.parse(answer)
            answer = spoken.words
            voice_wire = json.dumps({"delivery":spoken.delivery},ensure_ascii=False)
            update(ident,input="\n".join(lines),answer=answer,kind=kind,wire=voice_wire)
        else:
            update(ident,input="\n".join(lines),answer=answer,kind=kind)
    with st.database() as db:
        record=db.execute("SELECT * FROM turns WHERE id=?",(ident,)).fetchone()
    deliver_action(record,peer,settings,requested=mode_for([r["body"] for r in rows])[1],chosen=chosen)


def deliver_action(record,peer,settings,*,requested="",chosen=None,action=False,descriptive=False):
    ident=record["id"]
    kind,answer,job_id=record["kind"],record["answer"],record["send_id"]
    voice_wire=record["wire"] if kind=="voice" else None
    def save(**values):
        update(ident,action=action,**values)
    if not job_id:
        turn_id=record["turn_id"] if action else ident
        ensure_group_open(peer,turn_id)
        save(phase="media")
        key = "reply-"+ident
        if kind == "sticker":
            name=""
            if not chosen:
                with st.database() as db:
                    chosen = st.get(db,"sticker:"+ident)
                    if not chosen and descriptive:
                        name=explicit_sticker_for_action(db,turn_id)
            existing = record["wire"] if chosen else None
            if not chosen:
                chosen = account_stickers.choose(ident,name or requested,description_settings=settings if descriptive else None,exact_name=bool(name))
            if existing:
                account_stickers.ensure_owned(chosen)
            wire = json.loads(existing) if existing else account_stickers.prepare(chosen)
            save(answer="〔"+chosen["group"]+" · "+chosen["label"]+"〕",wire=json.dumps(wire),phase="send")
            ensure_group_open(peer,turn_id)
            job = wx("/sends",{"threadId":peer,"kind":"sticker","source":wire},key)
        elif kind == "voice":
            path = st.ROOT/(ident+".wav")
            if not path.exists():
                delivery = json.loads(voice_wire)["delivery"] if voice_wire and providers.has_audio_tags(settings) else answer
                audio = providers.synthesize(settings,delivery)
                tmp = path.with_suffix(".part")
                tmp.write_bytes(audio)
                tmp.replace(path)
            else:
                # A retry may find an audio file staged by an older release.
                # Validate it without changing the source of an idempotent send.
                pcm_audio.samples(path.read_bytes())
            save(phase="send")
            ensure_group_open(peer,turn_id)
            job = cli("messages","send",peer,"--voice",path,"--idempotency-key",key,"--detach")
        else:
            reply_actions.check_media_format(answer)
            save(phase="send")
            ensure_group_open(peer,turn_id)
            job = wx("/sends",{"threadId":peer,"text":answer},key)
        job_id = job["id"]
        save(send_id=job_id)
    save(phase="receipt")
    for _ in range(20):
        receipt = wx("/sends/"+esc(job_id))
        detail = receipt.get("result") or {}
        if (receipt.get("destination") or receipt.get("threadId")) != peer:
            raise ValueError("微信回执的目标会话不匹配")
        sent = receipt.get("state")
        if sent == "accepted" or (kind=="sticker" and sent=="available" and detail.get("serverAcceptance")=="accepted"):
            server = receipt.get("serverId") or receipt.get("serverMessageId") or detail.get("serverMessageId")
            if not server:
                raise ValueError("微信未提供可核验的消息编号")
            save(status="accepted",receipt=str(server),error=None)
            if kind == "sticker":
                with st.database() as db:
                    picked = st.get(db,"sticker:"+ident)
                    if picked:
                        st.put(db,"last_owned_sticker",picked["item"])
            (st.ROOT/(ident+".wav")).unlink(missing_ok=True)
            return
        if sent in ("failed","rejected","needs_review","cancelled","expired","available","uncertain"):
            raise ValueError("微信未确认接收："+str(sent))
        time.sleep(.75)
    raise TimeoutError("微信投递回执仍在等待")


def explain(exc):
    if isinstance(exc,HTTPError):
        site = "微信" if exc.url.startswith("http://127.0.0.1:28220/") else "模型服务"
        return site + "接口返回 HTTP " + str(exc.code)
    if isinstance(exc,FileNotFoundError) and not ds.KEY.exists():
        return "请先在管理页保存 DeepSeek Key"
    if isinstance(exc,subprocess.SubprocessError):
        return "微信音频处理暂时失败"
    if isinstance(exc,(ValueError,TimeoutError)):
        return str(exc)[:120]
    if isinstance(exc,URLError):
        return "上游服务暂时无法连接"
    return "操作失败（"+type(exc).__name__+"）"


def next_turn(db,scope):
    return db.execute("""SELECT t.* FROM turns t WHERE t.scope=? AND t.next_try<=?
        AND t.status IN ('collecting','sealed','ready')
        AND NOT EXISTS (SELECT 1 FROM turns earlier WHERE earlier.peer=t.peer AND earlier.rowid<t.rowid
                        AND earlier.status IN ('collecting','sealed','ready'))
        ORDER BY t.rowid LIMIT 1""",(scope,time.time())).fetchone()


def deliver(scope="direct"):
    while True:
        try:
            with st.database() as db:
                db.execute("BEGIN IMMEDIATE")
                row = next_turn(db,scope)
                if row:
                    db.execute("UPDATE turns SET status='ready' WHERE id=?",(row["id"],))
            if not row:
                time.sleep(.4)
                continue
            try:
                process(row)
            except GroupTurnClosed:
                continue
            except Exception as exc:
                attempts = row["attempts"] + 1
                with st.database() as db:
                    db.execute("""UPDATE turns SET status=?,attempts=?,error=?,next_try=?
                               WHERE id=? AND status='ready'""",
                               ("failed" if attempts>=3 or isinstance(exc,reply_actions.MediaFormatError) else "ready",attempts,explain(exc),
                                time.time()+5*attempts,row["id"]))
        except Exception as exc:
            print("delivery worker:",type(exc).__name__,flush=True)
            time.sleep(3)
