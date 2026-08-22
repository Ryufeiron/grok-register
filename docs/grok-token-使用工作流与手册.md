# Grok Token 免费获取工作流与使用手册

> 本文档描述如何通过本项目的自动化流水线，免费获取 x.ai 账号的 Grok OAuth Token，并作为 OpenAI 兼容 API 使用。整个方案已在 GitHub Actions（美国出口）上实测跑通，成功率约 50%（主要受 Cloudflare Turnstile 随机性影响）。

## 目录

1. [原理与架构](#1-原理与架构)
2. [前置条件](#2-前置条件)
3. [方案 A：GitHub Actions 全自动流水线（推荐）](#3-方案-agitHub-actions-全自动流水线推荐)
4. [方案 B：本机/服务器部署](#4-方案-b本机服务器部署)
5. [Token 使用方法](#5-token-使用方法)
6. [关键配置项说明](#6-关键配置项说明)
7. [常见问题排查](#7-常见问题排查)
8. [风险与合规声明](#8-风险与合规声明)

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

## 6. 关键配置项说明

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

## 7. 常见问题排查

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

## 8. 风险与合规声明

- **违反 ToS**：批量注册 x.ai 账号违反 xAI 服务条款，账号随时可能被封禁。
- **风控标记**：大多新账号带 botFlagSource 标记（本方案用开关跳过拦截，token 仍能正常换到并使用，但存在失效可能）。
- **免费额度**：免费层有速率限制（实测 token 对话正常；高并发会被限流），规模化需轮换多账号。
- **仅供学习研究**：请遵守所在地法律法规与平台政策，善意使用。