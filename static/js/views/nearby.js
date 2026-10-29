/* Nearby (DESIGN_SYSTEM §7.17): registered hams in your range, nearest first.
   Phones: list rows. Desktop: a wall of QSL cards. Every entry opens the map at the neighbour (z15)
   with their card. Data: the shared neighbour list (HD.neighbours, refreshed when older than 5 min). */
(function () {
  "use strict";
  const HD = window.HD;
  const esc = HD.esc;
  const NB = HD.fmt.NBSP;
  const icon = (n, o) => HD.icon(n, o);
  const deskMQ = window.matchMedia ? matchMedia("(min-width: 1024px)") : { matches: false };

  let el = null;
  const st = { list: null, error: null, loading: false, posts: null, postsAt: 0, scroll: 0, visible: false };

  const me = () => HD.session.user || {};
  const rangeMi = () => me().range_miles || 25;

  function skeleton() {
    el.innerHTML = `<header class="hd-topbar" data-tuck="top" data-topbar>
        <div class="hd-topbar__row"><h1 class="hd-topbar__title" id="nearby-title" tabindex="-1">Nearby</h1>
          <a class="hd-btn hd-btn--quiet hd-btn--sm" href="#/you?focus=range"><span>Within <span data-range></span>${NB}mi</span>${icon("down", { size: 20 })}</a></div>
        <p class="hd-topbar__sub" data-nearby-sub></p>
      </header>
      <div class="hd-page"><div class="hd-nearby" data-nearby></div></div>`;
    HD.chrome.watchTopbar(el.querySelector("[data-topbar]"));
  }

  function mapHref(n) {
    return HD.router.href("map", { lat: Number(n.lat).toFixed(5), lon: Number(n.lon).toFixed(5), z: 15, sel: "member:" + n.callsign });
  }
  function facts(n) {
    const u = me();
    const approx = HD.isApprox(n);
    // a neighbour at your own spot (a household, a club station at your address) has no direction or beam heading:
    // the bearing of a point to itself is a made-up north (the map's rule, M.distDir)
    const apart = u.lat != null && n.lat != null && HD.geo.distMi(u, n) >= HD.geo.SAME_SPOT_MI;
    const brg = apart ? HD.geo.bearing(u, n) : null;
    return {
      approx,
      dist: n.distance_miles != null ? HD.fmt.mi(n.distance_miles, { about: approx }) : "",
      dir: brg != null ? HD.fmt.compass(brg) : "",
      dirWord: brg != null ? HD.fmt.compassWord(brg) : "",
      brg,
      grid: HD.geo.normGrid(n.grid) || HD.geo.toGrid(n.lat, n.lon, 6),
      // license_class: "" is a club licence ("C", "Club", as You prints yours), null a callsign the FCC directory has
      // no row for right now ("Not listed", an empty ring)
      cls: n.license_class != null ? HD.ui.classShort(n.license_class) : "",
      badge: HD.ui.classBadge(n.license_class, !!n.new),
      first: HD.firstName(n.name),
    };
  }

  function render() {
    if (!el) return;
    for (const s of el.querySelectorAll("[data-range]")) s.textContent = rangeMi();
    const box = el.querySelector("[data-nearby]");
    const sub = el.querySelector("[data-nearby-sub]");
    const grid = HD.me.grid();
    const of = grid ? ` of ${esc(grid)}` : "";
    const range = rangeMi();
    const noLoc = me().callsign && (me().lat == null || me().lon == null);
    if (noLoc || (st.error && st.error.status === 400)) {
      sub.textContent = "";
      box.innerHTML = HD.ui.empty({ icon: "pin", title: "Add your grid square or address to see your neighbours.", action: `<a class="hd-btn hd-btn--primary" href="#/you?focus=grid">Open You</a>` });
      return;
    }
    if (!st.list && st.error) {
      sub.innerHTML = `Neighbours on hamdoor within ${range}${NB}miles${of}`;
      box.innerHTML = `<div class="hd-liststate">${HD.ui.alert("Couldn't load your neighbours", "Check your connection and try again.")}<button class="hd-btn" type="button" data-retry>${icon("refresh", { size: 20 })}Try again</button></div>`;
      return;
    }
    if (!st.list) {
      sub.innerHTML = `Neighbours on hamdoor within ${range}${NB}miles${of}`;
      box.innerHTML = HD.ui.statusLine("clock", "Finding your neighbours.");
      return;
    }
    const list = st.list;
    const n = list.length;
    sub.innerHTML = `${n}${NB}${n === 1 ? "neighbour" : "neighbours"} on hamdoor within ${range}${NB}miles${of}`;
    if (!n) {
      box.innerHTML = HD.ui.empty({
        icon: "nearby",
        title: `No other hamdoor members within ${range}${NB}miles.`,
        body: "Widen your range, or tell your club about hamdoor.",
        action: `<a class="hd-btn hd-btn--primary" href="#/you?focus=range">Widen my range</a>`,
      });
      return;
    }
    if (deskMQ.matches) {
      const newest = new Map();
      for (const p of st.posts || []) if (!newest.has(p.author_callsign)) newest.set(p.author_callsign, p);
      box.innerHTML = `<ul class="hd-wall" aria-label="Neighbours">${list.map((m) => {
        const f = facts(m);
        const dirWord = f.dirWord ? " " + esc(f.dirWord) : "";
        const line = f.approx
          ? `About ${esc(f.dist.replace(/^about /, ""))}${dirWord}, shown at a grid-square centre`
          : `${esc(f.dist)}${dirWord}${f.brg != null ? `, beam heading ${Math.round(f.brg) % 360}°` : ""}`;
        const lp = newest.get(m.callsign);
        return `<li><a class="hd-qsl" href="${mapHref(m)}">
          <span class="hd-qsl__head"><span class="hd-call hd-call--member hd-wall__call">${esc(m.callsign)}</span>${f.badge}</span>
          <span class="hd-qsl__name hd-wall__name">${esc(f.first)}</span>
          <span class="hd-qsl__line">${line}</span>
          ${lp ? `<span class="hd-wall__post">${HD.ui.postBadge(lp.category)}<span>${esc(lp.title)}</span></span>` : ""}
          ${HD.ui.qso([["Grid", esc(f.grid || "Not listed")], ["Class", esc(f.cls || "Not listed")]])}
        </a></li>`;
      }).join("")}</ul>`;
    } else {
      box.innerHTML = `<ul class="hd-list" aria-label="Neighbours">${list.map((m) => {
        const f = facts(m);
        const meta = [f.grid, f.cls].filter(Boolean).map(esc).join(", ");
        return `<li><a class="hd-row" href="${mapHref(m)}"><span class="hd-row__lead">${f.badge}</span>
          <span class="hd-row__title"><span class="hd-call hd-call--list hd-call--member">${esc(m.callsign)}</span><span class="hd-row__name">${esc(f.first)}</span></span>
          <span class="hd-row__meta">${meta}</span><span class="hd-row__trail">${esc(f.dist)}</span><span class="hd-row__trail2">${esc(f.dir)}</span></a></li>`;
      }).join("")}</ul>`;
    }
  }

  async function load(force) {
    if (st.loading) return;
    st.loading = true;
    st.error = null;
    try {
      const list = await HD.neighbours({ maxAge: force ? 0 : 5 * 60 * 1000 });
      st.list = (list || []).slice().sort((a, b) => (a.distance_miles ?? 1e9) - (b.distance_miles ?? 1e9));
    } catch (e) {
      st.error = e;
    } finally {
      st.loading = false;
    }
    if (deskMQ.matches) await loadPosts();
    if (st.visible) { render(); restore(); }
  }
  async function loadPosts() {
    if (st.posts && Date.now() - st.postsAt < 60000) return;
    try {
      st.posts = (await HD.api("/api/posts/feed?page=1")) || [];
      st.postsAt = Date.now();
    } catch { st.posts = st.posts || []; }
  }
  function restore() {
    if (st.restoreTo == null) return;
    const y = st.restoreTo;
    st.restoreTo = null;
    window.scrollTo(0, y);
    HD.chrome.listReset();
  }

  HD.router.register("nearby", {
    mount(section) {
      el = section;
      skeleton();
      el.addEventListener("click", (e) => {
        if (!e.target.closest("[data-retry]")) return;
        // Try again goes away with the error: focus then lands on the first neighbour, or on the new Try again
        st.error = null;
        render();
        load(true).then(() => {
          const ae = document.activeElement;
          if (!st.visible || (ae && ae !== document.body && ae.isConnected)) return;
          HD.focusTo(el.querySelector("[data-nearby] [data-retry], [data-nearby] .hd-row, [data-nearby] .hd-qsl, [data-nearby] .hd-empty__title"));
        });
      });
      const relayout = () => { if (st.visible) { if (deskMQ.matches) loadPosts().then(render); else render(); } };
      if (deskMQ.addEventListener) deskMQ.addEventListener("change", relayout); else if (deskMQ.addListener) deskMQ.addListener(relayout);
      HD.on("user", ({ user }) => { if (!user) { st.list = null; st.posts = null; } });
    },
    show() {
      st.visible = true;
      st.restoreTo = st.scroll;
      render();
      restore();
      load(false);
    },
    hide() {
      st.scroll = window.scrollY;
      st.visible = false;
    },
  });
})();
