# Templar Bot Shortcuts

Install bot maintenance aliases on the bot/control-plane host:

```bash
scripts/install_templar_bot_aliases.sh
bot_commands
```

## Banner commands

The bot reads banner images from `app/assets/banners`. In Docker installs this directory is bind-mounted from the host repo, so replacing a file on the server is enough. Telegram `file_id` values are cached in `data/banner_file_ids.json`; the banner command clears the affected cache keys after each replacement.

| Command | Purpose |
| --- | --- |
| `add_banner` | Interactive banner replacement. Selects slot, language, and local image path on the server. |
| `set_banner SLOT LANG FILE` | Non-interactive banner replacement. Converts PNG/JPG/WebP to optimized JPEG when Pillow is installed. |
| `list_banners` | Lists all banner slots, files, sizes, and modification times. |
| `reset_banner` | Backs up and removes selected banner file(s), then clears Telegram banner cache keys. |

Slots:

```text
main_menu profile referral support download about resources welcome
```

Languages:

```text
ru en fallback all
```

Examples:

```bash
add_banner
set_banner main_menu ru /root/main-menu.png
set_banner profile all /root/profile-banner.webp
list_banners
```

Recommended source image format: 16:9, 1280x720 or 1600x900, with important text away from the edges. The command writes `.jpg` files because the current bot banner map expects JPEG filenames.

For PNG/WebP conversion on the host, install Pillow into a bot-tools venv:

```bash
python3 -m venv /opt/bedolaga/.venv-bot-tools
/opt/bedolaga/.venv-bot-tools/bin/python -m pip install --upgrade pip pillow
```

Do not create `/opt/bedolaga/.venv` only for banner tools: node aliases may also pick it up.
