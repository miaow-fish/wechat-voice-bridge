"""Bound-peer WeChat voice calls over the public v2 and v3 call APIs."""
import asyncio
import base64
from collections import deque
from concurrent.futures import Future
from contextlib import AsyncExitStack, aclosing
from datetime import datetime
from difflib import SequenceMatcher
from hashlib import sha256
import json
from importlib.util import find_spec
import math
import sqlite3
import struct
from threading import Event, Lock, Thread
import time
from uuid import uuid4

import channel_state as st
import conversation_prompt
import pcm_audio
import providers
import sensevoice_local
import spoken_reply
import voice_iat
from websockets.exceptions import ConnectionClosed, WebSocketException
from wechat_case import HOST, SCOPE, TOKEN, esc, wx

FORMAT={"sampleRate":16000,"channels":1,"frameMs":20}
PCM_HEADER=struct.Struct(">4sBBHIIQQI")
END_PAUSE=0.48
TURN_PAUSE=0.9
PCM_FRAME_BYTES = 640
PCM_RESERVE_BYTES = 16 * PCM_FRAME_BYTES
PCM_PRIME_WAIT = 0.200
# v3 accepts variable-size chunks and clocks them out of its own PCM queue.
NATIVE_WRITE_BYTES = 8 * PCM_FRAME_BYTES
NATIVE_QUEUE_SECONDS = 0.560
END_MARK="[[END_CALL]]"
CHIME=bytes(640)+b"".join(struct.pack("<h",round(1300*math.sin(2*math.pi*720*i/16000)
                          *math.sin(math.pi*i/1600)**2)) for i in range(1600))
CALL_STAGES={"dialing":"正在拨号","ringing":"微信响铃中","connecting":"微信正在建立通话",
             "connected":"微信已接通，正在连接音频","uncertain":"微信通话结果待确认",
             "ending":"微信正在结束通话","ended":"微信通话已结束"}
BASE=HOST.replace("http://","ws://",1)+SCOPE+"/calls"
LOCK=Lock()
stop_event=None
active_peer=None
standby={"ready":False,"message":"自动接听已关闭"}
progress={"active":False,"stage":"尚未测试","received":0,"sent":0,"replies":0}


def change(**values):
    with LOCK:
        progress.update(values)


def snapshot():
    with LOCK:
        return dict(progress)


def incoming_peer():
    with st.database() as db:
        if st.get(db,"profile",{}).get("call_auto_answer",False):
            return st.get(db,"peer")
    return None


def is_bound_offer(offer,peer,offer_id=None):
    return bool(offer and offer.get("callId") and offer.get("callerId")==peer
                and offer.get("callType")=="voice" and offer.get("kind","direct")=="direct"
                and (offer_id is None or offer["callId"]==offer_id))


def set_standby(ready,message):
    with LOCK:
        standby.update(ready=ready,message=message)


def standby_status():
    with LOCK:
        return dict(standby)


def remember_offer(peer,offer_id):
    with st.database() as db:
        db.execute("BEGIN IMMEDIATE")
        if st.get(db,"peer")!=peer or not st.get(db,"profile",{}).get("call_auto_answer"):
            return False
        last=st.get(db,"bound_call_receipt",{})
        if last.get("peer")!=peer or last.get("offerId")!=offer_id:
            st.put(db,"bound_call_receipt",{"peer":peer,"offerId":offer_id,"receiptAt":time.time(),"direction":"incoming"})
        return True


async def watch_incoming(sock,peer):
    """Observe offers without reserving the account's outgoing-call slot."""
    await send(sock,"call.hello",supportedProtocolVersions=[3,2])
    hello=json.loads(await asyncio.wait_for(sock.recv(),5))
    if (hello.get("type")!="hello" or hello.get("protocolVersion") not in (2,3)
            or not hello.get("capabilities",{}).get("directAudio")):
        raise ValueError("微信来电接口尚未就绪")
    set_standby(True,"正在等已绑定好友来电")
    offer=next((o for o in (hello.get("currentOffer"),hello.get("currentPendingOffer")) if is_bound_offer(o,peer)),None)
    handled=None
    while await asyncio.to_thread(incoming_peer)==peer:
        if is_bound_offer(offer,peer) and offer["callId"]!=handled:
            handled=offer["callId"]
            if await asyncio.to_thread(remember_offer,peer,handled) and not snapshot()["active"]:
                try:
                    await asyncio.to_thread(start,"answer",expected_peer=peer,expected_offer=handled)
                    set_standby(True,"正在等已绑定好友来电")
                except (OSError,ValueError,TimeoutError):
                    set_standby(False,"来电已到，暂时无法接听，请检查通话配置")
        try:
            event=json.loads(await asyncio.wait_for(sock.recv(),4))
        except TimeoutError:
            continue
        if event.get("type")=="protocol.error":
            raise ValueError("微信来电连接中断")
        offer=event.get("offer") if event.get("type") in ("call.pending","call.offered") else None


async def incoming_connections():
    from websockets.asyncio.client import connect
    while True:
        try:
            peer=await asyncio.to_thread(incoming_peer)
            if peer:
                set_standby(False,"正在连接微信来电")
                headers={"Authorization":"Bearer "+TOKEN.read_text().strip()}
                async with connect(BASE+"/socket",additional_headers=headers,proxy=None,open_timeout=5) as sock:
                    await watch_incoming(sock,peer)
            else:
                set_standby(False,"自动接听未启用或尚未绑定好友")
        except (OSError,ValueError,TimeoutError,WebSocketException,sqlite3.Error):
            set_standby(False,"正在重新连接微信来电")
        await asyncio.sleep(2)


