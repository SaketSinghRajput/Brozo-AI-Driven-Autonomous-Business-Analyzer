"""Add a user to a workspace with a role (L-2; the membership API is P2).

    python -m app.security.members add --email analyst@example.com --workspace-id ws_... \
        --role viewer|analyst|admin

A new user's password is read like bootstrap's (APP_MEMBER_PASSWORD, else a no-echo prompt,
else one line from stdin), never from argv. An existing user (email matched case-insensitively)
only gets the membership and keeps their password; no password is read. Every addition is
audited on the system chain (``membership.add``: user id, workspace id, role, whether the user
was created). Output contains ids only.
"""

import argparse
import sys
from collections.abc import Callable

from sqlalchemy.exc import IntegrityError

from app.core.config import load_config
from app.core.container import Container, build_container
from app.core.scope import SystemScope
from app.security.audit import AuditLog
from app.security.bootstrap import read_password
from app.security.passwords import check_new_password
from app.security.repository import AuthRepository
from app.tools.permissions import Role

MEMBERS = SystemScope("system:members")


def add_member(
    container: Container,
    email: str,
    workspace_id: str,
    role: Role,
    password: Callable[[], str],
) -> tuple[str, bool]:
    """Returns (user_id, user_created). ``password`` is called only for a new user."""
    with container.uow(MEMBERS) as s:
        repo = AuthRepository(s)
        if not repo.workspace_exists(MEMBERS, workspace_id):
            raise ValueError("workspace not found")
        user = repo.find_user(MEMBERS, email)
        if user is None:
            secret = password()
            check_new_password(secret)
            user_id = container.ids.new("usr")
            repo.create_user(MEMBERS, user_id, email, container.auth.hasher.hash(secret))
        else:
            user_id = user.id
            if any(ws == workspace_id for ws, _ in repo.memberships(MEMBERS, user_id)):
                raise ValueError("the user is already a member of that workspace")
        repo.add_membership(MEMBERS, user_id, workspace_id, role)
        AuditLog(s, container.auth.audit_keys, container.clock, container.ids).append(
            MEMBERS,
            action="membership.add",
            resource=user_id,
            result="success",
            metadata={
                "workspace_id": workspace_id,
                "role": role.value,
                "user_created": user is None,
            },
        )
    return user_id, user is None


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    parser = argparse.ArgumentParser(description="Manage workspace memberships.")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="add a user (created if new) to a workspace")
    add.add_argument("--email", required=True)
    add.add_argument("--workspace-id", required=True)
    add.add_argument("--role", required=True, choices=[r.value for r in Role])
    args = parser.parse_args(argv)
    container = build_container(load_config())
    try:
        user_id, created = add_member(
            container,
            args.email,
            args.workspace_id,
            Role(args.role),
            lambda: read_password("APP_MEMBER_PASSWORD", "Password for the new user: "),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except IntegrityError:
        # A concurrent run added the same user or membership; the driver DETAIL carries the
        # email, so it is never printed.
        print("error: the user or membership already exists", file=sys.stderr)
        return 1
    finally:
        container.engine.dispose()
    what = "created user" if created else "existing user"
    print(f"added {what} {user_id} to {args.workspace_id} as {args.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
