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
| `GET /v1/elos/pesquisadores` · `/pesquisadores/{id}` | pesquisadores; filtros `cnpj`, `laboratorio`, `papel` (responsavel, equipe, coautor), `origem`, `tema`, `q`; perfil com laboratórios, temas e coautores |
| `GET /v1/elos/pesquisadores/{id}/coautores` · `/afinidades` | todos os coautores, com as publicações em comum; quem pesquisa temas parecidos e nunca publicou junto |
| `GET /v1/elos/laboratorios/{id}` · `/laboratorios/{id}/coautorias` · `/v1/elos/temas` | responsável, equipe e coautores do responsável; laboratórios que copublicam; temas |
| `GET /v1/publicacoes` · `/publicacoes/{id}` | publicações desde 2019 dos responsáveis (OpenAlex); filtros `q` (título), `ano`, `financiador`, `ods`, `ordem` (recentes, citadas, impacto); autores da base, financiadores, auxílios, palavras-chave e ODS |
| `GET /v1/publicacoes/financiadores` · `/financiadores/{id}` | financiadores citados nas publicações; quem cada um apoiou (pesquisadores, laboratórios, nº dos processos) |
| `GET /v1/elos/pesquisadores/{id}/publicacoes` · `/financiamentos` | publicações e financiadores de uma pessoa |
| `GET /v1/elos/laboratorios/{id}/publicacoes` · `/financiamentos` | idem, do responsável e da equipe de um laboratório |
| `GET /v1/financiadores` · `/financiadores/{id ou slug}` | financiadores brasileiros e a agência dos editais de cada um; um financiador com os editais abertos, auxílios por ano e modalidade, pesquisadores e laboratórios apoiados |
| `GET /v1/auxilios` · `/auxilios/{id}` | auxílios publicados pelas agências (FAPESP, FAPEMIG, CAPES cooperação, FINEP/Funttel) ligados à base; filtros `financiador`, `ano`, `vigentes`, `cnpj`, `q`, `ordem` (recentes, valor); o detalhe traz as publicações da base e o projeto FINEP |
| `GET /v1/elos/pesquisadores/{id}/auxilios` · `/v1/elos/laboratorios/{id}/auxilios` | auxílios em que a pessoa, ou o responsável e a equipe, são os responsáveis |
| `GET /v1/investimentos/projetos/finep/{contrato}/publicacoes` | publicações da base que citam o contrato FINEP |
| `GET /v1/editais/{id}/pesquisadores` · `/v1/editais/{id}/laboratorios` | **inferência**: quem tem perfil para o edital, pela semelhança entre o tema do edital e as publicações; FAP estadual só mostra gente do estado; editais de todas as áreas ou só para empresas vêm sem lista, com o motivo |
| `GET /v1/elos/laboratorios/{id}?estavel=true` · `/v1/elos/pesquisadores?laboratorio=&papel=coautor&estavel=true` | coautores com perfil completo do OpenAlex (publicações, citações, h-index, instituição atual) e `colaborador_estavel` (**inferência**: 2+ anos com publicação junto ao responsável), que vêm primeiro |
| `GET /v1/elos/pesquisadores/{id}/editais` · `/v1/elos/laboratorios/{id}/editais` | o caminho inverso: editais (abertos, por padrão) em que a pessoa ou o laboratório aparece |

**Elos ampliado.** Na API, todo pesquisador que publicou com o responsável de um laboratório desde 2019
fica ligado a ele com o papel `coautor`, com o número de publicações em comum e as mais recentes como
evidência (links do OpenAlex). Coautores de fora da base entram com nome, ORCID e instituição da
publicação mais recente. Também voltam os dados que o site corta por espaço no Supabase: 10 temas por
responsável, coautorias sem teto, até 20 afinidades e todos os pares de laboratórios. O Elos e as
publicações ficam em `build/tabelas/*.parquet`, ao lado do `fomenta.duckdb`, e a API os lê como views:
em Parquet ocupam menos da metade do DuckDB, e a função da Vercel tem teto de 225 MB (o build recusa
dados acima de 190 MB, e cada arquivo precisa ficar abaixo de 100 MB para entrar na branch deploy). Os resumos ficam só em
`resumos_obras.parquet`, na Release `snapshot-plataforma`.

A busca `q` não diferencia acento nem caixa, e cada palavra precisa aparecer.

**Privacidade.** Ficam fora da API e dos arquivos: o contato pessoal dos laboratórios (nome,
telefone e e-mail do responsável), qualquer pesquisador com pedido de remoção (aprovado ou
pendente) no Elos, vínculos pesquisador–laboratório com confiança abaixo de 0,8 e os relatórios
internos de carga. Os pedidos de remoção valem também para os coautores.

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
# Elos ampliado, a partir do cache do OpenAlex (sem gasto de cota); no FomentaBR:
PYTHONPATH=src python <fomenta-api>/tools/elos_ampliado.py calcular --saida elos.pkl.gz
# de volta aqui (substitui as tabelas elos_* do retrato):
python tools/elos_ampliado.py exportar elos.pkl.gz
# publicações completas (usa a chave do OpenAlex, ~US$ 0,17); no FomentaBR:
PYTHONPATH=src python <fomenta-api>/tools/openalex_obras.py coletar elos.pkl.gz --saida obras.jsonl.gz
python tools/openalex_obras.py exportar obras.jsonl.gz      # de volta aqui
# financiadores e auxílios (~US$ 0,10); no FomentaBR:
PYTHONPATH=src python <fomenta-api>/tools/openalex_auxilios.py coletar --saida auxilios.jsonl.gz
python tools/openalex_auxilios.py exportar auxilios.jsonl.gz  # de volta aqui, depois das obras
# perfil dos coautores de fora (~US$ 0,40) e colaborador estável; depois das obras:
python tools/coautores_perfis.py ids --saida coautores.txt.gz
PYTHONPATH=src python <fomenta-api>/tools/coautores_perfis.py coletar coautores.txt.gz --saida coautores.jsonl.gz  # no FomentaBR
python tools/coautores_perfis.py exportar coautores.jsonl.gz
# perfil para editais (vetores da OpenAI, ~US$ 0,17 na primeira vez; depois só os novos):
python tools/perfis_editais.py perfis --saida perfis.jsonl.gz
PYTHONPATH=src python <fomenta-api>/tools/perfis_editais.py vetores perfis.jsonl.gz --pasta vetores/  # no FomentaBR
python tools/perfis_editais.py exportar vetores/           # de volta aqui, depois dos auxílios e dos coautores
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
