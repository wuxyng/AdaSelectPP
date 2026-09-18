# Archive and provenance index

Do not run relocated historical scripts in the Core worktree. Reproduce in a separate checkout of `97d925989e53bdeea282be3b7ec66ef6c221f251` using the original paths below. Legacy Phase 0.5 remains `1a1129ad3b2dc512a3951aca9cdada97d966e774`. No reset/clean was used.

| Original path at 97d9259 | Archived copy | Original Git blob SHA-256 |
|---|---|---|
| adasel/config/adaselect.json | experiments/reproduce/legacy_97d9259/adasel/config/adaselect.json | ef9355d9af63fb8f35e246fefd4e689d892cff05140da6a40c76fa686fcd5563 |
| adasel/config/adaselect_clean_spine.json | experiments/reproduce/legacy_97d9259/adasel/config/adaselect_clean_spine.json | b5c28d672fd224991e13298ab1f47d838522f5b4aa9a1b87a5bf7ee180ba36f8 |
| adasel/config_flags.py | experiments/reproduce/legacy_97d9259/adasel/config_flags.py | 83d29e16ab094915866a17207add7bacb0c89321ec288bd7dc8fc54c95499f5e |
| adasel/main.py | experiments/reproduce/legacy_97d9259/adasel/main.py | 8159bed1d857945d3b4a02e64f47ba8cb70e9d6027f26cfb2a96111f607fde3c |
| scripts/adaselect.sh | experiments/reproduce/legacy_97d9259/scripts/adaselect.sh | c30c0c177f9b9c1c872e025782f79220a8d7890bebb17326644db720e6e6dc2e |
| scripts/adaselect_fixed.sh | experiments/reproduce/legacy_97d9259/scripts/adaselect_fixed.sh | 9a4c957e22d189571f02b45ace98cf13068a684135c3d513e4f16e7bf45e244c |
| scripts/sweep_adaselect_lambda_wdcg.sh | experiments/reproduce/legacy_97d9259/scripts/sweep_adaselect_lambda_wdcg.sh | ca992a4cc3212891850cd1b9364acd8c2a0098e9124f422835438ad6899e1a26 |
| tools/width2_mask_campaign.py | experiments/reproduce/legacy_97d9259/tools/width2_mask_campaign.py | 8dd71c915ad9b54a3a49e21a05f9de0c2fe97f76b5d9da8daa2e79e24a5bb4ca |
| tools/width2_mask_replay.py | experiments/reproduce/legacy_97d9259/tools/width2_mask_replay.py | 469f9771c747efa8485dfa9b8bcbafc6db2f8aed22319828df7c3519e0a8a447 |
| tools/width2_mask_report.py | experiments/reproduce/legacy_97d9259/tools/width2_mask_report.py | df238beb3c35622aa5ae018f9a0c6ecf933ed09133a3dc94d7142923b47e2679 |

## Preserved external evidence

- Worktree intake HEAD/status/binary diffs: `E:/AdaSelectPP-core-slimming-v2-results/intake_20260918_123932/`.
- Original dirty workspace: `E:/AdaSelectPP`, branch `codex/action-semantics-v1`, HEAD `f1d552c03260206354c6047eafe6cb1a40744890`; untouched.
- Accepted M2Q checkout: `E:/AdaSelectPP-width2-mask-fix`, HEAD `97d925989e53bdeea282be3b7ec66ef6c221f251`; untouched.
- Accepted remote diagnostic: `/home/lx/experiments/mask_diagnostic_v2_live_20260917_135639`; manifest SHA-256 `580cecc4ce794639a02a0585aae3038898ac8326f6629115be8eb1d388f025b9`.
- Accepted adapter `tool/live_support.py` SHA-256 `78382edcbf93cfa8c110a06253f7f3c604f3da81fd76cfdab08e7f39a023573f`; safety classes/functions copied with AST identity checks (only obsolete NativeDatabase action-config convenience evaluator omitted).
- Transport source files retain their accepted hashes in `core_v2_safety_provenance.json`. The file name `hypopg_action_scope` identifies a safety backend, not an action policy.
- Accepted 490 observations: `E:/AdaSelectPP/runs/width2_mask_fix_v1_input/reference/expected_observations.csv` and `E:/AdaSelectPP/runs/width2_mask_fix_v1_support/fixtures/`; per-checkpoint hashes emitted by the actual new replay.
- Existing MCTS/TaskB/TCNN/research files, historical tests and prior reports remain addressable at their original commits and paths. They are not part of the Core entry/import graph. `pytest.ini` explicitly selects the individually audited `tests/core` suite.
- Input v2 ZIP SHA-256 `bb6b0384b47bcd84c39f4de2cedbb101f4c01b6cc947f4ede1346296b90deea2`; original packet remains outside this worktree.
- No Core v1 worktree/campaign was present at intake; no intermediate dual-budget release was executed.
