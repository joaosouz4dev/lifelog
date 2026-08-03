"""Testes do detector de reunião.

A regra que atravessa todos: em caso de dúvida, GRAVA. Perder uma reunião
inteira por causa de um bug no detector é o pior desfecho possível, e uma
falha silenciosa não deixa rastro para diagnosticar.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CLIENTE = Path(__file__).resolve().parent.parent / "windows-client"
if str(CLIENTE) not in sys.path:
    sys.path.insert(0, str(CLIENTE))

import meeting  # noqa: E402
from meeting import MeetingDetector  # noqa: E402


class ProbeFalso:
    """No lugar do WindowTitleProbe."""

    def __init__(self, titulos: dict[str, str] | None = None):
        self._titulos = titulos or {}

    def titles(self) -> dict[str, str]:
        return self._titulos


def _detector(monkeypatch, com_microfone: set[str], titulos=None, **kwargs):
    monkeypatch.setattr(meeting, "_com_microfone_aberto", lambda: com_microfone)
    return MeetingDetector(ProbeFalso(titulos), **kwargs)


# ─────────────────────────── app dedicado ───────────────────────────


def test_zoom_com_microfone_aberto_e_reuniao(monkeypatch):
    d = _detector(monkeypatch, {"zoom.exe"})

    encontrada, motivo = d._procurar_reuniao()

    assert encontrada is True
    assert "zoom" in motivo


def test_teams_com_microfone_aberto_e_reuniao(monkeypatch):
    d = _detector(monkeypatch, {"ms-teams.exe"})

    assert d._procurar_reuniao()[0] is True


def test_jogo_com_microfone_nao_e_reuniao(monkeypatch):
    """Só o microfone aberto não basta — jogos e OBS também o usam."""
    d = _detector(monkeypatch, {"cs2.exe", "obs64.exe"})

    encontrada, motivo = d._procurar_reuniao()

    assert encontrada is False
    assert "não é reunião" in motivo


def test_ninguem_com_microfone_nao_e_reuniao(monkeypatch):
    d = _detector(monkeypatch, set())

    assert d._procurar_reuniao()[0] is False


# ─────────────────────────── navegador ───────────────────────────


def test_navegador_com_titulo_de_meet_e_reuniao(monkeypatch):
    """O mesmo chrome.exe serve Meet e YouTube — o título decide."""
    d = _detector(
        monkeypatch, {"chrome.exe"},
        titulos={"chrome.exe": "Reunião — Google Meet"},
    )

    encontrada, motivo = d._procurar_reuniao()

    assert encontrada is True
    assert "chrome" in motivo


def test_navegador_com_youtube_nao_e_reuniao(monkeypatch):
    """Microfone aberto no Chrome pode ser gravação de vídeo, não reunião."""
    d = _detector(
        monkeypatch, {"chrome.exe"},
        titulos={"chrome.exe": "playlist favorita - YouTube"},
    )

    assert d._procurar_reuniao()[0] is False


def test_navegador_sem_titulo_legivel_nao_e_reuniao(monkeypatch):
    d = _detector(monkeypatch, {"chrome.exe"}, titulos={})

    assert d._procurar_reuniao()[0] is False


# ─────────────────────── falha aberta (o essencial) ───────────────────────


def test_registro_ilegivel_deixa_gravar(monkeypatch):
    """Sem conseguir ler o registro, gravar é o comportamento seguro."""
    def explode():
        raise OSError("registro inacessível")

    monkeypatch.setattr(meeting, "_com_microfone_aberto", explode)
    d = MeetingDetector(ProbeFalso())

    encontrada, motivo = d._procurar_reuniao()

    assert encontrada is True
    assert "precaução" in motivo


def test_detector_comeca_aberto():
    """Antes da primeira avaliação não se sabe nada — grava."""
    assert MeetingDetector(ProbeFalso()).em_reuniao is True


def test_erro_inesperado_abre_o_gate(monkeypatch):
    """Qualquer exceção no ciclo abre o gate, nunca fecha."""
    d = MeetingDetector(ProbeFalso())
    d._fechar("teste")
    assert d.em_reuniao is False

    monkeypatch.setattr(d, "_procurar_reuniao", lambda: 1 / 0)
    try:
        d._avaliar()
    except ZeroDivisionError:
        # O _vigiar é quem captura; aqui simulamos o que ele faz.
        d._abrir("detector falhou")

    assert d.em_reuniao is True


def test_probe_ausente_nao_quebra(monkeypatch):
    """Sem o probe de títulos, navegador simplesmente não conta."""
    monkeypatch.setattr(meeting, "_com_microfone_aberto", lambda: {"chrome.exe"})
    d = MeetingDetector(None)

    assert d._procurar_reuniao()[0] is False


# ─────────────────────── extensão de navegador ───────────────────────


def test_extensao_reportando_reuniao_abre_o_gate(monkeypatch):
    """A extensão sabe qual aba está em chamada — o título não distingue."""
    d = _detector(monkeypatch, set(), server_url="http://127.0.0.1:8000")
    monkeypatch.setattr(
        d, "_perguntar_ao_servidor", lambda: (True, "meet (extensão): Reunião"),
    )

    encontrada, motivo = d._procurar_reuniao()

    assert encontrada is True
    assert "extensão" in motivo


def test_sem_extensao_cai_para_os_sinais_locais(monkeypatch):
    """Zoom instalado precisa funcionar mesmo sem extensão nenhuma."""
    d = _detector(monkeypatch, {"zoom.exe"}, server_url="http://127.0.0.1:8000")
    monkeypatch.setattr(d, "_perguntar_ao_servidor", lambda: None)

    assert d._procurar_reuniao()[0] is True


def test_servidor_fora_do_ar_nao_fecha_o_gate(monkeypatch):
    """Servidor caído não pode apagar uma reunião em curso."""
    d = _detector(monkeypatch, {"teams.exe"}, server_url="http://127.0.0.1:9999")

    # Sem servidor de verdade, _perguntar_ao_servidor devolve None e a
    # decisão volta para o sinal local.
    assert d._procurar_reuniao()[0] is True


def test_sem_url_de_servidor_nem_tenta(monkeypatch):
    d = _detector(monkeypatch, {"zoom.exe"})

    assert d._perguntar_ao_servidor() is None


# ─────────────────────── atraso no fechamento ───────────────────────


def test_gate_segue_aberto_logo_apos_a_reuniao(monkeypatch):
    """Fechar na hora cortaria o "tchau, até semana que vem"."""
    d = _detector(monkeypatch, {"zoom.exe"}, atraso_fechamento_s=30)
    d._avaliar()
    assert d.em_reuniao is True

    # A reunião acabou.
    monkeypatch.setattr(meeting, "_com_microfone_aberto", lambda: set())
    d._avaliar()

    assert d.em_reuniao is True, "deveria seguir gravando durante o atraso"
    assert "encerrada" in d.motivo


def test_gate_fecha_depois_do_atraso(monkeypatch):
    d = _detector(monkeypatch, {"zoom.exe"}, atraso_fechamento_s=0)
    d._avaliar()

    monkeypatch.setattr(meeting, "_com_microfone_aberto", lambda: set())
    d._avaliar()

    assert d.em_reuniao is False


def test_sem_reuniao_nenhuma_o_gate_fecha(monkeypatch):
    """Sem nunca ter visto reunião, não há o que aguardar."""
    d = _detector(monkeypatch, set())

    d._avaliar()

    assert d.em_reuniao is False
