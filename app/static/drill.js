import { renderBoard, fmtClock } from '/static/board.js';

const el = id => document.getElementById(id);
const seen = [];                 // scenario ids this session, so we don't repeat
const REPLAY_MS = 1000;          // pause per half-move while replaying the lead-in
let current = null, shownAt = 0, answered = false;
let done = 0, right = 0;
// Lead-in replay state. `replayToken` invalidates timers from a skipped or superseded replay.
let replaying = false, replayTimer = null, replayToken = 0, timerStarted = false;

async function load() {
  answered = false;
  timerStarted = false;
  stopReplay();
  el('reveal').classList.add('hidden');
  el('replay').classList.add('hidden');
  el('moves').textContent = '';
  setButtons(false);
  const q = seen.length ? `?exclude=${seen.slice(-40).join(',')}` : '';
  const res = await fetch(`/api/drill/next${q}`);
  if (!res.ok) {
    el('sub').textContent = (await res.json()).detail || 'No positions available.';
    return;
  }
  current = await res.json();
  seen.push(current.scenario_id);
  playHistory();
}

// ---------------------------------------------------------------- lead-in replay

const squares = uci => uci ? [uci.slice(0, 2), uci.slice(2, 4)] : [];

// "12. Nf3 Bb4 13. O-O" for the plies played so far; the latest move is emphasised.
function moveList(upto) {
  const hist = current.history;
  const parts = [];
  for (let i = 0; i < upto; i++) {
    const h = hist[i];
    const white = h.ply % 2 === 1;
    if (white) parts.push(`${(h.ply + 1) / 2}.`);
    else if (i === 0) parts.push(`${h.ply / 2}...`);
    parts.push(i === upto - 1 ? `<span class="cur">${h.san}</span>` : h.san);
  }
  return parts.join(' ');
}

// Draw frame `i`: the position before history[i] (or the drill position when i === n),
// highlighting the move that produced it.
function frame(i) {
  const hist = current.history;
  const fen = i < hist.length ? hist[i].fen : current.fen;
  const last = i > 0 ? squares(hist[i - 1].uci) : [];
  renderBoard(el('board'), fen, current.user_color, last);
  el('moves').innerHTML = moveList(i);
}

function playHistory() {
  const hist = current.history || [];
  if (!hist.length) { frame(0); landed(); return; }

  replaying = true;
  const token = ++replayToken;
  setButtons(false);
  el('board').classList.add('replaying');
  el('replay').classList.add('hidden');
  el('clock').textContent = '--:--';
  el('tomove').textContent = '';
  const n = hist.length;
  el('sub').textContent =
    `Replaying the last ${n} half-move${n === 1 ? '' : 's'}… press any key to skip.`;

  let i = 0;
  const step = () => {
    if (token !== replayToken) return;
    frame(i);
    if (i === n) { landed(); return; }
    i++;
    replayTimer = setTimeout(step, REPLAY_MS);
  };
  step();
}

function stopReplay() {
  replayToken++;
  clearTimeout(replayTimer);
  replayTimer = null;
  replaying = false;
  el('board').classList.remove('replaying');
}

function skipReplay() {
  if (!replaying) return;
  stopReplay();
  frame(current.history.length);
  landed();
}

function replayAgain() {
  if (!current || replaying) return;
  playHistory();
}

// The drill position is on the board: show the clock and start the response timer.
function landed() {
  stopReplay();
  setButtons(!answered);
  el('replay').classList.toggle('hidden', !(current.history || []).length);
  el('clock').textContent = fmtClock(current.clock_before);
  const yourMove = current.side_to_move === current.user_color;
  el('tomove').innerHTML =
    `<span class="dot ${current.side_to_move}"></span> ${yourMove ? 'You' : 'Opponent'} to move` +
    ` &middot; move ${current.move_number}`;
  const tc = current.increment
    ? `${current.base_seconds / 60}|${current.increment}` : `${current.base_seconds / 60}|0`;
  el('sub').textContent = `You are ${current.user_color}. ${tc} blitz. This is the clock you had.`;
  if (!timerStarted) { shownAt = performance.now(); timerStarted = true; }
}

function setButtons(on) {
  el('btn-long').disabled = !on;
  el('btn-short').disabled = !on;
}

async function answer(choice) {
  if (answered || !current || replaying) return;
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
el('replay').onclick = replayAgain;
el('board').onclick = skipReplay;
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (replaying) { e.preventDefault(); skipReplay(); return; }
  const k = e.key.toLowerCase();
  if (!answered && k === 'l') answer('LONG');
  else if (!answered && k === 's') answer('SHORT');
  else if (k === 'r') replayAgain();
  else if (answered && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); load(); }
});

load();
