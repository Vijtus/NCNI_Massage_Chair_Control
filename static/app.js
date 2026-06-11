const stateUrl = "/api/state";
const commandUrl = "/api/command";
const mount = document.getElementById("svgMount");
const serialStatus = document.getElementById("serialStatus");
const modeStatus = document.getElementById("modeStatus");
const frameStatus = document.getElementById("frameStatus");
const byteDump = document.getElementById("byteDump");
const syncStatus = document.getElementById("syncStatus");
const driftStatus = document.getElementById("driftStatus");
const timeStatus = document.getElementById("timeStatus");
const timeSource = document.getElementById("timeSource");
const intensityStatus = document.getElementById("intensityStatus");
const speedStatus = document.getElementById("speedStatus");
const footSpeedStatus = document.getElementById("footSpeedStatus");
const historyList = document.getElementById("historyList");
const logList = document.getElementById("logList");
const powerButton = document.querySelector('.power[data-command="power"]');
const commandButtons = new Map(
  [...document.querySelectorAll("[data-command]")].map((button) => [button.dataset.command, button])
);
const HOLD_TO_ADJUST_COMMANDS = new Set(["oparcie_w_gore", "oparcie_w_dol"]);

let svgRoot = null;
let layerMap = new Map();
let textLayerMap = new Map();
let pollTimer = null;
let activeHold = null;

// ── Frame timeline + diff ──────────────────────────────────────────

const frameTimelineEl = document.getElementById("frameTimeline");
const frameDiffEl = document.getElementById("frameDiff");
const timelineBadge = document.getElementById("timelineBadge");
const diffBadge = document.getElementById("diffBadge");
const frameCountEl = document.getElementById("frameCount");

let frameHistory = [];
let frameLastTs = 0;
let selectedBefore = null;
let selectedAfter = null;

