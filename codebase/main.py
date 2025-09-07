import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio
import aiohttp
import re

load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

RADIO_STATIONS = [
    ("record", "https://radiorecord.hostingradio.ru/rr_main96.aacp"),
    ("russian_mix", "https://radiorecord.hostingradio.ru/rus64.aacp"),
    ("hits-all-time", "https://radiorecord.hostingradio.ru/alltimers96.aacp"),
    ("russian_hits", "https://radiorecord.hostingradio.ru/russianhits96.aacp"),
    ("colbas_ceh", "https://radiorecord.hostingradio.ru/pump96.aacp"),
    ("festivals", "https://radiorecord.hostingradio.ru/livedjsets96.aacp"),
    ("deep", "https://radiorecord.hostingradio.ru/deep96.aacp"),
    ("chill-out", "https://radiorecord.hostingradio.ru/chil96.aacp"),
    ("shashliki", "https://radiorecord.hostingradio.ru/nashashlyki96.aacp"),
    ("megamix", "https://radiorecord.hostingradio.ru/mix96.aacp"),
    ("pirate_station", "https://radiorecord.hostingradio.ru/ps96.aacp"),
    ("rock", "https://radiorecord.hostingradio.ru/rock96.aacp"),
    ("liquid_funk", "https://radiorecord.hostingradio.ru/liquidfunk96.aacp")

    # ... добавьте другие станции!
]
STATION_NAMES = [name for name, url in RADIO_STATIONS]
STATION_URLS = dict(RADIO_STATIONS)

player_state = {}  # guild_id: {"station_idx": int, "paused": bool}
guild_locks = {}   # guild_id: asyncio.Lock
control_messages = {}  # guild_id: {"channel_id": int, "message_id": int, "last_content": str}
http_session = None  # type: ignore[assignment]
track_updater_task = None  # type: ignore[assignment]
control_refresh_task = None  # type: ignore[assignment]

async def ensure_http_session():
    global http_session
    if http_session is None or http_session.closed:  # type: ignore[attr-defined]
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
    return http_session

