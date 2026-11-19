"""Consultas da API sobre o `fomenta.duckdb` (somente leitura).

Uma conexão por instância da função; cada requisição usa um cursor próprio
(as rotas são síncronas e o FastAPI as roda em threads). Todo valor vindo da
requisição entra como parâmetro, nunca concatenado no SQL.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb

DB_PATH = Path(os.environ.get("FOMENTA_DB", Path(__file__).resolve().parents[1] / "build" / "fomenta.duckdb"))
MAX_PER_PAGE = 100


@lru_cache(maxsize=1)
def _connection() -> duckdb.DuckDBPyConnection:
    tabelas = sorted(DB_PATH.with_name("tabelas").glob("*.parquet"))
    if not tabelas:
        return duckdb.connect(str(DB_PATH), read_only=True)
    # Elos e publicações vêm em Parquet ao lado do banco (ver pipeline/build.py: PARQUET_PREFIXES).
    # Banco em memória com uma view por arquivo e o fomenta.duckdb anexado só para leitura; com o
    # search_path, todas as tabelas são lidas pelo nome simples, inclusive em JOIN.
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{DB_PATH.as_posix()}' AS fomenta (READ_ONLY)")
    for f in tabelas:
        con.execute(f"CREATE VIEW {f.stem} AS SELECT * FROM read_parquet('{f.as_posix()}')")
    con.execute("SET parquet_metadata_cache = true")
    return con


@lru_cache(maxsize=1)
def _search_path() -> str | None:
    names = {r[0] for r in _connection().execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    return "SET search_path = 'memory.main,fomenta.main'" if "fomenta" in names and "memory" in names else None


def cursor() -> duckdb.DuckDBPyConnection:
    cur = _connection().cursor()
    sp = _search_path()
    if sp:
        cur.execute(sp)  # vale por conexão: cada cursor precisa do seu
    return cur


def _iso(v: Any) -> str | None:
    if isinstance(v, datetime):
        return v.isoformat() + "Z"
    if isinstance(v, date):
        return v.isoformat()
    return v


def _money(v: Any) -> float | None:
    return round(float(v), 2) if v is not None else None


def list_funders() -> list[dict[str, Any]]:
    rows = cursor().execute(
        "SELECT id, name, notes, data_as_of, imported_at, summary->'kpis' FROM funding_source ORDER BY sort_order"
    ).fetchall()
    return [{"id": r[0], "name": r[1], "notes": r[2], "data_as_of": _iso(r[3]), "imported_at": _iso(r[4]),
             "kpis": json.loads(r[5])} for r in rows]


def get_funder(funder_id: str) -> dict[str, Any] | None:
    r = cursor().execute(
        "SELECT id, name, source_urls, notes, data_as_of, imported_at, summary FROM funding_source WHERE id = ?",
        [funder_id]).fetchone()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "sources": json.loads(r[2]), "notes": r[3], "data_as_of": _iso(r[4]),
            "imported_at": _iso(r[5]), **json.loads(r[6])}


def list_awards(*, funder: str | None, year: int | None, uf: str | None, instrument: str | None,
                cnpj: str | None, q: str | None, page: int, per_page: int) -> dict[str, Any]:
    per_page = max(1, min(per_page, MAX_PER_PAGE))
    page = max(page, 1)
    where, params = ["true"], []
    if funder:
        where.append("a.source_id = ?")
        params.append(funder)
    if year:
        where.append("year(a.signing_date) = ?")
        params.append(year)
    if uf:
        where.append("a.uf = ?")
        params.append(uf.upper()[:2])
    if instrument:
        where.append("a.instrument = ?")
        params.append(instrument)
    if cnpj:
        where.append("a.id IN (SELECT award_id FROM funding_award_institution WHERE institution_cnpj = ?)")
        params.append("".join(ch for ch in cnpj if ch.isdigit()))
    if q:
        where.append("(a.title ILIKE ? OR a.executor_name ILIKE ? OR a.proponent_name ILIKE ?)")
        params += [f"%{q.strip()[:80]}%"] * 3
    w = " AND ".join(where)
    cur = cursor()
    total = cur.execute(f"SELECT count(*) FROM funding_award a WHERE {w}", params).fetchone()[0]
    rows = cur.execute(
        "SELECT source_id, source_key, title, instrument, program, executor_name, proponent_name, city, uf, "
        "signing_date, approved_value, released_value, status, extra "
        f"FROM funding_award a WHERE {w} ORDER BY approved_value DESC NULLS LAST, id LIMIT ? OFFSET ?",
        [*params, per_page, (page - 1) * per_page]).fetchall()
    return {"total": total, "page": page, "per_page": per_page, "items": [{
        "funder": r[0], "key": r[1], "title": r[2], "instrument": r[3], "program": r[4], "executor": r[5],
        "proponent": r[6], "city": r[7], "uf": r[8], "signed": _iso(r[9]), "approved": _money(r[10]),
        "released": _money(r[11]), "status": r[12], "detail": json.loads(r[13]) if r[13] else None,
    } for r in rows]}


def institution_investments(cnpj: str) -> dict[str, Any] | None:
    digits = "".join(ch for ch in cnpj if ch.isdigit())
    cur = cursor()
    inst = cur.execute("SELECT cnpj, name, initials, uf, city FROM institution WHERE cnpj = ?", [digits]).fetchone()
    if not inst:
        return None
    by_funder: dict[str, dict[str, Any]] = {}
    for fid, year, projects, approved, released in cur.execute(
        "SELECT source_id, year, projects, approved, released FROM inst_funder_year "
        "WHERE institution_cnpj = ? ORDER BY source_id, year", [digits]).fetchall():
        f = by_funder.setdefault(fid, {"funder": fid, "projects": 0, "approved": 0.0, "released": 0.0, "by_year": []})
        f["projects"] += projects
        f["approved"] += approved or 0
        f["released"] += released or 0
        f["by_year"].append({"year": year, "projects": projects, "approved": _money(approved),
                             "released": _money(released)})
    for f in by_funder.values():
        f["approved"], f["released"] = round(f["approved"], 2), round(f["released"], 2)
    return {
        "institution": {"cnpj": inst[0], "name": inst[1], "initials": inst[2], "uf": inst[3], "city": inst[4]},
        "funders": list(by_funder.values()),
        "note": "Valores nominais por financiador; não some fontes diferentes (parte do FNDCT passa por mais de uma).",
    }


def meta() -> dict[str, Any]:
    p = DB_PATH.parent / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
