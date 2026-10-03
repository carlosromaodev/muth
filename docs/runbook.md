# Operação local e preparação do piloto

## Inicializar

Executar `muth init`, seguido de `muth migrate`, na pasta do projecto. O primeiro
gera configuração e chaves; o segundo aplica o schema. Se `.env` já existir,
preservá-lo e configurar os novos campos — o bootstrap recusa sobrescrever.

O servidor deve ser iniciado com `uvicorn muth.main:create_app --factory`.
O modo `disabled` recusa verificação; `demo` executa o workflow sem biometria.
OCR local é independente do modo e exige Tesseract/idiomas instalados.
Readiness 200 no modo demo indica somente que o workflow e schema estão disponíveis.
Modo `biometric` exige o extra `biometrics`, pesos preparados e
`MUTH_BIOMETRIC_MANIFEST`; ver [preparação no README](../README.md).
Readiness indica `biometric_inference_ready`, mantendo identidade completa indisponível.
`muth migrate` aplica até `0005_identities`; não iniciar um servidor com schema antigo.
`muth doctor` distingue preparação documental/OCR e runtime biométrico. Para
activar pesos já preparados, usar `muth activate-biometrics` e reiniciar a app;
o comando conserva outras definições e não promove calibração comercial.

O guia de câmara v0.7 usa OpenCV/YuNet e transmite frames apenas após permissão
explícita `capture-camera-v1`. Frames não são conservados ou usados no treino.
Consultar [SDD ao vivo](live-camera-SDD.md) para quotas, capacidade, fallback e
limites de captura. `MUTH_LIVE_CAMERA_ENABLED=false` desactiva esse guia.

## Chaves e scopes

`MUTH_TENANTS` é uma lista JSON com `tenant_id`, `key_id`, `key_sha256` e `scopes`.
Criar chaves de alta entropia, partilhar a chave apenas com o backend da empresa
e configurar o SHA-256 no servidor. Cada `key_id` é único. Para rotação, acrescentar
outra chave com o mesmo tenant e novo key_id; depois remover a antiga e reiniciar
o processo. O rate limit local é por key_id, não uma quota comercial por empresa.

Scopes: `verify`, `read`, `review`, `delete`, `audit`, `metrics`, `feedback`, `learning`.
`capture_review` é independente e permite revisão da contribuição web/gestão
administrativa de perfis; não é concedido pelo bootstrap ou pela chave legacy.
`feedback` confirma labels; `learning` lê relatórios, executa calibração e rollback.
Chaves de integração antigas devem receber esses scopes só quando necessário.
Não atribuir scopes
de revisor ou eliminação a uma integração de captura sem necessidade. A compatibilidade
`MUTH_API_KEY` recebe todos os scopes apenas no tenant `local`; preferir a lista de hashes.
Não combinar a mesma chave legacy com uma entrada de outro tenant.

## Cifragem, backup e restore

`MUTH_DATA_KEY` deve permanecer estável enquanto existirem payloads cifrados. Guardar
num secret manager do operador; perda da chave impede recuperar dados. A chave
não é cifragem de todo o ficheiro SQLite: índices, timestamps e auditoria mínima
continuam visíveis. Definir cifragem de disco e backups na infraestrutura.

Para backup SQLite, usar a API de backup do SQLite ou parar escritores e copiar
o ficheiro de forma consistente. Guardar a chave separadamente. Ensaiar restore
num ambiente isolado, aplicar migrações e verificar leitura de uma sessão sintética.
Restaurações devem reaplicar pedidos de eliminação e purge antes de liberar acesso.
Eliminação lógica não é garantia de apagamento físico em páginas SQLite ou backups.

Rotação de `MUTH_DATA_KEY` requer recifrar dados existentes; não há comando automático
de rotação nesta versão. Não trocar a chave simplesmente e esperar ler resultados antigos.

## Retenção

Configurar `MUTH_RETENTION_DAYS` e executar `muth purge` num scheduler externo, por
exemplo diariamente. A API já recusa acesso após retenção mesmo antes do job. O purge
limpa consentimento/subject, resultado, fingerprint e idempotência, deixando um
tombstone e evento mínimo. Auditoria contém identificadores internos; sua retenção
e exportação precisam de política própria antes de um piloto com dados reais.

## Recuperar processamento

Um erro do motor liberta a sessão para retry. Uma interrupção abrupta deixa um
lease; após `MUTH_PROCESSING_LEASE_SECONDS`, reenviar a mesma captura com a mesma
chave recupera a tentativa, desde que a captura não tenha expirado. Se expirou,
criar nova sessão. Respostas de tentativas antigas não podem finalizar a sessão.
Cancelamento HTTP liberta a claim mesmo se ocorrer durante a aquisição. O worker
nativo conserva a capacidade até terminar; seu token deixa de poder finalizar uma
tentativa futura. Encerramento abrupto conserva a recuperação por lease. Medir
duração e configurar recursos antes de activar modelos pesados. P2 deve introduzir
workers e cancelamento/timeouts duráveis.

## Observabilidade e rede

`/health` indica processo; `/health/ready` verifica schema e modo. `/metrics` requer
scope `metrics`. Os labels usam rotas templated e não incluem tenant, sessão ou pessoa.
Erros incluem request ID; logs de falhas de motor incluem apenas ID e tipo da excepção.

`MUTH_MAX_INFERENCE_REQUESTS=2` limita uploads biométricos e workers reais por
processo. Decodificação e inferência que sobrevivem ao cancelamento continuam
contadas. Capacidade ocupada dá 429/Retry-After 1; o rate limit dá Retry-After 60.
`MUTH_MAX_CONTROL_REQUEST_BYTES=65536` limita controlos, health e rotas desconhecidas.
O buffer é limitado mas ocupa memória; múltiplos processos multiplicam limites e
modelos. O proxy deve limitar ligações, tamanho, taxa e tempo de transporte. HTTPS,
secret manager, limites distribuídos e monitoring são requisitos da infraestrutura
do piloto. Multipart pode usar temporários: avaliar tmpfs e cifragem do disco.

## Docker e CI

Dockerfile não-root e Compose/Caddy são usados nos ensaios locais com HTTPS,
Tesseract e modelos CPU. Resultados por versão estão em
[implementation-status.md](implementation-status.md); esses ensaios não constituem
um deployment público. O GitHub Actions recusou iniciar os jobs por bloqueio de
facturação da conta; os testes locais não são apresentados como aprovação da CI.
O container recebe variáveis por configuração externa;
a `.env` é excluída do build. Aplicar migrações em job separado antes de servir.
O volume `/app/data` deve permitir escrita pelo UID 10001. Não copiar segredos para
a imagem. Validar a imagem e fixar digests de base/actions antes do deploy comercial.

## Limites de produção

Face/Liveness têm inferência CPU real; faltam PAD/IAD validado localmente, captura
vinculada à sessão, cobertura de BI,
revisão com evidências, auditoria independente ou alta disponibilidade. Consultar
os gates P1/P2 do SDD antes de tratar decisões como verificações reais.
