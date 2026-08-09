/* hamdoor SPA — talks to the same REST API a future mobile app will use. */
"use strict";

const state = {
  token: localStorage.getItem("hamdoor_token") || null,
  user: JSON.parse(localStorage.getItem("hamdoor_user") || "null"),
  feedPage: 1,
  feedCategory: "",
  map: null,
  mapMarkers: [],
};

// ---------- helpers ----------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

async function api(path, { method = "GET", body = null, auth = true } = {}) {
  const headers = {};
  if (body !== null) headers["Content-Type"] = "application/json";
  if (auth && state.token) headers["Authorization"] = `Bearer ${state.token}`;
  const resp = await fetch(path, {
    method,
    headers,
    body: body === null ? null : JSON.stringify(body),
  });
  if (resp.status === 401 && auth) {
    setSession(null, null);
    showAuthTab("login");
    switchView("auth");
    throw new Error("session expired — please log in again");
  }
  if (resp.status === 204) return null;
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = data && data.detail;
    let msg;
    if (typeof detail === "string") {
      msg = detail;
    } else if (Array.isArray(detail)) {
      // pydantic validation errors arrive as a list — make them readable
      msg = detail.map((d) => {
        const field = (d.loc || []).filter((x) => x !== "body").join(".") || "input";
        return `${field}: ${String(d.msg).replace(/^Value error, /, "")}`;
      }).join("; ");
    } else {
      msg = `request failed (${resp.status})`;
    }
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function setSession(token, user) {
  state.token = token;
  state.user = user;
  if (token) {
    localStorage.setItem("hamdoor_token", token);
    localStorage.setItem("hamdoor_user", JSON.stringify(user));
  } else {
    localStorage.removeItem("hamdoor_token");
    localStorage.removeItem("hamdoor_user");
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtDate(iso) {
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  return d.toLocaleString();
}

// ---------- views ----------

const VIEWS = ["auth", "feed", "map", "operators", "activity", "profile"];

function switchView(name) {
  VIEWS.forEach((v) => hide($(`#view-${v}`)));
  show($(`#view-${name}`));
  $$(".nav-btn[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  if (name === "feed") loadFeed();
  if (name === "operators") loadOperators();
  if (name === "activity") loadActivity();
  if (name === "profile") loadProfile();
  if (name === "map") loadMap();
}

function enterApp() {
  show($("#nav"));
  switchView("feed");
}

// ---------- auth ----------

function authError(msg) { $("#auth-error").textContent = msg || ""; }

function showAuthTab(which) {
  const signup = which === "signup";
  $("#tab-signup").classList.toggle("active", signup);
  $("#tab-login").classList.toggle("active", !signup);
  signup ? (show($("#form-signup")), hide($("#form-login")))
         : (show($("#form-login")), hide($("#form-signup")));
  authError("");
}

$("#tab-signup").addEventListener("click", () => showAuthTab("signup"));
$("#tab-login").addEventListener("click", () => showAuthTab("login"));

$("#lookup-btn").addEventListener("click", async () => {
  const cs = $("#su-callsign").value.trim().toUpperCase();
  authError("");
  if (!cs) { authError("enter a callsign first"); return; }
  $("#lookup-status").textContent = "Looking up " + cs + "…";
  try {
    const r = await api(`/api/lookup/${encodeURIComponent(cs)}`, { auth: false });
    if (!r.found) {
      $("#lookup-status").textContent = "";
      authError(`${cs} was not found in the FCC database. Check the spelling.`);
      hide($("#signup-details"));
      return;
    }
    $("#su-name").value = r.name || "";
    $("#su-address").value = r.address_line || "";
    $("#su-city").value = r.city || "";
    $("#su-state").value = r.state || "";
    $("#su-zip").value = r.zip || "";
    $("#su-grid").value = r.grid || "";
    if (r.email) $("#su-email").value = r.email;
    $("#lookup-status").textContent =
      `Found: ${r.name} — ${[r.city, r.state].filter(Boolean).join(", ")}` +
      (r.license_class ? ` (class ${r.license_class})` : "") +
      `. Add your email and a password to finish.`;
    show($("#signup-details"));
    $("#su-email").focus();
  } catch (e) {
    $("#lookup-status").textContent = "";
    authError(e.message);
  }
});

$("#form-signup").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  authError("");
  try {
    const r = await api("/api/auth/signup", {
      method: "POST",
      auth: false,
      body: {
        callsign: $("#su-callsign").value.trim().toUpperCase(),
        password: $("#su-password").value,
        email: $("#su-email").value,
        name: $("#su-name").value,
        address_line: $("#su-address").value,
        city: $("#su-city").value,
        state: $("#su-state").value,
        zip: $("#su-zip").value,
        grid: $("#su-grid").value,
      },
    });
    setSession(r.token, r.user);
    enterApp();
  } catch (e) {
    if (e.status === 409) {
      // already registered — that's a login, not a signup: move the user to
      // the Log in tab with their callsign prefilled instead of dead-ending
      const cs = $("#su-callsign").value.trim().toUpperCase();
      showAuthTab("login");
      $("#li-callsign").value = cs;
      $("#li-password").focus();
      authError(`${cs} already has an account — log in below.`);
    } else {
      authError(e.message);
    }
  }
});

$("#form-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  authError("");
  try {
    const r = await api("/api/auth/login", {
      method: "POST",
      auth: false,
      body: { callsign: $("#li-callsign").value.trim().toUpperCase(), password: $("#li-password").value },
    });
    setSession(r.token, r.user);
    enterApp();
  } catch (e) { authError(e.message); }
});

$("#logout-btn").addEventListener("click", () => {
  setSession(null, null);
  hide($("#nav"));
  showAuthTab("login");  // returning user — land on Log in, not Sign up
  switchView("auth");
});

// ---------- feed ----------

async function loadFeed() {
  const list = $("#feed-list");
  list.innerHTML = "";
  $("#feed-range-note").textContent =
    `Showing posts within ${state.user.range_miles} miles of you.`;
  $("#feed-page").textContent = `page ${state.feedPage}`;
  try {
    const cat = state.feedCategory ? `&category=${encodeURIComponent(state.feedCategory)}` : "";
    const posts = await api(`/api/posts/feed?page=${state.feedPage}${cat}`);
    if (!posts.length) {
      list.innerHTML = `<div class="card muted">No posts in range yet. Be the first to say 73!</div>`;
      return;
    }
    posts.forEach((p) => list.appendChild(renderPost(p)));
  } catch (e) {
    list.innerHTML = `<div class="card error">${esc(e.message)}</div>`;
  }
}

function renderPost(p) {
  const node = $("#tpl-post").content.firstElementChild.cloneNode(true);
  node.dataset.id = p.id;
  node.querySelector(".badge").textContent = p.category;
  node.querySelector(".post-title").textContent = p.title;
  node.querySelector(".post-meta").textContent =
    `${p.author_callsign}${p.author_name ? " · " + p.author_name : ""}` +
    (p.distance_miles != null ? ` · ${p.distance_miles} mi away` : "") +
    ` · ${fmtDate(p.created_at)}`;
  node.querySelector(".post-body").textContent = p.body;

  const actions = node.querySelector(".post-actions");
  const toggle = document.createElement("button");
  toggle.textContent = `💬 ${p.comment_count} comment${p.comment_count === 1 ? "" : "s"}`;
  toggle.addEventListener("click", () => toggleComments(node, p.id));
  actions.appendChild(toggle);

  if (state.user && p.author_callsign === state.user.callsign) {
    const del = document.createElement("button");
    del.textContent = "Delete";
    del.className = "danger";
    del.addEventListener("click", async () => {
      if (!confirm("Delete this post?")) return;
      await api(`/api/posts/${p.id}`, { method: "DELETE" });
      loadFeed();
    });
    actions.appendChild(del);
  }

  const form = node.querySelector(".comment-form");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = form.querySelector(".comment-input");
    if (!input.value.trim()) return;
    await api(`/api/posts/${p.id}/comments`, { method: "POST", body: { body: input.value.trim() } });
    input.value = "";
    await loadComments(node, p.id);
  });
  return node;
}

