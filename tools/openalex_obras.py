"""Etapa 1 do plano do OpenAlex: as publicações completas dos responsáveis desde 2019.

Pede ao OpenAlex título, resumo, palavras-chave, ODS, financiadores e auxílios, veículo,
impacto e referências de cada publicação, em lotes de 100 autores por filtro (uma
publicação com vários responsáveis vem uma vez só). Custa ~US$ 0,10 por mil páginas de
200; o cliente do FomentaBR guarda tudo em cache e para no teto diário (rodar de novo
retoma de onde parou).

    # 1) no ambiente do FomentaBR (usa OPENALEX_API_KEY do .env; nada no banco)
    cd <FomentaBR>
    PYTHONPATH=src python <fomenta-api>/tools/openalex_obras.py coletar elos.pkl.gz --saida obras.jsonl.gz

    # 2) no ambiente da API: grava oa_* no retrato
    python tools/openalex_obras.py exportar obras.jsonl.gz

`elos.pkl.gz` é a saída de `tools/elos_ampliado.py calcular` (lista dos responsáveis).
"""
from __future__ import annotations

import argparse
import gzip
import json
import pickle
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "build" / "snapshot"
FROM = "2019-01-01"
BATCH = 100  # ids de autor por filtro (limite do OpenAlex para OR)
SELECT = (
    "id,doi,title,publication_year,publication_date,type,language,primary_location,open_access,authorships,"
    "corresponding_author_ids,cited_by_count,fwci,citation_normalized_percentile,primary_topic,keywords,"
    "sustainable_development_goals,funders,awards,referenced_works_count,abstract_inverted_index"
)
MAX_AUTHORS = 100  # o OpenAlex corta a lista de autores em 100 nas listagens


def _sid(url: str | None) -> str | None:
    return url.rstrip("/").rsplit("/", 1)[-1] if url else None


def _abstract(inv: dict[str, list[int]] | None) -> str | None:
    if not inv:
        return None
    pos = sorted((p, w) for w, ps in inv.items() for p in ps)
    return " ".join(w for _, w in pos)


def _slim(w: dict[str, Any]) -> dict[str, Any]:
    """Só o que vai para as tabelas (o cache do cliente guarda a resposta inteira)."""
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    pct = w.get("citation_normalized_percentile") or {}
    return {
        "id": _sid(w["id"]), "doi": w.get("doi"), "titulo": w.get("title"), "ano": w.get("publication_year"),
        "data": w.get("publication_date"), "tipo": w.get("type"), "idioma": w.get("language"),
        "veiculo_id": _sid(src.get("id")), "veiculo": src.get("display_name"), "veiculo_tipo": src.get("type"),
        "issn_l": src.get("issn_l"), "acesso_aberto": (w.get("open_access") or {}).get("is_oa"),
        "oa_url": (w.get("open_access") or {}).get("oa_url"), "citacoes": w.get("cited_by_count"),
        "fwci": w.get("fwci"), "percentil": pct.get("value"), "top10": pct.get("is_in_top_10_percent"),
        "top1": pct.get("is_in_top_1_percent"), "referencias": w.get("referenced_works_count"),
        "tema": _sid((w.get("primary_topic") or {}).get("id")),
        "resumo": _abstract(w.get("abstract_inverted_index")),
        "autores": [{"id": _sid((a.get("author") or {}).get("id")), "posicao": a.get("author_position"),
                     "correspondente": bool(a.get("is_corresponding")),
                     "inst": [_sid(i.get("id")) for i in a.get("institutions") or [] if i.get("id")]}
                    for a in (w.get("authorships") or [])[:MAX_AUTHORS] if (a.get("author") or {}).get("id")],
        "n_autores": len(w.get("authorships") or []),
        "palavras": [{"id": _sid(k.get("id")), "nome": k.get("display_name"), "nota": k.get("score")}
                     for k in w.get("keywords") or []],
        "ods": [{"id": _sid(s.get("id")), "nome": s.get("display_name"), "nota": s.get("score")}
                for s in w.get("sustainable_development_goals") or []],
        "financiadores": [{"id": _sid(f.get("id")), "nome": f.get("display_name"), "ror": f.get("ror")}
                          for f in w.get("funders") or []],
        "auxilios": [{"id": _sid(a.get("id")), "processo": a.get("funder_award_id"),
                      "financiador": _sid(a.get("funder_id")), "financiador_nome": a.get("funder_display_name"),
                      "nome": a.get("display_name")} for a in w.get("awards") or []],
    }


