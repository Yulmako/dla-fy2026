"""PSC groups as dashboard markets.

Every DLA contract action carries one Product and Service Code. Its official
group (USAspending PSC filter tree, tier 1) is used as the market filter:
  products  – 2-digit Federal Supply Group, e.g. 59 Electrical and Electronic
              Equipment Components, 53 Hardware and Abrasives, 84 Clothing…
  services  – 1-letter category, e.g. R Support Services, J Maintenance…
  R&D       – 2-letter group, e.g. AC Defense Systems.
Names come from the public endpoint /api/v2/references/filter_tree/psc/ and are
cached in data/processed/psc_group_names.json. Without them the code is shown.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

TIERS = ("Product", "Service", "Research and Development")


def group_of(code: Any) -> str | None:
    if code is None or not isinstance(code, str) or not code.strip():
        return None
    c = code.strip().upper()
    if c[0].isdigit():
        return c[:2]
    if c[0] == "A":
        return c[:2]
    return c[0]


def kind_of(group: str) -> str:
    return "product" if group[0].isdigit() else ("rnd" if group[0] == "A" and len(group) == 2 else "service")


def fetch_group_names(client: Any, cache: Path) -> dict[str, str]:
    """Tier-1 PSC groups with official names; cached after the first success."""
    if cache.exists():
        try:
            names = json.loads(cache.read_text(encoding="utf-8"))
            if names:
                return names
        except ValueError:
            pass
    names: dict[str, str] = {}
    for tier in TIERS:
        r = client.get(f"/api/v2/references/filter_tree/psc/{quote(tier)}/", label=f"psc_tree_{tier}")
        for node in ((r.data or {}).get("results", []) if r.ok else []):
            gid, desc = str(node.get("id", "")).upper(), node.get("description")
            if gid and desc:
                names[gid] = " ".join(str(desc).split())
    if names:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(names, indent=1, sort_keys=True), encoding="utf-8")
    return names


def label(group: str, names: dict[str, str]) -> str:
    n = names.get(group)
    return f"{group} — {n}" if n else f"PSC group {group}"   # official wording, unchanged
