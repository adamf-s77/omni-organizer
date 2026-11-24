"""Omni-Organizer v3 Discord Cog: Bridge HTTP client, HITL buttons/dropdowns, and /org commands."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks


DEFAULT_BRIDGE_URL = os.environ.get("OMNI_BRIDGE_URL", "http://127.0.0.1:8766")
DEFAULT_TOKEN_FILE = os.environ.get("OMNI_BRIDGE_TOKEN_FILE") or str(
    Path(__file__).resolve().parents[2] / "config" / "bridge_secret.local.txt"
)


def _fmt_size(size_bytes: int) -> str:
    size = float(max(0, int(size_bytes or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size_bytes} B"


def _trunc(text: str, limit: int = 900) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


class OrganizerBridgeClient:
    """Async HTTP client talking exclusively to Omni-Organizer's localhost Bridge API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BRIDGE_URL,
        token: Optional[str] = None,
        token_file: Optional[str] = DEFAULT_TOKEN_FILE,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BRIDGE_URL).rstrip("/")
        self._explicit_token = (token or "").strip()
        self.token_file = token_file
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: Optional[aiohttp.ClientSession] = None

    def resolve_token(self) -> str:
        """Resolve Bearer token dynamically from explicit arg, file, or environment."""
        if self._explicit_token:
            return self._explicit_token
        if self.token_file:
            p = Path(self.token_file)
            if p.is_file():
                try:
                    val = p.read_text(encoding="utf-8").strip()
                    if val:
                        return val
                except OSError:
                    pass
        return os.environ.get("OMNI_BRIDGE_TOKEN", "").strip()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[dict[str, Any]] = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        session = await self._get_session()
        headers: dict[str, str] = {}
        if auth:
            tok = self.resolve_token()
            if tok:
                headers["Authorization"] = f"Bearer {tok}"

        url = f"{self.base_url}{path}"
        async with session.request(method, url, json=json_body, headers=headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                text = await resp.text()
                data = {"ok": False, "error": f"invalid_json_http_{resp.status}", "detail": text[:200]}
            if not isinstance(data, dict):
                data = {"ok": False, "error": f"unexpected_payload_http_{resp.status}"}
            data.setdefault("_http_status", resp.status)
            return data

    async def get_health(self) -> dict[str, Any]:
        return await self._request("GET", "/health", auth=False)

    async def get_status(self) -> dict[str, Any]:
        return await self._request("GET", "/status")

    async def get_destinations(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/destinations")
        return list(data.get("destinations") or []) if data.get("ok") else []

    async def get_pending(self, unnotified: bool = False) -> list[dict[str, Any]]:
        q = "?unnotified=1" if unnotified else ""
        data = await self._request("GET", f"/pending{q}")
        return list(data.get("pending") or []) if data.get("ok") else []

    async def ack_pending(self, pending_id: str) -> bool:
        data = await self._request("POST", f"/pending/{pending_id}/ack")
        return bool(data.get("ok"))

    async def get_notifications(self, unacked: bool = False) -> list[dict[str, Any]]:
        q = "?unacked=1" if unacked else ""
        data = await self._request("GET", f"/notifications{q}")
        return list(data.get("notifications") or []) if data.get("ok") else []

    async def ack_notification(self, event_id: str) -> bool:
        data = await self._request("POST", f"/notifications/{event_id}/ack")
        return bool(data.get("ok"))

    async def decide(
        self,
        pending_id: str,
        action: str,
        dest_id: Optional[str] = None,
        remember: bool = False,
        keyword: Optional[str] = None,
        actor: str = "discord_user",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pending_id": pending_id,
            "action": action,
            "remember": remember,
            "actor": actor,
        }
        if dest_id:
            payload["dest_id"] = dest_id
        if keyword:
            payload["keyword"] = keyword
        return await self._request("POST", "/decide", json_body=payload)

    async def undo(self, tx_id: Optional[str] = None) -> dict[str, Any]:
        body = {"tx_id": tx_id} if tx_id else {}
        return await self._request("POST", "/undo", json_body=body)

    async def get_rules(self) -> dict[str, Any]:
        return await self._request("GET", "/rules")

    async def delete_rule(self, rule_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/rules/{rule_id}")


# ----------------------------------------------------------------------
# Authorization & Client Helpers
# ----------------------------------------------------------------------
def _check_interaction_auth(interaction: discord.Interaction) -> bool:
    client = interaction.client
    auth_fn = getattr(client, "is_user_authorized", None)
    if callable(auth_fn):
        return bool(auth_fn(interaction.user, interaction.channel))

    cfg = getattr(client, "pc_config", {}) or {}
    allowed_ids = [str(uid) for uid in cfg.get("allowed_user_ids", []) if uid]
    if allowed_ids:
        return str(interaction.user.id) in allowed_ids
    if isinstance(interaction.user, discord.Member):
        if interaction.user.guild.owner_id == interaction.user.id or interaction.user.guild_permissions.administrator:
            return True
    cfg_cid = str(cfg.get("channel_id") or "").strip()
    if cfg_cid and interaction.channel and str(interaction.channel.id) == cfg_cid:
        return True
    return False


def _get_bridge_client(interaction: discord.Interaction) -> Optional[OrganizerBridgeClient]:
    client = getattr(interaction.client, "organizer_client", None)
    if client is not None:
        return client
    cog = getattr(interaction.client, "get_cog", lambda _: None)("OrganizerCog")
    return getattr(cog, "client", None)


async def _ensure_auth_and_client(
    interaction: discord.Interaction,
) -> Optional[OrganizerBridgeClient]:
    if not _check_interaction_auth(interaction):
        await interaction.response.send_message(
            "⛔ **拒絕存取**：您沒有操作 Omni-Organizer 的權限！",
            ephemeral=True,
        )
        return None
    bridge = _get_bridge_client(interaction)
    if bridge is None:
        await interaction.response.send_message(
            "⚠️ Organizer Cog 尚未就緒，請稍後再試。",
            ephemeral=True,
        )
        return None
    return bridge


async def _handle_decide_interaction(
    interaction: discord.Interaction,
    pending_id: str,
    action: str,
    dest_id: Optional[str] = None,
    remember: bool = False,
) -> None:
    bridge = await _ensure_auth_and_client(interaction)
    if bridge is None:
        return

    await interaction.response.defer()
    actor = f"discord:{interaction.user}"
    try:
        res = await bridge.decide(
            pending_id=pending_id,
            action=action,
            dest_id=dest_id,
            remember=remember,
            actor=actor,
        )
    except Exception as exc:
        await interaction.followup.send(
            f"❌ 無法連線至 Omni-Organizer Bridge：`{type(exc).__name__}: {exc}`",
            ephemeral=True,
        )
        return

    if not res.get("ok"):
        await interaction.followup.send(
            f"⚠️ 操作未成功：`{res.get('error', 'unknown_error')}`",
            ephemeral=True,
        )
        return

    if action == "ignore":
        embed = discord.Embed(
            title="🙈 已忽略此檔案（冷卻 24 小時）",
            description=f"Pending ID：`{pending_id}`\n操作者：{interaction.user.mention}",
            color=discord.Color.light_grey(),
        )
        new_view = None
        summary = "🙈 已設定 24 小時忽略冷卻。"
    else:
        tx_id = res.get("tx_id", "")
        dest_label = res.get("dest_label") or res.get("dst", "")
        rule_note = f"\n🧠 已新增學習規則：`{res['rule_id']}`" if res.get("rule_id") else ""
        embed = discord.Embed(
            title="✅ 檔案已完成歸檔",
            description=(
                f"**目的地**：`{_trunc(dest_label, 300)}`\n"
                f"**交易 ID**：`{tx_id}`{rule_note}\n"
                f"**操作者**：{interaction.user.mention}"
            ),
            color=discord.Color.green(),
        )
        new_view = discord.ui.View(timeout=None)
        if tx_id:
            new_view.add_item(OrgUndoButton(tx_id))
        summary = f"✅ 已搬移至 `{dest_label}`（`{tx_id}`）。"

    try:
        if interaction.message:
            await interaction.message.edit(embed=embed, view=new_view)
    except Exception:
        pass
    await interaction.followup.send(summary, ephemeral=True)


# ----------------------------------------------------------------------
# Persistent DynamicItem Components (Survive Bot Restarts)
# ----------------------------------------------------------------------
class OrgAcceptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"org:accept:(?P<pid>[A-Za-z0-9_-]+)",
):
    def __init__(self, pending_id: str) -> None:
        self.pending_id = pending_id
        super().__init__(
            discord.ui.Button(
                label="✅ 接受建議",
                style=discord.ButtonStyle.success,
                custom_id=f"org:accept:{pending_id}",
                row=0,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "OrgAcceptButton":
        return cls(match.group("pid"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_decide_interaction(interaction, self.pending_id, action="accept")


class OrgRememberButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"org:remember:(?P<pid>[A-Za-z0-9_-]+)",
):
    def __init__(self, pending_id: str) -> None:
        self.pending_id = pending_id
        super().__init__(
            discord.ui.Button(
                label="🧠 移動並記住規則",
                style=discord.ButtonStyle.primary,
                custom_id=f"org:remember:{pending_id}",
                row=0,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "OrgRememberButton":
        return cls(match.group("pid"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_decide_interaction(
            interaction,
            self.pending_id,
            action="accept_and_remember",
            remember=True,
        )


class OrgIgnoreButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"org:ignore:(?P<pid>[A-Za-z0-9_-]+)",
):
    def __init__(self, pending_id: str) -> None:
        self.pending_id = pending_id
        super().__init__(
            discord.ui.Button(
                label="🙈 忽略 (24h)",
                style=discord.ButtonStyle.secondary,
                custom_id=f"org:ignore:{pending_id}",
                row=0,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "OrgIgnoreButton":
        return cls(match.group("pid"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_decide_interaction(interaction, self.pending_id, action="ignore")


class OrgUndoButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"org:undo:(?P<txid>[A-Za-z0-9_-]+)",
):
    def __init__(self, tx_id: str) -> None:
        self.tx_id = tx_id
        super().__init__(
            discord.ui.Button(
                label="↩️ Undo 還原",
                style=discord.ButtonStyle.danger,
                custom_id=f"org:undo:{tx_id}",
                row=0,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "OrgUndoButton":
        return cls(match.group("txid"))

    async def callback(self, interaction: discord.Interaction) -> None:
        bridge = await _ensure_auth_and_client(interaction)
        if bridge is None:
            return
        await interaction.response.defer()
        try:
            res = await bridge.undo(tx_id=self.tx_id)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ 無法連線至 Bridge：`{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
            return

        if not res.get("ok"):
            await interaction.followup.send(
                f"⚠️ Undo 失敗：`{res.get('error', 'unknown_error')}`",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="↩️ 已一鍵還原檔案位置",
            description=(
                f"**交易 ID**：`{res.get('tx_id')}`\n"
                f"**已還原至**：`{_trunc(res.get('restored_to', ''), 400)}`\n"
                f"🛡️ 已自動加入 **24 小時防拔河冷卻**，不會立刻重複搬走。"
            ),
            color=discord.Color.orange(),
        )
        try:
            if interaction.message:
                await interaction.message.edit(embed=embed, view=None)
        except Exception:
            pass
        await interaction.followup.send(f"↩️ 已還原至 `{res.get('restored_to')}`。", ephemeral=True)


class OrgFolderSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"org:select:(?P<pid>[A-Za-z0-9_-]+)",
):
    def __init__(
        self,
        pending_id: str,
        destinations: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self.pending_id = pending_id
        options: list[discord.SelectOption] = []
        for dest in (destinations or [])[:25]:
            dest_id = str(dest.get("id") or "")[:100]
            label = str(dest.get("label") or dest_id)[:100]
            desc = str(dest.get("description") or "")[:100] or None
            if dest_id and label:
                options.append(discord.SelectOption(label=label, value=dest_id, description=desc))
        if not options:
            options.append(discord.SelectOption(label="無可用目錄", value="__none__"))

        super().__init__(
            discord.ui.Select(
                custom_id=f"org:select:{pending_id}",
                placeholder="📂 改選其他目標資料夾...",
                min_values=1,
                max_values=1,
                options=options,
                row=1,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Select,
        match: re.Match[str],
        /,
    ) -> "OrgFolderSelect":
        return cls(match.group("pid"))

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.item.values[0] if self.item.values else ""
        if not selected or selected == "__none__":
            await interaction.response.send_message("⚠️ 未選擇有效的目標資料夾。", ephemeral=True)
            return
        await _handle_decide_interaction(
            interaction,
            self.pending_id,
            action="choose",
            dest_id=selected,
            remember=False,
        )


DYNAMIC_ITEM_CLASSES = (
    OrgAcceptButton,
    OrgRememberButton,
    OrgIgnoreButton,
    OrgUndoButton,
    OrgFolderSelect,
)


# ----------------------------------------------------------------------
# Card Builders
# ----------------------------------------------------------------------
def build_pending_card(
    item: dict[str, Any],
    destinations: list[dict[str, Any]],
) -> tuple[discord.Embed, discord.ui.View]:
    """Build a Discord Embed + interactive View for a Tier 3 pending or archive file."""
    pid = str(item.get("id") or "")
    filename = str(item.get("filename") or "(unknown)")
    kind = str(item.get("kind") or "classify")
    is_archive = kind == "archive"
    is_sensitive = bool(item.get("is_sensitive"))
    is_misplaced = bool(item.get("is_misplaced_candidate"))
    suggested_id = item.get("suggested_dest_id")
    suggested_label = item.get("suggested_dest_label") or suggested_id
    confidence = float(item.get("confidence") or 0.0)

    if is_sensitive:
        title = f"🔒 敏感檔案人工確認：{_trunc(filename, 180)}"
        color = discord.Color.red()
    elif is_archive:
        title = f"🗄️ 定期歸檔詢問：{_trunc(filename, 180)}"
        color = discord.Color.gold()
    elif is_misplaced:
        title = f"🧭 疑似放錯位置：{_trunc(filename, 180)}"
        color = discord.Color.orange()
    else:
        title = f"📂 待分類檔案：{_trunc(filename, 180)}"
        color = discord.Color.blurple()

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="📍 目前路徑", value=f"`{_trunc(item.get('src_path', ''), 900)}`", inline=False)
    embed.add_field(
        name="📦 檔案資訊",
        value=f"範圍：`{item.get('scope_id', '-')}` • 大小：`{_fmt_size(item.get('size_bytes', 0))}`",
        inline=True,
    )
    if is_sensitive:
        embed.add_field(
            name="🛡️ 隱私保護狀態",
            value="已略過內容讀取與 AI 分析（絕不外送）",
            inline=True,
        )
    elif is_archive and suggested_label:
        embed.add_field(
            name="📦 建議封存位置",
            value=f"`{_trunc(suggested_label, 300)}`",
            inline=True,
        )
    elif suggested_label:
        embed.add_field(
            name="🤖 AI 建議位置",
            value=f"`{_trunc(suggested_label, 300)}`（信心 `{confidence:.2f}`）",
            inline=True,
        )
    else:
        embed.add_field(name="🤖 AI 建議", value="*未提供建議，請從下方選單指定*", inline=True)

    embed.add_field(
        name="📝 判斷說明",
        value=_trunc(item.get("reason") or "請選擇目標資料夾或忽略", 900),
        inline=False,
    )
    embed.set_footer(text=f"Omni-Organizer v3 • Pending ID: {pid}")

    view = discord.ui.View(timeout=None)
    if suggested_id:
        view.add_item(OrgAcceptButton(pid))
        if not is_sensitive and not is_archive:
            view.add_item(OrgRememberButton(pid))
    view.add_item(OrgIgnoreButton(pid))
    if destinations:
        view.add_item(OrgFolderSelect(pid, destinations[:25]))
    return embed, view


def build_automove_card(event: dict[str, Any]) -> tuple[discord.Embed, Optional[discord.ui.View]]:
    """Build a Discord Embed (+ Undo button for moves) from a Bridge notification event."""
    kind = str(event.get("kind") or "info")
    filename = str(event.get("filename") or "")
    tx_id = event.get("tx_id")

    if kind == "auto_moved":
        embed = discord.Embed(
            title=f"✨ 已自動歸檔：{_trunc(filename, 180)}",
            color=discord.Color.green(),
        )
        embed.add_field(name="📤 原路徑", value=f"`{_trunc(event.get('src', ''), 900)}`", inline=False)
        embed.add_field(
            name="📥 目標位置",
            value=f"`{_trunc(event.get('dest_label') or event.get('dst', ''), 900)}`",
            inline=False,
        )
        embed.add_field(
            name="⚙️ 決策資訊",
            value=(
                f"層級：`{event.get('tier', '-')}` • "
                f"信心：`{float(event.get('confidence') or 1.0):.2f}`\n"
                f"理由：{_trunc(event.get('reason', ''), 400)}"
            ),
            inline=False,
        )
        if tx_id:
            embed.set_footer(text=f"Omni-Organizer v3 • tx_id: {tx_id} • 按下方按鈕可一鍵還原")
            view = discord.ui.View(timeout=None)
            view.add_item(OrgUndoButton(str(tx_id)))
            return embed, view
        return embed, None

    embed = discord.Embed(
        title=f"⚠️ 檔案通知：{_trunc(filename, 180)}",
        description=_trunc(event.get("reason") or "", 900),
        color=discord.Color.orange(),
    )
    if event.get("src"):
        embed.add_field(name="📍 原路徑", value=f"`{_trunc(event['src'], 900)}`", inline=False)
    if event.get("dst"):
        embed.add_field(name="📁 目標路徑", value=f"`{_trunc(event['dst'], 900)}`", inline=False)
    return embed, None


# ----------------------------------------------------------------------
# GroupCog: /org <status|pending|undo|rules> + Bridge Poller
# ----------------------------------------------------------------------
class OrganizerCog(commands.GroupCog, group_name="org", group_description="Omni-Organizer v3 智慧檔案整理與審核"):
    """Decoupled optional Discord Cog bridging any discord.py Bot with Omni-Organizer v3."""

    def __init__(self, bot: commands.Bot, client: Optional[OrganizerBridgeClient] = None) -> None:
        self.bot = bot
        cfg = getattr(bot, "pc_config", {}) or {}
        if client is not None:
            self.client = client
        else:
            self.client = OrganizerBridgeClient(
                base_url=str(cfg.get("organizer_bridge_url") or DEFAULT_BRIDGE_URL),
                token=(cfg.get("organizer_token") or None),
                token_file=str(cfg.get("organizer_token_file") or DEFAULT_TOKEN_FILE),
            )
        setattr(bot, "organizer_client", self.client)

        self._bridge_online: Optional[bool] = None
        self._cached_destinations: list[dict[str, Any]] = []

        poll_seconds = max(3, int(cfg.get("organizer_poll_seconds") or 5))
        self.poll_bridge_loop.change_interval(seconds=poll_seconds)

    async def cog_load(self) -> None:
        if not self.poll_bridge_loop.is_running():
            self.poll_bridge_loop.start()

    async def cog_unload(self) -> None:
        if self.poll_bridge_loop.is_running():
            self.poll_bridge_loop.cancel()
        if getattr(self.bot, "organizer_client", None) is self.client:
            setattr(self.bot, "organizer_client", None)
        await self.client.close()

    async def _resolve_target_channel(self) -> Optional[discord.TextChannel]:
        cfg = getattr(self.bot, "pc_config", {}) or {}
        target_cid = str(
            cfg.get("organizer_channel_id")
            or cfg.get("channel_id")
            or os.environ.get("OMNI_DISCORD_CHANNEL_ID")
            or ""
        ).strip()
        channel = None
        if target_cid.isdigit():
            channel = self.bot.get_channel(int(target_cid))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(target_cid))
                except Exception:
                    channel = None
        if channel is None:
            for guild in self.bot.guilds:
                for ch in guild.text_channels:
                    if "organizer" in ch.name.lower():
                        channel = ch
                        break
                if channel is not None:
                    break
        return channel if isinstance(channel, discord.TextChannel) else None

    async def poll_once(self, channel: Optional[discord.abc.Messageable] = None) -> int:
        """Poll unnotified pending items & unacked notifications once; returns posted count."""
        target = channel or await self._resolve_target_channel()
        if target is None:
            return 0

        pending_items = await self.client.get_pending(unnotified=True)
        notifications = await self.client.get_notifications(unacked=True)
        if not pending_items and not notifications:
            return 0

        if pending_items and not self._cached_destinations:
            self._cached_destinations = await self.client.get_destinations()
        elif pending_items:
            fresh = await self.client.get_destinations()
            if fresh:
                self._cached_destinations = fresh

        posted = 0
        for item in pending_items:
            embed, view = build_pending_card(item, self._cached_destinations)
            await target.send(embed=embed, view=view)
            await self.client.ack_pending(str(item["id"]))
            posted += 1

        for ev in notifications:
            embed, view = build_automove_card(ev)
            if view is not None:
                await target.send(embed=embed, view=view)
            else:
                await target.send(embed=embed)
            await self.client.ack_notification(str(ev["id"]))
            posted += 1

        return posted

    @tasks.loop(seconds=5)
    async def poll_bridge_loop(self) -> None:
        try:
            await self.poll_once()
            if self._bridge_online is False:
                print("[ORG] Omni-Organizer Bridge 連線已恢復。")
            self._bridge_online = True
        except Exception as exc:
            if self._bridge_online is not False:
                print(f"[ORG] Bridge 暫時離線（後續重試不重複洗版）：{type(exc).__name__}: {exc}")
            self._bridge_online = False

    @poll_bridge_loop.before_loop
    async def _before_poll_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Slash Commands (/org ...)
    # ------------------------------------------------------------------
    @app_commands.command(name="status", description="查看 Omni-Organizer v3 運行狀態、監控範圍與 AI 引擎健康")
    async def org_status(self, interaction: discord.Interaction) -> None:
        if not _check_interaction_auth(interaction):
            await interaction.response.send_message("⛔ **拒絕存取**！", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            data = await self.client.get_status()
        except Exception as exc:
            await interaction.followup.send(
                f"❌ **Omni-Organizer Bridge 離線**：`{type(exc).__name__}: {exc}`\n"
                "請確認背景服務 `watcher.py` 已啟動。"
            )
            return

        if not data.get("ok"):
            await interaction.followup.send(f"⚠️ 取得狀態失敗：`{data.get('error', 'unknown')}`")
            return

        ai = data.get("ai") or {}
        health = ai.get("health") or {}
        counts = data.get("counts") or {}
        scopes = data.get("monitored_scopes") or []

        ai_state = "🟢 就緒" if health.get("available") else f"🟡 離線/停用（{health.get('reason', 'offline')}）"
        embed = discord.Embed(
            title=f"🗂️ Omni-Organizer v{data.get('version', '3')} 系統狀態",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="⚙️ 核心服務",
            value=f"PID：`{data.get('pid')}` • 運行：`{data.get('uptime_sec', 0)}s`",
            inline=True,
        )
        embed.add_field(
            name="🤖 AI 分類引擎",
            value=f"`{ai.get('provider')}` / `{ai.get('model')}`\n{ai_state}",
            inline=True,
        )
        embed.add_field(
            name="📊 統計",
            value=(
                f"待審：`{counts.get('pending', 0)}` • "
                f"學習規則：`{counts.get('learned_rules', 0)}` • "
                f"可還原紀錄：`{counts.get('undo_records', 0)}` • "
                f"冷卻中：`{counts.get('active_cooldowns', 0)}`"
            ),
            inline=False,
        )
        scope_lines = [
            f"{'✅' if s.get('exists') else '⚠️'} `[{s.get('id')}]` {s.get('path')} (`{s.get('mode')}`)"
            for s in scopes
        ]
        embed.add_field(
            name="👀 監控範圍",
            value=_trunc("\n".join(scope_lines) or "(無)", 1000),
            inline=False,
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="pending", description="列出目前待人工確認的檔案並發送互動卡片（最多 5 張）")
    async def org_pending(self, interaction: discord.Interaction) -> None:
        if not _check_interaction_auth(interaction):
            await interaction.response.send_message("⛔ **拒絕存取**！", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            items = await self.client.get_pending(unnotified=False)
            destinations = await self.client.get_destinations()
        except Exception as exc:
            await interaction.followup.send(f"❌ 無法連線至 Bridge：`{type(exc).__name__}: {exc}`")
            return

        if not items:
            await interaction.followup.send("✅ 目前沒有待確認的檔案！")
            return

        shown = items[:5]
        await interaction.followup.send(f"📋 共有 **{len(items)}** 個待審檔案（顯示前 {len(shown)} 筆）：")
        for item in shown:
            embed, view = build_pending_card(item, destinations)
            await interaction.channel.send(embed=embed, view=view)
            await self.client.ack_pending(str(item["id"]))

    @app_commands.command(name="undo", description="還原最近一次（或指定 tx_id）的自動歸檔搬移")
    @app_commands.describe(tx_id="可選：指定要還原的交易 ID（例如 tx_a1b2c3d4）")
    async def org_undo(self, interaction: discord.Interaction, tx_id: Optional[str] = None) -> None:
        if not _check_interaction_auth(interaction):
            await interaction.response.send_message("⛔ **拒絕存取**！", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            res = await self.client.undo(tx_id=tx_id.strip() if tx_id else None)
        except Exception as exc:
            await interaction.followup.send(f"❌ 無法連線至 Bridge：`{type(exc).__name__}: {exc}`")
            return

        if not res.get("ok"):
            await interaction.followup.send(f"⚠️ 還原失敗：`{res.get('error', 'unknown_error')}`")
            return
        await interaction.followup.send(
            f"↩️ **已還原** `{res.get('tx_id')}`\n"
            f"• 回到：`{res.get('restored_to')}`\n"
            "• 🛡️ 已啟用 24 小時防拔河冷卻。"
        )

    @app_commands.command(name="rules", description="查看學習規則與內建規則，或刪除指定學習規則")
    @app_commands.describe(delete_id="可選：輸入要刪除的學習規則 ID（例如 lr_1234abcd）")
    async def org_rules(self, interaction: discord.Interaction, delete_id: Optional[str] = None) -> None:
        if not _check_interaction_auth(interaction):
            await interaction.response.send_message("⛔ **拒絕存取**！", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            if delete_id:
                res = await self.client.delete_rule(delete_id.strip())
                if res.get("ok"):
                    await interaction.followup.send(f"🗑️ 已刪除學習規則 `{delete_id.strip()}`。")
                else:
                    await interaction.followup.send(f"⚠️ 找不到學習規則 `{delete_id.strip()}`。")
                return
            data = await self.client.get_rules()
        except Exception as exc:
            await interaction.followup.send(f"❌ 無法連線至 Bridge：`{type(exc).__name__}: {exc}`")
            return

        if not data.get("ok"):
            await interaction.followup.send(f"⚠️ 查詢規則失敗：`{data.get('error', 'unknown')}`")
            return

        learned = data.get("learned_rules") or []
        builtin = data.get("tier1_rules") or []
        learned_lines = [
            f"• `{r['id']}` 關鍵字 `{r.get('keyword') or r.get('regex')}` -> `{r['dest_id']}` (命中 {r.get('hit_count', 0)} 次)"
            for r in learned[:15]
        ]
        builtin_lines = [
            f"• `{r['id']}` {r['name']} -> `{r['dest_id']}`"
            for r in builtin[:15]
        ]

        embed = discord.Embed(title="📜 Omni-Organizer v3 分類規則一覽", color=discord.Color.blue())
        embed.add_field(
            name=f"🧠 學習規則 ({len(learned)})",
            value=_trunc("\n".join(learned_lines) or "*尚無學習規則（透過「移動並記住規則」按鈕新增）*", 1000),
            inline=False,
        )
        embed.add_field(
            name=f"📌 內建 Tier 1 規則 ({len(builtin)})",
            value=_trunc("\n".join(builtin_lines) or "*無*", 1000),
            inline=False,
        )
        embed.set_footer(text="輸入 /org rules delete_id:<id> 可刪除學習規則")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Register persistent DynamicItem handlers and load OrganizerCog."""
    bot.add_dynamic_items(*DYNAMIC_ITEM_CLASSES)
    await bot.add_cog(OrganizerCog(bot))
