"""Contact normalization helpers for radio backends."""

from __future__ import annotations

from typing import Any


def normalize_contact_dict(contact: Any) -> dict[str, Any]:
    """Return a meshcore_py-style contact dict from dicts or pyMC Contact objects."""

    if contact is None:
        return {}
    if isinstance(contact, dict):
        result = dict(contact)
    else:
        public_key = getattr(contact, "public_key", "")
        if isinstance(public_key, bytes):
            public_key = public_key.hex()
        out_path = getattr(contact, "out_path", b"")
        if isinstance(out_path, bytes):
            out_path_value: Any = out_path.hex()
        else:
            out_path_value = out_path
        result = {
            "public_key": public_key,
            "name": getattr(contact, "name", ""),
            "adv_name": getattr(contact, "name", ""),
            "type": getattr(contact, "adv_type", 0),
            "flags": getattr(contact, "flags", 0),
            "out_path": out_path_value,
            "out_path_len": getattr(contact, "out_path_len", -1),
            "last_advert": getattr(contact, "last_advert_timestamp", 0),
            "last_seen": getattr(contact, "lastmod", 0),
            "latitude": getattr(contact, "gps_lat", 0.0),
            "longitude": getattr(contact, "gps_lon", 0.0),
        }

    result.setdefault("name", result.get("adv_name", ""))
    result.setdefault("adv_name", result.get("name", ""))
    result.setdefault("public_key", "")
    result.setdefault("type", result.get("adv_type", 0))
    result.setdefault("flags", 0)
    result.setdefault("out_path", "")
    result.setdefault("out_path_len", -1)
    return result


def contacts_dict_from_iterable(contacts: Any) -> dict[str, dict[str, Any]]:
    """Build a dict keyed by public key from a contact iterable or mapping."""

    values = contacts.values() if isinstance(contacts, dict) else contacts or []
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        contact = normalize_contact_dict(item)
        public_key = (contact.get("public_key") or "").strip()
        if public_key:
            result[public_key] = contact
    return result
