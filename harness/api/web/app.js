"use strict";

const $ = (id) => document.getElementById(id);
const names = {
  queued: "排队中", running: "运行中", awaiting_approval: "等待审批",
  paused: "已暂停", completed: "已完成", failed: "失败", cancelled: "已取消",
  submitting: "正在发送", send_failed: "发送失败",
};
const active = new Set(["queued", "running", "awaiting_approval", "paused"]);
const progressLabels = {
  submitting: ["正在发送", "正在创建任务"],
  queued: ["等待运行", "正在调度任务"],
  running: ["等待模型响应", "任务仍在运行"],
  awaiting_approval: ["等待审批", "此任务需要你确认下一步操作"],
  paused: ["已暂停", "可以恢复运行或取消任务"],
};
const actionLabels = {
  cancel: ["cancelling", "正在取消"],
  resume: ["resuming", "正在恢复"],
  allow: ["approving", "正在提交审批"],
  deny: ["approving", "正在提交审批"],
};
const approvalToolNames = { bash: "终端命令", computer_use: "界面操作" };
const computerTargetNames = { browser: "后台浏览器", desktop: "当前桌面" };
const actionButtons = [...document.querySelectorAll("[data-action]")];
const idPattern = /^[a-f0-9]{32}$/;
const workerNames = { desktop: "桌面", browser: "云端浏览器" };
const approvalHints = {
  manual: "每次工具调用都等你确认",
  auto: "低风险操作自动通过，其他操作需确认",
  allow: "所有工具调用都直接执行",
};
const attachmentLimits = { count: 8, file: 20 * 1024 ** 2, total: 40 * 1024 ** 2 };
const fileSizeFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const imageTypes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"]);
const fileSourceNames = { output: "输出文件", preview: "运行预览" };
const textPreviewLimit = 256 * 1024;
sessionStorage.removeItem("harness-token");
let modelConfig = null;
const conversations = new Map();
const draftConversations = new Map();
const startedSessions = new Map();
let draftSequence = 0;
let newDraft = createDraft();
let conversation = newDraft;
let recentSessions = [];
let run = null;
const approvalUpdates = new Map();
const actions = new Map();
let scrollToLatest = false;
let conversationLoading = false;
const turns = new Map();
let filePreview = null;
const filePanelOverlay = matchMedia("(max-width: 1120px)");
let revision = 0;
let overviewPolling = false;
let detailRevision = -1;
let historyKey = "";
const readyWorkers = new Set();
const urls = new Set();
const visibleHistoryTurns = new IntersectionObserver((entries) => {
  for (const { target, isIntersecting } of entries) {
    if (!isIntersecting) continue;
    visibleHistoryTurns.unobserve(target);
    const id = target.dataset.runId;
    const view = turns.get(id);
    if (view && !view.traceLoaded && !view.loading) void loadTrace(view, id, true);
  }
}, { root: $("scroll"), rootMargin: "160px 0px" });

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (className) node.className = className;
  return node;
}

function notice(message = "", source = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
  $("notice").dataset.source = source;
}

function conversationNotice(origin, message = "", source = "") {
  origin.notice = { message, source };
  if (origin === conversation) notice(message, source);
}

function requestErrorMessage(path, status, data) {
  data = data && typeof data === "object" ? data : {};
  const details = Array.isArray(data.detail) ? data.detail : [];
  const oldAssetRoute = details.some((item) =>
    Array.isArray(item.loc) && item.loc[0] === "path" && item.loc[1] === "asset");
  if (["/ui/providers", "/ui/capabilities", "/ui/memory/settings", "/v1/settings/feishu"].includes(path) &&
      (status === 404 || (status === 422 && oldAssetRoute))) {
    return "页面与服务端版本不一致。请先处理进行中或暂停的任务，再重启 Mini Harness 并刷新页面。";
  }
  if (typeof data.detail === "string") return data.detail;
  if (status === 422 && details.length) {
    // Only show known field names and fixed messages, never server-provided input values.
    const fields = new Set(["name", "model", "base_url", "api_key", "view", "register", "computer_use", "enabled", "automatic", "personal",
      "app_id", "app_secret", "allowed_open_ids",
      "task", "worker_id", "worker_target", "approval_mode", "session_id", "upload_ids", "command", "timeout"]);
    const field = (Array.isArray(details[0].loc) ? details[0].loc : []).find((part) => fields.has(part));
    const kind = String(details[0].type || "");
    const reason = kind === "missing" ? "缺少必填内容" :
      kind.includes("too_long") ? "内容过长" :
      kind.includes("bool") ? "需要布尔值" :
      kind === "extra_forbidden" ? "包含不支持的字段" : "内容不符合要求";
    return "请求参数无效" + (field ? "（" + field + "）" : "") + "：" + reason + "。";
  }
  return "请求失败 (" + status + ")";
}

