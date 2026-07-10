// Utilidades compartidas: formateo, buscador con sugerencias y precios en vivo.

const CURRENCY_SYMBOLS = { USD: '$', EUR: '€', GBP: '£', GBp: 'p' };
const numberFmt = new Intl.NumberFormat('es-ES', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function fmtPrice(value, currency) {
  if (value === null || value === undefined) return '—';
  const symbol = CURRENCY_SYMBOLS[currency] || currency + ' ';
  return `${numberFmt.format(value)} ${symbol}`;
}

function fmtPct(value) {
  const sign = value >= 0 ? '+' : '';
  return `${sign}${numberFmt.format(value)}%`;
}

function flashCell(el) {
  el.classList.remove('tick');
  void el.offsetWidth; // reinicia la animación
  el.classList.add('tick');
}

const MARKET_STATES = {
  open: { label: 'Abierto', cls: 'm-open' },
  pre: { label: 'Pre-market', cls: 'm-pre' },
  post: { label: 'After-hours', cls: 'm-post' },
  closed: { label: 'Cerrado', cls: 'm-closed' },
  unknown: { label: '—', cls: 'm-closed' },
};

function setMarketBadge(el, state) {
  const info = MARKET_STATES[state] || MARKET_STATES.unknown;
  el.innerHTML = '';
  const dot = document.createElement('span');
  dot.className = 'market-dot ' + info.cls;
  const label = document.createElement('span');
  label.textContent = info.label;
  el.append(dot, label);
  el.classList.add('market-badge');
}

function renderMarketBadges() {
  for (const el of document.querySelectorAll('.c-market')) {
    setMarketBadge(el, el.dataset.state);
  }
}

// ---- mensajes flash: se ocultan solos y limpian la URL -------------------

function cleanFlashes() {
  const url = new URL(location);
  if (url.searchParams.has('msg') || url.searchParams.has('err')) {
    url.searchParams.delete('msg');
    url.searchParams.delete('err');
    history.replaceState(null, '', url);
  }
  for (const flash of document.querySelectorAll('.flash.ok, .flash.err')) {
    setTimeout(() => {
      flash.classList.add('fade');
      setTimeout(() => flash.remove(), 600);
    }, 3500);
  }
}

// ---- borrar valores de la lista sin recargar ------------------------------

function setupStockDeletion() {
  for (const btn of document.querySelectorAll('.delete-stock')) {
    btn.addEventListener('click', async () => {
      if (!confirm(`¿Quitar ${btn.dataset.ticker} de la lista?`)) return;
      try {
        const res = await fetch(`/api/stocks/${btn.dataset.id}/delete`, { method: 'POST' });
        const data = await res.json();
        if (!data.ok) return;
      } catch { return; }
      const row = btn.closest('tr');
      const tbody = row.parentElement;
      row.remove();
      if (!tbody.children.length) location.reload(); // mostrar estado vacío
    });
  }
}

// ---- buscador con sugerencias -------------------------------------------

function setupSearch(input, box) {
  if (!input || !box) return;
  let timer = null;

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { box.hidden = true; return; }
    timer = setTimeout(async () => {
      let items = [];
      try {
        const res = await fetch('/api/search?q=' + encodeURIComponent(q));
        items = await res.json();
      } catch { /* sin red: no mostramos nada */ }
      box.innerHTML = '';
      if (items.length === 0) {
        const li = document.createElement('li');
        li.className = 'no-results';
        li.textContent = `Sin resultados para «${q}». Prueba otro nombre o el ticker exacto.`;
        box.appendChild(li);
        box.hidden = false;
        return;
      }
      for (const it of items) {
        const li = document.createElement('li');
        const sym = document.createElement('b');
        sym.textContent = it.symbol;
        const name = document.createElement('span');
        name.textContent = it.name;
        const meta = document.createElement('small');
        meta.textContent = [it.exchange, it.type].filter(Boolean).join(' · ');
        li.append(sym, name, meta);
        // mousedown para ganar al blur del input
        li.addEventListener('mousedown', (e) => {
          e.preventDefault();
          input.value = it.symbol;
          box.hidden = true;
          input.form.submit();
        });
        box.appendChild(li);
      }
      box.hidden = items.length === 0;
    }, 250);
  });

  input.addEventListener('blur', () => setTimeout(() => { box.hidden = true; }, 150));
}

