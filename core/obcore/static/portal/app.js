/* The OdooBridger customer portal: one account, every service, one bill. */

import '/assets/password-toggle.js';
import { fmtDate, makeClient, money } from '/assets/api.js';
import { $, $$, banner, empty, esc, field, guard, loading, readForm, timezoneNames, toast, wireSearchSelects, wireTips } from '/assets/ui.js';
import { detectZone } from '/site/geo.js';
import { authCard, dialog, match, mountShell, parseHash, submitting, unmountShell } from '/assets/shell.js';
import { multiSelect } from '/site/multiselect.js';
import { renderReports } from './reports.js';

const { api, session, restore, signOut } = makeClient('customer');
const state = { me: null, account: null, available: [], stripe: false, catalog: null };
const NEXT_KEY = 'ob.next';

const STATUS = {
  trialing: ['trialing', 'Free Trial'],
  active: ['ok', 'Active'],
  past_due: ['bad', 'Payment Needed'],
  suspended: ['bad', 'Suspended'],
  cancelled: ['mute', 'Removed'],
};
const statusPill = (s) => `<span class="pill ${STATUS[s]?.[0] || 'mute'}">${esc(STATUS[s]?.[1] || s)}</span>`;
const canManage = () => ['owner', 'admin'].includes(state.me?.role);
const isOwner = () => state.me?.role === 'owner';

async function catalog() {
  if (!state.catalog) state.catalog = await api.get('/public/catalog');
  return state.catalog;
}

/* =========================================================================
 * Signed-out pages
 * ====================================================================== */
function renderLogin(route) {
  const root = authCard(`
    <h1>Sign In</h1>
    <p class="sub">One login for every OdooBridger service.</p>
    ${route.query.reset ? banner('Password Changed', 'Sign in with your new password.', 'ok') : ''}
    <form id="form">
      ${field({ name: 'email', label: 'Email', type: 'email', required: true })}
      ${field({ name: 'password', label: 'Password', type: 'password', required: true })}
      <p class="auth-alt" style="text-align:right;margin:-4px 0 12px"><a href="#/forgot-password">Forgot password?</a></p>
      <button class="primary" style="width:100%" id="go">Sign In</button>
    </form>
    <p class="auth-alt">No account yet? <a href="/#pricing">Start a free trial</a></p>`);
  $('#form', root).addEventListener('submit', (e) => {
    e.preventDefault();
    submitting($('#go', root), async () => {
      session.persist(await api.post('/auth/login', readForm(e.target)));
      afterSignIn();
    });
  });
}

function renderForgot() {
  const root = authCard(`
    <h1>Reset Your Password</h1>
    <p class="sub">We'll email you a link to choose a new one.</p>
    <form id="form">
      ${field({ name: 'email', label: 'Email', type: 'email', required: true })}
      <button class="primary" style="width:100%" id="go">Send Link</button>
    </form>
    <p class="auth-alt"><a href="#/login">&larr; Back to sign-in</a></p>`);
  $('#form', root).addEventListener('submit', (e) => {
    e.preventDefault();
    submitting($('#go', root), async () => {
      const r = await api.post('/auth/forgot-password', readForm(e.target));
      $('#form', root).outerHTML = banner('Check Your Email', r.message, 'ok');
    });
  });
}

async function renderSetPassword(route) {
  const token = route.query.token || '';
  const root = authCard(loading());
  let info;
  try {
    info = await api.post('/auth/link/check', { token });
  } catch (error) {
    authCard(`<h1>Link Expired</h1>${banner('', error.message, 'warn')}
      <p class="auth-alt"><a href="#/forgot-password">Send a new link</a> · <a href="#/login">Sign in</a></p>`);
    return;
  }
  const first = info.purpose === 'verify';
  authCard(`
    <h1>${info.signup ? 'Create Your Account' : first ? 'Choose Your Password' : 'Choose A New Password'}</h1>
    <p class="sub">${info.signup
      ? `Your email is confirmed. Choose a password for ${esc(info.email)} to create ${esc(info.account)}${info.mode === 'buy' ? ' with the services you paid for' : ' and start your free trial'}.`
      : first ? `Welcome to OdooBridger. Your email is confirmed — set a password for ${esc(info.email)} (${esc(info.account)}).`
      : `For ${esc(info.email)}.`}</p>
    <form id="form">
      ${field({ name: 'password', label: 'New password', type: 'password', required: true,
        help: 'At least 10 characters, with upper and lower case letters and a number.' })}
      ${field({ name: 'confirm', label: 'Confirm password', type: 'password', required: true })}
      <button class="primary" style="width:100%" id="go">${info.signup ? ('Create Account') : first ? 'Save And Sign In' : 'Save Password'}</button>
    </form>`);
  $('#form').addEventListener('submit', (e) => {
    e.preventDefault();
    const values = readForm(e.target);
    submitting($('#go'), async () => {
      if (values.password !== values.confirm) throw new Error('The two passwords are different.');
      const result = await api.post('/auth/link/use', { token, password: values.password });
      session.persist(result);
      if (result.checkout_url) { window.location.href = result.checkout_url; return; }
      if (result.notice) toast(result.notice, 'bad');
      else toast(info.signup ? 'Your account is ready.' : first ? 'Welcome! You are signed in.' : 'Password changed.', 'ok');
      window.location.hash = '#/services';
    });
  });
  void root;
}

/* =========================================================================
 * Opening a service (SSO)
 * ====================================================================== */
async function openService(slug, query = {}) {
  const r = await api.post('/sso/authorize', {
    service: slug, redirect_uri: query.redirect_uri || null, state: query.state || null,
  });
  window.location.href = r.redirect_url;
}

