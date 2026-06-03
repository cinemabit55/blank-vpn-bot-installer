# Blank VPN Bot Installer

Installer for deploying a clean, unbranded VPN bot control-plane based on the Bedolaga/RemnaWave stack.

The installer is intentionally separate from the bot source repository. It clones the bot source, writes fresh production configuration, starts Docker Compose stacks, installs maintenance aliases, and prints a root-only install summary.

## Quick Start

On a fresh Ubuntu server:

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/cinemabit55/blank-vpn-bot-installer/main/scripts/install_blank_vpn_bot.sh)
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
- Telegram Stars as the only enabled payment method by default
- Bot banner aliases: `add_banner`, `list_banners`, `reset_banner`
- Node aliases from the bot repo: `add_direct`, `add_cascade`, `add_inbound`, `add_routes`, `delete_node`, `change_sni`

## Required Input

The installer asks for:

- Bot source repository and branch/tag
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
- Optional RemnaWave API token

If the bot source repository is private, the server must have access before the install starts. Use an SSH deploy key or an HTTPS URL with a token.

## DNS Rules

Create these A records pointing to the server IPv4:

```text
panel.example.com   A   SERVER_IP
sub.example.com     A   SERVER_IP
cabinet.example.com A   SERVER_IP
api.example.com     A   SERVER_IP
```

Important: the subscription domain (`sub.example.com`) must stay DNS-only in Cloudflare. Do not proxy it.

## RemnaWave API Token

The first MVP can accept the RemnaWave API token during install, but it does not create the token automatically yet. If you leave it empty:

1. Open the RemnaWave panel.
2. Create an API token.
3. Put it into `/opt/bedolaga/.env` as `REMNAWAVE_API_KEY`.
4. Put it into `/opt/remnawave/.env.subscription` as `REMNAWAVE_API_TOKEN`.
5. Restart:

```bash
cd /opt/bedolaga && docker compose restart bot
cd /opt/remnawave && docker compose restart remnawave-subscription-page
```

## Non-Interactive Answers

You can pass a JSON file:

```bash
sudo python3 blank_vpn_bot_installer/installer.py --answers answers.example.json
```

See [docs/answers.example.json](docs/answers.example.json).

## Current MVP Limits

- `add_payment` is not implemented in this installer repo yet.
- RemnaWave API token creation is not automated yet.
- Cloudflare DNS upsert is implemented for A records, but advanced domain/proxy policy still needs a dedicated module.
- The bot source repository must be available to the target server.

These are the next modules after the base install flow.
