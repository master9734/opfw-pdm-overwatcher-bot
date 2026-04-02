import os
import io
import asyncio
import docker
import discord
import pytz
from datetime import datetime
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", CHANNEL_ID))
OFFLINE_ROLE_ID = int(os.getenv("OFFLINE_ROLE_ID", "0"))
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "30"))

# Only show containers ending with these
ALLOWED_SUFFIXES = ("-bot", "_bot", "bot")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

docker_client = docker.from_env()

dashboard_message_id = None
last_status_map = {}  # {container_name: "online"/"offline"}

BST = pytz.timezone("Asia/Dhaka")


def now_bst():
    return datetime.now(BST).strftime("%d %b %Y • %I:%M %p BST")


def is_bot_container(container_name: str):
    name = container_name.lower()
    return (
        name.endswith("-bot")
        or name.endswith("_bot")
        or name == "bot"
        or "-bot-" in name
        or "_bot_" in name
    )


def clean_container_name(name: str):
    # Show original full name, but cleaner if docker adds slash
    return name.replace("/", "").strip()


def get_all_bot_containers():
    containers = docker_client.containers.list(all=True)
    return [c for c in containers if is_bot_container(c.name)]


def get_container_stats(container):
    try:
        container.reload()
        stats = container.stats(stream=False)

        # CPU %
        cpu_delta = (
            stats["cpu_stats"]["cpu_usage"]["total_usage"]
            - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            stats["cpu_stats"]["system_cpu_usage"]
            - stats["precpu_stats"]["system_cpu_usage"]
        )
        num_cpus = len(stats["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))

        cpu_percent = 0.0
        if system_delta > 0 and cpu_delta > 0:
            cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0

        # RAM
        mem_usage = stats["memory_stats"].get("usage", 0)
        mem_limit = stats["memory_stats"].get("limit", 1)

        mem_usage_mib = mem_usage / (1024 * 1024)
        mem_limit_gib = mem_limit / (1024 * 1024 * 1024)

        # Uptime / docker state
        state = container.attrs.get("State", {})
        status = state.get("Status", "unknown")
        started_at = state.get("StartedAt", "")

        docker_uptime = "Unknown"
        if started_at and status == "running":
            try:
                started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                now_utc = datetime.utcnow().astimezone(started_dt.tzinfo)
                delta = now_utc - started_dt

                total_seconds = int(delta.total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60

                if hours > 0:
                    docker_uptime = f"Up {hours} hours"
                else:
                    docker_uptime = f"Up {minutes} minutes"
            except:
                docker_uptime = status.capitalize()
        else:
            docker_uptime = status.capitalize()

        return {
            "cpu_percent": round(cpu_percent, 2),
            "mem_usage_mib": round(mem_usage_mib, 2),
            "mem_limit_gib": round(mem_limit_gib, 2),
            "docker_uptime": docker_uptime,
            "status": status
        }

    except Exception as e:
        return {
            "cpu_percent": 0.0,
            "mem_usage_mib": 0.0,
            "mem_limit_gib": 0.0,
            "docker_uptime": "Unknown",
            "status": "unknown"
        }


def build_dashboard_embed():
    embed = discord.Embed(
        title="🤖 VPS Bot Monitor",
        description="Live status of all detected bot containers.",
        color=discord.Color.blurple()
    )

    containers = get_all_bot_containers()

    if not containers:
        embed.add_field(
            name="No bot containers found",
            value="No Docker containers ending in `-bot` were detected.",
            inline=False
        )
    else:
        for container in containers:
            stats = get_container_stats(container)
            name = clean_container_name(container.name)

            is_online = stats["status"] == "running"
            status_emoji = "🟢" if is_online else "🔴"
            status_text = "Online" if is_online else "Offline"

            value = (
                f"**Status:** {status_emoji} {status_text}\n"
                f"**Docker:** `{stats['docker_uptime']}`\n"
                f"**CPU:** `{stats['cpu_percent']}%`\n"
                f"**RAM:** `{stats['mem_usage_mib']}MiB / {stats['mem_limit_gib']}GiB`"
            )

            embed.add_field(
                name=f"📦 {name}",
                value=value,
                inline=False
            )

    embed.set_footer(text=f"Last checked: {now_bst()}")
    return embed


async def send_alerts_if_needed():
    global last_status_map

    alert_channel = bot.get_channel(ALERT_CHANNEL_ID)
    if not alert_channel:
        return

    containers = get_all_bot_containers()
    current_status_map = {}

    for container in containers:
        stats = get_container_stats(container)
        name = clean_container_name(container.name)
        current_status = "online" if stats["status"] == "running" else "offline"
        current_status_map[name] = current_status

        previous_status = last_status_map.get(name)

        if previous_status is None:
            continue

        if previous_status != current_status:
            role_ping = f"<@&{OFFLINE_ROLE_ID}> " if OFFLINE_ROLE_ID else ""

            if current_status == "offline":
                await alert_channel.send(
                    f"{role_ping}🚨 **{name}** is now **OFFLINE**.\nChecked: `{now_bst()}`"
                )
            elif current_status == "online":
                await alert_channel.send(
                    f"{role_ping}✅ **{name}** is back **ONLINE**.\nChecked: `{now_bst()}`"
                )

    last_status_map = current_status_map


class RestartDropdown(discord.ui.Select):
    def __init__(self):
        containers = get_all_bot_containers()

        options = []
        for c in containers[:25]:  # Discord max options = 25
            options.append(
                discord.SelectOption(
                    label=clean_container_name(c.name)[:100],
                    value=c.name
                )
            )

        if not options:
            options = [
                discord.SelectOption(
                    label="No bot containers found",
                    value="none"
                )
            ]

        super().__init__(
            placeholder="Select a bot container to restart...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message("No bot containers found.", ephemeral=True)
            return

        container_name = self.values[0]

        try:
            container = docker_client.containers.get(container_name)
            await interaction.response.send_message(
                f"🔄 Restarting **{container_name}**...",
                ephemeral=True
            )

            await asyncio.to_thread(container.restart)

            await asyncio.sleep(3)
            await update_dashboard_message()

            await interaction.followup.send(
                f"✅ Restarted **{container_name}** successfully.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to restart **{container_name}**.\n```{e}```",
                ephemeral=True
            )


class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RestartDropdown())

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, custom_id="refresh_dashboard")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await update_dashboard_message()
        await interaction.response.send_message("🔄 Dashboard refreshed.", ephemeral=True)

    @discord.ui.button(label="Restart All", style=discord.ButtonStyle.danger, custom_id="restart_all_bots")
    async def restart_all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        containers = get_all_bot_containers()

        if not containers:
            await interaction.response.send_message("No bot containers found.", ephemeral=True)
            return

        await interaction.response.send_message("⚠️ Restarting all bot containers...", ephemeral=True)

        for container in containers:
            try:
                await asyncio.to_thread(container.restart)
            except Exception:
                pass

        await asyncio.sleep(5)
        await update_dashboard_message()

        await interaction.followup.send("✅ All detected bot containers have been restarted.", ephemeral=True)


async def update_dashboard_message():
    global dashboard_message_id

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    embed = build_dashboard_embed()
    view = DashboardView()

    try:
        if dashboard_message_id:
            msg = await channel.fetch_message(dashboard_message_id)
            await msg.edit(embed=embed, view=view)
        else:
            msg = await channel.send(embed=embed, view=view)
            dashboard_message_id = msg.id
    except discord.NotFound:
        msg = await channel.send(embed=embed, view=view)
        dashboard_message_id = msg.id
    except Exception as e:
        print(f"Dashboard update error: {e}")


@tasks.loop(seconds=REFRESH_SECONDS)
async def auto_refresh_dashboard():
    await update_dashboard_message()
    await send_alerts_if_needed()


@bot.event
async def on_ready():
    global last_status_map

    print(f"Logged in as {bot.user}")

    bot.add_view(DashboardView())

    # Initialize status map
    containers = get_all_bot_containers()
    for c in containers:
        stats = get_container_stats(c)
        name = clean_container_name(c.name)
        last_status_map[name] = "online" if stats["status"] == "running" else "offline"

    await update_dashboard_message()

    if not auto_refresh_dashboard.is_running():
        auto_refresh_dashboard.start()


bot.run(DISCORD_TOKEN)