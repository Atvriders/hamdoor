/* hamdoor core — API client, escaping, formatting, storage, session, events,
   hash router and boot, plus the shell services every view and the map use:
   toasts, the announcer, dialogs, theme, geo and grid helpers, the shared
   neighbour list and the small HTML snippets of the design system (HD.ui).
   Classic script: everything hangs off window.HD, and every other script
   (views, map core, map layers) builds on this file. The same REST API
   serves the future mobile app. Spec: DESIGN_SYSTEM.md (§4 formats, §7.7
   toasts and dialogs, §10 theme, §13 copy, §14 API). */
(function () {
  "use strict";
  const HD = (window.HD = window.HD || {});

  // ---------- events (document-level CustomEvents named "hd:<name>") ----------

  HD.on = function (name, fn) {
    const handler = (e) => fn(e.detail);
    document.addEventListener("hd:" + name, handler);
    return () => document.removeEventListener("hd:" + name, handler);
  };
  HD.emit = function (name, detail) {
    document.dispatchEvent(new CustomEvent("hd:" + name, { detail }));
  };

  // ---------- storage (localStorage can throw or be empty: private mode, blocked data) ----------

  HD.store = {
    get(key, fallback = null) {
      try {
        const raw = localStorage.getItem(key);
        return raw == null ? fallback : JSON.parse(raw);
      } catch {
        return fallback;
      }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* best effort */ }
    },
    del(key) {
      try { localStorage.removeItem(key); } catch { /* best effort */ }
    },
  };

  // ---------- theme (§10): System (default), Light, Dark; a choice sets data-theme on <html> ----------

  const THEME_KEY = "hamdoor.theme";
  const darkMQ = window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : null;
  HD.theme = {
    /** "system" | "light" | "dark" */
    get() {
      const t = HD.store.get(THEME_KEY, "system");
      return t === "light" || t === "dark" ? t : "system";
    },
    set(t) {
      const v = t === "light" || t === "dark" ? t : "system";
      if (v === "system") HD.store.del(THEME_KEY); else HD.store.set(THEME_KEY, v);
      applyTheme();
      HD.emit("theme", { theme: v, dark: HD.theme.isDark() });
    },
    /** Is the chrome dark right now (a choice, or the system's when "system")? */
    isDark() {
      const t = HD.theme.get();
      return t === "dark" || (t === "system" && !!(darkMQ && darkMQ.matches));
    },
  };
  // index.html keys its two theme-color metas on prefers-color-scheme (right before any script runs, and for
  // "System"); a manual choice gives both the effective theme's surface colour, so the browser's toolbar and
  // status bar never show the OS scheme's colour above the other theme
  const themeMetas = Array.from(document.querySelectorAll('meta[name="theme-color"]')).map((m) => ({ m, content: m.getAttribute("content") }));
  function applyTheme() {
    const t = HD.theme.get();
    const root = document.documentElement;
    if (t === "system") delete root.dataset.theme;
    else root.dataset.theme = t;
    let surface = "";
    if (t !== "system") {
      try { surface = getComputedStyle(root).getPropertyValue("--hd-surface").trim(); } catch { surface = ""; }
      if (!/^#[0-9a-f]{3,8}$|^rgba?\(/i.test(surface)) surface = t === "dark" ? "#151d45" : "#ffffff";
    }
    for (const x of themeMetas) x.m.setAttribute("content", surface || x.content);
  }
  applyTheme(); // before the first paint of the shell
  if (darkMQ) {
    const onScheme = () => { if (HD.theme.get() === "system") HD.emit("theme", { theme: "system", dark: HD.theme.isDark() }); };
    if (darkMQ.addEventListener) darkMQ.addEventListener("change", onScheme); else if (darkMQ.addListener) darkMQ.addListener(onScheme);
  }

  // ---------- session ----------
  // Keys and formats match the previous frontend ("hamdoor_token" holds the raw
  // JWT string, "hamdoor_user" the JSON user), so an upgrade logs nobody out.

  function readToken() {
    try { return localStorage.getItem("hamdoor_token") || null; } catch { return null; }
  }

  HD.session = {
    token: readToken(),
    user: HD.store.get("hamdoor_user", null),
    set(token, user) {
      this.token = token || null;
      this.user = user || null;
      try {
        if (token) {
          localStorage.setItem("hamdoor_token", token);
          localStorage.setItem("hamdoor_user", JSON.stringify(user));
        } else {
          localStorage.removeItem("hamdoor_token");
          localStorage.removeItem("hamdoor_user");
        }
      } catch { /* best effort */ }
      HD.emit("user", { user: this.user });
    },
    setUser(user) {
      this.set(this.token, user);
    },
    /** End the session. Views hear "session-ending" first, while the token still works, and send what they owe the
     *  server (a delete waiting for its Undo toast, §14.5). reason: "logout" (default), "expired" (the server already
     *  refused the token), "deleted" (the account is gone). */
    clear(reason) {
      if (this.token) HD.emit("session-ending", { reason: reason || "logout" });
      this.set(null, null);
    },
    /** The server rejected our token: forget it and send the person to Log in. */
    expire() {
      if (!this.token) return;
      this.clear("expired");
      HD.emit("session-expired", {});
      HD.router.go("auth", { mode: "login", expired: "1" }, { replace: true });
    },
  };

  // ---------- API client ----------

  const NETWORK_TEXT = "Couldn't reach hamdoor. Check your connection and try again.";

  /**
   * JSON in, JSON out. Resolves null for 204. Rejects with an Error whose
   * .status is the HTTP status (0 for network failures) and whose .message is
   * readable text (pydantic validation lists are flattened). AbortError is
   * re-thrown untouched so callers can ignore superseded requests.
   * Extras: `timeout` (ms; a timeout rejects like a network failure, with
   * .timedOut) and `keepalive` (a request that outlives the page, §14.5).
   */
  HD.api = async function (path, { method = "GET", body = null, auth = true, signal = null, timeout = 0, keepalive = false } = {}) {
    const headers = {};
    if (body !== null) headers["Content-Type"] = "application/json";
    if (auth && HD.session.token) headers["Authorization"] = `Bearer ${HD.session.token}`;
    let ctl = null, timer = 0, timedOut = false;
    if (timeout > 0) {
      ctl = new AbortController();
      timer = setTimeout(() => { timedOut = true; ctl.abort(); }, timeout);
      if (signal) {
        if (signal.aborted) ctl.abort();
        else signal.addEventListener("abort", () => ctl.abort(), { once: true });
      }
    }
    let resp;
    try {
      resp = await fetch(path, {
        method,
        headers,
        body: body === null ? null : JSON.stringify(body),
        signal: ctl ? ctl.signal : signal,
        keepalive,
      });
    } catch (e) {
      clearTimeout(timer);
      if (e && e.name === "AbortError" && !timedOut) throw e;
      const err = new Error(NETWORK_TEXT);
      err.status = 0;
      if (timedOut) err.timedOut = true;
      throw err;
    }
    clearTimeout(timer);
    if (resp.status === 401 && auth) {
      HD.session.expire();
      const err = new Error("Your session ended. Log in again to carry on.");
      err.status = 401;
      throw err;
    }
    if (resp.status === 204) return null;
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const detail = data && data.detail;
      let msg;
      if (typeof detail === "string") {
        msg = detail;
      } else if (Array.isArray(detail)) {
        msg = detail.map((d) => {
          const field = (d.loc || []).filter((x) => x !== "body" && x !== "query").join(".") || "input";
          return `${field}: ${String(d.msg).replace(/^Value error, /, "")}`;
        }).join("; ");
      } else {
        msg = `Request failed (${resp.status})`;
      }
      const err = new Error(msg);
      err.status = resp.status;
      err.detail = detail;
      throw err;
    }
    return data;
  };

  /** True for errors from a superseded (aborted) request — callers just return. */
  HD.isAbort = (e) => !!e && (e.name === "AbortError" || e.code === 20);

  /** The §13 / §14.5 sentence for a failed request. `what` names the thing ("post"). */
  HD.errorText = function (err, what) {
    const s = err && err.status;
    if (s === 403 && what === "post") return "You can only change your own posts.";
    if (s === 404 && what === "post") return "This post was deleted";
    if (s === 429) return "That's busy right now. Try again in a minute.";
    if (s === 401) return "Your session ended. Log in again to carry on.";
    if (!s || s >= 500) return NETWORK_TEXT;
    return (err && err.message) || NETWORK_TEXT;
  };

  /** Field names a 422 complained about ("email", "password", ...). */
  HD.errorFields = function (err) {
    const d = err && err.detail;
    if (!Array.isArray(d)) return [];
    return d.map((x) => (x.loc || []).filter((k) => k !== "body" && k !== "query").join(".")).filter(Boolean);
  };

  // ---------- escaping ----------

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  /** Escape any value for HTML text or attribute context. All upstream text
   *  (FCC names, APRS comments, repeater notes, posts) is untrusted. */
  HD.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

  /** Only http(s) URLs may become links; anything else returns null. */
  HD.safeUrl = function (u) {
    try {
      const url = new URL(String(u), location.href);
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
    } catch {
      return null;
    }
  };

  // ---------- formatting (§4) ----------
  // Numbers and units are joined with a no-break space (U+00A0) so they never wrap apart.

  const NBSP = "\u00a0";
  const MINUS = "\u2212";
  const NNBSP = "\u202f"; // narrow no-break space: "146.940 −" never splits
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const MONTH = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const DAY = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const DIR8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const DIR8W = { N: "north", NE: "north-east", E: "east", SE: "south-east", S: "south", SW: "south-west", W: "west", NW: "north-west" };

  function toDate(t) {
    if (t == null || t === "") return null;
    if (t instanceof Date) return isNaN(t) ? null : t;
    if (typeof t === "number") return new Date(t < 1e12 ? t * 1000 : t);  // unix seconds or ms
    let s = String(t).trim();
    if (/^\d{4}-\d\d-\d\d \d\d:\d\d/.test(s)) s = s.replace(" ", "T"); // "2026-10-22 18:20:00" (wspr.live)
    // API datetimes are naive UTC ("2026-10-16T20:15:27"): treat them as UTC
    const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
    return isNaN(d) ? null : d;
  }
  /** A local date-time without a zone ("2026-10-28T20:00", an event time) as a local Date. */
  function localDate(s) {
    const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(String(s || ""));
    return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) : null;
  }
  const pad2 = (n) => String(n).padStart(2, "0");
  const clock12 = (d) => `${d.getHours() % 12 || 12}:${pad2(d.getMinutes())}${NBSP}${d.getHours() < 12 ? "am" : "pm"}`;
  const cap1 = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

  HD.fmt = {
    NBSP,
    MINUS,
    NNBSP,
    date: toDate,
    localDate,
    num(n, digits = 0) {
      if (n == null || n === "" || isNaN(n)) return "";
      return Number(n).toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: digits });
    },
    /** 1234 -> "1.2k", 296000 -> "296k" (map bubbles, chip counts) */
    compact(n) {
      if (n == null || isNaN(n)) return "";
      if (n < 1000) return String(Math.round(n));
      if (n < 10000) return (Math.round(n / 100) / 10).toString().replace(/\.0$/, "") + "k";
      if (n < 1e6) return Math.round(n / 1000) + "k";
      return (Math.round(n / 1e5) / 10).toString().replace(/\.0$/, "") + "M";
    },
    /** "0.7 mi", "19 mi"; {about: true}: whole miles (at least 1) after "about" — "about 3 mi" */
    mi(miles, opts) {
      if (miles == null || miles === "" || isNaN(miles)) return "";
      const v = Number(miles);
      if (opts && opts.about) return "about " + Math.max(1, Math.round(v)).toLocaleString("en-US") + NBSP + "mi";
      return (v < 10 ? v.toFixed(1) : Math.round(v).toLocaleString("en-US")) + NBSP + "mi";
    },
    /** "about 3 mi" */
    aboutMi(miles) { return HD.fmt.mi(miles, { about: true }); },
    /** distance plus an 8-point direction: "0.7 mi NE", "about 3 mi SW" */
    dist(miles, deg, opts) {
      const m = HD.fmt.mi(miles, opts);
      const c = HD.fmt.compass(deg);
      return m && c ? m + " " + c : m;
    },
    km(km) {
      if (km == null || isNaN(km)) return "";
      const v = Number(km);
      return (v < 10 ? v.toFixed(1) : Math.round(v).toLocaleString("en-US")) + NBSP + "km";
    },
    /** 146.94 -> "146.940" */
    mhz(f, digits = 3) {
      if (f == null || f === "" || isNaN(f)) return "";
      return Number(f).toFixed(digits);
    },
    /** A repeater output: 3 decimals, 4 when the fourth digit is not 0 ("927.6125") */
    freq(f) {
      if (f == null || f === "" || isNaN(f)) return "";
      let s = Number(f).toFixed(4);
      if (s.endsWith("0")) s = s.slice(0, -1);
      return s;
    },
    /** signed repeater offset in MHz -> "+", "−" or "" (simplex) */
    offsetSign(off) {
      if (off == null || isNaN(off) || Math.abs(off) < 1e-6) return "";
      return off > 0 ? "+" : MINUS;
    },
    /** dB with a true minus: "−12 dB", "18 dB" */
    db(n) {
      if (n == null || n === "" || isNaN(n)) return "";
      const v = Math.round(Number(n));
      return (v < 0 ? MINUS + Math.abs(v) : String(v)) + NBSP + "dB";
    },
    /** watts from dBm: "5 W", "0.2 W", whole watts from 1 W */
    watts(dbm) {
      if (dbm == null || dbm === "" || isNaN(dbm)) return "";
      const w = Math.pow(10, Number(dbm) / 10) / 1000;
      const s = w >= 1 ? String(Math.round(w)) : w >= 0.1 ? w.toFixed(1) : String(Number(w.toPrecision(1)));
      return s + NBSP + "W";
    },
    /** Relative time (§4): "just now", "4 min ago", "1 hour ago", "yesterday", "3 days ago",
     *  then "Oct 12" (this year) or "Oct 12, 2025". {cap: true} capitalises it to start a line. */
    ago(t, opts) {
      const d = toDate(t);
      if (!d) return "";
      const s = (Date.now() - d.getTime()) / 1000;
      let out;
      if (s < 60) out = "just now";
      else if (s < 3600) out = Math.floor(s / 60) + NBSP + "min ago";
      else if (s < 86400) { const h = Math.floor(s / 3600); out = h + NBSP + (h === 1 ? "hour" : "hours") + " ago"; }
      else if (s < 172800) out = "yesterday";
      else if (s < 7 * 86400) out = Math.floor(s / 86400) + NBSP + "days ago";
      else out = `${MON[d.getMonth()]}${NBSP}${d.getDate()}` + (d.getFullYear() === new Date().getFullYear() ? "" : `, ${d.getFullYear()}`);
      return opts && opts.cap ? cap1(out) : out;
    },
    dateTime(t) {
      const d = toDate(t);
      return d ? d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
    },
    time(t) {
      const d = toDate(t);
      return d ? d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }) : "";
    },
    /** Local 12-hour time for people: "3:52 pm" */
    clock(t) {
      const d = toDate(t);
      return d ? clock12(d) : "";
    },
    /** "20:15 UTC" — hams log in UTC. Takes a Date, unix time, an ISO string or a spot's "1823Z". */
    utc(t) {
      const s = typeof t === "string" ? t.trim() : "";
      const hm = /^(\d\d):?(\d\d)Z?$/i.exec(s);
      if (hm) return `${hm[1]}:${hm[2]}${NBSP}UTC`;
      const d = toDate(t);
      return d ? d.toISOString().slice(11, 16) + NBSP + "UTC" : "";
    },
    /** Callsigns are always upper case; the typeface slashes the zero. */
    call(cs) {
      return String(cs ?? "").toUpperCase();
    },
    /** plural(3, "post") -> "3 posts" (with a no-break space) */
    plural(n, word, pluralWord) {
      return HD.fmt.num(n) + NBSP + (n === 1 ? word : pluralWord || word + "s");
    },
    /** 0..360 degrees -> "N", "NE", ... */
    compass(deg) {
      if (deg == null || deg === "" || isNaN(deg)) return "";
      return DIR8[Math.round((((deg % 360) + 360) % 360) / 45) % 8];
    },
    /** 0..360 degrees -> "north-east" */
    compassWord(deg) {
      const c = HD.fmt.compass(deg);
      return c ? DIR8W[c] : "";
    },
    /** "140° SE"; {about: true} rounds to 10° after "about" */
    bearing(deg, opts) {
      if (deg == null || deg === "" || isNaN(deg)) return "";
      const d = ((Number(deg) % 360) + 360) % 360;
      if (opts && opts.about) return `about ${(Math.round(d / 10) * 10) % 360}°`;
      return `${Math.round(d) % 360}°${NBSP}${HD.fmt.compass(d)}`;
    },
    /** FCC "MM/DD/YYYY" -> "Sep 2026" ("" when missing) */
    monthYear(us) {
      const m = /^(\d\d)\/(\d\d)\/(\d{4})$/.exec(us || "");
      return m ? `${MON[+m[1] - 1]}${NBSP}${m[3]}` : "";
    },
    /** FCC "MM/DD/YYYY" -> "Jun 3, 2025" */
    longDate(us) {
      const m = /^(\d\d)\/(\d\d)\/(\d{4})$/.exec(us || "");
      return m ? `${MON[+m[1] - 1]}${NBSP}${+m[2]}, ${m[3]}` : "";
    },
    /** An event time (local "2026-10-28T20:00"): "Tuesdays, 8:00 pm" when weekly,
     *  else "Saturday, October 24, 9:00 am" (the year added when it is not this year). */
    event(eventAt, weekly) {
      const d = localDate(eventAt);
      if (!d) return "";
      if (weekly) return `${DAY[d.getDay()]}s, ${clock12(d)}`;
      const y = d.getFullYear() === new Date().getFullYear() ? "" : `, ${d.getFullYear()}`;
      return `${DAY[d.getDay()]}, ${MONTH[d.getMonth()]}${NBSP}${d.getDate()}${y}, ${clock12(d)}`;
    },
    /** "Tuesday 8:00 pm" — the next time an event happens, for short lines */
    eventShort(d) {
      return d ? `${DAY[d.getDay()]} ${clock12(d)}` : "";
    },
    cap: cap1,
  };

  // ---------- geo and Maidenhead grids (same math as app/geo.py and geocode.py) ----------

  const R_MI = 3958.8;
  const RAD = Math.PI / 180;
  HD.geo = {
    /** great-circle miles between {lat, lon} points */
    distMi(a, b) {
      if (!a || !b || a.lat == null || b.lat == null) return null;
      const dLat = (b.lat - a.lat) * RAD, dLon = (b.lon - a.lon) * RAD;
      const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * RAD) * Math.cos(b.lat * RAD) * Math.sin(dLon / 2) ** 2;
      return 2 * R_MI * Math.asin(Math.min(1, Math.sqrt(x)));
    },
    /** initial bearing in degrees from a to b */
    bearing(a, b) {
      if (!a || !b || a.lat == null || b.lat == null) return null;
      const y = Math.sin((b.lon - a.lon) * RAD) * Math.cos(b.lat * RAD);
      const x = Math.cos(a.lat * RAD) * Math.sin(b.lat * RAD) - Math.sin(a.lat * RAD) * Math.cos(b.lat * RAD) * Math.cos((b.lon - a.lon) * RAD);
      return (Math.atan2(y, x) / RAD + 360) % 360;
    },
    /** "em28PX" -> "EM28px"; "" unless it is a valid 4, 6 or 8 character locator */
    normGrid(g) {
      const s = String(g || "").trim();
      if (!/^[A-Ra-r]{2}\d{2}([A-Xa-x]{2}(\d{2})?)?$/.test(s)) return "";
      return s.slice(0, 4).toUpperCase() + s.slice(4, 6).toLowerCase() + s.slice(6);
    },
    /** centre {lat, lon} of a 4, 6 or 8 character locator, or null */
    gridCentre(g) {
      const s = HD.geo.normGrid(g).toUpperCase();
      if (!s) return null;
      let lon = (s.charCodeAt(0) - 65) * 20 - 180 + Number(s[2]) * 2;
      let lat = (s.charCodeAt(1) - 65) * 10 - 90 + Number(s[3]);
      if (s.length === 4) return { lat: lat + 0.5, lon: lon + 1 };
      lon += (s.charCodeAt(4) - 65) * (5 / 60);
      lat += (s.charCodeAt(5) - 65) * (2.5 / 60);
      if (s.length === 6) return { lat: lat + 2.5 / 120, lon: lon + 5 / 120 };
      lon += Number(s[6]) * (5 / 600);
      lat += Number(s[7]) * (2.5 / 600);
      return { lat: lat + 2.5 / 1200, lon: lon + 5 / 1200 };
    },
    /** the 4, 6 or 8 character locator of a point ("EM28px") */
    toGrid(lat, lon, length = 6) {
      if (lat == null || lon == null || isNaN(lat) || isNaN(lon)) return "";
      const per = { 4: 1, 6: 24, 8: 240 }[length] || 24;
      const x = Math.min(Math.floor((Math.min(Math.max(lon, -180), 180) + 180) / 2 * per), 180 * per - 1);
      const y = Math.min(Math.floor((Math.min(Math.max(lat, -90), 90) + 90) * per), 180 * per - 1);
      const sx = Math.floor(x / per), sy = Math.floor(y / per);
      let out = String.fromCharCode(65 + Math.floor(sx / 10)) + String.fromCharCode(65 + Math.floor(sy / 10)) + (sx % 10) + (sy % 10);
      if (length >= 6) {
        const unit = per / 24;
        out += String.fromCharCode(97 + (Math.floor(x / unit) % 24)) + String.fromCharCode(97 + (Math.floor(y / unit) % 24));
      }
      if (length === 8) out += String(x % 10) + String(y % 10);
      return out;
    },
    /** Is the point the centre of the locator in `grid` (its first 4 or 6, or all 8 characters)? (§3.2, §7.13) */
    isGridCentre(lat, lon, grid, lengths = [4, 6]) {
      if (lat == null || lon == null) return false;
      const g = HD.geo.normGrid(grid);
      if (!g) return false;
      for (const n of lengths) {
        if (g.length < n) continue;
        const c = HD.geo.gridCentre(g.slice(0, n));
        if (c && Math.abs(c.lat - lat) <= 1e-5 && Math.abs(c.lon - lon) <= 1e-5) return true;
      }
      return false;
    },
    /** The locator a member in grid precision is shown at (geocode.member_subsquare). */
    shownLocator(grid, lat, lon) {
      const g = HD.geo.normGrid(grid);
      const have = lat != null && lon != null;
      if (g.length >= 6) return g.slice(0, 6);
      if (g.length === 4) return have && HD.geo.toGrid(lat, lon, 4) === g ? HD.geo.toGrid(lat, lon, 6) : g;
      return have ? HD.geo.toGrid(lat, lon, 6) : "";
    },
  };

  // ---------- hash router ----------
  // URLs look like  #/map?lat=39.1&lon=-94.6&z=12&sel=aprs:N0ABC-9
  //                 #/feed/post/42      #/activity/near      #/you?focus=range      #/auth?mode=login
  // A view registers once: HD.router.register("feed", { mount(el), show(params, sub), hide() }).
  // <body> carries data-route (= the route), which the shared CSS keys on. Sections are
  // <section data-view="NAME"> (contract §1); lookups name the section so nothing else can match.

  const views = new Map();
  let current = null; // { name, params, sub }
  let lastHash = null;
  try { history.scrollRestoration = "manual"; } catch { /* old browsers */ }

  function parse(hash) {
    const h = String(hash || "").replace(/^#\/?/, "");
    const [pathPart, query = ""] = h.split("?");
    const parts = pathPart.split("/").filter(Boolean).map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
    const params = {};
    new URLSearchParams(query).forEach((v, k) => { params[k] = v; });
    return { name: parts[0] || "", sub: parts.slice(1), params };
  }

  function build(name, params = {}, sub = []) {
    const path = [name, ...sub].map(encodeURIComponent).join("/");
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") q.set(k, v); });
    const qs = q.toString();
    return "#/" + path + (qs ? "?" + qs : "");
  }

  HD.router = {
    DEFAULT: "map",
    register(name, view) {
      views.set(name, view);
      const el = document.querySelector(`section[data-view="${name}"]`);
      if (view.mount && el) view.mount(el);
    },
    has(name) {
      return views.has(name);
    },
    current() {
      return current;
    },
    href(name, params, sub) {
      return build(name, params, sub);
    },
    /** Navigate. opts.replace swaps the history entry (no new Back step). */
    go(name, params = {}, opts = {}) {
      const sub = opts.sub || [];
      const url = build(name, params, sub);
      if (opts.replace) history.replaceState(null, "", url);
      else if (location.hash !== url) history.pushState(null, "", url);
      route();
    },
    /** Update the current URL's params (and, with opts.sub, its sub-path) without re-showing the view (map pans). */
    patch(params, opts = {}) {
      if (!current) return;
      current.params = { ...current.params, ...params };
      if (opts.sub) current.sub = opts.sub;
      const url = build(current.name, current.params, opts.sub || current.sub);
      history.replaceState(null, "", url);
      lastHash = location.hash;
    },
  };

  function route() {
    let { name, sub, params } = parse(location.hash);
    lastHash = location.hash;
    if (!HD.session.token && name !== "auth") {
      name = "auth";
      sub = [];
      params = { mode: params.mode || "signup" };
    }
    if (!views.has(name) && !(name === HD.router.DEFAULT && HD.session.token)) name = HD.session.token ? HD.router.DEFAULT : "auth";
    const view = views.get(name);
    if (current && current.name !== name) {
      const prev = views.get(current.name);
      if (prev && prev.hide) {
        try { prev.hide(); } catch (e) { console.error("[hd] view failed to hide", current.name, e); }
      }
      const prevEl = document.querySelector(`section[data-view="${current.name}"]`);
      if (prevEl) prevEl.hidden = true;
    }
    const prevName = current ? current.name : null;
    const el = document.querySelector(`section[data-view="${name}"]`);
    if (el) el.hidden = false;
    document.body.dataset.route = name;
    current = { name, sub, params };
    HD.emit("route", { name, params, sub, prev: prevName });
    if (view && view.show) {
      try {
        view.show(params, sub);
      } catch (e) {
        console.error("[hd] view failed to show", name, e);
      }
    }
    HD.emit("view", { name, params, sub, prev: prevName });
  }

  // popstate and hashchange both fire for one Back: route once per URL
  const onNav = () => { if (location.hash !== lastHash) route(); };
  window.addEventListener("popstate", onNav);
  window.addEventListener("hashchange", onNav);
  HD._route = route;

  // ---------- toasts and the announcer (§7.7) ----------
  // The container #hd-toasts is the one polite live region (role="status");
  // toasts carry no role of their own. 5 s, 8 s with an action.

  const MAX_TOASTS = 3;
  /**
   * HD.toast(message, {kind: "info"|"ok"|"error", action: {label, onClick}, timeout, onClose(reason)})
   * Returns {close()}. onClose gets "timeout", "action" or "closed" (closed by code or replaced).
   */
  HD.toast = function (message, opts = {}) {
    const box = document.getElementById("hd-toasts");
    const handle = { close() {} };
    if (!box || !message) return handle;
    const kind = opts.kind || "info";
    const action = opts.action && opts.action.label ? opts.action : null;
    const el = document.createElement("div");
    el.className = "hd-toast" + (kind === "error" ? " hd-toast--error" : "");
    el.innerHTML = (kind === "error" && HD.icon ? HD.icon("alert", { size: 20 }) : "") +
      `<span class="hd-toast__text">${HD.esc(message)}</span>` +
      (action ? `<button class="hd-toast__action" type="button">${HD.esc(action.label)}</button>` : "");
    while (box.children.length >= MAX_TOASTS) {
      const old = box.firstElementChild;
      if (old && old._hdClose) old._hdClose("closed"); else if (old) old.remove();
    }
    box.appendChild(el);
    let closed = false, timer = 0, remaining = opts.timeout || (action ? 8000 : 5000), started = Date.now();
    let returnTo = null; // where focus was before it came into the toast (Tab from the page, a screen reader)
    el.addEventListener("focusin", (e) => { if (!returnTo && e.relatedTarget && !el.contains(e.relatedTarget)) returnTo = e.relatedTarget; });
    const close = (reason) => {
      if (closed) return;
      closed = true;
      clearTimeout(timer);
      // a toast that leaves with focus inside gives it back (an action that moved focus itself already did):
      // to where it came from, else the page, never to the top of the document
      const hadFocus = el.contains(document.activeElement);
      el.remove();
      if (hadFocus && !HD.focusTo(returnTo)) HD.focusTo(document.getElementById("hd-main"));
      if (opts.onClose) { try { opts.onClose(reason); } catch (e) { console.error(e); } }
    };
    const arm = () => { started = Date.now(); timer = setTimeout(() => close("timeout"), remaining); };
    // a toast someone is reading or about to press stays put (WCAG 2.2.1)
    const pause = () => { if (!timer) return; clearTimeout(timer); timer = 0; remaining = Math.max(1500, remaining - (Date.now() - started)); };
    const resume = () => { if (!timer && !closed) arm(); };
    el.addEventListener("pointerenter", pause);
    el.addEventListener("pointerleave", resume);
    el.addEventListener("focusin", pause);
    el.addEventListener("focusout", resume);
    if (action) {
      el.querySelector(".hd-toast__action").addEventListener("click", () => {
        try { action.onClick && action.onClick(); } finally { close("action"); }
      });
    }
    el._hdClose = close;
    arm();
    handle.close = () => close("closed");
    handle.el = el;
    return handle;
  };

  let announceTimer = 0;
  /** A quiet announcement for screen readers (filter results, loaded lists, state changes).
   *  It clears, then sets the text 60 ms later, so a repeat is announced again. */
  HD.announce = function (text) {
    const a = document.getElementById("hd-announce");
    if (!a) return;
    a.textContent = "";
    clearTimeout(announceTimer);
    announceTimer = setTimeout(() => { a.textContent = String(text || ""); }, 60);
  };

  // ---------- dialogs (§7.7: <dialog class="hd-dialog">, 12 px radius, title 20 px 800, actions right) ----------

  let dialogSeq = 0;
  HD.dialog = {
    /** Open a modal <dialog> built from `html`. It is removed when it closes; onClose(returnValue). */
    open({ html, className = "hd-dialog", label, labelledBy, onClose, onCancel } = {}) {
      const d = document.createElement("dialog");
      d.className = className;
      if (labelledBy) d.setAttribute("aria-labelledby", labelledBy);
      else if (label) d.setAttribute("aria-label", label);
      d.innerHTML = html;
      document.body.appendChild(d);
      d.addEventListener("cancel", (e) => { if (onCancel) { e.preventDefault(); onCancel(e); } });
      d.addEventListener("close", () => {
        if (onClose) { try { onClose(d.returnValue); } catch (e) { console.error(e); } }
        d.remove();
      });
      try { d.showModal(); } catch { d.setAttribute("open", ""); }
      return d;
    },
    /** A yes/no question. Resolves true when the confirm button was pressed. */
    confirm({ title, body = "", confirm = "OK", cancel = "Cancel", danger = false } = {}) {
      const id = "hd-dlg-" + ++dialogSeq;
      return new Promise((resolve) => {
        const d = HD.dialog.open({
          labelledBy: id,
          html: `<form method="dialog"><h2 class="hd-dialog__title" id="${id}">${HD.esc(title)}</h2>` +
            (body ? `<p class="hd-dialog__body">${HD.esc(body)}</p>` : "") +
            `<div class="hd-dialog__actions"><button class="hd-btn hd-btn--quiet" value="cancel" data-autofocus>${HD.esc(cancel)}</button>` +
            `<button class="hd-btn ${danger ? "hd-btn--danger" : "hd-btn--primary"}" value="ok">${HD.esc(confirm)}</button></div></form>`,
          onClose: (v) => resolve(v === "ok"),
        });
        const first = d.querySelector("[data-autofocus]");
        if (first) first.focus();
      });
    },
  };

  // ---------- shared HTML snippets of the design system ----------

  const CLASS_NAME = { E: "Amateur Extra", A: "Advanced", G: "General", T: "Technician", P: "Technician Plus", N: "Novice", "": "Club licence" };
  const CLASS_SHORT = { E: "Extra", A: "Advanced", G: "General", T: "Technician", P: "Technician Plus", N: "Novice", "": "Club" };
  const CATEGORIES = [
    { id: "general", label: "General", icon: "general" },
    { id: "for-sale", label: "For sale", icon: "tag" },
    { id: "nets-events", label: "Nets and events", icon: "calendar" },
    { id: "help-wanted", label: "Help wanted", icon: "help" },
    { id: "elmer", label: "Elmer mentoring", icon: "elmer" },
  ];
  const CAT_BY = Object.fromEntries(CATEGORIES.map((c) => [c.id, c]));
  const BAND_NUM = { "10m": "10", "6m": "6", "2m": "2", "1.25m": "220", "70cm": "70", "33cm": "33", "23cm": "23" };
  // the codes the API can return: the 50 states, DC and the territories (§9.8)
  const STATES = { AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado", CT: "Connecticut",
    DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois",
    IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts",
    MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada",
    NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota",
    OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota",
    TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia",
    WI: "Wisconsin", WY: "Wyoming", PR: "Puerto Rico", GU: "Guam", VI: "U.S. Virgin Islands", MP: "Northern Mariana Islands",
    AS: "American Samoa" };
  const SPOKEN = { 0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", "-": "dash", "/": "stroke" };

  function starPath(r, k) {
    let d = "";
    for (let i = 0; i < 10; i++) {
      const a = -Math.PI / 2 + (i * Math.PI) / 5, rr = i % 2 ? r * k : r;
      d += (i ? "L" : "M") + (rr * Math.cos(a)).toFixed(2) + " " + (rr * Math.sin(a)).toFixed(2);
    }
    return d + "Z";
  }

  HD.ui = {
    CLASS_NAME,
    CLASS_SHORT,
    CATEGORIES,
    category: (id) => CAT_BY[id] || CAT_BY.general,
    /** class colour group: the API's buckets (Technician Plus rides with Novice in Other) */
    classGroup: (cls) => (cls === "T" ? "t" : cls === "G" ? "g" : cls === "E" ? "e" : "o"),
    classLetter: (cls) => ({ T: "T", P: "T", G: "G", E: "E", A: "A", N: "N" }[cls] || "C"),
    className: (cls) => (cls in CLASS_NAME ? CLASS_NAME[cls] : "Not listed"),
    classShort: (cls) => (cls in CLASS_SHORT ? CLASS_SHORT[cls] : "Not listed"),
    /** "KS" -> "Kansas" (unknown codes print as given) */
    stateName: (code) => STATES[String(code || "").toUpperCase()] || String(code || ""),
    /** "Overland Park, Kansas" */
    place: (city, state) => [city, STATES[String(state || "").toUpperCase()] || state].filter(Boolean).join(", "),
    /** The two Map privacy radio cards (§7.13), shared by Sign up and You. `locator` is where a grid choice puts the pin. */
    privacyRadios(name, locator, checked) {
      const area = locator && locator.length === 4 ? `about 100 by 70${NBSP}miles` : `about 4 by 3${NBSP}miles`;
      const where = locator ? `the centre of grid ${HD.esc(locator)}` : "the centre of your grid square";
      return `<fieldset class="hd-radios"><legend>Show me on the map at</legend>` +
        `<label class="hd-radio"><input class="hd-radio__input" type="radio" name="${name}" value="address"${checked !== "grid" ? " checked" : ""}><span class="hd-radio__label">My licence address <span class="hd-muted" style="font-weight:500">(default)</span></span><span class="hd-radio__hint">Your pin goes at the address on your FCC licence, which is public record, or at the address in your profile if you changed it. Neighbours never see an address written out.</span></label>` +
        `<label class="hd-radio"><input class="hd-radio__input" type="radio" name="${name}" value="grid"${checked === "grid" ? " checked" : ""}><span class="hd-radio__label">My grid square</span><span class="hd-radio__hint">Your pin goes at ${where}, an area ${area}, not at your house. Distances to you are rounded to whole miles. On the map, hams farther away see only your licence, like any other. Your FCC record, with its address, stays public as always.</span></label>` +
        `</fieldset>`;
    },
    /** Class badge (§7.5): a ring in the class colour around the printed letter; a star outline when newly licensed.
     *  `cls` null/undefined = unknown (a member without an FCC row): an empty Other ring. */
    classBadge(cls, isNew) {
      if (cls == null) return '<span class="hd-class hd-class--o" title="Licence class not listed"></span>';
      const g = HD.ui.classGroup(cls), letter = HD.ui.classLetter(cls), name = HD.esc(HD.ui.classShort(cls));
      if (isNew) {
        return `<span class="hd-class hd-class--${g} hd-class--new" title="Newly licensed ${name}"><svg viewBox="-16 -16 32 32" aria-hidden="true"><path d="${starPath(15, 0.56)}" fill="var(--hd-surface)" stroke="var(--c)" stroke-width="3" stroke-linejoin="round"/></svg><span>${letter}</span></span>`;
      }
      return `<span class="hd-class hd-class--${g}" title="${name}">${letter}</span>`;
    },
    /** A callsign with its APRS SSID set in 500: KG0M<span>-7</span> */
    callHtml(call) {
      const c = String(call || "");
      const m = /^([A-Z0-9/]+)(-[0-9A-Z]{1,2})$/i.exec(c);
      return m ? `${HD.esc(m[1])}<span class="hd-call__ssid">${HD.esc(m[2])}</span>` : HD.esc(c);
    },
    /** "K G zero M dash 7" — how a screen reader should say a callsign heading */
    spokenCall(call) {
      return String(call || "").toUpperCase().split("").map((ch) => SPOKEN[ch] || ch).join(" ");
    },
    /** The ruled QSO box: cells [[label, valueHtml, wide?]] */
    qso(cells, three) {
      return `<div class="hd-qso${three ? " hd-qso--3" : ""}">${cells.map(([k, v, wide]) =>
        `<div class="hd-qso__cell${wide ? " hd-qso__cell--wide" : ""}"><span class="hd-qso__k">${HD.esc(k)}</span><span class="hd-qso__v">${v}</span></div>`).join("")}</div>`;
    },
    /** A status line (loading, counts): icon + text, never a spinner */
    statusLine(iconName, html) {
      return `<p class="hd-statusline">${HD.icon ? HD.icon(iconName, { size: 20 }) : ""}<span>${html}</span></p>`;
    },
    /** Empty state: 32 px icon, a 20 px sentence, one line of help, at most one button (html) */
    empty({ icon, title, body, action } = {}) {
      return `<div class="hd-empty">${icon && HD.icon ? HD.icon(icon, { size: 32, cls: "hd-empty__icon" }) : ""}` +
        `<h2 class="hd-empty__title">${title}</h2>${body ? `<p class="hd-empty__body">${body}</p>` : ""}${action || ""}</div>`;
    },
    /** Error alert: 2 px red border, alert icon, red title, ink body with the fix */
    alert(title, body) {
      return `<div class="hd-alert" role="alert">${HD.icon ? HD.icon("alert", { size: 24, cls: "hd-alert__icon" }) : ""}` +
        `<p class="hd-alert__title">${title}</p>${body ? `<p class="hd-alert__body">${body}</p>` : ""}</div>`;
    },
    /** A post's category glyph in a small ink speech bubble (lists, the aside, the Nearby wall) */
    postBadge(category) {
      const c = HD.ui.category(category);
      return `<svg class="hd-postbadge" viewBox="0 0 20 20" width="20" height="20" aria-hidden="true" focusable="false"><path d="M3.6 1.5h12.8a2.1 2.1 0 0 1 2.1 2.1v8.3a2.1 2.1 0 0 1-2.1 2.1h-5.6l-3.9 3.6v-3.6H3.6a2.1 2.1 0 0 1-2.1-2.1V3.6a2.1 2.1 0 0 1 2.1-2.1Z" fill="var(--hd-ink)"/><svg x="5" y="3" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="var(--hd-on-ink)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">${HD.icon ? HD.icon(c.icon, { size: 24 }).replace(/^<svg[^>]*>|<\/svg>$/g, "") : ""}</svg></svg>`;
    },
    /** hamqsl's bands ("80m-40m", day and night rows) -> [{band: "80–40 m", day, night}] in feed order */
    bandRows(b) {
      const order = [], by = {};
      for (const r of (b && b.bands) || []) {
        if (!by[r.band]) {
          const m = /^(\d+)m-(\d+)m$/.exec(r.band || "");
          by[r.band] = { band: m ? `${m[1]}–${m[2]}${NBSP}m` : String(r.band || "") };
          order.push(r.band);
        }
        by[r.band][r.time === "night" ? "night" : "day"] = r.condition || "";
      }
      return order.map((k) => by[k]);
    },
    /** A band condition: three bars and the word (Good, Fair, Poor); colour is never alone */
    cond(c) {
      const w = String(c || "").trim();
      if (!w) return "";
      const k = w.toLowerCase();
      return `<span class="hd-cond hd-cond--${/^(good|fair|poor)$/.test(k) ? k : "poor"}"><span class="hd-cond__bars" aria-hidden="true"><i></i><i></i><i></i></span>${HD.esc(w)}</span>`;
    },
    /** Repeater helpers: band key, cap number, "146.910 −", tone text, mode text */
    rpt: {
      bandKey: (band) => (band === "2m" ? "2m" : band === "70cm" ? "70cm" : "other"),
      bandNum: (band) => BAND_NUM[band] || String(band || "").replace(/m$/, "") || "?",
      line(r) {
        const sign = HD.fmt.offsetSign(r.offset_mhz);
        return HD.fmt.freq(r.output_mhz) + (sign ? NNBSP + sign : "");
      },
      tone(r) {
        if (r.tone_up) return "T" + Number(r.tone_up).toFixed(1);
        if (r.dcs) return "D" + String(r.dcs).padStart(3, "0");
        if (r.dmr_color_code != null) return "CC" + r.dmr_color_code;
        if (r.p25_nac) return "NAC" + r.p25_nac;
        return "";
      },
      modes(r) {
        const names = { FM: "FM", DMR: "DMR", YSF: "Fusion", DSTAR: "D-STAR", P25: "P25", NXDN: "NXDN", M17: "M17", ATV: "ATV" };
        const list = (r.modes || []).map((m) => names[m] || m);
        return list.length ? list.join(", ") : "FM";
      },
      cap(r, cls) {
        const k = HD.ui.rpt.bandKey(r.band);
        return `<span class="hd-mk-rpt__cap hd-mk-rpt__cap--${k}${cls ? " " + cls : ""}">${HD.esc(HD.ui.rpt.bandNum(r.band))}</span>`;
      },
    },
  };

  // ---------- people: the shared neighbour list and "where is my pin" ----------

  /** First name = the first word of `name` (FCC names arrive title-cased; print them as they come). */
  HD.firstName = (name) => String(name || "").trim().split(/\s+/)[0] || "";

  /**
   * The neighbour list (/api/operators/nearby, your range, nearest first), shared with the map:
   * HD.map.neighbours() when the map provides it, else one cached request of our own.
   * opts.maxAge (ms) asks for a list no older than that.
   */
  const nbCache = { at: 0, promise: null, key: "" };
  HD.neighbours = function (opts = {}) {
    if (HD.map && typeof HD.map.neighbours === "function") return Promise.resolve(HD.map.neighbours(opts));
    const maxAge = opts.maxAge == null ? Infinity : opts.maxAge;
    if (nbCache.promise && Date.now() - nbCache.at <= maxAge) return nbCache.promise;
    nbCache.at = Date.now();
    const p = HD.api("/api/operators/nearby").then((list) => list || []);
    nbCache.promise = p;
    p.catch(() => { if (nbCache.promise === p) { nbCache.promise = null; nbCache.at = 0; } });
    return p;
  };
  /** Is a neighbour's mark approximate (dashed ring, "about" and whole miles, §3.2)? */
  HD.isApprox = (n) => !!n && (n.map_precision === "grid" || HD.geo.isGridCentre(n.lat, n.lon, n.grid));

  const meCache = { call: "", promise: null };
  HD.me = {
    /** Your licence (/api/hams/{you}): class, grant and expiry dates, its point; null when FCC records lack it. */
    licence(opts = {}) {
      const u = HD.session.user;
      if (!u || !u.callsign) return Promise.resolve(null);
      if (!opts.force && meCache.promise && meCache.call === u.callsign) return meCache.promise;
      meCache.call = u.callsign;
      meCache.promise = HD.api("/api/hams/" + encodeURIComponent(u.callsign)).catch((e) => {
        if (e.status === 404) return null;
        meCache.promise = null;
        throw e;
      });
      return meCache.promise;
    },
    /** Where your mark sits and why (§7.13, §13), from location_source (G1d) with the five-case test
     *  as the fallback for accounts placed before it existed. Returns {kind, phrase, reason, locator}
     *  or {kind: "none"} without a location. `licence` is only needed for that fallback. */
    pin(user, licence) {
      const u = user || HD.session.user;
      if (!u) return { kind: "none" };
      if (u.map_precision === "grid") {
        const loc = HD.geo.shownLocator(u.grid, u.lat, u.lon);
        if (!loc) return { kind: "none" };
        return { kind: "grid", locator: loc, phrase: `at the centre of grid ${loc}`, reason: ", not at your house." };
      }
      if (u.lat == null || u.lon == null) return { kind: "none" };
      const licenceCase = { kind: "licence", phrase: "at your FCC licence address", reason: ", which is public record." };
      const zipCase = { kind: "zip", phrase: "at the centre of your ZIP code area", reason: ", because your licence has no street address we could match." };
      const profileCase = { kind: "profile", phrase: "at the address in your profile", reason: "." };
      const gridFallback = () => {
        const g = HD.geo.normGrid(u.grid) || HD.geo.toGrid(u.lat, u.lon, 6);
        return { kind: "gridfallback", locator: g, phrase: `at the centre of grid ${g}`, reason: ", because we couldn't place your address." };
      };
      switch (u.location_source) {
        case "licence":
        case "provider": return licenceCase; // the provider's coordinates are the licence address, tens of metres off
        case "zip": return zipCase;
        case "grid": return gridFallback();
        case "geocoded": return profileCase;
        default: break;
      }
      const lic = licence;
      if (lic && lic.lat != null && lic.loc === "address" && HD.geo.distMi(u, lic) * 1609.344 <= 150) return licenceCase;
      if (lic && lic.lat != null && lic.loc === "zip" && Math.abs(lic.lat - u.lat) <= 1e-5 && Math.abs(lic.lon - u.lon) <= 1e-5) return zipCase;
      if (HD.geo.isGridCentre(u.lat, u.lon, u.grid, [4, 6, 8])) return gridFallback();
      return profileCase;
    },
    /** HD.me.pin() once the licence is known (only fetched when the fallback needs it). */
    async pinAsync(user) {
      const u = user || HD.session.user;
      if (!u) return { kind: "none" };
      if (u.map_precision !== "grid" && u.lat != null && !u.location_source) {
        const lic = await HD.me.licence().catch(() => null);
        return HD.me.pin(u, lic);
      }
      return HD.me.pin(u);
    },
    /** Your own grid for display: the profile grid, else the 6-character locator of your point. */
    grid(user) {
      const u = user || HD.session.user;
      if (!u) return "";
      return HD.geo.normGrid(u.grid) || HD.geo.toGrid(u.lat, u.lon, 6);
    },
  };

  // a changed location or range makes the cached neighbour list (and your licence, for a new callsign) stale
  let lastUserKey = "";
  HD.on("user", ({ user }) => {
    const key = user ? [user.callsign, user.lat, user.lon, user.range_miles, user.map_precision].join("|") : "";
    if (key !== lastUserKey) {
      lastUserKey = key;
      nbCache.promise = null;
      nbCache.at = 0;
    }
    if (!user || user.callsign !== meCache.call) meCache.promise = null;
  });

  // ---------- repeater chips in posts and comments (§7.5) ----------
  // A number becomes a chip when it looks like a frequency in a repeater sub-band, a listed repeater within
  // 30 miles of the post's place has that output to the kHz (and the offset sign the text gave), the nearest
  // wins, and each repeater appears once, in order of first mention. Shared with the map's post card (F2).

  const RPT_RANGES = [[28.9, 29.7], [50, 54], [144, 148], [222, 225], [420, 450], [902, 928], [1240, 1300]];
  const rptNear = new Map(); // place (0.01°) -> Promise of /api/repeaters/near rows
  const chipCache = new Map(); // place|text -> html

  /** The frequencies in `text` that could be repeater outputs: [{f, sign}] (sign 1, −1 or 0). */
  function freqMentions(text) {
    const out = [];
    const re = /(^|[^\d.])(\d{2,4}\.\d{1,4})(?:\s?mhz)?\s?([+\-−])?(?!\d|\.\d)/gi;
    let m;
    while ((m = re.exec(String(text || "")))) {
      const f = parseFloat(m[2]);
      if (!RPT_RANGES.some(([a, b]) => f >= a && f <= b)) continue;
      out.push({ f, sign: m[3] ? (m[3] === "+" ? 1 : -1) : 0 });
    }
    return out;
  }
  function repeatersNear(place) {
    const lat = Number(place.lat.toFixed(2)), lon = Number(place.lon.toFixed(2));
    const key = lat + "," + lon;
    if (!rptNear.has(key)) {
      const p = HD.api(`/api/repeaters/near?lat=${lat}&lon=${lon}&radius_km=48&limit=200`).then((r) => (r && r.repeaters) || []);
      rptNear.set(key, p);
      p.catch(() => rptNear.delete(key));
    }
    return rptNear.get(key);
  }
  function rptChipHtml(r) {
    const R = HD.ui.rpt;
    const tone = R.tone(r);
    const spokenTone = !tone ? "" : tone[0] === "T" ? ", tone " + tone.slice(1) : tone.startsWith("CC") ? ", colour code " + tone.slice(2)
      : tone.startsWith("NAC") ? ", NAC " + tone.slice(3) : ", DCS " + tone.slice(1);
    const label = `${HD.fmt.freq(r.output_mhz)} megahertz repeater, ${r.callsign || ""}${spokenTone}`;
    return `<button class="hd-rptchip" type="button" data-rpt="${HD.esc(r.id)}" data-lat="${HD.esc(r.lat)}" data-lon="${HD.esc(r.lon)}" aria-label="${HD.esc(label)}">` +
      `<span class="hd-rptchip__cap hd-rptchip__cap--${R.bandKey(r.band)}">${HD.esc(R.bandNum(r.band))}</span>` +
      `<span class="hd-rptchip__f">${HD.esc(R.line(r))}</span>${tone ? `<span class="hd-rptchip__x">${HD.esc(tone)}</span>` : ""}</button>`;
  }
  const chipKey = (place, text) => place.lat.toFixed(4) + "," + place.lon.toFixed(4) + "|" + text;
  HD.repeaterChips = {
    mentions: freqMentions,
    /** Promise of the chips' HTML for `text` at `place` ({lat, lon}: where §3.5 puts the post). "" when none. */
    async html(text, place) {
      if (!place || place.lat == null || !(HD.config && HD.config.features && HD.config.features.repeaters)) return "";
      const ms = freqMentions(text);
      if (!ms.length) return "";
      const key = chipKey(place, text);
      if (chipCache.has(key)) return chipCache.get(key);
      let list;
      try { list = await repeatersNear(place); } catch { return ""; }
      const seen = new Set();
      let html = "";
      for (const m of ms) {
        let best = null, bestD = Infinity;
        for (const r of list) {
          if (Math.abs(r.output_mhz - m.f) >= 0.0006) continue; // to the kHz: 146.91 is 146.910
          if (m.sign && Math.sign(r.offset_mhz || 0) !== m.sign) continue; // 146.910- never matches a + machine
          const d = HD.geo.distMi(place, r);
          if (d == null || d > 30) continue;
          if (d < bestD) { best = r; bestD = d; }
        }
        if (!best || seen.has(best.id)) continue;
        seen.add(best.id);
        html += rptChipHtml(best);
      }
      chipCache.set(key, html);
      return html;
    },
    /** The cached HTML (undefined until html() has run for this text and place). */
    cached(text, place) { return place && place.lat != null ? chipCache.get(chipKey(place, text)) : undefined; },
    clear() { chipCache.clear(); },
  };
  // a chip anywhere (Feed, a thread, the map's post card) opens the repeater's card on the map
  document.addEventListener("click", (e) => {
    const chip = e.target.closest && e.target.closest(".hd-rptchip[data-rpt]");
    if (!chip || e.defaultPrevented) return;
    e.preventDefault();
    HD.showOnMap({ lat: Number(chip.dataset.lat), lon: Number(chip.dataset.lon), zoom: 14, sel: "rpt:" + chip.dataset.rpt });
  });

  // ---------- keyboard focus that survives re-renders (§11, contract §0 "visible keyboard focus") ----------

  /** The strip of a list page that is not under its fixed or sticky bars: [top, bottom] in viewport px. */
  function visibleBand() {
    let top = 0, bottom = window.innerHeight;
    const sec = document.querySelector(".hd-view:not([hidden])");
    if (sec) {
      for (const bar of sec.querySelectorAll("[data-topbar]")) {
        const r = bar.getBoundingClientRect();
        if (r.height && r.bottom > top && r.bottom < bottom) top = r.bottom;
      }
    }
    const tab = document.querySelector(".hd-tabbar");
    if (tab) {
      const r = tab.getBoundingClientRect();
      if (r.height && r.width > r.height && r.top > top && r.top < bottom) bottom = r.top; // the bottom tab bar, not the landscape rail
    }
    return { top, bottom };
  }
  /** Scroll the page just enough that the top of `node` is in view below the bars. */
  function revealNode(node) {
    const r = node.getBoundingClientRect(), b = visibleBand();
    let dy = 0;
    if (r.top < b.top + 4) dy = r.top - (b.top + 16);
    else if (r.top > b.bottom - 48) dy = r.top - (b.top + (b.bottom - b.top) / 3);
    if (Math.abs(dy) >= 1) {
      window.scrollBy(0, dy);
      if (HD.chrome && HD.chrome.listReset) HD.chrome.listReset(); // a programmatic scroll is never a gesture
    }
  }
  const FOCUSABLE = "a[href], button, input, select, textarea, summary, [tabindex]";
  /**
   * Move focus to `node`. A post, a row or a heading that is not focusable takes focus for the moment
   * (tabindex -1 until it loses it). The page stays still unless opts.reveal (scroll the node's top into view
   * below the bars). Returns true when the node took focus (hidden nodes cannot).
   */
  HD.focusTo = function (node, opts = {}) {
    if (!node || !node.isConnected || typeof node.focus !== "function") return false;
    const temp = !node.matches(FOCUSABLE);
    if (temp) node.setAttribute("tabindex", "-1");
    node.focus({ preventScroll: true });
    if (document.activeElement !== node) {
      if (temp) node.removeAttribute("tabindex");
      return false;
    }
    if (temp) node.addEventListener("blur", () => node.removeAttribute("tabindex"), { once: true });
    if (opts.reveal) revealNode(node);
    return true;
  };
  /**
   * Re-render without losing keyboard focus: `key(node)` names what has focus inside `root` as a selector, `render`
   * rebuilds, then the equivalent element in the new markup gets focus. When nothing matches, `fallback()` decides.
   * Focus that survived (an in-place update) is left alone.
   */
  HD.keepFocus = function (root, key, render, fallback) {
    const ae = document.activeElement;
    const had = !!(root && ae && ae !== document.body && root.contains(ae));
    let sel = null;
    if (had) { try { sel = key(ae); } catch { sel = null; } }
    render();
    if (!had || (ae.isConnected && document.activeElement === ae && ae.getClientRects().length)) return;
    let next = null;
    if (sel) { try { next = root.querySelector(sel); } catch { next = null; } }
    if (!(next && HD.focusTo(next)) && fallback) fallback();
  };

  /** Scroll a horizontal scroller (tabs, chip rows) so `item` is fully visible; never scrolls the page. */
  HD.revealInScroller = function (scroller, item, pad = 16) {
    if (!scroller || !item) return;
    const s = scroller.getBoundingClientRect(), r = item.getBoundingClientRect();
    if (r.left < s.left + pad) scroller.scrollLeft -= s.left + pad - r.left;
    else if (r.right > s.right - pad) scroller.scrollLeft += r.right - (s.right - pad);
  };

  /** Open the map at a place with a card. From another view this routes to #/map?lat=&lon=&z=&sel= (contract §3:
   *  "Show on map" -> HD.router.go("map", {lat, lon, z, sel}), so Back and reload keep it); on the map itself it asks
   *  the map to focus (HD.map.focus, guarded: the map builder provides it). */
  HD.showOnMap = function ({ lat, lon, zoom, sel } = {}) {
    const cur = HD.router.current();
    const hasPoint = lat != null && lon != null && !isNaN(lat) && !isNaN(lon);
    if (cur && cur.name === "map" && HD.map && typeof HD.map.focus === "function") {
      const f = { zoom, sel };
      if (hasPoint) { f.lat = Number(lat); f.lon = Number(lon); }
      HD.map.focus(f);
      return;
    }
    const p = {};
    if (hasPoint) {
      p.lat = Number(lat).toFixed(5);
      p.lon = Number(lon).toFixed(5);
    }
    if (zoom != null) p.z = Math.round(zoom);
    if (sel) p.sel = sel;
    HD.router.go("map", p);
  };

  // ---------- config + boot ----------

  HD.config = null;

  /** Load /api/config (public), refresh the signed-in user, then route.
   *  Called once by app.js after every view script has registered itself. */
  HD.boot = async function () {
    try {
      HD.config = await HD.api("/api/config", { auth: false });
    } catch (e) {
      console.warn("[hd] /api/config unavailable — using defaults", e);
      HD.config = { basemaps: [], default_basemap: "osm", features: {}, new_ham_days: 90, aprs_window_max_s: 21600 };
    }
    HD.config.features = HD.config.features || {};
    if (HD.session.token) {
      try {
        const me = await HD.api("/api/me");
        HD.session.setUser(me);
      } catch (e) {
        if (e.status !== 401) console.warn("[hd] could not refresh the profile", e);
      }
    }
    route();
    HD.emit("booted", {});
  };
})();
