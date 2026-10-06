"""A throw-away database on the test cluster, for tests that must not touch the shared one
(migration downgrades with data, external connector sources)."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, make_url, text

from .conftest import ADMIN_URL


@contextmanager
def scratch_database(prefix: str) -> Iterator[str]:
    """Yields the owner URL (password included) of a new database; drops it afterwards."""
    assert ADMIN_URL
    base = make_url(ADMIN_URL)
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
