import discord
from discord.ext import commands, tasks
import requests
import json
import os
from datetime import datetime
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TORN_KEY = os.getenv("TORN_API_KEY")
USER_ID = int(os.getenv("DISCORD_USER_ID"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/London")

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = "rotation.json"
tz = pytz.timezone(TIMEZONE)

# --- Role Check ---

def has_company_role(interaction: discord.Interaction) -> bool:
    """Check if the user has Employee or Director role."""
    allowed_roles = {"employee", "director"}
    user_roles = {r.name.lower() for r in interaction.user.roles}
    return bool(allowed_roles.intersection(user_roles))



# --- Helper Functions ---
def get_company_data():
    url = f"https://api.torn.com/company/?selections=detailed,employees&key={TORN_KEY}"
    try:
        response = requests.get(url)
        data = response.json()
        return data
    except Exception as e:
        print(f"Error fetching Torn API: {e}")
        return None

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f:
            json.dump({"employees": [], "trained": {}}, f)
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

# --- Torn Sync Job ---
# --- Torn Sync Job ---
def sync_torn_data():
    data = load_data()
    company = get_company_data()
    if not company or "company_employees" not in company:
        print("Error: invalid Torn data.")
        return

    employees = []

    # Sort by days_in_company DESC (oldest first), tie-break by name for stability
    emp_items = sorted(
        company["company_employees"].items(),
        key=lambda x: (-int(x[1].get("days_in_company", 0)), x[1].get("name","").lower())
)
    employees = [e["name"] for _, e in emp_items]


    data["employees"] = employees
    for name in employees:
        data["trained"].setdefault(name, "N")

    save_data(data)
    print(f"[{datetime.now(tz).strftime('%H:%M:%S')}] Torn company sync complete.")

    trains = company["company_detailed"].get("trains_available", 0)
    if trains >= 10:
        asyncio.run_coroutine_threadsafe(
            notify_trains_available(trains),
            bot.loop
        )


async def notify_trains_available(trains):
    user = await bot.fetch_user(USER_ID)
    await user.send(f"🚨 You have **{trains}** company trains available!")

# --- Commands ---
import re

async def verify_employee(member: discord.Member):
    """Checks member nickname for Torn name and gives/removes Employee role."""
    data = load_data()
    rotation_names = [n.lower() for n in data.get("employees", [])]
    guild = member.guild

    # Find (or create) Employee role
    role = discord.utils.get(guild.roles, name="Employee")
    if not role:
        role = await guild.create_role(name="Employee", reason="Auto-created by bot")

    # Extract Torn name from nickname
    nickname = member.nick or member.name
    match = re.match(r"^(.+?)\s*\[\d+\]", nickname)
    torn_name = match.group(1).strip().lower() if match else nickname.lower()

    if torn_name in rotation_names:
        if role not in member.roles:
            await member.add_roles(role, reason="Matched in rotation list")
            return f"✅ Verified and Employee role assigned! Welcome, {torn_name.title()}."
        else:
            return f"✅ Already verified, {torn_name.title()}."
    else:
        # Not in company rotation
        if role in member.roles:
            await member.remove_roles(role, reason="No longer in company list")
        return f"🚫 {torn_name.title()} not found in the current company rotation list."


@bot.tree.command(name="rotation", description="Show the current training rotation order")
async def rotation(interaction: discord.Interaction):
    if not has_company_role(interaction):
        await interaction.response.send_message("🚫 You don’t have permission to use this command.", ephemeral=True)
        return

    data = load_data()
    data = load_data()
    employees = data.get("employees", [])
    trained = data.get("trained", {})
    if not employees:
        await interaction.response.send_message("No employee data yet. Wait for the 9 PM update.", ephemeral=True)
        return

    msg = "**Training Rotation Order:**\n"
    for i, name in enumerate(employees, start=1):
        status = "✅" if trained.get(name) == "Y" else "❌"
        msg += f"{i}. {name} — {status}\n"
    await interaction.response.send_message(msg)

@bot.tree.command(name="remaining", description="Show employees who still need training this rotation")
async def remaining(interaction: discord.Interaction):
    if not has_company_role(interaction):
        await interaction.response.send_message("🚫 You don’t have permission to use this command.", ephemeral=True)
        return

    try:
        data = load_data()
        trained = data.get("trained", {})
        employees = data.get("employees", [])

        if not employees:
            await interaction.response.send_message("⚠️ No employee data available yet. Try running /forceupdate first.", ephemeral=True)
            return

        remaining_list = [n for n in employees if trained.get(n) != "Y"]
        if remaining_list:
            msg = "**Employees left to train:**\n" + "\n".join([f"❌ {name}" for name in remaining_list])
        else:
            msg = "✅ All employees are trained this rotation!"

        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"Error in /remaining: {e}")
        await interaction.response.send_message("⚠️ An unexpected error occurred while processing /remaining.", ephemeral=True)


@bot.tree.command(name="status", description="Show company sync and training status summary")
async def status(interaction: discord.Interaction):
    if not has_company_role(interaction):
        await interaction.response.send_message("🚫 You don’t have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()  # <--- acknowledge immediately

    try:
        data = load_data()
        employees = data.get("employees", [])
        trained = data.get("trained", {})
        trained_count = sum(1 for v in trained.values() if v == "Y")
        total = len(employees)

        company = get_company_data()
        trains = company["company_detailed"].get("trains_available", 0) if company else "N/A"

        now = datetime.now(tz)
        next_sync_time = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if next_sync_time < now:
            next_sync_time += timedelta(days=1)
        time_until_next = next_sync_time - now
        hours, remainder = divmod(int(time_until_next.total_seconds()), 3600)
        minutes = remainder // 60

        last_sync = now.strftime("%Y-%m-%d %H:%M")

        msg = (
            f"🏢 **Company:** Violent RE:Solutions\n"
            f"📅 **Last Sync:** {last_sync} (UK)\n"
            f"🔄 **Next Sync:** in {hours}h {minutes}m\n"
            f"💪 **Trains Available:** {trains}\n"
            f"🎯 **Rotation Progress:** {trained_count} / {total} trained"
        )

        await interaction.followup.send(msg)  # <--- send after work is done
    except Exception as e:
        print(f"Error in /status: {e}")
        await interaction.followup.send("⚠️ Failed to retrieve status.", ephemeral=True)


@bot.tree.command(name="train", description="(Owner only) Mark an employee as trained")
async def train(interaction: discord.Interaction, name: str):
    # Restrict to owner ID only
    if interaction.user.id != 209088844186910722:
        await interaction.response.send_message("🚫 You don’t have permission to do that.", ephemeral=True)
        return

    data = load_data()
    if name not in data["trained"]:
        await interaction.response.send_message("Employee not found in rotation.")
        return

    data["trained"][name] = "Y"
    save_data(data)
    await interaction.response.send_message(f"✅ {name} marked as trained.")

    # Reset if all trained
    if all(v == "Y" for v in data["trained"].values()):
        for n in data["trained"]:
            data["trained"][n] = "N"
        save_data(data)
        await interaction.followup.send("♻️ All employees trained! Rotation reset.")


@bot.tree.command(name="forceupdate", description="(Owner only) Manually sync company data from Torn API now")
async def forceupdate(interaction: discord.Interaction):
    # Restrict to owner ID only
    if interaction.user.id != 209088844186910722:
        await interaction.response.send_message("🚫 You don’t have permission to do that.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    sync_torn_data()
    await interaction.followup.send("🔄 Forced Torn data update complete.")

@bot.tree.command(name="verify", description="Verify your Torn account and receive the Employee role if eligible")
async def verify(interaction: discord.Interaction):
    member = interaction.user

    await interaction.response.defer(thinking=True)
    result_msg = await verify_employee(member)
    await interaction.followup.send(result_msg, ephemeral=True)



    # Reset if all trained
    if all(v == "Y" for v in data["trained"].values()):
        for n in data["trained"]:
            data["trained"][n] = "N"
        save_data(data)
        await interaction.followup.send("♻️ All employees trained! Rotation reset.")

# --- Scheduler ---
scheduler = AsyncIOScheduler(timezone=tz)
scheduler.add_job(sync_torn_data, "cron", hour=21, minute=0)  # 9 PM UK

import asyncio

@bot.event
async def on_member_join(member: discord.Member):
    # the server (guild) where the bot is running
    guild = member.guild

    # your welcome message
    welcome_message = (
        f"👋 Welcome to **{guild.name}**, {member.mention}!\n"
        "Please use the `/verify` command to get your employee role."
    )

    # pick the channel you want the message in
    # easiest: first text channel named 'general'
    channel = discord.utils.get(guild.text_channels, name="general")

    if channel:
        await channel.send(welcome_message)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"🔁 Synced {len(synced)} commands globally.")
    except Exception as e:
        print(f"⚠️ Command sync failed: {e}")

    if not scheduler.running:
        scheduler.start()
        print("📅 Scheduler started.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(bot.start(TOKEN))


