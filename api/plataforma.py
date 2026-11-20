"""Rotas /v1 da plataforma: editais, congressos, infraestrutura (PNIPE), sinergia e Elos.

Os dados vêm do retrato do Supabase do FomentaBR (`tools/export_supabase_snapshot.py`),
carregado no `fomenta.duckdb` pelo build. Somente leitura, tudo por parâmetro.
A busca (`q`) é sem acento e sem caixa, sobre a coluna `busca` montada no build.
"""
from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request, Response

from api import queries
from api.guard import guard

MAX = queries.MAX_PER_PAGE
PAGE = Query(1, ge=1, le=5000)
PER_PAGE = Query(25, ge=1, le=MAX)
Q = Query(None, min_length=3, max_length=80, description="busca sem acento e sem caixa")
UF = Query(None, min_length=2, max_length=2, description="sigla da UF")
CALL_KINDS = ("chamada", "bolsa", "selecao", "credenciamento", "atualizacao")
CALL_KIND_SQL = (
    "CASE WHEN json_extract_string(e.extraction_metadata, '$.call_kind') IN "
    f"({', '.join(repr(k) for k in CALL_KINDS)}) THEN json_extract_string(e.extraction_metadata, '$.call_kind') "
    "WHEN e.modality = 'scholarship' THEN 'bolsa' WHEN e.modality = 'other' THEN 'atualizacao' ELSE 'chamada' END"
)
CALL_LISTS = ("eligible_entities", "funding_items", "counterpart_requirements", "required_documents",
              "evaluation_criteria")


def _norm(s: str) -> str:
    n = unicodedata.normalize("NFKD", s).encode("ASCII", "ignore").decode("ASCII")
    return " ".join(n.lower().split())


def _search(expr: str, q: str, where: list[str], params: list[Any]) -> None:
    """Cada palavra de `q` precisa aparecer em `expr` (já sem acento e minúscula), em qualquer ordem."""
    for word in _norm(q).split()[:8]:
        where.append(f"{expr} LIKE ?")
        params.append(f"%{word}%")


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat() + "Z"
    return v.isoformat() if hasattr(v, "isoformat") else v


