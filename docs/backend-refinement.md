# Refinamento biométrico e interno — v0.4

SDD 1.2 · 2 de Outubro de 2026. Complementa o [SDD biométrico](biometrics-SDD.md).
Esta iteração reforça Face/Liveness, a aprendizagem supervisionada e a operação
do backend. OCR e autenticação reutilizável continuam nos gates seguintes.

## Referências verificadas e decisões

| Fonte | Utilização no MUTH | Licença e limite |
| --- | --- | --- |
| [BSI OFIQ](https://github.com/BSI-OFIQ/OFIQ-Project/tree/d9beef8220c3829661d11e2aa3a96aef9a94a5f3) | Referência para qualidade facial e futuro benchmark de exposição/pose/resolução | MIT do código; modelos e dados têm termos próprios. Não integrado; sem alegação de conformidade ISO/IEC 29794-5 |
| [Bob measure](https://github.com/bioidiap/bob.measure/tree/b58fe757fcf59d4b1c24f3bcba1f4117a98d77ca) | Referência metodológica: não ocultar falhas de aquisição nos denominadores | BSD-3-Clause; implementação MUTH própria, sem nova dependência |
| [Bob PAD](https://github.com/bioidiap/bob.pad.base/tree/5ca1b84b35545433e2a3853bf5a70bce4ed25061) | Referência metodológica: APCER por PAI e pior categoria | GPL-3.0; apenas consulta, nenhum código copiado ou importado |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime/tree/27f3d47e38cd949539662415f3791cf9ed751cbd) | Runtime já integrado; threads intra/inter-op limitadas e modelos CPU explícitos | MIT; medir no hardware de destino, sem assumir ganho automático |
| [NIST FRVT](https://github.com/usnistgov/frvt/tree/18c2f84d80c0636cede6ff1db0479fe068aedac4) | Protocolos/interfaces como referência para avaliação facial 1:1 | Domínio público nos EUA, atribuição internacional conforme LICENSE; não constitui avaliação ou certificação NIST da MUTH |

SHAs e URLs de licença estão em [sources.json](research/sources.json). A licença
do código não substitui os direitos dos pesos, dados ou treino. Não se acrescentou
um segundo runtime pesado para cada referência encontrada.

## Requisitos implementados

| ID | Comportamento obrigatório | Evidência automatizada |
| --- | --- | --- |
| R-01 | Admissão após autorização e antes de ler o upload; ocupado recebe 429 | `test_backend_capacity.py`: corpo não lido, disconnect e libertação |
| R-02 | Decoder e inferência nativa conservam capacidade após cancelamento HTTP | Workers bloqueados em teste; novo pedido recusado enquanto continuam |
| R-03 | Cancelamento durante claim ou inferência liberta o token da sessão | `test_session_cancellation.py`: duas janelas, retry e replay reais ASGI |
| R-04 | Readiness não desfaz transacções SQLite; nested commit pertence à transacção externa | `test_database_readiness.py`: escrita pendente, duas threads, savepoint e rollback externo |
| R-05 | Modelos carregados dos bytes verificados e contratos validados no arranque | `test_biometric_quality.py`: sem reabertura de caminhos, shapes/types/probes incompatíveis |
| R-06 | Geometria, exposição, transparência e outputs inválidos causam abstention | Landmarks truncados/desordenados, imagem preta/branca, overflow e falhas nativas |
| R-07 | Validação reprovada não consulta holdout nem gasta orçamento | `test_learning.py`: relatórios sem teste, orçamento vitalício e snapshot efectiva |
| R-08 | Recapturas PAD da mesma pessoa não fabricam independência | Deduplicação por pessoa capturada, mesmo entre contas documentais |
| R-09 | Benchmark conta falhas e separa limiar diagnóstico da decisão do motor | `test_benchmark.py`: FMR/FNMR, abstention, PAD por categoria e pior PAI |
| R-10 | Dataset evita leakage detectável e relatório não publica dados individuais | Sujeitos/imagens entre splits, sujeitos contraditórios, symlinks e bytes alterados |

## Admissão e persistência

Há dois contadores por processo: pedidos biométricos admitidos e workers
síncronos activos. O primeiro limita o buffering/parser do upload; o segundo
impede ultrapassar a capacidade quando a tarefa HTTP termina antes do código
nativo. O mesmo limite, por defeito 2, aplica-se à decodificação e à inferência.
Não há fila de uploads ilimitada na aplicação. A medição RAM deve incluir buffers,
imagens expandidas e modelos; o número de pedidos não é uma garantia de consumo
de memória constante. Múltiplos processos multiplicam estes limites.

Pedidos de controlo, health e rotas desconhecidas usam no máximo 64 KiB por
pedido. Content-Length duplicado, combinado com Transfer-Encoding ou diferente
do corpo recebido é recusado. O proxy continua responsável por tempo de upload,
limites de ligações e limites distribuídos. As métricas adicionais não contêm IDs
de pessoas, empresas ou sessões:

- `muth_inference_requests_inflight`
- `muth_inference_workers_active`
- `muth_inference_capacity_rejections_total`

SQLite em memória usa uma conexão partilhada. Readiness reutiliza a sessão activa
na mesma thread e o lock nas outras, evitando rollback por fecho de uma segunda
conexão. Savepoints exigem BEGIN real antes de abrir um nested transaction quando
o driver ainda não iniciou a transacção. A aprendizagem conserva BEGIN IMMEDIATE
nos escritores que precisam de exclusão entre processos.

A claim é aguardada com uma task preservada: cancelamento durante a aquisição
não perde o token. Cleanup protegido liberta a tentativa; capacidade continua
retida pelo worker que sobreviveu. Encerramento abrupto do processo mantém a
recuperação por lease. Não existe cancelamento seguro de inferência nativa nem
suporte anunciado a PostgreSQL nesta versão.

## Integridade, qualidade e aprendizagem

OpenCV carrega YuNet/SFace dos buffers cujos hashes foram validados; ONNX Runtime
já recebia bytes. Probes de arranque e contratos de saída detectam bundles
incompatíveis. Falhas nativas devolvem inconclusivo com motivos controlados;
excepções inesperadas no adapter expõem 503 uniforme, sem mensagem interna.

Qualidade acrescenta dimensão à escala do detector, landmarks coerentes,
deslocamento relativo do nariz, clipping de exposição e recusa de transparência
parcial. Estes sinais são regras iniciais, não métricas ISO ou prova de viés
resolvido. Preprocessing `yunet-sface-landmark-quality-bgr-byte-minifas-2.7-4-v2`
e versões OpenCV/numpy/ORT entram no fingerprint. Mudanças exigem nova calibração.

Validação decide se o candidato merece consultar o teste. Relatórios guardam
`holdout_used` e apenas os membros consultados. O orçamento de teste persiste por
tenant/role/fingerprint, mesmo após revogação, expiração ou alteração do painel.
PAD identifica a pessoa capturada, evitando que a mesma captura em várias contas
conte como participantes diferentes. Feedback expirado é recusado. Estas medidas
reduzem optimismo artificial; não tornam labels independentes automaticamente.

## Protocolo de avaliação por imagem

Preparar um manifesto local `biometric-dataset-v1` a partir do
[template](../examples/biometric-dataset.example.json). Contém finalidade,
declaração local de autorização, origem de labels e ensaios com referência
pseudónima de pessoa, dispositivo, split e categoria de ataque quando aplicável.
`human_review` exige confirmação independente; `upstream_example` só é aceite
como `research_fixture`. As declarações não são verificadas independentemente.

Pessoas e bytes de imagens não atravessam calibration/validation/test. Imagens
idênticas atribuídas a sujeitos conhecidos diferentes são recusadas em datasets
consentidos. Paths absolutos, traversal e symlinks externos são recusados; leitura local
usa descritores POSIX com O_NOFOLLOW contra troca por symlink e revalida o hash antes de
inferir. Linux é a plataforma suportada para esta leitura segura. Duplicados
transformados ou pseudónimos indevidamente atribuídos precisam de revisão humana.

`muth validate-biometric-dataset` valida sem carregar pesos. O comando
`muth benchmark-biometrics` executa os motores CPU a limiares fixos fornecidos;
não treina, não calibra e nunca promove políticas. Relatórios são separados por
split e incluem grupos de dispositivo/PAI, fingerprints e latência p50/p95 das
chamadas aos motores, excluindo arranque e decoding do dataset.

As taxas faciais/PAD condicionam em scores avaliáveis. Falhas de aquisição,
qualidade, divergência e falta de score permanecem visíveis. Dois resultados
distintos impedem confundir threshold exploratório com autorização real:

- `positive_diagnostic_non_accept`: score abaixo do limiar ou diagnóstico bloqueante.
- `positive_engine_non_accept`: resultado real diferente de PASS, incluindo baseline inconclusiva.

APCER é mostrado por PAI e `worst_supported_pai_apcer` conserva o pior valor
observado, sem intervalo combinado que esconda diferenças entre ataques.
Injection/other não constituem evidência de resistência PAD suportada. A presença
de uma categoria no manifesto não prova cobertura de todos os seus ataques.

Intervalos Wilson são omitidos para fixtures públicas, pessoas desconhecidas ou
recapturas da mesma pessoa no estrato. Pessoas distintas não eliminam correlação
por ambiente/dispositivo; o relatório explicita essa limitação. Os agregados não
incluem imagens, embeddings, nomes, paths, IDs ou scores individuais.

## Gate que permanece aberto

A inferência e os controlos internos foram implementados e testados. Refinamento
biométrico para Angola requer um dataset consentido representativo, pares
genuínos/impostores, dispositivos de entrada, variações de luz e ataques físicos
confirmados. O smoke público só prova execução. RGB PAD não vincula o upload a
uma câmara fresca nem resolve injection/deepfake. A aprendizagem actual ajusta
limiares com labels confirmados; não altera pesos neurais nem melhora por previsão
auto-rotulada. OCR/Auth aguardam o fecho deste gate biométrico.
