#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


DEFAULT_BOT_DIR = Path("/opt/bedolaga")


@dataclass(frozen=True)
class PaymentField:
    key: str
    label: str
    default: str = ""
    required: bool = True
    secret: bool = False
    choices: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PaymentProvider:
    method_id: str
    label: str
    enabled_key: str
    display_key: str
    fields: tuple[PaymentField, ...] = ()
    sub_options: dict[str, bool] | None = None
    extra_updates: dict[str, str] = field(default_factory=dict)


def status(message: str) -> None:
    print(f"[status] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[warn] {message}", flush=True)


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    printable = " ".join(args)
    if cwd:
        printable = f"(cd {cwd} && {printable})"
    status(printable)
    return subprocess.run(args, cwd=str(cwd) if cwd else None, check=check, text=True)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def format_env_line(key: str, value: str) -> str:
    normalized = str(value).replace("\r", " ").replace("\n", " ").strip()
    return f"{key}={normalized}"


def update_env_file(path: Path, updates: dict[str, str]) -> Path:
    if not path.exists():
        die(f"Bot .env file was not found: {path}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak.{timestamp}")
    shutil.copy2(path, backup_path)

    lines = path.read_text(encoding="utf-8").splitlines()
    index_by_key: dict[str, int] = {}
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            continue
        key, _value = raw_line.split("=", 1)
        index_by_key[key.strip()] = index

    for key, value in updates.items():
        rendered = format_env_line(key, value)
        if key in index_by_key:
            lines[index_by_key[key]] = rendered
        else:
            lines.append(rendered)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return backup_path


def public_url(env: dict[str, str], path: str = "") -> str:
    base = (env.get("CABINET_URL") or env.get("WEBHOOK_URL") or "").rstrip("/")
    if not base:
        return ""
    if not path:
        return base
    return f"{base}/{path.lstrip('/')}"


