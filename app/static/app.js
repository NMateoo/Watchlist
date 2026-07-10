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
    }
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
