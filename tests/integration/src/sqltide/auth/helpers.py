from concurrent.futures import Future, ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

from sqltide._native import ControlStore
from sqltide.auth.exceptions import AccountConflictError
from sqltide.auth.models import UserAccount


def concurrently_revoke_admin_roles(
    *, store: ControlStore, user_ids: tuple[UUID, UUID]
) -> tuple[str, str]:
    barrier: Barrier = Barrier(2)

    def revoke(user_id: UUID) -> str:
        barrier.wait(timeout=10)
        try:
            store.revoke_role(user_id=user_id, role_name="admin", actor_user_id=None)
        except AccountConflictError:
            return "protected"
        return "revoked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures: tuple[Future[str], Future[str]] = (
            executor.submit(revoke, user_ids[0]),
            executor.submit(revoke, user_ids[1]),
        )
        return futures[0].result(timeout=15), futures[1].result(timeout=15)


def concurrently_provision_proxy_identity(
    *, store: ControlStore, username: str
) -> tuple[UserAccount, UserAccount]:
    barrier: Barrier = Barrier(2)

    def provision() -> UserAccount:
        barrier.wait(timeout=10)
        return store.provision_proxy_user(
            subject=username,
            username=username,
            display_name=None,
            email=None,
            default_role="viewer",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures: tuple[Future[UserAccount], Future[UserAccount]] = (
            executor.submit(provision),
            executor.submit(provision),
        )
        return futures[0].result(timeout=15), futures[1].result(timeout=15)
