"""When a badge and a name disagree, a person decides.

A badge that sits on exactly one Odoo employee used to be matched on the spot.
That is right when the name on the device is that person's, and wrong when two
different people carry the same number: the badge is reused, mistyped, or from
another system, and one person's attendance lands on someone else.

So the evidence is compared before a match is made:

* **badge_only**: the badge is found on one Odoo employee, but the device calls
  the person something else. Not matched; the Odoo employee is suggested.
* **name_only**: the badge is found nowhere in Odoo (or not on that person), but
  an Odoo employee has the same name, with a different badge or none. Not
  matched, and not auto-created either (that would make a duplicate); the
  employee is suggested.

Either way the badge stays *unmatched* (its punches wait, nothing is lost),
carries the suggestion, and is listed ahead of the plain unmatched badges. One
click confirms it, or the picker chooses someone else. When there is no name to
compare (a vendor that does not report one, or a placeholder), nothing changes:
the badge matches as before.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from app.models import EmployeeMapping, MappingStatus

KIND_BADGE_ONLY = "badge_only"
KIND_NAME_ONLY = "name_only"
#: A person said "not the same": the suggestion is not made again for this badge.
KIND_DISMISSED = "dismissed"

#: Odoo fields that hold a badge number (work_email is an identity, not a badge).
BADGE_FIELDS = ("barcode", "pin", "registration_number")

_PLACEHOLDERS = {"unknown", "unknown badge", "unnamed", "no name", "n a", "na", "null", "none"}
_TITLES = {"mr", "mrs", "ms", "miss", "dr", "eng", "sir"}


def tokens(name: str | None) -> list[str]:
    """Lower-case words with accents and punctuation gone: "Sharma, Ahmed" and
    "ahmed  sharma" give the same tokens."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return [t for t in re.split(r"[^a-z0-9\u0080-￿]+", text) if t and t not in _TITLES]


def usable_name(name: str | None, code: str | None = None) -> str | None:
    """The name on the device when it says something about the person. A blank,
    a placeholder ("Unknown Badge", "Employee 1001") or the badge itself does not."""
    words = tokens(name)
    if not words:
        return None
    joined = " ".join(words)
    if joined in _PLACEHOLDERS:
        return None
    code_tokens = " ".join(tokens(code))
    if code_tokens and joined in (code_tokens, f"employee {code_tokens}"):
        return None
    if all(w.isdigit() for w in words):
        return None
    if all(len(w) == 1 for w in words):
        return None                       # initials only ("X Y"): nothing to compare
    return str(name).strip()


def _ratio(a: list[str], b: list[str]) -> float:
    return SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()


def compatible(device_name: str | None, odoo_name: str | None) -> bool:
    """Could these be the same person? Generous: used to *accept* a badge match,
    so only a clear difference sends it for review. Same words in any order, one
    name contained in the other ("Ahmed" / "Ahmed Sharma"), or a near spelling
    ("Mohammed" / "Muhammad")."""
    a, b = tokens(device_name), tokens(odoo_name)
    if not a or not b:
        return True                       # nothing to compare: do not block the match
    sa_, sb = set(a), set(b)
    if sa_ == sb or sa_ <= sb or sb <= sa_:
        return True
    return _ratio(a, b) >= 0.82


def same_name(device_name: str | None, odoo_name: str | None) -> bool:
    """Is this almost certainly the same person, by name alone? Strict: used to
    *suggest* someone for a badge that is found nowhere, so "Ahmed" does not
    suggest every Ahmed. Same words, a near spelling of a full name, or a full
    name contained in a longer one."""
    a, b = tokens(device_name), tokens(odoo_name)
    if not a or not b:
        return False
    sa_, sb = set(a), set(b)
    if sa_ == sb:
        return True
    if min(len(a), len(b)) < 2:
        return False
    if sa_ <= sb or sb <= sa_:
        return True
    return _ratio(a, b) >= 0.9


def badge_of(row: dict[str, Any]) -> str | None:
    """The badge number an Odoo employee row carries, if any."""
    for field_name in BADGE_FIELDS:
        value = row.get(field_name)
        if value:
            return str(value).strip()
    return None


def name_candidates(device_name: str, roster: list[dict[str, Any]], taken: set[int]) -> list[dict[str, Any]]:
    """Odoo employees that share the device name and are free to take the badge
    (not already matched to another badge), best spelling first, then lowest id."""
    found = [r for r in roster if r.get("id") not in taken and same_name(device_name, r.get("name"))]
    found.sort(key=lambda r: (-_ratio(tokens(device_name), tokens(r.get("name"))), r.get("id") or 0))
    return found


# --------------------------------------------------------------------------- #
# Writing the outcome on the mapping
# --------------------------------------------------------------------------- #
def clear_suggestion(mapping: EmployeeMapping) -> None:
    mapping.suggested_employee_id = None
    mapping.suggested_employee_name = None
    mapping.suggestion_kind = None


def suggest_badge_only(mapping: EmployeeMapping, code: str, device_name: str, odoo_id: int, odoo_name: str | None) -> None:
    _hold(mapping, KIND_BADGE_ONLY, odoo_id, odoo_name, device_name,
          f"Badge {code} is “{device_name}” on the device but “{odoo_name or 'an employee'}” in Odoo. "
          "Confirm they are the same person, or pick another employee.")


def suggest_name_only(mapping: EmployeeMapping, code: str, device_name: str, row: dict[str, Any], others: int) -> None:
    badge = badge_of(row)
    where = f"with badge {badge}" if badge and badge != code else "with no badge set"
    more = f" {others} other Odoo employee(s) share this name." if others else ""
    _hold(mapping, KIND_NAME_ONLY, row["id"], row.get("name"), device_name,
          f"“{device_name}” is also in Odoo, {where}, not {code}. "
          f"Confirm it is the same person, or pick another employee.{more}")


def _hold(mapping: EmployeeMapping, kind: str, odoo_id: int, odoo_name: str | None,
          device_name: str, note: str) -> None:
    mapping.status = MappingStatus.unmapped.value
    mapping.odoo_employee_id = None
    mapping.odoo_employee_name = None
    mapping.match_method = None
    mapping.source_name = device_name or mapping.source_name
    mapping.suggested_employee_id = odoo_id
    mapping.suggested_employee_name = odoo_name
    mapping.suggestion_kind = kind
    mapping.match_note = note


def dismissed(mapping: EmployeeMapping | None) -> bool:
    return mapping is not None and mapping.suggestion_kind == KIND_DISMISSED


def dismiss_suggestion(mapping: EmployeeMapping) -> None:
    """"Not the same person": forget the suggestion and do not make it again."""
    mapping.suggested_employee_id = None
    mapping.suggested_employee_name = None
    mapping.suggestion_kind = KIND_DISMISSED
    mapping.match_note = "Waiting to be matched to an Odoo employee."


def hold_dismissed_badge(mapping: EmployeeMapping, code: str, device_name: str, odoo_name: str | None) -> None:
    """The badge is on an Odoo employee a person already said is someone else:
    it stays unmatched (never auto-matched), with no suggestion."""
    mapping.status = MappingStatus.unmapped.value
    mapping.odoo_employee_id = None
    mapping.odoo_employee_name = None
    mapping.match_method = None
    mapping.source_name = device_name or mapping.source_name
    mapping.match_note = (
        f"Badge {code} is on “{odoo_name or 'an employee'}” in Odoo, who you said is not “{device_name}”. "
        "Pick the right employee, or correct the badge in Odoo."
    )
