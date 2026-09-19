// Minimal static board renderer. No dependencies — Unicode pieces on a CSS grid.
const GLYPH = { k: '♚', q: '♛', r: '♜', b: '♝', n: '♞', p: '♟' };
const FILES = 'abcdefgh';

export function renderBoard(el, fen, orientation = 'white') {
  const rows = fen.split(' ')[0].split('/');
  const grid = [];
  for (const row of rows) {
    const cells = [];
    for (const ch of row) {
      if (/\d/.test(ch)) { for (let i = 0; i < +ch; i++) cells.push(null); }
      else cells.push(ch);
    }
    grid.push(cells);
  }
  if (orientation === 'black') { grid.reverse(); grid.forEach(r => r.reverse()); }

  el.innerHTML = '';
  for (let r = 0; r < 8; r++) {
    for (let f = 0; f < 8; f++) {
      const rank = orientation === 'white' ? 8 - r : r + 1;
      const file = orientation === 'white' ? FILES[f] : FILES[7 - f];
      const sq = document.createElement('div');
      sq.className = 'sq ' + ((r + f) % 2 === 0 ? 'l' : 'd');
      const piece = grid[r][f];
      if (piece) {
        const span = document.createElement('span');
        span.className = 'p ' + (piece === piece.toUpperCase() ? 'w' : 'b');
        span.textContent = GLYPH[piece.toLowerCase()];
        sq.appendChild(span);
      }
      if (f === 0 || r === 7) {
        const c = document.createElement('span');
        c.className = 'coord';
        c.textContent = f === 0 && r === 7 ? file + rank : (f === 0 ? rank : file);
        sq.appendChild(c);
      }
      el.appendChild(sq);
    }
  }
}

export function fmtClock(sec) {
  if (sec == null) return '--:--';
  const m = Math.floor(sec / 60), s = sec - m * 60;
  return `${m}:${(s < 10 ? '0' : '')}${s.toFixed(1)}`;
}
