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
  if (stats.skipped) {
    alerts.push(
      `<span class="tag excluded" title="Fora da lista de permitidos — capturados, mas não transcritos">` +
      `${stats.skipped} ignorados</span>`
    );
  }

  statsEl.innerHTML = `
    <div><b>${stats.total_segments}</b> segmentos</div>
    <div><b>${humanDuration(stats.total_speech_ms)}</b> de fala</div>
    <div>${bySource}</div>
    ${alerts.length ? `<div>${alerts.join(' ')}</div>` : ''}
  `;
}

// O cliente grava "chrome.exe｜Reunião — Google Meet". Na etiqueta o título
// diz muito mais que o executável, então ele vem primeiro e o .exe some.
function originLabel(appName) {
  return appName
    .split('+')
    .map((parte) => {
      const [exe, titulo] = parte.split('｜');
      const limpo = (titulo || '').trim();
      if (limpo) return limpo.length > 42 ? `${limpo.slice(0, 41)}…` : limpo;
      return exe.replace(/\.exe$/i, '');
    })
    .join(' + ');
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
  } else if (segment.status === 'skipped') {
    transcript.classList.add('empty-text');
    transcript.textContent = '(não transcrito — fora da lista)';
  } else {
    transcript.classList.add('pending');
    transcript.textContent = 'transcrevendo…';
  }
  body.appendChild(transcript);

  const tags = document.createElement('div');
  tags.className = 'tags';
  const chips = [`<span class="tag">${SOURCE_LABEL[segment.source] || segment.source}</span>`];

  // Mostra a origem e sinaliza o que o relatório ignora — sem isso não dá
  // para entender por que um trecho não apareceu no resumo do dia.
  if (segment.app_name) {
    const excluded = segment.category === 'entertainment' || segment.status === 'skipped';
    chips.push(
      `<span class="tag${excluded ? ' excluded' : ''}" ` +
      `title="${escapeHTML(segment.app_name)}">` +
      `${escapeHTML(originLabel(segment.app_name))}</span>`
    );
  }
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

// ──────────────────────────────── chat ────────────────────────────────

const chatLog = $('#chat-log');
const chatInput = $('#chat-input');
const chatForm = $('#chat-form');

let chatBusy = false;

/**
 * Converte as citações [1] em botões que destacam o trecho correspondente.
 * Sem isso, o número não teria como ser verificado — que é o ponto de citar.
 */
function renderAnswer(text) {
  return escapeHTML(text)
    .replace(/\[(\d+)\]/g, '<button class="cite" data-n="$1">$1</button>')
    .split(/\n{2,}/)
    .map((p) => `<p>${p.replace(/\n/g, '<br>')}</p>`)
    .join('');
}

function renderSources(sources) {
  if (!sources.length) return '';
  const items = sources.map((s) => {
    const when = new Date(s.started_at);
    const origem = SOURCE_LABEL[s.source] || s.source;
    return `<div class="chat-source" data-n="${s.n}">
      <span class="n">${s.n}</span>
      <span>
        <span class="when">${when.toLocaleString('pt-BR', {
          day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
        })} · ${origem}</span>
        ${escapeHTML(s.text)}
      </span>
    </div>`;
  }).join('');

  return `<details class="chat-sources">
    <summary>${sources.length} trecho(s) usados</summary>${items}</details>`;
}

async function askChat(question) {
  if (chatBusy) return;
  chatBusy = true;
  chatInput.value = '';

  // Primeira pergunta substitui o texto de ajuda.
  const placeholder = chatLog.querySelector('.empty');
  if (placeholder) placeholder.remove();

  const turn = document.createElement('div');
  turn.className = 'chat-turn';
  turn.innerHTML =
    `<div class="chat-question">${escapeHTML(question)}</div>` +
    `<div class="chat-answer chat-thinking">Procurando na transcrição…</div>`;
  chatLog.appendChild(turn);
  chatLog.scrollTop = chatLog.scrollHeight;

  const answerEl = turn.querySelector('.chat-answer');

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    const body = await response.json();

    if (!response.ok) {
      // O servidor diz o motivo real (sem provedor, teto de gasto); mostrar
      // isso é mais útil que "erro ao perguntar".
      answerEl.classList.remove('chat-thinking');
      answerEl.innerHTML = `<p>${escapeHTML(body.detail || `Falhou (HTTP ${response.status})`)}</p>`;
      return;
    }

    answerEl.classList.remove('chat-thinking');
    answerEl.innerHTML =
      renderAnswer(body.answer) +
      renderSources(body.sources || []) +
      (body.provider
        ? `<div class="chat-meta">${escapeHTML(body.provider)}` +
          `${body.cost_cents ? ` · ${body.cost_cents.toFixed(2)}¢` : ''}</div>`
        : '');

    // Clicar na citação abre as fontes e destaca o trecho citado.
    for (const cite of answerEl.querySelectorAll('.cite')) {
      cite.addEventListener('click', () => {
        const details = answerEl.querySelector('.chat-sources');
        if (details) details.open = true;
        for (const src of answerEl.querySelectorAll('.chat-source')) {
          src.classList.toggle('is-target', src.dataset.n === cite.dataset.n);
        }
        answerEl.querySelector(`.chat-source[data-n="${cite.dataset.n}"]`)
          ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      });
    }
  } catch {
    answerEl.classList.remove('chat-thinking');
    answerEl.innerHTML = '<p>Servidor indisponível.</p>';
  } finally {
    chatBusy = false;
    chatLog.scrollTop = chatLog.scrollHeight;
  }
}