async function request(path, { method = "GET", body, extraHeaders, timeout = 12000 } = {}) {
  const headers = { "X-Harness-UI": "1", ...extraHeaders };
  if (body !== undefined && !(body instanceof Blob)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch(path, {
    method, headers, body, cache: "no-store", signal: AbortSignal.timeout(timeout),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const message = requestErrorMessage(path, response.status, data);
    throw Object.assign(new Error(message), { status: response.status });
  }
  return response;
}

async function api(path, options) {
  return (await request(path, options)).json();
}

function connection(count) {
  const node = $("connection");
  node.classList.toggle("online", count > 0);
  const label = count === null ? "未连接" : count + " 个 Worker 在线";
  if (node.textContent.trim() !== label) {
    node.replaceChildren(element("i"), document.createTextNode(" " + label));
  }
}

function approvalHint() {
  $("approval-hint").textContent = approvalHints[$("approval-mode").value];
}

function computerTargetName(args) {
  return Object.hasOwn(computerTargetNames, args?.target) ? computerTargetNames[args.target] : "";
}

function nearLatest() {
  const scroll = $("scroll");
  return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 24;
}

function updateJump() {
  $("jump-latest").hidden = $("run").hidden || nearLatest();
}

function scrollToEnd() {
  const scroll = $("scroll");
  scroll.scrollTop = scroll.scrollHeight;
  updateJump();
}

function preserveScroll(update, followLatest = true) {
  const scroll = $("scroll");
  const following = followLatest && nearLatest();
  const height = scroll.scrollHeight;
  const viewport = scroll.clientHeight;
  update();
  if (following && (height !== scroll.scrollHeight || viewport !== scroll.clientHeight)) scrollToEnd();
  else updateJump();
}

function resizeComposer() {
  const input = $("task");
  input.style.height = "auto";
  input.style.height = input.scrollHeight + "px";
}

function createDraft() {
  return { key: "draft/" + ++draftSequence, task: "", files: [] };
}

function getConversation(key) {
  if (!conversations.has(key)) conversations.set(key, { key, task: "", files: [] });
  return conversations.get(key);
}

function pendingSubmission() {
  return conversation.submission;
}

function pendingAction() {
  return actions.get(run?.id);
}

function pendingApprovalMode() {
  return Boolean(run && approvalUpdates.has(run.id));
}

function workerAvailable() {
  const target = $("worker").value;
  return target === "auto" ? readyWorkers.size > 0 : readyWorkers.has(target);
}

function setText(node, content) {
  if (node.textContent !== content) node.textContent = content;
}

function updateSendControl() {
  const pending = Boolean(pendingSubmission());
  const button = $("send");
  button.disabled = !($("task").value.trim() || conversation.files.length) || !workerAvailable() || pending ||
    (!conversation.key.startsWith("draft/") && !run) || active.has(run?.status) || Boolean(pendingAction());
  button.setAttribute("aria-busy", String(pending));
  button.setAttribute("aria-label", pending ? "正在发送" : "发送消息");
  $("task").readOnly = pending;
}

function showRunControls() {
  const pending = pendingSubmission();
  const currentAction = pendingAction();
  const changingApprovalMode = pendingApprovalMode();
  const hasConversation = !conversation.key.startsWith("draft/") || Boolean(pending);
  $("empty").hidden = hasConversation;
  $("conversation-loading").hidden = !conversationLoading;
  $("run").hidden = !turns.size && !pending;
  const first = $("turns").firstElementChild;
  setText($("title"), first?.querySelector(".task")?.textContent.split("\n")[0]
    || (hasConversation ? "对话" : "新对话"));

  const status = $("status");
  const [state, label] = actionLabels[currentAction] || (pending ? ["submitting", "正在发送"] :
    conversationLoading ? ["loading", "读取对话中"] : [run?.status || "", names[run?.status] || ""]);
  status.dataset.state = state;
  status.hidden = !label;
  setText(status, label);
  const approval = run?.approval;
  $("approval").hidden = !approval || run.status !== "awaiting_approval";
  const tool = approval?.tool;
  const toolName = Object.hasOwn(approvalToolNames, tool) ? approvalToolNames[tool] : tool || "";
  const args = approval?.arguments;
  const target = tool === "computer_use" ? computerTargetName(args) : "";
  const desktopInput = target && args.target === "desktop"
    && ["click", "double_click", "right_click", "move", "drag", "type", "press", "key", "scroll"].includes(args.action);
  const toolDetails = [toolName, target, desktopInput ? "会使用鼠标或键盘" : ""].filter(Boolean);
  setText($("approval-tool"), toolDetails.join(" · "));
  const reason = approval?.reason || "";
  setText($("approval-reason"), reason);
  $("approval-reason").hidden = !reason;
  const argumentsText = args ? JSON.stringify(args, null, 2) : "";
  const command = approval?.entrypoint === "bash" ? approval.command : "";
  setText($("approval-detail"), command ? "CLI 命令\n" + command + "\n\n执行参数\n" + argumentsText : argumentsText);
  $("approval-mode").disabled = conversationLoading;
  $("approval-mode").setAttribute("aria-busy", String(changingApprovalMode));
  for (const button of actionButtons) {
    if (button.dataset.action === "resume") button.hidden = run?.status !== "paused";
    if (button.dataset.action === "cancel") button.hidden = !active.has(run?.status);
    button.disabled = Boolean(currentAction) || (changingApprovalMode && ["allow", "deny"].includes(button.dataset.action));
    button.setAttribute("aria-busy", String(currentAction === button.dataset.action));
  }
  $("run").querySelector(".run-actions").hidden = !active.has(run?.status);
  updateSendControl();
}

function sidebar(open) {
  $("sidebar").classList.toggle("open", open);
  $("menu").setAttribute("aria-expanded", String(open));
  $("sidebar-scrim").hidden = !open;
}

function select(hash, initial = null) {
  const sessionId = hash.startsWith("session/") ? hash.slice("session/".length) : "";
  const next = idPattern.test(sessionId) || idPattern.test(hash) ? getConversation(hash) :
    draftConversations.get(hash) || newDraft;
  conversation.task = $("task").value;
  conversation = next;
  revision++;
  $("task").value = conversation.task;
  attachmentError();
  showDraftFiles();
  resizeComposer();
  history.replaceState(null, "", conversation.key.startsWith("draft/") ? location.pathname : "#" + conversation.key);
  for (const button of $("history").querySelectorAll("button")) {
    button.setAttribute("aria-current", String(button.dataset.key === conversation.key));
  }
  for (const url of urls) URL.revokeObjectURL(url);
  urls.clear();
  closeFilePanel(false);
  visibleHistoryTurns.disconnect();
  turns.clear();
  $("turns").replaceChildren();
  run = null;
  conversationLoading = !conversation.key.startsWith("draft/") && !initial;
  showApprovalMode();
  scrollToLatest = true;
  sidebar(false);
  notice(conversation.notice?.message, conversation.notice?.source);
  const pending = pendingSubmission();
  if (pending) $("turns").append(pending.placeholder);
  if (initial) showTurns([initial]);
  else {
    showRunControls();
    if (pending) scrollToEnd();
    updateJump();
  }
  refresh();
}

function showHistory() {
  const sessionHistory = [...startedSessions.values(), ...recentSessions].map((item) => {
    const key = "session/" + item.id;
    const status = conversations.get(key)?.submission ? "submitting" : item.status;
    return { key, title: item.title, status };
  });
  const draftHistory = [...draftConversations.values()].reverse().map((item) => ({
    key: item.key, title: item.title, status: item.submission ? "submitting" : "send_failed",
  }));
  const items = [...draftHistory, ...sessionHistory];
  const key = JSON.stringify(items);
  if (historyKey === key) return;
  historyKey = key;
  const nodes = items.map((item) => {
    const button = element("button");
    button.type = "button";
    button.title = item.title + " · " + names[item.status];
    button.dataset.key = item.key;
    button.setAttribute("aria-current", String(item.key === conversation.key));
    const dot = element("i");
    dot.dataset.state = item.status;
    button.append(dot, element("span", item.title));
    button.addEventListener("click", () => {
      const wasOpen = $("sidebar").classList.contains("open");
      if (item.key !== conversation.key) select(item.key);
      else sidebar(false);
      if (wasOpen) $("scroll").focus({ preventScroll: true });
    });
    return button;
  });
  $("history").replaceChildren(...(nodes.length ? nodes : [element("p", "暂无对话")]));
}

function showWorkers(workers) {
  const online = workers.filter((worker) => worker.connected);
  connection(online.length);
  const desktops = workers.filter((worker) => ["macos-desktop", "windows-desktop"].includes(worker.environment)
    && worker.capabilities?.computer === true);
  const browsers = workers.filter((worker) => worker.environment === "cloud-browser");
  const workerSelect = $("worker");
  readyWorkers.clear();
  for (const [mode, candidates] of Object.entries({ desktop: desktops, browser: browsers })) {
    const ready = candidates.some((worker) => worker.connected && !worker.run_id);
    if (ready) readyWorkers.add(mode);
    const availability = ready ? "" : candidates.some((worker) => worker.connected) ? "（忙碌）" : "（离线）";
    const option = [...workerSelect.options].find((item) => item.value === mode);
    setText(option, workerNames[mode] + availability);
  }
  updateSendControl();
}

async function showScreenshot(container, name, id) {
  let url;
  try {
    const response = await request("/v1/runs/" + id + "/artifacts/" + encodeURIComponent(name));
    const blob = await response.blob();
    if (!container.isConnected) return;
    url = URL.createObjectURL(blob);
    const image = element("img", undefined, "trace-image");
    image.alt = "Worker 截图";
    image.src = url;
    await image.decode();
    if (!container.isConnected) return;
    urls.add(url);
    const button = element("button", undefined, "trace-image-open");
    button.type = "button";
    button.title = "查看 Worker 截图";
    button.setAttribute("aria-label", button.title);
    bindFileTrigger(button, { name, source: "preview", size: blob.size, mime_type: blob.type }, id, { localFile: blob });
    button.append(image);
    preserveScroll(() => container.replaceChildren(button), false);
  } catch {
    if (container.isConnected) {
      preserveScroll(() => container.replaceChildren(element("p", "截图读取失败。", "trace-note")), false);
    }
  } finally {
    if (url && !urls.has(url)) URL.revokeObjectURL(url);
  }
}

function fileSize(size) {
  if (size >= 1024 ** 2) return fileSizeFormat.format(size / 1024 ** 2) + " MB";
  if (size >= 1024) return fileSizeFormat.format(size / 1024) + " KB";
  return fileSizeFormat.format(size) + " B";
}

function fileSummary(file) {
  const label = Object.hasOwn(fileSourceNames, file.source) ? fileSourceNames[file.source] : "";
  return [label, fileSize(file.size)].filter(Boolean).join(" · ");
}

function filePreviewType(file) {
  const type = (file.mime_type || file.type || "").split(";")[0].trim().toLowerCase();
  if (imageTypes.has(type)) return type;
  if (type.startsWith("text/") || /(?:[+/](?:json|xml)|application\/x-yaml)$/.test(type)) return "text";
  if ((!type || type === "application/octet-stream") &&
      /\.(?:txt|md|markdown|csv|tsv|log|json|jsonl|xml|yaml|yml|toml|ini|sql)$/i.test(file.name)) return "text";
  return "";
}

async function fileData({ file, runId, localFile }, limit) {
  if (localFile) return limit ? localFile.slice(0, limit) : localFile;
  const path = "/v1/runs/" + encodeURIComponent(runId) + "/files/" + encodeURIComponent(file.id);
  const extraHeaders = limit ? { Range: "bytes=0-" + (limit - 1) } : undefined;
  const response = await request(path, { extraHeaders, timeout: 60000 });
  const data = await response.blob();
  return limit ? data.slice(0, limit) : data;
}

function releaseFilePreview() {
  if (filePreview?.url) URL.revokeObjectURL(filePreview.url);
  filePreview?.trigger.setAttribute("aria-expanded", "false");
  filePreview = null;
}

function syncFilePanelMode() {
  const panel = $("file-panel");
  const overlay = filePanelOverlay.matches && !panel.hidden;
  $("sidebar").inert = overlay;
  document.querySelector("main").inert = overlay;
  $("file-panel-scrim").hidden = !overlay;
  if (overlay) {
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    if (!panel.contains(document.activeElement)) $("close-file").focus({ preventScroll: true });
  } else {
    panel.removeAttribute("role");
    panel.removeAttribute("aria-modal");
  }
}

function toggleFilePanel(open) {
  preserveScroll(() => {
    $("file-panel").hidden = !open;
    document.body.classList.toggle("file-panel-open", open);
    syncFilePanelMode();
    resizeComposer();
  });
}

function closeFilePanel(restoreFocus = true) {
  if (!filePreview) return;
  const trigger = filePreview.trigger;
  releaseFilePreview();
  $("file-preview").replaceChildren();
  $("file-version").replaceChildren();
  toggleFilePanel(false);
  if (restoreFocus) (trigger.getClientRects().length ? trigger : $("scroll")).focus({ preventScroll: true });
}

async function showFile(file, runId, trigger, { localFile, versions = [file] } = {}) {
  const moveFocus = filePreview?.trigger !== trigger;
  releaseFilePreview();
  const preview = { file, runId, trigger, localFile, versions };
  filePreview = preview;
  const type = filePreviewType(file);
  const content = $("file-preview");
  const folderPath = file.source === "output" && file.path !== file.name ? file.path : "";
  setText($("file-panel-title"), file.name);
  setText($("file-panel-meta"), [fileSummary(file), folderPath].filter(Boolean).join(" · "));
  $("file-version-control").hidden = versions.length < 2;
  const versionOptions = versions.length > 1 ? versions.map((version, index) => {
    const option = element("option", "第 " + version.version + " 版" + (index === 0 ? "（最新）" : ""));
    option.value = String(index);
    option.selected = version === file;
    return option;
  }) : [];
  $("file-version").replaceChildren(...versionOptions);
  trigger.setAttribute("aria-expanded", "true");
  $("file-download-error").hidden = true;
  $("download-file").disabled = false;
  content.dataset.kind = type.startsWith("image/") ? "image" : type || "file";
  content.replaceChildren(element("p", type ? "正在读取预览…" : "下载原件后查看文件内容。", "file-preview-note"));
  if ($("file-panel").hidden) toggleFilePanel(true);
  content.parentElement.scrollTop = 0;
  if (moveFocus) $("close-file").focus({ preventScroll: true });
  if (!type) return;
  try {
    const limit = type === "text" && file.size > textPreviewLimit ? textPreviewLimit : undefined;
    const data = await fileData(preview, limit);
    if (filePreview !== preview) return;
    if (!limit) preview.data = data;
    if (type === "text") {
      const text = await data.text();
      if (filePreview !== preview) return;
      const body = text ? element("pre", text, "file-preview-text") : element("p", "文件为空。", "file-preview-note");
      content.replaceChildren(body);
      if (limit) content.append(element("p", "仅显示前 256 KB，可下载查看完整文件。", "file-preview-note"));
      return;
    }
    const image = element("img", undefined, "file-preview-image");
    image.alt = file.name;
    preview.url = URL.createObjectURL(data.slice(0, data.size, type));
    image.src = preview.url;
    await image.decode();
    if (filePreview === preview) content.replaceChildren(image);
  } catch {
    if (filePreview === preview) content.replaceChildren(element("p", "无法读取预览，可下载文件。", "file-preview-note"));
  }
}

function bindFileTrigger(button, file, runId, options = {}) {
  const { localFile, versions = [file] } = options;
  const selected = Boolean(filePreview && filePreview.runId === runId && (localFile
    ? filePreview.localFile === localFile : versions.some((version) => version.id === filePreview.file.id)));
  button.setAttribute("aria-controls", "file-panel");
  button.setAttribute("aria-expanded", String(selected));
  if (selected) filePreview.trigger = button;
  button.addEventListener("click", () => void showFile(file, runId, button, options));
}

function fileCard(file, runId, localFile, remove) {
  const item = element("li", undefined, "file-card");
  const button = element("button", undefined, "file-open");
  button.type = "button";
  button.title = file.name;
  const copy = element("span", undefined, "file-copy");
  copy.append(element("span", file.name, "file-name"), element("span", fileSummary(file), "file-meta"));
  button.append(traceIcon("file"), copy);
  bindFileTrigger(button, file, runId, { localFile });
  item.append(button);
  if (remove) {
    const dismiss = element("button", undefined, "file-remove");
    dismiss.type = "button";
    dismiss.setAttribute("aria-label", "移除 " + file.name);
    dismiss.append(traceIcon("close"));
    dismiss.addEventListener("click", remove);
    item.append(dismiss);
  }
  return item;
}

function attachmentError(message = "") {
  const error = $("attachment-error");
  setText(error, message);
  error.hidden = !message;
}

function showDraftFiles() {
  const container = $("attachment-draft");
  container.hidden = !conversation.files.length;
  $("attach").disabled = Boolean(pendingSubmission());
  container.replaceChildren(...conversation.files.map((entry, index) => fileCard(entry.file, null, entry.file, () => {
    if (pendingSubmission()) return;
    preserveScroll(() => {
      conversation.files = conversation.files.filter((candidate) => candidate !== entry);
      attachmentError();
      showDraftFiles();
    });
    updateSendControl();
    (container.children[Math.min(index, conversation.files.length - 1)]?.querySelector(".file-remove") || $("attach")).focus();
  })));
}

function promptMessage(task) {
  const node = element("div", undefined, "turn-prompt");
  if (task) node.append(element("div", task, "task"));
  const files = element("ul", undefined, "file-list turn-uploads");
  files.setAttribute("aria-label", "上传附件");
  files.hidden = true;
  node.append(files);
  return { node, files };
}

function artifactGrid(groups, runId) {
  const grid = element("ul", undefined, "artifact-grid");
  for (const versions of groups) {
    const file = versions[0];
    const item = element("li", undefined, "artifact-card");
    const button = element("button", undefined, "file-open");
    button.type = "button";
    button.title = file.path || file.name;
    const format = element("span", undefined, "artifact-format");
    const type = filePreviewType(file);
    format.dataset.kind = type.startsWith("image/") ? "image" : type || "file";
    const extension = /\.([a-z\d]{1,5})$/i.exec(file.name)?.[1];
    format.setAttribute("aria-hidden", "true");
    format.append(extension ? extension.toUpperCase() : traceIcon("file"));
    const info = [fileSize(file.size)];
    if (versions.length > 1) info.push(versions.length + " 个版本");
    const copy = element("span", undefined, "file-copy");
    copy.append(element("span", file.name, "file-name"), element("span", info.join(" · "), "file-meta"));
    button.append(format, copy, traceIcon("chevron"));
    bindFileTrigger(button, file, runId, { versions });
    item.append(button);
    grid.append(item);
  }
  return grid;
}

function artifactGroup(files, source, runId, expanded) {
  const byPath = new Map();
  // API order is oldest first, including versions of a path.
  for (const file of [...files].reverse()) {
    const path = file.path || file.id;
    if (!byPath.has(path)) byPath.set(path, []);
    byPath.get(path).push(file);
  }
  const groups = [...byPath.values()];
  const section = element("section", undefined, "artifact-group");
  section.dataset.source = source;
  const visible = source === "preview" ? 6 : groups.length;
  section.append(element("h4", source === "output" ? "交付文件" : fileSourceNames.preview),
    artifactGrid(groups.slice(0, visible), runId));
  if (groups.length > visible) {
    const details = element("details", undefined, "artifact-more");
    details.open = expanded;
    const summary = element("summary");
    summary.append(traceIcon("chevron"), element("span", "其余 " + (groups.length - visible) + " 项预览"));
    details.append(summary, artifactGrid(groups.slice(visible), runId));
    section.append(details);
  }
  return section;
}

function renderTurnFiles(view, value) {
  const files = value.files;
  if (!Array.isArray(files)) return;
  const key = JSON.stringify(files);
  if (view.filesKey === key) return;
  view.filesKey = key;
  const uploads = files.filter((file) => file.source === "upload");
  const outputs = files.filter((file) => file.source === "output");
  const previews = files.filter((file) => file.source === "preview");
  view.uploads.hidden = !uploads.length;
  view.outputs.hidden = !outputs.length && !previews.length;
  view.uploads.replaceChildren(...uploads.map((file) => fileCard(file, value.id)));
  const expanded = Boolean(view.outputFiles.querySelector(".artifact-more[open]"));
  const groups = [];
  if (outputs.length) groups.push(artifactGroup(outputs, "output", value.id, false));
  if (previews.length) groups.push(artifactGroup(previews, "preview", value.id, expanded));
  view.outputFiles.replaceChildren(...groups);
}

function assistantMessage(text) {
  const message = element("div", undefined, "message");
  renderMarkdown(message, text);
  return message;
}

const traceIcons = {
  activity: "M3 12h4l3-7 4 14 3-7h4",
  chevron: "m9 18 6-6-6-6",
  screen: "M4 4h16a1 1 0 0 1 1 1v11H3V5a1 1 0 0 1 1-1ZM3 16v1a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-1M8 21h8m-4-3v3",
  terminal: "m5 7 5 5-5 5m8 0h6",
  message: "M20 11.5a7.5 7.5 0 0 1-7.5 7.5H5l1.8-3.6A7.5 7.5 0 1 1 20 11.5Z",
  file: "M13 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V10ZM13 3v7h7m-11 5h6m-6 3h6",
  folder: "M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z",
  close: "m6 6 12 12M18 6 6 18",
};
const traceActions = {
  screenshot: "截取屏幕", screen_info: "查看屏幕信息", observe: "观察屏幕", navigate: "打开网页", click: "点击",
  double_click: "双击", right_click: "右键点击", move: "移动指针", drag: "拖动",
  type: "输入文字", scroll: "滚动页面", key: "按键", press: "按键", download: "下载文件", upload: "上传文件",
};
const traceCallEvents = new Set(["tool_call", "tool_result", "approval_requested", "approval_decision"]);
const traceTime = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

function traceIcon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const path = document.createElementNS(ns, "path");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "trace-icon");
  svg.setAttribute("aria-hidden", "true");
  path.setAttribute("d", traceIcons[name]);
  svg.append(path);
  return svg;
}