async function renderOpen(route, params) {
  authCard(`<h1>Opening ${esc(params.slug)}</h1><p class="sub">Signing you in…</p>`);
  try {
    await openService(params.slug, route.query);
  } catch (error) {
    authCard(`<h1>Can't Open This Service</h1>${banner('', error.message, 'warn')}
      <p class="auth-alt"><a href="#/services">Go to your services</a></p>`);
  }
}

/* =========================================================================
 * Services
 * ====================================================================== */
function packagePicker(service, current, interval, name = 'package_id') {
  const packages = service.packages.filter((p) => p.is_active !== false);
  const pick = current || (packages.find((p) => p.is_default) || packages[0])?.id;
  return `<div class="pkg-grid">${packages.map((p) => {
    const cents = interval === 'year' ? p.yearly_price_cents : p.monthly_price_cents;
    return `<label class="pkg"><input type="radio" name="${esc(name)}" value="${esc(p.id)}" ${p.id === pick ? 'checked' : ''}>
      <b>${esc(p.name)}</b>
      <div class="price">${cents == null ? 'Custom' : `${money(cents, p.currency)}<span class="muted" style="font-size:12px">/${interval === 'year' ? 'yr' : 'mo'}</span>`}</div>
      ${p.description ? `<div class="hint">${esc(p.description)}</div>` : ''}
      ${p.features?.length ? `<ul>${p.features.map((f) => `<li>${esc(f)}</li>`).join('')}</ul>` : ''}
    </label>`;
  }).join('')}</div>`;
}

const TRASH = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-12M9 7V4.5a.5.5 0 0 1 .5-.5h5a.5.5 0 0 1 .5.5V7"/></svg>';

function serviceCard(row) {
  const s = row.service;
  const facts = [
    ['Plan', esc(row.package?.name || '—')],
    row.status === 'trialing' && row.trial_ends_at ? ['Trial ends', esc(fmtDate(row.trial_ends_at))] : null,
  ].filter(Boolean);
  const live = row.status !== 'cancelled';
  // A service that works from Odoo can't start until an Odoo is connected.
  const needsSetup = live && s.uses_odoo && (row.setup_complete === false
    || (row.setup_complete === undefined || row.setup_complete === null) && !(state.account.odoo || []).length);
  return `
    <div class="svc-card${needsSetup ? ' needs-setup' : ''}" data-slug="${esc(s.slug)}">
      <div class="svc-head">
        <div class="svc-name">${esc(s.name)}</div>
        ${statusPill(row.status)}
      </div>
      <dl class="svc-facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      ${row.pending_change ? `<div class="hint">Changes to <strong>${esc(row.pending_change.package.name)}</strong> on ${esc(fmtDate(row.pending_change.at))}. ${
        canManage() ? `<a href="#" data-cancel-change="${esc(s.slug)}">Cancel Change</a>` : ''}</div>` : ''}
      ${row.status === 'past_due' ? `<div class="hint" style="color:var(--bad)">${row.billed ? 'The last payment failed.' : 'The free trial has ended.'} <a href="#/billing">Pay to carry on</a>.</div>` : ''}
      <div class="svc-actions">
        ${live ? `<span class="setup-start"><button class="primary" data-open="${esc(s.slug)}">Open</button>${needsSetup
          ? '<span class="setup-pointer" role="note"><span class="setup-pointer-arrow" aria-hidden="true"></span>Complete Setup To Begin</span>' : ''}</span>` : ''}
        ${live && canManage() ? `<button data-change="${esc(s.slug)}">Change Plan</button>` : ''}
        ${live && canManage() ? `<button class="icon-btn" style="margin-left:auto" data-remove="${esc(s.slug)}" title="Remove" aria-label="Remove ${esc(s.name)}">${TRASH}</button>` : ''}
        ${!live && canManage() ? `<button data-readd="${esc(s.slug)}">Add Again</button>` : ''}
      </div>
    </div>`;
}

/* Add Services: pick one or more services and a plan for each, then either
 * start the free trial or buy them now (Stripe Checkout). An account that
 * already pays online just adds them to its subscription. */
async function addServicesDialog(preselect = []) {
  const cat = await catalog();
  const offered = cat.services.filter((s) => state.available.some((a) => a.slug === s.slug));
  if (!offered.length) return;
  const paying = state.account.pays_online;
  const canBuy = !paying && state.stripe;
  let interval = state.account.billing_interval || 'month';
  let menu;
  const plans = (form) => {
    const chosen = menu ? menu.values() : [];
    const kept = readForm(form);
    form.querySelector('[data-plans]').innerHTML = chosen.map((slug) => {
      const service = offered.find((s) => s.slug === slug);
      return `<div class="section-title" style="margin-top:16px">${esc(service.name)}</div>
        ${packagePicker(service, kept[`pkg_${slug}`], interval, `pkg_${slug}`)}`;
    }).join('');
  };
  const result = await dialog({
    title: 'Add Services',
    wide: true,
    submitLabel: paying ? 'Add To Subscription' : canBuy ? 'Buy Now' : 'Start Free Trial',
    secondaryLabel: canBuy ? 'Start Free Trial' : null,
    body: `
      <div class="field"><label>Services</label><div data-services></div></div>
      ${canBuy ? `<div class="field"><label>Billing</label>
        <div class="seg" role="group" aria-label="Billing interval">
          <button type="button" class="seg-btn ${interval === 'month' ? 'on' : ''}" data-int="month">Monthly</button>
          <button type="button" class="seg-btn ${interval === 'year' ? 'on' : ''}" data-int="year">Yearly</button>
        </div></div>` : ''}
      <div data-plans></div>
      <p class="hint" style="margin-top:12px">${paying
        ? 'They join your subscription today; the first charge is prorated.'
        : canBuy
          ? 'Start a free trial with no card, or buy now and pay by card. Buying includes any services already on trial.'
          : 'A free trial, no card needed. Add payment details any time from Billing.'}</p>`,
    onOpen: (form) => {
      menu = multiSelect(form.querySelector('[data-services]'),
        offered.map((s) => ({ value: s.slug, label: s.name, hint: s.tagline })),
        { placeholder: 'Select one or more services', selected: preselect, onChange: () => plans(form) });
      form.querySelectorAll('[data-int]').forEach((b) => b.addEventListener('click', () => {
        interval = b.dataset.int;
        form.querySelectorAll('[data-int]').forEach((x) => x.classList.toggle('on', x === b));
        plans(form);
      }));
      plans(form);
    },
    onSubmit: async (form, action) => {
      const chosen = menu.values();
      if (!chosen.length) throw new Error('Choose at least one service.');
      const values = readForm(form);
      for (const slug of chosen) {
        await api.post('/portal/services', { service: slug, package_id: values[`pkg_${slug}`] || null });
      }
      if (canBuy && action === 'primary') {
        const { url } = await api.post('/portal/billing/checkout', { interval });
        window.location.href = url;
        return { redirecting: true };
      }
      return { added: chosen.length };
    },
  });
  if (result?.redirecting) return;
  if (result) {
    toast(`${result.added === 1 ? 'Service' : `${result.added} services`} added.`, 'ok');
    await loadAccount();
    route();
  }
}

