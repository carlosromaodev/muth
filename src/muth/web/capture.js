"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const icon = (name) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `/assets/icons.svg#${name}`);
    svg.append(use);
    return svg;
  };
  const captureTypes = [null, "document_front", "document_back", "selfie"];
  const stepNames = ["Consentimento", "Frente do documento", "Verso do documento", "A sua selfie", "Rever e enviar", "Resultado"];
  const documentFieldLabels = {
    name: "Nome", document_number: "Número do documento", birth_date: "Data de nascimento",
    expiry_date: "Data de validade", sex: "Sexo", nationality: "Nacionalidade",
    parentage: "Filiação", father_name: "Nome do pai", mother_name: "Nome da mãe",
  };
  const sourceLabels = { front: "Frente", back: "Verso", both: "Frente e verso" };
  const validationLabels = { valid: "Formato consistente", invalid: "Formato a rever", unvalidated: "Por confirmar" };
  const learningLabels = {
    not_opted_in: "Sem autorização", pending_review: "Pendente de revisão", reviewed: "Revisto",
    withdrawn: "Consentimento retirado", unavailable: "Indisponível",
  };
  const identityLabels = {
    not_requested: "Não solicitado", pending: "Em preparação", enrolled_provisional: "Registo provisório",
    unavailable: "Indisponível", rejected: "Não registado", deleted: "Eliminado",
  };
  const captureCopy = {
    document_front: {
      title: "Fotografe a frente do documento",
      description: "Coloque o documento numa superfície plana e enquadre os quatro cantos.",
      badge: "FRENTE DO BI", placeholder: "O documento aparece aqui", icon: "document", label: "Frente do documento",
      tips: [["Mostre os quatro cantos", "O documento deve aparecer inteiro."], ["Evite reflexos", "Use luz natural, sem flash directo."], ["Mantenha a imagem nítida", "Não mexa o telemóvel ao fotografar."]],
    },
    document_back: {
      title: "Agora, fotografe o verso",
      description: "Vire o mesmo documento e mantenha toda a informação dentro do enquadramento.",
      badge: "VERSO DO BI", placeholder: "O verso aparece aqui", icon: "document", label: "Verso do documento",
      tips: [["Vire o mesmo documento", "Fotografe o verso do documento da etapa anterior."], ["Enquadre toda a informação", "Não corte os cantos nem o texto."], ["Confirme a legibilidade", "Evite sombras, reflexos e desfocagem."]],
    },
    selfie: {
      title: "É a sua vez. Tire uma selfie.",
      description: "Olhe directamente para a câmara, com o rosto inteiro visível e boa iluminação.",
      badge: "A SUA SELFIE", placeholder: "O seu rosto aparece aqui", icon: "person", label: "A sua selfie",
      tips: [["Olhe para a câmara", "Mantenha o rosto de frente, sem o inclinar."], ["Deixe o rosto visível", "Retire óculos escuros e acessórios que o tapem."], ["Use luz uniforme", "Evite uma janela ou luz forte atrás de si."]],
    },
  };
  const reasonMessages = {
    document_sides_identical: "A frente e o verso são a mesma imagem. Tire fotografias dos dois lados.",
    document_front_resolution_insufficient: "A fotografia da frente precisa de maior resolução.",
    document_back_resolution_insufficient: "A fotografia do verso precisa de maior resolução.",
    document_front_exposure_unsupported: "A frente está demasiado clara ou escura.",
    document_back_exposure_unsupported: "O verso está demasiado claro ou escuro.",
    document_front_too_blurred_or_blank: "A fotografia da frente está desfocada ou sem informação legível.",
    document_back_too_blurred_or_blank: "A fotografia do verso está desfocada ou sem informação legível.",
    back_document_authenticity_not_implemented: "O verso foi recebido, mas a sua autenticidade não foi confirmada.",
    document_authenticity_not_verified: "A autenticidade deste documento ainda não foi confirmada.",
    face_threshold_not_locally_calibrated: "A comparação facial ainda precisa de validação para permitir uma conclusão.",
    liveness_threshold_not_locally_calibrated: "A análise da selfie ainda precisa de validação para permitir uma conclusão.",
    liveness_ensemble_disagreement: "A análise da selfie não chegou a uma conclusão consistente.",
    rgb_pad_pass: "A selfie apresenta sinais compatíveis com um rosto real.",
    presentation_attack: "Foram encontrados sinais de possível apresentação fraudulenta.",
    face_too_blurred: "A fotografia do rosto precisa de mais nitidez.",
    face_exposure_unsupported: "O rosto está demasiado claro ou escuro.",
    face_outside_frame: "Enquadre o rosto inteiro dentro da imagem.",
    invalid_capture_image: "Não foi possível analisar o formato desta imagem.",
    demo_face_model_not_configured: "A comparação do rosto não está disponível no modo de demonstração.",
    demo_liveness_model_not_configured: "A análise biométrica da selfie não está disponível no modo de demonstração.",
    demo_document_model_not_configured: "A análise do documento não está disponível no modo de demonstração.",
    no_face: "Não foi possível encontrar um rosto nítido.", face_not_detected: "Não foi possível encontrar um rosto nítido.",
    no_face_detected: "Não foi possível encontrar um rosto nítido.", multiple_faces: "A imagem contém mais de um rosto.",
    multiple_faces_detected: "A imagem contém mais de um rosto.", face_too_small: "O rosto aparece demasiado pequeno.",
    image_blurry: "A imagem precisa de mais nitidez.", face_blurry: "A fotografia do rosto precisa de mais nitidez.",
    low_image_quality: "A qualidade da imagem não permite uma conclusão.", face_quality_failed: "A qualidade do rosto não permite uma conclusão.",
    excessive_face_roll: "Mantenha o rosto direito, de frente para a câmara.", face_roll_excessive: "Mantenha o rosto direito, de frente para a câmara.",
    invalid_landmarks: "O enquadramento do rosto não permite uma comparação fiável.", invalid_face_landmarks: "O enquadramento do rosto não permite uma comparação fiável.",
    excessive_exposure_clipping: "A imagem tem zonas demasiado claras ou escuras.", face_exposure_clipped: "A imagem tem zonas demasiado claras ou escuras.",
    face_pose_unsupported: "Mantenha o rosto de frente para a câmara.", face_match_below_threshold: "A comparação dos rostos ficou abaixo do limiar definido.",
    face_match_above_threshold: "Os rostos apresentam sinais de correspondência.", face_match: "Os rostos apresentam sinais de correspondência.",
    face_mismatch: "Os rostos não apresentam correspondência suficiente.", liveness_below_threshold: "Foram encontrados sinais que precisam de revisão.",
    liveness_above_threshold: "A selfie apresenta sinais compatíveis com um rosto real.", spoof_detected: "Foram encontrados sinais de possível apresentação fraudulenta.",
    liveness_model_disagreement: "A análise da selfie não chegou a uma conclusão consistente.", model_disagreement: "A análise da selfie não chegou a uma conclusão consistente.",
    calibration_required: "Este resultado ainda precisa de validação para permitir uma conclusão.", uncalibrated_threshold: "Este resultado ainda precisa de validação para permitir uma conclusão.",
    baseline_uncalibrated: "Este resultado ainda precisa de validação para permitir uma conclusão.", research_model: "Este resultado ainda precisa de validação para permitir uma conclusão.",
    document_authenticity_unavailable: "A autenticidade deste documento ainda não foi confirmada.", document_authenticity_not_supported: "A autenticidade deste documento ainda não foi confirmada.",
    document_validation_not_implemented: "A autenticidade deste documento ainda não foi confirmada.", document_back_not_validated: "O verso foi recebido, mas a sua autenticidade não foi confirmada.",
    document_back_received: "O verso foi recebido, mas a sua autenticidade não foi confirmada.", ocr_not_configured: "Os dados do documento ainda precisam de confirmação.",
    document_portrait_only: "A fotografia do documento foi usada para comparar o rosto.", demo_mode: "A avaliação biométrica não está disponível neste modo.",
    demo_no_verification: "A avaliação biométrica não está disponível neste modo.", engine_unavailable: "A análise está temporariamente indisponível.",
  };
  const state = {
    config: null, phase: 0, captures: {}, stream: null, cameraGeneration: 0, imageGeneration: 0,
    busy: false, processing: false, editReturn: false, session: null, idempotencyKey: null,
    submitted: false, result: null, cameraPending: false, dismissed: false, flowGeneration: 0, controller: null,
    proposedFields: {}, correctionBaseline: {}, learningAction: null, identity: null, identityAction: null,
  };
  const identityPrivacy = document.createElement("p"); identityPrivacy.id = "privacy-identity-retention";
  identityPrivacy.className = "privacy-retention"; identityPrivacy.hidden = true;
  $("privacy-learning-retention").after(identityPrivacy);
  $("privacy-dialog").querySelector("li:last-child").textContent = "«Nova verificação» descarta as capturas, os dados apresentados e as credenciais desta página. Os registos de identidade e as contribuições autorizadas mantêm-se pelos respectivos prazos. Os botões do resultado permitem retirar a aprendizagem ou eliminar a identidade de forma independente.";
  const announce = (text) => { $("announcements").textContent = text; };
  const hideError = () => { $("global-error").hidden = true; $("global-error").textContent = ""; };
  const showError = (message) => {
    $("global-error").textContent = message;
    $("global-error").hidden = false;
    $("global-error").focus();
    announce(message);
  };
  function stopCamera() {
    state.cameraGeneration += 1;
    if (state.stream) state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
    state.cameraPending = false;
    $("camera-video").srcObject = null;
    $("camera-video").hidden = true;
    $("live-label").hidden = true;
  }
  const currentType = () => captureTypes[state.phase];
  const hasAllCaptures = () => captureTypes.slice(1).every((name) => Boolean(state.captures[name]));
  function updateButtons() {
    const type = currentType();
    $("next-button").disabled = state.busy || !state.config?.enabled || (state.phase === 0 ? !$("consent").checked : state.phase < 4 ? !state.captures[type] : !hasAllCaptures());
    $("open-camera").disabled = state.busy || state.cameraPending;
    $("take-photo").disabled = state.busy || !state.stream || $("camera-video").readyState < 2;
    $("retake-photo").disabled = state.busy;
    $("file-input").disabled = state.busy;
    $("file-label").disabled = state.busy;
    $("back-button").disabled = state.busy;
    $("restart-button").disabled = state.busy;
    $("learning-opt-in").disabled = state.busy || Boolean(state.session) || !learningEnabled();
    $("identity-opt-in").disabled = state.busy || Boolean(state.session) || !identityEnabled();
    $("submit-corrections").disabled = state.busy;
    $("withdraw-learning").disabled = state.busy;
    $("submit-corrections").textContent = state.learningAction === "corrections" ? "A enviar sugestões…" : "Enviar sugestões de correcção";
    $("withdraw-learning").textContent = state.learningAction === "withdraw" ? "A retirar consentimento…" : "Retirar consentimento de aprendizagem";
    $("correction-fields").querySelectorAll("input").forEach((input) => { input.disabled = state.busy; });
    $("identity-selfie-input").disabled = state.busy;
    $("compare-identity").disabled = state.busy;
    $("compare-identity-label").textContent = state.identityAction === "compare" ? "A comparar a nova selfie…" : "Comparar uma nova selfie";
    $("delete-identity").disabled = state.busy;
    $("delete-identity").textContent = state.identityAction === "delete" ? "A eliminar identidade…" : "Eliminar identidade guardada";
    $("footer-note").replaceChildren(icon("lock"), state.phase === 5 && preserveCompletedResult() ? "Nova verificação limpa esta página; os registos autorizados mantêm-se." : "Você decide quando enviar.");
    document.querySelectorAll("[data-edit]").forEach((button) => { button.disabled = state.busy; });
  }
  function renderSteps() {
    const shownPhase = state.processing ? 4 : state.phase;
    document.querySelectorAll("#steps li").forEach((item, index) => {
      item.classList.toggle("is-current", index === shownPhase);
      item.classList.toggle("is-complete", index < shownPhase);
      if (index === shownPhase) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current");
      const number = item.querySelector(".step-number");
      if (index < shownPhase) number.replaceChildren(icon("check")); else number.textContent = String(index + 1).padStart(2, "0");
    });
    $("mobile-step-label").textContent = stepNames[shownPhase];
    $("mobile-step-count").textContent = `${shownPhase + 1} de 6`;
    document.querySelectorAll(".progress-segments i").forEach((segment, index) => segment.classList.toggle("active", index <= shownPhase));
    $("panel-eyebrow").textContent = `${String(shownPhase + 1).padStart(2, "0")} / ${stepNames[shownPhase].toLocaleUpperCase("pt")}`;
    $("session-label").textContent = state.processing ? "Envio e análise em curso" : state.phase === 5 ? "Análise concluída" : "As imagens ficam consigo até enviar";
  }
  function renderCapture() {
    const type = currentType();
    const copy = captureCopy[type];
    const capture = state.captures[type];
    $("capture-title").textContent = copy.title;
    $("capture-description").textContent = copy.description;
    $("capture-badge").textContent = copy.badge;
    $("placeholder-label").textContent = copy.placeholder;
    $("camera-placeholder-icon").replaceChildren(icon(copy.icon).firstChild);
    $("camera-stage").classList.toggle("is-selfie", type === "selfie");
    $("camera-stage").classList.toggle("has-preview", Boolean(capture));
    $("file-input").setAttribute("capture", type === "selfie" ? "user" : "environment");
    $("capture-preview").hidden = !capture;
    if (capture) $("capture-preview").src = capture.url; else $("capture-preview").removeAttribute("src");
    $("camera-empty").hidden = Boolean(capture || state.stream);
    $("captured-label").hidden = !capture;
    $("open-camera").hidden = Boolean(capture || state.stream);
    $("take-photo").hidden = !state.stream;
    $("retake-photo").hidden = !capture;
    $("capture-guide").hidden = Boolean(capture);
    $("image-meta").textContent = capture ? `${capture.width} × ${capture.height} px · ${formatBytes(capture.blob.size)} · Confirme a nitidez` : `JPEG ou PNG · máximo ${formatBytes(uploadLimit())} por imagem`;
    $("capture-tips").replaceChildren(...copy.tips.map(([title, description], index) => {
      const li = document.createElement("li"); const number = document.createElement("span"); const content = document.createElement("div");
      const strong = document.createElement("strong"); const p = document.createElement("p");
      number.textContent = String(index + 1).padStart(2, "0"); strong.textContent = title; p.textContent = description;
      content.append(strong, p); li.append(number, content); return li;
    }));
  }
  function renderReview() {
    $("review-grid").replaceChildren(...captureTypes.slice(1).map((type, index) => {
      const capture = state.captures[type]; const item = document.createElement("article");
      item.className = `review-item ${type === "selfie" ? "selfie" : ""}`;
      const image = document.createElement(capture ? "img" : "div"); image.className = "review-image";
      if (capture) { image.src = capture.url; image.alt = captureCopy[type].label; }
      else { image.classList.add("review-placeholder"); image.setAttribute("role", "img"); image.append(icon(captureCopy[type].icon)); image.setAttribute("aria-label", `${captureCopy[type].label}: fotografia em falta`); }
      const body = document.createElement("div"); body.className = "review-item-body";
      const title = document.createElement("div"); title.className = "review-item-title"; title.textContent = captureCopy[type].label; if (capture) title.append(icon("check"));
      const meta = document.createElement("p"); meta.className = "review-item-meta"; meta.textContent = capture ? `${capture.width} × ${capture.height} px · ${formatBytes(capture.blob.size)}` : "Fotografia em falta";
      const edit = document.createElement("button"); edit.type = "button"; edit.className = "text-link"; edit.dataset.edit = type;
      edit.setAttribute("aria-label", `${capture ? "Tirar novamente" : "Capturar fotografia"}: ${captureCopy[type].label.toLocaleLowerCase("pt")}`); edit.append(icon(capture ? "repeat" : "camera"), capture ? "Tirar novamente" : "Capturar fotografia");
      edit.addEventListener("click", () => {
        if (state.session) { restart("As capturas foram descartadas. Confirme novamente o consentimento para iniciar uma nova verificação."); return; }
        state.editReturn = true; setPhase(index + 1);
      });
      body.append(title, meta, edit); item.append(image, body); return item;
    }));
  }
  function render(focus = true) {
    renderSteps();
    const isCapture = state.phase > 0 && state.phase < 4;
    $("consent-panel").hidden = state.phase !== 0 || state.processing;
    $("capture-panel").hidden = !isCapture || state.processing;
    $("review-panel").hidden = state.phase !== 4 || state.processing;
    $("processing-panel").hidden = !state.processing;
    $("result-panel").hidden = state.phase !== 5 || state.processing;
    $("card-footer").hidden = state.processing;
    $("back-button").hidden = state.phase === 0 || state.phase === 5;
    $("next-button").hidden = state.phase === 5;
    $("restart-button").hidden = state.phase !== 5;
    $("next-label").textContent = state.phase === 0 ? "Concordar e começar" : state.phase === 4 ? state.session ? "Tentar novamente" : "Confirmar e verificar" : state.editReturn ? "Guardar e voltar à revisão" : state.phase === 3 ? "Rever as fotografias" : "Continuar";
    if (isCapture) renderCapture();
    if (state.phase === 4 && !state.processing) renderReview();
    updateButtons();
    if (focus) {
      const heading = state.processing ? $("processing-title") : state.phase === 0 ? $("panel-title") : isCapture ? $("capture-title") : state.phase === 4 ? $("review-title") : $("result-title");
      heading.focus({ preventScroll: true });
      if (!state.dismissed) heading.scrollIntoView({ block: "nearest", behavior: "instant" });
      announce(state.processing ? "Verificação em curso. Aguarde o resultado." : `${stepNames[state.phase]}. Etapa ${state.phase + 1} de 6.`);
    }
  }
  function setPhase(phase) {
    stopCamera(); state.imageGeneration += 1; state.phase = phase; hideError(); render();
  }
  function formatBytes(bytes) { return bytes >= 1048576 ? `${(bytes / 1048576).toLocaleString("pt", { maximumFractionDigits: 1 })} MB` : `${Math.round(bytes / 1024)} kB`; }
  const uploadLimit = () => Math.min(5 * 1024 * 1024, state.config?.max_upload_bytes || 5 * 1024 * 1024);
  const imagePixelLimit = () => state.config?.max_image_pixels || 20000000;
  function assertImageDimensions(width, height) {
    if (!Number.isFinite(width) || !Number.isFinite(height) || width < 240 || height < 240) throw new Error("A imagem tem pouca resolução. Use uma fotografia com pelo menos 240 × 240 píxeis.");
    if (width * height > imagePixelLimit()) throw new Error("A fotografia tem uma resolução demasiado elevada. Escolha uma imagem de menor resolução.");
  }
  function canvasBlob(canvas, quality) { return new Promise((resolve, reject) => canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("Não foi possível preparar a fotografia. Tente novamente.")), "image/jpeg", quality)); }
  async function normaliseImage(source, width, height) {
    assertImageDimensions(width, height);
    const scale = Math.min(1, 1600 / Math.max(width, height));
    const canvas = document.createElement("canvas"); canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) throw new Error("Este navegador não conseguiu preparar a fotografia.");
    context.fillStyle = "#ffffff"; context.fillRect(0, 0, canvas.width, canvas.height); context.drawImage(source, 0, 0, canvas.width, canvas.height);
    const blob = await canvasBlob(canvas, .86);
    if (blob.size > uploadLimit()) throw new Error(`A imagem excede o limite de ${formatBytes(uploadLimit())}. Escolha uma fotografia mais pequena.`);
    return { blob, width: canvas.width, height: canvas.height, sourceWidth: width, sourceHeight: height };
  }
  async function storeCapture(capture, phase, generation) {
    if (generation !== state.imageGeneration || phase !== state.phase || state.dismissed) return;
    const type = captureTypes[phase];
    if (state.captures[type]) URL.revokeObjectURL(state.captures[type].url);
    state.captures[type] = { ...capture, url: URL.createObjectURL(capture.blob) };
    stopCamera(); hideError(); render(false); announce(`${captureCopy[type].label}: fotografia pronta. Confirme a nitidez e continue.`);
  }
  async function openCamera() {
    if (state.busy || state.cameraPending || !$("consent").checked || !currentType()) return;
    hideError();
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      showError("A câmara precisa de uma ligação HTTPS ou localhost e de um navegador compatível. Pode usar «Escolher fotografia»."); return;
    }
    stopCamera(); const generation = state.cameraGeneration; const phase = state.phase;
    state.cameraPending = true; updateButtons(); $("open-camera").textContent = "A pedir acesso…";
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: currentType() === "selfie" ? "user" : "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } } });
      if (generation !== state.cameraGeneration || phase !== state.phase || state.dismissed) { stream.getTracks().forEach((track) => track.stop()); return; }
      state.stream = stream; $("camera-video").srcObject = stream; $("camera-video").hidden = false;
      await $("camera-video").play();
      if (generation !== state.cameraGeneration || phase !== state.phase || state.dismissed) { stream.getTracks().forEach((track) => track.stop()); return; }
      $("live-label").hidden = false; state.cameraPending = false; renderCapture(); updateButtons();
      $("take-photo").focus(); announce("Câmara activa. Enquadre a imagem e toque em Capturar fotografia.");
    } catch (error) {
      if (generation !== state.cameraGeneration || phase !== state.phase || state.dismissed) return;
      stopCamera();
      const messages = { NotAllowedError: "O acesso à câmara foi recusado. Permita a câmara nas definições do navegador ou escolha uma fotografia.", NotFoundError: "Não encontrámos uma câmara neste dispositivo. Pode escolher uma fotografia.", NotReadableError: "A câmara pode estar a ser usada noutra aplicação. Feche-a e tente novamente, ou escolha uma fotografia.", OverconstrainedError: "A câmara não suporta o enquadramento solicitado. Pode escolher uma fotografia.", SecurityError: "O navegador bloqueou o acesso à câmara. Use HTTPS ou escolha uma fotografia." };
      renderCapture(); showError(messages[error.name] || "Não foi possível abrir a câmara. Tente novamente ou escolha uma fotografia.");
    } finally {
      if (generation === state.cameraGeneration) state.cameraPending = false;
      $("open-camera").replaceChildren(icon("camera"), "Abrir câmara"); updateButtons();
    }
  }
  async function takePhoto() {
    const video = $("camera-video"); if (!state.stream || state.busy || video.readyState < 2) return;
    const phase = state.phase, generation = state.imageGeneration; state.busy = true; updateButtons();
    try { await storeCapture(await normaliseImage(video, video.videoWidth, video.videoHeight), phase, generation); }
    catch (error) { showError(error.message || "Não foi possível captar a fotografia. Tente novamente."); }
    finally { if (generation === state.imageGeneration) { state.busy = false; updateButtons(); } }
  }
  async function chooseFile(event) {
    const file = event.target.files?.[0]; event.target.value = "";
    if (!file || state.busy || !currentType() || !$("consent").checked) return;
    const phase = state.phase, generation = state.imageGeneration; state.busy = true; updateButtons(); hideError();
    let bitmap = null, temporaryUrl = null;
    try {
      if (!["image/jpeg", "image/png"].includes(file.type)) throw new Error("Escolha uma imagem JPEG ou PNG. HEIC, PDF e outros formatos não são aceites.");
      if (file.size > 24 * 1024 * 1024) throw new Error("O ficheiro é demasiado grande. Escolha uma fotografia com menos de 24 MB.");
      if (file.size === 0) throw new Error("O ficheiro está vazio. Escolha outra fotografia.");
      let source;
      if (typeof createImageBitmap === "function") { bitmap = await createImageBitmap(file, { imageOrientation: "from-image" }); source = bitmap; }
      else {
        temporaryUrl = URL.createObjectURL(file); const image = new Image(); image.src = temporaryUrl;
        await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = () => reject(new Error("Não foi possível ler esta imagem. Escolha outra fotografia.")); }); source = image;
      }
      const width = source.width || source.naturalWidth; const height = source.height || source.naturalHeight;
      await storeCapture(await normaliseImage(source, width, height), phase, generation);
    } catch (error) { if (generation === state.imageGeneration) showError(error.message || "Não foi possível ler a imagem. Escolha outra fotografia."); }
    finally { if (bitmap) bitmap.close(); if (temporaryUrl) URL.revokeObjectURL(temporaryUrl); if (generation === state.imageGeneration) { state.busy = false; updateButtons(); } }
  }
  function retakePhoto() {
    if (state.busy) return;
    const type = currentType(); stopCamera(); state.imageGeneration += 1;
    if (state.captures[type]) { URL.revokeObjectURL(state.captures[type].url); delete state.captures[type]; }
    hideError(); render(false); $("open-camera").focus(); announce("Captura descartada. Abra a câmara ou escolha outra fotografia.");
  }
  const randomKey = () => window.crypto?.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(24)), (n) => n.toString(16).padStart(2, "0")).join("");
  const learningEnabled = () => state.config?.learning_collection_enabled === true && typeof state.config.learning_policy_version === "string" && state.config.learning_policy_version.length > 0;
  const identityEnabled = () => state.config?.identity_enrollment_enabled === true && typeof state.config.identity_enrollment_policy_version === "string" && state.config.identity_enrollment_policy_version.length > 0;
  const preserveCompletedResult = () => state.submitted && (state.result?.learning?.consented === true || state.result?.identity?.biometric_template_saved === true && state.result.identity.status !== "deleted");
  function deviceGroup() { const agent = navigator.userAgent || ""; if (/Android/i.test(agent)) return "android_modern"; if (/iPhone|iPad|iPod/i.test(agent) || /Macintosh/i.test(agent) && navigator.maxTouchPoints > 1) return "ios"; return "web"; }
  async function responseJSON(response) {
    const type = response.headers.get("content-type") || "";
    let data = null;
    if (type.includes("application/json")) { try { data = await response.json(); } catch { /* The generic error below is sufficient. */ } }
    if (!response.ok) {
      const error = new Error(response.status === 429 ? "O serviço está a processar outras verificações. Aguarde um momento e tente novamente." : response.status === 503 ? "A análise está temporariamente indisponível. As capturas continuam consigo; tente novamente." : response.status === 413 ? "As imagens excedem o limite de envio. Inicie novamente e escolha fotografias mais pequenas." : [401, 403, 404, 410].includes(response.status) ? "A sessão não está disponível ou expirou. Inicie uma nova verificação." : response.status === 422 ? "Não foi possível analisar estas imagens. Confirme o formato, o documento e o enquadramento." : "Não foi possível concluir a verificação. As capturas continuam consigo; tente novamente.");
      error.status = response.status; error.code = typeof data?.error?.code === "string" ? data.error.code : null; throw error;
    }
    if (!data || typeof data !== "object") throw new Error("Recebemos uma resposta inesperada. Tente novamente dentro de momentos.");
    return data;
  }
  async function verify() {
    if (state.busy || !hasAllCaptures() || !state.config?.enabled || !$("consent").checked) return;
    const totalBytes = captureTypes.slice(1).reduce((sum, type) => sum + state.captures[type].blob.size, 0) + 4096;
    const requestLimit = typeof state.config.max_request_bytes === "number" ? state.config.max_request_bytes : 16 * 1024 * 1024;
    if (totalBytes > requestLimit) { showError("As três fotografias excedem o limite total de envio. Recapture com imagens mais pequenas; as capturas actuais continuam consigo."); return; }
    const generation = state.flowGeneration, controller = new AbortController();
    state.controller = controller;
    const active = () => generation === state.flowGeneration && !state.dismissed && state.controller === controller;
    state.busy = true; state.processing = true; hideError(); stopCamera(); render();
    try {
      if (!state.session) {
        const learningOptIn = learningEnabled() && $("learning-opt-in").checked;
        const identityOptIn = identityEnabled() && $("identity-opt-in").checked;
        const consent = { accepted: true, purpose: "onboarding", policy_version: state.config.privacy_policy_version, learning_opt_in: learningOptIn, identity_enrollment_opt_in: identityOptIn };
        if (learningOptIn) consent.learning_policy_version = state.config.learning_policy_version;
        if (identityOptIn) consent.identity_enrollment_policy_version = state.config.identity_enrollment_policy_version;
        const createdSession = await responseJSON(await fetch("/capture-api/sessions", { method: "POST", signal: controller.signal, credentials: "same-origin", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ consent, device_group: deviceGroup() }) }));
        if (!active()) return;
        state.session = createdSession;
        if (!/^[a-zA-Z0-9_-]+$/.test(state.session.session_id || "") || typeof state.session.capture_token !== "string" || !state.session.capture_token) { state.session = null; throw new Error("Não foi possível iniciar uma sessão válida. Tente novamente."); }
        state.idempotencyKey = randomKey();
      }
      if (!active()) return;
      const form = new FormData();
      captureTypes.slice(1).forEach((type) => form.append(type, state.captures[type].blob, `${type}.jpg`));
      const result = await responseJSON(await fetch(`/capture-api/sessions/${encodeURIComponent(state.session.session_id)}/verify`, { method: "POST", signal: controller.signal, credentials: "same-origin", cache: "no-store", headers: { Authorization: `Bearer ${state.session.capture_token}`, "Idempotency-Key": state.idempotencyKey }, body: form }));
      if (!active()) return;
      if (!result.score || !result.checks || typeof result.verification_id !== "string") throw new Error("A resposta não contém uma avaliação completa. Tente novamente.");
      state.result = result; state.submitted = true; state.processing = false; state.phase = 5; renderResult(result); render();
      discardCaptures();
    } catch (error) {
      if (!active()) return;
      state.processing = false;
      if ([401, 403, 404, 410].includes(error.status)) {
        state.session = null; state.idempotencyKey = null; discardCaptures(); $("consent").checked = false; $("learning-opt-in").checked = false; $("identity-opt-in").checked = false; state.phase = 0;
      }
      render(); showError(error.message || "A ligação foi interrompida. As capturas continuam consigo; tente novamente.");
    } finally { if (active()) { state.controller = null; state.busy = false; updateButtons(); } }
  }
  function checkText(check, fallback) {
    if (!check || !Array.isArray(check.reasons)) return fallback;
    const messages = check.reasons.map((reason) => reasonMessages[reason]).filter(Boolean);
    return [...new Set(messages)].slice(0, 2).join(" ") || fallback;
  }
  function fieldValue(field) {
    return typeof field?.value === "string" && field.value.trim() ? field.value : null;
  }
  function normaliseProposals(fields) {
    const proposals = {};
    if (!fields || typeof fields !== "object") return proposals;
    Object.keys(documentFieldLabels).forEach((key) => {
      const value = typeof fields[key] === "string" ? fields[key] : fieldValue(fields[key]);
      if (typeof value === "string" && value.trim()) proposals[key] = value;
    });
    return proposals;
  }
  function renderDocumentData(data) {
    const fields = data?.fields && typeof data.fields === "object" ? data.fields : {};
    const available = Object.keys(documentFieldLabels).filter((key) => fieldValue(fields[key]));
    const status = available.length ? data?.status === "extracted" ? "extracted" : "partial" : "unavailable";
    $("document-data-status").textContent = status === "extracted" ? "Dados lidos" : status === "partial" ? "Leitura parcial" : "Leitura indisponível";
    $("document-data-description").textContent = status === "unavailable"
      ? "Não foi possível extrair dados destas fotografias. Experimente uma nova verificação com o texto nítido, sem reflexos e com o documento inteiro visível."
      : status === "partial" ? "Conseguimos ler parte da informação. Confirme os dados abaixo no seu documento original. Os campos em falta não foram inferidos."
      : "Confirme a informação abaixo no seu documento original. A confiança indicada refere-se à leitura do texto.";
    const type = data?.document_type === "bi" ? "Bilhete de Identidade" : data?.document_type === "passport" ? "Passaporte" : null;
    const country = data?.issuing_country === "AO" ? "Angola" : null;
    $("document-data-kind").hidden = !type && !country;
    $("document-data-kind").textContent = [type, country].filter(Boolean).join(" · ");
    $("document-fields").replaceChildren(...Object.entries(documentFieldLabels).map(([key, label]) => {
      const field = fields[key]; const value = fieldValue(field); const row = document.createElement("div"); row.className = "document-field";
      if (!value) row.classList.add("is-unavailable");
      const title = document.createElement("dt"); title.textContent = label;
      const detail = document.createElement("dd"); const text = document.createElement("span"); text.className = "document-field-value"; text.textContent = value || "Não extraído"; detail.append(text);
      if (value) {
        const meta = document.createElement("span"); meta.className = "document-field-meta";
        const notes = [];
        if (Object.hasOwn(sourceLabels, field.source_side)) notes.push(sourceLabels[field.source_side]);
        if (typeof field.confidence === "number" && Number.isFinite(field.confidence) && field.confidence >= 0 && field.confidence <= 1) notes.push(`Confiança de leitura: ${Math.round(field.confidence * 100)}%`);
        if (Object.hasOwn(validationLabels, field.validation)) notes.push(validationLabels[field.validation]);
        meta.textContent = notes.join(" · "); detail.append(meta);
      }
      row.append(title, detail); return row;
    }));
    const conflicts = Array.isArray(data?.conflicts) ? data.conflicts : [];
    $("document-conflicts").hidden = !conflicts.length;
    $("document-conflicts").textContent = conflicts.length ? "A leitura encontrou informação que precisa de confirmação entre os lados do documento. Consulte o original antes de usar estes dados." : "";
  }
  function renderIdentity(identity) {
    const status = typeof identity?.status === "string" && Object.hasOwn(identityLabels, identity.status) ? identity.status : "unavailable";
    const saved = status === "enrolled_provisional" && identity.biometric_template_saved === true;
    const credential = saved && typeof identity.identity_id === "string" && /^[a-zA-Z0-9_-]+$/.test(identity.identity_id) && typeof identity.identity_token === "string" && identity.identity_token.length > 0;
    state.identity = credential ? { identity_id: identity.identity_id, identity_token: identity.identity_token } : null;
    $("identity-status").textContent = identityLabels[status];
    const explanations = {
      not_requested: "Não autorizou guardar uma identidade reutilizável. A leitura do documento e a nota continuam a pertencer apenas a esta verificação.",
      pending: "O registo autorizado ainda está em preparação. A verificação mantém-se preliminar.",
      enrolled_provisional: "Guardámos os dados lidos do documento e uma representação matemática do rosto de forma cifrada. O registo é provisório: não confirma a autenticidade do documento nem autoriza automaticamente operações sensíveis.",
      unavailable: "Não foi possível guardar uma identidade reutilizável com os sinais disponíveis. A leitura do documento e a nota desta verificação foram mantidas.",
      rejected: "Os sinais disponíveis não permitiram guardar um registo de identidade. Pode experimentar uma nova verificação com capturas mais nítidas.",
      deleted: "A identidade guardada foi eliminada. A leitura original, a nota desta verificação e o consentimento de aprendizagem foram mantidos de forma independente.",
    };
    $("identity-explanation").textContent = explanations[status];
    $("identity-details").hidden = !saved;
    $("identity-actions").hidden = !credential;
    const details = [];
    if (saved) {
      details.push(["Referência da identidade", identity.identity_id], ["Rosto guardado", "Representação facial cifrada (128 dimensões). As fotografias não são guardadas."], ["Estado", "Provisório — autenticidade por confirmar"]);
      if (typeof identity.retain_until === "string") {
        const until = new Date(identity.retain_until);
        if (Number.isFinite(until.getTime())) details.push(["Conservação máxima", until.toLocaleDateString("pt-AO", { year: "numeric", month: "long", day: "numeric" })]);
      }
    }
    $("identity-details").replaceChildren(...details.map(([label, value]) => {
      const row = document.createElement("div"); const title = document.createElement("dt"); const text = document.createElement("dd");
      title.textContent = label; text.textContent = typeof value === "string" ? value : "Por confirmar"; row.append(title, text); return row;
    }));
  }
  function identityError(message) {
    $("identity-error").textContent = message; $("identity-error").hidden = false; $("identity-error").focus(); announce(message);
  }
  async function prepareIdentitySelfie(file) {
    if (!["image/jpeg", "image/png"].includes(file.type)) throw new Error("Escolha uma selfie JPEG ou PNG. HEIC, PDF e outros formatos não são aceites.");
    if (file.size === 0 || file.size > 24 * 1024 * 1024) throw new Error("Escolha uma fotografia com menos de 24 MB e que não esteja vazia.");
    let bitmap = null, temporaryUrl = null;
    try {
      let source;
      if (typeof createImageBitmap === "function") { bitmap = await createImageBitmap(file, { imageOrientation: "from-image" }); source = bitmap; }
      else {
        temporaryUrl = URL.createObjectURL(file); const image = new Image(); image.src = temporaryUrl;
        await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = () => reject(new Error("Não foi possível ler esta selfie. Escolha outra fotografia.")); }); source = image;
      }
      return await normaliseImage(source, source.width || source.naturalWidth, source.height || source.naturalHeight);
    } finally { if (bitmap) bitmap.close(); if (temporaryUrl) URL.revokeObjectURL(temporaryUrl); }
  }
  async function compareIdentity(event) {
    const file = event.target.files?.[0]; event.target.value = "";
    if (!file || state.busy || !state.identity) return;
    const identity = state.identity, generation = state.flowGeneration, controller = new AbortController();
    const active = () => generation === state.flowGeneration && !state.dismissed && state.controller === controller;
    state.controller = controller; state.busy = true; state.identityAction = "compare";
    $("identity-error").hidden = true; $("identity-message").hidden = true; $("identity-comparison").hidden = true; updateButtons();
    try {
      const capture = await prepareIdentitySelfie(file); if (!active()) return;
      const form = new FormData(); form.append("selfie", capture.blob, "selfie.jpg");
      const result = await responseJSON(await fetch(`/identity-api/identities/${encodeURIComponent(identity.identity_id)}/compare`, { method: "POST", signal: controller.signal, credentials: "same-origin", cache: "no-store", headers: { Authorization: `Bearer ${identity.identity_token}` }, body: form }));
      if (!active()) return;
      if (!result.face_match || !result.liveness || result.authenticated !== false) throw new Error("O serviço devolveu uma comparação inesperada. Tente novamente.");
      const heading = document.createElement("h4"); heading.textContent = "Comparação 1:1 — autenticação por confirmar";
      const face = document.createElement("p"); face.textContent = checkText(result.face_match, "A comparação do rosto com o registo precisa de validação adicional.");
      const liveness = document.createElement("p"); liveness.textContent = checkText(result.liveness, "A análise desta selfie não confirma, por si só, que a pessoa esteja presente.");
      const note = document.createElement("p"); note.textContent = "O registo mantém-se provisório. Esta comparação não altera os dados do documento nem a nota da verificação original.";
      $("identity-comparison").replaceChildren(heading, face, liveness, note); $("identity-comparison").hidden = false;
      announce("Comparação concluída. A autenticação continua por confirmar.");
    } catch (error) { if (active()) identityError([401, 403, 404, 410].includes(error.status) ? "A credencial desta identidade expirou ou já não está disponível. A verificação original foi mantida." : error.status ? "Não foi possível comparar esta selfie. Tente novamente; a identidade e a verificação foram mantidas." : error.message || "Não foi possível preparar esta selfie."); }
    finally { if (active()) { state.controller = null; state.busy = false; state.identityAction = null; updateButtons(); } }
  }
  async function deleteIdentity() {
    if (state.busy || !state.identity) return;
    const identity = state.identity, generation = state.flowGeneration, controller = new AbortController();
    const active = () => generation === state.flowGeneration && !state.dismissed && state.controller === controller;
    state.controller = controller; state.busy = true; state.identityAction = "delete";
    $("identity-error").hidden = true; $("identity-message").hidden = true; updateButtons();
    try {
      const response = await fetch(`/identity-api/identities/${encodeURIComponent(identity.identity_id)}`, { method: "DELETE", signal: controller.signal, credentials: "same-origin", cache: "no-store", headers: { Authorization: `Bearer ${identity.identity_token}` } });
      if (response.status !== 204) await responseJSON(response);
      if (!active()) return;
      state.result.identity = { status: "deleted", identity_id: identity.identity_id, biometric_template_saved: false, authenticity_confirmed: false, identity_token: null };
      renderIdentity(state.result.identity); $("identity-comparison").replaceChildren(); $("identity-comparison").hidden = true;
      $("identity-message").textContent = "Identidade guardada eliminada. A verificação e o consentimento de aprendizagem foram mantidos."; $("identity-message").hidden = false;
      $("identity-result-title").focus({ preventScroll: true }); announce("Identidade guardada eliminada. A verificação original foi mantida.");
    } catch (error) { if (active()) identityError([401, 403, 404, 410].includes(error.status) ? "A credencial desta identidade expirou ou já não está disponível. Não foi possível confirmar a eliminação; o prazo de conservação continua a aplicar-se." : "Não foi possível confirmar a eliminação da identidade. Tente novamente; o registo mantém-se até à confirmação do serviço."); }
    finally { if (active()) { state.controller = null; state.busy = false; state.identityAction = null; updateButtons(); } }
  }
  function renderLearning(learning) {
    const status = typeof learning?.status === "string" && Object.hasOwn(learningLabels, learning.status) ? learning.status : "unavailable";
    const consented = learning?.consented === true && !["withdrawn", "not_opted_in", "unavailable"].includes(status);
    $("learning-status").textContent = learningLabels[status];
    const explanations = {
      not_opted_in: "Esta verificação não contribui para aprendizagem. Não autorizou a recolha de sinais e correcções para melhoria do sistema.",
      pending_review: "Autorizou a contribuição desta sessão. Os sinais recolhidos e as sugestões de correcção ficam disponíveis para avaliação. As sugestões precisam de revisão humana antes de serem usadas em melhorias.",
      reviewed: "A contribuição desta sessão foi revista. A revisão ajuda a avaliar melhorias e não altera automaticamente a nota nem confirma a autenticidade do documento.",
      withdrawn: "O consentimento de aprendizagem foi retirado. Os exemplos e as sugestões ligados a esta sessão foram eliminados da aprendizagem. A leitura original e a nota mantêm-se, no máximo, pelo prazo normal contado a partir da retirada.",
      unavailable: "A contribuição para aprendizagem não está disponível nesta verificação. A leitura do documento e a nota continuam disponíveis conforme os sinais analisados.",
    };
    $("learning-explanation").textContent = explanations[status];
    $("corrections-section").hidden = !consented;
    $("withdraw-learning").hidden = !consented;
    if (!consented) {
      $("correction-fields").replaceChildren(); $("proposed-fields").replaceChildren(); $("proposed-corrections").hidden = true;
      state.correctionBaseline = {}; state.proposedFields = {}; return;
    }
    const original = state.result?.document_data?.fields || {};
    state.correctionBaseline = {};
    $("correction-fields").replaceChildren(...Object.entries(documentFieldLabels).map(([key, label]) => {
      const row = document.createElement("div"); row.className = "correction-field";
      const title = document.createElement("label"); title.htmlFor = `correction-${key}`; title.textContent = ["birth_date", "expiry_date"].includes(key) ? `${label} (AAAA-MM-DD)` : label;
      const input = document.createElement("input"); input.id = `correction-${key}`; input.name = key; input.type = "text"; input.maxLength = 160;
      input.autocomplete = "off"; input.spellcheck = false; input.value = state.proposedFields[key] || fieldValue(original[key]) || "";
      input.placeholder = ["birth_date", "expiry_date"].includes(key) ? "Ex.: 1990-01-15" : "Não extraído — pode sugerir o valor"; input.dataset.field = key;
      state.correctionBaseline[key] = input.value.trim();
      row.append(title, input); return row;
    }));
    const proposals = Object.entries(normaliseProposals(state.proposedFields));
    $("proposed-corrections").hidden = !proposals.length;
    $("proposed-fields").replaceChildren(...proposals.map(([key, value]) => {
      const row = document.createElement("div"); const title = document.createElement("dt"); const detail = document.createElement("dd");
      title.textContent = documentFieldLabels[key]; detail.textContent = value; row.append(title, detail); return row;
    }));
    $("proposed-corrections").querySelector("p").textContent = status === "reviewed"
      ? "A contribuição foi revista. Os dados apresentados acima continuam a ser a leitura original."
      : "Aguardam revisão. Os dados apresentados acima continuam a ser a leitura original.";
  }
  function learningError(message) {
    $("learning-error").textContent = message; $("learning-error").hidden = false; $("learning-error").focus(); announce(message);
  }
  async function learningRequest(action, fields = null) {
    if (state.busy || !state.session || state.result?.learning?.consented !== true) return;
    const generation = state.flowGeneration, controller = new AbortController();
    const active = () => generation === state.flowGeneration && !state.dismissed && state.controller === controller;
    state.controller = controller; state.busy = true; state.learningAction = action;
    $("learning-error").hidden = true; $("learning-message").hidden = true; updateButtons();
    try {
      const url = `/capture-api/sessions/${encodeURIComponent(state.session.session_id)}/${action === "withdraw" ? "learning-consent" : "document-corrections"}`;
      const headers = { Authorization: `Bearer ${state.session.capture_token}` };
      const options = { method: action === "withdraw" ? "DELETE" : "POST", signal: controller.signal, credentials: "same-origin", cache: "no-store", headers };
      if (action !== "withdraw") { headers["Content-Type"] = "application/json"; options.body = JSON.stringify({ fields }); }
      const data = await responseJSON(await fetch(url, options));
      if (!active()) return;
      if (!data.learning || typeof data.learning.status !== "string" || typeof data.learning.consented !== "boolean") throw new Error("A resposta não confirmou esta operação. Actualize a página ou tente novamente.");
      state.result.learning = data.learning;
      state.proposedFields = action === "withdraw" ? {} : { ...state.proposedFields, ...normaliseProposals(data.proposed_fields) };
      renderLearning(data.learning);
      const message = action === "withdraw" ? "Consentimento de aprendizagem retirado. A sua verificação foi mantida." : "Sugestões enviadas para revisão humana. A leitura original e a nota foram mantidas.";
      $("learning-message").textContent = message; $("learning-message").hidden = false; announce(message);
      if (action === "withdraw") { $("learning-result-title").focus({ preventScroll: true }); $("learning-explanation").scrollIntoView({ block: "nearest", behavior: "instant" }); }
    } catch (error) {
      if (!active()) return;
      const message = action === "withdraw" ? "Não foi possível confirmar a retirada do consentimento. Tente novamente; a contribuição continua activa até à confirmação do serviço." : "Não foi possível enviar as sugestões. Os valores que escreveu continuam nesta página; tente novamente.";
      learningError([401, 403, 404, 410].includes(error.status) ? "A sessão expirou ou já não está disponível. O prazo de conservação definido pelo serviço continua a aplicar-se." : message);
    } finally {
      if (active()) { state.controller = null; state.busy = false; state.learningAction = null; updateButtons(); }
    }
  }
  function submitCorrections(event) {
    event.preventDefault(); if (state.busy) return;
    const fields = {};
    $("correction-fields").querySelectorAll("input[data-field]").forEach((input) => {
      const key = input.dataset.field; const value = input.value.trim();
      if (Object.hasOwn(documentFieldLabels, key) && value && value !== state.correctionBaseline[key]) fields[key] = value;
    });
    if (!Object.keys(fields).length) { learningError("Altere pelo menos um campo para enviar uma sugestão. Os valores vazios não substituem dados já lidos."); return; }
    const invalidDate = ["birth_date", "expiry_date"].find((key) => {
      if (!fields[key]) return false;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(fields[key])) return true;
      const date = new Date(`${fields[key]}T00:00:00Z`);
      return !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== fields[key];
    });
    if (invalidDate) { learningError(`Use uma data válida no formato AAAA-MM-DD em «${documentFieldLabels[invalidDate]}». Por exemplo: 1990-01-15.`); $(`correction-${invalidDate}`).focus(); return; }
    learningRequest("corrections", fields);
  }
  function renderResult(result) {
    const score = result.score; const rawValue = score.value;
    const value = typeof rawValue === "number" && Number.isFinite(rawValue) && rawValue >= 0 && rawValue <= 10 && score.kind === "indicative" ? rawValue : null;
    $("score-value").textContent = value === null ? "—" : value.toLocaleString("pt", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    $("score-ring-value").setAttribute("stroke-dasharray", `${value === null ? 0 : value * 10} 100`);
    $("score-label").textContent = value === null ? "Sem nota disponível" : "Sinais analisados, com transparência";
    $("score-explanation").textContent = value === null
      ? typeof score.explanation === "string" ? score.explanation : "A análise disponível não permite confirmar a autenticidade desta identidade."
      : value <= 2 ? "Os sinais recolhidos precisam de revisão. Confirme a qualidade das capturas e a correspondência com o seu documento."
      : "A nota combina os sinais da comparação do rosto, da selfie e da legibilidade das fotografias. A autenticidade do documento exige confirmação adicional.";
    const ceiling = typeof score.evidence_ceiling === "number" && score.evidence_ceiling >= 0 && score.evidence_ceiling < 10 ? score.evidence_ceiling : null;
    $("score-ceiling").hidden = ceiling === null;
    $("score-ceiling").textContent = ceiling === null ? "" : `Teto desta avaliação: ${ceiling.toLocaleString("pt")} / 10. Notas superiores exigem confirmação documental e validação adicional.`;
    $("result-status").textContent = result.status === "rejected" ? "Precisa de revisão" : "Avaliação preliminar";
    $("authenticity-state").replaceChildren(icon("info"), "Autenticidade por confirmar");
    const checks = [
      { title: "Frente do documento", icon: "document", outcome: result.checks.document_front?.outcome, text: checkText(result.checks.document_front, "A fotografia do documento foi recebida. A autenticidade exige confirmação adicional.") },
      { title: "Verso do documento", icon: "document", outcome: result.checks.document_back?.outcome, text: checkText(result.checks.document_back, "O verso foi recebido. A autenticidade exige confirmação adicional.") },
      { title: "A sua selfie", icon: "person", outcome: result.checks.liveness?.outcome === "fail" || result.checks.face_match?.outcome === "fail" ? "fail" : result.checks.liveness?.outcome === "pass" && result.checks.face_match?.outcome === "pass" ? "pass" : "inconclusive", text: `${checkText(result.checks.face_match, "A comparação do rosto precisa de validação adicional.")} ${checkText(result.checks.liveness, "A análise da selfie não confirma, por si só, uma pessoa real.")}` },
    ];
    $("result-checks").replaceChildren(...checks.map((check) => {
      const card = document.createElement("article"); card.className = "result-check";
      const title = document.createElement("h3"); title.className = "result-check-heading"; title.append(icon(check.icon), check.title);
      const status = document.createElement("span"); status.className = `check-state ${["pass", "fail"].includes(check.outcome) ? check.outcome : ""}`;
      status.textContent = check.outcome === "pass" ? "Sinais consistentes" : check.outcome === "fail" ? "A rever" : "Por confirmar";
      const text = document.createElement("p"); text.textContent = check.text; card.append(title, status, text); return card;
    }));
    const baseLimitations = ["Esta nota é indicativa e não certifica a autenticidade da sua identidade.", "A autenticidade do documento exige confirmação adicional.", "Uma fotografia ou selfie isolada não comprova que a pessoa esteja presente neste momento."];
    $("limitations-list").replaceChildren(...baseLimitations.map((text) => { const li = document.createElement("li"); li.textContent = text; return li; }));
    $("result-reference").textContent = result.verification_id;
    state.proposedFields = normaliseProposals(result.proposed_fields);
    $("learning-error").hidden = true; $("learning-message").hidden = true;
    $("identity-error").hidden = true; $("identity-message").hidden = true; $("identity-comparison").hidden = true; $("identity-comparison").replaceChildren();
    renderDocumentData(result.document_data); renderIdentity(result.identity); renderLearning(result.learning);
  }
  function discardCaptures() {
    Object.values(state.captures).forEach((capture) => URL.revokeObjectURL(capture.url)); state.captures = {};
    $("capture-preview").removeAttribute("src"); $("review-grid").replaceChildren(); $("file-input").value = "";
  }
  function discardResultData() {
    state.result = null; state.proposedFields = {}; state.correctionBaseline = {}; state.identity = null;
    $("document-fields").replaceChildren(); $("correction-fields").replaceChildren(); $("proposed-fields").replaceChildren();
    $("result-reference").textContent = "—"; $("learning-error").textContent = ""; $("learning-message").textContent = "";
    $("identity-details").replaceChildren(); $("identity-comparison").replaceChildren(); $("identity-selfie-input").value = "";
    $("identity-error").textContent = ""; $("identity-message").textContent = "";
  }
  async function deleteSession(session) {
    if (!session) return true;
    try {
      const response = await fetch(`/capture-api/sessions/${encodeURIComponent(session.session_id)}`, { method: "DELETE", credentials: "same-origin", cache: "no-store", keepalive: true, headers: { Authorization: `Bearer ${session.capture_token}` } });
      return response.ok || [404, 410].includes(response.status);
    } catch { return false; }
  }
  async function restart(message = "") {
    if (state.busy) return;
    const generation = ++state.flowGeneration;
    if (state.controller) { state.controller.abort(); state.controller = null; }
    const preserve = preserveCompletedResult();
    const previous = state.session; state.session = null; state.idempotencyKey = null; discardResultData(); state.learningAction = null;
    state.identityAction = null;
    state.submitted = false; state.editReturn = false; stopCamera(); state.imageGeneration += 1; discardCaptures();
    $("consent").checked = false; $("learning-opt-in").checked = false; $("identity-opt-in").checked = false; state.phase = 0; hideError(); render();
    $("restart-notice").hidden = !preserve;
    $("restart-notice").textContent = preserve ? "As capturas, os dados apresentados e as credenciais foram limpos desta página. A contribuição ou a identidade anteriormente autorizadas foram mantidas no serviço pelos respectivos prazos de conservação." : "";
    if (message) announce(message);
    if (!preserve && !await deleteSession(previous) && generation === state.flowGeneration && !state.dismissed) showError("As capturas deste dispositivo foram descartadas, mas não foi possível confirmar a eliminação do resultado no serviço. O prazo de conservação continua a aplicar-se.");
  }
  async function loadConfig() {
    try {
      const config = await responseJSON(await fetch("/capture-api/config", { credentials: "same-origin", cache: "no-store" }));
      if (typeof config.enabled !== "boolean" || typeof config.privacy_policy_version !== "string") throw new Error("O serviço devolveu uma configuração incompleta.");
      state.config = config;
      if (!config.enabled) { $("config-warning").textContent = "Este serviço ainda não está disponível para novas verificações. Tente novamente mais tarde."; $("config-warning").hidden = false; }
      else if (config.mode === "demo") { $("config-warning").textContent = "Modo de demonstração: pode experimentar o fluxo de captura, mas não será emitida uma nota de autenticidade."; $("config-warning").hidden = false; }
      if (typeof config.retention_days === "number" && config.retention_days >= 0) $("privacy-retention").textContent = `Os resultados cifrados são conservados por até ${config.retention_days} ${config.retention_days === 1 ? "dia" : "dias"}. As imagens não são guardadas pelo serviço.`;
      const enabled = learningEnabled();
      $("learning-availability").textContent = enabled ? "Pode retirar esta autorização no resultado, mantendo a verificação." : "A contribuição para melhoria está desactivada neste serviço. Pode continuar com a sua verificação.";
      if (!enabled) $("learning-opt-in").checked = false;
      if (enabled && typeof config.learning_retention_days === "number" && config.learning_retention_days >= 0) {
        const days = `${config.learning_retention_days} ${config.learning_retention_days === 1 ? "dia" : "dias"}`;
        const normalDays = typeof config.retention_days === "number" && config.retention_days >= 0 ? `${config.retention_days} ${config.retention_days === 1 ? "dia" : "dias"}` : "o prazo normal do serviço";
        $("learning-consent-description").textContent = `Se autorizar, os sinais da análise, os dados lidos e as suas sugestões de correcção serão guardados de forma cifrada por até ${days} para avaliação. As sugestões precisam de revisão humana antes de contribuir para melhorias. Sem esta autorização, os resultados ficam por até ${normalDays}. Não guardamos fotografias para treino nem alteramos automaticamente os modelos em cada verificação.`;
        $("privacy-learning-retention").textContent = `Com a contribuição voluntária, os resultados, sinais e sugestões cifrados ficam por até ${days}. Ao retirar o consentimento, os exemplos e as sugestões são eliminados da aprendizagem; a leitura original e a nota ficam, no máximo, por ${normalDays} após a retirada e podem expirar antes.`;
        $("privacy-learning-retention").hidden = false;
      } else $("privacy-learning-retention").hidden = true;
      const identityAvailable = identityEnabled();
      $("identity-availability").textContent = identityAvailable ? "Pode eliminar a identidade guardada no resultado, sem alterar a verificação nem a contribuição para melhoria." : "Guardar uma identidade reutilizável está desactivado neste serviço. Pode continuar com a verificação.";
      if (!identityAvailable) $("identity-opt-in").checked = false;
      if (identityAvailable && typeof config.identity_retention_days === "number" && config.identity_retention_days >= 0) {
        const days = `${config.identity_retention_days} ${config.identity_retention_days === 1 ? "dia" : "dias"}`;
        $("identity-consent-description").textContent = `Se autorizar, guardamos os dados lidos do documento e uma representação matemática cifrada do rosto por até ${days}, para comparações futuras. O registo é provisório e não confirma automaticamente a autenticidade da identidade. As fotografias não são guardadas. Este consentimento é independente da aprendizagem.`;
        identityPrivacy.textContent = `Guardar uma identidade reutilizável exige autorização separada: os dados do documento e a representação biométrica cifrada ficam por até ${days}, sem guardar fotografias. O registo é provisório. Pode eliminá-lo no resultado, mantendo esta verificação e a contribuição de aprendizagem. As comparações utilizam apenas o registo indicado (1:1).`;
        identityPrivacy.hidden = false;
      } else identityPrivacy.hidden = true;
      updateButtons();
    } catch { showError("Não foi possível carregar o serviço de verificação. Actualize a página para tentar novamente."); }
  }
  $("consent").addEventListener("change", updateButtons);
  $("learning-opt-in").addEventListener("change", updateButtons);
  $("identity-opt-in").addEventListener("change", updateButtons);
  $("identity-selfie-input").addEventListener("change", compareIdentity);
  $("compare-identity").addEventListener("click", () => { if (!state.busy && state.identity) $("identity-selfie-input").click(); });
  $("delete-identity").addEventListener("click", deleteIdentity);
  $("document-corrections-form").addEventListener("submit", submitCorrections);
  $("withdraw-learning").addEventListener("click", () => learningRequest("withdraw"));
  $("open-camera").addEventListener("click", openCamera);
  $("take-photo").addEventListener("click", takePhoto);
  $("retake-photo").addEventListener("click", retakePhoto);
  $("file-input").addEventListener("change", chooseFile);
  $("file-label").addEventListener("click", () => { if (!state.busy) $("file-input").click(); });
  $("camera-video").addEventListener("loadeddata", updateButtons);
  $("next-button").addEventListener("click", () => {
    if (state.busy || $("next-button").disabled) return;
    if (state.phase === 4) { verify(); return; }
    if (state.editReturn && state.phase > 0) { state.editReturn = false; setPhase(4); return; }
    setPhase(state.phase + 1);
  });
  $("back-button").addEventListener("click", () => {
    if (state.busy) return;
    if (state.session) { restart(); return; }
    if (state.editReturn) { state.editReturn = false; setPhase(4); return; }
    setPhase(Math.max(0, state.phase - 1));
  });
  $("restart-button").addEventListener("click", () => restart());
  document.querySelectorAll("[data-open-privacy]").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); $("privacy-dialog").showModal(); }));
  document.querySelectorAll("[data-open-help]").forEach((button) => button.addEventListener("click", () => $("help-dialog").showModal()));
  document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } }));
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") { stopCamera(); if (currentType() && !state.processing) { renderCapture(); updateButtons(); } } });
  window.addEventListener("pagehide", () => { state.dismissed = true; state.flowGeneration += 1; if (state.controller) { state.controller.abort(); state.controller = null; } stopCamera(); state.imageGeneration += 1; discardCaptures(); discardResultData(); state.session = null; state.idempotencyKey = null; });
  window.addEventListener("pageshow", (event) => { if (event.persisted) { state.flowGeneration += 1; state.dismissed = false; state.busy = false; state.processing = false; discardResultData(); state.learningAction = null; state.identityAction = null; state.phase = 0; $("consent").checked = false; $("learning-opt-in").checked = false; $("identity-opt-in").checked = false; render(false); loadConfig(); } });
  render(false); loadConfig();
})();
