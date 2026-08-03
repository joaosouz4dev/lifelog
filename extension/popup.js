'use strict';

/**
 * Popup da extensão: mostra o que o Lifelog está fazendo agora e permite
 * forçar ou impedir a gravação sem abrir a interface completa.
 */

const SERVIDOR = 'http://127.0.0.1:8000';

const $ = (sel) => document.querySelector(sel);

async function pedir(caminho, opcoes) {
  const resposta = await fetch(`${SERVIDOR}${caminho}`, opcoes);
  if (!resposta.ok) throw new Error(`${resposta.status} em ${caminho}`);
  return resposta.json();
}

function hora(iso) {
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function duracao(ms) {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const min = Math.floor(s / 60);
  if (min < 60) return `${min}min`;
  return `${Math.floor(min / 60)}h${String(min % 60).padStart(2, '0')}`;
}

// ─────────────────────────── renderização ───────────────────────────

function mostrarEstado(estado) {
  const ponto = $('#ponto');
  const titulo = $('#estado-titulo');
  const motivo = $('#estado-motivo');

  ponto.className = 'ponto';

  if (estado.modo === 'nunca') {
    ponto.classList.add('espera');
    titulo.textContent = 'Não gravando';
    motivo.textContent = 'desligado manualmente';
  } else if (estado.ativa) {
    ponto.classList.add('gravando');
    titulo.textContent = 'Gravando';
    motivo.textContent = estado.modo === 'sempre'
      ? 'forçado manualmente'
      : `${estado.servico || 'reunião'}${estado.titulo ? ` — ${estado.titulo}` : ''}`;
  } else {
    ponto.classList.add('espera');
    titulo.textContent = 'Em espera';
    motivo.textContent = 'nenhuma reunião detectada';
  }

  for (const botao of document.querySelectorAll('.modo')) {
    botao.classList.toggle('ativo', botao.dataset.modo === (estado.modo || 'auto'));
  }
}

function mostrarNumeros(stats) {
  $('#n-segmentos').textContent = stats.total_segments ?? 0;
  $('#n-fala').textContent = duracao(stats.total_speech_ms || 0);
  $('#n-fila').textContent = stats.pending ?? 0;
}

function mostrarRecentes(segmentos) {
  const lista = $('#lista-recentes');
  // Só o que tem texto: um segmento ainda na fila não diz nada a quem olha.
  const comTexto = segmentos.filter((s) => s.transcript && s.transcript.trim());

  if (!comTexto.length) {
    lista.innerHTML = '<li class="vazio">Nada transcrito hoje ainda.</li>';
    return;
  }

  lista.replaceChildren(...comTexto.slice(0, 5).map((s) => {
    const li = document.createElement('li');
    const quando = document.createElement('span');
    quando.className = 'hora';
    quando.textContent = hora(s.started_at);
    li.append(quando, document.createTextNode(s.transcript.trim()));
    return li;
  }));
}

// ─────────────────────────── carregamento ───────────────────────────

async function carregar() {
  try {
    const [estado, stats, segmentos] = await Promise.all([
      pedir('/api/meeting/state'),
      pedir('/api/stats'),
      pedir('/api/segments?limit=12'),
    ]);

    mostrarEstado(estado);
    mostrarNumeros(stats);
    mostrarRecentes(segmentos);
    $('#erro').hidden = true;

    pedir('/api/version')
      .then((v) => { $('#versao').textContent = `v${v.current}`; })
      .catch(() => {});
  } catch (err) {
    // O Lifelog pode estar fechado — dizer isso é mais útil que um erro cru.
    $('#ponto').className = 'ponto erro';
    $('#estado-titulo').textContent = 'Lifelog não está rodando';
    $('#estado-motivo').textContent = 'abra o app na bandeja';
    $('#erro').textContent = err.message;
    $('#erro').hidden = false;
  }
}

// ─────────────────────────── ações ───────────────────────────

for (const botao of document.querySelectorAll('.modo')) {
  botao.addEventListener('click', async () => {
    try {
      const estado = await pedir('/api/meeting/mode', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ modo: botao.dataset.modo }),
      });
      mostrarEstado(estado);
    } catch (err) {
      $('#erro').textContent = err.message;
      $('#erro').hidden = false;
    }
  });
}

$('#abrir').addEventListener('click', () => {
  chrome.tabs.create({ url: SERVIDOR });
  window.close();
});

carregar();
// O popup fica aberto enquanto a pessoa olha; atualizar mostra a gravação
// avançando em tempo real.
setInterval(carregar, 4000);
