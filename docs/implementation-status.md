# Resultado da execução do SDD

2 de Outubro de 2026 · MUTH v0.6.0 · SDD 1.4.

## Estado actual — OCR, aprendizagem web e identidade guardada v0.6

O resultado apresenta dados documentais extraídos por Tesseract local e nota
indicativa. OCR de frente/verso conserva apenas campos estruturados com
confiança/origem, validações e conflitos; MRZ verifica estrutura/dígitos sem
consultar o emissor. Idiomas português/inglês estão incluídos no Dockerfile;
instalação local exige os respectivos pacotes. Campos ausentes não são inventados.
`processing_version=muth-document-ocr-v2` rastreia a extracção. PSM 6 tem fallback
adaptativo PSM 11, com deadline/orçamento partilhados e conflitos preservados.
Segue [web-learning-SDD.md](web-learning-SDD.md) e a migração `0005_identities`.

A captura pode contribuir por opt-in separado. Fila e dados/correcções OCR ficam
cifrados; uma contribuição pending_review não cria amostras de aprendizagem.
Propostas do utilizador não confirmam labels nem referências de pessoa. Operador
com scope explícito `capture_review` declara evidência independente e referências
pseudónimas com namespace; apenas então o motor existente recebe amostras para
calibração de limiares. O scope não é concedido pelo bootstrap nem à chave legacy.
Sem dataset suficiente e gates satisfeitos, o worker conserva collecting.
Não há actualização autónoma de pesos nem promessa de melhoria por verificação.

Um terceiro consentimento permite guardar template SFace de 128 valores
normalizados junto dos campos documentais e fingerprint do modelo, com Fernet.
Exige dados/sinais suficientes, sem inventar uma identidade validada. Perfil
sempre provisório, nunca expõe vector na API, e tem capability própria para
consulta, eliminação e comparação 1:1 com nova selfie. Esta comparação devolve
Face/PAD e `authenticated=false`; não é ainda um protocolo de login.

Retenção por defeito: resultado sem contribuição um dia; contribuição/resultado
30 dias; perfil facial 365 dias com ciclo independente. Retirada elimina fila e
amostras/revoga calibrações dependentes, conservando resultado original até ao
menor prazo entre o anterior e um dia a contar da retirada. DELETE explícito da
captura elimina perfis derivados; expiração normal da captura não os elimina.
Reiniciar para nova captura conserva os registos consentidos; retirada/eliminação
explícitas estão disponíveis na interface. Imagens continuam transitórias e
tokens ficam só em memória. Quota pública passa para 10 000 sessões activas.
Perfis activos têm quota independente de 10 000 por tenant, configurada por
`MUTH_IDENTITY_MAX_PROFILES` (`identity_max_profiles`), aplicada transaccionalmente.

Verificação de integração executada durante esta entrega:

- Tesseract 5.5 local leu oito campos de uma credencial sintética com retrato,
  normalizada pelo fluxo de captura e processada com OCR v2/PSM 11 adaptativo.
  O ensaio CPU produziu face cosine 0,94443 e PAD 0,99981; são scores da fixture,
  não taxas de precisão ou prova da autenticidade de um documento real.
- Build Docker com OCR incluiu `por`/`eng`; execução não requer OCR remoto.
- Suite completa: **258 testes passaram**, incluindo inferência biométrica CPU
  real. A distribuição base executou a mesma suite, com 26 casos opcionais
  ignorados. Ruff, formato, sintaxe JavaScript e `git diff --check` passaram.
- HTTPS local através de Caddy, com certificado verificado: **68 verificações
  passaram**, incluindo OCR `por+eng`, nota 6, perfil cifrado, comparação 1:1,
  revisão, retirada e eliminação. A fixture gerou uma amostra facial para testar
  a revisão; nenhuma label de liveness foi atribuída à fotografia. Todas as
  amostras e payloads do ensaio foram eliminados.
- Chromium completou o fluxo com o backend real, sugestões, comparação e
  eliminação. Larguras 320/390/1280 sem overflow; storage e erros de consola zero.
  Axe encontrou zero violações no resultado revisto, com contraste incompleto;
  isto não substitui avaliação manual ou em dispositivos físicos.
- A validação encontrou uma corrida no encerramento entre tarefas de limpeza e
  o fecho de SQLite. O servidor passa a aguardar os workers antes de fechar o
  banco; testes determinísticos cobrem retenção, refinamento e cancelamentos.
- Wheel e sdist verificados: assets/migrações incluídos; sem credenciais, banco
  ou pesos. `muth purge` também elimina os perfis cuja retenção terminou.

