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

# Fora do bundle, o pacote `server` está um nível acima deste script. Sob
# PyInstaller ele já vem embutido e sys.path aponta para o bundle.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    import uvicorn

    from server.config import get_config

    # Importa a aplicação em vez de passar "server.main:app" como string: o
    # uvicorn resolveria essa string por import dinâmico, que não funciona
    # dentro do bundle do PyInstaller. Com o objeto, `reload` deixa de estar
    # disponível — irrelevante aqui, isto é produção.
    from server.main import app

    cfg = get_config()
    host = cfg.get("server.host", "0.0.0.0")
    port = int(cfg.get("server.port", 8000))

    # Log em arquivo: sob pythonw (e no .exe empacotado) não há console para
    # onde escrever, e sem isso qualquer falha de inicialização é invisível.
    log_dir = cfg.resolve_path("server.data_dir", "./data")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / ".escrita-ok").touch()
        (log_dir / ".escrita-ok").unlink()
    except OSError as exc:
        # Sem lugar para escrever, subir mesmo assim é melhor que não subir —
        # o servidor é o que importa, o log é diagnóstico.
        print(f"aviso: {log_dir} não é gravável ({exc}); log só no console")
        uvicorn.run(app, host=host, port=port)
        return 0

    uvicorn.run(
        app,
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
