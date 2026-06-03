# Blank VPN Bot Installer

Installer for deploying a clean, unbranded VPN bot control-plane based on the Bedolaga/RemnaWave stack.

The installer is self-contained by default: it uses the bundled `bot-source/` snapshot, writes fresh production configuration, creates the RemnaWave admin/API token, starts Docker Compose stacks, installs maintenance aliases, and prints a root-only install summary.

## Quick Start

On a fresh Ubuntu server:

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/cinemabit55/blank-vpn-bot-installer/main/scripts/install_blank_vpn_bot.sh)
```

For a private repository, pass a GitHub token that can read this repository:

```bash
export INSTALLER_GITHUB_TOKEN=github_pat_or_classic_token
curl -fsSL -H "Authorization: Bearer $INSTALLER_GITHUB_TOKEN" \
  https://raw.githubusercontent.com/cinemabit55/blank-vpn-bot-installer/main/scripts/install_blank_vpn_bot.sh \
  | sudo INSTALLER_GITHUB_TOKEN="$INSTALLER_GITHUB_TOKEN" bash
```

Until the GitHub repository is created, run locally from a cloned checkout:

```bash
sudo bash scripts/install_blank_vpn_bot.sh
```

## What It Installs

- Bot stack in `/opt/bedolaga`
- RemnaWave stack in `/opt/remnawave`
- Caddy reverse proxy in `/opt/caddy-remnawave`
- Cabinet frontend in `/opt/cabinet`
- RemnaWave admin account and API token
- Telegram Stars as the only enabled payment method by default
- Neutral default banner pack for the bot screens
- Non-destructive source sync on reruns, preserving runtime `data`, `logs`, and uploads
- Payment alias: `add_payment`
- Backup alias: `backup_install`
- Bot banner aliases: `add_banner`, `list_banners`, `reset_banner`
- Node aliases from the bot repo: `add_direct`, `add_cascade`, `add_inbound`, `add_routes`, `delete_node`, `change_sni`

## Required Input

The installer asks for:

- Bot source mode:
  - bundled `bot-source/` from this installer repository
  - optional external Git repository and branch/tag
- Server IPv4
- Root domain and generated subdomains:
  - `panel.example.com`
  - `sub.example.com`
  - `cabinet.example.com`
  - `api.example.com`
- DNS mode: manual or Cloudflare API
- Telegram bot token and optional bot username
- Telegram admin IDs
- Telegram support username
- Support mode:
  - tickets + contact
  - tickets only
  - contact only
- Telegram Stars RUB rate
- RemnaWave admin username
- Optional RemnaWave admin password. If empty, the installer generates one.
- Optional existing RemnaWave API token. If empty, the installer creates one automatically.

If you choose external Git source mode and the bot source repository is private, the server must have access before the install starts. Use an SSH deploy key or an HTTPS URL with a token.

## DNS Rules

Create these A records pointing to the server IPv4:

```text
panel.example.com   A   SERVER_IP
sub.example.com     A   SERVER_IP
cabinet.example.com A   SERVER_IP
api.example.com     A   SERVER_IP
```

Important: the subscription domain (`sub.example.com`) must stay DNS-only in Cloudflare. Do not proxy it.

## RemnaWave Bootstrap

For normal installs the RemnaWave token step is automatic:

1. Start the RemnaWave stack.
2. Register the RemnaWave admin if registration is open.
3. Login with the configured admin credentials.
4. Create a long-lived API token through `POST /api/tokens`.
5. Write that token into `/opt/bedolaga/.env` and `/opt/remnawave/.env.subscription`.
6. Start the bot, cabinet, subscription page, and Caddy.

The final root-only summary includes the RemnaWave panel URL, admin username/password, generated API token, metrics credentials, and bot Web API token.

## Adding Payment Methods

After installation, Telegram Stars is enabled by default. To add another provider:

```bash
add_payment
```

The command asks for the provider, button display name, required API credentials, optional redirect URLs, then:

- backs up `/opt/bedolaga/.env`;
- writes provider env vars;
- recreates the bot container so new env vars are applied;
- enables the provider in `payment_method_configs` when the database table is available.

Supported methods include YooKassa, CryptoBot, Heleket, MulenPay, PAL24, Platega, WATA, CloudPayments, Freekassa, KassaAI, RioPay, SeverPay, PayPear, RollyPay, Overpay, AuraPay, Tribute, and Telegram Stars.

## Backups

Create a root-only backup archive with env files, installer state, Caddy config, and SQL dumps when Docker is available:

```bash
backup_install
```

Use `backup_install --include-runtime` to also include bot `data`, `uploads`, and banner files.

## Non-Interactive Answers

You can pass a JSON file:

```bash
sudo python3 blank_vpn_bot_installer/installer.py --answers answers.example.json
```

See [docs/answers.example.json](docs/answers.example.json).

## Operator Manual

- Markdown: [docs/operator-manual.md](docs/operator-manual.md)
- PDF: [docs/operator-manual.pdf](docs/operator-manual.pdf)

Rebuild the PDF after editing the Markdown:

```bash
python3 scripts/build_manual_pdf.py
```

## Current MVP Limits

- Cloudflare DNS upsert is implemented for A records, but advanced domain/proxy policy still needs a dedicated module.
- External Git source mode requires repository access from the target server.
- Default banners are intentionally neutral. Use `add_banner` to replace them with operator-provided images.

These are the next modules after the base install flow.
