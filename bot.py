import os
import json
import re
import logging
from datetime import datetime, timedelta

import requests
import discord
from discord.ext import commands
from discord import app_commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
from dotenv import load_dotenv

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
logging.getLogger("discord").setLevel(logging.WARNING)

# ---------------------------
# Env / Config
# ---------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
TORN_API_KEY = os.getenv("TORN_API_KEY", "").strip()
DIRECTOR_ID = int(os.getenv("DISCORD_USER_ID", "0"))         
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))           
TIMEZONE = os.getenv("TIMEZONE", "Europe/London")
WELCOME_CHANNEL_NAME = os.getenv("WELCOME_CHANNEL", "general")
DATA_FILE = os.getenv("DATA_FILE", "data.json")

# Scheduler time: 19:30 UK
SYNC_HOUR = 19
SYNC_MINUTE = 30
tz = pytz.timezone(TIMEZONE)

GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None
_COMMANDS_SYNCED = False

def guild_only():
    if GUILD_OBJ:
        return app_commands.guilds(GUILD_OBJ)
    def identity(fn):
        return fn
    return identity

# ---------------------------
# Discord Intents / Bot
# ---------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True           
intents.message_content = True   
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------
# Storage helpers
# ---------------------------
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"employees": [], "trained": {}, "rotation_cycle": 0, "company_snapshot": {}, "last_sync": None}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.exception("Failed to load data.json")
        return {"employees": [], "trained": {}, "rotation_cycle": 0, "company_snapshot": {}, "last_sync": None}

def save_data(d: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        logging.exception("Failed to save data.json")

# ---------------------------
# Rotation helpers & Checks
# ---------------------------
def norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip().casefold()

def all_trained(data: dict) -> bool:
    emps = data.get("employees", [])
    trained = data.get("trained", {})
    return bool(emps) and all(trained.get(e, "N") == "Y" for e in emps)

def reset_rotation(data: dict):
    trained = data.setdefault("trained", {})
    for e in data.get("employees", []):
        trained[e] = "N"
    data["rotation_cycle"] = data.get("rotation_cycle", 0) + 1
    save_data(data)
    logging.info(f"Rotation reset (cycle #{data['rotation_cycle']}).")

def director_check(interaction: discord.Interaction) -> bool:
    if interaction.user.id == DIRECTOR_ID:
        return True
    roles = [r.name.lower() for r in getattr(interaction.user, "roles", [])]
    return "director" in roles

def company_role_check(interaction: discord.Interaction) -> bool:
    if interaction.user.id == DIRECTOR_ID:
        return True
    roles = [r.name.lower() for r in getattr(interaction.user, "roles", [])]
    return ("employee" in roles) or ("director" in roles)

async def verify_employee(member: discord.Member) -> str:
    guild = member.guild
    employee_role = discord.utils.find(lambda r: r.name.lower() == "employee", guild.roles)
    if not employee_role:
        return "⚠️ I can't find an **Employee** role in this server."

    data = load_data()
    employees = data.get("employees", [])
    if not employees:
        return "⚠️ I don't have any company employees loaded yet."

    nickname = member.nick or member.name
    base_name = re.split(r"\[|\(", nickname)[0].strip()
    
    match = None
    for e in employees:
        if norm(e) == norm(base_name):
            match = e
            break

    if not match:
        return f"❌ I couldn't find a company employee matching `{base_name}`."

    if employee_role in member.roles:
        return f"✅ You're already verified as **{match}** and have the Employee role."

    try:
        await member.add_roles(employee_role, reason="Verified via /verify command")
        return f"✅ Verified as **{match}** and given the **Employee** role."
    except discord.Forbidden:
        return "⚠️ I don't have permission to assign roles."
    except Exception:
        logging.exception("Error assigning Employee role")
        return "⚠️ Something went wrong assigning your role."

# ---------------------------
# Torn API
# ---------------------------
def get_company_data() -> dict | None:
    if not TORN_API_KEY:
        logging.error("Missing TORN_API_KEY")
        return None
    url = f"https://api.torn.com/company/?selections=detailed,employees&key={TORN_API_KEY}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "company_detailed" in data and "company_employees" in data:
            return data
        logging.error("Unexpected Torn API structure")
        return None
    except Exception:
        logging.exception("Error fetching Torn API")
        return None

def sync_torn_data() -> bool:
    base = load_data()
    company = get_company_data()
    if not company or "company_employees" not in company:
        return False

    api_emps = [emp["name"] for _, emp in sorted(
        company["company_employees"].items(),
        key=lambda kv: (-int(kv[1].get("days_in_company", 0)), kv[1].get("name", "").lower())
    )]

    trained = base.setdefault("trained", {})
    for k in list(trained.keys()):
        if k not in api_emps:
            trained.pop(k, None)
    for e in api_emps:
        trained.setdefault(e, "N")

    base["employees"] = api_emps
    base["company_snapshot"] = company
    base["last_sync"] = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    save_data(base)

    if all_trained(base):
        reset_rotation(base)

    trains = company["company_detailed"].get("trains_available", 0)
    logging.info(f"[sync] Employees: {len(api_emps)}, trains={trains}")
    return True

# ---------------------------
# Scheduler
# ---------------------------
scheduler = AsyncIOScheduler(timezone=tz)

async def dm_director(message: str):
    try:
        user = await bot.fetch_user(DIRECTOR_ID)
        if user:
            await user.send(message)
    except Exception:
        logging.exception("Failed to DM director")

def scheduled_sync():
    ok = sync_torn_data()
    if not ok: return
    data = load_data()
    trains = int(data.get("company_snapshot", {}).get("company_detailed", {}).get("trains_available", 0) or 0)
    if trains >= 10:
        bot.loop.create_task(dm_director(f"🔔 Trains available: **{trains}**."))

# ---------------------------
# Events
# ---------------------------
@bot.event
async def on_ready():
    global _COMMANDS_SYNCED
    
    # ------------------------

    try:
        if not _COMMANDS_SYNCED:
            if GUILD_OBJ:
                await bot.tree.sync(guild=GUILD_OBJ)
                logging.info(f"🔁 Synced slash commands to guild {GUILD_ID}.")
            else:
                await bot.tree.sync()
                logging.info("🔁 Synced slash commands globally.")
            _COMMANDS_SYNCED = True
    except Exception:
        logging.exception("Failed to sync commands")

    logging.info(f"✅ Logged in as {bot.user} ({bot.user.id})")

    try:
        if not scheduler.running:
            scheduler.add_job(scheduled_sync, "cron", hour=SYNC_HOUR, minute=SYNC_MINUTE)
            scheduler.start()
            logging.info("📅 Scheduler started.")
    except Exception:
        logging.exception("Failed to start scheduler")

@bot.event
async def on_member_join(member: discord.Member):
    channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL_NAME)
    if channel:
        try:
            await channel.send(f"👋 Welcome to **{member.guild.name}**, {member.mention}! Use `/verify`.")
        except Exception:
            logging.exception("Failed to send welcome message")

