"""Testes da descarga do modelo por ociosidade.

O `large-v3` ocupa ~5 GB de VRAM; numa placa de 8 GB isso não deixa margem
para mais nada enquanto ninguém fala.
"""

from __future__ import annotations

import asyncio
import time

from server.hub.stt.faster_whisper_provider import FasterWhisperProvider


class ModeloFalso:
    """Fica no lugar do WhisperModel — carregar o de verdade custa ~7s."""


def _provedor(monkeypatch, idle: float = 0.3) -> FasterWhisperProvider:
    p = FasterWhisperProvider("whisper_teste", {"idle_unload_seconds": idle})
    monkeypatch.setattr(p, "_load_model_sync", ModeloFalso)
    return p


def test_modelo_e_reaproveitado_entre_chamadas(monkeypatch):
    """Recarregar a cada segmento pagaria ~7s por fala."""
    p = _provedor(monkeypatch)

    async def cenario():
        return await p._get_model(), await p._get_model()

    a, b = asyncio.run(cenario())
    assert a is b


def test_modelo_sai_da_memoria_depois_do_tempo_ocioso(monkeypatch):
    p = _provedor(monkeypatch)

    async def cenario():
        await p._get_model()
        assert p._model is not None
        await asyncio.sleep(0.9)  # acima do idle_unload_seconds
        return p._model

    assert asyncio.run(cenario()) is None, "o modelo deveria ter sido descarregado"


def test_uso_recente_adia_a_descarga(monkeypatch):
    """Uma pausa entre frases não pode custar recarga."""
    p = _provedor(monkeypatch)

    async def cenario():
        await p._get_model()
        # Fala intermitente: pausas curtas, com uso marcado entre elas, como
        # o transcribe() faz ao terminar.
        for _ in range(4):
            await asyncio.sleep(0.2)
            p._last_used = time.monotonic()
        return p._model

    assert asyncio.run(cenario()) is not None, "não deveria sair enquanto está em uso"


def test_zero_desativa_a_descarga(monkeypatch):
    """Em máquina com VRAM sobrando, recarregar só atrapalharia."""
    p = _provedor(monkeypatch, idle=0)

    async def cenario():
        await p._get_model()
        await asyncio.sleep(0.5)
        return p._model, p._idle_task

    modelo, vigia = asyncio.run(cenario())
    assert modelo is not None
    assert vigia is None, "não deveria nem subir o vigia"


def test_recarrega_sozinho_depois_de_descarregar(monkeypatch):
    p = _provedor(monkeypatch)

    async def cenario():
        primeiro = await p._get_model()
        await asyncio.sleep(0.9)
        assert p._model is None
        return primeiro, await p._get_model()

    primeiro, segundo = asyncio.run(cenario())
    assert segundo is not None
    assert segundo is not primeiro, "deveria ser uma instância nova"
