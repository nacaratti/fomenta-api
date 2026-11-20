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


def test_cnpq_reduce_rows_from_workbook_extract(tmp_path):
    rows = [  # (ano, consulta, instituição, UF por extenso, linha, modalidade, origem, valor)
        (2005, "2026-05-11 00:00:00", "Universidade de Sao Paulo", "Sao Paulo", "BOLSAS", "Doutorado Sanduíche",
         "CNPq", 100.0),
        (2005, "2026-05-11 00:00:00", "Universidade de Sao Paulo", "São Paulo", "BOLSAS", "Doutorado Sanduíche",
         "CNPq", 50.0),
        (2005, "2026-05-11 00:00:00", "Universidade de Sao Paulo", "Sao Paulo", "AUXILIOS", "Auxílio Pesquisa",
         "PI SEM USO", 25.0),
        (2005, "2026-05-11 00:00:00", "Cornell University", None, "AUXILIOS", "Pós-Doutorado Exterior", "CNPq", 0.0),
        (2006, "2026-05-11 00:00:00", "Universidade de Sao Paulo", "Sao Paulo", "BOLSAS", "Doutorado", "CNPq", 10.0),
    ]
    files = cnpq.reduce_rows(rows, {2005: 175.0, 2006: 10.0}, tmp_path, years={2005})
    assert [f.name for f in files] == ["2005.csv"]
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # cabeçalho + 2 (o valor zero fica de fora)
    assert "2005,2026-05-11,Universidade de Sao Paulo,SP,BOLSAS,Doutorado Sanduíche no País,CNPq,150.00" in lines
    assert any(line.endswith(",OUTRAS,25.00") for line in lines)  # mesmo rótulo do export
    data = cnpq.parse(tmp_path)
    assert {a["approved_value"] for a in data.awards.values()} == {150.0, 25.0}


def test_cnpq_reduce_rows_rejects_total_mismatch(tmp_path):
    rows = [(2005, "2026-05-11", "USP", "São Paulo", "BOLSAS", "Doutorado", "CNPq", 100.0)]
    with pytest.raises(ValueError, match="total do extrato"):
        cnpq.reduce_rows(rows, {2005: 999.0}, tmp_path)


def test_cnpq_parse_years():
    assert cnpq.parse_years(None) is None
    assert cnpq.parse_years("2026") == {2026}
    assert cnpq.parse_years("2005-2007") == {2005, 2006, 2007}


def test_capes_old_files(tmp_path):
    """Arquivos de 2012–2021: SG_UF em vez de SG_UF_IES, milhar com vírgula e programa com espaço."""
    head = ("AN_CONCESSAO;SG_PROGRAMA_FOMENTO;CD_IES_CAPES;SG_IES_CAPES;NM_IES_CAPES;CD_PPG;NM_PPG;NM_REGIAO;SG_UF;"
            "NM_AREA_CONHECIMENTO;TP_NATUREZA_DESPESA;VL_CONCEDIDO")
    rows = ['2017; PROAP;1;UNIR;UNIVERSIDADE FEDERAL DE RONDÔNIA;1P1;BIOLOGIA;NORTE;RO;PARASITOLOGIA;CUSTEIO;" 27,720"',
            "2012;PROEX;1;UNIR;UNIVERSIDADE FEDERAL DE RONDÔNIA;1P1;BIOLOGIA;NORTE;RO;PARASITOLOGIA;CAPITAL;52000.00",
            "2012;PROEX;1;UNIR;UNIVERSIDADE FEDERAL DE RONDÔNIA;1P1;BIOLOGIA;NORTE;RO;PARASITOLOGIA;CUSTEIO;48000.00"]
    (tmp_path / "a.csv").write_bytes("\n".join([head, *rows]).encode("cp1252"))
    data = capes.parse(tmp_path)
    assert data.awards["2017-PROAP-1-1P1"]["approved_value"] == 27720.0
    assert data.awards["2017-PROAP-1-1P1"]["uf"] == "RO"
    assert data.awards["2012-PROEX-1-1P1"]["extra"]["natureza"] == {"capital": 52000.0, "custeio": 48000.0}


def test_synergy_graph_goes_to_parquet_sorted(tmp_path):
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    from pipeline.build import load_snapshot

    snap = tmp_path / "snap"
    snap.mkdir()
    pq.write_table(pa.Table.from_pylist([{"key": k, "node_ids": [1, 2]} for k in ("b", "a", "c")]),
                   snap / "synergy_graph.parquet")
    pq.write_table(pa.Table.from_pylist([{"lab_id": 1}]), snap / "synergy_lab.parquet")
    (snap / "snapshot.json").write_text('{"tables": ["synergy_graph", "synergy_lab"], "exported_at": "x"}',
                                        encoding="utf-8")
    con = duckdb.connect(str(tmp_path / "f.duckdb"))
    load_snapshot(con, snap, tmp_path / "tabelas")
    tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert tables == {"synergy_lab"}
    keys = [r[0] for r in duckdb.sql(
        f"SELECT key FROM read_parquet('{(tmp_path / 'tabelas' / 'synergy_graph.parquet').as_posix()}')").fetchall()]
    assert keys == ["a", "b", "c"]
