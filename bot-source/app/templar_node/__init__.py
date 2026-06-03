"""Templar node onboarding CLI primitives."""

from app.templar_node.config_builder import generate_cascade_direct, generate_extra_ru_edge, generate_ru_warp
from app.templar_node.layer1 import run_layer1_local_bootstrap
from app.templar_node.layer2a import run_layer2a_pre_bootstrap
from app.templar_node.layer2b import run_layer2b_post_bootstrap
from app.templar_node.loader import load_node_config
from app.templar_node.planner import build_plan
from app.templar_node.remnawave import LocalRemnaWaveAdapter
from app.templar_node.render import render_bundle
from app.templar_node.routes import RouteOverrideStore
from app.templar_node.schemas import NodeConfig
from app.templar_node.secrets import LocalSecretStore
from app.templar_node.simulation import FakeEnvironmentStore, simulate_onboarding
from app.templar_node.state import NodeStateStore


__all__ = [
    'FakeEnvironmentStore',
    'LocalRemnaWaveAdapter',
    'LocalSecretStore',
    'NodeConfig',
    'NodeStateStore',
    'RouteOverrideStore',
    'build_plan',
    'generate_cascade_direct',
    'generate_extra_ru_edge',
    'generate_ru_warp',
    'load_node_config',
    'render_bundle',
    'run_layer1_local_bootstrap',
    'run_layer2a_pre_bootstrap',
    'run_layer2b_post_bootstrap',
    'simulate_onboarding',
]
