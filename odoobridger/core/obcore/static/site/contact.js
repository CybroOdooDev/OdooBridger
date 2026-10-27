/* The contact page: topic tabs, services dropdown, phone with country code. */

import { esc } from '/assets/ui.js';
import { multiSelect } from '/site/multiselect.js';
import { detectCountryEntry, detectZone } from '/site/geo.js';
import { phoneCode } from '/site/phone-code.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// Planned services shown until they are in the catalog.
const COMING = [
  { slug: 'marketplace', name: 'Marketplace Connector' },
  { slug: 'shipping', name: 'Shipping & Logistics' },
];
const ICONS = {
  email: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>',
  phone: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2Z"/></svg>',
  hours: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  website: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
  address: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s7-6.2 7-12a7 7 0 1 0-14 0c0 5.8 7 12 7 12Z"/><circle cx="12" cy="9" r="2.5"/></svg>',
};

let topic = new URLSearchParams(window.location.search).get('topic') === 'question' ? 'Sales question' : 'Demo';
let services = null;

// Country code: worked out from the browser's location (its time zone); type to search.
const code = phoneCode($('#c_dial'), detectCountryEntry());

function showTopic() {
  $$('[data-topic]').forEach((b) => b.classList.toggle('on', b.dataset.topic === topic));
  $$('.demo-only').forEach((el) => { el.style.display = topic === 'Demo' ? '' : 'none'; });
}
$$('[data-topic]').forEach((b) => b.addEventListener('click', () => { topic = b.dataset.topic; showTopic(); }));
showTopic();

(async () => {
  let catalog = { services: [] };
  try { catalog = await (await fetch('/api/v1/public/catalog')).json(); } catch { /* the form still works */ }
  const all = [...catalog.services, ...COMING.filter((c) => !catalog.services.some((s) => s.slug === c.slug))];
  services = multiSelect($('#c_services'), all.map((s) => ({ value: s.slug, label: s.name })), { placeholder: 'Select one or more services' });
  try {
    const info = await (await fetch('/api/v1/public/site')).json();
    const rows = [['email', 'Email', info.email, info.email && `mailto:${info.email}`], ['phone', 'Phone', info.phone, info.phone && `tel:${info.phone.replace(/[^+\d]/g, '')}`],
      ['address', 'Address', info.address, null]]
      .filter((r) => r[2]);
    const list = $('#contactInfo');
    list.innerHTML = rows.map(([k, label, value, href]) => `<li><span class="ci-icon">${ICONS[k]}</span><span><b>${label}</b>
      ${href ? `<a href="${esc(href)}">${esc(value)}</a>` : `<span>${esc(value)}</span>`}</span></li>`).join('');
    $('#companyName').textContent = info.company || '';
    $('#companyCard').hidden = !(info.company || rows.length);
    list.hidden = !rows.length;
  } catch { /* nothing to show */ }
})();

$('#contactForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('#contactErr');
  err.textContent = '';
  const data = Object.fromEntries(new FormData(e.target));
  if (!data.name || !data.email || !data.company) { err.textContent = 'Please fill in your name, email and company.'; return; }
  const button = $('#contactGo');
  button.disabled = true;
  const number = (data.phone || '').trim();
  try {
    const response = await fetch('/api/v1/public/contact', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        topic, name: data.name, email: data.email, company: data.company,
        phone: number ? `${code.dial()} ${number}` : null,
        message: data.message || null, website: data.website || null,
        services: services ? services.values() : [],
        preferred_date: topic === 'Demo' ? data.preferred_date || null : null,
        preferred_window: topic === 'Demo' ? data.preferred_window || null : null,
        timezone: detectZone(),
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Please check the form and try again.');
    $('#contactCard').innerHTML = `<div class="done"><h3>Thank You</h3><p>${esc(payload.message)}</p></div>`;
  } catch (error) {
    err.textContent = error.message;
    button.disabled = false;
  }
});
