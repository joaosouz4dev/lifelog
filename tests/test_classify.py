"""Testes da classificação por app de origem.

O caso que motivou tudo: um dia com Netflix e uma reunião não pode produzir um
relatório sobre a série.
"""

from __future__ import annotations

import pytest

from server.classify import Category, classify_app, is_report_worthy, label


@pytest.mark.parametrize(
    ("app", "esperado"),
    [
        ("teams.exe", Category.CONVERSATION),
        ("zoom.exe", Category.CONVERSATION),
        ("Discord.exe", Category.CONVERSATION),
        ("slack.exe", Category.CONVERSATION),
        ("netflix.exe", Category.ENTERTAINMENT),
        ("spotify.exe", Category.ENTERTAINMENT),
        ("vlc.exe", Category.ENTERTAINMENT),
        ("ffplay.exe", Category.ENTERTAINMENT),
        ("valorant.exe", Category.ENTERTAINMENT),
        ("msedge.exe", Category.BROWSER),
        ("chrome.exe", Category.BROWSER),
        ("firefox.exe", Category.BROWSER),
        ("algumcoisa.exe", Category.UNKNOWN),
        (None, Category.UNKNOWN),
    ],
)
def test_classifica_apps_conhecidos(app, esperado):
    assert classify_app(app) is esperado


def test_microfone_e_sempre_a_pessoa():
    """O filtro vale para o áudio do sistema; o mic é a própria pessoa."""
    assert classify_app("netflix.exe", source="mic") is Category.MICROPHONE
    assert classify_app(None, source="mic") is Category.MICROPHONE


def test_reuniao_com_musica_ao_fundo_conta_como_conversa():
    """A regra escolhida: basta um app de conversa para valer.

    Perder uma reunião porque havia Spotify tocando seria o erro mais caro.
    """
    assert classify_app("teams.exe+spotify.exe") is Category.CONVERSATION
    assert classify_app("spotify.exe+zoom.exe") is Category.CONVERSATION


def test_so_entretenimento_continua_entretenimento():
    assert classify_app("spotify.exe+vlc.exe") is Category.ENTERTAINMENT


def test_entretenimento_fica_fora_do_relatorio():
    """O ponto de tudo isto."""
    assert is_report_worthy(Category.ENTERTAINMENT) is False
    assert is_report_worthy(Category.CONVERSATION) is True
    assert is_report_worthy(Category.MICROPHONE) is True


def test_navegador_entra_por_padrao():
    """msedge pode ser Meet ou Netflix — na dúvida, incluir.

    Omitir uma reunião é pior que incluir um vídeo.
    """
    assert is_report_worthy(Category.BROWSER) is True
    assert is_report_worthy(Category.BROWSER, include_browser=False) is False


def test_desconhecido_entra():
    """App não catalogado pode ser uma ferramenta de trabalho."""
    assert is_report_worthy(Category.UNKNOWN) is True


def test_rotulos_em_portugues():
    assert label(Category.ENTERTAINMENT) == "entretenimento"
    assert label(Category.CONVERSATION) == "conversa"
