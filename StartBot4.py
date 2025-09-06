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
    ("pirate_station", "https://radiorecord.hostingradio.ru/ps96.aacp"),
    ("liquid_funk", "https://radiorecord.hostingradio.ru/liquidfunk96.aacp"),
    ("colbas_ceh", "https://radiorecord.hostingradio.ru/pump96.aacp")
    # ... добавьте другие станции!
]
STATION_NAMES = [name for name, url in RADIO_STATIONS]
STATION_URLS = dict(RADIO_STATIONS)

player_state = {}  # guild_id: {"station_idx": int, "paused": bool}
guild_locks = {}   # guild_id: asyncio.Lock
control_messages = {}  # guild_id: {"channel_id": int, "message_id": int}

def get_guild_lock(guild_id: int) -> asyncio.Lock:
    lock = guild_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        guild_locks[guild_id] = lock
    return lock

async def delete_control_message(guild_id: int):
    ref = control_messages.get(guild_id)
    if not ref:
        return
    channel = bot.get_channel(ref["channel_id"])  # type: ignore[arg-type]
    try:
        if channel is None:
            channel = await bot.fetch_channel(ref["channel_id"])  # type: ignore[assignment]
        message = await channel.fetch_message(ref["message_id"])  # type: ignore[attr-defined]
        await message.delete()
    except Exception:
        pass
    finally:
        control_messages.pop(guild_id, None)

async def ensure_is_current_control(interaction: discord.Interaction) -> bool:
    guild_id = interaction.guild.id
    ref = control_messages.get(guild_id)
    if not ref or interaction.message.id != ref["message_id"]:
        try:
            await interaction.response.send_message(
                "Это устаревшее сообщение управления. Используйте последнее сообщение от бота.",
                ephemeral=True
            )
        except Exception:
            pass
        try:
            if interaction.message and interaction.message.author == bot.user:
                await interaction.message.delete()
        except Exception:
            pass
        return False
    return True

class RadioControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⏮️ Предыдущая", style=discord.ButtonStyle.primary, custom_id="prev_station")
    async def prev_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ensure_is_current_control(interaction):
            return
        await interaction.response.defer()
        await handle_switch_station(interaction, -1)

    @discord.ui.button(label="⏸️ Пауза", style=discord.ButtonStyle.secondary, custom_id="pause_station")
    async def pause_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ensure_is_current_control(interaction):
            return
        await interaction.response.defer()
        await handle_pause_resume(interaction, pause=True)

    @discord.ui.button(label="▶️ Продолжить", style=discord.ButtonStyle.secondary, custom_id="resume_station")
    async def resume_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ensure_is_current_control(interaction):
            return
        await interaction.response.defer()
        await handle_pause_resume(interaction, pause=False)

    @discord.ui.button(label="⏹️ Стоп", style=discord.ButtonStyle.danger, custom_id="stop_station")
    async def stop_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ensure_is_current_control(interaction):
            return
        await interaction.response.defer()
        await handle_stop(interaction)

    @discord.ui.button(label="⏭️ Следующая", style=discord.ButtonStyle.primary, custom_id="next_station")
    async def next_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ensure_is_current_control(interaction):
            return
        await interaction.response.defer()
        await handle_switch_station(interaction, 1)

# Вспомогательная функция для подключения к voice с повторными попытками
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
            return voice_client
        # Повторяем несколько попыток подключения
        last_exc = None
        for _ in range(3):
            try:
                voice_client = await asyncio.wait_for(voice_channel.connect(timeout=15), timeout=20)
                return voice_client
            except Exception as exc:  # noqa: PERF203
                last_exc = exc
                await asyncio.sleep(1.5)
        if last_exc:
            raise last_exc
    except Exception as e:
        await interaction.followup.send(f"Ошибка подключения: {type(e).__name__}: {e}", ephemeral=True)
        return None

async def start_radio(interaction, station_idx):
    guild_id = interaction.guild.id
    name, radio_url = RADIO_STATIONS[station_idx]
    async with get_guild_lock(guild_id):
        voice_client = await ensure_voice(interaction)
        if not voice_client:
            return

        player_state[guild_id] = {"station_idx": station_idx, "paused": False}

        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -fflags +nobuffer -flags low_delay -probesize 32k -analyzeduration 0",
            "options": "-vn -bufsize 256k"
        }
        try:
            source = discord.FFmpegPCMAudio(radio_url, **ffmpeg_options)

            def after_playback(error):
                if error:
                    bot.loop.call_soon_threadsafe(asyncio.create_task, interaction.followup.send(f"Поток прерван: {error}", ephemeral=True))

            voice_client.play(source, after=after_playback)
        except Exception as e:
            await interaction.followup.send(f"Не удалось запустить поток: {type(e).__name__}: {e}", ephemeral=True)
            return
        # Удаляем предыдущее сообщение управления, если было
        await delete_control_message(guild_id)
        view = RadioControlView()
        msg = await interaction.followup.send(
            f"🎶 Сейчас играет Radio Record: **{name}**",
            view=view,
            ephemeral=False
        )
        try:
            control_messages[guild_id] = {"channel_id": msg.channel.id, "message_id": msg.id}
        except Exception:
            pass

