'use strict';

/**
 * Avisa o Lifelog quando há uma reunião em curso no navegador.
 *
 * O cliente Windows detecta reuniões pelo microfone aberto, mas isso não
 * distingue um Meet de um YouTube dentro do mesmo chrome.exe: ele lê o título
 * da MAIOR janela do processo, não da aba que emite som. Com várias janelas
 * abertas, um Meet numa janela menor é invisível — e perder a reunião inteira
 * é o erro mais caro do produto.
 *
 * A extensão sabe exatamente qual aba está em chamada.
 *
 * Privacidade: só reporta se há reunião, em qual serviço e o título da aba.
 * Nunca o conteúdo da página — não há permissão para lê-lo.
 */

const SERVIDOR = 'http://127.0.0.1:8000/api/meeting/state';

// Um relato vale 45s no servidor. Reportar a cada 10s tolera algumas falhas
// seguidas sem o gate fechar no meio de uma fala.
const INTERVALO_MS = 10_000;

// Domínios de reunião e o rótulo que vai para o servidor.
const SERVICOS = [
  { padrao: /^https:\/\/meet\.google\.com\/[a-z]{3}-/i, nome: 'meet' },
  { padrao: /^https:\/\/teams\.(microsoft|live)\.com\//i, nome: 'teams' },
  { padrao: /^https:\/\/[\w.-]*zoom\.us\/(j|wc|s)\//i, nome: 'zoom' },
  { padrao: /^https:\/\/[\w.-]*webex\.com\/(meet|wbxmjs)/i, nome: 'webex' },
  { padrao: /^https:\/\/whereby\.com\/./i, nome: 'whereby' },
  { padrao: /^https:\/\/meet\.jit\.si\/./i, nome: 'jitsi' },
  { padrao: /^https:\/\/app\.gather\.town\//i, nome: 'gather' },
  { padrao: /^https:\/\/discord\.com\/channels\//i, nome: 'discord' },
];

let ultimoRelato = null;

/**
 * A aba está numa chamada de verdade?
 *
 * A URL sozinha não basta: a página inicial do Meet (meet.google.com sem
 * código de sala) fica aberta o dia todo sem reunião nenhuma. Por isso o
 * padrão exige o formato de sala, e exigimos também que a aba esteja
 * emitindo áudio ou com o microfone/câmera em uso.
 */
function servicoDaAba(aba) {
  if (!aba.url) return null;
  for (const { padrao, nome } of SERVICOS) {
    if (padrao.test(aba.url)) return nome;
  }
  return null;
}

function abaEstaEmChamada(aba) {
  // `audible` = está saindo som dela. Numa reunião alguém fala, mesmo que
  // você esteja mudo.
  if (aba.audible) return true;
  // O Chrome marca a aba quando ela captura mídia. Nem toda versão expõe,
  // então é um reforço, não o único sinal.
  return Boolean(aba.mutedInfo && aba.mutedInfo.reason === 'capture');
}

async function procurarReuniao() {
  let abas;
  try {
    abas = await chrome.tabs.query({});
  } catch (err) {
    console.warn('Lifelog: não deu para listar as abas', err);
    return null;
  }

  // Primeiro as que estão claramente em chamada; só depois as candidatas
  // silenciosas, que podem ser uma reunião em que ninguém fala no momento.
  const candidatas = [];
  for (const aba of abas) {
    const servico = servicoDaAba(aba);
    if (!servico) continue;
    if (abaEstaEmChamada(aba)) {
      return { servico, titulo: aba.title || null };
    }
    candidatas.push({ servico, titulo: aba.title || null });
  }

  // Uma sala aberta sem som ainda é uma reunião — a pessoa pode estar
  // ouvindo alguém mudo, ou esperando o outro entrar.
  return candidatas[0] || null;
}

async function reportar(reuniao) {
  const corpo = reuniao
    ? { ativa: true, servico: reuniao.servico, titulo: reuniao.titulo }
    : { ativa: false };

  try {
    await fetch(SERVIDOR, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(corpo),
    });
  } catch (err) {
    // O Lifelog pode estar fechado — não é erro, e o TTL do servidor cuida
    // de expirar o relato antigo sozinho.
    console.debug('Lifelog indisponível', err);
  }
}

async function verificar() {
  const reuniao = await procurarReuniao();
  const assinatura = reuniao ? `${reuniao.servico}|${reuniao.titulo}` : null;

  // Reporta sempre que houver reunião (para renovar o TTL) e uma única vez
  // quando ela termina.
  if (reuniao || ultimoRelato !== null) {
    await reportar(reuniao);
  }
  ultimoRelato = assinatura;
}

// O service worker do Manifest V3 hiberna; o alarme o acorda.
chrome.alarms.create('lifelog-verificar', { periodInMinutes: INTERVALO_MS / 60_000 });
chrome.alarms.onAlarm.addListener((alarme) => {
  if (alarme.name === 'lifelog-verificar') verificar();
});

// Reage na hora quando uma aba muda de estado, sem esperar o alarme: entrar
// numa reunião deve começar a gravar imediatamente.
chrome.tabs.onUpdated.addListener((_id, mudancas) => {
  if ('audible' in mudancas || 'url' in mudancas || 'title' in mudancas) verificar();
});
chrome.tabs.onRemoved.addListener(() => verificar());

verificar();
