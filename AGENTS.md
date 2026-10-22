# LavaDJ

Bot de Discord de música con DJ de radio + paneles de control (GTK4 en Linux, web en
cualquier sistema). Tres procesos separados: **Lavalink** (audio; en contenedor en
Linux, directo con Java en Windows) ← **bot.py** (Discord + API HTTP) ← **panel**
(GTK4 o web, hablan por HTTP). Los paneles no saben nada del proceso del bot.

## Comandos

```sh
.venv/bin/python -m pytest              # toda la suite, unos segundos y sin red
.venv/bin/python bot.py                 # el bot (necesita Lavalink arriba)
scripts/lavalink/start-lavalink.sh      # nodo de audio (podman)
python3 scripts/lavadj-panel.py       # el panel GTK4 — python del SISTEMA, no el venv

# Comprobar que el audio de YouTube suena de verdad (Lavalink arriba, bot apagado):
.venv/bin/python -m pytest -m integracion            # REST contra Lavalink y yt-dlp reales
.venv/bin/python scripts/probar-que-suena.py         # entra a voz y toca varias pistas
```

En Windows el venv usa `.venv\Scripts\python.exe` en lugar de `.venv/bin/python`, y
los scripts de `scripts\windows\` hacen el trabajo de los de arriba: `setup.ps1`
(instalación), `lavalink.ps1` (nodo de audio) y `start.ps1` (nodo + bot + panel web).
Las dos últimas líneas de arriba son, en Windows,
`.venv\Scripts\python.exe -m pytest -m integracion` y
`.venv\Scripts\python.exe scripts\probar-que-suena.py`.

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

En Linux no instales Java: se lo lleva adentro el contenedor de Lavalink. Es
deliberado — en distros inmutables, meter Java con `rpm-ostree` obliga a reiniciar y
rehacer la capa en cada actualización del sistema. (En Windows no hay contenedor:
`setup.ps1` instala Java y descarga `Lavalink.jar` y el plugin de YouTube.)

## Arquitectura

| Ruta | Responsabilidad |
|---|---|
| `bot.py` | Arranque, carga de cogs |
| `cogs/music.py` | Comandos de Discord de música (wavelink) y el vigilante que destraba la cola |
| `core/estacion.py` | El estado de un servidor en un solo objeto (`Estacion`); `teardown` lo tira entero |
| `core/reintentos.py` | Política de reintentos de una pista caída: función pura, el cog aplica la decisión |
| `core/locutor.py` | El DJ frente al player: sus wavs se vuelven pistas al frente de la cola |
| `core/cola_spotify.py` | Carga de una lista de Spotify: busca cada nombre en YouTube y lo encola por lotes |
| `core/controller.py` | Estado de reproducción y colas |
| `core/api.py` | API HTTP en `:9876` que consumen los paneles; también sirve el panel web en `/` |
| `core/static/panel.html` | Panel web (multiplataforma) |
| `core/panel.py` | Panel de botones de Discord que acompaña a la canción |
| `core/resolver.py` | El audio de YouTube: yt-dlp saca la URL, Lavalink la toca |
| `core/sponsorblock.py` | Partes de un videoclip que no son música (`music_offtopic`) por la API pública de SponsorBlock con k-anonimato, con caché. La intro se salta arrancando en su final (`ResolvingPlayer.play`); las del medio las vigila el cog con `seek` o skip. Si falla o está apagado (`SPONSORBLOCK`), el tema suena entero |
| `core/dj.py`, `tts.py`, `feeds.py` | DJ de radio: noticias → Piper o espeak-ng (cae a espeak si Piper falla) → ffmpeg → `dj_cache/*.wav`. Piper es GPL y va **siempre como subproceso** (`python -m piper`), nunca importado |
| `core/voces.py` | Voces de Piper: catálogo curado (id, tamaño y md5 fijos, URLs a `rhasspy/piper-voices`), descarga verificada a `voces/` (no se versiona) y voces instaladas |
| `core/ajustes.py` | Ajustes persistentes de los paneles en `estado/ajustes.json` (`voz`, `filtro_radio`); escritura atómica |
| `core/guionista.py` | Guionista opcional del DJ (`frases` \| `claude` \| `ollama`, `DJ_GUIONISTA`): escribe la presentación de cada tema; si falla devuelve `None` y el DJ cae a sus frases. Claude va por el SDK `anthropic`; se cambia en caliente con `elegir()` |
| `core/spotify.py` | Embed público: de Spotify salen **solo nombres**, nunca audio |
| `core/lyrics.py` | Letras (LRCLIB con tiempos, lyrics.ovh de respaldo) para `!lyrics` y `/api/letra`, con caché por canción |
| `core/letra_en_vivo.py` | Letra en vivo en Discord (`/letra` y botón 🎤 del panel, apagada por defecto): `LetraEnVivo`, una por `Estacion` con su tarea y su mensaje, edita línea a línea. `indice_de` y `armar_texto` son puras (escapan el markdown de la letra, tope de 2000). Solo edita al cambiar de línea y cada `EDITAR_CADA` como mucho, y no en pausa; con letra sin tiempos o ausente avisa y no hay bucle. El cog la republica debajo del panel con cada tema y la apaga en `teardown` y al acabarse la cola. Comparte con `/api/letra` la caché `Music.letras` (clave `api.id_de_pista`) |
| `core/salud.py`, `config.py` | Salud de la transmisión, configuración |
| `core/persistencia.py` | La cola de cada servidor en `estado/colas.json` (no se versiona): el cog la guarda sola y el bot la retoma al volver, si hay gente en el canal y no pasaron 6 h |
| `scripts/lavadj-panel.py` | Panel GTK4 (proceso aparte): solo la ventana y el arranque |
| `scripts/lavadj_panel_logica.py` | Lo del panel sin GTK (API del bot, arranque, textos, ajustes), con tests |
| `scripts/lavadj-panel.css` | Estilo del panel; si falta, arranca sin estilo y avisa por stderr |
| `scripts/windows/` | Instalación y arranque en Windows (PowerShell) |
| `scripts/probar-que-suena.py` | Prueba de punta a punta: entra a voz y comprueba que suena (ver "Verificar que suena") |
| `tests/test_integracion.py` | Tests `integracion` (excluidos por defecto): Lavalink y yt-dlp reales |

Puertos: bot API `9876`, Lavalink `2333`.
`dj_cache/` se monta **en la misma ruta** dentro del contenedor de Lavalink — si
cambias esa ruta hay que cambiarla en los dos lados o el DJ se queda mudo.

## Reglas

1. **El audio siempre sale de Lavalink.** Spotify aporta nombres de canciones y nada
   más. Nada de bajar audio por otra vía.
2. **`.env` nunca se commitea.** El `DISCORD_TOKEN` vive ahí; `.env.example` es la
   plantilla y es lo único versionado.
3. **El panel se comunica solo por HTTP.** No lo acoples al proceso del bot
   (ya hubo una caída por eso: el panel pisaba `View._refresh()`).
4. Los tests son async (`asyncio_mode = auto` en `pytest.ini`); no hace falta
   decorar cada test con `@pytest.mark.asyncio`.
5. **Los comandos de música son híbridos** (`!` y `/`): `description` y
   `app_commands.describe` en español (100 caracteres como máximo), `await ctx.defer()`
   al empezar si buscan o resuelven (Discord da 3 s) y toda ruta termina en un mensaje.

## De dónde sale el audio de YouTube

**Lo resuelve yt-dlp, no Lavalink** (`core/resolver.py`). El plugin
`youtube-source` dejó de poder sacar el audio en 2026-08 y no hay versión que lo
arregle —1.18.2 es la última publicada—, así que el bot le pide la URL directa a
yt-dlp y se la pasa a Lavalink por su fuente `http`. Lavalink sigue haciendo lo
que hace bien (transcodificar y mandar a Discord) y la parte que YouTube rompe
seguido queda en yt-dlp, que saca versión nueva apenas YouTube cambia algo. El bot
lo actualiza solo (`Resolver.mantener_al_dia`): revisa la versión al arrancar y cada
24 h y, si tiene más de 30 días, la actualiza (pip si el binario está junto al python
del bot, `-U` si no), como mucho cada 6 h. `YTDLP_AUTOACTUALIZAR=false` lo apaga y
solo avisa en el log y en los paneles.

Tres cosas que hay que respetar si tocas esto:
- **La URL se pide al reproducir, nunca al encolar.** Las de googlevideo caducan
  en horas; una cola larga las mataría antes de que les toque el turno.
- **El enganche va en `Player.play()`** (`ResolvingPlayer`), no en el comando
  `!play`: quien va pasando de tema en tema es el AutoPlay de wavelink, y así
  quedan cubiertos comando, botón del panel y autoplay de una sola vez.
- **La pista que suena lleva los metadatos de la de YouTube.** La fuente `http`
  no trae ninguno y sin eso el panel muestra "Unknown title".

Si yt-dlp no puede, se devuelve la pista original y Lavalink la intenta a su
manera: el camino viejo sigue ahí para el día que el plugin reviva.

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

**Si la cola se queda muda** (el AutoPlay de wavelink se frena tras errores seguidos y
nada suena con temas esperando), el vigilante del cog lo destraba: cada `VIGILAR_CADA`
(20 s) revisa cada player, y si lo ve dos veces seguidas conectado, sin pausa, sin
sonar ni resolver y con algo en `queue` o `auto_queue`, arranca el siguiente por el
mismo camino que `!play`. Solo el vigilante arranca temas del `auto_queue`.

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

**El bot ya intenta arreglarlo solo** (`core/resolver.py`). Tras 3 fallos pasajeros
seguidos de yt-dlp (error sin motivo definitivo o salida sin URL; los videos
bloqueados y los timeouts no cuentan) sondea en segundo plano los clientes de
`YTDLP_CLIENTES` contra `dQw4w9WgXcQ` y se pasa al primero que entrega URL, con un
`warning` en el log. Si ninguno funciona y `YTDLP_AUTOACTUALIZAR` está encendido,
actualiza yt-dlp y sondea una vez más, empezando por el cliente actual. Hay un
sondeo cada 10 min como máximo. **El cliente cambiado solo dura hasta reiniciar**:
para fijarlo, cópialo del log a `YTDLP_PLAYER_CLIENT` en el `.env`; los paneles
muestran el cambio mientras tanto. Lo de abajo sigue siendo el diagnóstico cuando
eso no alcanza: busca en el log el `error` "Ningún cliente de YouTube entregó audio".

**El arreglo casi nunca está en el yml.** Si el audio ya no sale, lo primero es
saber quién falló, porque el bot tiene dos caminos: yt-dlp (el normal) y el
plugin (el de respaldo). La prueba que los separa:

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
—o cuando alguien dice "no suena"— córrela antes de dar el problema por resuelto. Entra a voz
y toca varias pistas por el mismo camino que el bot, así que cubre justo lo que los
`curl` y `yt-dlp` de arriba no pueden (ver la sección de más abajo).

**Este proyecto no usa credenciales de YouTube** — ni cuentas, ni cookies, ni
sesiones de ningún tipo. Es decisión del dueño, no un pendiente: no las
propongas ni las agregues.

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
que es el dato a bajar—. Igual que el bot, adelanta la URL de la pista siguiente
mientras suena la actual, así que desde la segunda el tiempo baja; con
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
Se saltan solos, con el motivo, si Lavalink o yt-dlp no están.

```sh
.venv/bin/python -m pytest -m integracion -rs              # Linux (-rs muestra por qué se salta)
.\.venv\Scripts\python.exe -m pytest -m integracion -rs    # Windows (PowerShell)
```

**Nada de esto va a CI.** YouTube bloquea las IP de centros de datos: en un runner
fallaría por motivos que no son del bot. Se corre en la máquina del dueño, con su
Lavalink y su `.env`.

## Arranque diario

- **Linux:** `scripts/instalar-acceso-directo.sh` crea el lanzador del panel GTK4;
  desde ahí, botón **Salir al aire** (levanta Lavalink si está abajo y después el bot).
- **Windows:** `scripts\windows\crear-acceso-directo.ps1` crea un acceso directo que
  corre `start.ps1` (Lavalink, bot y panel web).
- El panel web queda en `http://127.0.0.1:9876/` mientras el bot esté arriba.

## Convenciones

- README, docs, commits y comentarios en **español neutro** (tuteo, sin voseo ni modismos locales) — es el
  idioma del proyecto entero, respétalo.
- Los mensajes de commit describen el efecto, no el archivo tocado.
