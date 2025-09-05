import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio

load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

RADIO_STATIONS = [
    ("record", "https://radiorecord.hostingradio.ru/rr_main96.aacp"),
    ("russian_mix", "https://radiorecord.hostingradio.ru/rus64.aacp"),
    # ... добавьте другие станции!
]
STATION_NAMES = [name for name, url in RADIO_STATIONS]
STATION_URLS = dict(RADIO_STATIONS)

player_state = {}  # guild_id: {"station_idx": int, "paused": bool}

class RadioControlView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="⏮️ Предыдущая", style=discord.ButtonStyle.primary, custom_id="prev_station")
    async def prev_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_switch_station(interaction, -1)

    @discord.ui.button(label="⏸️ Пауза", style=discord.ButtonStyle.secondary, custom_id="pause_station")
    async def pause_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_pause_resume(interaction, pause=True)

    @discord.ui.button(label="▶️ Продолжить", style=discord.ButtonStyle.secondary, custom_id="resume_station")
    async def resume_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_pause_resume(interaction, pause=False)

    @discord.ui.button(label="⏹️ Стоп", style=discord.ButtonStyle.danger, custom_id="stop_station")
    async def stop_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_stop(interaction)

    @discord.ui.button(label="⏭️ Следующая", style=discord.ButtonStyle.primary, custom_id="next_station")
    async def next_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_switch_station(interaction, 1)

# Вспомогательная функция для подключения к voice с обработкой ошибок и followup
async def ensure_voice(interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        # После defer только followup!
        await interaction.followup.send("Сначала зайди в голосовой канал!", ephemeral=True)
        return None
    voice_channel = interaction.user.voice.channel
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    try:
        if voice_client:
            if voice_client.channel != voice_channel:
                await voice_client.move_to(voice_channel)
        else:
            voice_client = await voice_channel.connect(timeout=20)
        return voice_client
    except Exception as e:
        await interaction.followup.send(f"Ошибка подключения: {type(e).__name__}: {e}", ephemeral=True)
        return None

async def start_radio(interaction, station_idx):
    guild_id = interaction.guild.id
    name, radio_url = RADIO_STATIONS[station_idx]
    voice_client = await ensure_voice(interaction)
    if not voice_client:
        return

    player_state[guild_id] = {"station_idx": station_idx, "paused": False}

    if voice_client.is_playing():
        voice_client.stop()
    ffmpeg_options = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn"
    }
    try:
        source = discord.FFmpegPCMAudio(radio_url, **ffmpeg_options)
        voice_client.play(source)
    except Exception as e:
        await interaction.followup.send(f"Не удалось запустить поток: {type(e).__name__}: {e}", ephemeral=True)
        return
    view = RadioControlView(guild_id)
    await interaction.followup.send(
        f"🎶 Сейчас играет Radio Record: **{name}**",
        view=view,
        ephemeral=False
    )

async def switch_radio(interaction, direction):
    guild_id = interaction.guild.id
    state = player_state.get(guild_id)
    if state is None:
        await interaction.response.edit_message(content="Ничего не играет.", view=None)
        return
    idx = (state["station_idx"] + direction) % len(RADIO_STATIONS)
    name, radio_url = RADIO_STATIONS[idx]
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if not voice_client:
        await interaction.response.edit_message(content="Бот не подключен к голосовому каналу.", view=None)
        return
    if voice_client.is_playing():
        voice_client.stop()
    ffmpeg_options = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn"
    }
    try:
        source = discord.FFmpegPCMAudio(radio_url, **ffmpeg_options)
        voice_client.play(source)
    except Exception as e:
        await interaction.response.edit_message(content=f"Не удалось запустить поток: {type(e).__name__}: {e}", view=None)
        return
    player_state[guild_id]["station_idx"] = idx
    await interaction.response.edit_message(
        content=f"🎶 Сейчас играет Radio Record: **{name}**",
        view=RadioControlView(guild_id)
    )

async def handle_switch_station(interaction, direction):
    await switch_radio(interaction, direction)

async def handle_pause_resume(interaction, pause=True):
    guild_id = interaction.guild.id
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    state = player_state.get(guild_id)
    if not voice_client or not state:
        await interaction.response.edit_message(content="Сейчас ничего не играет.", view=None)
        return
    if pause:
        if voice_client.is_playing():
            voice_client.pause()
            state["paused"] = True
            await interaction.response.edit_message(
                content=f"⏸️ Воспроизведение на паузе: **{RADIO_STATIONS[state['station_idx']][0]}**",
                view=RadioControlView(guild_id)
            )
        else:
            await interaction.response.send_message("Поток уже на паузе.", ephemeral=True)
    else:
        if voice_client.is_paused():
            voice_client.resume()
            state["paused"] = False
            await interaction.response.edit_message(
                content=f"▶️ Воспроизведение продолжено: **{RADIO_STATIONS[state['station_idx']][0]}**",
                view=RadioControlView(guild_id)
            )
        else:
            await interaction.response.send_message("Поток уже играет.", ephemeral=True)

async def handle_stop(interaction):
    guild_id = interaction.guild.id
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if voice_client:
        voice_client.stop()
        await voice_client.disconnect(force=True)
        await interaction.response.edit_message(
            content="⏹️ Воспроизведение остановлено и бот покинул канал.",
            view=None
        )
    else:
        await interaction.response.edit_message(content="Сейчас ничего не играет.", view=None)
    player_state.pop(guild_id, None)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"Sync error: {e}")

@bot.tree.command(name="play", description="Включить станцию Radio Record в голосовом канале")
@app_commands.describe(station="Название станции (например: record, russian_mix, ...)")
async def play_radio(interaction: discord.Interaction, station: str = "record"):
    await interaction.response.defer(thinking=True)
    if station.lower() not in STATION_URLS:
        available = ", ".join(STATION_URLS.keys())
        await interaction.followup.send(
            f"❌ Неизвестная станция: {station}\nДоступные: {available}",
            ephemeral=True)
        return
    idx = STATION_NAMES.index(station.lower())
    await start_radio(interaction, idx)

@play_radio.autocomplete('station')
async def station_autocomplete(interaction: discord.Interaction, current: str):
    try:
        stations = [
            app_commands.Choice(name=name, value=name)
            for name in STATION_NAMES
            if current.lower() in name.lower()
        ]
        return stations[:25]
    except Exception as e:
        print("Autocomplete error:", e)
        return []

@bot.tree.command(name="stations", description="Показать все доступные станции Radio Record")
async def list_stations(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    station_list = "\n".join(f"- `{name}`" for name in STATION_NAMES)
    await interaction.followup.send(
        f"**Доступные станции:**\n{station_list}",
        ephemeral=True
    )

@bot.tree.command(name="nowplaying", description="Показать, какая станция сейчас играет")
async def now_playing(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    guild_id = interaction.guild.id
    state = player_state.get(guild_id)
    if state is not None:
        name = STATION_NAMES[state["station_idx"]]
        paused = state.get("paused", False)
        text = f"🔊 Сейчас играет: **{name}**"
        if paused:
            text += " (на паузе)"
        await interaction.followup.send(text, ephemeral=False)
    else:
        await interaction.followup.send("Сейчас ничего не играет.", ephemeral=True)

bot.run(DISCORD_TOKEN)