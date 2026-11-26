"""Select only Saved or added stickers of the authenticated WeChat account."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import struct
from tempfile import NamedTemporaryFile, TemporaryFile
from threading import Lock
import time
from urllib.error import HTTPError
from urllib.request import Request
from zipfile import ZipFile

import channel_state as st
from wechat_case import cli, esc, HOST, HTTP, SCOPE, TOKEN, wx, media, image_type

PENDING = ("queued","running","pending")
UNKNOWN = "〔收到表情包〕"
DESCRIPTION_INSTRUCTIONS = "用一句中文描述画面里的主体、动作、表情和可读文字。"
SELECTION_INSTRUCTIONS = "为助手挑一张表情。候选按整包名分组，每项是编号和表情名。有指定就按要求选，否则选贴合助手回应的。只回已有编号。"
_recognizing = Lock()
CAP = 64*1024*1024
SEND = re.compile(r"^(?:(?:麻烦|请|你|可以|能不能|能|给我|帮我|再|也)\s*){0,8}(?:发给我|发|来|回)(?:一?[张个款只]|一下|下)?(?:微信)?(.{0,32}?)(?:表情包|表情贴纸|贴纸|贴图|表情)(?:给我)?(?:看看|试试)?[吧呀嘛吗呗啦呢哈啊～~！!。？?\s]*$")


def request_details(line):
    text=line.strip()
    for prefix in ("/表情包","/表情"):
        if text==prefix or text.startswith(prefix+" "):
            return text[len(prefix):].strip(),True
    if "的话" in text:
        text=text.rsplit("的话",1)[-1].strip("，, ")
    m=SEND.fullmatch(text)
    if not m:
        return None
    raw=m[1].strip().removesuffix("的").strip()
    named=re.match(r"^(?:名字叫|名字是|名叫|叫做|叫|名为)\s*",raw)
    exact=bool(named or any(c in raw for c in "「“【\""))
    name=(raw[named.end():] if named else raw).strip("「」【】“”\"' ")
    if name in ("","随便","任意","你喜欢","我下载","你下载","之前下载","收藏","我","你","你收藏"):
        name=""
    return name,exact


def request_name(line):
    result=request_details(line)
    return result[0] if result else None


def catalog():
    def item(row,pack,group,unnamed):
        orig=row.get("original") or {}
        return {"pack":pack,"item":row["id"],"label":row.get("caption") or unnamed,"group":group,
                "caption":row.get("caption") or "", "sha256":orig.get("sha256"),
                "original_id":orig.get("id") if orig.get("state")=="available" and 0<(orig.get("byteLength") or 0)<=4*1024*1024 else None}
    saved=wx("/custom-stickers")["items"]
    out=[item(r,"","我的收藏","收藏表情") for r in saved if r.get("id")]
    packs=wx("/sticker-packs?membership=added")["items"]
    for p in packs:
        if p.get("membership")!="added":
            continue
        items=wx("/sticker-packs/"+esc(p["id"])+"/stickers")["items"]
        out.extend(item(i,p["id"],p.get("name") or "已添加的表情包","表情")
                   for i in items if i.get("id"))
    return out


def wait_job(job,route="/jobs/",seconds=36):
    end=time.monotonic()+seconds
    while job.get("state") in PENDING and time.monotonic()<end:
        time.sleep(.35)
        job=wx(route+esc(job["id"]))
    return job


def synchronize(turn=None):
    key=turn or secrets.token_hex(16)
    job=wait_job(wx("/sticker-syncs",{"scope":"all"},"wc-owned-sync-"+hashlib.sha256(key.encode()).hexdigest()))
    if job.get("state") not in ("available","accepted"):
        raise ValueError("微信暂时无法同步收藏和已添加的表情包")
    return catalog()


def pixel_key(raw):
    # A static sticker can arrive re-encoded as PNG. Never identify an
    # animation solely by its first frame, or two different drawings by similarity.
    try:
        from PIL import Image
        with Image.open(BytesIO(raw)) as im:
            if im.width>1024 or im.height>1024 or getattr(im,"n_frames",1)!=1: return ""
            rgba=Image.alpha_composite(Image.new("RGBA",im.size),im.convert("RGBA"))
            return "pixels-"+hashlib.sha256(struct.pack(">II",*im.size)+rgba.tobytes()).hexdigest()
    except (ImportError,OSError,ValueError):
        return ""


def summary(items, *, index_pixels=False):
    groups={}
    names={}
    with st.database() as db: cached=st.get(db,"sticker_pixel_keys",{})
    pixels={}
    for row in items:
        groups[row["group"]]=groups.get(row["group"],0)+1
        if row.get("caption"):
            name="〔微信表情 · "+row["group"]+" · "+row["caption"]+"〕"
            ident=row["item"]
            if ident in cached: pixels[ident]=cached[ident]
            elif index_pixels:
                path=cached_png(row["pack"],ident) if row["pack"] and re.fullmatch(r"[0-9a-f]{32}",ident) else None
                try:
                    raw=None
                    if path: raw=path.read_bytes()
                    elif row.get("original_id"): raw=wx("/media/"+esc(row["original_id"])+"/content",binary=True)
                    if raw and ((path and hashlib.md5(raw).hexdigest()==ident) or hashlib.sha256(raw).hexdigest()==row.get("sha256")):
                        pixels[ident]=pixel_key(raw)
                except (OSError,ValueError): pass
            for key in (ident,row.get("sha256"),pixels.get(ident)):
                if key:
                    key=key.lower()
                    names[key]=name if key not in names or names[key]==name else None
    with st.database() as db:
        st.put(db,"sticker_inventory",groups)
        st.put(db,"sticker_names",names)
        if index_pixels: st.put(db,"sticker_pixel_keys",pixels)
        st.put(db,"sticker_synced_at",int(time.time()))
    return groups


def incoming_label(msg,body,names):
    info=msg.get("sticker") or {}
    title=info.get("caption") or info.get("name") or info.get("description")
    if not title and 0<len(body)<=80 and not re.search(r"[<>\x00-\x1f]|https?://|[0-9a-f]{20,}",body,re.I):
        clean=body.strip("[]【】 ")
        if clean.casefold() not in ("表情","表情包","动画表情","emoji","sticker"): title=clean
    if isinstance(title,str) and title.strip():
        return "〔微信表情 · "+title.strip()[:80]+"〕"
    for key in ("stickerId","md5","id"):
        match=names.get(str(info.get(key,"")).lower())
        if match: return match
    return UNKNOWN


def received(message_id,cfg):
    """Read this message's sticker attachment. Cache a description by original bytes."""
    import providers
    import deepseek_case as ds
    assets=wx("/messages/"+esc(message_id)+"/media").get("items",[])
    original=next((a for a in assets if a.get("messageId")==message_id and a.get("kind")=="sticker"
                   and a.get("variant")=="original"),None)
    if not original: return UNKNOWN
    digest=original.get("sha256") or ""
    allow_vision=cfg.get("sticker_vision","off")=="once"
    with _recognizing:
        with st.database() as db: names=st.get(db,"sticker_names")
        if names is None:
            try: summary(catalog(),index_pixels=True)
            except (OSError,ValueError,KeyError): pass
        with st.database() as db:
            names=st.get(db,"sticker_names",{})
            known=names.get(digest)
            for key in st.get(db,"sticker_source:"+digest,[]):
                known=known or names.get(key)
            if not known and digest and allow_vision: known=st.get(db,"sticker_seen:"+digest)
        if known: return known
        data=media(message_id,("original",))
        digest=hashlib.sha256(data).hexdigest()
        pixels=pixel_key(data)
        md5=hashlib.md5(data).hexdigest()
        with st.database() as db:
            st.put(db,"sticker_source:"+digest,[md5,pixels])
            known=names.get(md5) or names.get(digest) or names.get(pixels)
            if not known and allow_vision:
                known=st.get(db,"sticker_seen:"+digest) or (pixels and st.get(db,"sticker_seen:"+pixels))
                if known: st.put(db,"sticker_seen:"+digest,known)
        if known or not allow_vision: return known or UNKNOWN
        pic=ds.image_part(data,image_type(data))
        result=providers.chat(cfg,[{"role":"system","content":DESCRIPTION_INSTRUCTIONS},
                                   {"role":"user","content":[pic]}],vision=True)
        description=" ".join(result.split())[:360]
        if not description: return UNKNOWN
        label="〔微信表情画面 · "+description+"〕"
        with st.database() as db:
            st.put(db,"sticker_seen:"+digest,label)
            if pixels: st.put(db,"sticker_seen:"+pixels,label)
        return label


