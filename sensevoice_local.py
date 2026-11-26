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
            if not pcm: return "","uncertain"
        conn.sendall(pcm)
        conn.shutdown(socket.SHUT_WR)
        with conn.makefile("rb") as response:
            data=response.readline(128)
        tone=json.loads(data)["tone"]
        if tone and tone not in LABELS.values():
            raise ValueError("未知语气标签")
        return tone,"tagged" if tone else "uncertain"


def start(pcm):
    future=Future()
    if not SLOT.acquire(blocking=False):
        future.set_result(("","busy"))
        return future

    def run():
        try:
            future.set_result(local_cue(pcm))
        except (OSError,ValueError,KeyError,TypeError):
            future.set_result(("","unavailable"))
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
        return ""
    tones=set()
    # Bound attention memory and avoid attributing a mixed recording to one mood.
    pieces=(len(samples)+8*RATE-1)//(8*RATE)
    for piece in np.array_split(samples,pieces):
        s=model.create_stream()
        s.accept_waveform(RATE,piece)
        model.decode_stream(s)
        result=s.result
        if result.text.strip():
            tones.add(LABELS.get(result.emotion,""))
        if "" in tones or len(tones)>1:
            return ""
    return next(iter(tones),"")


if __name__=="__main__":
    # For live calls the connection opens on speech onset. Load weights while
    # the person is still speaking, then read only that utterance's samples.
    model=load_model()
    raw=sys.stdin.buffer.read(LIMIT+1)
    if not raw or len(raw)>LIMIT or len(raw)%2:
        raise SystemExit(2)
    print(json.dumps({"tone":classify(raw,model)},ensure_ascii=False),flush=True)