// ---- ordenación de la tabla por columnas ----------------------------------

// Orden vigente {key, dir}; se re-aplica tras cada refresco de precios.
let currentSort = null;

function sortValue(row, key) {
  const num = (x) => { const v = parseFloat(x); return isNaN(v) ? -Infinity : v; };
  switch (key) {
    case 'ticker': return row.dataset.ticker;
    case 'name': return (row.dataset.name || '').toLowerCase();
    case 'price': return num(row.dataset.price);
    case 'change': return num(row.dataset.change);
    case 'target': { // distancia al objetivo en %
      const t = num(row.dataset.target), p = num(row.dataset.price);
      return t > 0 && p > 0 ? t / p - 1 : -Infinity;
    }
    case 'pos': { // rentabilidad de la posición en %
      const q = num(row.dataset.qty), b = num(row.dataset.buy), p = num(row.dataset.price);
      return q > 0 && b > 0 && p > 0 ? p / b - 1 : -Infinity;
    }
    default: return 0;
  }
}

function applySort() {
  if (!currentSort) return;
  const tbody = document.querySelector('table.watchlist tbody');
  if (!tbody) return;
  const rows = [...tbody.querySelectorAll('tr[data-ticker]')];
  rows.sort((a, b) => {
    const va = sortValue(a, currentSort.key);
    const vb = sortValue(b, currentSort.key);
    const cmp = typeof va === 'string' ? va.localeCompare(vb) : va - vb;
    return cmp * currentSort.dir;
  });
  for (const row of rows) tbody.appendChild(row);
}

function setupSorting() {
  const table = document.querySelector('table.watchlist');
  if (!table) return;
  for (const th of table.querySelectorAll('th.sortable')) {
    th.addEventListener('click', () => {
      const key = th.dataset.key;
      if (currentSort && currentSort.key === key) {
        currentSort.dir = -currentSort.dir;
      } else {
        // texto: A→Z primero; números: de mayor a menor primero
        currentSort = { key, dir: key === 'ticker' || key === 'name' ? 1 : -1 };
      }
      for (const mark of table.querySelectorAll('th .dir')) mark.remove();
      const mark = document.createElement('span');
      mark.className = 'dir';
      mark.textContent = currentSort.dir === 1 ? ' ▲' : ' ▼';
      th.appendChild(mark);
      applySort();
    });
  }
}

// ---- mini-gráficas (sparklines) del último mes ----------------------------

function drawSparkline(svg, values) {
  const w = 72, h = 26;
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) =>
    `${((i / (values.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - min) / span) * (h - 4)).toFixed(1)}`
  ).join(' ');
  const up = values[values.length - 1] >= values[0];
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.innerHTML = `<polyline points="${pts}" fill="none" stroke="${up ? 'var(--up)' : 'var(--down)'}" stroke-width="1.5"/>`;
}

async function loadSparklines() {
  let data;
  try {
    data = await (await fetch('/api/sparklines')).json();
  } catch { return; }
  for (const el of document.querySelectorAll('.spark[data-ticker]')) {
    const points = data[el.dataset.ticker];
    if (points && points.length > 1) drawSparkline(el, points.map((p) => p.close));
  }
}

// ---- celdas derivadas del precio: distancia al objetivo y P/L -------------

