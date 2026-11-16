"""Agregados por financiador (o que alimenta os gráficos e o ranking).

Mesma forma do payload que a página /investimentos do FomentaBR já usa:
kpis, by_year, by_instrument, by_status, by_uf, instruments, rankings, coverage.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

import duckdb

UF_REGION = {
    "AC": "Norte", "AP": "Norte", "AM": "Norte", "PA": "Norte", "RO": "Norte", "RR": "Norte", "TO": "Norte",
    "AL": "Nordeste", "BA": "Nordeste", "CE": "Nordeste", "MA": "Nordeste", "PB": "Nordeste",
    "PE": "Nordeste", "PI": "Nordeste", "RN": "Nordeste", "SE": "Nordeste",
    "DF": "Centro-Oeste", "GO": "Centro-Oeste", "MT": "Centro-Oeste", "MS": "Centro-Oeste",
    "ES": "Sudeste", "MG": "Sudeste", "RJ": "Sudeste", "SP": "Sudeste",
    "PR": "Sul", "RS": "Sul", "SC": "Sul",
}
RANKING_LIMIT = 30
_ROLE_PREF = ("executor", "ambos", "proponente")


def _norm_inst(name: str | None) -> str:
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", str(name)).encode("ASCII", "ignore").decode("ASCII")
    return " ".join(re.sub(r"[^a-zA-Z0-9\s]", " ", n).lower().split())


def _rnd(d: dict) -> dict:
    for k in ("approved", "released"):
        if k in d:
            d[k] = round(d[k], 2)
    return d


def build_summary(con: duckdb.DuckDBPyConnection, source_id: str) -> dict[str, Any]:
    released_by_year = dict(con.execute(
        "SELECT year(d.paid_on), sum(d.value) FROM funding_disbursement d JOIN funding_award a ON a.id = d.award_id "
        "WHERE a.source_id = ? AND d.paid_on IS NOT NULL GROUP BY 1", [source_id]).fetchall())
    links: dict[int, list[dict]] = {}
    for award_id, cnpj, role, name, initials in con.execute(
        "SELECT l.award_id, l.institution_cnpj, l.role, i.name, i.initials FROM funding_award_institution l "
        "JOIN funding_award a ON a.id = l.award_id JOIN institution i ON i.cnpj = l.institution_cnpj "
        "WHERE a.source_id = ?", [source_id]).fetchall():
        links.setdefault(award_id, []).append({"cnpj": cnpj, "role": role, "name": name, "initials": initials})
    awards = con.execute(
        "SELECT id, approved_value, released_value, instrument, status, uf, year(signing_date), executor_name, "
        "proponent_name FROM funding_award WHERE source_id = ?", [source_id]).fetchall()

    by_instrument: dict[str, dict] = {}
    by_status: dict[str, dict] = {}
    by_uf: dict[str, dict] = {}
    by_year: dict[int, dict] = {}
    insts: dict[str, dict] = {}
    insts_instr: dict[str, dict[str, dict]] = {}
    approved_total = released_total = 0.0

    def acc(store: dict, key: str, registered: bool, name: str, initials: str | None, cnpj: str | None,
            released: float, approved: float) -> None:
        agg = store.get(key)
        if agg is None:
            agg = store[key] = {"key": key, "name": name, "cnpj": cnpj, "initials": initials,
                                "registered": registered, "released": 0.0, "approved": 0.0, "projects": 0}
        elif not registered and len(name) > len(agg["name"]):
            agg["name"] = name
        agg["released"] += released
        agg["approved"] += approved
        agg["projects"] += 1

    for award_id, approved_v, released_v, instrument, status, uf, year, exec_name, prop_name in awards:
        approved, released = float(approved_v or 0), float(released_v or 0)
        approved_total += approved
        released_total += released
        ik = instrument or "(não informado)"
        d = by_instrument.setdefault(ik, {"label": ik, "projects": 0, "approved": 0.0, "released": 0.0})
        d["projects"] += 1
        d["approved"] += approved
        d["released"] += released
        sk = (status or "(não informado)").strip().upper()
        by_status.setdefault(sk, {"label": sk.title(), "projects": 0})["projects"] += 1
        if uf:
            du = by_uf.setdefault(uf, {"uf": uf, "region": UF_REGION.get(uf, "—"), "projects": 0,
                                       "approved": 0.0, "released": 0.0})
            du["projects"] += 1
            du["approved"] += approved
            du["released"] += released
        if year:
            dy = by_year.setdefault(int(year), {"year": int(year), "approved": 0.0, "released": 0.0, "projects": 0})
            dy["approved"] += approved
            dy["projects"] += 1
        lk = links.get(award_id) or []
        chosen = next((x for want in _ROLE_PREF for x in lk if x["role"] == want), None)
        if chosen:
            key, registered = "cnpj:" + chosen["cnpj"], True
            name, initials, cnpj = chosen["name"], chosen["initials"], chosen["cnpj"]
        else:
            raw = exec_name or prop_name
            key, registered = "name:" + _norm_inst(raw), False
            name, initials, cnpj = (raw or "(sem instituição)").strip(), None, None
        acc(insts, key, registered, name, initials, cnpj, released, approved)
        acc(insts_instr.setdefault(ik, {}), key, registered, name, initials, cnpj, released, approved)

    for y, v in released_by_year.items():
        by_year.setdefault(int(y), {"year": int(y), "approved": 0.0, "released": 0.0, "projects": 0})["released"] += v

    by_instrument_l = sorted((_rnd(v) for v in by_instrument.values()), key=lambda x: -x["released"])
    by_year_l = sorted((_rnd(v) for v in by_year.values()), key=lambda x: x["year"])

    def ranking(inst_map: dict[str, dict]) -> dict:
        rows = sorted((_rnd(dict(v)) for v in inst_map.values()), key=lambda x: -x["released"])
        n_total = len(inst_map)
        n_reg = sum(1 for v in inst_map.values() if v["registered"])
        rel_tot = sum(v["released"] for v in inst_map.values())
        rel_reg = sum(v["released"] for v in inst_map.values() if v["registered"])
        app_tot = sum(v["approved"] for v in inst_map.values())
        app_reg = sum(v["approved"] for v in inst_map.values() if v["registered"])
        return {"institutions": rows[:RANKING_LIMIT], "coverage": {
            "institutions_total": n_total, "institutions_registered": n_reg,
            "institutions_registered_pct": round(n_reg / n_total, 4) if n_total else 0,
            "released_total": round(rel_tot, 2), "released_to_registered": round(rel_reg, 2),
            "released_to_registered_pct": round(rel_reg / rel_tot, 4) if rel_tot else 0,
            "approved_total": round(app_tot, 2), "approved_to_registered": round(app_reg, 2),
            "approved_to_registered_pct": round(app_reg / app_tot, 4) if app_tot else 0,
        }}

    rankings = {"all": ranking(insts)}
    for label in (it["label"] for it in by_instrument_l):
        if label in insts_instr:
            rankings[label] = ranking(insts_instr[label])
    coverage = rankings["all"]["coverage"]
    years = [y["year"] for y in by_year_l]
    return {
        "kpis": {"approved_total": round(approved_total, 2), "released_total": round(released_total, 2),
                 "projects": len(awards), "institutions": coverage["institutions_total"],
                 "year_min": min(years) if years else None, "year_max": max(years) if years else None},
        "by_year": by_year_l,
        "by_instrument": by_instrument_l,
        "by_status": sorted(by_status.values(), key=lambda x: -x["projects"]),
        "by_uf": sorted((_rnd(v) for v in by_uf.values()), key=lambda x: -x["released"]),
        "instruments": [it["label"] for it in by_instrument_l],
        "rankings": rankings,
        "coverage": coverage,
    }