def run_incoming():
    asyncio.run(incoming_connections())


def private_call_note(peer):
    with LOCK:
        latest=dict(progress) if active_peer==peer and progress.get("active") and progress.get("receiptAt") else None
    if latest is None:
        with st.database() as db:
            saved=st.get(db,"bound_call_receipt",{})
        latest=saved if saved.get("peer")==peer else None
    if not latest:
        return "微信通话回执：没有本次接听或接通的记录，线路原因未知。"
    stamp=datetime.fromtimestamp(latest["receiptAt"],conversation_prompt.BEIJING)
    direction="来电" if latest["direction"]=="incoming" else "去电"
    connected=any(e.get("state")=="connected" for e in latest.get("states",[]))
    if connected:
        result="微信已确认接通"+("，本机语音通道已建立" if latest.get("mediaReady") else "")
    elif latest.get("actionAccepted"):
        result=("微信已受理接听" if direction=="来电" else "微信已受理拨号")+"，尚无接通回执"
    else:
        result="已收到来电邀请，尚无接听回执" if direction=="来电" else "尚无拨号回执"
    return f"微信最近一通有回执的{direction}是 {stamp:%m-%d %H:%M:%S}，{result}。只据对应时间的回执说明状态，手机听感和线路原因未知。"


def stop():
    with LOCK:
        if stop_event:
            stop_event.set()
    return {"message":"正在结束本次通话测试"}


def start(mode,*,expected_peer=None,turn_id=None,expected_offer=None):
    global stop_event,active_peer
    if mode not in ("answer","dial"):
        raise ValueError("通话方式无效")
    with st.database() as db:
        peer=st.get(db,"peer")
        cfg=st.profile(db)
    if expected_peer is not None and peer!=expected_peer:
        raise ValueError("微信私聊绑定已改变")
    if expected_offer and (mode!="answer" or not cfg.get("call_auto_answer")):
        raise ValueError("自动接听已关闭")
    if not peer or not wx().get("authenticated"):
        raise ValueError("请先登录并绑定私聊")
    chat_service=providers.service(cfg["chat_service"])
    if not providers.public(chat_service)["configured"]:
        raise ValueError("请先配置对话模型")
    if find_spec("aiohttp"):
        cfg["_call_chat"]=chat_service
    for role in ("asr","tts"):
        if cfg[role]=="off" or not providers.public(providers.service(cfg[role]))["configured"]:
            raise ValueError("请先配置语音识别与合成服务")
    asr=providers.service(cfg["asr"])
    if asr["kind"]=="xfyun" and cfg["tone_service"] in ("off",sensevoice_local.ROUTE):
        cfg["_call_iat"]=asr
    tts=providers.service(cfg["tts"])
    cfg["_call_tts"]=tts
    if tts["kind"]=="elevenlabs" and find_spec("aiohttp"):
        cfg["_call_eleven"]=tts
    cfg["_peer"]=peer
    if expected_offer:
        cfg["_incoming_offer"]=expected_offer
    cfg["_call_history"]=st.recent_dialogue(peer,turn_id,for_call=True)
    if turn_id:
        with st.database() as db:
            row=db.execute("SELECT input FROM turns WHERE id=? AND peer=?",(turn_id,peer)).fetchone()
        if row and row[0]:
            cfg["_call_history"].append({"role":"user","content":row[0][:2000]})
    if not cfg["voice"] or (providers.service(cfg["tts"])["kind"]!="deepseek" and not cfg["tts_model"]):
        raise ValueError("请先选择合成模型和音色")
    if not wx("/calls/current").get("capabilities",{}).get("directAudio"):
        raise ValueError("这个微信会话当前没有一对一通话能力")
    with LOCK:
        if progress["active"]:
            raise ValueError("已有一项通话测试进行中")
        stop_event=Event()
        flag=stop_event
        active_peer=peer
        progress.clear()
        progress.update(active=True,stage="准备微信通话",received=0,sent=0,replies=0,states=[],
                        direction="incoming" if mode=="answer" else "outgoing",receiptAt=None,actionAccepted=False,mediaReady=False,
                        endReason="",hangupSent=False,interruptions=0,sendGeneration=None,latencyMs={},
                        turnTimings=[],greetingMs=None,joinedDrafts=0,maxFeedGapMs=0)
    def run():
        try:
            asyncio.run(session(mode,peer,cfg,flag))
        except (OSError,TimeoutError):
            change(stage="通话连接超时或中断")
        except ConnectionClosed:
            change(stage="微信通话控制连接已结束")
        except Exception as exc:
            change(stage=str(exc)[:90] if isinstance(exc,ValueError) else "通话接口异常（"+type(exc).__name__+"）")
        finally:
            with LOCK:
                progress["active"]=False
                saved=dict(progress)
                with st.database() as db:
                    st.put(db,"last_call",saved)
                    last=st.get(db,"bound_call_receipt",{})
                    if saved.get("receiptAt") and saved["receiptAt"]>=last.get("receiptAt",0):
                        receipt={k:saved[k] for k in ("receiptAt","direction","actionAccepted","mediaReady","states")}
                        st.put(db,"bound_call_receipt",{**receipt,"peer":peer,"offerId":saved.get("receiptId")})
                    if turn_id:
                        connected=any(v["state"]=="connected" for v in saved["states"])
                        db.execute("UPDATE turns SET status=?,phase='call',answer=?,error=? WHERE id=?",
                                   ("call_connected" if connected else "failed","微信电话已接通" if connected else None,
                                    None if connected else saved["stage"],turn_id))
    Thread(target=run,daemon=True).start()
    return {"message":"等待已绑定联系人来电" if mode=="answer" else "正在拨给已绑定联系人"}


