/* The bell: which of the customer's services have alerts.
 *
 * One line per service with something to report, "You have alerts in your
 * BioBridge" with the count as a badge on the right (see services/notifications.py). The alerts themselves are shown by
 * the service, so the line just opens it. Live only: nothing is stored or
 * dismissed here, and a line disappears when its service stops reporting. */

import { $, esc } from '/assets/ui.js';

const BELL = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 8 3 8H3s3-1 3-8M10.3 21a1.9 1.9 0 0 0 3.4 0"/></svg>';

const state = { count: 0, worst: null, items: [] };
let timer = null;
let hooks = null;

function bellHtml() {
  const n = state.count;
  const label = n ? `${n} alert${n === 1 ? '' : 's'}` : 'No Alerts';
  return `<button class="sm bell ${n ? state.worst : ''}" id="bell" aria-haspopup="true"
      aria-label="${esc(label)}" title="${esc(label)}">${BELL}${n ? '<span class="bell-count"></span>' : ''}</button>`;
}

function closePanel() {
  $('#bellPanel')?.remove();
}

function openPanel() {
  closePanel();
  const bell = $('#bell');
  if (!bell) return;
  const rows = state.items.map((item, i) => `
    <li class="alert-item ${esc(item.severity)}">
      <div class="alert-line">
        <strong>${esc(item.text)}</strong>
        <span class="alert-badge" title="${item.count} alert${item.count === 1 ? '' : 's'}">${item.count > 99 ? '99+' : item.count}</span>
      </div>
      <a href="#" data-bell-open="${i}">Open ${esc(item.service)} &rsaquo;</a>
    </li>`).join('');
  const panel = document.createElement('div');
  panel.id = 'bellPanel';
  panel.className = 'alert-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Notifications');
  panel.innerHTML = `<div class="alert-head">Notifications</div>${rows
    ? `<ul>${rows}</ul>`
    : '<div class="alert-none"><span class="dot ok"></span> Nothing needs your attention.</div>'}`;
  const bar = bell.closest('.topbar') || document.body;
  bar.appendChild(panel);
  const r = bell.getBoundingClientRect();
  const b = bar.getBoundingClientRect();
  panel.style.top = `${r.bottom - b.top + 8}px`;
  panel.style.right = `${Math.max(12, b.right - r.right)}px`;
}

async function refresh() {
  if (!hooks) return;
  try {
    Object.assign(state, await hooks.api.get('/portal/notifications'));
  } catch { return; }   // the bell is a convenience: never an error on the page
  const bell = $('#bell');
  if (bell) bell.outerHTML = bellHtml();
  if ($('#bellPanel')) openPanel();
}

/** Put the bell in the top bar and keep it current. Safe to call on every page. */
export function watchBell({ api, open }) {
  hooks = { api, open };
  const slot = $('#topActions');
  if (slot && !$('#bell')) slot.innerHTML = bellHtml();
  refresh();
  if (timer) return;
  timer = setInterval(() => { if (!document.hidden) refresh(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  document.addEventListener('click', (event) => {
    const line = event.target.closest('[data-bell-open]');
    if (line) {
      event.preventDefault();
      const item = state.items[Number(line.dataset.bellOpen)];
      closePanel();
      if (item) hooks.open(item.slug, {});
    } else if (event.target.closest('#bell')) {
      if ($('#bellPanel')) closePanel(); else openPanel();
    } else if (!event.target.closest('#bellPanel')) {
      closePanel();
    }
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closePanel(); });
}

/** Signed out: forget what the last account's bell showed. */
export function resetBell() {
  Object.assign(state, { count: 0, worst: null, items: [] });
}
