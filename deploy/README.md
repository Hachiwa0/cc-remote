# 部署到 vps(Phase 3)

把本地跑通的 cc-remote 搬到公网:公司 wrapper 通过 `wss://` 连 vps 上的 relay,手机浏览器也连同一个域名。模型链路(cc → 本地代理 → z.AI GLM)不动,只有控制链路上公网。

```
公司电脑 wrapper ──wss:443──▶ Caddy(vps, 自动 HTTPS) ──▶ relay(127.0.0.1:8765) ◀──wss:443── 手机浏览器
                                                                   └─ serves web/dist(同源)
```

## 前置

- **vps**:Ubuntu/Debian,开放 **80 + 443**(80 给 Let's Encrypt 验证,443 给 wss)。
- **域名**:A 记录指向 vps 公网 IP。Caddy 自动签 Let's Encrypt 证书,手机直接信任。
- **公司电脑**:Linux(本文用 systemd 常驻 wrapper)。确认公司内网**出站 443** 到公网放行(通常都放)。若公司走 HTTP 代理出网,见末尾「公司代理」。

## 1. 生成强 token

```bash
openssl rand -hex 32   # 跑两次,分别做 CLIENT_TOKEN 和 WRAPPER_TOKEN
```

记下这两个值,后面三处要用:vps relay 的 `.env`、web 构建的 `VITE_CLIENT_TOKEN`、公司 wrapper 的 `.env`(只用 WRAPPER_TOKEN)。

## 2. 在 dev 机器构建 web(把 CLIENT_TOKEN 烤进 JS)

```bash
cd /home/youruser/claude-workspace/cc-remote
VITE_CLIENT_TOKEN=<上一步的 CLIENT_TOKEN> npm --prefix web run build
```

产物在 `web/dist/`。

> 注:token 烤进 JS,任何能打开页面的人都能提取。强 token 是第一道门,后续 Phase 3 会换成登录签发的短期 token。

## 3. 把代码 + dist 拷到 vps

```bash
# 在 dev 机器(把 <vps> 换成你的 vps 用户@IP)
rsync -av --exclude='.venv' --exclude='web/node_modules' --exclude='__pycache__' \
  /home/youruser/claude-workspace/cc-remote/ <vps>:/opt/cc-remote/
```

确保 vps 上有:`/opt/cc-remote/cc_remote/`、`/opt/cc-remote/web/dist/`、`/opt/cc-remote/requirements.txt`、`/opt/cc-remote/deploy/`。

## 4. vps:配 .env + 跑 setup

```bash
# 在 vps 上
cd /opt/cc-remote
cp deploy/env.relay.example .env
# 编辑 .env:填 CLIENT_TOKEN、WRAPPER_TOKEN(用第 1 步的值)
nano .env

# 一键装依赖 + Caddy + systemd(把域名换成你的)
sudo bash deploy/setup-vps.sh cc-remote.example.com
```

脚本做:装 python3-venv + caddy、建 ccremote 用户、venv + pip install、写 Caddyfile、起 `cc-remote-relay` + `caddy` 服务。

验证:
```bash
curl https://cc-remote.example.com/healthz
# 期望:{"ok":true,"wrapper_connected":false,"clients":0}
```

## 5. 公司电脑:配 wrapper .env + systemd

```bash
cd /home/youruser/claude-workspace/cc-remote
cp deploy/env.wrapper.example .env
# 编辑 .env:RELAY_URL=wss://你的域名/ws,WRAPPER_TOKEN(同 vps 的),CC_CWD
nano .env

# python venv(若还没有)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 装 systemd 服务
sudo cp deploy/cc-remote-wrapper.service /etc/systemd/system/
# 若你的用户名/路径不是 youruser,编辑 service 文件改 User / WorkingDirectory / EnvironmentFile
sudo systemctl daemon-reload
sudo systemctl enable --now cc-remote-wrapper

# 看 wrapper 是否连上 relay
journalctl -u cc-remote-wrapper -f
# 期望:connected to relay, wrapper running
```

再回 vps 看:
```bash
curl https://cc-remote.example.com/healthz
# 期望:wrapper_connected:true
```

## 6. 手机验证

手机浏览器(任意网络)打开 `https://cc-remote.example.com/`,发条消息 → 应看到流式回复 + 打断可用 + 多端同步。

## 公司代理(若公司走 HTTP 代理出网)

wrapper 用 `websockets` 连出,支持 `HTTPS_PROXY`/`ALL_PROXY` 环境变量。在公司 wrapper 的 `.env` 加:
```
HTTPS_PROXY=http://公司代理:端口
```
(若代理是 SOCKS,用 `ALL_PROXY=socks5://...`)。websockets 库会自动用。若公司代理做 TLS 中间人(MITM),需要把它的根证书加入信任。

## 安全提示

- **强 token 是唯一的门**:relay 暴露公网,持有 CLIENT_TOKEN 就能看/控 cc。token 别提交 git,别贴聊天。`.env` 在 `.gitignore` 里。
- **bypassPermissions**:cc 在公司电脑跑任意 shell/编辑无提示。你能连到 relay 的人 = 能在你公司电脑执行命令的人。务必用强 token,别公开域名。
- **relay 无状态**:relay 重启不丢对话(wrapper 的 buffer 在公司电脑,relay 重启后 wrapper 自动重连 + client replay)。但 **wrapper 重启会丢内存 buffer**(cc session 在,但 buffer 空,新客户端看不到 wrapper 重启前的历史)。持久化 buffer 是 Phase 3 剩余项。

## Phase 3 剩余(可选,后续)

- 持久化 ring buffer(SQLite/JSONL),wrapper 重启不丢历史。
- 登录端点签发短期 HMAC token,替换烤进 JS 的静态 CLIENT_TOKEN。
- 全量 tool_result HTTP 拉取端点(truncated 输出)。
- Capacitor 把 web/dist 包成真 APK。
- 可观测性:`/healthz` 已有,加指标(turn 数、drain timeout、重连次数)。
