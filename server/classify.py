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

    apps = [part.strip().lower() for part in app_name.split("+") if part.strip()]

    if any(any(k in app for k in _CONVERSATION) for app in apps):
        return Category.CONVERSATION
    if any(any(k in app for k in _BROWSERS) for app in apps):
        return Category.BROWSER
    if any(any(k in app for k in _ENTERTAINMENT) for app in apps):
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


def label(category: Category) -> str:
    """Nome legível, para a interface."""
    return {
        Category.CONVERSATION: "conversa",
        Category.ENTERTAINMENT: "entretenimento",
        Category.BROWSER: "navegador",
        Category.MICROPHONE: "microfone",
        Category.UNKNOWN: "desconhecido",
    }[category]
