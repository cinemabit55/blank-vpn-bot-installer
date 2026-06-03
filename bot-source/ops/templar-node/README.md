# VPN Node CLI

Первый слой автоматизации для настройки VPN-нод.

Текущее состояние:

- читает node config YAML;
- валидирует роли `foreign-exit`, `ru-edge`, `ru-warp`;
- строит dry-run план `Layer 2a -> Layer 1 -> Layer 2b`;
- генерирует node config YAML для сценариев `cascade-direct`, дополнительной `ru-edge` к уже существующему foreign-exit и `ru-warp`; быстрые команды по умолчанию используют `remote_dest`/xHTTP без локального decoy-сайта;
- проверяет локальные `secrets/...` refs через filesystem secret store;
- записывает локальные secrets через prompt/stdin/file без вывода значения в stdout;
- проверяет реальные RemnaWave и Cloudflare API read-only командами;
- получает RemnaWave Node `SECRET_KEY` через `/api/keygen` и пишет его в secret store;
- рендерит полноценный RemnaWave/Xray Config Profile для REALITY, transit, DNS routing и Xray-native WARP;
- выполняет live RemnaWave Layer 2a через `pre-bootstrap --adapter http`: создает/обновляет Config Profile и Node без ручных действий в панели;
- выполняет live RemnaWave Layer 2b через `post-bootstrap --adapter http`: проверяет online Node, создает/обновляет Host, Internal/External Squad и transit service user;
- создает/обновляет Cloudflare DNS-only A/AAAA записи для node-domain;
- ведет локальный control-plane state/checkpoints atomic JSON-файлами;
- рендерит локальные bootstrap artifacts: RemnaWave Node compose/env, Caddyfile, UFW plan, Xray snippets и статический decoy-site;
- выполняет первый live SSH bootstrap чистого VPS через `sshpass` + root password secret;
- умеет Bedolaga DB adapter для привязки уже обнаруженных RemnaWave squad UUID к тарифам и resync подписок;
- симулирует end-to-end onboarding в fake RemnaWave/Bedolaga/SSH окружении;
- ведет локальный route overrides YAML для RU-direct маршрутов на каскадной ноде и умеет применять его в RemnaWave/Xray Config Profile;
- регистрирует Cloudflare WARP через `warp-register`, пишет `warp.registration_ref` JSON-секрет и вычисляет `reserved` из WARP `client_id`; live `pre-bootstrap --adapter http` делает это автоматически для `warp.mode: xray_native`, если секрет еще не существует.
- дает верхний операторский слой `operator` с 5 сценариями: `cascade-direct`, `ru-direct`, `routing-add`, `ru-direct-remote`, `ru-edge-add`.
- готовит и выполняет подтвержденную node-domain rotation через `rotate-domain`: rotated YAML, DNS-only Cloudflare records, REALITY `serverNames` backup/update, certificate/bootstrap, RemnaWave Host/Profile update, Bedolaga resync и rollback state.
- запускает alert-only `ru-edge-check` для проверки foreign-exit из российской RU-edge vantage point: DNS, TCP 443, TLS, decoy HTTP и transit `10443`.
- запускает synthetic `synthetic-vpn-check`: локальный Xray client, SOCKS, HTTP probe через VPN и WARP egress check по Cloudflare trace.
- выполняет ручный `decommission`: сначала dry-run, затем по `--yes` удаляет RemnaWave/Bedolaga объекты и выбранные хвосты state/render/secrets/DNS/routes/monitor/remote VPS.

Примеры:

Команды предполагают активированное Python-окружение с зависимостями из `requirements.txt` / `uv.lock`.

