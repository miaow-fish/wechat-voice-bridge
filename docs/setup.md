# 配置与排查

## 先配哪几个服务

| 功能 | 在工作台怎么填 | 第一次怎么验 |
| --- | --- | --- |
| 私聊文字 | 「服务商」里配置 DeepSeek 或自己的 OpenAI 兼容地址，读取模型列表或手填服务商提供的模型 ID。在「模型与声音 → 文字聊天」选中并保存。 | 从绑定的微信发一句「你好」。 |
| 听微信语音 | 初次使用建议选「讯飞听写」，有免费测试额度。填开通了流式听写的 APPID、API Key、API Secret，选择中文和相应方言。 | 从手机发一条短语音，在「通话与记录」核对转写。 |
| 发原生语音条 | 给 ElevenLabs 填 API Key，选择支持语音合成的模型，读取音色列表或填写自己有权使用的 Voice ID。 | 在微信里说「发条语音跟我说晚安」，手机会收到可以点开的语音条。 |
| 接打微信电话 | 先验通上面三项。「通话与记录」里有「拨给已绑定的私聊」和「等待来电」。 | 助手拨来时，在手机上接听、说一句话并听到回应。测来电方向时，先点「等待来电」，再从手机打给助手号。 |
| 微信表情 | 在助手号的微信里添加表情包，在工作台「微信表情」同步。 | 发「发个表情包」或 `/表情 表情名`，在手机上查看发送结果。 |
| 图片输入 | 选择支持图片输入的接口与模型。 | 发一张照片，问图中有什么。 |

