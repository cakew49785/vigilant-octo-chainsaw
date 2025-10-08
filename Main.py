import os
import asyncio
import aiosqlite
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View

# ---------- Config from env ----------
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID")) if os.getenv("TICKET_CATEGORY_ID") else None
SUPPORT_ROLE_ID = int(os.getenv("SUPPORT_ROLE_ID")) if os.getenv("SUPPORT_ROLE_ID") else None
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID")) if os.getenv("LOG_CHANNEL_ID") else None
DATABASE = os.getenv("DATABASE_PATH", "tickets.db")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable required")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# ---------- Database helpers ----------
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                channel_id INTEGER UNIQUE,
                owner_id INTEGER,
                status TEXT,
                created_at TEXT
            );
        """)
        await db.commit()

async def create_ticket_record(guild_id, channel_id, owner_id):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            "INSERT INTO tickets (guild_id, channel_id, owner_id, status, created_at) VALUES (?, ?, ?, 'open', ?)",
            (guild_id, channel_id, owner_id, datetime.utcnow().isoformat())
        )
        await db.commit()

async def close_ticket_record(channel_id):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE tickets SET status = 'closed' WHERE channel_id = ?", (channel_id,))
        await db.commit()

async def get_ticket_by_channel(channel_id):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT id, guild_id, channel_id, owner_id, status, created_at FROM tickets WHERE channel_id = ?", (channel_id,))
        row = await cur.fetchone()
        return row

async def next_ticket_number(guild_id):
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ?", (guild_id,))
        cnt = (await cur.fetchone())[0] or 0
        return cnt + 1

# ---------- Ticket UI ----------
class TicketView(View):
    def __init__(self, ticket_name: str="Support Ticket"):
        super().__init__(timeout=None)
        self.ticket_name = ticket_name
        self.add_item(Button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="create_ticket"))

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        author = interaction.user

        category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        ticket_num = await next_ticket_number(guild.id)
        channel_name = f"ticket-{ticket_num}-{author.name}".lower().replace(" ", "-")[:90]

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            author: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        # support role
        if SUPPORT_ROLE_ID:
            role = guild.get_role(SUPPORT_ROLE_ID)
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        # create channel
        channel = await guild.create_text_channel(channel_name, overwrites=overwrites, category=category, reason="New support ticket")
        await create_ticket_record(guild.id, channel.id, author.id)

        # initial message in ticket
        embed = discord.Embed(title="Ticket created", description=f"{author.mention}, a staff member will be with you shortly.\nUse `!close` to close this ticket.", color=discord.Color.blurple())
        await channel.send(content=f"{author.mention}", embed=embed)

        await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)

# ---------- Utility: transcript ----------
async def create_transcript(channel: discord.TextChannel) -> str:
    messages = []
    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.isoformat()
        author = f"{msg.author} ({msg.author.id})"
        content = msg.content
        attachments = " ".join(a.url for a in msg.attachments) if msg.attachments else ""
        messages.append(f"[{ts}] {author}: {content} {attachments}")
    transcript = "\n".join(messages) or "No messages"
    filename = f"transcript-{channel.id}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(transcript)
    return filename

# ---------- Commands / Slash setup ----------
@tree.command(name="setup", description="Create the ticket panel in this channel", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.checks.has_permissions(administrator=True)
async def slash_setup(interaction: discord.Interaction):
    view = TicketView()
    embed = discord.Embed(title="Create a ticket", description="Click the button below to open a ticket.", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

# ---------- Core ticket commands (workflows) ----------
@bot.command(name="close")
@commands.has_permissions(manage_channels=True)
async def cmd_close(ctx: commands.Context, *, reason: Optional[str] = "Closed by staff"):
    row = await get_ticket_by_channel(ctx.channel.id)
    if not row:
        await ctx.send("This channel is not a ticket.")
        return
    await ctx.send("Closing ticket — creating transcript...")
    transcript_file = await create_transcript(ctx.channel)
    await close_ticket_record(ctx.channel.id)
    # send transcript to log channel
    if LOG_CHANNEL_ID:
        log_ch = bot.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            try:
                await log_ch.send(f"Ticket closed: {ctx.channel.name} — reason: {reason}", file=discord.File(transcript_file))
            except Exception as e:
                await ctx.send(f"Failed to upload transcript to log channel: {e}")
    # optionally DM owner
    owner_id = row[3]
    try:
        owner = await bot.fetch_user(owner_id)
        with open(transcript_file, "rb") as f:
            await owner.send(f"Your ticket {ctx.channel.name} was closed. Reason: {reason}", file=discord.File(f, filename=transcript_file))
    except Exception:
        pass
    # finally, archive or delete channel
    await ctx.channel.edit(topic=f"Closed: {reason}")
    await ctx.send("Ticket closed. Channel will be deleted in 10 seconds.")
    await asyncio.sleep(10)
    await ctx.channel.delete(reason=f"Ticket closed: {reason}")

@bot.command(name="claim")
@commands.has_role(SUPPORT_ROLE_ID)  # requires support role
async def cmd_claim(ctx: commands.Context):
    await ctx.send(f"{ctx.author.mention} claimed this ticket.")

@bot.command(name="add")
@commands.has_permissions(manage_channels=True)
async def cmd_add(ctx: commands.Context, member: discord.Member):
    perms = ctx.channel.overwrites_for(member)
    perms.read_messages = True
    perms.send_messages = True
    await ctx.channel.set_permissions(member, overwrite=perms)
    await ctx.send(f"{member.mention} was added to the ticket.")

@bot.command(name="remove")
@commands.has_permissions(manage_channels=True)
async def cmd_remove(ctx: commands.Context, member: discord.Member):
    await ctx.channel.set_permissions(member, overwrite=None)
    await ctx.send(f"{member.mention} was removed from the ticket.")

@bot.command(name="transcript")
@commands.has_permissions(manage_channels=True)
async def cmd_transcript(ctx: commands.Context):
    await ctx.send("Creating transcript...")
    f = await create_transcript(ctx.channel)
    await ctx.send(file=discord.File(f))

# ---------- Moderation command framework (50+ command stubs) ----------
# Implemented commands (examples): ban, kick, mute, unmute, warn, warnlist, purge
@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def cmd_ban(ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
    await member.ban(reason=reason)
    await ctx.send(f"{member} has been banned. Reason: {reason}")

@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def cmd_kick(ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
    await member.kick(reason=reason)
    await ctx.send(f"{member} has been kicked. Reason: {reason}")

@bot.command(name="mute")
@commands.has_permissions(manage_roles=True)
async def cmd_mute(ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None):
    # simple mute implementation: create/get 'Muted' role and apply deny send_messages
    guild = ctx.guild
    role = discord.utils.get(guild.roles, name="Muted")
    if role is None:
        role = await guild.create_role(name="Muted", reason="Create mute role")
        for ch in guild.channels:
            await ch.set_permissions(role, send_messages=False, speak=False)
    await member.add_roles(role, reason=reason)
    await ctx.send(f"{member} muted. Reason: {reason}")

@bot.command(name="unmute")
@commands.has_permissions(manage_roles=True)
async def cmd_unmute(ctx: commands.Context, member: discord.Member):
    role = discord.utils.get(ctx.guild.roles, name="Muted")
    if role:
        await member.remove_roles(role)
    await ctx.send(f"{member} unmuted.")

@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def cmd_warn(ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = "No reason provided"):
    # This is a stub: connect to persistent warning storage later
    await ctx.send(f"{member.mention} warned. Reason: {reason}")

@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def cmd_purge(ctx: commands.Context, amount: int):
    deleted = await ctx.channel.purge(limit=amount + 1)  # include the command message
    await ctx.send(f"Deleted {len(deleted)-1} messages.", delete_after=5)

# Many more moderator stubs (names only, ready to implement)
MODERATOR_STUBS = [
    "tempban", "softban", "forceban", "viewban", "listbans", "massban",
    "timeout", "untimeout", "nick", "setnick", "delnick", "roleadd", "roleremove",
    "rolecreate", "roledel", "forcerole", "lock", "unlock", "slowmode",
    "announce", "setwelcome", "setgoodbye", "purgeuser", "clearwarns", "warns",
    "modlog", "setmodlog", "slowmode_user", "masskick", "audit", "forceroleupdate",
    "backupserver", "restorebackup", "setrules", "lockdown", "endlockdown",
    "starboard", "setstarboard", "togglefeature", "setprefix", "getprefix",
    "blacklist", "unblacklist", "ghostping", "pruneinactive", "checkraid",
    "antispam", "antiraid", "setantiraid", "whitelist", "unwhitelist",
    "rolehierarchy", "managechannels", "createtext", "createvoice"
]

# create stubs
for name in MODERATOR_STUBS:
    async def _stub(ctx, *args, _name=name, **kwargs):
        await ctx.send(f"Moderator command `{_name}` called. This is a stub; implement behavior.")
    _stub.__name__ = f"cmd_{name}"
    cmd = commands.Command(_stub, name=name)
    bot.add_command(cmd)

# ---------- Help command ----------
@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    em = discord.Embed(title="Ticket Bot - Help", color=discord.Color.blurple())
    em.add_field(name="Ticket", value="`!close`, `!claim`, `!add @user`, `!remove @user`, `/setup` (admin only)", inline=False)
    em.add_field(name="Moderation", value="Many mod commands available; use `!help <command>`", inline=False)
    await ctx.send(embed=em)

# ---------- Events ----------
@bot.event
async def on_ready():
    await init_db()
    if GUILD_ID:
        print(f"Logged in as {bot.user} (guild restricted). Ready.")
    else:
        print(f"Logged in as {bot.user}. Ready.")
    try:
        synced = await tree.sync(guild=discord.Object(id=GUILD_ID)) if GUILD_ID else await tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print("Failed to sync commands:", e)

# ---------- Run ----------
if __name__ == "__main__":
    bot.run(TOKEN)