```bash
python3 scripts/templar_node.py validate ops/templar-node/examples/foreign-exit.yml
python3 scripts/templar_node.py plan ops/templar-node/examples/ru-edge.yml
python3 scripts/templar_node.py plan ops/templar-node/examples/ru-warp.yml --format json
python3 scripts/templar_node.py bootstrap --dry-run ops/templar-node/examples/ru-warp.yml
python3 scripts/templar_node.py secrets-check ops/templar-node/examples/foreign-exit.yml --secrets-dir /opt/templar/secrets
python3 scripts/templar_node.py secret-set secrets/remnawave-api-key --secrets-dir /opt/templar/secrets
python3 scripts/templar_node.py secret-set secrets/cloudflare-api-token --secrets-dir /opt/templar/secrets
python3 scripts/templar_node.py remnawave-check ops/templar-node/examples/foreign-exit.yml --api-key-ref secrets/remnawave-api-key --secrets-dir /opt/templar/secrets
python3 scripts/templar_node.py remnawave-keygen ops/templar-node/examples/foreign-exit.yml --api-key-ref secrets/remnawave-api-key --auth-type bearer --secrets-dir /opt/templar/secrets --overwrite
python3 scripts/templar_node.py cloudflare-check example.com example.net --api-token-ref secrets/cloudflare-api-token --secrets-dir /opt/templar/secrets
python3 scripts/templar_node.py dns-upsert ops/templar-node/examples/foreign-exit.yml --secrets-dir /opt/templar/secrets
python3 scripts/templar_node.py rotate-domain ops/templar-node/examples/ru-warp.yml --to ru-warp-01b.example.com --output-config /opt/templar/configs/ru-warp-01b.yml --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes --upsert-dns --update-reality-secret
python3 scripts/templar_node.py rotate-domain /opt/templar/configs/ru-warp.yml --to ru-warp-01b.example.com --output-config /opt/templar/configs/ru-warp-01b.yml --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes --render-dir /var/lib/templar-onboarding/render --upsert-dns --update-reality-secret --switch --confirm-switch --switch-adapter live --api-key-ref secrets/remnawave-api-key --auth-type bearer --root-password-ref secrets/ssh-root-password-ru-test
python3 scripts/templar_node.py delete --config-dir /opt/templar/configs --full --yes
scripts/install_templar_node_aliases.sh
delete_node
node_commands
python3 scripts/templar_node.py ru-edge-check /opt/templar/configs/foreign-exit.yml --ru-edge-host 192.0.2.30 --ru-edge-private-key-ref secrets/ssh-admin-private-key --secrets-dir /opt/templar/secrets
python3 scripts/templar_node.py synthetic-vpn-check /opt/templar/configs/ru-warp.yml --client-config /opt/templar/synthetic/ru-warp-test-outbound.json --xray-bin /usr/local/bin/xray --expect-warp
python3 scripts/templar_node.py warp-register ops/templar-node/examples/ru-warp.yml --secrets-dir /opt/templar/secrets
python3 scripts/templar_node.py state-init ops/templar-node/examples/foreign-exit.yml --state-dir /var/lib/templar-onboarding/nodes
python3 scripts/templar_node.py state-mark ops/templar-node/examples/foreign-exit.yml remnawave_node_registered --state-dir /var/lib/templar-onboarding/nodes
python3 scripts/templar_node.py render ops/templar-node/examples/foreign-exit.yml --output-dir /tmp/templar-render
python3 scripts/templar_node.py simulate ops/templar-node/examples/foreign-exit.yml --env-dir /tmp/templar-fake-env --state-dir /tmp/templar-state --render-dir /tmp/templar-render
python3 scripts/templar_node.py simulate ops/templar-node/examples/foreign-exit.yml ops/templar-node/examples/ru-edge.yml --env-dir /tmp/templar-fake-env --state-dir /tmp/templar-state
python3 scripts/templar_node.py route-add ops/templar-node/examples/ru-edge.yml --routes-file /tmp/templar-routes.yml --domain gosuslugi.ru --ip 203.0.113.5 --comment "direct from RU"
python3 scripts/templar_node.py route-apply ops/templar-node/examples/ru-edge.yml --routes-file /tmp/templar-routes.yml --adapter http --api-key-ref secrets/remnawave-api-key --auth-type bearer --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes
python3 scripts/templar_node.py generate ru-warp --output-dir /tmp/templar-configs --main-ipv4 198.51.100.20 --remnawave-api-url https://panel.example.com --admin-allowlist 198.51.100.20 --internal-name RU-WARP-01 --display-name "RU WARP" --domain ru-warp-01.example.com --ipv4 192.0.2.40 --tariff-name "Базовый"
python3 scripts/templar_node.py generate ru-warp --output-dir /tmp/templar-configs --main-ipv4 198.51.100.20 --remnawave-api-url https://panel.example.com --admin-allowlist 198.51.100.20 --internal-name RU-WARP-IP-01 --display-name "RU WARP IP" --ipv4 192.0.2.41 --reality-strategy remote_dest --reality-target ya.ru:443 --reality-server-name ya.ru --tariff-name "Базовый"
python3 scripts/templar_node.py generate cascade-direct --output-dir /tmp/templar-configs --main-ipv4 198.51.100.20 --remnawave-api-url https://panel.example.com --admin-allowlist 198.51.100.20 --foreign-internal-name FOREIGN-EXIT-01 --foreign-display-name "Latvia WARP" --foreign-country-code LV --foreign-domain foreign-01.example.com --foreign-ipv4 203.0.113.10 --ru-internal-name RU-EDGE-IP-01 --ru-display-name "RU Cascade IP" --ru-ipv4 192.0.2.31 --ru-reality-strategy remote_dest --ru-reality-target ya.ru:443 --ru-reality-server-name ya.ru --foreign-tariff-name "Базовый" --ru-tariff-name "Темные списки"
python3 scripts/templar_node.py generate ru-edge /opt/templar/configs/foreign-exit.yml --output-dir /tmp/templar-configs --updated-foreign-config /tmp/templar-configs/foreign-exit-with-ru2.yml --internal-name RU-EDGE-IP-02 --display-name "RU Cascade IP 2" --ipv4 192.0.2.32 --reality-strategy remote_dest --reality-target ya.ru:443 --tariff-name "Темные списки"
python3 scripts/templar_node.py wizard cascade-direct --output-dir /tmp/templar-configs
python3 scripts/templar_node.py pre-bootstrap ops/templar-node/examples/ru-warp.yml --adapter local --env-dir /tmp/templar-fake-env --secrets-dir /tmp/templar-secrets --state-dir /tmp/templar-state
python3 scripts/templar_node.py pre-bootstrap ops/templar-node/examples/ru-edge.yml --adapter http --api-key-ref secrets/remnawave-api-key --auth-type bearer --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes
python3 scripts/templar_node.py bootstrap ops/templar-node/examples/ru-warp.yml --adapter local --env-dir /tmp/templar-fake-env --secrets-dir /tmp/templar-secrets --state-dir /tmp/templar-state --render-dir /tmp/templar-render
python3 scripts/templar_node.py bootstrap ops/templar-node/examples/ru-warp.yml --adapter ssh --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes --render-dir /var/lib/templar-onboarding/render --root-password-ref secrets/ssh-root-password-ru-test
python3 scripts/templar_node.py post-bootstrap ops/templar-node/examples/ru-warp.yml --adapter local --env-dir /tmp/templar-fake-env --state-dir /tmp/templar-state
python3 scripts/templar_node.py post-bootstrap ops/templar-node/examples/ru-edge.yml --adapter http --api-key-ref secrets/remnawave-api-key --auth-type bearer --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes
python3 scripts/templar_node.py post-bootstrap ops/templar-node/examples/ru-warp.yml --adapter db --state-dir /var/lib/templar-onboarding/nodes
```