async function toggleComments(node, postId) {
  const box = node.querySelector(".comments");
  const form = node.querySelector(".comment-form");
  if (box.dataset.open === "1") {
    box.dataset.open = "0";
    box.innerHTML = "";
    hide(box); hide(form);
    return;
  }
  box.dataset.open = "1";
  show(box); show(form);
  await loadComments(node, postId);
}

async function loadComments(node, postId) {
  const box = node.querySelector(".comments");
  const post = await api(`/api/posts/${postId}`);
  box.innerHTML = post.comments.map((c) => {
    const mine = state.user && c.author_callsign === state.user.callsign;
    return `<div class="comment"><span class="who">${esc(c.author_callsign)}</span>: ${esc(c.body)}
      <span class="muted small">· ${fmtDate(c.created_at)}</span>
      ${mine ? `<a href="#" data-cid="${c.id}" class="del-comment muted small">delete</a>` : ""}</div>`;
  }).join("") || `<div class="muted small">No comments yet.</div>`;
  box.querySelectorAll(".del-comment").forEach((a) =>
    a.addEventListener("click", async (ev) => {
      ev.preventDefault();
      await api(`/api/comments/${a.dataset.cid}`, { method: "DELETE" });
      await loadComments(node, postId);
    }));
}

$("#form-post").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("/api/posts", {
      method: "POST",
      body: {
        title: $("#post-title").value,
        body: $("#post-body").value,
        category: $("#post-category").value,
      },
    });
    $("#post-title").value = "";
    $("#post-body").value = "";
    state.feedPage = 1;
    loadFeed();
  } catch (e) { alert(e.message); }
});

