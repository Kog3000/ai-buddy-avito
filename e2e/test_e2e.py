"""E2E: проход сценариев в настоящем браузере (Playwright + Chromium) с замером покрытия JS через CDP.

Запуск: python -m pytest e2e -q   (нужен Chromium: `playwright install chromium` или CHROME_PATH)
Отчёт о покрытии JS: coverage/js-coverage.txt
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "app" / "static" / "js"
REPORT = ROOT / "coverage" / "js-coverage.txt"
JS_THRESHOLD = 80.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    tmp = tempfile.mkdtemp()
    cfg = type("E2EConfig", (Config,), {
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp}/e2e.db", "ACCESS_AUTO_APPROVE_SECONDS": 1,
        "SYNC_JOBS": False, "LLM_PROVIDER": "offline", "TESTING": True})
    app = create_app(cfg)
    port = _free_port()
    srv = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


class Coverage:
    """Накопление V8 precise coverage по файлам /static/js/*.js между навигациями."""

    def __init__(self, page):
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.send("Profiler.enable")
        self.cdp.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})
        self.hits: dict[str, bytearray] = {}

    def collect(self):
        result = self.cdp.send("Profiler.takePreciseCoverage")["result"]
        for script in result:
            m = re.search(r"/static/js/([\w.-]+\.js)", script["url"])
            if not m:
                continue
            name = m.group(1)
            source = (JS_DIR / name).read_text(encoding="utf-8")
            counts = [0] * len(source)
            for fn in script["functions"]:
                for rng in fn["ranges"]:  # вложенные диапазоны идут после внешних и переопределяют их
                    for i in range(rng["startOffset"], min(rng["endOffset"], len(source))):
                        counts[i] = rng["count"]
            acc = self.hits.setdefault(name, bytearray(len(source)))
            for i, c in enumerate(counts):
                if c > 0:
                    acc[i] = 1

    def report(self) -> float:
        lines_out, total, covered = [], 0, 0
        for path in sorted(JS_DIR.glob("*.js")):
            source = path.read_text(encoding="utf-8")
            hits = self.hits.get(path.name, bytearray(len(source)))
            f_total = f_cov = 0
            missing = []
            offset = 0
            for no, line in enumerate(source.split("\n"), 1):
                stripped = line.strip()
                code = stripped and not stripped.startswith(("//", "/*", "*")) and stripped not in {"}", "});", "})();", "};", ")", "]);", "});"}
                if code:
                    f_total += 1
                    idx = [offset + i for i, ch in enumerate(line) if not ch.isspace()]
                    if any(hits[i] for i in idx if i < len(hits)):
                        f_cov += 1
                    else:
                        missing.append(no)
                offset += len(line) + 1
            total += f_total
            covered += f_cov
            pct = 100 * f_cov / f_total if f_total else 100
            lines_out.append(f"{path.name:<12} {f_cov:>4}/{f_total:<4} {pct:5.1f}%  missing: {_ranges(missing)}")
        pct_all = 100 * covered / total if total else 100
        REPORT.parent.mkdir(exist_ok=True)
        REPORT.write_text("JS line coverage (V8 precise coverage, E2E-сценарии)\n" + "\n".join(lines_out)
                          + f"\nTOTAL        {covered:>4}/{total:<4} {pct_all:5.1f}%\n", encoding="utf-8")
        return pct_all


def _ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in nums + [None]:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = n
    return ", ".join(out[:25])


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        exe = os.getenv("CHROME_PATH") or shutil.which("google-chrome") or shutil.which("chromium")
        kwargs = {"executable_path": exe} if exe else {}
        try:
            b = p.chromium.launch(args=["--no-sandbox"], **kwargs)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium недоступен: {exc}")
        yield b
        b.close()


def test_full_journey(server, browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ru-RU")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    cov = Coverage(page)

    def go(path):
        cov.collect()
        page.goto(server + path)
        page.wait_for_load_state("networkidle")

    def bot_count():
        return page.evaluate("document.querySelectorAll('.msg.bot:not(#typing):not(.error)').length")

    def wait_reply(n):
        page.wait_for_function(
            f"document.querySelectorAll('.msg.bot:not(#typing):not(.error)').length > {n} && !document.getElementById('typing') && document.body.dataset.pending !== '1'",
            timeout=15000)

    def ask(text):
        n = bot_count()
        page.fill("#chat-input", text)
        page.press("#chat-input", "Enter")
        wait_reply(n)

    def click_and_wait(selector):
        """Клик по элементу последнего сообщения AI-buddy (или по подсказкам) и ожидание ответа."""
        n = bot_count()
        try:
            if selector.startswith(".suggestions"):
                page.click(selector)
            else:
                page.locator(".msg.bot:not(#typing)").filter(has=page.locator(selector)).last.locator(selector).first.click()
        except Exception:
            REPORT.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(REPORT.parent / "e2e-failure.png"))
            raise
        wait_reply(n)

    def login(name):
        cov.collect()
        page.goto(server + "/login")
        page.click(".accounts summary")
        page.click(f"[data-login={name}]")
        page.wait_for_load_state("networkidle")

    # ---------------- стажёр: главная → чат
    login("arina")
    assert "Привет, Арина!" in page.content()
    page.click("text=Задать вопрос")
    page.wait_for_selector(".msg.bot")

    # вопрос по базе знаний + оценка ответа + подсказка
    ask("Где хранятся документы нашей команды?")
    assert "Confluence" in page.inner_text(".chat-log")
    page.locator(".msg.bot").last.locator(".fb button").first.click()
    page.wait_for_selector(".fb button.on")
    click_and_wait(".suggestions button >> nth=0")

    # пока ответ не пришёл — повторная отправка блокируется тостом
    page.fill("#chat-input", "Что дальше?")
    page.press("#chat-input", "Enter")
    page.fill("#chat-input", "ещё вопрос")
    page.press("#chat-input", "Enter")
    page.wait_for_selector("#toast.show")
    page.wait_for_function("!document.getElementById('typing')", timeout=15000)
    page.wait_for_timeout(300)
    # карточка шагов: «Помочь» и «Готово»
    click_and_wait(".steps-list [data-ask]")
    ask("Что дальше?")
    click_and_wait(".steps-list [data-done]")

    # сценарий доступов: карточка с полями
    ask("Нужен доступ к Grafana")
    page.locator(".msg.bot").filter(has=page.locator("select[name=level]")).last.locator("select[name=level]").select_option("read")
    page.locator(".msg.bot").filter(has=page.locator("textarea[name=justification]")).last.locator("textarea[name=justification]").fill( "Смотреть дашборды сервиса")
    click_and_wait("text=Отправить заявку")
    assert "сервис доступов" in page.inner_text(".chat-log")
    ask("Нужен доступ к Jira")
    click_and_wait("text=Отправить заявку")

    # отгул: дата → подтверждение с чекбоксом
    ask("Как оформить отгул?")
    ask("в понедельник")
    page.locator(".msg.bot").filter(has=page.locator("input[name=comment]")).last.locator("input[name=comment]").fill("Семейные дела")
    page.locator(".msg.bot").filter(has=page.locator("input[name=notify_mentor]")).last.locator("input[name=notify_mentor]").uncheck()
    click_and_wait("text=Отправить заявку")

    # встреча с наставником: слоты → подтверждение
    ask("Запланируй встречу с наставником")
    click_and_wait(".acard .slot")
    click_and_wait("text=Запланировать")
    assert "Отправил приглашение" in page.inner_text(".chat-log")

    # вложение: файл без текста → предложение передать наставнику
    page.set_input_files("#chat-file", files=[{"name": "error.log", "mimeType": "text/plain", "buffer": b"TypeError: x is undefined"}])
    page.wait_for_selector("#attach-chip:not([hidden])")
    page.click("#attach-clear")
    page.set_input_files("#chat-file", files=[{"name": "error.log", "mimeType": "text/plain", "buffer": b"TypeError: x is undefined"}])
    n = bot_count()
    page.click("#send-btn")
    wait_reply(n)
    assert page.locator(".msg.user .att").count() >= 1
    page.set_input_files("#chat-file", files=[{"name": "big.txt", "mimeType": "text/plain", "buffer": b"0" * (5 * 1024 * 1024 + 10)}])
    page.wait_for_selector("#toast.show")
    ask("Добавь меня во все нужные чаты")
    click_and_wait("text=Добавить во все")

    # нет ответа → эскалация, затем отмена второго черновика
    ask("Сколько стоит обед в столовой?")
    click_and_wait("text=Передать наставнику")
    ask("Добавь цель: изучить TanStack Query")
    click_and_wait("text=Отмена")

    # сбой сервера → «Повторить»
    state = {"fail": True}

    def flaky(route):
        if state["fail"]:
            state["fail"] = False
            route.fulfill(status=500, content_type="application/json", body='{"error":"boom","message":"Сбой"}')
        else:
            route.continue_()
    page.route("**/api/chat/send", flaky)
    page.fill("#chat-input", "Мои заявки")
    page.press("#chat-input", "Enter")
    page.wait_for_selector(".error-line button")
    n = bot_count()
    page.click(".error-line button")
    wait_reply(n)
    page.unroute("**/api/chat/send")
    # фоновое обновление чата подтягивает проактивное сообщение (политика одобрила Grafana)
    page.wait_for_function("document.querySelector('.chat-log').innerText.includes('выдан')", timeout=15000)

    # автоматическая отправка из ?q=
    go("/chat?q=" + "Кто мой наставник?")
    page.wait_for_function("document.querySelector('.chat-log').innerText.includes('Твой наставник')", timeout=15000)

    # ---------------- чек-лист
    go("/checklist")
    before = page.inner_text("#cl-summary")
    page.click(".week.open .task:not(.done) .cbox >> nth=0")
    page.wait_for_function(f"document.getElementById('cl-summary').innerText !== {before!r}")
    page.click(".week[data-week='2'] .week-head")
    assert page.is_visible(".week[data-week='2'] .week-body")

    # ---------------- база знаний
    go("/kb")
    page.fill("#kb-search", "code review")
    page.wait_for_selector("#kb-results.open a")
    page.fill("#kb-search", "zzzzqqq")
    page.wait_for_selector("#kb-results >> text=спросить AI-buddy")
    page.click("h1")
    page.fill("#kb-search", "a")
    go("/kb/topic/comm-channels")
    page.click("#topic-done")
    page.wait_for_selector("#topic-done >> text=Изучено")
    page.click("#topic-done")
    page.wait_for_selector("#topic-done >> text=Отметить изученным")

    # ---------------- прогресс: карусель недель и трекер целей
    go("/progress")
    for _ in range(4):
        page.click("#wk-next")
    assert "Этап 5 из 5" in page.inner_text("#wk-label")
    page.click("#wk-prev")
    page.fill("#goal-form input[name=title]", "Закрыть 2 задачи из бэклога")
    page.fill("#goal-form input[name=metric]", "2 MR")
    with page.expect_navigation():
        page.click("#goal-form button")
    page.wait_for_selector("[data-goal]")
    page.check("[data-goal] input[type=checkbox] >> nth=0")
    page.wait_for_selector(".goal-row.done")

    # ---------------- наставник: встреча и заметки
    go("/mentor")
    page.click("[data-open='#meet-modal']")
    page.wait_for_selector("#cal-grid .cal-day.free")
    page.click("#meet-form button[type=submit]")  # без времени → тост
    page.click("#cal-next")
    page.click("#cal-prev")
    page.click("#cal-grid .cal-day.free >> nth=1")
    page.wait_for_selector("#slots .slot")
    page.click("#slots .slot >> nth=0")
    page.fill("#meet-form textarea[name=agenda]", "Разбор ревью")
    page.click("#meet-form button[type=submit]")
    page.wait_for_url("**/mentor")
    page.wait_for_load_state("networkidle")
    assert "Ждёт подтверждения наставника" in page.inner_text("main")
    page.click("[data-meeting-cancel] >> nth=-1")
    page.wait_for_selector("#toast.show")
    page.click("[data-note-new]")
    page.keyboard.press("Escape")
    page.click("[data-note-new]")
    page.fill("#note-form input[name=title]", "1:1 №1")
    page.fill("#note-form textarea[name=body]", "Разобрали ревью")
    with page.expect_navigation():
        page.click("#note-form button[type=submit]")
    page.wait_for_selector(".note >> text=1:1 №1")
    page.click(".note:has-text('1:1 №1') [data-note-edit]")
    page.fill("#note-form input[name=title]", "1:1 №1 (итоги)")
    with page.expect_navigation():
        page.click("#note-form button[type=submit]")
    page.wait_for_selector(".note >> text=1:1 №1 (итоги)")
    page.click(".note:has-text('1:1 №1 (итоги)') [data-note-edit]")
    with page.expect_navigation():
        page.click("[data-note-delete]")
    page.click("[data-open='#meet-modal']")
    page.mouse.click(5, 5)

    go("/home")
    go("/mentor#schedule")
    page.wait_for_selector("#meet-modal.open")

    # ---------------- доступы: сервисы, каналы, отгулы
    go("/requests")
    assert "/access" in page.url
    with page.expect_navigation():
        page.click("[data-req-cancel] >> nth=0")
    go("/access?tab=chats")
    with page.expect_navigation():
        page.click("[data-join] >> nth=0")
    go("/access?tab=absence")
    assert page.locator("[data-req] [data-cancel]").count() >= 1  # заявку на отгул согласует руководитель ниже

    # ---------------- профиль: редактирование
    go("/profile")
    page.click("[data-open-profile]")
    page.fill("#profile-form input[name=full_name]", "Арина Язева-Петрова")
    with page.expect_navigation():
        page.click("#profile-form button[type=submit]")
    assert "Арина Язева-Петрова" in page.inner_text("main")
    page.click("[data-open-profile]")
    page.fill("#profile-form input[name=email]", "bad")
    page.fill("#profile-form input[name=full_name]", "Ф")
    page.evaluate("document.getElementById('profile-form').noValidate = true")
    page.click("#profile-form button[type=submit]")
    page.wait_for_selector("#toast.show.error")
    go("/progress")
    page.click("[data-goal] [data-del] >> nth=0")
    page.wait_for_function("!document.querySelector('[data-goal]')")

    # ---------------- руководитель: согласования, цели, уведомления, флаги
    login("maria")
    go("/approvals")
    page.fill("[data-req] [data-comment] >> nth=0", "Ок")
    page.click("[data-req] [data-decide='1'] >> nth=0")
    page.wait_for_selector("#toast.show")
    go("/team")
    page.click("[data-toggle='#notif']")
    page.wait_for_selector("#notif.open")
    page.click("body")
    go("/metrics")
    page.click(".switch >> nth=2")
    page.wait_for_selector("#toast.show")
    go("/team")
    page.click("[data-toggle='#notif']")
    page.wait_for_function("!document.querySelector('.bell .count')")

    # ---------------- наставник: приглашения на встречи
    login("ivan")
    go("/meetings")
    page.fill("[data-meeting] [data-comment] >> nth=0", "До встречи!")
    page.click("[data-meet-decide='1'] >> nth=0")
    page.wait_for_selector("#toast.show")
    page.wait_for_load_state("networkidle")
    go("/meetings")
    page.click("[data-meet-decide='0'] >> nth=0")
    page.wait_for_selector("#toast.show")
    go("/chat")
    click_and_wait(".suggestions button >> nth=0")

    # ---------------- куратор: кабинеты
    login("elena")
    assert "/admin/users" in page.url
    page.click("[data-open-user]")
    page.select_option("#role-select", "mentor")
    page.select_option("#role-select", "intern")
    page.fill("#user-form input[name=full_name]", "Пётр Смирнов")
    page.fill("#user-form input[name=email]", "petr@avito.ru")
    page.fill("#user-form input[name=start_date]", "2026-12-01")
    page.click("#user-form button[type=submit]")
    page.wait_for_selector("#cred-modal.open")
    assert page.inner_text("#cred-login") == "petr"
    with page.expect_navigation():
        page.click("[data-reload-close]")
    row = "[data-user]:has-text('Пётр Смирнов')"
    page.click(f"{row} [data-assign]")
    with page.expect_navigation():
        page.click("#assign-form button[type=submit]")
    page.click(f"{row} [data-reset]")
    page.wait_for_selector("#cred-modal.open")
    page.click("#cred-modal [data-close]")
    with page.expect_navigation():
        page.click(f"{row} [data-active]")
    page.click("[data-open-user]")
    page.select_option("#role-select", "mentor")
    page.fill("#user-form input[name=full_name]", "Без Почты")
    page.evaluate("document.getElementById('user-form').noValidate = true")
    page.click("#user-form button[type=submit]")
    page.wait_for_selector("#toast.show.error")

    # ---------------- мобильная версия: выезжающее меню
    page.set_viewport_size({"width": 390, "height": 844})
    login("arina")
    page.click(".burger")
    page.wait_for_selector("body.drawer-open")
    page.click(".drawer-backdrop", position={"x": 350, "y": 400})
    page.set_viewport_size({"width": 1440, "height": 1000})

    # ---------------- наставник: ответ на вопрос стажёра
    login("ivan")
    go("/team/questions")
    q = "[data-question]:has-text('обед')"
    page.click(f"{q} [data-send-answer]")  # пустой ответ → тост
    page.fill(f"{q} [data-answer]", "Около 350 ₽")
    page.click(f"{q} [data-send-answer]")
    page.wait_for_selector("#toast.show")

    # стажёр получил ответ в чате
    login("arina")
    go("/chat")
    page.wait_for_function("document.querySelector('.chat-log').innerText.includes('350')", timeout=10000)

    cov.collect()
    pct = cov.report()
    assert not errors, errors
    assert pct >= JS_THRESHOLD, REPORT.read_text(encoding="utf-8")
    ctx.close()
