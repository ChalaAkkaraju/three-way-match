"""Read model over the master data the match engine needs.

Deliberately thin and swappable: replace the constructor with SAP OData
calls, a database, or a nightly extract and nothing downstream changes.
"""

from __future__ import annotations

import difflib
import re
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .models import GoodsReceipt, PurchaseOrder, Vendor, qty

LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "co",
    "company", "corp", "corporation", "plc", "gmbh", "ag", "sa", "nv",
    "bv", "pty", "srl", "spa", "oy", "ab", "as",
}


def normalise_name(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t and t not in LEGAL_SUFFIXES]
    return " ".join(tokens)


class MasterData:
    def __init__(
        self,
        vendors: Dict[str, Vendor],
        pos: Dict[str, PurchaseOrder],
        grs: List[GoodsReceipt],
        prior_invoiced: Optional[Dict[Tuple[str, str], Decimal]] = None,
        posted_invoices: Optional[List[dict]] = None,
    ) -> None:
        self.vendors = vendors
        self.pos = pos
        self.grs = grs
        self.prior_invoiced = dict(prior_invoiced or {})
        self.posted_invoices = list(posted_invoices or [])

        self._received: Dict[Tuple[str, str], Decimal] = {}
        self._gr_dates: Dict[Tuple[str, str], List[str]] = {}
        for g in grs:
            key = (g.ebeln, g.ebelp)
            self._received[key] = qty(self._received.get(key, Decimal("0")) + g.signed_qty)
            self._gr_dates.setdefault(key, []).append(g.budat)

        self._by_norm_name: Dict[str, List[Vendor]] = {}
        for v in vendors.values():
            self._by_norm_name.setdefault(normalise_name(v.name), []).append(v)

    # -- lookups ----------------------------------------------------------

    def po(self, ebeln: Optional[str]) -> Optional[PurchaseOrder]:
        return self.pos.get(ebeln) if ebeln else None

    def received_qty(self, ebeln: str, ebelp: str) -> Decimal:
        return self._received.get((ebeln, ebelp), Decimal("0"))

    def gr_rows(self, ebeln: str, ebelp: str) -> List[GoodsReceipt]:
        return [g for g in self.grs if g.ebeln == ebeln and g.ebelp == ebelp]

    def last_gr_date(self, ebeln: str, ebelp: str) -> Optional[str]:
        dates = self._gr_dates.get((ebeln, ebelp))
        return max(dates) if dates else None

    def already_invoiced(self, ebeln: str, ebelp: str) -> Decimal:
        return self.prior_invoiced.get((ebeln, ebelp), Decimal("0"))

    # -- vendor resolution ------------------------------------------------

    def resolve_vendor(
        self, lifnr: Optional[str], name: Optional[str], threshold: float = 0.88
    ) -> Tuple[Optional[Vendor], float, str]:
        """Return (vendor, confidence, method).

        A vendor number is master data and is not printed on a supplier's
        invoice, so in practice this resolves by name. Exact-after-
        normalisation first, fuzzy second, and it refuses rather than
        picking between two near-equal candidates.
        """
        if lifnr and lifnr in self.vendors:
            return self.vendors[lifnr], 1.0, "vendor_number"

        if not name:
            return None, 0.0, "no_name"

        norm = normalise_name(name)
        exact = self._by_norm_name.get(norm)
        if exact and len(exact) == 1:
            return exact[0], 1.0, "exact_name"

        scored = sorted(
            (
                (difflib.SequenceMatcher(None, norm, cand).ratio(), cand)
                for cand in self._by_norm_name
            ),
            reverse=True,
        )
        if not scored:
            return None, 0.0, "no_candidates"
        best_score, best = scored[0]
        if best_score < threshold:
            return None, best_score, "below_threshold"
        if len(scored) > 1 and scored[1][0] > best_score - 0.05:
            return None, best_score, "ambiguous"
        return self._by_norm_name[best][0], best_score, "fuzzy_name"

    # -- duplicate ledger -------------------------------------------------

    def find_duplicates(
        self, lifnr: Optional[str], xblnr: Optional[str], bldat: Optional[str], gross: Optional[Decimal]
    ) -> List[dict]:
        """Two independent signals, because vendors re-send invoices with a
        new number and fraudsters change one character on purpose."""
        hits: List[dict] = []
        for p in self.posted_invoices:
            if lifnr and p.get("lifnr") != lifnr:
                continue
            same_number = bool(xblnr) and _loose_eq(p.get("xblnr"), xblnr)
            same_fingerprint = (
                bldat is not None
                and gross is not None
                and p.get("bldat") == bldat
                and Decimal(str(p.get("gross_total", "0"))) == gross
            )
            if same_number or same_fingerprint:
                hit = dict(p)
                hit["reason"] = "invoice_number" if same_number else "vendor_date_amount"
                hits.append(hit)
        return hits

    def register_posted(self, lifnr, xblnr, bldat, gross, belnr) -> None:
        self.posted_invoices.append(
            {
                "belnr": belnr,
                "lifnr": lifnr,
                "xblnr": xblnr,
                "bldat": bldat,
                "gross_total": str(gross),
            }
        )


def _loose_eq(a: Optional[str], b: Optional[str]) -> bool:
    """Invoice numbers compared with separators and case removed, so
    'BRI-40119' and 'bri 40119' are the same document."""
    if not a or not b:
        return False
    ca = re.sub(r"[^a-z0-9]", "", a.lower())
    cb = re.sub(r"[^a-z0-9]", "", b.lower())
    return ca == cb