Evidência agregada: [browser v0.6](research/capture-browser-smoke-v06.json) e
[HTTPS v0.6](research/capture-container-smoke-v06.json).

Nota permanece em escala 0–10, com teto experimental 6/10 e
`authenticity_confirmed=false`. OCR/MRZ, embedding guardado e scores altos não
substituem validação documental/calibração local. Dataset angolano consentido,
ataques físicos, licenças comerciais e comparação independente continuam gates
abertos. Não houve deploy externo nem piloto em telemóvel físico. O GitHub Actions
continua impedido de iniciar pelo bloqueio de facturação da conta.

## Histórico v0.5

### Captura mobile e web v0.5

Portal responsive servido pelo FastAPI, seguindo [capture-SDD.md](capture-SDD.md):
consentimento, câmara/upload, frente/verso, selfie, revisão e resultado. Câmara
aberta por gesto, tracks paradas e alternativa para permissão negada. Não há
chave B2B no navegador, persistência de imagens/tokens no browser ou aprendizagem
no portal. Resultados cifrados usam a migração `0003_capture`, com retenção
limitada e purge periódico.

Nota real dos sinais disponíveis em escala 0–10, com teto experimental de 6/10,
indisponível sem evidência. Nenhuma captura confirma autenticidade documental ou
aprova automaticamente uma identidade. OCR, autenticação de BI e calibração
local permanecem gates abertos.

Validação executada nesta versão:

- **166 testes passaram**, incluindo os motores CPU opcionais.
- Instalação isolada do wheel sem biometria: **140 passaram, 26 opcionais omitidos**.
- Ruff lint/formatação, JavaScript syntax check e `git diff --check` passaram.
- Wheel/sdist incluem interface e migração; excluem credenciais, imagens, pesos e DB.
- Migrações upgrade/downgrade/upgrade e comparação ORM/schema passaram.
- Browser Chromium: câmara virtual, recaptura, upload real, revisão e inferência CPU.
  O exemplo público produziu 6/10, com `authenticity_confirmed=false` e estado review.
- Layouts 320/380/390 px sem overflow; teclado, estados de erro e Axe nos estados
  registados. Não substitui teste com câmaras de telemóveis físicos.
- Testes ASGI cobrem origem, tenant reservado, token cruzado/expirado, prefixo da
  app, deadline de upload, quota, replay, cifragem, eliminação e ausência de learning.
- Build Docker passou com proxy/CA do ambiente e utilizador UID 10001; assets
  presentes no pacote instalado. Compose foi validado sintacticamente.
- Smoke com API/container e gateway Caddy por HTTPS passou: certificado validado
  pela CA local, health/readiness, interface, inferência CPU, replay, leitura e
  eliminação. DNS público/TLS de produção e telemóvel físico não foram testados.

[Registo de browser](research/capture-browser-smoke-v05.json) documenta fixtures,
nota, dispositivos simulados e limites da verificação. Os exemplos públicos não
alimentam a aprendizagem e não medem desempenho numa população.
[Registo de container/HTTPS](research/capture-container-smoke-v05.json) conserva
somente os resultados agregados, sem credenciais, IDs ou imagens.

Código publicado em <https://github.com/carlosromaodev/muth>. GitHub Actions
continua impedido de iniciar pelo bloqueio de facturação da conta. Não houve
deploy num domínio externo nem teste físico em telemóvel. A configuração HTTPS
com Caddy, isolamento de rede e provisionamento de modelos é fornecida no SDD.

## Histórico v0.4

## Estado actual — biometria e backend v0.4

Refinamento implementado segundo [backend-refinement.md](backend-refinement.md):
qualidade geométrica/exposição, bytes verificados em OpenCV, contratos de modelos,
admissão antes do upload, capacidade de decoder/inferência retida após cancelamento,
cleanup de claims e transacções SQLite/readiness corrigidas. Aprendizagem consulta
teste apenas após validação; PAD deduplica pela pessoa capturada e relatórios
conservam somente exemplos consultados.

Benchmark offline CPU integrado na CLI: separação de pessoas/imagens, leitura
segura, FMR/FNMR/APCER/BPCER por split/dispositivo/PAI e pior PAI. Mostra falhas de
aquisição/qualidade, abstentions e distinção entre limiar diagnóstico e resultado
real do motor. Não treina pesos nem promove políticas.

Validação executada nesta versão:

