/* hamdoor map 3/9: the map's chrome and interaction. DESIGN_SYSTEM.md §6, §7.9 to §7.11, §8.2, §9.2, §9.6 to §9.8.
   Everything the map shows outside the map lives in #hd-map-ui (contract §1):
   the search pill and its results, the map tools (live chip, grid chip, Layers),
   zoom and locate, the zoom hint, "List what's in view", the scale and the
   phone live chip, and two sheets (.hd-sheet--layers, .hd-sheet--detail) that are
   bottom sheets on phones, side panels on short phones and floating panels on
   desktop. Map gestures are reported to the shell's chrome controller
   (HD.chrome.tuck/show); taps that start while results, a field, the keyboard
   or a sheet is open only dismiss that thing. The map is one tab stop with a
   roving focus over the marks that are visible and not under chrome. */
(function () {
  "use strict";
  const HD = window.HD;
  const L = window.L;
  const M = HD.map._;
  const K = M.marks;
  const U = (M.ui = {});
  const NB = M.NB;
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const esc = M.esc;
  const body = document.body;
  const root = document.documentElement;
  const raf2 = (fn) => requestAnimationFrame(() => requestAnimationFrame(fn));
  const isEditable = (el) => !!el && (el.isContentEditable || (el.matches && el.matches('textarea, select, input:not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="color"]):not([type="file"])')));
  const keyboardUp = () => root.classList.contains("hd-keyboard-up") || !!document.querySelector('[data-keyboard="up"]');

  let mapEl = null, uiEl = null;
  let layersSheet = null, detailSheet = null;
  let selection = null, returnFocus = null, layersWasOpen = false, pendingReveal = null;
  let lastInput = "pointer", lastPointerAt = 0;

  /* ------------------------------------------------------------ build (once, at the first show) */
  U.build = function () {
    uiEl = document.getElementById("hd-map-ui");
    mapEl = document.getElementById("hd-map");
    const me = M.me();
    uiEl.innerHTML = `
      <div class="hd-mapbar" data-tuck="top">
        <div class="hd-search" role="search">
          ${M.ic("search") || ""}
          <label class="hd-sr" for="hd-search-input">Search callsign, place or APRS station</label>
          <input id="hd-search-input" class="hd-search__input" type="search" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" enterkeyhint="search"
                 placeholder="Callsign, place or APRS" role="combobox" aria-expanded="false" aria-controls="hd-search-results" aria-autocomplete="list">
          <button class="hd-search__me" type="button" id="hd-search-me" aria-label="Your station card"${me ? "" : " hidden"}><span data-you-call>${esc(me ? me.callsign : "")}</span></button>
          <div class="hd-results" id="hd-search-results" role="listbox" aria-label="Search results" hidden></div>
        </div>
      </div>
      <div class="hd-maptools" data-tuck="top">
        <div class="hd-status hd-desk-only" id="hd-live" hidden><span><span data-live-text></span><span class="hd-status__age" aria-hidden="true" data-live-age></span></span></div>
        <div class="hd-status hd-gridchip hd-desk-only" id="hd-gridchip" title="Grid square at the map centre">${M.ic("center", 20)}<span data-grid-text></span></div>
        <button class="hd-mapbtn hd-mapbtn--label hd-maptools__layers" type="button" id="hd-layers-btn" aria-expanded="false" aria-controls="hd-sheet-layers">${M.ic("layers")}Layers</button>
      </div>
      <div class="hd-scrim" id="hd-scrim" data-open="false"></div>
      <aside class="hd-sheet hd-sheet--layers" id="hd-sheet-layers" data-snap="closed" role="dialog" aria-modal="false" aria-labelledby="hd-layers-title">
        <button class="hd-sheet__handle" type="button" aria-label="Resize map layers"></button>
        <div class="hd-sheet__head" data-drag>
          <div class="hd-sheet__titlebar">
            <h2 class="hd-sheet__title" id="hd-layers-title" tabindex="-1">Map layers</h2>
            <button class="hd-btn hd-btn--quiet hd-btn--sm hd-phone-only" type="button" data-close-layers>Done</button>
            <button class="hd-iconbtn hd-desk-only" type="button" data-close-layers aria-label="Hide map layers">${M.ic("left") || M.ic("chevron") || "&lsaquo;"}</button>
          </div>
        </div>
        <div class="hd-sheet__body" id="hd-layers-body"></div>
      </aside>
      <div class="hd-mapside" data-tuck="side">
        <div class="hd-zoom" role="group" aria-label="Zoom">
          <button class="hd-zoom__btn" type="button" data-zoom="1" aria-label="Zoom in">${M.ic("plus") || "+"}</button>
          <button class="hd-zoom__btn" type="button" data-zoom="-1" aria-label="Zoom out">${M.ic("minus") || "&minus;"}</button>
        </div>
        <button class="hd-mapbtn" type="button" id="hd-locate" aria-label="Show where I am">${M.ic("locate")}</button>
      </div>
      <div class="hd-mapfoot">
        <div class="hd-zoomhint__wrap" data-tuck="drop">
          <button class="hd-zoomhint" type="button" id="hd-zoomhint" hidden>${M.ic("plus", 20)}<span data-hint-text></span></button>
        </div>
      </div>
      <aside class="hd-sheet hd-sheet--detail" id="hd-sheet-detail" data-snap="closed" role="dialog" aria-modal="false" aria-labelledby="hd-detail-title">
        <button class="hd-sheet__handle" type="button" aria-label="Resize station card"></button>
        <div class="hd-sheet__body" id="hd-detail-body"></div>
      </aside>
      <button class="hd-skipmap" type="button" id="hd-skipmap" data-inview>List what's in view</button>
      <p class="hd-sr" id="hd-map-help">Arrow keys move the map. Plus and minus zoom. Press Enter to move to the stations on the map, then use the arrow keys to go from one to the next, Enter to open one and Escape to come back to the map.</p>
      <div class="hd-mapscale" id="hd-mapscale" data-tuck="top" aria-hidden="true"></div>
      <div class="hd-mapstatus" id="hd-mapstatus" data-tuck="top" hidden><div class="hd-status"><span class="hd-mapstatus__text"><span data-live-text></span><span class="hd-status__age" aria-hidden="true" data-live-age></span></span></div></div>`;
    body.dataset.detail = "closed";
    body.dataset.layers = "closed";
  };

  /* ------------------------------------------------------------ attach (after the Leaflet map exists) */
  U.attach = function () {
    const map = M.map;
    const scale = L.control.scale({ position: "bottomleft", imperial: true, metric: false, maxWidth: 110 }).addTo(map);
    $("#hd-mapscale").appendChild(scale.getContainer());
    layersSheet = makeSheet($("#hd-sheet-layers"), { snaps: ["half", "full"], scrim: true, onClose: onLayersClose });
    // when the card grows and its mark ends up behind it, the reveal rule runs again once the sheet has settled
    detailSheet = makeSheet($("#hd-sheet-detail"), { snaps: ["peek", "half", "full"], onClose: onDetailClose,
      onSnap: (snap, prev) => { if (prev !== "closed" && snap !== "closed" && snap !== "full" && selection) { const sel = selection; setTimeout(() => { if (selection === sel) reveal(sel, !M.reduced()); }, 360); } } });
    buildLayers();
    bindButtons();
    bindSearch();
    bindDetail();
    bindGestures();
    bindKeyboard();
    map.on("zoomend", syncZoomButtons);
    syncZoomButtons();
    if (HD.chrome && HD.chrome.onChange) HD.chrome.onChange(() => relayoutSoon());
    // zoom and locate never move for a toast (a tap on "+" must not land on "−"): on the map the toasts keep out of
    // their column instead (map.css)
    addEventListener("resize", onResize);
    if (window.visualViewport) visualViewport.addEventListener("resize", onResize);
    M.onMQ(M.MQ.desk, onLayoutChange); M.onMQ(M.MQ.short, onLayoutChange);
    setInterval(tickLive, 1000);
    // desktop: the layers panel opens on the first visit and remembers its state (§6.3)
    if (M.isDesk() && M.prefs.layersOpen) openLayers({ quiet: true });
  };

  /** --hd-attr-h: the attribution's height (one to three lines); the zoom hint sits clear of it (map.css) */
  function syncAttrHeight() {
    const a = $(".hd-map .leaflet-control-attribution");
    const h = a ? Math.round(a.getBoundingClientRect().height) : 0;
    const v = h ? h + "px" : "";
    if (body.style.getPropertyValue("--hd-attr-h") !== v) {
      if (v) body.style.setProperty("--hd-attr-h", v); else body.style.removeProperty("--hd-attr-h");
      syncSideRoom(); // the column's lift follows the attribution
    }
  }
  /** --hd-hint-clear: while the zoom hint shows, the map's toasts stack above it (map.css), so a toast never covers it.
   *  The hint, like zoom and locate, never moves for a toast. A tucked hint drops off the screen and clears it. */
  function syncHintClear() {
    const hint = $("#hd-zoomhint");
    let v = 0;
    if (hint && !hint.hidden && M.visible) {
      const r = hint.getBoundingClientRect();
      if (r.height && r.top < window.innerHeight && r.bottom > 0) v = Math.round(window.innerHeight - r.top + 8);
    }
    if ((body.dataset.hint || "") !== (v ? "up" : "")) { if (v) body.dataset.hint = "up"; else delete body.dataset.hint; }
    const css = v ? v + "px" : "";
    if (body.style.getPropertyValue("--hd-hint-clear") !== css) { if (v) body.style.setProperty("--hd-hint-clear", css); else body.style.removeProperty("--hd-hint-clear"); }
  }
  let relayoutTimer = 0;
  /** labels are re-laid 280 ms after chrome tucks or returns and when a sheet snaps (§9.2) */
  function relayoutSoon() { clearTimeout(relayoutTimer); relayoutTimer = setTimeout(() => { if (M.visible) { syncAttrHeight(); syncHintClear(); K.layoutLabels(); K.renderSel(); } }, 280); }
  U.relayoutSoon = relayoutSoon;
  let resizeRaf = 0;
  function onResize() {
    if (resizeRaf) return;
    resizeRaf = requestAnimationFrame(() => { resizeRaf = 0; if (!M.map || !M.visible) return; M.map.invalidateSize({ pan: false }); updateMapBottom(); reportSheets(); fitTitle(); syncAttrHeight(); M.requestRender(); });
  }
  function onLayoutChange() {
    if (!M.map) return;
    // phones, short phones and narrow desktops show one panel at a time
    if (!M.isDesk() && layersSheet.isOpen() && detailSheet.isOpen()) layersSheet.close({ quiet: true });
    if (M.isDesk() && !layersSheet.isOpen() && M.prefs.layersOpen && !(M.isNarrowDesk() && detailSheet.isOpen())) openLayers({ quiet: true });
    onResize();
  }

  /* ------------------------------------------------------------ sheets (§7.9) */
  const peekPx = () => (parseFloat(getComputedStyle(body).getPropertyValue("--hd-sheet-peek")) || 168) + (parseFloat(getComputedStyle(root).getPropertyValue("--hd-safe-bottom")) || 0);
  function makeSheet(el, { snaps, scrim, onClose, onSnap }) {
    const s = {
      el, snaps, snap: "closed", justDragged: false,
      isOpen() { return this.snap !== "closed"; },
      set(snap) {
        const prev = this.snap;
        this.snap = snap; el.dataset.snap = snap; el.style.removeProperty("--hd-sheet-y");
        if (onSnap && prev !== snap) onSnap(snap, prev);
        if (scrim) $("#hd-scrim").dataset.open = String(snap !== "closed" && M.isPhone());
        updateMapBottom(); reportSheets(); relayoutSoon();
      },
      open(snap) { this.set(snap || snaps[0]); },
      close(opts) { if (this.snap === "closed") return; this.set("closed"); if (onClose) onClose(opts || {}); },
    };
    let drag = null;
    const currentY = () => new DOMMatrixReadOnly(getComputedStyle(el).transform).m42;
    el.addEventListener("pointerdown", (e) => {
      if (!M.isPhone() || e.button > 0 || !e.target.closest(".hd-sheet__handle, .hd-sheet__head, [data-drag]") || e.target.closest("a, input, select, textarea, button:not(.hd-sheet__handle)")) return;
      drag = { id: e.pointerId, sy: e.clientY, y0: currentY(), lastY: e.clientY, lastT: performance.now(), v: 0, moved: false };
    });
    el.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dy = e.clientY - drag.sy;
      if (!drag.moved && Math.abs(dy) > 4) {
        // capture only once it is a drag: a captured pointer retargets the click, and a tap on
        // the handle must stay a click on the handle (it cycles the snaps)
        drag.moved = true;
        el.dataset.dragging = "true";
        try { el.setPointerCapture(e.pointerId); } catch (er) { /* not capturable */ }
      }
      if (!drag.moved) return;
      const now = performance.now();
      drag.v = (e.clientY - drag.lastY) / Math.max(1, now - drag.lastT); drag.lastY = e.clientY; drag.lastT = now;
      el.style.setProperty("--hd-sheet-y", Math.max(0, drag.y0 + dy) + "px");
    });
    const end = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      el.dataset.dragging = "false";
      const moved = drag.moved, y = currentY() + drag.v * 160; // projected by velocity x 160 ms
      drag = null; s.justDragged = moved;
      if (!moved) { el.style.removeProperty("--hd-sheet-y"); return; }
      const H = el.offsetHeight, pos = { peek: H - peekPx(), half: H * 0.42, full: 0 };
      const cands = snaps.map((k) => [k, pos[k]]), lowest = Math.max(...cands.map((c) => c[1]));
      if (y > lowest + 70) { s.close(); return; }
      let best = cands[0];
      for (const c of cands) if (Math.abs(c[1] - y) < Math.abs(best[1] - y)) best = c;
      s.set(best[0]);
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    // activating the handle cycles the snaps (keyboard and switch users)
    el.querySelector(".hd-sheet__handle").addEventListener("click", () => {
      if (s.justDragged) { s.justDragged = false; return; }
      const i = snaps.indexOf(s.snap); s.set(snaps[(i + 1) % snaps.length]);
    });
    // focus that lands below the visible part of a phone sheet (Tab from the heading of a card at peek) raises the
    // sheet to full, so a keyboard or switch user never works on controls they cannot see. Measured where the sheet
    // comes to rest, not where it is mid-slide: a card opened from the keyboard focuses its heading while the sheet is
    // still sliding up from below the screen, and that must leave it at peek (§7.9)
    el.addEventListener("focusin", (e) => {
      if (!M.isPhone() || s.snap === "full" || s.snap === "closed" || e.target === el || !snaps.includes("full") || el.dataset.dragging === "true") return;
      const r = e.target.getBoundingClientRect(), sr = el.getBoundingClientRect();
      const restTop = window.innerHeight - sheetVisible(s);
      if (restTop + (r.bottom - sr.top) > window.innerHeight - 8) s.set("full");
    });
    return s;
  }
  /** how much of a phone sheet shows at its snap (§7.9: peek 168 px plus the safe area, half 58 %, full all of it) */
  function sheetVisible(s) {
    const H = s.el.offsetHeight || innerHeight - 59;
    return s.snap === "peek" ? peekPx() : s.snap === "half" ? H * 0.58 : s.snap === "full" ? H : 0;
  }
  /** While a phone sheet is at peek or half, the attribution, zoom and locate ride above it. */
  function updateMapBottom() {
    if (!detailSheet) return;
    const open = M.isPhone() ? [detailSheet, layersSheet].filter((s) => s.isOpen()) : [];
    if (!open.length || open[0].snap === "full") body.style.removeProperty("--hd-map-bottom");
    else body.style.setProperty("--hd-map-bottom", Math.round(sheetVisible(open[0])) + "px");
    syncSideRoom();
  }
  /** Zoom and locate sit 52 px above an open sheet (§6.1). On a shorter phone a half sheet leaves no room for them
   *  under the Layers button: then they step away, as they do behind a full sheet, rather than sit on top of Layers. */
  function syncSideRoom() {
    let none = false;
    const mb = parseFloat(body.style.getPropertyValue("--hd-map-bottom"));
    const side = $(".hd-mapside"), tools = $(".hd-maptools");
    if (M.isPhone() && mb > 0 && side && tools) {
      // offsetTop and offsetHeight ignore the tuck transforms: the room is measured where the chrome rests.
      // The column's lift above the sheet is map.css's: 52 px, or the attribution's height plus 8 px when more
      const lift = Math.max(52, (parseFloat(body.style.getPropertyValue("--hd-attr-h")) || 40) + 8);
      const toolsBottom = tools.offsetTop + tools.offsetHeight;
      none = window.innerHeight - (mb + lift) - side.offsetHeight < toolsBottom + 8;
    }
    if ((body.dataset.mapside === "none") !== none) { if (none) body.dataset.mapside = "none"; else delete body.dataset.mapside; }
  }
  let sheetReported = null;
  /** map sheets block tucking and hide the tab bar on phones (contract §2, §8) */
  function reportSheets() {
    const open = M.visible && !M.isDesk() && !!detailSheet && (detailSheet.isOpen() || layersSheet.isOpen());
    if (open === sheetReported) return;
    sheetReported = open;
    if (HD.chrome && HD.chrome.setSheetOpen) HD.chrome.setSheetOpen(open);
  }

  /* ------------------------------------------------------------ layers panel (§7.10) */
  /** a toggle chip with its live count; a chip that is off shows no number */
  M.chip = (path, label, swatch) => `<button class="hd-chip" type="button" aria-pressed="true" data-pref="${path}">${swatch || ""}<span class="hd-chip__label">${label}</span><span class="hd-chip__count" data-count="${path}"></span></button>`;
  M.sw = (c, extra = "") => `<span class="hd-sw ${extra}" style="--c:${c}"></span>`;
  function buildLayers() {
    // the id comes from /api/config: percent-encoded (quotes too) so it can never leave the CSS url()
    const thumbUrl = (id) => `/static/img/basemap-${encodeURIComponent(id).replace(/['()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase())}.png`;
    const thumbs = M.switcherBasemaps().map((b) => `<button class="hd-basemap" type="button" data-basemap="${esc(b.id)}" aria-pressed="false"><span class="hd-basemap__thumb" style="background-image:url('${esc(thumbUrl(b.id))}')"></span>${esc(b.label)}</button>`).join("");
    let html = `<section class="hd-layers__sec"><div class="hd-basemaps" role="group" aria-label="Map style">${thumbs}</div></section>`;
    for (const st of M.layerList) {
      if (!st.enabled() || !st.def.section) continue;
      html += `<section class="hd-layers__sec" data-sec="${st.id}"><div class="hd-layers__head"><div class="hd-layers__t"><h3 class="hd-layers__title" id="hd-lt-${st.id}">${esc(st.def.title)}</h3><p class="hd-layers__meta" data-meta="${st.id}"></p></div><button class="hd-switch" type="button" role="switch" aria-checked="true" aria-labelledby="hd-lt-${st.id}" data-pref="${st.id}/on"></button></div>${st.def.section(M.prefs[st.id])}</section>`;
    }
    html += `<div class="hd-layers__inview"><button class="hd-btn hd-btn--sm hd-btn--block" type="button" data-inview>${M.ic("list", 20)}List what's in view</button></div>`;
    html += '<div class="hd-layers__foot"><span>Saved on this device</span><button class="hd-btn hd-btn--quiet hd-btn--sm" type="button" data-reset>Reset</button></div>';
    $("#hd-layers-body").innerHTML = html;
    syncLayers();
  }
  function syncLayers() {
    const lb = $("#hd-layers-body");
    if (!lb || !lb.firstElementChild || !M.map || !M.map._loaded) return;
    const ctx = M.ctx || { zoom: M.map.getZoom(), bounds: M.map.getBounds() };
    for (const b of $$("[data-basemap]", lb)) b.setAttribute("aria-pressed", String(b.dataset.basemap === M.basemapId));
    for (const el of $$("[data-pref]", lb)) {
      const on = !!M.pref(el.dataset.pref);
      el.setAttribute(el.getAttribute("role") === "switch" ? "aria-checked" : "aria-pressed", String(on));
    }
    for (const s of $$("[data-sec]", lb)) s.dataset.off = String(!M.isOn(s.dataset.sec));
    for (const st of M.layerList) {
      if (!st.enabled() || !st.def.section) continue;
      const meta = $(`[data-meta="${st.id}"]`, lb);
      let c = { meta: "", chips: {} };
      if (!M.isOn(st.id)) c.meta = "Hidden";
      else if (st.def.counts) { try { c = st.def.counts(st.data, ctx, st) || c; } catch (e) { console.error("[hd] counts", st.id, e); } }
      if (meta && meta.textContent !== c.meta) meta.textContent = c.meta || "";
      for (const el of $$(`[data-count^="${st.id}/"]`, lb)) {
        const key = el.dataset.count.slice(st.id.length + 1);
        const v = M.isOn(st.id) && M.pref(el.dataset.count) ? c.chips && c.chips[key] : null;
        const t = typeof v === "number" ? v.toLocaleString("en-US") : typeof v === "string" ? v : "";
        if (el.textContent !== t) el.textContent = t;
      }
      if (st.def.syncSection) st.def.syncSection(lb, M.prefs[st.id]);
    }
  }
  U.syncLayers = syncLayers;
  function openLayers(opts = {}) {
    layersSheet.open("half");
    body.dataset.layers = "open";
    $("#hd-layers-btn").setAttribute("aria-expanded", "true");
    // phones, short phones and narrow desktops show one panel at a time
    if (!M.isDesk() || M.isNarrowDesk()) { layersWasOpen = false; if (detailSheet.isOpen()) detailSheet.close({ quiet: true }); }
    if (M.isDesk() && !opts.quiet) { M.prefs.layersOpen = true; M.savePrefs(); }
    if (HD.chrome && HD.chrome.show) HD.chrome.show("sheet");
    syncLayers();
    if (!opts.quiet && lastInput === "keyboard") { const t = $("#hd-layers-title"); if (t) t.focus({ preventScroll: true }); }
    M.requestRender();
  }
  function onLayersClose(o) {
    body.dataset.layers = "closed";
    $("#hd-layers-btn").setAttribute("aria-expanded", "false");
    if (M.isDesk() && !o.quiet) { M.prefs.layersOpen = false; M.savePrefs(); }
    if (!o.quiet && $("#hd-sheet-layers").contains(document.activeElement)) $("#hd-layers-btn").focus({ preventScroll: true });
    M.requestRender();
  }
  U.openLayers = openLayers;
  function onLayersClick(e) {
    const bm = e.target.closest("[data-basemap]");
    if (bm) { M.prefs.basemap = bm.dataset.basemap; M.savePrefs(); M.setBasemap(bm.dataset.basemap); syncLayers(); return; }
    const p = e.target.closest("[data-pref]");
    if (p) {
      const path = p.dataset.pref, id = path.split("/")[0], v = !M.pref(path);
      M.setPref(path, v);
      const st = M.L(id);
      if (st && st.def.onPref) st.def.onPref(path.split("/").slice(1).join("/"), v);
      if (path.endsWith("/on") && !v) { if (st) { if (st.ctrl) { st.ctrl.abort(); st.ctrl = null; st.seq++; } } }
      else if (st) M.loadLayer(st);
      // another layer can depend on this one (APRS hides repeater objects while Repeaters is on)
      syncCredits(); syncLayers(); M.requestRender();
      return;
    }
    const sg = e.target.closest("[data-seg]");
    if (sg) {
      const [path, val] = [sg.dataset.seg, sg.dataset.value];
      const cur = M.pref(path), v = typeof cur === "number" ? Number(val) : val;
      M.setPref(path, v);
      const st = M.L(path.split("/")[0]); if (st) M.loadLayer(st);
      syncLayers(); M.requestRender();
      return;
    }
    if (e.target.closest("[data-reset]")) {
      M.resetPrefs();
      M.setBasemap(M.effectiveBasemap());
      buildLayers(); syncCredits(); M.loadAll(); M.requestRender();
      M.toast("Map layers reset");
      return;
    }
    if (e.target.closest("[data-inview]")) { U.openDetail({ layer: "ui", kind: "inview" }); return; }
    if (e.target.closest("[data-close-layers]")) layersSheet.close();
  }

  /* layer credits ride in the attribution while their layer is on (§7.10) */
  const creditOn = {};
  function syncCredits() {
    if (!M.map) return;
    for (const st of M.layerList) {
      const credit = st.def.attribution;
      if (!credit) continue;
      const want = M.isOn(st.id);
      if (want && !creditOn[st.id]) M.map.attributionControl.addAttribution(credit);
      if (!want && creditOn[st.id]) M.map.attributionControl.removeAttribution(credit);
      creditOn[st.id] = want;
    }
  }

  /* ------------------------------------------------------------ cards (§7.9, §7.11) */
  U.selection = () => selection;
  const selIdOf = (it) => {
    if (!it) return "";
    if (it.layer === "ui") return it.kind === "inview" ? "inview:x" : "";
    const st = M.L(it.layer);
    return st && st.def.selId ? st.def.selId(it) || "" : "";
  };
  U.selId = () => selIdOf(selection);
  /** where the selection ring goes: {latlng, d, el} or null */
  U.ringFor = (it) => { const st = M.L(it.layer); return st && st.def.ring ? st.def.ring(it, st) : null; };
  /** the DOM element of an item's mark, if it is drawn */
  U.markEl = (it) => {
    const st = M.L(it.layer);
    if (st && st.def.markEl) return st.def.markEl(it, st);
    if (!st || !st.def.markKey) return null;
    const keys = [].concat(st.def.markKey(it) || []);
    for (const k of keys) { const m = st.store.get(k); if (m && m.getElement()) return m.getElement(); }
    // a mark may also live in another layer's store (a neighbour's post badge, you)
    if (st.def.markIn) { const o = M.L(st.def.markIn(it)); if (o) for (const k of keys) { const m = o.store.get(k); if (m && m.getElement()) return m.getElement(); } }
    return null;
  };
  U.latlngOf = (it) => { const st = M.L(it.layer); return st && st.def.latlng ? st.def.latlng(it) : null; };

  /** activate a mark (a click, a tap or Enter on a focused mark) */
  U.activate = function (d, e, opts = {}) {
    // a screen reader's activation arrives as a click with no pointer: treat it like the keyboard (§7.9)
    const oe = e && e.originalEvent;
    if (oe && oe.detail === 0 && performance.now() - lastPointerAt > 800) opts = Object.assign({ keyboard: true }, opts);
    if (d.onClick) { d.onClick(e, opts); return; }
    if (d.item) U.openDetail(d.item, opts);
  };
  function cardHtml(it) {
    if (it.layer === "ui") return it.kind === "inview" ? inViewHtml() : it.kind === "chooser" ? chooserHtml(it) : "";
    const st = M.L(it.layer);
    if (!st || !st.def.card) return "";
    try { return st.def.card(it, M.ctx || {}) || ""; } catch (e) { console.error("[hd] card", it.layer, e); return ""; }
  }
  function renderDetail() {
    const db = $("#hd-detail-body");
    pickItems = [];
    inViewStale = false;
    db.innerHTML = cardHtml(selection);
    db.scrollTop = 0;
    fitTitle();
  }
  /** The signature is never wrapped and never runs under the close button (§4, §7.9): a long one (KC0UV9-13, a
   *  "146.940 −" plate at 320 px) steps down in size until it fits its column. */
  function fitTitle() {
    const t = $("#hd-detail-title");
    if (!t || !t.classList.contains("hd-detail__call")) return;
    t.style.removeProperty("font-size");
    const w = t.clientWidth;
    if (!w || t.scrollWidth <= w + 1) return;
    let size = Math.max(20, Math.floor((parseFloat(getComputedStyle(t).fontSize) || 39) * w / t.scrollWidth));
    t.style.fontSize = size + "px";
    while (size > 20 && t.scrollWidth > t.clientWidth + 1) { size -= 1; t.style.fontSize = size + "px"; }
  }
  /* Live updates (an APRS poll every 30 s, a card's own details arriving) patch the open card in place instead of
     rebuilding it, so keyboard and screen-reader focus, the scroll position and hover survive (§9.6, §11). */
  function morph(from, to) {
    const a = Array.from(from.childNodes), b = Array.from(to.childNodes);
    for (let i = 0; i < b.length; i++) {
      const x = a[i], y = b[i];
      if (!x) { from.appendChild(y); continue; }
      if (x.nodeType !== y.nodeType || x.nodeName !== y.nodeName) { from.replaceChild(y, x); continue; }
      if (x.nodeType !== 1) { if (x.nodeValue !== y.nodeValue) x.nodeValue = y.nodeValue; continue; }
      if (x.isEqualNode(y)) continue;
      for (const at of Array.from(x.attributes)) if (!y.hasAttribute(at.name)) x.removeAttribute(at.name);
      for (const at of Array.from(y.attributes)) if (x.getAttribute(at.name) !== at.value) x.setAttribute(at.name, at.value);
      morph(x, y);
    }
    for (let i = a.length - 1; i >= b.length; i--) from.removeChild(a[i]);
  }
  /** a selector for the same control in a redrawn card, when the focused one had to be replaced */
  const FOCUS_ATTRS = ["id", "data-act", "data-w", "data-copy", "data-goto", "data-ham", "data-pick", "data-close-detail", "data-rpt", "href"];
  function focusKey(el) {
    const parts = [];
    for (const a of FOCUS_ATTRS) if (el.hasAttribute && el.hasAttribute(a)) parts.push(`[${a}="${CSS.escape(el.getAttribute(a))}"]`);
    return parts.length ? el.tagName.toLowerCase() + parts.join("") : null;
  }
  let inViewStale = false;
  /** a layer fetched more about the open card (a licence's dates, a station's track): draw it again in place */
  U.refreshDetail = function (it) {
    if (!selection || (it && selection !== it)) return;
    const db = $("#hd-detail-body"), ae = document.activeElement, inside = !!ae && ae !== db && db.contains(ae);
    // "In view" is a list someone is reading, often with a screen reader: it never changes under their focus, and
    // catches up when focus leaves the card
    if (inside && selection.layer === "ui" && selection.kind === "inview") { inViewStale = true; return; }
    inViewStale = false;
    const key = inside ? focusKey(ae) : null;
    pickItems = [];
    const tpl = document.createElement("template");
    tpl.innerHTML = cardHtml(selection);
    morph(db, tpl.content);
    fitTitle();
    if (inside && !ae.isConnected) {
      const f = (key && db.querySelector(key)) || $("#hd-detail-title");
      if (f) f.focus({ preventScroll: true });
    }
    M.requestRender();
  };
  U.openDetail = function (item, opts = {}) {
    if (!item || !detailSheet) return;
    for (const st of M.layerList) if (st.def.onSelect) st.def.onSelect(item, selection);
    selection = item;
    renderDetail();
    body.dataset.detail = "open";
    if (!M.isDesk() && layersSheet.isOpen()) layersSheet.close({ quiet: true });
    if (M.isNarrowDesk() && layersSheet.isOpen()) { layersWasOpen = true; layersSheet.close({ quiet: true }); }
    // phones open a card at peek (§7.9), the chooser too (phone-chooser); "In view" is a list someone asked for, so half
    detailSheet.open(M.isDesk() || M.isShort() ? "half" : opts.snap || (item.layer === "ui" && item.kind === "inview" ? "half" : "peek"));
    fitTitle(); // a closed desktop panel has no width to measure until it opens
    if (HD.chrome && HD.chrome.show) HD.chrome.show("sheet");
    const id = selIdOf(item);
    const cur = HD.router.current();
    if (cur && cur.name === "map") HD.router.patch({ sel: id || undefined });
    M.requestRender();
    // the reveal rule measures the mark and its label, so it runs once the placer has laid out the selection
    // (a selected label is never culled, so it can be wider than it was a frame ago)
    pendingReveal = { it: item, animate: !opts.quiet && !M.reduced() };
    const st = M.L(item.layer);
    if (st && st.def.cardLoad) { try { st.def.cardLoad(item); } catch (e) { console.error("[hd] cardLoad", e); } }
    const t = $("#hd-detail-title");
    if (opts.silent || !t) return;
    // keyboard or assistive activation moves focus to the heading; a tap announces the card instead
    if (opts.keyboard || (lastInput === "keyboard" && !opts.pointer)) t.focus({ preventScroll: true });
    else M.announce(`${t.getAttribute("aria-label") || t.textContent.trim()}. Card open.`);
  };
  U.closeDetail = () => { if (detailSheet) detailSheet.close(); };
  function onDetailClose(o) {
    const was = selection;
    selection = null;
    body.dataset.detail = "closed";
    for (const st of M.layerList) if (st.def.onSelect) st.def.onSelect(null, was);
    const cur = HD.router.current();
    if (cur && cur.name === "map" && M.visible) HD.router.patch({ sel: undefined });
    M.requestRender();
    // closing returns focus to the mark that opened it, when there was one
    const det = $("#hd-sheet-detail");
    if (!o.quiet && returnFocus && returnFocus.isConnected && (document.activeElement === body || det.contains(document.activeElement) || !document.activeElement)) returnFocus.focus({ preventScroll: true });
    else if (!o.quiet && det.contains(document.activeElement)) mapEl.focus({ preventScroll: true });
    returnFocus = null;
    if (layersWasOpen) { layersWasOpen = false; if (M.isNarrowDesk() && M.prefs.layersOpen) openLayers({ quiet: true }); }
  }
  // the zoom from which each layer draws its marks (§9.1), and the zoom a link moves to when the mark is not drawn
  const SEL_DRAWN_Z = { fcc: 10, rpt: 10, aprs: 9, posts: 13 };
  const SEL_SHOW_Z = { fcc: 15, rpt: 13, aprs: 13, posts: 14 };
  /** Open a card from a selection id (the URL, HD.map.focus, search). */
  U.openSel = function (sel, opts = {}) {
    if (!sel) return;
    const i = sel.indexOf(":"), kind = i < 0 ? sel : sel.slice(0, i), rest = i < 0 ? "" : sel.slice(i + 1);
    if (kind === "inview") { U.openDetail({ layer: "ui", kind: "inview" }, opts); return; }
    const st = M.L(M.layerOfSel[kind]);
    if (!st || !st.def.resolve) return;
    const want = sel;
    U._pendingSel = want;
    Promise.resolve(st.def.resolve(kind, rest)).then((item) => {
      if (U._pendingSel !== want) return;
      U._pendingSel = null;
      if (!item) return;
      // a card opened from a link shows its mark: when the mark is off screen, or not drawn at this zoom, the map
      // centres on it (at the zoom where its layer draws it); otherwise the reveal rule nudges it clear of chrome
      const ll = U.latlngOf(item);
      if (ll && M.map) {
        // a ZIP group, or one ZIP-level licence (drawn only inside its ZIP's group, from z13)
        const zip = item.layer === "fcc" && (item.kind === "zip" || (item.kind === "fcc" && item.loc === "zip"));
        const z = M.map.getZoom(), need = zip ? M.MINZ.zipGroups : SEL_DRAWN_Z[item.layer] || 0;
        const onScreen = M.map.getBounds().pad(-0.04).contains(ll);
        if (!onScreen || z < need) M.map.setView(ll, z < need ? (zip ? 14 : SEL_SHOW_Z[item.layer] || need) : z, { animate: false });
      }
      U.openDetail(item, Object.assign({ quiet: true }, opts));
    }).catch(() => { if (U._pendingSel === want) U._pendingSel = null; });
  };

  /* The reveal rule (§7.9): when a card opens and its mark or the mark's label sits behind a
     sheet, a panel, the rail, the search pill or the attribution, pan just enough to bring the
     whole box 48 px clear. Never otherwise. */
  function selectionBox(it) {
    const ll = U.latlngOf(it);
    if (!ll) return null;
    const mr = M.map.getContainer().getBoundingClientRect(), p = M.map.latLngToContainerPoint(ll);
    let box = [p.x - 15, p.y - 15, p.x + 15, p.y + 15];
    const el = U.markEl(it);
    if (el) {
      for (const x of [el, ...el.querySelectorAll(".hd-lbl, .hd-pill, .hd-mk-rpt")]) {
        const r = x.getBoundingClientRect();
        if (!r.width || getComputedStyle(x).display === "none") continue;
        box = [Math.min(box[0], r.left - mr.left), Math.min(box[1], r.top - mr.top), Math.max(box[2], r.right - mr.left), Math.max(box[3], r.bottom - mr.top)];
      }
    } else if (it.layer === "fcc" && M.map.getZoom() >= M.MINZ.fccLabels) box[2] = p.x + 96; // the canvas callsign to its right
    return box;
  }
  function reveal(it, animate) {
    if (!M.visible || !it || !M.map) return;
    const box = selectionBox(it);
    if (!box) return;
    const mr = M.map.getContainer().getBoundingClientRect(), m = 48;
    let rect;
    if (M.isDesk() || M.isShort()) {
      const rail = $(".hd-tabbar") || $(".hd-rail");
      let leftEdge = 0;
      if (M.isShort() && rail) leftEdge = rail.getBoundingClientRect().right - mr.left;
      if (M.isDesk() && layersSheet.isOpen()) leftEdge = $("#hd-sheet-layers").getBoundingClientRect().right - mr.left;
      const open = [detailSheet, layersSheet].filter((s) => s.isOpen() && (M.isShort() || s === detailSheet));
      const right = open.length ? Math.min(...open.map((s) => s.el.getBoundingClientRect().left - mr.left)) : mr.width;
      rect = { l: Math.max(0, leftEdge), r: right, t: M.isShort() ? 8 : 72, b: mr.height - 24 };
    } else {
      const H = $("#hd-sheet-detail").offsetHeight;
      const vis = detailSheet.snap === "peek" ? peekPx() : detailSheet.snap === "half" ? H * 0.58 : 0;
      rect = { l: 0, r: mr.width, t: $(".hd-mapbar").getBoundingClientRect().bottom - mr.top + 56, b: mr.height - vis - 30 };
    }
    // the attribution is chrome too: a selected mark never ends up under it
    const attr = $(".hd-map .leaflet-control-attribution"), ar = attr && attr.getBoundingClientRect();
    const cx = (box[0] + box[2]) / 2;
    if (ar && ar.height && ar.right > mr.left + cx - 1 && ar.left < mr.left + cx + 1) rect.b = Math.min(rect.b, ar.top - mr.top - 8);
    let dx = 0, dy = 0;
    if (box[0] < rect.l + m) dx = box[0] - (rect.l + m); else if (box[2] > rect.r - m) dx = Math.min(box[2] - (rect.r - m), box[0] - (rect.l + m));
    if (box[1] < rect.t + m) dy = box[1] - (rect.t + m); else if (box[3] > rect.b - m) dy = Math.min(box[3] - (rect.b - m), box[1] - (rect.t + m));
    // the floating buttons (zoom, locate, Layers, the desktop chips) are chrome too: a mark or its
    // label that would end up under one moves sideways just clear of it
    const moved = [box[0] - dx, box[1] - dy, box[2] - dx, box[3] - dy];
    for (const el of $$(".hd-mapside .hd-zoom, .hd-mapside .hd-mapbtn, .hd-maptools > :not([hidden])", uiEl)) {
      const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
      if (!r.width || cs.display === "none" || cs.visibility === "hidden") continue;
      const o = [r.left - mr.left - 8, r.top - mr.top - 8, r.right - mr.left + 8, r.bottom - mr.top + 8];
      if (moved[0] >= o[2] || moved[2] <= o[0] || moved[1] >= o[3] || moved[3] <= o[1]) continue;
      const shift = (moved[0] + moved[2]) / 2 < (o[0] + o[2]) / 2 ? moved[2] - o[0] : moved[0] - o[2];
      if (moved[0] - shift >= rect.l) { dx += shift; moved[0] -= shift; moved[2] -= shift; }
    }
    if (dx || dy) M.map.panBy([Math.round(dx), Math.round(dy)], { animate: !!animate });
  }
  U.reveal = reveal;
  U.onBasemapReady = () => { if (selection && !M._revealedAfterBase) { M._revealedAfterBase = true; setTimeout(() => reveal(selection, false), 50); } };

  /** The part of the map a person can see, in container pixels: the map minus the search pill, an open sheet or
   *  panel (where it comes to rest), the tab bar or the rail, the attribution and, beside a landscape side panel, the
   *  zoom and locate column. A sheet at full leaves nothing: then it is the whole map. */
  function visibleRect() {
    const mr = mapEl.getBoundingClientRect(), W = mr.width, H = mr.height;
    const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return r.width && r.height ? r : null; };
    const open = [detailSheet, layersSheet].find((s) => s && s.isOpen());
    let l = 0, r = W, t = 0, b = H;
    if (M.isPhone()) {
      const pill = box($(".hd-mapbar .hd-search"));
      if (pill && pill.bottom > mr.top) t = pill.bottom - mr.top;
      if (open) b = window.innerHeight - sheetVisible(open) - mr.top;
      else { const tab = box($(".hd-tabbar")); if (tab && tab.top < window.innerHeight) b = tab.top - mr.top; }
    } else if (M.isShort()) {
      const rail = box($(".hd-tabbar"));
      if (rail && rail.right > mr.left) l = rail.right - mr.left;
      if (open) { const sr = box(open.el); if (sr) r = sr.left - mr.left; }
      const side = box($(".hd-mapside"));
      if (side && side.left - mr.left < r && side.right - mr.left > l) r = Math.min(r, side.left - mr.left - 8);
    } else {
      if (layersSheet && layersSheet.isOpen()) { const lr = box($("#hd-sheet-layers")); if (lr) l = lr.right - mr.left; }
      if (detailSheet && detailSheet.isOpen()) { const dr = box($("#hd-sheet-detail")); if (dr) r = dr.left - mr.left; }
    }
    const attr = box($(".hd-map .leaflet-control-attribution"));
    if (attr && attr.top - mr.top > t && attr.top - mr.top < b) b = attr.top - mr.top;
    return r - l < 96 || b - t < 96 ? { l: 0, r: W, t: 0, b: H } : { l, r, t, b };
  }
  U.visibleRect = visibleRect;
  /** Center, Follow and Locate put their target in the middle of what you can see, never under the card that holds
   *  the button you pressed (§7.9, §7.11). Programmatic: it never tucks the chrome. */
  U.centerOn = function (latlng, zoom, animate) {
    if (!M.map) return;
    const z = zoom == null ? M.map.getZoom() : zoom, v = visibleRect(), size = M.map.getSize();
    const cx = (v.l + v.r) / 2, cy = (v.t + v.b) / 2;
    if (z === M.map.getZoom()) {
      const p = M.map.latLngToContainerPoint(latlng);
      if (Math.abs(p.x - cx) < 1 && Math.abs(p.y - cy) < 1) return; // already there
    }
    const target = M.map.unproject(M.map.project(L.latLng(latlng), z).subtract(L.point(cx - size.x / 2, cy - size.y / 2)), z);
    M.map.setView(target, z, { animate: !!animate && !M.reduced() });
  };

  /* rows for "Choose a station" and "In view" */
  let pickItems = [];
  function pickRow(it, trail) {
    const st = M.L(it.layer);
    const r = (st && st.def.row && st.def.row(it)) || null;
    if (!r) return "";
    const i = pickItems.push(it) - 1;
    return K.row(`data-pick="${i}"`, r.lead, r.title, r.meta, trail != null ? trail : r.trail, r.trail2);
  }
  U.pickRow = pickRow;
  function chooserHtml(it) {
    return `${K.head("", "Choose a station", "hd-detail__title")}
      <p class="hd-detail__lede">${it.items.length}${NB}marks are close to where you tapped.</p>
      <ul class="hd-memberlist hd-detail__section">${it.items.map((x) => pickRow(x)).join("")}</ul>`;
  }
  // "In view" sections: On hamdoor, licences, repeaters, APRS, activators, posts (§7.11)
  const INVIEW_ORDER = ["members", "fcc", "rpt", "aprs", "spots", "posts"];
  function inViewHtml() {
    const ctx = M.ctx || {};
    const secs = [];
    const layers = M.layerList.slice().sort((a, b) => INVIEW_ORDER.indexOf(a.id) - INVIEW_ORDER.indexOf(b.id));
    for (const st of layers) {
      if (!M.isOn(st.id) || !st.def.inView) continue;
      let parts;
      try { parts = st.def.inView(st.data, ctx, st); } catch (e) { console.error("[hd] inView", st.id, e); parts = null; }
      for (const s of [].concat(parts || [])) {
        const rows = (s.items || []).map((x) => pickRow(x)).join("");
        if (!rows && !s.more) continue;
        secs.push(`<section class="hd-detail__section"><h3 class="hd-detail__h">${esc(s.heading)}</h3>${rows ? `<ul class="hd-memberlist">${rows}</ul>` : ""}${s.more ? `<p class="hd-memberlist__more">${esc(s.more)}</p>` : ""}</section>`);
      }
    }
    return `${K.head("", "In view", "hd-detail__title")}
      <p class="hd-detail__lede">Everything the map shows here, as a list.</p>
      ${secs.join("") || '<p class="hd-detail__note">Nothing is shown here. Zoom out, or turn layers on in Layers.</p>'}`;
  }

  function bindDetail() {
    const sheet = $("#hd-sheet-detail");
    // "In view" held still while focus was inside it: it catches up once focus leaves the card
    sheet.addEventListener("focusout", (e) => {
      if (!inViewStale || sheet.contains(e.relatedTarget)) return;
      setTimeout(() => { if (inViewStale && !sheet.contains(document.activeElement)) U.refreshDetail(); }, 0);
    });
    sheet.addEventListener("click", (e) => {
      if (e.target.closest("[data-close-detail]")) { U.closeDetail(); return; }
      const pk = e.target.closest("[data-pick]");
      if (pk) {
        const it = pickItems[+pk.dataset.pick];
        if (!it) return;
        if (it.kind === "zoom" && it.zoomTo) { U.closeDetail(); it.zoomTo(); return; }
        const ll = U.latlngOf(it);
        if (ll && M.map.getZoom() < 13 && it.kind !== "mgroup") M.map.setView(ll, 15, { animate: false });
        U.openDetail(it, { keyboard: e.detail === 0 });
        return;
      }
      const c = e.target.closest("[data-copy]");
      if (c) { M.copy(c.dataset.copy, c.dataset.copyMsg || `Copied ${c.dataset.copy}`); return; }
      const go = e.target.closest("[data-goto]");
      if (go) { e.preventDefault(); const args = JSON.parse(go.dataset.goto); HD.router.go(args[0], args[1] || {}, args[2] || {}); return; }
      if (selection) {
        const st = M.L(selection.layer);
        if (st && st.def.onCardClick && st.def.onCardClick(e, selection)) return;
      }
    });
  }

  /* ------------------------------------------------------------ map buttons, zoom hint, grid and live chips */
  function bindButtons() {
    for (const b of $$("[data-zoom]", uiEl)) b.addEventListener("click", () => M.map.setZoom(M.map.getZoom() + Number(b.dataset.zoom), { animate: !M.reduced() }));
    // locate centres on your mark without zooming out past z13
    $("#hd-locate").addEventListener("click", () => {
      const y = M.youPlace();
      if (!y) { M.toast("Add your grid square or address in You to see your neighbourhood."); return; }
      stopFollow("locate"); // a move you asked for elsewhere ends Follow, like a pan (§7.11)
      U.centerOn([y.lat, y.lon], Math.max(M.map.getZoom(), 13), true);
    });
    $("#hd-layers-btn").addEventListener("click", () => (layersSheet.isOpen() ? layersSheet.close() : openLayers()));
    $("#hd-sheet-layers").addEventListener("click", onLayersClick);
    $("#hd-scrim").addEventListener("click", () => layersSheet.close());
    $("#hd-skipmap").addEventListener("click", () => U.openDetail({ layer: "ui", kind: "inview" }, { keyboard: true }));
    $("#hd-zoomhint").addEventListener("click", () => { const t = Number($("#hd-zoomhint").dataset.target) || M.map.getZoom() + 1; M.map.setZoom(Math.max(t, M.map.getZoom() + 1), { animate: !M.reduced() }); });
    $("#hd-search-me").addEventListener("click", () => U.openDetail({ layer: "members", kind: "you" }));
  }
  function syncZoomButtons() {
    const z = M.map.getZoom();
    $('[data-zoom="1"]', uiEl).disabled = z >= M.map.getMaxZoom();
    $('[data-zoom="-1"]', uiEl).disabled = z <= M.map.getMinZoom();
  }
  /** The zoom hint names only layers that are on and hidden at this zoom (§7.10, §9.1). */
  function updateHint(ctx) {
    const hint = $("#hd-zoomhint");
    const z = ctx.zoom, need = [];
    for (const st of M.layerList) {
      if (!M.isOn(st.id) || !st.def.hint) continue;
      const h = st.def.hint(z, st);
      if (h) need.push(h);
    }
    if (!need.length) { if (!hint.hidden) { hint.hidden = true; } return; }
    need.sort((a, b) => (a[2] || 0) - (b[2] || 0)); // "APRS, repeaters and every ham"
    const names = need.map((n) => n[0]);
    const list = names.length === 1 ? names[0] : names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
    const text = `Zoom in to see ${list}`;
    const span = $("[data-hint-text]", hint);
    if (span.textContent !== text) span.textContent = text;
    hint.dataset.target = String(Math.min(...need.map((n) => n[1])));
    hint.hidden = false;
  }
  function updateGrid() {
    const c = M.map.getCenter(), g = M.grid(c.lat, c.lng, 6);
    const el = $("[data-grid-text]", uiEl);
    if (el && el.textContent !== g) el.textContent = g;
  }
  /* the live chip (§7.10): desktop top right; on phones only while stale or failing, in the scale's slot.
     The APRS layer reports {state: ok|stale|error, stations, at} (C's setLive, §15.3). "Stale" also comes from
     the clock: no success for 90 s while updates are expected (map shown, page visible, layer on, z >= 9). A
     request that never answers is aborted by the next poll and reports nothing, so only the age can tell. */
  let live = null, liveAnnounced = "", expectSince = 0, expecting = false;
  const LIVE_STALE_MS = 90000;
  U.setLive = function (s) { live = s; tickLive(); };
  function liveState() {
    if (!live) return "off";
    const exp = M.visible && !document.hidden && M.isOn("aprs") && !!M.map && M.map.getZoom() >= M.MINZ.aprs;
    if (exp && !expecting) expectSince = Date.now(); // a pause (hidden page, low zoom) is not a silence
    expecting = exp;
    if (live.state === "ok" && exp && Date.now() - Math.max(live.at || 0, expectSince) > LIVE_STALE_MS) return "stale";
    return live.state;
  }
  function tickLive() {
    if (!uiEl || !M.visible) return;
    const on = !!live && M.isOn("aprs");
    const state = on ? liveState() : "off";
    if (on && state !== liveAnnounced) {
      // only state changes are announced, never the ticking age (§7.10, §11)
      if (liveAnnounced && liveAnnounced !== "off") M.announce(state === "ok" ? "Live APRS is connected again" : state === "stale" ? "Live APRS is reconnecting" : "Couldn't reach APRS. Trying again in 30 seconds");
      liveAnnounced = state;
    }
    const desk = $("#hd-live"), phone = $("#hd-mapstatus");
    if (body.dataset.live !== state) body.dataset.live = state;
    desk.hidden = !on;
    phone.hidden = !on || state === "ok";
    if (!on) return;
    const ageS = Math.max(0, Math.round((Date.now() - (live.at || Date.now())) / 1000));
    const ageTxt = ageS < 60 ? `${ageS}${NB}s` : ageS < 5400 ? `${Math.round(ageS / 60)}${NB}min` : `${Math.round(ageS / 3600)}${NB}${Math.round(ageS / 3600) === 1 ? "hour" : "hours"}`;
    let main, age;
    if (state === "ok") { main = `Live: ${M.fmtN(live.stations || 0)}${NB}${live.stations === 1 ? "station" : "stations"}`; age = `, updated ${ageTxt} ago`; }
    else if (state === "stale") { main = "Live: reconnecting"; age = `, last update ${ageTxt} ago`; }
    else { main = "Couldn't reach APRS"; age = `. Trying again in 30${NB}s`; }
    for (const chip of [desk, phone]) {
      const t = $("[data-live-text]", chip), a = $("[data-live-age]", chip);
      if (t.textContent !== main) t.textContent = main;
      if (a.textContent !== age) a.textContent = age;
      const st = chip.classList.contains("hd-status") ? chip : $(".hd-status", chip);
      st.classList.toggle("hd-status--error", state === "error");
      st.title = main + age;
    }
  }

  /* after every render: the hint now; labels, counts, chips and credits on the next frame */
  U.afterRender = function (ctx) {
    updateHint(ctx);
    requestAnimationFrame(() => {
      if (!M.visible) return;
      K.layoutLabels();
      if (pendingReveal) { const r = pendingReveal; pendingReveal = null; if (r.it === selection) { K.renderSel(); reveal(r.it, r.animate); } }
      syncLayers(); updateGrid(); syncCredits(); syncAttrHeight(); syncHintClear(); tickLive();
    });
  };
  U.onMoveEnd = () => { scheduleIdle(); };
  U.onShow = () => { reportSheets(); updateMapBottom(); };
  U.onHide = () => {
    if (detailSheet && detailSheet.isOpen()) detailSheet.close({ quiet: true });
    if (layersSheet && layersSheet.isOpen() && !M.isDesk()) layersSheet.close({ quiet: true });
    closeResults();
    sheetReported = null;
    if (HD.chrome && HD.chrome.setSheetOpen) HD.chrome.setSheetOpen(false);
  };
  /* a layer's data arrived: an open list or a card that shows that data is drawn again */
  let dataRefreshTimer = 0;
  U.onLayerData = (st) => {
    const sel = selection;
    if (!sel) return;
    let refresh = false;
    if (sel.layer === "ui" && sel.kind === "inview") refresh = true;
    else if (st.id === "posts" && sel.layer === "members") refresh = true; // a neighbour's latest post
    else if (st.id === sel.layer && st.def.refreshSel) refresh = st.def.refreshSel(sel, st.data);
    if (!refresh) return;
    clearTimeout(dataRefreshTimer);
    dataRefreshTimer = setTimeout(() => { if (selection === sel) U.refreshDetail(sel); }, 120);
  };
  U.onUser = (o = {}) => {
    const me = M.me(), b = $("#hd-search-me");
    if (b) { b.hidden = !me; const s = $("[data-you-call]", b); if (s) s.textContent = me ? me.callsign : ""; }
    if (o.callChanged) {
      // someone else signed in (or nobody is): nothing the last person had open stays open
      U._pendingSel = null;
      if (detailSheet && detailSheet.isOpen()) detailSheet.close({ quiet: true });
      closeResults();
      const inp = input();
      if (inp) inp.value = "";
      lastQ = ""; resItems = [];
      return;
    }
    if (selection && selection.layer === "members") U.refreshDetail(selection);
  };
  /** The neighbour list arrived or changed: an open licence card that turns out to be a person's becomes their red
   *  card (or leaves them out of a shared address), never staying open at their house (§3.1). */
  U.onNeighbours = () => {
    const sel = selection;
    if (!sel) return;
    const st = M.L(sel.layer);
    if (st && st.def.onPersons) {
      const focused = $("#hd-sheet-detail").contains(document.activeElement);
      Promise.resolve(st.def.onPersons(sel)).then((next) => {
        if (selection !== sel) return;
        if (next === sel) { U.refreshDetail(sel); return; }
        if (!next) { U.closeDetail(); return; }
        U.openDetail(next, { quiet: true, keyboard: focused, silent: !focused });
      }, () => {});
      return;
    }
    if (sel.layer === "members") U.refreshDetail(sel);
  };

  /* ------------------------------------------------------------ search (§9.8) */
  let resItems = [], active = -1, searchCtrl = null, searchTimer = 0, lastQ = "";
  const input = () => $("#hd-search-input");
  const results = () => $("#hd-search-results");
  const resultsOpen = () => { const r = results(); return !!r && !r.hidden; };
  function closeResults() {
    const r = results(); if (!r) return;
    if (!r.hidden) { r.hidden = true; input().setAttribute("aria-expanded", "false"); input().removeAttribute("aria-activedescendant"); $(".hd-mapbar").dataset.results = "closed"; relayoutSoon(); }
  }
  const markMatch = (text, q) => {
    const t = String(text || ""), i = t.toUpperCase().indexOf(q.toUpperCase());
    return i !== 0 || !q ? esc(t) : `<mark>${esc(t.slice(0, q.length))}</mark>${esc(t.slice(q.length))}`;
  };
  M.markMatch = markMatch;
  const STATE_NAME = { AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado", CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming", PR: "Puerto Rico", GU: "Guam", VI: "U.S. Virgin Islands", MP: "Northern Mariana Islands", AS: "American Samoa" };
  M.stateName = (st) => STATE_NAME[String(st || "").toUpperCase()] || String(st || "");
  const GRID_RE = /^[A-R]{2}\d{2}([A-X]{2})?$/i;
  const CALL_RE = /^(?=.*\d)[A-Z0-9]{3,7}(-[0-9A-Z]{1,2})?$/i;

  async function runSearch(q) {
    if (searchCtrl) searchCtrl.abort();
    const ctrl = (searchCtrl = new AbortController());
    const sig = ctrl.signal, U2 = q.toUpperCase();
    const me = M.me(), f = M.features();
    // a source that fails is a failure, not an empty answer: it is never reported as "no match" (§14.5). A 4xx other
    // than 429 is the server answering that the question doesn't apply to it (a pasted street address is longer than
    // any APRS name: 422) — no match, not "couldn't reach hamdoor"
    const failed = [];
    const answered = (e) => e && e.status >= 400 && e.status < 500 && e.status !== 429;
    const source = (p) => p.then((rows) => rows || [], (e) => { if (!HD.isAbort(e) && !answered(e)) failed.push(e); return []; });
    const tasks = [
      source(HD.api(`/api/hams/search?q=${encodeURIComponent(q)}&limit=8`, { signal: sig }).then((d) => (d && d.results) || [])),
      source(HD.api(`/api/places/search?q=${encodeURIComponent(q)}&limit=3`, { signal: sig }).then((d) => (d && d.results) || [])),
    ];
    const aprsSt = M.L("aprs");
    // APRS names are 9 characters at most (the server takes up to 32)
    tasks.push(f.aprs && aprsSt && aprsSt.def.search && q.length <= 32 ? source(aprsSt.def.search(q, sig)) : Promise.resolve([]));
    // licence rows need the neighbour list: a neighbour's licence row is dropped (their red row stands for it, §9.8),
    // so without the list no licence row can be shown at all
    tasks.push(M.whenNeighbours(4000));
    let [hams, places, aprs, known] = await Promise.all(tasks);
    if (sig.aborted) return null;
    if (!known) { if (hams.length) failed.push({ status: 0 }); hams = []; }
    // Callsigns: you, then neighbours, then licences (exact first, shorter, alphabetical); up to 4.
    // A licence row whose callsign is you or a neighbour is dropped: the person row stands for it.
    const calls = [];
    if (me && me.callsign.startsWith(U2)) calls.push({ layer: "members", kind: "you" });
    for (const m of M.neighbourList()) if (m.callsign.startsWith(U2)) calls.push({ layer: "members", kind: "member", m: M.L("members").def.person(m) });
    for (const h of hams) { if (calls.length >= 4) break; if (!M.isPerson(h.callsign)) calls.push({ layer: "fcc", kind: "fcc", fromSearch: true, h, lat: h.lat, lon: h.lon, loc: h.loc }); }
    const placeRows = [];
    if (GRID_RE.test(q)) placeRows.push({ type: "grid", g: M.gridFmt(q) });
    for (const p of places) { if (placeRows.length >= 3) break; placeRows.push({ type: "place", p }); }
    return { calls: calls.slice(0, 4), places: placeRows, aprs: aprs.slice(0, 3), failed: failed.length ? failed : null };
  }
  /** §13/§14.5: 429 is "busy"; any other failure (network, 5xx) is "couldn't reach hamdoor" */
  const failText = (errs) => (errs.some((e) => e && e.status === 429) ? "That's busy right now. Try again in a minute." : "Couldn't reach hamdoor. Check your connection and try again.");
  function resultRow(it, q, i) {
    let lead = "", title = "", sub = "", trail = "";
    if (it.type === "grid") { lead = M.ic("center") || K.glyph("pin", 24); title = `Grid ${esc(it.g)}`; sub = "Maidenhead grid square, opens at its centre"; }
    else if (it.type === "place") {
      const p = it.p;
      lead = M.ic("map") || K.glyph("pin", 24);
      if (p.kind === "zip") { title = `ZIP ${markMatch(p.zip, q)}`; sub = `${esc(p.place)}, ${esc(M.stateName(p.state))}`; }
      else { title = markMatch(p.place, q); sub = esc(M.stateName(p.state)); }
      const d = M.distFromHome(p.lat, p.lon); trail = d == null ? "" : M.mi(d);
    } else if (it.type === "lookup") { lead = M.ic("external") || ""; title = `Look up ${esc(it.call)} on aprs.fi`; sub = it.sub ? esc(it.sub) : "Asks aprs.fi for its last position"; }
    else {
      const st = M.L(it.layer), r = st && st.def.searchRow ? st.def.searchRow(it, q) : st.def.row(it);
      lead = r.lead; title = r.title; sub = r.meta; trail = r.trail || "";
    }
    return `<div class="hd-results__item" role="option" id="hd-res-${i}" aria-selected="false" data-i="${i}"><span class="hd-results__lead">${lead}</span><span class="hd-results__text"><span class="hd-results__title">${title}</span><span class="hd-results__sub">${sub}</span></span><span class="hd-results__trail">${trail}</span></div>`;
  }
  function renderResults(q, groups) {
    const r = results();
    resItems = []; active = -1;
    let html = "", i = 0;
    const add = (label, items) => {
      if (!items.length) return;
      html += `<div class="hd-results__group" role="presentation">${label}</div>`;
      for (const it of items) { html += resultRow(it, q, i); resItems.push(it); i++; }
    };
    add("Callsigns", groups.calls.map((x) => x));
    add("Places", groups.places);
    add("APRS stations", groups.aprs);
    if (!resItems.length && groups.failed) {
      // nothing came back because hamdoor did not answer: say so, never "check the letters" (§14.5)
      html = `<p class="hd-results__empty">${failText(groups.failed)}</p>`;
      M.announce(failText(groups.failed));
    } else if (!resItems.length) {
      html = `<p class="hd-results__empty">No callsign, place or APRS station matches “${esc(q)}”. Check the letters and zeros.</p>`;
      if (M.features().aprsfi_lookup && CALL_RE.test(q)) {
        const it = { type: "lookup", call: q.toUpperCase() };
        html += resultRow(it, q, 0); resItems.push(it);
      }
    } else if (groups.failed) html += `<p class="hd-results__empty">${failText(groups.failed)}</p>`; // what came back, and that some did not
    r.innerHTML = html;
    r.hidden = false;
    input().setAttribute("aria-expanded", "true");
    $(".hd-mapbar").dataset.results = "open";
  }
  function setActive(i) {
    active = i;
    for (const el of $$(".hd-results__item", results())) el.setAttribute("aria-selected", String(+el.dataset.i === i));
    if (i >= 0) { input().setAttribute("aria-activedescendant", "hd-res-" + i); const el = $("#hd-res-" + i); if (el) el.scrollIntoView({ block: "nearest" }); }
    else input().removeAttribute("aria-activedescendant");
  }
  /** Choosing a result flies there (no tuck) and opens its card. */
  function chooseResult(i) {
    const it = resItems[i];
    if (!it) return;
    if (it.type === "lookup") { lookupAprsFi(it); return; }
    closeResults();
    input().blur();
    const fly = (ll, z, then) => {
      const go = () => { if (then) then(); };
      const c = M.map.getCenter();
      if (Math.abs(c.lat - ll[0]) < 1e-6 && Math.abs(c.lng - ll[1]) < 1e-6 && M.map.getZoom() === z) { go(); return; }
      M.map.once("moveend", go);
      M.map.setView(ll, z, { animate: !M.reduced() });
    };
    // a place or grid is somewhere else, not a station: the card that was open (and its sel in the URL) goes
    if (it.type === "grid") { const c = M.gridCentre(it.g); if (c) { if (detailSheet) detailSheet.close({ quiet: true }); fly([c.lat, c.lon], it.g.length > 4 ? 14 : 10); } return; }
    if (it.type === "place") { if (detailSheet) detailSheet.close({ quiet: true }); fly([it.p.lat, it.p.lon], it.p.kind === "zip" ? 14 : 13); return; }
    const ll = U.latlngOf(it);
    const z = it.layer === "fcc" ? 16 : it.layer === "aprs" ? 14 : 15;
    if (!ll) { U.openDetail(it, { keyboard: lastInput === "keyboard" }); return; }
    fly(ll, Math.max(z, Math.min(M.map.getZoom(), 17)), () => U.openDetail(it, { keyboard: lastInput === "keyboard" }));
  }
  async function lookupAprsFi(it) {
    try {
      const d = await HD.api(`/api/aprs/lookup/${encodeURIComponent(it.call)}`);
      if (!d || !d.found || !M.finite(d.lat) || !M.finite(d.lon)) { it.sub = `aprs.fi has no recent position for ${it.call}`; renderResults(input().value.trim(), { calls: [], places: [], aprs: [] }); return; }
      closeResults(); input().blur();
      const item = M.L("aprs").def.fromLookup(d);
      M.map.once("moveend", () => U.openDetail(item));
      M.map.setView([d.lat, d.lon], 13, { animate: !M.reduced() });
    } catch (e) {
      if (e.status === 429) M.toast("aprs.fi lookups are busy. Try again in a minute.");
      else M.toast("Couldn't reach hamdoor. Check your connection and try again.");
    }
  }
  function bindSearch() {
    const inp = input();
    const run = () => {
      const q = inp.value.trim();
      lastQ = q;
      if (q.length < 2) { if (searchCtrl) searchCtrl.abort(); closeResults(); return; }
      runSearch(q).then((g) => { if (g && lastQ === q && document.activeElement === inp) renderResults(q, g); }).catch(() => {});
    };
    inp.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(run, 250); });
    inp.addEventListener("focus", () => { if (inp.value.trim().length >= 2 && resItems.length) { results().hidden = false; inp.setAttribute("aria-expanded", "true"); $(".hd-mapbar").dataset.results = "open"; } });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); if (resultsOpen()) setActive(Math.min(resItems.length - 1, active + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); if (resultsOpen()) setActive(Math.max(-1, active - 1)); }
      else if (e.key === "Enter") { e.preventDefault(); if (resultsOpen() && resItems.length) chooseResult(active >= 0 ? active : 0); else run(); }
      else if (e.key === "Escape") {
        e.preventDefault(); e.stopPropagation();
        if (resultsOpen()) closeResults(); else { inp.value = ""; lastQ = ""; inp.blur(); }
      }
    });
    const r = results();
    r.addEventListener("mousedown", (e) => e.preventDefault()); // keep focus in the field while choosing
    r.addEventListener("click", (e) => { const el = e.target.closest(".hd-results__item"); if (el) chooseResult(+el.dataset.i); });
  }

  /* ------------------------------------------------------------ gestures -> the chrome controller (§8.2) */
  const AH = {};
  const readAH = () => { AH.drag = M.cssNum("--hd-ah-tuck-drag-px", 10); AH.tapMs = M.cssNum("--hd-ah-tap-max-ms", 250); AH.tapPx = M.cssNum("--hd-ah-tap-max-px", 10);
    AH.idle = M.cssNum("--hd-ah-idle-return-ms", 3000); AH.tapWait = M.cssNum("--hd-ah-tap-wait-ms", 260); AH.pick = M.cssNum("--hd-pick-px", 22); };
  const tucked = () => (HD.chrome && HD.chrome.isTucked ? HD.chrome.isTucked() : root.classList.contains("hd-tucked"));
  const stickyTuck = () => root.classList.contains("hd-tucked-sticky") || !!document.querySelector('[data-tuck-kind="sticky"]');
  function tuck(reason, sticky) {
    if (!M.visible || M.isDesk() || !HD.chrome || !HD.chrome.tuck) return;
    // focus never stays inside chrome that leaves: the map takes it first
    const ae = document.activeElement;
    if (ae && ae !== mapEl && !isEditable(ae) && ae.closest && ae.closest("#hd-map-ui [data-tuck]")) mapEl.focus({ preventScroll: true });
    HD.chrome.tuck(reason, sticky ? { sticky: true } : {});
  }
  function showChrome(reason) { if (HD.chrome && HD.chrome.show) HD.chrome.show(reason); }
  let gesture = { blocking: false, sheet: false }, downCount = 0, idleTimer = 0;
  const pointers = new Set();
  let ptr = null, lastTap = null, tapTimer = 0;
  function scheduleIdle() {
    clearTimeout(idleTimer);
    if (!M.visible || pointers.size || !tucked() || stickyTuck()) return;
    // 3 s after the last moveend with no pointer down, an auto tuck returns (sticky tucks have no timer)
    idleTimer = setTimeout(() => { if (!pointers.size && tucked() && !stickyTuck()) showChrome("idle"); }, AH.idle);
  }
  /** "Choose a station": every DOM mark and canvas licence whose centre is within 22 px of the tap (§9.7) */
  function pickAt(x, y) {
    const cands = [];
    for (const el of mapEl.querySelectorAll(".hd-mark")) {
      const m = el._hdMarker;
      if (!m || !m._hd || !m._hd.item || m._hd.interactive === false) continue;
      const r = el.getBoundingClientRect();
      if (!r.width) continue;
      const d = Math.hypot(r.left + r.width / 2 - x, r.top + r.height / 2 - y);
      if (d <= AH.pick) cands.push({ d, item: m._hd.item, desc: m._hd });
    }
    for (const st of M.layerList) if (st.def.pick && M.isOn(st.id)) for (const c of st.def.pick(x, y, AH.pick, st)) cands.push(c);
    if (!cands.length) return 0;
    cands.sort((a, b) => a.d - b.d);
    // the same item can show twice (a mark and its canvas twin): keep the nearest
    const seen = new Set(), uniq = [];
    for (const c of cands) { const k = selIdOf(c.item) || c.item; if (seen.has(k)) continue; seen.add(k); uniq.push(c); }
    K.suppressClickUntil = performance.now() + 450;
    if (uniq.length === 1) { U.activate(uniq[0].desc || { item: uniq[0].item }, null, { pointer: true }); return 1; }
    U.openDetail({ layer: "ui", kind: "chooser", items: uniq.slice(0, 8).map((c) => c.item) }, { pointer: true });
    return uniq.length;
  }
  /** Is a pointer on a mark? A DOM mark under it, or a canvas licence within Leaflet's hit tolerance.
      (The licence canvas covers the whole map, and Leaflet leaves its hover class on it, so the
      element under the pointer says nothing.) */
  function onMarkAt(e) {
    if (e.target.closest && e.target.closest(".leaflet-marker-icon")) return true;
    const st = M.L("fcc");
    if (!st || !st.def.pick || !M.isOn("fcc")) return false;
    const tol = e.pointerType === "mouse" ? M.cssNum("--hd-canvas-tol-fine", 3) : M.cssNum("--hd-canvas-tol-coarse", 14);
    return st.def.pick(e.clientX, e.clientY, tol + 9, st).length > 0;
  }
  function stopFollow(why) { const st = M.L("aprs"); if (st && st.def.stopFollow) st.def.stopFollow(why); }
  M.stopFollow = stopFollow;
  U._gestureState = () => ({ gesture, downCount, pointers: pointers.size, lastTap, ptr: !!ptr }); // for tests
  function bindGestures() {
    readAH();
    // A gesture that starts while results, a field, the keyboard or a sheet is open only dismisses that thing.
    // Recorded at the first pointerdown, in the capture phase, because results close on that same pointerdown.
    document.addEventListener("pointerdown", (e) => {
      lastInput = "pointer"; lastPointerAt = performance.now();
      if (downCount === 0) {
        gesture = {
          blocking: resultsOpen() || isEditable(document.activeElement) || keyboardUp(),
          sheet: !!detailSheet && (detailSheet.isOpen() || (layersSheet.isOpen() && !M.isDesk())),
        };
      }
      downCount++;
      if (!e.target.closest || !e.target.closest(".hd-search")) closeResults();
    }, true);
    const up = () => { downCount = Math.max(0, downCount - 1); };
    document.addEventListener("pointerup", up, true);
    document.addEventListener("pointercancel", up, true);
    document.addEventListener("keydown", () => { lastInput = "keyboard"; }, true);
    mapEl.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".leaflet-control-container")) return;
      pointers.add(e.pointerId);
      clearTimeout(idleTimer);
      if (gesture.blocking && isEditable(document.activeElement)) document.activeElement.blur(); // the field blurs
      if (pointers.size === 1) ptr = { id: e.pointerId, type: e.pointerType, x: e.clientX, y: e.clientY, lx: e.clientX, ly: e.clientY, t: performance.now(), travel: 0, onMark: onMarkAt(e) };
      else { stopFollow("gesture"); if (!gesture.blocking && !gesture.sheet) tuck("pinch"); }
    }, { passive: true });
    mapEl.addEventListener("pointermove", (e) => {
      if (!ptr || e.pointerId !== ptr.id) return;
      ptr.travel += Math.hypot(e.clientX - ptr.lx, e.clientY - ptr.ly); ptr.lx = e.clientX; ptr.ly = e.clientY;
      if (ptr.travel > AH.drag && !ptr.dragged) {
        ptr.dragged = true;
        stopFollow("gesture");
        if (!tucked() && !gesture.blocking && !gesture.sheet) tuck("drag");
      }
    }, { passive: true });
    const ptrEnd = (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.delete(e.pointerId);
      if (ptr && e.pointerId === ptr.id) {
        const isTap = ptr.travel < AH.tapPx && performance.now() - ptr.t <= AH.tapMs;
        if (isTap && gesture.blocking) { K.suppressClickUntil = performance.now() + 450; lastTap = { dismiss: true }; }
        else if (isTap && ptr.type !== "mouse" && pickAt(ptr.x, ptr.y)) lastTap = null;
        else lastTap = isTap && !ptr.onMark ? { dismiss: gesture.blocking || gesture.sheet } : null;
        ptr = null;
      }
      if (!pointers.size) scheduleIdle();
    };
    addEventListener("pointerup", ptrEnd, { passive: true });
    addEventListener("pointercancel", ptrEnd, { passive: true });
    // a wheel has no pointerdown, so the last pointer gesture says nothing about it: the controller itself
    // refuses while a sheet, a field, results or the keyboard is up (§8.2 "never tuck while")
    mapEl.addEventListener("wheel", () => { stopFollow("gesture"); tuck("wheel"); }, { passive: true });
    M.map.on("dblclick", () => { clearTimeout(tapTimer); stopFollow("gesture"); if (!gesture.blocking && !gesture.sheet) tuck("dblclick"); });
    M.map.on("click", () => {
      const tap = lastTap;
      lastTap = null;
      if (!tap || performance.now() < K.suppressClickUntil && !tap.dismiss) return;
      // wait longer than Leaflet's 200 ms double-tap window; a second tap replaces the first
      clearTimeout(tapTimer);
      tapTimer = setTimeout(() => {
        if (tap.dismiss) {
          if (isEditable(document.activeElement)) document.activeElement.blur();
          if (detailSheet.isOpen()) U.closeDetail();
          return;
        }
        if (detailSheet.isOpen()) { U.closeDetail(); return; }
        if (tucked()) showChrome("tap"); else tuck("tap", true);
      }, AH.tapWait);
    });
    M.map.on("movestart", () => clearTimeout(idleTimer));
  }

  /* ------------------------------------------------------------ the keyboard on the map (§9.6) */
  function markCands() {
    const mr = mapEl.getBoundingClientRect(), obs = K.chromeRects(), out = [];
    for (const el of mapEl.querySelectorAll(".hd-mark")) {
      if (!el._hdMarker || el.getAttribute("role") !== "button") continue;
      const r = el.getBoundingClientRect();
      if (!r.width) continue;
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      if (x < mr.left + 4 || x > mr.right - 4 || y < mr.top + 4 || y > mr.bottom - 4) continue;
      if (obs.some((o) => x >= o.left && x <= o.right && y >= o.top && y <= o.bottom)) continue; // never focus something under chrome
      out.push({ el, x, y });
    }
    return out;
  }
  function focusMark(el) {
    el.focus({ preventScroll: true });
    mapEl.scrollTop = 0; mapEl.scrollLeft = 0; // the container never scrolls
    const m = el._hdMarker;
    if (m && m._hd && m._hd.item) reveal(m._hd.item, false);
  }
  function bindKeyboard() {
    mapEl.addEventListener("keydown", (e) => {
      const t = e.target;
      if (t === mapEl) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          const c = markCands();
          if (!c.length) { M.announce("Nothing to select here. Zoom in, or use List what's in view."); return; }
          const mr = mapEl.getBoundingClientRect(), cx = mr.left + mr.width / 2, cy = mr.top + mr.height / 2;
          c.sort((a, b) => Math.hypot(a.x - cx, a.y - cy) - Math.hypot(b.x - cx, b.y - cy));
          focusMark(c[0].el);
        } else if (/^Arrow/.test(e.key) || e.key === "+" || e.key === "-" || e.key === "=") stopFollow("keyboard");
        return;
      }
      if (!t.classList || !t.classList.contains("hd-mark")) return;
      const dirs = { ArrowRight: [1, 0], ArrowLeft: [-1, 0], ArrowDown: [0, 1], ArrowUp: [0, -1] };
      if (dirs[e.key]) {
        e.preventDefault(); e.stopPropagation();
        const r = t.getBoundingClientRect(), x0 = r.left + r.width / 2, y0 = r.top + r.height / 2, [dx, dy] = dirs[e.key];
        let best = null, bestS = Infinity;
        for (const c of markCands()) {
          if (c.el === t) continue;
          const vx = c.x - x0, vy = c.y - y0, along = vx * dx + vy * dy, side = Math.abs(vx * dy - vy * dx);
          if (along <= 0 || side > along) continue; // a 90 degree cone around the arrow
          const sc = along + 2 * side;
          if (sc < bestS) { bestS = sc; best = c; }
        }
        if (best) focusMark(best.el);
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        returnFocus = t;
        const m = t._hdMarker;
        if (m && m._hd) U.activate(m._hd, null, { keyboard: true });
      } else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); mapEl.focus({ preventScroll: true }); }
    });
    // Escape closes the card (or the phone layers sheet); the search field handles its own Escape
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || e.defaultPrevented || !M.visible) return;
      if (document.activeElement === input()) return;
      if (detailSheet.isOpen()) { e.preventDefault(); U.closeDetail(); }
      else if (layersSheet.isOpen() && !M.isDesk()) { e.preventDefault(); layersSheet.close(); }
    });
  }
})();
