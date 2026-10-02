# MUTH

Infraestrutura africana de identidade digital, com foco inicial no BI angolano.
Versão v0.5 em Python 3.12/FastAPI, seguindo o [SDD](docs/SDD.md) e o
[SDD biométrico](docs/biometrics-SDD.md).

**Face e Liveness executam modelos reais em CPU.** YuNet/SFace e MiniFASNet ONNX
estão integrados; thresholds não calibrados devolvem inconclusivo. OCR/autenticidade
documental não estão implementados. A calibração automática usa apenas scores
autorizados e labels humanos confirmados, sem alterar pesos neurais. Não existe
dataset angolano avaliado nem prontidão comercial comprovada.

## Executar localmente

```bash
cd /home/carlos/Documentos/project/muth
UV_CACHE_DIR=/tmp/muth-uv-cache uv sync --locked --extra biometrics
.venv/bin/muth init
.venv/bin/muth migrate
.venv/bin/uvicorn muth.main:create_app --factory --host 127.0.0.1 --port 8000
```

`muth init` cria `.env` com chaves aleatórias e permissões 0600. Não sobrescreve
configuração existente e não imprime segredos. O processo lê `.env` automaticamente.
Documentação interativa: <http://127.0.0.1:8000/docs>.

## Captura mobile e web

Abrir <http://127.0.0.1:8000> para consentimento, frente/verso do documento,
selfie, revisão e resultado. Câmara e fotografias são escolhidas pelo utilizador;
nenhuma chave B2B vai para o browser. O mesmo servidor fornece interface e API.
Aplicar `muth migrate` para a migração `0003_capture` antes de iniciar a v0.5.

A nota 0–10 mede sinais disponíveis, com **teto actual de 6/10** e autenticidade
por confirmar. Demo/falta de evidência mostra nota indisponível. Não confirma
oficialmente documentos nem garante uma pessoa real. A captura do verso analisa
legibilidade; OCR/autenticidade continuam por implementar.

No telemóvel, a câmara JavaScript precisa de HTTPS; localhost funciona no próprio
dispositivo. [Portal, segurança e configuração HTTPS](docs/capture-SDD.md).
`MUTH_CAPTURE_PORTAL_ENABLED=false` desactiva os endpoints públicos de captura.
Resultados do portal têm retenção de um dia, limpeza periódica e eliminação por
sessão; imagens/tokens não são guardados no browser e imagens não são persistidas
pelo serviço. O fluxo público não participa na aprendizagem.

## Activar os motores biométricos

Preparação explícita dos pesos públicos e exportação CPU:

```bash
UV_CACHE_DIR=/tmp/muth-uv-cache uv sync --locked --extra biometrics --group export
.venv/bin/python scripts/fetch_biometrics.py
.venv/bin/python scripts/export_liveness.py
```

No `.env`, definir `MUTH_ENGINE_MODE=biometric` e
`MUTH_BIOMETRIC_MANIFEST=models/biometrics/manifest.json`, mantendo as chaves locais.
Aplicar `.venv/bin/muth migrate` antes de iniciar a app factory. Numa instalação
nova, `muth init` começa em demo até activar explicitamente o bundle. Credenciais,
dados de verificação, ambientes virtuais e pesos não são distribuídos pelo GitHub;
a configuração e os modelos devem ser preparados na máquina de destino.

O manifesto declara uso research e verifica SHA-256 antes de carregar os modelos.
Fontes e licenças ficam em `models/biometrics/sources.json` e `vendor/`; não existem
downloads na API. O servidor precisa apenas de `uv sync --locked --extra biometrics`,
sem o grupo pesado de exportação. Docker instala o extra; montar pesos verificados
e fornecer o caminho do manifesto. Pesos não entram no wheel/container por omissão.

Smoke com exemplos públicos e comparação PyTorch/ONNX:

```bash
.venv/bin/python scripts/biometric_smoke.py --fetch-public-samples
MUTH_TEST_BIOMETRICS=1 .venv/bin/python -m unittest discover -s tests -v
```

O smoke grava `data/evaluation/biometric-smoke.json`; não mede precisão local.

