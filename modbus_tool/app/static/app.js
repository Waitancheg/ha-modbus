const $ = (id) => document.getElementById(id);
let profiles = [];
let previewToken = null;
let scanTimer = null;

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

function settingsPayload() {
  return {
    device: $("device").value.trim(),
    baudrate: Number($("baud").value),
    parity: $("parity").value,
    stopbits: Number($("stopbits").value),
    timeout: Number($("timeout").value),
    inter_frame_delay: Number($("gap").value),
    echo_guard: Number($("echo").value),
    hub_name: $("hub").value.trim(),
    auto_release_hub: $("auto").checked,
  };
}

function fillSettings(state) {
  const s = state.settings;
  $("device").value = s.device;
  $("baud").innerHTML = state.bauds.map((b) => `<option value="${b}">${b}</option>`).join("");
  $("baud").value = String(s.baudrate);
  $("parity").value = s.parity;
  $("stopbits").value = String(s.stopbits);
  $("timeout").value = s.timeout;
  $("gap").value = s.inter_frame_delay;
  $("echo").value = s.echo_guard;
  $("hub").value = s.hub_name || "";
  $("auto").checked = !!s.auto_release_hub;
  $("yamlHub").value = s.hub_name || "";
  $("gapInfo").textContent = s.inter_frame_delay > 0
    ? `Пауза кадра: ${s.inter_frame_delay} с.`
    : `Пауза кадра автоматически: ${Number(state.gap).toFixed(4)} с (не меньше 3.5 символа и 15 мс).`;
  const list = $("ports");
  const paths = new Set((state.ports || []).map((p) => p.path));
  paths.add(s.device);
  list.innerHTML = [...paths].map((p) => `<option value="${esc(p)}"></option>`).join("");
  const baudBoxes = $("scanBauds");
  if (!baudBoxes.childElementCount) {
    baudBoxes.innerHTML = state.bauds.map((b) => (
      `<label class="check"><input type="checkbox" value="${b}" ${b === s.baudrate ? "checked" : ""}>${b}</label>`
    )).join("");
  }
  const hub = state.hub || {};
  $("hubState").textContent = !s.hub_name
    ? "хаб не задан"
    : hub.released ? `хаб «${s.hub_name}» остановлен` : `хаб «${s.hub_name}»`;
  $("version").textContent = `версия ${state.version}`;
}

