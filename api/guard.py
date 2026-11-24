"""Acesso comum a todas as rotas: identifica o cliente (chave ou IP), aplica o plano e o cache.

Proteção contra raspagem, em camadas:
- limite por minuto e por dia, por chave ou por IP, em contadores compartilhados (`api/contador.py`);
- sem chave, no máximo 50 itens por página e 1.000 registros alcançáveis paginando: dá para
  consultar tudo com filtros, mas não para baixar a base inteira página a página (quem quer a base
  completa tem os arquivos das Releases);
- só respostas que qualquer anônimo poderia pedir ficam no cache público da CDN; as que exigem
  chave saem com `no-store`, para a CDN não entregá-las a quem não tem chave.
A regra de limite por IP no Firewall da Vercel (ver README) barra as rajadas antes da função.

Editais (por enquanto fora da API pública): só com uma chave que tenha a permissão `editais`, ou pelo
site oficial, com o token de sessão (`api/sessao.py`), limites próprios por IP e sem cache.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, Response

from api.contador import counter
from api import sessao
from api.planos import KEY_PREFIX, PLANO_SITE_EDITAIS, PLANOS, Plano, expired, key_hash, lookup

CACHE = "public, s-maxage=86400, stale-while-revalidate=604800"  # o dado só muda a cada deploy
NO_STORE = "private, no-store"
#: parâmetros que pedem "quantos itens" fora de `per_page`
SIZE_PARAMS = ("per_page", "limite", "coautores")
PLANOS_URL = "/v1/planos"
#: rotas com dados de editais, fechadas para a API pública
PROTECTED = re.compile(r"^/v1/(editais(/.*)?|elos/(pesquisadores|laboratorios)/[^/]+/editais|newsletter/edicoes/[^/]+)/?$")
SESSION_HEADER = "x-fomenta-sessao"
NO_INDEX = "noindex, nofollow, noarchive"


@dataclass(frozen=True)
class Cliente:
    id: str
    plano: Plano
    nome: str | None = None
    #: pode ver editais (chave com a permissão, ou o site com sessão válida)
    editais: bool = False


def client_ip(request: Request) -> str:
    return ((request.headers.get("x-real-ip") or "").strip()
            or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def api_key(request: Request) -> str | None:
    key = (request.headers.get("x-api-key") or "").strip()
    if not key:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key or None


def identify(request: Request) -> Cliente:
    key = api_key(request)
    if key is None:
        return Cliente(f"ip:{client_ip(request)}", PLANOS["anonimo"])
    info = lookup(key) if key.startswith(KEY_PREFIX) else None
    if info is None:
        raise HTTPException(401, f"Chave de API inválida. Confira a chave ou veja como pedir uma em {PLANOS_URL}.",
                            headers={"Cache-Control": NO_STORE, "WWW-Authenticate": "Bearer"})
    if expired(info):
        raise HTTPException(401, "Chave de API expirada. Fale conosco para renovar.",
                            headers={"Cache-Control": NO_STORE, "WWW-Authenticate": "Bearer"})
    return Cliente(f"key:{key_hash(key)[:16]}", PLANOS[info["plano"]], info.get("nome"), bool(info.get("editais")))


def is_protected(path: str) -> bool:
    return bool(PROTECTED.match(path))


def editais_access(request: Request, cliente: Cliente) -> Cliente:
    """Libera rotas de editais para chave com permissão ou para o site com sessão válida; senão, 403."""
    if cliente.editais:
        return cliente
    ip = client_ip(request)
    if sessao.verify(request.headers.get(SESSION_HEADER), ip):
        return Cliente(f"site:{ip}", PLANO_SITE_EDITAIS, editais=True)
    raise HTTPException(403, "Os editais estão disponíveis só no site do Fomenta BR por enquanto, e não pela API "
                             "pública. Para uso institucional, fale conosco.",
                        headers={"Cache-Control": NO_STORE, "X-Robots-Tag": NO_INDEX, "X-Fomenta-Motivo": "sessao"})


def _int(v: object, default: int) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def check_scope(plano: Plano, *, page: int = 1, per_page: int = 25, sizes: dict[str, int] | None = None) -> None:
    """Recusa (403) páginas grandes demais ou fundas demais para o plano."""
    for name, size in {"per_page": per_page, **(sizes or {})}.items():
        if size > plano.max_por_pagina:
            raise HTTPException(403, f"O plano {plano.nome} permite até {plano.max_por_pagina} itens por consulta "
                                     f"(`{name}`). Com uma chave o limite sobe; veja {PLANOS_URL}.",
                                headers={"Cache-Control": NO_STORE})
    if plano.max_registros is not None and page * per_page > plano.max_registros:
        raise HTTPException(403, f"O plano {plano.nome} alcança até {plano.max_registros:,} registros por consulta "
                                 "(página × itens por página).".replace(",", ".") +
                                 " Refine com filtros, baixe a base completa nas Releases do repositório, "
                                 f"ou use uma chave ({PLANOS_URL}).",
                            headers={"Cache-Control": NO_STORE})


def _fits_anonymous(page: int, per_page: int, sizes: dict[str, int]) -> bool:
    try:
        check_scope(PLANOS["anonimo"], page=page, per_page=per_page, sizes=sizes)
        return True
    except HTTPException:
        return False


def count(cliente: Cliente) -> dict[str, str]:
    """Conta a requisição; 429 se passou do minuto ou do dia. Devolve os cabeçalhos de limite."""
    now = datetime.now(UTC)
    minute = int(now.timestamp() // 60)
    day = now.strftime("%Y%m%d")
    p = cliente.plano
    used_min = counter.hit(f"rl:{cliente.id}:m:{minute}", 70)
    used_day = counter.hit(f"rl:{cliente.id}:d:{day}", 90_000)
    reset_min = (minute + 1) * 60
    tomorrow = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    if used_day > p.por_dia:
        wait = int((tomorrow - now).total_seconds()) + 1
        raise HTTPException(429, f"Cota diária do plano {p.nome} esgotada ({p.por_dia:,} consultas).".replace(",", ".") +
                                 f" Volta à meia-noite (UTC). Para mais, veja {PLANOS_URL}.",
                            headers={"Retry-After": str(wait), "Cache-Control": NO_STORE})
    if used_min > p.por_minuto:
        wait = max(1, reset_min - int(now.timestamp()))
        raise HTTPException(429, f"Muitas consultas por minuto para o plano {p.nome} ({p.por_minuto}/min). "
                                 f"Tente de novo em {wait} s.",
                            headers={"Retry-After": str(wait), "Cache-Control": NO_STORE})
    return {
        "X-Plano": p.id,
        "X-RateLimit-Limit": str(p.por_minuto),
        "X-RateLimit-Remaining": str(max(0, p.por_minuto - used_min)),
        "X-RateLimit-Reset": str(reset_min),
        "X-RateLimit-Limit-Day": str(p.por_dia),
        "X-RateLimit-Remaining-Day": str(max(0, p.por_dia - used_day)),
    }


def guard(request: Request, response: Response) -> Cliente:
    cliente = identify(request)
    protected = is_protected(request.url.path)
    if protected:
        cliente = editais_access(request, cliente)
    qp = request.query_params
    page, per_page = _int(qp.get("page"), 1), _int(qp.get("per_page"), 25)
    sizes = {k: _int(qp.get(k), 0) for k in SIZE_PARAMS[1:] if qp.get(k) is not None}
    check_scope(cliente.plano, page=page, per_page=per_page, sizes=sizes)
    response.headers.update(count(cliente))
    # A CDN guarda a resposta pelo endereço, sem olhar a chave: só o que um anônimo pode pedir vai para o cache.
    # Editais nunca: a resposta em cache passaria por cima da checagem de sessão.
    public = not protected and _fits_anonymous(page, per_page, sizes)
    response.headers["Cache-Control"] = CACHE if public else NO_STORE
    if protected:
        response.headers["X-Robots-Tag"] = NO_INDEX
    request.state.cliente = cliente
    return cliente
