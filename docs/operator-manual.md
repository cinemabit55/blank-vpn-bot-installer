# Инструкция оператора Blank VPN Bot Installer

Эта инструкция описывает установку пустого VPN-бота без бренда: бот, RemnaWave-панель, страница подписки, кабинет, базовые алиасы, Telegram Stars по умолчанию и команды для добавления серверов, платежек и баннеров.

## 1. Что подготовить до запуска

Нужен свежий сервер Ubuntu с root-доступом, публичный IPv4 и открытыми портами `80/tcp`, `443/tcp`, `443/udp`.

Нужен домен. Можно использовать Cloudflare или ручную настройку DNS. Если используется Cloudflare, поддомен подписки `sub.example.com` обязательно должен быть DNS-only, без проксирования через Cloudflare. Остальные публичные поддомены можно проксировать, но по умолчанию безопаснее оставить DNS-only до первой проверки.

Нужен Telegram-бот, созданный через BotFather:

- token бота;
- username бота без `@`;
- Telegram ID админа или нескольких админов через запятую.

Нужен Telegram поддержки и режим поддержки:

- `tickets + contact` - в боте есть тикеты и контакт поддержки;
- `tickets only` - только тикеты;
- `contact only` - только контакт поддержки.

Если исходный репозиторий бота приватный, сервер должен иметь доступ к нему до запуска установки. Подойдет SSH deploy key или HTTPS-ссылка с токеном.

## 2. Запуск установки

На свежем сервере:

```bash
sudo bash <(curl -fsSL https://raw.githubusercontent.com/cinemabit55/blank-vpn-bot-installer/main/scripts/install_blank_vpn_bot.sh)
```

Пока GitHub-репозиторий установщика не опубликован, можно запускать из локального checkout:

```bash
sudo bash scripts/install_blank_vpn_bot.sh
```

Скрипт показывает статус каждого этапа: пакеты, Docker, DNS, клонирование бота, генерация конфигов, запуск контейнеров, установка алиасов и итоговый summary.

## 3. Что спрашивает установщик

Установщик попросит:

- ссылку на репозиторий бота и branch/tag;
- директорию установки, обычно `/opt`;
- название проекта, которое попадет в кабинет;
- IPv4 сервера;
- корневой домен;
- домены панели, подписки, кабинета и API;
- email для Let's Encrypt;
- режим DNS: manual или Cloudflare;
- Cloudflare API token, если выбран Cloudflare;
- token Telegram-бота;
- username Telegram-бота;
- Telegram ID админов;
- Telegram поддержки;
- режим поддержки;
- курс Telegram Stars;
- RemnaWave API token, если он уже создан.

В конце установщик выводит ссылки, логины, важные токены и список команд. Копия summary сохраняется в:

```text
/opt/blank-vpn-bot-installer/install-summary.txt
```

Файл доступен только root.

## 4. DNS

Нужно четыре A-записи на IPv4 сервера:

```text
panel.example.com   A   SERVER_IP
sub.example.com     A   SERVER_IP
cabinet.example.com A   SERVER_IP
api.example.com     A   SERVER_IP
```

Правило для Cloudflare: `sub.example.com` всегда DNS-only. Его нельзя проксировать, иначе HAPP/Xray-подписка и некоторые проверки могут работать нестабильно.

## 5. RemnaWave API token

Если token не был введен во время установки:

1. Открой RemnaWave panel.
2. Создай API token.
3. Вставь его в `/opt/bedolaga/.env` как `REMNAWAVE_API_KEY`.
4. Вставь его в `/opt/remnawave/.env.subscription` как `REMNAWAVE_API_TOKEN`.
5. Пересоздай/перезапусти сервисы:

```bash
cd /opt/bedolaga && docker compose up -d --force-recreate bot
cd /opt/remnawave && docker compose restart remnawave-subscription-page
```

## 6. Команды после установки

Основные команды:

