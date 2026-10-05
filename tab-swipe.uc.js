// ==UserScript==
// @name QWQC Two-Finger Tab Swipe
// @description Progressive two-finger touchscreen tab switching with live page previews.
// @author qwqc
// ==/UserScript==

(() => {
  "use strict";

  const INSTANCE_KEY = "__qwqcZenTabSwipe";
  const LEFT_PREF = "browser.gesture.swipe.left";
  const RIGHT_PREF = "browser.gesture.swipe.right";
  const PREF_BRANCH = "mod.qwqc.tab_swipe.";
  const PREFS = {
    enabled: `${PREF_BRANCH}enabled`,
    reverse: `${PREF_BRANCH}reverse`,
    protect: `${PREF_BRANCH}protect`,
    debug: `${PREF_BRANCH}debug`
  };
  const DEFAULTS = { enabled: true, reverse: false, protect: true, debug: false };
  const FULL_DRAG_DISTANCE = 0.46;
  const POLL_MS = 16;
  const ANIM_MS = 210;
  const THUMBNAIL_CACHE_LIMIT = 10;
  const HTML_NS = "http://www.w3.org/1999/xhtml";

  const { PageThumbs } = ChromeUtils.importESModule("resource://gre/modules/PageThumbs.sys.mjs");

  function getPref(name, fallback) {
    try {
      const type = Services.prefs.getPrefType(name);
      if (type === Services.prefs.PREF_BOOL) return Services.prefs.getBoolPref(name);
      if (type === Services.prefs.PREF_INT) return Services.prefs.getIntPref(name);
      if (type === Services.prefs.PREF_STRING) return Services.prefs.getStringPref(name);
    } catch (_) {}
    return fallback;
  }

  function createController() {
    const configPath = PathUtils.join(PathUtils.profileDir, "chrome", "qwqc-tab-swipe-config.json");
    const statePath = PathUtils.join(PathUtils.profileDir, "chrome", "qwqc-tab-swipe-state.json");
    const original = {
      left: Services.prefs.getStringPref(LEFT_PREF, "Browser:BackOrBackDuplicate"),
      right: Services.prefs.getStringPref(RIGHT_PREF, "Browser:ForwardOrForwardDuplicate")
    };
    const config = { ...DEFAULTS };
    let prefObserver = null;
    let gestureObserver = null;
    let applying = false;
    let destroyed = false;
    let pollTimer = 0;
    let polling = false;
    let lastStateMtime = -1;
    let lastStateSeq = -1;
    let session = null;
    const thumbnailUrls = new Map();
    const thumbnailCaptures = new Map();
    let prewarmTimer = 0;

    const log = (...args) => {
      if (config.debug) console.debug("[QWQC Tab Swipe]", ...args);
    };

    function readConfig() {
      config.enabled = Boolean(getPref(PREFS.enabled, DEFAULTS.enabled));
      config.reverse = Boolean(getPref(PREFS.reverse, DEFAULTS.reverse));
      config.protect = Boolean(getPref(PREFS.protect, DEFAULTS.protect));
      config.debug = Boolean(getPref(PREFS.debug, DEFAULTS.debug));
    }

    async function writeTouchscreenConfig(reason) {
      try {
        await IOUtils.writeJSON(configPath, {
          enabled: config.enabled,
          reverse: config.reverse,
          updatedAt: Date.now(),
          reason
        });
        Services.prefs.setBoolPref("qwqc.tab_swipe.runtime.touchscreen_config", true);
      } catch (error) {
        Services.prefs.setStringPref("qwqc.tab_swipe.runtime.error", String(error));
        log("touchscreen config write failed", error);
      }
    }

    function desired() {
      // The host touchpad observer owns horizontal tab gestures. Leaving these
      // native commands active would make Firefox switch immediately underneath
      // the progressive preview. Pinch preferences are intentionally untouched.
      return { left: "", right: "" };
    }

    function setMapping(left, right, reason) {
      applying = true;
      try {
        if (Services.prefs.getStringPref(LEFT_PREF, "") !== left) Services.prefs.setStringPref(LEFT_PREF, left);
        if (Services.prefs.getStringPref(RIGHT_PREF, "") !== right) Services.prefs.setStringPref(RIGHT_PREF, right);
        Services.prefs.setStringPref("qwqc.tab_swipe.runtime.left", left);
        Services.prefs.setStringPref("qwqc.tab_swipe.runtime.right", right);
        Services.prefs.setStringPref("qwqc.tab_swipe.runtime.last_reason", reason);
      } finally {
        applying = false;
      }
    }

    function applyMapping(reason = "apply") {
      if (destroyed) return;
      if (!config.enabled) {
        setMapping(original.left, original.right, `${reason}:disabled`);
        return;
      }
      const map = desired();
      setMapping(map.left, map.right, reason);
    }

    function html(tag) {
      return document.createElementNS(HTML_NS, tag);
    }

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value));
    }

    function blockUnderlyingWheel(event) {
      if (!session || session.ending) return;
      // Once the host has classified this as a horizontal tab drag, the raw
      // libinput scroll stream must not move the page underneath the preview.
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
    }

    function visibleTabs() {
      try {
        return Array.from(gBrowser.visibleTabs || []).filter(tab => !tab.hidden && !tab.closing);
      } catch (_) {
        return [];
      }
    }

    function targetForPhysicalSign(startTab, physicalSign) {
      if (!physicalSign) return null;
      const tabs = visibleTabs();
      const index = tabs.indexOf(startTab);
      if (index < 0) return null;
      const logicalStep = (config.reverse ? 1 : -1) * physicalSign;
      return tabs[index + logicalStep] || null;
    }

    function tabTitle(tab, fallback) {
      return tab?.label || tab?.getAttribute?.("label") || fallback;
    }

    function applyThumbnailToPanel(panel, tab, url, animateFallback = true) {
      if (!panel || !url || panel._qwqcTab !== tab) return;
      panel.style.backgroundImage = `url("${url}")`;
      const fallback = panel._qwqcFallback;
      if (!fallback) return;
      if (!animateFallback || fallback.style.display === "none") {
        fallback.style.display = "none";
        fallback.style.opacity = "0";
        return;
      }
      fallback.style.transition = "opacity 80ms linear";
      fallback.style.opacity = "0";
      window.setTimeout(() => {
        if (panel.isConnected && panel._qwqcTab === tab && panel._qwqcFallback === fallback) {
          fallback.style.display = "none";
        }
      }, 90);
    }

    function makePanel(tab, fallbackTitle) {
      const panel = html("div");
      panel.className = "qwqc-tab-swipe-panel";
      Object.assign(panel.style, {
        position: "absolute",
        inset: "0",
        overflow: "hidden",
        backgroundColor: "rgb(20 20 20)",
        backgroundPosition: "center",
        backgroundRepeat: "no-repeat",
        backgroundSize: "cover",
        transform: "translate3d(0,0,0)",
        willChange: "transform",
        borderRadius: "inherit"
      });

      const fallback = html("div");
      fallback.className = "qwqc-tab-swipe-fallback";
      Object.assign(fallback.style, {
        position: "absolute",
        inset: "0",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexDirection: "column",
        gap: "10px",
        padding: "36px",
        color: "rgba(255,255,255,.88)",
        background: "linear-gradient(145deg, rgb(23 23 23), rgb(10 10 10))",
        font: "500 16px system-ui, sans-serif",
        textAlign: "center"
      });

      const icon = tab?.image || tab?.getAttribute?.("image");
      if (icon) {
        const img = html("img");
        img.src = icon;
        Object.assign(img.style, { width: "32px", height: "32px", borderRadius: "8px" });
        fallback.append(img);
      }

      const label = html("div");
      label.textContent = tabTitle(tab, fallbackTitle);
      fallback.append(label);
      panel.append(fallback);
      panel._qwqcFallback = fallback;
      panel._qwqcTab = tab;

      const cachedUrl = tab ? thumbnailUrls.get(tab) : null;
      if (cachedUrl) {
        thumbnailUrls.delete(tab);
        thumbnailUrls.set(tab, cachedUrl);
        applyThumbnailToPanel(panel, tab, cachedUrl, false);
        Services.prefs.setIntPref(
          "qwqc.tab_swipe.runtime.thumbnail_cache_hits",
          Services.prefs.getIntPref("qwqc.tab_swipe.runtime.thumbnail_cache_hits", 0) + 1
        );
      } else if (tab) {
        Services.prefs.setIntPref(
          "qwqc.tab_swipe.runtime.thumbnail_cache_misses",
          Services.prefs.getIntPref("qwqc.tab_swipe.runtime.thumbnail_cache_misses", 0) + 1
        );
      }
      return panel;
    }

    async function captureThumbnail(tab, panel = null) {
      if (!tab?.linkedBrowser || destroyed) return null;
      const hadCached = thumbnailUrls.has(tab);

      let capture = thumbnailCaptures.get(tab);
      if (!capture) {
        capture = (async () => {
          let blob = null;
          try {
            const rect = panel?.isConnected
              ? panel.getBoundingClientRect()
              : gBrowser?.selectedBrowser?.getBoundingClientRect?.();
            const canvas = html("canvas");
            const cssWidth = Math.max(320, rect?.width || 960);
            const cssHeight = Math.max(240, rect?.height || 600);
            const scale = Math.min(1.35, window.devicePixelRatio || 1);
            canvas.width = Math.max(640, Math.min(1440, Math.round(cssWidth * scale)));
            canvas.height = Math.max(420, Math.round(canvas.width * (cssHeight / cssWidth)));
            await PageThumbs.captureTabPreviewThumbnail(tab.linkedBrowser, canvas);
            blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
          } catch (previewError) {
            log("tab-preview capture fallback", tabTitle(tab, "tab"), previewError);
            blob = await PageThumbs.captureToBlob(tab.linkedBrowser, {
              fullViewport: true,
              targetWidth: 1280,
              preserveAspectRatio: true
            });
          }

          if (!blob || destroyed) return null;
          const url = URL.createObjectURL(blob);
          const previousUrl = thumbnailUrls.get(tab);
          thumbnailUrls.delete(tab);
          thumbnailUrls.set(tab, url);
          while (thumbnailUrls.size > THUMBNAIL_CACHE_LIMIT) {
            const oldestTab = thumbnailUrls.keys().next().value;
            const oldestUrl = thumbnailUrls.get(oldestTab);
            thumbnailUrls.delete(oldestTab);
            if (oldestUrl && oldestUrl !== url) {
              try { URL.revokeObjectURL(oldestUrl); } catch (_) {}
            }
          }
          Services.prefs.setIntPref("qwqc.tab_swipe.runtime.thumbnail_cache_size", thumbnailUrls.size);
          if (previousUrl && previousUrl !== url) {
            window.setTimeout(() => {
              try { URL.revokeObjectURL(previousUrl); } catch (_) {}
            }, 1000);
          }
          Services.prefs.setIntPref(
            "qwqc.tab_swipe.runtime.thumbnail_success",
            Services.prefs.getIntPref("qwqc.tab_swipe.runtime.thumbnail_success", 0) + 1
          );
          return url;
        })().catch(error => {
          Services.prefs.setStringPref("qwqc.tab_swipe.runtime.thumbnail_error", String(error));
          log("thumbnail failed", tabTitle(tab, "tab"), error);
          return null;
        }).finally(() => {
          thumbnailCaptures.delete(tab);
        });
        thumbnailCaptures.set(tab, capture);
      }

      const url = await capture;
      if (url && panel?.isConnected && panel._qwqcTab === tab) {
        applyThumbnailToPanel(panel, tab, url, !hadCached);
      }
      return url;
    }

    function dropThumbnail(tab) {
      const url = thumbnailUrls.get(tab);
      thumbnailUrls.delete(tab);
      Services.prefs.setIntPref("qwqc.tab_swipe.runtime.thumbnail_cache_size", thumbnailUrls.size);
      if (url) {
        try { URL.revokeObjectURL(url); } catch (_) {}
      }
    }

    function prewarmNeighbors() {
      if (destroyed || !config.enabled || session || !gBrowser?.selectedTab) return;
      const selected = gBrowser.selectedTab;
      const tabs = [
        selected,
        targetForPhysicalSign(selected, -1),
        targetForPhysicalSign(selected, +1)
      ].filter(Boolean);
      for (const tab of new Set(tabs)) captureThumbnail(tab);
      Services.prefs.setIntPref("qwqc.tab_swipe.runtime.thumbnail_cache_size", thumbnailUrls.size);
    }

    function schedulePrewarm(delay = 100) {
      if (prewarmTimer) window.clearTimeout(prewarmTimer);
      prewarmTimer = window.setTimeout(() => {
        prewarmTimer = 0;
        prewarmNeighbors();
      }, delay);
    }

    function cleanupSession(activeSession = session) {
      if (!activeSession) return;
      try { activeSession.overlay?.remove(); } catch (_) {}
      try { window.removeEventListener("wheel", blockUnderlyingWheel, { capture: true }); } catch (_) {}
      if (session === activeSession) session = null;
      Services.prefs.setBoolPref("qwqc.tab_swipe.runtime.preview_active", false);
      if (!destroyed) schedulePrewarm(80);
    }

    function startPreview(state) {
      cleanupSession();
      if (!config.enabled || !gBrowser?.selectedTab || !gBrowser?.selectedBrowser) return;

      const startTab = gBrowser.selectedTab;
      const browser = gBrowser.selectedBrowser;
      const rect = browser.getBoundingClientRect();
      if (rect.width < 80 || rect.height < 80) return;

      const rightTarget = targetForPhysicalSign(startTab, +1);
      const leftTarget = targetForPhysicalSign(startTab, -1);
      const overlay = html("div");
      overlay.id = "qwqc-tab-swipe-preview";
      Object.assign(overlay.style, {
        position: "fixed",
        left: `${rect.left}px`,
        top: `${rect.top}px`,
        width: `${rect.width}px`,
        height: `${rect.height}px`,
        zIndex: "2147483200",
        overflow: "hidden",
        pointerEvents: "none",
        borderRadius: "8px",
        background: "rgb(12 12 12)",
        boxShadow: "0 18px 56px rgba(0,0,0,.22)",
        contain: "layout paint size style"
      });

      const leftPanel = makePanel(leftTarget, "No tab on this side");
      const rightPanel = makePanel(rightTarget, "No tab on this side");
      const currentPanel = makePanel(startTab, tabTitle(startTab, "Current tab"));
      leftPanel.style.visibility = "hidden";
      rightPanel.style.visibility = "hidden";
      overlay.append(leftPanel, rightPanel, currentPanel);
      document.documentElement.append(overlay);

      session = {
        seq: state.seq,
        startTab,
        leftTarget,
        rightTarget,
        overlay,
        currentPanel,
        leftPanel,
        rightPanel,
        lastDelta: Number(state.delta) || 0,
        activeTarget: null,
        ending: false
      };

      window.addEventListener("wheel", blockUnderlyingWheel, { capture: true, passive: false });
      Services.prefs.setBoolPref("qwqc.tab_swipe.runtime.preview_active", true);
      Services.prefs.setStringPref("qwqc.tab_swipe.runtime.preview_phase", "begin");

      // Cached snapshots are painted synchronously on panel creation. Refresh
      // them in the background without ever replacing a good frame with the
      // dark fallback.
      captureThumbnail(startTab, currentPanel);
      if (leftTarget) captureThumbnail(leftTarget, leftPanel);
      if (rightTarget) captureThumbnail(rightTarget, rightPanel);
      renderProgress(session.lastDelta);
    }

    function targetDataForDelta(delta) {
      if (!session) return null;
      const physicalSign = delta > 0 ? 1 : delta < 0 ? -1 : 0;
      if (!physicalSign) return null;
      if (physicalSign > 0) {
        return { sign: 1, tab: session.rightTarget, panel: session.rightPanel };
      }
      return { sign: -1, tab: session.leftTarget, panel: session.leftPanel };
    }

    function renderProgress(delta) {
      if (!session || session.ending) return;
      delta = clamp(Number(delta) || 0, -FULL_DRAG_DISTANCE * 1.2, FULL_DRAG_DISTANCE * 1.2);
      session.lastDelta = delta;
      const targetData = targetDataForDelta(delta);
      const physicalSign = targetData?.sign || 0;
      let visual = clamp(delta / FULL_DRAG_DISTANCE, -1, 1);

      // At an edge there is no target page. Keep a small elastic pull instead
      // of letting the current page disappear into empty space.
      if (physicalSign && !targetData.tab) visual *= 0.18;

      const x = visual * 100;
      session.currentPanel.style.transition = "none";
      session.currentPanel.style.transform = `translate3d(${x}%,0,0)`;

      for (const data of [
        { sign: 1, tab: session.rightTarget, panel: session.rightPanel },
        { sign: -1, tab: session.leftTarget, panel: session.leftPanel }
      ]) {
        const isActive = data.sign === physicalSign;
        data.panel.style.visibility = isActive ? "visible" : "hidden";
        data.panel.style.transition = "none";
        if (isActive) {
          const start = data.sign > 0 ? -100 : 100;
          data.panel.style.transform = `translate3d(${start + x}%,0,0)`;
          data.panel.style.filter = data.tab ? "none" : "brightness(.72)";
        }
      }
      session.activeTarget = targetData;
      Services.prefs.setStringPref("qwqc.tab_swipe.runtime.preview_phase", "drag");
      Services.prefs.setIntPref("qwqc.tab_swipe.runtime.preview_percent", Math.round(Math.abs(visual) * 100));
    }

    function finishPreview(state) {
      if (!session || session.ending || state.seq !== session.seq) return;
      session.ending = true;
      const delta = Number(state.delta) || session.lastDelta || 0;
      const targetData = targetDataForDelta(delta);
      const shouldCommit = Boolean(state.commit && targetData?.tab);
      const sign = targetData?.sign || (delta >= 0 ? 1 : -1);
      const easing = "cubic-bezier(.2,.82,.22,1)";
      const transition = `transform ${ANIM_MS}ms ${easing}, opacity ${ANIM_MS}ms ease`;

      session.currentPanel.style.transition = transition;
      session.leftPanel.style.transition = transition;
      session.rightPanel.style.transition = transition;

      if (!shouldCommit) {
        session.currentPanel.style.transform = "translate3d(0,0,0)";
        if (targetData?.panel) {
          const rest = sign > 0 ? -100 : 100;
          targetData.panel.style.transform = `translate3d(${rest}%,0,0)`;
        }
        Services.prefs.setStringPref("qwqc.tab_swipe.runtime.preview_phase", "cancel");
        window.setTimeout(() => cleanupSession(), ANIM_MS + 25);
        return;
      }

      const targetPanel = targetData.panel;
      targetPanel.style.visibility = "visible";
      targetPanel.style.transform = "translate3d(0,0,0)";
      session.currentPanel.style.transform = `translate3d(${sign * 105}%,0,0)`;
      Services.prefs.setStringPref("qwqc.tab_swipe.runtime.preview_phase", "commit");

      // Switch the actual browser underneath the preview near the end of the
      // animation. The overlay then fades away onto the already-selected tab.
      window.setTimeout(() => {
        if (!session || session.seq !== state.seq) return;
        try { gBrowser.selectedTab = targetData.tab; } catch (_) {}
      }, Math.max(70, ANIM_MS - 70));

      window.setTimeout(() => {
        if (!session || session.seq !== state.seq) return;
        session.overlay.style.transition = "opacity 75ms ease";
        session.overlay.style.opacity = "0";
        window.setTimeout(() => cleanupSession(), 85);
      }, ANIM_MS);
    }

    async function pollSwipeState() {
      if (destroyed || polling) return;
      polling = true;
      try {
        if (!(await IOUtils.exists(statePath))) return;
        const stat = await IOUtils.stat(statePath);
        if (stat.lastModified === lastStateMtime) return;
        lastStateMtime = stat.lastModified;
        const state = await IOUtils.readJSON(statePath);
        if (!state || typeof state.seq !== "number") return;

        if (state.phase === "begin") {
          if (state.seq !== lastStateSeq || !session) {
            lastStateSeq = state.seq;
            startPreview(state);
          }
          return;
        }
        if (!session || state.seq !== session.seq) return;
        if (state.phase === "update") {
          renderProgress(state.delta);
        } else if (state.phase === "end") {
          finishPreview(state);
        }
      } catch (error) {
        log("state poll failed", error);
      } finally {
        polling = false;
      }
    }

    const onTabSelect = () => schedulePrewarm(60);
    const onTabClose = event => dropThumbnail(event.target);

    readConfig();
    writeTouchscreenConfig("startup");
    applyMapping("startup");
    pollTimer = window.setInterval(pollSwipeState, POLL_MS);
    gBrowser?.tabContainer?.addEventListener("TabSelect", onTabSelect);
    gBrowser?.tabContainer?.addEventListener("TabClose", onTabClose);
    schedulePrewarm(120);

    prefObserver = {
      observe() {
        readConfig();
        writeTouchscreenConfig("settings-change");
        applyMapping("settings-change");
        if (!config.enabled) cleanupSession();
        else schedulePrewarm(60);
      }
    };
    Services.prefs.addObserver(PREF_BRANCH, prefObserver);

    gestureObserver = {
      observe() {
        if (!applying && config.enabled && config.protect) {
          window.setTimeout(() => applyMapping("zen-reset-guard"), 0);
        }
      }
    };
    Services.prefs.addObserver(LEFT_PREF, gestureObserver);
    Services.prefs.addObserver(RIGHT_PREF, gestureObserver);

    Services.prefs.setBoolPref("qwqc.tab_swipe.runtime.loaded", true);
    Services.prefs.setStringPref("qwqc.tab_swipe.runtime.version", "0.4.2");
    Services.prefs.setBoolPref("qwqc.tab_swipe.runtime.progressive_preview", true);

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      if (pollTimer) window.clearInterval(pollTimer);
      if (prewarmTimer) window.clearTimeout(prewarmTimer);
      cleanupSession();
      try { gBrowser?.tabContainer?.removeEventListener("TabSelect", onTabSelect); } catch (_) {}
      try { gBrowser?.tabContainer?.removeEventListener("TabClose", onTabClose); } catch (_) {}
      for (const url of thumbnailUrls.values()) {
        try { URL.revokeObjectURL(url); } catch (_) {}
      }
      thumbnailUrls.clear();
      try { Services.prefs.removeObserver(PREF_BRANCH, prefObserver); } catch (_) {}
      try { Services.prefs.removeObserver(LEFT_PREF, gestureObserver); } catch (_) {}
      try { Services.prefs.removeObserver(RIGHT_PREF, gestureObserver); } catch (_) {}

      const map = desired();
      const currentLeft = Services.prefs.getStringPref(LEFT_PREF, "");
      const currentRight = Services.prefs.getStringPref(RIGHT_PREF, "");
      if (currentLeft === map.left && currentRight === map.right) {
        applying = true;
        try {
          Services.prefs.setStringPref(LEFT_PREF, original.left);
          Services.prefs.setStringPref(RIGHT_PREF, original.right);
        } finally {
          applying = false;
        }
      }

      Services.prefs.setBoolPref("qwqc.tab_swipe.runtime.loaded", false);
      if (window[INSTANCE_KEY]?.destroy === destroy) delete window[INSTANCE_KEY];
    }

    return { destroy, applyMapping, pollSwipeState };
  }

  const start = () => {
    try {
      window[INSTANCE_KEY]?.destroy?.();
      const controller = createController();
      window[INSTANCE_KEY] = controller;
      if (typeof window.addUnloadListener === "function") window.addUnloadListener(() => controller.destroy());
    } catch (error) {
      Services.prefs.setStringPref("qwqc.tab_swipe.runtime.error", String(error));
      console.error("[QWQC Tab Swipe] failed to initialize", error);
    }
  };

  if (document.readyState === "complete") start();
  else window.addEventListener("load", start, { once: true });
})();
