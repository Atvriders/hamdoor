/* Feed (DESIGN_SYSTEM §7.16), posts and comments (§7.5), the composer (§7.6) and threads (§7.15).
   Routes: #/feed (?category= keeps a filter, ?author=CALL is "See Marisol's posts"),
           #/feed/post/<id> (?reply=1 focuses the reply field).
   Phones: a thread is its own page and Back returns to the feed at the same scroll position.
   Desktop: the thread replaces the aside column and the post in the list is marked. */
(function () {
  "use strict";
  const HD = window.HD;
  const esc = HD.esc;
  const NB = HD.fmt.NBSP;
  const icon = (n, o) => HD.icon(n, o);
  const deskMQ = window.matchMedia ? matchMedia("(min-width: 1024px)") : { matches: false };
  const reduced = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  /** Keyboard mode (§8.4): only then does moving focus also scroll its target into view. */
  const kbd = () => !!(HD.chrome && HD.chrome.keyboardMode && HD.chrome.keyboardMode());
  const PAGE = 20;
  const FRESH_MS = 60 * 1000;

  let el = null; // the section
  const st = {
    filter: { category: "", author: "" },
    lists: new Map(), // "category|author" -> {posts, page, done, at, loading, error, ctl}
    mode: "list", // "list" | "thread" (the phone thread page)
    threadId: null,
    reply: false,
    threads: new Map(), // id -> {post, at, error, loading}
    feedScroll: 0,
    viewScroll: 0,
    cameFromList: false,
    pending: new Map(), // post id -> deletion waiting for its Undo toast
    pendingComments: new Map(),
    nb: null, // neighbour list (null until loaded)
    nbBy: new Map(),
    aside: { at: 0, html: "", loading: false },
    visible: false,
  };

  // ---------------------------------------------------------------- helpers

  const me = () => HD.session.user || {};
  const rangeMi = () => me().range_miles || 25;
  const listKey = (f) => `${f.category || ""}|${(f.author || "").toUpperCase()}`;
  const curList = () => {
    const k = listKey(st.filter);
    if (!st.lists.has(k)) st.lists.set(k, { posts: [], page: 0, done: false, at: 0, loading: false, error: null, ctl: null });
    return st.lists.get(k);
  };
  const isMine = (p) => p && me().callsign && p.author_callsign === me().callsign;
  const findPost = (id) => {
    id = Number(id);
    const t = st.threads.get(id);
    if (t && t.post) return t.post;
    for (const L of st.lists.values()) { const p = L.posts.find((x) => x.id === id); if (p) return p; }
    return null;
  };
  function forEachCopy(id, fn) {
    id = Number(id);
    for (const L of st.lists.values()) for (const p of L.posts) if (p.id === id) fn(p);
    const t = st.threads.get(id);
    if (t && t.post) fn(t.post);
  }
  function invalidateLists() { for (const L of st.lists.values()) L.at = 0; }

  function loadNeighbours() {
    return HD.neighbours().then((list) => {
      st.nb = list || [];
      st.nbBy = new Map(st.nb.map((n) => [n.callsign, n]));
      return st.nb;
    }).catch(() => { st.nb = st.nb || []; return st.nb; });
  }

  /** A post's distance (§3.5): yours "You"; a neighbour's from the neighbour list; anyone else whole miles after "about". */
  function authorDist(p) {
    if (isMine(p)) return "You";
    const n = st.nbBy.get(p.author_callsign);
    if (n && n.distance_miles != null) return HD.fmt.mi(n.distance_miles, { about: HD.isApprox(n) });
    return p.distance_miles != null ? HD.fmt.mi(p.distance_miles, { about: true }) : "";
  }
  /** Where a post sits (§3.5): the author's mark for you and your neighbours, else its 6-character subsquare. */
  function postPlace(p) {
    const u = me();
    if (isMine(p)) {
      if (u.map_precision === "grid") return HD.geo.gridCentre(HD.geo.shownLocator(u.grid, u.lat, u.lon));
      if (u.lat != null) return { lat: u.lat, lon: u.lon };
    }
    const n = st.nbBy.get(p.author_callsign);
    if (n && n.lat != null) return { lat: n.lat, lon: n.lon };
    if (p.lat == null || p.lon == null) return null;
    return HD.geo.gridCentre(HD.geo.toGrid(p.lat, p.lon, 6));
  }
  // author_class "" is a club licence (a "C" badge), null an author the FCC directory has no row for (an empty ring)
  const classBadge = (p) => HD.ui.classBadge(p.author_class, !!p.author_new);
  const repliesText = (n) => (n ? `${n}${NB}${n === 1 ? "reply" : "replies"}` : "No replies yet");

  // ---------------------------------------------------------------- repeater chips (§7.5, matcher in hd-core: HD.repeaterChips)

  const cachedChips = (text, place) => HD.repeaterChips.cached(text, place);
  /** Fill every chip slot under `root` (posts and comments) once their repeater data is in. */
  function fillChips(root) {
    for (const slot of root.querySelectorAll("[data-chips-for]")) {
      if (slot.dataset.filled) continue;
      const p = findPost(slot.dataset.chipsFor);
      if (!p) continue;
      const cid = slot.dataset.comment;
      const text = cid ? ((p.comments || []).find((c) => String(c.id) === cid) || {}).body : p.body;
      const place = postPlace(p);
      slot.dataset.filled = "1";
      HD.repeaterChips.html(text || "", place).then((html) => {
        if (!html || !slot.isConnected) return;
        slot.innerHTML = html;
        const when = slot.closest(".hd-post__when");
        if (when) when.hidden = false;
      });
    }
  }

  // ---------------------------------------------------------------- post HTML

  function postHtml(p, mode) {
    const thread = mode !== "list";
    const cat = HD.ui.category(p.category);
    const place = postPlace(p);
    const chipsNow = cachedChips(p.body, place);
    const ev = p.event_at ? HD.fmt.event(p.event_at, p.repeats_weekly) : "";
    const hTag = mode === "page" ? "h2" : "h3";
    const own = isMine(p);
    const ownBtns = own ? `<span class="hd-post__own"><button class="hd-btn hd-btn--quiet hd-btn--sm" type="button" data-edit="${p.id}" aria-describedby="pt-${mode}-${p.id}">Edit</button>` +
      `<button class="hd-btn hd-btn--quiet hd-btn--sm" type="button" data-delete="${p.id}" aria-describedby="pt-${mode}-${p.id}">Delete</button></span>` : "";
    const actions = thread
      ? (own ? `<div class="hd-post__actions">${ownBtns}</div>` : "")
      : `<div class="hd-post__actions"><a class="hd-btn hd-btn--quiet hd-btn--sm" href="#/feed/post/${p.id}" data-open-thread aria-describedby="pt-${mode}-${p.id}">${icon("reply", { size: 20 })}<span>${repliesText(p.comment_count)}</span></a>` +
        `<a class="hd-btn hd-btn--quiet hd-btn--sm" href="#/feed/post/${p.id}?reply=1" data-open-thread aria-describedby="pt-${mode}-${p.id}">Reply</a>${ownBtns}</div>`;
    const current = !thread && st.threadId === p.id && deskMQ.matches ? ' aria-current="true"' : "";
    return `<article class="hd-post" data-post="${p.id}" aria-labelledby="pt-${mode}-${p.id}"${current}${st.pending.has(p.id) ? ' data-pending="true"' : ""}>
      <div class="hd-post__from">${classBadge(p)}<span class="hd-call hd-post__call">${esc(p.author_callsign)}</span><span class="hd-post__name">${esc(HD.firstName(p.author_name))}</span><span class="hd-post__dist">${esc(authorDist(p))}</span></div>
      <div class="hd-post__meta">${icon(cat.icon, { size: 20 })}<span>${esc(cat.label)}</span><time datetime="${esc(p.created_at)}Z">${esc(HD.fmt.ago(p.created_at, { cap: true }))}</time></div>
      <${hTag} class="hd-post__title" id="pt-${mode}-${p.id}"${thread ? ' tabindex="-1"' : ""}>${esc(p.title)}</${hTag}>
      ${p.body ? `<p class="hd-post__body">${esc(p.body)}</p>` : ""}
      <div class="hd-post__when"${ev || chipsNow ? "" : " hidden"}>${ev ? `<span class="hd-post__clock">${icon("clock", { size: 20 })}<span>${esc(ev)}</span></span>` : ""}<span class="hd-post__chips" style="display:contents" data-chips-for="${p.id}"${chipsNow !== undefined ? ' data-filled="1"' : ""}>${chipsNow || ""}</span></div>
      ${actions}${thread ? commentsHtml(p) : ""}</article>`;
  }

  function commentsHtml(p) {
    const mine = me().callsign;
    const rows = (p.comments || []).map((c) => {
      const canDelete = c.author_callsign === mine || p.author_callsign === mine;
      const chips = cachedChips(c.body, postPlace(p));
      return `<div class="hd-comment" data-comment="${c.id}"${st.pendingComments.has(c.id) ? ' data-pending="true"' : ""}>
        <span class="hd-call hd-comment__call">${esc(c.author_callsign)}</span><span class="hd-comment__time">${esc(HD.firstName(c.author_name))}, ${esc(HD.fmt.ago(c.created_at))}</span>
        <p class="hd-comment__body">${esc(c.body)}</p>
        <div class="hd-comment__chips" data-chips-for="${p.id}" data-comment="${c.id}"${chips !== undefined ? ' data-filled="1"' : ""}>${chips || ""}</div>
        ${canDelete ? `<button class="hd-btn hd-btn--quiet hd-btn--sm hd-comment__own" type="button" data-delete-comment="${c.id}" data-post-id="${p.id}">Delete<span class="hd-sr"> reply from ${esc(c.author_callsign)}</span></button>` : ""}
      </div>`;
    }).join("");
    return `<div class="hd-comments">${rows}
      <form class="hd-reply" data-reply-form="${p.id}" novalidate><label class="hd-sr" for="reply-${p.id}">Write a reply</label>
        <input class="hd-input" id="reply-${p.id}" name="body" placeholder="Write a reply" maxlength="4000" autocomplete="off">
        <button class="hd-btn hd-btn--primary" type="submit">Send</button></form></div>`;
  }

  // ---------------------------------------------------------------- skeleton

  function skeleton() {
    const rangeBtn = `<a class="hd-btn hd-btn--quiet hd-btn--sm" href="#/you?focus=range" data-range-btn><span>Within <span data-range></span>${NB}mi</span>${icon("down", { size: 20 })}</a>`;
    el.innerHTML = `
      <div class="hd-feedpage" data-feedpage>
        <header class="hd-topbar" data-tuck="top" data-topbar>
          <div class="hd-topbar__row"><h1 class="hd-topbar__title" id="feed-title" tabindex="-1">Feed</h1>${rangeBtn}</div>
          <div class="hd-topbar__row hd-topbar__chips"><div class="hd-chips hd-chips--scroll" role="group" aria-label="Show posts about" data-feed-chips></div></div>
        </header>
        <div class="hd-page">
          <div class="hd-page__grid" data-feed-grid>
            <div class="hd-page__col">
              <div class="hd-feedhead"><button class="hd-compose" type="button" data-compose>${icon("pencil", { size: 24 })}<span>Write to hams within <span data-range></span>${NB}miles</span></button></div>
              <div data-feed-state></div>
              <div class="hd-list" data-feed-list></div>
              <div data-feed-more></div>
            </div>
            <aside class="hd-page__aside hd-aside" aria-label="Your area" data-feed-aside></aside>
          </div>
        </div>
      </div>
      <div class="hd-threadpage" data-threadpage hidden>
        <header class="hd-topbar" data-tuck="top" data-topbar>
          <div class="hd-topbar__row">
            <a class="hd-backbtn" href="#/feed" data-back>${icon("left", { size: 24 })}Feed</a>
            <h1 class="hd-sr">Post</h1><span class="hd-topbar__spacer"></span>
            <button class="hd-btn hd-btn--quiet hd-btn--sm" type="button" data-post-map>${icon("map", { size: 20 })}Show on map</button>
          </div>
        </header>
        <div class="hd-page"><div class="hd-page__col" data-thread-body></div></div>
      </div>`;
    for (const bar of el.querySelectorAll("[data-topbar]")) HD.chrome.watchTopbar(bar);
  }

  function renderRange() {
    for (const s of el.querySelectorAll("[data-range]")) s.textContent = rangeMi();
  }

  /** The filter chips. Choosing one updates them in place, so the chip you pressed keeps keyboard focus (§11);
   *  they are rebuilt only between the category row and the "Posts by" chip, and focus then lands on the chosen chip. */
  function renderChips() {
    const box = el.querySelector("[data-feed-chips]");
    const a = st.filter.author;
    const mode = a ? "author:" + a.toUpperCase() : "category";
    if (box._hdMode === mode) {
      if (!a) for (const b of box.querySelectorAll("[data-cat]")) b.setAttribute("aria-pressed", String(b.dataset.cat === st.filter.category));
    } else {
      const hadFocus = box.contains(document.activeElement);
      if (a) {
        box.innerHTML = `<button class="hd-chip hd-chip--choice" type="button" aria-pressed="true" data-clear-author><span class="hd-chip__label">Posts by ${esc(a.toUpperCase())}</span>${icon("close", { size: 20, cls: "hd-chip__x" })}<span class="hd-sr">, show every post</span></button>`;
      } else {
        const chips = [{ id: "", label: "All" }].concat(HD.ui.CATEGORIES);
        box.innerHTML = chips.map((c) => `<button class="hd-chip hd-chip--choice" type="button" aria-pressed="${c.id === st.filter.category}" data-cat="${c.id}"><span class="hd-chip__label">${esc(c.label)}</span></button>`).join("");
      }
      box._hdMode = mode;
      if (hadFocus) HD.focusTo(box.querySelector('[aria-pressed="true"]'));
    }
    requestAnimationFrame(() => HD.revealInScroller(box, box.querySelector('[aria-pressed="true"]')));
  }

  // ---------------------------------------------------------------- keyboard focus across re-renders (§11)

  /** What has focus, as a selector the next render of the feed can find again (null: nothing to find). */
  function focusKey(node) {
    if (node.id) return "#" + CSS.escape(node.id); // the reply field, the titles, the thread heading
    const where = node.closest("[data-feed-aside]") ? "[data-feed-aside] " : node.closest("[data-thread-body]") ? "[data-thread-body] "
      : node.closest("[data-feed-more]") ? "[data-feed-more] " : node.closest("[data-feed-state]") ? "[data-feed-state] " : "[data-feed-list] ";
    const post = node.closest(".hd-post[data-post]");
    const inPost = post ? `.hd-post[data-post="${post.dataset.post}"] ` : "";
    const comment = node.closest(".hd-comment[data-comment]");
    const inComment = comment ? `.hd-comment[data-comment="${comment.dataset.comment}"] ` : "";
    for (const attr of ["data-edit", "data-delete", "data-delete-comment", "data-post-map", "data-thread-close", "data-retry", "data-retry-thread", "data-more", "data-compose", "data-rpt"]) {
      if (node.hasAttribute(attr)) {
        const v = node.getAttribute(attr);
        return where + inPost + inComment + `[${attr}${v ? `="${CSS.escape(v)}"` : ""}]`;
      }
    }
    if (node.matches("a[href]")) return where + inPost + `a[href="${CSS.escape(node.getAttribute("href"))}"]`; // replies, Reply, Nets rows
    if (comment && node === comment) return where + inPost + `.hd-comment[data-comment="${comment.dataset.comment}"]`;
    if (post && node === post) return where + `.hd-post[data-post="${post.dataset.post}"]`;
    return null;
  }
  /** Run a re-render of the feed and give focus back to the equivalent element (or to `fallback()`). */
  const withFocus = (render, fallback) => HD.keepFocus(el, focusKey, render, fallback);
  /** Focus a post in the list (made focusable for the moment), scrolled into view below the bars if needed. */
  function focusPost(id) {
    const a = id != null ? el.querySelector(`[data-feed-list] .hd-post[data-post="${id}"]`) : null;
    return a ? HD.focusTo(a, { reveal: kbd() }) : focusListStart();
  }
  /** The start of the list: a Try again button, the empty state's sentence, the first post, else the title. */
  function focusListStart() {
    const t = el.querySelector("[data-feed-state] [data-retry], [data-feed-state] .hd-empty__title") ||
      el.querySelector("[data-feed-list] .hd-post") || el.querySelector("#feed-title");
    return HD.focusTo(t, { reveal: kbd() });
  }
  /** The post after `article` in the list (else the one before it): where focus goes when `article` leaves. */
  function neighbourPostId(article) {
    if (!article) return null;
    let n = article.nextElementSibling;
    while (n && (!n.matches(".hd-post") || n.dataset.pending)) n = n.nextElementSibling;
    if (!n) { n = article.previousElementSibling; while (n && (!n.matches(".hd-post") || n.dataset.pending)) n = n.previousElementSibling; }
    return n ? Number(n.dataset.post) : null;
  }
  /** Did the person's focus go nowhere (to the page body, or out of the feed with the control they pressed)? */
  const focusLost = () => !document.activeElement || document.activeElement === document.body || !document.activeElement.isConnected;

  // ---------------------------------------------------------------- the list

  function noLocation() {
    const u = me();
    return u.callsign && (u.lat == null || u.lon == null);
  }

  /** Render the list, its state line and its foot; whatever had focus in them keeps it (or its new copy does). */
  function renderList() {
    if (!el) return;
    withFocus(renderListNow);
  }
  function renderListNow() {
    const L = curList();
    const stateEl = el.querySelector("[data-feed-state]");
    const listEl = el.querySelector("[data-feed-list]");
    const moreEl = el.querySelector("[data-feed-more]");
    const cat = st.filter.category ? HD.ui.category(st.filter.category) : null;
    const range = rangeMi();
    const posts = L.posts.filter((p) => !st.pending.has(p.id));
    let state = "";
    // without a location a post would reach nobody's feed: the launcher steps aside for the "Open You" state
    const head = el.querySelector(".hd-feedhead");
    if (head) head.hidden = !!(L.noLocation || noLocation());
    if (L.noLocation || (noLocation() && !posts.length)) {
      state = HD.ui.empty({ icon: "pin", title: "Add your grid square or address to see posts near you.", action: `<a class="hd-btn hd-btn--primary" href="#/you?focus=grid">Open You</a>` });
    } else if (!posts.length && L.loading) {
      state = HD.ui.statusLine("clock", "Loading posts near you.");
    } else if (!posts.length && L.error) {
      state = `<div class="hd-liststate">${HD.ui.alert("Couldn't load the feed", "Check your connection; we'll try again when you're back online.")}<button class="hd-btn" type="button" data-retry>${icon("refresh", { size: 20 })}Try again</button></div>`;
    } else if (!posts.length && L.at) {
      if (st.filter.author) {
        const n = st.nbBy.get(st.filter.author.toUpperCase());
        const who = n ? HD.firstName(n.name) : st.filter.author.toUpperCase();
        state = HD.ui.empty({ icon: "feed", title: `${esc(who)} hasn't posted within your range yet.` });
      } else {
        state = HD.ui.empty({
          icon: "feed",
          title: cat ? `No ${esc(cat.label)} posts within ${range}${NB}miles yet.` : `No posts within ${range}${NB}miles yet.`,
          body: "Ask a question or announce a net, and hams near you will see it here.",
          action: `<button class="hd-btn hd-btn--primary" type="button" data-compose>Write a post</button>`,
        });
      }
    }
    if (stateEl._hd !== state) { stateEl.innerHTML = state; stateEl._hd = state; } // an unchanged state keeps its Try again button
    listEl.innerHTML = posts.map((p) => postHtml(p, "list")).join("");
    // the foot: "Show older posts" turns into "Loading older posts" and back in place, so it keeps focus while it loads
    const kind = !posts.length ? "" : L.moreError ? "error" : !L.done ? "more" : "end";
    const btn = kind === "more" && moreEl._hdKind === "more" ? moreEl.querySelector("[data-more]") : null;
    if (btn) {
      btn.textContent = L.loading ? "Loading older posts" : "Show older posts";
      if (L.loading) btn.setAttribute("aria-busy", "true"); else btn.removeAttribute("aria-busy");
    } else if (kind !== moreEl._hdKind || kind === "end") {
      let more = "";
      if (kind === "error") more = `<div class="hd-feedmore">${HD.ui.alert("Couldn't load older posts", "Check your connection and try again.")}<button class="hd-btn hd-btn--quiet" type="button" data-more>Try again</button></div>`;
      else if (kind === "more") more = `<div class="hd-feedmore"><button class="hd-btn hd-btn--quiet" type="button" data-more${L.loading ? ' aria-busy="true"' : ""}>${L.loading ? "Loading older posts" : "Show older posts"}</button></div>`;
      else if (kind === "end") more = `<p class="hd-feedend">That's every post within ${range}${NB}miles.</p>`;
      moreEl.innerHTML = more;
      moreEl._hdKind = kind;
    }
    fillChips(listEl);
  }

  /** Load page `page` of the current filter (1 = reload from the top, keeping older pages already shown). */
  async function loadPage(page, opts = {}) {
    const f = { ...st.filter };
    const key = listKey(f);
    const L = curList();
    if (L.loading && !opts.force) return;
    if (L.ctl) L.ctl.abort();
    const ctl = new AbortController();
    L.ctl = ctl;
    L.loading = true;
    L.moreError = false;
    if (page === 1) L.error = null;
    if (st.visible && listKey(st.filter) === key) renderList();
    const q = new URLSearchParams({ page: String(page) });
    if (f.category) q.set("category", f.category);
    if (f.author) q.set("author", f.author.toUpperCase());
    try {
      const [rows] = await Promise.all([HD.api("/api/posts/feed?" + q, { signal: ctl.signal }), st.nb ? null : loadNeighbours()]);
      const list = rows || [];
      if (page === 1) {
        // newest first: keep older pages already loaded, drop what page 1 now repeats
        const ids = new Set(list.map((p) => p.id));
        const keepOlder = L.page > 1 ? L.posts.slice(PAGE).filter((p) => !ids.has(p.id)) : [];
        L.posts = list.concat(keepOlder);
        if (L.page <= 1) L.done = list.length < PAGE;
        L.page = Math.max(1, L.page);
      } else {
        const ids = new Set(L.posts.map((p) => p.id));
        L.posts = L.posts.concat(list.filter((p) => !ids.has(p.id)));
        L.page = page;
        L.done = list.length < PAGE;
      }
      L.at = Date.now();
      L.noLocation = false;
      L.error = null;
      if (opts.announce) {
        const n = L.posts.length;
        const label = f.category ? HD.ui.category(f.category).label + " " : "";
        HD.announce(f.author ? `Showing ${n} ${n === 1 ? "post" : "posts"} by ${f.author.toUpperCase()}` : `Showing ${n} ${label}${n === 1 ? "post" : "posts"}`);
      }
    } catch (e) {
      if (HD.isAbort(e)) return;
      if (e.status === 400) L.noLocation = true;
      else if (page === 1) L.error = e;
      else L.moreError = true;
    } finally {
      if (L.ctl === ctl) { L.ctl = null; L.loading = false; }
    }
    if (st.visible && listKey(st.filter) === key && st.mode === "list") {
      keepAnchor(() => renderList());
      if (deskMQ.matches && !st.threadId) renderAside();
    }
  }

  /** Re-render without the page jumping: keep the first visible post where it was. */
  function keepAnchor(fn) {
    if (window.scrollY < 4) { fn(); return; }
    const top = HD.chrome && document.querySelector(".hd-view:not([hidden]) [data-topbar]");
    const edge = top ? top.getBoundingClientRect().bottom : 0;
    let anchor = null, y0 = 0;
    for (const a of el.querySelectorAll("[data-feed-list] .hd-post")) {
      const r = a.getBoundingClientRect();
      if (r.bottom > edge) { anchor = a.dataset.post; y0 = r.top; break; }
    }
    fn();
    if (!anchor) return;
    const a2 = el.querySelector(`[data-feed-list] .hd-post[data-post="${anchor}"]`);
    if (!a2) return;
    const dy = a2.getBoundingClientRect().top - y0;
    if (Math.abs(dy) > 1) { window.scrollBy(0, dy); HD.chrome.listReset(); }
  }

  // ---------------------------------------------------------------- the desktop aside (§7.16)

  async function renderAside() {
    const box = el && el.querySelector("[data-feed-aside]");
    if (!box || !deskMQ.matches) return;
    if (st.threadId) return; // the thread column
    const fresh = Date.now() - st.aside.at < 5 * 60 * 1000;
    if (fresh && st.aside.html) { withFocus(() => { box.innerHTML = st.aside.html; }); return; }
    if (st.aside.loading) return;
    st.aside.loading = true;
    const u = me();
    const range = rangeMi();
    const grid = HD.me.grid();
    const [nb, rpt, bands, nets] = await Promise.allSettled([
      loadNeighbours(),
      HD.config.features && HD.config.features.repeaters
        ? HD.api(`/api/repeaters/near?radius_km=${Math.min(805, Math.round(range * 1.609344))}&limit=1`) : Promise.resolve(null),
      HD.api("/api/activity/bands"),
      HD.api("/api/posts/feed?category=nets-events"),
    ]);
    st.aside.loading = false;
    let html = `<section class="hd-aside__sec"><h2 class="hd-aside__h">Your neighbourhood</h2><dl class="hd-kv">
      <dt>Range</dt><dd>${range}${NB}miles${grid ? " of " + esc(grid) : ""}</dd>`;
    if (nb.status === "fulfilled") html += `<dt>On hamdoor</dt><dd>${HD.fmt.plural(nb.value.length, "neighbour")}</dd>`;
    if (rpt.status === "fulfilled" && rpt.value && rpt.value.total != null) {
      html += `<dt>Repeaters</dt><dd class="hd-num">${HD.fmt.num(rpt.value.total)} listed</dd>`; // every one in range, as Activity
    }
    html += `</dl></section>`;
    if (bands.status === "fulfilled" && bands.value) html += bandsAside(bands.value);
    html += netsAside(nets.status === "fulfilled" ? nets.value || [] : null);
    st.aside = { at: Date.now(), html, loading: false };
    if (!u.callsign || st.threadId || !el.isConnected) return;
    const b2 = el.querySelector("[data-feed-aside]");
    if (b2 && !st.threadId) withFocus(() => { b2.innerHTML = html; });
  }
  function bandsAside(b) {
    const rows = HD.ui.bandRows(b);
    return `<section class="hd-aside__sec"><h2 class="hd-aside__h">Band conditions</h2>
      <div class="hd-readouts">${[["Solar flux", b.solar_flux], ["A index", b.a_index], ["K index", b.k_index]].filter(([, v]) => v !== "" && v != null)
        .map(([k, v]) => `<span><span class="hd-readout__k">${k}</span><span class="hd-readout__v">${esc(v)}</span></span>`).join("")}</div>
      ${rows.length ? `<table class="hd-outlook" style="margin-top:12px"><thead><tr><th>Band</th><th>Day</th><th>Night</th></tr></thead><tbody>${rows.map((r) =>
        `<tr><td>${esc(r.band)}</td><td>${HD.ui.cond(r.day)}</td><td>${HD.ui.cond(r.night)}</td></tr>`).join("")}</tbody></table>` : ""}</section>`;
  }
  /** "Nets this week" (G4 shipped): nets and events with a time in the next 7 days, soonest first. */
  function netsAside(posts) {
    let rows = "";
    if (posts) {
      const now = Date.now(), week = now + 7 * 86400000;
      const upcoming = [];
      for (const p of posts) {
        const next = nextOccurrence(p);
        if (next && next.getTime() >= now - 3600000 && next.getTime() <= week) upcoming.push({ p, next });
      }
      upcoming.sort((a, b) => a.next - b.next);
      rows = upcoming.slice(0, 3).map(({ p, next }) => `<a class="hd-xref" href="#/feed/post/${p.id}">${HD.ui.postBadge(p.category)}<span class="hd-xref__t"><b>${esc(p.title)}</b><span>${esc(p.author_callsign)}, ${esc(HD.fmt.eventShort(next))}</span></span></a>`).join("");
    }
    return `<section class="hd-aside__sec"><h2 class="hd-aside__h">Nets this week</h2><div class="hd-aside__list">${rows || '<p class="hd-muted">No nets or events near you this week.</p>'}</div></section>`;
  }
  function nextOccurrence(p) {
    const d = HD.fmt.localDate(p.event_at);
    if (!d) return null;
    if (!p.repeats_weekly) return d;
    const now = new Date();
    const n = new Date(now.getFullYear(), now.getMonth(), now.getDate(), d.getHours(), d.getMinutes());
    let add = (d.getDay() - n.getDay() + 7) % 7;
    if (add === 0 && n.getTime() < now.getTime() - 3600000) add = 7;
    n.setDate(n.getDate() + add);
    return n < d ? d : n;
  }

  // ---------------------------------------------------------------- the thread (§7.15)

  async function loadThread(id, opts = {}) {
    const t = st.threads.get(id) || { post: null, at: 0, error: null, loading: false };
    st.threads.set(id, t);
    if (t.loading) return t;
    if (t.post && !opts.force && Date.now() - t.at < 30000) return t;
    t.loading = true;
    t.error = null;
    try {
      const [post] = await Promise.all([HD.api("/api/posts/" + id), st.nb ? null : loadNeighbours()]);
      t.post = post;
      t.at = Date.now();
      // keep list copies' counters in step with the thread
      forEachCopy(id, (p) => { if (p !== post) { p.comment_count = post.comment_count; p.title = post.title; p.body = post.body; } });
    } catch (e) {
      t.error = e;
      if (e.status === 404) t.post = null;
    } finally {
      t.loading = false;
    }
    return t;
  }

  function threadInner(id, mode) {
    const t = st.threads.get(id);
    if (!t || (t.loading && !t.post)) return HD.ui.statusLine("clock", "Loading the post.");
    if (t.error && t.error.status === 404) {
      return HD.ui.empty({ icon: "feed", title: "This post was deleted", action: `<a class="hd-btn hd-btn--primary" href="#/feed" data-back>Back to the feed</a>` });
    }
    if (t.error && !t.post) {
      return `<div class="hd-liststate">${HD.ui.alert("Couldn't load the post", esc(HD.errorText(t.error)))}<button class="hd-btn" type="button" data-retry-thread="${id}">${icon("refresh", { size: 20 })}Try again</button></div>`;
    }
    return postHtml(t.post, mode);
  }

  function renderThread(focus) {
    const id = st.threadId;
    if (!id) return;
    // "Show on map" only for a post that is there to show (its list copy will do while the thread loads): for a post
    // that is deleted, out of range or failed to load it did nothing
    const t = st.threads.get(id);
    const p = t && !t.post && t.error ? null : findPost(id);
    const mappable = !!(p && postPlace(p));
    if (st.mode === "thread") {
      const body = el.querySelector("[data-thread-body]");
      const mapBtn = el.querySelector("[data-threadpage] [data-post-map]");
      if (mapBtn) mapBtn.hidden = !mappable;
      withFocus(() => { body.innerHTML = threadInner(id, "page"); });
      fillChips(body);
      if (focus) focusThread("page");
    } else {
      const box = el.querySelector("[data-feed-aside]");
      withFocus(() => {
        box.innerHTML = `<div class="hd-thread" role="region" aria-labelledby="thread-h">
        <div class="hd-thread__head"><h2 class="hd-thread__h" id="thread-h" tabindex="-1">Post and replies</h2>
          <span class="hd-thread__actions">${mappable ? `<button class="hd-btn hd-btn--quiet hd-btn--sm" type="button" data-post-map="${id}">${icon("map", { size: 20 })}Show on map</button>` : ""}
          <a class="hd-iconbtn hd-iconbtn--wash" href="${HD.router.href("feed", st.filter)}" data-thread-close aria-label="Close">${icon("close", { size: 24 })}</a></span></div>
        ${threadInner(id, "column")}</div>`;
      });
      fillChips(box);
      if (focus) focusThread("column");
    }
  }
  /** Leaving a thread (the desktop column's close button, the phone page's ‹ Feed, Back): focus goes back to what
   *  opened it (its "N replies" or Reply link, a Nets row), else to the post in the list. */
  function focusOpener(id) {
    const o = st.opener && st.opener.id === id ? st.opener : null;
    let target = null;
    if (o && o.sel) { try { target = el.querySelector(o.sel); } catch { target = null; } }
    if (!target) target = el.querySelector(`[data-feed-list] .hd-post[data-post="${id}"]`);
    return target ? HD.focusTo(target, { reveal: kbd() }) : focusListStart();
  }
  /** Is `node` inside a thread (the desktop column or the phone page)? */
  const inThread = (node) => !!(node && node.closest && node.closest(".hd-thread, [data-threadpage]"));
  function focusThread(mode) {
    requestAnimationFrame(() => {
      let target = null;
      if (st.reply) target = el.querySelector(`#reply-${st.threadId}`);
      if (!target) target = mode === "page" ? el.querySelector("[data-thread-body] .hd-post__title") : el.querySelector("#thread-h");
      if (target) { target.focus({ preventScroll: mode !== "page" || !st.reply }); return; }
      // a phone thread page without its post (deleted, out of range, failed): what it says instead, or Try again,
      // so focus never stays on the page body
      if (mode === "page") HD.focusTo(el.querySelector("[data-thread-body] [data-retry-thread], [data-thread-body] .hd-empty__title"));
    });
  }

  // ---------------------------------------------------------------- layout and routing inside the feed

  function applyLayout() {
    const page = el.querySelector("[data-feedpage]"), tpage = el.querySelector("[data-threadpage]");
    page.hidden = st.mode === "thread";
    tpage.hidden = st.mode !== "thread";
    const grid = el.querySelector("[data-feed-grid]");
    grid.classList.toggle("hd-page__grid--thread", !!(st.threadId && deskMQ.matches));
  }

  function show(params, sub) {
    st.visible = true;
    const threadId = sub && sub[0] === "post" && /^\d+$/.test(sub[1] || "") ? Number(sub[1]) : null;
    const desk = deskMQ.matches;
    const prevMode = st.mode, prevThread = st.threadId;
    // a thread is closing while focus is in it (or was lost with it): focus goes back to what opened it, below
    const leavingThread = !!(prevThread && prevThread !== threadId && (focusLost() || inThread(document.activeElement)));
    if (threadId && !desk && prevMode === "list" && st.listShown) st.feedScroll = window.scrollY;
    st.cameFromList = threadId && prevMode === "list" && st.listShown ? true : threadId ? st.cameFromList : false;
    let filterChanged = false;
    if (!threadId) {
      const f = { category: params.category || "", author: (params.author || "").toUpperCase() };
      if (f.category && !HD.ui.CATEGORIES.some((c) => c.id === f.category)) f.category = "";
      filterChanged = listKey(f) !== listKey(st.filter);
      st.filter = f;
      if (filterChanged) st.announceNext = true;
    }
    st.threadId = threadId;
    st.reply = params.reply === "1";
    st.mode = threadId && !desk ? "thread" : "list";
    applyLayout();
    renderRange();
    renderChips();

    if (st.mode === "list") {
      const tb = el.querySelector("[data-thread-body]");
      if (tb && tb.firstChild) tb.innerHTML = ""; // no hidden copy of a post (or its reply field) stays behind
      const L = curList();
      renderList();
      const stale = Date.now() - L.at > FRESH_MS;
      if (!L.at || stale) loadPage(1, { announce: st.announceNext });
      else if (st.announceNext) loadPage(1, { announce: true });
      st.announceNext = false;
      if (threadId) {
        // desktop: the thread in the aside column
        const t = st.threads.get(threadId);
        renderThread(false);
        loadThread(threadId, { force: !t || !t.post }).then(() => { if (st.threadId === threadId && st.mode === "list") renderThread(prevThread !== threadId); });
      } else {
        renderAside();
      }
      // scroll: another category or author -> the new list's top (its newest posts and the composer; the old position
      // left a desktop list scrolled to its end); back from a phone thread page -> where the feed was; from another tab
      // -> its own position
      let y = null;
      if (filterChanged) y = 0;
      else if (prevMode === "thread" && !threadId) y = st.feedScroll;
      else if (!st.listShown) y = st.viewScroll;
      if (y != null) restoreScroll(y);
      st.listShown = true;
      // focus, once the scroll is back (after restoreScroll's frame): a deleted post's neighbour, or the opener
      const intent = st.focusIntent;
      st.focusIntent = null;
      if (intent || (leavingThread && !threadId)) {
        requestAnimationFrame(() => {
          if (!st.visible || st.mode !== "list") return;
          if (intent && intent.post !== undefined) focusPost(intent.post);
          else if (focusLost() || inThread(document.activeElement)) focusOpener(prevThread);
        });
      }
    } else {
      st.listShown = false;
      window.scrollTo(0, 0);
      HD.chrome.listReset();
      renderThread(false);
      loadThread(threadId, { force: true }).then(() => { if (st.threadId === threadId && st.mode === "thread") renderThread(true); });
    }
  }
  function restoreScroll(y) {
    const go = () => { window.scrollTo(0, y); HD.chrome.listReset(); };
    go();
    requestAnimationFrame(go);
  }

  // ---------------------------------------------------------------- deleting with Undo (§14.5)

  function deletePost(id) {
    id = Number(id);
    const p = findPost(id);
    if (!p || st.pending.has(id)) return;
    // focus leaves with the post: it goes to the next post in the list (else the one before, else the list's start)
    const nextId = neighbourPostId(el.querySelector(`[data-feed-list] .hd-post[data-post="${id}"]`));
    const pend = { id, toast: null };
    st.pending.set(id, pend);
    if (st.threadId === id) {
      // its thread closes; the list takes focus once it shows again
      st.focusIntent = { post: nextId };
      rerenderAll();
      if (st.mode === "thread") goBack();
      else HD.router.go("feed", st.filter, { replace: true });
    } else {
      rerenderAll();
      focusPost(nextId);
    }
    pend.toast = HD.toast("Post deleted", {
      action: {
        label: "Undo",
        onClick() {
          st.pending.delete(id);
          rerenderAll();
          // back to the restored post's Delete button, where the person was
          if (st.visible) HD.focusTo(el.querySelector(`[data-feed-list] [data-delete="${id}"], [data-thread-body] [data-delete="${id}"]`), { reveal: kbd() });
          HD.toast("Post restored");
        },
      },
      onClose(reason) { if (reason !== "action") commitDelete(id); },
    });
  }
  async function commitDelete(id, keepalive) {
    if (!st.pending.has(id)) return;
    st.pending.delete(id);
    try {
      await HD.api("/api/posts/" + id, { method: "DELETE", keepalive: !!keepalive });
      for (const L of st.lists.values()) L.posts = L.posts.filter((p) => p.id !== id);
      st.threads.delete(id);
      st.aside.at = 0;
    } catch (e) {
      if (e.status !== 404) HD.toast(HD.errorText(e, "post"), { kind: "error" });
    }
    rerenderAll();
  }
  function deleteComment(cid, postId) {
    cid = Number(cid);
    postId = Number(postId);
    if (st.pendingComments.has(cid)) return;
    const pend = { postId, toast: null };
    st.pendingComments.set(cid, pend);
    const c = el.querySelector(`.hd-comment[data-comment="${cid}"]`);
    if (c) {
      // focus leaves with the reply: to the next reply; after the last one, to the reply field when you use a keyboard
      // (the next stop in the page; a tap there would open the on-screen keyboard), else the reply before, else the title
      const move = c.contains(document.activeElement) || focusLost();
      const sib = (n, dir) => { while (n && (!n.matches(".hd-comment") || n.dataset.pending)) n = n[dir]; return n; };
      const thread = c.closest(".hd-post");
      const to = sib(c.nextElementSibling, "nextElementSibling") || (kbd() && thread && thread.querySelector(".hd-reply input")) ||
        sib(c.previousElementSibling, "previousElementSibling") || (thread && thread.querySelector(".hd-post__title"));
      c.dataset.pending = "true";
      if (move) HD.focusTo(to, { reveal: kbd() });
    }
    pend.toast = HD.toast("Reply deleted", {
      action: {
        label: "Undo",
        onClick() {
          st.pendingComments.delete(cid);
          const c2 = el.querySelector(`.hd-comment[data-comment="${cid}"]`);
          if (c2) { delete c2.dataset.pending; HD.focusTo(c2.querySelector("[data-delete-comment]"), { reveal: kbd() }); }
        },
      },
      onClose(reason) { if (reason !== "action") commitDeleteComment(cid); },
    });
  }
  async function commitDeleteComment(cid, keepalive) {
    if (!st.pendingComments.has(cid)) return;
    const postId = st.pendingComments.get(cid).postId;
    st.pendingComments.delete(cid);
    try {
      await HD.api("/api/comments/" + cid, { method: "DELETE", keepalive: !!keepalive });
      forEachCopy(postId, (p) => {
        if (p.comments) p.comments = p.comments.filter((x) => x.id !== cid);
        p.comment_count = Math.max(0, (p.comment_count || 1) - 1);
      });
    } catch (e) {
      if (e.status !== 404) HD.toast(HD.errorText(e), { kind: "error" });
    }
    if (st.visible) rerenderAll();
  }
  // a delete waiting for its Undo still happens when the page goes away (keepalive)
  window.addEventListener("pagehide", () => {
    for (const id of Array.from(st.pending.keys())) commitDelete(id, true);
    for (const cid of Array.from(st.pendingComments.keys())) commitDeleteComment(cid, true);
  });
  // ... and when the session ends (Log out): HD.session.clear() tells us while the token still works, so the DELETE
  // goes out with it (keepalive) and the Undo toast closes. A session the server already ended can't delete anything.
  HD.on("session-ending", ({ reason }) => {
    const send = reason === "logout";
    for (const [id, pend] of Array.from(st.pending)) {
      if (send) commitDelete(id, true); else st.pending.delete(id);
      if (pend.toast) pend.toast.close();
    }
    for (const [cid, pend] of Array.from(st.pendingComments)) {
      if (send) commitDeleteComment(cid, true); else st.pendingComments.delete(cid);
      if (pend.toast) pend.toast.close();
    }
  });

  function rerenderAll() {
    if (!st.visible) return;
    if (st.mode === "list") { keepAnchor(() => renderList()); if (st.threadId) renderThread(false); }
    else renderThread(false);
  }

  function goBack() {
    if (st.cameFromList && history.length > 1) history.back();
    else HD.router.go("feed", st.filter, { replace: true });
  }

  // ---------------------------------------------------------------- replies

  /** The reply row's field error (§7.3: under the row, alert icon and fix-it words, linked with aria-describedby). */
  function setReplyErr(form, text) {
    const input = form.querySelector("input");
    const id = input.id + "-err";
    let p = form.querySelector("#" + id);
    const desc = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter((x) => x && x !== id);
    if (!text) {
      if (p) p.remove();
      input.removeAttribute("aria-invalid");
      if (desc.length) input.setAttribute("aria-describedby", desc.join(" ")); else input.removeAttribute("aria-describedby");
      return;
    }
    if (!p) { p = document.createElement("p"); p.className = "hd-field__error"; p.id = id; form.appendChild(p); }
    p.innerHTML = `${icon("alert", { size: 20 })}<span>${esc(text)}</span>`;
    input.setAttribute("aria-invalid", "true");
    input.setAttribute("aria-describedby", desc.concat(id).join(" "));
  }

  async function sendReply(form) {
    const id = Number(form.dataset.replyForm);
    const input = form.querySelector("input");
    const text = input.value.trim();
    if (form.dataset.busy) return;
    // Send is never disabled: pressed with nothing written, it says why (§11, like the composer's Post)
    if (!text) { setReplyErr(form, "Write your reply first, then press Send."); input.focus(); return; }
    setReplyErr(form, "");
    form.dataset.busy = "1";
    const btn = form.querySelector("button");
    btn.setAttribute("aria-busy", "true");
    try {
      const c = await HD.api(`/api/posts/${id}/comments`, { method: "POST", body: { body: text } });
      forEachCopy(id, (p) => {
        if (p.comments && !p.comments.some((x) => x.id === c.id)) p.comments = p.comments.concat(c);
        p.comment_count = (p.comment_count || 0) + 1;
      });
      const t = st.threads.get(id);
      if (t && t.post && !(t.post.comments || []).some((x) => x.id === c.id)) t.post.comments = (t.post.comments || []).concat(c);
      input.value = "";
      rerenderAll();
      HD.announce("Reply posted");
      const again = el.querySelector(`#reply-${id}`);
      if (again) again.focus({ preventScroll: true });
    } catch (e) {
      if (e.status === 404) HD.toast("This post was deleted", { kind: "error" });
      else HD.toast(HD.errorText(e), { kind: "error" });
    } finally {
      delete form.dataset.busy;
      btn.removeAttribute("aria-busy");
    }
  }

  // ---------------------------------------------------------------- the composer (§7.6)

  function openComposer(opts = {}) {
    const editing = opts.post || null;
    const kindNow = editing ? editing.category : opts.category || st.filter.category || "general";
    const ev = editing && editing.event_at ? editing.event_at : "";
    const orig = { title: editing ? editing.title : "", body: editing ? editing.body : "", date: ev.slice(0, 10), time: ev.slice(11, 16), weekly: !!(editing && editing.repeats_weekly), kind: kindNow };
    const stc = { kind: kindNow, busy: false, closing: false };
    const range = rangeMi();
    const d = HD.dialog.open({
      className: "hd-composer",
      labelledBy: "cd-title",
      html: `<form class="hd-composer__form" novalidate style="display:contents">
        <div class="hd-composer__bar">
          <button class="hd-btn hd-btn--quiet hd-btn--sm" type="button" data-cancel>Cancel</button>
          <h2 class="hd-composer__title" id="cd-title" tabindex="-1">${editing ? "Edit post" : "New post"}</h2>
          <button class="hd-btn hd-btn--primary hd-btn--sm" type="submit" data-submit>${editing ? "Save" : "Post"}</button>
        </div>
        <div class="hd-composer__body">
          <div data-cd-alert></div>
          <div class="hd-field"><span class="hd-field__label" id="cd-kind">Kind of post</span>
            <div class="hd-chips" role="group" aria-labelledby="cd-kind">${HD.ui.CATEGORIES.map((c) => `<button class="hd-chip hd-chip--choice" type="button" aria-pressed="${c.id === kindNow}" data-kind="${c.id}"><span class="hd-chip__label">${esc(c.label)}</span></button>`).join("")}</div></div>
          <div class="hd-field"><label class="hd-field__label" for="cd-t">Title</label>
            <input class="hd-input" id="cd-t" name="title" maxlength="200" autocomplete="off" value="${esc(orig.title)}" aria-describedby="cd-t-h">
            <p class="hd-field__hint" id="cd-t-h">For example: Antenna party Saturday, need an Elmer</p></div>
          <div class="hd-field"><label class="hd-field__label" for="cd-b">Details</label>
            <textarea class="hd-textarea" id="cd-b" name="body" maxlength="10000" aria-describedby="cd-b-h">${esc(orig.body)}</textarea>
            <p class="hd-field__hint" id="cd-b-h">Frequencies like 146.910 become repeater chips.</p></div>
          <div class="hd-field" data-when${kindNow === "nets-events" ? "" : " hidden"}><span class="hd-field__label" id="cd-when">When</span>
            <div class="hd-fieldrow hd-fieldrow--even" role="group" aria-labelledby="cd-when">
              <div class="hd-field"><label class="hd-field__hint" for="cd-date">Date</label><input class="hd-input" id="cd-date" type="date" value="${esc(orig.date)}"></div>
              <div class="hd-field"><label class="hd-field__hint" for="cd-time">Time</label><input class="hd-input" id="cd-time" type="time" value="${esc(orig.time)}"></div>
            </div>
            <label class="hd-check" style="margin-top:4px"><input class="hd-check__input" type="checkbox" id="cd-weekly"${orig.weekly ? " checked" : ""}>Every week at this time</label></div>
          <p class="hd-composer__note">Hams within ${range}${NB}miles will see this, with your callsign and name.</p>
        </div></form>`,
      onCancel: () => cancel(),
    });
    const $ = (s) => d.querySelector(s);
    const values = () => ({ title: $("#cd-t").value, body: $("#cd-b").value, date: $("#cd-date").value, time: $("#cd-time").value, weekly: $("#cd-weekly").checked, kind: stc.kind });
    const dirty = () => {
      const v = values();
      return v.title.trim() !== orig.title.trim() || v.body.trim() !== orig.body.trim() || (editing && (v.kind !== orig.kind || v.date !== orig.date || v.time !== orig.time || v.weekly !== orig.weekly));
    };
    async function cancel() {
      if (stc.closing) return;
      if (!dirty()) { d.close("cancel"); return; }
      stc.closing = true;
      const ok = await HD.dialog.confirm({ title: "Discard this post?", body: "What you wrote will be lost.", confirm: "Discard", cancel: "Keep writing", danger: true });
      stc.closing = false;
      if (ok) d.close("cancel");
      else $("#cd-t").focus();
    }
    function setErr(input, text) {
      const field = input.closest(".hd-field");
      const id = input.id + "-err";
      let p = d.querySelector("#" + id);
      if (!text) { if (p) p.remove(); input.removeAttribute("aria-invalid"); input.setAttribute("aria-describedby", (input.getAttribute("aria-describedby") || "").replace(id, "").trim()); return; }
      if (!p) { p = document.createElement("p"); p.className = "hd-field__error"; p.id = id; field.appendChild(p); }
      p.innerHTML = `${icon("alert", { size: 20 })}<span>${text}</span>`;
      input.setAttribute("aria-invalid", "true");
      if (!(input.getAttribute("aria-describedby") || "").includes(id)) input.setAttribute("aria-describedby", ((input.getAttribute("aria-describedby") || "") + " " + id).trim());
    }
    d.addEventListener("click", (e) => {
      const k = e.target.closest("[data-kind]");
      if (k) {
        stc.kind = k.dataset.kind;
        for (const b of d.querySelectorAll("[data-kind]")) b.setAttribute("aria-pressed", String(b === k));
        $("[data-when]").hidden = stc.kind !== "nets-events";
        return;
      }
      if (e.target.closest("[data-cancel]")) cancel();
    });
    d.addEventListener("input", (e) => {
      if (e.target.id === "cd-t" && e.target.getAttribute("aria-invalid") === "true" && e.target.value.trim()) setErr(e.target, "");
      if ((e.target.id === "cd-date" || e.target.id === "cd-time") && $("#cd-date").getAttribute("aria-invalid")) setErr($("#cd-date"), "");
    });
    d.querySelector("form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (stc.busy) return; // "Posting" ignores more presses
      const v = values();
      const alertBox = $("[data-cd-alert]");
      alertBox.innerHTML = "";
      // Post is never disabled: pressing it with an empty title explains (§7.6)
      if (!v.title.trim()) { setErr($("#cd-t"), "Give your post a title, so neighbours know what it's about."); $("#cd-t").focus(); return; }
      const wantsEvent = v.kind === "nets-events";
      let eventAt = null;
      if (wantsEvent && (v.date || v.time || v.weekly)) {
        if (!v.date || !v.time) { setErr($("#cd-date"), "Give both a date and a time, or leave them empty."); $(v.date ? "#cd-time" : "#cd-date").focus(); return; }
        eventAt = `${v.date}T${v.time.slice(0, 5)}`;
      }
      const body = { title: v.title.trim(), body: v.body.trim(), category: v.kind };
      if (editing) { body.event_at = eventAt; body.repeats_weekly = !!(eventAt && v.weekly); }
      else if (eventAt) { body.event_at = eventAt; body.repeats_weekly = !!v.weekly; }
      stc.busy = true;
      const btn = $("[data-submit]");
      btn.textContent = editing ? "Saving" : "Posting";
      btn.setAttribute("aria-busy", "true");
      try {
        const saved = await HD.api(editing ? "/api/posts/" + editing.id : "/api/posts", { method: editing ? "PATCH" : "POST", body });
        stc.busy = false;
        d.close("saved");
        if (editing) afterEdit(saved); else afterPost(saved);
      } catch (err) {
        stc.busy = false;
        btn.textContent = editing ? "Save" : "Post";
        btn.removeAttribute("aria-busy");
        if (err.status === 422 && /title/.test(HD.errorFields(err).join(" "))) { setErr($("#cd-t"), "Give your post a title, so neighbours know what it's about."); $("#cd-t").focus(); return; }
        const text = err.status === 403 ? "You can only change your own posts." : err.status === 404 ? "This post was deleted"
          : err.status === 400 ? "Add your grid square or address in You first, so we know who is near you."
            : editing ? "Couldn't save. Check your connection and try again." : "Couldn't post. Check your connection and try again.";
        alertBox.innerHTML = HD.ui.alert(esc(text), "");
        const a = alertBox.querySelector(".hd-alert");
        if (a) { a.setAttribute("tabindex", "-1"); a.focus(); }
      }
    });
    // opening moves focus to the title heading (no visible ring); the keyboard opens when you tap a field
    requestAnimationFrame(() => $("#cd-title").focus());
  }

  function afterPost(post) {
    // the post appears at the top of the Feed; the filter switches to All if it would hide it
    for (const [k, L] of st.lists) {
      const [cat, author] = k.split("|");
      if ((!cat || cat === post.category) && (!author || author === post.author_callsign)) L.posts = [post].concat(L.posts.filter((p) => p.id !== post.id));
      else L.at = 0;
    }
    st.aside.at = 0;
    loadNeighbours().then((nb) => {
      const n = nb.length, range = rangeMi();
      HD.toast(n ? `Posted to ${n}${NB}${n === 1 ? "neighbour" : "neighbours"} within ${range}${NB}miles` : `Posted. No neighbours on hamdoor within ${range}${NB}miles yet.`, { kind: "ok" });
    });
    const hidden = (st.filter.category && st.filter.category !== post.category) || (st.filter.author && st.filter.author !== post.author_callsign);
    const cur = HD.router.current();
    if (hidden || !cur || cur.name !== "feed" || st.threadId) HD.router.go("feed", {});
    else { window.scrollTo(0, 0); HD.chrome.listReset(); renderList(); }
    // the composer gave focus back to what opened it; when that went away (the empty state's "Write a post"),
    // the new post takes it
    if (st.visible && focusLost()) focusPost(post.id);
  }
  function afterEdit(post) {
    forEachCopy(post.id, (p) => Object.assign(p, post, { comments: post.comments && post.comments.length ? post.comments : p.comments }));
    const t = st.threads.get(post.id);
    if (t) { t.post = post; t.at = Date.now(); }
    for (const [k, L] of st.lists) {
      const cat = k.split("|")[0];
      if (cat && cat !== post.category) L.posts = L.posts.filter((p) => p.id !== post.id);
    }
    st.aside.at = 0;
    rerenderAll(); // the Edit button the composer gave focus back to is rebuilt: its new copy takes focus (withFocus)
    if (st.visible && focusLost()) focusPost(post.id);
    HD.toast("Post updated", { kind: "ok" });
  }

  /** "Show older posts": the next page, then focus moves to the first post it brought, so keyboard and screen-reader
   *  users carry on reading there instead of after it (or to the end line, or to Try again when it failed). */
  function showOlder() {
    const posts = el.querySelectorAll("[data-feed-list] .hd-post");
    const lastId = posts.length ? posts[posts.length - 1].dataset.post : null;
    const trigger = document.activeElement;
    loadPage(curList().page + 1).then(() => {
      if (!st.visible || st.mode !== "list") return;
      const ae = document.activeElement;
      if (!(focusLost() || ae === trigger || el.querySelector("[data-feed-more]").contains(ae))) return; // the person moved on
      let next = lastId ? el.querySelector(`[data-feed-list] .hd-post[data-post="${lastId}"]`) : null;
      next = next && next.nextElementSibling;
      while (next && !next.matches(".hd-post")) next = next.nextElementSibling;
      if (next) { HD.focusTo(next, { reveal: kbd() }); return; }
      if (focusLost()) HD.focusTo(el.querySelector("[data-feed-more] [data-more], [data-feed-more] .hd-feedend"), { reveal: kbd() });
    });
  }

  // ---------------------------------------------------------------- events

  function onClick(e) {
    const t = e.target;
    const cat = t.closest("[data-cat]");
    if (cat) {
      const params = cat.dataset.cat ? { category: cat.dataset.cat } : {};
      st.announceNext = true;
      HD.router.go("feed", params, { replace: true });
      return;
    }
    if (t.closest("[data-clear-author]")) { HD.router.go("feed", {}, { replace: true }); return; }
    if (t.closest("[data-compose]")) { openComposer({ category: st.filter.category }); return; }
    if (t.closest("[data-retry]")) {
      // Try again goes away with the error: focus then lands on what came back (the posts, or a new Try again)
      curList().error = null;
      loadPage(1, { force: true }).then(() => { if (st.visible && st.mode === "list" && focusLost()) focusListStart(); });
      return;
    }
    if (t.closest("[data-more]")) { showOlder(); return; }
    // remember what opened a thread, so closing it can give focus back there
    const ot = t.closest("[data-open-thread], [data-feed-aside] a.hd-xref[href]");
    if (ot) {
      const m = /#\/feed\/post\/(\d+)/.exec(ot.getAttribute("href") || "");
      st.opener = m ? { id: Number(m[1]), sel: focusKey(ot) } : null;
    }
    const rt = t.closest("[data-retry-thread]");
    if (rt) { const id = Number(rt.dataset.retryThread); loadThread(id, { force: true }).then(() => renderThread(true)); renderThread(false); return; }
    const ed = t.closest("[data-edit]");
    if (ed) { const p = findPost(ed.dataset.edit); if (p) openComposer({ post: p }); return; }
    const del = t.closest("[data-delete]");
    if (del) { deletePost(del.dataset.delete); return; }
    const dc = t.closest("[data-delete-comment]");
    if (dc) { deleteComment(dc.dataset.deleteComment, dc.dataset.postId); return; }
    const pm = t.closest("[data-post-map]");
    if (pm) {
      const p = findPost(pm.dataset.postMap || st.threadId);
      const place = p && postPlace(p);
      if (place) HD.showOnMap({ lat: place.lat, lon: place.lon, zoom: 15, sel: "post:" + p.id });
      return;
    }
    const back = t.closest("[data-back]");
    if (back && !e.metaKey && !e.ctrlKey && !e.shiftKey) { e.preventDefault(); goBack(); return; }
    const close = t.closest("[data-thread-close]");
    if (close && !e.metaKey && !e.ctrlKey) { e.preventDefault(); HD.router.go("feed", st.filter); }
  }

  HD.router.register("feed", {
    mount(section) {
      el = section;
      skeleton();
      el.addEventListener("click", onClick);
      el.addEventListener("submit", (e) => {
        const f = e.target.closest("[data-reply-form]");
        if (f) { e.preventDefault(); sendReply(f); }
      });
      // the empty-reply error goes as soon as there is something to send
      el.addEventListener("input", (e) => {
        const f = e.target.closest && e.target.closest("[data-reply-form]");
        if (f && e.target.getAttribute("aria-invalid") === "true" && e.target.value.trim()) setReplyErr(f, "");
      });
      const relayout = () => {
        const cur = HD.router.current();
        if (cur && cur.name === "feed") show(cur.params, cur.sub);
      };
      if (deskMQ.addEventListener) deskMQ.addEventListener("change", relayout); else if (deskMQ.addListener) deskMQ.addListener(relayout);
      // fresh data: when you come back online, and when your range or place changed
      window.addEventListener("online", () => { invalidateLists(); if (st.visible && st.mode === "list") loadPage(1); });
      let lastKey = "";
      HD.on("user", ({ user }) => {
        const key = user ? [user.callsign, user.lat, user.lon, user.range_miles, user.map_precision].join("|") : "";
        if (key === lastKey) return;
        const first = !lastKey;
        lastKey = key;
        if (!user) { st.lists.clear(); st.threads.clear(); st.nb = null; st.nbBy = new Map(); st.aside = { at: 0, html: "", loading: false }; return; }
        if (!first) { invalidateLists(); st.nb = null; st.nbBy = new Map(); st.aside.at = 0; HD.repeaterChips.clear(); }
        renderRange();
      });
    },
    show,
    hide() {
      if (st.mode === "list") st.viewScroll = window.scrollY;
      st.visible = false;
      st.listShown = false;
    },
  });

  // other views open the composer or a filtered feed through these
  HD.feed = { compose: (category) => openComposer({ category }) };
})();
