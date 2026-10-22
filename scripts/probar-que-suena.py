#!/usr/bin/env python3
"""Prueba de punta a punta que el bot de verdad suena, sin hacerlo a mano en Discord.

Es el procedimiento de AGENTS.md ("Verificar que suena") hecho script: se conecta
con el token del `.env`, entra a un canal de voz, toca varias pistas de YouTube por
el mismo camino de código que usa el bot (`Resolver` + `ResolvingPlayer`) y espera,
por cada una, a que Lavalink avise que arrancó o que falló.

`loadtracks` no prueba nada: el formato se resuelve recién al ejecutar la pista.
Esto sí. Corre en la máquina del dueño y no en CI, porque YouTube bloquea las IP de
centros de datos.

Usa el python del venv (el del sistema no tiene wavelink):

    .venv/bin/python scripts/probar-que-suena.py             # Linux
    .venv\\Scripts\\python.exe scripts\\probar-que-suena.py    # Windows
"""

import argparse
import asyncio
import logging
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# El script vive en scripts/ y `core` en la raíz: sin esto no se puede importar.
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import aiohttp  # noqa: E402
import discord  # noqa: E402
import wavelink  # noqa: E402

from core import config  # noqa: E402

# Varias y de gustos distintos a propósito: un tema suelto puede sonar y el bot
# caerse igual en el siguiente (AGENTS.md insiste en esto). Son temas enormes, de
# los que no los borran ni los bloquean por región.
CONSULTAS_POR_DEFECTO = (
    "Rick Astley Never Gonna Give You Up",
    "Queen Bohemian Rhapsody",
    "Daft Punk Get Lucky",
    "Soda Stereo De música ligera",
)
TIMEOUT_POR_DEFECTO = 45.0
SEGUNDOS_POR_DEFECTO = 5.0
CUANTAS_POR_DEFECTO = 4

# Cuánto se espera una búsqueda y la entrada a voz. Entrar a voz tarda segundos si
# todo va bien; lo que cuelga 30 s es un canal sin permiso, que ya se filtra antes.
TIMEOUT_BUSQUEDA = 30.0
TIMEOUT_VOZ = 20.0
TIMEOUT_NODO = 30.0
TIMEOUT_CLIENTE = 30.0
# Si el único canal usable tiene gente, el tiempo que se le da al dueño para
# arrepentirse antes de que empiece a sonar.
PAUSA_CON_GENTE = 5.0

COD_TODO_SONO = 0
COD_ALGUNA_FALLO = 1
COD_NO_SE_PUDO = 2   # no se pudo ni preparar la prueba (no confundir con "falló")
COD_INTERRUMPIDO = 130   # la convención de Ctrl+C

TIPOS_QUE_DECIDEN = ("arranco", "excepcion", "atascada")


class NoSePudoProbar(Exception):
    """Algo del entorno impide hacer la prueba (no es que una pista haya fallado)."""


@dataclass
class Evento:
    """Un aviso de Lavalink, con la hora a la que llegó (no a la que se leyó)."""

    tipo: str       # "arranco", "excepcion", "atascada" o "termino"
    payload: Any
    t: float        # time.monotonic() al llegar


@dataclass
class Resultado:
    titulo: str
    ok: bool
    segundos: float | None = None    # desde play() hasta que arrancó
    motivo: str = ""
    resolucion: float | None = None  # lo que tardó play() en volver (yt-dlp incluido)


# --- el bot y Lavalink --------------------------------------------------------

def _url_api(puerto: int, ruta: str) -> str:
    return f"http://127.0.0.1:{puerto}{ruta}"


# Sin proxy a propósito: con uno configurado en el sistema (frecuente en Windows
# de empresa) urllib mandaría también 127.0.0.1 al proxy y la prueba mediría otra cosa.
_SIN_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _abrir(peticion, timeout: float):
    return _SIN_PROXY.open(peticion, timeout=timeout)


