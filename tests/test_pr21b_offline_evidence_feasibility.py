import hashlib
import unittest
from decimal import Decimal

from tools.pr21b_offline_evidence_feasibility import (
    FIXED_ACTION,
    VERDICT_OBSERVED,
    EvidenceSession,
    EvaluationWindow,
    PolicyWindow,
    SqlGroup,
    budget_for_label,
    canonical_response_key,
    choose_from_paired_evidence,
    evaluate_verdict,
    fixed_action_choice,
    oracle_choice,
    random_reveal_policy,
    uniform_reveal_policy,
)


BASELINE = "cfg_0"
CONFIG_A = "cfg_a"
CONFIG_B = "cfg_b"
EPOCH = "e" * 64


def _window(*, fixed_available=True):
    actions = ((FIXED_ACTION, CONFIG_A),) if fixed_available else ()
    return PolicyWindow(
        round_id=2,
        baseline_configuration_id=BASELINE,
        configuration_ids=(BASELINE, CONFIG_A, CONFIG_B),
        sql_groups=(
            SqlGroup("q1", 3, 0),
            SqlGroup("q2", 1, 1),
        ),
        action_configurations=actions,
        epoch_hash=EPOCH,
    )


def _responses():
    return {
        ("q1", BASELINE): Decimal("10"),
        ("q1", CONFIG_A): Decimal("8"),
        ("q1", CONFIG_B): Decimal("9"),
        ("q2", BASELINE): Decimal("10"),
        ("q2", CONFIG_A): Decimal("20"),
        ("q2", CONFIG_B): Decimal("9"),
    }


def _evaluation():
    window = _window()
    responses = _responses()
    objectives = {
        configuration_id: sum(
            Decimal(group.multiplicity)
            * responses[(group.exact_sql_hash, configuration_id)]
            for group in window.sql_groups
        )
        for configuration_id in window.configuration_ids
    }
    return EvaluationWindow(window, responses, objectives)


