# Контракт backend ↔ frontend

Меняет только A. Живой пример: `docs/sample-snapshot.json` (это реальный ответ на исходных данных).

## Как фронту получать данные
Опрос `GET /api/snapshot` раз в 1 с с заголовком `If-None-Match: "<version>"`.
Ничего не поменялось → `304` без тела. Поменялось → `200` с полным снимком и новым `ETag`.
Правка файла видна на сервере за ~0,5 с → на экране ≤1,5 с.

## `GET /api/snapshot`
```jsonc
{
  "version": 7,                       // растёт при любом изменении
  "server_time": "2026-11-20T14:00:12Z", // реальное время сервера
  "now": "2026-11-11T12:30:40Z",      // «сейчас» дашборда — от него считать «сколько осталось»
  "clock_offset_ms": -784771000,      // now - server_time; таймер на фронте: Date.now() + clock_offset_ms
  "time_mode": "export",              // export | real
  "export_anchor": "2026-11-11T12:30:36Z",
  "file": {
    "status": "ok",                   // ok | corrupt | missing → показать плашку «файл повреждён, данные на <last_ok_at>»
    "error": null,
    "last_ok_at": "...", "last_change_at": "...", "count": 44, "warnings": [], "path": "..."
  },
  "groups": [                         // порядок = порядок колонок
    {"key": "critical", "title": "Критические", "ids": ["INC..."], "count": 0},
    {"key": "coordinators", "title": "На координаторах", "ids": [...], "count": 2},
    {"key": "high", "title": "Высокий приоритет", "ids": [...], "count": 1},
    {"key": "research", "title": "Исследование ситуации", "ids": [...], "count": 5}
  ],                                  // ids уже отсортированы: просроченные и срочные сверху, решённые внизу
  "counts": {"critical": 0, ...},     // без «решённых, но ещё висящих» — для счётчиков в шапке
  "incidents": {"INC...": Incident},  // ВСЕ инциденты из файла (для поиска/фильтров), не только из групп
  "events": [Event, ...],             // последние 50, старые → новые
  "last_event_seq": 4
}
```

## Incident
```jsonc
{
  "id": "INC000021938167", "sys_id": "...",
  "title": "...",                     // header
  "short_description": "...", "description": "...",
  "solution": null,                   // только у решённых
  "product": "Летающий ковер", "product_code": "BI_41", "service": "...", "scenario": "...",
  "state_id": 2, "state": "Выполняется",
  "state_kind": "active",             // active | resolved | closed | research | unknown
  "state_reason": "Нет",
  "priority_id": 3, "priority": "Высокий",
  "priority_level": "high",           // critical | high | medium | low | unknown
  "impact_id": 3, "impact": "Высокое", "urgency_id": 4, "urgency": "Наивысшая",
  "service_type": "Клиентский",
  "team": "ОГЭ Приморье",             // исполнитель-команда
  "is_coordinators": false,
  "assignee": "Винни Пух (winnie)", "assignee_login": "winnie",
  "initiator": "...", "channel": "...",
  "created_at": "2026-11-10T06:32:14Z",   // «время возникновения»
  "deadline": "2026-11-10T12:32:14Z",     // может быть null → «срок —»
  "resolved_at": null,
  "assigned_to_team_at": "...",
  "is_active": true,
  "is_master": false, "is_double": false, "is_reason": false, "is_effect": false, "is_need_up": null,
  "itsm_url": "https://itsm.example.mts.ru/incident/INC000021938167",
  "hash": "a1b2c3d4e5f6",             // меняется при любой правке записи
  "groups": ["critical", "coordinators"], // во всех, где показывать; «также в: …» = groups минус текущая
  "resolved_in": ["critical"],        // в этих группах карточка «решён» (зелёная), а не активная
  "ai": {
    "source": "rules",                // rules | gigachat | cache
    "summary": "Короткая выжимка",
    "route": {                        // только у is_coordinators, иначе null
      "team": "КЦ ДОИ VMWare", "confidence": 0.42,
      "reason": "...", "alternatives": [{"team": "...", "score": 0.3}], "similar_ids": ["INC..."]
    },
    "risk": {"score": 72, "level": "high", "factors": ["срок нарушен", "..."]}  // у активных некритических
  }
}
```
Оставшееся время: `deadline - (Date.now() + clock_offset_ms)`. Отрицательное → «просрочен на …».

## Event
```jsonc
{
  "seq": 4, "type": "became_critical",
  "incident_id": "INC...", "title": "...", "product": "...",
  "before": "Высокий", "after": "Наивысший",
  "alert": "critical",                // critical | coordinators | critical_resolved | null
  "text": "стал критическим: Высокий → Наивысший",
  "at": "...реальное время...", "clock_at": "...время дашборда..."
}
```
Типы: `new, removed, became_critical, critical_resolved, to_coordinators, from_coordinators,
priority_changed, deadline_changed, team_changed, assignee_changed, status_changed, resolved, reopened`.
При старте сервера событий нет (это базовая точка).

## Как делать тревоги (рекомендация)
Баннер строить **по состоянию**, а не только по событиям: каждый инцидент в группе `critical`
(не в `resolved_in`) и в `coordinators`, которого координатор ещё не «принял», — тревога.
«Принял» хранить в localStorage по ключу `ack:<group>:<id>`; при повторном входе в группу
(событие `became_critical` / `to_coordinators` с новым seq) ключ сбрасывать.
Так тревога не теряется при перезагрузке страницы. События — для ленты, звука и подсветки «было → стало».

## Остальное
- `GET /api/incidents/{id}` — Incident + `raw` (исходная запись из файла) для панели деталей.
- `GET /api/events?after=<seq>` — события после seq.
- `GET /api/health` — статус файла и версия.
