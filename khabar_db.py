"""
khabar_db.py — drop-in replacement for the Supabase Python client, backed by a
plain PostgreSQL connection (works against PlanetScale *or* Supabase-direct).

Goal: let existing code keep calling
    supabase = create_client(...)
    supabase.table("x").select("...").eq("a", 1).execute()
    supabase.table("x").upsert(rows, on_conflict="a,b").execute()
…unchanged. Only the import line changes:  from khabar_db import create_client

Connection: reads env KHABAR_DB_URL (falls back to SUPABASE_DB_URL). Point that
at PlanetScale to run on PlanetScale; point it at Supabase-direct to A/B test.
"""

import os
import time
import psycopg2
import psycopg2.extras
from psycopg2.extras import Json

_DB_URL = (os.environ.get("KHABAR_DB_URL", "").strip()
           or os.environ.get("SUPABASE_DB_URL", "").strip()
           or None)

# Known parent-child relationships for embedded selects (child.col -> parent.col).
# Used to translate  select("...parent!inner(...)")  into a SQL JOIN. Extra
# relationships are auto-resolved from information_schema on first use.
_REL = {
    ("user_sizes", "users"): ("user_id", "telegram_id"),
    ("user_brands", "users"): ("user_id", "telegram_id"),
    ("product_variants", "products"): ("product_id", "id"),
}


class _Result:
    __slots__ = ("data", "count")
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


def _adapt(v):
    # jsonb/json columns receive Python dict/list -> wrap for psycopg2.
    if isinstance(v, (dict, list)):
        return Json(v)
    return v


