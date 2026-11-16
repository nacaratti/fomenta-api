"""FINEP: planilhas da Central de Downloads (URL fixa, atualização semanal).

- `Contratacao.xlsx`: um projeto por linha, com CNPJ do executor, valor Finep e
  valor pago. É a base dos awards.
- `Liberacao.xlsx`: uma linha por liberação (parcela), com data e valor. É a
  base dos pagamentos; projetos que só aparecem aqui também viram award.

Escopo: apoio direto (Não Reembolsável a ICTs, Crédito Direto, Subvenção
Direta / Não Reembolsável a Empresas e Subvenção via agentes). Ficam de fora o
crédito e a subvenção descentralizados no nível do beneficiário final (repetem
o dinheiro do repasse ao agente), o investimento em startups e a ANCINE.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from pipeline.common import SourceData, clean_cnpj, norm_key, text_or_none

log = logging.getLogger(__name__)

FILES = ("Contratacao.xlsx", "Liberacao.xlsx")
URL = "https://download.finep.gov.br/{name}"
META = {
    "id": "finep",
    "name": "Financiadora de Estudos e Projetos (FINEP)",
    "urls": [
        "https://legacy.finep.gov.br/transparencia-finep/paineis-e-downloads/central-de-downloads",
        "https://download.finep.gov.br/Contratacao.xlsx",
        "https://download.finep.gov.br/Liberacao.xlsx",
    ],
    "notes": (
        "Valores nominais, não corrigidos pela inflação. Apoio direto da FINEP; o crédito e a "
        "subvenção descentralizados aparecem só no repasse ao agente. Localização (UF) conforme a planilha."
    ),
    "sort_order": 0,
    "link_mode": "cnpj",
}
# aba (normalizada) -> instrumento quando a aba não tem coluna "Instrumento"
SHEETS = {
    "projetosnaoreembolsavelicts": "Não Reembolsável a ICTs",
    "projetoscreditodireto": "Crédito Direto",
    "projetossubvencaodireta": None,
    "projetossubvdiretaagentes": "Subvenção Descentralizada",
}
_EXCEL_EPOCH = date(1899, 12, 30)
_POSICAO = re.compile(r"Posi[çc][ãa]o:\s*(\d{2})/(\d{2})/(\d{4})")


def _num(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: object) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _date(v: object) -> date | None:
    d: date | None = None
    if isinstance(v, datetime):
        d = v.date()
    elif isinstance(v, date):
        d = v
    elif isinstance(v, (int, float)) and 20000 < v < 80000:  # serial do Excel (algumas abas vêm assim)
        d = _EXCEL_EPOCH + timedelta(days=int(v))
    if d is None or d.year < 1980:  # datas-lixo do Excel (1899/1900) viram nulo
        return None
    return d


def download(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=300, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for name in FILES:
            log.info("FINEP: baixando %s", name)
            r = c.get(URL.format(name=name))
            r.raise_for_status()
            (dest / name).write_bytes(r.content)


@dataclass
class _Sheet:
    instrument: str | None
    rows: list[dict[str, Any]]
    as_of: date | None


def _read_sheets(path: Path) -> Iterator[_Sheet]:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        for name in wb.sheetnames:
            k = norm_key(name)
            if k not in SHEETS:
                continue
            raw = list(wb[name].iter_rows(values_only=True))
            as_of, header_idx = None, None
            for i, r in enumerate(raw[:15]):
                for c in r:
                    if isinstance(c, str) and (m := _POSICAO.search(c)):
                        as_of = date(int(m[3]), int(m[2]), int(m[1]))
                keys = {norm_key(c) for c in r if c is not None}
                if "contrato" in keys and "titulo" in keys:
                    header_idx = i
                    break
            if header_idx is None:
                raise ValueError(f"{path.name}/{name}: cabeçalho não encontrado")
            headers = [norm_key(c) for c in raw[header_idx]]
            rows = [row for r in raw[header_idx + 1:]
                    if text_or_none((row := {h: v for h, v in zip(headers, r) if h}).get("contrato"))]
            log.info("FINEP %s / %s: %d linhas", path.name, name, len(rows))
            yield _Sheet(SHEETS[k], rows, as_of)
    finally:
        wb.close()


def _award(row: dict[str, Any], instrument: str | None) -> dict[str, Any]:
    uf = text_or_none(row.get("ufprojeto") or row.get("ufexecutor") or row.get("uf") or row.get("ufproponente"))
    return {
        "source_key": text_or_none(row.get("contrato")),
        "ref": text_or_none(row.get("ref")),
        "title": text_or_none(row.get("titulo")),
        "instrument": text_or_none(row.get("instrumento")) or instrument,
        "program": text_or_none(row.get("demanda")),
        "proponent_cnpj": clean_cnpj(row.get("cnpjproponente")),
        "proponent_name": text_or_none(row.get("proponente")),
        "executor_cnpj": clean_cnpj(row.get("cnpjexecutor")),
        "executor_name": text_or_none(row.get("executor")),
        "city": text_or_none(row.get("municipioexecutor") or row.get("municipio") or row.get("municipioproponente")),
        "uf": uf[:2].upper() if uf else None,
        "signing_date": _date(row.get("dataassinatura")),
        "end_date": _date(row.get("prazoexecucao") or row.get("prazoutilizacao")),
        "approved_value": _num(row.get("valorfinep")),
        "paid_value": _num(row.get("valorpago")),
        "status": text_or_none(row.get("status")),
        "extra": None,
    }


def parse(directory: Path) -> SourceData:
    out = SourceData(**META)
    as_ofs: list[date] = []
    for sheet in _read_sheets(directory / "Contratacao.xlsx"):
        as_ofs += [sheet.as_of] if sheet.as_of else []
        for row in sheet.rows:
            a = _award(row, sheet.instrument)
            out.awards.setdefault(a["source_key"], a)

    pending: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    duplicates = 0
    for sheet in _read_sheets(directory / "Liberacao.xlsx"):
        as_ofs += [sheet.as_of] if sheet.as_of else []
        for row in sheet.rows:
            a = _award(row, sheet.instrument)
            existing = out.awards.setdefault(a["source_key"], a)
            for k, v in a.items():  # completa campos que a Contratação não trouxe
                if existing.get(k) is None and v is not None:
                    existing[k] = v
            value, paid_on = _num(row.get("valorliberado")), _date(row.get("dataliberacao"))
            if value is None and paid_on is None:
                continue
            parcel = _int(row.get("noparcela") or row.get("nparcela"))  # "Nº" vira "no"
            release = _int(row.get("noliberacao") or row.get("nliberacao"))
            dup_key = (a["source_key"], parcel, release, paid_on, value)
            if parcel is not None and dup_key in seen:
                # A planilha repete liberações idênticas (mesma parcela, nº, data e
                # valor); contadas duas vezes, o liberado passa do "Valor Pago".
                duplicates += 1
                continue
            seen.add(dup_key)
            pending.append({"source_key": a["source_key"], "paid_on": paid_on, "value": value,
                            "parcel_number": parcel, "release_number": release})

    out.as_of = max(as_ofs) if as_ofs else None
    future = 0
    for d in pending:
        # Liberação com data depois da posição da planilha é erro de digitação na
        # fonte: mantém o valor no total, mas fora da série por ano.
        if out.as_of and d["paid_on"] and d["paid_on"] > out.as_of:
            d["paid_on"] = None
            future += 1
        out.disbursements.append(d)
    out.stats = {"duplicatas_descartadas": duplicates, "datas_futuras": future}
    return out
