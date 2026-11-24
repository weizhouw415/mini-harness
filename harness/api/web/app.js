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
  running: ["正在运行", "运行有进展时会自动更新"],
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

async function request(path, { method = "GET", body } = {}) {
  const headers = { "X-Harness-UI": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store", signal: AbortSignal.timeout(12000),
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
  button.disabled = !$("task").value.trim() || !chosenWorker() || Boolean(submitting) ||
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
  setText($("approval-detail"), args ? JSON.stringify(args, null, 2) : "");
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
  const oldDraft = $("task").value;
  if (oldDraft) drafts.set(draftKey(), oldDraft);
  else drafts.delete(draftKey());
  revision++;
  selected = idPattern.test(sessionId) ? sessionId : "";
  legacyRun = idPattern.test(hash) ? hash : "";
  $("task").value = drafts.get(draftKey()) || "";
  resizeComposer();
  const nextHash = selected ? "#session/" + selected : legacyRun ? "#" + legacyRun : location.pathname;
  history.replaceState(null, "", nextHash);
  for (const button of $("history").querySelectorAll("button")) {
    button.setAttribute("aria-current", String(button.dataset.id === selected));
  }
  for (const url of urls) URL.revokeObjectURL(url);
  urls.clear();
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
};
const traceActions = {
  screenshot: "截取屏幕", observe: "观察屏幕", navigate: "打开网页", click: "点击",
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
  const args = element("div");
  args.hidden = true;
  args.append(element("dt", "执行参数"), element("dd"));
  const result = element("div");
  result.hidden = true;
  result.append(element("dt", "工具结果"), element("dd"));
  fields.append(args, result);
  const screenshot = element("div");
  detail.append(traceTimeLabel(event.created_at), approval, fields, screenshot);
  details.append(summary, detail);
  const step = { details, tool: data.tool, title, caption, state, approval, fields: { args, result }, screenshot };
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

function addTraceEntry(view, icon, title, caption, time, message) {
  const entry = element("div", undefined, "trace-entry");
  const symbol = element("span", undefined, "trace-step-symbol");
  symbol.append(traceIcon(icon));
  const content = element("div", undefined, "trace-step-text");
  content.append(element("span", title, "trace-step-title"));
  if (caption) content.append(element("span", caption, "trace-step-caption"));
  entry.append(symbol, content, traceTimeLabel(time));
  if (message) {
    const body = element("div", undefined, "trace-entry-body");
    body.append(assistantMessage(message));
    entry.append(body);
  }
  view.timeline.append(entry);
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

function appendReasoning(view, content) {
  if (typeof content !== "string" || !content.trim()) return;
  const segment = element("div", undefined, "reasoning-segment");
  renderMarkdown(segment, content);
  view.reasoningBody.append(segment);
  view.reasoningPreview.textContent = content.trim();
  view.reasoning.hidden = false;
}

function appendEvents(view, events, id) {
  for (const event of events) {
    const { data, type } = event;
    view.after = event.seq;
    view.eventLog.push(event);
    if (type === "assistant") {
      appendReasoning(view, data.message?.reasoning_content);
      for (const call of data.calls || []) view.plannedCalls.set(call.call_id, call.arguments);
      if (data.message?.content && data.calls?.length) {
        addTraceEntry(view, "message", "助手更新", "", event.created_at, data.message.content);
      }
    } else if (type === "worker_bound") {
      addTraceEntry(view, "screen", "已连接 Worker", data.worker_id, event.created_at);
    } else if (data.call_id && traceCallEvents.has(type)) {
      recordTraceCall(view, event, id);
    }
  }
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

  const reasoning = element("details", undefined, "reasoning");
  reasoning.hidden = true;
  const reasoningSummary = element("summary", undefined, "reasoning-summary");
  const reasoningSymbol = element("span", undefined, "reasoning-symbol");
  reasoningSymbol.append(traceIcon("message"));
  const reasoningPreview = element("span", undefined, "reasoning-preview");
  reasoningPreview.setAttribute("aria-hidden", "true");
  reasoningSummary.append(reasoningSymbol, element("span", "思考过程", "reasoning-title"),
    reasoningPreview, traceIcon("chevron"));
  const reasoningBody = element("div", undefined, "reasoning-body");
  reasoning.append(reasoningSummary, reasoningBody);
  reasoning.addEventListener("toggle", updateJump);

  const trace = element("details", undefined, "trace");
  const summary = element("summary", undefined, "trace-summary");
  const symbol = element("span", undefined, "trace-symbol");
  symbol.append(traceIcon("activity"));
  const traceCount = element("span", undefined, "trace-count");
  traceCount.hidden = true;
  const traceStatusLabel = element("span", undefined, "trace-status");
  summary.append(symbol, element("span", "运行轨迹", "trace-title"), traceCount, traceStatusLabel, traceIcon("chevron"));

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
  const response = element("div", undefined, "turn-response");
  response.append(reasoning, trace, answer);
  node.append(element("div", value.task, "task"), response);
  const view = {
    node, trace, timeline, tracePending, traceError, traceCount, traceStatusLabel, answer,
    reasoning, reasoningBody, reasoningPreview,
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
  const indicator = element("span", undefined, "activity-indicator");
  indicator.setAttribute("aria-hidden", "true");
  indicator.append(element("span"), element("span"), element("span"));
  const copy = element("div", undefined, "activity-copy");
  copy.append(element("span", title, "activity-title"),
    element("span", error || description, "activity-description"));
  card.append(indicator, copy);
  return card;
}

function pendingTurn(task) {
  const node = element("article", undefined, "turn");
  const answer = element("div", undefined, "turn-answer");
  answer.append(turnProgress("submitting"));
  node.append(element("div", task, "task"), answer);
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
  const hasApproval = Boolean(value.approval);
  if (view.output !== value.output || view.error !== value.error || view.status !== value.status ||
      view.hasApproval !== hasApproval) {
    const outcome = turnOutcome(value);
    view.answer.replaceChildren(...(outcome ? [outcome] : []));
    view.output = value.output;
    view.error = value.error;
    view.status = value.status;
    view.hasApproval = hasApproval;
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
  if (!task || $("send").disabled) return;
  const draftVersion = revision;
  if (!modelConfig?.configured) {
    await openSettings(true);
    if (!modelConfig?.configured || draftVersion !== revision || input.value !== draft || $("send").disabled) return;
  }
  const version = ++revision;
  const origin = draftKey();
  const body = {
    task, worker_id: chosenWorker(), session_id: selected || null,
    approval_mode: $("approval-mode").value,
  };
  const placeholder = pendingTurn(task);
  const submission = { version, approvalMode: body.approval_mode };
  submitting = submission;
  input.value = "";
  resizeComposer();
  $("turns").append(placeholder);
  showRunControls();
  notice();
  scrollToEnd();
  try {
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
    if (origin === draftKey() && !input.value) input.value = draft;
    else if (origin !== draftKey() && !drafts.has(origin)) drafts.set(origin, draft);
    if (version === revision) notice(error.message);
  } finally {
    submitting = null;
    preserveScroll(() => {
      placeholder.remove();
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
  if (event.key === "Escape" && !$("model-dialog").open && $("sidebar").classList.contains("open")) {
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
$("task").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  if (!event.repeat && !$("send").disabled && $("task").value.trim()) $("composer").requestSubmit($("send"));
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
