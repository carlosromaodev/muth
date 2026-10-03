# Recuperação da extracção documental — MUTH v0.6.1

SDD 1.5 · 2 de Outubro de 2026. Complementa
[OCR e aprendizagem](web-learning-SDD.md), [captura](capture-SDD.md) e
[estado da implementação](implementation-status.md). Schema corrente:
`0005_identities`; esta correcção não exige migração adicional.

## Problema observado e compromisso da entrega

Uma captura real de um BI angolano devolveu um nome de uma letra, perdeu o número,
a data de nascimento e a filiação, e conservou lixo junto da data de validade.
As fotografias incluem fundo, mãos e texto de um computador. Tratar toda a cena
como uma página uniforme permitiu que linhas externas ao documento competissem
com os seus campos. O parser anterior perdeu linhas de nomes/filiação e não
tratou correctamente campos independentes na mesma linha.

O ensaio anterior de oito campos numa credencial sintética verificou integração;
não demonstrou extracção fiável em documentos reais. Este incidente é uma
regressão funcional prioritária. A entrega deve recuperar a leitura observada,
impedir falsos campos plausíveis e criar uma avaliação repetível para novas
capturas. Não se pode converter um resultado parcial em sucesso alterando os
campos esperados, nem preencher campos a partir da pessoa conhecida.

Os avisos de modo `demo` e a ausência de nota têm uma causa operacional separada:
o servidor usado na captura não executava os motores biométricos reais. OCR
funciona independentemente do modo biométrico. Corrigir OCR não activa modelos
por si só nem confirma autenticidade documental.

## Fluxo e fronteiras de responsabilidade

```text
Câmara/ficheiro
  → orientação EXIF e normalização no navegador
  → JPEG de documento com resolução preservada e revisão da imagem inteira
  → limites de bytes/pixels, autorização e controlo de capacidade da API
  → PreparedDocument: detectar cartão e corrigir perspectiva quando seguro
  → Tesseract local: palavras, confiança e coordenadas TSV
  → leitura de linhas/colunas e extracção delimitada por rótulos
  → validações de datas, número, nomes e conflitos
  → document_data estruturado, cifrado e apresentado sem inferir valores
```

`PreparedDocument` é uma vista interna do documento. A detecção procura bordas
compatíveis com um cartão; não procura identidades nem extrai texto de ecrãs.
Quando a geometria é incerta, conserva a imagem completa com motivo explícito.
Não usa um recorte central fixo. A correcção de perspectiva deve amostrar pixels
da resolução disponível antes da etapa de detecção reduzida; ampliar não
recupera detalhes que a câmara não capturou. A imagem da selfie conserva o seu
processamento biométrico separado.

TSV fornece palavras e caixas geométricas. Blocos esparsos na mesma linha física
podem ser reunidos, mas linhas/colunas de campos diferentes continuam delimitadas.
Extracção, validação e confiança são conceitos distintos: uma confiança elevada
de Tesseract não torna um nome truncado ou uma data impossível válidos.

`document_data.processing_version=muth-document-ocr-v3` identifica o parser e
o processamento desta entrega. `muth-document-geometry-v1` identifica a etapa
geométrica interna. A versão do executável Tesseract continua separada. Dados
anteriormente extraídos permanecem associados à sua versão; o resultado original
não é reescrito por uma proposta de correcção do utilizador.

## Requisitos e severidade

