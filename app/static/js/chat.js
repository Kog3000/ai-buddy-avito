/* Чат с AI-buddy: неблокирующая отправка, опрос статуса задачи, карточки действий. */
(function () {
  const log = document.getElementById("chat-log");
  const form = document.getElementById("composer");
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("send-btn");
  const fileInput = document.getElementById("chat-file");
  const chip = document.getElementById("attach-chip");
  const MAX_FILE = 5 * 1024 * 1024;
  let file = null;
  const { escapeHtml: esc, md } = window.api;
  let lastId = 0;
  let pending = false;
  let messages = [];

  const scroll = () => { log.scrollTop = log.scrollHeight; };

  function sourcesHtml(sources) {
    if (!sources || !sources.length) return "";
    return `<div class="sources">Источники: ${sources.map((s) =>
      `<a href="${esc(s.url || "/kb/topic/" + s.slug)}">${esc(s.title)}</a>`).join("")}</div>`;
  }

  function fieldHtml(f) {
    const id = "f_" + Math.random().toString(36).slice(2);
    if (f.type === "select") {
      return `<div class="field"><label for="${id}">${esc(f.label)}</label><select id="${id}" name="${esc(f.name)}">${
        f.options.map(([v, l]) => `<option value="${esc(v)}" ${v === f.value ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>`;
    }
    if (f.type === "textarea") {
      return `<div class="field"><label for="${id}">${esc(f.label)}</label><textarea id="${id}" name="${esc(f.name)}">${esc(f.value)}</textarea></div>`;
    }
    if (f.type === "checkbox") {
      return `<label class="check-inline"><input type="checkbox" name="${esc(f.name)}" ${f.value ? "checked" : ""}> ${esc(f.label)}</label>`;
    }
    return `<div class="field"><label for="${id}">${esc(f.label)}</label><input type="text" id="${id}" name="${esc(f.name)}" value="${esc(f.value)}"></div>`;
  }

  function rowsHtml(rows) {
    if (!rows || !rows.length) return "";
    return `<table>${rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}</table>`;
  }

  function cardHtml(card, live) {
    const dis = live ? "" : "disabled";
    if (card.type === "confirm") {
      return `<form class="acard ${live ? "" : "done"}" data-card="confirm">
        <h4>${esc(card.title)}</h4>${rowsHtml(card.rows)}
        ${(card.fields || []).map(fieldHtml).join("")}
        <div class="actions">${(card.actions || []).map((a, i) =>
          `<button type="button" class="btn sm ${a.style === "primary" ? "primary" : "secondary"}" data-i="${i}" ${dis}>${esc(a.label)}</button>`).join("")}</div>
        ${card.note ? `<div class="card-note">${esc(card.note)}</div>` : ""}</form>`;
    }
    if (card.type === "slots") {
      return `<div class="acard ${live ? "" : "done"}" data-card="slots"><h4>${esc(card.title)}</h4>
        <div class="slot-grid">${card.slots.map((s, i) => `<button type="button" class="slot" data-i="${i}" ${dis}>${esc(s.label)}</button>`).join("")}</div></div>`;
    }
    if (card.type === "steps") {
      return `<div class="acard"><h4>${esc(card.title)}</h4><div class="steps-list">${card.items.map((s, i) => `
        <div class="item"><span class="w">${esc(s.period || "Этап " + s.week)}</span><span class="t">${esc(s.title)}</span>
          ${s.prompt ? `<button type="button" class="btn xs secondary" data-ask="${i}">Помочь</button>` : ""}
          ${s.link ? `<a class="btn xs light" href="${esc(s.link)}">Материал</a>` : ""}
          ${!s.auto ? `<button type="button" class="btn xs light" data-done="${i}">Готово</button>` : ""}
        </div>`).join("")}</div></div>`;
    }
    if (card.type === "request_status") {
      return `<div class="acard"><h4><span>${esc(card.title)}</span><span class="chip ${esc(card.status)}">${esc(card.status_label)}</span></h4>
        ${rowsHtml(card.rows)}${card.link ? `<a href="${esc(card.link)}" class="card-link">Открыть →</a>` : ""}</div>`;
    }
    return "";
  }

  function bindCard(el, card) {
    if (card.type === "confirm") {
      el.querySelectorAll("button[data-i]").forEach((btn) => btn.addEventListener("click", () => {
        const a = card.actions[+btn.dataset.i];
        const params = { ...(a.params || {}) };
        el.querySelectorAll("[name]").forEach((f) => { params[f.name] = f.type === "checkbox" ? f.checked : f.value; });
        disable(el);
        send({ action: a.action, params, label: a.label });
      }));
    } else if (card.type === "slots") {
      el.querySelectorAll("button[data-i]").forEach((btn) => btn.addEventListener("click", () => {
        const s = card.slots[+btn.dataset.i];
        btn.classList.add("selected"); disable(el);
        send({ action: card.action, params: { start: s.start }, label: s.label });
      }));
    } else if (card.type === "steps") {
      el.querySelectorAll("[data-ask]").forEach((b) => b.addEventListener("click", () => send({ text: card.items[+b.dataset.ask].prompt })));
      el.querySelectorAll("[data-done]").forEach((b) => b.addEventListener("click", () => {
        const s = card.items[+b.dataset.done]; b.disabled = true;
        send({ action: "task_done", params: { task_id: s.task_id }, label: `Готово: ${s.title}` });
      }));
    }
  }

  function attachmentsHtml(list) {
    if (!list || !list.length) return "";
    return `<div class="attachments">${list.map((a) => `<a class="att" href="${esc(a.url)}" target="_blank" rel="noopener">${
      a.image ? `<img src="${esc(a.url)}" alt="${esc(a.name)}">` : `<span class="att-ico">📎</span>`}<span>${esc(a.name)}<small>${esc(a.size)}</small></span></a>`).join("")}</div>`;
  }

  function setFile(f) {
    file = f || null;
    chip.hidden = !file;
    if (file) {
      document.getElementById("attach-name").textContent = file.name;
      document.getElementById("attach-size").textContent = ` · ${Math.max(1, Math.round(file.size / 1024))} КБ`;
    }
    if (!file) fileInput.value = "";
  }
  fileInput.addEventListener("change", () => {
    const f = fileInput.files[0];
    if (f && f.size > MAX_FILE) { api.toast("Файл больше 5 МБ", true); setFile(null); return; }
    setFile(f);
  });
  document.getElementById("attach-clear").addEventListener("click", () => setFile(null));

  function disable(el) { el.classList.add("done"); el.querySelectorAll("button, input, select, textarea").forEach((x) => { x.disabled = true; }); }

  function renderMessage(m, isLast) {
    const row = document.createElement("div");
    row.className = "msg " + (m.role === "user" ? "user" : "bot");
    row.dataset.id = m.id;
    if (m.role === "user") {
      row.innerHTML = `<div class="bubble">${esc(m.text)}${attachmentsHtml(m.attachments)}<span class="time">${esc(m.time)}</span></div>`;
    } else {
      const fb = m.id ? `<div class="fb"><button type="button" data-v="1" class="${m.feedback === 1 ? "on" : ""}" title="Полезно">👍</button><button type="button" data-v="-1" class="${m.feedback === -1 ? "on" : ""}" title="Не помогло">👎</button></div>` : "";
      const sources = m.sources && m.sources.length ? m.sources : (m.meta && m.meta.sources) || [];
      row.innerHTML = `<span class="bot-ava"><img src="${window.BOT_AVATAR}" alt=""></span>
        <div class="bubble">${md(m.text)}${sourcesHtml(sources)}<div class="cards"></div><div class="foot">${fb}<span class="time">${esc(m.time)}</span></div></div>`;
      const holder = row.querySelector(".cards");
      (m.cards || []).forEach((card) => {
        const wrap = document.createElement("div");
        wrap.innerHTML = cardHtml(card, isLast);
        const el = wrap.firstElementChild;
        if (!el) return;
        holder.appendChild(el);
        if (isLast || card.type === "steps") bindCard(el, card);
      });
      row.querySelectorAll(".fb button").forEach((b) => b.addEventListener("click", async () => {
        try {
          await api.post(`/api/chat/messages/${m.id}/feedback`, { value: +b.dataset.v });
          row.querySelectorAll(".fb button").forEach((x) => x.classList.toggle("on", x === b));
          api.toast("Спасибо за оценку!");
        } catch (e) { api.toast(e.message, true); }
      }));
    }
    log.appendChild(row);
    return row;
  }

  function renderSuggestions(list) {
    log.querySelectorAll(".suggestions").forEach((s) => s.remove());
    if (!list || !list.length) return;
    const box = document.createElement("div");
    box.className = "suggestions";
    list.forEach((text) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = text;
      b.addEventListener("click", () => send({ text }));
      box.appendChild(b);
    });
    log.appendChild(box);
  }

  function addMessages(list) {
    list.forEach((m) => {
      if (messages.some((x) => x.id === m.id)) return;
      messages.push(m);
      lastId = Math.max(lastId, m.id || 0);
    });
    const errors = Array.from(log.querySelectorAll(".msg.error"));
    log.innerHTML = "";
    // карточки активны, пока пользователь не написал следующее сообщение
    // (проактивные уведомления AI-buddy их не «гасят»)
    let lastUser = -1;
    messages.forEach((m, i) => { if (m.role === "user") lastUser = i; });
    messages.forEach((m, i) => renderMessage(m, i > lastUser && m.role === "assistant"));
    const last = messages[messages.length - 1];
    if (last && last.role === "assistant") {
      const withSuggestions = [...messages].reverse().find((m, j) => messages.length - 1 - j > lastUser && m.suggestions && m.suggestions.length);
      renderSuggestions((withSuggestions || last).suggestions);
    }
    errors.forEach((row) => log.appendChild(row));
    scroll();
  }

  function showTyping() {
    const row = document.createElement("div");
    row.className = "msg bot"; row.id = "typing";
    row.innerHTML = `<span class="bot-ava"><img src="${window.BOT_AVATAR}" alt=""></span><div class="bubble"><span class="typing"><i></i><i></i><i></i></span><div class="status-line" id="typing-status">AI-buddy думает…</div></div>`;
    log.appendChild(row); scroll();
    return row;
  }

  function showError(payload, text) {
    const row = document.createElement("div");
    row.className = "msg bot error";
    row.innerHTML = `<span class="bot-ava"><img src="${window.BOT_AVATAR}" alt=""></span><div class="bubble"><div class="error-line">${esc(text)}<button type="button" class="btn xs primary">Повторить</button></div></div>`;
    row.querySelector("button").addEventListener("click", () => { row.remove(); send(payload, true); });
    log.appendChild(row); scroll();
  }

  async function poll(jobId) {
    const started = Date.now();
    while (Date.now() - started < 60000) {
      await new Promise((r) => setTimeout(r, 450));
      const job = await api.get(`/api/chat/jobs/${jobId}`);
      if (job.status === "done") return job.message;
      if (job.status === "error") throw new Error("Не удалось получить ответ");
      const st = document.getElementById("typing-status");
      if (st && Date.now() - started > 6000) st.textContent = "Обращаюсь к сервисам — это может занять до минуты…";
    }
    throw new Error("Ответ задерживается");
  }

  async function send(payload, isRetry) {
    if (pending) { api.toast("Дождитесь ответа на предыдущее сообщение"); return; }
    pending = true; sendBtn.classList.add("busy"); document.body.dataset.pending = "1";
    log.querySelectorAll(".suggestions").forEach((s) => s.remove());
    let typing;
    try {
      let body = payload;
      if (payload.file) {
        body = new FormData();
        body.append("text", payload.text || "");
        body.append("file", payload.file);
      }
      const res = await api.post("/api/chat/send", body);
      if (!isRetry || !messages.some((m) => m.id === res.user_message.id)) addMessages([res.user_message]);
      typing = showTyping();
      const msg = await poll(res.job_id);
      typing.remove();
      addMessages([msg]);
    } catch (e) {
      if (typing) typing.remove();
      showError(payload, e.message || "Ошибка");
    } finally {
      pending = false; sendBtn.classList.remove("busy"); document.body.dataset.pending = ""; input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text && !file) return;
    if (pending) { api.toast("AI-buddy ещё отвечает на предыдущее сообщение"); return; }
    input.value = "";
    const payload = file ? { text, file } : { text };
    setFile(null);
    send(payload);
  });

  async function refresh() {
    if (pending) return;
    try {
      const data = await api.get(`/api/chat/messages?after=${lastId}`);
      if (data.messages.length) addMessages(data.messages);
    } catch (e) { /* фоновое обновление — без тоста */ }
  }

  (async function init() {
    try {
      const data = await api.get("/api/chat/messages");
      addMessages(data.messages);
    } catch (e) { api.toast(e.message, true); }
    const q = new URLSearchParams(location.search).get("q");
    if (q) { history.replaceState(null, "", "/chat"); send({ text: q }); }
    setInterval(refresh, 4000);
    input.focus();
  })();
})();
