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
    <h1>${info.signup ? 'Create Your Account' : first ? 'Choose Your Password' : 'Choose A New Password'}</h1>
    <p class="sub">${info.signup
      ? `Your email is confirmed. Choose a password for ${esc(info.email)} to create ${esc(info.account)}${info.mode === 'buy' ? ', then go on to payment' : ' and start your free trial'}.`
      : first ? `Welcome to OdooBridger. Your email is confirmed — set a password for ${esc(info.email)} (${esc(info.account)}).`
      : `For ${esc(info.email)}.`}</p>
    <form id="form">
      ${field({ name: 'password', label: 'New password', type: 'password', required: true,
        help: 'At least 10 characters, with upper and lower case letters and a number.' })}
      ${field({ name: 'confirm', label: 'Repeat it', type: 'password', required: true })}
      <button class="primary" style="width:100%" id="go">${info.signup ? (info.mode === 'buy' ? 'Create Account And Pay' : 'Create Account') : first ? 'Save And Sign In' : 'Save Password'}</button>
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
            ${i.invoice_pdf ? `<a class="btn sm" href="${esc(i.invoice_pdf)}" target="_blank" rel="noopener">PDF</a>` : ''}</td></tr>`).join('')}</tbody></table></div></div>`
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
 * Odoo: the account's one connection, used by every Odoo service
 * ====================================================================== */
const ODOO_STATUS = {
  connected: ['ok', 'Connected'],
  failed: ['bad', 'Not Working'],
  unverified: ['mute', 'Not Checked'],
};
const odooPill = (s) => `<span class="pill ${ODOO_STATUS[s]?.[0] || 'mute'}">${esc(ODOO_STATUS[s]?.[1] || s)}</span>`;
const readyPill = (r) => (r === true ? '<span class="pill ok">Ready</span>'
  : r === false ? '<span class="pill bad">Not Ready</span>' : '<span class="pill mute">Not Checked</span>');

function routeField(agents, conn = null) {
  const options = [{ value: '', label: 'Over the internet (a public address)' },
    ...agents.map((a) => ({ value: a.id, label: `Through the agent "${a.name}" (a private network)` }))];
  return field({ name: 'agent_id', label: 'How OdooBridger reaches it', options, value: conn?.agent?.id || '',
    help: agents.length ? '' : 'For an Odoo on a private network, add an OdooBridger Agent below first.' });
}

