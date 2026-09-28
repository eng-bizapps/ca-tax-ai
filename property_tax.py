"""Ring 4 -- deterministic California property tax math (Prop 13 base year
value, Disabled Veterans' Exemption, Prop 19 base-year-value transfers,
Prop 19 parent-child exclusion value cap, supplemental assessments).
LLM-free, same principle as income_brackets.py/entity_tax.py: a number that
changes a taxpayer's answer is computed by code, never composed by a model.

Scope, verified against BOE Publication 29/800-10, BOE's official Prop 19
page, BOE Letters To Assessors, Cal. Const. Art. XIII A, and Rev. & Tax.
Code directly (not secondary tax-prep sources) -- see
property_tax_inventory.py for the full ledger of what's built vs.
deliberately excluded and why. Five tractable slices: (1) the core Prop 13
base-year-value estimate, (2) the Disabled Veterans' Exemption, (3) the
Prop 19 base-year-value transfer formula, (4) the Prop 19 parent-child/
grandparent-grandchild exclusion's value-cap formula (eligibility itself is
a checklist, see property_eligibility.py -- this module only computes the
dollar consequence once eligibility is already confirmed True), (5)
supplemental assessments (a bimodal fiscal-year proration rule). Still
deliberately NOT modeled: exact local ad-valorem add-ons (real per-parcel
data exists across 58 counties' tax-rate-area tables, just not centrally
ingested), Mello-Roos CFD special taxes (no statewide registry exists at
all), Prop 8 decline-in-value / multi-year assessed-value history
(path-dependent state a single question can't reconstruct, same complexity
class as AMT's multi-year-basis limitation).
"""
# Property tax runs on a FISCAL year (Jul 1-Jun 30) driven by the PRIOR
# Jan 1 lien date -- NOT the same year-boundary convention income_brackets.
# DEFAULT_TAX_YEAR uses. As of 2026-09-07, FY2026-27's Jan 1 2026 lien date
# has already passed and its bill is the one currently being paid (1st
# installment due Nov 1 2026) -- so "my current property tax" means lien
# year 2026. Bump this each January when BOE publishes the new lien year's
# DV-exemption figures via Letter To Assessors, not before.
DEFAULT_LIEN_YEAR = 2026

# Constitutional structural constants (Cal. Const. Art. XIII A) -- same
# "structural formula constant, not annually-republished data" class as
# income_brackets.CASUALTY_LOSS_AGI_FLOOR_RATE.
PROP13_BASE_RATE = 0.01                 # Art. XIII A Sec. 1(a), 1% cap
PROP13_INFLATION_CAP_RATE = 0.02        # Art. XIII A Sec. 2(b), 2%/yr cap
HOMEOWNERS_EXEMPTION_AMOUNT = 7000.0    # Art. XIII A Sec. 3(k); R&TC 218

PROP19_MAX_TRANSFERS_AGE_DISABLED = 3   # R&TC 69.6 -- NOT capped for disaster claimants
PROP19_REPLACEMENT_WINDOW_YEARS = 2
PROP19_CLAIMANT_TYPES = {"age_55", "disabled", "disaster"}

PROP13_CITATION = "Cal. Const. Art. XIII A Sec. 1(a), Sec. 2(b); Rev. & Tax. Code Sec. 51"
PROP13_SOURCE_URL = "https://www.boe.ca.gov/proptaxes/pdf/pub29.pdf"
HOMEOWNERS_EXEMPTION_CITATION = "Cal. Const. Art. XIII Sec. 3(k); Rev. & Tax. Code Sec. 218"
DV_EXEMPTION_CITATION = "Rev. & Tax. Code Sec. 205.5; BOE Letter To Assessors 2025/014"
DV_EXEMPTION_SOURCE_URL = "https://www.boe.ca.gov/proptaxes/dv_exemption.htm"
PROP19_CITATION = "Rev. & Tax. Code Sec. 69.6; Cal. Const. Art. XIII A Sec. 2(a)"
PROP19_SOURCE_URL = "https://www.boe.ca.gov/prop19/"


