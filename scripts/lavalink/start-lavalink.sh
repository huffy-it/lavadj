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
VOLUMEN_PLUGINS="lavadj-lavalink-plugins"

# El DJ sintetiza sus clips aquí y Lavalink los abre con la fuente "local": el
# contenedor tiene que verlos en LA MISMA ruta absoluta que usa el bot, porque
# lo que viaja por el protocolo es la ruta tal cual.
DJ_CACHE="$PROYECTO/dj_cache"

# Compat: antes del cambio de nombre el contenedor se llamaba huffybot-lavalink.
# Si quedó uno vivo, sigue ocupando el puerto 2333 y el nuevo no podría arrancar.
# (El volumen viejo huffybot-lavalink-plugins queda huérfano: el plugin se baja
# de nuevo en el volumen nuevo; se puede borrar con `podman volume rm`.)
podman rm -f huffybot-lavalink >/dev/null 2>&1 || true

corriendo() {
  [ "$(podman container inspect "$NOMBRE" --format '{{.State.Running}}' 2>/dev/null)" = "true" ]
}

# La contraseña sale del propio yml para que no se desincronicen.
CLAVE="$(sed -n 's/^ *password: *"\(.*\)"/\1/p' "$AQUI/application.yml" | head -1)"

responde() {
  # /version responde 401 sin cabecera de autorización, así que hay que mandarla:
  # si no, el chequeo falla aunque el nodo esté perfecto.
  curl -sf -m 2 -H "Authorization: $CLAVE" "http://127.0.0.1:$PUERTO/version" >/dev/null 2>&1
}

# Las etiquetas SELinux importan: sin ellas el contenedor recibe "permission
# denied" al leer el yml y Lavalink arranca en silencio con su configuración por
# defecto (puerto 8080, sin plugins). `Z` reetiqueta privado, `z` compartido —
# dj_cache lo comparte con el bot, que sigue escribiendo ahí.
# Sin tope la JVM se reserva un cuarto de la RAM de la máquina. Un nodo para un
# bot de música vive cómodo con un heap chico y el GC serial (un solo hilo).
argumentos_comunes=(
  --name "$NOMBRE"
  --memory "${LAVALINK_MEMORY:-640m}"
  --cpus "${LAVALINK_CPUS:-1.5}"
  --env "JAVA_TOOL_OPTIONS=-Xms64m -Xmx${LAVALINK_HEAP:-384m} -XX:+UseSerialGC -XX:MaxMetaspaceSize=128m"
  --publish "127.0.0.1:$PUERTO:2333"
  --volume "$AQUI/application.yml:/opt/Lavalink/application.yml:ro,Z"
  --volume "$VOLUMEN_PLUGINS:/opt/Lavalink/plugins"
  --volume "$DJ_CACHE:$DJ_CACHE:ro,z"
)

case "${1:-start}" in
  start)
    if responde; then
      echo "Lavalink ya está arriba en el puerto $PUERTO."
      exit 0
    fi
    mkdir -p "$DJ_CACHE"
    podman rm -f "$NOMBRE" >/dev/null 2>&1 || true
    podman run -d --replace "${argumentos_comunes[@]}" "$IMAGEN" >/dev/null

    # La primera vez baja el plugin de YouTube: puede tardar bastante.
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
    mkdir -p "$DJ_CACHE"
    podman rm -f "$NOMBRE" >/dev/null 2>&1 || true
    exec podman run --rm -it "${argumentos_comunes[@]}" "$IMAGEN"
    ;;

  *)
    echo "Uso: $(basename "$0") [start|stop|status|logs|run]" >&2
    exit 2
    ;;
esac
