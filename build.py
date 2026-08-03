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
    # O pacote `server` inteiro: o PyInstaller segue imports estáticos, e
    # tray.py (o ponto de entrada) não importa o servidor — sem isto o
    # LifelogServer.exe sobe e morre com ModuleNotFoundError.
    "server",
    "server.main",
    "server.config",
    "server.db",
    "server.models",
    "server.classify",
    "server.hub",
    "server.hub.stt",
    "server.hub.llm",
    "server.pipeline",
    "server.pipeline.ingest",
    "server.pipeline.transcribe",
    "server.reports",
    "server.reports.builder",
    "server.reports.generator",
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
    # O faster_whisper baixa o modelo do HuggingFace na primeira transcrição e
    # usa estes por dentro. Sem eles o bundle sobe, aceita áudio e falha em
    # toda transcrição com "No module named 'requests'".
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "huggingface_hub",
    "tokenizers",
    "tqdm",
    "filelock",
    "fsspec",
    "packaging",
    "yaml",
    "pycaw",
    "comtypes",
    "pyaudiowpatch",
    "win32event",
    "win32api",
    "winerror",
    # Fallback do ditado para textos longos: cola via clipboard em vez de
    # digitar caractere a caractere.
    "win32clipboard",
    # A janela nativa: o pywebview escolhe o backend por string em runtime, e
    # no Windows o backend é o WebView2 via clr/pythonnet.
    "webview",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    # Encerra o servidor de versão antiga preso na porta após uma atualização.
    "psutil",
]

# Os executáveis gerados, na ordem: o primeiro recebe as dependências e os
# demais são fundidos na pasta dele.
TARGETS = [
    ("Lifelog", "windows-client/tray.py"),          # a bandeja
    ("LifelogServer", "scripts/run_server.py"),     # o servidor
    ("LifelogUI", "windows-client/window.py"),      # a janela da interface
]

# Pacotes que não fazem parte do Lifelog mas aparecem no Python do
# desenvolvedor por causa de outros projetos. O hook do torch puxa o
# tensorboard, que quebra o build com a versão de protobuf instalada — e nada
# disso é usado aqui: a transcrição roda em CTranslate2, o VAD em ONNX.
# Excluir também derruba centenas de MB do instalador.
EXCLUDES = [
    "torch",
    "torchvision",
    "torchaudio",
    "tensorboard",
    "tensorflow",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "notebook",
    "pytest",
    "insightface",
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
    for module in EXCLUDES:
        args += ["--exclude-module", module]

    # Varre o pacote inteiro em vez de depender da lista acima ficar completa:
    # um módulo novo em server/ passaria despercebido até alguém rodar o .exe.
    args += ["--collect-submodules", "server"]

    # Os módulos do cliente são importados por nome dentro de funções (para
    # adiar o custo), o que o PyInstaller não enxerga na análise estática.
    # `text_input` não se chama `typer` de propósito: existe um pacote `typer`
    # no PyPI, dependência do FastAPI, e o import resolvia para ele — o ditado
    # transcrevia e o texto sumia sem erro visível.
    for modulo in ("text_input", "sounds", "hotkey", "dictation", "window_title"):
        args += ["--hidden-import", modulo]

    # Estes carregam submódulos e arquivos de dados por caminho em runtime, o
    # que o PyInstaller não enxerga. Listar hidden-imports um a um já falhou
    # (faltou `requests` e toda transcrição quebrou); --collect-all pega tudo.
    for pkg in ("faster_whisper", "huggingface_hub", "requests", "certifi", "tokenizers"):
        args += ["--collect-all", pkg]
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

    # A bandeja primeiro: é o executável principal, e os outros entram na
    # mesma pasta reaproveitando as dependências.
    for name, entry in TARGETS:
        run(pyinstaller_args(name, entry, windowed=True))

    # O PyInstaller cria uma pasta por executável; junta todas na primeira
    # para o instalador empacotar um diretório só.
    tray_dir = DIST / TARGETS[0][0]
    for name, _ in TARGETS[1:]:
        extra = DIST / name
        if not extra.exists():
            continue
        for item in extra.iterdir():
            target = tray_dir / item.name
            if target.exists():
                continue  # dependência já veio no build da bandeja
            shutil.move(str(item), str(target))
        shutil.rmtree(extra, ignore_errors=True)

    for name, _ in TARGETS:
        if not (tray_dir / f"{name}.exe").exists():
            raise SystemExit(f"executável não gerado: {tray_dir / f'{name}.exe'}")

    total = sum(f.stat().st_size for f in tray_dir.rglob("*") if f.is_file())
    print(f"\npronto: {tray_dir}  ({total / 1_000_000:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
