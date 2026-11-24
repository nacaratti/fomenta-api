"""Rotas de auxílios e financiadores (OpenAlex), ligadas aos editais, aos investimentos e ao Elos.

- `oa_auxilio`: auxílios que as próprias agências publicam no OpenAlex (FAPESP, FAPEMIG, CAPES
  cooperação internacional, FINEP/Funttel), com modalidade, vigência, valor e o responsável;
  só os ligados à base (`tools/openalex_auxilios.py`).
- `oa_award`: auxílios citados nos agradecimentos das publicações (etapa 1), só com o processo.
- `oa_funder_br` e `oa_funder_agencia`: financiadores brasileiros e a agência dos editais de cada um.
- `oa_award_finep`: contratos FINEP citados, para casar com os projetos da API de investimentos.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from api.guard import guard
from api.obras import N_W, W_COLS, _enrich, _lab_people, _work
from api.plataforma import EDITAL_COLS, MAX, PAGE, PER_PAGE, Q, _all, _digits, _edital, _one, _page, _search

financiadores = APIRouter(prefix="/v1/financiadores", tags=["Auxílios e financiadores (OpenAlex)"])
auxilios = APIRouter(prefix="/v1/auxilios", tags=["Auxílios e financiadores (OpenAlex)"])
pessoas = APIRouter(prefix="/v1/elos", tags=["Auxílios e financiadores (OpenAlex)"])
projetos = APIRouter(prefix="/v1/investimentos", tags=["Investimentos"])
NOTE = ("Auxílios do OpenAlex (CC0), como publicados pela agência (FAPESP, FAPEMIG, CAPES cooperação internacional, "
        "FINEP/Funttel). O valor vem na moeda da agência e muitas vezes falta: não some com os investimentos.")

A_COLS = ("x.id, x.processo, x.titulo, x.funder_id, f.nome, x.modalidade, x.tipo, x.inicio, x.fim, x.valor, x.moeda, "
          "x.valor_usd, x.fonte, x.pagina, x.publicacoes, x.researcher_id, x.investigador, x.orcid, x.inst_cnpj, "
          "x.afiliacao")
A_FROM = "oa_auxilio x LEFT JOIN oa_funder_br f ON f.id = x.funder_id"


def _aux(r: tuple) -> dict[str, Any]:
    return {"id": r[0], "processo": r[1], "titulo": r[2], "financiador": {"id": r[3], "nome": r[4]},
            "modalidade": r[5], "tipo": r[6], "inicio": r[7], "fim": r[8], "valor": r[9], "moeda": r[10],
            "valor_usd": r[11], "fonte": r[12], "pagina": r[13], "publicacoes_no_openalex": r[14],
            "pesquisador": ({"id": r[15], "nome": r[16]} if r[15] else None),
            "responsavel": r[16], "orcid": r[17],
            # afiliação como a agência informa; o CNPJ vem do casamento do Elos (às vezes o de uma unidade)
            "instituicao": {"nome": r[19], "cnpj": r[18]} if (r[18] or r[19]) else None,
            "openalex": f"https://openalex.org/{r[0]}"}


def _funder_id(v: str) -> str | None:
    """Aceita o id do OpenAlex (F…) ou o slug da agência dos editais (fapesp, cnpq…)."""
    if v[:1] in "Ff" and v[1:].isdigit():
        return v.upper()
    r = _one("SELECT funder_id FROM oa_funder_agencia WHERE agencia = ?", [v.lower()])
    return r[0] if r else None


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


# --------------------------------------------------------------------------- financiadores
@financiadores.get("", summary="Financiadores brasileiros")
def list_funders(request: Request, response: Response, q: str | None = Q,
                 com_editais: bool = Query(False, description="só os que têm agência com editais no Fomenta"),
                 page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    """Os financiadores brasileiros do OpenAlex, com a agência dos editais quando há, e quanto
    apoiaram da base: auxílios publicados pela agência e publicações que citam o financiador."""
    guard(request, response)
    where, params = [], []
    if q:
        _search("lower(strip_accents(f.nome))", q, where, params)
    if com_editais:
        where.append("g.agencia IS NOT NULL")
    sel = "f.id, f.nome, f.ror, g.agencia, a.name, coalesce(xa.n, 0), coalesce(wp.n, 0), coalesce(ed.n, 0)"
    src = ("oa_funder_br f LEFT JOIN oa_funder_agencia g ON g.funder_id = f.id "
           "LEFT JOIN agencia a ON a.slug = g.agencia "
           "LEFT JOIN (SELECT funder_id, count(*) n FROM oa_auxilio GROUP BY 1) xa ON xa.funder_id = f.id "
           "LEFT JOIN (SELECT funder_id, count(*) n FROM oa_work_funder GROUP BY 1) wp ON wp.funder_id = f.id "
           "LEFT JOIN (SELECT agency_id, count(*) n FROM edital GROUP BY 1) ed ON ed.agency_id = a.id")
    return _page(src, where, params, sel, "coalesce(wp.n, 0) + coalesce(xa.n, 0) DESC, f.id", page, per_page,
                 lambda r: {"id": r[0], "nome": r[1], "ror": r[2],
                            "agencia": {"slug": r[3], "nome": r[4]} if r[3] else None,
                            "auxilios": r[5], "publicacoes": r[6], "editais": r[7]})


@financiadores.get("/{financiador}", summary="Um financiador: editais, auxílios e quem ele apoiou")
def get_funder(financiador: str, request: Request, response: Response,
               limite: int = Query(15, ge=1, le=MAX)) -> dict:
    """`financiador` é o id do OpenAlex (F…) ou o slug da agência (`fapesp`, `cnpq`…). Junta os editais
    abertos da agência no Fomenta, os auxílios publicados por ela com gente da base e os pesquisadores
    e laboratórios mais apoiados (por auxílio ou por publicação que cita o financiador)."""
    cliente = guard(request, response)
    fid = _funder_id(financiador)
    f = _one("SELECT f.id, f.nome, f.ror, f.crossref, f.auxilios, f.publicacoes, g.agencia, a.name, a.id "
             "FROM oa_funder_br f LEFT JOIN oa_funder_agencia g ON g.funder_id = f.id "
             "LEFT JOIN agencia a ON a.slug = g.agencia WHERE f.id = ?", [fid]) if fid else None
    if not f:
        raise HTTPException(404, "Financiador brasileiro não encontrado (use o id F… ou o slug da agência).")
    now = datetime.now(UTC).replace(tzinfo=None)
    # Editais só para quem tem a permissão (chave institucional); a resposta com eles não vai para o cache.
    abertos = _all(f"SELECT {EDITAL_COLS} FROM edital e JOIN agencia a ON a.id = e.agency_id "
                   "WHERE e.agency_id = ? AND e.submission_deadline >= ? ORDER BY e.submission_deadline, e.id LIMIT ?",
                   [f[8], now, limite]) if f[8] and cliente.editais else []
    if cliente.editais:
        response.headers["Cache-Control"] = "private, no-store"
    by_year = _all("SELECT ano, count(*), count(valor), sum(valor) FILTER (WHERE moeda = 'BRL') FROM oa_auxilio "
                   "WHERE funder_id = ? AND ano IS NOT NULL GROUP BY 1 ORDER BY 1", [fid])
    modal = _all("SELECT modalidade, count(*) n FROM oa_auxilio WHERE funder_id = ? AND modalidade IS NOT NULL "
                 "GROUP BY 1 ORDER BY n DESC, 1 LIMIT ?", [fid, limite])
    # Pessoas: auxílios em que são responsáveis + publicações que citam o financiador.
    people = _all(
        "WITH a AS (SELECT researcher_id rid, count(*) aux FROM oa_auxilio WHERE funder_id = ? "
        "           AND researcher_id IS NOT NULL GROUP BY 1), "
        "p AS (SELECT wa.researcher_id rid, count(*) pub FROM oa_work_author wa "
        "      JOIN oa_work_funder wf ON wf.work_id = wa.work_id WHERE wf.funder_id = ? GROUP BY 1) "
        "SELECT r.id, r.nome, r.instituicao_sigla, coalesce(a.aux, 0), coalesce(p.pub, 0) "
        "FROM a FULL JOIN p ON p.rid = a.rid JOIN elos_researcher r ON r.id = coalesce(a.rid, p.rid) "
        "ORDER BY coalesce(a.aux, 0) * 3 + coalesce(p.pub, 0) DESC, r.id LIMIT ?", [fid, fid, limite])
    labs = _all(
        "SELECT rl.lab_id, l.name, l.initials, count(DISTINCT x.id) n FROM oa_auxilio x "
        "JOIN elos_researcher_lab rl ON rl.researcher_id = x.researcher_id AND rl.papel <> 'coautor' "
        "LEFT JOIN pnipe_laboratory l ON l.id = rl.lab_id WHERE x.funder_id = ? "
        "GROUP BY ALL ORDER BY n DESC, rl.lab_id LIMIT ?", [fid, limite])
    return {
        "id": f[0], "nome": f[1], "ror": f[2], "crossref": f[3], "auxilios_no_openalex": f[4],
        "publicacoes_no_openalex": f[5], "agencia": {"slug": f[6], "nome": f[7]} if f[6] else None, "nota": NOTE,
        "editais_abertos": [_edital(r) for r in abertos],
        "auxilios_por_ano": [{"ano": y, "auxilios": n, "com_valor": nv, "valor_brl": v} for y, n, nv, v in by_year],
        "modalidades": [{"modalidade": m, "auxilios": n} for m, n in modal],
        "pesquisadores": [{"id": x[0], "nome": x[1], "instituicao_sigla": x[2], "auxilios": x[3], "publicacoes": x[4]}
                          for x in people],
        "laboratorios": [{"id": x[0], "nome": x[1], "sigla": x[2], "auxilios": x[3]} for x in labs],
        "auxilios_url": f"/v1/auxilios?financiador={f[0]}",
        "publicacoes_url": f"/v1/publicacoes?financiador={f[0]}",
        "editais_url": f"/v1/editais?agencia={f[6]}&situacao=abertos" if f[6] else None,
    }


# --------------------------------------------------------------------------- auxílios
def _aux_filters(where: list[str], params: list[Any], financiador: str | None, ano: int | None,
                 vigentes: bool, q: str | None, cnpj: str | None) -> None:
    if financiador:
        fid = _funder_id(financiador)
        where.append("x.funder_id = ?")
        params.append(fid or "-")
    if ano:
        where.append("x.ano = ?")
        params.append(ano)
    if vigentes:
        where.append("x.fim >= ?")
        params.append(_today())
    if cnpj:
        where.append("left(x.inst_cnpj, 8) = left(?, 8)")  # raiz: a instituição toda, com as unidades
        params.append(_digits(cnpj))
    if q:
        _search("lower(strip_accents(concat_ws(' ', x.titulo, x.processo, x.modalidade)))", q, where, params)


FIN = Query(None, max_length=20, description="id do financiador (F…) ou slug da agência (fapesp, fapemig…)")
ANO = Query(None, ge=1950, le=2100, description="ano de início")
VIG = Query(False, description="só os que terminam hoje ou depois")
CNPJ = Query(None, max_length=20, description="CNPJ da instituição do responsável (PNIPE); vale a raiz, com as unidades")
ORDEM = Query("recentes", pattern="^(recentes|valor)$")
ORDER_SQL = {"recentes": "x.inicio DESC NULLS LAST, x.id", "valor": "x.valor_usd DESC NULLS LAST, x.id"}


def _list(where: list[str], params: list[Any], ordem: str, page: int, per_page: int) -> dict[str, Any]:
    return {**_page(A_FROM, where, params, A_COLS, ORDER_SQL[ordem], page, per_page, _aux), "nota": NOTE}


@auxilios.get("", summary="Auxílios publicados pelas agências, ligados à base")
def list_awards(request: Request, response: Response, financiador: str | None = FIN, ano: int | None = ANO,
                vigentes: bool = VIG, cnpj: str | None = CNPJ, q: str | None = Q, ordem: str = ORDEM,
                page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    """Busca no título, no processo e na modalidade. `ordem=valor` usa o valor em dólar que o OpenAlex
    calcula, para comparar moedas."""
    guard(request, response)
    where, params = [], []
    _aux_filters(where, params, financiador, ano, vigentes, q, cnpj)
    return _list(where, params, ordem, page, per_page)


def _finep_projects(award_id: str) -> list[dict[str, Any]]:
    rows = _all("SELECT DISTINCT p.source_key, p.title, p.instrument, p.executor_name, p.signing_date, "
                "p.approved_value, p.released_value FROM oa_award_finep c "
                "JOIN funding_award p ON p.source_id = 'finep' AND left(p.source_key, 10) = c.contrato "
                "WHERE c.award_id = ? ORDER BY p.source_key", [award_id])
    return [{"contrato": r[0], "titulo": r[1], "instrumento": r[2], "executor": r[3],
             "assinatura": r[4].isoformat() if r[4] else None, "aprovado": r[5], "liberado": r[6]} for r in rows]


@auxilios.get("/{award_id}", summary="Um auxílio: dados, publicações da base e projeto FINEP")
def get_award(award_id: str, request: Request, response: Response, limite: int = Query(25, ge=1, le=MAX)) -> dict:
    """Vale para os auxílios publicados pela agência e para os citados nas publicações (só o processo).
    Para contratos FINEP, traz o projeto correspondente da API de investimentos."""
    guard(request, response)
    gid = award_id.upper()
    r = _one(f"SELECT {A_COLS} FROM {A_FROM} WHERE x.id = ?", [gid])
    if r:
        out = _aux(r)
    else:
        c = _one("SELECT a.id, a.processo, a.nome, a.funder_id, a.financiador FROM oa_award a WHERE a.id = ?", [gid])
        if not c:
            raise HTTPException(404, "Auxílio não encontrado na base.")
        out = {"id": c[0], "processo": c[1], "titulo": c[2], "financiador": {"id": c[3], "nome": c[4]},
               "fonte": "agradecimentos das publicações", "openalex": f"https://openalex.org/{c[0]}"}
    works = _all(f"SELECT {W_COLS} FROM oa_work w JOIN oa_work_award x ON x.work_id = w.id WHERE x.award_id = ? "
                 "ORDER BY w.ano DESC, w.citacoes DESC, w.id LIMIT ?", [gid, limite])
    total = _one("SELECT count(*) FROM oa_work_award WHERE award_id = ?", [gid])[0]
    return {**out, "nota": NOTE, "projetos_finep": _finep_projects(gid), "publicacoes_da_base": total,
            "publicacoes": _enrich([_work(w[:N_W]) for w in works])}


# --------------------------------------------------------------------------- pessoas e laboratórios
@pessoas.get("/pesquisadores/{researcher_id}/auxilios", summary="Auxílios em que o pesquisador é o responsável")
def researcher_awards(researcher_id: str, request: Request, response: Response, financiador: str | None = FIN,
                      vigentes: bool = VIG, ordem: str = ORDEM, page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    guard(request, response)
    where, params = ["x.researcher_id = ?"], [researcher_id]
    _aux_filters(where, params, financiador, None, vigentes, None, None)
    return {"pesquisador": researcher_id, **_list(where, params, ordem, page, per_page)}


@pessoas.get("/laboratorios/{lab_id}/auxilios", summary="Auxílios do responsável e da equipe de um laboratório")
def lab_awards(lab_id: int, request: Request, response: Response, financiador: str | None = FIN,
               vigentes: bool = VIG, ordem: str = ORDEM, page: int = PAGE, per_page: int = PER_PAGE) -> dict:
    guard(request, response)
    where, params = ["list_contains(?, x.researcher_id)"], [_lab_people(lab_id)]
    _aux_filters(where, params, financiador, None, vigentes, None, None)
    return {"laboratorio": lab_id, **_list(where, params, ordem, page, per_page)}


# --------------------------------------------------------------------------- projetos FINEP
@projetos.get("/projetos/finep/{contrato}/publicacoes", summary="Publicações da base que citam um contrato FINEP")
def finep_works(contrato: str, request: Request, response: Response, page: int = PAGE,
                per_page: int = PER_PAGE) -> dict:
    """`contrato` como em `/v1/investimentos/projetos` (`key`, ex.: 01.20.0026.00). O casamento usa os
    10 primeiros caracteres (o último par muda com aditivos) e o nº citado nos agradecimentos."""
    guard(request, response)
    key = contrato.strip()[:10]
    if len(key) != 10:
        raise HTTPException(422, "Contrato no formato 01.20.0026.00.")
    where = ["w.id IN (SELECT x.work_id FROM oa_work_award x JOIN oa_award_finep c ON c.award_id = x.award_id "
             "WHERE c.contrato = ?)"]
    out = _page("oa_work w", where, [key], W_COLS, "w.ano DESC, w.citacoes DESC, w.id", page, per_page, _work)
    out["items"] = _enrich(out["items"])
    return {"contrato": key, **out}


ROUTERS = (financiadores, auxilios, pessoas, projetos)