# ─────────────────────────────────────────────────────────────── fase 1: coletar
def coletar(elos: Path, saida: Path, limite: int | None) -> None:
    from fomenta.config import settings
    from fomenta.elos.openalex import BudgetExceededError, OpenAlexClient

    if not settings.openalex_api_key:
        sys.exit("Defina OPENALEX_API_KEY no .env do FomentaBR (paginação por cursor exige chave).")
    with gzip.open(elos, "rb") as f:
        data = pickle.load(f)  # gerado por elos_ampliado.py calcular nesta máquina
    ids = sorted({oa for r in data["researchers"] if r.get("origem") == "responsavel" for oa in r["openalex_ids"]})
    batches = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)][: limite or None]
    print(f"{len(ids)} perfis de responsáveis em {len(batches)} lotes")

    client = OpenAlexClient(api_key=settings.openalex_api_key, cache_dir=settings.openalex_cache_dir,
                            daily_budget_usd=settings.openalex_daily_budget_usd)
    seen: set[str] = set()
    done = 0
    try:
        with gzip.open(saida, "wt", encoding="utf-8") as out:
            for n, batch in enumerate(batches, 1):
                flt = f"author.id:{'|'.join(batch)},from_publication_date:{FROM}"
                for w in client.iterate("works", flt, SELECT, sort="publication_date:desc"):
                    wid = _sid(w.get("id"))
                    if wid and wid not in seen:
                        seen.add(wid)
                        out.write(json.dumps(_slim(w), ensure_ascii=False) + "\n")
                done = n
                print(f"lote {n}/{len(batches)}: {len(seen)} publicações, {client.requests} requisições, "
                      f"US$ {client.spent_usd:.4f}", flush=True)
    except BudgetExceededError as e:
        print(f"Parado pelo teto diário ({e}) no lote {done + 1}. Rode de novo amanhã: o cache retoma.")
        sys.exit(2)
    print(f"pronto: {len(seen)} publicações -> {saida}")


