"""Loopback-only operator UI for a single Linux WeChat test instance."""
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
from threading import Lock, Thread
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import channel_state as st
import channel_worker
import deepseek_case as ds
import providers
import sensevoice_local
import account_stickers
import call_channel
import group_chats
import worldbooks
from wechat_case import esc, wx

FILES = Path(__file__).resolve().parent
LOGIN_LOCK = Lock()
TERMINAL = {"succeeded", "failed", "expired", "rejected", "cancelled", "needs_login"}


def login_status(db):
    ident = st.get(db, "login")
    if not ident:
        return {}
    try:
        operation = wx("/login-attempts/" + esc(ident))
    except HTTPError as exc:
        if exc.code != 404:
            raise
        return {"state": "expired"}
    result = operation.get("result") or {}
    return {"state": operation.get("state"), "generation": result.get("qrGeneration") if result.get("qrAvailable") else None}


def begin_login():
    with LOGIN_LOCK, st.database() as db:
        if wx().get("authenticated"):
            return {"message": "微信已经登录"}
        prior = login_status(db)
        if not prior or prior["state"] in TERMINAL:
            if prior:
                st.put(db,"login",None)
                st.put(db,"login_key",None)
            admission = st.get(db,"login_key") or "login-" + secrets.token_hex(16)
            st.put(db,"login_key",admission)
            db.commit()
            result = wx("/login-attempts", {"strategy":"qr", "allowQr":True, "timeoutSeconds":300,
                        "qrRefresh":{"mode":"auto","maxRefreshes":3}}, admission)
            if not result.get("id"):
                raise ValueError("微信没有返回本次登录编号")
            st.put(db, "login", result["id"])
        return {"message": "二维码准备中，请在手机微信里扫码并确认"}


def pairing():
    if not wx().get("authenticated"):
        raise ValueError("请先用微信扫码登录")
    with st.database() as db:
        db.execute("BEGIN IMMEDIATE")
        if st.get(db, "peer"):
            raise ValueError("这个实例已经绑定私聊")
        old=st.get(db,"pair",{})
        if old.get("code") and old.get("expires",0)>time.time():
            code=old["code"]
        else:
            code = "微信配对 " + secrets.token_hex(4).upper()
            st.put(db, "pair", {"code":code, "hash":sha256(code.encode()).hexdigest(), "expires":time.time()+600})
    return {"code": code, "message": "从你自己的微信私聊发给刚登录的助手号，十分钟内有效"}


def validate_memory(url):
    dest = urlsplit(url)
    if url and (len(url)>1000 or dest.scheme not in ("http","https") or not dest.hostname or
                dest.username is not None or dest.password is not None or dest.query or dest.fragment):
        raise ValueError("请填写不含账号、密码、参数的 HTTP(S) 服务地址")


def save_asset(data):
    kind=data.get("kind")
    name=str(data.get("name","")).strip()
    body=str(data.get("body","")).strip()
    if kind not in ("persona","worldbook","memory") or not 0<len(name)<=80 or not body:
        raise ValueError("请填写名称及内容")
    fmt=data.get("format","text")
    entries=worldbooks.parse(body,fmt) if kind=="worldbook" else None
    if kind!="worldbook" and len(body)>16000: raise ValueError("内容超过 16,000 字符")
    if kind=="memory":
        validate_memory(body)
    with st.database() as db:
        db.execute("BEGIN IMMEDIATE")
        library=st.get(db,"library",{})
        ident=data.get("id")
        if ident and (ident not in library or library[ident]["kind"]!=kind):
            raise ValueError("资料不存在")
        ident=ident or secrets.token_hex(8)
        item={"id":ident,"kind":kind,"name":name,"body":body}
        if entries is not None:
            st.put(db,"worldbook-source:"+ident,body)
            st.put(db,"worldbook-entries:"+ident,entries)
            item.update(body="",format=fmt,count=len(entries),enabled=sum(e["enabled"] for e in entries))
        library[ident]=item
        st.put(db,"library",library)
        if data.get("select") is True:
            profile=st.get(db,"profile",{})
            profile.update({kind+"_id":ident, "memory_url" if kind=="memory" else kind:item["body"]})
            st.put(db,"profile",profile)
    return {"id":ident,"message":"资料已保存，可在聊天设置中选用"}


