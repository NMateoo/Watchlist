// Utilidades compartidas: formateo, buscador con sugerencias y precios en vivo.

const CURRENCY_SYMBOLS = { USD: '$', EUR: '€', GBP: '£' };
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
      const newPrice = fmtPrice(q.price, q.currency);
      if (priceCell.textContent.trim() !== newPrice) {
        priceCell.textContent = newPrice;
        flashCell(priceCell);
      }
      changeCell.textContent = fmtPct(q.change_pct);
      changeCell.classList.toggle('up', q.change_pct >= 0);
      changeCell.classList.toggle('down', q.change_pct < 0);
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
    const newPrice = fmtPrice(q.price, q.currency);
    if (priceEl.textContent.trim() !== newPrice) {
      priceEl.textContent = newPrice;
      flashCell(priceEl);
    }
    if (changeEl) {
      changeEl.textContent = fmtPct(q.change_pct) + ' hoy';
      changeEl.classList.toggle('up', q.change_pct >= 0);
      changeEl.classList.toggle('down', q.change_pct < 0);
    }
  }
  setInterval(refresh, intervalSeconds * 1000);
}
