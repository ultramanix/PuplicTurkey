import logging
import math
import random
import time

import discord
from discord import app_commands


def register_xp(bot, db, cursor):
    """Register persistent, per-server XP and level commands."""

    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS xp_ayarlar (
        guild_id INTEGER PRIMARY KEY,
        xp_min INTEGER NOT NULL DEFAULT 15,
        xp_max INTEGER NOT NULL DEFAULT 25,
        cooldown_seconds INTEGER NOT NULL DEFAULT 60,
        daily_min INTEGER NOT NULL DEFAULT 50,
        daily_max INTEGER NOT NULL DEFAULT 150,
        daily_cooldown INTEGER NOT NULL DEFAULT 43200,
        level_channel_id INTEGER,
        aktif INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS xp_kullanicilar (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        xp INTEGER NOT NULL DEFAULT 0,
        level INTEGER NOT NULL DEFAULT 0,
        last_message REAL,
        last_daily REAL,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS xp_roller (
        guild_id INTEGER NOT NULL,
        level INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, level),
        UNIQUE (guild_id, role_id)
    );
    """)
    db.commit()
    # Mevcut kurulumlarda XP varsayılan olarak açık kalsın.
    try:
        cursor.execute("ALTER TABLE xp_ayarlar ADD COLUMN aktif INTEGER NOT NULL DEFAULT 1")
    except Exception as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
    db.commit()

    def is_enabled(guild_id):
        cursor.execute("SELECT aktif FROM xp_ayarlar WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        return True if row is None else bool(row[0])

    async def require_enabled(interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Bu komut bir sunucuda kullanılmalı.", ephemeral=True)
            return False
        if not is_enabled(interaction.guild.id):
            await interaction.response.send_message("Bu sunucuda XP sistemi kapalı.", ephemeral=True)
            return False
        return True

    def settings(guild_id):
        cursor.execute(
            "INSERT OR IGNORE INTO xp_ayarlar (guild_id) VALUES (?)",
            (guild_id,),
        )
        db.commit()
        cursor.execute(
            "SELECT xp_min, xp_max, cooldown_seconds, daily_min, daily_max, "
            "daily_cooldown, level_channel_id FROM xp_ayarlar WHERE guild_id = ?",
            (guild_id,),
        )
        row = cursor.fetchone()
        return {
            "xp_min": row[0],
            "xp_max": row[1],
            "cooldown": row[2],
            "daily_min": row[3],
            "daily_max": row[4],
            "daily_cooldown": row[5],
            "channel_id": row[6],
        }

    def level_for_xp(total_xp):
        return int(math.sqrt(max(0, total_xp) / 100))

    def is_admin(interaction):
        return bool(
            interaction.guild
            and (
                interaction.user.id == interaction.guild.owner_id
                or interaction.user.guild_permissions.administrator
            )
        )

    def user_stats(guild_id, user_id):
        cursor.execute(
            "INSERT OR IGNORE INTO xp_kullanicilar (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        db.commit()
        cursor.execute(
            "SELECT xp, level, last_message, last_daily FROM xp_kullanicilar "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = cursor.fetchone()
        return {"xp": row[0], "level": row[1], "last_message": row[2], "last_daily": row[3]}

    async def update_level_role(member, level):
        cursor.execute(
            "SELECT level, role_id FROM xp_roller WHERE guild_id = ? ORDER BY level DESC",
            (member.guild.id,),
        )
        configured = cursor.fetchall()
        if not configured:
            return
        target_role_id = next((role_id for required, role_id in configured if level >= required), None)
        managed_ids = {role_id for _, role_id in configured}
        remove_roles = [
            role for role in member.roles
            if role.id in managed_ids and role.id != target_role_id
        ]
        target_role = member.guild.get_role(target_role_id) if target_role_id else None
        try:
            if remove_roles:
                await member.remove_roles(*remove_roles, reason="XP seviye rolü güncellendi")
            if target_role and target_role not in member.roles:
                if member.guild.me and target_role >= member.guild.me.top_role:
                    logging.warning("XP rolü botun en yüksek rolünün üstünde: guild=%s role=%s", member.guild.id, target_role.id)
                    return
                await member.add_roles(target_role, reason=f"XP seviyesi {level}")
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("XP seviye rolü verilemedi: guild=%s user=%s", member.guild.id, member.id)

    async def notify_level_up(member, level, channel_id=None):
        await update_level_role(member, level)
        channel = member.guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            channel = member.guild.system_channel
        if channel is None:
            return
        try:
            await channel.send(f"Tebrikler {member.mention}! **{level}. seviyeye** ulaştın.")
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("XP seviye bildirimi gönderilemedi: guild=%s user=%s", member.guild.id, member.id)

    async def award_xp(member, amount):
        stats = user_stats(member.guild.id, member.id)
        new_xp = max(0, stats["xp"] + amount)
        new_level = level_for_xp(new_xp)
        cursor.execute(
            "UPDATE xp_kullanicilar SET xp = ?, level = ? WHERE guild_id = ? AND user_id = ?",
            (new_xp, new_level, member.guild.id, member.id),
        )
        db.commit()
        if new_level > stats["level"]:
            cfg = settings(member.guild.id)
            await notify_level_up(member, new_level, cfg["channel_id"])
        return new_xp, new_level

    async def award_message_xp(message):
        if message.guild is None or message.author.bot or not message.content.strip():
            return
        if not is_enabled(message.guild.id):
            return
        content = message.content.lstrip()
        if content.startswith(("G!", "g!", "G+", "g+")):
            return
        cfg = settings(message.guild.id)
        now = time.time()
        stats = user_stats(message.guild.id, message.author.id)
        if stats["last_message"] is not None and now - stats["last_message"] < cfg["cooldown"]:
            return
        amount = random.randint(cfg["xp_min"], cfg["xp_max"])
        new_xp = stats["xp"] + amount
        new_level = level_for_xp(new_xp)
        cursor.execute(
            "UPDATE xp_kullanicilar SET xp = ?, level = ?, last_message = ? "
            "WHERE guild_id = ? AND user_id = ?",
            (new_xp, new_level, now, message.guild.id, message.author.id),
        )
        db.commit()
        if new_level > stats["level"]:
            await notify_level_up(message.author, new_level, cfg["channel_id"])

    async def daily_reward(guild, user):
        if not is_enabled(guild.id):
            return None, "Bu sunucuda XP sistemi kapalı."
        cfg = settings(guild.id)
        stats = user_stats(guild.id, user.id)
        now = time.time()
        if stats["last_daily"] is not None:
            remaining = int(cfg["daily_cooldown"] - (now - stats["last_daily"]))
            if remaining > 0:
                hours, remainder = divmod(remaining, 3600)
                minutes = remainder // 60
                return None, f"Günlük XP'ni tekrar almak için **{hours} saat {minutes} dakika** beklemelisin."
        amount = random.randint(cfg["daily_min"], cfg["daily_max"])
        cursor.execute(
            "UPDATE xp_kullanicilar SET last_daily = ? WHERE guild_id = ? AND user_id = ?",
            (now, guild.id, user.id),
        )
        db.commit()
        new_xp, new_level = await award_xp(user, amount)
        return (amount, new_xp, new_level), None

    @bot.tree.command(name="xp-sistem", description="Bu sunucunun XP sistemini açar veya kapatır.")
    @app_commands.describe(aktif="True: XP sistemini açar; False: XP sistemini kapatır.")
    async def xp_sistem(interaction: discord.Interaction, aktif: bool):
        if interaction.guild is None:
            await interaction.response.send_message("Bu komut bir sunucuda kullanılmalı.", ephemeral=True)
            return
        if not is_admin(interaction):
            await interaction.response.send_message("Bu komut için Yönetici yetkisi gerekli.", ephemeral=True)
            return
        cursor.execute(
            "INSERT INTO xp_ayarlar (guild_id, aktif) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET aktif=excluded.aktif",
            (interaction.guild.id, int(aktif)),
        )
        db.commit()
        durum = "açıldı" if aktif else "kapatıldı"
        await interaction.response.send_message(
            f"Bu sunucuda XP sistemi {durum}. " + ("XP komutları yeniden kullanılabilir." if aktif else "XP kazanımı ve tüm XP komutları devre dışı.")
        )

    @bot.tree.command(name="seviye", description="Kendinin veya seçtiğin üyenin XP seviyesini gösterir.")
    @app_commands.describe(kisi="Seviyesini görmek istediğin üye")
    async def xp_seviye(interaction: discord.Interaction, kisi: discord.Member = None):
        if not await require_enabled(interaction):
            return
        member = kisi or interaction.user
        stats = user_stats(interaction.guild.id, member.id)
        level = level_for_xp(stats["xp"])
        current_floor = level * level * 100
        next_floor = (level + 1) * (level + 1) * 100
        progress = stats["xp"] - current_floor
        needed = next_floor - current_floor
        embed = discord.Embed(
            title=f"{member.display_name} — Seviye {level}",
            description=f"Toplam XP: **{stats['xp']}**\nSonraki seviye: **{max(0, next_floor - stats['xp'])} XP**",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="İlerleme", value=f"{progress}/{needed} XP", inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="sıralama", description="Sunucunun en yüksek XP seviyelerini gösterir.")
    async def xp_siralama(interaction: discord.Interaction):
        if not await require_enabled(interaction):
            return
        cursor.execute(
            "SELECT user_id, xp FROM xp_kullanicilar WHERE guild_id = ? "
            "ORDER BY xp DESC LIMIT 10",
            (interaction.guild.id,),
        )
        rows = cursor.fetchall()
        if not rows:
            await interaction.response.send_message("Henüz XP kazanan kimse yok.")
            return
        satirlar = []
        for index, (user_id, total_xp) in enumerate(rows, 1):
            satirlar.append(f"**{index}.** <@{user_id}> — Seviye {level_for_xp(total_xp)} ({total_xp} XP)")
        await interaction.response.send_message(
            embed=discord.Embed(title="XP Sıralaması", description="\n".join(satirlar), color=discord.Color.gold())
        )

    @bot.tree.command(name="dailyxp", description="12 saatte bir rastgele günlük XP kazan.")
    async def xp_daily(interaction: discord.Interaction):
        if not await require_enabled(interaction):
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Bu komut bir sunucuda kullanılmalı.", ephemeral=True)
            return
        result, error = await daily_reward(interaction.guild, interaction.user)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        amount, total, level = result
        await interaction.response.send_message(f"Günlük ödülün: **+{amount} XP**. Toplam: **{total} XP** — Seviye **{level}**.")

    @bot.command(name="dailyxp")
    async def prefix_dailyxp(ctx):
        if ctx.guild is None or ctx.author.bot:
            return
        if not is_enabled(ctx.guild.id):
            await ctx.reply("Bu sunucuda XP sistemi kapalı.", delete_after=10)
            return
        result, error = await daily_reward(ctx.guild, ctx.author)
        if error:
            await ctx.reply(error, delete_after=10)
            return
        amount, total, level = result
        await ctx.reply(f"Günlük ödülün: **+{amount} XP**. Toplam: **{total} XP** — Seviye **{level}**.", delete_after=15)

    @bot.tree.command(name="xp-ayarla", description="Sunucunun mesaj ve günlük XP kazançlarını ayarlar.")
    @app_commands.describe(
        mesaj_min="Mesaj başına en az XP",
        mesaj_max="Mesaj başına en fazla XP",
        bekleme="Mesaj XP bekleme süresi (saniye)",
        gunluk_min="Günlük en az XP",
        gunluk_max="Günlük en fazla XP",
    )
    async def xp_ayarla(
        interaction: discord.Interaction,
        mesaj_min: app_commands.Range[int, 1, 1000],
        mesaj_max: app_commands.Range[int, 1, 1000],
        bekleme: app_commands.Range[int, 1, 3600],
        gunluk_min: app_commands.Range[int, 1, 10000],
        gunluk_max: app_commands.Range[int, 1, 10000],
    ):
        if not await require_enabled(interaction):
            return
        if not is_admin(interaction):
            await interaction.response.send_message("Bu komut için Yönetici yetkisi gerekli.", ephemeral=True)
            return
        if mesaj_min > mesaj_max or gunluk_min > gunluk_max:
            await interaction.response.send_message("Minimum değer maksimum değerden büyük olamaz.", ephemeral=True)
            return
        cursor.execute(
            "INSERT INTO xp_ayarlar (guild_id, xp_min, xp_max, cooldown_seconds, daily_min, daily_max) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "xp_min=excluded.xp_min, xp_max=excluded.xp_max, cooldown_seconds=excluded.cooldown_seconds, "
            "daily_min=excluded.daily_min, daily_max=excluded.daily_max",
            (interaction.guild.id, mesaj_min, mesaj_max, bekleme, gunluk_min, gunluk_max),
        )
        db.commit()
        await interaction.response.send_message(
            f"XP ayarlandı. Mesaj: {mesaj_min}-{mesaj_max} XP / {bekleme} sn. "
            f"Günlük: {gunluk_min}-{gunluk_max} XP / 12 saat."
        )

    @bot.tree.command(name="xp-kanal", description="Seviye atlama bildirimlerinin gönderileceği kanalı ayarlar.")
    async def xp_kanal(interaction: discord.Interaction, kanal: discord.TextChannel):
        if not await require_enabled(interaction):
            return
        if not is_admin(interaction):
            await interaction.response.send_message("Bu komut için Yönetici yetkisi gerekli.", ephemeral=True)
            return
        cursor.execute(
            "INSERT INTO xp_ayarlar (guild_id, level_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET level_channel_id=excluded.level_channel_id",
            (interaction.guild.id, kanal.id),
        )
        db.commit()
        await interaction.response.send_message(f"Seviye bildirimleri {kanal.mention} kanalına gönderilecek.", ephemeral=True)

    @bot.tree.command(name="xp-rol-ekle", description="Bir seviyeye ulaşınca verilecek rolü ayarlar.")
    async def xp_rol_ekle(
        interaction: discord.Interaction,
        seviye: app_commands.Range[int, 1, 1000],
        rol: discord.Role,
    ):
        if not await require_enabled(interaction):
            return
        if not is_admin(interaction):
            await interaction.response.send_message("Bu komut için Yönetici yetkisi gerekli.", ephemeral=True)
            return
        if interaction.guild.me and rol >= interaction.guild.me.top_role:
            await interaction.response.send_message("Bu rol botun en yüksek rolünün altında olmalı.", ephemeral=True)
            return
        cursor.execute(
            "DELETE FROM xp_roller WHERE guild_id = ? AND role_id = ? AND level <> ?",
            (interaction.guild.id, rol.id, seviye),
        )
        cursor.execute(
            "INSERT INTO xp_roller (guild_id, level, role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, level) DO UPDATE SET role_id=excluded.role_id",
            (interaction.guild.id, seviye, rol.id),
        )
        db.commit()
        await interaction.response.send_message(f"Seviye {seviye} için {rol.mention} rolü ayarlandı.", ephemeral=True)

    @bot.tree.command(name="xp-rol-sil", description="Bir seviyeye bağlı XP rolünü kaldırır.")
    async def xp_rol_sil(interaction: discord.Interaction, seviye: app_commands.Range[int, 1, 1000]):
        if not await require_enabled(interaction):
            return
        if not is_admin(interaction):
            await interaction.response.send_message("Bu komut için Yönetici yetkisi gerekli.", ephemeral=True)
            return
        cursor.execute("DELETE FROM xp_roller WHERE guild_id = ? AND level = ?", (interaction.guild.id, seviye))
        db.commit()
        await interaction.response.send_message(f"Seviye {seviye} rol ayarı kaldırıldı.", ephemeral=True)

    async def admin_xp_change(interaction, kisi, miktar, sign):
        if not await require_enabled(interaction):
            return
        if not interaction.user.guild_permissions.administrator and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("Bu komut için Yönetici yetkisi gerekli.", ephemeral=True)
            return
        stats = user_stats(interaction.guild.id, kisi.id)
        new_xp = max(0, stats["xp"] + sign * miktar)
        new_level = level_for_xp(new_xp)
        cursor.execute(
            "UPDATE xp_kullanicilar SET xp = ?, level = ? WHERE guild_id = ? AND user_id = ?",
            (new_xp, new_level, interaction.guild.id, kisi.id),
        )
        db.commit()
        if new_level != stats["level"]:
            await update_level_role(kisi, new_level)
        await interaction.response.send_message(
            f"{kisi.mention}: **{new_xp} XP**, seviye **{new_level}**."
        )

    @bot.tree.command(name="xp-ver", description="Bir üyeye XP ekler.")
    async def xp_ver(interaction: discord.Interaction, kisi: discord.Member, miktar: app_commands.Range[int, 1, 1000000]):
        await admin_xp_change(interaction, kisi, miktar, 1)

    @bot.tree.command(name="xp-al", description="Bir üyeden XP siler.")
    async def xp_al(interaction: discord.Interaction, kisi: discord.Member, miktar: app_commands.Range[int, 1, 1000000]):
        await admin_xp_change(interaction, kisi, miktar, -1)

    return award_message_xp
