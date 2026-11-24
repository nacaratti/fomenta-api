"""Planos de acesso da API: limites por plano, chaves e as rotas /v1/planos e /v1/conta.

As chaves ficam fora do código: a variável de ambiente `FOMENTA_API_KEYS` guarda um JSON com o
SHA-256 de cada chave e os dados dela (`tools/api_keys.py criar` gera a chave e a entrada).

    FOMENTA_API_KEYS='{"<sha256>": {"plano": "pro", "nome": "Universidade X", "expira": "2027-12-31"}}'

Sem chave, a pessoa usa o plano `anonimo`, contado por IP.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class Plano:
    id: str
    nome: str
    descricao: str
    por_minuto: int
    por_dia: int
    max_por_pagina: int
    #: quantos registros uma consulta pode alcançar paginando (página × por página); None = sem limite
    max_registros: int | None
    #: preço mensal em reais; None = sob consulta
    preco_mensal: float | None
    recursos: tuple[str, ...] = field(default_factory=tuple)
    precisa_chave: bool = True


PLANOS: dict[str, Plano] = {
    "anonimo": Plano(
        "anonimo", "Aberto", "Para explorar e para o site. Sem cadastro, contado por IP.",
        por_minuto=60, por_dia=2_000, max_por_pagina=50, max_registros=1_000, preco_mensal=0,
        recursos=("Todos os dados abertos (editais só no site)", "Até 50 itens por página", "Até 1.000 registros por consulta"),
        precisa_chave=False),
    "gratuito": Plano(
        "gratuito", "Pesquisa", "Para pesquisadores, estudantes e projetos sem fins lucrativos.",
        por_minuto=120, por_dia=10_000, max_por_pagina=100, max_registros=10_000, preco_mensal=0,
        recursos=("Chave pessoal", "Até 100 itens por página", "Até 10.000 registros por consulta",
                  "Servidor MCP para IA")),
    "pro": Plano(
        "pro", "Profissional", "Para consultorias, empresas e produtos que usam os dados todo dia.",
        por_minuto=600, por_dia=200_000, max_por_pagina=100, max_registros=None, preco_mensal=None,
        recursos=("Tudo do Pesquisa", "Paginação sem limite de profundidade", "Uso comercial",
                  "Suporte por e-mail")),
    "institucional": Plano(
        "institucional", "Institucional", "Para universidades, NITs, FAPs e órgãos públicos.",
        por_minuto=1_200, por_dia=1_000_000, max_por_pagina=100, max_registros=None, preco_mensal=None,
        recursos=("Tudo do Profissional", "Várias chaves por instituição", "Relatórios sob encomenda",
                  "Contrato e nota fiscal")),
}

#: editais pelo site oficial (sessão do site): mais apertado que o Aberto e fora da lista pública de planos
PLANO_SITE_EDITAIS = Plano(
    "site", "Site", "Editais pelo site oficial do Fomenta BR.",
    por_minuto=30, por_dia=600, max_por_pagina=50, max_registros=200, preco_mensal=None, precisa_chave=False)

KEY_PREFIX = "fmt_"


def key_hash(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


@lru_cache(maxsize=1)
def _keys(raw: str) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def lookup(key: str) -> dict[str, Any] | None:
    """Os dados da chave (plano, nome, expira) ou None se ela não existe."""
    info = _keys(os.environ.get("FOMENTA_API_KEYS", "")).get(key_hash(key))
    if not info or info.get("plano") not in PLANOS:
        return None
    return info


def expired(info: dict[str, Any]) -> bool:
    exp = info.get("expira")
    if not exp:
        return False
    try:
        return date.fromisoformat(exp) < datetime.now(UTC).date()
    except ValueError:
        return True


def public(p: Plano) -> dict[str, Any]:
    d = asdict(p)
    d["recursos"] = list(p.recursos)
    return d
