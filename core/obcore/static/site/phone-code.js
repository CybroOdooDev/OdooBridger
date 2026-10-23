/* A country-code picker you can type into: matches the country name, the
 * dialling code (with or without "+") or the two-letter code. */

import { esc } from '/assets/ui.js';
import { COUNTRIES, flag } from '/site/geo.js';

export function phoneCode(root, initial) {
  let current = initial;
  let shown = COUNTRIES;
  let index = 0;
  root.innerHTML = `<details class="cc">
    <summary aria-label="Country code"><span class="cc-cur"></span>
      <svg viewBox="0 0 12 8" width="10" height="7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m1 1.5 5 5 5-5"/></svg></summary>
    <div class="cc-pop"><input type="search" class="cc-q" placeholder="Search country or code" autocomplete="off" spellcheck="false" aria-label="Search country or code">
      <ul class="cc-list" role="listbox"></ul></div></details>`;
  const details = root.querySelector('details');
  const q = root.querySelector('.cc-q');
  const list = root.querySelector('.cc-list');

  const paintCurrent = () => { root.querySelector('.cc-cur').textContent = `${flag(current[0])} +${current[2]}`; };
  const paintList = () => {
    list.innerHTML = shown.length
      ? shown.map((c, i) => `<li role="option" data-i="${i}" class="${i === index ? 'hot' : ''}${c[0] === current[0] ? ' sel' : ''}">
          <span class="cc-flag">${flag(c[0])}</span><span class="cc-name">${esc(c[1])}</span><span class="cc-dial">+${esc(c[2])}</span></li>`).join('')
      : '<li class="none">No match</li>';
    list.querySelector('.hot')?.scrollIntoView({ block: 'nearest' });
  };
  const filter = () => {
    const t = q.value.trim().toLowerCase().replace(/^\+/, '');
    shown = !t ? COUNTRIES : COUNTRIES.filter((c) => c[1].toLowerCase().includes(t) || c[2].startsWith(t) || c[0].toLowerCase() === t);
    // best matches first: name starts with it, or the code starts with it
    if (t) shown = [...shown].sort((a, b) => Number(b[1].toLowerCase().startsWith(t) || b[2] === t) - Number(a[1].toLowerCase().startsWith(t) || a[2] === t));
    index = 0;
    paintList();
  };
  const pick = (c) => { current = c; paintCurrent(); details.open = false; };

  q.addEventListener('input', filter);
  q.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { index = Math.min(index + 1, shown.length - 1); paintList(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { index = Math.max(index - 1, 0); paintList(); e.preventDefault(); }
    else if (e.key === 'Enter') { if (shown[index]) pick(shown[index]); e.preventDefault(); }
    else if (e.key === 'Escape') { details.open = false; details.querySelector('summary').focus(); }
  });
  list.addEventListener('click', (e) => { const li = e.target.closest('li[data-i]'); if (li) pick(shown[Number(li.dataset.i)]); });
  details.addEventListener('toggle', () => {
    if (details.open) { q.value = ''; filter(); q.focus(); const sel = shown.findIndex((c) => c[0] === current[0]); if (sel >= 0) { index = sel; paintList(); } }
  });
  document.addEventListener('click', (e) => { if (!root.contains(e.target)) details.open = false; });
  paintCurrent();
  return { dial: () => `+${current[2]}` };
}