Короткие live-команды для обычного использования после установки алиасов:

```bash
add_kaskaddir     # каскад + иностранная напрямую; оба публичных входа без локального сайта, через remote_dest/xHTTP
add_direct_site   # РФ напрямую + WARP с доменом и decoy site
add_direct        # РФ напрямую + WARP без локального сайта, через REALITY remote_dest
add_routes        # выбрать RU-edge и добавить домены/IP в правила маршрутизации
add_inbound       # добавить еще один RU-edge к существующему foreign-exit, без RU decoy site
change_sni        # поменять remote_dest/SNI для ноды с IP-входом и сразу применить в RemnaWave
delete_node       # выбрать подключение и полностью удалить его хвосты
```

Эти команды сами спрашивают IP, root-пароли, названия в приложении, домены/поддомены и тарифы. По умолчанию быстрые команды выбирают пресет `Все три`: `Базовый`, `Темные списки` и `Триал`; при запуске можно выбрать любой другой пресет, ручные названия или slug.

Основные операторские команды:

```bash
# 1. Каскад + иностранная напрямую: сначала FOREIGN-EXIT, потом RU-EDGE.
python3 scripts/templar_node.py operator cascade-direct /opt/templar/configs/foreign-exit.yml /opt/templar/configs/ru-edge.yml --adapter live --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes --render-dir /var/lib/templar-onboarding/render --api-key-ref secrets/remnawave-api-key --auth-type bearer --foreign-root-password-ref secrets/ssh-root-password-lv-test --ru-root-password-ref secrets/ssh-root-password-ru-test
python3 scripts/templar_node.py operator ru-edge-add /tmp/templar-configs/foreign-exit-with-ru2.yml /tmp/templar-configs/ru-edge-ip-02.yml --adapter live --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes --render-dir /var/lib/templar-onboarding/render --api-key-ref secrets/remnawave-api-key --auth-type bearer --foreign-root-password-ref secrets/ssh-root-password-lv-test --ru-root-password-ref secrets/ssh-root-password-ru2-test

# 2. РФ напрямую с купленным доменом и decoy-сайтом.
python3 scripts/templar_node.py operator ru-direct /opt/templar/configs/ru-warp.yml --adapter live --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes --render-dir /var/lib/templar-onboarding/render --api-key-ref secrets/remnawave-api-key --auth-type bearer --root-password-ref secrets/ssh-root-password-ru-test

# 3. Добавить домен/IP в прямую маршрутизацию из РФ для каскадной ноды.
python3 scripts/templar_node.py operator routing-add /opt/templar/configs/ru-edge.yml --routes-file /opt/templar/routes/ru-direct.yml --domain gosuslugi.ru --comment "direct from RU" --apply --adapter live --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes --api-key-ref secrets/remnawave-api-key --auth-type bearer

# 4. РФ напрямую без купленного node-domain: REALITY remote_dest, например ya.ru.
python3 scripts/templar_node.py operator ru-direct-remote /opt/templar/configs/ru-warp-remote.yml --adapter live --secrets-dir /opt/templar/secrets --state-dir /var/lib/templar-onboarding/nodes --render-dir /var/lib/templar-onboarding/render --api-key-ref secrets/remnawave-api-key --auth-type bearer --root-password-ref secrets/ssh-root-password-ru-test
```

