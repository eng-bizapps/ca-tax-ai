"""City-level Cannabis Business Tax lookup (Ring 1: local-ordinance layer).

Unlike sales-tax district rates (one statewide CDTFA-backed dataset) or the
statewide CDTFA fee regimes in fees.py, a Cannabis Business Tax is set by each
city's OWN municipal ordinance -- no single source, no uniform structure (flat
%, tiered by revenue, per-square-foot for cultivation...), and it changes by
city-council/ballot action independently in each city. Coverage here is
necessarily PARTIAL and explicit about it: absence of a row means "not yet
researched", NOT "no tax" -- that distinction matters, so callers must check
status, never assume silence means zero.

Unlike CDTFA fees (eWaste, tire...), this tax is a cost to the RETAILER, not an
itemized checkout charge -- it is priced into the shelf price, not a separate
line a customer sees. Disclosure language must reflect that difference.
"""
import db

CANNABIS_TRIGGERS = ("cannabis", "marijuana", "thc", "dispensary", "cannabinoid")


def _mentions_cannabis(question: str) -> bool:
    q = question.lower()
    return any(t in q for t in CANNABIS_TRIGGERS)


def lookup(conn, jurisdiction: str):
    """jurisdiction: an uppercased city name (same normalization as
    local_rates). Returns a dict describing what we know, or None if this
    city has not been researched at all (distinct from a confirmed no-tax)."""
    if not jurisdiction:
        return None
    row = conn.execute(
        "SELECT applies, rate, rate_note, citation, source_url, status, as_of "
        "FROM cannabis_local_tax WHERE jurisdiction=%s", (jurisdiction,)).fetchone()
    if not row:
        return None
    applies, rate, note, citation, source_url, status, as_of = row
    return {
        "jurisdiction": jurisdiction, "applies": bool(applies),
        "rate": float(rate) if rate is not None else None,
        "rate_note": note, "citation": citation, "source_url": source_url,
        "status": status, "as_of": as_of,
    }


def applicable(conn, question: str, jurisdiction: str, amount: float = None):
    """Returns a dict describing the city cannabis business tax for this
    question/location, or None if not relevant (no cannabis mention, no known
    jurisdiction, or nothing on file for that city). NEVER guesses a rate for
    an unresearched city."""
    if not _mentions_cannabis(question) or not jurisdiction:
        return None
    info = lookup(conn, jurisdiction)
    if not info:
        return None
    result = {**info, "amount": amount, "cost": None}
    if info["applies"] and info["rate"] is not None and amount is not None:
        result["cost"] = round(amount * info["rate"], 2)
    return result
