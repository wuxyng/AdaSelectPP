# AdaSelectPP Core v2

One online policy: bounded `probe_grow_fair` candidates, full-window ADD observations for new width-2 indexes, adaptive benefit history, a single QCP budget, and the original conservative selector.

## Policy inputs

`PolicyConfig(max_num, qcp_budget, alpha, beta)` has exactly four required inputs. QCP is an explicit nonnegative integer and counts actual optimizer EXPLAIN attempts, including roots and failures. It is not a candidate quota. Versioned algorithm constants remain in [adasel/policy.py](adasel/policy.py); this is not a parameter-free method.

```python
from adasel.ada_select import AdaSelect
from adasel.policy import PolicyConfig, RuntimeConfig

tuner = AdaSelect(benchmark, verified_backend,
                  PolicyConfig(max_num=10, qcp_budget=26, alpha=.55, beta=1.1),
                  runtime=RuntimeConfig(timeout_ms=30000))
target = tuner.run(window, current, runtimes, window_id=physical_window_id,
                   qcp_budget=registered_caps[physical_window_id])
```

The verified release driver owns the database connections, root checks and physical DDL. Do not substitute the historical unsafe connector. Only the registered isolated workstation is authorized by the release contract.

## Install and test

Python 3.10+, `pip install -r requirements-core-test.txt`, then `python -m pytest`.
The policy and pure tests also run with only sqlglot and pytest installed; psycopg2 is required for the real database driver. No numpy, torch, TCNN or action policy is needed.

Measured-response regression:

```text
python experiments/core_v2/replay.py --diagnostic DIAGNOSTIC_FIXTURES --package WIDTH2_HANDOFF --output REPLAY_OUTPUT
```

## Registered release

After a clean revision is frozen, offline registration verifies the original profiles, traces and snapshot references:

```text
python experiments/core_v2/prepare.py --previous PRIOR_REGISTRATION --diagnostic ACCEPTED_DIAGNOSTIC --output NEW_RELEASE_DIRECTORY
python -m adasel.main --output NEW_RELEASE_DIRECTORY campaign
python experiments/core_v2/report.py --output NEW_RELEASE_DIRECTORY
```

Exactly nine workloads / 603 windows, one version and one repeat, cap 22,271 QCP. Preflight is separate and limited to 30 EXPLAINs. Campaign wall limit is 7,200 seconds plus at most 900 seconds for cleanup. The first workload is the wiring check; there is no intermediate matrix or additional smoke matrix. Completion or safety failure stops the campaign; there is no automatic restart.

See [design](docs/CORE_DESIGN.md), [limitations](docs/LIMITATIONS.md), [archive/provenance](docs/ARCHIVE_INDEX.md), and the [individual historical test migration](docs/CORE_TEST_MIGRATION.md).
