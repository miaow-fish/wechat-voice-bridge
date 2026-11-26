"""Import user-selected lorebooks and select their background for a chat or a call."""
import base64
from io import BytesIO
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

import channel_state as st


MAX_FILE = 2 * 1024 * 1024
MAX_SOURCE = 240_000
TURN_LIMIT = 16_000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(raw):
    try:
        with ZipFile(BytesIO(raw)) as archive:
            part = archive.getinfo("word/document.xml")
            if part.file_size > 6 * 1024 * 1024:
                raise ValueError("Word 正文过大")
            xml = archive.read(part)
        if b"<!DOCTYPE" in xml: raise ValueError("不支持此 Word 格式")
        doc = ET.fromstring(xml)
    except (BadZipFile, KeyError, ET.ParseError, RuntimeError) as exc:
        raise ValueError("无法读取 Word 正文，请选择 .docx 或另存为纯文本") from exc
    paragraphs = []
    for p in doc.iter(W + "p"):
        paragraphs.append("".join((n.text or "") if n.tag==W+"t" else "\t" if n.tag==W+"tab" else "\n"
                                  for n in p.iter() if n.tag in (W+"t",W+"br",W+"tab")))
    return "\n".join(paragraphs)


def keywords(value):
    if isinstance(value,str): value = [x.strip() for x in value.split(",")]
    if not value: return []
    if not isinstance(value,list) or not all(isinstance(x,str) for x in value):
        raise ValueError("世界书关键词须为文字")
    return [x for x in value if x]


def parse(source, fmt):
    if not isinstance(source,str) or not source.strip() or len(source)>MAX_SOURCE:
        raise ValueError("世界书正文须在 1 至 240,000 字符之间")
    if fmt=="text": return [{"content":source.strip(),"always":True,"enabled":True,"order":0}]
    if fmt!="json": raise ValueError("不支持的世界书格式")
    try: obj=json.loads(source)
    except (ValueError,RecursionError) as exc: raise ValueError("世界书 JSON 格式无效") from exc
    if isinstance(obj,dict):
        nested=obj.get("data")
        if isinstance(nested,dict) and "character_book" in nested: obj=nested["character_book"]
        elif "character_book" in obj: obj=obj["character_book"]
        if isinstance(obj,dict): obj=obj.get("entries")
    if isinstance(obj,dict): obj=list(obj.values())
    if not isinstance(obj,list) or not 0<len(obj)<=4000: raise ValueError("文件中没有可读的世界书条目（entries）")
    entries=[]
    for n,row in enumerate(obj,1):
        if not isinstance(row,dict) or not isinstance(row.get("content"),str):
            raise ValueError(f"第 {n} 条世界书缺少文本 content")
        if not row["content"].strip(): continue
        ext=row.get("extensions") or {}
        if not isinstance(ext,dict): raise ValueError(f"第 {n} 条的扩展格式无效")
        keys=keywords(row.get("key",row.get("keys")))
        secondary=keywords(row.get("keysecondary",row.get("secondary_keys")))
        try:
            order=int(row.get("order",row.get("insertion_order",100)))
            logic=int(row.get("selectiveLogic",ext.get("selectiveLogic",0)))
        except (TypeError,ValueError) as exc: raise ValueError(f"第 {n} 条的排序或匹配方式无效") from exc
        entries.append({"content":row["content"],"always":bool(row.get("constant")), "keys":keys,
                        "enabled":not row.get("disable",False) and row.get("enabled",True) is not False,
                        "secondary":secondary if row.get("selective",True) else [],"logic":logic,
                        "case":bool(row.get("caseSensitive",row.get("case_sensitive",False))),
                        "whole":bool(row.get("matchWholeWords",ext.get("match_whole_words",False))),"order":order})
    if not entries: raise ValueError("世界书没有非空正文")
    return entries