async def switch_radio(interaction, direction):
    guild_id = interaction.guild.id
    state = player_state.get(guild_id)
    if state is None:
        await interaction.followup.send("Ничего не играет.", ephemeral=True)
        return
    idx = (state["station_idx"] + direction) % len(RADIO_STATIONS)
    name, radio_url = RADIO_STATIONS[idx]
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if not voice_client:
        await interaction.followup.send("Бот не подключен к голосовому каналу.", ephemeral=True)
        return
    async with get_guild_lock(guild_id):
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -fflags +nobuffer -flags low_delay -probesize 32k -analyzeduration 0",
            "options": "-vn -bufsize 256k"
        }
        try:
            source = discord.FFmpegPCMAudio(radio_url, **ffmpeg_options)
            voice_client.play(source)
        except Exception as e:
            ref = control_messages.get(guild_id)
            target_id = ref["message_id"] if ref else interaction.message.id
            await interaction.followup.edit_message(message_id=target_id, content=f"Не удалось запустить поток: {type(e).__name__}: {e}", view=None)
            return
        player_state[guild_id]["station_idx"] = idx
        ref = control_messages.get(guild_id)
        target_id = ref["message_id"] if ref else interaction.message.id
        await interaction.followup.edit_message(
            message_id=target_id,
            content=f"🎶 Сейчас играет Radio Record: **{name}**",
            view=RadioControlView()
        )

async def handle_switch_station(interaction, direction):
    await switch_radio(interaction, direction)

async def handle_pause_resume(interaction, pause=True):
    guild_id = interaction.guild.id
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    state = player_state.get(guild_id)
    if not voice_client or not state:
        await interaction.followup.send("Сейчас ничего не играет.", ephemeral=True)
        return
    async with get_guild_lock(guild_id):
        if pause:
            if voice_client.is_playing():
                voice_client.pause()
                state["paused"] = True
                ref = control_messages.get(guild_id)
                target_id = ref["message_id"] if ref else interaction.message.id
                await interaction.followup.edit_message(
                    message_id=target_id,
                    content=f"⏸️ Воспроизведение на паузе: **{RADIO_STATIONS[state['station_idx']][0]}**",
                    view=RadioControlView()
                )
            else:
                await interaction.followup.send("Поток уже на паузе.", ephemeral=True)
        else:
            if voice_client.is_paused():
                voice_client.resume()
                state["paused"] = False
                ref = control_messages.get(guild_id)
                target_id = ref["message_id"] if ref else interaction.message.id
                await interaction.followup.edit_message(
                    message_id=target_id,
                    content=f"▶️ Воспроизведение продолжено: **{RADIO_STATIONS[state['station_idx']][0]}**",
                    view=RadioControlView()
                )
            else:
                await interaction.followup.send("Поток уже играет.", ephemeral=True)

async def handle_stop(interaction):
    guild_id = interaction.guild.id
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    async with get_guild_lock(guild_id):
        if voice_client:
            voice_client.stop()
            await voice_client.disconnect(force=True)
            # Удаляем сообщение управления и не оставляем следов в канале
            await delete_control_message(guild_id)
            await interaction.followup.send("⏹️ Воспроизведение остановлено, бот покинул канал.", ephemeral=True)
        else:
            await interaction.followup.send("Сейчас ничего не играет.", ephemeral=True)
        player_state.pop(guild_id, None)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"Sync error: {e}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Если сам бот покинул голосовой канал, удаляем сообщение управления
    try:
        if member.bot and bot.user and member.id == bot.user.id:
            if before.channel and not after.channel and member.guild:
                await delete_control_message(member.guild.id)
    except Exception:
        pass

@bot.tree.command(name="play", description="Включить станцию Radio Record в голосовом канале")
@app_commands.describe(station="Название станции (например: record, russian_mix, ...)")
async def play_radio(interaction: discord.Interaction, station: str = "record"):
    await interaction.response.defer()
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
    await interaction.response.defer()
    station_list = "\n".join(f"- `{name}`" for name in STATION_NAMES)
    await interaction.followup.send(
        f"**Доступные станции:**\n{station_list}",
        ephemeral=True
    )

@bot.tree.command(name="nowplaying", description="Показать, какая станция сейчас играет")
async def now_playing(interaction: discord.Interaction):
    await interaction.response.defer()
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