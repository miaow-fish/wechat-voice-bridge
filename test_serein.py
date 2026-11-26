"""A synthetic private exchange, delivery retries and isolated group/call inputs."""
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import call_channel
import channel_state as st
import channel_worker as worker
import panel
import providers
import serein_memory as memory
from tools import serein_relay
import worldbooks


class PrivateMemoryTest(unittest.TestCase):
    def setUp(self):
        stack=self.enterContext(ExitStack())
        folder=stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.object(st,"ROOT",Path(folder)))
        st.init()
        with st.database() as db:
            st.put(db,"peer","friend")
            st.put(db,"profile",{"merge_messages":False,"tts":"off","asr":"off"})
        self.remote=stack.enter_context(patch.object(memory,"request"))
        self.link=memory.pair("http://127.0.0.1:28229")

    def incoming(self, mid, body):
        msg={"id":mid,"threadId":"friend","senderId":"friend","direction":"incoming","type":1,"body":body}
        tick=datetime.now(timezone.utc).isoformat()
        with patch.object(worker,"wx",return_value=msg), st.database() as db:
            worker.ingest(db,{"kind":"message.committed","createdAt":tick,
                              "data":{"threadId":"friend","messageId":mid,"direction":"incoming","origin":"live"}})
            return db.execute("SELECT * FROM turns ORDER BY rowid DESC LIMIT 1").fetchone()

    def test_private_query_and_archive_use_only_current_messages_and_real_receipts(self):
        st.remember_call_line("friend","电话里说过的暗号","电话回复")
        turn=self.incoming("m1","周末见")
        self.remote.return_value={"ok":True,"note":"上周约在书店","ids":["event:test"]}
        def chat(cfg,messages,**kwargs):
            self.assertNotIn("书店",messages[0]["content"])
            self.assertIn("周末见\n\n〔系统附注〕",messages[-1]["content"])
            self.assertIn("上周约在书店",messages[-1]["content"])
            return "好\n书店见"
        def wx(path, data=None, key=None):
            if path=="/sends":
                return {"id":"job"}
            return {"state":"accepted","serverId":"receipt","threadId":"friend"}
        with patch.object(providers,"chat",side_effect=chat),patch.object(worker,"wx",side_effect=wx):
            worker.process(turn)
        route,payload=self.remote.call_args.args[1:3]
        self.assertEqual(("recall",{"query":"周末见","delivered_ids":[]}), (route,payload))
        archived=[]
        def archive(link,route,payload,timeout):
            self.assertEqual(route,"archive")
            archived.extend(payload["events"])
            return {"ok":True,"ids":[e["source_event_id"] for e in archived]}
        self.remote.side_effect=archive
        self.assertTrue(memory.flush())
        self.assertFalse(memory.flush())
        self.assertEqual([("user","周末见"),("assistant","好"),("assistant","书店见")],[(e["role"],e["text"]) for e in archived])
        with st.database() as db:
            self.assertEqual(memory.status(db),{"paired":True,"archived":1,"pending":0})
        second=self.incoming("m2","到了")
        self.remote.side_effect=None
        memory.recall(second["id"],"friend",["到了"])
        self.assertEqual(self.remote.call_args.args[2]["delivered_ids"],["event:test"])

    def test_retry_preserves_original_ids_and_time_without_sending_wechat_twice(self):
        turn=self.incoming("one","你好")
        with st.database() as db:
            db.execute("UPDATE turns SET answer='你好呀',kind='text',receipt='accepted-id',status='accepted' WHERE id=?",(turn["id"],))
        self.remote.side_effect=TimeoutError
        self.assertFalse(memory.flush())
        first=self.remote.call_args.args[2]["events"]
        self.assertFalse(memory.flush())
        with st.database() as db:
            db.execute("UPDATE serein_turns SET next_try=0")
        self.remote.side_effect=None
        self.remote.return_value={"ok":True,"ids":[e["source_event_id"] for e in first]}
        self.assertTrue(memory.flush())
        self.assertEqual(self.remote.call_args.args[2]["events"],first)

    def test_no_group_call_old_turn_or_new_recipient_access(self):
        with st.database() as db:
            st.put(db,"profile",{"memory_url":""})
        old=self.incoming("past","接入前的私聊")
        memory.pair("http://127.0.0.1:28229")
        self.assertIsNone(memory.recall(old["id"],"friend",["旧消息"]))
        current=self.incoming("first","今天")
        group_cfg={"name":"测试群","mode":"auto"}
        note={"note":"私聊资料"}
        with st.database() as db:
            cfg=st.profile(db)
        with patch.object(worker,"history",return_value=[]):
            messages=worker.reply_messages(cfg,"group@chatroom",None,"群里发言",[],group=group_cfg,memory=note)
        self.assertNotIn("私聊资料",str(messages))
        self.assertIsNone(memory.recall(current["id"],"group@chatroom",["群里发言"]))
        self.assertNotIn("私聊资料",str(call_channel.call_messages(cfg,[],"电话原话")))
        with st.database() as db:
            st.put(db,"peer","new-friend")
            self.assertFalse(memory.status(db)["paired"])
        self.assertIsNone(memory.recall(current["id"],"friend",["已经换绑"]))
        self.assertFalse(memory.flush())
        self.remote.assert_not_called()

    def test_relay_scopes_each_event_and_forwards_only_acknowledged_ids(self):
        core=Mock()
        window=self.link["window"]
        with self.assertRaises(PermissionError):
            serein_relay.relay(core,window,"/recall",{"window":"other"})
        with self.assertRaises(LookupError):
            serein_relay.relay(core,window,"/admin",{"window":window})
        event={"source_event_id":"a"*64,"created_at":datetime.now(timezone.utc).isoformat(),
               "role":"user","text":"本轮","source":"other","session_id":"other"}
        core.call.return_value={"ok":True,"rejected":0,"items":[{"source_event_id":"a"*64,"status":"duplicate"}]}
        self.assertEqual(serein_relay.relay(core,window,"/archive",{"window":window,"events":[event]})["ids"],["a"*64])
        path,data=core.call.call_args.args
        self.assertEqual(path,"/api/ingest-raw")
        self.assertEqual(data["session_id"],window)
        self.assertNotIn("session_id",data["events"][0])
        core.call.return_value={"ok":True,"rejected":0,"items":[]}
        with self.assertRaises(RuntimeError):
            serein_relay.relay(core,window,"/archive",{"window":window,"events":[event]})


