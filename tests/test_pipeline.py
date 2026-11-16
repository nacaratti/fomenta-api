from datetime import date
from pathlib import Path

import openpyxl
import pytest

from pipeline.matching import InstitutionMatcher
from pipeline.sources import capes, cnpq, finep


@pytest.mark.parametrize("raw,expected", [
    ("43.224,00", 43224.0), ("42,976.00", 42976.0), ("1.500", 1500.0), ("10,5", 10.5), ("", None),
])
def test_capes_money_formats(raw, expected):
    assert capes.money(raw) == expected


def _cnpq_export(path: Path, total: str) -> Path:
    cols = list(cnpq.EXPORT_COLS.values()) + ["Sexo"]
    rows = [
        ["2025", "Universidade de Sao Paulo", "São Paulo", "BOLSAS DE FORMAÇÃO E DE PESQUISADORES", "Doutorado",
         "CNPq", "11/05/2026", "R$ 1.000,50", "Feminino"],
        ["2025", "Universidade de Sao Paulo", "São Paulo", "BOLSAS DE FORMAÇÃO E DE PESQUISADORES", "Doutorado",
         "CNPq", "11/05/2026", "R$ 999,50", "Masculino"],
        ["Total"] * 7 + [total, "Total"],
    ]
    text = "\n".join("\t".join(r) for r in [cols, *rows])
    path.write_bytes(text.encode("utf-16"))
    return path


def test_cnpq_reduce_aggregates_and_drops_personal_columns(tmp_path):
    out = cnpq.reduce_export(_cnpq_export(tmp_path / "x.csv", "R$ 2.000,00"), tmp_path / "raw")
    content = out.read_text(encoding="utf-8")
    assert out.name == "2025.csv"
    assert "Feminino" not in content and "Masculino" not in content
    assert content.strip().splitlines()[1].endswith(",2000.00")
    data = cnpq.parse(tmp_path / "raw")
    (award,) = data.awards.values()
    assert award["approved_value"] == 2000.0 and award["uf"] == "SP"
    assert data.as_of == date(2026, 5, 11)


def test_cnpq_reduce_rejects_total_mismatch(tmp_path):
    with pytest.raises(ValueError, match="Total"):
        cnpq.reduce_export(_cnpq_export(tmp_path / "x.csv", "R$ 9.999,00"), tmp_path / "raw")


def _finep_sheet(wb, name, header, rows):
    ws = wb.create_sheet(name)
    ws.append([])
    ws.append(["Posição: 23/10/2026"])
    ws.append(header)
    for r in rows:
        ws.append(r)


def test_finep_drops_duplicate_releases_and_future_dates(tmp_path):
    head = ["Demanda", "Ref", "Contrato", "Data Assinatura", "Título", "Proponente", "CNPJ Proponente",
            "Executor", "UF", "Valor Finep", "Status"]
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    _finep_sheet(wb, "Projetos_Não_Reembolsavel_ICTs", head + ["CNPJ Executor", "Valor Pago"],
                 [["D", "1/24", "01.24.0001.00", date(2024, 1, 2), "T", "P", "11.111.111/0001-11", "E", "SP", 100, "OK",
                   "22.222.222/0001-22", 60]])
    wb.save(tmp_path / "Contratacao.xlsx")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    rel = head + ["Nº Parcela", "Nº Liberação", "Data Liberação", "Valor Liberado"]
    base = ["D", "1/24", "01.24.0001.00", date(2024, 1, 2), "T", "P", "11.111.111/0001-11", "E", "SP", 100, "OK"]
    _finep_sheet(wb, "Projetos Não Reembolsavel ICTs", rel, [
        base + [1, 1, date(2024, 2, 1), 30],
        base + [1, 1, date(2024, 2, 1), 30],   # duplicata exata
        base + [2, 1, date(2027, 1, 1), 30],   # data depois da posição
    ])
    wb.save(tmp_path / "Liberacao.xlsx")
    data = finep.parse(tmp_path)
    assert data.as_of == date(2026, 10, 23)
    assert data.stats == {"duplicatas_descartadas": 1, "datas_futuras": 1}
    assert sum(d["value"] for d in data.disbursements) == 60
    assert data.awards["01.24.0001.00"]["executor_cnpj"] == "22222222000122"


def test_matcher(tmp_path):
    (tmp_path / "pnipe_institutions.csv").write_text(
        "cnpj,social_reason,initials,state,city\n"
        "63025530000104,Universidade de São Paulo,USP,SP,São Paulo\n"
        "00000000000191,Fundação Universidade Federal de Rondônia,UNIR,RO,Porto Velho\n", encoding="utf-8")
    (tmp_path / "pnipe_labs.csv").write_text("name,initials,institution_cnpj\n", encoding="utf-8")
    m = InstitutionMatcher.from_ref(tmp_path)
    assert m.match("63025530000104", None) == ("63025530000104", "cnpj")
    assert m.match(None, "UNIVERSIDADE DE SAO PAULO") == ("63025530000104", "nome")
    assert m.match(None, "usp") == ("63025530000104", "sigla")
    assert m.match(None, "Universidade Federal de Rondônia")[1] == "trecho"
    assert m.match(None, "Hospital Qualquer") is None
