# Protocolo de ingestão

Contrato entre qualquer cliente de captura e o servidor. O cliente Windows, o
app Android (Fase 2) e o gadget ESP32 (Fase 5) falam exatamente este protocolo —
o servidor não distingue entre eles, só pelo campo `device_id`.

Implementar um cliente novo significa implementar este documento. Nada mais.

## Princípios

1. **O cliente decide o que é fala.** O VAD roda no cliente, não no servidor.
   Silêncio nunca entra na rede. Em teste real isso descartou 86% do áudio.
2. **O cliente nunca perde dados.** Fila local em disco, envio com retry. Se o
   servidor estiver fora do ar por horas, nada se perde.
3. **Reenviar é seguro.** `client_uid` torna toda ingestão idempotente.
4. **O servidor transcreve.** O cliente só captura, corta e envia.

## Endpoint

```
POST /ingest
Content-Type: multipart/form-data
```

Dois campos obrigatórios:

| Campo   | Tipo   | Conteúdo                          |
|---------|--------|-----------------------------------|
| `audio` | binário| Segmento de fala em Opus (Ogg)    |
| `meta`  | texto  | JSON com os metadados (abaixo)    |

### `meta`

```json
{
  "device_id": "win-russo",
  "source": "mic",
  "started_at": "2026-07-25T14:32:07.412000",
  "duration_ms": 5100,
  "client_uid": "mic-a150a8a9e8d640af",
  "app_name": "chrome.exe",
  "sample_rate": 16000
}
```

| Campo         | Obrigatório | Regra |
|---------------|-------------|-------|
| `device_id`   | sim | 1–64 caracteres. Estável por dispositivo. Use o prefixo da plataforma por convenção (`win-`, `android-`, `gadget-`) — o servidor não o interpreta, mas ele torna a origem legível na interface. |
| `source`      | sim | `mic`, `system` ou `gadget`. |
| `started_at`  | sim | ISO 8601 do início da fala. **Envie com offset** (`2026-07-25T14:32:07-03:00`); ver "Fuso horário" abaixo. |
| `duration_ms` | sim | Inteiro, 1 a 600000 (10 min). |
| `client_uid`  | sim | 8–64 caracteres, único e **estável entre retentativas**. |
| `app_name`    | não | App que originou o áudio, quando o cliente souber. |
| `sample_rate` | não | 8000–48000. Padrão 16000. Validado mas ainda não persistido — o servidor assume que o áudio está em 16 kHz. |

### Fuso horário

Clientes devem enviar `started_at` **com offset**. O servidor converte para a
hora local dele e armazena sem offset, de modo que celular viajando, gadget com
relógio próprio e PC caiam todos no mesmo eixo de tempo.

Timestamps sem offset são aceitos e tratados como já sendo hora local do
servidor — é o que o cliente Windows envia hoje, por rodar na mesma máquina.
Para qualquer cliente remoto, omitir o offset agrupa os segmentos no dia errado.

`client_uid` precisa ser gerado **uma vez, ao enfileirar** — nunca a cada
tentativa de envio. Se mudar entre retentativas, a idempotência quebra e o
segmento duplica.

### Formato do áudio

| Propriedade | Valor |
|-------------|-------|
| Codec | Opus em container Ogg |
| Taxa | 16000 Hz |
| Canais | 1 (mono) |
| Bitrate | ~24 kbps (transparente para voz) |
| Tamanho máximo | 50 MB |

Um segmento de 5 s ocupa ~15 KB. Mono 16 kHz é o que o Whisper consome
internamente — enviar mais que isso é desperdício de banda.

O encode não tem solução compartilhada entre plataformas; cada cliente usa o
que tem à mão:

| Plataforma | Encoder |
|------------|---------|
| Windows | `ffmpeg` via subprocess (exige ffmpeg no PATH) |
| Android | `MediaCodec` com `MIMETYPE_AUDIO_OPUS`, nativo desde a API 21 |
| ESP32 | `libopus` compilado no firmware |

## Respostas

**200 — aceito**
```json
{ "segment_id": 42, "status": "pending", "duplicate": false }
```
`duplicate: true` significa que este `client_uid` já existia. Não é erro: o
cliente deve tratar como sucesso e remover o item da fila.

**Erros**

| Código | Significado | O cliente deve |
|--------|-------------|----------------|
| 413 | Áudio acima de 50 MB | Descartar |
| 422 | `meta` inválido ou áudio vazio | Descartar |
| 5xx | Falha no servidor | Retentar com backoff |
| timeout / rede | Servidor inacessível | Retentar com backoff |

