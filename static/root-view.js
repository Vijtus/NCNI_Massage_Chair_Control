(() => {
  const root = document;
  const screen = root.querySelector(".screen");
  const screenSvg = screen ? screen.querySelector("svg") : null;
  const powerButton = root.querySelector(".power");
  const leftButtons = Array.from(root.querySelectorAll(".stack--left .btn"));
  const rightButtons = Array.from(root.querySelectorAll(".stack--right .btn"));
  const bottomButtons = Array.from(root.querySelectorAll(".bottom-grid .btn"));
  const orientationQuery = window.matchMedia("(orientation: portrait)");

  const DOM = {
    power: powerButton,
    ramiona: leftButtons[0],
    przedramiona: leftButtons[1],
    nogi: leftButtons[2],
    sila_nacisku_plus: leftButtons[3],
    sila_nacisku_minus: leftButtons[4],
    masaz_posladkow: leftButtons[5],
    szyja: rightButtons[0],
    do_przodu_do_tylu_2: rightButtons[1],
    plecy_i_talia: rightButtons[2],
    do_przodu_do_tylu_1: rightButtons[3],
    predkosc_plus: rightButtons[4],
    predkosc_minus: rightButtons[5],
    masaz_stop: bottomButtons[0],
    pauza: bottomButtons[1],
    czas: bottomButtons[2],
    grawitacja_zero: bottomButtons[3],
    oparcie_w_gore: bottomButtons[4],
    predkosc_masazu_stop: bottomButtons[5],
    ogrzewanie: bottomButtons[6],
    masaz_calego_ciala: bottomButtons[7],
    tryb_automatyczny: bottomButtons[8],
    oparcie_w_dol: bottomButtons[9]
  };

  const MANAGED_LAYERS = [
    "Background",
    "Body",
    "Ramiona",
    "Przedramiona",
    "Nogi",
    "Masaz_Posladkow",
    "Masaz_Stop",
    "Ogrzewanie",
    "Szyja",
    "? Szyja",
    "Plecy_i_talia",
    "? Plecy_i_talia",
    "Sila_nacisku-TEXT",
    "Sila_nacisku-LVL1",
    "Sila_nacisku-LVL2",
    "Sila_nacisku-LVL3",
    "PredkoscTEXT",
    "Predkosc-LVL1",
    "Predkosc-LVL2",
    "Predkosc-LVL3",
    "Predkosc_masazu_stop",
    "Predkosc_masazu_stop-LVL1",
    "Predkosc_masazu_stop-LVL2",
    "Predkosc_masazu_stop-LVL3",
    "Czas-TEXT",
    "Czas-NUMBER",
    "SHAPE_CHECK-TEXT",
    "Tryb_manualny",
    "Tryb_automatyczny",
    "Tryb_automatyczny-A",
    "Tryb_automatyczny-B",
    "Tryb_automatyczny-C",
    "Tryb_automatyczny-D"
  ];

  const buttonToCommand = new Map(
    Object.entries(DOM)
      .filter(([command, button]) => command !== "power" && button)
      .map(([command, button]) => [button, command])
  );
  const HOLD_TO_ADJUST_COMMANDS = new Set(["oparcie_w_gore", "oparcie_w_dol"]);

  const svgLayers = new Map();
  let pollTimer = null;
  let lastEtag = null;
  let lastState = null;
  let activeHold = null;
  const statusChip = document.createElement("div");
  const style = document.createElement("style");
  const networkDom = {
    panelAddress: root.getElementById("panelAddress"),
    lanAddress: root.getElementById("lanAddress"),
    lanIp: root.getElementById("lanIp"),
    panelPort: root.getElementById("panelPort"),
    debugLink: root.getElementById("debugLink"),
    serialLink: root.getElementById("serialLink"),
    connectionNote: root.getElementById("connectionNote"),
    panelQr: root.getElementById("panelQr"),
  };

  const helpPill = root.getElementById("helpPill");
  const helpModal = root.getElementById("helpModal");
  const helpClose = root.getElementById("helpClose");

  function openHelp() {
    if (!helpModal) return;
    helpModal.hidden = false;
    if (helpPill) helpPill.setAttribute("aria-expanded", "true");
    if (helpClose) helpClose.focus();
  }

  function closeHelp() {
    if (!helpModal) return;
    helpModal.hidden = true;
    if (helpPill) {
      helpPill.setAttribute("aria-expanded", "false");
      helpPill.focus();
    }
  }

  if (helpPill) {
    helpPill.addEventListener("click", () => {
      if (helpModal && !helpModal.hidden) closeHelp();
      else openHelp();
    });
  }
  if (helpClose) helpClose.addEventListener("click", closeHelp);
  if (helpModal) {
    helpModal.addEventListener("click", (event) => {
      if (event.target === helpModal) closeHelp();
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && helpModal && !helpModal.hidden) closeHelp();
  });

  // ── Label autofit ────────────────────────────────────────────────
  // Some Polish labels overflow on real Android Chrome landscape.
  // Long-label classes already shrink via CSS, but we measure each
  // .label after layout and add .is-autofit if the text still
  // overflows its button shape. Re-runs on resize / orientation /
  // after fonts settle.
  const labelNodes = Array.from(document.querySelectorAll(".label"));
  function fitLabels() {
    for (const label of labelNodes) {
      label.classList.remove("is-autofit");
    }
    // Read after the next paint so the browser has the final box sizes.
    requestAnimationFrame(() => {
      for (const label of labelNodes) {
        const overflow =
          label.scrollWidth > label.clientWidth + 1 ||
          label.scrollHeight > label.clientHeight + 1;
        if (overflow) label.classList.add("is-autofit");
      }
    });
  }
  window.addEventListener("resize", fitLabels, { passive: true });
  window.addEventListener("orientationchange", fitLabels, { passive: true });
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(fitLabels).catch(() => fitLabels());
  } else {
    setTimeout(fitLabels, 60);
  }

  function syncViewportOrientation() {
    root.documentElement.classList.toggle("is-portrait", orientationQuery.matches);
    root.documentElement.classList.toggle("is-landscape", !orientationQuery.matches);
  }

  function lockLandscapeBestEffort() {
    const orientation = window.screen && window.screen.orientation;
    if (!orientation || typeof orientation.lock !== "function") return;
    orientation.lock("landscape").catch(() => {});
  }

  syncViewportOrientation();
  if (typeof orientationQuery.addEventListener === "function") {
    orientationQuery.addEventListener("change", syncViewportOrientation);
  } else if (typeof orientationQuery.addListener === "function") {
    orientationQuery.addListener(syncViewportOrientation);
  }
  window.addEventListener("resize", syncViewportOrientation, { passive: true });
  window.addEventListener("orientationchange", syncViewportOrientation, {
    passive: true,
  });
  document.addEventListener("pointerdown", lockLandscapeBestEffort, { once: true });

  style.textContent = `
    .bridge-warning-chip {
      position: fixed;
      inset-block-start: max(10px, env(safe-area-inset-top));
      inset-inline-start: 50%;
      z-index: 20;
      max-inline-size: min(92vw, 760px);
      transform: translateX(-50%);
      padding: 10px 14px;
      border: 1px solid rgba(255, 70, 95, 0.78);
      border-radius: 999px;
      background: rgba(40, 0, 8, 0.92);
      color: #fff;
      font: 700 0.88rem/1.25 Arial, sans-serif;
      letter-spacing: 0.02em;
      text-align: center;
      box-shadow: 0 14px 40px rgba(0, 0, 0, 0.35);
      pointer-events: none;
    }
    .bridge-warning-chip[hidden] { display: none; }
    .btn.is-failed .shape__fill,
    .power.is-failed .power__fill {
      fill: #ff1744 !important;
    }
  `;
  document.head.appendChild(style);
  statusChip.className = "bridge-warning-chip";
  statusChip.hidden = true;
  document.body.appendChild(statusChip);

  function readLayerLabel(element) {
    return (
      element.getAttribute("data-layer") ||
      element.getAttribute("inkscape:label") ||
      element.getAttribute("label") ||
      Array.from(element.attributes || []).find((attribute) =>
        attribute.name.endsWith(":label")
      )?.value ||
      ""
    );
  }

  function buildSvgLayerMap() {
    if (!screenSvg) return;
    screenSvg.querySelectorAll("*").forEach((element) => {
      const label = readLayerLabel(element);
      if (!label) return;
      if (!svgLayers.has(label)) {
        svgLayers.set(label, []);
      }
      svgLayers.get(label).push(element);
    });
  }

  function withLayer(label, callback) {
    const elements = svgLayers.get(label) || [];
    elements.forEach(callback);
  }

  function setLayerVisible(label, visible) {
    withLayer(label, (element) => {
      element.style.display = visible ? "" : "none";
    });
  }

  function setSvgTextContent(element, value) {
    const tspans = Array.from(element.querySelectorAll("tspan"));
    if (!tspans.length) {
      element.textContent = String(value);
      return;
    }
    tspans.forEach((tspan, index) => {
      tspan.textContent = index === tspans.length - 1 ? String(value) : "";
    });
  }

  function setLayerText(label, value) {
    withLayer(label, (element) => {
      setSvgTextContent(element, value);
    });
  }

  function expandCumulativeLevelLayers(visible) {
    const expanded = new Set(visible);
    [
      "Sila_nacisku-LVL",
      "Predkosc-LVL",
      "Predkosc_masazu_stop-LVL"
    ].forEach((prefix) => {
      for (let level = 3; level >= 1; level -= 1) {
        if (expanded.has(`${prefix}${level}`)) {
          for (let fill = 1; fill < level; fill += 1) {
            expanded.add(`${prefix}${fill}`);
          }
        }
      }
    });
    return expanded;
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

  function renderLayers(state) {
    const visible = expandCumulativeLevelLayers(visibleLayersFromState(state));
    MANAGED_LAYERS.forEach((label) => {
      setLayerVisible(label, visible.has(label));
    });
    const textLayers = (state.layers && state.layers.text) || {};
    Object.entries(textLayers).forEach(([label, value]) => {
      setLayerText(label, value);
    });
  }

  function paintBlocked(button, blocked) {
    button.setAttribute("aria-disabled", String(blocked));
    button.dataset.blocked = String(blocked);
    button.style.opacity = blocked ? "0.38" : "";
    button.style.filter = blocked ? "grayscale(1)" : "";
    button.style.cursor = blocked ? "not-allowed" : "";
  }

  function renderButtons(state) {
    if (DOM.power) {
      const powerMeta = state.buttons ? state.buttons.power : null;
      DOM.power.classList.toggle("is-active", !!state.power_on);
      DOM.power.classList.toggle("is-failed", !!(powerMeta && powerMeta.failed));
      DOM.power.setAttribute("aria-pressed", String(!!state.power_on));
      paintBlocked(DOM.power, !!(powerMeta && powerMeta.blocked));
    }

    Object.entries(DOM).forEach(([command, button]) => {
      if (!button || command === "power") return;
      const meta = state.buttons ? state.buttons[command] : null;
      const active = !!(meta && meta.active);
      const blocked = !!(meta && meta.blocked);
      button.classList.toggle("is-active", active);
      button.classList.toggle("is-failed", !!(meta && meta.failed));
      button.setAttribute("aria-pressed", String(active));
      paintBlocked(button, blocked);
    });
  }

  function renderWarnings(state) {
    const warnings = [];
    if (state.frame_stale) {
      warnings.push("Frame stale");
    }
    if (Array.isArray(state.drift) && state.drift.length) {
      warnings.push(`Drift: ${state.drift.map((item) => item.field).join(", ")}`);
    }
    if (state.last_error) {
      warnings.push(state.last_error);
    }
    const unverifiedCount = Array.isArray(state.unverified_commands)
      ? state.unverified_commands.length
      : 0;
    if (unverifiedCount > 0) {
      const recent = state.unverified_commands
        .slice(-3)
        .map((item) => item.command)
        .join(", ");
      warnings.push(`Unverified: ${unverifiedCount} (${recent})`);
    }
    statusChip.textContent = warnings.join(" | ");
    statusChip.hidden = warnings.length === 0;
  }

  function render(state) {
    renderLayers(state);
    renderButtons(state);
    if (activeHold) {
      setLocalHoldActive(activeHold.button, true);
    }
    renderWarnings(state);
    if (screen) {
      screen.classList.add("is-ready");
    }
    window.__chairBridgeState = state;
  }

  function setText(node, value) {
    if (node) node.textContent = value;
  }

  function setLink(node, href, label) {
    if (!node) return;
    node.href = href;
    node.textContent = label || href;
  }

  async function refreshNetworkInfo() {
    try {
      const response = await fetch("/api/network", { cache: "no-store" });
      if (!response.ok) return;
      const network = await response.json();
      const panelUrl = network.public_url || network.lan_url || "/";
      const lanUrl = network.lan_url || panelUrl;
      setLink(networkDom.panelAddress, panelUrl, panelUrl);
      setText(networkDom.lanAddress, network.local_only ? "Local only" : lanUrl);
      setText(networkDom.lanIp, network.lan_ip || "--");
      setText(networkDom.panelPort, String(network.port || "--"));
      setLink(networkDom.debugLink, network.debug_url || "/debug", "Strona pomocy");
      setLink(networkDom.serialLink, network.serial_url || "/debug", "Odczyt");
      setText(
        networkDom.connectionNote,
        network.local_only ? "Tryb tylko lokalny." : "To samo Wi-Fi/LAN wymagane."
      );
      if (networkDom.panelQr) {
        networkDom.panelQr.hidden = network.qr_available === false;
      }
    } catch (error) {
      console.warn("Network info unavailable:", error);
    }
  }

  async function copyTextForTarget(targetId) {
    const target = root.getElementById(targetId);
    if (!target) return false;
    const value =
      target.tagName === "A"
        ? target.getAttribute("href")
        : target.value || target.textContent;
    if (!value) return false;
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (_error) {
      const textarea = document.createElement("textarea");
      textarea.value = value;
      textarea.setAttribute("readonly", "readonly");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      return copied;
    }
  }

  async function refreshState() {
    const headers = lastEtag ? { "If-None-Match": lastEtag } : {};
    const response = await fetch("/api/state", { cache: "no-store", headers });
    if (response.status === 304) {
      const hint = (lastState && lastState.poll_hint_ms) || 350;
      scheduleNextPoll(hint);
      return;
    }
    if (!response.ok) {
      throw new Error(`State fetch failed: ${response.status}`);
    }
    lastEtag = response.headers.get("ETag");
    const state = await response.json();
    lastState = state;
    render(state);
    scheduleNextPoll(state.poll_hint_ms || 350);
  }

  function scheduleNextPoll(delayMs) {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(() => {
      refreshState().catch(handleError);
    }, delayMs);
  }

  async function postCommandPayload(payload, options = {}) {
    const response = await fetch("/api/command", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest"
      },
      body: JSON.stringify(payload),
      keepalive: Boolean(options.keepalive)
    });
    if (!response.ok) {
      throw new Error(`Command failed: ${response.status}`);
    }
    const result = await response.json();
    lastState = result.state;
    lastEtag = null;
    render(result.state);
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
    button.setAttribute("aria-pressed", String(active));
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
    if (activeHold || button.dataset.blocked === "true") return;
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
      if (
        pointerId !== null &&
        typeof button.setPointerCapture === "function"
      ) {
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
    scheduleNextPoll(1200);
  }

  buildSvgLayerMap();

  root.addEventListener("pointerdown", (event) => {
    if (event.button !== undefined && event.button !== 0) return;
    const button = event.target.closest(".js-toggle");
    if (!button || button === DOM.power) return;
    const command = buttonToCommand.get(button);
    if (!HOLD_TO_ADJUST_COMMANDS.has(command)) return;
    event.preventDefault();
    beginHold(button, command, event);
  });

  root.addEventListener("pointerup", (event) => {
    if (!activeHold) return;
    if (activeHold.pointerId !== null && event.pointerId !== activeHold.pointerId) {
      return;
    }
    event.preventDefault();
    endActiveHold("pointerup");
  });

  root.addEventListener("pointercancel", (event) => {
    if (!activeHold) return;
    if (activeHold.pointerId !== null && event.pointerId !== activeHold.pointerId) {
      return;
    }
    event.preventDefault();
    endActiveHold("pointercancel");
  });

  root.addEventListener("pointermove", (event) => {
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

  root.addEventListener("pointerleave", (event) => {
    if (!activeHold || event.target !== activeHold.button) return;
    endActiveHold("pointerleave");
  });

  root.addEventListener("lostpointercapture", (event) => {
    if (!activeHold || event.target !== activeHold.button) return;
    endActiveHold("lostpointercapture");
  });

  window.addEventListener("blur", () => endActiveHold("blur"));
  window.addEventListener("pagehide", () => endActiveHold("pagehide"));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) endActiveHold("visibilitychange");
  });

  root.addEventListener("click", (event) => {
    const copyButton = event.target.closest(".copy-btn");
    if (copyButton) {
      copyTextForTarget(copyButton.dataset.copyTarget).then((copied) => {
        copyButton.textContent = copied ? "Skopiowano" : "Kopiuj";
        window.setTimeout(() => {
          copyButton.textContent = "Kopiuj";
        }, 900);
      });
      return;
    }

    const button = event.target.closest(".js-toggle");
    if (!button) return;

    if (button === DOM.power) {
      sendCommand("power").catch(handleError);
      return;
    }

    const command = buttonToCommand.get(button);
    if (!command) return;
    if (HOLD_TO_ADJUST_COMMANDS.has(command)) {
      event.preventDefault();
      return;
    }
    sendCommand(command).catch(handleError);
  });

  refreshNetworkInfo();
  refreshState().catch(handleError);
})();
