/* hamdoor icons — one 24 px grid, 2 px round strokes (DESIGN_SYSTEM §4, components.css C.0).
   HD.icon(name, {size = 20, label, cls}) returns an inline SVG string ("" for an unknown name).
   Without a label the icon is decorative (aria-hidden); with one it is role="img" with that name.
   Sizes 16, 20, 24 and 32 map to the .hd-icon size classes; any other size is set inline.
   HD.icon.logo(size) is the hamdoor door mark (a door with a red knob: a person at the door). */
(function () {
  "use strict";
  const HD = (window.HD = window.HD || {});

  const P = {
    // navigation
    map: '<path d="M9 4 3.5 6v14L9 18l6 2 5.5-2V4L15 6 9 4Z"/><path d="M9 4v14M15 6v14"/>',
    feed: '<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M8 8.5h8M8 12h8M8 15.5h5"/>',
    nearby: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19.5c.6-3.3 2.8-5 5.5-5s4.9 1.7 5.5 5"/><circle cx="17" cy="9.5" r="2.4"/><path d="M15.6 14.3c2.5-.4 4.4 1.2 5 4.2"/>',
    activity: '<path d="M12 11.5 8.8 21M12 11.5l3.2 9.5M9.9 17.8h4.2"/><circle cx="12" cy="9.5" r="1.6"/><path d="M8.3 6a5.4 5.4 0 0 0 0 7M15.7 6a5.4 5.4 0 0 1 0 7M5.4 3.5a9 9 0 0 0 0 12M18.6 3.5a9 9 0 0 1 0 12"/>',
    // map controls (DESIGN_SYSTEM §15.1)
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/>',
    layers: '<path d="m12 3.5 9 4.8-9 4.8-9-4.8 9-4.8Z"/><path d="m3 12.2 9 4.8 9-4.8M3 16l9 4.8 9-4.8"/>',
    locate: '<circle cx="12" cy="12" r="6.5"/><circle cx="12" cy="12" r="2.2"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    minus: '<path d="M5 12h14"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
    follow: '<path d="m12 3 7 17-7-4-7 4 7-17Z"/>',
    center: '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v5M12 16.5v5M2.5 12h5M16.5 12h5"/>',
    copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
    up: '<path d="m6 15 6-6 6 6"/>',
    left: '<path d="m15 6-6 6 6 6"/>',
    right: '<path d="m9 6 6 6-6 6"/>',
    pin: '<path d="M12 21s-6.5-6.2-6.5-11a6.5 6.5 0 0 1 13 0c0 4.8-6.5 11-6.5 11Z"/><circle cx="12" cy="10" r="2.3"/>',
    external: '<path d="M13.5 4.5h6v6M19.5 4.5 11 13"/><path d="M17.5 13.5v5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1h5"/>',
    refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
    list: '<path d="M9 6.5h11M9 12h11M9 17.5h11"/><circle cx="4.8" cy="6.5" r="1.1" fill="currentColor"/><circle cx="4.8" cy="12" r="1.1" fill="currentColor"/><circle cx="4.8" cy="17.5" r="1.1" fill="currentColor"/>',
    // posts and categories
    pencil: '<path d="M15.5 4.5 19.5 8.5 9 19H5v-4L15.5 4.5Z"/><path d="m13 7 4 4"/>',
    reply: '<path d="M4.5 5.5h15v10h-8l-4.5 3.5v-3.5H4.5v-10Z"/>',
    send: '<path d="M20.5 3.5 10 14"/><path d="m20.5 3.5-6.5 17-4-6.5-6.5-4 17-6.5Z"/>',
    calendar: '<rect x="4" y="5.5" width="16" height="14.5" rx="2"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    tag: '<path d="M3.5 12.2V4.5a1 1 0 0 1 1-1h7.7l8.3 8.3-8.7 8.7-8.3-8.3Z"/><circle cx="8.3" cy="8.3" r="1.4"/>',
    help: '<path d="M8 20v-2.5l-3-3V9.8a1.4 1.4 0 0 1 2.8 0v3.4"/><path d="M7.8 13.2V5.4a1.4 1.4 0 0 1 2.8 0v6.8M10.6 11.5V4.4a1.4 1.4 0 0 1 2.8 0v7.1M13.4 11.6V5.6a1.4 1.4 0 0 1 2.8 0v8.1c0 2.8-1.6 4.8-3.6 6.3"/>',
    elmer: '<path d="m12 4.5 9.5 4.5-9.5 4.5L2.5 9 12 4.5Z"/><path d="M6.5 11v4.2c1.4 1.6 3.3 2.3 5.5 2.3s4.1-.7 5.5-2.3V11M21.5 9v5.5"/>',
    general: '<path d="M4 10v4h3l7 4.5V5.5L7 10H4Z"/><path d="M17.5 9a4 4 0 0 1 0 6"/>',
    trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
    // states and misc
    check: '<circle cx="12" cy="12" r="8.5"/><path d="m8.2 12.2 2.6 2.6 5-5.3"/>',
    tick: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    alert: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v5.5"/><circle cx="12" cy="16.3" r=".6" fill="currentColor"/>',
    info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><circle cx="12" cy="7.8" r=".6" fill="currentColor"/>',
    lock: '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3"/>',
    signal: '<path d="M7 16.5a7 7 0 0 1 0-9M17 7.5a7 7 0 0 1 0 9M10 13.5a2.8 2.8 0 0 1 0-3M14 10.5a2.8 2.8 0 0 1 0 3"/>',
    tree: '<path d="M12 3 6.5 10h3L6 15h12l-3.5-5h3L12 3Z" fill="currentColor" stroke="none"/><path d="M12 15v5.5" stroke-width="2.6"/>',
    summit: '<path d="M12 4 21 19.5H3L12 4Z"/>',
    star: '<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9L12 3.5Z"/>',
    user: '<circle cx="12" cy="8" r="3.6"/><path d="M5 20c.8-4 3.6-6 7-6s6.2 2 7 6"/>',
    logout: '<path d="M14.5 4.5h-8a1.5 1.5 0 0 0-1.5 1.5v12a1.5 1.5 0 0 0 1.5 1.5h8"/><path d="M11 12h9.5M17 8.5l3.5 3.5-3.5 3.5"/>',
    grid: '<path d="M4 9h16M4 15h16M9 4v16M15 4v16"/><rect x="4" y="4" width="16" height="16" rx="2"/>',
    eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    moon: '<path d="M19.5 14.5A7.5 7.5 0 1 1 9.5 4.5a6 6 0 0 0 10 10Z"/>',
  };
  // other names the spec and the map use for the same glyphs
  const ALIAS = {
    chevron: "down", "chevron-down": "down", "chevron-up": "up", "chevron-left": "left", "chevron-right": "right",
    back: "left", forward: "right", x: "close", edit: "pencil", delete: "trash", comment: "reply", comments: "reply",
    "zoom-in": "plus", "zoom-out": "minus", target: "locate", location: "pin", "external-link": "external",
    time: "clock", event: "calendar", "nets-events": "calendar", nets: "calendar", "for-sale": "tag", sale: "tag",
    "help-wanted": "help", mentoring: "elmer", megaphone: "general", ok: "check", error: "alert", warning: "alert",
    park: "tree", pota: "tree", sota: "summit", triangle: "summit", new: "star", you: "user", repeater: "activity",
    tower: "activity", radio: "signal", gridsquare: "grid", password: "lock",
  };
  const SIZE_CLASS = { 16: " hd-icon--16", 20: " hd-icon--20", 24: "", 32: " hd-icon--32" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function icon(name, opts) {
    const o = opts || {};
    const key = P[name] ? name : ALIAS[name];
    const body = key && P[key];
    if (!body) return "";
    const size = Number(o.size) || 20;
    const sizeCls = SIZE_CLASS[size] !== undefined ? SIZE_CLASS[size] : "";
    const style = SIZE_CLASS[size] !== undefined ? "" : ` style="width:${size}px;height:${size}px"`;
    const a11y = o.label ? ` role="img" aria-label="${esc(o.label)}"` : ' aria-hidden="true" focusable="false"';
    return `<svg class="hd-icon${sizeCls}${o.cls ? " " + esc(o.cls) : ""}" viewBox="0 0 24 24" width="${size}" height="${size}"${style}${a11y}>${body}</svg>`;
  }

  /** The hamdoor door mark: a door with a red knob. */
  icon.logo = function (size = 34, label) {
    const a11y = label ? ` role="img" aria-label="${esc(label)}"` : ' aria-hidden="true" focusable="false"';
    return `<svg class="hd-logo" width="${size}" height="${size}" viewBox="0 0 34 34"${a11y}><path d="M9 30V11a8 8 0 0 1 16 0v19" fill="none" stroke="currentColor" stroke-width="2.6"/><path d="M5 30h24" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/><circle cx="21" cy="20" r="2.4" fill="var(--hd-member)"/></svg>`;
  };
  /** Every name HD.icon knows (aliases included). */
  icon.names = () => Object.keys(P).concat(Object.keys(ALIAS));
  icon.has = (name) => !!(P[name] || P[ALIAS[name]]);

  HD.icon = icon;
})();
