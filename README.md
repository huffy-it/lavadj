# LavaDJ

Bot de Discord que pone música en un canal de voz con un locutor de radio entre canción y canción.

- **Música** de YouTube, de Spotify (solo toma los nombres; el audio siempre sale de YouTube) y de otras fuentes de Lavalink con su URL: SoundCloud, Bandcamp, Vimeo, Twitch y audio directo por `http`.
- **DJ de radio** con voz neural de Piper o con `espeak-ng`, filtro de radio, titulares de noticias (RSS) y un guionista opcional (Claude u Ollama) que presenta cada tema.
- **Autoplay**: al acabarse la cola sigue solo con temas parecidos.
- **Comandos con `!` y con `/`**, con autocompletado en `/play` y `/playnext`.
- **Panel de botones** en Discord que acompaña a cada canción.
- **Letra**: en vivo en Discord (un mensaje que se edita línea a línea) y sincronizada en el panel web.
- **SponsorBlock**: salta las partes de un videoclip que no son la canción.
- **Retoma la cola** si el bot se reinicia.
- **Se arregla solo**: cambia de cliente de YouTube y actualiza yt-dlp cuando dejan de dar audio, destraba la cola si se queda muda, reintenta las pistas caídas y adelanta la URL del tema que viene.
- **Lista blanca de servidores** para que solo funcione en los tuyos.
- **Paneles de control** en tu máquina: web (cualquier sistema) y GTK4 (Linux).
- Funciona en Linux y en Windows.

> Proyecto escrito en su mayor parte con ayuda de IA (Claude) y revisado por su dueño, no por un equipo profesional. Tiene tests, pero úsalo bajo tu propio riesgo.

## Cómo funciona

Son tres procesos separados. **Lavalink** recibe el audio y lo manda a Discord; el **bot** (`bot.py`) atiende los comandos, decide qué suena y expone una API HTTP local; los **paneles** son clientes de esa API y no saben nada del proceso del bot.

```
  Panel web / GTK4 --HTTP 127.0.0.1:9876--> bot.py --WebSocket :2333--> Lavalink --audio--> Discord (voz)
                                             |  ^                          ^
                              comandos <-----+  +-- yt-dlp (URL del audio) +-- dj_cache/*.wav (voz del DJ)
```

**Por qué el audio lo resuelve yt-dlp.** El plugin de YouTube de Lavalink ya no consigue sacar el audio (la búsqueda sí funciona). Por eso, justo antes de reproducir, el bot le pide a yt-dlp la URL directa del audio y se la pasa a Lavalink por su fuente `http`; Lavalink sigue haciendo lo que hace bien, transcodificar y mandar a Discord. La parte que YouTube rompe seguido queda en yt-dlp, que saca versión nueva apenas YouTube cambia algo, y el bot lo mantiene al día.

## Requisitos e instalación

### Requisitos

| | Linux | Windows |
|---|---|---|
| Python con `venv` | 3.10 o superior (se desarrolla con 3.11) | ídem |
| Lavalink 4 | contenedor con `podman` (sin Java en el host) | `Lavalink.jar` con Java 21 |
| `yt-dlp` y `piper-tts` | en `requirements.txt` | en `requirements.txt` |
| `ffmpeg` | lo instalas tú (recomendado) | lo instala `setup.ps1` |
| `espeak-ng` | lo instalas tú | lo instala `setup.ps1` |
| `python3-gobject` (GTK4/libadwaita) | solo para el panel GTK | no aplica |

- `ffmpeg` es opcional: sin él el DJ habla igual, pero sin filtro de radio ni volumen ajustable.
- Sin `espeak-ng` ni una voz de Piper instalada, el DJ queda apagado y la música sigue igual.
- En Linux, `curl` baja el plugin de YouTube de Lavalink la primera vez.

### Crear el bot en Discord