```text
add_payment      добавить платежный метод
add_banner       интерактивно заменить баннер
set_banner       заменить баннер одной командой
list_banners     показать установленные баннеры
reset_banner     убрать баннер после backup
bot_commands     показать bot-команды

add_direct       добавить прямое подключение
add_cascade      добавить каскадное подключение
add_inbound      добавить RU edge к иностранной ноде
add_routes       обновить RU direct routing
delete_node      удалить подключение
change_sni       сменить SNI/маскировку
node_commands    показать node-команды
```

Скрипты добавления нод берутся из актуального репозитория бота. В них должны быть заложены последние настройки: XHTTP/REALITY, Firefox/Safari SNI по выбранному сценарию, HAPP-заголовки, фрагментация, no-limit, маршрутизация RU direct, Whoosh direct и UDP direct/WARP-логика из текущей версии бота.

## 7. Добавление платежки

По умолчанию включен только Telegram Stars.

Чтобы добавить платежку:

```bash
add_payment
```

Команда спросит платежный метод, название кнопки в боте, ключи/API/ID магазина и дополнительные URL. После этого она:

- делает backup `/opt/bedolaga/.env`;
- включает нужные env-переменные;
- пересоздает контейнер бота через `docker compose up -d --force-recreate bot`;
- включает метод в таблице `payment_method_configs`, если база уже готова.

Список поддерживаемых методов:

```text
telegram_stars, tribute, cryptobot, heleket, yookassa, mulenpay, pal24,
platega, wata, cloudpayments, freekassa, kassa_ai, riopay, severpay,
paypear, rollypay, overpay, aurapay
```

Посмотреть список без изменений:

```bash
add_payment --list
```

## 8. Баннеры

Загрузить баннер интерактивно:

```bash
add_banner
```

Заменить конкретный слот:

```bash
set_banner main_menu ru /root/banner.png
set_banner profile all /root/profile.webp
```

Проверить состояние:

```bash
list_banners
```

Слоты: `main_menu`, `profile`, `referral`, `support`, `download`, `about`, `resources`, `welcome`.

Языки: `ru`, `en`, `fallback`, `all`.

## 9. Добавление серверов

Для нового пустого бота сначала нужно поднять сам control-plane, затем добавлять ноды алиасами.

Прямое иностранное подключение:

```bash
add_direct
```

Каскад:

```bash
add_cascade
```

Добавить RU edge к существующему иностранному выходу:

```bash
add_inbound
```

Обновить маршруты:

```bash
add_routes
```

Удалить ноду:

```bash
delete_node
```

Сменить SNI/маскировку:

```bash
change_sni
```

## 10. Проверка после установки

Проверь:

- `https://panel.example.com` открывает RemnaWave;
- `https://sub.example.com/connect` открывает страницу подписки;
- `https://cabinet.example.com` открывает кабинет;
- бот отвечает в Telegram;
- `docker compose ps` в `/opt/bedolaga`, `/opt/remnawave`, `/opt/caddy-remnawave` без постоянных рестартов.

Логи:

```bash
cd /opt/bedolaga && docker compose logs -f bot
cd /opt/remnawave && docker compose logs -f
cd /opt/caddy-remnawave && docker compose logs -f
```

## 11. Типовые проблемы

Если панель или подписка не открываются, сначала проверь DNS и Cloudflare proxy mode. `sub` должен быть DNS-only.

Если бот не видит RemnaWave, проверь `REMNAWAVE_API_KEY` в `/opt/bedolaga/.env`.

Если страница подписки не работает, проверь `REMNAWAVE_API_TOKEN` в `/opt/remnawave/.env.subscription`.

Если платежка не появилась после `add_payment`, открой кабинет и проверь раздел платежных методов. Env уже сохранен, но видимость можно включить вручную.

Если после правки `.env` бот не подхватил изменения, используй:

```bash
cd /opt/bedolaga && docker compose up -d --force-recreate bot
```
