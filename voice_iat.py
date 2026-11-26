"""iFlytek's documented short dictation WebSocket, using this provider's credentials."""
import asyncio
import base64
from email.utils import formatdate
import hmac
import json
from urllib.parse import urlencode


def signed_url(credentials):
    host, path, date = "iat-api.xfyun.cn", "/v2/iat", formatdate(usegmt=True)
    signed = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1".encode()
    sig = base64.b64encode(hmac.digest(credentials["secret"].encode(),signed,"sha256")).decode()
    auth = f'api_key="{credentials["key"]}", algorithm="hmac-sha256", headers="host date request-line", signature="{sig}"'
    return "wss://"+host+path+"?"+urlencode({"host":host,"date":date,"authorization":base64.b64encode(auth.encode()).decode()})


async def recognize_pcm(pcm, credentials, language="zh_cn", accent="mandarin"):
    if not 0 < len(pcm) <= 60*32000 or len(pcm)%2:
        raise ValueError("讯飞短语音听写支持 60 秒以内的语音")
    async def chunks():
        for pos in range(0,len(pcm),1280):
            yield pcm[pos:pos+1280]
            await asyncio.sleep(.04)
    return await recognize_stream(chunks(),credentials,language,accent)


async def recognize_stream(chunks, credentials, language, accent, *, on_final=None):
    from websockets.asyncio.client import connect
    async with asyncio.timeout(85), connect(signed_url(credentials),open_timeout=8,proxy=None) as socket:
        async def send_audio():
            first=True
            async for chunk in chunks:
                sample = {"data":{"status":0 if first else 1,"format":"audio/L16;rate=16000","encoding":"raw",
                                  "audio":base64.b64encode(chunk).decode()}}
                if first:
                    business={"language":language,"domain":"iat"}
                    if language=="zh_cn":
                        business["accent"]=accent
                    sample.update(common={"app_id":credentials["app_id"]}, business=business)
                    first=False
                await socket.send(json.dumps(sample))
            await socket.send(json.dumps({"data":{"status":2,"format":"audio/L16;rate=16000","encoding":"raw","audio":""}}))
        writer = asyncio.create_task(send_audio())
        words = {}
        try:
            async for raw in socket:
                response = json.loads(raw)
                if response.get("code") != 0:
                    raise ValueError("讯飞听写错误码 "+str(response.get("code")))
                data = response.get("data") or {}
                result = data.get("result") or {}
                if result.get("pgs") == "rpl":
                    low,high = result["rg"]
                    words = {k:v for k,v in words.items() if not low<=k<=high}
                if result.get("ws"):
                    words[int(result.get("sn",len(words)))] = "".join((w.get("cw") or [{}])[0].get("w","") for w in result["ws"])
                if data.get("status")==2:
                    text = "".join(words[k] for k in sorted(words)).strip()
                    if not text:
                        raise ValueError("讯飞没有识别到语音")
                    if on_final is not None:
                        on_final(text)
                    return text
        finally:
            writer.cancel()
            await asyncio.gather(writer,return_exceptions=True)
    raise ValueError("讯飞听写连接提前结束")


class LiveDictation:
    def __init__(self,cfg):
        self.frames=asyncio.Queue()
        self.ready = asyncio.Event()
        self.text = None
        self.task = asyncio.create_task(recognize_stream(
            self.chunks(), cfg["_call_iat"], cfg["asr_language"], cfg["asr_accent"], on_final=self._final))
        self.task.add_done_callback(self._closed)

    def _final(self, text):
        # The final transcript is usable while the WebSocket still closes gracefully.
        self.text = text
        self.ready.set()

    def _closed(self, task):
        if not task.cancelled():
            task.exception()  # Also consume errors for an utterance abandoned when a call ends.
        self.ready.set()

    async def transcript(self):
        await self.ready.wait()
        if self.text is not None:
            return self.text
        return await self.task

    async def chunks(self):
        buf=bytearray()
        while True:
            frame=await self.frames.get()
            if frame is None:
                if buf: yield bytes(buf)
                return
            buf.extend(frame)
            while len(buf)>=1280:
                yield bytes(buf[:1280])
                del buf[:1280]
