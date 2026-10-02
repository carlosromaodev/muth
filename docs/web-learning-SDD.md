# OCR, identidade guardada e aprendizagem web — MUTH v0.6

SDD 1.4 · 2 de Outubro de 2026. Complementa [captura](capture-SDD.md) e
[biometria](biometrics-SDD.md). Migração corrente: `0005_identities`.

## Objectivo e resultado

Consentimentos → frente → verso → selfie → revisão → resultado com dados extraídos
do documento e nota 0–10. OCR é leitura de texto, não prova de autenticidade.
Campos ausentes não são inventados; conflitos entre lados são apresentados para
revisão. Datas/números passam validações estruturais. Checks MRZ validam a
estrutura e os dígitos, sem consulta à autoridade emissora.

O motor inicial é Tesseract local, com português/inglês e contrato substituível.
Timeout, limites de imagem/saída, execução sem shell e eliminação dos temporários
fazem parte do contrato. Sem motor/linguagem/evidência suficiente, o resultado
declara indisponibilidade ou leitura parcial. Nenhum documento vai para terceiros.
O resultado `document_data` distingue extracted/partial/unavailable e inclui
campos suportados, origem OCR/MRZ, lado, confiança textual, validação estrutural,
conflitos e motivos. Nome, número, nascimento, validade, sexo, nacionalidade e
filiação são opcionais. Confiança OCR não é probabilidade de identidade.
`document_data.processing_version` é `muth-document-ocr-v2`, distinto da versão
do executável Tesseract, e permite rastrear mudanças no processamento/extracção.

Tesseract recebe argumentos sem shell, tem deadline de 12 segundos por defeito,
saída limitada e directório temporário privado eliminado no fim. Instalação Debian:
`apt-get install tesseract-ocr tesseract-ocr-por tesseract-ocr-eng`. Docker inclui
os mesmos pacotes. O default é `por+eng`; se só existir inglês, o fallback declara
`ocr_language_fallback_eng`. Não há download de idiomas no pedido nem OCR remoto.

O processamento começa em PSM 6 e tenta PSM 11 para layout esparso quando a
primeira leitura tem campos inválidos, menos de três campos ou, na frente,
faltam nome/número/nascimento/validade/sexo. O fallback é adaptativo, não executado
em toda a imagem. Frente, verso e tentativas partilham o deadline/orçamento de
saída. Uma leitura validada pode recuperar um campo inválido; duas leituras
plausíveis diferentes produzem conflito e não promovem um valor silenciosamente.

## Consentimentos independentes

| Propósito | Política por defeito | Retenção por defeito | Necessário para verificar |
| --- | --- | --- | --- |
| Verificação | `capture-privacy-v1` | Resultado: 1 dia | Sim |
| Contribuir para aprendizagem | `capture-learning-v1` | Fila e resultado: 30 dias | Não |
| Guardar identidade reutilizável | `identity-enrollment-v1` | Perfil: 365 dias | Não |

Opt-ins opcionais começam desmarcados. Backend confirma `true` estrito e versão
da política; recusa inscrição de identidade em demo/sem runtime biométrico.
Uma autorização não implica as outras. Imagens ficam transitórias mesmo quando
o utilizador contribui ou guarda identidade. O resultado expõe separadamente
`learning` e `identity`, incluindo indisponibilidade e prazos efectivos.

## Aprendizagem consentida

Opt-in independente e inicialmente desmarcado. A verificação funciona sem
contribuir. Com autorização, scores/model fingerprints e dados/correcções OCR
são conservados cifrados numa fila por sessão; a fila não conserva imagens ou
embeddings. A retenção inicial é 30 dias para a contribuição e respectivo
resultado; sem opt-in, um dia. Ambos os prazos são configuráveis e apresentados.

O navegador pode sugerir correcções dos campos permitidos. Não pode confirmar
rótulos biométricos, escolher referências de sujeitos ou promover modelos.
Uma contribuição pendente não participa na calibração. Um revisor autorizado
com scope explícito `capture_review` declara evidência independente e referência
estável pseudónima com namespace; números/nome extraídos por OCR não viram
automaticamente identidades confirmadas. A declaração do revisor exige processo
operacional; uma caixa assinalada não comprova acesso a um registo oficial.