def _json(v: str | None) -> Any:
    return json.loads(v) if v else None


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _page(sql_from: str, where: list[str], params: list[Any], select: str, order: str,
          page: int, per_page: int, row: Callable[[tuple], dict]) -> dict[str, Any]:
    w = " AND ".join(where) or "true"
    cur = queries.cursor()
    total = cur.execute(f"SELECT count(*) FROM {sql_from} WHERE {w}", params).fetchone()[0]
    rows = cur.execute(f"SELECT {select} FROM {sql_from} WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                       [*params, per_page, (page - 1) * per_page]).fetchall()
    return {"total": total, "page": page, "per_page": per_page, "items": [row(r) for r in rows]}


def _one(sql: str, params: list[Any]) -> tuple | None:
    return queries.cursor().execute(sql, params).fetchone()


def _all(sql: str, params: list[Any]) -> list[tuple]:
    return queries.cursor().execute(sql, params).fetchall()


# --------------------------------------------------------------------------- editais
editais = APIRouter(prefix="/v1", tags=["Editais"])
EDITAL_COLS = (f"e.id, e.title, e.description_summary, e.submission_deadline, e.publication_date, "
               f"e.max_funding_value, e.total_funding_value, e.currency, e.modality, {CALL_KIND_SQL}, e.status, "
               "e.source_url, e.pdf_url, a.slug, a.name, e.target_audience")


def _edital(r: tuple) -> dict[str, Any]:
    return {"id": r[0], "titulo": r[1], "resumo": r[2], "prazo": _iso(r[3]), "publicacao": _iso(r[4]),
            "valor_maximo": r[5], "valor_total": r[6], "moeda": r[7], "modalidade": r[8], "tipo": r[9],
            "situacao": r[10], "url": r[11], "pdf_url": r[12], "agencia": {"slug": r[13], "nome": r[14]},
            "publico_alvo": r[15]}


@editais.get("/editais", summary="Editais de fomento")
def list_calls(
    request: Request, response: Response,
    agencia: str | None = Query(None, max_length=60, description="slug da agência (ver /v1/agencias)"),
    situacao: str = Query("todos", pattern="^(abertos|encerrados|sem_prazo|todos)$",
                          description="pelo prazo de submissão, na data da consulta"),
    tipo: str | None = Query(None, pattern=f"^({'|'.join(CALL_KINDS)})$"),
    modalidade: str | None = Query(None, max_length=40),
    area: str | None = Query(None, max_length=20, description="código da área do conhecimento (ver /v1/areas)"),
    q: str | None = Q, page: int = PAGE, per_page: int = PER_PAGE,
) -> dict:
    """Ordenados pelo prazo: os abertos do mais próximo ao mais distante; os demais, do mais recente."""
    guard(request, response)
    where, params = [], []
    if agencia:
        where.append("a.slug = ?")
        params.append(agencia)
    now = datetime.now(UTC).replace(tzinfo=None)
    if situacao == "abertos":
        where.append("e.submission_deadline >= ?")
        params.append(now)
    elif situacao == "encerrados":
        where.append("e.submission_deadline < ?")
        params.append(now)
    elif situacao == "sem_prazo":
        where.append("e.submission_deadline IS NULL")
    if tipo:
        where.append(f"({CALL_KIND_SQL}) = ?")
        params.append(tipo)
    if modalidade:
        where.append("e.modality = ?")
        params.append(modalidade)
    if area:
        where.append("e.id IN (SELECT ea.call_id FROM edital_area ea JOIN area_conhecimento k ON k.id = ea.area_id "
                     "WHERE k.code = ?)")
        params.append(area)
    if q:
        _search("e.busca", q, where, params)
    order = ("e.submission_deadline ASC NULLS LAST, e.id" if situacao == "abertos"
             else "e.submission_deadline DESC NULLS LAST, e.id DESC")
    return _page("edital e JOIN agencia a ON a.id = e.agency_id", where, params, EDITAL_COLS, order,
                 page, per_page, _edital)


@editais.get("/editais/{call_id}", summary="Detalhe de um edital")
def call_detail(call_id: int, request: Request, response: Response) -> dict:
    guard(request, response)
    r = _one(f"SELECT {EDITAL_COLS}, e.extraction_metadata, e.discovered_at, e.last_seen_at "
             "FROM edital e JOIN agencia a ON a.id = e.agency_id WHERE e.id = ?", [call_id])
    if not r:
        raise HTTPException(404, "Edital não encontrado.")
    meta = _json(r[16]) or {}
    areas = [{"codigo": c, "nome": n} for c, n in _all(
        "SELECT k.code, k.name FROM edital_area ea JOIN area_conhecimento k ON k.id = ea.area_id "
        "WHERE ea.call_id = ? ORDER BY ea.confidence DESC", [call_id])]
    has_text = _one("SELECT 1 FROM edital_texto WHERE call_id = ?", [call_id]) is not None
    return {**_edital(r), "areas": areas, **{k: meta.get(k) for k in CALL_LISTS},
            "confianca_extracao": meta.get("confidence"), "descoberto_em": _iso(r[17]), "visto_em": _iso(r[18]),
            "texto_url": f"/v1/editais/{call_id}/texto" if has_text else None}


@editais.get("/editais/{call_id}/texto", summary="Texto integral do edital")
def call_text(call_id: int, request: Request, response: Response) -> dict:
    """O texto extraído da página ou do PDF do edital, como o coletor o leu."""
    guard(request, response)
    r = _one("SELECT texto FROM edital_texto WHERE call_id = ?", [call_id])
    if not r:
        raise HTTPException(404, "Texto não disponível para este edital.")
    return {"id": call_id, "texto": r[0]}


@editais.get("/agencias", summary="Agências de fomento")
def agencies(request: Request, response: Response) -> dict:
    guard(request, response)
    rows = _all("SELECT a.slug, a.name, a.type, a.base_url, count(e.id) FROM agencia a "
                "LEFT JOIN edital e ON e.agency_id = a.id GROUP BY ALL ORDER BY a.name", [])
    return {"items": [{"slug": r[0], "nome": r[1], "tipo": r[2], "site": r[3], "editais": r[4]} for r in rows]}


@editais.get("/areas", summary="Áreas do conhecimento usadas nos editais")
def areas(request: Request, response: Response) -> dict:
    guard(request, response)
    rows = _all("SELECT k.code, k.name, k.level, p.code, count(ea.call_id) FROM area_conhecimento k "
                "LEFT JOIN area_conhecimento p ON p.id = k.parent_id LEFT JOIN edital_area ea ON ea.area_id = k.id "
                "GROUP BY ALL ORDER BY k.code", [])
    return {"items": [{"codigo": r[0], "nome": r[1], "nivel": r[2], "pai": r[3], "editais": r[4]} for r in rows]}


# --------------------------------------------------------------------------- congressos
congressos = APIRouter(prefix="/v1", tags=["Congressos"])
CONF_COLS = "id, title, acronym, location, start_date, end_date, submission_deadline, categories, source_url"


def _conf(r: tuple) -> dict[str, Any]:
    return {"id": r[0], "titulo": r[1], "sigla": r[2], "local": r[3], "inicio": _iso(r[4]), "fim": _iso(r[5]),
            "prazo_submissao": _iso(r[6]), "categorias": r[7], "url": r[8]}


@congressos.get("/congressos", summary="Congressos e eventos científicos")
def list_conferences(
    request: Request, response: Response,
    situacao: str = Query("todos", pattern="^(proximos|passados|todos)$", description="pela data de início"),
    categoria: str | None = Query(None, max_length=60),
    q: str | None = Q, page: int = PAGE, per_page: int = PER_PAGE,
) -> dict:
    guard(request, response)
    where, params = [], []
    today = datetime.now(UTC).date()
    if situacao == "proximos":
        where.append("start_date >= ?")
        params.append(today)
    elif situacao == "passados":
        where.append("start_date < ?")
        params.append(today)
    if categoria:
        where.append("list_contains(categories, ?)")
        params.append(categoria)
    if q:
        _search("busca", q, where, params)
    order = "start_date ASC NULLS LAST, id" if situacao == "proximos" else "start_date DESC NULLS LAST, id"
    return _page("congresso", where, params, CONF_COLS, order, page, per_page, _conf)


@congressos.get("/congressos/{conf_id}", summary="Detalhe de um congresso")
def conference(conf_id: int, request: Request, response: Response) -> dict:
    guard(request, response)
    r = _one(f"SELECT {CONF_COLS}, description, notification_date FROM congresso WHERE id = ?", [conf_id])
    if not r:
        raise HTTPException(404, "Congresso não encontrado.")
    return {**_conf(r), "descricao": r[9], "notificacao": _iso(r[10])}


# --------------------------------------------------------------------------- infraestrutura
infra = APIRouter(prefix="/v1", tags=["Infraestrutura (PNIPE)"])


def _inst(r: tuple) -> dict[str, Any]:
    return {"cnpj": r[0], "nome": r[1], "sigla": r[2], "cidade": r[3], "uf": r[4], "laboratorios": r[5],
            "equipamentos": r[6], "embrapii": r[7], "forca_armada": r[8]}


INST_COLS = "cnpj, social_reason, initials, city, state, total_labs, total_equipments, is_embrapii, military_branch"


@infra.get("/instituicoes", summary="Instituições com infraestrutura de pesquisa")
def list_institutions(
    request: Request, response: Response, uf: str | None = UF,
    embrapii: bool | None = Query(None, description="só unidades Embrapii"),
    forca_armada: str | None = Query(None, pattern="^(marinha|exercito|aeronautica)$"),
    q: str | None = Q, page: int = PAGE, per_page: int = PER_PAGE,
) -> dict:
    guard(request, response)
    where, params = [], []
    if uf:
        where.append("state = ?")
        params.append(uf.upper())
    if embrapii is not None:
        where.append("is_embrapii = ?")
        params.append(embrapii)
    if forca_armada:
        where.append("military_branch = ?")
        params.append(forca_armada)
    if q:
        _search("busca", q, where, params)
    return _page("pnipe_institution", where, params, INST_COLS, "total_equipments DESC NULLS LAST, cnpj",
                 page, per_page, _inst)


def cnpj_or_ror(v: str) -> str:
    """CNPJ (com ou sem pontuação), ROR (`036rp1748` ou a URL) ou id do OpenAlex (`I17974374`) -> CNPJ do PNIPE.
    Sem par conhecido devolve o que veio, só com os números (a rota responde 404)."""
    v = v.strip()
    if any(ch.isalpha() for ch in v):
        key = v.rstrip("/").rsplit("/", 1)[-1]
        r = _one("SELECT arg_max(cnpj, confianca) FROM elos_institution WHERE ror = ? OR openalex_id = ?",
                 [f"https://ror.org/{key.lower()}", key.upper()])
        if r and r[0]:
            return r[0]
    return _digits(v)


@infra.get("/instituicoes/ror/{ror}", summary="Instituição pelo ROR ou pelo id do OpenAlex")
def institution_by_ror(ror: str, request: Request, response: Response) -> dict:
    """ROR (`036rp1748`) ou id do OpenAlex (`I17974374`): a instituição no OpenAlex, a instituição-mãe e o
    CNPJ do PNIPE quando há casamento (`origem`: auto, manual, nome_openalex ou lineage, que herda da mãe)."""
    guard(request, response)
    key = ror.strip().rstrip("/").rsplit("/", 1)[-1]
    i = _one("SELECT i.id, i.ror, i.nome, i.siglas, i.tipo, i.cidade, i.uf, i.publicacoes, i.mae, m.nome "
             "FROM oa_institution_br i LEFT JOIN oa_institution_br m ON m.id = i.mae "
             "WHERE i.ror = ? OR i.id = ?", [f"https://ror.org/{key.lower()}", key.upper()])
    if not i:
        raise HTTPException(404, "Instituição brasileira não encontrada no OpenAlex.")
    pares = _all("SELECT e.cnpj, p.initials, p.social_reason, e.confianca, e.origem FROM elos_institution e "
                 "LEFT JOIN pnipe_institution p ON p.cnpj = e.cnpj WHERE e.openalex_id = ? ORDER BY e.confianca DESC",
                 [i[0]])
    unidades = _all("SELECT id, ror, nome, tipo FROM oa_institution_br WHERE mae = ? ORDER BY publicacoes DESC NULLS LAST "
                    "LIMIT 50", [i[0]])
    return {"openalex": i[0], "ror": i[1], "nome": i[2], "siglas": i[3], "tipo": i[4], "cidade": i[5], "uf": i[6],
            "publicacoes": i[7], "mae": {"openalex": i[8], "nome": i[9]} if i[8] else None,
            "pnipe": [{"cnpj": x[0], "sigla": x[1], "nome": x[2], "confianca": x[3], "origem": x[4]} for x in pares],
            "unidades": [{"openalex": x[0], "ror": x[1], "nome": x[2], "tipo": x[3]} for x in unidades]}


@infra.get("/instituicoes/{cnpj}", summary="Detalhe de uma instituição")
def institution(cnpj: str, request: Request, response: Response) -> dict:
    """CNPJ com os 14 números, ROR ou id do OpenAlex. Os investimentos recebidos estão em
    `/v1/instituicoes/{cnpj}/investimentos`."""
    guard(request, response)
    d = cnpj_or_ror(cnpj)
    r = _one(f"SELECT {INST_COLS}, zip_code, street, number, complement, district, phone_number, situation, "
             "embrapii_units FROM pnipe_institution WHERE cnpj = ?", [d])
    if not r:
        raise HTTPException(404, "Instituição não encontrada no cadastro do PNIPE.")
    oa = _all("SELECT e.openalex_id, e.ror, e.nome_openalex, e.confianca, e.origem FROM elos_institution e "
              "WHERE e.cnpj = ? ORDER BY e.origem = 'lineage', e.confianca DESC, e.openalex_id", [d])
    pessoas = _one("SELECT count(*) FILTER (WHERE origem <> 'coautor'), count(*) FILTER (WHERE origem = 'coautor') "
                   "FROM elos_researcher WHERE inst_cnpj = ?", [d])
    return {**_inst(r), "endereco": {"cep": r[9], "logradouro": r[10], "numero": r[11], "complemento": r[12],
                                     "bairro": r[13]},
            "telefone": r[14], "situacao": r[15], "unidades_embrapii": r[16],
            # casamento com o OpenAlex: a instituição e as unidades que herdam o CNPJ pela hierarquia (lineage)
            "openalex": [{"id": x[0], "ror": x[1], "nome": x[2], "confianca": x[3], "origem": x[4]} for x in oa],
            "elos": {"pesquisadores_da_base": pessoas[0], "coautores": pessoas[1],
                     "url": f"/v1/elos/pesquisadores?cnpj={d}"},
            "laboratorios_url": f"/v1/laboratorios?cnpj={d}", "investimentos_url": f"/v1/instituicoes/{d}/investimentos"}


LAB_COLS = ("l.id, l.name, l.initials, l.institution_cnpj, i.initials, i.social_reason, l.city, l.resolved_state, "
            "l.area_keys, (SELECT count(*) FROM pnipe_equipment q WHERE q.laboratory_id = l.id)")


def _lab(r: tuple) -> dict[str, Any]:
    return {"id": r[0], "nome": r[1], "sigla": r[2], "instituicao": {"cnpj": r[3], "sigla": r[4], "nome": r[5]},
            "cidade": r[6], "uf": r[7], "areas": r[8], "equipamentos": r[9]}


@infra.get("/laboratorios", summary="Laboratórios")
def list_labs(
    request: Request, response: Response, uf: str | None = UF,
    cnpj: str | None = Query(None, max_length=20, description="CNPJ da instituição"),
    area: str | None = Query(None, max_length=120, description="área de atuação (sem acento, minúscula)"),
    q: str | None = Q, page: int = PAGE, per_page: int = PER_PAGE,
) -> dict:
    guard(request, response)
    where, params = [], []
    if uf:
        where.append("l.resolved_state = ?")
        params.append(uf.upper())
    if cnpj:
        where.append("l.institution_cnpj = ?")
        params.append(_digits(cnpj))
    if area:
        where.append("list_contains(l.area_keys, ?)")
        params.append(_norm(area))
    if q:
        _search("l.busca", q, where, params)
    return _page("pnipe_laboratory l LEFT JOIN pnipe_institution i ON i.cnpj = l.institution_cnpj", where, params,
                 LAB_COLS, "l.name, l.id", page, per_page, _lab)


@infra.get("/laboratorios/{lab_id}", summary="Detalhe de um laboratório")
def lab(lab_id: int, request: Request, response: Response) -> dict:
    """Sem o contato pessoal do responsável (nome, telefone e e-mail), que o PNIPE publica mas a API não repassa."""
    guard(request, response)
    r = _one(f"SELECT {LAB_COLS}, l.about, l.website, l.latitude, l.longitude, l.area_expertise_list, "
             "l.technique_list, l.zip_code, l.street, l.number, l.complement, l.district "
             "FROM pnipe_laboratory l LEFT JOIN pnipe_institution i ON i.cnpj = l.institution_cnpj WHERE l.id = ?",
             [lab_id])
    if not r:
        raise HTTPException(404, "Laboratório não encontrado.")
    equips = _all("SELECT id, name, brand, model, is_multiuser, situation FROM pnipe_equipment "
                  "WHERE laboratory_id = ? ORDER BY name", [lab_id])
    return {**_lab(r), "sobre": r[10], "site": r[11], "latitude": r[12], "longitude": r[13],
            "areas_de_atuacao": [a.get("name") for a in (_json(r[14]) or [])],
            "tecnicas": [t.get("name") for t in (_json(r[15]) or [])],
            "endereco": {"cep": r[16], "logradouro": r[17], "numero": r[18], "complemento": r[19], "bairro": r[20]},
            "lista_equipamentos": [{"id": e[0], "nome": e[1], "marca": e[2], "modelo": e[3], "multiusuario": e[4],
                                    "situacao": e[5]} for e in equips],
            "pesquisadores_url": f"/v1/elos/laboratorios/{lab_id}", "sinergia_url": f"/v1/sinergia/laboratorios/{lab_id}"}


EQ_COLS = ("q.id, q.name, q.brand, q.model, q.is_multiuser, q.situation, q.laboratory_id, l.name, "
           "q.institution_cnpj, l.resolved_state")


def _equip(r: tuple) -> dict[str, Any]:
    return {"id": r[0], "nome": r[1], "marca": r[2], "modelo": r[3], "multiusuario": r[4], "situacao": r[5],
            "laboratorio": {"id": r[6], "nome": r[7]}, "instituicao_cnpj": r[8], "uf": r[9]}


@infra.get("/equipamentos", summary="Equipamentos de pesquisa")
def list_equipments(
    request: Request, response: Response, uf: str | None = UF,
    cnpj: str | None = Query(None, max_length=20, description="CNPJ da instituição"),
    laboratorio: int | None = Query(None, description="id do laboratório"),
    multiusuario: bool | None = None,
    q: str | None = Q, page: int = PAGE, per_page: int = PER_PAGE,
) -> dict:
    guard(request, response)
    where, params = [], []
    if uf:
        where.append("l.resolved_state = ?")
        params.append(uf.upper())
    if cnpj:
        where.append("q.institution_cnpj = ?")
        params.append(_digits(cnpj))
    if laboratorio is not None:
        where.append("q.laboratory_id = ?")
        params.append(laboratorio)
    if multiusuario is not None:
        where.append("q.is_multiuser = ?")
        params.append(multiusuario)
    if q:
        _search("q.busca", q, where, params)
    return _page("pnipe_equipment q LEFT JOIN pnipe_laboratory l ON l.id = q.laboratory_id", where, params,
                 EQ_COLS, "q.name, q.id", page, per_page, _equip)


@infra.get("/equipamentos/{equip_id}", summary="Detalhe de um equipamento")
def equipment(equip_id: int, request: Request, response: Response) -> dict:
    guard(request, response)
    r = _one(f"SELECT {EQ_COLS}, q.initials, q.code, q.purchase_year, q.website, q.technique_list, q.parameter_list "
             "FROM pnipe_equipment q LEFT JOIN pnipe_laboratory l ON l.id = q.laboratory_id WHERE q.id = ?",
             [equip_id])
    if not r:
        raise HTTPException(404, "Equipamento não encontrado.")
    return {**_equip(r), "sigla": r[10], "codigo": r[11], "ano_compra": r[12], "site": r[13],
            "tecnicas": [t.get("name") for t in (_json(r[14]) or [])],
            "parametros": [p.get("name") for p in (_json(r[15]) or [])]}


# --------------------------------------------------------------------------- sinergia
sinergia = APIRouter(prefix="/v1/sinergia", tags=["Sinergia"])
BASES = ("hybrid", "technique", "equipment")
RELATIONS = ("affinity", "complementarity")


@sinergia.get("/grafos", summary="Grafos de sinergia (por área e por grande área)")
def graphs(
    request: Request, response: Response,
    tipo: str | None = Query(None, pattern="^(area|grande_area)$"),
    q: str | None = Q, page: int = PAGE, per_page: int = PER_PAGE,
) -> dict:
    guard(request, response)
    where, params = [], []
    if tipo:
        where.append("kind = ?")
        params.append(tipo)
    if q:
        _search("lower(strip_accents(label))", q, where, params)
    return _page("synergy_graph", where, params, "key, kind, label, member_count, len(node_ids)",
                 "member_count DESC, key", page, per_page,
                 lambda r: {"chave": r[0], "tipo": r[1], "rotulo": r[2], "laboratorios_na_area": r[3], "nos": r[4]})


@sinergia.get("/grafos/{key:path}", summary="Nós e arestas de um grafo")
def graph(
    key: str, request: Request, response: Response,
    base: str = Query("hybrid", pattern=f"^({'|'.join(BASES)})$", description="técnicas, equipamentos ou os dois"),
    relacao: str = Query("affinity", pattern=f"^({'|'.join(RELATIONS)})$",
                         description="afinidade (portfólios parecidos) ou complementaridade (pontes)"),
) -> dict:
    """Arestas `[lab_a, lab_b, tokens_em_comum, score]`, já cortadas no top N por nó do pré-cálculo."""
    guard(request, response)
    r = _one("SELECT key, kind, label, member_count, node_ids, edges, communities FROM synergy_graph WHERE key = ?",
             [key])
    if not r:
        raise HTTPException(404, "Grafo não encontrado.")
    edges = ((_json(r[5]) or {}).get(base) or {}).get(relation := relacao) or []
    communities = (_json(r[6]) or {}).get(base) or {}
    nodes = _all("SELECT lab_id, name, initials, inst_cnpj, inst_label, uf, equip_count FROM synergy_lab "
                 "WHERE list_contains(?, lab_id)", [r[4]])
    return {"chave": r[0], "tipo": r[1], "rotulo": r[2], "laboratorios_na_area": r[3], "base": base,
            "relacao": relation,
            "nos": [{"id": n[0], "nome": n[1], "sigla": n[2], "instituicao_cnpj": n[3], "instituicao": n[4],
                     "uf": n[5], "equipamentos": n[6], "comunidade": communities.get(str(n[0]))} for n in nodes],
            "arestas": [{"a": e[0], "b": e[1], "em_comum": e[2], "score": e[3]} for e in edges]}


@sinergia.get("/laboratorios/{lab_id}", summary="Perfil de sinergia de um laboratório")
def synergy_lab(lab_id: int, request: Request, response: Response) -> dict:
    """Técnicas e equipamentos canônicos do laboratório (os tokens usados no cálculo) e os grafos em que aparece."""
    guard(request, response)
    r = _one("SELECT lab_id, name, initials, inst_label, uf, equip_count, tt, et, em, areas, ga FROM synergy_lab "
             "WHERE lab_id = ?", [lab_id])
    if not r:
        raise HTTPException(404, "Laboratório fora da sinergia.")

    def labels(base: str, ids: list[int] | None) -> list[str]:
        if not ids:
            return []
        return [x[0] for x in _all("SELECT label FROM synergy_token_label WHERE base = ? AND list_contains(?, token_id) "
                                   "ORDER BY label", [base, ids])]

    keys = [x[0] for x in _all("SELECT key FROM synergy_graph WHERE list_contains(node_ids, ?) ORDER BY member_count DESC",
                               [lab_id])]
    return {"id": r[0], "nome": r[1], "sigla": r[2], "instituicao": r[3], "uf": r[4], "equipamentos": r[5],
            "tecnicas": labels("technique", r[6]), "tipos_de_equipamento": labels("equipment", r[7]),
            "oferece_a_terceiros": labels("equipment", r[8]), "areas": r[9], "grandes_areas": r[10], "grafos": keys}


ROUTERS = (editais, congressos, infra, sinergia)
