"""La letra en vivo: el mensaje que se edita línea a línea.

Sin red ni Discord: el canal y el mensaje son dobles, y el reloj, la posición y el sueño del bucle los mueve la `Escena`.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

from core import letra_en_vivo as mod
from core.letra_en_vivo import (
    EDITAR_CADA, ESPERA_MAX, ESPERA_MIN, HABLA_EL_DJ, LIMITE, SIN_LETRA, SIN_TIEMPOS, LetraEnVivo, armar_texto,
    espera_hasta, indice_de,
)
from core.lyrics import Letra

LINEAS = [(2000, "Uno"), (6000, "Dos"), (10000, ""), (14000, "Cuatro"), (18000, "Cinco")]
LETRA = Letra(True, LINEAS)
TIEMPOS = [ms for ms, _ in LINEAS]
FIN = 22        # s: cuando se acaba el tema de los ejemplos


def _http(estado=500):
    return discord.HTTPException(SimpleNamespace(status=estado, reason="x"), "falló")


def _no_encontrado():
    return discord.NotFound(SimpleNamespace(status=404, reason="x"), "Unknown Message")


@pytest.mark.parametrize("posicion, esperado", [
    (0, -1), (1999, -1), (2000, 0), (5999, 0), (6000, 1), (9999, 1), (10000, 2), (18000, 4), (10**9, 4),
])
def test_la_linea_actual_es_la_ultima_que_ya_empezo(posicion, esperado):
    assert indice_de(TIEMPOS, posicion) == esperado
    assert indice_de([], posicion) == -1       # sin líneas no hay actual


@pytest.mark.parametrize("posicion, indice, esperado", [
    (2000, 0, ESPERA_MAX),          # faltan 4 s: se duerme el tope, no hasta la línea
    (5500, 0, 0.5),                 # falta medio segundo: se duerme justo eso
    (5999, 0, ESPERA_MIN),          # falta un milisegundo: no se da vueltas en vacío
    (0, -1, 1.0),                   # antes de la primera, la próxima es la 0 (a 2 s)
    (1700, -1, 0.3),
    (18000, 4, ESPERA_MAX),         # la última no tiene próxima
])
def test_cuanto_se_duerme_hasta_la_proxima_linea(posicion, indice, esperado):
    assert espera_hasta(TIEMPOS, posicion, indice) == pytest.approx(esperado)


CINCO = [(0, "Uno"), (1000, "Dos"), (2000, "Tres"), (3000, "Cuatro"), (4000, "Cinco")]


@pytest.mark.parametrize("lineas, indice, esperado", [
    (CINCO, 2, "🎤 Un tema\n-# Dos\n**Tres**\nCuatro\nCinco"),      # anterior gris, actual en negrita, dos siguientes
    (LINEAS, 0, "🎤 Un tema\n**Uno**\nDos\n♪"),                     # la primera no tiene anterior
    (LINEAS, -1, "🎤 Un tema\n**♪**\nUno\nDos"),                    # antes de la primera: la pausa y lo que viene
    (LINEAS, 3, "🎤 Un tema\n-# ♪\n**Cuatro**\nCinco"),             # las últimas no inventan siguientes
    (LINEAS, 4, "🎤 Un tema\n-# Cuatro\n**Cinco**"),
    (LINEAS, 1, "🎤 Un tema\n-# Uno\n**Dos**\n♪\nCuatro"),          # una línea vacía es pausa instrumental en cualquier lugar
    (LINEAS, 2, "🎤 Un tema\n-# Dos\n**♪**\nCuatro\nCinco"),
    ([(0, "   "), (1, "x")], 0, "🎤 Un tema\n**♪**\nx"),               # solo espacios también es pausa
    ([(0, "uno\n-# dos\r\n  tres")], 0, "🎤 Un tema\n**uno -# dos tres**"),   # los saltos se juntan: un `-#` de adentro no queda al inicio
])
def test_el_mensaje_arma_titulo_anterior_actual_y_siguientes(lineas, indice, esperado):
    assert armar_texto("Un tema", lineas, indice) == esperado


def test_la_letra_viene_de_afuera_y_se_escapa():
    lineas = [(0, "# titulo"), (1, "**negrita** y ||spoiler||"), (2, "> cita"), (3, "`codigo` _cursiva_ ~~tachado~~")]
    texto = armar_texto("**El** tema", lineas, 1)
    assert texto == (
        "🎤 \\*\\*El\\*\\* tema\n"
        "-# \\# titulo\n"
        "**\\*\\*negrita\\*\\* y \\|\\|spoiler\\|\\|**\n"
        "\\> cita\n"
        "\\`codigo\\` \\_cursiva\\_ \\~\\~tachado\\~\\~"
    )


def test_el_mensaje_cabe_en_un_mensaje_de_discord_aunque_la_letra_sea_enorme():
    enorme = [(i, "*" * 5000) for i in range(5)]       # el peor caso: cada `*` se escapa y dobla
    for indice in (-1, 0, 2, 4):
        assert len(armar_texto("_" * 5000, enorme, indice)) <= LIMITE


def test_cada_linea_y_el_titulo_se_recortan():
    texto = armar_texto("t" * 5000, [(0, "a" * 5000), (1, "b" * 5000)], 0)
    assert texto == f"🎤 {'t' * mod.LARGO_TITULO}\n**{'a' * mod.LARGO_LINEA}**\n{'b' * mod.LARGO_LINEA}"


def test_si_los_recortes_no_alcanzaran_el_tope_de_discord_corta(monkeypatch):
    monkeypatch.setattr(mod, "LARGO_LINEA", 5000)
    texto = armar_texto("T", [(i, "x" * 5000) for i in range(5)], 2)
    assert len(texto) == LIMITE


class Mensaje:
    def __init__(self, canal, contenido):
        self.canal, self.contenido, self.borrado = canal, contenido, False

    async def edit(self, **kwargs):
        if self.borrado:
            raise _no_encontrado()
        if self.canal.fallas:
            raise self.canal.fallas.pop(0)
        self.contenido = kwargs["content"]
        self.canal.anotar("edit", self.contenido, kwargs)

    async def delete(self):
        if self.canal.falla_al_borrar:
            raise _http()
        self.borrado = True
        self.canal.anotar("delete", self.contenido, {})


class Canal:
    """Anota qué se hizo y cuándo (en tiempo de la escena) y en qué posición del tema."""

    def __init__(self, escena):
        self.escena, self.mensajes, self.hechos, self.opciones = escena, [], [], []
        self.fallas, self.falla_al_borrar, self.falla_el_envio = [], False, None

    def anotar(self, accion, contenido, kwargs):
        self.hechos.append(SimpleNamespace(accion=accion, t=self.escena.t, pos=self.escena.pos, contenido=contenido))
        self.opciones.append(kwargs)

    async def send(self, contenido=None, **kwargs):
        if self.falla_el_envio:
            raise self.falla_el_envio
        self.mensajes.append(Mensaje(self, contenido))
        self.anotar("send", contenido, kwargs)
        return self.mensajes[-1]

    @property
    def publicaciones(self):
        return [h for h in self.hechos if h.accion != "delete"]


class Escena:
    """Un tema sonando con reloj, posición y sueño controlables: dormir avanza el reloj (y la posición, si no hay pausa)."""

    def __init__(self):
        self.t, self.pos, self.eventos, self.dormidas = 0.0, 0, [], []
        self.pista = object()
        self.vc = SimpleNamespace(connected=True, paused=False, current=self.pista)
        self.canal = Canal(self)
        self.congelar, self.congelada, self._libre = None, False, asyncio.Event()

    def reloj(self):
        return self.t

    def posicion(self, vc):
        return self.pos

    def a_los(self, segundos, accion):
        self.eventos = sorted([*self.eventos, (segundos, accion)], key=lambda e: e[0])

    def termina_a_los(self, segundos):
        self.a_los(segundos, lambda: setattr(self.vc, "current", object()))

    def _avanzar(self, segundos):
        self.t += segundos
        if not self.vc.paused:
            self.pos += round(segundos * 1000)

    async def dormir(self, segundos):
        self.dormidas.append(segundos)
        fin = self.t + segundos
        while self.eventos and self.eventos[0][0] <= fin:
            momento, accion = self.eventos.pop(0)
            self._avanzar(momento - self.t)
            accion()
        self._avanzar(fin - self.t)
        if self.congelar is not None and self.t >= self.congelar:
            self.congelar, self.congelada = None, True
            self._libre.clear()
            await self._libre.wait()
            self.congelada = False
        await asyncio.sleep(0)

    def soltar(self):
        self._libre.set()

    def en_vivo(self, **kwargs):
        return LetraEnVivo(reloj=self.reloj, dormir=self.dormir, **kwargs)

    def seguir(self, en_vivo, letra=LETRA, titulo="Tema"):
        en_vivo.seguir(self.vc, self.pista, self.canal, titulo=titulo, posicion=self.posicion, obtener=desde(letra))
        return en_vivo.tarea

    async def correr(self, letra=LETRA, titulo="Tema"):
        """Sigue el tema hasta que se acaba (hay que dar un `termina_a_los`); devuelve el seguimiento."""
        en_vivo = self.en_vivo()
        await asyncio.wait_for(self.seguir(en_vivo, letra, titulo), timeout=5)
        return en_vivo


def desde(letra):
    async def obtener():
        if isinstance(letra, Exception):
            raise letra
        return letra
    return obtener


async def _hasta(condicion, vueltas=500):
    for _ in range(vueltas):
        if condicion():
            return
        await asyncio.sleep(0)
    raise AssertionError("no llegó a pasar lo esperado")


@pytest.fixture
def escena():
    return Escena()


async def test_publica_al_empezar_y_edita_en_cada_cambio_de_linea(escena):
    escena.termina_a_los(FIN)
    await escena.correr()
    esperado = [armar_texto("Tema", LINEAS, i) for i in (-1, 0, 1, 2, 3, 4)]
    assert [h.contenido for h in escena.canal.hechos] == esperado
    assert [h.accion for h in escena.canal.hechos] == ["send"] + ["edit"] * 5
    assert [h.t for h in escena.canal.hechos] == [0, 2, 6, 10, 14, 18]
    assert len(escena.canal.mensajes) == 1
    assert ESPERA_MIN <= min(escena.dormidas) and max(escena.dormidas) <= ESPERA_MAX     # duerme hasta la próxima línea, con tope


async def test_sin_cambio_de_linea_no_edita(escena):
    escena.termina_a_los(5)         # de 2 s a 6 s es la misma línea
    await escena.correr()
    assert [h.contenido for h in escena.canal.hechos] == [armar_texto("Tema", LINEAS, -1), armar_texto("Tema", LINEAS, 0)]
    assert len(escena.dormidas) > 3         # miró la posición varias veces y no tocó nada


async def test_si_las_lineas_pasan_mas_rapido_se_saltean_las_intermedias(escena):
    rapidas = [(i * 300, f"l{i}") for i in range(40)]
    tiempos = [ms for ms, _ in rapidas]
    escena.termina_a_los(10)
    await escena.correr(Letra(True, rapidas))
    hechos = escena.canal.hechos
    assert len(hechos) <= int(10 / EDITAR_CADA) + 1 < len(rapidas)
    assert all(b.t - a.t >= EDITAR_CADA - 1e-9 for a, b in zip(hechos, hechos[1:]))
    # Cada edición muestra la línea de ese momento, no una que ya pasó.
    assert all(h.contenido == armar_texto("Tema", rapidas, indice_de(tiempos, h.pos)) for h in hechos)


async def test_en_pausa_no_edita_y_al_reanudar_pone_la_linea_que_toca(escena):
    escena.a_los(5, lambda: setattr(escena.vc, "paused", True))
    escena.a_los(6, lambda: setattr(escena, "pos", 12_000))        # algo mueve la posición mientras está en pausa
    escena.a_los(9, lambda: setattr(escena.vc, "paused", False))
    escena.termina_a_los(FIN)
    await escena.correr()
    hechos = escena.canal.hechos
    assert [h for h in hechos if 5 <= h.t < 9] == []
    despues = [h for h in hechos if h.t >= 9]
    assert despues and despues[0].contenido == armar_texto("Tema", LINEAS, 2)


async def test_si_ya_esta_en_pausa_al_empezar_publica_la_primera_vista_y_nada_mas(escena):
    escena.vc.paused = True
    escena.a_los(3, lambda: setattr(escena, "pos", 20_000))
    escena.termina_a_los(8)
    await escena.correr()
    assert [(h.accion, h.contenido) for h in escena.canal.hechos] == [("send", armar_texto("Tema", LINEAS, -1))]


async def test_un_salto_en_el_tema_se_nota_en_menos_de_un_tope(escena):
    escena.a_los(5, lambda: setattr(escena, "pos", escena.pos + 20_000))
    escena.termina_a_los(FIN)
    await escena.correr()
    salto = [h for h in escena.canal.hechos if h.t >= 5][0]
    assert salto.contenido == armar_texto("Tema", LINEAS, 4)
    assert salto.t - 5 <= ESPERA_MAX


@pytest.mark.parametrize("campo, valor", [("current", object()), ("connected", False)])
async def test_si_el_tema_cambia_o_se_desconecta_el_seguimiento_acaba_y_deja_el_mensaje(escena, campo, valor):
    escena.a_los(7, lambda: setattr(escena.vc, campo, valor))
    en_vivo = await escena.correr()
    assert en_vivo.tarea is None
    assert [h.accion for h in escena.canal.hechos] == ["send", "edit", "edit"]
    assert not escena.canal.mensajes[0].borrado


@pytest.mark.parametrize("letra, aviso", [(Letra(False, [(None, "uno"), (None, "dos")]), SIN_TIEMPOS), (None, SIN_LETRA)])
async def test_sin_tiempos_o_sin_letra_lo_dice_y_no_hay_bucle(escena, letra, aviso):
    en_vivo = await escena.correr(letra)
    assert [h.contenido for h in escena.canal.hechos] == [aviso]
    assert escena.dormidas == [] and en_vivo.tarea is None
    assert "`/lyrics`" in SIN_TIEMPOS


async def test_si_buscar_la_letra_revienta_se_anota_y_dice_que_no_hay(escena, caplog):
    with caplog.at_level("WARNING", logger="letra"):
        await escena.correr(RuntimeError("sin red"))
    assert [h.contenido for h in escena.canal.hechos] == [SIN_LETRA]
    assert "No pude buscar la letra de 'Tema'" in caplog.text


async def test_el_mensaje_no_menciona_a_nadie_aunque_la_letra_traiga_un_everyone(escena):
    escena.termina_a_los(8)
    await escena.correr(Letra(True, [(0, "hola @everyone"), (3000, "<@123> y <@&456>")]), titulo="@here")
    assert any("@everyone" in h.contenido for h in escena.canal.hechos)
    assert escena.canal.opciones and all(
        o["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict() for o in escena.canal.opciones
    )


async def test_el_dj_edita_el_mensaje_y_corta_el_seguimiento(escena):
    escena.congelar = 3
    en_vivo = escena.en_vivo()
    tarea = escena.seguir(en_vivo)
    await _hasta(lambda: escena.congelada)
    await en_vivo.dj(escena.canal)
    assert escena.canal.mensajes[0].contenido == HABLA_EL_DJ
    assert tarea.cancelled() and en_vivo.tarea is None
    assert len(escena.canal.mensajes) == 1


async def test_si_habla_el_dj_sin_mensaje_lo_publica(escena):
    en_vivo = escena.en_vivo()
    await en_vivo.dj(escena.canal)
    assert [h.contenido for h in escena.canal.hechos] == [HABLA_EL_DJ]


async def test_si_no_puede_avisar_del_dj_lo_anota_y_sigue(escena, caplog):
    escena.canal.falla_el_envio = _http(403)
    with caplog.at_level("WARNING", logger="letra"):
        await escena.en_vivo().dj(escena.canal)
    assert "habla el DJ" in caplog.text


async def test_si_borraron_el_mensaje_lo_vuelve_a_publicar_y_sigue_editando_el_nuevo(escena):
    escena.congelar = 3
    escena.termina_a_los(FIN)
    en_vivo = escena.en_vivo()
    tarea = escena.seguir(en_vivo)
    await _hasta(lambda: escena.congelada)
    escena.canal.mensajes[0].borrado = True
    escena.soltar()
    await asyncio.wait_for(tarea, timeout=5)
    primero, segundo = escena.canal.mensajes
    assert segundo.contenido == armar_texto("Tema", LINEAS, 4) and primero.contenido == armar_texto("Tema", LINEAS, 0)
    assert [h.accion for h in escena.canal.hechos] == ["send", "edit", "send", "edit", "edit", "edit"]
    assert en_vivo.mensaje is segundo


async def test_un_fallo_suelto_al_editar_se_reintenta_en_la_proxima_edicion(escena):
    escena.canal.fallas = [_http()]
    escena.termina_a_los(FIN)
    await escena.correr()
    publicadas = [h for h in escena.canal.hechos if h.t >= 2]
    assert publicadas[0].contenido == armar_texto("Tema", LINEAS, 0) and publicadas[0].t >= 2 + EDITAR_CADA
    assert escena.canal.fallas == [] and escena.canal.mensajes[0].contenido == armar_texto("Tema", LINEAS, 4)


async def test_tras_varios_fallos_seguidos_deja_el_tema_y_lo_anota_sin_romper(escena, caplog):
    escena.canal.fallas = [_http() for _ in range(mod.FALLOS_MAX)]
    escena.termina_a_los(FIN)
    with caplog.at_level("WARNING", logger="letra"):
        en_vivo = await escena.correr()
    assert escena.canal.fallas == [] and en_vivo.tarea is None
    assert [h.accion for h in escena.canal.hechos] == ["send"]
    assert "No puedo editar la letra en vivo de 'Tema'" in caplog.text


async def test_un_error_inesperado_se_anota_y_no_sale_de_la_tarea(escena, caplog):
    escena.canal.falla_el_envio = RuntimeError("boom")
    with caplog.at_level("WARNING", logger="letra"):
        en_vivo = await escena.correr()
    assert en_vivo.tarea is None
    assert "La letra en vivo de 'Tema' se cortó" in caplog.text


async def test_parar_corta_el_seguimiento_y_borra_el_mensaje_pero_sigue_prendida(escena):
    escena.congelar = 3
    en_vivo = escena.en_vivo()
    en_vivo.activa = True
    tarea = escena.seguir(en_vivo)
    await _hasta(lambda: escena.congelada)
    await en_vivo.parar()
    assert tarea.cancelled() and en_vivo.tarea is None
    assert escena.canal.mensajes[0].borrado and en_vivo.mensaje is None
    assert en_vivo.activa is True


async def test_apagar_ademas_la_deja_apagada(escena):
    en_vivo = escena.en_vivo()
    en_vivo.activa = True
    await en_vivo.dj(escena.canal)
    await en_vivo.apagar()
    assert en_vivo.activa is False and escena.canal.mensajes[0].borrado and en_vivo.mensaje is None


async def test_parar_sin_nada_en_marcha_no_hace_nada_y_una_letra_nueva_esta_apagada_y_vacia(escena):
    en_vivo = escena.en_vivo()
    assert en_vivo.activa is False and en_vivo.mensaje is None and en_vivo.tarea is None
    await en_vivo.parar()
    await en_vivo.apagar()
    assert escena.canal.hechos == [] and en_vivo.activa is False


async def test_si_no_puede_borrar_el_mensaje_no_falla_y_lo_suelta(escena):
    en_vivo = escena.en_vivo()
    await en_vivo.dj(escena.canal)
    escena.canal.falla_al_borrar = True
    await en_vivo.parar()
    assert en_vivo.mensaje is None


async def test_seguir_otro_tema_deja_sin_efecto_el_seguimiento_anterior(escena):
    escena.congelar = 3
    en_vivo = escena.en_vivo()
    anterior = escena.seguir(en_vivo)
    await _hasta(lambda: escena.congelada)
    nueva = escena.seguir(en_vivo, Letra(True, [(0, "otra")]))
    await asyncio.gather(anterior, return_exceptions=True)
    assert anterior.cancelled() and nueva is en_vivo.tarea and nueva is not anterior
    nueva.cancel()


async def test_cancelar_corta_la_tarea_y_es_idempotente(escena):
    escena.congelar = 3
    en_vivo = escena.en_vivo()
    tarea = escena.seguir(en_vivo)
    await _hasta(lambda: escena.congelada)
    en_vivo.cancelar()
    en_vivo.cancelar()
    await asyncio.gather(tarea, return_exceptions=True)
    assert tarea.cancelled() and en_vivo.tarea is None
