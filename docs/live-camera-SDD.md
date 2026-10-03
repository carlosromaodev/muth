# Guia de câmara em tempo real — MUTH v0.7

SDD 1.6 · 3 de Outubro de 2026. Complementa
[captura](capture-SDD.md) e [recuperação OCR](ocr-recovery-SDD.md).
Este documento define a nova funcionalidade; os resultados de teste v0.6.1 não
validam o guia de câmara. A verificação v0.7 é registada separadamente abaixo.

## Resultado pretendido e limites

Depois de autorizar a verificação e abrir a câmara por gesto, o utilizador recebe
orientação sobre o cartão/rosto detectado, enquadramento, qualidade e estabilidade.
Três avaliações consecutivas adequadas durante pelo menos 1,4 segundos permitem
capturar automaticamente. Frente e verso usam a mesma câmara traseira; antes de
aceitar o verso, o guia exige mudança visual em relação à frente. Depois do
verso, fecha a câmara traseira e abre a frontal para a selfie. A revisão continua
obrigatória: as fotografias finais só seguem para verificação quando o utilizador
as envia explicitamente.

Detecção geométrica e qualidade não confirmam autenticidade, validade do BI,
lado oficial do cartão ou presença de uma pessoa viva. Uma mudança visual não
prova que a imagem seja o verso. A detecção facial não faz face match, PAD ou
inscrição. A avaliação documental usa OpenCV; a facial usa YuNet do runtime
biométrico quando disponível. Demo pode orientar um cartão, mas não passa a ter
biometria real por activar este guia.

## Fluxo de interacção e consentimento

```text
Consentimento de verificação e explicação dos frames em tempo real
  → gesto Abrir câmara
  → documento frente: detectar → orientar → estabilidade → guardar foto local
  → documento verso: mesma stream → mudança visual → estabilidade → foto local
  → fechar stream traseira → abrir nova stream frontal → selfie/fallback
  → fechar stream → revisão das três fotografias
  → envio explícito da verificação
```

O texto apresentado antes de abrir a câmara deve explicar que, enquanto está
activa, imagens pequenas são transmitidas ao **servidor MUTH** para orientação.
Esta transmissão acontece antes do envio final da verificação. Frames não são
enviados a terceiros, persistidos, usados para treino ou conservados em métricas.
Consentimento de verificação não marca os opt-ins de aprendizagem ou identidade.
O consentimento geral mantém `capture-privacy-v1` por compatibilidade; a permissão
de frames é explícita e tem política própria `capture-camera-v1`.

`Consent`/`BrowserConsent` acrescentam `camera_frames_opt_in`, booleano estrito
com default `false`, e `camera_policy_version`. Quando a interface autoriza o
guia live com o consentimento apresentado e gesto de câmara, envia
`camera_frames_opt_in=true` e a versão vigente. O servidor valida essa versão e
guarda a permissão cifrada no consentimento da sessão. A criação devolve
`camera_frames_enabled`; configuração publica `live_camera_policy_version`.
Abrir a câmara ou aceitar uma sessão antiga não cria esta permissão implicitamente.
Uma sessão só com ficheiros ou criada sem os campos novos continua sem autorização
para transmitir frames. Isto não selecciona os opt-ins de aprendizagem/perfil.

Não há abertura automática da câmara ao entrar na página. Cada início depende de
gesto explícito e permissão do navegador. Permissão negada, câmara indisponível,
modelo facial ausente, avaliação indisponível, limite de pedidos ou qualidade
insuficiente conservam captura manual/upload com instrução clara. O guia não
bloqueia o fluxo por falta de detecção automática.

Ao mudar frente→verso, a stream traseira continua aberta e a interface orienta
virar o documento. Uma assinatura visual compacta é guardada apenas na memória
local para exigir diferença do verso. Back→selfie fecha todas as tracks e exige
nova `getUserMedia` com `facingMode: user`; não reutiliza um track traseiro como
se fosse frontal. A aplicação deve tratar browsers que não conseguem satisfazer
essa escolha e fornecer alternativa manual.

`visibilitychange` para hidden, `pagehide`, cancelar, mudar de sessão e sair da
captura interrompem avaliações, descartam requests antigos e fecham tracks.
Voltar à página não reinicia a câmara sem novo gesto. Capturas locais e tokens
mantêm os ciclos de vida já definidos para revisão/reinício/eliminação.

## Contrato do endpoint

`POST /capture-api/sessions/{session_id}/camera-assessment`