async def send(sock,kind,**data):
    await sock.send(json.dumps({"type":kind,**data},separators=(",",":")))


def pcm_frame(track_id,generation,samples):
    return PCM_HEADER.pack(b"WXCM",3,1,0,track_id,generation,0,0,len(samples))+samples


def read_frame(frame):
    if len(frame)<PCM_HEADER.size:
        raise ValueError("通话音频帧不完整")
    magic,version,kind,flags,track_id,generation,_,_,size=PCM_HEADER.unpack_from(frame)
    if magic!=b"WXCM" or version!=3 or len(frame)!=PCM_HEADER.size+size:
        raise ValueError("通话音频格式不匹配")
    if kind!=1:
        return None
    if flags or not 0<size<=65536 or size%2:
        raise ValueError("通话采样数据不完整")
    return track_id,generation,frame[PCM_HEADER.size:]


class Signal:
    def __init__(self,sock,flag):
        self.sock=sock
        self.flag=flag
        self.hello={}
        self.call={}
        self.pending=self.offer=None
        self.owned=None
        self.token=None
        self.states=[]
        self.send_generations = {}
        self.clear_pending = None
        self.audio_ready = asyncio.Event()
        self.audio_ready.set()
        self.started=time.monotonic()
        self.connected_at=None
        self.end_reason="管理页请求结束"

    def observe_send_track(self, track):
        track_id, generation = track["id"], track["generation"]
        self.send_generations[track_id] = max(generation, self.send_generations.get(track_id, 0))
        if self.clear_pending:
            _, pending_track, old_generation = self.clear_pending
            if pending_track == track_id and generation > old_generation:
                self.finish_clear()

    def finish_clear(self):
        self.clear_pending = None
        self.audio_ready.set()

    async def clear_audio(self, track_id):
        request_id = uuid4().hex
        self.clear_pending = (request_id, track_id, self.send_generations[track_id])
        self.audio_ready.clear()
        await send(self.sock, "call.clear_audio", requestId=request_id, callId=self.owned)

    def record(self, state):
        if state in CALL_STAGES and (not self.states or self.states[-1]["state"]!=state):
            if state=="connected" and self.connected_at is None:
                self.connected_at=time.monotonic()
            self.states.append({"state":state,"seconds":round(time.monotonic()-self.started,1)})
            change(stage=CALL_STAGES[state],states=list(self.states))

    def observe(self,e):
        kind=e.get("type")
        if kind=="hello":
            self.hello=e
            self.pending,self.offer=e.get("currentPendingOffer"),e.get("currentOffer")
            c=e.get("currentCall") or {}
            if c: self.call[c["callId"]]=c
        elif kind=="call.pending":
            self.pending=e.get("offer")
        elif kind=="call.offered":
            self.offer=e.get("offer")
        elif kind=="call.state":
            c=e["call"]
            self.call[c["callId"]]=c
            if c["callId"]==self.owned:
                self.record(c.get("state"))
        elif kind in ("track.added", "track.updated") and self.owned and e.get("callId") == self.owned:
            track = e["track"]
            if track.get("media", "audio") == "audio" and track.get("direction") == "send":
                self.observe_send_track(track)
        elif kind == "command.result" and self.clear_pending and e.get("requestId") == self.clear_pending[0]:
            if e.get("status") not in ("accepted", "replayed"):
                self.finish_clear()
        elif kind=="protocol.error":
            raise ValueError("微信通话控制协议协商失败")

    async def next(self,deadline):
        while time.monotonic()<deadline:
            if self.flag.is_set():
                raise ValueError("通话测试已结束")
            try:
                event=json.loads(await asyncio.wait_for(self.sock.recv(),.5))
            except asyncio.TimeoutError:
                continue
            self.observe(event)
            return event
        raise TimeoutError()

    async def command(self,kind,**payload):
        req=uuid4().hex
        await send(self.sock,kind,requestId=req,**payload)
        until=time.monotonic()+8
        while True:
            e=await self.next(until)
            if e.get("type")=="command.result" and e.get("requestId")==req:
                if e.get("status") not in ("accepted","replayed"):
                    raise ValueError("微信通话请求未获接受（"+str(e.get("code") or "unknown")+"）")
                return e

    async def claim(self,mode,peer,*,expected_offer=None):
        await send(self.sock,"call.hello",supportedProtocolVersions=[3,2])
        until=time.monotonic()+5
        while not self.hello:
            await self.next(until)
        if self.hello.get("protocolVersion") not in (2,3) or not self.hello.get("capabilities",{}).get("directAudio"):
            raise ValueError("当前账号未开放一对一语音")
        if mode=="dial":
            if (self.hello.get("availability") or {}).get("busy"):
                raise ValueError("微信账号正在通话")
            await self.command("call.ready",ready=True,format=FORMAT,scope={"kind":"outgoing"})
            change(stage="等待对方接听")
            result=await self.command("call.dial",target={"kind":"direct","contactId":peer},callType="voice")
            change(receiptAt=time.time(),receiptId=result.get("callId"))
        else:
            change(stage="等待已绑定联系人来电")
            until=time.monotonic()+(12 if expected_offer else 120)
            prepared=None
            if expected_offer and not any(is_bound_offer(o,peer,expected_offer) for o in (self.pending,self.offer)):
                raise ValueError("这通来电已撤回")
            while not is_bound_offer(self.offer,peer,expected_offer):
                if is_bound_offer(self.pending,peer,expected_offer):
                    current=(self.pending["callId"],self.pending["revision"])
                    if prepared!=current:
                        prepared=current
                        change(receiptAt=time.time(),receiptId=current[0])
                        await self.command("call.ready",ready=True,format=FORMAT,
                                           scope={"kind":"offer","callId":current[0],"expectedRevision":current[1]})
                        continue
                try:
                    await self.next(until)
                except TimeoutError:
                    if expected_offer or prepared:
                        raise ValueError("来电已到，等候接听的邀请已过期") from None
                    raise ValueError("两分钟内没有收到已绑定好友的来电") from None
            if expected_offer and await asyncio.to_thread(incoming_peer)!=peer:
                raise ValueError("这通来电的自动接听已关闭")
            change(receiptAt=time.time(),receiptId=self.offer["callId"])
            result=await self.command("call.answer",callId=self.offer["callId"],expectedRevision=self.offer["revision"])
        self.owned,self.token=result.get("callId"),result.get("ownerToken")
        if not self.owned or not self.token:
            raise ValueError("微信没有授予此通话的音频控制")
        change(actionAccepted=True)
        until=float("inf")
        while True:
            state=self.call.get(self.owned,{}).get("state")
            self.record(state)
            if state=="connected":
                return
            if state=="ended":
                if any(e["state"]=="connecting" for e in self.states):
                    raise ValueError("微信在建立连接时结束，音频尚未接通")
                raise ValueError("微信结束了呼叫，服务器未收到接通信号")
            await self.next(until)

    async def watch_end(self):
        while self.call.get(self.owned,{}).get("state")!="ended" and not self.flag.is_set():
            try:
                await self.next(float("inf"))
            except ValueError:
                if not self.flag.is_set(): raise
        return

    async def release(self, await_ack=False):
        c=self.call.get(self.owned,{})
        if self.owned and not c.get("revision"):
            latest=(await asyncio.to_thread(wx,"/calls/current")).get("currentCall") or {}
            if latest.get("callId")==self.owned:
                c=latest
        if self.owned and c.get("revision") and c.get("state") not in ("ending","ended"):
            cmd="call.hangup" if c.get("state")=="connected" or c.get("direction")=="incoming" else "call.cancel"
            req=uuid4().hex
            await send(self.sock,cmd,requestId=req,callId=self.owned,expectedRevision=c["revision"])
            change(hangupSent=True,endReason=self.end_reason)
            if await_ack:
                deadline=time.monotonic()+6
                while time.monotonic()<deadline:
                    try:
                        ev=json.loads(await asyncio.wait_for(self.sock.recv(),max(.01,deadline-time.monotonic())))
                    except (TimeoutError,ConnectionClosed):
                        break
                    self.observe(ev)
                    if ev.get("requestId")==req or self.call.get(self.owned,{}).get("state")=="ended":
                        break


