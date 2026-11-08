"""Market configuration model and loader (config/market_config.yaml)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "market_config.yaml"

Tier = Literal["core", "extended", "review"]
NAICS_RE = re.compile(r"^\d{6}$")
PSC_RE = re.compile(r"^[A-Z0-9]{4}$")


class CodeEntry(BaseModel):
    code: str
    tier: Tier
    label: str = ""
    rationale: str = ""

    @field_validator("code", mode="before")
    @classmethod
    def _strip(cls, v: object) -> str:
        return str(v).strip().upper()


class NaicsEntry(CodeEntry):
    @field_validator("code")
    @classmethod
    def _six_digits(cls, v: str) -> str:
        if not NAICS_RE.match(v):
            raise ValueError(f"NAICS code must be 6 digits, got {v!r}")
        return v


class PscEntry(CodeEntry):
    @field_validator("code")
    @classmethod
    def _four_chars(cls, v: str) -> str:
        if not PSC_RE.match(v):
            raise ValueError(f"PSC code must be 4 alphanumeric characters, got {v!r}")
        return v


class PrefixEntry(BaseModel):
    """A code family, e.g. PSC group '84' or NAICS industry group '4238'."""
    prefix: str
    tier: Tier
    label: str = ""
    rationale: str = ""

    @field_validator("prefix", mode="before")
    @classmethod
    def _strip(cls, v: object) -> str:
        return str(v).strip().upper()


class NaicsPrefix(PrefixEntry):
    @field_validator("prefix")
    @classmethod
    def _digits(cls, v: str) -> str:
        if not re.fullmatch(r"\d{2,5}", v):
            raise ValueError(f"NAICS prefix must be 2-5 digits, got {v!r}")
        return v


class PscPrefix(PrefixEntry):
    @field_validator("prefix")
    @classmethod
    def _chars(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z0-9]{1,3}", v) or v.startswith("A"):
            raise ValueError(f"PSC prefix must be 1-3 characters (not R&D 'A…'), got {v!r}")
        return v


class AgencyRef(BaseModel):
    name: str


class AgencyScope(BaseModel):
    basis: Literal["awarding", "funding"] = "awarding"
    toptier_agencies: list[AgencyRef] = Field(default_factory=list)
    # Optional narrowing to sub-tier agencies (e.g. Defense Logistics Agency).
    # Names exactly as USAspending's "Awarding Sub-Agencies" list shows them.
    subtier_agencies: list[AgencyRef] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _subtier_needs_toptier(self) -> "AgencyScope":
        if self.subtier_agencies and len(self.toptier_agencies) != 1:
            raise ValueError("subtier_agencies require exactly one parent toptier agency")
        return self


class KeywordConfig(BaseModel):
    mode: Literal["tag_only"] = "tag_only"
    terms: list[str] = Field(default_factory=list)


class Exclusion(BaseModel):
    kind: Literal["naics", "psc"]
    codes: list[str]
    reason: str


class DiscoveryPrefixes(BaseModel):
    naics: list[str] = Field(default_factory=list)
    psc: list[str] = Field(default_factory=list)


class Market(BaseModel):
    name: str
    status: Literal["candidate", "validated", "planned"]
    # all_codes: every contract in the agency scope, no NAICS/PSC filter
    # (e.g. "everything DLA buys"). Code lists are then not used.
    all_codes: bool = False
    validated_on: str | None = None
    description: str = ""
    definition_note: str = ""
    included_tiers: list[Tier] = Field(default_factory=lambda: ["core", "extended"])
    agency_scope: AgencyScope = Field(default_factory=AgencyScope)
    keywords: KeywordConfig = Field(default_factory=KeywordConfig)
    naics_codes: list[NaicsEntry] = Field(default_factory=list)
    psc_codes: list[PscEntry] = Field(default_factory=list)
    naics_prefixes: list[NaicsPrefix] = Field(default_factory=list)
    psc_prefixes: list[PscPrefix] = Field(default_factory=list)
    discovery_prefixes: DiscoveryPrefixes = Field(default_factory=DiscoveryPrefixes)
    documented_exclusions: list[Exclusion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _checks(self) -> "Market":
        for kind, codes in (("NAICS", [e.code for e in self.naics_codes]),
                            ("PSC", [e.code for e in self.psc_codes]),
                            ("NAICS prefix", [e.prefix for e in self.naics_prefixes]),
                            ("PSC prefix", [e.prefix for e in self.psc_prefixes])):
            dupes = sorted({c for c in codes if codes.count(c) > 1})
            if dupes:
                raise ValueError(f"{self.name}: duplicate {kind} codes {dupes}")
        if "review" in self.included_tiers:
            raise ValueError(f"{self.name}: 'review' tier codes cannot be included")
        if self.status != "planned" and not self.all_codes and not (
                self.naics_codes or self.psc_codes or self.naics_prefixes or self.psc_prefixes):
            raise ValueError(f"{self.name}: an active market needs NAICS or PSC codes")
        if self.status == "validated" and not self.definition_note:
            raise ValueError(f"{self.name}: a validated market must document its definition limits")
        # Excluded codes may be scanned at 'review' tier but never included.
        for ex in self.documented_exclusions:
            included = set(self.naics() if ex.kind == "naics" else self.psc())
            clash = sorted(included & set(ex.codes))
            if clash:
                raise ValueError(f"{self.name}: excluded {ex.kind} codes are included: {clash}")
        return self

    def excluded_codes(self, kind: str) -> set[str]:
        return {c for ex in self.documented_exclusions if ex.kind == kind for c in ex.codes}

    def naics_prefix_list(self, tiers: tuple[str, ...] | None = None) -> list[str]:
        tiers = tiers or tuple(self.included_tiers)
        return [e.prefix for e in self.naics_prefixes if e.tier in tiers]

    def psc_prefix_list(self, tiers: tuple[str, ...] | None = None) -> list[str]:
        tiers = tiers or tuple(self.included_tiers)
        return [e.prefix for e in self.psc_prefixes if e.tier in tiers]

    def has_prefixes(self) -> bool:
        return bool(self.naics_prefixes or self.psc_prefixes)

    def _tier(self, code, exact, prefixes, kind: str) -> str | None:
        """Tier of a code: exact entry first, else the longest matching prefix.
        Documented exclusions always win. Returns the entry's tier (any tier)."""
        if code is None or not isinstance(code, str) or code in self.excluded_codes(kind):
            return None
        for e in exact:
            if e.code == code:
                return e.tier
        best = None
        for e in prefixes:
            if code.startswith(e.prefix) and (best is None or len(e.prefix) > len(best.prefix)):
                best = e
        return best.tier if best else None

    def naics_tier(self, code) -> str | None:
        if self.all_codes:
            return "core"
        return self._tier(code, self.naics_codes, self.naics_prefixes, "naics")

    def psc_tier(self, code) -> str | None:
        if self.all_codes:
            return "core"
        return self._tier(code, self.psc_codes, self.psc_prefixes, "psc")

    def bases(self) -> list[str]:
        """Extraction bases: 'all' for an all-codes market, else naics and/or psc."""
        if self.all_codes:
            return ["all"]
        out = []
        if self.naics() or self.naics_prefix_list():
            out.append("naics")
        if self.psc() or self.psc_prefix_list():
            out.append("psc")
        return out

    def covered_by(self, other: "Market") -> bool:
        """True if `other` is an all-codes market with the same agency scope, so this
        market's transactions are a subset of other's download."""
        return other.all_codes and other is not self and \
            other.agency_filters() == self.agency_filters()

    def in_scope(self, toptier_agency_name, subtier_agency_name=None) -> bool:
        """Awarding-agency scope check (USAspending awarding toptier/sub-tier names)."""
        names = {a.name.casefold() for a in self.agency_scope.toptier_agencies}
        if names and not (isinstance(toptier_agency_name, str)
                          and toptier_agency_name.casefold() in names):
            return False
        subs = {a.name.casefold() for a in self.agency_scope.subtier_agencies}
        return not subs or (isinstance(subtier_agency_name, str)
                            and subtier_agency_name.casefold() in subs)

    def match(self, naics, psc) -> tuple[str, str] | None:
        """(matched_by, tier) if a transaction's codes put it in this market."""
        if self.all_codes:
            return "all", "core"
        inc = set(self.included_tiers)
        nt, pt = self.naics_tier(naics), self.psc_tier(psc)
        n_ok, p_ok = nt in inc, pt in inc
        if not (n_ok or p_ok):
            return None
        by = "both" if n_ok and p_ok else ("naics" if n_ok else "psc")
        tier = "core" if "core" in {nt if n_ok else None, pt if p_ok else None} else "extended"
        return by, tier

    def naics(self, tiers: tuple[str, ...] | None = None) -> list[str]:
        tiers = tiers or tuple(self.included_tiers)
        return [e.code for e in self.naics_codes if e.tier in tiers]

    def psc(self, tiers: tuple[str, ...] | None = None) -> list[str]:
        tiers = tiers or tuple(self.included_tiers)
        return [e.code for e in self.psc_codes if e.tier in tiers]

    def agency_filters(self) -> list[dict[str, str]]:
        """USAspending `agencies` filter objects for the scope (sub-tier if set)."""
        sc = self.agency_scope
        if sc.subtier_agencies:
            return [{"type": sc.basis, "tier": "subtier", "name": a.name,
                     "toptier_name": sc.toptier_agencies[0].name} for a in sc.subtier_agencies]
        return [{"type": sc.basis, "tier": "toptier", "name": a.name}
                for a in sc.toptier_agencies]


