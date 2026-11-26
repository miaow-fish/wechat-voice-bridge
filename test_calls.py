"""Private-channel fixtures for control negotiation and the two directions of audio."""
import asyncio
import base64
import json
import struct
from threading import Event
import unittest
from unittest.mock import patch

import call_channel as calls
import channel_state as st
import providers
import voice_iat


class Socket:
    def __init__(self):
        self.events=asyncio.Queue()
        self.sent=[]
        self.on_send=lambda _:None

    def event(self,data):
        self.events.put_nowait(data if isinstance(data,bytes) else json.dumps(data))

    async def recv(self):
        return await self.events.get()

    async def send(self,data):
        event=data if isinstance(data,bytes) else json.loads(data)
        self.sent.append(event)
        self.on_send(event)


class DirectCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_dictation_reaches_chat_before_socket_teardown(self):
        closing = asyncio.Event()
        cfg = st.DEFAULT | {"_call_iat": {}}

        async def recognize(chunks, credentials, language, accent, *, on_final):
            on_final("听清了")
            await closing.wait()
            return "听清了"

        with patch.object(voice_iat, "recognize_stream", recognize):
            live = voice_iat.LiveDictation(cfg)
            try:
                self.assertEqual(await asyncio.wait_for(live.transcript(), .5), "听清了")
                self.assertFalse(live.task.done())
            finally:
                closing.set()
                await live.task
        with patch.object(voice_iat, "recognize_stream", side_effect=ValueError("听写失败")):
            live = voice_iat.LiveDictation(cfg)
            with self.assertRaisesRegex(ValueError, "听写失败"):
                await asyncio.wait_for(live.transcript(), .5)

    async def test_barge_in_off_finishes_audio_and_retains_the_overlapping_utterance(self):
        ctl, media = Socket(), Socket()
        signal = calls.Signal(ctl, Event())
        signal.owned = "ours"
        signal.call["ours"] = {"state": "connected"}
        ready = {"protocolVersion": 3,
                 "sendTracks": [{"media": "audio", "id": 1, "generation": 1, "format": calls.FORMAT}],
                 "receiveTracks": [{"media": "audio", "id": 2, "generation": 1}]}
        speech = calls.pcm_frame(2, 1, struct.pack("<h", 1600)*320*9 + bytes(640*45))
        first, second = (struct.pack("<h", n)*320 for n in (35, 42))
        received = bytearray()

        def listen(frame):
            if isinstance(frame, bytes):
                first_write = not received
                received.extend(calls.read_frame(frame)[2])
                if first_write:
                    media.event(speech)
                if received.endswith(calls.CHIME):
                    ctl.event({"type": "call.state", "call": {"callId": "ours", "state": "ended"}})

        media.on_send = listen
        media.event(speech)
        cfg = st.DEFAULT | {"call_barge_in": False, "call_cue": "on"}
        with patch.object(providers, "recognize_pcm", side_effect=["那先这样", "等等，还有件事"]), \
             patch.object(providers, "chat", side_effect=["晚安\n"+calls.END_MARK, "我听着"]) as chat, \
             patch.object(providers, "synthesize_pcm", side_effect=[first*12, second*4]):
            await asyncio.wait_for(calls.conversation(signal, media, ready, cfg, signal.flag), 4)
        self.assertEqual(received, first*12 + second*4 + calls.CHIME)
        self.assertEqual(ctl.sent, [])
        messages = chat.call_args_list[1].args[1]
        self.assertIn({"role": "assistant", "content": "晚安"}, messages)
        self.assertIn("等等，还有件事", messages[-1]["content"])

    async def test_playout_reserve_absorbs_an_uneven_stream_and_delivers_every_sample(self):
        ctl, media = Socket(), Socket()
        signal = calls.Signal(ctl, Event())
        signal.owned = "ours"
        signal.call["ours"] = {"state": "connected"}
        ready = {"protocolVersion": 3,
                 "sendTracks": [{"media": "audio", "id": 1, "generation": 1, "format": calls.FORMAT}],
                 "receiveTracks": [{"media": "audio", "id": 2, "generation": 1}]}
        media.event(calls.pcm_frame(2, 1, struct.pack("<h", 1800)*320*10 + bytes(640*45)))
        first, tail = bytes([3, 0])*320*5, bytes([4, 0])*320*12
        received = bytearray()
        gaps = []
        clock = asyncio.get_running_loop().time
        audio_until = 0

        async def delayed_stream(client, cfg, text):
            next_arrival = clock() + .165
            yield first
            await asyncio.sleep(max(0, next_arrival-clock()))
            yield tail

        def hear(frame):
            nonlocal audio_until
            if not isinstance(frame, bytes):
                return
            now = clock()
            if received:
                gaps.append(max(0, now-audio_until))
            block = calls.read_frame(frame)[2]
            audio_until = max(now, audio_until) + len(block)/32000
            received.extend(block)
            if len(received) == len(first)+len(tail):
                ctl.event({"type": "call.state", "call": {"callId": "ours", "state": "ended"}})

        media.on_send = hear
        with patch.object(providers, "recognize_pcm", return_value="听到了"), \
             patch.object(providers, "chat", return_value="继续说"), \
             patch.object(providers, "stream_call_pcm", delayed_stream), patch.object(calls, "TURN_PAUSE", 0):
            await asyncio.wait_for(calls.conversation(signal, media, ready,
                                   st.DEFAULT | {"_tts_client": object()}, signal.flag), 3)
        self.assertEqual(received, first + tail)
        self.assertLess(max(gaps), .035)

    async def test_native_playout_survives_a_sender_stall_and_drains_before_goodbye(self):
        ctl, media = Socket(), Socket()
        gate = Event()
        signal = calls.Signal(ctl, gate)
        signal.owned = "ours"
        signal.call["ours"] = {"state": "connected"}
        ready = {"protocolVersion": 3,
                 "sendTracks": [{"media": "audio", "id": 1, "generation": 1, "format": calls.FORMAT}],
                 "receiveTracks": [{"media": "audio", "id": 2, "generation": 1}]}
        media.event(calls.pcm_frame(2, 1, struct.pack("<h", 1800)*320*9 + bytes(640*45)))
        # Distinguishable frames and a short tail. The daemon can only play whole frames.
        pcm = b"".join(struct.pack("<h", number)*320 for number in range(1, 66)) + b"\x27\x00"*13
        presented = bytearray()
        clock = asyncio.get_running_loop().time
        queued_until = max_gap = peak_queue = 0.0
        stalled = False

        async def native_send(message):
            nonlocal queued_until, max_gap, peak_queue, stalled
            if not isinstance(message, bytes):
                return
            if not stalled and len(presented) >= 32*640:
                stalled = True
                await asyncio.sleep(.330)
            samples = calls.read_frame(message)[2]
            now = clock()
            if presented:
                max_gap = max(max_gap, now-queued_until)
            queued_until = max(queued_until, now) + len(samples)/32000
            peak_queue = max(peak_queue, queued_until-now)
            presented.extend(samples)

        async def synthesize(*_):
            # The network may split a sample or frame across successive reads.
            for block in (pcm[:701], pcm[701:24773], pcm[24773:]):
                yield block

        media.send = native_send
        cfg = st.DEFAULT | {"call_barge_in": False, "_tts_client": object()}
        with patch.object(providers, "recognize_pcm", return_value="那晚安"), \
             patch.object(providers, "chat", return_value="晚安\n"+calls.END_MARK), \
             patch.object(providers, "stream_call_pcm", synthesize), patch.object(calls, "TURN_PAUSE", 0):
            await asyncio.wait_for(calls.conversation(signal, media, ready, cfg, gate, own_control=False), 4)
        self.assertTrue(stalled)
        self.assertEqual(presented, pcm.ljust(66*640, b"\0"))
        self.assertLess(max_gap, .045)
        self.assertGreater(peak_queue, .35)
        self.assertLess(peak_queue, calls.NATIVE_QUEUE_SECONDS + .03)
        self.assertGreaterEqual(clock(), queued_until)
        self.assertEqual(signal.end_reason, "助手已道别")

    def test_history_counts_user_turns_when_a_spoken_reply_is_interrupted(self):
        history = [
            {"role":"user","content":"第一轮"},
            {"role":"assistant","content":"第一轮的回答"},
            {"role":"user","content":"第二轮，回答被打断"},
            {"role":"user","content":"第三轮"},
            {"role":"assistant","content":"第三轮的回答"},
        ]
        for limit, expected in ((0, []), (1, history[3:]), (2, history[2:]), (3, history)):
            with self.subTest(limit=limit):
                messages=calls.call_messages({"history_turns":limit},history,"当前这轮")
                self.assertEqual(messages[1:-1],expected)
                self.assertIn("当前这轮",messages[-1]["content"])

    def test_hangup_is_an_explicit_action(self):
        self.assertEqual(calls.end_decision("明天听你说下半段。晚安\n[[END_CALL]]"),
                         ("明天听你说下半段。晚安","助手已道别"))
        for words in ("我还想再聊会儿","好，那我先挂了。","你挂呀"):
            self.assertEqual(calls.end_decision(words),(words,""))
        self.assertEqual(calls.end_decision("[[END_CALL]]"),("",""))
        self.assertFalse(calls.end_decision("比如这样\n```\n[[END_CALL]]\n```")[1])

    async def test_same_hangup_request_can_continue_or_end_after_spoken_goodbye(self):
        for end in (False, True):
            with self.subTest(end=end):
                ctl,media=Socket(),Socket()
                signal=calls.Signal(ctl,Event())
                signal.owned="ours"
                signal.call["ours"]={"state":"connected","revision":4}
                ready={"protocolVersion":3,"sendTracks":[{"media":"audio","id":1,"generation":1,"format":calls.FORMAT}],
                       "receiveTracks":[{"media":"audio","id":2,"generation":1}]}
                media.event(calls.pcm_frame(2,1,struct.pack("<h",1800)*320*10+bytes(640*48)))
                words="那留到明天说，晚安" if end else "我还想听你说会儿"
                answer=words+"\n"+calls.END_MARK if end else words
                pcm=b"\x05\x00"*320*3
                audio=[]
                def hear(frame):
                    if isinstance(frame, bytes):
                        self.assertFalse(signal.flag.is_set())
                        audio.append(calls.read_frame(frame)[2])
                        if not end and b"".join(audio)==pcm:
                            ctl.event({"type":"call.state","call":{"callId":"ours","state":"ended","revision":5}})
                media.on_send=hear
                with patch.object(providers,"recognize_pcm",return_value="先挂电话吧。"),patch.object(providers,"chat",return_value=answer) as model,patch.object(providers,"synthesize_pcm",return_value=pcm) as voice:
                    await asyncio.wait_for(calls.conversation(signal,media,ready,st.DEFAULT,signal.flag),4)
                model.assert_called_once()
                self.assertIn("先挂电话吧",model.call_args.args[1][-1]["content"])
                voice.assert_called_once_with(st.DEFAULT,words)
                self.assertEqual(b"".join(audio),pcm)
                self.assertEqual(signal.flag.is_set(),end)
                if signal.flag.is_set():
                    await signal.release()
                self.assertEqual([e["type"] for e in ctl.sent],["call.hangup"] if end else [])

    async def test_media_reopens_while_the_same_control_connection_stays_owned(self):
        sock=Socket()
        signal=calls.Signal(sock,Event())
        signal.owned,signal.token="ours","test"
        signal.hello={"protocolVersion":3,"clientId":"test"}
        signal.call["ours"]={"state":"connected","revision":4}
        opened=[]
        class Media(Socket):
            async def __aenter__(self):
                opened.append(True)
                if len(opened)==1: raise OSError("temporary media outage")
                self.event({"type":"media.ready","protocolVersion":3,"sendTracks":[{"media":"audio","id":1,"generation":1,"format":calls.FORMAT}],"receiveTracks":[]})
                sock.event({"type":"call.state","call":{"callId":"ours","state":"ended"}})
                return self
            async def __aexit__(self,*args): pass
        await asyncio.wait_for(calls.connected_session(signal,st.DEFAULT,signal.flag,None,lambda *a,**kw:Media(),{}),2)
        self.assertEqual(len(opened),2)
        self.assertFalse(any(e["type"] in ("call.cancel","call.hangup") for e in sock.sent))

    async def test_barge_in_discards_old_audio_and_rings_only_after_complete_reply(self):
        ctl,media=Socket(),Socket()
        signal=calls.Signal(ctl,Event())
        signal.owned="ours"
        signal.call["ours"]={"state":"connected"}
        ready={"protocolVersion":3,"sendTracks":[{"media":"audio","id":1,"generation":1,"format":calls.FORMAT}],
               "receiveTracks":[{"media":"audio","id":2,"generation":1}]}
        speech=calls.pcm_frame(2,1,struct.pack("<h",1600)*320*9+bytes(640*45))
        media.event(speech)
        first=b"\x03\x00"*320
        second=b"\x04\x00"*320
        sent=[]
        def outgoing(e):
            if not isinstance(e,bytes): return
            samples=calls.read_frame(e)[2]
            sent.append(samples)
            if sent==[first]: media.event(speech)
            if b"".join(sent).endswith(calls.CHIME):
                ctl.event({"type":"call.state","call":{"callId":"ours","state":"ended"}})
        media.on_send=outgoing
        ctl.on_send=lambda e: media.event({"type":"track.updated","track":{"media":"audio","direction":"send","id":1,"generation":2}}) if e["type"]=="call.clear_audio" else None
        with patch.object(providers,"recognize_pcm",side_effect=["那今天聊到这里","等会儿，我还有句话"]),patch.object(providers,"chat",side_effect=["晚安\n"+calls.END_MARK,"你说"]),patch.object(providers,"synthesize_pcm",side_effect=[first*30,second]):
            await asyncio.wait_for(calls.conversation(signal,media,ready,st.DEFAULT|{"call_cue":"on"},signal.flag),3)
        self.assertEqual(b"".join(sent),first+second+calls.CHIME)
        self.assertEqual([e["type"] for e in ctl.sent],["call.clear_audio"])
        self.assertFalse(signal.flag.is_set())

    async def test_two_control_only_interruptions_keep_the_next_reply_audible(self):
        ctl, media = Socket(), Socket()
        signal = calls.Signal(ctl, Event())
        signal.owned = "ours"
        signal.call["ours"] = {"state": "connected"}
        ready = {"protocolVersion": 3,
                 "sendTracks": [{"media": "audio", "id": 1, "generation": 1, "format": calls.FORMAT}],
                 "receiveTracks": [{"media": "audio", "id": 2, "generation": 1}]}
        speech = calls.pcm_frame(2, 1, struct.pack("<h", 1800) * 320 * 10 + bytes(640 * 45))
        clips = [struct.pack("<h", level) * 320 for level in (1200, 2000, 2800)]
        generation, started = 1, None
        starts, audible, discarded = [], [], []

        def end():
            ctl.event({"type": "call.state", "call": {"callId": "ours", "state": "ended"}})

        def control(event):
            nonlocal generation
            if event["type"] == "call.clear_audio":
                generation += 1
                # The native daemon publishes the audio send revision on the control socket.
                ctl.event({"type": "track.updated", "callId": "ours", "track": {
                    "media": "audio", "direction": "send", "id": 1, "generation": generation}})

        def receive(frame):
            nonlocal started
            if isinstance(frame, dict):
                if frame["type"] == "track.start":
                    started = frame["generation"]
                    starts.append(started)
                return
            _, actual, samples = calls.read_frame(frame)
            if actual != generation or started != generation:
                discarded.append((actual, generation))
                end()
                return
            audible.append(samples)
            if len(audible) <= 2:
                media.event(speech)
            elif b"".join(audible).endswith(calls.CHIME):
                end()

        ctl.on_send, media.on_send = control, receive
        media.event(speech)
        with patch.object(providers, "recognize_pcm", return_value="嗯"), \
             patch.object(providers, "chat", return_value="听见了"), \
             patch.object(providers, "synthesize_pcm", side_effect=[clips[0] * 30, clips[1] * 30, clips[2]]):
            await asyncio.wait_for(calls.conversation(signal, media, ready, st.DEFAULT | {"call_cue": "on"}, signal.flag), 6)
        self.assertEqual(discarded, [])
        self.assertEqual(starts, [1, 2, 3])
        self.assertEqual(b"".join(audible), b"".join(clips) + calls.CHIME)

    async def test_send_track_waits_for_its_own_revision_after_clear(self):
        ctl = Socket()
        signal = calls.Signal(ctl, Event())
        signal.owned = "ours"
        track = {"direction": "send", "id": 1, "generation": 4}
        signal.observe_send_track(track)
        await signal.clear_audio(1)
        request = ctl.sent[-1]["requestId"]
        signal.observe({"type": "command.result", "requestId": request, "status": "accepted"})
        event = {"type": "track.updated", "callId": "ours", "track": track | {"generation": 5}}
        signal.observe(event | {"callId": "someone-else"})
        signal.observe(event | {"track": event["track"] | {"direction": "receive"}})
        self.assertFalse(signal.audio_ready.is_set())
        signal.observe(event)
        self.assertTrue(signal.audio_ready.is_set())
        signal.observe_send_track(track)
        self.assertEqual(signal.send_generations[1], 5)
        await signal.clear_audio(1)
        signal.observe({"type": "command.result", "requestId": ctl.sent[-1]["requestId"], "status": "rejected"})
        self.assertTrue(signal.audio_ready.is_set())

    async def test_first_tts_chunk_plays_before_provider_finishes(self):
        ctl,media=Socket(),Socket()
        signal=calls.Signal(ctl,Event());signal.owned="ours"
        signal.call["ours"]={"state":"connected"}
        ready={"protocolVersion":3,"sendTracks":[{"media":"audio","id":1,"generation":1,"format":calls.FORMAT}],
               "receiveTracks":[{"media":"audio","id":2,"generation":1}]}
        media.event(calls.pcm_frame(2,1,struct.pack("<h",1900)*320*12+bytes(640*46)))
        first_played=asyncio.Event()
        async def stream(client,cfg,text):
            self.assertEqual(text,"[whispers] 听见了。")
            yield b"\x01\x00"*320
            await first_played.wait()
            yield b"\x02\x00"*320
        def sent(value):
            if isinstance(value,bytes):
                if calls.read_frame(value)[2][0]==1: first_played.set()
                else: ctl.event({"type":"call.state","call":{"callId":"ours","state":"ended"}})
        media.on_send=sent
        with patch.object(providers,"has_audio_tags",return_value=True),patch.object(providers,"recognize_pcm",return_value="你好呀"),patch.object(providers,"chat",return_value="[whispers] 听见了。"),patch.object(providers,"stream_call_pcm",new=stream),patch.object(providers,"synthesize_pcm",side_effect=AssertionError("Buffered whole reply")):
            await asyncio.wait_for(calls.conversation(signal,media,ready,st.DEFAULT|{"_tts_client":object()},signal.flag),2)
        self.assertTrue(first_played.is_set())
    async def check_claim(self,mode,version=2):
        sock=Socket()
        own={"callId":"ours","kind":"direct","callerId":"friend","callType":"voice","revision":2}
        def respond(e):
            kind=e["type"]
            if kind=="call.hello":
                self.assertIn(version,e["supportedProtocolVersions"])
                sock.event({"type":"hello","protocolVersion":version,"clientId":"client","capabilities":{"directAudio":True},
                            "currentOffer":own|{"callerId":"somebody-else"} if mode=="answer" else None,
                            "currentPendingOffer":own if mode=="answer" else None})
            elif kind=="call.ready":
                sock.event({"type":"command.result","requestId":e["requestId"],"status":"accepted"})
                if mode=="answer": sock.event({"type":"call.offered","offer":own|{"revision":3}})
            elif kind in ("call.answer","call.dial"):
                sock.event({"type":"command.result","requestId":e["requestId"],"status":"accepted","ownerToken":"fixture","callId":"ours"})
                sock.event({"type":"call.state","call":own|{"revision":4,"state":"connected","direction":"outgoing" if mode=="dial" else "incoming"}})
        sock.on_send=respond
        signal=calls.Signal(sock,Event())
        await signal.claim(mode,"friend")
        self.assertEqual(signal.owned,"ours")
        if mode=="answer":
            self.assertEqual(sock.sent[1]["scope"],{"kind":"offer","callId":"ours","expectedRevision":2})
            self.assertEqual(sock.sent[2]["expectedRevision"],3)
        else:
            self.assertEqual(sock.sent[2]["target"],{"kind":"direct","contactId":"friend"})
        await signal.release()
        self.assertEqual((sock.sent[-1]["type"],sock.sent[-1]["expectedRevision"]),("call.hangup",4))

    async def test_incoming_is_scoped_to_the_bound_peer(self):
        await self.check_claim("answer")

    async def test_standby_accepts_only_distinct_voice_offers_from_the_bound_friend(self):
        sock=Socket()
        own={"callId":"one","kind":"direct","callerId":"friend","callType":"voice","revision":2}
        sock.event({"type":"hello","protocolVersion":3,"capabilities":{"directAudio":True},
                    "currentOffer":own|{"callerId":"stranger"},"currentPendingOffer":own})
        for offer in (own,own|{"callerId":"stranger","callId":"foreign"},
                      own|{"kind":"conference","callId":"group"},own|{"callType":"video","callId":"video"},
                      None,own|{"callId":"two"},None):
            sock.event({"type":"call.offered","offer":offer})
        handled=[]
        def start(mode,*,expected_peer,expected_offer):
            handled.append((mode,expected_peer,expected_offer))
        enabled=lambda: "friend" if len(handled)<2 else None
        with patch.object(calls,"incoming_peer",side_effect=enabled),patch.object(calls,"start",side_effect=start), \
             patch.object(calls,"remember_offer",return_value=True) as record, \
             patch.object(calls,"snapshot",return_value={"active":False}):
            await asyncio.wait_for(calls.watch_incoming(sock,"friend"),1)
        self.assertEqual(handled,[("answer","friend","one"),("answer","friend","two")])
        self.assertEqual([c.args for c in record.call_args_list],[("friend","one"),("friend","two")])
        self.assertEqual([e["type"] for e in sock.sent],["call.hello"])

    async def test_expired_automatic_offer_cannot_claim_a_different_call_from_the_same_friend(self):
        sock=Socket()
        sock.event({"type":"hello","capabilities":{"directAudio":True},"protocolVersion":3,
                    "currentOffer":{"callId":"new","callerId":"friend","callType":"voice","revision":1}})
        with self.assertRaisesRegex(ValueError,"撤回"):
            await calls.Signal(sock,Event()).claim("answer","friend",expected_offer="old")
        self.assertEqual([e["type"] for e in sock.sent],["call.hello"])

    async def test_switching_off_before_pickup_leaves_the_ringing_offer_unanswered(self):
        sock=Socket()
        offer={"callId":"one","callerId":"friend","callType":"voice","revision":1}
        sock.event({"type":"hello","capabilities":{"directAudio":True},"protocolVersion":3,"currentOffer":offer})
        with patch.object(calls,"incoming_peer",return_value=None),self.assertRaisesRegex(ValueError,"关闭"):
            await calls.Signal(sock,Event()).claim("answer","friend",expected_offer="one")
        self.assertEqual([e["type"] for e in sock.sent],["call.hello"])

    async def test_caller_can_speak_before_the_greeting_is_ready(self):
        ctl,media=Socket(),Socket()
        signal=calls.Signal(ctl,Event())
        signal.owned="ours"
        signal.call["ours"]={"state":"connected"}
        ready={"protocolVersion":3,"sendTracks":[{"media":"audio","id":1,"generation":1,"format":calls.FORMAT}],
               "receiveTracks":[{"media":"audio","id":2,"generation":1}]}
        speech=struct.pack("<h",1600)*320*10 + bytes(640*45)
        media.event(calls.pcm_frame(2,1,speech))
        answer=b"\x07\x00"*320
        def sent(frame):
            if isinstance(frame,bytes):
                self.assertEqual(calls.read_frame(frame)[2],answer)
                ctl.event({"type":"call.state","call":{"callId":"ours","state":"ended"}})
        media.on_send=sent
        welcome=asyncio.create_task(asyncio.sleep(30,result=b"\x08\x00"*320))
        with patch.object(providers,"recognize_pcm",return_value="喂"),patch.object(providers,"chat",return_value="我在"), \
             patch.object(providers,"synthesize_pcm",return_value=answer),patch.object(calls,"TURN_PAUSE",0):
            await asyncio.wait_for(calls.conversation(signal,media,ready,st.DEFAULT,signal.flag,welcome),2)
        self.assertTrue(welcome.cancelled())

    async def test_outgoing_registers_audio_before_dial(self):
        await self.check_claim("dial")
        await self.check_claim("dial",3)

    async def test_v3_greeting_and_reply_follow_distinct_binary_tracks(self):
        sock,media=Socket(),Socket()
        flag=Event()
        signal=calls.Signal(sock,flag)
        signal.owned="ours"
        signal.call["ours"]={"state":"connected"}
        samples=struct.pack("<h",1900)*320*10+bytes(640*48)
        incoming=struct.pack(">4sBBHIIQQI",b"WXCM",3,1,0,2,5,52,20000,len(samples))+samples
        ready={"protocolVersion":3,"sendTracks":[{"media":"audio","id":1,"generation":4,"format":calls.FORMAT}],
               "receiveTracks":[{"media":"audio","id":2,"generation":5,"format":calls.FORMAT}]}
        count=0
        def output(e):
            nonlocal count
            if isinstance(e,bytes):
                count+=1
                if count==1: asyncio.get_running_loop().call_later(.04,media.event,incoming)
                if count==2: sock.event({"type":"call.state","call":{"callId":"ours","state":"ended"}})
        media.on_send=output
        hello=asyncio.sleep(0,result=bytes(640))
        with patch.object(providers,"recognize_pcm",return_value="喂"),patch.object(providers,"chat",return_value="听见了"),patch.object(providers,"synthesize_pcm",return_value=b"\x01\x00"*320):
            await asyncio.wait_for(calls.conversation(signal,media,ready,st.DEFAULT,flag,hello),2)
        self.assertEqual(media.sent[0],{"type":"track.start","trackId":1,"generation":4})
        self.assertEqual(calls.read_frame(media.sent[1]),(1,4,bytes(640)))
        self.assertEqual(calls.read_frame(media.sent[2]),(1,4,b"\x01\x00"*320))
        with self.assertRaises(ValueError):
            calls.read_frame(media.sent[2][:-1])

    async def test_received_pcm_goes_to_asr_and_synthesis_goes_to_media_socket(self):
        sock,media=Socket(),Socket()
        flag=Event()
        signal=calls.Signal(sock,flag)
        signal.owned="ours"
        signal.call["ours"]={"state":"connected"}
        track={"id":1,"generation":2,"format":calls.FORMAT}
        receive={"id":2,"generation":1,"format":calls.FORMAT}
        samples=struct.pack("<h",1900)*320*10 + bytes(640*48)
        media.event({"type":"audio.chunk","trackId":2,"generation":1,"pcmBase64":base64.b64encode(samples).decode()})
        def got_output(e):
            if e["type"]=="audio.chunk":
                sock.event({"type":"call.state","call":{"callId":"ours","state":"ended","revision":6}})
        media.on_send=got_output
        tone=struct.pack("<h",1200)*320
        with patch.object(providers,"recognize_pcm",return_value="测试甲") as transcribe,patch.object(providers,"chat",return_value="[sighs] 听清了") as chat,patch.object(providers,"synthesize_pcm",return_value=tone) as tts,patch("pcm_audio.subprocess.run",side_effect=AssertionError("Unexpected transcode")):
            await asyncio.wait_for(calls.conversation(signal,media,{"sendTrack":track,"receiveTracks":[receive]},st.DEFAULT,flag),2)
        self.assertEqual(transcribe.call_count,1)
        self.assertEqual(transcribe.call_args.args[1],samples[:640*(10+round(calls.END_PAUSE/.02))])
        current=chat.call_args.args[1][-1]["content"]
        self.assertEqual(current.splitlines()[0],"测试甲")
        self.assertIn("〔当前北京时间〕",current)
        self.assertEqual(tts.call_args.args[1],"听清了")
        outgoing=next(e for e in media.sent if e["type"]=="audio.chunk")
        self.assertEqual(base64.b64decode(outgoing["pcmBase64"]),tone)


if __name__=="__main__":
    unittest.main()
