"""Durable execution receipts. No scheduling or policy decisions live here."""
import json
import os
from pathlib import Path

from database.core_backend import Meter
from database.safe_backend import CampaignStop


def write_json(path, value):
    """Readers see either the previous receipt or the complete new receipt."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.pending')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class ReceiptMeter(Meter):
    """An in-flight intent is a charge, not proof of an actual DB request."""
    def __init__(self, deadline, path):
        super().__init__(deadline)
        self.path = path
        self.inflight = None
        self.confirmed = 0
        self.uncertain = 0
        self.save()

    def save(self):
        try:
            write_json(self.path, dict(charged=self.attempts, confirmed=self.confirmed,
                                      uncertain=self.uncertain, inflight=self.inflight,
                                      failures=self.failures, explain_s=self.explain_ms / 1000,
                                      window=self.current_window))
        except Exception as exc:
            raise CampaignStop('QCP_RECEIPT_WRITE_FAILED:' + str(exc)) from exc

    def begin(self):
        # Register uncertainty before the base meter can charge. A signal may
        # arrive between any of its Python statements, including charge/count.
        self.inflight = dict(stage='CHARGING', window=self.current_window)
        before_used, before_attempts = self.budget.used, self.attempts
        try:
            token = super().begin()
        except BaseException:
            self.attempts = before_attempts + (self.budget.used - before_used)
            raise
        self.inflight = token
        self.save()  # durable intent before transport dispatch
        return token

    def end(self, token, start, error=None):
        super().end(token, start, error)
        # A result or server SQLSTATE confirms a response. A local interruption
        # cannot prove whether the request crossed the driver/server boundary.
        if error is None or getattr(error, 'pgcode', None):
            self.confirmed += 1
        else:
            self.uncertain += 1
        self.inflight = None
        self.save()

    @property
    def counts_complete(self):
        return (self.inflight is None and self.uncertain == 0 and
                self.confirmed == self.attempts)


def qcp_receipt(manifest, cap, checkpoint=None):
    """Unknown worker tails are charged the registered cap, never silently zero."""
    checkpoint = checkpoint or {}
    finalized = manifest.get('status') in ('COMPLETE', 'INCOMPLETE')
    known = manifest.get('qcp_used')
    if finalized and known is not None:
        exact = manifest.get('qcp_counters_complete', False)
        return dict(qcp_used=known, qcp_budget_charge=known,
                    qcp_actual_attempts=manifest.get('qcp_actual_attempts', known if exact else None),
                    qcp_counters_complete=exact,
                    qcp_confirmed_lower_bound=manifest.get('qcp_confirmed_lower_bound', known if exact else 0))
    return dict(qcp_used=None, qcp_budget_charge=cap, qcp_actual_attempts=None,
                qcp_counters_complete=False,
                qcp_confirmed_lower_bound=checkpoint.get('confirmed', 0),
                qcp_persisted_charge=checkpoint.get('charged'),
                qcp_inflight=checkpoint.get('inflight'),
                qcp_uncertainty='Worker did not finalize; unrecorded/in-flight requests are unknown. Charge full registered run cap.')