function formatFrameTs(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatFrameAge(ts) {
  const age = Date.now() / 1000 - ts;
  if (age < 1) return "now";
  if (age < 60) return `${Math.floor(age)}s`;
  return `${Math.floor(age / 60)}m`;
}

function formatBytes(bytes) {
  return bytes.slice(0, 10).map((b) => b.toString(16).padStart(2, "0").toUpperCase()).join(" ");
}

function formatZones(zones) {
  const active = Object.entries(zones).filter(([, v]) => v).map(([k]) => k.slice(0, 3));
  return active.join(" ") || "—";
}

function refreshFrameSelection() {
  const rows = frameTimelineEl?.querySelectorAll(".frame-row");
  rows?.forEach((row) => {
    const i = parseInt(row.dataset.index, 10);
    row.classList.remove("selected-before", "selected-after");
    if (i === selectedBefore) row.classList.add("selected-before");
    if (i === selectedAfter) row.classList.add("selected-after");
    const bb = row.querySelector(".btn-before");
    const ab = row.querySelector(".btn-after");
    bb?.classList.toggle("active-before", i === selectedBefore);
    ab?.classList.toggle("active-after", i === selectedAfter);
  });
}

function renderFrameRow(frame, index) {
  const row = document.createElement("div");
  row.className = "frame-row";
  row.dataset.index = index;
  const ageClass = frame.age < 3000 ? "fresh" : frame.age < 10000 ? "" : "stale";
  row.innerHTML = `
    <span class="frame-ts">${formatFrameTs(frame.ts)}</span>
    <span class="frame-age ${ageClass}">${formatFrameAge(frame.ts)}</span>
    <span class="frame-sig">${frame.signature}</span>
    <span class="frame-bytes">${formatBytes(frame.raw)}</span>
    <span class="frame-zones">${formatZones(frame.zones)}</span>
    <span class="frame-actions">
      <button class="btn-before" title="Set as before">B</button>
      <button class="btn-after" title="Set as after">A</button>
    </span>
  `;
  row.addEventListener("click", (e) => {
    if (e.target.closest(".frame-actions")) return;
    if (selectedBefore === index && selectedAfter === null) {
      selectedAfter = index;
    } else {
      selectedBefore = index;
      selectedAfter = null;
    }
    refreshFrameSelection();
    renderDiff();
  });
  row.querySelector(".btn-before").addEventListener("click", (e) => {
    e.stopPropagation();
    selectedBefore = index;
    selectedAfter = null;
    refreshFrameSelection();
    renderDiff();
  });
  row.querySelector(".btn-after").addEventListener("click", (e) => {
    e.stopPropagation();
    selectedAfter = index;
    selectedBefore = null;
    refreshFrameSelection();
    renderDiff();
  });
  if (index === selectedBefore) row.classList.add("selected-before");
  if (index === selectedAfter) row.classList.add("selected-after");
  const beforeBtn = row.querySelector(".btn-before");
  const afterBtn = row.querySelector(".btn-after");
  if (index === selectedBefore) beforeBtn.classList.add("active-before");
  if (index === selectedAfter) afterBtn.classList.add("active-after");
  return row;
}

function renderDiff() {
  if (!frameDiffEl) return;
  if (selectedBefore === null && selectedAfter === null) {
    frameDiffEl.innerHTML = '<div class="empty-state">Click B on one frame, A on another to compare</div>';
    diffBadge.textContent = "Select frames";
    return;
  }
  let beforeFrame, afterFrame;
  if (selectedBefore !== null && selectedAfter !== null) {
    beforeFrame = frameHistory[selectedBefore];
    afterFrame = frameHistory[selectedAfter];
  } else if (selectedAfter !== null) {
    beforeFrame = frameHistory[selectedAfter - 1] || frameHistory[selectedAfter];
    afterFrame = frameHistory[selectedAfter];
  } else {
    beforeFrame = frameHistory[selectedBefore];
    afterFrame = frameHistory[selectedBefore + 1] || frameHistory[selectedBefore];
  }
  if (!beforeFrame || !afterFrame) {
    frameDiffEl.innerHTML = '<div class="empty-state">Not enough frames to compare</div>';
    return;
  }
  diffBadge.textContent = "Diff";
  let html = "";
  html += `<div class="diff-frame-label"><span class="label-before">BEFORE</span> <span class="label-ts">${formatFrameTs(beforeFrame.ts)} ${beforeFrame.signature}</span></div>`;
  html += `<div class="diff-frame-label"><span class="label-after">AFTER</span>  <span class="label-ts">${formatFrameTs(afterFrame.ts)} ${afterFrame.signature}</span></div>`;
  html += `<div class="diff-section"><div class="diff-section-title">Parsed Fields</div>`;
  const fields = [
    { key: "mode", label: "Mode" },
    { key: "power_on", label: "Power" },
    { key: "intensity", label: "Intensity" },
    { key: "speed", label: "Speed" },
    { key: "foot_speed", label: "Foot spd" },
  ];
  fields.forEach(({ key, label }) => {
    const bv = beforeFrame[key];
    const av = afterFrame[key];
    const bStr = typeof bv === "boolean" ? (bv ? "ON" : "OFF") : String(bv);
    const aStr = typeof av === "boolean" ? (av ? "ON" : "OFF") : String(av);
    const changed = bStr !== aStr;
    html += `<div class="diff-row">
      <span class="diff-field">${label}</span>
      <span class="diff-value">
        <span class="${changed ? "val-changed" : "val-same"}">${bStr}</span>
        <span class="diff-arrow">→</span>
        <span class="${changed ? "val-changed" : "val-same"}">${aStr}</span>
      </span>
    </div>`;
  });
  html += `<div class="diff-section"><div class="diff-section-title">Zones</div>`;
  const zoneKeys = Object.keys(beforeFrame.zones);
  zoneKeys.forEach((zk) => {
    const bv = beforeFrame.zones[zk];
    const av = afterFrame.zones[zk];
    const bStr = bv ? "●" : "○";
    const aStr = av ? "●" : "○";
    const changed = bv !== av;
    html += `<div class="diff-row">
      <span class="diff-field">${zk}</span>
      <span class="diff-value">
        <span class="${changed ? "val-changed" : "val-same"}">${bStr}</span>
        <span class="diff-arrow">→</span>
        <span class="${changed ? "val-changed" : "val-same"}">${aStr}</span>
      </span>
    </div>`;
  });
  html += `</div>`;
  html += `<div class="diff-section"><div class="diff-section-title">Byte Diff (first 16)</div>`;
  const maxBytes = Math.max(beforeFrame.raw.length, afterFrame.raw.length);
  for (let i = 0; i < Math.min(maxBytes, 16); i++) {
    const bv = beforeFrame.raw[i];
    const av = afterFrame.raw[i];
    const bHex = bv !== undefined ? bv.toString(16).padStart(2, "0").toUpperCase() : "--";
    const aHex = av !== undefined ? av.toString(16).padStart(2, "0").toUpperCase() : "--";
    const same = bv === av;
    const cls = same ? "same" : "changed";
    html += `<div class="diff-byte-row">
      <span class="diff-byte-pos">[${i}]</span>
      <span class="diff-byte-val ${cls}">${bHex}</span>
      <span class="diff-arrow">→</span>
      <span class="diff-byte-val ${cls}">${aHex}</span>
    </div>`;
  }
  html += `</div>`;
  frameDiffEl.innerHTML = html;
}

async function pollFrames() {
  try {
    const params = frameLastTs ? `?since=${frameLastTs}` : "";
    const res = await fetch(`/api/frames${params}`, { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    if (!data.frames || data.frames.length === 0) return;
    // Append new frames to existing history (don't replace)
    frameHistory = [...frameHistory, ...data.frames];
    frameLastTs = data.frames[data.frames.length - 1]?.ts || frameLastTs;
    // Trim to limit
    if (frameHistory.length > 200) {
      frameHistory = frameHistory.slice(-200);
    }
    if (frameCountEl) frameCountEl.textContent = frameHistory.length;
    if (timelineBadge) timelineBadge.textContent = `${frameHistory.length} frames`;
    // Always re-render when new frames arrive
    renderFrameTimeline();
    if (selectedBefore === null && selectedAfter === null && frameHistory.length >= 2) {
      selectedBefore = frameHistory.length - 2;
      selectedAfter = frameHistory.length - 1;
      refreshFrameSelection();
      renderDiff();
    } else if (selectedBefore !== null || selectedAfter !== null) {
      refreshFrameSelection();
      renderDiff();
    }
  } catch (err) {
    console.warn("Frame poll failed:", err);
  }
}

function renderFrameTimeline() {
  if (!frameTimelineEl) return;
  frameTimelineEl.innerHTML = "";
  if (frameHistory.length === 0) {
    frameTimelineEl.innerHTML = '<div class="empty-state">No frames received yet</div>';
    return;
  }
  for (let i = frameHistory.length - 1; i >= 0; i--) {
    const frame = frameHistory[i];
    frame.age = Date.now() / 1000 - frame.ts;
    const row = renderFrameRow(frame, i);
    frameTimelineEl.appendChild(row);
  }
  frameTimelineEl.scrollTop = frameTimelineEl.scrollHeight;
}

// ── End frame timeline + diff ──────────────────────────────────────

async function loadDisplay() {
  const response = await fetch("/display.svg", { cache: "no-store" });
  const markup = await response.text();
  mount.innerHTML = markup;
  svgRoot = mount.querySelector("svg");
  if (!svgRoot) {
    throw new Error("Display SVG did not load");
  }

  layerMap = new Map();
  textLayerMap = new Map();
  svgRoot.querySelectorAll("[data-layer]").forEach((element) => {
    const label = element.getAttribute("data-layer");
    if (!label) return;
    layerMap.set(label, element);
    if (element.tagName.toLowerCase() === "text") {
      textLayerMap.set(label, element);
    }
  });
}

function setVisibleLayers(visibleLabels, textValues) {
  if (!svgRoot) return;
  const visibleSet = new Set(visibleLabels);
  layerMap.forEach((element, label) => {
    element.style.display = visibleSet.has(label) ? "" : "none";
  });
  Object.entries(textValues || {}).forEach(([label, value]) => {
    const textElement = textLayerMap.get(label);
    if (textElement) {
      textElement.textContent = value;
    }
  });
}

function heatIsExplicitlyOff(state) {
  const zoneHeat = state.zones && state.zones.ogrzewanie;
  const buttonHeat =
    state.buttons &&
    state.buttons.ogrzewanie &&
    state.buttons.ogrzewanie.active;
  if (zoneHeat === true || buttonHeat === true) return false;
  return zoneHeat === false || buttonHeat === false;
}

function visibleLayersFromState(state) {
  const visible = new Set((state.layers && state.layers.visible) || []);
  if (heatIsExplicitlyOff(state)) {
    visible.delete("Ogrzewanie");
  }
  return visible;
}

function renderList(target, items, formatter) {
  target.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("li");
    empty.textContent = "No data";
    target.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const node = document.createElement("li");
    node.textContent = formatter(item);
    target.appendChild(node);
  });
}

function renderState(state) {
  if (serialStatus) {
    serialStatus.textContent = state.connected
      ? `${state.port_name}${state.listening ? " - listen on" : ""}`
      : state.port_name;
  }
  if (modeStatus) {
    modeStatus.textContent = state.power_on
      ? `${state.mode.toUpperCase()}${state.auto_profile ? ` - ${state.auto_profile}` : ""}`
      : "OFF";
  }
  if (frameStatus) {
    frameStatus.textContent = state.raw_frame
      ? `${state.frame_signature} - ${state.raw_frame.map((value) => value.toString(16).padStart(2, "0")).join(" ")}`
      : "No frame";
  }
  if (byteDump) {
    byteDump.textContent = `Byte 3..6: ${state.bytes_3_to_6.map((value) => value.toString(16).padStart(2, "0")).join(" ")}`;
  }
  if (syncStatus) {
    const mode = state.sync.frame_live ? "live board frames" : "semantic fallback";
    const readiness = state.board_ready ? "board ready" : "waiting for firmware";
    const stale = state.frame_stale ? " - FRAME STALE" : "";
    syncStatus.textContent = `${mode} - ${readiness}${stale}`;
  }
  if (driftStatus) {
    const drift = Array.isArray(state.drift) ? state.drift : [];
    driftStatus.textContent = drift.length
      ? drift.map((item) => `${item.field}: model=${item.model} frame=${item.frame}`).join("\n")
      : "No persistent drift";
  }
  if (timeStatus) {
    timeStatus.textContent = state.time_text || "--";
  }
  if (timeSource) {
    timeSource.textContent = state.sync.time_source;
  }
  if (intensityStatus) {
    intensityStatus.textContent = `L${state.levels.intensity}`;
  }
  if (speedStatus) {
    speedStatus.textContent = `L${state.levels.speed}`;
  }
  if (footSpeedStatus) {
    footSpeedStatus.textContent = `L${state.levels.foot_speed}`;
  }

  setVisibleLayers(visibleLayersFromState(state), state.layers.text);

  Object.entries(state.buttons).forEach(([command, meta]) => {
    const button = commandButtons.get(command);
    if (!button) return;
    button.classList.toggle("is-active", Boolean(meta.active));
    button.classList.toggle("is-blocked", Boolean(meta.blocked));
    button.classList.toggle("is-failed", Boolean(meta.failed));
    button.disabled = Boolean(meta.blocked);
  });
  if (activeHold) {
    setLocalHoldActive(activeHold.button, true);
  }

  powerButton.classList.toggle("is-active", Boolean(state.power_on));

  if (historyList) {
    renderList(
      historyList,
      state.command_history.slice().reverse(),
      (item) => `${new Date(item.at * 1000).toLocaleTimeString()}  ${item.command}`
    );
  }
  if (logList) {
    renderList(logList, state.backend_log.slice().reverse(), (item) => item);
  }
}

async function refreshState() {
  const response = await fetch(stateUrl, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`State fetch failed: ${response.status}`);
  }
  const state = await response.json();
  renderState(state);
  scheduleNextPoll(state.poll_hint_ms || 350);
}

