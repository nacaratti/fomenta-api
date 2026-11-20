"""Pesquisadores e laboratórios com perfil para cada edital (inferência), e o caminho inverso.

Calculado offline por `tools/perfis_editais.py`: semelhança entre o tema do edital e o perfil
de cada pessoa (temas, palavras-chave e títulos das publicações) e de cada laboratório, com
correção de "hubs", só gente e laboratórios do estado nas FAPs estaduais e um bônus pequeno
para quem a agência já financiou. Editais abertos a todas as áreas ou só para empresas não têm lista.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from api.guard import guard
from api.plataforma import EDITAL_COLS, MAX, _all, _edital, _one

editais = APIRouter(prefix="/v1/editais", tags=["Perfil para editais (inferência)"])
pessoas = APIRouter(prefix="/v1/elos", tags=["Perfil para editais (inferência)"])
NOTE = ("Inferência por semelhança de texto entre o tema do edital e as publicações (OpenAlex) de cada pessoa ou "
        "laboratório. Não verifica elegibilidade: confira as regras do edital. Nas FAPs estaduais, só aparecem "
        "pessoas e laboratórios do estado da agência.")
SITUACAO = {
    "tematico": None,
    "todas_as_areas": "Edital aberto a todas as áreas: não há perfil temático a comparar.",
    "so_empresas": "Edital só para empresas: não há lista de pesquisadores.",
}
LIMITE = Query(20, ge=1, le=30)
N_EDITAL = 16  # colunas de EDITAL_COLS lidas por _edital


def _call(call_id: int) -> tuple[dict[str, Any], str | None]:
    r = _one(f"SELECT {EDITAL_COLS} FROM edital e JOIN agencia a ON a.id = e.agency_id WHERE e.id = ?", [call_id])
    if not r:
        raise HTTPException(404, "Edital não encontrado.")
    s = _one("SELECT situacao FROM edital_perfil WHERE call_id = ?", [call_id])
    return _edital(r), (s[0] if s else None)


def _head(call: dict[str, Any], situacao: str | None) -> dict[str, Any]:
    motivo = SITUACAO.get(situacao or "", "Perfil ainda não calculado para este edital.")
    return {"edital": {k: call[k] for k in ("id", "titulo", "prazo", "situacao", "agencia")},
            "perfil": situacao or "sem_calculo", "motivo_sem_lista": motivo, "nota": NOTE}


@editais.get("/{call_id}/pesquisadores", summary="Pesquisadores com perfil para o edital (inferência)")
def call_researchers(call_id: int, request: Request, response: Response, limite: int = LIMITE) -> dict:
    """Em ordem de nota. `semelhanca` é o cosseno bruto (0 a 1); `auxilios_da_agencia` e
    `publicacoes_da_agencia` contam o que a agência do edital já financiou da pessoa."""
    guard(request, response)
    call, situacao = _call(call_id)
    rows = _all("SELECT p.researcher_id, r.nome, r.instituicao_sigla, r.instituicao_nome, r.orcid, p.posicao, p.nota, "
                "p.semelhanca, p.auxilios_da_agencia, p.publicacoes_da_agencia, "
                "(SELECT list({'id': rl.lab_id, 'nome': l.name, 'papel': rl.papel} ORDER BY rl.papel DESC, rl.lab_id) "
                " FROM elos_researcher_lab rl LEFT JOIN pnipe_laboratory l ON l.id = rl.lab_id "
                " WHERE rl.researcher_id = p.researcher_id AND rl.papel <> 'coautor') "
                "FROM edital_pesquisador p JOIN elos_researcher r ON r.id = p.researcher_id "
                "WHERE p.call_id = ? ORDER BY p.posicao LIMIT ?", [call_id, limite])
    return {**_head(call, situacao), "items": [
        {"id": r[0], "nome": r[1], "instituicao_sigla": r[2], "instituicao": r[3], "orcid": r[4], "posicao": r[5],
         "nota": round(r[6], 4), "semelhanca": round(r[7], 4), "auxilios_da_agencia": r[8],
         "publicacoes_da_agencia": r[9], "laboratorios": r[10] or []} for r in rows]}


@editais.get("/{call_id}/laboratorios", summary="Laboratórios com perfil para o edital (inferência)")
def call_labs(call_id: int, request: Request, response: Response, limite: int = LIMITE) -> dict:
    """A nota junta a descrição do laboratório no PNIPE e o perfil do responsável e da equipe."""
    guard(request, response)
    call, situacao = _call(call_id)
    rows = _all("SELECT x.lab_id, l.name, l.initials, i.initials, i.city, l.resolved_state, x.posicao, x.nota, "
                "x.semelhanca_laboratorio, x.semelhanca_equipe FROM edital_laboratorio x "
                "JOIN pnipe_laboratory l ON l.id = x.lab_id LEFT JOIN pnipe_institution i ON i.cnpj = l.institution_cnpj "
                "WHERE x.call_id = ? ORDER BY x.posicao LIMIT ?", [call_id, limite])
    return {**_head(call, situacao), "items": [
        {"id": r[0], "nome": r[1], "sigla": r[2], "instituicao_sigla": r[3], "cidade": r[4], "uf": r[5],
         "posicao": r[6], "nota": round(r[7], 4), "semelhanca_laboratorio": round(r[8], 4),
         "semelhanca_equipe": round(r[9], 4) if r[9] is not None else None} for r in rows]}


def _reverse(table: str, key: str, value: Any, abertos: bool, limite: int) -> list[dict[str, Any]]:
    where, params = [f"x.{key} = ?"], [value]
    if abertos:
        where.append("e.submission_deadline >= ?")
        params.append(datetime.now(UTC).replace(tzinfo=None))
    rows = _all(f"SELECT {EDITAL_COLS}, x.posicao, x.nota FROM {table} x JOIN edital e ON e.id = x.call_id "
                f"JOIN agencia a ON a.id = e.agency_id WHERE {' AND '.join(where)} "
                "ORDER BY x.nota DESC, e.id LIMIT ?", [*params, limite])
    n = N_EDITAL
    return [{**_edital(r[:n]), "posicao_no_edital": r[n], "nota": round(r[n + 1], 4)} for r in rows]


ABERTOS = Query(True, description="só editais com prazo a partir de hoje")


@pessoas.get("/pesquisadores/{researcher_id}/editais", summary="Editais em que a pessoa aparece com perfil (inferência)")
def researcher_calls(researcher_id: str, request: Request, response: Response, abertos: bool = ABERTOS,
                     limite: int = Query(20, ge=1, le=MAX)) -> dict:
    """Só editais temáticos em que a pessoa ficou entre as 30 primeiras."""
    guard(request, response)
    return {"pesquisador": researcher_id, "nota": NOTE,
            "items": _reverse("edital_pesquisador", "researcher_id", researcher_id, abertos, limite)}


@pessoas.get("/laboratorios/{lab_id}/editais", summary="Editais em que o laboratório aparece com perfil (inferência)")
def lab_calls(lab_id: int, request: Request, response: Response, abertos: bool = ABERTOS,
              limite: int = Query(20, ge=1, le=MAX)) -> dict:
    """Só editais temáticos em que o laboratório ficou entre os 20 primeiros."""
    guard(request, response)
    return {"laboratorio": lab_id, "nota": NOTE, "items": _reverse("edital_laboratorio", "lab_id", lab_id, abertos, limite)}


ROUTERS = (editais, pessoas)
