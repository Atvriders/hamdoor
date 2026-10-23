/* Sign up and Log in (DESIGN_SYSTEM §7.14, copy §13). Route #/auth?mode=signup|login (&expired=1).
   Both sit outside the signed-in shell: one readable column, the logo top left and
   the other screen's link top right. */
(function () {
  "use strict";
  const HD = window.HD;
  const esc = HD.esc;
  const icon = (n, o) => HD.icon(n, o);

  const MSG = {
    notFound: (c) => `We couldn't find ${esc(c)} in FCC records. Check the letters and zeros. A licence granted in the last few days can take up to a week to reach FCC records.`,
    noCall: "Enter your callsign, like W1AW.",
    badCall: "Enter a callsign like W1AW.",
    email: "Enter an email address like you@example.com.",
    password: "Use at least 8 characters.",
    noAccount: (c) => `There's no hamdoor account for ${esc(c)} on this server. Check the letters and zeros, or sign up.`,
    wrongPw: "That password doesn't match. Check it and try again.",
    busy: "That's busy right now. Try again in a minute.",
    noPw: "Enter your password.",
  };
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  const CALL_RE = /^[A-Z0-9]{3,8}$/;

  let el = null;
  // sign-up steps: "start" (callsign), "found" (the QSL preview), "manual" (FCC records unreachable)
  const su = { step: "start", call: "", lookup: null, error: "", taken: false, net: false, busy: false, precision: "address" };
  const li = { busy: false };

  const brand = (otherHref, otherLabel) =>
    `<div class="hd-auth__brand"><a class="hd-auth__logo" href="#/auth?mode=signup">${HD.icon.logo(34)}hamdoor</a>` +
    `<a class="hd-link hd-link--solo" href="${otherHref}">${otherLabel}</a></div>`;

  function fieldError(id, text) {
    return text ? `<p class="hd-field__error" id="${id}">${icon("alert", { size: 20 })}<span>${text}</span></p>` : "";
  }

  const privacyRadios = (name, locator, checked) => HD.ui.privacyRadios(name, locator, checked);

  const privacySummary = (precision, locator) => precision === "grid"
    ? `Your pin goes at ${locator ? "the centre of grid " + esc(locator) : "the centre of your grid square"}. You can choose your licence address instead.`
    : "Your pin goes at your licence address. You can choose your grid square instead.";

  function privacyDisclosure(locator) {
    return `<details class="hd-disclosure" data-privacy><summary><span class="hd-disclosure__sum">Map privacy<small data-privacy-sum>${privacySummary(su.precision, locator)}</small></span>${icon("down", { size: 24 })}</summary>
      <div class="hd-disclosure__body">${privacyRadios("su-precision", locator, su.precision)}</div></details>`;
  }

  // ---------------------------------------------------------------- sign up

  function lookupLocator() {
    const g = su.lookup && su.lookup.grid;
    return HD.geo.shownLocator(g, null, null);
  }

  function renderSignup(focusSel) {
    let body = "";
    if (su.step === "start") {
      body = `<form class="hd-auth__form" data-lookup-form novalidate>
        <div class="hd-field"><label class="hd-field__label" for="su-call">Your callsign</label>
          <div class="hd-inputrow"><input class="hd-input hd-input--callsign" id="su-call" name="callsign" value="${esc(su.call)}" autocapitalize="characters" autocomplete="off" spellcheck="false" maxlength="10" aria-describedby="su-call-h${su.error ? " su-call-err" : ""}"${su.error ? ' aria-invalid="true"' : ""}>
          <button class="hd-btn hd-btn--primary" type="submit"${su.busy ? ' aria-busy="true"' : ""}>${su.busy ? "Looking up" : "Look up"}</button></div>
          <p class="hd-field__hint" id="su-call-h">For example, W1AW</p>${fieldError("su-call-err", su.error)}</div>
      </form>`;
    } else if (su.step === "found") {
      const r = su.lookup;
      const cls = r.license_class || "";
      const place = esc(HD.ui.place(r.city, r.state));
      const cells = [["Grid", r.grid ? esc(HD.geo.normGrid(r.grid) || r.grid) : "Not listed"], ["Class", esc(HD.ui.classShort(cls))]];
      if (r.granted) cells.push(["Licensed", esc(HD.fmt.monthYear(r.granted) || "Not listed")], ["Expires", esc(HD.fmt.monthYear(r.expires) || "Not listed")]);
      else cells.push(["Expires", esc(HD.fmt.monthYear(r.expires) || "Not listed"), true]);
      body = `<p class="hd-auth__found">${icon("check", { size: 24 })}<span>Found <b class="hd-call">${esc(r.callsign)}</b> in FCC records</span><button class="hd-link hd-link--solo" type="button" data-change>Change</button></p>
        <article class="hd-qsl" aria-label="Your licence">
          <div class="hd-qsl__head"><span class="hd-call hd-call--hero hd-call--member" style="flex:1">${esc(r.callsign)}</span>${HD.ui.classBadge(cls, !!r.new)}</div>
          <p class="hd-qsl__name">${esc(r.name)}</p>${place ? `<p class="hd-qsl__place">${place}</p>` : ""}
          ${HD.ui.qso(cells)}
        </article>
        <p>Is this you? You can change your address after you sign up. <button class="hd-link hd-link--solo" type="button" data-change>Not me</button></p>
        ${su.taken ? takenAlert(r.callsign) : ""}${su.net === "busy" ? HD.ui.alert("That's busy right now", "Try again in a minute.") : su.net ? HD.ui.alert("Couldn't reach hamdoor", "Check your connection and try again.") : ""}
        <form class="hd-auth__form" data-signup-form novalidate>
          ${privacyDisclosure(lookupLocator())}
          ${emailField(r.email || "")}
          ${passwordField()}
          <button class="hd-btn hd-btn--primary hd-btn--lg hd-btn--block" type="submit"${su.busy ? ' aria-busy="true"' : ""}>${su.busy ? "Creating account" : "Create account"}</button>
        </form>`;
    } else {
      body = `${HD.ui.alert("Couldn't reach FCC records right now", "Enter your details yourself. We check your callsign against FCC records when you create your account.")}
        ${su.taken ? takenAlert(su.call) : ""}${su.net === "busy" ? HD.ui.alert("That's busy right now", "Try again in a minute.") : su.net ? HD.ui.alert("Couldn't reach hamdoor", "Check your connection and try again.") : ""}
        <form class="hd-auth__form" data-signup-form data-manual novalidate>
          <div class="hd-field"><label class="hd-field__label" for="su-call2">Your callsign</label>
            <input class="hd-input hd-input--callsign" id="su-call2" name="callsign" value="${esc(su.call)}" autocapitalize="characters" autocomplete="off" spellcheck="false" maxlength="10"${su.error ? ' aria-invalid="true" aria-describedby="su-call2-err"' : ""}>${fieldError("su-call2-err", su.error)}</div>
          ${plainField("su-name", "name", "Name", "name")}
          ${plainField("su-street", "address_line", "Street address", "street-address")}
          ${plainField("su-city", "city", "City", "address-level2")}
          <div class="hd-fieldrow">${plainField("su-state", "state", "State", "address-level1", ' maxlength="2" autocapitalize="characters"')}${plainField("su-zip", "zip", "ZIP", "postal-code", ' inputmode="numeric" maxlength="10"')}</div>
          <div class="hd-field"><label class="hd-field__label" for="su-grid">Grid square <span class="hd-muted" style="font-weight:500">(optional)</span></label>
            <input class="hd-input" id="su-grid" name="grid" maxlength="8" autocapitalize="characters" autocomplete="off" spellcheck="false" aria-describedby="su-grid-h"><p class="hd-field__hint" id="su-grid-h">Six characters, like EM28px.</p></div>
          ${privacyDisclosure("")}
          ${emailField("")}
          ${passwordField()}
          <button class="hd-btn hd-btn--primary hd-btn--lg hd-btn--block" type="submit"${su.busy ? ' aria-busy="true"' : ""}>${su.busy ? "Creating account" : "Create account"}</button>
        </form>`;
    }
    el.innerHTML = `<div class="hd-authpage"><div class="hd-auth">
      ${brand("#/auth?mode=login", "Log in")}
      <div><h1 class="hd-auth__title" id="auth-title" tabindex="-1">Sign up with your callsign</h1><p class="hd-auth__lede">We fill in your name and address from public FCC records.</p></div>
      ${body}
      <p class="hd-muted">Already on hamdoor? <a class="hd-link hd-link--solo" href="#/auth?mode=login">Log in</a></p>
    </div></div>`;
    if (focusSel) { const f = el.querySelector(focusSel); if (f) f.focus(); }
  }

  const takenAlert = (call) => HD.ui.alert(`${esc(call)} already has a hamdoor account here`,
    `<a class="hd-link" href="#/auth?mode=login&amp;call=${encodeURIComponent(call)}">Log in instead</a>, or ask the person who runs this server for help.`);
  const plainField = (id, name, label, auto, extra = "") =>
    `<div class="hd-field"><label class="hd-field__label" for="${id}">${label}</label><input class="hd-input" id="${id}" name="${name}" autocomplete="${auto}"${extra}></div>`;
  const emailField = (value) =>
    `<div class="hd-field"><label class="hd-field__label" for="su-email">Email</label><input class="hd-input" id="su-email" name="email" type="email" autocomplete="email" value="${esc(value)}" aria-describedby="su-email-h"><p class="hd-field__hint" id="su-email-h">Only you see your email.</p></div>`;
  const passwordField = () =>
    `<div class="hd-field"><label class="hd-field__label" for="su-pw">Password</label><input class="hd-input" id="su-pw" name="password" type="password" autocomplete="new-password" aria-describedby="su-pw-h"><p class="hd-field__hint" id="su-pw-h">At least 8 characters</p></div>`;

  /** Mark one field invalid with its fix-it words (keeps the rest of the form as typed). */
  function setFieldError(input, text) {
    if (!input) return;
    const field = input.closest(".hd-field");
    const id = input.id + "-err";
    let p = field.querySelector("#" + CSS.escape(id));
    if (!text) {
      if (p) p.remove();
      input.removeAttribute("aria-invalid");
      input.setAttribute("aria-describedby", (input.getAttribute("aria-describedby") || "").replace(id, "").trim());
      if (!input.getAttribute("aria-describedby")) input.removeAttribute("aria-describedby");
      return;
    }
    if (!p) {
      p = document.createElement("p");
      p.className = "hd-field__error";
      p.id = id;
      field.appendChild(p);
    }
    p.innerHTML = `${icon("alert", { size: 20 })}<span>${text}</span>`;
    input.setAttribute("aria-invalid", "true");
    const desc = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter((x) => x && x !== id);
    desc.push(id);
    input.setAttribute("aria-describedby", desc.join(" "));
  }

  async function doLookup(form) {
    if (su.busy) return;
    const input = form.querySelector("#su-call");
    const call = (input.value || "").trim().toUpperCase();
    su.call = call;
    if (!call) { setFieldError(input, MSG.noCall); input.focus(); return; }
    if (!CALL_RE.test(call)) { su.error = MSG.notFound(call); renderSignup("#su-call"); return; }
    su.busy = true;
    su.error = "";
    renderSignup();
    try {
      const r = await HD.api("/api/lookup/" + encodeURIComponent(call), { auth: false, timeout: 10000 });
      su.busy = false;
      if (!r || !r.found) {
        su.error = MSG.notFound(call);
        renderSignup("#su-call");
        HD.announce("Not found in FCC records");
        return;
      }
      su.lookup = r;
      su.step = "found";
      su.taken = false;
      su.net = false;
      renderSignup();
      HD.announce(`Found ${r.callsign} in FCC records`);
      const f = el.querySelector(".hd-auth__found");
      if (f) { f.setAttribute("tabindex", "-1"); f.focus({ preventScroll: true }); }
    } catch (e) {
      su.busy = false;
      if (HD.isAbort(e)) return;
      // too many lookups from here (the server throttles them): wait, don't type everything in by hand
      if (e.status === 429) { su.error = MSG.busy; renderSignup("#su-call"); return; }
      // the lookup failed, timed out or answered 5xx: the manual path (never for found: false)
      su.step = "manual";
      su.error = "";
      renderSignup();
      HD.announce("Couldn't reach FCC records right now. Enter your details yourself.");
      const f = el.querySelector("#su-name");
      if (f) f.focus();
    }
  }

  async function doSignup(form) {
    if (su.busy) return;
    const v = (n) => { const i = form.querySelector(`[name="${n}"]`); return i ? i.value.trim() : ""; };
    const emailIn = form.querySelector("#su-email"), pwIn = form.querySelector("#su-pw");
    const manual = form.hasAttribute("data-manual");
    let bad = null;
    if (manual) {
      const ci = form.querySelector("#su-call2");
      const call = v("callsign").toUpperCase();
      su.call = call;
      setFieldError(ci, "");
      if (!call) { setFieldError(ci, MSG.noCall); bad = bad || ci; }
      else if (!CALL_RE.test(call)) { setFieldError(ci, MSG.badCall); bad = bad || ci; }
      const gi = form.querySelector("#su-grid");
      setFieldError(gi, "");
      if (v("grid") && !HD.geo.normGrid(v("grid"))) { setFieldError(gi, "Enter a grid square like EM28 or EM28px."); bad = bad || gi; }
    }
    setFieldError(emailIn, "");
    setFieldError(pwIn, "");
    if (!EMAIL_RE.test(v("email"))) { setFieldError(emailIn, MSG.email); bad = bad || emailIn; }
    if ((pwIn.value || "").length < 8) { setFieldError(pwIn, MSG.password); bad = bad || pwIn; }
    if (bad) { bad.focus(); return; }

    const r = su.lookup || {};
    const body = manual
      ? { callsign: su.call, name: v("name"), address_line: v("address_line"), city: v("city"), state: v("state").toUpperCase(), zip: v("zip"), grid: HD.geo.normGrid(v("grid")), email: v("email"), password: pwIn.value, map_precision: su.precision }
      : { callsign: r.callsign, name: r.name || "", address_line: r.address_line || "", city: r.city || "", state: r.state || "", zip: r.zip || "", grid: r.grid || "", email: v("email"), password: pwIn.value, map_precision: su.precision };
    const btn = form.querySelector('button[type="submit"]');
    su.busy = true;
    btn.textContent = "Creating account";
    btn.setAttribute("aria-busy", "true");
    try {
      const res = await HD.api("/api/auth/signup", { method: "POST", body, auth: false });
      su.busy = false;
      reset();
      HD.session.set(res.token, res.user);
      HD.router.go(HD.router.DEFAULT, {}, { replace: true });
    } catch (e) {
      su.busy = false;
      btn.textContent = "Create account";
      btn.removeAttribute("aria-busy");
      const keep = { email: v("email"), pw: pwIn.value, fields: manual ? Object.fromEntries(["name", "address_line", "city", "state", "zip", "grid"].map((n) => [n, v(n)])) : null };
      if (e.status === 409) {
        su.taken = true;
        su.net = false;
        renderSignup();
        restore(keep);
        const a = el.querySelector(".hd-alert");
        if (a) { a.setAttribute("tabindex", "-1"); a.focus(); }
      } else if (e.status === 400) {
        // not in FCC records: the step-1 error
        su.error = MSG.notFound(body.callsign);
        if (manual) { renderSignup("#su-call2"); restore(keep); }
        else { su.step = "start"; su.call = body.callsign; renderSignup("#su-call"); }
      } else if (e.status === 422) {
        const fields = HD.errorFields(e).join(" ");
        if (/email/.test(fields)) setFieldError(emailIn, MSG.email);
        if (/password/.test(fields)) setFieldError(pwIn, MSG.password);
        if (/callsign/.test(fields)) {
          const ci = form.querySelector("#su-call2");
          if (ci) setFieldError(ci, MSG.badCall); else { su.step = "start"; su.error = MSG.badCall; renderSignup("#su-call"); return; }
        }
        const first = form.querySelector('[aria-invalid="true"]');
        if (first) first.focus();
      } else {
        su.net = e.status === 429 ? "busy" : true; // 429: the server throttles sign-ups
        su.taken = false;
        renderSignup();
        restore(keep);
        const a = el.querySelectorAll(".hd-alert");
        const last = a[a.length - 1];
        if (last) { last.setAttribute("tabindex", "-1"); last.focus(); }
      }
    }
  }
  function restore(keep) {
    const set = (sel, val) => { const i = el.querySelector(sel); if (i && val != null) i.value = val; };
    set("#su-email", keep.email);
    set("#su-pw", keep.pw);
    if (keep.fields) for (const [n, val] of Object.entries(keep.fields)) set(`[name="${n}"]`, val);
  }
  function reset() {
    Object.assign(su, { step: "start", call: "", lookup: null, error: "", taken: false, net: false, busy: false, precision: "address" });
  }

  // ---------------------------------------------------------------- log in

  function renderLogin(params) {
    const expired = params && params.expired;
    el.innerHTML = `<div class="hd-authpage"><div class="hd-auth">
      ${brand("#/auth?mode=signup", "Sign up")}
      <div><h1 class="hd-auth__title" id="auth-title" tabindex="-1">Log in with your callsign</h1></div>
      ${expired ? `<p class="hd-auth__notice" role="status">${icon("info", { size: 24 })}<span>Your session ended. Log in again to carry on.</span></p>` : ""}
      <div data-login-alert></div>
      <form class="hd-auth__form" data-login-form novalidate>
        <div class="hd-field"><label class="hd-field__label" for="li-call">Your callsign</label>
          <input class="hd-input hd-input--callsign" id="li-call" name="callsign" value="${esc((params && params.call) || "")}" autocapitalize="characters" autocomplete="username" spellcheck="false" maxlength="10" aria-describedby="li-call-h">
          <p class="hd-field__hint" id="li-call-h">For example, W1AW</p></div>
        <div class="hd-field"><label class="hd-field__label" for="li-pw">Password</label>
          <input class="hd-input" id="li-pw" name="password" type="password" autocomplete="current-password"></div>
        <button class="hd-btn hd-btn--primary hd-btn--lg hd-btn--block" type="submit">Log in</button>
        <details class="hd-disclosure"><summary><span class="hd-disclosure__sum">Forgot your password?</span>${icon("down", { size: 24 })}</summary>
          <div class="hd-disclosure__body"><p>hamdoor can't email you a new password. Ask the person who runs this hamdoor server to set a new one for you, then change it in You.</p></div></details>
      </form>
      <p class="hd-muted">New to hamdoor? <a class="hd-link hd-link--solo" href="#/auth?mode=signup">Sign up with your callsign</a></p>
    </div></div>`;
    if (expired) HD.announce("Your session ended. Log in again to carry on.");
  }

  async function doLogin(form) {
    if (li.busy) return;
    const ci = form.querySelector("#li-call"), pi = form.querySelector("#li-pw");
    const call = (ci.value || "").trim().toUpperCase();
    const alertBox = el.querySelector("[data-login-alert]");
    alertBox.innerHTML = "";
    setFieldError(ci, "");
    setFieldError(pi, "");
    let bad = null;
    if (!call) { setFieldError(ci, MSG.noCall); bad = ci; }
    if (!pi.value) { setFieldError(pi, MSG.noPw); bad = bad || pi; }
    if (bad) { bad.focus(); return; }
    const btn = form.querySelector('button[type="submit"]');
    li.busy = true;
    btn.textContent = "Logging in";
    btn.setAttribute("aria-busy", "true");
    try {
      const res = await HD.api("/api/auth/login", { method: "POST", body: { callsign: call, password: pi.value }, auth: false });
      li.busy = false;
      HD.session.set(res.token, res.user);
      HD.router.go(HD.router.DEFAULT, {}, { replace: true });
    } catch (e) {
      li.busy = false;
      btn.textContent = "Log in";
      btn.removeAttribute("aria-busy");
      if (e.status === 404) { setFieldError(ci, MSG.noAccount(call)); ci.focus(); }
      else if (e.status === 401) { setFieldError(pi, MSG.wrongPw); pi.focus(); pi.select(); }
      else if (e.status === 422) { setFieldError(ci, MSG.badCall); ci.focus(); }
      else if (e.status === 429) { alertBox.innerHTML = HD.ui.alert("That's busy right now", "Try again in a minute."); }
      else {
        alertBox.innerHTML = HD.ui.alert("Couldn't reach hamdoor", "Check your connection and try again.");
        const a = alertBox.querySelector(".hd-alert");
        if (a) { a.setAttribute("tabindex", "-1"); a.focus(); }
      }
    }
  }

  // ---------------------------------------------------------------- view

  HD.router.register("auth", {
    mount(section) {
      el = section;
      el.addEventListener("submit", (e) => {
        const f = e.target;
        e.preventDefault();
        if (f.matches("[data-lookup-form]")) doLookup(f);
        else if (f.matches("[data-signup-form]")) doSignup(f);
        else if (f.matches("[data-login-form]")) doLogin(f);
      });
      el.addEventListener("click", (e) => {
        if (e.target.closest("[data-change]")) {
          su.step = "start";
          su.error = "";
          su.taken = false;
          su.net = false;
          su.call = su.lookup ? su.lookup.callsign : su.call;
          su.lookup = null;
          renderSignup("#su-call");
        }
      });
      el.addEventListener("change", (e) => {
        if (e.target.name === "su-precision") {
          su.precision = e.target.value === "grid" ? "grid" : "address";
          const sum = el.querySelector("[data-privacy-sum]");
          if (sum) sum.innerHTML = privacySummary(su.precision, su.step === "found" ? lookupLocator() : "");
        }
      });
      el.addEventListener("input", (e) => {
        // fix-it words go away as soon as the field is edited
        if (e.target.getAttribute("aria-invalid") === "true") setFieldError(e.target, "");
        if (e.target.id === "su-call") su.error = "";
      });
    },
    show(params) {
      if (HD.session.token) { HD.router.go(HD.router.DEFAULT, {}, { replace: true }); return; }
      window.scrollTo(0, 0);
      if (params.mode === "login") renderLogin(params);
      else {
        if (params.call && su.step === "start" && !su.call) su.call = String(params.call).toUpperCase();
        renderSignup();
      }
      const h = el.querySelector("#auth-title");
      if (h && document.activeElement && document.activeElement !== document.body && !el.contains(document.activeElement)) h.focus({ preventScroll: true });
    },
    hide() {},
  });
})();
