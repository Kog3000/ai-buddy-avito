"""База знаний: категории, прогресс изучения и поиск (BM25 по стемам) с учётом прав."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from ..analytics import track
from ..models import KBCategory, KBTopic, TopicProgress, User, db
from ..permissions import PermissionDenied, topic_visible
from ..textutil import tokens

_INDEX: dict = {"version": None}


@dataclass
class SearchHit:
    topic: KBTopic
    score: float

    def to_dict(self) -> dict:
        return {"slug": self.topic.slug, "title": self.topic.title, "score": round(self.score, 3),
                "category": self.topic.category.title, "source": self.topic.source}


def _version() -> tuple:
    count = db.session.query(db.func.count(KBTopic.id)).scalar()
    last = db.session.query(db.func.max(KBTopic.id)).scalar()
    return count, last


def _build_index() -> dict:
    version = _version()
    if _INDEX.get("version") == version:
        return _INDEX
    docs = {}
    df: Counter = Counter()
    for topic in KBTopic.query.all():
        terms = tokens(topic.title) * 3 + tokens(topic.tags) * 2 + tokens(topic.body)
        tf = Counter(terms)
        docs[topic.id] = (tf, len(terms))
        df.update(set(terms))
    avg = sum(length for _, length in docs.values()) / max(1, len(docs))
    _INDEX.clear()
    _INDEX.update({"version": version, "docs": docs, "df": df, "n": len(docs), "avg": avg})
    return _INDEX


def search(user: User, query: str, limit: int = 3) -> list[SearchHit]:
    """Ищет только среди материалов, доступных пользователю."""
    index = _build_index()
    q_terms = tokens(query)
    if not q_terms:
        return []
    k1, b = 1.4, 0.6
    hits: list[SearchHit] = []
    topics = {t.id: t for t in KBTopic.query.all()}
    for topic_id, (tf, length) in index["docs"].items():
        topic = topics[topic_id]
        if not topic_visible(user, topic):
            continue
        score = 0.0
        for term in set(q_terms):
            freq = tf.get(term)
            if not freq:
                continue
            idf = math.log(1 + (index["n"] - index["df"][term] + 0.5) / (index["df"][term] + 0.5))
            score += idf * freq * (k1 + 1) / (freq + k1 * (1 - b + b * length / index["avg"]))
        if score <= 0:
            continue
        coverage = sum(1 for term in set(q_terms) if term in tf) / len(set(q_terms))
        score *= 0.5 + coverage
        if topic.team and topic.team == user.team:
            score *= 1.15  # контекст: материалы своей команды важнее
        if topic.source == "faq":
            score *= 1.1  # ответы наставника на частые вопросы
        hits.append(SearchHit(topic, score))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def search_catalog(user: User, query: str, limit: int = 8) -> dict:
    """Поиск по каталогу: сначала разделы и темы, в названии которых есть запрос (в т.ч. по началу слова),
    затем — темы, найденные по содержанию."""
    q = _norm(query).strip()
    if len(q) < 2:
        return {"categories": [], "topics": []}
    words = [w for w in q.replace("-", " ").split() if len(w) >= 2]

    def title_score(title: str) -> int:
        t = _norm(title)
        if q in t:
            return 3 if t.startswith(q) else 2
        title_words = t.replace("-", " ").replace(":", " ").split()
        if words and all(any(tw.startswith(w) for tw in title_words) for w in words):
            return 1
        return 0

    cats = []
    for cat in KBCategory.query.order_by(KBCategory.order).all():
        if title_score(cat.title):
            count = sum(1 for t in cat.topics if topic_visible(user, t))
            cats.append({"slug": cat.slug, "title": cat.title, "count": count})
    visible = [t for t in KBTopic.query.all() if topic_visible(user, t)]
    by_title = sorted(((title_score(t.title), t) for t in visible), key=lambda x: -x[0])
    topics, seen = [], set()
    for score, t in by_title:
        if score and len(topics) < limit:
            topics.append({"slug": t.slug, "title": t.title, "category": t.category.title, "match": "title"})
            seen.add(t.id)
    for hit in search(user, query, limit=limit):
        if hit.topic.id not in seen and len(topics) < limit and hit.score >= 2:
            topics.append({**hit.to_dict(), "match": "content"})
            seen.add(hit.topic.id)
    return {"categories": cats[:4], "topics": topics}


def get_topic(user: User, slug: str) -> KBTopic:
    topic = KBTopic.query.filter_by(slug=slug).first()
    if topic is None or not topic_visible(user, topic):
        # не раскрываем факт существования недоступного материала
        raise LookupError(slug)
    return topic


def categories(user: User) -> list[dict]:
    done_ids = {p.topic_id for p in TopicProgress.query.filter_by(user_id=user.id)}
    result = []
    for cat in KBCategory.query.order_by(KBCategory.order).all():
        visible = [t for t in cat.topics if topic_visible(user, t) and t.source == "kb"]
        faq = [t for t in cat.topics if topic_visible(user, t) and t.source == "faq"]
        done = sum(1 for t in visible if t.id in done_ids)
        result.append({
            "slug": cat.slug, "title": cat.title, "hours": cat.hours, "count": len(visible),
            "done": done, "percent": round(done * 100 / len(visible)) if visible else 0,
            "topics": [{"slug": t.slug, "title": t.title, "minutes": t.minutes, "done": t.id in done_ids,
                        "team": bool(t.team)} for t in visible],
            "faq": [{"slug": t.slug, "title": t.title} for t in faq],
        })
    return result


def progress(user: User) -> dict:
    cats = categories(user)
    total = sum(c["count"] for c in cats)
    done = sum(c["done"] for c in cats)
    return {"done": done, "total": total, "percent": round(done * 1000 / total) / 10 if total else 0}


def mark_done(user: User, slug: str, done: bool = True) -> None:
    topic = get_topic(user, slug)
    row = TopicProgress.query.filter_by(user_id=user.id, topic_id=topic.id).first()
    if done and row is None:
        db.session.add(TopicProgress(user_id=user.id, topic_id=topic.id))
        db.session.commit()
        track(user, "kb_topic_done", slug=slug)
    elif not done and row is not None:
        db.session.delete(row)
        db.session.commit()


def add_faq(author: User, question: str, answer: str, team: str | None) -> KBTopic:
    """Ответ наставника публикуется в FAQ — следующий стажёр получит его без участия наставника."""
    if author.role not in {"mentor", "lead"}:
        raise PermissionDenied("Публиковать в FAQ может только наставник или руководитель")
    cat = KBCategory.query.filter_by(slug="faq").first()
    count = KBTopic.query.filter_by(source="faq").count()
    topic = KBTopic(slug=f"faq-mentor-{count + 1}", category_id=cat.id, title=question.strip().rstrip("?") + "?",
                    body=answer.strip() + f"\n\n_Ответ наставника: {author.full_name}._", minutes=5,
                    tags=question, team=team, source="faq", order=100 + count)
    db.session.add(topic)
    db.session.commit()
    return topic