// ───────────────────────────── relatórios ─────────────────────────────

const reportListEl = $('#report-list');
const reportBodyEl = $('#report-body');
const feedbackEl = $('#gen-feedback');

let activeReportId = null;

/** Escapa antes de qualquer marcação — o texto vem de um LLM. */
function escapeHTML(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Markdown mínimo: títulos, listas, negrito, itálico e código.
 * É o que os prompts de relatório produzem — um parser completo seria
 * dependência externa, e a página roda sem acesso a CDN.
 */
function renderMarkdown(md) {
  const html = [];
  let inList = false;

  const inline = (text) =>
    escapeHTML(text)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|\W)\*(?!\s)(.+?)(?<!\s)\*/g, '$1<em>$2</em>')
      .replace(/`(.+?)`/g, '<code>$1</code>');

  const closeList = () => {
    if (inList) { html.push('</ul>'); inList = false; }
  };

  for (const raw of md.split('\n')) {
    const line = raw.trimEnd();

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length, 3);
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    const item = line.match(/^[-*]\s+(.*)$/);
    if (item) {
      if (!inList) { html.push('<ul>'); inList = true; }
      html.push(`<li>${inline(item[1])}</li>`);
      continue;
    }

    if (!line.trim()) { closeList(); continue; }

    closeList();
    html.push(`<p>${inline(line)}</p>`);
  }

  closeList();
  return html.join('');
}

function reportLabel(report) {
  const start = new Date(`${report.period_start}T00:00:00`);
  if (report.type === 'monthly') {
    return start.toLocaleDateString('pt-BR', { month: 'long', year: 'numeric' });
  }
  return start.toLocaleDateString('pt-BR', {
    weekday: 'short', day: '2-digit', month: '2-digit',
  });
}

async function loadReports() {
  let reports;
  try {
    reports = await fetchJSON('/api/reports');
  } catch {
    reportListEl.innerHTML = '<p class="empty">Não foi possível carregar.</p>';
    return;
  }

  if (!reports.length) {
    reportListEl.replaceChildren();
    reportBodyEl.innerHTML =
      '<p class="empty">Nenhum relatório ainda.<br>' +
      '<small>Use os botões acima para gerar o primeiro.</small></p>';
    return;
  }

  reportListEl.replaceChildren();
  for (const report of reports) {
    const button = document.createElement('button');
    button.className = 'report-item';
    button.type = 'button';
    button.dataset.id = report.id;
    if (report.id === activeReportId) button.classList.add('is-active');
    button.innerHTML =
      `${escapeHTML(reportLabel(report))}` +
      `<span class="meta">${report.type === 'monthly' ? 'mensal' : 'diário'}` +
      `${report.cost_cents ? ` · ${report.cost_cents.toFixed(1)}¢` : ''}</span>`;
    button.addEventListener('click', () => openReport(report.id));
    reportListEl.appendChild(button);
  }

  if (activeReportId === null) openReport(reports[0].id);
}

async function openReport(id) {
  activeReportId = id;
  for (const item of reportListEl.querySelectorAll('.report-item')) {
    item.classList.toggle('is-active', Number(item.dataset.id) === id);
  }

  reportBodyEl.innerHTML = '<p class="empty">Carregando…</p>';
  try {
    const report = await fetchJSON(`/api/reports/${id}`);
    const generated = new Date(report.generated_at.replace(' ', 'T'));
    reportBodyEl.innerHTML =
      renderMarkdown(report.content_md) +
      `<footer class="report-footer">Gerado por ${escapeHTML(report.llm_provider || '?')} ` +
      `em ${generated.toLocaleString('pt-BR')} · ` +
      `${report.tokens_in.toLocaleString('pt-BR')} tokens de entrada · ` +
      `${report.cost_cents.toFixed(2)} centavos</footer>`;
  } catch {
    reportBodyEl.innerHTML = '<p class="empty">Não foi possível abrir este relatório.</p>';
  }
}