function renderExchange(target, data) {
  if (!data) {
    target.innerHTML = "";
    return;
  }
  const parsed = data.parsed || {};
  let table = "";
  if (parsed.exception) {
    table = `<p class="warn">Исключение ${esc(parsed.exception_code)}: ${esc(parsed.exception_text)}. Устройство на связи.</p>`;
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

function selectedBauds() {
  return [...$("scanBauds").querySelectorAll("input:checked")].map((node) => Number(node.value));
}

function renderScan(scan) {
  const total = scan.total || 0;
  const done = scan.done || 0;
  $("scanBar").style.width = total ? `${Math.round(100 * done / total)}%` : "0";
  const cur = scan.current ? `сейчас ${scan.current.address} @ ${scan.current.baudrate}` : "";
  $("scanMeta").textContent = `${done} / ${total} ${cur} ${scan.error || ""}`.trim();
  $("scanBody").innerHTML = (scan.found || []).map((row) => {
    const how = row.exception
      ? `FC${String(row.fc).padStart(2, "0")} исключение ${row.exception_code}`
      : `FC${String(row.fc).padStart(2, "0")}`;
    return `<tr>
      <td>${row.address}</td><td>${row.baudrate}</td><td>${esc(how)}</td>
      <td><button type="button" data-use="${row.address}" data-baud="${row.baudrate}">В формы</button></td>
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

function useAddress(address, baud) {
  for (const id of ["readSlave", "writeSlave", "yamlAddr"]) $(id).value = address;
  document.querySelectorAll("[data-slave]").forEach((node) => { node.value = address; });
  if (baud) $("baud").value = String(baud);
  say(`Адрес ${address} подставлен в формы. Скорость порта ${baud || "без изменений"} — сохраните её на вкладке «Порт», если меняли.`);
}

function renderProfiles() {
  const yaml = $("yamlProfile");
  yaml.innerHTML = profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  $("profileList").innerHTML = profiles.map((profile) => {
    const notes = (profile.notes || []).map((line) => `<li>${esc(line)}</li>`).join("");
    const coils = (profile.points || []).filter((p) => p.kind === "coil").map((p) => (
      `<span class="row">${esc(p.name)}
        <button type="button" data-coil="${profile.id}" data-addr="${p.address}" data-on="1">Вкл</button>
        <button type="button" data-coil="${profile.id}" data-addr="${p.address}" data-on="0">Выкл</button>
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
        <button type="button" data-apply="${esc(profile.id)}">Подставить скорость профиля в порт</button>
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
  const body = {
    fc,
    slave: Number($("writeSlave").value),
    address: Number($("writeAddr").value),
  };
  if (fc === 5) body.on = $("writeOn").value === "1";
  else if (fc === 6) body.value = Number($("writeValue").value);
  else if (fc === 15) body.coils = $("writeList").value.split(",").map((x) => Number(x.trim())).filter((x) => !Number.isNaN(x));
  else body.values = $("writeList").value.split(",").map((x) => Number(x.trim())).filter((x) => !Number.isNaN(x));
  return body;
}

document.querySelector("nav").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (!button) return;
  document.querySelectorAll("nav button").forEach((node) => node.classList.toggle("active", node === button));
  document.querySelectorAll("section.panel").forEach((node) => {
    node.hidden = node.id !== button.dataset.tab;
  });
  if (button.dataset.tab === "log") refreshLog().catch((err) => say(err.message, true));
});

$("savePort").addEventListener("click", async () => {
  try {
    const data = await post("api/settings", settingsPayload());
    $("gapInfo").textContent = data.settings.inter_frame_delay > 0
      ? `Пауза кадра: ${data.settings.inter_frame_delay} с.`
      : `Пауза кадра: ${Number(data.gap).toFixed(4)} с.`;
    say("Настройки порта сохранены до перезапуска дополнения.");
  } catch (err) {
    say(err.message, true);
  }
});

$("btnRelease").addEventListener("click", async () => {
  try {
    await post("api/hub/release");
    say("Хаб остановлен, порт должен освободиться.");
    const state = await api("api/state");
    fillSettings(state);
  } catch (err) {
    say(err.message, true);
  }
});

$("btnRestore").addEventListener("click", async () => {
  try {
    const data = await post("api/hub/restore");
    const extra = data.result && data.result.fallback ? ` ${data.result.fallback}` : "";
    say(`Хаб возвращён.${extra}`);
    const state = await api("api/state");
    fillSettings(state);
  } catch (err) {
    say(err.message, true);
  }
});

$("scanFull").addEventListener("click", () => {
  $("scanFrom").value = 1;
  $("scanTo").value = 255;
});

$("scanStart").addEventListener("click", async () => {
  try {
    say("");
    const data = await post("api/scan/start", {
      address_from: Number($("scanFrom").value),
      address_to: Number($("scanTo").value),
      bauds: selectedBauds(),
    });
    renderScan(data.scan);
    if (scanTimer) clearInterval(scanTimer);
    scanTimer = setInterval(pollScan, 400);
  } catch (err) {
    say(err.message, true);
  }
});

$("scanStop").addEventListener("click", () => post("api/scan/stop").catch((err) => say(err.message, true)));

$("scanBody").addEventListener("click", (event) => {
  const button = event.target.closest("[data-use]");
  if (!button) return;
  useAddress(button.dataset.use, button.dataset.baud);
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
    const data = await post("api/raw", { hex: $("hexIn").value, append_crc: $("hexCrc").checked });
    renderExchange($("hexOut"), data);
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
    });
    $("yamlOut").value = data.yaml;
  } catch (err) {
    say(err.message, true);
  }
});

$("yamlCopy").addEventListener("click", async () => {
  const text = $("yamlOut").value;
  try {
    await navigator.clipboard.writeText(text);
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
      await post("api/settings", settingsPayload());
      say(`Порт переключён на ${defaults.baudrate} ${defaults.parity || "N"}${defaults.stopbits || 1}. В устройство скорость не записывалась.`);
      return;
    }
    const run = event.target.closest("[data-run]");
    if (run) {
      const data = await post("api/profile/run", {
        profile_id: profile.id,
        action: run.dataset.action,
        slave: profileSlave(card),
      });
      renderExchange(out, data);
      return;
    }
    const prep = event.target.closest("[data-prep]");
    if (prep) {
      const data = await post("api/profile/run", {
        profile_id: profile.id,
        action: prep.dataset.action,
        slave: profileSlave(card),
        value: Number(actionValue(card, prep.dataset.action)),
      });
      showPreview(data.preview);
      say("Проверьте кадр и подтвердите запись.");
      return;
    }
    const coil = event.target.closest("[data-coil]");
    if (coil) {
      const data = await post("api/write/preview", {
        fc: 5,
        slave: profileSlave(card),
        address: Number(coil.dataset.addr),
        on: coil.dataset.on === "1",
        profile_id: profile.id,
      });
      showPreview(data.preview);
      say("Проверьте кадр и подтвердите запись.");
    }
  } catch (err) {
    say(err.message, true);
  }
});

document.body.addEventListener("input", (event) => {
  if (!previewToken || event.target.closest("#confirm")) return;
  clearPreview();
});

writeFields();
api("api/state").then((state) => fillSettings(state)).catch((err) => say(err.message, true));
api("api/profiles").then((data) => {
  profiles = data.profiles || [];
  renderProfiles();
}).catch((err) => say(err.message, true));
