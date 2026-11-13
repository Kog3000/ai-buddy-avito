"""Страницы интерфейса (серверный рендеринг + JS-клиент к /api)."""
from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, request, url_for

from . import metrics
from .analytics import all_flags, track
from .auth import current_user, home_for, login_required, roles_required
from .models import KBCategory, User, db
from .permissions import can_view_user
from .services import absence, access, calendar, checklist, goals, kb, mentor_questions, messenger, notes, users

bp = Blueprint("web", __name__)


def _page(name: str) -> None:
    track(current_user(), "page_view", page=name)


@bp.route("/")
@login_required
def index():
    return redirect(home_for(current_user()))


@bp.route("/home")
@roles_required("intern")
def home():
    u = current_user()
    _page("home")
    meetings = [m for m in calendar.upcoming(u) if m.kind == "mentor"]
    return render_template("home.html", active="home", checklist=checklist.get_checklist(u),
                           steps=checklist.next_steps(u, 2), mentor=u.mentor,
                           next_meeting=calendar.to_dict(meetings[0], u) if meetings else None)


@bp.route("/chat")
@login_required
def chat_page():
    _page("chat")
    return render_template("chat.html", active="chat")


@bp.route("/checklist")
@roles_required("intern")
def checklist_page():
    _page("checklist")
    return render_template("checklist.html", active="checklist", data=checklist.get_checklist(current_user()))


@bp.route("/kb")
@login_required
def kb_page():
    _page("kb")
    return render_template("kb.html", active="kb", categories=kb.categories(current_user()))


@bp.route("/kb/<slug>")
@login_required
def kb_category(slug):
    cats = {c["slug"]: c for c in kb.categories(current_user())}
    if slug not in cats:
        abort(404)
    return render_template("kb_category.html", active="kb", cat=cats[slug])


@bp.route("/kb/topic/<slug>")
@login_required
def kb_topic(slug):
    try:
        topic = kb.get_topic(current_user(), slug)
    except LookupError:
        abort(404)
    _page("kb_topic")
    done_slugs = {t["slug"] for c in kb.categories(current_user()) for t in c["topics"] if t["done"]}
    cat = db.session.get(KBCategory, topic.category_id)
    return render_template("kb_topic.html", active="kb", topic=topic, cat=cat, done=topic.slug in done_slugs)


@bp.route("/progress")
@roles_required("intern")
def progress_page():
    u = current_user()
    _page("progress")
    return render_template("progress.html", active="progress", data=checklist.get_checklist(u),
                           kb_progress=kb.progress(u), goals=[goals.to_dict(g) for g in goals.list_goals(u, u)])


@bp.route("/mentor")
@roles_required("intern")
def mentor_page():
    u = current_user()
    _page("mentor")
    meetings = [calendar.to_dict(m, u) for m in calendar.upcoming(u) if m.kind == "mentor"]
    return render_template("mentor.html", active="mentor", mentor=u.mentor, meetings=meetings,
                           notes=[notes.to_dict(n) for n in notes.list_notes(u)],
                           questions=mentor_questions.of_intern(u)[:5])


@bp.route("/profile")
@login_required
def profile_page():
    u = current_user()
    events = [calendar.to_dict(m, u) for m in calendar.upcoming(u) if m.status == "accepted"][:3]
    return render_template("profile.html", active="profile", events=events)


@bp.route("/requests")
@login_required
def requests_page():
    return redirect(url_for("web.access_page"))


@bp.route("/access")
@roles_required("intern")
def access_page():
    """Доступы: сервисы со статусом доступа, каналы мессенджера, отгулы и отпуска."""
    u = current_user()
    _page("access")
    access.sync(u)
    reqs = access.my_requests(u)
    services = []
    for res in access.resources():
        mine = [r for r in reqs if r.resource_id == res.id]
        active = next((r for r in mine if r.status in {"granted", "pending_approval"}), None)
        shown = active or (mine[0] if mine else None)
        services.append({"code": res.code, "name": res.name, "description": res.description, "base": res.base,
                         "levels": access.allowed_levels(u, res), "rec": access.recommended_level(u, res),
                         "request": access.to_dict(shown) if shown else None})
    tab = request.args.get("tab", "services")
    if tab not in {"services", "chats", "absence"}:
        tab = "services"
    return render_template("access.html", active="access", services=services, chats=messenger.status(u),
                           missing=len(messenger.missing_required(u)), tab=tab,
                           absence_requests=[absence.to_dict(r) for r in absence.my_requests(u)])


@bp.route("/help")
@login_required
def help_page():
    return render_template("help.html", active="help")


# ------------------------------------------------------------------ наставник / руководитель / куратор
@bp.route("/team")
@roles_required("mentor", "lead")
def team():
    u = current_user()
    interns = metrics.interns_of(u)
    cards = []
    for intern in interns:
        data = checklist.get_checklist(intern)
        cards.append({"user": intern, "checklist": data, "next": checklist.next_steps(intern, 2),
                      "goals": [goals.to_dict(g) for g in goals.list_goals(u, intern)]})
    return render_template("team.html", active="team", interns=cards,
                           questions=mentor_questions.open_for(u) if u.role == "mentor" else [],
                           metrics=metrics.compute(u))


@bp.route("/team/questions")
@roles_required("mentor", "lead")
def team_questions():
    u = current_user()
    return render_template("questions.html", active="questions", open=mentor_questions.open_for(u),
                           answered=mentor_questions.answered_for(u))


@bp.route("/team/intern/<int:user_id>")
@roles_required("mentor", "lead")
def intern_page(user_id):
    intern = db.session.get(User, user_id)
    if intern is None or not can_view_user(current_user(), intern):
        abort(403)
    return render_template("intern.html", active="team", intern=intern, data=checklist.get_checklist(intern),
                           goals=[goals.to_dict(g) for g in goals.list_goals(current_user(), intern)])


@bp.route("/approvals")
@roles_required("lead", "mentor")
def approvals():
    u = current_user()
    access.sync()
    return render_template("approvals.html", active="approvals",
                           access_items=[access.to_dict(r) for r in access.for_approval(u)],
                           absence_items=[absence.to_dict(r) for r in absence.for_approval(u)])


@bp.route("/meetings")
@roles_required("mentor")
def meetings_page():
    u = current_user()
    upcoming = [calendar.to_dict(m, u) for m in calendar.upcoming(u, limit=20) if m.status == "accepted"]
    return render_template("meetings.html", active="meetings",
                           pending=[calendar.to_dict(m, u) for m in calendar.pending_for(u)], upcoming=upcoming)


@bp.route("/admin/users")
@roles_required("admin")
def admin_users():
    u = current_user()
    return render_template("admin_users.html", active="admin",
                           users=[users.to_dict(x) for x in users.list_users(u)],
                           mentors=User.query.filter_by(role="mentor", active=True).all(),
                           leads=User.query.filter_by(role="lead", active=True).all())


@bp.route("/metrics")
@roles_required("mentor", "lead", "admin")
def metrics_page():
    return render_template("metrics.html", active="metrics", m=metrics.compute(current_user()), flags=all_flags())
