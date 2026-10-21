"""Acciones de reproducción usando Wavelink.
Todas devuelven el texto que se le muestra al usuario y son asíncronas.
"""

import wavelink

async def pause(vc: wavelink.Player) -> str:
    if vc and vc.playing:
        await vc.pause(True)
        return "Pausado ⏸️"
    return "No hay nada reproduciendo."

async def resume(vc: wavelink.Player) -> str:
    if vc and vc.paused:
        await vc.pause(False)
        return "Reanudado ▶️"
    return "No está pausado."

async def toggle_pause(vc: wavelink.Player) -> str:
    if vc and vc.paused:
        return await resume(vc)
    return await pause(vc)

async def skip(vc: wavelink.Player) -> str:
    if not vc or not vc.playing:
        return "No hay nada reproduciendo."
    await vc.skip(force=True)
    return "Skipped ⏭️"

async def toggle_loop(vc: wavelink.Player) -> str:
    if not vc:
        return "No hay reproductor conectado."
    if vc.queue.mode == wavelink.QueueMode.normal:
        vc.queue.mode = wavelink.QueueMode.loop
        return "Loop activado 🔁"
    else:
        vc.queue.mode = wavelink.QueueMode.normal
        return "Loop desactivado"

def shuffle(vc: wavelink.Player) -> str:
    if not vc or not vc.queue:
        return "La cola está vacía."
    vc.queue.shuffle()
    return "Cola mezclada 🔀"

async def set_volume(vc: wavelink.Player, pct: int) -> str:
    if not 0 <= pct <= 100:
        return "El volumen debe ser entre 0 y 100."
    if vc:
        await vc.set_volume(pct)
    return f"Volumen: {pct}% 🔊"

async def nudge_volume(vc: wavelink.Player, delta: int) -> str:
    if not vc:
        return "No hay reproductor."
    pct = max(0, min(100, vc.volume + delta))
    return await set_volume(vc, pct)
