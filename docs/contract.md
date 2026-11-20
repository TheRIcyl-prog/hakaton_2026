# Контракт backend ↔ frontend

Меняет только A. Живой пример: `docs/sample-snapshot.json` (это реальный ответ на исходных данных).

## Как фронту получать данные
Опрос `GET /api/snapshot` раз в 1 с с заголовком `If-None-Match: <ETag из прошлого ответа>`.
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
  "actions": [Action, ...],           // очередь «Что делать сейчас», уже отсортирована
  "ai_status": {...},                 // состояние ИИ (GigaChat / правила / нет связи) — см. раздел «ИИ»
  "boot_id": "a1b2c3d4",              // меняется при перезапуске сервера → сбросить счётчик событий
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
  "forecast": {                       // только у активных со сроком, иначе null
    "progress": 0.83,                 // доля истёкшего срока (может быть > 1)
    "level": "breach",                // ok | risk (≥75%) | breach (прогноз: не успеют) | overdue
    "eta": "...", "median_minutes": 155, "basis": "медиана по 4 решённым (приоритет)"
  },
  "ai": { ... }                        // ИИ: см. раздел «ИИ (поле ai)» ниже
}
```
Оставшееся время: `deadline - (Date.now() + clock_offset_ms)`. Отрицательное → «просрочен на …».

## ИИ (поле `ai` у Incident, `ai_status` в снимке)
Владелец — сессия ИИ (`backend/app/ai/`). Правила считают всё мгновенно; GigaChat в фоне дописывает слова, снимок его
никогда не ждёт. Числа (уверенность, баллы риска) — всегда из правил и истории, не со слов модели.
**Метка «ИИ» и кнопка «почему?»** — у каждой функции есть `source` (`rules` | `gigachat` | `cache` — ответ GigaChat из
кэша прошлого запуска) и `at` (реальное время ответа, ISO UTC).

Инциденты **на экране** (в группах, кроме «решён», или отмеченные динамикой) получают полный объект.
Остальные — компактный: только непустые поля, без `summary_meta`, `breakdown` и подробностей дублей.
Отсутствующее поле = `null`.

```jsonc
"ai": {
  "source": "rules",                 // rules | gigachat | cache — есть ли в карточке слова GigaChat
  "pending": false,                  // GigaChat ещё думает над этим инцидентом (можно показать «…»)

  // 8.1 Выжимка — показывать вместо длинной сути (оригинал — в панели)
  "summary": "Абоненты не могут войти в Зеркальце: SMS-код не приходит",   // ≤ 90 знаков
  "summary_meta": {"source": "gigachat", "at": "2026-11-21T09:12:03Z"},

  // 8.2 Кому передать — только у «на координаторах» (активные/исследование), иначе null
  "route": {
    "team": "КЦ Ступа",
    "confidence": 0.8,                       // доля похожих, которые вела эта команда (по истории!)
    "confidence_text": "8 из 10 похожих",    // так и писать на карточке: «ИИ: передать в «КЦ Ступа», 8 из 10 похожих»
    "basis": "по сервису «[Зеркальце] Вход»",// по какому признаку искали похожие (сервис → сценарий → продукт)
    "reason": "8 из 10 похожих по сервису «…» вела эта команда, решила 7",  // от GigaChat — его 1–2 предложения
    "alternatives": [{"team": "КЦ Леший", "score": 0.2, "confidence_text": "2 из 10 похожих"}],  // 2 запасных
    "candidates": [{"team": "...", "count": 8, "solved": 7, "score": 0.8}],  // все 5 кандидатов — для «почему?»
    "similar_ids": ["INC..."],
    "similar": [{"id": "INC...", "title": "...", "team": "...", "state": "Завершен", "url": "/itsm/INC..."}],
    "rules_team": "КЦ Ступа",                // только если GigaChat выбрал не первого кандидата
    "source": "gigachat", "at": "..."
  },

  // 8.3 Риск стать критическим — у активных «Высоких»; у средних/низких — только при тревожной динамике
  "risk": {
    "score": 76, "level": "high",            // high ≥ 70 | medium ≥ 40 | low
    "factors": ["срок нарушен", "затронуты клиенты", "нарастает: …"],
    "breakdown": [{"label": "приоритет «Высокий»", "points": 30}, {"label": "срок нарушен", "points": 20}],  // «почему?»
    "comment": "Срок сорван, клиенты ждут — проверьте массовость",  // GigaChat, только топ-20 по баллу, иначе null
    "comment_meta": {"source": "gigachat", "at": "..."},
    "source": "rules", "at": "..."
  },

  // 8.5 Тревожная динамика — у любых активных (включая средние/низкие), иначе null
  "dynamics": {
    "flag": true, "label": "нарастает",
    "text": "Средний → Высокий → Наивысший за 40 мин",   // показывать рядом с «нарастает»
    "patterns": [{"kind": "priority_up", "count": 2, "text": "..."}],
                // kind: priority_up (≥2 повышений за 2 ч) | reassign (≥3 переназначений за 4 ч)
                //       | deadline_moved (срок переносили ≥2 раз) | reopened (вернули в работу после «Завершен»)
    "since": "...",                          // первое изменение в истории
    "events": [{"at": "...", "kind": "priority", "before": "Средний", "after": "Высокий", "up": true}],
                // kind: priority | team | deadline (later, shift_min) | reopen | state — «история изменений» для панели
    "comment": "Приоритет растёт — похоже на начало массового сбоя",  // GigaChat, одна фраза; иначе null
    "source": "gigachat", "at": "..."
  },

  // 8.4 Дубли — у каждого участника группы, иначе null
  "duplicates": {
    "group_id": "dup-INC001",               // свернуть группу на стене в одну карточку по этому ключу
    "master_id": "INC001", "is_master": true,
    "ids": ["INC001", "INC002"],            // до 20, мастер первым; всего — size
    "size": 6,
    "name": "Зеркальце: не доставляются сообщения",   // от GigaChat; без него — продукт + заголовок мастера
    "reason": "6 похожих инцидентов по «[Зеркальце] Сообщения» за 40 мин",
    "confirmed": false,                     // true — в файле уже isMaster/isDouble (координатор подтвердил)
    "similarity": 0.62,
    "source": "rules", "at": "..."
  }
}
```

`ai_status` в снимке (и в `/api/health` → `ai`):
```jsonc
{"provider": "gigachat",            // rules — GigaChat выключен (LLM_PROVIDER=rules)
 "state": "ok",                     // ok | starting | offline (нет связи/лимит — работают правила, повтор раз в минуту) | off
 "label": "ИИ: нет связи с GigaChat, работают правила",   // готовая подпись для шапки/плашки
 "error": null, "queue": 12, "in_flight": 2, "done": 140, "cached": 480,
 "last_ok_at": "...", "next_retry_at": "..."}