$("#feed-refresh").addEventListener("click", loadFeed);
$("#feed-filter").addEventListener("change", (ev) => {
  state.feedCategory = ev.target.value;
  state.feedPage = 1;
  loadFeed();
});
$("#feed-prev").addEventListener("click", () => { if (state.feedPage > 1) { state.feedPage--; loadFeed(); } });
$("#feed-next").addEventListener("click", () => { state.feedPage++; loadFeed(); });

// ---------- operators ----------

async function loadOperators() {
  const list = $("#ops-list");
  list.innerHTML = "";
  try {
    const ops = await api("/api/operators/nearby");
    if (!ops.length) {
      list.innerHTML = `<div class="card muted">No other operators within your range yet. Tell your local club!</div>`;
      return;
    }
    list.innerHTML = ops.map((o) => `
      <div class="card op-card">
        <div>
          <span class="op-call">${esc(o.callsign)}</span>
          ${o.name ? `<span> — ${esc(o.name)}</span>` : ""}
          <div class="muted small">${o.grid ? `grid ${esc(o.grid)}` : "location approximate"}</div>
        </div>
        <div>${o.distance_miles != null ? o.distance_miles + " mi" : ""}</div>
      </div>`).join("");
  } catch (e) {
    list.innerHTML = `<div class="card error">${esc(e.message)}</div>`;
  }
}

$("#ops-refresh").addEventListener("click", loadOperators);

// ---------- map ----------

async function loadMap() {
  if (!state.map) {
    state.map = L.map("map");
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(state.map);
    state.hamLayer = L.layerGroup().addTo(state.map);
    state.map.on("moveend", () => { if ($("#map-all-hams").checked) loadHamLayer(); });
  }
  setTimeout(() => state.map.invalidateSize(), 50);
  state.mapMarkers.forEach((m) => m.remove());
  state.mapMarkers = [];

  const bounds = [];
  const me = state.user;
  if (me && me.lat != null && me.lon != null) {
    const m = L.marker([me.lat, me.lon]).addTo(state.map)
      .bindPopup(`<b>${esc(me.callsign)}</b> (you)`).openPopup();
    state.mapMarkers.push(m);
    bounds.push([me.lat, me.lon]);
    L.circle([me.lat, me.lon], {
      radius: (me.range_miles || 25) * 1609.34,
      color: "#4da3ff", weight: 1, fillOpacity: 0.06,
    }).addTo(state.map);
  }
  if ($("#map-ops").checked) {
    try {
      const ops = await api("/api/operators/nearby");
      ops.forEach((o) => {
        if (o.lat == null || o.lon == null) return;
        const m = L.marker([o.lat, o.lon]).addTo(state.map)
          .bindPopup(`<b>${esc(o.callsign)}</b>${o.name ? " — " + esc(o.name) : ""}<br>${o.distance_miles ?? "?"} mi away (registered)`);
        state.mapMarkers.push(m);
        bounds.push([o.lat, o.lon]);
      });
    } catch (e) { /* banner shown in operators view */ }
  }
  if (bounds.length) state.map.fitBounds(bounds, { padding: [30, 30], maxZoom: 11 });
  else state.map.setView([39.8, -98.5], 4);

  loadHamLayer();
}

