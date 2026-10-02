# MUTH e o mercado de identidade digital

Pesquisa realizada em 2 de Outubro de 2026. Fontes oficiais e documentação técnica;
não é uma comparação independente de precisão, preço ou cobertura angolana.

## Comparação

| Plataforma | Capacidades observadas nas fontes | Implicação para MUTH |
| --- | --- | --- |
| [Persona](https://docs.withpersona.com/inquiries) | Inquiries persistentes, templates versionados, lifecycle, integrações e webhooks | Uma chamada facial não basta: sessão e configuração são parte do produto |
| [Veriff](https://www.veriff.com/product/identity-verification) | Documento+selfie, captura assistida, liveness, revisão humana, sinais de dispositivo e rede | Conversão e resistência à fraude dependem do canal de captura e da operação |
| [Jumio](https://documentation.jumio.ai) | Selfie+ID, autenticação, sinais de risco, SDKs e screening | Fazer onboarding bem antes de expandir para AML e autenticação |
| [Entrust / antigo Onfido](https://documentation.identity.entrust.com/) | Workflow Studio, documento, biometria, SDKs, prevenção de takeover e passkeys | Reutilização precisa de protocolo de autenticação, não apenas embedding guardado |
| [Sumsub](https://sumsub.com/) | IDV, liveness/deepfake, reusable KYC, workflow, risco, case management e monitoring | Evitar competir em todas as linhas de compliance no primeiro produto |
| [Smile ID](https://smile.id/) | Especialização local, biometria, fontes governamentais, dispositivos/canais, onboarding e autenticação | “Africano” já é posicionamento competitivo; a diferença tem de ser comprovada |

As páginas de marketing anunciam números de precisão, volume e cobertura. Esses
números não usam necessariamente os mesmos datasets ou definições. Não os comparar
com o MUTH como se fossem métricas equivalentes. Confirmar BI angolano e versões
suportadas por consulta comercial ou teste; não inferir ausência de cobertura dos rivais.
As páginas de produto de Persona, Jumio e Entrust bloquearam a leitura automatizada;
a comparação desses três usa os portais oficiais de documentação acessíveis.

## Onde a ideia é forte

A combinação documento + presença + comparação 1:1 resolve um problema útil. Uma
API comum pode reduzir o trabalho de integração e permitir evolução dos modelos.
O conhecimento de versões de BI, erros de OCR, câmaras económicas e condições locais
pode tornar-se activo próprio quando acompanhado de dados consentidos e avaliação.

## Onde a ideia precisa de prova

- Mercado: entrevistar compradores, identificar fluxo com custo de fraude/revisão
  relevante e fechar um piloto com critérios e orçamento.
- Qualidade: medir captura, exactidão por campo, FMR/FNMR e ataques de apresentação.
- Fontes oficiais: não existe integração com emissão de BI neste projecto.
- Reutilização: empresas precisam de regras de consentimento e autorização; não
  criar uma identidade biométrica global por omissão.
- Custos: sem modelos/hardware/revisão medidos, não há custo unitário credível.

## Primeiro produto recomendado

Verificação de BI angolano + selfie para um caso de onboarding, com sessão,
decisão explicável, revisão e integração backend. Escolher uma instituição parceira
e um conjunto limitado de documentos antes da expansão pan-africana.

O activo defensável é a combinação de avaliação local, dados com direitos claros,
normalização documental, operação antifraude e integração. Colar bibliotecas não
estabelece isso por si só.

## Economia a medir no piloto

`custo/verificação = inferência + infraestrutura + licenças + revisão humana + operação`

Comparar aprovação legítima, fraude aceite, abandono, latência, percentagem de revisão,
tempo de integração e custo por verificação concluída. Construir um cenário por
volume e risco. Não inventar preços dos fornecedores ou declarar MUTH mais barato.
