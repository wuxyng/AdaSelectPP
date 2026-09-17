"""Measurement outcomes and an optional experimental QCP cutoff (no scheduler)."""

COMPLETE = "COMPLETE"
BUDGET_DEFER = "BUDGET_DEFER"
EVALUATION_FAILED = "EVALUATION_FAILED"
CONTEXT_INVALID = "CONTEXT_INVALID"


class QCPBudget:
    """No cache, reservations, retries or carry. Charge immediately before EXPLAIN."""

    def __init__(self, cap):
        if not isinstance(cap, int) or cap < 0:
            raise ValueError("QCP cap must be a nonnegative integer")
        self.cap = cap
        self.used = 0

    def fits(self, required):
        if not isinstance(required, int) or required < 0:
            raise ValueError("invalid atomic QCP requirement")
        return required <= self.cap - self.used

    def charge(self):
        if not self.fits(1):
            raise AssertionError("QCP accounting error: unfitted request")
        self.used += 1
