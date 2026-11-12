import logging
import secrets
import time

import discord
from discord import app_commands
from discord.ext import tasks


def register_market(bot, db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS market_ayarlar (
        guild_id INTEGER PRIMARY KEY, talep_kanal_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS market_urunler (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        ad TEXT NOT NULL, aciklama TEXT NOT NULL DEFAULT '',
        fiyat INTEGER NOT NULL, tur TEXT NOT NULL,
        rol_id INTEGER, sure_gun INTEGER NOT NULL DEFAULT 0,
        aktif INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS market_kodlar (
        kod TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        urun_id INTEGER NOT NULL, satin_alma REAL NOT NULL,
        kullanildi REAL, durum TEXT NOT NULL DEFAULT 'bekliyor'
    );
    CREATE TABLE IF NOT EXISTS market_talepler (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, urun_id INTEGER NOT NULL, kod TEXT NOT NULL,
        kanal_id INTEGER NOT NULL, mesaj_id INTEGER, durum TEXT NOT NULL DEFAULT 'bekliyor',
        olusturma REAL NOT NULL, karar_veren INTEGER, karar_zamani REAL
    );
    CREATE TABLE IF NOT EXISTS market_sureli_roller (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL, role_id INTEGER NOT NULL, expires_at REAL NOT NULL
    );
    """)
    db.commit()

    def is_admin(interaction):
        p = interaction.user.guild_permissions
        return interaction.user.id == interaction.guild.owner_id or p.administrator or p.manage_guild

    async def fail(interaction, message):
        await interaction.response.send_message(message, ephemeral=True)

    class RequestView(discord.ui.View):
        def __init__(self, request_id):
            super().__init__(timeout=None)
            self.request_id = request_id
            self.approve.custom_id = f"market:approve:{request_id}"
            self.reject.custom_id = f"market:reject:{request_id}"

        async def decide(self, interaction, approved):
            if not interaction.guild or not is_admin(interaction):
                await fail(interaction, "Bu işlem için Sunucuyu Yönet yetkisi gerekli.")
                return
            row = db.execute(
                "SELECT user_id, kod, durum FROM market_talepler WHERE id=? AND guild_id=?",
                (self.request_id, interaction.guild.id),
            ).fetchone()
            if not row or row[2] != "bekliyor":
                await fail(interaction, "Bu talep zaten sonuçlandırılmış.")
                return
            user_id, code, _ = row
            status = "onaylandi" if approved else "reddedildi"
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute(
                    "UPDATE market_talepler SET durum=?, karar_veren=?, karar_zamani=? "
                    "WHERE id=? AND durum='bekliyor'",
                    (status, interaction.user.id, time.time(), self.request_id),
                )
                if cur.rowcount != 1:
                    db.rollback()
                    await fail(interaction, "Talep başka bir yetkili tarafından sonuçlandırıldı.")
                    return
                db.execute("UPDATE market_kodlar SET durum=? WHERE kod=?", (status, code))
                db.commit()
            except Exception:
                db.rollback()
                logging.exception("Market talebi sonuçlandırılamadı")
                await fail(interaction, "İşlem sırasında hata oluştu.")
                return
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"Talep {'onaylandı' if approved else 'reddedildi'}. Yetkili: {interaction.user.mention}",
                view=self,
            )
            member = interaction.guild.get_member(user_id)
            if member:
                try:
                    await member.send(f"{interaction.guild.name} sunucusundaki market talebin {'onaylandı' if approved else 'reddedildi'}.")
                except discord.HTTPException:
                    pass

        @discord.ui.button(label="Onayla", style=discord.ButtonStyle.success)
        async def approve(self, interaction, button):
            await self.decide(interaction, True)

        @discord.ui.button(label="Reddet", style=discord.ButtonStyle.danger)
        async def reject(self, interaction, button):
            await self.decide(interaction, False)

    for (rid,) in db.execute("SELECT id FROM market_talepler WHERE durum='bekliyor'").fetchall():
        bot.add_view(RequestView(rid))

    @bot.tree.command(name="market-kanal", description="Market taleplerinin gönderileceği özel kanalı ayarlar.")
    async def market_kanal(interaction: discord.Interaction, kanal: discord.TextChannel):
        if not interaction.guild:
            await fail(interaction, "Bu komut yalnızca sunucuda kullanılabilir.")
            return
        if not is_admin(interaction):
            await fail(interaction, "Sunucuyu Yönet veya Yönetici yetkisi gerekli.")
            return
        perms = kanal.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await fail(interaction, "Botun kanalı görme, mesaj gönderme ve embed gönderme izinleri olmalı.")
            return
        db.execute(
            "INSERT INTO market_ayarlar(guild_id, talep_kanal_id) VALUES(?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET talep_kanal_id=excluded.talep_kanal_id",
            (interaction.guild.id, kanal.id),
        )
        db.commit()
        await interaction.response.send_message(f"Market talepleri {kanal.mention} kanalına gönderilecek.", ephemeral=True)

    @bot.tree.command(name="market-urun-ekle", description="Markete ürün ekler.")
    @app_commands.describe(ad="Ürün adı", fiyat="Coin fiyatı", tur="rol veya talep", aciklama="Ürün açıklaması", rol="Rol ürünü için verilecek rol", sure_gun="Rol süresi; 0 sınırsız")
    @app_commands.choices(tur=[
        app_commands.Choice(name="Otomatik rol", value="rol"),
        app_commands.Choice(name="Yetkili onaylı talep", value="talep"),
    ])
    async def market_urun_ekle(
        interaction: discord.Interaction,
        ad: str,
        fiyat: app_commands.Range[int, 0, 1000000000],
        tur: app_commands.Choice[str],
        aciklama: str = "",
        rol: discord.Role = None,
        sure_gun: app_commands.Range[int, 0, 3650] = 0,
    ):
        if not interaction.guild or not is_admin(interaction):
            await fail(interaction, "Bu komut için sunucu yöneticisi olmalısın.")
            return
        if not ad.strip() or len(ad) > 80:
            await fail(interaction, "Ürün adı 1-80 karakter arasında olmalı.")
            return
        if len(aciklama) > 300:
            await fail(interaction, "Açıklama en fazla 300 karakter olabilir.")
            return
        if tur.value == "rol":
            if not rol or rol.is_default() or rol.managed or (interaction.guild.me and rol >= interaction.guild.me.top_role):
                await fail(interaction, "Botun verebileceği geçerli bir rol seçmelisin.")
                return
        elif rol:
            await fail(interaction, "Yetkili onaylı ürünlerde rol seçilmez.")
            return
        cur = db.execute(
            "INSERT INTO market_urunler(guild_id,ad,aciklama,fiyat,tur,rol_id,sure_gun) VALUES(?,?,?,?,?,?,?)",
            (interaction.guild.id, ad.strip(), aciklama, fiyat, tur.value, rol.id if rol else None, sure_gun if tur.value == "rol" else 0),
        )
        db.commit()
        await interaction.response.send_message(f"Ürün eklendi. Market ID: {cur.lastrowid}", ephemeral=True)

    @bot.tree.command(name="market", description="Market ürünlerini listeler.")
    async def market(interaction: discord.Interaction):
        if not interaction.guild:
            await fail(interaction, "Bu komut yalnızca sunucuda kullanılabilir.")
            return
        rows = db.execute(
            "SELECT id,ad,aciklama,fiyat,tur,sure_gun FROM market_urunler WHERE guild_id=? AND aktif=1 ORDER BY id",
            (interaction.guild.id,),
        ).fetchall()
        if not rows:
            await interaction.response.send_message("Markette ürün yok.", ephemeral=True)
            return
        cur = db.execute("SELECT para_adi FROM ekonomi_ayarlar WHERE guild_id=?", (interaction.guild.id,)).fetchone()
        currency = cur[0] if cur else "Coin"
        embed = discord.Embed(title="Sunucu Marketi", color=discord.Color.blurple())
        for pid, name, desc, price, kind, days in rows[:25]:
            detail = desc or ("Otomatik rol" if kind == "rol" else "Yetkili onayı gerekir")
            if kind == "rol" and days:
                detail += f" • {days} gün"
            embed.add_field(name=f"#{pid} • {name} — {price:,} {currency}", value=detail, inline=False)
        embed.set_footer(text="Satın almak için /satinal ürün_id kullan.")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="satinal", description="Market ürünü satın alır ve sana özel kod üretir.")
    async def market_satinal(interaction: discord.Interaction, urun_id: app_commands.Range[int, 1, 2147483647]):
        if not interaction.guild:
            await fail(interaction, "Bu komut yalnızca sunucuda kullanılabilir.")
            return
        product = db.execute(
            "SELECT ad,fiyat FROM market_urunler WHERE id=? AND guild_id=? AND aktif=1",
            (urun_id, interaction.guild.id),
        ).fetchone()
        if not product:
            await fail(interaction, "Ürün bulunamadı veya satışta değil.")
            return
        name, price = product
        cfg = db.execute("SELECT para_adi,baslangic_bakiye FROM ekonomi_ayarlar WHERE guild_id=?", (interaction.guild.id,)).fetchone()
        currency = cfg[0] if cfg else "Coin"
        start_balance = cfg[1] if cfg else 100
        code = secrets.token_urlsafe(12).replace("-", "X").replace("_", "Y").upper()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT OR IGNORE INTO ekonomi_kullanicilar(guild_id,user_id,bakiye) VALUES(?,?,?)",
                (interaction.guild.id, interaction.user.id, start_balance),
            )
            cur = db.execute(
                "UPDATE ekonomi_kullanicilar SET bakiye=bakiye-? WHERE guild_id=? AND user_id=? AND bakiye>=?",
                (price, interaction.guild.id, interaction.user.id, price),
            )
            if cur.rowcount != 1:
                db.rollback()
                await fail(interaction, f"Yeterli {currency} bakiyen yok.")
                return
            db.execute(
                "INSERT INTO market_kodlar(kod,guild_id,user_id,urun_id,satin_alma) VALUES(?,?,?,?,?)",
                (code, interaction.guild.id, interaction.user.id, urun_id, time.time()),
            )
            db.execute(
                "INSERT INTO ekonomi_islemler(guild_id,gonderen_id,alici_id,miktar,tur,tarih) VALUES(?,?,NULL,?,'market_satin_alma',?)",
                (interaction.guild.id, interaction.user.id, price, time.time()),
            )
            db.commit()
        except Exception:
            db.rollback()
            logging.exception("Market satın alma hatası")
            await fail(interaction, "Satın alma başarısız. Bakiyen değiştirilmedi.")
            return
        embed = discord.Embed(title="Satın alma başarılı!", color=discord.Color.green())
        embed.add_field(name="Ürün", value=name, inline=False)
        embed.add_field(name="Ödenen", value=f"{price:,} {currency}", inline=True)
        embed.add_field(name="Kişisel kodun", value=f"||{code}||", inline=True)
        embed.description = "Kod yalnızca satın alan Discord hesabında çalışır. /kullan kod ile etkinleştir."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="envanter", description="Satın aldığın ürünleri ve kodlarını gösterir.")
    async def market_envanter(interaction: discord.Interaction):
        if not interaction.guild:
            await fail(interaction, "Bu komut yalnızca sunucuda kullanılabilir.")
            return
        rows = db.execute(
            "SELECT u.ad,k.kod,k.durum FROM market_kodlar k JOIN market_urunler u ON u.id=k.urun_id "
            "WHERE k.guild_id=? AND k.user_id=? ORDER BY k.satin_alma DESC LIMIT 20",
            (interaction.guild.id, interaction.user.id),
        ).fetchall()
        if not rows:
            await interaction.response.send_message("Envanterin boş.", ephemeral=True)
            return
        labels = {"bekliyor":"Kullanılmadı","talep":"Yetkili onayı bekliyor","onaylandi":"Onaylandı","reddedildi":"Reddedildi","aktif":"Etkinleştirildi"}
        text = [f"**{name}** — {labels.get(status,status)} • Kod: ||{code}||" for name,code,status in rows]
        await interaction.response.send_message(embed=discord.Embed(title="Envanterin",description="\\n".join(text),color=discord.Color.blurple()),ephemeral=True)

    @bot.tree.command(name="kullan", description="Sana ait market kodunu kullanır.")
    @app_commands.describe(kod="Satın alırken aldığın kod", dosya="Talep için görsel veya çıkartma dosyası")
    async def market_kullan(interaction: discord.Interaction, kod: str, dosya: discord.Attachment = None):
        if not interaction.guild:
            await fail(interaction, "Bu komut yalnızca sunucuda kullanılabilir.")
            return
        row = db.execute(
            "SELECT k.user_id,k.urun_id,k.durum,u.ad,u.tur,u.rol_id,u.sure_gun "
            "FROM market_kodlar k JOIN market_urunler u ON u.id=k.urun_id WHERE k.kod=? AND k.guild_id=?",
            (kod.upper(), interaction.guild.id),
        ).fetchone()
        if not row:
            await fail(interaction, "Kod bulunamadı.")
            return
        owner_id, product_id, status, name, kind, role_id, days = row
        if owner_id != interaction.user.id:
            await fail(interaction, "Bu kod başka bir kullanıcıya ait. Yalnızca satın alan hesap kullanabilir.")
            return
        if status != "bekliyor":
            await fail(interaction, "Bu kod daha önce kullanılmış veya işlemde.")
            return
        if kind == "talep":
            cfg = db.execute("SELECT talep_kanal_id FROM market_ayarlar WHERE guild_id=?", (interaction.guild.id,)).fetchone()
            channel = interaction.guild.get_channel(cfg[0]) if cfg and cfg[0] else None
            if not channel:
                await fail(interaction, "Market talep kanalı ayarlanmamış.")
                return
            if dosya and dosya.size > 8*1024*1024:
                await fail(interaction, "Dosya en fazla 8 MB olabilir.")
                return
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute(
                    "INSERT INTO market_talepler(guild_id,user_id,urun_id,kod,kanal_id,olusturma) VALUES(?,?,?,?,?,?)",
                    (interaction.guild.id, interaction.user.id, product_id, kod.upper(), channel.id, time.time()),
                )
                rid = cur.lastrowid
                changed = db.execute("UPDATE market_kodlar SET durum='talep' WHERE kod=? AND durum='bekliyor'", (kod.upper(),))
                if changed.rowcount != 1:
                    db.rollback()
                    await fail(interaction, "Kod başka bir işlem tarafından kullanılmış.")
                    return
                db.commit()
            except Exception:
                db.rollback()
                logging.exception("Market talebi oluşturulamadı")
                await fail(interaction, "Talep oluşturulamadı.")
                return
            embed = discord.Embed(title="Yeni market talebi", color=discord.Color.orange())
            embed.add_field(name="Ürün",value=name,inline=True)
            embed.add_field(name="Üye",value=f"{interaction.user.mention} ({interaction.user.id})",inline=False)
            embed.add_field(name="Kod",value=kod.upper(),inline=True)
            try:
                file = await dosya.to_file() if dosya else None
                msg = await channel.send(embed=embed,file=file,view=RequestView(rid))
            except discord.HTTPException:
                db.execute("DELETE FROM market_talepler WHERE id=?",(rid,))
                db.execute("UPDATE market_kodlar SET durum='bekliyor' WHERE kod=?",(kod.upper(),))
                db.commit()
                await fail(interaction, "Talep kanala gönderilemedi. Bot izinlerini kontrol et.")
                return
            db.execute("UPDATE market_talepler SET mesaj_id=? WHERE id=?",(msg.id,rid))
            db.commit()
            bot.add_view(RequestView(rid))
            await interaction.response.send_message("Talebin özel yetkili kanalına gönderildi. Sonuç DM ile bildirilecek.",ephemeral=True)
            return
        role = interaction.guild.get_role(role_id) if role_id else None
        if not role or (interaction.guild.me and role >= interaction.guild.me.top_role):
            await fail(interaction, "Ürünün rolü bulunamadı veya bot bu rolü veremiyor.")
            return
        try:
            await interaction.user.add_roles(role,reason=f"Market ürünü {product_id}")
        except discord.HTTPException:
            await fail(interaction, "Rol verilemedi. Botun rol izinlerini kontrol et.")
            return
        db.execute("UPDATE market_kodlar SET durum='aktif',kullanildi=? WHERE kod=? AND durum='bekliyor'",(time.time(),kod.upper()))
        if days:
            db.execute(
                "INSERT INTO market_sureli_roller(guild_id,user_id,role_id,expires_at) VALUES(?,?,?,?)",
                (interaction.guild.id,interaction.user.id,role.id,time.time()+days*86400),
            )
        db.commit()
        await interaction.response.send_message(f"{name} etkinleştirildi: {role.mention}",ephemeral=True)

    @bot.tree.command(name="market-urun-kapat", description="Market ürününü satıştan kaldırır.")
    async def market_urun_kapat(interaction: discord.Interaction, urun_id: app_commands.Range[int,1,2147483647]):
        if not interaction.guild or not is_admin(interaction):
            await fail(interaction, "Bu komut için sunucu yöneticisi olmalısın.")
            return
        cur = db.execute("UPDATE market_urunler SET aktif=0 WHERE id=? AND guild_id=? AND aktif=1",(urun_id,interaction.guild.id))
        db.commit()
        await interaction.response.send_message("Ürün satıştan kaldırıldı." if cur.rowcount else "Aktif ürün bulunamadı.",ephemeral=True)

    @tasks.loop(minutes=1)
    async def market_expire_roles():
        now = time.time()
        rows = db.execute("SELECT id,guild_id,user_id,role_id FROM market_sureli_roller WHERE expires_at<=?",(now,)).fetchall()
        for rid,gid,uid,role_id in rows:
            guild = bot.get_guild(gid)
            member = guild.get_member(uid) if guild else None
            role = guild.get_role(role_id) if guild else None
            try:
                if member and role and role in member.roles:
                    await member.remove_roles(role,reason="Market ürün süresi doldu")
                db.execute("DELETE FROM market_sureli_roller WHERE id=?",(rid,))
                db.commit()
            except discord.HTTPException:
                logging.exception("Süresi dolan market rolü kaldırılamadı")

    @market_expire_roles.before_loop
    async def before_market_expire_roles():
        await bot.wait_until_ready()

    @bot.listen("on_ready")
    async def start_market_expiry_loop():
        if not market_expire_roles.is_running():
            market_expire_roles.start()
