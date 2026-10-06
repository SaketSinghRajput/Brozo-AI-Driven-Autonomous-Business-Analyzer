"""Repository base class (TASK-DB-011).

Every public repository method must take `scope` as its first argument, so no repository can
offer an unscoped query. The rule is enforced when the subclass is defined, not at call time.
"""

import inspect
from typing import Any

from sqlalchemy.orm import Session


class Repository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, member in vars(cls).items():
            if name.startswith("_") or not inspect.isfunction(member):
                continue
            params = list(inspect.signature(member).parameters)
            if len(params) < 2 or params[1] != "scope":
                raise TypeError(
                    f"{cls.__name__}.{name} must take `scope` as its first argument "
                    "(WorkspaceScope or SystemScope)"
                )
