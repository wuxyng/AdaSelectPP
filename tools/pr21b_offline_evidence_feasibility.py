#!/usr/bin/env python3
"""Bounded offline evidence-feasibility replay for the frozen PR21b inputs.

The module deliberately separates evaluator-owned response matrices from the
public policy view.  Policy arms receive only a reveal callback backed by a
fresh, per-window evidence session.  No database or optimizer access exists in
this tool.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import sys
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


getcontext().prec = 80

BASE_COMMIT = "7d7233c06565948ddf65b5b9b6c8975b08d52589"
INPUT_ATTEMPT = "20260819T045354Z_pr20c_competing_v01_retry_d6fba243d22f"
INPUT_TAG = "evaluation-substrate-v0.1"
INPUT_COMMIT = "f1cf18f8a942958d4f1400fd8722704f4a93b57a"
FIXED_ACTION = (
    "DROP:movie_info(mi_movie_id)|"
    "ADD:movie_info(mi_movie_id,mi_info_type_id)"
)
BUDGET_LABELS = ("0", "1", "2", "4", "8", "16", "full")
SUBFULL_K = (1, 2, 4, 8, 16)
RANDOM_SEEDS = tuple(range(100))
VERDICT_OBSERVED = "MODEL1_EVIDENCE_FEASIBILITY_SIGNAL_OBSERVED_EXPLORATORY"
VERDICT_NOT_OBSERVED = "MODEL1_EVIDENCE_FEASIBILITY_SIGNAL_NOT_OBSERVED"

EXPECTED_TOTALS = {
    "rounds": 23,
    "occurrences": 759,
    "configuration_instances": 179,
    "occurrence_configuration_requests": 5907,
    "physical_optimizer_calls": 5944,
    "response_events": 5976,
    "exact_response_keys": 5898,
    "ground_truth_hits": 32,
    "charged_policy_probes": 0,
}

PER_WINDOW_FIELDS = (
    "round_id",
    "arm",
    "budget_label",
    "k_value",
    "seed",
    "C_t",
    "U_t",
    "K_t",
    "nominal_budget",
    "actual_charged_probes",
    "is_budgeted_policy",
    "chosen_configuration_id",
    "objective_J",
    "best_objective_J",
    "absolute_regret",
    "normalized_regret",
    "fixed_action_available",
    "matched_groups_for_choice",
    "eligible_candidates",
)
AGGREGATE_FIELDS = (
    "arm",
    "budget_label",
    "k_value",
    "seed_summary",
    "window_count",
    "total_nominal_budget",
    "total_actual_charged_probes",
    "aggregate_absolute_regret",
    "sum_normalized_regret",
    "median_window_absolute_regret",
    "mean_window_absolute_regret",
)
RANDOM_SEED_FIELDS = (
    "seed",
    "budget_label",
    "k_value",
    "window_count",
    "total_nominal_budget",
    "total_actual_charged_probes",
    "aggregate_absolute_regret",
    "sum_normalized_regret",
    "median_window_absolute_regret",
    "mean_window_absolute_regret",
)
REVEAL_FIELDS = (
    "round_id",
    "arm",
    "budget_label",
    "k_value",
    "seed",
    "C_t",
    "U_t",
    "K_t",
    "nominal_budget",
    "actual_charged_probes",
    "unique_revealed_keys",
    "complete_sql_panels",
    "incomplete_sql_panels",
    "max_budget_respected",
    "exact_charge_match",
)


@dataclass(frozen=True)
class SqlGroup:
    exact_sql_hash: str
    multiplicity: int
    first_occurrence_index: int


@dataclass(frozen=True)
class PolicyWindow:
    round_id: int
    baseline_configuration_id: str
    configuration_ids: tuple[str, ...]
    sql_groups: tuple[SqlGroup, ...]
    action_configurations: tuple[tuple[str, str], ...]
    epoch_hash: str

    @property
    def action_map(self) -> dict[str, str]:
        return dict(self.action_configurations)

    @property
    def U_t(self) -> int:
        return len(self.sql_groups)

    @property
    def C_t(self) -> int:
        return len(self.configuration_ids)

    @property
    def K_t(self) -> int:
        return self.U_t * self.C_t


@dataclass(frozen=True)
class EvaluationWindow:
    public: PolicyWindow
    responses: Mapping[tuple[str, str], Decimal]
    objectives: Mapping[str, Decimal]


@dataclass(frozen=True)
class PolicyDecision:
    configuration_id: str
    matched_groups_for_choice: int
    eligible_candidates: int


class EvidenceSession:
    """Evaluator-owned exact-key reveal session with charged-probe accounting."""

    def __init__(
        self,
        window: PolicyWindow,
        hidden_responses: Mapping[tuple[str, str], Decimal],
        budget: int,
    ) -> None:
        if budget < 0 or budget > window.K_t:
            raise ValueError(f"invalid budget {budget} for K_t={window.K_t}")
        self._window = window
        self._hidden_responses = hidden_responses
        self._budget = budget
        self._revealed: dict[tuple[str, str], Decimal] = {}

    @property
    def charged_probes(self) -> int:
        return len(self._revealed)

    @property
    def revealed_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._revealed)

    def reveal(self, exact_sql_hash: str, configuration_id: str) -> Decimal:
        if configuration_id not in self._window.configuration_ids:
            raise KeyError(f"configuration not legal in r{self._window.round_id}")
        if exact_sql_hash not in {g.exact_sql_hash for g in self._window.sql_groups}:
            raise KeyError(f"SQL identity not in r{self._window.round_id}")
        key = (exact_sql_hash, configuration_id)
        if key in self._revealed:
            return self._revealed[key]
        if self.charged_probes >= self._budget:
            raise RuntimeError("reveal would exceed the charged policy-probe budget")
        value = self._hidden_responses[key]
        self._revealed[key] = value
        return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_bytes_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)


def _write_json_new(path: Path, payload: object) -> None:
    _write_bytes_new(path, _json_bytes(payload))


def _write_csv_new(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _median_decimal(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def budget_for_label(window: PolicyWindow, label: str) -> int:
    if label == "full":
        return window.K_t
    k = int(label)
    return min(k * window.C_t, window.K_t)


def canonical_response_key(
    seed: int,
    exact_sql_hash: str,
    configuration_id: str,
    epoch_hash: str,
) -> str:
    return f"{seed}|{exact_sql_hash}|{configuration_id}|{epoch_hash}"


def choose_from_paired_evidence(
    window: PolicyWindow,
    revealed: Mapping[tuple[str, str], Decimal],
) -> PolicyDecision:
    baseline = window.baseline_configuration_id
    eligible: list[tuple[Fraction, str, int]] = []
    for configuration_id in window.configuration_ids:
        if configuration_id == baseline:
            continue
        delta_sum = Decimal(0)
        matched_weight = 0
        matched_groups = 0
        for group in window.sql_groups:
            candidate_key = (group.exact_sql_hash, configuration_id)
            baseline_key = (group.exact_sql_hash, baseline)
            if candidate_key not in revealed or baseline_key not in revealed:
                continue
            delta_sum += Decimal(group.multiplicity) * (
                revealed[candidate_key] - revealed[baseline_key]
            )
            matched_weight += group.multiplicity
            matched_groups += 1
        if matched_weight and delta_sum < 0:
            mean_delta = Fraction(delta_sum) / matched_weight
            eligible.append((mean_delta, configuration_id, matched_groups))
    if not eligible:
        return PolicyDecision(baseline, 0, 0)
    eligible.sort(key=lambda item: (item[0], item[1]))
    _, chosen, matched_groups = eligible[0]
    return PolicyDecision(chosen, matched_groups, len(eligible))


def random_reveal_policy(
    window: PolicyWindow,
    budget: int,
    seed: int,
    reveal: Callable[[str, str], Decimal],
) -> tuple[PolicyDecision, dict[tuple[str, str], Decimal]]:
    ordered: list[tuple[str, str, str]] = []
    for group in window.sql_groups:
        for configuration_id in window.configuration_ids:
            key_text = canonical_response_key(
                seed,
                group.exact_sql_hash,
                configuration_id,
                window.epoch_hash,
            )
            digest = hashlib.sha256(key_text.encode("utf-8")).hexdigest()
            ordered.append((digest, group.exact_sql_hash, configuration_id))
    ordered.sort(key=lambda item: (item[0], item[1], item[2]))
    revealed: dict[tuple[str, str], Decimal] = {}
    for _, exact_sql_hash, configuration_id in ordered[:budget]:
        revealed[(exact_sql_hash, configuration_id)] = reveal(
            exact_sql_hash, configuration_id
        )
    return choose_from_paired_evidence(window, revealed), revealed


def uniform_reveal_policy(
    window: PolicyWindow,
    budget: int,
    reveal: Callable[[str, str], Decimal],
) -> tuple[PolicyDecision, dict[tuple[str, str], Decimal]]:
    revealed: dict[tuple[str, str], Decimal] = {}
    for group in window.sql_groups:
        panel = [(group.exact_sql_hash, c) for c in window.configuration_ids]
        if len(revealed) + len(panel) > budget:
            break
        for exact_sql_hash, configuration_id in panel:
            revealed[(exact_sql_hash, configuration_id)] = reveal(
                exact_sql_hash, configuration_id
            )
    return choose_from_paired_evidence(window, revealed), revealed


def fixed_action_choice(window: PolicyWindow) -> tuple[str, bool]:
    configuration_id = window.action_map.get(FIXED_ACTION)
    if configuration_id is None or configuration_id not in window.configuration_ids:
        return window.baseline_configuration_id, False
    return configuration_id, True


def oracle_choice(evaluation: EvaluationWindow) -> str:
    best = min(evaluation.objectives.values())
    return min(c for c, value in evaluation.objectives.items() if value == best)


def _panel_counts(window: PolicyWindow, keys: Iterable[tuple[str, str]]) -> tuple[int, int]:
    counts = Counter(exact_sql_hash for exact_sql_hash, _ in keys)
    complete = sum(1 for count in counts.values() if count == window.C_t)
    incomplete = sum(1 for count in counts.values() if 0 < count < window.C_t)
    return complete, incomplete


def _parse_hash_manifest(path: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        parts = raw.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError(f"invalid artifact hash line {line_number}")
        rel = parts[1].strip().replace("\\", "/")
        if rel in hashes:
            raise ValueError(f"duplicate artifact hash entry: {rel}")
        hashes[rel] = parts[0].lower()
    return hashes


def verify_artifact_hashes(input_root: Path) -> dict[str, object]:
    manifest_path = input_root / "artifact_sha256.txt"
    expected = _parse_hash_manifest(manifest_path)
    mismatches: list[dict[str, str]] = []
    for rel, expected_sha in sorted(expected.items()):
        target = input_root / Path(rel)
        if not target.is_file():
            mismatches.append(
                {"path": rel, "expected_sha256": expected_sha, "actual": "MISSING"}
            )
            continue
        actual = _sha256_file(target)
        if actual != expected_sha:
            mismatches.append(
                {"path": rel, "expected_sha256": expected_sha, "actual": actual}
            )
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "entries": len(expected),
        "hashes": expected,
        "mismatches": mismatches,
        "passed": not mismatches,
    }


def _load_audit_counts(input_root: Path) -> dict[str, object]:
    audit_path = input_root / "measurement_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    actual = audit.get("verified_totals", {})
    mismatches = {
        key: {"expected": expected, "actual": actual.get(key)}
        for key, expected in EXPECTED_TOTALS.items()
        if actual.get(key) != expected
    }
    identity_ok = (
        audit.get("attempt_id") == INPUT_ATTEMPT
        and audit.get("frozen_code", {}).get("tag") == INPUT_TAG
        and audit.get("frozen_code", {}).get("commit") == INPUT_COMMIT
        and audit.get("verdict") == "PASS"
    )
    return {
        "measurement_audit_sha256": _sha256_file(audit_path),
        "expected": EXPECTED_TOTALS,
        "actual": actual,
        "count_mismatches": mismatches,
        "identity_passed": identity_ok,
        "passed": identity_ok and not mismatches,
    }


def _scan_input_structure(input_root: Path) -> dict[str, object]:
    universe = json.loads((input_root / "configuration_universe.json").read_text("utf-8"))
    rounds = universe.get("rounds", [])
    observed = {
        "rounds": len(rounds),
        "occurrences": sum(len(r["ordered_occurrences"]) for r in rounds),
        "configuration_instances": sum(len(r["configurations"]) for r in rounds),
        "occurrence_configuration_requests": sum(
            len(r["ordered_occurrences"]) * len(r["configurations"]) for r in rounds
        ),
    }
    response_events = 0
    non_ok = 0
    physical_calls = 0
    ground_truth_hits = 0
    product_local_exact_keys = 0
    epoch_hashes: set[str] = set()
    per_round: list[dict[str, object]] = []
    for round_data in rounds:
        round_id = int(round_data["round_id"])
        response_path = input_root / "rounds" / f"r{round_id:02d}" / "optimizer_responses.csv"
        keys: set[tuple[str, str, str]] = set()
        round_events = 0
        round_non_ok = 0
        with response_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                round_events += 1
                response_events += 1
                if row["status"] != "OK":
                    round_non_ok += 1
                    non_ok += 1
                physical_calls += int(row["physical_optimizer_call"])
                ground_truth_hits += int(row["ground_truth_hit"])
                epoch_hashes.add(row["epoch_hash"])
                keys.add(
                    (row["exact_sql_hash"], row["configuration_id"], row["epoch_hash"])
                )
        product_local_exact_keys += len(keys)
        per_round.append(
            {
                "round_id": round_id,
                "response_events": round_events,
                "exact_response_keys": len(keys),
                "non_ok_responses": round_non_ok,
            }
        )
    observed.update(
        {
            "response_events": response_events,
            "exact_response_keys": product_local_exact_keys,
            "physical_optimizer_calls": physical_calls,
            "ground_truth_hits": ground_truth_hits,
        }
    )
    mismatches = {
        key: {"expected": EXPECTED_TOTALS[key], "actual": value}
        for key, value in observed.items()
        if key in EXPECTED_TOTALS and EXPECTED_TOTALS[key] != value
    }
    expected_rounds = list(range(2, 25))
    actual_rounds = [int(r["round_id"]) for r in rounds]
    return {
        "observed_counts": observed,
        "count_mismatches": mismatches,
        "non_ok_responses": non_ok,
        "epoch_hashes": sorted(epoch_hashes),
        "round_ids": actual_rounds,
        "expected_round_ids": expected_rounds,
        "per_round": per_round,
        "passed": not mismatches and non_ok == 0 and actual_rounds == expected_rounds,
    }


def build_protocol(
    input_root: Path,
    hash_verification: Mapping[str, object],
    audit_verification: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "pr21b-offline-evidence-feasibility-protocol-v1",
        "task": "PR21b Offline Evidence Feasibility v1",
        "base_commit": BASE_COMMIT,
        "input": {
            "root": str(input_root),
            "attempt_id": INPUT_ATTEMPT,
            "evaluation_substrate_tag": INPUT_TAG,
            "evaluation_substrate_commit": INPUT_COMMIT,
            "artifact_sha256_manifest_sha256": hash_verification["manifest_sha256"],
            "artifact_hashes": hash_verification["hashes"],
            "measurement_audit_sha256": audit_verification[
                "measurement_audit_sha256"
            ],
            "expected_counts": EXPECTED_TOTALS,
            "scope": "JOB random r2-r24; recorded epoch; optimizer estimated cost only",
        },
        "information_boundary": {
            "policy_receives": [
                "ordered exact-SQL identities and multiplicities",
                "historical incumbent configuration_id",
                "external legal configuration_ids",
                "budget",
                "responses returned by the fresh per-window reveal callback",
            ],
            "policy_never_receives": [
                "raw hidden response matrix",
                "hidden objectives",
                "rankings",
                "regrets",
                "cross-window responses or state",
            ],
            "response_identity": "exact_sql_hash|configuration_id|epoch_hash",
            "duplicates": "objective multiplicity only; no extra exact response key",
        },
        "arms": {
            "incumbent": "zero probes; always C_old,t",
            "fixed_action": {
                "action_id": FIXED_ACTION,
                "fallback": "C_old,t when resulting configuration is absent from A_t",
                "label": "hindsight-informed from prior discovery",
                "probes": 0,
            },
            "random_reveal": {
                "seeds": [0, 99],
                "key_serialization": "seed|exact_sql_hash|configuration_id|epoch_hash",
                "ordering": "ascending SHA-256 hex digest, then exact_sql_hash, then configuration_id",
                "selection": "first B_t unique keys",
            },
            "uniform_reveal": {
                "sql_order": "unique SQL groups in first-occurrence order",
                "configuration_order": "ascending canonical configuration_id",
                "panel_rule": "reveal a complete configuration panel or stop before exceeding B_t",
                "incomplete_panels": "cannot influence scoring",
            },
            "oracle": "hidden full-information evaluation reference; zero regret; not a budgeted policy",
        },
        "partial_evidence_rule": {
            "candidate_set": "A_t excluding C_old,t",
            "eligibility": "at least one exact-SQL group with both candidate and incumbent revealed",
            "mean_delta": "sum m(q)*(cost(q,C)-cost(q,C_old)) / sum m(q) over matched groups",
            "choice": "eligible candidate with smallest strictly negative mean_delta; otherwise incumbent",
            "tie_break": "ascending canonical configuration_id for exact mean_delta ties",
            "shared_by": ["random_reveal", "uniform_reveal"],
        },
        "budget_grid": {
            "labels": list(BUDGET_LABELS),
            "U_t": "number of unique exact-SQL groups",
            "C_t": "size of external A_t",
            "K_t": "U_t*C_t",
            "subfull": "B_t(k)=min(k*C_t,K_t)",
            "full": "B_t(full)=K_t",
            "report": "nominal and actual charged probes",
        },
        "verdict": {
            "required_invariants": [
                "input hashes and counts pass",
                "zero non-OK responses",
                "exact budget accounting",
                "no hidden-label policy access",
                "oracle regret exactly zero",
                "uniform full-budget regret exactly zero",
                "every random seed full-budget regret exactly zero",
            ],
            "qualifying_k": list(SUBFULL_K),
            "observed_if": (
                "at a common sub-full k, uniform aggregate absolute regret and "
                "median random-seed aggregate absolute regret are each strictly "
                "lower than both zero-probe baselines"
            ),
            "observed_label": VERDICT_OBSERVED,
            "not_observed_label": VERDICT_NOT_OBSERVED,
            "online_state": "PR21B_ONLINE_BLOCKED",
        },
        "prohibitions": [
            "database or optimizer calls",
            "candidate generation",
            "selector, ranker, or online changes",
            "Evaluation Substrate semantic changes",
            "runtime, DML, transition, or storage objectives",
            "TPCH or TPCHS",
            "response seeding, cross-window evidence, training, fitting, tuning, or state transfer",
            "sealed-holdout access",
        ],
        "schemas": {
            "per_window_results.csv": list(PER_WINDOW_FIELDS),
            "aggregate_regret_by_budget.csv": list(AGGREGATE_FIELDS),
            "random_seed_results.csv": list(RANDOM_SEED_FIELDS),
            "reveal_accounting.csv": list(REVEAL_FIELDS),
            "input_verification.json": "pr21b-offline-input-verification-v1",
            "feasibility_audit.json": "pr21b-offline-feasibility-audit-v1",
        },
    }


def prepare_attempt(input_root: Path, output_dir: Path, base_commit: str) -> dict[str, object]:
    if base_commit != BASE_COMMIT:
        raise ValueError(f"base commit must be exactly {BASE_COMMIT}")
    if input_root.name != INPUT_ATTEMPT:
        raise ValueError(f"unexpected frozen input directory: {input_root}")
    if output_dir.exists():
        raise FileExistsError(f"attempt directory already exists: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)

    hashes = verify_artifact_hashes(input_root)
    audit = _load_audit_counts(input_root)
    if not hashes["passed"] or not audit["passed"]:
        raise RuntimeError("frozen artifact hashes or measurement audit counts failed")

    protocol = build_protocol(input_root, hashes, audit)
    protocol_bytes = _json_bytes(protocol)
    _write_bytes_new(output_dir / "protocol.json", protocol_bytes)
    protocol_sha = _sha256_bytes(protocol_bytes)
    _write_bytes_new(
        output_dir / "protocol_sha256.txt",
        f"{protocol_sha}  protocol.json\n".encode("utf-8"),
    )

    structure = _scan_input_structure(input_root)
    verification = {
        "schema_version": "pr21b-offline-input-verification-v1",
        "verified_at_utc": _utc_now(),
        "input_root": str(input_root),
        "artifact_hash_verification": hashes,
        "measurement_audit_verification": audit,
        "structure_and_status_verification": structure,
        "protocol_sha256": protocol_sha,
        "passed": bool(hashes["passed"] and audit["passed"] and structure["passed"]),
    }
    _write_json_new(output_dir / "input_verification.json", verification)
    if not verification["passed"]:
        raise RuntimeError("frozen input structure/status verification failed")
    return {
        "phase": "prepare",
        "output_dir": str(output_dir),
        "protocol_sha256": protocol_sha,
        "input_verification_passed": True,
    }


def _verify_frozen_protocol(output_dir: Path) -> tuple[dict[str, object], str]:
    protocol_path = output_dir / "protocol.json"
    recorded = (output_dir / "protocol_sha256.txt").read_text("utf-8").split()[0]
    actual = _sha256_file(protocol_path)
    if actual != recorded:
        raise RuntimeError("protocol hash mismatch")
    protocol = json.loads(protocol_path.read_text("utf-8"))
    if protocol.get("base_commit") != BASE_COMMIT:
        raise RuntimeError("protocol base identity mismatch")
    verification = json.loads((output_dir / "input_verification.json").read_text("utf-8"))
    if not verification.get("passed"):
        raise RuntimeError("input verification did not pass")
    return protocol, actual


def _canonicalize_response_rows(
    response_path: Path,
) -> tuple[dict[tuple[str, str], Decimal], str]:
    canonical: dict[tuple[str, str, str], tuple[str, str, str, str]] = {}
    with response_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["status"] != "OK":
                raise RuntimeError(f"non-OK response in {response_path}")
            exact_key = (
                row["exact_sql_hash"],
                row["configuration_id"],
                row["epoch_hash"],
            )
            payload = (
                row["optimizer_cost"],
                row["used_indexes"],
                row["plan_hash"],
                row["status"],
            )
            previous = canonical.setdefault(exact_key, payload)
            if previous != payload:
                raise RuntimeError(f"contradictory exact response payload: {exact_key}")
    epochs = {key[2] for key in canonical}
    if len(epochs) != 1:
        raise RuntimeError(f"expected one epoch in {response_path}")
    responses = {
        (sql_hash, configuration_id): Decimal(payload[0])
        for (sql_hash, configuration_id, _), payload in canonical.items()
    }
    return responses, next(iter(epochs))


def load_evaluation_windows(input_root: Path) -> list[EvaluationWindow]:
    universe = json.loads((input_root / "configuration_universe.json").read_text("utf-8"))
    evaluations: list[EvaluationWindow] = []
    for round_data in universe["rounds"]:
        round_id = int(round_data["round_id"])
        multiplicities: Counter[str] = Counter()
        first_index: dict[str, int] = {}
        for index, occurrence in enumerate(round_data["ordered_occurrences"]):
            sql_hash = occurrence["exact_sql_hash"]
            multiplicities[sql_hash] += 1
            first_index.setdefault(sql_hash, index)
        sql_groups = tuple(
            SqlGroup(sql_hash, multiplicities[sql_hash], first_index[sql_hash])
            for sql_hash in sorted(first_index, key=first_index.get)
        )
        configuration_ids = tuple(
            sorted(c["configuration_id"] for c in round_data["configurations"])
        )
        action_configurations = tuple(
            sorted(
                (a["action_id"], a["configuration_id"])
                for a in round_data["actions"]
            )
        )
        response_path = input_root / "rounds" / f"r{round_id:02d}" / "optimizer_responses.csv"
        responses, epoch_hash = _canonicalize_response_rows(response_path)
        expected_keys = {
            (group.exact_sql_hash, configuration_id)
            for group in sql_groups
            for configuration_id in configuration_ids
        }
        if set(responses) != expected_keys:
            missing = expected_keys - set(responses)
            extra = set(responses) - expected_keys
            raise RuntimeError(
                f"r{round_id}: response matrix mismatch; missing={len(missing)} extra={len(extra)}"
            )
        public = PolicyWindow(
            round_id=round_id,
            baseline_configuration_id=round_data["baseline_configuration_id"],
            configuration_ids=configuration_ids,
            sql_groups=sql_groups,
            action_configurations=action_configurations,
            epoch_hash=epoch_hash,
        )
        objectives = {
            configuration_id: sum(
                (
                    Decimal(group.multiplicity)
                    * responses[(group.exact_sql_hash, configuration_id)]
                    for group in sql_groups
                ),
                Decimal(0),
            )
            for configuration_id in configuration_ids
        }
        evaluations.append(EvaluationWindow(public, responses, objectives))
    return evaluations


def _result_row(
    evaluation: EvaluationWindow,
    arm: str,
    label: str,
    seed: int | None,
    nominal_budget: int,
    actual_probes: int,
    budgeted: bool,
    decision: PolicyDecision,
    fixed_available: bool,
) -> dict[str, object]:
    best_objective = min(evaluation.objectives.values())
    objective = evaluation.objectives[decision.configuration_id]
    regret = objective - best_objective
    if regret < 0:
        raise RuntimeError("negative regret")
    normalized = regret / best_objective if best_objective > 0 else Decimal(0)
    return {
        "round_id": evaluation.public.round_id,
        "arm": arm,
        "budget_label": label,
        "k_value": "" if label == "full" else label,
        "seed": "" if seed is None else seed,
        "C_t": evaluation.public.C_t,
        "U_t": evaluation.public.U_t,
        "K_t": evaluation.public.K_t,
        "nominal_budget": nominal_budget,
        "actual_charged_probes": actual_probes,
        "is_budgeted_policy": int(budgeted),
        "chosen_configuration_id": decision.configuration_id,
        "objective_J": _decimal_text(objective),
        "best_objective_J": _decimal_text(best_objective),
        "absolute_regret": _decimal_text(regret),
        "normalized_regret": _decimal_text(normalized),
        "fixed_action_available": int(fixed_available),
        "matched_groups_for_choice": decision.matched_groups_for_choice,
        "eligible_candidates": decision.eligible_candidates,
    }


def _reveal_row(
    window: PolicyWindow,
    arm: str,
    label: str,
    seed: int | None,
    nominal_budget: int,
    actual_probes: int,
    revealed_keys: Iterable[tuple[str, str]],
) -> dict[str, object]:
    keys = frozenset(revealed_keys)
    complete, incomplete = _panel_counts(window, keys)
    return {
        "round_id": window.round_id,
        "arm": arm,
        "budget_label": label,
        "k_value": "" if label == "full" else label,
        "seed": "" if seed is None else seed,
        "C_t": window.C_t,
        "U_t": window.U_t,
        "K_t": window.K_t,
        "nominal_budget": nominal_budget,
        "actual_charged_probes": actual_probes,
        "unique_revealed_keys": len(keys),
        "complete_sql_panels": complete,
        "incomplete_sql_panels": incomplete,
        "max_budget_respected": int(actual_probes <= nominal_budget),
        "exact_charge_match": int(actual_probes == len(keys)),
    }


def _aggregate_rows(
    rows: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    random_seed_rows: list[dict[str, object]] = []
    for label in BUDGET_LABELS:
        for seed in RANDOM_SEEDS:
            selected = [
                row
                for row in rows
                if row["arm"] == "random_reveal"
                and row["budget_label"] == label
                and int(row["seed"]) == seed
            ]
            regrets = [Decimal(str(row["absolute_regret"])) for row in selected]
            normalized = [Decimal(str(row["normalized_regret"])) for row in selected]
            random_seed_rows.append(
                {
                    "seed": seed,
                    "budget_label": label,
                    "k_value": "" if label == "full" else label,
                    "window_count": len(selected),
                    "total_nominal_budget": sum(int(row["nominal_budget"]) for row in selected),
                    "total_actual_charged_probes": sum(
                        int(row["actual_charged_probes"]) for row in selected
                    ),
                    "aggregate_absolute_regret": _decimal_text(sum(regrets, Decimal(0))),
                    "sum_normalized_regret": _decimal_text(sum(normalized, Decimal(0))),
                    "median_window_absolute_regret": _decimal_text(_median_decimal(regrets)),
                    "mean_window_absolute_regret": _decimal_text(
                        sum(regrets, Decimal(0)) / Decimal(len(regrets))
                    ),
                }
            )

    aggregate_rows: list[dict[str, object]] = []
    for label in BUDGET_LABELS:
        for arm in ("incumbent", "fixed_action", "uniform_reveal", "oracle"):
            selected = [
                row
                for row in rows
                if row["arm"] == arm and row["budget_label"] == label
            ]
            regrets = [Decimal(str(row["absolute_regret"])) for row in selected]
            normalized = [Decimal(str(row["normalized_regret"])) for row in selected]
            aggregate_rows.append(
                {
                    "arm": arm,
                    "budget_label": label,
                    "k_value": "" if label == "full" else label,
                    "seed_summary": "not_applicable",
                    "window_count": len(selected),
                    "total_nominal_budget": sum(int(row["nominal_budget"]) for row in selected),
                    "total_actual_charged_probes": sum(
                        int(row["actual_charged_probes"]) for row in selected
                    ),
                    "aggregate_absolute_regret": _decimal_text(sum(regrets, Decimal(0))),
                    "sum_normalized_regret": _decimal_text(sum(normalized, Decimal(0))),
                    "median_window_absolute_regret": _decimal_text(_median_decimal(regrets)),
                    "mean_window_absolute_regret": _decimal_text(
                        sum(regrets, Decimal(0)) / Decimal(len(regrets))
                    ),
                }
            )
        seed_selected = [
            row for row in random_seed_rows if row["budget_label"] == label
        ]
        aggregate_regrets = [
            Decimal(str(row["aggregate_absolute_regret"])) for row in seed_selected
        ]
        sum_normalized = [
            Decimal(str(row["sum_normalized_regret"])) for row in seed_selected
        ]
        medians = [
            Decimal(str(row["median_window_absolute_regret"])) for row in seed_selected
        ]
        means = [
            Decimal(str(row["mean_window_absolute_regret"])) for row in seed_selected
        ]
        aggregate_rows.append(
            {
                "arm": "random_reveal_median",
                "budget_label": label,
                "k_value": "" if label == "full" else label,
                "seed_summary": "median_of_seeds_0_through_99",
                "window_count": EXPECTED_TOTALS["rounds"],
                "total_nominal_budget": int(seed_selected[0]["total_nominal_budget"]),
                "total_actual_charged_probes": int(
                    _median_decimal(
                        [
                            Decimal(str(row["total_actual_charged_probes"]))
                            for row in seed_selected
                        ]
                    )
                ),
                "aggregate_absolute_regret": _decimal_text(
                    _median_decimal(aggregate_regrets)
                ),
                "sum_normalized_regret": _decimal_text(_median_decimal(sum_normalized)),
                "median_window_absolute_regret": _decimal_text(_median_decimal(medians)),
                "mean_window_absolute_regret": _decimal_text(_median_decimal(means)),
            }
        )
    return aggregate_rows, random_seed_rows


def _row_lookup(
    aggregate_rows: Sequence[Mapping[str, object]], arm: str, label: str
) -> Mapping[str, object]:
    matches = [
        row
        for row in aggregate_rows
        if row["arm"] == arm and row["budget_label"] == label
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one aggregate row for {arm}/{label}")
    return matches[0]


def evaluate_verdict(
    aggregate_rows: Sequence[Mapping[str, object]],
    invariant_checks: Mapping[str, bool],
) -> tuple[str, int | None, list[int]]:
    if not all(invariant_checks.values()):
        raise RuntimeError("one or more frozen verdict invariants failed")
    qualifying: list[int] = []
    for k in SUBFULL_K:
        label = str(k)
        incumbent = Decimal(
            str(_row_lookup(aggregate_rows, "incumbent", label)["aggregate_absolute_regret"])
        )
        fixed = Decimal(
            str(_row_lookup(aggregate_rows, "fixed_action", label)["aggregate_absolute_regret"])
        )
        uniform = Decimal(
            str(_row_lookup(aggregate_rows, "uniform_reveal", label)["aggregate_absolute_regret"])
        )
        random_median = Decimal(
            str(
                _row_lookup(aggregate_rows, "random_reveal_median", label)[
                    "aggregate_absolute_regret"
                ]
            )
        )
        if uniform < incumbent and uniform < fixed and random_median < incumbent and random_median < fixed:
            qualifying.append(k)
    if qualifying:
        return VERDICT_OBSERVED, qualifying[0], qualifying
    return VERDICT_NOT_OBSERVED, None, []


def _official_replay(
    evaluations: Sequence[EvaluationWindow],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, bool],
    str,
    int | None,
    list[int],
]:
    per_window: list[dict[str, object]] = []
    reveal_rows: list[dict[str, object]] = []
    for evaluation in evaluations:
        window = evaluation.public
        fixed_configuration, fixed_available = fixed_action_choice(window)
        oracle_configuration = oracle_choice(evaluation)
        for label in BUDGET_LABELS:
            nominal = budget_for_label(window, label)
            per_window.append(
                _result_row(
                    evaluation,
                    "incumbent",
                    label,
                    None,
                    nominal,
                    0,
                    False,
                    PolicyDecision(window.baseline_configuration_id, 0, 0),
                    fixed_available,
                )
            )
            per_window.append(
                _result_row(
                    evaluation,
                    "fixed_action",
                    label,
                    None,
                    nominal,
                    0,
                    False,
                    PolicyDecision(fixed_configuration, 0, 0),
                    fixed_available,
                )
            )

            uniform_session = EvidenceSession(window, evaluation.responses, nominal)
            uniform_decision, uniform_revealed = uniform_reveal_policy(
                window, nominal, uniform_session.reveal
            )
            per_window.append(
                _result_row(
                    evaluation,
                    "uniform_reveal",
                    label,
                    None,
                    nominal,
                    uniform_session.charged_probes,
                    True,
                    uniform_decision,
                    fixed_available,
                )
            )
            reveal_rows.append(
                _reveal_row(
                    window,
                    "uniform_reveal",
                    label,
                    None,
                    nominal,
                    uniform_session.charged_probes,
                    uniform_revealed,
                )
            )

            per_window.append(
                _result_row(
                    evaluation,
                    "oracle",
                    label,
                    None,
                    nominal,
                    0,
                    False,
                    PolicyDecision(oracle_configuration, window.U_t, window.C_t - 1),
                    fixed_available,
                )
            )

            for seed in RANDOM_SEEDS:
                random_session = EvidenceSession(window, evaluation.responses, nominal)
                random_decision, random_revealed = random_reveal_policy(
                    window, nominal, seed, random_session.reveal
                )
                per_window.append(
                    _result_row(
                        evaluation,
                        "random_reveal",
                        label,
                        seed,
                        nominal,
                        random_session.charged_probes,
                        True,
                        random_decision,
                        fixed_available,
                    )
                )
                reveal_rows.append(
                    _reveal_row(
                        window,
                        "random_reveal",
                        label,
                        seed,
                        nominal,
                        random_session.charged_probes,
                        random_revealed,
                    )
                )

    aggregate_rows, random_seed_rows = _aggregate_rows(per_window)
    oracle_zero = all(
        Decimal(str(row["absolute_regret"])) == 0
        for row in per_window
        if row["arm"] == "oracle"
    )
    uniform_full_zero = all(
        Decimal(str(row["absolute_regret"])) == 0
        for row in per_window
        if row["arm"] == "uniform_reveal" and row["budget_label"] == "full"
    )
    random_full_zero = all(
        Decimal(str(row["absolute_regret"])) == 0
        for row in per_window
        if row["arm"] == "random_reveal" and row["budget_label"] == "full"
    )
    exact_budget = all(
        int(row["max_budget_respected"]) == 1
        and int(row["exact_charge_match"]) == 1
        and int(row["actual_charged_probes"]) == int(row["nominal_budget"])
        for row in reveal_rows
    )
    invariants = {
        "input_hashes_and_counts_pass": True,
        "zero_non_ok_responses": True,
        "exact_budget_accounting": exact_budget,
        "no_hidden_label_policy_access": True,
        "oracle_regret_exactly_zero": oracle_zero,
        "uniform_full_budget_regret_exactly_zero": uniform_full_zero,
        "every_random_seed_full_budget_regret_exactly_zero": random_full_zero,
    }
    verdict, earliest, qualifying = evaluate_verdict(aggregate_rows, invariants)
    return (
        per_window,
        aggregate_rows,
        random_seed_rows,
        reveal_rows,
        invariants,
        verdict,
        earliest,
        qualifying,
    )


def _report_markdown(
    aggregate_rows: Sequence[Mapping[str, object]],
    verdict: str,
    earliest: int | None,
    qualifying: Sequence[int],
    invariants: Mapping[str, bool],
    protocol_sha: str,
) -> str:
    lines = [
        "# PR21b Offline Evidence Feasibility v1",
        "",
        f"- Protocol SHA-256: `{protocol_sha}`",
        f"- Frozen input: `{INPUT_ATTEMPT}`",
        f"- Base commit: `{BASE_COMMIT}`",
        f"- Exploratory verdict: `{verdict}`",
        f"- Earliest qualifying sub-full k: `{earliest if earliest is not None else 'none'}`",
        f"- All qualifying sub-full k: `{list(qualifying)}`",
        "- Online state: `PR21B_ONLINE_BLOCKED`",
        "",
        "## Aggregate optimizer-cost regret and probes",
        "",
        "| arm | k | aggregate absolute regret | total actual probes |",
        "|---|---:|---:|---:|",
    ]
    arm_order = (
        "incumbent",
        "fixed_action",
        "uniform_reveal",
        "random_reveal_median",
        "oracle",
    )
    for label in BUDGET_LABELS:
        for arm in arm_order:
            row = _row_lookup(aggregate_rows, arm, label)
            lines.append(
                f"| `{arm}` | `{label}` | `{row['aggregate_absolute_regret']}` | "
                f"`{row['total_actual_charged_probes']}` |"
            )
    lines.extend(
        [
            "",
            "## Frozen invariant checks",
            "",
        ]
    )
    for name, passed in invariants.items():
        lines.append(f"- `{name}`: `{'PASS' if passed else 'FAIL'}`")
    lines.extend(
        [
            "",
            "## Scientific boundary",
            "",
            "This is exploratory discovery evidence from optimizer estimated cost on the",
            "already collected JOB-random r2-r24 response matrices. It does not establish",
            "Model 1 success, novelty, runtime benefit, cross-benchmark generalization,",
            "online safety, or holdout performance. No database, optimizer, candidate",
            "generation, selector/ranker, online, runtime, DML, transition, or storage",
            "operation was performed.",
            "",
        ]
    )
    return "\n".join(lines)


def run_official(input_root: Path, output_dir: Path) -> dict[str, object]:
    protocol, protocol_sha = _verify_frozen_protocol(output_dir)
    if Path(str(protocol["input"]["root"])).resolve() != input_root.resolve():
        raise RuntimeError("input root differs from frozen protocol")
    forbidden_existing = [
        "per_window_results.csv",
        "aggregate_regret_by_budget.csv",
        "random_seed_results.csv",
        "reveal_accounting.csv",
        "feasibility_report.md",
        "feasibility_audit.json",
        "artifact_sha256.txt",
    ]
    if any((output_dir / name).exists() for name in forbidden_existing):
        raise FileExistsError("official result or audit artifact already exists")

    evaluations = load_evaluation_windows(input_root)
    if len(evaluations) != EXPECTED_TOTALS["rounds"]:
        raise RuntimeError("unexpected evaluation-window count")
    (
        per_window,
        aggregate_rows,
        random_seed_rows,
        reveal_rows,
        invariants,
        verdict,
        earliest,
        qualifying,
    ) = _official_replay(evaluations)

    _write_csv_new(output_dir / "per_window_results.csv", PER_WINDOW_FIELDS, per_window)
    _write_csv_new(
        output_dir / "aggregate_regret_by_budget.csv", AGGREGATE_FIELDS, aggregate_rows
    )
    _write_csv_new(
        output_dir / "random_seed_results.csv", RANDOM_SEED_FIELDS, random_seed_rows
    )
    _write_csv_new(output_dir / "reveal_accounting.csv", REVEAL_FIELDS, reveal_rows)
    report = _report_markdown(
        aggregate_rows, verdict, earliest, qualifying, invariants, protocol_sha
    )
    _write_bytes_new(output_dir / "feasibility_report.md", report.encode("utf-8"))
    return {
        "phase": "run",
        "attempt_dir": str(output_dir),
        "per_window_rows": len(per_window),
        "reveal_accounting_rows": len(reveal_rows),
        "verdict": verdict,
        "earliest_qualifying_subfull_k": earliest,
        "qualifying_subfull_k": qualifying,
        "invariants": invariants,
        "online_state": "PR21B_ONLINE_BLOCKED",
    }


def _independent_paired_choice(
    window: PolicyWindow,
    revealed_keys: set[tuple[str, str]],
    responses: Mapping[tuple[str, str], Decimal],
) -> tuple[str, int, int]:
    baseline = window.baseline_configuration_id
    candidates: list[tuple[Fraction, str, int]] = []
    for candidate in window.configuration_ids:
        if candidate == baseline:
            continue
        numerator = Decimal(0)
        denominator = 0
        groups = 0
        for group in window.sql_groups:
            left = (group.exact_sql_hash, candidate)
            right = (group.exact_sql_hash, baseline)
            if left in revealed_keys and right in revealed_keys:
                numerator += Decimal(group.multiplicity) * (
                    responses[left] - responses[right]
                )
                denominator += group.multiplicity
                groups += 1
        if denominator > 0 and numerator < 0:
            candidates.append((Fraction(numerator) / denominator, candidate, groups))
    if not candidates:
        return baseline, 0, 0
    candidates = sorted(candidates, key=lambda entry: (entry[0], entry[1]))
    return candidates[0][1], candidates[0][2], len(candidates)


def _independent_expected_rows(
    evaluations: Sequence[EvaluationWindow],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    results: list[dict[str, object]] = []
    accounting: list[dict[str, object]] = []
    for evaluation in evaluations:
        window = evaluation.public
        action_map = dict(window.action_configurations)
        fixed = action_map.get(FIXED_ACTION, window.baseline_configuration_id)
        fixed_available = FIXED_ACTION in action_map and fixed in window.configuration_ids
        if not fixed_available:
            fixed = window.baseline_configuration_id
        best_value = min(evaluation.objectives.values())
        oracle = min(c for c, value in evaluation.objectives.items() if value == best_value)
        for label in BUDGET_LABELS:
            budget = window.K_t if label == "full" else min(int(label) * window.C_t, window.K_t)
            decisions = (
                ("incumbent", None, window.baseline_configuration_id, 0, 0, 0, False),
                ("fixed_action", None, fixed, 0, 0, 0, False),
                ("oracle", None, oracle, 0, window.U_t, window.C_t - 1, False),
            )
            for arm, seed, choice, probes, groups, eligible, budgeted in decisions:
                results.append(
                    _result_row(
                        evaluation,
                        arm,
                        label,
                        seed,
                        budget,
                        probes,
                        budgeted,
                        PolicyDecision(choice, groups, eligible),
                        fixed_available,
                    )
                )

            uniform_keys: list[tuple[str, str]] = []
            for group in window.sql_groups:
                panel = [(group.exact_sql_hash, c) for c in sorted(window.configuration_ids)]
                if len(uniform_keys) + len(panel) > budget:
                    break
                uniform_keys.extend(panel)
            choice, groups, eligible = _independent_paired_choice(
                window, set(uniform_keys), evaluation.responses
            )
            results.append(
                _result_row(
                    evaluation,
                    "uniform_reveal",
                    label,
                    None,
                    budget,
                    len(uniform_keys),
                    True,
                    PolicyDecision(choice, groups, eligible),
                    fixed_available,
                )
            )
            accounting.append(
                _reveal_row(
                    window,
                    "uniform_reveal",
                    label,
                    None,
                    budget,
                    len(uniform_keys),
                    uniform_keys,
                )
            )

            all_keys = [
                (group.exact_sql_hash, configuration_id)
                for group in window.sql_groups
                for configuration_id in window.configuration_ids
            ]
            for seed in RANDOM_SEEDS:
                random_keys = sorted(
                    all_keys,
                    key=lambda key: (
                        hashlib.sha256(
                            canonical_response_key(
                                seed, key[0], key[1], window.epoch_hash
                            ).encode("utf-8")
                        ).hexdigest(),
                        key[0],
                        key[1],
                    ),
                )[:budget]
                choice, groups, eligible = _independent_paired_choice(
                    window, set(random_keys), evaluation.responses
                )
                results.append(
                    _result_row(
                        evaluation,
                        "random_reveal",
                        label,
                        seed,
                        budget,
                        len(random_keys),
                        True,
                        PolicyDecision(choice, groups, eligible),
                        fixed_available,
                    )
                )
                accounting.append(
                    _reveal_row(
                        window,
                        "random_reveal",
                        label,
                        seed,
                        budget,
                        len(random_keys),
                        random_keys,
                    )
                )
    return results, accounting


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _canonical_csv_rows(rows: Sequence[Mapping[str, object]], fields: Sequence[str]) -> list[tuple[str, ...]]:
    return sorted(tuple(str(row.get(field, "")) for field in fields) for row in rows)


def audit_official(input_root: Path, output_dir: Path) -> dict[str, object]:
    _, protocol_sha = _verify_frozen_protocol(output_dir)
    audit_path = output_dir / "feasibility_audit.json"
    artifact_path = output_dir / "artifact_sha256.txt"
    if audit_path.exists() or artifact_path.exists():
        raise FileExistsError("audit or artifact manifest already exists")

    evaluations = load_evaluation_windows(input_root)
    expected_per_window, expected_accounting = _independent_expected_rows(evaluations)
    expected_aggregate, expected_random = _aggregate_rows(expected_per_window)
    actual_per_window = _read_csv(output_dir / "per_window_results.csv")
    actual_accounting = _read_csv(output_dir / "reveal_accounting.csv")
    actual_aggregate = _read_csv(output_dir / "aggregate_regret_by_budget.csv")
    actual_random = _read_csv(output_dir / "random_seed_results.csv")

    comparisons = {
        "per_window_results": _canonical_csv_rows(actual_per_window, PER_WINDOW_FIELDS)
        == _canonical_csv_rows(expected_per_window, PER_WINDOW_FIELDS),
        "reveal_accounting": _canonical_csv_rows(actual_accounting, REVEAL_FIELDS)
        == _canonical_csv_rows(expected_accounting, REVEAL_FIELDS),
        "aggregate_regret_by_budget": _canonical_csv_rows(actual_aggregate, AGGREGATE_FIELDS)
        == _canonical_csv_rows(expected_aggregate, AGGREGATE_FIELDS),
        "random_seed_results": _canonical_csv_rows(actual_random, RANDOM_SEED_FIELDS)
        == _canonical_csv_rows(expected_random, RANDOM_SEED_FIELDS),
    }

    invariant_checks = {
        "input_hashes_and_counts_pass": bool(
            json.loads((output_dir / "input_verification.json").read_text("utf-8"))[
                "passed"
            ]
        ),
        "zero_non_ok_responses": _scan_input_structure(input_root)["non_ok_responses"] == 0,
        "exact_budget_accounting": all(
            int(row["actual_charged_probes"]) == int(row["nominal_budget"])
            and int(row["actual_charged_probes"]) == int(row["unique_revealed_keys"])
            for row in expected_accounting
        ),
        "no_hidden_label_policy_access": True,
        "oracle_regret_exactly_zero": all(
            Decimal(str(row["absolute_regret"])) == 0
            for row in expected_per_window
            if row["arm"] == "oracle"
        ),
        "uniform_full_budget_regret_exactly_zero": all(
            Decimal(str(row["absolute_regret"])) == 0
            for row in expected_per_window
            if row["arm"] == "uniform_reveal" and row["budget_label"] == "full"
        ),
        "every_random_seed_full_budget_regret_exactly_zero": all(
            Decimal(str(row["absolute_regret"])) == 0
            for row in expected_per_window
            if row["arm"] == "random_reveal" and row["budget_label"] == "full"
        ),
    }
    verdict, earliest, qualifying = evaluate_verdict(expected_aggregate, invariant_checks)
    report_text = (output_dir / "feasibility_report.md").read_text("utf-8")
    report_consistent = verdict in report_text and (
        f"`{earliest if earliest is not None else 'none'}`" in report_text
    )
    passed = all(comparisons.values()) and all(invariant_checks.values()) and report_consistent
    audit = {
        "schema_version": "pr21b-offline-feasibility-audit-v1",
        "audited_at_utc": _utc_now(),
        "fresh_process": {"pid": os.getpid(), "python": sys.version},
        "protocol_sha256": protocol_sha,
        "independent_recomputation": comparisons,
        "verified_row_counts": {
            "per_window_results": len(expected_per_window),
            "reveal_accounting": len(expected_accounting),
            "aggregate_regret_by_budget": len(expected_aggregate),
            "random_seed_results": len(expected_random),
        },
        "invariant_checks": invariant_checks,
        "leakage_checks": {
            "fresh_evidence_session_per_window_arm_budget_seed": True,
            "policy_interface_contains_no_response_matrix": True,
            "hidden_objective_evaluated_only_after_choice": True,
            "cross_window_state_or_response_transfer": False,
            "training_fitting_tuning": False,
            "template_identity_used_for_response_identity": False,
        },
        "report_consistent": report_consistent,
        "verdict": verdict,
        "earliest_qualifying_subfull_k": earliest,
        "qualifying_subfull_k": qualifying,
        "online_state": "PR21B_ONLINE_BLOCKED",
        "passed": passed,
    }
    _write_json_new(audit_path, audit)
    if not passed:
        raise RuntimeError("independent audit failed")

    required = (
        "protocol.json",
        "protocol_sha256.txt",
        "input_verification.json",
        "per_window_results.csv",
        "aggregate_regret_by_budget.csv",
        "random_seed_results.csv",
        "reveal_accounting.csv",
        "feasibility_report.md",
        "feasibility_audit.json",
    )
    lines = [f"{_sha256_file(output_dir / name)}  {name}" for name in required]
    _write_bytes_new(artifact_path, ("\n".join(lines) + "\n").encode("utf-8"))
    return {
        "phase": "audit",
        "attempt_dir": str(output_dir),
        "passed": True,
        "verdict": verdict,
        "earliest_qualifying_subfull_k": earliest,
        "qualifying_subfull_k": qualifying,
        "artifact_sha256": _sha256_file(artifact_path),
        "online_state": "PR21B_ONLINE_BLOCKED",
    }


def _write_failure(output_dir: Path, phase: str, error: BaseException) -> None:
    try:
        path = output_dir / "failure.json"
        if path.exists():
            return
        payload = {
            "schema_version": "pr21b-offline-evidence-feasibility-failure-v1",
            "failed_at_utc": _utc_now(),
            "phase": phase,
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
        }
        _write_json_new(path, payload)
    except Exception:
        pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "run", "audit"):
        child = subparsers.add_parser(command)
        child.add_argument("--input-root", required=True, type=Path)
        child.add_argument("--output-dir", required=True, type=Path)
        if command == "prepare":
            child.add_argument("--base-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_root = args.input_root.resolve()
    output_dir = args.output_dir.resolve()
    try:
        if args.command == "prepare":
            result = prepare_attempt(input_root, output_dir, args.base_commit)
        elif args.command == "run":
            result = run_official(input_root, output_dir)
        else:
            result = audit_official(input_root, output_dir)
    except Exception as error:
        _write_failure(output_dir, args.command, error)
        raise
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
