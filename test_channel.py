"""Isolated event-to-send tests. No live login, messages or API keys are used."""
from datetime import datetime, timezone
import base64
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import channel_state as st
import channel_worker as worker
import deepseek_case as ds
import pcm_audio
import providers
import spoken_reply
import account_stickers
import conversation_prompt
import worldbooks
import panel
import reply_actions
from concurrent.futures import Future


class ChannelTest(unittest.TestCase):
    def test_builtin_preview_matches_model_messages_and_omits_personal_text(self):
        personal={"persona":"个人测试人设","worldbook":"个人测试背景","pinned_context":"个人测试指令","private_context":"私聊测试指令"}
        parts=[{"type":"text","text":"当前消息"}]
        providers.save({"id":"elevenlabs","kind":"elevenlabs","name":"测试合成","key":"fixture"})
        providers.save({"id":"xfyun","kind":"xfyun","name":"测试听写","key":"fixture","app_id":"fixture","secret":"fixture"})
        for enabled in (True,False):
            cfg={**st.DEFAULT,"tts":"elevenlabs" if enabled else "off","voice":"fixture",
                 "tts_model":"eleven_v4","asr":"xfyun" if enabled else "off","split_bubbles":enabled}
            with st.database() as db: st.put(db,"profile",{**cfg,**personal})
            with patch.object(worker,"history",side_effect=AssertionError("preview read history")), \
                 patch.object(providers,"chat",side_effect=AssertionError("preview called a model")):
                entries=panel.builtin_prompts()
            texts={entry["title"]:entry["text"] for entry in entries}
            for value in personal.values(): self.assertNotIn(value,str(entries))
            with patch.object(worker,"history",return_value=[]):
                for title,group in (("微信私聊",None),("微信群聊",{"name":"群"})):
                    actual=worker.reply_messages(cfg,"peer",None,parts,["微信文字"],group=group)
                    self.assertEqual(texts[title],actual[0]["content"])
                    self.assertIn("只回标点或表情包",texts[title])
                    self.assertEqual(texts[title].count(reply_actions.STICKER),1)
                    self.assertEqual(texts[title].count(reply_actions.VOICE),int(enabled))
                    self.assertNotIn("[laughs]",texts[title])
            self.assertEqual(texts["实时语音电话"],worker.call_channel.call_messages(cfg,[],"喂")[0]["content"])
            self.assertEqual("想分气泡就换行" in texts["微信私聊"],enabled)
            self.assertEqual(reply_actions.CALL in texts["微信私聊"],enabled)
            self.assertNotIn(reply_actions.CALL,texts["微信群聊"])
            self.assertIn(reply_actions.SILENT,texts["微信群聊"])
            self.assertNotIn(reply_actions.SILENT,texts["微信私聊"])
            self.assertEqual("[laughs]" in texts["私聊请求语音"],enabled)
            self.assertEqual([e["title"] for e in entries if e["kind"]=="scene"],["微信私聊","微信群聊","实时语音电话"])

    def test_chat_only_explains_audio_actions_with_ready_routes(self):
        cfg=st.DEFAULT|{"voice":"sample","tts_model":"eleven_v4"}
        def instructions(**changes):
            return worker.chat_instructions(cfg|changes, group=False)
        self.assertNotIn(reply_actions.VOICE,instructions())
        providers.save({"id":"elevenlabs","kind":"elevenlabs","name":"测试合成","key":"fixture"})
        self.assertIn(reply_actions.VOICE,instructions())
        self.assertNotIn(reply_actions.VOICE,instructions(voice=""))
        self.assertNotIn(reply_actions.VOICE,instructions(tts_model=""))
        self.assertNotIn(reply_actions.CALL,instructions())
        custom=providers.save({"kind":"compatible","name":"听写","base":"http://localhost:9000/v1"})["id"]
        self.assertNotIn(reply_actions.CALL,instructions(asr=custom))
        self.assertIn(reply_actions.CALL,instructions(asr=custom,asr_model="recognizer"))

    def test_unknown_sticker_reaches_chat_and_is_recognized_once_across_messages(self):
        self.bind()
        self.incoming("st1","",kind=47);self.incoming("st2","",kind=47)
        raw=ds.chart()
        digest=hashlib.sha256(raw).hexdigest()
        def assets(path):
            return {"items":[{"messageId":path.split("/")[2],"kind":"sticker","variant":"original","sha256":digest}]}
        with st.database() as db:
            rows=db.execute("SELECT * FROM incoming WHERE kind=47 ORDER BY rowid").fetchall()
            st.put(db,"sticker_names",{})
        with patch.object(account_stickers,"wx",side_effect=assets),patch.object(account_stickers,"media",return_value=raw) as read,patch.object(providers,"chat",return_value="小兔子正把胡萝卜放进篮子") as model:
            parts,words,_=worker.input_parts(rows,st.DEFAULT|{"sticker_vision":"once"})
            self.assertEqual(words,[words[0],words[0]])
            self.assertIn("胡萝卜",parts[0]["text"])
            self.assertTrue(model.call_args.kwargs["vision"]);model.assert_called_once();read.assert_called_once()
        with st.database() as db:
            self.assertEqual(db.execute("select body from incoming where id='st2'").fetchone()[0],words[0])
        account_stickers.summary([{"item":hashlib.md5(raw).hexdigest(),"group":"整包","label":"开饭","caption":"开饭","sha256":digest}])
        with patch.object(account_stickers,"wx",side_effect=assets),patch.object(account_stickers,"media",side_effect=AssertionError("unexpected download")):
            self.assertEqual(account_stickers.received("st3",st.DEFAULT),"〔微信表情 · 整包 · 开饭〕")

    def test_worldbook_import_selects_only_active_entries_in_chat_and_call(self):
        source=json.dumps({"entries":{"0":{"constant":True,"content":"常驻背景"},"1":{"key":["山茶"],"content":"花园故事"},
                                       "2":{"constant":True,"disable":True,"content":"停用内容"}}},ensure_ascii=False)
        view=worldbooks.preview("花园.json",base64.b64encode(source.encode()).decode())
        self.assertEqual((view["count"],view["enabled"]),(3,2))
        ref=panel.save_asset({"kind":"worldbook","name":"花园","format":view["format"],"body":view["body"],"select":True})["id"]
        panel.save_profile({"memory_url":"http://127.0.0.1:8123"})
        with st.database() as db: cfg=st.profile(db)
        self.assertEqual(cfg["worldbook_id"],ref)
        self.assertEqual(cfg["memory_url"],"http://127.0.0.1:8123")
        self.assertEqual(worldbooks.sections(cfg,[],"在吗"),("常驻背景",""))
        chat=worker.reply_messages(cfg,"peer",None,[{"type":"text","text":"山茶"}],["微信文字"])
        call=worker.call_channel.call_messages(cfg,[{"role":"user","content":"刚说到山茶"}],"然后呢")
        for messages in (chat,call):
            self.assertIn("常驻背景",messages[0]["content"])
            self.assertNotIn("花园故事",messages[0]["content"])
            self.assertIn("花园故事",str(messages[-1]["content"]))
            self.assertNotIn("停用内容",str(messages))
        panel.save_profile({"worldbook_id":""})
        with st.database() as db:
            self.assertEqual(worldbooks.sections(st.profile(db),[],"山茶"),("",""))
            self.assertEqual(st.get(db,"worldbook-source:"+ref),source)

    def test_clipped_worldbook_keeps_original_entry_priority_and_a_stable_prefix(self):
        entries = [{"constant":True,"content":"底色"}, {"key":["山茶"],"content":"一二三四"},
                   {"constant":True,"content":"甲乙丙丁戊己庚"}]
        with st.database() as db:
            st.put(db,"worldbook-entries:book",worldbooks.parse(json.dumps(entries),"json"))
        with patch.object(worldbooks,"TURN_LIMIT",12):
            cfg={"worldbook_id":"book"}
            self.assertEqual(worldbooks.sections(cfg,[],"你好"),("底色","甲乙丙丁戊己庚"))
            self.assertEqual(worldbooks.sections(cfg,[],"山茶"),("底色","一二三四\n\n甲乙"))

    def test_stable_settings_history_current_parts_and_beijing_clock(self):
        cfg=st.DEFAULT|{"persona":"纸鹤","worldbook":"画室","voice":"fixture","tts_model":"eleven_v4"}
        recent=[{"role":"user","content":"上回留的什么色？"},{"role":"assistant","content":"淡青"}]
        first=[{"type":"text","text":"开灯了吗"},{"type":"text","text":"我刚进门"}]
        mixed=[ds.image_part(ds.chart(),"image/png"),{"type":"text","text":"〔语音转写〕\n这次呢〔开心〕"}]
        before=datetime(2026,11,26,15,59,tzinfo=timezone.utc)
        after=datetime(2026,11,26,16,0,tzinfo=timezone.utc)
        with patch.object(worker,"history",return_value=recent):
            plain=worker.reply_messages(cfg,"peer",None,first,["微信文字","微信文字"],now=before)
            voice=worker.reply_messages(cfg,"peer",None,mixed,["微信图片","微信语音条（已转写）"],now=after)
            edited=worker.reply_messages(cfg|{"persona":"新的人设"},"peer",None,first,["微信文字"],now=after)
        self.assertEqual(plain[:-1],voice[:-1])
        self.assertNotIn("[laughs]",plain[0]["content"])
        self.assertNotIn("[laughs]",str(voice[-1]["content"]))
        self.assertNotEqual(edited[0],plain[0])
        self.assertEqual(plain[1:-1],recent)
        body,clock=plain[-1]["content"].rsplit(conversation_prompt.CLOCK_HEADER,1)
        self.assertEqual(body,"开灯了吗\n我刚进门")
        self.assertEqual(voice[-1]["content"][:-2],mixed)
        self.assertEqual(clock,"2026-11-26 23:59 星期四")
        self.assertIn("2026-11-27 00:00 星期五",voice[-1]["content"][-1]["text"])
        amended=conversation_prompt.with_chat_note(voice,"本轮补充")
        self.assertEqual(amended[:-1],voice[:-1])
        self.assertEqual(amended[-1]["content"][:-2],voice[-1]["content"][:-1])
        self.assertEqual(amended[-1]["content"][-1],voice[-1]["content"][-1])
        call=worker.call_channel.call_messages(cfg,recent,"我的新一句",now=after)
        self.assertEqual(call[1:-1],recent)
        self.assertTrue(call[-1]["content"].startswith("我的新一句\n"))
        self.assertTrue(call[-1]["content"].endswith(voice[-1]["content"][-1]["text"]))
        self.assertEqual(call[0]["content"].split("画室")[0],plain[0]["content"].split("画室")[0])

    def test_worldbook_docx_and_markdown_are_previewed_without_saving(self):
        from io import BytesIO
        from zipfile import ZipFile
        raw=BytesIO()
        with ZipFile(raw,"w") as z:
            z.writestr("word/document.xml",'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>第一行</w:t></w:r></w:p><w:p><w:r><w:t>下一行</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(worldbooks.preview("背景.docx",base64.b64encode(raw.getvalue()).decode())["body"],"第一行\n下一行")
        self.assertEqual(worldbooks.preview("资料.md",base64.b64encode("资料".encode()).decode())["body"],"资料")
        with self.assertRaises(ValueError): worldbooks.preview("坏文件.json","e30=")
        with st.database() as db: self.assertFalse(st.get(db,"library"))

    def test_model_actions_are_whole_lines_and_single_dial_is_idempotent(self):
        self.assertEqual(reply_actions.parse("我先说一句\n[[voice]]\n我在听。\n[[sticker]]\n回头聊"),
                         [("text","我先说一句"),("voice","我在听。"),("sticker",""),("text","回头聊")])
        self.assertIsNone(reply_actions.parse("call"))
        self.assertIsNone(reply_actions.parse("它说 `[[call]]` 的用法是\n```\n[[call]]\n```"))
        self.assertIsNone(reply_actions.parse("你刚说 [[voice]] 是什么意思？"))
        self.assertEqual(reply_actions.parse("[[call]]"),[("call","")])
        self.assertEqual(worker.mode_for(["打给我打给我"]),("call",""))
        self.assertEqual(worker.mode_for(["你打给我","不用了"]),("text",""))
        self.assertEqual(worker.mode_for(["他说\n〔引用〕打给我"]),("text",""))
        self.bind();self.incoming("incoming","打给我打给我")
        item=self.turns()[0]
        with patch.object(worker.call_channel,"start") as start:
            worker.process(item);worker.process(item)
        start.assert_called_once_with("dial",expected_peer="peer",turn_id=item["id"])
        self.assertEqual(self.deliveries,[])

    def test_mixed_reply_resumes_without_duplicate_text_and_keeps_marks_private(self):
        self.bind()
        with st.database() as db: st.put(db,"profile",{"tts_model":"eleven_v4","voice":"fixture"})
        self.incoming("msg","今天抽中了")
        reply="真中了啊\n[[voice]]\n[laughs] 这下能睡着了吧。\n[[sticker]]"
        owned={"group":"已下载","label":"开心","item":"own","pack":"pack"}
        with patch.object(providers,"chat",return_value=reply) as model,patch.object(providers,"synthesize",return_value=pcm_audio.as_wav(bytes(640))) as tts,patch.object(worker,"cli",side_effect=[TimeoutError(),{"id":"job"}]) as send_voice,patch.object(account_stickers,"choose",return_value=owned) as pick,patch.object(account_stickers,"prepare",return_value={"id":"fixture"}):
            with self.assertRaises(TimeoutError): worker.process(self.turns()[0])
            worker.process(self.turns()[0])
        self.assertEqual(model.call_count,1)
        self.assertEqual(tts.call_count,1)
        self.assertEqual(tts.call_args.args[-1],"[laughs] 这下能睡着了吧。")
        self.assertEqual(send_voice.call_args_list[0],send_voice.call_args_list[1])
        self.assertIn("今天抽中了",pick.call_args.args[1])
        self.assertEqual([v[0].get("text","sticker") for v in self.deliveries],["真中了啊","sticker"])
        self.assertEqual(self.turns()[0]["status"],"accepted")
        self.assertNotIn("[[",self.turns()[0]["answer"])

    def test_bare_voice_marker_uses_context_to_compose_original_words(self):
        self.bind()
        with st.database() as db: st.put(db,"profile",{"tts_model":"eleven_v4","voice":"fixture"})
        st.remember_call_line("peer","我做的蛋糕塌了","那勺子总还能用吧")
        st.remember_call_line("another","私有内容","不会串进来")
        self.incoming("msg","发个语音")
        with patch.object(providers,"chat",side_effect=["[[voice]]","留一勺给我。"]) as model,patch.object(providers,"synthesize",return_value=pcm_audio.as_wav(bytes(640))) as tts,patch.object(worker,"cli",return_value={"id":"job"}):
            worker.process(self.turns()[0])
        context=str(model.call_args.args[1])
        draft, refinement = [call.args[1] for call in model.call_args_list]
        self.assertEqual(refinement[:-1],draft[:-1])
        first,first_clock=draft[-1]["content"].rsplit(conversation_prompt.CLOCK_HEADER,1)
        second,second_clock=refinement[-1]["content"].rsplit(conversation_prompt.CLOCK_HEADER,1)
        self.assertIn(worker.VOICE_REQUEST_NOTE,first)
        self.assertNotIn(worker.VOICE_REQUEST_NOTE,second)
        self.assertIn(worker.MISSING_VOICE_NOTE,second)
        self.assertEqual(second.count(spoken_reply.VOICE_INSTRUCTION),1)
        self.assertEqual(first_clock,second_clock)
        self.assertEqual(self.turns()[0]["input"],"发个语音")
        self.assertIn("蛋糕",context)
        self.assertNotIn("私有内容",context)
        self.assertEqual(tts.call_args.args[-1],"留一勺给我。")

    def test_previous_literal_text_is_not_replayed_as_a_successful_media_action(self):
        self.bind()
        self.incoming("old","收到没有")
        turn=self.turns()[0]["id"]
        old="〔微信语音条〕看到了\n〔微信表情包〕"
        worker.update(turn,status="accepted",kind="text",answer=old,receipt="fixture")
        messages=worker.reply_messages(st.DEFAULT,"peer",None,[{"type":"text","text":"真的？"}],["微信文字"])
        self.assertEqual(messages[2],{"role":"assistant","content":"看到了"})
        self.assertIn("误发成了文字",messages[-1]["content"])
        self.assertNotIn("[[sticker]]",messages[2]["content"])
        self.assertEqual(self.turns()[0]["answer"],old)

        for quoted in ("`〔微信表情包〕` 这个是文字","“〔微信语音条〕” 是记录标签","看这个\n```\n〔微信表情包〕\n```","[微笑]","`[语音]` 是一个例子"):
            self.assertEqual(reply_actions.without_display_labels(quoted),(quoted,False))
            reply_actions.check_media_format(quoted,split=True)
        with self.assertRaises(reply_actions.MediaFormatError):
            reply_actions.check_media_format("这是一段足够长的回复来模拟分成两个气泡。〔微信表情包〕",split=True)
        for label in ("[语音]","【微信表情】","[voice]"):
            self.assertEqual(reply_actions.without_display_labels(label+" 早安"),("早安",True))
            with self.assertRaises(reply_actions.MediaFormatError):
                reply_actions.check_media_format(label+" 早安",split=True)

    def test_pending_parts_are_not_shown_as_sent_and_calls_only_replay_speech(self):
        self.bind()
        self.incoming("old","开窗了吗")
        turn=self.turns()[0]["id"]
        worker.save_plan(turn,["开窗了吗"],[("voice","打开了"),("sticker",""),("text","晚些再说")],"")
        with st.database() as db:
            db.execute("UPDATE turns SET status='failed' WHERE id=?",(turn,))
            db.execute("UPDATE turn_actions SET status='accepted',receipt='fixture' WHERE turn_id=? AND pos=0",(turn,))
        self.assertEqual(st.recent_dialogue("peer")[-1]["content"],"[[voice]]\n打开了")
        self.assertEqual(st.recent_dialogue("peer",for_call=True)[-1]["content"],"打开了")

    def test_punctuation_and_standalone_sticker_are_complete_replies(self):
        self.bind()
        self.incoming("odd","她把闹钟定在起床之后")
        with patch.object(providers,"chat",return_value="？"):
            worker.process(self.turns()[0])
        self.assertEqual(self.deliveries[0][0],{"threadId":"peer","text":"？"})

        self.incoming("again","她又睡过去了")
        owned={"group":"测试包","label":"捂脸","item":"own","pack":"pack"}
        with (patch.object(providers,"chat",return_value=reply_actions.STICKER),
              patch.object(account_stickers,"choose",return_value=owned) as choose,
              patch.object(account_stickers,"prepare",return_value={"kind":"prepared","preparationId":"fixture"})):
            worker.process(self.turns()[1])
        self.assertEqual(len(self.deliveries),2)
        self.assertEqual(self.deliveries[1][0]["kind"],"sticker")
        self.assertIn("助手：？",choose.call_args.args[1])
        self.assertIn("对方这轮：她又睡过去了",choose.call_args.args[1])
        self.assertEqual(self.turns()[1]["status"],"accepted")

    def test_local_ear_uses_independent_transcript_and_an_inline_label(self):
        result=Future();result.set_result(("难过","tagged"))
        cfg=st.DEFAULT|{"tone_service":"local-sensevoice"}
        with patch("sensevoice_local.start",return_value=result),patch.object(providers,"recognize_pcm",return_value="我回来啦"):
            words,tone,source,status=providers.hear_utterance(cfg,bytes(640))
        self.assertEqual((providers.with_tone(words,tone),source,status),("我回来啦〔难过〕","讯飞听写","tagged"))

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.object(st,"ROOT",Path(temp.name)))
        self.enterContext(patch.object(ds, "KEY", st.ROOT / "deepseek.key"))
        st.init()
        providers.initialize()
        self.messages = {}
        self.deliveries = []
        self.enterContext(patch.object(worker,"wx",side_effect=self.wx))

    def wx(self,path,data=None,idem=None):
        if path.startswith("/threads/"):
            return {"kind":"direct"}
        if path.startswith("/messages/"):
            return self.messages[path.split("/")[-1]]
        if path == "/sends":
            self.deliveries.append((data,idem))
            return {"id":"job"}
        if path == "/sends/job":
            return {"state":"accepted","serverId":"server-receipt","threadId":"peer"}
        raise AssertionError(path)

    def incoming(self,mid,body,*,peer="peer",kind=1,stamp=None,origin="live",direction="incoming"):
        self.messages[mid] = {"id":mid,"body":body,"threadId":peer,"senderId":peer,"direction":direction,"type":kind}
        event = {"kind":"message.committed","createdAt":datetime.fromtimestamp(stamp or time.time(),timezone.utc).isoformat(),
                 "data":{"origin":origin,"direction":direction,"threadId":peer,"messageId":mid}}
        with st.database() as db:
            worker.ingest(db,event)

    def bind(self):
        with st.database() as db:
            st.put(db,"pair",{"hash":hashlib.sha256(b"test-pair").hexdigest(),"expires":time.time()+30})
        self.incoming("code","test-pair")
        with st.database() as db:
            self.assertEqual("peer",st.get(db,"peer"))

    def turns(self):
        with st.database() as db:
            return db.execute("SELECT * FROM turns ORDER BY rowid").fetchall()

    def test_pair_is_one_private_chat_no_backfill_or_echo(self):
        self.incoming("early","old")
        self.bind()
        self.incoming("other","hello",peer="stranger")
        self.incoming("historic","old",origin="sync")
        self.incoming("out","my own text",direction="outgoing")
        self.assertEqual([],self.turns())

    def test_new_turn_after_publication_even_within_seven_seconds(self):
        self.bind()
        now = time.time()
        self.incoming("1","第一句",stamp=now)
        self.incoming("2","第二句",stamp=now+6)
        self.incoming("2","第二句",stamp=now+6)
        self.incoming("out","上一轮回信",direction="outgoing",stamp=now+7)
        self.incoming("3","新一轮",stamp=now+8)
        self.incoming("4","隔了九秒",stamp=now+17)
        with st.database() as db:
            counts = [r[0] for r in db.execute("SELECT count(*) FROM incoming GROUP BY turn ORDER BY min(rowid)")]
        self.assertEqual(counts,[2,1,1])

    def test_custom_window_restarts_for_each_media_message_until_a_reply(self):
        self.bind()
        with st.database() as db:
            st.put(db, "profile", {"name": "原有私聊"})
            self.assertEqual(st.merge_window(st.profile(db)), 7)
        panel.save_profile({"merge_wait_seconds": 12})
        start = int(time.time())
        self.incoming("text", "一起看", stamp=start)
        self.incoming("photo", "", kind=3, stamp=start + 10)
        self.incoming("voice", "", kind=34, stamp=start + 21)
        self.incoming("sticker", "", kind=47, stamp=start + 32)
        waiting, = self.turns()
        self.assertEqual(waiting["next_try"], start + 44)
        self.incoming("out", "回信", direction="outgoing", stamp=start + 33)
        self.incoming("next", "另外一件事", stamp=start + 34)
        self.incoming("later", "已经过了十二秒", stamp=start + 47)
        with st.database() as db:
            counts = db.execute("SELECT COUNT(*) FROM incoming GROUP BY turn ORDER BY MIN(rowid)")
            self.assertEqual([row[0] for row in counts], [4, 1, 1])
            self.assertEqual(st.profile(db)["name"], "原有私聊")

    def test_turning_merge_off_flushes_one_batch_and_replies_to_each_new_message(self):
        self.bind()
        panel.save_profile({"merge_wait_seconds": 15})
        start = int(time.time())
        self.incoming("a", "第一行", stamp=start)
        self.incoming("b", "第二行", stamp=start + 1)
        panel.save_profile({"merge_messages": False})
        self.incoming("c", "单独一句", stamp=start + 1)
        self.incoming("d", "再来一句", stamp=start + 1)
        self.incoming("d", "再来一句", stamp=start + 1)
        rows = self.turns()
        self.assertEqual([(r["status"], r["next_try"]) for r in rows], [("ready", 0)] * 3)
        with patch.object(providers, "chat", side_effect=["收到两行", "收到一句", "再回一句"]):
            for row in rows:
                worker.process(row)
        self.assertEqual([msg["text"] for msg, _ in self.deliveries], ["收到两行", "收到一句", "再回一句"])
        self.assertEqual([r["input"] for r in self.turns()], ["第一行\n第二行", "单独一句", "再来一句"])
        st.init()
        with st.database() as db:
            cfg = st.profile(db)
            self.assertEqual((cfg["merge_messages"], cfg["merge_wait_seconds"]), (False, 15))
        panel.save_profile({"merge_messages": True})
        self.incoming("e", "重新开始", stamp=start + 2)
        self.assertEqual(self.turns()[-1]["next_try"], start + 17)

    def test_editing_a_live_timer_uses_the_last_message_and_leaves_expired_turns_closed(self):
        self.bind()
        start = int(time.time())
        self.incoming("a", "第一条", stamp=start)
        self.incoming("b", "第二条", stamp=start + 3)
        with patch.object(panel.time, "time", return_value=start + 5):
            panel.save_profile({"merge_wait_seconds": 15})
            self.assertEqual(self.turns()[0]["next_try"], start + 18)
            panel.save_profile({"merge_wait_seconds": 1})
        self.assertEqual(self.turns()[0]["status"], "ready")
        self.incoming("c", "新一轮", stamp=start + 6)
        with patch.object(panel.time, "time", return_value=start + 8):
            panel.save_profile({"merge_wait_seconds": 20})
        self.incoming("d", "下一轮", stamp=start + 9)
        self.assertEqual([r["status"] for r in self.turns()], ["ready", "ready", "collecting"])

    def test_invalid_merge_settings_do_not_modify_the_saved_settings_or_deadline(self):
        self.bind()
        self.incoming("a", "今天", stamp=int(time.time()))
        deadline = self.turns()[0]["next_try"]
        for patch_value in ({"merge_wait_seconds": 0}, {"merge_wait_seconds": -1}, {"merge_wait_seconds": 2.5},
                            {"merge_wait_seconds": True}, {"merge_wait_seconds": 2**53}, {"merge_messages": "off"}):
            with self.subTest(patch_value=patch_value), self.assertRaises(ValueError):
                panel.save_profile(patch_value)
        with st.database() as db:
            self.assertEqual((st.profile(db)["merge_messages"], st.profile(db)["merge_wait_seconds"]), (True, 7))
        self.assertEqual(self.turns()[0]["next_try"], deadline)

    def test_images_are_pixels_and_persona_and_worldbook_reach_selected_model(self):
        self.bind()
        self.incoming("img","",kind=3)
        self.incoming("words","这是什么")
        with st.database() as db:
            st.put(db,"profile",{"vision_model":"my-vision","persona":"人设甲","worldbook":"设定乙"})
        with patch.object(worker,"media",return_value=ds.chart()), patch.object(providers,"api_key",return_value="fixture"), patch.object(ds,"chat",return_value="左红右蓝") as model:
            worker.process(self.turns()[0])
        messages = model.call_args.args[0]
        self.assertEqual(model.call_args.kwargs["model"],"my-vision")
        self.assertIn("人设甲",messages[0]["content"])
        self.assertIn("设定乙",messages[0]["content"])
        self.assertTrue(messages[-1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(self.deliveries[0][0],{"threadId":"peer","text":"左红右蓝"})
        self.assertEqual(self.turns()[0]["receipt"],"server-receipt")

    def test_voice_and_sticker_use_native_routes(self):
        self.bind()
        self.incoming("v","/语音 现在听得见了")
        pcm = bytes(range(256)) * 8
        def submit(*args):
            self.assertEqual(args[:4], ("messages", "send", "peer", "--voice"))
            self.assertEqual(pcm_audio.samples(args[4].read_bytes()), pcm)
            return {"id":"job"}
        with patch.object(providers,"synthesize_pcm",return_value=pcm), patch.object(worker,"cli",side_effect=submit) as api:
            worker.process(self.turns()[0])
        args = api.call_args.args
        self.assertEqual(args[:4],("messages","send","peer","--voice"))
        self.assertEqual(self.turns()[0]["kind"],"voice")
        self.incoming("s","发个表情包")
        wire = {"kind":"prepared","preparationId":"fixture"}
        picked={"pack":"owned","item":"one","group":"我的整包","label":"开心"}
        with patch.object(account_stickers,"choose",return_value=picked),patch.object(account_stickers,"prepare",return_value=wire):
            worker.process(self.turns()[1])
        self.assertEqual(self.deliveries[0][0],{"kind":"sticker","threadId":"peer","source":wire})
        self.assertEqual(json.loads(self.turns()[1]["wire"]),wire)

    def test_retry_after_submit_uses_existing_receipt(self):
        self.bind()
        self.incoming("1","说句话")
        turn = self.turns()[0]
        worker.update(turn["id"], input="说句话",answer="听着呢",kind="text",send_id="job")
        worker.process(self.turns()[0])
        self.assertEqual(self.deliveries,[])
        self.assertEqual(self.turns()[0]["status"],"accepted")

    def test_voice_direction_reaches_one_native_note_and_stays_out_of_dialogue(self):
        self.bind()
        with st.database() as db:
            st.put(db,"profile",{"tts_model":"eleven_v4","voice":"fixture"})
        self.incoming("request","可以给我发条语音听听吗？")
        spoken=pcm_audio.as_wav(bytes(640))
        with patch.object(providers,"chat",return_value="[laughs] 最后一块蛋糕给你。") as model, \
             patch.object(providers,"synthesize",return_value=spoken) as tts, \
             patch.object(worker,"cli",side_effect=[TimeoutError(),{"id":"job"}]) as submit:
            with self.assertRaises(TimeoutError):
                worker.process(self.turns()[0])
            worker.process(self.turns()[0])
        self.assertEqual(submit.call_args_list[0],submit.call_args_list[1])
        self.assertEqual(model.call_count,1)
        self.assertIn("[laughs]",model.call_args.args[1][-1]["content"])
        self.assertEqual(tts.call_args.args[1],"[laughs] 最后一块蛋糕给你。")
        self.assertEqual(tts.call_count,1)
        result=self.turns()[0]
        self.assertEqual((result["kind"],result["answer"],result["status"]),("voice","最后一块蛋糕给你。","accepted"))
        self.assertEqual(self.deliveries,[])
        for phrase in ("别发语音","我喜欢听你说话","我刚发的语音呢"):
            self.assertEqual(worker.mode_for([phrase]),("text",""))
        line=spoken_reply.parse("[rain sounds][sighs] 唉。")
        self.assertEqual((line.words,line.delivery),("唉。","[sighs] 唉。"))

    def test_optional_note_source_rate_does_not_change_call_pcm(self):
        cfg=st.DEFAULT|{"note_rate":"24000","tts_model":"eleven_v4","voice":"fixture"}
        with patch.object(providers,"request",return_value=bytes(4800)) as request:
            wav=providers.synthesize(cfg,"嗯")
            self.assertEqual(int.from_bytes(wav[24:28],"little"),24000)
            self.assertEqual(request.call_args.kwargs["pcm_rate"],24000)
            self.assertEqual(providers.synthesize_pcm(cfg,"嗯"),bytes(4800))
            self.assertEqual(request.call_args.kwargs["pcm_rate"],16000)

    def test_unconfirmed_receipt_is_not_a_success(self):
        self.bind()
        self.incoming("1","你好")
        turn = self.turns()[0]
        worker.update(turn["id"], input="你好",answer="你好",kind="text",send_id="job")
        with patch.object(worker,"wx",return_value={"state":"accepted","threadId":"peer"}), self.assertRaises(ValueError):
            worker.process(self.turns()[0])
        self.assertIsNone(self.turns()[0]["receipt"])

    def test_separate_providers_and_voice_id(self):
        settings=st.DEFAULT | {"tts_model":"tts-example","voice":"fixture-voice","asr":"xfyun"}
        providers.save({"id":"elevenlabs","name":"ElevenLabs","kind":"elevenlabs","key":"voice-test"})
        providers.save({"id":"xfyun","name":"讯飞","kind":"xfyun","key":"iat-key","secret":"iat-secret","app_id":"iat-app"})
        pcm=b"\x12\x34"*16
        with patch.object(ds,"service_request",return_value=pcm) as net:
            wav=providers.synthesize(settings,"你好")
            self.assertEqual(pcm_audio.samples(wav),pcm)
            url,headers,body=net.call_args.args
            self.assertIn("fixture-voice",url)
            self.assertTrue(url.endswith("output_format=pcm_16000"))
            self.assertTrue(net.call_args.kwargs["pcm_response"])
            self.assertEqual(headers,{"xi-api-key":"voice-test"})
            self.assertEqual(body["model_id"],"tts-example")
        with patch("voice_iat.recognize_pcm",return_value="转写文字") as iat, patch("pcm_audio.subprocess.run",side_effect=AssertionError("Unexpected transcode")):
            self.assertEqual(providers.recognize(settings,wav),"转写文字")
            self.assertEqual(iat.call_args.args[0],pcm)
            credentials=iat.call_args.args[1]
            self.assertEqual((credentials["key"],credentials["app_id"]),("iat-key","iat-app"))
        with st.database() as db:
            shown=json.dumps(providers.all_public(db))
        self.assertNotIn("iat-secret",shown)
        self.assertNotIn("voice-test",shown)

    def test_extra_provider_routes_and_does_not_reuse_credentials_on_url_change(self):
        form={"name":"服务乙","kind":"compatible","base":"https://example.org/v1","key":"demo-key","manual":["demo/model"]}
        ident=providers.save(form)["id"]
        cfg=st.DEFAULT|{"chat_service":ident,"chat_model":"demo/model"}
        with patch.object(ds,"service_request",return_value={"choices":[{"message":{"content":"收到"}}]}) as net:
            self.assertEqual(providers.chat(cfg,[{"role":"user","content":"hi"}]),"收到")
            self.assertEqual(net.call_args.args[0],"https://example.org/v1/chat/completions")
            self.assertEqual(net.call_args.args[1],{"Authorization":"Bearer demo-key"})
        with self.assertRaises(ValueError):
            providers.save(form | {"id":ident,"base":"https://example.net/v1","key":""})

    def test_requested_sticker_stays_in_own_collection_and_is_pinned_on_retry(self):
        self.assertEqual(worker.mode_for(["发个「开心」的表情包"]),("sticker","开心"))
        self.assertEqual(worker.mode_for(["/表情 心动"]),("sticker","心动"))
        self.assertEqual(worker.mode_for(["收到了的话请发个表情包。"]),("sticker",""))
        self.assertEqual(account_stickers.request_details("发个叫摸摸头的表情包"),("摸摸头",True))
        self.assertEqual(worker.mode_for(["我不想发表情包"]),("text",""))
        rows=[{"group":"甲组","pack":"pack","item":"one","label":"开心"},{"group":"我的收藏","pack":"","item":"two","label":"摸鱼"}]
        with patch.object(account_stickers,"catalog",return_value=rows),patch.object(account_stickers,"synchronize",return_value=rows):
            picked=account_stickers.choose("new","开心")
            self.assertEqual(picked,rows[0])
            self.assertEqual(account_stickers.choose("new","别的"),picked)
            self.assertEqual(account_stickers.choose("saved","摸鱼"),rows[1])
            with self.assertRaises(ValueError):
                account_stickers.choose("missing","不存在")
            with patch.object(providers,"chat",return_value="2"):
                self.assertEqual(account_stickers.choose("mood","可爱",description_settings=st.DEFAULT),rows[1])

    def test_newly_added_and_removed_pack_membership_changes_next_pick(self):
        first={"group":"原整包","pack":"a","item":"a1","label":"甲"}
        new={"group":"新整包","pack":"b","item":"b2","label":"乙"}
        with patch.object(account_stickers,"synchronize",side_effect=[[first],[new]]) as sync:
            self.assertEqual(account_stickers.choose("first",""),first)
            self.assertEqual(account_stickers.choose("next",""),new)
            self.assertEqual(sync.call_count,2)

    def test_sticker_names_are_offered_only_when_selection_is_needed(self):
        rows=[{"group":"花房","pack":"p","item":"one","label":"铃兰"},
              {"group":"夜空","pack":"q","item":"two","label":"月亮"}]
        cfg=st.DEFAULT|{"persona":"测试设定"}
        with patch.object(account_stickers,"synchronize",return_value=rows), patch.object(providers,"chat",return_value="2") as choose:
            base=worker.reply_messages(cfg,"peer",None,[{"type":"text","text":"你好"}],["微信文字"])
            self.assertNotIn("铃兰",str(base))
            self.assertNotIn("月亮",str(base))
            self.assertEqual(account_stickers.choose("direct","铃兰",description_settings=cfg),rows[0])
            choose.assert_not_called()
            self.assertEqual(account_stickers.choose("intent","",description_settings=cfg),rows[1])
            self.assertEqual(account_stickers.choose("intent","另一种",description_settings=cfg),rows[1])
            choose.assert_called_once()
            self.assertEqual(json.loads(choose.call_args.args[1][-1]["content"])["可选表情"],{"花房":[[1,"铃兰"]],"夜空":[[2,"月亮"]]})
            choose.return_value="99"
            with self.assertRaises(ValueError):
                account_stickers.choose("bad","按语境选",description_settings=cfg)
            with st.database() as db: self.assertIsNone(st.get(db,"sticker:bad"))

    def test_voice_origin_survives_current_input_history_and_retry(self):
        self.bind()
        self.incoming("v","",kind=34)
        turn=self.turns()[0]
        with st.database() as db:
            rows=db.execute("SELECT * FROM incoming WHERE turn=?",(turn["id"],)).fetchall()
        with patch.object(worker,"media",return_value=pcm_audio.as_wav(bytes(640))),patch.object(providers,"recognize_pcm",return_value="名字是青禾") as asr:
            parts,lines,sources=worker.input_parts(rows,st.DEFAULT)
            self.assertEqual(worker.input_parts(rows,st.DEFAULT),(parts,lines,sources))
        self.assertEqual(asr.call_count,1)
        messages=worker.reply_messages(st.DEFAULT,"peer",turn["id"],parts,sources)
        self.assertIn("讯飞听写已转写",messages[-1]["content"])
        self.assertTrue(messages[-1]["content"].startswith("〔语音转写〕\n名字是青禾"))
        self.assertEqual(lines,["名字是青禾"])
        worker.update(turn["id"],status="accepted",input=lines[0],kind="text",answer="青禾")
        self.incoming("t","下一个问题")
        next_turn=self.turns()[1]
        self.assertIn("微信语音转写",worker.history("peer",next_turn["id"])[0]["content"])
        self.assertEqual(worker.history("peer",turn["id"]),[])

    def test_each_note_is_heard_once_and_its_cue_stays_on_its_own_words(self):
        self.bind()
        ident=providers.save({"name":"耳朵","kind":"compatible","base":"https://example.org/v1","manual":["ear"]})["id"]
        cfg=st.DEFAULT|{"tone_service":ident,"tone_model":"ear"}
        self.incoming("a","",kind=34)
        self.incoming("b","",kind=34)
        turn=self.turns()[0]
        clips={key:pcm_audio.as_wav(bytes([n,0])*320) for key,n in (("a",8),("b",12))}
        observations=iter([("我回来了。","开心","high"),("没事。","难过","low")])
        def listen(service,path,payload,**options):
            spoken,tone,confidence=next(observations)
            clip=payload["messages"][-1]["content"]
            self.assertEqual(len(clip),1)
            self.assertEqual(base64.b64decode(clip[0]["input_audio"]["data"]),clips["a" if spoken.startswith("我") else "b"])
            return {"choices":[{"message":{"content":json.dumps({"transcript":spoken,"tone":tone,"confidence":confidence},ensure_ascii=False)}}]}
        with st.database() as db:
            rows=db.execute("SELECT * FROM incoming WHERE turn=? ORDER BY rowid",(turn["id"],)).fetchall()
        with patch.object(worker,"media",side_effect=lambda key,_:clips[key]),patch.object(providers,"request",side_effect=listen) as net,patch.object(providers,"recognize_pcm") as fallback:
            parts,words,_=worker.input_parts(rows,cfg)
            self.assertEqual(worker.input_parts(rows,cfg)[0],parts)
        self.assertEqual(net.call_count,2)
        fallback.assert_not_called()
        self.assertEqual(words,["我回来了。","没事。"])
        self.assertEqual(parts[0]["text"],"〔语音转写〕\n我回来了。〔开心〕")
        self.assertEqual(parts[1]["text"],"〔语音转写〕\n没事。")
        self.assertEqual(self.deliveries,[])

    def test_ear_error_falls_back_to_words_without_an_emotion(self):
        ident=providers.save({"name":"耳朵","kind":"compatible","base":"https://example.org/v1","manual":["ear"]})["id"]
        cfg=st.DEFAULT|{"tone_service":ident,"tone_model":"ear","tone_format":"audio_url"}
        with patch.object(providers,"request",side_effect=TimeoutError()) as net,patch.object(providers,"recognize_pcm",return_value="嗯。") as fallback:
            result=providers.hear_utterance(cfg,bytes(640))
        self.assertEqual(result,("嗯。","","讯飞听写","fallback"))
        audio=net.call_args.args[2]["messages"][-1]["content"][0]
        self.assertTrue(audio["audio_url"]["url"].startswith("data:audio/wav;base64,"))
        fallback.assert_called_once_with(cfg,bytes(640))

    def test_compatible_speech_must_provide_lossless_audio(self):
        selected = {"kind":"compatible", "id":"demo"}
        cfg = st.DEFAULT | {"tts_model":"demo","voice":"example"}
        raw_24k = b"\x32\x01" * 2400
        with patch.object(providers, "service", return_value=selected):
            with patch.object(providers, "request", return_value=pcm_audio.as_wav(raw_24k, 24000)):
                self.assertEqual(len(providers.synthesize_pcm(cfg,"测试")), 3200)
            packet_wav = bytearray(pcm_audio.as_wav(raw_24k))
            packet_wav[20:22] = (7).to_bytes(2, "little")  # mu-law in a WAV container
            for payload in (b"ID3"+bytes(64), bytes(packet_wav)):
                with self.subTest(prefix=payload[:4]), patch.object(providers, "request", return_value=payload):
                    with self.assertRaisesRegex(ValueError, "PCM WAV"):
                        providers.synthesize(cfg,"测试")

    def test_mislabeled_provider_output_and_old_staged_mp3_cannot_be_sent(self):
        with patch.object(ds.HTTP,"open") as http:
            response=http.return_value.__enter__.return_value
            response.headers.get_content_type.return_value="audio/mpeg"
            with self.assertRaisesRegex(ValueError, "PCM"):
                ds.service_request("https://example.org/speech",{},binary=True,pcm_response=True)
            response.headers.get_content_type.return_value="application/octet-stream"
            response.read.return_value=b"ID3"+bytes(100)
            with self.assertRaisesRegex(ValueError, "PCM"):
                ds.service_request("https://example.org/speech",{},binary=True,pcm_response=True)
        self.bind()
        self.incoming("voice","/语音 听见了")
        turn=self.turns()[0]
        (st.ROOT/(turn["id"]+".wav")).write_bytes(b"ID3"+bytes(64))
        with patch.object(worker, "cli") as send, self.assertRaisesRegex(ValueError, "PCM WAV"):
            worker.process(turn)
        send.assert_not_called()

    def test_untyped_mpeg_frames_and_negative_pcm_samples_are_distinguished(self):
        mp3 = subprocess.check_output(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=430:duration=0.4",
             "-c:a", "libmp3lame", "-id3v2_version", "0", "-write_xing", "0", "-f", "mp3", "pipe:1"]
        )
        self.assertEqual(mp3[0], 255)
        with self.assertRaisesRegex(ValueError, "压缩音频"):
            pcm_audio.provider_pcm(mp3)
        raw = b"\xff\xff" * 320
        self.assertEqual(pcm_audio.provider_pcm(raw), raw)
        with self.assertRaisesRegex(ValueError, "不完整"):
            pcm_audio.samples(pcm_audio.as_wav(raw)[:-1])


if __name__ == "__main__":
    unittest.main()
