"""Panel de botones que acompaña a la canción que suena."""

import logging
import discord
import wavelink

from core import controller
from core.messages import split_message

log = logging.getLogger("panel")
VOLUME_STEP = 10

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
    def __init__(self, cog, guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    def _vc(self, interaction: discord.Interaction) -> wavelink.Player | None:
        return interaction.guild.voice_client if interaction.guild else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vc = self._vc(interaction)
        author_voice = getattr(interaction.user, "voice", None)
        if vc and author_voice and author_voice.channel == vc.channel:
            return True
        await interaction.response.send_message(
            "Tienes que estar en el mismo canal de voz que el bot.", ephemeral=True
        )
        return False

    async def _apply(self, interaction: discord.Interaction, note: str):
        try:
            await interaction.response.edit_message(
                embed=build_embed(self._vc(interaction)), view=self
            )
        except discord.HTTPException:
            log.debug("no pude editar el panel", exc_info=True)
        try:
            await interaction.followup.send(note, ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = await controller.toggle_pause(self._vc(interaction))
        await self._apply(interaction, note)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = await controller.skip(self._vc(interaction))
        await interaction.response.send_message(note, ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_playback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Parando 🛑", ephemeral=True)
        await self.cog.teardown(interaction.guild, "Parado 🛑")

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = await controller.toggle_loop(self._vc(interaction))
        await self._apply(interaction, note)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = controller.shuffle(self._vc(interaction))
        await self._apply(interaction, note)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = await controller.nudge_volume(self._vc(interaction), -VOLUME_STEP)
        await self._apply(interaction, note)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = await controller.nudge_volume(self._vc(interaction), VOLUME_STEP)
        await self._apply(interaction, note)

    @discord.ui.button(emoji="📋", style=discord.ButtonStyle.secondary, row=1)
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

    @discord.ui.button(emoji="🎙️", style=discord.ButtonStyle.primary, row=1)
    async def dj(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.dj.available():
            return await interaction.response.send_message(
                "El DJ está apagado o no hay voz disponible.", ephemeral=True
            )
        self.cog.dj.force(self.guild_id)
        await interaction.response.send_message(
            "🎙️ El DJ habla en el próximo corte.", ephemeral=True
        )
