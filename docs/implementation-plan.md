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
- Markdown and PDF operator manual.
- `add_payment` helper:
  - provider choice;
  - required credential prompts;
  - `.env` backup/update;
  - bot container recreate so env changes apply;
  - `PaymentMethodConfig` visibility update when the table exists.

## Next

- RemnaWave API token automation or guided pause after panel bootstrap.
- Default neutral banners for blank installs.
- Safer resume system with per-step idempotency checks.
- Optional backup/restore module for new blank installs.
