"use strict";
const el = id => document.getElementById(id);
let live, cfg, baseline, generation, serviceID, assetID, timer, controlStamp;
const roleNames={chat:"文字聊天",vision:"图片识别",asr:"语音转写",tts:"语音合成",tone:"说话语气"};
const fields={chat:["chat_service","chat_model"],vision:["vision_service","vision_model"],asr:["asr","asr_model"],tts:["tts","tts_model"],tone:["tone_service","tone_model"]};
const assetNames={persona:"人设",worldbook:"世界书"};
let selectedRole=sessionStorage.getItem("wx-role")||"asr";
if(!Object.hasOwn(fields,selectedRole))selectedRole="asr";
const pages={
  connect:["微信连接","登录助手微信，选择好友和群聊。"],
  routes:["模型与声音","为每种用途选择实际处理它的服务。"],
  providers:["服务商","管理接口地址、凭据和可用模型。"],
  profiles:["人设与世界书","设置助手的人设、提示词、世界书和历史对话轮数。"],
  memory:["外接记忆","管理微信通道与独立记忆系统的连接配置。"],
  stickers:["微信表情","管理助手能发出的表情，以及收到表情时的识别方式。"],
  activity:["通话与记录","发起微信电话，查看语音和消息的投递情况。"]
};
function showPage(){
  const hash=location.hash.slice(1);
  const profileAnchor=hash==="context" || hash==="builtin-prompts";
  const page=profileAnchor?"profiles":Object.hasOwn(pages,hash)?hash:"connect";
  for(const section of document.querySelectorAll(".view"))section.hidden=section.id!==page;
  for(const link of document.querySelectorAll("#sections a")){
    if(link.hash==="#"+page)link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");
  }
  el("pageTitle").textContent=pages[page][0];el("pageDesc").textContent=pages[page][1];
  document.title=pages[page][0]+" · 微信助手";document.querySelector(".rail").classList.remove("menu-open");
  el("menuButton").setAttribute("aria-expanded","false");
  if(hash==="builtin-prompts")el(hash).open=true;
  if(profileAnchor)el(hash).scrollIntoView();else window.scrollTo(0,0);
  if(page==="connect" && live?.group_account)startGroupsOnce();
}
el("menuButton").onclick=()=>el("menuButton").setAttribute("aria-expanded",String(document.querySelector(".rail").classList.toggle("menu-open")));
window.addEventListener("hashchange",showPage);
showPage();
const typeNames={compatible:"OpenAI 兼容",deepseek:"DeepSeek 官方",elevenlabs:"ElevenLabs 合成",xfyun:"讯飞听写"};
const builtin=["deepseek","xfyun","elevenlabs"];
const kindName={text:"文字",voice:"语音条",sticker:"表情包"};
function notice(txt){const n=el("notice");n.textContent=txt;n.hidden=false;clearTimeout(timer);timer=setTimeout(()=>{n.hidden=true;},7500);}
function node(tag,txt){const n=document.createElement(tag);if(txt!==undefined)n.textContent=txt;return n;}
function option(value,txt){return new Option(txt,value);}
function label(txt,control){const n=node("label",txt);n.append(control);return n;}
function btn(txt,fn){const b=node("button",txt);b.type="button";b.className="secondary";b.onclick=fn;return b;}
async function api(path,data){
  const opt=data===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)};
  const r=await fetch("/api/"+path,{...opt,cache:"no-store"});
  const j=await r.json();if(!r.ok)throw Error(j.error||"操作失败");return j;
}
async function withButton(b,work,feedback=notice){b.disabled=true;try{await work();}catch(e){feedback(e.message);}finally{b.disabled=false;}}
function service(id){return live.providers.find(s=>s.id===id);}
const availableGroups = new Map(), unverifiedGroups = new Map();
let groupCursor = "", groupChecked = "", groupBusy = false, groupAttempted = false;
let groupEpoch = 0, catalogComplete = true, groupError = "", lastRoutes;
function resetGroups() {
  groupEpoch++;
  availableGroups.clear();unverifiedGroups.clear();
  groupCursor="";groupChecked="";groupBusy=false;groupAttempted=false;
  catalogComplete=true;groupError="";lastRoutes=undefined;
}
function renderGroupRows(list, groups, routes, verified) {
  list.replaceChildren();
  for(const [id,name] of groups) {
    const mode=routes[id]?.mode || "off";
    if(!verified && mode==="off"){list.append(node("div",name));continue;}
    const control=node("select");
    for(const [value,title] of [["off","关闭"],["auto","由助手决定"],["mention","仅 @助手 时参与"]])
      if(verified || value==="off" || value===mode)control.append(option(value,title));
    control.value=mode;
    control.onchange=async()=>{
      const mode=control.value;
      control.disabled=true;
      try {const result=await api("groups",{id,mode});await refresh();notice(result.message);}
      catch(e){control.value=live.group_routes?.[id]?.mode || "off";notice(e.message);}
      finally{control.disabled=false;}
    };
    list.append(label(name,control));
  }
}
function showGroups(routes) {
  renderGroupRows(el("groupList"),availableGroups,routes,true);
  renderGroupRows(el("unverifiedList"),unverifiedGroups,routes,false);
  el("unverifiedGroups").hidden=!unverifiedGroups.size;
  el("unverifiedTitle").textContent="暂未确认的群（"+unverifiedGroups.size+"）";
  el("readGroups").disabled=!live?.group_account || groupBusy;
  el("moreGroups").hidden=!groupCursor;el("moreGroups").disabled=groupBusy;
  let status="登录后可查看群聊";
  if(groupChecked)status=groupChecked+" 已核对 · 已确认 "+availableGroups.size+" 个群"+(catalogComplete?"":" · 群目录尚未同步完整");
  if(groupBusy)status="正在向微信核对群聊…";
  el("groupState").textContent=groupError || status;
}
async function loadGroups(more=false) {
  if(groupBusy || !live?.group_account || (more && !groupCursor))return;
  const epoch=groupEpoch;
  groupBusy=true;groupAttempted=true;groupError="";showGroups(live.group_routes || {});
  try{
    const page=more?await api("groups?cursor="+encodeURIComponent(groupCursor)):await api("groups/refresh",{});
    if(epoch!==groupEpoch)return;
    if(!more){availableGroups.clear();unverifiedGroups.clear();catalogComplete=page.catalogComplete;}
    for(const item of page.unverified){
      availableGroups.delete(item.id);unverifiedGroups.set(item.id,item.name);
    }
    for(const item of page.items){
      unverifiedGroups.delete(item.id);availableGroups.set(item.id,item.name);
    }
    groupCursor=page.nextCursor || "";
    groupChecked=new Date().toLocaleTimeString("zh-CN",{hour12:false});
  }catch(e){if(epoch===groupEpoch)groupError="群列表刷新失败："+e.message;}
  finally{if(epoch===groupEpoch){groupBusy=false;showGroups(live.group_routes || {});}}
}
function startGroupsOnce(){
  if(!groupAttempted)loadGroups();
}
el("readGroups").onclick=()=>loadGroups();
el("moreGroups").onclick=()=>loadGroups(true);
const mergeSwitch = el("mergeMessages"), mergeSeconds = el("mergeSeconds");
mergeSeconds.max = String(Number.MAX_SAFE_INTEGER);
function syncMergeControls() {
  if (!mergeSwitch.checked && (!Number.isSafeInteger(mergeSeconds.valueAsNumber) || mergeSeconds.valueAsNumber < 1)) {
    mergeSeconds.value = baseline?.merge_wait_seconds ?? mergeSeconds.defaultValue;
  }
  mergeSeconds.disabled = !mergeSwitch.checked;
  el("mergeHelp").textContent = mergeSwitch.checked
    ? "每收到一条就重新计时，等你发完再一起回复。"
    : "每条新消息单独回复。";
}
mergeSwitch.addEventListener("change", syncMergeControls);
function capture(){
  if(!cfg)return;
  const f=el("settings");
  for (const n of f.querySelectorAll("[name]")) {
    if (!n.name || !Object.hasOwn(cfg, n.name)) continue;
    if (n.type === "checkbox") cfg[n.name] = n.checked;
    else if (n.type === "number") cfg[n.name] = n.value === "" ? null : n.valueAsNumber;
    else cfg[n.name] = n.value;
  }
}
function changedSettings(){
  capture();
  return Object.fromEntries(Object.entries(cfg||{}).filter(([k,v])=>v!==baseline?.[k]));
}
async function persist(section,controls){
  const changes=changedSettings(), patch={};
  const names=new Set(controls.map(n=>n.name));
  if(section.id==="routes")for(const field of [...Object.values(fields).flat(),"voice"])names.add(field);
  for(const name of names)if(Object.hasOwn(changes,name))patch[name]=changes[name];
  if(Object.keys(patch).length)await api("settings",patch);
  await refresh(true);
}
let promptRequest=0;
async function loadBuiltinPrompts(){
  if(!el("builtin-prompts").open)return;
  const request=++promptRequest, list=el("promptList");
  try{
    const entries=await api("builtin-prompts");
    if(request!==promptRequest)return;
    const tools=node("details");
    tools.open=Boolean(list.querySelector("details[open]"));
    tools.append(node("summary","按需使用的工具和补充指令"));
    const scenes=[];
    for(const entry of entries){
      const section=node("section");
      section.append(node("h3",entry.title),node("p",entry.when),node("pre",entry.text));
      if(entry.kind==="tool")tools.append(section);
      else scenes.push(section);
    }
    list.replaceChildren(...scenes,tools);
  }catch(e){if(request===promptRequest)list.textContent=e.message;}
}
el("builtin-prompts").ontoggle=loadBuiltinPrompts;
function roleService(role,s){
  if(!s)return "";
  if(s.kind==="deepseek"&&role==="asr")return s.name+" 语音转写工具（整段录音）";
  if(s.kind==="deepseek"&&role==="tts")return s.name+" 语音合成工具";
  return s.name;
}
function renderServices(){
  const parent=el("services");parent.replaceChildren();
  for(const s of live.providers){
    const row=node("div");row.className="row";
    const info=node("div");info.append(node("h3",s.name));
    const status=s.configured?(s.has_key?"已填凭据":"未填密钥"):"待填凭据";
    info.append(node("p",[typeNames[s.kind],status,...(s.kind!=="xfyun"?[s.models.length+" 个模型"]:[])].join(" · ")));
    row.append(info,btn("配置",()=>openService(s.id)));parent.append(row);
  }
}
function modelControl(role,s,field){
  const wrap=node("div");if(!s)return wrap;
  if((role==="tts"||role==="asr")&&(s.kind==="xfyun"||s.kind==="deepseek")){
    wrap.className="route-help";
    wrap.append(node("p",s.kind==="xfyun"?"通话时边说边听写，新用户有每日免费测试额度。":role==="asr"?"使用独立转写接口，整段识别后将文字交给聊天模型。":"使用独立合成接口将回复转成声音。"));
    return wrap;
  }
  const choices=s.models.filter(m=>!m.uses.length||m.uses.includes(role));
  const value=cfg[field]||"";
  if(value&&!choices.some(m=>m.id===value))choices.push({id:value,label:value,uses:[]});
  const sel=node("select");
  sel.setAttribute("aria-label",roleNames[role]+"模型");
  sel.append(option("","选择模型"),...choices.map(m=>option(m.id,m.label===m.id?m.id:m.label+" · "+m.id)),option("!manual","手动填写模型 ID…"));
  sel.value=value;
  const custom=node("input");custom.name=field;custom.value=value;custom.spellcheck=false;custom.placeholder="填写此服务商的模型 ID";
  custom.setAttribute("aria-label",roleNames[role]+"模型 ID");custom.hidden=true;
  if(!value&&!choices.length){sel.value="!manual";custom.hidden=false;}
  sel.onchange=()=>{custom.hidden=sel.value!=="!manual";custom.value=custom.hidden?sel.value:"";if(!custom.hidden)custom.focus();};
  wrap.append(label("模型",sel),custom);
  return wrap;
}
function renderModules(){
  const parent=el("moduleRows");parent.replaceChildren();
  const menu=el("roleMenu");menu.replaceChildren();
  for(const [role,[providerField,modelField]] of Object.entries(fields)){
    const item=btn(roleNames[role],()=>{capture();selectedRole=role;sessionStorage.setItem("wx-role",role);renderModules();el("role-"+role).focus();});
    item.id="role-"+role;item.setAttribute("aria-pressed",String(role===selectedRole));item.append(node("small",service(cfg[providerField])?.name||(cfg[providerField]==="local-sensevoice"?"SenseVoiceSmall":"关闭")));menu.append(item);
    const row=node("div");row.className="subrow";row.hidden=role!==selectedRole;row.append(node("h2",roleNames[role]));
    const grid=node("div");grid.className="grid";const list=node("select");list.name=providerField;
    if(["tts","asr","tone"].includes(role))list.append(option("off","关闭"));
    if(role==="tone" && (live.local_tone_available || cfg[providerField]==="local-sensevoice")){
      list.append(option("local-sensevoice","SenseVoiceSmall · 本机"));
    }
    for(const s of live.providers.filter(p=>p.roles.includes(role)))list.append(option(s.id,roleService(role,s)));
    list.value=cfg[providerField];const selected=service(list.value);
    list.onchange=()=>{capture();cfg[modelField]="";if(role==="tts")cfg.voice=list.value==="deepseek"?"tide":""; renderModules();};
    grid.append(label("服务商",list),modelControl(role,selected,modelField));row.append(grid);
    if(role==="tone"){
      if(list.value==="local-sensevoice"){
        row.append(node("p",live.local_tone_available?"用已选听写转文字，本机从同一段原音提取语气。":"本机语气服务暂不可用，继续使用已选听写。"));
      }else if(selected){
        const format=node("select");format.name="tone_format";
        format.append(option("input_audio","input_audio · WAV"),option("audio_url","audio_url · WAV data URI"));
        format.value=cfg.tone_format;row.append(label("接口接收音频的格式",format));
        row.append(node("p","每条原音交给所选服务一次，同时返回原话和语气。失败时使用已选听写。"));
      } else row.append(node("p","可在「服务商」添加能直接听音频的兼容接口。"));
    }
    if(role==="asr"&&selected?.kind==="xfyun"){
      const lng=node("select");lng.name="asr_language";lng.append(option("zh_cn","中文"),option("en_us","英语"));lng.value=cfg.asr_language;
      const accent=node("select");accent.name="asr_accent";accent.append(option("mandarin","普通话"),option("cantonese","粤语"));accent.value=cfg.asr_accent;
      const options=node("div");options.className="grid";options.append(label("听写语言",lng),label("中文方言",accent));row.append(options);
    }
    if(role==="tts"&&selected){
      const voice=node("input");voice.name="voice";voice.value=cfg.voice;voice.setAttribute("list","voicelist");
      voice.placeholder=selected.kind==="elevenlabs"?"粘贴 ElevenLabs Voice ID":selected.kind==="deepseek"?"tide 或 stella":"填写服务商的音色 ID";voice.spellcheck=false;
      const dl=node("datalist");dl.id="voicelist";const tools=node("div");tools.append(label("音色 ID",voice),dl);
      if(selected.kind==="deepseek")dl.append(option("tide","Tide"),option("stella","Stella"));
      if(selected.kind==="elevenlabs"){
        const rate=node("select");rate.name="note_rate";
        rate.append(option("16000","16 kHz · 直接送入微信编码器"),option("24000","24 kHz · 编码前转为 16 kHz"));
        rate.value=cfg.note_rate;tools.append(label("语音条合成采样率",rate));
        tools.append(btn("读取我的音色",e=>withButton(e.currentTarget,async()=>{
          const {voices}=await api("providers/voices",{id:selected.id});
          const voicesSelect=node("select");voicesSelect.setAttribute("aria-label","选择账户音色");voicesSelect.append(option("","选择音色"),...voices.map(v=>option(v.id,v.name)));
          voicesSelect.onchange=()=>{if(voicesSelect.value)voice.value=voicesSelect.value;};tools.append(voicesSelect);notice("已读取 "+voices.length+" 个音色");
        })));
      }
      const cue=node("select");cue.name="call_cue";
      cue.append(option("on","轻响一下"),option("off","自然停顿"));cue.value=cfg.call_cue;
      tools.append(label("电话里助手说完一轮后",cue));
      const interrupt=node("input");interrupt.type="checkbox";interrupt.name="call_barge_in";
      interrupt.className="switch";interrupt.setAttribute("role","switch");
      interrupt.checked=cfg.call_barge_in;
      tools.append(label("插话时打断助手",interrupt));
      row.append(tools);
    }
    parent.append(row);
  }
}
function renderAssets(){
  const area=el("assets");area.replaceChildren();
  for(const [kind,title] of Object.entries(assetNames)){
    const row=node("div");row.className="card";const list=node("select");list.name=kind+"_id";
    list.append(option("","暂不使用"),...live.library.filter(a=>a.kind===kind).map(a=>option(a.id,a.name)));
    list.value=cfg[list.name]||"";
    const edit=btn("编辑",e=>withButton(e.currentTarget,()=>openAsset(kind,list.value)));edit.disabled=!list.value;list.onchange=()=>{edit.disabled=!list.value;};
    const head=node("div");head.className="section-head";head.append(node("h2",title));
    const picker=el(kind+"File");
    head.append(btn("导入"+title,()=>{picker.value="";picker.click();}));
    row.append(head);
    if(kind==="persona")row.append(node("p","支持 TXT、Markdown、Word 和酒馆角色卡（JSON、PNG）。导入后可预览、编辑再选用。"));
    if(kind==="worldbook")row.append(node("p","支持 TXT、Markdown、Word（.docx）与酒馆世界书 JSON。文件可先预览，再选用。"));
    const controls=node("div");controls.className="asset-controls";
    controls.append(label("当前"+title,list),edit,btn(kind==="persona"?"新建人设":"手写世界书",()=>openAsset(kind)));row.append(controls);area.append(row);
  }
  const legacy=el("legacyMemory");legacy.replaceChildren();
  const saved=live.library.filter(a=>a.kind==="memory");
  if(saved.length){
    const select=node("select");select.append(option("","选择此前登记的地址"),...saved.map(a=>option(a.id,a.name)));
    select.onchange=()=>{const a=saved.find(a=>a.id===select.value);if(a)el("settings").elements.memory_url.value=a.body;};
    legacy.append(label("已有连接",select));
  }
}
function applyState(s,force=false,discard=[]){
  const changes=cfg?changedSettings():{};
  for(const key of discard)delete changes[key];
  const updated=JSON.stringify(s.settings)!==JSON.stringify(baseline);
  if(live?.group_account!==s.group_account)resetGroups();
  live=s;
  const routes=JSON.stringify(s.group_routes || {});
  if(routes!==lastRoutes){showGroups(s.group_routes || {});lastRoutes=routes;}
  if(s.group_account && !el("connect").hidden)startGroupsOnce();
  baseline={...s.settings};cfg={...baseline,...changes};
  const stamp=JSON.stringify([s.providers,s.library,s.local_tone_available]);
  if(force||updated||stamp!==controlStamp){
    for(const name of ["name","memory_url","sticker_vision","pinned_context","private_context"])el("settings").elements[name].value=cfg[name];
    el("historyTurns").value=cfg.history_turns;
    loadBuiltinPrompts();
    mergeSwitch.checked = cfg.merge_messages;
    el("splitBubbles").checked=cfg.split_bubbles;
    mergeSeconds.value = cfg.merge_wait_seconds ?? "";
    syncMergeControls();
    renderServices();renderModules();renderAssets();controlStamp=stamp;
  }
  const l=s.login||{};const ended=["expired","failed","rejected","cancelled","needs_login"].includes(l.state);
  el("summary").textContent=(s.logged?"微信在线":"微信待登录")+" · "+(s.paired?"已绑定私聊":"私聊待绑定");
  el("summary").classList.toggle("online",s.logged);
  el("memoryState").textContent=s.serein?.paired?"已配对私聊":s.settings.memory_url?"等待配对":"未启用";
  el("checkMemory").disabled=!s.serein?.paired;
  el("memoryCounts").textContent=s.serein?.paired?`已归档 ${s.serein.archived} 轮 · 待归档 ${s.serein.pending} 轮`:"";
  el("wxstate").textContent=s.logged?"已登录":ended?"上一次登录已结束，可以重新取码":l.state?"等待手机扫码或确认":"尚未登录";
  el("login").disabled=s.logged;el("pair").disabled=!s.logged||s.paired;
  el("pairstate").textContent=s.paired?"已绑定，助手可以在这段私聊中回复":s.logged?"请生成口令并私发给助手号":"等待助手号登录";
  el("pairbox").hidden=!s.paircode||s.paired;
  if(s.paircode)el("paircode").textContent=s.paircode;
  const show=!s.logged&&!ended&&Boolean(l.generation);el("scan").hidden=!show;
  if(show&&generation!==l.generation){generation=l.generation;el("qr").src="/api/qr?t="+Date.now();}
  if(!show){generation=null;el("qr").removeAttribute("src");}
  el("deliveries").textContent=Object.entries(kindName).map(([k,v])=>v+" "+(s.accepted[k]||0)).join("　·　")+"　（微信服务端已接收）";
  const groups=el("stickerGroups");groups.replaceChildren();
  for(const [name,count] of Object.entries(s.stickers||{})){const pack=node("div");pack.append(node("strong",name),node("small",count+" 张"));groups.append(pack);}
  el("stickerSyncTime").textContent=s.sticker_sync?"最近同步 "+new Date(s.sticker_sync*1000).toLocaleString("zh-CN"):"同步后显示账号表情清单";
  el("lastSticker").textContent=s.last_sticker?"最近收到 · "+s.last_sticker:"还没有收到私聊表情";
  el("speechBox").hidden=!s.speech;
  if(s.speech){
    const cue=s.speech.tone?"〔"+s.speech.tone+"〕":"";
    const fallback=s.speech.tone_status==="fallback"?"（语气服务未成功，已使用备用听写）":"";
    el("speech").textContent=(s.speech.provider?s.speech.provider+" · ":"")+s.speech.transcript+cue+fallback;
  }
  el("syncStickers").disabled=!s.logged;
  const call=s.call||{};el("callStatus").textContent=(call.stage||"尚未测试")+" · 收到音频 "+((call.received||0)*.02).toFixed(1)+" 秒 · 发出音频 "+((call.sent||0)*.02).toFixed(1)+" 秒";
  if(Number.isFinite(call.latencyMs?.["到第一帧"]))el("callStatus").textContent+=" · 上一轮开口用时 "+(call.latencyMs["到第一帧"]/1000).toFixed(1)+" 秒";
  if(call.maxFeedGapMs>20)el("callStatus").textContent+=" · 本机播放供音最长中断 "+call.maxFeedGapMs+" 毫秒";
  const autoAnswer=el("autoAnswerCall");
  if(!autoAnswer.disabled)autoAnswer.checked=Boolean(s.settings.call_auto_answer);
  el("callStandby").textContent=s.settings.call_auto_answer?(s.call_listener?.message||"正在连接微信来电"):"";
  el("dialCall").disabled=!s.logged||!s.paired||Boolean(call.active);
  el("answerCall").disabled=el("dialCall").disabled||autoAnswer.checked;
  el("stopCall").disabled=!call.active;
  el("last").textContent=!s.last?"还没有聊天记录":s.last.status==="silent"?"最近一轮群聊，助手选择旁听":s.last.status==="disabled"?"本轮已随群聊设置关闭":s.last.status==="accepted"?"最近一轮已取得微信回执":s.last.status==="call_connected"?"最近一通微信电话已接通":s.last.status==="call_requested"?"已发起微信通话":s.last.error||"正在处理本轮消息";
  if(s.cursor_gap)el("last").textContent+="。曾有微信事件过期，期间可能漏收。";
}
let stateReads=Promise.resolve();
function refresh(force=false,discard=[]){
  const read=stateReads.then(async()=>applyState(await api("state"),force,discard));
  stateReads=read.catch(()=>{});
  return read;
}
const sf=el("serviceForm"),af=el("assetForm");
function serviceFields(){
  const k=sf.elements.kind.value;
  el("baseLabel").hidden=k!=="compatible";el("appidLabel").hidden=el("secretLabel").hidden=k!=="xfyun";
  el("manualLabel").hidden=el("syncService").hidden=k==="xfyun";
}
function openService(id){
  const s=id?service(id):null;serviceID=id||"";sf.reset();
  sf.elements.name.value=s?.name||"";sf.elements.kind.value=s?.kind||"compatible";
  sf.elements.kind.disabled=builtin.includes(id);sf.elements.base.value=s?.base||"";
  sf.elements.manual.value=(s?.manual||[]).join("\n");sf.elements.app_id.value=s?.app_id||"";
  sf.elements.key.placeholder=s?.has_key?"已保存，留空沿用":"填写这个服务商的 API Key";
  sf.elements.secret.placeholder=s?.has_secret?"已保存，留空沿用":"填写讯飞 API Secret";
  el("serviceResult").textContent="";el("serviceTitle").textContent=s?"配置 "+s.name:"新增服务商";
  serviceFields();el("serviceDialog").showModal();
}
sf.elements.kind.onchange=serviceFields;
async function saveService(sync){
  const e=sf.elements;
  const result=await api("providers",{id:serviceID,name:e.name.value,kind:e.kind.value,base:e.base.value,key:e.key.value.trim(),
    secret:e.secret.value.trim(),app_id:e.app_id.value.trim(),manual:e.manual.value.split(/[\s,，]+/).filter(Boolean)});
  serviceID=result.id;e.key.value=e.secret.value="";
  await refresh(true);
  if(sync){const synced=await api("providers/sync",{id:serviceID});await refresh(true);notice(synced.message);}
  else notice(result.message);
  el("serviceDialog").close();
}
sf.onsubmit=e=>{e.preventDefault();withButton(e.submitter||sf.querySelector("button:not([type=button])"),()=>saveService(false),msg=>el("serviceResult").textContent=msg);};
el("syncService").onclick=e=>{if(sf.reportValidity())withButton(e.currentTarget,()=>saveService(true),msg=>el("serviceResult").textContent=msg);};
async function openAsset(kind,id,imported){
  const a=live.library.find(v=>v.id===id);
  let body=imported?.body??a?.body??"";
  if(a?.format)body=(await api("worldbooks/open",{id})).body;
  af.reset();assetID=id||"";
  af.elements.kind.value=kind;af.elements.format.value=imported?.format||a?.format||"text";
  af.elements.name.value=imported?.name||a?.name||"";af.elements.body.maxLength=kind==="worldbook"?240000:16000;af.elements.body.value=body;
  el("assetTitle").textContent=(imported?"预览并导入":a?"编辑":"新建")+assetNames[kind];
  const info=imported||a;
  const json=af.elements.format.value==="json";
  el("assetBodyLabel").textContent=json?"世界书 JSON":"正文";
  el("importInfo").textContent=kind==="worldbook"?(json?(info?.count||0)+" 条，启用 "+(info?.enabled||0)+" 条。支持常驻与文字关键词匹配，单轮最多 16,000 字符。":"正文作为常驻背景。每轮最多使用 16,000 字符。"):"";
  el("assetResult").textContent="";el("assetDialog").showModal();
}
async function importAsset(kind,e){
  const file=e.target.files[0];if(!file)return;
  try{
    if(file.size>2*1024*1024)throw Error("请选择 2 MiB 以内的文件");
    const bytes=new Uint8Array(await file.arrayBuffer());let binary="";
    for(let i=0;i<bytes.length;i+=16384)binary+=String.fromCharCode(...bytes.subarray(i,i+16384));
    const preview=await api(kind==="persona"?"personas/preview":"worldbooks/preview",{filename:file.name,file:btoa(binary)});
    await openAsset(kind,null,preview);
  }catch(err){notice(err.message);}
}
for(const kind of Object.keys(assetNames))el(kind+"File").onchange=e=>importAsset(kind,e);
af.onsubmit=e=>{e.preventDefault();withButton(e.submitter||af.querySelector("button:not([type=button])"),async()=>{
  const kind=af.elements.kind.value;
  await api("library",{id:assetID,kind,name:af.elements.name.value,body:af.elements.body.value,format:af.elements.format.value,select:true});
  await refresh(true,[kind+"_id",kind]);
  el("assetDialog").close();notice("资料已保存并选用");
},msg=>el("assetResult").textContent=msg);};
function saveSettings(e){
  e.preventDefault();const form=e.currentTarget;
  const section=form===el("historySettings")?el("context"):document.querySelector("#settings .view:not([hidden])");
  if(!section)return;
  const controls=Array.from(form.elements).filter(n=>section.contains(n));
  const button=controls.find(n=>n.matches(".savebar button"));
  if(!button || controls.some(n=>n.willValidate && !n.reportValidity()))return;
  withButton(button,async()=>{await persist(section,controls);notice(section.id==="connect"?"回复设置已保存":section.id==="memory"?"记忆服务地址已保存":"设置已保存，下一轮生效");});
}
el("settings").onsubmit=saveSettings;
el("historySettings").onsubmit=saveSettings;
el("addService").onclick=()=>openService();
el("syncStickers").onclick=e=>withButton(e.currentTarget,async()=>{notice((await api("stickers/sync",{})).message);await refresh();});
el("autoAnswerCall").onchange=async e=>{
  const control=e.currentTarget, enabled=control.checked;
  control.disabled=true;
  try{await api("settings",{call_auto_answer:enabled});}
  catch(error){control.checked=!enabled;notice(error.message);}
  finally{control.disabled=false;await refresh();}
};
for(const [id,mode] of [["dialCall","dial"],["answerCall","answer"],["stopCall","stop"]]){
  el(id).onclick=e=>withButton(e.currentTarget,async()=>{notice((await api("call",{mode})).message);await refresh();});
}
for(const b of document.querySelectorAll("[data-close]"))b.onclick=()=>el(b.dataset.close).close();
el("login").onclick=e=>withButton(e.currentTarget,async()=>{notice((await api("login",{})).message);await refresh();});
el("checkMemory").onclick=e=>withButton(e.currentTarget,async()=>{await api("memory/check",{});notice("已连通本机 Serein");});
el("pair").onclick=e=>withButton(e.currentTarget,async()=>{
  const p=await api("pair",{});el("paircode").textContent=p.code;el("pairbox").hidden=false;notice(p.message);
});
async function poll(){try{await refresh();}catch(e){el("summary").textContent=e.message;}finally{setTimeout(poll,3000);}}
poll();