def read_upload(filename, encoded, *, character_card=False):
    if not isinstance(filename,str) or not isinstance(encoded,str): raise ValueError("请选择要导入的文件")
    try: raw=base64.b64decode(encoded,validate=True)
    except ValueError as exc: raise ValueError("文件内容无效") from exc
    if not 0<len(raw)<=MAX_FILE: raise ValueError("请选择 2 MiB 以内的文件")
    suffix=Path(filename).suffix.lower()
    fmt="json" if suffix==".json" else "text"
    if suffix==".docx": source=read_docx(raw)
    elif suffix==".png" and character_card:
        from PIL import Image
        try:
            with Image.open(BytesIO(raw), formats=("PNG",)) as pic:
                encoded_card=pic.info.get("ccv3") or pic.info.get("chara")
                if not isinstance(encoded_card,str): raise ValueError
                source=base64.b64decode(encoded_card,validate=True).decode("utf-8-sig")
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ValueError("PNG 内没有可读取的酒馆角色卡") from exc
        fmt="json"
    elif suffix in (".txt",".md",".markdown",".json"):
        try: source=raw.decode("utf-16" if raw.startswith((b"\xff\xfe",b"\xfe\xff")) else "utf-8-sig")
        except UnicodeError as exc: raise ValueError("请用 UTF-8 或 UTF-16 保存文本") from exc
    else: raise ValueError("请选择 JSON、TXT、MD 或 DOCX 文件")
    return source,fmt


def persona_preview(filename, encoded):
    source,fmt=read_upload(filename,encoded,character_card=True)
    name=Path(filename).stem[:80]
    if fmt=="json":
        try:
            doc=json.loads(source)
        except (ValueError,RecursionError) as exc:
            raise ValueError("人设 JSON 格式无效") from exc
        card=doc.get("data",doc) if isinstance(doc,dict) else None
        if not isinstance(card,dict):
            raise ValueError("请选择带有人设正文的角色卡")
        card_name=card.get("name")
        if isinstance(card_name,str) and card_name.strip():
            name=card_name.strip()[:80]
        sections=[]
        for field,label in (("body",""),("persona",""),("description",""),("personality","性格"),
                            ("scenario","背景"),("system_prompt","角色指令"),("mes_example","对话示例")):
            value=card.get(field)
            if isinstance(value,str) and value.strip():
                sections.append((label+"\n" if label else "")+value.strip())
        if not sections:
            raise ValueError("角色卡中找不到人设、性格或背景正文")
        if isinstance(card_name,str) and card_name.strip():
            sections.insert(0,"角色名："+name)
        source="\n\n".join(sections)
    if not source.strip() or len(source.strip())>16000:
        raise ValueError("人设正文须在 1 至 16,000 字符之间")
    return {"name":name,"format":"text","body":source.strip()}


def preview(filename, encoded):
    source,fmt=read_upload(filename,encoded)
    entries=parse(source,fmt)
    return {"name":Path(filename).stem[:80],"format":fmt,"body":source,"count":len(entries),
            "enabled":sum(e["enabled"] for e in entries)}


def active(entry,text):
    if not entry["enabled"]: return False
    if entry["always"]: return True
    def matches(key):
        if entry["whole"]:
            return bool(re.search(r"(?<!\w)"+re.escape(key)+r"(?!\w)",text,0 if entry["case"] else re.I))
        return key in text if entry["case"] else key.casefold() in text.casefold()
    if not any(map(matches,entry["keys"])): return False
    secondary=entry["secondary"]
    if not secondary: return True
    hits=[matches(k) for k in secondary]
    return {0:any(hits),1:not all(hits),2:not any(hits),3:all(hits)}.get(entry["logic"],False)


def sections(cfg, history, current):
    """Keep unconditional background reusable without changing which text fits this turn."""
    ref = cfg.get("worldbook_id")
    entries = None
    if ref:
        with st.database() as db:
            entries = st.get(db, "worldbook-entries:" + ref)
    if entries is None:
        return cfg.get("worldbook", ""), ""

    recent = [m["content"] for m in history[-4:] if isinstance(m.get("content"), str)]
    text = "\n".join([*recent, current])
    fixed, topical = [], []
    used = possible = 0
    after_keyword = False
    for entry in sorted(entries, key=lambda e: e["order"]):
        if not entry["enabled"] or not (entry["always"] or entry.get("keys")):
            continue
        body = entry["content"]
        # If an earlier match could clip this entry, only include it in this turn's notes.
        guaranteed = entry["always"] and (not after_keyword or possible + len(body) <= TURN_LIMIT)
        possible += len(body) + 2
        after_keyword = after_keyword or not entry["always"]
        if active(entry, text):
            (fixed if guaranteed else topical).append(body[:TURN_LIMIT - used])
            used += len(body) + 2
            if used >= TURN_LIMIT:
                break
    return "\n\n".join(fixed), "\n\n".join(topical)
