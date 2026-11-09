const $ = (id) => document.getElementById(id);
let profiles = [];
let previewToken = null;
let scanTimer = null;
let saveTimer = null;
let hydrating = false;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function say(text, isError) {
  const node = $("msg");
  node.hidden = !text;
  node.textContent = text || "";
  node.className = isError ? "msg error" : "msg";
}

async function api(path, options) {
  const response = await fetch(path, options);
  let data = {};
  try {
    data = await response.json();
  } catch (err) {
    throw new Error("пустой ответ сервера");
  }
  if (!response.ok || data.ok === false) throw new Error(data.error || response.statusText);
  return data;
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function checkedValues(root) {
  return [...root.querySelectorAll("input:checked")].map((node) => node.value);
}

function collect() {
  return {
    device: $("device").value,
    baudrate: Number($("baud").value),
    parity: $("parity").value,
    stopbits: Number($("stopbits").value),
    bytesize: Number($("bytesize").value),
    timeout: Number($("timeout").value),
    inter_frame_delay: Number($("gap").value),
    echo_guard: Number($("echo").value),
    retries: Number($("retries").value),
    hub_name: $("hub").value.trim(),
    ha_not_using_port: $("haUnused").checked,
    scan: {
      address_from: Number($("scanFrom").value),
      address_to: Number($("scanTo").value),
      bauds: checkedValues($("scanBauds")).map(Number),
      parities: checkedValues($("scanParities")),
      probes: checkedValues($("scanProbes")).map(Number),
    },
  };
}

function paintStatus(data) {
  const port = data.port || {};
  const hub = data.hub || {};
  $("portState").textContent = port.exists ? "порт на месте" : "порт не найден";
  $("portState").className = port.exists ? "pill ok" : "pill bad";
  $("hubState").textContent = hub.label || "хаб";
  $("hubState").className = hub.mode === "blocked" ? "pill bad" : "pill ok";
  if (data.gap != null && data.settings) {
    const gap = data.settings.inter_frame_delay;
    $("gapInfo").textContent = gap > 0
      ? `Пауза кадра: ${gap} с. Повторы без ответа: ${data.settings.retries}.`
      : `Пауза кадра автоматически ${Number(data.gap).toFixed(4)} с. Повторы: ${data.settings.retries}.`;
  }
}

function fillPorts(ports, selected) {
  const paths = [];
  const push = (path) => {
    if (path && !paths.includes(path)) paths.push(path);
  };
  for (const item of ports || []) {
    if (item.path.includes("/serial/by-id/") || item.path.includes("/ttyUSB")) push(item.path);
  }
  for (const item of ports || []) push(item.path);
  push(selected);
  $("device").innerHTML = paths.map((path) => `<option value="${esc(path)}">${esc(path)}</option>`).join("");
  if (selected) $("device").value = selected;
}

function setChecks(root, values) {
  const wanted = new Set((values || []).map(String));
  root.querySelectorAll("input").forEach((node) => {
    node.checked = wanted.has(node.value);
  });
}

function fillSettings(state) {
  hydrating = true;
  const settings = state.settings;
  const scan = settings.scan || {};
  $("baud").innerHTML = state.bauds.map((baud) => `<option value="${baud}">${baud}</option>`).join("");
  $("baud").value = String(settings.baudrate);
  $("parity").value = settings.parity;
  $("stopbits").value = String(settings.stopbits);
  $("bytesize").value = String(settings.bytesize || 8);
  $("timeout").value = settings.timeout;
  $("gap").value = settings.inter_frame_delay;
  $("echo").value = settings.echo_guard;
  $("retries").value = settings.retries || 0;
  $("hub").value = settings.hub_name || "";
  $("yamlHub").value = settings.hub_name || "";
  $("haUnused").checked = !!settings.ha_not_using_port;
  fillPorts(state.ports, settings.device);
  if (!$("scanBauds").childElementCount) {
    $("scanBauds").innerHTML = state.bauds.map((baud) => (
      `<label class="check"><input type="checkbox" value="${baud}">${baud}</label>`
    )).join("");
  }
  $("scanFrom").value = scan.address_from || 1;
  $("scanTo").value = scan.address_to || 255;
  setChecks($("scanBauds"), scan.bauds || [settings.baudrate]);
  setChecks($("scanParities"), scan.parities || [settings.parity]);
  setChecks($("scanProbes"), scan.probes || [3, 1, 2]);
  paintStatus(state);
  $("version").textContent = `версия ${state.version}`;
  hydrating = false;
}

function scheduleSave() {
  if (hydrating) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      paintStatus(await post("api/settings", collect()));
    } catch (err) {
      say(err.message, true);
    }
  }, 200);
}