async function changePackageDialog(slug) {
  const cat = await catalog();
  const service = cat.services.find((s) => s.slug === slug);
  const row = state.account.services.find((r) => r.service.slug === slug);
  const result = await dialog({
    title: `${service.name} — Change Plan`,
    wide: true,
    submitLabel: 'Change Plan',
    body: `${packagePicker(service, row.pending_change?.package.id || row.package?.id, state.account.billing_interval)}
      ${row.billed ? `<div class="section-title" style="margin-top:16px">When To Change</div>
        <label class="row" style="gap:8px;align-items:flex-start;margin:6px 0"><input type="radio" name="when" value="now" checked>
          <span><strong>Change now</strong><br><span class="hint">The difference for the rest of this period is added to your next invoice.</span></span></label>
        <label class="row" style="gap:8px;align-items:flex-start;margin:6px 0"><input type="radio" name="when" value="period_end">
          <span><strong>At the next renewal${state.account.renews_at ? ` (${esc(fmtDate(state.account.renews_at))})` : ''}</strong><br><span class="hint">You keep the current package until then. Nothing extra is charged.</span></span></label>`
        : '<p class="hint" style="margin-top:12px">Applies now.</p>'}`,
    onSubmit: (form) => api.patch(`/portal/services/${slug}`, readForm(form)),
  });
  if (result) {
    const later = result.services?.find((r) => r.service.slug === slug)?.pending_change;
    toast(later ? `Package changes on ${fmtDate(later.at)}.` : 'Package changed.', 'ok');
    route();
  }
}

async function removeDialog(slug) {
  const row = state.account.services.find((r) => r.service.slug === slug);
  const result = await dialog({
    title: `Remove ${row.service.name}?`,
    danger: true,
    submitLabel: 'Remove',
    body: `<p>${row.billed
      ? 'It is taken off your subscription. If it is the only service you pay for, it stays on until the end of the paid period.'
      : 'It stops working now.'} The data it holds is kept until the account is deleted.</p>`,
    onSubmit: (form) => api.del(`/portal/services/${slug}`),
  });
  if (result) { toast(result.message || 'Removed.', 'ok'); route(); }
}

/* A customer who paid at registration is offered the free trial days: the paid
 * period runs in full, the trial days follow it, and nothing more is charged. */
const trialOffer = () => {
  const offer = state.account.free_trial_offer;
  if (!offer || !canManage()) return '';
  return `<div class="banner ok" style="display:flex;gap:14px;align-items:center;justify-content:space-between;flex-wrap:wrap">
    <span><strong>Add Your ${offer.days}-Day Free Trial</strong>Since you paid up front, you also get ${offer.days} free trial days on top of your paid period. Your first renewal moves back by ${offer.days} days and nothing is charged for them.</span>
    <button class="primary" data-free-trial>Add Free Trial Days</button></div>`;
};

function wireTrialOffer(mount) {
  $('[data-free-trial]', mount)?.addEventListener('click', () => guard(async () => {
    const days = state.account.free_trial_offer?.days;
    state.account = await api.post('/portal/billing/free-trial');
    toast(`${days} free trial days added. Your renewal moved back by ${days} days.`, 'ok');
    route();
  }));
}

