"""Ring 4 -- deterministic California property tax math (Prop 13 base year
value, Disabled Veterans' Exemption, Prop 19 base-year-value transfers,
Prop 19 parent-child exclusion value cap, supplemental assessments,
county-average local override rates, exact per-TRA rates for the counties
in TRA_FORMATS).
LLM-free, same principle as income_brackets.py/entity_tax.py: a number that
changes a taxpayer's answer is computed by code, never composed by a model.

Scope, verified against BOE Publication 29/800-10, BOE's official Prop 19
page, BOE Letters To Assessors, Cal. Const. Art. XIII A, Rev. & Tax. Code
(read directly from leginfo.legislature.ca.gov, not a secondary source),
the CA State Controller's Office "CA Property Tax Data" portal, and each
covered county's own published rate book directly -- see property_tax_inventory.py
for the full ledger of what's built vs. deliberately excluded and why.
Eight tractable slices: (1) the
core Prop 13 base-year-value estimate, (2) the Disabled Veterans'
Exemption, (3) the Prop 19 base-year-value transfer formula, (4) the Prop
19 parent-child/grandparent-grandchild exclusion's value-cap formula
(eligibility itself is a checklist, see property_eligibility.py -- this
module only computes the dollar consequence once eligibility is already
confirmed True), (5) supplemental assessments (a bimodal fiscal-year
proration rule), (6) county-average local ad-valorem OVERRIDE rates
(compute_county_override_rate/compute_property_tax_estimate_with_county_
rate -- a COUNTY-WIDE AVERAGE for 56 of 58 counties, sourced from the SCO
portal's Allocations+Levies data, NOT an exact per-parcel Tax-Rate-Area
figure; San Benito and Plumas are deliberately excluded, confirmed SCO
data errors -- see load_county_override_rates.py's own docstring for the
cross-check evidence), (7) Prop 8 decline-in-value for the ORDINARY market-
decline case (compute_property_tax_prop8_decline -- see the SECOND
correction note below; damage/destruction stays deferred, a genuinely
different mechanic), (8) exact per-TRA combined rates for the counties in
TRA_FORMATS (compute_tra_rate/compute_property_tax_estimate_with_tra_rate
-- real data from each county's own published rate book; every county NOT
in TRA_FORMATS still uses the (6) county-average -- see extract_kern_tra_
rates.py's and extract_tra_rates.py's docstrings for the extraction
methodology and property_tax_inventory.py's 'tra-rate-*' vs. 'local-tra-
rate' items). Still deliberately NOT modeled: exact per-parcel rates for
counties not in TRA_FORMATS (each publishes its own differently-formatted
source -- confirmed via live survey that even the largest counties share no
common format, and some gate their tools behind bot-protection or publish
no machine-readable source -- the county-AVERAGE in (6) is the real but
coarser substitute for those), Mello-Roos CFD special taxes (no
statewide registry exists at all), and Prop 8 decline-in-value for
property damaged/destroyed by disaster (Rev. & Tax. Code Sec. 51(b)/(c) --
a genuinely separate, itself multi-year/path-dependent mechanic, see (7)
above).

SECOND CORRECTION FOUND AND FIXED THIS SESSION, worth remembering just as
much as the Prop 19 one below: Prop 8 decline-in-value was assessed THREE
SEPARATE TIMES as "genuinely out of reach" on the theory that a current-
year determination needs the property's full multi-year assessment
history. That assumption was WRONG, caught only by reading Rev. & Tax.
Code Sec. 51 directly in full (not a secondary source, not recalled tax
knowledge) -- Sec. 51(a)(1)'s factored base year value ceiling compounds
PURELY from the original base year value at up to 2%/year, NEVER reset or
path-dependent on any intervening year's actual enrolled value; Sec. 51(e)
confirms the assessor just re-compares current full cash value against
that same independently-compounding ceiling every year "until that value
exceeds" it. No intervening-year history is needed for the ORDINARY case
-- only original purchase price/year (already used by the core estimate)
plus a stated CURRENT market value. Sec. 51(b) (damage/destruction, no
county Sec. 170 ordinance) IS a genuine, separate multi-year mechanic
(land/improvements computed separately, becoming a new base year value
"until restored, repaired, or reconstructed") -- correctly still deferred,
not conflated with the ordinary case. Lesson: don't accept "genuinely out
of reach" as settled without a direct primary-source read, even after
multiple prior passes reached the same conclusion -- the SAME lesson AMT's
own build history (see income-coverage-blueprint-progress memory) learned
repeatedly this session for a different domain.
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
                                            current_tax_year: int = DEFAULT_LIEN_YEAR,
                                            total_rate: float = PROP13_BASE_RATE):
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
    a homeowners_exemption parameter at all.

    total_rate (default the 1% base) is the combined rate applied to the
    post-exemption assessed value; the caller passes the exact-TRA or
    county-average rate from compute_local_rate when it has one."""
    base = compute_property_tax_estimate(purchase_price, purchase_year, current_tax_year,
                                          homeowners_exemption=False)
    if not base:
        return None
    dv = compute_disabled_veterans_exemption_ca(conn, household_income, current_tax_year)
    if not dv:
        return None
    assessed_value = max(0.0, base["factored_base_year_value"] - dv["exemption"])
    tax = round(total_rate * assessed_value, 2)
    return {**base, "total_rate": total_rate, "dv_exemption": dv["exemption"], "dv_tier": dv["tier"],
            "income_unconfirmed": dv["income_unconfirmed"],
            "income_limit": dv["income_limit"], "low_income_exemption": dv["low_income_exemption"],
            "assessed_value": round(assessed_value, 2), "tax": tax,
            "citation": DV_EXEMPTION_CITATION, "source_url": DV_EXEMPTION_SOURCE_URL}