- **136 testes passaram**, com dependências biométricas e modelos CPU locais.
- Instalação isolada do wheel sem biometria: **110 testes passaram, 26 opcionais omitidos**.
- Ruff lint/formatação e `git diff --check` passaram.
- Build e inspecção wheel/sdist passaram, incluindo licenças; sem pesos, DB, imagens ou segredos.
- Migrações upgrade/downgrade/upgrade e comparação ORM/schema passaram, com zero diferenças.
- CLI de validação/benchmark passou em quatro ensaios públicos: um self-match e três PAD.
- Paridade Torch/ONNX passou: erro máximo de logits 0,00000131 (V2) e 0,00000286 (V1SE).
- HTTP real em Uvicorn passou em versão/readiness, Face/Liveness, sessão/replay/delete,
  métricas de capacidade e limite de controlo. Servidor parado após o teste.

[Smoke v0.4](research/biometric-smoke-v04.json),
[benchmark público v0.4](research/biometric-benchmark-v04.json) e
[smoke HTTP](research/http-smoke-v04.json) registam a execução. As três imagens
públicas usam identidade desconhecida no benchmark; classes de ataque não
confirmadas ficam em `other`. Não existe denominador para APCER suportado nem
impostores confirmados para FMR. Todos os checks reais permanecem inconclusivos
sem calibração. Nenhuma fixture do smoke HTTP alimentou a aprendizagem.

Fingerprints incorporam preprocessing v2 e versões de runtime; políticas v0.3
não são reutilizadas. Schema mantém `0002_learning`. Pesquisa de OFIQ, Bob e NIST
orienta qualidade/protocolos, sem copiar código GPL nem alegar conformidade.

Código publicado em <https://github.com/carlosromaodev/muth>. GitHub Actions foi
impedido de iniciar pelo bloqueio de facturação da conta; isto não é um resultado
dos testes do código. Docker/deploy não foram executados. Dataset angolano
consentido e rotulado, ataques físicos e canal de captura continuam no gate aberto.

## Histórico v0.3

## Estado actual — biometria v0.3

Face e Liveness reais em CPU: YuNet/SFace e dois MiniFASNet ONNX, com checksums,
qualidade, abstention e fingerprints. Aprendizagem supervisionada de thresholds
implementada: opt-in, labels confirmados, separação por pessoa, gates, versões,
worker periódico, rollback e revogação. Segue [biometrics-SDD.md](biometrics-SDD.md).
Não actualiza pesos neurais nem se auto-rotula.

Inferência foi executada nas três imagens públicas do upstream. Self-comparison
deu coseno próximo de 1; PAD: T1 0,999879, F1 0,228581, F2 0,002344. Todos os checks
permanecem inconclusivos sem calibração local. Paridade ONNX/PyTorch teve erro
máximo de logits 0,00000131 (V2) e 0,00000286 (V1SE).
[Relatório público](research/biometric-smoke.json) e
[checksums/fontes](research/biometric-artifacts.json) preservam a evidência.
Estes resultados não são FMR/FNMR/APCER/BPCER de uma população.

Validação v0.3: testes de inferência, integração HTTP, feedback e ciclo de vida;
gates exercitados com scores **sintéticos**. Duas conexões persistentes em threads
só promovem a mesma snapshot uma vez. O worker periódico foi exercitado sem espera
real. A migração aplicada não difere do ORM. Wheel/sdist foram verificados sem
pesos, imagens, DB ou segredos e com licenças vendorizadas.

Configuração local: `.env` privado em modo `biometric`, manifesto research em
`models/biometrics/manifest.json`, DB migrada para `0002_learning`. Smoke HTTP real
em Uvicorn passou em health/readiness, face, liveness, sessões, replay, aprendizagem,
métricas e eliminação. Nenhum exemplo público do smoke HTTP foi usado para aprendizagem.
Servidor foi parado depois da verificação. Não houve deploy nem execução remota de CI.

**Gate aberto:** dataset angolano consentido e rotulado, avaliação física de ataques
e canal de captura, desempenho/fairness/custo local e comparação independente.
Não se pode declarar os motores refinados para Angola ou completos para produção
sem esta evidência. ID só extrai retrato; OCR/autenticidade e Auth permanecem pendentes.

## Registo final v0.3

- 68 testes, incluindo os seis testes CPU opcionais, passaram.
- Ruff lint e formatação passaram.
- Alembic upgrade/downgrade/upgrade e comparação ORM/schema passaram.
- Concorrência de calibração em duas conexões persistentes passou.
- Smoke HTTP real e paridade ONNX/PyTorch passaram.
- Build e inspecção do wheel/sdist passaram; pesos e segredos excluídos.
- Dockerfile e CI são configurações fornecidas; não foram executados remotamente.

