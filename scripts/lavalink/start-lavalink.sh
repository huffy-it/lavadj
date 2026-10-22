#!/usr/bin/env bash
# Nodo Lavalink de LavaDJ, dentro de un contenedor podman.
#
# En distros inmutables como Bazzite no hay Java y meterlo con `rpm-ostree
# install` obliga a reiniciar y a rehacer la capa en cada actualización del
# sistema; el contenedor se lleva el Java adentro y no toca nada de la base.
#
# Uso:  start-lavalink.sh [start|stop|status|logs|run]
#   start   (por defecto) levanta el nodo en segundo plano y espera a que responda
#   stop    lo baja y borra el contenedor
#   status  dice si está arriba (código de salida 0 = arriba)
#   logs    sigue el log del contenedor
#   run     lo corre en primer plano, para depurar
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROYECTO="$(cd "$AQUI/../.." && pwd)"

IMAGEN="ghcr.io/lavalink-devs/lavalink:4"
NOMBRE="lavadj-lavalink"
PUERTO="${LAVALINK_PORT:-2333}"

# El plugin de YouTube se instala a mano en esta carpeta del proyecto (la misma
# que usa Windows) y se monta de solo lectura en el contenedor. No va como
# `dependency` en el application.yml porque Lavalink se caería al arrancar: el
# repositorio maven.lavalink.dev devuelve una página HTML en vez del
# maven-metadata.xml que espera. El jar se baja de los releases de GitHub y se
# verifica contra el hash. Para cambiar de versión, cambia PLUGIN_VERSION y
# PLUGIN_SHA256 (la URL se arma sola; el hash sale de bajar el jar a mano y correr
# sha256sum), los mismos datos en scripts/windows/setup.ps1 y el nombre del jar en
# scripts/windows/lavalink.ps1; y borra el jar viejo de lavalink/plugins para que
# Lavalink no cargue los dos.
PLUGINS_DIR="$PROYECTO/lavalink/plugins"
PLUGIN_VERSION="1.18.2"
PLUGIN_ARCHIVO="youtube-plugin-$PLUGIN_VERSION.jar"
PLUGIN_URL="https://github.com/lavalink-devs/youtube-source/releases/download/$PLUGIN_VERSION/$PLUGIN_ARCHIVO"
PLUGIN_SHA256="dd4b3bce50dbc7582c80776bad47bad87bef1dc683c69d66ab4a084aa3006238"

# El DJ sintetiza sus clips aquí y Lavalink los abre con la fuente "local": el
# contenedor tiene que verlos en LA MISMA ruta absoluta que usa el bot, porque
# lo que viaja por el protocolo es la ruta tal cual.
DJ_CACHE="$PROYECTO/dj_cache"

# Compat: antes del cambio de nombre el contenedor se llamaba huffybot-lavalink.
# Si quedó uno vivo, sigue ocupando el puerto 2333 y el nuevo no podría arrancar.
# (Los volúmenes viejos huffybot-lavalink-plugins y lavadj-lavalink-plugins quedan
# huérfanos: el plugin ahora vive en lavalink/plugins y ya no se usa ningún
# volumen; se pueden borrar con `podman volume rm`.)
podman rm -f huffybot-lavalink >/dev/null 2>&1 || true

corriendo() {
  [ "$(podman container inspect "$NOMBRE" --format '{{.State.Running}}' 2>/dev/null)" = "true" ]
}

# La contraseña sale del propio yml para que no se desincronicen.
CLAVE="$(sed -n 's/^ *password: *"\(.*\)"/\1/p' "$AQUI/application.yml" | head -1)"