class SpeechBuffer:
    """Collect mono 20 ms frames into bounded utterances."""
    def __init__(self):
        self.leading=deque(maxlen=5)
        self.frames=[]
        self.silence=0
        self.voiced=0
        self.run=0
        self.confirmed=False
        self.last=time.monotonic()
        self.last_loud=self.last

    def feed(self,frame):
        self.last=time.monotonic()
        loud=sum(v*v for (v,) in struct.iter_unpack("<h",frame))>len(frame)//2*420**2
        if loud: self.last_loud=self.last
        if not self.frames and not loud:
            self.leading.append(frame)
            return None
        if not self.frames:
            self.frames=list(self.leading)
            self.leading.clear()
        self.frames.append(frame)
        self.silence=0 if loud else self.silence+1
        self.voiced+=int(loud)
        self.run=self.run+1 if loud else 0
        if self.run>=6: self.confirmed=True
        if self.silence>=round(END_PAUSE/.02) or len(self.frames)>=850:
            return self.flush()
        return None

    def flush(self):
        result=b"".join(self.frames) if self.confirmed else None
        self.frames=[]
        self.silence=self.voiced=0
        self.run=0
        self.confirmed=False
        return result


def end_decision(answer):
    """Only a final action line in the assistant's reply can end an audible turn."""
    lines = answer.strip().splitlines()
    marked = bool(lines and lines[-1].strip() == END_MARK and "```" not in answer)
    words = "\n".join(lines[:-1] if marked else lines).replace(END_MARK, "").strip()
    return words, "助手已道别" if marked and words else ""


def recent_call_turns(history, limit):
    """An interrupted reply can leave a user turn with no assistant message."""
    start = len(history)
    for index in range(len(history) - 1, -1, -1):
        if limit <= 0:
            break
        if history[index]["role"] == "user":
            start = index
            limit -= 1
    return history[start:]


