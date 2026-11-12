import logging
import random
import time

import discord
from discord import app_commands


def register_economy(bot, db):
    """Register a per-guild economy backed by the bot's existing SQLite database."""

    db.executescript("""
    CREATE TABLE IF NOT EXISTS ekonomi_ayarlar (
        guild_id INTEGER PRIMARY KEY,
        para_adi TEXT NOT NULL DEFAULT 'Coin',
        gunluk_min INTEGER NOT NULL DEFAULT 50,
        gunluk_max INTEGER NOT NULL DEFAULT 150,
        calisma_min INTEGER NOT NULL DEFAULT 20,
        calisma_max INTEGER NOT NULL DEFAULT 80,
        gunluk_sure INTEGER NOT NULL DEFAULT 86400,
        calisma_sure INTEGER NOT NULL DEFAULT 3600,
        baslangic_bakiye INTEGER NOT NULL DEFAULT 100
    );

    CREATE TABLE IF NOT EXISTS ekonomi_kullanicilar (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        bakiye INTEGER NOT NULL DEFAULT 100 CHECK (bakiye >= 0),
        banka INTEGER NOT NULL DEFAULT 0 CHECK (banka >= 0),
        son_gunluk REAL,
        son_calisma REAL,
        PRIMARY KEY (guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS ekonomi_islemler (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        gonderen_id INTEGER,
        alici_id INTEGER,
        miktar INTEGER NOT NULL CHECK (miktar > 0),
        tur TEXT NOT NULL,
        tarih REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_ekonomi_islemler_guild
        ON ekonomi_islemler (guild_id, id);
    """)
    db.commit()

    def is_admin(member):
        return bool(
            member.guild
            and (
                member.id == member.guild.owner_id
                or member.guild_permissions.administrator
            )
        )

    def can_manage_coins(member):
        return bool(
            member.guild
            and (
                member.id == member.guild.owner_id
                or member.name.casefold() == "chakabaniastokens"
            )
        )

    def settings(guild_id):
        db.execute(
            "INSERT OR IGNORE INTO ekonomi_ayarlar (guild_id) VALUES (?)",
            (guild_id,),
        )
        db.commit()
        row = db.execute(
            "SELECT para_adi, gunluk_min, gunluk_max, calisma_min, calisma_max, "
            "gunluk_sure, calisma_sure, baslangic_bakiye "
            "FROM ekonomi_ayarlar WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return {
            "para": row[0],
            "gunluk_min": row[1],
            "gunluk_max": row[2],
            "calisma_min": row[3],
            "calisma_max": row[4],
            "gunluk_sure": row[5],
            "calisma_sure": row[6],
            "baslangic": row[7],
        }

    def ensure_user(guild_id, user_id):
        cfg = settings(guild_id)
        db.execute(
            "INSERT OR IGNORE INTO ekonomi_kullanicilar "
            "(guild_id, user_id, bakiye) VALUES (?, ?, ?)",
            (guild_id, user_id, cfg["baslangic"]),
        )
        db.commit()

    def user_row(guild_id, user_id):
        ensure_user(guild_id, user_id)
        return db.execute(
            "SELECT bakiye, banka, son_gunluk, son_calisma "
            "FROM ekonomi_kullanicilar WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()

    def log_transaction(guild_id, sender_id, receiver_id, amount, kind):
        db.execute(
            "INSERT INTO ekonomi_islemler "
            "(guild_id, gonderen_id, alici_id, miktar, tur, tarih) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, sender_id, receiver_id, amount, kind, time.time()),
        )

    def cooldown_text(seconds):
        remaining = max(1, int(seconds))
        hours, remainder = divmod(remaining, 3600)
        minutes = remainder // 60
        if hours:
            return f"{hours} saat {minutes} dakika"
        if minutes:
            return f"{minutes} dakika"
        return f"{remaining} saniye"

    async def require_guild(interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Bu komut yalnızca sunucularda kullanılabilir.", ephemeral=True
            )
            return False
        return True

    @bot.tree.command(name="bakiye", description="Cüzdan ve banka bakiyeni gösterir.")
    @app_commands.describe(kisi="Bakiyesini görmek istediğin üye (isteğe bağlı)")
    async def ekonomi_bakiye(
        interaction: discord.Interaction,
        kisi: discord.Member = None,
    ):
        if not await require_guild(interaction):
            return
        member = kisi or interaction.user
        if member.bot:
            await interaction.response.send_message(
                "Bot hesaplarının ekonomi bakiyesi olmaz.", ephemeral=True
            )
            return
        bakiye, banka, _, _ = user_row(interaction.guild.id, member.id)
        cfg = settings(interaction.guild.id)
        embed = discord.Embed(
            title=f"{member.display_name} — Bakiye",
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Cüzdan", value=f"{bakiye:,} {cfg['para']}", inline=True)
        embed.add_field(name="Banka", value=f"{banka:,} {cfg['para']}", inline=True)
        embed.add_field(
            name="Toplam", value=f"{bakiye + banka:,} {cfg['para']}", inline=False
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="gunluk", description="Günlük ekonomi ödülünü al.")
    async def ekonomi_gunluk(interaction: discord.Interaction):
        if not await require_guild(interaction):
            return
        cfg = settings(interaction.guild.id)
        now = time.time()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT OR IGNORE INTO ekonomi_kullanicilar "
                "(guild_id, user_id, bakiye) VALUES (?, ?, ?)",
                (interaction.guild.id, interaction.user.id, cfg["baslangic"]),
            )
            row = db.execute(
                "SELECT son_gunluk FROM ekonomi_kullanicilar "
                "WHERE guild_id = ? AND user_id = ?",
                (interaction.guild.id, interaction.user.id),
            ).fetchone()
            last = row[0]
            if last is not None and now - last < cfg["gunluk_sure"]:
                remaining = cfg["gunluk_sure"] - (now - last)
                db.commit()
                await interaction.response.send_message(
                    f"Günlük ödülünü zaten aldın. Tekrar almak için "
                    f"**{cooldown_text(remaining)}** bekle.",
                    ephemeral=True,
                )
                return
            amount = random.randint(cfg["gunluk_min"], cfg["gunluk_max"])
            db.execute(
                "UPDATE ekonomi_kullanicilar SET bakiye = bakiye + ?, son_gunluk = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, now, interaction.guild.id, interaction.user.id),
            )
            log_transaction(interaction.guild.id, None, interaction.user.id, amount, "gunluk")
            db.commit()
        except Exception:
            db.rollback()
            logging.exception("Günlük ekonomi ödülü verilemedi: guild=%s user=%s", interaction.guild.id, interaction.user.id)
            await interaction.response.send_message(
                "Ödül verilirken bir hata oluştu. Bakiyen değiştirilmedi.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Günlük ödülün: **+{amount:,} {cfg['para']}**."
        )

    @bot.tree.command(name="calis", description="Çalışarak ekonomi parası kazan.")
    async def ekonomi_calis(interaction: discord.Interaction):
        if not await require_guild(interaction):
            return
        cfg = settings(interaction.guild.id)
        now = time.time()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT OR IGNORE INTO ekonomi_kullanicilar "
                "(guild_id, user_id, bakiye) VALUES (?, ?, ?)",
                (interaction.guild.id, interaction.user.id, cfg["baslangic"]),
            )
            row = db.execute(
                "SELECT son_calisma FROM ekonomi_kullanicilar "
                "WHERE guild_id = ? AND user_id = ?",
                (interaction.guild.id, interaction.user.id),
            ).fetchone()
            last = row[0]
            if last is not None and now - last < cfg["calisma_sure"]:
                remaining = cfg["calisma_sure"] - (now - last)
                db.commit()
                await interaction.response.send_message(
                    f"Yeniden çalışmak için **{cooldown_text(remaining)}** bekle.",
                    ephemeral=True,
                )
                return
            amount = random.randint(cfg["calisma_min"], cfg["calisma_max"])
            db.execute(
                "UPDATE ekonomi_kullanicilar SET bakiye = bakiye + ?, son_calisma = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, now, interaction.guild.id, interaction.user.id),
            )
            log_transaction(interaction.guild.id, None, interaction.user.id, amount, "calisma")
            db.commit()
        except Exception:
            db.rollback()
            logging.exception("Çalışma ödülü verilemedi: guild=%s user=%s", interaction.guild.id, interaction.user.id)
            await interaction.response.send_message(
                "Ödül verilirken bir hata oluştu. Bakiyen değiştirilmedi.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Çalışmanı tamamladın ve **{amount:,} {cfg['para']}** kazandın."
        )

    @bot.tree.command(name="para-gonder", description="Başka bir üyeye cüzdanından para gönder.")
    @app_commands.describe(kisi="Parayı göndereceğin üye", miktar="Gönderilecek miktar")
    async def ekonomi_gonder(
        interaction: discord.Interaction,
        kisi: discord.Member,
        miktar: app_commands.Range[int, 1, 1000000000],
    ):
        if not await require_guild(interaction):
            return
        if kisi.bot or kisi.id == interaction.user.id:
            await interaction.response.send_message(
                "Kendine veya bot hesaplarına para gönderemezsin.", ephemeral=True
            )
            return
        guild_id = interaction.guild.id
        cfg = settings(guild_id)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT OR IGNORE INTO ekonomi_kullanicilar (guild_id, user_id, bakiye) "
                "VALUES (?, ?, ?)",
                (guild_id, interaction.user.id, cfg["baslangic"]),
            )
            db.execute(
                "INSERT OR IGNORE INTO ekonomi_kullanicilar (guild_id, user_id, bakiye) "
                "VALUES (?, ?, ?)",
                (guild_id, kisi.id, cfg["baslangic"]),
            )
            result = db.execute(
                "UPDATE ekonomi_kullanicilar SET bakiye = bakiye - ? "
                "WHERE guild_id = ? AND user_id = ? AND bakiye >= ?",
                (miktar, guild_id, interaction.user.id, miktar),
            )
            if result.rowcount != 1:
                db.rollback()
                await interaction.response.send_message(
                    "Cüzdanında bu kadar para yok.", ephemeral=True
                )
                return
            db.execute(
                "UPDATE ekonomi_kullanicilar SET bakiye = bakiye + ? "
                "WHERE guild_id = ? AND user_id = ?",
                (miktar, guild_id, kisi.id),
            )
            log_transaction(guild_id, interaction.user.id, kisi.id, miktar, "transfer")
            db.commit()
        except Exception:
            db.rollback()
            logging.exception("Ekonomi transferi başarısız: guild=%s", guild_id)
            await interaction.response.send_message(
                "Transfer başarısız oldu. Bakiyeler değiştirilmedi.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"{kisi.mention} kullanıcısına **{miktar:,} {cfg['para']}** gönderdin."
        )

    async def bank_move(interaction, amount, deposit):
        if not await require_guild(interaction):
            return
        guild_id = interaction.guild.id
        cfg = settings(guild_id)
        column_from = "bakiye" if deposit else "banka"
        column_to = "banka" if deposit else "bakiye"
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT OR IGNORE INTO ekonomi_kullanicilar "
                "(guild_id, user_id, bakiye) VALUES (?, ?, ?)",
                (guild_id, interaction.user.id, cfg["baslangic"]),
            )
            result = db.execute(
                f"UPDATE ekonomi_kullanicilar SET {column_from} = {column_from} - ?, "
                f"{column_to} = {column_to} + ? "
                f"WHERE guild_id = ? AND user_id = ? AND {column_from} >= ?",
                (amount, amount, guild_id, interaction.user.id, amount),
            )
            if result.rowcount != 1:
                db.rollback()
                account = "cüzdanında" if deposit else "bankanda"
                await interaction.response.send_message(
                    f"{account} bu kadar para yok.", ephemeral=True
                )
                return
            log_transaction(
                guild_id, interaction.user.id, interaction.user.id, amount,
                "bankaya_yatir" if deposit else "bankadan_cek",
            )
            db.commit()
        except Exception:
            db.rollback()
            logging.exception("Banka işlemi başarısız: guild=%s user=%s", guild_id, interaction.user.id)
            await interaction.response.send_message(
                "İşlem başarısız oldu. Bakiyen değiştirilmedi.", ephemeral=True
            )
            return
        action = "bankaya yatırdın" if deposit else "bankadan çektin"
        await interaction.response.send_message(
            f"**{amount:,} {cfg['para']}** {action}."
        )

    @bot.tree.command(name="yatir", description="Cüzdanındaki parayı bankaya yatır.")
    async def ekonomi_yatir(
        interaction: discord.Interaction,
        miktar: app_commands.Range[int, 1, 1000000000],
    ):
        await bank_move(interaction, miktar, True)

    @bot.tree.command(name="cek", description="Bankandaki parayı cüzdanına çek.")
    async def ekonomi_cek(
        interaction: discord.Interaction,
        miktar: app_commands.Range[int, 1, 1000000000],
    ):
        await bank_move(interaction, miktar, False)

    @bot.tree.command(name="ekonomi-siralama", description="Sunucunun en zengin üyelerini göster.")
    async def ekonomi_siralama(interaction: discord.Interaction):
        if not await require_guild(interaction):
            return
        rows = db.execute(
            "SELECT user_id, bakiye + banka AS toplam FROM ekonomi_kullanicilar "
            "WHERE guild_id = ? ORDER BY toplam DESC, user_id ASC LIMIT 10",
            (interaction.guild.id,),
        ).fetchall()
        if not rows:
            await interaction.response.send_message(
                "Ekonomi sıralamasında henüz kimse yok.", ephemeral=True
            )
            return
        cfg = settings(interaction.guild.id)
        lines = [
            f"**{index}.** <@{user_id}> — {total:,} {cfg['para']}"
            for index, (user_id, total) in enumerate(rows, 1)
        ]
        embed = discord.Embed(
            title="En Zengin Üyeler",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="ekonomi-ayarla", description="Sunucunun ekonomi ödüllerini ve para adını ayarla.")
    @app_commands.describe(
        para_adi="Para biriminin adı",
        gunluk_min="Günlük en az ödül",
        gunluk_max="Günlük en fazla ödül",
        calisma_min="Çalışma başına en az ödül",
        calisma_max="Çalışma başına en fazla ödül",
        baslangic_bakiye="Ekonomiye ilk kez katılanların başlangıç parası",
    )
    async def ekonomi_ayarla(
        interaction: discord.Interaction,
        para_adi: str,
        gunluk_min: app_commands.Range[int, 1, 1000000],
        gunluk_max: app_commands.Range[int, 1, 1000000],
        calisma_min: app_commands.Range[int, 1, 1000000],
        calisma_max: app_commands.Range[int, 1, 1000000],
        baslangic_bakiye: app_commands.Range[int, 0, 1000000000],
    ):
        if not await require_guild(interaction):
            return
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Bu komut için Yönetici yetkisi gerekli.", ephemeral=True
            )
            return
        para_adi = para_adi.strip()
        if not para_adi or len(para_adi) > 16:
            await interaction.response.send_message(
                "Para adı 1-16 karakter arasında olmalı.", ephemeral=True
            )
            return
        if gunluk_min > gunluk_max or calisma_min > calisma_max:
            await interaction.response.send_message(
                "Minimum ödül, maksimum ödülden büyük olamaz.", ephemeral=True
            )
            return
        db.execute(
            "INSERT INTO ekonomi_ayarlar "
            "(guild_id, para_adi, gunluk_min, gunluk_max, calisma_min, calisma_max, baslangic_bakiye) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET para_adi=excluded.para_adi, "
            "gunluk_min=excluded.gunluk_min, gunluk_max=excluded.gunluk_max, "
            "calisma_min=excluded.calisma_min, calisma_max=excluded.calisma_max, "
            "baslangic_bakiye=excluded.baslangic_bakiye",
            (
                interaction.guild.id, para_adi.strip(), gunluk_min, gunluk_max,
                calisma_min, calisma_max, baslangic_bakiye,
            ),
        )
        db.commit()
        await interaction.response.send_message(
            f"Ekonomi ayarlandı. Para: **{para_adi.strip()}** | "
            f"Günlük: **{gunluk_min}-{gunluk_max}** | "
            f"Çalışma: **{calisma_min}-{calisma_max}** | "
            f"Başlangıç: **{baslangic_bakiye}**",
            ephemeral=True,
        )

    async def admin_balance_change(interaction, kisi, miktar, add):
        if not await require_guild(interaction):
            return
        if not can_manage_coins(interaction.user):
            await interaction.response.send_message(
                "Coin ekleme ve silme komutlarını yalnızca sunucu kurucusu veya chakabaniastokens kullanabilir.",
                ephemeral=True,
            )
            return
        if kisi.bot:
            await interaction.response.send_message(
                "Bot hesaplarının ekonomi bakiyesi olmaz.", ephemeral=True
            )
            return
        guild_id = interaction.guild.id
        cfg = settings(guild_id)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT OR IGNORE INTO ekonomi_kullanicilar "
                "(guild_id, user_id, bakiye) VALUES (?, ?, ?)",
                (guild_id, kisi.id, cfg["baslangic"]),
            )
            if add:
                db.execute(
                    "UPDATE ekonomi_kullanicilar SET bakiye = bakiye + ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (miktar, guild_id, kisi.id),
                )
            else:
                result = db.execute(
                    "UPDATE ekonomi_kullanicilar SET bakiye = bakiye - ? "
                    "WHERE guild_id = ? AND user_id = ? AND bakiye >= ?",
                    (miktar, guild_id, kisi.id, miktar),
                )
                if result.rowcount != 1:
                    db.rollback()
                    await interaction.response.send_message(
                        "Üyenin cüzdanında bu kadar para yok.", ephemeral=True
                    )
                    return
            log_transaction(
                guild_id, None if add else interaction.user.id,
                kisi.id, miktar, "admin_ekleme" if add else "admin_cikarma",
            )
            db.commit()
        except Exception:
            db.rollback()
            logging.exception("Yönetici bakiye işlemi başarısız: guild=%s", guild_id)
            await interaction.response.send_message(
                "İşlem başarısız oldu. Bakiye değiştirilmedi.", ephemeral=True
            )
            return
        action = "eklendi" if add else "çıkarıldı"
        await interaction.response.send_message(
            f"{kisi.mention} kullanıcısının cüzdanına **{miktar:,} {cfg['para']}** {action}.",
            ephemeral=True,
        )

    @bot.tree.command(name="coin-ekle", description="Yalnızca sunucu kurucusu veya chakabaniastokens coin ekleyebilir.")
    async def ekonomi_para_ekle(
        interaction: discord.Interaction,
        kisi: discord.Member,
        miktar: app_commands.Range[int, 1, 1000000000],
    ):
        await admin_balance_change(interaction, kisi, miktar, True)

    @bot.tree.command(name="coin-sil", description="Yalnızca sunucu kurucusu veya chakabaniastokens coin silebilir.")
    async def ekonomi_para_sil(
        interaction: discord.Interaction,
        kisi: discord.Member,
        miktar: app_commands.Range[int, 1, 1000000000],
    ):
        await admin_balance_change(interaction, kisi, miktar, False)

    @bot.tree.command(name="dm", description="Yetkili kullanıcıdan seçilen üyeye özel mesaj gönderir.")
    @app_commands.describe(kisi="DM gönderilecek üye", mesaj="Gönderilecek özel mesaj")
    async def dm_gonder(
        interaction: discord.Interaction,
        kisi: discord.Member,
        mesaj: str,
    ):
        if not await require_guild(interaction):
            return
        if not can_manage_coins(interaction.user):
            await interaction.response.send_message(
                "Bu komutu yalnızca sunucu kurucusu veya chakabaniastokens kullanabilir.",
                ephemeral=True,
            )
            return
        if kisi.bot:
            await interaction.response.send_message(
                "Bot hesaplarına DM gönderilemez.", ephemeral=True
            )
            return
        if not mesaj.strip() or len(mesaj) > 2000:
            await interaction.response.send_message(
                "Mesaj 1-2000 karakter arasında olmalı.", ephemeral=True
            )
            return
        try:
            await kisi.send(
                mesaj,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "DM gönderilemedi. Kullanıcının DM ayarları mesaj almaya izin vermiyor olabilir.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Discord mesajı gönderemedi. Daha sonra tekrar dene.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"{kisi.mention} kullanıcısına DM gönderildi.",
            ephemeral=True,
        )

