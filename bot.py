import os
import io
import re
import asyncio
import json
import random
import logging
import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont
from xp_system import register_xp
from economy_system import register_economy
from market_system import register_market
from jail_system import register_jail
from anti_spam_system import register_anti_spam

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN ortam değişkeni ayarlanmamış.")

TURKEY = ZoneInfo("Europe/Istanbul")

db = sqlite3.connect("uyarilar.db")
cursor = db.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS bot_durum (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    bakim INTEGER NOT NULL DEFAULT 0
)
""")
cursor.execute("INSERT OR IGNORE INTO bot_durum (id, bakim) VALUES (1, 0)")
db.commit()
cursor.execute("SELECT bakim FROM bot_durum WHERE id = 1")
BAKIM_MODU = bool(cursor.fetchone()[0])
cursor.execute("""
CREATE TABLE IF NOT EXISTS uyarilar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    sebep TEXT NOT NULL,
    tarih TEXT NOT NULL
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS moderasyon_webhooklari (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    webhook_id INTEGER NOT NULL,
    webhook_url TEXT NOT NULL,
    aktif INTEGER NOT NULL DEFAULT 1
)
""")
db.commit()


cursor.execute("""
CREATE TABLE IF NOT EXISTS ticket_ayarlar (
    guild_id INTEGER PRIMARY KEY,
    transcript_channel_id INTEGER,
    staff_role_ids TEXT NOT NULL DEFAULT '[]'
)
""")
db.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS cekilisler (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    kanal_id INTEGER NOT NULL,
    mesaj_id INTEGER NOT NULL,
    bitis REAL NOT NULL,
    odul TEXT NOT NULL,
    kazanan_sayisi INTEGER NOT NULL DEFAULT 1,
    aktif INTEGER NOT NULL DEFAULT 1,
    kazananlar TEXT NOT NULL DEFAULT '[]'
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS cekilis_katilim (
    cekilis_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (cekilis_id, user_id)
)
""")
db.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS hedefler (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    kanal_id INTEGER NOT NULL,
    mesaj_id INTEGER NOT NULL,
    hedef INTEGER NOT NULL,
    tur TEXT NOT NULL,
    user_id INTEGER,
    sayac INTEGER NOT NULL DEFAULT 0,
    son_bar INTEGER NOT NULL DEFAULT -1,
    aktif INTEGER NOT NULL DEFAULT 1
)
""")
db.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS giris_ayarlar (
    guild_id INTEGER PRIMARY KEY,
    role_id INTEGER NOT NULL,
    allowed_channel_ids TEXT NOT NULL DEFAULT '[]'
)
""")
cursor.execute("CREATE TABLE IF NOT EXISTS giris_uye_rolleri (guild_id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL)")
db.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS log_ayarlar (
    guild_id INTEGER PRIMARY KEY,
    kanal_id INTEGER NOT NULL
)
""")
db.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS filtre_ayarlar (
    guild_id INTEGER PRIMARY KEY,
    reklam_mesaji TEXT NOT NULL DEFAULT '',
    kufur_mesaji TEXT NOT NULL DEFAULT ''
)
""")
db.commit()
# Glue kanallarinda sabit mesajin her yeni mesajdan sonra en alta tasinmasi.
cursor.execute("""
CREATE TABLE IF NOT EXISTS glue_ayarlar (
    guild_id INTEGER PRIMARY KEY,
    kanal_id INTEGER NOT NULL,
    mesaj TEXT NOT NULL,
    bot_mesaj_id INTEGER
)
""")
db.commit()
try:
    cursor.execute("ALTER TABLE glue_ayarlar ADD COLUMN baslik TEXT NOT NULL DEFAULT ''")
except sqlite3.OperationalError:
    pass
db.commit()

# Drop sistemi için eski veritabanlarına yeni alanları ekle.
try:
    cursor.execute("ALTER TABLE cekilisler ADD COLUMN tip TEXT NOT NULL DEFAULT 'cekilis'")
except sqlite3.OperationalError:
    pass
try:
    cursor.execute("ALTER TABLE cekilisler ADD COLUMN baslama REAL NOT NULL DEFAULT 0")
except sqlite3.OperationalError:
    pass
db.commit()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

def super_games_aktif(guild):
    if guild is None or guild.me is None:
        return False
    en_ust_rol = max(guild.roles, key=lambda role: role.position)
    return guild.me.top_role == en_ust_rol

def dinamik_prefix(bot, message):
    if message.guild and super_games_aktif(message.guild):
        return ("G+", "g+", "W+", "w+")
    return ("G!", "g!", "W!", "w!")

class MaintenanceTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Bakım komutu bakım sırasında da kullanılabilir; diğer slash komutları tüm sunucularda kapanır.
        if BAKIM_MODU and (interaction.command is None or interaction.command.name != "bakım"):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "🛠️ GAMESTURKEY şu anda bakım modunda. Komutlar tüm sunucularda geçici olarak kapalı.",
                    ephemeral=True
                )
            return False
        return True


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=dinamik_prefix, intents=intents, tree_cls=MaintenanceTree)

    async def setup_hook(self):
        await self.tree.sync()
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

        # Ticket butonlarını bot yeniden başlasa bile aktif tut.
        # TicketView ve TicketCloseView timeout=None ile kalıcı (persistent) view'dur.
        bot.add_view(TicketView())
        bot.add_view(TicketCloseView())
        bot.add_view(TicketDeleteView())
        bot.add_view(FiltrePanelView())
        bot.add_view(KayitView())

bot = Bot()

async def uwu_webhook_kur(guild):
    cursor.execute("SELECT channel_id, webhook_id FROM moderasyon_webhooklari WHERE guild_id = ?", (guild.id,))
    row = cursor.fetchone()
    if row:
        try:
            hook = await bot.fetch_webhook(row[1])
            kanal = guild.get_channel(row[0])
            if kanal is not None and hook.channel_id == kanal.id:
                return hook
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    kanallar = ([guild.system_channel] if guild.system_channel else [])
    kanallar.extend(ch for ch in guild.text_channels if ch not in kanallar)
    for kanal in kanallar:
        izin = kanal.permissions_for(guild.me)
        if not (izin.view_channel and izin.send_messages and izin.manage_webhooks):
            continue
        try:
            avatar = await bot.user.display_avatar.read() if bot.user else None
            hook = await kanal.create_webhook(name="UwU Bot", avatar=avatar, reason="GAMESTURKEY moderasyon webhook'u")
            cursor.execute(
                "INSERT OR REPLACE INTO moderasyon_webhooklari (guild_id, channel_id, webhook_id, webhook_url, aktif) VALUES (?, ?, ?, ?, COALESCE((SELECT aktif FROM moderasyon_webhooklari WHERE guild_id = ?), 1))",
                (guild.id, kanal.id, hook.id, hook.url, guild.id)
            )
            db.commit()
            return hook
        except (discord.Forbidden, discord.HTTPException):
            continue
    logging.warning("UwU Bot webhook kurulamadı: %s", guild.id)
    return None

async def moderasyon_yanit(ctx, icerik):
    if not ctx.guild:
        return await ctx.send(icerik)
    cursor.execute("SELECT aktif FROM moderasyon_webhooklari WHERE guild_id = ?", (ctx.guild.id,))
    row = cursor.fetchone()
    if row and row[0]:
        hook = await uwu_webhook_kur(ctx.guild)
        if hook:
            try:
                return await hook.send(
                    content=icerik,
                    username="UwU Bot",
                    avatar_url=bot.user.display_avatar.url if bot.user else None,
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logging.exception("UwU Bot webhook mesajı gönderilemedi.")
    return await ctx.send(icerik)

@bot.listen("on_guild_join")
async def uwu_yeni_sunucuya_kur(guild):
    await uwu_webhook_kur(guild)

register_jail(bot, db)
register_anti_spam(bot, db)
bot.super_games_durum = {}

# Yalnızca chakabaniastokens kullanıcısının görebileceği sunucu sayısı + davet bağlantısı.
OZEL_SUNUCU_SAYISI_KULLANICI = "chakabaniastokens"

async def sunucu_sayisi_cevabi(guild):
    if guild is None:
        return "Bu komut bir sunucunun içinde kullanılmalı."

    me = guild.me
    davet = None
    # Önce komutun yazıldığı kanalı dene, ardından diğer metin kanallarını.
    kanallar = []
    if isinstance(guild.get_channel(getattr(guild, "system_channel_id", None)), discord.TextChannel):
        kanallar.append(guild.get_channel(guild.system_channel_id))
    kanallar.extend(k for k in guild.text_channels if k not in kanallar)

    for kanal in kanallar:
        izinler = kanal.permissions_for(me)
        if izinler.view_channel and izinler.create_instant_invite:
            try:
                davet = await kanal.create_invite(max_age=0, max_uses=0, reason="Sunucu davet bağlantısı komutu")
                break
            except (discord.Forbidden, discord.HTTPException):
                continue

    link_satiri = f"**Bu sunucunun davet bağlantısı:** https://discord.gg/{davet.code}" if davet else "Bu sunucuda davet oluşturamadım. Botun bir metin kanalında **Davet Oluştur** izni olmalı."
    return f"**GAMESTURKEY sunucu sayısı:** {len(bot.guilds)}\n{link_satiri}"

@bot.tree.command(name="sunucu-sayisi", description="Botun sunucu sayısını ve bu sunucunun davet bağlantısını gösterir.")
async def sunucu_sayisi(interaction: discord.Interaction):
    if interaction.user.name.casefold() != OZEL_SUNUCU_SAYISI_KULLANICI:
        await interaction.response.send_message("Bu komutu kullanma iznin yok.", ephemeral=True)
        return
    await interaction.response.send_message(await sunucu_sayisi_cevabi(interaction.guild), ephemeral=True)

@bot.command(name="sunucu-sayisi")
async def prefix_sunucu_sayisi(ctx):
    if ctx.author.name.casefold() != OZEL_SUNUCU_SAYISI_KULLANICI:
        return await ctx.reply("Bu komutu kullanma iznin yok.", delete_after=5)
    await ctx.reply(await sunucu_sayisi_cevabi(ctx.guild), mention_author=False)

# Bu kullanıcı, sunucuda Yönetici yetkisi olmasa bile bot yönetici komutlarını kullanabilir.
# Discord kullanıcı adı değişirse buradaki değeri güncelle.
BOT_AUTHORIZED_USERNAME = "chakabaniastokens"

@bot.tree.command(name="bakım", description="GAMESTURKEY botunun tüm sunuculardaki komutlarını açar veya kapatır.")
@app_commands.describe(aktif="Açık: tüm sunucularda bakım modunu etkinleştirir; kapalı: komutları geri açar.")
async def bakim(interaction: discord.Interaction, aktif: bool):
    global BAKIM_MODU
    # Sunucu sahibi veya Yönetici yetkisi bu özel izni geçersiz kılamaz.
    if interaction.user.name.casefold() != BOT_AUTHORIZED_USERNAME.casefold():
        await interaction.response.send_message(
            "Bu komutu yalnızca chakabaniastokens kullanabilir.",
            ephemeral=True
        )
        return

    BAKIM_MODU = aktif
    cursor.execute("UPDATE bot_durum SET bakim = ? WHERE id = 1", (int(aktif),))
    db.commit()

    if aktif:
        mesaj = "🛠️ **Bakım modu açıldı.** GAMESTURKEY'in tüm sunucularındaki slash ve prefix komutları kapatıldı. Yalnızca /bakım komutuyla tekrar açılabilir."
    else:
        mesaj = "✅ **Bakım modu kapatıldı.** GAMESTURKEY'in tüm sunucularındaki komutlar yeniden açıldı."

    await interaction.response.send_message(mesaj, ephemeral=True)


@bot.check
async def prefix_bakim_kontrolu(ctx: commands.Context) -> bool:
    if not BAKIM_MODU:
        return True
    await ctx.reply(
        "🛠️ GAMESTURKEY şu anda bakım modunda. Komutlar tüm sunucularda geçici olarak kapalı.",
        mention_author=False,
        delete_after=8
    )
    return False


def bot_yetkili_mi(user):
    # Yetkili kullanıcı: Discord kullanıcı adı, global adı veya görünen adı eşleşebilir.
    yetkili_ad = BOT_AUTHORIZED_USERNAME.casefold()
    adlar = {
        str(getattr(user, "name", "")).casefold(),
        str(getattr(user, "global_name", "") or "").casefold(),
        str(getattr(user, "display_name", "")).casefold(),
    }
    return user.guild_permissions.administrator or yetkili_ad in adlar

bot.hedef_invite_cache = {}
bot.hedef_invite_lock = asyncio.Lock()

def hedef_bar(sayac, hedef, uzunluk=20):
    oran = min(1.0, max(0.0, sayac / hedef))
    dolu = int(oran * uzunluk)
    return "█" * dolu + "░" * (uzunluk - dolu)

def hedef_metni(guild, hedef, sayac):
    bar = hedef_bar(sayac, hedef[4])
    yuzde = min(100, int((sayac / hedef[4]) * 100))
    tur_metni = "💬 Mesaj Hedefi" if hedef[5] == "mesaj" else "👥 Davet Hedefi"
    kisi_id = hedef[6]
    kisi_satiri = f"👤 Kişi: <@{kisi_id}>" if kisi_id else "👥 Kişi: Herkes"
    durum = "🎉 HEDEF TAMAMLANDI!" if sayac >= hedef[4] else "🎯 Hedef devam ediyor"
    return (
        f"🎯 **Sunucu Hedefi**\n\n"
        f"{tur_metni}\n"
        f"{bar} **{min(sayac, hedef[4])} / {hedef[4]}**\n"
        f"**%{yuzde}**\n\n"
        f"{kisi_satiri}\n"
        f"{durum}"
    )

async def hedef_mesajini_guncelle(hedef):
    guild = bot.get_guild(hedef[1])
    if guild is None:
        return False
    kanal = guild.get_channel(hedef[2])
    if kanal is None:
        try:
            kanal = await bot.fetch_channel(hedef[2])
        except discord.HTTPException:
            return False
    try:
        mesaj = await kanal.fetch_message(hedef[3])
        await mesaj.edit(content=hedef_metni(guild, hedef, hedef[7]))
        return True
    except discord.HTTPException:
        return False

async def hedefleri_mesaj_icin_guncelle(message):
    if not message.guild or message.author.bot:
        return
    cursor.execute(
        "SELECT id, guild_id, kanal_id, mesaj_id, hedef, tur, user_id, sayac, son_bar, aktif "
        "FROM hedefler WHERE guild_id = ? AND tur = 'mesaj' AND aktif = 1",
        (message.guild.id,)
    )
    hedefler = cursor.fetchall()
    for hedef in hedefler:
        if hedef[6] is not None and hedef[6] != message.author.id:
            continue
        yeni_sayac = min(hedef[4], hedef[7] + 1)
        oran_bar = int((yeni_sayac / hedef[4]) * 20)
        cursor.execute(
            "UPDATE hedefler SET sayac = ?, son_bar = ? WHERE id = ?",
            (yeni_sayac, oran_bar, hedef[0])
        )
        db.commit()
        yeni_hedef = (*hedef[:7], yeni_sayac, oran_bar, hedef[9])
        if oran_bar != hedef[8] or yeni_sayac >= hedef[4]:
            if not await hedef_mesajini_guncelle(yeni_hedef):
                cursor.execute("UPDATE hedefler SET aktif = 0 WHERE id = ?", (hedef[0],))
                db.commit()

async def hedef_invite_cache_yenile(guild):
    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return
    bot.hedef_invite_cache[guild.id] = {
        invite.code: (invite.uses or 0, invite.inviter.id if invite.inviter else None)
        for invite in invites
    }

async def hedef_davet_sayacini_guncelle(guild, inviter_id):
    if inviter_id is None:
        return
    cursor.execute(
        "SELECT id, guild_id, kanal_id, mesaj_id, hedef, tur, user_id, sayac, son_bar, aktif "
        "FROM hedefler WHERE guild_id = ? AND tur = 'davet' AND aktif = 1",
        (guild.id,)
    )
    hedefler = cursor.fetchall()
    for hedef in hedefler:
        if hedef[6] is not None and hedef[6] != inviter_id:
            continue
        yeni_sayac = min(hedef[4], hedef[7] + 1)
        oran_bar = int((yeni_sayac / hedef[4]) * 20)
        cursor.execute(
            "UPDATE hedefler SET sayac = ?, son_bar = ? WHERE id = ?",
            (yeni_sayac, oran_bar, hedef[0])
        )
        db.commit()
        yeni_hedef = (*hedef[:7], yeni_sayac, oran_bar, hedef[9])
        if oran_bar != hedef[8] or yeni_sayac >= hedef[4]:
            if not await hedef_mesajini_guncelle(yeni_hedef):
                cursor.execute("UPDATE hedefler SET aktif = 0 WHERE id = ?", (hedef[0],))
                db.commit()

@bot.event
async def on_ready():
    for guild in bot.guilds:
        bot.super_games_durum[guild.id] = super_games_aktif(guild)
        await hedef_invite_cache_yenile(guild)

async def super_games_durumunu_kontrol_et(guild):
    yeni_durum = super_games_aktif(guild)
    eski_durum = bot.super_games_durum.get(guild.id)
    bot.super_games_durum[guild.id] = yeni_durum
    if eski_durum is None or eski_durum == yeni_durum:
        return

    mesaj = (
        "🎮 **SÜPER GAMES AKTİFLEŞTİRİLDİ!**\nArtık prefix komutları `G+` / `g+` ile kullanın."
        if yeni_durum else
        "⚙️ **SÜPER GAMES KAPATILDI!**\nArtık prefix komutları `G!` / `g!` ile kullanın."
    )
    kanallar = []
    if guild.system_channel:
        kanallar.append(guild.system_channel)
    kanallar.extend(ch for ch in guild.text_channels if ch not in kanallar)
    for kanal in kanallar:
        izin = kanal.permissions_for(guild.me)
        if izin.view_channel and izin.send_messages:
            try:
                await kanal.send(mesaj)
                break
            except (discord.Forbidden, discord.HTTPException):
                continue

@bot.event
async def on_guild_role_update(before, after):
    if before.position != after.position:
        await super_games_durumunu_kontrol_et(after.guild)

async def giris_ayarlarini_uygula(guild):
    cursor.execute(
        "SELECT role_id, allowed_channel_ids FROM giris_ayarlar WHERE guild_id = ?",
        (guild.id,)
    )
    row = cursor.fetchone()
    if not row:
        return None

    role = guild.get_role(row[0])
    if role is None:
        return None

    try:
        allowed_ids = set(json.loads(row[1] or "[]"))
    except (TypeError, json.JSONDecodeError):
        allowed_ids = set()

    # Doğrulanmamış rolü sunucudaki kanallardan gizle.
    for kanal in guild.channels:
        try:
            await kanal.set_permissions(role, view_channel=(kanal.id in allowed_ids))
        except (discord.Forbidden, discord.HTTPException):
            continue
    return role

async def yeni_uye_rolunu_ver(member):
    cursor.execute("SELECT role_id FROM giris_ayarlar WHERE guild_id = ?", (member.guild.id,))
    row = cursor.fetchone()
    if not row:
        return
    role = member.guild.get_role(row[0])
    if role is None:
        return
    if role >= member.guild.me.top_role:
        return
    try:
        await member.add_roles(role, reason="Yeni üye doğrulama sistemi")
    except (discord.Forbidden, discord.HTTPException):
        pass

@bot.event
async def on_member_join(member):
    guild = member.guild
    await yeni_uye_rolunu_ver(member)
    await kayit_giris_mesaji_gonder(member)
    async with bot.hedef_invite_lock:
        eski = bot.hedef_invite_cache.get(guild.id, {})
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return
        yeni = {
            invite.code: (invite.uses or 0, invite.inviter.id if invite.inviter else None)
            for invite in invites
        }
        inviter_id = None
        for code, (uses, owner_id) in yeni.items():
            eski_uses = eski.get(code, (0, owner_id))[0]
            if uses > eski_uses:
                inviter_id = owner_id
                break
        bot.hedef_invite_cache[guild.id] = yeni
        if inviter_id is not None:
            cursor.execute("INSERT OR IGNORE INTO terfi_istatistik (guild_id, user_id) VALUES (?, ?)", (guild.id, inviter_id))
            cursor.execute("UPDATE terfi_istatistik SET davet = davet + 1 WHERE guild_id = ? AND user_id = ?", (guild.id, inviter_id))
            db.commit()
        await hedef_davet_sayacini_guncelle(guild, inviter_id)

@bot.event
async def on_member_update(before, after):
    if before.roles == after.roles:
        return

    # Takip edilen en yüksek yetkili rolüne giriş/çıkış zamanını kaydet.
    try:
        ayar = terfi_ayarlarini_getir(after.guild.id)
        takip_rolleri = [after.guild.get_role(i) for i in ayar["yetkili"]]
        takip_rolleri = [r for r in takip_rolleri if r is not None]
        yeni_rol = max((r for r in after.roles if r in takip_rolleri), key=lambda r: r.position, default=None)
        cursor.execute(
            "SELECT role_id FROM terfi_rol_sureleri WHERE guild_id = ? AND user_id = ?",
            (after.guild.id, after.id)
        )
        eski_kayit = cursor.fetchone()
        if yeni_rol is None:
            cursor.execute("DELETE FROM terfi_rol_sureleri WHERE guild_id = ? AND user_id = ?", (after.guild.id, after.id))
        elif eski_kayit is None or eski_kayit[0] != yeni_rol.id:
            cursor.execute(
                "INSERT INTO terfi_rol_sureleri (guild_id, user_id, role_id, started_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET role_id=excluded.role_id, started_at=excluded.started_at",
                (after.guild.id, after.id, yeni_rol.id, datetime.now().timestamp())
            )
        db.commit()
    except Exception:
        logging.exception("Terfi rol süresi kaydedilemedi (guild_id=%s user_id=%s)", after.guild.id, after.id)

    cursor.execute("SELECT role_id FROM giris_ayarlar WHERE guild_id = ?", (after.guild.id,))
    row = cursor.fetchone()
    if not row:
        return

    waiting_role = after.guild.get_role(row[0])
    if waiting_role is None or waiting_role not in after.roles:
        return

    other_roles = [r for r in after.roles if r != after.guild.default_role and r.id != waiting_role.id]
    if not other_roles:
        return

    cursor.execute("SELECT role_id FROM giris_uye_rolleri WHERE guild_id = ?", (after.guild.id,))
    normal = cursor.fetchone()
    if not normal:
        return
    member_role = after.guild.get_role(normal[0])
    if member_role is None:
        return
    if member_role >= after.guild.me.top_role or waiting_role >= after.guild.me.top_role:
        return

    try:
        await after.remove_roles(waiting_role, reason="Yeni üye doğrulaması tamamlandı")
    except (discord.Forbidden, discord.HTTPException):
        pass

@bot.event
async def on_invite_create(invite):
    cache = bot.hedef_invite_cache.setdefault(invite.guild.id, {})
    cache[invite.code] = (invite.uses or 0, invite.inviter.id if invite.inviter else None)

@bot.event
async def on_invite_delete(invite):
    cache = bot.hedef_invite_cache.get(invite.guild.id, {})
    cache.pop(invite.code, None)


async def log_gonder(guild, embed):
    if guild is None:
        return
    cursor.execute("SELECT kanal_id FROM log_ayarlar WHERE guild_id = ?", (guild.id,))
    row = cursor.fetchone()
    if not row:
        return
    kanal = guild.get_channel(row[0])
    if kanal is None:
        try:
            kanal = await bot.fetch_channel(row[0])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
    try:
        await kanal.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass

async def log_yeni_mesaj(message):
    """Sunucudaki her kullanıcı mesajını log kanalına kaydeder."""
    if message.guild is None or message.author.bot:
        return

    icerik = (message.content or "").strip()
    embed = discord.Embed(
        title="💬 Yeni mesaj",
        description=(icerik[:4000] if icerik else "*Metin yok; ek, görsel, sticker veya başka içerik olabilir.*"),
        color=discord.Color.green(),
        timestamp=message.created_at
    )
    embed.add_field(
        name="Kullanıcı",
        value=f"{message.author.mention} (`{message.author.id}`)",
        inline=False
    )
    embed.add_field(name="Kanal", value=message.channel.mention, inline=False)
    embed.add_field(name="Mesaj bağlantısı", value=f"[Mesaja git]({message.jump_url})", inline=False)

    if message.attachments:
        ekler = "\n".join(
            f"[{discord.utils.escape_markdown(ek.filename)}]({ek.url})"
            for ek in message.attachments
        )
        embed.add_field(name="Ekler", value=ekler[:1000], inline=False)

    if message.stickers:
        embed.add_field(
            name="Sticker",
            value=", ".join(discord.utils.escape_markdown(sticker.name) for sticker in message.stickers)[:1000],
            inline=False
        )

    await log_gonder(message.guild, embed)

@bot.tree.command(name="log", description="Sunucunun kayıt kanalını ayarlar veya mevcut ayarı gösterir.")
@app_commands.describe(kanal="Logların gönderileceği kanal; boş bırakırsan mevcut ayarı gösterir.")
async def log_ayarla(interaction: discord.Interaction, kanal: discord.TextChannel = None):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisi gerekir.", ephemeral=True)
        return
    if kanal is None:
        cursor.execute("SELECT kanal_id FROM log_ayarlar WHERE guild_id = ?", (interaction.guild.id,))
        row = cursor.fetchone()
        if not row:
            await interaction.response.send_message("Log kanalı ayarlanmamış. `/log kanal:#kanal` kullan.", ephemeral=True)
            return
        mevcut = interaction.guild.get_channel(row[0])
        await interaction.response.send_message(f"Mevcut log kanalı: {mevcut.mention if mevcut else 'Kanal bulunamadı'}", ephemeral=True)
        return
    perms = kanal.permissions_for(interaction.guild.me)
    if not perms.view_channel or not perms.send_messages or not perms.embed_links:
        await interaction.response.send_message("Bu kanalda Görüntüle, Mesaj Gönder ve Bağlantı Yerleştir izinlerim olmalı.", ephemeral=True)
        return
    cursor.execute(
        "INSERT INTO log_ayarlar (guild_id, kanal_id) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET kanal_id = excluded.kanal_id",
        (interaction.guild.id, kanal.id)
    )
    db.commit()
    await interaction.response.send_message(f"Log kanalı {kanal.mention} olarak ayarlandı.", ephemeral=True)

@bot.event
async def on_message_edit(before, after):
    if before.guild is None or before.author.bot or before.content == after.content:
        return
    embed = discord.Embed(title="✏️ Mesaj düzenlendi", color=discord.Color.orange(), timestamp=datetime.now(TURKEY))
    embed.add_field(name="Kullanıcı", value=f"{before.author.mention} (`{before.author.id}`)", inline=False)
    embed.add_field(name="Kanal", value=before.channel.mention, inline=False)
    embed.add_field(name="Önceki mesaj", value=(before.content[:1000] or "*Metin yok*"), inline=False)
    embed.add_field(name="Yeni mesaj", value=(after.content[:1000] or "*Metin yok*"), inline=False)
    embed.add_field(name="Mesaj bağlantısı", value=f"[Mesaja git]({after.jump_url})", inline=False)
    await log_gonder(before.guild, embed)

@bot.event
async def on_raw_message_delete(payload):
    if payload.guild_id is None:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    message = payload.cached_message
    if message is None or message.author.bot:
        return
    embed = discord.Embed(title="🗑️ Mesaj silindi", color=discord.Color.red(), timestamp=datetime.now(TURKEY))
    embed.add_field(name="Kullanıcı", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
    embed.add_field(name="Kanal", value=f"<#{payload.channel_id}>", inline=False)
    embed.add_field(name="Mesaj içeriği", value=(message.content[:1500] or "*Metin yok veya önbellekte bulunamadı*"), inline=False)
    await log_gonder(guild, embed)

@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command):
    if interaction.guild is None:
        return
    embed = discord.Embed(title="⚡ Slash komut kullanıldı", color=discord.Color.blurple(), timestamp=datetime.now(TURKEY))
    embed.add_field(name="Kullanıcı", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
    embed.add_field(name="Komut", value=f"/{command.qualified_name}", inline=False)
    embed.add_field(name="Kanal", value=getattr(interaction.channel, "mention", "Bilinmiyor"), inline=False)
    await log_gonder(interaction.guild, embed)

# Otomatik reklam ve küfür filtresi
# Her sunucunun uyarı metni SQLite veritabanında ayrı saklanır.
LINK_PATTERN = re.compile(
    r"(?:https?://|www\.)\S+|(?:discord\.gg|discord(?:app)?\.com/invite)/\S+|"
    r"(?<![@\w])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?:/\S*)?",
    re.IGNORECASE
)

KUFUR_KELIMELERI = (
    "amk", "aq", "a.q", "amina koyayim", "amina koyim", "orospu", "orospu çocuğu",
    "orospu cocugu", "piç", "pic", "yarrak", "yarak", "sik", "sikerim", "sikeyim",
    "siktir", "siktirgit", "göt", "got", "götveren", "gotveren", "pezevenk",
    "ibne", "kahpe", "bok", "ananı", "anani", "ananı sik", "anani sik",
    "fuck", "fucker", "fucking", "motherfucker", "shit", "bullshit", "bitch",
    "asshole", "bastard", "dickhead", "dick", "piss off", "son of a bitch"
)
KUFUR_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(k) for k in sorted(KUFUR_KELIMELERI, key=len, reverse=True)) + r")(?!\w)",
    re.IGNORECASE
)

