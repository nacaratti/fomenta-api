"""Etapa 5 do plano do OpenAlex: instituições por ROR, com a hierarquia (lineage) do OpenAlex.

Coleta as ~2.200 instituições brasileiras do OpenAlex (ROR, siglas, nomes alternativos, tipo,
cidade, UF e a cadeia de instituições-mãe) e amplia o casamento OpenAlex <-> CNPJ do PNIPE
(`elos_institution`) de dois jeitos:
- por nome: instituição do PNIPE ainda sem par cujo nome (ou sigla, na mesma UF) é exatamente o
  de uma instituição do OpenAlex, sem empate (`origem = 'nome_openalex'`);
- por lineage: unidade do OpenAlex sem par (instituto, hospital, campus) cuja instituição-mãe já
  tem CNPJ herda o CNPJ da mãe (`origem = 'lineage'`, confiança 0,7). Só a relação "parent":
  "related" é fraca demais (liga a USP ao Butantan).

    PYTHONPATH=src python <fomenta-api>/tools/instituicoes_ror.py coletar --saida instituicoes.jsonl.gz  # FomentaBR
    python tools/instituicoes_ror.py exportar instituicoes.jsonl.gz   # aqui, logo depois do retrato do Supabase

Rode `coautores_perfis.py exportar` e `openalex_auxilios.py exportar` de novo depois: eles usam o casamento.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "build" / "snapshot"
SELECT = ("id,ror,display_name,display_name_acronyms,display_name_alternatives,type,lineage,"
          "associated_institutions,geo,works_count")
NAO_HERDA = {"government", "funder"}
MAE_MIN = 0.95  # só herda de mãe com casamento firme (um "auto" de 0,9 ligou a FATEC paulista a outra FATEC)
CONF = {"nome_openalex": 0.9, "sigla_uf": 0.85, "lineage": 0.7}
UF = {"Acre": "AC", "Alagoas": "AL", "Amapá": "AP", "Amazonas": "AM", "Bahia": "BA", "Ceará": "CE",
      "Federal District": "DF", "Distrito Federal": "DF", "Espírito Santo": "ES", "Goiás": "GO", "Maranhão": "MA",
      "Mato Grosso": "MT", "Mato Grosso do Sul": "MS", "Minas Gerais": "MG", "Pará": "PA", "Paraíba": "PB",
      "Paraná": "PR", "Pernambuco": "PE", "Piauí": "PI", "Rio de Janeiro": "RJ", "Rio Grande do Norte": "RN",
      "Rio Grande do Sul": "RS", "Rondônia": "RO", "Roraima": "RR", "Santa Catarina": "SC", "São Paulo": "SP",
      "Sergipe": "SE", "Tocantins": "TO"}


def _sid(url: str | None) -> str | None:
    return url.rstrip("/").rsplit("/", 1)[-1] if url else None


def _norm(s: str | None) -> str:
    n = unicodedata.normalize("NFKD", s or "").encode("ASCII", "ignore").decode("ASCII").lower()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in n).split())


def coletar(saida: Path) -> None:
    from fomenta.config import settings
    from fomenta.elos.openalex import BudgetExceededError, OpenAlexClient

    client = OpenAlexClient(api_key=settings.openalex_api_key, cache_dir=settings.openalex_cache_dir,
                            daily_budget_usd=settings.openalex_daily_budget_usd)
    n = 0
    try:
        with gzip.open(saida, "wt", encoding="utf-8") as out:
            for i in client.iterate("institutions", "country_code:BR", SELECT):
                geo = i.get("geo") or {}
                out.write(json.dumps({
                    "id": _sid(i["id"]), "ror": i.get("ror"), "nome": i.get("display_name"),
                    "siglas": i.get("display_name_acronyms") or [], "alternativos": i.get("display_name_alternatives") or [],
                    "tipo": i.get("type"), "cidade": geo.get("city"), "uf": UF.get(geo.get("region") or ""),
                    "publicacoes": i.get("works_count"),
                    "maes": [_sid(x) for x in (i.get("lineage") or [])[1:]],
                    "parent": [_sid(a["id"]) for a in i.get("associated_institutions") or []
                               if a.get("relationship") == "parent"],
                }, ensure_ascii=False) + "\n")
                n += 1
    except BudgetExceededError as e:
        print(f"Parado pelo teto diário ({e}).")
        sys.exit(2)
    print(f"{n} instituições, {client.requests} requisições, US$ {client.spent_usd:.4f} -> {saida}")


def exportar(entrada: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    with gzip.open(entrada, "rt", encoding="utf-8") as f:
        oa = {r["id"]: r for r in map(json.loads, f)}
    ei = [r for r in pq.read_table(SNAPSHOT / "elos_institution.parquet").to_pylist()
          if r["origem"] not in CONF]  # rodar de novo: refaz só o que esta ferramenta acrescenta
    pnipe = pq.read_table(SNAPSHOT / "pnipe_institution.parquet",
                          columns=["cnpj", "social_reason", "initials", "state"]).to_pylist()
    novos: list[dict] = []
    motivo = Counter()

    # 1) Por nome: instituições do PNIPE sem par.
    casados = {r["cnpj"] for r in ei}
    por_nome, por_sigla = defaultdict(set), defaultdict(set)
    for i in oa.values():
        for n in [i["nome"], *i["alternativos"]]:
            por_nome[_norm(n)].add(i["id"])
        for s in i["siglas"]:
            por_sigla[(_norm(s), i["uf"])].add(i["id"])
    for p in pnipe:
        if p["cnpj"] in casados:
            continue
        cand, origem = por_nome.get(_norm(p["social_reason"]), set()), "nome_openalex"
        if len(cand) != 1 and p["initials"]:
            cand, origem = por_sigla.get((_norm(p["initials"]), p["state"]), set()), "sigla_uf"
        if len(cand) == 1:
            i = oa[next(iter(cand))]
            if i["uf"] and p["state"] and i["uf"] != p["state"] and "estado" in _norm(i["nome"]).split():
                continue  # órgão estadual de nome genérico em outro estado (ex.: secretarias de C&T)
            novos.append({"cnpj": p["cnpj"], "openalex_id": i["id"], "nome_openalex": i["nome"], "ror": i["ror"],
                          "confianca": CONF[origem], "origem": origem})
            motivo[origem] += 1

    # 2) Por lineage: unidades sem par herdam o CNPJ da mãe mais próxima que tenha par.
    melhor: dict[str, tuple[float, str]] = {}
    for r in [*ei, *novos]:
        if r["openalex_id"] and (r["openalex_id"] not in melhor or r["confianca"] > melhor[r["openalex_id"]][0]):
            melhor[r["openalex_id"]] = (r["confianca"], r["cnpj"])
    for i in oa.values():
        if i["id"] in melhor:
            continue
        if i["tipo"] == "funder":  # agência (FINEP, CNPq) não é unidade de ninguém
            continue
        for mae in [*i["maes"], *i["parent"]]:
            if (oa.get(mae) or {}).get("tipo") in NAO_HERDA:
                break  # ministério ou financiador como "mãe" (INCTs, FINEP sob o MCTI): não é a mesma instituição
            if mae in melhor and melhor[mae][0] >= MAE_MIN:
                novos.append({"cnpj": melhor[mae][1], "openalex_id": i["id"], "nome_openalex": i["nome"],
                              "ror": i["ror"], "confianca": CONF["lineage"], "origem": "lineage"})
                motivo["lineage"] += 1
                break

    schema = pq.read_schema(SNAPSHOT / "elos_institution.parquet")
    pq.write_table(pa.Table.from_pylist([*ei, *novos], schema=schema), SNAPSHOT / "elos_institution.parquet",
                   compression="zstd")
    s = pa.string()
    pq.write_table(pa.Table.from_pylist([
        {"id": i["id"], "ror": i["ror"], "nome": i["nome"], "siglas": i["siglas"], "tipo": i["tipo"],
         "cidade": i["cidade"], "uf": i["uf"], "publicacoes": i["publicacoes"],
         "mae": (i["maes"] or i["parent"] or [None])[0]} for i in oa.values()],
        schema=pa.schema([("id", s), ("ror", s), ("nome", s), ("siglas", pa.list_(s)), ("tipo", s), ("cidade", s),
                          ("uf", s), ("publicacoes", pa.int64()), ("mae", s)])),
        SNAPSHOT / "oa_institution_br.parquet", compression="zstd")

    meta_path = SNAPSHOT / "snapshot.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["tables"]["oa_institution_br"] = {"rows": len(oa)}
    meta["tables"]["elos_institution"] = {"rows": len(ei) + len(novos)}
    meta["instituicoes_ror"] = {"exportado_em": datetime.now(UTC).isoformat(timespec="seconds"),
                                "instituicoes_br": len(oa), "casamentos_novos": dict(motivo),
                                "pnipe_com_par": len({r["cnpj"] for r in [*ei, *novos]}), "pnipe": len(pnipe)}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta["instituicoes_ror"], ensure_ascii=False, indent=1))
    for r in novos:
        if r["origem"] != "lineage":
            p = next(x for x in pnipe if x["cnpj"] == r["cnpj"])
            print(f"  {r['origem']}: {p['initials']} {p['social_reason'][:50]} -> {r['nome_openalex']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("coletar")
    a.add_argument("--saida", type=Path, required=True)
    b = sub.add_parser("exportar")
    b.add_argument("entrada", type=Path)
    x = p.parse_args()
    coletar(x.saida) if x.cmd == "coletar" else exportar(x.entrada)


if __name__ == "__main__":
    main()
