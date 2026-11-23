# Mini Harness

Mini Harness 用同一个 Agent Core 调用云端 Chromium 或用户主动连接的 macOS／Windows 桌面。模型每轮看到两个工具，`bash` 和 `computer_use`。同一次 Run 的浏览器、命令和文件都留在同一个 Worker。SQLite 保存会话、审批、工具轨迹和恢复状态，截图随工具结果回给支持视觉输入的模型。

## 启动本地桌面版

在 macOS 或 Windows 的已登录桌面中，先运行 `python -m pip install -e '.[desktop]'`。在环境变量或 `.harness/local.env` 中配置 `HARNESS_API_TOKEN` 和 `HARNESS_WORKER_SECRET`，可以用 `python -c 'import secrets; print(secrets.token_urlsafe(32))'` 各生成一次。然后运行：

```bash
mini-harness local
```

打开 [本地 Web UI](http://127.0.0.1:8000/) 并配置模型。macOS 需为启动进程的应用授予屏幕录制与辅助功能权限。Ctrl+C 停止服务和桌面 Worker。

## 在无桌面的服务器上启动

Docker Compose 会启动浏览器 Worker。复制 `.env.example` 为 `.env`，填入 API 和 Worker 密钥及支持 OpenAI Chat Completions 工具调用与图片输入的模型配置。

```bash
docker compose up --build
```

直接在无桌面的机器上运行时，可安装 `python -m pip install -e '.[cloud]'`，用 `python -m playwright install chromium` 安装浏览器或用 `MINI_CLOUD_BROWSER_EXECUTABLE` 指定已有浏览器，然后运行 `mini-harness local --headless`。Linux 上 `mini-harness local` 默认使用浏览器 Worker。

API 默认只监听宿主机的 `127.0.0.1:8000`。可在 `.env` 中设置 `HARNESS_PORT` 更换宿主机端口，并为 CLI 设置对应的 `HARNESS_URL`。下面示例在另一个终端运行。先将 `.env` 中的 API 密钥设置为该终端的 `HARNESS_API_TOKEN`。

```bash
python -m pip install -e .
mini-harness workers
mini-harness run '打开 https://example.com，截图并告诉我页面标题。' --wait
mini-harness trace RUN_ID
```

自动选择优先使用桌面 Worker。服务尚未识别到启用了桌面控制的 Worker 时，才会自动选择浏览器 Worker。如果已知的桌面 Worker 都忙碌或离线，任务会暂停，待桌面可用后可用 `mini-harness resume RUN_ID` 继续。使用 `--worker WORKER_ID` 可指定节点，使用 `--session SESSION_ID` 可延续会话。已经绑定的 Run 需要原 Worker 和原工作区。如果 Worker 进程重启并丢失工作区，请启动新 Run。断线发生在操作发出之后时，轨迹会记录结果未知，Agent 可以先观察再决定下一步。用 `mini-harness cancel RUN_ID` 取消并释放节点。

`mini-harness run` 默认使用自动审批，可用 `--approval-mode manual` 或 `--approval-mode allow` 切换模式。`--wait` 遇到待审批操作时会返回 Run ID 和操作详情。用 `mini-harness approve RUN_ID CALL_ID allow` 放行，或用 `deny` 拒绝。通过 `mini-harness status RUN_ID` 查看后续状态。

Docker 中的 Worker 串行处理 Run，每次创建新的工作目录与 Chromium 浏览器上下文。下载路径和上传文件都相对于该目录。任务文件随 Run 关闭而清理，截图和工具轨迹保存在服务端。容器是云端的隔离边界。共享一个 Worker 容器的任务应属于同一信任域。部署时，将 Worker 与内网服务和宿主机敏感资源隔开。对不可信任务使用独立的 Worker 容器，并保留 Bash 审批。

也可以单独运行 `mini-harness serve` 和 `python -m workers.cloud` 来开发与测试。先安装 `python -m pip install -e '.[cloud]'`，再运行 `python -m playwright install chromium`。开发用 Worker 进程会以当前账号运行，其 Bash 拥有该账号的权限。

## 连接本地桌面

在要操作的桌面上安装 `python -m pip install -e '.[desktop]'`，设置下列变量，再运行 `python -m workers.desktop`。

| 变量 | 用途 |
| --- | --- |
| `MINI_WORKER_URL` | 服务器的 Worker WebSocket 地址，如 `wss://harness.example/v1/workers/ws` |
| `MINI_WORKER_ID` | 唯一的桌面节点 ID |
| `MINI_WORKER_SECRET` | 与服务端配置对应的节点共享密钥 |
| `MINI_DESKTOP_CONTROL=1` | 在设备上授权桌面控制 |
| `MINI_DESKTOP_BASH=1` | 在设备上另行授权 Bash，默认关闭 |
| `MINI_DESKTOP_BASH_KIND` | Windows 上选择 `git-bash` 或 `wsl` |
| `MINI_DESKTOP_BASH_PATH` | 可选的 Bash 或 WSL 可执行文件路径 |

macOS 需为启动 Worker 的终端授予屏幕录制和辅助功能权限。Windows Worker 应从已登录、未锁屏的桌面启动。本地控制使用主屏幕坐标，当前键入支持 ASCII。桌面文件选择对话框可以通过坐标和键盘操作。云端的浏览器选择器、下载和上传动作在桌面上不适用。

运行 `python -m workers.desktop --pause` 或将鼠标移至主屏幕角落以触发暂停。移动鼠标离开角落后，用 `python -m workers.desktop --resume` 解除本地暂停。结束 Worker 进程会断开远程控制。

远程连接应使用 TLS。服务端可用 `HARNESS_WORKER_SECRETS` 的 JSON 映射为每个 Worker 配置独立密钥。WebSocket 通过双向 HMAC 握手认证，明文 WebSocket 只用于本机开发或专用 Docker 网络。

## Skill 学习与复用

1. 对已完成的 Run 执行 `mini-harness learn RUN_ID`，让模型从轨迹提出一个 `SKILL.md` 候选。
2. 用 `mini-harness evaluate NAME VERSION cloud-browser` 在目标环境评测。
3. 检查候选和评测结果，运行 `mini-harness publish NAME VERSION --yes` 发布。后续任务按关键词加载适用于当前环境的已验证版本。
4. 用 `mini-harness skills --environment macos-desktop` 查看平台状态，用 `mini-harness rollback NAME VERSION --yes` 恢复已验证的历史版本。

评测依赖任务所需的独立环境和验收条件。部署时通过 `HARNESS_SKILL_EVALUATOR=package.module:function` 配置可信评测入口，或用 `create_app(settings, skill_evaluator=...)` 注入。入口是异步函数，接收 `(skill, environment)`，在隔离的评测 Worker 上运行验收任务，再返回包含布尔字段 `passed` 的 JSON 字典。只有通过当前环境评测的 Skill 才会被用于该环境，未评测的平台会显示为 `untested`。默认启动不注册评测入口。版本文件位于 `HARNESS_DATA_DIR/skills/versions`，生命周期和版本元数据保存在 SQLite。

## API 与验收

用 `mini-harness serve` 在本机启动后，直接打开 [本地 Web UI](http://127.0.0.1:8000/)。
首次运行时在「模型设置」中填写 OpenAI 兼容的 Base URL、模型名及服务需要的 API Key。
设置保存在 `HARNESS_DATA_DIR/model.json`，下次启动会优先使用已保存的配置；未保存时使用环境变量。
免 Token 连接只用于直接访问本机回环地址的浏览器页面，CLI 和外部 API 客户端仍使用 `HARNESS_API_TOKEN`。
侧栏显示最近 100 个对话，包括通过 CLI 创建的会话。选中对话后可连续发送消息，点击「新建对话」可开始独立会话。
助手回复支持 Markdown。每轮可展开运行轨迹和截图，也可以审批、恢复或取消当前运行。Enter 发送，Shift+Enter 换行。
后续消息会携带此前已完成、失败或取消运行的完整事件记录。历史截图以轨迹中的附件引用保留，不会重新作为图片发送给模型；每次运行使用新的工作区和浏览器上下文。长对话可能超过模型服务的上下文限制。

输入框下方的审批模式适用于下一条消息：

- **完全手动**：每次工具调用都需确认。
- **自动审批**：桌面和浏览器 Worker 的截图和观察自动通过，浏览器还允许不移动指针的短距离滚动。启用了 Bash 的 Worker 可以自动运行 `pwd`、带字面量参数的 `echo` 和有限格式的 `printf`，浏览器 Worker 还支持列出当前目录的 `ls`。其他命令、导航、点击、输入及上传下载需要确认。运行轨迹会记录审批理由。
- **完全允许**：工具调用直接执行。Worker 自己的权限限制继续生效。

在单一信任域中，可以设置 `HARNESS_TRUST_CLOUD_BASH=1`，让隔离的浏览器 Worker 在自动审批模式中运行任意 Bash 命令。

FastAPI 接口文档在 `/docs`。除健康检查和 Worker 握手外，请求使用 `Authorization: Bearer <HARNESS_API_TOKEN>`。核心入口是 `POST /v1/runs`、`GET /v1/runs/{id}` 和 `GET /v1/runs/{id}/events`。API 调用方可在创建 Run 时传入 `"approval_mode": "auto"`，省略时沿用旧审批行为。`POST /v1/chat/completions` 提供非流式文本请求，长任务可通过返回的 Run ID 从核心接口继续查看。服务进程使用单个 Uvicorn 实例。

```bash
python -m pip install -e '.[cloud]'
python -m playwright install chromium
python -m unittest discover -s tests -v
```

端到端验收使用本地测试网站和确定性测试模型，通过真实 WebSocket 与 Chromium 登录、下载 CSV、用 Bash 转换、再上传结果。它不需要真实账号或模型 API 密钥。设置 `MINI_CLOUD_BROWSER_EXECUTABLE` 可使用已有 Chromium。
