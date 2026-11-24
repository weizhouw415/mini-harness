"use strict";

const $ = (id) => document.getElementById(id);
const names = {
  queued: "排队中", running: "运行中", awaiting_approval: "等待审批",
  paused: "已暂停", completed: "已完成", failed: "失败", cancelled: "已取消",
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
let selected = "";
let legacyRun = "";
let run = null;
let approvalTouched = false;
let approvalRevision = 0;
const approvalUpdates = new Map();
let scrollToLatest = false;
let conversationLoading = false;
const turns = new Map();
const drafts = new Map();
let draftFiles = [];
let filePreview = null;
let revision = 0;
let polling = false;
let submitting = null;
let acting = null;
let historyKey = "";
let workerTouched = false;
let readyWorkers = {};
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
    const message = typeof data.detail === "string" ? data.detail : "请求失败 (" + response.status + ")";
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

function draftKey() {
  return selected ? "session/" + selected : legacyRun;
}

function saveDraft(key, task, files) {
  if (task || files.length) drafts.set(key, { task, files });
  else drafts.delete(key);
}

function pendingSubmission() {
  return submitting?.version === revision ? submitting : null;
}

function pendingAction() {
  return run && acting?.id === run.id ? acting.action : "";
}

function pendingApprovalMode() {
  return Boolean(run && approvalUpdates.has(run.id));
}

function chosenWorker() {
  return readyWorkers[$("worker").value];
}

function setText(node, content) {
  if (node.textContent !== content) node.textContent = content;
}

function updateSendControl() {
  const pending = Boolean(pendingSubmission());
  const button = $("send");
  button.disabled = !($("task").value.trim() || draftFiles.length) || !chosenWorker() || Boolean(submitting) ||
    Boolean((selected || legacyRun) && !run) || active.has(run?.status) || Boolean(pendingAction());
  button.setAttribute("aria-busy", String(pending));
  button.setAttribute("aria-label", pending ? "正在发送" : "发送消息");
  $("task").readOnly = pending;
}

function showRunControls() {
  const pending = pendingSubmission();
  const currentAction = pendingAction();
  const changingApprovalMode = pendingApprovalMode();
  const hasConversation = Boolean(selected || legacyRun || pending);
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
    button.disabled = Boolean(acting) || (changingApprovalMode && ["allow", "deny"].includes(button.dataset.action));
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
  saveDraft(draftKey(), $("task").value, draftFiles);
  revision++;
  selected = idPattern.test(sessionId) ? sessionId : "";
  legacyRun = idPattern.test(hash) ? hash : "";
  const draft = drafts.get(draftKey());
  $("task").value = draft?.task || "";
  draftFiles = draft?.files || [];
  attachmentError();
  showDraftFiles();
  resizeComposer();
  const nextHash = selected ? "#session/" + selected : legacyRun ? "#" + legacyRun : location.pathname;
  history.replaceState(null, "", nextHash);
  for (const button of $("history").querySelectorAll("button")) {
    button.setAttribute("aria-current", String(button.dataset.id === selected));
  }
  for (const url of urls) URL.revokeObjectURL(url);
  urls.clear();
  if ($("file-dialog").open) $("file-dialog").close();
  visibleHistoryTurns.disconnect();
  turns.clear();
  $("turns").replaceChildren();
  run = null;
  conversationLoading = Boolean(selected || legacyRun) && !initial;
  approvalTouched = false;
  $("approval-mode").value = "auto";
  approvalHint();
  scrollToLatest = true;
  sidebar(false);
  notice();
  if (initial) showTurns([initial]);
  else {
    showRunControls();
    updateJump();
  }
  void refresh();
}

function showHistory(sessions) {
  const key = JSON.stringify(sessions);
  if (historyKey === key) return;
  historyKey = key;
  const nodes = sessions.map((item) => {
    const button = element("button");
    button.type = "button";
    button.title = item.title + " · " + names[item.status];
    button.dataset.id = item.id;
    button.setAttribute("aria-current", String(item.id === selected));
    const dot = element("i");
    dot.dataset.state = item.status;
    button.append(dot, element("span", item.title));
    button.addEventListener("click", () => {
      const wasOpen = $("sidebar").classList.contains("open");
      if (item.id !== selected) select("session/" + item.id);
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
    && worker.capabilities?.computer !== false);
  const browsers = workers.filter((worker) => worker.environment === "cloud-browser");
  const workerSelect = $("worker");
  readyWorkers = {};
  for (const [mode, candidates] of Object.entries({ desktop: desktops, browser: browsers })) {
    const ready = candidates.find((worker) => worker.connected && !worker.run_id);
    if (ready) readyWorkers[mode] = ready.worker_id;
    const availability = ready ? "" : candidates.some((worker) => worker.connected) ? "（忙碌）" : "（离线）";
    const option = [...workerSelect.options].find((item) => item.value === mode);
    setText(option, workerNames[mode] + availability);
  }
  if (!workerTouched) workerSelect.value = desktops.length || !browsers.length ? "desktop" : "browser";
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
    preserveScroll(() => container.replaceChildren(image), false);
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

function clearFilePreview() {
  if (filePreview?.url) URL.revokeObjectURL(filePreview.url);
  filePreview = null;
  $("file-preview").replaceChildren();
}

async function showFile(file, runId, localFile) {
  clearFilePreview();
  const preview = { file, runId, localFile };
  filePreview = preview;
  const type = filePreviewType(file);
  const content = $("file-preview");
  const dialog = $("file-dialog");
  setText($("file-dialog-title"), file.name);
  setText($("file-dialog-meta"), fileSummary(file));
  $("file-download-error").hidden = true;
  $("download-file").disabled = false;
  content.append(element("p", type ? "正在读取预览…" : "此文件可下载后查看。", "file-preview-note"));
  if (!dialog.open) dialog.showModal();
  if (!type) return;
  try {
    const limit = type === "text" && file.size > textPreviewLimit ? textPreviewLimit : undefined;
    const data = await fileData(preview, limit);
    if (filePreview !== preview) return;
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

function fileCard(file, runId, localFile, remove) {
  const item = element("li", undefined, "file-card");
  const button = element("button", undefined, "file-open");
  button.type = "button";
  button.title = file.name;
  const copy = element("span", undefined, "file-copy");
  copy.append(element("span", file.name, "file-name"), element("span", fileSummary(file), "file-meta"));
  button.append(traceIcon("file"), copy);
  button.addEventListener("click", () => void showFile(file, runId, localFile));
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
  container.hidden = !draftFiles.length;
  $("attach").disabled = submitting?.origin === draftKey();
  container.replaceChildren(...draftFiles.map((entry, index) => fileCard(entry.file, null, entry.file, () => {
    if (submitting?.origin === draftKey()) return;
    preserveScroll(() => {
      draftFiles = draftFiles.filter((candidate) => candidate !== entry);
      attachmentError();
      showDraftFiles();
    });
    updateSendControl();
    (container.children[Math.min(index, draftFiles.length - 1)]?.querySelector(".file-remove") || $("attach")).focus();
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

function renderTurnFiles(view, value) {
  const files = value.files;
  if (!Array.isArray(files)) return;
  const key = JSON.stringify(files);
  if (view.filesKey === key) return;
  view.filesKey = key;
  const uploads = files.filter((file) => file.source === "upload");
  const outputs = files.filter((file) => Object.hasOwn(fileSourceNames, file.source));
  view.uploads.hidden = !uploads.length;
  view.outputs.hidden = !outputs.length;
  view.uploads.replaceChildren(...uploads.map((file) => fileCard(file, value.id)));
  view.outputFiles.replaceChildren(...outputs.map((file) => fileCard(file, value.id)));
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
    } else if (type === "worker_bound") {
      addTraceEntry(view, "screen", "已连接 Worker", data.worker_id, event.created_at);
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
  const count = view.calls.size;
  view.traceCount.textContent = count ? count + " 次工具操作" : "";
  view.traceCount.hidden = !count;
  view.tracePending.hidden = view.timeline.childElementCount > 0;
  view.tracePending.textContent = active.has(view.traceStatus) ? "等待执行步骤…" : "暂无执行步骤";
  view.raw.hidden = !view.eventLog.length;
  view.rawCount.textContent = view.eventLog.length + " 条";
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
  const rawCount = element("span", undefined, "trace-log-count");
  rawSummary.append(traceIcon("chevron"), element("span", "原始事件"), rawCount);
  const rawEntries = element("div", undefined, "trace-log-entries");
  raw.append(rawSummary, rawEntries);
  body.append(timeline, tracePending, traceError, raw);
  trace.append(summary, body);

  const answer = element("div", undefined, "turn-answer");
  const outputs = element("section", undefined, "turn-outputs");
  outputs.hidden = true;
  const outputFiles = element("ul", undefined, "file-list");
  outputs.append(element("h3", "运行产物"), outputFiles);
  const response = element("div", undefined, "turn-response");
  response.append(trace, answer, outputs);
  const prompt = promptMessage(value.task);
  node.append(prompt.node, response);
  const view = {
    node, trace, timeline, tracePending, traceError, traceCount, traceStatusLabel, answer,
    uploads: prompt.files, outputs, outputFiles,
    raw, rawCount, rawEntries, after: 0, eventLog: [], logRendered: 0, calls: new Map(), plannedCalls: new Map(),
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

function showTurns(values) {
  preserveScroll(() => {
    values.forEach((value, index) => paintTurn(value, index));
    run = values[values.length - 1] || null;
    const requestedMode = approvalUpdates.get(run?.id)?.mode;
    const mode = requestedMode || run?.approval_mode;
    if ((requestedMode || !approvalTouched || active.has(run?.status)) && Object.hasOwn(approvalHints, mode)) {
      $("approval-mode").value = mode;
      approvalHint();
    }
    showRunControls();
  });
  if (run && scrollToLatest) {
    scrollToEnd();
    scrollToLatest = false;
  }
}

async function refresh() {
  if (polling) return;
  polling = true;
  const version = revision;
  const approvalVersion = approvalRevision;
  try {
    const detail = pendingSubmission() || pendingAction() || pendingApprovalMode() ? "" :
      selected ? "/v1/sessions/" + selected + "/runs" : legacyRun ? "/v1/runs/" + legacyRun : "";
    const [recent, workers, current] = await Promise.allSettled([
      api("/v1/sessions"), api("/v1/workers"), ...(detail ? [api(detail)] : []),
    ]);
    if (version !== revision) return;
    if (workers.status === "fulfilled") showWorkers(workers.value);
    else connection(null);
    if (recent.status === "fulfilled") showHistory(recent.value);
    if (approvalVersion !== approvalRevision) return;
    if (current?.status === "fulfilled") {
      if (legacyRun) {
        select("session/" + current.value.session_id);
        return;
      }
      conversationLoading = false;
      showTurns(current.value);
      if (run && active.has(run.status)) {
        const view = turns.get(run.id);
        if (view) void loadTrace(view, run.id);
      }
    } else if (current && conversationLoading) {
      conversationLoading = false;
      showRunControls();
    }
    const failure = [recent, workers, current].find((result) => result?.status === "rejected");
    if ($("notice").hidden || $("notice").dataset.source === "refresh") {
      const message = failure?.reason.message || "";
      if ($("notice").textContent !== message) preserveScroll(() => notice(message, "refresh"));
    }
  } finally {
    polling = false;
    if (version !== revision || approvalVersion !== approvalRevision) void refresh();
  }
}

async function updateApprovalMode(id, mode) {
  const current = approvalUpdates.get(id);
  if (current) {
    current.mode = mode;
    return;
  }
  const update = { mode };
  approvalUpdates.set(id, update);
  approvalRevision++;
  if (run?.id === id) {
    approvalTouched = true;
    $("approval-mode").value = mode;
    approvalHint();
    showRunControls();
    if ($("notice").dataset.source === "approval-mode") notice();
  }
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
    if (run?.id === id) {
      preserveScroll(() => {
        run = value;
        paintTurn(value);
      });
    }
  } catch (error) {
    if (run?.id === id) {
      if (error.status !== 409) {
        approvalTouched = false;
        $("approval-mode").value = Object.hasOwn(approvalHints, run.approval_mode) ? run.approval_mode : "auto";
        approvalHint();
      }
      notice("审批模式未更新：" + error.message, "approval-mode");
    }
  } finally {
    approvalUpdates.delete(id);
    approvalRevision++;
    if (run?.id === id) preserveScroll(showRunControls);
    void refresh();
  }
}

async function loadModel() {
  modelConfig = await api("/ui/model");
  return modelConfig;
}

function showSettings(config) {
  $("base-url").value = config.base_url;
  $("model-name").value = config.model;
  $("api-key").value = "";
  $("key-hint").textContent = config.has_api_key
    ? "当前地址已保存密钥。留空会保留密钥，修改地址后需要重新输入。"
    : "模型服务允许匿名访问时可留空。";
  $("clear-key").checked = false;
  $("clear-key-label").hidden = !config.has_api_key;
  $("model-error").hidden = true;
  if (!$("model-dialog").open) $("model-dialog").showModal();
}

async function openSettings(onlyIfMissing = false) {
  try {
    const config = await loadModel();
    if ((!onlyIfMissing || !config.configured) && !$("model-dialog").open) showSettings(config);
  } catch (error) {
    notice(error.message);
  }
}

$("model-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("model-form").querySelector("[type=submit]");
  submit.disabled = true;
  $("model-error").hidden = true;
  try {
    modelConfig = await api("/ui/model", {
      method: "PUT",
      body: {
        base_url: $("base-url").value.trim(),
        model: $("model-name").value.trim(),
        api_key: $("api-key").value.trim() || ($("clear-key").checked ? "" : null),
      },
    });
    $("api-key").value = "";
    $("model-dialog").close();
    notice();
    sidebar(false);
    $("task").focus();
  } catch (error) {
    $("model-error").textContent = error.message;
    $("model-error").hidden = false;
  } finally {
    submit.disabled = false;
  }
});

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("task");
  const draft = input.value;
  const task = draft.trim();
  const attachments = draftFiles;
  if ($("send").disabled) return;
  const draftVersion = revision;
  if (!modelConfig?.configured) {
    await openSettings(true);
    if (!modelConfig?.configured || draftVersion !== revision || input.value !== draft ||
        draftFiles !== attachments || $("send").disabled) return;
  }
  const version = ++revision;
  const origin = draftKey();
  const body = {
    task, worker_id: chosenWorker(), session_id: selected || null,
    approval_mode: $("approval-mode").value,
  };
  const placeholder = pendingTurn(task, attachments);
  const submission = { version, origin, approvalMode: body.approval_mode };
  submitting = submission;
  input.value = "";
  draftFiles = [];
  attachmentError();
  showDraftFiles();
  resizeComposer();
  $("turns").append(placeholder);
  showRunControls();
  notice();
  scrollToEnd();
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
    const value = await api("/v1/runs", { method: "POST", body });
    if (version === revision) {
      submitting = null;
      placeholder.remove();
      if (!selected) {
        select("session/" + value.session_id, value);
      } else {
        revision++;
        preserveScroll(() => {
          run = value;
          paintTurn(value);
          showRunControls();
        });
      }
    }
    if (submission.approvalMode !== body.approval_mode && active.has(value.status)) {
      void updateApprovalMode(value.id, submission.approvalMode);
    }
  } catch (error) {
    if (origin === draftKey()) {
      if (!input.value) input.value = draft;
      draftFiles = attachments;
    } else {
      saveDraft(origin, drafts.get(origin)?.task || draft, attachments);
    }
    if (version === revision) notice(error.message);
  } finally {
    submitting = null;
    preserveScroll(() => {
      placeholder.remove();
      showDraftFiles();
      resizeComposer();
      showRunControls();
    });
    void refresh();
  }
});

for (const button of actionButtons) {
  button.addEventListener("click", async () => {
    if (acting || !run) return;
    const action = button.dataset.action;
    const id = run.id;
    const approval = action === "allow" || action === "deny";
    if (approval && (!run.approval || pendingApprovalMode())) return;
    const body = approval ? { call_id: run.approval.call_id, approved: action === "allow" } : undefined;
    const version = ++revision;
    const approvalVersion = approvalRevision;
    acting = { id, action };
    showRunControls();
    notice();
    try {
      const value = await api("/v1/runs/" + id + "/" + (approval ? "approve" : action), { method: "POST", body });
      if (version === revision && approvalVersion === approvalRevision && !pendingApprovalMode()) {
        revision++;
        preserveScroll(() => {
          run = value;
          paintTurn(value);
          showRunControls();
        });
      }
    } catch (error) {
      if (version === revision) notice(error.message);
    } finally {
      acting = null;
      preserveScroll(showRunControls);
      void refresh();
    }
  });
}

$("settings").addEventListener("click", () => void openSettings());
$("close-settings").addEventListener("click", () => $("model-dialog").close());
$("model-dialog").addEventListener("close", () => { $("api-key").value = ""; });
$("menu").addEventListener("click", () => sidebar(!$("sidebar").classList.contains("open")));
function closeSidebar() {
  sidebar(false);
  $("menu").focus();
}
$("sidebar-scrim").addEventListener("click", closeSidebar);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("model-dialog").open && !$("file-dialog").open && $("sidebar").classList.contains("open")) {
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
  approvalTouched = true;
  approvalHint();
  const mode = $("approval-mode").value;
  const submission = pendingSubmission();
  if (submission) submission.approvalMode = mode;
  else if (active.has(run?.status)) void updateApprovalMode(run.id, mode);
});
$("worker").addEventListener("change", () => {
  workerTouched = true;
  updateSendControl();
});
$("attach").addEventListener("click", () => $("attachment-input").click());
$("attachment-input").addEventListener("change", (event) => {
  const input = event.target;
  const picked = Array.from(input.files);
  input.value = "";
  if (!picked.length || submitting?.origin === draftKey()) return;
  const next = [...draftFiles, ...picked.map((file) => ({ file }))];
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
      draftFiles = next;
      showDraftFiles();
    }
  });
  updateSendControl();
});
$("close-file").addEventListener("click", () => $("file-dialog").close());
$("file-dialog").addEventListener("close", () => {
  if (!$("file-dialog").open) clearFilePreview();
});
$("download-file").addEventListener("click", async (event) => {
  const preview = filePreview;
  if (!preview) return;
  const button = event.currentTarget;
  button.disabled = true;
  $("file-download-error").hidden = true;
  try {
    const data = await fileData(preview);
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
setInterval(() => {
  if (run?.status !== "running" || document.hidden) return;
  const view = turns.get(run.id);
  if (view) void loadTrace(view, run.id);
}, 350);
