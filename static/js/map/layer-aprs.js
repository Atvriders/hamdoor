/* hamdoor map 7/9: live APRS. DESIGN_SYSTEM.md §3.4, §7.10, §7.11, §9.1, §14.4.
   GET /api/aprs/map every 30 s while the map is visible, the layer is on and
   z >= 9 (below z9 the API returns only a count). Stations are hessu/aprs-symbols
   sprites (aprs.fi's set) with a callsign pill tinted by category, a course
   tick when moving, an ambiguity box, and one magenta track with fix dots.
   The live chip shows the feed's state; Follow keeps a station centred. */
(function () {
  "use strict";
  const HD = window.HD;
  const M = HD.map._;
  const K = M.marks;
  const NB = M.NB;
  const esc = M.esc;
  const CATS = [["mobile", "Moving", ["/", ">"]], ["fixed", "Fixed", ["/", "-"]], ["wx", "Weather", ["/", "_"]], ["digi", "Digipeaters and iGates", ["/", "#"]], ["repeater", "Repeaters on APRS", ["/", "r"]], ["object", "Other objects", ["/", "o"]]];
  const WINDOWS = [[1800, "30 min"], [3600, "1 hour"], [10800, "3 hours"]];
  const NOUN = { "/>": "Car", "/j": "Jeep", "/k": "Pickup truck", "/v": "Van", "/u": "Truck", "/U": "Bus", "/R": "Motorhome", "/<": "Motorcycle", "/b": "Bicycle", "/[": "Person on foot",
    "/s": "Boat", "/Y": "Sailboat", "/'": "Small aircraft", "/^": "Aircraft", "/X": "Helicopter", "/O": "Balloon", "/a": "Ambulance", "/f": "Fire truck", "/P": "Police car", "/=": "Train",
    "/-": "Home station", "/y": "Home station with a beam", "/o": "Emergency operations centre", "/_": "Weather station", "/W": "Weather service site", "/#": "Digipeater", "/&": "Gateway",
    "/r": "Repeater", "/m": "Repeater", "/h": "Hospital", "/!": "Police station", "/d": "Fire department", "/+": "Red Cross", "/;": "Campground", "/$": "Phone", "/l": "Laptop", "/n": "Node",
    "/`": "Dish antenna", "/K": "School", "/H": "Hotel", "/c": "Incident command post", "/E": "Eyeball event", "/A": "Aid station", "/B": "Bulletin board",
    "\\#": "Digipeater", "\\&": "iGate", "\\>": "Car", "\\-": "Home station", "\\_": "Weather station", "\\k": "SUV", "\\u": "Truck", "\\v": "Van", "\\s": "Boat", "\\a": "ARES station", "\\j": "Work zone" };
  const CAT_NOUN = { mobile: "Moving station", fixed: "Fixed station", wx: "Weather station", digi: "Digipeater", repeater: "Repeater object", object: "Object" };
  const CAT_SYM = Object.fromEntries(CATS.map(([k, , s]) => [k, { table: s[0], code: s[1] }]));
  const noun = (s) => (s.sym && NOUN[(s.sym.table === "/" ? "/" : "\\") + s.sym.code]) || CAT_NOUN[s.cat] || "Station";
  const mph = (kmh) => Math.round(kmh * 0.6214);
  const isObject = (s) => s.kind && s.kind !== "station";
  const label = (s) => (isObject(s) ? esc(s.call) : M.callHtml(s.call));
  const moving = (s) => s.cat === "mobile" && M.finite(s.speed_kmh) && s.speed_kmh > 0 && M.finite(s.course);
  const pillCls = (s) => (s.emergency ? "hd-pill--emergency" : s.cat === "wx" ? "hd-pill--wx" : s.cat === "repeater" || s.cat === "object" ? "hd-pill--obj" : s.cat === "digi" || !s.messaging ? "hd-pill--nomsg" : "");
  const ageMin = (s) => Math.max(0, (Date.now() / 1000 - (s.last || 0)) / 60);

  /* ------------------------------------------------------------ what is drawn: APRS repeater objects that duplicate a Repeaters machine hide while that layer is on (§3.3) */
  function dupHidden(s) {
    if (s.cat !== "repeater" || !M.finite(s.freq) || !M.isOn("rpt")) return false;
    const rd = M.data("rpt");
    if (!rd || rd.type !== "repeaters") return false;
    return (rd.repeaters || []).some((r) => Math.abs(r.output_mhz - s.freq) <= 0.002 && M.distMi(r, s) <= 1);
  }
  const visible = (data) => (data && data.type === "stations" ? (data.stations || []).filter((s) => M.finite(s.lat) && M.finite(s.lon) && !dupHidden(s)) : []);

  /* ------------------------------------------------------------ live state (§7.10) */
  let liveConnected = true;
  function liveOk(data, st) {
    if (data.feed) liveConnected = data.feed.connected !== false;
    M.ui.setLive({ state: liveConnected ? "ok" : "stale", stations: data.total || 0, at: Date.now() });
    if (following) followUpdate(data);
  }
  /** the open station card follows each live update: a new position or a new beacon refetches its track */
  function refreshSel(sel, data) {
    if (sel.kind !== "aprs" || !data || data.type !== "stations") return false;
    const s = (data.stations || []).find((x) => x.key === sel.s.key);
    if (!s || (s.last === sel.s.last && s.lat === sel.s.lat && s.lon === sel.s.lon)) return false;
    sel.s = Object.assign({}, sel.s, s);
    refreshStation(sel);
    return false;
  }
  function liveFail(e, st) {
    // three failures in a row, or no answer for 90 s
    const since = Date.now() - (st.lastOkAt || 0);
    M.ui.setLive({ state: st.fails >= 3 ? "error" : since > 90000 || !st.lastOkAt ? "stale" : "ok", stations: st.data ? st.data.total || 0 : 0, at: st.lastOkAt || Date.now() });
  }

  /* ------------------------------------------------------------ Follow (§7.11) */
  let following = null, followCall = "";
  function followUpdate(data) {
    const s = (data.stations || []).find((x) => x.key === following);
    if (!s) { stopFollow("gone"); return; }
    // centred in what you can see: beside a landscape side panel, above a phone card at half (§7.11)
    M.ui.centerOn([s.lat, s.lon], M.map.getZoom(), true);
  }
  function stopFollow(why) {
    if (!following) return;
    const call = followCall;
    following = null; followCall = "";
    const b = document.querySelector("#hd-detail-body [data-act=follow]");
    if (b) { b.setAttribute("aria-pressed", "false"); const t = b.querySelector("span"); if (t) t.textContent = "Follow"; }
    if (why !== "button") M.toast(`Stopped following ${call}`);
  }

  /* ------------------------------------------------------------ render */
  function ambBox(s, z) {
    // ambiguity 1..4 = 0.1', 1', 10', 1 degree boxes
    const deg = [0, 0.1 / 60, 1 / 60, 10 / 60, 1][Math.min(4, s.amb)] || 0;
    if (!deg) return "";
    const a = M.map.project([s.lat - deg / 2, s.lon - deg / 2], z), b = M.map.project([s.lat + deg / 2, s.lon + deg / 2], z);
    const w = Math.max(8, Math.round(Math.abs(b.x - a.x))), h = Math.max(8, Math.round(Math.abs(b.y - a.y)));
    if (w > 4000 || h > 4000) return "";
    return `<span class="hd-mk-aprs__amb" style="width:${w}px;height:${h}px"></span>`;
  }
  function render(data, ctx) {
    const want = new Map(), z = ctx.zoom;
    if (!data || data.type !== "stations" || z < M.MINZ.aprs) return want;
    const compact = z < M.MINZ.aprsPills, selKey = ctx.sel && ctx.sel.layer === "aprs" && ctx.sel.s ? ctx.sel.s.key : null;
    const b = ctx.bounds.pad(0.25);
    for (const s of visible(data)) {
      if (!b.contains([s.lat, s.lon]) && s.key !== selKey) continue;
      const sel = s.key === selKey, am = ageMin(s);
      const age = am < 10 ? "" : am < 30 ? " hd-mk-aprs--age2" : " hd-mk-aprs--age3";
      const hdg = moving(s) ? `<span class="hd-mk-aprs__hdg" style="--crs:${Math.round(s.course)}deg"></span>` : "";
      const amb = z >= M.MINZ.aprsAmb && s.amb > 0 ? ambBox(s, z) : "";
      // heading east, the course triangle would hide under the pill on the right: the placer tries the left first
      const crs = moving(s) ? ((s.course % 360) + 360) % 360 : -1;
      want.set("ap:" + s.key, {
        latlng: [s.lat, s.lon], size: [24, 24], pane: "hd-aprs", z: sel ? 800 : 0, lbl: true, minZ: M.MINZ.aprsPills, pri: sel ? -5 : 1 + am / 1000, always: sel,
        preferLeft: crs > 35 && crs < 145,
        html: `<div class="hd-mk-aprs${compact ? " hd-mk-aprs--compact" : ""}${age}${sel ? " is-selected" : ""}">${amb}${K.spriteHtml(s.sym)}${hdg}<span class="hd-pill ${pillCls(s)}">${label(s)}</span></div>`,
        title: `${s.call}, ${s.emergency ? "emergency beacon, " : ""}${noun(s)}`, item: { layer: "aprs", kind: "aprs", s },
      });
    }
    return want;
  }
  /* tracks: one magenta line with fix dots; the selected one 5 px, every other dimmed to 35 % (§3.4) */
  const trackStore = new Map();
  let selDetail = null;
  function canvas(data, ctx) {
    const want = new Map(), z = ctx.zoom, T = ctx.T || M.T;
    if (data && data.type === "stations" && M.isOn("aprs") && M.prefs.aprs.tracks && z >= M.MINZ.tracks) {
      const selKey = ctx.sel && ctx.sel.layer === "aprs" && ctx.sel.s ? ctx.sel.s.key : null;
      for (const s of visible(data)) {
        const tr = s.key === selKey && selDetail && selDetail.key === s.key && selDetail.track ? selDetail.track : s.track;
        if (!tr || tr.length < 2) continue;
        want.set(s.key, { pts: tr.map((p) => [p[0], p[1]]), sig: `${tr.length}:${tr[0][2]}:${tr[tr.length - 1][2]}`, sel: s.key === selKey, dim: !!selKey && s.key !== selKey, dots: z >= M.MINZ.trackDots });
      }
    }
    for (const [k, t] of trackStore) if (!want.has(k)) { M.map.removeLayer(t.casing); M.map.removeLayer(t.line); trackStore.delete(k); }
    for (const [k, d] of want) {
      const casing = { color: T.casing, weight: (d.sel ? 5 : 3) + 3, opacity: d.dim ? 0.35 : 1 };
      const line = { color: T.track, weight: d.sel ? 5 : 3, opacity: d.dim ? 0.35 : 0.92, dots: d.dots };
      let t = trackStore.get(k);
      if (!t) {
        const common = { renderer: M.R.tracks, lineJoin: "round", lineCap: "round", interactive: false, smoothFactor: 0.5 };
        t = { casing: new K.TrackLine(d.pts, Object.assign({}, common, casing)).addTo(M.map), line: new K.TrackLine(d.pts, Object.assign({}, common, line)).addTo(M.map), sig: d.sig, style: "" };
        trackStore.set(k, t);
      } else if (t.sig !== d.sig) { t.casing.setLatLngs(d.pts); t.line.setLatLngs(d.pts); t.sig = d.sig; }
      const style = JSON.stringify([casing, line]);
      if (t.style !== style) { t.casing.setStyle(casing); t.line.setStyle(line); t.style = style; }
    }
  }

  /* ------------------------------------------------------------ panel */
  function counts(data, ctx) {
    if (!data) return { meta: "", chips: {} };
    if (data.type !== "stations") return { meta: `${M.fmtN(data.total || 0)} ${data.total === 1 ? "station" : "stations"} in view. Zoom in to see them`, chips: {} };
    const b = M.screenBounds(), c = { mobile: 0, fixed: 0, wx: 0, digi: 0, repeater: 0, object: 0, tracks: 0 };
    let n = 0;
    for (const s of visible(data)) if (b.contains([s.lat, s.lon])) { n++; c[s.cat] = (c[s.cat] || 0) + 1; if (s.track && s.track.length > 1) c.tracks++; }
    return { meta: data.truncated ? `${M.fmtN(data.total)}+ stations in view` : `${M.fmtN(n)} ${n === 1 ? "station" : "stations"} in view`, chips: c };
  }
  const spriteSw = (tab, code) => { const i = code.charCodeAt(0) - 33; return `<span class="hd-sw hd-sw--sprite hd-mk-aprs__sym--${tab === "/" ? "p" : "a"}" style="background-position:${-(i % 16) * 20}px ${-Math.floor(i / 16) * 20}px"></span>`; };
  function section() {
    const max = (HD.config && HD.config.aprs_window_max_s) || 21600;
    return `<div class="hd-chips hd-layers__chips">${CATS.map(([k, l, s]) => M.chip("aprs/" + k, l, spriteSw(s[0], s[1]))).join("")}${M.chip("aprs/tracks", "Tracks", '<span class="hd-sw hd-sw--line"></span>')}</div>
      <p class="hd-layers__sub" id="hd-aprs-window">Show stations heard in the last</p>
      <div class="hd-seg" role="group" aria-labelledby="hd-aprs-window" style="margin-top:8px">${WINDOWS.filter(([v]) => v <= max).map(([v, l]) => `<button class="hd-seg__btn" type="button" data-seg="aprs/window" data-value="${v}" aria-pressed="false">${l.replace(" ", NB)}</button>`).join("")}</div>`;
  }
  function syncSection(lb, p) { for (const b of lb.querySelectorAll('[data-seg="aprs/window"]')) b.setAttribute("aria-pressed", String(Number(b.dataset.value) === p.window)); }
  const hint = (z) => (z < M.MINZ.aprs ? ["APRS", M.MINZ.aprs, 1] : null);
  function filterParams(p) {
    const on = CATS.map(([k]) => k).filter((k) => p[k]);
    return `window=${p.window}&tracks=${p.tracks ? 1 : 0}${on.length < CATS.length ? `&cats=${on.length ? on.join(",") : "none"}` : ""}`;
  }

  /* ------------------------------------------------------------ the station card (§7.11) */
  function heardVia(s) {
    const path = String(s.path || "");
    if (/TCPIP/i.test(path)) return "Internet";
    const used = path.split(",").find((x) => x.endsWith("*") && !/^(WIDE|TRACE|RELAY|q)/i.test(x));
    return (used || s.igate || "").replace("*", "");
  }
  function trackSummary(tr) {
    if (!tr || tr.length < 2) return "";
    const pts = tr.map((p) => ({ lat: p[0], lon: p[1] }));
    let mi = 0;
    for (let i = 1; i < pts.length; i++) mi += M.distMi(pts[i - 1], pts[i]);
    const mins = Math.max(1, Math.round((tr[tr.length - 1][2] - tr[0][2]) / 60));
    const dir = M.dir8w(M.bearing(pts[0], pts[pts.length - 1]));
    const span = mins < 60 ? `${mins}${NB}${mins === 1 ? "minute" : "minutes"}` : `${Math.round(mins / 6) / 10}${NB}hours`;
    return `${tr.length} positions over ${mi.toFixed(1)}${NB}mi in the last ${span}${mi >= 0.05 ? `, heading ${dir}` : ""}.`;
  }
  // a fix-to-fix speed is only shown when the two fixes are 10 s or more apart and the speed is believable
  // (a station that reports flying speeds may show anything)
  const FIX_MIN_SECONDS = 10, FIX_MAX_MPH = 250;
  function fixRows(tr, s) {
    const out = [];
    const flies = !!(s && M.finite(s.speed_kmh) && mph(s.speed_kmh) > FIX_MAX_MPH);
    for (let i = tr.length - 1; i >= 0 && out.length < 12; i--) {
      const p = tr[i], q = tr[i - 1];
      let sp = "", dir = "";
      if (q) {
        const d = M.distMi({ lat: q[0], lon: q[1] }, { lat: p[0], lon: p[1] }), secs = p[2] - q[2];
        const v = secs >= FIX_MIN_SECONDS ? d / (secs / 3600) : NaN;
        if (Number.isFinite(v) && (v <= FIX_MAX_MPH || flies)) sp = `${Math.round(v)}${NB}mph`;
        dir = d >= 0.01 ? M.dir8(M.bearing({ lat: q[0], lon: q[1] }, { lat: p[0], lon: p[1] })) : "";
      }
      out.push(`<tr><td><span class="hd-fixdot"></span>${M.clock(new Date(p[2] * 1000), true)}</td><td>${sp}</td><td>${dir}</td></tr>`);
    }
    return out.join("");
  }
  function weatherHtml(wx) {
    if (!wx) return "";
    const rows = [];
    if (M.finite(wx.temp_c)) rows.push(["Temperature", `${Math.round(wx.temp_c * 9 / 5 + 32)}${NB}°F`]);
    if (M.finite(wx.wind_kmh)) rows.push(["Wind", wx.wind_kmh < 1 ? "Calm" : `${M.finite(wx.wind_dir) ? `From the ${M.dir8w(wx.wind_dir)} at ` : ""}${mph(wx.wind_kmh)}${NB}mph${M.finite(wx.gust_kmh) && wx.gust_kmh > wx.wind_kmh ? `, gusts ${mph(wx.gust_kmh)}${NB}mph` : ""}`]);
    if (M.finite(wx.rain_1h_mm)) rows.push(["Rain, last hour", `${(wx.rain_1h_mm / 25.4).toFixed(2)}${NB}in`]);
    if (M.finite(wx.rain_24h_mm)) rows.push(["Rain, last 24 hours", `${(wx.rain_24h_mm / 25.4).toFixed(2)}${NB}in`]);
    if (M.finite(wx.humidity)) rows.push(["Humidity", `${Math.round(wx.humidity)}${NB}%`]);
    if (M.finite(wx.pressure_hpa)) rows.push(["Pressure", `${wx.pressure_hpa.toFixed(1)}${NB}hPa`]);
    return rows.length ? `<section class="hd-detail__section"><h3 class="hd-detail__h">Weather</h3><dl class="hd-kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></section>` : "";
  }
  function card(it) {
    if (it.kind === "aprsfi") return aprsfiCard(it);
    const s = it.s, mv = moving(s), n = noun(s);
    const lede = mv ? `${n}, heading ${M.dir8w(s.course)} at ${mph(s.speed_kmh)}${NB}mph`
      : s.cat === "wx" && s.wx && M.finite(s.wx.wind_kmh) && s.wx.wind_kmh >= 1 && M.finite(s.wx.wind_dir) ? `${n}, wind from the ${M.dir8w(s.wx.wind_dir)} at ${mph(s.wx.wind_kmh)}${NB}mph` : n;
    const cells = [["Grid", M.finite(s.lat) ? M.grid(s.lat, s.lon, 6) : ""], ["Distance", M.distDir(s.lat, s.lon) || "Not known"]];
    if (mv) cells.push(["Course", `${Math.round(s.course)}° <small>${M.dir8(s.course)}</small>`], ["Speed", `${mph(s.speed_kmh)}${NB}mph`]);
    if (M.finite(s.alt_m)) cells.push(["Altitude", `${M.fmtN(Math.round(s.alt_m * 3.281))}${NB}ft`]);
    const via = heardVia(s);
    if (via) cells.push(["Heard via", esc(via), cells.length % 2 === 0]);
    const d = it.detail && it.detail.key === s.key ? it.detail : null;
    const tr = (d && d.track) || s.track;
    const w = it.window || 3600;
    const following_ = following === s.key;
    const xrefHtml = xrefRow(it.xref);
    const url = HD.safeUrl(s.aprsfi_url);
    return `${K.head(`<span class="hd-mk-aprs" style="width:48px;height:48px;cursor:default">${K.spriteHtml(s.sym, 48)}</span>`, label(s), "hd-call hd-detail__call", M.spell(s.call))}
      ${s.emergency ? `<p class="hd-detail__lede hd-detail__lede--alert">${M.ic("alert", 20)}<span>Emergency beacon</span></p>` : ""}
      <p class="hd-detail__lede">${esc(lede)}</p><p class="hd-detail__sub">Heard ${esc(M.ago(s.last))}</p>
      <div class="hd-detail__actions">${mv ? `<button class="hd-btn hd-btn--primary hd-btn--sm" type="button" data-act="follow" aria-pressed="${following_}">${M.ic("follow", 20)}<span>${following_ ? "Following" : "Follow"}</span></button>` : ""}<button class="hd-btn hd-btn--sm" type="button" data-act="center">${M.ic("center", 20)}Center</button><button class="hd-btn hd-btn--sm" type="button" data-copy="${s.lat.toFixed(5)}, ${s.lon.toFixed(5)}" data-copy-msg="Copied ${s.lat.toFixed(5)}, ${s.lon < 0 ? M.MINUS + Math.abs(s.lon).toFixed(5) : s.lon.toFixed(5)}">${M.ic("copy", 20)}Copy position</button></div>
      <div class="hd-detail__section">${K.qso(cells)}</div>
      ${tr && tr.length > 1 ? `<section class="hd-detail__section" aria-labelledby="hd-trk-h"><div class="hd-detail__h"><span id="hd-trk-h">Track</span><div class="hd-seg" role="group" aria-label="Track length">${[[1800, "30 min"], [3600, "1 h"], [10800, "3 h"]].map(([v, l]) => `<button class="hd-seg__btn" type="button" data-act="trk" data-w="${v}" aria-pressed="${v === w}">${l.replace(" ", NB)}</button>`).join("")}</div></div>
        <p class="hd-detail__note" style="margin-top:0">${trackSummary(tr)}</p>
        <table class="hd-fixes"><tbody>${fixRows(tr, s)}</tbody></table></section>` : ""}
      ${s.comment ? `<section class="hd-detail__section"><h3 class="hd-detail__h">Comment</h3><p class="hd-rptnotes">${esc(s.comment)}</p></section>` : ""}
      ${s.status ? `<section class="hd-detail__section"><h3 class="hd-detail__h">Status</h3><p class="hd-rptnotes">${esc(s.status)}</p></section>` : ""}
      ${s.mice_msg ? K.note(`Mic-E message: ${esc(s.mice_msg)}`) : ""}
      ${s.device ? K.note(`Radio: ${esc(s.device)}`) : ""}
      ${M.finite(s.freq) ? K.note(`Announces ${esc(M.freq(s.freq))}${NB}MHz`) : ""}
      ${weatherHtml(s.wx)}
      ${xrefHtml}
      <p class="hd-detail__source">Received from APRS-IS.${s.path ? ` Path: ${esc(String(s.path).replace(/,/g, ", "))}.` : ""}${url ? ` <a class="hd-link" href="${esc(url)}" target="_blank" rel="noopener">Open on aprs.fi</a>` : ""}</p>`;
  }
  /** The FCC licence cross-reference (§7.11): you and your neighbours are their red marks (a grid neighbour's ring
   *  dashed), with the class from your own licence or from the neighbour list; anyone else is their licence. A class
   *  the records do not have is said so, never guessed as "Club licence" (that is what an empty class means). */
  const classText = (cls) => (cls == null ? "Licence class not listed" : M.className(cls));
  function xrefRow(xr) {
    if (!xr) return "";
    let lead, title, sub;
    if (xr.who === "you") {
      const y = M.youPlace();
      lead = `<span style="display:grid;place-items:center;width:28px;height:28px;flex:none">${K.youDot(!!(y && y.approx), 18, false)}</span>`; // the 28 px slot of the other leads
      title = xr.loading ? esc(xr.callsign) : `${esc(xr.callsign)}: ${esc(classText(xr.license_class))}`;
      sub = "This is you";
    } else if (xr.who === "member") {
      lead = K.memberSvg(xr.m, 28);
      title = `${esc(xr.callsign)}: ${esc(classText(xr.m.license_class))}`;
      sub = "On hamdoor";
    } else {
      lead = K.classBadge(xr.license_class, xr.new);
      title = `${esc(xr.callsign)}: ${esc(classText(xr.license_class))}`;
      sub = `Licensed in ${[xr.city, xr.state].filter(Boolean).map(esc).join(", ")}`;
    }
    return `<section class="hd-detail__section"><h3 class="hd-detail__h">FCC licence</h3><button class="hd-xref" type="button" data-act="xref">${lead}<span class="hd-xref__t"><b>${title}</b><span>${sub}</span></span></button></section>`;
  }
  function aprsfiCard(it) {
    const d = it.d, url = HD.safeUrl(d.aprsfi_url);
    return `${K.head("", M.callHtml(d.call), "hd-call hd-detail__call", M.spell(d.call))}
      <p class="hd-detail__lede">Last position from aprs.fi</p><p class="hd-detail__sub">${d.last ? `Heard ${esc(M.ago(d.last))}` : ""}</p>
      <div class="hd-detail__section">${K.qso([["Grid", M.grid(d.lat, d.lon, 6)], ["Distance", M.distDir(d.lat, d.lon) || "Not known"]])}</div>
      ${d.comment ? `<section class="hd-detail__section"><h3 class="hd-detail__h">Comment</h3><p class="hd-rptnotes">${esc(d.comment)}</p></section>` : ""}
      <p class="hd-detail__source">data: aprs.fi${url ? ` <a class="hd-link" href="${esc(url)}" target="_blank" rel="noopener">Open on aprs.fi</a>` : ""}</p>`;
  }
  /** the station's whole track in the chosen window, its packets, and its FCC licence */
  function refreshStation(it) {
    const key = it.s.key, w = it.window || 3600;
    HD.api(`/api/aprs/station?key=${encodeURIComponent(key)}&window=${w}`).then((d) => {
      if (!d) return;
      it.detail = d; it.s = Object.assign({}, it.s, d, { track: it.s.track || d.track });
      selDetail = d;
      M.ui.refreshDetail(it);
    }, (e) => { if (e && e.status === 404 && it.partial) M.ui.refreshDetail(it); });
  }
  function cardLoad(it) {
    if (it.kind !== "aprs") return;
    selDetail = null;
    refreshStation(it);
    const base = String(it.s.call || "").replace(/-[0-9A-Z]{1,2}$/i, "").toUpperCase();
    if (isObject(it.s) || !/^(?=.*\d)[A-Z0-9]{3,7}$/.test(base)) return;
    const me = M.me();
    if (me && me.callsign === base) {
      // your own station: your class comes from your licence (the shell's cached /api/hams/{you})
      it.xref = { who: "you", callsign: base, loading: true };
      M.ui.refreshDetail(it);
      const done = (l) => { if (it.xref && it.xref.who === "you") { it.xref = { who: "you", callsign: base, license_class: l ? l.license_class : null }; M.ui.refreshDetail(it); } };
      Promise.resolve(HD.me && HD.me.licence ? HD.me.licence() : M.hamDetail(base)).then(done, () => done(null));
      return;
    }
    // a neighbour is their red card, never their licence at their house; only the neighbour list can tell (§3.1)
    M.whenNeighbours().then((known) => {
      if (!known || M.ui.selection() !== it) return;
      const n = M.neighbour(base);
      if (n) { it.xref = { who: "member", callsign: base, m: M.L("members").def.person(n) }; M.ui.refreshDetail(it); return; }
      if (M.hamDetail) M.hamDetail(base).then((h) => { if (h && h.callsign && !M.isPerson(h.callsign)) { it.xref = Object.assign({ who: "licence" }, h); M.ui.refreshDetail(it); } }, () => {});
    });
  }
  function onCardClick(e, it) {
    const a = e.target.closest("[data-act]");
    if (!a || it.kind !== "aprs") return false;
    const s = it.s, act = a.dataset.act;
    if (act === "follow") {
      if (following === s.key) { stopFollow("button"); return true; }
      following = s.key; followCall = s.call;
      a.setAttribute("aria-pressed", "true"); const t = a.querySelector("span"); if (t) t.textContent = "Following";
      M.ui.centerOn([s.lat, s.lon], Math.max(M.map.getZoom(), 13), true);
      M.toast(`Following ${s.call}. The map keeps it in view.`);
      return true;
    }
    if (act === "center") { M.ui.centerOn([s.lat, s.lon], Math.max(M.map.getZoom(), 13), true); return true; }
    if (act === "trk") { it.window = Number(a.dataset.w); refreshStation(it); for (const b of a.parentElement.querySelectorAll("[data-act=trk]")) b.setAttribute("aria-pressed", String(b === a)); return true; }
    if (act === "xref" && it.xref) {
      const x = it.xref, me = M.me(), kb = { keyboard: e.detail === 0 };
      // for you or a neighbour the red card opens, never their licence (§3.1)
      if (x.who === "you" || (me && x.callsign === me.callsign)) { M.ui.openDetail({ layer: "members", kind: "you" }, kb); return true; }
      const n = M.neighbour(x.callsign);
      if (n) { M.ui.openDetail({ layer: "members", kind: "member", m: M.L("members").def.person(n) }, kb); return true; }
      if (x.who === "licence" && M.finite(x.lat) && M.neighboursKnown() && !M.isPerson(x.callsign)) {
        const fi = M.L("fcc").def.fccItem({ callsign: x.callsign, name: x.name, city: x.city, state: x.state, license_class: x.license_class, expired: x.expired, new: x.new, granted: x.granted, registered: x.registered }, x.lat, x.lon, x.loc, x.zip);
        fi.detail = x;
        M.map.setView([x.lat, x.lon], Math.max(M.map.getZoom(), 15), { animate: false });
        M.ui.openDetail(fi, kb);
      }
      return true;
    }
    return false;
  }
  function onSelect(item) {
    if (following && !(item && item.layer === "aprs" && item.s && item.s.key === following)) stopFollow(item ? "select" : "close");
    if (!item || item.layer !== "aprs") selDetail = null;
  }

  /* ------------------------------------------------------------ rows, search, In view */
  function row(it) {
    if (it.kind !== "aprs") return null;
    const s = it.s, d = M.distFromHome(s.lat, s.lon);
    return { lead: `<span class="hd-mk-aprs" style="cursor:inherit">${K.spriteHtml(s.sym || CAT_SYM[s.cat])}</span>`, title: `<span class="hd-call hd-call--list">${label(s)}</span>`,
      meta: `${esc(noun(s))}${moving(s) ? `, ${mph(s.speed_kmh)}${NB}mph ${M.dir8(s.course)}` : ""}`, trail: d == null ? "" : M.mi(d), trail2: esc(M.ago(s.last)) };
  }
  function searchRow(it, q) {
    const s = it.s, d = M.distFromHome(s.lat, s.lon);
    return { lead: `<span class="hd-mk-aprs" style="cursor:inherit">${K.spriteHtml(s.sym || CAT_SYM[s.cat])}</span>`, title: `<span class="hd-call">${M.markMatch(s.call, q)}</span>`,
      meta: `${esc(noun(s))}, heard ${esc(M.ago(s.last))}`, trail: d == null ? "" : M.mi(d) };
  }
  async function search(q, signal) {
    const d = await HD.api(`/api/aprs/search?q=${encodeURIComponent(q)}&limit=3`, { signal });
    const known = new Map(((M.data("aprs") || {}).stations || []).map((s) => [s.key, s]));
    return ((d && d.results) || []).filter((r) => M.finite(r.lat)).map((r) => ({ layer: "aprs", kind: "aprs", partial: !known.has(r.key), s: known.get(r.key) || Object.assign({ kind: r.key.startsWith("obj:") || r.key.startsWith("item:") ? "object" : "station" }, r) }));
  }
  function inView(data, ctx) {
    if (!data || data.type !== "stations") return null;
    const b = M.screenBounds(), c = M.map.getCenter(), cp = { lat: c.lat, lon: c.lng };
    const ss = visible(data).filter((s) => b.contains([s.lat, s.lon]));
    if (!ss.length) return null;
    const near = ss.map((s) => ({ s, d: M.distMi(cp, s) })).sort((a, q) => a.d - q.d).slice(0, 20).map((x) => ({ layer: "aprs", kind: "aprs", s: x.s }));
    return { heading: "APRS stations", items: near, more: ss.length > 20 ? `${M.fmtN(ss.length - 20)} more in view.` : "" };
  }
  async function resolve(kind, key) {
    key = decodeURIComponent(key);
    const known = ((M.data("aprs") || {}).stations || []).find((s) => s.key === key);
    if (known) return { layer: "aprs", kind: "aprs", s: known };
    const d = await HD.api(`/api/aprs/station?key=${encodeURIComponent(key)}`).catch(() => null);
    return d && M.finite(d.lat) ? { layer: "aprs", kind: "aprs", s: d, detail: d } : null;
  }
  const fromLookup = (d) => ({ layer: "aprs", kind: "aprsfi", d, s: { key: "aprsfi:" + d.call, call: d.call, lat: d.lat, lon: d.lon } });
  const selId = (it) => (it.kind === "aprs" ? `aprs:${it.s.key}` : "");
  const latlng = (it) => (it.s && M.finite(it.s.lat) ? [it.s.lat, it.s.lon] : it.d ? [it.d.lat, it.d.lon] : null);
  const markKey = (it) => (it.s ? "ap:" + it.s.key : null);

  HD.map.addLayer({
    id: "aprs", order: 40, title: "APRS, live", minZoom: 9,
    feature: (f) => f.aprs !== false,
    defaults: { mobile: true, fixed: true, wx: true, digi: true, repeater: true, object: true, tracks: true, window: 3600 },
    cacheMs: 0, poll: 30000, pollWhen: (z) => z >= M.MINZ.aprs,
    filterKey: (p) => filterParams(p),
    load: (view, signal) => HD.api(`/api/aprs/map?${M.bboxQuery(view)}&${view.filterKey}`, { signal }),
    onData: liveOk, onError: liveFail,
    render, canvas, counts, section, syncSection, hint, card, cardLoad, onCardClick, onSelect, refreshSel, row, searchRow, search, inView, resolve, selId, latlng, markKey, fromLookup,
    // a selected station carries its own ring and an ink pill; a position looked up on aprs.fi has no mark of its own
    // (it is not in this server's feed), so the target ring marks where aprs.fi last heard it
    ring: (it) => (it.kind === "aprsfi" && M.finite(it.d.lat) && M.finite(it.d.lon) ? { latlng: [it.d.lat, it.d.lon], d: 30, el: null } : null),
    stopFollow,
    attribution: 'APRS symbols: <a href="https://github.com/hessu/aprs-symbols" target="_blank" rel="noopener">hessu/aprs-symbols</a>',
  });
})();
