#!/usr/bin/python3
"""Panel de control de LavaDJ: levantar, cortar y refrescar noticias.

Corre con el python del SISTEMA (necesita GTK4/libadwaita, que no están en el
venv del proyecto) y lanza el bot con el python del venv.

No sabe nada del proceso del bot: le habla por HTTP a la API que el propio bot
levanta en localhost (core/api.py). Lo único que hace "a la antigua" es el
arranque, que sí necesita lanzar un proceso.

Aquí solo vive la ventana. La lógica sin GTK está en lavadj_panel_logica.py (con
tests) y el estilo en lavadj-panel.css, los dos junto a este archivo.

El aparato tiene dos acabados —noche y día— y el testigo de aire es rojo en los
dos: una lámpara de ON AIR no cambia de color según la luz de la sala.
"""

import sys
import threading
import time
import webbrowser
from pathlib import Path

from lavadj_panel_logica import (
    API_BASE, LINEAS_MONITOR, LOG_FILE, REFRESCO, START_LOG, aviso_de_salud,
    aviso_sin_titulares, botones_activos, consultar, cortar_todo, estado_bot,
    guardar_ajuste, lavalink_arriba, leer_ajustes, levantar_bot, levantar_lavalink,
    lineas_de_log, motivo_de_la_caida, postear, resumen_de_titulares,
    texto_de_estado, veredicto_de_arranque,
)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

ESTILO = Path(__file__).with_name("lavadj-panel.css")


