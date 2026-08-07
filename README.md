# Codex Provider Hub

<div align="center">

为 Codex 和 Claude Code 提供供应商探测、健康监测、本地中转、即时切换、自动恢复和 Token 统计。

![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-4b5563?style=flat-square)
![README](https://img.shields.io/badge/README-%E4%B8%AD%E6%96%87-c43d3d?style=flat-square)
[![Release](https://img.shields.io/github/v/release/loongkkk/codex-provider-hub?style=flat-square)](https://github.com/loongkkk/codex-provider-hub/releases/latest)

</div>

## 目录

- [它做什么](#它做什么)
- [核心工作流](#核心工作流)
- [核心能力](#核心能力)
- [安装与快速启动](#安装与快速启动)
- [供应商健康监测](#供应商健康监测)
- [项目结构](#项目结构)
- [测试](#测试)
- [安全边界](#安全边界)
- [许可证](#许可证)

## 它做什么

Codex Provider Hub 面向同时使用多个 Codex API 和 Claude Code 供应商的个人自部署场景。安装包只启动一个监听 `127.0.0.1:17890` 的后台服务，并在同一端口提供 Codex 与 Claude Code 两个控制台视图。

项目包含三组可以独立使用的能力：

- 本地中转：从 CC Switch 数据库只读加载 Codex 与 Claude 供应商，通过一个本地服务提供两个 Web 控制台、即时切换、失败重试和 Token 统计。
- 健康监测：定时探测供应商和模型可用性，通过独立状态页展示当前状态、历史结果和请求诊断。
- 探测工具：提供命令行、GUI 和 TUI 适配工具，用隔离的 Codex 运行目录验证供应商能力。

## 核心工作流

```text
Codex ───── Responses API / SSE ────┐
                                    │
Claude Code ─ Messages API / SSE ───┼──► Local Proxy ───► Upstream Providers
                                    │    单端口路由 / 重试 / 统计
CC Switch SQLite DB ──── 只读 ──────┘

独立监测链路：

Provider Config ──► Probe Worker ──► SQLite ──► Status Web
```

切换供应商后，新请求立即使用新供应商。尚未输出正文的旧请求如果发生可重试错误，下一次尝试会由最新选中的供应商接管；已经输出内容的请求不会跨供应商重放，避免重复文本、工具调用或计费。

## 核心能力

### 本地中转与控制台

- 只监听 `127.0.0.1:17890`：`/v1/messages` 及其 Token 计数接口使用 Claude 协议，其他 `/v1/*` 使用 Codex Responses 协议。
- Codex 与 Claude Code 共用进程和端口，但保留独立供应商选择、重试状态、Token 数据与恢复记录。
- 从 `~/.cc-switch/cc-switch.db` 只读加载 Codex API 供应商。
- 支持供应商即时切换、隐藏、拖动排序和搜索。
- 支持 Windows 通知区域常驻、重复启动检测和桌面快捷方式。
- 支持从控制台配置本地端口、供应商数据源和服务器检测地址。
- 控制台提供浅色、深色和跟随系统主题。

### 自动恢复

- 对建连错误、首个输出前的流中断以及 HTTP `429/500/502/503/504` 自动重试。
- 支持固定或递增等待、无限重试、最大等待时间和供应商熔断。
- 能识别 HTTP 200 SSE 流中正文输出前的内嵌 429 错误。
- 手动切换供应商后，等待中的旧请求会在下一次重试时切换到新供应商。
- 脱敏后的恢复记录保留最近 24 小时，重启程序后仍可在控制台查看。

### Token 统计

- 优先读取上游 `usage`，上游未提供时使用 `tiktoken` 估算。
- 支持今日、近 24 小时、近 7 日、近 30 日和全部时间范围。
- 按供应商汇总输入、输出、缓存和总 Token。
- 本地只保存聚合所需字段，不保存请求正文、回答正文或 Key。

### 健康监测

- 按供应商和模型独立探测，支持健康与异常状态下的不同调度间隔。
- 同一供应商可以按模型选择隔离的 Codex CLI 或 Claude CLI 探测流程；未指定时默认使用 Codex CLI。
- 提供手动优先探测、状态历史、诊断分类和公开只读数据库。
- Worker 与 Web 服务分离，包含 systemd 和 Nginx 部署模板。

## 安装与快速启动

### Windows 便携版（推荐）

Windows x64 用户可以从 [GitHub Releases](https://github.com/loongkkk/codex-provider-hub/releases/latest) 下载便携版，无需安装 Python 或项目依赖。文件名暂时保留 `CodexLocalProxy`，程序会启动一个同时支持 Codex 与 Claude Code 的本地中转服务：

- [下载 `CodexLocalProxy-win-x64.exe`](https://github.com/loongkkk/codex-provider-hub/releases/latest/download/CodexLocalProxy-win-x64.exe)
- [下载 SHA-256 校验文件](https://github.com/loongkkk/codex-provider-hub/releases/latest/download/CodexLocalProxy-win-x64.exe.sha256)

下载后直接双击 EXE，程序会静默启动并常驻 Windows 通知区域，不会自动打开网页。右键托盘图标可分别打开 Codex 和 Claude Code 控制台，也可开启当前用户的“开机自启”。使用前需要先安装并配置 CC Switch，确保当前用户目录存在 `~/.cc-switch/cc-switch.db`。共享配置与 Codex/Claude Code 的独立状态均保存在 `~/.codex-local-proxy/`。

### macOS 便携版

Apple Silicon（M 系列芯片）Mac 用户可以从 [GitHub Releases](https://github.com/loongkkk/codex-provider-hub/releases/latest) 下载 `.zip` 便携版，无需安装 Python 或项目依赖。文件名暂时保留 `CodexLocalProxy`，程序会启动一个同时支持 Codex 与 Claude Code 的本地中转服务：

- [下载 `CodexLocalProxy-macos-arm64.zip`](https://github.com/loongkkk/codex-provider-hub/releases/latest/download/CodexLocalProxy-macos-arm64.zip)
- [下载 SHA-256 校验文件](https://github.com/loongkkk/codex-provider-hub/releases/latest/download/CodexLocalProxy-macos-arm64.zip.sha256)

使用步骤：

1. 下载并解压 `.zip`，得到 `CodexLocalProxy-macos-arm64.app`。
2. 将其拖入「应用程序」文件夹（可选）。
3. **首次打开**：右键点击 `.app` → 选择「打开」→ 在弹出的「无法验证开发者」对话框中点击「打开」。由于当前版本未经 Apple 代码签名与公证，双击会被 Gatekeeper 拦截，必须通过右键打开一次；之后即可正常双击启动。
   - 命令行等价方式：`xattr -dr com.apple.quarantine /路径/到/CodexLocalProxy-macos-arm64.app`
4. 程序会静默启动并常驻 macOS 菜单栏，不会自动打开网页；需要时从菜单栏手动打开 Codex 或 Claude Code 控制台。使用前需要先安装并配置 CC Switch，确保当前用户目录存在 `~/.cc-switch/cc-switch.db`。共享配置与 Codex/Claude Code 的独立状态均保存在 `~/.codex-local-proxy/`。

> 说明：当前仅提供 ARM64 包，覆盖 Apple Silicon 机型；Intel Mac 暂不支持便携版，可从源码运行。

### 从源码运行

源码运行提供两种平级方式，可按本机习惯任选其一。

### 1. 使用 uv

先安装 [uv](https://docs.astral.sh/uv/)，然后在仓库根目录执行：

Windows PowerShell：

```powershell
uv venv --clear .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-status.txt
uv run --python .venv\Scripts\python.exe local_proxy_app.py
```

macOS/Linux：

```bash
uv venv --clear .venv
uv pip install --python .venv/bin/python -r requirements-status.txt
uv run --python .venv/bin/python local_proxy_app.py
```

默认不会自动打开网页；如需在启动时同时打开两个控制台，可追加 `--open-browser`。

### 2. 使用原生 Python venv

Windows PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-status.txt
```

Linux：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-status.txt
```

### 2. 启动本地中转

需要先安装并配置 CC Switch，确保本机存在 `~/.cc-switch/cc-switch.db`。

```powershell
.\.venv\Scripts\python.exe local_proxy_app.py
```

默认不会自动打开网页。如需在启动时同时打开两个控制台，可显式追加 `--open-browser`。

控制台地址：

```text
http://127.0.0.1:17890/control/codex/
```

Claude Code 控制台：

```text
http://127.0.0.1:17890/control/claude/
```

Claude Code PowerShell 配置：

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:17890"
$env:ANTHROPIC_API_KEY = "local-claude-proxy"
claude
```

本地 Key 只是占位值，中转会删除客户端认证头并注入当前 Claude 供应商的真实认证。Claude 中转支持 Anthropic `/v1/messages`、SSE、HTTP `408/429/500/502/503/504/529` 重试和输出前切换供应商。CC Switch 中标记为 `openai_chat` 的 Claude 供应商第一版会显示为协议不兼容，不能选择。

Windows 用户可以安装桌面快捷方式：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_local_proxy_shortcut.ps1
```

在控制台点击“复制 Codex 配置”，将生成的片段合并到 Codex `config.toml`，重启一次 Codex 后即可通过控制台切换供应商。

Codex 供应商列表中的“复制临时启动命令”会生成一条直接使用该供应商启动 Codex CLI 的单次命令，不修改 `config.toml`。Windows 使用 PowerShell 命令，macOS/Linux 使用 POSIX shell 命令；命令包含供应商地址和认证信息，应按密钥处理，避免保存到公共脚本或共享终端历史。

完整说明见 [Codex 本地中转文档](docs/codex-local-proxy.md)。

### 3. 使用供应商探测工具

列出 CC Switch 中的供应商：

```powershell
.\.venv\Scripts\python.exe probe_codex_cc_switch.py --list-providers
```

探测当前供应商并输出 JSON：

```powershell
.\.venv\Scripts\python.exe probe_codex_cc_switch.py --current-only --json
```

## 供应商健康监测

复制公开示例配置并替换为自己的供应商地址和 systemd credential 名称：

```bash
cp config/providers.example.toml config/providers.toml
```

`model_clients` 可以把指定模型映射为 `claude`，其余模型默认使用 `codex`。启用 Claude 模型时还需要配置服务级 `claude_bin` 和供应商级 `claude_base_url`；两种客户端都使用临时隔离目录，不复用本机登录状态。

单次运行 Worker：

```bash
./.venv/bin/python -m provider_status.worker \
  --config config/providers.toml \
  --control-database var/control/manual-probes.sqlite3 \
  --once
```

启动状态页：

```bash
./.venv/bin/python -m provider_status.web \
  --database var/public/status.sqlite3 \
  --control-database var/control/manual-probes.sqlite3 \
  --host 127.0.0.1 \
  --port 8000
```

生产部署模板位于 `deploy/`。示例供应商和域名全部是占位值，不能直接用于生产。

## 项目结构

```text
.
├── local_proxy_app.py         本地中转统一启动入口
├── local_proxy/               统一应用、公共转发核心、Codex/Claude 平级协议模块
│   ├── application.py         统一服务生命周期和托盘
│   ├── server.py              一个 FastAPI 应用和一个 LocalProxyServer
│   ├── core.py                公共路由、重试、存储和转发基础能力
│   ├── shared_settings.py     共享运行设置与旧数据迁移
│   ├── codex.py / claude.py   两套平级供应商实现
│   ├── codex_profile.py       Codex 配置与 Profile 装配
│   ├── claude_profile.py      Claude 配置与 Profile 装配
│   ├── protocols/             协议适配器
│   └── transports/            上游传输实现
├── proxy_static/              Codex 与 Claude Code 共享控制台
├── probe_codex_cc_switch.py   CC Switch 供应商探测 CLI
├── probe_codex_gui.py         桌面探测界面
├── probe_tools/               探测入口共用的客户端和 GUI 支持模块
├── provider_status/           健康监测 Worker、Codex/Claude 探测、存储和状态页
├── config/                    公开供应商配置示例
├── deploy/                    systemd 与 Nginx 部署模板
├── scripts/                   Windows 快捷方式安装脚本
├── tests/                     Python、PowerShell 和前端运行时测试
└── docs/                      使用说明
```

## 测试

运行完整 Python 测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

检查前端脚本与主题运行时：

```powershell
node --check proxy_static/app.js
node --check provider_status/static/app.js
node --test tests/theme_runtime.test.js
```

## 安全边界

- 本地中转只允许监听回环地址。
- CC Switch SQLite 数据库使用只读连接。
- 普通状态、统计和配置 API 不返回上游 Key；只有用户点击 Codex 供应商的“复制临时启动命令”时，受本地控制请求保护且禁止缓存的接口会把 Key 写入剪贴板。Key 不会渲染到页面或写入访问日志。
- 转发前移除客户端认证头，再应用当前供应商认证配置。
- Token 统计不保存请求正文和回答正文。
- 私有配置、数据库、日志、探测报告、虚拟环境、证书和密钥默认忽略。
- `config/providers.example.toml` 只包含示例域名，不包含真实供应商或凭据。

公开发布前建议再次运行敏感信息扫描，并只推送公开 `main` 分支，不要使用 `git push --all`。

## 许可证

当前仓库尚未附带开源许可证。除非项目所有者另行授权，代码仅供查看和评估。
