"""Limite por IP e Cache-Control, comuns a todas as rotas."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response

CACHE = "public, s-maxage=86400, stale-while-revalidate=604800"  # o dado só muda a cada deploy
RATE_LIMIT = 120  # requisições por minuto, por IP e por instância (defesa leve)

_hits: dict[str, list[datetime]] = defaultdict(list)


def client_ip(request: Request) -> str:
    return ((request.headers.get("x-real-ip") or "").strip()
            or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def guard(request: Request, response: Response) -> None:
    ip, now = client_ip(request), datetime.now(timezone.utc)
    recent = [t for t in _hits[ip] if t >= now - timedelta(minutes=1)]
    if len(recent) >= RATE_LIMIT:
        _hits[ip] = recent
        raise HTTPException(429, "Muitas requisições. Tente de novo em alguns instantes.", headers={"Retry-After": "60"})
    _hits[ip] = [*recent, now]
    response.headers["Cache-Control"] = CACHE
