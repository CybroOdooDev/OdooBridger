/* The app shell shared by the portal and the staff console: sidebar, top bar,
 * theme switch, a sign-in card, a dialog helper and a tiny hash router. */

import { $, esc } from './ui.js';

const ICONS = {
  grid: '<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/>',
  card: '<rect x="3" y="5.5" width="18" height="13" rx="2"/><path d="M3 10h18M7 15h4"/>',
  people: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c.8-3.2 3-5 5.5-5s4.7 1.8 5.5 5"/><circle cx="17" cy="9" r="2.4"/><path d="M16 14c2.4 0 4 1.5 4.6 4"/>',
  // A person in a circle: the account.
  account: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="10" r="3"/><path d="M6.2 18.4a6.5 6.5 0 0 1 11.6 0"/>',
  settings: '<path d="M10.3 3.2h3.4l.5 2.4 1.9.8 2-1.4 2.4 2.4-1.4 2 .8 1.9 2.4.5v3.4l-2.4.5-.8 1.9 1.4 2-2.4 2.4-2-1.4-1.9.8-.5 2.4h-3.4l-.5-2.4-1.9-.8-2 1.4-2.4-2.4 1.4-2-.8-1.9-2.4-.5v-3.4l2.4-.5.8-1.9-1.4-2 2.4-2.4 2 1.4 1.9-.8z"/><circle cx="12" cy="12" r="3.2"/>',
  overview: '<path d="M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-4H4zM14 4v4h6V4z"/>',
  accounts: '<rect x="3" y="4" width="18" height="6" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7h.01M7 17h.01"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 6.5l8.5 6.5 8.5-6.5"/>',
  tag: '<path d="M3.5 12.2V4.5a1 1 0 0 1 1-1h7.7l8.3 8.3a1.4 1.4 0 0 1 0 2l-6.7 6.7a1.4 1.4 0 0 1-2 0z"/><circle cx="8" cy="8" r="1.5"/>',
  odoo: '<ellipse cx="12" cy="6" rx="7.5" ry="2.8"/><path d="M4.5 6v12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V6M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
  closed: '<rect x="3" y="4" width="18" height="5" rx="1.2"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4"/>',
};
export const icon = (name) => `<svg class="nav-icon" viewBox="0 0 24 24" width="17" height="17" fill="none"
  stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;

export const BRAND = `<div class="brand"><span class="brand-mark">O</span>
  <span class="brand-word"><b>Odoo</b><span>Bridger</span></span></div>`;

/* ---- theme ------------------------------------------------------------- */
function currentTheme() { return document.documentElement.getAttribute('data-theme') || 'system'; }
function setTheme(value) {
  if (value === 'light' || value === 'dark') document.documentElement.setAttribute('data-theme', value);
  else document.documentElement.removeAttribute('data-theme');
  try {
    if (value === 'light' || value === 'dark') localStorage.setItem('ob.theme', value);
    else localStorage.removeItem('ob.theme');
  } catch { /* lasts until the tab closes */ }
  paintTheme();
}
/** Dark now: chosen explicitly, or the device's own setting when left on auto. */
function isDark() {
  const t = currentTheme();
  if (t === 'dark') return true;
  if (t === 'light') return false;
  return !!window.matchMedia?.('(prefers-color-scheme: dark)').matches;
}
const SUN = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4L7 17M17 7l1.4-1.4"/></svg>';
const MOON = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>';
function paintTheme() {
  const holder = $('#themeSwitch');
  if (!holder) return;
  const dark = isDark();
  holder.innerHTML = `<button type="button" class="switch" role="switch" aria-checked="${dark}" aria-label="Dark mode" data-theme-flip>
      <span class="knob">${dark ? MOON : SUN}</span></button>`;
}

/* ---- sign-in card -------------------------------------------------------- */
export function authCard(inner, { badge = '', foot = '' } = {}) {
  $('#app-root').classList.add('hidden');
  const root = $('#auth-root');
  root.classList.remove('hidden');
  root.innerHTML = `
    <div class="auth-wrap">
      <div class="auth-card${badge ? ' staff' : ''}">
        ${BRAND.replace('</div>', `${badge ? `<span class="pill warn">${esc(badge)}</span>` : ''}</div>`)}
        ${inner}
        <p class="err" id="authError"></p>
      </div>
      ${foot ? `<p class="auth-foot">${foot}</p>` : ''}
    </div>`;
  return root;
}

export async function submitting(button, fn) {
  const error = $('#authError');
  if (error) error.textContent = '';
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Working…';
  try {
    await fn();
  } catch (e) {
    if (error) error.textContent = e.message || 'Something went wrong';
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

/* ---- the shell --------------------------------------------------------- */
export function mountShell({ nav, user, pill = '', onSignOut }) {
  $('#auth-root').classList.add('hidden');
  const root = $('#app-root');
  root.classList.remove('hidden');
  if (!root.dataset.built) {
    root.dataset.built = '1';
    root.innerHTML = `
      <div class="shell">
        <aside class="sidebar" id="sidebar">
          ${BRAND}
          <nav id="sidenav"></nav>
          <div class="side-foot">
            <div class="side-user" id="sideUser"></div>
            <div class="theme-toggle" id="themeSwitch"></div>
            <button class="link" id="signOut" style="padding-left:0">Sign Out</button>
          </div>
        </aside>
        <div class="main">
          <div class="topbar">
            <button id="menuToggle" class="sm">Menu</button>
            <div class="title-block"><h1 id="pageTitle"></h1></div>
            <div class="spacer"></div>
            <div class="top-actions" id="topActions"></div>
            <span id="topPill"></span>
          </div>
          <div class="content" id="content"></div>
        </div>
      </div>`;
    $('#signOut', root).addEventListener('click', onSignOut);
    $('#menuToggle', root).addEventListener('click', () => $('#sidebar', root).classList.toggle('open'));
    $('#themeSwitch', root).addEventListener('click', (e) => {
      if (e.target.closest('[data-theme-flip]')) setTheme(isDark() ? 'light' : 'dark');
    });
    paintTheme();
  }
  const path = currentPath();
  $('#sidenav').innerHTML = nav.map((group) => `
    ${group.label ? `<div class="nav-group"><div class="nav-group-label">${esc(group.label)}</div></div>` : ''}
    ${group.items.map((item) => {
      const active = (item.paths || [item.path]).some((p) => p === path || (p !== '/' && path.startsWith(`${p}/`)));
      const href = item.href || `#${item.path}`;
      return `<a class="nav ${active ? 'active' : ''}" href="${esc(href)}"${active ? ' aria-current="page"' : ''}>
        <span class="nav-label">${item.icon ? icon(item.icon) : ''}${esc(item.title)}</span>
        ${item.badge ? `<span class="nav-badge">${esc(item.badge)}</span>` : ''}</a>`;
    }).join('')}`).join('');
  $('#sideUser').textContent = user || '';
  $('#topPill').innerHTML = pill;
  $('#sidebar').classList.remove('open');
  return root;
}

export function unmountShell() {
  const root = $('#app-root');
  root.classList.add('hidden');
  root.innerHTML = '';
  delete root.dataset.built;
}

/* ---- routing ------------------------------------------------------------- */
export function parseHash() {
  const raw = window.location.hash.replace(/^#/, '') || '/';
  const [path, qs] = raw.split('?');
  return { path: path.length > 1 ? path.replace(/\/+$/, '') : path || '/', query: Object.fromEntries(new URLSearchParams(qs || '')) };
}
export const currentPath = () => parseHash().path;

/** Match "/accounts/:id" against a path; returns params or null. */
export function match(pattern, path) {
  const a = pattern.split('/');
  const b = path.split('/');
  if (a.length !== b.length) return null;
  const params = {};
  for (let i = 0; i < a.length; i += 1) {
    if (a[i].startsWith(':')) params[a[i].slice(1)] = decodeURIComponent(b[i]);
    else if (a[i] !== b[i]) return null;
  }
  return params;
}

/* ---- dialogs ------------------------------------------------------------- */
/** A modal form. ``body`` is HTML; resolves with the dialog element on submit
 * (the caller reads it), or null on cancel. ``onSubmit`` may throw to keep it open.
 * ``secondaryLabel`` adds a second submit button; ``onSubmit(form, action)`` is
 * told which was pressed ("primary" or "secondary"). ``onOpen(form)`` wires
 * anything interactive in the body. */
export function dialog({ title, body, submitLabel = 'Save', secondaryLabel = null, danger = false, wide = false, onSubmit, onOpen }) {
  return new Promise((resolve) => {
    const host = document.createElement('div');
    host.innerHTML = `
      <div class="wiz-backdrop" data-close></div>
      <div class="wiz${wide ? '' : ' narrow'}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <header><div><h3>${esc(title)}</h3></div><button class="link wiz-x" type="button" data-close aria-label="Close">&times;</button></header>
        <form class="wiz-body" novalidate>
          ${body}
          <p class="err" data-err></p>
          <div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px">
            <button type="button" data-close>Cancel</button>
            ${secondaryLabel ? `<button type="submit" data-action="secondary">${esc(secondaryLabel)}</button>` : ''}
            <button type="submit" data-action="primary" class="${danger ? 'danger' : 'primary'}">${esc(submitLabel)}</button>
          </div>
        </form>
      </div>`;
    document.body.append(host);
    const close = (value) => { host.remove(); document.removeEventListener('keydown', onKey); resolve(value); };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', onKey);
    host.querySelectorAll('[data-close]').forEach((el) => el.addEventListener('click', () => close(null)));
    const form = host.querySelector('form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const buttons = [...form.querySelectorAll('[type=submit]')];
      const action = e.submitter?.dataset.action || 'primary';
      const err = form.querySelector('[data-err]');
      err.textContent = '';
      buttons.forEach((b) => { b.disabled = true; });
      try {
        const result = onSubmit ? await onSubmit(form, action) : form;
        close(result ?? form);
      } catch (error) {
        err.textContent = error.message || 'Something went wrong';
        buttons.forEach((b) => { b.disabled = false; });
      }
    });
    if (onOpen) onOpen(form);
    form.querySelector('input, select, textarea')?.focus();
  });
}
