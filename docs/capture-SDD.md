# Portal de captura — MUTH v0.5

SDD 1.3 · 2 de Outubro de 2026. Complementa os SDD da
[plataforma](SDD.md) e [biometria](biometrics-SDD.md).

## Experiência e contratos

O utilizador abre `/` ou `/capture`, aceita a política, fotografa frente/verso do
documento e selfie, revê as três imagens e recebe o resultado. A câmara só abre
num gesto explícito. Documentos preferem a câmara traseira, selfie a frontal;
ficheiros JPEG/PNG e captura nativa do dispositivo servem de alternativa.
Não existe login ou chave B2B no navegador.

Layouts: sidebar e área de trabalho em desktop; progressão compacta e acções
adaptadas ao telemóvel. Há estados de permissão negada, indisponibilidade,
captura/recaptura, revisão incompleta, processamento, erro retryable, expiração e
resultado. Foco, teclado, live regions, contraste e redução de movimento integram
o fluxo. Não há timers que finjam a conclusão da verificação.

O browser normaliza orientação e exporta JPEG até 1600 pixels/.86, limita tamanho
individual e soma antes do envio. O backend volta a validar bytes, dimensões e
formato. Nenhuma imagem ou token entra em localStorage/sessionStorage. Tracks são
paradas após foto, troca de etapa, navegação, invisibilidade e reinício.

| Operação | Acesso e resultado |
| --- | --- |
| `GET /capture-api/config` | Público; modo, disponibilidade, política e limites |
| `POST /capture-api/sessions` | Consentimento estrito JSON; devolve ID/token/expiração |
| `POST /capture-api/sessions/{id}/verify` | Bearer + Idempotency-Key; exactamente document_front/document_back/selfie |
| `GET /capture-api/sessions/{id}` | Bearer da mesma sessão; resultado concluído |
| `DELETE /capture-api/sessions/{id}` | Bearer da mesma sessão; elimina payloads/resultados |

## Fronteira de segurança e retenção

Tokens Fernet derivados para a finalidade de captura contêm audience, sessão,
tenant reservado e expiração. Ficam em memória e headers, nunca em URLs ou logs.
Não autenticam `/v1`, não acedem a outras sessões nem permitem feedback/review/
learning. `capture-portal` não pode ter TenantKey; `local` também é recusado para
evitar colisão com a chave legacy. O portal nunca activa opt-in de aprendizagem.

Origin e Sec-Fetch-Site bloqueiam mutações cross-site. Rotas são validadas mesmo
quando a app é montada sob um prefixo. Autorização e existência/expiração precedem
a leitura do upload; o token é revalidado depois da leitura. Multipart tem três
ficheiros, zero campos e limites por imagem/corpo. Deadline total de 120 segundos
por defeito responde 408 e liberta capacidade quando o upload não acaba.

Quota pública: 1000 sessões activas por defeito; limitação por endereço pseudónimo,
limite global e mapa de janelas com tamanho controlado. X-Forwarded-For só deve
ser aceite de proxies confiáveis. O Compose mantém a API numa rede interna e só
expõe Caddy. Ao disponibilizar a API de outra forma, rever a configuração de proxy.

Resultados do portal são cifrados por um dia por defeito. Migração `0003_capture`
acrescenta `capture_result`; resultado normal e relatório são finalizados na mesma
transacção. Delete/purge eliminam ambos. Worker limpa retenção e capturas
abandonadas a cada 300 segundos e a criação liberta quota expirada. Não há
garantia de apagamento físico de páginas SQLite/backups; estes exigem operação
própria. O token dura até ao prazo de captura, não todo o período de retenção.

## Nota 0–10 e evidência disponível

O resultado apresenta **Nota dos sinais de autenticidade**, de natureza
**indicativa**, sempre `authenticity_confirmed=false`. Não representa uma
probabilidade, certificação ou confirmação oficial. Não altera o estado da
verificação para approved. O verso avalia legibilidade, sem identificar campos,
confirmar que é um BI ou autenticar o documento.

Na versão actual, a fórmula versionada é:

```text
min(6, 10 × max(0, cosine_face) × score_PAD × min(legibilidade_frente, legibilidade_verso))
```

