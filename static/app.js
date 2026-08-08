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
    switchView("auth");
    throw new Error("session expired — please log in again");
  }
  if (resp.status === 204) return null;
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `request failed (${resp.status})`);
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

const VIEWS = ["auth", "feed", "map", "operators", "profile"];

function switchView(name) {
  VIEWS.forEach((v) => hide($(`#view-${v}`)));
  show($(`#view-${name}`));
  $$(".nav-btn[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  if (name === "feed") loadFeed();
  if (name === "operators") loadOperators();
  if (name === "profile") loadProfile();
  if (name === "map") loadMap();
}

function enterApp() {
  show($("#nav"));
  switchView("feed");
}

// ---------- auth ----------

function authError(msg) { $("#auth-error").textContent = msg || ""; }

$("#tab-signup").addEventListener("click", () => {
  $("#tab-signup").classList.add("active");
  $("#tab-login").classList.remove("active");
  show($("#form-signup")); hide($("#form-login")); authError("");
});
$("#tab-login").addEventListener("click", () => {
  $("#tab-login").classList.add("active");
  $("#tab-signup").classList.remove("active");
  show($("#form-login")); hide($("#form-signup")); authError("");
});

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
  } catch (e) { authError(e.message); }
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
  try {
    const ops = await api("/api/operators/nearby");
    ops.forEach((o) => {
      if (o.lat == null || o.lon == null) return;
      const m = L.marker([o.lat, o.lon]).addTo(state.map)
        .bindPopup(`<b>${esc(o.callsign)}</b>${o.name ? " — " + esc(o.name) : ""}<br>${o.distance_miles ?? "?"} mi away`);
      state.mapMarkers.push(m);
      bounds.push([o.lat, o.lon]);
    });
  } catch (e) { /* banner shown in operators view */ }
  if (bounds.length) state.map.fitBounds(bounds, { padding: [30, 30], maxZoom: 11 });
  else state.map.setView([39.8, -98.5], 4);
}

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
