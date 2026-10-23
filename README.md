# LavaDJ

Bot de Discord que pone música en un canal de voz con un locutor de radio entre canción y canción. Corre en tu máquina, en Linux o en Windows, y se maneja con comandos, con botones en Discord y con un panel de control.

> Proyecto escrito en su mayor parte con ayuda de IA (Claude) y revisado por su dueño, no por un equipo profesional. Tiene tests, pero úsalo bajo tu propio riesgo.

## Qué hace

- **Música** de YouTube, de Spotify (solo toma los nombres; el audio siempre sale de YouTube) y de otras fuentes de Lavalink con su URL (SoundCloud, Bandcamp, Vimeo, Twitch y audio directo).
- **Comandos con `!` y con `/`**, con autocompletado en `/play` y `/playnext`, y un **panel de botones** en Discord que acompaña a cada canción.
- **Autoplay y radio**: al acabarse la cola sigue con temas parecidos, y la radio sigue al último tema que pides. El botón 📻 del panel de Discord, `/radio` y el botón del panel web la rearman a partir de lo que suena.
- **DJ de radio** con voz neuronal de Piper o con `espeak-ng`, filtro de radio y titulares de noticias. Un guionista opcional (Claude u Ollama) presenta cada tema.
- **Letra en vivo** en Discord (un mensaje que se edita línea a línea) y sincronizada en el panel web.
- **SponsorBlock**: salta las partes de un videoclip que no son la canción.
- **Aguanta fallos**: retoma la cola si el bot se reinicia y se arregla solo cuando YouTube deja de dar audio (cambia de cliente, actualiza yt-dlp, reintenta las pistas caídas).
- **Paneles de control** en tu máquina: web (cualquier sistema) y GTK4 (Linux). **Apagar todo** en el panel web, o **Cortar** en el GTK4, cierra el bot y baja también Lavalink.

