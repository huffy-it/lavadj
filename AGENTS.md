# LavaDJ

Bot de Discord de música con DJ de radio + paneles de control (GTK4 en Linux, web en
cualquier sistema). Tres procesos separados: **Lavalink** (audio; en contenedor en
Linux, directo con Java en Windows) ← **bot.py** (Discord + API HTTP) ← **panel**
(GTK4 o web, hablan por HTTP). Los paneles no saben nada del proceso del bot.

## Comandos

| Para | Linux | Windows (PowerShell) |
|---|---|---|
| Instalar | `python3 -m venv .venv` + `.venv/bin/python -m pip install -r requirements.txt` | `scripts\windows\setup.ps1` |
| Tests (toda la suite, unos segundos y sin red) | `.venv/bin/python -m pytest` | `.venv\Scripts\python.exe -m pytest` |
| Nodo de audio | `scripts/lavalink/start-lavalink.sh` (`start\|stop\|status\|logs\|run`, podman) | `scripts\windows\lavalink.ps1` (`start\|stop\|status\|run`) |
| Bot (necesita Lavalink arriba) | `.venv/bin/python bot.py` | `scripts\windows\start.ps1` (nodo + bot + panel web) |
| Panel GTK4 | `python3 scripts/lavadj-panel.py` — python del SISTEMA, no el venv | no existe: usa el panel web |
| Tests `integracion` (Lavalink arriba, bot apagado) | `.venv/bin/python -m pytest -m integracion` | `.venv\Scripts\python.exe -m pytest -m integracion` |
| Comprobar que suena (Lavalink arriba, bot apagado) | `.venv/bin/python scripts/probar-que-suena.py` | `.venv\Scripts\python.exe scripts\probar-que-suena.py` |

Los tests marcados `integracion` están excluidos de `pytest` a secas (`pytest.ini`),
para que la suite siga siendo rápida y sin red.

**Dos intérpretes distintos, a propósito:**
- **bot y tests → `.venv/bin/python`.** Con el `python3` del sistema los tests ni
  siquiera colectan (`ModuleNotFoundError: wavelink`).
- **panel GTK4 → `python3` del sistema.** GTK4/libadwaita vienen de `python3-gobject`
  del sistema y no están en el venv. No intentes "arreglar" esto metiendo pygobject
  al venv: en distros inmutables como Bazzite no compila y no hace falta.
- **`scripts/lavadj_panel_logica.py`** corre con el del sistema y se prueba con el del
  venv: solo biblioteca estándar, ni `gi` ni nada del venv (lo vigila un test).

**Java.** En Linux no instales Java: se lo lleva adentro el contenedor de Lavalink. Es
deliberado — en distros inmutables, meter Java con `rpm-ostree` obliga a reiniciar y
rehacer la capa en cada actualización del sistema. En Windows no hay contenedor:
`setup.ps1` instala Java y descarga `Lavalink.jar` y el plugin de YouTube.

