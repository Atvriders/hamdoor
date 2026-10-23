/* hamdoor map 4/9: licensed hams from the FCC directory. DESIGN_SYSTEM.md §3.1, §3.5, §7.10, §7.11, §9.1 to §9.4.
   GET /api/hams/map answers clusters below z10 (and for dense views) or one
   point per distinct location with every licence there. Points become canvas
   marks (class dots, stars for new hams, hollow rings for expired licences,
   the shared-address copy); postmarks and ZIP groups are DOM marks. A person
   (you or a neighbour) is never drawn or listed as a licence: their red mark
   stands for them, matched by callsign, never by `registered`. */
(function () {
  "use strict";
  const HD = window.HD;
  const M = HD.map._;
  const K = M.marks;
  const NB = M.NB;
  const esc = M.esc;
  const RANK = { E: 0, G: 1, T: 2, O: 3 };
  const DRAW = { O: 0, T: 1, G: 2, E: 3 }; // rarer marks sit on top
  const byRank = (a, b) => (a.expired - b.expired) || (RANK[M.grp(a.license_class)] - RANK[M.grp(b.license_class)]) || (a.callsign < b.callsign ? -1 : 1);
  const canvasStore = new Map();
  let memo = { key: "", desired: new Map(), labels: [] };

  const newDays = () => (HD.config && HD.config.new_ham_days) || 90;

  /* ------------------------------------------------------------ the model: persons left out, ZIP points grouped */
  let modelMemo = { data: null, persons: "", model: null };
  function model(data) {
    const persons = [...M.personCalls].join(",");
    if (modelMemo.data === data && modelMemo.persons === persons) return modelMemo.model;
    const out = { type: data.type, addr: [], zip: [], cells: data.cells || [], cell: data.cell_degrees };
    if (data.type === "points") {
      const zips = new Map();
      for (const p of data.points || []) {
        const hams = (p.hams || []).filter((h) => !M.isPerson(h.callsign));
        if (!hams.length) continue;
        hams.sort(byRank);
        if (p.loc === "zip") {
          // points with the same lat/lon but another ZIP are drawn as one group
          const k = `${p.lat},${p.lon}`;
          let g = zips.get(k);
          if (!g) zips.set(k, (g = { lat: p.lat, lon: p.lon, zips: [], hams: [] }));
          if (p.zip && !g.zips.includes(p.zip)) g.zips.push(p.zip);
          g.hams.push(...hams);
        } else out.addr.push({ lat: p.lat, lon: p.lon, key: `${p.lat},${p.lon}`, hams, shared: !!p.shared });
      }
      for (const g of zips.values()) { g.hams.sort(byRank); out.zip.push(g); }
    }
    modelMemo = { data, persons, model: out };
    return out;
  }
  /** A member outside your range (registered: true, not you, not a neighbour) keeps the red ring and the red
      callsign (§3.1). Only a known neighbour list can say they are not a neighbour. */
  const farMember = (h) => !!h.registered && M.neighboursKnown() && !M.isPerson(h.callsign);
  /** what a point shows at a zoom: expired licences only from z12, where the hollow ring reads (§3.1) */
  const visibleHams = (hams, z) => (z >= M.MINZ.expired ? hams : hams.filter((h) => !h.expired));

  /* ------------------------------------------------------------ items */
  const fccItem = (h, lat, lon, loc, zip) => ({ layer: "fcc", kind: "fcc", h, lat, lon, loc: loc || "address", zip: zip || null });
  function pointItem(pt, vis) {
    return vis.length > 1 ? { layer: "fcc", kind: "addr", lat: pt.lat, lon: pt.lon, hams: vis, shared: pt.shared } : fccItem(vis[0], pt.lat, pt.lon, "address");
  }
  const zipItem = (g, vis) => ({ layer: "fcc", kind: "zip", lat: g.lat, lon: g.lon, zips: g.zips, hams: vis });

  /* ------------------------------------------------------------ query */
  function filterParams(p) {
    const parts = [];
    const cls = [];
    if (p.E) cls.push("E"); if (p.G) cls.push("G"); if (p.T) cls.push("T"); if (p.O) cls.push("A", "N", "U");
    if (cls.length < 6) parts.push("classes=" + cls.join(","));
    if (!p.isNew) parts.push("new=0");
    if (!p.expired) parts.push("expired=0");
    return parts.join("&");
  }

  /* ------------------------------------------------------------ render: postmarks and ZIP groups (DOM), dots (canvas) */
  function render(data, ctx) {
    const want = new Map();
    if (!data) return want;
    const z = ctx.zoom;
    if (data.type === "clusters") {
      const md = model(data);
      // count bubbles at the mean position of their members, never the cell centre
      let c = md.cell || M.cellDeg(z), cells = md.cells;
      const b = ctx.bounds.pad(0.25);
      if (data.reason === "dense") {
        // a dense answer at z10+ comes in cells 36 px apart, closer than the bubbles are wide: each 2 x 2
        // block of cells becomes one bubble at the count-weighted mean, which is still the mean of its members
        const merged = new Map();
        for (const cell of cells) {
          const iy = Math.floor(Math.floor(cell.lat / c) / 2), ix = Math.floor(Math.floor(cell.lon / c) / 2), k = iy + ":" + ix;
          let g = merged.get(k);
          if (!g) merged.set(k, (g = { count: 0, slat: 0, slon: 0 }));
          g.count += cell.count; g.slat += cell.lat * cell.count; g.slon += cell.lon * cell.count;
        }
        cells = [...merged.values()].map((g) => ({ lat: g.slat / g.count, lon: g.slon / g.count, count: g.count }));
        c *= 2;
      }
      for (const cell of cells) {
        if (!b.contains([cell.lat, cell.lon])) continue;
        const n = cell.count, d = n < 50 ? 28 : n < 500 ? 34 : n < 5000 ? 40 : n < 50000 ? 46 : 52;
        const tier = d >= 46 ? " hd-mk-postmark--xl" : d >= 40 ? " hd-mk-postmark--l" : "";
        const iy = Math.floor(cell.lat / c), ix = Math.floor(cell.lon / c);
        const zoomTo = () => M.map.fitBounds([[iy * c, ix * c], [(iy + 1) * c, (ix + 1) * c]], { maxZoom: Math.max(z + 2, M.MINZ.fccDots), animate: !M.reduced() });
        const item = { layer: "fcc", kind: "zoom", lat: cell.lat, lon: cell.lon, n, zoomTo, text: `${M.fmtN(n)} licensed hams`, sub: "Counted by area. Zoom in to see each one." };
        want.set(`b${c}:${iy}:${ix}`, { latlng: [cell.lat, cell.lon], size: [d, d], pane: "hd-clusters", html: `<div class="hd-mk-postmark${tier}">${M.compact(n)}</div>`,
          title: `${M.fmtN(n)} licensed hams. Select to zoom in.`, item, onClick: () => zoomTo(), postmark: true });
        const p = ctx.toPoint(cell.lat, cell.lon);
        ctx.postmarks.push({ x: p.x, y: p.y, r: d / 2 });
      }
      return want;
    }
    // points name people: until the neighbour list is known, a neighbour's licence could pass for anyone's (§3.1)
    if (z < M.MINZ.zipGroups || !M.neighboursKnown()) return want;
    const md = model(data);
    for (const g of md.zip) {
      const vis = visibleHams(g.hams, z);
      if (!vis.length) continue;
      const n = vis.length, d = n === 1 ? 24 : Math.min(40, 26 + Math.round(Math.sqrt(n) * 2.5));
      const inner = n === 1 ? `<span class="hd-mk-zip__dot" style="--c:${M.CLASS_VAR[M.grp(vis[0].license_class)]}"></span>` : String(n);
      const zipTxt = g.zips.join(" and ");
      want.set(`z:${g.lat},${g.lon}`, { latlng: [g.lat, g.lon], size: [d, d], pane: "hd-zip", html: `<div class="hd-mk-zip">${inner}</div>`,
        title: `${n} ${n === 1 ? "licence" : "licences"} placed by ZIP code ${zipTxt}`, item: zipItem(g, vis) });
    }
    return want;
  }
  function canvas(data, ctx, st) {
    const z = ctx.zoom;
    const on = !!data && M.isOn("fcc") && z >= M.MINZ.fccDots && data.type === "points" && M.neighboursKnown();
    if (!on) { if (canvasStore.size) K.syncCanvas(canvasStore, new Map(), M.R.fcc); st.labelPoints = []; memo = { key: "", desired: new Map(), labels: [] }; return; }
    const md = model(data);
    const key = `${z >= M.MINZ.expired ? 1 : 0}|${modelMemo.persons}`;
    if (memo.model !== md || memo.key !== key) {
      const desired = new Map(), labels = [];
      for (const pt of md.addr) {
        const vis = visibleHams(pt.hams, z);
        if (!vis.length) continue;
        const h = vis[0], g = M.grp(h.license_class), star = !!h.new, exp = !!h.expired, stack = vis.length > 1;
        // members outside your range keep their class dot with a red ring (§3.1)
        const mem = farMember(h) && !exp;
        const rank = (star ? 2 : exp ? 1 : 0) * 4 + DRAW[g];
        desired.set(pt.key, { latlng: [pt.lat, pt.lon], g, star, exp, stack, mem, rank, sig: `${g}${+star}${+exp}${+stack}${+mem}`, item: pointItem(pt, vis) });
        labels.push({ latlng: [pt.lat, pt.lon], call: h.callsign, star, exp, mem, n: vis.length, hash: M.hash32(h.callsign) });
      }
      memo = { model: md, key, desired, labels };
    }
    K.syncCanvas(canvasStore, memo.desired, M.R.fcc);
    st.labelPoints = memo.labels;
  }

  /* ------------------------------------------------------------ counts (§7.10): by_class at every zoom, flags from z10 */
  function counts(data) {
    if (!data) return { meta: "", chips: {} };
    if (data.type === "clusters") {
      const b = data.by_class || {}, o = (b.A || 0) + (b.N || 0) + (b.U || 0);
      return { meta: `About ${M.compact(data.total || 0)} in view, counted by area`, chips: { T: M.compact(b.T || 0), G: M.compact(b.G || 0), E: M.compact(b.E || 0), O: M.compact(o), isNew: null, expired: null } };
    }
    if (!M.neighboursKnown()) return { meta: "", chips: {} }; // nothing is drawn yet (§3.1)
    const md = model(data), bounds = M.screenBounds();
    const c = { T: 0, G: 0, E: 0, O: 0, isNew: 0, expired: 0 };
    let total = 0;
    const tally = (hams) => { for (const h of hams) { c[M.grp(h.license_class)]++; if (h.new) c.isNew++; if (h.expired) c.expired++; total++; } };
    for (const pt of md.addr) if (bounds.contains([pt.lat, pt.lon])) tally(pt.hams);
    for (const g of md.zip) if (bounds.contains([g.lat, g.lon])) tally(g.hams);
    return { meta: `${M.fmtN(total)} in view, from FCC records`, chips: c };
  }
  const starSw = () => `<svg class="hd-sw hd-sw--star" viewBox="-10 -10 20 20" aria-hidden="true"><path d="${K.starPath(8.6)}" style="fill:var(--fcc-tech);stroke:var(--ov-ring)" stroke-width="1.2" stroke-linejoin="round"/></svg>`;
  function section() {
    return `<div class="hd-chips hd-layers__chips">${M.chip("fcc/T", "Technician", M.sw("var(--fcc-tech)"))}${M.chip("fcc/G", "General", M.sw("var(--fcc-general)"))}${M.chip("fcc/E", "Extra", M.sw("var(--fcc-extra)"))}${M.chip("fcc/O", "Other", M.sw("var(--fcc-other)"))}
        ${M.chip("fcc/isNew", `New, last ${newDays()}${NB}days`, starSw())}${M.chip("fcc/expired", "Expired, in grace", '<span class="hd-sw hd-sw--ring"></span>')}</div>
      <p class="hd-layers__note"><span class="hd-sw hd-sw--dashed"></span>Dashed ring: placed by ZIP code, not a street address</p>
      <p class="hd-layers__note"><span class="hd-mk-stack" aria-hidden="true">3</span>Number tag: licensees who share one street address</p>
      <p class="hd-layers__note">Other covers Advanced, Novice, Technician Plus and club licences. Expired licences show only when you zoom in close.</p>`;
  }

  /* ------------------------------------------------------------ cards (§7.11) */
  const detailCache = new Map();
  function fetchDetail(call) {
    if (detailCache.has(call)) return detailCache.get(call);
    const p = HD.api(`/api/hams/${encodeURIComponent(call)}`).catch((e) => { detailCache.delete(call); throw e; });
    detailCache.set(call, p);
    return p;
  }
  M.hamDetail = fetchDetail;
  function hamRow(h) {
    return K.row(`data-ham="${esc(h.callsign)}"`, K.classBadge(h.license_class, h.new),
      `<span class="hd-call hd-call--list${farMember(h) ? " hd-call--member" : ""}">${esc(h.callsign)}</span>`, esc(h.name),
      esc(M.classShort(h.license_class)), h.expired ? "Expired" : h.new ? "New" : "");
  }
  function card(it) {
    const range = M.me() ? M.me().range_miles : 25;
    if (it.kind === "fcc") {
      const h = it.h, d = it.detail || {}, mem = farMember(h);
      const lic = M.parseUS(h.granted || d.granted), exp = M.parseUS(d.expires || h.expires);
      const notes = [];
      if (h.new && lic) notes.push(`New ham: licensed ${M.daysAgo(lic)}${NB}${M.daysAgo(lic) === 1 ? "day" : "days"} ago.`);
      if (h.expired) notes.push(exp ? `This licence expired on ${M.longDate(exp)}. It is in the two-year grace period, when it can still be renewed.` : "This licence has expired. It is in the two-year grace period, when it can still be renewed.");
      const zip = it.zip || d.zip || (it.loc === "zip" ? h.zip : null);
      notes.push(it.loc === "zip" ? `Placed at the centre of ZIP code ${esc(zip || "")}. The licence has no street address we could match.` : "Shown at the mailing address on the FCC licence.");
      if (mem) notes.push(`On hamdoor. They're farther away than your ${range}-mile range, so their posts don't show in your feed.`);
      const place = [h.city || d.city, h.state || d.state].filter(Boolean).map(esc).join(", ");
      return `${K.head(K.classBadge(h.license_class, h.new), M.callHtml(h.callsign), "hd-call hd-detail__call" + (mem ? " hd-call--member" : ""), M.spell(h.callsign))}
        <p class="hd-detail__lede">${esc(h.name || d.name || "")}</p>${place ? `<p class="hd-detail__sub">${place}</p>` : ""}
        <div class="hd-detail__section">${K.qso([["Grid", M.finite(it.lat) ? M.grid(it.lat, it.lon, 6) : "Not listed"], ["Class", esc(M.className(h.license_class))],
          ["Licensed", M.monYear(lic) + (h.new ? " <small>new</small>" : "")], ["Distance", M.distDir(it.lat, it.lon) || "Not known"]])}</div>
        ${notes.map(K.note).join("")}
        <div class="hd-detail__actions"><button class="hd-btn hd-btn--sm" type="button" data-copy="${esc(h.callsign)}">${M.ic("copy", 20)}Copy callsign</button>${K.ext("https://www.qrz.com/db/" + encodeURIComponent(h.callsign), "QRZ.com")}</div>`;
    }
    if (it.kind === "addr") {
      const n = it.hams.length, h0 = it.hams[0];
      const d = M.distFromHome(it.lat, it.lon);
      return `${K.head(`<span class="hd-mk-stack" style="min-width:36px;height:36px;border-radius:18px;font-size:17px;padding:0 8px">${n}</span>`, "Shared address", "hd-detail__title")}
        <p class="hd-detail__lede">${n}${NB}licensees at one street address</p><p class="hd-detail__sub">${[h0.city, h0.state].filter(Boolean).map(esc).join(", ")}${d != null ? `. ${M.mi(d)} from you.` : ""}</p>
        ${K.note(`${n >= 10 || it.shared ? "Clubs, offices and mail services often share an address." : "Families often share an address."} Every licence here is listed below.`)}
        <ul class="hd-memberlist hd-detail__section">${it.hams.map(hamRow).join("")}</ul>`;
    }
    if (it.kind === "zip") {
      const n = it.hams.length, place = it.hams.find((h) => h.city) || {};
      return `${K.head(`<span class="hd-mk-zip" style="width:40px;height:40px">${n}</span>`, "Placed by ZIP code", "hd-detail__title")}
        <p class="hd-detail__lede">${n}${NB}${n === 1 ? "licensee" : "licensees"} in ${esc(it.zips.join(" and ") || "this ZIP code")}${place.city ? `, ${esc(place.city)}` : ""}</p>
        ${K.note("These licences list a PO box or an address the census could not match to a street, so they are drawn at the centre of the ZIP code, not at a home.")}
        <ul class="hd-memberlist hd-detail__section">${it.hams.map(hamRow).join("")}</ul>`;
    }
    return "";
  }
  /** a licence card fetches its dates (grant, expiry) when it opens */
  function cardLoad(it) {
    if (it.kind !== "fcc" || it.detail) return;
    fetchDetail(it.h.callsign).then((d) => {
      it.detail = d || {};
      if (d) { if (!it.h.granted) it.h.granted = d.granted; if (it.h.name == null) it.h.name = d.name; }
      M.ui.refreshDetail(it);
    }, () => {});
  }
  function onCardClick(e, sel) {
    const hm = e.target.closest("[data-ham]");
    if (!hm) return false;
    const call = hm.dataset.ham;
    const h = (sel.hams || []).find((x) => x.callsign === call);
    if (h) M.ui.openDetail(fccItem(h, sel.lat, sel.lon, sel.kind === "zip" ? "zip" : "address", sel.kind === "zip" ? sel.zips[0] : null), { keyboard: e.detail === 0 });
    return true;
  }
  function row(it) {
    if (it.kind === "fcc") {
      const d = M.distFromHome(it.lat, it.lon);
      return { lead: K.classBadge(it.h.license_class, it.h.new), title: `<span class="hd-call hd-call--list${farMember(it.h) ? " hd-call--member" : ""}">${esc(it.h.callsign)}</span>`,
        meta: esc(it.h.name || "") + (it.h.expired ? ", expired" : ""), trail: d == null ? "" : M.mi(d) };
    }
    if (it.kind === "addr") { const d = M.distFromHome(it.lat, it.lon); return { lead: `<span class="hd-mk-stack">${it.hams.length}</span>`, title: "Shared address", meta: `${it.hams.length}${NB}licensees`, trail: d == null ? "" : M.mi(d) }; }
    if (it.kind === "zip") return { lead: `<span class="hd-mk-zip" style="width:30px;height:30px">${it.hams.length}</span>`, title: `ZIP ${esc(it.zips[0] || "")}`, meta: "Placed by ZIP code", trail: "" };
    if (it.kind === "zoom") return { lead: `<span class="hd-mk-postmark" style="width:30px;height:30px">${K.glyph("plus", 16)}</span>`, title: esc(it.text), meta: esc(it.sub), trail: "" };
    return null;
  }
  function searchRow(it, q) {
    const h = it.h, d = M.distFromHome(it.lat, it.lon);
    return { lead: K.classBadge(h.license_class, h.new), title: `<span class="hd-call${farMember(h) ? " hd-call--member" : ""}">${M.markMatch(h.callsign, q)}</span>`,
      meta: [h.name, h.city].filter(Boolean).map(esc).join(", "), trail: d == null ? "" : M.mi(d) };
  }
  /** "In view" (§7.11): licences nearest the centre, 30, then a count of the rest */
  function inView(data, ctx) {
    if (!data || ctx.zoom < M.MINZ.fccDots || data.type !== "points") return { heading: "Licensed hams", items: [], more: "Zoom in to list licences one by one." };
    if (!M.neighboursKnown()) return null; // nothing is drawn yet: the list could name a neighbour's licence (§3.1)
    const md = model(data), b = M.screenBounds(), c = M.map.getCenter(), cp = { lat: c.lat, lon: c.lng }, z = ctx.zoom, all = [];
    for (const pt of md.addr) if (b.contains([pt.lat, pt.lon])) for (const h of visibleHams(pt.hams, z)) all.push(fccItem(h, pt.lat, pt.lon, "address"));
    if (z >= M.MINZ.zipGroups) for (const g of md.zip) if (b.contains([g.lat, g.lon])) for (const h of visibleHams(g.hams, z)) all.push(fccItem(h, g.lat, g.lon, "zip", g.zips[0]));
    const near = all.map((x) => ({ x, d: M.distMi(cp, x) })).sort((p, q) => p.d - q.d).slice(0, 30).map((p) => p.x);
    return { heading: "Licensed hams, nearest the centre", items: near, more: all.length > 30 ? `${M.fmtN(all.length - 30)} more licences in view. Zoom in or search to reach them.` : "" };
  }
  /** canvas licences near a finger tap (Choose a station) */
  function pick(x, y, r) {
    const out = [];
    if (!canvasStore.size) return out;
    const mr = M.map.getContainer().getBoundingClientRect(), b = M.map.getBounds().pad(0.05);
    for (const m of canvasStore.values()) {
      const ll = m.getLatLng();
      if (!b.contains(ll)) continue;
      const p = M.map.latLngToContainerPoint(ll), d = Math.hypot(p.x + mr.left - x, p.y + mr.top - y);
      if (d <= r) out.push({ d, item: m._hd.item });
    }
    return out;
  }
  /** the loaded point a licence is drawn at, if any */
  function findDrawn(call) {
    const data = M.data("fcc");
    if (!data || data.type !== "points") return null;
    const md = model(data);
    for (const pt of md.addr) if (pt.hams.some((h) => h.callsign === call)) return pt;
    for (const g of md.zip) if (g.hams.some((h) => h.callsign === call)) return g;
    return null;
  }
  async function resolve(kind, id) {
    id = decodeURIComponent(id || "").toUpperCase();
    // only the neighbour list says whether this licence is a person's (yours or a neighbour's), whose red card
    // opens instead, never their licence at their house (§3.1). A link that arrives before the list waits for it;
    // with no list at all, no licence card opens.
    if (!(await M.whenNeighbours())) return null;
    if (kind === "zip") {
      const data = await M.waitData("fcc", (d) => d && d.type === "points");
      if (!data) return null;
      const g = model(data).zip.find((x) => x.zips.includes(id));
      return g ? zipItem(g, visibleHams(g.hams, M.map.getZoom())) : null;
    }
    if (M.isPerson(id)) return M.L("members").def.resolve(id === (M.me() || {}).callsign ? "you" : "member", id);
    if (kind === "addr") {
      const data = await M.waitData("fcc", (d) => d && d.type === "points");
      const pt = data && model(data).addr.find((p) => p.hams.some((h) => h.callsign === id));
      if (pt) { const vis = visibleHams(pt.hams, M.map.getZoom()); if (vis.length) return pointItem(pt, vis); }
    }
    const d = await fetchDetail(id).catch(() => null);
    if (!d || !M.finite(d.lat) || !M.neighboursKnown()) return null;
    if (M.isPerson(d.callsign)) return M.L("members").def.resolve(d.callsign === (M.me() || {}).callsign ? "you" : "member", d.callsign);
    // when the licence is drawn, its card, ring and label use the drawn point (the map answer is rounded to 6 decimals)
    const drawn = findDrawn(d.callsign);
    const it = fccItem({ callsign: d.callsign, name: d.name, city: d.city, state: d.state, license_class: d.license_class, expired: d.expired, new: d.new, granted: d.granted, registered: d.registered },
      drawn ? drawn.lat : d.lat, drawn ? drawn.lon : d.lon, d.loc, d.zip);
    it.detail = d;
    return it;
  }
  /** The neighbour list changed while a licence card is open: a person's licence never stays open as a licence.
   *  Returns the item to show instead (the same one, a person's card, a group without them), or null to close. */
  function onPersons(it) {
    if (it.kind === "fcc") {
      if (!M.isPerson(it.h.callsign)) return it;
      const me = M.me();
      return M.L("members").def.resolve(me && it.h.callsign === me.callsign ? "you" : "member", it.h.callsign);
    }
    if (it.kind === "addr" || it.kind === "zip") {
      if (!it.hams.some((h) => M.isPerson(h.callsign))) return it;
      const hams = it.hams.filter((h) => !M.isPerson(h.callsign));
      if (!hams.length) return null;
      if (it.kind === "addr" && hams.length === 1) return fccItem(hams[0], it.lat, it.lon, "address");
      return Object.assign({}, it, { hams });
    }
    return it;
  }
  const selId = (it) => (it.kind === "fcc" ? `fcc:${it.h.callsign}` : it.kind === "addr" ? `addr:${it.hams[0].callsign}` : it.kind === "zip" ? `zip:${it.zips[0] || ""}` : "");
  const latlng = (it) => (M.finite(it.lat) && M.finite(it.lon) ? [it.lat, it.lon] : null);
  const markKey = (it) => (it.kind === "zip" ? `z:${it.lat},${it.lon}` : null);
  const ring = (it) => {
    const ll = latlng(it);
    if (!ll || it.kind === "zoom") return null;
    return { latlng: ll, d: it.kind === "zip" ? 46 : it.kind === "addr" ? 32 : 30, el: it.kind === "zip" ? M.ui.markEl(it) : null };
  };
  function hint(z, st) {
    if (z < M.MINZ.fccDots) return ["every ham", M.MINZ.fccDots, 3];
    if (st.data && st.data.type === "clusters" && st.data.reason === "dense") return ["every ham", z + 1, 3];
    return null;
  }

  HD.map.addLayer({
    id: "fcc", order: 10, title: "Licensed hams", minZoom: 10,
    defaults: { T: true, G: true, E: true, O: true, isNew: true, expired: true },
    filterKey: (p) => filterParams(p),
    // a dense answer at z10+ is merged 2 x 2 (render): ask for whole blocks
    snapCells: (z) => (z >= M.MINZ.fccDots ? 2 : 1),
    load: (view, signal) => HD.api(`/api/hams/map?${M.bboxQuery(view)}${view.filterKey ? "&" + view.filterKey : ""}`, { signal }),
    render, canvas, counts, section, hint, card, cardLoad, onCardClick, row, searchRow, inView, pick, resolve, onPersons, selId, latlng, markKey, ring,
    fccItem,
  });
})();