async function renderServices(mount) {
  const live = state.account.services.filter((r) => r.status !== 'cancelled');
  const gone = state.account.services.filter((r) => r.status === 'cancelled');
  mount.innerHTML = `
    ${state.account.status === 'suspended' ? banner('Account Suspended', 'Your services are paused. Contact support to restore them.', 'bad') : ''}
    ${live.some((r) => r.service.uses_odoo && r.setup_complete !== true) && !(state.account.odoo || []).length && canManage()
      ? banner('Connect to Odoo', 'Your services need an Odoo connection before they can start working.', 'warn', { href: '#/odoo', label: 'Connect Odoo' }) : ''}
    ${trialOffer()}
    ${live.some((r) => r.status === 'trialing') && !state.account.pays_online && canManage()
      ? banner('You Are On A Free Trial', 'Add payment details before it ends to keep every service running.', '', { href: '#/billing', label: 'Billing' }) : ''}
    ${canManage() && state.available.length ? `
      <div class="row" style="justify-content:flex-end;margin-bottom:14px">
        <button class="primary" data-add-services>Add Services</button>
      </div>` : ''}
    ${!live.length ? `<div class="card" style="margin-bottom:16px">${empty('No Services Yet',
      canManage() ? 'Add a service to start its free trial, or buy it now.' : 'Ask your account owner to add one.')}</div>` : ''}
    <div class="svc-grid">
      ${live.map(serviceCard).join('')}
      ${canManage() && state.available.length ? state.available.map((s) => `
        <div class="svc-card add">
          <div class="svc-name">${esc(s.name)}</div>
          <div class="svc-tag">${esc(s.tagline || '')}</div>
          <div class="svc-actions"><button data-add="${esc(s.slug)}">Add Service</button></div>
        </div>`).join('') : ''}
    </div>
    ${gone.length ? `<div class="section-title">Removed</div><div class="svc-grid">${gone.map(serviceCard).join('')}</div>` : ''}`;
  wireTrialOffer(mount);
  mount.addEventListener('click', (e) => {
    const undo = e.target.closest('[data-cancel-change]');
    if (undo) {
      e.preventDefault();
      guard(async () => { await api.del(`/portal/services/${undo.dataset.cancelChange}/pending-change`); toast('Change cancelled.', 'ok'); route(); });
      return;
    }
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.open) guard(() => openService(b.dataset.open));
    if (b.dataset.addServices !== undefined) guard(() => addServicesDialog());
    if (b.dataset.add) guard(() => addServicesDialog([b.dataset.add]));
    if (b.dataset.readd) guard(() => addServicesDialog([b.dataset.readd]));
    if (b.dataset.change) guard(() => changePackageDialog(b.dataset.change));
    if (b.dataset.remove) guard(() => removeDialog(b.dataset.remove));
  });
}

/* =========================================================================
 * Billing
 * ====================================================================== */
/* ``quiet``: refresh in place, keeping the page as it is until the new one is
 * ready, rather than blanking it to a loading line (a toggle or a button on
 * the page itself). */