| Elemento | Contrato |
| --- | --- |
| Query | `target=document_front`, `document_back` ou `selfie` |
| Autorização | Bearer capability da mesma sessão e origin aceite; avaliados antes do corpo |
| Consentimento | `camera_frames_opt_in=true` estrito e `camera_policy_version` vigente, guardados na sessão; sessões legacy/file-only sem opt-in são recusadas antes do corpo e revalidadas após decode |
| Estado | Sessão criada/à espera de captura, dentro do prazo; não aceita sessão já concluída |
| Corpo | Multipart com exactamente um ficheiro `frame` e zero campos adicionais |
| Frame | JPEG/PNG estático, no máximo 512 KiB, maior aresta 1280 pixels e no máximo 2 megapixels |
| Envelope | Corpo até orçamento do frame + 8192 bytes de overhead multipart |
| Prazo | Deadline total de 10 segundos; leitura usa o menor entre timeout de configuração e esse prazo |
| Capacidade | Inferência/decode em worker limitado; cancelamento HTTP não liberta a capacidade enquanto o trabalho nativo continua |
| Frequência | Via independente das quotas ordinárias: 120 pedidos/minuto por IP, 480 globais/minuto; estado HMAC limitado a 2048 entradas |
| Expiração | Revalidada depois do upload, decode e análise; resultado obsoleto não avança a captura |

Resposta `camera-assessment-v1`:

| Campo | Significado |
| --- | --- |
| `version` | Versão do contrato, `camera-assessment-v1` |
| `target` | Target solicitado, sem inferir identidade |
| `detector_ready` | Detector aplicável disponível |
| `detected` | Cartão/rosto localizado neste frame |
| `ready` | Geometria e qualidade adequadas ao guia neste frame; não é aprovação |
| `kind` | `card`, `face` ou `null` |
| `bounds` | Caixa normalizada `{x,y,width,height}` ou `null` |
| `corners` | Cantos observados normalizados `{x,y}` ou `null` |
| `reasons` | Códigos limitados e sem dados pessoais para explicar o guia |
| `appearance_signature` | Assinatura visual de 16 caracteres hexadecimais para documento ou `null` |
| `authenticity_confirmed` | Sempre `false` |

Coordenadas descrevem o frame submetido. O navegador deve aplicar a transformação
correcta para o elemento de vídeo, incluindo letterboxing, espelhamento e
orientação. A caixa no ecrã não é um recorte do ficheiro final. O servidor nunca
devolve OCR, campos documentais, embedding, score PAD ou identidade neste endpoint.

Os frames são transitórios: sem fila de aprendizagem, payload de captura,
ficheiro persistente, auditoria com imagem, log de corpo ou token em URL. Motivos
e contadores podem ser agregados, sem imagem, assinatura visual persistida ou
referência pessoal. A assinatura é um descritor visual efémero, não identificador
de pessoa, template facial ou prova antifraude.

## Qualidade e estabilidade

O backend documental exige quadrilátero real com quatro cantos e orçamento de
resolução nativa mínimo de 240 pixels na aresta curta e 380 na longa. Gates de
enquadramento/exposição/nitidez evitam capturar cartão cortado, demasiado pequeno,
descentrado ou ilegível. A geometria conservadora do SDD OCR continua a aplicar-se;
um rectângulo detectado não certifica que seja BI ou que o texto seja legível.

O navegador envia frames com cadência configurada, por defeito 700 ms. JPEG
1280/0,80 é adaptado ao limite de 512 KiB. Há no máximo um pedido em voo; não
acumula pedidos se a análise exceder o intervalo. Um `AbortController` e uma
geração de captura impedem resultados atrasados de alterar uma fase nova.

Captura automática exige três respostas `ready` consecutivas e pelo menos 1,4
segundos de estabilidade. Entre elas, centro varia no máximo 0,025 e dimensões
no máximo 0,045 em coordenadas normalizadas. Movimento, falha de qualidade,
frame inválido, erro ou mudança de target reiniciam a sequência. Um resultado
isolado nunca dispara a fotografia.

Verso exige cooldown de 2 segundos e distância Hamming de pelo
menos 10/64 entre assinatura actual e assinatura da frente. Retirar/reapresentar
a mesma frente não deve satisfazer o gate apenas por desaparecer da imagem.
Iluminação/cenário podem afectar o descritor; o gate é conveniência de captura,
não validação de lados. O utilizador conserva alternativa manual e revisão.

Frames do guia têm resolução/orçamento menores que as fotografias finais.
A captura documental final conserva o fluxo 2400px/JPEG0,94 da v0.6.1; frames de
1280px nunca substituem silenciosamente a imagem final de maior resolução.

