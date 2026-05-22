# Phase 1 grouped analysis by workload semantics
This note re-reads the existing Phase 1 results by workload semantics rather than only full-run averages.
## 1. Shifting = phase transition
- For `tpchs_shifting`, the final `new` configuration is identical between `curr_only` and `mainline` on **all 80 rounds**.
- The most important feature is the **boundary shock** at rounds 20, 40, and 60. Around round 20, `exec_avg` jumps from about 863 to 3501 in `curr_only`, and from about 998 to 3618 in `mainline`; this is the true cost of the phase switch, not a keep-lane effect.
- `selected_keep_avg` in `mainline` is near 0 in phase 0, then rises to ~2 around the second phase, ~5 around the third phase, and ~6 by the last phase, showing that the keep lane becomes more active as incumbents accumulate, but it still does **not** alter the chosen `new` configuration.
- Interpretation: on shifting, Phase 1 currently behaves as a **continuity lane**, not a configuration-changing lane.
## 2. Noisy = structured intrusion
- For `tpchs_noisy`, the final `new` configuration is again identical between variants on **all 96 rounds**.
- The noisy windows (20-23, 44-47, 68-71, 92-95) show exactly what the generator intended: short bursts of another group. In `mainline`, `selected_keep_avg` spikes strongly in these windows (e.g. ~5.2 in `noisyA`, ~4.8 in `noisyB`), while `curr_only` stays at 0.
- However, `candidate_count` and `what_if_calls` remain essentially the same, and because `new` is identical, the keep lane is **absorbing intrusion at the visibility/evaluation level**, not changing the actual physical design decision.
- Interpretation: on noisy, Phase 1 is acting as a **state-stability fuse**, not yet as a stronger decision policy.
## 3. Random = mixed stream
- `tpchs_random` must not be interpreted like a phase workload. After the global shuffle, each 21-query round contains only about **10-15 distinct templates**, not 21.
- On this workload, `mainline` still leaves the final `new` configuration identical on **all 25 rounds**.
- What changes is the evaluation path: `selected_keep` averages about 1-2 per round in `mainline`, and `candidate_count / evaluated_count / what_if_calls` all drop slightly relative to `curr_only`.
- This means Phase 1 is helping more on the **action visibility / evaluation pressure** side than on the actual chosen configuration.
## 4. One real positive signal
- The only workload where Phase 1 changes the final configuration is `tpch_random` (6 late rounds differ). In those rounds, `mainline` replaces `orders(o_custkey)` with `lineitem(l_receiptdate)` and gets a small but real reduction in execution and total time.
- So the architecture is connected correctly; it is just that on the skewed `tpchs` workloads, the effect size is not yet large enough to move the final decision path.
## Bottom line
Phase 1 is no longer a fake feature: `U_keep` is active. But on `tpchs`, it currently changes **visibility and continuity**, not **final configuration**. The grouped view shows why averages alone were misleading: shifting measures phase-boundary shock, noisy measures intrusion buffering, and random measures mixed-stream evaluation pressure.
