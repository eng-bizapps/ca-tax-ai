"""Ring 4 -- deterministic California property tax math (Prop 13 base year
value, Disabled Veterans' Exemption, Prop 19 base-year-value transfers).
LLM-free, same principle as income_brackets.py/entity_tax.py: a number that
changes a taxpayer's answer is computed by code, never composed by a model.

Scope, verified against BOE Publication 29/800-10, BOE's official Prop 19
page, Cal. Const. Art. XIII A, and Rev. & Tax. Code directly (not secondary
tax-prep sources) -- see property_tax_inventory.py for the full ledger of
what's built vs. deliberately excluded and why. Three tractable slices:
(1) the core Prop 13 base-year-value estimate, (2) the Disabled Veterans'
Exemption, (3) the Prop 19 base-year-value transfer formula. Deliberately
NOT modeled: exact local ad-valorem add-ons (real per-parcel data exists
across 58 counties' tax-rate-area tables, just not centrally ingested),
Mello-Roos CFD special taxes (no statewide registry exists at all), Prop 8
decline-in-value / multi-year assessed-value history (path-dependent state
a single question can't reconstruct, same complexity class as AMT's
multi-year-basis limitation), Prop 19's parent-child exclusion eligibility
(checklist-shaped, not formula-shaped), and supplemental assessments
(tractable in principle but genuinely more complex -- a bimodal
fiscal-year proration rule -- left for a dedicated future slice).
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
