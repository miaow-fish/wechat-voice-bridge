"""Keep spoken words, delivery instructions and WeChat text separate."""
from dataclasses import dataclass
import re


DIRECTIONS = {"laughs", "sighs", "whispers", "curious", "mischievously"}
TAG_MODELS = {"eleven_v3", "eleven_v3_conversational", "eleven_v4", "eleven_v4_turbo"}
BRACKETS = re.compile(r"\[([^\[\]\n]{1,80})\]")
VOICE_INSTRUCTION = (
    "用语音条时，台词可自然带嗯、欸、啊。需要改变说法时，可在相应的字前加 "
    "[laughs]、[sighs]、[whispers]、[curious] 或 [mischievously]，一条最多两处。"
)


@dataclass(frozen=True)
class SpokenReply:
    words: str
    delivery: str


def parse(text):
    words, delivery = [], []
    offset = count = 0
    for match in BRACKETS.finditer(text):
        before = text[offset:match.start()]
        words.append(before)
        delivery.append(before)
        name = match[1].strip()
        if name.lower() in DIRECTIONS:
            if count < 2:
                delivery.append("[" + name.lower() + "]")
                count += 1
        elif not re.fullmatch(r"[a-z][a-z -]+", name):
            # Ordinary bracketed text is spoken literally, never forwarded as a TTS command.
            words.append(name)
            delivery.append(name)
        offset = match.end()
    words.append(text[offset:])
    delivery.append(text[offset:])
    plain = "".join(words).strip()
    styled = "".join(delivery).strip()
    if not plain:
        raise ValueError("语音回复缺少要说的话")
    return SpokenReply(plain, styled)