function traceTimeLabel(value) {
  const date = new Date(value);
  const time = element("time", "", "trace-time");
  if (value && !Number.isNaN(date.getTime())) {
    time.dateTime = value;
    time.textContent = traceTime.format(date);
  }
  return time;
}

function traceStepState(step, status) {
  if (step.approved === false) return ["warning", "已拒绝"];
  if (step.result) {
    const result = step.result;
    if (result.timed_out) return ["error", "已超时"];
    if (result.outcome_unknown) return ["warning", "结果未知"];
    if (result.error || result.ok === false) return ["error", "失败"];
    if (typeof result.exit_code === "number" && result.exit_code !== 0) {
      return ["error", "退出码 " + result.exit_code];
    }
    return ["success", "完成"];
  }
  if (status === "cancelled") return ["neutral", "已取消"];
  if (status === "paused") return ["warning", "已暂停"];
  if (!active.has(status)) return ["neutral", "无结果"];
  if (step.started) return ["working", "执行中"];
  if (step.approved) return ["neutral", "已允许"];
  return ["warning", "待审批"];
}

function refreshTraceStep(step, status) {
  const args = step.args || {};
  const result = step.result || {};
  const [tone, label] = traceStepState(step, status);
  const action = Object.hasOwn(traceActions, args.action) ? traceActions[args.action] : "操作电脑";
  const target = computerTargetName(args);
  const computerTitle = target ? target + " · " + action : action;
  step.title.textContent = step.tool === "bash" ? "执行命令" : step.tool === "computer_use" ? computerTitle : step.tool || "工具操作";
  step.caption.textContent = result.error || args.command || args.url || args.path || args.key || args.selector
    || result.title || result.url || result.path || "";
  step.caption.hidden = !step.caption.textContent;
  step.state.textContent = label;
  step.state.dataset.tone = tone;
}

function loadTraceScreenshot(view, step, id) {
  const name = step.result?.artifact;
  if (!view.trace.open || !step.details.open || typeof name !== "string" || step.screenshot.dataset.loaded) return;
  step.screenshot.dataset.loaded = "true";
  step.screenshot.replaceChildren(element("p", "读取截图…", "trace-note"));
  void showScreenshot(step.screenshot, name, id);
}

