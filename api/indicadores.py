"""Indicadores de impacto (laboratório e instituição) e congressos <-> pesquisadores que publicam neles.

Tudo sai das publicações desde 2019 dos responsáveis (OpenAlex), calculado por
`tools/indicadores_congressos.py`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from api.guard import guard
from api.plataforma import MAX, PAGE, PER_PAGE, _all, _one, _page, cnpj_or_ror

infra = APIRouter(prefix="/v1", tags=["Indicadores e congressos (OpenAlex)"])
pessoas = APIRouter(prefix="/v1/elos", tags=["Indicadores e congressos (OpenAlex)"])
NOTE = ("Publicações desde 2019 do OpenAlex (CC0). FWCI: citações relativas à média mundial da área e do ano (1 = média). "
        "`fwci_medio` apara cada publicação em 10 (artigos de consórcio passam de 500). `posicao_top10`: percentil do "
        "% no top 10% entre os pares com 10+ publicações (100 = melhor). Publicações recentes ainda têm poucas citações.")
IND_COLS = ("publicacoes, citacoes, fwci_medio, fwci_mediano, pct_top10, pct_top1, pct_acesso_aberto, "
            "pct_com_financiador, autores_mediana, desde, ate, posicao_top10")


def _ind(r: tuple) -> dict[str, Any]:
    keys = [c.strip() for c in IND_COLS.split(",")]
    return dict(zip(keys, r))


def _extras(works_sql: str, params: list[Any]) -> dict[str, Any]:
    ods = _all(f"SELECT ods_id, ods, count(*) n FROM oa_work_sdg WHERE work_id IN ({works_sql}) "
               "GROUP BY ALL ORDER BY n DESC LIMIT 5", params)
    fin = _all(f"SELECT f.id, f.nome, count(*) n FROM oa_work_funder x JOIN oa_funder f ON f.id = x.funder_id "
               f"WHERE x.work_id IN ({works_sql}) GROUP BY ALL ORDER BY n DESC LIMIT 5", params)
    return {"ods": [{"id": x[0], "nome": x[1], "publicacoes": x[2]} for x in ods],
            "financiadores": [{"id": x[0], "nome": x[1], "publicacoes": x[2]} for x in fin]}


@infra.get("/laboratorios/{lab_id}/indicadores", summary="Indicadores de impacto de um laboratório")
def lab_indicators(lab_id: int, request: Request, response: Response) -> dict:
    """Publicações do responsável e da equipe. Laboratórios com o mesmo responsável têm os mesmos números."""
    guard(request, response)
    r = _one(f"SELECT {IND_COLS} FROM ind_laboratorio WHERE lab_id = ?", [lab_id])
    if not r:
        raise HTTPException(404, "Laboratório sem publicações do responsável no OpenAlex.")
    works = ("SELECT a.work_id FROM oa_work_author a JOIN elos_researcher_lab rl ON rl.researcher_id = a.researcher_id "
             "WHERE rl.lab_id = ? AND rl.papel <> 'coautor'")
    return {"laboratorio": lab_id, **_ind(r), **_extras(works, [lab_id]), "nota": NOTE,
            "publicacoes_url": f"/v1/elos/laboratorios/{lab_id}/publicacoes"}


@infra.get("/instituicoes/{cnpj}/indicadores", summary="Indicadores de impacto de uma instituição")
def institution_indicators(cnpj: str, request: Request, response: Response) -> dict:
    """Publicações dos responsáveis e da equipe com o CNPJ (aceita ROR e id do OpenAlex)."""
    guard(request, response)
    d = cnpj_or_ror(cnpj)
    r = _one(f"SELECT {IND_COLS} FROM ind_instituicao WHERE cnpj = ?", [d])
    if not r:
        raise HTTPException(404, "Instituição sem publicações de pesquisadores da base.")
    works = ("SELECT a.work_id FROM oa_work_author a JOIN elos_researcher r ON r.id = a.researcher_id "
             "WHERE r.inst_cnpj = ? AND r.origem <> 'coautor'")
    return {"cnpj": d, **_ind(r), **_extras(works, [d]), "nota": NOTE}


@infra.get("/indicadores/laboratorios", summary="Laboratórios ordenados por um indicador")
def rank_labs(request: Request, response: Response,
              ordem: str = Query("pct_top10", pattern="^(pct_top10|fwci_mediano|publicacoes|citacoes|pct_acesso_aberto)$"),
              minimo: int = Query(10, ge=1, le=1000, description="publicações mínimas"),
              uf: str | None = Query(None, min_length=2, max_length=2), page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    guard(request, response)
    where, params = ["i.publicacoes >= ?"], [minimo]
    if uf:
        where.append("l.resolved_state = ?")
        params.append(uf.upper())
    out = _page("ind_laboratorio i JOIN pnipe_laboratory l ON l.id = i.lab_id", where, params,
                f"i.lab_id, l.name, l.initials, l.institution_cnpj, l.resolved_state, {', '.join('i.' + c.strip() for c in IND_COLS.split(','))}",
                f"i.{ordem} DESC NULLS LAST, i.lab_id", page, per_page,
                lambda r: {"id": r[0], "nome": r[1], "sigla": r[2], "instituicao_cnpj": r[3], "uf": r[4], **_ind(r[5:])})
    return {**out, "nota": NOTE}


# --------------------------------------------------------------------------- congressos
def _venue(cid: int) -> tuple | None:
    return _one("SELECT veiculo_id, veiculo, semelhanca FROM congresso_veiculo WHERE congresso_id = ?", [cid])


@infra.get("/congressos/{congress_id}/pesquisadores", summary="Pesquisadores da base que publicam no congresso")
def congress_researchers(congress_id: int, request: Request, response: Response,
                         page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    """Pelos anais do congresso no OpenAlex (casados pela sigla e pelo nome): quem da base (responsáveis e
    equipe) já publicou lá desde 2019, do que mais publicou para o que menos."""
    guard(request, response)
    c = _one("SELECT id, title, acronym, start_date FROM congresso WHERE id = ?", [congress_id])
    if not c:
        raise HTTPException(404, "Congresso não encontrado.")
    v = _venue(congress_id)
    head = {"congresso": {"id": c[0], "titulo": c[1], "sigla": c[2], "inicio": c[3].isoformat() if c[3] else None}}
    if not v:
        return {**head, "veiculo": None, "total": 0, "page": page, "per_page": per_page, "items": [],
                "motivo_sem_lista": "Não achamos os anais deste congresso entre as publicações da base."}
    src = ("(SELECT a.researcher_id rid, count(*) n, max(w.ano) ultimo FROM oa_work w "
           "JOIN oa_work_author a ON a.work_id = w.id WHERE w.veiculo_id = ? GROUP BY 1) x "
           "JOIN elos_researcher r ON r.id = x.rid")
    out = _page(src, ["r.origem <> 'coautor'"], [v[0]], "r.id, r.nome, r.instituicao_sigla, x.n, x.ultimo",
                "x.n DESC, x.ultimo DESC, r.id", page, per_page,
                lambda r: {"id": r[0], "nome": r[1], "instituicao_sigla": r[2], "publicacoes_no_congresso": r[3],
                           "ultimo_ano": r[4]})
    return {**head, "veiculo": {"openalex": v[0], "nome": v[1], "semelhanca": v[2]}, **out,
            "nota": "Anais casados pela sigla e por metade ou mais das palavras do nome; publicações desde 2019."}


@pessoas.get("/pesquisadores/{researcher_id}/congressos", summary="Congressos em que a pessoa costuma publicar")
def researcher_congresses(researcher_id: str, request: Request, response: Response,
                          futuros: bool = Query(True, description="só congressos que ainda vão acontecer"),
                          limite: int = Query(20, ge=1, le=MAX)) -> dict:
    guard(request, response)
    where, params = ["a.researcher_id = ?"], [researcher_id]
    if futuros:
        where.append("c.start_date >= ?")
        params.append(datetime.now(UTC).date())
    rows = _all("SELECT c.id, c.title, c.acronym, c.start_date, c.location, v.veiculo, count(DISTINCT w.id) n "
                "FROM congresso_veiculo v JOIN congresso c ON c.id = v.congresso_id "
                "JOIN oa_work w ON w.veiculo_id = v.veiculo_id JOIN oa_work_author a ON a.work_id = w.id "
                f"WHERE {' AND '.join(where)} GROUP BY ALL ORDER BY c.start_date, c.id LIMIT ?", [*params, limite])
    return {"pesquisador": researcher_id, "items": [
        {"id": r[0], "titulo": r[1], "sigla": r[2], "inicio": r[3].isoformat() if r[3] else None, "local": r[4],
         "veiculo": r[5], "publicacoes_no_veiculo": r[6]} for r in rows]}


# --------------------------------------------------------------------------- sinergia por citação
sinergia = APIRouter(prefix="/v1/sinergia", tags=["Sinergia"])


@sinergia.get("/laboratorios/{lab_id}/citacoes", summary="Laboratórios com a mesma base de conhecimento (citações)")
def lab_citation_synergy(lab_id: int, request: Request, response: Response,
                         so_novos: bool = Query(False, description="só pares que ainda não publicam juntos")) -> dict:
    """Até 15 laboratórios que citam as mesmas referências (acoplamento bibliográfico: cosseno TF-IDF, a
    referência genérica pesa pouco) e as citações diretas nos dois sentidos. Laboratórios com gente em
    comum ficam de fora. `so_novos` mostra só quem nunca publicou junto: candidatos a parceria."""
    guard(request, response)
    where = "x.lab_a = ?" + (" AND NOT x.publicam_juntos" if so_novos else "")
    rows = _all("SELECT x.lab_b, l.name, l.initials, i.initials, l.resolved_state, x.acoplamento, x.referencias_em_comum, "
                "x.cita_b, x.citado_por_b, x.publicam_juntos FROM sinergia_citacao x "
                "LEFT JOIN pnipe_laboratory l ON l.id = x.lab_b LEFT JOIN pnipe_institution i ON i.cnpj = l.institution_cnpj "
                f"WHERE {where} ORDER BY x.acoplamento DESC, x.lab_b", [lab_id])
    return {"laboratorio": lab_id, "nota": (
        "Inferência pela base de conhecimento: referências citadas nas publicações desde 2019 do responsável e da "
        "equipe (OpenAlex). `cita` = publicações deste laboratório que citam as do outro; `citado_por`, o inverso."),
        "items": [{"id": r[0], "nome": r[1], "sigla": r[2], "instituicao_sigla": r[3], "uf": r[4], "acoplamento": r[5],
                   "referencias_em_comum": r[6], "cita": r[7], "citado_por": r[8], "publicam_juntos": r[9]} for r in rows]}


ROUTERS = (infra, pessoas, sinergia)
