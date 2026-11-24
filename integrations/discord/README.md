# Optional Discord Remote Integration (`organizer_cog.py`)

Omni-Organizer works **100% locally out of the box** using the native Desktop Approval Popup (`src/desktop_prompt.py`).

If you also want to review pending files, receive auto-move alerts with **1-click Undo**, or trigger `/org` commands remotely from your phone, you can drop `organizer_cog.py` into any `discord.py >= 2.3` bot running on the **same PC**.

---

## Features

- Persistent `DynamicItem` buttons & dropdowns (survive bot restarts):
  - **Accept Suggestion**, **Move & Remember Rule**, **Choose Folder Dropdown** (up to 25 whitelisted destinations), **Ignore (Cooldown)**, **1-Click Undo**.
- Slash command group:
  - `/org status` — Bridge health, monitored scopes, AI engine status, and queue counts
  - `/org pending` — List and push pending interactive review cards
  - `/org undo [tx_id]` — Revert the latest (or specified) file move + activate anti-tug-of-war cooldown
  - `/org rules [delete_id]` — Inspect or delete learned Tier 1 rules

---

## Setup in Your Discord Bot

1. Install optional dependencies:
   ```bash
   pip install discord.py aiohttp
   ```
2. Copy `integrations/discord/organizer_cog.py` into your bot's `cogs/` folder.
3. Configure environment variables (or set `bot.pc_config` keys `organizer_bridge_url`, `organizer_token_file`, `channel_id`):
   ```powershell
   $env:OMNI_BRIDGE_URL = "http://127.0.0.1:8766"
   $env:OMNI_BRIDGE_TOKEN_FILE = "C:\path\to\omni-organizer\config\bridge_secret.local.txt"
   $env:OMNI_DISCORD_CHANNEL_ID = "<your_private_channel_id>"
   ```
4. Attach an authorization check on your `commands.Bot` subclass (recommended):
   ```python
   bot.pc_config = {"channel_id": "...", "allowed_user_ids": ["..."]}
   bot.is_user_authorized = lambda user, channel: str(user.id) in ALLOWED_IDS
   await bot.load_extension("cogs.organizer_cog")
   ```

> **Security Note:** The Bridge API binds strictly to `127.0.0.1` (`localhost`) with DNS-rebinding checks and Bearer Token authentication, so the Discord Bot must run locally on the same machine—never expose port `8766` to the public internet.