Todas as operações `/v1` e `/metrics` exigem `X-API-Key` e o scope correspondente.
As chaves são de **backend para backend**; não as inserir num frontend público.
Sem `.env`, a DB é efémera, os motores ficam desactivados e nenhuma chave é aceite.

## Fluxo com sessões

Para os exemplos locais, carregar apenas a configuração gerada por `muth init`:

```bash
set -a
source .env
set +a
curl http://127.0.0.1:8000/v1/sessions \
  -H "X-API-Key: $MUTH_LOCAL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"consent":{"accepted":true,"purpose":"onboarding","policy_version":"privacy-v1"},"subject_reference":"cliente-teste"}'
```

Usar o `session_id` devolvido para enviar imagens **sintéticas** JPEG/PNG:

```bash
curl http://127.0.0.1:8000/v1/sessions/SESSION_ID/verify \
  -H "X-API-Key: $MUTH_LOCAL_API_KEY" \
  -H 'Idempotency-Key: tentativa-001' \
  -F 'document=@/caminho/documento-sintetico.png' \
  -F 'selfie=@/caminho/selfie-sintetica.png'

curl http://127.0.0.1:8000/v1/sessions/SESSION_ID \
  -H "X-API-Key: $MUTH_LOCAL_API_KEY"
```

A repetição da mesma chave e imagens retorna o resultado persistido. Payload
diferente com a mesma chave recebe 409. A sessão identifica a declaração de
consentimento recebida e o resultado; não prova que a captura veio de uma câmara.

## Implementado

- Sessões persistentes, consentimento versionado, TTL e idempotência.
- Isolamento entre empresas, chaves por hash e scopes.
- Resultados e dados de sessão cifrados com Fernet; nenhuma imagem ou embedding persistido.
- Claims atómicos com lease, recuperação e bloqueio de resultados obsoletos.
- Rejeição manual, eliminação, purge e auditoria mínima.
- Bundle único para Face/Liveness/ID, sem provider fixo nas rotas.
- Limites de corpo antes do parser, validação de imagem em thread e erros uniformes.
- Métricas Prometheus, request IDs, Alembic, CLI, testes e configuração de CI.
- Validação offline de manifestos e relatório de FMR/FNMR com intervalos Wilson.
- Detecção/alinhamento/embedding e comparação facial 1:1 com qualidade e abstention.
- PAD RGB com dois modelos, preprocessing verificado e diagnóstico de divergência.
- Opt-in de aprendizagem, feedback por revisor e partições separadas por pessoa.
- Calibração automática, gates de erro/regressão, versões, rollback e revogação.
- Admissão antes do upload e limite de workers, incluindo decodificação cancelada.
- Carregamento dos bytes verificados, contratos dos modelos e qualidade geométrica/exposição.
- Benchmark CPU por imagem, dispositivo e ataque, com abstentions e pior APCER por PAI.

## Aprendizagem supervisionada

Na criação da sessão, adicionar `learning_opt_in:true` ao consentimento, fornecer
uma referência estável pseudónima e `device_group` quando conhecido. Sem opt-in,
a verificação não gera amostras de aprendizagem. Um revisor autorizado confirma
`genuine/impostor` ou `live/spoof` através do endpoint de feedback. Score vem do
motor; o pedido de feedback não aceita score nem threshold.

Exemplo de corpo de feedback para Face:

```json
{"role":"face","label":"genuine","source":"human_review","source_reference":"review-opaque-001"}
```

O worker avalia a cada 300 segundos, configurável. `POST /v1/learning/refine`
permite executar imediatamente; `GET /v1/learning` mostra amostras e relatórios.
Dados insuficientes dão `collecting`; uma versão só substitui a anterior quando
passa gates e melhora métricas. A recolha não garante melhoria a cada verificação.
Labels devem vir de evidência independente; scores não servem como seus próprios labels.
O teste final só é consultado depois de passar a validação. Cada fingerprint tem
um orçamento limitado de consultas ao teste; novas amostras não o reiniciam.

