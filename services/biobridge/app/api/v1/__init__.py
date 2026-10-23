from fastapi import APIRouter, Depends

from app.api.deps import standalone_only
from app.api.v1 import admin, auth, billing, connections, public, sync

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(connections.router)
api_router.include_router(sync.router)
# Platform mode: billing is the OdooBridger core's.
api_router.include_router(billing.router, dependencies=[Depends(standalone_only)])
# The marketing website's calls — no account needed. See app/api/v1/public.py.
api_router.include_router(public.router, dependencies=[Depends(standalone_only)])
# Not tenant-scoped — see app/api/v1/admin.py. Behind get_platform_admin.
api_router.include_router(admin.router)

__all__ = ["api_router"]
