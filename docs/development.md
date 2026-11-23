> **2026-11-23 开发计划增补：** [事件驱动求职工作流计划](event-driven-job-workflow-2026-11-23.md)。使多来源岗位去重、账户验证暂停、授权申请与回执对账具备可恢复状态。新增里程碑属于待开发项，原有签收状态不变。

# 开发与测试

## 项目结构

| 路径 | 职责 |
| --- | --- |
| `serve.py`、`desk/web.py` | 本机 HTTP 服务、页面和 API、配置校验 |
| `desk/engine.py`、`desk/storage.py` | 巡查调度、匹配和通知动作、SQLite 存储 |
| `desk/platforms.py`、`desk/sources.py` | 平台浏览器会话、公开招聘板、企业招聘页和搜索 |
| `desk/models.py`、`desk/backends.py` | 简历解析、匹配评分、模型目录与调用 |
| `desk/searxng.py`、`plugins/searxng/` | 可选搜索插件与 Compose 模板 |
| `desk/notifications.py`、`desk/secrets.py` | Windows 通知、邮箱预设与密钥处理 |
| `index.html`、`desk.js`、`backend-picker.js`、`desk.css` | 主工作台界面，直接由本机服务提供 |
| `review.html`、`core.js`、`app.js`、`fixture.js`、`style.css` | 传统人工审阅台与演示数据 |

## 运行回归测试

先按 [README](../README.md) 安装 Python 依赖和 Chromium。在仓库根目录执行下列命令。Node.js 用于 JavaScript 测试，运行主工作台只需 Python 环境。

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_workbench
node test.cjs
```

macOS / Linux 将 Python 路径替换为 `.venv/bin/python`。Python 测试使用临时数据库、模拟招聘页面和本机测试服务，覆盖来源、评分与去重、账号验证暂停、会话绑定、投递状态、通知重试及桌面与移动端界面。生产账号的访问权限、真实消息回执、实际邮箱送达和各第三方模型可用性需要在对应账号中验证。

## 单独运行人工审阅台的浏览器测试

该测试使用 Node Playwright。以下为 Windows PowerShell 示例，在项目根目录执行。第一个终端安装测试依赖并启动独立数据目录。

```powershell
npm install --no-save --package-lock=false playwright
npx playwright install chromium
.\.venv\Scripts\python.exe serve.py --port 8877 --data-dir .workbench/test-browser
```

在第二个终端运行浏览器测试。

```powershell
$env:TEST_PORT = "8877"
node test-browser.cjs
```

选用其他端口时同步修改 `TEST_PORT`。测试默认使用 Node Playwright 安装的 Chromium，也可设置 `CHROMIUM` 为浏览器可执行文件的绝对路径。测试结束后在第一个终端按 Ctrl+C。

## 提交问题与改动

报告问题时，附操作系统、Python 版本、复现步骤和脱敏后的错误提示。平台、模型和邮件问题请注明接入方式和发生阶段。可使用虚构简历和测试岗位复现。在发布截图或日志前，移除邮箱、联系方式、简历内容、令牌、Cookie 和会话标识。提交改动前运行相应测试，并检查 `git diff --cached` 中的文件。

仓库目前未指定开源许可证。