def compute_property_tax_estimate(purchase_price: float, purchase_year: int,
                                   current_tax_year: int = DEFAULT_LIEN_YEAR,
                                   homeowners_exemption: bool = False):
    """Core Prop 13 estimate. Pure math, no DB lookup.

    factored_base_year_value = purchase_price * (1 + PROP13_INFLATION_CAP_RATE)
                                ** (current_tax_year - purchase_year)

    A DELIBERATE OVERESTIMATE: the real annual factor is the CA CPI change
    (published yearly by BOE), usually LESS than 2% -- assuming the full 2%
    cap every year mirrors this codebase's established "when simplifying,
    err toward overestimating rather than understating" precedent (see
    income_brackets.compute_itemized_deduction_phaseout's own disclosed
    approximation). This MUST be disclosed in the caller's answer text
    every time, not left as a docstring-only caveat.

    Returns None if purchase_price <= 0 or current_tax_year < purchase_year.
    Does NOT model Prop 8 decline-in-value or local ad-valorem add-ons on
    top of the 1% -- the rate here is ONLY the constitutional cap, never
    presented as "your total tax rate.\""""
    if purchase_price is None or purchase_price <= 0:
        return None
    if current_tax_year is None or purchase_year is None or current_tax_year < purchase_year:
        return None
    years = current_tax_year - purchase_year
    factored_base_year_value = purchase_price * (1.0 + PROP13_INFLATION_CAP_RATE) ** years
    exemption_applied = HOMEOWNERS_EXEMPTION_AMOUNT if homeowners_exemption else 0.0
    assessed_value = max(0.0, factored_base_year_value - exemption_applied)
    tax = round(PROP13_BASE_RATE * assessed_value, 2)
    return {
        "purchase_price": purchase_price, "purchase_year": purchase_year,
        "current_tax_year": current_tax_year, "years_compounded": years,
        "factored_base_year_value": round(factored_base_year_value, 2),
        "homeowners_exemption": homeowners_exemption,
        "exemption_applied": exemption_applied,
        "assessed_value": round(assessed_value, 2),
        "tax": tax, "citation": PROP13_CITATION, "source_url": PROP13_SOURCE_URL,
    }


def compute_disabled_veterans_exemption_ca(conn, household_income: float = None,
                                            tax_year: int = DEFAULT_LIEN_YEAR):
    """Looks up ca_disabled_veterans_exemption for tax_year. If
    household_income is given and <= income_limit, returns the
    low_income_exemption tier; otherwise (income given and over the limit,
    OR income not stated at all) returns basic_exemption -- basic applies
    with NO income limit per R&TC 205.5, so it's always the safe floor.
    When household_income is None, income_unconfirmed=True is set so the
    caller can disclose "a higher exemption may apply if your household
    income is under $X" rather than silently assuming the lower tier.
    Returns None if tax_year has no seeded row."""
    r = conn.execute(
        "SELECT basic_exemption, low_income_exemption, income_limit, citation, source_url "
        "FROM ca_disabled_veterans_exemption WHERE tax_year=%s", (tax_year,)).fetchone()
    if not r:
        return None
    basic_exemption, low_income_exemption, income_limit, citation, source_url = (
        float(r[0]), float(r[1]), float(r[2]), r[3], r[4])
    income_unconfirmed = household_income is None
    if household_income is not None and household_income <= income_limit:
        exemption = low_income_exemption
        tier = "low_income"
    else:
        exemption = basic_exemption
        tier = "basic"
    return {
        "tax_year": tax_year, "household_income": household_income,
        "income_limit": income_limit, "tier": tier, "exemption": exemption,
        "basic_exemption": basic_exemption, "low_income_exemption": low_income_exemption,
        "income_unconfirmed": income_unconfirmed,
        "citation": citation or DV_EXEMPTION_CITATION, "source_url": source_url or DV_EXEMPTION_SOURCE_URL,
    }