function createTraceStep(view, data, event, id) {
  const details = element("details", undefined, "trace-step");
  details.dataset.callId = data.call_id;
  const summary = element("summary", undefined, "trace-step-summary");
  const symbol = element("span", undefined, "trace-step-symbol");
  symbol.append(traceIcon(data.tool === "bash" ? "terminal" : "screen"));
  const label = element("span", undefined, "trace-step-text");
  const title = element("span", undefined, "trace-step-title");
  const caption = element("span", undefined, "trace-step-caption");
  label.append(title, caption);
  const state = element("span", undefined, "trace-step-state");
  summary.append(symbol, label, state, traceIcon("chevron"));

  const detail = element("div", undefined, "trace-step-detail");
  const approval = element("p", undefined, "trace-note");
  approval.hidden = true;
  const fields = element("dl", undefined, "trace-fields");
  const command = element("div");
  command.hidden = true;
  command.append(element("dt", "CLI 命令"), element("dd"));
  const args = element("div");
  args.hidden = true;
  args.append(element("dt", "执行参数"), element("dd"));
  const result = element("div");
  result.hidden = true;
  result.append(element("dt", "工具结果"), element("dd"));
  fields.append(command, args, result);
  const screenshot = element("div");
  detail.append(traceTimeLabel(event.created_at), approval, fields, screenshot);
  details.append(summary, detail);
  const step = { details, tool: data.tool, title, caption, state, approval, fields: { command, args, result }, screenshot };
  summary.addEventListener("click", () => { view.tracePreference = true; });
  details.addEventListener("toggle", () => loadTraceScreenshot(view, step, id));
  view.timeline.append(details);
  return step;
}

function showTraceField(field, value) {
  field.lastElementChild.replaceChildren(element("pre", JSON.stringify(value, null, 2)));
  field.hidden = false;
}

function recordTraceCall(view, event, id) {
  const { type, data } = event;
  const step = view.calls.get(data.call_id) || createTraceStep(view, data, event, id);
  view.calls.set(data.call_id, step);
  if (step.fields.command.hidden && data.entrypoint === "bash" && typeof data.command === "string") {
    step.fields.command.lastElementChild.append(element("pre", data.command));
    step.fields.command.hidden = false;
  }
  if (step.args === undefined && data.arguments !== undefined) {
    step.args = data.arguments;
    showTraceField(step.fields.args, step.args);
  }
  let approvalLabel;
  if (type === "tool_call") {
    if (!step.started) view.toolCallCount++;
    step.started = true;
    if (data.auto_approval_reason) {
      approvalLabel = "自动通过";
      step.approvalReason = data.auto_approval_reason;
    }
  } else if (type === "approval_requested" && data.reason) {
    approvalLabel = "待确认";
    step.approvalReason = data.reason;
  } else if (type === "approval_decision") {
    step.approved = data.approved;
    approvalLabel = data.approved ? "审批通过" : "审批拒绝";
  }
  if (approvalLabel) {
    step.approval.textContent = step.approvalReason ? approvalLabel + " · " + step.approvalReason : approvalLabel;
    step.approval.hidden = false;
  }
  if (type === "tool_result") {
    step.result = data.result;
    const planned = view.plannedCalls.get(data.call_id);
    if (step.args === undefined && planned !== undefined) {
      try {
        step.args = JSON.parse(planned);
      } catch {
        step.args = planned;
      }
      showTraceField(step.fields.args, step.args);
    }
    showTraceField(step.fields.result, step.result);
    loadTraceScreenshot(view, step, id);
  }
  refreshTraceStep(step, view.traceStatus);
}

function addTraceEntry(view, icon, title, caption, time) {
  const entry = element("div", undefined, "trace-entry");
  const symbol = element("span", undefined, "trace-step-symbol");
  symbol.append(traceIcon(icon));
  const content = element("div", undefined, "trace-step-text");
  const label = element("span", title, "trace-step-title");
  content.append(label);
  if (caption) content.append(element("span", caption, "trace-step-caption"));
  entry.append(symbol, content, traceTimeLabel(time));
  view.timeline.append(entry);
  return { entry, label };
}

function showRawEvents(view) {
  if (!view.raw.open || view.logRendered === view.eventLog.length) return;
  const batch = document.createDocumentFragment();
  for (const event of view.eventLog.slice(view.logRendered)) {
    const record = element("div", undefined, "trace-record");
    const heading = element("div", undefined, "trace-record-heading");
    heading.append(element("code", event.type), traceTimeLabel(event.created_at));
    record.append(heading, element("pre", JSON.stringify(event.data, null, 2)));
    batch.append(record);
  }
  view.rawEntries.append(batch);
  view.logRendered = view.eventLog.length;
}

function createReasoning(view, generation) {
  const entry = element("details", undefined, "trace-reasoning");
  const summary = element("summary", undefined, "trace-step-summary");
  const symbol = element("span", undefined, "trace-step-symbol");
  symbol.append(traceIcon("activity"));
  const label = element("span", undefined, "trace-step-text");
  const title = element("span", "思考过程", "trace-step-title");
  const preview = element("span", undefined, "trace-step-caption");
  label.append(title, preview);
  const timestamp = traceTimeLabel(generation.time);
  summary.append(symbol, label, timestamp, traceIcon("chevron"));
  const body = element("div", undefined, "trace-step-detail trace-reasoning-body");
  const text = element("div", undefined, "trace-reasoning-text");
  body.append(text);
  entry.append(summary, body);
  view.timeline.insertBefore(entry, generation.update?.entry || null);
  const reasoning = { entry, title, preview, body, text };
  summary.addEventListener("click", () => { reasoning.preference = !entry.open; });
  entry.addEventListener("toggle", updateJump);
  return reasoning;
}

function assistantGeneration(view, event) {
  const id = event.data.generation_id;
  if (!view.generation || view.generation.id !== id) {
    view.generation?.reasoningRow?.entry.remove();
    view.generation?.update?.entry.remove();
    view.generation = { id, time: event.created_at, reasoning: "", content: "" };
  }
  return view.generation;
}

function paintGeneration(view, generation, settled = false) {
  const running = view.traceStatus === "running" && !generation.interrupted;
  const thinking = !settled && running && !generation.content.trim();
  if (generation.reasoning.trim()) {
    const row = generation.reasoningRow ||= createReasoning(view, generation);
    setText(row.title, thinking ? "正在思考" : "思考过程");
    const preview = generation.reasoning.trim().replace(/\s+/g, " ");
    setText(row.preview, thinking ? preview.slice(-160) : preview.slice(0, 160));
    row.entry.dataset.live = String(thinking);
    if (row.preference === undefined) row.entry.open = thinking;
    const atBottom = row.body.scrollHeight - row.body.scrollTop - row.body.clientHeight < 24;
    if (settled) renderMarkdown(row.text, generation.reasoning);
    else setText(row.text, generation.reasoning);
    if (thinking && atBottom) row.body.scrollTop = row.body.scrollHeight;
  }
  if (!settled && view.traceStatus === "completed") {
    generation.update?.entry.remove();
    generation.update = undefined;
  } else if (generation.content.trim()) {
    if (!generation.update) {
      const update = addTraceEntry(view, "message", "助手更新", "", generation.time);
      update.entry.classList.add("trace-update");
      const body = element("div", undefined, "trace-entry-body");
      update.message = element("div", undefined, "message");
      body.append(update.message);
      update.entry.append(body);
      generation.update = update;
    }
    const update = generation.update;
    const live = !settled && running;
    setText(update.label, live ? "助手正在更新" : settled ? "助手更新" : "部分回复");
    update.entry.dataset.live = String(live);
    if (settled) renderMarkdown(update.message, generation.content);
    else setText(update.message, generation.content);
  }
}

function paintRunProgress(view) {
  if (view.traceStatus !== "running") return;
  const progress = view.answer.firstElementChild;
  if (!progress?.classList.contains("turn-progress")) return;
  let [title, description] = progressLabels.running;
  const generation = view.generation;
  if (view.phase === "generating" && generation && !generation.interrupted) {
    if (generation.content.trim()) title = "正在生成内容";
    else if (generation.reasoning.trim()) title = "正在思考";
  } else if (view.phase === "tool") {
    title = "正在执行工具";
    description = view.activeStep?.title.textContent || description;
  } else if (view.phase === "preparing") {
    title = "正在准备工具操作";
  } else if (view.phase === "tool-result") {
    description = "工具已返回，等待模型继续";
  } else if (view.phase === "finishing") {
    title = "正在整理结果";
  } else if (!view.phase && !view.traceLoaded) {
    title = "任务运行中";
  }
  setText(progress.querySelector(".activity-title"), title);
  setText(progress.querySelector(".activity-description"), description);
}