讯飞短语音听写支持最长 60 秒的输入。ElevenLabs 使用未压缩的 PCM 合成语音，微信语音条由微信通道编码。实时电话使用 16 kHz 单声道音频。「一句说完时轻响」可在模型设置中开启。[讯飞听写文档](https://www.xfyun.cn/doc/asr/voicedictation/API.html) · [ElevenLabs 语音合成](https://elevenlabs.io/docs/api-reference/text-to-speech/convert) · [Agent WeChat 通话接口](https://github.com/thisnick/agent-wechat/blob/v3.1.0/docs/src/content/docs/reference/calls-api.mdx)

每个功能可以选不同服务商。新建兼容服务时填 API 根地址，并在对应模块选择模型。识图、听写和合成是否可用取决于服务商及所选模型。DeepSeek 的聊天模型和同平台单独的音频工具分开调用。[DeepSeek 服务说明](https://api-docs.deepseek.com/)

## 私聊和群聊怎样等你说完

打开「微信连接 → 消息合并」，调整「合并连续消息」和「等待时长」，再点「保存回复设置」。例如设成 10 秒，在第 8 秒同一会话又来一条语音，就从这条语音重新等 10 秒。文字、图片、语音条和表情可以进同一轮。

在「微信连接 → 群聊」里选助手号已经加入的群。「由助手决定」把新消息交给模型判断是否参与。助手选择旁听时，其他群成员看不到回复气泡。「仅 @助手 时参与」识别微信自带的点名，群成员手打昵称或 @所有人 都不会单独触发。每个群分别计时、记录发言者和近期对话。

打开微信连接页时，工作台会同步群目录并向微信核对当前页的成员身份。改名或退群后可点「刷新群列表」，群多时可点「查看更多群」。退群后微信已返回空名单的旧群会从页面消失，重新加入后可刷新找回。查询失败或成员资料不全时，对应条目会收在「暂未确认的群」里。

关闭合并会把已收齐的消息先作为一轮处理，之后每条各回一轮。再次开启时沿用上次保存的秒数。

## 人设、世界书和上下文

在「人设与世界书 → 对话上下文」填写每次带入的历史轮数，点「保存轮数」。例如设为 2 时，每次取本会话最近两轮历史，再加当前发言。合并处理的几条微信算一轮。历史不足时带入已有轮数，0 表示跳过历史。默认 30 轮，上限 100 轮。群聊中的旁听轮也计入本群历史。

「提示词（Prompt）」里可以填写通用指令和私聊与电话专用指令。「查看内置提示词原文」列出私聊、群聊、已接通电话各自使用的短指令，以及按需调用的工具。只想回标点或一张表情包时，助手可以直接发出。世界书可导入 TXT、Markdown、DOCX 或酒馆世界书 JSON，关键词条目按对话内容激活，常驻条目随每轮使用。

「外接记忆」只登记地址，本版尚未执行外部记忆读写。陌生表情首次识图可以单独开启，已认出的同一张图会使用缓存。表情素材来自各自微信号已添加的表情包与收藏。

## 可选的本机语气识别

先验通听写和电话。需要对一句话附加语气标签时，可以在有余量的 VPS 上安装 SenseVoiceSmall，模型和依赖在安装时单独下载。默认由 socket 按需启动。

~~~sh
sudo apt-get install -y python3-pip
sudo bash install-sensevoice.sh
~~~

在「听原话和说话语气」选择本机服务，转写继续由所选听写服务提供。语气标签是识别结果，来得太晚或不确定时，聊天可以只用原话继续。[SenseVoiceSmall 说明](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)

## 服务与本机数据

| 用途 | 默认位置 |
| --- | --- |
| 微信账号接口及 systemd 服务 | `127.0.0.1:28220` · `wechat-vps-case.service` |
| 私聊工作台及 systemd 服务 | `127.0.0.1:28222` · `wechat-vps-panel.service` |
| 程序 | `/opt/wechat-vps-case` |
| 微信登录和账号数据 | `/var/lib/wechat-vps-case/hub` |
| 私聊配对、服务商设置、转写与投递记录 | `/var/lib/wechat-vps-panel` |
| 工作台连接微信通道的凭据 | `/etc/wechat-vps-case/wechat.token` |

这几个数据目录属于你自己的实例。分享项目时请发源码或 GitHub 链接。关闭电脑上的 SSH 转发只会关闭本机浏览器的入口，VPS 后台继续工作。

## 遇到问题

**浏览器打不开。** 先在 VPS 上检查两个服务，再检查电脑上转发终端是否保持连接。浏览器打开 `http://127.0.0.1:28227/`。本机端口被占用时，只改转发命令左侧的 `28227` 和浏览器地址，保持右侧 `28222`。

~~~sh
systemctl is-active wechat-vps-case.service wechat-vps-panel.service
curl -fsS http://127.0.0.1:28220/health
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:28222/api/state
~~~

**能聊文字，打电话没声音。** 分别检查来语音能否转写、发语音条能否播放、当前模型和音色是否属于你自己的服务商账号。音频服务均可用后，再在「通话与记录」查看接通状态与音频收发秒数。手机上的实际声音是验收依据。

**扫码成功但没有回复。** 打开「微信连接」核对「微信在线」和「已绑定私聊」。如果开了「合并连续消息」，从最后一条算起等到你设的秒数，再看「通话与记录」里这一轮的状态。

**安装提示已有目录或端口被占用。** 首次安装脚本用于新实例，遇到原有目录会退出。已有部署可用 `systemctl status` 查看服务。启动报错时可在本机查日志。

### 更新已有工作台

在之前克隆的仓库里运行以下命令。选一个没有通话和待发回复的时段。这个版本沿用已有的微信登录、绑定和服务商设置，工作台首次启动时自动升级聊天数据库。

~~~sh
git fetch origin --tags
git switch --detach v0.2.5
panel=/opt/wechat-vps-case/panel
sudo tar -C "$panel" --exclude=deps --exclude=__pycache__ -czf "/root/wechat-panel-code-$(date +%Y%m%d-%H%M%S).tar.gz" .
sudo systemctl stop wechat-vps-panel
sudo install -m 0644 account_stickers.py call_channel.py channel_state.py channel_worker.py conversation_prompt.py group_chats.py panel.py providers.py reply_actions.py spoken_reply.py index.html panel.js panel.css "$panel/"
sudo systemctl start wechat-vps-panel
sudo systemctl is-active wechat-vps-panel wechat-vps-case
~~~

~~~sh
sudo journalctl -u wechat-vps-case -u wechat-vps-panel -n 40 --no-pager
~~~

## 开发测试

Python 3.11 及以上，系统中需有 FFmpeg 和 ffprobe。以下测试使用临时数据库和模拟回执。

~~~sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B -m unittest discover -v
~~~