CALL_INSTRUCTIONS = (
    "微信电话已接通。你读到的是对方刚说的话，回复会念给对方听。"
    "按最后补充或纠正的意思自然接话。听写有分歧时结合前后文，关键信息听不清才简短确认。只凭转写无法判断声源或网络故障。"
    f"想挂断时先道别，最后单独写一行 {END_MARK}，说完再挂。"
)


def call_messages(cfg, history, current, *, notes=(), now=None):
    limit=cfg.get("history_turns",st.DEFAULT["history_turns"])
    instructions=providers.voice_instructions(cfg,CALL_INSTRUCTIONS)
    return conversation_prompt.build(cfg,instructions,recent_call_turns(history,limit),current,notes=notes,now=now,private=True)


def alternative_hearing(primary, local):
    """Flag a material disagreement on a short utterance without rewriting what was heard."""
    def letters(text):
        return "".join(c for c in text.casefold() if c.isalnum())
    first,second=letters(primary),letters(local)
    if not first or not second or max(len(first),len(second))>10 or first==second:
        return ""
    return local if SequenceMatcher(None,first,second).ratio()<.8 else ""


async def draft_until_speech(cfg,messages,new_speech):
    request=asyncio.create_task(providers.call_chat(cfg,messages))
    heard=asyncio.create_task(new_speech.wait())
    try:
        await asyncio.wait((request,heard),return_when=asyncio.FIRST_COMPLETED)
        return None if new_speech.is_set() else request.result()
    finally:
        heard.cancel()
        request.cancel()
        await asyncio.gather(request,heard,return_exceptions=True)


async def buffered_call_audio(source, reserve_bytes=PCM_RESERVE_BYTES, prime_wait=PCM_PRIME_WAIT):
    """Read synthesis independently of the playback clock; keep the buffer bounded."""
    if isinstance(source, bytes):
        if source:
            yield source
        return

    queue = asyncio.Queue(maxsize=8)

    async def receive():
        try:
            async for block in source:
                if block:
                    await queue.put(block)
        except Exception as error:
            await queue.put(error)
        else:
            await queue.put(None)

    reader = asyncio.create_task(receive())
    try:
        block = await queue.get()
        reserve = bytearray()
        deadline = asyncio.get_running_loop().time() + prime_wait
        while isinstance(block, bytes):
            reserve.extend(block)
            if len(reserve) >= reserve_bytes:
                break
            try:
                async with asyncio.timeout_at(deadline):
                    block = await queue.get()
            except TimeoutError:
                break
        if reserve:
            yield bytes(reserve)
        while isinstance(block, bytes):
            block = await queue.get()
            if isinstance(block, bytes):
                yield block
        if isinstance(block, Exception):
            raise block
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def call_audio_writes(source, queue_in_daemon):
    """Preserve sample order across provider chunks; flush the final partial daemon frame."""
    chunk_bytes = NATIVE_WRITE_BYTES if queue_in_daemon else PCM_FRAME_BYTES
    reserve_bytes = round(NATIVE_QUEUE_SECONDS * pcm_audio.BYTES_PER_SECOND) if queue_in_daemon else PCM_RESERVE_BYTES
    prime_wait = .350 if queue_in_daemon else PCM_PRIME_WAIT
    tail = b""
    async with aclosing(buffered_call_audio(source, reserve_bytes, prime_wait)) as blocks:
        async for block in blocks:
            data = tail + block
            whole = len(data) - len(data) % PCM_FRAME_BYTES
            for start in range(0, whole, chunk_bytes):
                yield data[start:min(start + chunk_bytes, whole)]
            tail = data[whole:]
    if len(tail) % 2:
        raise ValueError("通话音频在半个采样处结束")
    if tail:
        yield tail.ljust(PCM_FRAME_BYTES, b"\0")