function renderExchange(target, data) {
  if (!data) {
    target.innerHTML = "";
    return;
  }
  const parsed = data.parsed || {};
  let table = "";
  if (parsed.exception) {
    table = `<p class="warn">Исключение ${esc(parsed.exception_code)}: ${esc(parsed.exception_text)}.</p>`;
  } else if (parsed.views) {
    const rows = parsed.views.map((view, index) => (
      `<tr><td>${index}</td><td>${view.dec}</td><td>${esc(view.hex)}</td><td>${view.signed}</td><td class="mono">${esc(view.bin)}</td></tr>`
    )).join("");
    table = `<table><thead><tr><th>#</th><th>DEC</th><th>HEX</th><th>SIGNED</th><th>BIN</th></tr></thead><tbody>${rows}</tbody></table>`;
  } else if (parsed.bits) {
    const rows = parsed.bits.map((bit, index) => `<tr><td>${index}</td><td>${bit ? "1" : "0"}</td></tr>`).join("");
    table = `<table><thead><tr><th>Бит</th><th>Значение</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  const decoded = (data.decoded || []).map((row) => `<li>${esc(row.name)}: ${esc(row.text)}</li>`).join("");
  target.innerHTML = `
    <div class="out">
      <div>TX <code>${esc(data.tx)}</code></div>
      <div>RX <code>${esc(data.rx || "—")}</code></div>
      <div class="muted">${esc(data.note || "")}${data.echo ? " Эхо запроса снято." : ""}</div>
      ${decoded ? `<ul>${decoded}</ul>` : ""}
      <div class="table-wrap">${table}</div>
    </div>`;
}

function showPreview(preview) {
  previewToken = preview.token;
  $("confirm").hidden = false;
  $("confirmText").textContent = preview.summary;
  $("confirmHex").textContent = preview.hex;
}

function clearPreview() {
  previewToken = null;
  $("confirm").hidden = true;
}

function renderScan(scan) {
  const total = scan.total || 0;
  const done = scan.done || 0;
  $("scanBar").style.width = total ? `${Math.round(100 * done / total)}%` : "0";
  const cur = scan.current ? `сейчас ${scan.current.address} @ ${scan.current.baudrate} ${scan.current.parity || ""}` : "";
  $("scanMeta").textContent = `${done} / ${total} ${cur} ${scan.error || ""}`.trim();
  $("scanBody").innerHTML = (scan.found || []).map((row) => {
    const how = row.exception
      ? `FC${String(row.fc).padStart(2, "0")} исключение ${row.exception_code}`
      : `FC${String(row.fc).padStart(2, "0")}`;
    return `<tr>
      <td>${row.address}</td><td>${row.baudrate}</td><td>${esc(row.parity || "")}</td><td>${esc(how)}</td>
      <td><button type="button" data-use="${row.address}" data-baud="${row.baudrate}" data-parity="${esc(row.parity || "")}">В формы</button></td>
    </tr>`;
  }).join("");
}

async function pollScan() {
  try {
    const data = await api("api/scan");
    renderScan(data.scan);
    if (!data.scan.running) {
      clearInterval(scanTimer);
      scanTimer = null;
    }
  } catch (err) {
    say(err.message, true);
  }
}

function renderProfiles() {
  $("yamlProfile").innerHTML = profiles.map((profile) => `<option value="${esc(profile.id)}">${esc(profile.name)}</option>`).join("");
  $("profileList").innerHTML = profiles.map((profile) => {
    const notes = (profile.notes || []).map((line) => `<li>${esc(line)}</li>`).join("");
    const coils = (profile.points || []).filter((point) => point.kind === "coil").map((point) => (
      `<span class="row">${esc(point.name)}
        <button type="button" data-coil="${profile.id}" data-addr="${point.address}" data-on="1">Вкл</button>
        <button type="button" data-coil="${profile.id}" data-addr="${point.address}" data-on="0">Выкл</button>
      </span>`
    )).join("");
    const actions = (profile.actions || []).map((action) => {
      if (action.op === "read" || action.op === "raw") {
        return `<button type="button" data-run="${esc(profile.id)}" data-action="${esc(action.id)}">${esc(action.label)}</button>`;
      }
      let field = "";
      if (action.input === "enum") {
        const options = Object.entries(action.enum || {}).map(([key, label]) => (
          `<option value="${esc(key)}">${esc(label)}</option>`
        )).join("");
        field = `<select data-for="${esc(action.id)}">${options}</select>`;
      } else {
        const min = action.min ?? 0;
        const max = action.max ?? 65535;
        field = `<input data-for="${esc(action.id)}" type="number" min="${min}" max="${max}" value="${min}">`;
      }
      return `<div class="row"><span>${esc(action.label)}</span>${field}
        <button type="button" data-prep="${esc(profile.id)}" data-action="${esc(action.id)}">Подготовить</button></div>`;
    }).join("");
    const defaults = profile.defaults || {};
    return `<article class="card" data-profile="${esc(profile.id)}">
      <h3>${esc(profile.name)}</h3>
      <p class="muted">${esc(profile.subtitle || "")}</p>
      <ul>${notes}</ul>
      <div class="grid">
        <label>Адрес на шине<input data-slave type="number" min="0" max="255" value="${defaults.address ?? 1}"></label>
      </div>
      <div class="row">
        <button type="button" data-apply="${esc(profile.id)}">Подставить скорость профиля</button>
      </div>
      <div class="row">${actions}</div>
      ${coils}
      <div class="out" data-out></div>
    </article>`;
  }).join("");
}

function profileSlave(card) {
  return Number(card.querySelector("[data-slave]").value);
}

function actionValue(card, actionId) {
  const field = card.querySelector(`[data-for="${actionId}"]`);
  return field ? field.value : null;
}

function handleResult(target, data) {
  if (data.preview) {
    showPreview(data.preview);
    say("Проверьте кадр и подтвердите запись.");
    return;
  }
  renderExchange(target, data);
}

async function refreshLog() {
  const data = await api("api/log");
  $("logBody").innerHTML = (data.log || []).map((row) => (
    `<tr><td>${esc(row.ts)}</td><td>${esc(row.dir)}</td><td class="mono">${esc(row.hex)}</td><td>${esc(row.note || "")}</td></tr>`
  )).join("");
}

function writeFields() {
  const fc = $("writeFc").value;
  const box = $("writeFields");
  if (fc === "5") {
    box.innerHTML = `<label>Состояние<select id="writeOn"><option value="1">ВКЛ — 0xFF00</option><option value="0">ВЫКЛ — 0x0000</option></select></label>`;
  } else if (fc === "6") {
    box.innerHTML = `<label>Значение 0…65535<input id="writeValue" type="number" min="0" max="65535" value="0"></label>`;
  } else if (fc === "15") {
    box.innerHTML = `<label>Катушки через запятую, 1 или 0<input id="writeList" value="1,0,0,0"></label>`;
  } else {
    box.innerHTML = `<label>Значения регистров через запятую<input id="writeList" value="1"></label>`;
  }
}

function writeBody() {
  const fc = Number($("writeFc").value);
  const body = { fc, slave: Number($("writeSlave").value), address: Number($("writeAddr").value) };
  if (fc === 5) body.on = $("writeOn").value === "1";
  else if (fc === 6) body.value = Number($("writeValue").value);
  else if (fc === 15) body.coils = $("writeList").value.split(",").map((item) => Number(item.trim())).filter((item) => !Number.isNaN(item));
  else body.values = $("writeList").value.split(",").map((item) => Number(item.trim())).filter((item) => !Number.isNaN(item));
  return body;
}

document.querySelector("nav").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (!button) return;
  document.querySelectorAll("nav button").forEach((node) => node.classList.toggle("active", node === button));
  document.querySelectorAll("section.panel").forEach((node) => {
    if (node.closest(".top") || node.id === "scanLive") return;
    node.hidden = node.id !== button.dataset.tab;
  });
  if (button.dataset.tab === "log") refreshLog().catch((err) => say(err.message, true));
});

$("conn").addEventListener("change", scheduleSave);
$("conn").addEventListener("input", scheduleSave);
$("scanBox").addEventListener("change", scheduleSave);
$("scanBox").addEventListener("input", scheduleSave);

$("btnPorts").addEventListener("click", async () => {
  try {
    const state = await api("api/state");
    fillPorts(state.ports, $("device").value);
    paintStatus(state);
  } catch (err) {
    say(err.message, true);
  }
});

$("btnRelease").addEventListener("click", async () => {
  try {
    await post("api/hub/release");
    paintStatus(await api("api/state"));
    say("Хаб остановлен. Пока не нажмёте «Вернуть хаб», stop/reload на каждую операцию не вызываются.");
  } catch (err) {
    say(err.message, true);
  }
});

$("btnRestore").addEventListener("click", async () => {
  try {
    await post("api/hub/restore");
    paintStatus(await api("api/state"));
    say("Хаб перечитан через modbus.reload.");
  } catch (err) {
    say(err.message, true);
  }
});

$("scanFull").addEventListener("click", () => {
  $("scanFrom").value = 1;
  $("scanTo").value = 255;
  scheduleSave();
});

$("scanStart").addEventListener("click", async () => {
  try {
    say("");
    const body = collect();
    if (!body.scan.bauds.length || !body.scan.parities.length || !body.scan.probes.length) {
      throw new Error("отметьте скорость, чётность и функцию опроса");
    }
    await post("api/settings", body);
    const data = await post("api/scan/start", body.scan);
    renderScan(data.scan);
    if (scanTimer) clearInterval(scanTimer);
    scanTimer = setInterval(pollScan, 400);
  } catch (err) {
    say(err.message, true);
  }
});

$("scanStop").addEventListener("click", () => post("api/scan/stop").then((data) => renderScan(data.scan)).catch((err) => say(err.message, true)));

$("scanBody").addEventListener("click", (event) => {
  const button = event.target.closest("[data-use]");
  if (!button) return;
  for (const id of ["readSlave", "writeSlave", "yamlAddr"]) $(id).value = button.dataset.use;
  document.querySelectorAll("[data-slave]").forEach((node) => { node.value = button.dataset.use; });
  if (button.dataset.baud) $("baud").value = button.dataset.baud;
  if (button.dataset.parity) $("parity").value = button.dataset.parity;
  scheduleSave();
  say(`Адрес ${button.dataset.use} подставлен в формы.`);
});

$("readFc").addEventListener("change", () => {
  $("readQty").max = Number($("readFc").value) <= 2 ? 2000 : 125;
});

$("readGo").addEventListener("click", async () => {
  try {
    say("");
    const data = await post("api/read", {
      slave: Number($("readSlave").value),
      fc: Number($("readFc").value),
      address: Number($("readAddr").value),
      quantity: Number($("readQty").value),
    });
    renderExchange($("readOut"), data);
  } catch (err) {
    say(err.message, true);
  }
});

$("writeFc").addEventListener("change", () => {
  writeFields();
  clearPreview();
});

$("writePrep").addEventListener("click", async () => {
  try {
    const data = await post("api/write/preview", writeBody());
    showPreview(data.preview);
    say("Проверьте кадр и подтвердите запись.");
  } catch (err) {
    say(err.message, true);
  }
});

$("hexGo").addEventListener("click", async () => {
  try {
    say("");
    handleResult($("hexOut"), await post("api/raw", { hex: $("hexIn").value, append_crc: $("hexCrc").checked }));
  } catch (err) {
    say(err.message, true);
  }
});

$("logRefresh").addEventListener("click", () => refreshLog().catch((err) => say(err.message, true)));

$("yamlGo").addEventListener("click", async () => {
  try {
    const data = await post("api/yaml", {
      profile_id: $("yamlProfile").value,
      address: Number($("yamlAddr").value),
      hub: $("yamlHub").value.trim(),
      full_hub: $("yamlFull").checked,
      channels: Number($("yamlChannels").value),
    });
    $("yamlOut").value = data.yaml;
  } catch (err) {
    say(err.message, true);
  }
});

$("yamlCopy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("yamlOut").value);
    say("Фрагмент скопирован.");
  } catch (err) {
    $("yamlOut").focus();
    $("yamlOut").select();
    say("Выделите текст и скопируйте вручную.");
  }
});

$("confirmNo").addEventListener("click", clearPreview);

$("confirmGo").addEventListener("click", async () => {
  if (!previewToken) return;
  try {
    const data = await post("api/write/commit", { token: previewToken });
    clearPreview();
    renderExchange($("writeOut"), data);
    say(data.note || "Запись отправлена.");
  } catch (err) {
    say(err.message, true);
  }
});

$("profileList").addEventListener("click", async (event) => {
  const card = event.target.closest("[data-profile]");
  if (!card) return;
  const profile = profiles.find((item) => item.id === card.dataset.profile);
  const out = card.querySelector("[data-out]");
  try {
    say("");
    if (event.target.closest("[data-apply]")) {
      const defaults = profile.defaults || {};
      $("baud").value = String(defaults.baudrate || $("baud").value);
      $("parity").value = defaults.parity || "N";
      $("stopbits").value = String(defaults.stopbits || 1);
      scheduleSave();
      say(`Порт: ${defaults.baudrate} ${defaults.parity || "N"}${defaults.stopbits || 1}. В устройство это не записывается.`);
      return;
    }
    const run = event.target.closest("[data-run]");
    if (run) {
      handleResult(out, await post("api/profile/run", {
        profile_id: profile.id,
        action: run.dataset.action,
        slave: profileSlave(card),
      }));
      return;
    }
    const prep = event.target.closest("[data-prep]");
    if (prep) {
      handleResult(out, await post("api/profile/run", {
        profile_id: profile.id,
        action: prep.dataset.action,
        slave: profileSlave(card),
        value: Number(actionValue(card, prep.dataset.action)),
      }));
      return;
    }
    const coil = event.target.closest("[data-coil]");
    if (coil) {
      handleResult(out, await post("api/write/preview", {
        fc: 5,
        slave: profileSlave(card),
        address: Number(coil.dataset.addr),
        on: coil.dataset.on === "1",
        profile_id: profile.id,
      }));
    }
  } catch (err) {
    say(err.message, true);
  }
});

document.body.addEventListener("input", (event) => {
  if (!previewToken || event.target.closest("#confirm") || event.target.closest("#conn") || event.target.closest("#scanBox")) return;
  clearPreview();
});

writeFields();
api("api/state").then((state) => {
  fillSettings(state);
  if (state.scan) renderScan(state.scan);
}).catch((err) => say(err.message, true));
api("api/profiles").then((data) => {
  profiles = data.profiles || [];
  renderProfiles();
}).catch((err) => say(err.message, true));
