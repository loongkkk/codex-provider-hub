+++
id = "2026-08-07-uv-startup-instructions"
type = "docs"
release_bump = "none"
status = "verified"
+++

# 增加 uv 启动说明

## 目标

在 README 中提供使用 uv 创建环境、安装依赖并启动项目的可复制命令。

## 现状

README 仅说明使用原生 Python venv 和 pip，未提供 uv 工作流。

## 设计范围

- 在源码运行章节增加 uv 方式。
- 将 uv 与原生 Python venv 标记为两种平级的可选方式。
- 提供 Windows、macOS/Linux 的环境创建、依赖安装和启动命令。

## 非目标

- 不修改项目依赖、构建配置或启动逻辑。
- 不新增 uv lockfile。

## 兼容性

仅修改 README，无运行时、接口、配置、数据或迁移影响。

## 风险

uv 未安装时命令无法执行；README 提供 uv 官方安装入口，并保留原生 Python venv 方式。

## 测试计划

- 检查 README 命令与现有入口和依赖文件一致。
- 运行完整 Python、Node 和脚本语法验证。

## 实际改动

- `README.md` 的“从源码运行”章节增加 uv 与原生 Python venv 的平级说明，以及 Windows、macOS/Linux 的环境创建、依赖安装和启动命令。

## 验证结果

- `uv --version`：通过，版本 `0.11.6`。
- `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`：通过，共 359 项测试。
- `Get-ChildItem tests -Filter *.test.js | ForEach-Object { node --test $_.FullName; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }`：通过，共 21 项测试。
- `node --check proxy_static/app.js`：通过。
- `node --check provider_status/static/app.js`：通过。
- `git diff --check`：通过。
- `Get-NetTCPConnection -LocalPort 17890 -State Listen`：通过，确认项目服务已停止且端口未监听。

## PR

pending
