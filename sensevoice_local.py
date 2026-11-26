"""On-demand waveform emotion cues for one utterance. No recording is retained."""
from concurrent.futures import Future
import json
import os
from pathlib import Path
import socket
import sys
from threading import BoundedSemaphore, Thread


ROUTE = "local-sensevoice"
SOCKET = Path("/run/wechat-vps-sensevoice.sock")
MODEL_DIR = Path("/opt/wechat-vps-case/sensevoice")
RATE = 16000
LIMIT = RATE * 2 * 120
SLOT = BoundedSemaphore(1)
LABELS = {"<|HAPPY|>":"开心", "<|SAD|>":"难过", "<|ANGRY|>":"生气",
          "<|FEARFUL|>":"害怕", "<|DISGUSTED|>":"厌恶", "<|SURPRISED|>":"惊讶",
          "<|NEUTRAL|>":"平静"}


def installed():
    return SOCKET.is_socket() and os.access(SOCKET,os.W_OK)


def local_cue(pcm):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
        conn.settimeout(15)
        conn.connect(str(SOCKET))
        if isinstance(pcm,Future):
            pcm=pcm.result(timeout=20)
            if not pcm: return "","uncertain",""
        conn.sendall(pcm)
        conn.shutdown(socket.SHUT_WR)
        with conn.makefile("rb") as response:
            data=json.loads(response.readline(4096))
        tone=data["tone"]
        if tone and tone not in LABELS.values():
            raise ValueError("未知语气标签")
        words=data.get("words","")
        return tone,"tagged" if tone else "uncertain",words if isinstance(words,str) else ""


def start(pcm, *, with_words=False):
    future=Future()
    def finish(tone,status,words=""):
        future.set_result((tone,status,words) if with_words else (tone,status))
    if not SLOT.acquire(blocking=False):
        finish("","busy")
        return future

    def run():
        try:
            finish(*local_cue(pcm))
        except (OSError,ValueError,KeyError,TypeError):
            finish("","unavailable")
        finally:
            SLOT.release()
    Thread(target=run,daemon=True).start()
    return future


def load_model():
    import sherpa_onnx
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(MODEL_DIR/"model.int8.onnx"),tokens=str(MODEL_DIR/"tokens.txt"),
        num_threads=1,language="auto",
    )


def classify(pcm,model):
    import numpy as np

    samples=np.frombuffer(pcm,dtype="<i2").astype(np.float32)/32768
    if not np.any(samples):
        return {"tone":"","words":""}
    tones=set()
    words=""
    # Bound attention memory and avoid attributing a mixed recording to one mood.
    pieces=(len(samples)+8*RATE-1)//(8*RATE)
    for piece in np.array_split(samples,pieces):
        s=model.create_stream()
        s.accept_waveform(RATE,piece)
        model.decode_stream(s)
        result=s.result
        if result.text.strip():
            tones.add(LABELS.get(result.emotion,""))
            if pieces==1:
                words=result.text.strip()[:256]
        if "" in tones or len(tones)>1:
            break
    tone=next(iter(tones)) if len(tones)==1 else ""
    return {"tone":tone,"words":words}


if __name__=="__main__":
    # For live calls the connection opens on speech onset. Load weights while
    # the person is still speaking, then read only that utterance's samples.
    model=load_model()
    raw=sys.stdin.buffer.read(LIMIT+1)
    if not raw or len(raw)>LIMIT or len(raw)%2:
        raise SystemExit(2)
    print(json.dumps(classify(raw,model),ensure_ascii=False),flush=True)
