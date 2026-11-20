"""Rotas de publicações e financiamento (OpenAlex), ligadas às pessoas e laboratórios do Elos.

Publicações dos responsáveis desde 2019 (`tools/openalex_obras.py`), com veículo, impacto,
palavras-chave, ODS, financiadores e auxílios (nº do processo informado pelo financiador).
Só entram publicações com alguém da base; quem pediu remoção do Elos não aparece como autor.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from api.guard import guard
from api.plataforma import MAX, PAGE, PER_PAGE, Q, _all, _one, _page, _search

publicacoes = APIRouter(prefix="/v1/publicacoes", tags=["Publicações e financiamento (OpenAlex)"])
pessoas = APIRouter(prefix="/v1/elos", tags=["Publicações e financiamento (OpenAlex)"])
NOTE = ("Publicações desde 2019 dos responsáveis por laboratórios do PNIPE, do OpenAlex (CC0). Financiadores e "
        "auxílios como informados nas publicações; o valor dos auxílios não está aqui.")
W_COLS = ("w.id, w.titulo, w.ano, w.tipo, w.doi, w.veiculo, w.veiculo_tipo, w.acesso_aberto, w.oa_url, w.citacoes, "
          "w.fwci, w.percentil, w.top10, w.n_autores")
N_W = len(W_COLS.split(","))
UNICO = "w.id IN (SELECT work_id FROM oa_work_author WHERE list_contains(?, researcher_id))"


def _work(r: tuple) -> dict[str, Any]:
    return {"id": r[0], "titulo": r[1], "ano": r[2], "tipo": r[3], "doi": r[4], "veiculo": r[5],
            "veiculo_tipo": r[6], "acesso_aberto": r[7], "oa_url": r[8], "citacoes": r[9], "fwci": r[10],
            "percentil": r[11], "top10": r[12], "n_autores": r[13], "openalex": f"https://openalex.org/{r[0]}"}


def _enrich(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Autores da base, financiadores, auxílios, palavras-chave e ODS de cada publicação (uma consulta por tipo)."""
    ids = [w["id"] for w in items]
    if not ids:
        return items
    by: dict[str, dict[str, list]] = {i: {"autores": [], "financiadores": [], "auxilios": [], "palavras_chave": [],
                                          "ods": []} for i in ids}
    for wid, rid, nome, pos, corr in _all(
            "SELECT a.work_id, a.researcher_id, r.nome, a.posicao, a.correspondente FROM oa_work_author a "
            "JOIN elos_researcher r ON r.id = a.researcher_id WHERE list_contains(?, a.work_id)", [ids]):
        by[wid]["autores"].append({"id": rid, "nome": nome, "posicao": pos, "correspondente": corr})
    for wid, fid, nome in _all("SELECT x.work_id, f.id, f.nome FROM oa_work_funder x JOIN oa_funder f ON f.id = x.funder_id "
                               "WHERE list_contains(?, x.work_id)", [ids]):
        by[wid]["financiadores"].append({"id": fid, "nome": nome})
    for wid, aid, proc, fin in _all("SELECT x.work_id, a.id, a.processo, a.financiador FROM oa_work_award x "
                                    "JOIN oa_award a ON a.id = x.award_id WHERE list_contains(?, x.work_id)", [ids]):
        by[wid]["auxilios"].append({"id": aid, "processo": proc, "financiador": fin})
    for wid, nome in _all("SELECT x.work_id, k.nome FROM oa_work_keyword x JOIN oa_keyword k ON k.id = x.keyword_id "
                          "WHERE list_contains(?, x.work_id) ORDER BY x.nota DESC", [ids]):
        by[wid]["palavras_chave"].append(nome)
    for wid, ods in _all("SELECT work_id, ods FROM oa_work_sdg WHERE list_contains(?, work_id) ORDER BY nota DESC", [ids]):
        by[wid]["ods"].append(ods)
    return [{**w, **by[w["id"]]} for w in items]


def _filters(where: list[str], params: list[Any], ano: int | None, financiador: str | None, ods: str | None,
             q: str | None) -> None:
    if ano:
        where.append("w.ano = ?")
        params.append(ano)
    if financiador:
        where.append("w.id IN (SELECT work_id FROM oa_work_funder WHERE funder_id = ?)")
        params.append(financiador.upper())
    if ods:
        where.append("w.id IN (SELECT work_id FROM oa_work_sdg WHERE ods_id = ?)")
        params.append(ods)
    if q:
        _search("w.busca", q, where, params)


