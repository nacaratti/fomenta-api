"""Elos ampliado: refaz a carga do Elos a partir do cache do OpenAlex, sem os cortes
que o Supabase gratuito obrigou, e liga ao laboratório todo pesquisador que publicou
com o responsável dele.

Por que aqui: no FomentaBR o Elos cabe em ~95 MB do banco, então a carga guarda só 5
temas por responsável, 10 coautorias por pessoa, 10 laboratórios parceiros com até 4
pessoas e a equipe provável (mesma instituição, 3+ trabalhos). A API lê um arquivo
DuckDB e não tem esse aperto.

Duas fases, porque o cálculo usa o código e o ambiente do FomentaBR e a exportação usa
pyarrow (ambiente da API):

    # 1) no ambiente do FomentaBR (lê o PNIPE e os pedidos de remoção, só leitura;
    #    o OpenAlex só pelo cache em disco: nenhuma requisição, nenhum gasto)
    cd <FomentaBR>
    PYTHONPATH=src python <fomenta-api>/tools/elos_ampliado.py calcular --saida elos.pkl.gz

    # 2) no ambiente da API: troca as tabelas elos_* do retrato
    python tools/elos_ampliado.py exportar elos.pkl.gz

O que muda em relação ao Elos do site:
- `coautor`: novo papel em `elos_researcher_lab`. Quem publicou com o responsável desde
  2019 (publicações com até 30 autores) fica ligado aos laboratórios dele, com o número de
  publicações em comum e as mais recentes como evidência. Só para responsáveis com casamento
  de confiança ≥ 0,8, e o vínculo herda essa confiança;
- coautores de fora da base entram em `elos_researcher` (`origem = 'coautor'`), com nome,
  ORCID e instituição tirados da publicação mais recente em comum, e temas e grandes áreas
  das publicações em comum;
- 10 temas por responsável e 5 por pessoa de equipe (eram 5 e 3);
- coautorias sem teto por pessoa, com as publicações em comum de cada par;
- afinidade com até 20 pares por responsável (eram 10);
- todos os pares de laboratórios que publicam juntos, com até 10 pessoas por par.
"""
from __future__ import annotations

import argparse
import gzip
import json
import pickle
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "build" / "snapshot"
DISPLAY_CUT = 0.8
COAUTHOR_TOPICS = 5
EVIDENCE_WORKS = 5  # publicações em comum guardadas por par (as mais recentes)


# ─────────────────────────────────────────────────────────────── fase 1: calcular
class OfflineMiss(RuntimeError):
    pass


def _offline_client(build_mod: Any, openalex_mod: Any, cache_dir: str) -> tuple[Any, list, dict]:
    """Cliente que só lê o cache. `api_key` não vazia para reproduzir as mesmas chaves de
    cache da carga original (cursor e busca por nome); a rede nunca é usada."""
    client = openalex_mod.OpenAlexClient(api_key="offline", cache_dir=cache_dir, daily_budget_usd=0)

    def _no_network(*_a: Any, **_k: Any) -> Any:
        raise OfflineMiss

    client._request = _no_network
    misses: list[tuple[str, dict]] = []
    orig_get = client.get

    def get(path: str, **params: Any) -> dict[str, Any]:
        try:
            return orig_get(path, **params)
        except (OfflineMiss, openalex_mod.BudgetExceededError):
            misses.append((path, {k: v for k, v in params.items() if k != "api_key"}))
            return {"results": [], "meta": {"count": 0, "next_cursor": None}}

    client.get = get

    # guarda os autores de cada publicação lida (nome, ORCID, instituições, tema)
    works: dict[str, dict[str, Any]] = {}
    orig_iterate = client.iterate

    def iterate(path: str, filter: str, select: str, *a: Any, **k: Any):
        for w in orig_iterate(path, filter, select, *a, **k):
            if path == "works" and w.get("id"):
                wid = openalex_mod.short_id(w["id"])
                if wid not in works:
                    auth = []
                    for x in w.get("authorships") or []:
                        aid = openalex_mod.short_id((x.get("author") or {}).get("id"))
                        if not aid:
                            continue
                        auth.append({
                            "id": aid,
                            "nome": (x.get("author") or {}).get("display_name"),
                            "orcid": build_mod._orcid((x.get("author") or {}).get("orcid")),
                            "inst": [(openalex_mod.short_id(i.get("id")), i.get("display_name"), i.get("country_code"))
                                     for i in x.get("institutions") or [] if i.get("id")],
                        })
                    works[wid] = {"ano": w.get("publication_year"), "autores": auth, "tema": w.get("primary_topic")}
            yield w

    client.iterate = iterate
    return client, misses, works


