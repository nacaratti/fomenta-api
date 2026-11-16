"""Exporta do banco do FomentaBR o snapshot do PNIPE usado no casamento de instituições.

Só leitura. Rode quando o cadastro do PNIPE mudar (depois de uma coleta) e
faça commit dos CSVs em `data/ref/`:

    DATABASE_URL=postgresql://... python tools/export_pnipe_ref.py

Grava só o necessário: instituições (CNPJ, razão social, sigla, UF, cidade) e
laboratórios (nome, sigla, CNPJ da instituição) — nada de contato ou endereço.
"""
from __future__ import annotations

import asyncio
import csv
import os
import re
import sys
from pathlib import Path

REF_DIR = Path(__file__).resolve().parents[1] / "data" / "ref"


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("Defina DATABASE_URL (o mesmo do FomentaBR).")
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


async def main() -> None:
    import asyncpg

    conn = await asyncpg.connect(_dsn())
    try:
        insts = await conn.fetch(
            "SELECT cnpj, social_reason, initials, state, city FROM pnipe_institution ORDER BY cnpj")
        labs = await conn.fetch(
            "SELECT name, initials, institution_cnpj FROM pnipe_laboratory "
            "WHERE institution_cnpj IS NOT NULL AND name NOT LIKE '%(Placeholder)%' ORDER BY id")
    finally:
        await conn.close()
    REF_DIR.mkdir(parents=True, exist_ok=True)
    with (REF_DIR / "pnipe_institutions.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cnpj", "social_reason", "initials", "state", "city"])
        w.writerows([r["cnpj"], r["social_reason"] or "", r["initials"] or "", r["state"] or "", r["city"] or ""]
                    for r in insts)
    with (REF_DIR / "pnipe_labs.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "initials", "institution_cnpj"])
        w.writerows([r["name"] or "", r["initials"] or "", r["institution_cnpj"]] for r in labs)
    print(f"{len(insts)} instituições, {len(labs)} laboratórios em {REF_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
