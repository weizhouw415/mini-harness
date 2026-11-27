# Mini Harness

## 核心设计

Mini Harness 采用单工具架构。一个 Agent Loop 统一推理、执行和观察，模型通过一个执行入口调用 Worker 上的命令、程序和可复用技能。

- **Bash 是唯一的模型执行接口。** 优先用命令、脚本、SDK 和 API 处理任务。
- **CLI 封装能力。** `harness computer` 操作界面，`harness view` 检查产物，`harness register` 交付文件。
- **Artifact 承载感知。** 上传文件、截图和运行产物共享文件引用与多模态观察。支持视觉的模型可以直接查看画面，再据此验证和修正结果。
- **Skills 沉淀可复用的能力。** Agent 将有效的流程和脚本保存到技能库，后续任务可以直接查找和复用。
- **Harness 是稳定的运行时。** 运行时负责绑定 Worker、审批、工作区、事件记录和恢复。新能力可以通过 CLI、Skill 或脚本扩展，模型接口和 Agent Loop 保持稳定。

同一次 Run 的浏览器、命令和文件留在同一个 Worker 中。SQLite 保存会话、审批、工具轨迹和恢复状态。Bash 命令可指定 `timeout`，默认 120 秒，并受 `HARNESS_MAX_TOOL_SECONDS` 上限约束。

## 启动本地桌面版

在 macOS 或 Windows 的已登录桌面中，先运行 `python -m pip install -e '.[desktop]'`。在环境变量或 `.harness/local.env` 中配置 `HARNESS_API_TOKEN` 和 `HARNESS_WORKER_SECRET`，可以用 `python -c 'import secrets; print(secrets.token_urlsafe(32))'` 各生成一次。然后运行：

```bash
mini-harness local
```

