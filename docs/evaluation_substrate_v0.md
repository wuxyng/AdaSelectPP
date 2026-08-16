# Evaluation Substrate v0

## Scope and measurement objective

Evaluation Substrate v0 is an offline measurement layer for experiments about
limited optimizer interaction. Its only measured objective is PostgreSQL's
optimizer-estimated workload cost under explicitly supplied hypothetical index
configurations.

For a workload containing query occurrences \(q_1, \ldots, q_n\), the substrate
may report the additive proxy

```text
estimated_workload_cost(C) = sum(reveal(q_i, C).optimizer_cost)
```

Duplicates are occurrences and must remain duplicated in this sum. An optimizer
estimate is not execution latency, DML overhead, index-build cost, storage cost,
or transition cost. Results from this substrate must not be presented as any of
those quantities.

The candidate universe and every configuration to be measured are inputs. The
substrate does not generate candidates, rank them, choose configurations, or
define a budget-allocation or replay policy.

## Three identities that must remain separate

Each `QuerySpec` is one workload occurrence with:

```text
occurrence_id
exact_sql_hash
optional template_id
```

`exact_sql_hash` is SHA-256 over the exact UTF-8 SQL text retained by the
object, including literal values, predicate values, and leading or trailing
whitespace. The exact optimizer-response identity is:

```text
(exact_sql_hash, configuration_id, epoch_hash)
```

Neither `occurrence_id` nor `template_id` enters that key. Two occurrence IDs
with byte-identical SQL can therefore reuse one evaluator response, while both
occurrences remain present when an additive workload objective is calculated.
Rebinding an occurrence ID to different exact SQL or conflicting template
metadata fails closed.

`template_id` is optional analysis metadata only. Queries with the same
template but different literal values have different exact SQL hashes and do
not share exact responses. Changing only template metadata does not change the
response key. Template history is not exact evidence and cannot create a cache
hit, fill missing ground truth, or authorize an approximation.

`query_id` is retained temporarily as a source-compatibility alias for
`occurrence_id`, and `query_hash` as an alias for `exact_sql_hash`. A
`QuerySpec` call supplying both `occurrence_id` and `query_id` is rejected so
the alias cannot hide ambiguous identity.

## Collection interaction unit

One optimizer interaction is one uncached HypoPG-backed optimizer attempt for
one exact `(query, configuration)` pair. On the successful path, the collector
installs and verifies that exact hypothetical configuration and performs
exactly one `EXPLAIN (FORMAT JSON)` operation.

The only public operation returning optimizer cost is
`reveal(run_context, query, configuration)`. The HypoPG session adapter,
uncached measurement path, response store, and service are private
implementation details and are not package exports. A caller cannot construct
a public backend evaluator or request a public force-refresh operation.

Creating, resetting, or checking hypothetical indexes is integrity work; it
does not create a second optimizer response. Every cache miss admitted to the
optimizer backend increments `physical_optimizer_calls` by exactly one,
including an admitted attempt that subsequently fails during HypoPG setup,
verification, or `EXPLAIN`. An evaluator ground-truth hit increments it by
zero. A request rejected before backend admission is not physically charged.

This definition is intentionally different from historical counters that may
count candidate evaluations, supported-query subsets, or batches of optimizer
operations. Those counters must not be substituted for
`physical_optimizer_calls`. The older `charged_optimizer_calls` accessor is a
temporary compatibility alias with this physical-call meaning only; it is
never used for simulated policy evidence.

## Candidate snapshots and configurations

V0 accepts either of these explicit candidate inputs:

```text
candidate_snapshot_tier1.csv
candidate_snapshot_tier2.csv
```

Both files use the same exact header:

```text
candidate_id,table,columns,source,generator_version,snapshot_hash
```

- `candidate_id` is a non-empty stable identifier for the external snapshot.
- `table` is one unquoted, unqualified PostgreSQL base-table identifier. V0
  resolves it explicitly in the `public` schema when installing HypoPG indexes.
