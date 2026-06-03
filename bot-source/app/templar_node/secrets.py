"""Local filesystem secret storage adapter for Templar node onboarding."""

from __future__ import annotations

import json
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.templar_node.schemas import SECRET_REF_PREFIX


class SecretStoreError(ValueError):
    """Raised when a secret ref is unsafe or cannot be resolved."""


@dataclass(frozen=True)
class SecretRefCheck:
    ref: str
    path: Path | None
    exists: bool
    readable: bool
    secure_permissions: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.exists and self.readable and self.secure_permissions


@dataclass(frozen=True)
class SecretCheckSummary:
    checks: tuple[SecretRefCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def missing_refs(self) -> list[str]:
        return [check.ref for check in self.checks if not check.exists]

    @property
    def insecure_refs(self) -> list[str]:
        return [check.ref for check in self.checks if check.exists and not check.secure_permissions]

    def to_lines(self) -> list[str]:
        lines = [f'Secret refs checked: {len(self.checks)}']
        for check in self.checks:
            if check.ok:
                lines.append(f'OK {check.ref}')
                continue
            status_parts: list[str] = []
            if not check.exists:
                status_parts.append('missing')
            elif not check.readable:
                status_parts.append('not readable')
            if not check.secure_permissions:
                status_parts.append('insecure permissions')
            if check.warnings:
                status_parts.extend(check.warnings)
            lines.append(f'FAIL {check.ref}: {", ".join(status_parts)}')
        return lines


class LocalSecretStore:
    """Resolve `secrets/...` refs against a local root directory.

    The adapter intentionally does not support absolute refs or `..` path
    traversal. A secret ref maps to one raw file value or one typed JSON object.
    """

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir).expanduser().resolve()

    def path_for_ref(self, ref: str) -> Path:
        relative = self._relative_path(ref)
        path = (self.root_dir / relative).resolve()
        if not path.is_relative_to(self.root_dir):
            raise SecretStoreError(f'secret ref escapes root: {ref}')
        return path

    def read_text(self, ref: str) -> str:
        path = self.path_for_ref(ref)
        try:
            return path.read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise SecretStoreError(f'cannot read secret {ref}: {exc}') from exc

    def read_json(self, ref: str) -> dict[str, Any]:
        raw = self.read_text(ref)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretStoreError(f'secret {ref} is not valid JSON: {exc}') from exc
        if not isinstance(value, dict):
            raise SecretStoreError(f'secret {ref} JSON value must be an object')
        return value

    def write_text(self, ref: str, value: str, *, overwrite: bool = False) -> Path:
        path = self.path_for_ref(ref)
        if path.exists() and not overwrite:
            raise SecretStoreError(f'secret {ref} already exists')
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f'.{path.name}.{uuid.uuid4().hex}.tmp'
        try:
            tmp_path.write_text(value.strip() + '\n', encoding='utf-8')
            tmp_path.chmod(0o600)
            tmp_path.replace(path)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SecretStoreError(f'cannot write secret {ref}: {exc}') from exc
        return path

    def delete_ref(self, ref: str) -> bool:
        path = self.path_for_ref(ref)
        if not path.exists():
            return False
        if not path.is_file():
            raise SecretStoreError(f'secret ref is not a file: {ref}')
        try:
            path.unlink()
        except OSError as exc:
            raise SecretStoreError(f'cannot delete secret {ref}: {exc}') from exc
        return True

    def check_refs(self, refs: list[str]) -> SecretCheckSummary:
        checks = tuple(self.check_ref(ref) for ref in sorted(set(refs)))
        return SecretCheckSummary(checks=checks)

    def check_ref(self, ref: str) -> SecretRefCheck:
        try:
            path = self.path_for_ref(ref)
        except SecretStoreError as exc:
            return SecretRefCheck(
                ref=ref,
                path=None,
                exists=False,
                readable=False,
                secure_permissions=False,
                warnings=(str(exc),),
            )

        exists = path.is_file()
        readable = False
        secure_permissions = False
        warnings: list[str] = []
        if exists:
            try:
                with path.open('r', encoding='utf-8'):
                    readable = True
            except OSError as exc:
                warnings.append(str(exc))
            secure_permissions = self._has_secure_permissions(path)
        return SecretRefCheck(
            ref=ref,
            path=path,
            exists=exists,
            readable=readable,
            secure_permissions=secure_permissions,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _relative_path(ref: str) -> Path:
        normalized = ref.strip()
        if not normalized.startswith(SECRET_REF_PREFIX):
            raise SecretStoreError(f'secret ref must start with {SECRET_REF_PREFIX!r}')
        relative = normalized.removeprefix(SECRET_REF_PREFIX)
        # Inspect raw segments BEFORE Path() normalizes away '.' segments.
        raw_segments = relative.split('/')
        if (
            not relative
            or any(segment in {'', '.', '..'} for segment in raw_segments)
            or '\\' in relative
            or '\0' in relative
        ):
            raise SecretStoreError(f'unsafe secret ref: {ref}')
        path = Path(relative)
        if path.is_absolute() or not path.parts:
            raise SecretStoreError(f'unsafe secret ref: {ref}')
        return path

    @staticmethod
    def _has_secure_permissions(path: Path) -> bool:
        mode = stat.S_IMODE(path.stat().st_mode)
        return mode & 0o077 == 0