ANO = Query(None, ge=2019, le=2100)
FIN = Query(None, max_length=20, description="id do financiador no OpenAlex (F…), ver /v1/publicacoes/financiadores")
ODS = Query(None, max_length=4, description="número do ODS (1 a 17)")
ORDEM = Query("recentes", pattern="^(recentes|citadas|impacto)$")
ORDER_SQL = {"recentes": "w.ano DESC, w.citacoes DESC, w.id", "citadas": "w.citacoes DESC NULLS LAST, w.id",
             "impacto": "w.fwci DESC NULLS LAST, w.id"}


def _list(where: list[str], params: list[Any], ordem: str, page: int, per_page: int) -> dict[str, Any]:
    out = _page("oa_work w", where, params, W_COLS, ORDER_SQL[ordem], page, per_page, _work)
    out["items"] = _enrich(out["items"])
    return {**out, "nota": NOTE}


@publicacoes.get("", summary="Busca nas publicações (quem publicou sobre X)")
def search_works(request: Request, response: Response, q: str | None = Q, ano: int | None = ANO,
                 financiador: str | None = FIN, ods: str | None = ODS, ordem: str = ORDEM,
                 page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    """Busca no título (sem acento e sem caixa); cada publicação traz os autores que estão no Elos."""
    guard(request, response)
    where, params = [], []
    _filters(where, params, ano, financiador, ods, q)
    return _list(where, params, ordem, page, per_page)


@publicacoes.get("/financiadores", summary="Financiadores citados nas publicações")
def funders(request: Request, response: Response, q: str | None = Q,
            page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    guard(request, response)
    where, params = [], []
    if q:
        _search("lower(strip_accents(f.nome))", q, where, params)
    return _page("oa_funder f", where, params,
                 "f.id, f.nome, f.ror, (SELECT count(*) FROM oa_work_funder x WHERE x.funder_id = f.id) AS n",
                 "n DESC, f.id", page, per_page,
                 lambda r: {"id": r[0], "nome": r[1], "ror": r[2], "publicacoes": r[3]})


@publicacoes.get("/financiadores/{funder_id}", summary="Quem um financiador apoiou")
def funder(funder_id: str, request: Request, response: Response, limite: int = Query(20, ge=1, le=MAX)) -> dict:
    """Publicações por ano, os pesquisadores e laboratórios com mais publicações financiadas e os
    auxílios (nº do processo) mais citados."""
    guard(request, response)
    fid = funder_id.upper()
    f = _one("SELECT id, nome, ror FROM oa_funder WHERE id = ?", [fid])
    if not f:
        raise HTTPException(404, "Financiador não encontrado nas publicações.")
    works = "SELECT work_id FROM oa_work_funder WHERE funder_id = ?"
    by_year = _all(f"SELECT w.ano, count(*) FROM oa_work w WHERE w.id IN ({works}) GROUP BY 1 ORDER BY 1", [fid])
    people = _all(f"SELECT r.id, r.nome, r.instituicao_sigla, count(*) n FROM oa_work_author a "
                  f"JOIN elos_researcher r ON r.id = a.researcher_id WHERE a.work_id IN ({works}) "
                  "GROUP BY ALL ORDER BY n DESC, r.id LIMIT ?", [fid, limite])
    labs = _all(f"SELECT rl.lab_id, l.name, l.initials, count(DISTINCT a.work_id) n FROM oa_work_author a "
                "JOIN elos_researcher_lab rl ON rl.researcher_id = a.researcher_id AND rl.papel <> 'coautor' "
                f"LEFT JOIN pnipe_laboratory l ON l.id = rl.lab_id WHERE a.work_id IN ({works}) "
                "GROUP BY ALL ORDER BY n DESC, rl.lab_id LIMIT ?", [fid, limite])
    awards = _all("SELECT a.id, a.processo, count(*) n FROM oa_award a JOIN oa_work_award x ON x.award_id = a.id "
                  "WHERE a.funder_id = ? GROUP BY ALL ORDER BY n DESC, a.id LIMIT ?", [fid, limite])
    return {"id": f[0], "nome": f[1], "ror": f[2], "nota": NOTE,
            "publicacoes_por_ano": [{"ano": y, "publicacoes": n} for y, n in by_year],
            "pesquisadores": [{"id": x[0], "nome": x[1], "instituicao_sigla": x[2], "publicacoes": x[3]} for x in people],
            "laboratorios": [{"id": x[0], "nome": x[1], "sigla": x[2], "publicacoes": x[3]} for x in labs],
            "auxilios": [{"id": x[0], "processo": x[1], "publicacoes": x[2]} for x in awards],
            "publicacoes_url": f"/v1/publicacoes?financiador={fid}"}


@publicacoes.get("/{work_id}", summary="Uma publicação")
def work(work_id: str, request: Request, response: Response) -> dict:
    guard(request, response)
    r = _one(f"SELECT {W_COLS} FROM oa_work w WHERE w.id = ?", [work_id.upper()])
    if not r:
        raise HTTPException(404, "Publicação não encontrada (só as dos responsáveis desde 2019).")
    return {**_enrich([_work(r)])[0], "nota": NOTE}


def _lab_people(lab_id: int) -> list[str]:
    return [x[0] for x in _all("SELECT researcher_id FROM elos_researcher_lab WHERE lab_id = ? AND papel <> 'coautor'",
                               [lab_id])]


def _funding(people: list[str]) -> dict[str, Any]:
    if not people:
        return {"publicacoes": 0, "com_financiador": 0, "financiadores": [], "auxilios": []}
    total, funded = _one(f"SELECT count(*), count(*) FILTER (WHERE w.id IN (SELECT work_id FROM oa_work_funder)) "
                         f"FROM oa_work w WHERE {UNICO}", [people])
    fun = _all(f"SELECT f.id, f.nome, count(*) n, min(w.ano), max(w.ano) FROM oa_work w "
               f"JOIN oa_work_funder x ON x.work_id = w.id JOIN oa_funder f ON f.id = x.funder_id WHERE {UNICO} "
               "GROUP BY ALL ORDER BY n DESC, f.id LIMIT 30", [people])
    aw = _all(f"SELECT a.id, a.processo, a.financiador, count(*) n, max(w.ano) FROM oa_work w "
              f"JOIN oa_work_award x ON x.work_id = w.id JOIN oa_award a ON a.id = x.award_id WHERE {UNICO} "
              "GROUP BY ALL ORDER BY n DESC, a.id LIMIT 50", [people])
    return {"publicacoes": total, "com_financiador": funded, "nota": NOTE,
            "financiadores": [{"id": x[0], "nome": x[1], "publicacoes": x[2], "de": x[3], "ate": x[4]} for x in fun],
            "auxilios": [{"id": x[0], "processo": x[1], "financiador": x[2], "publicacoes": x[3], "ultimo_ano": x[4]}
                         for x in aw]}


@pessoas.get("/pesquisadores/{researcher_id}/publicacoes", summary="Publicações de um pesquisador")
def researcher_works(researcher_id: str, request: Request, response: Response, q: str | None = Q,
                     ano: int | None = ANO, financiador: str | None = FIN, ods: str | None = ODS,
                     ordem: str = ORDEM, page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    """As publicações coletadas em que a pessoa aparece (dos responsáveis desde 2019; para quem não é
    responsável, só as escritas com um)."""
    guard(request, response)
    where, params = [UNICO], [[researcher_id]]
    _filters(where, params, ano, financiador, ods, q)
    return _list(where, params, ordem, page, per_page)


@pessoas.get("/pesquisadores/{researcher_id}/financiamentos", summary="Quem financiou as publicações de um pesquisador")
def researcher_funding(researcher_id: str, request: Request, response: Response) -> dict:
    guard(request, response)
    return {"pesquisador": researcher_id, **_funding([researcher_id])}


@pessoas.get("/laboratorios/{lab_id}/publicacoes", summary="Publicações do responsável e da equipe de um laboratório")
def lab_works(lab_id: int, request: Request, response: Response, q: str | None = Q, ano: int | None = ANO,
              financiador: str | None = FIN, ods: str | None = ODS, ordem: str = ORDEM,
              page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    guard(request, response)
    where, params = [UNICO], [_lab_people(lab_id)]
    _filters(where, params, ano, financiador, ods, q)
    return {"laboratorio": lab_id, **_list(where, params, ordem, page, per_page)}


@pessoas.get("/laboratorios/{lab_id}/financiamentos", summary="Quem financiou as publicações de um laboratório")
def lab_funding(lab_id: int, request: Request, response: Response) -> dict:
    """Financiadores e auxílios citados nas publicações do responsável e da equipe (sem os coautores)."""
    guard(request, response)
    return {"laboratorio": lab_id, **_funding(_lab_people(lab_id))}


ROUTERS = (publicacoes, pessoas)
