"""Tipos e utilitários compartilhados pelas fontes do pipeline."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any


def norm_key(s: object) -> str:
    """Minúsculo, sem acento e sem nada que não seja letra ou número ("Nº Parcela" -> "noparcela")."""
    n = unicodedata.normalize("NFKD", str(s or "")).encode("ASCII", "ignore").decode("ASCII")
    return re.sub(r"[^a-z0-9]", "", n.lower())


def clean_cnpj(value: object) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits.zfill(14) if digits else None


def clean_name(value: object) -> str:
    """Nome normalizado para casamento: sem acento, sem pontuação, minúsculo, espaços simples."""
    if not value:
        return ""
    n = unicodedata.normalize("NFKD", str(value)).encode("ASCII", "ignore").decode("ASCII")
    n = re.sub(r"[^a-zA-Z0-9\s]", "", n)
    return " ".join(n.lower().split())


def text_or_none(v: object) -> str | None:
    if v is None:
        return None
    s = " ".join(str(v).split())
    return s or None


# Colunas de um award, na ordem da tabela funding_award.
AWARD_FIELDS = (
    "source_key", "ref", "title", "instrument", "program", "proponent_cnpj", "proponent_name",
    "executor_cnpj", "executor_name", "city", "uf", "signing_date", "end_date", "approved_value",
    "paid_value", "status", "extra",
)


@dataclass
class SourceData:
    """O que cada fonte entrega ao build: metadados, awards e pagamentos.

    `awards` é indexado por `source_key`; cada pagamento referencia o seu award
    pela mesma chave. `link_mode` diz como casar com o PNIPE: "cnpj" (FINEP:
    proponente e executor, com CNPJ) ou "name" (CAPES/CNPq: só o nome, e a sigla
    em `extra.sigla` quando houver).
    """

    id: str
    name: str
    urls: list[str]
    notes: str
    sort_order: int
    link_mode: str
    as_of: date | None = None
    awards: dict[str, dict[str, Any]] = field(default_factory=dict)
    disbursements: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
