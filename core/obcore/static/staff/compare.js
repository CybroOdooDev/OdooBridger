/* "▲ 12% vs previous 30 days": the comparison line under a figure, shared by the overview and Leads. */

import { esc } from '/assets/ui.js';

export const pct = (v) => (v == null ? '—' : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`);

/** "▲ 12% vs previous 30 days". ``upIsGood`` decides the colour; ``fmt`` formats the
 *  numbers shown when the earlier figure was 0 (a percentage of 0 means nothing). */
export function compare(m, upIsGood = true, fmt = (v) => String(v)) {
  if (!m || !m.basis) return '';
  if (m.change_pct == null) {
    return m.previous === 0 && m.value
      ? `<span class="cmp up ${upIsGood ? 'good' : 'bad'}">+${esc(fmt(m.value))}</span> <span class="cmp-b">vs ${esc(fmt(0))} ${esc(m.basis)}</span>` : '';
  }
  if (m.change_pct === 0) return `<span class="cmp flat">No change</span> <span class="cmp-b">vs ${esc(m.basis)}</span>`;
  const up = m.change_pct > 0;
  return `<span class="cmp ${up ? 'up' : 'down'} ${up === upIsGood ? 'good' : 'bad'}">${up ? '▲' : '▼'} ${pct(Math.abs(m.change_pct))}</span> <span class="cmp-b">vs ${esc(m.basis)}</span>`;
}
