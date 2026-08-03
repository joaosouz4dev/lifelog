"""Estado de reunião reportado pela extensão de navegador.

O cliente Windows detecta reuniões sozinho pelo microfone aberto, mas isso
não distingue um Meet de um YouTube dentro do mesmo `chrome.exe` quando há
várias janelas — a leitura de título pega a maior, não a que emite som. A
extensão sabe qual aba está em chamada e reporta aqui.

O estado é **efêmero**, não configuração: vive em memória e expira sozinho.
O TTL é o ponto central deste módulo — a extensão pode morrer junto com o
navegador sem mandar o "acabou", e sem expiração o gate ficaria aberto para
sempre, gravando o dia inteiro. Falha na direção errada.
"""

from __future__ import annotations

import threading
import time

# Quanto vale um relato sem renovação. A extensão reporta a cada ~10 s, então
# 45 s tolera algumas falhas seguidas sem fechar o gate no meio de uma fala.
TTL_SEGUNDOS = 45.0

_lock = threading.Lock()
_estado: dict | None = None
_expira_em = 0.0

# Sobreposição manual, escolhida no popup da extensão ou na interface:
#   "auto"   — decide pela detecção de reunião (o padrão)
#   "sempre" — grava independente de reunião
#   "nunca"  — não grava nada, nem em reunião
#
# Vive em memória junto com o resto: é uma decisão do momento ("agora quero
# gravar isto"), não configuração permanente. Reiniciar volta ao automático,
# que é o comportamento seguro — ninguém quer descobrir semanas depois que
# deixou em "nunca" e perdeu tudo.
_modo = "auto"


def definir_modo(modo: str) -> str:
    """Troca o modo de captura. Devolve o que ficou valendo."""
    global _modo
    if modo not in ("auto", "sempre", "nunca"):
        raise ValueError(f"modo inválido: {modo}")
    with _lock:
        _modo = modo
    return _modo


def modo_atual() -> str:
    with _lock:
        return _modo


def reportar(*, ativa: bool, servico: str | None = None, titulo: str | None = None) -> dict:
    """Registra o que a extensão viu. Devolve o estado resultante."""
    global _estado, _expira_em

    with _lock:
        if not ativa:
            _estado = None
            _expira_em = 0.0
        else:
            _estado = {
                "ativa": True,
                "servico": servico,
                "titulo": titulo,
                "visto_em": time.time(),
            }
            _expira_em = time.monotonic() + TTL_SEGUNDOS

    return atual()


def atual() -> dict:
    """Estado agora, já considerando a expiração e o modo manual."""
    with _lock:
        modo = _modo
        expirou = _estado is None or time.monotonic() > _expira_em
        base = (
            {"ativa": False, "servico": None, "titulo": None, "expira_em_s": 0}
            if expirou
            else {**_estado, "expira_em_s": round(_expira_em - time.monotonic(), 1)}
        )

    # O manual vence a detecção: quem apertou o botão sabe o que quer.
    if modo == "sempre":
        return {**base, "ativa": True, "servico": base.get("servico") or "manual",
                "modo": modo}
    if modo == "nunca":
        return {**base, "ativa": False, "modo": modo}
    return {**base, "modo": modo}


def limpar() -> None:
    """Zera o estado. Existe para os testes não vazarem entre si."""
    global _estado, _expira_em, _modo
    with _lock:
        _estado = None
        _expira_em = 0.0
        _modo = "auto"
