"""Rotas /v1/elos: pesquisadores, laboratórios e coautorias.

O Elos da API vem de `tools/elos_ampliado.py`, que refaz a carga do FomentaBR a partir do
cache do OpenAlex sem os cortes de espaço do Supabase e liga ao laboratório quem publicou
com o responsável dele (papel `coautor`).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from api.guard import guard
from api.plataforma import MAX, PAGE, PER_PAGE, Q, _all, _digits, _one, _page, _search

elos = APIRouter(prefix="/v1/elos", tags=["Elos (pesquisadores)"])
ELOS_NOTE = ("Pesquisadores e coautorias do OpenAlex (CC0), cruzados com o PNIPE pelo Fomenta. Quem pediu remoção "
             "não aparece. Para pedir remoção, use a página do Elos no fomentabr.org.")
PAPEIS = {
    "responsavel": "responsável cadastrado no PNIPE, casado com o perfil do OpenAlex",
    "equipe": "equipe provável: da mesma instituição, 3+ publicações com o responsável desde 2019 e ativo desde "
              "2023, ou informada pelo laboratório",
    "coautor": "publicou com o responsável desde 2019 (publicações com até 30 autores); não quer dizer que "
               "trabalhe no laboratório. `colaborador_estavel` (inferência): publicou com o responsável em 2 anos "
               "distintos ou mais",
}
ESTAVEL = Query(None, description="com papel coautor: só colaboradores estáveis (2+ anos com publicação junto ao "
                                  "responsável), ou só os demais com false")
PAPEL = Query(None, pattern="^(responsavel|equipe|coautor)$",
              description="; ".join(f"`{k}`: {v}" for k, v in PAPEIS.items()))
R_COLS = ("id, nome, orcid, inst_cnpj, publicacoes, citacoes, h_index, desde_2019, ultimo_ano, grandes_areas, "
          "origem, instituicao_nome, instituicao_sigla, instituicao_pais")
N_R = len(R_COLS.split(","))
PAPEL_ORDEM = "CASE rl.papel WHEN 'responsavel' THEN 0 WHEN 'equipe' THEN 1 ELSE 2 END"


def _res(r: tuple) -> dict[str, Any]:
    return {"id": r[0], "nome": r[1], "orcid": r[2], "instituicao_cnpj": r[3], "publicacoes": r[4],
            "citacoes": r[5], "h_index": r[6], "publicacoes_desde_2019": r[7], "ultimo_ano": r[8],
            "grandes_areas": r[9], "origem": r[10], "instituicao": r[11], "instituicao_sigla": r[12], "pais": r[13]}


def _rcols(alias: str) -> str:
    return ", ".join(f"{alias}.{c.strip()}" for c in R_COLS.split(","))


def _obras(ids: list[str] | None) -> list[str]:
    return [f"https://openalex.org/{w}" for w in ids or []]


@elos.get("/pesquisadores", summary="Pesquisadores ligados aos laboratórios")
def researchers(
    request: Request, response: Response,
    cnpj: str | None = Query(None, max_length=20, description="CNPJ da instituição atual"),
    laboratorio: int | None = Query(None, description="id do laboratório"),
    papel: str | None = PAPEL,
    estavel: bool | None = ESTAVEL,
    origem: str | None = Query(None, pattern="^(responsavel|equipe|coautor|outro)$",
                               description="como a pessoa entrou na base"),
    tema: str | None = Query(None, max_length=40, description="id do tema (ver /v1/elos/temas)"),
    q: str | None = Q, page: int = PAGE, per_page: int = PER_PAGE,
) -> dict:
    """Responsáveis, equipe provável e coautores dos responsáveis. Com `laboratorio` (e `papel`), quem está
    ligado àquele laboratório; sem ele, `papel` filtra quem tem esse papel em algum laboratório."""
    guard(request, response)
    where, params = [], []
    if cnpj:
        where.append("inst_cnpj = ?")
        params.append(_digits(cnpj))
    if laboratorio is not None or papel or estavel is not None:
        cond, sub = [], []
        if laboratorio is not None:
            cond.append("lab_id = ?")
            sub.append(laboratorio)
        if papel:
            cond.append("papel = ?")
            sub.append(papel)
        if estavel is not None:
            cond.append("colaborador_estavel = ?")
            sub.append(estavel)
        where.append(f"id IN (SELECT researcher_id FROM elos_researcher_lab WHERE {' AND '.join(cond)})")
        params += sub
    if origem:
        where.append("origem = ?")
        params.append(origem)
    if tema:
        where.append("id IN (SELECT researcher_id FROM elos_researcher_topic WHERE topic_id = ?)")
        params.append(tema)
    if q:
        _search("busca", q, where, params)
    out = _page("elos_researcher", where, params, R_COLS,
                # a base primeiro: os coautores de fora têm perfil completo e passariam à frente
                "origem = 'coautor', publicacoes DESC NULLS LAST, ultimo_ano DESC NULLS LAST, id", page, per_page, _res)
    return {**out, "papeis": PAPEIS, "nota": ELOS_NOTE}


def _coauthors(rid: str, limit: int, offset: int) -> dict[str, Any]:
    base = ("FROM (SELECT CASE WHEN a = ? THEN b ELSE a END AS other, peso, ultimo_ano, obras FROM elos_edge "
            "WHERE tipo = 'coautoria' AND (a = ? OR b = ?)) e JOIN elos_researcher o ON o.id = e.other")
    total = _one(f"SELECT count(*) {base}", [rid] * 3)[0]
    rows = _all(f"SELECT {_rcols('o')}, e.peso, e.ultimo_ano, e.obras {base} "
                "ORDER BY e.peso DESC, e.ultimo_ano DESC NULLS LAST, o.id LIMIT ? OFFSET ?",
                [rid] * 3 + [limit, offset])
    return {"total": total,
            "items": [{**_res(x[:N_R]), "publicacoes_em_comum": int(x[N_R]), "ultimo_ano_em_comum": x[N_R + 1],
                       "publicacoes": _obras(x[N_R + 2])} for x in rows]}


@elos.get("/pesquisadores/{researcher_id}", summary="Perfil de um pesquisador")
def researcher(researcher_id: str, request: Request, response: Response) -> dict:
    """Laboratórios (com o papel e, para coautor, as publicações em comum com o responsável), temas e os
    15 coautores mais frequentes; a lista completa está em `/pesquisadores/{id}/coautores`."""
    guard(request, response)
    r = _one(f"SELECT {R_COLS}, openalex_ids FROM elos_researcher WHERE id = ?", [researcher_id])
    if not r:
        raise HTTPException(404, "Pesquisador não encontrado.")
    labs = _all("SELECT rl.lab_id, l.name, l.initials, l.institution_cnpj, rl.papel, rl.responsavel_id, p.nome, "
                "rl.publicacoes_em_comum, rl.ultimo_ano, rl.obras, rl.anos_em_comum, rl.colaborador_estavel "
                "FROM elos_researcher_lab rl "
                "LEFT JOIN pnipe_laboratory l ON l.id = rl.lab_id "
                "LEFT JOIN elos_researcher p ON p.id = rl.responsavel_id "
                f"WHERE rl.researcher_id = ? ORDER BY {PAPEL_ORDEM}, rl.publicacoes_em_comum DESC NULLS LAST, rl.lab_id",
                [researcher_id])
    topics = _all("SELECT t.id, t.nome, t.area, rt.n FROM elos_researcher_topic rt "
                  "JOIN elos_topic t ON t.id = rt.topic_id WHERE rt.researcher_id = ? ORDER BY rt.n DESC LIMIT 15",
                  [researcher_id])
    co = _coauthors(researcher_id, 15, 0)
    return {**_res(r), "openalex_ids": r[N_R], "nota": ELOS_NOTE, "papeis": PAPEIS,
            "laboratorios": [{"id": x[0], "nome": x[1], "sigla": x[2], "instituicao_cnpj": x[3], "papel": x[4],
                              "responsavel": {"id": x[5], "nome": x[6]} if x[5] else None,
                              "publicacoes_em_comum": x[7], "ultimo_ano": x[8], "publicacoes": _obras(x[9]),
                              "anos_em_comum": x[10], "colaborador_estavel": x[11]}
                             for x in labs],
            "temas": [{"id": x[0], "nome": x[1], "area": x[2], "publicacoes": x[3]} for x in topics],
            "coautores": co["items"], "total_coautores": co["total"],
            "coautores_url": f"/v1/elos/pesquisadores/{researcher_id}/coautores"}


@elos.get("/pesquisadores/{researcher_id}/coautores", summary="Todos os coautores de um pesquisador")
def researcher_coauthors(researcher_id: str, request: Request, response: Response,
                         page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    """Coautores na base, dos mais frequentes para os menos, com até 5 publicações em comum (as mais
    recentes) como links do OpenAlex. Para um responsável, são todos os coautores desde 2019."""
    guard(request, response)
    if not _one("SELECT 1 FROM elos_researcher WHERE id = ?", [researcher_id]):
        raise HTTPException(404, "Pesquisador não encontrado.")
    return {"pesquisador": researcher_id, "page": page, "per_page": per_page,
            **_coauthors(researcher_id, per_page, (page - 1) * per_page)}


def _affinity_on_demand(rid: str, limit: int = 20) -> list[tuple]:
    """Cosseno entre os vetores de temas (peso = publicações no tema), só com quem compartilha algum tema e
    nunca publicou com a pessoa. Nada é guardado: calcular para todos ocuparia ~4 milhões de pares."""
    return _all(
        "WITH me AS (SELECT topic_id, n FROM elos_researcher_topic WHERE researcher_id = ?), "
        "co AS (SELECT CASE WHEN a = ? THEN b ELSE a END AS other FROM elos_edge "
        "       WHERE tipo = 'coautoria' AND (a = ? OR b = ?)), "
        "cand AS (SELECT rt.researcher_id AS other, sum(rt.n * me.n) AS dot, count(*) AS comuns "
        "         FROM elos_researcher_topic rt JOIN me USING (topic_id) "
        "         WHERE rt.researcher_id <> ? AND rt.researcher_id NOT IN (SELECT other FROM co) GROUP BY 1), "
        "norm AS (SELECT researcher_id AS other, sqrt(sum(n * n)) AS nn FROM elos_researcher_topic "
        "         WHERE researcher_id IN (SELECT other FROM cand) GROUP BY 1) "
        f"SELECT {_rcols('o')}, round(c.dot / (n.nn * (SELECT sqrt(sum(n * n)) FROM me)), 4) AS peso "
        "FROM cand c JOIN norm n USING (other) JOIN elos_researcher o ON o.id = c.other "
        "ORDER BY peso DESC, c.comuns DESC, o.ultimo_ano DESC NULLS LAST, o.publicacoes DESC NULLS LAST, o.id "
        "LIMIT ?", [rid] * 5 + [limit])


@elos.get("/pesquisadores/{researcher_id}/afinidades",
          summary="Quem pesquisa temas parecidos e ainda não publicou junto")
def researcher_affinity(researcher_id: str, request: Request, response: Response) -> dict:
    """Para os responsáveis de laboratório, as afinidades pré-calculadas no build. Para os demais, o mesmo
    cosseno calculado na hora (`calculo: sob_demanda`): com poucos temas, muita gente empata, e a lista é
    uma estimativa mais grosseira."""
    guard(request, response)
    rows = _all(f"SELECT {_rcols('o')}, e.peso FROM (SELECT CASE WHEN a = ? THEN b ELSE a END AS other, peso "
                "FROM elos_edge WHERE tipo = 'afinidade' AND (a = ? OR b = ?)) e "
                "JOIN elos_researcher o ON o.id = e.other ORDER BY e.peso DESC LIMIT 20", [researcher_id] * 3)
    calculo = "pre_calculada"
    if not rows:
        if not _one("SELECT 1 FROM elos_researcher WHERE id = ?", [researcher_id]):
            raise HTTPException(404, "Pesquisador não encontrado.")
        rows = _affinity_on_demand(researcher_id)
        calculo = "sob_demanda"
    mine = [x[0] for x in _all("SELECT topic_id FROM elos_researcher_topic WHERE researcher_id = ? ORDER BY n DESC",
                               [researcher_id])]
    names = dict(_all("SELECT id, nome FROM elos_topic WHERE list_contains(?, id)", [mine])) if mine else {}
    theirs: dict[str, set[str]] = {}
    if rows:
        for oid, tid in _all("SELECT researcher_id, topic_id FROM elos_researcher_topic "
                             "WHERE list_contains(?, researcher_id)", [[x[0] for x in rows]]):
            theirs.setdefault(oid, set()).add(tid)
    nota = ("Afinidade: cosseno entre os temas das publicações recentes, entre responsáveis que nunca publicaram juntos."
            if calculo == "pre_calculada" else
            "Afinidade estimada na hora: cosseno entre os temas das publicações recentes, com quem nunca publicou junto. "
            "Com poucos temas, muitas pessoas empatam; no empate, vêm primeiro mais temas em comum e atividade recente.")
    return {"pesquisador": researcher_id, "calculo": calculo, "nota": nota,
            "items": [{**_res(x[:N_R]), "afinidade": x[N_R],
                       "temas_em_comum": [names.get(t, t) for t in mine if t in theirs.get(x[0], set())][:3]}
                      for x in rows]}


@elos.get("/laboratorios/{lab_id}", summary="Pessoas ligadas a um laboratório")
def lab_team(lab_id: int, request: Request, response: Response,
             coautores: int = Query(20, ge=0, le=MAX, description="quantos coautores trazer (os mais frequentes)"),
             estavel: bool | None = ESTAVEL) -> dict:
    """Responsável e equipe completos, mais os coautores do responsável que mais publicam com ele. A lista
    inteira, paginada, está em `/v1/elos/pesquisadores?laboratorio={id}&papel=coautor`. Os colaboradores
    estáveis (2+ anos com publicação junto ao responsável) vêm primeiro."""
    guard(request, response)
    cols = (f"{_rcols('r')}, rl.papel, rl.publicacoes_em_comum, rl.ultimo_ano, rl.obras, rl.responsavel_id, "
            "rl.anos_em_comum, rl.colaborador_estavel")

    def row(x: tuple) -> dict[str, Any]:
        return {**_res(x[:N_R]), "papel": x[N_R], "publicacoes_com_responsavel": x[N_R + 1],
                "ultimo_ano_com_responsavel": x[N_R + 2], "publicacoes": _obras(x[N_R + 3]),
                "responsavel_id": x[N_R + 4], "anos_em_comum": x[N_R + 5], "colaborador_estavel": x[N_R + 6]}

    core = _all(f"SELECT {cols} FROM elos_researcher_lab rl JOIN elos_researcher r ON r.id = rl.researcher_id "
                f"WHERE rl.lab_id = ? AND rl.papel <> 'coautor' ORDER BY {PAPEL_ORDEM}, r.publicacoes DESC NULLS LAST",
                [lab_id])
    total_co, total_est = _one("SELECT count(*), count(*) FILTER (WHERE colaborador_estavel) FROM elos_researcher_lab "
                               "WHERE lab_id = ? AND papel = 'coautor'", [lab_id])
    only = "" if estavel is None else " AND rl.colaborador_estavel = ?"
    co = _all(f"SELECT {cols} FROM elos_researcher_lab rl JOIN elos_researcher r ON r.id = rl.researcher_id "
              f"WHERE rl.lab_id = ? AND rl.papel = 'coautor'{only} "
              "ORDER BY rl.colaborador_estavel DESC NULLS LAST, rl.publicacoes_em_comum DESC, rl.ultimo_ano DESC NULLS LAST, "
              "r.id LIMIT ?", [lab_id, *([] if estavel is None else [estavel]), coautores])
    return {"laboratorio": lab_id, "nota": ELOS_NOTE, "papeis": PAPEIS,
            "pesquisadores": [row(x) for x in core],
            "coautores": [row(x) for x in co], "total_coautores": total_co, "colaboradores_estaveis": total_est,
            "coautores_url": f"/v1/elos/pesquisadores?laboratorio={lab_id}&papel=coautor"}


@elos.get("/laboratorios/{lab_id}/coautorias", summary="Laboratórios que publicam com este")
def lab_links(lab_id: int, request: Request, response: Response, limite: int = Query(30, ge=1, le=MAX)) -> dict:
    """Pares de laboratórios cujos responsáveis ou equipes publicaram juntos desde 2019 (publicações distintas)."""
    guard(request, response)
    rows = _all("SELECT CASE WHEN k.lab_a = ? THEN k.lab_b ELSE k.lab_a END AS other, k.coautorias, k.pessoas, "
                "l.name, l.initials, l.institution_cnpj FROM elos_lab_link k "
                "LEFT JOIN pnipe_laboratory l ON l.id = CASE WHEN k.lab_a = ? THEN k.lab_b ELSE k.lab_a END "
                "WHERE k.lab_a = ? OR k.lab_b = ? ORDER BY k.coautorias DESC LIMIT ?", [lab_id] * 4 + [limite])
    ids = sorted({p for x in rows for p in x[2] or []})
    names = dict(_all("SELECT id, nome FROM elos_researcher WHERE list_contains(?, id)", [ids])) if ids else {}
    return {"laboratorio": lab_id,
            "items": [{"id": x[0], "coautorias": x[1], "nome": x[3], "sigla": x[4], "instituicao_cnpj": x[5],
                       "pessoas": [{"id": p, "nome": names.get(p)} for p in x[2] or []]} for x in rows]}


@elos.get("/temas", summary="Temas de pesquisa (taxonomia do OpenAlex)")
def topics(request: Request, response: Response, q: str | None = Q, page: int = PAGE,
           per_page: int = PER_PAGE) -> dict:
    guard(request, response)
    where, params = [], []
    if q:
        _search("lower(strip_accents(concat_ws(' ', nome, subarea, area)))", q, where, params)
    return _page("elos_topic t", where, params,
                 "id, nome, subarea, area, grande_area, "
                 "(SELECT count(*) FROM elos_researcher_topic rt WHERE rt.topic_id = t.id)",
                 "nome, id", page, per_page,
                 lambda r: {"id": r[0], "nome": r[1], "subarea": r[2], "area": r[3], "grande_area": r[4],
                            "pesquisadores": r[5]})
