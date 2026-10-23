/* hamdoor shell and chrome controller.
   The shell (DESIGN_SYSTEM §6, §7.1): the desktop rail, the phone tab bar (which
   the short-phone layout turns into a left rail, components.css C.29), the
   status scrim and the skip link.
   The chrome controller (§8, everything except the map's gestures, which the map
   detects and reports through HD.chrome.tuck/show):
   - the state hooks on <html> that the shared CSS keys on (§8.1):
     hd-tucked, hd-tucked-sticky, hd-keyboard-up, hd-kbd (+ hd-sheet-open);
   - tuck() refuses by itself whenever "never tuck while" applies (§8.2): desktop,
     keyboard mode, the on-screen keyboard, a focused field, an open map sheet
     (setSheetOpen), open search results, a dialog, the setting off;
   - the first sticky tuck's one-time tip;
   - lists (§8.3): one passive, rAF-throttled scroll listener hides the top bar
     and the tab bar together on a scroll down and brings them back;
   - keyboards, screen readers, reduced motion (§8.4), with the review fixes: keys
     typed into a field never switch keyboard mode on (except Tab, Escape, F6), any
     pointer or touch ends it; a click that reaches a tucked control shows the chrome
     first; reduced motion fades (CSS) and never hides anything from assistive tech;
   - resize, rotation and the on-screen keyboard (§8.5): only a real resize shows the
     chrome; the keyboard counts as up only with a focused field, no pinch zoom and
     more than 150 px lost; the tab bar then steps below the screen. */