```

Нужно от бэкенда (ещё не сделано):
- `POST /api/duplicates/confirm` `{"master_id": "...", "ids": [...], "by": "Ольга"}` → в файл `isMaster: true` у мастера,
  `isDouble: true` у остальных + запись в `workLog`; после перечитывания `duplicates.confirmed = true`.
  Группы берутся из `ai.duplicate_groups()`.
- История изменений для панели — можно брать из `ai.history(id)` (ИИ копит её сам в `cache/ai_history.json`).

## Action
```jsonc
{
  "id": "route:INC...", "kind": "route",   // route — передать от координаторов; escalate — эскалировать горящий срок
  "incident_id": "INC...", "title": "Передать в «КЦ ДОИ VMWare»", "reason": "...",
  "target_team": "КЦ ДОИ VMWare", "confidence": 0.45,
  "draft": "Коллеги, передаём вам…"        // готовый текст передачи/эскалации для копирования
}
```
Порядок: критические первыми, внутри — по оставшемуся времени.

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


## Смена: общие тревоги и «в работе» (с ноября: стена + АРМ)
В снимке:
```jsonc
"alerts": [{"key": "critical:INC…", "group": "critical", "incident_id": "INC…",
            "since": "…", "acked_by": "Ольга Новикова" | null, "acked_at": "…" | null}],  // критические первыми
"claims": {"INC…": {"by": "Ольга Новикова", "at": "…"}},
"coordinators": ["Ольга Новикова", …],   // кто выходил на смену
"teams": ["КЦ ДОИ VMWare", …]            // для выбора команды при передаче
```
Incident: `work_log: [{at, author, action: assign|state|priority|deadline|comment|escalate, text}]`.
Event: `by` — автор правки (если правка пришла из АРМ/ITSM), новые типы `comment`, `escalated`.

## Действия (пишут в файл инцидентов; ответ — обновлённый Incident)
| Метод | Тело | Что делает |
|---|---|---|
| `POST /api/alerts/ack` | `{key, by}` | «Принял» — баннер пропадает на всех экранах |
| `POST /api/incidents/{id}/claim` / `DELETE` | `{by}` | взять в работу / отпустить |
| `POST /api/itsm/{id}/assign` | `{team, assignee?, comment?, by}` | передать в команду (Выполняется → Назначен) |
| `POST /api/itsm/{id}/state` | `{state_id, reason?, comment?, by}` | смена статуса по `transitions` |
| `POST /api/itsm/{id}/priority` | `{priority_id, recalc_deadline?, comment?, by}` | приоритет (+ срок по SLA) |
| `POST /api/itsm/{id}/deadline` | `{deadline (ISO UTC), comment?, by}` | срок |
| `POST /api/itsm/{id}/comment` | `{text, by}` | комментарий в журнал |
| `POST /api/itsm/{id}/escalate` | `{comment?, by}` | эскалация (в журнал) |
| `GET /api/meta` | — | статусы, переходы, приоритеты, SLA, команды |
| `GET /api/export` / `POST /api/import` | — / JSON-файл | скачать / заменить файл (старый → `cache/backups`) |
Ошибки: 400 (нельзя по правилам), 404, 409 (файл сейчас битый/занят — правка не сохранена).