class Panel(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LavaDJ")
        self.add_css_class("panel")
        self.set_default_size(430, 330)
        self.esta_al_aire = False
        self.ocupado = False       # hay una tarea larga en un hilo
        self.tarea = ""            # qué es esa tarea, para el detalle
        self.ticks = 0             # vueltas del refresco, para lo que no va cada vez
        self.vigilando = None      # (posición del log, hasta cuándo esperamos)
        self.foco_puesto_para = None   # para qué estado acomodamos el foco
        self.envio_volumen = None      # timeout pendiente para mandar el volumen
        self.volumen_puesto = False    # ya le pasamos el volumen a ESTE bot
        # Normalmente el cajón muestra el log del bot; si el bot se cayó al
        # arrancar, el traceback está en el otro archivo y ahí hay que mirar.
        self.fuente_log = LOG_FILE

        # Los avisos van a toasts; el estado se queda quieto en su etiqueta.
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        raiz = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toasts.set_child(raiz)

        barra = Adw.HeaderBar(css_classes=["flat"])
        barra.set_title_widget(self._legend("LAVADJ · TRANSMISOR"))
        self.piloto = Gtk.Label(label="AUDIO", css_classes=["piloto"],
                                valign=Gtk.Align.CENTER,
                                tooltip_text="Nodo Lavalink")
        barra.pack_start(self.piloto)

        # El testigo de averías. Como en cualquier aparato: apagado mientras
        # todo anda, encendido cuando hay algo que mirar. Existe porque el bot
        # puede estar vivo y contestando y aun así no estar sonando nada.
        self.pulso = Gtk.Label(label="", css_classes=["piloto"],
                               valign=Gtk.Align.CENTER, visible=False)
        barra.pack_start(self.pulso)

        # La luz del panel: en un aparato esto es un dimmer, no un "tema".
        self.boton_luz = Gtk.Button(css_classes=["flat"])
        self.boton_luz.connect("clicked", self.cambiar_luz)
        barra.pack_end(self.boton_luz)
        raiz.append(barra)

        chasis = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            css_classes=["chasis"], vexpand=True,
        )
        # Sin esto, al estirar la ventana los botones se desparraman a lo ancho.
        marco = Adw.Clamp(maximum_size=420, tightening_threshold=380, child=chasis)
        raiz.append(marco)

        # Estado: testigo + palabra + detalle.
        fila = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.testigo = Gtk.Box(css_classes=["testigo"], valign=Gtk.Align.CENTER)
        fila.append(self.testigo)
        columna = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.estado = Gtk.Label(xalign=0, css_classes=["estado"])
        self.detalle = Gtk.Label(xalign=0, css_classes=["detalle"], ellipsize=3)
        columna.append(self.estado)
        columna.append(self.detalle)
        fila.append(columna)
        chasis.append(fila)

        # Controles.
        controles = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
            homogeneous=True, vexpand=True, valign=Gtk.Align.CENTER,
        )
        self.boton_aire = Gtk.Button(css_classes=["aire"])
        self.boton_aire.set_child(Adw.ButtonContent(
            icon_name="media-record-symbolic", label="Salir al aire"))
        self.boton_aire.connect("clicked", self.al_aire)
        self.boton_cortar = Gtk.Button(css_classes=["destructive-action"],
                                       tooltip_text="Corta la transmisión y baja también el nodo de audio")
        self.boton_cortar.set_child(Adw.ButtonContent(
            icon_name="media-playback-stop-symbolic", label="Cortar"))
        self.boton_cortar.connect("clicked", self.cortar_transmision)
        controles.append(self.boton_aire)
        controles.append(self.boton_cortar)
        chasis.append(controles)

        self.boton_noticias = Gtk.Button(
            tooltip_text="Baja los titulares de nuevo y los muestra aquí abajo",
        )
        self.boton_noticias.set_child(Adw.ButtonContent(
            icon_name="view-refresh-symbolic", label="Refrescar noticias"))
        self.boton_noticias.connect("clicked", self.refrescar_noticias)
        chasis.append(self.boton_noticias)

        # Volumen de la voz del DJ: un dial más del aparato, no un ajuste de app.
        chasis.append(self._control_de_voz())

        # Monitor: una línea siempre visible y el resto en un cajón que se abre.
        self.monitor = Gtk.Label(xalign=0, css_classes=["monitor"], ellipsize=3)
        raiz.append(self.monitor)

        tiradores = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                            spacing=2, halign=Gtk.Align.END)
        self.tirador_titulares = Gtk.Button(
            css_classes=["tirador", "flat"],
            tooltip_text="Ver los titulares que el DJ tiene en cola")
        self.tirador_titulares.set_child(Adw.ButtonContent(
            icon_name="pan-down-symbolic", label="Titulares"))
        self.tirador_titulares.connect("clicked", self.alternar_titulares)
        tiradores.append(self.tirador_titulares)

        self.tirador = Gtk.Button(css_classes=["tirador", "flat"],
                                  tooltip_text="Ver el log")
        self.tirador.set_child(Adw.ButtonContent(
            icon_name="pan-down-symbolic", label="Log"))
        self.tirador.connect("clicked", self.alternar_cajon)
        tiradores.append(self.tirador)
        raiz.append(tiradores)

        # Cajón de titulares: qué noticias tiene cargadas el DJ esperando salir.
        self.lista_titulares = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4,
            css_classes=["titulares"],
        )
        vitrina = Gtk.ScrolledWindow(
            css_classes=["cajon"], min_content_height=190,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        vitrina.set_child(self.lista_titulares)
        self.cajon_titulares = Gtk.Revealer(
            child=vitrina, transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            transition_duration=180,
        )
        raiz.append(self.cajon_titulares)

        self.vista_log = Gtk.TextView(
            editable=False, cursor_visible=False, monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
        )
        visor = Gtk.ScrolledWindow(
            css_classes=["cajon"], min_content_height=190,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        visor.set_child(self.vista_log)
        self.cajon = Gtk.Revealer(
            child=visor, transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            transition_duration=180,
        )
        raiz.append(self.cajon)

        self.actualizar()
        GLib.timeout_add_seconds(REFRESCO, self.actualizar)

    def _control_de_voz(self) -> Gtk.Widget:
        """Fader del volumen de la voz del DJ: leyenda, corredera y lectura."""
        fila = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        fila.append(self._legend("VOZ DJ"))

        self.volumen = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 5)
        self.volumen.set_hexpand(True)
        self.volumen.set_draw_value(False)   # el número lo pone la lectura de al lado
        self.volumen.add_mark(100, Gtk.PositionType.BOTTOM, None)   # el nivel de fábrica
        self.volumen.set_value(leer_ajustes().get("dj_volumen", 100))
        self.volumen.set_tooltip_text(
            "Volumen de la voz del DJ (0 = mudo).\n"
            "El clip se prepara con la canción anterior: se escucha desde el próximo corte."
        )
        self.volumen.connect("value-changed", self.volumen_movido)
        fila.append(self.volumen)

        self.eco_volumen = Gtk.Label(css_classes=["detalle"], width_chars=4, xalign=1)
        fila.append(self.eco_volumen)
        self._mostrar_volumen()

        voces = Gtk.Button(label="Voces…", css_classes=["flat"],
                           tooltip_text="Elegir y descargar voces: abre el panel web")
        voces.connect("clicked", self.abrir_voces)
        fila.append(voces)
        return fila

    def _legend(self, texto: str) -> Gtk.Label:
        etiqueta = Gtk.Label(css_classes=["legend"])
        etiqueta.set_markup(f'<span letter_spacing="2200">{texto}</span>')
        return etiqueta

    # --- Luz del panel ---
    def aplicar_luz(self, oscuro: bool) -> None:
        self.remove_css_class("dia")
        self.remove_css_class("noche")
        self.add_css_class("noche" if oscuro else "dia")
        # El icono muestra a qué se cambia, no en qué se está.
        self.boton_luz.set_icon_name(
            "weather-clear-symbolic" if oscuro else "weather-clear-night-symbolic")
        self.boton_luz.set_tooltip_text(
            "Encender la luz del panel" if oscuro else "Apagar la luz del panel")

    def cambiar_luz(self, _boton):
        gestor = Adw.StyleManager.get_default()
        oscuro = not gestor.get_dark()
        gestor.set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if oscuro else Adw.ColorScheme.FORCE_LIGHT)
        guardar_ajuste("luz", "noche" if oscuro else "dia")

    # --- Avisos ---
    def avisar(self, texto: str, *, falla: bool = False, con_log: bool = False) -> None:
        """Un toast por evento. El estado del aparato NO se toca aquí."""
        toast = Adw.Toast(title=texto)
        # Un fallo de arranque no debería evaporarse solo: se cierra a mano.
        toast.set_timeout(0 if falla else 4)
        if con_log:
            toast.set_button_label("Ver log")
            toast.connect("button-clicked", lambda *_: self.abrir_cajon(True))
        self.toasts.add_toast(toast)

    def en_hilo(self, trabajo, luego, que: str = "levantando el nodo de audio…") -> None:
        """Corre algo lento sin congelar la ventana. `que` es lo que se muestra."""
        self.ocupado = True
        self.tarea = que

        def correr():
            resultado = trabajo()
            GLib.idle_add(terminar, resultado)

        def terminar(resultado):
            self.ocupado = False
            luego(resultado)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=correr, daemon=True).start()
        self.actualizar()

    # --- Acciones ---
    def abrir_cajon(self, abierto: bool) -> None:
        self.cajon.set_reveal_child(abierto)
        self.tirador.get_child().set_icon_name(
            "pan-up-symbolic" if abierto else "pan-down-symbolic")
        self.tirador.set_tooltip_text("Ocultar el log" if abierto else "Ver el log")
        if abierto:
            # Los dos cajones salen del mismo borde: abiertos a la vez, la
            # ventana se estira al doble y no se ve bien ninguno.
            self.abrir_titulares(False)
            self.volcar_log()

    def alternar_cajon(self, _boton):
        self.abrir_cajon(not self.cajon.get_reveal_child())

    # --- Titulares del DJ ---
    def abrir_titulares(self, abierto: bool, recargar: bool = True) -> None:
        self.cajon_titulares.set_reveal_child(abierto)
        self.tirador_titulares.get_child().set_icon_name(
            "pan-up-symbolic" if abierto else "pan-down-symbolic")
        self.tirador_titulares.set_tooltip_text(
            "Ocultar los titulares" if abierto
            else "Ver los titulares que el DJ tiene en cola")
        if abierto:
            self.abrir_cajon(False)
            if recargar:
                self.cargar_titulares()

    def alternar_titulares(self, _boton):
        self.abrir_titulares(not self.cajon_titulares.get_reveal_child())

    def cargar_titulares(self) -> None:
        if not self.esta_al_aire:
            return self.pintar_titulares(None, "El bot está fuera del aire.")
        error, datos = consultar("headlines", timeout=1.5)
        if error:
            return self.pintar_titulares(None, f"No pude preguntarle al bot: {error}")
        self.pintar_titulares(datos)

    def pintar_titulares(self, datos: dict | None, aviso: str = "") -> None:
        """Rehace la lista del cajón. `datos` es lo que devolvió la API."""
        while (viejo := self.lista_titulares.get_first_child()) is not None:
            self.lista_titulares.remove(viejo)

        if datos is None:
            self.lista_titulares.append(
                Gtk.Label(label=aviso, xalign=0, css_classes=["titular", "leido"]))
            return

        titulares = datos.get("titulares") or []
        if not titulares:
            self.lista_titulares.append(Gtk.Label(
                label=aviso_sin_titulares(datos), xalign=0, wrap=True,
                css_classes=["titular", "leido"]))
            return

        self.lista_titulares.append(self._legend(resumen_de_titulares(datos)))
        for titular in titulares:
            ya = bool(titular.get("leido"))
            fila = Gtk.Label(
                label=f"{'✓' if ya else '•'}  {titular.get('texto', '')}",
                xalign=0, wrap=True,
                css_classes=["titular", "leido"] if ya else ["titular"],
            )
            self.lista_titulares.append(fila)

    # --- Volumen del DJ ---
    def _mostrar_volumen(self) -> None:
        valor = int(self.volumen.get_value())
        self.eco_volumen.set_text("MUDO" if valor == 0 else f"{valor}%")

    def abrir_voces(self, _boton):
        """El selector de voces vive en el panel web: aquí solo se lo abre."""
        if not self.esta_al_aire:
            self.avisar("El bot está fuera del aire: sin él no hay panel de voces.")
        elif not webbrowser.open(API_BASE.removesuffix("/api") + "/"):
            self.avisar("No pude abrir el navegador.", falla=True)

    def volumen_movido(self, _escala):
        """Espera a que la mano se quede quieta antes de molestar al bot."""
        self._mostrar_volumen()
        if self.envio_volumen:
            GLib.source_remove(self.envio_volumen)
        self.envio_volumen = GLib.timeout_add(400, self.mandar_volumen)

    def mandar_volumen(self, avisar_fallos: bool = True):
        self.envio_volumen = None
        valor = int(self.volumen.get_value())
        guardar_ajuste("dj_volumen", valor)
        if self.esta_al_aire:
            error = postear("dj_volume", valor=valor)
            # El envío automático al levantar el bot no molesta con un toast:
            # nadie tocó nada, y un bot viejo sin ese endpoint daría error solo.
            if error and avisar_fallos:
                self.avisar(f"No pude mover el volumen del DJ: {error}", falla=True)
        return GLib.SOURCE_REMOVE

    def al_aire(self, _boton):
        # Se desactiva ya mismo, no cuando el bot conteste: dos clicks seguidos
        # lanzarían dos procesos y el segundo se cae peleando por el puerto.
        self.boton_aire.set_sensitive(False)
        # Sin nodo de audio el bot levanta pero no suena nada, así que el botón
        # se encarga de los dos: primero Lavalink, después el bot.
        if lavalink_arriba():
            self.arrancar_el_bot()
            return
        self.en_hilo(levantar_lavalink, self.tras_lavalink)

    def tras_lavalink(self, error: str):
        if error:
            self.avisar(f"El nodo de audio no levantó: {error}", falla=True)
            return
        self.arrancar_el_bot()

    def arrancar_el_bot(self):
        error, marca = levantar_bot()
        if error:
            self.avisar(error, falla=True, con_log=True)
            return
        # Si en 20 s no contesta la API, algo lo mató: hay que decir qué.
        self.vigilando = (marca, time.monotonic() + 20)

    def cortar_transmision(self, _boton):
        if not self.esta_al_aire:
            return
        self.vigilando = None
        # Esperar el cierre y bajar Lavalink tarda: va en un hilo, como al levantar.
        self.en_hilo(lambda: cortar_todo("boton Cortar del panel"), self.tras_cortar, que="apagando todo…")

    def tras_cortar(self, error: str):
        if error:
            self.avisar(f"No pude apagar todo: {error}", falla=True)
        else:
            self.avisar("Transmisión cortada y Lavalink abajo")

    def refrescar_noticias(self, _boton):
        if not self.esta_al_aire:
            return
        # Ahora la bajada ocurre aquí mismo (varios feeds con su timeout), así
        # que va en un hilo: si no, la ventana se queda dura unos segundos.
        self.en_hilo(
            lambda: consultar("refresh_news", "POST", timeout=20),
            self.tras_refrescar,
            que="bajando titulares…",
        )

    def tras_refrescar(self, resultado):
        error, datos = resultado
        if error:
            self.avisar(f"No pude refrescar: {error}", falla=True)
            return
        self.abrir_titulares(True, recargar=False)
        self.pintar_titulares(datos)
        cuantos = len(datos.get("titulares") or [])
        self.avisar(f"{cuantos} titulares en cola" if cuantos
                    else "Ningún feed devolvió titulares")

    # --- Estado ---
    def volcar_log(self):
        texto = "\n".join(lineas_de_log(LINEAS_MONITOR, self.fuente_log)) or "Sin log todavía"
        buffer = self.vista_log.get_buffer()
        if buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == texto:
            return
        buffer.set_text(texto)
        # Pegado abajo, como cualquier consola.
        self.vista_log.scroll_to_iter(buffer.get_end_iter(), 0, False, 0, 0)

    def revisar_arranque(self, estado: dict | None) -> None:
        """Vigila el arranque para no dejar al bot muriendo en silencio."""
        veredicto = veredicto_de_arranque(self.vigilando, estado, time.monotonic())
        if not veredicto:
            return
        marca = self.vigilando[0]
        self.vigilando = None
        if veredicto == "levanto":
            self.fuente_log = LOG_FILE
            return
        # El traceback del arranque no pasa por bot.log: el cajón tiene que
        # apuntar al archivo donde sí está, o "Ver log" no muestra nada útil.
        self.fuente_log = START_LOG
        motivo = motivo_de_la_caida(marca)
        self.avisar(motivo or "El bot no levantó", falla=True, con_log=True)

    def pintar_salud(self, estado: dict | None) -> None:
        """Enciende el testigo de averías, o lo apaga si no hay nada que decir."""
        aviso = aviso_de_salud(estado)
        if aviso is None:
            self.pulso.set_visible(False)
            return
        texto, clase, detalle = aviso
        self.pulso.set_text(texto)
        self.pulso.set_css_classes(["piloto", clase])
        self.pulso.set_tooltip_text(detalle)
        self.pulso.set_visible(True)

    def actualizar(self, *_):
        estado = estado_bot()
        self.esta_al_aire = estado is not None
        al_aire = self.esta_al_aire
        self.ticks += 1
        self.revisar_arranque(estado)

        # El bot arranca con el volumen del .env y no sabe dónde quedó el fader;
        # la posición del panel manda, así que se la pasamos apenas levanta.
        if al_aire and not self.volumen_puesto:
            self.volumen_puesto = True
            self.mandar_volumen(avisar_fallos=False)
        elif not al_aire:
            self.volumen_puesto = False

        for widget in (self.testigo, self.estado):
            if al_aire:
                widget.add_css_class("al-aire")
            else:
                widget.remove_css_class("al-aire")
        # El latido solo si el sistema quiere animaciones: es una luz que pulsa
        # en un panel que puede quedar abierto horas.
        if al_aire and Gtk.Settings.get_default().get_property("gtk-enable-animations"):
            self.testigo.add_css_class("late")
        else:
            self.testigo.remove_css_class("late")

        self.estado.set_text("AL AIRE" if al_aire else "FUERA DEL AIRE")

        # El piloto de audio: si el bot está arriba se lo preguntamos a él, que
        # sabe si llegó a enganchar el nodo; si no, miramos el nodo directo.
        if estado:
            audio = bool(estado.get("lavalink", {}).get("conectado"))
        else:
            audio = lavalink_arriba()
        if audio:
            self.piloto.add_css_class("encendido")
        else:
            self.piloto.remove_css_class("encendido")
        self.piloto.set_tooltip_text(
            "Nodo Lavalink conectado" if audio
            else "Sin nodo Lavalink: no va a sonar nada")

        self.pintar_salud(estado)

        self.detalle.set_css_classes(["detalle"] if al_aire else ["detalle", "apagado"])
        self.detalle.set_text(
            texto_de_estado(estado, self.ocupado, self.tarea, bool(self.vigilando)))

        aire, cortar, noticias = botones_activos(al_aire, self.ocupado, bool(self.vigilando))
        self.boton_aire.set_sensitive(aire)
        self.boton_cortar.set_sensitive(cortar)
        self.boton_noticias.set_sensitive(noticias)

        # Cuando el bot levanta, "Salir al aire" se apaga y GTK le pasa el foco
        # al botón de al lado, que es CORTAR: el siguiente Espacio o Enter te
        # tumbaba la transmisión sin que la tocaras. El foco se manda a mano a
        # un botón que no hace daño.
        if al_aire is not self.foco_puesto_para:
            self.foco_puesto_para = al_aire
            self.set_focus(self.boton_noticias if al_aire else self.boton_aire)

        ultima = lineas_de_log(1, self.fuente_log)
        self.monitor.set_text(ultima[0] if ultima else "Sin log todavía")
        if self.cajon.get_reveal_child():
            self.volcar_log()
        # Los titulares cambian de a poco (uno por corte del DJ): rehacer la
        # lista en cada vuelta sería una consulta HTTP cada dos segundos al pedo.
        if self.cajon_titulares.get_reveal_child() and self.ticks % 5 == 0:
            self.cargar_titulares()
        return GLib.SOURCE_CONTINUE


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.huffy_it.lavadj.panel")

    def do_activate(self):
        gestor = Adw.StyleManager.get_default()
        # Sin preferencia guardada seguimos al sistema; en cuanto se toca el
        # botón, manda la elección.
        guardado = leer_ajustes().get("luz")
        if guardado == "noche":
            gestor.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        elif guardado == "dia":
            gestor.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)

        try:
            css = ESTILO.read_bytes()
        except OSError as error:
            print(f"lavadj-panel: arranco sin estilo, no pude leer el CSS: {error}",
                  file=sys.stderr)
        else:
            proveedor = Gtk.CssProvider()
            proveedor.load_from_data(css)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), proveedor,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        ventana = Panel(self)
        ventana.aplicar_luz(gestor.get_dark())
        gestor.connect("notify::dark",
                       lambda g, _p: ventana.aplicar_luz(g.get_dark()))
        ventana.present()


if __name__ == "__main__":
    App().run(None)
