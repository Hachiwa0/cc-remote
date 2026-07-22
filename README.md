# cc-remote

<p align="center"><strong>把你机器上的 Claude Code / Codex，带到手机和任意浏览器。</strong></p>
<p align="center">自托管 · 双引擎 · 多会话 · 实时过程 · 响应式 Web</p>
<p align="center">
  <a href="README_en.md">English</a> ·
  <a href="#本地快速开始一台机器5-分钟">5 分钟上手</a> ·
  <a href="#生产部署公网-vps-中继--你机器上的-wrapper">生产部署</a> ·
  <a href="#安全须知务必读">安全须知</a>
</p>

cc-remote 是一个开源的远程控制面：本机 `wrapper` 驱动已经安装并登录的
`claude` / `codex`，浏览器通过你自托管的 WebSocket 中继查看和控制会话。
模型、认证与工具执行仍由本地 CLI 决定；cc-remote 不代理模型 API，也不会把
API key 烤进网页。

<p align="center">
  <img src="assets/readme-claude-multisession.jpg" alt="cc-remote 的 Claude 会话与多会话工作台" width="960">
</p>

---

## 目录

- [核心能力](#核心能力)
- [架构](#架构)
- [真实界面与实用功能](#真实界面与实用功能)
- [本地快速开始（一台机器，5 分钟）](#本地快速开始一台机器5-分钟)
- [生产部署（公网 VPS 中继 + 你机器上的 wrapper）](#生产部署公网-vps-中继--你机器上的-wrapper)
- [环境变量](#环境变量)
- [鉴权模型](#鉴权模型)
- [可靠性边界](#可靠性边界)
- [安全须知（务必读）](#安全须知务必读)
- [模型后端（可选）](#模型后端可选)
- [开发](#开发)
- [FAQ](#faq)
- [许可](#许可)

---

## 核心能力

| 场景 | 可以做什么 |
|---|---|
| **双引擎** | 在同一个 Web UI 中使用 Claude Code 和 Codex；每个会话保持自己的模型、思考强度、权限与运行状态。 |
| **Code / Work 双空间** | Code 继续面向代码仓库；Work 是完全独立的 Cowork 工作区，用于文档、表格、演示、资料整理和临时协作，不混入代码会话列表。 |
| **Work 项目与资料库** | 为 Claude/Codex 分别建立私有项目、文件/链接/笔记资料库和可复用工作模板；创建工作时会把选定上下文物化到专属目录。 |
| **Work 定时任务与隔离** | 支持一次、每日、每周任务；执行记录、租约、失败重试和防重叠状态均持久化。每个工作默认只能访问自己的私有目录，需要的资料通过会话附件或项目资料库显式加入。 |
| **远程操作** | 手机、平板或桌面浏览器实时看流式回复，发送附件，排队下一条消息，随时打断当前回合。 |
| **完整过程** | 折叠展示引擎公开提供的 reasoning 摘要、计划、命令输出、文件 diff、MCP、协作代理、Hook 和终端交互事件。 |
| **Artifacts 与文件预览** | Work 自动列出当前工作产生的文件；源码可定位行号，Markdown 可预览和冲突安全编辑，HTML 在隔离 iframe 中渲染，图片/PDF 可直接查看，DOCX/XLSX/PPTX 由 wrapper 本机沙箱临时转换后预览。 |
| **人工确认** | 回传 Claude `can_use_tool`，以及 Codex 命令、文件修改、用户输入、通用权限和 MCP elicitation；终端占用时可只读镜像，也可由用户主动接管。 |
| **会话管理** | 搜索、切换、重命名、归档、删除和消息级派生；Codex 支持对话与冲突安全的代码回滚、主动 compact、原生 Review，以及在 Git 仓库中派生到独立 worktree。 |
| **运行控制** | 切换模型、思考强度、服务档位、权限和 Plan 模式；Codex Code 通过 `/permissions` 控制审批并继承本机 Sandbox 配置；`/goal` 管理长目标，`/status` 只读展示 app-server 状态、用量与限额。 |
| **真实扩展目录** | 通过 `/extensions`、`/skills`、`/plugins`、`/apps`、`/mcp`、`/hooks` 按需读取当前引擎目录；Skills 与 Claude Hooks 可安全管理，Codex Hooks 按官方只读能力展示，插件安装/卸载调用原生管理器。 |
| **连续性** | 后台会话继续运行，多端实时同步；浏览器本地投影先绘制，wrapper 从 Claude transcript / Codex rollout 的物化摘要索引分页校验，断线后只按游标补实时尾巴。 |
| **多机器与 PWA** | 一个 relay 可连接多个具名 wrapper；可选账号策略把用户限制到指定机器。网页可安装为 PWA，并在后台回合完成/失败时发送不含对话正文的系统通知。 |
| **自托管** | wrapper 只出站连接；会话、Work 数据和预览转换都留在本机，VPS 只做无状态中继且可替换；网页认证使用 HttpOnly cookie，CLI 凭据与 API key 不进入前端。 |

> 不同引擎可用的模型、服务档位和运行控制以本机 CLI 及其 SDK/app-server 能力为准。

## 架构

两条**互相独立**的链路：

```
模型链路（cc-remote 不碰）:  claude / codex ──(各自本地配置)──▶ 模型服务

控制链路（本仓库）:          浏览器 ⇄ 中继(WebSocket) ⇄ wrapper ⇄ SDK / app-server ⇄ 本地 CLI
```

| 组件 | 跑在哪 | 干什么 |
|---|---|---|
| **wrapper** | `claude` / `codex` 所在的机器 | 持有会话池、把 SDK/app-server 事件翻成线协议、管打断/排空、按需从 transcript/rollout 读历史，并在本机临时转换 Office 预览。**只出站连中继，机器不需要开入站端口。** |
| **relay（中继）** | 公网 VPS（或本地） | 纯 WebSocket 转发器（FastAPI）。每个 `machine_id` 一个 wrapper 槽，浏览器使用 HttpOnly 会话 cookie，并只接收所选机器的事件。**不持久化会话或 Artifact，从不 import `claude-agent-sdk`、从不碰模型 API**。 |
| **web** | 浏览器 | React 客户端；中继同源托管它的静态文件（`web/dist`）。 |

### Code 与 Work

侧栏顶部的 **Code / Work** 开关复用同一套 Claude/Codex 引擎，但两类会话在
存储、列表和权限上彼此隔离：

- **Code** 保持原有行为，以用户选择的代码仓库为工作目录，适合开发、调试和部署。
- **Work** 适合文档、表格、PPT、调研、资料库和临时对话。Claude 数据默认在
  `~/.claude/cc-remote/work`，Codex 数据默认在 `~/.codex/cc-remote/work`；每项工作有
  独立的 `workspace/` 和上传文件。Artifact 是该工作目录中产生的普通文件，删除工作时
  只删除注册表确认属于该 Work 的目录。Work 会替换两家 CLI 面向代码开发的基础提示词；
  闲聊不会主动检查文件或提及代码项目，需要编程时仍可按用户的明确要求执行。
- Work 不开放用户主目录或任意外部目录。需要引用现有资料时，通过附件或项目资料库
  显式复制到私有工作目录，避免对话意外读取其他项目和历史。
- 工作模板是可复用的工作说明/流程模板，会写入该项目的 `WORK.md`，不会在后台执行
  未审核的第三方代码。定时任务由 wrapper 持久化和领取，使用同一 Work 隔离策略运行。

### 原生终端与 Remote 如何协同

Code 会话按两家 CLI 的真实控制面协同，不替换官方命令：

- **Claude：**`claude` 始终还是官方命令和官方 TUI，cc-remote 不创建 alias、shim
  或 PATH 劫持。直接用 `claude`、Claude Desktop 或 Agent View 打开的会话在 Remote
  中实时只读镜像，避免两个输入端同时写入。需要从 Remote 写入时，由用户主动点击接管；
  cc-remote 只向扫描到的精确同用户 Claude 进程发送 SIGTERM，确认释放后再由 SDK 恢复
  同一会话。它不终止终端 Shell、不使用 SIGKILL，也不会静默接管现有进程。
- **Codex Code：**默认通过 Codex 官方共享 app-server daemon 接入，让原生 Codex
  客户端与 Remote 共享 thread 和控制状态；如果本机版本不支持，会明确降级到私有
  app-server。`CC_REMOTE_CODEX_DAEMON=off` 可用于故障排查。
- **Work：**Claude 与 Codex Work 都保持各自的私有进程和目录，不加入 Code 的共享
  控制面，避免工作资料与代码会话互相泄漏。

### Artifact 预览在哪里运行

- HTML 内容在浏览器端经 DOMPurify 清理后进入无脚本、无外部网络的 sandbox iframe。
- PNG/JPEG/GIF/WebP/AVIF 和 PDF 由 wrapper 做路径、类型和大小校验后，通过当前鉴权
  WebSocket 定向返回给请求它的浏览器。
- DOC/DOCX/ODT/RTF、XLS/XLSX/ODS、PPT/PPTX/ODP 由 **wrapper 所在机器**上的
  LibreOffice 转成 PDF；Linux 使用 bubblewrap 隔离网络、用户目录和文件系统，只挂载本次
  临时目录。转换完成后临时目录立即删除。
- VPS relay 只转发有上限的预览帧，不落原文件或转换结果。换 VPS 不需要迁移会话；换
  wrapper 设备时迁移本机 transcript/rollout、Work 根目录和 cc-remote 状态即可。

## 真实界面与实用功能

以下截图来自实际运行中的 cc-remote，不是设计稿。

### 多会话管理：后台继续跑，随时切回来

左侧会话池按工作目录分组，可以搜索、切换、重命名和归档会话；一个会话在后台处理时，仍可进入另一个会话继续工作，切回来即可看到完整实时进度。Claude Code 与 Codex 会话共用同一套工作台，但各自保留独立的上下文、模型、权限和运行状态。

<p align="center">
  <img src="assets/readme-multi-session.jpg" alt="按项目分组并可搜索切换的多会话工作台" width="960">
</p>

### Claude Code：思考、工具调用和 Hook 都能看见

Claude 会话不是一个只显示最终文字的简化聊天框。Remote 会接收 Claude Code SDK 暴露的思考、命令调用、工具结果和 Hook 生命周期，按发生顺序折叠展示；底部同时显示 Claude 当前模型、思考强度、权限模式和上下文占用。

<p align="center">
  <img src="assets/readme-claude-session.jpg" alt="Claude Code 的思考、命令调用和 Hook 处理过程" width="960">
</p>

### 新会话：先选引擎和工作目录

一个入口创建 Claude Code 或 Codex 会话；工作目录可浏览选择，第一条消息可直接带图片或文件。会话建立后再按需要调整模型、权限和 Plan 模式，不用先填写一排默认参数。

<p align="center">
  <img src="assets/readme-new-session.jpg" alt="选择引擎和工作目录并创建新会话" width="960">
</p>

### Codex：计划与处理过程完整保留

Codex 会话把 app-server 提供的 reasoning 摘要、计划、命令、diff、MCP、协作代理与 Hook 组织成可折叠时间线。运行中可以展开追踪细节，完成后收起为一行摘要；最终答复始终独立显示。

<p align="center">
  <img src="assets/readme-process-timeline.jpg" alt="可折叠的计划、Hook 和工具调用处理过程" width="960">
</p>

### Codex 会话级控制：模型、思考、权限与状态

模型、思考强度、服务档位和权限都绑定当前会话；可以在不改本机全局配置的情况下调整下一回合。输入区同时提供附件、排队/打断、上下文占用以及 `/goal`、`/status` 等命令入口。

<p align="center">
  <img src="assets/readme-model-controls.jpg" alt="Codex 模型选择和会话控制" width="960">
</p>

### 常用操作速查

- **会话**：新建、搜索、后台运行、重命名、归档、删除、派生、Codex 对话与代码回滚、compact、Review、Codex worktree。
- **回合**：流式输出、排队、打断、复制、编辑重发、从指定消息派生。
- **工具**：命令输出、文件修改与 diff、MCP、协作代理、Hook、审批和用户输入回传。
- **终端协同**：Codex Code 共享官方 daemon 并支持双向控制；Claude 原生 CLI、Desktop
  和 Agent View 在 Remote 中实时只读镜像，需要写入时由用户明确接管。
- **状态**：模型、思考强度、权限、Plan、上下文、目标、用量、rate limit 和运行告警。
- **扩展**：通过斜杠命令实时查看 Skills、Plugins、Apps、MCP 和 Hooks；安全增删本地 Skills、管理 Claude Hooks，并通过引擎原生管理器安装/卸载插件。Codex Hooks 因官方暂无写接口保持只读。
- **设备**：响应式手机界面、深浅主题、多浏览器/多机器同步、PWA、后台完成提醒与断线重连。

## 本地快速开始（一台机器，5 分钟）

先在 **agent CLI 所在的那台机器**上把中继 + wrapper + 网页都跑起来，验证整条链路。生产部署见下一节。

### 前置

- 一台已完成 **Claude Code** 或支持 `app-server` 的 **Codex CLI** 登录、且 CLI **本身已经能正常对话**的机器；Claude 默认使用锁定 SDK 自带并经过回归的官方 CLI，Codex 每次新建 app-server 时会重新选择本机最新可用版本。两个都可用即可在网页中切换引擎。
- **Python 3.10+**、**Node 20.19+**（用来构建网页）。
- 可选：要预览 DOCX/XLSX/PPTX 等 Office 文件，Linux wrapper 主机需安装
  **LibreOffice + bubblewrap**（例如 `sudo apt install libreoffice bubblewrap`）；VPS 不需要。

### 1）装依赖 + 构建网页

```bash
git clone https://github.com/muggle-stack/cc-remote.git && cd cc-remote

python3 -m venv .venv && source .venv/bin/activate
pip install --require-hashes --only-binary=:all: -r requirements.lock

npm --prefix web ci
npm --prefix web run build          # 产出 web/dist/
```

### 2）配置

```bash
install -m 600 .env.example .env
```

编辑 `.env`，至少改这几项：

```ini
# 网页登录口令（自己定一个强口令）
LOGIN_PASSWORD=<一个强口令>
# 给会话 token 签名用的密钥
SESSION_SECRET=<openssl rand -hex 32>
# wrapper ⇄ relay 的共享 token
WRAPPER_TOKEN=<openssl rand -hex 32>
# 浏览器访问中继时的精确来源；本地 HTTP 只允许 loopback
PUBLIC_ORIGIN=http://127.0.0.1:8765
# 让中继同源托管网页
WEB_STATIC_DIR=web/dist
# agent 会话的默认工作目录（你要让它操作的项目目录）
CC_CWD=/path/to/your/project
# systemd/PATH 找不到 Claude CLI 时可显式指定；否则留空
CLAUDE_BIN=/absolute/path/to/claude
```

> 本地 loopback 快速体验时，中继和 wrapper 可读同一个 `.env`；它不适合生产。
> 公网 wrapper 必须按下文使用 root-only `/etc/cc-remote/wrapper.env`，避免
> `bypassPermissions` 模型/工具直接读取控制面密钥。

### 3）跑起来（两个终端）

```bash
# 终端 1：中继（同源提供 网页 + /ws + /api，监听 http://127.0.0.1:8765）
python -m cc_remote.relay

# 终端 2：wrapper（驱动本地 claude / codex）
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

- **VPS**：Ubuntu 22.04+ / Debian 12+（或其他自带 Python 3.10+ 的 Debian 系发行版），放行 **80 + 443**（80 给 Let's Encrypt 验证，443 给 wss）。
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
npm --prefix web ci
npm --prefix web run build   # 产出 web/dist/
```

> 现在网页**不再把 token 烤进 JS**：登录改为向中继 POST 口令换取短期会话 token。所以构建不需要任何 `VITE_*` 变量。

> **升级到协议 v16**：线协议会严格拒绝版本不一致。请在同一次维护窗口部署
> `cc_remote/` 和新的 `web/dist/`，然后依次重启 relay、wrapper；不要新旧版本滚动混跑。
> 升级期间已有 WebSocket 会短暂重连，relay 重启也会要求浏览器重新登录。已打开的
> 旧版页面必须做一次**硬刷新**（重新加载新的带 hash 静态资源），仅重新登录不够。
> 手工发布时先停本机 wrapper，再停服更新 relay + web，最后启动 v16 relay 和
> v16 wrapper；这样旧 wrapper 不会占住同一 `machine_id` 的连接槽。

### 3）上传 staging，由原子 release 安装器发布

```bash
# dev 机器：普通账号只写自己的 staging，不直接写 root-owned /opt
rsync -av --delete --exclude='.git' --exclude='.venv' \
  --exclude='web/node_modules' --exclude='.env' \
  ./ <vps-user>@<vps>:~/cc-remote-upload/

# VPS：不要把 staging 覆盖到正在运行的 /opt 正式目录
ssh <vps-user>@<vps>
sudo mkdir -p /opt/cc-remote
```

安装器会把 staging 复制到新的
`/opt/cc-remote/releases/release-*`，在其中构建独立 venv，全部校验通过后再原子切换
`/opt/cc-remote/current`。旧 release 的代码、`web/dist` 和 venv 会完整保留用于失败回滚；
不会再对脏的正式目录执行 `rsync --delete`。

### 4）VPS：配 `.env` + 一键 setup

```bash
# 在 VPS 上：.env 是 releases 之外唯一共享的运行配置
sudo test -f /opt/cc-remote/.env || sudo install -m 600 \
  ~/cc-remote-upload/deploy/env.relay.example /opt/cc-remote/.env
sudoedit /opt/cc-remote/.env
# 填 LOGIN_PASSWORD / SESSION_SECRET / WRAPPER_TOKEN；保持：
# WEB_STATIC_DIR=/opt/cc-remote/current/web/dist

# 升级时先停本机 wrapper，随后让安装器一次切换 relay + web
sudo bash ~/cc-remote-upload/deploy/setup-vps.sh \
  your-domain.com ~/cc-remote-upload
```

脚本会：装 `python3-venv` + Caddy、建 `ccremote` 系统用户、创建不可变 release
和 release-local venv、合并 Caddy 配置、原子切换 `current`，再重启 relay。若新
relay 重启或健康检查失败，`current`、Caddyfile、systemd unit 会作为一个事务全部
恢复，并验证旧 release 的 `/healthz`。成功后再启动 v16 wrapper。

验证：

```bash
curl https://your-domain.com/healthz
# 期望：{"ok":true,"wrapper_connected":false,"clients":0}
```

### 5）你的机器：配 root-only wrapper 环境 + systemd

如果需要 Office Artifact 预览，先在这台 wrapper 主机安装转换沙箱（不要装到 VPS）：

```bash
sudo apt-get update && sudo apt-get install -y libreoffice bubblewrap
```

```bash
cd /path/to/cc-remote
python3 -m venv .venv
.venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock

# 密钥源由 root 持有；模型/工具使用你的普通用户运行，不能直接读取该文件。
sudo install -d -o root -g root -m 0755 /etc/cc-remote
sudo install -o root -g root -m 0600 deploy/env.wrapper.example \
  /etc/cc-remote/wrapper.env
sudoedit /etc/cc-remote/wrapper.env  # 填 RELAY_URL / WRAPPER_TOKEN / CC_CWD

# 装 systemd 服务（先编辑 User、仓库/venv/home 路径；不要改回仓库 .env）
sudo cp deploy/cc-remote-wrapper.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cc-remote-wrapper
journalctl -u cc-remote-wrapper -f     # 期望：connected to relay / wrapper running
```

回 VPS 再看 `curl https://your-domain.com/healthz` → 应 `wrapper_connected:true`。

#### 用设备中心配对 Mac / Linux（推荐）

登录网页，点顶部的设备图标，选择“允许添加设备”。页面会生成一个只使用一次、
默认 10 分钟过期的配对码和命令。在新机器的 cc-remote 仓库中执行：

```bash
python -m cc_remote.device pair https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name "MacBook Pro"
python -m cc_remote.wrapper
```

交互运行时，凭据会以 `0600` 保存到 `~/.cc-remote/device.json`。Linux systemd
部署建议直接写入 root-only EnvironmentFile，再重启服务：

```bash
sudo .venv/bin/python -m cc_remote.device pair \
  https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name nono --env-file /etc/cc-remote/device.env
sudo systemctl restart cc-remote-wrapper
```

Relay 只保存设备凭据的哈希；配对成功后明文凭据不会再次显示。设备中心可查看
在线/离线状态、切换机器、重命名或单独撤销设备。旧的 `WRAPPER_TOKEN` /
`WRAPPER_TOKENS_JSON` 手工配置方式仍保持兼容。

### 6）手机验证

手机浏览器（任意网络）开 `https://your-domain.com/` → 用 `LOGIN_PASSWORD` 登录 → 发消息，应看到流式回复 + 可打断 + 多端同步。

### 公司/内网走 HTTP 代理出网？

wrapper 用 `websockets` 出站，认 `HTTPS_PROXY` / `ALL_PROXY` 环境变量。在
`/etc/cc-remote/wrapper.env` 加：

```ini
HTTPS_PROXY=http://your-proxy:port      # SOCKS 用 ALL_PROXY=socks5://...
```

（若代理做 TLS 中间人，需把它的根证书加进系统信任。）

## 环境变量

**中继（relay）**

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_HOST` / `RELAY_PORT` | `127.0.0.1` / `8765` | 监听地址（公网部署交给 Caddy，保持 127.0.0.1）。 |
| `LOGIN_PASSWORD` | 空 | 单用户网页登录口令。未设置 `LOGIN_USERS_JSON` 时**必须设**。 |
| `LOGIN_USERS_JSON` | 空 | 可选多用户策略：`{"alice":{"password":"…","machines":["mac","nono"]}}`；设置后替代 `LOGIN_PASSWORD`。 |
| `SESSION_SECRET` | 空 | 给会话 token 签名的 HMAC 密钥。**必须设**（`openssl rand -hex 32`）。 |
| `SESSION_TTL_SECONDS` | `604800` | 会话 token 有效期（默认 7 天）。 |
| `LOGIN_BODY_MAX_BYTES` / `LOGIN_READ_TIMEOUT` / `LOGIN_INFLIGHT_CAP` | `4096` / `10` / `32` | 登录请求体字节数、总读取秒数和并发读取数的硬上限。 |
| `SESSION_REGISTRY_CAP` | `1024` | 进程内可撤销浏览器会话注册表的硬上限。 |
| `PUSH_VAPID_PUBLIC_KEY` / `PUSH_VAPID_PRIVATE_KEY` / `PUSH_VAPID_SUBJECT` | 空 | 可选真实 Web Push；三项必须同时配置。私钥建议填写 relay 用户可读的 PEM 绝对路径。通知只含完成/失败状态，不含对话内容。 |
| `PUSH_DB_PATH` | `~/.cc-remote/relay-push.sqlite3` | 持久化、按用户和机器隔离的浏览器 Push 订阅库。 |
| `DEVICE_DB_PATH` | `~/.cc-remote/relay-devices.sqlite3` | 持久设备注册、显示名、最近在线时间和凭据哈希；不保存会话或 Artifact。 |
| `DEVICE_PAIRING_TTL_SECONDS` | `600` | 一次性配对码有效秒数，允许 60–3600。 |
| `PUBLIC_ORIGIN` | 空 | 浏览器允许连接 WS 的精确来源，如 `https://remote.example.com`；**必须设**，非 loopback 必须 HTTPS。 |
| `WRAPPER_TOKEN` | 占位值 | 单机器/兼容模式下的 wrapper Bearer token；未设置 `WRAPPER_TOKENS_JSON` 时必须配置。 |
| `WRAPPER_TOKENS_JSON` | 空 | 可选机器绑定 token：`{"mac":"…","nono":"…"}`；设置后替代 relay 的通配 `WRAPPER_TOKEN`。 |
| `WEB_STATIC_DIR` | 空 | 指向 `web/dist` 则同源托管网页；留空则只做 API/WS。 |
| `CLIENT_QUEUE_CAP` / `CLIENT_QUEUE_BYTES` | `4096` / `16777216` | 单客户端待发帧数/字节硬上限；超限断开慢客户端，不静默丢帧。 |
| `MAX_CLIENTS` / `CLIENT_HELLO_TIMEOUT` | `8` / `10` | 已接受客户端总数和首个 Hello 帧等待秒数的硬上限。 |
| `WS_MAX_SIZE_BYTES` | `16777216` | relay 与 wrapper 接受的单个 WebSocket 帧上限。 |

**wrapper**

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_URL` | `ws://127.0.0.1:8765/ws` | 中继的 WebSocket 地址（公网用 `wss://域名/ws`）。 |
| `WRAPPER_TOKEN` | `change-me-wrapper` | 同中继。 |
| `CC_REMOTE_MACHINE_ID` | `default` | 多机器 relay 中的稳定路由 id；使用 `WRAPPER_TOKENS_JSON` 时必须匹配对应键。 |
| `CC_REMOTE_DEVICE_CONFIG` | `~/.cc-remote/device.json` | 交互配对凭据路径；文件必须仅当前用户可读。显式的 `RELAY_URL` / `WRAPPER_TOKEN` / `CC_REMOTE_MACHINE_ID` 优先。 |
| `CLAUDE_BIN` | 空 | 可选的 Claude CLI 绝对路径；systemd/PATH 找不到 `claude` 时设置。 |
| `CC_REMOTE_CODEX_PROXY` | 空 | 仅注入 wrapper 启动的 Codex 子进程的 HTTP(S)/SOCKS5 代理；不改 wrapper 到 relay 的连接，也不影响用户终端里的 `codex`。例如 nono 可填 `http://127.0.0.1:7897`。 |
| `CC_REMOTE_CODEX_DAEMON` | `auto` | Code 默认连接 Codex 官方共享 daemon；`off` 强制使用私有 stdio app-server。Work 始终私有，不受此项影响。 |
| `CC_CWD` | 当前目录 | 新会话默认工作目录。Claude `--resume` 靠它定位 `~/.claude/projects/` 下的会话文件，**必须对**；Codex 恢复时会优先从 rollout 取原 cwd。 |
| `CC_RESUME_SESSION_ID` | 空 | 恢复指定会话 UUID；留空开新会话。首次启动后 id 会持久化到 `~/.cc-remote/`。 |
| `CLAUDE_WORK_ROOT` | `~/.claude/cc-remote/work` | Claude Work 的私有注册表、资料库、会话目录和策略文件根目录。 |
| `CODEX_WORK_ROOT` | `~/.codex/cc-remote/work` | Codex Work 的私有注册表、资料库、会话目录和策略文件根目录。 |
| `MAX_CONCURRENT_SESSIONS` | `20` | 常驻 agent 子进程上限（内存随引擎/版本变化）。超了就驱逐 idle 的；客户端缓存仍在，可再切回。 |
| `DRAIN_TIMEOUT` | `15` | interrupt 后等终止 ResultMessage 的秒数，超时强制重连（排空保险）。 |
| `CODEX_TURN_IDLE_WARN_SECONDS` | `90` | Codex app-server 连续无事件时显示“仍在等待”提示；`0` 禁用。只提示，不自动打断 ultra 推理或长工具。 |
| `RING_MAX_EVENTS` / `RING_MAX_BYTES` / `TOOL_RESULT_MAX` | 见 `.env.example` | 实时尾巴缓冲 / 工具输出截断上限调优。 |
| `HISTORY_SOURCE_MAX_BYTES` | `67108864` | 单个 Claude transcript 的安全读取上限；超限返回明确错误，避免 SDK transcript 全量解析耗尽内存。Codex rollout 不受此总文件上限限制。 |
| `CODEX_HISTORY_WINDOW_MAX_BYTES` | `33554432` | Codex 超长 rollout 每页最多解析的源窗口；历史按轮次从文件尾流式分页，单轮超限时保留最近窗口和可继续加载的稳定游标。 |
| `WRAPPER_INBOX_CAP` / `WRAPPER_SEND_QUEUE_CAP` | `1024` / `8192` | wrapper 入站/出站内存队列条数硬上限。 |
| `WRAPPER_INBOX_BYTES` / `WRAPPER_SEND_QUEUE_BYTES` | `33554432` / `33554432` | wrapper 入站/出站队列序列化字节硬上限。 |
| `TURN_READER_QUEUE_CAP` | `4` | 单回合 SDK/app-server 读取队列上限；满时向模型流施加背压。 |

单次消息最多 8 个附件，单个最多 6 MiB，解码后合计最多 8 MiB；超限会在启动模型前拒绝。

## 鉴权模型

- **网页客户端**：向中继 `POST /api/login` 换一个短期 HMAC 会话，放在 **HttpOnly + SameSite=Strict** cookie 中；JavaScript 读不到，URL 中也没有 token。配置 `LOGIN_USERS_JSON` 后，签名会话还携带允许的机器集合，机器列表和 WebSocket 路由都会再次校验。WebSocket 同时必须通过精确 `Origin` 校验。
- **wrapper ⇄ 中继**：WS 握手时带机器凭据；手工配置可使用 `WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON`，设备中心则签发独立、机器绑定且可单独撤销的凭据。Relay 只保存哈希，任何凭据都不能声明另一台机器的 `machine_id`。
- token 只走 cookie/请求头，从不进 URL 或线协议消息体；日志会自动打码 token/password 字段。

## 可靠性边界

- Web 与 TUI 会给可重试命令附加稳定的 `cmd_id`，断线重连或 wrapper 恢复后重发；wrapper 在同一进程生命周期内去重并返回 ACK。每个实时会话还用 wrapper generation 配对 cursor，避免 wrapper 重启后把旧序号误当成新序号。
- 未确认命令队列和通用命令去重表是**有界内存状态**：浏览器硬刷新、TUI 退出或 wrapper 进程崩溃，不承诺跨进程的 exactly-once。cc-remote 是交互控制面，不是持久任务队列；这类故障后应先核对 transcript/rollout 和会话状态，再决定是否重发。
- 已落盘的 Claude transcript / Codex rollout 是历史事实来源；wrapper 的 SQLite 摘要索引和浏览器 IndexedDB 都是可重建投影，实时 ring 只负责有界的断线补流。工具/思考等大块详情按单轮展开，不阻塞会话首屏。
- Work 定时任务是例外：计划、运行记录、租约、心跳、重试次数和下次运行时间写入 SQLite；wrapper 重启后会恢复过期租约，但仍不会把不确定结果伪装成成功。

## 安全须知（务必读）

> **cc-remote 会让远端的人在你机器上跑任意命令。请当成「给别人一个你机器的 shell」来对待。**

- Code 会话仍是远程开发控制面：Claude 默认使用 `permissionMode: bypassPermissions`；Codex 默认审批策略是 `never` 并继承本机 Codex sandbox 配置，也可切到 `on-request` / `untrusted`。**能登录且能进入 Code 的人，仍应等同于拿到了这台机器的远程 agent/shell 权限。** Work 会话使用独立私有根目录且不开放外部目录，但这只是缩小默认能力面，不替代操作系统级的独立用户、容器或虚拟机隔离。
- `LOGIN_PASSWORD` / `LOGIN_USERS_JSON`、`WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON` 和 `SESSION_SECRET` 是认证边界：用强随机值、别提交 git、别贴到聊天里、定期轮换。仓库 `.env` 只适合本机开发；生产 wrapper 必须使用上述 root-only `/etc/cc-remote/wrapper.env`。systemd 模板会禁止服务及模型子进程读取这个源文件和遗留仓库 `.env`；Linux wrapper 还会关闭 dumpability，避免子进程从 `/proc/<pid>/environ` 或进程内存取回已经捕获的 token。
- 公网必须上 TLS（`wss://`，本仓库用 Caddy 自动签证书）。别用明文 `ws://` 暴露公网。
- 建议：给中继加 IP 白名单 / 只在需要时开、给登录加失败限速（已内置每 IP 每分钟 5 次）。

## 模型后端（可选）

cc-remote **不碰模型 API**——它只驱动你机器上已经配置好的 CLI：Claude 使用 `~/.claude/settings.json`，Codex 使用自己的登录与 `~/.codex/config.toml`。所以：

- 用**官方 Anthropic API**：装好 `claude` 能对话即可，cc-remote 直接用。
- 用**兼容端点（如 GLM / z.AI）**：照常在 `settings.json` 里设 `ANTHROPIC_BASE_URL`（指向官方兼容端点或你自建的代理），cc-remote 一样只做控制链路。
- 用 **Codex**：先确保本机 `codex` 可正常对话且 `codex app-server` 可启动；cc-remote 不接触其 API key，也不会改写全局认证配置。

## 开发

```
cc_remote/
  protocol.py      # pydantic 线协议（客户端/中继/wrapper 都依赖）
  config.py        # 环境变量配置
  relay/           # FastAPI 中继：server / auth / pairing / forward
  wrapper/         # Claude SDK + Codex app-server / 会话池 / stream / ringbuffer / transport
web/               # React 客户端（Vite + TS）
tests/             # 零 token 单元测试 + 端到端脚本
deploy/            # Caddyfile / systemd / setup-vps.sh / env 示例
```

```bash
python -m pip install -r requirements-dev.txt
pytest                              # 单元测试（不触模型，零 token）
npm --prefix web run test:reliability # 前端可靠性纯测试

# 显式真实链路测试（需要已运行的 relay + wrapper，会调用模型）
CC_REMOTE_RUN_E2E=1 CC_REMOTE_E2E_SCENARIO=smoke \
  RELAY_URL=wss://remote.example/ws LOGIN_PASSWORD='...' \
  pytest -q tests/test_e2e_entry.py
npm --prefix web run lint           # 前端静态检查
npm --prefix web run dev            # 网页开发模式
npm --prefix web run build          # 网页生产构建
```

贡献指南与内部架构约定见 [CLAUDE.md](CLAUDE.md)。

## FAQ

- **wrapper 重启会丢历史吗？** 已落盘历史不会；它来自磁盘上的 Claude transcript / Codex rollout。重启会丢尚未确认的内存命令和实时 ring，详见上面的可靠性边界。
- **中继重启会断吗？** 会短暂断连并要求重新登录（进程内撤销注册表会重置）；对话不丢，因为会话在 wrapper 机器上。
- **可以更换 VPS 或迁移到新设备吗？** 可以。VPS 只提供 relay + Web 静态文件，不是会话权威；更换它只需部署同版本并让 wrapper 指向新的 `RELAY_URL`。迁移 wrapper 设备时，复制 Claude transcript、Codex rollout、`CLAUDE_WORK_ROOT` / `CODEX_WORK_ROOT` 和 `~/.cc-remote`，在新设备重新登录 CLI 后再启动 wrapper。
- **要开入站端口吗？** 不用。wrapper 只出站连中继。
- **多贵？** cc-remote 本身零模型开销；浏览/刷新/看历史都不花 token。真正的模型花费取决于本地 agent CLI 使用的后端。

## 许可

MIT，见 [LICENSE](LICENSE)。
