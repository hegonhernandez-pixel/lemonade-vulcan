"use strict";
const ESTUDIO = { imagenes: [], total: 0, offset: 0, seleccion: null, enviando: false, error: "", firma: "", fotoURL: null, cargando: false };

function controlesEstudio() {
  const foto = !!$("#estArchivo").files.length, rapido = $("#estCalidad").value === "rapida";
  const intensidad = $("#estIntensidad");
  intensidad.min = rapido ? "0.25" : "0.04";
  intensidad.step = rapido ? "0.25" : "0.01";
  let valor = Math.max(+intensidad.min, +intensidad.value);
  if (rapido) valor = Math.max(.25, Math.round(valor * 4) / 4);
  intensidad.value = valor;
  $("#estIntensidadCampo").hidden = !foto;
  $("#estIntensidadValor").textContent = `${Math.round(valor * 100)}%`;
  $("#estIntensidadNota").textContent = T(rapido ? "est.intensidad_turbo" : "est.intensidad_base");
  $("#estFotoCaja").hidden = !foto;
  const activo = ESTUDIO.imagenes.some(i => ["en cola", "corriendo"].includes(i.estado));
  $("#estGenerar").disabled = ESTUDIO.enviando || activo || !puedeExportar();
  $("#estGenerar").textContent = T(ESTUDIO.enviando ? "est.enviando" : activo ? "est.en_proceso" : "est.generar");
}

function renderEstudio() {
  controlesEstudio();
  const items = ESTUDIO.imagenes;
  const elegido = items.find(i => i.id === ESTUDIO.seleccion?.id);
  if (elegido) ESTUDIO.seleccion = elegido;
  if (!ESTUDIO.seleccion && items.length) ESTUDIO.seleccion = items[0];
  const sel = ESTUDIO.seleccion;
  $("#estError").hidden = !ESTUDIO.error;
  $("#estError").textContent = ESTUDIO.error;
  $("#estCantidad").textContent = T("est.cantidad", { n: ESTUDIO.total });
  $("#estAnterior").disabled = ESTUDIO.offset === 0;
  $("#estSiguiente").disabled = ESTUDIO.offset + 24 >= ESTUDIO.total;
  $("#estPagina").textContent = `${Math.floor(ESTUDIO.offset / 24) + 1} / ${Math.max(1, Math.ceil(ESTUDIO.total / 24))}`;
  $("#estGaleria").innerHTML = items.length ? items.map(i => `
    <button type="button" class="est-mini ${sel?.id === i.id ? "seleccionada" : ""}" data-est-id="${esc(i.id)}" aria-pressed="${sel?.id === i.id}">
      ${i.url ? `<img loading="lazy" src="${esc(i.url)}" alt="${esc(i.prompt)}">` : `<span class="est-mini-estado">${["en cola", "corriendo"].includes(i.estado) ? '<span class="giro"></span>' : "!"}${esc(T(`est.estado_${i.estado.replaceAll(" ", "_")}`))}</span>`}
      <span class="est-mini-texto">${esc(i.prompt)}</span><small>${esc(i.formato)} · ${esc(T(`est.${i.calidad}`))}</small>
    </button>`).join("") : `<div class="est-vacio"><strong>${T("est.galeria_vacia")}</strong><p>${T("est.galeria_vacia_d")}</p></div>`;
  const preview = $("#estPreview");
  if (!sel) {
    preview.innerHTML = `<div class="est-vacio"><span class="est-simbolo" aria-hidden="true">✦</span><h2>${T("est.preview_vacio")}</h2><p>${T("est.preview_vacio_d")}</p></div>`;
    $("#estDetalle").hidden = true;
    return;
  }
  $("#estDetalle").hidden = false;
  const activo = ["en cola", "corriendo"].includes(sel.estado), t = sel.trabajo;
  const paso = t?.pasos?.find(p => p.estado === "corriendo");
  const porcentaje = Math.min(100, Math.round((t?.progreso || 0) * 100));
  preview.innerHTML = sel.url ? `<button type="button" class="est-ampliar" aria-label="${esc(T("est.ampliar"))}"><img src="${esc(sel.url)}" alt="${esc(sel.prompt)}"></button>`
    : activo ? `<div class="est-vacio" role="status"><span class="giro"></span><h2>${T(`est.estado_${sel.estado.replaceAll(" ", "_")}`)}</h2><p>${esc(paso?.nombre || T("est.espera_cola"))}</p><progress max="100" value="${porcentaje}" aria-label="${esc(T("est.progreso"))}"></progress><p>${porcentaje}%</p></div>`
    : `<div class="est-vacio" role="alert"><h2>${T("est.estado_error")}</h2><p>${esc(sel.error || T("est.err_generar"))}</p></div>`;
  $("#estPromptDetalle").textContent = sel.prompt;
  $("#estMetaDetalle").textContent = `${sel.formato} · ${T(`est.${sel.calidad}`)} · ${T(sel.con_foto ? "est.desde_foto" : "est.desde_texto")} · ${new Date(sel.creado * 1000).toLocaleString(LANG)}`;
  $("#estPNG").disabled = $("#estJPG").disabled = !sel.url;
  $("#estEliminar").disabled = activo;
  const img = $(".est-ampliar", preview);
  if (img) img.onclick = () => verImagen(sel.url);
}

