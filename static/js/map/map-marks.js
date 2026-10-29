/* hamdoor map 2/9: marks and labels. DESIGN_SYSTEM.md §3, §5, §9.2 to §9.4.
   - canvas licence marks: L.Canvas.include({ _hdMark }) plus a CircleMarker
     subclass (dot, star for new, hollow ring for expired, the shared-address
     copy, the white ring with its hairline on light land, red ring for members
     farther away); colours are read from the map-tone tokens at draw time
   - track lines with fix dots on canvas
   - keyed diffing of DOM marks and canvas marks: a render removes what left,
     adds what arrived and touches only what changed, so cards and keyboard
     focus survive every pan (§9.4)
   - the label placer: one greedy pass per moveend over a 48 px occupancy grid,
     chrome first, then marks, DOM labels by priority, FCC dots, shared-address
     tags and canvas callsigns (§9.2); "You" and the selected station are placed
     together, so they keep clear of each other
   - the rim allocator for marks riding postmarks below z10, and for a selected
     station your dot would cover (§3.2)
   - the selection ring, and the HTML of marks, badges and card parts. */
(function () {
  "use strict";
  const HD = window.HD;
  const L = window.L;
  const M = HD.map._;
  const K = (M.marks = {});
  const NB = M.NB;

  /* ------------------------------------------------------------ canvas licence marks (§3.1) */
  const dotR = (z) => (z >= 16 ? 5.5 : z >= 15 ? 5 : z >= 14 ? 4.5 : z >= 13 ? 4 : z >= 12 ? 3.5 : z >= 11 ? 3 : 2.5);
  K.dotR = dotR;
  /** the drawn radius of a licence mark at a zoom */
  K.markR = (o, z) => (o.star ? dotR(z) + 3.5 : o.exp ? Math.max(dotR(z), 3.5) : dotR(z));
  L.Canvas.include({
    _hdMark(layer) {
      if (!this._drawing || layer._empty()) return;
      const p = layer._point, ctx = this._ctx, r = layer._radius, o = layer.options, T = M.T, z = layer._map ? layer._map.getZoom() : 12;
      const fill = o.exp ? T.exF : T[o.g] || T.O;
      const ring = o.exp ? T.exS : o.mem ? T.member : T.ring;
      const w = o.exp ? (z >= 14 ? 2.5 : 2) : o.mem ? 2 : z >= 13 ? 1.5 : 1;
      const hair = !o.exp && M.tone === "light" && z >= 12 ? T.hair : null;
      const shape = (x, y) => {
        ctx.beginPath();
        if (o.star) {
          for (let i = 0; i < 10; i++) {
            const a = -Math.PI / 2 + (i * Math.PI) / 5, rr = i % 2 ? r * 0.5 : r;
            const px = x + rr * Math.cos(a), py = y + rr * Math.sin(a);
            if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py);
          }
          ctx.closePath();
        } else ctx.arc(x, y, r, 0, Math.PI * 2);
      };
      ctx.lineJoin = "round";
      ctx.globalAlpha = 1;
      if (o.stack) { // a shared address: a 55 % copy of the dot, offset up and right
        ctx.globalAlpha = 0.55; shape(p.x + 3, p.y - 3); ctx.fillStyle = fill; ctx.fill();
        ctx.globalAlpha = 1; ctx.lineWidth = w; ctx.strokeStyle = ring; ctx.stroke();
      }
      if (hair) { shape(p.x, p.y); ctx.lineWidth = w + 2; ctx.strokeStyle = hair; ctx.stroke(); }
      shape(p.x, p.y); ctx.fillStyle = fill; ctx.fill();
      ctx.lineWidth = w; ctx.strokeStyle = ring; ctx.stroke();
    },
  });
  const Mark = L.CircleMarker.extend({
    _project() {
      if (this._map) this._radius = K.markR(this.options, this._map.getZoom());
      L.CircleMarker.prototype._project.call(this);
    },
    _updatePath() { this._renderer._hdMark(this); },
  });
  K.Mark = Mark;

  /** A polyline that also draws its fix dots (every point but the newest) from z13 (§3.4). */
  K.TrackLine = L.Polyline.extend({
    _updatePath() {
      const r = this._renderer;
      r._updatePoly(this);
      const o = this.options;
      if (!o.dots || !r._drawing || !this._rings || !this._rings[0]) return;
      const ctx = r._ctx, pts = this._rings[0];
      ctx.globalAlpha = o.opacity == null ? 1 : o.opacity;
      for (let i = 0; i < pts.length - 1; i++) {
        ctx.beginPath(); ctx.arc(pts[i].x, pts[i].y, 3.5, 0, Math.PI * 2);
        ctx.fillStyle = o.color; ctx.fill(); ctx.lineWidth = 1.5; ctx.strokeStyle = M.T.ring; ctx.stroke();
      }
      ctx.globalAlpha = 1;
    },
  });

  /* ------------------------------------------------------------ keyed diffing (§9.4) */
  K.suppressClickUntil = 0; // a finger tap that opened "Choose a station" swallows the click that follows
  function onMarkClick(e) {
    if (performance.now() < K.suppressClickUntil) return;
    const d = e.target && e.target._hd;
    if (d && M.ui) M.ui.activate(d, e);
  }
  const mkIcon = (d) => L.divIcon({ className: "hd-mark" + (d.cls ? " " + d.cls : ""), iconSize: d.size, iconAnchor: d.anchor || [d.size[0] / 2, d.size[1] / 2], html: d.html });
  /** desired: Map(key -> {latlng, size, anchor?, pane, html, title, item, z?, lbl?, minZ?, pri?, always?, noFlip?}) */
  K.syncMarkers = function (store, desired) {
    const map = M.map;
    for (const [k, m] of store) if (!desired.has(k) || desired.get(k).pane !== m._hd.pane) { map.removeLayer(m); store.delete(k); }
    for (const [k, d] of desired) {
      let m = store.get(k);
      if (!m) {
        m = L.marker(d.latlng, { icon: mkIcon(d), pane: d.pane, keyboard: false, autoPanOnFocus: false, bubblingMouseEvents: false, zIndexOffset: d.z || 0, interactive: d.interactive !== false });
        m._hd = d;
        m.on("click", onMarkClick);
        m.addTo(map);
        store.set(k, m);
      } else {
        const o = m._hd;
        if (o.html !== d.html || o.size[0] !== d.size[0] || o.size[1] !== d.size[1] || String(o.anchor) !== String(d.anchor) || o.cls !== d.cls) m.setIcon(mkIcon(d));
        const ll = m.getLatLng();
        if (ll.lat !== d.latlng[0] || ll.lng !== d.latlng[1]) m.setLatLng(d.latlng);
        if ((o.z || 0) !== (d.z || 0)) m.setZIndexOffset(d.z || 0);
        m._hd = d;
      }
      const el = m.getElement();
      if (el) {
        // the map is one tab stop: marks take focus only through the roving model (§9.6).
        // (A div's tabIndex property reads -1 without the attribute, so test the attribute.)
        if (el.getAttribute("tabindex") !== "-1") el.setAttribute("tabindex", "-1");
        if (d.interactive === false) { el.removeAttribute("role"); el.setAttribute("aria-hidden", "true"); }
        else { if (el.getAttribute("role") !== "button") el.setAttribute("role", "button"); if (el.getAttribute("aria-label") !== d.title) el.setAttribute("aria-label", d.title || ""); }
        el._hdMarker = m;
      }
    }
  };
  K.clearStore = (store) => { for (const m of store.values()) M.map.removeLayer(m); store.clear(); };

  function onCanvasClick(e) {
    if (performance.now() < K.suppressClickUntil) return;
    const d = e.target && e.target._hd;
    if (d && M.ui) M.ui.activate(d, e);
  }
  /** Canvas marks by key; draw order follows d.rank (Other, Technician, General, Extra, expired, new). */
  K.syncCanvas = function (store, desired, renderer) {
    const map = M.map;
    let changed = false;
    for (const [k, m] of store) if (!desired.has(k)) { map.removeLayer(m); store.delete(k); }
    for (const [k, d] of desired) {
      let m = store.get(k);
      if (!m) {
        m = new Mark(d.latlng, { renderer, g: d.g, star: d.star, exp: d.exp, stack: d.stack, mem: d.mem, rank: d.rank, weight: 1.5, bubblingMouseEvents: false, interactive: true });
        m._hd = d; m._sig = d.sig;
        m.on("click", onCanvasClick);
        m.addTo(map);
        store.set(k, m);
        changed = true;
      } else {
        if (m._sig !== d.sig) {
          if (m.options.rank !== d.rank) changed = true;
          Object.assign(m.options, { g: d.g, star: d.star, exp: d.exp, stack: d.stack, mem: d.mem, rank: d.rank });
          m._sig = d.sig;
          m.redraw();
        }
        m._hd = d;
      }
    }
    if (changed) reorder(renderer);
  };
  function reorder(renderer) {
    let ok = true, prev = -Infinity;
    for (let o = renderer._drawFirst; o; o = o.next) { const r = o.layer.options.rank || 0; if (r < prev) { ok = false; break; } prev = r; }
    if (ok) return;
    const all = [];
    for (let o = renderer._drawFirst; o; o = o.next) all.push(o.layer);
    all.sort((a, b) => (a.options.rank || 0) - (b.options.rank || 0));
    for (const l of all) renderer._bringToFront(l);
  }

  /* ------------------------------------------------------------ shapes and glyphs */
  K.starPath = (r, k = 0.5) => {
    let d = "";
    for (let i = 0; i < 10; i++) {
      const a = -Math.PI / 2 + (i * Math.PI) / 5, rr = i % 2 ? r * k : r;
      d += (i ? "L" : "M") + (rr * Math.cos(a)).toFixed(2) + " " + (rr * Math.sin(a)).toFixed(2);
    }
    return d + "Z";
  };
  // mark glyphs drawn inside map marks (the chrome's icons come from HD.icon)
  const GLYPH = {
    tree: '<path d="M12 3 6.5 10h3L6 15h12l-3.5-5h3L12 3Z" fill="currentColor" stroke="none"/><path d="M12 15v5.5" stroke-width="2.6"/>',
    general: '<path d="M4 10v4h3l7 4.5V5.5L7 10H4Z"/><path d="M17.5 9a4 4 0 0 1 0 6"/>',
    tag: '<path d="M3.5 12.2V4.5a1 1 0 0 1 1-1h7.7l8.3 8.3-8.7 8.7-8.3-8.3Z"/><circle cx="8.3" cy="8.3" r="1.4"/>',
    calendar: '<rect x="4" y="5.5" width="16" height="14.5" rx="2"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
    help: '<path d="M8 20v-2.5l-3-3V9.8a1.4 1.4 0 0 1 2.8 0v3.4"/><path d="M7.8 13.2V5.4a1.4 1.4 0 0 1 2.8 0v6.8M10.6 11.5V4.4a1.4 1.4 0 0 1 2.8 0v7.1M13.4 11.6V5.6a1.4 1.4 0 0 1 2.8 0v8.1c0 2.8-1.6 4.8-3.6 6.3"/>',
    elmer: '<path d="m12 4.5 9.5 4.5-9.5 4.5L2.5 9 12 4.5Z"/><path d="M6.5 11v4.2c1.4 1.6 3.3 2.3 5.5 2.3s4.1-.7 5.5-2.3V11M21.5 9v5.5"/>',
    pin: '<path d="M12 21s-6.5-6.2-6.5-11a6.5 6.5 0 0 1 13 0c0 4.8-6.5 11-6.5 11Z"/><circle cx="12" cy="10" r="2.3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
  };
  /** A mark glyph as an inline SVG (stroke currentColor, 24-unit grid). */
  K.glyph = (name, size = 20, style = "") => `<svg class="hd-icon" viewBox="0 0 24 24" width="${size}" height="${size}" style="width:${size}px;height:${size}px;${style}" aria-hidden="true">${GLYPH[name] || ""}</svg>`;
  /** post categories (API values) -> label and glyph */
  K.CAT = { general: ["General", "general"], "for-sale": ["For sale", "tag"], "nets-events": ["Nets and events", "calendar"], "help-wanted": ["Help wanted", "help"], elmer: ["Elmer mentoring", "elmer"] };
  K.catOf = (c) => K.CAT[c] || K.CAT.general;

  /** A neighbour's mark: ring-colour disc, red ring (dashed when approximate), their class dot or star (§3.2). */
  K.memberSvg = (m, size = 24) => {
    const c = M.CLASS_VAR[M.grp(m.license_class || "")];
    const inner = m.new
      ? `<path d="${K.starPath(6.6)}" style="fill:${c};stroke:var(--ov-ring)" stroke-width="1.3" stroke-linejoin="round"/>`
      : `<circle r="5" style="fill:${c};stroke:var(--ov-ring)" stroke-width="1.4"/>`;
    const ring = `<circle r="9.2" fill="none" style="stroke:var(--member)" stroke-width="3.2"${m.approx ? ' stroke-dasharray="4.2 2.6"' : ""}/>`;
    return `<svg viewBox="-12 -12 24 24" width="${size}" height="${size}" aria-hidden="true"><circle r="11.2" style="fill:var(--ov-ring)"/>${ring}${inner}</svg>`;
  };
  /** your mark (static, never pulsing) */
  K.youDot = (approx, size = 18, halo = true) => `<span class="hd-mk-you${approx ? " hd-mk-you--approx" : ""}" style="display:block;position:relative;width:${size}px;height:${size}px">${halo ? '<span class="hd-mk-you__halo"></span>' : ""}<span class="hd-mk-you__dot"></span></span>`;
  /** a post's speech bubble (26 px) or its badge on an author's mark (20 px) */
  K.postSvg = (cat, size = 26) => {
    const [, g] = K.catOf(cat);
    return `<svg viewBox="0 0 26 26" width="${size}" height="${size}" aria-hidden="true"><path d="M5 2.5h16A2.5 2.5 0 0 1 23.5 5v11a2.5 2.5 0 0 1-2.5 2.5h-7.5l-5 4.5v-4.5H5A2.5 2.5 0 0 1 2.5 16V5A2.5 2.5 0 0 1 5 2.5Z" style="fill:var(--post);stroke:var(--ov-ring)" stroke-width="1.5" stroke-linejoin="round"/><svg x="6" y="3.5" width="14" height="14" viewBox="0 0 24 24" fill="none" style="stroke:var(--post-ink)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">${GLYPH[g]}</svg></svg>`;
  };
  K.postBadge = (cat, inline) => {
    const [, g] = K.catOf(cat);
    const cls = inline ? "" : ' class="hd-mk-postbadge"';
    return `<svg${cls} viewBox="0 0 20 20" width="20" height="20" aria-hidden="true"${inline ? ' style="flex:none"' : ""}><path d="M3.6 1.5h12.8a2.1 2.1 0 0 1 2.1 2.1v8.3a2.1 2.1 0 0 1-2.1 2.1h-5.6l-3.9 3.6v-3.6H3.6a2.1 2.1 0 0 1-2.1-2.1V3.6a2.1 2.1 0 0 1 2.1-2.1Z" style="fill:var(--post);stroke:var(--ov-ring)" stroke-width="1.4" stroke-linejoin="round"/><svg x="5" y="3" width="10" height="10" viewBox="0 0 24 24" fill="none" style="stroke:var(--post-ink)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">${GLYPH[g]}</svg></svg>`;
  };
  /** POTA: rounded square with a tree; SOTA: a triangle. Same green, different shape (§3.5). */
  K.spotBadge = (kind) => kind === "pota"
    ? `<div class="hd-mk-spot__badge">${K.glyph("tree", 16)}</div>`
    : '<div class="hd-mk-spot__badge"><svg viewBox="0 0 24 22" width="24" height="22" aria-hidden="true"><path d="M12 2.4 22.4 20.2H1.6Z" style="fill:var(--spot);stroke:var(--ov-ring)" stroke-width="2" stroke-linejoin="round"/></svg></div>';
  /** hessu/aprs-symbols sprite: index = charCode - 33, 16 x 6 cells; overlays from sheet 2 (§3.4) */
  K.spriteHtml = (sym, cell = 24) => {
    let tab = sym && sym.table, code = sym && sym.code;
    const ovl = sym && sym.overlay;
    const ok = (c) => typeof c === "string" && c.length === 1 && c.charCodeAt(0) >= 33 && c.charCodeAt(0) <= 126;
    if (!ok(code)) { tab = "/"; code = "/"; }
    const prim = tab !== "\\";
    const pos = (ch) => { const i = ch.charCodeAt(0) - 33; return `${-(i % 16) * cell}px ${-Math.floor(i / 16) * cell}px`; };
    const sz = cell === 24 ? "" : `width:${cell}px;height:${cell}px;background-size:${16 * cell}px ${6 * cell}px;`;
    let h = `<span class="hd-mk-aprs__sym hd-mk-aprs__sym--${prim ? "p" : "a"}" style="${sz}background-position:${pos(code)}"></span>`;
    if (!prim && ok(ovl)) h += `<span class="hd-mk-aprs__ovl" style="${sz}background-position:${pos(ovl)}"></span>`;
    return h;
  };
  /** The class badge: a ring in the class colour around the letter; a star outline when new (§7.5). `cls` null or
   *  undefined = unknown (a member the FCC directory has no row for): an empty Other ring, as HD.ui.classBadge draws
   *  it in the Feed; "" is a club licence, "C". */
  K.classBadge = (cls, isNew) => {
    if (cls == null) return '<span class="hd-class hd-class--o" title="Licence class not listed"></span>';
    const g = M.grp(cls || "").toLowerCase(), letter = M.letterOf(cls || "");
    if (isNew) return `<span class="hd-class hd-class--${g} hd-class--new" title="Newly licensed ${M.esc(M.classShort(cls || ""))}"><svg viewBox="-16 -16 32 32" aria-hidden="true"><path d="${K.starPath(15, 0.56)}" style="fill:var(--hd-surface);stroke:var(--c)" stroke-width="3" stroke-linejoin="round"/></svg><span>${letter}</span></span>`;
    return `<span class="hd-class hd-class--${g}" title="${M.esc(M.classShort(cls || ""))}">${letter}</span>`;
  };
  /** repeater band key and the number printed on its cap (§3.3) */
  K.bandKey = (band) => (band === "2m" ? "2m" : band === "70cm" ? "70cm" : "other");
  // the band as hams say it; the rare bands outside the US repeater list keep their unit so "6" stays six metres
  K.bandNum = (band) => ({ "10m": "10", "6m": "6", "4m": "4", "2m": "2", "1.25m": "220", "70cm": "70", "33cm": "33", "23cm": "23", "13cm": "13", "9cm": "9cm", "6cm": "6cm", "3cm": "3cm" }[band] || "?");
  K.capHtml = (r, big) => `<span class="hd-mk-rpt__cap hd-mk-rpt__cap--${K.bandKey(r.band)}" style="${big ? "min-width:44px;height:36px;font-size:17px;border-radius:6px" : "height:22px;border-radius:4px"}">${M.esc(K.bandNum(r.band))}</span>`;

  /* ------------------------------------------------------------ card parts (§7.11) */
  const closeBtn = () => `<button class="hd-iconbtn hd-iconbtn--wash hd-detail__close" type="button" data-close-detail aria-label="Close">${M.ic("close") || "&#x2715;"}</button>`;
  /** card head: [mark] [heading] [close, always top right] */
  K.head = (mark, titleHtml, cls = "hd-call hd-detail__call", label) => {
    const aria = label ? ` aria-label="${M.esc(label)}"` : "";
    return mark
      ? `<div class="hd-detail__head" data-drag><div class="hd-detail__mark">${mark}</div><h2 class="${cls}" id="hd-detail-title" tabindex="-1"${aria}>${titleHtml}</h2>${closeBtn()}</div>`
      : `<div class="hd-detail__head hd-detail__head--nomark" data-drag><h2 class="${cls}" id="hd-detail-title" tabindex="-1"${aria}>${titleHtml}</h2>${closeBtn()}</div>`;
  };
  /* The QSO box chooses cells that fit (§7.8: values ellipsize, so choose cells that fit; a phone QSL card uses
     2 x 2): three columns only when every value fits a third of the card, a value too wide for its column takes a
     whole row, and a row left short is filled by the cell before the gap, so the box has no holes and keeps its order. */
  let measurer = null;
  /** the width a QSO value takes (the same class, so the same 20 px 700 tabular figures and 14 px small print) */
  function valueWidth(html) {
    if (!measurer) {
      measurer = document.createElement("span");
      measurer.className = "hd-qso__v";
      measurer.setAttribute("aria-hidden", "true");
      measurer.style.cssText = "position:absolute;left:-10000px;top:0;visibility:hidden;display:inline-block;width:auto;overflow:visible;pointer-events:none";
      document.body.appendChild(measurer);
    }
    measurer.innerHTML = html; // card values are composed and escaped by the card that passes them
    return measurer.getBoundingClientRect().width;
  }
  /** the width inside the open card (or the card that is about to open) */
  K.cardWidth = () => {
    const db = document.getElementById("hd-detail-body");
    if (db && db.clientWidth) { const cs = getComputedStyle(db); return db.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight); }
    const g = 2 * M.cssNum("--hd-gutter", 16);
    if (M.isDesk()) return M.cssNum("--hd-detail-w", 384) - 2 - g;
    if (M.isShort()) return M.cssNum("--hd-spanel-w", 360) - 2 - g;
    return window.innerWidth - g;
  };
  K.qso = (cells, three) => {
    cells = cells.filter(Boolean).map((c) => [c[0], c[1], !!c[2]]);
    let cols = three ? 3 : 2;
    const W = K.cardWidth();
    if (W > 0) {
      // a column: the card minus the 1 px outer rules and the 1 px rules between, minus 12 px of padding each side
      const room = (n) => (W - 2 - (n - 1)) / n - 24;
      const w = cells.map((c) => valueWidth(c[1]));
      if (cols === 3 && cells.some((c, i) => !c[2] && w[i] > room(3))) cols = 2;
      cells.forEach((c, i) => { if (!c[2] && w[i] > room(cols)) c[2] = true; });
    }
    const spans = [];
    let col = 0;
    for (const c of cells) {
      if (c[2]) { if (col) spans[spans.length - 1] += cols - col; spans.push(cols); col = 0; }
      else { spans.push(1); col = (col + 1) % cols; }
    }
    if (col) spans[spans.length - 1] += cols - col;
    return `<div class="hd-qso${cols === 3 ? " hd-qso--3" : ""}">${cells.map(([k, v], i) => `<div class="hd-qso__cell${spans[i] === cols ? " hd-qso__cell--wide" : ""}"${spans[i] > 1 && spans[i] < cols ? ` style="grid-column:span ${spans[i]}"` : ""}><span class="hd-qso__k">${k}</span><span class="hd-qso__v">${v}</span></div>`).join("")}</div>`;
  };
  K.ext = (href, label) => { const u = HD.safeUrl(href); return u ? `<a class="hd-btn hd-btn--sm" href="${M.esc(u)}" target="_blank" rel="noopener">${M.esc(label)}${M.ic("external", 20)}</a>` : ""; };
  K.note = (html) => `<p class="hd-detail__note">${html}</p>`;
  /** A row in "Choose a station", "In view" and the card lists: one shape for every kind of mark. */
  K.row = (attrs, lead, title, meta, trail, trail2) => `<li><button class="hd-row" type="button" ${attrs}><span class="hd-row__lead">${lead || ""}</span><span class="hd-row__title">${title}</span><span class="hd-row__meta">${meta || ""}</span><span class="hd-row__trail">${trail || ""}</span>${trail2 ? `<span class="hd-row__trail2">${trail2}</span>` : ""}</button></li>`;

  /* ------------------------------------------------------------ chrome rectangles (§9.2 step 1) */
  const CHROME_SEL = [".hd-mapbar .hd-search", ".hd-mapbar .hd-results", ".hd-maptools > *", ".hd-mapscale .leaflet-control-scale-line",
    ".hd-mapstatus .hd-status", ".hd-mapside .hd-zoom", ".hd-mapside .hd-mapbtn", ".hd-zoomhint", ".hd-map .leaflet-control-attribution",
    ".hd-statusscrim", ".hd-tabbar", ".hd-rail", ".hd-sheet", ".hd-skipmap:focus"];
  /** Every visible piece of map chrome in viewport coordinates. Tucked or faded chrome is not an obstacle. */
  K.chromeRects = () => {
    const out = [];
    const vw = window.innerWidth, vh = window.innerHeight;
    for (const sel of CHROME_SEL) for (const el of document.querySelectorAll(sel)) {
      if (el.hidden) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1 || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || cs.display === "none" || +cs.opacity === 0) continue;
      const mover = el.closest("[data-tuck]");
      if (mover && mover !== el && +getComputedStyle(mover).opacity === 0) continue;
      out.push(r);
    }
    return out;
  };

  /* ------------------------------------------------------------ the label placer (§9.2) */
  class Occ {
    constructor() { this.c = 48; this.cells = new Map(); }
    keys(b) { const c = this.c, ks = []; for (let x = Math.floor(b[0] / c); x <= Math.floor(b[2] / c); x++) for (let y = Math.floor(b[1] / c); y <= Math.floor(b[3] / c); y++) ks.push(x * 100003 + y); return ks; }
    // a label never collides with its own mark's icon box
    hits(b, self) {
      for (const k of this.keys(b)) {
        const a = this.cells.get(k);
        if (a) for (const o of a) if ((!self || o.src !== self) && b[0] < o[2] && b[2] > o[0] && b[1] < o[3] && b[3] > o[1]) return true;
      }
      return false;
    }
    add(b) { for (const k of this.keys(b)) { let a = this.cells.get(k); if (!a) this.cells.set(k, (a = [])); a.push(b); } }
    /** the area a box shares with what is placed (each box once; chrome counts four times: a label under chrome is gone) */
    cover(b, self) {
      let area = 0;
      const seen = new Set();
      for (const k of this.keys(b)) {
        const a = this.cells.get(k);
        if (a) for (const o of a) {
          if (seen.has(o) || (self && o.src === self)) continue;
          seen.add(o);
          const w = Math.min(b[2], o[2]) - Math.max(b[0], o[0]), h = Math.min(b[3], o[3]) - Math.max(b[1], o[1]);
          if (w > 0 && h > 0) area += w * h * (o.chrome ? 4 : 1);
        }
      }
      return area;
    }
  }
  K.Occ = Occ;
  /** the area two lists of boxes share */
  const shared = (as, bs) => { let s = 0; for (const a of as) for (const b of bs) { const w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]), h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]); if (w > 0 && h > 0) s += w * h; } return s; };
  let labelCanvas = null, measureCtx = null;
  const widths = new Map();
  K.clearWidths = () => widths.clear();
  /** the width of text in a canvas font ("800 15px <family>"), cached like the FCC callsigns' widths */
  K.textWidth = (font, text) => {
    let w = widths.get(font + text);
    if (w == null) {
      if (!measureCtx) measureCtx = document.createElement("canvas").getContext("2d");
      measureCtx.font = font;
      w = measureCtx.measureText(text).width;
      widths.set(font + text, w);
    }
    return w;
  };
  K.hideLabels = () => { if (labelCanvas) labelCanvas.style.visibility = "hidden"; };
  const roundRect = (ctx, x, y, w, h, r) => { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); };
  const LABEL_SEL = ".hd-lbl, .hd-pill, .hd-mk-rpt__plate, .hd-mk-rpt__tag + .hd-mk-rpt__tag, .hd-mk-rpt__more";

  /** One greedy, stable pass: chrome, marks, DOM labels by priority, FCC dots, shared-address tags, FCC callsigns. */
  K.layoutLabels = function () {
    const map = M.map;
    if (!map || !M.T || !M.visible) return;
    const z = map.getZoom(), size = map.getSize(), dpr = window.devicePixelRatio || 1;
    const mr = map.getContainer().getBoundingClientRect();
    const rel = (r) => [r.left - mr.left, r.top - mr.top, r.right - mr.left, r.bottom - mr.top];
    const occ = new Occ();
    // 1. chrome is occupied: no label is drawn where a control sits
    for (const r of K.chromeRects()) { const b = rel(r), c = [b[0] - 2, b[1] - 2, b[2] + 2, b[3] + 2]; c.chrome = true; occ.add(c); }
    // 2. every DOM mark's own box, postmarks and ZIP groups. Reads and writes are batched (one layout
    // for the plain boxes, one for the flipped ones, one for culled assemblies), never interleaved per mark.
    const all = [], dom = [];
    for (const st of M.layerList) for (const m of st.store.values()) { const el = m.getElement(); if (el) all.push({ m, el, d: m._hd }); }
    for (const it of all) it.el.classList.remove("hd-culled", "hd-flip", "hd-culled-bare");
    const measure = (lbls) => lbls.map((l) => rel(l.getBoundingClientRect())).filter((b) => b[2] > b[0] && b[3] > b[1]);
    for (const it of all) {
      const b = rel(it.el.getBoundingClientRect()); b.src = it.el; occ.add(b);
      if (!it.d.lbl) continue;
      it.lbls = Array.from(it.el.querySelectorAll(LABEL_SEL));
      if (!it.lbls.length) continue;
      it.pri = it.d.always ? -10 + (it.d.pri || 0) / 100 : it.d.pri || 0;
      it.boxes = measure(it.lbls);
      dom.push(it);
    }
    // every label may try the other side (§9.2), "You" and the selected one too
    const flippable = dom.filter((it) => !it.d.noFlip);
    for (const it of flippable) it.el.classList.add("hd-flip");
    for (const it of flippable) it.flipped = measure(it.lbls);
    for (const it of flippable) it.el.classList.remove("hd-flip");
    // a culled assembly keeps its first cap and says how many more machines it stands for ("+19"), where that fits
    const counted = dom.filter((it) => (it.more = it.el.querySelector(".hd-mk-rpt__n")));
    for (const it of counted) it.el.classList.add("hd-culled");
    for (const it of counted) it.moreBox = measure([it.more])[0];
    for (const it of counted) it.el.classList.remove("hd-culled");
    // 3. DOM labels in priority order: You, the selected station, neighbours, APRS (freshest first), repeaters, activators, post titles
    dom.sort((a, b) => a.pri - b.pri);
    const onScreen = (b) => b[0] >= 0 && b[2] <= size.x && b[1] >= 0 && b[3] <= size.y;
    // the selection ring is drawn above every label (hd-sel pane, §3.6) and belongs to its mark: no other mark's
    // label goes under it, so a plate hanging below a selected ZIP group flips or culls instead (§9.2 step 2). The
    // canvas labels after this keep off it on their own (the selected licence's callsign and count tag).
    const ring = K.selRing();
    const underRing = (b, el) => !!ring && el !== ring.own
      && Math.hypot(M.clamp(ring.cx, b[0], b[2]) - ring.cx, M.clamp(ring.cy, b[1], b[3]) - ring.cy) < ring.R;
    const fits = (boxes, el) => boxes.every(onScreen) && boxes.every((b) => !occ.hits(b, el) && !underRing(b, el));
    // each label tries the right, then the left (preferLeft: a moving APRS station heading east, whose course
    // triangle would sit under its own pill, tries the left first)
    const sidesOf = (it) => {
      const sides = [[it.boxes, false]];
      if (it.flipped) { if (it.d.preferLeft) sides.unshift([it.flipped, true]); else sides.push([it.flipped, true]); }
      return sides;
    };
    const flip = [], cull = [], bare = [];
    const place = (it, side) => { side[0].forEach((b) => occ.add(b)); if (side[1]) flip.push(it.el); };
    // You and the selected station are never hidden, and they keep clear of each other: they are placed together, You
    // first. Each takes the first side that fits; when the selection fits on neither side of the "You" that took its
    // place, "You" moves to its other side; when nothing fits, the pair of sides that covers the least: chrome and the
    // screen edge count four times, the two labels over each other eight times (§9.2, §3.2)
    const offScreen = (b) => (b[2] - b[0]) * (b[3] - b[1]) - Math.max(0, Math.min(b[2], size.x) - Math.max(b[0], 0)) * Math.max(0, Math.min(b[3], size.y) - Math.max(b[1], 0));
    const always = dom.filter((it) => it.d.always);
    if (always.length) {
      const area = (b) => (b[2] - b[0]) * (b[3] - b[1]);
      const opts = always.map((it) => sidesOf(it).map((side) => ({ side, cost: side[0].reduce((s, b) => s + occ.cover(b, it.el) + 4 * offScreen(b) + (underRing(b, it.el) ? 4 * area(b) : 0), 0) })));
      let best = null;
      const walk = (i, chosen, cost) => {
        if (best && cost >= best.cost) return;
        if (i === always.length) { best = { cost, chosen: chosen.slice() }; return; }
        for (const o of opts[i]) {
          let c = cost + o.cost;
          for (const p of chosen) c += 8 * shared(o.side[0], p.side[0]);
          chosen.push(o); walk(i + 1, chosen, c); chosen.pop();
        }
      };
      walk(0, [], 0);
      always.forEach((it, i) => place(it, best.chosen[i].side));
    }
    for (const it of dom) {
      const { el, d } = it;
      if (d.always) continue;
      if (z < (d.minZ || 0)) { cull.push(el); continue; }
      const ok = sidesOf(it).find(([boxes]) => fits(boxes, el));
      if (ok) { place(it, ok); continue; }
      cull.push(el); // a culled repeater assembly keeps its first cap, and its "+19" where that fits
      if (it.moreBox && fits([it.moreBox], el)) occ.add(it.moreBox);
      else if (it.more) bare.push(el);
    }
    for (const el of flip) el.classList.add("hd-flip");
    for (const el of cull) el.classList.add("hd-culled");
    for (const el of bare) el.classList.add("hd-culled-bare");
    // canvas: shared-address tags, then FCC callsigns
    if (!labelCanvas) labelCanvas = L.DomUtil.create("canvas", "hd-labelcanvas", map.getPane("hd-labels"));
    L.DomUtil.setPosition(labelCanvas, map.containerPointToLayerPoint([0, 0]));
    const W = Math.round(size.x * dpr), H = Math.round(size.y * dpr);
    if (labelCanvas.width !== W || labelCanvas.height !== H) { labelCanvas.width = W; labelCanvas.height = H; labelCanvas.style.width = size.x + "px"; labelCanvas.style.height = size.y + "px"; }
    const ctx = labelCanvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.x, size.y);
    labelCanvas.style.visibility = "";
    const fcc = M.L("fcc");
    const pts = fcc && M.isOn("fcc") && z >= M.MINZ.fccDots && fcc.labelPoints ? fcc.labelPoints : [];
    if (!pts.length) return;
    const T = M.T, inView = [];
    for (const it of pts) {
      const p = map.latLngToContainerPoint(it.latlng);
      if (p.x > -40 && p.y > -20 && p.x < size.x + 40 && p.y < size.y + 20) inView.push({ it, p, r: K.markR(it, z) });
    }
    // 4. FCC dots' own boxes (each knows its dot: a count tag sits up and right of its own dot, touching it, and
    // must only give way to other marks and labels)
    for (const { it, p, r } of inView) { const b = [p.x - r - 1, p.y - r - 1, p.x + r + 1, p.y + r + 1]; b.src = it; occ.add(b); }
    // the selected licence or shared address keeps its callsign and its count tag, both set just outside its selection
    // ring (§3.6) (matched by callsign: /api/hams/{call} answers full precision, /api/hams/map rounds to 6 decimals)
    const sel = M.ui ? M.ui.selection() : null;
    const selFcc = sel && sel.layer === "fcc" && (sel.kind === "fcc" || sel.kind === "addr") ? sel : null;
    const selCalls = selFcc ? new Set(selFcc.kind === "fcc" ? [selFcc.h.callsign] : selFcc.hams.map((h) => h.callsign)) : null;
    const isSel = (it) => !!selFcc && (selCalls.has(it.call) || (Math.abs(it.latlng[0] - selFcc.lat) < 2e-6 && Math.abs(it.latlng[1] - selFcc.lon) < 2e-6));
    // the ring's outer band ends d/2 + 7 px out: a tag whose inner corner sits 1 px beyond it, on the diagonal
    const ringOut = selFcc ? Math.ceil(((((M.ui.ringFor(selFcc) || {}).d) || 30) / 2 + 8) / Math.SQRT2) : 0;
    ctx.textBaseline = "middle"; ctx.lineJoin = "round";
    // shared-address tags: a quiet 16 px paper tag, from z15, from z13 when five or more share the point; up and right
    // of the dot, touching it, or for the selected point up and right of its ring, else up and left
    ctx.font = `800 13px ${T.font}`; ctx.textAlign = "center";
    for (const { it, p, r } of inView.slice().sort((a, b) => (isSel(b.it) - isSel(a.it)) || (b.it.n - a.it.n))) {
      if (it.n < 2 || !(z >= M.MINZ.stack || (z >= M.MINZ.stackBig && it.n >= 5))) continue;
      const t = String(it.n), w = Math.max(16, ctx.measureText(t).width + 8);
      const spots = isSel(it) ? [[p.x + ringOut + w / 2, p.y - ringOut - 8], [p.x - ringOut - w / 2, p.y - ringOut - 8]] : [[p.x + r + w / 2 + 1, p.y - r - 6]];
      const spot = spots.find(([cx, cy]) => { const b = [cx - w / 2 - 1, cy - 9, cx + w / 2 + 1, cy + 9]; return !(b[0] < 0 || b[2] > size.x || b[1] < 0 || b[3] > size.y || occ.hits(b, it)); });
      if (!spot) continue;
      const [cx, cy] = spot;
      roundRect(ctx, cx - w / 2, cy - 8, w, 16, 8); ctx.fillStyle = T.stackFill; ctx.fill(); ctx.lineWidth = 1.5; ctx.strokeStyle = T.stackLine; ctx.stroke();
      ctx.fillStyle = T.stackInk; ctx.fillText(t, cx, cy + 0.5);
      occ.add([cx - w / 2 - 1, cy - 9, cx + w / 2 + 1, cy + 9]);
    }
    if (z < M.MINZ.fccLabels) return;
    // 5. FCC callsigns: newly licensed first, then a stable hash, so the same labels win while you pan
    const cap = M.isDesk() ? 400 : 160;
    let drawn = 0;
    inView.sort((a, b) => (isSel(b.it) - isSel(a.it)) || (b.it.star - a.it.star) || (a.it.hash - b.it.hash));
    ctx.textAlign = "left"; ctx.lineWidth = 3.5; ctx.strokeStyle = T.halo;
    for (const { it, p, r } of inView) {
      if (drawn >= cap) break;
      const font = `${it.mem ? 800 : it.exp ? 500 : 700} 13px ${T.font}`;
      ctx.font = font;
      const key = font + it.call;
      let w = widths.get(key);
      if (w == null) { w = ctx.measureText(it.call).width; widths.set(key, w); }
      const sel = isSel(it), off = sel ? 27 : r + 5;
      let x = p.x + off, box = [x - 2, p.y - 9, x + w + 2, p.y + 9];
      const bad = (b) => b[0] < 0 || b[2] > size.x || b[1] < 0 || b[3] > size.y || occ.hits(b);
      if (bad(box)) {
        if (!sel) continue;
        // the selected licence keeps its callsign (§9.2: the selected station always): on the left when the right is taken
        const lx = p.x - off - w, lbox = [lx - 2, p.y - 9, lx + w + 2, p.y + 9];
        if (!bad(lbox) || box[2] > size.x) { x = lx; box = lbox; }
      }
      occ.add(box);
      ctx.strokeText(it.call, x, p.y);
      ctx.fillStyle = it.mem ? T.member : it.exp ? T.ink2 : T.ink;
      ctx.fillText(it.call, x, p.y);
      drawn++;
    }
  };

  /* ------------------------------------------------------------ riding postmark rims below z10 (§3.2) */
  const YOU_R = 13; // your dot: 18 px, a 3 px ring and a 1 px hairline
  K.makeRim = function (ctx) {
    const y = M.youPlace(), z = ctx.zoom;
    const yp = y ? ctx.toPoint(y.lat, y.lon) : null;
    // the neighbours riding on your rim (layer-people.js), so a station that rides there takes another slot
    const taken = [];
    /* From z10 your dot sits above every station, so a selected station your dot would cover rides on your rim instead,
       clear of the dot (and so is the label row beside it), in the first slot of up-left, down-left, down-right,
       up-right that no riding neighbour takes (§3.2). d: a mark descriptor. Returns its new icon anchor, or undefined
       to stay put. */
    const offYou = (d) => {
      if (!yp || z < M.MINZ.fccDots) return undefined;
      const p = ctx.toPoint(d.latlng[0], d.latlng[1]), w = d.size[0], h = d.size[1], a = d.anchor || [w / 2, h / 2];
      const l = p.x - a[0], t = p.y - a[1];
      if (Math.hypot(M.clamp(yp.x, l, l + w) - yp.x, M.clamp(yp.y, t, t + h) - yp.y) >= YOU_R) return undefined;
      const k = Math.max(w, h) / 2 + YOU_R + 1; // its label runs level with it: the whole row clears your dot
      const free = (cx, cy) => !taken.some((o) => cx - w / 2 < o[2] && cx + w / 2 > o[0] && cy - h / 2 < o[3] && cy + h / 2 > o[1]);
      const c = [[-1, -1], [-1, 1], [1, 1], [1, -1]].map(([sx, sy]) => [yp.x + sx * k, yp.y + sy * k]).find(([cx, cy]) => free(cx, cy)) || [yp.x - k, yp.y - k];
      return [Math.round(w / 2 - (c[0] - p.x)), Math.round(h / 2 - (c[1] - p.y))];
    };
    if (z >= M.MINZ.fccDots) return { anchor: () => undefined, youLbl: 10, youLblL: 10, yp, taken, offYou };
    const circles = ctx.postmarks || [];
    const boxes = [];
    let youLbl = 10, youLblL = 10;
    if (yp) {
      // below z10 you sit under your metro's postmark; the word "You" starts 6 px outside its right edge, or flipped,
      // ends 6 px outside its left edge (at least 10 px from your dot either way)
      const home = circles.find((k) => Math.hypot(yp.x - k.x, yp.y - k.y) < k.r + 6);
      const lblLeft = Math.max(yp.x + 19, home ? home.x + home.r + 6 : 0);
      youLbl = lblLeft - (yp.x + 9);
      youLblL = (yp.x - 9) - Math.min(yp.x - 19, home ? home.x - home.r - 6 : Infinity);
      boxes.push([yp.x - 12, yp.y - 12, yp.x + 12, yp.y + 12]);
      boxes.push([lblLeft - 2, yp.y - 10, lblLeft + 38, yp.y + 10]);
    }
    const hit = (b) => boxes.filter((o) => b[0] < o[2] && b[2] > o[0] && b[1] < o[3] && b[3] > o[1]);
    return {
      yp, youLbl, youLblL, taken, offYou,
      /** an icon anchor that draws a w x h mark on a postmark's rim (or clear of you), or undefined to stay put */
      anchor(lat, lon, w, h) {
        const p = ctx.toPoint(lat, lon);
        let on = circles.find((k) => Math.hypot(p.x - k.x, p.y - k.y) < k.r + 6);
        if (!on) {
          const b = [p.x - w / 2, p.y - h / 2, p.x + w / 2, p.y + h / 2], hs = hit(b);
          if (!hs.length) { boxes.push(b); return undefined; }
          const cy = Math.min(...hs.map((o) => o[1])) - h / 2 - 3; // step up until it clears you and "You"
          // stepping up must not bury a postmark's count: a mark that lands on one rides its rim instead
          on = circles.find((k) => Math.hypot(p.x - k.x, cy - k.y) < k.r + Math.max(w, h) / 2);
          if (!on) {
            boxes.push([p.x - w / 2, cy - h / 2, p.x + w / 2, cy + h / 2]);
            return [w / 2, h / 2 - (cy - p.y)];
          }
        }
        const d = on.r * 0.78, slots = [[d, -d], [-d, -d], [d, d], [-d, d]]; // up-right, up-left, down-right, down-left
        let pick = slots[0];
        for (const sl of slots) { const cx = on.x + sl[0], cy = on.y + sl[1]; if (!hit([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]).length) { pick = sl; break; } }
        const cx = on.x + pick[0], cy = on.y + pick[1];
        boxes.push([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]);
        return [w / 2 - (cx - p.x), h / 2 - (cy - p.y)];
      },
    };
  };

  /* ------------------------------------------------------------ the selection ring (§3.6) */
  let selMarker = null;
  /** The selection ring where it is drawn: {latlng, d, el, own, p, cx, cy, R} in container pixels, R reaching the
   *  outside of its 7 px of bands (2 ring colour, 3 ink, 2 ring colour), own the selected mark's element, or null.
   *  A mark riding a rim is drawn away from its point: the ring goes where the mark (el) is. */
  K.selRing = function () {
    const it = M.ui ? M.ui.selection() : null;
    const ring = it && M.ui.ringFor(it); // {latlng, d, el?}
    if (!ring) return null;
    const p = M.map.latLngToContainerPoint(ring.latlng);
    let cx = p.x, cy = p.y;
    if (ring.el && ring.el.isConnected) {
      const r = ring.el.getBoundingClientRect(), mr = M.map.getContainer().getBoundingClientRect();
      if (r.width) { cx = r.left + r.width / 2 - mr.left; cy = r.top + r.height / 2 - mr.top; }
    }
    return Object.assign({}, ring, { p, cx, cy, R: ring.d / 2 + 7, own: ring.el || M.ui.markEl(it) });
  };
  K.renderSel = function () {
    const ring = K.selRing();
    if (!ring) { if (selMarker) { M.map.removeLayer(selMarker); selMarker = null; } return; }
    const { latlng, d, p, cx, cy } = ring;
    const anchor = [d / 2 - (cx - p.x), d / 2 - (cy - p.y)];
    const icon = L.divIcon({ className: "hd-selring", iconSize: [d, d], iconAnchor: anchor, html: '<div class="hd-mk-sel"></div>' });
    if (!selMarker) selMarker = L.marker(latlng, { pane: "hd-sel", interactive: false, keyboard: false, icon }).addTo(M.map);
    else { selMarker.setLatLng(latlng); selMarker.setIcon(icon); }
  };
})();
