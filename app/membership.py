"""Shared membership semantics for private controls and delayed operations."""


def is_current_member(person):
    return isinstance(person, dict) and (person.get("status") in ("member", "administrator", "creator")
        or person.get("status") == "restricted" and person.get("is_member") is True)