def bot_corriendo(puerto: int, timeout: float = 2.0) -> bool:
    """¿Contesta la API del bot? Cualquier respuesta cuenta: hay alguien al otro lado."""
    try:
        with _abrir(_url_api(puerto, "/api/status"), timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def cortar_bot(puerto: int, espera: float = 10.0, sondeo: float = 0.25,
               timeout_sondeo: float = 1.0) -> bool:
    """Le pide al bot que se apague y espera a que la API deje de contestar.

    El POST va con urllib y sin cabecera `Origin`: la API rechaza los POST con un
    `Origin` ajeno (es la defensa contra páginas web hostiles) pero deja pasar los
    que no lo traen, que es como habla el panel de escritorio.

    Que la API calle es la señal buena: `bot.py` la cierra al final, después de
    desconectar los players y de cerrar el cliente de Discord.
    """
    peticion = urllib.request.Request(
        _url_api(puerto, "/api/stop?motivo=probar-que-suena"), data=b"", method="POST"
    )
    try:
        _abrir(peticion, 5).close()
    except (urllib.error.URLError, OSError):
        pass   # si ya no contesta, es justo lo que se quería
    limite = time.monotonic() + espera
    while time.monotonic() < limite:
        if not bot_corriendo(puerto, timeout=timeout_sondeo):
            return True
        time.sleep(sondeo)
    return not bot_corriendo(puerto, timeout=timeout_sondeo)


def lavalink_no_contesta(uri: str, password: str, timeout: float = 3.0) -> str | None:
    """None si Lavalink contesta; si no, el motivo en una frase."""
    peticion = urllib.request.Request(
        f"{uri.rstrip('/')}/version", headers={"Authorization": password}
    )
    try:
        with _abrir(peticion, timeout):
            return None
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return f"Lavalink en {uri} rechazó la contraseña: revisa LAVALINK_PASSWORD en el .env."
        return f"Lavalink en {uri} contestó HTTP {error.code} a /version."
    except (urllib.error.URLError, OSError, ValueError) as error:
        return (
            f"Lavalink no contesta en {uri} ({error}). Levántalo con "
            "scripts/lavalink/start-lavalink.sh (Linux) o scripts\\windows\\lavalink.ps1 (Windows)."
        )


# --- elegir dónde tocar --------------------------------------------------------

def humanos_en(canal) -> list:
    return [miembro for miembro in canal.members if not miembro.bot]


def motivo_para_no_usar(canal, yo) -> str | None:
    """Por qué el bot no puede tocar en este canal, o None si puede.

    Se mira antes de conectar porque un canal sin permiso no falla rápido: tarda
    30 s en reventar con `ChannelTimeoutException`.
    """
    permisos = canal.permissions_for(yo)
    if not permisos.connect:
        return "al bot le falta el permiso Conectar"
    if not permisos.speak:
        return "al bot le falta el permiso Hablar"
    limite = getattr(canal, "user_limit", 0) or 0
    # Quien puede mover miembros (o es administrador) entra aunque esté lleno.
    if limite and len(canal.members) >= limite and not getattr(permisos, "move_members", False):
        return f"está lleno (límite de {limite})"
    return None


def elegir_canal(canales, yo):
    """El primer canal de voz donde el bot puede tocar, mejor si no hay humanos.

    Un canal sin humanos evita meterle música de prueba a alguien que está
    conversando. Si todos los usables tienen gente, se devuelve el primero de
    ellos (quien llama avisa) en vez de no probar nada. None si no hay ninguno.
    """
    con_gente = None
    for canal in canales:
        if motivo_para_no_usar(canal, yo):
            continue
        if not humanos_en(canal):
            return canal
        if con_gente is None:
            con_gente = canal
    return con_gente


def elegir_servidor(guilds, permitidos, forzado: int | None = None):
    """El servidor donde probar: el pedido, o el primero de la lista blanca.

    Con `forzado`, solo vale si el bot está ahí y la lista blanca lo deja pasar.
    Lista vacía = sin restricción (igual que en el bot). None si no hay candidato.
    """
    for guild in guilds:
        if forzado is not None and guild.id != forzado:
            continue
        if permitidos and guild.id not in permitidos:
            continue
        return guild
    return None


def elegir_destino(client, args, permitidos):
    """(servidor, canal) donde se va a tocar, o NoSePudoProbar con el porqué."""
    if args.canal is not None:
        canal = client.get_channel(args.canal)
        if not isinstance(canal, discord.VoiceChannel):
            raise NoSePudoProbar(
                f"El canal {args.canal} no existe, el bot no lo ve o no es un canal de voz."
            )
        guild = canal.guild
        if args.servidor is not None and args.servidor != guild.id:
            raise NoSePudoProbar(
                f"El canal {args.canal} es del servidor {guild.id}, no del {args.servidor}."
            )
        if permitidos and guild.id not in permitidos:
            raise NoSePudoProbar(
                f"El servidor {guild.name} ({guild.id}) no está en SERVIDORES_PERMITIDOS: "
                "no se le toca música ahí ni de prueba."
            )
        motivo = motivo_para_no_usar(canal, guild.me)
        if motivo:
            raise NoSePudoProbar(f"No se puede usar el canal «{canal.name}»: {motivo}.")
        return guild, canal

    guild = elegir_servidor(client.guilds, permitidos, args.servidor)
    if guild is None:
        if args.servidor is not None:
            raise NoSePudoProbar(
                f"El bot no está en el servidor {args.servidor}, o ese servidor no está "
                "en SERVIDORES_PERMITIDOS."
            )
        raise NoSePudoProbar(
            "El bot no está en ningún servidor de SERVIDORES_PERMITIDOS "
            "(o no está en ninguno)."
        )
    canal = elegir_canal(guild.voice_channels, guild.me)
    if canal is None:
        raise NoSePudoProbar(
            f"En {guild.name} no hay ningún canal de voz donde el bot tenga los permisos "
            "Conectar y Hablar (y que no esté lleno). Indica uno con --canal."
        )
    return guild, canal


# --- reconocer los avisos de Lavalink ----------------------------------------

def _extras_de(track) -> dict:
    try:
        return dict(getattr(track, "extras", None) or {})
    except (TypeError, ValueError):
        return {}


def _identidades(pista) -> tuple[str, str]:
    return getattr(pista, "identifier", "") or "", getattr(pista, "uri", "") or ""


def coincide(payload, pista, sonando=None) -> bool:
    """¿Este aviso de Lavalink es de la pista que se está probando?

    `pista` es la que salió de la búsqueda; `sonando`, la que `play()` devolvió, que
    con el resolver es otra (la del stream `http`). Cada pista llega con una cara
    distinta según por dónde pase, así que se prueban varias pruebas de identidad:

    - el mismo `encoded` (lo que Lavalink de verdad toca);
    - el origen que el resolver guarda en `userData`, que es lo único que sobrevive
      el viaje a Lavalink y vuelta (el título y el `identifier` llegan como
      "Unknown title");
    - el identifier o la uri de YouTube, por si el audio se tocó por el plugin.

    Sin esto, el aviso de una pista que se acaba de reemplazar contaría como el de
    la siguiente.
    """
    track = getattr(payload, "track", None)
    if track is None:
        return False
    if sonando is not None and getattr(payload, "original", None) is sonando:
        return True

    codificadas = {
        getattr(p, "encoded", None) for p in (pista, sonando) if p is not None
    } - {None, ""}
    if getattr(track, "encoded", None) in codificadas:
        return True

    ident, uri = _identidades(pista)
    if ident and getattr(track, "identifier", None) == ident:
        return True
    if uri and getattr(track, "uri", None) == uri:
        return True
    for valor in _extras_de(track).values():
        if isinstance(valor, dict):
            if (ident and valor.get("identifier") == ident) or (uri and valor.get("uri") == uri):
                return True
    return False


def motivo_de_fallo(tipo: str, payload) -> str | None:
    """Por qué este aviso significa que la pista falló, o None si no es un fallo."""
    if tipo == "excepcion":
        exc = getattr(payload, "exception", None)
        exc = exc if isinstance(exc, dict) else {}
        mensaje = exc.get("message") or "sin mensaje"
        causa = exc.get("cause") or ""
        texto = f"Lavalink no pudo reproducirla: {mensaje}"
        if causa:
            texto += f" ({causa})"
        return texto[:300]
    if tipo == "atascada":
        return f"la pista se atascó (umbral de {getattr(payload, 'threshold', '?')} ms)"
    if tipo == "termino":
        if str(getattr(payload, "reason", "")).lower() == "loadfailed":
            return "Lavalink no logró cargarla (loadFailed)"
    return None


# --- la prueba de una pista ---------------------------------------------------

def describir(pista) -> str:
    titulo = getattr(pista, "title", "") or "(sin título)"
    autor = getattr(pista, "author", "") or ""
    return f"{titulo} - {autor}" if autor else titulo


async def probar_pista(vc, eventos: "asyncio.Queue[Evento]", pista, timeout: float,
                       segundos: float) -> Resultado:
    """Toca una pista y dice si sonó.

    Va en dos tiempos. Primero, hasta `timeout` desde la llamada a `play()` para
    que llegue el aviso de arranque (o de fallo). Después, `segundos` de escucha:
    una pista puede arrancar y reventar a los dos segundos (googlevideo cortando
    la lectura), y eso también es "no suena". El tiempo de arranque se mide con
    la hora a la que llegó el aviso, no a la que este bucle lo leyó.

    Los avisos de otras pistas (la anterior, ya reemplazada) se descartan.
    """
    titulo = describir(pista)
    t0 = time.monotonic()
    try:
        sonando = await asyncio.wait_for(vc.play(pista), timeout)
    except asyncio.TimeoutError:
        return Resultado(
            titulo, False,
            motivo=f"play() no terminó en {timeout:g} s (¿yt-dlp o Lavalink colgados?)",
        )
    except Exception as error:  # noqa: BLE001 - cualquier fallo de play() es un resultado
        return Resultado(titulo, False, motivo=f"play() falló: {type(error).__name__}: {error}")

    resolucion = time.monotonic() - t0
    if sonando is None:
        # ResolvingPlayer.play devuelve None si el player se fue mientras resolvía.
        return Resultado(
            titulo, False, resolucion=resolucion,
            motivo="el player se desconectó mientras se resolvía la pista",
        )

    limite = t0 + timeout
    ajenos = 0
    arranque = None
    while arranque is None:
        restante = limite - time.monotonic()
        try:
            if restante <= 0:
                raise asyncio.TimeoutError
            evento = await asyncio.wait_for(eventos.get(), restante)
        except asyncio.TimeoutError:
            nota = f" (llegaron {ajenos} avisos de otras pistas)" if ajenos else ""
            return Resultado(
                titulo, False, resolucion=resolucion,
                motivo=f"no arrancó en {timeout:g} s{nota}",
            )
        if not coincide(evento.payload, pista, sonando):
            if evento.tipo in TIPOS_QUE_DECIDEN:
                ajenos += 1
            continue
        if evento.tipo == "arranco":
            arranque = evento.t - t0
            fin_de_escucha = evento.t + segundos
            continue
        motivo = motivo_de_fallo(evento.tipo, evento.payload)
        if motivo:
            return Resultado(
                titulo, False, segundos=evento.t - t0, motivo=motivo, resolucion=resolucion
            )

    while True:
        restante = fin_de_escucha - time.monotonic()
        if restante <= 0:
            break
        try:
            evento = await asyncio.wait_for(eventos.get(), restante)
        except asyncio.TimeoutError:
            break
        if not coincide(evento.payload, pista, sonando) or evento.tipo == "arranco":
            continue
        motivo = motivo_de_fallo(evento.tipo, evento.payload)
        if motivo:
            return Resultado(
                titulo, False, segundos=arranque, resolucion=resolucion,
                motivo=f"arrancó, pero a los {evento.t - t0 - arranque:.1f} s falló: {motivo}",
            )
        if evento.tipo == "termino" and str(getattr(evento.payload, "reason", "")).lower() == "finished":
            break   # una pista corta que terminó sola: es un éxito, no hay más que escuchar
    return Resultado(titulo, True, segundos=arranque, resolucion=resolucion)


# --- el informe -----------------------------------------------------------------

def linea_de(resultado: Resultado) -> str:
    if resultado.ok:
        detalle = f"sonó a los {resultado.segundos:.1f} s"
        if resultado.resolucion is not None:
            detalle += f" (play() tardó {resultado.resolucion:.1f} s)"
        return f"OK     {resultado.titulo}  [{detalle}]"
    return f"FALLA  {resultado.titulo}  [{resultado.motivo}]"


def resumen(resultados: list[Resultado]) -> str:
    """El cierre: cuántas sonaron y cuánto tardaron en arrancar."""
    buenas = [r for r in resultados if r.ok]
    texto = f"{len(buenas)} de {len(resultados)} pistas sonaron."
    tiempos = [r.segundos for r in buenas if r.segundos is not None]
    if tiempos:
        texto += (
            f"\nTiempo hasta arrancar: medio {sum(tiempos) / len(tiempos):.1f} s "
            f"(mínimo {min(tiempos):.1f}, máximo {max(tiempos):.1f})."
        )
    return texto


def codigo_de_salida(resultados: list[Resultado]) -> int:
    """0 si todas sonaron; 1 si alguna falló o no se probó nada."""
    if resultados and all(r.ok for r in resultados):
        return COD_TODO_SONO
    return COD_ALGUNA_FALLO


# --- conectar -------------------------------------------------------------------

class ClientePrueba(discord.Client):
    """El cliente de Discord de la prueba: solo anota los avisos de Lavalink.

    Se anotan en una cola, con su hora, en vez de esperarlos con `wait_for`: el
    aviso de arranque puede llegar antes de que `play()` termine de volver, y un
    `wait_for` que se registra después se lo perdería.
    """

    def __init__(self):
        # Los intents por defecto bastan (guilds y voz). No hace falta
        # `message_content`, que además es privilegiado.
        super().__init__(intents=discord.Intents.default())
        # `ResolvingPlayer` toma de aquí su resolver, igual que en el bot.
        self.resolver = None
        self.eventos: asyncio.Queue[Evento] = asyncio.Queue()
        # `Pool.connect` vuelve al abrir el websocket, antes de que Lavalink mande
        # su "ready" (sin él no hay sesión y `play()` fallaría).
        self.nodo_listo = asyncio.Event()

    def _anotar(self, tipo: str, payload) -> None:
        self.eventos.put_nowait(Evento(tipo, payload, time.monotonic()))

    async def on_wavelink_node_ready(self, payload):
        self.nodo_listo.set()

    async def on_wavelink_track_start(self, payload):
        self._anotar("arranco", payload)

    async def on_wavelink_track_exception(self, payload):
        self._anotar("excepcion", payload)

    async def on_wavelink_track_stuck(self, payload):
        self._anotar("atascada", payload)

    async def on_wavelink_track_end(self, payload):
        self._anotar("termino", payload)


async def arrancar_cliente(client: ClientePrueba, token: str) -> "asyncio.Task":
    """Inicia sesión y espera a que el cliente esté listo. Devuelve la tarea de la sesión."""
    sesion = asyncio.create_task(client.start(token))
    listo = asyncio.create_task(client.wait_until_ready())
    try:
        hechas, _ = await asyncio.wait(
            {sesion, listo}, timeout=TIMEOUT_CLIENTE, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        listo.cancel()
    if listo in hechas:
        return sesion
    if sesion in hechas:
        # Terminó antes de estar listo: el error de verdad (el token malo, la red) está aquí.
        try:
            sesion.result()
        except discord.LoginFailure as error:
            raise NoSePudoProbar(f"Discord rechazó el token (DISCORD_TOKEN en el .env): {error}")
        except Exception as error:  # noqa: BLE001
            raise NoSePudoProbar(f"No pude conectar con Discord: {type(error).__name__}: {error}")
        raise NoSePudoProbar("Discord cerró la conexión antes de que el cliente estuviera listo.")
    raise NoSePudoProbar(f"Discord no dejó listo al cliente en {TIMEOUT_CLIENTE:g} s.")


async def conectar_nodo(client: ClientePrueba, sesion_http: aiohttp.ClientSession) -> None:
    # `retries=0`: un solo intento. Con el valor por defecto wavelink reintenta
    # para siempre y la prueba se colgaría si Lavalink no contesta.
    nodo = wavelink.Node(
        uri=config.LAVALINK_URI,
        password=config.LAVALINK_PASSWORD,
        session=sesion_http,
        retries=0,
        # Sin humanos en el canal wavelink empezaría a avisar de "player
        # inactivo" a las pocas pistas; aquí nadie lo escucha y solo estorba.
        inactive_channel_tokens=None,
        inactive_player_timeout=None,
    )
    # `Pool.connect` no lanza si falla: lo registra en el log y deja el nodo
    # desconectado.
    nodos = await asyncio.wait_for(
        wavelink.Pool.connect(client=client, nodes=[nodo]), TIMEOUT_NODO
    )
    if nodo.identifier not in nodos or nodo.status is wavelink.NodeStatus.DISCONNECTED:
        raise NoSePudoProbar(
            f"No pude conectar con el nodo Lavalink de {config.LAVALINK_URI} "
            "(mira el log de arriba; ¿contraseña o versión de Lavalink?)."
        )
    try:
        await asyncio.wait_for(client.nodo_listo.wait(), TIMEOUT_NODO)
    except asyncio.TimeoutError:
        raise NoSePudoProbar(
            f"El nodo Lavalink de {config.LAVALINK_URI} abrió el websocket pero no avisó "
            f"que estaba listo en {TIMEOUT_NODO:g} s."
        )


def _primeras(encontrado, cuantas: int) -> list:
    """Las primeras pistas de un resultado de búsqueda (lista de pistas o playlist)."""
    return list(getattr(encontrado, "tracks", encontrado))[:cuantas]


async def reunir_pistas(args, resultados: list[Resultado]) -> list:
    """Busca las pistas a probar. Una búsqueda que falla se anota como FALLA.

    Se busca con la fuente por defecto de wavelink, que es `ytmsearch`: la misma
    que usa el bot. Así una búsqueda rota también aparece aquí.
    """
    pistas: list = []
    vistas: set[str] = set()

    def agregar(pista) -> None:
        # Dos veces el mismo video no prueba nada nuevo y confunde los avisos.
        clave = getattr(pista, "identifier", None) or str(id(pista))
        if clave not in vistas:
            vistas.add(clave)
            pistas.append(pista)

    async def buscar(consulta: str, cuantas: int) -> None:
        etiqueta = f"búsqueda {consulta!r}"
        try:
            encontrado = await asyncio.wait_for(
                wavelink.Playable.search(consulta), TIMEOUT_BUSQUEDA
            )
            primeras = _primeras(encontrado, cuantas)
        except Exception as error:  # noqa: BLE001
            motivo = f"{type(error).__name__}: {error}"
        else:
            if primeras:
                for pista in primeras:
                    agregar(pista)
                return
            motivo = "no devolvió resultados"
        resultado = Resultado(etiqueta, False, motivo=motivo)
        resultados.append(resultado)
        print(linea_de(resultado), flush=True)

    consultas = list(args.consulta or [])
    if not consultas and not args.lista:
        consultas = list(CONSULTAS_POR_DEFECTO)
    for consulta in consultas:
        await buscar(consulta, 1)
    if args.lista:
        await buscar(args.lista, args.cuantas)
    return pistas


async def cerrar(client: ClientePrueba, vc, sesion, sesion_http) -> None:
    """Deja todo como estaba, haya salido bien o mal. Cada paso aguanta los fallos del anterior."""
    if vc is not None:
        try:
            await asyncio.wait_for(vc.disconnect(), 10)
        except Exception as error:  # noqa: BLE001
            print(f"Aviso: no pude desconectar el player limpiamente ({error}).", file=sys.stderr)
    try:
        await wavelink.Pool.close()
    except Exception:  # noqa: BLE001
        pass
    if getattr(client, "resolver", None) is not None:
        await client.resolver.cerrar()
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass
    if sesion is not None:
        try:
            await asyncio.wait_for(sesion, 5)
        except (Exception, asyncio.CancelledError):  # ya se cerró; aquí solo se recoge la tarea
            pass
    if sesion_http is not None:
        await sesion_http.close()


async def principal(args) -> int:
    # Se importan aquí y no arriba: así el script se puede cargar (y probar su
    # lógica) aunque el resolver esté a medio escribir, y el error sale claro.
    try:
        from core.resolver import Resolver, ResolvingPlayer
    except ImportError as error:
        raise NoSePudoProbar(
            f"core.resolver no ofrece lo que esta prueba necesita (Resolver y ResolvingPlayer): {error}"
        )

    client = ClientePrueba()
    client.resolver = Resolver()
    resultados: list[Resultado] = []
    sesion = vc = None
    sesion_http = aiohttp.ClientSession()
    try:
        sesion = await arrancar_cliente(client, config.DISCORD_TOKEN)
        print(f"Conectado a Discord como {client.user}.", flush=True)

        guild, canal = elegir_destino(client, args, config.SERVIDORES_PERMITIDOS)
        humanos = humanos_en(canal)
        print(f"Servidor: {guild.name} ({guild.id}). Canal de voz: {canal.name} ({canal.id}).", flush=True)
        if humanos:
            print(
                f"AVISO: hay {len(humanos)} persona(s) en «{canal.name}» y la prueba va a sonar "
                f"ahí. Tienes {PAUSA_CON_GENTE:g} s para cortar con Ctrl+C e indicar otro "
                "canal con --canal.",
                flush=True,
            )
            await asyncio.sleep(PAUSA_CON_GENTE)

        await conectar_nodo(client, sesion_http)
        pistas = await reunir_pistas(args, resultados)
        if not pistas:
            print("No se encontró ninguna pista que probar.", flush=True)
        else:
            print(f"Probando {len(pistas)} pista(s) (cada una suena {args.segundos:g} s).", flush=True)
            try:
                vc = await canal.connect(cls=ResolvingPlayer, timeout=TIMEOUT_VOZ)
            except Exception as error:  # noqa: BLE001
                raise NoSePudoProbar(
                    f"No pude entrar al canal de voz «{canal.name}»: {type(error).__name__}: {error}"
                )
            # Sin autoplay: aquí quien decide qué suena es el script, no wavelink.
            vc.autoplay = wavelink.AutoPlayMode.disabled
            for indice, pista in enumerate(pistas):
                # Como el bot: la URL del tema que viene se adelanta mientras suena
                # este, así el tiempo hasta arrancar mide lo que oye la gente. La
                # primera pista siempre se resuelve en frío.
                if not args.sin_precarga and indice + 1 < len(pistas):
                    client.resolver.precargar(pistas[indice + 1])
                resultado = await probar_pista(
                    vc, client.eventos, pista, args.timeout, args.segundos
                )
                resultados.append(resultado)
                print(linea_de(resultado), flush=True)
    finally:
        await cerrar(client, vc, sesion, sesion_http)

    print("", flush=True)
    print(resumen(resultados), flush=True)
    return codigo_de_salida(resultados)


# --- la línea de comandos -------------------------------------------------------

class _AyudaEnEspanol(argparse.RawDescriptionHelpFormatter):
    """Lo mismo que el formateador de siempre, con el "uso:" en castellano."""

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix or "uso: ")


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probar-que-suena.py",
        add_help=False,
        formatter_class=_AyudaEnEspanol,
        description=(
            "Prueba de punta a punta que el audio de YouTube suena: entra a un canal de voz\n"
            "con el token del .env, toca varias pistas por el mismo camino que usa el bot\n"
            "(Resolver + ResolvingPlayer) y espera a que Lavalink avise si arrancaron."
        ),
        epilog=(
            "Códigos de salida:\n"
            "  0  todas las pistas sonaron\n"
            "  1  alguna falló\n"
            "  2  no se pudo preparar la prueba (el bot sigue corriendo, no hay canal,\n"
            "     Lavalink caído...)\n"
            "\n"
            "Por defecto prueba 4 búsquedas variadas. --consulta (repetible) las reemplaza y\n"
            "--lista agrega las primeras pistas de una playlist (con --lista sola se prueba\n"
            "solo la playlist). Un tema suelto no alcanza: una lista tipo RD es la mejor prueba.\n"
            "\n"
            "El bot no puede estar corriendo: los dos se pelearían la sesión de voz. Con\n"
            "--cortar-bot lo apaga por la API; después arráncalo tú.\n"
            "\n"
            "YouTube bloquea las IP de centros de datos: esto se corre en tu máquina, no en CI.\n"
            "No lo ejecutes mientras haya gente escuchando el bot."
        ),
    )
    parser._positionals.title = "argumentos"
    parser._optionals.title = "opciones"
    parser.add_argument("-h", "--help", action="help", help="muestra esta ayuda y sale")
    parser.add_argument(
        "--cortar-bot", action="store_true",
        help="si el bot está corriendo, lo apaga (POST /api/stop) y espera; sin esto se detiene con un aviso",
    )
    parser.add_argument(
        "--servidor", type=int, metavar="ID",
        help="ID del servidor donde probar (por defecto, el primero de SERVIDORES_PERMITIDOS donde esté el bot)",
    )
    parser.add_argument(
        "--canal", type=int, metavar="ID",
        help="ID del canal de voz (por defecto, el primero con permisos Conectar y Hablar, "
             "prefiriendo los que no tienen personas)",
    )
    parser.add_argument(
        "--consulta", action="append", metavar="TEXTO",
        help="búsqueda a probar, se toma el primer resultado (repetible; reemplaza las de por defecto)",
    )
    parser.add_argument(
        "--lista", metavar="URL",
        help="URL de una playlist (por ejemplo una RD...): se prueban sus primeras pistas",
    )
    parser.add_argument(
        "--cuantas", type=int, default=CUANTAS_POR_DEFECTO, metavar="N",
        help=f"cuántas pistas tomar de --lista (por defecto {CUANTAS_POR_DEFECTO})",
    )
    parser.add_argument(
        "--timeout", type=float, default=TIMEOUT_POR_DEFECTO, metavar="SEG",
        help=f"segundos que se espera a que arranque cada pista, resolución con yt-dlp incluida "
             f"(por defecto {TIMEOUT_POR_DEFECTO:g})",
    )
    parser.add_argument(
        "--segundos", type=float, default=SEGUNDOS_POR_DEFECTO, metavar="SEG",
        help=f"segundos que se deja sonar cada pista antes de pasar a la siguiente "
             f"(por defecto {SEGUNDOS_POR_DEFECTO:g})",
    )
    parser.add_argument(
        "--sin-precarga", action="store_true",
        help="no adelanta la URL de la pista siguiente (sirve para comparar los tiempos "
             "hasta arrancar con y sin precarga)",
    )
    parser.add_argument(
        "-v", "--verboso", action="store_true",
        help="muestra el log de las librerías y del resolver (cuánto tardó yt-dlp, por ejemplo)",
    )
    return parser