class PersonaUploadTest(unittest.TestCase):
    def test_preview_text_word_and_character_json(self):
        enc=lambda b:base64.b64encode(b).decode()
        original="安静一点\n也会笑"
        self.assertEqual(worldbooks.persona_preview("角色.md",enc(original.encode("utf-16")))["body"],original)
        word=BytesIO()
        with ZipFile(word,"w") as z:
            z.writestr("word/document.xml",'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>真诚</w:t></w:r></w:p></w:document>')
        self.assertEqual(worldbooks.persona_preview("角色.docx",enc(word.getvalue()))["body"],"真诚")
        card={"data":{"name":"测试角色","description":"沉静","personality":"有主见","first_mes":"初次问候",
                      "character_book":{"entries":[{"content":"独立的世界书条目"}]}}}
        p=worldbooks.persona_preview("卡.json",enc(json.dumps(card).encode()))
        self.assertEqual((p["name"],p["format"]),("测试角色","text"))
        self.assertIn("沉静",p["body"])
        self.assertNotIn("初次问候",p["body"])
        self.assertNotIn("独立的世界书条目",p["body"])
        with self.assertRaises(ValueError):
            worldbooks.persona_preview("过长.txt",enc(("长"*16001).encode()))

    def test_png_card_selects_persona_in_all_chat_scenes(self):
        from PIL import Image,PngImagePlugin
        raw=BytesIO(); meta=PngImagePlugin.PngInfo()
        payload=json.dumps({"data":{"name":"测试名","description":"说话轻快"}}).encode()
        meta.add_text("chara",base64.b64encode(payload).decode())
        Image.new("RGB",(2,2)).save(raw,format="PNG",pnginfo=meta)
        p=worldbooks.persona_preview("角色.png",base64.b64encode(raw.getvalue()).decode())
        with tempfile.TemporaryDirectory() as folder,patch.object(st,"ROOT",Path(folder)):
            st.init()
            panel.save_asset({"kind":"persona","name":p["name"],"body":p["body"],"select":True})
            with st.database() as db: cfg=st.profile(db)
            with patch.object(worker,"history",return_value=[]):
                for group in (None,{"name":"群"}):
                    msgs=worker.reply_messages(cfg,"p",None,"你好",[],group=group)
                    self.assertTrue(msgs[0]["content"].startswith("人设\n角色名：测试名\n\n说话轻快"))
            self.assertIn("说话轻快",call_channel.call_messages(cfg,[],"你好")[0]["content"])