Son tres procesos: **Lavalink** (el audio) ← **bot** (Discord y una API HTTP local) ← **paneles**. El audio de YouTube lo resuelve yt-dlp y Lavalink lo transcodifica y lo manda a Discord. La arquitectura está en [AGENTS.md](AGENTS.md#arquitectura).

## Instalación

### Crear el bot en Discord

1. En el [Portal de desarrolladores de Discord](https://discord.com/developers/applications) crea una aplicación, añade un **Bot** y copia su token (irá en `DISCORD_TOKEN`).
2. En la pestaña **Bot**, activa el intent privilegiado **Message Content Intent**; sin él no se leen los comandos con prefijo.
3. En **OAuth2 > URL Generator** elige los scopes `bot` y `applications.commands` y los permisos ver canales, enviar mensajes, insertar enlaces, leer el historial, añadir reacciones, conectar y hablar. Abre la URL e invita el bot a tu servidor.
4. Para que nadie más pueda invitarlo, desactiva **Public Bot** en la pestaña **Bot**. Si el portal no te deja, primero pon **Install Link** en *None* en la pestaña **Installation**.
5. Pon los IDs de tus servidores en `SERVIDORES_PERMITIDOS` (modo desarrollador de Discord, *Copiar ID del servidor*). Con la lista llena, el bot se va de cualquier servidor nuevo que no esté en ella.

### Windows

Necesitas Python y `winget` (viene con Windows 11). Desde la raíz del proyecto, en PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1   # una sola vez
```

`setup.ps1` crea el venv, instala las dependencias, Java 21, ffmpeg y eSpeak NG, descarga Lavalink y su plugin de YouTube y crea `.env`. Abre una terminal nueva si winget instaló algo, pon tu `DISCORD_TOKEN` en `.env` y arranca:

```powershell
scripts\windows\start.ps1                  # levanta Lavalink, abre el panel web y corre el bot
```

### Linux

Instala con tu gestor `podman`, `espeak-ng`, `ffmpeg` y `curl`. No hace falta Java: se lo lleva adentro el contenedor de Lavalink (útil en distros inmutables como Bazzite). Necesitas Python 3.10 o superior.

```bash
git clone https://github.com/huffy-it/lavadj.git
cd lavadj
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env                       # y pon adentro tu DISCORD_TOKEN
scripts/lavalink/start-lavalink.sh         # el nodo de audio (start | stop | status | logs | run)
.venv/bin/python bot.py
```

Para el panel GTK4 instala `python3-gobject` y lánzalo con el python **del sistema**, no con el del venv: `python3 scripts/lavadj-panel.py`.

## Configuración

Todo sale del `.env`; su plantilla, [`.env.example`](.env.example), explica cada variable. **Nunca subas tu `.env`**: ya está en `.gitignore`.

- `DISCORD_TOKEN`: el token del bot. Es lo único obligatorio.
- `SERVIDORES_PERMITIDOS`: los servidores donde puede funcionar. Vacía, acepta cualquiera.

El guionista del DJ viene apagado (`DJ_GUIONISTA=frases`). Con `claude` usa la API de Anthropic y **cobra por uso**; con `ollama` usa un modelo local y gratis.

## Uso

El prefijo por defecto es `!` (cámbialo con `PREFIX`) y `!help` lista los comandos. Todos funcionan también como comandos de barra (`/play`, `/skip`...), menos `!search`, que elige con reacciones. Para pedir música tienes que estar en un canal de voz.

| Comando | Qué hace |
|---|---|
| `!play <tema o URL>` | Reproduce o encola un tema, una URL, una playlist o un link de Spotify |
| `!playnext <tema o URL>` | Lo mete al principio de la cola |
| `!search <tema>` | Muestra 5 opciones y se elige con una reacción |
| `!queue`, `!nowplaying` | Muestra la cola; dice qué suena y quién lo pidió |
| `!pause`, `!resume`, `!skip`, `!stop` | Controlan la reproducción; `!stop` vacía la cola y sale del canal |
| `!loop`, `!shuffle`, `!volume <0-100>`, `!remove <posición>` | Repetición, mezcla, volumen y quitar un tema de la cola |
| `!autoplay [on\|off]` | Al vaciarse la cola, sigue solo con temas parecidos |
| `!radio` | Rearma la radio a partir de lo que suena |
| `!lyrics [artista - canción]`, `!letra [on\|off]` | Letra completa; letra en vivo en el chat |
| `!dj`, `!dj on\|off`, `!dj vol [0-200]` | El DJ habla en el próximo corte; se enciende o apaga; volumen de su voz |
| `!panel`, `!refresh` | Vuelve a publicar el panel de botones; renueva los titulares del DJ |

El **panel de botones** que acompaña a cada tema tiene pausa, salto, parar, repetición, mezcla, volumen, cola, 🎙️ (DJ), 🎤 (letra en vivo), 🔮 (autoplay) y 📻 (radio). El **panel web** está en <http://127.0.0.1:9876/> mientras el bot esté arriba: muestra el estado, la letra, el log y los titulares, y deja elegir la voz del DJ, el filtro de radio y el guionista. La API escucha solo en `127.0.0.1` y no tiene autenticación: no la expongas a la red. Si los `/` no aparecen, reinvita al bot con el permiso `applications.commands`.

## Arranque diario

- **Windows:** `scripts\windows\crear-acceso-directo.ps1` crea el acceso directo **LavaDJ** en el escritorio, que corre `start.ps1`. **Apagar todo** en el panel web baja el bot y Lavalink.
- **Linux:** `scripts/instalar-acceso-directo.sh` crea el lanzador del panel GTK4; desde ahí, **Salir al aire** levanta Lavalink y el bot, y **Cortar** los baja. Sin el panel: `start-lavalink.sh` y `bot.py`, como arriba.

## Si algo no suena

Casi siempre es yt-dlp desactualizado o un cliente de YouTube que YouTube cerró. El bot intenta arreglarlo solo y deja el detalle en `logs/bot.log`. Si aun así no suena, [AGENTS.md](AGENTS.md#cuando-youtube-deja-de-sonar) explica cómo separar las capas y qué cambiar, y `scripts/probar-que-suena.py` comprueba de punta a punta que suena ([cómo usarlo](AGENTS.md#verificar-que-suena-sin-probar-a-mano-en-discord)).

## Desarrollo

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest                 # unos segundos, sin red
```

En Windows, `.venv\Scripts\python.exe` en lugar de `.venv/bin/python`. Antes de tocar código lee [AGENTS.md](AGENTS.md): arquitectura, reglas del proyecto y diagnósticos de audio.

## Licencia

[MIT](LICENSE). Piper (`piper-tts`) es GPL-3.0: LavaDJ lo corre siempre como proceso aparte (`python -m piper`) y nunca lo importa. El proyecto no usa credenciales de YouTube: ni cuentas, ni cookies, ni sesiones.
