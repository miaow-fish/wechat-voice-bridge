"""Build the model input shared by a WeChat conversation and its phone calls."""
from datetime import datetime
from zoneinfo import ZoneInfo

import worldbooks


BEIJING = ZoneInfo("Asia/Shanghai")
CLOCK_HEADER = "\n\n〔当前北京时间〕"
NOTE_HEADER = "\n\n〔通道附注〕\n"


def build(cfg, instructions, history, current, notes=(), *, now=None, private=False):
    words = current if isinstance(current, str) else "\n".join(
        part["text"] for part in current if part["type"] == "text"
    )
    # Text bubbles use the same newline-separated representation as delivered history.
    if not isinstance(current, str) and all(part["type"] == "text" for part in current):
        current = words
    fixed, topical = worldbooks.sections(cfg, history, words)
    settings = []
    if cfg.get("persona"):
        settings.append("人设\n" + cfg["persona"])
    if fixed:
        settings.append("常驻背景\n" + fixed)
    if cfg.get("pinned_context"):
        settings.append("固定上下文\n" + cfg["pinned_context"])
    if private and cfg.get("private_context"):
        settings.append("私聊固定上下文\n" + cfg["private_context"])
    settings.append("文末时间和通道附注由系统提供。")
    settings.append(instructions)

    info = (["本轮背景设定\n" + topical] if topical else []) + list(notes)
    suffix = [NOTE_HEADER + "\n".join(info)] if info else []
    when = datetime.now(BEIJING) if now is None else now.astimezone(BEIJING)
    weekday = "一二三四五六日"[when.weekday()]
    suffix.append(f"{CLOCK_HEADER}{when:%Y-%m-%d %H:%M} 星期{weekday}")
    if isinstance(current, str):
        content = current + "".join(suffix)
    else:
        content = list(current) + [{"type": "text", "text": s} for s in suffix]
    return [{"role": "system", "content": "\n\n".join(settings)}, *history,
            {"role": "user", "content": content}]


def with_chat_note(messages, note):
    """Refine a chat draft with the original history, attachments and turn clock."""
    original = messages[-1]["content"]
    extra = NOTE_HEADER + note
    if isinstance(original, str):
        words, clock = original.rsplit(CLOCK_HEADER, 1)
        content = words + extra + CLOCK_HEADER + clock
    else:
        content = [*original[:-1], {"type": "text", "text": extra}, original[-1]]
    return [*messages[:-1], {"role": "user", "content": content}]