class OfflineEvidenceFeasibilityTests(unittest.TestCase):
    def test_duplicate_occurrences_weight_objective_but_reveal_charges_once(self):
        evaluation = _evaluation()
        self.assertEqual(evaluation.objectives[BASELINE], Decimal("40"))
        self.assertEqual(evaluation.objectives[CONFIG_A], Decimal("44"))

        session = EvidenceSession(evaluation.public, evaluation.responses, budget=2)
        self.assertEqual(session.reveal("q1", CONFIG_A), Decimal("8"))
        self.assertEqual(session.reveal("q1", CONFIG_A), Decimal("8"))
        self.assertEqual(session.charged_probes, 1)
        session.reveal("q1", BASELINE)
        self.assertEqual(session.charged_probes, 2)
        with self.assertRaisesRegex(RuntimeError, "exceed"):
            session.reveal("q2", BASELINE)

    def test_partial_rule_uses_multiplicity_weighted_pairs_and_strict_negative(self):
        window = _window()
        responses = _responses()
        revealed = {
            ("q1", BASELINE): responses[("q1", BASELINE)],
            ("q1", CONFIG_A): responses[("q1", CONFIG_A)],
            ("q1", CONFIG_B): responses[("q1", CONFIG_B)],
            ("q2", BASELINE): responses[("q2", BASELINE)],
            ("q2", CONFIG_A): responses[("q2", CONFIG_A)],
            ("q2", CONFIG_B): responses[("q2", CONFIG_B)],
        }
        # A: (3*-2 + 1*10)/4 = +1 and is ineligible.
        # B: (3*-1 + 1*-1)/4 = -1 and is selected.
        decision = choose_from_paired_evidence(window, revealed)
        self.assertEqual(decision.configuration_id, CONFIG_B)
        self.assertEqual(decision.matched_groups_for_choice, 2)
        self.assertEqual(decision.eligible_candidates, 1)

        no_pair = {("q1", CONFIG_A): Decimal("1")}
        self.assertEqual(
            choose_from_paired_evidence(window, no_pair).configuration_id, BASELINE
        )

    def test_exact_mean_tie_breaks_by_canonical_configuration_id(self):
        window = _window()
        revealed = {
            ("q1", BASELINE): Decimal("10"),
            ("q1", CONFIG_A): Decimal("9"),
            ("q1", CONFIG_B): Decimal("9"),
        }
        self.assertEqual(
            choose_from_paired_evidence(window, revealed).configuration_id, CONFIG_A
        )

    def test_uniform_reveal_uses_complete_panels_only(self):
        window = _window()
        responses = _responses()
        revealed_keys = []

        def reveal(sql_hash, configuration_id):
            revealed_keys.append((sql_hash, configuration_id))
            return responses[(sql_hash, configuration_id)]

        decision, revealed = uniform_reveal_policy(window, budget=4, reveal=reveal)
        self.assertEqual(len(revealed), window.C_t)
        self.assertEqual(set(revealed), {("q1", c) for c in window.configuration_ids})
        self.assertEqual(
            revealed_keys, [("q1", c) for c in window.configuration_ids]
        )
        self.assertEqual(decision.configuration_id, CONFIG_A)

    def test_random_reveal_is_sha256_ordered_and_deterministic(self):
        window = _window()
        responses = _responses()
        observed = []

        def reveal(sql_hash, configuration_id):
            observed.append((sql_hash, configuration_id))
            return responses[(sql_hash, configuration_id)]

        random_reveal_policy(window, budget=4, seed=7, reveal=reveal)
        expected = sorted(
            [
                (group.exact_sql_hash, configuration_id)
                for group in window.sql_groups
                for configuration_id in window.configuration_ids
            ],
            key=lambda key: (
                hashlib.sha256(
                    canonical_response_key(7, key[0], key[1], EPOCH).encode("utf-8")
                ).hexdigest(),
                key[0],
                key[1],
            ),
        )[:4]
        self.assertEqual(observed, expected)

    def test_full_budget_uniform_and_random_have_zero_regret(self):
        evaluation = _evaluation()
        best = min(evaluation.objectives.values())
        self.assertEqual(oracle_choice(evaluation), CONFIG_B)

        uniform_session = EvidenceSession(
            evaluation.public, evaluation.responses, evaluation.public.K_t
        )
        uniform, _ = uniform_reveal_policy(
            evaluation.public, evaluation.public.K_t, uniform_session.reveal
        )
        self.assertEqual(evaluation.objectives[uniform.configuration_id] - best, 0)

        for seed in range(10):
            random_session = EvidenceSession(
                evaluation.public, evaluation.responses, evaluation.public.K_t
            )
            random, _ = random_reveal_policy(
                evaluation.public,
                evaluation.public.K_t,
                seed,
                random_session.reveal,
            )
            self.assertEqual(evaluation.objectives[random.configuration_id] - best, 0)

    def test_fixed_action_is_availability_aware_with_incumbent_fallback(self):
        self.assertEqual(fixed_action_choice(_window(fixed_available=True)), (CONFIG_A, True))
        self.assertEqual(
            fixed_action_choice(_window(fixed_available=False)), (BASELINE, False)
        )

    def test_budget_grid_scales_by_configuration_count_and_caps_at_full(self):
        window = _window()
        self.assertEqual(window.U_t, 2)
        self.assertEqual(window.C_t, 3)
        self.assertEqual(window.K_t, 6)
        self.assertEqual(budget_for_label(window, "0"), 0)
        self.assertEqual(budget_for_label(window, "1"), 3)
        self.assertEqual(budget_for_label(window, "2"), 6)
        self.assertEqual(budget_for_label(window, "16"), 6)
        self.assertEqual(budget_for_label(window, "full"), 6)

    def test_policy_window_exposes_no_hidden_response_or_objective_fields(self):
        window = _window()
        self.assertFalse(hasattr(window, "responses"))
        self.assertFalse(hasattr(window, "objectives"))
        self.assertFalse(hasattr(window, "rankings"))
        self.assertFalse(hasattr(window, "regrets"))

    def test_frozen_verdict_returns_earliest_common_qualifying_budget(self):
        rows = []
        for k in (1, 2, 4, 8, 16):
            incumbent = Decimal("100")
            fixed = Decimal("90")
            uniform = Decimal("80") if k >= 4 else Decimal("95")
            random_median = Decimal("85") if k >= 4 else Decimal("92")
            for arm, regret in (
                ("incumbent", incumbent),
                ("fixed_action", fixed),
                ("uniform_reveal", uniform),
                ("random_reveal_median", random_median),
            ):
                rows.append(
                    {
                        "arm": arm,
                        "budget_label": str(k),
                        "aggregate_absolute_regret": str(regret),
                    }
                )
        verdict, earliest, qualifying = evaluate_verdict(
            rows, {"all_frozen_invariants": True}
        )
        self.assertEqual(verdict, VERDICT_OBSERVED)
        self.assertEqual(earliest, 4)
        self.assertEqual(qualifying, [4, 8, 16])


if __name__ == "__main__":
    unittest.main()
