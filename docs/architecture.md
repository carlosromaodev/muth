# Arquitectura

O desenho vigente é o [SDD](SDD.md), com requisitos e critérios de aceitação.
Face/Liveness e aprendizagem seguem [biometrics-SDD.md](biometrics-SDD.md).

```text
src/muth/
├── main.py              # App factory, handlers e readiness
├── api.py               # Rotas de sessões, engines e contrato Auth
├── config.py            # Tenant keys, limites e configuração
├── domain.py            # Evidência, consentimento, sessões e estados
├── security.py          # Principal derivado da chave + scopes
├── middleware.py        # Limite de corpo, rate limit e métricas
├── media.py             # Validação de imagens em thread
├── storage.py           # Transacções, cifragem e auditoria
├── learning.py          # Scores consentidos, labels, calibração e revogação
├── migrations/          # Schema Alembic versionado
├── engines/             # Bundle e contratos Face/Liveness/ID
├── services/            # Orquestração e decisão
├── evaluation.py        # Manifesto e métricas faciais offline
└── cli.py               # Bootstrap, migrate, purge e avaliação
```

MUTH ID deve devolver um retrato extraído para comparação 1:1. Sem retrato ou
motor, os resultados são inconclusivos. O bundle pode ser injectado para testes;
v0.3 integra YuNet/SFace e MiniFASNet, mantendo demo separado. Cada verificação
usa uma versão de calibração por empresa/modelo/dispositivo. Pesos não se alteram
durante a verificação; thresholds não calibrados abstêm. ID não prova autenticidade.

SQLAlchemy suporta a configuração SQLite validada nesta fase. A aplicação não
aplica migrações à DB persistente automaticamente; readiness e rotas de sessão
recusam operar sem schema correcto. O CLI utiliza as migrações incluídas no pacote.
