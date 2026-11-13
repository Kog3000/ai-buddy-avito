# SDD-спецификации AI-buddy

Разработка AI-функций велась по спецификациям в формате [OpenSpec](https://github.com/Fission-AI/OpenSpec):
сначала описывается требование и сценарии (`WHEN / THEN`), затем пишется тест, затем код.
Каждый сценарий ссылается на автотест, который его проверяет.

| Спецификация | О чём | Тесты |
|---|---|---|
| [agent/spec.md](agent/spec.md) | Поведение агента: маршрутизация, подтверждения, RAG, деградация LLM | `tests/test_agent_flows.py`, `tests/test_nlu.py`, `tests/test_llm.py` |
| [tools/spec.md](tools/spec.md) | Контракт MCP-инструментов корпоративных сервисов | `tests/test_mcp.py` |
| [security/spec.md](security/spec.md) | Права, данные, CSRF, чувствительные сценарии | `tests/test_services.py`, `tests/test_api_web.py` |
| [scenarios/01-kb-qa.md](scenarios/01-kb-qa.md) | Вопросы о команде и инструментах с учётом контекста | `test_kb_answer_*`, `test_context_answers` |
| [scenarios/02-access.md](scenarios/02-access.md) | Доступы к основным сервисам | `test_access_*` |
| [scenarios/03-day-off.md](scenarios/03-day-off.md) | Отгул через сервис отсутствий | `test_absence_*` |
| [scenarios/04-mentor.md](scenarios/04-mentor.md) | Встреча 1:1 и вопросы наставнику | `test_meeting_flow`, `test_not_found_escalation_and_faq_reuse` |
| [scenarios/05-adaptation-progress.md](scenarios/05-adaptation-progress.md) | Этапы адаптации, статусы, следующие шаги, трекер целей | `test_checklist_*`, `test_goal_flow`, `test_cron_*` |
| [scenarios/06-curator-access-chats.md](scenarios/06-curator-access-chats.md) | Кабинеты от куратора, доступы, каналы, вложения, уведомления | `tests/test_new_features.py` |

Изменение поведения агента = сначала правка спецификации, затем теста, затем кода.
