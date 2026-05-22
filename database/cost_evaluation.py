from adaselect_pp.common import sql_only
from tcnn import tcnn_util

import torch
from torch import nn
from tcnn.tree_util import prepare_trees
from typing import Iterable, Set, Tuple


class CostEvaluation:
    """
    Responsibilities:
    - `calculate_cost(workload, indexes)`: materializes `indexes`, then
      calls `calculate_now_cost` to get total cost.
    - `calculate_now_cost(workload)`: for each query, either uses learned model
      or falls back to `db_con.get_query_cost`.
    """
    def __init__(
        self,
        db_con,
        benchmark: str,
        cuda: bool = True,
        net_file: str | None = None
    ):
        self.db_con = db_con

        # Optional learned cost model
        self.net = None
        if net_file:
            self.cuda = cuda
            # Operators and columns lists
            self.operators = [line.strip() for line in open("txt/operators.txt")]
            # load indexable columns for the learned model
            self.columns = [line.strip().split()[0]
                            for line in open(f"txt/{benchmark}_indexable_columns.txt")]
            self.net = nn.Sequential(
                tcnn_util.BinaryTreeConv(len(self.operators) + len(self.columns) + 1, 256),
                tcnn_util.TreeLayerNorm(),
                tcnn_util.TreeActivation(nn.ReLU()),
                tcnn_util.BinaryTreeConv(256, 128),
                tcnn_util.TreeLayerNorm(),
                tcnn_util.TreeActivation(nn.ReLU()),
                tcnn_util.BinaryTreeConv(128, 64),
                tcnn_util.TreeLayerNorm(),
                tcnn_util.TreeActivation(nn.ReLU()),
                tcnn_util.DynamicPooling(),
                nn.Linear(64, 32), nn.ReLU(),
                nn.Linear(32, 16), nn.ReLU(),
                nn.Linear(16, 1),
            )
            self.net.load_state_dict(torch.load(net_file))
            if self.cuda:
                self.net.cuda()
    def calculate_cost(
        self,
        workload: Iterable[str],
        indexes: Set[tuple]
    ) -> float:
        """Materialize `indexes`, then compute and return total cost."""
        existing = set(self.db_con.get_indexes())
        # drop extra
        for idx in existing - indexes:
            self.db_con.drop_index(idx[0], idx[1:])
        # create missing
        for idx in indexes - existing:
            self.db_con.create_index(idx[0], idx[1:])
        return self.calculate_now_cost(workload)

    def calculate_now_cost(self, workload: Iterable[str]) -> float:
        """Compute cost for queries in `workload` under current indexes."""
        total = 0.0
        for q in workload:
            q = sql_only(q)
            if self.net:
                plan = self.db_con.get_plan(q)
                opt_cost = plan.get("Total Cost", 0.0)
                pred = self.net(prepare_trees([plan], self.operators, self.columns, self.cuda))
                if self.cuda:
                    pred = pred.cpu()
                pred = float(torch.squeeze(pred).detach().numpy())
                total += opt_cost / 100 if opt_cost / pred > 1000 else pred
            else:
                total += self.db_con.get_query_cost(q)
        return total

    # --------------------------------------------------------------
    # fast Δ-cost prior —— cheap what-if via HypoPG
    # --------------------------------------------------------------
    def fast_delta(self, table: str, cols: Tuple[str, ...]) -> float:
        """Improved fast?Δ using a *selectivity probe*.

        1. Pick a constant from the column so the predicate is selective and
           the planner will seriously consider an Index Scan.
        2. Measure cost **before** / **after** creating a HypoPG index.
        3. Return positive delta; 0 means the index seemed useless.
        """
        if not cols:
            return 0.0

        col = cols[0]

        # (0) sample a constant so predicate is selective
        sampled_val = self.db_con.fetch_one_value(
            f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL LIMIT 1")

        # Build probe query as *plain string* for EXPLAIN
        if sampled_val is None:
            probe_sql = f"SELECT 1 FROM {table} WHERE {col} IS NOT NULL LIMIT 1"
        else:
            # Minimal literal escaping (strings only — refine as needed)
            if isinstance(sampled_val, str):
                lit = "'" + sampled_val.replace("'", "''") + "'"
            else:
                lit = str(sampled_val)
            probe_sql = f"SELECT 1 FROM {table} WHERE {col} = {lit} LIMIT 1"

        # (1) baseline cost
        base = self.db_con.get_query_cost(probe_sql)

        # (2) virtual create (HypoPG)
        self.db_con.create_index(table, cols)
        try:
            new_cost = self.db_con.get_query_cost(probe_sql)
        finally:
            self.db_con.drop_index(table, cols)

        delta = base - new_cost
        return max(delta, 0.0)
