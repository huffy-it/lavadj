#!/usr/bin/env bash
# Crea el acceso directo del panel de LavaDJ en el escritorio y en el menú.
# Reejecutable: pisa lo que haya.
set -euo pipefail

PROYECTO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PANEL="$PROYECTO/scripts/lavadj-panel.py"
ICONO="$PROYECTO/scripts/lavadj.svg"
NOMBRE="io.github.huffy_it.lavadj.panel.desktop"

ESCRITORIO="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Escritorio")"
MENU="$HOME/.local/share/applications"
mkdir -p "$MENU" "$ESCRITORIO"

# Compat: quita los accesos directos de antes del cambio de nombre
# (huffybot-panel.desktop) para que no queden dos lanzadores del mismo panel.
rm -f "$MENU/huffybot-panel.desktop" "$ESCRITORIO/huffybot-panel.desktop"

# El panel necesita GTK4/libadwaita: van con el python del sistema, no con el
# del venv (que no los tiene).
if ! /usr/bin/python3 -c "import gi; gi.require_version('Adw', '1')" 2>/dev/null; then
  echo "Aviso: al python del sistema le falta GTK4/libadwaita (python3-gobject)." >&2
fi

# Path= importa: sin él el lanzador hereda el directorio de quien lo abrió, y el
# bot terminaría arrancando con el cwd equivocado.
cat > "$MENU/$NOMBRE" <<EOF
[Desktop Entry]
Type=Application
Name=LavaDJ
GenericName=Control de transmisión
Comment=Levantar, cortar y refrescar las noticias del bot de Discord
Exec=/usr/bin/python3 $PANEL
Path=$PROYECTO
Icon=$ICONO
Terminal=false
Categories=AudioVideo;
StartupNotify=true
EOF

chmod +x "$MENU/$NOMBRE"
cp "$MENU/$NOMBRE" "$ESCRITORIO/$NOMBRE"
chmod +x "$ESCRITORIO/$NOMBRE"
# KDE pide que el .desktop sea de confianza para no mostrarlo como texto.
gio set "$ESCRITORIO/$NOMBRE" metadata::trusted true 2>/dev/null || true
update-desktop-database "$MENU" 2>/dev/null || true

if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$MENU/$NOMBRE" || echo "El .desktop tiene observaciones." >&2
fi

echo "Acceso directo en $ESCRITORIO/$NOMBRE y en el menú de aplicaciones."
