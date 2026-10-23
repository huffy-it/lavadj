"""Panel de botones que acompaña a la canción que suena."""

import inspect
import logging
import discord
import wavelink

from core import controller
from core.messages import split_message

log = logging.getLogger("panel")
VOLUME_STEP = 10
ID_COLA = "lavadj:cola"
VERDE, GRIS = discord.ButtonStyle.success, discord.ButtonStyle.secondary

def format_duration(milliseconds: int) -> str:
    if not milliseconds:
        return "?"
    seconds = milliseconds // 1000
    mins, secs = divmod(seconds, 60)
    return f"{mins}:{secs:02d}"

def build_embed(vc: wavelink.Player) -> discord.Embed:
    track = vc.current if vc else None
    title = track.title if track else "Nada sonando"
    paused = bool(vc and vc.paused)

    embed = discord.Embed(
        title=("⏸️ " if paused else "♫ ") + title[:240],
        colour=discord.Colour.dark_teal(),
    )
    if track:
        req = getattr(track, "requester", None) or "autoplay"
        embed.add_field(name="Pedido por", value=req, inline=True)
        embed.add_field(name="Duración", value=format_duration(track.length), inline=True)
    
    vol = getattr(vc, "volume", 100)
    embed.add_field(name="Volumen", value=f"{vol}%", inline=True)

    if vc and vc.queue:
        siguiente = vc.queue[0].title
        embed.add_field(name="Después", value=siguiente[:100], inline=False)
        embed.set_footer(text=f"{len(vc.queue)} en cola")
    elif vc and getattr(vc, "auto_queue", None):
        # Con la cola vacía y el autoplay encendido igual hay un "después": el
        # tema que eligió solo. Sin esto el panel decía "cola vacía" mientras
        # la radio seguía sonando.
        embed.add_field(name="Después (autoplay)", value=vc.auto_queue[0].title[:100], inline=False)
        embed.set_footer(text="Cola vacía · sigo con temas parecidos")
    else:
        embed.set_footer(text="Cola vacía")

    flags = []
    if vc and vc.queue.mode == wavelink.QueueMode.loop:
        flags.append("🔁 loop")
    if vc and getattr(vc, "autoplay", None) is wavelink.AutoPlayMode.enabled:
        flags.append("🔮 autoplay")
    if flags:
        embed.description = " · ".join(flags)
    return embed