Regra: **4xx é definitivo** (descarte, senão a fila entope para sempre);
**5xx e erros de rede são temporários** (retentar). Exceção: `429` é temporário
e deve ser retentado.

## Fluxo do cliente

```
captura contínua
      │
      ▼
  VAD (Silero)          ← descarta silêncio: ~86% do áudio
      │
      ▼ segmento de fala
  encode Opus
      │
      ▼
  fila local (SQLite + arquivo)   ← gera client_uid aqui, uma única vez
      │
      ▼
  POST /ingest  ──┬── 200 ────────▶ remove da fila, apaga o áudio local
                  ├── 4xx ────────▶ descarta (não adianta insistir)
                  └── 5xx/rede ───▶ backoff exponencial e tenta de novo
```

### Backoff sugerido

`5s → 10s → 20s → 40s …` dobrando até o teto de 10 min, com no máximo 10
tentativas. É o que o cliente Windows usa (`windows-client/buffer.py`).

## Parâmetros de VAD

Todos vivem sob `capture.vad` no `config.yaml` e foram ajustados com áudio real:

| Parâmetro | Valor | Motivo |
|-----------|-------|--------|
| `threshold` | 0.5 | Probabilidade acima da qual a janela é fala. |
| `min_speech_ms` | 700 | Abaixo disso o Whisper devolve vazio ou confiança baixa, gastando GPU à toa. |
| `min_silence_ms` | 700 | Silêncio que encerra um segmento; evita cortar no meio da frase. |
| `padding_ms` | 300 | Margem antes/depois para não decepar sílabas. |
| `max_segment_ms` | 30000 | Teto para fala contínua sem pausa. |

O Silero opera em janelas de **512 amostras** a 16 kHz — é o tamanho que o
modelo espera, não é ajustável.

## Notas por plataforma

### Windows — sem restrições
WASAPI loopback captura tudo que sai da placa de som, qualquer aplicativo.
Implementado em `windows-client/capture.py`.

### Android — restrições reais do sistema
Três limites que **não têm contorno por software**:

1. **Chamadas não podem ser gravadas.** Google baniu a prática em 2022 e a
   classifica como spyware. Vale para telefone e WhatsApp. Único caminho:
   viva-voz captado pelo microfone.
2. **`AudioPlaybackCapture` é parcial.** Só captura apps que não fizeram
   opt-out (`android:allowAudioPlaybackCapture="false"`) e cujo
   `AudioAttributes.usage` seja `MEDIA`, `GAME` ou `UNKNOWN`. Spotify, Netflix
   e apps com DRM se recusam. Só uma sessão `MediaProjection` por vez no sistema.
3. **Foreground service obrigatório** com tipo `microphone` (Android 14+) e
   notificação permanente visível.

### Gadget (ESP32-S3) — Fase 5
Fecha exatamente o buraco do Android: conversas presenciais e chamadas via
microfone dedicado. Requisitos: I2S para o microfone, VAD local (uma versão
quantizada do Silero ou um detector de energia mais simples), encode Opus e
WiFi. O servidor não precisa de nenhuma mudança — é só mais um `device_id`.

## Exemplo (Python)

```python
import json, uuid, httpx
from datetime import datetime

client_uid = f"mic-{uuid.uuid4().hex[:16]}"   # gerar UMA vez, ao enfileirar
meta = {
    "device_id": "win-russo",
    "source": "mic",
    "started_at": datetime.now().isoformat(),
    "duration_ms": 5100,
    "client_uid": client_uid,
}

response = httpx.post(
    "http://192.168.0.10:8000/ingest",
    files={"audio": ("s.opus", audio_bytes, "audio/ogg")},
    data={"meta": json.dumps(meta)},
    timeout=60,
)
```

## Consulta

| Endpoint | Descrição |
|----------|-----------|
| `GET /api/segments?day=YYYY-MM-DD&source=mic` | Segmentos do dia |
| `GET /api/search?q=termo` | Busca textual (ignora acentos) |
| `GET /api/segments/{id}/audio` | Baixa o áudio (404 se o segmento não existe, 410 se o áudio expirou) |
| `GET /api/stats?day=YYYY-MM-DD` | Contagens do dia |
| `GET /api/days` | Dias com captura |
| `GET /api/hub/stt` | Estado dos provedores e gasto do dia |
| `POST /api/segments/retry` | Recoloca falhados na fila |
| `POST /api/retention/purge` | Aplica a política de retenção |
