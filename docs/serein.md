# 用自己电脑上的 Serein 记住微信私聊

在工作台绑定好友后，可给这段私聊接入自己的 Serein。助手用当轮微信文字或语音条转写查询记忆，相关片段放在当轮的背景附注。收到微信投递回执后归档原话，电脑离线期间待归档的对话会排队。群聊和实时电话独立运行。新接入时从下一轮开始，换绑好友需要重新配对。

## 首次配对

先启动自己电脑上的 Serein，确认记忆核心和网页可用。以下示例在**存有本项目源码的电脑**执行，需要 Python 3.10 或更高版本和能管理该 VPS 的 SSH 登录。把 `user@your-vps` 换成自己的 SSH 地址，把 Serein 路径换成本机实际安装路径。

在项目目录创建私有配对文件。VPS 登录若以 root 身份运行，可以省去 sudo。

~~~sh
umask 077
mkdir -p "$HOME/.config/wechat-voice-bridge"
ssh -T user@your-vps 'sudo -n systemd-run --quiet --wait --pipe --collect -p DynamicUser=yes -p User=wechat-vps-panel -p StateDirectory=wechat-vps-panel -p UMask=0077 -- /usr/bin/python3 /opt/wechat-vps-case/panel/serein_memory.py pair http://127.0.0.1:28229' > "$HOME/.config/wechat-voice-bridge/serein-link.json"
~~~

命令会选用工作台当下绑定的好友并登记连接地址。配对文件含此私聊的专用凭据，应保存在自己的电脑上。Serein 自己的 API 密钥由下一条命令在本机读取。

~~~sh
python3 tools/serein_relay.py \
  --pair "$HOME/.config/wechat-voice-bridge/serein-link.json" \
  --core http://127.0.0.1:28218 \
  --core-token-file "$HOME/apps/serein/deploy/secrets/api-token" \
  --port 28229
~~~

打开另一个终端，从电脑连到 VPS，把 VPS 本机的 28229 转发到刚启动的连接器。SSH 服务需允许回环转发，默认的 `GatewayPorts no` 可保留回环监听。

~~~sh
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -R 127.0.0.1:28229:127.0.0.1:28229 user@your-vps
~~~

在 VPS 用 `ss -ltn 'sport = :28229'` 确认只有 `127.0.0.1:28229`。在工作台打开「外接记忆」，点「测试已保存的连接」。电脑端的两个进程和 Serein 需要常驻。电脑休眠时微信服务继续回复，待归档轮数可在工作台查看。清空工作台的连接地址并保存即可暂停读写。

## 对话如何进入记忆

召回使用 Serein 的 Hook。每次只查询绑定私聊的当前输入，最近五次成功提交给聊天模型的记忆编号参与去重。原话走 Serein 的归档接口，每条消息有独立编号和时间，重复提交会收到相同编号。待发送草稿、朗读指令、模型输入里的召回材料均不作为聊天原话保存。图片以消息类型留痕，语音条保存转写。

已归档的原话可在 Serein 查看。原话何时整理为可召回的 Event，取决于该 Serein 实例的自动摘要开关、模型配置与整理进度。对应功能见 [Serein 的自动摘要说明](https://github.com/Yinglianchun/Serein/blob/main/docs/automatic-events.md)。
