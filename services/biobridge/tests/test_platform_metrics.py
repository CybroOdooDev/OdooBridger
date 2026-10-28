"""GET /platform/v1/metrics: the numbers the core's staff dashboard shows for BioBridge."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.models import Tenant
from app.models.attendance import EmployeeMapping, PunchRecord, SyncRun
from app.models.connection import Device, DeviceSource
from app.models.scheduler import LEASE_ID, SchedulerLease
from tests.test_platform_mode import ACCOUNT, client, put, state, token  # noqa: F401 — fixture

EMPTY = hashlib.sha256(b"").hexdigest()


def get(client, typ="metrics", digest=EMPTY):  # noqa: F811
    bearer = token(typ, {"sub": "odoobridger-core", "body_sha256": digest})
    return client.get("/platform/v1/metrics", headers={"Authorization": f"Bearer {bearer}"})


def _seed(client):  # noqa: F811
    put(client, state())
    db = client.Session()
    tenant = db.query(Tenant).filter(Tenant.platform_account_id == ACCOUNT).one()
    other = Tenant(name="Standalone", slug="standalone")
    db.add(other)
    db.flush()
    ok = DeviceSource(tenant_id=tenant.id, name="Head office", provider="zkteco", base_url="10.0.0.5",
                      username="", status="connected")
    bad = DeviceSource(tenant_id=tenant.id, name="Warehouse", provider="zkteco", base_url="10.0.0.6",
                       username="", status="failed", status_message="Terminal did not answer.\nmore")
    off = DeviceSource(tenant_id=tenant.id, name="Old", provider="zkteco", base_url="10.0.0.7",
                       username="", status="failed", is_active=False)
    alien = DeviceSource(tenant_id=other.id, name="Theirs", provider="zkteco", base_url="10.0.0.8",
                         username="", status="failed")
    db.add_all([ok, bad, off, alien])
    db.flush()
    now = datetime.now(timezone.utc)
    db.add_all([
        SyncRun(tenant_id=tenant.id, status="success", started_at=now - timedelta(hours=3),
                finished_at=now - timedelta(hours=3)),
        SyncRun(tenant_id=tenant.id, status="partial", started_at=now - timedelta(hours=2)),
        SyncRun(tenant_id=tenant.id, status="failed", started_at=now - timedelta(hours=1)),
    ])
    for i, st in enumerate(["pending", "error", "synced", "unmapped"]):
        db.add(PunchRecord(tenant_id=tenant.id, source_id=ok.id, external_id=f"x{i}", emp_code="1",
                           punch_time_utc=datetime(2026, 10, 1, 9, i), process_state=st))
    db.add(PunchRecord(tenant_id=other.id, source_id=alien.id, external_id="y", emp_code="1",
                       punch_time_utc=datetime(2026, 10, 1, 9), process_state="pending"))
    db.merge(SchedulerLease(id=LEASE_ID, mode="inprocess", last_tick_at=datetime.utcnow()))
    db.commit()
    ids = bad.id
    db.close()
    return ids


def test_metrics_need_a_metrics_token(client):  # noqa: F811
    assert client.get("/platform/v1/metrics").status_code == 401
    assert get(client, typ="provision").status_code == 401
    assert get(client, digest=hashlib.sha256(b"x").hexdigest()).status_code == 403


def test_metrics_count_platform_tenants_only(client):  # noqa: F811
    bad_id = _seed(client)
    r = get(client)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["service"] == "biometric" and m["version"]
    assert m["integrations"] == {"active": 2, "failed": 1}
    assert m["jobs"]["pending"] == 2 and m["jobs"]["oldest_pending_at"]
    assert m["last_success_at"] is not None
    assert m["workers"]["scheduler"]["ok"] is True
    assert m["status"] == "degraded"          # 1 of 2 failing
    [account] = m["accounts"]
    assert account["account_id"] == ACCOUNT and (account["active"], account["failed"]) == (2, 1)
    [failure] = account["failures"]
    assert failure["key"] == "devices" and failure["issue"] == "Sync failing"
    assert failure["detail"] == "1 of 2 devices not syncing"
    # Nothing customer-specific about connections leaves BioBridge.
    body = json.dumps(m)
    for secret in ("Warehouse", "Head office", "Terminal did not answer", "10.0.0.", bad_id):
        assert secret not in body, secret
    # The first bad run after the last good one, not the latest.
    since = datetime.fromisoformat(failure["since"])
    assert timedelta(hours=1, minutes=50) < datetime.now(timezone.utc) - since < timedelta(hours=2, minutes=10)


def _tenant(client):  # noqa: F811
    db = client.Session()
    tenant = db.query(Tenant).filter(Tenant.platform_account_id == ACCOUNT).one()
    db.expunge(tenant)
    db.close()
    return tenant


def test_dashboard_shape_usage_and_attention(client):  # noqa: F811
    _seed(client)
    tenant = _tenant(client)
    db = client.Session()
    t = db.get(Tenant, tenant.id)
    t.limit_max_devices = 2
    t.limit_max_employees = 0          # 0 = unlimited
    src = db.query(DeviceSource).filter(DeviceSource.tenant_id == t.id, DeviceSource.name == "Head office").one()
    db.add_all([Device(tenant_id=t.id, source_id=src.id, serial_number=f"SN{i}") for i in range(2)])
    db.add_all([EmployeeMapping(tenant_id=t.id, emp_code=str(i), status="mapped") for i in range(3)]
               + [EmployeeMapping(tenant_id=t.id, emp_code="x", status="unmapped")])
    synced = db.query(PunchRecord).filter(PunchRecord.process_state == "synced").one()
    synced.attempts = 1   # touch: synced today
    db.commit()
    db.close()
    d = get(client).json()["dashboard"]
    tiles = {x["key"]: x["value"] for x in d["tiles"]}
    assert tiles == {"customers": 1, "devices": 2, "employees": 3, "punches_30d": 1, "pending": 2}
    assert [c["key"] for c in d["columns"]] == ["devices", "employees", "sync_interval", "last_sync_at", "health"]
    assert len(d["series"]["points"]) == 30 and d["series"]["points"][-1]["value"] == 1
    row = d["accounts"][ACCOUNT]
    assert row["devices"] == {"used": 2, "limit": 2} and row["employees"] == {"used": 3, "limit": None}
    assert row["health"] == "failing" and row["sync_interval"] and row["last_sync_at"]
    titles = {(a["title"], a["severity"]) for a in d["attention"]}
    assert ("Sync failing", "high") in titles and ("At device limit", "medium") in titles
    limit = next(a for a in d["attention"] if a["title"] == "At device limit")
    assert limit["detail"].startswith("2 of 2 devices") and limit["since"] is None
    assert "Warehouse" not in json.dumps(d)


def test_dashboard_health_values(client):  # noqa: F811
    put(client, state())
    tenant = _tenant(client)
    d = get(client).json()["dashboard"]
    assert d["accounts"][ACCOUNT]["health"] == "not_started"
    db = client.Session()
    db.add(DeviceSource(tenant_id=tenant.id, name="A", provider="zkteco", base_url="10.0.0.5",
                        username="", status="connected"))
    now = datetime.now(timezone.utc)
    run = SyncRun(tenant_id=tenant.id, status="success", started_at=now - timedelta(minutes=2),
                  finished_at=now - timedelta(minutes=2))
    db.add(run)
    db.commit()
    d = get(client).json()["dashboard"]
    assert d["accounts"][ACCOUNT]["health"] == "healthy" and d["attention"] == []
    run = db.get(SyncRun, run.id)
    run.started_at = run.finished_at = now - timedelta(days=1)
    db.commit()
    db.close()
    d = get(client).json()["dashboard"]
    assert d["accounts"][ACCOUNT]["health"] == "delayed"
    [delay] = d["attention"]
    assert delay["title"] == "Sync delayed" and delay["detail"].startswith("Last sync 1 day ago")


def test_metrics_with_nothing_yet(client):  # noqa: F811
    m = get(client).json()
    assert m["integrations"] == {"active": 0, "failed": 0}
    assert m["jobs"] == {"pending": 0, "oldest_pending_at": None}
    assert m["accounts"] == [] and m["workers"]["scheduler"]["ok"] is False
    assert m["status"] == "degraded"          # nothing has ticked


def test_metrics_404_outside_platform_mode(client, monkeypatch):  # noqa: F811
    from app.core.config import settings

    monkeypatch.setattr(settings, "platform_mode", False)
    assert get(client).status_code == 404