def _parse_icy_metadata_block(block: bytes) -> str | None:
    """Extract and decode StreamTitle from ICY metadata block bytes.

    Strategy:
    - Work on bytes and extract raw title bytes via regex
    - Try UTF-8 first (most modern streams)
    - Then CP1251 (many RU streams)
    - Finally Latin-1 as a last resort
    """
    try:
        trimmed = block.rstrip(b"\x00")
        match = re.search(rb"StreamTitle='(.*?)';", trimmed, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        raw = match.group(1)
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                title = raw.decode(enc).strip()
                if title:
                    return title
            except Exception:
                continue
        # Heuristic recovery path
        try:
            fallback = raw.decode("latin-1", errors="ignore").encode("latin-1", errors="ignore").decode("utf-8", errors="ignore").strip()
            if fallback:
                return fallback
        except Exception:
            pass
    except Exception:
        pass
    return None

async def fetch_icy_title(stream_url: str) -> str | None:
    try:
        session = await ensure_http_session()
        headers = {"Icy-MetaData": "1", "User-Agent": "DiscordBot/1.0 (+ICY)"}
        async with session.get(stream_url, headers=headers) as resp:
            metaint_header = resp.headers.get("icy-metaint") or resp.headers.get("Icy-MetaInt")
            if not metaint_header:
                return None
            try:
                metaint = int(metaint_header)
            except Exception:
                return None
            # Drain up to the first metadata block
            remaining = metaint
            while remaining > 0:
                chunk = await resp.content.read(min(remaining, 4096))
                if not chunk:
                    return None
                remaining -= len(chunk)
            # Length of metadata comes in blocks of 16 bytes
            length_byte = await resp.content.read(1)
            if not length_byte:
                return None
            meta_len = length_byte[0] * 16
            if meta_len == 0:
                return None
            meta_block = await resp.content.read(meta_len)
            if not meta_block:
                return None
            return _parse_icy_metadata_block(meta_block)
    except Exception:
        return None

def _compose_presence_text(state: dict) -> str:
    station_name = STATION_NAMES[state["station_idx"]]
    track_title = state.get("track")
    paused = state.get("paused", False)
    if track_title:
        base = f"{station_name}: {track_title}"
    else:
        base = f"{station_name}"
    if paused:
        base = f"⏸️ {base}"
    else:
        base = f"🎶 {base}"
    # Discord shows up to ~128 chars; trim just in case
    if len(base) > 128:
        base = base[:125] + "..."
    return base

def compose_control_content(state: dict) -> str:
    station_name = STATION_NAMES[state["station_idx"]]
    paused = state.get("paused", False)
    header = f"⏸️ Воспроизведение на паузе: **{station_name}**" if paused else f"▶️ Воспроизведение продолжено: **{station_name}**"
    track_title = state.get("track")
    track_line = f"🎧 Трек: **{track_title}**" if track_title else "🎧 Трек: —"
    return f"{header}\n{track_line}"

async def update_presence_for_guild(guild_id: int):
    try:
        state = player_state.get(guild_id)
        if not state:
            await bot.change_presence(activity=None)
            return
        activity = discord.Activity(type=discord.ActivityType.listening, name=_compose_presence_text(state))
        await bot.change_presence(activity=activity, status=discord.Status.online)
    except Exception:
        pass

async def track_updater_loop():
    # Periodically fetch ICY metadata for playing stations and update presence
    while True:
        try:
            # Snapshot to avoid runtime dict size change issues
            items = list(player_state.items())
            for guild_id, state in items:
                if state.get("paused", False):
                    continue
                try:
                    name, url = RADIO_STATIONS[state["station_idx"]]
                except Exception:
                    continue
                title = await fetch_icy_title(url)
                if title and state.get("track") != title:
                    # Update current track and push to history
                    state["track"] = title
                    history = state.get("history") or []
                    if not history or history[-1] != title:
                        history.append(title)
                        if len(history) > 20:
                            history = history[-20:]
                        state["history"] = history
                    await update_presence_for_guild(guild_id)
                await asyncio.sleep(0.2)
        except Exception:
            # Never break the loop on error
            pass
        await asyncio.sleep(1)

async def control_refresh_loop():
    # Periodically refresh the control message to show the latest track
    while True:
        try:
            refs = list(control_messages.items())
            for guild_id, ref in refs:
                state = player_state.get(guild_id)
                if not state:
                    continue
                try:
                    channel = bot.get_channel(ref["channel_id"])  # type: ignore[arg-type]
                    if channel is None:
                        channel = await bot.fetch_channel(ref["channel_id"])  # type: ignore[assignment]
                    content = compose_control_content(state)
                    if ref.get("last_content") == content:
                        continue
                    message = await channel.fetch_message(ref["message_id"])  # type: ignore[attr-defined]
                    await message.edit(content=content)
                    ref["last_content"] = content
                except Exception:
                    try:
                        await delete_control_message(guild_id)
                    except Exception:
                        pass
                await asyncio.sleep(0.2)
        except Exception:
            pass
        await asyncio.sleep(5)

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

        player_state[guild_id] = {"station_idx": station_idx, "paused": False, "track": None, "history": []}

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
        content = compose_control_content(player_state[guild_id])
        msg = await interaction.followup.send(content, view=view, ephemeral=False)
        try:
            control_messages[guild_id] = {"channel_id": msg.channel.id, "message_id": msg.id, "last_content": content}
        except Exception:
            pass
        try:
            await update_presence_for_guild(guild_id)
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
        player_state[guild_id]["track"] = None
        player_state[guild_id]["history"] = []
        ref = control_messages.get(guild_id)
        target_id = ref["message_id"] if ref else interaction.message.id
        new_content = compose_control_content(player_state[guild_id])
        await interaction.followup.edit_message(message_id=target_id, content=new_content, view=RadioControlView())
        if ref is not None:
            ref["last_content"] = new_content
        try:
            await update_presence_for_guild(guild_id)
        except Exception:
            pass

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
                await interaction.followup.edit_message(message_id=target_id, content=compose_control_content(state), view=RadioControlView())
                if ref is not None:
                    ref["last_content"] = compose_control_content(state)
                try:
                    await update_presence_for_guild(guild_id)
                except Exception:
                    pass
            else:
                await interaction.followup.send("Поток уже на паузе.", ephemeral=True)
        else:
            if voice_client.is_paused():
                voice_client.resume()
                state["paused"] = False
                ref = control_messages.get(guild_id)
                target_id = ref["message_id"] if ref else interaction.message.id
                new_content = compose_control_content(state)
                await interaction.followup.edit_message(message_id=target_id, content=new_content, view=RadioControlView())
                if ref is not None:
                    ref["last_content"] = new_content
                try:
                    await update_presence_for_guild(guild_id)
                except Exception:
                    pass
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
        try:
            await bot.change_presence(activity=None)
        except Exception:
            pass

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"Sync error: {e}")
    # Start background tasks
    try:
        await ensure_http_session()
    except Exception:
        pass
    global track_updater_task
    if track_updater_task is None or track_updater_task.done():  # type: ignore[union-attr]
        try:
            track_updater_task = asyncio.create_task(track_updater_loop())
        except Exception:
            pass
    global control_refresh_task
    if control_refresh_task is None or control_refresh_task.done():  # type: ignore[union-attr]
        try:
            control_refresh_task = asyncio.create_task(control_refresh_loop())
        except Exception:
            pass

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

@bot.tree.command(name="track", description="Показать текущий трек станции")
async def current_track(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = interaction.guild.id
    state = player_state.get(guild_id)
    if not state:
        await interaction.followup.send("Сейчас ничего не играет.", ephemeral=True)
        return
    name = STATION_NAMES[state["station_idx"]]
    title = state.get("track")
    if not title and not state.get("paused", False):
        try:
            _, url = RADIO_STATIONS[state["station_idx"]]
            title = await fetch_icy_title(url)
            if title:
                state["track"] = title
                try:
                    await update_presence_for_guild(guild_id)
                except Exception:
                    pass
        except Exception:
            title = None
    if title:
        await interaction.followup.send(f"🎧 Трек: **{title}** (станция: `{name}`)", ephemeral=False)
    else:
        await interaction.followup.send(f"Текущий трек недоступен. Станция: `{name}`", ephemeral=True)

@bot.tree.command(name="history", description="Показать историю треков текущей станции")
async def track_history(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = interaction.guild.id
    state = player_state.get(guild_id)
    if not state:
        await interaction.followup.send("Сейчас ничего не играет.", ephemeral=True)
        return
    station_name = STATION_NAMES[state["station_idx"]]
    history = state.get("history") or []
    if not history:
        await interaction.followup.send(f"История пуста для станции `{station_name}`.", ephemeral=True)
        return
    last_items = history[-10:]
    lines = [f"{idx+1}. {title}" for idx, title in enumerate(last_items)]
    msg = f"**История треков для `{station_name}` (последние {len(last_items)}):**\n" + "\n".join(lines)
    await interaction.followup.send(msg, ephemeral=False)

bot.run(DISCORD_TOKEN)