function appendEvents(view, events, id) {
  for (const event of events) {
    if (event.seq <= view.after) continue;
    const { data, type } = event;
    view.after = event.seq;
    view.eventLog.push(event);
    if (type === "assistant_delta") {
      const generation = assistantGeneration(view, event);
      if (typeof data.content === "string") generation.content += data.content;
      if (typeof data.reasoning_content === "string") generation.reasoning += data.reasoning_content;
      view.phase = "generating";
    } else if (type === "assistant") {
      view.modelResponseCount++;
      const generation = assistantGeneration(view, event);
      generation.reasoning = data.message?.reasoning_content ?? generation.reasoning;
      generation.content = data.message?.content ?? "";
      for (const call of data.calls || []) view.plannedCalls.set(call.call_id, call.arguments);
      if (!data.calls?.length && view.traceStatus === "completed") {
        generation.update?.entry.remove();
        generation.content = "";
      }
      paintGeneration(view, generation, true);
      view.generation = null;
      view.phase = data.calls?.length ? "preparing" : "finishing";
      if (!data.calls?.length) view.finalUpdate = generation.update?.entry;
    } else if (type === "skill_review") {
      const labels = { queued: "技能复盘排队中", running: "正在提炼技能", completed: "技能已沉淀", proposed: "已生成技能候选", skipped: "本次无需沉淀技能", failed: "技能复盘失败" };
      view.skillReviewPending = ["queued", "running"].includes(data.status);
      view.skillReview.hidden = false;
      view.skillReview.textContent = (labels[data.status] || "技能复盘") + (data.name ? "：" + data.name : "") + (data.reason ? " · " + data.reason : "");
    } else if (type === "worker_bound") {
      addTraceEntry(view, "screen", "已连接 Worker", data.worker_id, event.created_at);
    } else if (type === "model_retry") {
      view.generation?.reasoningRow?.entry.remove();
      view.generation?.update?.entry.remove();
      view.generation = null;
      addTraceEntry(view, "activity", "模型连接中断，正在重试", `第 ${data.attempt} 次重试`, event.created_at);
    } else if (type === "model_compatibility" && data.reasoning_effort === "none") {
      addTraceEntry(view, "activity", "模型兼容模式", "该模型通过 Chat Completions 调用工具时已自动关闭推理。", event.created_at);
    } else if (data.call_id && traceCallEvents.has(type)) {
      recordTraceCall(view, event, id);
      if (type === "tool_call") {
        view.phase = "tool";
        view.activeStep = view.calls.get(data.call_id);
      } else if (type === "tool_result") {
        view.phase = "tool-result";
        view.activeStep = null;
      }
    } else if (type === "status" && data.status === "paused" && view.generation) {
      view.generation.interrupted = true;
      view.phase = null;
    }
  }
  if (view.generation) paintGeneration(view, view.generation);
  paintRunProgress(view);
  const { toolCallCount, modelResponseCount } = view;
  setText(view.traceCount, toolCallCount + " 次工具调用");
  view.traceCount.hidden = !toolCallCount;
  view.tracePending.hidden = view.timeline.childElementCount > 0;
  view.tracePending.textContent = active.has(view.traceStatus) ? "等待执行步骤…" : "暂无执行步骤";
  view.raw.hidden = !view.eventLog.length;
  setText(view.traceTotals, toolCallCount + " 次工具调用 · " + modelResponseCount + " 轮模型响应");
  showRawEvents(view);
}

async function loadTrace(view, id, force = false) {
  if (!force && !view.trace.open && !active.has(view.traceStatus)) return;
  if (view.loading) {
    view.traceNeedsRefresh ||= force;
    return;
  }
  view.loading = true;
  try {
    const events = await api("/v1/runs/" + id + "/events?after=" + view.after);
    if (!view.trace.isConnected) return;
    if (events.length || !view.traceLoaded || !view.traceError.hidden) {
      preserveScroll(() => {
        appendEvents(view, events, id);
        view.traceError.hidden = true;
        view.traceLoaded = true;
      }, !view.traceOpening);
    }
  } catch (error) {
    if (view.trace.isConnected && (view.traceError.hidden || view.traceError.textContent !== error.message)) {
      preserveScroll(() => {
        view.tracePending.hidden = true;
        view.traceError.textContent = error.message;
        view.traceError.hidden = false;
      }, !view.traceOpening);
    }
  } finally {
    view.loading = false;
    view.traceOpening = false;
    if (view.traceNeedsRefresh && view.trace.isConnected) {
      view.traceNeedsRefresh = false;
      void loadTrace(view, id, true);
    }
  }
}

function updateTrace(view, value) {
  const previous = view.traceStatus;
  if (previous === value.status) return;
  view.traceStatus = value.status;
  view.trace.dataset.state = value.status;
  view.traceStatusLabel.textContent = names[value.status] || "";
  if (view.tracePreference === undefined) view.trace.open = active.has(value.status);
  for (const step of view.calls.values()) refreshTraceStep(step, value.status);
  if (view.traceLoaded) {
    view.tracePending.textContent = active.has(value.status) ? "等待执行步骤…" : "暂无执行步骤";
  }
  if (active.has(previous) && !active.has(value.status)) void loadTrace(view, value.id, true);
}

function createTurn(value) {
  const node = element("article", undefined, "turn");
  node.dataset.runId = value.id;

  const trace = element("details", undefined, "trace");
  const summary = element("summary", undefined, "trace-summary");
  const symbol = element("span", undefined, "trace-symbol");
  symbol.append(traceIcon("activity"));
  const traceCount = element("span", undefined, "trace-count");
  traceCount.hidden = true;
  const traceStatusLabel = element("span", undefined, "trace-status");
  summary.append(symbol, element("span", "运行过程", "trace-title"), traceCount, traceStatusLabel, traceIcon("chevron"));

  const body = element("div", undefined, "trace-body");
  const timeline = element("div", undefined, "trace-timeline");
  const tracePending = element("p", "正在读取记录…", "trace-note");
  const traceError = element("p", undefined, "trace-error");
  traceError.hidden = true;
  const raw = element("details", undefined, "trace-log");
  raw.hidden = true;
  const rawSummary = element("summary");
  const traceTotals = element("span", undefined, "trace-log-count");
  rawSummary.append(traceIcon("chevron"), element("span", "原始事件"), traceTotals);
  const rawEntries = element("div", undefined, "trace-log-entries");
  raw.append(rawSummary, rawEntries);
  body.append(timeline, tracePending, traceError, raw);
  trace.append(summary, body);

  const answer = element("div", undefined, "turn-answer");
  const outputs = element("section", undefined, "turn-outputs");
  outputs.hidden = true;
  const outputFiles = element("div");
  const outputHeading = element("h3", undefined, "artifact-heading");
  outputHeading.append(traceIcon("folder"), "运行产物");
  outputs.append(outputHeading, outputFiles);
  const response = element("div", undefined, "turn-response");
  const skillReview = element("p", undefined, "skill-review-status");
  skillReview.hidden = true;
  skillReview.setAttribute("role", "status");
  response.append(trace, answer, outputs, skillReview);
  const prompt = promptMessage(value.task);
  node.append(prompt.node, response);
  const view = {
    node, trace, timeline, tracePending, traceError, traceCount, traceStatusLabel, answer, skillReview,
    uploads: prompt.files, outputs, outputFiles,
    raw, traceTotals, rawEntries, after: 0, eventLog: [], logRendered: 0, calls: new Map(), plannedCalls: new Map(),
    toolCallCount: 0, modelResponseCount: 0,
  };
  summary.addEventListener("click", () => {
    view.tracePreference = !trace.open;
    view.traceOpening = view.tracePreference;
  });
  rawSummary.addEventListener("click", () => { view.tracePreference = true; });
  raw.addEventListener("toggle", () => preserveScroll(() => showRawEvents(view), false));
  trace.addEventListener("toggle", () => {
    if (!trace.open) return;
    void loadTrace(view, value.id);
    for (const step of view.calls.values()) loadTraceScreenshot(view, step, value.id);
  });
  updateTrace(view, value);
  turns.set(value.id, view);
  if (!active.has(value.status)) visibleHistoryTurns.observe(node);
  return view;
}

function turnProgress(state, error) {
  const [title, description] = progressLabels[state];
  const card = element("div", undefined, "turn-progress");
  card.dataset.state = state;
  card.setAttribute("role", "status");
  const indicator = element("span", undefined, "activity-indicator");
  indicator.setAttribute("aria-hidden", "true");
  indicator.append(element("span"), element("span"), element("span"));
  const copy = element("div", undefined, "activity-copy");
  copy.append(element("span", title, "activity-title"),
    element("span", error || description, "activity-description"));
  card.append(indicator, copy);
  return card;
}

function pendingTurn(task, attachments) {
  const node = element("article", undefined, "turn");
  const answer = element("div", undefined, "turn-answer");
  const prompt = promptMessage(task);
  prompt.files.hidden = !attachments.length;
  prompt.files.append(...attachments.map(({ file }) => fileCard(file, null, file)));
  answer.append(turnProgress("submitting", attachments.length ? "正在上传附件…" : undefined));
  node.append(prompt.node, answer);
  return node;
}

function turnOutcome(value) {
  if (value.status === "cancelled" || value.status === "failed" || (value.error && !active.has(value.status))) {
    const status = element("div", undefined, "turn-status");
    status.dataset.state = value.status;
    status.append(element("strong", value.status === "cancelled" ? "已取消" : "运行失败"));
    if (value.error) status.append(element("span", value.error));
    return status;
  }
  if (active.has(value.status)) {
    if (value.status === "awaiting_approval" && value.approval) return null;
    return turnProgress(value.status, value.error);
  }
  return value.output ? assistantMessage(value.output) : null;
}

function paintTurn(value, index) {
  const container = $("turns");
  const view = turns.get(value.id) || createTurn(value);
  if (index !== undefined) {
    const next = container.children[index] || null;
    if (next !== view.node) container.insertBefore(view.node, next);
  } else if (!view.node.isConnected) {
    container.append(view.node);
  }

  updateTrace(view, value);
  renderTurnFiles(view, value);
  const hasApproval = Boolean(value.approval);
  if (view.output !== value.output || view.error !== value.error || view.status !== value.status ||
      view.hasApproval !== hasApproval) {
    const outcome = turnOutcome(value);
    view.answer.replaceChildren(...(outcome ? [outcome] : []));
    view.output = value.output;
    view.error = value.error;
    view.status = value.status;
    view.hasApproval = hasApproval;
    if (value.status === "completed") view.finalUpdate?.remove();
    if (view.generation) paintGeneration(view, view.generation);
    paintRunProgress(view);
    if (view.trace.open || active.has(value.status)) void loadTrace(view, value.id);
  }
}

