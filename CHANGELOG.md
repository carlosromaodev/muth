# Alterações

## 0.4.0

Qualidade facial geométrica/exposição, contratos de inferência no arranque e
carregamento OpenCV a partir dos bytes verificados. Admissão antes de uploads,
workers limitados mesmo após cancelamento, cleanup de claims e correcção das
transacções SQLite/readiness. Aprendizagem consulta holdout apenas após validação,
deduplica PAD pela pessoa capturada e conserva membros efectivamente consultados.
Benchmark CPU por imagens com splits, denominadores, abstentions e pior PAI.

**Compatibilidade:** fingerprint inclui preprocessing v2 e versões de runtime;
políticas anteriores deixam de aplicar-se e a baseline fica inconclusiva até
nova calibração validada. Schema permanece `0002_learning`. Novos parâmetros
`MUTH_MAX_INFERENCE_REQUESTS` (2) e `MUTH_MAX_CONTROL_REQUEST_BYTES` (64 KiB).
Pedidos ocupados recebem 429/Retry-After 1; campos JSON/controlos têm limite menor.
Benchmark offline exige paths locais POSIX e não promove políticas.

## 0.3.0

YuNet/SFace e MiniFASNetV2/V1SE locais em CPU, quality gates, fingerprints e
manifesto de bundle com SHA-256. Preparação/exportação explícita, paridade
PyTorch/ONNX e testes de inferência real. Aprendizagem supervisionada por scores
consentidos, labels independentes, partições por pessoa, calibração automática,
gates de promoção, versões, rollback e revogação por eliminação/expiração.

**Compatibilidade:** migração `0002_learning` obrigatória. Novos scopes `feedback`
e `learning`; bootstrap inclui-os, chaves antigas exigem concessão explícita.
Consentimento expõe `learning_opt_in=false` e Check inclui metadados de modelo,
score e calibração. Similaridade aceita [-1,1]. Arranque exclusivamente pela app
factory `muth.main:create_app --factory`, evitando carregar pesos duas vezes.
Demo conserva comportamento anterior; modo biometric exige bundle verificado.
ID apenas extrai retrato e mantém autenticidade inconclusiva; Auth continua 501.

## 0.2.0

Sessões persistentes e consentimento, multiempresa e scopes, cifragem, idempotência,
claims com lease, auditoria, rejeição manual, retenção e eliminação. Adicionados
Alembic, CLI, métricas, limite de corpo, manifestos, benchmark offline, SDD e pesquisa.

**Compatibilidade:** `/v1` agora exige credenciais mesmo em demo. A chamada antiga
`POST /v1/verifications` continua disponível com autenticação e mantém resposta
efémera; preferir sessões para resultado persistente. Os erros passam a usar
`error.code`, `error.message` e `request_id`. A DB persistente requer chave Fernet
e migração. O arranque recomendado usa a app factory.

## 0.1.0

Estrutura modular FastAPI, contratos Face/Liveness/ID, demo, Verify e política base.
