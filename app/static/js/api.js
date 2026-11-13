/* Явный API-клиент интерфейса: CSRF, JSON, единая обработка ошибок. */
(function () {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  async function request(method, url, body) {
    const opts = { method, headers: { "X-CSRF-Token": csrf }, credentials: "same-origin" };
    if (body instanceof FormData) {
      opts.body = body;  // multipart: заголовок с boundary проставит браузер
    } else if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      throw new ApiError("Нет связи с сервером", 0);
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* пустой ответ */ }
    if (!res.ok) {
      if (res.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); }
      throw new ApiError((data && (data.message || data.error)) || ("Ошибка " + res.status), res.status);
    }
    return data;
  }

  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  let toastTimer;
  function toast(text, isError) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = text;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* markdown-lite: **жирный**, _курсив_, [ссылка](/path), списки «- » и «1. » */
  function md(text) {
    const inline = (s) => escapeHtml(s)
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/(^|[\s(])_(.+?)_(?=[\s.,)!?]|$)/g, "$1<i>$2</i>")
      .replace(/\[([^\]]+)\]\((\/[^)\s]*|https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>');
    const out = []; let list = null;
    for (const raw of String(text || "").split("\n")) {
      const line = raw.trim();
      const b = line.match(/^[-•]\s+(.*)/); const n = line.match(/^\d+\.\s+(.*)/);
      const tag = b ? "ul" : n ? "ol" : null;
      if (tag !== list && list) { out.push(`</${list}>`); list = null; }
      if (tag) { if (!list) { out.push(`<${tag}>`); list = tag; } out.push(`<li>${inline((b || n)[1])}</li>`); }
      else if (line) out.push(`<p>${inline(line)}</p>`);
    }
    if (list) out.push(`</${list}>`);
    return out.join("");
  }

  window.api = {
    get: (u) => request("GET", u),
    post: (u, b) => request("POST", u, b ?? {}),
    put: (u, b) => request("PUT", u, b ?? {}),
    del: (u) => request("DELETE", u),
    ApiError, toast, escapeHtml, md,
  };
})();