function showApprovalMode() {
  const requestedMode = approvalUpdates.get(run?.id)?.mode || pendingSubmission()?.approvalMode;
  const mode = requestedMode || (active.has(run?.status) ? run.approval_mode :
    conversation.approvalMode || run?.approval_mode);
  $("approval-mode").value = Object.hasOwn(approvalHints, mode) ? mode : "auto";
  approvalHint();
}

function showTurns(values) {
  const pending = pendingSubmission();
  if (pending) values = values.slice(0, values.findIndex((value) => value.id === pending.previousRunId) + 1);
  preserveScroll(() => {
    values.forEach((value, index) => paintTurn(value, index));
    run = values[values.length - 1] || null;
    showApprovalMode();
    showRunControls();
  });
  if (run && scrollToLatest) {
    scrollToEnd();
    scrollToLatest = false;
  }
}

function refreshNotice(message, source) {
  if (($("notice").hidden || $("notice").dataset.source === source) && $("notice").textContent !== message) {
    preserveScroll(() => notice(message, source));
  }
}

async function refreshOverview() {
  if (overviewPolling) return;
  overviewPolling = true;
  try {
    const [recent, workers] = await Promise.allSettled([api("/v1/sessions"), api("/v1/workers")]);
    if (workers.status === "fulfilled") showWorkers(workers.value);
    else connection(null);
    if (recent.status === "fulfilled") {
      recentSessions = recent.value;
      for (const item of recentSessions) startedSessions.delete(item.id);
      showHistory();
    }
    const failure = [recent, workers].find((result) => result.status === "rejected");
    refreshNotice(failure?.reason.message || "", "overview");
  } finally {
    overviewPolling = false;
  }
}

async function refreshConversation() {
  if (conversation.key.startsWith("draft/") || detailRevision === revision ||
      (!conversationLoading && (pendingAction() || pendingApprovalMode()))) return;
  const version = revision;
  const key = conversation.key;
  const legacy = idPattern.test(key);
  detailRevision = version;
  try {
    const path = legacy ? "/v1/runs/" + key : "/v1/sessions/" + key.slice("session/".length) + "/runs";
    const values = await api(path);
    if (version !== revision) return;
    if (legacy) {
      select("session/" + values.session_id);
      return;
    }
    conversationLoading = false;
    showTurns(values);
    if (run && active.has(run.status)) {
      const view = turns.get(run.id);
      if (view) void loadTrace(view, run.id);
    }
    refreshNotice("", "detail");
  } catch (error) {
    if (version !== revision) return;
    if (conversationLoading) {
      conversationLoading = false;
      showRunControls();
    }
    refreshNotice(error.message, "detail");
  } finally {
    if (detailRevision === version) detailRevision = -1;
  }
}

function refresh() {
  void refreshOverview();
  void refreshConversation();
}

async function updateApprovalMode(origin, id, mode) {
  const current = approvalUpdates.get(id);
  if (current) {
    current.mode = mode;
    return;
  }
  const update = { mode };
  approvalUpdates.set(id, update);
  if (origin === conversation) {
    revision++;
    showApprovalMode();
    showRunControls();
  }
  const version = revision;
  if (origin.notice?.source === "approval-mode") conversationNotice(origin);
  try {
    let value;
    let requestedMode;
    do {
      requestedMode = update.mode;
      try {
        value = await api("/v1/runs/" + id + "/approval-mode", {
          method: "PATCH", body: { approval_mode: requestedMode },
        });
      } catch (error) {
        if (requestedMode === update.mode) throw error;
      }
    } while (requestedMode !== update.mode);
    if (version === revision && run?.id === id && !pendingAction()) {
      preserveScroll(() => {
        run = value;
        paintTurn(value);
      });
    }
  } catch (error) {
    if (error.status !== 409 && origin.approvalMode === update.mode) origin.approvalMode = undefined;
    conversationNotice(origin, "审批模式未更新：" + error.message, "approval-mode");
  } finally {
    approvalUpdates.delete(id);
    if (origin === conversation) {
      revision++;
      preserveScroll(() => {
        showApprovalMode();
        showRunControls();
      });
    }
    refresh();
  }
}

let editingProviderId = null;
let defaultModelBaseUrl = "https://api.openai.com/v1";

async function loadModel() {
  modelConfig = await api("/ui/providers");
  if (!modelConfig.providers.length) {
    const defaults = await api("/ui/model");
    defaultModelBaseUrl = defaults.base_url || defaultModelBaseUrl;
  }
  return modelConfig;
}

function modelError(message = "") {
  $("model-error").textContent = message;
  $("model-error").hidden = !message;
}

function showProviderList() {
  $("api-key").value = "";
  $("model-form").hidden = true;
  $("provider-list-view").hidden = false;
  modelError();
  const list = $("provider-list");
  list.replaceChildren();
  if (!modelConfig.providers.length) {
    list.append(element("p", "还没有保存供应商，请先添加一个。", "provider-empty"));
    return;
  }
  for (const provider of modelConfig.providers) {
    const card = element("div", undefined, "provider-card");
    const heading = element("div", undefined, "provider-heading");
    heading.append(element("span", provider.name, "provider-name"));
    if (provider.id === modelConfig.active_provider_id) {
      heading.append(element("span", "使用中", "provider-active"));
    }
    const detail = element("p", provider.model + " · " + provider.base_url, "provider-detail");
    const actions = element("div", undefined, "provider-actions");
    function button(label, action) {
      const control = element("button", label);
      control.type = "button";
      control.addEventListener("click", action);
      actions.append(control);
    }
    if (provider.id !== modelConfig.active_provider_id) {
      button("启用", () => void updateProvider(provider, "activate"));
    }
    button("编辑", () => editProvider(provider));
    button("删除", () => void updateProvider(provider, "delete"));
    card.append(heading, detail, actions);
    list.append(card);
  }
}

function editProvider(provider = null) {
  editingProviderId = provider?.id || null;
  $("provider-form-title").textContent = provider ? "编辑供应商" : "添加供应商";
  $("provider-name").value = provider?.name || "";
  $("base-url").value = provider?.base_url || defaultModelBaseUrl;
  $("model-name").value = provider?.model || "";
  $("api-key").value = "";
  $("key-hint").textContent = provider?.has_api_key
    ? "当前地址已保存密钥。留空会保留密钥，修改地址后需要重新输入。"
    : "模型服务允许匿名访问时可留空。";
  $("clear-key").checked = false;
  $("clear-key-label").hidden = !provider?.has_api_key;
  $("provider-list-view").hidden = true;
  $("model-form").hidden = false;
  modelError();
  $("provider-name").focus();
}

