# Moving the bot to a Vultr VPS — phone guide

Time: about 30 minutes. Cost: $5/month. Everything below works from a phone.

---

## Part 1 — GitHub (already done for you)

The new bot, the server scripts and this guide are already in your
`forex-bot` repo. Until the server is running, GitHub keeps sending
signals as before (now using OANDA data), so there is no gap.

Only check one thing: **Settings → Secrets and variables → Actions** has
`OANDA_API_KEY` (you added it for the backtests). `TWELVE_DATA_API_KEY`
is no longer used.

---

## Part 2 — Create the server on Vultr (10 min)

1. Sign up at **vultr.com** and add a payment method.
2. Tap **Deploy +** → **Deploy New Server**.
3. Choose:
   - **Type:** Cloud Compute – Shared CPU → **Regular Performance**
   - **Location:** London or Frankfurt (either is fine)
   - **Image:** **Ubuntu 24.04 LTS x64**
   - **Plan:** the **$5/month, 1 GB** plan *with IPv4*
     (skip the $2.50 "IPv6 only" plan — GitHub can't be reached from it)
   - **Auto backups:** off (costs extra, not needed)
   - **Hostname:** `fib-bot`
4. Tap **Deploy Now**. Wait until the status says **Running** (1–3 min).
5. Tap the server. Note the **IP Address** and the **Password**
   (tap the eye icon, then copy it).

---

## Part 3 — Connect from your phone (5 min)

1. Install **Termius** (free) from the App Store.
2. **New Host**: Address = the server IP, Username = `root`,
   Password = the Vultr password → Save → tap it to connect.
   Accept the "new host" fingerprint prompt the first time.
3. Paste these two lines (change `YOUR-GITHUB-NAME` to your GitHub username):
   ```
   git clone https://github.com/YOUR-GITHUB-NAME/forex-bot.git
   bash forex-bot/setup_vps.sh
   ```
4. It installs everything, then asks for three keys one at a time.
   The paste is hidden for safety — after each one it shows
   "✓ got 46 characters" so you know it worked:
   - Telegram bot token
   - Telegram chat ID
   - OANDA API token (practice)
5. At the end you get a **🔎 Manual check** message on Telegram.
   That means it works. You can close Termius — the server keeps running.
6. GitHub's own schedule is now switched off, so signals only come from the server.

---

## Everyday use

You never need to log in. The bot checks every minute, 24/7, and sends a
heartbeat at 8am Lagos time every day (weekend heartbeats will say the
market is closed — that's normal).

| Want to | Do this |
|---|---|
| **Daily health check** | Termius: `bash forex-bot/check.sh` (add `--send` to also test Telegram) |
| Status check now | GitHub → Actions → **Run workflow**, or in Termius: `bash forex-bot/run_bot.sh --report` |
| See what the bot did | Termius: `tail -n 40 forex-bot/logs/bot.log` |
| Install a bot update | Upload the new file to GitHub, then Termius: `cd forex-bot && git pull` |
| Change account size | Edit `ACCOUNT_SIZE` in `fib_structure_bot.py` on GitHub, then `git pull` |
| Replace a key | Termius: `bash forex-bot/setup_vps.sh` and answer **y** |

## If something goes wrong

- **No Telegram message after setup** → run `tail -n 30 forex-bot/logs/bot.log`
  and send a screenshot of that (it never shows your keys).
- **"daily data unavailable" for every pair** → the OANDA token is wrong or
  expired. Make a new one in OANDA (Manage API Access), then re-run setup.
- **No heartbeat for a day** → log into Vultr and check the server says Running.

## Safety

- Your keys live only in the file `.env` on the server, readable only by
  the server's admin account. Never upload `.env` to GitHub.
- Never screenshot the Vultr password or anything showing a token.
- The setup turns on the firewall and automatic security updates.