const CLASS_COLORS = {
  E: "#c792ea",  // Amateur Extra
  A: "#ff9e64",  // Advanced
  G: "#7ee787",  // General
  T: "#4da3ff",  // Technician
  P: "#e3b341",  // Technician Plus
  N: "#e3b341",  // Novice
};
const CLASS_NAMES = {
  E: "Amateur Extra", A: "Advanced", G: "General",
  T: "Technician", P: "Technician Plus", N: "Novice",
};
const UNKNOWN_COLOR = "#8fa0b5";   // club stations / no class on record
const EXPIRED_COLOR = "#ff6b6b";   // past expiration, in renewal grace period
const CLUSTER_COLOR = "#e3b341";

function hamColor(h) {
  if (h.expired) return EXPIRED_COLOR;
  return CLASS_COLORS[h.license_class] || UNKNOWN_COLOR;
}

async function loadHamLayer() {
  if (!state.map) return;
  state.hamLayer.clearLayers();
  const status = $("#map-status");
  if (!$("#map-all-hams").checked) { status.textContent = ""; return; }
  const b = state.map.getBounds();
  const z = state.map.getZoom();
  status.textContent = "loading hams…";
  try {
    const q = `min_lat=${b.getSouth()}&max_lat=${b.getNorth()}&min_lon=${b.getWest()}&max_lon=${b.getEast()}&zoom=${z}`;
    const data = await api(`/api/hams/map?${q}`);
    if (data.type === "clusters") {
      let total = 0;
      data.cells.forEach((c) => {
        total += c.count;
        const r = Math.min(28, 6 + Math.log10(c.count) * 7);
        L.circleMarker([c.lat, c.lon], {
          radius: r, color: CLUSTER_COLOR, weight: 1, fillColor: CLUSTER_COLOR, fillOpacity: 0.35,
        }).addTo(state.hamLayer)
          .bindTooltip(`${c.count.toLocaleString()} hams`, { direction: "top" });
      });
      status.textContent = `${total.toLocaleString()} hams in view (zoom in to ${z >= 9 ? "1 more level" : "level 10"} for individual callsigns)`;
    } else {
      data.hams.forEach((h) => {
        const color = hamColor(h);
        const cls = h.expired ? "EXPIRED — renewal grace period"
                              : (CLASS_NAMES[h.license_class] || "Club / unknown class");
        L.circleMarker([h.lat, h.lon], {
          radius: 4, color: color, weight: 1, fillColor: color, fillOpacity: 0.65,
        }).addTo(state.hamLayer)
          .bindTooltip(`<b>${esc(h.callsign)}</b> — ${esc(h.name)}<br>${esc(h.city)}, ${esc(h.state)} · ${cls}`,
                       { direction: "top" });
      });
      status.textContent = `${data.hams.length.toLocaleString()} hams shown` +
        (data.truncated ? " (capped — zoom in further)" : "");
    }
  } catch (e) {
    status.textContent = e.message;
  }
}

$("#map-all-hams").addEventListener("change", loadHamLayer);
$("#map-ops").addEventListener("change", loadMap);

// ---------- profile ----------

function loadProfile() {
  const u = state.user;
  if (!u) return;
  $("#profile-heading").textContent = `${u.callsign} — profile`;
  $("#pf-name").value = u.name || "";
  $("#pf-email").value = u.email || "";
  $("#pf-address").value = u.address_line || "";
  $("#pf-city").value = u.city || "";
  $("#pf-state").value = u.state || "";
  $("#pf-zip").value = u.zip || "";
  $("#pf-grid").value = u.grid || "";
  $("#pf-range").value = u.range_miles || 25;
  $("#pf-range-label").textContent = u.range_miles || 25;
}

$("#pf-range").addEventListener("input", (ev) => {
  $("#pf-range-label").textContent = ev.target.value;
});

$("#form-profile").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#profile-status").textContent = "Saving…";
  try {
    const u = await api("/api/me", {
      method: "PATCH",
      body: {
        name: $("#pf-name").value,
        email: $("#pf-email").value,
        address_line: $("#pf-address").value,
        city: $("#pf-city").value,
        state: $("#pf-state").value,
        zip: $("#pf-zip").value,
        grid: $("#pf-grid").value,
        range_miles: Number($("#pf-range").value),
      },
    });
    setSession(state.token, u);
    $("#profile-status").textContent = "Saved. Your feed range is now " + u.range_miles + " miles.";
  } catch (e) { $("#profile-status").textContent = e.message; }
});

