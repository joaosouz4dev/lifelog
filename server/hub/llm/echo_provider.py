"""Provedor de demonstração — não usa modelo de linguagem nenhum.

Monta um relatório a partir da própria transcrição, sem resumir nem
interpretar. Serve para ver a interface funcionando e conferir o formato antes
de gastar tokens, e como último recurso quando nenhum provedor real responde:
melhor um índice do dia do que nada.

Ative com `type: echo` no config.yaml.
"""

from __future__ import annotations

import re
from typing import Any

from ...models import Completion


def _extract_header(prompt: str) -> tuple[str, str]:
    """Separa o cabeçalho factual do corpo da transcrição."""
    parts = prompt.split("\n\n---\n\n", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", prompt)


class EchoProvider:
    name: str
    requires_network = False

    def __init__(self, name: str, cfg: dict[str, Any] | None = None):
        self.name = name

    async def complete(
        self, prompt: str, *, system: str | None = None, max_tokens: int | None = None
    ) -> Completion:
        header, body = _extract_header(prompt)

        blocks = [b.strip() for b in body.split("\n\n") if b.strip()]
        lines = ["# Relatório (sem resumo)", ""]

        if header:
            lines += [f"*{line}*" for line in header.splitlines() if line.strip()]
            lines.append("")

        lines += [
            "Nenhum provedor de linguagem está ativo, então este relatório é",
            "apenas a transcrição organizada — sem resumo, temas ou pendências.",
            "Configure `llm.providers` no config.yaml para ter o relatório de verdade.",
            "",
            "## O que foi capturado",
            "",
        ]

        for block in blocks[:60]:
            # Cada bloco chega como "[HH:MM] (fonte) texto".
            match = re.match(r"^\[(\d{2}:\d{2})\]\s*\(([^)]+)\)\s*(.*)$", block, re.S)
            if match:
                hora, fonte, texto = match.groups()
                texto = " ".join(texto.split())
                lines.append(f"- **{hora}** ({fonte}) {texto}")
            else:
                lines.append(f"- {' '.join(block.split())}")

        if len(blocks) > 60:
            lines += ["", f"*…e mais {len(blocks) - 60} blocos.*"]

        text = "\n".join(lines)
        return Completion(
            text=text,
            provider=self.name,
            model="echo",
            tokens_in=len(prompt) // 4,
            tokens_out=len(text) // 4,
            cost_cents=0.0,
        )

    async def health(self) -> bool:
        return True

    def estimate_cost_cents(self, tokens_in: int, tokens_out: int) -> float:
        return 0.0
