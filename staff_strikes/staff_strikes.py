diff --git a/staff_strikes/staff_strikes.py b/staff_strikes/staff_strikes.py
index 36fa5ab..23aab9e 100644
--- a/staff_strikes/staff_strikes.py
+++ b/staff_strikes/staff_strikes.py
@@ -41,7 +41,7 @@ except ImportError:
             {"name": "Overseer", "role_id": 1546847847067025509},
             {"name": "Head of Staff", "role_id": 1458892950309441709},
             {"name": "Admin", "role_id": 1424785285782438089},
-            {"name": "Head Admin", "role_id": 1272561419061297184},
+            {"name": "Head Admin", "role_id": 1484601981006774423},
         ]
         STRIKE_AUTHORITY_RANK = "Staff Management"
         ALL_STRIKES_AUTHORITY_RANKS = {"Admin", "Head Admin"}
@@ -273,6 +273,31 @@ class StaffStrikes(commands.Cog):
             or any(rank.role_id in role_ids for rank in self.ranks)
         )
 
+    def has_game_admin_manager_authority(self, member: discord.Member) -> bool:
+        game_admin_role_id = int(getattr(config, "GAME_ADMIN_ROLE_ID", 0))
+        return game_admin_role_id > 0 and any(
+            role.id == game_admin_role_id for role in member.roles
+        )
+
+    def can_manage_strike_target(
+        self, issuer: discord.Member, target: discord.Member
+    ) -> bool:
+        # The existing Game Admin role is a separate manager scope, not a rank:
+        # its holders can manage Game Admin strikes, but not other staff.
+        if self.has_game_admin_manager_authority(
+            issuer
+        ) and self.has_game_admin_manager_authority(target):
+            return True
+
+        issuer_rank = self.highest_rank(issuer)
+        target_rank = self.highest_rank(target)
+        return (
+            self.has_strike_authority(issuer)
+            and issuer_rank is not None
+            and target_rank is not None
+            and target_rank.level <= issuer_rank.level
+        )
+
     def has_strike_authority(self, member: discord.Member) -> bool:
         rank = self.highest_rank(member)
         required = self.rank_by_name.get(config.STRIKE_AUTHORITY_RANK.casefold())
@@ -317,18 +342,15 @@ class StaffStrikes(commands.Cog):
             changed = False
             for strike in self.pending_acknowledgements(record):
                 deadline = acknowledgement_deadline(strike)
-                if (
-                    deadline is None
-                    or deadline > now
-                    or (
-                        strike.get("missed_ack_notified_at") is not None
-                        and strike.get("missed_ack_log_sent_at") is not None
-                    )
-                ):
+                if deadline is None or deadline > now:
                     continue
-                if await self.notify_missed_acknowledgement(record, strike):
-                    strike["missed_ack_notified_at"] = now
-                    changed = True
+
+                # Track DM and channel delivery independently. A failed channel
+                # log must not cause an already-delivered DM to be sent again.
+                if strike.get("missed_ack_notified_at") is None:
+                    if await self.notify_missed_acknowledgement(record, strike):
+                        strike["missed_ack_notified_at"] = now
+                        changed = True
                 if strike.get("missed_ack_log_sent_at") is None:
                     if await self.log_missed_acknowledgement(record, strike):
                         strike["missed_ack_log_sent_at"] = now
@@ -647,14 +669,8 @@ class StaffStrikes(commands.Cog):
                 if payout_cycle_id is not None and blocked_cycle_id != payout_cycle_id:
                     strike["payout_blocked_cycle_id"] = payout_cycle_id
                     changed = True
-                if strike.get("missed_ack_notified_at") is None:
-                    if await self.notify_missed_acknowledgement(record, strike):
-                        strike["missed_ack_notified_at"] = now
-                        changed = True
-                if strike.get("missed_ack_log_sent_at") is None:
-                    if await self.log_missed_acknowledgement(record, strike):
-                        strike["missed_ack_log_sent_at"] = now
-                        changed = True
+                # The acknowledgement monitor owns notifications so repeated
+                # payout checks cannot send duplicate admin messages.
                 if changed:
                     await self.save_record(
                         guild_id,
@@ -709,8 +725,8 @@ class StaffStrikes(commands.Cog):
         if not await self.ensure_ready(ctx):
             return
         issuer = ctx.author
-        if not isinstance(issuer, discord.Member) or not self.has_strike_authority(issuer):
-            await ctx.send("Only Staff Management and higher ranks can issue strikes.")
+        if not isinstance(issuer, discord.Member):
+            await ctx.send("Only staff managers can issue strikes in this server.")
             return
 
         member = await self.resolve_member(ctx, member_value)
@@ -718,13 +734,12 @@ class StaffStrikes(commands.Cog):
             await ctx.send("Strikes can only be applied to staff team members.")
             return
 
-        issuer_rank = self.highest_rank(issuer)
-        target_rank = self.highest_rank(member)
-        if issuer_rank is None or target_rank is None:
-            await ctx.send("Both the issuer and target must have a configured staff rank.")
-            return
-        if target_rank.level > issuer_rank.level:
-            await ctx.send("You cannot strike a staff member ranked higher than you.")
+        if not self.can_manage_strike_target(issuer, member):
+            await ctx.send(
+                "You cannot manage this target's strikes. Staff Management and "
+                "higher ranks can manage ranked staff; Game Admins can manage "
+                "Game Admin strikes only."
+            )
             return
 
         try:
@@ -843,8 +858,8 @@ class StaffStrikes(commands.Cog):
         if not await self.ensure_ready(ctx):
             return
         issuer = ctx.author
-        if not isinstance(issuer, discord.Member) or not self.has_strike_authority(issuer):
-            await ctx.send("Only Staff Management and higher ranks can remove strikes.")
+        if not isinstance(issuer, discord.Member):
+            await ctx.send("Only staff managers can remove strikes in this server.")
             return
 
         member = await self.resolve_member(ctx, member_value)
@@ -852,13 +867,12 @@ class StaffStrikes(commands.Cog):
             await ctx.send("Strikes can only be managed for staff team members.")
             return
 
-        issuer_rank = self.highest_rank(issuer)
-        target_rank = self.highest_rank(member)
-        if issuer_rank is None or target_rank is None:
-            await ctx.send("Both the issuer and target must have a configured staff rank.")
-            return
-        if target_rank.level > issuer_rank.level:
-            await ctx.send("You cannot remove a strike from a staff member ranked higher than you.")
+        if not self.can_manage_strike_target(issuer, member):
+            await ctx.send(
+                "You cannot manage this target's strikes. Staff Management and "
+                "higher ranks can manage ranked staff; Game Admins can manage "
+                "Game Admin strikes only."
+            )
             return
 
         record = await self.get_record(ctx.guild.id, member.id)
