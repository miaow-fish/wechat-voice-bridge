"""WeChat speech uses mono 16 kHz signed little-endian PCM between providers."""
import io
import subprocess
import wave


RATE = 16000
BYTES_PER_SECOND = RATE * 2
WAV_CODECS = "pcm_u8,pcm_s16le,pcm_s24le,pcm_s32le,pcm_s64le,pcm_f32le,pcm_f64le"


def checked(samples, max_seconds=300, rate=RATE):
    if not samples or len(samples) % 2 or len(samples) > 2 * rate * max_seconds:
        raise ValueError("PCM 音频为空、不完整或时长超限")
    return samples


def provider_pcm(data, rate=RATE):
    if data.startswith((b"ID3", b"OggS", b"RIFF", b"fLaC")):
        raise ValueError("语音服务在原始 PCM 接口返回了封装音频")
    # MP3 and ADTS may begin with a frame sync instead of a file header.
    # A signed PCM sample can also start this way, so confirm with a demuxer.
    if len(data) >= 4 and data[0] == 255 and data[1] & 0xe0 == 0xe0:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "pipe", "-format_whitelist", "mp3,aac",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", "pipe:0"],
            input=data[:65536], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            raise ValueError("语音服务在原始 PCM 接口返回了压缩音频")
    return checked(data, rate=rate)


def as_wav(samples, rate=RATE):
    if not samples or len(samples) % 2:
        raise ValueError("PCM 音频为空或不完整")
    result = io.BytesIO()
    with wave.open(result, "wb") as target:
        target.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        target.writeframes(samples)
    return result.getvalue()


def samples(data, *, from_wechat=False):
    """Read a decoded voice note or a TTS WAV. Only the receive boundary accepts encoded media."""
    max_seconds = 120 if from_wechat else 300
    is_wav = data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if not is_wav and not from_wechat:
        raise ValueError("语音合成服务须返回未压缩的 PCM WAV")
    if is_wav:
        try:
            with wave.open(io.BytesIO(data)) as source:
                if (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (RATE, 1, 2):
                    declared = source.getnframes()
                    limit = RATE * max_seconds
                    # Streaming WAV may declare an unknown length (0xffffffff).
                    # Let the bounded reader below determine its actual size.
                    if declared <= limit or from_wechat and declared * 2 <= len(data):
                        count = min(declared, limit)
                        raw = source.readframes(count + int(count == declared))
                        if len(raw) != count * 2:
                            raise ValueError("WAV 音频的采样数据不完整")
                        return checked(raw, max_seconds)
        except (wave.Error, EOFError):
            pass  # FFmpeg supports additional PCM WAV types such as float.

    formats = (["-format_whitelist", "wav,mp3,flac,ogg,aac,mov,matroska,webm,caf,amr"]
               if from_wechat else ["-f", "wav", "-codec_whitelist", WAV_CODECS])
    duration = max_seconds if from_wechat else max_seconds + 1
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "pipe", *formats,
         "-i", "pipe:0", "-map", "0:a:0", "-t", str(duration), "-ac", "1", "-ar", str(RATE),
         "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=25,
    )
    if result.returncode:
        raise ValueError("微信语音解码失败" if from_wechat else "语音合成服务须返回可读取的未压缩 PCM WAV")
    return checked(result.stdout, max_seconds)
