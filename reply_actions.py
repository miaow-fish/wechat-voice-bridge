"""Parse the assistant's private WeChat action markers on whole lines."""
import re

VOICE = "[[voice]]"
STICKER = "[[sticker]]"
CALL = "[[call]]"
SILENT = "[[silent]]"

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


def parse(text):
    if text.strip()==CALL:
        return [("call","")]
    chunks=[]
    plain=[]
    queued_voice=False
    fenced=False
    seen=False

    def flush():
        value="\n".join(plain).strip()
        if value: chunks.append(("text",value))
        plain.clear()

    for line in text.splitlines():
        value=line.strip()
        if value.startswith("```") or value.startswith("~~~"):
            fenced=not fenced
            plain.append(line)
            continue
        if not fenced and value==SILENT:
            raise ValueError("旁听代号应单独作为整条群聊回复")
        if not fenced and value in (VOICE,STICKER,CALL):
            seen=True
            flush()
            if queued_voice:
                chunks.append(("voice",""))
            if value==CALL:
                raise ValueError("拨号代号应单独作为整条回复")
            queued_voice=value==VOICE
            if not queued_voice:
                chunks.append(("sticker",""))
        elif queued_voice and value and not fenced:
            chunks.append(("voice",value))
            queued_voice=False
        else:
            plain.append(line)
    if not seen: return None
    if queued_voice: chunks.append(("voice",""))
    flush()
    return chunks
