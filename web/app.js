/**
 * DuyguAI Studio & İkizler Masal Durağı — Web Test & Production Studio Client
 */

const PROJECT_DUYGU = "DuyguAI Studio";
const PROJECT_IKIZLER = "İkizler Masal Durağı";

let currentProject = PROJECT_DUYGU;
let allPresets = {};
let currentConfig = {};
let logOffset = 0;
let pollTimer = null;
let isJobRunning = false;
let currentActiveFolder = "";

// Sayfa Yüklendiğinde Başlat
document.addEventListener("DOMContentLoaded", async () => {
  await fetchConfig();
  await fetchPresets();
  await pollStatus();
  await loadOutputsList();

  // Periyodik durum kontrolü
  pollTimer = setInterval(pollStatus, 2000);
});

// ---------------------------------------------------------------------------
// 1. Proje Değiştirme
// ---------------------------------------------------------------------------
function switchProject(projectName) {
  currentProject = projectName;
  const isDuygu = projectName === PROJECT_DUYGU;

  // Tema ve Arayüz Sınıfları
  document.body.className = isDuygu ? "theme-duygu" : "theme-ikizler";
  document.getElementById("tabDuygu").classList.toggle("active", isDuygu);
  document.getElementById("tabIkizler").classList.toggle("active", !isDuygu);

  // Başlıklar ve İkonlar
  document.getElementById("brandIcon").textContent = isDuygu ? "🎬" : "🧚‍♀️";
  document.getElementById("panelHeading").textContent = isDuygu
    ? "🎬 DuyguAI Studio Kampanya Tanımı"
    : "🧚‍♀️ İkizler Masal Durağı İçerik Tanımı";

  // Form İpuçları
  document.getElementById("briefHint").textContent = isDuygu
    ? "Reklamı yapılacak ürün, hedef kitle, faydalar"
    : "Masal konusu, bilmece teması, pedagojik mesaj veya deney";

  // İçerik Türü Varsayılanı
  const typeSelect = document.getElementById("inputContentType");
  typeSelect.value = isDuygu ? "ürün reklamı" : "çocuk masalı";

  // Gem Linki Güncelle
  updateGemLink();

  // Hazır Şablonları Render Et
  renderPresets();
}

function updateGemLink() {
  const isDuygu = currentProject === PROJECT_DUYGU;
  const gemUrl = isDuygu
    ? (currentConfig.gem_url_duygu_ai || "https://gemini.google.com/gem/af11977bbdce")
    : (currentConfig.gem_url_ikizler || "https://gemini.google.com/gem/8e63671e6329");

  const linkEl = document.getElementById("activeGemLink");
  linkEl.href = gemUrl;
  linkEl.textContent = isDuygu ? "DuyguAI Gem (af11977bbdce) ↗" : "İkizler Gem (8e63671e6329) ↗";
}

// ---------------------------------------------------------------------------
// 2. Yapılandırma ve Hazır Test Şablonları (Presets)
// ---------------------------------------------------------------------------
async function fetchConfig() {
  try {
    const res = await fetch("/api/config");
    currentConfig = await res.json();
    if (currentConfig.selected_project) {
      currentProject = currentConfig.selected_project;
      switchProject(currentProject);
    }
  } catch (err) {
    console.error("Config fetch error:", err);
  }
}

async function fetchPresets() {
  try {
    const res = await fetch("/api/presets");
    allPresets = await res.json();
    renderPresets();
  } catch (err) {
    console.error("Presets fetch error:", err);
  }
}

function renderPresets() {
  const container = document.getElementById("presetsList");
  container.innerHTML = "";

  const list = allPresets[currentProject] || [];
  list.forEach((preset, idx) => {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "preset-pill";
    pill.textContent = preset.name;
    pill.onclick = () => applyPreset(preset);
    container.appendChild(pill);
  });
}

function applyPreset(preset) {
  document.getElementById("inputBrief").value = preset.brief || "";
  document.getElementById("inputScenario").value = preset.scenario || "";
  if (preset.content_type) document.getElementById("inputContentType").value = preset.content_type;
  if (preset.video_format) document.getElementById("inputVideoFormat").value = preset.video_format;
  if (preset.scene_count) document.getElementById("inputSceneCount").value = preset.scene_count;
  if (preset.seconds_per_scene) document.getElementById("inputSeconds").value = preset.seconds_per_scene;
  if (typeof preset.voiceover === "boolean") {
    document.getElementById("checkVoiceover").checked = preset.voiceover;
  }

  appendLog("INFO", `⚡ "${preset.name}" test şablonu yüklendi.`);
}

