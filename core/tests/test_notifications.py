"""The customer's bell: one line per service, "You have alerts in your <service>" with a count."""

from __future__ import annotations

from obcore.services import notifications
from tests.conftest import signup


def _bell(client, headers):
    r = client.get("/api/v1/portal/notifications", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_service_with_alerts_gets_one_line(client, catalog, service_app):
    headers = signup(client)
    service_app.alerts = {"count": 3, "severity": "warn"}
    bell = _bell(client, headers)
    assert bell["count"] == 3 and bell["worst"] == "warn"
    assert bell["items"] == [{
        "service": "BioBridge", "slug": "biometric", "severity": "warn", "count": 3,
        "text": "You have alerts in your BioBridge"}]


def test_the_line_names_the_service_and_the_count_travels_separately(client, catalog, service_app):
    headers = signup(client)
    service_app.alerts = {"count": 1, "severity": "bad"}
    bell = _bell(client, headers)
    assert bell["items"][0]["text"] == "You have alerts in your BioBridge"
    assert bell["items"][0]["count"] == 1
    assert bell["worst"] == "bad"


def test_nothing_to_report_is_an_empty_bell(client, catalog, service_app):
    headers = signup(client)
    assert _bell(client, headers) == {"count": 0, "worst": None, "items": []}


def test_the_answer_is_data_not_trusted(client, catalog, service_app):
    headers = signup(client)
    for odd in ({"count": "7", "severity": "bad"}, {"count": True}, {"count": -2}, {"count": None}, {}, []):
        service_app.alerts = odd
        notifications.forget()
        assert _bell(client, headers)["count"] == 0, odd
    service_app.alerts = {"count": 10 ** 9, "severity": "<b>x</b>"}
    notifications.forget()
    item = _bell(client, headers)["items"][0]
    assert item["count"] == 999 and item["severity"] == "warn"


def test_a_service_that_is_down_or_has_no_feed_adds_nothing(client, catalog, service_app):
    headers = signup(client)
    service_app.alerts = {"count": 2, "severity": "bad"}
    service_app.down = True
    assert _bell(client, headers)["count"] == 0
    service_app.down = False
    service_app.alerts = None
    notifications.forget()
    assert _bell(client, headers)["count"] == 0


def test_answers_are_reused_for_a_few_seconds(client, catalog, service_app):
    headers = signup(client)
    service_app.alerts = {"count": 1, "severity": "warn"}
    assert _bell(client, headers)["count"] == 1
    service_app.alerts = {"count": 0, "severity": None}   # fixed a moment ago
    assert _bell(client, headers)["count"] == 1           # still the cached answer
    notifications.forget()
    assert _bell(client, headers)["count"] == 0           # and gone once it is re-read


def test_the_bell_is_for_signed_in_customers_only(client, catalog):
    assert client.get("/api/v1/portal/notifications").status_code in (401, 403)
