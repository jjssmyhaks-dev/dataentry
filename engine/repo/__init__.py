"""Tenant-scoped persistence.

Every function here takes a cursor that is already scoped to one tenant's schema
(``Database.tenant``) so a query cannot reach another tenant's data by accident.
"""

from engine.repo import posting, review, tenant  # noqa: F401

__all__ = ["tenant", "review", "posting"]