// ---------------------------------------------------------------------------
// 3. Durum Kontrolü & Canlı Günlükler (Polling)
// ---------------------------------------------------------------------------
async function pollStatus() {
  try {
    const res = await fetch(`/api/status?offset=${logOffset}`);
    const data = await res.json();

    // Chrome CDP Rozeti Güncelle
    updateChromeChip(data.chrome);

    // İş Durumu ve Günlükler
    if (data.job) {
      updateJobProgress(data.job);
    }
  } catch (err) {
    // Sunucu yanıt vermiyorsa
    const chip = document.getElementById("chromeStatusChip");
    const pulse = document.getElementById("chromePulse");
    pulse.className = "status-pulse offline";
    document.getElementById("chromeTitle").textContent = "Sunucu Bağlantısı Bekleniyor…";
  }
}

function updateChromeChip(chrome) {
  const pulse = document.getElementById("chromePulse");
  const title = document.getElementById("chromeTitle");
  const detail = document.getElementById("chromeDetail");

  if (chrome && chrome.online) {
    pulse.className = "status-pulse online";
    title.textContent = `Chrome CDP Aktif (${chrome.gemini_tabs_count} Gemini sekmesi)`;
    
    let gemInfo = [];
    if (chrome.duygu_ai_gem_open) gemInfo.push("DuyguAI Gem ✅");
    if (chrome.ikizler_gem_open) gemInfo.push("İkizler Gem ✅");
    detail.textContent = gemInfo.length > 0 ? gemInfo.join(" | ") : "Port 9222 bağlı";
  } else {
    pulse.className = "status-pulse offline";
    title.textContent = "Chrome CDP Kapalı";
    detail.textContent = "Port 9222 üzerinde Chrome bulunamadı";
  }
}

function updateJobProgress(job) {
  isJobRunning = job.is_running;

  // Buton durumlarını güncelle
  document.getElementById("btnRunFull").disabled = isJobRunning;
  document.getElementById("btnRunStage1").disabled = isJobRunning;
  document.getElementById("btnRunStage2").disabled = isJobRunning;
  document.getElementById("btnStop").classList.toggle("hidden", !isJobRunning);

  // Stepper ilerleme çubuğu
  updateStepper(job.stage);

  // Yeni günlükleri ekle
  if (job.logs && job.logs.length > 0) {
    job.logs.forEach(log => {
      appendLog(log.level, log.msg, log.time);
    });
    logOffset = job.total_logs;
  }

  // Aktif çıktı klasörü varsa
  if (job.active_output_dir) {
    const folderParts = job.active_output_dir.split("/");
    currentActiveFolder = folderParts[folderParts.length - 1];
  }

  // İş bittiğinde çıktı detaylarını yükle
  if (!isJobRunning && job.stage === "completed" && currentActiveFolder) {
    loadOutputDetail(currentActiveFolder);
    loadOutputsList();
  }
}

function updateStepper(stage) {
  const n1 = document.getElementById("stepNode1");
  const n2 = document.getElementById("stepNode2");
  const n3 = document.getElementById("stepNode3");
  const n4 = document.getElementById("stepNode4");
  const c1 = document.getElementById("stepConn1");
  const c2 = document.getElementById("stepConn2");
  const c3 = document.getElementById("stepConn3");

  // Sıfırla
  [n1, n2, n3, n4].forEach(n => n.className = "step-node");
  [c1, c2, c3].forEach(c => c.className = "step-connector");

  if (stage === "stage_1_script") {
    n1.classList.add("active");
  } else if (stage === "stage_2_images") {
    n1.classList.add("completed");
    c1.classList.add("completed");
    n2.classList.add("active");
  } else if (stage === "stage_3_videos") {
    n1.classList.add("completed");
    c1.classList.add("completed");
    n2.classList.add("completed");
    c2.classList.add("completed");
    n3.classList.add("active");
  } else if (stage === "stage_4_delivery" || stage === "completed") {
    n1.classList.add("completed");
    c1.classList.add("completed");
    n2.classList.add("completed");
    c2.classList.add("completed");
    n3.classList.add("completed");
    c3.classList.add("completed");
    n4.classList.add("completed");
  }
}

// ---------------------------------------------------------------------------
// 4. Günlük Terminali
// ---------------------------------------------------------------------------
function appendLog(level, message, timeStr) {
  const terminal = document.getElementById("terminalLogs");
  const time = timeStr || new Date().toLocaleTimeString();

  const line = document.createElement("div");
  line.className = `log-line ${level.toLowerCase()}`;
  line.innerHTML = `
    <span class="log-time">[${time}]</span>
    <span class="log-text">${escapeHtml(message)}</span>
  `;

  terminal.appendChild(line);
  terminal.scrollTop = terminal.scrollHeight;
}

function clearLogs() {
  document.getElementById("terminalLogs").innerHTML = "";
}

