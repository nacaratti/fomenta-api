"""Servidor MCP (Model Context Protocol) da Fomenta API, em `POST /mcp`.

Deixa assistentes de IA (Claude, ChatGPT e outros clientes MCP) consultarem os dados como
ferramentas. Transporte "Streamable HTTP" sem estado: cada POST traz uma mensagem JSON-RPC e
recebe a resposta em JSON, o que cabe numa função da Vercel. Sem sessão, sem SSE.

As ferramentas chamam as mesmas funções das rotas, então valem os mesmos planos e limites: a
chave vai em `Authorization: Bearer fmt_...` (ou `X-API-Key`) e cada chamada de ferramenta conta
como uma consulta. Sem chave, vale o plano Aberto.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from api import elos, indicadores, obras, perfis, plataforma, queries
from api.guard import check_scope, identify

router = APIRouter(tags=["MCP (assistentes de IA)"])

SERVER = {"name": "fomenta-br", "title": "Fomenta BR", "version": "1.0.0"}
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 60_000
INSTRUCTIONS = (
    "Dados públicos sobre fomento e infraestrutura de pesquisa no Brasil: "
    "laboratórios e equipamentos do PNIPE, pesquisadores e coautorias (OpenAlex), publicações desde 2019 e "
    "investimentos de FINEP, CAPES e CNPq. Comece pelas buscas (buscar_*) e use o id devolvido nas ferramentas "
    "de detalhe. Editais de fomento só estão no site fomentabr (não aqui). Valores em reais nominais; não some financiadores diferentes. Resultados marcados como "
    "inferência (perfil para edital, laboratórios parecidos) são estimativas, não registros oficiais. "
    "Cite a fonte como Fomenta BR e o link do site quando responder."
)


# --------------------------------------------------------------------------- ferramentas
def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


Q = {"type": "string", "minLength": 3, "maxLength": 80, "description": "texto de busca; cada palavra precisa aparecer, sem acento nem caixa"}
UF = {"type": "string", "minLength": 2, "maxLength": 2, "description": "sigla da UF, como SP"}
PAGE = {"type": "integer", "minimum": 1, "default": 1}
PER_PAGE = {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}

TOOLS: list[dict[str, Any]] = [
    {"name": "buscar_editais", "title": "Buscar editais de fomento",
     "description": "Editais (chamadas, bolsas, seleções) coletados das agências brasileiras. Por padrão só os abertos, do prazo mais próximo ao mais distante.",
     "inputSchema": _schema({"q": Q, "situacao": {"type": "string", "enum": ["abertos", "encerrados", "sem_prazo", "todos"], "default": "abertos"},
                             "agencia": {"type": "string", "description": "slug da agência, como fapesp, cnpq, finep"},
                             "tipo": {"type": "string", "enum": list(plataforma.CALL_KINDS)},
                             "area": {"type": "string", "description": "código da área do conhecimento (CNPq), como 3.00.00.00-9"},
                             "page": PAGE, "per_page": PER_PAGE})},
    {"name": "edital", "title": "Detalhe de um edital",
     "description": "Um edital pelo id: resumo, prazo, valores, quem pode participar, o que financia, documentos e critérios. Com incluir_perfis, traz também pesquisadores e laboratórios com perfil para ele (inferência).",
     "inputSchema": _schema({"id": {"type": "integer"}, "incluir_perfis": {"type": "boolean", "default": False}}, ["id"])},
    {"name": "buscar_laboratorios", "title": "Buscar laboratórios",
     "description": "Laboratórios do PNIPE (Plataforma Nacional de Infraestrutura de Pesquisa) por nome, técnica, área, UF ou instituição.",
     "inputSchema": _schema({"q": Q, "uf": UF, "area": {"type": "string", "description": "área de atuação, sem acento e minúscula"},
                             "cnpj": {"type": "string", "description": "CNPJ da instituição"}, "page": PAGE, "per_page": PER_PAGE})},
    {"name": "laboratorio", "title": "Detalhe de um laboratório",
     "description": "Um laboratório pelo id: descrição, áreas, técnicas, equipamentos, responsável e equipe, e indicadores de impacto das publicações.",
     "inputSchema": _schema({"id": {"type": "integer"}}, ["id"])},
    {"name": "laboratorios_parecidos", "title": "Laboratórios com a mesma base de conhecimento",
     "description": "Inferência: até 15 laboratórios que citam as mesmas referências. Com so_novos, só os que nunca publicaram juntos (candidatos a parceria).",
     "inputSchema": _schema({"id": {"type": "integer"}, "so_novos": {"type": "boolean", "default": False}}, ["id"])},
    {"name": "buscar_equipamentos", "title": "Buscar equipamentos",
     "description": "Equipamentos de pesquisa por nome, marca ou modelo. Multiusuário = aberto a usuários de fora do laboratório.",
     "inputSchema": _schema({"q": Q, "uf": UF, "multiusuario": {"type": "boolean"}, "page": PAGE, "per_page": PER_PAGE})},
    {"name": "buscar_pesquisadores", "title": "Buscar pesquisadores",
     "description": "Responsáveis por laboratórios, equipes e coautores, com publicações, citações e índice h.",
     "inputSchema": _schema({"q": Q, "tema": {"type": "string", "description": "id do tema do OpenAlex, como T10205"},
                             "cnpj": {"type": "string", "description": "CNPJ da instituição atual"},
                             "papel": {"type": "string", "enum": ["responsavel", "equipe", "coautor"]},
                             "page": PAGE, "per_page": PER_PAGE})},
    {"name": "pesquisador", "title": "Perfil de um pesquisador",
     "description": "Uma pessoa pelo id do OpenAlex (A...): laboratórios, temas, principais coautores e editais abertos em que tem perfil (inferência).",
     "inputSchema": _schema({"id": {"type": "string"}}, ["id"])},
    {"name": "buscar_publicacoes", "title": "Buscar publicações",
     "description": "Publicações desde 2019 dos pesquisadores da base. A busca é no título (em geral em inglês).",
     "inputSchema": _schema({"q": Q, "ano": {"type": "integer", "minimum": 2019},
                             "ordem": {"type": "string", "enum": ["recentes", "citadas", "impacto"], "default": "recentes"},
                             "page": PAGE, "per_page": PER_PAGE})},
    {"name": "buscar_congressos", "title": "Buscar congressos",
     "description": "Congressos e eventos científicos, com local, datas e prazo de submissão.",
     "inputSchema": _schema({"q": Q, "situacao": {"type": "string", "enum": ["proximos", "passados", "todos"], "default": "proximos"},
                             "page": PAGE, "per_page": PER_PAGE})},
    {"name": "investimentos", "title": "Investimentos em PD&I",
     "description": "O que FINEP, CAPES e CNPq investiram: sem financiador, os totais de cada um; com financiador, a série por ano, por UF e por instrumento. Valores nominais; não some financiadores diferentes.",
     "inputSchema": _schema({"financiador": {"type": "string", "enum": ["finep", "capes", "cnpq"]}})},
]
for _t in TOOLS:
    _t["annotations"] = {"readOnlyHint": True, "openWorldHint": False}


def _paged(args: dict[str, Any]) -> tuple[int, int]:
    return int(args.get("page") or 1), min(int(args.get("per_page") or 10), 100)


def _q(args: dict[str, Any]) -> str | None:
    q = (args.get("q") or "").strip() or None
    if q is not None and len(q) < 3:
        raise ValueError("A busca precisa de pelo menos 3 letras.")
    return q


def _tool_impl(name: str) -> Callable[[Request, Response, dict[str, Any]], Any]:
    def buscar_editais(rq, rs, a):
        page, pp = _paged(a)
        return plataforma.list_calls(rq, rs, agencia=a.get("agencia"), situacao=a.get("situacao") or "abertos",
                                     tipo=a.get("tipo"), modalidade=None, area=a.get("area"), q=_q(a), page=page, per_page=pp)

    def edital(rq, rs, a):
        out = {"edital": plataforma.call_detail(int(a["id"]), rq, rs)}
        if a.get("incluir_perfis"):
            out["pesquisadores_com_perfil"] = perfis.call_researchers(int(a["id"]), rq, rs, limite=10)
            out["laboratorios_com_perfil"] = perfis.call_labs(int(a["id"]), rq, rs, limite=10)
        return out

    def buscar_laboratorios(rq, rs, a):
        page, pp = _paged(a)
        return plataforma.list_labs(rq, rs, uf=a.get("uf"), cnpj=a.get("cnpj"), area=a.get("area"), q=_q(a), page=page, per_page=pp)

    def laboratorio(rq, rs, a):
        lid = int(a["id"])
        out: dict[str, Any] = {"laboratorio": plataforma.lab(lid, rq, rs),
                               "pessoas": elos.lab_team(lid, rq, rs, coautores=10, estavel=None)}
        try:
            out["indicadores"] = indicadores.lab_indicators(lid, rq, rs)
        except HTTPException as e:
            if e.status_code != 404:
                raise
            out["indicadores"] = None
        return out

    def laboratorios_parecidos(rq, rs, a):
        return indicadores.lab_citation_synergy(int(a["id"]), rq, rs, so_novos=bool(a.get("so_novos")))

    def buscar_equipamentos(rq, rs, a):
        page, pp = _paged(a)
        return plataforma.list_equipments(rq, rs, uf=a.get("uf"), cnpj=None, laboratorio=None,
                                          multiusuario=a.get("multiusuario"), q=_q(a), page=page, per_page=pp)

    def buscar_pesquisadores(rq, rs, a):
        page, pp = _paged(a)
        return elos.researchers(rq, rs, cnpj=a.get("cnpj"), laboratorio=None, papel=a.get("papel"), estavel=None,
                                origem=None, tema=a.get("tema"), q=_q(a), page=page, per_page=pp)

    def pesquisador(rq, rs, a):
        rid = str(a["id"])
        out = {"pesquisador": elos.researcher(rid, rq, rs)}
        if getattr(rq.state, "editais", False):
            out["editais_abertos_com_perfil"] = perfis.researcher_calls(rid, rq, rs, abertos=True, limite=10)
        return out

    def buscar_publicacoes(rq, rs, a):
        page, pp = _paged(a)
        return obras.search_works(rq, rs, q=_q(a), ano=a.get("ano"), financiador=None, ods=None,
                                  ordem=a.get("ordem") or "recentes", page=page, per_page=pp)

    def buscar_congressos(rq, rs, a):
        page, pp = _paged(a)
        return plataforma.list_conferences(rq, rs, situacao=a.get("situacao") or "proximos", categoria=None,
                                           q=_q(a), page=page, per_page=pp)

    def investimentos(rq, rs, a):
        from api.guard import guard
        guard(rq, rs)
        f = a.get("financiador")
        if not f:
            return {"financiadores": queries.list_funders()}
        d = queries.get_funder(f)
        if d is None:
            raise HTTPException(404, "Financiador não encontrado.")
        top = (d.get("rankings") or {}).get("all", {}).get("institutions", [])[:15]
        return {k: d.get(k) for k in ("id", "name", "notes", "data_as_of", "kpis", "by_year", "by_instrument", "by_uf")} | {
            "instituicoes_que_mais_receberam": top}

    return locals()[name]


TOOL_NAMES = {t["name"] for t in TOOLS}
#: editais estão fora da API pública por enquanto: só para chaves com a permissão `editais`
EDITAIS_TOOLS = {"buscar_editais", "edital"}


def _tools_for(request: Request) -> list[dict[str, Any]]:
    can = identify(request).editais
    return [t for t in TOOLS if can or t["name"] not in EDITAIS_TOOLS]


def _call_tool(request: Request, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name not in TOOL_NAMES:
        raise KeyError(name)
    cliente = identify(request)
    if name in EDITAIS_TOOLS and not cliente.editais:
        return {"content": [{"type": "text", "text": "Editais estão disponíveis só no site do Fomenta BR por enquanto, "
                                                     "não pela API nem pelo MCP."}], "isError": True}
    request.state.editais = cliente.editais
    page, per_page = _paged(args) if ("page" in args or "per_page" in args) else (1, 10)
    try:
        check_scope(cliente.plano, page=page, per_page=per_page)
        data = _tool_impl(name)(request, Response(), args)
    except HTTPException as e:
        return {"content": [{"type": "text", "text": str(e.detail)}], "isError": True}
    except (ValueError, TypeError, KeyError) as e:
        return {"content": [{"type": "text", "text": f"Argumentos inválidos: {e}"}], "isError": True}
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n… (resposta cortada; use filtros ou pagine)"
    return {"content": [{"type": "text", "text": text}]}


# --------------------------------------------------------------------------- JSON-RPC
def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _handle(request: Request, msg: Any) -> dict[str, Any] | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Requisição JSON-RPC inválida.")
    method, id_, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if "id" not in msg:  # notificação: não tem resposta
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": _tools_for(request)}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in {t["name"] for t in _tools_for(request)} and name not in EDITAIS_TOOLS or not isinstance(args, dict):
            return _error(id_, -32602, f"Ferramenta desconhecida: {name}")
        return {"jsonrpc": "2.0", "id": id_, "result": _call_tool(request, name, args)}
    return _error(id_, -32601, f"Método não suportado: {method}")


@router.post("/mcp", summary="Servidor MCP para assistentes de IA", include_in_schema=True)
async def mcp(request: Request) -> Response:
    """JSON-RPC 2.0 do Model Context Protocol (initialize, tools/list, tools/call, ping). Uma mensagem
    ou uma lista por POST; a resposta vem em JSON. Chave opcional em `Authorization: Bearer`."""
    identify(request)  # chave inválida responde 401 logo, como pede o protocolo
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(_error(None, -32700, "JSON inválido."), status_code=400)
    headers = {"Cache-Control": "no-store"}
    if isinstance(body, list):
        out = [r for r in [await run_in_threadpool(_handle, request, m) for m in body] if r is not None]
        return JSONResponse(out, headers=headers) if out else Response(status_code=202, headers=headers)
    res = await run_in_threadpool(_handle, request, body)
    return JSONResponse(res, headers=headers) if res is not None else Response(status_code=202, headers=headers)


@router.get("/mcp", include_in_schema=False)
def mcp_get() -> Response:
    # Sem fluxo SSE do servidor para o cliente: o protocolo manda responder 405.
    return Response(status_code=405, headers={"Allow": "POST"})
