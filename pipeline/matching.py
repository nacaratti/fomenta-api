"""Casamento de beneficiários de fomento com as instituições do PNIPE.

Lê o snapshot do cadastro (`data/ref/pnipe_institutions.csv` e
`data/ref/pnipe_labs.csv`, gerados por `tools/export_pnipe_ref.py`). Ordem:

1. CNPJ exato (`cnpj`).
2. Nome normalizado igual à razão social (`nome`).
3. Sigla igual (`sigla`, só com mais de 2 letras).
4. Nome ou sigla de um laboratório do PNIPE (`laboratorio`; só para o executor da FINEP).
5. Razão social como trecho contíguo do nome, com fronteira de palavra
   (`trecho`): pega "FUNDAÇÃO <univ>", "<univ> - CAMPUS X" etc. Só nomes com
   14+ letras e 2+ palavras, para não casar nomes genéricos curtos.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.common import clean_name

REF_DIR = Path(__file__).resolve().parents[1] / "data" / "ref"


@dataclass
class InstitutionMatcher:
    cnpjs: set[str] = field(default_factory=set)
    by_name: dict[str, str] = field(default_factory=dict)
    by_initials: dict[str, str] = field(default_factory=dict)
    lab_by_name: dict[str, str] = field(default_factory=dict)
    lab_by_initials: dict[str, str] = field(default_factory=dict)
    substrings: list[tuple[str, str]] = field(default_factory=list)
    _cache: dict[str, tuple[str, str] | None] = field(default_factory=dict)

    @classmethod
    def from_ref(cls, ref_dir: Path = REF_DIR) -> InstitutionMatcher:
        m = cls()
        seen: set[tuple[str, str]] = set()

        def add_sub(nm: str, cnpj: str) -> None:
            if len(nm) >= 14 and len(nm.split()) >= 2 and (nm, cnpj) not in seen:
                seen.add((nm, cnpj))
                m.substrings.append((f" {nm} ", cnpj))

        with (ref_dir / "pnipe_institutions.csv").open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cnpj = r["cnpj"]
                m.cnpjs.add(cnpj)
                if r["social_reason"]:
                    nm = clean_name(r["social_reason"])
                    m.by_name.setdefault(nm, cnpj)
                    add_sub(nm, cnpj)
                    if nm.startswith("fundacao "):  # o cadastro às vezes guarda o prefixo que a fonte omite
                        add_sub(nm[len("fundacao "):], cnpj)
                if r["initials"]:
                    m.by_initials.setdefault(clean_name(r["initials"]), cnpj)
        with (ref_dir / "pnipe_labs.csv").open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["name"]:
                    m.lab_by_name.setdefault(clean_name(r["name"]), r["institution_cnpj"])
                if r["initials"]:
                    m.lab_by_initials.setdefault(clean_name(r["initials"]), r["institution_cnpj"])
        m.substrings.sort(key=lambda x: -len(x[0]))
        return m

    def match(self, cnpj: str | None, name: str | None, *, use_labs: bool = True) -> tuple[str, str] | None:
        """(cnpj_pnipe, método) ou None."""
        if cnpj and cnpj in self.cnpjs:
            return cnpj, "cnpj"
        nm = clean_name(name)
        if not nm:
            return None
        key = f"{int(use_labs)}|{nm}"
        if key in self._cache:
            return self._cache[key]
        res: tuple[str, str] | None = None
        if nm in self.by_name:
            res = (self.by_name[nm], "nome")
        elif len(nm) > 2 and nm in self.by_initials:
            res = (self.by_initials[nm], "sigla")
        elif use_labs and nm in self.lab_by_name:
            res = (self.lab_by_name[nm], "laboratorio")
        elif use_labs and len(nm) > 2 and nm in self.lab_by_initials:
            res = (self.lab_by_initials[nm], "laboratorio")
        else:
            padded = f" {nm} "
            res = next(((c, "trecho") for sub, c in self.substrings if sub in padded), None)
        self._cache[key] = res
        return res

    def links_for(self, award: dict, mode: str) -> list[tuple[str, str, str]]:
        """Vínculos (cnpj, papel, método) de um award."""
        if mode == "name":
            hit = self.match(None, award.get("executor_name"), use_labs=False) or self.match(
                None, (award.get("extra") or {}).get("sigla"), use_labs=False)
            return [(hit[0], "executor", hit[1])] if hit else []
        ex = self.match(award.get("executor_cnpj"), award.get("executor_name"))
        pr = self.match(award.get("proponent_cnpj"), award.get("proponent_name"), use_labs=False)
        if ex and pr:
            if ex[0] == pr[0]:
                best = ex if ex[1] == "cnpj" else pr
                return [(best[0], "ambos", best[1])]
            return [(ex[0], "executor", ex[1]), (pr[0], "proponente", pr[1])]
        if ex:
            return [(ex[0], "executor", ex[1])]
        if pr:
            return [(pr[0], "proponente", pr[1])]
        return []
