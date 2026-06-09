"""Dry-run plan generation for Templar node onboarding."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.templar_node.schemas import NodeConfig, NodeRole, RealityStrategy, TransitMode, WarpMode


@dataclass(frozen=True)
class PlanStep:
    layer: str
    action: str
    details: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class NodePlan:
    internal_name: str
    role: str
    display_name: str
    domain: str
    steps: tuple[PlanStep, ...]

    def to_lines(self) -> list[str]:
        lines = [
            f'Node: {self.internal_name}',
            f'Role: {self.role}',
            f'Display: {self.display_name}',
            f'Domain: {self.domain}',
            '',
            'Plan:',
        ]
        for index, step in enumerate(self.steps, start=1):
            lines.append(f'{index}. [{step.layer}] {step.action}')
            lines.extend(f'   - {detail}' for detail in step.details)
        return lines

    def to_dict(self) -> dict:
        return {
            'internal_name': self.internal_name,
            'role': self.role,
            'display_name': self.display_name,
            'domain': self.domain,
            'steps': [
                {
                    'layer': step.layer,
                    'action': step.action,
                    'details': list(step.details),
                }
                for step in self.steps
            ],
        }


def build_plan(config: NodeConfig) -> NodePlan:
    steps: list[PlanStep] = []
    steps.extend(_layer_2a_steps(config))
    steps.extend(_layer_1_steps(config))
    steps.extend(_layer_2b_steps(config))
    return NodePlan(
        internal_name=config.display.internal_name,
        role=config.role.value,
        display_name=config.display.name,
        domain=config.domain,
        steps=tuple(steps),
    )


def _layer_2a_steps(config: NodeConfig) -> list[PlanStep]:
    details = [
        f'node key: {config.display.internal_name}',
        f'country: {config.country_code}',
        f'node API port: {config.remnanode.node_port}',
        f'secret ref: {config.remnanode.secret_key_ref}',
        f'public REALITY ref: {config.reality.credentials_ref or "<missing>"}',
    ]
    if config.xray.config_profile_uuid:
        details.append(f'config profile: {config.xray.config_profile_uuid}')
    else:
        details.append('config profile: resolve/create and persist discovered UUID')

    return [
        PlanStep(
            layer='Layer 2a',
            action='Resolve/create RemnaWave Node and store SECRET_KEY',
            details=tuple(details),
        ),
    ]


def _layer_1_steps(config: NodeConfig) -> list[PlanStep]:
    steps = [
        PlanStep(
            layer='Layer 1',
            action='Run read-only preflight checks',
            details=(
                f'target IPv4: {config.public_ipv4}',
                f'SSH admin user after hardening: {config.ssh.admin_user}',
                f'allowed SSH sources: {", ".join(config.ssh.admin_allowlist)}',
            ),
        ),
        PlanStep(
            layer='Layer 1',
            action='Install base packages, Docker Engine and compose plugin',
            details=('packages: curl, ca-certificates, gnupg, ufw, jq, git, rsync, logrotate',),
        ),
        PlanStep(
            layer='Layer 1',
            action='Apply SSH hardening and UFW rules with delayed rollback',
            details=(
                f'SSH port: {config.ssh.port}',
                f'admin user: {config.ssh.admin_user}',
                f'public HTTPS port: {config.reality.public_port}',
            ),
        ),
        PlanStep(
            layer='Layer 1',
            action='Write RemnaWave Node compose and local state',
            details=(
                'network_mode: host',
                f'NODE_PORT: {config.remnanode.node_port}',
                f'XTLS API port: {config.xray.xtls_api_port}',
            ),
        ),
    ]

    if config.reality.strategy == RealityStrategy.LOCAL_DECOY_SITE:
        steps.insert(
            3,
            PlanStep(
                layer='Layer 1',
                action='Install Caddy decoy site and public certificate material',
                details=(
                    f'site domain: {config.domain}',
                    f'template: {config.site.template}',
                    f'certificate mode: {config.site.certificate_mode.value}',
                ),
            ),
        )
    else:
        steps.insert(
            3,
            PlanStep(
                layer='Layer 1',
                action='Skip local Caddy decoy for remote_dest REALITY',
                details=(
                    f'connect address: {config.effective_host_address()}',
                    f'remote dest: {config.reality.target}',
                    f'SNI/serverNames: {", ".join(config.effective_reality_server_names())}',
                ),
            ),
        )

    if config.warp.mode == WarpMode.XRAY_NATIVE:
        steps.append(
            PlanStep(
                layer='Layer 1',
                action='Prepare Xray-native WARP outbound prerequisites',
                details=(
                    f'outbound tag: {config.warp.outbound_tag}',
                    f'registration ref: {config.warp.registration_ref}',
                ),
            ),
        )

    if config.transit.mode == TransitMode.VLESS_REALITY and config.transit.inbound_tag and config.transit.listen_port:
        steps.append(
            PlanStep(
                layer='Layer 1',
                action='Open transit inbound only for RU-edge allow-list',
                details=(
                    f'listen port: {config.transit.listen_port}',
                    f'allow from: {", ".join(config.transit.allow_from or [])}',
                ),
            ),
        )

    steps.append(
        PlanStep(
            layer='Layer 1',
            action='Start services and run health checks',
            details=(
                'RemnaWave Node online',
                'decoy HTTPS reachable' if config.reality.strategy == RealityStrategy.LOCAL_DECOY_SITE else 'remote_dest REALITY probe reachable',
                'unexpected public ports absent',
            ),
        ),
    )
    return steps


def _layer_2b_steps(config: NodeConfig) -> list[PlanStep]:
    steps = [
        PlanStep(
            layer='Layer 2b',
            action='Verify RemnaWave Node is online',
            details=(f'node key: {config.display.internal_name}',),
        ),
        PlanStep(
            layer='Layer 2b',
            action='Create/update Host and Internal/External Squads',
            details=(
                f'host address: {config.effective_host_address()}',
                f'host remark: {config.effective_host_remark()}',
                f'internal squad: {config.bedolaga.internal_squad_name}',
                f'external squad: {config.bedolaga.external_squad_name}',
            ),
        ),
    ]

    if config.transit.mode == TransitMode.VLESS_REALITY:
        if config.transit.inbound_tag and config.transit.listen_port:
            action = 'Create/update transit inbound and bridge service user'
            details = (
                f'inbound tag: {config.transit.inbound_tag}',
                f'service user: {config.transit.service_user}',
                f'credential ref: {config.transit.service_user_credential_ref}',
                f'reality ref: {config.transit.reality_credentials_ref}',
            )
        else:
            action = 'Attach selective transit outbound to foreign exit'
            details = (
                f'foreign exit: {config.transit.foreign_exit_domain}:{config.transit.foreign_exit_port}',
                f'outbound tag: {config.transit.outbound_tag}',
                f'selective domains: {len(config.transit.selective_domains)}',
                f'selective IPs: {len(config.transit.selective_ips)}',
                f'credential ref: {config.transit.service_user_credential_ref}',
                f'reality ref: {config.transit.reality_credentials_ref}',
            )
        steps.append(PlanStep(layer='Layer 2b', action=action, details=details))

    steps.append(
        PlanStep(
            layer='Layer 2b',
            action='Generate and push Xray profile snippets',
            details=tuple(_xray_snippet_details(config)),
        ),
    )
    steps.append(
        PlanStep(
            layer='Layer 2b',
            action='Attach squads to selected Bedolaga tariffs and resync subscriptions',
            details=tuple(_tariff_details(config)),
        ),
    )
    return steps


def _xray_snippet_details(config: NodeConfig) -> list[str]:
    details = [
        f'REALITY strategy: {config.reality.strategy.value}',
        f'REALITY serverNames: {", ".join(config.effective_reality_server_names())}',
    ]
    if config.warp.mode == WarpMode.XRAY_NATIVE:
        details.append(f'WARP outbound: {config.warp.outbound_tag}')
    if config.role == NodeRole.RU_EDGE and config.routing is not None:
        details.extend(
            [
                f'RU route: {config.routing.ru_route.value}',
                f'default route: {config.routing.default_route.value}',
                'foreign DNS via transit: true',
            ],
        )
    return details


def _tariff_details(config: NodeConfig) -> list[str]:
    details = [f'cabinet display: {config.effective_cabinet_name()}']
    if config.bedolaga.attach_to_tariff_slugs:
        details.append(f'tariff slugs: {", ".join(config.bedolaga.attach_to_tariff_slugs)}')
    if config.bedolaga.attach_to_tariff_names:
        details.append(f'tariff names: {", ".join(config.bedolaga.attach_to_tariff_names)}')
    if config.bedolaga.trial_eligible:
        details.append('free trial pool: enabled')
    return details
