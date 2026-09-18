# Limits of Core v2

- Candidate supply is heuristic and bounded. Removing an evaluation quota does not enumerate every index or recover candidates pruned by generation.
- FULL_WINDOW_ADD means complete current-window optimizer-cost observations for one new width-2 ADD against its current root. It does not establish wall-clock benefit, interaction-free attribution, or future benefit.
- Width-1 ADD and every KEEP remain LEGACY_MASK_APPROX, including original fallback behavior. They are not relabeled full. Hypothetical C+u may temporarily exceed installed capacity; actual recommendations remain bounded by max_num.
- Mu is historical adaptive smoothing. Top-K plus symmetric beta does not guarantee the best configuration, swap utility, payback, or convergence. No exploration, SWAP or payback mechanism is enabled.
- D02 (removed diagnostics), D07 (removed count gate), and D08 (first-round cap) can change later histories and configurations. A single new trajectory cannot identify their separate performance contributions.
- The 490 ledger regression covers fixed observed states. New candidates/configurations in a closed loop require real measurements; missing historical responses are never synthesized.
- The nine-workload release is one version and one repeat. It is screening/operational acceptance only, with no demonstrated statistical significance, automatic speedup claim, or inherited 2.71x result.
- Safety/catalog/scope overhead remains included in recommendation time. The two original ownership/recovery receipts remain alongside minimal policy output to preserve recovery behavior.
- Completion stops work. READY is not production-merge authorization. Failure, deadline or unverifiable source identity must be reported as BLOCKED with completed/incomplete scope.
