"""Etapa 6 do plano do OpenAlex: indicadores de impacto e congressos <-> veículos de publicação.

Sem coleta: tudo sai das publicações já no retrato (etapa 1).

- `ind_laboratorio` e `ind_instituicao`: publicações desde 2019 do responsável e da equipe (por
  laboratório) e dos pesquisadores da base com o CNPJ (por instituição): citações, FWCI médio e
  mediano (a média com cada FWCI aparado em 10: artigos de consórcio passam de 500), % no top 10% e no
  top 1% da área, % em acesso aberto, % com financiador, autores por publicação e a posição do % no
  top 10% (indicador PP(top 10%) do Leiden Ranking) entre os pares com 10+ publicações.
- `congresso_veiculo`: congresso da plataforma <-> conferência em que a base publica (OpenAlex), pela
  sigla igual e pelo menos metade das palavras do nome em comum, só veículos do tipo conferência
  (descarta revistas de sigla parecida e trilhas de workshop).

    python tools/indicadores_congressos.py exportar     # depois das obras
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "build" / "snapshot"
MIN_PARES = 10  # publicações para entrar na posição relativa
JACCARD_MIN = 0.5
STOP = set("the of on and in for de da do das dos e em na no international conference congress congresso simposio "
           "symposium workshop annual ieee acm edition anais proceedings brazilian brasileiro nacional encontro "
           "meeting".split())


def _words(t: str | None) -> set[str]:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\b\d+(st|nd|rd|th|a|o)?\b", " ", t)
    return set(re.findall(r"[a-z][a-z0-9&-]+", t)) - STOP


def _acronym(a: str | None) -> str | None:
    a = re.sub(r"[’']\d{2,4}|\b\d{2,4}\b|IEEE|ACM|-\s*Workshops?.*$|Workshop \d+|\d+(st|nd|rd|th)", " ", a or "")
    toks = re.findall(r"[A-Za-z][A-Za-z0-9&/-]{2,}", a)
    return toks[0].lower() if toks else None


FWCI_TETO = 10  # artigos de consórcio (centenas de autores) chegam a FWCI > 500 e dominariam a média
INDICADORES = """
    count(*) AS publicacoes, sum(w.citacoes) AS citacoes,
    round(avg(least(w.fwci, FWCI_TETO)), 3) AS fwci_medio, round(median(w.fwci), 3) AS fwci_mediano,
    round(100.0 * count(*) FILTER (WHERE w.top10) / nullif(count(w.percentil), 0), 1) AS pct_top10,
    round(100.0 * count(*) FILTER (WHERE w.top1) / nullif(count(w.percentil), 0), 1) AS pct_top1,
    round(100.0 * count(*) FILTER (WHERE w.acesso_aberto) / count(*), 1) AS pct_acesso_aberto,
    round(100.0 * count(*) FILTER (WHERE w.id IN (SELECT work_id FROM wf)) / count(*), 1) AS pct_com_financiador,
    median(w.n_autores) AS autores_mediana, min(w.ano) AS desde, max(w.ano) AS ate"""


def exportar() -> None:
    import duckdb

    s = SNAPSHOT.as_posix()
    con = duckdb.connect()
    for t in ("oa_work", "oa_work_author", "oa_work_funder", "elos_researcher_lab", "elos_researcher",
              "congresso", "pnipe_laboratory"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM '{s}/{t}.parquet'")
    con.execute("CREATE TEMP TABLE wf AS SELECT DISTINCT work_id FROM oa_work_funder")

    def rank(table: str) -> None:
        con.execute(f"""CREATE OR REPLACE TABLE {table} AS SELECT *,
            CASE WHEN publicacoes >= {MIN_PARES} AND pct_top10 IS NOT NULL THEN
              round(100.0 * percent_rank() OVER (PARTITION BY publicacoes >= {MIN_PARES} AND pct_top10 IS NOT NULL
                                                 ORDER BY pct_top10), 1) END AS posicao_top10
            FROM {table}_""")

    con.execute(f"""CREATE TABLE ind_laboratorio_ AS
        WITH w AS (SELECT DISTINCT rl.lab_id, a.work_id FROM elos_researcher_lab rl
                   JOIN oa_work_author a ON a.researcher_id = rl.researcher_id
                   WHERE rl.papel <> 'coautor' AND rl.confianca >= 0.8)
        SELECT w.lab_id, {INDICADORES.replace('w.', 'x.').replace('FWCI_TETO', str(FWCI_TETO))}
        FROM w JOIN oa_work x ON x.id = w.work_id GROUP BY 1""")
    rank("ind_laboratorio")
    con.execute(f"""CREATE TABLE ind_instituicao_ AS
        WITH w AS (SELECT DISTINCT r.inst_cnpj, a.work_id FROM elos_researcher r
                   JOIN oa_work_author a ON a.researcher_id = r.id
                   WHERE r.origem <> 'coautor' AND r.inst_cnpj IS NOT NULL)
        SELECT w.inst_cnpj AS cnpj, {INDICADORES.replace('w.', 'x.').replace('FWCI_TETO', str(FWCI_TETO))}
        FROM w JOIN oa_work x ON x.id = w.work_id GROUP BY 1""")
    rank("ind_instituicao")

    # Congressos <-> conferências em que a base publica.
    venues = con.execute("SELECT veiculo_id, any_value(veiculo), count(*) FROM oa_work "
                         "WHERE veiculo_tipo = 'conference' GROUP BY 1").fetchall()
    by_acr: dict[str, list] = {}
    for vid, nome, n in venues:
        acrs = set(re.findall(r"\(([A-Za-z][A-Za-z0-9&/ -]{1,20})\)", nome)) | set(re.findall(r"\b[A-Z][A-Z0-9&]{2,}\b", nome))
        for a in acrs:
            k = _acronym(a)
            if k:
                by_acr.setdefault(k, []).append((vid, nome, n))
    pares = []
    for cid, title, acr in con.execute("SELECT id, title, acronym FROM congresso").fetchall():
        tw = _words(title)
        best = None
        for vid, nome, n in by_acr.get(_acronym(acr) or "", []):
            vw = _words(nome)
            j = len(tw & vw) / max(1, len(tw | vw))
            if j >= JACCARD_MIN and (best is None or j > best[2]):
                best = (vid, nome, j)
        if best:
            pares.append((cid, best[0], best[1], round(best[2], 2)))
    con.execute("CREATE TABLE congresso_veiculo (congresso_id BIGINT, veiculo_id VARCHAR, veiculo VARCHAR, semelhanca DOUBLE)")
    if pares:
        con.executemany("INSERT INTO congresso_veiculo VALUES (?, ?, ?, ?)", pares)

    meta_path = SNAPSHOT / "snapshot.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for t in ("ind_laboratorio", "ind_instituicao", "congresso_veiculo"):
        con.execute(f"COPY {t} TO '{(SNAPSHOT / f'{t}.parquet').as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
        meta["tables"][t] = {"rows": con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]}
    futuros = con.execute("SELECT count(*) FROM congresso_veiculo v JOIN congresso c ON c.id = v.congresso_id "
                          "WHERE c.start_date >= current_date").fetchone()[0]
    meta["indicadores_congressos"] = {
        "calculado_em": datetime.now(UTC).isoformat(timespec="seconds"),
        "laboratorios": meta["tables"]["ind_laboratorio"]["rows"], "instituicoes": meta["tables"]["ind_instituicao"]["rows"],
        "congressos_com_veiculo": len(pares), "congressos_futuros_com_veiculo": futuros,
        "congressos": con.execute("SELECT count(*) FROM congresso").fetchone()[0],
        "regras": {"min_pares": MIN_PARES, "jaccard_min": JACCARD_MIN, "fwci_teto": FWCI_TETO}}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta["indicadores_congressos"], ensure_ascii=False, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("exportar")
    p.parse_args()
    exportar()


if __name__ == "__main__":
    main()
