/* The OdooBridger customer portal: one account, every service, one bill. */

import '/assets/password-toggle.js';
import { fmtDate, makeClient, money } from '/assets/api.js';
import { $, $$, banner, empty, esc, field, guard, loading, readForm, toast, wireTips } from '/assets/ui.js';
import { authCard, dialog, match, mountShell, parseHash, submitting, unmountShell } from '/assets/shell.js';

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

function limitLine(ent = {}) {
  const parts = [];
  if ('max_devices' in ent) parts.push(ent.max_devices == null ? 'Unlimited terminals' : `${ent.max_devices} terminals`);
  if ('max_employees' in ent) parts.push(ent.max_employees == null ? 'unlimited employees' : `${ent.max_employees} employees`);
  if ('max_seats' in ent) parts.push(ent.max_seats == null ? 'Unlimited seats' : `${ent.max_seats} seats`);
  return parts.join(' · ');
}

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
    <p class="auth-alt">No account yet? <a href="/#pricing">Start a free trial</a></p>`,
  { foot: '<a href="/staff/">Staff sign-in</a>' });
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
    <h1>${first ? 'Choose Your Password' : 'Choose A New Password'}</h1>
    <p class="sub">${first ? `Welcome to OdooBridger. Your email is confirmed — set a password for ${esc(info.email)} (${esc(info.account)}).`
      : `For ${esc(info.email)}.`}</p>
    <form id="form">
      ${field({ name: 'password', label: 'New password', type: 'password', required: true,
        help: 'At least 10 characters, with upper and lower case letters and a number.' })}
      ${field({ name: 'confirm', label: 'Repeat it', type: 'password', required: true })}
      <button class="primary" style="width:100%" id="go">${first ? 'Save And Sign In' : 'Save Password'}</button>
    </form>`);
  $('#form').addEventListener('submit', (e) => {
    e.preventDefault();
    const values = readForm(e.target);
    submitting($('#go'), async () => {
      if (values.password !== values.confirm) throw new Error('The two passwords are different.');
      session.persist(await api.post('/auth/link/use', { token, password: values.password }));
      toast(first ? 'Welcome! You are signed in.' : 'Password changed.', 'ok');
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
function packagePicker(service, current, interval) {
  const packages = service.packages.filter((p) => p.is_active !== false);
  const pick = current || (packages.find((p) => p.is_default) || packages[0])?.id;
  return `<div class="pkg-grid">${packages.map((p) => {
    const cents = interval === 'year' ? p.yearly_price_cents : p.monthly_price_cents;
    return `<label class="pkg"><input type="radio" name="package_id" value="${esc(p.id)}" ${p.id === pick ? 'checked' : ''}>
      <b>${esc(p.name)}</b>
      <div class="price">${cents == null ? 'Custom' : `${money(cents, p.currency)}<span class="muted" style="font-size:12px">/${interval === 'year' ? 'yr' : 'mo'}</span>`}</div>
      ${p.description ? `<div class="hint">${esc(p.description)}</div>` : ''}
      ${p.features?.length ? `<ul>${p.features.map((f) => `<li>${esc(f)}</li>`).join('')}</ul>` : ''}
    </label>`;
  }).join('')}</div>`;
}

function serviceCard(row) {
  const s = row.service;
  const facts = [
    ['Package', esc(row.package?.name || '—')],
    row.status === 'trialing' && row.trial_ends_at ? ['Trial ends', esc(fmtDate(row.trial_ends_at))] : null,
    limitLine(row.entitlements) ? ['Includes', esc(limitLine(row.entitlements))] : null,
    ['Billing', row.billed ? 'On your subscription' : row.status === 'cancelled' ? '—' : 'Not paying yet'],
  ].filter(Boolean);
  const live = row.status !== 'cancelled';
  return `
    <div class="svc-card" data-slug="${esc(s.slug)}">
      <div class="svc-head">
        <div><div class="svc-name">${esc(s.name)}</div>${s.tagline ? `<div class="svc-tag">${esc(s.tagline)}</div>` : ''}</div>
        ${statusPill(row.status)}
      </div>
      <dl class="svc-facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      ${row.status === 'past_due' ? `<div class="hint" style="color:var(--bad)">${row.billed ? 'The last payment failed.' : 'The free trial has ended.'} <a href="#/billing">Pay to carry on</a>.</div>` : ''}
      <div class="svc-actions">
        ${live ? `<button class="primary" data-open="${esc(s.slug)}">Open</button>` : ''}
        ${live && canManage() ? `<button data-change="${esc(s.slug)}">Change Package</button>` : ''}
        ${live && canManage() ? `<button class="danger-outline" data-remove="${esc(s.slug)}">Remove</button>` : ''}
        ${!live && canManage() ? `<button data-readd="${esc(s.slug)}">Add Again</button>` : ''}
      </div>
    </div>`;
}

async function addServiceDialog(slug) {
  const cat = await catalog();
  const service = cat.services.find((s) => s.slug === slug);
  if (!service) return;
  const paying = state.account.pays_online;
  const result = await dialog({
    title: `Add ${service.name}`,
    wide: true,
    submitLabel: paying ? 'Add To Subscription' : 'Start Free Trial',
    body: `<p class="muted">${esc(service.description || service.tagline || '')}</p>
      ${packagePicker(service, null, state.account.billing_interval)}
      <p class="hint" style="margin-top:12px">${paying
        ? 'It joins your subscription today; the first charge is prorated.'
        : `A free trial, no card needed${cat.default_trial_days ? '' : ''}. Add payment details any time from Billing.`}</p>`,
    onSubmit: async (form) => {
      const { package_id: packageId } = readForm(form);
      return api.post('/portal/services', { service: slug, package_id: packageId });
    },
  });
  if (result) { toast(`${service.name} added.`, 'ok'); route(); }
}

async function changePackageDialog(slug) {
  const cat = await catalog();
  const service = cat.services.find((s) => s.slug === slug);
  const row = state.account.services.find((r) => r.service.slug === slug);
  const result = await dialog({
    title: `${service.name} — Change Package`,
    wide: true,
    submitLabel: 'Change Package',
    body: `${packagePicker(service, row.package?.id, state.account.billing_interval)}
      <p class="hint" style="margin-top:12px">${row.billed
        ? 'Your subscription changes now; the difference is prorated on the next invoice.'
        : 'Applies now.'}</p>`,
    onSubmit: (form) => api.patch(`/portal/services/${slug}`, readForm(form)),
  });
  if (result) { toast('Package changed.', 'ok'); route(); }
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

async function renderServices(mount) {
  const live = state.account.services.filter((r) => r.status !== 'cancelled');
  const gone = state.account.services.filter((r) => r.status === 'cancelled');
  mount.innerHTML = `
    ${state.account.status === 'suspended' ? banner('Account Suspended', 'Your services are paused. Contact support to restore them.', 'bad') : ''}
    ${live.some((r) => r.status === 'trialing') && !state.account.pays_online && canManage()
      ? banner('You Are On A Free Trial', 'Add payment details before it ends to keep every service running.', '', { href: '#/billing', label: 'Billing' }) : ''}
    <div class="svc-grid">
      ${live.map(serviceCard).join('')}
      ${canManage() && state.available.length ? state.available.map((s) => `
        <div class="svc-card add">
          <div class="svc-name">${esc(s.name)}</div>
          <div class="svc-tag">${esc(s.tagline || '')}</div>
          <div class="svc-actions"><button data-add="${esc(s.slug)}">Add Service</button></div>
        </div>`).join('') : ''}
      ${!live.length && !state.available.length ? `<div class="card">${empty('No Services Yet', 'Ask your account owner to add one.')}</div>` : ''}
    </div>
    ${gone.length ? `<div class="section-title">Removed</div><div class="svc-grid">${gone.map(serviceCard).join('')}</div>` : ''}`;
  mount.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.open) guard(() => openService(b.dataset.open));
    if (b.dataset.add) guard(() => addServiceDialog(b.dataset.add));
    if (b.dataset.readd) guard(() => addServiceDialog(b.dataset.readd));
    if (b.dataset.change) guard(() => changePackageDialog(b.dataset.change));
    if (b.dataset.remove) guard(() => removeDialog(b.dataset.remove));
  });
}

/* =========================================================================
 * Billing
 * ====================================================================== */
async function renderBilling(mount, route) {
  mount.innerHTML = loading();
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
      <td class="num">${money(cents, r.package?.currency)}<span class="muted">/${interval === 'year' ? 'yr' : 'mo'}</span></td></tr>`;
  }).join('');
  const pm = data.payment_method;
  mount.innerHTML = `
    ${data.error ? banner('Billing Is Unavailable Right Now', data.error, 'warn') : ''}
    <div class="card">
      <div class="card-head"><h2>Your Subscription</h2>
        <div class="actions">${a.pays_online && canManage()
          ? '<button id="portalBtn">Manage Payment Details</button>' : ''}</div></div>
      ${a.pays_online ? `<p>${a.cancel_at_period_end
        ? `Cancelled — your services stop on <strong>${esc(fmtDate(a.renews_at))}</strong>.`
        : `Paying ${interval === 'year' ? 'yearly' : 'monthly'} · renews <strong>${esc(fmtDate(a.renews_at))}</strong>`}</p>`
        : '<p>You are not paying online yet. One subscription covers every service below, on one invoice.</p>'}
      <div class="table-wrap"><table>
        <thead><tr><th>Service</th><th>Package</th><th>Status</th><th class="num">Price</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="muted">No services.</td></tr>'}</tbody>
        <tfoot><tr><td colspan="3"><strong>Total</strong></td><td class="num"><strong>${money(a.monthly_total_cents)}</strong><span class="muted">/mo${interval === 'year' ? ' (billed yearly)' : ''}</span></td></tr></tfoot>
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
            ${i.invoice_pdf ? `<a class="btn sm" href="${esc(i.invoice_pdf)}" target="_blank" rel="noopener">PDF</a>` : ''}</td></tr>`).join('')}</tbody></table></div>`
        : empty('No Invoices Yet', '')}
    </div>`;
  $$('[data-interval]', mount).forEach((b) => b.addEventListener('click', () => guard(async () => {
    await api.post('/portal/billing/interval', { interval: b.dataset.interval });
    renderBilling(mount, { query: {} });
  })));
  $('#payBtn', mount)?.addEventListener('click', () => guard(async () => {
    const r = await api.post('/portal/billing/checkout', { interval });
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
    if (ok) { toast('Subscription cancelled.', 'ok'); renderBilling(mount, { query: {} }); }
  });
  $('#resumeBtn', mount)?.addEventListener('click', () => guard(async () => {
    await api.post('/portal/billing/resume');
    renderBilling(mount, { query: {} });
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
      <div class="card-head"><h2>People</h2><div class="actions">${canManage() ? '<button class="primary" id="invite">Invite Someone</button>' : ''}</div></div>
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
      title: 'Invite Someone', submitLabel: 'Send Invitation',
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
  mount.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Company</h2></div>
      <form id="company">
        ${field({ name: 'name', label: 'Company name', value: a.name, required: true })}
        ${field({ name: 'country', label: 'Country', value: a.country || '' })}
        ${field({ name: 'timezone', label: 'Timezone', value: a.timezone || 'UTC' })}
        ${canManage() ? '<button class="primary">Save</button>' : ''}
      </form>
    </div>
    <div class="card" style="margin-top:14px">
      <div class="card-head"><h2>Your Password</h2></div>
      <form id="pw">
        ${field({ name: 'current_password', label: 'Current password', type: 'password', required: true })}
        ${field({ name: 'new_password', label: 'New password', type: 'password', required: true,
          help: 'At least 10 characters, with upper and lower case letters and a number.' })}
        <button>Change Password</button>
      </form>
    </div>
    ${isOwner() ? `<div class="card danger-zone" style="margin-top:14px">
      <div class="card-head"><h2>Delete Account</h2></div>
      <p>Deletes ${esc(a.name)}, every service's data for it, and its subscription. This can't be undone.</p>
      <button class="danger-outline" id="delete">Delete Account</button>
    </div>` : ''}`;
  $('#company', mount).addEventListener('submit', (e) => {
    e.preventDefault();
    guard(async () => { state.account = await api.patch('/portal/account', readForm(e.target)); }, 'Saved.');
  });
  $('#pw', mount).addEventListener('submit', (e) => {
    e.preventDefault();
    guard(async () => { await api.post('/auth/change-password', readForm(e.target)); e.target.reset(); }, 'Password changed.');
  });
  $('#delete', mount)?.addEventListener('click', async () => {
    const ok = await dialog({
      title: 'Delete This Account?', danger: true, submitLabel: 'Delete Everything',
      body: `<p>Type <strong>${esc(a.name)}</strong> to confirm.</p>
        ${field({ name: 'confirm_name', label: 'Company name', required: true })}
        ${field({ name: 'reason', label: 'Why are you leaving?', type: 'textarea' })}`,
      onSubmit: (form) => api.post('/portal/account/delete', readForm(form)),
    });
    if (ok) { await signOut(); window.location.href = '/'; }
  });
}

/* =========================================================================
 * Router
 * ====================================================================== */
const PAGES = [
  { path: '/services', title: 'Services', icon: 'grid', render: renderServices },
  { path: '/billing', title: 'Billing', icon: 'card', render: renderBilling },
  { path: '/team', title: 'People', icon: 'people', render: renderTeam },
  { path: '/account', title: 'Account', icon: 'settings', render: renderAccount },
];

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
    const page = PAGES.find((p) => p.path === r.path);
    await loadAccount();
    mountShell({
      nav: [{ items: PAGES }],
      user: state.me.email,
      pill: `<span class="pill">${esc(state.account.name)}</span>`,
      onSignOut: async () => { await signOut(); unmountShell(); window.location.hash = '#/login'; },
    });
    $('#pageTitle').textContent = page ? page.title : 'Not Found';
    const content = $('#content');
    const fresh = content.cloneNode(false);   // drop listeners from the last page
    content.replaceWith(fresh);
    if (!page) { fresh.innerHTML = empty('Page Not Found', ''); return; }
    try {
      await page.render(fresh, r);
    } catch (error) {
      if (error.status !== 401) fresh.innerHTML = banner('Could Not Load This Page', error.message, 'bad');
    }
  } finally {
    running = false;
    if (again) { again = false; route(); }
  }
}

window.addEventListener('ob:signed-out', () => { unmountShell(); window.location.hash = '#/login'; route(); });
window.addEventListener('hashchange', route);
wireTips();
route();
