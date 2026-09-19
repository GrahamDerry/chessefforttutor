import { renderBoard, fmtClock } from '/static/board.js';

const el = id => document.getElementById(id);
const seen = [];                 // scenario ids this session, so we don't repeat
let current = null, shownAt = 0, answered = false;
let done = 0, right = 0;

async function load() {
  answered = false;
  el('reveal').classList.add('hidden');
  setButtons(true);
  const q = seen.length ? `?exclude=${seen.slice(-40).join(',')}` : '';
  const res = await fetch(`/api/drill/next${q}`);
  if (!res.ok) {
    el('sub').textContent = (await res.json()).detail || 'No positions available.';
    setButtons(false);
    return;
  }
  current = await res.json();
  seen.push(current.scenario_id);

  renderBoard(el('board'), current.fen, current.user_color);
  el('clock').textContent = fmtClock(current.clock_before);
  const yourMove = current.side_to_move === current.user_color;
  el('tomove').innerHTML =
    `<span class="dot ${current.side_to_move}"></span> ${yourMove ? 'You' : 'Opponent'} to move` +
    ` &middot; move ${current.move_number}`;
  const tc = current.increment
    ? `${current.base_seconds / 60}|${current.increment}` : `${current.base_seconds / 60}|0`;
  el('sub').textContent = `You are ${current.user_color}. ${tc} blitz. This is the clock you had.`;
  shownAt = performance.now();
}

function setButtons(on) {
  el('btn-long').disabled = !on;
  el('btn-short').disabled = !on;
}

async function answer(choice) {
  if (answered || !current) return;
  answered = true;
  setButtons(false);
  const res = await fetch('/api/drill/answer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scenario_id: current.scenario_id,
      answer: choice,
      response_ms: Math.round(performance.now() - shownAt),
    }),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail || detail; } catch {}
    showError(`Could not record that answer — ${detail}`);
    return;
  }
  reveal(await res.json());
}

function showError(msg) {
  const v = el('verdict');
  v.className = 'verdict no';
  v.textContent = 'Something went wrong';
  el('explanation').textContent = msg;
  el('facts').innerHTML = '';
  el('gamelink').style.display = 'none';
  el('reveal').classList.remove('hidden');
  answered = true;
}

function reveal(r) {
  done++; if (r.correct) right++;
  el('session').textContent = `${right} / ${done}`;

  const v = el('verdict');
  v.className = 'verdict ' + (r.correct ? 'ok' : 'no');
  v.textContent = r.correct
    ? `Correct — think ${r.ground_truth.toLowerCase()}.`
    : `Not quite — this was think ${r.ground_truth.toLowerCase()}.`;
  el('explanation').textContent = r.explanation;

  const pct = x => x == null ? '—' : `${(x * 100).toFixed(0)}%`;
  const num = x => x == null ? '—' : x.toFixed(2);
  const rows = [
    ['You played', `${r.move_played_san} in ${r.seconds_spent?.toFixed(1) ?? '?'}s`],
    ['Engine prefers', r.pv_san?.length ? r.pv_san.join(' ') : (r.best_move_san ?? '—')],
    ['Cost', r.e_loss != null ? `${num(r.e_loss)} expected points` : '—'],
    ['Criticality', num(r.criticality) + (r.obvious ? ' (obvious move)' : '')],
  ];
  if (r.commitment != null) rows.push(['Commitment', num(r.commitment)]);
  rows.push(['Scenario', `${r.kind} · ${r.phase ?? '—'}`]);
  rows.push(['Game result', r.result_user === 1 ? 'you won'
    : r.result_user === 0 ? 'you lost' : 'draw']);

  el('facts').innerHTML = rows
    .map(([k, val]) => `<dt>${k}</dt><dd>${val}</dd>`).join('');
  el('gamelink').style.display = '';
  el('gamelink').href = r.game_url || '#';
  el('reveal').classList.remove('hidden');
  el('next').focus();
}

el('btn-long').onclick = () => answer('LONG');
el('btn-short').onclick = () => answer('SHORT');
el('next').onclick = load;
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  const k = e.key.toLowerCase();
  if (!answered && k === 'l') answer('LONG');
  else if (!answered && k === 's') answer('SHORT');
  else if (answered && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); load(); }
});

load();