def calcular(saida: Path) -> None:
    from sqlalchemy import text

    from fomenta.config import settings
    from fomenta.db import sync_session
    from fomenta.elos import build, openalex
    from fomenta.elos.taxonomy import researcher_grandes_areas

    # cortes do Supabase desfeitos (os que são lidos do módulo na hora da chamada)
    build.TOPICS_PER_TEAM_MEMBER = 5
    build.EDGES_PER_PERSON = 20  # afinidade: até 20 por responsável

    client, misses, works = _offline_client(build, openalex, settings.openalex_cache_dir)
    with sync_session() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        result = build.run(session, client, None, log=print, stages=5)  # ligações calculadas aqui
        optout = session.execute(text(
            "SELECT openalex_id, orcid FROM elos_optout WHERE status IN ('aprovado', 'pendente')")).all()
        session.rollback()
    print(f"cache: {len(misses)} respostas faltando; {len(works)} publicações lidas")
    if misses:
        print("primeiras faltas:", misses[:5])

    out_ids = {r[0] for r in optout if r[0]}
    out_orcids = {r[1] for r in optout if r[1]}

    def fora(rid: str, oa_ids: list[str], orcid: str | None) -> bool:
        return bool({rid, *oa_ids} & out_ids) or bool(orcid and orcid in out_orcids)

    alias = result.alias
    researchers = {rid: {**r, "origem": None} for rid, r in result.researchers.items()
                   if not fora(rid, r["openalex_ids"], r["orcid"])}
    removed = len(result.researchers) - len(researchers)
    alias = {k: v for k, v in alias.items() if v in researchers}
    links = [x for x in result.lab_links if x["researcher_id"] in researchers]
    papel_de: dict[str, set[str]] = defaultdict(set)
    for x in links:
        papel_de[x["researcher_id"]].add(x["papel"])
    for rid, r in researchers.items():
        r["origem"] = "responsavel" if "responsavel" in papel_de[rid] else ("equipe" if papel_de[rid] else "outro")

    # labs de cada responsável (só casamento ≥ 0,8, o mesmo corte do site)
    resp_labs: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for x in links:
        if x["papel"] == "responsavel" and x["confianca"] >= DISPLAY_CUT:
            resp_labs[x["researcher_id"]].append((x["lab_id"], x["confianca"]))
    responsaveis = {x["researcher_id"] for x in links if x["papel"] == "responsavel"}

    # instituição do OpenAlex -> CNPJ do PNIPE
    oa_to_cnpj = {row["openalex_id"]: cnpj for cnpj, rows in result.institutions.items() for row in rows}

    # pares de coautoria: todo par com pelo menos um responsável + pares entre pessoas da base
    pair_n: Counter[tuple[str, str]] = Counter()
    pair_works: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    ext: dict[str, dict[str, Any]] = {}  # coautores de fora da base
    for wid, w in works.items():
        if len(w["autores"]) > build.MAX_AUTHORS_PER_WORK:
            continue
        year = w["ano"] or 0
        people: dict[str, dict] = {}
        for a in w["autores"]:
            pid = alias.get(a["id"], a["id"])
            people.setdefault(pid, a)
        ids = sorted(people)
        resp_here = [p for p in ids if p in responsaveis]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                in_base = a in researchers and b in researchers
                if not (in_base or a in resp_here or b in resp_here):
                    continue
                key = (a, b)
                pair_n[key] += 1
                pair_works[key].append((year, wid))
        if resp_here:
            for pid, a in people.items():
                if pid in researchers:
                    continue
                e = ext.setdefault(pid, {"nome": Counter(), "orcid": None, "ultimo": (-1, None), "temas": Counter()})
                if a["nome"]:
                    e["nome"][a["nome"]] += 1
                e["orcid"] = e["orcid"] or a["orcid"]
                if a["inst"] and year >= e["ultimo"][0]:
                    e["ultimo"] = (year, a["inst"][0])
                if w["tema"]:
                    e["temas"][build._register_topic(result, w["tema"])] += 1

    # coautores de fora da base viram pesquisadores (origem = coautor)
    topics_rows: list[dict] = []
    for pid, e in ext.items():
        orcid = e["orcid"]
        if fora(pid, [pid], orcid) or not e["nome"]:
            continue
        year, inst = e["ultimo"]
        top = e["temas"].most_common(COAUTHOR_TOPICS)
        researchers[pid] = {
            "id": pid, "openalex_ids": [pid], "orcid": orcid, "nome": e["nome"].most_common(1)[0][0],
            "inst_cnpj": oa_to_cnpj.get(inst[0]) if inst else None,
            "publicacoes": None, "citacoes": None, "h_index": None, "desde_2019": None, "ultimo_ano": None,
            "grandes_areas": researcher_grandes_areas(
                {"field": {"id": f"fields/{result.topics[t]['field_id']}"}, "count": n} for t, n in top) if top else None,
            "origem": "coautor",
        }
        researchers[pid]["_inst"] = inst
        result.researcher_topics[pid] = top
    # instituição atual (OpenAlex) também para a base, pela publicação mais recente
    for r in researchers.values():
        inst = r.pop("_inst", None)
        r["instituicao_openalex"] = inst[0] if inst else None
        r["instituicao_nome"] = inst[1] if inst else None
        r["instituicao_pais"] = inst[2] if inst else None

    for rid, tops in result.researcher_topics.items():
        if rid in researchers:
            topics_rows += [{"researcher_id": rid, "topic_id": t, "n": n} for t, n in tops]

    edges = []
    for (a, b), n in pair_n.items():
        if a not in researchers or b not in researchers:
            continue
        ws = sorted(pair_works[(a, b)], reverse=True)
        last = ws[0][0] or None
        edges.append({"a": a, "b": b, "tipo": "coautoria", "peso": float(n), "ultimo_ano": last,
                      "obras": [w for _, w in ws[:EVIDENCE_WORKS]]})
    # ultimo_ano dos coautores de fora: a publicação mais recente com um responsável
    last_year: dict[str, int] = {}
    for e in edges:
        for p in (e["a"], e["b"]):
            if researchers[p]["origem"] == "coautor" and e["ultimo_ano"]:
                last_year[p] = max(last_year.get(p, 0), e["ultimo_ano"])
    for p, y in last_year.items():
        researchers[p]["ultimo_ano"] = y

    already = {(e["a"], e["b"]) for e in edges}
    result.researchers = {k: v for k, v in researchers.items() if v["origem"] != "coautor"}
    result.lab_links = links
    affinity = [{**e, "obras": None} for e in build._affinity_edges(result, already=already)]
    edges += affinity

    # vínculo coautor -> laboratórios do responsável
    lab_rows = [{"researcher_id": x["researcher_id"], "lab_id": x["lab_id"], "papel": x["papel"],
                 "confianca": x["confianca"], "responsavel_id": (x.get("evidencia") or {}).get("responsavel"),
                 "publicacoes_em_comum": (x.get("evidencia") or {}).get("coautorias"), "ultimo_ano": None,
                 "obras": None}
                for x in links]
    have = {(x["researcher_id"], x["lab_id"]) for x in lab_rows}
    best: dict[tuple[str, int], dict] = {}
    for e in edges:
        if e["tipo"] != "coautoria":
            continue
        for resp, other in ((e["a"], e["b"]), (e["b"], e["a"])):
            for lab_id, conf in resp_labs.get(resp, ()):
                key = (other, lab_id)
                if other == resp or key in have:
                    continue
                cur = best.get(key)
                if cur is None or e["peso"] > cur["publicacoes_em_comum"]:
                    best[key] = {"researcher_id": other, "lab_id": lab_id, "papel": "coautor", "confianca": conf,
                                 "responsavel_id": resp, "publicacoes_em_comum": int(e["peso"]),
                                 "ultimo_ano": e["ultimo_ano"], "obras": e["obras"]}
    lab_rows += best.values()

    # pares de laboratórios (responsável e equipe, como no site), sem corte
    labs_of: dict[str, set[int]] = defaultdict(set)
    for x in links:
        if x["confianca"] >= DISPLAY_CUT:
            labs_of[x["researcher_id"]].add(x["lab_id"])
    work_list = [(w["ano"], tuple(a["id"] for a in w["autores"])) for w in works.values()
                 if len(w["autores"]) <= build.MAX_AUTHORS_PER_WORK]
    lab_pairs = build._lab_pairs(work_list, alias, labs_of, max_people=10, min_works=1, keep=10**9)

    used_topics = {t["topic_id"] for t in topics_rows}
    payload = {
        "exported_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "researchers": list(researchers.values()),
        "researcher_lab": lab_rows,
        "researcher_topic": topics_rows,
        "topics": [t for tid, t in result.topics.items() if tid in used_topics],
        "edges": edges,
        "lab_links": lab_pairs,
        "institutions": [{"cnpj": cnpj, "openalex_id": row["openalex_id"], "nome_openalex": row.get("nome_openalex"),
                          "ror": row.get("ror"), "confianca": row.get("confianca"), "origem": row.get("origem")}
                         for cnpj, rows in result.institutions.items() for row in rows],
        "stats": {"publicacoes_lidas": len(works), "faltas_no_cache": len(misses),
                  "removidos_a_pedido": removed},
    }
    with gzip.open(saida, "wb") as f:
        pickle.dump(payload, f)
    papel = Counter(x["papel"] for x in lab_rows)
    print(f"{len(researchers)} pesquisadores ({sum(1 for r in researchers.values() if r['origem'] == 'coautor')} coautores "
          f"de fora), vínculos {dict(papel)}, {len(edges)} ligações, {len(lab_pairs)} pares de labs -> {saida}")


