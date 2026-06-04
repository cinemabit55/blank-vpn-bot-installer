# Node Shortcuts

Install on a control-plane host:

```bash
cd /opt/bedolaga
scripts/install_templar_node_aliases.sh
```

The installer creates executable wrappers in `/usr/local/bin`. They use `/opt/bedolaga` by default and can be overridden with `TEMPLAR_NODE_REPO_DIR`, `TEMPLAR_NODE_PYTHON`, or `TEMPLAR_NODE_CLI`.

| Command | Underlying script command | What it does |
| --- | --- | --- |
| `add_kaskaddir` | `quick cascade-direct` | Interactive full provisioning of foreign direct + RU cascade without local decoy sites. Prompts for IPs, root passwords, display names, tariffs and remote REALITY SNI, then generates TCP/REALITY remote-dest YAML and runs onboarding. Tariff default is all three pools. |
| `add_cascade` / `add_cascade_direct` | `quick cascade-direct` | English aliases for `add_kaskaddir`. |
| `add_direct_site` | `quick direct-site` | Interactive full provisioning of one RU direct WARP node with a bought domain and decoy site. |
| `add_direct` | `quick direct-remote` | Interactive full provisioning of one RU direct WARP node without a local decoy site, using REALITY `remote_dest` defaults tested with `ya.ru:443`. |
| `add_inbound` | `quick extra-ru-edge` | Selects an existing foreign exit and adds one more RU cascade edge to it without a RU decoy site. Remote-dest foreign exits are linked by server IP with their REALITY SNI. |
| `add_routes` | `quick routing-add` | Selects a RU cascade edge, asks for domains/IPs, writes `routes.yml`, and applies the profile update by default. |
| `delete_node` | `delete --full --yes` | Shows server selection when needed, then fully deletes the selected node everywhere. |
| `preview_delete_node` | `delete --full` | Shows the same full deletion plan without applying it. |
| `list_nodes` | `delete --list` | Lists discovered node configs. |
| `validate_node` | `validate` | Validates a node YAML config. |
| `plan_node` | `plan` | Prints the onboarding plan. |
| `render_node` | `render` | Renders bootstrap artifacts. |
| `generate_node` | `generate` | Generates node YAML configs. |
| `wizard_node` | `wizard` | Runs the interactive config generator. |
| `simulate_node` | `simulate` | Runs local fake onboarding. |
| `check_node_secrets` | `secrets-check` | Checks secret refs. |
| `set_node_secret` | `secret-set` | Writes one secret ref. |
| `check_remnawave` | `remnawave-check` | Runs RemnaWave API contract checks. |
| `remnawave_keygen` | `remnawave-keygen` | Fetches/writes a RemnaWave Node SECRET_KEY. |
| `check_cloudflare` | `cloudflare-check` | Checks Cloudflare zones. |
| `upsert_node_dns` | `dns-upsert` | Creates or updates node DNS records. |
| `rotate_node_domain` | `rotate-domain` | Prepares or switches node domain rotation. |
| `change_sni` / `change_cine` | `quick sni-change` | Selects a remote-dest node, changes REALITY target/SNI, updates the REALITY secret and applies RemnaWave profile/host by default. |
| `check_node_availability` | `availability-check` | Runs main/control-plane availability checks. |
| `check_foreign_from_ru` | `ru-edge-check` | Checks a foreign-exit from a RU-edge SSH vantage point. |
| `check_vpn_client` | `synthetic-vpn-check` | Runs synthetic VPN client/WARP checks. |
| `register_node_warp` | `warp-register` | Registers WARP and writes registration secret. |
| `show_node_state` | `state-show` | Shows saved onboarding state. |
| `init_node_state` | `state-init` | Initializes onboarding state. |
| `mark_node_state` | `state-mark` | Marks a checkpoint. |
| `cleanup_node_orphans` | `cleanup-orphans` | Shows or clears orphaned state records. |
| `add_node_route` | `route-add` | Adds route overrides. |
| `apply_node_routes` | `route-apply` | Applies route overrides to RemnaWave profile. |
| `show_node_routes` | `route-show` | Shows route overrides. |
| `run_node_operator` | `operator` | Runs high-level operator scenarios. |
| `prebootstrap_node` | `pre-bootstrap` | Runs pre-bootstrap phase. |
| `bootstrap_node` | `bootstrap` | Runs bootstrap phase. |
| `postbootstrap_node` | `post-bootstrap` | Runs post-bootstrap phase. |
| `node_commands` | built-in wrapper help | Prints the shortcut list. |
