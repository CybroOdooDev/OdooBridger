"""The count OdooBridger's bell reads: the same alerts BioBridge's own bell shows."""

from __future__ import annotations

from sqlalchemy import select

from app.models import ConnectionStatus, EmployeeMapping, MappingStatus, OdooConnection
from app.services.alerts import tenant_alerts
from app.services.platform_alerts import summary


def test_nothing_wrong_counts_zero(db, tenant):
    out = summary(db, tenant)
    assert out["count"] == len(tenant_alerts(db, tenant))


def test_the_count_and_severity_match_the_services_own_bell(db, tenant):
    before = summary(db, tenant)["count"]
    for code in ("1001", "1002", "1003"):
        db.add(EmployeeMapping(tenant_id=tenant.id, emp_code=code, status=MappingStatus.unmapped.value))
    odoo = db.scalars(select(OdooConnection).where(OdooConnection.tenant_id == tenant.id)).first()
    odoo.status = ConnectionStatus.failed.value
    db.commit()

    out = summary(db, tenant)
    assert out["count"] == before + 2                    # unmatched badges, and Odoo failing
    assert out["count"] == len(tenant_alerts(db, tenant))
    assert out["severity"] == "bad"                      # worst first
    assert set(out) == {"count", "severity"}             # only the number leaves the service
