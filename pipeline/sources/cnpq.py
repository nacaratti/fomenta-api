"""CNPq: bolsas e auxílios, a partir do Painel de Fomento em CT&I.

O painel (http://bi.cnpq.br/painel/v2/p-fomento-cti.html) é um workbook do
Tableau Public publicado pelo CNPq, e o arquivo do workbook (~27 MB, `.twbx`)
traz o extrato inteiro, de 2005 ao ano corrente: uma linha por combinação de
beneficiário (sexo, cor, naturalidade...) × área × modalidade × instituição.
Nada disso entra no repositório:

    pip install tableauhyperapi            # só para este comando, não no build
    python -m pipeline.sources.cnpq painel  # baixa o workbook e grava todos os anos

grava `data/raw/cnpq/<ano>.csv` só com o que interessa (ano, instituição, UF,
linha de fomento, modalidade, origem do recurso, valor), agregado. Os dados
pessoais dos beneficiários são descartados, e a soma de cada ano é conferida
com o total do extrato. `--anos 2024-2026` grava só esses anos; `--arquivo`
usa um `.twbx` já baixado.

O painel também exporta um ano por vez em UTF-16 com tabulação (~68 MB), com
uma linha final "Total"; `python -m pipeline.sources.cnpq reduce <export.csv>`
reduz esse arquivo da mesma forma e confere a soma com a linha "Total".

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
import tempfile
import unicodedata
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

import httpx

from pipeline.common import SourceData

log = logging.getLogger(__name__)

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "cnpq"
META = {
    "id": "cnpq",
    "name": "Conselho Nacional de Desenvolvimento Científico e Tecnológico (CNPq)",
    "urls": ["http://bi.cnpq.br/painel/v2/p-fomento-cti.html",
             "https://public.tableau.com/app/profile/luiz.eduardo.costa8085/viz/"
             "PaineldeFomentoemCinciaTecnologiaeInovaoNovo/Painel",
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
WORKBOOK_URL = "https://public.tableau.com/workbooks/PaineldeFomentoemCinciaTecnologiaeInovaoNovo.twb"
# Colunas do extrato do workbook com o mesmo conteúdo das colunas do export (EXPORT_COLS).
HYPER_QUERY = """
    SELECT extract(year FROM "NRO_ANO")::int, max("DATA_HORA_CONSULTA")::text, "NME_INST_MACRO", "NME_UF",
           "NME_LINHA_FOMENTO", "NME_MODAL_ESTAT", "NME_ORIGEM_RECURSO", sum("VLR_TOTAL")
    FROM "Extract"."Extract" GROUP BY 1, 3, 4, 5, 6, 7"""
HYPER_TOTALS = 'SELECT extract(year FROM "NRO_ANO")::int, sum("VLR_TOTAL") FROM "Extract"."Extract" GROUP BY 1'


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
    dest = _write_year(out_dir, year, max(extracted) if extracted else "", agg)
    log.info("CNPq %s: %d linhas -> %d agregadas, R$ %.2f (Total declarado %s)",
             year, len(rows) - 2, len(agg), total, total_declared)
    return dest


def _write_year(out_dir: Path, year: int | str, extracted: str, agg: dict[tuple, float]) -> Path:
    """Grava `<ano>.csv` com uma linha por (ano, instituição, UF, linha, modalidade, origem)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{year}.csv"
    with dest.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(REDUCED_COLS)
        for (ano, inst, uf, linha, mod, orig), v in sorted(agg.items()):
            if round(v, 2) != 0:
                w.writerow([ano, extracted, inst, uf, linha, mod, orig, f"{v:.2f}"])
    return dest


