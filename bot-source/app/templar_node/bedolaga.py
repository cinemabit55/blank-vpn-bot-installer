"""Local Bedolaga adapter contracts for post-bootstrap onboarding."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from app.templar_node.fake_env import FakeEnvironmentStore
from app.templar_node.schemas import NodeConfig


T = TypeVar('T')


class BedolagaAdapterError(RuntimeError):
    """Raised when a Bedolaga adapter cannot complete tariff/sync writes."""


@dataclass(frozen=True)
class TariffAttachRecord:
    key: str
    status: str


@dataclass(frozen=True)
class BedolagaDetachAction:
    kind: str
    key: str
    status: str
    detail: str = ''


@dataclass(frozen=True)
class BedolagaDecommissionResult:
    actions: tuple[BedolagaDetachAction, ...]

    def to_lines(self) -> list[str]:
        lines = ['Bedolaga cleanup:']
        if not self.actions:
            lines.append('- nothing matched')
            return lines
        lines.extend(
            f'- {action.kind} {action.key}: {action.status}' + (f' ({action.detail})' if action.detail else '')
            for action in self.actions
        )
        return lines


@dataclass(frozen=True)
class BedolagaSyncResult:
    tariff_records: tuple[TariffAttachRecord, ...]
    resync_keys: tuple[str, ...]


class BedolagaAdapter(Protocol):
    def attach_squads(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str,
        external_squad_uuid: str,
    ) -> BedolagaSyncResult:
        """Attach squads to requested tariffs and resync active subscriptions."""


class LocalBedolagaAdapter:
    """Local fake Bedolaga adapter backed by FakeEnvironmentStore."""

    def __init__(self, env_store: FakeEnvironmentStore):
        self.env_store = env_store

    def attach_squads(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str,
        external_squad_uuid: str,
    ) -> BedolagaSyncResult:
        environment = self.env_store.load()
        records: list[TariffAttachRecord] = []
        resync_keys: list[str] = []
        server_squads = environment['bedolaga'].setdefault('server_squads', {})
        server_squads[internal_squad_uuid] = {
            'squad_uuid': internal_squad_uuid,
            'display_name': config.effective_cabinet_name(),
            'original_name': config.bedolaga.internal_squad_name,
            'country_code': config.country_code,
            'is_available': config.host.visibility,
            'is_trial_eligible': config.bedolaga.trial_eligible,
        }
        tariff_refs = [
            *(('slug', value) for value in config.bedolaga.attach_to_tariff_slugs),
            *(('name', value) for value in config.bedolaga.attach_to_tariff_names),
        ]
        for ref_type, ref_value in tariff_refs:
            key = f'{ref_type}:{ref_value}'
            tariff = environment['bedolaga']['tariffs'].setdefault(
                key,
                {
                    'ref_type': ref_type,
                    'ref_value': ref_value,
                    'allowed_internal_squad_uuids': [],
                    'external_squad_uuids': [],
                },
            )
            changed = False
            if internal_squad_uuid not in tariff['allowed_internal_squad_uuids']:
                tariff['allowed_internal_squad_uuids'].append(internal_squad_uuid)
                changed = True
            if external_squad_uuid not in tariff['external_squad_uuids']:
                tariff['external_squad_uuids'].append(external_squad_uuid)
                changed = True
            status = 'attached' if changed else 'existing'
            records.append(TariffAttachRecord(key=key, status=status))
            if changed:
                resync_key = _append_unique_resync(environment, key, config.display.internal_name)
                resync_keys.append(resync_key)
        if config.bedolaga.trial_eligible:
            resync_key = _append_unique_resync(environment, 'trial:eligible', config.display.internal_name)
            resync_keys.append(resync_key)
        self.env_store.save(environment)
        return BedolagaSyncResult(tariff_records=tuple(records), resync_keys=tuple(resync_keys))

    def detach_squads(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str | None,
        external_squad_uuid: str | None,
        dry_run: bool,
    ) -> BedolagaDecommissionResult:
        environment = self.env_store.load()
        actions: list[BedolagaDetachAction] = []
        for key, tariff in environment['bedolaga']['tariffs'].items():
            allowed = [str(item) for item in tariff.get('allowed_internal_squad_uuids') or [] if item]
            external = [str(item) for item in tariff.get('external_squad_uuids') or [] if item]
            changed = False
            if internal_squad_uuid and internal_squad_uuid in allowed:
                actions.append(_detach_action('tariff_allowed_squad', key, 'remove', dry_run=dry_run, detail=internal_squad_uuid))
                if not dry_run:
                    tariff['allowed_internal_squad_uuids'] = [item for item in allowed if item != internal_squad_uuid]
                changed = True
            if external_squad_uuid and external_squad_uuid in external:
                actions.append(_detach_action('tariff_external_squad', key, 'remove', dry_run=dry_run, detail=external_squad_uuid))
                if not dry_run:
                    tariff['external_squad_uuids'] = [item for item in external if item != external_squad_uuid]
                changed = True
            if changed:
                _remove_fake_resyncs(environment, key, config.display.internal_name, actions, dry_run=dry_run)
        if not actions:
            actions.append(BedolagaDetachAction('bedolaga', config.display.internal_name, 'missing'))
        if not dry_run:
            self.env_store.save(environment)
        return BedolagaDecommissionResult(actions=tuple(actions))


def _run_database_coro(coro) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_with_database_cleanup(coro))
    coro.close()
    raise BedolagaAdapterError('DatabaseBedolagaAdapter cannot run inside an existing event loop')


async def _with_database_cleanup(coro) -> T:
    try:
        return await coro
    finally:
        try:
            from app.database.database import close_db

            await close_db()
        except Exception:
            pass


class DatabaseBedolagaAdapter:
    """Live Bedolaga adapter using the bot database on the control-plane host.

    The adapter is intentionally conservative: it appends the new internal squad
    UUID to tariff.allowed_squads and only sets tariff.external_squad_uuid when
    the field is empty. Existing external squad assignments are left untouched.
    """

    def __init__(self, *, resync_subscriptions: bool = True):
        self.resync_subscriptions = resync_subscriptions

    def attach_squads(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str,
        external_squad_uuid: str,
    ) -> BedolagaSyncResult:
        return _run_database_coro(
            self._attach_squads_async(
                config,
                internal_squad_uuid=internal_squad_uuid,
                external_squad_uuid=external_squad_uuid,
            ),
        )

    async def _attach_squads_async(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str,
        external_squad_uuid: str,
    ) -> BedolagaSyncResult:
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import joinedload

            from app.config import settings
            from app.database.crud.server_squad import (
                create_server_squad,
                get_effective_trial_squad_uuids,
                get_server_squad_by_uuid,
            )
            from app.database.database import AsyncSessionLocal
            from app.database.models import Subscription, SubscriptionStatus, Tariff, User
            from app.services.remnawave_service import RemnaWaveService
        except Exception as exc:  # pragma: no cover - depends on app runtime env
            raise BedolagaAdapterError(f'cannot import Bedolaga database runtime: {exc}') from exc

        records: list[TariffAttachRecord] = []
        resync_keys: list[str] = []
        tariff_refs = _tariff_refs(config)

        async with AsyncSessionLocal() as db:
            server = await get_server_squad_by_uuid(db, internal_squad_uuid)
            if server is None:
                await create_server_squad(
                    db=db,
                    squad_uuid=internal_squad_uuid,
                    display_name=config.effective_cabinet_name(),
                    original_name=config.bedolaga.internal_squad_name,
                    country_code=config.country_code,
                    price_kopeks=0,
                    description=f'Created by templar-node for {config.display.internal_name}',
                    is_available=config.host.visibility,
                    is_trial_eligible=config.bedolaga.trial_eligible,
                )
            else:
                server.display_name = config.effective_cabinet_name()
                server.original_name = config.bedolaga.internal_squad_name
                server.country_code = config.country_code
                server.is_available = config.host.visibility
                server.is_trial_eligible = config.bedolaga.trial_eligible
                server.updated_at = datetime.now(UTC)

            touched_tariff_ids: list[int] = []
            for ref_type, ref_value in tariff_refs:
                tariff = await _find_tariff(db, Tariff, ref_type, ref_value)
                if tariff is None:
                    raise BedolagaAdapterError(f'tariff {ref_type}:{ref_value!r} not found in Bedolaga DB')

                changed = False
                allowed = [str(item) for item in (tariff.allowed_squads or []) if item]
                if internal_squad_uuid not in allowed:
                    allowed.append(internal_squad_uuid)
                    tariff.allowed_squads = allowed
                    changed = True

                external_status = 'external-kept'
                if not tariff.external_squad_uuid:
                    tariff.external_squad_uuid = external_squad_uuid
                    external_status = 'external-set'
                    changed = True
                elif tariff.external_squad_uuid == external_squad_uuid:
                    external_status = 'external-existing'

                if changed:
                    tariff.updated_at = datetime.now(UTC)
                    touched_tariff_ids.append(tariff.id)
                status = 'attached' if changed else 'existing'
                records.append(TariffAttachRecord(key=f'{ref_type}:{ref_value}', status=f'{status}:{external_status}'))

            trial_tariff_for_sync = None
            if config.bedolaga.trial_eligible:
                trial_tariff_for_sync = await _find_trial_tariff(db, Tariff, settings)
                if trial_tariff_for_sync is not None and (trial_tariff_for_sync.allowed_squads or []):
                    changed = False
                    allowed = [str(item) for item in (trial_tariff_for_sync.allowed_squads or []) if item]
                    if internal_squad_uuid not in allowed:
                        allowed.append(internal_squad_uuid)
                        trial_tariff_for_sync.allowed_squads = allowed
                        changed = True

                    external_status = 'external-kept'
                    if not trial_tariff_for_sync.external_squad_uuid:
                        trial_tariff_for_sync.external_squad_uuid = external_squad_uuid
                        external_status = 'external-set'
                        changed = True
                    elif trial_tariff_for_sync.external_squad_uuid == external_squad_uuid:
                        external_status = 'external-existing'

                    if changed:
                        trial_tariff_for_sync.updated_at = datetime.now(UTC)
                        touched_tariff_ids.append(trial_tariff_for_sync.id)
                    status = 'attached' if changed else 'existing'
                    records.append(
                        TariffAttachRecord(
                            key=f'trial-tariff:{_tariff_key(trial_tariff_for_sync)}',
                            status=f'{status}:{external_status}',
                        ),
                    )

            await db.commit()

            if self.resync_subscriptions and (touched_tariff_ids or config.bedolaga.trial_eligible):
                service = RemnaWaveService()
                async with service.get_api_client() as api:
                    for tariff_id in dict.fromkeys(touched_tariff_ids):
                        tariff = await _find_tariff(db, Tariff, 'id', str(tariff_id))
                        if tariff is None:
                            continue
                        result = await db.execute(
                            select(Subscription)
                            .join(User, Subscription.user_id == User.id)
                            .options(joinedload(Subscription.user))
                            .where(
                                Subscription.tariff_id == tariff_id,
                                Subscription.status.in_(
                                    [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value],
                                ),
                            ),
                        )
                        subscriptions = list(result.unique().scalars().all())
                        updated = 0
                        for sub in subscriptions:
                            remnawave_uuid = (
                                getattr(sub, 'remnawave_uuid', None)
                                if settings.is_multi_tariff_enabled()
                                else (sub.user.remnawave_uuid if sub.user else None)
                            )
                            if not remnawave_uuid:
                                continue
                            update_kwargs = {
                                'uuid': remnawave_uuid,
                                'active_internal_squads': list(tariff.allowed_squads or []),
                            }
                            if tariff.external_squad_uuid is not None:
                                update_kwargs['external_squad_uuid'] = tariff.external_squad_uuid
                            await api.update_user(**update_kwargs)
                            sub.connected_squads = list(tariff.allowed_squads or [])
                            sub.updated_at = datetime.now(UTC)
                            updated += 1
                        await db.commit()
                        resync_keys.append(f'tariff:{tariff_id}:subscriptions:{updated}')

                    if config.bedolaga.trial_eligible:
                        trial_allowed_squads = (
                            trial_tariff_for_sync.allowed_squads if trial_tariff_for_sync is not None else None
                        )
                        trial_squads = await get_effective_trial_squad_uuids(db, trial_allowed_squads)
                        result = await db.execute(
                            select(Subscription)
                            .join(User, Subscription.user_id == User.id)
                            .options(joinedload(Subscription.user), joinedload(Subscription.tariff))
                            .where(
                                Subscription.is_trial.is_(True),
                                Subscription.status.in_(
                                    [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value],
                                ),
                            ),
                        )
                        subscriptions = list(result.unique().scalars().all())
                        db_updated = 0
                        panel_updated = 0
                        for sub in subscriptions:
                            current_squads = [str(item) for item in (sub.connected_squads or []) if item]
                            if current_squads != trial_squads:
                                sub.connected_squads = list(trial_squads)
                                sub.updated_at = datetime.now(UTC)
                                db_updated += 1

                            remnawave_uuid = (
                                getattr(sub, 'remnawave_uuid', None)
                                if settings.is_multi_tariff_enabled()
                                else (sub.user.remnawave_uuid if sub.user else None)
                            )
                            if not remnawave_uuid:
                                continue

                            update_kwargs = {
                                'uuid': remnawave_uuid,
                                'active_internal_squads': list(trial_squads),
                            }
                            external_uuid = sub.tariff.external_squad_uuid if sub.tariff else None
                            if external_uuid is not None:
                                update_kwargs['external_squad_uuid'] = external_uuid
                            await api.update_user(**update_kwargs)
                            panel_updated += 1
                        await db.commit()
                        resync_keys.append(f'trial:subscriptions:db={db_updated}:panel={panel_updated}')

        return BedolagaSyncResult(tariff_records=tuple(records), resync_keys=tuple(resync_keys))

    def detach_squads(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str | None,
        external_squad_uuid: str | None,
        dry_run: bool,
    ) -> BedolagaDecommissionResult:
        if not internal_squad_uuid and not external_squad_uuid:
            return BedolagaDecommissionResult(
                actions=(BedolagaDetachAction('bedolaga', config.display.internal_name, 'missing_state'),),
            )
        return _run_database_coro(
            self._detach_squads_async(
                config,
                internal_squad_uuid=internal_squad_uuid,
                external_squad_uuid=external_squad_uuid,
                dry_run=dry_run,
            ),
        )

    async def _detach_squads_async(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str | None,
        external_squad_uuid: str | None,
        dry_run: bool,
    ) -> BedolagaDecommissionResult:
        try:
            from sqlalchemy import String, cast, delete, or_, select
            from sqlalchemy.orm import joinedload

            from app.config import settings
            from app.database.database import AsyncSessionLocal
            from app.database.models import (
                ServerSquad,
                Subscription,
                SubscriptionServer,
                SubscriptionStatus,
                Tariff,
                User,
            )
            from app.services.remnawave_service import RemnaWaveService
        except Exception as exc:  # pragma: no cover - depends on app runtime env
            raise BedolagaAdapterError(f'cannot import Bedolaga database runtime: {exc}') from exc

        actions: list[BedolagaDetachAction] = []
        touched_tariff_ids: set[int] = set()
        external_cleared_tariff_ids: set[int] = set()

        async with AsyncSessionLocal() as db:
            tariffs_result = await db.execute(select(Tariff))
            tariffs = list(tariffs_result.scalars().all())
            for tariff in tariffs:
                changed = False
                allowed = [str(item) for item in (tariff.allowed_squads or []) if item]
                if internal_squad_uuid and internal_squad_uuid in allowed:
                    actions.append(
                        _detach_action(
                            'tariff_allowed_squad',
                            _tariff_key(tariff),
                            'remove',
                            dry_run=dry_run,
                            detail=internal_squad_uuid,
                        ),
                    )
                    changed = True
                    if not dry_run:
                        tariff.allowed_squads = [item for item in allowed if item != internal_squad_uuid]
                        tariff.updated_at = datetime.now(UTC)

                if external_squad_uuid and tariff.external_squad_uuid == external_squad_uuid:
                    actions.append(
                        _detach_action(
                            'tariff_external_squad',
                            _tariff_key(tariff),
                            'clear',
                            dry_run=dry_run,
                            detail=external_squad_uuid,
                        ),
                    )
                    changed = True
                    external_cleared_tariff_ids.add(tariff.id)
                    if not dry_run:
                        tariff.external_squad_uuid = None
                        tariff.updated_at = datetime.now(UTC)

                if changed:
                    touched_tariff_ids.add(tariff.id)

            subscription_filters = []
            if internal_squad_uuid:
                subscription_filters.append(cast(Subscription.connected_squads, String).like(f'%"{internal_squad_uuid}"%'))
            if external_cleared_tariff_ids:
                subscription_filters.append(Subscription.tariff_id.in_(external_cleared_tariff_ids))

            subscriptions = []
            if subscription_filters:
                query = (
                    select(Subscription)
                    .join(User, Subscription.user_id == User.id)
                    .options(joinedload(Subscription.user), joinedload(Subscription.tariff))
                    .where(
                        Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
                        or_(*subscription_filters),
                    )
                )
                result = await db.execute(query)
                subscriptions = list(result.unique().scalars().all())

            panel_updated = 0
            subscription_updated = 0
            if subscriptions:
                if dry_run:
                    actions.append(
                        BedolagaDetachAction(
                            'subscriptions',
                            config.display.internal_name,
                            'would_resync',
                            str(len(subscriptions)),
                        ),
                    )
                else:
                    service = RemnaWaveService()
                    async with service.get_api_client() as api:
                        for subscription in subscriptions:
                            current = [str(item) for item in (subscription.connected_squads or []) if item]
                            new_squads = [item for item in current if item != internal_squad_uuid]
                            remnawave_uuid = (
                                getattr(subscription, 'remnawave_uuid', None)
                                if settings.is_multi_tariff_enabled()
                                else (subscription.user.remnawave_uuid if subscription.user else None)
                            )
                            external_uuid = subscription.tariff.external_squad_uuid if subscription.tariff else None
                            if remnawave_uuid:
                                await api.update_user(
                                    uuid=remnawave_uuid,
                                    active_internal_squads=new_squads,
                                    external_squad_uuid=external_uuid,
                                )
                                panel_updated += 1
                            if new_squads != current:
                                subscription.connected_squads = new_squads
                                subscription.updated_at = datetime.now(UTC)
                                subscription_updated += 1
                    actions.append(
                        BedolagaDetachAction(
                            'subscriptions',
                            config.display.internal_name,
                            'resynced',
                            f'db={subscription_updated} panel={panel_updated}',
                        ),
                    )

            if internal_squad_uuid:
                server_result = await db.execute(select(ServerSquad).where(ServerSquad.squad_uuid == internal_squad_uuid).limit(1))
                server = server_result.scalars().first()
                if server is None:
                    actions.append(BedolagaDetachAction('server_squad', internal_squad_uuid, 'missing'))
                elif dry_run:
                    actions.append(BedolagaDetachAction('server_squad', internal_squad_uuid, 'would_delete', server.display_name))
                else:
                    await db.execute(delete(SubscriptionServer).where(SubscriptionServer.server_squad_id == server.id))
                    await db.execute(delete(ServerSquad).where(ServerSquad.id == server.id))
                    actions.append(BedolagaDetachAction('server_squad', internal_squad_uuid, 'deleted', server.display_name))

            if not actions:
                actions.append(BedolagaDetachAction('bedolaga', config.display.internal_name, 'missing'))
            if dry_run:
                await db.rollback()
            else:
                await db.commit()

        return BedolagaDecommissionResult(actions=tuple(actions))


class PsqlBedolagaAdapter:
    """Live Bedolaga cleanup through dockerized psql, without Python DB deps."""

    def __init__(self, *, db_container: str = 'remnawave_bot_db'):
        self.db_container = db_container

    def detach_squads(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str | None,
        external_squad_uuid: str | None,
        dry_run: bool,
    ) -> BedolagaDecommissionResult:
        if not internal_squad_uuid and not external_squad_uuid:
            return BedolagaDecommissionResult(
                actions=(BedolagaDetachAction('bedolaga', config.display.internal_name, 'missing_state'),),
            )
        payload = self._run_cleanup_sql(
            internal_squad_uuid=internal_squad_uuid,
            external_squad_uuid=external_squad_uuid,
            dry_run=dry_run,
        )
        return BedolagaDecommissionResult(actions=_psql_payload_actions(config, payload, dry_run=dry_run))

    def _run_cleanup_sql(
        self,
        *,
        internal_squad_uuid: str | None,
        external_squad_uuid: str | None,
        dry_run: bool,
    ) -> dict[str, int]:
        docker = shutil.which('docker')
        if docker is None:
            raise BedolagaAdapterError('docker is required for --bedolaga-adapter psql')
        command = [
            docker,
            'exec',
            '-i',
            self.db_container,
            'sh',
            '-lc',
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -tA',
        ]
        try:
            completed = subprocess.run(  # noqa: S603
                command,
                input=_psql_decommission_sql(
                    internal_squad_uuid=internal_squad_uuid,
                    external_squad_uuid=external_squad_uuid,
                    dry_run=dry_run,
                ),
                text=True,
                capture_output=True,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, 'stderr', None) or getattr(exc, 'stdout', None) or str(exc)
            raise BedolagaAdapterError(f'psql Bedolaga cleanup failed: {detail}') from exc
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith('{') and stripped.endswith('}'):
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise BedolagaAdapterError(f'cannot parse psql cleanup payload: {stripped}') from exc
                return {key: int(value or 0) for key, value in payload.items()}
        raise BedolagaAdapterError(f'psql cleanup did not return JSON payload: {completed.stdout.strip()}')


class HttpBedolagaAdapter:
    """Placeholder for the real Bedolaga adapter."""

    def attach_squads(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str,
        external_squad_uuid: str,
    ) -> BedolagaSyncResult:
        raise BedolagaAdapterError('http Bedolaga adapter is not wired yet; use adapter=local')

    def detach_squads(
        self,
        config: NodeConfig,
        *,
        internal_squad_uuid: str | None,
        external_squad_uuid: str | None,
        dry_run: bool,
    ) -> BedolagaDecommissionResult:
        raise BedolagaAdapterError('http Bedolaga adapter is not wired yet; use adapter=local')


def _psql_payload_actions(config: NodeConfig, payload: dict[str, int], *, dry_run: bool) -> tuple[BedolagaDetachAction, ...]:
    actions: list[BedolagaDetachAction] = []
    tariff_count = payload.get('tariffs_updated', 0)
    subscription_count = payload.get('subscriptions_updated', 0)
    subscription_server_count = payload.get('subscription_servers_deleted', 0)
    server_count = payload.get('server_squads_deleted', 0)
    update_status = 'would_update' if dry_run else 'updated'
    delete_status = 'would_delete' if dry_run else 'deleted'
    if tariff_count:
        actions.append(BedolagaDetachAction('tariffs', config.display.internal_name, update_status, str(tariff_count)))
    if subscription_count:
        actions.append(BedolagaDetachAction('subscriptions', config.display.internal_name, update_status, str(subscription_count)))
    if subscription_server_count:
        actions.append(
            BedolagaDetachAction(
                'subscription_servers',
                config.display.internal_name,
                delete_status,
                str(subscription_server_count),
            ),
        )
    if server_count:
        actions.append(BedolagaDetachAction('server_squad', config.display.internal_name, delete_status, str(server_count)))
    if not actions:
        actions.append(BedolagaDetachAction('bedolaga', config.display.internal_name, 'missing'))
    return tuple(actions)


def _psql_decommission_sql(
    *,
    internal_squad_uuid: str | None,
    external_squad_uuid: str | None,
    dry_run: bool,
) -> str:
    internal = _sql_literal(internal_squad_uuid)
    external = _sql_literal(external_squad_uuid)
    params = f'SELECT {internal}::text AS internal_uuid, {external}::text AS external_uuid'
    tariff_match = """
        (p.internal_uuid IS NOT NULL AND t.allowed_squads::text LIKE '%' || p.internal_uuid || '%')
        OR (p.external_uuid IS NOT NULL AND t.external_squad_uuid::text = p.external_uuid)
    """
    subscription_match = "p.internal_uuid IS NOT NULL AND s.connected_squads::text LIKE '%' || p.internal_uuid || '%'"
    server_match = 'p.internal_uuid IS NOT NULL AND sv.squad_uuid::text = p.internal_uuid'
    if dry_run:
        return f"""WITH params AS ({params})