O aviso ONNX Runtime sobre ID de telemetria não persistido no HOME restrito é
observável na inicialização; telemetria desactivada antes da inferência. O aviso
TestClient/httpx permanece, sem falha nos testes.

## Histórico da entrega v0.2

As secções seguintes registam a fundação anterior, antes da integração biométrica.

## Conclusão v0.2

O pacote P0 do SDD foi implementado. O MUTH evoluiu de uma API efémera de demonstração
para uma fundação B2B com sessões, fronteiras de autorização, persistência cifrada,
controlo de concorrência e ciclo de vida. Os motores continuam demo; não existe
verificação de identidade real, certificação biométrica ou validação comercial.

## Rastreabilidade

| Requisito | Implementação | Validação |
| --- | --- | --- |
| P0-01 Sessões e consentimento | Schemas e SessionStore; resultado persistido | Create→verify→read; consentimento estrito; restart da API |
| P0-02 Tenant e scopes | SHA-256, principal derivado da chave, scopes e OpenAPI | 401/403; 404 entre empresas em leitura, verify, review, delete e eventos |
| P0-03 Idempotência e claim | Fingerprint HMAC, update condicional e lease | Replay exacto, payload conflitante, duas conexões, lease e resultado obsoleto |
| P0-04 Cifragem e retenção | Fernet, TTL, scrub e purge | Payload não legível na DB; captura expirada; acesso após retenção; eliminação |
| P0-05 Auditoria e review | Eventos mínimos e rejeição manual | Sequência de eventos, scopes, rejeição e bloqueio de aprovação demo |
| P0-06 Bundle de engines | Injecção única em rotas e Verify | Provider substituído em teste; demo nunca aprova; falha retryable |
| P0-07 Limites e operação | ASGI body limit, thread decode, rate limit e métricas | Corpo declarado e chunked, auth antes da leitura, métricas sem IDs e request IDs |
| P0-08 Entrega | Alembic, CLI, runbook, lock, Dockerfile e CI | Up/down/up, comparação ORM/schema, bootstrap 0600 e build de wheel/sdist |
| P0-09 Laboratório | Manifesto/checksum e avaliação facial offline | Pesos alterados, licença ausente, traversal, taxas e intervalos conhecidos |

## Verificações executadas

- **42 testes passaram** com Python 3.12.14.
- Ruff lint e verificação de formatação passaram.
- Alembic upgrade→downgrade→upgrade passou em DB temporária.
- Comparação entre schema migrado e metadata ORM devolveu zero diferenças.
- Build de distribuição fonte e wheel passou; wheel inclui CLI e migrações.
- Fluxo por HTTP real em Uvicorn, com ficheiros sintéticos: health, readiness,
  Swagger/OpenAPI, create, verify, read, replay, review, events, delete e metrics.
- Credenciais inválidas devolveram 401 com request ID consistente.
- Exemplo sintético do benchmark gerou taxas esperadas e aviso de amostra pequena.

O aviso de depreciação de Starlette sobre TestClient/httpx foi observado; os testes
passam com as dependências bloqueadas. Actualizar o cliente de testes quando for
feita a próxima revisão de dependências. O aviso não constitui falha da API.

## Entrega local

Configuração local foi gerada em `.env`, sem imprimir chaves, e a migração foi
aplicada a `data/muth.db`. Ambos estão excluídos de controlo de versões e dos
pacotes distribuídos. O servidor utilizado para a verificação foi parado após
os testes; iniciar com o comando da app factory no README.

As fontes de pesquisa foram consolidadas em `docs/research/sources.json` com
URLs, estados e SHAs. Não foram mantidas cópias completas de READMEs de terceiros
nem descarregados pesos biométricos.

## O que não foi executado ou comprovado

- Dockerfile e workflow CI foram criados; não houve build de container, execução
  remota de CI, deploy, publicação ou criação de repositório remoto.
- Nenhuma inferência InsightFace/PaddleOCR/Silent-Face foi integrada ou executada.
- Não existem dataset angolano consentido, licença comercial de pesos, cobertura
  de BI validada, métricas PAD/IAD, SLA medido ou preço unitário calculado.
- Review P0 apenas rejeita; não dispõe de evidências armazenadas para análise visual.
- Chaves/quotas são locais; PostgreSQL, webhooks duráveis, autenticação reutilizável,
  capture SDK e defesas contra injection continuam nos gates P1–P3.

O próximo gate técnico é P1: escolher e licenciar pesos, integrar adapters reais e
medir o cenário local. O próximo gate comercial é um piloto com comprador e métricas
de fraude, conversão, latência, revisão e custo acordadas.
