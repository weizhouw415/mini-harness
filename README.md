# Mini Harness

## 核心设计

Mini Harness 采用单工具架构。一个 Agent Loop 统一推理、执行和观察，模型通过一个执行入口调用 Worker 上的命令、程序和可复用技能。

- **Bash 是唯一的模型执行接口。** 优先用命令、脚本、SDK 和 API 处理任务。
- **CLI 封装能力。** `harness computer` 操作界面，`harness view` 检查产物，`harness register` 交付文件。Skill 和 Env 是同一命令体系里可继续扩展的入口，目前通过工作区技能索引和 Worker 能力声明发现。
- **Artifact 承载感知。** 上传文件、截图和运行产物共享文件引用与多模态观察。支持视觉的模型可以直接查看画面，再据此验证和修正结果。
- **Skills 沉淀可复用的能力。** 将经过验证的流程和脚本保存为技能，通过评测与审批后供后续任务使用。
- **Harness 是稳定的运行时。** 运行时负责绑定 Worker、审批、工作区、事件记录和恢复。新能力可以通过 CLI、Skill 或脚本扩展，模型接口和 Agent Loop 保持稳定。

同一次 Run 的浏览器、命令和文件留在同一个 Worker 中。SQLite 保存会话、审批、工具轨迹和恢复状态。Bash 命令可指定 `timeout`，默认 120 秒，并受 `HARNESS_MAX_TOOL_SECONDS` 上限约束。

## 启动本地桌面版

在 macOS 或 Windows 的已登录桌面中，先运行 `python -m pip install -e '.[desktop]'`。在环境变量或 `.harness/local.env` 中配置 `HARNESS_API_TOKEN` 和 `HARNESS_WORKER_SECRET`，可以用 `python -c 'import secrets; print(secrets.token_urlsafe(32))'` 各生成一次。然后运行：

```bash
mini-harness local
```

