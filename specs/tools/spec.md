# Спецификация: MCP-инструменты корпоративных сервисов

Сервер: `POST /mcp`, JSON-RPC 2.0, методы `initialize`, `tools/list`, `tools/call` (`app/mcp/server.py`).
Агент использует тот же сервер через in-process `MCPClient`, поэтому проверки прав едины для UI, агента и внешних клиентов.

## Общие требования

### Requirement: Идентичность и роли
- Вызывающий определяется по сессии (в Авито — корпоративная авторизация); аргумента «от имени кого» нет.
- `tools/list` возвращает только инструменты, разрешённые роли.
- Вызов запрещённого инструмента → `isError=true`, текст «Доступ запрещён: …».
- Тест: `test_initialize_and_list_filtered_by_role`, `test_tools_call_success_and_errors`

### Requirement: Валидация аргументов
- Схема `inputSchema` (JSON Schema): обязательные поля, типы, `enum`, `additionalProperties=false`.
- Ошибки бизнес-валидации сервиса возвращаются как `isError=true` с понятным текстом.
- Тест: `test_validate_args`

### Requirement: Аннотации
- `annotations.readOnlyHint` — инструмент только читает.
- `annotations.requiresConfirmation` — инструмент меняет данные; агент вызывает его только после подтверждения пользователем.

## Каталог

| Инструмент | Роли | Изменяет | Назначение |
|---|---|---|---|
| `profile_get` | все | — | команда, наставник, руководитель, этап адаптации |
| `kb_search(query, limit)` | все | — | поиск по доступным материалам |
| `kb_get_topic(slug)` | все | — | текст материала (недоступный = «не найден») |
| `checklist_get`, `checklist_next_steps(limit)` | стажёр | — | этапы адаптации и следующие шаги |
| `checklist_set_task(task_id, done)` | стажёр | ✔ | отметка задачи |
| `access_list_resources` | все | — | каталог ресурсов, доступные уровни, активные заявки |
| `access_create_request(resource, level, justification, project)` | все | ✔ | заявка в сервис доступов |
| `access_my_requests` | все | — | статусы своих заявок |
| `access_decide(request_id, approve, comment)` | согласующие | ✔ | решение по заявке (только назначенный согласующий) |
| `absence_policy(kind)` | все | — | тестовые правила отсутствий |
| `absence_create_request(kind, date_from, date_to, comment, notify_mentor)` | стажёр | ✔ | заявка в сервис отсутствий |
| `absence_my_requests` | все | — | свои заявки |
| `absence_decide(...)` | руководитель | ✔ | решение по отсутствию |
| `calendar_upcoming` | все | — | ближайшие события |
| `calendar_free_slots(days)` | стажёр | — | общие свободные слоты со своим наставником |
| `calendar_request_mentor_meeting(start, title, agenda)` | стажёр | ✔ | приглашение на 1:1 (ждёт подтверждения) |
| `calendar_pending_requests` | наставник | — | приглашения на подтверждении |
| `calendar_decide_meeting(meeting_id, accept, comment)` | наставник | ✔ | подтвердить/отклонить встречу |
| `messenger_list_chats` | все | — | каналы мессенджера и участие |
| `messenger_join_chats(codes)` | все | ✔ | вступить в каналы (по умолчанию — во все обязательные) |
| `mentor_interns_overview`, `mentor_intern_context(intern_id)` | наставник, руководитель | — | сводка по стажёрам, контекст для повестки 1:1 |
| `goals_list`, `goals_add(title, metric, kind)` | стажёр | —/✔ | трекер целей |
| `mentor_ask(question, attachment_id)` | стажёр | ✔ | эскалация наставнику (с вложением) |
| `mentor_answer(question_id, answer, publish_to_faq)` | наставник, руководитель | ✔ | ответ и публикация в FAQ |

## Модель статусов заявок

**Доступ:** `created → pending_approval → granted | rejected`, `pending_approval → cancelled`.
Согласующий: `policy` (автоматически, имитация задержки `ACCESS_AUTO_APPROVE_SECONDS`), `owner` (владелец проекта,
по умолчанию — руководитель команды). Стажёр получает те же права, что и участники команды: максимум «редактирование»,
администрирование — у владельцев проектов, prod-БД — только у дежурных SRE; стратегия и финпоказатели — по заявке с задачей.

**Отсутствие:** `created → pending_approval → approved | rejected`, `pending_approval → cancelled`.
Согласует руководитель, наставник получает уведомление. После согласования дни отсутствия исключаются из слотов встреч.

## Замена имитаций на реальные сервисы
Каждый инструмент вызывает модуль `app/services/<service>.py`. Для подключения реальной системы модуль заменяется
клиентом RPC/HTTP с тем же интерфейсом; события смены статусов приходят через Kafka-консьюмер вместо `access.sync()`.
