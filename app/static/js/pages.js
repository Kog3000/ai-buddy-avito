/* Интерактив страниц: чек-лист, база знаний, наставник, цели, согласования, флаги. */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const { toast, escapeHtml: esc } = window.api;
  const fail = (e) => toast(e.message || "Ошибка", true);

  // ---- общие: дропдауны, модалки, POST-кнопки
  $$("[data-toggle]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); $(b.dataset.toggle).classList.toggle("open"); }));
  document.addEventListener("click", () => $$(".dropdown.open").forEach((d) => d.classList.remove("open")));
  const openModal = (sel) => $(sel)?.classList.add("open");
  const closeModals = () => $$(".modal-bg.open").forEach((m) => m.classList.remove("open"));
  $$("[data-close]").forEach((b) => b.addEventListener("click", closeModals));
  $$(".modal-bg").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) closeModals(); }));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModals(); });
  $$("[data-api-post]").forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    try { await api.post(b.dataset.apiPost); if (b.hasAttribute("data-reload")) location.reload(); } catch (err) { fail(err); }
  }));

  // ---- уведомления: открытие колокольчика отмечает их прочитанными
  $("[data-notif-open]")?.addEventListener("click", async (e) => {
    const bell = e.currentTarget; const count = bell.querySelector(".count");
    if (!count) return;
    try { await api.post("/api/notifications/read"); count.remove(); } catch (err) { /* не критично */ }
  });
  // ---- мобильное меню
  $$("[data-drawer]").forEach((b) => b.addEventListener("click", () => document.body.classList.toggle("drawer-open")));

  // ---- аккордеон недель
  $$(".week-head").forEach((h) => h.addEventListener("click", () => {
    const w = h.closest(".week"); w.classList.toggle("open");
    h.setAttribute("aria-expanded", w.classList.contains("open"));
  }));

  // ---- чек-лист: отметка задач (оптимистично, с откатом при ошибке)
  $$(".task[data-task] .cbox").forEach((box) => box.addEventListener("click", async () => {
    const task = box.closest(".task");
    const done = !task.classList.contains("done");
    task.classList.toggle("done", done);
    try {
      const data = await api.post(`/api/checklist/${task.dataset.task}`, { done });
      $("#cl-summary").textContent = `${data.done} из ${data.total} задач выполнено`;
      $("#cl-pct").textContent = `${data.percent}%`;
      $("#cl-bar").style.width = `${data.percent}%`;
      data.weeks.forEach((w) => {
        const el = $(`.week[data-week="${w.week}"] [data-cnt]`);
        if (el) el.textContent = `${w.done} из ${w.total}`;
      });
    } catch (e) { task.classList.toggle("done", !done); fail(e); }
  }));

  // ---- база знаний: поиск с debounce
  const search = $("#kb-search");
  if (search) {
    const box = $("#kb-results"); let timer;
    search.addEventListener("input", () => {
      clearTimeout(timer);
      const q = search.value.trim();
      if (q.length < 2) { box.classList.remove("open"); return; }
      timer = setTimeout(async () => {
        try {
          const { categories, results } = await api.get(`/api/kb/search?q=${encodeURIComponent(q)}`);
          const cats = categories.map((c) => `<a href="/kb/${esc(c.slug)}" class="res-cat"><span>Раздел: ${esc(c.title)}</span><span class="muted">${c.count} тем</span></a>`);
          const tops = results.map((r) => `<a href="/kb/topic/${esc(r.slug)}"><span>${esc(r.title)}${r.match === "content" ? '<small class="muted"> · найдено в тексте</small>' : ""}</span><span class="muted">${esc(r.category)}</span></a>`);
          const ask = `<a href="/chat?q=${encodeURIComponent(q)}" class="res-ask"><span>${cats.length || tops.length ? "Нет нужного? Спросить AI-buddy" : "В базе знаний такой темы нет — спросить AI-buddy"}</span><span class="muted">→</span></a>`;
          box.innerHTML = [...cats, ...tops, ask].join("");
          box.classList.add("open");
        } catch (e) { fail(e); }
      }, 220);
    });
    document.addEventListener("click", (e) => { if (!e.target.closest(".search")) box.classList.remove("open"); });
  }
  const topicDone = $("#topic-done");
  if (topicDone) topicDone.addEventListener("click", async () => {
    const done = topicDone.dataset.done !== "1";
    try {
      await api.post(`/api/kb/topics/${topicDone.dataset.slug}/done`, { done });
      topicDone.dataset.done = done ? "1" : "0";
      topicDone.textContent = done ? "Изучено ✓" : "Отметить изученным";
      topicDone.classList.toggle("secondary", done);
      toast(done ? "Тема отмечена изученной" : "Отметка снята");
    } catch (e) { fail(e); }
  });

  // ---- прогресс: карусель этапов
  const plan = $("#plan");
  if (plan) {
    const total = +plan.dataset.total; let cur = +plan.dataset.current; let start = Math.max(1, Math.min(cur - 1, total - 2));
    const tl = $("#timeline");
    const render = () => {
      $$(".wk", plan).forEach((c) => { const w = +c.dataset.w; c.style.display = w >= start && w < start + 3 ? "" : "none"; });
      tl.innerHTML = [0, 1, 2].map((i) => start + i <= total
        ? `<span class="pt ${start + i === cur ? "cur" : ""}" style="left:calc(${(i * 100) / 3}% + ${100 / 6}%)"></span>` : "").join("");
      $("#wk-label").textContent = `Этап ${cur} из ${total}`;
    };
    $("#wk-prev").addEventListener("click", () => { cur = Math.max(1, cur - 1); if (cur < start) start = cur; render(); });
    $("#wk-next").addEventListener("click", () => { cur = Math.min(total, cur + 1); if (cur > start + 2) start = cur - 2; render(); });
    render();
  }

  // ---- трекер целей
  const goalForm = $("#goal-form");
  if (goalForm) goalForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(goalForm);
    try {
      await api.post("/api/goals", { title: f.get("title"), metric: f.get("metric"), due: f.get("due") || null });
      location.reload();
    } catch (err) { fail(err); }
  });
  $$("[data-goal]").forEach((row) => {
    const id = row.dataset.goal;
    row.querySelector("input[type=checkbox]")?.addEventListener("change", async (e) => {
      try { await api.post(`/api/goals/${id}/status`, { done: e.target.checked }); row.classList.toggle("done", e.target.checked); } catch (err) { fail(err); }
    });
    row.querySelector("[data-del]")?.addEventListener("click", async () => {
      try { await api.del(`/api/goals/${id}`); row.remove(); } catch (err) { fail(err); }
    });
  });
  $$("[data-agree]").forEach((b) => b.addEventListener("click", async () => {
    try { await api.post(`/api/goals/${b.dataset.agree}/agree`); b.outerHTML = '<span class="chip granted">Согласовано</span>'; } catch (e) { fail(e); }
  }));

  // ---- наставник: календарь и приглашение на встречу
  const meetForm = $("#meet-form");
  if (meetForm) {
    let selected = null; let days = []; let page = 0;
    const WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
    const grid = $("#cal-grid"); const slotsBox = $("#slots");
    const pickDay = (d) => {
      $$(".cal-day", grid).forEach((x) => x.classList.toggle("selected", x.dataset.date === d.date));
      $("#slots-label").textContent = d.slots.length ? `Свободное время · ${d.label}` : `${d.label}: свободного времени нет`;
      slotsBox.innerHTML = ""; selected = null;
      d.slots.forEach((s) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "slot"; b.textContent = s.label;
        b.addEventListener("click", () => { $$(".slot", slotsBox).forEach((x) => x.classList.remove("selected")); b.classList.add("selected"); selected = s.start; });
        slotsBox.appendChild(b);
      });
    };
    const renderCal = () => {
      // неделя = 7 дней начиная с понедельника
      const weeks = [];
      if (days.length) {
        const lead = days[0].weekday;
        const padded = Array(lead).fill(null).concat(days);
        for (let i = 0; i < padded.length; i += 7) weeks.push(padded.slice(i, i + 7));
      }
      page = Math.max(0, Math.min(page, Math.max(0, weeks.length - 2)));
      const shown = weeks.slice(page, page + 2);
      const real = shown.flat().filter(Boolean);
      $("#cal-title").textContent = real.length ? `${real[0].day} ${real[0].month} — ${real[real.length - 1].day} ${real[real.length - 1].month}` : "Нет данных";
      grid.innerHTML = WD.map((w) => `<span class="cal-wd">${w}</span>`).join("") + shown.map((wk) => wk.concat(Array(7 - wk.length).fill(null)).map((d) => d
        ? `<button type="button" class="cal-day ${d.slots.length ? "free" : "busy"}" data-date="${d.date}" ${d.slots.length ? "" : "disabled"}><b>${d.day}</b><small>${d.slots.length ? d.slots.length + " " + (d.slots.length === 1 ? "окно" : d.slots.length < 5 ? "окна" : "окон") : "—"}</small></button>`
        : '<span class="cal-day empty"></span>').join("")).join("");
      $$(".cal-day[data-date]", grid).forEach((b) => b.addEventListener("click", () => pickDay(days.find((d) => d.date === b.dataset.date))));
      $("#cal-prev").disabled = page === 0; $("#cal-next").disabled = page + 2 >= weeks.length;
    };
    $("#cal-prev").addEventListener("click", () => { page -= 1; renderCal(); });
    $("#cal-next").addEventListener("click", () => { page += 1; renderCal(); });
    const loadCal = async () => {
      selected = null; slotsBox.innerHTML = ""; $("#slots-label").textContent = "Выбери день в календаре";
      try {
        ({ days } = await api.get("/api/meetings/availability?days=28"));
        page = 0; renderCal();
        const firstFree = days.find((d) => d.slots.length);
        if (firstFree) pickDay(firstFree);
      } catch (e) { grid.innerHTML = `<span class="error-line">${esc(e.message)}</span>`; }
    };
    $$("[data-open]").forEach((b) => b.addEventListener("click", () => { openModal(b.dataset.open); loadCal(); }));
    if (location.hash === "#schedule") { openModal("#meet-modal"); loadCal(); }
    meetForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!selected) { toast("Выбери день и время встречи", true); return; }
      const f = new FormData(meetForm);
      try {
        await api.post("/api/meetings", { start: selected, title: f.get("title"), agenda: f.get("agenda") });
        toast("Приглашение отправлено наставнику"); setTimeout(() => location.replace("/mentor"), 500);
      } catch (err) { fail(err); }
    });
  }
  $$("[data-meeting-cancel]").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest("[data-meeting]");
    try { await api.post(`/api/meetings/${row.dataset.meeting}/cancel`); toast("Приглашение отменено"); row.remove(); } catch (e) { fail(e); }
  }));
  $$("[data-meet-decide]").forEach((b) => b.addEventListener("click", async () => {
    const card = b.closest("[data-meeting]");
    const accept = b.dataset.meetDecide === "1";
    try {
      await api.post(`/api/meetings/${card.dataset.meeting}/decide`, { accept, comment: card.querySelector("[data-comment]").value });
      toast(accept ? "Встреча подтверждена — стажёр получил уведомление" : "Отклонено — стажёр выберет другое время");
      setTimeout(() => location.reload(), 500);
    } catch (e) { fail(e); }
  }));

  // ---- профиль
  const profileForm = $("#profile-form");
  if (profileForm) {
    $("[data-open-profile]").addEventListener("click", () => openModal("#profile-modal"));
    profileForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(profileForm);
      try {
        await api.put("/api/profile", { full_name: f.get("full_name"), phone: f.get("phone"), email: f.get("email") });
        toast("Профиль сохранён"); setTimeout(() => location.reload(), 400);
      } catch (err) { fail(err); }
    });
  }

  // ---- доступы: каналы и отмена заявок
  $("[data-join-all]")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { const { added } = await api.post("/api/chats/join", {}); toast(`Добавил в ${added.length} каналов`); setTimeout(() => location.reload(), 500); } catch (err) { e.target.disabled = false; fail(err); }
  });
  $$("[data-join]").forEach((b) => b.addEventListener("click", async () => {
    try { await api.post("/api/chats/join", { codes: [b.dataset.join] }); location.reload(); } catch (e) { fail(e); }
  }));
  $$("[data-req-cancel]").forEach((b) => b.addEventListener("click", async () => {
    try { await api.post(`/api/requests/${b.dataset.reqCancel}/cancel`); location.reload(); } catch (e) { fail(e); }
  }));

  // ---- куратор: кабинеты пользователей
  const userForm = $("#user-form");
  if (userForm) {
    const showCreds = (login, password) => { $("#cred-login").textContent = login; $("#cred-pass").textContent = password; closeModals(); openModal("#cred-modal"); };
    const role = $("#role-select");
    const syncRole = () => $$(".intern-only", userForm).forEach((x) => { x.style.display = role.value === "intern" ? "" : "none"; });
    role.addEventListener("change", syncRole); syncRole();
    $("[data-open-user]").addEventListener("click", () => openModal("#user-modal"));
    $("[data-reload-close]").addEventListener("click", () => location.reload());
    userForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(userForm);
      const body = Object.fromEntries(f.entries());
      body.join_chats = f.get("join_chats") === "on";
      if (body.role !== "intern") { delete body.mentor_id; delete body.lead_id; }
      try { const res = await api.post("/api/admin/users", body); showCreds(res.user.login, res.password); } catch (err) { fail(err); }
    });
    const assignForm = $("#assign-form");
    $$("[data-user]").forEach((row) => {
      const id = row.dataset.user;
      row.querySelector("[data-assign]")?.addEventListener("click", () => {
        assignForm.elements.user_id.value = id;
        if (row.dataset.mentor) assignForm.elements.mentor_id.value = row.dataset.mentor;
        if (row.dataset.lead) assignForm.elements.lead_id.value = row.dataset.lead;
        $("#assign-who").textContent = $("b", row).textContent;
        openModal("#assign-modal");
      });
      row.querySelector("[data-reset]")?.addEventListener("click", async () => {
        try { const res = await api.post(`/api/admin/users/${id}/password`); showCreds($(".small", row).textContent.split(" · ")[0], res.password); } catch (e) { fail(e); }
      });
      row.querySelector("[data-active]")?.addEventListener("click", async (e) => {
        try { await api.post(`/api/admin/users/${id}/active`, { active: e.target.dataset.active === "1" }); location.reload(); } catch (err) { fail(err); }
      });
    });
    assignForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const el = assignForm.elements;
      try {
        await api.post(`/api/admin/users/${el.user_id.value}/assign`, { mentor_id: el.mentor_id.value, lead_id: el.lead_id.value });
        toast("Наставник и руководитель закреплены"); setTimeout(() => location.reload(), 400);
      } catch (err) { fail(err); }
    });
  }

  // ---- заметки
  const noteForm = $("#note-form");
  if (noteForm) {
    const el = (n) => noteForm.elements[n];
    const openNote = (note) => {
      el("note_id").value = note ? note.id : "";
      el("title").value = note ? note.title : "";
      el("body").value = note ? note.body : "";
      $("#note-modal-title").textContent = note ? "Редактировать заметку" : "Новая заметка";
      $("[data-note-delete]").style.display = note ? "" : "none";
      openModal("#note-modal");
    };
    $("[data-note-new]").addEventListener("click", () => openNote(null));
    $$("[data-note-edit]").forEach((b) => b.addEventListener("click", () => {
      const card = b.closest(".note");
      openNote({ id: card.dataset.note, title: $("h4", card).textContent, body: $("p", card).textContent });
    }));
    noteForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { title: el("title").value, body: el("body").value };
      const id = el("note_id").value;
      try {
        if (id) await api.put(`/api/notes/${id}`, body); else await api.post("/api/notes", body);
        location.reload();
      } catch (err) { fail(err); }
    });
    $("[data-note-delete]").addEventListener("click", async () => {
      try { await api.del(`/api/notes/${el("note_id").value}`); location.reload(); } catch (err) { fail(err); }
    });
  }

  // ---- заявки: отмена и согласование
  $$("[data-req]").forEach((card) => {
    const [kind, id] = card.dataset.req.split("/");
    card.querySelector("[data-cancel]")?.addEventListener("click", async () => {
      try { await api.post(`/api/requests/${kind}/${id}/cancel`); location.reload(); } catch (e) { fail(e); }
    });
    $$("[data-decide]", card).forEach((b) => b.addEventListener("click", async () => {
      const comment = card.querySelector("[data-comment]")?.value || "";
      try {
        await api.post(`/api/approvals/${kind}/${id}`, { approve: b.dataset.decide === "1", comment });
        toast(b.dataset.decide === "1" ? "Согласовано — стажёр получил уведомление" : "Отклонено");
        card.style.opacity = ".4"; setTimeout(() => card.remove(), 600);
      } catch (e) { fail(e); }
    }));
  });

  // ---- ответы наставника
  $$("[data-question]").forEach((card) => {
    card.querySelector("[data-send-answer]").addEventListener("click", async () => {
      const answer = card.querySelector("[data-answer]").value.trim();
      if (!answer) { toast("Напишите ответ", true); return; }
      try {
        await api.post(`/api/mentor/questions/${card.dataset.question}/answer`, { answer, publish: card.querySelector("[data-publish]").checked });
        toast("Ответ отправлен стажёру в чат"); card.style.opacity = ".4"; setTimeout(() => card.remove(), 600);
      } catch (e) { fail(e); }
    });
  });

  // ---- feature flags
  $$("[data-flag]").forEach((input) => input.addEventListener("change", async () => {
    try { await api.post(`/api/flags/${input.dataset.flag}`, { enabled: input.checked }); toast("Флаг обновлён"); } catch (e) { input.checked = !input.checked; fail(e); }
  }));
})();