function scheduleNextPoll(delayMs) {
  window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(() => {
    Promise.all([
      refreshState().catch(handleError),
      pollFrames().catch(() => {}),
    ]);
  }, delayMs);
}

async function postCommandPayload(payload, options = {}) {
  const response = await fetch(commandUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest",
    },
    body: JSON.stringify(payload),
    keepalive: Boolean(options.keepalive),
  });
  if (!response.ok) {
    throw new Error(`Command failed: ${response.status}`);
  }
  const result = await response.json();
  renderState(result.state);
  return result;
}

async function sendCommand(command) {
  return postCommandPayload({ command });
}

function makeHoldId(command) {
  const random =
    window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${command}-${random}`;
}

function setLocalHoldActive(button, active) {
  if (!button) return;
  button.classList.toggle("is-active", active);
  button.disabled = false;
}

function sendHoldStop(hold, reason, attempt = 0) {
  postCommandPayload(
    {
      command: hold.command,
      action: "hold_stop",
      hold_id: hold.holdId,
    },
    { keepalive: true }
  ).catch((error) => {
    if (attempt >= 2) {
      handleError(error);
      return;
    }
    window.setTimeout(() => sendHoldStop(hold, reason, attempt + 1), 160 * (attempt + 1));
  });
}

function endActiveHold(reason) {
  const hold = activeHold;
  if (!hold || hold.stopped) return;
  hold.stopped = true;
  activeHold = null;
  try {
    if (
      hold.button &&
      typeof hold.button.releasePointerCapture === "function" &&
      hold.pointerId !== null &&
      hold.button.hasPointerCapture(hold.pointerId)
    ) {
      hold.button.releasePointerCapture(hold.pointerId);
    }
  } catch (_error) {}
  setLocalHoldActive(hold.button, false);
  sendHoldStop(hold, reason);
}

function beginHold(button, command, event) {
  if (activeHold || button.disabled) return;
  const pointerId = event && "pointerId" in event ? event.pointerId : null;
  const hold = {
    command,
    button,
    holdId: makeHoldId(command),
    pointerId,
    stopped: false,
  };
  activeHold = hold;
  setLocalHoldActive(button, true);
  try {
    if (pointerId !== null && typeof button.setPointerCapture === "function") {
      button.setPointerCapture(pointerId);
    }
  } catch (_error) {}
  postCommandPayload({
    command,
    action: "hold_start",
    hold_id: hold.holdId,
  }).then((result) => {
    if (!result.ok && activeHold === hold) {
      activeHold = null;
      setLocalHoldActive(button, false);
    }
  }).catch((error) => {
    if (activeHold === hold) {
      activeHold = null;
      setLocalHoldActive(button, false);
    }
    handleError(error);
  });
}

function handleError(error) {
  console.error(error);
  if (serialStatus) {
    serialStatus.textContent = `Error: ${error.message}`;
  }
  scheduleNextPoll(1200);
}

document.addEventListener("pointerdown", (event) => {
  if (event.button !== undefined && event.button !== 0) return;
  const button = event.target.closest("[data-command]");
  if (!button) return;
  const command = button.dataset.command;
  if (!HOLD_TO_ADJUST_COMMANDS.has(command)) return;
  event.preventDefault();
  beginHold(button, command, event);
});

document.addEventListener("pointerup", (event) => {
  if (!activeHold) return;
  if (activeHold.pointerId !== null && event.pointerId !== activeHold.pointerId) {
    return;
  }
  event.preventDefault();
  endActiveHold("pointerup");
});

document.addEventListener("pointercancel", (event) => {
  if (!activeHold) return;
  if (activeHold.pointerId !== null && event.pointerId !== activeHold.pointerId) {
    return;
  }
  event.preventDefault();
  endActiveHold("pointercancel");
});

document.addEventListener("pointermove", (event) => {
  if (!activeHold || event.pointerType !== "mouse") return;
  if (activeHold.pointerId !== null && event.pointerId !== activeHold.pointerId) {
    return;
  }
  const rect = activeHold.button.getBoundingClientRect();
  if (
    event.clientX < rect.left ||
    event.clientX > rect.right ||
    event.clientY < rect.top ||
    event.clientY > rect.bottom
  ) {
    endActiveHold("pointermove-outside");
  }
});

document.addEventListener("pointerleave", (event) => {
  if (!activeHold || event.target !== activeHold.button) return;
  endActiveHold("pointerleave");
});

document.addEventListener("lostpointercapture", (event) => {
  if (!activeHold || event.target !== activeHold.button) return;
  endActiveHold("lostpointercapture");
});

window.addEventListener("blur", () => endActiveHold("blur"));
window.addEventListener("pagehide", () => endActiveHold("pagehide"));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) endActiveHold("visibilitychange");
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-command]");
  if (!button) return;
  const command = button.dataset.command;
  if (HOLD_TO_ADJUST_COMMANDS.has(command)) {
    event.preventDefault();
    return;
  }
  sendCommand(command).catch(handleError);
});

Promise.resolve()
  .then(loadDisplay)
  .then(refreshState)
  .catch(handleError);