打开 [本地 Web UI](http://127.0.0.1:8000/) 并配置模型。本地启动器会启动桌面和云端浏览器两个 Worker。操作 macOS 当前桌面时，需为启动进程的应用授予屏幕录制与辅助功能权限。Ctrl+C 停止服务和两个 Worker。

输入框下方默认选「桌面」，用于操作当前桌面和原生应用，点击和输入会使用实际鼠标或键盘。选择「云端浏览器」后，下一轮会在独立的 Chromium 会话里完成网页操作。每轮浏览器使用独立的标签页和登录状态，下载和上传使用该轮的工作目录。云端浏览器在本地启动时优先使用本机安装的 Chrome，也可用 `python -m playwright install chromium` 安装 Chromium，或通过 `MINI_CLOUD_BROWSER_EXECUTABLE` 指定浏览器。审批卡和运行轨迹会显示操作目标。

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

macOS 需为操作当前桌面的进程授予屏幕录制和辅助功能权限。Windows Worker 应从已登录、未锁屏的桌面启动。当前桌面的控制使用主屏幕坐标，键入支持 ASCII。桌面文件选择对话框可以通过坐标和键盘操作。浏览器选择器及自动下载上传适用于云端浏览器。

macOS 截图需要在「系统设置 → 隐私与安全性 → 屏幕与系统音频录制」中授权启动 Worker 的应用，例如终端。授权后，从同一个应用重新启动 Mini Harness 或独立桌面 Worker。

桌面和云端浏览器都支持截图、单击、双击、右键点击、移动指针、拖动、输入文本、按键和滚动。滚动量单位为像素，Windows 桌面会换算为近似的滚轮步数。

### Computer CLI

在 Run 的 Bash 工具中，每次提交一条独立的 `harness computer` 命令。Harness 解析参数后按具体界面动作审批，再调用同一 Worker 的图形后端。

```bash
harness computer --help
harness computer screen-info
harness computer screenshot --output screen.png
harness computer click 500 300 --observe
harness computer drag 100 200 500 600
harness computer type 'Hello World'
harness computer key Ctrl+S
harness computer scroll down 500
```

`--observe` 会在动作后返回截图。`screenshot --output` 同时保存一份到工作区，后续 Bash 调用可用 `harness view screen.png` 查看。云端浏览器还支持 `navigate`、元素选择器和工作区文件上传下载。

界面动作沿用截图、导航、鼠标、键盘及文件操作各自的审批规则。`HARNESS_TRUST_CLOUD_BASH` 适用于普通 Shell 命令，界面动作仍由图形审批判断。桌面 Bash 与 GUI 由 Worker 分别授权，启用桌面 Bash 后，Shell 命令以当前操作系统账号执行。

运行 `python -m workers.desktop --pause` 可暂停 Worker。操作当前桌面时，也可以把鼠标移至主屏幕角落触发暂停。用 `python -m workers.desktop --resume` 解除本地暂停。结束 Worker 进程会断开远程控制。

远程连接应使用 TLS。服务端可用 `HARNESS_WORKER_SECRETS` 的 JSON 映射为每个 Worker 配置独立密钥。WebSocket 通过双向 HMAC 握手认证，明文 WebSocket 只用于本机开发或专用 Docker 网络。

## Skill 学习与复用

启用 Bash 且支持 Skill 工作区的 Worker 会将当前环境中已通过评测的 active Skill 放入运行目录的 `.mini-harness/skills/active/`，索引位于其中的 `index.json`。Agent 可以通过 Bash 读取对应的 `<name>/SKILL.md`，也可将可复用的流程或改进写入 `.mini-harness/skills/candidates/<name>/SKILL.md`。Run 成功结束时，Harness 校验并收录候选。候选需要通过评测并经明确批准后才能用于后续任务。旧 Worker 和未启用 Bash 的 Worker 继续接收匹配的 Skill 指引。

1. 也可对已完成的 Run 执行 `mini-harness learn RUN_ID`，让模型从轨迹提出一个 `SKILL.md` 候选。
2. 用 `mini-harness evaluate NAME VERSION cloud-browser` 在目标环境评测。
3. 检查候选和评测结果，运行 `mini-harness publish NAME VERSION --yes` 发布。后续任务可使用适用于当前环境的已验证版本。
4. 用 `mini-harness skills --environment macos-desktop` 查看平台状态，用 `mini-harness rollback NAME VERSION --yes` 恢复已验证的历史版本。

评测依赖任务所需的独立环境和验收条件。部署时通过 `HARNESS_SKILL_EVALUATOR=package.module:function` 配置可信评测入口，或用 `create_app(settings, skill_evaluator=...)` 注入。入口是异步函数，接收 `(skill, environment)`，在隔离的评测 Worker 上运行验收任务，再返回包含布尔字段 `passed` 的 JSON 字典。只有通过当前环境评测的 Skill 才会被用于该环境，未评测的平台会显示为 `untested`。默认启动不注册评测入口。版本文件位于 `HARNESS_DATA_DIR/skills/versions`，生命周期和版本元数据保存在 SQLite。

## API 与验收

用 `mini-harness serve` 在本机启动后，直接打开 [本地 Web UI](http://127.0.0.1:8000/)。
首次运行时在「模型设置」中填写 OpenAI 兼容的 Base URL、模型名及服务需要的 API Key。
设置保存在 `HARNESS_DATA_DIR/model.json`，下次启动会优先使用已保存的配置；未保存时使用环境变量。
免 Token 连接只用于直接访问本机回环地址的浏览器页面，CLI 和外部 API 客户端仍使用 `HARNESS_API_TOKEN`。
侧栏显示最近 100 个对话，包括通过 CLI 创建的会话。选中对话后可连续发送消息，点击「新建对话」可开始独立会话。
助手回复支持 Markdown。每轮可展开运行轨迹和截图，也可以审批、恢复或取消当前运行。Enter 发送，Shift+Enter 换行。
Harness 以流式方式向模型请求回复。模型返回的思考过程和正文会随生成进度显示，工具调用收齐后再审批和执行。模型请求的读取超时为 15 分钟，在等待下一段数据时计时。
后续消息会携带此前已完成、失败或取消运行的事件记录，模型回复按合并后的完整消息回传。历史截图以轨迹中的附件引用保留，不会重新作为图片发送给模型；每次运行使用新的工作区和浏览器上下文。长对话可能超过模型服务的上下文限制。

### 上传附件与查看产物

通过输入框旁的附件按钮选择文件。每轮可上传 8 个文件，单个文件最多 20 MiB，合计最多 40 MiB。原件保存在服务端，并在该轮开始时复制到 Worker 的 `uploads/` 目录。模型会收到原件路径，并在前几步收到支持格式的图像预览或文本摘要。

Agent 可以通过 Bash 执行 `harness view`，主动查看工作区内的文件。例如用 Playwright 生成 `preview.png`，再执行 `harness view preview.png`，模型就可以检查实际渲染效果。`harness view --help` 列出页面、工作表、视频帧和图像裁剪等选项。用 `harness register <文件路径>` 将原件交付给用户。两条命令在 Bash 调用结束时读取文件，并沿用现有的 Bash 审批模式。

每轮对话显示已上传的文件，以及运行生成的预览和显式交付的文件。图片和文本可在页面中预览，其他原件可以下载。已登记文件随服务数据目录保留。PDF 预览需要 Poppler，SVG 渲染需要 `rsvg-convert`，Word 和 PowerPoint 的页面渲染需要在 Worker 上安装 LibreOffice，媒体预览需要 FFmpeg。模型视觉预览需要支持图像输入的 OpenAI 兼容模型。

API 客户端可向 `POST /v1/uploads` 发送 `application/octet-stream` 原始文件，在 `X-File-Name` 中提供 URL 编码后的文件名，并将返回的 ID 作为 `upload_ids` 传给 `POST /v1/runs`。运行的文件列表和下载地址分别为 `GET /v1/runs/{id}/files` 和 `GET /v1/runs/{id}/files/{file_id}`。

输入框下方的审批模式可以在运行中切换。已经开始的工具操作会继续执行，正在等待审批和还未执行的操作按新模式判断。没有运行中的任务时，所选模式用于下一条消息。

- **完全手动**：每次工具调用都需确认。
- **自动审批**：桌面和浏览器 Worker 的截图和观察自动通过，浏览器还允许不移动指针的短距离滚动。启用了 Bash 的 Worker 可以自动运行符合规则的常见查看命令，包括读取文件和列出指定目录，以及受支持的只读命令组合；写入、运行未知程序和无法确认安全性的命令仍需确认。导航、点击、输入及上传下载也需要确认。运行轨迹会记录审批理由。
- **完全允许**：工具调用直接执行。Worker 自己的权限限制继续生效。

在单一信任域中，可以设置 `HARNESS_TRUST_CLOUD_BASH=1`，让隔离的浏览器 Worker 在自动审批模式中运行任意 Bash 命令。

FastAPI 接口文档在 `/docs`。除健康检查和 Worker 握手外，请求使用 `Authorization: Bearer <HARNESS_API_TOKEN>`。核心入口是 `POST /v1/runs`、`GET /v1/runs/{id}` 和 `GET /v1/runs/{id}/events`。API 调用方可在创建 Run 时传入 `"approval_mode": "auto"`，省略时沿用旧审批行为；未结束的 Run 可以用 `PATCH /v1/runs/{id}/approval-mode` 更改。`POST /v1/chat/completions` 提供非流式文本请求，长任务可通过返回的 Run ID 从核心接口继续查看。服务进程使用单个 Uvicorn 实例。

```bash
python -m pip install -e '.[cloud]'
python -m playwright install chromium
python -m unittest discover -s tests -v
```

端到端验收使用本地测试网站和确定性测试模型，通过真实 WebSocket 与 Chromium 登录、下载 CSV、用 Bash 转换、再上传结果。它不需要真实账号或模型 API 密钥。设置 `MINI_CLOUD_BROWSER_EXECUTABLE` 可使用已有 Chromium。
