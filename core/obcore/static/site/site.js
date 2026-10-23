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
let activeService = null;

function paintServices() {
  const live = catalog.services.map((s) => `<div class="site-card"><h3>${esc(s.name)}</h3><p>${esc(s.tagline || s.description || '')}</p></div>`);
  const soon = COMING.filter((c) => !catalog.services.some((s) => s.slug === c.slug))
    .map((c) => `<div class="site-card"><span class="soon">Coming soon</span><h3>${esc(c.name)}</h3><p>${esc(c.tagline)}</p></div>`);
  $('#serviceCards').innerHTML = [...live, ...soon].join('');
}

function paintPricing() {
  const body = $('#pricingBody');
  const services = catalog.services.filter((s) => s.packages.length);
  if (!services.length) { body.innerHTML = '<p class="sub">Pricing is on its way — <a href="#contact">ask us</a>.</p>'; return; }
  if (!services.some((s) => s.slug === activeService)) activeService = services[0].slug;
  const s = services.find((x) => x.slug === activeService);
  const tabs = services.length > 1
    ? `<div class="toggle"><div class="seg svc-tabs" role="tablist" aria-label="Service">${services.map((x) =>
        `<button type="button" role="tab" class="seg-btn${x.slug === activeService ? ' on' : ''}" data-svc-tab="${esc(x.slug)}">${esc(x.name)}</button>`).join('')}</div></div>`
    : '';
  body.innerHTML = `${tabs}
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
          <a class="btn-lg${p.is_default ? ' primary' : ''}" href="${cents == null ? '/site/contact.html' : href}">${cents == null ? 'Contact Us' : 'Start Free Trial'}</a>
        </div>`;
      }).join('')}</div>
    </div>`;
  $$('[data-svc-tab]', body).forEach((b) => b.addEventListener('click', () => { activeService = b.dataset.svcTab; paintPricing(); }));
}

$$('[data-interval]').forEach((b) => b.addEventListener('click', () => {
  interval = b.dataset.interval;
  $$('[data-interval]').forEach((x) => x.classList.toggle('on', x === b));
  paintPricing();
}));

(async () => {
  try {
    catalog = await (await fetch('/api/v1/public/catalog')).json();
  } catch { /* the page still works without prices */ }
  paintServices();
  paintPricing();
})();