$("#pf-refresh-callsign").addEventListener("click", async () => {
  $("#profile-status").textContent = "Re-syncing from FCC data…";
  try {
    const u = await api("/api/me/refresh-callsign", { method: "POST" });
    setSession(state.token, u);
    loadProfile();
    $("#profile-status").textContent = "Profile refreshed from the FCC database.";
  } catch (e) { $("#profile-status").textContent = e.message; }
});

$("#form-password").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#password-status").textContent = "";
  try {
    await api("/api/me/password", {
      method: "POST",
      body: { current_password: $("#pw-current").value, new_password: $("#pw-new").value },
    });
    $("#pw-current").value = "";
    $("#pw-new").value = "";
    $("#password-status").textContent = "Password changed.";
  } catch (e) { $("#password-status").textContent = e.message; }
});

// ---------- activity toolbox ----------

const act = { src: "bands", loaded: {} };

function actTable(cols, rows) {
  if (!rows.length) return `<p class="muted">No spots right now.</p>`;
  const head = cols.map((c) => `<th>${c[1]}</th>`).join("");
  const body = rows.map((r) =>
    `<tr>${cols.map((c) => `<td>${c[2] ? c[2](r) : esc(r[c[0]] ?? "")}</td>`).join("")}</tr>`).join("");
  return `<div class="table-wrap"><table class="spots"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

const fmtFreqMHz = (khz) => khz ? (Number(khz) / 1000).toFixed(3) + " MHz" : "";
const fmtDist = (mi) => mi != null ? mi + " mi" : "";
const fmtTime = (t) => {
  if (!t) return "";
  const d = new Date(t.endsWith("Z") || t.includes("+") ? t : t + "Z");
  return isNaN(d) ? esc(t) : d.toUTCString().slice(17, 22) + " UTC";
};

function nearFilter(rows) {
  if (!$("#act-near-only").checked || !state.user) return rows;
  return rows.filter((r) => r.distance_miles != null && r.distance_miles <= state.user.range_miles);
}

async function loadActivity() {
  $("#act-callsign").textContent = state.user ? state.user.callsign : "";
  $("#act-dir-wrap").style.display = act.src === "pskreporter" ? "" : "none";
  $("#act-mine-wrap").style.display = act.src === "rbn" ? "" : "none";
  $("#act-radius-wrap").style.display = act.src === "aprs" ? "" : "none";
  $("#act-near-wrap").style.display = ["pskreporter", "aprs", "pota", "sota"].includes(act.src) ? "" : "none";

  const status = $("#act-status"), content = $("#act-content");
  status.textContent = "Loading " + act.src + "…";
  content.innerHTML = "";
  $$(".act-tab").forEach((b) => b.classList.toggle("active", b.dataset.src === act.src));

  try {
    let url = `/api/activity/${act.src}`;
    if (act.src === "pskreporter") url += `?direction=${$("#act-direction").value}`;
    if (act.src === "rbn") url += `?mine_only=${$("#act-mine-only").checked}`;
    if (act.src === "aprs") url += `?radius_km=${Number($("#act-radius").value) || 150}`;
    const data = await api(url);
    $("#act-source").textContent = data.source ? `source: ${data.source}` : "";

    if (act.src === "bands") {
      const condColor = { Good: "#7ee787", Fair: "#e3b341", Poor: "#ff7b72" };
      status.textContent = data.updated ? "updated " + data.updated : "";
      content.innerHTML = `
        <div class="row solar-row">
          <div class="stat">SFI<br><strong>${esc(data.solar_flux)}</strong></div>
          <div class="stat">A<br><strong>${esc(data.a_index)}</strong></div>
          <div class="stat">K<br><strong>${esc(data.k_index)}</strong></div>
          <div class="stat">Sunspots<br><strong>${esc(data.sunspots)}</strong></div>
          <div class="stat">Solar wind<br><strong>${esc(data.solar_wind)}</strong> km/s</div>
          <div class="stat">Geomag<br><strong>${esc(data.geomag)}</strong></div>
        </div>
        ${actTable([["band", "Band"], ["time", "Time of day"],
          ["condition", "Condition", (r) => `<span style="color:${condColor[r.condition] || "inherit"}">${esc(r.condition)}</span>`]],
          data.bands || [])}`;
    } else if (act.src === "pskreporter") {
      const sent = $("#act-direction").value === "sent";
      status.textContent = `${data.spots.length} reports (last ~hours, cached 1 min)`;
      content.innerHTML = actTable(
        [["time", "Time", (r) => fmtTime(r.time)],
         [sent ? "receiver_callsign" : "sender_callsign", sent ? "Heard by" : "Heard"],
         ["mode", "Mode"],
         ["frequency_hz", "Freq", (r) => fmtFreqMHz(r.frequency_hz / 1000)],
         ["snr", "SNR", (r) => r.snr + " dB"],
         [sent ? "receiver_grid" : "sender_grid", "Grid"],
         ["distance_miles", "Distance", (r) => fmtDist(r.distance_miles)]],
        nearFilter(data.spots));
    } else if (act.src === "wspr") {
      status.textContent = `${data.spots.length} WSPR reports in the last 24h`;
      content.innerHTML = actTable(
        [["time", "Time", (r) => fmtTime(r.time)],
         ["tx_callsign", "TX"], ["rx_callsign", "RX"],
         ["band_m", "Band", (r) => r.band_m != null ? r.band_m + " m" : ""],
         ["snr", "SNR", (r) => r.snr + " dB"],
         ["power_dbm", "Power", (r) => r.power_dbm != null ? r.power_dbm + " dBm" : ""],
         ["distance_miles", "Distance", (r) => fmtDist(r.distance_miles)]],
        data.spots);
    } else if (act.src === "dxcluster") {
      status.textContent = `${data.spots.length} spots in a ${data.sample_seconds}s live sample`;
      content.innerHTML = actTable(
        [["time", "Time"], ["dx_callsign", "DX"],
         ["frequency_khz", "Freq", (r) => fmtFreqMHz(r.frequency_khz)],
         ["spotter", "Spotter"], ["comment", "Comment"]],
        data.spots);
    } else if (act.src === "rbn") {
      status.textContent = `${data.spots.length} CW skimmer reports in a ${data.sample_seconds}s live sample`;
      content.innerHTML = actTable(
        [["time", "Time"], ["dx_callsign", "Station"],
         ["frequency_khz", "Freq", (r) => fmtFreqMHz(r.frequency_khz)],
         ["spotter", "Skimmer"], ["comment", "Report"]],
        data.spots);
    } else if (act.src === "aprs") {
      status.textContent = `${data.stations.length} stations within ${data.radius_km} km (${data.sample_seconds}s live sample)`;
      content.innerHTML = actTable(
        [["callsign", "Station"], ["object", "Object"],
         ["distance_miles", "Distance", (r) => fmtDist(r.distance_miles)],
         ["comment", "Comment"]],
        nearFilter(data.stations));
    } else if (act.src === "pota") {
      status.textContent = `${data.spots.length} active parks`;
      content.innerHTML = actTable(
        [["time", "Time", (r) => fmtTime(r.time)], ["activator", "Activator"],
         ["park_ref", "Park"], ["frequency_khz", "Freq", (r) => r.frequency_khz ? r.frequency_khz + " kHz" : ""],
         ["mode", "Mode"], ["distance_miles", "Distance", (r) => fmtDist(r.distance_miles)],
         ["comments", "Comments"]],
        nearFilter(data.spots));
    } else if (act.src === "sota") {
      status.textContent = `${data.spots.length} summit spots (last 2h)`;
      content.innerHTML = actTable(
        [["time", "Time", (r) => fmtTime(r.time)], ["activator", "Activator"],
         ["summit", "Summit"], ["frequency_khz", "Freq kHz"],
         ["mode", "Mode"], ["distance_miles", "Distance", (r) => fmtDist(r.distance_miles)],
         ["comments", "Comments"]],
        nearFilter(data.spots));
    }
  } catch (e) {
    status.textContent = "";
    content.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

$$(".act-tab").forEach((b) => b.addEventListener("click", () => { act.src = b.dataset.src; loadActivity(); }));
$("#act-refresh").addEventListener("click", loadActivity);
$("#act-direction").addEventListener("change", loadActivity);
$("#act-mine-only").addEventListener("change", loadActivity);
$("#act-near-only").addEventListener("change", loadActivity);
$("#act-radius").addEventListener("change", loadActivity);

// ---------- nav & boot ----------

$$(".nav-btn[data-view]").forEach((b) =>
  b.addEventListener("click", () => switchView(b.dataset.view)));

(async function boot() {
  if (!state.token) { hide($("#nav")); switchView("auth"); return; }
  try {
    const me = await api("/api/me");
    setSession(state.token, me);
    enterApp();
  } catch {
    hide($("#nav"));
    switchView("auth");
  }
})();
