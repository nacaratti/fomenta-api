"""Etapa 7 do plano do OpenAlex: sinergia entre laboratórios pela base de conhecimento (citações).

Dois laboratórios que citam as mesmas referências trabalham sobre a mesma base de conhecimento, mesmo
sem nunca terem publicado juntos (acoplamento bibliográfico); e um laboratório que cita as publicações
de outro já depende do trabalho dele (citação direta). É a terceira Sinergia, ao lado de técnicas e
equipamentos (PNIPE) e da coautoria (Elos).

- coletar (FomentaBR, ~US$ 0,17): `referenced_works` das publicações dos responsáveis desde 2019, nos
  mesmos lotes de 100 autores da etapa 1. As referências brutas não vão para a API.
- exportar (aqui): `sinergia_citacao` com os 15 pares mais fortes de cada laboratório:
  cosseno TF-IDF entre os conjuntos de referências (a referência citada por quase todos pesa pouco),
  referências em comum (3 ou mais), citações diretas nos dois sentidos e se já publicam juntos. Pares que
  compartilham gente (mesmo responsável ou equipe) ficam de fora: aí a semelhança é óbvia.

    PYTHONPATH=src python <fomenta-api>/tools/sinergia_citacao.py coletar elos.pkl.gz --saida refs.jsonl.gz
    python tools/sinergia_citacao.py exportar refs.jsonl.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import pickle
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "build" / "snapshot"
FROM = "2019-01-01"
BATCH = 100
TOP = 15
MIN_REFS = 20  # laboratório com menos referências que isso fica de fora (vetor ralo demais)
MIN_COMUM = 3  # par com menos referências em comum é ruído (acoplamento ~0,02)
MAX_DF = 0.02  # referência citada por mais de 2% dos laboratórios é genérica (métodos, estatística) e sai


def _sid(url: str | None) -> str | None:
    return url.rstrip("/").rsplit("/", 1)[-1] if url else None


def coletar(elos: Path, saida: Path) -> None:
    from fomenta.config import settings
    from fomenta.elos.openalex import BudgetExceededError, OpenAlexClient

    if not settings.openalex_api_key:
        sys.exit("Defina OPENALEX_API_KEY no .env do FomentaBR.")
    with gzip.open(elos, "rb") as f:
        data = pickle.load(f)  # gerado por elos_ampliado.py calcular nesta máquina
    ids = sorted({oa for r in data["researchers"] if r.get("origem") == "responsavel" for oa in r["openalex_ids"]})
    batches = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    client = OpenAlexClient(api_key=settings.openalex_api_key, cache_dir=settings.openalex_cache_dir,
                            daily_budget_usd=settings.openalex_daily_budget_usd)
    seen: set[str] = set()
    try:
        with gzip.open(saida, "wt", encoding="utf-8") as out:
            for n, batch in enumerate(batches, 1):
                flt = f"author.id:{'|'.join(batch)},from_publication_date:{FROM}"
                for w in client.iterate("works", flt, "id,referenced_works", sort="publication_date:desc"):
                    wid = _sid(w.get("id"))
                    if wid and wid not in seen:
                        seen.add(wid)
                        out.write(json.dumps({"id": wid, "refs": [_sid(r) for r in w.get("referenced_works") or []]})
                                  + "\n")
                if n % 100 == 0:
                    print(f"lote {n}/{len(batches)}: {len(seen)} publicações, US$ {client.spent_usd:.4f}", flush=True)
    except BudgetExceededError as e:
        print(f"Parado pelo teto diário ({e}). Rode de novo amanhã: o cache retoma.")
        sys.exit(2)
    print(f"pronto: {len(seen)} publicações, {client.requests} requisições, US$ {client.spent_usd:.4f}")


def exportar(entrada: Path) -> None:
    import duckdb
    import numpy as np
    from scipy import sparse

    s = SNAPSHOT.as_posix()
    con = duckdb.connect()
    # Publicações de cada laboratório (responsável + equipe) e as pessoas de cada um.
    lab_work = con.execute(f"""
        SELECT DISTINCT rl.lab_id, a.work_id FROM '{s}/elos_researcher_lab.parquet' rl
        JOIN '{s}/oa_work_author.parquet' a ON a.researcher_id = rl.researcher_id
        WHERE rl.papel <> 'coautor' AND rl.confianca >= 0.8""").fetchall()
    people: dict[int, set[str]] = {}
    for lab, rid in con.execute(f"SELECT lab_id, researcher_id FROM '{s}/elos_researcher_lab.parquet' "
                                "WHERE papel <> 'coautor' AND confianca >= 0.8").fetchall():
        people.setdefault(lab, set()).add(rid)
    coauth = {(a, b) for a, b in con.execute(f"SELECT lab_a, lab_b FROM '{s}/elos_lab_link.parquet'").fetchall()}

    refs: dict[str, list[str]] = {}
    with gzip.open(entrada, "rt", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            refs[d["id"]] = d["refs"]
    labs = sorted({lab for lab, _ in lab_work})
    li = {lab: i for i, lab in enumerate(labs)}
    works_of: dict[int, list[str]] = {}
    for lab, w in lab_work:
        works_of.setdefault(lab, []).append(w)

    # Matriz laboratório x referência (contagem), depois TF-IDF e cosseno.
    ri: dict[str, int] = {}
    rows, cols = [], []
    for lab, ws in works_of.items():
        for w in ws:
            for r in refs.get(w, ()):
                rows.append(li[lab])
                cols.append(ri.setdefault(r, len(ri)))
    M = sparse.csr_matrix((np.ones(len(rows), dtype=np.float32), (rows, cols)), shape=(len(labs), len(ri)))
    M.sum_duplicates()
    n_refs = np.asarray(M.sum(axis=1)).ravel()
    df = np.asarray((M > 0).sum(axis=0)).ravel()
    keep = df <= max(2, MAX_DF * len(labs))
    idf = np.where(keep, np.log(len(labs) / np.maximum(df, 1)), 0).astype(np.float32)
    X = M.multiply(idf).tocsr()
    X.data = np.log1p(X.data)
    norm = np.sqrt(np.asarray(X.multiply(X).sum(axis=1)).ravel())
    X = sparse.diags(1 / np.maximum(norm, 1e-9)) @ X
    ok = n_refs >= MIN_REFS
    B = (M > 0).astype(np.float32).tocsr()

    # Citação direta: publicações do laboratório A que citam publicações do laboratório B.
    owner: dict[str, list[int]] = {}
    for lab, w in lab_work:
        owner.setdefault(w, []).append(li[lab])
    cit: dict[tuple[int, int], int] = {}
    for lab, ws in works_of.items():
        a = li[lab]
        for w in ws:
            for r in refs.get(w, ()):
                for b in owner.get(r, ()):
                    if b != a:
                        cit[(a, b)] = cit.get((a, b), 0) + 1

    out = []
    step = 500
    for start in range(0, len(labs), step):
        block = X[start:start + step] @ X.T
        block = block.toarray()
        for k in range(block.shape[0]):
            a = start + k
            if not ok[a]:
                continue
            sims = block[k]
            sims[a] = 0
            sims[~ok] = 0
            n = 0
            for b in np.argsort(-sims)[: TOP * 3]:
                if sims[b] <= 0 or n >= TOP:
                    break
                la, lb = labs[a], labs[b]
                if people.get(la, set()) & people.get(lb, set()):
                    continue  # mesma gente: semelhança óbvia
                comum = int(B[a].multiply(B[b]).sum())
                if comum < MIN_COMUM:
                    continue
                out.append({"lab_a": la, "lab_b": lb, "acoplamento": round(float(sims[b]), 4),
                            "referencias_em_comum": comum, "cita_b": cit.get((a, b), 0), "citado_por_b": cit.get((b, a), 0),
                            "publicam_juntos": (la, lb) in coauth or (lb, la) in coauth})
                n += 1
        print(f"{min(start + step, len(labs))}/{len(labs)} laboratórios, {len(out)} pares", flush=True)

    con.execute("CREATE TABLE t (lab_a BIGINT, lab_b BIGINT, acoplamento DOUBLE, referencias_em_comum INTEGER, "
                "cita_b INTEGER, citado_por_b INTEGER, publicam_juntos BOOLEAN)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [tuple(r.values()) for r in out])
    con.execute(f"COPY t TO '{(SNAPSHOT / 'sinergia_citacao.parquet').as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
    meta_path = SNAPSHOT / "snapshot.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["tables"]["sinergia_citacao"] = {"rows": len(out)}
    meta["sinergia_citacao"] = {
        "calculado_em": datetime.now(UTC).isoformat(timespec="seconds"), "laboratorios": int(ok.sum()),
        "referencias_distintas": len(ri), "referencias_genericas_fora": int((~keep).sum()), "pares": len(out),
        "pares_que_nao_publicam_juntos": sum(1 for r in out if not r["publicam_juntos"]),
        "regras": {"top": TOP, "min_refs": MIN_REFS, "max_df": MAX_DF, "min_comum": MIN_COMUM}}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta["sinergia_citacao"], ensure_ascii=False, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("coletar")
    a.add_argument("elos", type=Path)
    a.add_argument("--saida", type=Path, required=True)
    b = sub.add_parser("exportar")
    b.add_argument("entrada", type=Path)
    x = p.parse_args()
    coletar(x.elos, x.saida) if x.cmd == "coletar" else exportar(x.entrada)


if __name__ == "__main__":
    main()
