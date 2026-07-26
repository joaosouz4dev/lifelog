"""Lançador do servidor para a Tarefa Agendada.

Existe para ser um alvo direto do `pythonw.exe`. Registrar
`powershell -Command "...uvicorn..."` na tarefa não funciona: o PowerShell
encerra quando a linha retorna e leva o uvicorn junto, deixando a tarefa com
resultado 0 e nada rodando.

Também resolve o PYTHONPATH aqui, em vez de depender do ambiente da tarefa.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    import uvicorn

    from server.config import get_config

    cfg = get_config()
    host = cfg.get("server.host", "0.0.0.0")
    port = int(cfg.get("server.port", 8000))

    # Log em arquivo: sob pythonw não há console para onde escrever, e sem
    # isso qualquer falha de inicialização seria invisível.
    log_dir = cfg.resolve_path("server.data_dir", "./data")
    log_dir.mkdir(parents=True, exist_ok=True)

    uvicorn.run(
        "server.main:app",
        host=host,
        port=port,
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s"}
            },
            "handlers": {
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": str(log_dir / "server.log"),
                    "maxBytes": 5_000_000,
                    "backupCount": 3,
                    "encoding": "utf-8",
                    "formatter": "default",
                }
            },
            "root": {"handlers": ["file"], "level": "INFO"},
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
