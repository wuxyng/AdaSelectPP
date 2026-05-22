# workload_parser.py
# Updated with alias resolution and accurate column parsing

from typing import Dict, List, Set
import re
from typing import Protocol


class _DBConn(Protocol):
    def get_tables(self): ...
    def get_columns(self, table_name: str): ...


class WorkloadParser:
    def __init__(self, db_connector: _DBConn):
        self.db = db_connector
        # Cache tables
        self._tables: List[str] = self.db.get_tables()
        # Preload metadata: table -> set of its columns
        self.table_columns: Dict[str, Set[str]] = {}
        for tbl in self._tables:
            self.table_columns[tbl] = set(self.db.get_columns(tbl))

    def get_tables(self) -> List[str]:
        """
        Return cached list of table names available for parsing.
        """
        return self._tables

    def store_indexable_columns_1(self, query: str, tables: List[str]) -> Dict[str, List[str]]:
        """
        Parse the SQL query to extract candidate indexable columns per table,
        resolving table aliases and filtering via metadata.
        Returns a dict mapping each table name to a list of column names referenced.
        """
        # 1) Build alias map from FROM clause
        alias_map: Dict[str, str] = {}
        from_match = re.search(
            r"from\s+(.*?)\s+(?:where|group\s+by|order\s+by)",
            query, re.IGNORECASE | re.DOTALL
        )
        if from_match:
            # split tables/aliases by comma
            raw_tables = from_match.group(1)
            for part in re.split(r",\s*", raw_tables):
                tokens = part.strip().split()
                if len(tokens) == 2:
                    table, alias = tokens
                else:
                    table = tokens[0]
                    alias = table
                alias_map[alias] = table

        # 2) Prepare result map
        idx_map: Dict[str, List[str]] = {tbl: [] for tbl in tables}

        # 3) Find all candidate column references
        # pattern captures optional table prefix
        pattern = re.compile(r"(?:([A-Za-z_]\w*)\.)?([A-Za-z_][\w-]*)")
        refs = pattern.findall(query)

        # 4) For each table, collect columns
        for tbl in tables:
            cols_seen: Set[str] = set()
            # only if table or its alias is present
            # check both actual table and any alias
            tbl_pattern = re.compile(
                rf"(?<!\w)(?:{re.escape(tbl)}|" +
                rf"{'|'.join(map(re.escape, [a for a,t in alias_map.items() if t==tbl]))})(?!\w)",
                re.IGNORECASE
            )
            if not tbl_pattern.search(query):
                continue

            for tbl_part, col_part in refs:
                # resolve prefix
                if tbl_part:
                    actual_tbl = alias_map.get(tbl_part, tbl_part)
                    if actual_tbl.lower() != tbl.lower():
                        continue
                # metadata check
                if col_part in self.table_columns.get(tbl, set()):
                    cols_seen.add(col_part)

            idx_map[tbl] = list(cols_seen)

        return idx_map

    def store_indexable_columns(
            self,
            query: str,
            tables: List[str]
    ) -> Dict[str, List[str]]:
        """
        For each table name in `tables`, if the table name appears in the
        query text (case-insensitive), collect all column names from that table
        whose lowercase name appears in the query text. Return a mapping
        of table_name -> list of referenced columns.
        """
        idx_map: Dict[str, List[str]] = {tbl: [] for tbl in tables}
        q_lower = query.lower()
        for tbl in tables:
            if tbl.lower() not in q_lower:
                continue
            cols_seen: List[str] = []
            for col in self.table_columns.get(tbl, []):
                if col.lower() in q_lower:
                    cols_seen.append(col)
            idx_map[tbl] = cols_seen
        return idx_map