async function generateReport(kind, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Gerando…';
  feedbackEl.textContent = 'Pode levar um minuto.';
  feedbackEl.className = 'feedback';

  try {
    const response = await fetch(`/api/reports/${kind}`, { method: 'POST' });
    const body = await response.json();

    if (!response.ok) {
      // O servidor devolve o motivo real (sem material, sem provedor, teto
      // de gasto) — mostrar isso é mais útil que "erro ao gerar".
      feedbackEl.textContent = body.detail || `Falhou (HTTP ${response.status})`;
      feedbackEl.className = 'feedback error';
      return;
    }

    feedbackEl.textContent = `Pronto — ${body.provider}, ${body.cost_cents.toFixed(2)}¢`;
    feedbackEl.className = 'feedback success';
    activeReportId = body.id;
    await loadReports();
    await openReport(body.id);
  } catch (err) {
    feedbackEl.textContent = 'Servidor indisponível.';
    feedbackEl.className = 'feedback error';
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

// ────────────────────────────── navegação ──────────────────────────────

function switchView(view) {
  for (const tab of document.querySelectorAll('.tab')) {
    tab.classList.toggle('is-active', tab.dataset.view === view);
  }
  $('#view-timeline').hidden = view !== 'timeline';
  $('#view-reports').hidden = view !== 'reports';
  $('#view-chat').hidden = view !== 'chat';

  // Data e busca só fazem sentido na timeline.
  dayPicker.hidden = view !== 'timeline';
  searchBox.hidden = view !== 'timeline';
  statsEl.hidden = view !== 'timeline';

  if (view === 'reports') {
    clearTimeout(refreshTimer);
    loadReports();
  } else if (view === 'chat') {
    clearTimeout(refreshTimer);
    chatInput.focus();
  } else {
    loadDay(dayPicker.value);
  }
}

function renderHubSection(title, hub) {
  if (!hub) return `<h3>${title}</h3><p><small>indisponível</small></p>`;

  const rows = hub.providers.map((p) => {
    const state = p.circuit_open ? 'circuito aberto'
      : p.available ? 'disponível' : 'indisponível';
    const cls = p.available && !p.circuit_open ? 'up' : 'down';
    return `<div class="provider"><span>${escapeHTML(p.name)}</span>
            <span class="dot ${cls}">● ${state}</span></div>`;
  }).join('');

  const budget = hub.daily_budget_cents
    ? `${hub.spent_today_cents.toFixed(2)} de ${hub.daily_budget_cents.toFixed(2)} centavos hoje`
    : 'sem teto configurado';

  return `<h3>${title}</h3>
          <p><small>ordem: ${escapeHTML(hub.chain.join(' → ')) || '(nenhum provedor ativo)'}</small></p>
          ${rows || '<p><small>nada configurado</small></p>'}
          <p><small>Gasto: ${budget}</small></p>`;
}

async function showHub() {
  const body = $('#hub-body');
  body.textContent = 'carregando…';
  hubDialog.showModal();

  // Os dois hubs são independentes, inclusive nos tetos de gasto — mostrar
  // só o de transcrição esconderia metade do custo.
  const [stt, llm] = await Promise.all([
    fetchJSON('/api/hub/stt').catch(() => null),
    fetchJSON('/api/hub/llm').catch(() => null),
  ]);

  if (!stt && !llm) {
    body.textContent = 'não foi possível ler o status dos provedores.';
    return;
  }

  body.innerHTML =
    renderHubSection('Transcrição', stt) +
    renderHubSection('Relatórios e chat', llm);
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

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => switchView(tab.dataset.view));
}

$('#gen-daily').addEventListener('click', (e) => generateReport('daily', e.target));
$('#gen-monthly').addEventListener('click', (e) => generateReport('monthly', e.target));

chatForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const question = chatInput.value.trim();
  if (question) askChat(question);
});

// Pausa o polling quando a aba está oculta — não faz sentido no celular no bolso.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) clearTimeout(refreshTimer);
  else if (!$('#view-timeline').hidden) loadDay(dayPicker.value);
});

// ─────────────────────────────── versão ───────────────────────────────

async function loadVersion() {
  const label = $('#version-label');
  const status = $('#update-status');

  try {
    const info = await fetchJSON('/api/version');
    label.textContent = `Lifelog ${info.current}`;

    if (info.update_available) {
      status.className = 'update-status has-update';
      status.innerHTML =
        `versão ${escapeHTML(info.latest)} disponível — ` +
        `<a href="${info.url}" target="_blank" rel="noopener">baixar</a>`;
    } else if (info.checked) {
      status.textContent = 'atualizado';
    } else {
      // Rodando do código-fonte, ou sem internet: nada a oferecer.
      status.textContent = '';
    }
  } catch {
    label.textContent = 'Lifelog';
  }
}

loadVersion();
loadDay(dayPicker.value);
