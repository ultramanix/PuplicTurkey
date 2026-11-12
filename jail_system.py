import json
import logging

import discord
from discord import app_commands


def register_jail(bot, db):
    """Register the per-guild jail system and its slash commands."""

    db.execute("""
        CREATE TABLE IF NOT EXISTS jail_ayarlar (
            guild_id INTEGER PRIMARY KEY,
            role_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS jail_uyeler (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_ids TEXT NOT NULL,
            overwrite_data TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    db.commit()

    def ayarlar(guild_id):
        return db.execute(
            "SELECT role_id, channel_id FROM jail_ayarlar WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()

    def yetkili_mi(member):
        return bool(
            member.guild_permissions.manage_roles
            or member.guild_permissions.administrator
            or member.id == member.guild.owner_id
        )

    def bot_izinleri(guild):
        me = guild.me
        if me is None:
            return "Bot sunucu üyesi bilgisine ulaşamadı."
        if not me.guild_permissions.manage_roles:
            return "Botun **Rolleri Yönet** izni olmalı."
        if not me.guild_permissions.manage_channels:
            return "Botun **Kanalları Yönet** izni olmalı."
        return None

    def jail_overwrite():
        return discord.PermissionOverwrite(
            view_channel=False,
            send_messages=False,
            send_messages_in_threads=False,
            create_public_threads=False,
            create_private_threads=False,
            connect=False,
            speak=False,
        )

    async def jail_kanal_izinlerini_uygula(guild, role, channel):
        deny = jail_overwrite()
        for target in guild.channels:
            if target.id == channel.id:
                continue
            await target.set_permissions(
                role,
                overwrite=deny,
                reason="Jail sistemi: jaillilerin diğer kanallara erişimini kapat",
            )
        await channel.set_permissions(
            guild.default_role,
            view_channel=False,
            send_messages=False,
            reason="Jail sohbetini gizle",
        )
        await channel.set_permissions(
            role,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            send_messages_in_threads=True,
            reason="Jail rolüne özel sohbet erişimi",
        )
        await channel.set_permissions(
            guild.me,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
            reason="Botun jail kanalını yönetebilmesi",
        )

    @bot.tree.command(name="jail-setup", description="Jail rolünü ve özel sohbet kanalını kurar.")
    async def jail_setup(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Bu komut yalnızca sunucuda kullanılabilir.", ephemeral=True
            )
            return
        if not yetkili_mi(interaction.user):
            await interaction.response.send_message(
                "Bu komut için Rolleri Yönet yetkisi gerekli.", ephemeral=True
            )
            return
        problem = bot_izinleri(guild)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            saved = ayarlar(guild.id)
            role = guild.get_role(saved[0]) if saved else None
            channel = guild.get_channel(saved[1]) if saved else None

            if role is None:
                role = discord.utils.get(guild.roles, name="Jailed")
                if role is None:
                    role = await guild.create_role(
                        name="Jailed",
                        reason=f"Jail sistemi kurulumu: {interaction.user}",
                    )
            if not isinstance(channel, discord.TextChannel):
                channel = discord.utils.get(guild.text_channels, name="jail-sohbet")
                if channel is None:
                    channel = await guild.create_text_channel(
                        "jail-sohbet",
                        topic="Jail üyeleri için özel sohbet kanalı.",
                        reason=f"Jail sistemi kurulumu: {interaction.user}",
                    )

            if role >= guild.me.top_role:
                await interaction.followup.send(
                    "Jailed rolü botun en yüksek rolünün altında olmalı. Sunucu rol sırasından Jailed rolünü bot rolünün altına taşı.",
                    ephemeral=True,
                )
                return

            await role.edit(permissions=discord.Permissions(0), reason="Jail rolünde ayrıcalıklı sunucu izinleri olmasın")
            await jail_kanal_izinlerini_uygula(guild, role, channel)
            db.execute(
                "INSERT INTO jail_ayarlar (guild_id, role_id, channel_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id, channel_id=excluded.channel_id",
                (guild.id, role.id, channel.id),
            )
            db.commit()
        except discord.Forbidden:
            await interaction.followup.send(
                "Kurulum tamamlanamadı. Botun Rolleri Yönet ve Kanalları Yönet izinlerini ve rol sırasını kontrol et.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logging.exception("Jail kurulumu başarısız: guild=%s", guild.id)
            await interaction.followup.send(
                "Discord kaynakları oluştururken hata verdi. Biraz sonra tekrar dene.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Jail sistemi hazır. Rol: {role.mention}\nÖzel sohbet: {channel.mention}\n"
            "Jailliler diğer kanalları göremez; yalnızca bu kanalda yazabilir.",
            ephemeral=True,
        )

    @bot.tree.command(name="jail", description="Bir üyeyi jail kanalına alır.")
    @app_commands.describe(kisi="Jaile alınacak üye", sebep="Jail sebebi")
    async def jail(interaction: discord.Interaction, kisi: discord.Member, sebep: str = "Sebep belirtilmedi."):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Bu komut yalnızca sunucuda kullanılabilir.", ephemeral=True
            )
            return
        if not yetkili_mi(interaction.user):
            await interaction.response.send_message(
                "Bu komut için Rolleri Yönet yetkisi gerekli.", ephemeral=True
            )
            return
        problem = bot_izinleri(guild)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        saved = ayarlar(guild.id)
        if not saved:
            await interaction.response.send_message(
                "Önce /jail-setup komutunu çalıştır.", ephemeral=True
            )
            return
        role = guild.get_role(saved[0])
        channel = guild.get_channel(saved[1])
        if role is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Jail rolü veya kanalı bulunamadı. /jail-setup komutunu yeniden çalıştır.",
                ephemeral=True,
            )
            return
        if kisi.bot or kisi.id == guild.owner_id or kisi.id == interaction.user.id:
            await interaction.response.send_message(
                "Botları, sunucu sahibini veya kendini jaile alamazsın.", ephemeral=True
            )
            return
        if kisi.guild_permissions.administrator:
            await interaction.response.send_message(
                "Yönetici yetkisi olan bir üyeyi jail sistemine alamazsın. Önce yönetici yetkisini kaldır.",
                ephemeral=True,
            )
            return
        if kisi.top_role >= guild.me.top_role or role >= guild.me.top_role:
            await interaction.response.send_message(
                "Üyenin en yüksek rolü veya Jailed rolü botun rolünün altında olmalı.",
                ephemeral=True,
            )
            return
        if db.execute(
            "SELECT 1 FROM jail_uyeler WHERE guild_id = ? AND user_id = ?",
            (guild.id, kisi.id),
        ).fetchone():
            await interaction.response.send_message(
                "Bu üye zaten jailde.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        old_roles = [r for r in kisi.roles if not r.is_default()]
        # Managed roles cannot be removed by the bot and could grant channel access.
        if any(r.managed for r in old_roles):
            await interaction.followup.send(
                "Üyede bot/entegrasyon tarafından yönetilen bir rol var. Güvenli şekilde jail uygulamak için önce bu rolü kaldır.",
                ephemeral=True,
            )
            return

        overwrite_data = []
        try:
            for target in guild.channels:
                overwrite = target.overwrites_for(kisi)
                if overwrite is not None and overwrite.pair()[0].value | overwrite.pair()[1].value:
                    allow, deny = overwrite.pair()
                    overwrite_data.append({
                        "channel_id": target.id,
                        "allow": allow.value,
                        "deny": deny.value,
                    })

            db.execute(
                "INSERT INTO jail_uyeler (guild_id, user_id, role_ids, overwrite_data) VALUES (?, ?, ?, ?)",
                (
                    guild.id,
                    kisi.id,
                    json.dumps([r.id for r in old_roles]),
                    json.dumps(overwrite_data),
                ),
            )
            db.commit()

            # Remove member-specific channel overrides so they cannot bypass role denies.
            for target in guild.channels:
                if target.overwrites_for(kisi) != discord.PermissionOverwrite():
                    await target.set_permissions(
                        kisi, overwrite=None, reason=f"Jail: {interaction.user}"
                    )
            if old_roles:
                await kisi.remove_roles(*old_roles, reason=f"Jail: {sebep}")
            await kisi.add_roles(role, reason=f"Jail: {sebep}")
            await jail_kanal_izinlerini_uygula(guild, role, channel)
        except discord.Forbidden:
            logging.exception("Jail uygulaması için izin yetersiz: guild=%s user=%s", guild.id, kisi.id)
            await interaction.followup.send(
                "Jail uygulanamadı. Botun rol sırasını ve kanal/rol yönetme izinlerini kontrol et. Gerekirse üyenin rollerini kontrol et.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logging.exception("Jail uygulaması başarısız: guild=%s user=%s", guild.id, kisi.id)
            await interaction.followup.send(
                "Discord işlemi tamamlayamadı. Üyenin rol durumunu kontrol et; gerekirse /unjail kullan.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"{kisi.mention} jaile alındı.\nSebep: **{sebep[:300]}**\n"
            f"Yalnızca {channel.mention} kanalını görebilir.",
            ephemeral=True,
        )

    @bot.tree.command(name="unjail", description="Bir üyeyi jailden çıkarıp eski rollerini geri verir.")
    @app_commands.describe(kisi="Jailden çıkarılacak üye")
    async def unjail(interaction: discord.Interaction, kisi: discord.Member):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Bu komut yalnızca sunucuda kullanılabilir.", ephemeral=True
            )
            return
        if not yetkili_mi(interaction.user):
            await interaction.response.send_message(
                "Bu komut için Rolleri Yönet yetkisi gerekli.", ephemeral=True
            )
            return
        problem = bot_izinleri(guild)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        row = db.execute(
            "SELECT role_ids, overwrite_data FROM jail_uyeler WHERE guild_id = ? AND user_id = ?",
            (guild.id, kisi.id),
        ).fetchone()
        if not row:
            await interaction.response.send_message(
                "Bu üye jail kayıtlarında bulunamadı.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        saved = ayarlar(guild.id)
        jail_role = guild.get_role(saved[0]) if saved else None
        try:
            if jail_role and jail_role in kisi.roles:
                await kisi.remove_roles(jail_role, reason=f"Jailden çıkarıldı: {interaction.user}")

            role_ids = json.loads(row[0])
            roles = [
                guild.get_role(role_id)
                for role_id in role_ids
                if guild.get_role(role_id) is not None
                and guild.get_role(role_id) < guild.me.top_role
                and not guild.get_role(role_id).managed
            ]
            if roles:
                await kisi.add_roles(*roles, reason=f"Jailden çıkarıldı: {interaction.user}")

            for item in json.loads(row[1]):
                target = guild.get_channel(item["channel_id"])
                if target is None:
                    continue
                allow = discord.Permissions(item["allow"])
                deny = discord.Permissions(item["deny"])
                await target.set_permissions(
                    kisi,
                    overwrite=discord.PermissionOverwrite.from_pair(allow, deny),
                    reason="Jailden çıkarıldı: eski kanal izinlerini geri yükle",
                )
            db.execute(
                "DELETE FROM jail_uyeler WHERE guild_id = ? AND user_id = ?",
                (guild.id, kisi.id),
            )
            db.commit()
        except (discord.Forbidden, discord.HTTPException, ValueError, TypeError):
            logging.exception("Jail kaldırma başarısız: guild=%s user=%s", guild.id, kisi.id)
            await interaction.followup.send(
                "Üye tamamen jailden çıkarılamadı. Botun rol sırasını ve izinlerini kontrol et; kayıt silinmedi.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"{kisi.mention} jailden çıkarıldı. Önceki rolleri ve kaydedilmiş kanal izinleri geri yüklendi.",
            ephemeral=True,
        )
