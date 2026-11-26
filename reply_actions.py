"""Parse the assistant's private WeChat action markers on whole lines."""
import re

VOICE = "[[voice]]"
STICKER = "[[sticker]]"
CALL = "[[call]]"
SILENT = "[[silent]]"
VOICE_LABEL = "〔微信语音条〕"
STICKER_LABEL = "〔微信表情包〕"
# These look like media in a transcript. Only the exact action lines above execute.
MEDIA_PREFIX = re.compile(r"^[\[［【〔]{1,2}\s*(?:(?:微信)?(?:语音(?:条|消息)?|表情(?:包)?|贴纸)|voice|sticker)\s*[\]］】〕]{1,2}",re.I)


class MediaFormatError(ValueError):
    """The model used a display label or put an action and its argument on one line."""


def message_lines(text):
    fence = None
    for line in text.splitlines():
        value = line.lstrip()
        if fence:
            if value.startswith(fence):
                fence = None
            yield line, False
        elif value.startswith(("```", "~~~")):
            fence = value[:3]
            yield line, False
        else:
            yield line, True


def without_display_labels(text):
    """Recover the words from older text receipts while preserving quoted examples."""
    cleaned = []
    changed = False
    for line, executable in message_lines(text):
        value = line.lstrip()
        marker = MEDIA_PREFIX.match(value) if executable else None
        if marker:
            changed = True
            while marker:
                value = value[marker.end():].lstrip()
                marker = MEDIA_PREFIX.match(value)
            if not value:
                continue
            line = value
        cleaned.append(line)
    return ("\n".join(cleaned), True) if changed else (text, False)

# Skip punctuation inside links, inline code and quotations. Newlines in
# formatted material belong to the example or table and stay together.
STRUCTURED = re.compile(r"(?m)^\s*(?:`{3}|~{3}|\||>|[-*+]\s|\d+[.)、]\s)")
BOUNDARIES = re.compile(
    r"https?://[^\s<>。！？，]+|`[^`\n]*`|“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|"
    r'"[^"\n]*"|(?P<end>[。！？!?]+|\.(?=\s+[A-Z]))'
)


def text_bubbles(text):
    text=text.strip()
    if STRUCTURED.search(text):
        return [text]
    bubbles=[]
    for raw in text.splitlines():
        line=raw.strip()
        if not line:
            continue
        if len(line)<=24:
            bubbles.append(line)
            continue
        start=0
        for match in BOUNDARIES.finditer(line):
            if match.lastgroup=="end":
                bubbles.append(line[start:match.end()].strip())
                start=match.end()
        if line[start:].strip():
            bubbles.append(line[start:].strip())
    # Keep all the words of unusually long answers without flooding the chat.
    return bubbles[:5]+["\n".join(bubbles[5:])] if len(bubbles)>6 else bubbles


def check_media_format(text, *, split=False):
    for bubble in text_bubbles(text) if split else (text,):
        for line, executable in message_lines(bubble):
            value = line.strip()
            if not executable or value in (VOICE,STICKER,CALL,SILENT):
                continue
            if MEDIA_PREFIX.match(value) or value.startswith((CALL,SILENT)):
                raise MediaFormatError("媒体格式不正确，未发送这条回复")


def parse(text):
    if text.strip()==CALL:
        return [("call","")]
    chunks=[]
    plain=[]
    queued_voice=False
    seen=False

    def flush():
        value="\n".join(plain).strip()
        if value: chunks.append(("text",value))
        plain.clear()

    for line, executable in message_lines(text):
        value=line.strip()
        if not executable:
            plain.append(line)
            continue
        if value==SILENT:
            raise ValueError("旁听代号应单独作为整条群聊回复")
        if value in (VOICE,STICKER,CALL):
            seen=True
            flush()
            if queued_voice:
                chunks.append(("voice",""))
            if value==CALL:
                raise ValueError("拨号代号应单独作为整条回复")
            queued_voice=value==VOICE
            if not queued_voice:
                chunks.append(("sticker",""))
        elif queued_voice and value:
            chunks.append(("voice",value))
            queued_voice=False
        else:
            plain.append(line)
    if not seen: return None
    if queued_voice: chunks.append(("voice",""))
    flush()
    return chunks
