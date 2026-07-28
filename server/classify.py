"""Classifica um segmento pelo app que originou o áudio.

O relatório diário não deve resumir a série que você assistiu — só o que veio
de conversa. A classificação separa os dois a partir do `app_name` que o
cliente registra.

Navegador é o caso difícil: o mesmo `msedge.exe` serve uma reunião no Meet e
um filme no Netflix. Sem o título da janela não dá para distinguir, então ele
tem categoria própria e o relatório decide o que fazer (por padrão, inclui —
perder uma reunião é pior que incluir um vídeo).
"""

from __future__ import annotations

from enum import StrEnum


class Category(StrEnum):
    CONVERSATION = "conversation"   # reuniões e chamadas
    ENTERTAINMENT = "entertainment"  # série, música, jogo
    BROWSER = "browser"              # ambíguo: pode ser Meet ou Netflix
    MICROPHONE = "microphone"        # a própria pessoa falando
    UNKNOWN = "unknown"


# Casa por substring no nome do executável, tudo minúsculo.
_CONVERSATION = (
    "teams", "zoom", "slack", "discord", "webex", "skype", "whatsapp",
    "telegram", "meet", "gotomeeting", "ringcentral", "mumble", "ts3client",
)

_ENTERTAINMENT = (
    "netflix", "spotify", "vlc", "mpv", "wmplayer", "musicbee", "foobar",
    "itunes", "applemusic", "primevideo", "disney", "hbomax", "globoplay",
    "deezer", "tidal", "youtubemusic", "steam", "epicgames", "leagueclient",
    "valorant", "csgo", "dota", "minecraft", "ffplay", "potplayer", "kodi",
)

_BROWSERS = (
    "chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "safari",
    "librewolf", "arc",
)

# O cliente manda "chrome.exe｜Reunião — Google Meet". A barra vertical cheia
# (U+FF5C) separa executável de título: o título comum já usa "|" e "-".
TITLE_SEP = "｜"

# Casadas contra o título da janela, que é o que revela o site aberto.
_CONVERSATION_SITES = (
    "meet.google", "google meet", "zoom", "teams", "webex", "whereby",
    "jitsi", "chime", "bluejeans", "gather", "around.co", "discord",
    "slack", "whatsapp", "telegram", "hangouts", "skype",
)

_ENTERTAINMENT_SITES = (
    "youtube", "netflix", "spotify", "twitch", "primevideo", "prime video",
    "disney", "hbo", "max.com", "globoplay", "crunchyroll", "deezer",
    "tidal", "soundcloud", "vimeo", "tiktok", "instagram", "facebook",
    "kwai", "pluto tv",
)


def classify_app(app_name: str | None, source: str = "system") -> Category:
    """Categoria de um segmento.

    `app_name` pode trazer vários apps separados por '+' quando tocavam
    juntos. Nesse caso vale a regra que você escolheu: basta um app de
    conversa para o segmento contar como conversa — perder uma reunião por
    causa de música ao fundo seria o erro mais caro.
    """
    if source == "mic":
        return Category.MICROPHONE

    if not app_name:
        return Category.UNKNOWN

    partes = [part.strip().lower() for part in app_name.split("+") if part.strip()]

    categorias = {_classify_one(parte) for parte in partes}

    # Basta um app de conversa: perder uma reunião por causa de música ao
    # fundo seria o erro mais caro.
    if Category.CONVERSATION in categorias:
        return Category.CONVERSATION
    if Category.BROWSER in categorias:
        return Category.BROWSER
    if Category.ENTERTAINMENT in categorias:
        return Category.ENTERTAINMENT
    return Category.UNKNOWN


def _classify_one(parte: str) -> Category:
    """Categoria de um app, considerando o título da janela se houver."""
    executavel, _, titulo = parte.partition(TITLE_SEP)

    if any(k in executavel for k in _CONVERSATION):
        return Category.CONVERSATION

    if titulo:
        # O título é o que distingue Meet de Netflix dentro do mesmo
        # navegador. Conversa primeiro: uma reunião com o YouTube aberto
        # noutra aba continua sendo reunião.
        if any(k in titulo for k in _CONVERSATION_SITES):
            return Category.CONVERSATION
        if any(k in titulo for k in _ENTERTAINMENT_SITES):
            return Category.ENTERTAINMENT

    if any(k in executavel for k in _BROWSERS):
        return Category.BROWSER
    if any(k in executavel for k in _ENTERTAINMENT):
        return Category.ENTERTAINMENT
    return Category.UNKNOWN


def is_report_worthy(category: Category, *, include_browser: bool = True) -> bool:
    """Este segmento deve entrar no relatório do dia?

    Entretenimento fica de fora sempre. O navegador entra por padrão porque
    não dá para saber se era uma reunião no Meet ou um vídeo — e omitir uma
    reunião é pior que incluir um vídeo.
    """
    if category is Category.ENTERTAINMENT:
        return False
    if category is Category.BROWSER:
        return include_browser
    return True


def should_transcribe(
    app_name: str | None,
    source: str,
    *,
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> tuple[bool, str]:
    """Este segmento deve ser transcrito? Devolve (sim/não, motivo).

    Com `allowlist` preenchida, só passa o que casar com ela — evita gastar
    transcrição (e guardar áudio de terceiros) com o que não interessa. O
    microfone passa sempre: é a sua própria voz, o núcleo do lifelog.
    """
    if source == "mic":
        return True, "microfone"

    alvo = (app_name or "").lower()

    for termo in blocklist or ():
        if termo.lower().strip() in alvo:
            return False, f"bloqueado por '{termo}'"

    if allowlist:
        for termo in allowlist:
            if termo.lower().strip() in alvo:
                return True, f"permitido por '{termo}'"
        return False, "fora da lista de permitidos"

    # Sem allowlist, mantém o comportamento anterior: tudo menos
    # entretenimento reconhecido.
    categoria = classify_app(app_name, source)
    if categoria is Category.ENTERTAINMENT:
        return False, "entretenimento"
    return True, str(categoria)


def label(category: Category) -> str:
    """Nome legível, para a interface."""
    return {
        Category.CONVERSATION: "conversa",
        Category.ENTERTAINMENT: "entretenimento",
        Category.BROWSER: "navegador",
        Category.MICROPHONE: "microfone",
        Category.UNKNOWN: "desconhecido",
    }[category]