function escapeHtml(str) {
  if (!str) return "";
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---------------------------------------------------------------------------
// 5. İş Akışını Başlatma ve Durdurma
// ---------------------------------------------------------------------------
async function startWorkflow(stageFilter = "all") {
  const brief = document.getElementById("inputBrief").value.trim();
  const scenario = document.getElementById("inputScenario").value.trim();

  if (!brief && !scenario) {
    alert("Lütfen en azından bir Brief veya Senaryo açıklaması girin (veya üstteki hazır şablonlardan birine tıklayın).");
    return;
  }

  const payload = {
    project_name: currentProject,
    brief: brief,
    scenario: scenario,
    content_type: document.getElementById("inputContentType").value,
    video_format: document.getElementById("inputVideoFormat").value,
    scene_count: parseInt(document.getElementById("inputSceneCount").value, 10),
    seconds_per_scene: parseInt(document.getElementById("inputSeconds").value, 10),
    dry_run: document.getElementById("checkDryRun").checked,
    voiceover: document.getElementById("checkVoiceover").checked,
    stage_filter: stageFilter,
  };

  // UI'ı hazırla
  switchStudioTab("logs");
  appendLog("INFO", `🚀 [${currentProject}] iş akışı başlatılıyor (Aşama Filtresi: ${stageFilter})…`);

  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await res.json();
    if (!result.ok) {
      appendLog("ERROR", `Başlatma hatası: ${result.error}`);
      alert(result.error);
    }
  } catch (err) {
    appendLog("ERROR", `İstek hatası: ${err}`);
  }
}

async function stopWorkflow() {
  if (!confirm("Devam eden iş akışını durdurmak istediğinize emin misiniz?")) return;
  try {
    await fetch("/api/stop", { method: "POST" });
    appendLog("WARN", "Durdurma sinyali sunucuya iletildi.");
  } catch (err) {
    console.error("Stop error:", err);
  }
}

// ---------------------------------------------------------------------------
// 6. Stüdyo Tab Değiştirme
// ---------------------------------------------------------------------------
function switchStudioTab(tabKey) {
  const tabs = {
    logs: { btn: "tabViewLogs", pane: "paneLogs" },
    scenario: { btn: "tabViewScenario", pane: "paneScenario" },
    gallery: { btn: "tabViewGallery", pane: "paneGallery" },
    outputs: { btn: "tabViewOutputs", pane: "paneOutputs" },
  };

  Object.keys(tabs).forEach(k => {
    const isTarget = k === tabKey;
    document.getElementById(tabs[k].btn).classList.toggle("active", isTarget);
    document.getElementById(tabs[k].pane).classList.toggle("active", isTarget);
  });
}

function toggleScenarioView(viewKey) {
  const isJson = viewKey === "json";
  document.getElementById("btnSubJson").classList.toggle("active", isJson);
  document.getElementById("btnSubRaw").classList.toggle("active", !isJson);
  document.getElementById("sceneCardsList").classList.toggle("hidden", !isJson);
  document.getElementById("rawScenarioText").classList.toggle("hidden", isJson);
}

