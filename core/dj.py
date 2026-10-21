"""El DJ: decide si habla, arma qué dice y devuelve el wav listo para sonar.

El clip se prepara cuando ARRANCA la canción anterior, no en el corte: así hay
minutos de margen y la transición suena instantánea aunque la síntesis tarde.
Cada clip recuerda para qué canción se hizo; si mientras tanto hubo skip o
shuffle, una presentación que nombra la canción equivocada se descarta.

El DJ nunca puede impedir que suene la música: cualquier falla se registra y
la canción arranca igual.
"""

import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

from core import config, tts
from core.feeds import FeedCache

log = logging.getLogger("dj")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PHRASES_FILE = PROJECT_ROOT / "dj_phrases.json"
FEEDS_FILE = PROJECT_ROOT / "dj_feeds.json"

# 1 de cada 3 transiciones, y nunca dos seguidas.
SPEAK_CHANCE = 1 / 3

MAX_CHARS = 200

URL_RE = re.compile(r"https?://\S+|www\.\S+")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# espeak lee los emoji como basura o los deletrea.
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿]"
)
WHITESPACE_RE = re.compile(r"\s+")


def sanitize(text: str) -> str:
    """Deja el texto en algo que espeak pueda leer sin hacer el ridículo."""
    text = URL_RE.sub("", text or "")
    text = EMOJI_RE.sub("", text)
    text = CONTROL_RE.sub(" ", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        # Cortar en la última frase completa si hay una razonablemente cerca.
        for stop in (". ", "; ", ", "):
            idx = cut.rfind(stop)
            if idx > MAX_CHARS // 2:
                return cut[: idx + 1].strip()
        # Corte duro: el punto final tiene que caber DENTRO del tope.
        return cut[: MAX_CHARS - 1].rstrip() + "."
    return text


def track_id(track) -> str | None:
    """Identifica una canción para saber si el clip preparado sigue sirviendo.

    Ojo con `source`: en wavelink es el nombre de la fuente ("youtube"), igual
    para todos los temas, así que no distingue nada. El identificador propio de
    la pista sí. Se deja `source` de última para los dobles de los tests.
    """
    if not track:
        return None
    for atributo in ("identifier", "uri", "source"):
        valor = getattr(track, atributo, None)
        if valor:
            return str(valor)
    return None


@dataclass
class Clip:
    """Un segmento listo para reproducir."""
    path: Path
    text: str
    for_source: str | None   # a qué canción presenta; None = genérico
    kind: str


class DJ:
    def __init__(self, feed_cache: FeedCache | None = None):
        self.phrases = self._load_phrases()
        self.feeds = feed_cache
        self.enabled = True
        self.volume = config.DJ_VOLUME_DEFAULT
        # Estado por servidor.
        self._spoke_last: dict[int, bool] = {}
        self._forced: set[int] = set()
        self._pending: dict[int, Clip] = {}

    # --- configuración -----------------------------------------------------

    @staticmethod
    def _load_phrases() -> dict:
        try:
            with open(PHRASES_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            return {k: v for k, v in data.items() if isinstance(v, list) and v}
        except (OSError, json.JSONDecodeError):
            log.exception("no pude leer %s; el DJ va sin banco de frases", PHRASES_FILE)
            return {}

    @staticmethod
    def load_feed_urls() -> list[str]:
        try:
            with open(FEEDS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            return [u for u in data.get("feeds", []) if isinstance(u, str) and u.startswith("http")]
        except FileNotFoundError:
            log.info("no hay %s; el DJ va sin noticias (copia dj_feeds.example.json)", FEEDS_FILE.name)
            return []
        except (OSError, json.JSONDecodeError):
            log.exception("no pude leer %s; el DJ va sin noticias", FEEDS_FILE)
            return []

    def available(self) -> bool:
        # Volumen 0 es mutear: mejor no hablar que encolar un clip de silencio,
        # que se llevaría su turno igual y dejaría un hueco en la transición.
        return self.enabled and self.volume > 0 and tts.available() and bool(self.phrases)

    def set_volume(self, pct: int) -> int:
        """Ajusta el volumen de la voz (0-200%). Devuelve el valor aplicado."""
        self.volume = max(0, min(tts.MAX_VOLUME, int(pct)))
        return self.volume

    # --- decisión ----------------------------------------------------------

    def force(self, guild_id: int) -> None:
        """!dj o el botón 🎙️: hablar en el próximo corte, sí o sí."""
        self._forced.add(guild_id)

    def should_speak(self, guild_id: int) -> bool:
        if not self.available():
            return False
        if guild_id in self._forced:
            return True
        if self._spoke_last.get(guild_id):
            return False  # nunca dos seguidas
        return random.random() < SPEAK_CHANCE

    # --- texto -------------------------------------------------------------

    # Categorías del banco que no son stingers.
    BANK_CATEGORIES = ("bumper", "ad", "intro", "outro", "intermision")

    # Categorías escritas en inglés: se leen con voz inglesa. Los titulares y
    # cualquier bumper que se agregue en castellano van con la voz española.
    ENGLISH_CATEGORIES = frozenset({"intermision", "slang"})

    @classmethod
    def voice_for(cls, category: str) -> str:
        return tts.VOICE_EN if category in cls.ENGLISH_CATEGORIES else tts.VOICE_ES

    def _has_bank(self) -> bool:
        return any(self.phrases.get(c) for c in self.BANK_CATEGORIES)

    def _weights(self, has_track: bool) -> list[tuple[str, float]]:
        """Fuentes y pesos, filtrados por lo que realmente hay.

        Ofrecer una fuente vacía significa quedarse mudo en ese corte, así que
        se descarta antes de sortear: sin feeds no hay noticias, sin bumpers ni
        intros no hay banco.
        """
        base = {
            "banco": 40.0,
            "noticia": 35.0,
            "slang": 25.0,
        }
        disponible = {
            "banco": self._has_bank(),
            "noticia": bool(self.feeds),
            "slang": bool(self.phrases.get("slang")),
        }
        return [(k, w) for k, w in base.items() if disponible[k]]

    def _pick_source(self, has_track: bool) -> str:
        options = self._weights(has_track)
        if not options:
            return "banco"
        kinds = [k for k, _ in options]
        weights = [w for _, w in options]
        return random.choices(kinds, weights=weights, k=1)[0]

    def _from_bank(self, title: str, artist: str) -> tuple[str, bool, str]:
        """Devuelve (texto, nombra_la_cancion, categoría)."""
        categories = ["bumper", "ad", "outro", "intermision"]
        if title and "intro" in self.phrases:
            categories.append("intro")
            categories.append("intro")  # las intros pesan doble cuando hay tema
        categories = [c for c in categories if self.phrases.get(c)]
        if not categories:
            return "", False, "banco"
        category = random.choice(categories)
        template = random.choice(self.phrases[category])
        if category == "intro":
            return template.format(titulo=title, artista=artist or title), True, category
        return template, False, category

    def _slang(self) -> str:
        pool = self.phrases.get("slang") or []
        return random.choice(pool) if pool else ""

    async def _news(self, session) -> str:
        if not self.feeds:
            return ""
        headline = await self.feeds.next_headline(session)
        if not headline:
            return ""
        templates = self.phrases.get("noticia") or ["{titular}."]
        return random.choice(templates).format(titular=headline)

    async def build_text(self, title: str, artist: str, session) -> tuple[str, bool, str]:
        """(texto, nombra_la_cancion, fuente). Cae al banco si la fuente falla."""
        source = self._pick_source(bool(title))

        if source == "slang":
            text = self._slang()
            if text:
                return text, False, "slang"
        elif source == "noticia":
            text = await self._news(session)
            if text:
                return text, False, "noticia"

        text, names_track, category = self._from_bank(title, artist)
        if text:
            return text, names_track, category
        # Último recurso: un stinger. Mejor eso que un corte mudo.
        return self._slang(), False, "slang"

    # --- clips -------------------------------------------------------------

    async def prepare(self, guild_id: int, track, session) -> None:
        """Genera el clip para el PRÓXIMO corte y lo deja pendiente."""
        if not self.should_speak(guild_id):
            return
        if guild_id in self._pending:
            return

        title = getattr(track, "title", "") if track else ""
        artist = ""
        if title and " - " in title:
            artist = title.split(" - ", 1)[0].strip()

        try:
            raw, names_track, kind = await self.build_text(title, artist, session)
            text = sanitize(raw)
            if not text:
                return
            path = await tts.synthesize(text, voice=self.voice_for(kind), volume=self.volume)
            if not path:
                return
        except Exception:
            log.exception("no pude preparar el segmento del DJ")
            return

        source = track_id(track) if names_track else None
        self._pending[guild_id] = Clip(path=path, text=text, for_source=source, kind=kind)
        log.info("DJ listo (%s): %r", kind, text[:80])

    async def aviso(self, texto: str) -> Clip | None:
        """Un clip con algo que hay que avisar en voz. None si no se pudo.

        Es el canal que sigue vivo justo cuando falla lo que hay que avisar: los
        clips son wavs locales que Lavalink toca por su fuente `file`, sin pasar
        por YouTube ni por yt-dlp.

        No mira `enabled` a propósito. Apagar el locutor con `!dj off` saca las
        noticias y los bumpers; no saca el aviso de que la música se cortó, que
        es lo único que uno quiere escuchar cuando deja de sonar.
        """
        if not tts.available():
            return None
        limpio = sanitize(texto)
        if not limpio:
            return None
        try:
            path = await tts.synthesize(limpio, voice=tts.VOICE_ES, volume=self.volume)
        except Exception:
            log.exception("no pude sintetizar el aviso")
            return None
        if not path:
            return None
        return Clip(path=path, text=limpio, for_source=None, kind="aviso")

    def take(self, guild_id: int, track) -> Clip | None:
        """Retira el clip pendiente si sigue siendo válido para esta canción.

        Una presentación hecha para otro tema (hubo skip/shuffle) se descarta;
        un bumper o una noticia no nombran nada, así que se usan igual.
        """
        clip = self._pending.pop(guild_id, None)
        if not clip:
            self._spoke_last[guild_id] = False
            return None

        if clip.for_source and clip.for_source != track_id(track):
            log.info("descarto el clip: era para otra canción")
            tts.cleanup(clip.path)
            self._spoke_last[guild_id] = False
            return None

        self._forced.discard(guild_id)
        self._spoke_last[guild_id] = True
        return clip

    def drop(self, guild_id: int) -> None:
        """Tira el clip pendiente (al parar o desconectar)."""
        clip = self._pending.pop(guild_id, None)
        if clip:
            tts.cleanup(clip.path)
        self._spoke_last.pop(guild_id, None)
        self._forced.discard(guild_id)
