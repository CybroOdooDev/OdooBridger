/* A dropdown that picks several things: a button showing the choice, a list of
 * checkboxes below it. Plain <details>, so it works from the keyboard. */

import { esc } from '/assets/ui.js';

export function multiSelect(root, items, { placeholder = 'Select one or more', selected = [], onChange } = {}) {
  const chosen = new Set(selected);
  root.innerHTML = `<details class="ms">
    <summary><span class="ms-text"></span>
      <svg viewBox="0 0 12 8" width="12" height="8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m1 1.5 5 5 5-5"/></svg></summary>
    <div class="ms-list">${items.map((i) => `<label><input type="checkbox" value="${esc(i.value)}"${chosen.has(i.value) ? ' checked' : ''}>
      <span><b>${esc(i.label)}</b>${i.hint ? `<small>${esc(i.hint)}</small>` : ''}</span></label>`).join('')}</div></details>`;
  const details = root.querySelector('details');
  const text = root.querySelector('.ms-text');
  const paint = () => {
    const names = items.filter((i) => chosen.has(i.value)).map((i) => i.label);
    text.textContent = names.length ? names.join(', ') : placeholder;
    text.classList.toggle('placeholder', !names.length);
  };
  root.querySelectorAll('input').forEach((box) => box.addEventListener('change', () => {
    box.checked ? chosen.add(box.value) : chosen.delete(box.value);
    paint();
    if (onChange) onChange([...chosen]);
  }));
  document.addEventListener('click', (e) => { if (!root.contains(e.target)) details.open = false; });
  details.addEventListener('keydown', (e) => { if (e.key === 'Escape') { details.open = false; details.querySelector('summary').focus(); } });
  paint();
  return { values: () => [...chosen] };
}