def compute_property_tax_with_dv_exemption(conn, purchase_price: float, purchase_year: int,
                                            household_income: float = None,
                                            current_tax_year: int = DEFAULT_LIEN_YEAR):
    """Composes compute_property_tax_estimate with
    compute_disabled_veterans_exemption_ca: the factored base year value is
    computed identically to the core estimate, then the DV exemption
    (NEVER the Homeowners' Exemption) is subtracted before the 1% rate
    applies. Confirmed directly from BOE's own page ("Only one exemption
    per property is allowed. If you are granted the Disabled Veterans'
    Exemption, the Homeowners' Exemption is not available on the same
    property") that these are mutually exclusive and DV always wins when
    eligible -- there is no case where a DV-eligible caller would want the
    smaller Homeowners' Exemption instead, so this function does not accept
    a homeowners_exemption parameter at all."""
    base = compute_property_tax_estimate(purchase_price, purchase_year, current_tax_year,
                                          homeowners_exemption=False)
    if not base:
        return None
    dv = compute_disabled_veterans_exemption_ca(conn, household_income, current_tax_year)
    if not dv:
        return None
    assessed_value = max(0.0, base["factored_base_year_value"] - dv["exemption"])
    tax = round(PROP13_BASE_RATE * assessed_value, 2)
    return {**base, "dv_exemption": dv["exemption"], "dv_tier": dv["tier"],
            "income_unconfirmed": dv["income_unconfirmed"],
            "income_limit": dv["income_limit"], "low_income_exemption": dv["low_income_exemption"],
            "assessed_value": round(assessed_value, 2), "tax": tax,
            "citation": DV_EXEMPTION_CITATION, "source_url": DV_EXEMPTION_SOURCE_URL}


# Timing bucket -> the percentage of the ORIGINAL home's full cash value
# the REPLACEMENT'S full cash value gets compared against -- a genuinely
# separate rule from the 3-transfer cap, confirmed against BOE's own
# worked example AND independently cross-checked via web search (initial
# design mistakenly omitted this entirely, computing as if every
# replacement were purchased BEFORE the original sale -- a real, found-
# before-shipping correction, not a hypothetical). "before_sale" = 100%
# (no bonus); "within_1_year" = 105%; "within_2_years" = 110%. Purchasing
# the replacement MORE than 2 years after the sale is NOT a worse
# percentage -- it's outright ineligible (PROP19_REPLACEMENT_WINDOW_YEARS).
PROP19_TIMING_COMPARISON_PCT = {
    "before_sale": 1.00, "within_1_year": 1.05, "within_2_years": 1.10,
}


def compute_prop19_base_year_transfer(original_byv: float, original_fcv: float,
                                       replacement_fcv: float,
                                       claimant_type: str = "age_55",
                                       transfer_number: int = 1,
                                       replacement_timing: str = "within_1_year"):
    """BOE's own worked-example formula (R&TC 69.6), CONFIRMED via BOE's
    exact worked example (original FCV $400,000, FBYV $100,000,
    replacement purchased within 1 year at FCV $600,000 -> adjusted
    original FCV = $400,000 x 1.05 = $420,000; value added =
    max(0, $600,000-$420,000) = $180,000; new taxable value =
    $100,000+$180,000 = $280,000 -- matches BOE's own page exactly):

    comparison_fcv = original_fcv * PROP19_TIMING_COMPARISON_PCT[replacement_timing]
    value_add = max(0, replacement_fcv - comparison_fcv)
    new_taxable_value = original_byv + value_add

    claimant_type: 'age_55' | 'disabled' | 'disaster'. For 'age_55'/
    'disabled', transfer_number > PROP19_MAX_TRANSFERS_AGE_DISABLED (3)
    returns {'eligible': False, ...} instead of a number -- a disallowed
    transfer must never be silently computed as if it were allowed.
    'disaster' claimants have NO transfer-count cap per the statute's own
    text -- transfer_number is accepted but never blocks eligibility for
    that claimant_type. replacement_timing outside
    PROP19_TIMING_COMPARISON_PCT (i.e. "more than 2 years after the sale")
    is OUTRIGHT INELIGIBLE (a worse percentage does not apply beyond the
    2-year window -- the whole transfer stops qualifying), returns
    {'eligible': False, ...} same as the transfer-count cap.

    Does NOT verify the original home's own sale/destruction, or same/
    different-county rules -- these, and the stated replacement_timing
    bucket itself, are disclosed INPUT ASSUMPTIONS the caller states,
    exactly like every other "trust the stated figures" compute function
    in this codebase (e.g. income_brackets.compute_charitable_cap
    trusting a pre-floor charitable_amount)."""
    if claimant_type not in PROP19_CLAIMANT_TYPES:
        return None
    if original_byv is None or original_fcv is None or replacement_fcv is None:
        return None
    if original_byv < 0 or original_fcv < 0 or replacement_fcv < 0:
        return None
    if claimant_type in ("age_55", "disabled") and transfer_number > PROP19_MAX_TRANSFERS_AGE_DISABLED:
        return {"eligible": False, "claimant_type": claimant_type,
                "transfer_number": transfer_number,
                "max_transfers": PROP19_MAX_TRANSFERS_AGE_DISABLED,
                "reason": "transfer_count_exceeded",
                "citation": PROP19_CITATION, "source_url": PROP19_SOURCE_URL}
    comparison_pct = PROP19_TIMING_COMPARISON_PCT.get(replacement_timing)
    if comparison_pct is None:
        return {"eligible": False, "claimant_type": claimant_type,
                "replacement_timing": replacement_timing,
                "reason": "replacement_timing_outside_window",
                "citation": PROP19_CITATION, "source_url": PROP19_SOURCE_URL}
    comparison_fcv = original_fcv * comparison_pct
    value_add = max(0.0, replacement_fcv - comparison_fcv)
    new_taxable_value = original_byv + value_add
    tax = round(PROP13_BASE_RATE * new_taxable_value, 2)
    return {"eligible": True, "claimant_type": claimant_type, "transfer_number": transfer_number,
            "replacement_timing": replacement_timing, "comparison_pct": comparison_pct,
            "original_byv": original_byv, "original_fcv": original_fcv,
            "comparison_fcv": round(comparison_fcv, 2), "replacement_fcv": replacement_fcv,
            "value_add": round(value_add, 2),
            "new_taxable_value": round(new_taxable_value, 2), "tax": tax,
            "citation": PROP19_CITATION, "source_url": PROP19_SOURCE_URL}


