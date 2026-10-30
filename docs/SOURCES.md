# Fontes da base de conhecimento

Coleta em 2026-10-30 por `python -m app.ingest --refresh` (httpx + stdlib). Saída: `data/knowledge/chunks.jsonl`
(462 chunks, 300–800 caracteres, texto literal da página, dedupe por sha256 do texto normalizado).

| Fonte | Idioma | Chunks* | Cobre | NÃO cobre |
|---|---|---|---|---|
| https://www.getnet.net/en/ (+ sitemap `/en/sitemap.xml`: soluções, setores, parceiros, developers, about, 12 posts de blog, client stories) | en | ~290 | Visão global: pagamentos presenciais/online/omnichannel, SEP (Single Entry Point), antecipação (value added), agentic commerce, segurança (tokenização), setores (restaurantes, viagens, grandes empresas) | Preços, taxas e produtos específicos do Brasil; 6 client stories dão 404 (ignoradas); blog limitado a 12 posts (`--limit-blog`) |
| https://www.getnet.net/pt/ (sitemap `/pt/sitemap.xml`: soluções, tarifas, FAQ de suporte) | pt-PT (Portugal) | ~100 | FAQ operacional de Get Classic, Get Smart, Get Tap on Phone, Get Checkout; tarifas (€) | É **Portugal**, não Brasil: moeda, bandeiras (MB/MB Way) e regras diferem; páginas `/legal/` e `/contacto` ignoradas |
| https://site.getnet.com.br/ (home, todas-as-maquininhas, pix, link-de-pagamento, get-tap, getcheckout, tef, whatsapp, segurança, get-ajuda, ouvidoria, eye, artigos) | pt-BR | ~80 | Produtos do Brasil: Get Clássica R$79,90/mês, Get Smart R$129,90/mês, Get Tap, Pix, link de pagamento (plano básico R$28/mês), Get Checkout, TEF, recebimento em 2 dias úteis | Taxas por faturamento/bandeira são carregadas por JavaScript (tabelas simuladas) e não aparecem no HTML; `fale-conosco` dá 404; sem dados de cliente (isso é o CRM, não o site) |

\* contagem aproximada por domínio; a exata sai do `--refresh`.

## Lacunas conhecidas
- **PDFs não ingeridos** (manual de comodato, Código de Conduta 2026, PDF do Metrobus): o venv não tem biblioteca de PDF e
  o desafio proíbe dependência nova sem listar. Candidato: `pypdf` (puro Python) se sobrar tempo.
- Produtos citados no enunciado sem página própria encontrada no site: **crediário**, **conta digital** (aparece só
  como "Get Conta com cartão grátis" em `todas-as-maquininhas`), **antecipação** (só em `/en` value-added e menções no BR).
  A resposta sobre eles deve admitir que a fonte é rasa.
- O site é renderizado em parte por JS: o que só existe após execução de script não é coletado (sem browser headless).
- Texto de interface (login, CTA, horário) é filtrado por regex em `ingest._UI`; sobra algum ruído de carrossel de depoimentos.

## Avaliação de retrieval (`evals/retrieval.jsonl`, 30 casos pt/en)
Relevância marcada lendo as páginas-fonte (cada id tem uma palavra-chave literal verificada no texto do chunk),
não olhando o buscador. Após a primeira medição, 2 casos ganharam chunks relevantes adicionais que eu tinha
deixado de fora (FAQ de compatibilidade do Get Tap; trecho que define encryption vs tokenisation) — ajuste feito
lendo o texto, e registrado aqui por transparência.

BM25 do kit (`app.rag`, k=3): **Recall@3 = 0,650 · MRR = 0,592** (hit@3 = 0,70). Falhas típicas: pergunta curta com
stopwords que o kit remove ("get"), perguntas de preço onde a página PT-PT ganha da PT-BR, e idiomas misturados.
Híbrido BM25 + embeddings multilíngue (RRF) **não foi implementado** neste prazo.