async def conversation(signal,media,ready,cfg,flag,greeting=None,*,own_control=True,history=None):
    binary=ready.get("protocolVersion")==3
    track=(next((t for t in ready.get("sendTracks",[]) if t.get("media")=="audio"),None)
           if binary else ready.get("sendTrack"))
    if not track or track.get("format")!=FORMAT:
        raise ValueError("微信通话音频格式不匹配")
    signal.observe_send_track(track)
    # A fresh attachment supplies the daemon's current generation, even after a missed update.
    signal.finish_clear()
    receive={t["id"]:t["generation"] for t in ready["receiveTracks"] if t.get("media","audio")=="audio"}
    audio=SpeechBuffer()
    pending=bytearray()
    queue=asyncio.Queue()
    history=[] if history is None else history
    totals={k:snapshot().get(k,0) for k in ("received","sent","replies","interruptions","joinedDrafts","maxFeedGapMs")}
    timings=deque(snapshot().get("turnTimings",[]),maxlen=60)
    started=None
    epoch=0
    barge_in=cfg.get("call_barge_in",st.DEFAULT["call_barge_in"])
    advanced=asyncio.Event()
    speech_began=asyncio.Event()
    playing=False
    live=None
    cue_input=cue_result=None
    iat_tasks=set()
    voice_preparation=None
    voice_used_at=None

    def prepare_voice():
        nonlocal voice_preparation,voice_used_at
        if not cfg.get("_call_eleven") or not cfg.get("_tts_client"):
            return
        now=time.monotonic()
        if (voice_preparation and not voice_preparation.done()) or (voice_used_at is not None and now-voice_used_at<25):
            return
        voice_used_at=now
        voice_preparation=asyncio.create_task(providers.prepare_call_voice(cfg["_tts_client"],cfg))

    def enqueue(raw):
        nonlocal live,cue_input,cue_result
        if not raw: return
        if live: live.frames.put_nowait(None)
        if cue_input is not None:
            cue_input.set_result(raw)
        queue.put_nowait((epoch,raw,live,cue_result,time.monotonic(),audio.last_loud))
        live=None
        cue_input=cue_result=None

    def updated(ev):
        if ev.get("type") in ("track.updated","track.added"):
            t=ev["track"]
            if t.get("media","audio")!="audio": return
            if t.get("direction")=="send" and t["id"]==track["id"]:
                signal.observe_send_track(t)
            elif t.get("direction")=="receive":
                receive[t["id"]]=t["generation"]
        elif ev.get("type")=="track.removed":
            receive.pop(ev.get("trackId"),None)

    async def listen():
        nonlocal epoch,advanced,live,cue_input,cue_result
        while not flag.is_set():
            try:
                message=await asyncio.wait_for(media.recv(),.2)
            except asyncio.TimeoutError:
                if audio.frames and time.monotonic()-audio.last>=END_PAUSE: enqueue(audio.flush())
                continue
            if isinstance(message,bytes) and binary:
                frame=read_frame(message)
                if frame is None: continue
                track_id,generation,samples=frame
            else:
                ev=json.loads(message)
                if ev.get("type")=="protocol.error":
                    raise ValueError("微信拒绝了通话音频，代码 "+str(ev.get("code","unknown"))[:48])
                updated(ev)
                if binary or ev.get("type")!="audio.chunk": continue
                track_id,generation=ev.get("trackId"),ev.get("generation")
                samples=base64.b64decode(ev["pcmBase64"],validate=True)
            if generation!=receive.get(track_id): continue
            pending.extend(samples)
            while len(pending)>=640:
                frame=bytes(pending[:640])
                del pending[:640]
                was=audio.confirmed
                raw=audio.feed(frame)
                if audio.confirmed and not was:
                    speech_began.set()
                    prepare_voice()
                    advanced.set()
                    advanced=asyncio.Event()
                    epoch+=1
                    if cfg.get("tone_service")==sensevoice_local.ROUTE:
                        cue_input=Future()
                        cue_result=sensevoice_local.start(cue_input,with_words=True,
                                                         language=cfg.get("asr_language") if cfg.get("_call_iat") else None)
                    if playing and barge_in:
                        totals["interruptions"]+=1
                        await signal.clear_audio(track["id"])
                        change(stage="正在听你说",**totals)
                    if cfg.get("_call_iat"):
                        live=voice_iat.LiveDictation(cfg)
                        iat_tasks.add(live.task)
                        live.task.add_done_callback(iat_tasks.discard)
                        live.frames.put_nowait(b"".join(audio.frames))
                elif live:
                    live.frames.put_nowait(frame)
                enqueue(raw)
                totals["received"]+=1
            if totals["received"]%25==0: change(received=totals["received"])

    async def play(out,turn,timing=None):
        nonlocal started,playing
        if turn!=epoch or flag.is_set(): return False
        interrupted=advanced
        committed=False
        queue_in_daemon=binary and not barge_in
        async def pump():
            nonlocal started,playing,committed
            first=True
            deadline=None
            async with aclosing(call_audio_writes(out,queue_in_daemon)) as writes:
                async for samples in writes:
                    if first:
                        # Old-generation PCM is silently dropped by the WeChat daemon.
                        try:
                            await asyncio.wait_for(signal.audio_ready.wait(), 6)
                        except TimeoutError:
                            raise RuntimeError("微信未确认发送音轨，重新连接音频") from None
                        if timing:
                            await asyncio.sleep(max(0,timing[0]+TURN_PAUSE-time.monotonic()))
                        deadline=time.monotonic()
                    duration=len(samples)/pcm_audio.BYTES_PER_SECOND
                    if queue_in_daemon:
                        # The native client consumes this reserve even if Python or TTS stalls.
                        await asyncio.sleep(max(0,deadline+duration-NATIVE_QUEUE_SECONDS-time.monotonic()))
                    if flag.is_set() or (turn!=epoch and (barge_in or not committed)): return False
                    generation = signal.send_generations[track["id"]]
                    if started != generation:
                        await send(media,"track.start" if binary else "audio.start",trackId=track["id"],generation=generation)
                        started=generation
                        change(sendGeneration=generation)
                    if first and turn!=epoch: return False
                    playing=committed=True
                    if binary:
                        await media.send(pcm_frame(track["id"],started,samples))
                    else:
                        await send(media,"audio.chunk",trackId=track["id"],generation=started,
                                   pcmBase64=base64.b64encode(samples).decode())
                    now=time.monotonic()
                    if first and timing is not None:
                        last_voice,sealed,dequeued,transcribed,written=timing
                        latency={"停顿":round((sealed-last_voice)*1000),
                                          "排队":round((dequeued-sealed)*1000),
                                          "听写收尾":round((transcribed-dequeued)*1000),
                                          "生成回复":round((written-transcribed)*1000),
                                          "合成首帧":round((now-written)*1000),
                                          "到第一帧":round((now-last_voice)*1000)}
                        timings.append(dict(turn=turn,**latency))
                        change(latencyMs=latency,turnTimings=list(timings))
                    elif first and turn==0 and signal.connected_at is not None:
                        change(greetingMs=round((now-signal.connected_at)*1000))
                    if not first:
                        totals["maxFeedGapMs"]=max(totals["maxFeedGapMs"],round((now-deadline)*1000))
                    first=False
                    totals["sent"]+=len(samples)//PCM_FRAME_BYTES
                    deadline=max(deadline,now)+duration
                    if not queue_in_daemon:
                        # Keep interruptible and v2 playback responsive to a generation change.
                        await asyncio.sleep(max(.004,deadline-.040-time.monotonic()))
            if deadline is not None:
                await asyncio.sleep(max(0,deadline-time.monotonic()))
            return committed and (not barge_in or turn==epoch) and not flag.is_set()
        task=asyncio.create_task(pump())
        interrupt=asyncio.create_task(interrupted.wait())
        try:
            done,_=await asyncio.wait((task,interrupt),return_when=asyncio.FIRST_COMPLETED)
            if task in done or (committed and not barge_in):
                return await task
            return False
        finally:
            task.cancel(); interrupt.cancel()
            await asyncio.gather(task,interrupt,return_exceptions=True)
            if not isinstance(out,bytes):
                await out.aclose()
            playing=False
            change(**totals)

    async def transcribe(raw,stream,cue):
        if not stream and cfg.get("tone_service","off") not in ("off",sensevoice_local.ROUTE):
            words,tone,_,_=await asyncio.to_thread(providers.hear_utterance,cfg,raw)
            return providers.with_tone(words,tone),""
        try:
            words=await stream.transcript() if stream else await asyncio.to_thread(providers.recognize_pcm,cfg,raw)
        except (OSError,ValueError,TimeoutError,ConnectionClosed):
            words=""
        tone=local=""
        if cue:
            try:
                tone,_,local=await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(cue)),.12)
            except TimeoutError: pass
        if not words and not local:
            return "〔这段语音没听清〕",""
        return providers.with_tone(words or local,tone),alternative_hearing(words,local)

    async def call_output(delivery):
        nonlocal voice_used_at
        if cfg.get("_tts_client"):
            try:
                async with aclosing(providers.stream_call_pcm(cfg["_tts_client"],cfg,delivery)) as stream:
                    async for block in stream:
                        yield block
            finally:
                voice_used_at=time.monotonic()
        else:
            yield await asyncio.to_thread(providers.synthesize_pcm,cfg,delivery)

    async def reply():
        if greeting is not None:
            welcome=asyncio.ensure_future(greeting)
            heard=asyncio.create_task(speech_began.wait())
            try:
                done,_=await asyncio.wait((welcome,heard),return_when=asyncio.FIRST_COMPLETED)
                hello=welcome.result() if welcome in done and not heard.done() else None
            except (OSError,ValueError,TimeoutError): hello=None
            finally:
                heard.cancel()
                welcome.cancel()
                await asyncio.gather(heard,welcome,return_exceptions=True)
            if hello and epoch==0:
                await play(hello+(CHIME if cfg.get("call_cue")=="on" else b""),0)
        fragments=[]
        while not flag.is_set():
            turn,raw,stream,cue,sealed,last_voice=await queue.get()
            dequeued=time.monotonic()
            try:
                fragments.append(await transcribe(raw,stream,cue))
                transcribed=time.monotonic()
                if turn!=epoch: continue
                msg={"role":"user","content":"\n".join(words for words,_ in fragments)}
                notes=[f"第{n}段听写有分歧，本机听成「{alt}」。" for n,(_,alt) in enumerate(fragments,1) if alt]
                draft=await draft_until_speech(cfg,call_messages(cfg,history,msg["content"],notes=notes),advanced)
                if draft is None or turn!=epoch:
                    totals["joinedDrafts"]+=1
                    continue
                answer,finish=end_decision(draft)
                if not answer: continue
                spoken=spoken_reply.parse(answer)
                answer=spoken.words
                delivery=spoken.delivery if providers.has_audio_tags(cfg) else answer
                written=time.monotonic()
            except (OSError,ValueError,TimeoutError,KeyError,IndexError,ConnectionClosed):
                change(stage="通话中，正在等你继续说")
                continue
            sent_before=totals["sent"]
            try:
                complete=await play(call_output(delivery),turn,(last_voice,sealed,dequeued,transcribed,written))
            except (OSError,ValueError,TimeoutError):
                change(stage="语音合成暂时中断，通话保持中")
                complete=False
            if totals["sent"]>sent_before:
                fragments.clear()
                history.append(msg)
                if complete:
                    history.append({"role":"assistant","content":answer})
                history[:]=recent_call_turns(history,cfg.get("history_turns",st.DEFAULT["history_turns"]))
            elif turn!=epoch:
                totals["joinedDrafts"]+=1
            if not complete: continue
            totals["replies"]+=1
            if cfg.get("_peer"):
                await asyncio.to_thread(st.remember_call_line,cfg["_peer"],msg["content"],answer)
            if finish:
                # Leave enough time to hear a brief correction or interruption.
                if epoch==turn:
                    await asyncio.sleep(.65)
                if epoch==turn and not audio.confirmed:
                    signal.end_reason=finish
                    flag.set()
            elif cfg.get("call_cue")=="on" and turn==epoch:
                await play(CHIME,turn)
            change(**totals)

    prepare_voice()
    tasks=[asyncio.create_task(listen()),asyncio.create_task(reply())]
    if own_control: tasks.append(asyncio.create_task(signal.watch_end()))
    try:
        done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for t in done: t.result()
    finally:
        if cue_input is not None and not cue_input.done():
            cue_input.set_result(b"")
        if voice_preparation is not None:
            tasks.append(voice_preparation)
        for t in tasks+list(iat_tasks): t.cancel()
        await asyncio.gather(*tasks,*iat_tasks,return_exceptions=True)
        change(**totals)