Legibilidade usa resolução, contornos e clipping de exposição conforme os limites
de `services/capture.py`. Similaridade e softmax entram num índice experimental;
não são probabilidades de identidade ou autenticidade. Teto de evidência: **6/10**,
exposto na API/UI. Este teto e os pesos do índice são regras iniciais de produto,
não uma escala validada numa população. Mudanças exigem versionamento/avaliação.

Evidence fail calibrada limita a nota a 2. Lados iguais, inclusive pixels após
normalização, indicam capturas contraditórias e dão 0 quando há evidência
biométrica válida. Demo, falta de scores, imagens insuficientes, output inesperado
ou divergência PAD devolvem **nota indisponível**, nunca um valor inventado.
Sem autenticidade documental, o resultado permanece review/rejected.

## Rastreabilidade

| ID | Critério | Evidência |
| --- | --- | --- |
| C-01 | Consentimento → frente → verso → selfie → revisão → resultado | Browser com upload real ligado aos motores CPU |
| C-02 | Câmara por gesto e tracks paradas | Câmara virtual Chromium, captura/recaptura e inspeção de tracks |
| C-03 | Responsive e acessível | 320/380/390 px sem overflow; Axe nos estados analisados e foco por teclado |
| C-04 | Token limitado, origem e isolamento | CaptureApiTests; token cruzado/expirado, tenant legacy e root_path |
| C-05 | Upload limitado e sem acumulação | Três partes, deadline, revalidação do token, métricas a zero |
| C-06 | Resultados cifrados, replay e eliminação | Replay exacto, alteração do verso 409, purge/scrub, sem LearningSample |
| C-07 | Nota honesta | CaptureScoreTests: nulidade, teto, duplicação, falhas e escala |
| C-08 | Entrega portátil | Assets no wheel, migração, configuração HTTPS e instruções locais |

## HTTPS e acesso mobile

`getUserMedia` exige contexto seguro: HTTPS ou localhost. No computador, executar
o servidor do README e abrir `http://127.0.0.1:8000`. Para usar a câmara no
telemóvel através da rede, configurar domínio/HTTPS ou utilizar a alternativa
de fotografia nativa quando suportada. Um endereço HTTP de LAN não habilita
automaticamente a câmara JavaScript.

Configuração HTTPS fornecida em [compose.yaml](../compose.yaml) e
[Caddyfile](../infra/Caddyfile). DNS/domínio e acesso às portas 80/443 devem apontar
para a máquina de destino. Gerar `.env`, preparar os modelos, definir MUTH_DOMAIN
no ambiente ou `.env`. O container corre como UID/GID 10001: os directórios do
bundle precisam de traversal e manifesto/licença/pesos de leitura para esse
utilizador. Para os pesos públicos preparados pelos scripts, uma cópia dedicada
evita alterar permissões de `.env`, dados ou imagens:

```bash
install -d -m 0755 container-models container-models/biometrics
install -m 0644 models/biometrics/{manifest.json,license-review.txt,yunet.onnx,sface.onnx,minifas-v2.onnx,minifas-v1se.onnx} container-models/biometrics/
export MUTH_MODELS_DIRECTORY=./container-models
```

Este exemplo aplica-se ao bundle público research, não a ficheiros privados de
clientes. Ambas as pastas de modelos estão excluídas de Git/build. Confirmar
que o manifesto pode ser lido como utilizador do container e executar a migração:

```bash
docker compose run --rm --no-deps api python -c "from pathlib import Path; print(len(Path('/app/models/biometrics/manifest.json').read_bytes()))"
docker compose --profile tools run --rm migrate
docker compose up --build -d api gateway
```

Num ambiente com proxy TLS, o Dockerfile aceita opcionalmente o secret BuildKit
`proxy_ca`; passar o bundle de CA apropriado com `docker build --secret
id=proxy_ca,src=/caminho/ca-bundle.pem -t muth:0.5 .`. O certificado só é montado
durante a instalação de dependências, com verificação TLS activa.

A API não publica porta no host; o gateway termina TLS. O manifesto research não
autoriza um serviço comercial só porque foi colocado num container. A instalação
de destino precisa dos seus próprios pesos/configuração e da avaliação do piloto.
Nenhum deploy externo foi realizado nesta entrega.