def descriptive_choice(wanted,items,settings):
    import providers
    if len(items)>220:
        finalists=[descriptive_choice(wanted,items[i:i+200],settings) for i in range(0,len(items),200)]
        return descriptive_choice(wanted,finalists,settings)
    options=items
    packs={}
    for number,row in enumerate(options,1):
        packs.setdefault(row["group"],[]).append([number,row["label"]])
    data={"可选表情":packs,"本轮":wanted}
    context=[{"role":"system","content":SELECTION_INSTRUCTIONS},
             {"role":"user","content":json.dumps(data,ensure_ascii=False,separators=(",",":"))}]
    reply=providers.chat(settings,context)
    if not re.fullmatch(r"[1-9][0-9]{0,2}",reply.strip()):
        raise ValueError("表情选择服务未返回有效编号")
    n=int(reply)
    if not 1<=n<=len(options):
        raise ValueError("表情选择超出当前微信库存")
    return options[n-1]


def choose(ident,wanted,*,description_settings=None,exact_name=False):
    with st.database() as db:
        previous=st.get(db,"sticker:"+ident)
        last=st.get(db,"last_owned_sticker")
    if previous:
        return previous
    rows=synchronize(ident)
    def matching(items):
        needle=wanted.casefold().strip()
        if not needle:
            return [r for r in items if r["pack"]] or items
        named=[r for r in items if r["label"].casefold()==needle]
        if named or exact_name:
            return named
        return [r for r in items if needle in (r["group"]+" "+r["label"]).casefold()]
    options=matching(rows)
    summary(rows)
    if not options and description_settings is not None and not exact_name:
        options=rows
    if not options:
        if wanted:
            raise ValueError("当前微信账号的表情库中没有找到「"+wanted[:32]+"」")
        raise ValueError("当前微信账号还没有收藏或添加可发送的表情")
    candidates=[r for r in options if r["item"]!=last] or options
    if len(candidates)>1 and description_settings is not None:
        picked=descriptive_choice(wanted,candidates,description_settings)
    else:
        picked=secrets.choice(candidates)
    with st.database() as db:
        db.execute("BEGIN IMMEDIATE")
        previous=st.get(db,"sticker:"+ident)
        if previous:
            return previous
        st.put(db,"sticker:"+ident,picked)
    return picked


