# Adaptive Smoothing Restored

This package keeps the bounded prefix-growth candidate generator, but restores AdaSelect's core adaptive benefit smoothing.

Restored behavior:

- `_calculate_adaptive_lambda()` implements RSFE/MAD tracking-signal smoothing.
- `_choose_lambda()` supports `adaptive` and `fixed` modes.
- In fixed mode, adaptive lambda is still computed as `idx_alphas_shadow` for diagnostics.
- NO_HIT / ALL_FALLBACK observations are gated and do not poison RSFE/MAD.
- Unseen-index decay uses per-index adaptive lambda in adaptive mode, `benefit_decay_fixed` in fixed mode, or explicit `benefit_decay` if set.
- Timeout reset clears adaptive smoothing state.

This restores AdaSelect's benefit-update core without reintroducing legacy candidate generation, G0 merge, retain/swap, or compile hard gate.