// ---------------------------------------------------------------------------
// 7. Senaryo & JSON Kartları Render
// ---------------------------------------------------------------------------
function renderSceneCards(scenes, rawText) {
  const container = document.getElementById("sceneCardsList");
  const rawEl = document.getElementById("rawScenarioText");
  rawEl.textContent = rawText || "Senaryo metni bulunamadı.";

  if (!scenes || scenes.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <span class="empty-icon">📝</span>
        <p>Ayrıştırılmış sahne JSON verisi bulunamadı.</p>
      </div>`;
    return;
  }

  container.innerHTML = "";
  scenes.forEach(sc => {
    const card = document.createElement("div");
    card.className = "scene-card";
    card.innerHTML = `
      <div class="scene-card-header">
        <span class="scene-badge">SAHNE ${sc.index}</span>
        <span class="scene-title">${escapeHtml(sc.title || "Sahne")}</span>
      </div>
      <div class="scene-detail-row">
        <span class="scene-field-label">Açıklama:</span>
        <div class="scene-field-val">${escapeHtml(sc.description || "")}</div>
      </div>
      <div class="scene-detail-row">
        <span class="scene-field-label">Görsel Prompt'u (Gemini 9:16 Çizim):</span>
        <div class="scene-field-val">${escapeHtml(sc.image_prompt || "")}</div>
      </div>
      <div class="scene-detail-row">
        <span class="scene-field-label">Video Prompt'u (Gemini Kamera Hareketi):</span>
        <div class="scene-field-val">${escapeHtml(sc.video_prompt || "")}</div>
      </div>
      ${sc.voiceover_text ? `
      <div class="scene-detail-row">
        <span class="scene-field-label">Seslendirme / Diyalog:</span>
        <div class="scene-field-val">${escapeHtml(sc.voiceover_text)}</div>
      </div>` : ""}
    `;
    container.appendChild(card);
  });
}

// ---------------------------------------------------------------------------
// 8. Medya Galerisi (Görseller ve Videolar) Render
// ---------------------------------------------------------------------------
function renderMediaGallery(images, videos, folderName) {
  const grid = document.getElementById("mediaGrid");
  const countBadge = document.getElementById("galleryCount");
  const folderTitle = document.getElementById("galleryFolderTitle");
  const showcase = document.getElementById("videoShowcase");
  const videoPlayer = document.getElementById("mainVideoPlayer");

  folderTitle.textContent = folderName ? `Çıktı Klasörü: ${folderName}` : "Aktif Üretim Çıktıları";
  const totalCount = (images?.length || 0) + (videos?.length || 0);
  countBadge.textContent = totalCount;

  // Tam video var mı?
  const fullVideo = videos?.find(v => v.is_full);
  if (fullVideo) {
    showcase.classList.remove("hidden");
    videoPlayer.src = fullVideo.url;
  } else {
    showcase.classList.add("hidden");
    videoPlayer.removeAttribute("src");
  }

  if (totalCount === 0) {
    grid.innerHTML = `
      <div class="empty-state">
        <span class="empty-icon">🖼️</span>
        <p>Bu klasörde henüz görsel veya video üretilmemiş.</p>
      </div>`;
    return;
  }

  grid.innerHTML = "";

  // Görselleri Listele
  (images || []).forEach(img => {
    const card = document.createElement("div");
    card.className = "media-item-card";
    card.innerHTML = `
      <div class="media-thumb-wrap">
        <span class="media-badge">Görsel 9:16</span>
        <img src="${img.url}" class="media-thumb-img" alt="${img.name}" loading="lazy">
      </div>
      <div class="media-info">
        <span class="media-name">${img.name}</span>
        <span class="media-size">${img.size_kb} KB</span>
      </div>
    `;
    grid.appendChild(card);
  });

  // Videoları Listele
  (videos || []).forEach(vid => {
    const card = document.createElement("div");
    card.className = "media-item-card";
    card.innerHTML = `
      <div class="media-thumb-wrap">
        <span class="media-badge">${vid.is_full ? "🎬 TAM VİDEO" : "Sahne Videosu"}</span>
        <video src="${vid.url}" controls class="media-thumb-video"></video>
      </div>
      <div class="media-info">
        <span class="media-name">${vid.name}</span>
        <span class="media-size">${vid.size_mb} MB</span>
      </div>
    `;
    grid.appendChild(card);
  });
}

// ---------------------------------------------------------------------------
// 9. Çıktı Klasörleri Listesi ve Detay Yükleme
// ---------------------------------------------------------------------------
async function loadOutputsList() {
  try {
    const res = await fetch("/api/outputs");
    const folders = await res.json();
    const container = document.getElementById("outputsList");

    if (!folders || folders.length === 0) {
      container.innerHTML = `<div class="empty-state"><p>Henüz çıktı klasörü yok.</p></div>`;
      return;
    }

    container.innerHTML = "";
    folders.forEach(f => {
      const row = document.createElement("div");
      row.className = "output-row";
      row.innerHTML = `
        <div class="output-name">
          <span>📁</span>
          <strong>${escapeHtml(f.name)}</strong>
        </div>
        <div class="output-date">${f.modified}</div>
        <div class="output-imgs">${f.images_count} adet</div>
        <div class="output-vids">${f.videos_count} adet</div>
        <div class="output-actions">
          <button type="button" class="btn btn-sm btn-sub" onclick="loadOutputDetail('${escapeHtml(f.name)}')">İncele</button>
        </div>
      `;
      container.appendChild(row);
    });

    // İlk klasörü varsayılan olarak yükle
    if (!currentActiveFolder && folders.length > 0) {
      loadOutputDetail(folders[0].name);
    }
  } catch (err) {
    console.error("Outputs list fetch error:", err);
  }
}

async function loadOutputDetail(folderName) {
  currentActiveFolder = folderName;
  try {
    const res = await fetch(`/api/output-detail?folder=${encodeURIComponent(folderName)}`);
    const data = await res.json();
    if (data.error) return;

    // Senaryo kartlarını güncelle
    renderSceneCards(data.scenes, data.scenario_text);

    // Medya galerisini güncelle
    renderMediaGallery(data.images, data.videos, folderName);
  } catch (err) {
    console.error("Output detail fetch error:", err);
  }
}

// ---------------------------------------------------------------------------
// 10. macOS Finder'da Klasör Aç
// ---------------------------------------------------------------------------
async function openCurrentFolderInFinder() {
  if (!currentActiveFolder) {
    alert("Henüz seçili bir çıktı klasörü yok.");
    return;
  }
  try {
    await fetch("/api/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: `outputs/${currentActiveFolder}` }),
    });
  } catch (err) {
    console.error("Open folder error:", err);
  }
}