async def connected_session(signal,cfg,flag,greeting,connect,headers):
    version=signal.hello["protocolVersion"]
    endpoint=BASE+"/"+esc(signal.owned)+"/media"
    history=list(cfg.get("_call_history",[]))
    async def audio_loop():
        nonlocal greeting
        delay=.5
        while not flag.is_set():
            try:
                async with connect(endpoint,additional_headers=headers,proxy=None,open_timeout=6) as media:
                    await send(media,"media.attach",supportedProtocolVersions=[version],clientId=signal.hello["clientId"],ownerToken=signal.token)
                    ready=json.loads(await asyncio.wait_for(media.recv(),6))
                    if ready.get("type")!="media.ready" or ready.get("protocolVersion")!=version:
                        raise ValueError("通话音频未就绪")
                    delay=.5
                    change(stage="已接通，等待讲话",mediaReady=True)
                    first,greeting=greeting,None
                    await conversation(signal,media,ready,cfg,flag,first,own_control=False,history=history)
            except Exception as error:
                # Media and provider failures never own the control socket.
                # Its independent watcher processes the remote end or operator action.
                if flag.is_set(): return
                change(stage="微信通话保持中，正在恢复声音",mediaIssue=type(error).__name__)
                await asyncio.sleep(delay)
                delay=min(4,delay*2)
    watcher=asyncio.create_task(signal.watch_end())
    audio=asyncio.create_task(audio_loop())
    try:
        done,_=await asyncio.wait([watcher,audio],return_when=asyncio.FIRST_COMPLETED)
        for t in done: t.result()
    finally:
        watcher.cancel()
        audio.cancel()
        await asyncio.gather(watcher,audio,return_exceptions=True)