# ─────────────────────────────────────────────────────────────── fase 2: exportar
def exportar(entrada: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    # Pessoas que podem aparecer: as que estão no retrato do Elos (quem pediu remoção já saiu de lá).
    people = set(pq.read_table(SNAPSHOT / "elos_researcher.parquet", columns=["id"]).column("id").to_pylist())
    alias: dict[str, str] = {}
    for rid, oas in zip(*pq.read_table(SNAPSHOT / "elos_researcher.parquet", columns=["id", "openalex_ids"])
                        .to_pydict().values()):
        for oa in oas or []:
            alias[oa] = rid

    work, wa, kw, sdg, wf, waw, abstracts = [], [], [], [], [], [], []
    funders: dict[str, dict] = {}
    awards: dict[str, dict] = {}
    keywords: dict[str, str] = {}
    with gzip.open(entrada, "rt", encoding="utf-8") as f:
        for line in f:
            w = json.loads(line)
            authors = [(alias.get(a["id"]), a) for a in w["autores"]]
            in_base = [(rid, a) for rid, a in authors if rid in people]
            if not in_base:
                continue  # só publicações de alguém que está na base
            wid = w["id"]
            work.append({k: w[k] for k in ("id", "doi", "titulo", "ano", "data", "tipo", "idioma", "veiculo_id",
                                           "veiculo", "veiculo_tipo", "issn_l", "acesso_aberto", "oa_url",
                                           "citacoes", "fwci", "percentil", "top10", "top1", "referencias",
                                           "tema", "n_autores")})
            if w["resumo"]:
                abstracts.append({"work_id": wid, "resumo": w["resumo"]})
            seen_r: set[str] = set()
            for rid, a in in_base:
                if rid not in seen_r:
                    seen_r.add(rid)
                    wa.append({"work_id": wid, "researcher_id": rid, "posicao": a["posicao"],
                               "correspondente": a["correspondente"]})
            for k in w["palavras"]:
                keywords[k["id"]] = k["nome"]
                kw.append({"work_id": wid, "keyword_id": k["id"], "nota": k["nota"]})
            for s in w["ods"]:
                sdg.append({"work_id": wid, "ods_id": s["id"], "ods": s["nome"], "nota": s["nota"]})
            for fu in w["financiadores"]:
                if fu["id"]:
                    funders.setdefault(fu["id"], {"id": fu["id"], "nome": fu["nome"], "ror": fu["ror"]})
                    wf.append({"work_id": wid, "funder_id": fu["id"]})
            for aw in w["auxilios"]:
                if aw["id"]:
                    awards.setdefault(aw["id"], {"id": aw["id"], "processo": aw["processo"], "nome": aw["nome"],
                                                 "funder_id": aw["financiador"],
                                                 "financiador": aw["financiador_nome"]})
                    waw.append({"work_id": wid, "award_id": aw["id"]})

    def write(name: str, rows: list[dict]) -> None:
        if not rows:
            print(f"{name}: vazio")
            return
        pq.write_table(pa.Table.from_pylist(rows), SNAPSHOT / f"{name}.parquet", compression="zstd")
        print(f"{name:20s} {len(rows):>9} linhas {(SNAPSHOT / f'{name}.parquet').stat().st_size / 1e6:7.2f} MB")

    write("oa_work", work)
    # Resumos: só no Parquet da Release (análise e embeddings), fora do banco da API (tamanho).
    pq.write_table(pa.Table.from_pylist(abstracts), SNAPSHOT / "resumos_obras.parquet", compression="zstd")
    print(f"resumos_obras (fora do banco) {len(abstracts)} linhas")
    write("oa_work_author", wa)
    write("oa_keyword", [{"id": k, "nome": v} for k, v in keywords.items()])
    write("oa_work_keyword", kw)
    write("oa_work_sdg", sdg)
    write("oa_funder", list(funders.values()))
    write("oa_work_funder", wf)
    write("oa_award", list(awards.values()))
    write("oa_work_award", waw)

    meta_path = SNAPSHOT / "snapshot.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for name in ("oa_work", "oa_work_author", "oa_keyword", "oa_work_keyword", "oa_work_sdg", "oa_funder",
                 "oa_work_funder", "oa_award", "oa_work_award"):
        p = SNAPSHOT / f"{name}.parquet"
        if p.exists():
            meta["tables"][name] = {"rows": pq.ParquetFile(p).metadata.num_rows, "bytes": p.stat().st_size}
    meta["openalex_obras"] = {"coletado_em": datetime.fromtimestamp(entrada.stat().st_mtime, UTC)
                              .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                              "desde": FROM, "publicacoes": len(work),
                              "financiadores_mais_citados": Counter(r["funder_id"] for r in wf).most_common(5)}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("coletar")
    c.add_argument("elos", type=Path)
    c.add_argument("--saida", type=Path, default=Path("obras.jsonl.gz"))
    c.add_argument("--limite", type=int, default=None, help="só os primeiros N lotes (teste)")
    e = sub.add_parser("exportar")
    e.add_argument("entrada", type=Path)
    a = ap.parse_args()
    if a.cmd == "coletar":
        coletar(a.elos, a.saida, a.limite)
    else:
        exportar(a.entrada)


if __name__ == "__main__":
    main()
