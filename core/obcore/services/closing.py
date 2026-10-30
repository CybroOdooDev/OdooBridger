"""Deleting an account for good: stop billing, have every service delete its
data, keep a closure record, then remove the account."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from obcore.models import Account, AccountClosure, BillingEvent
from obcore.services import billing, provisioning
from obcore.services.accounts import audit, owner_of

log = logging.getLogger(__name__)


class ClosingError(RuntimeError):
    pass


def delete_account(db: Session, account: Account, *, actor: str, reason: str | None = None,
                   force: bool = False) -> list[str]:
    """Returns warnings. Raises ClosingError when a service could not delete the
    account's data and ``force`` is off — nothing is deleted then."""
    failures: list[str] = []
    for row in account.services:
        try:
            provisioning.remove(db, row.service, account.id)
        except provisioning.ProvisionError as exc:
            failures.append(f"{row.service.name}: {exc}")
    if failures and not force:
        raise ClosingError("Not deleted — these services could not delete the account's data: "
                           + "; ".join(failures))
    lost = billing.paid_mrr_cents(account)
    billing.cancel_now(account)
    if lost:
        db.add(BillingEvent(account_id=account.id, account_name=account.name, kind="churn",
                            mrr_before_cents=account.recorded_mrr_cents if account.recorded_mrr_cents is not None else lost,
                            mrr_after_cents=0))
    owner = owner_of(account)
    db.add(AccountClosure(account_id=account.id, account_name=account.name,
                          owner_email=owner.email if owner else None,
                          services=[r.service.slug for r in account.services],
                          closed_by=actor, reason=reason, account_created_at=account.created_at))
    audit(db, actor, "account.delete", account_id=account.id, detail=reason)
    db.delete(account)
    db.flush()
    return failures
