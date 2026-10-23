/* You (DESIGN_SYSTEM §7.13, copy §13): your QSL card, Map privacy (with the "Right now your pin is" line,
   driven by location_source, G1d), Neighbourhood range (5 to 500 mi on a log scale), Profile, Display
   (theme, the auto-hide setting) and Account (Change password, Log out, Delete account, G7).
   Route #/you (?focus=range scrolls to the range and focuses the slider; ?focus=privacy, ?focus=grid). */
(function () {
  "use strict";
  const HD = window.HD;
  const esc = HD.esc;
  const NB = HD.fmt.NBSP;
  const icon = (n, o) => HD.icon(n, o);
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  const LOG_AT = (mi) => Math.log(mi / 5) / Math.log(100);

  let el = null;
  const st = { licence: undefined, scroll: 0, visible: false, counts: { seq: 0 }, saveTimer: 0, savingRange: false, busy: {} };
  const me = () => HD.session.user || {};

  const posOf = (mi) => Math.round(LOG_AT(Math.min(500, Math.max(5, mi))) * 100);
  const miOf = (pos) => Math.min(500, Math.max(5, Math.round((5 * Math.pow(100, pos / 100)) / 5) * 5));

  // ---------------------------------------------------------------- pieces

  function qslCard() {
    const u = me(), lic = st.licence;
    const cls = lic ? lic.license_class : null;
    const cells = [
      ["Grid", esc(HD.me.grid(u) || "Not listed")],
      ["Class", lic ? esc(HD.ui.classShort(lic.license_class)) : lic === null ? "Not listed" : " "],
      ["Licensed", lic ? esc(HD.fmt.monthYear(lic.granted) || "Not listed") : lic === null ? "Not listed" : " "],
      ["Expires", lic ? esc(HD.fmt.monthYear(lic.expires) || "Not listed") : lic === null ? "Not listed" : " "],
    ];
    const place = HD.ui.place(u.city, u.state);
    return `<article class="hd-qsl" aria-label="Your QSL card" data-qsl>
      <div class="hd-qsl__head"><span class="hd-call hd-call--hero hd-call--member">${esc(u.callsign)}</span>${lic ? HD.ui.classBadge(cls, !!lic.new) : ""}</div>
      <p class="hd-qsl__name">${esc(u.name || "")}</p>${place ? `<p class="hd-qsl__place">${esc(place)}</p>` : ""}
      ${HD.ui.qso(cells)}</article>`;
  }

  function pinLine(pin) {
    if (!pin || pin.kind === "none") {
      return "Right now you have no pin, because we couldn't place your address or grid square. Add one under Profile.";
    }
    return `Right now your pin is ${esc(pin.phrase)}${esc(pin.reason)}`;
  }

  const field = (id, name, label, value, extra = "", hint = "") =>
    `<div class="hd-field"><label class="hd-field__label" for="${id}">${label}</label><input class="hd-input" id="${id}" name="${name}" value="${esc(value || "")}"${hint ? ` aria-describedby="${id}-h"` : ""}${extra}>${hint ? `<p class="hd-field__hint" id="${id}-h">${hint}</p>` : ""}</div>`;

  function render() {
    const u = me();
    if (!u.callsign) { el.querySelector("[data-you]").innerHTML = ""; return; }
    const locator = HD.geo.shownLocator(u.grid, u.lat, u.lon);
    const range = u.range_miles || 25;
    const pos = posOf(range);
    const theme = HD.theme.get();
    el.querySelector("[data-you]").innerHTML = `<div class="hd-you">
      ${qslCard()}
      <section class="hd-section" id="you-privacy" aria-labelledby="h-privacy"><h2 class="hd-section__h" id="h-privacy">Map privacy</h2>
        <p class="hd-section__lede">Neighbours see your callsign, name, grid square and how far away you are. They never see your email.</p>
        ${HD.ui.privacyRadios("you-precision", locator, u.map_precision)}
        <p class="hd-pinnow" data-pin-now>${icon("pin", { size: 20 })}<span>${pinLine(HD.me.pin(u, st.licence || null))}</span></p></section>
      <section class="hd-section" id="you-range" aria-labelledby="h-range">
        <div class="hd-rangehead"><h2 class="hd-section__h" id="h-range"><label for="you-range-in">Neighbourhood range</label></h2><span class="hd-rangeval"><span data-range-v>${range}</span>${NB}miles</span></div>
        <div><input class="hd-range" id="you-range-in" type="range" min="0" max="100" step="1" value="${pos}" style="--val:${pos}%" aria-valuetext="${range} miles" aria-describedby="you-range-h" data-range>
          <div class="hd-range__ticks" aria-hidden="true">${[5, 25, 100, 500].map((v) => `<span style="--at:${LOG_AT(v).toFixed(4)}">${v}${v === 500 ? NB + "mi" : ""}</span>`).join("")}</div></div>
        <p class="hd-field__hint" id="you-range-h" data-range-count>Your feed and Nearby cover the neighbours on hamdoor inside this range.</p></section>
      <section class="hd-section" id="you-profile" aria-labelledby="h-profile"><h2 class="hd-section__h" id="h-profile">Profile</h2>
        <form class="hd-form" data-profile novalidate>
          <div data-profile-alert></div>
          ${field("pf-name", "name", "Name", u.name, ' autocomplete="name"')}
          ${field("pf-email", "email", "Email", u.email, ' type="email" autocomplete="email"', "Only you see your email.")}
          ${field("pf-street", "address_line", "Street address", u.address_line, ' autocomplete="street-address"')}
          ${field("pf-city", "city", "City", u.city, ' autocomplete="address-level2"')}
          <div class="hd-fieldrow">${field("pf-state", "state", "State", u.state, ' autocomplete="address-level1" maxlength="2" autocapitalize="characters"')}${field("pf-zip", "zip", "ZIP", u.zip, ' autocomplete="postal-code" inputmode="numeric" maxlength="10"')}</div>
          ${field("pf-grid", "grid", "Grid square", u.grid, ' maxlength="8" autocapitalize="characters" autocomplete="off" spellcheck="false"', "Six characters, like EM28px. Your distance to neighbours is measured from your address, or from here if we can't place the address.")}
          <p class="hd-field__hint">Changing your address or grid moves your pin and your range. Your FCC licence keeps the address on file with the FCC.</p>
          <div class="hd-btngroup"><button class="hd-btn hd-btn--primary" type="submit" data-save>Save changes</button><button class="hd-btn" type="button" data-resync>Update from FCC</button></div>
          <p class="hd-field__hint">Update from FCC replaces your name, address and grid with what's on your licence today.</p>
        </form></section>
      <section class="hd-section" id="you-display" aria-labelledby="h-display"><h2 class="hd-section__h" id="h-display">Display</h2>
        <div class="hd-seg" role="group" aria-label="Theme">${[["system", "System"], ["light", "Light"], ["dark", "Dark"]].map(([k, l]) => `<button class="hd-seg__btn" type="button" aria-pressed="${theme === k}" data-theme="${k}">${l}</button>`).join("")}</div>
        <div class="hd-switchrow"><span class="hd-switchrow__t"><span id="sw-autohide">Hide map controls while I move the map</span><span class="hd-switchrow__hint" id="sw-autohide-h">Tap the map to bring them back.</span></span>
          <button class="hd-switch" type="button" role="switch" aria-checked="${HD.chrome.autohide()}" aria-labelledby="sw-autohide" aria-describedby="sw-autohide-h" data-autohide></button></div></section>
      <section class="hd-section" id="you-account" aria-labelledby="h-account"><h2 class="hd-section__h" id="h-account">Account</h2>
        <div class="hd-btngroup"><button class="hd-btn" type="button" data-password>${icon("lock", { size: 20 })}Change password</button><button class="hd-btn hd-btn--quiet" type="button" data-logout>Log out</button></div>
        <div class="hd-btngroup"><button class="hd-btn hd-btn--quiet" type="button" data-delete-account>Delete account</button></div></section>
    </div>`;
    updateCounts(range);
  }

  function refreshPin() {
    const p = el.querySelector("[data-pin-now] span");
    if (p) p.innerHTML = pinLine(HD.me.pin(me(), st.licence || null));
  }
  function refreshCard() {
    const c = el.querySelector("[data-qsl]");
    if (c) c.outerHTML = qslCard();
  }

  // ---------------------------------------------------------------- range (§7.13.3)

  let countTimer = 0;
  function updateCounts(mi) {
    clearTimeout(countTimer);
    const seq = ++st.counts.seq;
    countTimer = setTimeout(async () => {
      const u = me();
      const out = el.querySelector("[data-range-count]");
      if (!out || u.lat == null) {
        if (out) out.textContent = "Add your grid square or address to set a neighbourhood range.";
        return;
      }
      const same = mi === u.range_miles;
      const [nb, lic] = await Promise.allSettled([
        same ? HD.neighbours() : HD.api(`/api/operators/nearby?range=${mi}`),
        HD.api(`/api/hams/near-count?radius_miles=${mi}`),
      ]);
      if (seq !== st.counts.seq || !out.isConnected) return;
      let text = "";
      if (nb.status === "fulfilled") {
        const n = (nb.value || []).length;
        text = `Your feed and Nearby cover ${n}${NB}${n === 1 ? "neighbour" : "neighbours"} on hamdoor inside this range.`;
      } else text = "Your feed and Nearby cover the neighbours on hamdoor inside this range.";
      if (lic.status === "fulfilled" && lic.value && lic.value.count != null) {
        text += ` FCC records place ${HD.fmt.num(lic.value.count)}${NB}licensed ${lic.value.count === 1 ? "ham" : "hams"} there.`;
      }
      out.textContent = text;
    }, mi === me().range_miles ? 0 : 300);
  }
  function setRangeUi(input, mi) {
    const pos = Number(input.value);
    input.style.setProperty("--val", pos + "%");
    input.setAttribute("aria-valuetext", `${mi} miles`);
    const v = el.querySelector("[data-range-v]");
    if (v) v.textContent = mi;
  }
  function onRangeInput(input) {
    const mi = miOf(Number(input.value));
    setRangeUi(input, mi);
    updateCounts(mi);
  }
  /** It saves when you let go (the change event, also for arrow keys); key repeats are coalesced into one save. */
  function onRangeChange(input) {
    clearTimeout(st.saveTimer);
    st.saveTimer = setTimeout(async () => {
      st.saveTimer = 0;
      const mi = miOf(Number(input.value));
      const before = me().range_miles;
      if (mi === before) return;
      try {
        const u = await HD.api("/api/me", { method: "PATCH", body: { range_miles: mi } });
        HD.session.setUser(u);
        HD.toast(`Saved. Your feed covers ${mi}${NB}miles.`, { kind: "ok" });
      } catch (e) {
        input.value = posOf(before);
        setRangeUi(input, before);
        updateCounts(before);
        HD.toast(HD.errorText(e), { kind: "error" });
      }
    }, 450);
  }
  function onRangeKey(e) {
    // arrows step to the next 5-mile value (several slider positions share one value at the low end)
    const input = e.target;
    const dir = e.key === "ArrowRight" || e.key === "ArrowUp" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowDown" ? -1 : 0;
    if (!dir) return;
    e.preventDefault();
    const cur = miOf(Number(input.value));
    let p = Number(input.value);
    while (p + dir >= 0 && p + dir <= 100) {
      p += dir;
      if (miOf(p) !== cur) break;
    }
    input.value = String(p);
    onRangeInput(input);
    onRangeChange(input);
  }

  // ---------------------------------------------------------------- privacy (§7.13.2)

  async function onPrecision(radio) {
    const want = radio.value === "grid" ? "grid" : "address";
    const before = me().map_precision || "address";
    if (want === before) return;
    try {
      const u = await HD.api("/api/me", { method: "PATCH", body: { map_precision: want } });
      HD.session.setUser(u);
      const pin = await HD.me.pinAsync(u);
      refreshPin();
      HD.toast(pin && pin.phrase ? `Saved. Neighbours now see your pin ${pin.phrase}.` : "Saved", { kind: "ok" });
    } catch (e) {
      // a failed save puts the radio back and says why
      for (const r of el.querySelectorAll('input[name="you-precision"]')) r.checked = r.value === before;
      HD.toast(HD.errorText(e), { kind: "error" });
    }
  }

  // ---------------------------------------------------------------- profile (§7.13.4)

  function setErr(input, text) {
    const field = input.closest(".hd-field");
    const id = input.id + "-err";
    let p = field.querySelector("#" + id);
    const desc = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter((x) => x && x !== id);
    if (!text) {
      if (p) p.remove();
      input.removeAttribute("aria-invalid");
      if (desc.length) input.setAttribute("aria-describedby", desc.join(" ")); else input.removeAttribute("aria-describedby");
      return;
    }
    if (!p) { p = document.createElement("p"); p.className = "hd-field__error"; p.id = id; field.appendChild(p); }
    p.innerHTML = `${icon("alert", { size: 20 })}<span>${text}</span>`;
    input.setAttribute("aria-invalid", "true");
    desc.push(id);
    input.setAttribute("aria-describedby", desc.join(" "));
  }

  async function saveProfile(form) {
    if (st.busy.save) return;
    const u = me();
    const box = form.querySelector("[data-profile-alert]");
    box.innerHTML = "";
    const val = (n) => form.querySelector(`[name="${n}"]`).value.trim();
    const emailIn = form.querySelector("#pf-email"), gridIn = form.querySelector("#pf-grid");
    setErr(emailIn, "");
    setErr(gridIn, "");
    let bad = null;
    if (val("email") !== (u.email || "") && !EMAIL_RE.test(val("email"))) { setErr(emailIn, "Enter an email address like you@example.com."); bad = emailIn; }
    const g = val("grid");
    if (g && !HD.geo.normGrid(g)) { setErr(gridIn, "Enter a grid square like EM28 or EM28px."); bad = bad || gridIn; }
    if (bad) { bad.focus(); return; }
    const changes = {};
    for (const n of ["name", "email", "address_line", "city", "state", "zip"]) if (val(n) !== (u[n] || "")) changes[n] = n === "state" ? val(n).toUpperCase() : val(n);
    const grid = g ? HD.geo.normGrid(g) : "";
    if (grid !== (u.grid || "")) changes.grid = grid;
    if (!Object.keys(changes).length) { HD.toast("Saved", { kind: "ok" }); return; }
    st.busy.save = true;
    const btn = form.querySelector("[data-save]");
    btn.setAttribute("aria-busy", "true");
    btn.textContent = "Saving";
    try {
      const nu = await HD.api("/api/me", { method: "PATCH", body: changes });
      const moved = nu.lat !== u.lat || nu.lon !== u.lon;
      HD.session.setUser(nu);
      HD.me.licence({ force: true }).then((l) => { st.licence = l; refreshCard(); refreshPin(); }).catch(() => {});
      refreshCard();
      if (moved) refreshPin();
      HD.toast("Saved", { kind: "ok" });
    } catch (e) {
      if (e.status === 422 && /email/.test(HD.errorFields(e).join(" "))) { setErr(emailIn, "Enter an email address like you@example.com."); emailIn.focus(); }
      else {
        box.innerHTML = HD.ui.alert("Couldn't save your profile", esc(HD.errorText(e)));
        const a = box.querySelector(".hd-alert");
        if (a) { a.setAttribute("tabindex", "-1"); a.focus(); }
      }
    } finally {
      st.busy.save = false;
      btn.removeAttribute("aria-busy");
      btn.textContent = "Save changes";
    }
  }

  async function resync(btn) {
    if (st.busy.resync) return;
    const hadFocus = btn === document.activeElement || document.activeElement === document.body;
    st.busy.resync = true;
    btn.setAttribute("aria-busy", "true");
    btn.textContent = "Updating from FCC";
    const u = me();
    try {
      const nu = await HD.api("/api/me/refresh-callsign", { method: "POST" });
      const keys = ["name", "address_line", "city", "state", "zip", "grid", "lat", "lon"];
      const changed = keys.some((k) => (nu[k] ?? "") !== (u[k] ?? ""));
      HD.session.setUser(nu);
      st.licence = await HD.me.licence({ force: true }).catch(() => st.licence);
      render();
      HD.toast(changed ? "Updated from your FCC licence." : "Updated from your FCC licence. Nothing had changed.", { kind: "ok" });
    } catch (e) {
      if (e.status === 404) HD.toast(`FCC records don't have ${u.callsign} right now. Try again later.`, { kind: "error" });
      else HD.toast(HD.errorText(e), { kind: "error" });
    } finally {
      st.busy.resync = false;
      const b = el.querySelector("[data-resync]");
      if (b) { b.removeAttribute("aria-busy"); b.textContent = "Update from FCC"; }
      // render() rebuilt the page: the button the person pressed (its new copy) keeps keyboard focus
      if (b && hadFocus && (document.activeElement === document.body || !el.contains(document.activeElement))) HD.focusTo(b);
    }
  }

  // ---------------------------------------------------------------- account (§7.13.6, §7.7 dialogs)

  function passwordDialog() {
    const u = me();
    const d = HD.dialog.open({
      labelledBy: "pw-title",
      html: `<form class="hd-form" novalidate>
        <h2 class="hd-dialog__title" id="pw-title">Change your password</h2>
        <input type="text" name="username" autocomplete="username" value="${esc(u.callsign)}" hidden>
        <div data-pw-alert></div>
        <div class="hd-field"><label class="hd-field__label" for="pw-cur">Current password</label><input class="hd-input" id="pw-cur" type="password" autocomplete="current-password"></div>
        <div class="hd-field"><label class="hd-field__label" for="pw-new">New password</label><input class="hd-input" id="pw-new" type="password" autocomplete="new-password" aria-describedby="pw-new-h"><p class="hd-field__hint" id="pw-new-h">At least 8 characters</p></div>
        <div class="hd-dialog__actions" style="margin-top:4px"><button class="hd-btn hd-btn--quiet" type="button" data-cancel>Cancel</button><button class="hd-btn hd-btn--primary" type="submit">Change password</button></div>
      </form>`,
    });
    d.querySelector("[data-cancel]").addEventListener("click", () => d.close("cancel"));
    d.querySelector("#pw-cur").focus();
    let busy = false;
    d.querySelector("form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return;
      const cur = d.querySelector("#pw-cur"), nw = d.querySelector("#pw-new");
      d.querySelector("[data-pw-alert]").innerHTML = "";
      setErr(cur, "");
      setErr(nw, "");
      if (!cur.value) { setErr(cur, "Enter your current password."); cur.focus(); return; }
      if (nw.value.length < 8) { setErr(nw, "Use at least 8 characters."); nw.focus(); return; }
      busy = true;
      try {
        const r = await HD.api("/api/me/password", { method: "POST", body: { current_password: cur.value, new_password: nw.value } });
        // the change ends every other session; this one carries on with the fresh token
        if (r && r.token) HD.session.set(r.token, r.user || HD.session.user);
        d.close("ok");
        HD.toast("Password changed", { kind: "ok" });
      } catch (err) {
        busy = false;
        if (err.status === 400) { setErr(cur, "That isn't your current password. Check it and try again."); cur.focus(); cur.select(); }
        else if (err.status === 422) { setErr(nw, "Use at least 8 characters."); nw.focus(); }
        else d.querySelector("[data-pw-alert]").innerHTML = HD.ui.alert("Couldn't change your password", esc(HD.errorText(err)));
      }
    });
  }

  function deleteDialog() {
    const u = me();
    const d = HD.dialog.open({
      labelledBy: "del-title",
      html: `<form class="hd-form" novalidate>
        <h2 class="hd-dialog__title" id="del-title">Delete your hamdoor account?</h2>
        <p class="hd-dialog__body" style="margin-top:0">Your posts and comments are removed. Your FCC licence stays public, because it is.</p>
        <input type="text" name="username" autocomplete="username" value="${esc(u.callsign)}" hidden>
        <div data-del-alert></div>
        <div class="hd-field"><label class="hd-field__label" for="del-pw">Your password</label><input class="hd-input" id="del-pw" type="password" autocomplete="current-password"></div>
        <div class="hd-dialog__actions" style="margin-top:4px"><button class="hd-btn hd-btn--quiet" type="button" data-cancel>Keep my account</button><button class="hd-btn hd-btn--danger" type="submit">Delete account</button></div>
      </form>`,
    });
    d.querySelector("[data-cancel]").addEventListener("click", () => d.close("cancel"));
    d.querySelector("[data-cancel]").focus();
    let busy = false;
    d.querySelector("form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return;
      const pw = d.querySelector("#del-pw");
      setErr(pw, "");
      d.querySelector("[data-del-alert]").innerHTML = "";
      if (!pw.value) { setErr(pw, "Enter your password to delete your account."); pw.focus(); return; }
      busy = true;
      try {
        await HD.api("/api/me", { method: "DELETE", body: { password: pw.value } });
        d.close("ok");
        HD.session.clear("deleted"); // the server removed the posts too: nothing pending is sent
        HD.router.go("auth", { mode: "signup" }, { replace: true });
        HD.toast("Your hamdoor account is deleted.");
      } catch (err) {
        busy = false;
        if (err.status === 400) { setErr(pw, "That isn't your password. Check it and try again."); pw.focus(); pw.select(); }
        else d.querySelector("[data-del-alert]").innerHTML = HD.ui.alert("Couldn't delete your account", esc(HD.errorText(err)));
      }
    });
  }

  // ---------------------------------------------------------------- focus targets (#/you?focus=range)

  function applyFocus(params) {
    const f = params && params.focus;
    if (!f) return false;
    const target = f === "range" ? el.querySelector("#you-range") : f === "privacy" ? el.querySelector("#you-privacy") : f === "grid" ? el.querySelector("#pf-grid") : null;
    if (!target) return false;
    requestAnimationFrame(() => {
      const bar = el.querySelector("[data-topbar]");
      const barH = bar ? bar.getBoundingClientRect().bottom : 0;
      const y = target.getBoundingClientRect().top + window.scrollY - barH - 16;
      window.scrollTo(0, Math.max(0, y));
      HD.chrome.listReset();
      const focusEl = f === "range" ? el.querySelector("#you-range-in") : f === "privacy" ? el.querySelector('input[name="you-precision"]:checked') : target;
      if (focusEl) focusEl.focus({ preventScroll: true });
    });
    return true;
  }

  // ---------------------------------------------------------------- view

  HD.router.register("you", {
    mount(section) {
      el = section;
      el.innerHTML = `<header class="hd-topbar" data-tuck="top" data-topbar><div class="hd-topbar__row"><h1 class="hd-topbar__title" id="you-title" tabindex="-1">You</h1></div></header>
        <div class="hd-page"><div class="hd-page__col hd-page__col--wide" data-you></div></div>`;
      HD.chrome.watchTopbar(el.querySelector("[data-topbar]"));
      el.addEventListener("input", (e) => {
        if (e.target.matches("[data-range]")) onRangeInput(e.target);
        else if (e.target.getAttribute("aria-invalid") === "true") setErr(e.target, "");
      });
      el.addEventListener("change", (e) => {
        if (e.target.matches("[data-range]")) onRangeChange(e.target);
        else if (e.target.name === "you-precision") onPrecision(e.target);
      });
      el.addEventListener("keydown", (e) => { if (e.target.matches && e.target.matches("[data-range]")) onRangeKey(e); });
      el.addEventListener("submit", (e) => { if (e.target.matches("[data-profile]")) { e.preventDefault(); saveProfile(e.target); } });
      el.addEventListener("click", (e) => {
        const t = e.target;
        const th = t.closest("[data-theme]");
        if (th) {
          HD.theme.set(th.dataset.theme);
          for (const b of el.querySelectorAll("[data-theme]")) b.setAttribute("aria-pressed", String(b === th));
          return;
        }
        const ah = t.closest("[data-autohide]");
        if (ah) {
          const on = ah.getAttribute("aria-checked") !== "true";
          HD.chrome.setAutohide(on);
          ah.setAttribute("aria-checked", String(on));
          return;
        }
        if (t.closest("[data-resync]")) { resync(t.closest("[data-resync]")); return; }
        if (t.closest("[data-password]")) { passwordDialog(); return; }
        if (t.closest("[data-delete-account]")) { deleteDialog(); return; }
        if (t.closest("[data-logout]")) {
          // no confirmation: clears the session and goes to Log in
          HD.session.clear();
          HD.router.go("auth", { mode: "login" }, { replace: true });
        }
      });
      HD.on("user", ({ user }) => { if (!user) st.licence = undefined; });
      // Log out right after letting go of the range slider: the save waiting for its 450 ms goes out now, while the
      // token works, instead of failing after it as "Your session ended" on the log-in screen
      HD.on("session-ending", ({ reason }) => {
        if (!st.saveTimer) return;
        clearTimeout(st.saveTimer);
        st.saveTimer = 0;
        const input = el.querySelector("[data-range]");
        const mi = input ? miOf(Number(input.value)) : 0;
        if (reason === "logout" && mi && mi !== me().range_miles) HD.api("/api/me", { method: "PATCH", body: { range_miles: mi }, keepalive: true }).catch(() => {});
      });
    },
    show(params) {
      st.visible = true;
      render();
      if (!applyFocus(params)) { window.scrollTo(0, st.scroll); HD.chrome.listReset(); }
      // your licence (class, dates) and a fresh /api/me (the pin can move after an FCC import)
      HD.me.licence().then((l) => { if (st.licence !== l) { st.licence = l; if (st.visible) { refreshCard(); refreshPin(); } } }).catch(() => { st.licence = st.licence || null; });
      HD.api("/api/me").then((u) => {
        const before = JSON.stringify(me());
        if (JSON.stringify(u) !== before) {
          HD.session.setUser(u);
          if (st.visible && !el.contains(document.activeElement)) { const y = window.scrollY; render(); window.scrollTo(0, y); HD.chrome.listReset(); }
        }
      }).catch(() => {});
    },
    hide() {
      st.scroll = window.scrollY;
      st.visible = false;
    },
  });
})();
