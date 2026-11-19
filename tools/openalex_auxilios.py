"""Etapa 2 do plano do OpenAlex: financiadores e auxílios.

Coleta os financiadores brasileiros e os auxílios que as próprias agências publicam no
OpenAlex (FAPESP pela Biblioteca Virtual, FAPEMIG pelos projetos contratados, CAPES pela
cooperação internacional e FINEP/Funttel): título, modalidade, vigência, valor e o
pesquisador responsável. Os auxílios que o OpenAlex só deduz dos agradecimentos das
publicações já estão em `oa_award` (etapa 1). Custa ~US$ 0,10 por mil páginas de 200.

    # 1) no ambiente do FomentaBR (usa OPENALEX_API_KEY do .env; nada no banco)
    cd <FomentaBR>
    PYTHONPATH=src python <fomenta-api>/tools/openalex_auxilios.py coletar --saida auxilios.jsonl.gz

    # 2) no ambiente da API: grava oa_auxilio, oa_funder_agencia e oa_award_finep no retrato
    python tools/openalex_auxilios.py exportar auxilios.jsonl.gz

Só entram auxílios ligados à base: o responsável é pesquisador do Elos, a instituição dele
está no PNIPE ou alguma publicação da base cita o auxílio. O nome do responsável só vai
junto quando ele está na base ou tem ORCID (sem contato de ninguém).
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "build" / "snapshot"

# Origens em que a própria agência publica o auxílio (as demais vêm dos agradecimentos).
CURADAS = ("fapesp_bv", "fapemig_projetos_contratados", "capes_cooperacao_internacional", "finep_funttel")
AWARD_SELECT = ("id,display_name,funder_award_id,funder,amount,currency,amount_usd,funding_type,funder_scheme,"
                "start_date,end_date,landing_page_url,provenance,lead_investigator,funded_outputs_count")
FUNDER_SELECT = "id,display_name,alternate_titles,awards_count,works_count,ids,country_code"

# Agência dos editais (slug em `agencia`) -> financiador no OpenAlex. Conferido à mão em 19/11/2026
# entre os 404 financiadores brasileiros; FAPESPA, FAPERR, FAPERO e FAPT não aparecem lá.
AGENCIAS = {
    "fapesp": "F4320320997", "facepe": "F4320323678", "finep": "F4320322904", "fapern": "F4320323352",
    "fapema": "F4320322137", "fapdf": "F4320323350", "faperj": "F4320322749", "cnpq": "F4320322025",
    "fapesb": "F4320323681", "fapemig": "F4320322980", "funcap": "F4320323202", "fapepi": "F4320322989",
    "fapesc": "F4320323349", "fapesq": "F4320323351", "fapes": "F4320323682", "araucaria": "F4320322772",
    "fundect": "F4320323354", "fapeg": "F4320323204", "fapac": "F4320323206", "fapeal": "F4320321887",
    "fapeam": "F4320322994", "fapemat": "F4320323205", "fapergs": "F4320322502", "fapitec": "F4320323353",
    "fapeap": "F4320318857", "petrobras": "F4320322468", "embrapii": "F4320330107",
}
FINEP = "F4320322904"
# Contrato FINEP: 01.20.0026.00 (o último par muda com aditivos; o casamento usa os 10 primeiros).
FINEP_CONTRATO = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\.\d{2}\b")


def _sid(url: str | None) -> str | None:
    return url.rstrip("/").rsplit("/", 1)[-1] if url else None


def contrato_finep(processo: str | None) -> list[str]:
    return sorted({".".join(m) for m in FINEP_CONTRATO.findall(processo or "")})


def _slim_award(a: dict[str, Any]) -> dict[str, Any]:
    li = a.get("lead_investigator") or {}
    author = li.get("author") or {}
    insts = li.get("institutions") or []
    nome = " ".join(p for p in (li.get("given_name"), li.get("family_name")) if p) or author.get("display_name")
    return {
        "id": _sid(a["id"]), "processo": a.get("funder_award_id"), "titulo": a.get("display_name"),
        "funder_id": _sid((a.get("funder") or {}).get("id")), "modalidade": a.get("funder_scheme"),
        "tipo": a.get("funding_type"), "inicio": a.get("start_date"), "fim": a.get("end_date"),
        "valor": a.get("amount"), "moeda": a.get("currency"), "valor_usd": a.get("amount_usd"),
        "fonte": a.get("provenance"), "pagina": a.get("landing_page_url"),
        "publicacoes": a.get("funded_outputs_count"),
        "investigador_oa": _sid(author.get("id")), "investigador_nome": nome,
        "investigador_orcid": (author.get("orcid") or li.get("orcid") or "").rsplit("/", 1)[-1] or None,
        "inst_oa": _sid(insts[0].get("id")) if insts else None, "inst_ror": insts[0].get("ror") if insts else None,
        "afiliacao": ((li.get("affiliation") or {}).get("name")),
    }


# ─────────────────────────────────────────────────────────────── fase 1: coletar
def coletar(saida: Path) -> None:
    from fomenta.config import settings
    from fomenta.elos.openalex import BudgetExceededError, OpenAlexClient

    if not settings.openalex_api_key:
        sys.exit("Defina OPENALEX_API_KEY no .env do FomentaBR (paginação por cursor exige chave).")
    client = OpenAlexClient(api_key=settings.openalex_api_key, cache_dir=settings.openalex_cache_dir,
                            daily_budget_usd=settings.openalex_daily_budget_usd)
    n = Counter()
    try:
        with gzip.open(saida, "wt", encoding="utf-8") as out:
            for f in client.iterate("funders", "country_code:BR", FUNDER_SELECT):
                ids = f.get("ids") or {}
                out.write(json.dumps({"tipo": "funder", "id": _sid(f["id"]), "nome": f.get("display_name"),
                                      "ror": ids.get("ror"), "crossref": ids.get("crossref"),
                                      "auxilios": f.get("awards_count"), "publicacoes": f.get("works_count")},
                                     ensure_ascii=False) + "\n")
                n["funder"] += 1
            flt = f"funder.country_code:BR,provenance:{'|'.join(CURADAS)}"
            for a in client.iterate("awards", flt, AWARD_SELECT):
                out.write(json.dumps(_slim_award(a), ensure_ascii=False) + "\n")
                n["award"] += 1
                if n["award"] % 20000 == 0:
                    print(f"{n['award']} auxílios, {client.requests} requisições, US$ {client.spent_usd:.4f}",
                          flush=True)
    except BudgetExceededError as e:
        print(f"Parado pelo teto diário ({e}). Rode de novo amanhã: o cache retoma.")
        sys.exit(2)
    print(f"pronto: {n['funder']} financiadores e {n['award']} auxílios, {client.requests} requisições, "
          f"US$ {client.spent_usd:.4f} -> {saida}")


# ─────────────────────────────────────────────────────────────── fase 2: exportar
def exportar(entrada: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    er = pq.read_table(SNAPSHOT / "elos_researcher.parquet", columns=["id", "openalex_ids", "orcid"]).to_pylist()
    alias = {oa: r["id"] for r in er for oa in r["openalex_ids"] or []}
    by_orcid = {r["orcid"].rsplit("/", 1)[-1]: r["id"] for r in er if r.get("orcid")}
    inst = pq.read_table(SNAPSHOT / "elos_institution.parquet").to_pylist()
    cnpj_by_oa = {i["openalex_id"]: i["cnpj"] for i in inst if i.get("openalex_id")}
    cnpj_by_ror = {i["ror"]: i["cnpj"] for i in inst if i.get("ror")}
    cited = set(pq.read_table(SNAPSHOT / "oa_work_award.parquet", columns=["award_id"]).column("award_id").to_pylist())

    funders, awards = [], []
    motivo = Counter()
    with gzip.open(entrada, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if "processo" not in r:  # financiador (no auxílio, "tipo" é o tipo de financiamento)
                r.pop("tipo", None)
                funders.append(r)
                continue
            rid = alias.get(r["investigador_oa"]) or by_orcid.get(r["investigador_orcid"] or "")
            cnpj = cnpj_by_oa.get(r["inst_oa"]) or cnpj_by_ror.get(r["inst_ror"])
            if not (rid or cnpj or r["id"] in cited):
                motivo["fora da base"] += 1
                continue
            motivo["pesquisador da base" if rid else "instituição da base" if cnpj else "citado na base"] += 1
            # Nome do responsável só se estiver na base ou tiver ORCID (regra do plano).
            nome = r["investigador_nome"] if (rid or r["investigador_orcid"]) else None
            awards.append({
                "id": r["id"], "processo": r["processo"], "titulo": r["titulo"], "funder_id": r["funder_id"],
                "modalidade": r["modalidade"], "tipo": r["tipo"], "inicio": r["inicio"], "fim": r["fim"],
                "ano": int(r["inicio"][:4]) if r["inicio"] else None,
                "valor": r["valor"], "moeda": r["moeda"], "valor_usd": r["valor_usd"], "fonte": r["fonte"],
                "pagina": r["pagina"], "publicacoes": r["publicacoes"], "researcher_id": rid,
                "investigador": nome, "orcid": r["investigador_orcid"] if nome else None, "inst_cnpj": cnpj,
                "afiliacao": r["afiliacao"],
            })

    # Agências dos editais <-> financiadores.
    known = {fu["id"] for fu in funders}
    agencia = [{"funder_id": fid, "agencia": slug} for slug, fid in AGENCIAS.items() if fid in known]
    missing = sorted(slug for slug, fid in AGENCIAS.items() if fid not in known)
    if missing:
        print(f"aviso: financiador não veio na coleta para {missing}")

    # Contratos FINEP citados (nos agradecimentos ou nos auxílios curados) para casar com os
    # projetos FINEP da API de investimentos (`funding_award.source_key`).
    finep = []
    seen = set()
    award_rows = pq.read_table(SNAPSHOT / "oa_award.parquet", columns=["id", "processo", "funder_id"]).to_pylist()
    for a in [*award_rows, *awards]:
        if a["funder_id"] != FINEP and not FINEP_CONTRATO.search(a["processo"] or ""):
            continue
        for c in contrato_finep(a["processo"]):
            if (a["id"], c) not in seen:
                seen.add((a["id"], c))
                finep.append({"award_id": a["id"], "contrato": c})

    def write(name: str, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), SNAPSHOT / f"{name}.parquet", compression="zstd")

    s = pa.string()
    write("oa_auxilio", awards, pa.schema([
        ("id", s), ("processo", s), ("titulo", s), ("funder_id", s), ("modalidade", s), ("tipo", s),
        ("inicio", s), ("fim", s), ("ano", pa.int32()), ("valor", pa.float64()), ("moeda", s),
        ("valor_usd", pa.float64()), ("fonte", s), ("pagina", s), ("publicacoes", pa.int32()),
        ("researcher_id", s), ("investigador", s), ("orcid", s), ("inst_cnpj", s), ("afiliacao", s)]))
    write("oa_funder_br", funders, pa.schema([
        ("id", s), ("nome", s), ("ror", s), ("crossref", s), ("auxilios", pa.int64()), ("publicacoes", pa.int64())]))
    write("oa_funder_agencia", agencia, pa.schema([("funder_id", s), ("agencia", s)]))
    write("oa_award_finep", finep, pa.schema([("award_id", s), ("contrato", s)]))

    meta_path = SNAPSHOT / "snapshot.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for name, rows in (("oa_auxilio", awards), ("oa_funder_br", funders), ("oa_funder_agencia", agencia),
                       ("oa_award_finep", finep)):
        meta["tables"][name] = {"rows": len(rows)}
    meta["openalex_auxilios"] = {"exportado_em": datetime.now(UTC).isoformat(timespec="seconds"),
                                 "origens": list(CURADAS), "auxilios": len(awards), "motivos": dict(motivo),
                                 "financiadores_br": len(funders), "agencias_casadas": len(agencia),
                                 "contratos_finep_citados": len(finep)}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta["openalex_auxilios"], ensure_ascii=False, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("coletar")
    c.add_argument("--saida", type=Path, required=True)
    e = sub.add_parser("exportar")
    e.add_argument("entrada", type=Path)
    a = p.parse_args()
    if a.cmd == "coletar":
        coletar(a.saida)
    else:
        exportar(a.entrada)


if __name__ == "__main__":
    main()