def png_path(pack,item):
    if not re.fullmatch(r"[a-f0-9]{32}",item):
        raise ValueError("微信表情编号无效")
    return st.ROOT/"sticker-originals"/hashlib.sha256(pack.encode()).hexdigest()/(item+".png")


def cached_png(pack,item):
    path=png_path(pack,item)
    if path.is_file() and hashlib.md5(path.read_bytes()).hexdigest()==item:
        return path
    return None


def read_pack_pngs(pack,job):
    # Some account-owned packs contain PNG pixels in entries named .gif.
    # Match original bytes to official item IDs before handing them to the native preparer.
    if job.get("kind")!="sticker_acquire" or not re.fullmatch(r"job_[\w-]{1,100}",job.get("id","")):
        raise ValueError("微信表情下载记录无效")
    ids={r["id"] for r in wx("/sticker-packs/"+esc(pack)+"/stickers")["items"]}
    path="/media/"+esc(job["id"]+"_archive")
    meta=wx(path)
    if meta.get("state")!="available" or meta.get("contentType")!="application/zip" or not 0<(meta.get("byteLength") or 0)<=CAP:
        raise ValueError("已添加表情包的原图压缩包尚未就绪")
    req=Request(HOST+SCOPE+path+"/content",headers={"Authorization":"Bearer "+TOKEN.read_text().strip()})
    with TemporaryFile(dir=st.ROOT) as archive, HTTP.open(req,timeout=30) as resp:
        size=0
        digest=hashlib.sha256()
        while chunk:=resp.read(65536):
            size+=len(chunk)
            if size>CAP: raise ValueError("表情包超过本机下载上限")
            digest.update(chunk)
            archive.write(chunk)
        if size!=meta["byteLength"] or digest.hexdigest()!=meta.get("sha256"):
            raise ValueError("表情包原件校验失败")
        archive.seek(0)
        with ZipFile(archive) as z:
            members=z.infolist()
            if len(members)>2*len(ids)+64 or sum(m.file_size for m in members)>CAP:
                raise ValueError("微信表情包解压大小超限")
            name=re.compile(re.escape(pack)+r"/large/[0-9]+\.(gif|png)",re.I)
            for m in members:
                if not name.fullmatch(m.filename) or not 0<m.file_size<=4*1024*1024 or m.flag_bits&1:
                    continue
                data=z.read(m)
                item=hashlib.md5(data).hexdigest()
                if item not in ids or not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data)<24:
                    continue
                w,h=struct.unpack_from(">II",data,16)
                if not (0<w<=1024 and 0<h<=1024):
                    continue
                dest=png_path(pack,item)
                dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                with NamedTemporaryFile(dir=dest.parent,delete=False) as f:
                    f.write(data)
                    temp=f.name
                Path(temp).replace(dest)


