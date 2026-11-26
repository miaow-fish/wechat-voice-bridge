"""Exercise group subscriptions, speaker context and delivery without contacting WeChat."""
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import channel_state as st
import channel_worker as worker
import group_chats
import providers
import call_channel
import panel
import reply_actions


ROOM = "room@chatroom"
OTHER = "other@chatroom"


class GroupTest(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(st, "ROOT", Path(temp)))
        self.enterContext(patch.object(providers.ds,"KEY",Path(temp)/"test.key"))
        st.init()
        providers.initialize()
        self.messages = {}
        self.sent = []
        self.lookups = []
        self.enterContext(patch.object(group_chats, "wx", side_effect=self.wx))
        self.enterContext(patch.object(worker, "wx", side_effect=self.wx))
        group_chats.speaker_name.cache_clear()
        with st.database() as db:
            st.put(db, "peer", "friend")
            st.put(db, "profile", {"persona":"固定人设", "voice":"demo", "merge_messages":False})
        group_chats.configure(ROOM, "auto")

    def wx(self, path="", data=None, idem=None):
        from urllib.parse import unquote
        path = unquote(path)
        if not path: return {"authenticated":True,"ownUsername":"assistant"}
        if path.startswith("/groups?"):
            return {"items":[{"id":ROOM,"displayName":"测试群"}, {"id":OTHER,"deleted":True}], "nextCursor":"page2"}
        if path.startswith("/groups/"):
            room = path.split("/")[2]
            if "/lookup?" in path: return {"id":room,"items":[{"id":"assistant"}]}
            if "/members/" in path:
                member = path.split("/")[-1]
                return {"member":{"id":member,"roomDisplayName":{"a":"甲","b":"乙"}[member]}}
            return {"id": room, "displayName": "测试群", "isInGroup": room in (ROOM,OTHER)}
        if path.startswith("/messages/"):
            ident = path.split("/")[-1]
            self.lookups.append(ident)
            return self.messages[ident]
        if path == "/sends":
            self.sent.append(data)
            return {"id":str(len(self.sent))}
        if path.startswith("/sends/"):
            item=self.sent[int(path.split("/")[-1])-1]
            return {"state":"accepted", "threadId":item["threadId"], "serverId":"ok"}
        self.fail(path)

    def add(self, ident, text, *, peer=ROOM, sender="a", mentions=None, origin="live", direction="incoming", stamp=None):
        self.messages[ident]={"id":ident, "threadId":peer, "type":1, "senderId":sender,
                              "body":text, "direction":direction, "mentions":mentions}
        when=datetime.fromtimestamp(stamp or time.time(), timezone.utc).isoformat()
        event={"kind":"message.committed","createdAt":when,"data":{"threadId":peer,"messageId":ident,
               "direction":direction,"origin":origin}}
        with st.database() as db:
            worker.ingest(db,event)

    def turn(self, ident):
        with st.database() as db:
            return db.execute("SELECT t.* FROM turns t JOIN incoming i ON i.turn=t.id WHERE i.id=?", (ident,)).fetchone()

    def test_group_can_listen_without_a_bubble_and_keeps_speakers_out_of_private_chat(self):
        self.add("private","私聊暗号",peer="friend",sender="friend")
        with patch.object(providers,"chat",return_value="知道了"):
            worker.process(self.turn("private"))
        self.add("a","乙给我发个表情包")
        with patch.object(providers,"chat",return_value="[[silent]]") as model:
            worker.process(self.turn("a"))
            self.assertEqual(model.call_count,1)
        self.assertEqual(self.turn("a")["status"],"silent")
        self.assertEqual(len(self.sent),1)
        self.add("b","刚才谁叫我",sender="b")
        with patch.object(providers,"chat",return_value="甲刚才叫你呢") as model:
            worker.process(self.turn("b"))
        messages=model.call_args.args[1]
        self.assertIn("固定人设",messages[0]["content"])
        self.assertIn("甲",messages[1]["content"])
        self.assertIn("乙",messages[-1]["content"])
        self.assertNotIn("私聊暗号",str(messages))
        self.assertNotIn("[[silent]]",str(messages[1:]))
        self.assertEqual(self.sent[-1],{"threadId":ROOM,"text":"甲刚才叫你呢"})
        self.assertNotIn("表情包",str(st.recent_dialogue("friend")))

    def test_group_and_private_timers_are_independent_and_two_senders_keep_their_names(self):
        group_chats.configure(OTHER,"auto")
        with st.database() as db: st.put(db,"profile",{"merge_messages":True,"merge_wait_seconds":7})
        now=time.time()
        self.add("a","第一句",stamp=now)
        self.add("b","第二句",sender="b",stamp=now+5)
        self.add("p","私聊",peer="friend",sender="friend",stamp=now)
        self.add("other","另一个群",peer=OTHER,stamp=now)
        self.assertEqual(self.turn("a")["id"],self.turn("b")["id"])
        self.assertNotEqual(self.turn("a")["id"],self.turn("p")["id"])
        with st.database() as db:
            db.execute("UPDATE turns SET status='ready',next_try=0 WHERE peer IN (?,?)",("friend",OTHER))
            self.assertEqual(worker.next_turn(db,"direct")["peer"],"friend")
            self.assertEqual(worker.next_turn(db,"group")["peer"],OTHER)
        with patch.object(providers,"chat",return_value="[[silent]]") as model:
            worker.process(self.turn("a"))
        current=model.call_args.args[1][-1]["content"]
        self.assertIn("〔群成员 甲〕\n第一句\n〔群成员 乙〕\n第二句",current)
        self.assertNotIn("另一个群",current)

    def test_mentions_real_metadata_and_replays(self):
        group_chats.configure(ROOM,"mention")
        self.add("other","不读",peer=OTHER)
        self.add("old","启用前",stamp=time.time()-60)
        self.add("backfill","旧消息",origin="sync")
        self.add("own","助手自己说的",direction="outgoing")
        self.assertEqual(self.lookups,[])
        self.add("typed","@助手 这是手打的")
        with patch.object(providers,"chat") as model:
            worker.process(self.turn("typed"))
            model.assert_not_called()
        self.add("at","问你呢",mentions={"state":"known","self":True}, sender="b")
        self.add("at","重复事件",mentions={"state":"known","self":True}, sender="b")
        with patch.object(providers,"chat",return_value="在") as model:
            worker.process(self.turn("at"))
        self.assertIn("乙 · @了你",model.call_args.args[1][-1]["content"])
        self.assertEqual(self.sent,[{"threadId":ROOM,"text":"在"}])
        self.assertEqual(self.lookups.count("at"),1)

    def test_group_model_controls_voice_and_sticker_and_cannot_ring_private_contact(self):
        self.add("call","打给我")
        with patch.object(providers,"chat",return_value="[[call]]"), patch.object(worker.call_channel,"start") as dial:
            worker.process(self.turn("call"))
            dial.assert_not_called()
        self.add("media","你来发吧")
        def voice_cli(*args):
            self.assertEqual(args[:4],("messages","send",ROOM,"--voice"))
            self.sent.append({"threadId":ROOM,"kind":"voice"})
            return {"id":str(len(self.sent))}
        with (patch.object(providers,"chat",return_value="[[voice]]\n听到了\n[[sticker]]"),
              patch.object(providers,"synthesize",return_value=b"staged fixture"),
              patch.object(worker,"cli",side_effect=voice_cli),
              patch.object(worker.account_stickers,"choose",return_value={"group":"包","label":"开心"}),
              patch.object(worker.account_stickers,"prepare",return_value={"id":"owned"})):
            worker.process(self.turn("media"))
        self.assertEqual([x["kind"] for x in self.sent],["voice","sticker"])
        self.assertTrue(all(x["threadId"]==ROOM for x in self.sent))
        self.assertEqual(self.turn("media")["status"],"accepted")

    def test_switching_off_during_model_call_cancels_delivery_and_new_input(self):
        self.add("switch","说点什么")
        def disable(*args,**kwargs):
            group_chats.configure(ROOM,"off")
            return "来啦"
        with patch.object(providers,"chat",side_effect=disable), self.assertRaises(worker.GroupTurnClosed):
            worker.process(self.turn("switch"))
        self.assertEqual(self.sent,[])
        self.assertEqual(self.turn("switch")["status"],"disabled")
        self.add("later","关闭后")
        self.assertIsNone(self.turn("later"))
        self.assertEqual(group_chats.list_page()["items"],[{"id":ROOM,"name":"测试群"}])
        with self.assertRaises(ValueError): group_chats.configure("friend","auto")
        with self.assertRaises(ValueError): group_chats.configure("left@chatroom","auto")

    def test_refresh_checks_current_membership_and_uses_the_latest_group_name(self):
        rejoined=False
        def native(path="",data=None):
            if not path: return {"ownUsername":"assistant"}
            if path=="/group-sync": return {"id":"operation","state":"queued"}
            if path=="/operations/operation": return {"state":"succeeded","result":{"complete":True}}
            if path.startswith("/groups?"): return {"items":[
                {"id":ROOM,"displayName":"新群名"},{"id":OTHER,"displayName":"旧群名"}]}
            if "/lookup?" in path:
                room=ROOM if "other" not in path else OTHER
                present=room==ROOM or rejoined
                return {"id":room,"version":12 if present else 0,"items":[{"id":"assistant"}] if present else []}
            self.fail(path)
        with patch.object(group_chats,"wx",side_effect=native), patch.object(group_chats.time,"sleep"):
            page=group_chats.refresh_page()
            rejoined=True
            after_rejoin=group_chats.list_page()
        self.assertTrue(page["catalogComplete"])
        self.assertEqual(page["items"],[{"id":ROOM,"name":"新群名"}])
        self.assertEqual(page["unverified"],[])
        self.assertEqual([room["id"] for room in after_rejoin["items"]],[ROOM,OTHER])

    def test_lookup_timeout_preserves_unverified_route_and_pagination(self):
        original=self.wx
        def native(path="",data=None):
            if "/lookup?" in path: raise TimeoutError()
            return original(path,data)
        with patch.object(group_chats,"wx",side_effect=native) as calls:
            page=group_chats.list_page("next page")
        calls.assert_any_call("/groups?limit=10&cursor=next%20page")
        self.assertEqual(page["items"],[])
        self.assertEqual(page["unverified"],[{"id":ROOM,"name":"测试群"}])
        self.assertEqual(page["nextCursor"],"page2")
        with st.database() as db: self.assertEqual(st.get(db,"group_routes")[ROOM]["mode"],"auto")

    def test_old_database_migrates_with_private_binding_and_history_intact(self):
        temp=self.enterContext(TemporaryDirectory())
        with patch.object(st,"ROOT",Path(temp)):
            with closing(sqlite3.connect(st.ROOT/"channel.sqlite")) as db, db:
                db.execute("CREATE TABLE turns(id TEXT PRIMARY KEY,peer TEXT,status TEXT,kind TEXT,input TEXT,answer TEXT,phase TEXT,error TEXT)")
                db.execute("INSERT INTO turns(id,peer,status,kind,answer) VALUES('old','friend','accepted','text','旧回复')")
                db.execute("CREATE TABLE incoming(id TEXT PRIMARY KEY,peer TEXT,kind INTEGER,body TEXT,stamp REAL,turn TEXT)")
                db.execute("INSERT INTO incoming VALUES('m','friend',1,'旧私聊',1,'old')")
            st.init()
            st.init()
            self.assertEqual([m["content"] for m in st.recent_dialogue("friend")],["旧私聊","旧回复"])

    def test_bubbles_are_separate_wechat_sends_and_retry_resumes_at_the_missing_bubble(self):
        self.add("msg","在吗",peer="friend",sender="friend")
        original=self.wx
        def disconnect(path,data=None,idem=None):
            if path=="/sends" and len(self.sent)==1:
                raise TimeoutError("test")
            return original(path,data,idem)
        draft="在\n\n。\n今天早点回来"
        with patch.object(providers,"chat",return_value=draft) as model:
            with patch.object(worker,"wx",side_effect=disconnect), self.assertRaises(TimeoutError):
                worker.process(self.turn("msg"))
            worker.process(self.turn("msg"))
        self.assertEqual(model.call_count,1)
        self.assertEqual([m["text"] for m in self.sent],["在","。","今天早点回来"])
        with st.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM turn_actions WHERE status='accepted'").fetchone()[0],3)
        self.assertEqual(len(st.recent_dialogue("friend")),2)
        panel.save_profile({"split_bubbles":False})
        self.add("plain","再说一遍",peer="friend",sender="friend")
        with patch.object(providers,"chat",return_value=draft):
            worker.process(self.turn("plain"))
        self.assertEqual(self.sent[-1]["text"],draft)

    def test_text_splitting_keeps_links_code_and_standalone_punctuation(self):
        code="例子\n```python\nprint('hello?')\n```"
        self.assertEqual(reply_actions.text_bubbles(code),[code])
        line="你看看这个链接 https://example.org/p?a=one!two 。我刚才看过了。"
        self.assertEqual(reply_actions.text_bubbles(line),[line.split("。")[0]+"。","我刚才看过了。"])
        self.assertEqual(reply_actions.text_bubbles("。。。"),["。。。"])
        self.assertEqual(len(reply_actions.text_bubbles("\n".join("第%d句"%n for n in range(9)))),6)

    def test_pinned_context_scope_and_adjustable_history(self):
        panel.save_profile({"history_turns":1,"pinned_context":"共同固定设定","private_context":"仅私聊的暗号"})
        for n in range(2):
            self.add("p"+str(n),f"历史{n}",peer="friend",sender="friend")
            with patch.object(providers,"chat",return_value="好的"):
                worker.process(self.turn("p"+str(n)))
        self.assertEqual(st.recent_dialogue("friend")[0]["content"],"历史1")
        self.add("g","群里好")
        with patch.object(providers,"chat",return_value="[[silent]]") as model:
            worker.process(self.turn("g"))
        group=model.call_args.args[1]
        self.assertIn("共同固定设定",group[0]["content"])
        self.assertNotIn("仅私聊的暗号",str(group))
        panel.save_profile({"history_turns":0})
        self.add("p2","当前私聊",peer="friend",sender="friend")
        with patch.object(providers,"chat",return_value="在呢") as model:
            worker.process(self.turn("p2"))
        private=model.call_args.args[1]
        self.assertEqual([m["role"] for m in private],["system","user"])
        self.assertIn("共同固定设定",private[0]["content"])
        self.assertIn("仅私聊的暗号",private[0]["content"])
        with st.database() as db: cfg=st.profile(db)
        call=call_channel.call_messages(cfg,[{"role":"user","content":"旧电话"}],"本次电话")
        self.assertEqual(len(call),2)
        self.assertIn("仅私聊的暗号",call[0]["content"])
        self.assertNotIn("旧电话",str(call))