У `operator` есть безопасный `--adapter local`: он прогоняет те же сценарии в fake RemnaWave/Bedolaga окружении без реальных API, SSH и серверов. Для локальной проверки добавь `--env-dir /tmp/templar-fake-env`.

`simulate` прогоняет весь порядок `Layer 2a -> Layer 1 -> Layer 2b` локально: создает fake RemnaWave Node/Host/Squads, fake Bedolaga тарифы, checkpoint state и опционально rendered artifacts. Команду можно запускать повторно: control-plane ресурсы будут переиспользованы, а не продублированы. Если передать несколько YAML, они выполняются в одном fake окружении; так удобно проверять пару `foreign-exit + ru-edge` для каскада и прямого иностранного подключения.

`generate cascade-direct` пишет два конфига за один проход: `foreign-exit` для прямого иностранного подключения и `ru-edge` для каскада. CLI defaults для обеих публичных нод: `remote_dest` + xHTTP, Host address равен публичному IPv4 сервера, локальный Caddy decoy не используется. Если нужен старый доменный режим, передай `--foreign-reality-strategy local_decoy_site` и/или `--ru-reality-strategy local_decoy_site` вместе с node-domain. Тарифы можно передать общие через `--tariff-name/--tariff-slug` или раздельные через `--foreign-tariff-name` и `--ru-tariff-name`.

`generate ru-edge <foreign-exit.yml>` добавляет еще одну российскую cascade edge к уже существующему foreign-exit. Команда наследует foreign transit endpoint/port, service user и REALITY credential refs, генерирует только новый RU-edge YAML и по `--updated-foreign-config` пишет копию foreign YAML с новым RU IPv4 в `transit.allow_from`. Если foreign-exit работает через `remote_dest`, RU-edge получает публичный IP foreign-сервера как transit endpoint и его REALITY serverNames, а не служебный `*.node.invalid` домен. Эту обновленную foreign-конфигурацию нужно применить на иностранной ноде, чтобы открыть `10443/tcp` для второго RU сервера, затем онбордить новый RU-edge через `operator ru-edge-add`.

