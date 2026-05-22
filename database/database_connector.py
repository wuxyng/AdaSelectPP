# -*- coding: utf-8 -*-
# database_connector.py - 正确的超时处理
from typing import Any, List, Optional
import psycopg2
import psycopg2.errors
from psycopg2 import extensions
import time
import logging
from adaselect_pp.common import sql_only

logger = logging.getLogger(__name__)



class DatabaseConnector:
    def __init__(self, database, virtual=True, run_num=1):
        self.__database = database
        self.__host = "localhost"
        self.__port = 50222
        self.__user = "lx"
        self.__virtual = virtual
        self.__run_num = run_num

        self.__connection = psycopg2.connect(
            database=self.__database,
            port=self.__port,
            host=self.__host,
            user=self.__user
        )
        self.__cursor = self.__connection.cursor()

        self.__indexes = []
        self.__virtual_id = {}
        self.__true_name = {}

        # Schema-guard warning de-duplication.
        # We still guard against invalid (table, cols), but avoid spamming logs.
        self._schema_guard_warned = set()

    def close(self):
        self.drop_all_indexes()
        self.__cursor.close()
        self.__connection.close()

    # ---------------------------------------------------------------------
    # Connection health / reconnection
    # ---------------------------------------------------------------------
    def _ensure_connection(self) -> None:
        """Best-effort guard to ensure the underlying connection/cursor are valid.

        Notes:
        - In practice, psycopg2 connections can enter a bad state after timeouts
          (QueryCanceled / InFailedSqlTransaction) until rollback.
        - This helper is intentionally lightweight: if the connection is closed,
          we reconnect using the same constructor parameters.
        """
        try:
            if getattr(self, "_DatabaseConnector__connection", None) is None:
                return
            if self.__connection.closed != 0:
                self.__connection = psycopg2.connect(
                    database=self.__database,
                    port=self.__port,
                    host=self.__host,
                    user=self.__user,
                )
                self.__cursor = self.__connection.cursor()
        except Exception:
            # Do not fail hard here; caller will surface errors if any.
            return

    def _ensure_tx_ok(self) -> None:
        """Ensure we are not stuck in an aborted transaction.

        Psycopg2 keeps the connection in `InFailedSqlTransaction` after any
        SQL error until an explicit ROLLBACK. In workloads with timeouts or
        failed DDL, this can easily poison subsequent commands (e.g., EXPLAIN).

        This guard is cheap and idempotent.
        """
        try:
            if getattr(self, "_DatabaseConnector__connection", None) is None:
                return
            if self.__connection.closed != 0:
                return
            if self.__connection.get_transaction_status() == extensions.TRANSACTION_STATUS_INERROR:
                self.__connection.rollback()
        except Exception:
            # Best effort.
            return

    def rollback(self):
        """回滚当前事务"""
        try:
            self.__connection.rollback()
        except Exception:
            pass

    def get_plan(self, query: str):
        query = sql_only(query)
        """获取查询计划（不执行）"""
        query = sql_only(query)
        # 关键修复：任何异常都必须 rollback，否则连接会进入 aborted 状态
        #（InFailedSqlTransaction），后续 round 会全部失败。
        self._ensure_connection()
        self._ensure_tx_ok()
        try:
            # SET LOCAL 需要事务上下文；psycopg2 默认会隐式开启事务。
            self.__cursor.execute("SET LOCAL statement_timeout = 0")
            self.__cursor.execute(f"EXPLAIN (FORMAT JSON) {query}")
            result = self.__cursor.fetchone()
            self.__connection.commit()
            return result[0][0]["Plan"]
        except Exception:
            try:
                self.__connection.rollback()
            except Exception:
                pass
            raise

        # =========================================================================
        # 修复 2: get_query_runtime - 只添加异常处理，不改其他逻辑
        # =========================================================================

    def get_query_runtime(self, query: str) -> float:
        query = sql_only(query)
        """
        获取查询真实运行时间

        修复：添加 QueryCanceled 异常处理和 rollback
        不修改：并行设置、超时设置（保持原有行为）
        """
        self._ensure_connection()
        runtime = 0.0

        for _ in range(self.__run_num):
            self._ensure_tx_ok()
            try:
                time_1 = time.time()
                self.__cursor.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {query}")
                result = self.__cursor.fetchone()
                time_2 = time.time()
                self.__connection.commit()

                # 解析结果
                if result and result[0]:
                    plan_result = result[0]
                    if isinstance(plan_result, list) and len(plan_result) > 0:
                        runtime += plan_result[0]["Plan"]["Actual Total Time"]
                    else:
                        runtime += (time_2 - time_1) * 1000
                else:
                    runtime += (time_2 - time_1) * 1000

            except Exception:
                # Any error leaves the connection in an aborted state until rollback.
                try:
                    self.__connection.rollback()
                except Exception:
                    pass
                raise

        return runtime / self.__run_num
    # =========================================================================
    # 其他方法
    # =========================================================================

    def execute_only(self, query: str):
        query = sql_only(query)
        self._ensure_connection()
        self._ensure_tx_ok()
        try:
            self.__cursor.execute(query)
            self.__connection.commit()
        except Exception:
            try:
                self.__connection.rollback()
            except Exception:
                pass
            raise

    def execute_and_fetch(self, query: str, one: bool = True):
        query = sql_only(query)
        self._ensure_connection()
        self._ensure_tx_ok()
        try:
            self.__cursor.execute(query)
            if one:
                row = self.__cursor.fetchone()
            else:
                row = self.__cursor.fetchall()
            self.__connection.commit()
            return row
        except Exception:
            try:
                self.__connection.rollback()
            except Exception:
                pass
            raise

    def get_query_cost(self, query: str) -> float:
        query = sql_only(query)
        return self.get_plan(query)["Total Cost"]

    def exec_fetchall(self, query):
        query = sql_only(query)
        self._ensure_connection()
        self._ensure_tx_ok()
        try:
            self.__cursor.execute(query)
            rows = self.__cursor.fetchall()
            self.__connection.commit()
            return rows
        except Exception:
            try:
                self.__connection.rollback()
            except Exception:
                pass
            raise

    def exec_fetchall_params(self, query: str, params: tuple):
        """Execute a parameterized query and fetch all rows."""
        self._ensure_connection()
        self._ensure_tx_ok()
        try:
            self.__cursor.execute(query, params)
            rows = self.__cursor.fetchall()
            self.__connection.commit()
            return rows
        except Exception:
            try:
                self.__connection.rollback()
            except Exception:
                pass
            raise

    def fetch_one_value(self, sql: str):
        """Helper: execute a query and return the first column of the first row."""
        self._ensure_connection()
        self._ensure_tx_ok()
        try:
            with self.__connection.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
            self.__connection.commit()
            return row[0] if row else None
        except Exception:
            try:
                self.__connection.rollback()
            except Exception:
                pass
            raise

    def create_index(self, table, columns):
        # Schema guard: skip impossible (table, columns) early to avoid aborting the transaction.
        if not self._schema_guard_ok(table, columns):
            return
        # Build canonical index key.
        index = (table, *tuple(columns))
        if index in self.__indexes:
            return

        # Create the index in the database first; only update local state on success.
        if self.__virtual:
            query = f"SELECT * FROM hypopg_create_index('CREATE INDEX ON {table} (" + ", ".join(columns) + ")')"
            try:
                vid = self.execute_and_fetch(query, True)[0]
            except Exception:
                # execute_and_fetch already rolled back; keep state unchanged.
                return
            self.__virtual_id[index] = vid
            self.__indexes.append(index)
        else:
            index_name = f"index_{time.time()}".replace(".", "_")
            query = f"CREATE INDEX {index_name} ON {table} (" + ", ".join(columns) + ")"
            try:
                self.execute_only(query)
            except (psycopg2.errors.ProgramLimitExceeded, psycopg2.errors.QueryCanceled):
                # keep state unchanged
                return
            except Exception:
                # keep state unchanged
                return
            self.__true_name[index] = index_name
            self.__indexes.append(index)

    def drop_all_indexes(self):
        # drop indexes from the database
        if self.__virtual:
            try:
                self.execute_only("SELECT * FROM hypopg_reset()")
            except Exception:
                # ensure we don't keep the connection in an aborted state
                self.rollback()
            self.__virtual_id.clear()
        else:
            for index_name in self.__true_name.values():
                try:
                    self.execute_only(f"DROP INDEX {index_name}")
                except Exception:
                    # ignore drop failures (already dropped, concurrent, etc.)
                    self.rollback()
            self.__true_name.clear()

        # drop indexes from self.__indexes
        self.__indexes.clear()

    def drop_index(self, table, columns):
        index = [table]
        index.extend(columns)
        index = tuple(index)

        if index not in self.__indexes:
            return

        # drop the index from the database first; only update local state on success.
        try:
            if self.__virtual:
                vid = self.__virtual_id.get(index)
                if vid is not None:
                    self.execute_only(f"SELECT * FROM hypopg_drop_index({vid})")
            else:
                name = self.__true_name.get(index)
                if name:
                    self.execute_only(f"DROP INDEX {name}")
        except Exception:
            # keep state unchanged if drop fails
            return

        # update local state
        try:
            self.__indexes.remove(index)
        except Exception:
            pass
        self.__virtual_id.pop(index, None)
        self.__true_name.pop(index, None)



    def get_workload_cost(self, workload):
        total_cost = 0.0
        for query in workload:
            total_cost += self.get_query_cost(query)
        return total_cost

    def get_indexes(self):
        return self.__indexes.copy()

    def get_virtual_index_oid(self, table, columns):
        """Return HypoPG OID for an existing virtual index, if known."""
        index = [table]
        index.extend(columns)
        index = tuple(index)
        return self.__virtual_id.get(index)

    def get_virtual_index_metadata(self, table, columns):
        """Return HypoPG metadata using current HypoPG APIs.

        This function is diagnostic-only in the clean spine; compile validation is not
        a candidate hard gate. It must not spam server logs with invalid catalog queries.
        """
        oid = self.get_virtual_index_oid(table, columns)
        if oid is None:
            return None
        meta = {"oid": oid, "table": table, "columns": tuple(columns)}
        try:
            row = self.execute_and_fetch(
                "SELECT index_name, hypopg_get_indexdef(indexrelid) "
                f"FROM hypopg_list_indexes WHERE indexrelid = {int(oid)}",
                True,
            )
            if row:
                meta["name"] = str(row[0])
                meta["indexdef"] = str(row[1])
                return meta
        except Exception:
            self.rollback()
        try:
            row = self.execute_and_fetch(
                "SELECT indexname, hypopg_get_indexdef(indexrelid) "
                f"FROM hypopg() WHERE indexrelid = {int(oid)}",
                True,
            )
            if row:
                meta["name"] = str(row[0])
                meta["indexdef"] = str(row[1])
        except Exception:
            self.rollback()
        return meta

    def set_mode(self, virtual):
        if self.__virtual != virtual:
            self.drop_all_indexes()
            self.__virtual = virtual

    # NOTE: rollback() is defined once above with safety guards.

    def get_index_size(self, table, columns):
        if self.__virtual:
            index = [table]
            index.extend(columns)
            index = tuple(index)

            created = False  # whether creating a new virtual index

            if index not in self.__indexes:
                self.create_index(table, columns)
                created = True

            result = self.execute_and_fetch(
                f"SELECT hypopg_relation_size({self.__virtual_id[index]})", True)[0]

            if created:
                self.drop_index(table, columns)

            return result / 1048576
        else:
            self.close()
            print("Does not support getting the size of the real index.")
            exit()

    def get_database_size(self):
        result = self.execute_and_fetch(f"SELECT pg_database_size('{self.__database}')")
        return result[0] / 1048576

    # this function is only useful for true indexes
    def disable_index(self, table, columns):
        index = [table]
        index.extend(columns)
        index = tuple(index)

        self.execute_only("update pg_index set indisvalid=false "
                          f"where indexrelid='{self.__true_name[index]}'::regclass")

    def enable_index(self, table, columns):
        index = [table]
        index.extend(columns)
        index = tuple(index)

        self.execute_only("update pg_index set indisvalid=true "
                          f"where indexrelid='{self.__true_name[index]}'::regclass")

    def disable_all_indexes(self):
        for index_name in self.__true_name.values():
            self.execute_only("update pg_index set indisvalid=false "
                              f"where indexrelid='{index_name}'::regclass")

    def enable_all_indexes(self):
        for index_name in self.__true_name.values():
            self.execute_only("update pg_index set indisvalid=true "
                              f"where indexrelid='{index_name}'::regclass")

    # unit of measurement: MB
    def get_table_size(self, table):
        return self.execute_and_fetch(f"SELECT pg_table_size('{table}')")[0] / 1048576

    # Add this method to your DatabaseConnector class (e.g. in database/database_connector.py)
    # It reads and caches the maximum declared length of a VARCHAR/CHAR column from information_schema.
    # Returns None for types without a max_length (e.g. TEXT), or the integer length in bytes.

    def get_column_max_length(self, table: str, column: str) -> Optional[int]:
        """Return declared storage length (bytes) for a column.

        * VARCHAR/CHAR(n)          → n bytes
        * Fixed?width numeric/int  → constant bytes
        * TEXT / BYTEA / JSONB     → None (unlimited)
        * NUMERIC(p, s)           → ?p/2? + 1 bytes (rough PG rule)
        """
        sql = (
            "SELECT data_type, character_maximum_length, numeric_precision "
            "FROM information_schema.columns "
            f"WHERE table_schema = 'public' AND table_name = '{table}' AND column_name = '{column}'"
        )
        row = self.exec_fetchall(sql)
        if not row:
            return None

        dtype, char_len, num_prec = row[0]

        # String with declared length
        if char_len is not None:
            return int(char_len)

        dtype = dtype.lower()
        fixed = {
            ("integer", "int4"): 4,
            ("bigint", "int8"): 8,
            ("smallint", "int2"): 2,
            ("date", "timestamp", "timestamptz", "float4", "float8"): 8,
        }
        for names, size in fixed.items():
            if dtype in names:
                return size

        if dtype.startswith("numeric") and num_prec:
            return (num_prec + 1) // 2 + 1

        # TEXT / BYTEA / JSONB / NUMERIC without precision
        return None

    # Add the following methods to your DatabaseConnector class in database/database_connector.py

    def get_tables2(self) -> List[str]:
        """
        Return a list of user-defined table names in the 'public' schema.
        """
        sql = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        rows = self.execute_and_fetch(sql, False)
        return [row[0] for row in rows]

    def get_tables(self) -> List[str]:
        """Return list of user tables in the public schema."""
        sql = (
            "SELECT table_name "
            "FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        rows = self.exec_fetchall(sql)
        return [r[0] for r in rows]

    def get_columns(self, table: str) -> List[str]:
        """
        Return a list of column names for the given table (public schema), ordered.
        """
        # 1) Inline the table name safely (表名受限于数据库元数据，不应包含注入风险)
        sql = (
            "SELECT column_name"
            "  FROM information_schema.columns"
            " WHERE table_schema = 'public'"
            f"   AND table_name   = '{table}'"
            " ORDER BY ordinal_position"
        )
        # 2) 执行并取全表
        rows = self.exec_fetchall(sql)
        return [row[0] for row in rows]




    def _get_columns_cached(self, table: str) -> set:
        """Cached set of column names for a table (best-effort)."""
        if not hasattr(self, "_col_cache"):
            self._col_cache = {}
        # normalize schema-qualified names
        t = (table or "").strip().strip('"')
        if '.' in t:
            t = t.split('.')[-1]
        if t not in self._col_cache:
            try:
                self._col_cache[t] = set(self.get_columns(t))
            except Exception as e:
                logger.warning("Failed to load columns for %s: %s", t, e)
                self._col_cache[t] = set()
        return self._col_cache[t]

    def _schema_guard_ok(self, table: str, columns) -> bool:
        """Return False if any column does not exist in the table (best-effort)."""
        try:
            cols = list(columns) if columns is not None else []
            if not cols:
                return True
            existing = self._get_columns_cached(table)
            if not existing:
                # If we cannot fetch schema, do not block execution.
                return True
            missing = [c for c in cols if c not in existing]
            if missing:
                # De-duplicate to avoid log spam on recurring invalid candidates.
                key = (str(table), tuple(cols), tuple(missing))
                if key not in self._schema_guard_warned:
                    self._schema_guard_warned.add(key)
                    logger.warning("Schema guard: skip index on %s(%s); missing=%s", table, cols, missing)
                return False
            return True
        except Exception as e:
            logger.warning("Schema guard internal error for %s(%s): %s", table, columns, e)
            return True
