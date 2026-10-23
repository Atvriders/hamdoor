/* hamdoor map 1/9: the engine. DESIGN_SYSTEM.md §3, §5, §6.5, §9, §14.4, §15.
   - HD.map.show / hide / focus / refresh / neighbours / addLayer (contract §3, §5c)
   - the "map" route and its URL state (#/map?lat&lon&z&sel)
   - basemaps: OpenFreeMap vector styles through MapLibre GL 5.24.0 and
     maplibre-gl-leaflet 0.1.4, loaded lazily with SRI; Esri, USGS and the OSM
     raster fallback (§9.5)
   - preferences (localStorage "hamdoor.map.v1" through HD.store)
   - the layer registry (§15.3) and its loader: 250 ms debounce after moveend,
     superseded requests aborted, stale answers dropped, a session cache per
     layer keyed by zoom, the cell-snapped bbox and the filters (50 entries),
     polling only while the map is visible and the page is not hidden
   - the render loop: every layer renders, marks are diffed by key
     (map-marks.js), labels are placed once per moveend
   Marks and labels live in map-marks.js, the chrome in map-ui.js, each layer
   in its own layer-*.js file. */
(function () {
  "use strict";
  const HD = window.HD;
  const L = window.L;
  const api = (HD.map = HD.map || {});
  const M = (api._ = api._ || {});

  /* ------------------------------------------------------------ constants */
  M.NB = " ";      // a number keeps its unit with a no-break space (§4)
  M.THIN = " ";    // between a frequency and its offset sign
  M.MINUS = "−";
  // zoom thresholds (§9.1, §9.3)
  M.MINZ = { fccDots: 10, fccLabels: 14, expired: 12, stack: 15, stackBig: 13, zipGroups: 13,
    rpt: 10, rptPlates: 12, rptTone: 13, aprs: 9, aprsPills: 11, aprsAmb: 11, tracks: 10, trackDots: 13,
    memberLabels: 10, posts: 13, postLabels: 14, spotPills: 11, spotCluster: 8 };
  // Leaflet panes (§5)
  M.PANES = [["hd-range", 410], ["hd-tracks", 420], ["hd-fcc", 430], ["hd-labels", 440], ["hd-clusters", 600],
    ["hd-zip", 603], ["hd-rpt", 606], ["hd-spots", 609], ["hd-aprs", 612], ["hd-posts", 618], ["hd-ops", 625],
    ["hd-sel", 630], ["hd-you", 635], ["hd-ride", 640]];
  M.CLASS_NAME = { E: "Amateur Extra", A: "Advanced", G: "General", T: "Technician", P: "Technician Plus", N: "Novice", "": "Club licence" };
  M.CLASS_SHORT = { E: "Extra", A: "Advanced", G: "General", T: "Technician", P: "Technician Plus", N: "Novice", "": "Club" };
  // colour group = the API's buckets: Technician Plus (P) rides with Novice in N, so it is Other (§3.1)
  M.grp = (cls) => (cls === "T" ? "T" : cls === "G" ? "G" : cls === "E" ? "E" : "O");
  M.letterOf = (cls) => ({ T: "T", P: "T", G: "G", E: "E", A: "A", N: "N" }[cls] || "C");
  M.className = (cls) => M.CLASS_NAME[cls] || M.CLASS_NAME[""];
  M.classShort = (cls) => M.CLASS_SHORT[cls] || M.CLASS_SHORT[""];
  M.CLASS_VAR = { T: "var(--fcc-tech)", G: "var(--fcc-general)", E: "var(--fcc-extra)", O: "var(--fcc-other)" };

  /* ------------------------------------------------------------ small helpers */
  M.esc = (s) => HD.esc(s);
  M.$ = (sel, el) => (el || document).querySelector(sel);
  M.$$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));
  M.clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  M.finite = (v) => typeof v === "number" && isFinite(v);
  M.num = (v) => { const n = typeof v === "number" ? v : parseFloat(v); return isFinite(n) ? n : null; };
  M.hash32 = (s) => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; };
  M.ic = (name, size) => { try { return (HD.icon && HD.icon(name, size ? { size } : undefined)) || ""; } catch (e) { return ""; } };
  M.toast = (msg, opts) => { if (HD.toast) HD.toast(msg, opts); else console.info("[hd] " + msg); };
  M.announce = (text) => {
    if (HD.announce) { HD.announce(text); return; }
    // the shell owns the one announcer; until it exists the map keeps a private one
    let el = document.getElementById("hd-map-announce");
    if (!el) { el = document.createElement("div"); el.id = "hd-map-announce"; el.className = "hd-sr"; el.setAttribute("role", "status"); document.body.appendChild(el); }
    el.textContent = ""; setTimeout(() => { el.textContent = text; }, 60);
  };
  M.me = () => (HD.session && HD.session.user) || null;
  M.features = () => (HD.config && HD.config.features) || {};

  const mq = (q) => window.matchMedia(q);
  const MQ = { desk: mq("(min-width: 1024px)"), short: mq("(max-width: 1023.98px) and (max-height: 499.98px)"),
    narrow: mq("(min-width: 1024px) and (max-width: 1279.98px)"), reduced: mq("(prefers-reduced-motion: reduce)"),
    coarse: mq("(pointer: coarse)"), dark: mq("(prefers-color-scheme: dark)") };
  M.MQ = MQ;
  M.isDesk = () => MQ.desk.matches;
  M.isShort = () => MQ.short.matches;
  M.isPhone = () => !MQ.desk.matches && !MQ.short.matches;
  M.isNarrowDesk = () => MQ.narrow.matches;
  M.reduced = () => MQ.reduced.matches;
  M.coarse = () => MQ.coarse.matches;
  M.onMQ = (m, fn) => { if (m.addEventListener) m.addEventListener("change", fn); else if (m.addListener) m.addListener(fn); };
  const cssNum = (name, fallback) => { const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)); return isFinite(v) ? v : fallback; };
  M.cssNum = cssNum;

  /* ------------------------------------------------------------ geography (app/geo.py, app/integrations/geocode.py) */
  const R_MI = 3958.7613, RAD = Math.PI / 180;
  M.distMi = (a, b) => {
    const dLat = (b.lat - a.lat) * RAD, dLon = (b.lon - a.lon) * RAD;
    const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * RAD) * Math.cos(b.lat * RAD) * Math.sin(dLon / 2) ** 2;
    return 2 * R_MI * Math.asin(Math.min(1, Math.sqrt(x)));
  };
  M.bearing = (a, b) => {
    const y = Math.sin((b.lon - a.lon) * RAD) * Math.cos(b.lat * RAD);
    const x = Math.cos(a.lat * RAD) * Math.sin(b.lat * RAD) - Math.sin(a.lat * RAD) * Math.cos(b.lat * RAD) * Math.cos((b.lon - a.lon) * RAD);
    return (Math.atan2(y, x) / RAD + 360) % 360;
  };
  const DIR8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const DIR8W = { N: "north", NE: "north-east", E: "east", SE: "south-east", S: "south", SW: "south-west", W: "west", NW: "north-west" };
  M.dir8 = (d) => DIR8[Math.round((((d % 360) + 360) % 360) / 45) % 8];
  M.dir8w = (d) => DIR8W[M.dir8(d)];
  /** Maidenhead locator of a point, 4, 6 or 8 characters (latlon_to_grid). */
  M.grid = (lat, lon, length = 6) => {
    const per = { 4: 1, 6: 24, 8: 240 }[length] || 24;
    const x = Math.min(Math.floor(((Math.min(Math.max(lon, -180), 180) + 180) / 2) * per), 180 * per - 1);
    const y = Math.min(Math.floor((Math.min(Math.max(lat, -90), 90) + 90) * per), 180 * per - 1);
    const sx = Math.floor(x / per), sy = Math.floor(y / per);
    let out = String.fromCharCode(65 + Math.floor(sx / 10)) + String.fromCharCode(65 + Math.floor(sy / 10)) + (sx % 10) + (sy % 10);
    if (length >= 6) { const unit = per / 24; out += String.fromCharCode(97 + (Math.floor(x / unit) % 24)) + String.fromCharCode(97 + (Math.floor(y / unit) % 24)); }
    if (length === 8) out += `${x % 10}${y % 10}`;
    return out;
  };
  /** Centre of a 4, 6 or 8 character locator, or null (grid_to_latlon). */
  M.gridCentre = (grid) => {
    const g = String(grid || "").trim().toUpperCase();
    if (![4, 6, 8].includes(g.length) || !/^[A-R]{2}\d\d/.test(g)) return null;
    const A = (c) => c.charCodeAt(0) - 65;
    let lon = A(g[0]) * 20 - 180 + Number(g[2]) * 2, lat = A(g[1]) * 10 - 90 + Number(g[3]);
    if (g.length >= 6) {
      if (!/^[A-X]{2}$/.test(g.slice(4, 6))) return null;
      lon += A(g[4]) * (5 / 60); lat += A(g[5]) * (2.5 / 60);
      if (g.length === 8) {
        if (!/^\d\d$/.test(g.slice(6))) return null;
        lon += Number(g[6]) * (5 / 600); lat += Number(g[7]) * (2.5 / 600);
        return { lat: lat + 2.5 / 1200, lon: lon + 5 / 1200 };
      }
      return { lat: lat + 2.5 / 120, lon: lon + 5 / 120 };
    }
    return { lat: lat + 0.5, lon: lon + 1 };
  };
  M.gridFmt = (g) => { g = String(g || "").trim(); return g.length < 4 ? g.toUpperCase() : g.slice(0, 4).toUpperCase() + g.slice(4, 6).toLowerCase() + g.slice(6); };
  /** The locator a grid-precision member is shown at (member_subsquare). */
  M.memberSubsquare = (grid, lat, lon) => {
    const g = String(grid || "").trim();
    if (g.length >= 6 && M.gridCentre(g.slice(0, 6))) return g.slice(0, 4).toUpperCase() + g.slice(4, 6).toLowerCase();
    const have = M.finite(lat) && M.finite(lon);
    if (g.length >= 4 && M.gridCentre(g.slice(0, 4))) {
      const sq = g.slice(0, 4).toUpperCase();
      return have && M.grid(lat, lon, 4) === sq ? M.grid(lat, lon, 6) : sq;
    }
    return have ? M.grid(lat, lon, 6) : null;
  };
  /** A point that is the centre of the 4- or 6-character locator in `grid` is approximate (§3.2). */
  M.isGridCentre = (lat, lon, grid) => {
    const g = String(grid || "").trim();
    for (const n of [6, 4]) {
      if (g.length < n) continue;
      const c = M.gridCentre(g.slice(0, n));
      if (c && Math.abs(c.lat - lat) < 1e-5 && Math.abs(c.lon - lon) < 1e-5) return true;
    }
    return false;
  };

  /* ------------------------------------------------------------ formatting (§4) */
  const NB = M.NB;
  M.fmtN = (n) => Number(n).toLocaleString("en-US");
  M.compact = (n) => {
    n = Math.round(Number(n) || 0);
    if (n < 1000) return String(n);
    if (n < 9950) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    if (n < 999500) return Math.round(n / 1000) + "k";
    return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  };
  /** "0.7 mi", "19 mi", or for an approximate place "about 3 mi" (whole miles, at least 1). */
  M.mi = (mi, approx) => {
    if (!M.finite(mi)) return "";
    if (approx) return `about ${M.fmtN(Math.max(1, Math.round(mi)))}${NB}mi`;
    return `${mi < 10 ? mi.toFixed(1) : M.fmtN(Math.round(mi))}${NB}mi`;
  };
  /** Repeater output: 3 decimals, 4 when the fourth digit is not 0 (146.940, 927.6125). */
  M.freq = (f) => { if (!M.finite(Number(f))) return ""; let s = Number(f).toFixed(4); if (s.endsWith("0")) s = s.slice(0, -1); return s; };
  M.offSign = (off) => (!M.finite(off) || Math.abs(off) < 1e-6 ? "" : off > 0 ? "+" : M.MINUS);
  M.khzToMhz = (khz) => { const k = M.num(khz); return k == null || k <= 0 ? "" : (k / 1000).toFixed(3); };
  M.dB = (n) => `${n < 0 ? M.MINUS + Math.abs(n) : n}${NB}dB`;
  /** local 12-hour clock for people: "3:52 pm" (with seconds for APRS fixes) */
  M.clock = (d, secs) => {
    if (!(d instanceof Date)) d = HD.fmt.date(d);
    if (!d) return "";
    const h = d.getHours(), m = String(d.getMinutes()).padStart(2, "0"), s = secs ? ":" + String(d.getSeconds()).padStart(2, "0") : "";
    return `${h % 12 || 12}:${m}${s}${NB}${h < 12 ? "am" : "pm"}`;
  };
  /** radio time: "18:23 UTC" from an ISO string (naive = UTC) or unix seconds */
  M.utc = (t) => { const d = HD.fmt.date(t); return d ? d.toISOString().slice(11, 16) + NB + "UTC" : ""; };
  M.ago = (t) => (HD.fmt && HD.fmt.ago ? HD.fmt.ago(t) : "");
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  M.parseUS = (d) => { const m = /^(\d\d)\/(\d\d)\/(\d{4})$/.exec(d || ""); return m ? new Date(+m[3], +m[1] - 1, +m[2]) : null; };
  M.monYear = (d) => (d ? `${MON[d.getMonth()]}${NB}${d.getFullYear()}` : "Not listed");
  M.longDate = (d) => (d ? `${MON[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}` : "");
  M.daysAgo = (d) => Math.max(0, Math.round((Date.now() - d.getTime()) / 86400000));
  /** "KG0M-7" with the SSID set in 500 (§4) */
  M.callHtml = (c) => { const m = /^([A-Z0-9]+)(-[0-9A-Z]{1,2})$/i.exec(String(c || "")); return m ? `${M.esc(m[1])}<span class="hd-call__ssid">${M.esc(m[2])}</span>` : M.esc(c); };
  /** Spoken callsign for headings: "K G zero M dash 7" (§4) */
  M.spell = (c) => String(c || "").toUpperCase().split("").map((ch) => (ch === "0" ? "zero" : ch === "-" ? "dash" : ch === "/" ? "stroke" : ch)).join(" ");
  M.firstName = (name) => String(name || "").trim().split(/\s+/)[0] || "";
  M.copy = (text, msg) => {
    const done = () => M.toast(msg || `Copied ${text}`);
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(done, done); return; }
    } catch (e) { /* no clipboard: still say it */ }
    done();
  };

  /** Basemap attributions arrive as HTML from /api/config: keep only text and http(s) links. */
  M.safeAttribution = (html) => {
    const out = [];
    let doc;
    try { doc = new DOMParser().parseFromString(`<div>${html || ""}</div>`, "text/html"); } catch (e) { return M.esc(html); }
    const walk = (node) => {
      for (const n of node.childNodes) {
        if (n.nodeType === 3) out.push(M.esc(n.nodeValue));
        else if (n.nodeType === 1 && n.tagName === "A") {
          const href = HD.safeUrl(n.getAttribute("href"));
          if (href) out.push(`<a href="${M.esc(href)}" target="_blank" rel="noopener">${M.esc(n.textContent)}</a>`);
          else out.push(M.esc(n.textContent));
        } else if (n.nodeType === 1) walk(n);
      }
    };
    walk(doc.body.firstChild || doc.body);
    return out.join("");
  };

  /* ------------------------------------------------------------ you (§3.2) */
  /** Your mark: your stored point in address precision; in grid precision the
   *  subsquare neighbours see, computed with the backend's rule. */
  M.youPlace = () => {
    const me = M.me();
    if (!me || !M.finite(me.lat) || !M.finite(me.lon)) return null;
    if (me.map_precision === "grid") {
      const g = M.memberSubsquare(me.grid, me.lat, me.lon), c = g && M.gridCentre(g);
      return c ? { lat: c.lat, lon: c.lon, approx: true, grid: g } : { lat: me.lat, lon: me.lon, approx: true, grid: M.grid(me.lat, me.lon, 6) };
    }
    return { lat: me.lat, lon: me.lon, approx: false, grid: me.grid ? M.gridFmt(me.grid) : M.grid(me.lat, me.lon, 6) };
  };
  /** Distances are measured from your stored location (the Feed and Nearby do the same). */
  M.home = () => { const me = M.me(); return me && M.finite(me.lat) && M.finite(me.lon) ? { lat: me.lat, lon: me.lon } : null; };
  M.distFromHome = (lat, lon) => { const h = M.home(); return h && M.finite(lat) && M.finite(lon) ? M.distMi(h, { lat, lon }) : null; };
  /** "0.7 mi NE" from you, or "" without a location */
  M.distDir = (lat, lon, approx) => {
    const h = M.home(); if (!h || !M.finite(lat) || !M.finite(lon)) return "";
    const d = M.distMi(h, { lat, lon });
    return `${M.mi(d, approx)}${d >= 0.05 ? ` <small>${M.dir8(M.bearing(h, { lat, lon }))}</small>` : ""}`;
  };

  /* ------------------------------------------------------------ preferences (§7.10) */
  const PREF_KEY = "hamdoor.map.v1";
  M.prefs = null;
  const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
  function defaultPrefs() {
    const p = { basemap: null, layersOpen: true };
    for (const st of M.layerList) p[st.id] = Object.assign({ on: true }, JSON.parse(JSON.stringify(st.def.defaults || {})));
    return p;
  }
  function mergePrefs(d, s) {
    const o = {};
    for (const k of Object.keys(d)) {
      if (isObj(d[k])) o[k] = mergePrefs(d[k], isObj(s) ? s[k] : null);
      else o[k] = isObj(s) && k in s && typeof s[k] === typeof d[k] ? s[k] : d[k];
    }
    return o;
  }
  function loadPrefs() {
    const saved = HD.store.get(PREF_KEY, null);
    M.prefs = mergePrefs(defaultPrefs(), saved);
    M.prefs.basemap = isObj(saved) && typeof saved.basemap === "string" ? saved.basemap : null;
  }
  M.savePrefs = () => HD.store.set(PREF_KEY, M.prefs);
  M.pref = (path) => path.split("/").reduce((o, k) => (o == null ? undefined : o[k]), M.prefs);
  M.setPref = (path, v) => { const ks = path.split("/"), last = ks.pop(); const o = ks.reduce((a, k) => a[k], M.prefs); o[last] = v; M.savePrefs(); };
  M.resetPrefs = () => { M.prefs = defaultPrefs(); M.savePrefs(); };

  /* ------------------------------------------------------------ map tone tokens (§3) */
  M.T = null;
  M.tone = "light";
  M.readTokens = () => {
    const cs = getComputedStyle(document.body), g = (n) => cs.getPropertyValue(n).trim();
    M.T = { ring: g("--ov-ring"), ink: g("--ov-ink"), ink2: g("--ov-ink-2"), halo: g("--ov-halo"), hair: g("--ov-hairline"),
      T: g("--fcc-tech"), G: g("--fcc-general"), E: g("--fcc-extra"), O: g("--fcc-other"), exS: g("--fcc-expired-stroke"), exF: g("--fcc-expired-fill"),
      member: g("--member"), stackFill: g("--stack-fill"), stackInk: g("--stack-ink"), stackLine: g("--stack-line"), range: g("--range"),
      track: g("--track"), casing: g("--track-casing"), font: g("--hd-font") || "sans-serif" };
    return M.T;
  };

  /* ------------------------------------------------------------ layer registry (§15.3) */
  M.layers = new Map();
  M.layerList = [];
  /**
   * def: { id, order, title, minZoom, hint, defaults, feature(features), poll, cacheMs, bbox,
   *   filterKey(prefs, zoom), load(view, signal), render(data, ctx) -> Map(key -> mark descriptor),
   *   canvas(data, ctx), counts(data, ctx) -> {meta, chips}, section(prefs) -> html, card(item, ctx),
   *   row(item), inView(data, ctx), search(q, signal), resolve(kind, id), selId(item), latlng(item),
   *   markKey(item), attribution }
   */
  api.addLayer = function (def) {
    const st = { id: def.id, def, data: null, view: null, store: new Map(), cache: new Map(), ctrl: null, seq: 0,
      loading: false, fails: 0, lastOkAt: 0, error: null, pollTimer: 0, stale: false };
    st.enabled = () => !def.feature || !!def.feature(M.features());
    M.layers.set(def.id, st);
    M.layerList.push(st);
    M.layerList.sort((a, b) => (a.def.order || 0) - (b.def.order || 0));
    return st;
  };
  M.L = (id) => M.layers.get(id);
  M.isOn = (id) => { const st = M.layers.get(id); return !!st && st.enabled() && !!(M.prefs && M.prefs[id] && M.prefs[id].on); };
  M.data = (id) => { const st = M.layers.get(id); return st ? st.data : null; };
  M.layerOfSel = { fcc: "fcc", addr: "fcc", zip: "fcc", member: "members", you: "members", rpt: "rpt", aprs: "aprs", spot: "spots", post: "posts" };

  /* ------------------------------------------------------------ loading (§9.9, §14.4) */
  const CELL = { 4: 5, 5: 3, 6: 2, 7: 1, 8: 0.5, 9: 0.25, 10: 0.05, 11: 0.025, 12: 0.0125, 13: 0.00625 };
  M.cellDeg = (z) => (z < 4 ? 6 : CELL[z] || CELL[13]);
  /** The view a layer asks for: Leaflet's bounds snapped outward to the API's cell grid (stable cache keys), or to
   *  a grid `cells` times coarser (the licence layer merges dense answers 2 x 2: a request cut mid-block would show
   *  half a block at the screen edge, with the wrong count, jumping as you pan) */
  M.viewFor = (zoom, cells = 1) => {
    const b = M.map.getBounds(), c = M.cellDeg(zoom) * cells;
    let w = b.getWest(), e = b.getEast();
    if (e - w >= 360) { w = -180; e = 180; }
    const s = M.clamp(Math.floor(b.getSouth() / c) * c, -90, 90), n = M.clamp((Math.floor(b.getNorth() / c) + 1) * c, -90, 90);
    w = M.clamp(Math.floor(w / c) * c, -180, 180); e = M.clamp((Math.floor(e / c) + 1) * c, -180, 180);
    const r = (v) => Number(v.toFixed(6));
    return { s: r(s), n: r(n), w: r(w), e: r(e), zoom };
  };
  M.bboxQuery = (v) => `min_lat=${v.s}&max_lat=${v.n}&min_lon=${v.w}&max_lon=${v.e}&zoom=${v.zoom}`;
  /** The bounds actually on screen (counts "in view" are computed against these). */
  M.screenBounds = () => M.map.getBounds();

  function viewKey(st) {
    const def = st.def, z = M.map.getZoom();
    const fk = def.filterKey ? def.filterKey(M.prefs[st.id] || {}, z) : "";
    if (def.bbox === false) return { key: "all|" + fk, view: { zoom: z, filterKey: fk } };
    const zz = def.zoomFor ? def.zoomFor(z) : z;
    const v = M.viewFor(zz, def.snapCells ? def.snapCells(zz) : 1);
    v.filterKey = fk;
    return { key: `${v.zoom}|${v.s},${v.n},${v.w},${v.e}|${fk}`, view: v };
  }

  M.loadLayer = function (st, opts = {}) {
    if (!M.map || !st.enabled() || !M.isOn(st.id)) return;
    const def = st.def;
    if (!def.load) return;
    if (def.wants && !def.wants(M.map.getZoom())) return;
    const { key, view } = viewKey(st);
    const ttl = def.cacheMs == null ? Infinity : def.cacheMs;
    if (!opts.force) {
      if (st.viewKey === key && st.data && (Date.now() - st.dataAt < ttl)) return;
      const hit = st.cache.get(key);
      if (hit && Date.now() - hit.t < ttl) {
        if (st.ctrl) { st.ctrl.abort(); st.ctrl = null; st.seq++; }
        st.cache.delete(key); st.cache.set(key, hit); // most recently used last
        apply(st, hit.data, view, key, hit.t);
        return;
      }
      if (st.ctrl && st.loadingKey === key) return; // already on its way
    }
    if (st.ctrl) st.ctrl.abort();
    const ctrl = (st.ctrl = new AbortController());
    const seq = ++st.seq;
    st.loading = true; st.loadingKey = key;
    Promise.resolve().then(() => def.load(view, ctrl.signal)).then((data) => {
      if (seq !== st.seq) return; // a newer request superseded this one: drop it
      st.ctrl = null; st.loading = false; st.fails = 0; st.error = null; st.lastOkAt = Date.now();
      if (ttl > 0) {
        st.cache.set(key, { data, t: Date.now() });
        while (st.cache.size > 50) st.cache.delete(st.cache.keys().next().value);
      }
      apply(st, data, view, key, Date.now());
    }).catch((e) => {
      if (HD.isAbort(e) || seq !== st.seq) return;
      st.ctrl = null; st.loading = false; st.fails++; st.error = e;
      // map layers fail quietly: the last good marks stay (§14.5)
      if (def.onError) def.onError(e, st);
      M.requestRender();
    });
  };
  function apply(st, data, view, key, at) {
    st.data = data; st.view = view; st.viewKey = key; st.dataAt = at || Date.now();
    if (st.def.onData) st.def.onData(data, st);
    for (const w of (st.waiters || []).slice()) if (w.test(data)) { st.waiters.splice(st.waiters.indexOf(w), 1); clearTimeout(w.timer); w.resolve(data); }
    if (M.ui && M.ui.onLayerData) M.ui.onLayerData(st);
    M.requestRender();
  }
  /** Resolve with a layer's data once it passes `test` (now, or when it next arrives), or null after `ms`. */
  M.waitData = (id, test, ms = 8000) => new Promise((resolve) => {
    const st = M.layers.get(id);
    if (!st) { resolve(null); return; }
    if (st.data && test(st.data)) { resolve(st.data); return; }
    const w = { test, resolve };
    w.timer = setTimeout(() => { const i = (st.waiters || []).indexOf(w); if (i >= 0) st.waiters.splice(i, 1); resolve(st.data && test(st.data) ? st.data : null); }, ms);
    (st.waiters = st.waiters || []).push(w);
  });
  M.loadAll = (opts) => { for (const st of M.layerList) M.loadLayer(st, opts); };
  let loadTimer = 0;
  M.scheduleLoad = () => { clearTimeout(loadTimer); loadTimer = setTimeout(() => M.loadAll(), 250); };
  /** Drop a layer's data and cache (filters or the user changed) and load again now. */
  M.reloadLayer = (id) => {
    const st = M.layers.get(id); if (!st) return;
    st.cache.clear(); st.viewKey = null;
    if (st.ctrl) { st.ctrl.abort(); st.ctrl = null; st.seq++; }
    if (!M.isOn(id)) { st.data = null; M.requestRender(); return; }
    M.loadLayer(st, { force: true });
  };

  /* polling: only while the map is visible, the page is not hidden and the layer is on (§14.4) */
  M.visible = false;
  let nbTimer = 0;
  function startPolling() {
    // the neighbour list: when the map opens, then every 5 minutes while it is visible (§14.4)
    clearInterval(nbTimer);
    nbTimer = setInterval(() => { if (M.visible && !document.hidden) api.neighbours().catch(() => {}); }, 5 * 60 * 1000);
    for (const st of M.layerList) {
      clearInterval(st.pollTimer);
      if (!st.def.poll) continue;
      st.pollTimer = setInterval(() => {
        if (!M.visible || document.hidden || !M.map || !M.isOn(st.id)) return;
        if (st.def.pollWhen && !st.def.pollWhen(M.map.getZoom())) return;
        M.loadLayer(st, { force: true });
      }, st.def.poll);
    }
  }
  function stopPolling() { clearInterval(nbTimer); nbTimer = 0; for (const st of M.layerList) { clearInterval(st.pollTimer); st.pollTimer = 0; } }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !M.visible || !M.map) return;
    api.neighbours().catch(() => {}); // refetches only when older than 5 minutes
    // resume with an immediate refresh of what polls
    for (const st of M.layerList) if (st.def.poll && M.isOn(st.id) && (!st.def.pollWhen || st.def.pollWhen(M.map.getZoom()))) M.loadLayer(st, { force: true });
  });

  /* ------------------------------------------------------------ the neighbour list (§15.2) */
  // nb.list is the last list fetched, for the user nb.key describes. A failed refresh keeps it (map layers keep their
  // last good state, §14.5). A list that has not arrived for the person signed in is UNKNOWN, and while it is unknown
  // nothing that could show a person's licence as a licence is drawn, listed or opened: their licence point is their
  // house, and only the list says who is a neighbour (§3.1; a grid-precision neighbour comes back registered: false).
  // An unknown list is asked for again with a backoff (2 s, doubling, at most a minute) while the map is shown.
  const nb = { promise: null, list: null, at: 0, key: "", error: null, pending: false, none: "", retry: 0, retryMs: 0 };
  const userKey = (u) => (u ? [u.callsign, u.lat, u.lon, u.range_miles, u.map_precision, u.grid].join("|") : "");
  function retryNeighbours(e) {
    clearTimeout(nb.retry); nb.retry = 0;
    // 400 = you have no location; 401 ends the session; the others will not change by asking again
    if (!e || HD.isAbort(e) || [400, 401, 403, 404, 422].includes(e.status)) return;
    nb.retryMs = Math.min(60000, nb.retryMs ? nb.retryMs * 2 : 2000);
    nb.retry = setTimeout(() => {
      nb.retry = 0;
      if (M.visible && !document.hidden && HD.session && HD.session.token && !M.neighboursKnown()) api.neighbours().catch(() => {});
    }, nb.retryMs);
  }
  /** Promise of the /api/operators/nearby list, fetched once and shared by the
   *  map, Nearby, the Feed and search; refreshed when the user changes and when
   *  it is older than 5 minutes. opts.maxAge (ms) asks for a list no older than
   *  that (Nearby's "Try again" passes 0); opts.force refetches.
   *  Rejects with the API error (400 = no location). */
  api.neighbours = function (opts = {}) {
    const me = M.me(), key = userKey(me);
    const maxAge = opts.maxAge == null || isNaN(opts.maxAge) ? 5 * 60 * 1000 : Number(opts.maxAge);
    const fresh = !!nb.list && nb.key === key && Date.now() - nb.at < maxAge;
    if (!opts.force && nb.promise && (nb.pending || fresh)) return nb.promise;
    if (!HD.session || !HD.session.token) return Promise.resolve([]);
    nb.pending = true; nb.pendingKey = key;
    const p = HD.api("/api/operators/nearby").then((list) => {
      if (nb.promise !== p) return list;
      nb.pending = false; nb.list = Array.isArray(list) ? list : []; nb.at = Date.now(); nb.key = key; nb.error = null; nb.none = "";
      clearTimeout(nb.retry); nb.retry = 0; nb.retryMs = 0;
      M.onNeighbours();
      return nb.list;
    }, (e) => {
      if (nb.promise === p) {
        nb.pending = false; nb.error = e; nb.promise = null;
        if (e && e.status === 400) { nb.none = key; nb.list = null; } // no location: nobody is your neighbour
        else if (nb.key !== key) nb.list = null; // a list asked for someone else, or for another range, is no list
        retryNeighbours(e);
        M.onNeighbours();
      }
      throw e;
    });
    nb.promise = p;
    return p;
  };
  /** The list as the map knows it right now: [] until it arrives (or when you have no location). */
  M.neighbourList = () => (nb.list && nb.key === userKey(M.me()) ? nb.list : []);
  /** Is the neighbour list known for the person signed in? Signed out or without a location there is nobody to know. */
  M.neighboursKnown = () => {
    const me = M.me();
    if (!me || !HD.session || !HD.session.token || !M.home()) return true;
    const key = userKey(me);
    return (!!nb.list && nb.key === key) || nb.none === key;
  };
  M.neighboursReady = () => M.neighboursKnown();
  /** Resolves true once the list is known (asking for it when it is not), false when it is still unknown after `ms`
   *  or after a failed request. Every path that could open a licence card waits on it (§3.1). */
  M.whenNeighbours = (ms = 5000) => {
    if (M.neighboursKnown()) return Promise.resolve(true);
    return new Promise((resolve) => {
      let done = false;
      const finish = () => { if (done) return; done = true; clearTimeout(timer); resolve(M.neighboursKnown()); };
      const timer = setTimeout(finish, ms);
      api.neighbours().then(finish, finish);
    });
  };
  M.onNeighbours = () => {
    M.personCalls = new Set([...(M.me() ? [M.me().callsign] : []), ...M.neighbourList().map((m) => m.callsign)]);
    if (M.map) { M.requestRender(); M.ui && M.ui.onNeighbours && M.ui.onNeighbours(); }
  };
  M.personCalls = new Set();
  /** A person (you or a neighbour) is never shown as a licence (§3.1). */
  M.isPerson = (call) => M.personCalls.has(String(call || "").toUpperCase());
  M.neighbour = (call) => M.neighbourList().find((m) => m.callsign === String(call || "").toUpperCase()) || null;
  /** Drop a layer's data, cache and request (it belonged to another user, or to another range), and load it again
   *  when the map is shown. */
  M.dropLayer = (st) => {
    if (st.ctrl) { st.ctrl.abort(); st.ctrl = null; }
    st.seq++; st.loading = false; st.cache.clear(); st.viewKey = null; st.data = null; st.dataAt = 0;
    if (M.map) { M.requestRender(); if (M.visible) M.loadLayer(st, { force: true }); }
  };
  if (HD.on) {
    // who the map last drew for: another callsign (or log-out) drops everything that belonged to the last one;
    // another range or place drops what depends on them (the posts of your feed)
    let seen = { call: M.me() ? M.me().callsign : null, key: userKey(M.me()) };
    HD.on("user", (d) => {
      const u = d && d.user, call = u ? u.callsign : null, key = userKey(u);
      const callChanged = call !== seen.call, keyChanged = key !== seen.key;
      seen = { call, key };
      if (key !== nb.key) {
        nb.list = null;
        if (nb.none !== key) nb.none = "";
        clearTimeout(nb.retry); nb.retry = 0; nb.retryMs = 0;
        // a request already on its way for this very user stands; anything else is asked for again
        if (!(nb.pending && nb.pendingKey === key)) {
          nb.promise = null; nb.pending = false;
          if (u && HD.session.token) api.neighbours().catch(() => {});
        }
      }
      if (callChanged || keyChanged) for (const st of M.layerList) if (st.def.perUser) M.dropLayer(st);
      M.personCalls = new Set([...(u ? [u.callsign] : []), ...M.neighbourList().map((m) => m.callsign)]);
      if (M.map) { if (M.ui && M.ui.onUser) M.ui.onUser({ callChanged }); M.requestRender(); }
    });
  }

  /* ------------------------------------------------------------ basemaps (§9.5) */
  const GL = {
    css: ["https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.css", "sha384-uTttxo/aOKbdE5RlD/SPzSDoDmNvGlUYPjONi2MN/b7c9HPSvW07OIuyP7uL6jxK"],
    js: ["https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.js", "sha384-5+cfbwT0iiub6VsQAdn6yz16nr6sDiQoHx6tm4O8OVYXHYOxcffFmCJBL0dgdvGp"],
    plugin: ["https://unpkg.com/@maplibre/maplibre-gl-leaflet@0.1.4/leaflet-maplibre-gl.js", "sha384-tXYNKOHx4T02jMP7YYCtBxPIv1B5gaA5mcVPBzqMp6d7VzWzxJgI2aWF/nJLrQdS"],
  };
  const FALLBACK_BASEMAPS = [
    { id: "osm", label: "OSM", kind: "raster", url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", tone: "light", max_native_zoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' },
  ];
  M.basemaps = () => { const b = HD.config && Array.isArray(HD.config.basemaps) && HD.config.basemaps.length ? HD.config.basemaps : FALLBACK_BASEMAPS; return b; };
  M.basemapById = (id) => M.basemaps().find((b) => b.id === id) || null;
  /** The switcher lists every basemap except the one others name as their fallback (osm). */
  M.switcherBasemaps = () => { const all = M.basemaps(), fb = new Set(all.map((b) => b.fallback).filter(Boolean)); const list = all.filter((b) => !fb.has(b.id)); return list.length ? list : all; };
  M.hasWebGL = (() => { let v; return () => { if (v === undefined) { try { const c = document.createElement("canvas"); v = !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl"))); } catch (e) { v = false; } } return v; }; })();
  const glFailedThisSession = () => { try { return sessionStorage.getItem("hamdoor.gl.failed") === "1"; } catch (e) { return !!M._glFailed; } };
  const loadTag = (tag, url, sri) => new Promise((resolve, reject) => {
    const el = document.createElement(tag);
    if (tag === "link") { el.rel = "stylesheet"; el.href = url; } else { el.src = url; el.async = false; }
    el.integrity = sri; el.crossOrigin = "anonymous";
    el.onload = () => resolve(); el.onerror = () => reject(new Error("could not load " + url));
    document.head.appendChild(el);
  });
  let glPromise = null;
  M.loadGL = () => {
    if (glPromise) return glPromise;
    if (!M.hasWebGL()) return Promise.reject(new Error("no WebGL"));
    if (window.maplibregl && L.maplibreGL) return (glPromise = Promise.resolve());
    const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error("MapLibre took too long")), 15000));
    glPromise = Promise.race([Promise.all([
      loadTag("link", GL.css[0], GL.css[1]).catch(() => {}),
      (window.maplibregl ? Promise.resolve() : loadTag("script", GL.js[0], GL.js[1])).then(() => (L.maplibreGL ? null : loadTag("script", GL.plugin[0], GL.plugin[1]))),
    ]), timeout]).then(() => { if (!window.maplibregl || !L.maplibreGL) throw new Error("MapLibre did not register"); });
    glPromise.catch(() => { glPromise = null; });
    return glPromise;
  };
  /** Dark theme: the default basemap is Dark until the person picks one (§9.5). The theme is the shell's (HD.theme). */
  const themeIsDark = () => (HD.theme && HD.theme.isDark ? HD.theme.isDark() : MQ.dark.matches);
  M.effectiveBasemap = () => {
    const pick = M.prefs && M.prefs.basemap;
    if (pick && M.basemapById(pick)) return pick;
    const def = (HD.config && HD.config.default_basemap) || "streets";
    if (themeIsDark() && M.basemapById("dark")) return "dark";
    return M.basemapById(def) ? def : (M.switcherBasemaps()[0] || {}).id;
  };
  let base = null, baseSeq = 0;
  M.basemapId = null;
  /** Overlay colours follow the basemap tone, not the theme: canvas layers re-read the tokens and redraw (§3). */
  function setTone(tone) {
    M.tone = tone || "light";
    document.body.dataset.mapTone = M.tone;
    M.readTokens();
    M.redrawCanvases();
    M.requestRender();
  }
  M.redrawCanvases = () => {
    if (!M.R) return;
    for (const r of Object.values(M.R)) if (r && r._map && r._ctx && r._bounds) { r._redrawBounds = null; r._redraw(); }
  };
  function rasterLayer(src, className) {
    const layer = L.tileLayer(src.url, { maxNativeZoom: src.max_native_zoom || 19, maxZoom: 19, attribution: M.safeAttribution(src.attribution), className: className || "", crossOrigin: false });
    return layer;
  }
  /** Switch the basemap. A GL style that fails falls back to OSM raster for the session (§9.5). */
  M.setBasemap = function (id, opts = {}) {
    if (!M.map) return;
    const def = M.basemapById(id) || M.basemapById(M.effectiveBasemap()) || M.basemaps()[0];
    if (!def) return;
    const seq = ++baseSeq;
    M.basemapId = def.id;
    document.body.dataset.basemapReady = "0";
    const ready = () => { if (seq === baseSeq) { document.body.dataset.basemapReady = "1"; if (M.ui && M.ui.onBasemapReady) M.ui.onBasemapReady(); } };
    const swap = (layer, tone) => {
      if (seq !== baseSeq) { return false; }
      if (base) M.map.removeLayer(base);
      base = layer;
      base.addTo(M.map);
      setTone(tone);
      if (M.ui && M.ui.syncLayers) M.ui.syncLayers();
      return true;
    };
    const fallbackDef = M.basemapById(def.fallback || "osm") || FALLBACK_BASEMAPS[0];
    const useRaster = (night) => {
      const layer = rasterLayer(fallbackDef, night ? "hd-tiles-night" : "");
      layer.once("load", ready);
      swap(layer, night ? "dark" : fallbackDef.tone || "light");
    };
    if (def.kind === "gl") {
      if (opts.forceRaster || glFailedThisSession() || !M.hasWebGL()) { useRaster(def.tone === "dark"); return; }
      // keep the overlays' tone right while the library loads
      setTone(def.tone);
      M.loadGL().then(() => {
        if (seq !== baseSeq) return;
        const layer = L.maplibreGL({ style: def.url, pane: "tilePane", attributionControl: { customAttribution: M.safeAttribution(def.attribution) } });
        layer.once("add", () => {
          const m = layer.getMaplibreMap && layer.getMaplibreMap();
          if (!m) return;
          let styled = false, tileErrors = 0, idle = false;
          const failTimer = setTimeout(() => { if (!styled && seq === baseSeq) glFailed(def); }, 8000);
          const flatten = () => {
            try {
              for (const ly of m.getStyle().layers || []) {
                if (ly.type === "fill-extrusion") m.setLayoutProperty(ly.id, "visibility", "none"); // 3-D buildings lean off their footprints
                if (ly.id === "poi_r20" || ly.id === "poi_r7" || ly.id === "poi_transit") m.setLayoutProperty(ly.id, "visibility", "none"); // POI and transit icons read as marks
              }
              if (m.getLayer("landuse_residential")) m.setPaintProperty("landuse_residential", "fill-color", "hsla(226, 22%, 90%, 0.45)");
            } catch (e) { /* a style without these layers: nothing to change */ }
          };
          m.on("style.load", () => { styled = true; clearTimeout(failTimer); flatten(); });
          if (m.isStyleLoaded && m.isStyleLoaded()) { styled = true; clearTimeout(failTimer); flatten(); }
          m.on("error", (ev) => {
            if (idle || seq !== baseSeq) return;
            const isTile = ev && (ev.sourceId || ev.tile);
            if (!isTile ? !styled : ++tileErrors >= 5) { clearTimeout(failTimer); glFailed(def); }
          });
          m.on("idle", () => { idle = true; ready(); });
        });
        swap(layer, def.tone);
      }).catch(() => { if (seq === baseSeq) glFailed(def); });
      return;
    }
    const layer = rasterLayer(def, "");
    layer.once("load", ready);
    swap(layer, def.tone);
  };
  function glFailed(def) {
    try { sessionStorage.setItem("hamdoor.gl.failed", "1"); } catch (e) { M._glFailed = true; }
    if (M._glToastShown !== true) { M._glToastShown = true; M.toast("The street map couldn't load, so the map is showing OpenStreetMap tiles."); }
    M.setBasemap(def.id, { forceRaster: true });
  }
  // the theme can change while the map is open (You > Display, or the system's scheme while it is "System"):
  // follow it unless a basemap was picked. The shell emits hd:theme for both.
  const followTheme = () => { if (M.map && M.prefs && !M.prefs.basemap && M.effectiveBasemap() !== M.basemapId) M.setBasemap(M.effectiveBasemap()); };
  HD.on("theme", followTheme);

  /* ------------------------------------------------------------ the render loop */
  let renderQueued = false;
  M.requestRender = () => {
    if (renderQueued || !M.map) return;
    renderQueued = true;
    requestAnimationFrame(() => { renderQueued = false; M.renderAll(); });
  };
  /** Every layer renders in order (licences first, so people and activators can ride
   *  postmark rims), marks are diffed by key, then labels are laid out once. */
  M.renderAll = () => {
    if (!M.map || !M.T) return;
    const z = M.map.getZoom();
    document.body.dataset.zoomband = z < M.MINZ.fccDots ? "area" : "street";
    const ctx = M.ctx = { zoom: z, tone: M.tone, T: M.T, prefs: M.prefs, sel: M.ui ? M.ui.selection() : null,
      toPoint: (lat, lon) => M.map.latLngToContainerPoint([lat, lon]), bounds: M.map.getBounds(), postmarks: [], rim: null };
    for (const st of M.layerList) {
      const on = M.isOn(st.id);
      if (st.id === "members") ctx.rim = M.marks.makeRim(ctx);
      let desired = new Map();
      if ((on || st.def.alwaysRender) && st.def.render) { try { desired = st.def.render(st.data, ctx, st) || desired; } catch (e) { console.error("[hd] map layer", st.id, e); } }
      M.marks.syncMarkers(st.store, desired);
      if (st.def.canvas) { try { st.def.canvas(on ? st.data : null, ctx, st); } catch (e) { console.error("[hd] map canvas", st.id, e); } }
    }
    M.marks.renderSel(ctx);
    if (M.ui) M.ui.afterRender(ctx);
  };

  /* ------------------------------------------------------------ map creation */
  M.map = null;
  M.R = null;
  function initMap() {
    const el = document.getElementById("hd-map");
    const reduced = M.reduced();
    const map = (M.map = L.map(el, { zoomControl: false, attributionControl: true, preferCanvas: true, minZoom: 3, maxZoom: 19, zoomSnap: 1,
      keyboard: true, worldCopyJump: false, zoomAnimation: !reduced, fadeAnimation: !reduced, markerZoomAnimation: !reduced }));
    map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
    for (const [name, z] of M.PANES) { const p = map.createPane(name); p.style.zIndex = String(z); }
    map.getPane("hd-labels").style.pointerEvents = "none";
    map.getPane("hd-sel").style.pointerEvents = "none";
    const tol = M.coarse() ? cssNum("--hd-canvas-tol-coarse", 14) : cssNum("--hd-canvas-tol-fine", 3);
    M.R = { range: L.canvas({ pane: "hd-range" }), tracks: L.canvas({ pane: "hd-tracks", tolerance: 0 }), fcc: L.canvas({ pane: "hd-fcc", tolerance: tol }) };
    for (const r of Object.values(M.R)) map.addLayer(r);
    // the map is one widget (§9.6)
    el.setAttribute("aria-roledescription", "map");
    el.setAttribute("aria-describedby", "hd-map-help");
    map.on("moveend", onMoveEnd);
    map.on("zoomstart", () => { if (M.marks) M.marks.hideLabels(); });
    map.on("resize", () => M.requestRender());
    return map;
  }
  function onMoveEnd() {
    M.requestRender();
    M.scheduleLoad();
    const c = M.map.getCenter(), z = M.map.getZoom();
    const v = { lat: Number(c.lat.toFixed(5)), lon: Number(c.lng.toFixed(5)), z };
    HD.store.set("hamdoor.map.view", v);
    const cur = HD.router.current();
    if (M.visible && cur && cur.name === "map") HD.router.patch({ lat: v.lat, lon: v.lon, z: v.z });
    if (M.ui && M.ui.onMoveEnd) M.ui.onMoveEnd();
  }

  /* ------------------------------------------------------------ first view (§6.5) */
  function initialView() {
    const map = M.map, y = M.youPlace();
    const v = HD.store.get("hamdoor.map.view", null);
    if (v && M.finite(v.lat) && M.finite(v.lon) && M.finite(v.z)) map.setView([v.lat, v.lon], M.clamp(Math.round(v.z), 3, 19), { animate: false });
    else if (y) map.setView([y.lat, y.lon], 12, { animate: false });
    else map.setView([39.8, -98.5], 4, { animate: false });
    if (!y) M.toast("Add your grid square or address in You to see your neighbourhood.");
  }
  function applyView(params, first) {
    const map = M.map;
    const lat = M.num(params.lat), lon = M.num(params.lon), z = parseInt(params.z, 10);
    if (lat != null && lon != null && Math.abs(lat) <= 90 && Math.abs(lon) <= 180) {
      const zz = isFinite(z) ? M.clamp(z, 3, 19) : first ? 13 : Math.max(map.getZoom(), 13);
      const c = first ? null : map.getCenter();
      const moved = first || Math.abs(c.lat - lat) > 1e-5 || Math.abs(c.lng - lon) > 1e-5 || map.getZoom() !== zz;
      if (moved) map.setView([lat, lon], zz, { animate: !first && !M.reduced() });
    } else if (first) initialView();
  }
  function applyParams(params, first) {
    applyView(params, first);
    if (params.sel && M.ui && params.sel !== M.ui.selId()) M.ui.openSel(String(params.sel), { fromNav: true });
  }

  let booted = false;
  function init(params) {
    loadPrefs();
    M.readTokens();
    M.personCalls = new Set(M.me() ? [M.me().callsign] : []);
    M.ui.build();
    initMap();
    applyView(params || {}, true);
    M.ui.attach();
    if (params && params.sel) M.ui.openSel(String(params.sel), { fromNav: true });
    M.setBasemap(M.effectiveBasemap());
    booted = true;
    if (document.fonts && document.fonts.load) document.fonts.load('700 13px "Atkinson Hyperlegible Next"').then(() => { M.marks.clearWidths(); M.requestRender(); }, () => {});
  }

  /* ------------------------------------------------------------ the route and the public API (contract §3) */
  function show(params) {
    params = params || {};
    M.visible = true; // the shell marks <body data-route="map">; the shared CSS keys on that
    if (!booted) init(params);
    else { M.map.invalidateSize({ pan: false }); applyParams(params, false); M.requestRender(); }
    api.neighbours().catch(() => {});
    for (const st of M.layerList) if (st.stale) { st.stale = false; st.cache.clear(); st.viewKey = null; }
    M.loadAll();
    startPolling();
    if (M.ui && M.ui.onShow) M.ui.onShow();
  }
  function hide() {
    M.visible = false;
    stopPolling();
    if (M.ui && M.ui.onHide) M.ui.onHide();
  }
  api.show = (params) => show(params);
  api.hide = () => hide();
  /** Open the map at a place and (optionally) a card: sel = fcc:<call>, member:<call>, you:me,
   *  rpt:<id>, aprs:<key>, spot:<kind>:<call>, post:<id>, zip:<zip>. */
  api.focus = function ({ lat, lon, zoom, sel } = {}) {
    const params = {};
    if (M.finite(Number(lat)) && M.finite(Number(lon))) { params.lat = Number(Number(lat).toFixed(5)); params.lon = Number(Number(lon).toFixed(5)); }
    if (zoom != null && isFinite(zoom)) params.z = Math.round(zoom);
    if (sel) params.sel = sel;
    const cur = HD.router.current();
    if (cur && cur.name === "map" && booted && M.visible) {
      applyParams(params, false);
      HD.router.patch(params);
      return;
    }
    HD.router.go("map", params);
  };
  /** Reload one layer (or all) now, dropping cached answers. */
  api.refresh = function (layerId) {
    for (const st of M.layerList) {
      if (layerId && st.id !== layerId) continue;
      st.cache.clear(); st.viewKey = null;
      if (M.visible && M.map) M.loadLayer(st, { force: true }); else st.stale = true;
    }
    if (!layerId || layerId === "members") api.neighbours({ force: true }).catch(() => {});
  };

  /* At mount the map chrome moves in front of the map, so DOM order is keyboard
     order (§9.6, §15.4). The heavy setup waits for the first show, when every
     layer file has registered itself. */
  HD.router.register("map", {
    mount(el) {
      const ui = document.getElementById("hd-map-ui"), mapEl = document.getElementById("hd-map");
      if (ui && mapEl && mapEl.parentNode === el && ui.nextElementSibling !== mapEl) el.insertBefore(ui, mapEl);
    },
    show(params) { show(params); },
    hide() { hide(); },
  });
})();
