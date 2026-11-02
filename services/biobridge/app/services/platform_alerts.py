"""What OdooBridger's bell shows for this service: how many alerts, how bad.

Deliberately not the alerts themselves. They are shown in full on BioBridge's
own Alerts bell, and repeating them in OdooBridger only duplicates that page;
the portal says "You have 3 alerts in your BioBridge" and opens the service.
The count is the same list the service's own bell counts, so the two agree.

Nothing is stored: it is computed from current state, so it falls to zero the
moment the causes are fixed.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Tenant
from app.services.alerts import tenant_alerts


def summary(db: Session, tenant: Tenant) -> dict:
    alerts = tenant_alerts(db, tenant)       # worst first
    return {"count": len(alerts), "severity": alerts[0]["severity"] if alerts else None}
