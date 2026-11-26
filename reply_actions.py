"""Parse the assistant's private WeChat action markers on whole lines."""
import re

VOICE = "[[voice]]"
STICKER = "[[sticker]]"
CALL = "[[call]]"
SILENT = "[[silent]]"
MEDIA_RULES = (
    f"平常直接写微信聊天内容。需要语音时单独写一行 {VOICE}，下一行写你自己想说的话，"
    "后台只把那一行合成为原生语音条。自行接着上文说，其他行照常发文字。"
    f"需要表情时单独写一行 {STICKER}，后台按当前话题从这个微信号下载的表情里选一张。"
)
RULES = MEDIA_RULES + (
    f"对方邀请你打来时，整条只写 {CALL}，后台拨给当前私聊。"
    "标记是内部动作。普通提问、引用和举例按聊天内容理解，介绍写法时使用行内代码。"
)
GROUP_RULES = MEDIA_RULES + (
    "你是群里的一员。留意谁在和谁说话。别人互相聊天时可以旁听；有人叫你或你有话想接，就自然回应。"
    f"选择旁听时，整条只写 {SILENT}，微信里不会出现任何气泡。当前会话只支持群内回复。"
)

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
