/* The marketing site: services, pricing from the live catalog, contact form. */

import { money } from '/assets/api.js';
import { esc } from '/assets/ui.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// Planned services shown until they are in the catalog.
const COMING = [
  { slug: 'marketplace', name: 'Marketplace Connector', tagline: 'Orders, stock and prices between Odoo and online marketplaces.' },
  { slug: 'shipping', name: 'Shipping & Logistics', tagline: 'Rates, labels and tracking from carriers, straight into Odoo deliveries.' },
];

let catalog = { services: [] };
let interval = 'month';

function paintServices() {
  const live = catalog.services.map((s) => `<div class="site-card"><h3>${esc(s.name)}</h3><p>${esc(s.tagline || s.description || '')}</p></div>`);
  const soon = COMING.filter((c) => !catalog.services.some((s) => s.slug === c.slug))
    .map((c) => `<div class="site-card"><span class="soon">Coming soon</span><h3>${esc(c.name)}</h3><p>${esc(c.tagline)}</p></div>`);
  $('#serviceCards').innerHTML = [...live, ...soon].join('');
}

function paintPricing() {
  const body = $('#pricingBody');
  if (!catalog.services.length) { body.innerHTML = '<p class="sub">Pricing is on its way — <a href="#contact">ask us</a>.</p>'; return; }
  body.innerHTML = catalog.services.map((s) => `
    <div class="price-service">
      <h3>${esc(s.name)}</h3><p>${esc(s.tagline || '')}</p>
      <div class="price-grid">${s.packages.map((p) => {
        const cents = interval === 'year' ? p.yearly_price_cents : p.monthly_price_cents;
        const href = `/site/signup.html?service=${encodeURIComponent(s.slug)}&package=${encodeURIComponent(p.id)}&billing=${interval}`;
        return `<div class="price-card${p.is_default ? ' featured' : ''}">
          ${p.is_default ? '<span class="tag">Most Popular</span>' : ''}
          <div class="name">${esc(p.name)}</div>
          <div class="amt">${cents == null ? 'Custom' : `${money(cents, p.currency)}<span>/${interval === 'year' ? 'year' : 'month'}</span>`}</div>
          <div class="desc">${esc(p.description || '')}</div>
          <ul>${(p.features || []).map((f) => `<li>${esc(f)}</li>`).join('')}</ul>
          <a class="btn-lg${p.is_default ? ' primary' : ''}" href="${cents == null ? '#contact' : href}">${cents == null ? 'Contact Us' : 'Start Free Trial'}</a>
        </div>`;
      }).join('')}</div>
    </div>`).join('');
}

function paintContactServices() {
  const all = [...catalog.services, ...COMING.filter((c) => !catalog.services.some((s) => s.slug === c.slug))];
  $('#c_services').innerHTML = all.map((s) => `<label><input type="checkbox" value="${esc(s.slug)}"> ${esc(s.name)}</label>`).join('');
}

function wireContact() {
  let topic = 'Demo';
  const demoOnly = () => $$('.demo-only').forEach((el) => { el.style.display = topic === 'Demo' ? '' : 'none'; });
  $$('[data-topic]').forEach((b) => b.addEventListener('click', () => {
    topic = b.dataset.topic;
    $$('[data-topic]').forEach((x) => x.classList.toggle('on', x === b));
    demoOnly();
  }));
  demoOnly();
  $('#contactForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const err = $('#contactErr');
    err.textContent = '';
    const data = Object.fromEntries(new FormData(form));
    if (!data.name || !data.email || !data.company) { err.textContent = 'Please fill in your name, email and company.'; return; }
    const button = $('#contactGo');
    button.disabled = true;
    try {
      const response = await fetch('/api/v1/public/contact', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          topic, name: data.name, email: data.email, company: data.company, phone: data.phone || null,
          message: data.message || null, website: data.website || null,
          services: $$('#c_services input:checked').map((c) => c.value),
          preferred_date: topic === 'Demo' ? data.preferred_date || null : null,
          preferred_window: topic === 'Demo' ? data.preferred_window || null : null,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
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
}

$$('[data-interval]').forEach((b) => b.addEventListener('click', () => {
  interval = b.dataset.interval;
  $$('[data-interval]').forEach((x) => x.classList.toggle('on', x === b));
  paintPricing();
}));
$('#year').textContent = new Date().getFullYear();

(async () => {
  try {
    catalog = await (await fetch('/api/v1/public/catalog')).json();
  } catch { /* the page still works without prices */ }
  paintServices();
  paintPricing();
  paintContactServices();
})();
wireContact();