VARSAYILAN_REKLAM_UYARISI = "{user}, bu sunucuda bağlantı paylaşmak yasak. Mesajın silindi."
VARSAYILAN_KUFUR_UYARISI = "{user}, lütfen sohbet kurallarına uy ve küfür kullanma. Mesajın silindi."

def filtre_mesajlarini_getir(guild_id):
    cursor.execute(
        "SELECT reklam_mesaji, kufur_mesaji FROM filtre_ayarlar WHERE guild_id = ?",
        (guild_id,)
    )
    row = cursor.fetchone()
    if row is None:
        return VARSAYILAN_REKLAM_UYARISI, VARSAYILAN_KUFUR_UYARISI
    return (
        row[0] or VARSAYILAN_REKLAM_UYARISI,
        row[1] or VARSAYILAN_KUFUR_UYARISI
    )

def otomatik_moderasyondan_muaf(member):
    if not isinstance(member, discord.Member):
        return False
    izin = member.guild_permissions
    return (
        member.id == member.guild.owner_id
        or izin.administrator
        or izin.manage_messages
        or izin.moderate_members
        or izin.kick_members
        or izin.ban_members
    )

class FiltreUyariModal(discord.ui.Modal):
    def __init__(self, guild_id: int, tur: str, mevcut: str):
        baslik = "Küfür uyarısını düzenle" if tur == "kufur" else "Reklam uyarısını düzenle"
        super().__init__(title=baslik)
        self.guild_id = guild_id
        self.tur = tur
        self.metin = discord.ui.TextInput(
            label="Uyarı mesajı",
            style=discord.TextStyle.paragraph,
            placeholder="Örnek: {user}, bu sunucuda küfür yasak!",
            default=mevcut[:1000],
            required=True,
            max_length=1000
        )
        self.add_item(self.metin)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("Bu panel başka bir sunucuya ait.", ephemeral=True)
            return
        uye = interaction.user
        if not isinstance(uye, discord.Member) or not (
            uye.guild_permissions.manage_guild
            or uye.guild_permissions.administrator
            or uye.id == interaction.guild.owner_id
        ):
            await interaction.response.send_message("Bu ayarı değiştirmek için Sunucuyu Yönet yetkisi gerekli.", ephemeral=True)
            return

        reklam, kufur = filtre_mesajlarini_getir(self.guild_id)
        yeni_metin = str(self.metin).strip()
        if not yeni_metin:
            await interaction.response.send_message("Uyarı mesajı boş olamaz.", ephemeral=True)
            return
        if self.tur == "kufur":
            kufur = yeni_metin
        else:
            reklam = yeni_metin
        cursor.execute(
            "INSERT INTO filtre_ayarlar (guild_id, reklam_mesaji, kufur_mesaji) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET reklam_mesaji = excluded.reklam_mesaji, "
            "kufur_mesaji = excluded.kufur_mesaji",
            (self.guild_id, reklam, kufur)
        )
        db.commit()
        await interaction.response.send_message(
            "Uyarı mesajı kaydedildi. Sonraki ihlallerde kullanılacak. Kullanıcıyı etiketlemek için {user} yaz.",
            ephemeral=True
        )

class FiltrePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def yetki_kontrol(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Bu panel yalnızca sunucuda kullanılabilir.", ephemeral=True)
            return False
        uye = interaction.user
        if not isinstance(uye, discord.Member) or not (
            uye.guild_permissions.manage_guild
            or uye.guild_permissions.administrator
            or uye.id == interaction.guild.owner_id
        ):
            await interaction.response.send_message("Bu paneli kullanmak için Sunucuyu Yönet yetkisi gerekli.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Küfür uyarısını düzenle", style=discord.ButtonStyle.danger, custom_id="filtre_panel:kufur")
    async def kufur_duzenle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.yetki_kontrol(interaction):
            return
        _, kufur = filtre_mesajlarini_getir(interaction.guild.id)
        await interaction.response.send_modal(FiltreUyariModal(interaction.guild.id, "kufur", kufur))

    @discord.ui.button(label="Reklam uyarısını düzenle", style=discord.ButtonStyle.primary, custom_id="filtre_panel:reklam")
    async def reklam_duzenle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.yetki_kontrol(interaction):
            return
        reklam, _ = filtre_mesajlarini_getir(interaction.guild.id)
        await interaction.response.send_modal(FiltreUyariModal(interaction.guild.id, "reklam", reklam))

@bot.tree.command(name="filtre-panel", description="Reklam ve küfür filtresi uyarı mesajlarını düzenleme panelini gönderir.")
async def filtre_panel(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut yalnızca sunucuda kullanılabilir.", ephemeral=True)
        return
    uye = interaction.user
    if not isinstance(uye, discord.Member) or not (
        uye.guild_permissions.manage_guild
        or uye.guild_permissions.administrator
        or uye.id == interaction.guild.owner_id
    ):
        await interaction.response.send_message("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.", ephemeral=True)
        return
    reklam, kufur = filtre_mesajlarini_getir(interaction.guild.id)
    embed = discord.Embed(
        title="GAMESTURKEY • Filtre Ayarları",
        description=(
            "Düğmelerle uyarı metinlerini düzenleyebilirsin. Kullanıcıyı etiketlemek için **{user}** yaz. "
            "Ayarlar sunucuya özel kaydedilir ve bot yeniden başlasa da korunur."
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(name="Reklam uyarısı", value=reklam[:1024], inline=False)
    embed.add_field(name="Küfür uyarısı", value=kufur[:1024], inline=False)
    await interaction.response.send_message(embed=embed, view=FiltrePanelView())

@bot.tree.command(name="glue", description="Secilen kanalda sabit embed mesajini en altta tutar.")
@app_commands.describe(kanal="Glue mesajinin duracagi kanal", baslik="Embed basligi", aciklama="Embed aciklamasi")
async def glue_ayarla(interaction: discord.Interaction, kanal: discord.TextChannel, baslik: app_commands.Range[str, 1, 256], aciklama: app_commands.Range[str, 1, 4000]):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanilabilir.", ephemeral=True)
        return
    if not (interaction.user.id == interaction.guild.owner_id or interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild):
        await interaction.response.send_message("Bu komut icin Sunucuyu Yonet yetkisi gerekir.", ephemeral=True)
        return
    izin = kanal.permissions_for(interaction.guild.me)
    if not izin.view_channel or not izin.send_messages or not izin.read_message_history or not izin.manage_messages or not izin.embed_links:
        await interaction.response.send_message("Botun secilen kanalda Kanalı Goruntule, Mesaj Gonder, Mesaj Gecmisini Oku, Mesajlari Yonet ve Baglantilari Gom izinleri olmali.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cursor.execute("SELECT kanal_id, bot_mesaj_id FROM glue_ayarlar WHERE guild_id = ?", (interaction.guild.id,))
    eski = cursor.fetchone()
    if eski and eski[1]:
        try:
            eski_kanal = interaction.guild.get_channel(eski[0])
            if eski_kanal:
                await (await eski_kanal.fetch_message(eski[1])).delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    embed = discord.Embed(title=baslik, description=aciklama, color=discord.Color.blurple())
    try:
        yeni = await kanal.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        await interaction.followup.send("Glue embed mesaji gonderilemedi. Kanal izinlerini kontrol et.", ephemeral=True)
        return
    cursor.execute("INSERT INTO glue_ayarlar (guild_id, kanal_id, mesaj, bot_mesaj_id, baslik) VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET kanal_id=excluded.kanal_id, mesaj=excluded.mesaj, bot_mesaj_id=excluded.bot_mesaj_id, baslik=excluded.baslik", (interaction.guild.id, kanal.id, aciklama, yeni.id, baslik))
    db.commit()
    await interaction.followup.send(f"Glue embed aktif: {kanal.mention} kanalinda her yeni mesajdan sonra en alta tasinacak.", ephemeral=True)

@bot.tree.command(name="glue-kapat", description="Bu sunucudaki Glue mesajini kapatir.")
async def glue_kapat(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanilabilir.", ephemeral=True)
        return
    if not (interaction.user.id == interaction.guild.owner_id or interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild):
        await interaction.response.send_message("Bu komut icin Sunucuyu Yonet yetkisi gerekir.", ephemeral=True)
        return
    cursor.execute("SELECT kanal_id, bot_mesaj_id FROM glue_ayarlar WHERE guild_id = ?", (interaction.guild.id,))
    glue = cursor.fetchone()
    if not glue:
        await interaction.response.send_message("Bu sunucuda aktif Glue ayari yok.", ephemeral=True)
        return
    try:
        hedef_kanal = interaction.guild.get_channel(glue[0])
        if hedef_kanal and glue[1]:
            await (await hedef_kanal.fetch_message(glue[1])).delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    cursor.execute("DELETE FROM glue_ayarlar WHERE guild_id = ?", (interaction.guild.id,))
    db.commit()
    await interaction.response.send_message("Glue kapatildi.", ephemeral=True)


@bot.event
async def on_message(message):
    if message.guild and not message.author.bot:
        cursor.execute("SELECT kanal_id, mesaj, bot_mesaj_id FROM glue_ayarlar WHERE guild_id = ?", (message.guild.id,))
        glue = cursor.fetchone()
        if glue and message.channel.id == glue[0]:
            try:
                if glue[2]:
                    try:
                        onceki = await message.channel.fetch_message(glue[2])
                        await onceki.delete()
                    except discord.NotFound:
                        pass
                cursor.execute("SELECT baslik FROM glue_ayarlar WHERE guild_id = ?", (message.guild.id,))
                baslik_row = cursor.fetchone()
                embed = discord.Embed(title=(baslik_row[0] if baslik_row and baslik_row[0] else ' '), description=glue[1], color=discord.Color.blurple())
                yeni_glue = await message.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                cursor.execute("UPDATE glue_ayarlar SET bot_mesaj_id = ? WHERE guild_id = ?", (yeni_glue.id, message.guild.id))
                db.commit()
            except (discord.Forbidden, discord.HTTPException):
                logging.exception("Glue mesaji alta tasinamadi (guild_id=%s)", message.guild.id)
        cursor.execute("INSERT OR IGNORE INTO terfi_istatistik (guild_id, user_id) VALUES (?, ?)", (message.guild.id, message.author.id))
        cursor.execute("UPDATE terfi_istatistik SET mesaj = mesaj + 1 WHERE guild_id = ? AND user_id = ?", (message.guild.id, message.author.id))
        db.commit()
        # Filtre mesajı silse bile önce loga kaydedilir.
        await log_yeni_mesaj(message)
        if not otomatik_moderasyondan_muaf(message.author):
            icerik = message.content or ""
            sebep = None
            if LINK_PATTERN.search(icerik):
                sebep = "reklam bağlantısı"
            elif KUFUR_PATTERN.search(icerik):
                sebep = "küfür"
            if sebep:
                try:
                    await message.delete()
                    reklam_mesaji, kufur_mesaji = filtre_mesajlarini_getir(message.guild.id)
                    sablon = reklam_mesaji if sebep == "reklam bağlantısı" else kufur_mesaji
                    uyari = sablon.replace("{user}", message.author.mention)
                    await message.channel.send(
                        uyari,
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                        delete_after=10
                    )
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass
                return
        await hedefleri_mesaj_icin_guncelle(message)
        await xp_message_award(message)
    await bot.process_commands(message)

@bot.tree.command(name="hedef", description="Mesaj veya davet hedefi oluşturur.")
@app_commands.describe(
    toplam="Ulaşılacak toplam sayı",
    tur="Hedef türü",
    kisi="Boş bırakırsan herkes için geçerli olur"
)
@app_commands.choices(tur=[
    app_commands.Choice(name="Mesaj", value="mesaj"),
    app_commands.Choice(name="Davet", value="davet")
])
async def hedef(interaction, toplam: app_commands.Range[int, 1, 1000000], tur: app_commands.Choice[str], kisi: discord.Member = None):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.", ephemeral=True)
        return
    if tur.value == "davet" and not interaction.guild.me.guild_permissions.manage_guild:
        await interaction.response.send_message("Davet hedefleri için botun Sunucuyu Yönet yetkisine ihtiyacı var.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    cursor.execute(
        "INSERT INTO hedefler (guild_id, kanal_id, mesaj_id, hedef, tur, user_id, sayac, son_bar, aktif) "
        "VALUES (?, ?, 0, ?, ?, ?, 0, 0, 1)",
        (interaction.guild.id, interaction.channel.id, toplam, tur.value, kisi.id if kisi else None)
    )
    db.commit()
    hedef_id = cursor.lastrowid

    try:
        mesaj = await interaction.channel.send(
            hedef_metni(
                interaction.guild,
                (hedef_id, interaction.guild.id, interaction.channel.id, 0, toplam, tur.value, kisi.id if kisi else None, 0, 0, 1),
                0
            )
        )
    except discord.HTTPException:
        cursor.execute("DELETE FROM hedefler WHERE id = ?", (hedef_id,))
        db.commit()
        await interaction.followup.send("Hedef mesajı gönderilemedi.", ephemeral=True)
        return

    cursor.execute("UPDATE hedefler SET mesaj_id = ? WHERE id = ?", (mesaj.id, hedef_id))
    db.commit()
    await interaction.followup.send(f"Hedef oluşturuldu: {mesaj.jump_url}", ephemeral=True)

@bot.tree.command(name="giris-ayarla", description="Yeni üyelerin bekleme ve normal rol sistemini ayarlar.")
@app_commands.describe(
    bekleme_rol="Yeni gelenlere verilecek geçici rol",
    uye_rol="Doğrulamadan sonra verilecek normal üye rolü",
    kanal1="Yeni üyelerin görebileceği kanal",
    kanal2="İkinci açık kanal",
    kanal3="Üçüncü açık kanal",
    kanal4="Dördüncü açık kanal",
    kanal5="Beşinci açık kanal"
)
async def giris_ayarla(
    interaction,
    bekleme_rol: discord.Role,
    uye_rol: discord.Role,
    kanal1: discord.TextChannel,
    kanal2: discord.TextChannel = None,
    kanal3: discord.TextChannel = None,
    kanal4: discord.TextChannel = None,
    kanal5: discord.TextChannel = None
):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
        return
    if not interaction.guild.me.guild_permissions.manage_roles or not interaction.guild.me.guild_permissions.manage_channels:
        await interaction.response.send_message("Botun Rolleri Yönet ve Kanalları Yönet yetkileri olmalı.", ephemeral=True)
        return
    if bekleme_rol >= interaction.guild.me.top_role or uye_rol >= interaction.guild.me.top_role:
        await interaction.response.send_message("İki rol de botun en yüksek rolünün altında olmalı.", ephemeral=True)
        return
    if bekleme_rol == interaction.guild.default_role or uye_rol == interaction.guild.default_role:
        await interaction.response.send_message("@everyone rolü kullanılamaz.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    kanallar = [k for k in [kanal1, kanal2, kanal3, kanal4, kanal5] if k is not None]
    allowed_ids = list(dict.fromkeys(k.id for k in kanallar))

    cursor.execute(
        "INSERT INTO giris_ayarlar (guild_id, role_id, allowed_channel_ids) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id, allowed_channel_ids=excluded.allowed_channel_ids",
        (interaction.guild.id, bekleme_rol.id, json.dumps(allowed_ids))
    )
    db.commit()
    # Normal üye rolünü ayrı tabloda tut.
    cursor.execute("CREATE TABLE IF NOT EXISTS giris_uye_rolleri (guild_id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL)")
    cursor.execute(
        "INSERT INTO giris_uye_rolleri (guild_id, role_id) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id",
        (interaction.guild.id, uye_rol.id)
    )
    db.commit()

    await giris_ayarlarini_uygula(interaction.guild)
    await interaction.followup.send(
        f"Yeni üye sistemi ayarlandı.\nBekleme: {bekleme_rol.mention}\nÜye: {uye_rol.mention}\n"
        f"Açık kanallar: {', '.join(k.mention for k in kanallar)}"
    )

@bot.tree.command(name="kayit", description="Bir üyeyi doğrular ve normal üye rolünü verir.")
@app_commands.describe(kisi="Doğrulanacak üye")
async def kayit(interaction, kisi: discord.Member):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Rolleri Yönet yetkisine sahip olmalısın.", ephemeral=True)
        return
    cursor.execute("SELECT role_id FROM giris_ayarlar WHERE guild_id = ?", (interaction.guild.id,))
    bekleme = cursor.fetchone()
    cursor.execute("SELECT role_id FROM giris_uye_rolleri WHERE guild_id = ?", (interaction.guild.id,))
    uye = cursor.fetchone()
    if not bekleme or not uye:
        await interaction.response.send_message("Önce /giris-ayarla ile sistemi kurmalısın.", ephemeral=True)
        return
    bekleme_rol = interaction.guild.get_role(bekleme[0])
    uye_rol = interaction.guild.get_role(uye[0])
    if not bekleme_rol or not uye_rol:
        await interaction.response.send_message("Ayarlanan roller bulunamadı.", ephemeral=True)
        return
    if uye_rol >= interaction.guild.me.top_role or bekleme_rol >= interaction.guild.me.top_role:
        await interaction.response.send_message("Roller botun en yüksek rolünün altında olmalı.", ephemeral=True)
        return
    try:
        if bekleme_rol in kisi.roles:
            await kisi.remove_roles(bekleme_rol, reason=f"Doğrulama: {interaction.user}")
        await kisi.add_roles(uye_rol, reason=f"Doğrulama: {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("Rol verilemedi. Botun rol sırasını kontrol et.", ephemeral=True)
        return
    await interaction.response.send_message(f"{kisi.mention} doğrulandı ve {uye_rol.mention} rolü verildi.", ephemeral=True)

async def ortak_uyari(guild, kisi, moderator, sebep):
    tarih = datetime.now(TURKEY).strftime("%d.%m.%Y %H:%M:%S")
    cursor.execute(
        "INSERT INTO uyarilar (guild_id,user_id,moderator_id,sebep,tarih) VALUES (?,?,?,?,?)",
        (guild.id, kisi.id, moderator.id, sebep, tarih)
    )
    db.commit()
    uyari_id = cursor.lastrowid
    try:
        await kisi.send(f"""**GAMESTURKEY — Uyarı**

Sunucu: **{guild.name}**

Uyarı No: **#{uyari_id}**
Tarih: **{tarih}**
Sebep: **{sebep}**

Bu mesaj otomatik olarak gönderilmiştir.""")
        dm = "Kullanıcıya DM gönderildi."
    except discord.Forbidden:
        dm = "Kullanıcının DM'leri kapalı olduğu için DM gönderilemedi."
    return uyari_id, tarih, dm

async def ortak_mute(guild, kisi, moderator, sure, sebep):
    if kisi.id == moderator.id:
        return False, "Kendine zaman aşımı uygulayamazsın."
    if kisi.top_role >= guild.me.top_role:
        return False, "Bu kullanıcının rolü botun en yüksek rolüyle aynı veya daha yüksek."
    m = re.fullmatch(r"(\d+)(s|m|h|d)", sure.lower())
    if not m:
        return False, "Geçersiz süre. Örnek: 30s, 10m, 2h, 1d, 7d"
    n, u = int(m.group(1)), m.group(2)
    saniye = n if u == "s" else n * 60 if u == "m" else n * 3600 if u == "h" else n * 86400
    if not 1 <= saniye <= 28 * 86400:
        return False, "Zaman aşımı 1 saniye ile 28 gün arasında olmalı."
    try:
        await kisi.timeout(timedelta(seconds=saniye), reason=sebep)
    except discord.Forbidden:
        return False, "Discord zaman aşımı uygulamama izin vermedi. Botun yetkisini ve rol sırasını kontrol et."
    bitis = datetime.now(TURKEY) + timedelta(seconds=saniye)
    return True, bitis.strftime("%d.%m.%Y %H:%M:%S")

async def ortak_kick(guild, kisi, moderator, sebep):
    if kisi.id == moderator.id or (kisi.top_role >= moderator.top_role and moderator.id != guild.owner_id) or kisi.top_role >= guild.me.top_role:
        return False, "Bu kullanıcıyı atmaya yetkim yok."
    try:
        await kisi.kick(reason=sebep)
    except discord.Forbidden:
        return False, "Kullanıcı atılamadı. Botun yetkisini ve rol sırasını kontrol et."
    return True, ""

async def ortak_ban(guild, kisi, moderator, sebep):
    if kisi.id == moderator.id or (kisi.top_role >= moderator.top_role and moderator.id != guild.owner_id) or kisi.top_role >= guild.me.top_role:
        return False, "Bu kullanıcıyı yasaklamaya yetkim yok."
    try:
        await kisi.ban(reason=sebep, delete_message_seconds=0)
    except discord.Forbidden:
        return False, "Kullanıcı yasaklanamadı. Botun yetkisini ve rol sırasını kontrol et."
    return True, ""

async def ortak_rol(guild, kisi, rol, ver=True):
    if rol >= guild.me.top_role:
        return False, "Bu rol botun en yüksek rolüyle aynı veya daha yüksek." if ver else "Rol alınamadı. Botun rol sırasını kontrol et."
    try:
        await (kisi.add_roles(rol) if ver else kisi.remove_roles(rol))
    except discord.Forbidden:
        return False, "Rol verilemedi. Botun rol sırasını kontrol et." if ver else "Rol alınamadı. Botun rol sırasını kontrol et."
    return True, ""

async def ortak_nick(kisi, takma_ad):
    try:
        await kisi.edit(nick=takma_ad)
    except discord.Forbidden:
        return False
    return True

async def ortak_yavasmod(kanal, saniye):
    try:
        await kanal.edit(slowmode_delay=saniye)
    except discord.Forbidden:
        return False
    return True

async def ortak_kilit(kanal, guild, ac=False):
    ow = kanal.overwrites_for(guild.default_role)
    ow.send_messages = None if ac else False
    try:
        await kanal.set_permissions(guild.default_role, overwrite=ow)
    except discord.Forbidden:
        return False
    return True


def sureyi_saniyeye_cevir(sure):
    m = re.fullmatch(r"(\d+)(s|m|h|d)", sure.lower())
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    return n if u == "s" else n * 60 if u == "m" else n * 3600 if u == "h" else n * 86400

class GiveawayView(discord.ui.View):
    def __init__(self, cekilis_id):
        super().__init__(timeout=None)
        self.cekilis_id = cekilis_id
        button = discord.ui.Button(label="Katıl", style=discord.ButtonStyle.green, emoji="🎉", custom_id=f"giveaway_join:{cekilis_id}")
        button.callback = self.katil
        self.add_item(button)

    async def katil(self, interaction: discord.Interaction):
        cursor.execute("SELECT aktif, tip FROM cekilisler WHERE id = ?", (self.cekilis_id,))
        row = cursor.fetchone()
        if not row or not row[0] or row[1] != "cekilis":
            await interaction.response.send_message("Bu çekiliş sona ermiş.", ephemeral=True)
            return
        try:
            cursor.execute(
                "INSERT INTO cekilis_katilim (cekilis_id, user_id) VALUES (?, ?)",
                (self.cekilis_id, interaction.user.id)
            )
            db.commit()
            await interaction.response.send_message("Çekilişe katıldın!", ephemeral=True)
        except sqlite3.IntegrityError:
            await interaction.response.send_message("Zaten çekilişe katıldın.", ephemeral=True)


class DropView(discord.ui.View):
    def __init__(self, drop_id, aktif=False):
        super().__init__(timeout=None)
        self.drop_id = drop_id
        button = discord.ui.Button(
            label="YAKALA",
            style=discord.ButtonStyle.green,
            custom_id=f"drop_catch:{drop_id}",
            disabled=not aktif
        )
        button.callback = self.yakala
        self.add_item(button)

    async def yakala(self, interaction: discord.Interaction):
        cursor.execute(
            "SELECT aktif, tip, baslama, odul, kanal_id FROM cekilisler WHERE id = ?",
            (self.drop_id,)
        )
        row = cursor.fetchone()
        if not row or not row[0] or row[1] != "drop":
            await interaction.response.send_message("Bu drop sona ermiş.", ephemeral=True)
            return

        if row[2] > datetime.now().timestamp():
            kalan = max(1, int(row[2] - datetime.now().timestamp()))
            await interaction.response.send_message(
                f"Drop henüz başlamadı. **{kalan} saniye** kaldı.",
                ephemeral=True
            )
            return

        # Aynı anda basan kişilerde yalnızca ilk başarılı UPDATE kazanır.
        cursor.execute(
            "UPDATE cekilisler SET aktif = 0, kazananlar = ? WHERE id = ? AND aktif = 1",
            (json.dumps([interaction.user.id]), self.drop_id)
        )
        if cursor.rowcount != 1:
            db.commit()
            await interaction.response.send_message("Geç kaldın, drop zaten yakalandı.", ephemeral=True)
            return
        db.commit()

        kanal = bot.get_channel(row[4])
        if kanal is None:
            try:
                kanal = await bot.fetch_channel(row[4])
            except discord.HTTPException:
                kanal = None

        await interaction.response.send_message("Drop'u yakaladın!", ephemeral=True)
        if kanal:
            try:
                mesaj = await kanal.fetch_message(
                    (cursor.execute("SELECT mesaj_id FROM cekilisler WHERE id = ?", (self.drop_id,)).fetchone() or [0])[0]
                )
                await mesaj.edit(
                    content=f"DROP #{self.drop_id} — YAKALANDI!\n\nÖdül: **{row[3]}**\nKazanan: {interaction.user.mention}",
                    view=None
                )
                await kanal.send(f"DROP #{self.drop_id}: Tebrikler {interaction.user.mention}! **{row[3]}** kazandın!")
            except discord.HTTPException:
                pass


async def cekilis_mesaji_guncelle(cekilis_id, bitis):
    cursor.execute("SELECT kanal_id, mesaj_id, odul, kazanan_sayisi FROM cekilisler WHERE id = ?", (cekilis_id,))
    row = cursor.fetchone()
    if not row:
        return
    kanal = bot.get_channel(row[0])
    if kanal is None:
        try:
            kanal = await bot.fetch_channel(row[0])
        except discord.HTTPException:
            return
    try:
        mesaj = await kanal.fetch_message(row[1])
        kalan = max(0, int(bitis - datetime.now().timestamp()))
        if kalan:
            await mesaj.edit(content=f"🎉 **ÇEKİLİŞ #{cekilis_id}**\n\nÖdül: **{row[2]}**\nKazanan sayısı: **{row[3]}**\n{bitis_metni}\n\nKatılmak için aşağıdaki **Katıl** butonuna bas!", view=GiveawayView(cekilis_id))
    except discord.HTTPException:
        pass

async def cekilisi_bitir(cekilis_id):
    cursor.execute("SELECT guild_id, kanal_id, mesaj_id, odul, kazanan_sayisi, aktif FROM cekilisler WHERE id = ?", (cekilis_id,))
    row = cursor.fetchone()
    if not row or not row[5]:
        return
    cursor.execute("SELECT user_id FROM cekilis_katilim WHERE cekilis_id = ?", (cekilis_id,))
    katilanlar = [x[0] for x in cursor.fetchall()]
    kazanan_sayisi = min(row[4], len(katilanlar))
    kazananlar = random.sample(katilanlar, kazanan_sayisi) if kazanan_sayisi else []
    cursor.execute("UPDATE cekilisler SET aktif = 0, kazananlar = ? WHERE id = ?", (json.dumps(kazananlar), cekilis_id))
    db.commit()

    kanal = bot.get_channel(row[1])
    if kanal is None:
        try:
            kanal = await bot.fetch_channel(row[1])
        except discord.HTTPException:
            return
    try:
        mesaj = await kanal.fetch_message(row[2])
        await mesaj.edit(content=f"🎉 **ÇEKİLİŞ #{cekilis_id} — SONA ERDİ**\n\nÖdül: **{row[3]}**\nKatılan: **{len(katilanlar)}**\n\nKazananlar: " + (", ".join(f"<@{x}>" for x in kazananlar) if kazananlar else "Katılan olmadı."), view=None)
        if kazananlar:
            await kanal.send(f"🎉 Tebrikler {', '.join(f'<@{x}>' for x in kazananlar)}! **{row[3]}** kazandınız!")
        else:
            await kanal.send(f"Çekiliş #{cekilis_id} sona erdi ama katılan olmadı.")
    except discord.HTTPException:
        pass

async def cekilis_dongusu():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now().timestamp()

        # Süresi dolan normal çekilişleri bitir.
        cursor.execute("SELECT id, bitis FROM cekilisler WHERE aktif = 1 AND tip = 'cekilis'")
        for cekilis_id, bitis in cursor.fetchall():
            if bitis > 0 and bitis <= now:
                await cekilisi_bitir(cekilis_id)

        # Bekleyen dropları başlat.
        cursor.execute("SELECT id, kanal_id, mesaj_id, odul, baslama FROM cekilisler WHERE aktif = 1 AND tip = 'drop' AND baslama > 0 AND baslama <= ?", (now,))
        for drop_id, kanal_id, mesaj_id, odul, baslama in cursor.fetchall():
            kanal = bot.get_channel(kanal_id)
            if kanal is None:
                try:
                    kanal = await bot.fetch_channel(kanal_id)
                except discord.HTTPException:
                    continue
            try:
                mesaj = await kanal.fetch_message(mesaj_id)
                await mesaj.edit(
                    content=f"**DROP #{drop_id} BAŞLADI!**\n\nÖdül: **{odul}**\n\nİlk yakalayan kazanır!\n\n[YAKALA] butonuna bas!",
                    view=DropView(drop_id, aktif=True)
                )
            except discord.HTTPException:
                pass
            cursor.execute("UPDATE cekilisler SET baslama = 0 WHERE id = ?", (drop_id,))
            db.commit()

        await asyncio.sleep(2)


async def aktif_cekilisleri_yukle():
    cursor.execute("SELECT id, tip, baslama FROM cekilisler WHERE aktif = 1")
    for item_id, tip, baslama in cursor.fetchall():
        if tip == "drop":
            bot.add_view(DropView(item_id, aktif=(baslama == 0 or baslama <= datetime.now().timestamp())))
        else:
            bot.add_view(GiveawayView(item_id))


async def ticket_ayarini_getir(guild_id):
    cursor.execute("SELECT transcript_channel_id, staff_role_ids FROM ticket_ayarlar WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    if not row:
        return None, []
    try:
        roller = json.loads(row[1] or "[]")
    except (TypeError, json.JSONDecodeError):
        roller = []
    return row[0], roller

async def ticket_transkript_olustur(kanal, kapatan):
    mesajlar = []
    async for mesaj in kanal.history(limit=None, oldest_first=True):
        zaman = mesaj.created_at.astimezone(TURKEY).strftime("%d.%m.%Y %H:%M:%S")
        yazar = discord.utils.escape_html(mesaj.author.display_name)
        icerik = discord.utils.escape_html(mesaj.content or "")
        satir = f'<div class="message"><div class="meta"><b>{yazar}</b> <span>{zaman}</span></div>'
        if icerik:
            satir += f'<div class="content">{icerik.replace(chr(10), "<br>")}</div>'
        if mesaj.attachments:
            satir += '<div class="attachments">'
            for ek in mesaj.attachments:
                url = discord.utils.escape_html(ek.url)
                ad = discord.utils.escape_html(ek.filename)
                satir += f'<a href="{url}">{ad}</a><br>'
            satir += '</div>'
        satir += '</div>'
        mesajlar.append(satir)
    sahip = "Bilinmiyor"
    if kanal.topic and kanal.topic.startswith("Ticket sahibi:"):
        sahip_id = kanal.topic.split(":", 1)[1].strip()
        sahip = discord.utils.escape_html(sahip_id)
    kapatan_adi = discord.utils.escape_html(kapatan.display_name)
    html = f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<title>GAMESTURKEY Ticket Transkripti - {discord.utils.escape_html(kanal.name)}</title>
<style>
body{{font-family:Arial,sans-serif;background:#202225;color:#eee;margin:0;padding:24px}}
h1{{margin-top:0}} .info{{background:#2f3136;padding:14px;border-radius:8px;margin-bottom:18px}}
.message{{background:#2f3136;border-radius:8px;padding:10px 12px;margin:8px 0}}
.meta{{font-size:14px;margin-bottom:5px}} .meta span{{color:#aaa;margin-left:8px}}
.content{{white-space:normal;word-wrap:break-word}} a{{color:#58a6ff}} .attachments{{margin-top:8px}}
</style></head><body>
<h1>GAMESTURKEY Ticket Transkripti</h1>
<div class="info">
<b>Ticket:</b> {discord.utils.escape_html(kanal.name)}<br>
<b>Sahip ID:</b> {sahip}<br>
<b>Kapatılan:</b> {kapatan_adi}<br>
<b>Tarih:</b> {datetime.now(TURKEY).strftime("%d.%m.%Y %H:%M:%S")}
</div>
{"".join(mesajlar)}
</body></html>"""
    return html

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ticket Aç", style=discord.ButtonStyle.green, emoji="🎫", custom_id="ticket_create")
    async def ticket_ac(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Bu buton sadece sunucuda kullanılabilir.", ephemeral=True)
            return
        transcript_channel_id, staff_role_ids = await ticket_ayarini_getir(guild.id)
        if transcript_channel_id is None or not staff_role_ids:
            await interaction.response.send_message("Ticket sistemi henüz ayarlanmamış. Yönetici önce G!ticket-ayarla ile transkript kanalı ve yetkili rollerini ayarlamalı.", ephemeral=True)
            return
        category = discord.utils.get(guild.categories, name="TICKETS")
        if category is None:
            try:
                category = await guild.create_category("TICKETS")
            except discord.Forbidden:
                await interaction.response.send_message("Ticket kategorisini oluşturamıyorum. Botun Kanalları Yönet yetkisini kontrol et.", ephemeral=True)
                return
        mevcut = discord.utils.find(lambda ch: ch.topic and ch.topic.startswith(f"Ticket sahibi: {interaction.user.id}"), category.text_channels)
        if mevcut:
            await interaction.response.send_message(f"Zaten açık bir ticketın var: {mevcut.mention}", ephemeral=True)
            return
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True)
        }
        for role_id in staff_role_ids:
            role = guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        try:
            kanal = await guild.create_text_channel(f"ticket-{interaction.user.id}", category=category, overwrites=overwrites, topic=f"Ticket sahibi: {interaction.user.id}")
        except discord.Forbidden:
            await interaction.response.send_message("Ticket kanalı oluşturulamadı. Botun Kanal Yönetme yetkisini kontrol et.", ephemeral=True)
            return
        await kanal.send(f"{interaction.user.mention} ticketın açıldı. Yetkili ekipten biri ilgilenecek.", view=TicketCloseView())
        await interaction.response.send_message(f"Ticket açıldı: {kanal.mention}", ephemeral=True)

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ticket Kapat", style=discord.ButtonStyle.red, emoji="🔒", custom_id="ticket_close")
    async def ticket_kapat(self, interaction: discord.Interaction, button: discord.ui.Button):
        kanal = interaction.channel
        if not isinstance(kanal, discord.TextChannel) or not kanal.topic or not kanal.topic.startswith("Ticket sahibi:"):
            await interaction.response.send_message("Bu kanal bir ticket kanalı değil.", ephemeral=True)
            return
        if "| DURUM: KAPALI" in kanal.topic:
            await interaction.response.send_message("Bu ticket zaten kapalı.", ephemeral=True)
            return
        try:
            sahip_id = int(kanal.topic.split(":", 1)[1].split("|", 1)[0].strip())
        except ValueError:
            await interaction.response.send_message("Ticket sahibi bilgisi bozuk.", ephemeral=True)
            return

        _, staff_role_ids = await ticket_ayarini_getir(kanal.guild.id)
        role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
        yetkili = (
            interaction.user.id == sahip_id
            or bot_yetkili_mi(interaction.user)
            or any(int(x) in role_ids for x in staff_role_ids)
        )
        if not yetkili:
            await interaction.response.send_message("Bu ticketı kapatma yetkin yok.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            html = await ticket_transkript_olustur(kanal, interaction.user)
            transcript_channel_id, _ = await ticket_ayarini_getir(kanal.guild.id)
            transcript_channel = kanal.guild.get_channel(transcript_channel_id) if transcript_channel_id else None
            if transcript_channel is None and transcript_channel_id:
                transcript_channel = await bot.fetch_channel(transcript_channel_id)
            if not isinstance(transcript_channel, discord.TextChannel):
                await interaction.followup.send(
                    "Transkript gönderilemedi: ayarlanan transkript kanalı bulunamadı. /ticket-ayarla ile doğru kanalı seç.",
                    ephemeral=True
                )
                return

            import io
            dosya = discord.File(io.BytesIO(html.encode("utf-8")), filename=f"transcript-{kanal.name}.html")
            await transcript_channel.send(
                f"**Ticket transkripti**\nTicket: {kanal.name}\nKapatan: {interaction.user.mention}",
                file=dosya
            )
        except Exception as ex:
            print(f"Ticket transkript hatası ({kanal.guild.id}/{kanal.id}): {type(ex).__name__}: {ex}")
            await interaction.followup.send(
                "Transkript gönderilemedi; ticket açık bırakıldı. Render loglarında 'Ticket transkript hatası' satırını kontrol et. "
                "Botun transkript kanalında Kanalı Gör, Mesaj Gönder ve Dosya Ekle izinleri olmalı.",
                ephemeral=True
            )
            return

        try:
            sahip = kanal.guild.get_member(sahip_id)
            if sahip is not None:
                await kanal.set_permissions(
                    sahip,
                    view_channel=True,
                    send_messages=False,
                    add_reactions=False,
                    attach_files=False,
                    create_public_threads=False,
                    create_private_threads=False,
                    reason=f"Ticket kapatıldı: {interaction.user}"
                )
            await kanal.edit(
                topic=f"Ticket sahibi: {sahip_id} | DURUM: KAPALI",
                reason=f"Ticket kapatıldı: {interaction.user}"
            )
            await kanal.send(
                f"🔒 Ticket {interaction.user.mention} tarafından kapatıldı. Kanal silinmedi; yalnızca yetkililer silebilir.",
                view=TicketDeleteView()
            )
            await interaction.followup.send(
                f"Ticket kapatıldı ve transkript {transcript_channel.mention} kanalına gönderildi. Kanal silinmedi.",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Transkript gönderildi ama ticket kilitlenemedi. Botun Kanalları Yönet iznini kontrol et.",
                ephemeral=True
            )
        except discord.HTTPException as ex:
            print(f"Ticket kilitleme hatası ({kanal.guild.id}/{kanal.id}): {ex}")
            await interaction.followup.send("Transkript gönderildi ama kanal kilitlenirken hata oluştu.", ephemeral=True)


class TicketDeleteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ticket Sil (Yetkili)", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="ticket_delete")
    async def ticket_sil(self, interaction: discord.Interaction, button: discord.ui.Button):
        kanal = interaction.channel
        if not isinstance(kanal, discord.TextChannel) or not kanal.topic or not kanal.topic.startswith("Ticket sahibi:"):
            await interaction.response.send_message("Bu kanal bir ticket kanalı değil.", ephemeral=True)
            return
        if "| DURUM: KAPALI" not in kanal.topic:
            await interaction.response.send_message("Önce ticket kapatılmalı.", ephemeral=True)
            return

        _, staff_role_ids = await ticket_ayarini_getir(kanal.guild.id)
        role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
        yetkili = bot_yetkili_mi(interaction.user) or any(int(x) in role_ids for x in staff_role_ids)
        if not yetkili:
            await interaction.response.send_message("Bu silme tuşunu yalnızca ticket yetkilileri kullanabilir.", ephemeral=True)
            return

        await interaction.response.send_message("Ticket siliniyor...", ephemeral=True)
        try:
            await kanal.delete(reason=f"Ticket yetkilisi {interaction.user} tarafından silindi")
        except discord.Forbidden:
            await interaction.followup.send("Kanal silinemedi. Botun Kanalları Yönet yetkisini kontrol et.", ephemeral=True)
        except discord.HTTPException as ex:
            print(f"Ticket silme hatası ({kanal.guild.id}/{kanal.id}): {ex}")
            await interaction.followup.send("Kanal silinirken hata oluştu.", ephemeral=True)


async def ticket_panel_gonder(kanal):
    await kanal.send(
        "**GAMESTURKEY DESTEK MERKEZİ**\n\nDestek almak veya yetkililerle iletişime geçmek için aşağıdaki **Ticket Aç** butonuna bas.\nHer kullanıcı aynı anda yalnızca bir ticket açabilir.",
        view=TicketView()
    )


class DuzenleModal(discord.ui.Modal, title="Bot Mesajını Düzenle"):
    eski_mesaj = discord.ui.TextInput(label="Eski mesaj (kopyala-yapıştır)", style=discord.TextStyle.paragraph, placeholder="Botun mevcut mesajını buraya yapıştır.", required=True, max_length=4000)
    yeni_mesaj = discord.ui.TextInput(label="Yeni mesaj", style=discord.TextStyle.paragraph, placeholder="Botun göndereceği yeni mesajı yaz.", required=True, max_length=4000)
    def __init__(self, kanal: discord.TextChannel):
        super().__init__()
        self.kanal = kanal
    async def on_submit(self, interaction: discord.Interaction):
        if not bot_yetkili_mi(interaction.user):
            await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        eski = str(self.eski_mesaj).strip()
        yeni = str(self.yeni_mesaj)
        bulunan = None
        try:
            async for mesaj in self.kanal.history(limit=200):
                if mesaj.author == bot.user and mesaj.content == eski:
                    bulunan = mesaj
                    break
        except discord.Forbidden:
            await interaction.followup.send("Bu kanaldaki mesajları okuyamıyorum. Botun Mesaj Geçmişini Gör yetkisini kontrol et.", ephemeral=True)
            return
        if bulunan is None:
            await interaction.followup.send("Bu kanalda, verdiğin eski metne sahip bir bot mesajı bulamadım. Metni birebir kopyaladığından emin ol.", ephemeral=True)
            return
        try:
            await bulunan.edit(content=yeni)
        except discord.Forbidden:
            await interaction.followup.send("Mesajı düzenleyemedim. Botun Mesajları Yönet yetkisini ve mesajın bota ait olduğunu kontrol et.", ephemeral=True)
            return
        await interaction.followup.send("Bot mesajı başarıyla düzenlendi.", ephemeral=True)

async def zaman_asimi_yetkisi(interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return False
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Üyelere Zaman Aşımı Uygula yetkisine sahip olmalısın.", ephemeral=True)
        return False
    return True

async def yonetici_yetkisi(interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return False
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
        return False
    return True

cekilis_gorevi = None
cekilisler_yuklendi = False

@bot.event
async def on_ready():
    global cekilis_gorevi, cekilisler_yuklendi
    for guild in bot.guilds:
        await uwu_webhook_kur(guild)
    if not cekilisler_yuklendi:
        await aktif_cekilisleri_yukle()
        cekilisler_yuklendi = True
    if cekilis_gorevi is None or cekilis_gorevi.done():
        cekilis_gorevi = asyncio.create_task(cekilis_dongusu())
    print(f"Bot aktif: {bot.user}")
    print("Slash komutları hazır!")

@bot.tree.command(name="ticket-kur", description="Seçilen kanala ticket paneli kurar.")
@app_commands.describe(kanal="Ticket panelinin gönderileceği kanal")
async def ticket_kur(interaction, kanal: discord.TextChannel):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
        return
    await ticket_panel_gonder(kanal)
    await interaction.response.send_message(f"Ticket paneli {kanal.mention} kanalına kuruldu.", ephemeral=True)

@bot.tree.command(name="yardım", description="Botun komutlarını gösterir.")
async def yardim(interaction):
    mesaj = """**GAMESTURKEY — Komutlar (Slash & G! Prefix)**

/yardım veya G!yardım veya g!yardım
Botun komutlarını gösterir.

/say
Seçilen kanala mesaj gönderir.
Gereken yetki: Yönetici

/düzenle
Botun bir mesajını kopyala-yapıştır yöntemiyle düzenler.
Gereken yetki: Yönetici

/duyuru
Seçilen kanala formatlı duyuru gönderir.
Gereken yetki: Yönetici

/yavaşmod
Seçilen kanalda yavaş modu ayarlar.
Gereken yetki: Kanalları Yönet

/rol-ver
Bir kullanıcıya rol verir.
Gereken yetki: Rolleri Yönet

/rol-al
Bir kullanıcıdan rol alır.
Gereken yetki: Rolleri Yönet

/nick
Bir kullanıcının sunucu takma adını değiştirir.
Gereken yetki: Takma Adları Yönet

/uyari
Bir kullanıcıya uyarı verir ve DM gönderir.
Gereken yetki: Üyelere Zaman Aşımı Uygula

/çekiliş, /drop, /çekiliş-bitir, /reroll\n/ticket-kur — Seçilen kanala ticket paneli kurar. (Yönetici)\nDrop: /drop Ödül [başlama süresi] — örn. /drop Nitro 30s. Süre boşsa hemen başlar; ilk yakalayan kazanır.

/uyari-liste veya /uyari-listele
Bir kullanıcının tüm uyarılarını gösterir.
Gereken yetki: Yok

/uyari-sil
Bir kullanıcının seçilen uyarısını siler.
Gereken yetki: Yönetici

/mute
Bir kullanıcıya zaman aşımı uygular.
Gereken yetki: Üyelere Zaman Aşımı Uygula

/kick
Bir kullanıcıyı sunucudan atar.
Gereken yetki: Üyeleri At

/ban
Bir kullanıcıyı sunucudan yasaklar.
Gereken yetki: Üyeleri Yasakla

/temizle
Kanaldaki son mesajları toplu siler.
Gereken yetki: Mesajları Yönet

/kilit
Seçilen kanalı yazmaya kapatır.
Gereken yetki: Kanalları Yönet

/kilit-aç
Seçilen kanalın yazma kilidini açar.
Gereken yetki: Kanalları Yönet
"""
    await interaction.response.send_message(mesaj)

@bot.tree.command(name="düzenle", description="Botun seçilen kanaldaki bir mesajını düzenler.")
@app_commands.describe(kanal="Düzenlenecek bot mesajının bulunduğu kanal")
async def duzenle(interaction, kanal: discord.TextChannel):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
        return
    await interaction.response.send_modal(DuzenleModal(kanal))

@bot.tree.command(name="duyuru", description="Seçilen kanala formatlı duyuru gönderir.")
@app_commands.describe(kanal="Duyurunun gönderileceği kanal", mesaj="Duyuru metni", format="Duyuru formatı")
@app_commands.choices(format=[app_commands.Choice(name="Normal", value="normal"), app_commands.Choice(name="Embed", value="embed"), app_commands.Choice(name="Uyarı", value="uyari"), app_commands.Choice(name="Başarı", value="basari")])
async def duyuru(interaction, kanal: discord.TextChannel, mesaj: str, format: str = "embed"):
    if not await yonetici_yetkisi(interaction): return
    if format == "normal": await kanal.send(f"**DUYURU**\n{mesaj}")
    else:
        baslik = {"embed": "DUYURU", "uyari": "UYARI", "basari": "BAŞARILI"}.get(format, "DUYURU")
        await kanal.send(embed=discord.Embed(title=baslik, description=mesaj))
    await interaction.response.send_message("Duyuru gönderildi.", ephemeral=True)

@bot.tree.command(name="yavaşmod", description="Seçilen kanalda yavaş modu ayarlar.")
@app_commands.describe(kanal="Yavaş mod uygulanacak kanal", saniye="0-21600 saniye")
async def yavasmod(interaction, kanal: discord.TextChannel, saniye: app_commands.Range[int, 0, 21600]):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True); return
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Kanalları Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    if not await ortak_yavasmod(kanal, saniye):
        await interaction.response.send_message("Yavaş modu değiştiremedim.", ephemeral=True); return
    await interaction.response.send_message(f"{kanal.mention} yavaş modu **{saniye} saniye** olarak ayarlandı.")

@bot.tree.command(name="rolü-taşı", description="Bir rolü botun taşıyabileceği en üst konuma taşır.")
@app_commands.describe(rol="En üste taşınacak rol")
async def rolu_tasi(interaction: discord.Interaction, rol: discord.Role):
    guild = interaction.guild
    if guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
        return

    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.response.send_message("Botun Rolleri Yönet izni olmalı.", ephemeral=True)
        return
    if rol.is_default():
        await interaction.response.send_message("@everyone rolü taşınamaz.", ephemeral=True)
        return
    if rol.managed:
        await interaction.response.send_message("Bot veya entegrasyon tarafından yönetilen bu rol taşınamaz.", ephemeral=True)
        return
    if rol >= me.top_role:
        await interaction.response.send_message("Bu rol botun en yüksek rolüyle aynı veya daha yukarıda; bot onu taşıyamaz.", ephemeral=True)
        return

    hedef_konum = me.top_role.position - 1
    try:
        await rol.edit(position=hedef_konum, reason=f"/rolü-taşı komutu: {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("Rol taşınamadı. Botun Rolleri Yönet iznini ve rol sırasını kontrol et.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.response.send_message("Discord rolü taşıyamadı. Biraz sonra tekrar dene.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"**{rol.name}** rolü, botun taşıyabildiği en üst konuma taşındı.",
        ephemeral=True
    )

@bot.tree.command(name="rol-ver", description="Bir kullanıcıya rol verir.")
@app_commands.describe(kisi="Rol verilecek kişi", rol="Verilecek rol")
async def rol_ver(interaction, kisi: discord.Member, rol: discord.Role):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Rolleri Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    ok, hata = await ortak_rol(interaction.guild, kisi, rol, True)
    if not ok:
        await interaction.response.send_message(hata, ephemeral=True); return
    await interaction.response.send_message(f"{rol.mention} rolü {kisi.mention} kullanıcısına verildi.")

@bot.tree.command(name="rol-al", description="Bir kullanıcıdan rol alır.")
@app_commands.describe(kisi="Rolü alınacak kişi", rol="Alınacak rol")
async def rol_al(interaction, kisi: discord.Member, rol: discord.Role):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Rolleri Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    ok, hata = await ortak_rol(interaction.guild, kisi, rol, False)
    if not ok:
        await interaction.response.send_message(hata, ephemeral=True); return
    await interaction.response.send_message(f"{rol.mention} rolü {kisi.mention} kullanıcısından alındı.")

@bot.tree.command(name="nick", description="Bir kullanıcının sunucu takma adını değiştirir.")
@app_commands.describe(kisi="Takma adı değiştirilecek kişi", takma_ad="Yeni takma ad")
async def nick(interaction, kisi: discord.Member, takma_ad: str):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Takma Adları Yönet veya bot bypass yetkisine sahip olmalısın.", ephemeral=True); return
    if not await ortak_nick(kisi, takma_ad):
        await interaction.response.send_message("Takma ad değiştirilemedi. Botun yetkisini ve rol sırasını kontrol et.", ephemeral=True); return
    await interaction.response.send_message(f"{kisi.mention} kullanıcısının takma adı **{takma_ad}** olarak değiştirildi.")

@bot.tree.command(name="say", description="Seçilen kanala mesaj gönderir.")
@app_commands.describe(kanal="Mesajın gönderileceği kanal", mesaj="Gönderilecek mesaj")
async def say(interaction, kanal: discord.TextChannel, mesaj: str):
    if not await yonetici_yetkisi(interaction): return
    await kanal.send(mesaj)
    await interaction.response.send_message("Mesaj gönderildi.", ephemeral=True)


async def ortak_cekilis_baslat(guild, kanal, sure, odul, kazanan_sayisi, baslatan):
    if not 1 <= kazanan_sayisi <= 20:
        return None, "Kazanan sayısı 1-20 arasında olmalı."
    if sure:
        saniye = sureyi_saniyeye_cevir(sure)
        if saniye is None or not 5 <= saniye <= 30 * 86400:
            return None, "Süre hatalı. Örnek: 30s, 10m, 2h, 1d. Süre 5 saniye-30 gün arasında olmalı."
        bitis = datetime.now().timestamp() + saniye
        bitis_metni = f"Bitiş: <t:{int(bitis)}:R>"
    else:
        bitis = 0
        bitis_metni = "Bitiş: **Yetkili bitirene kadar**"
    cursor.execute(
        "INSERT INTO cekilisler (guild_id, kanal_id, mesaj_id, bitis, odul, kazanan_sayisi, tip, baslama) VALUES (?,?,?,?,?,?,?,?)",
        (guild.id, kanal.id, 0, bitis, odul, kazanan_sayisi, "cekilis", 0)
    )
    db.commit()
    cekilis_id = cursor.lastrowid
    try:
        mesaj = await kanal.send(
            f"🎉 **ÇEKİLİŞ #{cekilis_id}**\n\nÖdül: **{odul}**\nKazanan sayısı: **{kazanan_sayisi}**\n{bitis_metni}\n\nKatılmak için aşağıdaki **Katıl** butonuna bas!",
            view=GiveawayView(cekilis_id)
        )
        cursor.execute("UPDATE cekilisler SET mesaj_id = ? WHERE id = ?", (mesaj.id, cekilis_id))
        db.commit()
        return cekilis_id, None
    except discord.HTTPException as e:
        cursor.execute("DELETE FROM cekilisler WHERE id = ?", (cekilis_id,))
        db.commit()
        return None, f"Çekiliş mesajı gönderilemedi: {e}"


async def ortak_drop_baslat(guild, kanal, sure, odul, baslatan):
    baslama = datetime.now().timestamp()
    if sure:
        saniye = sureyi_saniyeye_cevir(sure)
        if saniye is None or not 5 <= saniye <= 30 * 86400:
            return None, "Drop başlama süresi hatalı. Örnek: 30s, 1m, 5m. Süre 5 saniye-30 gün arasında olmalı."
        baslama += saniye

    cursor.execute(
        "INSERT INTO cekilisler (guild_id, kanal_id, mesaj_id, bitis, odul, kazanan_sayisi, tip, baslama) VALUES (?,?,?,?,?,?,?,?)",
        (guild.id, kanal.id, 0, 0, odul, 1, "drop", baslama)
    )
    db.commit()
    drop_id = cursor.lastrowid

    try:
        if baslama > datetime.now().timestamp():
            baslama_metni = f"Başlangıç: <t:{int(baslama)}:R>"
            view = DropView(drop_id, aktif=False)
            icerik = f"**DROP #{drop_id} HAZIRLANIYOR**\n\nÖdül: **{odul}**\n{baslama_metni}\n\nSüre dolunca **YAKALA** butonu aktif olacak!"
        else:
            view = DropView(drop_id, aktif=True)
            icerik = f"**DROP #{drop_id} BAŞLADI!**\n\nÖdül: **{odul}**\n\nİlk yakalayan kazanır!\n\n[YAKALA] butonuna bas!"
        mesaj = await kanal.send(icerik, view=view)
        cursor.execute("UPDATE cekilisler SET mesaj_id = ? WHERE id = ?", (mesaj.id, drop_id))
        db.commit()
        return drop_id, None
    except discord.HTTPException as e:
        cursor.execute("DELETE FROM cekilisler WHERE id = ?", (drop_id,))
        db.commit()
        return None, f"Drop mesajı gönderilemedi: {e}"


@bot.tree.command(name="çekiliş", description="Yeni bir çekiliş başlatır.")
@app_commands.describe(sure="Örn: 10m, 2h, 1d", odul="Verilecek ödül", kazanan_sayisi="Kaç kişi kazanacak?")
async def cekilis(interaction, odul: str = "Ödül belirtilmedi", sure: str = None, kazanan_sayisi: app_commands.Range[int, 1, 20] = 1):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True); return
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    cekilis_id, hata = await ortak_cekilis_baslat(interaction.guild, interaction.channel, sure, odul, kazanan_sayisi, interaction.user)
    if hata:
        await interaction.response.send_message(hata, ephemeral=True); return
    await interaction.response.send_message(f"Çekiliş **#{cekilis_id}** başlatıldı.", ephemeral=True)

@bot.tree.command(name="drop", description="İlk yakalayanın kazandığı drop başlatır.")
@app_commands.describe(odul="Drop ödülü", sure="Opsiyonel başlama gecikmesi: 30s, 1m, 5m. Boşsa hemen başlar.")
async def drop(interaction, odul: str = "Drop ödülü", sure: str = None):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True); return
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    drop_id, hata = await ortak_drop_baslat(interaction.guild, interaction.channel, sure, odul, interaction.user)
    if hata:
        await interaction.response.send_message(hata, ephemeral=True); return
    await interaction.response.send_message(f"Drop **#{drop_id}** hazırlandı.", ephemeral=True)

@bot.tree.command(name="çekiliş-bitir", description="Aktif bir çekilişi hemen bitirir.")
@app_commands.describe(cekilis_no="Bitirilecek çekiliş numarası")
async def cekilis_bitir_komut(interaction, cekilis_no: int):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True); return
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    cursor.execute("SELECT id, aktif FROM cekilisler WHERE id = ? AND guild_id = ?", (cekilis_no, interaction.guild.id))
    row = cursor.fetchone()
    if not row:
        await interaction.response.send_message("Bu çekiliş bulunamadı.", ephemeral=True); return
    if not row[1]:
        await interaction.response.send_message("Bu çekiliş zaten bitmiş.", ephemeral=True); return
    await cekilisi_bitir(cekilis_no)
    await interaction.response.send_message(f"Çekiliş **#{cekilis_no}** bitirildi.", ephemeral=True)

@bot.tree.command(name="reroll", description="Sona ermiş çekilişin kazananını yeniden seçer.")
@app_commands.describe(cekilis_no="Reroll yapılacak çekiliş numarası")
async def reroll(interaction, cekilis_no: int):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True); return
    if not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    cursor.execute("SELECT kanal_id, odul, aktif, kazananlar FROM cekilisler WHERE id = ? AND guild_id = ?", (cekilis_no, interaction.guild.id))
    row = cursor.fetchone()
    if not row:
        await interaction.response.send_message("Bu çekiliş bulunamadı.", ephemeral=True); return
    if row[2]:
        await interaction.response.send_message("Bu çekiliş henüz bitmedi.", ephemeral=True); return
    cursor.execute("SELECT user_id FROM cekilis_katilim WHERE cekilis_id = ?", (cekilis_no,))
    katilanlar = [x[0] for x in cursor.fetchall()]
    eski = set(json.loads(row[3] or "[]"))
    adaylar = [x for x in katilanlar if x not in eski]
    if not adaylar:
        await interaction.response.send_message("Reroll için başka katılımcı yok.", ephemeral=True); return
    yeni = random.choice(adaylar)
    cursor.execute("UPDATE cekilisler SET kazananlar = ? WHERE id = ?", (json.dumps(list(eski | {yeni})), cekilis_no))
    db.commit()
    kanal = bot.get_channel(row[0])
    if kanal:
        await kanal.send(f"🔄 **Çekiliş #{cekilis_no} reroll!** Yeni kazanan: <@{yeni}> — **{row[1]}**")
    await interaction.response.send_message(f"Yeni kazanan: <@{yeni}>", ephemeral=True)

@bot.tree.command(name="degistir", description="Moderasyon mesajlarında UwU Bot webhook'unu açar veya kapatır.")
async def degistir(interaction: discord.Interaction):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
        return
    cursor.execute("SELECT aktif FROM moderasyon_webhooklari WHERE guild_id = ?", (interaction.guild.id,))
    row = cursor.fetchone()
    if row is None:
        hook = await uwu_webhook_kur(interaction.guild)
        if hook is None:
            await interaction.response.send_message("UwU Bot webhook'u kurulamadı. Botun uygun bir kanalda Webhook'ları Yönet iznine ihtiyacı var.", ephemeral=True)
            return
        durum = 1
    else:
        durum = 0 if row[0] else 1
        cursor.execute("UPDATE moderasyon_webhooklari SET aktif = ? WHERE guild_id = ?", (durum, interaction.guild.id))
        db.commit()
        if durum:
            await uwu_webhook_kur(interaction.guild)
    await interaction.response.send_message(
        "UwU Bot moderasyon webhook'u açıldı." if durum else "UwU Bot moderasyon webhook'u kapatıldı.",
        ephemeral=True
    )

@bot.tree.command(name="uyari", description="Bir kullanıcıya uyarı verir.")
@app_commands.describe(kisi="Uyarılacak kişi", sebep="Uyarının sebebi")
async def uyari(interaction, kisi: discord.Member, sebep: str):
    if not await zaman_asimi_yetkisi(interaction): return
    uyari_id, tarih, dm = await ortak_uyari(interaction.guild, kisi, interaction.user, sebep)
    await interaction.response.send_message(f"Uyarı verildi.\n\nKişi: {kisi.mention}\nUyarı No: **#{uyari_id}**\nTarih: **{tarih}**\nSebep: **{sebep}**\n\n{dm}", ephemeral=True)

def uyari_listesi_metni(guild, kisi):
    cursor.execute(
        "SELECT id, sebep, tarih, moderator_id FROM uyarilar WHERE guild_id = ? AND user_id = ? ORDER BY id ASC",
        (guild.id, kisi.id)
    )
    uyarilar = cursor.fetchall()
    if not uyarilar:
        return f"{kisi.mention} adlı kişinin hiç uyarısı yok."
    metin = f"**{kisi.display_name} — Uyarı Listesi**\n\n"
    for uyari_id, sebep, tarih, moderator_id in uyarilar:
        moderator = guild.get_member(moderator_id)
        moderator_ismi = moderator.display_name if moderator else f"ID: {moderator_id}"
        metin += f"**#{uyari_id}**\nTarih: {tarih}\nSebep: {sebep}\nVeren: {moderator_ismi}\n\n"
    return metin[:1900] + ("\n\n... Liste çok uzun." if len(metin) > 1900 else "")

@bot.tree.command(name="uyari-liste", description="Bir kullanıcının uyarılarını listeler.")
@app_commands.describe(kisi="Uyarıları gösterilecek kişi")
async def uyari_liste(interaction, kisi: discord.Member):
    await interaction.response.send_message(uyari_listesi_metni(interaction.guild, kisi))

@bot.tree.command(name="uyari-listele", description="Bir kullanıcının uyarılarını listeler.")
@app_commands.describe(kisi="Uyarıları gösterilecek kişi")
async def uyari_listele(interaction, kisi: discord.Member):
    await interaction.response.send_message(uyari_listesi_metni(interaction.guild, kisi))

@bot.tree.command(name="uyari-sil", description="Bir kullanıcının seçilen uyarısını siler.")
@app_commands.describe(kisi="Uyarısı silinecek kişi", uyari_no="Silinecek uyarının numarası")
async def uyari_sil(interaction, kisi: discord.Member, uyari_no: int):
    if not await yonetici_yetkisi(interaction): return
    cursor.execute("SELECT id, sebep, tarih FROM uyarilar WHERE id = ? AND guild_id = ? AND user_id = ?", (uyari_no, interaction.guild.id, kisi.id))
    uyari = cursor.fetchone()
    if not uyari:
        await interaction.response.send_message(f"#{uyari_no} numaralı uyarı bulunamadı.", ephemeral=True); return
    cursor.execute("DELETE FROM uyarilar WHERE id = ? AND guild_id = ? AND user_id = ?", (uyari_no, interaction.guild.id, kisi.id)); db.commit()
    await interaction.response.send_message(f"""Uyarı silindi.\n\nKişi: {kisi.mention}\nUyarı No: **#{uyari[0]}**\nTarih: {uyari[2]}\nSebep: {uyari[1]}""", ephemeral=True)

@bot.tree.command(name="mute", description="Bir kullanıcıya Discord zaman aşımı uygular.")
@app_commands.describe(kisi="Mute uygulanacak kişi", sure="Örn: 10m, 2h, 1d, 7d", sebep="Mute sebebi")
async def mute(interaction, kisi: discord.Member, sure: str, sebep: str):
    if not await zaman_asimi_yetkisi(interaction): return
    ok, sonuc = await ortak_mute(interaction.guild, kisi, interaction.user, sure, sebep)
    if not ok: return await interaction.response.send_message(sonuc, ephemeral=True)
    await interaction.response.send_message(f"Kişi: {kisi.mention}\nSüre: **{sure}**\nBitiş: {sonuc}\nSebep: **{sebep}**")

@bot.tree.command(name="kick", description="Bir kullanıcıyı sunucudan atar.")
@app_commands.describe(kisi="Atılacak kişi", sebep="Atılma sebebi")
async def kick(interaction, kisi: discord.Member, sebep: str = "Sebep belirtilmedi."):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Üyeleri At yetkisine sahip olmalısın.", ephemeral=True); return
    ok, hata = await ortak_kick(interaction.guild, kisi, interaction.user, sebep)
    if not ok: return await interaction.response.send_message(hata, ephemeral=True)
    await interaction.response.send_message(f"{kisi.mention} sunucudan atıldı. Sebep: **{sebep}**")

@bot.tree.command(name="ban", description="Bir kullanıcıyı sunucudan yasaklar.")
@app_commands.describe(kisi="Yasaklanacak kişi", sebep="Yasaklama sebebi")
async def ban(interaction, kisi: discord.Member, sebep: str = "Sebep belirtilmedi."):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Üyeleri Yasakla yetkisine sahip olmalısın.", ephemeral=True); return
    ok, hata = await ortak_ban(interaction.guild, kisi, interaction.user, sebep)
    if not ok: return await interaction.response.send_message(hata, ephemeral=True)
    await interaction.response.send_message(f"{kisi.mention} sunucudan yasaklandı. Sebep: **{sebep}**")

@bot.tree.command(name="temizle", description="Kanaldaki son mesajları siler.")
@app_commands.describe(miktar="Silinecek mesaj sayısı (1-100)")
async def temizle(interaction, miktar: app_commands.Range[int, 1, 100]):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Mesajları Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try: silinen = await interaction.channel.purge(limit=miktar)
    except discord.Forbidden:
        await interaction.followup.send("Mesajları silemedim. Botun Mesajları Yönet yetkisini kontrol et.", ephemeral=True); return
    await interaction.followup.send(f"**{len(silinen)}** mesaj silindi.", ephemeral=True)

@bot.tree.command(name="kilit", description="Seçilen kanalı yazmaya kapatır.")
@app_commands.describe(kanal="Kilitlenecek kanal")
async def kilit(interaction, kanal: discord.TextChannel):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Kanalları Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    if not await ortak_kilit(kanal, interaction.guild, False):
        await interaction.response.send_message("Kanalı kilitleyemedim. Botun Kanalları Yönet yetkisini kontrol et.", ephemeral=True); return
    await interaction.response.send_message(f"{kanal.mention} kilitlendi.")

@bot.tree.command(name="kilit-aç", description="Seçilen kanalın yazma kilidini açar.")
@app_commands.describe(kanal="Kilidi açılacak kanal")
async def kilit_ac(interaction, kanal: discord.TextChannel):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Kanalları Yönet yetkisine sahip olmalısın.", ephemeral=True); return
    if not await ortak_kilit(kanal, interaction.guild, True):
        await interaction.response.send_message("Kanal kilidini açamadım. Botun Kanalları Yönet yetkisini kontrol et.", ephemeral=True); return
    await interaction.response.send_message(f"{kanal.mention} kilidi açıldı.")


@bot.command(name="çekiliş")
async def prefix_cekilis(ctx, sure: str, kazanan_sayisi: int = 1, *, odul: str):
    if not ctx.guild or not bot_yetkili_mi(ctx.author):
        return await ctx.send("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.")
    cekilis_id, hata = await ortak_cekilis_baslat(ctx.guild, ctx.channel, sure, odul, kazanan_sayisi, ctx.author)
    if hata: return await ctx.send(hata)
    await ctx.send(f"Çekiliş **#{cekilis_id}** başlatıldı.", delete_after=5)

@bot.command(name="drop")
async def prefix_drop(ctx, *args):
    if not ctx.guild or not bot_yetkili_mi(ctx.author):
        return await ctx.send("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.")
    if not args:
        return await ctx.send("Kullanım: G!drop ödül [başlama_süresi]")

    sure = None
    odul_parts = list(args)
    if len(odul_parts) >= 2 and sureyi_saniyeye_cevir(odul_parts[-1]) is not None:
        sure = odul_parts.pop()
    odul = " ".join(odul_parts).strip()
    if not odul:
        return await ctx.send("Ödül belirtmelisin.")

    drop_id, hata = await ortak_drop_baslat(ctx.guild, ctx.channel, sure, odul, ctx.author)
    if hata:
        return await ctx.send(hata)
    await ctx.send(f"Drop **#{drop_id}** hazırlandı.", delete_after=5)

@bot.command(name="çekiliş-bitir")
async def prefix_cekilis_bitir(ctx, cekilis_no: int):
    if not ctx.guild or not bot_yetkili_mi(ctx.author):
        return await ctx.send("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.")
    cursor.execute("SELECT aktif FROM cekilisler WHERE id = ? AND guild_id = ?", (cekilis_no, ctx.guild.id))
    row = cursor.fetchone()
    if not row: return await ctx.send("Bu çekiliş bulunamadı.")
    if not row[0]: return await ctx.send("Bu çekiliş zaten bitmiş.")
    await cekilisi_bitir(cekilis_no)
    await ctx.send(f"Çekiliş **#{cekilis_no}** bitirildi.", delete_after=5)

@bot.command(name="reroll")
async def prefix_reroll(ctx, cekilis_no: int):
    if not ctx.guild or not bot_yetkili_mi(ctx.author):
        return await ctx.send("Bu komut için Sunucuyu Yönet yetkisine sahip olmalısın.")
    cursor.execute("SELECT kanal_id, odul, aktif, kazananlar FROM cekilisler WHERE id = ? AND guild_id = ?", (cekilis_no, ctx.guild.id))
    row = cursor.fetchone()
    if not row: return await ctx.send("Bu çekiliş bulunamadı.")
    if row[2]: return await ctx.send("Bu çekiliş henüz bitmedi.")
    cursor.execute("SELECT user_id FROM cekilis_katilim WHERE cekilis_id = ?", (cekilis_no,))
    katilanlar = [x[0] for x in cursor.fetchall()]
    eski = set(json.loads(row[3] or "[]"))
    adaylar = [x for x in katilanlar if x not in eski]
    if not adaylar: return await ctx.send("Reroll için başka katılımcı yok.")
    yeni = random.choice(adaylar)
    cursor.execute("UPDATE cekilisler SET kazananlar = ? WHERE id = ?", (json.dumps(list(eski | {yeni})), cekilis_no)); db.commit()
    kanal = bot.get_channel(row[0])
    if kanal: await kanal.send(f"🔄 **Çekiliş #{cekilis_no} reroll!** Yeni kazanan: <@{yeni}> — **{row[1]}**")
    await ctx.send(f"Yeni kazanan: <@{yeni}>", delete_after=5)

@bot.tree.command(name="ticket-ayarla", description="Ticket transkript kanalını ve ticket yetkili rollerini ayarlar.")
@app_commands.describe(transkript_kanali="HTML transkriptlerinin gideceği kanal", rol1="Ticket ile ilgilenecek ilk rol", rol2="İkinci rol", rol3="Üçüncü rol", rol4="Dördüncü rol", rol5="Beşinci rol", rol6="Altıncı rol", rol7="Yedinci rol", rol8="Sekizinci rol", rol9="Dokuzuncu rol", rol10="Onuncu rol")
async def ticket_ayarla(interaction, transkript_kanali: discord.TextChannel, rol1: discord.Role, rol2: discord.Role = None, rol3: discord.Role = None, rol4: discord.Role = None, rol5: discord.Role = None, rol6: discord.Role = None, rol7: discord.Role = None, rol8: discord.Role = None, rol9: discord.Role = None, rol10: discord.Role = None):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
        return
    roller = [r for r in [rol1, rol2, rol3, rol4, rol5, rol6, rol7, rol8, rol9, rol10] if r]
    cursor.execute("INSERT INTO ticket_ayarlar (guild_id, transcript_channel_id, staff_role_ids) VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET transcript_channel_id=excluded.transcript_channel_id, staff_role_ids=excluded.staff_role_ids", (interaction.guild.id, transkript_kanali.id, json.dumps([r.id for r in roller])))
    db.commit()
    await interaction.response.send_message("Ticket sistemi ayarlandı.\nTranskript: " + transkript_kanali.mention + "\nRoller: " + ", ".join(r.mention for r in roller))

@bot.command(name="ticket-ayarla")
async def prefix_ticket_ayarla(ctx, transkript_kanali: discord.TextChannel, *roller: discord.Role):
    if not ctx.guild or not bot_yetkili_mi(ctx.author):
        return await ctx.send("Bu komut için Yönetici yetkisine sahip olmalısın.")
    roller = list(roller)[:10]
    if not roller:
        return await ctx.send("En az bir yetkili rol belirtmelisin. Örnek: G!ticket-ayarla #transkript @Destek @Mod")
    cursor.execute("INSERT INTO ticket_ayarlar (guild_id, transcript_channel_id, staff_role_ids) VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET transcript_channel_id=excluded.transcript_channel_id, staff_role_ids=excluded.staff_role_ids", (ctx.guild.id, transkript_kanali.id, json.dumps([r.id for r in roller])))
    db.commit()
    await ctx.send("Ticket sistemi ayarlandı.\nTranskript: " + transkript_kanali.mention + "\nRoller: " + ", ".join(r.mention for r in roller), delete_after=10)


@bot.command(name="giris-ayarla")
async def prefix_giris_ayarla(ctx, rol: discord.Role, *kanallar: discord.TextChannel):
    if not ctx.guild or not bot_yetkili_mi(ctx.author):
        return await ctx.send("Bu komut için Yönetici yetkisine sahip olmalısın.")
    if not ctx.guild.me.guild_permissions.manage_roles:
        return await ctx.send("Botun Rolleri Yönet yetkisi olmalı.")
    if not ctx.guild.me.guild_permissions.manage_channels:
        return await ctx.send("Botun Kanalları Yönet yetkisi olmalı.")
    if rol >= ctx.guild.me.top_role:
        return await ctx.send("Seçtiğin rol botun en yüksek rolünün altında olmalı.")
    kanallar = list(dict.fromkeys(kanallar))
    if not kanallar:
        return await ctx.send("En az bir açık kanal belirtmelisin. Örnek: G!giris-ayarla @YeniGelen #kurallar #dogrulama")

    allowed_ids = [k.id for k in kanallar]
    cursor.execute(
        "INSERT INTO giris_ayarlar (guild_id, role_id, allowed_channel_ids) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id, allowed_channel_ids=excluded.allowed_channel_ids",
        (ctx.guild.id, rol.id, json.dumps(allowed_ids))
    )
    db.commit()
    await giris_ayarlarini_uygula(ctx.guild)
    await ctx.send(
        f"Yeni üye sistemi ayarlandı.\nBekleme rolü: {rol.mention}\n"
        f"Açık kanallar: {', '.join(k.mention for k in kanallar)}"
    )

@bot.command(name="ticket-kur")
async def prefix_ticket_kur(ctx, kanal: discord.TextChannel):
    if not ctx.guild or not bot_yetkili_mi(ctx.author):
        return await ctx.send("Bu komut için Yönetici yetkisine sahip olmalısın.")
    await ticket_panel_gonder(kanal)
    await ctx.send(f"Ticket paneli {kanal.mention} kanalına kuruldu.", delete_after=5)

@bot.command(name="yardım")
async def prefix_yardim(ctx):
    yardim_metni = """**GAMESTURKEY — Komutlar (Slash & Prefix)**

/yardım veya G!yardım veya g!yardım — Botun komutlarını gösterir.
/say veya G!say veya g!say — Seçilen kanala mesaj gönderir. (Yönetici)
/düzenle veya G!düzenle veya g!düzenle — Bot mesajını düzenler. (Yönetici)
/duyuru veya G!duyuru veya g!duyuru — Formatlı duyuru gönderir. (Yönetici)\n  G!duyuru kullanım: G!duyuru #kanal [normal|embed|uyari|basari] mesaj
/yavaşmod veya G!yavaşmod veya g!yavaşmod — Yavaş modu ayarlar. (Kanalları Yönet)
/rol-ver veya G!rol-ver veya g!rol-ver — Bir kullanıcıya rol verir. (Rolleri Yönet)
/rol-al veya G!rol-al veya g!rol-al — Bir kullanıcıdan rol alır. (Rolleri Yönet)
/nick veya G!nick veya g!nick — Takma ad değiştirir. (Takma Adları Yönet)
/uyari veya G!uyari veya g!uyari veya G!uyarı veya g!uyarı — Uyarı verir ve kullanıcıya DM gönderir. (Üyelere Zaman Aşımı Uygula)
/ticket-ayarla veya G!ticket-ayarla — Transkript kanalı ve ticket yetkili rollerini ayarlar.
/giris-ayarla veya G!giris-ayarla veya g!giris-ayarla — Yeni üyelerin bekleme rolünü ve görebileceği kanalları ayarlar. (Yönetici)
/hedef — Mesaj veya davet hedefi oluşturur; kişi boşsa herkes için çalışır. (Yönetici)
/çekiliş veya G!çekiliş veya g!çekiliş
/drop veya G!drop veya g!drop
/çekiliş-bitir veya G!çekiliş-bitir veya g!çekiliş-bitir
/reroll veya G!reroll veya g!reroll
/uyari-liste veya /uyari-listele veya G!uyarılar veya g!uyarılar — Uyarıları gösterir.
/uyari-sil veya G!uyari-sil veya g!uyari-sil — Uyarı siler. (Yönetici)
/mute veya G!mute veya g!mute — Zaman aşımı uygular. (Üyelere Zaman Aşımı Uygula)
/kick veya G!kick veya g!kick — Kullanıcıyı atar. (Üyeleri At)
/ban veya G!ban veya g!ban — Kullanıcıyı yasaklar. (Üyeleri Yasakla)
/temizle veya G!sil veya g!sil — Mesajları toplu siler. (Mesajları Yönet)
/kilit veya G!kilit veya g!kilit — Kanalı kilitler. (Kanalları Yönet)
/kilit-aç veya G!kilit-aç veya g!kilit-aç — Kanal kilidini açar. (Kanalları Yönet)"""
    aktif_prefix = "G+" if super_games_aktif(ctx.guild) else "G!"
    await ctx.send(yardim_metni.replace("G!", aktif_prefix).replace("g!", aktif_prefix.lower()))

@bot.command(name="say")
async def prefix_say(ctx, kanal: discord.TextChannel, *, mesaj: str):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Yönetici yetkisine sahip olmalısın.")
    await kanal.send(mesaj); await ctx.send("Mesaj gönderildi.", delete_after=5)

@bot.command(name="duyuru")
async def prefix_duyuru(ctx, kanal: discord.TextChannel, *, veri: str):
    if not ctx.guild or not bot_yetkili_mi(ctx.author):
        return await ctx.send("Bu komut için Yönetici yetkisine sahip olmalısın.")
    parcalar = veri.split(maxsplit=1)
    if len(parcalar) == 1:
        format = "embed"
        mesaj = parcalar[0]
    else:
        format, mesaj = parcalar
        format = format.lower()
        if format not in {"normal", "embed", "uyari", "basari"}:
            format = "embed"
            mesaj = veri
    if format == "normal":
        await kanal.send(f"**DUYURU**\n{mesaj}")
    else:
        baslik = {"embed": "DUYURU", "uyari": "UYARI", "basari": "BAŞARILI"}[format]
        await kanal.send(embed=discord.Embed(title=baslik, description=mesaj))
    await ctx.send("Duyuru gönderildi.", delete_after=5)

@bot.command(name="yavaşmod", aliases=["yavasmod"])
async def prefix_yavasmod(ctx, kanal: discord.TextChannel, saniye: int):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Kanalları Yönet yetkisine sahip olmalısın.")
    if not 0 <= saniye <= 21600: return await ctx.send("Süre 0-21600 saniye arasında olmalı.")
    if not await ortak_yavasmod(kanal, saniye): return await ctx.send("Yavaş modu değiştiremedim.")
    await ctx.send(f"{kanal.mention} yavaş modu **{saniye} saniye** olarak ayarlandı.")

@bot.command(name="rol-ver")
async def prefix_rol_ver(ctx, kisi: discord.Member, rol: discord.Role):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Rolleri Yönet yetkisine sahip olmalısın.")
    ok, hata = await ortak_rol(ctx.guild, kisi, rol, True)
    if not ok: return await ctx.send(hata)
    await ctx.send(f"{rol.mention} rolü {kisi.mention} kullanıcısına verildi.")

@bot.command(name="rol-al")
async def prefix_rol_al(ctx, kisi: discord.Member, rol: discord.Role):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Rolleri Yönet yetkisine sahip olmalısın.")
    ok, hata = await ortak_rol(ctx.guild, kisi, rol, False)
    if not ok: return await ctx.send(hata)
    await ctx.send(f"{rol.mention} rolü {kisi.mention} kullanıcısından alındı.")

@bot.command(name="nick")
async def prefix_nick(ctx, kisi: discord.Member, *, takma_ad: str):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Takma Adları Yönet veya bot bypass yetkisine sahip olmalısın.")
    if not await ortak_nick(kisi, takma_ad): return await ctx.send("Takma ad değiştirilemedi. Botun yetkisini ve rol sırasını kontrol et.")
    await ctx.send(f"{kisi.mention} kullanıcısının takma adı **{takma_ad}** olarak değiştirildi.")

@bot.command(name="uyari", aliases=["uyarı"])
async def prefix_uyari(ctx, kisi: discord.Member, *, sebep: str):
    if not ctx.guild or not bot_yetkili_mi(ctx.author):
        return await ctx.send("Bu komut için Üyelere Zaman Aşımı Uygula yetkisine sahip olmalısın.")
    uyari_id, tarih, dm = await ortak_uyari(ctx.guild, kisi, ctx.author, sebep)
    await moderasyon_yanit(ctx, f"Uyarı verildi: {kisi.mention}\nUyarı No: **#{uyari_id}**\nTarih: **{tarih}**\nSebep: **{sebep}**\n{dm}")

@bot.command(name="uyarılar")
async def prefix_uyari_liste(ctx, kisi: discord.Member):
    if not ctx.guild:
        return
    await moderasyon_yanit(ctx, uyari_listesi_metni(ctx.guild, kisi))

@bot.command(name="uyari-sil")
async def prefix_uyari_sil(ctx, kisi: discord.Member, uyari_no: int):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Yönetici yetkisine sahip olmalısın.")
    cursor.execute("SELECT id,sebep,tarih FROM uyarilar WHERE id=? AND guild_id=? AND user_id=?", (uyari_no,ctx.guild.id,kisi.id))
    row = cursor.fetchone()
    if not row: return await moderasyon_yanit(ctx, f"#{uyari_no} numaralı uyarı bulunamadı.")
    cursor.execute("DELETE FROM uyarilar WHERE id=? AND guild_id=? AND user_id=?", (uyari_no,ctx.guild.id,kisi.id)); db.commit()
    await moderasyon_yanit(ctx, f"#{row[0]} numaralı uyarı silindi.")

@bot.command(name="mute")
async def prefix_mute(ctx, kisi: discord.Member, sure: str, *, sebep: str):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Üyelere Zaman Aşımı Uygula yetkisine sahip olmalısın.")
    ok, sonuc = await ortak_mute(ctx.guild, kisi, ctx.author, sure, sebep)
    if not ok: return await moderasyon_yanit(ctx, sonuc)
    await moderasyon_yanit(ctx, f"{kisi.mention} kullanıcısına **{sure}** zaman aşımı uygulandı. Bitiş: {sonuc}")

@bot.command(name="kick")
async def prefix_kick(ctx, kisi: discord.Member, *, sebep: str = "Sebep belirtilmedi."):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Üyeleri At yetkisine sahip olmalısın.")
    ok, hata = await ortak_kick(ctx.guild, kisi, ctx.author, sebep)
    if not ok: return await moderasyon_yanit(ctx, hata)
    await moderasyon_yanit(ctx, f"{kisi.mention} sunucudan atıldı. Sebep: **{sebep}**")

@bot.command(name="ban")
async def prefix_ban(ctx, kisi: discord.Member, *, sebep: str = "Sebep belirtilmedi."):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Üyeleri Yasakla yetkisine sahip olmalısın.")
    ok, hata = await ortak_ban(ctx.guild, kisi, ctx.author, sebep)
    if not ok: return await moderasyon_yanit(ctx, hata)
    await moderasyon_yanit(ctx, f"{kisi.mention} sunucudan yasaklandı. Sebep: **{sebep}**")

@bot.command(name="sil")
async def prefix_temizle(ctx, miktar: int):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Mesajları Yönet yetkisine sahip olmalısın.")
    if not 1 <= miktar <= 100: return await ctx.send("Miktar 1-100 arasında olmalı.")
    silinen = await ctx.channel.purge(limit=miktar + 1)
    normal_silinen = max(0, len(silinen) - 1)
    await ctx.send(f"**{normal_silinen}** mesaj + komut mesajı silindi.", delete_after=5)

@bot.command(name="kilit")
async def prefix_kilit(ctx, kanal: discord.TextChannel):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Kanalları Yönet yetkisine sahip olmalısın.")
    if not await ortak_kilit(kanal, ctx.guild, False): return await ctx.send("Kanalı kilitleyemedim. Botun Kanalları Yönet yetkisini kontrol et.")
    await ctx.send(f"{kanal.mention} kilitlendi.")

@bot.command(name="kilit-aç")
async def prefix_kilit_ac(ctx, kanal: discord.TextChannel):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Kanalları Yönet yetkisine sahip olmalısın.")
    if not await ortak_kilit(kanal, ctx.guild, True): return await ctx.send("Kanal kilidini açamadım. Botun Kanalları Yönet yetkisini kontrol et.")
    await ctx.send(f"{kanal.mention} kilidi açıldı.")

@bot.command(name="düzenle")
async def prefix_duzenle(ctx, kanal: discord.TextChannel, *, veri: str):
    if not ctx.guild or not bot_yetkili_mi(ctx.author): return await ctx.send("Bu komut için Yönetici yetkisine sahip olmalısın.")
    if " | " not in veri: return await ctx.send("Kullanım: G!düzenle #kanal eski mesaj | yeni mesaj")
    eski, yeni = veri.split(" | ", 1)
    async for msg in kanal.history(limit=200):
        if msg.author == bot.user and msg.content == eski.strip():
            await msg.edit(content=yeni); return await ctx.send("Bot mesajı başarıyla düzenlendi.", delete_after=5)
    await ctx.send("Bu kanalda eski mesaj bulunamadı.")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound): return
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)): return await ctx.send("Kullanım hatalı. G!yardım yazarak komutları görebilirsin.")
    if isinstance(error, discord.Forbidden): return await ctx.send("Bu işlem için botun yetkileri yetersiz.")
    raise error


# Üye kayıt sistemi
cursor.execute("""
CREATE TABLE IF NOT EXISTS kayit_ayarlar (
    guild_id INTEGER PRIMARY KEY,
    kanal_id INTEGER NOT NULL,
    kayitli_rol_id INTEGER NOT NULL,
    kayitsiz_rol_id INTEGER NOT NULL
)
""")
db.commit()


cursor.execute("""
CREATE TABLE IF NOT EXISTS kayit_gorunur_kanallar (
    guild_id INTEGER PRIMARY KEY,
    kanal_ids TEXT NOT NULL DEFAULT '[]'
)
""")
db.commit()


class KayitView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Kayıt Et", style=discord.ButtonStyle.success, custom_id="gamesturkey:kayit_et")
    async def kayit_et(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("Bu buton yalnızca sunucuda kullanılabilir.", ephemeral=True)
            return

        # Butonu görebilen/kanala erişebilen herkes kayıt yapabilir.
        permissions = interaction.channel.permissions_for(interaction.user)
        if not permissions.view_channel:
            await interaction.response.send_message("Bu kayıt kanalına erişimin yok.", ephemeral=True)
            return

        try:
            member_id = int(interaction.message.embeds[0].footer.text.split(":")[-1].strip())
        except (IndexError, AttributeError, ValueError):
            await interaction.response.send_message("Kayıt mesajındaki üye bilgisi okunamadı.", ephemeral=True)
            return

        member = interaction.guild.get_member(member_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(member_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await interaction.response.send_message("Bu üye artık sunucuda değil.", ephemeral=True)
                return

        if member.bot:
            await interaction.response.send_message("Bot hesapları bu butonla kaydedilemez.", ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message("Kendini bu butonla kaydedemezsin.", ephemeral=True)
            return

        cursor.execute(
            "SELECT kayitli_rol_id, kayitsiz_rol_id FROM kayit_ayarlar WHERE guild_id = ?",
            (interaction.guild.id,)
        )
        settings = cursor.fetchone()
        if not settings:
            await interaction.response.send_message("Kayıt sistemi henüz ayarlanmamış.", ephemeral=True)
            return

        kayitli_rol = interaction.guild.get_role(settings[0])
        kayitsiz_rol = interaction.guild.get_role(settings[1])
        if kayitli_rol is None or kayitsiz_rol is None:
            await interaction.response.send_message("Kayıt rolleri bulunamadı. Yönetici /kayıt-ayarla komutuyla yeniden ayarlasın.", ephemeral=True)
            return
        if kayitli_rol >= interaction.guild.me.top_role or kayitsiz_rol >= interaction.guild.me.top_role:
            await interaction.response.send_message("Botun rolü, kayıtlı ve kayıtsız rollerinin üstünde olmalı.", ephemeral=True)
            return
        if kayitli_rol in member.roles and kayitsiz_rol not in member.roles:
            await interaction.response.send_message("Bu üye zaten kayıtlı.", ephemeral=True)
            return

        try:
            await member.add_roles(kayitli_rol, reason=f"{interaction.user} tarafından kayıt edildi")
            if kayitsiz_rol in member.roles:
                await member.remove_roles(kayitsiz_rol, reason=f"{interaction.user} tarafından kayıt edildi")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message("Roller değiştirilemedi. Botun Rolleri Yönet iznini ve rol sırasını kontrol edin.", ephemeral=True)
            return

        terfi_ayar = terfi_ayarlarini_getir(interaction.guild.id)
        if terfi_ayar["kayit"] and interaction.user.id in terfi_ayar["kayit"]:
            cursor.execute("INSERT OR IGNORE INTO terfi_istatistik (guild_id, user_id) VALUES (?, ?)", (interaction.guild.id, interaction.user.id))
            cursor.execute("UPDATE terfi_istatistik SET kayit = kayit + 1 WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, interaction.user.id))
            db.commit()

        await interaction.response.send_message(
            f"✅ {member.mention} üyesini {interaction.user.mention} kaydetti. "
            f"{kayitli_rol.mention} verildi, {kayitsiz_rol.mention} kaldırıldı.",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False)
        )


async def kayit_giris_mesaji_gonder(member: discord.Member):
    cursor.execute("SELECT kanal_id, kayitsiz_rol_id FROM kayit_ayarlar WHERE guild_id = ?", (member.guild.id,))
    row = cursor.fetchone()
    if not row:
        return

    kayitsiz_rol = member.guild.get_role(row[1])
    if kayitsiz_rol and kayitsiz_rol < member.guild.me.top_role and kayitsiz_rol not in member.roles:
        try:
            await member.add_roles(kayitsiz_rol, reason="Kayıt sistemi: yeni üye")
        except (discord.Forbidden, discord.HTTPException):
            pass

    kanal = member.guild.get_channel(row[0])
    if not isinstance(kanal, discord.TextChannel):
        return
    izinler = kanal.permissions_for(member.guild.me)
    if not izinler.view_channel or not izinler.send_messages:
        return

    embed = discord.Embed(
        title="Yeni üye katıldı",
        description=f"{member.mention} sunucuya katıldı! Kayıt etmek için aşağıdaki butona basın.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Kayıt edilecek üye ID: {member.id}")
    try:
        await kanal.send(
            embed=embed,
            view=KayitView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False)
        )
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.tree.command(name="kayıt-ayarla", description="Üye kayıt kanalını ve kayıt rollerini ayarlar.")
@app_commands.describe(
    kanal="Yeni üyelerin giriş mesajının gönderileceği kanal",
    kayitli_rol="Kayıt tamamlanınca verilecek rol",
    kayitsiz_rol="Kayıt tamamlanınca kaldırılacak rol"
)
async def kayit_ayarla(
    interaction: discord.Interaction,
    kanal: discord.TextChannel,
    kayitli_rol: discord.Role,
    kayitsiz_rol: discord.Role
):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
        return
    if kayitli_rol.id == kayitsiz_rol.id:
        await interaction.response.send_message("Kayıtlı ve kayıtsız rolleri farklı olmalı.", ephemeral=True)
        return
    if not interaction.guild.me.guild_permissions.manage_roles:
        await interaction.response.send_message("Botun Rolleri Yönet izni olmalı.", ephemeral=True)
        return
    if kayitli_rol >= interaction.guild.me.top_role or kayitsiz_rol >= interaction.guild.me.top_role:
        await interaction.response.send_message("Botun en yüksek rolü, seçilen iki rolün de üstünde olmalı.", ephemeral=True)
        return
    kanal_izinleri = kanal.permissions_for(interaction.guild.me)
    if not kanal_izinleri.view_channel or not kanal_izinleri.send_messages or not kanal_izinleri.embed_links:
        await interaction.response.send_message("Botun seçilen kanalda Kanalı Görüntüle, Mesaj Gönder ve Bağlantı Yerleştir izinleri olmalı.", ephemeral=True)
        return

    cursor.execute(
        "INSERT INTO kayit_ayarlar (guild_id, kanal_id, kayitli_rol_id, kayitsiz_rol_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET kanal_id=excluded.kanal_id, kayitli_rol_id=excluded.kayitli_rol_id, kayitsiz_rol_id=excluded.kayitsiz_rol_id",
        (interaction.guild.id, kanal.id, kayitli_rol.id, kayitsiz_rol.id)
    )
    db.commit()
    await interaction.response.send_message(
        f"Kayıt sistemi ayarlandı.\nKanal: {kanal.mention}\n"
        f"Kayıtlı rol: {kayitli_rol.mention}\nKayıtsız rol: {kayitsiz_rol.mention}\n"
        "Yeni katılanlar için kayıt mesajı gönderilecek. Kayıt butonunu görebilen herkes kullanabilir.",
        ephemeral=True
    )


@bot.tree.command(name="kayıt-kanalları-ayarla", description="Kayıtsız üyelerin görebileceği kanalları seçer.")
@app_commands.describe(
    kanal1="Kayıtsız üyelerin görebileceği kanal",
    kanal2="İsteğe bağlı ikinci kanal",
    kanal3="İsteğe bağlı üçüncü kanal",
    kanal4="İsteğe bağlı dördüncü kanal",
    kanal5="İsteğe bağlı beşinci kanal",
    kanal6="İsteğe bağlı altıncı kanal",
    kanal7="İsteğe bağlı yedinci kanal",
    kanal8="İsteğe bağlı sekizinci kanal",
    kanal9="İsteğe bağlı dokuzuncu kanal",
    kanal10="İsteğe bağlı onuncu kanal"
)
async def kayit_kanallari_ayarla(
    interaction: discord.Interaction,
    kanal1: discord.TextChannel,
    kanal2: discord.TextChannel = None,
    kanal3: discord.TextChannel = None,
    kanal4: discord.TextChannel = None,
    kanal5: discord.TextChannel = None,
    kanal6: discord.TextChannel = None,
    kanal7: discord.TextChannel = None,
    kanal8: discord.TextChannel = None,
    kanal9: discord.TextChannel = None,
    kanal10: discord.TextChannel = None
):
    if interaction.guild is None or not bot_yetkili_mi(interaction.user):
        await interaction.response.send_message("Bu komut için Yönetici yetkisine sahip olmalısın.", ephemeral=True)
        return

    cursor.execute(
        "SELECT kayitsiz_rol_id FROM kayit_ayarlar WHERE guild_id = ?",
        (interaction.guild.id,)
    )
    row = cursor.fetchone()
    if not row:
        await interaction.response.send_message("Önce /kayıt-ayarla komutuyla kayıtlı ve kayıtsız rollerini ayarla.", ephemeral=True)
        return

    kayitsiz_rol = interaction.guild.get_role(row[0])
    if kayitsiz_rol is None:
        await interaction.response.send_message("Kayıtsız rolü bulunamadı; /kayıt-ayarla ile yeniden ayarla.", ephemeral=True)
        return
    if kayitsiz_rol >= interaction.guild.me.top_role:
        await interaction.response.send_message("Botun en yüksek rolü kayıtsız rolünün üstünde olmalı.", ephemeral=True)
        return
    if not interaction.guild.me.guild_permissions.manage_roles or not interaction.guild.me.guild_permissions.manage_channels:
        await interaction.response.send_message("Botun Rolleri Yönet ve Kanalları Yönet izinleri olmalı.", ephemeral=True)
        return

    secilen = [k for k in (kanal1, kanal2, kanal3, kanal4, kanal5, kanal6, kanal7, kanal8, kanal9, kanal10) if k is not None]
    secilen = list({k.id: k for k in secilen}.values())
    secilen_ids = {k.id for k in secilen}

    await interaction.response.defer(ephemeral=True, thinking=True)
    hatalar = []
    for kanal in interaction.guild.channels:
        try:
            await kanal.set_permissions(
                kayitsiz_rol,
                view_channel=(kanal.id in secilen_ids),
                reason=f"Kayıt sistemi görünür kanal ayarı: {interaction.user}"
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            hatalar.append(kanal.name)
    cursor.execute(
        "INSERT INTO kayit_gorunur_kanallar (guild_id, kanal_ids) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET kanal_ids=excluded.kanal_ids",
        (interaction.guild.id, json.dumps(sorted(secilen_ids)))
    )
    db.commit()

    metin = ", ".join(k.mention for k in secilen)
    cevap = f"Kayıtsız üyelerin görebileceği kanallar ayarlandı: {metin}"
    if hatalar:
        cevap += "\nBazı kanalların izinleri değiştirilemedi: " + ", ".join(hatalar[:10])
    cevap += "\nKayıt olan üyeler kayıtlı rolü üzerinden normal kanallara erişebilir."
    await interaction.followup.send(cevap, ephemeral=True)



# Yetkili terfi/ilerleme sistemi
cursor.execute("""
CREATE TABLE IF NOT EXISTS terfi_ayarlar (
    guild_id INTEGER PRIMARY KEY,
    yetkili_rol_ids TEXT NOT NULL DEFAULT '[]',
    max_rol_id INTEGER,
    kayit_rol_ids TEXT NOT NULL DEFAULT '[]'
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS terfi_istatistik (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    mesaj INTEGER NOT NULL DEFAULT 0,
    ses_saniye INTEGER NOT NULL DEFAULT 0,
    davet INTEGER NOT NULL DEFAULT 0,
    kayit INTEGER NOT NULL DEFAULT 0,
    ses_baslangic REAL,
    PRIMARY KEY (guild_id, user_id)
)
""")
db.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS terfi_rol_sureleri (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    started_at REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
)
""")
db.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS terfi_rol_hedefleri (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    mesaj_hedef INTEGER NOT NULL DEFAULT 1000,
    ses_hedef INTEGER NOT NULL DEFAULT 25,
    davet_hedef INTEGER NOT NULL DEFAULT 25,
    kayit_hedef INTEGER NOT NULL DEFAULT 50,
    PRIMARY KEY (guild_id, role_id)
)
""")
db.commit()

def terfi_rol_hedefi_getir(guild_id, role_id):
    cursor.execute(
        "SELECT mesaj_hedef, ses_hedef, davet_hedef, kayit_hedef FROM terfi_rol_hedefleri WHERE guild_id = ? AND role_id = ?",
        (guild_id, role_id)
    )
    row = cursor.fetchone()
    if not row:
        return {"mesaj": 1000, "ses": 25, "davet": 25, "kayit": 50}
    return {"mesaj": max(1, row[0]), "ses": max(1, row[1]), "davet": max(1, row[2]), "kayit": max(1, row[3])}

cursor.execute("""
CREATE TABLE IF NOT EXISTS terfi_kisi_hedefleri (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    mesaj_hedef INTEGER NOT NULL DEFAULT 1000,
    ses_hedef INTEGER NOT NULL DEFAULT 25,
    davet_hedef INTEGER NOT NULL DEFAULT 25,
    kayit_hedef INTEGER NOT NULL DEFAULT 50,
    PRIMARY KEY (guild_id, user_id)
)
""")
db.commit()

def terfi_kisi_hedefi_getir(guild_id, user_id):
    cursor.execute(
        "SELECT mesaj_hedef, ses_hedef, davet_hedef, kayit_hedef FROM terfi_kisi_hedefleri WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    row = cursor.fetchone()
    if not row:
        return {"mesaj": 1000, "ses": 25, "davet": 25, "kayit": 50}
    return {"mesaj": max(1, row[0]), "ses": max(1, row[1]), "davet": max(1, row[2]), "kayit": max(1, row[3])}

def terfi_kisi_hedefi_var(guild_id, user_id):
    cursor.execute(
        "SELECT 1 FROM terfi_kisi_hedefleri WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    return cursor.fetchone() is not None




# Sunucu ayarları ve kayıtları için indirilebilir JSON yedeği ve güvenli geri yükleme.
SUNUCU_YEDEK_SURUMU = 7
SUNUCU_YEDEK_TABLOLARI = {
    # Terfi sistemi ve ilerlemeler
    "terfi_ayarlar": (
        "guild_id", "yetkili_rol_ids", "max_rol_id", "kayit_rol_ids"
    ),
    "terfi_istatistik": (
        "guild_id", "user_id", "mesaj", "ses_saniye", "davet", "kayit", "ses_baslangic"
    ),
    "terfi_rol_hedefleri": (
        "guild_id", "role_id", "mesaj_hedef", "ses_hedef", "davet_hedef", "kayit_hedef"
    ),
    "terfi_kisi_hedefleri": (
        "guild_id", "user_id", "mesaj_hedef", "ses_hedef", "davet_hedef", "kayit_hedef"
    ),
    # Uyarılar: id otomatik yeniden oluşturulur, böylece başka sunucularla ID çakışmaz.
    "uyarilar": ("guild_id", "user_id", "moderator_id", "sebep", "tarih"),
    # Log, ticket, filtre, giriş ve kayıt ayarları
    "log_ayarlar": ("guild_id", "kanal_id"),
    "ticket_ayarlar": ("guild_id", "transcript_channel_id", "staff_role_ids"),
    "filtre_ayarlar": ("guild_id", "reklam_mesaji", "kufur_mesaji"),
    "giris_ayarlar": ("guild_id", "role_id", "allowed_channel_ids"),
    "giris_uye_rolleri": ("guild_id", "role_id"),
    "kayit_ayarlar": ("guild_id", "kanal_id", "kayitli_rol_id", "kayitsiz_rol_id"),
    "kayit_gorunur_kanallar": ("guild_id", "kanal_ids"),
    # Hedefler: dahili id yeniden oluşturulur; kanal/mesaj bağlantıları korunur.
    "hedefler": (
        "guild_id", "kanal_id", "mesaj_id", "hedef", "tur",
        "user_id", "sayac", "son_bar", "aktif"
    ),
    # XP ayarları, kullanıcı seviyeleri ve otomatik rol eşleştirmeleri
    "xp_ayarlar": (
        "guild_id", "xp_min", "xp_max", "cooldown_seconds",
        "daily_min", "daily_max", "daily_cooldown", "level_channel_id", "aktif"
    ),
    "xp_kullanicilar": (
        "guild_id", "user_id", "xp", "level", "last_message", "last_daily"
    ),
    "xp_roller": ("guild_id", "level", "role_id"),
    # Ekonomi ayarları, üye bakiyeleri ve işlem geçmişi
    "ekonomi_ayarlar": (
        "guild_id", "para_adi", "gunluk_min", "gunluk_max",
        "calisma_min", "calisma_max", "gunluk_sure", "calisma_sure",
        "baslangic_bakiye"
    ),
    "ekonomi_kullanicilar": (
        "guild_id", "user_id", "bakiye", "banka", "son_gunluk", "son_calisma"
    ),
    "ekonomi_islemler": (
        "guild_id", "gonderen_id", "alici_id", "miktar", "tur", "tarih"
    ),
    # Market ürünleri, kişisel kodlar ve yetkili talepleri
    "market_ayarlar": ("guild_id", "talep_kanal_id"),
    "market_urunler": ("guild_id", "ad", "aciklama", "fiyat", "tur", "rol_id", "sure_gun", "aktif"),
    "market_kodlar": ("kod", "guild_id", "user_id", "urun_id", "satin_alma", "kullanildi", "durum"),
    "market_talepler": ("guild_id", "user_id", "urun_id", "kod", "kanal_id", "mesaj_id", "durum", "olusturma", "karar_veren", "karar_zamani"),
    "market_sureli_roller": ("guild_id", "user_id", "role_id", "expires_at"),
    # Jail ayarları ve üyelerin geri yüklenebilir eski rol/kanal izinleri
    "jail_ayarlar": ("guild_id", "role_id", "channel_id"),
    "jail_uyeler": ("guild_id", "user_id", "role_ids", "overwrite_data"),
}

# Sürüm 4 ve öncesi yedekler ekonomi tablolarını içermez.
EKONOMI_YEDEK_TABLOLARI = {
    "ekonomi_ayarlar", "ekonomi_kullanicilar", "ekonomi_islemler"
}
SUNUCU_YEDEK_TABLOLARI_V5 = {
    name: columns for name, columns in SUNUCU_YEDEK_TABLOLARI.items()
    if name not in {"jail_ayarlar", "jail_uyeler"}
}
SUNUCU_YEDEK_TABLOLARI_V4 = {
    name: columns for name, columns in SUNUCU_YEDEK_TABLOLARI_V5.items()
    if name not in EKONOMI_YEDEK_TABLOLARI
}

# Sürüm 3 yedekleri XP ayarlarında henüz aktif alanı içermez.
SUNUCU_YEDEK_TABLOLARI_V3 = {
    name: (columns[:-1] if name == "xp_ayarlar" else columns)
    for name, columns in SUNUCU_YEDEK_TABLOLARI_V4.items()
}

# Sürüm 2 yedekleri XP tabloları içermediği için geriye dönük desteklenir.
SUNUCU_YEDEK_TABLOLARI_V2 = {
    name: columns for name, columns in SUNUCU_YEDEK_TABLOLARI_V4.items()
    if name not in {"xp_ayarlar", "xp_kullanicilar", "xp_roller"}
}

def terfi_yedek_yetkili_mi(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and (
            interaction.user.id == interaction.guild.owner_id
            or str(getattr(interaction.user, "name", "")).casefold()
            == BOT_AUTHORIZED_USERNAME.casefold()
        )
    )

@bot.tree.command(name="yedek", description="Sunucu ayarlarını, XP, ekonomi ve kayıt verilerini JSON dosyasına yedekler.")
async def terfi_yedek(interaction: discord.Interaction):
    if not terfi_yedek_yetkili_mi(interaction):
        await interaction.response.send_message(
            "Bu komutu yalnızca sunucu kurucusu veya chakabaniastokens kullanabilir.",
            ephemeral=True,
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "Bu komut bir sunucuda kullanılmalı.", ephemeral=True
        )
        return

    # Büyük yedeklerde Discord'un ilk yanıt için verdiği 3 saniyelik süre aşılmasın.
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild_id = interaction.guild.id
    try:
        tables = {}
        for table, columns in SUNUCU_YEDEK_TABLOLARI.items():
            schema = db.execute(f"PRAGMA table_info({table})").fetchall()
            existing_columns = {column[1] for column in schema}
            missing_columns = [column for column in columns if column not in existing_columns]
            if not schema:
                raise RuntimeError(f"{table} tablosu veritabanında bulunamadı.")
            if missing_columns:
                raise RuntimeError(
                    f"{table} tablosunda eksik sütun(lar): {', '.join(missing_columns)}"
                )

            selected = ", ".join(columns)
            rows = db.execute(
                f"SELECT {selected} FROM {table} WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
            serialized_rows = []
            for result in rows:
                row = dict(zip(columns, result))
                if table == "terfi_istatistik":
                    row["ses_baslangic"] = None
                serialized_rows.append(row)
            tables[table] = serialized_rows

        payload = {
            "format": "GAMESTURKEY_SERVER_BACKUP",
            "version": SUNUCU_YEDEK_SURUMU,
            "guild_id": guild_id,
            "guild_name": interaction.guild.name,
            "created_at": datetime.now(TURKEY).isoformat(),
            "tables": tables,
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        # /yükle ile aynı sınırı uygula; oluşturulan her yedek geri yüklenebilir boyutta kalsın.
        upload_limit = min(getattr(interaction.guild, "filesize_limit", 8 * 1024 * 1024) or (8 * 1024 * 1024), 8 * 1024 * 1024)
        if len(raw) > upload_limit:
            await interaction.followup.send(
                f"Yedek hazırlandı ancak dosya Discord yükleme sınırını aşıyor "
                f"({len(raw) / (1024 * 1024):.2f} MB / {upload_limit / (1024 * 1024):.2f} MB). "
                "Veriler değiştirilmedi.",
                ephemeral=True,
            )
            return

        filename = f"sunucu-yedek-{guild_id}-{datetime.now(TURKEY).strftime('%Y%m%d-%H%M%S')}.json"
        file = discord.File(io.BytesIO(raw), filename=filename)
        await interaction.followup.send(
            "Sunucu yedeğin hazır. XP, ekonomi bakiyeleri, banka kayıtları, ayarlar ve terfi verileri dosyaya eklendi. "
            "Dosyayı güvenli bir yerde sakla; /yükle ile geri yükleyebilirsin.",
            file=file,
            ephemeral=True,
        )
    except Exception:
        logging.exception("Sunucu yedeği oluşturulamadı (guild_id=%s)", guild_id)
        await interaction.followup.send(
            "Yedek oluşturulamadı. Hata ayrıntısı Railway günlüklerine kaydedildi. "
            "Veritabanındaki mevcut kayıtlar değiştirilmedi.",
            ephemeral=True,
        )

@bot.tree.command(name="yükle", description="Daha önce /yedek ile oluşturulan sunucu JSON yedeğini geri yükler.")
@app_commands.describe(dosya=" /yedek komutunun oluşturduğu .json dosyası")
async def terfi_yedek_yukle(
    interaction: discord.Interaction,
    dosya: discord.Attachment,
):
    if not terfi_yedek_yetkili_mi(interaction):
        await interaction.response.send_message(
            "Bu komutu yalnızca sunucu kurucusu veya chakabaniastokens kullanabilir.",
            ephemeral=True,
        )
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "Bu komut bir sunucuda kullanılmalı.", ephemeral=True
        )
        return
    if not dosya.filename.lower().endswith(".json") or dosya.size > 8 * 1024 * 1024:
        await interaction.response.send_message(
            "Yalnızca 8 MB veya daha küçük bir .json yedek dosyası yükleyebilirsin.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        raw = await dosya.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("tables"), dict):
            await interaction.followup.send(
                "JSON dosyası geçerli bir yedek biçiminde değil. Mevcut veriler değiştirilmedi.",
                ephemeral=True,
            )
            return

        # Hem eski terfi-only v1 yedeğini hem yeni tam sunucu v2 yedeğini destekle.
        eski_terfi_tablolari = {
            "terfi_ayarlar": (
                "guild_id", "yetkili_rol_ids", "max_rol_id", "kayit_rol_ids"
            ),
            "terfi_istatistik": (
                "guild_id", "user_id", "mesaj", "ses_saniye", "davet", "kayit", "ses_baslangic"
            ),
            "terfi_rol_hedefleri": (
                "guild_id", "role_id", "mesaj_hedef", "ses_hedef", "davet_hedef", "kayit_hedef"
            ),
            "terfi_kisi_hedefleri": (
                "guild_id", "user_id", "mesaj_hedef", "ses_hedef", "davet_hedef", "kayit_hedef"
            ),
        }
        if (
            payload.get("format") == "GAMESTURKEY_SERVER_BACKUP"
            and payload.get("version") == SUNUCU_YEDEK_SURUMU
        ):
            yedek_tablolari = SUNUCU_YEDEK_TABLOLARI
        elif (
            payload.get("format") == "GAMESTURKEY_SERVER_BACKUP"
            and payload.get("version") == 5
        ):
            yedek_tablolari = SUNUCU_YEDEK_TABLOLARI_V5
        elif (
            payload.get("format") == "GAMESTURKEY_SERVER_BACKUP"
            and payload.get("version") == 4
        ):
            yedek_tablolari = SUNUCU_YEDEK_TABLOLARI_V4
        elif (
            payload.get("format") == "GAMESTURKEY_SERVER_BACKUP"
            and payload.get("version") == 3
        ):
            yedek_tablolari = SUNUCU_YEDEK_TABLOLARI_V3
        elif (
            payload.get("format") == "GAMESTURKEY_SERVER_BACKUP"
            and payload.get("version") == 2
        ):
            yedek_tablolari = SUNUCU_YEDEK_TABLOLARI_V2
        elif (
            payload.get("format") == "GAMESTURKEY_TERFI_BACKUP"
            and payload.get("version") == 1
        ):
            yedek_tablolari = eski_terfi_tablolari
        else:
            await interaction.followup.send(
                "Bu JSON desteklenen bir GAMESTURKEY yedeği değil. "
                "Eski terfi yedekleri ve yeni sunucu yedekleri desteklenir; mevcut veriler değiştirilmedi.",
                ephemeral=True,
            )
            return

        tables = payload["tables"]
        if set(tables.keys()) != set(yedek_tablolari.keys()):
            await interaction.followup.send(
                "Yedekte beklenen tablolar eksik veya farklı. Mevcut veriler değiştirilmedi.",
                ephemeral=True,
            )
            return

        # Tüm satırları işlem başlamadan doğrula; yedekteki eski sunucu ID'si hedefe uyarlanır.
        for table, columns in yedek_tablolari.items():
            rows = tables[table]
            if not isinstance(rows, list):
                raise ValueError(f"{table}: satır listesi geçersiz")
            for row in rows:
                if not isinstance(row, dict) or set(row.keys()) != set(columns):
                    raise ValueError(f"{table}: satır yapısı geçersiz")

            # Ekonomi verilerini işlem başlamadan kontrol et. Hatalı dosya
            # hiçbir tabloya yazılmadan reddedilir.
            if table == "ekonomi_ayarlar":
                if len(rows) > 1:
                    raise ValueError("ekonomi_ayarlar: yinelenen sunucu ayarı")
                for row in rows:
                    if (
                        not isinstance(row["para_adi"], str)
                        or not 1 <= len(row["para_adi"].strip()) <= 16
                        or any(not isinstance(row[k], int) or isinstance(row[k], bool) for k in
                               ("gunluk_min", "gunluk_max", "calisma_min", "calisma_max",
                                "gunluk_sure", "calisma_sure", "baslangic_bakiye"))
                        or row["gunluk_min"] < 1
                        or row["gunluk_max"] < row["gunluk_min"]
                        or row["calisma_min"] < 1
                        or row["calisma_max"] < row["calisma_min"]
                        or row["gunluk_sure"] < 1
                        or row["calisma_sure"] < 1
                        or row["baslangic_bakiye"] < 0
                    ):
                        raise ValueError("ekonomi_ayarlar: değerler geçersiz")
            elif table == "ekonomi_kullanicilar":
                seen_users = set()
                for row in rows:
                    uid = row["user_id"]
                    if (
                        not isinstance(uid, int) or isinstance(uid, bool)
                        or uid in seen_users
                        or not isinstance(row["bakiye"], int) or isinstance(row["bakiye"], bool)
                        or not isinstance(row["banka"], int) or isinstance(row["banka"], bool)
                        or row["bakiye"] < 0 or row["banka"] < 0
                        or any(value is not None and
                               (not isinstance(value, (int, float)) or isinstance(value, bool))
                               for value in (row["son_gunluk"], row["son_calisma"]))
                    ):
                        raise ValueError("ekonomi_kullanicilar: değerler geçersiz")
                    seen_users.add(uid)
            elif table == "ekonomi_islemler":
                for row in rows:
                    if (
                        not isinstance(row["miktar"], int) or isinstance(row["miktar"], bool)
                        or row["miktar"] <= 0
                        or not isinstance(row["tur"], str) or not row["tur"]
                        or not isinstance(row["tarih"], (int, float))
                        or isinstance(row["tarih"], bool)
                        or any(value is not None and
                               (not isinstance(value, int) or isinstance(value, bool))
                               for value in (row["gonderen_id"], row["alici_id"]))
                    ):
                        raise ValueError("ekonomi_islemler: değerler geçersiz")

        # Tek transaction: yalnızca seçilen yedek türündeki tabloların hedef sunucu kayıtları değişir.
        db.execute("BEGIN IMMEDIATE")
        try:
            for table, columns in yedek_tablolari.items():
                cursor.execute(f"DELETE FROM {table} WHERE guild_id = ?", (interaction.guild.id,))
                if tables[table]:
                    placeholders = ", ".join("?" for _ in columns)
                    column_sql = ", ".join(columns)
                    values = [
                        tuple(interaction.guild.id if column == "guild_id" else row[column] for column in columns)
                        for row in tables[table]
                    ]
                    cursor.executemany(
                        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
                        values,
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise

        toplam = sum(len(tables[t]) for t in yedek_tablolari)
        await interaction.followup.send(
            f"Yedek geri yüklendi. {toplam} kayıt satırı işlendi. "
            "Bu sunucunun XP, ekonomi, terfi ve ayar kayıtları yedekteki verilerle değiştirildi; "
            "diğer sunucuların verileri etkilenmedi.",
            ephemeral=True,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, KeyError, TypeError):
        await interaction.followup.send(
            "Yedek dosyası okunamadı veya biçimi geçersiz. Mevcut veriler değiştirilmedi.",
            ephemeral=True,
        )
    except Exception:
        db.rollback()
        await interaction.followup.send(
            "Yedek geri yüklenirken hata oluştu. İşlem geri alındı; mevcut veriler korunmaya çalışıldı.",
            ephemeral=True,
        )


def terfi_yetkili_kontrol(interaction):
    guild = interaction.guild
    return bool(guild and (
        interaction.user.id == guild.owner_id
        or str(getattr(interaction.user, "name", "")).casefold() == BOT_AUTHORIZED_USERNAME.casefold()
        or interaction.user.guild_permissions.administrator
    ))

def terfi_sahip_kontrol(interaction):
    guild = interaction.guild
    return bool(guild and (
        interaction.user.id == guild.owner_id
        or str(getattr(interaction.user, "name", "")).casefold() == BOT_AUTHORIZED_USERNAME.casefold()
    ))

def terfi_ayarlarini_getir(guild_id):
    cursor.execute("SELECT yetkili_rol_ids, max_rol_id, kayit_rol_ids FROM terfi_ayarlar WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    if not row:
        return {"yetkili": [], "max": [], "kayit": []}
    try:
        yetkili = json.loads(row[0] or "[]")
    except (TypeError, json.JSONDecodeError):
        yetkili = []
    try:
        kayit = json.loads(row[2] or "[]")
    except (TypeError, json.JSONDecodeError):
        kayit = []
    # Eski sürümlerde maksimum rol tek bir sayı olarak saklanıyordu.
    try:
        max_roller = json.loads(row[1]) if isinstance(row[1], str) and row[1].startswith("[") else ([int(row[1])] if row[1] else [])
        if not isinstance(max_roller, list):
            max_roller = [int(max_roller)] if max_roller else []
        max_roller = [int(rol_id) for rol_id in max_roller]
    except (TypeError, ValueError, json.JSONDecodeError):
        max_roller = [int(row[1])] if row[1] else []
    return {"yetkili": yetkili, "max": max_roller, "kayit": kayit}

def terfi_istatistik_getir(guild_id, user_id):
    cursor.execute("INSERT OR IGNORE INTO terfi_istatistik (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id))
    db.commit()
    cursor.execute("SELECT mesaj, ses_saniye, davet, kayit, ses_baslangic FROM terfi_istatistik WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    row = cursor.fetchone()
    mesaj, ses, davet, kayit, baslangic = row
    if baslangic is not None:
        ses += max(0, int(datetime.now().timestamp() - baslangic))
    return {"mesaj": mesaj, "ses": ses, "davet": davet, "kayit": kayit}

def terfi_istatistik_arttir(guild_id, user_id, tur, miktar):
    alanlar = {"mesaj": "mesaj", "ses": "ses_saniye", "davet": "davet", "kayit": "kayit"}
    if tur not in alanlar:
        return False
    cursor.execute("INSERT OR IGNORE INTO terfi_istatistik (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id))
    alan = alanlar[tur]
    deger = miktar * 3600 if tur == "ses" else miktar
    cursor.execute(f"UPDATE terfi_istatistik SET {alan} = MAX(0, {alan} + ?) WHERE guild_id = ? AND user_id = ?", (deger, guild_id, user_id))
    db.commit()
    return True

def terfi_bar(sayi, hedef, uzunluk=12):
    oran = min(1.0, max(0.0, sayi / hedef))
    dolu = int(oran * uzunluk)
    return "█" * dolu + "░" * (uzunluk - dolu)

def terfi_ilerleme_satiri(etiket, sayi, hedef, birim=""):
    return f"**{etiket}:** {min(sayi, hedef)}/{hedef}{birim}  {terfi_bar(sayi, hedef)}"


def terfi_png_metni(metin):
    # Pillow'un varsayılan fontunda bulunmayan emoji/Unicode karakterleri güvenli hale getir.
    return str(metin).encode("latin-1", "replace").decode("latin-1")

def terfi_png_olustur(kisi, rol_adi, ist, kayit_goster=False, maksimum=False, hedefler=None):
    """Büyük ve okunaklı yazılı terfi kartı oluşturur; sunucu adı içermez."""
    hedefler = hedefler or {"mesaj": 1000, "ses": 25, "davet": 25, "kayit": 50}
    satirlar = [
        ("MESAJ", ist["mesaj"], max(1, hedefler.get("mesaj", 1000)), "mesaj"),
        ("SES", ist["ses"] // 3600, max(1, hedefler.get("ses", 25)), "saat"),
        ("DAVET", ist["davet"], max(1, hedefler.get("davet", 25)), "davet"),
    ]
    if kayit_goster:
        satirlar.append(("KAYIT", ist["kayit"], max(1, hedefler.get("kayit", 50)), "kayıt"))

    genislik, satir_yukseklik = 1200, 115
    yukseklik = 260 + len(satirlar) * satir_yukseklik + 55
    img = Image.new("RGB", (genislik, yukseklik), (15, 18, 32))
    draw = ImageDraw.Draw(img)
    try:
        baslik_font = ImageFont.truetype("DejaVuSans.ttf", 32)
        ad_font = ImageFont.truetype("DejaVuSans.ttf", 40)
        durum_font = ImageFont.truetype("DejaVuSans.ttf", 25)
        satir_font = ImageFont.truetype("DejaVuSans.ttf", 27)
        alt_font = ImageFont.truetype("DejaVuSans.ttf", 20)
    except OSError:
        baslik_font = ImageFont.load_default()
        ad_font = ImageFont.load_default()
        durum_font = satir_font = alt_font = baslik_font

    draw.rounded_rectangle((24, 24, genislik - 24, yukseklik - 24), radius=28, fill=(25, 30, 50), outline=(75, 86, 130), width=3)
    draw.text((58, 48), terfi_png_metni("TERFI ILERLEMESI"), font=baslik_font, fill=(160, 178, 255))
    draw.text((58, 95), terfi_png_metni(str(kisi.display_name)[:42]), font=ad_font, fill=(248, 249, 255))
    durum = "MAKSIMUM RUTBE TAMAMLANDI" if maksimum else "MEVCUT RUTBE: " + str(rol_adi)
    draw.text((58, 153), terfi_png_metni(durum[:80]), font=durum_font, fill=(255, 210, 105) if maksimum else (184, 195, 225))
    y = 215
    for ad, sayi, hedef, birim in satirlar:
        yuzde = max(0, min(100, int((sayi / hedef) * 100)))
        draw.text((60, y), terfi_png_metni(f"{ad}   {min(sayi, hedef)}/{hedef} {birim}   %{yuzde}"), font=satir_font, fill=(238, 241, 255))
        bx1, by1, bx2, by2 = 60, y + 43, genislik - 60, y + 69
        draw.rounded_rectangle((bx1, by1, bx2, by2), radius=12, fill=(47, 54, 78))
        dolu = int((bx2 - bx1) * yuzde / 100)
        if dolu:
            draw.rounded_rectangle((bx1, by1, bx1 + dolu, by2), radius=12, fill=(116, 133, 255))
        y += satir_yukseklik
    hedef_ozet = f"Hedefler: {hedefler.get('mesaj', 1000)} mesaj • {hedefler.get('ses', 25)} saat ses • {hedefler.get('davet', 25)} davet"
    if kayit_goster:
        hedef_ozet += f" • {hedefler.get('kayit', 50)} kayıt"
    draw.text((60, yukseklik - 60), terfi_png_metni(hedef_ozet), font=alt_font, fill=(145, 155, 184))
    output = io.BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output

class TerfiAyarPaneli(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def _izin(self, interaction):
        if interaction.guild is None or interaction.guild.id != self.guild_id or not terfi_yetkili_kontrol(interaction):
            if not interaction.response.is_done():
                await interaction.response.send_message("Bu paneli yalnızca sunucu sahibi, Yönetici veya chakabaniastokens kullanabilir.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Terfi sistemine dahil yetkili rollerini seç", min_values=1, max_values=10, custom_id="terfi:yetkili-roller")
    async def yetkili_roller(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not await self._izin(interaction):
            return
        ayar = terfi_ayarlarini_getir(self.guild_id)
        ayar["yetkili"] = [r.id for r in select.values]
        cursor.execute(
            "INSERT INTO terfi_ayarlar (guild_id, yetkili_rol_ids, max_rol_id, kayit_rol_ids) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET yetkili_rol_ids=excluded.yetkili_rol_ids",
            (self.guild_id, json.dumps(ayar["yetkili"]), json.dumps(ayar["max"]) if ayar["max"] else None, json.dumps(ayar["kayit"]))
        )
        db.commit()
        await interaction.response.send_message("Terfi takibine dahil yetkili rolleri kaydedildi.", ephemeral=True)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Maksimum rütbeye ulaşan roller", min_values=1, max_values=10, custom_id="terfi:max-rol")
    async def max_rol(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not await self._izin(interaction):
            return
        ayar = terfi_ayarlarini_getir(self.guild_id)
        ayar["max"] = [r.id for r in select.values]
        cursor.execute(
            "INSERT INTO terfi_ayarlar (guild_id, yetkili_rol_ids, max_rol_id, kayit_rol_ids) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET max_rol_id=excluded.max_rol_id",
            (self.guild_id, json.dumps(ayar["yetkili"]), json.dumps(ayar["max"]), json.dumps(ayar["kayit"]))
        )
        db.commit()
        await interaction.response.send_message("Maksimum rütbe rolleri kaydedildi: " + ", ".join(r.mention for r in select.values), ephemeral=True)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Kayıt barı görünecek rolleri seç", min_values=1, max_values=10, custom_id="terfi:kayit-roller")
    async def kayit_roller(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not await self._izin(interaction):
            return
        ayar = terfi_ayarlarini_getir(self.guild_id)
        ayar["kayit"] = [r.id for r in select.values]
        cursor.execute(
            "INSERT INTO terfi_ayarlar (guild_id, yetkili_rol_ids, max_rol_id, kayit_rol_ids) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET kayit_rol_ids=excluded.kayit_rol_ids",
            (self.guild_id, json.dumps(ayar["yetkili"]), json.dumps(ayar["max"]) if ayar["max"] else None, json.dumps(ayar["kayit"]))
        )
        db.commit()
        await interaction.response.send_message("Kayıt ilerleme çubuğu yalnızca seçtiğin rollere sahip kişilere gösterilecek.", ephemeral=True)

@bot.tree.command(name="terfi-panel", description="Yetkili terfi, maksimum rol ve kayıt istatistiği rollerini ayarlar.")
async def terfi_panel(interaction: discord.Interaction):
    if interaction.guild is None or not terfi_yetkili_kontrol(interaction):
        await interaction.response.send_message("Bu panel için sunucu sahibi, Yönetici veya chakabaniastokens olmalısın.", ephemeral=True)
        return
    ayar = terfi_ayarlarini_getir(interaction.guild.id)
    yetkili_metni = ", ".join(interaction.guild.get_role(i).mention for i in ayar["yetkili"] if interaction.guild.get_role(i)) or "Henüz seçilmedi"
    max_roller = [interaction.guild.get_role(i) for i in (ayar.get("max") or [])]
    max_roller = [r for r in max_roller if r is not None]
    kayit_metni = ", ".join(interaction.guild.get_role(i).mention for i in ayar["kayit"] if interaction.guild.get_role(i)) or "Henüz seçilmedi"
    embed = discord.Embed(
        title="Yetkili Terfi Ayar Paneli",
        description="Aşağıdaki menülerden terfi takibine girecek rolleri, maksimum rolü ve kayıt barı görecek rolleri seç. Hedefler: 1000 mesaj, 25 saat ses, 25 davet; kayıt hedefi 50 kayıt.",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Takip edilen yetkili rolleri", value=yetkili_metni, inline=False)
    embed.add_field(name="Maksimum rütbe rolleri", value=", ".join(r.mention for r in max_roller) or "Henüz seçilmedi", inline=False)
    embed.add_field(name="Kayıt barı görecek roller", value=kayit_metni, inline=False)
    await interaction.response.send_message(embed=embed, view=TerfiAyarPaneli(interaction.guild.id), ephemeral=True)

@bot.tree.command(name="ekleterfi", description="Bir kişinin terfi ilerlemesine miktar ekler.")
@app_commands.describe(
    kisi="İlerlemesi artırılacak kişi",
    mesaj="Eklenecek mesaj sayısı",
    ses_saati="Eklenecek ses saati",
    davet="Eklenecek davet sayısı",
    kayit="Eklenecek kayıt sayısı"
)
async def ekleterfi(
    interaction: discord.Interaction,
    kisi: discord.Member,
    mesaj: app_commands.Range[int, 0, 1000000] = 0,
    ses_saati: app_commands.Range[int, 0, 1000000] = 0,
    davet: app_commands.Range[int, 0, 1000000] = 0,
    kayit: app_commands.Range[int, 0, 1000000] = 0
):
    if interaction.guild is None or not terfi_yetkili_kontrol(interaction):
        await interaction.response.send_message("Bu komut için sunucu sahibi veya Yönetici yetkisi gerekir.", ephemeral=True)
        return
    if kisi.bot:
        await interaction.response.send_message("Bir bot yerine gerçek bir üye seçmelisin.", ephemeral=True)
        return
    if mesaj == 0 and ses_saati == 0 and davet == 0 and kayit == 0:
        await interaction.response.send_message("En az bir alana eklenecek miktarı 1 veya daha fazla gir.", ephemeral=True)
        return

    for tur, miktar in (("mesaj", mesaj), ("ses", ses_saati), ("davet", davet), ("kayit", kayit)):
        if miktar > 0:
            terfi_istatistik_arttir(interaction.guild.id, kisi.id, tur, miktar)

    ist = terfi_istatistik_getir(interaction.guild.id, kisi.id)
    await interaction.response.send_message(
        f"{kisi.mention} için terfi ilerlemesi artırıldı. Eklenen: **+{mesaj} mesaj • +{ses_saati} saat ses • +{davet} davet • +{kayit} kayıt**.\n"
        f"Güncel ilerleme: **{ist['mesaj']} mesaj • {ist['ses'] // 3600} saat ses • {ist['davet']} davet • {ist['kayit']} kayıt**. Hedefler değişmedi.",
        ephemeral=True
    )


@bot.tree.command(name="silterfi", description="Bir kişinin terfi ilerlemesinden miktar düşer.")
@app_commands.describe(
    kisi="İlerlemesi azaltılacak kişi",
    mesaj="Silinecek mesaj sayısı",
    ses_saati="Silinecek ses saati",
    davet="Silinecek davet sayısı",
    kayit="Silinecek kayıt sayısı"
)
async def silterfi(
    interaction: discord.Interaction,
    kisi: discord.Member,
    mesaj: app_commands.Range[int, 0, 1000000] = 0,
    ses_saati: app_commands.Range[int, 0, 1000000] = 0,
    davet: app_commands.Range[int, 0, 1000000] = 0,
    kayit: app_commands.Range[int, 0, 1000000] = 0
):
    if interaction.guild is None or not terfi_yetkili_kontrol(interaction):
        await interaction.response.send_message("Bu komut için sunucu sahibi veya Yönetici yetkisi gerekir.", ephemeral=True)
        return
    if kisi.bot:
        await interaction.response.send_message("Bir bot yerine gerçek bir üye seçmelisin.", ephemeral=True)
        return
    if mesaj == 0 and ses_saati == 0 and davet == 0 and kayit == 0:
        await interaction.response.send_message("En az bir alandan silinecek miktarı 1 veya daha fazla gir.", ephemeral=True)
        return

    for tur, miktar in (("mesaj", mesaj), ("ses", ses_saati), ("davet", davet), ("kayit", kayit)):
        if miktar > 0:
            terfi_istatistik_arttir(interaction.guild.id, kisi.id, tur, -miktar)

    ist = terfi_istatistik_getir(interaction.guild.id, kisi.id)
    await interaction.response.send_message(
        f"{kisi.mention} için terfi ilerlemesi azaltıldı. Silinen: **-{mesaj} mesaj • -{ses_saati} saat ses • -{davet} davet • -{kayit} kayıt**.\n"
        f"Güncel ilerleme: **{ist['mesaj']} mesaj • {ist['ses'] // 3600} saat ses • {ist['davet']} davet • {ist['kayit']} kayıt**. Sayaçlar 0'ın altına düşmez; hedefler ve panel ayarları değişmez.",
        ephemeral=True
    )

@bot.tree.command(name="terfi", description="Kendi terfi ilerlemeni gösterir.")
@app_commands.describe(kisi="İsteğe bağlı; yalnızca kendini seçebilirsin")
async def terfi(interaction: discord.Interaction, kisi: discord.Member | None = None):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut bir sunucuda kullanılmalı.", ephemeral=True)
        return
    if kisi is not None and kisi.id != interaction.user.id:
        await interaction.response.send_message("Başka üyelerin terfi bilgilerini görüntüleyemezsin. Yalnızca kendi bilgilerini görebilirsin.", ephemeral=True)
        return
    kisi = interaction.user
    ayar = terfi_ayarlarini_getir(interaction.guild.id)
    if not ayar["yetkili"]:
        await interaction.response.send_message("Terfi sistemi henüz ayarlanmamış. Yönetici önce /terfi-panel üzerinden takip edilecek yetkili rollerini seçmeli.", ephemeral=True)
        return
    yetkili_roller = [interaction.guild.get_role(i) for i in ayar["yetkili"]]
    yetkili_roller = [r for r in yetkili_roller if r is not None]
    kisi_yetkili_rolleri = [r for r in kisi.roles if r in yetkili_roller]
    max_roller = [interaction.guild.get_role(i) for i in (ayar.get("max") or [])]
    max_roller = [r for r in max_roller if r is not None]
    kisi_max_rolu = next((r for r in max_roller if r in kisi.roles), None)
    if not kisi_yetkili_rolleri and not kisi_max_rolu:
        await interaction.response.send_message("Bu kişide terfi panelinde seçilmiş yetkili rolü yok.", ephemeral=True)
        return

    ist = terfi_istatistik_getir(interaction.guild.id, kisi.id)
    kayit_goster = bool(ayar["kayit"] and any(rol.id in ayar["kayit"] for rol in kisi.roles))
    mevcut_rol = max(kisi_yetkili_rolleri, key=lambda r: r.position) if kisi_yetkili_rolleri else kisi_max_rolu
    rol_adi = mevcut_rol.name if mevcut_rol else "Yetkili"
    hedefler = terfi_rol_hedefi_getir(interaction.guild.id, mevcut_rol.id) if mevcut_rol else {"mesaj": 1000, "ses": 25, "davet": 25, "kayit": 50}

    embed = discord.Embed(
        title=f"{kisi.display_name} — Terfi İlerlemesi",
        description=("Maksimum rütbe tamamlandı." if kisi_max_rolu else f"Mevcut rütbe: **{rol_adi}**"),
        color=discord.Color.gold() if kisi_max_rolu else discord.Color.blurple()
    )
    embed.set_thumbnail(url=kisi.display_avatar.url)

    satirlar = [
        ("Mesaj", ist["mesaj"], max(1, hedefler.get("mesaj", 1000)), ""),
        ("Ses", ist["ses"] // 3600, max(1, hedefler.get("ses", 25)), " saat"),
        ("Davet", ist["davet"], max(1, hedefler.get("davet", 25)), " davet"),
    ]
    if kayit_goster:
        satirlar.append(("Kayıt", ist["kayit"], max(1, hedefler.get("kayit", 50)), " kayıt"))

    for ad, sayi, hedef, birim in satirlar:
        yuzde = min(100, int((sayi / hedef) * 100))
        embed.add_field(
            name=f"{ad} · {min(sayi, hedef)}/{hedef}{birim} (%{yuzde})",
            value=terfi_bar(sayi, hedef, uzunluk=18),
            inline=False
        )

    cursor.execute(
        "SELECT role_id, started_at FROM terfi_rol_sureleri WHERE guild_id = ? AND user_id = ?",
        (interaction.guild.id, kisi.id)
    )
    sure_kaydi = cursor.fetchone()
    if sure_kaydi and mevcut_rol and sure_kaydi[0] == mevcut_rol.id:
        toplam_saniye = max(0, int(datetime.now().timestamp() - sure_kaydi[1]))
        gun, kalan = divmod(toplam_saniye, 86400)
        saat, kalan = divmod(kalan, 3600)
        dakika = kalan // 60
        sure_metni = f"{gun} gün {saat} saat {dakika} dakika"
    elif mevcut_rol:
        # Bu özellik eklenmeden önce verilmiş rollerin geçmiş süresi bilinemez.
        cursor.execute(
            "INSERT OR IGNORE INTO terfi_rol_sureleri (guild_id, user_id, role_id, started_at) VALUES (?, ?, ?, ?)",
            (interaction.guild.id, kisi.id, mevcut_rol.id, datetime.now().timestamp())
        )
        db.commit()
        sure_metni = "Süre takibi şimdi başladı; önceki süre bilinmiyor."
    else:
        sure_metni = "Kayıtlı yetkili rolü bulunamadı."
    embed.add_field(name="Mevcut yetkili rolünde kalma süresi", value=sure_metni, inline=False)
    embed.set_footer(text=f"Üye: {kisi} • Hedefler role göre belirlenir")
    await interaction.response.send_message(embed=embed)

@bot.command(name="terfi")
async def prefix_terfi(ctx, kisi: discord.Member = None):
    if ctx.guild is None:
        return await ctx.send("Bu komut yalnızca sunucuda kullanılabilir.")
    if kisi is not None and kisi.id != ctx.author.id:
        return await ctx.send("Başka üyelerin terfi bilgilerini görüntüleyemezsin. Yalnızca kendi bilgilerini görebilirsin.")
    kisi = ctx.author
    ayar = terfi_ayarlarini_getir(ctx.guild.id)
    if not ayar["yetkili"]:
        return await ctx.send("Terfi sistemi henüz ayarlanmamış. Yönetici önce /terfi-panel üzerinden takip edilecek yetkili rollerini seçmeli.")
    yetkili_roller = [ctx.guild.get_role(i) for i in ayar["yetkili"]]
    yetkili_roller = [r for r in yetkili_roller if r is not None]
    kisi_yetkili_rolleri = [r for r in kisi.roles if r in yetkili_roller]
    max_roller = [ctx.guild.get_role(i) for i in (ayar.get("max") or [])]
    max_roller = [r for r in max_roller if r is not None]
    kisi_max_rolu = next((r for r in max_roller if r in kisi.roles), None)
    if not kisi_yetkili_rolleri and not kisi_max_rolu:
        return await ctx.send("Sende terfi panelinde seçilmiş bir yetkili rolü bulunmuyor.")
    ist = terfi_istatistik_getir(ctx.guild.id, kisi.id)
    kayit_goster = bool(ayar["kayit"] and any(rol.id in ayar["kayit"] for rol in kisi.roles))
    mevcut_rol = max(kisi_yetkili_rolleri, key=lambda r: r.position) if kisi_yetkili_rolleri else kisi_max_rolu
    hedefler = terfi_rol_hedefi_getir(ctx.guild.id, mevcut_rol.id) if mevcut_rol else {"mesaj": 1000, "ses": 25, "davet": 25, "kayit": 50}
    embed = discord.Embed(
        title=f"{kisi.display_name} — Terfi İlerlemesi",
        description=("Maksimum rütbe tamamlandı." if kisi_max_rolu else f"Mevcut rütbe: **{mevcut_rol.name}**"),
        color=discord.Color.gold() if kisi_max_rolu else discord.Color.blurple()
    )
    embed.set_thumbnail(url=kisi.display_avatar.url)
    satirlar = [
        ("Mesaj", ist["mesaj"], max(1, hedefler.get("mesaj", 1000)), ""),
        ("Ses", ist["ses"] // 3600, max(1, hedefler.get("ses", 25)), " saat"),
        ("Davet", ist["davet"], max(1, hedefler.get("davet", 25)), " davet"),
    ]
    if kayit_goster:
        satirlar.append(("Kayıt", ist["kayit"], max(1, hedefler.get("kayit", 50)), " kayıt"))
    for ad, sayi, hedef, birim in satirlar:
        yuzde = min(100, int((sayi / hedef) * 100))
        embed.add_field(name=f"{ad} · {min(sayi, hedef)}/{hedef}{birim} (%{yuzde})", value=terfi_bar(sayi, hedef, uzunluk=18), inline=False)
    cursor.execute(
        "SELECT role_id, started_at FROM terfi_rol_sureleri WHERE guild_id = ? AND user_id = ?",
        (ctx.guild.id, kisi.id)
    )
    sure_kaydi = cursor.fetchone()
    if sure_kaydi and mevcut_rol and sure_kaydi[0] == mevcut_rol.id:
        toplam_saniye = max(0, int(datetime.now().timestamp() - sure_kaydi[1]))
        gun, kalan = divmod(toplam_saniye, 86400)
        saat, kalan = divmod(kalan, 3600)
        sure_metni = f"{gun} gün {saat} saat {kalan // 60} dakika"
    elif mevcut_rol:
        cursor.execute(
            "INSERT OR IGNORE INTO terfi_rol_sureleri (guild_id, user_id, role_id, started_at) VALUES (?, ?, ?, ?)",
            (ctx.guild.id, kisi.id, mevcut_rol.id, datetime.now().timestamp())
        )
        db.commit()
        sure_metni = "Süre takibi şimdi başladı; önceki süre bilinmiyor."
    else:
        sure_metni = "Kayıtlı yetkili rolü bulunamadı."
    embed.add_field(name="Mevcut yetkili rolünde kalma süresi", value=sure_metni, inline=False)
    embed.set_footer(text=f"Üye: {kisi} • Hedefler role göre belirlenir")
    await ctx.send(embed=embed)

@bot.tree.command(name="ilerleme-arttır", description="Bir yetkilinin mesaj, ses saati, davet veya kayıt ilerlemesini değiştirir.")
@app_commands.describe(kisi="İlerlemesi değiştirilecek kişi", tur="Değiştirilecek istatistik", miktar="Eklenecek/çıkarılacak miktar; ses için saat")
@app_commands.choices(tur=[
    app_commands.Choice(name="Mesaj", value="mesaj"),
    app_commands.Choice(name="Ses saati", value="ses"),
    app_commands.Choice(name="Davet", value="davet"),
    app_commands.Choice(name="Kayıt", value="kayit")
])
async def ilerleme_arttir(interaction: discord.Interaction, kisi: discord.Member, tur: app_commands.Choice[str], miktar: app_commands.Range[int, -100000, 100000]):
    if interaction.guild is None or not terfi_yetkili_kontrol(interaction):
        await interaction.response.send_message("Bu komut için Yönetici veya chakabaniastokens yetkisi gerekir.", ephemeral=True)
        return
    if tur.value == "kayit":
        ayar = terfi_ayarlarini_getir(interaction.guild.id)
        if not ayar["kayit"] or kisi.id not in ayar["kayit"]:
            await interaction.response.send_message("Bu üyede kayıt ilerlemesi açık değil; önce /terfi-panel içinden kayıt rollerini seç.", ephemeral=True)
            return
    terfi_istatistik_arttir(interaction.guild.id, kisi.id, tur.value, miktar)
    birim = " saat" if tur.value == "ses" else ""
    await interaction.response.send_message(f"{kisi.mention} için {tur.name} istatistiği {miktar:+}{birim} olarak değiştirildi.", ephemeral=True)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or member.guild is None:
        return
    now = datetime.now().timestamp()
    cursor.execute("INSERT OR IGNORE INTO terfi_istatistik (guild_id, user_id) VALUES (?, ?)", (member.guild.id, member.id))
    cursor.execute("SELECT ses_baslangic FROM terfi_istatistik WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id))
    row = cursor.fetchone()
    baslangic = row[0] if row else None
    ses_acildi = before.channel is None and after.channel is not None
    ses_kapandi = before.channel is not None and after.channel is None
    if ses_acildi:
        cursor.execute("UPDATE terfi_istatistik SET ses_baslangic = ? WHERE guild_id = ? AND user_id = ?", (now, member.guild.id, member.id))
    elif ses_kapandi and baslangic is not None:
        sure = max(0, int(now - baslangic))
        cursor.execute("UPDATE terfi_istatistik SET ses_saniye = ses_saniye + ?, ses_baslangic = NULL WHERE guild_id = ? AND user_id = ?", (sure, member.guild.id, member.id))
    db.commit()


xp_message_award = register_xp(bot, db, cursor)
economy_commands = register_economy(bot, db)
market_commands = register_market(bot, db)

bot.run(TOKEN)