"""Etapa 3 do plano do OpenAlex: pesquisadores e laboratórios com perfil para cada edital.

É uma inferência por semelhança de texto: o vetor do edital (o mesmo da busca híbrida do
FomentaBR) contra o perfil de cada pesquisador (temas, palavras-chave e títulos recentes das
publicações) e de cada laboratório (o vetor do laboratório na busca híbrida e o do responsável
e da equipe). Regras por cima da semelhança:
- FAP estadual: só gente e laboratório do estado da agência (a instituição define a UF);
- só quem publicou de 2022 em diante;
- bônus pequeno para quem a agência já financiou (auxílio ou publicação que cita o financiador);
- editais só para empresas ficam sem lista;
- editais abertos a todas as áreas ("Todas as áreas" ou 5+ áreas) também: a semelhança ali só acha
  quem pesquisa gestão de ciência, não quem tem perfil (conferido à mão em 20 editais).

    # 1) aqui: os textos dos perfis (responsáveis e equipe)
    python tools/perfis_editais.py perfis --saida perfis.jsonl.gz
    # 2) no FomentaBR (lê os vetores do Supabase só para leitura; gera os das pessoas na OpenAI,
    #    ~US$ 0,15, com cache para retomar):
    PYTHONPATH=src python <fomenta-api>/tools/perfis_editais.py vetores perfis.jsonl.gz --pasta vetores/
    # 3) aqui: calcula e grava edital_pesquisador e edital_laboratorio no retrato
    python tools/perfis_editais.py exportar vetores/
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "build" / "snapshot"
DIM = 512  # igual ao índice do chat do FomentaBR (EMBEDDING_DIMENSIONS)
BATCH = 500
TOP_PESSOAS, TOP_LABS = 30, 20
ATIVO_DESDE = 2022
BONUS_FINANCIADO = 0.02
HUB_K = 10  # CSLS: desconta a semelhança média de cada pessoa com os 10 editais mais próximos dela
AREAS_GENERICO = 5  # a partir de quantas áreas do conhecimento o edital é "todas as áreas"
# UF de cada FAP (slug da agência dos editais).
UF_FAP = {
    "fapesp": "SP", "facepe": "PE", "fapern": "RN", "fapema": "MA", "fapdf": "DF", "faperj": "RJ", "fapesb": "BA",
    "fapemig": "MG", "fapespa": "PA", "funcap": "CE", "fapepi": "PI", "faperr": "RR", "fapero": "RO", "fapesc": "SC",
    "fapesq": "PB", "fapes": "ES", "araucaria": "PR", "fundect": "MS", "fapeg": "GO", "fapac": "AC", "fapeal": "AL",
    "fapeam": "AM", "fapemat": "MT", "fapt": "TO", "fapergs": "RS", "fapitec": "SE", "fapeap": "AP",
}


# ─────────────────────────────────────────────────────────────── 1: perfis (ambiente da API)
def perfis(saida: Path) -> None:
    import duckdb

    con = duckdb.connect()
    s = SNAPSHOT.as_posix()
    con.execute(f"""
        CREATE TEMP TABLE pessoa AS
        SELECT DISTINCT r.id, r.nome FROM '{s}/elos_researcher.parquet' r
        JOIN '{s}/elos_researcher_lab.parquet' rl ON rl.researcher_id = r.id AND rl.papel <> 'coautor'
        WHERE r.ultimo_ano >= {ATIVO_DESDE}""")
    temas = dict(con.execute(f"""
        SELECT rt.researcher_id, string_agg(t.nome, '; ' ORDER BY rt.n DESC) FROM (
          SELECT *, row_number() OVER (PARTITION BY researcher_id ORDER BY n DESC) k
          FROM '{s}/elos_researcher_topic.parquet' WHERE researcher_id IN (SELECT id FROM pessoa)) rt
        JOIN '{s}/elos_topic.parquet' t ON t.id = rt.topic_id WHERE rt.k <= 6 GROUP BY 1""").fetchall())
    palavras = dict(con.execute(f"""
        WITH k AS (SELECT a.researcher_id rid, kw.nome, sum(wk.nota) peso
                   FROM '{s}/oa_work_author.parquet' a
                   JOIN '{s}/oa_work_keyword.parquet' wk ON wk.work_id = a.work_id
                   JOIN '{s}/oa_keyword.parquet' kw ON kw.id = wk.keyword_id
                   WHERE a.researcher_id IN (SELECT id FROM pessoa) GROUP BY 1, 2),
             r AS (SELECT *, row_number() OVER (PARTITION BY rid ORDER BY peso DESC, nome) k FROM k)
        SELECT rid, string_agg(nome, ', ' ORDER BY peso DESC, nome) FROM r WHERE k <= 15 GROUP BY 1""").fetchall())
    titulos = dict(con.execute(f"""
        WITH t AS (SELECT a.researcher_id rid, w.titulo,
                          row_number() OVER (PARTITION BY a.researcher_id ORDER BY w.ano DESC, w.citacoes DESC, w.id) k
                   FROM '{s}/oa_work_author.parquet' a JOIN '{s}/oa_work.parquet' w ON w.id = a.work_id
                   WHERE a.researcher_id IN (SELECT id FROM pessoa) AND w.titulo IS NOT NULL)
        SELECT rid, string_agg(left(titulo, 160), ' | ' ORDER BY k) FROM t WHERE k <= 4 GROUP BY 1""").fetchall())
    n = 0
    with gzip.open(saida, "wt", encoding="utf-8") as out:
        for rid, nome in con.execute("SELECT id, nome FROM pessoa ORDER BY id").fetchall():
            partes = [f"Temas: {temas[rid]}." if rid in temas else "",
                      f"Palavras-chave: {palavras[rid]}." if rid in palavras else "",
                      f"Publicações: {titulos[rid]}" if rid in titulos else ""]
            texto = " ".join(p for p in partes if p)
            if texto:
                out.write(json.dumps({"id": rid, "texto": texto[:2000]}, ensure_ascii=False) + "\n")
                n += 1
    # Tema de cada edital: título, áreas em texto livre e resumo. Sem público, itens financiáveis e
    # contrapartida, que o índice do chat inclui para a busca mas puxam quem pesquisa gestão de C&T.
    m = 0
    with gzip.open(saida.with_name("editais_" + saida.name), "wt", encoding="utf-8") as out:
        for cid, titulo, areas, resumo in con.execute(f"""
                SELECT id, title, json_extract_string(extraction_metadata, '$.knowledge_areas_freetext'),
                       description_summary FROM '{s}/edital.parquet' ORDER BY id""").fetchall():
            temas = "; ".join(json.loads(areas)) if areas else ""
            texto = " ".join(p for p in (f"Edital: {titulo}.", f"Temas: {temas}." if temas else "",
                                         f"Resumo: {resumo}" if resumo else "") if p)
            out.write(json.dumps({"id": str(cid), "texto": texto[:2000]}, ensure_ascii=False) + "\n")
            m += 1
    print(f"{n} perfis e {m} editais -> {saida}")


# ─────────────────────────────────────────────────────────────── 2: vetores (ambiente do FomentaBR)
def _write_vectors(path: Path, ids: list[str], vecs: list[list[float]]) -> None:
    arr = array("f")
    for v in vecs:
        arr.extend(v)
    path.with_suffix(".f32").write_bytes(arr.tobytes())
    path.with_suffix(".ids.json").write_text(json.dumps(ids), encoding="utf-8")


def vetores(entrada: Path, pasta: Path) -> None:
    from openai import OpenAI
    from sqlalchemy import text

    from fomenta.config import settings
    from fomenta.db import sync_session_factory

    pasta.mkdir(parents=True, exist_ok=True)
    # Editais e laboratórios: os vetores que a busca híbrida já guarda (só leitura no banco de produção).
    with sync_session_factory() as s:
        s.execute(text("SET TRANSACTION READ ONLY"))
        for kind in ("calls", "labs"):
            rows = s.execute(text("SELECT ref_id, embedding::text FROM search_embedding WHERE kind = :k ORDER BY ref_id"),
                             {"k": kind}).fetchall()
            _write_vectors(pasta / kind, [r[0] for r in rows], [json.loads(r[1]) for r in rows])
            print(f"{kind}: {len(rows)} vetores")
        s.rollback()

    if not settings.openai_api_key:
        sys.exit("Defina OPENAI_API_KEY no .env do FomentaBR.")
    client = OpenAI(api_key=settings.openai_api_key)
    with gzip.open(entrada, "rt", encoding="utf-8") as f:
        perfis_ = [json.loads(line) for line in f]
    tokens = 0
    temas = entrada.with_name("editais_" + entrada.name)
    if temas.exists():
        with gzip.open(temas, "rt", encoding="utf-8") as f:
            eds = [json.loads(line) for line in f]
        vecs = []
        for i in range(0, len(eds), BATCH):
            resp = client.embeddings.create(model=settings.openai_embedding_model, dimensions=DIM,
                                            input=[e["texto"] for e in eds[i:i + BATCH]])
            tokens += resp.usage.total_tokens
            vecs += [d.embedding for d in resp.data]
        _write_vectors(pasta / "calls_tema", [e["id"] for e in eds], vecs)
        print(f"calls_tema: {len(eds)} vetores")
    lotes = pasta / "pessoas_lotes"
    lotes.mkdir(exist_ok=True)
    for i in range(0, len(perfis_), BATCH):
        cache = lotes / f"{i:06d}.json.gz"
        if cache.exists():
            continue
        lote = perfis_[i:i + BATCH]
        resp = client.embeddings.create(model=settings.openai_embedding_model, dimensions=DIM,
                                        input=[p["texto"] for p in lote])
        tokens += resp.usage.total_tokens
        cache.write_bytes(gzip.compress(json.dumps(
            {"ids": [p["id"] for p in lote], "v": [d.embedding for d in resp.data]}).encode("utf-8")))
        print(f"{i + len(lote)}/{len(perfis_)} perfis, {tokens} tokens", flush=True)
    ids, vecs = [], []
    for c in sorted(lotes.glob("*.json.gz")):
        d = json.loads(gzip.decompress(c.read_bytes()))
        ids += d["ids"]
        vecs += d["v"]
    _write_vectors(pasta / "pessoas", ids, vecs)
    print(f"pessoas: {len(ids)} vetores; {tokens} tokens nesta execução "
          f"(~US$ {tokens / 1e6 * 0.02:.3f} no text-embedding-3-small)")


# ─────────────────────────────────────────────────────────────── 3: exportar (ambiente da API)
def _load(pasta: Path, name: str):
    import numpy as np

    ids = json.loads((pasta / f"{name}.ids.json").read_text(encoding="utf-8"))
    v = np.frombuffer((pasta / f"{name}.f32").read_bytes(), dtype=np.float32).reshape(len(ids), DIM)
    v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    return ids, v


def exportar(pasta: Path) -> None:
    import duckdb
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    s = SNAPSHOT.as_posix()
    con = duckdb.connect()
    # Vetor de tema do edital quando existe (ver `perfis`); senão, o da busca híbrida.
    call_ids, E = _load(pasta, "calls_tema" if (pasta / "calls_tema.ids.json").exists() else "calls")
    lab_ids, L = _load(pasta, "labs")
    p_ids, P = _load(pasta, "pessoas")
    call_ids = [int(x) for x in call_ids]
    lab_ids = [int(x) for x in lab_ids]

    editais = {r[0]: r[1:] for r in con.execute(
        f"SELECT e.id, a.slug, a.type, e.target_audience FROM '{s}/edital.parquet' e "
        f"JOIN '{s}/agencia.parquet' a ON a.id = e.agency_id").fetchall()}
    uf_pessoa = dict(con.execute(
        f"SELECT r.id, i.state FROM '{s}/elos_researcher.parquet' r "
        f"JOIN '{s}/pnipe_institution.parquet' i ON i.cnpj = r.inst_cnpj").fetchall())
    uf_lab = dict(con.execute(
        f"SELECT l.id, coalesce(l.resolved_state, i.state) FROM '{s}/pnipe_laboratory.parquet' l "
        f"LEFT JOIN '{s}/pnipe_institution.parquet' i ON i.cnpj = l.institution_cnpj").fetchall())
    funder_of = dict(con.execute(f"SELECT agencia, funder_id FROM '{s}/oa_funder_agencia.parquet'").fetchall())
    aux = {}  # (funder, pessoa) -> nº de auxílios em que é responsável
    for fid, rid, n in con.execute(f"SELECT funder_id, researcher_id, count(*) FROM '{s}/oa_auxilio.parquet' "
                                   "WHERE researcher_id IS NOT NULL GROUP BY ALL").fetchall():
        aux[(fid, rid)] = n
    pub = {}  # (funder, pessoa) -> nº de publicações que citam o financiador
    for fid, rid, n in con.execute(f"SELECT wf.funder_id, a.researcher_id, count(*) FROM '{s}/oa_work_funder.parquet' wf "
                                   f"JOIN '{s}/oa_work_author.parquet' a ON a.work_id = wf.work_id "
                                   f"WHERE wf.funder_id IN (SELECT funder_id FROM '{s}/oa_funder_agencia.parquet') "
                                   "GROUP BY ALL").fetchall():
        pub[(fid, rid)] = n
    genericos = {r[0] for r in con.execute(
        f"SELECT ea.call_id FROM '{s}/edital_area.parquet' ea JOIN '{s}/area_conhecimento.parquet' k ON k.id = ea.area_id "
        f"GROUP BY 1 HAVING bool_or(k.name = 'Todas as áreas') OR count(*) >= {AREAS_GENERICO}").fetchall()}
    equipe: dict[int, list[int]] = {}
    p_index = {rid: i for i, rid in enumerate(p_ids)}
    for lab, rid in con.execute(f"SELECT lab_id, researcher_id FROM '{s}/elos_researcher_lab.parquet' "
                                "WHERE papel <> 'coautor' AND confianca >= 0.8").fetchall():
        if rid in p_index:
            equipe.setdefault(lab, []).append(p_index[rid])

    p_uf = np.array([uf_pessoa.get(r) or "" for r in p_ids])
    l_uf = np.array([uf_lab.get(lab) or "" for lab in lab_ids])
    sim_p = E @ P.T  # editais x pessoas
    # Quem pesquisa gestão de C&T, inovação ou cienciometria fica perto de quase todo edital
    # ("hub"). CSLS (Conneau et al., 2018): nota = semelhança - metade da média dos K editais
    # mais próximos da pessoa; o especialista sobe, o hub desce.
    hub = np.sort(sim_p, axis=0)[-HUB_K:].mean(axis=0) / 2
    adj_p = sim_p - hub
    sim_l = E @ L.T  # editais x laboratórios
    adj_l = sim_l - np.sort(sim_l, axis=0)[-HUB_K:].mean(axis=0) / 2  # mesma correção de hubs
    out_p, out_l, situacao = [], [], []
    for ci, cid in enumerate(call_ids):
        if cid not in editais:
            continue
        slug, tipo, publico = editais[cid]
        publico = list(publico or [])
        if publico and set(publico) <= {"company"}:
            situacao.append({"call_id": cid, "situacao": "so_empresas"})
            continue
        if cid in genericos:
            situacao.append({"call_id": cid, "situacao": "todas_as_areas"})
            continue
        situacao.append({"call_id": cid, "situacao": "tematico"})
        uf = UF_FAP.get(slug) if tipo == "state_fap" else None
        fid = funder_of.get(slug)
        sp = adj_p[ci].copy()
        if uf:
            sp[p_uf != uf] = -10  # fora do estado da FAP
        # Bônus para quem a agência já financiou (só reordena entre os parecidos).
        if fid:
            cand = np.argsort(-sp)[: TOP_PESSOAS * 10]
            for j in cand:
                if aux.get((fid, p_ids[j])) or pub.get((fid, p_ids[j])):
                    sp[j] += BONUS_FINANCIADO
        top = [j for j in np.argsort(-sp)[:TOP_PESSOAS] if sp[j] > -5]
        for k, j in enumerate(top, 1):
            rid = p_ids[j]
            out_p.append({"call_id": cid, "researcher_id": rid, "posicao": k, "nota": round(float(sp[j]), 4),
                          "semelhanca": round(float(sim_p[ci, j]), 4),
                          "auxilios_da_agencia": aux.get((fid, rid), 0) if fid else 0,
                          "publicacoes_da_agencia": pub.get((fid, rid), 0) if fid else 0})
        # Laboratório: metade o vetor do laboratório, metade a melhor pessoa do responsável/equipe.
        sl = adj_l[ci].copy()
        best = np.full(len(lab_ids), np.nan, dtype=np.float32)
        for li, lab in enumerate(lab_ids):
            js = equipe.get(lab)
            if js:
                best[li] = adj_p[ci, js].max()
        score = np.where(np.isnan(best), sl, 0.5 * sl + 0.5 * best)
        if uf:
            score[l_uf != uf] = -10
        for k, li in enumerate([i for i in np.argsort(-score)[:TOP_LABS] if score[i] > -5], 1):
            out_l.append({"call_id": cid, "lab_id": lab_ids[li], "posicao": k, "nota": round(float(score[li]), 4),
                          "semelhanca_laboratorio": round(float(sim_l[ci, li]), 4),
                          "semelhanca_equipe": None if np.isnan(best[li]) else
                          round(float(sim_p[ci, equipe[lab_ids[li]]].max()), 4)})

    pq.write_table(pa.Table.from_pylist(out_p, schema=pa.schema([
        ("call_id", pa.int64()), ("researcher_id", pa.string()), ("posicao", pa.int32()), ("nota", pa.float32()),
        ("semelhanca", pa.float32()), ("auxilios_da_agencia", pa.int32()), ("publicacoes_da_agencia", pa.int32())])),
        SNAPSHOT / "edital_pesquisador.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(out_l, schema=pa.schema([
        ("call_id", pa.int64()), ("lab_id", pa.int64()), ("posicao", pa.int32()), ("nota", pa.float32()),
        ("semelhanca_laboratorio", pa.float32()), ("semelhanca_equipe", pa.float32())])),
        SNAPSHOT / "edital_laboratorio.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(situacao, schema=pa.schema([("call_id", pa.int64()), ("situacao", pa.string())])),
                   SNAPSHOT / "edital_perfil.parquet", compression="zstd")
    meta_path = SNAPSHOT / "snapshot.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["tables"]["edital_pesquisador"] = {"rows": len(out_p)}
    meta["tables"]["edital_laboratorio"] = {"rows": len(out_l)}
    meta["tables"]["edital_perfil"] = {"rows": len(situacao)}
    meta["perfis_editais"] = {"calculado_em": datetime.now(UTC).isoformat(timespec="seconds"),
                              "editais": {k: sum(1 for x in situacao if x["situacao"] == k) for k in ("tematico", "todas_as_areas", "so_empresas")},
                              "pessoas_com_vetor": len(p_ids), "modelo": "text-embedding-3-small/512",
                              "regras": {"ativo_desde": ATIVO_DESDE, "bonus_financiado": BONUS_FINANCIADO,
                                         "fap_so_do_estado": True, "areas_generico": AREAS_GENERICO}}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta["perfis_editais"], ensure_ascii=False, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("perfis")
    a.add_argument("--saida", type=Path, required=True)
    b = sub.add_parser("vetores")
    b.add_argument("entrada", type=Path)
    b.add_argument("--pasta", type=Path, required=True)
    c = sub.add_parser("exportar")
    c.add_argument("pasta", type=Path)
    x = p.parse_args()
    if x.cmd == "perfis":
        perfis(x.saida)
    elif x.cmd == "vetores":
        vetores(x.entrada, x.pasta)
    else:
        exportar(x.pasta)


if __name__ == "__main__":
    main()
