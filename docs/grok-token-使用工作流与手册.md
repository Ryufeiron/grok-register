# Grok Token 免费获取工作流与使用手册

> 本文档描述如何通过本项目的自动化流水线，免费获取 x.ai 账号的 Grok OAuth Token，并作为 OpenAI 兼容 API 使用。整个方案已在 GitHub Actions（美国出口）上实测跑通，成功率约 50%（主要受 Cloudflare Turnstile 随机性影响）。

## 目录

1. [原理与架构](#1-原理与架构)
2. [前置条件](#2-前置条件)
3. [方案 A：GitHub Actions 全自动流水线（推荐）](#3-方案-agitHub-actions-全自动流水线推荐)
4. [方案 B：本机/服务器部署](#4-方案-b本机服务器部署)
5. [Token 使用方法](#5-token-使用方法)
6. [集成到 AI Agent（Claude Code / Codex / OpenCode 等）](#6-集成到-ai-agentclaude-code--codex--opencode-等)
7. [关键配置项说明](#7-关键配置项说明)
8. [常见问题排查](#8-常见问题排查)
9. [风险与合规声明](#9-风险与合规声明)

---

## 1. 原理与架构

```
┌────────────────────────────────────────────────────────────────┐
│  GitHub Actions (ubuntu-latest, 美国出口 IP)                     │
│                                                                │
│  Camoufox 反检测浏览器（模拟真实 Chrome/Firefox 指纹）             │
│    │                                                           │
│    ├─ 1. 打开 accounts.x.ai/sign-up 注册页 (HTTP 200 通过)      │
│    ├─ 2. 点击 "Sign up with email"                              │
│    ├─ 3. 自动创建临时邮箱 (Mail.tm 免注册免费 API)                │
│    ├─ 4. 接收验证码邮件并回填                                   │
│    ├─ 5. 填写随机资料 + 通过 Cloudflare Turnstile 人机验证       │
│    ├─ 6. 提交注册，等待 SSO cookie                              │
│    │    └─ 风控检查 (botFlagSource) → sso_allow_flagged 跳过    │
│    └─ 7. 用 SSO cookie 走 Device Flow 兑换 OAuth Token          │
│         (access_token + refresh_token, 有效期 6 小时)           │
│                                                                │
│  产物: cpa_auth/xai-<邮箱>.json + grok2api_auth/ 系列文件        │
└────────────────────────────────────────────────────────────────┘
          │ 下载 Artifact
          ▼
  本地拿到 Token → 直接调用 cli-chat-proxy.grok.com/v1 (OpenAI 兼容)
```

**关键事实（实测验证）：**

- x.ai 区域封锁**只按出口 IP 的地理国家**判断：中国 IP 直接拒绝（"not available in your region"），美国出口（含数据中心）可正常注册。
- Cloudflare Turnstile 是主要随机变量：纯自动点击约 50% 能拿到 token；用 `register_count >= 4` 提高整批成功率。
- 免费账号有速率限制（约每小时几十次请求），大量使用需多账号轮换。
- Token 走 `cli-chat-proxy.grok.com/v1`（CLI 通道），**不能**用 `api.x.ai/v1`（付费计费通道，会 402）。

## 2. 前置条件

| 项目 | 要求 | 说明 |
|---|---|---|
| GitHub 账号 | 任意免费账号 | 用于 fork 仓库 + 跑 Actions（免费额度 2000 分钟/月） |
| 网络 | 无需梯子 | Actions 运行在 GitHub 机房（美国出口） |
| 本机 | 可选 | 仅取 artifact/token 时用浏览器即可；跑持续服务需 Docker/Node |

> 无需任何付费：邮箱用 Mail.tm 的免费公开 API（免注册，域名 emalupe.com），代理零配置（Actions 直连）。

## 3. 方案 A：GitHub Actions 全自动流水线（推荐）

### 3.1 准备仓库

1. Fork 本项目到你自己的 GitHub 账号：`https://github.com/<原仓库>/grok-register`
2. 上传/确认以下三个关键文件存在（本仓库已内置在分支中）：

| 文件 | 作用 |
|---|---|
| `.github/workflows/run_register.yml` | 主流水线：注册 + 换 token + 打包 artifact |
| `tools/turnstile_probe.py` | Turnstile 点击探针（playwright-captcha 集成） |
| `backend/registration/signup_flow.py` | 点击 Turnstile frame 的 shadow-root 策略 |

### 3.2 按需修改配置

编辑 `.github/workflows/run_register.yml` 中的写配置段（`Write config` 步骤）：

```yaml
- name: Write config
  run: |
    ...
    cfg = {
      "email_provider": "duckmail",          # 邮箱提供商
      "duckmail_api_base": "https://api.mail.tm",  # 免 key 的免费邮箱
      "duckmail_api_key": "",                # 留空
      "proxy": "",                           # Actions 直连，无需代理
      "register_count": 4,                   # 每轮注册几个账号（建议 4，提高 Turnstile 成功率）
      "register_workers": 1,
      "browser_engine": "camoufox",
      "browser_headless": False,
      "cpa_token_mode": "device_protocol",   # Device Flow 换 token
      "sso_allow_flagged": True,             # 关键开关：跳过注册风控拦截，继续换 OAuth token
      "cpa_auto_add": True,                  # 必须为 true，否则不兑换 token
      ...
    }
```

> 没有 `sso_allow_flagged=true`，新注册账号会被 x.ai 风控（botFlagSource=1）拦截，不会产出 token。本项目已内置该开关（engine.py `ensure_sso_oauth_eligible`，commit 5051b93）。

### 3.3 触发运行

- **手动触发**：仓库页面 → Actions → Run Register Probe → **Run workflow**。
- **自动触发**：往 main 分支任意 push（空 commit 也行）。

每次运行约 15~20 分钟（4 个账号，含随机等待）。运行结束后所有步骤应为绿色。

### 3.4 下载 Token（Artifact）

1. 打开运行详情页：`https://github.com/<你的账号>/grok-register/actions/runs/<run-id>`（页面上看最新一次 run）。
2. 页面底部 **Artifacts** 区域 → 点击 **register-artifacts** 自动下载 zip（需登录；没有别的按钮，直接点名字即可）。
3. 解压 zip，路径结构：

```
register-artifacts/
└── home/runner/work/grok-register/grok-register/data/
    ├── cpa_auth/
    │   └── xai-<邮箱>.json          # ★ 核心产物：OAuth token
    ├── grok2api_auth/
    │   ├── g2a-<邮箱>.json          # Grok2API build 格式
    │   ├── grok-web-<邮箱>.json      # Grok Web 格式
    │   └── grok-console-<邮箱>.json  # Grok Console 格式
    └── accounts/
        └── <邮箱>.txt               # 邮箱 + 密码 + SSO 会话
```

`tmp/auth.json` 与 `tmp/logs_tail.json` 也有全部 token 与运行日志备份。

### 3.5 验收标准

解压出 `cpa_auth/xai-*.json` 即代表注册成功；再用 §5 的方法实际调用一次模型接口确认 token 可用。

## 4. 方案 B：本机/服务器部署

适合没有 GitHub 或需要长期自建服务的情况。**注意：出口 IP 必须在非中国且信誉良好**（美国住宅/机房均可；中国 IP 会在注册页被 403 拦截；部分低信誉机房 IP 会被 Cloudflare Attention Required 拦截）。

### 4.1 构建并启动（Docker 方式）

```bash
cp .env.example .env
# 编辑 .env：GROK_BROWSER_ENGINE=camoufox（默认）
docker compose build
docker compose up -d
# 访问 http://服务器IP:8787
```

> Docker 模式复用 `data/config.json`（宿主机 data 目录）。未配置邮箱/代理可从 config.example.json 自动生成。

### 4.2 配置最小可用参数

`data/config.json` 最小可用配置（相当于 Actions 里的配置）：

```json
{
  "email_provider": "duckmail",
  "duckmail_api_base": "https://api.mail.tm",
  "duckmail_api_key": "",
  "register_count": 4,
  "register_workers": 1,
  "browser_engine": "camoufox",
  "browser_headless": false,
  "cpa_auto_add": true,
  "sso_allow_flagged": true,
  "cpa_token_mode": "device_protocol",
  "cpa_auth_dir": "data/cpa_auth",
  "grok2api_auth_dir": "data/grok2api_auth"
}
```

> 若走代理（如住宅代理），填入 `"proxy": "http://用户名:密码@IP:端口"`，Docker 下 `127.0.0.1` 会自动映射为 `host.docker.internal`。

### 4.3 通过 Web 控制台运行

1. 打开 `http://<服务器>:8787`，首次访问设置管理员账号密码。
2. 登录后在「配置」页确认邮箱提供商为 `duckmail`（Mail.tm），代理为空或填好。
3. 确认「连通性检查」全绿：`xAI 注册连通 = OK`、`邮箱 API = OK`。
4. 点击「开始注册」，日志会实时显示进度（约 2~4 分钟/账号）。
5. 等待任务结束，`data/cpa_auth/` 目录下出现 `xai-*.json` 即为成功。

## 5. Token 使用方法

### 5.1 Token 文件结构（cpa_auth/xai-<邮箱>.json）

```json
{
  "type": "xai",
  "auth_kind": "oauth",
  "email": "hsx3x82s33@emalupe.com",
  "access_token": "eyJ...",           // 有效期 6 小时
  "refresh_token": "mxj8...",         // 到期后用于续期
  "id_token": "eyJ...",               // ID Token
  "expires_in": 21600,
  "expired": "2026-08-23T03:22:27Z",  // access_token 过期时间
  "base_url": "https://cli-chat-proxy.grok.com/v1",  // ★ 必须用这个
  "headers": { "User-Agent": "grok-pager/0.2.93 ..." }  // 版本头可能过旧，见 5.4
}
```

### 5.2 模型列表（验证 token 有效）

```bash
curl -s https://cli-chat-proxy.grok.com/v1/models \
  -H "Authorization: Bearer <access_token>"
# 200 且列出 grok-4.5 / grok-4.6 等即有效
```

### 5.3 聊一次天（OpenAI 兼容，实测通过）

```bash
curl -s https://cli-chat-proxy.grok.com/v1/chat/completions \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -H "User-Agent: grok-pager/0.1.202 grok-shell/0.1.202 (linux; x86_64)" \
  -H "x-grok-client-version: 0.1.202" \
  -H "x-grok-client-identifier: grok-pager" \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role": "user", "content": "你好，介绍一下你自己"}],
    "max_tokens": 200
  }'
```

响应格式与 OpenAI 完全一致（`choices[0].message.content`）。

### 5.4 两个容易踩的坑

1. **版本号头**：token 文件内置的 `User-Agent`（grok-pager/0.2.93）过旧，直接使用会返回 HTTP 426 "Your Grok CLI version is outdated"。请求时必须覆盖为 `0.1.202` 或更高：
   - `User-Agent: grok-pager/0.1.202 grok-shell/0.1.202 (linux; x86_64)`
   - `x-grok-client-version: 0.1.202`
2. **必须走 cli-chat-proxy**：`base_url` 用 `https://cli-chat-proxy.grok.com/v1`；换成 `https://api.x.ai/v1` 会走计费通道返回 402。

### 5.5 refresh_token 续期

access_token 过期后，用 refresh_token 续期（平台 auth_exchange.py 已实现，本机账号文件被改动后也可手动调用）：

```bash
curl -s -X POST https://auth.x.ai/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=refresh_token" \
  --data-urlencode "client_id=b1a00492-073a-47ea-816f-4c329264a828" \
  --data-urlencode "refresh_token=<refresh_token>" \
  --data-urlencode "scope=openid profile email offline_access grok-cli:access api:access"
```

### 5.6 集成到 Grok2API / 一键导入

- `grok2api_auth/g2a-*.json` 是 Grok2API 项目的 JSON 导入格式（provider: grok_build），可在 Grok2API 管理端直接导入，变成 OpenAI 兼容服务。
- 平台配置里 `grok2api_auto_import=true` + 填 `grok2api_remote_url` / `grok2api_management_key` 可实现注册后自动推送。

## 6. 集成到 AI Agent（Claude Code / Codex / OpenCode 等）

免费 token 拿到手后，可以把它接入你日常的 AI 编程工具（cc-switch / Claude Code / Codex / OpenCode / Grok Build）。核心只有两个问题，解决后就全部通用：

1. **地址**：一律指向 `https://cli-chat-proxy.grok.com/v1`（不能用 api.x.ai）。
2. **版本头**：请求必须带 `x-grok-client-version` 头（缺了直接 426 "Grok CLI version is outdated"）。实测**只需要这一个头**（值 `0.1.202` 及以上即可），其余头可省。

> 实测（2026-08-23）：只带 `x-grok-client-version: 0.1.202`，`/v1/responses` 与 `/v1/chat/completions` 均 200 正常出词。

### 6.1 核心障碍：如何带上版本头

| 客户端 | 能否自定义请求头 | 处理方式 |
|---|---|---|
| OpenCode（自定义 provider） | ✅ `options.headers` | 直接写配置（见 6.2），零额外组件 |
| cc-switch 本地代理（15721） | ❌ 代理不注入该头，表单的自定义 header 也不下发 | 上游挂一个"头注入网关"（见 6.3） |
| Claude Code / Codex | ❌ 只认 base_url + api_key | 同上，经网关 |
| Grok Build (grok CLI) | ✅ 直接支持 api_key + base_url | 需网关或直接配（grok CLI 自带版本头） |

### 6.2 方式一：OpenCode 直接集成（最简单，无需任何中间件）

把下面这段合并进 `~/.config/opencode/opencode.json` 的 `provider` 节点（没有该文件就新建）：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "grok-free": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Grok Free",
      "options": {
        "baseURL": "https://cli-chat-proxy.grok.com/v1",
        "apiKey": "xai-粘贴你的access_token",
        "headers": {
          "x-grok-client-version": "0.1.202",
          "x-grok-client-identifier": "grok-pager",
          "User-Agent": "grok-pager/0.1.202 grok-shell/0.1.202 (linux; x86_64)"
        }
      },
      "models": {
        "grok-4.6": { "name": "Grok 4.6 (free)" },
        "grok-4.5": { "name": "Grok 4.5 (free)" }
      }
    }
  }
}
```

- `npm` 必须用 `@ai-sdk/openai-compatible`（OpenAI 兼容适配器，内部走 chat/completions）。
- 之后 opencode 内 `/models` 选择 `Grok Free` 下的 grok-4.6 即可使用。
- 若不想把 token 写死在文件里，可换成环境变量并配 `opencode auth` 连接流程。

### 6.3 方式二：头注入网关 + cc-switch（Claude Code / Codex / Gemini 全家桶）

cc-switch 是 Tauri 桌面应用（管理 Claude Code、Codex、Gemini CLI、Grok Build、OpenCode 等的 provider 切换与本地代理）。它的本地代理能把 Claude 的 Anthropic 格式转换成 OpenAI Responses/Chat 转发上游，但它**不会加 grok 版本头**。所以我们在上游前面挂一层本地"头注入网关"，把 token 与版本头都替客户端补上。

#### 6.3.1 启动头注入网关（本项目内置工具）

```bash
# 指向 token 文件（cpa_auth/xai-*.json 或只存 access_token 的文本都行）
python tools/grok_gateway.py --token-file data/cpa_auth/xai-<邮箱>.json --port 40200

# 或直接传 token / 环境变量
python tools/grok_gateway.py --token "xai-..." --port 40200
python tools/grok_gateway.py --token-env GROK_TOKEN --port 40200

# 多账号自动轮换（★ 推荐：data/cpa_auth 下所有 token 都加载，轮流使用）
python tools/grok_gateway.py --token-dir data/cpa_auth --port 40200
```

启动后本机出现一个 OpenAI 兼容端点 **`http://127.0.0.1:40200/v1`**：自动帮你加 `Authorization: Bearer <token>`、`x-grok-client-version` 等头，并转发到 cli-chat-proxy.grok.com。`/v1/models`、`/v1/chat/completions`、`/v1/responses` 全部实测 200。

**多 token 自动切换策略（`--token-dir` 模式）**：

- 加载目录下所有含 `access_token` 的 JSON（如 `data/cpa_auth/xai-*.json`），启动时打印账号列表。
- 请求按 round-robin 顺序自动轮换账号使用。
- 收到 **429（限流）** 或 **401/403（token 失效）** 时自动把该 token 冷却 `--ban-seconds` 秒（默认 300s）并立即切换下一个 token 重试，客户端无感知。
- **429 内部再识别 `free-usage-exhausted`（每日额度墙，见第 9 章）**：命中后按 `max_cooldown` 长冷却（约 24h），放掉短限流；全部 token 都不可用时返回 429 + Retry-After 提示。
- **自动续期（已实现）**：请求前检测 JWT `exp`，过期即用 `refresh_token` 调 `auth.x.ai/oauth2/token` 换新 access_token 并**写回原 JSON 文件**，下次启动无需手动处理；每个 token 只要账号未被吊销即可永久循环续期。
- **`--force-tool-choice`（已实现）**：`/v1/responses` 请求带 tools 但未指定 `tool_choice` 时，自动改写为 `required`——grok 免费通道在 auto 模式下倾向输出文字而不是调用工具，强制后 Claude Code 等客户端才能收到真正的 function_call。
- **`--filter-empty-edit`（已实现）**：流式响应中检测到 `Edit` 工具调用且 `old_string == new_string`（免费通道的劣化行为，会导致 Claude Code Error editing file 死循环）时，自动替换为无害的 Bash `echo` no-op 调用，Claude 继续后续任务而不会卡住。
- **`--control-port 40201`（已实现）**：管理接口。`GET /status` 返回全池健康快照（tokens_total/healthy/expired/cooling + 每 token 计数 + 请求统计）；`POST /refresh` 强制刷新全部过期 token。Web 平台顶栏网关控件 GET /api/gateway/status 即聚合此接口。
- token 冷却结束后自动回到轮换池。

#### 6.3.2 在 cc-switch 中添加 Grok provider（Claude Code 举例）

1. 打开 cc-switch → **Providers** → **Claude** → **添加 Provider**。
2. 名称随意（如 `Grok Free`）。
3. **Base URL**：填 `http://127.0.0.1:40200/v1`（网关地址）。
4. **API Key**：随便填一个非空占位（如 `grok`）——真正的 token 由网关注入，这个字段只是满足客户端非空校验。
5. **Advanced → API Format**：选 **OpenAI Responses**（走 `/v1/responses`，grok-4.6 实测支持；选 OpenAI Chat 也行，走 grok-4.5）。
6. 保存并设为当前 provider，开启 cc-switch 本地代理（http://127.0.0.1:15721）并启用该 app 的代理接管。

之后 Claude Code 的流量路径：

```
Claude Code → cc-switch 代理 (127.0.0.1:15721, Anthropic→OpenAI 转换)
           → grok_gateway (127.0.0.1:40200, 注入 token+版本头)
           → cli-chat-proxy.grok.com/v1 (真实 grok API)
```

#### 6.3.3 Codex / Gemini CLI

- **Codex**：添加 Codex provider → Base URL 填 `http://127.0.0.1:40200/v1`，API Key 填占位 → config.toml 里 `wire_api = "responses"`（或 `chat_completions`），保存启用即可。Codex 走 `~/.codex/config.toml` + `auth.json`。
- **Gemini CLI**：添加 Gemini provider → Base URL 填 `http://127.0.0.1:40200/v1`，key 占位。gemini 原生协议转换复杂度高，实测无保证，建议优先 Claude/Codex。

#### 6.3.4 Grok CLI (grok build) 直连

```toml
# ~/.grok/config.toml
[models]
default = "grok-4.6"

[model."grok-4.6"]
model = "grok-4.6"
base_url = "http://127.0.0.1:40200/v1"
name = "Grok Free"
api_key = "grok"          # 占位，网关会替换
api_backend = "responses"
```

> grok CLI 自己是官方客户端，自带版本头，所以也可以不用网关，把 `base_url` 直接写成 `https://cli-chat-proxy.grok.com/v1` + 真实 token。用网关的好处是 token 集中管理、可轮换。

### 6.4 网关常见问题

| 现象 | 处理 |
|---|---|
| 网关 502 "Bad Gateway" | token 过期或网络不通；换 token 重启网关 |
| 客户端 404 但 curl 网关正常 | 客户端请求路径带了 `/v1/v1`，cc-switch 会自动去重；直连 opencode 时 baseURL 不要带 `/v1` 后缀之外的重复段 |
| response_format/stream 报错 | grok 4.6 responses 不支持部分参数；可改 model 为 grok-4.5 + chat 端点 |
| 想自动续期 | 网关暂不自动刷新 token；接入本平台的 refresh 逻辑（backend/integrations/auth_exchange.py）可定时刷新后重启网关 |
| **流式请求 upstream 200 但 Claude Code 一直 "Waiting for API response" / "Stream error: error decoding response body"**（已实测修复） | 旧版网关把上游 SSE 原样转发却**没有声明响应边界**（无 Content-Length / 无 chunked / 未置 `Connection: close`），大请求或第二次请求时客户端读不到流结尾。**升级 grok_gateway.py 到含 `Connection: close` + 上游 EOF 后关闭的版本**（commit 之后版本），SSE 结束后客户端立即收到完整流 |
| **上游返回 Cloudflare 400 Bad Request HTML**（已实测修复） | 客户端（cc-switch）注入的小写 `authorization` 头 + 网关注入的大写 `Authorization` 头**重复**导致 CF 拒绝。**升级网关**：转发时剔除大小写所有 `authorization`/`user-agent`/版本头，统一由网关注入 |
| **401 "Invalid or expired credentials (... no auth context)"**（已实测修复） | 走 cc-switch 时其 API Key 被透传为 Bearer；旧版网关会再叠一层导致冲突。升级网关后客户端 key 填任意占位即可，网关负责注入真实 token |
| **502 "Upstream returned invalid JSON. Status: 200"（经 paritok 时，已实测修复）** | 上游（grok）对带 `Accept-Encoding: gzip` 的请求返回 gzip 压缩体，网关转发时删了 `content-encoding` 头但 body 未解压 → paritok 拿 gzip 当 JSON 解析失败。**升级网关**：读全 body 后发现 gzip 魔数自动 `gzip.decompress` 再转发（解压后去掉压缩头） |
| **429 + "subscription:free-usage-exhausted"（60+ 万 tokens / 500000 per 24h）** | 免费额度墙：每账号每天 50 万 tokens（滚动 24h 窗口），claude 大请求单次约 10 万 input tokens。24h 后自动回血；规模化=多账号轮换 + paritok 压缩（见第 9 章） |

**cc-switch 侧注意（实测要点）**：

- 在 cc-switch 运行中改数据库（`C:\Users\fr_li\.cc-switch\cc-switch.db`）`providers.settings_config` 的 `ANTHROPIC_AUTH_TOKEN` 为真实 token 会**立即生效**；但**重启 cc-switch 时活动文件会反填覆盖数据库**，注意顺序。
- Claude provider 的 **Advanced → API Format 必须选 "OpenAI Responses"**，否则 cc-switch 按 Anthropic 原生格式直发上游路径，grok 不认。
- 实测配置：Base URL `http://127.0.0.1:40200/v1`，API Key 任意非空占位（网关会覆盖），`response.completed` 事件 3.4s 内收到、流式完整返回。

## 7. 关键配置项说明

| 配置 | 默认 | 说明 |
|---|---|---|
| `email_provider` | cloudflare | `duckmail` + `duckmail_api_base=https://api.mail.tm` + key 留空 = 免费临时邮箱免 key |
| `register_count` | 1 | 单轮注册数（建议 4 抵消 Turnstile 随机失败） |
| `register_workers` | 1 | 并发注册数（越大约容易被风控，建议 1） |
| `sso_allow_flagged` | false（本项目已内置 true 场景） | **true 时跳过 botFlagSource 风控拦截继续 OAuth**；false 时风控账号直接失败 |
| `sso_detailed_risk_check` | false | 是否开启详细风控检查（会调 x.ai 账号风控接口） |
| `cpa_auto_add` | true | **必须 true 才会兑换 OAuth token**；false 则只保存 SSO 不换 token |
| `cpa_token_mode` | device_protocol | Device Flow 换 token，无需浏览器回调端口 |
| `browser_engine` | camoufox | 反检测浏览器；cloudflare 环境下不要用普通 playwright/chromium |
| `proxy` | 空 | 直连；如需代理填 `http://user:pass@ip:port` |
| `browser_headless` | false | 建议 false（headed），有小概率提高 Turnstile 通过率 |
| `enable_nsfw` | true | 是否在新账号开启 NSFW 解锁 |
| `register_count` + `account_interval` | 60-120s | 多账号间隔随机化，降低风控 |

## 8. 常见问题排查

| 现象 | 原因 | 解决 |
|---|---|---|
| 注册页打开就 403 "not available in your region" | 出口 IP 在中国 | 换非中国出口（Actions 天然避坑） |
| 403 "Attention Required!"（Cloudflare 挑战页） | 出口 IP 信誉低（部分机房/SG Azure） | 换 IP/换服务商；Actions 的 Azure US 段实测可用 |
| 日志反复 "点击 Turnstile" 但 token=0，最终失败 | Turnstile 非交互模式 + IP 随机评分低 | 多跑几轮；调大 register_count；换 run（每次 IP 都变） |
| 日志 "已获取到 sso cookie" 但提示风控拒绝 | botFlagSource 拦截 | 确认 `sso_allow_flagged: true` |
| 日志 "SSO→auth: 已关闭/不兑换 token" | `cpa_auto_add` 为 false | 改回 true |
| 调用模型报 426 outdated | 版本头过旧 | 覆盖 User-Agent + x-grok-client-version ≥ 0.1.202 |
| 调用报 402 | 走错通道 | base_url 必须是 cli-chat-proxy.grok.com/v1 |
| connectivity 检查 xAI 红 | 出口被拒 | 看上面 403 两条 |
| artifact 下载按钮找不到 | GitHub 页面交互限制 | 直接在地址栏输入 `/actions/runs/<id>/artifacts/<artifact-id>` 回车自动下载 |

## 9. 风险与合规声明

- **违反 ToS**：批量注册 x.ai 账号违反 xAI 服务条款，账号随时可能被封禁。
- **风控标记**：大多新账号带 botFlagSource 标记（本方案用开关跳过拦截，token 仍能正常换到并使用，但存在失效可能）。
- **免费额度**：免费层有速率限制（实测 token 对话正常；高并发会被限流），规模化需轮换多账号。
- **仅供学习研究**：请遵守所在地法律法规与平台政策，善意使用。