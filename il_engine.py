"""Illinois answers (Illinois Department of Revenue, income tax first).

Skeleton for the multi-state refactor: no Illinois rules are loaded yet, so
every question gets an explicit "not built yet" answer rather than anything
resembling a tax figure. Rules, engine and sweep arrive in the next step.
"""
import states


def answer_state(question, tax_type=None):
    return states._needs_review(
        "IL",
        "Illinois income tax rules are not loaded yet, so I can't answer this reliably. "
        "Illinois support is under construction.",
        "state_not_built")
