# Repositórios candidatos e decisões

Consulta de GitHub e READMEs oficiais em 2 de Outubro de 2026. Os SHAs e estado
observados estão em [sources.json](research/sources.json). Estrelas não são evidência
de segurança, adequação a Angola ou licença dos pesos.

Na v0.4, a pesquisa acrescentou OFIQ, Bob measure/PAD e NIST FRVT para orientar
qualidade e protocolos de avaliação, sem novas dependências pesadas. ONNX Runtime
foi consultado para controlo de threads. SHAs, licenças e decisões estão no
[refinamento do backend](backend-refinement.md). Bob PAD é GPL-3.0 e só foi usado
como referência metodológica; nenhum código copiado ou importado.

| Repositório | Papel | Licença de código observada | Decisão |
| --- | --- | --- | --- |
| [InsightFace](https://github.com/deepinsight/insightface) | Face, alinhamento, embeddings e liveness opcional | README declara MIT; API não identificou SPDX | Candidato P1; resolver direitos dos pesos e benchmark |
| [Silent-Face](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing) | PAD RGB / MiniFASNet | Apache-2.0 | Benchmark, não dependência obrigatória |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | OCR e análise documental | Apache-2.0 | Primeiro candidato MUTH ID; benchmark de campos do BI |
| [docTR](https://github.com/mindee/doctr) | Alternativa OCR | Apache-2.0 | Comparação em laboratório; escolher um motor inicialmente |
| [DeepFace](https://github.com/serengil/deepface) | Comparação de arquitecturas | MIT | Laboratório; modelos internos têm direitos próprios |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | Inferência CPU/GPU | MIT | Candidato a runtime para pesos validados |
| [OpenCV](https://github.com/opencv/opencv) | Perspectiva, qualidade e recortes | Apache-2.0 | Candidato ao pipeline de imagem P1 |
| [py_webauthn](https://github.com/duo-labs/py_webauthn) | Passkeys | BSD-3-Clause | Candidato MUTH Auth; exige origem, RP ID e challenges |
| [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) | Persistência transaccional | MIT | Integrado P0 |
| [Alembic](https://github.com/sqlalchemy/alembic) | Migrações | MIT | Integrado P0 |
| [cryptography](https://github.com/pyca/cryptography) | Cifragem autenticada | Apache-2.0 ou BSD, conforme LICENSE | Integrado P0; Fernet, sem algoritmo próprio |
| [Prometheus Python](https://github.com/prometheus/client_python) | Métricas operacionais | Apache-2.0 | Integrado P0 |
| [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) | Tracing | Apache-2.0 | P2 quando existir exportador definido |
| [Temporal Python](https://github.com/temporalio/sdk-python) | Workflows duráveis | MIT | P2 se o fluxo passar a assíncrono |
| [mrz](https://github.com/Arg0s1080/mrz) | Parsing/checks de MRZ | GPL-3.0 | Não integrar sem decisão sobre copyleft; não autentica passaporte |
| [Smile ID Python SDK](https://github.com/smileidentity/smileid-sdk-python) | Integração comercial de referência | Consultar licença e contrato | SDK de serviço, não motor biométrico aberto |

## Correcção verificada sobre InsightFace

O README consultado confirma InsightFace 2.0, servidor e addon de liveness RGB,
com actualização datada de 9 de Setembro de 2026. A proposta anterior tinha esse
ponto pendente; a fonte agora foi consultada. Isso não comprova desempenho no MUTH.

O README distingue licença MIT do código de modelos/dados para investigação não
comercial e fornece contactos para licença comercial de reconhecimento. Não activar
`buffalo_l` automaticamente em produção. O manifesto MUTH deve registar os termos
dos pesos específicos, sua integridade e a evidência da revisão de licença.

## Critérios de selecção

1. Direitos de utilização do código, pesos e dados verificados separadamente.
2. Inferência reprodutível sem downloads implícitos e com checksum/versionamento.
3. Métricas no cenário local e ataques representativos, com incerteza estatística.
4. Latência e custo CPU/GPU, quantidade de memória e comportamento de falha.
5. Contrato estável, manutenção e possibilidade de substituir o motor.

Resultados de pesquisas genéricas também incluíram repositórios de demonstração
de SDKs comerciais e implementações antigas. Não foram escolhidos por parecerem
“open source”; é necessário confirmar que oferecem implementação utilizável e
direitos compatíveis, e não apenas uma interface para software proprietário.
