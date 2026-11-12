import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord


WINDOW_SECONDS = 5
WORD_LIMIT = 10
SLOWMODE_SECONDS = 7 * 60
INVESTIGATION_LOOKBACK_SECONDS = 10 * 60


def register_anti_spam(bot, db):
    """Detect rapid word floods and temporarily slow down every text channel."""

    db.execute("""
        CREATE TABLE IF NOT EXISTS spam_slowmode_restore (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            old_delay INTEGER NOT NULL,
            ends_at REAL NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
    """)
    db.commit()

    activity = defaultdict(deque)
    guild_locks = defaultdict(asyncio.Lock)
    restore_tasks = {}
    last_report = {}
    last_registration_gate = {}

    def eligible(member, allow_bot=False):
        perms = member.guild_permissions
        return not (
            (member.bot and not allow_bot)
            or member.id == member.guild.owner_id
            or perms.administrator
            or perms.manage_messages
        )

    def age_text(value):
        if value is None:
            return "Bilinmiyor"
        seconds = max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days} gün {hours} saat"

    async def send_report(guild, member, trigger_message, word_count, samples):
        row = db.execute(
            "SELECT kanal_id FROM log_ayarlar WHERE guild_id = ?",
            (guild.id,),
        ).fetchone()
        channel = guild.get_channel(row[0]) if row else None
        if row and channel is None:
            try:
                channel = await bot.fetch_channel(row[0])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        # Log kanalı ayarlı değilse veya erişilemiyorsa raporu tetiklenen kanala gönder.
        if channel is None or not hasattr(channel, "send"):
            channel = trigger_message.channel
        if not hasattr(channel, "send"):
            logging.warning(
                "Spam raporu için gönderilebilir kanal yok: guild=%s user=%s",
                guild.id, member.id,
            )
            return

        created_at = member.created_at
        joined_at = member.joined_at
        roles = [role.mention for role in member.roles if not role.is_default()]
        role_text = ", ".join(roles)[:900] if roles else "Ek rol yok"
        perms = member.guild_permissions
        permission_text = ", ".join(
            name for name, enabled in (
                ("Yönetici", perms.administrator),
                ("Kanalları Yönet", perms.manage_channels),
                ("Mesajları Yönet", perms.manage_messages),
                ("Rolleri Yönet", perms.manage_roles),
            ) if enabled
        ) or "Özel yönetim izni yok"

        embed = discord.Embed(
            title="🚨 Otomatik spam koruması tetiklendi",
            description=(
                f"{member.mention} kullanıcısı 5 saniye içinde "
                f"**{word_count} kelime** gönderdi. Sunucunun metin ve forum kanallarına "
                f"7 dakikalık yavaş mod uygulandı."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Hesap incelemesi",
            value=(
                f"Kullanıcı: {member} (\u0060{member.id}\u0060)\n"
                f"Hesap yaşı: {age_text(created_at)}\n"
                f"Sunucuya katılma: {age_text(joined_at)} önce\n"
                f"Bot hesabı: {'Evet' if member.bot else 'Hayır'}"
            ),
            inline=False,
        )
        embed.add_field(name="Roller", value=role_text, inline=False)
        embed.add_field(name="Yetkiler", value=permission_text, inline=False)
        embed.add_field(
            name="Tetikleyen kanal",
            value=getattr(trigger_message.channel, "mention", "Bilinmiyor"),
            inline=True,
        )
        embed.add_field(
            name="Tetikleyen mesaj",
            value=(trigger_message.content or "*Metin yok*")[:900],
            inline=False,
        )
        if samples:
            embed.add_field(
                name="Son 10 dakikadaki örnek mesajlar",
                value="\n".join(samples)[:1000],
                inline=False,
            )
        embed.set_footer(text="Bu rapor otomatik tespittir; yaptırım kararı değildir.")
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("Spam raporu gönderilemedi: guild=%s", guild.id)

    async def collect_recent_samples(message):
        samples = []
        history = getattr(message.channel, "history", None)
        if history is None:
            return samples
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=INVESTIGATION_LOOKBACK_SECONDS
        )
        try:
            async for item in message.channel.history(limit=100, after=cutoff):
                if item.author.id != message.author.id or item.author.bot:
                    continue
                body = (item.content or "[metin dışı mesaj]").replace("\n", " ")
                samples.append(f"• {body[:180]}")
                if len(samples) >= 5:
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass
        return samples

    async def apply_server_slowmode(guild):
        async with guild_locks[guild.id]:
            now = time.time()
            ends_at = now + SLOWMODE_SECONDS
            channels = [
                channel for channel in guild.channels
                if isinstance(channel, (discord.TextChannel, discord.ForumChannel))
            ]
            for channel in channels:
                existing = db.execute(
                    "SELECT old_delay FROM spam_slowmode_restore "
                    "WHERE guild_id = ? AND channel_id = ?",
                    (guild.id, channel.id),
                ).fetchone()
                if existing:
                    try:
                        if int(getattr(channel, "slowmode_delay", 0) or 0) < SLOWMODE_SECONDS:
                            await channel.edit(
                                slowmode_delay=SLOWMODE_SECONDS,
                                reason="Otomatik spam koruması hâlâ aktif",
                            )
                        db.execute(
                            "UPDATE spam_slowmode_restore SET ends_at = ? "
                            "WHERE guild_id = ? AND channel_id = ?",
                            (ends_at, guild.id, channel.id),
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        logging.warning(
                            "Aktif spam slowmode yenilenemedi: guild=%s channel=%s",
                            guild.id, channel.id,
                        )
                    continue

                old_delay = int(getattr(channel, "slowmode_delay", 0) or 0)
                try:
                    if old_delay < SLOWMODE_SECONDS:
                        await channel.edit(
                            slowmode_delay=SLOWMODE_SECONDS,
                            reason="Otomatik spam koruması: 5 saniyede 10'dan fazla kelime",
                        )
                    db.execute(
                        "INSERT OR REPLACE INTO spam_slowmode_restore "
                        "(guild_id, channel_id, old_delay, ends_at) VALUES (?, ?, ?, ?)",
                        (guild.id, channel.id, old_delay, ends_at),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logging.warning(
                        "Spam slowmode uygulanamadı: guild=%s channel=%s",
                        guild.id, channel.id,
                    )
            db.commit()

            task = restore_tasks.get(guild.id)
            if task is None or task.done():
                restore_tasks[guild.id] = asyncio.create_task(
                    restore_when_due(guild.id)
                )

    async def restore_when_due(guild_id):
        while True:
            row = db.execute(
                "SELECT MAX(ends_at) FROM spam_slowmode_restore WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            ends_at = row[0] if row else None
            if ends_at is None:
                return
            remaining = ends_at - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue

            guild = bot.get_guild(guild_id)
            if guild is None:
                return
            async with guild_locks[guild_id]:
                rows = db.execute(
                    "SELECT channel_id, old_delay FROM spam_slowmode_restore "
                    "WHERE guild_id = ? AND ends_at <= ?",
                    (guild_id, time.time()),
                ).fetchall()
                for channel_id, old_delay in rows:
                    channel = guild.get_channel(channel_id)
                    if channel is None:
                        db.execute(
                            "DELETE FROM spam_slowmode_restore "
                            "WHERE guild_id = ? AND channel_id = ?",
                            (guild_id, channel_id),
                        )
                        continue
                    try:
                        await channel.edit(
                            slowmode_delay=old_delay,
                            reason="Otomatik spam koruması: 7 dakikalık yavaş mod sona erdi",
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        logging.warning(
                            "Önceki slowmode geri yüklenemedi; 60 saniye sonra yeniden denenecek: guild=%s channel=%s",
                            guild_id, channel_id,
                        )
                        db.execute(
                            "UPDATE spam_slowmode_restore SET ends_at = ? "
                            "WHERE guild_id = ? AND channel_id = ?",
                            (time.time() + 60, guild_id, channel_id),
                        )
                        continue
                    db.execute(
                        "DELETE FROM spam_slowmode_restore "
                        "WHERE guild_id = ? AND channel_id = ?",
                        (guild_id, channel_id),
                    )
                db.commit()

    async def registration_gate_reason(member):
        # .gg ile başlayan Discord kullanıcı adları doğrudan şüpheli kabul edilir.
        if (
            member.name.casefold().startswith(".gg")
            or member.display_name.casefold().startswith(".gg")
        ):
            return "Kullanıcı adı veya sunucu takma adı .gg ile başlıyor"

        # Kayıt sistemi bu sunucuda kurulmamışsa kayıt rolü kontrolünü atla.
        row = db.execute(
            "SELECT kayitli_rol_id FROM kayit_ayarlar WHERE guild_id = ?",
            (member.guild.id,),
        ).fetchone()
        if not row:
            return None

        registered_role = member.guild.get_role(row[0])
        if registered_role is None:
            logging.warning(
                "Kayıtlı rol bulunamadı; kayıt kontrolü atlandı: guild=%s role=%s",
                member.guild.id, row[0],
            )
            return None
        if registered_role not in member.roles:
            return "Kayıt sisteminde kayıtlı rolü bulunmuyor"
        return None

    async def send_registration_gate_report(message, reason):
        row = db.execute(
            "SELECT kanal_id FROM log_ayarlar WHERE guild_id = ?",
            (message.guild.id,),
        ).fetchone()
        channel = message.guild.get_channel(row[0]) if row else None
        if channel is None:
            channel = message.channel

        embed = discord.Embed(
            title="🛡️ Kayıt kontrolü nedeniyle yavaş mod",
            description=(
                f"{message.author.mention} için sunucunun metin ve forum kanallarına "
                f"7 dakikalık yavaş mod uygulandı.\n**Neden:** {reason}"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Kullanıcı", value=f"{message.author} (\u0060{message.author.id}\u0060)", inline=False)
        embed.add_field(name="Mesaj", value=(message.content or "*Metin yok*")[:900], inline=False)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logging.warning("Kayıt kontrolü raporu gönderilemedi: guild=%s", message.guild.id)

    async def on_message(message):
        if (
            message.guild is None
            or message.webhook_id is not None
            or not isinstance(message.author, discord.Member)
        ):
            return

        is_gg_account = (
            message.author.name.casefold().startswith(".gg")
            or message.author.display_name.casefold().startswith(".gg")
        )
        # Diğer botları yok say; .gg ile başlayan botları da koruma kapsamına al.
        if message.author.bot and not is_gg_account:
            return
        if not eligible(message.author, allow_bot=is_gg_account):
            return

        gate_reason = await registration_gate_reason(message.author)
        if gate_reason:
            gate_key = (message.guild.id, message.author.id)
            now_wall = time.time()
            # Aynı kullanıcı her mesajında süreyi uzatmasın; 7 dakikada bir tetikle.
            if now_wall - last_registration_gate.get(gate_key, 0) >= SLOWMODE_SECONDS:
                last_registration_gate[gate_key] = now_wall
                await apply_server_slowmode(message.guild)
                await send_registration_gate_report(message, gate_reason)
            return

        words = len((message.content or "").split())
        if words == 0:
            return

        now = time.monotonic()
        key = (message.guild.id, message.author.id)
        events = activity[key]
        events.append((now, words))
        while events and now - events[0][0] > WINDOW_SECONDS:
            events.popleft()

        total_words = sum(count for _, count in events)
        if total_words <= WORD_LIMIT:
            return

        # Clear the window so one burst cannot fire repeatedly on every message.
        events.clear()
        await apply_server_slowmode(message.guild)

        report_key = (message.guild.id, message.author.id)
        wall_now = time.time()
        if wall_now - last_report.get(report_key, 0) < 30:
            return
        last_report[report_key] = wall_now

        samples = await collect_recent_samples(message)
        await send_report(
            message.guild,
            message.author,
            message,
            total_words,
            samples,
        )

    async def restore_on_ready():
        rows = db.execute(
            "SELECT DISTINCT guild_id FROM spam_slowmode_restore"
        ).fetchall()
        for (guild_id,) in rows:
            if guild_id in restore_tasks and not restore_tasks[guild_id].done():
                continue
            restore_tasks[guild_id] = asyncio.create_task(
                restore_when_due(guild_id)
            )

    bot.add_listener(on_message, "on_message")
    bot.add_listener(restore_on_ready, "on_ready")