SELECT json_build_object(
  'tariffs_updated', (SELECT COUNT(*) FROM tariffs t, params p WHERE {tariff_match}),
  'subscriptions_updated', (SELECT COUNT(*) FROM subscriptions s, params p WHERE {subscription_match}),
  'subscription_servers_deleted', (
    SELECT COUNT(*)
    FROM subscription_servers ss
    JOIN server_squads sv ON ss.server_squad_id = sv.id
    CROSS JOIN params p
    WHERE {server_match}
  ),
  'server_squads_deleted', (SELECT COUNT(*) FROM server_squads sv, params p WHERE {server_match})
)::text;
"""
    return f"""BEGIN;
WITH params AS ({params}), updated_tariffs AS (
  UPDATE tariffs t
  SET
    allowed_squads = COALESCE(
      (
        SELECT json_agg(elem.value)
        FROM json_array_elements_text(COALESCE(t.allowed_squads, '[]'::json)) AS elem(value)
        WHERE (SELECT internal_uuid FROM params) IS NULL OR elem.value <> (SELECT internal_uuid FROM params)
      ),
      '[]'::json
    ),
    external_squad_uuid = CASE
      WHEN (SELECT external_uuid FROM params) IS NOT NULL
       AND t.external_squad_uuid::text = (SELECT external_uuid FROM params) THEN NULL
      ELSE t.external_squad_uuid
    END,
    updated_at = NOW()
  FROM params p
  WHERE {tariff_match}
  RETURNING t.id
), updated_subscriptions AS (
  UPDATE subscriptions s
  SET
    connected_squads = COALESCE(
      (
        SELECT json_agg(elem.value)
        FROM json_array_elements_text(COALESCE(s.connected_squads, '[]'::json)) AS elem(value)
        WHERE (SELECT internal_uuid FROM params) IS NULL OR elem.value <> (SELECT internal_uuid FROM params)
      ),
      '[]'::json
    ),
    updated_at = NOW()
  FROM params p
  WHERE {subscription_match}
  RETURNING s.id
), deleted_subscription_servers AS (
  DELETE FROM subscription_servers ss
  USING server_squads sv, params p
  WHERE ss.server_squad_id = sv.id
    AND {server_match}
  RETURNING ss.id
), deleted_server_squads AS (
  DELETE FROM server_squads sv
  USING params p
  WHERE {server_match}
  RETURNING sv.id
)
SELECT json_build_object(
  'tariffs_updated', (SELECT COUNT(*) FROM updated_tariffs),
  'subscriptions_updated', (SELECT COUNT(*) FROM updated_subscriptions),
  'subscription_servers_deleted', (SELECT COUNT(*) FROM deleted_subscription_servers),
  'server_squads_deleted', (SELECT COUNT(*) FROM deleted_server_squads)
)::text;
COMMIT;
"""


def _sql_literal(value: str | None) -> str:
    if value is None:
        return 'NULL'
    return "'" + value.replace("'", "''") + "'"


def _append_unique_resync(environment: dict, tariff_key: str, internal_name: str) -> str:
    resync_key = f'{tariff_key}:{internal_name}'
    if any(item.get('key') == resync_key for item in environment['bedolaga']['resyncs']):
        return resync_key
    environment['bedolaga']['resyncs'].append(
        {
            'key': resync_key,
            'tariff': tariff_key,
            'node': internal_name,
            'status': 'subscriptions_resynced',
        },
    )
    return resync_key


def _remove_fake_resyncs(
    environment: dict,
    tariff_key: str,
    internal_name: str,
    actions: list[BedolagaDetachAction],
    *,
    dry_run: bool,
) -> None:
    before = len(environment['bedolaga']['resyncs'])
    kept = [
        item for item in environment['bedolaga']['resyncs']
        if item.get('tariff') != tariff_key or item.get('node') != internal_name
    ]
    removed = before - len(kept)
    if removed:
        status = 'would_delete' if dry_run else 'deleted'
        actions.append(BedolagaDetachAction('fake_resync', tariff_key, status, str(removed)))
        if not dry_run:
            environment['bedolaga']['resyncs'] = kept


def _detach_action(kind: str, key: str, verb: str, *, dry_run: bool, detail: str = '') -> BedolagaDetachAction:
    status = f'would_{verb}' if dry_run else f'{verb}d'
    if verb == 'clear' and not dry_run:
        status = 'cleared'
    if verb == 'remove' and not dry_run:
        status = 'removed'
    return BedolagaDetachAction(kind=kind, key=key, status=status, detail=detail)


def _tariff_key(tariff) -> str:
    name = getattr(tariff, 'name', None) or '<unnamed>'
    return f'id:{tariff.id}:{name}'


def _tariff_refs(config: NodeConfig) -> list[tuple[str, str]]:
    return [
        *(('slug', value) for value in config.bedolaga.attach_to_tariff_slugs),
        *(('name', value) for value in config.bedolaga.attach_to_tariff_names),
    ]


async def _find_trial_tariff(db, tariff_model, settings):
    from sqlalchemy import select

    result = await db.execute(
        select(tariff_model)
        .where(tariff_model.is_trial_available.is_(True))
        .order_by(tariff_model.updated_at.desc().nullslast(), tariff_model.id.desc())
        .limit(1),
    )
    tariff = result.scalars().first()
    if tariff is not None:
        return tariff

    trial_tariff_id = settings.get_trial_tariff_id()
    if trial_tariff_id > 0:
        return await _find_tariff(db, tariff_model, 'id', str(trial_tariff_id))
    return None


async def _find_tariff(db, tariff_model, ref_type: str, ref_value: str):
    from sqlalchemy import func, select

    if ref_type == 'id':
        result = await db.execute(select(tariff_model).where(tariff_model.id == int(ref_value)).limit(1))
        return result.scalars().first()
    if ref_type == 'name':
        result = await db.execute(select(tariff_model).where(tariff_model.name == ref_value).limit(2))
        matches = result.scalars().all()
        if not matches:
            result = await db.execute(
                select(tariff_model).where(func.lower(tariff_model.name) == ref_value.lower()).limit(2),
            )
            matches = result.scalars().all()
        if len(matches) > 1:
            raise BedolagaAdapterError(f'multiple tariffs found with name {ref_value!r}')
        return matches[0] if matches else None
    if ref_type == 'slug':
        raise BedolagaAdapterError('Bedolaga DB tariffs do not have a slug field; use attach_to_tariff_names for live adapter')
    raise BedolagaAdapterError(f'unknown tariff ref type {ref_type!r}')
