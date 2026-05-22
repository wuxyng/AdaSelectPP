# Adaptive Smoothing Restoration Notes

This package restores AdaSelect's adaptive benefit smoothing while keeping the new bounded-prefix candidate generator.

Restored core logic:

- per-index RSFE / MAD tracking (`idx_error_smooth`, `idx_abs_error_smooth`)
- Trigg-style tracking signal to choose per-index EWMA lambda
- signal gating for `NO_HIT` / all-fallback observations
- fixed-lambda mode with shadow adaptive lambda diagnostics
- adaptive unseen-benefit decay using per-index lambda
- timeout reset clears adaptive state

This is intentionally separate from candidate generation.  The candidate generator remains `MCIGCandidateGenerator`; no CooccurrenceEnumerator, G0 merge, retain/swap, compile hard gate, or plan-cache path is restored.
