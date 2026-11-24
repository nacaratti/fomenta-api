"""Fomenta API — API pública e somente leitura sobre o `fomenta.duckdb` gerado no build.

Na Vercel, as rotas fixas (`/v1/investimentos/financiadores`, `/financiadores/{id}`
e `/v1/meta`) saem como JSON estático do build (ver `vercel.json`) e nem chegam
aqui; esta função atende as rotas com filtro. Localmente (`uvicorn api.index:app`)
todas as rotas passam por aqui.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import PlainTextResponse, RedirectResponse  # noqa: E402

from api import queries  # noqa: E402
from api.guard import guard as _guard  # noqa: E402
from api.plataforma import cnpj_or_ror  # noqa: E402
from api.elos import elos as elos_router  # noqa: E402
from api.obras import ROUTERS as OBRAS  # noqa: E402
from api.auxilios import ROUTERS as AUXILIOS  # noqa: E402
from api.perfis import ROUTERS as PERFIS  # noqa: E402
from api.indicadores import ROUTERS as INDICADORES  # noqa: E402
from api.plataforma import ROUTERS  # noqa: E402
from api.mcp import router as mcp_router  # noqa: E402
from api.avisos import router as avisos_router  # noqa: E402
from api.newsletter import router as newsletter_router  # noqa: E402
from api.planos import PLANOS, public as plano_publico  # noqa: E402
from api import sessao  # noqa: E402
from api.contador import counter  # noqa: E402
from api.guard import NO_STORE, client_ip  # noqa: E402

DESCRIPTION = """
Dados abertos do **Fomenta** sobre fomento e infraestrutura de pesquisa no Brasil:

- **Investimentos em PD&I** da FINEP, da CAPES (PROAP/PROEX) e do CNPq (bolsas e auxílios);
- **Editais** de fomento e **congressos** científicos coletados pelo FomentaBR;
- **Infraestrutura** do PNIPE: instituições, laboratórios e equipamentos;
- **Sinergia** entre laboratórios e **Elos**, os pesquisadores e as coautorias (OpenAlex).

Editais, congressos, PNIPE, sinergia e Elos vêm de um retrato do banco do FomentaBR; a data está em `/v1/meta`.

- Valores **nominais**, em reais, sem correção pela inflação.
- Cada financiador conta de um jeito (a FINEP traz projetos e liberações; CAPES e CNPq, o valor pago no ano).
  **Não some financiadores diferentes**: parte do FNDCT passa por mais de uma fonte.
- `data_as_of` é a posição informada pela própria fonte; `imported_at`, quando o Fomenta importou.
- Os dados completos (DuckDB e Parquet) ficam nas Releases do repositório, para análise offline.

**Acesso e planos.** Sem chave, vale o plano Aberto (por IP): 60 consultas por minuto, 2.000 por dia, até 50
itens por página e até 1.000 registros por consulta. Com uma chave (`Authorization: Bearer fmt_...` ou
`X-API-Key: fmt_...`) os limites sobem conforme o plano; veja `/v1/planos`. Toda resposta traz `X-Plano` e
`X-RateLimit-*`. Para assistentes de IA, há um servidor MCP em `POST /mcp`.

**Editais** estão disponíveis só no site do Fomenta BR por enquanto (ou com chave institucional com essa
permissão); as rotas `/v1/editais...` respondem 403 para a API pública.
"""

app = FastAPI(title="Fomenta API", version="1.1.0", description=DESCRIPTION,
              docs_url="/v1/docs", redoc_url=None, openapi_url="/v1/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"],
                   expose_headers=["X-Plano", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                                   "X-RateLimit-Limit-Day", "X-RateLimit-Remaining-Day", "Retry-After",
                                   "X-Fomenta-Motivo"])

investimentos = APIRouter(prefix="/v1/investimentos", tags=["Investimentos"])
instituicoes = APIRouter(prefix="/v1/instituicoes", tags=["Instituições"])


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/v1/docs")


@app.get("/v1/meta", tags=["Sobre os dados"], summary="Posição de cada fonte e arquivos do build")
def meta(request: Request, response: Response) -> dict:
    _guard(request, response)
    return queries.meta()


ROBOTS = """# Fomenta API. Os dados abertos podem ser consultados com os limites de /v1/planos.
# Editais não podem ser coletados por robôs, inclusive de IA.
User-agent: *
Disallow: /v1/editais
Disallow: /v1/elos/pesquisadores/*/editais
Disallow: /v1/elos/laboratorios/*/editais
Disallow: /v1/sessao
Disallow: /v1/avisos
Disallow: /v1/newsletter/edicoes/
Disallow: /mcp
Allow: /v1/docs
Allow: /