PROP8_DECLINE_CITATION = "Rev. & Tax. Code Sec. 51(a)(2), (e); Cal. Const. Art. XIII A"
PROP8_DECLINE_SOURCE_URL = "https://www.boe.ca.gov/pdf/pub800-10.pdf"


def compute_property_tax_prop8_decline(purchase_price: float, purchase_year: int,
                                        current_market_value: float,
                                        current_tax_year: int = DEFAULT_LIEN_YEAR,
                                        homeowners_exemption: bool = False,
                                        total_rate: float = PROP13_BASE_RATE):
    """R&TC 51(a) decline-in-value ('Prop 8') for the ORDINARY market-
    decline case -- see this module's own SECOND correction-found note for
    the full reasoning. Confirmed directly from the statute text (not a
    secondary source): the factored base year value (FBYV) ceiling under
    51(a)(1) compounds PURELY from the original base year value at up to
    2%/year, never reset or path-dependent on any intervening year's
    actual enrolled value -- 51(e) confirms the assessor just re-compares
    current full cash value against that SAME independently-compounding
    ceiling every year "until that value exceeds" it, at which point
    normal Prop 13 taxation resumes automatically. So a current-year
    determination needs NO intervening-year history -- only the same
    purchase_price/purchase_year compute_property_tax_estimate already
    uses, plus one more stated fact: current_market_value (trusted the
    same "trust the stated figures" way every other compute function in
    this codebase already works).

    assessed_value = min(factored_base_year_value, current_market_value),
    THEN the Homeowners' Exemption (if any) is subtracted -- compare
    first, exempt second, mirroring compute_property_tax_estimate's own
    internal order exactly. Never subtract the exemption from each side
    independently before the comparison.

    Does NOT detect or guard against Rev. & Tax. Code 51(b)/(c) (property
    damaged/destroyed by disaster/misfortune/calamity) -- that is a
    GENUINELY DIFFERENT, itself multi-year/path-dependent mechanic (land
    and improvements computed separately, becoming a new base year value
    "until restored, repaired, or reconstructed" under 51(b), or deferred
    entirely to a county's own Sec. 170 ordinance under 51(c)). The CALLER
    (engine.py) must wall that off BEFORE calling this function -- same
    "trust the stated figures, gate at the caller" precedent compute_
    prop19_base_year_transfer's own docstring documents for its own
    out-of-scope inputs.

    total_rate: the rate applied to the resulting assessed value. Defaults
    to the 1% base; the caller passes the exact-TRA or county-average
    combined rate from compute_local_rate when it has one -- the rate is
    orthogonal to whether the value came from Prop 8 (the decline only
    changes the assessed value; the voted debt overrides apply to it too).

    Returns None if the core estimate fails, or current_market_value is
    None/<= 0."""
    base = compute_property_tax_estimate(purchase_price, purchase_year, current_tax_year,
                                          homeowners_exemption=False)
    if not base:
        return None
    if current_market_value is None or current_market_value <= 0:
        return None
    factored_base_year_value = base["factored_base_year_value"]
    prop8_active = current_market_value < factored_base_year_value
    pre_exemption_assessed = min(factored_base_year_value, current_market_value)
    exemption_applied = HOMEOWNERS_EXEMPTION_AMOUNT if homeowners_exemption else 0.0
    assessed_value = max(0.0, pre_exemption_assessed - exemption_applied)
    tax = round(total_rate * assessed_value, 2)
    return {**base, "total_rate": total_rate, "current_market_value": current_market_value, "prop8_active": prop8_active,
            "pre_exemption_assessed_value": round(pre_exemption_assessed, 2),
            "homeowners_exemption": homeowners_exemption, "exemption_applied": exemption_applied,
            "assessed_value": round(assessed_value, 2), "tax": tax,
            "citation": PROP8_DECLINE_CITATION, "source_url": PROP8_DECLINE_SOURCE_URL}