`generate ru-warp` по умолчанию использует `remote_dest`: Host address в подписке становится публичным IPv4 сервера, локальный сайт не поднимается, REALITY маскируется через `--reality-target ya.ru:443` и `--reality-server-name ya.ru`. Старый режим `local_decoy_site` остается доступен явно и требует купленный домен, локальный Caddy decoy site и публичный сертификат.

`wizard` делает то же самое через интерактивные вопросы в терминале. Сейчас он спрашивает только публичные параметры: IP, домены, названия подключения, тарифы и refs. Root-пароли, API keys и приватные секреты в YAML не пишутся.

`secret-set` записывает один `secrets/...` ref локально с правами `0600`. По умолчанию команда спрашивает значение через скрытый prompt. Для automation можно использовать `--stdin` или `--from-file`; значение секрета не печатается в stdout.

`remnawave-check` делает только read-only запросы к панели: system metadata, stats, nodes, internal squads и external squads. API key читается из secret store по `--api-key-ref`; команда не создает и не меняет объекты в RemnaWave.

`remnawave-keygen` вызывает официальный endpoint `/api/keygen`, получает RemnaWave Node `SECRET_KEY` и пишет его в `config.remnanode.secret_key_ref`. Значение не печатается в stdout. Для твоей панели сейчас нужен `--auth-type bearer`.

`pre-bootstrap --adapter http` делает настоящий Layer 2a в RemnaWave: рендерит full Xray config profile, создает или обновляет Config Profile, создает или обновляет Node, получает/переиспользует RemnaWave Node `SECRET_KEY` и записывает UUID в state. Команда идемпотентна по имени profile/node и падает, если находит дубли.

`cloudflare-check` делает только read-only lookup зон Cloudflare и DNS record count. Токен читается из secret store по `--api-token-ref`; команда не создает и не меняет DNS records.

`dns-upsert` создает или обновляет DNS-only A/AAAA записи Cloudflare для `config.domain -> public_ipv4/public_ipv6`. Команда специально отказывается от `--proxied`, потому что VPN node-domain должен быть gray-cloud/DNS-only.

`rotate-domain` по умолчанию остается prepare-слоем: берет новый домен из `domain_rotation.spare_domains`, пишет новый YAML, может создать Cloudflare DNS-only A/AAAA записи, обновить локальный REALITY secret `serverNames` с backup-файлом и записывает `pending.domain_rotation` в state. С `--switch --confirm-switch` команда сразу выполняет switch: bootstrap с rotated config для сертификата/артефактов, затем RemnaWave Host/Profile update и Bedolaga resync. В state сохраняется `pending.domain_rotation.rollback` с old/new domain, config paths, DNS records и backup REALITY secret.

`delete` ничего не удаляет без `--yes`: dry-run показывает, какие RemnaWave Host/Squads/Node/Profile, Bedolaga tariff/subscription/server refs и локальные хвосты будут затронуты. Если `CONFIG` не передан, команда показывает список YAML из `--config-dir` или стандартных каталогов и просит выбрать сервер. Для полного удаления используй `--full --yes`: включатся RemnaWave HTTP cleanup, Bedolaga cleanup через dockerized `psql`, DNS, state/render/secrets/routes/monitor/config cleanup, transit service user, remote VPS cleanup и отключение monitor timer, если checks больше не осталось. Точечно можно отключить части через `--skip-dns-cleanup`, `--skip-ssh-cleanup` или `--bedolaga-adapter none`.

`ru-edge-check` запускает проверки foreign-exit из уже настроенной RU-edge ноды по SSH admin key. Это не полноценная проверка доступности самой RU-edge из разных российских сетей, но позволяет отличать общий foreign-exit outage от вероятной блокировки domain/IP на пути из РФ к foreign-exit.

`synthetic-vpn-check` запускает настоящий smoke test клиентского пути: поднимает временный Xray config с SOCKS inbound, берет тестовый outbound JSON из `--client-config`, делает HTTP probe через SOCKS и, если WARP ожидается, проверяет `warp=on/plus` в Cloudflare trace. Для `ru-warp` WARP check включается автоматически, его можно отключить `--no-expect-warp`.

