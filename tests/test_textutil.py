from datetime import date

from app.markdown import plural, render
from app.textutil import fmt_range, has_any, parse_dates, tokens, workdays_between

WED = date(2026, 11, 11)  # среда


def test_parse_relative_words():
    assert parse_dates("отгул завтра", WED) == (date(2026, 11, 12), date(2026, 11, 12))
    assert parse_dates("послезавтра", WED) == (date(2026, 11, 13), date(2026, 11, 13))
    assert parse_dates("сегодня", WED) == (WED, WED)


def test_parse_weekdays():
    assert parse_dates("в пятницу", WED) == (date(2026, 11, 13), date(2026, 11, 13))
    assert parse_dates("в среду", WED) == (date(2026, 11, 18), date(2026, 11, 18))
    assert parse_dates("в следующий понедельник", WED) == (date(2026, 11, 23), date(2026, 11, 23))


def test_parse_numeric_and_month_names():
    assert parse_dates("13.11", WED) == (date(2026, 11, 13), date(2026, 11, 13))
    assert parse_dates("05.01.2027", WED) == (date(2027, 1, 5), date(2027, 1, 5))
    assert parse_dates("с 1 по 3 декабря", WED) == (date(2026, 12, 1), date(2026, 12, 3))
    assert parse_dates("15 мая", WED) == (date(2027, 5, 15), date(2027, 5, 15))
    assert parse_dates("с 20.11 по 18.11", WED) == (date(2026, 11, 18), date(2026, 11, 20))


def test_parse_nothing():
    assert parse_dates("хочу отгул", WED) is None
    assert parse_dates("31.02", WED) is None


def test_tokens_and_has_any():
    assert "документ" in tokens("Документы команды")
    assert "и" not in tokens("и в на")
    assert has_any("Нужен доступ к GitLab", ["доступ"])
    assert not has_any("привет", ["доступ"])


def test_formatting():
    assert fmt_range(date(2026, 11, 13), date(2026, 11, 13)) == "Пт, 13 ноября"
    assert "—" in fmt_range(date(2026, 11, 13), date(2026, 11, 16))
    assert workdays_between(date(2026, 11, 13), date(2026, 11, 16)) == 2


def test_markdown_render_is_safe():
    html = render("**Жирный** и _курсив_\n- пункт [ссылка](/kb)\n1. раз\n\n<script>x</script>")
    assert "<b>Жирный</b>" in html and "<i>курсив</i>" in html
    assert "<ul><li>" in html and "<ol><li>раз</li></ol>" in html
    assert '<a href="/kb">ссылка</a>' in html
    assert "<script>" not in html


def test_plural():
    assert [plural(n, "тема", "темы", "тем") for n in (1, 2, 5, 11, 21, 22)] == \
        ["тема", "темы", "тем", "тем", "тема", "темы"]