async function renderBilling(mount, route, { quiet = false } = {}) {
  if (!quiet) mount.innerHTML = loading();
  const data = await api.get('/portal/billing');
  const a = data.account;
  state.account = a;
  if (route.query.checkout === 'success') {
    toast('Payment received — thank you!', 'ok');
    history.replaceState(null, '', '#/billing');
  }
  const billed = a.services.filter((r) => r.status !== 'cancelled');
  const interval = a.billing_interval;
  const rows = billed.map((r) => {
    const cents = interval === 'year' ? r.package?.yearly_price_cents : r.package?.monthly_price_cents;
    return `<tr><td>${esc(r.service.name)}</td><td>${esc(r.package?.name || '—')}</td><td>${statusPill(r.status)}</td>
      <td class="num" data-price data-m="${r.package?.monthly_price_cents ?? ''}" data-y="${r.package?.yearly_price_cents ?? ''}" data-cur="${esc(r.package?.currency || '')}">${money(cents, r.package?.currency)}<span class="muted">/${interval === 'year' ? 'yr' : 'mo'}</span></td></tr>`;
  }).join('');
  const pm = data.payment_method;
  mount.innerHTML = `
    ${data.error ? banner('Billing Is Unavailable Right Now', data.error, 'warn') : ''}
    ${trialOffer()}
    <div class="card">
      <div class="card-head"><h2>Your Subscription</h2>
        <div class="actions">${a.pays_online && canManage()
          ? '<button id="portalBtn">Manage Payment Details</button>' : ''}</div></div>
      ${a.pays_online ? `<p>${a.cancel_at_period_end
        ? `Cancelled — your services stop on <strong>${esc(fmtDate(a.renews_at))}</strong>.`
        : `Paying ${interval === 'year' ? 'yearly' : 'monthly'} · renews <strong>${esc(fmtDate(a.renews_at))}</strong>${
          a.free_trial_added_days ? ` · includes ${a.free_trial_added_days} free trial days` : ''}`}</p>`
        : '<p>You are not paying online yet. One subscription covers every service below, on one invoice.</p>'}
      <div class="table-wrap"><table style="table-layout:fixed;min-width:560px">
        <colgroup><col style="width:30%"><col style="width:24%"><col style="width:16%"><col style="width:30%"></colgroup>
        <thead><tr><th>Service</th><th>Package</th><th>Status</th><th class="num">Price</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="muted">No services.</td></tr>'}</tbody>
        <tfoot><tr><td colspan="3"><strong>Total</strong></td><td class="num" id="billTotal"><strong>${money(a.monthly_total_cents)}</strong><span class="muted">/mo${interval === 'year' ? ' (billed yearly)' : ''}</span></td></tr></tfoot>
      </table></div>
      ${!a.pays_online && canManage() ? `
        <div class="row" style="gap:10px;margin-top:16px;flex-wrap:wrap;align-items:center">
          <div class="seg" role="group" aria-label="Billing interval">
            <button type="button" class="seg-btn ${interval === 'month' ? 'on' : ''}" data-interval="month">Monthly</button>
            <button type="button" class="seg-btn ${interval === 'year' ? 'on' : ''}" data-interval="year">Yearly</button>
          </div>
          <button class="primary" id="payBtn" ${data.stripe_enabled && billed.length ? '' : 'disabled'}>Add Payment Details</button>
          ${!data.stripe_enabled ? '<span class="hint">Online payment is not set up yet — contact us to pay.</span>' : ''}
        </div>` : ''}
      ${a.pays_online && isOwner() ? `<div class="row" style="margin-top:14px">${a.cancel_at_period_end
        ? '<button id="resumeBtn">Keep My Subscription</button>'
        : '<button class="danger-outline" id="cancelBtn">Cancel Subscription</button>'}</div>` : ''}
    </div>
    ${pm ? `<div class="card" style="margin-top:14px"><div class="card-head"><h2>Payment Method</h2></div>
      <p>${esc((pm.brand || 'card').toUpperCase())} ending ${esc(pm.last4 || '••••')}${pm.exp_month ? ` · expires ${esc(pm.exp_month)}/${esc(pm.exp_year)}` : ''}</p></div>` : ''}
    <div class="card" style="margin-top:14px"><div class="card-head"><h2>Invoices</h2></div>
      ${data.invoices.length ? `<div class="table-wrap"><table><thead><tr><th>Date</th><th>Number</th><th>Status</th><th class="num">Amount</th><th></th></tr></thead>
        <tbody>${data.invoices.map((i) => `<tr><td>${esc(fmtDate(i.created))}</td><td class="mono">${esc(i.number || i.id)}</td>
          <td>${i.status === 'paid' ? '<span class="pill ok">Paid</span>' : i.status === 'open' ? '<span class="pill warn">Due</span>' : `<span class="pill mute">${esc(i.status)}</span>`}</td>
          <td class="num">${money(i.status === 'paid' ? i.amount_paid : i.amount_due, i.currency)}</td>
          <td class="right">${i.payable ? `<a class="btn sm" href="${esc(i.hosted_invoice_url)}" target="_blank" rel="noopener">Pay</a>` : ''}
            ${i.invoice_pdf ? `<a class="btn sm" href="${esc(i.invoice_pdf)}" target="_blank" rel="noopener">PDF</a>` : ''}</td></tr>`).join('')}</tbody></table></div></div>`
        : empty('No Invoices Yet', '')}
    </div>`;
  wireTrialOffer(mount);
  /* Switching the interval repaints the prices from what the page already holds, then
   * saves in the background; the table itself is never rebuilt, so nothing jumps. */
  const paint = (to) => {
    let total = 0;
    let cur;
    $$('[data-price]', mount).forEach((td) => {
      const m = Number(td.dataset.m);
      const y = Number(td.dataset.y);
      const c = to === 'year' ? y : m;
      cur = td.dataset.cur || cur;
      total += to === 'year' ? Math.floor(y / 12) : m;
      td.innerHTML = `${money(c, td.dataset.cur || undefined)}<span class="muted">/${to === 'year' ? 'yr' : 'mo'}</span>`;
    });
    const t = $('#billTotal', mount);
    if (t) t.innerHTML = `<strong>${money(total, cur)}</strong><span class="muted">/mo${to === 'year' ? ' (billed yearly)' : ''}</span>`;
    $$('[data-interval]', mount).forEach((x) => x.classList.toggle('on', x.dataset.interval === to));
  };
  let current = interval;
  $$('[data-interval]', mount).forEach((b) => b.addEventListener('click', () => {
    const to = b.dataset.interval;
    if (to === current) return;
    const before = current;
    current = to;
    paint(to);
    guard(async () => {
      try {
        await api.post('/portal/billing/interval', { interval: to });
        if (state.account) state.account.billing_interval = to;
      } catch (error) {
        current = before;
        paint(before);
        throw error;
      }
    });
  }));
  $('#payBtn', mount)?.addEventListener('click', () => guard(async () => {
    const r = await api.post('/portal/billing/checkout', { interval: current });
    window.location.href = r.url;
  }));
  $('#portalBtn', mount)?.addEventListener('click', () => guard(async () => {
    window.location.href = (await api.post('/portal/billing/portal', {})).url;
  }));
  $('#cancelBtn', mount)?.addEventListener('click', async () => {
    const ok = await dialog({
      title: 'Cancel Your Subscription?', danger: true, submitLabel: 'Cancel Subscription',
      body: `<p>Every service keeps working until <strong>${esc(fmtDate(a.renews_at))}</strong>, then stops. Nothing more is charged. Your account and data stay until you delete them.</p>`,
      onSubmit: () => api.post('/portal/billing/cancel'),
    });
    if (ok) { toast('Subscription cancelled.', 'ok'); renderBilling(mount, { query: {} }, { quiet: true }); }
  });
  $('#resumeBtn', mount)?.addEventListener('click', () => guard(async () => {
    await api.post('/portal/billing/resume');
    await renderBilling(mount, { query: {} }, { quiet: true });
  }, 'Your subscription continues.'));
}

/* =========================================================================
 * Team
 * ====================================================================== */