- `columns` is a non-empty comma-separated list of unquoted PostgreSQL
  identifiers. Whitespace around each item is removed and column order is
  significant.
- `source` describes the external source of the candidate; it is provenance,
  not an instruction to run a generator.
- `generator_version` records the external generator or export version; the
  substrate never invokes it.
- `snapshot_hash` is a 64-character SHA-256 digest supplied by the external
  snapshot producer. Every row in one snapshot must carry the same digest.

Because v0 supports unquoted identifiers only, `table` and `columns` are
normalized to PostgreSQL's lower-case form. Quoted or qualified names are
rejected instead of guessed.

The manifest separately records a SHA-256 hash of the candidate snapshot
artifact bytes. This avoids treating a provenance field inside the CSV as a
replacement for artifact integrity.

Configurations are also explicit input. Their serialization is a compact JSON
array of normalized `{table, columns}` definitions sorted by table and ordered
column tuple; column order within an index is preserved. The
`configuration_id` is `cfg_` followed by the SHA-256 digest of a canonical JSON
payload containing that sorted array and the serialization version. Equivalent
configurations therefore receive the same ID regardless of input iteration or
mapping order. Duplicate candidates, duplicate index definitions, empty
identifiers, and conflicting definitions are rejected. Configuration
serialization never consults online AdaSelectPP state.

### Metrics lineage boundary

In existing AdaSelectPP metrics:

- `old` is the physical configuration that executed workload window \(W_t\).
- `new` is the recommendation produced after \(W_t\).

They are not interchangeable. The metrics adapter requires callers to pass
`field="old"` explicitly before parsing an executed configuration. Passing
`field="new"`, another field name, an ambiguous configuration field, or no
field is rejected. V0 does not use a post-window recommendation as if it had
executed the window.

## `reveal` API

All experiment-facing optimizer access goes through:

```python
reveal(run_context, query, configuration)
```

For an exact query, exact configuration, and active epoch, a successful reveal
returns only:

- `optimizer_cost`: the finite PostgreSQL plan `Total Cost`;
- `used_indexes`: a deterministically ordered collection of indexes referenced
  by the returned plan;
- `plan_hash`: the hash of a canonical representation of the JSON plan;
- `epoch_hash`: the fingerprint of the environment in which the response was
  measured.

`run_context` is a validated `EvaluationRunContext`; passing a backend, store,
or unbound service is rejected. Every public reveal checks manifest-bound
candidate membership, Tier-1 inventory identity when applicable, and a durable
context-bound determinism authorization before cache or optimizer access.

The API does not return execution milliseconds, DML cost, transition cost, or
materialization cost. It performs no approximation: if an exact response is
missing in replay-only mode, collection is disabled, the epoch differs, the
budget guard is exhausted, or configuration verification fails, the request is
rejected.

## Evaluator ground truth and collection accounting

`optimizer_responses.csv` is evaluator ground truth and collection provenance.
Its reusable response key is exactly `(exact_sql_hash, configuration_id,
epoch_hash)`. Only a successful response measured in the active epoch is
reusable. A row from another epoch is stale ground truth, not a cache hit, and
contradictory successful payloads for one exact key are an integrity failure.

The append-only file has this schema:

```text
occurrence_id
exact_sql_hash
template_id
configuration_id
optimizer_cost
used_indexes
plan_hash
physical_optimizer_call
ground_truth_hit
epoch_hash
status
```

Occurrence and template fields are provenance on the collection event; they do
not enter the response key. For a fresh successful optimizer response,
`physical_optimizer_call=1`, `ground_truth_hit=0`, and `status=OK`. For an
evaluator-internal reuse during collection, `physical_optimizer_call=0` and
`ground_truth_hit=1`; this is not a policy evidence hit. Rejected and failed
collection events use `OPTIMIZER_ERROR`, `MISSING_REJECTED`,
`BUDGET_REJECTED`, `EPOCH_MISMATCH`, or `EPOCH_UNVERIFIABLE` and cannot be read
as scientific responses.

