import discord
from discord import app_commands
from discord.ext import commands
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

print(DISCORD_TOKEN)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Ссылка на основной поток Radio Record
RADIO_URL = "http://air.radiorecord.ru:805/rr_320"

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s).")
    except Exception as e:
        print(e)

@bot.tree.command(name="play", description="Включить Radio Record в голосовом канале")
async def play_radio(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("Сначала зайди в голосовой канал!", ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if voice_client and voice_client.is_connected():
        await voice_client.move_to(voice_channel)
    else:
        voice_client = await voice_channel.connect()

    # Остановка текущего проигрывания, если есть
    if voice_client.is_playing():
        voice_client.stop()

    # Проигрываем поток через FFmpeg
    source = await discord.FFmpegOpusAudio.from_probe(RADIO_URL)
    voice_client.play(source)
    await interaction.response.send_message("🎶 Радио Record играет!", ephemeral=False)

# Запусти бота с токеном
bot.run(DISCORD_TOKEN)