async function updateProvider(provider, action) {
  if (action === "delete") {
    const remaining = modelConfig.providers.filter((item) => item.id !== provider.id);
    const next = provider.id === modelConfig.active_provider_id
      ? (remaining.length ? "删除后将启用“" + remaining[0].name + "”。" : "删除后需要重新添加供应商才能发送任务。")
      : "";
    if (!confirm("确定删除供应商“" + provider.name + "”？此操作无法撤销。" + next)) return;
  }
  const buttons = $("provider-list").querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  modelError();
  try {
    const path = "/ui/providers/" + encodeURIComponent(provider.id);
    await api(action === "activate" ? path + "/activate" : path, {
      method: action === "activate" ? "PUT" : "DELETE",
    });
    await loadModel();
    showProviderList();
  } catch (error) {
    modelError(error.message);
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function openSettings(onlyIfMissing = false) {
  try {
    const config = await loadModel();
    if ((!onlyIfMissing || !config.configured) && !$("model-dialog").open) {
      settingsSession++;
      settingsLoaded = {};
      settingsLoading = {};
      selectSettingsTab("providers");
      showProviderList();
      $("model-dialog").showModal();
      if (!config.providers.length) editProvider();
      else if (!config.configured) {
        const current = config.providers.find((provider) => provider.id === config.active_provider_id);
        if (current) editProvider(current);
      }
    }
  } catch (error) {
    notice(error.message);
  }
}

$("add-provider").addEventListener("click", () => editProvider());
$("cancel-provider").addEventListener("click", showProviderList);
const settingsForms = {
  capabilities: { id: "capabilities-form", path: "/ui/capabilities" },
  memory: { id: "memory-settings-form", path: "/ui/memory/settings" },
  feishu: { id: "feishu-settings-form", path: "/v1/settings/feishu" },
};
const settingsTabs = ["providers", ...Object.keys(settingsForms)];
let settingsLoaded = {};
let settingsLoading = {};
let settingsSession = 0;
let activeSettingsTab = "providers";
let savedFeishuConfig = null;
let feishuStatusVersion = 0;
let feishuStatusPending = false;
const feishuRegistrationPath = "/v1/settings/feishu/registration";
let feishuRegistrationId = sessionStorage.getItem("feishu-registration");
let feishuScanPending = false;

function settingsStatus(message = "") {
  $("settings-status").textContent = message;
  $("settings-status").hidden = !message;
}

function hasSavedFeishuSecret() {
  return savedFeishuConfig?.has_app_secret && $("feishu-app-id").value.trim() === savedFeishuConfig.app_id;
}

function updateFeishuSecretHint() {
  const hasSecret = hasSavedFeishuSecret();
  const canClear = hasSecret && !$("feishu-app-secret").value.trim();
  $("feishu-clear-secret-label").hidden = !canClear;
  if (!canClear) $("feishu-clear-secret").checked = false;
  $("feishu-secret-hint").textContent = hasSecret ? "留空会保留当前应用的密钥。" : "在飞书开放平台的应用凭证页面获取。";
}

function showFeishuStatus(data) {
  const badge = $("feishu-status");
  const label = { disabled: "已停用", connecting: "连接中", connected: "已连接", error: "异常" }[data.status] || "状态未知";
  if (badge.textContent !== label) badge.textContent = label;
  badge.dataset.state = data.status;
  badge.hidden = false;
  const error = $("feishu-error");
  const message = data.error || "";
  if (error.textContent !== message) error.textContent = message;
  error.hidden = !message;
}

function showFeishuSettings(data) {
  savedFeishuConfig = data;
  $("feishu-enabled").checked = data.enabled;
  $("feishu-app-id").value = data.app_id;
  $("feishu-app-secret").value = "";
  $("feishu-clear-secret").checked = false;
  $("feishu-open-ids").value = data.allowed_open_ids.join("\n");
  $("feishu-approval-mode").value = data.approval_mode || "manual";
  $("feishu-approval-buttons").checked = Boolean(data.approval_buttons);
  updateFeishuSecretHint();
  showFeishuStatus(data);
  if (!feishuRegistrationId && !feishuScanPending) $("feishu-scan-start").disabled = false;
}

function showFeishuScan(data) {
  const code = $("feishu-scan-code");
  const link = $("feishu-scan-link");
  const status = $("feishu-scan-status");
  const active = ["starting", "waiting", "saving"].includes(data.status);
  const message = {
    starting: "正在生成二维码…",
    waiting: "请用手机飞书扫一扫，并在手机上确认创建。",
    saving: "已授权，正在保存连接…",
    completed: "已保存机器人。请在飞书打开机器人，发送一条单聊消息。",
    cancelled: "已取消扫码配置。",
  };
  status.textContent = data.error || message[data.status] || "";
  status.hidden = !status.textContent;
  if (data.qr_png && code.dataset.registration !== data.id) {
    if (code.src) URL.revokeObjectURL(code.src);
    const bytes = Uint8Array.from(atob(data.qr_png), (byte) => byte.charCodeAt(0));
    code.src = URL.createObjectURL(new Blob([bytes], { type: "image/png" }));
    code.dataset.registration = data.id;
    link.href = data.url;
  } else if (!data.qr_png && code.dataset.registration) {
    URL.revokeObjectURL(code.src);
    code.removeAttribute("src");
    delete code.dataset.registration;
    link.removeAttribute("href");
  }
  code.hidden = link.hidden = !data.qr_png;
  $("feishu-scan-start").disabled = active || !settingsLoaded.feishu;
  $("feishu-scan-cancel").hidden = !active || data.status === "saving";
  if (!active) {
    feishuRegistrationId = null;
    sessionStorage.removeItem("feishu-registration");
  }
  if (data.config) {
    feishuStatusVersion++;
    showFeishuSettings(data.config);
  }
}

async function refreshFeishuScan() {
  if (!feishuRegistrationId || feishuScanPending || activeSettingsTab !== "feishu" || !$("model-dialog").open) return;
  const id = feishuRegistrationId;
  feishuScanPending = true;
  try {
    showFeishuScan(await api(feishuRegistrationPath + "/" + id));
  } catch (error) {
    if (error.status === 404) showFeishuScan({ status: "expired", error: "二维码已失效，请重新生成。" });
  } finally {
    feishuScanPending = false;
  }
}

$("feishu-scan-start").addEventListener("click", async () => {
  if (feishuScanPending || feishuRegistrationId) return;
  if (savedFeishuConfig?.app_id && !window.confirm("扫码创建的机器人会替换当前飞书连接。继续生成二维码？")) return;
  feishuScanPending = true;
  showFeishuScan({ status: "starting" });
  try {
    const data = await api(feishuRegistrationPath, { method: "POST" });
    feishuRegistrationId = data.id;
    sessionStorage.setItem("feishu-registration", data.id);
    showFeishuScan(data);
  } catch (error) {
    showFeishuScan({ status: "error", error: error.message });
  } finally {
    feishuScanPending = false;
    void refreshFeishuScan();
  }
});

$("feishu-scan-cancel").addEventListener("click", async () => {
  if (!feishuRegistrationId || feishuScanPending) return;
  feishuScanPending = true;
  try {
    const path = feishuRegistrationPath + "/" + feishuRegistrationId;
    await request(path, { method: "DELETE" });
    showFeishuScan(await api(path));
  } catch (error) {
    modelError(error.message);
  } finally {
    feishuScanPending = false;
  }
});

async function refreshFeishuStatus() {
  if (
    feishuStatusPending || activeSettingsTab !== "feishu" || !settingsLoaded.feishu ||
    !$("model-dialog").open || document.hidden || $("feishu-settings-form").querySelector("[type=submit]").disabled
  ) return;
  const session = settingsSession;
  const statusVersion = feishuStatusVersion;
  feishuStatusPending = true;
  try {
    const data = await api(settingsForms.feishu.path);
    if (session === settingsSession && statusVersion === feishuStatusVersion) showFeishuStatus(data);
  } catch {
    if (session === settingsSession && statusVersion === feishuStatusVersion) {
      showFeishuStatus({ status: "unknown", error: "暂时无法读取连接状态。" });
    }
  } finally {
    feishuStatusPending = false;
  }
}

async function selectSettingsTab(name, focus = false) {
  activeSettingsTab = name;
  for (const tab of settingsTabs) {
    const selected = tab === name;
    $("tab-" + tab).setAttribute("aria-selected", String(selected));
    $("tab-" + tab).tabIndex = selected ? 0 : -1;
    $("panel-" + tab).hidden = !selected;
  }
  modelError();
  settingsStatus();
  if (focus) $("tab-" + name).focus();
  if (name === "providers" || settingsLoaded[name]) return;

  if (settingsLoading[name]) { settingsStatus("正在读取设置…"); return; }
  settingsLoading[name] = true;
  const session = settingsSession;
  const { id, path } = settingsForms[name];
  const controls = $(id).querySelectorAll("input, textarea, select, button");
  controls.forEach((control) => { control.disabled = true; });
  if (name === "feishu") {
    $("feishu-status").hidden = true;
    $("feishu-error").hidden = true;
  }
  settingsStatus("正在读取设置…");
  try {
    const data = await api(path);
    if (session !== settingsSession) return;
    if (name === "capabilities") {
      $("capability-view").checked = data.view;
      $("capability-register").checked = data.register;
      $("capability-computer-use").checked = data.computer_use;
    } else if (name === "memory") {
      $("memory-enabled").checked = data.enabled;
      $("memory-automatic").checked = data.automatic;
      $("memory-personal").checked = data.personal;
    } else {
      showFeishuSettings(data);
    }
    settingsLoaded[name] = true;
    if (activeSettingsTab === name) settingsStatus();
  } catch (error) {
    if (session === settingsSession && activeSettingsTab === name) {
      settingsStatus();
      modelError(error.message + "，请切换页签后重试。");
    }
  } finally {
    if (session === settingsSession) {
      settingsLoading[name] = false;
      controls.forEach((control) => { control.disabled = !settingsLoaded[name]; });
    }
  }
}

for (const name of settingsTabs) {
  $("tab-" + name).addEventListener("click", () => void selectSettingsTab(name));
  $("tab-" + name).addEventListener("keydown", (event) => {
    const index = settingsTabs.indexOf(name);
    let next;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % settingsTabs.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index + settingsTabs.length - 1) % settingsTabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = settingsTabs.length - 1;
    else return;
    event.preventDefault();
    void selectSettingsTab(settingsTabs[next], true);
  });
}

async function saveSettingsForm(event, name, body) {
  event.preventDefault();
  const session = settingsSession;
  const controls = event.currentTarget.querySelectorAll("input, textarea, select, button");
  const tabs = settingsTabs.map((tab) => $("tab-" + tab));
  controls.forEach((control) => { control.disabled = true; });
  tabs.forEach((tab) => { tab.disabled = true; });
  modelError();
  settingsStatus();
  if (name === "feishu") feishuStatusVersion++;
  try {
    const data = await api(settingsForms[name].path, { method: "PUT", body });
    if (session === settingsSession) {
      if (name === "feishu") showFeishuSettings(data);
      if (activeSettingsTab === name) settingsStatus("更改已保存。");
    }
    notice();
  } catch (error) {
    if (session === settingsSession && activeSettingsTab === name) modelError(error.message);
  } finally {
    tabs.forEach((tab) => { tab.disabled = false; });
    if (session === settingsSession) controls.forEach((control) => { control.disabled = false; });
  }
}

for (const { id } of Object.values(settingsForms)) {
  $(id).addEventListener("change", () => settingsStatus());
}

$("capabilities-form").addEventListener("submit", (event) => void saveSettingsForm(event, "capabilities", {
  view: $("capability-view").checked,
  register: $("capability-register").checked,
  computer_use: $("capability-computer-use").checked,
}));
$("memory-settings-form").addEventListener("submit", (event) => void saveSettingsForm(event, "memory", {
  enabled: $("memory-enabled").checked,
  automatic: $("memory-automatic").checked,
  personal: $("memory-personal").checked,
}));
$("feishu-app-id").addEventListener("input", updateFeishuSecretHint);
$("feishu-app-secret").addEventListener("input", updateFeishuSecretHint);
$("feishu-settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const enabled = $("feishu-enabled").checked;
  const appId = $("feishu-app-id").value.trim();
  const appSecret = $("feishu-app-secret").value.trim();
  const clearSecret = $("feishu-clear-secret").checked;
  const openIds = [...new Set($("feishu-open-ids").value.split(/\r?\n/).map((id) => id.trim()).filter(Boolean))];
  settingsStatus();
  if (openIds.length > 100 || openIds.some((id) => id.length > 128 || !/^ou_[A-Za-z0-9_]+$/.test(id))) {
    modelError("最多填写 100 个有效的用户 Open ID，每行一个，以 ou_ 开头。");
    $("feishu-open-ids").focus();
    return;
  }
  if (enabled && (!appId || !openIds.length || (!appSecret && (!hasSavedFeishuSecret() || clearSecret)))) {
    $("feishu-settings-form").querySelector("details").open = true;
    modelError("启用飞书连接需要填写 App ID、App Secret 和至少一位用户的 Open ID。");
    return;
  }
  void saveSettingsForm(event, "feishu", {
    enabled,
    app_id: appId,
    app_secret: appSecret || (clearSecret ? "" : null),
    allowed_open_ids: openIds,
    approval_mode: $("feishu-approval-mode").value,
    approval_buttons: $("feishu-approval-buttons").checked,
  });
});
$("model-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("model-form").querySelector("[type=submit]");
  submit.disabled = true;
  modelError();
  try {
    const path = "/ui/providers" + (editingProviderId ? "/" + encodeURIComponent(editingProviderId) : "");
    await api(path, {
      method: editingProviderId ? "PUT" : "POST",
      body: {
        name: $("provider-name").value.trim(),
        base_url: $("base-url").value.trim(),
        model: $("model-name").value.trim(),
        api_key: $("api-key").value.trim() || ($("clear-key").checked ? "" : null),
      },
    });
    await loadModel();
    showProviderList();
    notice();
  } catch (error) {
    modelError(error.message);
  } finally {
    submit.disabled = false;
  }
});

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("task");
  const origin = conversation;
  const draft = input.value;
  const task = draft.trim();
  const attachments = origin.files;
  if ($("send").disabled) return;
  const draftVersion = revision;
  if (!modelConfig?.configured) {
    await openSettings(true);
    if (!modelConfig?.configured || draftVersion !== revision || input.value !== draft ||
        origin.files !== attachments || $("send").disabled) return;
  }
  revision++;
  const body = {
    task, worker_target: $("worker").value,
    session_id: origin.key.startsWith("session/") ? origin.key.slice("session/".length) : null,
    approval_mode: $("approval-mode").value,
  };
  const placeholder = pendingTurn(task, attachments);
  const submission = { placeholder, previousRunId: run?.id, approvalMode: body.approval_mode };
  origin.submission = submission;
  if (!body.session_id) {
    origin.title = (task.split("\n")[0] || attachments[0]?.file.name || "新对话").slice(0, 160);
    draftConversations.set(origin.key, origin);
    if (origin === newDraft) newDraft = createDraft();
  }
  input.value = "";
  origin.task = "";
  origin.files = [];
  attachmentError();
  showDraftFiles();
  resizeComposer();
  $("turns").append(placeholder);
  showRunControls();
  conversationNotice(origin);
  showHistory();
  scrollToEnd();
  let value;
  try {
    body.upload_ids = [];
    for (const entry of attachments) {
      if (!entry.upload) {
        entry.upload = await api("/v1/uploads", {
          method: "POST", body: entry.file, timeout: 120000,
          extraHeaders: {
            "Content-Type": "application/octet-stream", "X-File-Name": encodeURIComponent(entry.file.name),
          },
        }).catch((error) => { throw new Error(entry.file.name + " 上传失败。" + error.message); });
      }
      body.upload_ids.push(entry.upload.id);
    }
    if (attachments.length) setText(placeholder.querySelector(".activity-description"), "正在创建任务");
    body.approval_mode = submission.approvalMode;
    value = await api("/v1/runs", { method: "POST", body });
  } catch (error) {
    origin.task = draft;
    origin.files = attachments;
    conversationNotice(origin, error.message);
  }
  origin.submission = null;
  let destination = origin;
  if (value && !body.session_id) {
    draftConversations.delete(origin.key);
    if (!recentSessions.some((item) => item.id === value.session_id)) {
      startedSessions.set(value.session_id, {
        id: value.session_id, title: origin.title, status: value.status,
      });
    }
    destination = getConversation("session/" + value.session_id);
    destination.approvalMode ??= origin.approvalMode;
  }
  if (conversation === origin) {
    if (destination !== origin) select(destination.key, value);
    else {
      revision++;
      preserveScroll(() => {
        placeholder.remove();
        input.value = origin.task;
        if (value) {
          run = value;
          conversationLoading = false;
          paintTurn(value);
        }
        showDraftFiles();
        resizeComposer();
        showRunControls();
      });
    }
  }
  showHistory();
  if (value && submission.approvalMode !== body.approval_mode && active.has(value.status)) {
    void updateApprovalMode(destination, value.id, submission.approvalMode);
  }
  refresh();
});