| ID | Severidade | Requisito e aceitação |
| --- | --- | --- |
| OCR-01 | P0 | Isolar cartão só com geometria suficientemente apoiada; rejeitar quadriláteros incompatíveis e conservar a cena quando incerto. Testar documento inclinado, fundo com texto, bordas interrompidas e cena sem cartão. |
| OCR-02 | P0 | Recuperar nome completo em várias linhas sem interpretar um apelido como rótulo de nascimento. Uma letra isolada nunca constitui nome válido. |
| OCR-03 | P0 | Ler o rótulo do número do BI, incluindo variantes de `Bilhete de Identidade Nº`; validar os caracteres efectivamente observados, sem substituir letras/números por adivinhação. |
| OCR-04 | P0 | Delimitar dois campos na mesma linha, incluindo emissão/validade e sexo/altura. Extrair uma data completa observada, normalizar para ISO e validar calendário; texto residual não é valor confirmado. |
| OCR-05 | P0 | Ler filiação em várias linhas; separar nomes dos progenitores apenas quando o layout observado sustenta duas secções. Conservar filiação e marcar interpretação de layout como não confirmada. |
| OCR-06 | P0 | Não inferir nacionalidade do cabeçalho de Angola, naturalidade, morada, nome, rosto ou emissor. Campo ausente fica ausente. |
| OCR-07 | P0 | Duas leituras plausíveis diferentes não escolhem silenciosamente a de maior confiança. Conflitos dentro do lado, entre layouts ou frente/verso ficam explícitos e impedem promoção do campo. |
| OCR-08 | P0 | Entrada vazia, ilegível, danificada ou externa ao cartão nunca deve produzir identidade completa por resíduos de fundo. Devolver parcial/indisponível e motivos quando necessário. |
| OCR-09 | P0 | OCR local sem shell, limite partilhado de tempo/saída, temporários privados e cleanup em sucesso/falha. Não imprimir texto bruto, pixels, campos pessoais ou paths privados em relatórios públicos. |
| OCR-10 | P1 | Documentos mantêm resolução suficiente na normalização web; revisão mostra a fotografia inteira e dá instruções úteis de iluminação, foco e enquadramento antes do envio. |
| OCR-11 | P1 | Campos inválidos/incertos não aparecem como dados confirmados nem preenchem o formulário de correcção como se fossem datas ISO válidas. Utilizador pode corrigir sem alterar retroactivamente a extracção. |
| OCR-12 | P1 | Diagnóstico expõe modo do servidor, prontidão de OCR/modelos e idiomas disponíveis sem chaves, vectores ou dados pessoais. Demo permanece explícito na captura e resultado. |
| OCR-13 | P1 | Avaliador offline aceita fotografias/anotações locais autorizadas e emite apenas estatísticas agregadas, com denominadores e versão do motor. Não promove políticas nem envia documentos a serviços externos. |

P0 bloqueia a conclusão desta recuperação se o defeito reproduzido persistir ou
se um valor inventado for apresentado como leitura. P1 bloqueia a conclusão do
respectivo fluxo quando impede operação ou revisão. Estes níveis não constituem
certificação biométrica ou documental.

## Leitura conservadora do BI angolano

O parser usa rótulos observados e a posição das linhas. `NASCIMENTO` isolado
nunca é alias de data: também ocorre em nomes. Datas exigem rótulos próprios e
um token completo com calendário válido. Nomes aceitam caracteres portugueses
e espaços, apóstrofos ou hífen, mas recusam uma letra isolada, números, rótulos
do documento, assinatura ou título administrativo misturados no valor.

Filiação não é uma linha única obrigatória. A extracção pode juntar segmentos
próximos alinhados na mesma coluna; pára num novo rótulo ou numa região estranha.
Separar progenitores exige evidência de layout; ter dois nomes não comprova a
relação familiar. Se o OCR perde linhas ou os grupos ficam ambíguos, a leitura
permanece parcial e não inventa um progenitor.

Número do BI e datas têm validação estrutural, não confirmação pelo emissor.
Um número com formato válido pode pertencer a um documento falsificado. O
`issuing_country` identificado pelo cabeçalho não preenche `nationality`.
Passaportes continuam a usar checks MRZ quando a leitura suporta esse formato;
checksums correctos também não certificam autenticidade.

## Qualidade, capacidade e falhas

O navegador normaliza documentos até uma aresta de 2400 pixels e JPEG 0,94,
mantendo a fotografia inteira na revisão. A selfie usa o orçamento biométrico
próprio. Backend valida byte/pixel limits antes de processamento. Preparação
geométrica tem limites de resolução/candidatos; leituras Tesseract partilham o
deadline configurado, por defeito 12 segundos, e o orçamento de saída. Tentativas
adaptativas não multiplicam este prazo por lado ou orientação.

PSM 6/11 são alternativas de layout, não votos de autenticidade. Idiomas por
defeito são `por+eng`. Docker instala ambos; um host precisa dos pacotes Tesseract
e respectivos dados linguísticos. Fallback apenas para inglês deve ser visível.
Não se descarregam idiomas durante um pedido.

Falta de motor, idioma, timeout, limite de saída, processo abortado, orientação
não recuperável ou geometria incerta devem produzir motivos sem revelar o
documento. Dados parciais não são anunciados como extracção completa. Uma nova
captura deve poder recuperar da falha sem perder as fronteiras de autorização,
claims, retenção ou idempotência já existentes.

