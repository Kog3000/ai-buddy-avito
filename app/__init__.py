"""AI-buddy — прототип ИИ-наставника для стажёров."""
from __future__ import annotations

import logging

from flask import Flask, render_template

from .config import Config


def create_app(config: type[Config] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config or Config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from . import api, auth, web
    from .models import db
    from .seed import seed
    from .services.notify import recent, unread

    from .markdown import plural, render as render_md

    db.init_app(app)
    app.jinja_env.filters["md"] = render_md
    app.jinja_env.filters["plural"] = plural
    from .icons import svg_icon
    app.jinja_env.globals["svg_icon"] = svg_icon
    app.register_blueprint(auth.bp)
    app.register_blueprint(web.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(api.mcp_bp)

    @app.context_processor
    def inject():
        user = auth.current_user()
        return {"me": user, "csrf_token": auth.csrf_token,
                "notifications": recent(user) if user else [],
                "unread_count": len(unread(user)) if user else 0}

    @app.errorhandler(403)
    def forbidden(_):
        return render_template("error.html", code=403, message="Недостаточно прав для этой страницы"), 403

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", code=404, message="Страница не найдена"), 404

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.cli.command("seed")
    def seed_cmd():
        """Создать схему и заполнить тестовыми данными."""
        db.create_all()
        seed()
        print("seeded")

    @app.cli.command("cron")
    def cron_cmd():
        """Выполнить задачи ИИ-крона (синхронизация заявок, напоминания)."""
        from .cron import run_all
        print(run_all())

    with app.app_context():
        db.create_all()
        if app.config["SEED_ON_START"]:
            seed()
        db.session.remove()
        if not app.config.get("TESTING"):
            db.engine.dispose()  # не делим соединения между воркерами gunicorn (--preload)
    return app
