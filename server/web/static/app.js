'use strict';

const $ = (sel) => document.querySelector(sel);

const timelineEl = $('#timeline');
const emptyEl = $('#empty');
const statsEl = $('#stats');
const dayPicker = $('#day-picker');
const searchBox = $('#search-box');
const hubDialog = $('#hub-dialog');

const SOURCE_LABEL = { mic: 'microfone', system: 'sistema', gadget: 'gadget' };

let refreshTimer = null;
let searchDebounce = null;

// ─────────────────────────────── helpers ───────────────────────────────

const pad = (n) => String(n).padStart(2, '0');

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function timeOf(iso) {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function hourOf(iso) {
  return `${pad(new Date(iso).getHours())}:00`;
}

function humanDuration(ms) {
  const totalSeconds = Math.round(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return `${minutes}min ${pad(totalSeconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${pad(minutes % 60)}min`;
}

async function fetchJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} em ${url}`);
  return response.json();
}

// ────────────────────────────── renderização ──────────────────────────────

function renderStats(stats) {
  const bySource = Object.entries(stats.by_source || {})
    .map(([source, n]) => `${SOURCE_LABEL[source] || source}: ${n}`)
    .join(' · ') || 'nada capturado';

  const alerts = [];
  if (stats.pending) alerts.push(`<span class="tag status-pending">${stats.pending} na fila</span>`);
  if (stats.failed) alerts.push(`<span class="tag status-failed">${stats.failed} com falha</span>`);

  statsEl.innerHTML = `
    <div><b>${stats.total_segments}</b> segmentos</div>
    <div><b>${humanDuration(stats.total_speech_ms)}</b> de fala</div>
    <div>${bySource}</div>
    ${alerts.length ? `<div>${alerts.join(' ')}</div>` : ''}
  `;
}

function segmentNode(segment) {
  const article = document.createElement('article');
  article.className = 'segment';
  article.dataset.source = segment.source;

  const bar = document.createElement('div');
  bar.className = 'bar';

  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.innerHTML = `${timeOf(segment.started_at)}
    <span class="dur">${humanDuration(segment.duration_ms)}</span>`;

  const body = document.createElement('div');
  body.className = 'body';

  const transcript = document.createElement('p');
  transcript.className = 'transcript';
  if (segment.transcript) {
    transcript.textContent = segment.transcript;   // textContent evita injeção de HTML
  } else if (segment.status === 'done') {
    transcript.classList.add('empty-text');
    transcript.textContent = '(sem fala reconhecida)';
  } else if (segment.status === 'failed') {
    transcript.classList.add('empty-text');
    transcript.textContent = '(falha ao transcrever)';
  } else {
    transcript.classList.add('pending');
    transcript.textContent = 'transcrevendo…';
  }
  body.appendChild(transcript);

  const tags = document.createElement('div');
  tags.className = 'tags';
  const chips = [`<span class="tag">${SOURCE_LABEL[segment.source] || segment.source}</span>`];
  if (segment.status !== 'done') {
    chips.push(`<span class="tag status-${segment.status}">${segment.status}</span>`);
  }
  if (segment.stt_provider) chips.push(`<span class="tag">${segment.stt_provider}</span>`);
  if (typeof segment.confidence === 'number') {
    const low = segment.confidence < 0.6 ? ' low-confidence' : '';
    chips.push(`<span class="tag${low}">${Math.round(segment.confidence * 100)}%</span>`);
  }
  tags.innerHTML = chips.join('');
  body.appendChild(tags);

  if (segment.has_audio) {
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.preload = 'none';   // não baixa nada até o usuário dar play
    audio.src = `/api/segments/${segment.id}/audio`;
    body.appendChild(audio);
  }

  article.append(bar, meta, body);
  return article;
}

function renderTimeline(segments) {
  timelineEl.replaceChildren();
  emptyEl.hidden = segments.length > 0;
  if (!segments.length) return;

  let currentHour = null;
  let group = null;

  for (const segment of segments) {
    const hour = hourOf(segment.started_at);
    if (hour !== currentHour) {
      currentHour = hour;
      group = document.createElement('section');
      group.className = 'hour-group';
      const label = document.createElement('div');
      label.className = 'hour-label';
      label.textContent = hour;
      group.appendChild(label);
      timelineEl.appendChild(group);
    }
    group.appendChild(segmentNode(segment));
  }
}

// ──────────────────────────────── ações ────────────────────────────────

async function loadDay(day) {
  try {
    const [segments, stats] = await Promise.all([
      fetchJSON(`/api/segments?day=${day}`),
      fetchJSON(`/api/stats?day=${day}`),
    ]);
    renderStats(stats);
    renderTimeline(segments);
    // Enquanto houver trabalho pendente, atualiza sozinho.
    scheduleRefresh(stats.pending > 0 ? 4000 : 20000);
  } catch (err) {
    statsEl.innerHTML = `<div class="tag status-failed">servidor indisponível</div>`;
    scheduleRefresh(10000);
  }
}

async function runSearch(query) {
  try {
    const segments = await fetchJSON(`/api/search?q=${encodeURIComponent(query)}`);
    statsEl.innerHTML = `<div><b>${segments.length}</b> resultado(s) para “${query}”</div>`;
    renderTimeline(segments);
  } catch {
    statsEl.innerHTML = `<div class="tag status-failed">busca falhou</div>`;
  }
}

function scheduleRefresh(delay) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    if (!searchBox.value.trim()) loadDay(dayPicker.value);
  }, delay);
}

async function showHub() {
  const body = $('#hub-body');
  body.textContent = 'carregando…';
  hubDialog.showModal();
  try {
    const hub = await fetchJSON('/api/hub/stt');
    const rows = hub.providers.map((p) => {
      const state = p.circuit_open ? 'circuito aberto'
        : p.available ? 'disponível' : 'indisponível';
      const cls = p.available && !p.circuit_open ? 'up' : 'down';
      return `<div class="provider"><span>${p.name}</span>
              <span class="dot ${cls}">● ${state}</span></div>`;
    }).join('');
    const budget = hub.daily_budget_cents
      ? `${hub.spent_today_cents.toFixed(2)} de ${hub.daily_budget_cents.toFixed(2)} centavos hoje`
      : 'sem teto configurado';
    body.innerHTML = `<p><small>ordem: ${hub.chain.join(' → ') || '(vazia)'}</small></p>
                      ${rows}<p><small>Gasto: ${budget}</small></p>`;
  } catch {
    body.textContent = 'não foi possível ler o status do hub.';
  }
}

// ──────────────────────────────── eventos ────────────────────────────────

dayPicker.value = todayISO();
dayPicker.addEventListener('change', () => {
  searchBox.value = '';
  loadDay(dayPicker.value);
});

searchBox.addEventListener('input', () => {
  clearTimeout(searchDebounce);
  const query = searchBox.value.trim();
  searchDebounce = setTimeout(() => {
    if (query.length >= 2) runSearch(query);
    else loadDay(dayPicker.value);
  }, 300);
});

$('#hub-btn').addEventListener('click', showHub);

// Pausa o polling quando a aba está oculta — não faz sentido no celular no bolso.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) clearTimeout(refreshTimer);
  else loadDay(dayPicker.value);
});

loadDay(dayPicker.value);