class MarketConfigFile(BaseModel):
    active_market: str
    markets: dict[str, Market]

    @model_validator(mode="after")
    def _active_exists(self) -> "MarketConfigFile":
        if self.active_market not in self.markets:
            raise ValueError(f"active_market {self.active_market!r} not defined")
        return self

    def market(self, key: str | None = None) -> Market:
        return self.markets[key or self.active_market]

    def active_keys(self) -> list[str]:
        return [k for k, m in self.markets.items() if m.status != "planned"]

    def covering(self, key: str, among: list[str]) -> str | None:
        """An all-codes market in `among` whose download contains this market's rows."""
        m = self.markets[key]
        return next((o for o in among if self.markets[o].all_codes and m.covered_by(self.markets[o])),
                    None)

    def download_keys(self, keys: list[str]) -> list[str]:
        """Markets that need their own download: those not covered by another one in keys."""
        return [k for k in keys if self.covering(k, keys) is None]

    def loadable_keys(self, staged: list[str]) -> list[str]:
        """Active markets that can be built from what is staged (own rows or covered)."""
        return [k for k in self.active_keys()
                if k in staged or self.covering(k, staged) is not None]

    def segment_ids(self) -> dict[str, int]:
        """Stable segment ids = position in market_config.yaml."""
        return {k: i + 1 for i, k in enumerate(self.markets)}


def load_market_config(path: Path | str = DEFAULT_CONFIG_PATH) -> MarketConfigFile:
    raw = Path(path).read_text(encoding="utf-8")
    return MarketConfigFile.model_validate(yaml.safe_load(raw))


def definition_hash(market: Market) -> str:
    """Stable hash of a market definition, stored with every load for lineage."""
    payload = market.model_dump_json(exclude={"description"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