def save_profile(data):
    if any(key not in st.DEFAULT or type(value) is not type(st.DEFAULT[key]) for key, value in data.items()):
        raise ValueError("设置字段或格式无效")
    with st.database() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = st.profile(db)
        cfg = {**previous, **data}
        if not 0 <= cfg["history_turns"] <= st.MAX_HISTORY_TURNS:
            raise ValueError("最近对话轮数须在 0 到 %d 之间"%st.MAX_HISTORY_TURNS)
        if max(len(cfg["pinned_context"]),len(cfg["private_context"]))>16000:
            raise ValueError("每段提示词最多 16,000 字符")
        if not 0 < cfg["merge_wait_seconds"] < 2**53:
            raise ValueError("等待时长须为有效的正整数秒")
        library=st.get(db,"library",{})
        if "memory_url" in data and "memory_id" not in data: cfg["memory_id"]=""
        for kind,field in (("persona","persona"),("worldbook","worldbook"),("memory","memory_url")):
            ref=cfg[kind+"_id"]
            if ref and (ref not in library or library[ref]["kind"]!=kind): raise ValueError("所选资料不存在")
            if kind+"_id" in data: cfg[field]=library[ref]["body"] if ref else data.get(field,"")
        if max(len(cfg[k]) for k in ("persona","worldbook"))>16000: raise ValueError("较长的世界书请通过文件导入")
        if not 0<len(cfg["name"].strip())<=80: raise ValueError("请填写配置名称，最多 80 字符")
        providers.check_routes(cfg)
        if (len(cfg["voice"])>220 or any(ord(c)<32 for c in cfg["voice"])
                or cfg["asr_language"] not in ("zh_cn","en_us") or cfg["note_rate"] not in ("16000","24000")
                or cfg["call_cue"] not in ("off","on") or cfg["sticker_vision"] not in ("off","once")
                or cfg["tone_format"] not in ("input_audio","audio_url") or cfg["asr_accent"] not in ("mandarin","cantonese")):
            raise ValueError("请检查声音与表情设置")
        cfg["memory_url"]=cfg["memory_url"].strip()
        validate_memory(cfg["memory_url"])
        old_window, new_window = st.merge_window(previous), st.merge_window(cfg)
        if old_window != new_window:
            if new_window:
                now = time.time()
                # Move only live timers. An expired batch must not reopen when the wait increases.
                db.execute("""UPDATE turns SET next_try=next_try+?
                              WHERE status='collecting' AND next_try>?""", (new_window - old_window, now))
                db.execute("""UPDATE turns SET status='ready', next_try=0
                              WHERE status='collecting' AND next_try<=?""", (now,))
            else:
                db.execute("UPDATE turns SET status='ready', next_try=0 WHERE status IN ('collecting','sealed')")
        st.put(db,"profile",cfg)
    return {"message": "设置已保存，下一轮生效"}


def builtin_prompts():
    with st.database() as db:
        cfg = st.profile(db)
    sticker_note = "已开启首次识图。" if cfg["sticker_vision"] == "once" else "首次识图当前已关闭。"
    tone_service = cfg.get("tone_service", "off")
    tone_note = {sensevoice_local.ROUTE:"当前使用本机 SenseVoice。", "off":"当前已关闭语气识别。"}.get(
        tone_service, "当前已选用外部语气服务。"
    )
    scenes = [
        ("微信私聊", "与绑定好友聊天时使用。", channel_worker.chat_instructions(cfg, group=False)),
        ("微信群聊", "在已启用的群里判断是否接话时使用。", channel_worker.chat_instructions(cfg, group=True)),
        ("实时语音电话", "电话接通后使用。", call_channel.CALL_INSTRUCTIONS),
    ]
    tools = [
        ("私聊请求语音", "对方请助手发语音时使用。", channel_worker.voice_note(cfg,channel_worker.VOICE_REQUEST_NOTE)),
        ("语音初稿否认可用功能", "助手误说发不了语音时重新生成。", channel_worker.voice_note(cfg,channel_worker.VOICE_DENIAL_NOTE)),
        ("未受邀拨号", "模型输出拨号代号，当前私聊消息未通过回拨条件时，带这句重新生成。", channel_worker.CALL_RETRY_NOTE),
        ("补写语音台词", "选择了语音却漏写台词时使用。", channel_worker.voice_note(cfg,channel_worker.MISSING_VOICE_NOTE)),
        ("识别陌生表情", "表情名与已有识别结果都未命中时交给识图模型。" + sticker_note, account_stickers.DESCRIPTION_INSTRUCTIONS),
        ("按语境挑表情", "按名称匹配失败且允许按描述选择时，模型从当前账号的表情中挑选。", account_stickers.SELECTION_INSTRUCTIONS),
        ("外部模型听语气", "仅在使用外部音频模型分析语气时发送。" + tone_note, providers.TONE_INSTRUCTIONS),
    ]
    return [{"kind": kind, "title": title, "when": when, "text": text}
            for kind, entries in (("scene", scenes), ("tool", tools)) for title, when, text in entries]