const ROLES = [['owner', 'Owner'], ['admin', 'Admin'], ['member', 'Member']];
async function renderTeam(mount) {
  const users = await api.get('/portal/users');
  mount.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>People</h2><div class="actions">${canManage() ? '<button class="primary sm" id="invite">Invite User</button>' : ''}</div></div>
      <p class="hint">Everyone here signs in once and can open every service your account has. Admins manage services and billing; members use the services.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th></th></tr></thead>
        <tbody>${users.map((u) => `<tr>
          <td>${esc(u.full_name || '—')}</td><td>${esc(u.email)}</td>
          <td>${canManage() && u.id !== state.me.id && u.role !== 'owner'
            ? `<select data-role="${esc(u.id)}">${ROLES.filter(([v]) => v !== 'owner' || isOwner()).map(([v, l]) => `<option value="${v}" ${u.role === v ? 'selected' : ''}>${l}</option>`).join('')}</select>`
            : esc(ROLES.find(([v]) => v === u.role)?.[1] || u.role)}</td>
          <td>${!u.is_active ? '<span class="pill mute">Disabled</span>' : u.has_password ? '<span class="pill ok">Active</span>' : '<span class="pill warn">Invited</span>'}</td>
          <td class="right nowrap">${canManage() && u.id !== state.me.id && u.role !== 'owner' ? `
            ${!u.has_password ? `<button class="sm" data-resend="${esc(u.id)}">Resend Invite</button>` : ''}
            <button class="sm danger-outline" data-remove="${esc(u.id)}">Remove</button>` : ''}</td></tr>`).join('')}</tbody>
      </table></div>
    </div>`;
  $('#invite', mount)?.addEventListener('click', async () => {
    const ok = await dialog({
      title: 'Invite User', submitLabel: 'Send Invitation',
      body: `${field({ name: 'full_name', label: 'Name' })}${field({ name: 'email', label: 'Email', type: 'email', required: true })}
        ${field({ name: 'role', label: 'Role', options: ROLES.filter(([v]) => v !== 'owner').map(([value, label]) => ({ value, label })), value: 'member' })}
        <p class="hint">They get an email to set their own password.</p>`,
      onSubmit: (form) => api.post('/portal/users', readForm(form)),
    });
    if (ok) { toast('Invitation sent.', 'ok'); renderTeam(mount); }
  });
  $$('[data-role]', mount).forEach((s) => s.addEventListener('change', () =>
    guard(() => api.patch(`/portal/users/${s.dataset.role}`, { role: s.value }), 'Role changed.')));
  $$('[data-resend]', mount).forEach((b) => b.addEventListener('click', () =>
    guard(() => api.post(`/portal/users/${b.dataset.resend}/resend`), 'Invitation sent again.')));
  $$('[data-remove]', mount).forEach((b) => b.addEventListener('click', async () => {
    const ok = await dialog({ title: 'Remove This Person?', danger: true, submitLabel: 'Remove',
      body: '<p>They can no longer sign in to OdooBridger or any of its services.</p>',
      onSubmit: () => api.del(`/portal/users/${b.dataset.remove}`) });
    if (ok) { toast('Removed.', 'ok'); renderTeam(mount); }
  }));
}

/* =========================================================================
 * Account
 * ====================================================================== */
async function renderAccount(mount) {
  const a = state.account;
  const stored = { timezone: a.timezone || '' };
  // Nothing saved yet: offer the browser's own time zone.
  const zone = stored.timezone && stored.timezone !== 'UTC' ? stored.timezone : detectZone();
  const zoneItems = timezoneNames().map((z) => ({ value: z, label: z }));
  const manage = canManage();
  mount.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Company</h2>
        <div class="actions">
          ${manage ? '<button class="primary sm" type="submit" form="company" id="saveCompany" hidden>Save Changes</button>' : ''}
          <button class="sm" type="button" id="changePw">Change Password</button>
          ${isOwner() ? `<button class="icon-btn" type="button" id="delete" title="Delete Account" aria-label="Delete account">${TRASH}</button>` : ''}</div></div>
      <form id="company">
        ${field({ name: 'name', label: 'Company name', value: a.name, required: true })}
        ${field({ name: 'timezone', label: 'Timezone', value: zone, items: zoneItems, placeholder: 'Search time zones' })}
      </form>
    </div>`;
  const form = $('#company', mount);
  const save = $('#saveCompany', mount);
  const initial = { name: a.name, timezone: stored.timezone };
  const changed = () => {
    const now = readForm(form);
    return ['name', 'timezone'].some((k) => (now[k] || '') !== (initial[k] || ''));
  };
  const refresh = () => { if (save) save.hidden = !changed(); };
  form.addEventListener('input', refresh);
  form.addEventListener('change', refresh);
  refresh();
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (!manage || !changed()) return;
    guard(async () => {
      state.account = await api.patch('/portal/account', readForm(e.target));
      Object.assign(initial, { name: state.account.name, timezone: state.account.timezone || '' });
      refresh();
    }, 'Saved.');
  });
  $('#changePw', mount).addEventListener('click', () => guard(async () => {
    const done = await dialog({
      title: 'Change Password', submitLabel: 'Change Password',
      body: `${field({ name: 'current_password', label: 'Current password', type: 'password', required: true })}
        ${field({ name: 'new_password', label: 'New password', type: 'password', required: true,
          help: 'At least 10 characters, with upper and lower case letters and a number.' })}
        ${field({ name: 'confirm_password', label: 'Confirm new password', type: 'password', required: true })}`,
      onSubmit: async (f) => {
        const v = readForm(f);
        if (v.new_password !== v.confirm_password) throw new Error('The new passwords do not match.');
        await api.post('/auth/change-password', { current_password: v.current_password, new_password: v.new_password });
      },
    });
    if (done) toast('Password changed.', 'ok');
  }));
  $('#delete', mount)?.addEventListener('click', async () => {
    const ok = await dialog({
      title: 'Delete This Account?', danger: true, submitLabel: 'Delete Everything',
      body: `<p>This deletes ${esc(a.name)}, every service's data for it, and its subscription. It can't be undone.</p>
        <p>Type <strong>${esc(a.name)}</strong> to confirm.</p>
        ${field({ name: 'confirm_name', label: 'Company name', required: true })}
        ${field({ name: 'reason', label: 'Why are you leaving?', type: 'textarea' })}`,
      onSubmit: (form) => api.post('/portal/account/delete', readForm(form)),
    });
    if (ok) { await signOut(); window.location.href = '/'; }
  });
}


/* =========================================================================
 * Odoo: the account's one connection, used by every Odoo service
 * ====================================================================== */
const ODOO_STATUS = {
  connected: ['ok', 'Connected'],
  failed: ['bad', 'Failed'],
  unverified: ['mute', 'Not Checked'],
};
const odooPill = (s) => `<span class="pill ${ODOO_STATUS[s]?.[0] || 'mute'}">${esc(ODOO_STATUS[s]?.[1] || s)}</span>`;
const readyPill = (r) => (r === true ? '<span class="pill ok">Ready</span>'
  : r === false ? '<span class="pill bad">Not Ready</span>' : '<span class="pill mute">Not Checked</span>');