# ---------------------------
# Slash Commands (Existing)
# ---------------------------
@guild_only()
@bot.tree.command(name="forceupdate", description="Director only: force sync")
@app_commands.check(director_check)
async def forceupdate(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if sync_torn_data():
        await interaction.followup.send("✅ Data synced.")
    else:
        await interaction.followup.send("❌ Sync failed.", ephemeral=True)

@forceupdate.error
async def forceupdate_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 Directors only.", ephemeral=True)

@guild_only()
@bot.tree.command(name="status", description="Show company status")
@app_commands.check(company_role_check)
async def status(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        data = load_data()
        emps = data.get("employees", [])
        trained = data.get("trained", {})
        trained_count = sum(1 for v in trained.values() if v == "Y")
        total = len(emps)
        trains = data.get("company_snapshot", {}).get("company_detailed", {}).get("trains_available", "N/A")
        
        embed = discord.Embed(
            title="📊 Company Status",
            color=discord.Color.blurple(),
            timestamp=datetime.now()
        )
        embed.add_field(name="Trains", value=str(trains))
        embed.add_field(name="Rotation", value=f"{trained_count}/{total} trained")
        await interaction.followup.send(embed=embed)
    except Exception:
        await interaction.followup.send("⚠️ Failed to retrieve status.", ephemeral=True)

@status.error
async def status_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 No permission.", ephemeral=True)

@guild_only()
@bot.tree.command(name="verify", description="Verify Torn account")
async def verify(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    msg = await verify_employee(interaction.user)
    await interaction.followup.send(msg, ephemeral=True)

@guild_only()
@bot.tree.command(name="train", description="Mark employee trained")
@app_commands.check(director_check)
async def train_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    data = load_data()
    employees = data.get("employees", [])
    trained = data.setdefault("trained", {})
    
    target = next((e for e in employees if norm(e) == norm(name)), None)
    if not target:
        await interaction.followup.send(f"❌ Employee '{name}' not found.", ephemeral=True)
        return

    trained[target] = "Y"
    save_data(data)
    if all_trained(data):
        reset_rotation(data)
        await interaction.followup.send(f"✅ Marked **{target}**. All trained! Rotation reset.")
    else:
        await interaction.followup.send(f"✅ Marked **{target}** as trained.")

@train_cmd.error
async def train_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("🚫 Directors only.", ephemeral=True)

# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN")
    bot.run(DISCORD_TOKEN)