def state():
    acct = wx()
    with st.database() as db:
        last = db.execute("SELECT status,kind,phase,error FROM turns ORDER BY rowid DESC LIMIT 1").fetchone()
        accepted = dict(db.execute("SELECT kind,count(*) FROM turns WHERE status='accepted' GROUP BY kind"))
        pair = st.get(db,"pair",{})
        speech=db.execute("""SELECT v.transcript,v.provider,t.tone,t.status AS tone_status FROM voice_inputs v JOIN incoming i ON i.id=v.message_id
                           LEFT JOIN voice_tones t ON t.message_id=v.message_id
                           WHERE i.peer=? ORDER BY i.rowid DESC LIMIT 1""",(st.get(db,"peer"),)).fetchone()
        call=call_channel.snapshot()
        if "states" not in call:
            call=st.get(db,"last_call",call)
        own = acct.get("ownUsername")
        return {"logged": bool(acct.get("authenticated")), "paired": bool(st.get(db, "peer")),
                "group_account":sha256(own.encode()).hexdigest()[:16] if own and acct.get("authenticated") else None,
                "paircode":pair.get("code") if acct.get("authenticated") and not st.get(db,"peer") and pair.get("expires",0)>time.time() else None,
                "key": public_key(db), "models":st.get(db, "models", []), "settings":st.profile(db),
                "group_routes":st.get(db,"group_routes",{}),
                "providers":providers.all_public(db),"library":list(st.get(db,"library",{}).values()),
                "stickers":st.get(db,"sticker_inventory",{}),"sticker_sync":st.get(db,"sticker_synced_at"),
                "last_sticker":next((row[0] for row in db.execute("SELECT body FROM incoming WHERE kind=47 AND peer=? ORDER BY stamp DESC LIMIT 1",(st.get(db,"peer"),))),None),
                "call":call,
                "local_tone_available":sensevoice_local.installed(),
                "speech":dict(speech) if speech else None,
                "login": {} if acct.get("authenticated") else login_status(db),
                "last": dict(last) if last else None, "accepted": accepted, "cursor_gap":st.get(db,"cursor_gap",False)}