function odooFields(conn = null) {
  return `
    ${field({ name: 'url', label: 'Odoo address', required: true, value: conn?.url || '',
      placeholder: 'https://yourcompany.odoo.com',
      help: 'The public address of your Odoo, e.g. https://yourcompany.odoo.com. If Odoo runs on your own server, use its public IP or domain with the port forwarded, e.g. http://203.0.113.10:8069.' })}
    ${field({ name: 'db', label: 'Database', value: conn?.db || '',
      help: conn ? '' : 'Leave blank on Odoo Online: it is the name before .odoo.com.' })}
    ${field({ name: 'login', label: 'Login', required: true, value: conn?.login || '',
      placeholder: 'integration@yourcompany.com' })}
    ${field({ name: 'api_key', label: 'API key', type: 'password', required: !conn,
      placeholder: conn ? 'Unchanged' : '',
      help: conn ? 'Leave blank to keep the current key.'
        : 'In Odoo: your avatar → My Profile → Account Security → New API Key.' })}
    ${field({ name: 'key_expires_at', label: 'Key expires on', type: 'date',
      value: conn?.key_expires_at ? conn.key_expires_at.slice(0, 10) : '' })}`;
}

function odooValues(form, conn) {
  const values = readForm(form);
  if (conn && !values.api_key) delete values.api_key;
  if (!values.key_expires_at) values.key_expires_at = null;
  return values;
}

function odooCard(conn) {
  const companies = (conn.companies || []).map((c) => esc(c.name)).join(', ') || '—';
  const facts = [
    ['Address', `<a href="${esc(conn.url)}" target="_blank" rel="noopener">${esc(conn.url)}</a>`],
    ['Database', esc(conn.db)],
    ['Login', esc(conn.login)],
    ['Version', conn.version ? `${esc(conn.version)} · ${conn.api === 'json2' ? 'JSON-2 API' : 'XML-RPC'}` : '—'],
    ['Companies', companies],
    ['Last checked', conn.last_checked_at ? esc(fmtDate(conn.last_checked_at)) : '—'],
    conn.key_expires_at ? ['Key expires', esc(fmtDate(conn.key_expires_at))] : null,
  ].filter(Boolean);
  return `
    <div class="card">
      <div class="card-head"><h2>${esc(conn.name)}</h2>
        <div class="actions">${backToBioBridge()}${odooPill(conn.status)}
          <button class="sm" data-odoo-test="${esc(conn.id)}">Test Connection</button>
          ${canManage() ? `<button class="sm" data-odoo-edit="${esc(conn.id)}">Edit</button>
            <button class="icon-btn" data-odoo-remove="${esc(conn.id)}" title="Disconnect" aria-label="Disconnect Odoo">${TRASH}</button>` : ''}</div></div>
      ${conn.status === 'failed' && conn.status_message ? banner('Odoo Is Refusing The Connection', conn.status_message, 'bad') : ''}
      <dl class="svc-facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
    </div>
    <div class="section-title">Services Using This Odoo</div>
    ${conn.services.length ? `<div class="card"><div class="table-wrap"><table><thead><tr><th>Service</th><th>Status</th><th>Details</th></tr></thead><tbody>
      ${conn.services.map((s) => `<tr>
        <td>${esc(s.name)}</td>
        <td>${readyPill(s.ready)}</td>
        <td>${esc(s.message || (s.in_sync ? '' : 'Waiting for the service to pick up the change.'))}
        </td>
      </tr>`).join('')}</tbody></table></div></div>`
    : `<div class="card">${empty('No Services Use Odoo Yet', 'Services that work with Odoo connect to it automatically when you add them.')}</div>`}`;
}


/* Set only when BioBridge sent the person here; keeps the way back for the visit. */
let cameFromBioBridge = false;
const backToBioBridge = () => (cameFromBioBridge
  ? '<a href="#" class="banner-action" data-back-bio style="text-decoration:underline">Continue With BioBridge Setup &rarr;</a>' : '');

async function renderOdoo(mount, route) {
  if (route) cameFromBioBridge = route.query?.from === 'biobridge';
  mount.innerHTML = loading();
  const data = await api.get('/portal/odoo');
  const conn = data.connections[0] || null;
  if (!conn) {
    mount.innerHTML = `
      <div class="card">
        <div class="card-head"><h2>Connect to Odoo</h2></div>
        ${canManage() ? `<form id="odooForm">${odooFields()}
            <p class="err" id="odooErr"></p>
            <div class="row" style="gap:10px"><button class="primary" id="odooGo">Test And Connect</button>${backToBioBridge()}</div></form>`
          : '<p class="muted">Ask your account owner or an admin to connect Odoo.</p>'}
      </div>`;
    $('[data-back-bio]', mount)?.addEventListener('click', (e) => { e.preventDefault(); guard(() => openService('biometric')); });
    $('#odooForm', mount)?.addEventListener('submit', (e) => {
      e.preventDefault();
      $('#odooErr', mount).textContent = '';
      submitting($('#odooGo', mount), async () => {
        try {
          await api.post('/portal/odoo', odooValues(e.target, null));
        } catch (error) {
          $('#odooErr', mount).textContent = error.message;
          return;
        }
        toast('Odoo connected.', 'ok');
        renderOdoo(mount);
      });
    });
    return;
  }
  mount.innerHTML = odooCard(conn);
  mount.onclick = (e) => {
    if (e.target.closest('[data-back-bio]')) { e.preventDefault(); guard(() => openService('biometric')); return; }
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.odooTest) {
      submitting(b, async () => {
        const r = await api.post(`/portal/odoo/${conn.id}/test`);
        const fresh = r.connections[0];
        toast(fresh.status === 'connected' ? 'Odoo answered. The connection works.' : fresh.status_message || 'The test failed.',
          fresh.status === 'connected' ? 'ok' : 'bad');
        renderOdoo(mount);
      });
    }
    if (b.dataset.odooEdit) {
      guard(async () => {
        const done = await dialog({
          title: 'Edit The Odoo Connection',
          submitLabel: 'Test And Save',
          body: odooFields(conn),
          onSubmit: (form) => api.patch(`/portal/odoo/${conn.id}`, odooValues(form, conn)),
        });
        if (done) { toast('Saved. Your services have the new details.', 'ok'); renderOdoo(mount); }
      });
    }
    if (b.dataset.odooRemove) {
      guard(async () => {
        const done = await dialog({
          title: 'Disconnect Odoo?', danger: true, submitLabel: 'Disconnect',
          body: `<p>${conn.services.map((s) => esc(s.name)).join(', ') || 'Your services'} will stop sending anything to Odoo until you connect it again. Nothing is deleted.</p>`,
          onSubmit: () => api.del(`/portal/odoo/${conn.id}`),
        });
        if (done) { toast('Odoo disconnected.', 'ok'); renderOdoo(mount); }
      });
    }
  };
}