function odooFields(conn = null, agents = [], route = true, mode = null) {
  const urlHelp = mode === 'public' ? 'The address you open Odoo at, e.g. https://yourcompany.odoo.com.'
    : mode === 'private' ? 'The address as your own network sees it, e.g. http://192.168.1.20:8069.'
      : 'Through an agent, use the address as your own network sees it, e.g. http://192.168.1.20:8069.';
  return `
    ${route ? routeField(agents, conn) : ''}
    ${field({ name: 'url', label: 'Odoo address', required: true, value: conn?.url || '',
      placeholder: mode === 'private' ? 'http://192.168.1.20:8069' : 'https://yourcompany.odoo.com', help: urlHelp })}
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
  values.agent_id = values.agent_id || null;
  return values;
}

function odooCard(conn) {
  const companies = (conn.companies || []).map((c) => esc(c.name)).join(', ') || '—';
  const facts = [
    ['Address', `<a href="${esc(conn.url)}" target="_blank" rel="noopener">${esc(conn.url)}</a>`],
    ['Database', esc(conn.db)],
    ['Login', esc(conn.login)],
    ['Route', conn.agent ? `Through the agent ${esc(conn.agent.name)} ${conn.agent.online ? '<span class="pill ok">Online</span>' : '<span class="pill bad">Offline</span>'}` : 'Over the internet'],
    ['Version', conn.version ? `${esc(conn.version)} · ${conn.api === 'json2' ? 'JSON-2 API' : 'XML-RPC'}` : '—'],
    ['Companies', companies],
    ['Last checked', conn.last_checked_at ? esc(fmtDate(conn.last_checked_at)) : '—'],
    conn.key_expires_at ? ['Key expires', esc(fmtDate(conn.key_expires_at))] : null,
  ].filter(Boolean);
  return `
    <div class="card">
      <div class="card-head"><h2>${esc(conn.name)}</h2>${odooPill(conn.status)}</div>
      ${conn.status === 'failed' && conn.status_message ? banner('Odoo Is Refusing The Connection', conn.status_message, 'bad') : ''}
      <dl class="svc-facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      <div class="svc-actions" style="margin-top:14px">
        <button data-odoo-test="${esc(conn.id)}">Test Connection</button>
        ${canManage() ? `<button data-odoo-edit="${esc(conn.id)}">Change</button>
          <button class="danger-outline" data-odoo-remove="${esc(conn.id)}">Disconnect</button>` : ''}
      </div>
    </div>
    <div class="section-title">Services Using This Odoo</div>
    ${conn.services.length ? `<div class="card"><div class="table-wrap"><table><thead><tr><th>Service</th><th>Readiness</th><th>Details</th></tr></thead><tbody>
      ${conn.services.map((s) => `<tr>
        <td>${esc(s.name)}</td>
        <td>${readyPill(s.ready)}</td>
        <td>${esc(s.message || (s.in_sync ? '' : 'Waiting for the service to pick up the change.'))}
          ${(s.checks || []).length ? `<div class="checks">${s.checks.map((c) => `<span class="check ${c.ok ? 'ok' : c.optional ? 'opt' : 'bad'}">${c.ok ? '✓' : c.optional ? '–' : '✕'} ${esc(c.name)}</span>`).join('')}</div>` : ''}</td>
      </tr>`).join('')}</tbody></table></div></div>`
    : `<div class="card">${empty('No Services Use Odoo Yet', 'Services that work with Odoo connect to it automatically when you add them.')}</div>`}`;
}


/* --- Guided setup for an Odoo on a private network (all in the portal) ------- */
function agentCommands(key, relayUrl) {
  const base = window.location.origin;
  const docker = `docker run -d --name odoobridger-agent --restart unless-stopped --network host \\\n  -e OB_AGENT_KEY=${key} \\\n  -e OB_RELAY_URL=${relayUrl} \\\n  python:3.12-slim sh -c "pip install -q websockets httpx && python -c \\"import urllib.request as u; u.urlretrieve('${base}/downloads/odoobridger_agent.py','/a.py')\\" && python /a.py"`;
  const python = `pip install websockets httpx\ncurl -O ${base}/downloads/odoobridger_agent.py\nexport OB_AGENT_KEY=${key}\nexport OB_RELAY_URL=${relayUrl}\npython3 odoobridger_agent.py`;
  return { docker, python };
}

/** Three steps without leaving the page: your Odoo's details, run the agent
 * (we watch for it to come online), then the connection is tested and saved. */
function privateWizard(box, agents, onDone) {
  const state = { values: null, agent: null, created: null };
  const alive = () => document.body.contains(box);

  const stepDetails = () => {
    box.innerHTML = `
      <p class="muted" style="margin-top:0">Your Odoo is only reachable inside your own network. You will run a small program there that opens one outgoing connection to OdooBridger. Nothing is opened inbound.</p>
      <form id="privForm">${odooFields(null, [], false, 'private')}
        <p class="err" id="privErr"></p>
        <button class="primary" id="privGo">Continue</button></form>`;
    $('#privForm', box).addEventListener('submit', (e) => {
      e.preventDefault();
      const values = odooValues(e.target, null);
      const err = $('#privErr', box);
      let origin;
      try { origin = new URL(/^https?:\/\//.test(values.url) ? values.url : `http://${values.url}`); } catch { origin = null; }
      if (!origin || !values.url) { err.textContent = 'Enter your Odoo address, for example http://192.168.1.20:8069.'; return; }
      values.url = `${origin.protocol}//${origin.host}`;
      state.values = values;
      submitting($('#privGo', box), async () => {
        try {
          const ready = agents.find((a) => a.online && !a.outdated);
          if (ready) { state.agent = ready; await stepConnect(); return; }
          const created = await api.post('/portal/agents', { name: `${origin.hostname} agent` });
          state.created = created; state.agent = created.agent;
          stepRun();
        } catch (error) { err.textContent = error.message; }
      });
    });
  };

  const stepRun = () => {
    const { key, relay_url: relayUrl } = state.created;
    const cmds = agentCommands(key, relayUrl);
    box.innerHTML = `
      <div class="section-title" style="margin-top:0">Run The Agent On Your Network</div>
      <p>Run this on any computer in the same network as your Odoo, such as the Odoo server itself. It only needs its key: we add your Odoo's address to it for you.</p>
      <div class="section-title">With Docker</div><pre class="code">${esc(cmds.docker)}</pre>
      <p class="hint">On Docker Desktop (Windows or Mac) remove <code>--network host</code>.</p>
      <div class="section-title">With Python</div><pre class="code">${esc(cmds.python)}</pre>
      <p class="hint">That is the only thing to run. What the agent can reach is managed here: connecting your Odoo or a biometric device adds its address, and changes reach the running agent straight away.</p>
      <div class="banner" id="waitBox" style="margin-top:14px"><span id="waitText">Waiting for the agent to connect…</span></div>
      <p class="err" id="privErr"></p>
      <div class="row" style="gap:8px;margin-top:10px"><button id="privBack">Back</button></div>`;
    $('#privBack', box).addEventListener('click', () => { state.stop = true; stepDetails(); });
    state.stop = false;
    const poll = async () => {
      if (!alive() || state.stop || !box.querySelector('#waitBox')) return;
      try {
        const { agents: list } = await api.get('/portal/agents');
        const me = list.find((a) => a.id === state.agent.id);
        if (me?.online) {
          if (me.outdated) {
            $('#waitText', box).textContent = 'The agent is online but is an older version. Restart it so it downloads the latest; this page carries on by itself.';
          } else { state.agent = me; await stepConnect(); return; }
        }
      } catch { /* try again */ }
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  };

  const stepConnect = async () => {
    box.innerHTML = `<p>Agent online. Testing your Odoo through it…</p>${loading()}`;
    try {
      await api.post('/portal/odoo', { ...state.values, agent_id: state.agent.id });
    } catch (error) {
      box.innerHTML = `${banner('The Connection Test Failed', error.message, 'bad')}
        <div class="row" style="gap:8px;margin-top:10px"><button class="primary" id="privRetry">Try Again</button>
        <button id="privBack">Change Details</button></div>`;
      $('#privRetry', box).addEventListener('click', stepConnect);
      $('#privBack', box).addEventListener('click', stepDetails);
      return;
    }
    toast('Odoo connected.', 'ok');
    onDone();
  };

  stepDetails();
}