def _split_top_level(s):
    """Split a select string on commas that are NOT inside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1; cur += ch
        elif ch == ")":
            depth -= 1; cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


class _Query:
    def __init__(self, client, table):
        self._c = client
        self._t = table
        self._op = "select"
        self._sel = "*"
        self._payload = None
        self._on_conflict = None
        self._filters = []          # list of (col, op, value)
        self._order = []            # list of (col, desc)
        self._limit = None
        self._offset = None
        self._count = None

    # ---- operations ----
    def select(self, cols="*", count=None):
        self._op = "select"; self._sel = cols or "*"; self._count = count; return self

    def insert(self, rows):
        self._op = "insert"; self._payload = rows if isinstance(rows, list) else [rows]; return self

    def upsert(self, rows, on_conflict=None):
        self._op = "upsert"; self._payload = rows if isinstance(rows, list) else [rows]
        self._on_conflict = on_conflict; return self

    def update(self, values):
        self._op = "update"; self._payload = values; return self

    def delete(self):
        self._op = "delete"; return self

    # ---- filters ----
    def eq(self, col, val):  self._filters.append((col, "=", val));  return self
    def neq(self, col, val): self._filters.append((col, "<>", val)); return self
    def gt(self, col, val):  self._filters.append((col, ">", val));  return self
    def gte(self, col, val): self._filters.append((col, ">=", val)); return self
    def lt(self, col, val):  self._filters.append((col, "<", val));  return self
    def lte(self, col, val): self._filters.append((col, "<=", val)); return self
    def in_(self, col, vals): self._filters.append((col, "in", list(vals))); return self
    def is_(self, col, val):  self._filters.append((col, "is", val));  return self

    # ---- shaping ----
    def order(self, col, desc=False): self._order.append((col, desc)); return self
    def limit(self, n): self._limit = n; return self
    def range(self, start, end): self._offset = start; self._limit = end - start + 1; return self
    def single(self): self._limit = 1; self._single = True; return self
    def maybe_single(self): self._limit = 1; self._single = True; return self

    # ---- WHERE builder (alias: table alias for base; embeds mapped separately) ----
    def _where(self, params, alias=None, embed_alias=None):
        clauses = []
        for col, op, val in self._filters:
            # dotted col like "products.brand" -> route to embedded alias
            if "." in col and embed_alias:
                rel, c = col.split(".", 1)
                qcol = f'{embed_alias.get(rel, (alias or self._t))}."{c}"'
            else:
                qcol = f'{alias}."{col}"' if alias else f'"{col}"'
            if op == "is":
                if val in (None, "null"):        clauses.append(f"{qcol} IS NULL")
                elif val in ("not.null",):        clauses.append(f"{qcol} IS NOT NULL")
                elif val is True:                 clauses.append(f"{qcol} IS TRUE")
                elif val is False:                clauses.append(f"{qcol} IS FALSE")
                else:                             clauses.append(f"{qcol} IS NULL")
            elif op == "in":
                clauses.append(f"{qcol} = ANY(%s)"); params.append(val)
            else:
                clauses.append(f"{qcol} {op} %s"); params.append(val)
        return (" WHERE " + " AND ".join(clauses)) if clauses else ""

    def _build_select(self, params):
        # Embedded-join select?  e.g. "id, product_id, products!inner(url, brand)"
        if "!" in self._sel:
            parts = _split_top_level(self._sel)
            top_cols, embeds = [], []
            for p in parts:
                if "!" in p:
                    head, coltxt = p.split("(", 1)
                    relname, jtype = head.split("!")
                    cols = [c.strip() for c in coltxt.rstrip(")").split(",") if c.strip()]
                    embeds.append((relname.strip(), jtype.strip(), cols))
                else:
                    top_cols.append(p.strip())
            base = "t"
            select_bits = [f't."{c}"' for c in top_cols]
            joins, embed_alias = [], {}
            for i, (rel, jtype, cols) in enumerate(embeds):
                ja = f"j{i}"; embed_alias[rel] = ja
                child_col, parent_col = self._c._resolve_fk(self._t, rel)
                jkw = "LEFT JOIN" if jtype == "left" else "INNER JOIN"
                joins.append(f'{jkw} "{rel}" {ja} ON t."{child_col}" = {ja}."{parent_col}"')
                obj = ", ".join([f"'{c}', {ja}.\"{c}\"" for c in cols])
                select_bits.append(f"json_build_object({obj})::json AS \"{rel}\"")
            sql = f'SELECT {", ".join(select_bits)} FROM "{self._t}" t ' + " ".join(joins)
            sql += self._where(params, alias=base, embed_alias=embed_alias)
        else:
            cols = "*" if self._sel.strip() == "*" else self._sel
            sql = f'SELECT {cols} FROM "{self._t}"' + self._where(params)
        if self._order:
            sql += " ORDER BY " + ", ".join(
                f'"{c}"{" DESC" if d else ""}' for c, d in self._order)
        if self._limit is not None:
            sql += f" LIMIT {int(self._limit)}"
        if self._offset:
            sql += f" OFFSET {int(self._offset)}"
        return sql

    def _cols_union(self):
        cols = []
        for r in self._payload:
            for k in r.keys():
                if k not in cols:
                    cols.append(k)
        return cols

    def _build_write(self, params):
        if self._op in ("insert", "upsert"):
            cols = self._cols_union()
            collist = ", ".join(f'"{c}"' for c in cols)
            rowsql = []
            for r in self._payload:
                ph = []
                for c in cols:
                    params.append(_adapt(r.get(c)))
                    ph.append("%s")
                rowsql.append("(" + ", ".join(ph) + ")")
            sql = f'INSERT INTO "{self._t}" ({collist}) VALUES ' + ", ".join(rowsql)
            if self._op == "upsert" and self._on_conflict:
                keys = [k.strip() for k in self._on_conflict.split(",")]
                upd = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c not in keys)
                conflict = ", ".join(f'"{k}"' for k in keys)
                if upd:
                    sql += f" ON CONFLICT ({conflict}) DO UPDATE SET {upd}"
                else:
                    sql += f" ON CONFLICT ({conflict}) DO NOTHING"
            elif self._op == "upsert":
                sql += " ON CONFLICT DO NOTHING"
            sql += " RETURNING *"
            return sql
        if self._op == "update":
            setbits = []
            for c, v in self._payload.items():
                setbits.append(f'"{c}" = %s'); params.append(_adapt(v))
            sql = f'UPDATE "{self._t}" SET ' + ", ".join(setbits) + self._where(params) + " RETURNING *"
            return sql
        if self._op == "delete":
            return f'DELETE FROM "{self._t}"' + self._where(params) + " RETURNING *"
        raise ValueError(f"unknown op {self._op}")

    def execute(self):
        params = []
        if self._op == "select":
            sql = self._build_select(params)
        else:
            sql = self._build_write(params)
        rows = self._c._run(sql, params)
        cnt = None
        if self._count == "exact":
            cp = []
            csql = f'SELECT count(*) AS n FROM "{self._t}"' + self._where(cp)
            cnt = (self._c._run(csql, cp) or [{"n": 0}])[0]["n"]
        data = [dict(r) for r in rows]
        return _Result(data, cnt)


class Client:
    def __init__(self, dsn):
        self._dsn = dsn
        self._conn = None
        self._fk_cache = dict(_REL)

    def _connect(self):
        self._conn = psycopg2.connect(self._dsn, connect_timeout=30)
        self._conn.autocommit = True

    def _run(self, sql, params):
        for attempt in range(2):
            try:
                if self._conn is None or self._conn.closed:
                    self._connect()
                with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    if cur.description:
                        return cur.fetchall()
                    return []
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                try: self._conn.close()
                except Exception: pass
                self._conn = None
                if attempt == 1:
                    raise
                time.sleep(1)

    def _resolve_fk(self, child, parent):
        if (child, parent) in self._fk_cache:
            return self._fk_cache[(child, parent)]
        rows = self._run("""
            SELECT kcu.column_name AS child_col, ccu.column_name AS parent_col
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name=ccu.constraint_name
            WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_name=%s AND ccu.table_name=%s
            LIMIT 1""", [child, parent])
        if rows:
            pair = (rows[0]["child_col"], rows[0]["parent_col"])
            self._fk_cache[(child, parent)] = pair
            return pair
        raise ValueError(f"No FK relationship {child} -> {parent} for embedded select")

    def table(self, name):
        return _Query(self, name)

    def rpc(self, fn, params=None):
        # Supabase .rpc("fn", {args}).execute()  ->  SELECT fn(args)
        params = params or {}
        keys = list(params.keys())
        args = ", ".join(f"{k} => %s" for k in keys)
        sql = f'SELECT * FROM "{fn}"({args})' if keys else f'SELECT * FROM "{fn}"()'
        vals = [_adapt(params[k]) for k in keys]
        q = _Query(self, fn)
        q._op = "_rpc_done"
        rows = self._run(sql, vals)
        q.execute = lambda: _Result([dict(r) for r in (rows or [])])
        return q


def create_client(*args, **kwargs):
    """Signature-compatible with supabase.create_client(url, key) — args ignored;
    connection comes from KHABAR_DB_URL (or SUPABASE_DB_URL)."""
    if not _DB_URL:
        raise RuntimeError("KHABAR_DB_URL (or SUPABASE_DB_URL) must be set")
    return Client(_DB_URL)
