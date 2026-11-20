"""Etapa 4 do plano do OpenAlex: perfil completo dos coautores de fora da base.

Os coautores (`elos_researcher.origem = 'coautor'`) entraram só com nome, ORCID e a instituição da
publicação em comum mais recente. Aqui ganham publicações, citações, h-index, publicações desde
2019, último ano com publicação e a instituição atual (OpenAlex, 100 autores por chamada, ~US$ 0,40).
E cada vínculo coautor -> laboratório ganha `anos_em_comum` (anos distintos com publicação junto ao
responsável) e `colaborador_estavel` (2 anos ou mais): separa o aluno que publicou uma vez de quem
trabalha com o laboratório. É uma inferência, como a "equipe provável".

    python tools/coautores_perfis.py ids --saida coautores.txt.gz                 # aqui
    PYTHONPATH=src python <fomenta-api>/tools/coautores_perfis.py coletar coautores.txt.gz \\
        --saida coautores.jsonl.gz                                                 # no FomentaBR
    python tools/coautores_perfis.py exportar coautores.jsonl.gz                  # aqui, depois das obras
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "build" / "snapshot"
BATCH = 100
SELECT = "id,display_name,orcid,works_count,cited_by_count,summary_stats,last_known_institutions,counts_by_year"
ANOS_ESTAVEL = 2


def _sid(url: str | None) -> str | None:
    return url.rstrip("/").rsplit("/", 1)[-1] if url else None


def ids(saida: Path) -> None:
    import duckdb

    rows = duckdb.connect().execute(
        f"SELECT DISTINCT unnest(openalex_ids) FROM '{(SNAPSHOT / 'elos_researcher.parquet').as_posix()}' "
        "WHERE origem = 'coautor' ORDER BY 1").fetchall()
    with gzip.open(saida, "wt", encoding="utf-8") as f:
        f.write("\n".join(r[0] for r in rows))
    print(f"{len(rows)} ids de coautores -> {saida}")


def coletar(entrada: Path, saida: Path) -> None:
    from fomenta.config import settings
    from fomenta.elos.openalex import BudgetExceededError, OpenAlexClient

    if not settings.openalex_api_key:
        sys.exit("Defina OPENALEX_API_KEY no .env do FomentaBR.")
    with gzip.open(entrada, "rt", encoding="utf-8") as f:
        todos = [x for x in f.read().split("\n") if x]
    client = OpenAlexClient(api_key=settings.openalex_api_key, cache_dir=settings.openalex_cache_dir,
                            daily_budget_usd=settings.openalex_daily_budget_usd)
    n = 0
    try:
        with gzip.open(saida, "wt", encoding="utf-8") as out:
            for i in range(0, len(todos), BATCH):
                lote = todos[i:i + BATCH]
                d = client.get("authors", filter=f"openalex:{'|'.join(lote)}", per_page=BATCH, select=SELECT)
                for a in d["results"]:
                    inst = (a.get("last_known_institutions") or [{}])[0] or {}
                    anos = [y for y in a.get("counts_by_year") or [] if y.get("works_count")]
                    st = a.get("summary_stats") or {}
                    out.write(json.dumps({
                        "id": _sid(a["id"]), "orcid": (a.get("orcid") or "").rsplit("/", 1)[-1] or None,
                        "publicacoes": a.get("works_count"), "citacoes": a.get("cited_by_count"),
                        "h_index": st.get("h_index"), "i10": st.get("i10_index"),
                        "desde_2019": sum(y["works_count"] for y in anos if y["year"] >= 2019),
                        "ultimo_ano": max((y["year"] for y in anos), default=None),
                        "inst_oa": _sid(inst.get("id")), "inst_nome": inst.get("display_name"),
                        "inst_ror": inst.get("ror"), "inst_pais": inst.get("country_code"),
                    }, ensure_ascii=False) + "\n")
                    n += 1
                if (i // BATCH) % 500 == 0:
                    print(f"{i + len(lote)}/{len(todos)} ids, {n} perfis, US$ {client.spent_usd:.4f}", flush=True)
    except BudgetExceededError as e:
        print(f"Parado pelo teto diário ({e}). Rode de novo amanhã: o cache retoma.")
        sys.exit(2)
    print(f"pronto: {n} perfis de {len(todos)} ids, {client.requests} requisições, US$ {client.spent_usd:.4f}")


def exportar(entrada: Path) -> None:
    import duckdb

    s = SNAPSHOT.as_posix()
    con = duckdb.connect()
    con.execute(f"CREATE TABLE perfil AS SELECT * FROM read_json_auto('{entrada.as_posix()}', format='newline_delimited')")
    # Instituição atual -> CNPJ pelo casamento do Elos (OpenAlex id ou ROR).
    con.execute(f"""
        CREATE TABLE r AS
        WITH p AS (SELECT r.id rid, p.*, row_number() OVER (PARTITION BY r.id ORDER BY p.publicacoes DESC NULLS LAST) k
                   FROM '{s}/elos_researcher.parquet' r, unnest(r.openalex_ids) AS u(oa)
                   JOIN perfil p ON p.id = u.oa WHERE r.origem = 'coautor'),
             -- uma instituição do OpenAlex pode casar com vários CNPJs (unidades): fica o de maior confiança
             io AS (SELECT openalex_id, arg_max(cnpj, confianca) cnpj FROM '{s}/elos_institution.parquet'
                    WHERE openalex_id IS NOT NULL GROUP BY 1),
             ir AS (SELECT ror, arg_max(cnpj, confianca) cnpj FROM '{s}/elos_institution.parquet'
                    WHERE ror IS NOT NULL GROUP BY 1)
        SELECT r.* REPLACE (
                 coalesce(r.orcid, p.orcid) AS orcid,
                 CASE WHEN p.rid IS NULL THEN r.publicacoes ELSE p.publicacoes END AS publicacoes,
                 CASE WHEN p.rid IS NULL THEN r.citacoes ELSE p.citacoes END AS citacoes,
                 CASE WHEN p.rid IS NULL THEN r.h_index ELSE p.h_index END AS h_index,
                 CASE WHEN p.rid IS NULL THEN r.desde_2019 ELSE p.desde_2019 END AS desde_2019,
                 -- pela instituição atual; sem ela, pela da publicação em comum (o casamento pode ter crescido)
                 coalesce(io.cnpj, ir.cnpj, CASE WHEN p.inst_oa IS NULL THEN r.inst_cnpj END) AS inst_cnpj,
                 coalesce(p.inst_oa, r.instituicao_openalex) AS instituicao_openalex,
                 coalesce(p.inst_nome, r.instituicao_nome) AS instituicao_nome,
                 coalesce(pi.initials, CASE WHEN p.inst_oa IS NULL THEN r.instituicao_sigla END) AS instituicao_sigla,
                 coalesce(p.inst_pais, r.instituicao_pais) AS instituicao_pais),
               p.rid IS NOT NULL AS _perfil
        FROM '{s}/elos_researcher.parquet' r
        LEFT JOIN p ON p.rid = r.id AND p.k = 1
        LEFT JOIN io ON io.openalex_id = coalesce(p.inst_oa, r.instituicao_openalex)
        LEFT JOIN ir ON ir.ror = p.inst_ror AND io.cnpj IS NULL
        LEFT JOIN '{s}/pnipe_institution.parquet' pi ON pi.cnpj = coalesce(io.cnpj, ir.cnpj)""")
    n_total = con.execute("SELECT count(*) FROM r").fetchone()[0]
    n_before = con.execute(f"SELECT count(*) FROM '{s}/elos_researcher.parquet'").fetchone()[0]
    assert n_total == n_before, (n_total, n_before)

    # Anos distintos de publicação junto ao responsável do laboratório (publicações desde 2019).
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{s}/elos_researcher_lab.parquet'").fetchall()]
    base = ", ".join(c for c in cols if c not in ("anos_em_comum", "colaborador_estavel"))  # rodar de novo
    con.execute(f"""
        CREATE TABLE rl AS
        WITH anos AS (
          SELECT x.researcher_id, x.lab_id, count(DISTINCT w.ano) anos
          FROM '{s}/elos_researcher_lab.parquet' x
          JOIN '{s}/oa_work_author.parquet' a1 ON a1.researcher_id = x.researcher_id
          JOIN '{s}/oa_work_author.parquet' a2 ON a2.work_id = a1.work_id AND a2.researcher_id = x.responsavel_id
          JOIN '{s}/oa_work.parquet' w ON w.id = a1.work_id
          WHERE x.papel = 'coautor' GROUP BY 1, 2)
        SELECT x.*, anos.anos AS anos_em_comum,
               CASE WHEN x.papel = 'coautor' THEN coalesce(anos.anos, 0) >= {ANOS_ESTAVEL} END AS colaborador_estavel
        FROM (SELECT {base} FROM '{s}/elos_researcher_lab.parquet') x
        LEFT JOIN anos ON anos.researcher_id = x.researcher_id AND anos.lab_id = x.lab_id""")

    stats = dict(zip(("coautores", "com_perfil", "com_instituicao", "instituicao_na_base"), con.execute(
        "SELECT count(*), count(*) FILTER (WHERE _perfil), count(instituicao_openalex), count(inst_cnpj) "
        "FROM r WHERE origem = 'coautor'").fetchone()))
    est = con.execute("SELECT count(*) FILTER (WHERE colaborador_estavel), count(*) FROM rl WHERE papel = 'coautor'").fetchone()
    for name, sql in (("elos_researcher", "SELECT * EXCLUDE (_perfil) FROM r"), ("elos_researcher_lab", "SELECT * FROM rl")):
        tmp = SNAPSHOT / f"{name}.parquet.tmp"
        con.execute(f"COPY ({sql}) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
        tmp.replace(SNAPSHOT / f"{name}.parquet")

    meta_path = SNAPSHOT / "snapshot.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["coautores_perfis"] = {"exportado_em": datetime.now(UTC).isoformat(timespec="seconds"), **stats,
                                "vinculos_coautor": est[1], "colaboradores_estaveis": est[0],
                                "regra_estavel": f"{ANOS_ESTAVEL}+ anos distintos com publicação junto ao responsável"}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta["coautores_perfis"], ensure_ascii=False, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ids")
    a.add_argument("--saida", type=Path, required=True)
    b = sub.add_parser("coletar")
    b.add_argument("entrada", type=Path)
    b.add_argument("--saida", type=Path, required=True)
    c = sub.add_parser("exportar")
    c.add_argument("entrada", type=Path)
    x = p.parse_args()
    {"ids": lambda: ids(x.saida), "coletar": lambda: coletar(x.entrada, x.saida),
     "exportar": lambda: exportar(x.entrada)}[x.cmd]()


if __name__ == "__main__":
    main()
