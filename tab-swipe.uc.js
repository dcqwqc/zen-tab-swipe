// ==UserScript==
// @name QWQC Two-Finger Tab Swipe
// @description Map native horizontal swipe gestures to adjacent tabs.
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
    const original = {
      left: Services.prefs.getStringPref(LEFT_PREF, "Browser:BackOrBackDuplicate"),
      right: Services.prefs.getStringPref(RIGHT_PREF, "Browser:ForwardOrForwardDuplicate")
    };
    const config = { ...DEFAULTS };
    let prefObserver = null;
    let gestureObserver = null;
    let applying = false;
    let destroyed = false;

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
      if (config.reverse) {
        return { left: "Browser:NextTab", right: "Browser:PrevTab" };
      }
      return { left: "Browser:PrevTab", right: "Browser:NextTab" };
    }

    function setMapping(left, right, reason) {
      applying = true;
      try {
        if (Services.prefs.getStringPref(LEFT_PREF, "") !== left) Services.prefs.setStringPref(LEFT_PREF, left);
        if (Services.prefs.getStringPref(RIGHT_PREF, "") !== right) Services.prefs.setStringPref(RIGHT_PREF, right);
        Services.prefs.setStringPref("qwqc.tab_swipe.runtime.left", left);
        Services.prefs.setStringPref("qwqc.tab_swipe.runtime.right", right);
        Services.prefs.setStringPref("qwqc.tab_swipe.runtime.last_reason", reason);
        log(reason, left, right);
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

    readConfig();
    writeTouchscreenConfig("startup");
    applyMapping("startup");

    prefObserver = {
      observe() {
        readConfig();
        writeTouchscreenConfig("settings-change");
        applyMapping("settings-change");
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
    Services.prefs.setStringPref("qwqc.tab_swipe.runtime.version", "0.1.0");

    function destroy() {
      if (destroyed) return;
      destroyed = true;
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

    return { destroy, applyMapping };
  }

  const start = () => {
    try {
      window[INSTANCE_KEY]?.destroy?.();
      const controller = createController();
      window[INSTANCE_KEY] = controller;
      if (typeof window.addUnloadListener === "function") window.addUnloadListener(() => controller.destroy());
    } catch (error) {
      console.error("[QWQC Tab Swipe] failed to initialize", error);
    }
  };

  if (document.readyState === "complete") start();
  else window.addEventListener("load", start, { once: true });
})();
