"""DeepSeek text, images and native-voice source audio for the Linux test instance."""
import argparse
import base64
import getpass
import io
import json
import os
from pathlib import Path
import secrets
import struct
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request
import wave
import zlib

import pcm_audio


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = build_opener(ProxyHandler({}), NoRedirect())
BASE = "https://api.deepseek.com"
KEY = Path(os.environ.get("WECHAT_CASE_KEY_FILE","/var/lib/wechat-vps-panel/deepseek.key"))
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")


def key():
    return KEY.read_text().strip()


def service_request(url, headers, payload=None, *, mime="application/json", binary=False, pcm_response=False, pcm_rate=16000, timeout=60):
    if isinstance(payload,(dict,list)):
        payload=json.dumps(payload,ensure_ascii=False).encode()
    req=Request(url,data=payload,headers={**headers,"Content-Type":mime})
    with HTTP.open(req,timeout=timeout) as response:
        if pcm_response and response.headers.get_content_type() not in ("audio/pcm", "audio/x-pcm", "audio/raw", "application/octet-stream"):
            raise ValueError("语音服务没有按请求返回原始 PCM")
        data=response.read(12*1024*1024+1)
    if len(data)>12*1024*1024:
        raise ValueError("接口返回的数据过大")
    if pcm_response:
        return pcm_audio.provider_pcm(data, rate=pcm_rate)
    return data if binary else json.loads(data)


def request(path, token, payload=None, **kwargs):
    return service_request(BASE+path,{"Authorization":"Bearer "+token},payload,**kwargs)


def chat(messages, token=None, *, model=None):
    reply=request("/chat/completions",token or key(),{"model":model or MODEL,"messages":messages,
                  "thinking":{"type":"disabled"},"max_tokens":768,"stream":False})
    choice=reply["choices"][0]
    text=choice["message"]["content"]
    if choice.get("finish_reason")!="stop" or not isinstance(text,str) or not text.strip():
        raise ValueError("Incomplete model reply")
    return text.strip()


def image_part(content, mime):
    if mime not in ("image/gif","image/png","image/jpeg","image/webp") or not 0<len(content)<=8*1024*1024:
        raise ValueError("Unreadable WeChat image")
    url="data:"+mime+";base64,"+base64.b64encode(content).decode("ascii")
    return {"type":"image_url","image_url":{"url":url}}


def speak(text, token=None, *, voice="tide"):
    data=request("/v1/tools/tts",token or key(),{"text":text,"format":"wav","voice_id":voice},binary=True)
    with wave.open(io.BytesIO(data)) as wav:
        if wav.getsampwidth()!=2 or wav.getnchannels() not in (1,2) or not wav.readframes(1):
            raise ValueError("Unreadable speech audio")
    return data


def transcribe(wav, token=None):
    if not 0<len(wav)<=8*1024*1024:
        raise ValueError("Audio is outside the speech recognition input range")
    boundary=secrets.token_hex(24)
    head=(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
          'Content-Type: audio/wav\r\n\r\n').encode()
    body=head+wav+(f"\r\n--{boundary}--\r\n").encode()
    result=request("/v1/tools/asr",token or key(),body,mime="multipart/form-data; boundary="+boundary)
    transcript=result.get("text")
    if not isinstance(transcript,str) or not transcript.strip():
        raise ValueError("Empty speech transcript")
    return transcript.strip()


def chart():
    def chunk(kind,data):
        return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data)&0xffffffff)
    row=b"\x00"+b"\xf0\x1e\x22"*64+b"\x20\x37\xea"*64
    return (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",128,64,8,2,0,0,0))+
            chunk(b"IDAT",zlib.compress(row*64))+chunk(b"IEND",b""))


def save_key(secret):
    secret = secret.strip()
    if not secret or any(c.isspace() for c in secret) or not secret.isascii():
        raise ValueError("Invalid API key format")
    models=request("/models",secret)
    ids={m["id"] for m in models["data"] if isinstance(m.get("id"),str)}
    if not ids:
        raise ValueError("这个 Key 没有返回可用模型")
    KEY.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with NamedTemporaryFile(dir=KEY.parent,mode="w",delete=False) as f:
        tmp=Path(f.name)
        try:
            f.write(secret+"\n")
            f.flush()
            os.fsync(f.fileno())
            os.replace(tmp,KEY)
        finally:
            tmp.unlink(missing_ok=True)
    return sorted(ids)


def configure():
    if not os.isatty(0):
        raise ValueError("Open configure in an interactive terminal")
    save_key(getpass.getpass("DeepSeek API Key（输入隐藏）: "))
    print("Key 已在 VPS 保存，模型列表已验证。")


def probe():
    token=key()
    result=chat([{"role":"user","content":"只返回这四个字：连接成功"}],token)
    if "连接成功" not in result:
        raise ValueError("Unexpected text probe response")
    print("文字接口通过",flush=True)
    result=chat([{"role":"user","content":[{"type":"text","text":"依次说出图片左半和右半的颜色"},
                                            image_part(chart(),"image/png")]}],token)
    if not 0<=result.find("红")<result.find("蓝"):
        raise ValueError("Unexpected ordering in synthetic image probe")
    print("合成测试图识别通过",flush=True)
    wav=speak("今天星期三，天气很好。",token)
    words=transcribe(wav,token)
    if "天气" not in words or not any(w in words for w in ("星期三","周三","礼拜三")):
        raise ValueError("Synthetic speech did not match the test phrase")
    print("语音合成及转写通过",flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("configure","probe"))
    args=parser.parse_args()
    try:
        configure() if args.action=="configure" else probe()
    except HTTPError as exc:
        raise SystemExit(f"DeepSeek HTTP {exc.code}") from None
    except (OSError,ValueError,KeyError,wave.Error) as exc:
        raise SystemExit(str(exc)) from None
