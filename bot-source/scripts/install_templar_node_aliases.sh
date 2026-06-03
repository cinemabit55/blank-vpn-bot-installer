#!/usr/bin/env sh
# Install VPN node shortcut commands into /usr/local/bin by default.
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="${1:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)}"
TARGET_DIR="${TEMPLAR_NODE_ALIAS_DIR:-/usr/local/bin}"
DISPATCHER="$TARGET_DIR/templar_node_alias"

COMMANDS="node_commands templar_node_commands add_kaskaddir add_cascade add_cascade_direct add_direct_site add_ru_direct_site add_direct add_direct_remote add_ru_direct add_ru_direct_remote add_inbound add_ru_edge add_cascade_inbound add_routes add_routing_rules delete_node delete_nodes preview_delete_node delete_node_preview list_nodes validate_node plan_node render_node generate_node wizard_node simulate_node check_node_secrets set_node_secret check_remnawave remnawave_keygen check_cloudflare upsert_node_dns rotate_node_domain change_sni change_cine change_sine check_node_availability check_foreign_from_ru check_vpn_client register_node_warp show_node_state init_node_state mark_node_state cleanup_node_orphans add_node_route apply_node_routes show_node_routes run_node_operator prebootstrap_node bootstrap_node postbootstrap_node"

install -d -m 0755 "$TARGET_DIR"
install -m 0755 "$REPO_DIR/scripts/templar_node_alias" "$DISPATCHER"
for name in $COMMANDS; do
  ln -sf "$DISPATCHER" "$TARGET_DIR/$name"
done

echo "Installed VPN node shortcuts to $TARGET_DIR"
echo "Run: node_commands"