Collection does not accept an epoch-hash callback. One private session object
owns the PostgreSQL connection used for HypoPG reset/install/verification,
`EXPLAIN`, and epoch collection. It captures the epoch before cache access and
again after every optimizer call on that same connection. Both must match the
manifest epoch. Replay-only access may omit a connection, but cannot collect a
missing response.

`physical_optimizer_calls` is reconstructed from every admitted optimizer call
in the ground-truth log. Epoch scope controls reuse eligibility; it does not
erase a physical interaction already spent. Thus an optimizer call followed by
epoch drift remains counted even though its response is unusable. The
implementation flushes each collection event before exposing it as a successful
result so an interrupted collection cannot silently create unrecorded ground
truth.

## Replay evidence sessions and simulated policy probes

Ground truth present on disk is hidden evaluator state. Its presence does not
mean a replay policy has observed it. `open_replay` therefore requires an
`evidence_session_id` and maintains two files separate from the response table:

```text
workload_occurrences.csv:
  occurrence_id,exact_sql_hash,template_id

evidence_events.csv:
  evidence_session_id,occurrence_id,exact_sql_hash,configuration_id,
  epoch_hash,status,charged_policy_probe,evidence_hit
```

For one evidence session, the first successful reveal of an unseen exact
response key records `charged_policy_probe=1,evidence_hit=0`. Later reveals of
that exact key in the same session record `0,1`, even when a different
occurrence ID refers to identical SQL. Another session begins with no such free
evidence. Explicit `seeded_evidence` is the only supported exception; the seed
must resolve to existing exact ground truth and is recorded as `SEEDED` before
it can make a later reveal free.

A missing exact ground-truth row records `MISSING_REJECTED` and one charged
policy probe, but does not enter the session's seen set. It fails closed without
consulting template history, another literal from the same template, another
epoch, or any summary/estimate. Reading a precomputed QCP row is merely an
evaluator implementation detail: the first policy-visible reveal remains
charged. `charged_policy_probes` and `physical_optimizer_calls` are independent
counters and neither is derived from the other.

## Epoch fingerprint

An epoch identifies the captured database and planner state under which
optimizer responses are eligible for reuse. `epoch_hash` is the SHA-256 hash
of a canonical, schema-versioned payload containing:

- PostgreSQL/server and HypoPG versions;
- current database name/OID, current and session users, `search_path`, row
  security, and database collation identity plus actual-version status;
- an explicit list of cost constants, memory/parallel/JIT settings, collapse
  and GEQO controls, constraint/cache settings, parsing/session settings which
  can change constant interpretation, and row security;
- deterministic availability records for version-specific
  `hash_mem_multiplier`, `plan_cache_mode`, and
  `parallel_leader_participation`;
- every available `enable_*` planner switch in name order;
- relevant relation kind, `relpages`, `reltuples`, `relallvisible`, and
  relation options;
- column type/typmod/collation/not-null identity, view/foreign-table identity,
  constraints, partitions, and inheritance relationships;
- ordinary column statistics and extended-statistics definitions/data;
- ordinary physical-index definitions, size estimates, relation options,
  tablespaces, and validity/readiness/liveness state.

All component queries execute inside one repeatable-read, read-only transaction
on the same connection used for optimizer collection. Failure to read a
required component, including extended-statistics data, fails closed.

The default scope is all non-system relations. A narrowed scope is accepted
only as explicit manifest state with workload-coverage attestation, and it must
contain every table in the candidate snapshot. If workload-relation
completeness cannot be established, the all-relations scope is required.
Because Tier 2 accepts future lazy query requests and v0 has no SQL relation
resolver, Tier-2 manifests always use `ALL_NON_SYSTEM_RELATIONS`. A narrowed
scope is therefore limited to a Tier-1 inventory with explicit completeness
attestation.

