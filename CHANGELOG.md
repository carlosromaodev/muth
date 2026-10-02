# Alterações

## 0.6.0

Resultado web com campos documentais extraídos localmente por Tesseract,
confiança/origem, conflitos e checks estruturais MRZ. Opt-in separado de
aprendizagem, propostas de correcção OCR e fila cifrada: apenas revisão
administrativa independente gera amostras para a calibração existente.
`document_data.processing_version=muth-document-ocr-v2` versiona a extracção,
com fallback adaptativo de PSM 6 para PSM 11 e resolução conservadora de conflitos.
Guardar identidade exige outro consentimento e persiste template SFace de 128
valores normalizados junto dos dados documentais, cifrados e versionados pelo
fingerprint do modelo. Consulta, eliminação e comparação posterior 1:1 usam
capability própria de perfil, sem expor vectores ou chaves B2B no browser.

**Compatibilidade:** aplicar `0004_capture_learning` e `0005_identities` com
`muth migrate`. Instalar Tesseract com `por`/`eng` no host; Docker inclui ambos.
Quota do portal passa de 1000 para 10 000 sessões activas. Token de captura permite
consulta/eliminação até à retenção; novos uploads conservam o prazo de 30 minutos.
Retenção por defeito: resultado sem contribuição 1 dia, contribuição 30 dias e
perfil facial independente 365 dias. Withdraw elimina contribuições/amostras,
revoga calibrações dependentes e limita o resultado original ao prazo normal
restante, no máximo um dia a partir da retirada. Delete explícito da captura
também elimina os perfis dela derivados; expiração normal do resultado não os elimina.
Nova quota `MUTH_IDENTITY_MAX_PROFILES=10000` para perfis activos por tenant,
independente da quota de captura; novas inscrições acima do limite recebem 429.

Novo scope administrativo `capture_review`, sem concessão pelo bootstrap/legacy,
para `/v1/capture-learning`, `/v1/capture-identities` e `/v1/auth/authenticate`.
Auth devolve comparação provisória e `authenticated=false`; continua sem protocolo
de login pronto. Nota 0–10 mantém teto experimental 6, autenticidade por confirmar
e ausência de precisão validada em BI angolanos. Reiniciar a interface conserva
contribuições/perfis autorizados; o utilizador pode retirá-los/eliminá-los explicitamente.

## 0.5.0

Portal responsive de captura servido pelo FastAPI: consentimento, câmara/ficheiro,
frente/verso, selfie, recaptura, revisão e resultado. Capabilities de sessão sem
chave B2B no browser, tenant reservado, guard de origem, quota/rate limit/deadline,
replay das três imagens e retenção automática. Relatório cifrado e nota indicativa
0–10 com teto actual 6, autenticidade sempre por confirmar e demo sem nota.

**Compatibilidade:** aplicar `0003_capture`. Captura pública activada por defeito
no servidor configurado; desactivar com `MUTH_CAPTURE_PORTAL_ENABLED=false`.
Retenção própria do portal: um dia por defeito, sem opt-in de aprendizagem.
Upload tem deadline de 120 segundos (`MUTH_REQUEST_BODY_TIMEOUT_SECONDS`).
Assets entram no wheel; B2B `/v1` conserva autenticação/scopes. HTTPS mobile tem
configuração Caddy/Compose fornecida, sem deploy externo nesta entrega.

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
