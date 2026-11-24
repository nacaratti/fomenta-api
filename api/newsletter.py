"""Arquivo da newsletter do FomentaBR: as edições já enviadas por e-mail.

Vêm do retrato do banco do FomentaBR (`newsletter_edicao` em `tools/export_supabase_snapshot.py`).
A lista é pública; o conteúdo de cada edição traz editais, então segue a mesma regra deles: só
pelo site (sessão) ou com chave que tenha a permissão `editais` (ver `api/guard.py`).
A inscrição continua no FomentaBR (`POST /api/subscribe`, com confirmação por e-mail).
"""
from __future__ import annotations

import html as htmllib
import json
import re
from typing import Any

import duckdb
from fastapi import APIRouter, HTTPException, Request, Response

from api.guard import guard
from api.plataforma import _all, _one

router = APIRouter(prefix="/v1/newsletter", tags=["Newsletter"])
NOTE = "Edições da newsletter mensal do FomentaBR já enviadas. Para assinar, use o site do FomentaBR."
_MD = re.compile(r"(!?\[([^\]]*)\]\([^)]*\))|[#*_>`|~]+")


def _resumo(md: str | None, n: int = 220) -> str:
    """Primeiras frases do texto da edição, sem a marcação."""
    if not md:
        return ""
    text = _MD.sub(lambda m: m.group(2) or " ", md)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


_HEAD = re.compile(r"<(style|head|script)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
#: cabeçalho fixo do e-mail ("FomentaBR · Newsletter mensal Edição de 05/11/2026")
_TOPO = re.compile(r"^.*?Edi[çc][ãa]o de \d{2}/\d{2}/\d{4}\s*", re.S)


def _resumo_html(html: str | None, n: int = 220) -> str:
    """Abertura da edição como foi enviada (o texto-rascunho às vezes traz o marcador "escreva aqui")."""
    if not html:
        return ""
    text = " ".join(htmllib.unescape(_TAG.sub(" ", _HEAD.sub(" ", html))).split())
    text = _TOPO.sub("", text, count=1)
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _count(v: Any) -> int:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return 0
    return len(v) if isinstance(v, (list, tuple)) else 0


def _rows(sql: str, params: list[Any]) -> list[tuple]:
    try:
        return _all(sql, params)
    except duckdb.CatalogException:  # retrato ainda sem a tabela da newsletter
        return []


@router.get("/edicoes", summary="Edições anteriores da newsletter")
def editions(request: Request, response: Response) -> dict:
    """Das mais recentes para as mais antigas: data, assunto, um resumo e quantos editais e congressos."""
    guard(request, response)
    rows = _rows("SELECT id, edition_date, subject, markdown, call_ids, conference_ids, html FROM newsletter_edicao "
                 "ORDER BY edition_date DESC, id DESC", [])
    return {"items": [{"id": r[0], "data": r[1].isoformat() if r[1] else None, "assunto": r[2],
                       "resumo": _resumo_html(r[6]) or _resumo(r[3]), "editais": _count(r[4]),
                       "congressos": _count(r[5])} for r in rows],
            "nota": NOTE}


@router.get("/edicoes/{edition_id}", summary="Uma edição da newsletter")
def edition(edition_id: int, request: Request, response: Response) -> dict:
    guard(request, response)  # rota protegida como os editais (ver PROTECTED em api/guard.py)
    try:
        r = _one("SELECT id, edition_date, subject, html, markdown FROM newsletter_edicao WHERE id = ?", [edition_id])
    except duckdb.CatalogException:
        r = None
    if not r:
        raise HTTPException(404, "Edição não encontrada.")
    return {"id": r[0], "data": r[1].isoformat() if r[1] else None, "assunto": r[2], "html": r[3], "markdown": r[4],
            "nota": NOTE}
