# cc-remote

用手机 / 浏览器远程遥控你机器上的 **Claude Code** —— 自托管、开源。

一台机器上跑着的 `claude`（Claude Code）会话，通过部署在 VPS 上的一个 WebSocket 中继，从手机或任意浏览器实时遥控：**流式输出、随时打断、多端同步、多会话切换、历史按需秒开**。

> 灵感来自 Claude Code 官方的 Remote Control，但完全自托管、不依赖 claude.ai 订阅；而且**模型后端随你**——官方 Anthropic API，或任何 Anthropic 兼容端点（比如自建的 GLM / z.AI 代理）。cc-remote 本身**从不碰模型 API**，只做「控制」这条链路。

**English:** [README_en.md](README_en.md)

---

## 目录

- [它能干什么](#它能干什么)
- [架构](#架构)
- [本地快速开始（一台机器，5 分钟）](#本地快速开始一台机器5-分钟)
- [生产部署（公网 VPS 中继 + 你机器上的 wrapper）](#生产部署公网-vps-中继--你机器上的-wrapper)
- [环境变量](#环境变量)
- [鉴权模型](#鉴权模型)
- [安全须知（务必读）](#安全须知务必读)
- [模型后端（可选）](#模型后端可选)
- [开发](#开发)
- [FAQ](#faq)
- [许可](#许可)

---

## 它能干什么

- 📱 **手机/浏览器实时遥控**：在外面用手机就能驱动家里/公司机器上的 Claude Code，流式看它敲字、跑工具。
- ⏹️ **随时打断**：一键中断当前回合（正确处理了 SDK 的 drain 语义，不会串台）。
- 🔀 **多会话**：常驻会话池，侧栏切换；后台会话继续跑，状态点实时更新。
- 🕘 **历史秒开**：历史按需从 transcript 整包读取（像各家 web chat 一样），刷新不卡、不重放洪流。
- 🔗 **多端同步**：多个设备连同一个中继，看到同一份对话。
- 🔒 **自托管**：中继是纯 WebSocket 转发器，不碰模型；你的代码/密钥都在自己机器上。

## 架构

两条**互相独立**的链路：

```
模型链路（cc-remote 不碰）:  claude ──(~/.claude/settings.json)──▶ Anthropic API 或你的兼容端点

控制链路（本仓库）:          浏览器 ⇄ 中继(WebSocket) ⇄ wrapper ⇄ claude-agent-sdk ⇄ claude
```

| 组件 | 跑在哪 | 干什么 |
|---|---|---|
| **wrapper** | `claude` 所在的机器 | 持有会话池、把 SDK 事件翻成线协议、管打断/排空、按需从 transcript 读历史。**只出站连中继，机器不需要开入站端口。** |
| **relay（中继）** | 公网 VPS（或本地） | 纯 WebSocket 转发器（FastAPI）。Bearer token 鉴权、单 wrapper 槽、多客户端扇出。**从不 import `claude-agent-sdk`、从不碰模型 API**，所以能安全放公网。 |
| **web** | 浏览器 | React 客户端；中继同源托管它的静态文件（`web/dist`）。 |

## 本地快速开始（一台机器，5 分钟）

先在 **`claude` 所在的那台机器**上把中继 + wrapper + 网页都跑起来，验证整条链路。生产部署见下一节。

### 前置

- 一台已装好 **Claude Code CLI**（`claude`，v2.1.51+）并且**本身已经能正常对话**的机器（不管你用官方 API 还是自建代理，只要 `claude` 跑得通）。
- **Python 3.10+**、**Node 18+**（用来构建网页）。

### 1）装依赖 + 构建网页

```bash
git clone <this-repo> cc-remote && cd cc-remote

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

npm --prefix web install
npm --prefix web run build          # 产出 web/dist/
```

### 2）配置

```bash
cp .env.example .env
```

编辑 `.env`，至少改这几项：

```ini
# 网页登录口令（自己定一个强口令）
LOGIN_PASSWORD=<一个强口令>
# 给会话 token 签名用的密钥
SESSION_SECRET=<openssl rand -hex 32>
# wrapper ⇄ relay 的共享 token
WRAPPER_TOKEN=<openssl rand -hex 32>
# 让中继同源托管网页
WEB_STATIC_DIR=web/dist
# claude 会话的工作目录（你要让它操作的项目目录）
CC_CWD=/path/to/your/project
```

> 本地单机时，中继和 wrapper 读同一个 `.env` 即可。

### 3）跑起来（两个终端）

```bash
# 终端 1：中继（同源提供 网页 + /ws + /api，监听 http://127.0.0.1:8765）
python -m cc_remote.relay

# 终端 2：wrapper（驱动 claude）
python -m cc_remote.wrapper
```

### 4）打开网页

浏览器开 **http://127.0.0.1:8765** → 用 `LOGIN_PASSWORD` 登录 → 发条消息，应能看到流式回复、可打断、可多会话切换。

> 想改网页代码时用开发模式：`npm --prefix web run dev`（Vite）。生产/联调直接用上面的 `build` + 中继同源托管更简单。

## 生产部署（公网 VPS 中继 + 你机器上的 wrapper）

把中继搬到公网，wrapper 从你的机器**出站** `wss://` 连它，手机浏览器连同一个域名。模型链路完全不动。

```
你的机器 wrapper ──wss:443──▶ Caddy(VPS, 自动 HTTPS) ──▶ relay(127.0.0.1:8765) ◀──wss:443── 手机浏览器
                                                              └─ 同源托管 web/dist
```

### 前置

- **VPS**：Ubuntu/Debian，放行 **80 + 443**（80 给 Let's Encrypt 验证，443 给 wss）。
- **域名**：A 记录指向 VPS 公网 IP（Caddy 自动签 + 续 Let's Encrypt 证书）。
- **你的机器**：Linux（下面用 systemd 常驻 wrapper），出站 443 到公网放行。

### 1）生成 token / 口令

```bash
openssl rand -hex 32   # WRAPPER_TOKEN（relay 与 wrapper 两边要一致）
openssl rand -hex 32   # SESSION_SECRET（relay 用）
# 再想一个 LOGIN_PASSWORD（网页登录口令）
```

### 2）在 dev 机器构建网页

```bash
npm --prefix web install && npm --prefix web run build   # 产出 web/dist/
```

> 现在网页**不再把 token 烤进 JS**：登录改为向中继 POST 口令换取短期会话 token。所以构建不需要任何 `VITE_*` 变量。

### 3）把代码 + dist 拷到 VPS

```bash
rsync -av --exclude='.venv' --exclude='web/node_modules' --exclude='.env' \
  ./ <vps-user>@<vps>:/opt/cc-remote/
```

确保 VPS 上有：`/opt/cc-remote/cc_remote/`、`/opt/cc-remote/web/dist/`、`/opt/cc-remote/requirements.txt`、`/opt/cc-remote/deploy/`。

### 4）VPS：配 `.env` + 一键 setup

```bash
# 在 VPS 上
cd /opt/cc-remote
cp deploy/env.relay.example .env
nano .env        # 填 LOGIN_PASSWORD / SESSION_SECRET / WRAPPER_TOKEN

# 装依赖 + Caddy + systemd（把域名换成你的）
sudo bash deploy/setup-vps.sh your-domain.com
```

脚本会：装 `python3-venv` + Caddy、建 `ccremote` 系统用户、建 venv + `pip install`、写 Caddyfile、起 `cc-remote-relay` + `caddy` 服务。

验证：

```bash
curl https://your-domain.com/healthz
# 期望：{"ok":true,"wrapper_connected":false,"clients":0}
```

### 5）你的机器：配 wrapper `.env` + systemd

```bash
cd /path/to/cc-remote
cp deploy/env.wrapper.example .env
nano .env        # RELAY_URL=wss://your-domain.com/ws、WRAPPER_TOKEN（同 VPS）、CC_CWD

python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 装 systemd 服务（先编辑文件里的 User / 路径为你自己的）
sudo cp deploy/cc-remote-wrapper.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cc-remote-wrapper
journalctl -u cc-remote-wrapper -f     # 期望：connected to relay / wrapper running
```

回 VPS 再看 `curl https://your-domain.com/healthz` → 应 `wrapper_connected:true`。

### 6）手机验证

手机浏览器（任意网络）开 `https://your-domain.com/` → 用 `LOGIN_PASSWORD` 登录 → 发消息，应看到流式回复 + 可打断 + 多端同步。

### 公司/内网走 HTTP 代理出网？

wrapper 用 `websockets` 出站，认 `HTTPS_PROXY` / `ALL_PROXY` 环境变量。在 wrapper 的 `.env` 加：

```ini
HTTPS_PROXY=http://your-proxy:port      # SOCKS 用 ALL_PROXY=socks5://...
```

（若代理做 TLS 中间人，需把它的根证书加进系统信任。）

## 环境变量

**中继（relay）**

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_HOST` / `RELAY_PORT` | `127.0.0.1` / `8765` | 监听地址（公网部署交给 Caddy，保持 127.0.0.1）。 |
| `LOGIN_PASSWORD` | 空 | 网页登录口令。**必须设**，否则没法登录。 |
| `SESSION_SECRET` | 空 | 给会话 token 签名的 HMAC 密钥。**必须设**（`openssl rand -hex 32`）。 |
| `SESSION_TTL_SECONDS` | `604800` | 会话 token 有效期（默认 7 天）。 |
| `WRAPPER_TOKEN` | `change-me-wrapper` | wrapper 连中继时的 Bearer token，两边必须一致。 |
| `WEB_STATIC_DIR` | 空 | 指向 `web/dist` 则同源托管网页；留空则只做 API/WS。 |
| `CLIENT_TOKEN` | *(legacy)* | 旧的静态客户端 token，网页已不用，可忽略。 |

**wrapper**

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_URL` | `ws://127.0.0.1:8765/ws` | 中继的 WebSocket 地址（公网用 `wss://域名/ws`）。 |
| `WRAPPER_TOKEN` | `change-me-wrapper` | 同中继。 |
| `CC_CWD` | 当前目录 | claude 会话的工作目录。`--resume` 靠它定位 `~/.claude/projects/` 下的会话文件，**必须对**。 |
| `CC_RESUME_SESSION_ID` | 空 | 恢复指定会话 UUID；留空开新会话。首次启动后 id 会持久化到 `~/.cc-remote/`。 |
| `MAX_CONCURRENT_SESSIONS` | `20` | 常驻 cc 子进程上限（每个 ~190MB）。超了就驱逐 idle 的（客户端缓存还在，切回瞬开）。 |
| `DRAIN_TIMEOUT` | `15` | interrupt 后等终止 ResultMessage 的秒数，超时强制重连（排空保险）。 |
| `RING_MAX_EVENTS` / `RING_MAX_BYTES` / `TOOL_RESULT_MAX` | 见 `.env.example` | 实时尾巴缓冲 / 工具输出截断上限调优。 |

## 鉴权模型

- **网页客户端**：向中继 `POST /api/login`（带 `LOGIN_PASSWORD`）换一个短期 **HMAC 会话 token**（存 localStorage），之后用它连 `/ws`。token 不烤进 JS。
- **wrapper ⇄ 中继**：WS 握手时带 `Authorization: Bearer <WRAPPER_TOKEN>`。
- 所有 token 只走请求头，从不进消息体；日志会自动打码 token 字段。

## 安全须知（务必读）

> **cc-remote 会让远端的人在你机器上跑任意命令。请当成「给别人一个你机器的 shell」来对待。**

- 会话以 `permissionMode: bypassPermissions` 跑（无人值守遥控的前提），意味着 agent 能**无提示**执行任意 shell / 改文件。**能连到中继 + 知道登录口令的人 = 能在你机器上执行命令的人。**
- `LOGIN_PASSWORD` / `WRAPPER_TOKEN` / `SESSION_SECRET` 是唯一的门：用强随机值、别提交 git（`.env` 已在 `.gitignore`）、别贴到聊天里、定期轮换。
- 公网必须上 TLS（`wss://`，本仓库用 Caddy 自动签证书）。别用明文 `ws://` 暴露公网。
- 建议：给中继加 IP 白名单 / 只在需要时开、给登录加失败限速（已内置每 IP 每分钟 5 次）。

## 模型后端（可选）

cc-remote **不碰模型 API**——它只驱动你机器上的 `claude` CLI，用的是 `~/.claude/settings.json` 里已经配好的后端。所以：

- 用**官方 Anthropic API**：装好 `claude` 能对话即可，cc-remote 直接用。
- 用**兼容端点（如 GLM / z.AI）**：照常在 `settings.json` 里设 `ANTHROPIC_BASE_URL`（指向官方兼容端点或你自建的代理），cc-remote 一样只做控制链路。

## 开发

```
cc_remote/
  protocol.py      # pydantic 线协议（客户端/中继/wrapper 都依赖）
  config.py        # 环境变量配置
  relay/           # FastAPI 中继：server / auth / pairing / forward
  wrapper/         # sdk / machine(会话池+状态机+排空) / stream(SDK→协议) / ringbuffer / transport
web/               # React 客户端（Vite + TS）
tests/             # 零 token 单元测试 + 端到端脚本
deploy/            # Caddyfile / systemd / setup-vps.sh / env 示例
```

```bash
pytest                              # 单元测试（不触模型，零 token）
npm --prefix web run dev            # 网页开发模式
npm --prefix web run build          # 网页生产构建
```

贡献指南与内部架构约定见 [CLAUDE.md](CLAUDE.md)。

## FAQ

- **wrapper 重启会丢历史吗？** 不会。历史来自磁盘上的 transcript（按需读取），wrapper 重启只丢内存里的「实时尾巴」缓冲，重连后照常。
- **中继重启会断吗？** 会短暂断连，客户端自动重连；对话不丢（wrapper 的会话在你机器上）。
- **要开入站端口吗？** 不用。wrapper 只出站连中继。
- **多贵？** cc-remote 本身零模型开销；浏览/刷新/看历史都不花 token。真正的模型花费取决于你 `claude` 用的后端。

## 许可

MIT，见 [LICENSE](LICENSE)。
