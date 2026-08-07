+++
id = "2026-08-07-session-route-settings"
type = "feature"
release_bump = "minor"
status = "verified"
+++

# 请求页会话路由设置

## 目标

在请求记录页提供集中式会话路由设置，并优化供应商列表的列宽与请求入口，方便按会话选择后续使用的供应商。

## 现状

供应商列表中的名称列会占用过多空间；请求记录行内直接放置后续路由下拉框，视觉密度高且不易理解。点击供应商活动请求时还会自动筛选当前供应商和运行中状态。

## 设计范围

- 限制供应商名称列宽，将可用空间分配给服务器检测、请求和 Token 列。
- 在请求页刷新按钮旁增加设置入口，以两个下拉框选择最近 7 天的 Codex 会话和后续供应商。
- 活跃会话优先排序，页面不暴露原始线程 ID。
- 移除请求行内路由控件；点击供应商活动请求进入请求页时默认查看全部状态和全部供应商。

## 非目标

- 不改变上游请求转发协议、重试策略或供应商检测逻辑。
- 不修改 Codex 会话内容、历史记录或外部配置文件格式。

## 兼容性

新增本地控制接口 `/api/sessions`，并复用现有会话路由接口；旧客户端配置和已有请求记录无需迁移。仅 Codex 配置启用会话路由设置，Claude 页面保持隐藏入口。

## 风险

会话索引文件不存在、格式异常或没有最近 7 天记录时，设置列表显示空状态；路由保存失败时恢复当前选择并提示错误。页面静态资源缓存版本同步递增。

## 测试计划

- Python 全量 unittest。
- Node 前端测试、JavaScript 语法检查、Python compileall。
- `git diff --check` 和提交前敏感信息扫描。
- 检查远端主线同步、分支 rebase、提交 hooks 和 PR required checks。

## 实际改动

实现于 `local_proxy/codex_sessions.py`、`local_proxy/core.py`、`local_proxy/server.py`、`local_proxy/codex_profile.py`、`proxy_static/app.js`、`proxy_static/index.html` 和 `proxy_static/styles.css`；新增会话目录 API、哈希会话标识、7 天活跃排序、集中式路由设置浮层、供应商表格列宽调整，并移除请求行内路由控件。测试覆盖同步更新。

## 验证结果

- `.\.venv\Scripts\python.exe -m unittest discover -s tests -q`：通过，358 项测试。
- `node --test tests/*.test.js`：通过，21 项测试。
- `node --check proxy_static/app.js`：通过。
- `.\.venv\Scripts\python.exe -m compileall -q local_proxy`：通过。
- `git diff --check`：通过。
- 对新增 diff 行扫描 API Key、Bearer Token、固定服务器地址等敏感模式：未发现敏感值。

## PR

pending