for (const button of actionButtons) {
  button.addEventListener("click", async () => {
    if (!run || pendingAction()) return;
    const origin = conversation;
    const action = button.dataset.action;
    const id = run.id;
    const approval = action === "allow" || action === "deny";
    if (approval && (!run.approval || pendingApprovalMode())) return;
    const body = approval ? { call_id: run.approval.call_id, approved: action === "allow" } : undefined;
    const version = ++revision;
    actions.set(id, action);
    showRunControls();
    conversationNotice(origin);
    try {
      const value = await api("/v1/runs/" + id + "/" + (approval ? "approve" : action), { method: "POST", body });
      if (version === revision && !pendingApprovalMode()) {
        preserveScroll(() => {
          run = value;
          paintTurn(value);
          showApprovalMode();
          showRunControls();
        });
      }
    } catch (error) {
      conversationNotice(origin, error.message);
    } finally {
      actions.delete(id);
      if (conversation === origin) {
        revision++;
        preserveScroll(showRunControls);
      }
      refresh();
    }
  });
}

$("settings").addEventListener("click", () => void openSettings());
$("close-settings").addEventListener("click", () => $("model-dialog").close());
$("model-dialog").addEventListener("close", () => {
  settingsSession++;
  $("api-key").value = "";
  $("feishu-app-secret").value = "";
});
$("menu").addEventListener("click", () => sidebar(!$("sidebar").classList.contains("open")));
function closeSidebar() {
  sidebar(false);
  $("menu").focus();
}
$("sidebar-scrim").addEventListener("click", closeSidebar);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || $("model-dialog").open) return;
  if (filePreview) {
    event.preventDefault();
    closeFilePanel();
  } else if ($("sidebar").classList.contains("open")) {
    closeSidebar();
  }
});
$("new").addEventListener("click", () => {
  select("");
  $("task").focus();
});
$("example").addEventListener("click", () => {
  $("task").value = "打开 https://example.com，截图并告诉我页面标题。";
  preserveScroll(resizeComposer);
  updateSendControl();
  $("task").focus();
});
$("approval-mode").addEventListener("change", () => {
  const mode = $("approval-mode").value;
  conversation.approvalMode = mode;
  approvalHint();
  const submission = pendingSubmission();
  if (submission) submission.approvalMode = mode;
  else if (active.has(run?.status)) void updateApprovalMode(conversation, run.id, mode);
});
$("worker").addEventListener("change", updateSendControl);
$("attach").addEventListener("click", () => $("attachment-input").click());
$("attachment-input").addEventListener("change", (event) => {
  const input = event.target;
  const picked = Array.from(input.files);
  input.value = "";
  if (!picked.length || pendingSubmission()) return;
  const next = [...conversation.files, ...picked.map((file) => ({ file }))];
  const tooLarge = picked.find((file) => file.size > attachmentLimits.file);
  let error = "";
  if (tooLarge) error = tooLarge.name + " 超过单个附件 20 MB 的限制。";
  else if (next.length > attachmentLimits.count) error = "每条消息最多可添加 8 个附件。";
  else if (next.reduce((size, { file }) => size + file.size, 0) > attachmentLimits.total) {
    error = "每条消息的附件合计不能超过 40 MB。";
  }
  preserveScroll(() => {
    attachmentError(error);
    if (!error) {
      conversation.files = next;
      showDraftFiles();
    }
  });
  updateSendControl();
});
$("close-file").addEventListener("click", () => closeFilePanel());
$("file-panel-scrim").addEventListener("click", () => closeFilePanel());
filePanelOverlay.addEventListener("change", syncFilePanelMode);
$("file-version").addEventListener("change", (event) => {
  const preview = filePreview;
  if (!preview) return;
  const file = preview.versions[Number(event.target.value)];
  if (file) void showFile(file, preview.runId, preview.trigger, preview);
});
$("download-file").addEventListener("click", async (event) => {
  const preview = filePreview;
  if (!preview) return;
  const button = event.currentTarget;
  button.disabled = true;
  $("file-download-error").hidden = true;
  try {
    const data = preview.data || await fileData(preview);
    if (filePreview !== preview) return;
    const url = URL.createObjectURL(data.slice(0, data.size, "application/octet-stream"));
    const link = element("a");
    link.href = url;
    link.download = preview.file.name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    if (filePreview === preview) {
      setText($("file-download-error"), "下载失败。" + error.message);
      $("file-download-error").hidden = false;
    }
  } finally {
    if (filePreview === preview) button.disabled = false;
  }
});
$("task").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  if (!event.repeat && !$("send").disabled) $("composer").requestSubmit($("send"));
});
$("task").addEventListener("input", () => {
  preserveScroll(resizeComposer);
  updateSendControl();
});
$("scroll").addEventListener("scroll", updateJump, { passive: true });
$("jump-latest").addEventListener("click", scrollToEnd);
window.addEventListener("resize", () => preserveScroll(resizeComposer));
window.addEventListener("hashchange", () => select(location.hash.slice(1)));
select(location.hash.slice(1));
void openSettings(true);
setInterval(refresh, 1600);
setInterval(refreshFeishuStatus, 3000);
setInterval(refreshFeishuScan, 1500);
setInterval(() => {
  if (run?.status !== "running" || document.hidden) return;
  const view = turns.get(run.id);
  if (view) void loadTrace(view, run.id);
}, 350);

setInterval(() => {
  if (document.hidden) return;
  for (const [id, view] of turns) {
    if (view.skillReviewPending) void loadTrace(view, id, true);
  }
}, 3000);
