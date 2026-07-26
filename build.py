"""Empacota o Lifelog em executáveis com PyInstaller.

Gera dois programas numa pasta só:
  Lifelog.exe        — a bandeja, que é o que o usuário abre
  LifelogServer.exe  — o servidor, iniciado pela bandeja ou pela tarefa

Ficam no mesmo diretório para compartilharem as dependências (torch, onnx e
companhia pesam demais para duplicar).

    python build.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# Módulos que o PyInstaller não descobre sozinho: uvicorn escolhe o loop e o
# protocolo por string em runtime, e o faster_whisper carrega o backend do
# CTranslate2 dinamicamente.
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "pycaw",
    "comtypes",
    "pyaudiowpatch",
]

# Arquivos que precisam viajar junto: a interface web, a configuração padrão,
# os prompts dos relatórios e os ícones.
DATAS = [
    ("server/web", "server/web"),
    ("server/reports/prompts", "server/reports/prompts"),
    ("config.yaml", "."),
    ("assets", "assets"),
]


def run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}\n", flush=True)
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"falhou: {' '.join(cmd[:3])}…")


def pyinstaller_args(name: str, entry: str, *, windowed: bool) -> list[str]:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", name,
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--icon", str(ROOT / "assets" / "icon.ico"),
        "--paths", str(ROOT),
        "--paths", str(ROOT / "windows-client"),
    ]
    # A bandeja é --windowed (sem console); o servidor também, porque roda em
    # segundo plano e um terminal preto na tela seria ruído.
    args.append("--windowed" if windowed else "--console")

    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    # Caminho absoluto: com --specpath apontando para build/, o PyInstaller
    # resolve os --add-data relativos a partir de lá e não encontra nada.
    for src, dest in DATAS:
        args += ["--add-data", f"{ROOT / src};{dest}"]

    args.append(entry)
    return args


def main() -> int:
    icon = ROOT / "assets" / "icon.ico"
    if not icon.exists():
        print("ícone ausente — gerando…")
        run([sys.executable, "scripts/make_icon.py"])

    for path in (DIST, BUILD):
        if path.exists():
            shutil.rmtree(path)

    # A bandeja primeiro: é o executável principal, e o segundo build entra
    # na mesma pasta reaproveitando as dependências.
    run(pyinstaller_args("Lifelog", "windows-client/tray.py", windowed=True))
    run(pyinstaller_args("LifelogServer", "scripts/run_server.py", windowed=True))

    # O PyInstaller cria dist/Lifelog/ e dist/LifelogServer/; junta os dois
    # para o instalador empacotar uma pasta só.
    tray_dir = DIST / "Lifelog"
    server_dir = DIST / "LifelogServer"
    if server_dir.exists():
        for item in server_dir.iterdir():
            target = tray_dir / item.name
            if target.exists():
                continue  # dependência já veio no build da bandeja
            shutil.move(str(item), str(target))
        shutil.rmtree(server_dir, ignore_errors=True)

    exe = tray_dir / "Lifelog.exe"
    server_exe = tray_dir / "LifelogServer.exe"
    for path in (exe, server_exe):
        if not path.exists():
            raise SystemExit(f"executável não gerado: {path}")

    total = sum(f.stat().st_size for f in tray_dir.rglob("*") if f.is_file())
    print(f"\npronto: {tray_dir}  ({total / 1_000_000:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