`warp-register` регистрирует Cloudflare WARP consumer device, получает WireGuard config, вычисляет `reserved` из base64 `client_id` и пишет один JSON secret в `warp.registration_ref`. Повторный запуск переиспользует существующий secret; `--overwrite` использовать только для осознанной ротации WARP registration. В live `pre-bootstrap --adapter http` эта проверка запускается автоматически, ее можно отключить через `--no-auto-warp-register`.

`route-add` по умолчанию только обновляет локальный YAML-файл route overrides. `route-apply`, `route-add --apply` и `operator routing-add --apply` читают этот YAML, добавляют домены/IP в direct-правило Xray для RU-edge и пушат обновленный Config Profile через RemnaWave HTTP adapter; для восстановления profile UUID берется из `--state-dir` или из `xray.config_profile_uuid`.

`pre-bootstrap --adapter local` выполняет безопасный Layer 2a без живого API: создает fake RemnaWave config profile/Node, пишет RemnaWave Node `SECRET_KEY` в локальный secret store и отмечает checkpoint `remnawave_node_registered`.

`bootstrap --adapter local` выполняет безопасную локальную часть Layer 1: проверяет, что `SECRET_KEY` уже лежит в secret store, рендерит серверный bundle в `--render-dir/<internal_name>` и отмечает Layer 1 checkpoints до `health_ok`.

`bootstrap --adapter ssh` выполняет первый live Layer 1 на чистом VPS: читает root password из `--root-password-ref`, заходит по SSH через `sshpass -f`, создает admin user/key, ставит базовые пакеты, Docker, Caddy, UFW, копирует RemnaWave Node artifacts, статический `/opt/node-site/public` и запускает контейнеры. Перед отключением password/root login он проверяет вход admin-ключом. Требуются refs `secrets/ssh-admin-public-key` и `secrets/ssh-admin-private-key`; пароль не передается в argv и не печатается в stdout.

`post-bootstrap --adapter local` выполняет безопасный Layer 2b: проверяет fake node online, создает fake Host/Internal Squad/External Squad, service user для transit, profile update, привязку тарифов и resync subscriptions. Повторный запуск переиспользует существующие объекты.

`post-bootstrap --adapter http` делает настоящий RemnaWave Layer 2b и затем Bedolaga DB attach: проверяет что Node online, создает или обновляет Host, Internal Squad, External Squad и transit service user, затем привязывает internal squad к тарифам в локальной БД. Для live-режима используй тарифы по `attach_to_tariff_names`.

`post-bootstrap --adapter db` работает только после того, как RemnaWave UUID уже записаны в state (`host_uuid`, `internal_squad_uuid`, `external_squad_uuid`). Он добавляет `internal_squad_uuid` в `Tariff.allowed_squads`, создает или обновляет локальный `ServerSquad`, ставит `external_squad_uuid` только если у тарифа поле пустое, и синхронизирует активные подписки. Для live-режима используй тарифы по `attach_to_tariff_names`; slug-полей в текущей Bedolaga DB нет.

Полная локальная цепочка до аренды серверов:

```bash
python3 scripts/templar_node.py wizard cascade-direct --output-dir /tmp/templar-configs
python3 scripts/templar_node.py pre-bootstrap /tmp/templar-configs/foreign-exit-01.yml --adapter local --env-dir /tmp/templar-fake-env --secrets-dir /tmp/templar-secrets --state-dir /tmp/templar-state
python3 scripts/templar_node.py bootstrap /tmp/templar-configs/foreign-exit-01.yml --adapter local --env-dir /tmp/templar-fake-env --secrets-dir /tmp/templar-secrets --state-dir /tmp/templar-state --render-dir /tmp/templar-render
python3 scripts/templar_node.py post-bootstrap /tmp/templar-configs/foreign-exit-01.yml --adapter local --env-dir /tmp/templar-fake-env --state-dir /tmp/templar-state
```

Следующий слой перед первым полным тестом каскада: настоящий прогон на тестовых RU/LV VPS в порядке `pre-bootstrap -> bootstrap -> post-bootstrap`.
