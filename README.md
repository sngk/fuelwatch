# FuelWatch Discord notifier

Fetches today's and tomorrow's prices from the official WA FuelWatch RSS feed, compares them, and posts the cheapest results to a Discord webhook. It reports whether tomorrow's cheapest price is cheaper, dearer, or unchanged, estimates the difference for a 50-litre fill, and compares matching stations. The default configuration covers:

- Diesel: Perth, Quinns Rocks, Bayswater
- Unleaded Petrol: Bayswater, Balcatta

FuelWatch publishes tomorrow's prices after 2:30 pm AWST. The included schedule runs at 2:40 pm AWST each day.

## Quick setup with GitHub Actions

1. Create a Discord webhook: **Server Settings → Integrations → Webhooks → New Webhook**, then copy its URL.
2. Push this repository to GitHub.
3. In the GitHub repository, open **Settings → Secrets and variables → Actions → New repository secret**.
4. Name the secret `DISCORD_WEBHOOK_URL` and paste the webhook URL as its value.
5. Open **Actions → Daily FuelWatch notification → Run workflow** to test it. Scheduled runs occur daily at 06:40 UTC / 14:40 AWST. GitHub may start scheduled jobs a few minutes late.

## Configure searches

Edit `config.json`. Each search accepts:

- `product`: `1` Unleaded, `2` Premium Unleaded, `4` Diesel, `5` LPG, `6` 98 RON, `10` E85, or `11` Brand diesel
- `suburb`: a WA suburb name
- `surrounding`: whether FuelWatch should include nearby suburbs
- `limit`: optional result count overriding `results_per_search`

The bot always requests both `today` and `tomorrow`. The webhook URL is deliberately not stored in the config or committed to Git.

## Run locally

PowerShell:

```powershell
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
python .\fuelwatch_bot.py --once
```

Fetch and preview without posting:

```powershell
python .\fuelwatch_bot.py --dry-run
```

Run continuously (the process waits for the configured AWST time each day):

```powershell
python .\fuelwatch_bot.py
```

Or with Docker:

```powershell
docker build -t fuelwatch-bot .
docker run -d --restart unless-stopped -e DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." fuelwatch-bot
```

The Discord message links each section to the official FuelWatch query and acknowledges FuelWatch as its data source.
