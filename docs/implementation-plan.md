# Implementation Plan

## Done in v0.1

- Separate installer repository skeleton.
- Shell bootstrap script.
- Python interactive installer.
- Manual or Cloudflare DNS A-record flow.
- Fresh bot `.env` generation.
- Fresh RemnaWave `.env` and subscription-page `.env` generation.
- Caddyfile generation with HAPP stability headers and RU direct routing header.
- Cabinet frontend config generation.
- Docker Compose startup sequence.
- Bot and node alias installation.
- Root-only install summary.

## Next

- `add_payment` command:
  - provider choice;
  - required secret prompts;
  - `.env` update;
  - `PaymentMethodConfig` update in DB;
  - bot restart;
  - method visibility smoke test.
- RemnaWave API token automation or guided pause after panel bootstrap.
- Default neutral banners for blank installs.
- Markdown and PDF operator manual.
- Safer resume system with per-step idempotency checks.
- Optional backup/restore module for new blank installs.
