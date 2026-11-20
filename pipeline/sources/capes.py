"""CAPES: custeio e capital do PROAP e do PROEX (portal de dados abertos, CKAN).

Três conjuntos: 2012–2016, 2017–2021 e 2022 em diante. O de 2022 publica um CSV
por janela de anos, com sobreposição (o arquivo 2023–2025 e o 2024–2025 são
idênticos): lemos todos e deduplicamos por
(ano, programa, IES, PPG, natureza da despesa). Um award por
(ano, programa, IES, PPG), com o valor concedido; PROAP e PROEX são repasses
diretos às IES, então o concedido conta como pago no ano. O CSV não traz CNPJ:
o casamento com o PNIPE é por nome e sigla da IES.

Bolsas ficam de fora: o conjunto de concessão de bolsas traz só quantidade.
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import date
from pathlib import Path

import httpx

from pipeline.common import SourceData

log = logging.getLogger(__name__)

DATASETS = (
    "2022-a-2025-custeio-e-capital-dos-programas-de-fomento-proap-e-proex-da-dpb",
    "2017-a-2021-custeio-e-capital-dos-programas-de-fomento-da-diretoria-de-programas-e-bolsas-no-pais",
    "2012-a-2016-custeio-e-capital-dos-programas-de-fomento-da-diretoria-de-programas-e-bolsas-no-pais",
)
API = "https://dadosabertos.capes.gov.br/api/3/action/package_show"
META = {
    "id": "capes",
    "name": "Coordenação de Aperfeiçoamento de Pessoal de Nível Superior (CAPES)",
    "urls": [*(f"https://dadosabertos.capes.gov.br/dataset/{d}" for d in DATASETS),
             "https://metadados.capes.gov.br/index.php/catalog/303"],
    "notes": (
        "Valores nominais, não corrigidos pela inflação. Só os programas PROAP e PROEX (custeio e "
        "capital repassados às instituições para a pós-graduação), 2012 em diante; as bolsas da "
        "CAPES ainda não entram. O valor concedido é contado como pago no ano da concessão."
    ),
    "sort_order": 1,
    "link_mode": "name",
}
PROGRAMS = {"PROAP": "PROAP (apoio à pós-graduação)", "PROEX": "PROEX (excelência acadêmica)"}
POSITION_FILE = "_posicao.txt"


def money(raw: str) -> float | None:
    """'43.224,00' (arquivos novos) ou '42,976.00' (2022): o último separador com 1–2 casas é o decimal."""
    s = (raw or "").strip().replace(" ", "")
    if not s:
        return None
    last = max(s.rfind(","), s.rfind("."))
    if last >= 0 and len(s) - last - 1 in (1, 2):
        s = re.sub(r"[.,]", "", s[:last]) + "." + s[last + 1:]
    else:
        s = re.sub(r"[.,]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def _read(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    txt = ""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    head = txt.split("\n", 1)[0]
    return list(csv.DictReader(txt.splitlines(), delimiter=";" if head.count(";") > head.count(",") else ","))


def download(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    fetched, modified = {}, []
    with httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for dataset in DATASETS:
            r = c.get(API, params={"id": dataset})
            r.raise_for_status()
            result = r.json()["result"]
            modified.append(result["metadata_modified"][:10])
            csvs = [res for res in result["resources"] if (res.get("format") or "").upper() == "CSV"]
            if not csvs:
                raise RuntimeError(f"CAPES: o conjunto {dataset} não listou nenhum CSV")
            for res in csvs:
                name = res["url"].rsplit("/", 1)[-1]
                log.info("CAPES: baixando %s", name)
                data = c.get(res["url"])
                data.raise_for_status()
                fetched[name] = data.content
    # Só troca os arquivos depois de baixar todos (o conjunto é a união das janelas de anos).
    for old in dest.glob("*.csv"):
        old.unlink()
    for name, content in fetched.items():
        (dest / name).write_bytes(content)
    # Última atualização dos conjuntos no portal = posição dos dados.
    (dest / POSITION_FILE).write_text(max(modified), encoding="utf-8")


def parse(directory: Path) -> SourceData:
    out = SourceData(**META)
    seen: dict[tuple, float] = {}
    meta: dict[str, dict] = {}
    rows_read = duplicates = 0
    for f in sorted(directory.glob("*.csv")):
        for r in _read(f):
            rows_read += 1
            g = lambda k: (r.get(k) or "").strip()  # noqa: E731
            year, prog, ies, ppg, nat = (g("AN_CONCESSAO"), g("SG_PROGRAMA_FOMENTO").upper(), g("CD_IES_CAPES"),
                                         g("CD_PPG"), g("TP_NATUREZA_DESPESA").upper())
            value = money(g("VL_CONCEDIDO"))
            if not (year.isdigit() and prog in PROGRAMS and ies and ppg and value is not None):
                continue
            key = (year, prog, ies, ppg, nat)
            if key in seen:
                duplicates += 1
                continue
            seen[key] = value
            k = f"{year}-{prog}-{ies}-{ppg}"
            meta.setdefault(k, {
                "source_key": k, "ref": None, "title": g("NM_PPG").title() or None,
                "instrument": PROGRAMS[prog], "program": f"{prog} {year}",
                "proponent_cnpj": None, "proponent_name": None, "executor_cnpj": None,
                "executor_name": g("NM_IES_CAPES") or None,
                "city": g("NM_MUNICIPIO_PPG").title() or None,
                "uf": (g("SG_UF_IES") or g("SG_UF"))[:2].upper() or None,
                "signing_date": date(int(year), 1, 1), "end_date": None, "status": "CONCEDIDO",
                "extra": {"sigla": g("SG_IES_CAPES") or None, "cd_ies": ies, "cd_ppg": ppg,
                          "area": g("NM_AREA_CONHECIMENTO") or None, "natureza": {}},
            })
    for (year, prog, ies, ppg, nat), value in seen.items():
        meta[f"{year}-{prog}-{ies}-{ppg}"]["extra"]["natureza"][(nat or "outro").lower()] = value
    for k, a in meta.items():
        total = round(sum(a["extra"]["natureza"].values()), 2)
        a["approved_value"] = a["paid_value"] = total
        out.awards[k] = a
        out.disbursements.append({"source_key": k, "paid_on": a["signing_date"], "value": total,
                                  "parcel_number": None, "release_number": None})
    pos = directory / POSITION_FILE
    if pos.exists():
        out.as_of = date.fromisoformat(pos.read_text(encoding="utf-8").strip())
    out.stats = {"linhas_lidas": rows_read, "duplicatas_descartadas": duplicates}
    return out
