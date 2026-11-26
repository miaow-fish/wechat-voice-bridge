"""Bound-peer WeChat voice calls over the public v2 and v3 call APIs."""
import asyncio
import base64
from collections import deque
from concurrent.futures import Future
import json
from importlib.util import find_spec
import math
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
from websockets.exceptions import ConnectionClosed
from wechat_case import HOST, SCOPE, TOKEN, esc, wx

FORMAT={"sampleRate":16000,"channels":1,"frameMs":20}
PCM_HEADER=struct.Struct(">4sBBHIIQQI")
END_PAUSE=0.48
TURN_PAUSE=0.9
END_MARK="[[END_CALL]]"
CHIME=bytes(640)+b"".join(struct.pack("<h",round(1300*math.sin(2*math.pi*720*i/16000)
                          *math.sin(math.pi*i/1600)**2)) for i in range(1600))
CALL_STAGES={"dialing":"正在拨号","ringing":"微信响铃中","connecting":"微信正在建立通话",
             "connected":"微信已接通，正在连接音频","uncertain":"微信通话结果待确认",
             "ending":"微信正在结束通话","ended":"微信通话已结束"}
BASE=HOST.replace("http://","ws://",1)+SCOPE+"/calls"
LOCK=Lock()
stop_event=None
progress={"active":False,"stage":"尚未测试","received":0,"sent":0,"replies":0}


def change(**values):
    with LOCK:
        progress.update(values)


def snapshot():
    with LOCK:
        return dict(progress)


def stop():
    with LOCK:
        if stop_event:
            stop_event.set()
    return {"message":"正在结束本次通话测试"}


def start(mode,*,expected_peer=None,turn_id=None):
    global stop_event
    if mode not in ("answer","dial"):
        raise ValueError("通话方式无效")
    with st.database() as db:
        peer=st.get(db,"peer")
        cfg=st.profile(db)
    if expected_peer is not None and peer!=expected_peer:
        raise ValueError("微信私聊绑定已改变")
    if not peer or not wx().get("authenticated"):
        raise ValueError("请先登录并绑定私聊")
    if not providers.public(providers.service(cfg["chat_service"]))["configured"]:
        raise ValueError("请先配置对话模型")
    for role in ("asr","tts"):
        if cfg[role]=="off" or not providers.public(providers.service(cfg[role]))["configured"]:
            raise ValueError("请先配置语音识别与合成服务")
    asr=providers.service(cfg["asr"])
    if asr["kind"]=="xfyun" and cfg["tone_service"] in ("off",sensevoice_local.ROUTE):
        cfg["_call_iat"]=asr
    tts=providers.service(cfg["tts"])
    if tts["kind"]=="elevenlabs" and find_spec("aiohttp"):
        cfg["_call_eleven"]=tts
    cfg["_peer"]=peer
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
        progress.update(active=True,stage="准备微信通话",received=0,sent=0,replies=0,states=[],
                        endReason="",hangupSent=False,interruptions=0)
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
        self.started=time.monotonic()
        self.end_reason="管理页请求结束"

    def record(self, state):
        if state in CALL_STAGES and (not self.states or self.states[-1]["state"]!=state):
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

    async def claim(self,mode,peer):
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
        else:
            change(stage="等待已绑定联系人来电")
            until=time.monotonic()+120
            prepared=None
            def permitted(offer):
                return bool(offer and offer.get("callerId")==peer and offer.get("callType")=="voice" and offer.get("kind","direct")=="direct")
            while not permitted(self.offer):
                if permitted(self.pending):
                    current=(self.pending["callId"],self.pending["revision"])
                    if prepared!=current:
                        prepared=current
                        await self.command("call.ready",ready=True,format=FORMAT,
                                           scope={"kind":"offer","callId":current[0],"expectedRevision":current[1]})
                        continue
                await self.next(until)
            result=await self.command("call.answer",callId=self.offer["callId"],expectedRevision=self.offer["revision"])
        self.owned,self.token=result.get("callId"),result.get("ownerToken")
        if not self.owned or not self.token:
            raise ValueError("微信没有授予此通话的音频控制")
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
    f"想挂断时先道别，最后单独写一行 {END_MARK}，说完再挂。"
)


def call_messages(cfg, history, current, *, now=None):
    limit=cfg.get("history_turns",st.DEFAULT["history_turns"])
    instructions=providers.voice_instructions(cfg,CALL_INSTRUCTIONS)
    return conversation_prompt.build(cfg,instructions,recent_call_turns(history,limit),current,now=now,private=True)


