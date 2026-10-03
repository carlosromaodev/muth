# Páginas e perfis da MUTH

SDD de produto 1.0 · 3 de Outubro de 2026. Proposta de **30 páginas principais**.
Complementa [a arquitectura](SDD.md), [a captura ao vivo](live-camera-SDD.md)
e [os contratos de identidade e aprendizagem](web-learning-SDD.md).

## Contagem e estado

| Área | Páginas | Destinatário |
| --- | ---: | --- |
| Utilizador | 6 | Pessoa que apresenta o documento e o rosto |
| Empresa | 14 | Organização que integra e utiliza a MUTH |
| Administração MUTH | 7 | Equipa operacional da plataforma |
| Acesso | 3 | Registo e acesso à área de empresa/administração |
| **Total** | **30** | |

Uma página é uma vista principal com finalidade e rota próprias. Modais,
separadores, versões mobile/desktop e passos de um wizard não aumentam a contagem.
Frente, verso e selfie são passos de uma única página de captura. Detalhes de
verificações ou identidades reutilizam uma rota parametrizada; não se contam
uma vez por cliente ou por registo.

**Estado actual:** a interface entregue é o portal de captura `/` e `/capture`,
com consentimento, fotografias, revisão e resultado no mesmo fluxo. APIs,
consentimentos, isolamento entre tenants, auditoria, retenção e aprendizagem
revista fornecem parte da fundação. Os portais completos descritos abaixo são
planeados: esta lista não significa que existam 30 páginas implementadas.
As rotas propostas ainda não constituem contratos de API.

## Utilizador — 6 páginas

O utilizador pode chegar por convite de uma empresa sem criar conta primeiro.
O convite deve dar acesso apenas à sua verificação. Uma futura área pessoal
exige um meio de acesso seguro; comparação facial inconclusiva não constitui
login nem cria automaticamente esse acesso.

| ID | Página e rota proposta | Acções e informação |
| --- | --- | --- |
| U01 | Iniciar verificação · `/verificar/:convite` | Identificar a empresa solicitante, finalidade, dados necessários, retenção e consentimentos. Explicar os fotogramas temporários antes de abrir a câmara. |
| U02 | Captura ao vivo · `/verificar/:id/captura` | Detectar enquadramento e qualidade; fotografar frente/verso na mesma câmara, mudar para selfie e rever as três imagens antes de enviar. Captura manual/upload continuam disponíveis. |
| U03 | Resultado · `/verificar/:id/resultado` | Dados documentais, campos ausentes/incertos, motivos, nota quando houver evidência e próximos passos. Modo demo e autenticidade por confirmar permanecem explícitos. |
| U04 | Minha identidade/credencial MUTH · `/minha-identidade` | Consultar o registo autorizado, estado, empresa/finalidade de origem, validade de retenção e pedidos de nova verificação. Não apresentar o perfil provisório como BI oficial ou certificado de identidade. |
| U05 | Consentimentos e dados · `/meus-dados` | Consultar autorizações, retirar contribuição, eliminar identidade, pedir acesso/exportação dos próprios dados e acompanhar o pedido. Finalidades independentes conservam controlos separados. |
| U06 | Correcção e revisão · `/verificar/:id/revisao` | Sugerir correcções com base no original, pedir revisão e acompanhar o estado. Uma proposta não altera automaticamente o OCR original ou a nota nem confirma labels biométricos. |

## Empresa — 14 páginas

Cada empresa constitui um tenant. Trocar um ID na URL ou num filtro nunca pode
dar acesso a outra empresa. Papéis propostos: proprietário, administrador,
gestor, revisor, programador e financeiro. Permissões efectivas são verificadas
pelo backend, incluindo exports; ocultar um botão não é uma autorização.

| ID | Página e rota proposta | Acções e informação |
| --- | --- | --- |
| E01 | Dashboard · `/empresa` | Volume, estados, qualidade de aquisição, tempo de processamento, custo/consumo e tarefas pendentes; filtros por período. |
| E02 | Verificações · `/empresa/verificacoes` | Pesquisar e filtrar verificações próprias, consultar estados e iniciar convites autorizados. |
| E03 | Detalhe da verificação · `/empresa/verificacoes/:id` | Evidências disponíveis, campos OCR, conflitos, versões, consentimentos, prazo e eventos. Rever/rejeitar apenas com a permissão correspondente. |
| E04 | Identidades · `/empresa/identidades` | Consultar registos autorizados, provisórios/expirados/eliminados, sem pesquisa facial 1:N nem exposição de embeddings. |
| E05 | Detalhe da identidade · `/empresa/identidades/:id` | Credencial e origem, consentimento, retenção, histórico autorizado de comparação 1:1 e eliminação. Comparação provisória não equivale a autenticação de conta. |
| E06 | Fluxos e políticas · `/empresa/fluxos` | Configurar finalidade, documentos/países efectivamente suportados, revisão e retenção. Não permitir ultrapassar gates do motor ou declarar autenticidade não implementada. |
| E07 | Fila de revisão · `/empresa/revisao` | Distribuir casos inconclusivos, registar evidência independente, decisões e motivos; distinguir revisão de negócio da revisão de labels para aprendizagem. |
| E08 | Credenciais da API · `/empresa/credenciais` | Criar, limitar, rodar e revogar chaves por finalidade/scope/ambiente. Mostrar o segredo apenas na criação; não incluir chaves em exports, URLs ou browser de captura. |
| E09 | Webhooks · `/empresa/webhooks` | Gerir destinos e assinaturas, eventos, entregas, falhas e reenvios autorizados. Assinaturas, idempotência e protecção de destinos fazem parte da implementação futura. |
| E10 | Integrações e documentação · `/empresa/integracoes` | Guias de API/SDK, ambientes, exemplos com dados sintéticos e diagnóstico de integração. |
| E11 | Relatórios e exportações · `/empresa/relatorios` | Exportar dados autorizados CSV/JSON/PDF por período, finalidade e campos; consultar pedidos, expiração de downloads e auditoria. |
| E12 | Equipa e permissões · `/empresa/equipa` | Convidar/remover membros, atribuir papéis, limitar acesso e consultar alterações. Acesso financeiro não implica leitura de documentos. |
| E13 | Consumo e facturação · `/empresa/facturacao` | Plano, consumo, limites, facturas e meios de pagamento, quando houver produto comercial e integração de pagamentos. |
| E14 | Configurações da empresa · `/empresa/configuracoes` | Dados da organização, contactos, branding de captura, políticas vigentes, prazos e preferências operacionais. |