def validar(parser: argparse.ArgumentParser, args) -> None:
    if args.cuantas < 1:
        parser.error("--cuantas debe ser al menos 1")
    if args.timeout <= 0:
        parser.error("--timeout debe ser mayor que 0")
    if args.segundos < 0:
        parser.error("--segundos no puede ser negativo")


def preparar_entorno(args) -> None:
    """Todo lo que se comprueba antes de tocar Discord. Falla con un mensaje claro.

    El orden importa: lo que no depende del bot va primero, para no apagarle la
    radio al dueño por una prueba que de todos modos no iba a poder correr.
    """
    if not config.DISCORD_TOKEN:
        raise NoSePudoProbar("DISCORD_TOKEN está vacío: ponlo en el .env.")
    motivo = lavalink_no_contesta(config.LAVALINK_URI, config.LAVALINK_PASSWORD)
    if motivo:
        raise NoSePudoProbar(motivo)

    if not bot_corriendo(config.API_PORT):
        return
    if not args.cortar_bot:
        raise NoSePudoProbar(
            f"El bot está corriendo (su API contesta en {_url_api(config.API_PORT, '/')}). "
            "Si los dos entran a voz se pelean la sesión y la prueba no vale.\n"
            "Detenlo tú, o vuelve a correr esto con --cortar-bot."
        )
    print("El bot está corriendo: lo detengo con POST /api/stop...", flush=True)
    if not cortar_bot(config.API_PORT):
        raise NoSePudoProbar(
            "Le pedí al bot que se detuviera, pero su API sigue contestando. "
            "Detenlo a mano y vuelve a intentar."
        )
    print("Bot detenido. Cuando termines, arráncalo otra vez.", flush=True)


def main(argv: list[str] | None = None) -> int:
    # Los títulos de YouTube traen de todo y una consola de Windows no siempre
    # puede imprimirlo: mejor un "?" que perder el informe con un UnicodeEncodeError.
    # Va antes de leer los argumentos para que `--help` también lo aproveche.
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(errors="replace")

    parser = construir_parser()
    args = parser.parse_args(argv)
    validar(parser, args)
    logging.basicConfig(
        level=logging.INFO if args.verboso else logging.WARNING,
        format="    [%(name)s] %(message)s",
    )

    try:
        preparar_entorno(args)
        return asyncio.run(principal(args))
    except NoSePudoProbar as error:
        print(f"No se pudo hacer la prueba: {error}", file=sys.stderr)
        return COD_NO_SE_PUDO
    except KeyboardInterrupt:
        print("\nInterrumpido. El player ya se desconectó.", file=sys.stderr)
        return COD_INTERRUMPIDO
    except Exception:  # noqa: BLE001
        # Un error del propio script no es "una pista falló": se distingue con el 2.
        traceback.print_exc()
        return COD_NO_SE_PUDO


if __name__ == "__main__":
    sys.exit(main())
