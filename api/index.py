"""Fomenta API — API pública e somente leitura sobre o `fomenta.duckdb` gerado no build.

Na Vercel, as rotas fixas (`/v1/investimentos/financiadores`, `/financiadores/{id}`
e `/v1/meta`) saem como JSON estático do build (ver `vercel.json`) e nem chegam
aqui; esta função atende as rotas com filtro. Localmente (`uvicorn api.index:app`)
todas as rotas passam por aqui.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402

from api import queries  # noqa: E402
from api.guard import guard as _guard  # noqa: E402
from api.elos import elos as elos_router  # noqa: E402
from api.obras import ROUTERS as OBRAS  # noqa: E402
from api.auxilios import ROUTERS as AUXILIOS  # noqa: E402
from api.perfis import ROUTERS as PERFIS  # noqa: E402
from api.plataforma import ROUTERS  # noqa: E402

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
"""

app = FastAPI(title="Fomenta API", version="1.1.0", description=DESCRIPTION,
              docs_url="/v1/docs", redoc_url=None, openapi_url="/v1/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

investimentos = APIRouter(prefix="/v1/investimentos", tags=["Investimentos"])
instituicoes = APIRouter(prefix="/v1/instituicoes", tags=["Instituições"])


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/v1/docs")


@app.get("/v1/meta", tags=["Sobre os dados"], summary="Posição de cada fonte e arquivos do build")
def meta(request: Request, response: Response) -> dict:
    _guard(request, response)
    return queries.meta()


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
    cnpj: str | None = Query(None, max_length=20, description="CNPJ de uma instituição do PNIPE"),
    q: str | None = Query(None, min_length=3, max_length=80, description="busca no título e nos nomes"),
    page: int = Query(1, ge=1, le=2000),
    per_page: int = Query(25, ge=1, le=queries.MAX_PER_PAGE),
) -> dict:
    """Ordenados pelo valor aprovado, do maior para o menor. `detail` traz o que é próprio de cada fonte
    (modalidades e origem do recurso no CNPq; custeio/capital e programa de pós na CAPES)."""
    _guard(request, response)
    return queries.list_awards(funder=financiador.lower() if financiador else None, year=ano, uf=uf,
                               instrument=instrumento, cnpj=cnpj, q=q, page=page, per_page=per_page)


@instituicoes.get("/{cnpj}/investimentos", summary="Investimentos recebidos por uma instituição")
def institution(cnpj: str, request: Request, response: Response) -> dict:
    """Por financiador e por ano, para uma instituição do cadastro do PNIPE (CNPJ com os 14 números)."""
    _guard(request, response)
    data = queries.institution_investments(cnpj)
    if data is None:
        raise HTTPException(404, "Instituição não encontrada no cadastro do PNIPE.")
    return data


app.include_router(investimentos)
app.include_router(instituicoes)
for _r in ROUTERS:
    app.include_router(_r)
app.include_router(elos_router)
for _r in (*OBRAS, *AUXILIOS, *PERFIS):
    app.include_router(_r)
