# Fomenta API

API pública e somente leitura do **Fomenta** sobre fomento e infraestrutura de pesquisa no Brasil:
**investimentos em PD&I** (FINEP, CAPES PROAP/PROEX, CNPq bolsas e auxílios), **editais** de
fomento, **congressos**, a **infraestrutura** do PNIPE (instituições, laboratórios, equipamentos), a
**sinergia** entre laboratórios e o **Elos** (pesquisadores e coautorias, a partir do OpenAlex).

Editais, congressos, PNIPE, sinergia e Elos vêm de um **retrato** do banco do FomentaBR
(Release `snapshot-plataforma`); a data dele está em `/v1/meta` → `plataforma.exported_at`.

Os dados mudam no ritmo das fontes (a FINEP, toda semana; CAPES e CNPq, poucas vezes por ano),
então não há banco ligado: um **build** baixa as fontes oficiais e gera um arquivo DuckDB, que a
API lê só para consulta. O mesmo build publica os dados em Parquet para quem quiser analisar.

```
fontes oficiais ──► pipeline/build.py ──► build/fomenta.duckdb ──► api/ (FastAPI, Vercel)
                                       ├► build/parquet/*.parquet ──► Release "dados-AAAA-MM-DD"
                                       ├► build/static/v1/*.json  ──► rotas fixas pela CDN
                                       └► build/manifest.json      (posição, totais, sha256)
```

## Rotas (`/v1`)

Documentação interativa em `/v1/docs`.

| Rota | O que devolve |
|---|---|
| `GET /v1/investimentos/financiadores` | financiadores, totais e data de posição dos dados |
| `GET /v1/investimentos/financiadores/{id}` | por ano, instrumento, situação, UF, região e ranking de instituições |
| `GET /v1/investimentos/projetos` | projetos e concessões; filtros `financiador`, `ano`, `uf`, `instrumento`, `cnpj`, `q`; até 100 por página |
| `GET /v1/instituicoes/{cnpj}/investimentos` | o que cada financiador investiu numa instituição do PNIPE, por ano |
| `GET /v1/meta` | o manifesto do build |
| `GET /v1/editais` · `/editais/{id}` · `/editais/{id}/texto` | editais; filtros `agencia`, `situacao` (abertos, encerrados, sem_prazo), `tipo`, `modalidade`, `area`, `q` |
| `GET /v1/agencias` · `/v1/areas` | agências e áreas do conhecimento, com contagem de editais |
| `GET /v1/congressos` · `/congressos/{id}` | congressos; filtros `situacao` (proximos, passados), `categoria`, `q` |
| `GET /v1/instituicoes` · `/instituicoes/{cnpj}` | instituições do PNIPE; filtros `uf`, `embrapii`, `forca_armada`, `q` |
| `GET /v1/laboratorios` · `/laboratorios/{id}` | laboratórios; filtros `uf`, `cnpj`, `area`, `q`; o detalhe lista os equipamentos |
| `GET /v1/equipamentos` · `/equipamentos/{id}` | equipamentos; filtros `uf`, `cnpj`, `laboratorio`, `multiusuario`, `q` |
| `GET /v1/sinergia/grafos` · `/grafos/{chave}` · `/laboratorios/{id}` | grafos por área, nós e arestas (`base`, `relacao`), perfil de um laboratório |
| `GET /v1/elos/pesquisadores` · `/pesquisadores/{id}` | pesquisadores; filtros `cnpj`, `laboratorio`, `tema`, `q`; perfil com laboratórios, temas e coautores |
| `GET /v1/elos/laboratorios/{id}` · `/laboratorios/{id}/coautorias` · `/v1/elos/temas` | equipe, laboratórios que copublicam e temas |

A busca `q` não diferencia acento nem caixa, e cada palavra precisa aparecer.

**Privacidade.** Ficam fora da API e dos arquivos: o contato pessoal dos laboratórios (nome,
telefone e e-mail do responsável), qualquer pesquisador com pedido de remoção (aprovado ou
pendente) no Elos, vínculos pesquisador–laboratório com confiança abaixo de 0,8 e os relatórios
internos de carga.

Somente GET, CORS aberto, `Cache-Control` de 1 dia na CDN (o dado só muda a cada deploy) e
120 requisições por minuto por IP. Valores **nominais**; **não some financiadores diferentes**
(parte do FNDCT passa pela FINEP e pelo CNPq).

## Fontes

| Fonte | Como entra | Atualização |
|---|---|---|
| FINEP | `Contratacao.xlsx` e `Liberacao.xlsx` da Central de Downloads (URL fixa) | automática, toda segunda |
| CAPES | CSVs de custeio e capital PROAP/PROEX, listados pela API CKAN do portal | automática |
| CNPq | export anual do Painel de Fomento, reduzido para `data/raw/cnpq/<ano>.csv` | manual, por ano |
| PNIPE | `data/ref/*.csv` (instituições e laboratórios) para casar beneficiários | manual, quando o cadastro mudar |

### Incluir um ano do CNPq

1. Em <http://bi.cnpq.br/painel/v2/p-fomento-cti.html>, exporte o ano.
2. Reduza (descarta os dados pessoais e confere a soma com a linha "Total"):
   ```
   python -m pipeline.sources.cnpq reduce caminho/do/export.csv
   ```
3. Faça commit de `data/raw/cnpq/<ano>.csv`. O push dispara o build e o deploy.

### Atualizar o retrato da plataforma

Só leitura no banco do FomentaBR; depois suba os arquivos na Release `snapshot-plataforma`:

```
DATABASE_URL=postgresql://... python tools/export_supabase_snapshot.py
gh release upload snapshot-plataforma build/snapshot/* --clobber
```

### Atualizar o snapshot do PNIPE

```
DATABASE_URL=postgresql://... python tools/export_pnipe_ref.py   # só leitura no banco do FomentaBR
```

## Rodar localmente

```
python -m venv .venv && .venv/Scripts/activate       # Windows
pip install -r requirements-pipeline.txt
python -m pipeline.build                             # ou --no-download para reusar build/cache
python -m pytest -q
uvicorn api.index:app --reload                       # http://127.0.0.1:8000/v1/docs
```

## Análises

O arquivo `fomenta.duckdb` (e os Parquet) da última Release servem direto para SQL:

```sql
-- duckdb fomenta.duckdb
-- Quanto cada instituição do PNIPE recebeu por fonte em 2024
SELECT i.initials, f.source_id, f.approved
FROM inst_funder_year f JOIN institution i ON i.cnpj = f.institution_cnpj
WHERE f.year = 2024 ORDER BY f.approved DESC LIMIT 20;

-- Série anual da FINEP por instrumento
SELECT year(signing_date) AS ano, instrument, round(sum(approved_value) / 1e6) AS mi
FROM funding_award WHERE source_id = 'finep' GROUP BY ALL ORDER BY ano;
```

No Python: `duckdb.read_parquet("funding_award.parquet").df()`.

## Deploy

O GitHub Actions (`.github/workflows/build-deploy.yml`) monta os dados e publica a branch
**`deploy`**: um commit único, reescrito a cada build, só com `api/`, `vercel.json`,
`requirements.txt` e `build/` (banco, manifesto e JSON estáticos). O projeto na Vercel (conta
própria da API, ligado ao repositório pelo painel) usa `deploy` como branch de produção; a `main`
não gera deploy (`git.deploymentEnabled` no `vercel.json`), porque nela não há banco.
