# LavaDJ

Bot de Discord de música con DJ de radio + paneles de control (GTK4 en Linux, web en
cualquier sistema). Tres procesos separados: **Lavalink** (audio; en contenedor en
Linux, directo con Java en Windows) ← **bot.py** (Discord + API HTTP) ← **panel**
(GTK4 o web, hablan por HTTP). Los paneles no saben nada del proceso del bot.

## Comandos

```sh
.venv/bin/python -m pytest              # toda la suite, menos de un segundo
.venv/bin/python bot.py                 # el bot (necesita Lavalink arriba)
scripts/lavalink/start-lavalink.sh      # nodo de audio (podman)
python3 scripts/lavadj-panel.py       # el panel GTK4 — python del SISTEMA, no el venv
```

En Windows el venv usa `.venv\Scripts\python.exe` en lugar de `.venv/bin/python`, y
los scripts de `scripts\windows\` hacen el trabajo de los de arriba: `setup.ps1`
(instalación), `lavalink.ps1` (nodo de audio) y `start.ps1` (nodo + bot + panel web).

**Dos intérpretes distintos, a propósito:**
- **bot y tests → `.venv/bin/python`.** Con el `python3` del sistema los tests ni
  siquiera colectan (`ModuleNotFoundError: wavelink`).
- **panel GTK4 → `python3` del sistema.** GTK4/libadwaita vienen de `python3-gobject`
  del sistema y no están en el venv. No intentes "arreglar" esto metiendo pygobject
  al venv: en distros inmutables como Bazzite no compila y no hace falta.

En Linux no instales Java: se lo lleva adentro el contenedor de Lavalink. Es
deliberado — en distros inmutables, meter Java con `rpm-ostree` obliga a reiniciar y
rehacer la capa en cada actualización del sistema. (En Windows no hay contenedor:
`setup.ps1` instala Java y descarga `Lavalink.jar`.)

## Arquitectura

| Ruta | Responsabilidad |
|---|---|
| `bot.py` | Arranque, carga de cogs |
| `cogs/music.py` | Comandos de Discord de música (wavelink) |
| `core/controller.py` | Estado de reproducción y colas |
| `core/api.py` | API HTTP en `:9876` que consumen los paneles; también sirve el panel web en `/` |
| `core/static/panel.html` | Panel web (multiplataforma) |
| `core/panel.py` | Panel de botones de Discord que acompaña a la canción |
| `core/resolver.py` | El audio de YouTube: yt-dlp saca la URL, Lavalink la toca |
| `core/dj.py`, `tts.py`, `feeds.py` | DJ de radio: noticias → espeak-ng → `dj_cache/*.wav` |
| `core/spotify.py` | Embed público: de Spotify salen **solo nombres**, nunca audio |
| `core/salud.py`, `config.py` | Salud de la transmisión, configuración |
| `scripts/lavadj-panel.py` | Panel GTK4 (proceso aparte) |
| `scripts/windows/` | Instalación y arranque en Windows (PowerShell) |

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

## De dónde sale el audio de YouTube

**Lo resuelve yt-dlp, no Lavalink** (`core/resolver.py`). El plugin
`youtube-source` dejó de poder sacar el audio en 2026-08 y no hay versión que lo
arregle —1.18.2 es la última publicada—, así que el bot le pide la URL directa a
yt-dlp y se la pasa a Lavalink por su fuente `http`. Lavalink sigue haciendo lo
que hace bien (transcodificar y mandar a Discord) y la parte que YouTube rompe
seguido queda en yt-dlp, que saca versión nueva apenas YouTube cambia algo. No se
actualiza solo: el bot avisa en el log y en los paneles cuando la versión instalada
tiene más de 30 días.

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

**Este proyecto no usa credenciales de YouTube** — ni cuentas, ni cookies, ni
sesiones de ningún tipo. Es decisión del dueño, no un pendiente: no las
propongas ni las agregues.

## Verificar que suena, sin probar a mano en Discord

`loadtracks` **no prueba nada**: la resolución del formato pasa hasta que se ejecuta
el track. La prueba de verdad es conectarse con el token del `.env` y wavelink,
entrar a un canal de voz, hacer `play` y esperar `on_wavelink_track_start` contra
`on_wavelink_track_exception`.

Dos cosas que hacen perder media hora si no las sabes:
- **Filtra los canales por `canal.permissions_for(guild.me)`.** Casi ninguno deja
  entrar al bot y los que no, tardan 30 s en fallar con `ChannelTimeoutException`.
- **Corta el bot antes** (`curl -X POST 127.0.0.1:9876/api/stop`) o los dos se
  pelean la sesión de voz.

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