# BOE republishes this every 2 years (odd years, effective Feb 16) per
# R&TC 63.2(d) -- current figure covers Feb 16 2025-Feb 15 2027. Bump when
# BOE publishes the 2027 figure (expect ~Dec 2026/Jan 2027), not before.
# Bare constant, not a tax_year-keyed table, same "promote to a table later,
# not now" precedent as HOMEOWNERS_EXEMPTION_AMOUNT -- only one cycle is
# confirmed right now.
PARENT_CHILD_EXCLUSION_VALUE_CAP = 1_044_586.0

PARENT_CHILD_EXCLUSION_CITATION = ("Rev. & Tax. Code Sec. 63.2; Cal. Const. Art. XIII A Sec. "
                                    "2.1(c); BOE Letter To Assessors 2026/026")
PARENT_CHILD_EXCLUSION_SOURCE_URL = "https://www.boe.ca.gov/prop19/"


def compute_parent_child_exclusion_value(old_fbyv: float, fmv: float, pct_interest: float = 1.0):
    """R&TC 63.2(d) value-cap formula for a Prop 19 parent-child (or
    grandparent-grandchild) transfer that has ALREADY been confirmed
    eligible -- see property_eligibility.detect_parent_child_exclusion_
    qualifies for the eligibility determination itself, a genuinely
    separate checklist-shaped question this function does NOT evaluate.
    Trusts eligibility the same way compute_prop19_base_year_transfer's own
    docstring cites income_brackets.compute_charitable_cap trusting a
    pre-floor charitable_amount -- "trust the stated figures" is this
    codebase's standing precedent for a compute function one layer below
    an eligibility gate.

    value_cap = old_fbyv + PARENT_CHILD_EXCLUSION_VALUE_CAP
    excess = max(0, fmv - value_cap)
    new_taxable_value = old_fbyv + excess    (== old_fbyv, unchanged, if fmv <= value_cap)
    transferred_share_value = new_taxable_value * pct_interest

    Confirmed via BOE's own worked-example MECHANIC (LTA 2026/026 Q50) --
    the mechanic (value_cap = old FBYV + current addend; excess = max(0,
    fmv-value_cap); new_taxable_value = old_fbyv+excess, prorated by
    pct_interest) is not in doubt. That example's own DOLLAR FIGURES,
    however, don't reconcile against PARENT_CHILD_EXCLUSION_VALUE_CAP
    ($163,076 old FBYV + $1,044,586 =/= the example's stated $1,163,076
    cap -- the example's own numbers only work with a $1,000,000 addend,
    an earlier cycle's figure) -- so that example is deliberately NOT used
    anywhere in this codebase as a regression fixture; see
    property_item_sweep.py for a fresh, self-consistent worked example
    instead.

    pct_interest applies only to the TRANSFERRED share's new taxable value
    -- the untransferred remainder (if pct_interest < 1) keeps its own old
    FBYV proportionally, unchanged, and is NOT part of this function's
    return (the caller already knows the untransferred share never
    reassesses at all).

    Returns None if old_fbyv/fmv is negative or pct_interest is outside
    (0, 1]."""
    if old_fbyv is None or fmv is None or old_fbyv < 0 or fmv < 0:
        return None
    if pct_interest is None or not (0 < pct_interest <= 1):
        return None
    value_cap = old_fbyv + PARENT_CHILD_EXCLUSION_VALUE_CAP
    reassessed = fmv > value_cap
    excess = max(0.0, fmv - value_cap)
    new_taxable_value = old_fbyv + excess
    transferred_share_value = round(new_taxable_value * pct_interest, 2)
    tax = round(PROP13_BASE_RATE * transferred_share_value, 2)
    return {
        "old_fbyv": old_fbyv, "fmv": fmv, "pct_interest": pct_interest,
        "value_cap": round(value_cap, 2), "reassessed": reassessed,
        "excess": round(excess, 2), "new_taxable_value": round(new_taxable_value, 2),
        "transferred_share_value": transferred_share_value, "tax": tax,
        "citation": PARENT_CHILD_EXCLUSION_CITATION,
        "source_url": PARENT_CHILD_EXCLUSION_SOURCE_URL,
    }