def public_key(db):
    native=st.get(db,"services",{}).get("deepseek")
    return bool(native and providers.api_key(native))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, obj, mime="application/json; charset=utf-8"):
        data = obj if isinstance(obj,bytes) else json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Cross-Origin-Resource-Policy","same-origin")
        self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self, post=False):
        host = self.headers.get("Host","")
        if (not re.fullmatch(r"(127\.0\.0\.1|localhost)(:[0-9]{1,5})?",host) or
            self.headers.get("Sec-Fetch-Site") == "cross-site" or
            (post and self.headers.get("Origin") != "http://" + host)):
            return self.reply(403, {"error":"请从本机管理页操作"})
        path = urlsplit(self.path).path
        if not post:
            static = {"/":("index.html","text/html; charset=utf-8"), "/panel.css":("panel.css","text/css; charset=utf-8"),
                      "/panel.js":("panel.js","text/javascript; charset=utf-8")}
            if path in static:
                file, mime = static[path]
                return self.reply(200, (FILES/file).read_bytes(), mime)
            if path == "/api/state":
                return self.reply(200,state())
            if path == "/api/builtin-prompts":
                return self.reply(200,builtin_prompts())
            if path == "/api/groups":
                cursor=parse_qs(urlsplit(self.path).query).get("cursor",[""])[0]
                return self.reply(200,group_chats.list_page(cursor))
            if path == "/api/qr":
                with st.database() as db:
                    ident = st.get(db, "login")
                if not ident or wx().get("authenticated"):
                    return self.reply(404, {"error":"当前没有待扫二维码"})
                data = wx("/login-attempts/" + esc(ident) + "/qr",binary=True)
                if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("二维码尚未就绪")
                return self.reply(200,data,"image/png")
        else:
            size = int(self.headers.get("Content-Length","0"))
            limit=3*1024*1024 if path in ("/api/worldbooks/preview","/api/library") else 160000
            if self.headers.get_content_type() != "application/json" or not 0 < size <= limit:
                return self.reply(400,{"error":"请求格式无效"})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data,dict):
                raise ValueError("请求格式无效")
            if path == "/api/login":
                return self.reply(200, begin_login())
            if path == "/api/pair":
                return self.reply(200, pairing())
            if path == "/api/groups/refresh":
                return self.reply(200,group_chats.refresh_page())
            if path == "/api/groups":
                return self.reply(200,group_chats.configure(data.get("id"),data.get("mode")))
            if path == "/api/settings":
                return self.reply(200, save_profile(data))
            if path == "/api/providers":
                return self.reply(200,providers.save(data))
            if path == "/api/providers/sync":
                return self.reply(200,providers.sync(data.get("id")))
            if path == "/api/providers/voices":
                return self.reply(200,providers.voices(data.get("id")))
            if path == "/api/library":
                return self.reply(200,save_asset(data))
            if path == "/api/worldbooks/preview":
                return self.reply(200,worldbooks.preview(data.get("filename"),data.get("file")))
            if path == "/api/worldbooks/open":
                with st.database() as db:
                    entry=st.get(db,"library",{}).get(data.get("id"))
                    if not entry or entry["kind"]!="worldbook": raise ValueError("找不到世界书")
                    return self.reply(200,{"body":st.get(db,"worldbook-source:"+entry["id"],entry["body"])})
            if path == "/api/stickers/sync":
                groups=account_stickers.summary(account_stickers.synchronize(),index_pixels=True)
                return self.reply(200,{"message":"已从微信同步 %d 张表情"%sum(groups.values())})
            if path == "/api/call":
                if data.get("mode")=="stop":
                    return self.reply(200,call_channel.stop())
                return self.reply(200,call_channel.start(data.get("mode")))
            if path == "/api/key":
                key = data.get("key")
                if not isinstance(key,str) or len(key)>1024:
                    raise ValueError("Key 格式无效")
                try:
                    ids = ds.save_key(key)
                except HTTPError as exc:
                    return self.reply(400, {"error": "DeepSeek 官方接口校验失败（HTTP %d）" % exc.code})
                with st.database() as db:
                    st.put(db, "models",ids)
                return self.reply(200,{"message":"DeepSeek 已验证，密钥已保存"})
        self.reply(404,{"error":"没有这个页面"})

    def handle_request(self, post):
        try:
            self.dispatch(post)
        except HTTPError as exc:
            who="微信" if exc.url.startswith("http://127.0.0.1:28220/") else "服务商"
            self.reply(502,{"error":who+"接口返回 HTTP %d" % exc.code})
        except (TimeoutError, URLError):
            self.reply(503,{"error":"上游连接超时或暂时不可用"})
        except (UnicodeError, ValueError) as exc:
            self.reply(400,{"error": str(exc)[:160]})
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            print("operator request failed:",type(exc).__name__, flush=True)
            self.reply(500,{"error":"本机服务遇到错误"})

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)


if __name__ == "__main__":
    st.init()
    providers.initialize()
    for run in (channel_worker.collect, channel_worker.deliver):
        Thread(target=run,daemon=True).start()
    Thread(target=channel_worker.deliver,args=("group",),daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1",28222),Handler).serve_forever()
