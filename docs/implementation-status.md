# Resultado da execução do SDD

2 de Outubro de 2026 · MUTH v0.4.0 · SDD 1.2.

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
