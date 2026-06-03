"""Load and validate Templar node YAML configs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.templar_node.schemas import NodeConfig


class NodeConfigLoadError(ValueError):
    """Raised when a node YAML config cannot be loaded or validated."""


def load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise NodeConfigLoadError(f'cannot read config {config_path}: {exc}') from exc
    except yaml.YAMLError as exc:
        raise NodeConfigLoadError(f'cannot parse YAML {config_path}: {exc}') from exc

    if raw is None:
        raise NodeConfigLoadError(f'config {config_path} is empty')
    if not isinstance(raw, dict):
        raise NodeConfigLoadError(f'config {config_path} must be a YAML mapping')
    return raw


def load_node_config(path: str | Path) -> NodeConfig:
    raw = load_yaml_mapping(path)
    try:
        return NodeConfig.model_validate(raw)
    except ValidationError as exc:
        message = _format_validation_error(exc)
        raise NodeConfigLoadError(f'config validation failed for {path}:\n{message}') from exc


def _format_validation_error(exc: ValidationError) -> str:
    lines: list[str] = []
    for error in exc.errors():
        location = '.'.join(str(item) for item in error['loc']) or '<root>'
        lines.append(f'- {location}: {error["msg"]}')
    return '\n'.join(lines)
