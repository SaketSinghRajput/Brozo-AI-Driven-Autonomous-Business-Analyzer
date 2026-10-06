"""Create the first workspace and its admin user (demo / fresh install).

    python -m app.security.bootstrap --email admin@example.com --workspace "Demo"

The password is never a command-line argument (argv shows up in shell history and process
lists): it is read from APP_BOOTSTRAP_PASSWORD, else prompted for without echo, else read as
one line from stdin. Output contains ids only.
"""

import argparse
import getpass
import os
import sys

from sqlalchemy.exc import IntegrityError

from app.core.config import load_config
from app.core.container import Container, build_container
from app.core.scope import SystemScope
from app.security.audit import AuditLog
from app.security.passwords import check_new_password
from app.security.repository import AuthRepository
from app.tools.permissions import Role

BOOTSTRAP = SystemScope("system:bootstrap")


def bootstrap_admin(
    container: Container, email: str, password: str, workspace_name: str
) -> tuple[str, str]:
    """Returns (workspace_id, user_id). Fails if the email is already registered."""
    check_new_password(password)
    workspace_id = container.ids.new("ws")
    user_id = container.ids.new("usr")
    password_hash = container.auth.hasher.hash(password)
    with container.uow(BOOTSTRAP) as s:
        repo = AuthRepository(s)
        if repo.find_user(BOOTSTRAP, email) is not None:
            raise ValueError("a user with that email already exists")
        repo.create_workspace(BOOTSTRAP, workspace_id, workspace_name)
        repo.create_user(BOOTSTRAP, user_id, email, password_hash)
        repo.add_membership(BOOTSTRAP, user_id, workspace_id, Role.ADMIN)
        AuditLog(s, container.auth.audit_keys, container.clock, container.ids).append(
            BOOTSTRAP,
            action="user.bootstrap",
            resource=user_id,
            result="success",
            metadata={"workspace_id": workspace_id, "role": Role.ADMIN.value},
        )
    return workspace_id, user_id


def read_password(env_var: str = "APP_BOOTSTRAP_PASSWORD", prompt: str = "Admin password: ") -> str:
    """From ``env_var``, else a no-echo prompt (asked twice), else one line of stdin."""
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env
    if sys.stdin.isatty():
        first = getpass.getpass(prompt)
        if getpass.getpass("Repeat password: ") != first:
            raise SystemExit("passwords do not match")
        return first
    return sys.stdin.readline().rstrip("\r\n")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    parser = argparse.ArgumentParser(description="Create the first workspace and admin user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--workspace", default="Default workspace")
    args = parser.parse_args(argv)
    container = build_container(load_config())
    try:
        workspace_id, user_id = bootstrap_admin(
            container, args.email, read_password(), args.workspace
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except IntegrityError:
        # A concurrent run registered the same email. The driver message carries the email in
        # its DETAIL line, so it is never printed.
        print("error: a user with that email already exists", file=sys.stderr)
        return 1
    finally:
        container.engine.dispose()
    print(f"created workspace {workspace_id} and admin user {user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