User-agent: GPTBot
User-agent: ChatGPT-User
User-agent: OAI-SearchBot
User-agent: ClaudeBot
User-agent: Claude-Web
User-agent: anthropic-ai
User-agent: Google-Extended
User-agent: PerplexityBot
User-agent: CCBot
User-agent: Bytespider
User-agent: Amazonbot
User-agent: meta-externalagent
User-agent: Applebot-Extended
Disallow: /v1/editais
Disallow: /v1/elos/pesquisadores/*/editais
Disallow: /v1/elos/laboratorios/*/editais
Disallow: /v1/sessao
Disallow: /v1/newsletter/edicoes/
"""


@app.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse(ROBOTS, headers={"Cache-Control": "public, s-maxage=86400"})


@app.post("/v1/sessao", include_in_schema=False)
def site_session(request: Request, response: Response) -> dict:
    """Token curto para o site oficial ver os editais. Só responde às origens do site."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin not in sessao.allowed_origins():
        raise HTTPException(403, "Sessão disponível só para o site do Fomenta BR.", headers={"Cache-Control": NO_STORE})
    ip = client_ip(request)
    minute = int(time.time() // 60)
    if counter.hit(f"rl:sessao:{ip}:{minute}", 70) > 20:
        raise HTTPException(429, "Muitos pedidos de sessão. Tente de novo em instantes.",
                            headers={"Retry-After": "60", "Cache-Control": NO_STORE})
    token, exp = sessao.issue(ip)
    response.headers["Cache-Control"] = NO_STORE
    return {"token": token, "expira": exp}


@app.get("/v1/planos", tags=["Acesso e planos"], summary="Planos de acesso e seus limites")
def planos(request: Request, response: Response) -> dict:
    """Os planos da API, do Aberto (sem chave) ao Institucional, com limites por minuto, por dia, por página e
    de profundidade de paginação. `preco_mensal` nulo quer dizer sob consulta."""
    _guard(request, response)
    return {"items": [plano_publico(p) for p in PLANOS.values()],
            "como_usar_a_chave": "Envie no cabeçalho `Authorization: Bearer <chave>` ou `X-API-Key: <chave>`. "
                                 "Não coloque a chave na URL.",
            "mcp": "/mcp"}


@app.get("/v1/conta", tags=["Acesso e planos"], summary="Seu plano e quanto falta da cota")
def conta(request: Request, response: Response) -> dict:
    """Com chave, mostra o plano e o que resta da cota do minuto e do dia (esta consulta conta)."""
    cliente = _guard(request, response)
    response.headers["Cache-Control"] = "private, no-store"
    h = response.headers
    return {"plano": plano_publico(cliente.plano), "nome": cliente.nome, "com_chave": not cliente.id.startswith("ip:"),
            "restante_no_minuto": int(h["X-RateLimit-Remaining"]), "restante_no_dia": int(h["X-RateLimit-Remaining-Day"])}


@investimentos.get("/financiadores", summary="Financiadores disponíveis")
def funders(request: Request, response: Response) -> dict:
    """Totais (aprovado, pago, nº de registros, anos) e a posição dos dados de cada financiador."""
    _guard(request, response)
    return {"items": queries.list_funders()}


@investimentos.get("/financiadores/{funder_id}", summary="Resumo de um financiador")
def funder(funder_id: str, request: Request, response: Response) -> dict:
    """Totais por ano, instrumento, situação, UF e região, e o ranking de instituições."""
    _guard(request, response)
    data = queries.get_funder(funder_id.lower())
    if data is None:
        raise HTTPException(404, "Financiador não encontrado.")
    return data


@investimentos.get("/projetos", summary="Projetos e concessões")
def awards(
    request: Request, response: Response,
    financiador: str | None = Query(None, description="finep, capes ou cnpq"),
    ano: int | None = Query(None, ge=1990, le=2100, description="ano de assinatura/concessão"),
    uf: str | None = Query(None, min_length=2, max_length=2, description="sigla da UF"),
    instrumento: str | None = Query(None, max_length=200, description="rótulo exato, como em /financiadores/{id}"),
    cnpj: str | None = Query(None, max_length=40, description="CNPJ de uma instituição do PNIPE, ROR ou id do OpenAlex"),
    q: str | None = Query(None, min_length=3, max_length=80, description="busca no título e nos nomes"),
    page: int = Query(1, ge=1, le=2000),
    per_page: int = Query(25, ge=1, le=queries.MAX_PER_PAGE),
) -> dict:
    """Ordenados pelo valor aprovado, do maior para o menor. `detail` traz o que é próprio de cada fonte
    (modalidades e origem do recurso no CNPq; custeio/capital e programa de pós na CAPES)."""
    _guard(request, response)
    return queries.list_awards(funder=financiador.lower() if financiador else None, year=ano, uf=uf,
                               instrument=instrumento, cnpj=cnpj_or_ror(cnpj) if cnpj and any(ch.isalpha() for ch in cnpj) else cnpj,
                               q=q, page=page, per_page=per_page)


@instituicoes.get("/{cnpj}/investimentos", summary="Investimentos recebidos por uma instituição")
def institution(cnpj: str, request: Request, response: Response) -> dict:
    """Por financiador e por ano, para uma instituição do cadastro do PNIPE (CNPJ com os 14 números, ROR ou
    id do OpenAlex)."""
    _guard(request, response)
    data = queries.institution_investments(cnpj_or_ror(cnpj) if any(ch.isalpha() for ch in cnpj) else cnpj)
    if data is None:
        raise HTTPException(404, "Instituição não encontrada no cadastro do PNIPE.")
    return data


app.include_router(investimentos)
app.include_router(instituicoes)
for _r in ROUTERS:
    app.include_router(_r)
app.include_router(elos_router)
for _r in (*OBRAS, *AUXILIOS, *PERFIS, *INDICADORES):
    app.include_router(_r)
app.include_router(mcp_router)
app.include_router(avisos_router)
app.include_router(newsletter_router)

