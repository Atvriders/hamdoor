/* hamdoor map 5/9: people on hamdoor. DESIGN_SYSTEM.md §3.2, §7.11, §7.13, §9.1.
   You (always drawn, always labelled, your range ring), and your neighbours from
   GET /api/operators/nearby (HD.map.neighbours), drawn at every zoom in the
   hd-ops pane exactly where the API puts them. Red is a ring, a label and a count
   disc, never a fill among the class dots. Marks closer than 24 px merge into a
   count disc; neighbours within 24 px of you ride on your rim; below z10 marks
   that land on a postmark ride its rim. A grid-precision neighbour is drawn with
   a dashed ring, and every distance to them is "about" whole miles. */
(function () {
  "use strict";
  const HD = window.HD;
  const L = window.L;
  const M = HD.map._;
  const K = M.marks;
  const NB = M.NB;
  const esc = M.esc;
  const MERGE = () => M.cssNum("--hd-merge-px", 24);

  /** a neighbour as the map uses it: approximate when they chose grid precision, or when the API
      could only place them at the centre of their grid locator (§3.2) */
  const cache = new Map();
  function person(m) {
    const key = JSON.stringify(m);
    const hit = cache.get(m.callsign);
    if (hit && hit._key === key) return hit;
    const approx = m.map_precision === "grid" || (M.finite(m.lat) && M.isGridCentre(m.lat, m.lon, m.grid));
    const grid = m.grid ? M.gridFmt(m.grid) : M.finite(m.lat) ? M.grid(m.lat, m.lon, 6) : "";
    const p = Object.assign({}, m, { first: M.firstName(m.name) || m.callsign, approx, grid, _key: key });
    cache.set(m.callsign, p);
    return p;
  }
  const people = () => M.neighbourList().filter((m) => M.finite(m.lat) && M.finite(m.lon)).map(person);
  const distTxt = (m) => (M.finite(m.distance_miles) ? M.mi(m.distance_miles, m.approx) : "");
  const brg = (m) => { const h = M.home(); return h ? M.bearing(h, { lat: m.lat, lon: m.lon }) : null; };

  /* ------------------------------------------------------------ render */
  function postBadgeFor(call, z) {
    const posts = M.L("posts");
    if (!posts || !M.isOn("posts") || z < M.MINZ.posts || !posts.def.newestBy) return "";
    const p = posts.def.newestBy(call);
    return p ? K.postBadge(p.category) : "";
  }
  function render(data, ctx) {
    const want = new Map(), z = ctx.zoom, me = M.me(), y = M.youPlace();
    const merge = MERGE();
    // you: always drawn, always labelled; below z10 the word "You" steps outside your metro's postmark
    if (y && me) {
      const lx = Math.round(ctx.rim ? ctx.rim.youLbl : 10);
      want.set("you", { latlng: [y.lat, y.lon], size: [18, 18], pane: "hd-you", z: 1000, lbl: true, always: true, pri: -10,
        item: { layer: "members", kind: "you" }, title: `You, ${me.callsign}`,
        html: `<div class="hd-mk-you${y.approx ? " hd-mk-you--approx" : ""}" style="--you-lbl:${lx}px"><div class="hd-mk-you__halo"></div><div class="hd-mk-you__dot"></div>${postBadgeFor(me.callsign, z)}<span class="hd-lbl hd-lbl--you hd-halo">You</span></div>` });
    }
    if (!M.isOn("members")) return want;
    const list = people();
    if (!list.length) return want;
    const pts = list.map((m) => ({ m, p: ctx.toPoint(m.lat, m.lon) }));
    const yp = y ? ctx.toPoint(y.lat, y.lon) : null;
    // neighbours within 24 px of you form one group that rides on your rim, like a badge
    const atYou = yp ? pts.filter((a) => Math.hypot(a.p.x - yp.x, a.p.y - yp.y) < merge) : [];
    const used = new Set(atYou.map((a) => pts.indexOf(a))), groups = [];
    if (atYou.length) { atYou.onYou = true; groups.push(atYou); }
    pts.forEach((a, i) => {
      if (used.has(i)) return;
      used.add(i);
      const g = [a];
      pts.forEach((b, j) => { if (!used.has(j) && Math.hypot(a.p.x - b.p.x, a.p.y - b.p.y) < merge) { used.add(j); g.push(b); } });
      groups.push(g);
    });
    // the rim slot: up-left of you, else down-left, down-right, up-right; free = no other neighbour within 24 px
    const others = pts.filter((a) => !atYou.includes(a));
    const slot = yp ? ([[-17, -17], [-17, 17], [17, 19], [17, -19]].find(([dx, dy]) => others.every((o) => Math.hypot(o.p.x - (yp.x + dx), o.p.y - (yp.y + dy)) >= merge)) || [-17, -17]) : null;
    const youAnchor = (g, lat, lon, w, h) => {
      if (!g.onYou || !yp) return undefined;
      const p = ctx.toPoint(lat, lon), cx = yp.x + slot[0], cy = yp.y + slot[1];
      return [w / 2 - (cx - p.x), h / 2 - (cy - p.y)];
    };
    for (const g of groups) {
      if (g.length === 1) {
        const m = g[0].m, ll = [m.lat, m.lon], item = { layer: "members", kind: "member", m };
        const title = `${m.callsign}, ${m.first}, on hamdoor${m.approx ? ", shown at the centre of a grid square" : ""}`;
        if (z >= M.MINZ.memberLabels) {
          const isSel = !!ctx.sel && ctx.sel.layer === "members" && ctx.sel.kind === "member" && ctx.sel.m.callsign === m.callsign;
          want.set("m:" + m.callsign, { latlng: ll, size: [24, 24], anchor: youAnchor(g, ll[0], ll[1], 24, 24), pane: g.onYou ? "hd-ride" : "hd-ops", lbl: true, pri: isSel ? -5 : 0, always: isSel, item, title, calls: [m.callsign],
            html: `<div class="hd-mk-member">${K.memberSvg(m)}${postBadgeFor(m.callsign, z)}<span class="hd-lbl hd-lbl--member hd-halo">${esc(m.callsign)}</span></div>` });
        } else {
          want.set("m:" + m.callsign, { latlng: ll, size: [14, 14], anchor: (ctx.rim && ctx.rim.anchor(ll[0], ll[1], 14, 14)) || youAnchor(g, ll[0], ll[1], 14, 14), pane: "hd-ops", item, title, calls: [m.callsign],
            html: '<div class="hd-mk-cluster" style="--c:var(--cluster-member)"></div>' });
        }
      } else {
        const ms = g.map((x) => x.m), n = ms.length;
        const lat = ms.reduce((a, x) => a + x.lat, 0) / n, lon = ms.reduce((a, x) => a + x.lon, 0) / n;
        const separable = apartAt(ms, 15), item = { layer: "members", kind: "mgroup", ms, lat, lon };
        const anchor = (z < M.MINZ.fccDots && ctx.rim ? ctx.rim.anchor(lat, lon, 26, 26) : undefined) || youAnchor(g, lat, lon, 26, 26);
        want.set("mg:" + ms.map((x) => x.callsign).sort().join(","), { latlng: [lat, lon], size: [26, 26], anchor, pane: g.onYou && z >= M.MINZ.fccDots ? "hd-ride" : "hd-ops", item, calls: ms.map((x) => x.callsign),
          html: `<div class="hd-mk-cluster" style="--c:var(--cluster-member);--ci:var(--cluster-member-ink)">${n}</div>`,
          title: `${n} neighbours on hamdoor. Select to ${separable ? "zoom in" : "list them"}.`,
          // selecting zooms to fit them (at most z15) when they would stand apart there, else lists them
          onClick: (e, opts) => (separable ? M.map.fitBounds(L.latLngBounds(ms.map((x) => [x.lat, x.lon])).pad(0.6), { maxZoom: 15, animate: !M.reduced() }) : M.ui.openDetail(item, opts)) });
      }
    }
    return want;
  }
  function apartAt(ms, z) {
    for (let i = 0; i < ms.length; i++) for (let j = i + 1; j < ms.length; j++) {
      const a = M.map.project([ms[i].lat, ms[i].lon], z), b = M.map.project([ms[j].lat, ms[j].lon], z);
      if (a.distanceTo(b) < MERGE()) return false;
    }
    return true;
  }
  /* your range: an L.circle centred on your mark, 1.5 px, dash 8 7, opacity .85, fill 4 % */
  let rangeCircle = null;
  function canvas(data, ctx) {
    const me = M.me(), y = M.youPlace();
    const want = y && me && M.isOn("members") && M.prefs.members.range && M.finite(me.range_miles);
    if (!want) { if (rangeCircle) { M.map.removeLayer(rangeCircle); rangeCircle = null; } return; }
    const r = me.range_miles * 1609.344, color = ctx.T.range;
    if (!rangeCircle) {
      rangeCircle = L.circle([y.lat, y.lon], { renderer: M.R.range, radius: r, color, weight: 1.5, opacity: 0.85, dashArray: "8 7", fillColor: color, fillOpacity: 0.04, interactive: false }).addTo(M.map);
      return;
    }
    const ll = rangeCircle.getLatLng();
    if (ll.lat !== y.lat || ll.lng !== y.lon) rangeCircle.setLatLng([y.lat, y.lon]);
    if (rangeCircle.getRadius() !== r) rangeCircle.setRadius(r);
    if (rangeCircle.options.color !== color) rangeCircle.setStyle({ color, fillColor: color });
  }

  /* ------------------------------------------------------------ panel */
  function counts(data, ctx) {
    if (!M.neighboursKnown()) return { meta: "", chips: {} }; // never "0 neighbours" while the list is on its way or failing
    const me = M.me(), b = M.screenBounds();
    const n = people().filter((m) => b.contains([m.lat, m.lon])).length;
    return { meta: `${n}${NB}${n === 1 ? "neighbour" : "neighbours"} in view, your range ${me ? me.range_miles : 25}${NB}mi`, chips: {} };
  }
  function section() {
    return `<div class="hd-chips hd-layers__chips">${M.chip("members/range", "My range ring", '<span class="hd-sw" style="background:transparent;box-shadow:none;border:2px dashed var(--range)"></span>')}</div>
      <p class="hd-layers__note"><svg class="hd-sw hd-sw--star" viewBox="-12 -12 24 24" aria-hidden="true"><circle r="11" style="fill:var(--ov-ring)"/><circle r="8.6" fill="none" style="stroke:var(--member)" stroke-width="3" stroke-dasharray="4 2.5"/></svg>Dashed red ring: a neighbour who shows only their grid square</p>`;
  }

  /* ------------------------------------------------------------ your licence (class, grant date) and where your pin is (§7.13) */
  /** your licence (/api/hams/{you}), shared with the You view through the shell's cache */
  function loadMyLicence() {
    if (!M.me()) return Promise.resolve(null);
    if (HD.me && HD.me.licence) return HD.me.licence().catch(() => null);
    return HD.api(`/api/hams/${encodeURIComponent(M.me().callsign)}`).catch(() => null);
  }
  /** "Your pin is " + phrase + reason (§7.13, §13): the shell's one rule (HD.me.pin, from location_source with the
   *  five-case test as its fallback), so your card and You > Map privacy always say the same thing. HTML, or null
   *  while the answer still depends on your licence (no location_source yet): never a guess that changes. */
  function pinPhrase(lic, haveLic) {
    const me = M.me() || {};
    if (!HD.me || !HD.me.pin) return null;
    if (!haveLic && me.map_precision !== "grid" && !me.location_source) return null;
    const pin = HD.me.pin(me, lic || null);
    if (!pin || pin.kind === "none" || !pin.phrase) return null;
    return [esc(pin.phrase), esc(pin.reason || ".")];
  }

  /* ------------------------------------------------------------ cards (§7.11) */
  function card(it) {
    const me = M.me() || {};
    if (it.kind === "you") {
      const y = M.youPlace(), lic = it.lic || {};
      const pp = pinPhrase(it.lic, "lic" in it);
      const granted = M.parseUS(lic.granted);
      // until your licence arrives (it decides two of the five cases when location_source is missing) the line waits
      const sub = pp ? `Your pin is ${pp[0]}${pp[1]} Neighbours never see your street address or email as text.`
        : y ? "Neighbours never see your street address or email as text." : "Add your grid square or address in You to put your pin on the map.";
      const cls = it.lic ? (lic.license_class != null ? M.classShort(lic.license_class) : "Not listed") : "";
      return `${K.head(`<span class="hd-mk-you${y && y.approx ? " hd-mk-you--approx" : ""}" style="display:block;position:relative;width:18px;height:18px"><span class="hd-mk-you__halo"></span><span class="hd-mk-you__dot"></span></span>`, esc(me.callsign), "hd-call hd-detail__call hd-call--member", M.spell(me.callsign))}
        <p class="hd-detail__lede">This is you</p><p class="hd-detail__sub">${sub}</p>
        <div class="hd-detail__section">${K.qso([["Grid", esc((y && y.grid) || M.gridFmt(me.grid) || "Not listed")], ["Class", esc(cls)],
          ["Range", `${esc(me.range_miles)}${NB}mi`], ["Licensed", granted ? M.monYear(granted) + (lic.new ? " <small>new</small>" : "") : it.lic ? "Not listed" : ""]])}</div>
        <div class="hd-detail__actions"><button class="hd-btn hd-btn--sm" type="button" data-goto="${esc(JSON.stringify(["you", { focus: "privacy" }]))}">${M.ic("lock", 20)}Map privacy and profile</button></div>`;
    }
    if (it.kind === "member") {
      const m = it.m, b = brg(m);
      const heading = b == null ? "" : m.approx ? `about ${Math.round(b / 10) * 10 % 360}°` : `${Math.round(b)}°`;
      const posts = M.L("posts"), lp = posts && posts.def.newestBy ? posts.def.newestBy(m.callsign) : null;
      const note = m.approx
        ? `Shown at the centre of grid ${esc(m.grid)}, not at ${esc(m.first)}'s home. Distance is rounded to whole miles.`
        : `Shown at ${esc(m.first)}'s address. hamdoor never shows addresses as text.`;
      // license_class is null when FCC records have no row for them right now: say so, never guess "club"
      const known = m.license_class != null;
      const newDays = (HD.config && HD.config.new_ham_days) || 90;
      return `${K.head(K.memberSvg(m, 40), esc(m.callsign), "hd-call hd-detail__call hd-call--member", M.spell(m.callsign))}
        <p class="hd-detail__lede">${esc(m.first)} is on hamdoor</p><p class="hd-detail__sub">${known ? esc(M.className(m.license_class)) : "Licence class not listed"}${known && m.new ? `, licensed in the last ${newDays}${NB}days` : ""}</p>
        <div class="hd-detail__section">${K.qso([["Grid", esc(m.grid || "Not listed")], ["Class", known ? esc(M.classShort(m.license_class)) : "Not listed"],
          ["Distance", `${distTxt(m)}${b == null ? "" : ` <small>${M.dir8(b)}</small>`}`], ["Beam heading", heading]])}</div>
        <div class="hd-detail__actions"><button class="hd-btn hd-btn--primary hd-btn--sm" type="button" data-goto="${esc(JSON.stringify(["feed", { author: m.callsign }]))}">See ${esc(m.first)}'s posts</button><button class="hd-btn hd-btn--sm" type="button" data-copy="${esc(m.callsign)}">${M.ic("copy", 20)}Copy callsign</button></div>
        ${lp ? `<section class="hd-detail__section"><h3 class="hd-detail__h">Latest post</h3><button class="hd-xref" type="button" data-goto="${esc(JSON.stringify(["feed", {}, { sub: ["post", String(lp.id)] }]))}">${K.postBadge(lp.category, true)}<span class="hd-xref__t"><b>${esc(lp.title)}</b><span>${esc(K.catOf(lp.category)[0])}, ${esc(M.ago(lp.created_at))}</span></span></button></section>` : ""}
        ${K.note(note)}`;
    }
    if (it.kind === "mgroup") {
      return `${K.head(`<span class="hd-mk-cluster" style="width:36px;height:36px;font-size:17px;--c:var(--cluster-member);--ci:var(--cluster-member-ink)">${it.ms.length}</span>`, `${it.ms.length}${NB}neighbours here`, "hd-detail__title")}
        <p class="hd-detail__lede">These neighbours are too close together to show apart.</p>
        <ul class="hd-memberlist hd-detail__section">${it.ms.map((m) => M.ui.pickRow({ layer: "members", kind: "member", m })).join("")}</ul>`;
    }
    return "";
  }
  function cardLoad(it) {
    if (it.kind !== "you" || "lic" in it) return;
    loadMyLicence().then((lic) => { it.lic = lic || {}; M.ui.refreshDetail(it); });
  }
  function row(it) {
    const me = M.me() || {};
    if (it.kind === "you") return { lead: K.youDot(false, 16, false), title: `<span class="hd-call hd-call--list hd-call--member">${esc(me.callsign)}</span>`, meta: "You", trail: "" };
    if (it.kind === "member") return { lead: K.memberSvg(it.m, 28), title: `<span class="hd-call hd-call--list hd-call--member">${esc(it.m.callsign)}</span>`, meta: `${esc(it.m.first)}, on hamdoor`, trail: distTxt(it.m) };
    if (it.kind === "mgroup") return { lead: `<span class="hd-mk-cluster" style="width:26px;height:26px;--c:var(--cluster-member);--ci:var(--cluster-member-ink)">${it.ms.length}</span>`, title: `${it.ms.length}${NB}neighbours`, meta: it.ms.map((m) => esc(m.callsign)).join(", "), trail: "" };
    return null;
  }
  function searchRow(it, q) {
    const me = M.me() || {};
    if (it.kind === "you") return { lead: K.youDot(false, 16, false), title: `<span class="hd-call hd-call--member">${M.markMatch(me.callsign, q)}</span>`, meta: "You", trail: "" };
    return { lead: K.memberSvg(it.m, 28), title: `<span class="hd-call hd-call--member">${M.markMatch(it.m.callsign, q)}</span>`, meta: `${esc(it.m.first)}, on hamdoor`, trail: distTxt(it.m) };
  }
  function inView(data, ctx) {
    const b = M.screenBounds(), y = M.youPlace(), items = [];
    if (y && b.contains([y.lat, y.lon])) items.push({ layer: "members", kind: "you" });
    if (M.isOn("members")) for (const m of people()) if (b.contains([m.lat, m.lon])) items.push({ layer: "members", kind: "member", m });
    return { heading: "On hamdoor", items };
  }
  async function resolve(kind, id) {
    const me = M.me();
    if (kind === "you" || (me && String(id).toUpperCase() === me.callsign)) return me ? { layer: "members", kind: "you" } : null;
    // a refresh that fails leaves the last list the map knows (map-core.js); with no list at all there is no card
    try { await HD.map.neighbours(); } catch (e) { if (!M.neighboursKnown()) return null; }
    const m = M.neighbour(decodeURIComponent(id));
    return m && M.finite(m.lat) ? { layer: "members", kind: "member", m: person(m) } : null;
  }
  const selId = (it) => (it.kind === "you" ? "you:me" : it.kind === "member" ? `member:${it.m.callsign}` : it.kind === "mgroup" ? `member:${it.ms[0].callsign}` : "");
  const latlng = (it) => {
    if (it.kind === "you") { const y = M.youPlace(); return y ? [y.lat, y.lon] : null; }
    if (it.kind === "member") return [it.m.lat, it.m.lon];
    if (it.kind === "mgroup") return [it.lat, it.lon];
    return null;
  };
  /** the element a person is drawn with (their own mark, or the disc they merged into) */
  function markEl(it, st) {
    if (it.kind === "you") { const m = st.store.get("you"); return m ? m.getElement() : null; }
    const calls = it.kind === "member" ? [it.m.callsign] : it.ms.map((x) => x.callsign);
    for (const m of st.store.values()) if (m._hd.calls && calls.every((c) => m._hd.calls.includes(c))) return m.getElement();
    return null;
  }
  // 30 px keeps the ring off the red callsign; drawn where the mark is drawn (it may ride a rim)
  const ring = (it, st) => (it.kind === "you" ? null : { latlng: latlng(it), d: it.kind === "mgroup" ? 36 : 30, el: markEl(it, st) });

  HD.map.addLayer({
    id: "members", order: 20, title: "On hamdoor",
    defaults: { range: true },
    bbox: false, alwaysRender: true, // you are never hidden; the switch hides neighbours and the range ring
    render, canvas, counts, section, card, cardLoad, row, searchRow, inView, resolve, selId, latlng, markEl, ring,
    person,
  });
})();