# Keyed by the PRESUMED EFFECTIVE MONTH (R&TC 75.41(b): the event date is
# legally presumed to be the FIRST DAY OF THE FOLLOWING CALENDAR MONTH,
# always rounding UP regardless of which day the event actually occurred
# in) -- NOT the raw event month. Only whole months from the presumed
# effective date through June 30 count. Cross-confirmed independently
# against both R&TC 75.41(c)'s own table and BOE's separate published
# Supplemental Assessment page -- this table is solid.
SUPPLEMENTAL_PRORATION_FACTOR = {
    7: 1.00, 8: 0.92, 9: 0.83, 10: 0.75, 11: 0.67, 12: 0.58,
    1: 0.50, 2: 0.42, 3: 0.33, 4: 0.25, 5: 0.17, 6: 0.08,
}

SUPPLEMENTAL_CITATION = "Rev. & Tax. Code Sec. 75.11, 75.41; BOE Pub. 29 pp.11-12,16"
SUPPLEMENTAL_SOURCE_URL = "https://www.boe.ca.gov/proptaxes/pdf/pub29.pdf"


def _presumed_effective_date(event_month: int, event_year: int):
    """R&TC 75.41(b) rounds the event date UP to the first day of the
    FOLLOWING calendar month, regardless of which day of event_month the
    event actually happened. A December event rolls the CALENDAR year
    forward (presumed January of event_year+1) but stays in the SAME
    fiscal year as the event itself (FY runs Jul-Jun) -- this asymmetry
    (calendar-year rollover without a fiscal-year rollover) is the
    single easiest place to get this feature wrong."""
    presumed_month = event_month % 12 + 1
    presumed_year = event_year + 1 if event_month == 12 else event_year
    return presumed_month, presumed_year


def _fiscal_year_start(presumed_month: int, presumed_year: int) -> int:
    return presumed_year if presumed_month >= 7 else presumed_year - 1