GREETING="喂，听得见吗？"


def greeting_cache(key, pcm=None):
    """Only the fixed greeting is cached, with one entry for the current voice."""
    try:
        with st.database() as db:
            if pcm is not None:
                st.put(db,"call_greeting_pcm",{"key":key,"pcm":base64.b64encode(pcm).decode("ascii")})
            else:
                saved=st.get(db,"call_greeting_pcm",{})
                if saved.get("key")==key:
                    return pcm_audio.checked(base64.b64decode(saved["pcm"],validate=True),15)
    except (sqlite3.Error,ValueError,KeyError,TypeError):
        pass
    return None


async def greeting_pcm(cfg):
    route=cfg.get("_call_tts",{})
    key=sha256(json.dumps([route.get("id"),route.get("base"),cfg.get("tts_model"),
                           cfg.get("voice"),GREETING,FORMAT],sort_keys=True).encode()).hexdigest()
    if route:
        cached=await asyncio.to_thread(greeting_cache,key)
        if cached:
            return cached
    if cfg.get("_tts_client"):
        audio=bytearray()
        async for block in providers.stream_call_pcm(cfg["_tts_client"],cfg,GREETING):
            audio.extend(block)
        pcm=bytes(audio)
    else:
        pcm=await asyncio.to_thread(providers.synthesize_pcm,cfg,GREETING)
    if route and 0<len(pcm)<=15*pcm_audio.BYTES_PER_SECOND:
        await asyncio.to_thread(greeting_cache,key,pcm)
    return pcm


async def session(mode,peer,cfg,flag):
    from websockets.asyncio.client import connect
    headers={"Authorization":"Bearer "+TOKEN.read_text().strip()}
    async with connect(BASE+"/socket",additional_headers=headers,proxy=None,open_timeout=6) as ctl, AsyncExitStack() as resources:
        if cfg.get("_call_eleven") or cfg.get("_call_chat"):
            import aiohttp
            client=await resources.enter_async_context(aiohttp.ClientSession(
                trust_env=False, connector=aiohttp.TCPConnector(keepalive_timeout=75),
                timeout=aiohttp.ClientTimeout(total=50,sock_connect=8,sock_read=15)))
            cfg=dict(cfg,_chat_client=client)
            if cfg.get("_call_eleven"):
                cfg["_tts_client"]=client
        signal=Signal(ctl,flag)
        greeting=None
        try:
            if mode=="dial" or cfg.get("_incoming_offer"):
                greeting=asyncio.create_task(greeting_pcm(cfg))
            await signal.claim(mode,peer,expected_offer=cfg.get("_incoming_offer"))
            if greeting is None:
                greeting=asyncio.create_task(greeting_pcm(cfg))
            await connected_session(signal,cfg,flag,greeting,connect,headers)
            if not flag.is_set():
                change(stage="微信通话已结束",endReason="微信已结束，未报告挂断方")
        except ConnectionClosed:
            change(stage="微信控制连接已断开",endReason="通话控制连接关闭")
        finally:
            try:
                if flag.is_set() and signal.owned:
                    await signal.release(await_ack=True)
                    change(stage="通话已结束",endReason=signal.end_reason)
            finally:
                if greeting is not None:
                    greeting.cancel()
                    await asyncio.gather(greeting,return_exceptions=True)