async function cargarEstudio(forzar = false) {
  if (ESTUDIO.cargando) return;
  ESTUDIO.cargando = true;
  try {
    const { data } = await api("GET", `/api/estudio?offset=${ESTUDIO.offset}`);
    const firma = JSON.stringify(data);
    ESTUDIO.imagenes = data.imagenes; ESTUDIO.total = data.total;
    if (ESTUDIO.offset && !data.imagenes.length) {
      ESTUDIO.offset = Math.max(0, ESTUDIO.offset - 24);
      ESTUDIO.firma = "";
      ESTUDIO.cargando = false;
      await cargarEstudio(true);
      return;
    }
    const habiaError = !!ESTUDIO.error;
    ESTUDIO.error = "";
    if (forzar || habiaError || ESTUDIO.firma !== firma) {
      ESTUDIO.firma = firma;
      renderEstudio();
    } else controlesEstudio();
  } catch (e) {
    ESTUDIO.error = e.message;
    renderEstudio();
  } finally { ESTUDIO.cargando = false; }
}

function resumenPresupuestoVisual(visual, conEstilo = false) {
  const partes = [T(conEstilo ? "visual.escena" : "visual.estudio", visual)];
  if (visual.palabras > visual.limite) partes.push(T("visual.recorte", visual));
  if (visual.aviso) partes.push(T(visual.aviso.exacto ? "visual.tokens" : "visual.tokens_aprox", visual.aviso));
  return partes.join(" ");
}

/** Muestra el presupuesto del texto que el Estudio envía al motor. */
let tPromptEstudio = null;
function revisarPromptEstudio() {
  clearTimeout(tPromptEstudio);
  tPromptEstudio = setTimeout(async () => {
    const texto = $("#estPrompt").value.trim(), nota = $("#estPromptLargo");
    let visual = null;
    if (texto) { try { visual = (await api("POST", "/api/prompt/clip", { texto })).data; } catch { visual = null; } }
    if (texto !== $("#estPrompt").value.trim()) return;  // cambió mientras preguntaba: responde la siguiente
    nota.hidden = !visual;
    nota.textContent = visual ? resumenPresupuestoVisual(visual) : "";
  }, 500);
}