Por defeito são necessários ≥50 exemplos por classe na calibração e ≥400 por
classe na validação e no teste, com pessoas separadas. Retirar consentimento em
`DELETE /v1/sessions/{id}/learning-consent` apaga exemplos e revoga versões
dependentes, conservando a verificação. [Critérios completos](docs/biometrics-SDD.md).

## Endpoints principais

| Endpoint | Scope |
| --- | --- |
| `POST /v1/sessions` | `verify` |
| `GET /v1/sessions/{id}` | `read` |
| `POST /v1/sessions/{id}/verify` | `verify` |
| `POST /v1/sessions/{id}/review` | `review` |
| `DELETE /v1/sessions/{id}` | `delete` |
| `GET /v1/sessions/{id}/events` | `audit` |
| `POST /v1/sessions/{id}/feedback` | `feedback` |
| `DELETE /v1/sessions/{id}/learning-consent` | `delete` |
| `GET /v1/learning`, `POST /v1/learning/refine`, `/v1/learning/{role}/rollback` | `learning` |
| `POST /v1/faces/compare`, `/v1/liveness`, `/v1/documents/analyze` | `verify` |
| `POST /v1/verifications` (efémero, compatibilidade) | `verify` |
| `POST /v1/auth/authenticate` (reservado, 501) | `verify` |
| `GET /metrics` | `metrics` |

`GET /health` e `/health/ready` são públicos. Readiness verifica o schema e
declara explicitamente `identity_verification_ready=false`.

## Validar

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
UV_CACHE_DIR=/tmp/muth-uv-cache uv build
```

Benchmark sintético da ferramenta, sem valor de precisão biométrica:

```bash
.venv/bin/muth evaluate-face examples/face-pairs.synthetic.csv --threshold 0.5
.venv/bin/muth validate-model /caminho/manifest.json --require-commercial
```

O manifesto de exemplo tem placeholders e deve falhar até serem fornecidos pesos,
checksums e evidência de revisão. A validação não emite aprovação jurídica.

Para avaliar **imagens rotuladas** com os motores reais, preparar um dataset local
a partir de [biometric-dataset.example.json](examples/biometric-dataset.example.json):

```bash
.venv/bin/muth validate-biometric-dataset /caminho/dataset/manifest.json
.venv/bin/muth benchmark-biometrics /caminho/dataset/manifest.json \
  --model-manifest models/biometrics/manifest.json \
  --face-threshold 0.363 --liveness-threshold 0.8 \
  --output data/evaluation/biometric-benchmark.json
```

O benchmark conserva limiares fixos e separa calibração, validação e teste. Mostra
FMR/FNMR, APCER por ataque, BPCER, aquisição/qualidade e latência, sem promover
políticas. Declarações de autorização exigem evidência local e não são verificadas
independentemente pela ferramenta. [Protocolo e melhorias](docs/backend-refinement.md).

Por defeito, `MUTH_MAX_INFERENCE_REQUESTS=2` limita pedidos biométricos por processo;
capacidade ocupada devolve 429 com `Retry-After: 1` antes de ler o corpo. Workers de
decodificação/inferência conservam o limite mesmo após cancelamento HTTP. Pedidos
de controlo têm limite de 64 KiB (`MUTH_MAX_CONTROL_REQUEST_BYTES`). Ajustar recursos
e limites do proxy através de medições no hardware de destino.

## Documentação

- [SDD e critérios de aceitação](docs/SDD.md)
- [Análise de mercado](docs/market-analysis.md)
- [Repositórios e licenças](docs/repository-assessment.md)
- [Fontes e SHAs consultados](docs/research/sources.json)
- [Operação e recuperação](docs/runbook.md)
- [Resultado da implementação](docs/implementation-status.md)
- [Alterações de compatibilidade](CHANGELOG.md)

O código está publicado em <https://github.com/carlosromaodev/muth>. Dockerfile e
workflow CI são fornecidos; não houve deploy. A execução do GitHub Actions foi
bloqueada pelo GitHub por um problema de facturação da conta; validação local fica
registada na documentação. O [roadmap](docs/roadmap.md) define os gates para piloto real.
