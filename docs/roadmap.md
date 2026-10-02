# Roadmap por gates

## P0 — fundação v0.2

Sessões, consentimento, isolamento, persistência cifrada, idempotência, lease,
auditoria, eliminação, observabilidade e avaliação offline. O estado e a validação
estão em [implementation-status.md](implementation-status.md).

## P1 — piloto algorítmico

v0.3 implementou adapters Face/Liveness CPU, inferência real, artefactos verificados
e calibração supervisionada. O gate de precisão local permanece aberto: ver
[SDD biométrico](biometrics-SDD.md). O escopo actual termina nos motores biométricos;
OCR/Auth não serão desenvolvidos nesta iteração.

- Revisão das licenças dos pesos, manifesto e inferência reproduzível.
- Dataset consentido e separado por identidade entre calibração e teste.
- Adapters InsightFace/alternativa, liveness e PaddleOCR/docTR.
- Normalização por versão do BI, recorte do retrato e validações documentais.
- Relatórios de FMR/FNMR, APCER/BPCER por ataque, exact match/CER e custo/latência.

Gate: evidência quantitativa no cenário local. Sem ela, não activar produção.

## P2 — integração comercial

- Captura vinculada à sessão, tokens limitados e defesa contra injeção.
- Revisão completa com evidências autorizadas e motivos auditáveis.
- Webhooks assinados com outbox, retries e controlo de endpoints.
- PostgreSQL validado, quotas partilhadas, métricas de operação, backup/restore e pentest.
- Piloto com comprador, metas de fraude/conversão e custo por decisão.

Gate: fluxo completo avaliado com clientes e operação monitorizada.

## P3 — Auth e expansão

Passkeys, recuperação, prova fresca e consentimento para reutilização. Expandir
documento por documento e país por país. Templates biométricos têm versões e
direitos próprios; não criar busca 1:N global por omissão.
