"""Testes da classificação por título de janela e da lista de permitidos.

O nome do executável não distingue uma reunião no Meet de um vídeo no
YouTube — ambos são "chrome.exe". O título da aba distingue.
"""

from __future__ import annotations

import pytest

from server.classify import (
    TITLE_SEP,
    Category,
    classify_app,
    should_transcribe,
)


def rotulo(executavel: str, titulo: str) -> str:
    """Monta o rótulo como o cliente grava."""
    return f"{executavel}{TITLE_SEP}{titulo}"


@pytest.mark.parametrize(
    ("titulo", "esperado"),
    [
        ("Reunião — Google Meet", Category.CONVERSATION),
        ("Zoom Meeting", Category.CONVERSATION),
        ("Microsoft Teams", Category.CONVERSATION),
        ("(2) WhatsApp Business", Category.CONVERSATION),
        ("ESTAGIÁRIO REVELA O MOTIVO - YouTube", Category.ENTERTAINMENT),
        ("O Poderoso Chefão - Netflix", Category.ENTERTAINMENT),
        ("Facebook Marketplace | Facebook", Category.ENTERTAINMENT),
        ("Twitch", Category.ENTERTAINMENT),
    ],
)
def test_titulo_revela_o_que_o_executavel_esconde(titulo, esperado):
    """Todos estes seriam apenas 'chrome.exe' antes."""
    assert classify_app(rotulo("chrome.exe", titulo)) is esperado


def test_sem_titulo_o_navegador_continua_ambiguo():
    """Sem título não dá para saber — a categoria própria existe para isso."""
    assert classify_app("chrome.exe") is Category.BROWSER


def test_reuniao_vence_video_em_outra_aba():
    """Uma reunião com o YouTube tocando ao lado continua sendo reunião."""
    combinado = "+".join([
        rotulo("chrome.exe", "Reunião — Google Meet"),
        rotulo("msedge.exe", "playlist - YouTube"),
    ])
    assert classify_app(combinado) is Category.CONVERSATION


def test_app_dedicado_dispensa_titulo():
    """'zoom.exe' já é conclusivo por si."""
    assert classify_app("zoom.exe") is Category.CONVERSATION


# ───────────────────────── lista de permitidos ─────────────────────────


def test_microfone_passa_sempre():
    """Sua própria voz é o núcleo do lifelog — nunca é filtrada."""
    ok, motivo = should_transcribe(None, "mic", allowlist=["meet"])
    assert ok is True
    assert motivo == "microfone"


def test_allowlist_deixa_passar_o_que_casa():
    ok, motivo = should_transcribe(
        rotulo("chrome.exe", "Reunião — Google Meet"), "system",
        allowlist=["meet", "zoom"],
    )
    assert ok is True
    assert "meet" in motivo


def test_allowlist_barra_o_resto():
    """O caso do dia: 135 segmentos de navegador sem valor de relatório."""
    ok, motivo = should_transcribe(
        rotulo("chrome.exe", "Facebook Marketplace | Facebook"), "system",
        allowlist=["meet", "zoom", "teams"],
    )
    assert ok is False
    assert motivo == "fora da lista de permitidos"


def test_blocklist_vence_a_allowlist():
    """Bloqueio explícito é mais forte que permissão genérica."""
    ok, motivo = should_transcribe(
        rotulo("chrome.exe", "Meet — gravação do treinamento"), "system",
        allowlist=["meet"], blocklist=["treinamento"],
    )
    assert ok is False
    assert "treinamento" in motivo


def test_sem_allowlist_mantem_o_comportamento_antigo():
    """Quem não configurar nada não deve notar mudança."""
    ok, _ = should_transcribe(rotulo("chrome.exe", "Reunião — Meet"), "system")
    assert ok is True

    ok, motivo = should_transcribe(rotulo("chrome.exe", "série - Netflix"), "system")
    assert ok is False
    assert motivo == "entretenimento"
