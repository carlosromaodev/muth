# Operação local e preparação do piloto

## Inicializar

Executar `muth init`, seguido de `muth migrate`, na pasta do projecto. O primeiro
gera configuração e chaves; o segundo aplica o schema. Se `.env` já existir,
preservá-lo e configurar os novos campos — o bootstrap recusa sobrescrever.

O servidor deve ser iniciado com `uvicorn muth.main:create_app --factory`.
O modo `disabled` recusa verificação; `demo` executa o workflow sem biometria/OCR.
Readiness 200 no modo demo indica somente que o workflow e schema estão disponíveis.
Modo `biometric` exige o extra `biometrics`, pesos preparados e
`MUTH_BIOMETRIC_MANIFEST`; ver [preparação no README](../README.md).
Readiness indica `biometric_inference_ready`, mantendo identidade completa indisponível.
`muth migrate` aplica também `0002_learning`; não iniciar um servidor com schema antigo.

## Chaves e scopes

`MUTH_TENANTS` é uma lista JSON com `tenant_id`, `key_id`, `key_sha256` e `scopes`.
Criar chaves de alta entropia, partilhar a chave apenas com o backend da empresa
e configurar o SHA-256 no servidor. Cada `key_id` é único. Para rotação, acrescentar
outra chave com o mesmo tenant e novo key_id; depois remover a antiga e reiniciar
o processo. O rate limit local é por key_id, não uma quota comercial por empresa.

Scopes: `verify`, `read`, `review`, `delete`, `audit`, `metrics`, `feedback`, `learning`.
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
O lease não cancela a inferência antiga: medir duração e configurar recursos antes
de activar modelos pesados. P2 deve introduzir workers e cancelamento/timeouts duráveis.

## Observabilidade e rede

`/health` indica processo; `/health/ready` verifica schema e modo. `/metrics` requer
scope `metrics`. Os labels usam rotas templated e não incluem tenant, sessão ou pessoa.
Erros incluem request ID; logs de falhas de motor incluem apenas ID e tipo da excepção.

Limites de corpo da aplicação são por pedido. O buffer é limitado mas ocupa memória;
o proxy deve limitar concorrência, tamanho, taxa e tempo de transporte. HTTPS,
secret manager, limites distribuídos e monitoring são requisitos da infraestrutura
do piloto. Multipart pode usar temporários: avaliar tmpfs e cifragem do disco.

## Docker e CI

Dockerfile não-root e CI são configurações de referência. Não foram executados
remotamente nesta iteração. O container recebe variáveis por configuração externa;
a `.env` é excluída do build. Aplicar migrações em job separado antes de servir.
O volume `/app/data` deve permitir escrita pelo UID 10001. Não copiar segredos para
a imagem. Validar a imagem e fixar digests de base/actions antes do deploy comercial.

## Limites de produção

Sem motores reais, PAD/IAD validado, captura vinculada à sessão, cobertura de BI,
revisão com evidências, auditoria independente ou alta disponibilidade. Consultar
os gates P1/P2 do SDD antes de tratar decisões como verificações reais.