`muth doctor` verifica configuração e dependências locais, incluindo o modo
efectivo do servidor. O estado de investigação pronto não equivale a produção.
Arranque `biometric` exige manifesto/bundle verificado; alterar o texto da página
ou instalar Tesseract não activa YuNet, SFace ou MiniFASNet.
`muth activate-biometrics` valida os pesos locais antes de actualizar a
configuração de forma atómica, preservando chaves e outras definições. Requer
reiniciar o servidor; não promove políticas nem resolve licenciamento comercial.
Imagens transparentes não podem revelar pixels ocultos após recorte. Um campo
excluído por conflito mantém esse estado em tentativas OCR posteriores.

## Avaliação privada e gates

As duas fotografias reais fornecidas pelo utilizador podem ser usadas localmente
para reproduzir e medir este defeito. A autorização é para testes de extracção;
não implica treino neural, inscrição de perfil ou publicação da credencial.
Fotografias, anotações, texto OCR e valores esperados não entram no Git, fixtures
públicas, screenshots de demonstração, logs ou artefactos de distribuição.
Casos sintéticos públicos devem usar dados diferentes e explícitos, derivados
das estruturas de layout e não da identidade particular.

`scripts/evaluate_document.py` compara campos efectivamente impressos com
anotações locais. O relatório partilhável contém apenas número de lados/campos,
matches, faltas, valores inesperados, conflitos, latência, idioma e versões.
Exact match normaliza apenas convenções documentadas, como espaços e datas ISO;
não remove partes do nome para fazer o resultado passar. Campos ausentes têm
denominador separado. Não há taxa populacional calculável a partir de um BI.

| Gate | Evidência exigida | Limite da conclusão |
| --- | --- | --- |
| G1 — regressão observada | Frente/verso privados: comparação dos oito campos impressos anotados; nacionalidade continua ausente quando não impressa; nenhum dado vem do texto do fundo. | Um documento prova recuperação do caso, não precisão geral. |
| G2 — captura web | Mesmas imagens após normalização JPEG do navegador, revisão sem recorte enganador e resultado coerente com a API. | Simulação de browser não substitui câmara física. |
| G3 — transformações | Ensaios separados de 90°/180°, perspectiva moderada, redução/JPEG, iluminação e foco. Registar sucesso, abstention e falha por transformação. | Não declarar robustez a orientações/qualidade ainda não ensaiadas. |
| G4 — negativos | Cena sem cartão, documento em branco, imagem danificada, uma letra, data impossível, apelido ambíguo e conflitos entre lados/layouts. | Ausência de alucinação nestes casos não comprova todos os ataques. |
| G5 — operação | OCR ausente/idioma ausente/timeout/saída excessiva e setup demo; diagnóstico sem dados sensíveis e limites preservados. | Dependências prontas não validam autenticidade. |
| G6 — integração | Suite relevante, lint/formato/JS, distribuição e API/browser sem regressões em consentimento, cifragem, retenção e capacidades. | Testes de software não medem FMR/FNMR/APCER/BPCER ou qualidade populacional OCR. |

Cada gate deve conservar evidência agregada da execução. Um gate não executado
fica pendente; um gate falhado conserva a falha e o motivo. Melhorias futuras
exigem um corpus consentido diversificado por versão documental, dispositivo,
iluminação e condição física, com anotação independente e separação por pessoa.
Comparar Tesseract com PaddleOCR/docTR nesse corpus é uma decisão mensurável,
não uma substituição automática por notoriedade de biblioteca.

Execução v0.6.1: G1 e o ensaio G2 das fotografias fornecidas recuperaram oito de
oito campos, mantendo nacionalidade não impressa ausente. **G3 permanece
falhado:** os ensaios de 90°/180°/270° recuperaram 4/8, 5/8 e 3/8 campos. Não se
declara orientação universalmente recuperada, precisão populacional ou câmara
mobile física validada. A suite executou 316 testes CPU, incluindo 45 de OCR;
testes de geometria/parser não substituem o corpus real de transformações.
A [evidência agregada](research/document-ocr-recovery-v061.json) e o
[estado](implementation-status.md) conservam os denominadores e limites.

## Compatibilidade e limites de produto

Contratos de consentimento, endpoints, cifragem e schema mantêm-se. `DocumentData`
identifica OCR v3; campos continuam opcionais, com origem, confiança textual,
validação e conflitos. OCR corrigido não inscreve automaticamente uma identidade
e não converte proposta do utilizador em evidência independente de aprendizagem.

A nota continua experimental, com teto 6/10 quando os sinais biométricos
existem; demo não recebe nota biométrica. `authenticity_confirmed=false` e a
comparação 1:1 provisória continuam. Este SDD não declara autenticação documental,
protocolo de login completo, treino autónomo de pesos ou validação de produção.
