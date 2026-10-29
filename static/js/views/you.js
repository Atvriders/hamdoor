/* You (DESIGN_SYSTEM §7.13, copy §13): your QSL card, Map privacy (with the "Right now your pin is" line,
   driven by location_source, G1d), Neighbourhood range (5 to 500 mi on a log scale), Profile, Display
   (theme, the auto-hide setting), Account (Change password, Log out, Delete account, G7) and, for the admins
   listed in ADMIN_CALLSIGNS, Admin (find a member; reset their password, unlock their log in, delete their
   account). After an admin reset (must_change_password) the password dialog opens on every boot and log in,
   and the page carries a banner, until the member chooses a new password.
   Route #/you (?focus=range scrolls to the range and focuses the slider; ?focus=privacy, ?focus=grid, ?focus=admin). */
(function () {
  "use strict";
  const HD = window.HD;
  const esc = HD.esc;
  const NB = HD.fmt.NBSP;
  const icon = (n, o) => HD.icon(n, o);
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  const LOG_AT = (mi) => Math.log(mi / 5) / Math.log(100);
  const MUST_CHANGE = "An admin reset your password. Choose a new one to keep using hamdoor.";
  const ADMIN_LIMIT = 50;
  /** Why an admin's account (yours, or another admin's line) has no Delete account. The server refuses it while
   *  the callsign is in ADMIN_CALLSIGNS: sign up checks only FCC records, so whoever signed up with the callsign
   *  next would be an admin. Plain text: escape it for HTML. */
  const adminKeepText = (call, yours) =>
    `An admin's account can't be deleted. To delete ${yours ? "yours" : "this one"}, take ${call} out of ADMIN_CALLSIGNS and restart hamdoor first.`;
  /** That refusal (400) from DELETE /api/me or /api/admin/users/{callsign}: the page was drawn before an operator listed the callsign. */
  const adminKept = (e) => !!e && e.status === 400 && /ADMIN_CALLSIGNS/.test(e.message || "");

  let el = null;
  const freshAdmin = () => ({ q: "", data: null, dataQ: "", error: null, seq: 0, ctl: null, timer: 0, busy: "" });
  const st = { licence: undefined, scroll: 0, visible: false, counts: { seq: 0 }, saveTimer: 0, savingRange: false, busy: {}, admin: freshAdmin(), mustPrompted: null };
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
      ${mustChangeBanner()}
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
          ${field("pf-email", "email", "Email", u.email, ' type="email" autocomplete="email"', "Only you and this server's admins see your email.")}
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
        ${u.is_admin ? `<p class="hd-field__hint hd-account__note" data-admin-keep>${esc(adminKeepText(u.callsign, true))}</p>`
          : '<div class="hd-btngroup"><button class="hd-btn hd-btn--quiet" type="button" data-delete-account>Delete account</button></div>'}</section>
      ${adminSection()}
    </div>`;
    updateCounts(range);
  }

  /** After an admin reset: the way to the password dialog stays on the page until the password changes. */
  function mustChangeBanner() {
    if (!me().must_change_password) return "";
    return `<div class="hd-banner" data-mustchange>${icon("lock", { size: 24 })}<p class="hd-banner__text">${MUST_CHANGE}</p>
      <button class="hd-btn hd-btn--primary" type="button" data-password>Change password</button></div>`;
  }
  /** The password changed: the banner goes, and focus that was on its button lands on the page title. */
  function refreshMustChange() {
    const b = el && el.querySelector("[data-mustchange]");
    if (!b || me().must_change_password) return;
    const had = b.contains(document.activeElement) || document.activeElement === document.body;
    b.remove();
    if (had && st.visible) HD.focusTo(el.querySelector("#you-title"));
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

  /** Change your password. After an admin reset (must_change_password) it says so, and the password you type
   *  first is the temporary one the admin gave you; the new one has to differ from it. */
  function passwordDialog() {
    const u = me();
    const must = !!u.must_change_password;
    const SAME = "Choose a password that's different from the temporary one.";
    const d = HD.dialog.open({
      labelledBy: "pw-title",
      html: `<form class="hd-form" novalidate>
        <h2 class="hd-dialog__title" id="pw-title">Change your password</h2>
        ${must ? `<p class="hd-dialog__body" style="margin-top:0" data-pw-must>${MUST_CHANGE}</p>` : ""}
        <input type="text" name="username" autocomplete="username" value="${esc(u.callsign)}" hidden>
        <div data-pw-alert></div>
        <div class="hd-field"><label class="hd-field__label" for="pw-cur">${must ? "Temporary password" : "Current password"}</label><input class="hd-input" id="pw-cur" type="password" autocomplete="current-password"></div>
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
      if (!cur.value) { setErr(cur, must ? "Enter the temporary password an admin gave you." : "Enter your current password."); cur.focus(); return; }
      if (nw.value.length < 8) { setErr(nw, "Use at least 8 characters."); nw.focus(); return; }
      if (must && nw.value === cur.value) { setErr(nw, SAME); nw.focus(); return; }
      busy = true;
      try {
        const r = await HD.api("/api/me/password", { method: "POST", body: { current_password: cur.value, new_password: nw.value } });
        // the change ends every other session; this one carries on with the fresh token (and is no longer asked)
        if (r && r.token) HD.session.set(r.token, Object.assign({}, r.user || HD.session.user, { must_change_password: false }));
        d.close("ok");
        HD.toast("Password changed", { kind: "ok" });
        refreshMustChange();
      } catch (err) {
        busy = false;
        if (err.status === 401) { d.close("expired"); return; } // the session ended (HD.api went to Log in)
        if (err.status === 400 && /temporary/i.test(err.message)) { setErr(nw, SAME); nw.focus(); }
        else if (err.status === 400) {
          setErr(cur, must ? "That isn't the temporary password. Check it and try again." : "That isn't your current password. Check it and try again.");
          cur.focus();
          cur.select();
        } else if (err.status === 422) { setErr(nw, "Use at least 8 characters."); nw.focus(); }
        else d.querySelector("[data-pw-alert]").innerHTML = HD.ui.alert("Couldn't change your password", esc(HD.errorText(err)));
      }
    });
  }

  /** On each boot and log in (a new session token), a member whose password an admin reset is asked for a new
   *  one. Closed, it waits for the next boot or log in; the You page keeps its banner meanwhile. */
  function promptMustChange() {
    const u = HD.session.user, token = HD.session.token;
    if (!token || !u || !u.must_change_password || st.mustPrompted === token) return;
    if (document.querySelector("dialog[open]")) return; // never on top of another dialog; the next view change asks
    st.mustPrompted = token;
    passwordDialog();
  }

  function deleteDialog() {
    const u = me();
    let madeAdmin = null; // the server refused: listed in ADMIN_CALLSIGNS since this page was drawn
    const d = HD.dialog.open({
      labelledBy: "del-title",
      // then the page shows you as an admin once the dialog closes: the note instead of Delete account, and Admin
      onClose: () => { if (madeAdmin) madeAdmin.then(redrawAsAdmin); },
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
        if (adminKept(err)) {
          d.querySelector("[data-del-alert]").innerHTML = HD.ui.alert("Couldn't delete your account", esc(adminKeepText(u.callsign, true)));
          madeAdmin = madeAdmin || HD.api("/api/me").then((nu) => HD.session.setUser(nu)).catch(() => {});
        } else if (err.status === 400) { setErr(pw, "That isn't your password. Check it and try again."); pw.focus(); pw.select(); }
        else d.querySelector("[data-del-alert]").innerHTML = HD.ui.alert("Couldn't delete your account", esc(HD.errorText(err)));
      }
    });
  }

  /** The session user turned out to be an admin: draw the page again where it was, focus the note that says why
   *  Delete account went, and load the member list. */
  function redrawAsAdmin() {
    if (!st.visible || !me().is_admin) return;
    const y = window.scrollY;
    render();
    window.scrollTo(0, y);
    HD.chrome.listReset();
    HD.focusTo(el.querySelector("[data-admin-keep]"));
    loadMembers();
  }

  // ---------------------------------------------------------------- admin (members listed in ADMIN_CALLSIGNS)

  function adminSection() {
    if (!me().is_admin) return "";
    return `<section class="hd-section" id="you-admin" aria-labelledby="h-admin"><h2 class="hd-section__h" id="h-admin">Admin</h2>
        <p class="hd-section__lede">You're an admin of this hamdoor server. Find a member to reset their password, let them log in again or delete their account.</p>
        <form class="hd-form" role="search" aria-label="Members" data-admin-search novalidate>
          <div class="hd-field"><label class="hd-field__label" for="ad-q">Find a member</label>
            <input class="hd-input" id="ad-q" name="q" type="search" value="${esc(st.admin.q)}" maxlength="64" autocomplete="off" autocapitalize="none" spellcheck="false" enterkeyhint="search" aria-describedby="ad-q-h" aria-controls="ad-list">
            <p class="hd-field__hint" id="ad-q-h">Callsign or name, like W1AW or Smith</p></div>
        </form>
        <div class="hd-admin" id="ad-list" data-admin-list>${adminListHtml()}</div></section>`;
  }

  const quoted = (q) => `“${esc(q)}”`;
  /** "5 members on this server", "2 members match “smi”", "Showing 50 of 312 members. Search to find the others." */
  function countText(total, shown, q) {
    const n = HD.fmt.plural(total, "member");
    if (!q) return shown < total ? `Showing ${shown} of ${n}. Search to find the others.` : `${n} on this server`;
    if (shown < total) return `Showing ${shown} of the ${n} that match ${quoted(q)}. Type more to narrow it down.`;
    return `${n} ${total === 1 ? "matches" : "match"} ${quoted(q)}`;
  }

  function memberHtml(m) {
    const call = esc(m.callsign);
    const you = m.callsign === me().callsign;
    const place = HD.ui.place(m.city, m.state);
    const count = (n, one, many) => (n ? HD.fmt.plural(n, one, many) : "no " + many);
    // a sentence of its own: "No posts, 1 reply." (it started "no posts" after "Joined Aug 16.")
    const wrote = m.posts || m.comments ? HD.fmt.cap(`${count(m.posts, "post", "posts")}, ${count(m.comments, "reply", "replies")}`) : "No posts or replies yet";
    const tags = [
      you ? '<span class="hd-tag hd-tag--you">You</span>' : "",
      m.is_admin ? '<span class="hd-tag">Admin</span>' : "",
      m.must_change_password ? '<span class="hd-tag">Must change password</span>' : "",
      m.locked ? `<span class="hd-tag hd-tag--alert">${icon("lock", { size: 16 })}Locked out</span>` : "",
    ].join("");
    const btn = (act, label, ic) =>
      `<button class="hd-btn hd-btn--quiet hd-btn--sm" type="button" data-admin-act="${act}">${icon(ic, { size: 20 })}<span data-label>${label}</span><span class="hd-sr"> for ${call}</span></button>`;
    const actions = [ // a locked-out member most likely needs Unlock: it comes first
      m.locked ? btn("unlock", "Unlock log in", "check") : "",
      you ? "" : btn("reset", "Reset password", "lock"),
      you || m.is_admin ? "" : btn("delete", "Delete account", "trash"), // an admin's account stays (adminKeepText)
    ].join("");
    const note = you ? "To change your own password, use Account above." : m.is_admin ? adminKeepText(m.callsign, false) : "";
    return `<li class="hd-member${you ? " hd-member--you" : ""}" data-member="${call}">
        <p class="hd-member__head"><span class="hd-call hd-call--list hd-call--member">${call}</span><span class="hd-member__name">${esc(m.name || "")}</span></p>
        ${place ? `<p class="hd-member__meta">${esc(place)}</p>` : ""}
        <p class="hd-member__meta">Joined ${esc(HD.fmt.ago(m.created_at))}. ${wrote}.</p>
        ${tags ? `<p class="hd-member__tags">${tags}</p>` : ""}
        ${actions ? `<div class="hd-member__actions">${actions}</div>` : ""}
        ${note ? `<p class="hd-member__note">${esc(note)}</p>` : ""}</li>`;
  }

  function adminListHtml() {
    const a = st.admin;
    if (!a.data && a.error) {
      const body = !a.error.status || a.error.status >= 500 ? "Check your connection and try again." : esc(HD.errorText(a.error));
      return `${HD.ui.alert("Couldn't load members", body)}<button class="hd-btn" type="button" data-admin-retry>${icon("refresh", { size: 20 })}Try again</button>`;
    }
    if (!a.data) return `<p class="hd-admin__count">${icon("clock", { size: 20 })}<span>Loading members.</span></p>`;
    const { users = [], total = 0 } = a.data;
    if (!users.length) {
      return `<p class="hd-admin__count" data-admin-count>${a.dataQ ? `No member matches ${quoted(a.dataQ)}. Check the letters and zeros.` : "No members yet."}</p>`;
    }
    return `<p class="hd-admin__count" data-admin-count>${countText(total, users.length, a.dataQ)}</p>
      <ul class="hd-members" aria-label="Members">${users.map(memberHtml).join("")}</ul>`;
  }

  const memberSel = (call) => `[data-member="${CSS.escape(call)}"]`;
  /** Re-draw the list, keeping keyboard focus on the same control of the same member; `fallback` decides when it is gone. */
  function renderMembers(fallback) {
    const box = el && el.querySelector("[data-admin-list]");
    if (!box) return;
    HD.keepFocus(box, (node) => {
      const li = node.closest("[data-member]"), act = node.closest("[data-admin-act]");
      if (li && act) return `${memberSel(li.dataset.member)} [data-admin-act="${act.dataset.adminAct}"]`;
      if (node.matches("[data-admin-retry]")) return "[data-admin-retry]";
      return null;
    }, () => { box.innerHTML = adminListHtml(); }, fallback);
  }

  /** GET /api/admin/users for the search field's text. A newer search supersedes an older one. */
  async function loadMembers(opts = {}) {
    const a = st.admin;
    if (!me().is_admin) return;
    clearTimeout(a.timer);
    if (a.ctl) a.ctl.abort();
    const ctl = (a.ctl = new AbortController());
    const seq = ++a.seq;
    const q = a.q.trim();
    try {
      a.data = await HD.api(`/api/admin/users?q=${encodeURIComponent(q)}&limit=${ADMIN_LIMIT}`, { signal: ctl.signal });
      a.dataQ = q;
      a.error = null;
    } catch (e) {
      if (HD.isAbort(e) || seq !== a.seq) return;
      if (e.status === 403) { lostAdmin(!!opts.announce); return; }
      if (e.status === 401) return; // HD.api ended the session
      a.error = e;
      a.data = null;
    } finally {
      if (seq === a.seq) a.ctl = null;
    }
    if (seq !== a.seq || !st.visible) return;
    renderMembers(opts.fallback);
    if (opts.announce) {
      const n = el.querySelector("[data-admin-count]");
      if (n) HD.announce(n.textContent);
    }
  }
  function searchSoon() {
    clearTimeout(st.admin.timer);
    st.admin.timer = setTimeout(() => loadMembers({ announce: true }), 300);
  }

  /** The server no longer counts you as an admin (ADMIN_CALLSIGNS changed): say so when you asked for
   *  something, and the section goes. */
  function lostAdmin(say = true) {
    if (say) HD.toast("Only admins can do this.", { kind: "error" });
    HD.api("/api/me").then((u) => {
      HD.session.setUser(u);
      const s = el.querySelector("#you-admin");
      if (!s || u.is_admin) return;
      const had = s.contains(document.activeElement);
      s.remove();
      if (had && st.visible) HD.focusTo(el.querySelector("#you-title"));
    }).catch(() => {});
  }

  /** The §13 sentence for a failed admin action on `call`. */
  function adminErrorText(e, call) {
    if (e.status === 403) return "Only admins can do this.";
    if (e.status === 404) return `${call} doesn't have a hamdoor account any more.`;
    if (adminKept(e)) return adminKeepText(call, false);
    return HD.errorText(e);
  }
  /** Says why an admin action failed. Returns whether the member list is worth loading again. */
  function adminFailed(e, call) {
    if (e.status === 401) return false; // HD.api ended the session and went to Log in
    if (e.status === 403) { lostAdmin(); return false; }
    HD.toast(adminErrorText(e, call), { kind: "error" });
    return true;
  }

  function setBusy(btn, label) {
    const l = btn && btn.querySelector("[data-label]");
    if (!l) return;
    if (label) { btn.dataset.idle = l.textContent; l.textContent = label; btn.setAttribute("aria-busy", "true"); }
    else { l.textContent = btn.dataset.idle || l.textContent; btn.removeAttribute("aria-busy"); }
  }
  /** When the control that had focus is gone after a re-draw: the member's first remaining control, else the count. */
  const focusMemberOr = (call) => () => {
    const li = el.querySelector(`[data-admin-list] ${memberSel(call)}`);
    HD.focusTo((li && li.querySelector("button")) || li || el.querySelector("[data-admin-count]") || el.querySelector("#ad-q"));
  };

  async function resetPassword(call, btn) {
    const ok = await HD.dialog.confirm({
      title: `Reset ${call}'s password?`,
      body: "They'll be logged out everywhere and must choose a new password at their next log in.",
      confirm: "Reset password",
    });
    if (!ok || st.admin.busy) return;
    st.admin.busy = call;
    setBusy(btn, "Resetting");
    let reload = true;
    try {
      const r = await HD.api(`/api/admin/users/${encodeURIComponent(call)}/reset-password`, { method: "POST" });
      setBusy(btn, "");
      await temporaryPasswordDialog(r.callsign || call, r.temporary_password);
    } catch (e) {
      setBusy(btn, "");
      reload = adminFailed(e, call);
    } finally {
      st.admin.busy = "";
    }
    if (reload) await loadMembers({ fallback: focusMemberOr(call) });
  }

  /** The temporary password, shown this once: large, selectable, with Copy. Never toasted, never stored. */
  function temporaryPasswordDialog(call, password) {
    return new Promise((resolve) => {
      let v = null;
      const fit = () => { if (v && v.isConnected) fitPassword(v); };
      const d = HD.dialog.open({
        labelledBy: "tp-title",
        onClose: () => { window.removeEventListener("resize", fit); resolve(); },
        html: `<form method="dialog">
          <h2 class="hd-dialog__title" id="tp-title">Temporary password for ${esc(call)}</h2>
          <div class="hd-temppw"><span class="hd-temppw__k">Temporary password</span><span class="hd-temppw__v" data-temppw>${esc(password)}</span></div>
          <p class="hd-dialog__body hd-temppw__give">Give this to ${esc(call)}. They'll be asked to choose a new password when they log in.</p>
          <p class="hd-field__hint hd-temppw__hint">hamdoor shows it only this once. If it gets lost, reset the password again.</p>
          <p class="hd-temppw__status" role="status" data-copy-status></p>
          <div class="hd-dialog__actions"><button class="hd-btn hd-btn--quiet" value="done">Done</button><button class="hd-btn hd-btn--primary" type="button" data-copy>${icon("copy", { size: 20 })}Copy</button></div>
        </form>`,
      });
      v = d.querySelector("[data-temppw]");
      fit(); // before the first paint; again when the window changes size (a phone turning) or a font arrives late
      window.addEventListener("resize", fit);
      if (document.fonts) document.fonts.ready.then(fit, () => {});
      const copy = d.querySelector("[data-copy]"), status = d.querySelector("[data-copy-status]");
      copy.addEventListener("click", async () => {
        const done = await copyText(password, v);
        status.textContent = done ? "Copied." : "Couldn't copy. Select the password and copy it yourself.";
        status.classList.toggle("hd-temppw__status--error", !done);
      });
      copy.focus();
    });
  }

  /** The temporary password stays on one line at its full size (app.css); one too wide for its box shrinks just
   *  enough to fit. W, M and m are the widest characters: twelve of them need about 11.6 em, more than a phone's
   *  box holds at the full size. */
  function fitPassword(v) {
    v.style.fontSize = "";
    const room = v.clientWidth;
    const text = document.createRange();
    text.selectNodeContents(v);
    for (let i = 0; i < 3; i++) { // scaling is near enough linear; a second pass covers rounding
      const need = text.getBoundingClientRect().width;
      if (!room || need <= room) return;
      const size = parseFloat(getComputedStyle(v).fontSize);
      v.style.fontSize = `${Math.floor(((size * room) / need) * 4) / 4 - (i ? 0.5 : 0)}px`;
    }
  }

  /** The clipboard API where the page may use it (https, localhost); otherwise the selection and execCommand,
   *  which also works over plain http on a home network. Leaves the text selected when both fail. */
  async function copyText(text, node) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch { /* fall back to the selection */ }
    try {
      const range = document.createRange();
      range.selectNodeContents(node);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      return document.execCommand("copy");
    } catch {
      return false;
    }
  }

  async function unlockMember(call, btn) {
    st.admin.busy = call;
    setBusy(btn, "Unlocking");
    let reload = true;
    try {
      await HD.api(`/api/admin/users/${encodeURIComponent(call)}/unlock`, { method: "POST" });
      HD.toast(`${call} can log in again.`, { kind: "ok" });
    } catch (e) {
      setBusy(btn, "");
      reload = adminFailed(e, call);
    } finally {
      st.admin.busy = "";
    }
    if (reload) await loadMembers({ fallback: focusMemberOr(call) });
  }

  /** Delete a member's account: the dialog asks for their callsign, typed out, before it does. */
  function deleteMember(call) {
    let madeAdmin = false; // the server refused: call is in ADMIN_CALLSIGNS since the list was drawn
    const d = HD.dialog.open({
      labelledBy: "dm-title",
      // then their line gets its Admin tag and loses Delete account once the dialog closes
      onClose: () => { if (madeAdmin) loadMembers({ fallback: focusMemberOr(call) }); },
      html: `<form class="hd-form" novalidate>
        <h2 class="hd-dialog__title" id="dm-title">Delete ${esc(call)}'s account?</h2>
        <p class="hd-dialog__body" style="margin-top:0">Their posts and replies are removed too, and they're logged out. This can't be undone. Their FCC licence stays public, because it is.</p>
        <div data-dm-alert></div>
        <div class="hd-field"><label class="hd-field__label" for="dm-call">Type ${esc(call)} to confirm</label><input class="hd-input" id="dm-call" maxlength="16" autocomplete="off" autocapitalize="characters" spellcheck="false"></div>
        <div class="hd-dialog__actions" style="margin-top:4px"><button class="hd-btn hd-btn--quiet" type="button" data-cancel>Cancel</button><button class="hd-btn hd-btn--danger" type="submit">Delete account</button></div>
      </form>`,
    });
    const input = d.querySelector("#dm-call");
    d.querySelector("[data-cancel]").addEventListener("click", () => d.close("cancel"));
    input.focus();
    let busy = false;
    d.querySelector("form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return;
      const box = d.querySelector("[data-dm-alert]");
      box.innerHTML = "";
      setErr(input, "");
      const typed = input.value.trim().toUpperCase();
      if (typed !== call) {
        setErr(input, typed ? `That doesn't match. Type ${esc(call)} to confirm.` : `Type ${esc(call)} to confirm.`);
        input.focus();
        return;
      }
      busy = true;
      try {
        await HD.api(`/api/admin/users/${encodeURIComponent(call)}`, { method: "DELETE" });
        d.close("ok");
        HD.toast(`Deleted ${call}'s account.`, { kind: "ok" });
      } catch (err) {
        busy = false;
        if (err.status !== 401 && err.status !== 403 && err.status !== 404) {
          madeAdmin = madeAdmin || adminKept(err);
          box.innerHTML = HD.ui.alert("Couldn't delete the account", esc(adminErrorText(err, call)));
          return;
        }
        d.close("failed");
        if (!adminFailed(err, call)) return;
      }
      loadMembers({ fallback: () => HD.focusTo(el.querySelector("[data-admin-count]") || el.querySelector("#ad-q")) });
    });
  }

  function onAdminAction(btn) {
    const li = btn.closest("[data-member]");
    if (!li || st.admin.busy) return;
    const call = li.dataset.member, act = btn.dataset.adminAct;
    if (act === "reset") resetPassword(call, btn);
    else if (act === "unlock") unlockMember(call, btn);
    else if (act === "delete") deleteMember(call);
  }

  // ---------------------------------------------------------------- focus targets (#/you?focus=range)

  function applyFocus(params) {
    const f = params && params.focus;
    if (!f) return false;
    const target = f === "range" ? el.querySelector("#you-range") : f === "privacy" ? el.querySelector("#you-privacy") : f === "grid" ? el.querySelector("#pf-grid")
      : f === "admin" ? el.querySelector("#you-admin") : null;
    if (!target) return false;
    requestAnimationFrame(() => {
      const bar = el.querySelector("[data-topbar]");
      const barH = bar ? bar.getBoundingClientRect().bottom : 0;
      const y = target.getBoundingClientRect().top + window.scrollY - barH - 16;
      window.scrollTo(0, Math.max(0, y));
      HD.chrome.listReset();
      if (f === "admin") { HD.focusTo(el.querySelector("#h-admin")); return; }
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
        else if (e.target.id === "ad-q") { st.admin.q = e.target.value; searchSoon(); }
        else if (e.target.getAttribute("aria-invalid") === "true") setErr(e.target, "");
      });
      el.addEventListener("change", (e) => {
        if (e.target.matches("[data-range]")) onRangeChange(e.target);
        else if (e.target.name === "you-precision") onPrecision(e.target);
      });
      el.addEventListener("keydown", (e) => { if (e.target.matches && e.target.matches("[data-range]")) onRangeKey(e); });
      el.addEventListener("submit", (e) => {
        if (e.target.matches("[data-profile]")) { e.preventDefault(); saveProfile(e.target); }
        else if (e.target.matches("[data-admin-search]")) { e.preventDefault(); loadMembers({ announce: true }); }
      });
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
        if (t.closest("[data-admin-act]")) { onAdminAction(t.closest("[data-admin-act]")); return; }
        // Try again stays until the answer: then focus moves to the count line (or the new Try again)
        if (t.closest("[data-admin-retry]")) { loadMembers({ fallback: () => HD.focusTo(el.querySelector("[data-admin-count]")) }); return; }
        if (t.closest("[data-logout]")) {
          // no confirmation: clears the session and goes to Log in
          HD.session.clear();
          HD.router.go("auth", { mode: "login" }, { replace: true });
        }
      });
      HD.on("user", ({ user }) => {
        if (user) return;
        st.licence = undefined;
        // the next person to log in on this device never sees this admin's member list
        clearTimeout(st.admin.timer);
        if (st.admin.ctl) st.admin.ctl.abort();
        st.admin = freshAdmin();
      });
      // after an admin reset: ask for a new password once per boot and once per log in, over whichever view opens
      HD.on("view", promptMustChange);
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
          if (u.is_admin && st.visible && !st.admin.data) loadMembers(); // made an admin since the page was drawn
        }
      }).catch(() => {});
      // the member list (drawn from the last answer, if any, then refreshed)
      if (me().is_admin) loadMembers();
    },
    hide() {
      st.scroll = window.scrollY;
      st.visible = false;
    },
  });
})();