def _bare(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()


_UF_BARE = {_bare(k): v for k, v in UF.items()}
# Rótulos do extrato que o export do painel mostra de outro jeito (conferido com o export de 2026).
EXPORT_LABELS = {"origem": {"PI SEM USO": "OUTRAS"},
                 "modalidade": {"Doutorado Sanduíche": "Doutorado Sanduíche no País"}}


def reduce_rows(rows, totals: dict[int, float], out_dir: Path = RAW_DIR,
                years: set[int] | None = None) -> list[Path]:
    """Linhas do extrato do painel, já somadas: (ano, data da consulta, instituição, UF por extenso
    com ou sem acento, linha, modalidade, origem, valor) -> um `<ano>.csv` por ano. Confere a soma
    de cada ano com `totals` (o total do extrato) e falha se não bater."""
    agg: dict[int, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
    extracted: dict[int, str] = {}
    for year, consulted, inst, uf, line, mod, orig, value in rows:
        year = int(year)
        if years and year not in years:
            continue
        key = (str(year), (inst or "").strip(), _UF_BARE.get(_bare(uf), ""), (line or "").strip(),
               EXPORT_LABELS["modalidade"].get((mod or "").strip(), (mod or "").strip()),
               EXPORT_LABELS["origem"].get((orig or "").strip(), (orig or "").strip()))
        agg[year][key] += value or 0.0
        extracted[year] = max(extracted.get(year, ""), (consulted or "")[:10])
    out = []
    for year in sorted(agg):
        total = sum(agg[year].values())
        if abs(total - totals[year]) > 1:
            raise ValueError(f"{year}: soma {total:.2f} diferente do total do extrato {totals[year]:.2f}")
        out.append(_write_year(out_dir, year, extracted[year], agg[year]))
        log.info("CNPq %s: %d linhas agregadas, R$ %.2f", year, len(agg[year]), total)
    return out


def reduce_workbook(path: Path, out_dir: Path = RAW_DIR, years: set[int] | None = None) -> list[Path]:
    """Arquivo do workbook do painel (`.twbx`) -> um `<ano>.csv` por ano. Precisa do tableauhyperapi."""
    try:
        from tableauhyperapi import Connection, HyperProcess, Telemetry
    except ImportError as e:
        raise SystemExit("Instale o leitor do extrato do Tableau: pip install tableauhyperapi") from e
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(path) as z:
        extracts = [i for i in z.infolist() if i.filename.endswith(".hyper")]
        if not extracts:
            raise ValueError(f"{path}: o workbook não traz extrato .hyper")
        hyper = Path(tmp) / "extrato.hyper"
        # O maior é o extrato; os outros são temporários quase vazios do Tableau.
        hyper.write_bytes(z.read(max(extracts, key=lambda i: i.file_size)))
        with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp, \
                Connection(hp.endpoint, str(hyper)) as con:
            totals = {int(y): v for y, v in con.execute_list_query(HYPER_TOTALS)}
            rows = con.execute_list_query(HYPER_QUERY)
    return reduce_rows(rows, totals, out_dir, years)


def download_workbook(dest: Path) -> Path:
    with httpx.Client(timeout=300, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        r = c.get(WORKBOOK_URL)
        r.raise_for_status()
    dest.write_bytes(r.content)
    log.info("CNPq: workbook do painel baixado (%.1f MB)", len(r.content) / 1e6)
    return dest


def parse_years(spec: str | None) -> set[int] | None:
    """"2026" ou "2005-2025" -> conjunto de anos; vazio -> None (todos)."""
    if not spec:
        return None
    a, _, b = spec.partition("-")
    return set(range(int(a), int(b or a) + 1))


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
    w = sub.add_parser("painel", help="lê o workbook do painel e grava data/raw/cnpq/<ano>.csv de todos os anos")
    w.add_argument("--arquivo", type=Path, help="workbook (.twbx) já baixado; sem ele, baixa do Tableau Public")
    w.add_argument("--anos", help="só estes anos, ex.: 2026 ou 2005-2025")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.cmd == "painel":
        with tempfile.TemporaryDirectory() as tmp:
            wb = args.arquivo or download_workbook(Path(tmp) / "painel.twbx")
            for f in reduce_workbook(wb, years=parse_years(args.anos)):
                print(f)
        return
    for f in args.files:
        print(reduce_export(f))


if __name__ == "__main__":
    sys.exit(main())
