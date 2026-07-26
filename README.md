# Lifelog

Captura contínua de microfone e áudio do sistema, transcrição automática e
relatórios diários e mensais. Roda no Windows hoje; Android e um gadget
dedicado nas próximas fases.

Tudo local por padrão: o áudio é transcrito na sua própria GPU e nada sai da
máquina a menos que você configure um provedor de nuvem.

## Como funciona

```
captura → VAD → Opus → fila local → servidor → transcrição → timeline / busca
```

O VAD (Silero) descarta silêncio antes de qualquer coisa subir para a rede.
Em teste real isso removeu **86% do áudio** mantendo toda a fala — é o que
torna viável gravar o dia inteiro sem encher o disco.

## Requisitos

- Python 3.12+
- ffmpeg no PATH
- GPU NVIDIA (opcional — sem ela o Whisper cai para CPU automaticamente)

## Instalação

```bash
pip install -r requirements.txt
```

## Uso

Servidor (transcreve e serve a interface):

```bash
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

Cliente de captura, em outro terminal:

```bash
python windows-client/main.py
```

Abra <http://localhost:8000>. Com `--host 0.0.0.0` a interface também abre no
celular pela rede local.

### Opções do cliente

```bash
python windows-client/main.py --list-devices    # diagnóstico de áudio
python windows-client/main.py --source mic      # só microfone
python windows-client/main.py --source system   # só áudio do sistema
python windows-client/main.py --server http://192.168.0.10:8000
```

Ctrl+C encerra com segurança: o segmento em curso é salvo e a fila local
permanece intacta para o próximo início.

## Configuração

`config.yaml` traz os padrões. Para ajustes locais crie `config.local.yaml`
(fora do controle de versão) com apenas o que quiser sobrepor:

```yaml
stt:
  providers:
    faster_whisper_local:
      model: small        # mais rápido; large-v3 é o padrão e acerta mais
```

Segredos nunca vão para o YAML — use variáveis de ambiente:

```yaml
api_key: ${DEEPGRAM_API_KEY}
```

### O hub de provedores

Transcrição e LLM são cadeias de provedores com fallback. Se o primeiro falhar,
o próximo assume automaticamente; um provedor que falha repetidamente entra em
circuit breaker e é pulado por um tempo.

```yaml
stt:
  chain: [faster_whisper_local, deepgram]
  daily_budget_cents: 200

llm:
  chain: [claude, ollama]
  daily_budget_cents: 500
```

Os dois hubs têm tetos independentes. Todo custo é registrado e visível em
`GET /api/hub/stt` e `GET /api/hub/llm`, e no botão **Hub** da interface. Ao
atingir o teto diário, provedores pagos são pulados e os locais continuam
trabalhando.

Trocar de provedor é uma linha de configuração — nenhum código muda.

Para ativar o Claude nos relatórios:

```bash
setx ANTHROPIC_API_KEY "sua-chave"
```

e mude `enabled: false` para `true` no bloco `llm.providers.claude`. Um
relatório diário custa cerca de 18 centavos de dólar com `claude-sonnet-5`.

## Privacidade

Gravar áudio do sistema captura vozes de terceiros. No Brasil, gravar conversa
da qual você participa é legal (STF, RE 583937), mas armazenar e transcrever
terceiros pede cuidado sob a LGPD. O projeto oferece:

- `retention.audio_days` — apaga o áudio após N dias, preservando a transcrição
- `POST /api/retention/purge` — aplica a retenção sob demanda
- Transcrição 100% local por padrão
- Ctrl+C encerra a captura a qualquer momento

Ainda não implementado: `capture.windows_blocklist` (apps cujo áudio nunca é
capturado) está previsto para a Fase 4 — a chave existe no `config.yaml` mas
nada a lê ainda.

O diretório `data/` está no `.gitignore`. Nunca versione áudio ou transcrições.

## Testes

```bash
python -m pytest tests/ -q
```

Cobrem o fallback do hub, o circuit breaker, o teto de gasto, a idempotência da
ingestão, a retenção, a fila local (incluindo servidor offline e reinício) e o
VAD com fala real.

## Estrutura

```
server/          FastAPI, banco, hub de provedores, pipeline
  hub/           interface de provedores, fallback, custos
  pipeline/      ingestão e worker de transcrição
  web/           interface (timeline, player, busca)
windows-client/  captura WASAPI, VAD, fila local, upload
protocol/        contrato de ingestão — base para Android e gadget
tests/           testes automatizados
```

## Fases

- **Fase 1 — concluída.** Servidor, hub de STT, cliente Windows, timeline e busca.
- **Fase 2 — Android.** React Native + módulo Kotlin. Ver as restrições reais do
  sistema em [protocol/ingest.md](protocol/ingest.md).
- **Fase 3 — Inteligência.** Hub de LLM pronto (Claude + Ollama, com fallback);
  faltam os relatórios diários e mensais, a busca semântica e o chat.
- **Fase 4 — Refinamento.** Diarização, dashboard de custos, blocklist ativa.
- **Fase 5 — Gadget.** ESP32-S3 com microfone I2S. O protocolo já está pronto.