class PlayerPanel(discord.ui.View):
    """Persistente: `custom_id` fijos y todo sale de `interaction.guild`, así que una instancia atiende a cualquier panel, también al de antes de un reinicio."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    def _vc(self, interaction: discord.Interaction) -> wavelink.Player | None:
        return interaction.guild.voice_client if interaction.guild else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (getattr(interaction, "data", None) or {}).get("custom_id") == ID_COLA:
            return True     # la cola es de solo lectura: la ve cualquiera
        vc = self._vc(interaction)
        author_voice = getattr(interaction.user, "voice", None)
        if vc and author_voice and author_voice.channel == vc.channel:
            return True
        await interaction.response.send_message(
            "Tienes que estar en el mismo canal de voz que el bot.", ephemeral=True
        )
        return False

    def pintar(self, vc: wavelink.Player | None, guild: discord.Guild) -> discord.Embed:
        """Ajusta los botones al estado de ahora (pausa, loop, autoplay, letra) y devuelve el embed que va con ellos."""
        self.toggle_pause.emoji = "▶️" if vc and vc.paused else "⏸️"
        self.loop.style = VERDE if vc and vc.queue.mode == wavelink.QueueMode.loop else GRIS
        self.autoplay.style = VERDE if self.cog.autoplay_activo(guild.id) else GRIS
        self.letra_en_vivo.style = VERDE if self.cog.estacion(guild.id).letra.activa else GRIS
        return build_embed(vc)

    def _huella(self, embed: discord.Embed) -> tuple:
        """Lo que se ve del panel: si una acción no lo cambia, editarlo no mostraría nada."""
        return embed.to_dict(), tuple((str(b.emoji), b.style) for b in self.children)

    async def _cambiar(self, interaction: discord.Interaction, accion):
        """Corre `accion(vc)`, que da el aviso (texto o corrutina). Si el panel se ve distinto lo edita; si no, el aviso va aparte y en privado."""
        vc, guild = self._vc(interaction), interaction.guild
        antes = self._huella(self.pintar(vc, guild))
        aviso = accion(vc)
        if inspect.isawaitable(aviso):
            aviso = await aviso
        embed = self.pintar(vc, guild)
        if self._huella(embed) == antes:
            return await interaction.response.send_message(aviso, ephemeral=True)
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            log.debug("no pude editar el panel", exc_info=True)

    @discord.ui.button(emoji="⏸️", style=GRIS, row=0, custom_id="lavadj:pausa")
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cambiar(interaction, controller.toggle_pause)

    @discord.ui.button(emoji="⏭️", style=GRIS, row=0, custom_id="lavadj:saltar")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = await controller.skip(self._vc(interaction))
        await interaction.response.send_message(note, ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0, custom_id="lavadj:parar")
    async def stop_playback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Parando 🛑", ephemeral=True)
        await self.cog.teardown(interaction.guild, "Parado 🛑")

    @discord.ui.button(emoji="🔁", style=GRIS, row=0, custom_id="lavadj:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cambiar(interaction, controller.toggle_loop)

    @discord.ui.button(emoji="🔀", style=GRIS, row=0, custom_id="lavadj:mezclar")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cambiar(interaction, controller.shuffle)     # solo se nota si cambia el "Después"

    @discord.ui.button(emoji="🔉", style=GRIS, row=1, custom_id="lavadj:vol-bajar")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cambiar(interaction, lambda vc: controller.nudge_volume(vc, -VOLUME_STEP))

    @discord.ui.button(emoji="🔊", style=GRIS, row=1, custom_id="lavadj:vol-subir")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cambiar(interaction, lambda vc: controller.nudge_volume(vc, VOLUME_STEP))

    @discord.ui.button(emoji="📋", style=GRIS, row=1, custom_id=ID_COLA)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc(interaction)
        if not vc or (not vc.current and not vc.queue):
            return await interaction.response.send_message("La cola está vacía.", ephemeral=True)
        
        lines = []
        if vc.current:
            lines.append(f"♫ **Ahora:** {vc.current.title}")
        for i, track in enumerate(vc.queue, 1):
            lines.append(f"`{i}.` {track.title} [{format_duration(track.length)}]")
            if i >= 15:
                lines.append(f"... y {len(vc.queue) - 15} más.")
                break
                
        chunks = split_message("\n".join(lines))
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @discord.ui.button(emoji="🎙️", style=discord.ButtonStyle.primary, row=2, custom_id="lavadj:dj")
    async def dj(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.dj.available():
            return await interaction.response.send_message(
                "El DJ está apagado o no hay voz disponible.", ephemeral=True
            )
        self.cog.dj.force(interaction.guild.id)
        await interaction.response.send_message(
            "🎙️ El DJ habla en el próximo corte.", ephemeral=True
        )

    # Con rótulo: el 🎤 se parece demasiado al 🎙️ del DJ.
    @discord.ui.button(emoji="🎤", label="Letra", style=GRIS, row=2, custom_id="lavadj:letra")
    async def letra_en_vivo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cambiar(interaction, lambda vc: self.cog.alternar_letra(interaction.guild))

    @discord.ui.button(emoji="🔮", label="Autoplay", style=GRIS, row=2, custom_id="lavadj:autoplay")
    async def autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        await self._cambiar(
            interaction, lambda vc: self.cog.poner_autoplay(guild_id, vc, not self.cog.autoplay_activo(guild_id))
        )

    @discord.ui.button(emoji="📻", label="Radio", style=GRIS, row=2, custom_id="lavadj:radio")
    async def radio(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc, guild = self._vc(interaction), interaction.guild
        antes = self._huella(self.pintar(vc, guild))
        await interaction.response.defer(ephemeral=True, thinking=True)     # buscar el mix tarda más que los 3 s de Discord
        await interaction.followup.send(await self.cog.poner_radio(guild), ephemeral=True)
        embed = self.pintar(vc, guild)      # el aviso va en privado, pero si se encendió el autoplay o cambió el "Después" el panel se repinta
        if self._huella(embed) != antes:
            try:
                await interaction.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                log.debug("no pude editar el panel", exc_info=True)