def webhook_url(env: dict[str, str], path: str) -> str:
    base = (env.get("WEBHOOK_URL") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/{path.lstrip('/')}"


def build_providers(env: dict[str, str]) -> dict[str, PaymentProvider]:
    return {
        "telegram_stars": PaymentProvider(
            method_id="telegram_stars",
            label="Telegram Stars",
            enabled_key="TELEGRAM_STARS_ENABLED",
            display_key="TELEGRAM_STARS_DISPLAY_NAME",
            fields=(
                PaymentField("TELEGRAM_STARS_RATE_RUB", "RUB rate for one Star", "1.3"),
            ),
        ),
        "tribute": PaymentProvider(
            method_id="tribute",
            label="Tribute",
            enabled_key="TRIBUTE_ENABLED",
            display_key="TRIBUTE_DISPLAY_NAME",
            fields=(
                PaymentField("TRIBUTE_DONATE_LINK", "Tribute donate/payment link"),
                PaymentField("TRIBUTE_API_KEY", "Tribute API key", required=False, secret=True),
            ),
        ),
        "cryptobot": PaymentProvider(
            method_id="cryptobot",
            label="CryptoBot",
            enabled_key="CRYPTOBOT_ENABLED",
            display_key="CRYPTOBOT_DISPLAY_NAME",
            fields=(
                PaymentField("CRYPTOBOT_API_TOKEN", "CryptoBot API token", secret=True),
                PaymentField("CRYPTOBOT_WEBHOOK_SECRET", "Webhook secret", required=False, secret=True),
                PaymentField("CRYPTOBOT_DEFAULT_ASSET", "Default asset", "USDT"),
                PaymentField("CRYPTOBOT_TESTNET", "Use testnet", "false", choices=(("false", "no"), ("true", "yes"))),
            ),
        ),
        "heleket": PaymentProvider(
            method_id="heleket",
            label="Heleket Crypto",
            enabled_key="HELEKET_ENABLED",
            display_key="HELEKET_DISPLAY_NAME",
            fields=(
                PaymentField("HELEKET_MERCHANT_ID", "Merchant ID"),
                PaymentField("HELEKET_API_KEY", "API key", secret=True),
                PaymentField("HELEKET_DEFAULT_CURRENCY", "Default currency", "USDT"),
                PaymentField("HELEKET_DEFAULT_NETWORK", "Default network", required=False),
                PaymentField("HELEKET_CALLBACK_URL", "Callback URL", webhook_url(env, "/heleket-webhook"), required=False),
                PaymentField("HELEKET_RETURN_URL", "Return URL", public_url(env), required=False),
                PaymentField("HELEKET_SUCCESS_URL", "Success URL", public_url(env), required=False),
            ),
        ),
        "yookassa": PaymentProvider(
            method_id="yookassa",
            label="YooKassa",
            enabled_key="YOOKASSA_ENABLED",
            display_key="YOOKASSA_DISPLAY_NAME",
            fields=(
                PaymentField("YOOKASSA_SHOP_ID", "Shop ID"),
                PaymentField("YOOKASSA_SECRET_KEY", "Secret key", secret=True),
                PaymentField("YOOKASSA_RETURN_URL", "Return URL", public_url(env), required=False),
                PaymentField("YOOKASSA_DEFAULT_RECEIPT_EMAIL", "Default receipt email", required=False),
                PaymentField("YOOKASSA_SBP_ENABLED", "Enable SBP", "true", choices=(("true", "yes"), ("false", "no"))),
                PaymentField("YOOKASSA_TEST_MODE", "Test mode", "false", choices=(("false", "no"), ("true", "yes"))),
            ),
            sub_options={"card": True, "sbp": True},
        ),
        "mulenpay": PaymentProvider(
            method_id="mulenpay",
            label="Mulen Pay",
            enabled_key="MULENPAY_ENABLED",
            display_key="MULENPAY_DISPLAY_NAME",
            fields=(
                PaymentField("MULENPAY_API_KEY", "API key", secret=True),
                PaymentField("MULENPAY_SECRET_KEY", "Secret key", secret=True),
                PaymentField("MULENPAY_SHOP_ID", "Shop ID"),
                PaymentField("MULENPAY_WEBSITE_URL", "Website URL", public_url(env), required=False),
                PaymentField("MULENPAY_IFRAME_EXPECTED_ORIGIN", "Iframe expected origin", public_url(env), required=False),
            ),
        ),
        "pal24": PaymentProvider(
            method_id="pal24",
            label="PAL24 / PayPalych",
            enabled_key="PAL24_ENABLED",
            display_key="PAL24_DISPLAY_NAME",
            fields=(
                PaymentField("PAL24_API_TOKEN", "API token", secret=True),
                PaymentField("PAL24_SHOP_ID", "Shop ID"),
                PaymentField("PAL24_SIGNATURE_TOKEN", "Signature token", required=False, secret=True),
                PaymentField("PAL24_SBP_BUTTON_VISIBLE", "Show SBP button", "true", choices=(("true", "yes"), ("false", "no"))),
                PaymentField("PAL24_CARD_BUTTON_VISIBLE", "Show card button", "true", choices=(("true", "yes"), ("false", "no"))),
            ),
            sub_options={"sbp": True, "card": True},
        ),
        "platega": PaymentProvider(
            method_id="platega",
            label="Platega",
            enabled_key="PLATEGA_ENABLED",
            display_key="PLATEGA_DISPLAY_NAME",
            fields=(
                PaymentField("PLATEGA_MERCHANT_ID", "Merchant ID"),
                PaymentField("PLATEGA_SECRET", "Secret", secret=True),
                PaymentField("PLATEGA_RETURN_URL", "Return URL", public_url(env), required=False),
                PaymentField("PLATEGA_FAILED_URL", "Failed URL", public_url(env), required=False),
                PaymentField("PLATEGA_ACTIVE_METHODS", "Active method IDs", "2,11,12,13"),
            ),
        ),
        "wata": PaymentProvider(
            method_id="wata",
            label="Wata",
            enabled_key="WATA_ENABLED",
            display_key="WATA_DISPLAY_NAME",
            fields=(
                PaymentField("WATA_ACCESS_TOKEN", "Access token", secret=True),
                PaymentField("WATA_TERMINAL_PUBLIC_ID", "Terminal public ID", required=False),
                PaymentField("WATA_SUCCESS_REDIRECT_URL", "Success redirect URL", public_url(env), required=False),
                PaymentField("WATA_FAIL_REDIRECT_URL", "Fail redirect URL", public_url(env), required=False),
            ),
        ),
        "cloudpayments": PaymentProvider(
            method_id="cloudpayments",
            label="CloudPayments",
            enabled_key="CLOUDPAYMENTS_ENABLED",
            display_key="CLOUDPAYMENTS_DISPLAY_NAME",
            fields=(
                PaymentField("CLOUDPAYMENTS_PUBLIC_ID", "Public ID"),
                PaymentField("CLOUDPAYMENTS_API_SECRET", "API secret", secret=True),
                PaymentField("CLOUDPAYMENTS_RETURN_URL", "Return URL", public_url(env), required=False),
                PaymentField("CLOUDPAYMENTS_REQUIRE_EMAIL", "Require email", "false", choices=(("false", "no"), ("true", "yes"))),
                PaymentField("CLOUDPAYMENTS_TEST_MODE", "Test mode", "false", choices=(("false", "no"), ("true", "yes"))),
            ),
        ),
        "freekassa": PaymentProvider(
            method_id="freekassa",
            label="Freekassa",
            enabled_key="FREEKASSA_ENABLED",
            display_key="FREEKASSA_DISPLAY_NAME",
            fields=(
                PaymentField("FREEKASSA_SHOP_ID", "Shop ID"),
                PaymentField("FREEKASSA_API_KEY", "API key", secret=True),
                PaymentField("FREEKASSA_SECRET_WORD_1", "Secret word 1", secret=True),
                PaymentField("FREEKASSA_SECRET_WORD_2", "Secret word 2", secret=True),
                PaymentField("FREEKASSA_USE_API", "Use API", "false", choices=(("false", "no"), ("true", "yes"))),
                PaymentField("FREEKASSA_SBP_ENABLED", "Show separate SBP method", "false", choices=(("false", "no"), ("true", "yes"))),
                PaymentField("FREEKASSA_CARD_ENABLED", "Show separate card method", "false", choices=(("false", "no"), ("true", "yes"))),
            ),
            sub_options={"sbp": True, "card": True},
        ),
        "kassa_ai": PaymentProvider(
            method_id="kassa_ai",
            label="KassaAI",
            enabled_key="KASSA_AI_ENABLED",
            display_key="KASSA_AI_DISPLAY_NAME",
            fields=(
                PaymentField("KASSA_AI_SHOP_ID", "Shop ID"),
                PaymentField("KASSA_AI_API_KEY", "API key", secret=True),
                PaymentField("KASSA_AI_SECRET_WORD_2", "Secret word 2", secret=True),
                PaymentField("KASSA_AI_PAYMENT_SYSTEM_ID", "Default payment system ID", "44"),
                PaymentField("KASSA_AI_SBP_ENABLED", "Show separate SBP method", "false", choices=(("false", "no"), ("true", "yes"))),
                PaymentField("KASSA_AI_CARD_ENABLED", "Show separate card method", "false", choices=(("false", "no"), ("true", "yes"))),
                PaymentField("KASSA_AI_SBERPAY_ENABLED", "Show separate SberPay method", "false", choices=(("false", "no"), ("true", "yes"))),
            ),
            sub_options={"sbp": True, "card": True, "sberpay": True},
        ),
        "riopay": PaymentProvider(
            method_id="riopay",
            label="RioPay",
            enabled_key="RIOPAY_ENABLED",
            display_key="RIOPAY_DISPLAY_NAME",
            fields=(
                PaymentField("RIOPAY_API_TOKEN", "API token", secret=True),
                PaymentField("RIOPAY_WEBHOOK_SECRET", "Webhook secret", required=False, secret=True),
                PaymentField("RIOPAY_SUCCESS_URL", "Success URL", public_url(env), required=False),
                PaymentField("RIOPAY_FAIL_URL", "Fail URL", public_url(env), required=False),
            ),
        ),
        "severpay": PaymentProvider(
            method_id="severpay",
            label="SeverPay",
            enabled_key="SEVERPAY_ENABLED",
            display_key="SEVERPAY_DISPLAY_NAME",
            fields=(
                PaymentField("SEVERPAY_MID", "Merchant ID"),
                PaymentField("SEVERPAY_TOKEN", "Token", secret=True),
                PaymentField("SEVERPAY_RETURN_URL", "Return URL", public_url(env), required=False),
                PaymentField("SEVERPAY_LIFETIME", "Invoice lifetime minutes", "1440"),
            ),
        ),
        "paypear": PaymentProvider(
            method_id="paypear",
            label="PayPear",
            enabled_key="PAYPEAR_ENABLED",
            display_key="PAYPEAR_DISPLAY_NAME",
            fields=(
                PaymentField("PAYPEAR_SHOP_ID", "Shop ID"),
                PaymentField("PAYPEAR_SECRET_KEY", "Secret key", secret=True),
                PaymentField("PAYPEAR_RETURN_URL", "Return URL", public_url(env), required=False),
                PaymentField(
                    "PAYPEAR_PAYMENT_METHOD",
                    "Payment method",
                    "sbp",
                    choices=(("sbp", "SBP"), ("bank_card", "bank card"), ("sberpay", "SberPay"), ("tpay", "T-Pay")),
                ),
            ),
            sub_options={"bank_card": True, "sbp": True, "sberpay": True, "tpay": True},
        ),
        "rollypay": PaymentProvider(
            method_id="rollypay",
            label="RollyPay",
            enabled_key="ROLLYPAY_ENABLED",
            display_key="ROLLYPAY_DISPLAY_NAME",
            fields=(
                PaymentField("ROLLYPAY_API_KEY", "API key", secret=True),
                PaymentField("ROLLYPAY_SIGNING_SECRET", "Signing secret", secret=True),
                PaymentField("ROLLYPAY_RETURN_URL", "Return URL", public_url(env), required=False),
            ),
            sub_options={"sbp": True, "card": True, "crypto": True},
        ),
        "overpay": PaymentProvider(
            method_id="overpay",
            label="Overpay",
            enabled_key="OVERPAY_ENABLED",
            display_key="OVERPAY_DISPLAY_NAME",
            fields=(
                PaymentField("OVERPAY_USERNAME", "Username"),
                PaymentField("OVERPAY_PASSWORD", "Password", secret=True),
                PaymentField("OVERPAY_PROJECT_ID", "Project ID"),
                PaymentField("OVERPAY_P12_PATH", "P12 certificate path", required=False),
                PaymentField("OVERPAY_P12_PASSPHRASE", "P12 passphrase", required=False, secret=True),
                PaymentField("OVERPAY_RETURN_URL", "Return URL", public_url(env), required=False),
                PaymentField("OVERPAY_PAYMENT_METHODS", "Payment methods", "card,fps"),
            ),
            sub_options={"card": True, "fps": True},
        ),
        "aurapay": PaymentProvider(
            method_id="aurapay",
            label="AuraPay",
            enabled_key="AURAPAY_ENABLED",
            display_key="AURAPAY_DISPLAY_NAME",
            fields=(
                PaymentField("AURAPAY_API_KEY", "API key", secret=True),
                PaymentField("AURAPAY_SHOP_ID", "Shop ID"),
                PaymentField("AURAPAY_SECRET_KEY", "Secret key", secret=True),
                PaymentField("AURAPAY_RETURN_URL", "Return URL", public_url(env), required=False),
                PaymentField("AURAPAY_PAYMENT_LIFETIME_MINUTES", "Payment lifetime minutes", "60"),
            ),
            sub_options={"card": True, "sbp": True},
        ),
    }


def choose_provider(providers: dict[str, PaymentProvider], explicit: str | None) -> PaymentProvider:
    if explicit:
        if explicit not in providers:
            die(f"Unknown payment method: {explicit}. Available: {', '.join(providers)}")
        return providers[explicit]

    print("Available payment methods:")
    for index, (method_id, provider) in enumerate(providers.items(), start=1):
        print(f"{index}. {provider.label} ({method_id})")

    while True:
        raw = input(f"Select payment method [1-{len(providers)}]: ").strip()
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(providers):
                return list(providers.values())[index - 1]
        if raw in providers:
            return providers[raw]
        print("Invalid choice.")


def prompt_choice(field: PaymentField, default: str) -> str:
    print(field.label)
    for index, (value, label) in enumerate(field.choices, start=1):
        suffix = " [default]" if value == default else ""
        print(f"{index}. {label} ({value}){suffix}")
    while True:
        raw = input(f"Choose [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(field.choices):
                return field.choices[index - 1][0]
        allowed = {value for value, _label in field.choices}
        if raw in allowed:
            return raw
        print("Invalid choice.")


def prompt_field(field: PaymentField, env: dict[str, str]) -> str:
    existing = env.get(field.key, "")
    default = existing or field.default
    if field.choices:
        return prompt_choice(field, default)

    while True:
        suffix = ""
        if default and field.secret:
            suffix = " [keep existing]" if existing else " [hidden default]"
        elif default:
            suffix = f" [{default}]"
        if field.secret:
            raw = getpass.getpass(f"{field.label}{suffix}: ").strip()
        else:
            raw = input(f"{field.label}{suffix}: ").strip()
        if raw:
            return raw
        if default:
            return default
        if not field.required:
            return ""
        print("Required value.")


def collect_updates(provider: PaymentProvider, env: dict[str, str]) -> tuple[dict[str, str], str]:
    default_display = env.get(provider.display_key) or provider.label
    display_name = input(f"Button display name [{default_display}]: ").strip() or default_display
    updates = {
        provider.enabled_key: "true",
        provider.display_key: display_name,
        **provider.extra_updates,
    }
    for field_spec in provider.fields:
        value = prompt_field(field_spec, env)
        if value != "":
            updates[field_spec.key] = value
    return updates, display_name


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def payment_config_sql(provider: PaymentProvider, display_name: str) -> str:
    sub_options = "NULL"
    if provider.sub_options is not None:
        sub_options = f"{sql_quote(json.dumps(provider.sub_options, ensure_ascii=False))}::json"
    display = sql_quote(display_name)
    method = sql_quote(provider.method_id)
    return f"""
DO $$
BEGIN
  IF to_regclass('public.payment_method_configs') IS NULL THEN
    RAISE NOTICE 'payment_method_configs table does not exist yet';
    RETURN;
  END IF;

  IF EXISTS (SELECT 1 FROM payment_method_configs WHERE method_id = {method}) THEN
    UPDATE payment_method_configs
       SET is_enabled = TRUE,
           display_name = {display},
           sub_options = COALESCE(sub_options, {sub_options}),
           updated_at = now()
     WHERE method_id = {method};
  ELSE
    INSERT INTO payment_method_configs (
      method_id,
      sort_order,
      is_enabled,
      display_name,
      sub_options,
      user_type_filter,
      first_topup_filter,
      promo_group_filter_mode,
      created_at,
      updated_at
    )
    VALUES (
      {method},
      COALESCE((SELECT MAX(sort_order) FROM payment_method_configs), -1) + 1,
      TRUE,
      {display},
      {sub_options},
      'all',
      'any',
      'all',
      now(),
      now()
    );
  END IF;
END $$;
"""


def enable_method_in_db(bot_dir: Path, env: dict[str, str], provider: PaymentProvider, display_name: str) -> None:
    if shutil.which("docker") is None:
        warn("docker is not available; payment method DB visibility was not updated")
        return
    sql = payment_config_sql(provider, display_name)
    postgres_user = env.get("POSTGRES_USER", "remnawave_user")
    postgres_db = env.get("POSTGRES_DB", "remnawave_bot")
    postgres_password = env.get("POSTGRES_PASSWORD", "")
    args = [
        "docker",
        "compose",
        "exec",
        "-T",
        "-e",
        f"PGPASSWORD={postgres_password}",
        "postgres",
        "psql",
        "-U",
        postgres_user,
        "-d",
        postgres_db,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    ]
    try:
        run(args, cwd=bot_dir)
    except subprocess.CalledProcessError:
        warn("Could not update payment_method_configs. The provider env was saved; enable the method in cabinet if needed.")


def recreate_bot_service(bot_dir: Path) -> None:
    if shutil.which("docker") is None:
        warn("docker is not available; skipping bot service recreation")
        return
    try:
        run(["docker", "compose", "up", "-d", "--force-recreate", "bot"], cwd=bot_dir)
    except subprocess.CalledProcessError:
        warn("Bot service recreate failed. Check docker compose logs in the bot directory.")


def print_provider_list(providers: dict[str, PaymentProvider]) -> None:
    print("Supported payment methods:")
    for method_id, provider in providers.items():
        required = [field_spec.key for field_spec in provider.fields if field_spec.required]
        print(f"- {method_id}: {provider.label}")
        if required:
            print(f"  required env: {', '.join(required)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Add or enable a payment method for the installed VPN bot")
    parser.add_argument("method", nargs="?", help="payment method id, e.g. yookassa, cryptobot, wata")
    parser.add_argument("--bot-dir", type=Path, default=DEFAULT_BOT_DIR, help="installed bot directory")
    parser.add_argument("--list", action="store_true", help="list supported methods")
    parser.add_argument("--no-recreate", action="store_true", help="do not recreate the bot container")
    parser.add_argument("--no-db", action="store_true", help="do not update payment_method_configs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bot_dir = args.bot_dir.expanduser().resolve()
    env_path = bot_dir / ".env"
    env = parse_env(env_path)
    providers = build_providers(env)

    if args.list:
        print_provider_list(providers)
        return 0

    if not env_path.exists():
        die(f"Bot .env file was not found: {env_path}")

    provider = choose_provider(providers, args.method)
    status(f"selected payment method: {provider.label} ({provider.method_id})")
    updates, display_name = collect_updates(provider, env)
    backup = update_env_file(env_path, updates)
    status(f"updated {env_path}")
    status(f"backup saved: {backup}")

    updated_env = parse_env(env_path)
    if not args.no_recreate:
        recreate_bot_service(bot_dir)
    if not args.no_db:
        enable_method_in_db(bot_dir, updated_env, provider, display_name)

    print()
    print(f"Payment method enabled: {provider.label}")
    print("Check the bot payment screen or the cabinet payment-method settings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