/* --- OdooBridger Agents: reaching an Odoo on a private network ------------- */
function agentSetup(key, relayUrl) {
  const cmds = agentCommands(key, relayUrl);
  return `
    <p>Copy the key now. It is shown only once.</p>
    <div class="secret-box">${esc(key)}</div>
    <p style="margin-top:14px">Run the agent on any computer in the same network as your Odoo or your biometric devices.</p>
    <div class="section-title">With Docker</div><pre class="code">${esc(cmds.docker)}</pre>
    <p class="hint">On Docker Desktop (Windows or Mac) remove <code>--network host</code>.</p>
    <div class="section-title">With Python</div><pre class="code">${esc(cmds.python)}</pre>
    <p class="hint">It connects out to OdooBridger over an encrypted connection. Nothing needs to be opened inbound.</p>
    <p class="hint">That is the only thing to run. What the agent can reach is managed here: connecting your Odoo or a biometric device adds its address, and changes reach the running agent straight away.</p>`;
}

function agentAddresses(a) {
  const list = (a.allowed || []).map((x) => `<span class="addr"><code>${esc(x)}</code>${canManage()
    ? `<button class="link sm" data-addr-remove="${esc(a.id)}" data-addr="${esc(x)}" aria-label="Remove ${esc(x)}">×</button>` : ''}</span>`).join(' ');
  return `${list || '<span class="muted">None yet: added when you connect Odoo or a device</span>'}
    ${(a.limited_to || []).length ? `<div class="hint">Limited on its computer to ${a.limited_to.map((x) => `<code>${esc(x)}</code>`).join(', ')}</div>` : ''}`;
}