Rótulos humanos confirmados alimentam o motor existente: partições por pessoa,
validação antes do teste, intervalos de erro, limites de regressão, orçamento de
holdout, versões e rollback. O worker inclui o tenant reservado da captura.
Refinamento automático aqui significa calibração de limiares sobre scores
revistos; não treino de pesos neurais. Correcções OCR conservam exemplos revistos
para avaliar a extracção; não produzem memorização automática de dados pessoais.
Nenhuma promessa de melhoria a cada captura substitui os gates de avaliação.

A revisão exige `independent_evidence_confirmed=true`, `source_reference` opaca,
`subject_reference` canónica (`namespace:referencia`) e pelo menos campos ou
label confirmada. Face aceita genuine/impostor; impostor exige sujeito da captura
distinto e confirmado. PAD aceita live/spoof; spoof exige tipo de ataque. Scores,
thresholds e fingerprints são obtidos do resultado cifrado, não do revisor.
Revisão repetida igual é idempotente; alteração de revisão confirmada é conflito.

## Perfil facial e credencial

Um embedding é um template numérico do rosto, não um algoritmo treinado para
cada pessoa. O modelo comum SFace produz 128 valores normalizados; o perfil guarda
este vector, fingerprint SHA-256 do modelo, campos documentais originais e
consentimento `identity_enrollment` num payload Fernet cifrado. O perfil tem ID
aleatório, tenant e sessão de origem; há no máximo um perfil por tenant/sessão.
O serviço valida dimensão, valores finitos e norma antes da persistência.
`identity_max_profiles` (`MUTH_IDENTITY_MAX_PROFILES`) limita a 10 000 perfis
activos por tenant por defeito. Perfis eliminados/expirados não contam; quota e
inscrição são transaccionais. Acima do limite, novas inscrições recebem
429/`identity_profile_limit`, sem substituir perfis existentes.

Só uma captura com runtime real, consentimento vigente, dados OCR disponíveis e
sinais suficientes pode inscrever um perfil. Face/PAD devem ter scores válidos,
sem falhas nem divergência PAD: limites experimentais mínimos de coseno 0,363 e
softmax 0,8, além dos limiares aplicáveis ao motor. Não são taxas de precisão
validadas. Persistência exige fingerprint e dados iguais ao resultado original;
correcções propostas não alteram a credencial guardada silenciosamente.

Todos os perfis ficam **provisional**, `authenticity_confirmed=false`. Consulta
devolve campos/prazos, nunca vector, hash estável de pessoa ou aprovação. A resposta
da captura emite capability Fernet derivada para finalidade de identidade, limitada
ao perfil/tenant/prazo. Só fica em memória; não serve em `/capture-api` ou `/v1`.

Comparação 1:1 recebe apenas nova selfie, aplica Face/PAD e exige fingerprint
compatível. Não há pesquisa 1:N de pessoas nem actualização automática do template
com selfies futuras. Resultado é `authenticated=false`, mesmo com scores altos.
Um protocolo de login com anti-replay, identidade documental validada e calibração
local permanece fora desta entrega. `/v1/auth/authenticate` é endpoint de operador
para a mesma comparação provisória, sem equivalência a login pronto.

## Contratos e autorização

| Operação | Contrato |
| --- | --- |
| Criar captura | Consentimento obrigatório de verificação e opt-ins/políticas independentes de aprendizagem/perfil |
| Verificar | Dados OCR, nota indicativa e estados de contribuição/perfil |
| `POST /capture-api/sessions/{id}/document-corrections` | Bearer da mesma sessão; propostas bounded, sem rótulos/subjects |
| `DELETE /capture-api/sessions/{id}/learning-consent` | Retira contribuição sem apagar o resultado da verificação |
| `GET /v1/capture-learning` | Fila pending_review, limit 1–100; operador capture_review |
| `POST /v1/capture-learning/{id}/review` | Revisão independente e labels; operador capture_review |
| `GET /v1/capture-learning/status`, `POST /v1/capture-learning/refine` | Estado/refinamento do tenant reservado; operador capture_review |
| `GET /identity-api/identities/{id}` | Capability própria; perfil público sem vector |
| `DELETE /identity-api/identities/{id}` | Capability própria; scrub do perfil e consentimento |
| `POST /identity-api/identities/{id}/compare` | Capability própria; multipart só selfie |
| `GET/DELETE /v1/capture-identities/{id}` | Gestão por operador capture_review |
| `POST /v1/auth/authenticate?identity_id=...` | Operador capture_review; comparação 1:1 provisória |