# Baja el plugin de YouTube si falta. Se mira solo si el archivo existe, no su
# hash: así un jar que ya esté en la carpeta (aunque sea otro build de la misma
# versión) no se reemplaza.
instalar_plugin() {
  if [ -f "$PLUGINS_DIR/$PLUGIN_ARCHIVO" ]; then
    return 0
  fi
  mkdir -p "$PLUGINS_DIR"
  echo "Bajando $PLUGIN_ARCHIVO (github.com/lavalink-devs/youtube-source, ~1.6 MB)..."
  # Se baja a un archivo aparte y solo recibe su nombre definitivo si el hash
  # cuadra: una descarga cortada o corrupta nunca queda como jar bueno.
  local temporal="$PLUGINS_DIR/$PLUGIN_ARCHIVO.descarga"
  if ! curl -fL -o "$temporal" "$PLUGIN_URL"; then
    rm -f "$temporal"
    echo "No pude bajar $PLUGIN_URL" >&2
    exit 1
  fi
  local hash
  hash="$(sha256sum "$temporal" | awk '{print $1}')"
  if [ "$hash" != "$PLUGIN_SHA256" ]; then
    rm -f "$temporal"
    echo "El SHA-256 de $PLUGIN_ARCHIVO no coincide: esperaba $PLUGIN_SHA256 y llegó $hash." >&2
    echo "Lo borré; vuelve a correr el script y, si se repite, revisa $PLUGIN_URL" >&2
    exit 1
  fi
  mv "$temporal" "$PLUGINS_DIR/$PLUGIN_ARCHIVO"
}

responde() {
  # /version responde 401 sin cabecera de autorización, así que hay que mandarla:
  # si no, el chequeo falla aunque el nodo esté perfecto.
  curl -sf -m 2 -H "Authorization: $CLAVE" "http://127.0.0.1:$PUERTO/version" >/dev/null 2>&1
}

# Las etiquetas SELinux importan: sin ellas el contenedor recibe "permission
# denied" al leer el yml y Lavalink arranca en silencio con su configuración por
# defecto (puerto 8080, sin plugins). `Z` reetiqueta privado, `z` compartido —
# dj_cache lo comparte con el bot, que sigue escribiendo ahí; la carpeta de
# plugins solo la usa este contenedor, así que va privada y de solo lectura.
# Sin tope la JVM se reserva un cuarto de la RAM de la máquina. Un nodo para un
# bot de música vive cómodo con un heap chico y el GC serial (un solo hilo).
argumentos_comunes=(
  --name "$NOMBRE"
  --memory "${LAVALINK_MEMORY:-640m}"
  --cpus "${LAVALINK_CPUS:-1.5}"
  --env "JAVA_TOOL_OPTIONS=-Xms64m -Xmx${LAVALINK_HEAP:-384m} -XX:+UseSerialGC -XX:MaxMetaspaceSize=128m"
  --publish "127.0.0.1:$PUERTO:2333"
  --volume "$AQUI/application.yml:/opt/Lavalink/application.yml:ro,Z"
  --volume "$PLUGINS_DIR:/opt/Lavalink/plugins:ro,Z"
  --volume "$DJ_CACHE:$DJ_CACHE:ro,z"
)

case "${1:-start}" in
  start)
    if responde; then
      echo "Lavalink ya está arriba en el puerto $PUERTO."
      exit 0
    fi
    instalar_plugin
    mkdir -p "$DJ_CACHE"
    podman rm -f "$NOMBRE" >/dev/null 2>&1 || true
    podman run -d --replace "${argumentos_comunes[@]}" "$IMAGEN" >/dev/null

    echo -n "Esperando a Lavalink"
    for _ in $(seq 1 90); do
      if responde; then
        echo " — listo en http://127.0.0.1:$PUERTO"
        exit 0
      fi
      if ! corriendo; then
        echo " — el contenedor se cayó:"
        podman logs --tail 30 "$NOMBRE" 2>&1 || true
        exit 1
      fi
      echo -n "."
      sleep 1
    done
    echo " — no respondió en 90 s. Últimas líneas:"
    podman logs --tail 30 "$NOMBRE" 2>&1 || true
    exit 1
    ;;

  stop)
    if podman rm -f "$NOMBRE" >/dev/null 2>&1; then
      echo "Lavalink detenido."
    else
      echo "No estaba corriendo."
    fi
    ;;

  status)
    if responde; then
      echo "arriba (puerto $PUERTO)"
    elif corriendo; then
      echo "arrancando (contenedor vivo, todavía no responde)"
      exit 1
    else
      echo "abajo"
      exit 1
    fi
    ;;

  logs)
    podman logs -f "$NOMBRE"
    ;;

  run)
    instalar_plugin
    mkdir -p "$DJ_CACHE"
    podman rm -f "$NOMBRE" >/dev/null 2>&1 || true
    exec podman run --rm -it "${argumentos_comunes[@]}" "$IMAGEN"
    ;;

  *)
    echo "Uso: $(basename "$0") [start|stop|status|logs|run]" >&2
    exit 2
    ;;
esac
