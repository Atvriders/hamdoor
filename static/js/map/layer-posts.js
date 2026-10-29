/* hamdoor map 9/9: posts from neighbours. DESIGN_SYSTEM.md §3.5, §7.5, §7.11, §9.1, §14.4.
   GET /api/posts/feed pages 1 to 3 when the map opens, then every 5 minutes.
   A post by you or a neighbour is never drawn at its own point: the author's
   newest post rides on their mark as a 20 px badge from z13 (layer-people.js
   asks newestBy). A post by anyone else is a 26 px bubble at the centre of the
   6-character subsquare of its point, from z13, titled from z14. The map shows
   a preview; the thread lives in the Feed (#/feed/post/<id>). */
(function () {
  "use strict";
  const HD = window.HD;
  const M = HD.map._;
  const K = M.marks;
  const NB = M.NB;
  const esc = M.esc;

  async function load(view, signal) {
    const out = [];
    for (let page = 1; page <= 3; page++) {
      let rows;
      try { rows = await HD.api(`/api/posts/feed?page=${page}`, { signal }); }
      catch (e) { if (HD.isAbort(e)) throw e; if (e.status === 400) break; if (page === 1) throw e; break; } // 400: no location yet
      if (!Array.isArray(rows)) break;
      out.push(...rows);
      if (rows.length < 20) break;
    }
    return { posts: out };
  }
  const isMine = (p) => { const me = M.me(); return !!me && p.author_callsign === me.callsign; };
  const authorIsPerson = (p) => isMine(p) || !!M.neighbour(p.author_callsign);
  /** newest post per author (the feed is newest first) */
  function newestBy(call) {
    const d = M.data("posts");
    if (!d) return null;
    return d.posts.find((p) => p.author_callsign === call) || null;
  }
  /** where a post shows (§3.5): on its author's mark, or at the centre of its 6-character subsquare */
  function place(p) {
    if (isMine(p)) { const y = M.youPlace(); return y ? { lat: y.lat, lon: y.lon, onMark: true } : null; }
    const n = M.neighbour(p.author_callsign);
    if (n && M.finite(n.lat)) return { lat: n.lat, lon: n.lon, onMark: true };
    if (!M.finite(p.lat) || !M.finite(p.lon)) return null;
    const c = M.gridCentre(M.grid(p.lat, p.lon, 6));
    return c ? { lat: c.lat, lon: c.lon, onMark: false } : null;
  }

  function render(data, ctx) {
    const want = new Map();
    if (!data || ctx.zoom < M.MINZ.posts) return want;
    const b = ctx.bounds.pad(0.2);
    for (const p of data.posts) {
      if (authorIsPerson(p)) continue; // a person's newest post rides on their mark; older ones are in the Feed
      const ll = place(p);
      if (!ll || !b.contains([ll.lat, ll.lon])) continue;
      const t = p.title.length > 30 ? p.title.slice(0, 29) + "…" : p.title;
      const isSel = !!ctx.sel && ctx.sel.layer === "posts" && ctx.sel.p && ctx.sel.p.id === p.id;
      want.set("p:" + p.id, { latlng: [ll.lat, ll.lon], size: [26, 26], anchor: [13, 24], pane: "hd-posts", lbl: true, pri: isSel ? -5 : 4, always: isSel && ctx.zoom >= M.MINZ.postLabels, minZ: M.MINZ.postLabels,
        html: `<div class="hd-mk-post${isSel ? " is-selected" : ""}">${K.postSvg(p.category)}<span class="hd-lbl hd-halo">${esc(t)}</span></div>`,
        title: `Post: ${p.title}`, item: { layer: "posts", kind: "post", p } });
    }
    return want;
  }
  function counts(data, ctx) {
    if (!data) return { meta: "", chips: {} };
    if (ctx.zoom < M.MINZ.posts) return { meta: "Shown when you zoom in", chips: {} };
    const b = M.screenBounds();
    const n = data.posts.filter((p) => { const ll = place(p); return ll && b.contains([ll.lat, ll.lon]) && (!ll.onMark || newestBy(p.author_callsign) === p); }).length;
    return { meta: `${M.fmtN(n)} in view`, chips: {} };
  }

  /* ------------------------------------------------------------ the preview card (§7.11) */
  /** "Tuesdays, 8:00 pm" or "Saturday, October 24, 9:00 am": the Feed's own wording (HD.fmt.event) */
  const whenText = (p) => (p.event_at && HD.fmt.event ? HD.fmt.event(p.event_at, p.repeats_weekly) : "");
  function authorDist(p) {
    if (isMine(p)) return "You";
    const n = M.neighbour(p.author_callsign);
    if (n) { const pn = M.L("members").def.person(n); return M.finite(n.distance_miles) ? M.mi(n.distance_miles, pn.approx) : ""; }
    return M.finite(p.distance_miles) ? M.mi(p.distance_miles, true) : "";
  }
  function card(it) {
    const p = it.p, [catLabel, glyph] = K.catOf(p.category);
    const n = p.comment_count || 0;
    const when = whenText(p);
    // repeater chips (§7.5) come from the shell's shared matcher; a chip opens the repeater's card on the map
    const chips = it.chips || "";
    return `${K.head(`<span style="display:block">${K.postSvg(p.category, 40)}</span>`, esc(p.title), "hd-detail__title")}
      <div class="hd-post__from" style="margin-top:12px">${K.classBadge(p.author_class, !!p.author_new)}<span class="hd-call hd-post__call">${esc(p.author_callsign)}</span><span class="hd-post__name">${esc(M.firstName(p.author_name))}</span><span class="hd-post__dist">${authorDist(p)}</span></div>
      <div class="hd-post__meta">${K.glyph(glyph, 20)}<span>${esc(catLabel)}</span><time>${esc(M.ago(p.created_at))}</time></div>
      ${p.body ? `<p class="hd-post__body hd-post__clamp" style="margin-top:12px">${esc(p.body)}</p>` : ""}
      ${when || chips ? `<div class="hd-post__when">${when ? `${M.ic("clock", 20)}<span>${esc(when)}</span>` : ""}${chips}</div>` : ""}
      <div class="hd-detail__actions"><button class="hd-btn hd-btn--primary hd-btn--sm" type="button" data-goto="${esc(JSON.stringify(["feed", {}, { sub: ["post", String(p.id)] }]))}">${M.ic("reply", 20)}${n ? `Read ${n}${NB}${n === 1 ? "reply" : "replies"}` : "Open the post"}</button><button class="hd-btn hd-btn--sm" type="button" data-goto="${esc(JSON.stringify(["feed", { reply: 1 }, { sub: ["post", String(p.id)] }]))}">Reply</button></div>`;
  }
  function cardLoad(it) {
    if (it.kind !== "post" || it.chips != null || !HD.repeaterChips) return;
    const ll = place(it.p);
    if (!ll) return;
    const text = it.p.body || ""; // the Feed matches the body too, so both share one cached answer
    const cached = HD.repeaterChips.cached(text, ll);
    if (cached !== undefined) { it.chips = cached; if (cached) M.ui.refreshDetail(it); return; }
    HD.repeaterChips.html(text, ll).then((html) => { it.chips = html || ""; if (it.chips) M.ui.refreshDetail(it); }, () => { it.chips = ""; });
  }
  function row(it) {
    const p = it.p;
    return { lead: K.postBadge(p.category, true), title: esc(p.title), meta: `${esc(p.author_callsign)}, ${esc(K.catOf(p.category)[0])}`, trail: esc(M.ago(p.created_at)) };
  }
  function inView(data, ctx) {
    if (!data || ctx.zoom < M.MINZ.posts) return null;
    const b = M.screenBounds();
    const ps = data.posts.filter((p) => { const ll = place(p); return ll && b.contains([ll.lat, ll.lon]) && (!ll.onMark || newestBy(p.author_callsign) === p); });
    return ps.length ? { heading: "Posts", items: ps.map((p) => ({ layer: "posts", kind: "post", p })) } : null;
  }
  async function resolve(kind, id) {
    const n = Number(decodeURIComponent(id));
    const d = M.data("posts"), known = d && d.posts.find((p) => p.id === n);
    if (known) return { layer: "posts", kind: "post", p: known };
    const p = await HD.api(`/api/posts/${n}`).catch(() => null);
    return p ? { layer: "posts", kind: "post", p } : null;
  }
  const selId = (it) => `post:${it.p.id}`;
  const latlng = (it) => { const ll = place(it.p); return ll ? [ll.lat, ll.lon] : null; };
  /** a person's post is drawn on their mark: ring that mark */
  function markEl(it, st) {
    const m = st.store.get("p:" + it.p.id);
    if (m) return m.getElement();
    const ppl = M.L("members");
    if (isMine(it.p)) { const y = ppl.store.get("you"); return y ? y.getElement() : null; }
    for (const mk of ppl.store.values()) if (mk._hd.calls && mk._hd.calls.includes(it.p.author_callsign)) return mk.getElement();
    return null;
  }
  const ring = (it, st) => { const ll = latlng(it); return ll ? { latlng: ll, d: 30, el: markEl(it, st) } : null; };

  HD.map.addLayer({
    id: "posts", order: 60, title: "Posts from neighbours",
    defaults: {},
    // your feed: your range, your place, your account. Another user (or another range) starts it again (map-core.js)
    bbox: false, cacheMs: 300000, poll: 300000, perUser: true,
    load, render, counts, section: () => "", card, cardLoad, row, inView, resolve, selId, latlng, markEl, ring,
    newestBy,
  });
})();