function updateDerivedCells(row, price) {
  const target = parseFloat(row.dataset.target);
  const tdist = row.querySelector('.tdist');
  if (tdist && target > 0 && price > 0) {
    tdist.textContent = `(${fmtPct((target / price - 1) * 100)})`;
  }
  const qty = parseFloat(row.dataset.qty);
  const buy = parseFloat(row.dataset.buy);
  const posCell = row.querySelector('.c-pos');
  if (posCell && qty > 0 && buy > 0 && price > 0) {
    const plPct = (price / buy - 1) * 100;
    posCell.innerHTML = '';
    const pct = document.createElement('span');
    pct.className = plPct >= 0 ? 'up' : 'down';
    pct.textContent = fmtPct(plPct);
    const amount = document.createElement('small');
    amount.className = 'muted';
    amount.textContent = ' ' + fmtPrice((price - buy) * qty, row.dataset.currency);
    posCell.append(pct, amount);
  }
}

// ---- precios en vivo: dashboard -----------------------------------------

// Último precio visto por ticker, para pintar la flecha de subida/bajada.
const lastPrices = {};

function tickArrow(arrowEl, ticker, newPrice) {
  const prev = lastPrices[ticker];
  lastPrices[ticker] = newPrice;
  if (prev === undefined || newPrice === prev || !arrowEl) return prev !== undefined && newPrice !== prev;
  const up = newPrice > prev;
  arrowEl.textContent = up ? '▲' : '▼';
  arrowEl.classList.toggle('up', up);
  arrowEl.classList.toggle('down', !up);
  return true;
}

function startLivePrices(intervalSeconds) {
  async function refresh() {
    let quotes;
    try {
      const res = await fetch('/api/quotes');
      quotes = await res.json();
    } catch { return; }
    for (const row of document.querySelectorAll('tr[data-ticker]')) {
      const q = quotes[row.dataset.ticker];
      if (!q) continue;
      row.dataset.price = q.price;
      row.dataset.change = q.change_pct;
      row.dataset.currency = q.currency;
      const priceCell = row.querySelector('.c-price');
      const changeCell = row.querySelector('.c-change');
      const changed = tickArrow(priceCell.querySelector('.p-arrow'), row.dataset.ticker, q.price);
      priceCell.querySelector('.p-val').textContent = fmtPrice(q.price, q.currency);
      if (changed) flashCell(priceCell);
      changeCell.textContent = fmtPct(q.change_pct);
      changeCell.classList.toggle('up', q.change_pct >= 0);
      changeCell.classList.toggle('down', q.change_pct < 0);
      const marketCell = row.querySelector('.c-market');
      if (marketCell && q.market_state) setMarketBadge(marketCell, q.market_state);
      updateDerivedCells(row, q.price);
    }
    applySort(); // mantener la ordenación elegida con los precios nuevos
    const note = document.getElementById('last-update');
    if (note) note.textContent = 'última: ' + new Date().toLocaleTimeString('es-ES');
  }
  setInterval(refresh, intervalSeconds * 1000);
}

// ---- precio en vivo: ficha de un valor -----------------------------------

function startLiveQuote(ticker, intervalSeconds) {
  const priceEl = document.getElementById('big-price');
  const changeEl = document.getElementById('big-change');
  if (!priceEl) return;
  async function refresh() {
    let q;
    try {
      const res = await fetch('/api/quote/' + encodeURIComponent(ticker));
      if (!res.ok) return;
      q = await res.json();
    } catch { return; }
    const changed = tickArrow(document.getElementById('big-arrow'), ticker, q.price);
    priceEl.textContent = fmtPrice(q.price, q.currency);
    if (changed) flashCell(priceEl);
    if (changeEl) {
      changeEl.textContent = fmtPct(q.change_pct) + ' hoy';
      changeEl.classList.toggle('up', q.change_pct >= 0);
      changeEl.classList.toggle('down', q.change_pct < 0);
    }
    const marketEl = document.getElementById('market-state');
    if (marketEl && q.market_state) setMarketBadge(marketEl, q.market_state);
  }
  setInterval(refresh, intervalSeconds * 1000);
}