Tenant reservado continua sem chaves B2B próprias. O scope de revisão é uma
permissão administrativa global, não é atribuído por `muth init` nem à chave
legacy. Origem, JSON/multipart, autorização antes de leitura, quotas, deadline,
idempotência e capacidade continuam a seguir o SDD de captura.
Quota por defeito: 10 000 sessões activas. A capability de captura permite
consulta/retirada/eliminação até à retenção; novos uploads terminam ao fim dos
30 minutos da sessão. Autorização compara também o estado/prazo real da DB,
incluindo os prazos reduzidos por retirada.

## Ciclo de vida e validação

Resultados OCR, fila e templates cifrados. Delete/purge/withdraw da contribuição
eliminam contribuições/amostras derivadas, revogando calibrações dependentes na
mesma transacção. Withdraw conserva a extracção original até ao menor prazo entre
a retenção anterior e um dia, por defeito, a contar da retirada; propostas
não alteram o resultado original silenciosamente. Referências/valores OCR não
aparecem em logs, métricas, mensagens de erro ou relatórios públicos de execução.

Perfil tem ciclo de vida independente: expiração normal do resultado original
não o elimina; retenção de perfil de 365 dias termina com scrub automático.
DELETE explícito da sessão elimina também os seus perfis derivados. DELETE do
perfil não apaga a verificação nem a contribuição, mas impede reinscrição por
replay da sessão. Withdraw de aprendizagem conserva o perfil autorizado em
separado. Reiniciar a interface para nova captura conserva contribuição/perfil
autorizados; o utilizador pode retirar/eliminar explicitamente no resultado.
Tokens em memória são descartados ao fechar/recarregar; o ID por si só não concede
acesso. O operador autorizado pode gerir um perfil pelo ID.

## Configuração e aceitação

`MUTH_CAPTURE_LEARNING_ENABLED` e `MUTH_LEARNING_ENABLED` controlam contribuição;
`MUTH_CAPTURE_LEARNING_RETENTION_DAYS=30` controla o prazo. OCR usa
`MUTH_DOCUMENT_OCR_ENABLED`, `MUTH_DOCUMENT_OCR_EXECUTABLE`,
`MUTH_DOCUMENT_OCR_LANGUAGES=por+eng` e `MUTH_DOCUMENT_OCR_TIMEOUT_SECONDS=12`.
Perfil usa `MUTH_IDENTITY_ENROLLMENT_ENABLED`,
`MUTH_IDENTITY_ENROLLMENT_POLICY_VERSION` e `MUTH_IDENTITY_RETENTION_DAYS=365`.
`MUTH_IDENTITY_MAX_PROFILES=10000` controla a quota de perfis por tenant,
independentemente de `MUTH_CAPTURE_MAX_SESSIONS`.
Todos os consentimentos devem apresentar os prazos configurados ao utilizador.

Critérios: OCR real de documento sintético marcado; validação de conflitos/MRZ;
erro/timeout/motor ausente; opt-in obrigatório; sugestões sem promoção; revisão
administrativa; isolamento; rollback atómico; revogação e retenção; navegador
com dados reais devolvidos pela API, nota e formulário de correcções. Perfil:
opt-in independente, template normalizado cifrado, ausência de vectores na API,
token cruzado/expirado, fingerprint incompatível, compare 1:1, DELETE/purge e
retenção independente. Demo nunca inscreve nem autentica uma identidade.
Dados sintéticos avaliam a integração, não a precisão em BI angolanos reais.
O [relatório de implementação](implementation-status.md) conserva evidência
executada e distingue browser/câmaras simuladas de piloto físico/externo.
