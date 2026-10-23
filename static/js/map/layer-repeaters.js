/* hamdoor map 6/9: repeaters. DESIGN_SYSTEM.md §3.3, §7.10, §7.11, §9.1.
   GET /api/repeaters/map: clusters below z10 (they only feed the layer count),
   machines from z10. A machine is a sign: a band cap with the number hams say
   aloud (2, 70, 220, 6) and a plate with the output and its offset sign
   (146.940 −); tone and mode join the plate from z13. Machines on one point or
   whose caps would touch merge into one assembly: up to three tags, then
   "+N more". Approximate sites get a dashed outline. When the plates do not fit
   to the right the whole assembly moves left, cap first, with a leader and a pin. */
(function () {
  "use strict";
  const HD = window.HD;
  const M = HD.map._;
  const K = M.marks;
  const NB = M.NB;
  const esc = M.esc;
  const ORDER = { "2m": 0, "70cm": 1, other: 2 };
  const BAND_NAME = { "10m": "10 m", "6m": "6 m", "4m": "4 m", "2m": "2 m", "1.25m": "1.25 m", "70cm": "70 cm", "33cm": "33 cm", "23cm": "23 cm",
    "13cm": "13 cm", "9cm": "9 cm", "6cm": "6 cm", "3cm": "3 cm", other: "Other" };
  /** A listing with no output frequency (an IRLP reflector, a network-only DMR id) has nothing to tune to and
   *  nothing to print on a plate, so the map leaves it out (its counts too); everything else is drawn. */
  const tunable = (r) => M.finite(r.output_mhz) && r.output_mhz > 0;
  /** "2 m", "70 cm" (HTML: the band comes from the directory, so anything unexpected is escaped) */
  const bandName = (b) => esc(BAND_NAME[b] || String(b || "")).replace(" ", NB);
  const MODE_LABEL = { FM: "FM", DMR: "DMR", YSF: "Fusion", DSTAR: "D-STAR", P25: "P25", NXDN: "NXDN", M17: "M17", ATV: "ATV", TETRA: "TETRA", OTHER: "Other" };
  const approxOf = (r) => r.location_quality === "city" || r.location_quality === "approximate";
  /** plates print digital modes; FM prints only its tone (§3.3) */
  const modeText = (r) => (r.modes || []).filter((m) => m !== "FM").map((m) => MODE_LABEL[m] || m).join(" ");
  const modeLong = (r) => { const ms = (r.modes || []).map((m) => (m === "FM" ? "Analog FM" : MODE_LABEL[m] || m)); return ms.length ? ms.join(" and ") : "Mode not listed"; };
  /** T plus Hz, D plus the DCS code, CC plus the colour code, NAC plus the code (§4) */
  const toneText = (r) => {
    if (M.finite(r.tone_up)) return `T${Number(r.tone_up).toFixed(1)}`;
    if (r.dcs != null && r.dcs !== "") return `D${String(r.dcs).padStart(3, "0")}`;
    if (r.dmr_color_code != null) return `CC${r.dmr_color_code}`;
    if (r.p25_nac != null && r.p25_nac !== "") return `NAC${r.p25_nac}`;
    if (r.nxdn_ran != null) return `RAN${r.nxdn_ran}`;
    if (r.m17_can != null) return `CAN${r.m17_can}`;
    return "";
  };
  const rptLine = (r) => { const s = M.offSign(r.offset_mhz); return `${M.freq(r.output_mhz)}${s ? M.THIN + s : ""}`; };
  M.rptLine = rptLine;
  M.rptTone = toneText;

  /* ------------------------------------------------------------ query: bands, modes, off air, simplex */
  function filterParams(p) {
    const parts = [];
    if (!(p["2m"] && p["70cm"] && p.other)) {
      const b = [];
      if (p["2m"]) b.push("2m"); if (p["70cm"]) b.push("70cm"); if (p.other) b.push("10m", "6m", "1.25m", "33cm", "23cm", "other");
      parts.push("bands=" + b.join(","));
    }
    // FM, DMR, Fusion and D-STAR are chips; P25, NXDN, M17, ATV and the rest always ride along
    if (!(p.FM && p.DMR && p.Fusion && p.DSTAR)) {
      const m = [];
      if (p.FM) m.push("FM"); if (p.DMR) m.push("DMR"); if (p.Fusion) m.push("YSF"); if (p.DSTAR) m.push("DSTAR");
      m.push("P25", "NXDN", "M17", "ATV", "OTHER");
      parts.push("modes=" + m.join(","));
    }
    if (p.offair) parts.push("onair=0");
    if (p.simplex) parts.push("simplex=1");
    return parts.join("&");
  }

  /* ------------------------------------------------------------ render */
  let selId = null;
  function render(data, ctx) {
    const want = new Map(), z = ctx.zoom;
    selId = ctx.sel && ctx.sel.layer === "rpt" ? (ctx.sel.kind === "rpt" ? ctx.sel.r.id : null) : null;
    if (!data || data.type !== "repeaters" || z < M.MINZ.rpt) return want;
    const compact = z < M.MINZ.rptPlates, b = ctx.bounds.pad(0.25);
    // sites: one point; then assemblies: caps that would touch (26 px, 20 px for compact caps)
    const sites = new Map();
    for (const r of data.repeaters || []) {
      if (!M.finite(r.lat) || !M.finite(r.lon) || !tunable(r) || !b.contains([r.lat, r.lon])) continue;
      const k = r.lat.toFixed(5) + "," + r.lon.toFixed(5);
      let s = sites.get(k);
      if (!s) sites.set(k, (s = { key: k, lat: r.lat, lon: r.lon, approx: false, items: [] }));
      s.items.push(r); s.approx = s.approx || approxOf(r);
    }
    const groups = [];
    for (const s of sites.values()) {
      const p = ctx.toPoint(s.lat, s.lon);
      const near = groups.find((g) => Math.hypot(g.p.x - p.x, g.p.y - p.y) < (compact ? 20 : 26));
      if (near) { near.items.push(...s.items); near.approx = near.approx || s.approx; near.keys.push(s.key); }
      else groups.push({ site: s, p, items: s.items.slice(), approx: s.approx, keys: [s.key] });
    }
    for (const g of groups) {
      const items = g.items.sort((a, c) => (ORDER[K.bandKey(a.band)] - ORDER[K.bandKey(c.band)]) || a.output_mhz - c.output_mhz);
      const shown = items.slice(0, compact ? 1 : 3), more = items.length - shown.length, first = items[0];
      const tags = shown.map((r) => {
        const x = z >= M.MINZ.rptTone ? [toneText(r), modeText(r)].filter(Boolean).join(" ") : "";
        return `<div class="hd-mk-rpt__tag${r.id === selId ? " is-selected" : ""}" data-rpt="${esc(r.id)}"><span class="hd-mk-rpt__cap hd-mk-rpt__cap--${K.bandKey(r.band)}">${esc(K.bandNum(r.band))}</span><span class="hd-mk-rpt__plate">${rptLine(r)}${x ? `<span class="hd-mk-rpt__x">${esc(x)}</span>` : ""}</span></div>`;
      }).join("");
      const moreHtml = more > 0 ? `<span class="hd-mk-rpt__more">+${more}${compact ? "" : " more"}</span>` : "";
      const cls = `hd-mk-rpt${compact ? " hd-mk-rpt--compact" : ""}${g.approx ? " hd-mk-rpt--approx" : ""}`;
      const pin = compact ? "" : `<span class="hd-mk-rpt__lead"></span><span class="hd-mk-rpt__pin hd-mk-rpt__pin--${K.bandKey(first.band)}"></span>`;
      const site = { lat: g.site.lat, lon: g.site.lon, approx: g.approx };
      const item = items.length === 1 ? { layer: "rpt", kind: "rpt", r: first } : { layer: "rpt", kind: "site", items, lat: site.lat, lon: site.lon, approx: g.approx };
      const isSel = !!ctx.sel && ctx.sel.layer === "rpt" && items.some((r) => r === ctx.sel.r || (ctx.sel.r && r.id === ctx.sel.r.id) || (ctx.sel.items && ctx.sel.items[0] && ctx.sel.items[0].id === r.id));
      want.set("s:" + g.keys.sort().join("|"), {
        latlng: [site.lat, site.lon], size: compact ? [18, 18] : [22, 22], pane: "hd-rpt", lbl: true, pri: isSel ? -5 : 2, always: isSel, z: isSel ? 800 : 0, noFlip: compact, ids: items.map((r) => r.id),
        html: `${pin}<div class="${cls}">${tags}${moreHtml}</div>`, item,
        title: items.length > 1 ? `${items.length} repeaters at this spot, first ${M.freq(first.output_mhz)} megahertz` : `${M.freq(first.output_mhz)} megahertz repeater, ${first.callsign || ""}`,
        onClick: (e, opts) => {
          const t = e && e.originalEvent && e.originalEvent.target && e.originalEvent.target.closest && e.originalEvent.target.closest("[data-rpt]");
          const r = t && !compact ? items.find((x) => x.id === t.dataset.rpt) : null;
          M.ui.openDetail(r ? { layer: "rpt", kind: "rpt", r } : item, opts);
        },
      });
    }
    return want;
  }

  /* ------------------------------------------------------------ panel */
  function counts(data, ctx) {
    if (!data) return { meta: "", chips: {} };
    if (data.type !== "repeaters") return { meta: `${M.fmtN(data.total || 0)} in view. Zoom in to see them`, chips: {} };
    const b = M.screenBounds(), c = { "2m": 0, "70cm": 0, other: 0, FM: 0, DMR: 0, Fusion: 0, DSTAR: 0, offair: 0, simplex: 0 };
    let n = 0;
    for (const r of data.repeaters || []) {
      if (!tunable(r) || !b.contains([r.lat, r.lon])) continue;
      n++; c[K.bandKey(r.band)]++;
      for (const m of r.modes || []) { if (m === "FM") c.FM++; else if (m === "DMR") c.DMR++; else if (m === "YSF") c.Fusion++; else if (m === "DSTAR") c.DSTAR++; }
      if (r.status === "off-air") c.offair++;
      if (r.kind === "simplex_link") c.simplex++;
    }
    return { meta: data.truncated ? `${M.fmtN(data.total)}+ in view` : `${M.fmtN(n)} in view`, chips: c };
  }
  const capSw = (band, num) => `<span class="hd-sw hd-sw--cap" style="--c:var(--rpt-${band});--ci:var(--rpt-${band}-ink)">${num}</span>`;
  function section() {
    return `<div class="hd-chips hd-layers__chips">${M.chip("rpt/2m", `2${NB}m`, capSw("2m", "2"))}${M.chip("rpt/70cm", `70${NB}cm`, capSw("70cm", "70"))}${M.chip("rpt/other", "Other bands", capSw("other", "6"))}</div>
      <div class="hd-chips hd-layers__chips">${M.chip("rpt/FM", "FM")}${M.chip("rpt/DMR", "DMR")}${M.chip("rpt/Fusion", "Fusion")}${M.chip("rpt/DSTAR", "D-STAR")}</div>
      <div class="hd-chips hd-layers__chips">${M.chip("rpt/offair", "Listed off the air")}${M.chip("rpt/simplex", "Linked simplex nodes")}</div>
      <p class="hd-layers__note"><span class="hd-sw hd-sw--dashed" style="border-radius:3px"></span>Dashed outline: listed at the city centre</p>`;
  }
  const hint = (z) => (z < M.MINZ.rpt ? ["repeaters", M.MINZ.rpt, 2] : null);

  /* ------------------------------------------------------------ cards (§7.11) */
  let status = null;
  const loadStatus = () => (status = status || HD.api("/api/repeaters/status").catch(() => { status = null; return null; }));
  const SOURCE = { hearham: ["HearHam", "a community repeater directory"], brandmeister: ["BrandMeister", "the DMR network's own list"], irlp: ["IRLP", "the Internet Radio Linking Project's node list"] };
  function linksHtml(r) {
    const l = r.links || {}, out = [];
    if (l.echolink) out.push(`EchoLink node ${esc(l.echolink)}`);
    if (l.allstar) out.push(`AllStar node ${esc(l.allstar)}`);
    if (l.irlp) out.push(`IRLP node ${esc(l.irlp)}`);
    if (l.wiresx) out.push(`WIRES-X ${esc(l.wiresx)}`);
    if (r.dmr_id) out.push(`DMR ID ${esc(r.dmr_id)}${r.dmr_network ? ` on ${esc(r.dmr_network)}` : ""}`);
    return out.length ? `<section class="hd-detail__section"><h3 class="hd-detail__h">Links</h3><ul class="hd-rptlinks">${out.map((t) => `<li>${t}</li>`).join("")}</ul></section>` : "";
  }
  /** The QSO box: Output, Input, Offset / Tone, Mode, Band / Distance (§7.11), three columns where they fit (K.qso
   *  falls back to two and widens a value that does not fit). With no tone listed the box has no Tone cell: the sub
   *  line says "no tone listed" instead of a cell that only says "Not listed". A machine with two or more modes
   *  ("FM, Fusion") gives Mode the wide last row. */
  function qsoCells(r, tone, offTxt) {
    const modes = (r.modes || []).map((m) => MODE_LABEL[m] || m);
    const mode = ["Mode", esc(modes.join(", ") || "Not listed")];
    const cells = [["Output", M.freq(r.output_mhz)], ["Input", M.finite(r.input_mhz) ? M.freq(r.input_mhz) : "Not listed"], ["Offset", offTxt]];
    if (tone) cells.push(["Tone", esc(tone)]);
    const dist = ["Distance", M.distDir(r.lat, r.lon) || "Not known"];
    if (modes.length > 1) return cells.concat([["Band", bandName(r.band)], dist, [mode[0], mode[1], true]]);
    return cells.concat([mode, ["Band", bandName(r.band)], dist]);
  }
  function card(it) {
    if (it.kind === "site") {
      const items = it.items, first = items[0];
      return `${K.head(K.capHtml(first, true), `${items.length}${NB}repeaters here`, "hd-detail__title")}
        <p class="hd-detail__lede">${it.approx ? `Listed at the centre of ${esc(first.city || "the town")}` : "On one site"}</p>
        ${it.approx ? K.note("The directory gives only the town for these machines, so they share one approximate point.") : ""}
        <ul class="hd-memberlist hd-detail__section">${items.map((r) => M.ui.pickRow({ layer: "rpt", kind: "rpt", r }, bandName(r.band))).join("")}</ul>`;
    }
    const r = it.detail || it.r;
    const tone = toneText(r), off = r.offset_mhz;
    const offTxt = !M.finite(off) || Math.abs(off) < 1e-6 ? "Simplex" : `${off > 0 ? "+" : M.MINUS}${Math.abs(off).toFixed(3)}`;
    const copyTxt = [M.freq(r.output_mhz), M.finite(off) && Math.abs(off) > 1e-6 ? `${off > 0 ? "+" : "-"}${Math.abs(off).toFixed(3)}` : "simplex", tone].filter(Boolean).join(" ");
    const src = SOURCE[r.source] || [r.source || "the directory", ""];
    const tg = (it.detail && it.detail.talkgroups) || [];
    const st = it.status;
    const rbUrl = st && st.repeaterbook_near_url ? st.repeaterbook_near_url.replace("{lat}", encodeURIComponent(r.lat.toFixed(4))).replace("{lon}", encodeURIComponent(r.lon.toFixed(4))) : null;
    const also = (r.also_in || []).filter((a) => HD.safeUrl(a.url)).map((a) => `<a class="hd-link" href="${esc(HD.safeUrl(a.url))}" target="_blank" rel="noopener">${esc((SOURCE[a.source] || [a.source])[0])}</a>`);
    const srcUrl = HD.safeUrl(r.source_url);
    return `${K.head(K.capHtml(r, true), `<span class="hd-num">${rptLine(r)}</span>`, "hd-call hd-detail__call", `${M.freq(r.output_mhz)} megahertz repeater`)}
      <p class="hd-detail__lede">${[r.callsign, r.city].filter(Boolean).map(esc).join(", ")}</p><p class="hd-detail__sub">${esc(modeLong(r))}, ${tone ? esc(tone) : "no tone listed"}</p>
      <div class="hd-detail__section">${K.qso(qsoCells(r, tone, offTxt), true)}</div>
      ${approxOf(r) ? K.note("The location is approximate. The directory lists only the town, so the map shows the town centre.") : ""}
      ${r.status === "off-air" ? K.note("This machine is listed as off the air.") : ""}
      <div class="hd-detail__actions"><button class="hd-btn hd-btn--primary hd-btn--sm" type="button" data-copy="${esc(copyTxt)}">${M.ic("copy", 20)}Copy settings</button>${rbUrl ? K.ext(rbUrl, "Search RepeaterBook near here") : ""}</div>
      ${linksHtml(r)}
      ${tg.length ? `<section class="hd-detail__section"><h3 class="hd-detail__h">Talkgroups</h3><table class="hd-fixes"><tbody>${tg.map((t) => `<tr><td>Slot${NB}${esc(t.slot)}</td><td>TG${NB}${esc(t.tg)}</td><td>${esc(t.name || "")}</td></tr>`).join("")}</tbody></table></section>` : ""}
      ${r.notes ? `<section class="hd-detail__section"><h3 class="hd-detail__h">Notes</h3><p class="hd-rptnotes">${esc(r.notes)}</p></section>` : ""}
      <p class="hd-detail__source">From ${srcUrl ? `<a class="hd-link" href="${esc(srcUrl)}" target="_blank" rel="noopener">${esc(src[0])}</a>` : esc(src[0])}${src[1] ? `, ${esc(src[1])}` : ""}. Listings can be out of date, so confirm on the air.${also.length ? ` Also listed by ${also.join(" and ")}.` : ""}</p>`;
  }
  function cardLoad(it) {
    if (it.kind !== "rpt") return;
    loadStatus().then((s) => { if (s && !it.status) { it.status = s; M.ui.refreshDetail(it); } });
    if (it.detail) return;
    HD.api(`/api/repeaters/${encodeURIComponent(it.r.id)}`).then((d) => { if (d) { it.detail = d; M.ui.refreshDetail(it); } }, () => {});
  }
  function row(it) {
    if (it.kind === "rpt") {
      const r = it.r, d = M.distFromHome(r.lat, r.lon), tone = toneText(r);
      return { lead: K.capHtml(r), title: `<span class="hd-call hd-num" style="font-size:20px">${rptLine(r)}</span>`, meta: [r.callsign, [modeText(r) || "FM", tone].filter(Boolean).join(" ")].filter(Boolean).map(esc).join(", "), trail: d == null ? "" : M.mi(d) };
    }
    if (it.kind === "site") return { lead: K.capHtml(it.items[0]), title: `${it.items.length}${NB}repeaters`, meta: it.approx ? "Listed at the town centre" : "On one site", trail: "" };
    return null;
  }
  function inView(data, ctx) {
    if (!data || data.type !== "repeaters" || ctx.zoom < M.MINZ.rpt) return null;
    const b = M.screenBounds(), c = M.map.getCenter(), cp = { lat: c.lat, lon: c.lng };
    const rs = (data.repeaters || []).filter((r) => tunable(r) && b.contains([r.lat, r.lon]));
    if (!rs.length) return null;
    const near = rs.map((r) => ({ r, d: M.distMi(cp, r) })).sort((a, q) => a.d - q.d).slice(0, 20).map((x) => ({ layer: "rpt", kind: "rpt", r: x.r }));
    return { heading: "Repeaters", items: near, more: rs.length > 20 ? `${M.fmtN(rs.length - 20)} more in view.` : "" };
  }
  async function resolve(kind, id) {
    const d = await HD.api(`/api/repeaters/${encodeURIComponent(decodeURIComponent(id))}`).catch(() => null);
    return d && M.finite(d.lat) ? { layer: "rpt", kind: "rpt", r: d, detail: d } : null;
  }
  const selIdOf = (it) => (it.kind === "rpt" ? `rpt:${it.r.id}` : it.kind === "site" ? `rpt:${it.items[0].id}` : "");
  const latlng = (it) => (it.kind === "rpt" ? [it.r.lat, it.r.lon] : [it.lat, it.lon]);
  function markEl(it, st) {
    const id = it.kind === "rpt" ? it.r.id : it.items[0].id;
    for (const m of st.store.values()) if (m._hd.ids && m._hd.ids.includes(id)) return m.getElement();
    return null;
  }

  HD.map.addLayer({
    id: "rpt", order: 30, title: "Repeaters", minZoom: 10,
    feature: (f) => f.repeaters !== false,
    defaults: { "2m": true, "70cm": true, other: true, FM: true, DMR: true, Fusion: true, DSTAR: true, offair: false, simplex: false },
    filterKey: (p) => filterParams(p),
    load: (view, signal) => HD.api(`/api/repeaters/map?${M.bboxQuery(view)}${view.filterKey ? "&" + view.filterKey : ""}`, { signal }),
    render, counts, section, hint, card, cardLoad, row, inView, resolve, selId: selIdOf, latlng, markEl,
    ring: () => null, // the selected tag carries its own ring
    attribution: 'Repeaters: <a href="https://hearham.com/" target="_blank" rel="noopener">HearHam</a>',
  });
})();