DISASTER_RELIEF_CITATION = "Rev. & Tax. Code Sec. 170(b), (g), (h)"
DISASTER_RELIEF_SOURCE_URL = "https://www.boe.ca.gov/proptaxes/disaster-relief.htm"
DISASTER_RELIEF_MIN_LOSS = 10000.0      # R&TC 170(b): relief only if the loss is $10,000 or more


def compute_disaster_relief_reassessment(purchase_price: float, purchase_year: int,
                                          value_before: float, value_after: float,
                                          damage_lien_year: int,
                                          target_lien_year: int = DEFAULT_LIEN_YEAR,
                                          pct_restored: float = 0.0,
                                          homeowners_exemption: bool = False,
                                          total_rate: float = PROP13_BASE_RATE,
                                          damage_month: int = None):
    """R&TC Sec. 170 disaster-relief reassessment (read directly from
    leginfo, not a secondary source), for a county whose board of
    supervisors has adopted a Sec. 170 ordinance (BOE's disaster-relief page
    says all counties now have one; an ordinance may still be limited to
    Governor-proclaimed disasters). Taxable value on a LATER lien date:

      roll_value      = min(factored base year value on the roll in effect
                            when the damage occurred, value_before)
      pct_reduction   = (value_before - value_after) / value_before      [170(b)]
      damaged_value   = roll_value * (1 - pct_reduction)
      damaged_at_L    = damaged_value * 1.02 ** (L - damage_lien_year)   [170(g)]
      fbyv_at_L       = purchase_price * 1.02 ** (L - purchase_year)
      taxable_at_L    = damaged_at_L + (fbyv_at_L - damaged_at_L) * pct_restored
                                                                 [170(g) 2nd para]

    value_before/value_after are the assessor's full cash values immediately
    before/after the damage (the caller trusts the stated figures, same as
    every other compute function here). damage_lien_year is the lien year of
    the roll in effect on the damage date (damage in Jul-Dec = that calendar
    year; Jan-Jun = the prior year). The 2% inflation factor is the cap, as
    in compute_property_tax_estimate: a disclosed overestimate.

    total_rate (default the 1% base) is applied to both the relieved and the
    undamaged taxable value; the caller passes the exact-TRA or county-
    average combined rate from compute_local_rate when it has one.

    damage_month (1-12, optional) enables the Sec. 170(e) damage-year
    proration. The fiscal year is July-June. The tax for the fiscal year of
    the calamity (the one whose lien year is damage_lien_year) is the sum of
    (1) the tax on the roll value prorated by the months of that fiscal year
    BEFORE the month of the calamity and (2) the tax on the reduced value
    prorated by the months FROM the month of the calamity through June
    (the calamity month counts on the reduced side -- BOE: "from the first
    of the month in which the damage occurred"). Both sides use the rate in
    effect (total_rate), per 170(e). The result's "damage_year" holds that
    figure; and with damage_month, target_lien_year == damage_lien_year is
    allowed and returns that prorated tax as "tax" (the year-of-calamity
    case). A restoration percentage in the year of calamity is refused (None):
    the statute prorates that year around the completion date, which this
    function is not given. For completion in a LATER year, see
    compute_disaster_completion_assessments (170(h)).

    NOT modeled: land/improvement/personal property separate percentages
    (one blended percentage is used), the Sec. 70(c) new-construction
    exclusion, and the 5-year (8 for LA/Ventura 2025 fires) rebuild-window.

    Returns {"eligible": False, ...} when the loss is under $10,000; None for
    unusable inputs."""
    if not purchase_price or purchase_price <= 0 or purchase_year is None:
        return None
    if value_before is None or value_before <= 0 or value_after is None or not 0 <= value_after <= value_before:
        return None
    if damage_lien_year is None or damage_lien_year < purchase_year:
        return None
    if target_lien_year is None:
        return None
    year_of_calamity = (damage_month is not None and target_lien_year == damage_lien_year)
    if target_lien_year <= damage_lien_year and not year_of_calamity:
        return None
    if damage_month is not None and not 1 <= damage_month <= 12:
        return None
    if not 0.0 <= pct_restored <= 1.0:
        return None
    if year_of_calamity and pct_restored > 0:
        return None
    loss = value_before - value_after
    out = {"purchase_price": purchase_price, "purchase_year": purchase_year,
           "value_before": value_before, "value_after": value_after, "loss": round(loss, 2),
           "damage_lien_year": damage_lien_year, "target_lien_year": target_lien_year,
           "pct_restored": pct_restored, "citation": DISASTER_RELIEF_CITATION,
           "source_url": DISASTER_RELIEF_SOURCE_URL}
    fbyv_at_target = purchase_price * (1 + PROP13_INFLATION_CAP_RATE) ** (target_lien_year - purchase_year)
    exemption_applied = HOMEOWNERS_EXEMPTION_AMOUNT if homeowners_exemption else 0.0
    out["homeowners_exemption"] = homeowners_exemption
    out["exemption_applied"] = exemption_applied
    out["factored_base_year_value"] = round(fbyv_at_target, 2)
    if loss < DISASTER_RELIEF_MIN_LOSS:
        taxable = fbyv_at_target
        out.update(eligible=False, pct_reduction=0.0, damaged_value=None,
                   taxable_value=round(taxable, 2))
    else:
        fbyv_at_damage = purchase_price * (1 + PROP13_INFLATION_CAP_RATE) ** (damage_lien_year - purchase_year)
        roll_value = min(fbyv_at_damage, value_before)
        pct_reduction = loss / value_before
        damaged_value = roll_value * (1 - pct_reduction)
        damaged_at_target = damaged_value * (1 + PROP13_INFLATION_CAP_RATE) ** (target_lien_year - damage_lien_year)
        taxable = damaged_at_target + max(0.0, fbyv_at_target - damaged_at_target) * pct_restored
        out.update(eligible=True, roll_value=round(roll_value, 2), pct_reduction=round(pct_reduction, 6),
                   damaged_value=round(damaged_value, 2),
                   damaged_value_at_target=round(damaged_at_target, 2), taxable_value=round(taxable, 2))
    assessed = max(0.0, taxable - exemption_applied)
    out["assessed_value"] = round(assessed, 2)
    out["total_rate"] = total_rate
    out["tax"] = round(total_rate * assessed, 2)
    out["undamaged_tax"] = round(total_rate * max(0.0, fbyv_at_target - exemption_applied), 2)
    out["year_of_calamity"] = year_of_calamity
    out["damage_year"] = None
    if damage_month is not None and out["eligible"]:
        months_before = damage_month - 7 if damage_month >= 7 else damage_month + 5
        months_after = 12 - months_before
        v_before = max(0.0, out["roll_value"] - exemption_applied)
        v_after = max(0.0, out["damaged_value"] - exemption_applied)
        dy_tax = total_rate * (v_before * months_before / 12 + v_after * months_after / 12)
        dy_undamaged = total_rate * v_before
        out["damage_year"] = {
            "lien_year": damage_lien_year,
            "fiscal_year_label": f"{damage_lien_year}-{str(damage_lien_year + 1)[-2:]}",
            "months_before": months_before, "months_after": months_after,
            "tax": round(dy_tax, 2), "undamaged_tax": round(dy_undamaged, 2),
            "relief": round(dy_undamaged - dy_tax, 2),
        }
        if year_of_calamity:
            out["taxable_value"] = out["damaged_value"]
            out["assessed_value"] = round(v_after, 2)
            out["tax"] = out["damage_year"]["tax"]
            out["undamaged_tax"] = out["damage_year"]["undamaged_tax"]
    return out