打开 [本地 Web UI](http://127.0.0.1:8000/) 并配置模型。本地启动器会启动一个桌面 Worker 和三个云端浏览器 Worker。操作 macOS 当前桌面时，需为启动进程的应用授予屏幕录制与辅助功能权限。Ctrl+C 停止服务和所有 Worker。

输入框下方默认使用「自动选择」，优先使用空闲的桌面 Worker，也可在桌面忙碌或离线时使用空闲的云端浏览器 Worker。可手动指定环境，也可随时切回「自动选择」。桌面操作使用实际鼠标和键盘，每台桌面同时供一个对话使用。云端浏览器每轮使用独立的 Chromium 会话和工作目录。本地浏览器优先使用本机安装的 Chrome，也可用 `python -m playwright install chromium` 安装 Chromium，或通过 `MINI_CLOUD_BROWSER_EXECUTABLE` 指定浏览器。审批卡和运行轨迹会显示操作目标。

## 在无桌面的服务器上启动

Docker Compose 会启动三个独立的浏览器 Worker。复制 `.env.example` 为 `.env`，填入 API 和 Worker 密钥及支持 OpenAI Chat Completions 工具调用与图片输入的模型配置。

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

CLI 自动选择优先使用桌面 Worker。服务尚未识别到启用了桌面控制的 Worker 时，才会自动选择浏览器 Worker。如果已知的桌面 Worker 都忙碌或离线，任务会暂停，待桌面可用后可用 `mini-harness resume RUN_ID` 继续。使用 `--worker WORKER_ID` 可指定节点，使用 `--session SESSION_ID` 可延续会话。已经绑定的 Run 需要原 Worker 和原工作区。如果 Worker 进程重启并丢失工作区，请启动新 Run。断线发生在操作发出之后时，轨迹会记录结果未知，Agent 可以先观察再决定下一步。用 `mini-harness cancel RUN_ID` 取消并释放节点。

`mini-harness run` 默认使用自动审批，可用 `--approval-mode manual` 或 `--approval-mode allow` 切换模式。`--wait` 遇到待审批操作时会返回 Run ID 和操作详情。用 `mini-harness approve RUN_ID CALL_ID allow` 放行，或用 `deny` 拒绝。通过 `mini-harness status RUN_ID` 查看后续状态。

Docker 中的 Worker 串行处理 Run，每次创建新的工作目录与 Chromium 浏览器上下文。下载路径和上传文件都相对于该目录。任务文件随 Run 关闭而清理，截图和工具轨迹保存在服务端。容器是云端的隔离边界。共享一个 Worker 容器的任务应属于同一信任域。部署时，将 Worker 与内网服务和宿主机敏感资源隔开。对不可信任务使用独立的 Worker 容器，并保留 Bash 审批。

也可以单独运行 `mini-harness serve` 和 `python -m workers.cloud` 来开发与测试。先安装 `python -m pip install -e '.[cloud]'`，再运行 `python -m playwright install chromium`。为每个浏览器 Worker 设置不同的 `MINI_WORKER_ID`，即可增加并行容量。开发用 Worker 进程会以当前账号运行，其 Bash 拥有该账号的权限。

## 接入飞书 IM

Docker 服务端镜像包含飞书依赖。使用本地 Python 安装时，在服务端环境运行 `python -m pip install -e '.[feishu]'` 并重启服务。飞书只需要服务端能够访问飞书开放平台，无需公开本地 API。

在「设置 → 飞书 IM」点击「生成二维码」，用手机飞书扫一扫并确认创建。Mini Harness 会自动保存机器人凭证、启用连接，并仅将扫码账号加入允许名单。确认后，在手机飞书打开机器人并发送单聊消息。如企业要求审批，按飞书页面提示完成审批后再测试消息。已有机器人可以展开「手动配置已有机器人」。 [飞书官方扫码注册说明](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/README.zh.md#一键创建应用)

手动配置步骤如下。

1. 在飞书开放平台创建企业自建应用，添加机器人能力，开通「获取用户发给机器人的单聊消息」和「以应用的身份发消息」权限。通过该应用的身份获取允许使用机器人的用户 Open ID。 [机器人指南](https://open.feishu.cn/document/client-docs/bot-v3/how-to-use-bot-in-feishu) · [Open ID 说明](https://open.feishu.cn/document/home/user-identity-introduction/open-id)
2. 在 Mini Harness 本地 Web UI 中打开「设置 → 飞书 IM」，填写 App ID、App Secret 和允许的用户 Open ID，启用并保存。等待页面显示已连接。使用 API Token 管理的服务也可调用 `PUT /v1/settings/feishu`。飞书会在保存长连接配置时检查客户端是否在线。 [长连接说明](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)
3. 在飞书应用的「事件与回调」中选择长连接，添加「接收消息」事件 `im.message.receive_v1` 和「卡片回传交互」回调 `card.action.trigger`。开通「发送、删除消息表情回复」权限 `im:message.reactions:write_only`，发布应用版本并确认白名单用户在应用可用范围内。已有机器人也可按这一步补充回调和表情权限。 [卡片交互配置](https://open.feishu.cn/document/feishu-cards/configuring-card-interactions) · [消息表情 API](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1#emoji-reply)

同一个用户的消息会在独立会话里依次处理。收到任务后，机器人在原消息上添加「正在输入」表情，发完最终回复后移除。工具步骤按批次回复到原消息，最终回复使用飞书 Markdown 卡片。审批默认使用飞书卡片内的指令。`/approve <审批码>` 同意本次，`/approve-session <审批码>` 允许本会话当前任务及后续任务的工具操作，`/deny <审批码>` 拒绝本次。`/approval manual` 撤销本会话对新任务的授权。

飞书长连接收到消息后，审批指令即可使用。希望使用按钮时，在飞书侧确认 `card.action.trigger` 长连接回调已配置并发布，然后在 Mini Harness 飞书 IM 设置中勾选「使用审批按钮」。如果飞书开发后台无法打开扫码创建的应用，仍可直接在对话里使用审批指令。飞书 IM 设置中的「完全允许」会为所有白名单用户跳过工具审批，并放行正在等待审批的飞书任务。恢复「逐次审批」后，新任务恢复默认策略，此前单独授权的会话可用 `/approval manual` 撤销。

任务使用浏览器 Worker。任务暂停时可在 Mini Harness 网页中恢复或取消。禁用连接会停止从飞书接收和回复，已创建的任务仍可在网页中操作。重新启用同一个应用后，网关继续处理已接收的消息。

首版支持单聊文本。把飞书白名单限定在信任此 Harness 实例的用户内。个人跨会话记忆开启后，同一实例中的不同会话可能访问全局记忆。

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

## Skill 文件

每个 Skill 存在 `<技能目录>/<名称>/SKILL.md`，附属文件可以放在同一目录下。
`SKILL.md` 的 YAML 头部包含 `name` 和 `description`，也可以用 `environments` 列出适用的 Worker 环境。
每个新 Run 只把名称、描述和文件路径加入系统提示。正文按需用 Bash 读取，增、改、删也直接操作文件。

本地启动时，Worker 共用 `HARNESS_DATA_DIR/skills`（默认 `.harness/skills/`），可用 `HARNESS_SKILLS_DIR` 覆盖。
Docker Compose 的 Worker 共用 `/skills` 卷。独立 Worker 可通过 `MINI_WORKER_SKILLS_DIR` 指向自己的持久目录或共享挂载。
服务升级时会一次性把旧目录中的启用版本和附属文件导入文件库，原目录保留为迁移备份。

同一会话累计 10 轮工具调用，或后续任务包含纠正时，后台会复盘已完成的运行记录，提炼可复用的方法。复盘可以读取相关 Skill，直接创建或更新技能文件和附属文件，文件发生并发变更时会报告冲突。复盘结果状态记录在任务中，复盘会额外调用模型。运行工作区结束后会清理，复盘主要依据运行记录和已有技能文件。

本地和 Compose 默认由服务端与 Worker 共享技能目录；独立 Worker 若使用不同的 `MINI_WORKER_SKILLS_DIR`，服务端新增的技能不会自动同步到该 Worker。

## 记忆

在 Web UI 的「设置 → 记忆」中可以启用记忆、控制自动提取和启用个人跨会话记忆。记忆默认关闭。自动提取开关默认打开，启用记忆后生效。关闭记忆会停止任务中的记忆召回、模型记忆命令和后台学习，已保存的记忆仍可通过管理接口查看或删除。设置保存在 `HARNESS_DATA_DIR/memory-settings.json`，修改后对后续模型步骤和后台任务生效。

默认使用会话范围，记忆仅在所属 Session 中召回。启用「个人跨会话记忆」后，Agent 保存和自动提取的记忆使用实例级个人范围，可以供此实例的其他会话使用。实例使用共享的 API Token，个人范围适用于同一使用者的部署。服务端集中保存记忆，桌面和浏览器 Worker 使用同一套数据。

任务开始时会根据用户请求检索相关记忆。模型需要查看更多信息时，可以在 Bash 中单独执行 `harness memory search QUERY`、`harness memory get ID` 或 `harness memory source ID`。用户明确要求记住或忘记时，模型也可以通过 `harness memory remember KEY VALUE` 和 `harness memory forget ID` 操作。自动提取会在 Run 成功结束后读取用户输入，使用当前配置的模型提出候选，并通过持久化任务队列处理。提取发生在后台，后续会话开始时任务可能还在处理。检索使用精确文本、SQLite 全文索引和中文短词匹配。

`harness memory profile` 展示当前可访问的偏好。启用记忆后，长任务达到上下文阈值时会生成工作检查点并保留原始运行记录。后台每天整理可追溯的相似记忆，处理带到期时间的短期记忆，并为复杂任务生成情景摘要。自动提取和整理会调用当前配置的模型，产生额外请求。

CLI 管理命令需要 `HARNESS_API_TOKEN` 和已有 Session ID。`remember` 对同一范围、类型和键更新记忆，可通过 `--expected-version` 检查当前版本。个人范围需要先在设置中启用。

```bash
mini-harness memory --session SESSION_ID remember --key design.style --value '喜欢简洁设计'
mini-harness memory --session SESSION_ID search '简洁'
mini-harness memory --session SESSION_ID profile
mini-harness memory --session SESSION_ID get MEMORY_ID
mini-harness memory --session SESSION_ID source MEMORY_ID
mini-harness memory --session SESSION_ID remember --key design.style --value '喜欢蓝白配色' --expected-version 1
mini-harness memory --session SESSION_ID forget MEMORY_ID
```

记忆、版本、来源引用和后台任务保存在 `HARNESS_DATA_DIR/runs.sqlite3`。遗忘会删除该记忆及其保存版本和索引，并阻止旧任务重新提取同一条记忆。原始会话、运行轨迹和上传文件按原有方式保留，可能仍包含用户最初提供的信息。显式重新保存可恢复记忆。

## API 与验收

用 `mini-harness serve` 在本机启动后，直接打开 [本地 Web UI](http://127.0.0.1:8000/)。
更新代码后需重启服务和 Worker 才会加载新版本；请先处理正在运行、等待审批或暂停的任务，因为重启 Worker 会清理其运行工作区。
首次运行时在「设置」中填写供应商名称、OpenAI 兼容的 Base URL、模型名及服务需要的 API Key。可以保存多组供应商配置，并随时启用其中一组；启用后，后续模型请求（包括进行中任务的下一次请求）使用该配置。
设置保存在 `HARNESS_DATA_DIR/model.json`，下次启动会优先使用已保存的配置；未保存时使用环境变量。已有的单模型配置会自动保留为一组供应商配置。
在「设置 → 任务能力」中可以分别启用或关闭文件预览（`view`）、文件交付（`register`）和界面操作（`computer_use`）。默认均启用，Bash 始终可用；全部关闭时，模型仅通过 Bash 执行任务。设置保存在 `HARNESS_DATA_DIR/capabilities.json`，每个任务开始时固定一份配置，暂停恢复时沿用；修改只影响之后开始的任务。关闭能力时，需同时更新独立运行的 Worker 到支持能力开关的版本。
免 Token 连接只用于直接访问本机回环地址的浏览器页面，CLI 和外部 API 客户端仍使用 `HARNESS_API_TOKEN`。
侧栏显示最近 100 个对话及其运行状态，包括通过 CLI 创建的会话。对话运行时可点击「新建对话」开始另一条任务，也可以从侧栏切回查看回复和审批。每个对话在当前轮结束后可继续发送消息。
助手回复支持 Markdown。每轮可展开运行轨迹和截图，也可以审批、恢复或取消当前运行。Enter 发送，Shift+Enter 换行。
Harness 以流式方式向模型请求回复。模型返回的思考过程和正文会随生成进度显示，工具调用收齐后再审批和执行。模型请求的读取超时为 15 分钟，在等待下一段数据时计时。
当模型服务明确拒绝在 Chat Completions 中同时使用推理和工具，并提示可将 `reasoning_effort` 设为 `none` 时，Harness 会关闭该次请求的推理并重试一次，运行轨迹会提示这一兼容处理。
后续消息会携带此前已完成、失败或取消运行的事件记录，模型回复按合并后的完整消息回传。历史截图以轨迹中的附件引用保留，不会重新作为图片发送给模型；每次运行使用新的工作区和浏览器上下文。长对话可能超过模型服务的上下文限制。

### 上传附件与查看产物

通过输入框旁的附件按钮选择文件。每轮可上传 8 个文件，单个文件最多 20 MiB，合计最多 40 MiB。原件保存在服务端，并在该轮开始时复制到 Worker 的 `uploads/` 目录。模型会收到原件路径，并在前几步收到支持格式的图像预览或文本摘要。

Agent 可以通过 Bash 执行 `harness view`，主动查看工作区内的文件。例如用 Playwright 生成 `preview.png`，再执行 `harness view preview.png`，模型就可以检查实际渲染效果。`harness view --help` 列出页面、工作表、视频帧和图像裁剪等选项。用 `harness register <文件路径>` 将原件交付给用户。两条命令在 Bash 调用结束时读取文件，并沿用现有的 Bash 审批模式。

每轮对话把运行产物分为交付文件和运行预览。点击产物、附件或轨迹截图可打开右侧文件面板，预览图片或文本并下载原件。产物卡片显示同一路径的最新版本，可在面板里切换历史版本。已登记文件随服务数据目录保留。PDF 预览需要 Poppler，SVG 渲染需要 `rsvg-convert`，Word 和 PowerPoint 的页面渲染需要在 Worker 上安装 LibreOffice，媒体预览需要 FFmpeg。模型视觉预览需要支持图像输入的 OpenAI 兼容模型。

API 客户端可向 `POST /v1/uploads` 发送 `application/octet-stream` 原始文件，在 `X-File-Name` 中提供 URL 编码后的文件名，并将返回的 ID 作为 `upload_ids` 传给 `POST /v1/runs`。运行的文件列表和下载地址分别为 `GET /v1/runs/{id}/files` 和 `GET /v1/runs/{id}/files/{file_id}`。

输入框下方的审批模式可以在运行中切换。已经开始的工具操作会继续执行，正在等待审批和还未执行的操作按新模式判断。没有运行中的任务时，所选模式用于下一条消息。

- **完全手动**：每次工具调用都需确认。
- **自动审批**：桌面和浏览器 Worker 的截图和观察自动通过，浏览器还允许不移动指针的短距离滚动。启用了 Bash 的 Worker 可以自动运行符合规则的常见查看命令，包括读取文件和列出指定目录，以及受支持的只读命令组合；写入、运行未知程序和无法确认安全性的命令仍需确认。导航、点击、输入及上传下载也需要确认。运行轨迹会记录审批理由。
- **完全允许**：工具调用直接执行。Worker 自己的权限限制继续生效。

在单一信任域中，可以设置 `HARNESS_TRUST_CLOUD_BASH=1`，让隔离的浏览器 Worker 在自动审批模式中运行任意 Bash 命令。

FastAPI 接口文档在 `/docs`。除健康检查和 Worker 握手外，请求使用 `Authorization: Bearer <HARNESS_API_TOKEN>`。核心入口是 `POST /v1/runs`、`GET /v1/runs/{id}` 和 `GET /v1/runs/{id}/events`。创建 Run 时可用 `worker_id` 指定节点，或用 `worker_target` 选择 `desktop`、`browser`、`auto`。`auto` 优先空闲桌面，其次空闲浏览器。API 调用方可在创建 Run 时传入 `"approval_mode": "auto"`，省略时沿用旧审批行为。未结束的 Run 可以用 `PATCH /v1/runs/{id}/approval-mode` 更改。`POST /v1/chat/completions` 提供非流式文本请求，长任务可通过返回的 Run ID 从核心接口继续查看。服务进程使用单个 Uvicorn 实例。

```bash
python -m pip install -e '.[cloud]'
python -m playwright install chromium
python -m unittest discover -s tests -v
```

端到端验收使用本地测试网站和确定性测试模型，通过真实 WebSocket 与 Chromium 登录、下载 CSV、用 Bash 转换、再上传结果。它不需要真实账号或模型 API 密钥。设置 `MINI_CLOUD_BROWSER_EXECUTABLE` 可使用已有 Chromium。
