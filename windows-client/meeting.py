"""Detecta se há uma reunião em curso.

O Lifelog só grava durante reuniões. A pergunta "estou numa reunião?" não tem
resposta direta no Windows, então combinamos dois sinais:

1. **O microfone está aberto.** O Windows registra em
   `ConsentStore\\microphone` quais processos têm o microfone em uso agora
   (`LastUsedTimeStop == 0`). Meet, Teams e Zoom sempre abrem o microfone numa
   chamada, mesmo mutado — e isso independe de qual aba está em foco, que é a
   fraqueza de olhar só o título da janela.

2. **O processo é de reunião.** Só o microfone não basta: jogos, OBS e o
   NVIDIA Broadcast também o abrem. Um navegador precisa ainda ter título de
   reunião para contar.

O detector FALHA ABERTO: se o registro ficar ilegível, se o COM cair, se a
thread morrer — ele deixa gravar. Uma falha silenciosa que apaga uma reunião
inteira não deixa rastro para diagnosticar; áudio gravado a mais pode ser
apagado depois.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("meeting")

# O httpx loga cada request em INFO. Consultando o servidor a cada 2s, isso
# afogaria o log em linhas idênticas e esconderia o que importa — quando o
# gate abriu e fechou.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Chave onde o Windows registra o uso do microfone por aplicativo.
_CONSENT = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore\microphone"
)

# Apps de reunião: com o microfone aberto, já são conclusivos.
APPS_DE_REUNIAO = (
    "zoom", "teams", "webex", "gotomeeting", "ringcentral", "bluejeans",
    "whereby", "discord", "slack",
)

# Navegadores: precisam do título para distinguir Meet de YouTube.
_NAVEGADORES = ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "arc")

# Consultar o registro e o COM a cada 100 ms seria desperdício; uma reunião
# não começa e termina nesse intervalo.
INTERVALO_S = 2.0


def _com_microfone_aberto() -> set[str]:
    """Executáveis com o microfone em uso agora, em minúsculas.

    Devolve conjunto vazio se o registro não puder ser lido — o chamador
    trata isso como "não sei", não como "ninguém está usando".
    """
    import winreg

    achados: set[str] = set()
    for sub in (_CONSENT, _CONSENT + r"\NonPackaged"):
        try:
            chave = winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            continue

        indice = 0
        while True:
            try:
                nome = winreg.EnumKey(chave, indice)
                indice += 1
            except OSError:
                break
            try:
                sub_chave = winreg.OpenKey(chave, nome)
                fim, _ = winreg.QueryValueEx(sub_chave, "LastUsedTimeStop")
            except OSError:
                continue
            # 0 significa "ainda em uso"; qualquer outro valor é um carimbo
            # de quando o uso terminou.
            if fim == 0:
                # As chaves usam # no lugar de \ no caminho do executável.
                achados.add(nome.replace("#", "\\").split("\\")[-1].lower())

    return achados


class MeetingDetector:
    """Diz se há reunião agora, com uma leitura barata para o laço de captura.

    O trabalho caro (registro, COM) roda numa thread própria; o laço de
    captura só lê um booleano.
    """

    def __init__(
        self,
        probe=None,   # WindowTitleProbe: precisa expor titles() -> {processo: título}
        *,
        server_url: str | None = None,
        apps: tuple[str, ...] = APPS_DE_REUNIAO,
        atraso_fechamento_s: float = 15.0,
        intervalo_s: float = INTERVALO_S,
    ):
        self.probe = probe
        self.server_url = server_url.rstrip("/") if server_url else None
        self.apps = tuple(a.lower() for a in apps)
        self.atraso_fechamento_s = atraso_fechamento_s
        self.intervalo_s = intervalo_s

        self._aberto = threading.Event()
        self._aberto.set()  # falha aberta: começa gravando até saber o contrário
        self._motivo = "iniciando"
        self._visto_em: float | None = None
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None

    # ─────────────────────────── leitura barata ───────────────────────────

    @property
    def em_reuniao(self) -> bool:
        """Chamado a cada 100 ms pelo laço de captura: precisa ser O(1)."""
        return self._aberto.is_set()

    @property
    def motivo(self) -> str:
        """Por que o gate está como está — para a bandeja e o log."""
        return self._motivo

    # ──────────────────────────── ciclo de vida ────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._vigiar, name="meeting-detector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ───────────────────────────── bastidores ─────────────────────────────

    def _vigiar(self) -> None:
        while not self._parar.is_set():
            try:
                self._avaliar()
            except Exception:
                # Qualquer falha inesperada abre o gate. Perder uma reunião
                # por causa de um bug no detector é o pior desfecho possível.
                log.exception("detector de reunião falhou; gravando por precaução")
                self._abrir("detector falhou")
            self._parar.wait(self.intervalo_s)

    def _avaliar(self) -> None:
        encontrada, motivo = self._procurar_reuniao()

        if encontrada:
            self._visto_em = time.monotonic()
            self._abrir(motivo)
            return

        if self._visto_em is None:
            self._fechar("nenhuma reunião")
            return

        # Atraso no fechamento: sem isso o gate corta o "tchau, até semana
        # que vem" no instante em que a chamada é encerrada.
        desde = time.monotonic() - self._visto_em
        if desde < self.atraso_fechamento_s:
            self._abrir(f"reunião encerrada há {desde:.0f}s")
        else:
            self._visto_em = None
            self._fechar("nenhuma reunião")

    def _procurar_reuniao(self) -> tuple[bool, str]:
        """Devolve (há reunião, motivo)."""
        # A extensão de navegador é a fonte mais precisa: ela sabe qual aba
        # está em chamada, coisa que a leitura de título não distingue quando
        # há várias janelas abertas.
        pela_extensao = self._perguntar_ao_servidor()
        if pela_extensao is not None:
            return pela_extensao

        try:
            com_microfone = _com_microfone_aberto()
        except Exception:
            log.debug("não deu para ler o registro do microfone", exc_info=True)
            return True, "registro ilegível — gravando por precaução"

        if not com_microfone:
            return False, "nenhum app com microfone aberto"

        # App dedicado com microfone aberto já é conclusivo.
        for processo in com_microfone:
            for app in self.apps:
                if app in processo:
                    return True, f"{processo} com microfone aberto"

        # Navegador precisa do título: o mesmo chrome.exe serve Meet e YouTube.
        navegadores = [p for p in com_microfone if any(n in p for n in _NAVEGADORES)]
        if navegadores and self.probe is not None:
            titulo = self._titulo_de_reuniao(navegadores)
            if titulo:
                return True, titulo

        return False, "microfone aberto, mas não é reunião"

    def _perguntar_ao_servidor(self) -> tuple[bool, str] | None:
        """O que a extensão de navegador reportou, ou None se não souber.

        Devolver None (em vez de False) importa: sem extensão instalada, ou
        com o servidor fora do ar, a decisão volta para os sinais locais em
        vez de fechar o gate. Servidor caído não pode apagar uma reunião.
        """
        if not self.server_url:
            return None

        try:
            import httpx

            resposta = httpx.get(f"{self.server_url}/api/meeting/state", timeout=2)
            if resposta.status_code != 200:
                return None
            estado = resposta.json()
        except Exception:
            log.debug("servidor não respondeu sobre reunião", exc_info=True)
            return None

        modo = estado.get("modo", "auto")
        if modo == "nunca":
            # Escolha explícita da pessoa: não grava nada, nem em reunião.
            # É a única situação em que fechamos o gate sem hesitar.
            return False, "desligado manualmente"
        if modo == "sempre":
            return True, "gravação forçada manualmente"

        if not estado.get("ativa"):
            # A extensão está viva e diz que não há reunião no navegador —
            # mas um Zoom instalado ainda pode estar rodando, então isso não
            # é conclusivo. Cai para os sinais locais.
            return None

        servico = estado.get("servico") or "navegador"
        titulo = (estado.get("titulo") or "")[:40]
        return True, f"{servico} (extensão): {titulo}"

    def _titulo_de_reuniao(self, navegadores: list[str]) -> str | None:
        """Título de reunião numa janela de navegador, se houver."""
        try:
            from server.classify import _CONVERSATION_SITES
        except Exception:
            return None

        try:
            mapa = self.probe.titles()
        except Exception:
            log.debug("não deu para ler os títulos", exc_info=True)
            return None

        for processo in navegadores:
            titulo = (mapa.get(processo) or "").lower()
            for site in _CONVERSATION_SITES:
                if site in titulo:
                    return f"{processo}: {titulo[:40]}"
        return None

    def _abrir(self, motivo: str) -> None:
        if not self._aberto.is_set():
            log.info("reunião detectada (%s) — gravando", motivo)
        self._motivo = motivo
        self._aberto.set()

    def _fechar(self, motivo: str) -> None:
        if self._aberto.is_set():
            log.info("sem reunião (%s) — captura em espera", motivo)
        self._motivo = motivo
        self._aberto.clear()