(function () {
  "use strict";
  const HD = window.HD;
  const root = document.documentElement;
  const mq = (q) => (window.matchMedia ? matchMedia(q) : { matches: false, addEventListener() {} });
  const deskMQ = mq("(min-width: 1024px)");
  const shortMQ = mq("(max-width: 1023.98px) and (max-height: 499.98px)");
  const portraitMQ = mq("(orientation: portrait)");
  const onMQ = (m, fn) => { if (m.addEventListener) m.addEventListener("change", fn); else if (m.addListener) m.addListener(fn); };

  const AUTOHIDE_KEY = "hamdoor.chrome.autohide";
  const TIP_KEY = "hamdoor.tip.tuck";
  const MOVERS = "[data-tuck], .hd-ah";
  const LIST_ROUTES = new Set(["feed", "nearby", "activity", "you"]);

  // constants live in tokens.css (--hd-ah-*); read once, after the stylesheet has loaded
  let AH = null;
  function C() {
    if (AH) return AH;
    const cs = getComputedStyle(root);
    const n = (name, fb) => { const v = parseFloat(cs.getPropertyValue(name)); return isNaN(v) ? fb : v; };
    AH = {
      hide: n("--hd-ah-list-hide-px", 24), show: n("--hd-ah-list-show-px", 16), end: n("--hd-ah-list-end-px", 120),
      jitter: n("--hd-ah-jitter-px", 2), kbd: n("--hd-ah-keyboard-px", 150),
      resize: n("--hd-ah-resize-px", 160), quiet: n("--hd-ah-resize-quiet-ms", 500),
    };
    return AH;
  }

  /** A field you type into: text inputs, textarea, select, contenteditable (not buttons, checkboxes, radios, ranges). */
  function isEditable(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.isContentEditable) return true;
    const tag = el.tagName;
    if (tag === "TEXTAREA" || tag === "SELECT") return true;
    if (tag !== "INPUT") return false;
    return !/^(button|submit|reset|checkbox|radio|range|color|file|image|hidden)$/i.test(el.type || "text");
  }
  const routeName = () => { const c = HD.router && HD.router.current(); return c ? c.name : ""; };

  // ---------------------------------------------------------------- state

  const S = { tucked: false, kind: "", kbd: false, kbUp: false, sheets: new Set(), listeners: new Set() };
  let tipShown = false;

  function paint() {
    root.classList.toggle("hd-tucked", S.tucked);
    root.classList.toggle("hd-tucked-sticky", S.tucked && S.kind === "sticky");
  }
  function notify(reason) {
    const detail = { tucked: S.tucked, kind: S.kind, reason: reason || "" };
    for (const fn of Array.from(S.listeners)) {
      try { fn(detail); } catch (e) { console.error("[hd] chrome listener failed", e); }
    }
    HD.emit("chrome", detail);
  }
  const autohideOn = () => HD.store.get(AUTOHIDE_KEY, true) !== false;

  /** Why the chrome may not tuck right now ("" when it may). §8.2 "Never tuck while", §8.3, §8.4. */
  function blockedBy() {
    if (deskMQ.matches) return "desktop";
    if (S.kbd) return "keyboard mode";
    if (S.kbUp) return "on-screen keyboard";
    if (isEditable(document.activeElement)) return "field";
    if (document.querySelector("dialog[open]")) return "dialog";
    if (document.querySelector('[role="combobox"][aria-expanded="true"]')) return "results";
    const r = routeName();
    if (r === "map") {
      if (S.sheets.size) return "sheet";
      if (!autohideOn()) return "setting";
    } else if (!LIST_ROUTES.has(r)) {
      return "no chrome";
    }
    return "";
  }

  /** Focus never sits inside chrome that leaves: on the map it goes to the map, on lists to the page. */
  function moveFocusOutOfChrome() {
    const ae = document.activeElement;
    if (!ae || ae === document.body || !ae.closest || !ae.closest(MOVERS)) return;
    const target = routeName() === "map" ? document.getElementById("hd-map") : document.getElementById("hd-main");
    if (target) target.focus({ preventScroll: true });
  }

  function firstTuckTip() {
    if (tipShown) return;
    tipShown = true; // storage failing: once per session
    if (HD.store.get(TIP_KEY, false)) return;
    HD.store.set(TIP_KEY, true);
    HD.toast("Tap the map to bring the controls back.");
  }

  /** Tuck the chrome. reason: drag, pinch, wheel, dblclick, tap, scroll. {sticky: true} = tucked by a tap (no idle return). */
  function tuck(reason, opts) {
    if (blockedBy()) return false;
    const kind = opts && opts.sticky ? "sticky" : "auto";
    if (S.tucked && S.kind === kind) return true;
    moveFocusOutOfChrome();
    const wasTucked = S.tucked;
    S.tucked = true;
    S.kind = kind;
    paint();
    notify(reason || kind);
    if (kind === "sticky" && !wasTucked) firstTuckTip();
    return true;
  }
  /** Bring the chrome back. reason: idle, tap, key, focus, activate, resize, rotate, view, sheet, scroll, ... */
  function show(reason) {
    if (!S.tucked) return false;
    S.tucked = false;
    S.kind = "";
    paint();
    notify(reason || "show");
    return true;
  }

  function setKbd(on) {
    if (S.kbd === on) return;
    S.kbd = on;
    root.classList.toggle("hd-kbd", on);
    HD.emit("input-mode", { keyboard: on });
  }

  // ---------------------------------------------------------------- keyboards and screen readers (§8.4)

  const NAV_KEYS = new Set(["Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Up", "Down", "Left", "Right",
    "PageUp", "PageDown", "Home", "End", " ", "Spacebar", "Enter", "Escape", "Esc", "F6"]);
  const FIELD_KEYS = new Set(["Tab", "Escape", "Esc", "F6"]);
  document.addEventListener("keydown", (e) => {
    if (!NAV_KEYS.has(e.key) || e.altKey || e.metaKey || e.ctrlKey) return;
    // a space or Enter typed into a field is typing, not navigating (review fix F04)
    if (isEditable(e.target) && !FIELD_KEYS.has(e.key)) return;
    setKbd(true);
    show("key"); // synchronously, before focus moves into the chrome
  }, true);
  // any pointer or touch ends keyboard mode
  document.addEventListener("pointerdown", () => setKbd(false), true);
  document.addEventListener("touchstart", () => setKbd(false), { capture: true, passive: true });
  // a click that reaches a tucked control (a keyboard activation, a switch, a screen reader's double-tap)
  // shows the chrome first; the control then acts as usual
  document.addEventListener("click", (e) => {
    if (S.tucked && e.target && e.target.closest && e.target.closest(MOVERS)) show("activate");
  }, true);
  // focus that arrives in tucked chrome brings it back (focus never sits in hidden chrome)
  document.addEventListener("focusin", (e) => {
    if (S.tucked && e.target && e.target.closest && e.target.closest(MOVERS)) show("focus");
    updateKeyboard();
  });
  document.addEventListener("focusout", () => setTimeout(updateKeyboard, 60));

  // ---------------------------------------------------------------- the on-screen keyboard (§8.5)

  function updateKeyboard() {
    const vv = window.visualViewport;
    let up = false, covered = 0;
    if (vv && isEditable(document.activeElement)) {
      const notZoomed = Math.abs((vv.scale || 1) - 1) <= 0.01; // a pinch-zoomed page is not a keyboard (F05)
      const lost = window.innerHeight - vv.height;
      if (notZoomed && lost > C().kbd) {
        up = true;
        covered = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
      }
    }
    root.style.setProperty("--hd-vv-bottom", covered + "px");
    if (up !== S.kbUp) {
      S.kbUp = up;
      root.classList.toggle("hd-keyboard-up", up);
      notify("keyboard");
    }
  }
  if (window.visualViewport) {
    visualViewport.addEventListener("resize", updateKeyboard);
    visualViewport.addEventListener("scroll", updateKeyboard);
  }

  // ---------------------------------------------------------------- resize and rotation (§8.5)

  let lastW = window.innerWidth, lastH = window.innerHeight, lastScrollT = 0;
  window.addEventListener("resize", () => {
    const w = window.innerWidth, h = window.innerHeight;
    const widthChanged = w !== lastW, dh = Math.abs(h - lastH);
    lastW = w;
    lastH = h;
    updateKeyboard();
    measureTopbars();
    // a height-only change during or right after a scroll is the browser toolbar collapsing (F03)
    if (widthChanged || (dh > C().resize && performance.now() - lastScrollT > C().quiet)) show("resize");
  });
  onMQ(portraitMQ, () => show("rotate"));
  onMQ(deskMQ, () => { show("layout"); updateNav(); });
  onMQ(shortMQ, () => show("layout"));

  // ---------------------------------------------------------------- lists (§8.3)

  let lastY = 0, dir = 0, travel = 0, ticking = false;
  function clampedY() {
    const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    return { y: Math.min(Math.max(window.scrollY, 0), max), max };
  }
  /** The visible top bar of the current list view (a thread page has its own). */
  function currentTopbar() {
    const sec = document.querySelector(".hd-view:not([hidden])");
    if (!sec) return null;
    for (const bar of sec.querySelectorAll("[data-topbar]")) if (bar.getClientRects().length) return bar; // inside a hidden page: no boxes
    return null;
  }
  function topbarHeight() {
    const bar = currentTopbar();
    if (!bar) return 56;
    const pad = parseFloat(getComputedStyle(bar).paddingTop) || 0;
    return Math.max(0, bar.offsetHeight - pad);
  }
  function onScrollFrame() {
    ticking = false;
    if (!LIST_ROUTES.has(routeName())) return;
    const { y, max } = clampedY(); // iOS rubber-banding
    const dy = y - lastY;
    if (Math.abs(dy) < C().jitter) return; // under 2 px: accumulate
    const d = Math.sign(dy);
    if (d !== dir) { dir = d; travel = 0; } // a change of direction resets the travel
    travel += Math.abs(dy);
    lastY = y;
    const barH = topbarHeight();
    if (y <= barH || max - y <= C().end) { show("scroll"); return; }
    if (d > 0 && travel >= C().hide) {
      // never hide a page shorter than 1.5 viewports (and the rest of blockedBy: a field, the keyboard, desktop...)
      if (!S.tucked && document.documentElement.scrollHeight >= window.innerHeight * 1.5) tuck("scroll");
    } else if (d < 0 && travel >= C().show) {
      show("scroll");
    }
  }
  window.addEventListener("scroll", () => {
    lastScrollT = performance.now();
    if (!ticking) { ticking = true; requestAnimationFrame(onScrollFrame); }
  }, { passive: true });
  /** Call after a programmatic scroll (restoring a position) so it never counts as a gesture. */
  function listReset() {
    lastY = clampedY().y;
    dir = 0;
    travel = 0;
  }

  // ---------------------------------------------------------------- top bars: the page pads itself by the bar's height

  const ro = window.ResizeObserver ? new ResizeObserver((entries) => { for (const en of entries) measureBar(en.target); }) : null;
  const bars = new Set();
  function measureBar(bar) {
    const holder = bar.parentElement;
    if (!holder || !bar.getClientRects().length) return;
    const pad = parseFloat(getComputedStyle(bar).paddingTop) || 0;
    holder.style.setProperty("--hd-topbar-h", Math.max(0, Math.round(bar.offsetHeight - pad)) + "px");
    // the bar's whole height, padding included: on desktop the sticky aside and thread column stick just under it
    holder.style.setProperty("--hd-topbar-full", Math.round(bar.offsetHeight) + "px");
  }
  function measureTopbars() { bars.forEach(measureBar); }
  /** Views register their top bars; the bar's parent gets --hd-topbar-h (its .hd-page pads by it). */
  function watchTopbar(bar) {
    if (!bar || bars.has(bar)) return;
    bars.add(bar);
    if (ro) ro.observe(bar);
    measureBar(bar);
  }

  // ---------------------------------------------------------------- sheets on the map (contract §2)

  /** Map sheets block tucking and step the phone tab bar below the screen. Optional id for several sheets. */
  function setSheetOpen(open, id) {
    const key = id || "sheet";
    if (open) S.sheets.add(key); else S.sheets.delete(key);
    root.classList.toggle("hd-sheet-open", S.sheets.size > 0);
    if (open) show("sheet");
  }

  // ---------------------------------------------------------------- the shell: rail, tab bar, skip link

  const NAV = [
    { key: "map", label: "Map", icon: "map" },
    { key: "feed", label: "Feed", icon: "feed" },
    { key: "nearby", label: "Near­by", icon: "nearby" },
    { key: "activity", label: "Activ­ity", icon: "activity" },
  ];
  const TITLES = { map: "Map", feed: "Feed", nearby: "Nearby", activity: "Activity", you: "You" };

  function renderShell() {
    const call = HD.esc(HD.session.user ? HD.session.user.callsign : "");
    const rail = document.querySelector("[data-nav-rail]");
    if (rail) {
      rail.innerHTML = `<a class="hd-rail__brand" href="#/map" aria-label="hamdoor map">${HD.icon.logo(34)}</a>` +
        NAV.map((n) => `<a class="hd-rail__item" href="#/${n.key}" data-nav="${n.key}">${HD.icon(n.icon, { size: 24 })}${n.label}</a>`).join("") +
        `<a class="hd-rail__item hd-rail__me" href="#/you" data-nav="you"><span class="hd-rail__call" data-you-call>${call}</span>You</a>`;
    }
    const tabs = document.querySelector("[data-nav-tabs]");
    if (tabs) {
      tabs.innerHTML = NAV.map((n) => `<a class="hd-tabbar__item" href="#/${n.key}" data-nav="${n.key}">${HD.icon(n.icon, { size: 24 })}<span>${n.label}</span></a>`).join("") +
        `<a class="hd-tabbar__item" href="#/you" data-nav="you"><span class="hd-tabbar__call" data-you-call>${call}</span><span>You</span></a>`;
    }
    updateNav();
  }
  function updateNav() {
    const r = routeName();
    for (const a of document.querySelectorAll("[data-nav]")) {
      if (a.dataset.nav === r) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
  }
  function updateCall() {
    const call = HD.session.user ? HD.session.user.callsign : "";
    for (const el of document.querySelectorAll("[data-you-call]")) el.textContent = call;
  }

  // the active tab, pressed again on its own list, scrolls back to the top (Activity's own tabs, #/activity/<tab>,
  // are the same page: the view keeps the tab you were on)
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("[data-nav]");
    if (!a || e.defaultPrevented || e.button > 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    const samePage = location.hash === a.getAttribute("href") || a.dataset.nav === "activity";
    if (a.dataset.nav === routeName() && samePage && LIST_ROUTES.has(a.dataset.nav)) {
      e.preventDefault();
      window.scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }
  });
  // the skip link: focus the page without touching the hash route
  document.addEventListener("click", (e) => {
    const s = e.target.closest && e.target.closest("[data-skip]");
    if (!s) return;
    e.preventDefault();
    const main = document.getElementById("hd-main");
    if (main) main.focus();
  });

  HD.on("user", updateCall);
  HD.on("route", ({ name }) => {
    updateNav();
    const t = name === "auth" ? null : TITLES[name];
    document.title = t ? `${t} on hamdoor` : "hamdoor";
  });
  HD.on("view", () => {
    show("view"); // §8.3: both bars come back on a view change
    listReset();
    requestAnimationFrame(() => { measureTopbars(); listReset(); });
    updateKeyboard();
  });

  renderShell();

  HD.chrome = {
    tuck,
    show,
    isTucked: () => S.tucked,
    /** fn({tucked, kind, reason}) on every change; returns an unsubscribe function */
    onChange(fn) {
      S.listeners.add(fn);
      return () => S.listeners.delete(fn);
    },
    setSheetOpen,
    // extras (documented in build/F1.md)
    kind: () => S.kind,
    canTuck: () => !blockedBy(),
    blockedBy,
    keyboardMode: () => S.kbd,
    keyboardUp: () => S.kbUp,
    isEditable,
    /** "phone" | "short" | "narrow" | "desktop" (§6) */
    layout: () => (deskMQ.matches ? (window.innerWidth < 1280 ? "narrow" : "desktop") : shortMQ.matches ? "short" : "phone"),
    autohide: autohideOn,
    setAutohide(on) {
      HD.store.set(AUTOHIDE_KEY, !!on);
      if (!on && routeName() === "map") show("setting");
      HD.emit("autohide", { on: !!on });
    },
    listReset,
    watchTopbar,
    measureTopbars,
  };
})();