## Requisitos e verificação v0.7

| ID | Critério verificável |
| --- | --- |
| CAM-01 | Sem pedido de câmara ou transmissão antes de consentimento válido, gesto e permissão. Texto informa envio prévio de frames; `camera_frames_opt_in=false` por omissão e política `capture-camera-v1` validada, incluindo recusa de sessões legacy/file-only. |
| CAM-02 | Capability cruzada/expirada, sessão concluída e origem inválida falham antes de parsing; prazo é revalidado após análise. |
| CAM-03 | Rejeitar frames excessivos, animados, dimensões inválidas, extras multipart e input indecodificável; manter limites de worker após cancelamento. |
| CAM-04 | Documento enquadrado produz bounds/corners normalizados; quadro ausente/pequeno/cortado/ilegível não avança automaticamente. |
| CAM-05 | Três respostas estáveis durante 1,4 segundos capturam uma única vez; respostas atrasadas, alvo anterior e movimento não avançam fases. |
| CAM-06 | Frente→verso conserva a stream traseira; mesma frente repetida não satisfaz mudança visual; verso real/modificado pode prosseguir para revisão sem aprovação documental. |
| CAM-07 | Verso→selfie fecha tracks traseiras e solicita nova câmara frontal; detector ausente/erro/permissão negada oferece manual/upload. |
| CAM-08 | Hidden/pagehide/cancel fecha tracks, aborta análise e impede reinício automático ao regressar. |
| CAM-09 | Um pedido em voo, lane limitada e bytes/pixels limitados; guia não esgota a quota normal de criação/consulta. |
| CAM-10 | Frames/signatures não são persistidos ou usados em OCR/embedding/treino; revisão explícita envia fotografias finais de resolução apropriada. |
| CAM-11 | Mobile e desktop: sem overflow, instruções legíveis, estado anunciado, controlos manuais acessíveis e preview alinhado com dados do detector. |
| CAM-12 | Avaliação real com câmara/dispositivos e condições variadas separada dos testes com vídeo/frames sintéticos; falhas mantêm denominadores e limites. |

Validação executada: 363 testes Python com motores CPU; 12 verificações JavaScript;
66 verificações HTTPS no Docker; 55 verificações de navegador com avaliações
simuladas, registadas separadamente. Chromium também completou o fluxo automático
com câmara de canvas e **backend de visão real**, mantendo a mesma stream para
frente/verso e fechando-a antes da selfie. Repetir a frente bloqueou a autocaptura;
revisão precedeu o único envio explícito. OCR foi 8/8 nesse ensaio e nos três
layouts 390/320/1440. Frames não modificaram nenhuma tabela no ensaio HTTPS.

[Evidência agregada](research/live-camera-validation-v07.json), sem imagens ou
valores pessoais. CAM-01–11 têm cobertura de software; CAM-12 permanece aberto
para ensaios de hardware/dispositivos e condições variadas. Axe não encontrou
violações, mas contraste tem itens incompletos que exigem revisão manual.
As 316 verificações de software v0.6.1 e o resultado OCR 8/8 não validam esta
funcionalidade nova. Testes em câmara virtual não certificam uso em telemóvel
físico, ataques de apresentação ou presença de pessoa viva.

## Compatibilidade e limites de entrega

Endpoint aditivo e contrato versionado. Schema mantém `0005_identities`; frames
não criam tabela ou novos dados retidos. Política de captura deve explicar a
transmissão transitória. `capture-privacy-v1` continua válida para a verificação;
permissão explícita de frames usa `capture-camera-v1`, cifrada no consentimento
existente. Opt-ins e retenções de aprendizagem/identidade continuam independentes.
Sessões antigas sem o novo opt-in só podem seguir pelos fluxos autorizados
anteriormente, sem transmissão live. O intervalo pode ser configurado por
`MUTH_LIVE_CAMERA_INTERVAL_MS`, por defeito 700 ms. A quota dedicada usa
`MUTH_LIVE_CAMERA_RATE_LIMIT_PER_MINUTE`, no máximo 120/minuto por IP.

O guia não resolve o gate de orientação OCR falhado da v0.6.1, autenticação de BI,
deepfake/injection, calibração biométrica ou treino de pesos. Nota continua
experimental com teto 6/10 quando existem sinais biométricos; demo sem nota e
`authenticity_confirmed=false` permanecem. Aceitar automaticamente uma fotografia
não aprova automaticamente uma identidade.