## Administração MUTH — 7 páginas

A administração da plataforma não concede leitura irrestrita de documentos.
Acesso operacional, suporte a um tenant, revisão biométrica e gestão comercial
são permissões diferentes. Acesso excepcional a dados pessoais exige finalidade,
autorização e auditoria. Revisão de contribuições respeita o opt-in recebido.

| ID | Página e rota proposta | Acções e informação |
| --- | --- | --- |
| M01 | Visão operacional · `/muth` | Métricas agregadas, capacidade, tempos, falhas, revisão pendente e estado dos motores, sem dados pessoais por defeito. |
| M02 | Empresas · `/muth/empresas` | Gerir organizações, estado, plano e limites; suspensões/revogação com motivo e auditoria. |
| M03 | Detalhe da empresa · `/muth/empresas/:id` | Configuração, consumo e incidentes do tenant; suporte limitado e rastreável. |
| M04 | Modelos e versões · `/muth/modelos` | Manifestos, hashes, licenças, fingerprints, calibração, métricas, promoção controlada e rollback. Não promover modelos que falhem os gates. |
| M05 | Contribuições e aprendizagem · `/muth/aprendizagem` | Rever contribuições consentidas, confirmar labels com evidência independente e acompanhar datasets/partições/avaliações. Verificações isoladas não treinam nem publicam pesos automaticamente. |
| M06 | Serviços e incidentes · `/muth/operacao` | Readiness, OCR/idiomas, workers, filas, tarefas, migrações, retenção e incidentes. Logs não devem conter fotografias, tokens, campos documentais ou embeddings. |
| M07 | Auditoria e pedidos de dados · `/muth/auditoria` | Acompanhar acessos, exports, alterações administrativas e pedidos de acesso/eliminação; conservar apenas a evidência necessária aos prazos aplicáveis. |

## Acesso — 3 páginas

| ID | Página e rota proposta | Acções e informação |
| --- | --- | --- |
| A01 | Registar empresa · `/registar-empresa` | Criar organização e contacto responsável, confirmar acesso e iniciar onboarding. Não se destina a emitir BI oficial. |
| A02 | Entrar · `/entrar` | Autenticar membros da empresa/equipa MUTH e apresentar apenas áreas autorizadas; MFA/passkeys podem integrar o mesmo fluxo. |
| A03 | Recuperar acesso · `/recuperar-acesso` | Recuperação com comprovativo independente, prazo e prevenção de enumeração; não aprovar recuperação apenas com score facial experimental. |

## Exportações e credenciais

Uma exportação deve conservar o significado dos dados: fonte, versão, campos
incertos, data de processamento, limitações e estado provisório. Uma nota 0–10
não deve ser exportada como probabilidade ou confirmação oficial. CSV exige
tratamento de fórmulas; downloads têm autorização, TTL e auditoria. Exports não
incluem fotografias que o serviço não conserva, embeddings, tokens ou segredos
de integração. O conjunto de campos depende da finalidade e da permissão.

A credencial MUTH representa um registo da plataforma, com origem, consentimento
e estado. Não substitui um BI/passaporte nem confirma emissão oficial. Caso se
implemente uma credencial assinada/reutilizável, emissor, audience, expiração,
revogação e minimização de dados exigem contrato próprio antes da disponibilização.

## Ordem de implementação

1. **Captura e recuperação documental:** concluir e validar U01–U03 no fluxo
   existente, incluindo guia ao vivo, fallback e revisão. Separar rotas somente
   quando o ciclo de vida e o acesso estiverem definidos.
2. **Portal de empresa inicial:** A01–A03, E01–E03, E08, E12 e E14, com sessões de
   acesso, RBAC, isolamento entre tenants e gestão real de chaves.
3. **Operação e reutilização:** U04–U06, E04–E07, E09–E11 e M01/M04–M07, com
   contratos de convite, suporte, webhook e exportação.
4. **Comercial e expansão:** E13, M02–M03 e expansão dos fluxos/documentos,
   condicionados a operação, segurança e validação do serviço.

Cada fase deve ter SDD, critérios de aceitação, testes de autorização e evidência
funcional antes de ser apresentada como disponível.