The equivalence claim is limited to the state explicitly listed above. It does
not claim to hash table contents, operating-system state, arbitrary extension
internals, or every PostgreSQL setting. Workloads whose planning depends on
uncaptured external or extension state require an additional input fingerprint
or are outside v0's safe reuse claim.

Any change to a captured epoch component produces a different hash. Stored responses
from the old hash remain evidence in the log but are ineligible for reuse. V0
never relabels, rescales, or approximates an old-epoch response.

## Run manifest

Each run writes and validates `manifest.json` before collection. A collection
connection cannot be opened without this manifest. It records:

- schema version and run identifier;
- Git commit and dirty status plus a byte-level hash inventory of the offline
  substrate Python modules (including untracked source files);
- SHA-256 hashes of the workload, candidate snapshot, and each supplied
  metrics/trace artifact;
- the expanded session, planner, catalog, statistics, schema, and index epoch;
- the physical-index fingerprint;
- the aggregate `epoch_hash`;
- collection tier, exact relation scope, candidate/workload artifact hashes,
  and the single-writer declaration;
- the finite Tier-2 fresh-call guard, or the canonical Tier-1 inventory.

Tier-1 inventory contains ordered `(occurrence_id, exact_sql_hash, optional
template_id)` occurrences and ordered `(configuration_id,
canonical_configuration)` entries plus its own canonical hash. Repeated exact
SQL remains repeated when occurrence IDs differ. Collection rejects any
supplied inventory which differs. Tier 2 remains lazy and binds each encountered
occurrence before response or evidence access.

Before collection, the run context rehashes every present input artifact and
the substrate source inventory and rejects drift. Production manifest construction reads Git state directly;
test-injected Git state is marked `TEST_INJECTED` and production collection
rejects it. The manifest is provenance, not permission to ignore later drift.

## Determinism gate

Before any public reveal or tier collection, the run context runs:

```python
run_context.validate_determinism(q, C)
```

It executes the same exact `(query, configuration)` three times without
satisfying the repetitions from the response cache. Every repetition must have
an identical finite `optimizer_cost` and identical `plan_hash`, under one
unchanged `epoch_hash`. Configuration installation is verified for each
measurement. A cost mismatch, plan-hash mismatch, epoch drift, optimizer
failure, or installed-configuration mismatch fails closed and prevents bulk
collection.

The collector writes primary evidence to `determinism_report.md` and a derived
`determinism_gate.json` authorization. The authorization is checksummed and
bound to run ID, manifest checksum, epoch, tested occurrence ID/exact SQL hash
and optional template metadata, configuration ID, candidate artifact hash,
exactly three charged uncached physical measurements,
identical costs (by canonical digest), identical plan hash, and the report hash.
Tier collection rereads and verifies both files; an in-memory Boolean cannot
authorize collection, and a gate cannot cross run/manifest/epoch/snapshot
boundaries. The validation operation returns authorization metadata, not an
optimizer cost.

## Mandatory run context and single writer

`EvaluationRunContext` binds the validated manifest, run ID, input artifacts,
candidate snapshot, relation scope, tier provenance, active epoch, response
store, private reveal service, and determinism authorization. Public `reveal()`
accepts no substitute.

V0 is deliberately single-writer. Opening either a collection or evidence
replay context atomically creates `optimizer_responses.writer.lock`. A second
writer is rejected before response/evidence admission. An in-process reentrant
lock serializes epoch check, ground-truth lookup, physical budget decision,
optimizer attempt, response append, and evidence append, so neither a final
physical-call unit nor a first-session reveal can be spent twice.

Normal context closure verifies lock ownership and removes the lock. Abnormal
termination intentionally leaves it behind. Operators must inspect the JSON
record (host, PID, token, and creation time), confirm that no writer exists,
inspect the response log and run outputs, and then explicitly remove the lock.
The implementation never guesses that a lock is stale and never clears one
automatically. Atomic create/exclusive semantics are supported on Windows and
Linux.

