"""Testes do ditado: buffer de áudio e parsing do atalho.

O que exige sessão interativa (o hook de teclado, a digitação de verdade)
fica para a verificação manual.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

CLIENTE = Path(__file__).resolve().parent.parent / "windows-client"
if str(CLIENTE) not in sys.path:
    sys.path.insert(0, str(CLIENTE))

from dictation import SAMPLE_RATE, DictationTap  # noqa: E402


def _fala(segundos: float) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * segundos), dtype=np.float32)


# ──────────────────────────────── buffer ────────────────────────────────


def test_so_acumula_enquanto_o_ditado_esta_ativo():
    tap = DictationTap()

    tap.alimentar(_fala(1.0))  # fora do ciclo: ignorado pela trilha
    assert tap.ativo is False

    tap.comecar()
    tap.alimentar(_fala(1.0))

    audio = tap.terminar()
    assert audio is not None
    assert audio.size == SAMPLE_RATE


def test_mantem_falas_curtas_que_o_vad_descartaria():
    """Um "sim" de 300ms morreria no min_speech_ms de 700ms."""
    tap = DictationTap()
    tap.comecar()
    tap.alimentar(_fala(0.3))

    audio = tap.terminar()

    assert audio is not None
    assert audio.size == int(SAMPLE_RATE * 0.3)


def test_soltar_a_tecla_sem_falar_nao_devolve_nada():
    """Sem isto, um toque acidental bateria no servidor à toa."""
    tap = DictationTap()
    tap.comecar()
    tap.alimentar(_fala(0.05))

    assert tap.terminar() is None


def test_respeita_o_teto_de_duracao():
    """Tecla travada não pode virar transcrição de meia hora."""
    tap = DictationTap(max_segundos=1.0)
    tap.comecar()
    for _ in range(30):
        tap.alimentar(_fala(0.5))

    audio = tap.terminar()

    assert audio.size <= SAMPLE_RATE * 1.5, "deveria ter parado de acumular"


def test_o_buffer_e_limpo_entre_ditados():
    """A segunda frase não pode vir com a primeira colada na frente."""
    tap = DictationTap()
    tap.comecar()
    tap.alimentar(_fala(2.0))
    tap.terminar()

    tap.comecar()
    tap.alimentar(_fala(0.5))
    segundo = tap.terminar()

    assert segundo.size == int(SAMPLE_RATE * 0.5)


def test_cancelar_joga_o_audio_fora():
    tap = DictationTap()
    tap.comecar()
    tap.alimentar(_fala(3.0))

    tap.cancelar()

    assert tap.ativo is False
    assert tap.terminar() is None


def test_terminar_desativa_o_tap():
    """Se continuasse ativo, a trilha seguiria desviando o áudio do lifelog."""
    tap = DictationTap()
    tap.comecar()
    tap.alimentar(_fala(1.0))

    tap.terminar()

    assert tap.ativo is False


# ──────────────────────────────── atalho ────────────────────────────────

pytest.importorskip("ctypes")


@pytest.mark.parametrize(
    ("combinacao", "esperado_tecla"),
    [
        ("ctrl+shift+space", 0x20),
        ("ctrl+alt+d", ord("D")),
        ("win+f9", 0x78),
        ("CTRL+SHIFT+ESPACO", 0x20),
    ],
)
def test_parse_reconhece_combinacoes(combinacao, esperado_tecla):
    from hotkey import parse

    _, tecla = parse(combinacao)
    assert tecla == esperado_tecla


def test_parse_soma_os_modificadores():
    from hotkey import MOD_CONTROL, MOD_SHIFT, parse

    mods, _ = parse("ctrl+shift+space")

    assert mods == MOD_CONTROL | MOD_SHIFT


@pytest.mark.parametrize("ruim", ["", "ctrl+shift", "ctrl+tecla-inexistente"])
def test_parse_recusa_combinacao_invalida(ruim):
    from hotkey import parse

    with pytest.raises(ValueError):
        parse(ruim)


# ─────────────────────────── entrega do texto ───────────────────────────


class _EntregaEspiada:
    """Substitui text_input para observar o que a entrega decidiu."""

    def __init__(self):
        self.digitado: list[str] = []
        self.copiado: list[str] = []


@pytest.fixture
def entrega(monkeypatch):
    import text_input

    espiao = _EntregaEspiada()
    monkeypatch.setattr(text_input, "digitar", lambda t: espiao.digitado.append(t) or True)
    monkeypatch.setattr(text_input, "copiar", lambda t: espiao.copiado.append(t) or True)
    return espiao


def _controlador():
    from dictation import DictationController, DictationTap

    return DictationController("http://127.0.0.1:8000", DictationTap())


def test_o_texto_vai_para_o_campo(entrega):
    """O ditado existe para escrever onde o cursor está."""
    ctrl = _controlador()

    ctrl._entregar("bom dia")

    assert entrega.digitado == ["bom dia"]
    assert ctrl.ultimo_erro is None


def test_digita_mesmo_se_o_foco_mudou(entrega):
    """Houve uma verificação de foco aqui, e ela errava na prática.

    Dentro de um mesmo programa o foco troca o tempo todo (abas, popups, a
    própria combinação de teclas), e o texto quase nunca chegava ao campo —
    ia para o clipboard. Entre proteger de um cenário raro e funcionar no
    comum, o comum ganha.
    """
    ctrl = _controlador()

    ctrl._entregar("bom dia")

    assert entrega.digitado == ["bom dia"]
    assert entrega.copiado == []


def test_falha_na_digitacao_e_registrada(entrega, monkeypatch):
    """Sem isto, uma recusa do Windows viraria silêncio outra vez."""
    import text_input

    monkeypatch.setattr(text_input, "digitar", lambda t: False)
    ctrl = _controlador()

    ctrl._entregar("bom dia")

    assert ctrl.ultimo_erro == "não foi possível digitar"