**Lavalink.** Puertos: bot API `9876`, Lavalink `2333`. `scripts/lavalink/application.yml`
escucha en `0.0.0.0` porque en Linux es dentro del contenedor (el puerto se publica solo
en `127.0.0.1`); `lavalink.ps1` fuerza `-Dserver.address=127.0.0.1` y copia ese yml a
`lavalink\` en cada arranque, así que se edita el de `scripts/`, no la copia.
`dj_cache/` se monta **en la misma ruta** dentro del contenedor de Lavalink — si
cambias esa ruta hay que cambiarla en los dos lados o el DJ se queda mudo. Los scripts
leen del entorno `LAVALINK_PORT`, `LAVALINK_HEAP` (384m en Linux, 768m en Windows) y,
en Linux, `LAVALINK_MEMORY` y `LAVALINK_CPUS`.

## Arquitectura

| Ruta | Responsabilidad |
|---|---|
| `bot.py` | Arranque y carga de cogs; lista blanca de servidores (check global, `on_guild_join`); registro de los comandos de barra (una vez por proceso: por servidor con lista blanca, global sin ella); `/refresh`; apagado ordenado (con `todo`, `bajar_lavalink` baja también el nodo, ver "API HTTP de los paneles") |
| `cogs/music.py` | Comandos de Discord de música (wavelink), eventos de pista, el vigilante que destraba la cola y el guardado/retoma de colas; la radio sigue a lo último pedido: `rearmar_radio` cambia la `auto_queue` por el mix de YouTube Music (`list=RD<id>`, la consulta del AutoPlay de wavelink) del tema, sin él ni lo ya sonado, bajo el `_auto_lock` de wavelink y sin tocar `queue`; se dispara en segundo plano al pedir un tema suelto con `play`, `playnext` o `search` (no con playlists, Spotify ni con el autoplay apagado) y a mano con `poner_radio` (`/radio` y el botón 📻) |
| `core/config.py` | Toda la configuración (`.env` y constantes); `.env.example` es su plantilla |
| `core/estacion.py` | El estado de un servidor en un solo objeto (`Estacion`); `teardown` lo tira entero |
| `core/reintentos.py` | Política de reintentos de una pista caída: función pura, el cog aplica la decisión (2 reintentos por pista; 3 caídas seguidas paran la cola y `!play` la retoma) |
| `core/locutor.py` | El DJ frente al player: sus wavs se vuelven pistas al frente de la cola |
| `core/cola_spotify.py` | Carga de una lista de Spotify: busca cada nombre en YouTube y lo encola por lotes |
| `core/controller.py` | Estado de reproducción y colas (pausa, salto, loop, mezcla, volumen 0-100) |
| `core/messages.py`, `logging_setup.py` | Trocea mensajes al tope de Discord; configuración del log (`logs/bot.log`) |
| `core/api.py` | API HTTP en `:9876` que consumen los paneles; también sirve el panel web en `/` (ver "API HTTP de los paneles") |
| `core/static/panel.html` | Panel web (multiplataforma) |
| `core/panel.py` | Panel de botones de Discord que acompaña a la canción. Los botones muestran su estado (pausa, loop, autoplay, letra) y sobreviven a un reinicio: `custom_id` fijos y una `PlayerPanel` registrada con `bot.add_view` atiende también a los paneles de antes |
| `core/resolver.py` | El audio de YouTube: `Resolver` (una instancia compartida, `bot.resolver`) saca la URL con yt-dlp, la precarga, cambia de cliente y actualiza yt-dlp; `ResolvingPlayer` lo engancha a `play()` (ver "Audio de YouTube") |
| `core/sponsorblock.py` | Partes de un videoclip que no son música (`music_offtopic`) por la API pública de SponsorBlock con k-anonimato, con caché. La intro se salta arrancando en su final (`ResolvingPlayer.play`); las del medio las vigila el cog con `seek` o skip. Si falla o está apagado (`SPONSORBLOCK`), el tema suena entero |
| `core/dj.py`, `tts.py`, `feeds.py` | DJ de radio: noticias → Piper o espeak-ng (cae a espeak si Piper falla) → ffmpeg → `dj_cache/*.wav`. Habla 1 de cada 3 cortes. Sin guionista solo hay frases del banco (`dj_phrases.json` trae `intermision` y `slang`; las claves `bumper`, `ad` e `intro` se usan si se agregan) y titulares (`dj_feeds.json`). Piper es GPL y va **siempre como subproceso** (`python -m piper`), nunca importado |
| `core/voces.py` | Voces de Piper: catálogo curado (id, tamaño y md5 fijos, URLs a `rhasspy/piper-voices`), descarga verificada a `voces/` (no se versiona) y voces instaladas |
| `core/ajustes.py` | Ajustes persistentes de los paneles en `estado/ajustes.json` (`voz`, `filtro_radio`, `guionista`); escritura atómica |
| `core/guionista.py` | Guionista opcional del DJ (`frases` \| `claude` \| `ollama`, `DJ_GUIONISTA`): escribe la presentación de cada tema; si falla devuelve `None` y el DJ cae a sus frases. Claude va por el SDK `anthropic`; se cambia en caliente con `elegir()` |
| `core/spotify.py` | Embed público: de Spotify salen **solo nombres**, nunca audio |
| `core/lyrics.py` | Letras (LRCLIB con tiempos, lyrics.ovh de respaldo) para `!lyrics` y `/api/letra`, con caché por canción |
| `core/letra_en_vivo.py` | Letra en vivo en Discord (`/letra` y botón 🎤 del panel, apagada por defecto): `LetraEnVivo`, una por `Estacion` con su tarea y su mensaje, edita línea a línea. `indice_de` y `armar_texto` son puras (escapan el markdown de la letra, tope de 2000). Solo edita al cambiar de línea y cada `EDITAR_CADA` como mucho, y no en pausa; con letra sin tiempos o ausente avisa y no hay bucle. El cog la republica debajo del panel con cada tema y la apaga en `teardown` y al acabarse la cola. Comparte con `/api/letra` la caché `Music.letras` (clave `api.id_de_pista`) |
| `core/salud.py` | Salud de la transmisión (`bien`, `flojo` con 2 fallos seguidos, `mudo` pasados `SILENCIO_PARA_MUDO` s al aire con cola) y hueco medido entre pistas; la leen la API y el DJ |
| `core/persistencia.py` | La cola de cada servidor en `estado/colas.json` (no se versiona): el cog la guarda sola cada 15 s y el bot la retoma al volver, si hay gente en el canal y no pasaron 6 h. `!stop`, el corte por canal vacío y la salida por inactividad la borran |
| `scripts/lavadj-panel.py` | Panel GTK4 (proceso aparte): solo la ventana y el arranque |
| `scripts/lavadj_panel_logica.py` | Lo del panel sin GTK (API del bot, arranque, textos, ajustes), con tests |
| `scripts/lavadj-panel.css` | Estilo del panel; si falta, arranca sin estilo y avisa por stderr |
| `scripts/lavalink/` | `start-lavalink.sh` (contenedor podman, instala el plugin) y `application.yml` |
| `scripts/windows/` | `setup.ps1` (instalación), `lavalink.ps1` (nodo), `start.ps1` (nodo + bot + panel web) y `crear-acceso-directo.ps1` |
| `scripts/probar-que-suena.py` | Prueba de punta a punta: entra a voz y comprueba que suena (ver "Verificar que suena") |
| `tests/` | La suite; `test_integracion.py` son los `integracion` (excluidos por defecto): Lavalink y yt-dlp reales |

## Reglas

1. **El audio siempre sale de Lavalink.** Spotify aporta nombres de canciones y nada
   más. Nada de bajar audio por otra vía.
2. **`.env` nunca se commitea.** El `DISCORD_TOKEN` vive ahí; `.env.example` es la
   plantilla y es lo único versionado: toda variable nueva de `core/config.py` va ahí.
3. **El panel se comunica solo por HTTP.** No lo acoples al proceso del bot
   (ya hubo una caída por eso: el panel pisaba `View._refresh()`).
4. Los tests son async (`asyncio_mode = auto` en `pytest.ini`); no hace falta
   decorar cada test con `@pytest.mark.asyncio`.
5. **Los comandos de música son híbridos** (`!` y `/`; solo `!search`, que elige con
   reacciones, es de prefijo): `description` y `app_commands.describe` en español (100
   caracteres como máximo), `await ctx.defer()` al empezar si buscan o resuelven
   (Discord da 3 s) y toda ruta termina en un mensaje.
6. **Este proyecto no usa credenciales de YouTube** — ni cuentas, ni cookies, ni
   sesiones de ningún tipo. Es decisión del dueño, no un pendiente: no las
   propongas ni las agregues.
7. **Código compacto:** pocos comentarios (solo el porqué que no se ve en el código),
   sin líneas en blanco de más y sin código de ejemplo en docstrings ni docs.

## Audio de YouTube

**Lo resuelve yt-dlp, no Lavalink** (`core/resolver.py`). El plugin
`youtube-source` dejó de poder sacar el audio en 2026-08 y no hay versión que lo
arregle —1.18.2 es la última publicada—, así que el bot le pide la URL directa a
yt-dlp y se la pasa a Lavalink por su fuente `http`. Lavalink sigue haciendo lo
que hace bien (transcodificar y mandar a Discord), sigue buscando (`ytmsearch`) y la
parte que YouTube rompe seguido queda en yt-dlp, que saca versión nueva apenas
YouTube cambia algo. Si yt-dlp no puede, se devuelve la pista original y Lavalink la
intenta a su manera: el camino viejo sigue ahí para el día que el plugin reviva.

Cuatro cosas que hay que respetar si tocas esto:
- **La URL se pide al reproducir, nunca al encolar.** Las de googlevideo caducan
  en horas; una cola larga las mataría antes de que les toque el turno.
- **La del tema que viene se adelanta unos 30 s antes del final del que suena**
  (`PRECARGAR_ANTES` en `cogs/music.py`; la tarea vive en `Estacion.precarga`), no
  al empezarlo: una URL de googlevideo usada minutos después de pedida falla muchas
  veces al instante con un 302 (`Not success status code: 302`). Cuenta como final
  el inicio de la parte final que no es música de SponsorBlock, porque ahí el cog
  salta al siguiente. Con un corte del DJ de por medio, se pide al arrancar el corte.
  Cada precarga se usa una sola vez y solo si su URL aguanta la canción entera más
  10 min (`Resolver.precargar`).
- **El enganche va en `Player.play()`** (`ResolvingPlayer`), no en el comando
  `!play`: quien va pasando de tema en tema es el AutoPlay de wavelink, y así
  quedan cubiertos comando, botón del panel y autoplay de una sola vez.
- **La pista que suena lleva los metadatos de la de YouTube.** La fuente `http`
  no trae ninguno y sin eso el panel muestra "Unknown title".

**El plugin se instala a mano, nunca con `dependency`.** `application.yml` no
declara `youtube-plugin` en `lavalink.plugins` a propósito: con `dependency`,
Lavalink consulta al arrancar el `maven-metadata.xml` en `maven.lavalink.dev`, que
hoy devuelve una página HTML, y Lavalink no arranca (`XmlException`; el `.jar` de
ahí tampoco se puede bajar). El jar sale de los releases de GitHub y lo instalan en
`lavalink/plugins/` `setup.ps1` (Windows) o `start-lavalink.sh` (Linux, con `curl`:
bajar un archivo no mete Java en el host), los dos verificando el SHA-256. En Linux
esa carpeta se monta en el contenedor de solo lectura; ya no hay volumen de
plugins. Para cambiar de versión: versión y hash en los dos scripts, el nombre del
jar en `lavalink.ps1`, y borrar el jar viejo para que no se carguen los dos.

**Lo que el bot arregla solo:**
- **yt-dlp viejo** (`Resolver.mantener_al_dia`): revisa la versión al arrancar y cada
  24 h y, si tiene más de 30 días, la actualiza (pip si el binario está junto al python
  del bot, `-U` si no), como mucho cada 6 h. `YTDLP_AUTOACTUALIZAR=false` lo apaga y
  solo avisa en el log y en los paneles.
- **Cliente de YouTube cerrado:** tras 3 fallos pasajeros seguidos de yt-dlp (error
  sin motivo definitivo o salida sin URL; los videos bloqueados y los timeouts no
  cuentan) sondea en segundo plano los clientes de `YTDLP_CLIENTES` contra
  `dQw4w9WgXcQ` y se pasa al primero que entrega URL, con un `warning` en el log. Si
  ninguno funciona y `YTDLP_AUTOACTUALIZAR` está encendido, actualiza yt-dlp y sondea
  una vez más, empezando por el cliente actual. Hay un sondeo cada 10 min como máximo.
  **El cliente cambiado solo dura hasta reiniciar**: para fijarlo, cópialo del log a
  `YTDLP_PLAYER_CLIENT` en el `.env`; los paneles muestran el cambio mientras tanto.
- **Cola muda** (el AutoPlay de wavelink se frena tras errores seguidos y nada suena
  con temas esperando): el vigilante del cog cada `VIGILAR_CADA` (20 s) revisa cada
  player, y si lo ve dos veces seguidas conectado, sin pausa, sin sonar ni resolver y
  con algo en `queue` o `auto_queue`, arranca el siguiente por el mismo camino que
  `!play`. Solo el vigilante arranca temas del `auto_queue`.
- **Pista caída:** `core/reintentos.py` (ver la tabla). **Lavalink reiniciado:** el cog
  rehace los players y repite el tema en curso desde donde iba.

## Cuando YouTube deja de sonar

**El síntoma engaña.** La búsqueda sigue funcionando y solo se cae al resolver el
formato, así que desde Discord parece "no carga ninguna canción". Separa las dos
capas antes de tocar nada:

```sh
# 1. ¿Encuentra? Si esto devuelve tracks, la búsqueda está sana y el bot no tiene la culpa.
#    ytmsearch y no ytsearch: es la que usa el bot (la de wavelink por defecto), y
#    ytsearch puede devolver "empty" en el plugin aunque la del bot funcione.
curl -H "Authorization: youshallnotpass" \
  "http://127.0.0.1:2333/v4/loadtracks?identifier=ytmsearch%3Aalgo"

# 2. ¿Reproduce? El error real sale aquí, no en logs/bot.log.
podman logs lavadj-lavalink 2>&1 | grep -iE "failed|AllClientsFailed"
```

Si el bot no logró arreglarlo solo, busca en `logs/bot.log` el `error` "Ningún cliente
de YouTube entregó audio". **El arreglo casi nunca está en el yml.** Lo primero es
saber quién falló, porque el bot tiene dos caminos: yt-dlp (el normal) y el plugin
(el de respaldo). La prueba que los separa:

```sh
# ¿yt-dlp todavía resuelve? Si esto imprime un itag, el resolver está sano.
yt-dlp --extractor-args "youtube:player_client=android" --print "%(format_id)s" \
  --skip-download "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

- **Imprime un itag pero el bot no suena** → el problema está en el bot, no en
  YouTube: mira `logs/bot.log` por errores de `resolver`.
- **También falla** → YouTube cerró ese cliente. Sondea los otros y cambia
  `YTDLP_PLAYER_CLIENT` en el `.env`, no el yml:

```sh
for C in android android_vr ios mweb web tv_simply web_embedded; do
  printf '%-14s ' "$C"
  yt-dlp --extractor-args "youtube:player_client=$C" --print "%(format_id)s" \
    --skip-download "https://www.youtube.com/watch?v=dQw4w9WgXcQ" 2>&1 | tail -1
done
```

Tocar `plugins.youtube.clients` en `application.yml` solo mueve el camino de
respaldo. Nombres válidos ahí (salen del jar, `ClientProviderV4$ClientMapping`):
`ANDROID`, `ANDROID_MUSIC`, `ANDROID_VR`, `IOS`, `MUSIC`, `MWEB`, `TV`,
`TVHTML5_SIMPLY`, `WEB`, `WEBEMBEDDED`. Los clientes que funcionan en yt-dlp
pueden fallar en el plugin: lo que se rompe no es el cliente, es la versión que
manda el plugin.

**Una prueba suelta no alcanza**: encola varios temas o una lista `RD` antes de
cantar victoria. Un tema suelto puede sonar y el bot caerse igual en el siguiente.

**La prueba final es `scripts/probar-que-suena.py`**: después de cualquier arreglo
—o cuando alguien dice "no suena"— córrela antes de dar el problema por resuelto
(ver la sección siguiente). Y recuerda la regla 6: la solución no son cookies ni cuentas.

## Verificar que suena, sin probar a mano en Discord

`loadtracks` **no prueba nada**: la resolución del formato pasa hasta que se ejecuta
el track. La prueba de verdad es conectarse con el token del `.env` y wavelink,
entrar a un canal de voz, hacer `play` y esperar `on_wavelink_track_start` contra
`on_wavelink_track_exception`.

Eso ya está hecho en `scripts/probar-que-suena.py`; no lo reescribas a mano cada vez:

```sh
# Linux
.venv/bin/python scripts/probar-que-suena.py                  # con Lavalink arriba y el bot apagado
.venv/bin/python scripts/probar-que-suena.py --cortar-bot     # apaga el bot por la API antes
.venv/bin/python scripts/probar-que-suena.py --lista "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ"
.venv/bin/python scripts/probar-que-suena.py --help           # servidor, canal, consultas, tiempos
```

```powershell
# Windows
.\.venv\Scripts\python.exe scripts\probar-que-suena.py
.\.venv\Scripts\python.exe scripts\probar-que-suena.py --cortar-bot
.\.venv\Scripts\python.exe scripts\probar-que-suena.py --lista "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ"
```

Usa el `Resolver` y el `ResolvingPlayer` reales (con `client.resolver`, como el bot),
toca unas 4 búsquedas variadas o las primeras pistas de una playlist, y por cada una
mide los segundos desde `play()` hasta que Lavalink avisa del arranque —yt-dlp incluido,
que es el dato a bajar—. Adelanta la URL de la pista siguiente mientras suena la
actual (el bot espera a los últimos 30 s del tema; aquí, que cada pista suena poco,
se pide al empezar), así que desde la segunda el tiempo baja; con
`--sin-precarga` se ve la diferencia. Después la deja sonar unos segundos, porque una
pista puede arrancar y reventar al rato. Imprime OK o FALLA por pista y un resumen;
sale con 0 si todas sonaron, 1 si alguna falló y 2 si ni siquiera pudo prepararse la
prueba.

Dos cosas que hacen perder media hora si no las sabes, y que el script ya respeta:
- **Filtra los canales por `canal.permissions_for(guild.me)`.** Casi ninguno deja
  entrar al bot y los que no, tardan 30 s en fallar con `ChannelTimeoutException`.
  El script solo considera canales con `connect` y `speak` y prefiere los que no
  tienen personas, para no meterle música de prueba a nadie.
- **Corta el bot antes** (`curl -X POST 127.0.0.1:9876/api/stop`) o los dos se
  pelean la sesión de voz. El script se detiene con un aviso si la API del bot
  contesta; con `--cortar-bot` hace ese POST por ti (sin cabecera `Origin`, que es lo
  que la API acepta) y espera a que calle. Después el bot queda apagado: arráncalo tú.

Respeta también `SERVIDORES_PERMITIDOS`: no toca en servidores fuera de la lista.

**Los tests `integracion`** (`tests/test_integracion.py`) comprueban por separado las
mitades que más se rompen, vía REST, sin entrar a voz: que yt-dlp entregue una URL
`https://`, que la fuente `http` de Lavalink la abra (`loadType == "track"`) y que la
búsqueda del bot (`ytmsearch`) devuelva resultados. Son más rápidos que el script y
señalan qué mitad falló, pero **no prueban que suene**: eso solo lo hace el script.
Se saltan solos, con el motivo, si Lavalink o yt-dlp no están (`-rs` muestra por qué).

**Nada de esto va a CI.** YouTube bloquea las IP de centros de datos: en un runner
fallaría por motivos que no son del bot. Se corre en la máquina del dueño, con su
Lavalink y su `.env`.

## API HTTP de los paneles

Escucha **solo en `127.0.0.1`** (`API_HOST` no se configura a propósito) y no tiene
autenticación. Un middleware (`proteger`) rechaza con 403 las peticiones cuyo `Host` no
sea `127.0.0.1`, `localhost` o `[::1]` con su puerto (DNS rebinding) y los POST con un
`Origin` distinto o `null` (CSRF); sin `Origin` (urllib del panel GTK, curl) pasa.

| Endpoint | Método | Qué hace |
|---|---|---|
| `/` | GET | El panel web |
| `/api/status` | GET | Estado del bot, Lavalink, voz, salud de la transmisión (con el hueco entre pistas) y yt-dlp |
| `/api/log` | GET | Últimas líneas de `bot.log` (`?lineas=1-500`) |
| `/api/headlines` | GET | Titulares que el DJ tiene en cola |
| `/api/letra` | GET | Letra de lo que suena, con tiempos si los hay, y por dónde va (`?servidor=<id>`; sin él, el primero que suene) |
| `/api/dj_volume` | GET/POST | Lee o fija el volumen del DJ (`?valor=0-200`) |
| `/api/voces` | GET | Catálogo de voces con su estado, la voz en uso y el filtro de radio |
| `/api/voces/muestra` | GET | Frase de muestra en `audio/wav` (`?voz=<id>`) |
| `/api/voz` | POST | Elige la voz del DJ, solo una instalada (`?voz=<id>`) |
| `/api/voz/filtro` | POST | Enciende o apaga el filtro de radio (`?activo=1\|0`) |
| `/api/voces/descargar` | POST | Baja una voz en segundo plano (`?voz=<id>`) |
| `/api/voces/borrar` | POST | Borra una voz instalada que no esté en uso (`?voz=<id>`) |
| `/api/guionista` | GET/POST | Guionista en uso, cuáles están disponibles y su modelo; con POST lo cambia y guarda la elección (`?nombre=frases\|claude\|ollama`) |
| `/api/refresh_news` | POST | Rebaja los titulares ahora mismo |
| `/api/radio` | POST | Rearma el autoplay desde lo que suena (`?servidor=<id>`; sin él, el primero que suene): `cog.poner_radio` y `{"ok", "aviso"}`; 503 sin cog, 409 si no suena nada |
| `/api/stop` | POST | Cierre limpio del bot (`?motivo=`). Con `?todo=1`, después de cerrar baja también Lavalink con el script de cada sistema (`lavalink.ps1 stop` / `start-lavalink.sh stop`, plazo de 30 s; un fallo solo deja un aviso en el log). Sin `todo` Lavalink sigue arriba: es lo que usan `probar-que-suena.py --cortar-bot` y los scripts |

## Arranque diario

- **Linux:** `scripts/instalar-acceso-directo.sh` crea el lanzador del panel GTK4;
  desde ahí, botón **Salir al aire** (levanta Lavalink si está abajo y después el bot);
  **Cortar** baja todo: el bot, que se despide de los canales de voz y baja Lavalink
  (`todo=1`), y el panel lo verifica y lo baja él si el nodo sigue arriba.
- **Windows:** `scripts\windows\crear-acceso-directo.ps1` crea un acceso directo que
  corre `start.ps1` (Lavalink, bot y panel web).
- El panel web queda en `http://127.0.0.1:9876/` mientras el bot esté arriba. **Apagar
  todo** (pide confirmar con un segundo click) baja el bot y Lavalink; cuando la API deja
  de contestar, el panel avisa de que todo está apagado. Para volver, el acceso directo.

## Convenciones

- README, docs, commits y comentarios en **español neutro** (tuteo, sin voseo ni modismos locales) — es el
  idioma del proyecto entero, respétalo.
- Los mensajes de commit describen el efecto, no el archivo tocado.