async def conversation(signal,media,ready,cfg,flag,greeting=None,*,own_control=True,history=None):
    binary=ready.get("protocolVersion")==3
    track=(next((t for t in ready.get("sendTracks",[]) if t.get("media")=="audio"),None)
           if binary else ready.get("sendTrack"))
    if not track or track.get("format")!=FORMAT:
        raise ValueError("微信通话音频格式不匹配")
    receive={t["id"]:t["generation"] for t in ready["receiveTracks"] if t.get("media","audio")=="audio"}
    audio=SpeechBuffer()
    pending=bytearray()
    queue=asyncio.Queue()
    history=[] if history is None else history
    totals={k:snapshot().get(k,0) for k in ("received","sent","replies","interruptions")}
    started=None
    epoch=0
    advanced=asyncio.Event()
    playing=False
    live=None
    cue_input=cue_result=None
    iat_tasks=set()

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
                track["generation"]=t["generation"]
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
                    advanced.set()
                    advanced=asyncio.Event()
                    epoch+=1
                    if cfg.get("tone_service")==sensevoice_local.ROUTE:
                        cue_input=Future()
                        cue_result=sensevoice_local.start(cue_input)
                    if playing:
                        totals["interruptions"]+=1
                        await send(signal.sock,"call.clear_audio",requestId=uuid4().hex,callId=signal.owned)
                        change(stage="正在听你说",**totals)
                    if cfg.get("_call_iat"):
                        live=voice_iat.LiveDictation(cfg)
                        iat_tasks.add(live.task)
                        live.frames.put_nowait(b"".join(audio.frames))
                elif live:
                    live.frames.put_nowait(frame)
                enqueue(raw)
                totals["received"]+=1
            if totals["received"]%25==0: change(received=totals["received"])

    async def play(out,turn,timing=None):
        nonlocal started,playing
        if epoch!=turn or flag.is_set(): return False
        playing=True
        interrupted=advanced
        async def chunks():
            if isinstance(out,bytes):
                yield out
            else:
                async for block in out: yield block
        async def pump():
            nonlocal started
            first=True
            deadline=None
            async for block in chunks():
                if deadline is None:
                    if timing:
                        # Finish dictation early, but reserve the conversational
                        # pause for a continuation until the first samples play.
                        await asyncio.sleep(max(0,timing[0]+TURN_PAUSE-time.monotonic()))
                    deadline=time.monotonic()
                for pos in range(0,len(block),640):
                    if flag.is_set() or epoch!=turn: return False
                    if started!=track["generation"]:
                        await send(media,"track.start" if binary else "audio.start",trackId=track["id"],generation=track["generation"])
                        started=track["generation"]
                    samples=block[pos:pos+640]
                    if binary:
                        await media.send(pcm_frame(track["id"],started,samples))
                    else:
                        await send(media,"audio.chunk",trackId=track["id"],generation=started,
                                   pcmBase64=base64.b64encode(samples).decode())
                    if first and timing is not None:
                        now=time.monotonic()
                        change(latencyMs={"停顿":round((timing[1]-timing[0])*1000),
                                          "听写收尾":round((timing[2]-timing[1])*1000),
                                          "生成回复":round((timing[3]-timing[2])*1000),
                                          "合成首帧":round((now-timing[3])*1000),
                                          "到第一帧":round((now-timing[0])*1000)})
                    first=False
                    totals["sent"]+=1
                    deadline=max(deadline,time.monotonic()-.04)+len(samples)/pcm_audio.BYTES_PER_SECOND
                    await asyncio.sleep(max(0,deadline-time.monotonic()))
            return not first and epoch==turn and not flag.is_set()
        task=asyncio.create_task(pump())
        interrupt=asyncio.create_task(interrupted.wait())
        try:
            done,_=await asyncio.wait((task,interrupt),return_when=asyncio.FIRST_COMPLETED)
            return task.result() if task in done else False
        finally:
            task.cancel(); interrupt.cancel()
            await asyncio.gather(task,interrupt,return_exceptions=True)
            if not isinstance(out,bytes):
                await out.aclose()
            playing=False
            change(**totals)

    async def transcribe(raw,stream,cue):
        if stream:
            try:
                words=await stream.task
            finally:
                iat_tasks.discard(stream.task)
        elif cfg.get("tone_service","off") in ("off",sensevoice_local.ROUTE):
            words=await asyncio.to_thread(providers.recognize_pcm,cfg,raw)
        else:
            words,tone,_,_=await asyncio.to_thread(providers.hear_utterance,cfg,raw)
            return providers.with_tone(words,tone)
        tone=""
        if cue:
            try:
                tone,_=await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(cue)),.12)
            except TimeoutError: pass
        return providers.with_tone(words,tone)

    async def reply():
        if greeting is not None:
            try: hello=await greeting
            except (OSError,ValueError,TimeoutError): hello=None
            if hello and epoch==0:
                await play(hello+(CHIME if cfg.get("call_cue")=="on" else b""),0)
        fragments=[]
        while not flag.is_set():
            turn,raw,stream,cue,sealed,last_voice=await queue.get()
            try:
                fragments.append(await transcribe(raw,stream,cue))
                transcribed=time.monotonic()
                if turn!=epoch: continue
                msg={"role":"user","content":"\n".join(fragments)}
                draft=await asyncio.to_thread(providers.chat,cfg,call_messages(cfg,history,msg["content"]))
                answer,finish=end_decision(draft)
                if turn!=epoch or not answer: continue
                spoken=spoken_reply.parse(answer)
                answer=spoken.words
                delivery=spoken.delivery if providers.has_audio_tags(cfg) else answer
                written=time.monotonic()
                if cfg.get("_tts_client"):
                    out=providers.stream_call_pcm(cfg["_tts_client"],cfg,delivery)
                else:
                    out=await asyncio.to_thread(providers.synthesize_pcm,cfg,delivery)
            except (OSError,ValueError,TimeoutError,KeyError,IndexError,ConnectionClosed):
                change(stage="通话中，正在等你继续说")
                continue
            if turn!=epoch: continue
            sent_before=totals["sent"]
            try:
                complete=await play(out,turn,(last_voice,sealed,transcribed,written))
            except (OSError,ValueError,TimeoutError):
                change(stage="语音合成暂时中断，通话保持中")
                continue
            if totals["sent"]>sent_before:
                fragments.clear()
                history.append(msg)
                if complete:
                    history.append({"role":"assistant","content":answer})
                history[:]=recent_call_turns(history,cfg.get("history_turns",st.DEFAULT["history_turns"]))
            if not complete: continue
            totals["replies"]+=1
            if cfg.get("_peer"):
                await asyncio.to_thread(st.remember_call_line,cfg["_peer"],msg["content"],answer)
            if finish:
                # Leave enough time to hear a brief correction or interruption.
                await asyncio.sleep(.65)
                if epoch==turn and not audio.confirmed:
                    signal.end_reason=finish
                    flag.set()
            elif cfg.get("call_cue")=="on" and epoch==turn:
                await play(CHIME,turn)
            change(**totals)

    tasks=[asyncio.create_task(listen()),asyncio.create_task(reply())]
    if own_control: tasks.append(asyncio.create_task(signal.watch_end()))
    try:
        done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for t in done: t.result()
    finally:
        if cue_input is not None and not cue_input.done():
            cue_input.set_result(b"")
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
                    change(stage="已接通，等待讲话")
                    first,greeting=greeting,None
                    await conversation(signal,media,ready,cfg,flag,first,own_control=False,history=history)
            except Exception as error:
                # Media and provider failures never own the control socket.
                # Its independent watcher processes the remote end or operator action.
                if flag.is_set(): return
                change(stage="微信通话保持中，正在恢复声音",mediaIssue=type(error).__name__)
                await asyncio.sleep(delay)
                delay=min(4,delay*2)
    client=None
    if cfg.get("_call_eleven"):
        import aiohttp
        client=aiohttp.ClientSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=50,sock_connect=8,sock_read=15))
        cfg=dict(cfg,_tts_client=client)
    watcher=asyncio.create_task(signal.watch_end())
    audio=asyncio.create_task(audio_loop())
    try:
        done,_=await asyncio.wait([watcher,audio],return_when=asyncio.FIRST_COMPLETED)
        for t in done: t.result()
    finally:
        watcher.cancel()
        audio.cancel()
        await asyncio.gather(watcher,audio,return_exceptions=True)
        if client is not None: await client.close()


async def session(mode,peer,cfg,flag):
    from websockets.asyncio.client import connect
    headers={"Authorization":"Bearer "+TOKEN.read_text().strip()}
    async with connect(BASE+"/socket",additional_headers=headers,proxy=None,open_timeout=6) as ctl:
        signal=Signal(ctl,flag)
        greeting=None
        try:
            if mode=="dial":
                greeting=asyncio.create_task(asyncio.to_thread(providers.synthesize_pcm,cfg,"喂，听得见吗？"))
            await signal.claim(mode,peer)
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