async function generarEstudioUI(e) {
  e.preventDefault();
  if (ESTUDIO.enviando) return;
  const prompt = $("#estPrompt").value.trim();
  if (!prompt) { $("#estPrompt").focus(); return; }
  const fd = new FormData();
  fd.append("prompt", prompt);
  fd.append("formato", $("#estFormato").value);
  fd.append("calidad", $("#estCalidad").value);
  fd.append("intensidad", $("#estIntensidad").value);
  const foto = $("#estArchivo").files[0];
  if (foto) fd.append("archivo", foto);
  ESTUDIO.enviando = true; ESTUDIO.error = "";
  controlesEstudio();
  try {
    const r = await fetch("/api/estudio", { method: "POST", headers: cabeceras(), body: fd });
    const data = await r.json().catch(() => null);
    if (!r.ok) throw new Error(errorHttp(data, r.status));
    ESTUDIO.seleccion = data; ESTUDIO.offset = 0;
    await cargarEstudio(true);
    programarPoll(0);
  } catch (err) { ESTUDIO.error = err.message; renderEstudio(); }
  finally { ESTUDIO.enviando = false; controlesEstudio(); }
}

async function descargarEstudio(formato) {
  const sel = ESTUDIO.seleccion;
  if (!sel?.url) return;
  const b = $(formato === "png" ? "#estPNG" : "#estJPG");
  b.disabled = true;
  try {
    const r = await fetch(`${sel.url}?formato=${formato}&descarga=true`, { headers: cabeceras() });
    if (!r.ok) throw new Error(errorHttp(await r.json().catch(() => null), r.status));
    const url = URL.createObjectURL(await r.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: `dcrypto-${sel.id.slice(0, 8)}.${formato}` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (err) { toast(err.message, "error"); }
  finally { b.disabled = false; }
}

function conectarEstudio() {
  $("#estSubir").onclick = () => $("#estArchivo").click();
  $("#estForm").onsubmit = generarEstudioUI;
  $("#estPrompt").addEventListener("input", revisarPromptEstudio);
  $("#estCalidad").onchange = controlesEstudio;
  $("#estIntensidad").oninput = controlesEstudio;
  $("#estArchivo").onchange = () => {
    const file = $("#estArchivo").files[0];
    if (ESTUDIO.fotoURL) URL.revokeObjectURL(ESTUDIO.fotoURL);
    ESTUDIO.fotoURL = null;
    if (file && (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 12 * 1024 * 1024)) {
      $("#estArchivo").value = ""; toast(T("est.err_foto"), "error");
    } else if (file) {
      ESTUDIO.fotoURL = URL.createObjectURL(file);
      $("#estFotoPreview").src = ESTUDIO.fotoURL;
    }
    if (!ESTUDIO.fotoURL) $("#estFotoPreview").removeAttribute("src");
    controlesEstudio();
  };
  $("#estQuitarFoto").onclick = () => { $("#estArchivo").value = ""; $("#estArchivo").onchange(); };
  $("#estGaleria").onclick = e => {
    const b = e.target.closest("[data-est-id]");
    if (b) { ESTUDIO.seleccion = ESTUDIO.imagenes.find(i => i.id === b.dataset.estId); renderEstudio(); }
  };
  $("#estActualizar").onclick = () => cargarEstudio(true);
  $("#estAnterior").onclick = () => { ESTUDIO.offset = Math.max(0, ESTUDIO.offset - 24); cargarEstudio(true); };
  $("#estSiguiente").onclick = () => { ESTUDIO.offset += 24; cargarEstudio(true); };
  $("#estPNG").onclick = () => descargarEstudio("png");
  $("#estJPG").onclick = () => descargarEstudio("jpg");
  $("#estEliminar").onclick = async () => {
    const sel = ESTUDIO.seleccion;
    if (!sel || !await confirmarAccion(T("est.confirmar_eliminar"), T("est.eliminar"))) return;
    $("#estEliminar").disabled = true;
    try {
      await api("DELETE", `/api/estudio/${sel.id}`);
      ESTUDIO.seleccion = null;
      await cargarEstudio(true);
      toast(T("est.eliminada"), "ok");
    } catch (err) { toast(err.message, "error"); $("#estEliminar").disabled = false; }
  };
}