function agentsCard(data) {
  const rows = data.agents.map((a) => `<tr>
      <td><strong>${esc(a.name)}</strong><div class="hint">key …${esc(a.key_hint)}${a.hostname ? ` · on ${esc(a.hostname)}` : ''}${a.version ? ` · v${esc(a.version)}` : ''}</div></td>
      <td>${a.online ? (a.outdated ? '<span class="pill warn">Update Needed</span>' : '<span class="pill ok">Online</span>') : '<span class="pill bad">Offline</span>'}
        <div class="hint">${a.outdated ? 'Restart it to get the latest version.' : a.online ? `since ${esc(fmtDate(a.connected_at))}` : a.last_seen_at ? `last seen ${esc(fmtDate(a.last_seen_at))}` : 'never connected'}</div></td>
      <td>${agentAddresses(a)}</td>
      <td class="right nowrap">${canManage() ? `<button class="sm" data-addr-add="${esc(a.id)}">Add Address</button>
        <button class="sm" data-agent-key="${esc(a.id)}">New Key</button>
        <button class="sm danger-outline" data-agent-remove="${esc(a.id)}">Remove</button>` : ''}</td>
    </tr>`).join('');
  return `
    <div class="section-title">OdooBridger Agents</div>
    <div class="card">
      <div class="card-head"><h2>Reach Your Private Network</h2>
        ${canManage() ? '<button class="primary sm" id="agentAdd">Add Agent</button>' : ''}</div>
      <p class="muted" style="margin-top:0">An agent runs inside your network and opens one outgoing connection to OdooBridger, so we can reach your Odoo and biometric devices there. No port forwarding or VPN. It only ever reaches the addresses listed here, inside your own network.</p>
      ${data.agents.length ? `<div class="table-wrap"><table><thead><tr><th>Agent</th><th>Status</th><th>Can reach</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : empty('No Agents Yet', 'Add one if your Odoo or devices are only reachable inside your own network.')}
    </div>`;
}

/** Wires "Add Agent"; returns the click handler for the rows (set on mount.onclick,
 * so re-rendering never stacks listeners). */
function wireAgents(mount, data, refresh) {
  $('#agentAdd', mount)?.addEventListener('click', () => guard(async () => {
    const created = await dialog({
      title: 'Add An OdooBridger Agent', submitLabel: 'Create Agent',
      body: field({ name: 'name', label: 'Name', value: 'Office server', required: true }),
      onSubmit: (form) => api.post('/portal/agents', readForm(form)),
    });
    if (!created) return;
    await dialog({ title: 'Set Up The Agent', wide: true, submitLabel: 'Done', body: agentSetup(created.key, created.relay_url) });
    refresh();
  }));
  return (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.agentKey) {
      guard(async () => {
        const done = await dialog({
          title: 'Issue A New Key?', submitLabel: 'New Key',
          body: '<p>The old key stops working at once and the agent disconnects until you give it the new key.</p>',
          onSubmit: () => api.post(`/portal/agents/${b.dataset.agentKey}/key`),
        });
        if (!done) return;
        await dialog({ title: 'The New Key', wide: true, submitLabel: 'Done', body: agentSetup(done.key, done.relay_url) });
        refresh();
      });
    }
    if (b.dataset.addrAdd) {
      guard(async () => {
        const done = await dialog({
          title: 'Add An Address', submitLabel: 'Add',
          body: field({ name: 'address', label: 'Address', required: true, placeholder: '192.168.1.50:80',
            help: 'An address on your network with its port, such as 192.168.1.50:80 for a web terminal or 10.0.11.43:4370 for a ZKTeco terminal. A range works too: 192.168.1.0/24@80+443.' }),
          onSubmit: (form) => api.post(`/portal/agents/${b.dataset.addrAdd}/addresses`, readForm(form)),
        });
        if (done) { toast('Address added. The agent has it now.', 'ok'); refresh(); }
      });
    }
    if (b.dataset.addrRemove) {
      guard(async () => {
        await api.del(`/portal/agents/${b.dataset.addrRemove}/addresses?address=${encodeURIComponent(b.dataset.addr)}`);
        toast('Address removed.', 'ok');
        refresh();
      });
    }
    if (b.dataset.agentRemove) {
      guard(async () => {
        const done = await dialog({
          title: 'Remove This Agent?', danger: true, submitLabel: 'Remove',
          body: '<p>Its key stops working and it disconnects. You can then uninstall it.</p>',
          onSubmit: () => api.del(`/portal/agents/${b.dataset.agentRemove}`),
        });
        if (done) { toast('Agent removed.', 'ok'); refresh(); }
      });
    }
  };
}

async function renderOdoo(mount) {
  mount.innerHTML = loading();
  const [data, relay] = await Promise.all([api.get('/portal/odoo'), api.get('/portal/agents')]);
  const conn = data.connections[0] || null;
  const agents = relay.agents;
  const refresh = () => renderOdoo(mount);
  const waiting = data.services_without.map((s) => esc(s.name)).join(', ');
  if (!conn) {
    mount.innerHTML = `
      ${waiting ? banner('Connect Your Odoo', `${waiting} ${data.services_without.length > 1 ? 'need' : 'needs'} it to work.`, 'warn') : ''}
      <div class="card">
        <div class="card-head"><h2>Connect Your Odoo</h2></div>
        ${canManage() ? `<div class="row" style="gap:8px;margin-bottom:14px">
            <button class="primary" data-route="public">Public Address</button>
            <button data-route="private">Private Network</button></div>
          <div id="routeBox"></div>`
          : '<p class="muted">Ask your account owner or an admin to connect Odoo.</p>'}
      </div>
      ${agentsCard(relay)}`;
    mount.onclick = wireAgents(mount, relay, refresh);
    const box = $('#routeBox', mount);
    const showPublic = () => {
      box.innerHTML = `<form id="odooForm">${odooFields(null, [], false, 'public')}
        <p class="err" id="odooErr"></p>
        <button class="primary" id="odooGo">Test And Connect</button></form>`;
      $('#odooForm', box).addEventListener('submit', (e) => {
        e.preventDefault();
        $('#odooErr', box).textContent = '';
        submitting($('#odooGo', box), async () => {
          try {
            await api.post('/portal/odoo', odooValues(e.target, null));
          } catch (error) {
            $('#odooErr', box).textContent = error.message;
            return;
          }
          toast('Odoo connected.', 'ok');
          renderOdoo(mount);
        });
      });
    };
    const choose = (route) => {
      mount.querySelectorAll('[data-route]').forEach((b) => b.classList.toggle('primary', b.dataset.route === route));
      if (route === 'private') privateWizard(box, agents, refresh); else showPublic();
    };
    mount.querySelectorAll('[data-route]').forEach((b) => b.addEventListener('click', () => choose(b.dataset.route)));
    if (box) choose('public');
    return;
  }
  mount.innerHTML = odooCard(conn) + agentsCard(relay);
  const agentClick = wireAgents(mount, relay, refresh);
  mount.onclick = (e) => {
    agentClick(e);
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
          title: 'Change The Odoo Connection',
          submitLabel: 'Test And Save',
          body: odooFields(conn, agents),
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
  { path: '/services', title: 'Services', icon: 'grid', render: renderServices },
  { path: '/odoo', title: 'Odoo', icon: 'odoo', render: renderOdoo },
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
