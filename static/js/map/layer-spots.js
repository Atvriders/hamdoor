/* hamdoor map 8/9: activators on the air outdoors. DESIGN_SYSTEM.md §3.5, §7.11, §9.1, §14.4.
   POTA park activators (a rounded green square with a tree) and SOTA summit
   activators (a green triangle) from GET /api/activity/pota and /sota, loaded when
   the map opens and every 2 minutes. About a hundred at a time, so never hidden:
   below z8 activators within 26 px share a count disc, below z10 they ride
   postmark rims after neighbours, and from z11 they carry a pill
   ("K0BWH 14.060 CW"). */
(function () {
  "use strict";
  const HD = window.HD;
  const L = window.L;
  const M = HD.map._;
  const K = M.marks;
  const NB = M.NB;
  const esc = M.esc;

  /** one shape for both sources: {kind, call, khz, mode, ref, name, lat, lon, spotter, comments, time} */
  function norm(kind, sp) {
    const lat = M.num(sp.lat), lon = M.num(sp.lon);
    return { kind, call: String(sp.activator || "").toUpperCase(), khz: M.num(sp.frequency_khz), mode: String(sp.mode || "").toUpperCase(),
      ref: kind === "pota" ? sp.park_ref || "" : sp.summit || "", name: kind === "pota" ? sp.park_name || "" : sp.summit_name || "",
      grid: sp.grid || "", lat, lon, spotter: sp.spotter || "", comments: sp.comments || "", time: sp.time || "" };
  }
  async function load(view, signal) {
    const get = (path) => HD.api(path, { signal }).then((d) => (d && d.spots) || [], (e) => { if (HD.isAbort(e)) throw e; return []; });
    const [pota, sota] = await Promise.all([get("/api/activity/pota"), get("/api/activity/sota")]);
    const out = [], seen = new Set();
    for (const [kind, list] of [["pota", pota], ["sota", sota]]) for (const sp of list) {
      const s = norm(kind, sp);
      if (!s.call || !M.finite(s.lat) || !M.finite(s.lon) || (s.lat === 0 && s.lon === 0)) continue;
      s.key = `${kind}:${s.call}:${s.ref}`;
      if (seen.has(s.key)) continue; // newest spot per activator and reference
      seen.add(s.key);
      out.push(s);
    }
    return { spots: out };
  }
  const shown = (data) => (data ? data.spots.filter((s) => M.prefs.spots[s.kind]) : []);
  const pill = (s) => `<span class="hd-pill"><b>${esc(s.call)}</b>${s.khz ? ` <span class="hd-num">${M.khzToMhz(s.khz)}</span>` : ""}${s.mode ? NB + esc(s.mode) : ""}</span>`;

  function render(data, ctx) {
    const want = new Map(), z = ctx.zoom;
    if (!data) return want;
    const b = ctx.bounds.pad(0.2);
    const vis = shown(data).filter((s) => b.contains([s.lat, s.lon]));
    const used = new Set();
    vis.forEach((a, i) => {
      if (used.has(i)) return;
      used.add(i);
      const grp = [a];
      if (z < M.MINZ.spotCluster) {
        const pa = ctx.toPoint(a.lat, a.lon);
        vis.forEach((c, j) => { if (!used.has(j)) { const pc = ctx.toPoint(c.lat, c.lon); if (Math.hypot(pa.x - pc.x, pa.y - pc.y) < 26) { used.add(j); grp.push(c); } } });
      }
      if (grp.length > 1) {
        const lat = grp.reduce((s, x) => s + x.lat, 0) / grp.length, lon = grp.reduce((s, x) => s + x.lon, 0) / grp.length;
        const zoomTo = () => M.map.fitBounds(L.latLngBounds(grp.map((x) => [x.lat, x.lon])).pad(0.4), { animate: !M.reduced() });
        want.set("sc:" + grp.map((x) => x.key).sort().join(","), { latlng: [lat, lon], size: [26, 26], anchor: ctx.rim ? ctx.rim.anchor(lat, lon, 26, 26) : undefined, pane: "hd-spots",
          html: `<div class="hd-mk-cluster" style="--c:var(--cluster-spot)">${grp.length}</div>`, onClick: () => zoomTo(),
          item: { layer: "spots", kind: "zoom", lat, lon, zoomTo, text: `${grp.length} activators on the air`, sub: "Zoom in to see each one." },
          title: `${grp.length} activators on the air. Select to zoom in.` });
      } else {
        const isSel = !!ctx.sel && ctx.sel.layer === "spots" && ctx.sel.s && ctx.sel.s.key === a.key;
        want.set(a.key, { latlng: [a.lat, a.lon], size: [22, 22], anchor: ctx.rim ? ctx.rim.anchor(a.lat, a.lon, 22, 22) : undefined, pane: "hd-spots", lbl: true, pri: isSel ? -5 : 3, always: isSel && z >= M.MINZ.spotPills, minZ: M.MINZ.spotPills,
          html: `<div class="hd-mk-spot hd-mk-spot--${a.kind}${isSel ? " is-selected" : ""}">${K.spotBadge(a.kind)}${pill(a)}</div>`, item: { layer: "spots", kind: "spot", s: a },
          title: `${a.call} activating ${a.name || a.ref}` });
      }
    });
    return want;
  }

  function counts(data) {
    if (!data) return { meta: "", chips: {} };
    const b = M.screenBounds(), c = { pota: 0, sota: 0 };
    let n = 0;
    for (const s of shown(data)) if (b.contains([s.lat, s.lon])) { c[s.kind]++; n++; }
    return { meta: `${n}${NB}${n === 1 ? "activator" : "activators"} in view`, chips: c };
  }
  function section() {
    return `<div class="hd-chips hd-layers__chips">${M.chip("spots/pota", "Parks", `<span class="hd-sw" style="border-radius:4px;background:var(--spot);box-shadow:none;display:grid;place-items:center;color:var(--spot-ink)">${K.glyph("tree", 12)}</span>`)}${M.chip("spots/sota", "Summits", '<svg class="hd-sw hd-sw--star" viewBox="0 0 24 22" aria-hidden="true"><path d="M12 2.4 22.4 20.2H1.6Z" style="fill:var(--spot)"/></svg>')}</div>`;
  }

  function card(it) {
    const s = it.s, pota = s.kind === "pota";
    const sub = pota ? [s.ref, s.grid].filter(Boolean).map(esc).join(", ") : esc(s.ref);
    const link = pota ? "https://pota.app/#/park/" + encodeURIComponent(s.ref) : "https://sotl.as/summits/" + s.ref.split("/").map(encodeURIComponent).join("/");
    const mhz = M.khzToMhz(s.khz);
    return `${K.head(`<span class="hd-mk-spot hd-mk-spot--${s.kind}" style="display:block;width:30px;height:30px">${K.spotBadge(s.kind)}</span>`, M.callHtml(s.call), "hd-call hd-detail__call", M.spell(s.call))}
      <p class="hd-detail__lede">Activating ${esc(s.name || s.ref)}</p>${sub ? `<p class="hd-detail__sub">${sub}</p>` : ""}
      <div class="hd-detail__section">${K.qso([["Frequency", mhz ? `${mhz} <small>MHz</small>` : "Not given"], ["Mode", esc(s.mode || "Not given")], ["Spotted", M.utc(s.time) || "Not given"], ["Distance", M.distDir(s.lat, s.lon) || "Not known"]])}</div>
      <div class="hd-detail__actions">${mhz ? `<button class="hd-btn hd-btn--primary hd-btn--sm" type="button" data-copy="${mhz}">${M.ic("copy", 20)}Copy frequency</button>` : ""}${s.ref ? K.ext(link, pota ? "pota.app" : "Summit page") : ""}</div>
      ${s.comments ? `<section class="hd-detail__section"><h3 class="hd-detail__h">Comment</h3><p class="hd-rptnotes">${esc(s.comments)}</p></section>` : ""}
      ${K.note(`Activators move on. This spot is from ${M.utc(s.time) || "earlier today"}${s.spotter ? `, reported by ${esc(s.spotter)}` : ""}.`)}
      <p class="hd-detail__source">${pota ? "Spots from pota.app, refreshed every minute." : "Spots from SOTAwatch (sota.org.uk), last 2 hours."}</p>`;
  }
  function row(it) {
    if (it.kind === "zoom") return { lead: `<span class="hd-mk-cluster" style="width:26px;height:26px;--c:var(--cluster-spot)">${K.glyph("plus", 14)}</span>`, title: esc(it.text), meta: esc(it.sub), trail: "" };
    const s = it.s, d = M.distFromHome(s.lat, s.lon), mhz = M.khzToMhz(s.khz);
    return { lead: `<span class="hd-mk-spot hd-mk-spot--${s.kind}" style="display:block">${K.spotBadge(s.kind)}</span>`, title: `<span class="hd-call hd-call--list">${esc(s.call)}</span>`,
      meta: [mhz ? `${mhz}${NB}MHz ${esc(s.mode)}`.trim() : esc(s.mode), esc(s.ref)].filter(Boolean).join(", "), trail: d == null ? "" : M.mi(d) };
  }
  function inView(data) {
    const b = M.screenBounds(), ss = shown(data).filter((s) => b.contains([s.lat, s.lon]));
    if (!ss.length) return null;
    return { heading: "On the air outdoors", items: ss.slice(0, 20).map((s) => ({ layer: "spots", kind: "spot", s })), more: ss.length > 20 ? `${ss.length - 20} more in view.` : "" };
  }
  async function resolve(kind, id) {
    // spot:<kind>:<call>
    const [k, call] = decodeURIComponent(id).split(":");
    const data = await M.waitData("spots", (d) => !!d);
    const s = data && data.spots.find((x) => x.kind === k && x.call === String(call || "").toUpperCase());
    return s ? { layer: "spots", kind: "spot", s } : null;
  }
  const selId = (it) => (it.kind === "spot" ? `spot:${it.s.kind}:${it.s.call}` : "");
  const latlng = (it) => (it.s ? [it.s.lat, it.s.lon] : [it.lat, it.lon]);
  const markKey = (it) => (it.s ? it.s.key : null);
  const ring = (it) => (it.kind === "spot" ? { latlng: latlng(it), d: 30, el: M.ui.markEl(it) } : null);

  HD.map.addLayer({
    id: "spots", order: 50, title: "On the air outdoors",
    defaults: { pota: true, sota: true },
    bbox: false, cacheMs: 120000, poll: 120000,
    load, render, counts, section, card, row, inView, resolve, selId, latlng, markKey, ring,
  });
})();