def pack_job(pack):
    token=hashlib.sha256(pack.encode()).hexdigest()
    return wait_job(wx("/sticker-downloads",{"packId":pack},"wc-owned-pack-"+token))


def native_ready(row):
    original=row.get("original") or {}
    return original.get("state")=="available" and original.get("contentType")=="image/gif" and bool(original.get("sha256"))


def ensure_owned(choice):
    pack,item=choice["pack"],choice["item"]
    if pack:
        base="/sticker-packs/"+esc(pack)
        if wx(base).get("membership")!="added" or wx(base+"/stickers/"+esc(item)).get("id")!=item:
            raise ValueError("这张表情已不在当前账号的已添加整包内")
    elif wx("/custom-stickers/"+esc(item)).get("id")!=item:
        raise ValueError("这张表情已不在当前账号收藏内")


def prepare(choice):
    pack,item=choice["pack"],choice["item"]
    if not pack:
        row=wx("/custom-stickers/"+esc(item))
        if not native_ready(row):
            job=wait_job(wx("/sticker-downloads",{"stickerId":item},"wc-saved-"+hashlib.sha256(item.encode()).hexdigest()))
            if job.get("state")!="available" or not native_ready(wx("/custom-stickers/"+esc(item))):
                raise ValueError("这张收藏表情的原件暂不可用")
        return {"kind":"saved","stickerId":item}
    base="/sticker-packs/"+esc(pack)
    if wx(base).get("membership")!="added":
        raise ValueError("这个表情包已从微信账号移除")
    row=wx(base+"/stickers/"+esc(item))
    if row.get("id")!=item:
        raise ValueError("所选表情不属于这个整包")
    ref={"kind":"pack","packId":pack,"stickerId":item}
    if native_ready(row):
        return ref
    png=cached_png(pack,item)
    if not png:
        job=pack_job(pack)
        if native_ready(wx(base+"/stickers/"+esc(item))):
            return ref
        if job.get("state")=="unsupported" and job.get("reason")=="unsupported_media":
            read_pack_pngs(pack,job)
            png=cached_png(pack,item)
    if not png:
        raise ValueError("这张微信表情的原图尚未可用")
    cachekey="sticker-gif:"+pack+":"+item
    with st.database() as db:
        cached=st.get(db,cachekey,{})
    if cached.get("expires",0)>time.time()+90:
        try:
            previous=wx("/sticker-preparations/"+esc(cached["id"]))
        except HTTPError as err:
            if err.code not in (404,410):
                raise
        else:
            orig=(previous.get("result") or {}).get("original") or {}
            if previous.get("state")=="available" and orig.get("sha256")==cached["sha256"]:
                return {"kind":"prepared","preparationId":cached["id"]}
    key=hashlib.sha256((pack+":"+item+":"+str(int(time.time()//86400))).encode()).hexdigest()
    job=wait_job(cli("stickers","prepare",png,"--idempotency-key","wc-gif-"+key,"--detach"),"/sticker-preparations/")
    result=job.get("result") or {}
    gif=result.get("original") or {}
    if job.get("state")!="available" or gif.get("contentType")!="image/gif" or not gif.get("sha256") or not 0<(gif.get("byteLength") or 0)<=16*1024*1024:
        raise ValueError("微信没有生成可发送的原生表情")
    with st.database() as db:
        st.put(db,cachekey,{"id":job["id"],"sha256":gif["sha256"],"expires":int(result.get("expiresAt") or 0)})
    return {"kind":"prepared","preparationId":job["id"]}
