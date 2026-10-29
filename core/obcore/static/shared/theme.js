/* Applies the theme someone picked before the stylesheet paints anything.
 *
 * A classic (non-module) script loaded from <head> on purpose: module scripts
 * run after the first paint, which would flash the computer's theme and then
 * switch. No stored choice means "follow the computer" — nothing is set.
 * Stored per browser only (localStorage); it is a viewing preference, not an
 * account setting. See the theme toggle in app.js. */
(function () {
  try {
    var theme = localStorage.getItem('ob.theme');
    if (theme === 'light' || theme === 'dark') {
      document.documentElement.setAttribute('data-theme', theme);
    }
  } catch (e) { /* storage blocked: follow the computer */ }
})();

/* The website's own light/dark switch (the apps draw theirs in shell.js):
 * any [data-site-theme] placeholder becomes the same switch. */
(function () {
  var SUN = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4L7 17M17 7l1.4-1.4"/></svg>';
  var MOON = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>';
  function isDark() {
    var t = document.documentElement.getAttribute('data-theme');
    if (t === 'dark') return true;
    if (t === 'light') return false;
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function paint() {
    var dark = isDark();
    var holders = document.querySelectorAll('[data-site-theme]');
    for (var i = 0; i < holders.length; i++) {
      holders[i].innerHTML = '<button type="button" class="switch" role="switch" aria-checked="' + dark
        + '" aria-label="Dark mode" title="' + (dark ? 'Switch to light mode' : 'Switch to dark mode') + '">'
        + '<span class="knob">' + (dark ? MOON : SUN) + '</span></button>';
    }
  }
  function flip() {
    var next = isDark() ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('ob.theme', next); } catch (e) { /* this tab only */ }
    paint();
  }
  document.addEventListener('DOMContentLoaded', function () {
    if (!document.querySelector('[data-site-theme]')) return;
    paint();
    document.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('[data-site-theme] .switch')) flip();
    });
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      if (mq.addEventListener) mq.addEventListener('change', paint);
    }
  });
})();