def compute_supplemental_assessment(event_month: int, event_year: int,
                                     new_base_year_value: float,
                                     prior_taxable_value: float = None,
                                     event_type: str = "change_of_ownership",
                                     ownership_pct: float = 1.0):
    """R&TC 75.11/75.41 bimodal supplemental-assessment proration.

    added_value = new_base_year_value - prior_taxable_value  (change of
                  ownership -- prior_taxable_value required) OR
                  new_base_year_value directly (new construction -- no
                  comparison, the added value IS the assessment)
    factor = SUPPLEMENTAL_PRORATION_FACTOR[presumed_effective_month]
    supplemental_1 = added_value * factor * ownership_pct  (always computed
                      -- proration for the remainder of the CURRENT fiscal
                      year, from the presumed effective date through June 30)

    R&TC 75.11(b) bimodal rule, keyed off the RAW event month (Jan-May
    inclusive -> presumed effective month Feb-Jun):
      - Jan 1-May 31 event: a SECOND supplemental assessment, for the
        ENTIRE next fiscal year, is ALSO owed -- because the next regular
        roll's Jan 1 lien date already passed before the event, so that
        roll would otherwise still show the OLD value. For a FULL-interest
        transfer this second supplemental is UNPRORATED (factor 1.00,
        the entire added_value). For a PARTIAL-interest transfer, the
        statute uses a genuinely different formula (sum of the new base
        year value of the transferred portion + the taxable value of the
        remainder on the roll being prepared, minus the taxable value of
        the whole property on that roll) that needs facts (the remainder's
        and whole property's taxable values ON THE ROLL BEING PREPARED)
        this function doesn't have -- deliberately NOT computed; returns
        supplemental_2=None with partial_interest_second_supplemental_note
        explaining why, rather than guessing. supplemental_1 is still
        returned in full either way -- an unknowable second number is never
        a reason to withhold the first, known one.
      - Jun 1-Dec 31 event: only ONE supplemental assessment (the next
        Jan 1 lien date hasn't happened yet, so the following regular roll
        will already capture the new value on its own).

    Tax rate is PROP13_BASE_RATE for both supplementals -- R&TC 75.41(a):
    the SAME total ad valorem rate that would otherwise apply, no special
    supplemental rate exists. (Same local-TRA-rate-gap disclosure as the
    core Prop 13 estimate applies -- this only models the 1% constitutional
    base; the caller's answer text must disclose that, not this function.)

    Returns None if event_month not in 1..12, new_base_year_value <= 0,
    event_type not recognized, ownership_pct outside (0, 1], or
    event_type=='change_of_ownership' with prior_taxable_value missing/
    negative."""
    if event_month is None or not (1 <= event_month <= 12):
        return None
    if new_base_year_value is None or new_base_year_value <= 0:
        return None
    if event_type not in ("change_of_ownership", "new_construction"):
        return None
    if ownership_pct is None or not (0 < ownership_pct <= 1):
        return None
    if event_type == "change_of_ownership":
        if prior_taxable_value is None or prior_taxable_value < 0:
            return None
        added_value = max(0.0, new_base_year_value - prior_taxable_value)
    else:
        added_value = new_base_year_value

    presumed_month, presumed_year = _presumed_effective_date(event_month, event_year)
    fiscal_year_start = _fiscal_year_start(presumed_month, presumed_year)
    factor = SUPPLEMENTAL_PRORATION_FACTOR[presumed_month]
    bimodal = 1 <= event_month <= 5

    supplemental_1 = round(added_value * factor * ownership_pct, 2)
    supplemental_1_tax = round(PROP13_BASE_RATE * supplemental_1, 2)

    supplemental_2 = None
    supplemental_2_tax = None
    partial_interest_second_supplemental_note = None
    supplemental_count = 1
    if bimodal:
        if ownership_pct == 1.0:
            supplemental_2 = round(added_value, 2)
            supplemental_2_tax = round(PROP13_BASE_RATE * supplemental_2, 2)
            supplemental_count = 2
        else:
            partial_interest_second_supplemental_note = (
                "A second supplemental assessment also applies for the entire next fiscal year, "
                "but for a partial-interest transfer it's computed differently (new base year "
                "value of the transferred portion, plus the taxable value of the remainder on "
                "the roll being prepared, minus the taxable value of the whole property on that "
                "roll) -- not modeled here; your county assessor issues that second bill."
            )

    return {
        "event_month": event_month, "event_year": event_year,
        "presumed_effective_month": presumed_month, "presumed_effective_year": presumed_year,
        "fiscal_year_start": fiscal_year_start,
        "fiscal_year_label": f"{fiscal_year_start}-{str(fiscal_year_start + 1)[-2:]}",
        "proration_factor": factor, "event_type": event_type,
        "added_value": round(added_value, 2), "ownership_pct": ownership_pct,
        "bimodal": bimodal, "supplemental_count": supplemental_count,
        "supplemental_1": supplemental_1, "supplemental_1_tax": supplemental_1_tax,
        "supplemental_2": supplemental_2, "supplemental_2_tax": supplemental_2_tax,
        "partial_interest_second_supplemental_note": partial_interest_second_supplemental_note,
        "citation": SUPPLEMENTAL_CITATION, "source_url": SUPPLEMENTAL_SOURCE_URL,
    }