# ─────────────────────────────────────────────────────────────── fase 2: exportar
def exportar(entrada: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    with gzip.open(entrada, "rb") as f:
        data = pickle.load(f)  # arquivo gerado pela fase 1 nesta máquina

    def write(name: str, rows: list[dict], cols: list[str]) -> None:
        table = pa.table({c: [r.get(c) for r in rows] for c in cols})
        pq.write_table(table, SNAPSHOT / f"{name}.parquet", compression="zstd")
        print(f"{name:24s} {len(rows):>9} linhas {(SNAPSHOT / f'{name}.parquet').stat().st_size / 1e6:7.2f} MB")

    # nome e sigla da instituição pelo CNPJ do PNIPE (vale para a base e para coautores de lá)
    inst = pq.read_table(SNAPSHOT / "pnipe_institution.parquet", columns=["cnpj", "social_reason", "initials"]).to_pylist()
    by_cnpj = {i["cnpj"]: i for i in inst}
    for r in data["researchers"]:
        i = by_cnpj.get(r.get("inst_cnpj") or "")
        r["instituicao_sigla"] = i["initials"] if i else None
        if i:
            r["instituicao_nome"] = i["social_reason"]
            r["instituicao_pais"] = "BR"
    write("elos_researcher", data["researchers"],
          ["id", "openalex_ids", "orcid", "nome", "inst_cnpj", "publicacoes", "citacoes", "h_index", "desde_2019",
           "ultimo_ano", "grandes_areas", "origem", "instituicao_openalex", "instituicao_nome", "instituicao_sigla",
           "instituicao_pais"])
    write("elos_researcher_lab", [x for x in data["researcher_lab"] if x["confianca"] >= DISPLAY_CUT],
          ["researcher_id", "lab_id", "papel", "confianca", "responsavel_id", "publicacoes_em_comum", "ultimo_ano",
           "obras"])
    write("elos_researcher_topic", data["researcher_topic"], ["researcher_id", "topic_id", "n"])
    write("elos_topic", data["topics"], ["id", "nome", "subarea", "area", "dominio", "field_id", "grande_area"])
    write("elos_edge", data["edges"], ["a", "b", "tipo", "peso", "ultimo_ano", "obras"])
    write("elos_lab_link", data["lab_links"], ["lab_a", "lab_b", "coautorias", "pessoas"])
    write("elos_institution", data["institutions"], ["cnpj", "openalex_id", "nome_openalex", "ror", "confianca", "origem"])

    meta_path = SNAPSHOT / "snapshot.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["elos"] = {"origem": "cache do OpenAlex, refeito por tools/elos_ampliado.py",
                    "calculado_em": data["exported_at"], **data["stats"]}
    for name in ("elos_researcher", "elos_researcher_lab", "elos_researcher_topic", "elos_topic", "elos_edge",
                 "elos_lab_link", "elos_institution"):
        p = SNAPSHOT / f"{name}.parquet"
        meta["tables"][name] = {"rows": pq.ParquetFile(p).metadata.num_rows, "bytes": p.stat().st_size}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calcular")
    c.add_argument("--saida", type=Path, default=Path("elos_ampliado.pkl.gz"))
    e = sub.add_parser("exportar")
    e.add_argument("entrada", type=Path)
    args = ap.parse_args()
    if args.cmd == "calcular":
        calcular(args.saida)
    else:
        exportar(args.entrada)


if __name__ == "__main__":
    sys.exit(main())