/* =========================================================================
 * Router
 * ====================================================================== */
const PAGES = [
  { path: '/services', title: 'Services', render: renderServices },
  { path: '/account', title: 'Company', render: renderCompany, group: 'account' },
  { path: '/odoo', title: 'Odoo', render: renderOdoo, group: 'account' },
  { path: '/billing', title: 'Billing', render: renderBilling, group: 'account' },
  { path: '/reports', title: 'Reports', render: (mount, route) => renderReports(mount, route, { api, canManage }) },
];
/** The Company tab: the company's details, then its people. */
async function renderCompany(mount) {
  mount.innerHTML = '<div id="companyPart"></div><div id="peoplePart" style="margin-top:14px"></div>';
  await Promise.all([renderAccount($('#companyPart', mount)), renderTeam($('#peoplePart', mount))]);
}
const ACCOUNT_TABS = PAGES.filter((p) => p.group === 'account');
/** The sidebar: Services, and Account holding company, people, Odoo and billing as tabs. */
const NAV = [
  { path: '/services', title: 'Services', icon: 'grid' },
  { path: '/reports', title: 'Reports', icon: 'reports' },
  { path: '/account', title: 'Account', icon: 'account', paths: [...ACCOUNT_TABS.map((p) => p.path), '/team'] },
];
const accountTabs = (path) => `<nav class="tabs" aria-label="Account">${ACCOUNT_TABS.map((p) =>
  `<a href="#${p.path}"${p.path === path ? ' class="active" aria-current="page"' : ''}>${esc(p.title)}</a>`).join('')}</nav>`;

/** The account's name in the top bar: a name tag, not a status pill. */
const accountChip = (name) => {
  const initial = (String(name || '').trim()[0] || '?').toUpperCase();
  return `<span class="account-chip" title="${esc(name)}"><span class="account-initial" aria-hidden="true">${esc(initial)}</span><span class="account-name">${esc(name)}</span></span>`;
};

async function loadAccount() {
  const data = await api.get('/portal/account');
  state.me = data.me;
  state.account = data.account;
  state.available = data.available;
  state.stripe = data.stripe_enabled;
}

function afterSignIn() {
  let next = null;
  try { next = sessionStorage.getItem(NEXT_KEY); sessionStorage.removeItem(NEXT_KEY); } catch { /* none */ }
  if (next && window.location.hash !== next) window.location.hash = next;
  else route();
}

let running = false;
let again = false;
async function route() {
  if (running) { again = true; return; }
  running = true;
  try {
    const r = parseHash();
    if (r.path === '/set-password' || r.path === '/reset-password') return renderSetPassword(r);
    const signedIn = session.signedIn || await restore();
    if (!signedIn) {
      if (r.path === '/forgot-password') return renderForgot();
      if (r.path !== '/login' && r.path !== '/') {
        try { sessionStorage.setItem(NEXT_KEY, window.location.hash); } catch { /* none */ }
      }
      return renderLogin(r);
    }
    const open = match('/open/:slug', r.path);
    if (open) return renderOpen(r, open);
    if (['/', '/login', '/forgot-password'].includes(r.path)) { window.location.hash = '#/services'; return; }
    if (r.path === '/team') { window.location.hash = '#/account'; return; }   // People now lives on Company
    const page = PAGES.find((p) => p.path === r.path);
    await loadAccount();
    mountShell({
      nav: [{ items: NAV }],
      user: state.me.email,
      pill: accountChip(state.account.name),
      onSignOut: async () => { await signOut(); unmountShell(); window.location.hash = '#/login'; },
    });
    $('#pageTitle').textContent = page ? (page.group === 'account' ? 'Account' : page.title) : 'Not Found';
    const content = $('#content');
    const fresh = content.cloneNode(false);   // drop listeners from the last page
    content.replaceWith(fresh);
    if (!page) { fresh.innerHTML = empty('Page Not Found', ''); return; }
    let target = fresh;
    if (page.group === 'account') {
      fresh.innerHTML = `${accountTabs(page.path)}<div id="tabBody"></div>`;
      target = $('#tabBody', fresh);
    }
    try {
      await page.render(target, r);
    } catch (error) {
      if (error.status !== 401) target.innerHTML = banner('Could Not Load This Page', error.message, 'bad');
    }
  } finally {
    running = false;
    if (again) { again = false; route(); }
  }
}

window.addEventListener('ob:signed-out', () => { unmountShell(); window.location.hash = '#/login'; route(); });
window.addEventListener('hashchange', route);
wireTips();
wireSearchSelects();
route();
