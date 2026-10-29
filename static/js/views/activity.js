/* Activity (DESIGN_SYSTEM §7.12, copy §13): four tabs that fit a 390 px phone —
   Bands | Near me (Repeaters, APRS) | Spots (Parks, Summits, DX cluster) | My signal (PSK Reporter, WSPR, CW skimmers).
   Route #/activity/<tab> (?src= picks a source). Each tab remembers its source for the session. Activity never
   polls: data loads when a tab or source opens (if older than its rule allows) and on Refresh.
   Errors (§13, gap G11 shipped): no answer at all -> "Couldn't reach hamdoor"; 502 -> the upstream did not answer,
   and so does a 5xx that is not hamdoor's JSON (a proxy's page in its place); another 5xx -> hamdoor or the upstream
   may be busy. An empty list now really means nothing came back. */
(function () {
  "use strict";
  const HD = window.HD;
  const esc = HD.esc;
  const NB = HD.fmt.NBSP;
  const icon = (n, o) => HD.icon(n, o);
  const MIN = 60 * 1000;

  const TABS = [["bands", "Bands"], ["near", "Near me"], ["spots", "Spots"], ["signal", "My signal"]];
  const SOURCES = {
    near: [["rpt", "Repeaters"], ["aprs", "APRS"]],
    spots: [["pota", "Parks"], ["sota", "Summits"], ["dx", "DX cluster"]],
    signal: [["psk", "PSK Reporter"], ["wspr", "WSPR"], ["rbn", "CW skimmers"]],
  };
  // what each source is called in a sentence, and how fresh its cache must be
  const NAME = { bands: "hamqsl.com", rpt: "the repeater directory", aprs: "APRS-IS", pota: "pota.app", sota: "SOTAwatch",
    dx: "the DX cluster", psk: "PSK Reporter", wspr: "wspr.live", rbn: "the Reverse Beacon Network" };
  const MAX_AGE = { bands: 15 * MIN, rpt: Infinity, aprs: MIN, pota: 2 * MIN, sota: 2 * MIN, dx: 30000, psk: MIN, wspr: MIN, rbn: 30000 };
  const SAMPLED = { dx: true, rbn: true };

  let el = null;
  const st = {
    tab: "bands",
    src: { near: "rpt", spots: "pota", signal: "psk" },
    ctl: { bands: { "2m": true, "70cm": true, other: true }, aprsMi: null, potaNear: false, psk: "sent", rbnMine: false },
    data: new Map(), // request key -> {status, rows, raw, at, err}
    visible: false,
    scroll: 0,
    refreshing: null,
  };

  const me = () => HD.session.user || {};
  const myCall = () => (me().callsign || "").toUpperCase();
  const rangeMi = () => me().range_miles || 25;
  // the whole range in km for /api/repeaters/near, which counts up to 805 km (500 miles, the widest range)
  const rangeKm = () => Math.min(805, Math.round(rangeMi() * 1.609344));
  const features = () => (HD.config && HD.config.features) || {};
  const aprsMiles = () => {
    if (st.ctl.aprsMi) return st.ctl.aprsMi;
    const r = rangeMi();
    return [100, 50, 25, 10].find((v) => v <= r) || 10; // the largest option not above your range, at least 10
  };
  const utcNow = (at) => HD.fmt.utc(new Date(at));
  const plural = (n, one, many) => `${HD.fmt.num(n)}${NB}${n === 1 ? one : many}`;
  // no frequency (a SOTA spot's "" becomes 0 after Number()/1000) is unknown, never "0.000"
  const mhz = (v) => (v == null || v === "" || isNaN(v) || !(Number(v) > 0) ? "" : Number(v).toFixed(3));

  // ---------------------------------------------------------------- requests

  function request(src) {
    switch (src) {
      case "bands": return { key: "bands", url: "/api/activity/bands" };
      case "rpt": {
        const on = Object.keys(st.ctl.bands).filter((k) => st.ctl.bands[k]);
        const all = on.length === 3;
        const tokens = [];
        if (st.ctl.bands["2m"]) tokens.push("2m");
        if (st.ctl.bands["70cm"]) tokens.push("70cm");
        if (st.ctl.bands.other) tokens.push("10m", "6m", "1.25m", "33cm", "23cm", "other");
        const q = `radius_km=${rangeKm()}&limit=50` + (all ? "" : `&bands=${tokens.join(",")}`);
        return { key: "rpt:" + q, url: "/api/repeaters/near?" + q, none: !on.length };
      }
      case "aprs": {
        const km = Math.min(2000, Math.max(10, Math.round(aprsMiles() * 1.609)));
        return { key: "aprs:" + km, url: `/api/activity/aprs?radius_km=${km}` };
      }
      case "pota": return { key: "pota", url: "/api/activity/pota" };
      case "sota": return { key: "sota", url: "/api/activity/sota" };
      case "dx": return { key: "dx", url: "/api/activity/dxcluster" };
      case "psk": return { key: "psk:" + st.ctl.psk, url: `/api/activity/pskreporter?direction=${st.ctl.psk}` };
      case "wspr": return { key: "wspr", url: "/api/activity/wspr" };
      case "rbn": return { key: "rbn:" + st.ctl.rbnMine, url: `/api/activity/rbn?mine_only=${st.ctl.rbnMine}` };
      default: return null;
    }
  }
  const currentSrc = () => (st.tab === "bands" ? "bands" : st.src[st.tab]);

  async function fetchSrc(src, force) {
    const rq = request(src);
    if (!rq || rq.none) return;
    if (src === "rpt" && !features().repeaters) return;
    const d = st.data.get(rq.key) || { status: "idle", at: 0 };
    st.data.set(rq.key, d);
    if (d.status === "loading") return d.promise;
    if (!force && d.status === "ok" && Date.now() - d.at < MAX_AGE[src]) return;
    d.status = "loading";
    d.err = null;
    if (isShowing(src, rq.key)) renderBody();
    d.promise = HD.api(rq.url).then((raw) => {
      d.raw = raw;
      d.at = Date.now();
      d.status = "ok";
      const n = countOf(src, raw);
      if (isShowing(src, rq.key)) HD.announce(loadedText(src, n));
    }).catch((e) => {
      if (HD.isAbort(e)) return;
      d.status = "error";
      d.err = e;
    }).finally(() => {
      d.promise = null;
      if (isShowing(src, rq.key)) renderBody();
    });
    return d.promise;
  }
  const isShowing = (src, key) => st.visible && currentSrc() === src && request(src).key === key;

  function countOf(src, raw) {
    if (!raw) return 0;
    if (src === "rpt") return (raw.repeaters || []).length;
    if (src === "aprs") return (raw.stations || []).length;
    if (src === "bands") return (raw.bands || []).length;
    return (raw.spots || []).length;
  }
  function loadedText(src, n) {
    const what = { rpt: ["repeater", "repeaters"], aprs: ["station", "stations"], bands: ["band forecast", "band forecasts"] }[src] || ["report", "reports"];
    if (src === "pota" || src === "sota" || src === "dx") return `Loaded ${plural(n, "spot", "spots")} from ${NAME[src]}`;
    if (src === "bands") return "Loaded band conditions from hamqsl.com";
    return `Loaded ${plural(n, what[0], what[1])} from ${NAME[src]}`;
  }

  // ---------------------------------------------------------------- shell

  function skeleton() {
    el.innerHTML = `<header class="hd-topbar" data-tuck="top" data-topbar>
        <div class="hd-topbar__row"><h1 class="hd-topbar__title" id="activity-title" tabindex="-1">Activity</h1>
          <button class="hd-btn hd-btn--quiet hd-btn--sm" type="button" data-refresh>${icon("refresh", { size: 20 })}Refresh</button></div>
        <div class="hd-tabsbox" data-tabsbox><div class="hd-tabs" role="tablist" aria-label="Activity" data-tabs></div>${icon("left", { size: 20, cls: "hd-tabsbox__less" })}${icon("right", { size: 20, cls: "hd-tabsbox__more" })}</div>
      </header>
      <div class="hd-page"><div class="hd-page__col hd-page__col--wide hd-act__body" role="tabpanel" data-actbody></div></div>`;
    HD.chrome.watchTopbar(el.querySelector("[data-topbar]"));
    el.querySelector("[data-tabs]").addEventListener("scroll", syncCue, { passive: true });
  }

  function renderTabs() {
    const box = el.querySelector("[data-tabs]");
    // built once; choosing a tab updates them in place, so the tab you pressed keeps keyboard focus
    if (!box.firstElementChild) {
      box.innerHTML = TABS.map(([k, t]) => `<button class="hd-tabs__tab" type="button" role="tab" id="act-tab-${k}" aria-controls="act-panel" aria-selected="false" tabindex="-1" data-tab="${k}">${t.replace(" ", NB)}</button>`).join("");
    }
    for (const b of box.querySelectorAll("[data-tab]")) {
      const on = b.dataset.tab === st.tab;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
    }
    const body = el.querySelector("[data-actbody]");
    body.id = "act-panel";
    body.setAttribute("aria-labelledby", "act-tab-" + st.tab);
    requestAnimationFrame(() => {
      HD.revealInScroller(box, box.querySelector('[aria-selected="true"]'), 40); // clear of the overflow cue
      syncCue();
    });
  }
  /** Tabs that overflow show a fade and a chevron at the cut edge (§7.4): on the right while tabs are hidden there
   *  (data-more), on the left once the row has scrolled (data-less, e.g. "My signal" chosen at 320 px). */
  function syncCue() {
    const box = el && el.querySelector("[data-tabsbox]"), tabs = el && el.querySelector("[data-tabs]");
    if (!box || !tabs) return;
    const max = tabs.scrollWidth - tabs.clientWidth;
    box.dataset.more = String(max > 1 && tabs.scrollLeft < max - 1);
    box.dataset.less = String(max > 1 && tabs.scrollLeft > 1);
  }

  // ---------------------------------------------------------------- rows

  const spotRow = ({ f, mode, time, what, dist, note, href, label }) => {
    const tag = href ? "a" : "div";
    return `<${tag} class="hd-spot"${href ? ` href="${href}"` : ""}${label ? ` aria-label="${esc(label)}"` : ""}>` +
      `<span><span class="hd-spot__f">${esc(f)}</span>${mode ? `<span class="hd-spot__mode">${esc(mode)}</span>` : ""}</span>` +
      `<span class="hd-spot__time">${esc(time || "")}</span><span class="hd-spot__what">${what}</span><span class="hd-spot__dist">${esc(dist || "")}</span>` +
      (note ? `<span class="hd-spot__note">${note}</span>` : "") + `</${tag}>`;
  };
  /** "Show on map" for a reception report: the other station's grid-square centre, zoomed to the region (no card). */
  const placeHref = (lat, lon) => (lat != null && lon != null && !isNaN(lat) && !isNaN(lon) ? HD.router.href("map", { lat: Number(lat).toFixed(4), lon: Number(lon).toFixed(4), z: 8 }) : "");
  const callB = (c, isMe) => `<span class="hd-spot__call${isMe ? " hd-spot__call--member" : ""}">${esc(c)}</span>`;
  /** a reporter's locator as the 4-character square the spec prints ("EM12") */
  const grid4 = (g) => HD.geo.normGrid(String(g || "").slice(0, 4)) || String(g || "").toUpperCase().slice(0, 4);
  const dirFrom = (lat, lon) => { const u = me(); return u.lat != null && lat != null ? HD.fmt.compass(HD.geo.bearing(u, { lat, lon })) : ""; };

  // APRS: the station noun (§7.12 "Car, 68 mph SE" or the station noun), from the symbol, else the category
  const NOUN_P = { ">": "Car", j: "Jeep", k: "Pickup truck", u: "Truck", v: "Van", "[": "Person on foot", b: "Bicycle", "<": "Motorcycle",
    R: "RV", U: "Bus", a: "Ambulance", f: "Fire truck", P: "Police car", s: "Boat", Y: "Yacht", "'": "Small aircraft", "^": "Aircraft",
    X: "Helicopter", O: "Balloon", "=": "Train", "-": "Home station", "#": "Digipeater", "&": "Gateway", _: "Weather station",
    W: "Weather service site", h: "Hospital", o: "Emergency operations centre", "!": "Police station", d: "Fire department",
    c: "Incident command post", K: "School", r: "Repeater", m: "Repeater", n: "Node", y: "Home station with a yagi", "`": "Dish antenna",
    ";": "Campground", A: "Aid station", "+": "Red Cross", z: "Shelter", e: "Horse", p: "Rover", "/": "Waypoint" };
  const NOUN_CAT = { mobile: "Moving station", fixed: "Fixed station", wx: "Weather station", digi: "Digipeater", repeater: "Repeater object", object: "Object" };
  function aprsNoun(s) {
    const sym = s.sym || {};
    if (s.cat === "wx") return "Weather station";
    if (s.cat === "repeater" && s.kind !== "station") return "Repeater object";
    if (sym.code === "h" && s.cat === "digi") return "Digipeater at a hospital";
    if (sym.table === "/" && NOUN_P[sym.code]) return NOUN_P[sym.code];
    if (sym.code === "#" || sym.code === "&") return sym.code === "#" ? "Digipeater" : "Gateway";
    return NOUN_CAT[s.cat] || "Station";
  }
  function spriteHtml(sym) {
    const code = (sym && sym.code) || "/";
    const table = (sym && sym.table) || "/";
    const pos = (ch) => { const i = Math.max(0, Math.min(95, ch.charCodeAt(0) - 33)); return `${-(i % 16) * 24}px ${-Math.floor(i / 16) * 24}px`; };
    let h = `<span class="hd-mk-aprs__sym hd-mk-aprs__sym--${table === "/" ? "p" : "a"}" style="background-position:${pos(code)}"></span>`;
    const ovl = sym && sym.overlay ? sym.overlay : table !== "/" && table !== "\\" ? table : "";
    if (ovl) h += `<span class="hd-mk-aprs__ovl" style="background-position:${pos(ovl)}"></span>`;
    return `<span class="hd-mk-aprs" aria-hidden="true">${h}</span>`;
  }

  function rowsRpt(raw) {
    const list = raw.repeaters || [];
    // the meta line wraps rather than cutting off the mode, which decides whether you can use the machine; the
    // callsign and each mode never break ("D-STAR"), and the lead column fits a 220 cap (app.css)
    return `<ul class="hd-list hd-list--rpt">${list.map((r) => {
      const mi = r.distance_km != null ? r.distance_km / 1.609344 : null;
      const tone = HD.ui.rpt.tone(r);
      const where = r.city ? r.city + (r.location_quality && r.location_quality !== "site" ? " area" : "") : "";
      const nowrap = (t) => `<span class="hd-nowrap">${esc(t)}</span>`;
      const meta = [r.callsign && nowrap(r.callsign), where && esc(where), HD.ui.rpt.modes(r).split(", ").map(nowrap).join(", ")]
        .filter(Boolean).join(", ");
      // a machine at your own spot has no direction (the bearing of one point to itself is a made-up north)
      const dir = mi != null && mi >= HD.geo.SAME_SPOT_MI ? HD.fmt.compass(r.bearing_deg) : "";
      const href = HD.router.href("map", { lat: r.lat.toFixed(5), lon: r.lon.toFixed(5), z: 14, sel: "rpt:" + r.id });
      return `<li><a class="hd-row" href="${href}"><span class="hd-row__lead">${HD.ui.rpt.cap(r)}</span>
        <span class="hd-row__title"><span class="hd-row__f">${esc(HD.ui.rpt.line(r))}</span>${tone ? `<span class="hd-row__tone">${esc(tone)}</span>` : ""}</span>
        <span class="hd-row__meta">${meta}</span><span class="hd-row__trail">${esc(HD.fmt.mi(mi))}</span><span class="hd-row__trail2">${esc(dir)}</span></a></li>`;
    }).join("")}</ul>`;
  }
  function rowsAprs(raw) {
    const list = raw.stations || [];
    return `<ul class="hd-list">${list.map((s) => {
      const isObj = s.kind && s.kind !== "station";
      const mph = s.speed_kmh != null ? Math.round(s.speed_kmh * 0.6214) : 0;
      const moving = s.cat === "mobile" && mph > 0;
      const meta = aprsNoun(s) + (moving ? `, ${mph}${NB}mph${s.course != null ? " " + HD.fmt.compass(s.course) : ""}` : "");
      const href = HD.router.href("map", { lat: Number(s.lat).toFixed(5), lon: Number(s.lon).toFixed(5), z: 14, sel: "aprs:" + s.key });
      return `<li><a class="hd-row" href="${href}"><span class="hd-row__lead">${spriteHtml(s.sym)}</span>
        <span class="hd-row__title"><span class="hd-call hd-call--list">${isObj ? esc(s.call) : HD.ui.callHtml(s.call)}</span></span>
        <span class="hd-row__meta">${esc(meta)}</span><span class="hd-row__trail">${esc(HD.fmt.mi(s.distance_miles))}</span><span class="hd-row__trail2">${esc(HD.fmt.ago(s.last, { cap: true }))}</span></a></li>`;
    }).join("")}</ul>`;
  }
  function potaSota(src, raw) {
    let list = (raw.spots || []).slice().sort((a, b) => (a.distance_miles ?? 1e9) - (b.distance_miles ?? 1e9));
    if (st.ctl.potaNear) list = list.filter((s) => s.distance_miles != null && s.distance_miles <= rangeMi());
    return list;
  }
  function rowsSpots(src, list) {
    return list.map((s) => {
      const name = src === "pota" ? s.park_name : s.summit_name;
      const ref = src === "pota" ? s.park_ref : s.summit;
      // a reference ("US-1791", "W0C/PR-001") never breaks at its hyphen or slash
      const place = [name ? esc(name) : "", ref ? `<span class="hd-nowrap">${esc(ref)}</span>` : ""].filter(Boolean).join(", ");
      const has = s.lat != null && s.lon != null;
      const href = has ? HD.router.href("map", { lat: Number(s.lat).toFixed(5), lon: Number(s.lon).toFixed(5), z: 11, sel: `spot:${src}:${s.activator}` }) : "";
      return spotRow({ f: mhz(Number(s.frequency_khz) / 1000), mode: s.mode, time: HD.fmt.utc(s.time), what: `${callB(s.activator)}${place ? " at " + place : ""}`, dist: HD.fmt.mi(s.distance_miles), href });
    }).join("");
  }
  function rowsDx(raw) {
    return (raw.spots || []).map((s) => spotRow({ f: mhz(s.frequency_mhz != null ? s.frequency_mhz : s.frequency_khz / 1000), time: HD.fmt.utc(s.time),
      what: `${callB(s.spotter)} spotted ${callB(s.dx_callsign, s.dx_callsign === myCall())}`, note: s.comment ? `“${esc(s.comment.trim())}”` : "" })).join("");
  }
  function rowsPsk(raw) {
    const sent = (raw.direction || st.ctl.psk) === "sent";
    return (raw.spots || []).map((s) => {
      const lat = sent ? s.receiver_lat : s.sender_lat, lon = sent ? s.receiver_lon : s.sender_lon;
      return spotRow({
        f: mhz(s.frequency_hz / 1e6), mode: s.mode, time: HD.fmt.utc(s.time),
        what: sent ? `${callB(s.receiver_callsign)} heard you${s.receiver_grid ? " in " + esc(grid4(s.receiver_grid)) : ""}${s.snr != null ? " at " + HD.fmt.db(s.snr) : ""}`
          : `You heard ${callB(s.sender_callsign)}${s.sender_grid ? " in " + esc(grid4(s.sender_grid)) : ""}${s.snr != null ? " at " + HD.fmt.db(s.snr) : ""}`,
        dist: HD.fmt.mi(s.distance_miles), href: placeHref(lat, lon),
      });
    }).join("");
  }
  function rowsWspr(raw) {
    const mine = myCall();
    return (raw.spots || []).map((s) => {
      const tx = (s.tx_callsign || "").toUpperCase() === mine;
      const other = tx ? { c: s.rx_callsign, lat: s.rx_lat, lon: s.rx_lon } : { c: s.tx_callsign, lat: s.tx_lat, lon: s.tx_lon };
      const g = other.lat != null ? HD.geo.toGrid(other.lat, other.lon, 4) : "";
      const what = tx
        ? `${callB(other.c)}${g ? " in " + esc(g) : ""} heard your ${s.power_dbm != null ? HD.fmt.watts(s.power_dbm) + " " : ""}beacon${s.snr != null ? " at " + HD.fmt.db(s.snr) : ""}`
        : `You heard ${callB(other.c)}${g ? " in " + esc(g) : ""}${s.snr != null ? " at " + HD.fmt.db(s.snr) : ""}`;
      return spotRow({ f: mhz(s.frequency_hz / 1e6), mode: "WSPR", time: HD.fmt.utc(s.time), what, dist: HD.fmt.mi(s.distance_miles), href: placeHref(other.lat, other.lon) });
    }).join("");
  }
  function rowsRbn(raw) {
    const mine = myCall();
    return (raw.spots || []).map((s) => {
      const m = /^(\S+)\s+(-?\d+)\s*dB(?:\s+(\d+)\s*WPM)?/i.exec(s.comment || "");
      const mode = m ? m[1].toUpperCase() : "CW";
      const extra = m ? ` at ${HD.fmt.db(m[2])}${m[3] ? `, ${m[3]}${NB}wpm` : ""}` : "";
      return spotRow({ f: mhz(s.frequency_mhz != null ? s.frequency_mhz : s.frequency_khz / 1000), mode, time: HD.fmt.utc(s.time),
        what: `Skimmer ${callB(s.spotter)} heard ${callB(s.dx_callsign, s.dx_callsign === mine)}${extra}` });
    }).join("");
  }

  // ---------------------------------------------------------------- per-source copy

  function emptyCopy(src, raw) {
    const range = rangeMi(), call = esc(myCall());
    const rb = repeaterBookLink();
    switch (src) {
      case "rpt": return [`No repeaters listed within ${range}${NB}miles.`, `Widen your range in You${rb ? `, or ${rb}⁠` : ""}.`]; // U+2060 keeps the full stop with the icon
      case "aprs": {
        const mi = aprsMiles();
        return raw && raw.mode === "sample"
          ? [`No APRS stations heard within ${mi}${NB}miles during this 15-second sample.`, "Refresh to listen again."]
          : [`No APRS stations heard within ${mi}${NB}miles in the last hour.`, "Stations appear here as soon as they beacon."];
      }
      case "pota": return st.ctl.potaNear
        ? [`No park activations within ${range}${NB}miles came back from pota.app.`, "Activators come and go all day. Check back in a few minutes."]
        : ["No park activations came back from pota.app.", "Activators come and go all day. Check back in a few minutes."];
      case "sota": return st.ctl.potaNear
        ? [`No summit activations within ${range}${NB}miles came back from SOTAwatch for the last 2${NB}hours.`, "Summit activations cluster on weekends."]
        : [`No summit activations came back from SOTAwatch for the last 2${NB}hours.`, "Summit activations cluster on weekends."];
      case "dx": return ["No DX spots came back from the DX cluster.", "Refresh to listen again."];
      case "psk": return st.ctl.psk === "sent"
        ? [`No reports of ${call} came back from PSK Reporter for the last 24${NB}hours.`, "It lists digital modes such as FT8, FT4 and PSK31. Call CQ, then refresh."]
        : [`No reception reports by ${call} came back from PSK Reporter for the last 24${NB}hours.`, "They appear when your receiving software reports to PSK Reporter."];
      case "wspr": return [`No WSPR reports for ${call} came back for the last 24${NB}hours.`, "Run a WSPR beacon, then check back."];
      case "rbn": return st.ctl.rbnMine
        ? [`No skimmer reported ${call} during this 15-second sample.`, "Call CQ on CW, then refresh."]
        : ["No CW skimmer reports arrived during this 15-second sample.", "Refresh to listen again."];
      default: return ["Nothing came back.", ""];
    }
  }
  function statusText(src, raw, n) {
    const range = rangeMi();
    switch (src) {
      case "rpt": {
        // the request asks for the nearest 50; total counts every listed repeater in range
        const total = raw.total != null ? raw.total : n;
        return total > n ? `The nearest ${HD.fmt.num(n)} of ${plural(total, "repeater", "repeaters")} within ${range}${NB}miles.`
          : `${plural(n, "repeater", "repeaters")} within ${range}${NB}miles, nearest first.`;
      }
      case "aprs": return raw.mode === "sample"
        ? `${plural(n, "station", "stations")} within ${aprsMiles()}${NB}miles in a 15-second sample.`
        : `${plural(n, "station", "stations")} within ${aprsMiles()}${NB}miles, heard in the last hour.`;
      case "pota": return `${plural(n, "park activation", "park activations")} spotted${st.ctl.potaNear ? ` within ${range}${NB}miles` : ""}, nearest first.`;
      // SOTAwatch lists every spot of the last 2 hours, one per QSY: they are spots, not activations
      case "sota": return `${plural(n, "summit spot", "summit spots")}${st.ctl.potaNear ? ` within ${range}${NB}miles` : ""}, nearest first.`;
      case "dx": return `${plural(n, "spot", "spots")} from the DX cluster, newest first.`;
      case "psk": {
        const spots = raw.spots || [];
        if ((raw.direction || st.ctl.psk) === "sent") {
          const k = new Set(spots.map((s) => s.receiver_callsign)).size;
          return `${plural(k, "station", "stations")} heard ${esc(myCall())} in the last 24${NB}hours.`;
        }
        const k = new Set(spots.map((s) => s.sender_callsign)).size;
        return `You heard ${plural(k, "station", "stations")} in the last 24${NB}hours.`;
      }
      case "wspr": return `${plural(n, "report", "reports")} of your WSPR beacon in the last 24${NB}hours.`;
      case "rbn": return `${plural(n, "skimmer report", "skimmer reports")} in a 15-second live sample.`;
      default: return "";
    }
  }
  function sourceLine(src) {
    switch (src) {
      case "rpt": { const rb = repeaterBookLink("Search RepeaterBook near here"); return `From HearHam, with BrandMeister and IRLP details. Listings can be out of date, so confirm on the air.${rb ? " " + rb : ""}`; }
      case "aprs": return "Live from APRS-IS (aprs2.net), received by this hamdoor server.";
      case "pota": return "Spots from pota.app, refreshed every minute.";
      case "sota": return `Spots from SOTAwatch (sota.org.uk), last 2${NB}hours.`;
      case "dx": return `From a DX cluster (telnet): its latest spots, and those that arrive in 15${NB}seconds of listening when you open or refresh it.`;
      case "psk": return "Reception reports from pskreporter.info, refreshed every minute.";
      case "wspr": return `From wspr.live, last 24${NB}hours.`;
      case "rbn": return `From the Reverse Beacon Network (telnet), sampled for 15${NB}seconds when you open or refresh it.`;
      default: return "";
    }
  }
  function loadingText(src) {
    if (SAMPLED[src]) return `Listening to ${NAME[src]} for 15${NB}seconds. The first load takes a moment.`;
    if (src === "aprs" && !features().aprs) return "A 15-second sample from APRS-IS. The first load takes a moment.";
    return `Loading from ${NAME[src]}.`;
  }
  function errorHtml(src, err) {
    const s = err && err.status;
    // the source didn't answer: hamdoor's JSON 502 (G11), or a proxy's own page in its place — Cloudflare swaps that 502
    // for its HTML "Bad gateway", and answers 504 or 52x when a sample outlasts it. This view knows which source it asked.
    const noAnswer = s === 502 || (s > 500 && err.notJson);
    let title, body;
    if (!s) { title = "Couldn't reach hamdoor"; body = "Check your connection and try again."; }
    else if (src === "bands" && noAnswer) { title = "Couldn't load band conditions from hamqsl.com"; body = "Try again in a few minutes."; }
    else if (noAnswer) { title = `Couldn't load from ${NAME[src]}`; body = `${NAME[src].replace(/^the /, "The ")} didn't answer. Try again in a minute.`; }
    else if (s === 429) { title = "That's busy right now"; body = "Try again in a minute."; }
    else if (s >= 500) { title = `Couldn't load from ${NAME[src]}`; body = `hamdoor or ${NAME[src]} may be busy. Try again in a minute.`; }
    else { title = `Couldn't load from ${NAME[src]}`; body = esc(HD.errorText(err)); }
    return `<div class="hd-act__err">${HD.ui.alert(esc(title), esc(body))}<button class="hd-btn" type="button" data-retry>${icon("refresh", { size: 20 })}Try again</button></div>`;
  }

  let rbUrl = null, rbAsked = false;
  function repeaterBookLink(label) {
    if (!rbAsked) {
      rbAsked = true;
      HD.api("/api/repeaters/status").then((s) => { rbUrl = s && s.repeaterbook_near_url; if (st.visible && currentSrc() === "rpt") renderBody(); }).catch(() => {});
    }
    const u = me();
    if (!rbUrl || u.lat == null) return "";
    const href = HD.safeUrl(rbUrl.replace("{lat}", u.lat.toFixed(4)).replace("{lon}", u.lon.toFixed(4)));
    if (!href) return "";
    // the external-link icon sits after the label, inline (§7.2): the last word and the icon never part
    const words = String(label || "search RepeaterBook near here").split(" ");
    const last = words.pop();
    return `<a class="hd-link" href="${esc(href)}" target="_blank" rel="noopener">${words.length ? words.join(" ") + " " : ""}<span class="hd-nowrap">${last}${icon("external", { size: 16 })}</span></a>`;
  }

  // ---------------------------------------------------------------- controls

  function controls(src) {
    const sw = (on, label, hint, data) => `<div class="hd-switchrow"><span class="hd-switchrow__t"><span id="act-sw-${data}">${label}</span>${hint ? `<span class="hd-switchrow__hint" id="act-sw-${data}-h">${hint}</span>` : ""}</span>` +
      `<button class="hd-switch" type="button" role="switch" aria-checked="${on}" aria-labelledby="act-sw-${data}"${hint ? ` aria-describedby="act-sw-${data}-h"` : ""} data-ctl="${data}"></button></div>`;
    if (src === "rpt") {
      const cap = (k, n) => `<span class="hd-sw hd-sw--cap" style="--c:var(--rpt-${k});--ci:var(--rpt-${k}-ink)">${n}</span>`;
      return `<div class="hd-chips" role="group" aria-label="Bands">${[["2m", "2 m", "2"], ["70cm", "70 cm", "70"], ["other", "Other bands", "6"]].map(([k, l, n]) =>
        `<button class="hd-chip" type="button" aria-pressed="${st.ctl.bands[k]}" data-band="${k}">${cap(k, n)}<span class="hd-chip__label">${l.replace(" ", NB)}</span></button>`).join("")}</div>`;
    }
    if (src === "aprs") {
      return `<label class="hd-field__label" for="act-mi">Within</label><select class="hd-select hd-selectsm" id="act-mi" data-ctl="aprsmi">${[10, 25, 50, 100].map((v) =>
        `<option value="${v}"${v === aprsMiles() ? " selected" : ""}>${v}${NB}miles</option>`).join("")}</select>`;
    }
    if (src === "pota" || src === "sota") return sw(st.ctl.potaNear, `Only within my range (${rangeMi()}${NB}miles)`, "", "near");
    if (src === "psk") {
      return `<div class="hd-seg" role="group" aria-label="Reports">${[["sent", "Heard me"], ["received", "I heard"]].map(([k, l]) =>
        `<button class="hd-seg__btn" type="button" aria-pressed="${st.ctl.psk === k}" data-psk="${k}">${l}</button>`).join("")}</div>`;
    }
    if (src === "rbn") return sw(st.ctl.rbnMine, "Only reports of my signal", `Skimmers that heard ${esc(myCall())}`, "mine");
    return "";
  }

  // ---------------------------------------------------------------- body

  /**
   * The body in three parts, so a control keeps keyboard focus when it is used (§11): the source chips (rebuilt only
   * when the tab changes; choosing a source updates them in place), the controls row (rebuilt only when the source
   * changes; a toggle updates itself in place) and what the source shows (rebuilt every time, focus kept by
   * HD.keepFocus). `full` rebuilds everything (showing the view, when copy such as your range may have changed).
   */
  function renderBody(full) {
    if (!el) return;
    const body = el.querySelector("[data-actbody]");
    const src = currentSrc();
    let chipsBox = body.querySelector(":scope > [data-act-chips]");
    if (!chipsBox) {
      body.innerHTML = `<div data-act-chips></div><div data-act-ctl></div><div data-act-src></div>`;
      chipsBox = body.querySelector(":scope > [data-act-chips]");
    }
    const ctlBox = body.querySelector(":scope > [data-act-ctl]"), srcBox = body.querySelector(":scope > [data-act-src]");
    // what each part was built for lives in JS properties: data-tab / data-src attributes would answer the click handlers
    if (full || chipsBox._hdTab !== st.tab) {
      chipsBox.innerHTML = st.tab === "bands" ? "" : `<div class="hd-chips hd-chips--scroll hd-srcchips" role="group" aria-label="Source">${SOURCES[st.tab].map(([k, l]) =>
        `<button class="hd-chip hd-chip--choice" type="button" aria-pressed="${k === src}" data-src="${k}"><span class="hd-chip__label">${l.replace(" ", NB)}</span></button>`).join("")}</div>`;
      chipsBox._hdTab = st.tab;
    } else {
      for (const b of chipsBox.querySelectorAll("[data-src]")) b.setAttribute("aria-pressed", String(b.dataset.src === src));
    }
    if (full || ctlBox._hdSrc !== src) {
      const ctl = st.tab === "bands" ? null : controls(src);
      ctlBox.innerHTML = ctl == null ? "" : ctl ? `<div class="hd-ctlrow">${ctl}</div>` : `<div style="height:12px;border-bottom:1px solid var(--hd-line)"></div>`;
      ctlBox._hdSrc = src;
    }
    HD.keepFocus(srcBox, focusKey, () => { srcBox.innerHTML = sourceBody(src); });
    const chips = chipsBox.querySelector(".hd-srcchips");
    if (chips) HD.revealInScroller(chips, chips.querySelector('[aria-pressed="true"]'));
  }
  /** What has focus in a source's rows, as a selector the next render can find again (rows and links by href). */
  function focusKey(node) {
    if (node.id) return "#" + CSS.escape(node.id);
    if (node.hasAttribute("data-retry")) return "[data-retry]";
    if (node.matches("a[href]")) return `a[href="${CSS.escape(node.getAttribute("href"))}"]`;
    return null;
  }
  /** After Try again: focus on what came back (its status line), or on the new Try again when it failed again. */
  function focusSource() {
    const box = el.querySelector("[data-act-src]");
    if (!box) return;
    HD.focusTo(box.querySelector("[data-retry]") || box.querySelector(".hd-statusline, .hd-empty__title, .hd-readouts"));
  }

  function sourceBody(src) {
    if (src === "rpt" && !features().repeaters) {
      return `<div class="hd-act__empty">${HD.ui.empty({ icon: "activity", title: "This hamdoor server has no repeater directory." })}</div>`;
    }
    const rq = request(src);
    if (rq.none) return `<div class="hd-act__empty">${HD.ui.empty({ icon: "activity", title: "No bands chosen.", body: "Turn on 2 m, 70 cm or Other bands to list repeaters." })}</div>`;
    const d = st.data.get(rq.key);
    const needsPlace = src === "rpt" || src === "aprs";
    if (!d || (d.status === "loading" && !d.raw) || d.status === "idle") return HD.ui.statusLine("clock", loadingText(src));
    if (d.status === "error") {
      if (d.err && d.err.status === 400 && needsPlace) {
        return `<div class="hd-act__empty">${HD.ui.empty({ icon: "pin", title: "Add your grid square or address to see what's near you.", action: `<a class="hd-btn hd-btn--primary" href="#/you?focus=grid">Open You</a>` })}</div>`;
      }
      return errorHtml(src, d.err);
    }
    const raw = d.raw || {};
    if (src === "bands") return bandsBody(raw, d);
    let rows = "", n = 0;
    if (src === "rpt") { n = (raw.repeaters || []).length; rows = rowsRpt(raw); }
    else if (src === "aprs") { n = (raw.stations || []).length; rows = rowsAprs(raw); }
    else if (src === "pota" || src === "sota") { const list = potaSota(src, raw); n = list.length; rows = rowsSpots(src, list); }
    else if (src === "dx") { n = (raw.spots || []).length; rows = rowsDx(raw); }
    else if (src === "psk") { n = (raw.spots || []).length; rows = rowsPsk(raw); }
    else if (src === "wspr") { n = (raw.spots || []).length; rows = rowsWspr(raw); }
    else if (src === "rbn") { n = (raw.spots || []).length; rows = rowsRbn(raw); }
    const busy = d.status === "loading";
    if (!n) {
      const [t, b] = emptyCopy(src, raw);
      return `${busy ? HD.ui.statusLine("clock", loadingText(src)) : ""}<div class="hd-act__empty">${HD.ui.empty({ icon: "signal", title: t, body: b })}</div><p class="hd-source">${sourceLine(src)}</p>`;
    }
    const status = busy ? loadingText(src) : `${statusText(src, raw, n)} Updated ${utcNow(d.at)}.`;
    return `${HD.ui.statusLine(busy ? "clock" : "check", status)}${rows}<p class="hd-source">${sourceLine(src)}</p>`;
  }

  function bandsBody(b, d) {
    const busy = d.status === "loading";
    const rd = [["Solar flux", b.solar_flux], ["A index", b.a_index], ["K index", b.k_index], ["Sunspots", b.sunspots]]
      .filter(([, v]) => v !== "" && v != null).map(([k, v]) => `<span><span class="hd-readout__k">${k}</span><span class="hd-readout__v">${esc(v)}</span></span>`).join("");
    const wind = b.solar_wind !== "" && b.solar_wind != null && !isNaN(b.solar_wind)
      ? `<span><span class="hd-readout__k">Solar wind</span><span class="hd-readout__v">${Math.round(Number(b.solar_wind))}<small>${NB}km/s</small></span></span>` : "";
    const rows = HD.ui.bandRows(b);
    const m = /(\d{2})(\d{2})\s*GMT/.exec(b.updated || "");
    const last = m ? ` (last at ${m[1]}:${m[2]}${NB}UTC)` : "";
    return `${busy ? HD.ui.statusLine("clock", loadingText("bands")) : ""}<div class="hd-readouts">${rd}${wind}</div>
      ${rows.length ? `<table class="hd-outlook"><caption class="hd-sr">HF outlook by band</caption><thead><tr><th scope="col">HF outlook</th><th scope="col">Day</th><th scope="col">Night</th></tr></thead><tbody>${rows.map((r) =>
        `<tr><th scope="row">${esc(r.band)}</th><td>${HD.ui.cond(r.day)}</td><td>${HD.ui.cond(r.night)}</td></tr>`).join("")}</tbody></table>` : ""}
      <p class="hd-source">Solar data and the band forecast come from hamqsl.com, updated every 15${NB}minutes${last}. They describe the sky, not your antenna.</p>`;
  }

  // ---------------------------------------------------------------- events

  function setTab(tab, focus) {
    HD.router.go("activity", {}, { sub: [tab], replace: true });
    if (focus) requestAnimationFrame(() => { const t = el.querySelector(`#act-tab-${tab}`); if (t) t.focus(); });
  }
  function onClick(e) {
    const t = e.target;
    const tab = t.closest("[data-tab]");
    if (tab) { if (tab.dataset.tab !== st.tab) setTab(tab.dataset.tab); return; }
    const s = t.closest("[data-src]");
    if (s) {
      if (s.dataset.src === currentSrc()) return;
      st.src[st.tab] = s.dataset.src;
      renderBody();
      HD.announce(`Showing ${s.textContent.trim()}`);
      fetchSrc(s.dataset.src, false);
      return;
    }
    // the controls update themselves in place (they keep focus); only what the source shows is rebuilt
    const band = t.closest("[data-band]");
    if (band) {
      const k = band.dataset.band;
      st.ctl.bands[k] = !st.ctl.bands[k];
      band.setAttribute("aria-pressed", String(st.ctl.bands[k]));
      renderBody();
      fetchSrc("rpt", false);
      return;
    }
    const psk = t.closest("[data-psk]");
    if (psk) {
      if (psk.dataset.psk !== st.ctl.psk) {
        st.ctl.psk = psk.dataset.psk;
        for (const b of el.querySelectorAll("[data-psk]")) b.setAttribute("aria-pressed", String(b.dataset.psk === st.ctl.psk));
        renderBody();
        fetchSrc("psk", false);
      }
      return;
    }
    const c = t.closest("[data-ctl]");
    if (c && c.dataset.ctl === "near") { st.ctl.potaNear = !st.ctl.potaNear; c.setAttribute("aria-checked", String(st.ctl.potaNear)); renderBody(); return; }
    if (c && c.dataset.ctl === "mine") { st.ctl.rbnMine = !st.ctl.rbnMine; c.setAttribute("aria-checked", String(st.ctl.rbnMine)); renderBody(); fetchSrc("rbn", false); return; }
    if (t.closest("[data-retry]")) {
      // Try again goes away with the error: focus then lands on what came back, or on the new Try again
      const p = fetchSrc(currentSrc(), true);
      if (p && p.then) p.then(() => { if (st.visible && (document.activeElement === document.body || !document.activeElement.isConnected)) focusSource(); });
      return;
    }
    if (t.closest("[data-refresh]")) {
      const src = currentSrc();
      const p = fetchSrc(src, true);
      if (p && p.then) p.then(() => { const d = st.data.get(request(src).key); if (d && d.status === "ok" && st.visible) HD.toast("Activity refreshed"); });
    }
  }
  function onKey(e) {
    // roving focus across the tab row (ARIA tabs): arrows, Home, End
    const tab = e.target.closest && e.target.closest("[data-tab]");
    if (!tab) return;
    const keys = TABS.map(([k]) => k);
    let i = keys.indexOf(tab.dataset.tab);
    if (e.key === "ArrowRight") i = (i + 1) % keys.length;
    else if (e.key === "ArrowLeft") i = (i - 1 + keys.length) % keys.length;
    else if (e.key === "Home") i = 0;
    else if (e.key === "End") i = keys.length - 1;
    else return;
    e.preventDefault();
    setTab(keys[i], true);
  }

  HD.router.register("activity", {
    mount(section) {
      el = section;
      skeleton();
      el.addEventListener("click", onClick);
      el.addEventListener("keydown", onKey);
      el.addEventListener("change", (e) => {
        if (e.target.matches("[data-ctl=aprsmi]")) { st.ctl.aprsMi = Number(e.target.value); renderBody(); fetchSrc("aprs", false); }
      });
      window.addEventListener("resize", () => { if (st.visible) syncCue(); });
      HD.on("user", ({ user }) => { if (!user) st.data.clear(); });
    },
    show(params, sub) {
      const wasVisible = st.visible;
      st.visible = true;
      let tab = TABS.some(([k]) => k === sub[0]) ? sub[0] : null;
      if (!tab) {
        // #/activity with no tab (the tab bar, the rail): the tab you were on this session, and the URL says so
        // (§7.12 "the route remembers the tab", §6.5 Activity keeps its scroll position); ?src= names its own tab
        tab = (params.src && Object.keys(SOURCES).find((k) => SOURCES[k].some(([s]) => s === params.src))) || st.tab;
        HD.router.patch({}, { sub: [tab] });
      }
      const tabChanged = tab !== st.tab;
      st.tab = tab;
      if (params.src && SOURCES[tab] && SOURCES[tab].some(([k]) => k === params.src)) st.src[tab] = params.src;
      renderTabs();
      renderBody(true);
      fetchSrc(currentSrc(), false);
      if (!wasVisible || tabChanged) {
        // back from another view to the same tab: where you were; another tab: its top
        window.scrollTo(0, !wasVisible && !tabChanged ? st.scroll : 0);
        HD.chrome.listReset();
      }
    },
    hide() {
      st.scroll = window.scrollY;
      st.visible = false;
    },
  });
})();