## Collection tiers

### Tier 1: exact small universes

Tier 1 accepts an explicit query/occurrence universe, validated Tier-1
candidate snapshot, and configuration list. It first rejects any supplied
configuration containing an index outside that candidate universe, then
collects the exact Cartesian product of the supplied queries and supplied
configurations. "Small" is an experimental choice, not a hard-coded
cardinality assumption. Tier 1 neither constructs additional configurations
nor removes supplied ones.

A Tier-1 table is complete only when every requested tuple has an active-epoch
`OK` response. Missing, rejected, stale, or failed rows make it incomplete and
must be surfaced; they are never filled by interpolation or a nearby
configuration.

### Tier 2: lazy requests

Tier 2 never enumerates the configuration space. It accepts a validated Tier-2
candidate snapshot and checks each requested configuration against it lazily.
It collects only exact `reveal(q, C)` requests issued by an external experiment.
`max_new_optimizer_calls` is a compatibility-named mandatory finite,
non-negative guard on fresh physical optimizer attempts in the ground-truth
log. Evaluator ground-truth hits do not consume the guard. Once the next
physical miss would exceed the guard, that request is rejected before optimizer
access and logged with an explicit budget-rejection status.

The guard is a safety cap, not a budget policy. V0 does not decide which reveal
request should be made next.

## Output layout

Each collection run is isolated under:

```text
runs/evaluation_substrate_v0/<run_id>/
```

The core outputs are:

```text
manifest.json
determinism_report.md
determinism_gate.json
optimizer_responses.csv
workload_occurrences.csv
evidence_events.csv
```

The final two files are created when an evidence replay context is opened. They
must never be inferred from, merged into, or pre-seeded by the ground-truth
response table except through the explicit checked seeding operation.

`optimizer_responses.writer.lock` exists only while a writer is open, except
after abnormal termination where it remains fail-closed for operator review.

Candidate snapshots, configuration inputs, and workload inputs may remain at
their source paths; their exact hashes belong in the manifest. Additional
derived summaries are permitted only when they can be regenerated from the
manifest and response event log. They must not overwrite primary evidence.

## Verification status and deferred items

The focused unit suite uses deterministic fakes for exact-SQL identity,
occurrence multiplicity, template non-identity, evidence-session isolation,
ground-truth hiding, boundary, epoch-component, gate-binding, artifact-drift,
lock, and in-process budget-race behavior. An
opt-in live test is controlled exclusively by
`EVALUATION_SUBSTRATE_TEST_DSN`; without an explicitly supplied scratch DSN it
is reported as SKIPPED. The live test may create and drop one uniquely named
scratch table and verifies real HypoPG installation/cleanup, authoritative
definitions, stable plan/cost normalization, same-session epoch capture, and a
controlled planner-GUC epoch change.

V0 explicitly defers general multi-writer collection, tamper-evident event hash
chains, automatic truncated-log recovery, baseline-only empty candidate
universes, richer Tier-1 partial-completeness diagnostics, and fingerprints for
uncaptured external/extension state. Template-level prediction, smoothing,
generalization, and any conversion of template history into exact evidence are
also out of scope.

## Explicit non-goals

Evaluation Substrate v0 does not:

- implement or alter Model 1;
- generate, expand, prune, or rank candidates;
- select or recommend an index configuration;
- allocate an interaction budget or implement a replay policy;
- predict, smooth, generalize, or approximate optimizer cost at template level;
- change candidate generation, selector, ranking, transition, or any other
  online AdaSelectPP behavior;
- execute workloads for latency measurement;
- estimate DML, storage, index-build, materialization, or transition costs;
- claim that optimizer cost is real-execution performance;
- compare or establish research novelty.

Its purpose is narrower: preserve exact optimizer responses, their interaction
cost, and enough environment provenance to support later deterministic,
fail-closed offline experiments.