1. En el [Portal de desarrolladores de Discord](https://discord.com/developers/applications) crea una aplicación, añade un **Bot** y copia su token a `DISCORD_TOKEN`.
2. En la pestaña **Bot**, activa el intent privilegiado **Message Content Intent**; sin él el bot no lee los comandos con prefijo. No usa ningún otro intent privilegiado.
3. Genera la invitación en **OAuth2 > URL Generator** con los scopes `bot` y `applications.commands` y estos permisos: ver canales, enviar mensajes, insertar enlaces, leer el historial, añadir reacciones (`!search` las usa), conectar y hablar.
4. Abre la URL e invita el bot a tu servidor.
5. Para que nadie más pueda invitarlo, desactiva **Public Bot** en la pestaña **Bot**. Si el portal no te deja, primero pon **Install Link** en *None* en la pestaña **Installation**: una aplicación privada no puede tener enlace de instalación.
6. Para que solo funcione en tus servidores, pon sus IDs en `SERVIDORES_PERMITIDOS` (activa el modo desarrollador de Discord y usa *Copiar ID del servidor*). Con la lista llena, el bot se va de cualquier servidor nuevo que no esté en ella y ignora los comandos en los demás; a los que ya estaba no los abandona, solo avisa en el log.

### Instalar en Windows

Necesitas Python y `winget` (viene con Windows 11). Desde la raíz del proyecto, en PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1   # una sola vez
```

`setup.ps1` crea el venv, instala las dependencias, instala por winget Java 21 (Temurin), ffmpeg y eSpeak NG, descarga `Lavalink.jar` y el plugin de YouTube (verificando su SHA-256) a `lavalink\` y crea `.env` y `dj_feeds.json` a partir de los ejemplos. Si winget instaló algo, abre una terminal nueva para que tome el PATH. Después edita `.env`, pon tu `DISCORD_TOKEN` y arranca:

```powershell
scripts\windows\start.ps1                  # levanta Lavalink, abre el panel web y corre el bot
```

### Instalar en Linux

```bash
git clone https://github.com/huffy-it/lavadj.git
cd lavadj

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

cp .env.example .env                       # y pon adentro tu DISCORD_TOKEN
cp dj_feeds.example.json dj_feeds.json     # opcional: noticias del DJ
```

Instala además, con el gestor de tu distribución, `podman`, `espeak-ng` y `ffmpeg`. No hace falta Java: se lo lleva adentro el contenedor de Lavalink (útil en distros inmutables como Bazzite). Para arrancar:

```bash
scripts/lavalink/start-lavalink.sh         # start | stop | status | logs | run
.venv/bin/python bot.py
```

La primera vez, `start-lavalink.sh` baja el plugin de YouTube a `lavalink/plugins/` y lo monta en el contenedor de solo lectura.

Para el panel GTK4, instala `python3-gobject` con tu gestor; corre con el python **del sistema**, no con el del venv:

```bash
scripts/instalar-acceso-directo.sh         # lo deja en el escritorio y en el menú
python3 scripts/lavadj-panel.py            # o lánzalo directamente
```

## Configuración

La configuración sale del `.env` (plantilla: [`.env.example`](.env.example), con los mismos grupos y el mismo orden que estas tablas). Lo único obligatorio es `DISCORD_TOKEN`. **Nunca subas tu `.env`**: ya está en `.gitignore`. Los booleanos aceptan `true`/`false` (también `1`, `si`, `on`, `yes`); un número inválido cae al valor por defecto.

**Discord**

| Variable | Por defecto | Para qué |
|---|---|---|
| `DISCORD_TOKEN` | — | Token del bot. Obligatoria |
| `PREFIX` | `!` | Prefijo de los comandos |
| `SERVIDORES_PERMITIDOS` | vacía | IDs de los servidores autorizados, separados por comas o espacios. Vacía = sin restricción |

**Lavalink y API local**

| Variable | Por defecto | Para qué |
|---|---|---|
| `LAVALINK_URI` | `http://127.0.0.1:2333` | El nodo de audio |
| `LAVALINK_PASSWORD` | `youshallnotpass` | Su contraseña; si la cambias, cámbiala también en `scripts/lavalink/application.yml` |
| `API_PORT` | `9876` | Puerto de la API y del panel web (siempre en `127.0.0.1`) |

**YouTube y yt-dlp**

| Variable | Por defecto | Para qué |
|---|---|---|
| `YTDLP_BIN` | autodetectado | Ruta a `yt-dlp` (se busca en el PATH, junto al python del venv y en `~/.local/bin`) |
| `YTDLP_PLAYER_CLIENT` | `android` | Cliente de YouTube que usa yt-dlp |
| `YTDLP_CLIENTES` | `android,android_vr,ios,mweb,web,tv_simply,web_embedded` | Clientes que se prueban, en ese orden, cuando el actual deja de dar audio |
| `YTDLP_AUTOACTUALIZAR` | `true` | Actualizar yt-dlp solo cuando tiene más de 30 días o ningún cliente suena; con `false` solo avisa |
| `YTDLP_TIMEOUT` | `30` | Segundos que se le aguantan a yt-dlp por canción |
| `SILENCIO_PARA_MUDO` | `300` | Segundos al aire, con cola y sin que arranque ninguna canción, para dar el bot por mudo |

**DJ y guionista**

| Variable | Por defecto | Para qué |
|---|---|---|
| `DJ_VOLUME` | `100` | Volumen inicial de la voz del DJ, de 0 (mudo) a 200 |
| `DJ_GUIONISTA` | `frases` | Quién escribe lo que dice el DJ: `frases`, `claude` u `ollama` |
| `DJ_GUIONISTA_MODELO` | `claude-opus-5-5` | Modelo de Claude del guionista |
| `ANTHROPIC_API_KEY` | — | Credencial de la API de Anthropic, solo con `claude` (la lee el SDK; también sirve `ant auth login`) |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | El Ollama local, solo con `ollama` |
| `OLLAMA_MODELO` | `llama3.2:3b` | Su modelo |

**SponsorBlock**

| Variable | Por defecto | Para qué |
|---|---|---|
| `SPONSORBLOCK` | `true` | Saltar lo que no es la canción en los videoclips |
| `SPONSORBLOCK_CATEGORIAS` | `music_offtopic` | Categorías de SponsorBlock que se saltan, separadas por comas |

**Colas y autoplay**

| Variable | Por defecto | Para qué |
|---|---|---|
| `QUEUE_LIMIT` | `500` | Tope duro de la cola |
| `PLAYLIST_LIMIT` | `100` | Cuántos temas se toman de una playlist de una vez |
| `AUTOPLAY_DEFAULT` | `true` | Autoplay al vaciarse la cola (se cambia por servidor con `!autoplay`) |
| `INACTIVE_CHANNEL_TOKENS` | `3` | Canciones seguidas sin humanos en el canal antes de cortar |
| `IDLE_TIMEOUT` | `300` | Segundos sin música antes de irse del canal |

Los scripts de Lavalink leen estas variables del entorno del shell, no del `.env`: `LAVALINK_PORT` (`2333`; si la cambias, cambia también `LAVALINK_URI`), `LAVALINK_HEAP` (tope de memoria de Java: `384m` en Linux, `768m` en Windows) y, solo en Linux, `LAVALINK_MEMORY` (`640m`) y `LAVALINK_CPUS` (`1.5`).

Lo que eliges desde el panel web (voz del DJ, filtro de radio y guionista) se guarda en `estado/ajustes.json` y gana sobre el `.env`. La cola de cada servidor se guarda en `estado/colas.json`. `.env`, `dj_feeds.json`, `estado/`, `voces/`, `logs/`, `dj_cache/` y `lavalink/` no se versionan.

## Uso

### Comandos

El prefijo por defecto es `!` (se cambia con `PREFIX`) y `!help` los lista. Todos funcionan también como comandos de barra (`/play`, `/skip`...), menos `!search`, que elige con reacciones y solo existe con prefijo. `/play` y `/playnext` autocompletan: mientras escribes, el bot busca en YouTube Music y ofrece hasta 25 temas (con una URL pegada no hace falta elegir nada). Para pedir música tienes que estar en un canal de voz, y los comandos de música no funcionan por mensaje directo.

| Comando | Alias | Qué hace |
|---|---|---|
| `!play <tema o URL>` | `!p` | Reproduce o encola un tema, una URL, una playlist o un link de Spotify |
| `!playnext <tema o URL>` | `!pn`, `!next` | Lo mete al principio de la cola (de una lista o de Spotify toma solo el primero) |
| `!search <tema>` | `!buscar` | Muestra 5 opciones y se elige con una reacción |
| `!queue` | `!q` | Muestra la cola |
| `!nowplaying` | `!np` | Qué está sonando y quién lo pidió |
| `!pause` | | Pausa |
| `!resume` | `!r` | Reanuda |
| `!skip` | `!s` | Salta a la siguiente |
| `!stop` | | Para todo, vacía la cola y sale del canal |
| `!loop` | `!l` | Activa o desactiva la repetición de la canción actual |
| `!shuffle` | | Mezcla la cola |
| `!volume <0-100>` | `!vol` | Ajusta el volumen de la música |
| `!remove <posición>` | `!rm` | Saca de la cola la canción en esa posición |
| `!autoplay [on\|off]` | `!ap` | Sin argumento muestra el estado; al vaciarse la cola sigue solo |
| `!lyrics [artista - canción]` | `!ly` | Letra completa de la canción actual o de la que indiques |
| `!letra [on\|off]` | | Letra en vivo en el chat; sin argumento, alterna |
| `!dj` | | El DJ habla en el próximo corte |
| `!dj on\|off` | | Enciende o apaga el DJ |
| `!dj vol [0-200]` | | Muestra o ajusta el volumen de la voz del DJ (0 = mudo) |
| `!refresh` | | Vence los titulares del DJ; se rebajan en el próximo corte |
| `!panel` | | Vuelve a publicar el panel de botones |

`!pause`, `!resume`, `!skip`, `!stop`, `!loop`, `!shuffle`, `!volume`, `!remove` y `!letra` exigen estar en el mismo canal de voz que el bot.

**Si los `/` no aparecen**, reinvita al bot con el permiso `applications.commands`. El bot los registra al arrancar: en cada servidor de `SERVIDORES_PERMITIDOS` (aparecen al instante) o, con la lista vacía, de forma global, y esto puede tardar en verse. El log dice cuántos registró y dónde.

**Spotify.** Acepta temas, álbumes y playlists (también `spotify:...`). De cada tema se toma "Artista - Título" y se busca en YouTube. Los nombres salen del reproductor embebible público, así que no hace falta registrar una app ni guardar credenciales; a cambio, una playlist entrega unos 50 temas como máximo y solo funcionan las listas públicas.

**Letra en vivo.** `/letra`, `!letra` o el botón 🎤 encienden un mensaje debajo del panel que se edita línea a línea al compás del tema: la actual en negrita, la anterior en gris y las dos que siguen. Necesita una letra de LRCLIB con tiempos; si el tema no la trae, el mensaje lo dice y queda `/lyrics`, que manda la letra entera. Se vuelve a publicar con cada tema y se apaga con `!stop` o al acabarse la cola.

### Panel de botones de Discord

Acompaña a cada tema con su título, quién lo pidió, la duración, el volumen y lo que viene después. Los botones exigen estar en el mismo canal de voz que el bot (menos 📋):

| Botón | Qué hace |
|---|---|
| ⏸️ / ▶️ | Pausa y reanuda |
| ⏭️ | Salta a la siguiente |
| ⏹️ | Para todo y sale del canal |
| 🔁 | Repetición de la canción actual (en verde si está activa) |
| 🔀 | Mezcla la cola |
| 🔉 / 🔊 | Baja o sube el volumen de la música de 10 en 10 |
| 📋 | Muestra la cola (solo a quien lo pulsa; lo puede usar cualquiera) |
| 🎙️ | El DJ habla en el próximo corte |
| 🎤 Letra | Enciende o apaga la letra en vivo (en verde si está activa) |
| 🔮 Autoplay | Enciende o apaga el autoplay (en verde si está activo) |

Lo que cambia el panel se actualiza en el propio mensaje; ⏭️, ⏹️, 📋, 🎙️, los errores y las acciones que no cambian nada visible responden en privado. El panel se borra al acabarse la cola, al parar y al apagar el bot; si el bot se cae sin cerrar, los botones siguen funcionando cuando vuelve.

### El DJ de radio

Entre canciones, el DJ habla en 1 de cada 3 cortes, nunca en dos seguidos (`!dj` o 🎙️ lo fuerzan). El clip se prepara mientras suena la canción anterior, así que no corta la música. También avisa en voz cuando una canción se cae y la salta.

- **Qué dice.** Con el guionista en `frases` (el valor por defecto) suelta frases de su banco (`dj_phrases.json`, editable) y lee titulares de noticias. Con un guionista, un modelo de lenguaje escribe la presentación de cada tema a partir del título, el artista, quién lo pidió, la hora y, a veces, un titular; si el modelo no responde en 8 segundos o se niega, ese corte sale con las frases.
- **Noticias.** Salen de los feeds RSS de `dj_feeds.json` (se crea con `cp dj_feeds.example.json dj_feeds.json` y se lee al arrancar). Sin ese archivo el DJ funciona igual, sin noticias. Los titulares viven en memoria y `!refresh` o el botón de los paneles los renueva.
- **Voz.** Por defecto habla `espeak-ng`. Con [Piper](https://github.com/OHF-Voice/piper1-gpl) puedes usar una voz neuronal en español (Argentina, España o México), mucho más natural. En el panel web, sección **Voz del DJ**, puedes escucharla, descargarla (de 28 a 114 MB, a `voces/`, verificando su md5 contra el catálogo de `rhasspy/piper-voices`), usarla o borrarla. El **filtro de radio** (ffmpeg) viene encendido y se apaga si prefieres la voz limpia. Si Piper falla, el DJ sigue con `espeak-ng` y lo avisa en el log.
- **Guionista.** Se elige con `DJ_GUIONISTA` o en caliente en el panel web (sección **Guionista del DJ**):
  - `frases`: sin guionista y sin costo.
  - `claude`: usa la API de Anthropic y **cobra por uso**, una llamada por presentación; el título, el artista y quién pidió el tema viajan a esa API. Pon `ANTHROPIC_API_KEY` en el `.env` (o inicia sesión con `ant auth login`). `DJ_GUIONISTA_MODELO=claude-haiku-5-5` es más barato.
  - `ollama`: usa un modelo local, gratis. Instala [Ollama](https://ollama.com), baja el modelo (`ollama pull llama3.2:3b`) y déjalo corriendo.

### Panel web

Con el bot en marcha, abre <http://127.0.0.1:9876/> (el puerto es `API_PORT`). Muestra:

- el estado del bot, de Lavalink, de yt-dlp (versión, si está viejo y si el bot cambió de cliente) y de la **transmisión** (`bien`, `flojo` si fallan varios temas seguidos, `mudo` si hay cola y no arranca nada);
- qué suena en cada servidor y su **letra**, al compás si LRCLIB trae los tiempos;
- el volumen y la **voz del DJ**, el filtro de radio y el **guionista**;
- los titulares pendientes con un botón para refrescarlos, el log reciente y un botón para apagar el bot.

La API escucha **solo en `127.0.0.1`** y no tiene autenticación: no la expongas a la red. Rechaza (403) las peticiones con un `Host` que no sea local y los POST de otro `Origin`, para que ninguna página web que abras pueda apagar el bot ni leer su estado. La lista de endpoints está en [AGENTS.md](AGENTS.md#api-http-de-los-paneles).

### Panel GTK4 (solo Linux)

`scripts/lavadj-panel.py`. Levanta y corta el bot (y Lavalink si hace falta), indica si hay audio y si la transmisión viene floja o muda, tiene el fader **VOZ DJ**, el botón de refrescar noticias, el log en vivo, los titulares con marca en los ya leídos y luz de panel clara u oscura. **Voces…** abre el selector del panel web. Guarda sus ajustes en `~/.config/lavadj/panel.json`.

## Arranque diario

- **Windows:** `scripts\windows\crear-acceso-directo.ps1` crea el acceso directo **LavaDJ** en el escritorio, que corre `start.ps1`: levanta Lavalink, abre el panel web y deja el bot en esa ventana, con el log a la vista. Ctrl+C o cerrarla apaga el bot; Lavalink queda arriba hasta `scripts\windows\lavalink.ps1 stop` (`start | stop | status | run`).
- **Linux:** `scripts/instalar-acceso-directo.sh` crea el lanzador del panel GTK4 en el escritorio y en el menú; desde ahí, el botón **Salir al aire** levanta Lavalink si está abajo y después el bot. Sin el panel: `scripts/lavalink/start-lavalink.sh` y `.venv/bin/python bot.py`.

## Cuando algo no suena

Si `!play` encuentra el tema pero no suena, casi siempre es yt-dlp desactualizado o un cliente de YouTube que YouTube cerró. El bot lo intenta arreglar solo (prueba los clientes de `YTDLP_CLIENTES` y, si ninguno sirve, actualiza yt-dlp) y deja todo en `logs/bot.log`; el cambio de cliente dura hasta reiniciar, así que para fijarlo ponlo en `YTDLP_PLAYER_CLIENT`. Si aun así no suena, separa las capas (en PowerShell usa `curl.exe`):

```sh
# 1. ¿Encuentra? Si devuelve tracks, la búsqueda está sana.
curl -H "Authorization: youshallnotpass" "http://127.0.0.1:2333/v4/loadtracks?identifier=ytmsearch%3Aalgo"

# 2. ¿yt-dlp resuelve? Si imprime un itag, el resolver está sano.
yt-dlp --extractor-args "youtube:player_client=android" --print "%(format_id)s" --skip-download "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

Si falla el segundo, prueba otro cliente (`android_vr`, `ios`, `mweb`, `web`...) y ponlo en `YTDLP_PLAYER_CLIENT`. Mira también `logs/bot.log` y el log de Lavalink (Linux: `podman logs lavadj-lavalink`; Windows: `lavalink\stdout.log` y `stderr.log`). Para comprobar de verdad que suena, usa `scripts/probar-que-suena.py` (ver [Desarrollo](#desarrollo)). El diagnóstico completo está en [AGENTS.md](AGENTS.md#cuando-youtube-deja-de-sonar).

## Desarrollo

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest                        # unos segundos, sin red
```

En Windows, `.\.venv\Scripts\python.exe` en lugar de `.venv/bin/python`. Los tests son async y hay que correrlos con el python del venv. Para comprobar el audio de YouTube hay dos pruebas aparte que **no** corren con `pytest` a secas y que se ejecutan en tu máquina, no en CI, porque YouTube bloquea las IP de centros de datos:

```bash
.venv/bin/python -m pytest -m integracion         # Lavalink y yt-dlp reales, sin entrar a voz
.venv/bin/python scripts/probar-que-suena.py      # entra a un canal de voz y toca varias pistas
```

Las dos necesitan Lavalink arriba, y el script además necesita el bot apagado (o `--cortar-bot`); `--help` lista sus opciones. Antes de tocar código lee [AGENTS.md](AGENTS.md): explica la arquitectura, las reglas del proyecto y los diagnósticos de audio. Documentación, comentarios y commits van en español neutro.

## Licencia

[MIT](LICENSE). Piper (`piper-tts`) es GPL-3.0: LavaDJ lo corre siempre como proceso aparte (`python -m piper`) y nunca lo importa. El proyecto no usa credenciales de YouTube: ni cuentas, ni cookies, ni sesiones.