def compute_disaster_completion_assessments(purchase_price: float, purchase_year: int,
                                             value_before: float, value_after: float,
                                             damage_lien_year: int,
                                             completion_month: int, completion_year: int,
                                             total_rate: float = PROP13_BASE_RATE):
    """R&TC Sec. 170(h) additional assessments when damaged property is
    fully restored, repaired or reconstructed (read from a statute summary
    cross-checked against BOE's page, NOT the verbatim leginfo text, which
    this environment could not fetch):

      new_taxable_value(L) = the lesser of full cash value or factored base
                             year value on the lien date L -- the model uses
                             the factored base year value (purchase_price *
                             1.02**(L - purchase_year)), i.e. it assumes the
                             restored full cash value is not below it
      taxable_on_roll(L)   = damaged value compounded at 2%/year to L
                             ((g)); NO partial-restoration step-ups before
                             completion are assumed
      completed Jan 1 - May 31 (month 1-5) -> TWO additional assessments:
          #1 = new_taxable_value(Lc)   - taxable_on_roll(Lc)    [current roll]
          #2 = new_taxable_value(Lc+1) - taxable_on_roll(Lc+1)  [roll being prepared]
      completed Jun 1 - Dec 31 (month 6-12) -> ONE:  #1 only
      where Lc is the lien year of the roll in effect at completion (month
      7-12 = completion_year, month 1-6 = completion_year - 1).

    Returns the VALUE added by each assessment and the tax on it for a FULL
    fiscal year ("annual_tax_equivalent" = total_rate * value). The statute
    summary does not state that the additional assessments are prorated by
    months (R&TC 75.41 appears only in 170(e)) and I could not verify how the
    assessor bills them, so no proration is applied: the caller must
    disclose that the actual bill may be a prorated part of that figure.
    The Homeowners' Exemption nets out of both sides of each difference and
    is not modeled here.

    Returns {"eligible": False, ...} when the loss is under $10,000 (no
    relief was ever granted, so nothing to add back); None for unusable
    inputs or a completion before the damage."""
    if not purchase_price or purchase_price <= 0 or purchase_year is None:
        return None
    if value_before is None or value_before <= 0 or value_after is None or not 0 <= value_after <= value_before:
        return None
    if damage_lien_year is None or damage_lien_year < purchase_year:
        return None
    if completion_month is None or not 1 <= completion_month <= 12 or completion_year is None:
        return None
    lc = completion_year if completion_month >= 7 else completion_year - 1
    if lc < damage_lien_year:
        return None
    loss = value_before - value_after
    out = {"loss": round(loss, 2), "completion_month": completion_month,
           "completion_year": completion_year, "current_roll_lien_year": lc,
           "total_rate": total_rate, "citation": DISASTER_RELIEF_CITATION,
           "source_url": DISASTER_RELIEF_SOURCE_URL}
    if loss < DISASTER_RELIEF_MIN_LOSS:
        out.update(eligible=False, assessments=[])
        return out
    fbyv_at_damage = purchase_price * (1 + PROP13_INFLATION_CAP_RATE) ** (damage_lien_year - purchase_year)
    roll_value = min(fbyv_at_damage, value_before)
    damaged_value = roll_value * (1 - loss / value_before)
    rolls = [lc] + ([lc + 1] if completion_month <= 5 else [])
    assessments = []
    for i, lien in enumerate(rolls):
        new_taxable = purchase_price * (1 + PROP13_INFLATION_CAP_RATE) ** (lien - purchase_year)
        on_roll = damaged_value * (1 + PROP13_INFLATION_CAP_RATE) ** (lien - damage_lien_year)
        added = max(0.0, new_taxable - on_roll)
        assessments.append({
            "number": i + 1, "against": "current roll" if i == 0 else "roll being prepared",
            "lien_year": lien, "new_taxable_value": round(new_taxable, 2),
            "taxable_on_roll": round(on_roll, 2), "added_value": round(added, 2),
            "annual_tax_equivalent": round(total_rate * added, 2)})
    out.update(eligible=True, assessments=assessments,
               enrolled_next_lien_value=assessments[-1]["new_taxable_value"])
    return out


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
                                       replacement_timing: str = "within_1_year",
                                       total_rate: float = PROP13_BASE_RATE):
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
    tax = round(total_rate * new_taxable_value, 2)
    return {"eligible": True, "total_rate": total_rate, "claimant_type": claimant_type, "transfer_number": transfer_number,
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


def compute_parent_child_exclusion_value(old_fbyv: float, fmv: float, pct_interest: float = 1.0,
                                         total_rate: float = PROP13_BASE_RATE):
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
    tax = round(total_rate * transferred_share_value, 2)
    return {
        "total_rate": total_rate, "old_fbyv": old_fbyv, "fmv": fmv, "pct_interest": pct_interest,
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
                                     ownership_pct: float = 1.0,
                                     total_rate: float = PROP13_BASE_RATE):
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

    Tax rate is total_rate for both supplementals -- R&TC 75.41(a): the SAME
    total ad valorem rate that would otherwise apply, no special
    supplemental rate exists. It defaults to the 1% constitutional base; the
    caller passes the exact-TRA or county-average rate from
    compute_local_rate when it has one, and its answer text must disclose
    which rate was used.

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
    supplemental_1_tax = round(total_rate * supplemental_1, 2)

    supplemental_2 = None
    supplemental_2_tax = None
    partial_interest_second_supplemental_note = None
    supplemental_count = 1
    if bimodal:
        if ownership_pct == 1.0:
            supplemental_2 = round(added_value, 2)
            supplemental_2_tax = round(total_rate * supplemental_2, 2)
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
        "total_rate": total_rate, "event_month": event_month, "event_year": event_year,
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


# All 58 real CA counties -- deliberately includes San Benito and Plumas
# (which have NO county_override_rates row, confirmed SCO data errors, see
# load_county_override_rates.py's own docstring) so they're RECOGNIZED as
# valid counties by any caller checking membership -- a lookup for either
# correctly falls through to "no data" rather than the county not being
# recognized as a county at all.
CA_COUNTIES = frozenset({
    "Alameda", "Alpine", "Amador", "Butte", "Calaveras", "Colusa",
    "Contra Costa", "Del Norte", "El Dorado", "Fresno", "Glenn",
    "Humboldt", "Imperial", "Inyo", "Kern", "Kings", "Lake", "Lassen",
    "Los Angeles", "Madera", "Marin", "Mariposa", "Mendocino", "Merced",
    "Modoc", "Mono", "Monterey", "Napa", "Nevada", "Orange", "Placer",
    "Plumas", "Riverside", "Sacramento", "San Benito", "San Bernardino",
    "San Diego", "San Francisco", "San Joaquin", "San Luis Obispo",
    "San Mateo", "Santa Barbara", "Santa Clara", "Santa Cruz", "Shasta",
    "Sierra", "Siskiyou", "Solano", "Sonoma", "Stanislaus", "Sutter",
    "Tehama", "Trinity", "Tulare", "Tuolumne", "Ventura", "Yolo", "Yuba",
})

COUNTY_OVERRIDE_RATE_CITATION = ('California State Controller\'s Office, "CA Property Tax Data" '
                                  'portal, FY2025-26 Allocations + Levies by county')
COUNTY_OVERRIDE_RATE_SOURCE_URL = "https://propertytax.bythenumbers.sco.ca.gov/"


def compute_county_override_rate(conn, county: str, tax_year: int = DEFAULT_LIEN_YEAR):
    """Looks up county_override_rates for `county`, using the MOST RECENT
    vintage AT OR BEFORE tax_year -- deliberately NOT an exact tax_year
    match the way compute_disabled_veterans_exemption_ca's WHERE tax_year=
    %s is. DV-exemption rows are published by BOE a YEAR IN ADVANCE; this
    data is the opposite -- county bond resolutions are adopted Aug-Sept
    and the SCO portal aggregates afterward, so it's naturally a vintage
    BEHIND DEFAULT_LIEN_YEAR. An exact match would silently return None
    for the entire current year every time DEFAULT_LIEN_YEAR is bumped,
    since a same-year row won't exist yet -- county override rates change
    gradually (bonds amortize down, new ones occasionally added), so using
    the most recent REAL county-specific rate as this year's estimate is a
    much smaller approximation than the core estimate's own 2%/year-cap
    overestimate already is.

    Returns None if county isn't a recognized CA county (not in
    CA_COUNTIES) or has no row at or before tax_year -- San Benito and
    Plumas are recognized counties with NO row (confirmed SCO data
    errors, deliberately excluded, never a guessed number)."""
    if county not in CA_COUNTIES:
        return None
    r = conn.execute(
        "SELECT tax_year, override_rate, citation, source_url, as_of "
        "FROM county_override_rates WHERE county=%s AND tax_year<=%s "
        "ORDER BY tax_year DESC LIMIT 1", (county, tax_year)).fetchone()
    if not r:
        return None
    data_vintage_year, override_rate, citation, source_url, as_of = r
    return {
        "county": county, "requested_tax_year": tax_year,
        "data_vintage_year": data_vintage_year,
        "data_vintage_fiscal_year_label": f"{data_vintage_year}-{str(data_vintage_year + 1)[-2:]}",
        "year_lag": tax_year - data_vintage_year,
        "override_rate": float(override_rate),
        "citation": citation or COUNTY_OVERRIDE_RATE_CITATION,
        "source_url": source_url or COUNTY_OVERRIDE_RATE_SOURCE_URL,
        "as_of": as_of,
    }


def compute_property_tax_estimate_with_county_rate(conn, purchase_price: float, purchase_year: int,
                                                     county: str,
                                                     current_tax_year: int = DEFAULT_LIEN_YEAR,
                                                     homeowners_exemption: bool = False):
    """Composes compute_property_tax_estimate with compute_county_override_
    rate: total_rate = PROP13_BASE_RATE + override_rate, applied to the SAME
    assessed_value the core estimate computes -- i.e. AFTER the Homeowners'
    Exemption if requested (the exemption reduces the net taxable value the
    FULL combined ad-valorem rate applies to, both the 1% base and voted
    debt overrides, not just the 1% portion -- standard CA property-tax
    mechanics, not independently re-verified this session the way the DV-
    exemption mutual-exclusivity rule was).

    Citation is MERGED (both PROP13_CITATION and the county-rate citation),
    not overwritten -- a deliberate small improvement over compute_
    property_tax_with_dv_exemption's own precedent of dropping the base
    citation, since here two independently-sourced rates are being SUMMED,
    not one figure replacing another.

    KNOWN v1 GAP, not fixed here: does not compose with the Disabled
    Veterans' Exemption -- a DV+county question answers via the unchanged
    compute_property_tax_with_dv_exemption path, without the county
    override rate, the same way Prop 19 and DV aren't composed with each
    other either.

    Returns None if the core estimate fails OR the county has no rate row
    at or before current_tax_year (unrecognized name, or recognized-but-
    excluded like San Benito/Plumas) -- the caller falls back to the plain
    core estimate + generic statewide-average disclosure in that case,
    unchanged from before this feature existed."""
    base = compute_property_tax_estimate(purchase_price, purchase_year, current_tax_year,
                                          homeowners_exemption=homeowners_exemption)
    if not base:
        return None
    rate_info = compute_county_override_rate(conn, county, current_tax_year)
    if not rate_info:
        return None
    total_rate = PROP13_BASE_RATE + rate_info["override_rate"]
    tax = round(total_rate * base["assessed_value"], 2)
    return {**base, "county": county, "override_rate": rate_info["override_rate"],
            "total_rate": total_rate,
            "data_vintage_year": rate_info["data_vintage_year"],
            "data_vintage_fiscal_year_label": rate_info["data_vintage_fiscal_year_label"],
            "year_lag": rate_info["year_lag"], "tax": tax,
            "citation": f"{PROP13_CITATION}; {rate_info['citation']}",
            "source_url": rate_info["source_url"]}


# Counties with exact per-TRA data in tra_rates, and each county's own TRA
# number format as the digit-group widths its rate book uses (TRA numbering
# is only meaningful within one county -- these are NOT interchangeable):
# Kern "001-002" -> (3, 3); Orange "01-227" -> (2, 3); San Diego "03014" ->
# (5,). Must stay in sync with load_tra_rates.py's own registry (the loader
# asserts this). Each county's rows carry their own vintage (tax_year), so
# counties that have published a newer rate book can be refreshed
# independently of the rest.
TRA_FORMATS = {
    "Alameda": (2, 3),
    "Butte": (3, 3),
    "Colusa": (3, 3),
    "Contra Costa": (5,),
    "El Dorado": (3, 3),
    "Fresno": (3, 3),
    "Humboldt": (3, 3),
    "Imperial": (3, 3),
    "Kern": (3, 3),
    "Kings": (3, 3),
    "Lake": (3, 3),
    "Marin": (2, 3),
    "Mariposa": (3, 3),
    "Mono": (3, 3),
    "Napa": (3, 3),
    "Nevada": (3, 3),
    "Orange": (2, 3),
    "Placer": (3, 3),
    "Riverside": (3, 3),
    "Sacramento": (2, 3),
    "San Diego": (5,),
    "San Joaquin": (3, 3),
    "San Luis Obispo": (3, 3),
    "San Mateo": (3, 3),
    "Santa Barbara": (3, 3),
    "Santa Clara": (3, 3),
    "Santa Cruz": (2, 3),
    "Shasta": (3, 3),
    "Sierra": (3, 3),
    "Solano": (6,),
    "Sonoma": (3, 3),
    "Stanislaus": (3, 3),
    "Sutter": (2, 3),
    "Tehama": (3, 3),
    "Tulare": (3, 3),
    "Ventura": (5,),
    "Yolo": (3, 3),
}


def normalize_tra_number(county: str, groups):
    """Canonical zero-padded TRA string for `county` from the digit groups a
    user typed (e.g. Kern ["1", "2"] -> "001-002"; San Diego ["3014"] ->
    "03014"), or None if the county has no TRA data or the typed shape
    doesn't match that county's own format (wrong group count, or a group
    wider than the county's format allows). Users reading their own bill
    may drop leading zeros, so padding is applied, never trusted as typed."""
    fmt = TRA_FORMATS.get(county)
    if fmt is None or len(groups) != len(fmt):
        return None
    parts = []
    for g, width in zip(groups, fmt):
        if not g.isdigit() or len(g.lstrip("0") or "0") > width:
            return None
        parts.append(str(int(g)).zfill(width))
    return "-".join(parts)


def compute_tra_rate(conn, county: str, tra_number: str, tax_year: int = DEFAULT_LIEN_YEAR):
    """Exact per-Tax-Rate-Area (TRA) combined rate lookup for the counties
    in TRA_FORMATS. Mirrors compute_county_override_rate's shape (same
    "most recent tax_year <= requested" lookup, same reasoning -- this data
    is only knowable in arrears relative to DEFAULT_LIEN_YEAR, same as the
    county-average data). tra_number is the canonical zero-padded form
    from normalize_tra_number.

    Unlike compute_county_override_rate, this stores and returns the FULL
    combined rate directly (already 1% base + all overrides, exactly as
    each county's own rate book publishes it) -- not a separately-tracked
    override portion. See extract_kern_tra_rates.py / extract_tra_rates.py
    for how each county's data was extracted and validated.

    Returns None if the county has no rows in this table or the TRA number
    isn't found (unrecognized/mistyped, or a real TRA the extraction
    didn't capture)."""
    r = conn.execute(
        "SELECT tax_year, area_name, total_rate, citation, source_url, as_of "
        "FROM tra_rates WHERE county=%s AND tra_number=%s AND tax_year<=%s "
        "ORDER BY tax_year DESC LIMIT 1", (county, tra_number, tax_year)).fetchone()
    if not r:
        return None
    data_vintage_year, area_name, total_rate, citation, source_url, as_of = r
    return {
        "county": county, "tra_number": tra_number, "area_name": area_name,
        "requested_tax_year": tax_year, "data_vintage_year": data_vintage_year,
        "data_vintage_fiscal_year_label": f"{data_vintage_year}-{str(data_vintage_year + 1)[-2:]}",
        "year_lag": tax_year - data_vintage_year,
        "total_rate": float(total_rate), "citation": citation, "source_url": source_url,
        "as_of": as_of,
    }


def compute_property_tax_estimate_with_tra_rate(conn, purchase_price: float, purchase_year: int,
                                                 county: str, tra_number: str,
                                                 current_tax_year: int = DEFAULT_LIEN_YEAR,
                                                 homeowners_exemption: bool = False):
    """Composes compute_property_tax_estimate with compute_tra_rate --
    mirrors compute_property_tax_estimate_with_county_rate's shape exactly,
    but applies the TRA's own total_rate DIRECTLY (not PROP13_BASE_RATE +
    an override, since this table already stores the full combined figure)
    to the SAME assessed_value the core estimate computes (after the
    Homeowners' Exemption, if requested -- same order-of-operations
    precedent as every other composed function in this module).

    Returns None if the core estimate fails OR the (county, tra_number)
    pair has no row -- the caller falls back to the county-average
    composed estimate in that case, unchanged."""
    base = compute_property_tax_estimate(purchase_price, purchase_year, current_tax_year,
                                          homeowners_exemption=homeowners_exemption)
    if not base:
        return None
    rate_info = compute_tra_rate(conn, county, tra_number, current_tax_year)
    if not rate_info:
        return None
    total_rate = rate_info["total_rate"]
    tax = round(total_rate * base["assessed_value"], 2)
    return {**base, "county": county, "tra_number": tra_number, "area_name": rate_info["area_name"],
            "total_rate": total_rate,
            "data_vintage_year": rate_info["data_vintage_year"],
            "data_vintage_fiscal_year_label": rate_info["data_vintage_fiscal_year_label"],
            "year_lag": rate_info["year_lag"], "tax": tax,
            "citation": rate_info["citation"], "source_url": rate_info["source_url"]}


def compute_local_rate(conn, county, tra_number=None, tax_year: int = DEFAULT_LIEN_YEAR):
    """Best available combined ad-valorem rate for a stated location, in the
    same precedence the plain estimate uses: the EXACT TRA rate if the
    (county, TRA) pair has a row, else the county-average (1% + override),
    else None (caller keeps the 1% base and discloses it). Returns
    {"basis": "tra"|"county_average", "total_rate", "county", "tra_number",
    "area_name", "data_vintage_fiscal_year_label", "year_lag", "citation",
    "source_url"}. Used to compose Prop 8 and Sec. 170 with local rates."""
    if county is None:
        return None
    if tra_number:
        t = compute_tra_rate(conn, county, tra_number, tax_year)
        if t:
            return {"basis": "tra", "total_rate": t["total_rate"], "county": county,
                    "tra_number": tra_number, "area_name": t["area_name"],
                    "data_vintage_fiscal_year_label": t["data_vintage_fiscal_year_label"],
                    "year_lag": t["year_lag"], "citation": t["citation"], "source_url": t["source_url"]}
    c = compute_county_override_rate(conn, county, tax_year)
    if c:
        return {"basis": "county_average", "total_rate": PROP13_BASE_RATE + c["override_rate"],
                "county": county, "tra_number": None, "area_name": None,
                "data_vintage_fiscal_year_label": c["data_vintage_fiscal_year_label"],
                "year_lag": c["year_lag"], "citation": c["citation"], "source_url": c["source_url"]}
    return None
