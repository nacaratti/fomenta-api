"""CNPq: bolsas e auxílios, a partir do Painel de Fomento em CT&I.

O painel (http://bi.cnpq.br/painel/v2/p-fomento-cti.html) exporta um ano por
vez em UTF-16 com tabulação (~68 MB), uma linha por combinação de beneficiário
(sexo, cor, naturalidade...) × área × modalidade × instituição, e uma linha
final "Total". Esse arquivo NÃO entra no repositório:

    python -m pipeline.sources.cnpq reduce <export.csv>

grava `data/raw/cnpq/<ano>.csv` só com o que interessa (ano, instituição, UF,
linha de fomento, modalidade, origem do recurso, valor), agregado. Os dados
pessoais dos beneficiários são descartados. O `reduce` confere a soma com a
linha "Total" e falha se não bater.

No build, um award por (ano, instituição, linha de fomento), com o detalhe por
modalidade e origem em `extra`. Sem CNPJ: casamento com o PNIPE só por nome.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from pipeline.common import SourceData

log = logging.getLogger(__name__)

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "cnpq"
META = {
    "id": "cnpq",
    "name": "Conselho Nacional de Desenvolvimento Científico e Tecnológico (CNPq)",
    "urls": ["http://bi.cnpq.br/painel/v2/p-fomento-cti.html",
             "https://www.gov.br/cnpq/pt-br/acesso-a-informacao/dados-abertos/paineis-de-dados"],
    "notes": (
        "Valores nominais, não corrigidos pela inflação: bolsas e auxílios pagos no ano, segundo o Painel "
        "de Fomento do CNPq (o ano corrente é parcial, até a data da extração). Inclui recursos do FNDCT e "
        "de outras origens. Parte do FNDCT também passa pela FINEP: não some o CNPq com a FINEP."
    ),
    "sort_order": 2,
    "link_mode": "name",
}
UF = {
    "Acre": "AC", "Alagoas": "AL", "Amapá": "AP", "Amazonas": "AM", "Bahia": "BA", "Ceará": "CE",
    "Distrito Federal": "DF", "Espírito Santo": "ES", "Goiás": "GO", "Maranhão": "MA", "Mato Grosso": "MT",
    "Mato Grosso do Sul": "MS", "Minas Gerais": "MG", "Paraná": "PR", "Paraíba": "PB", "Pará": "PA",
    "Pernambuco": "PE", "Piauí": "PI", "Rio Grande do Norte": "RN", "Rio Grande do Sul": "RS",
    "Rio de Janeiro": "RJ", "Rondônia": "RO", "Roraima": "RR", "Santa Catarina": "SC", "Sergipe": "SE",
    "São Paulo": "SP", "Tocantins": "TO",
}
EXPORT_COLS = {
    "ano": "Ano de Exercício", "instituicao": "Instituição", "uf": "Unidade Federação",
    "linha": "Linha de Fomento", "modalidade": "Modalidade", "origem": "Origem do Recurso",
    "extracao": "Data Extração BD PICC", "valor": "Valor em Real (R$)",
}
REDUCED_COLS = ["ano", "data_extracao", "instituicao", "uf", "linha", "modalidade", "origem", "valor"]


def _money_br(raw: str) -> float:
    s = (raw or "").replace("R$", "").strip().replace(".", "").replace(",", ".")
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def reduce_export(path: Path, out_dir: Path = RAW_DIR) -> Path:
    """Export do painel -> CSV pequeno e sem dados pessoais. Devolve o arquivo gravado."""
    rows = list(csv.reader(io.StringIO(_decode(path.read_bytes())), delimiter="\t"))
    ix = {c.strip(): i for i, c in enumerate(rows[0])}
    missing = [c for c in EXPORT_COLS.values() if c not in ix]
    if missing:
        raise ValueError(f"colunas ausentes no export: {missing}")
    agg: dict[tuple, float] = defaultdict(float)
    years, extracted, total_declared, total = set(), set(), None, 0.0
    for r in rows[1:]:
        if not r:
            continue
        g = lambda k: (r[ix[EXPORT_COLS[k]]] or "").strip()  # noqa: E731
        if not g("ano").isdigit():  # linha "Total"
            total_declared = _money_br(g("valor"))
            continue
        v = _money_br(g("valor"))
        total += v
        years.add(int(g("ano")))
        if m := re.match(r"(\d{2})/(\d{2})/(\d{4})", g("extracao")):
            extracted.add(f"{m[3]}-{m[2]}-{m[1]}")
        agg[(g("ano"), g("instituicao"), UF.get(g("uf"), ""), g("linha"), g("modalidade"), g("origem"))] += v
    if len(years) != 1:
        raise ValueError(f"o export deve ter um ano só; tem {sorted(years)}")
    if total_declared is not None and abs(total - total_declared) > 1:
        raise ValueError(f"soma {total:.2f} diferente da linha Total {total_declared:.2f}")
    year = years.pop()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{year}.csv"
    with dest.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(REDUCED_COLS)
        for (ano, inst, uf, linha, mod, orig), v in sorted(agg.items()):
            w.writerow([ano, max(extracted) if extracted else "", inst, uf, linha, mod, orig, f"{v:.2f}"])
    log.info("CNPq %s: %d linhas -> %d agregadas, R$ %.2f (Total declarado %s)",
             year, len(rows) - 2, len(agg), total, total_declared)
    return dest


def _sentence(s: str) -> str:
    s = " ".join((s or "").split()).lower()
    return s[:1].upper() + s[1:]


def parse(directory: Path = RAW_DIR) -> SourceData:
    out = SourceData(**META)
    groups: dict[tuple, dict] = {}
    as_of: date | None = None
    for f in sorted(directory.glob("*.csv")):
        with f.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                year, v = int(r["ano"]), float(r["valor"])
                if r["data_extracao"]:
                    d = date.fromisoformat(r["data_extracao"])
                    as_of = max(as_of, d) if as_of else d
                a = groups.setdefault((year, r["linha"], r["instituicao"]),
                                      {"value": 0.0, "mod": defaultdict(float), "orig": defaultdict(float),
                                       "uf": defaultdict(float)})
                a["value"] += v
                a["mod"][r["modalidade"]] += v
                a["orig"][r["origem"]] += v
                a["uf"][r["uf"]] += v
    for (year, line, inst), a in groups.items():
        total = round(a["value"], 2)
        if total <= 0:
            continue
        key = f"{year}-{hashlib.md5(f'{line}|{inst}'.encode()).hexdigest()[:16]}"
        uf = max(a["uf"].items(), key=lambda kv: kv[1])[0] or None
        out.awards[key] = {
            "source_key": key, "ref": None, "title": None, "instrument": _sentence(line) or None, "program": None,
            "proponent_cnpj": None, "proponent_name": None, "executor_cnpj": None, "executor_name": inst or None,
            "city": None, "uf": uf, "signing_date": date(year, 1, 1), "end_date": None,
            "approved_value": total, "paid_value": total, "status": "PAGO NO ANO",
            "extra": {"modalidades": {k: round(v, 2) for k, v in sorted(a["mod"].items(), key=lambda kv: -kv[1])},
                      "origem": {k: round(v, 2) for k, v in a["orig"].items()}},
        }
        out.disbursements.append({"source_key": key, "paid_on": date(year, 1, 1), "value": total,
                                  "parcel_number": None, "release_number": None})
    out.as_of = as_of
    out.stats = {"anos": sorted({k[0] for k in groups})}
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Ferramentas do CNPq")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reduce", help="reduz um export anual do Painel de Fomento para data/raw/cnpq/<ano>.csv")
    r.add_argument("files", nargs="+", type=Path)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for f in args.files:
        print(reduce_export(f))


if __name__ == "__main__":
    sys.exit(main())
