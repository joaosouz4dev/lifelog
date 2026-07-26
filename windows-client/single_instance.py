"""Garante uma única instância da captura.

Duas bandejas rodando disputam o mesmo dispositivo de áudio e a mesma fila
local: o WASAPI entrega o mesmo som às duas, cada uma grava sua cópia, e a
fila SQLite passa a ter dois escritores. O sintoma é captura irregular, que
parece "não está gravando".

Usa um mutex nomeado do Windows: o kernel garante unicidade por sessão de
usuário, e o mutex desaparece sozinho se o processo morrer — um arquivo de
lock ficaria órfão depois de um crash e travaria a próxima abertura.
"""

from __future__ import annotations

import logging

log = logging.getLogger("single_instance")

# Global\ valeria para a máquina inteira; Local\ é por sessão de usuário, que
# é o escopo certo: dois usuários no mesmo PC podem gravar em paralelo.
MUTEX_NAME = "Local\\LifelogCaptureSingleton"

_handle = None


def acquire() -> bool:
    """Tenta virar a instância única. False se já houver outra rodando.

    Fora do Windows (ou sem pywin32) devolve True: sem o mecanismo, bloquear
    a execução seria pior que o risco de duas instâncias.
    """
    global _handle

    try:
        import win32api
        import win32event
        import winerror
    except ImportError:
        log.debug("pywin32 ausente — controle de instância única desativado")
        return True

    try:
        _handle = win32event.CreateMutex(None, True, MUTEX_NAME)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            _handle = None
            return False
        return True
    except Exception:
        log.debug("falha ao criar o mutex", exc_info=True)
        return True


def release() -> None:
    """Libera o mutex. O Windows também o libera se o processo morrer."""
    global _handle
    if _handle is None:
        return
    try:
        import win32api

        win32api.CloseHandle(_handle)
    except Exception:
        pass
    _handle = None
