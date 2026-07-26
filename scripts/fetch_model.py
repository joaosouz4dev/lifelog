"""Baixa um modelo do Hugging Face com retomada.

O downloader do huggingface_hub vinha morrendo em silêncio no meio de arquivos
grandes (o large-v3 tem 3 GB). Este script baixa por HTTP com Range, retoma de
onde parou e insiste em cima de quedas de conexão — depois entrega os arquivos
no layout de cache que o faster-whisper espera.

    python scripts/fetch_model.py large-v3
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

REPOS = {
    "large-v3": "Systran/faster-whisper-large-v3",
    "medium": "Systran/faster-whisper-medium",
    "small": "Systran/faster-whisper-small",
    "base": "Systran/faster-whisper-base",
}

FILES = [
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.json",
    "preprocessor_config.json",
]

CHUNK = 1024 * 1024
MAX_RETRIES = 100
CACHE = Path.home() / ".cache" / "huggingface" / "hub"


def download(url: str, dest: Path) -> None:
    """Baixa `url` para `dest`, retomando se o arquivo parcial já existir."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")

    with httpx.Client(follow_redirects=True, timeout=60) as client:
        # HEAD no HF costuma vir sem content-length; o tamanho real aparece no
        # GET em streaming, então descobrimos lá dentro.
        total = 0

        for attempt in range(MAX_RETRIES):
            have = partial.stat().st_size if partial.exists() else 0
            if total and have >= total:
                break

            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                with client.stream("GET", url, headers=headers) as response:
                    # 416 = pedimos além do fim: o arquivo já está inteiro.
                    if response.status_code == 416:
                        break
                    if response.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {response.status_code}")

                    if not total:
                        length = int(response.headers.get("content-length", 0))
                        if response.status_code == 206:
                            content_range = response.headers.get("content-range", "")
                            total = int(content_range.rsplit("/", 1)[-1] or 0)
                        else:
                            total = length

                    mode = "ab" if have and response.status_code == 206 else "wb"
                    if mode == "wb":
                        have = 0

                    started, last_report = time.time(), time.time()
                    with open(partial, mode) as fh:
                        for chunk in response.iter_bytes(CHUNK):
                            fh.write(chunk)
                            have += len(chunk)
                            if time.time() - last_report >= 5:
                                speed = have / (time.time() - started) / 1024 / 1024
                                pct = f"{have * 100 / total:.0f}%" if total else "?"
                                print(
                                    f"  {dest.name}: {have / 1e6:.0f}/{total / 1e6:.0f} MB "
                                    f"({pct}) {speed:.1f} MB/s",
                                    flush=True,
                                )
                                last_report = time.time()
            except Exception as exc:
                # Queda de conexão é esperada em arquivos deste tamanho; o
                # próximo laço retoma exatamente de onde parou.
                print(f"  ...retomando após {type(exc).__name__} "
                      f"(tentativa {attempt + 1})", flush=True)
                time.sleep(min(2**attempt, 30))
                continue

            if total and partial.stat().st_size >= total:
                break

    if not partial.exists():
        raise RuntimeError(f"{dest.name}: nada foi baixado")
    if total and partial.stat().st_size < total:
        raise RuntimeError(f"{dest.name} incompleto: {partial.stat().st_size}/{total}")

    partial.replace(dest)
    print(f"  {dest.name}: OK ({dest.stat().st_size / 1e6:.0f} MB)", flush=True)


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "large-v3"
    repo = REPOS.get(name)
    if repo is None:
        print(f"modelo desconhecido: {name}. Opções: {', '.join(REPOS)}")
        return 1

    # Layout que o huggingface_hub procura ao resolver um modelo local.
    target = CACHE / f"models--{repo.replace('/', '--')}" / "snapshots" / "manual"
    target.mkdir(parents=True, exist_ok=True)

    print(f"baixando {repo} -> {target}\n")
    for filename in FILES:
        dest = target / filename
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  {filename}: já existe, pulando")
            continue
        download(f"https://huggingface.co/{repo}/resolve/main/{filename}", dest)

    print(f"\npronto. Use com: WhisperModel(r'{target}')")
    print(f"ou aponte o config.yaml para: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
