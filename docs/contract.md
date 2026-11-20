# Контракт backend ↔ frontend

Меняет сессия бэкенда. Живой пример: `docs/sample-snapshot.json` (ответ на демо-файле 5 000 инцидентов).
**Изменения 20.11** (разбор, `docs/decisions.md`): лёгкий снимок + серверный поиск, «Взять в работу» в файле
вместо «Принял»/claims, конфликт правок (409), история изменений, порядок колонок. Отмечены ⚑.

## Как фронту получать данные
Опрос `GET /api/snapshot` раз в 1 с с заголовком `If-None-Match: <ETag из прошлого ответа>`.
Ничего не поменялось → `304` без тела. Поменялось → `200` с полным снимком и новым `ETag`.
⚑ Ответ сжат gzip (браузер распаковывает сам), сериализуется один раз на версию для всех экранов.
Правка файла видна на сервере за ≤0,5 с (опрос mtime+size раз в 0,25 с; частые правки склеиваются, не чаще раза в 0,5 с),
пересчёт на 5 000 инцидентов ~0,15–0,6 с → на экране ≤1–1,5 с. Действия из АРМ/ITSM применяются сразу (без опроса).

## `GET /api/snapshot`
```jsonc
{
  "version": 7,                       // растёт при любом изменении
  "server_time": "2026-11-20T14:00:12Z", // реальное время сервера на момент сборки этой версии снимка
  "now": "2026-11-11T12:30:40Z",      // «сейчас» дашборда — от него считать «сколько осталось»
  "clock_offset_ms": -784771000,      // now - server_time; таймер на фронте: Date.now() + clock_offset_ms
  "time_mode": "real",                // export | real (на демо — real)
  "export_anchor": "2026-11-11T12:30:36Z",
  "file": {
    "status": "ok",                   // ok | corrupt | missing → плашка «файл повреждён, данные на <last_ok_at>»
    "error": null,
    "last_ok_at": "...", "last_change_at": "...", "count": 5000,
    "warnings": ["пропущено записей без id: 2", "..."],  // ⚑ первой строкой — сводка «пропущено N»
    "path": "..."
  },
  "groups": [                         // порядок = порядок колонок; ids уже отсортированы (см. «Порядок в колонках»)
    {"key": "critical", "title": "Критические", "ids": ["INC..."], "count": 4},
    {"key": "coordinators", "title": "На координаторах", "ids": [...], "count": 7},
    {"key": "high", "title": "Высокий приоритет", "ids": [...], "count": 46},
    {"key": "research", "title": "Исследование ситуации", "ids": [...], "count": 26}
  ],
  "counts": {"critical": 3, ...},     // без «решённых, но ещё висящих» — для счётчиков в шапке
  "incidents_scope": "visible",       // ⚑ в incidents НЕ все инциденты, а только видимые:
  "incidents": {"INC...": Incident},  // ⚑ из групп + взятые в работу (активные, для «Мои») + из очереди actions
  "total_incidents": 5000,            // ⚑ всего в файле
  "active_total": 767,                // ⚑ активных всего
  "outside_active": 713,              // ⚑ «ещё N активных вне групп» (средние/низкие) — для шапки стены и АРМ
  "products": ["Зеркальце", ...],     // ⚑ для фильтра «продукт»
  "teams": ["КЦ Ступа", ...],         // для фильтра «команда» и выбора команды при передаче
  "actions": [Action, ...],           // очередь «Что делать сейчас», уже отсортирована
  "alerts": [Alert, ...],             // ⚑ см. «Тревоги и „Взять в работу“»
  "claims": {"INC…": {"by": "Ольга", "at": "…"}},  // ⚑ теперь из поля coordinator в файле (оставлено для совместимости)
  "coordinators": ["Ольга", …],       // кто действовал на смене (подсказка имени)
  "ai_status": {...},
  "boot_id": "a1b2c3d4",              // меняется при перезапуске сервера → сбросить счётчик событий
  "events": [Event, ...],             // последние 50, старые → новые
  "last_event_seq": 4
}
```
Инцидентов вне `incidents` фронт не знает — их показывает поиск `GET /api/search` (по всем инцидентам).
Если открытая панель ссылается на инцидент, которого нет в снимке, — взять его через `GET /api/incidents/{id}`.

### Порядок в колонках ⚑ (decisions.md, п. 3)
- critical, high: просроченные → по оставшемуся времени → без срока; решённые («висят 30 мин») внизу;
- coordinators: **дольше ждёт у координаторов — выше** (по `assigned_to_team_at`, иначе `created_at`);
- research: новые сверху (по `resolved_at`, иначе `created_at`).

## Incident
```jsonc
{
  "id": "INC000030000001", "sys_id": "...",
  "title": "...",                     // header
  "short_description": "...", "description": "...",
  "solution": null,                   // только у решённых
  "product": "Ковёр-самолёт", "product_code": "BI_101", "service": "...", "scenario": "...",
  "state_id": 2, "state": "Выполняется",
  "state_kind": "active",             // active | resolved | closed | research | unknown
  "state_reason": "Нет",
  "priority_id": 3, "priority": "Высокий",
  "priority_level": "high",           // critical | high | medium | low | unknown
  "impact_id": 3, "impact": "Высокое", "urgency_id": 4, "urgency": "Наивысшая",
  "service_type": "Клиентский",
  "team": "КЦ Ступа",                 // исполнитель-команда
  "is_coordinators": false,
  "assignee": "Баба Яга (babayaga)", "assignee_login": "babayaga",   // null → «сотрудник не назначен»
  "initiator": "...", "channel": "...",
  "created_at": "2026-11-10T06:32:14Z",   // «время возникновения»
  "deadline": "2026-11-10T12:32:14Z",     // может быть null → «срок не задан»
  "resolved_at": null,
  "assigned_to_team_at": "...",
  "is_active": true,
  "is_master": false, "is_double": false, "is_reason": false, "is_effect": false, "is_need_up": null,
  "itsm_url": "/itsm/INC000030000001",
  "coordinator": {"name": "Ольга", "at": "2026-11-20T14:05:00Z"},   // ⚑ «в работе у Ольги»; null — никто не взял
  "work_log": [{"at", "author", "action", "text",
                "kind"?, "before"?, "after"?}],  // ⚑ action: take|release|assign|state|priority|deadline|comment|escalate
  "hash": "a1b2c3d4e5f6",             // меняется при любой правке записи; ⚑ передавать как expected_hash в действиях
  "groups": ["critical", "coordinators"], // во всех, где показывать; «также в: …» = groups минус текущая
  "resolved_in": ["critical"],        // в этих группах карточка «решён» (зелёная), а не активная
  "forecast": {                       // только у активных со сроком, иначе null
    "progress": 0.83,                 // доля истёкшего срока (может быть > 1)
    "level": "breach",                // ok | risk (≥75%) | breach (прогноз: не успеют) | overdue
    "eta": "...", "median_minutes": 155, "basis": "медиана по 4 решённым (приоритет)"
  },
  "ai": { ... }                       // форму поля ai описывает сессия ИИ (backend/app/ai/__init__.py)
}
```
Оставшееся время: `deadline - (Date.now() + clock_offset_ms)`. Отрицательное → «просрочен на …».

## Поиск и фильтры по всем инцидентам ⚑ — `GET /api/search`
Параметры (все необязательные; списки — через запятую):

| Параметр | Что |
|---|---|
| `q` | по номеру, продукту, команде, сотруднику, координатору, тексту (заголовок, описания, сервис, сценарий); слова — все должны встретиться; без учёта регистра |
| `status` | `all` (по умолчанию) · `active` · `done` (решённые/закрытые/исследование) |
| `product`, `team` | точное совпадение, можно несколько |
| `priority` | id через запятую: `4,3` |
| `coordinator` | «в работе у X» (без учёта регистра); «мои» = своё имя |
| `group` | `critical` / `coordinators` / `high` / `research` |
| `overdue` | только просроченные (`forecast.level = overdue`) |
| `burning` | горит срок: прогноз «не успеют» или просрочен |
| `untaken` | непринятые тревоги: в «критических»/«на координаторах» и никто не взял |
| `unassigned` | активные без сотрудника-исполнителя |
| `risk_high` | высокий риск ИИ (`ai.risk.level = high`) |
| `duplicates` | дубли и массовые (`is_master` / `is_double` / `ai.duplicates`) |
| `service_type` | `client` (Клиентский) · `internal` (остальные) |
| `new_hour` | созданы за последний час |
| `limit`, `offset` | страница (по умолчанию 100, максимум 500) |

Ответ: `{"total": 340, "items": [Incident, ...], "offset": 0, "limit": 100, "now": "..."}` → «12 из 340».
Порядок: точное совпадение номера первым, активные выше решённых, выше приоритет, меньше осталось времени, новее.
На 5 000 инцидентов — ~20 мс.

## Тревоги и «Взять в работу» ⚑ (decisions.md, п. 4, 7)
Кнопки «Принял» нет, `POST /api/alerts/ack` **удалён**. Тревогу снимает «Взять в работу» — оно пишется в файл
полем `coordinator` (и в `work_log`), поэтому видно на стене и всех АРМ и переживает перезапуск.
```jsonc
Alert = {"key": "critical:INC…", "group": "critical" | "coordinators", "incident_id": "INC…",
         "since": "…",                 // начало эпизода (вошёл в группу); повторный вход — новый эпизод
         "taken_by": "Ольга" | null, "taken_at": "…" | null,   // из поля coordinator инцидента
         "acked_by": …, "acked_at": …} // = taken_by/taken_at, оставлено для старого фронта
```
Тревога «не взята», пока `taken_by == null`. Отпустили, а инцидент всё ещё в группе — снова не взята.
«Уже в работе у кого-то и стал критическим → громкая тревога только у владельца» — фронт сравнивает `taken_by` со своим именем.

## Action
```jsonc
{
  "id": "route:INC...", "kind": "route",   // route — передать от координаторов; escalate — эскалировать горящий срок
  "incident_id": "INC...", "title": "Передать в «КЦ Ступа»", "reason": "...",
  "target_team": "КЦ Ступа", "confidence": 0.45,
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
  "by": "Ольга",                      // автор, если правка из АРМ/ITSM или с записью в workLog
  "at": "...реальное время...", "clock_at": "...время дашборда..."
}
```
Типы: `new, removed, became_critical, critical_resolved, to_coordinators, from_coordinators,
priority_changed, deadline_changed, team_changed, assignee_changed, status_changed, resolved, reopened,
comment, escalated,` ⚑ `taken, released`. При старте сервера и при подмене файла целиком событий нет (базовая точка).

## Остальное
- `GET /api/incidents/{id}` — Incident + `raw` (исходная запись из файла) + ⚑ `history` (история изменений, см. ниже)
  для панели деталей. Работает для любого инцидента, не только из снимка.
- `GET /api/events?after=<seq>` — события после seq.
- `GET /api/health` — статус файла, версия, ⚑ `last_apply_seconds` (сколько занял последний пересчёт).

### История изменений ⚑ (`history` в `/api/incidents/{id}`; для ИИ — `backend/app/history.py`)
```jsonc
[{"at": "…", "kind": "priority" | "team" | "assignee" | "deadline" | "state" | "reopen",
  "before": "Средний", "after": "Высокий", "by": "Ольга" | null,
  "up": true,                          // priority
  "before_id": 2, "after_id": 3,       // priority, state
  "later": true, "shift_min": 120}]    // deadline
```
Старые → новые. Сервер копит сам (и правки жюри, и действия АРМ), хранит в `cache/history.jsonl`,
при первом появлении инцидента засевает из `work_log` с полями `kind/before/after`.

## Действия (пишут в файл инцидентов; ответ — обновлённый Incident)
Во **всех** правках можно передать ⚑ `expected_hash` — `hash` инцидента, который видел координатор.
Если запись в файле с тех пор изменилась, правка **не сохраняется**, ответ `409`:
```jsonc
{"detail": "инцидент только что изменили — обновите карточку, правка не сохранена", "conflict": true,
 "hash": "<текущий>", "changes": [{"field": "priority", "label": "приоритет", "before": "Высокий",
                                   "after": "Наивысший", "text": "приоритет: Высокий → Наивысший"}, …]}
```
→ «Инцидент только что изменили: … Обновить?». После своего действия брать новый `hash` из ответа.

| Метод | Тело | Что делает |
|---|---|---|
| ⚑ `POST /api/incidents/{id}/claim` | `{by, expected_hash?, force?}` | «Взять в работу»: `coordinator` в файл + журнал; тревога снята у всех. Уже у другого → `409 {"detail", "coordinator"}`; `force: true` — забрать себе. Решённый → 400 |
| ⚑ `DELETE /api/incidents/{id}/claim?by=Имя&expected_hash=…` | — | «Отпустить»: снять `coordinator` + журнал |
| `POST /api/itsm/{id}/assign` | `{team, assignee?, comment?, by, expected_hash?}` | передать в команду (Выполняется → Назначен); ⚑ снимает `coordinator` |
| `POST /api/itsm/{id}/state` | `{state_id, reason?, comment?, by, expected_hash?}` | смена статуса по `transitions` |
| `POST /api/itsm/{id}/priority` | `{priority_id, recalc_deadline?, comment?, by, expected_hash?}` | приоритет (+ срок по SLA) |
| `POST /api/itsm/{id}/deadline` | `{deadline (ISO UTC), comment?, by, expected_hash?}` | срок |
| ⚑ `POST /api/duplicates/confirm` | `{master_id, ids, by}` | «Да, это один сбой»: `isMaster` у мастера, `isDouble` у остальных + журнал (`action: duplicates`), одной записью. Ответ `{ok, incidents: [Incident…]}` |
| `POST /api/itsm/{id}/comment` | `{text, by}` | комментарий в журнал |
| `POST /api/itsm/{id}/escalate` | `{comment?, by}` | эскалация (в журнал) |
| `GET /api/meta` | — | статусы, переходы, приоритеты, SLA, команды |
| `GET /api/export` / `POST /api/import` | — / JSON-файл | скачать / заменить файл (старый → `cache/backups`) |

Ошибки: 400 (нельзя по правилам), 404, 409 (конфликт, уже в работе у другого, файл сейчас битый/занят — правка не сохранена).
⚑ Перед каждой записью — копия файла в `cache/backups` (храним 20, `BACKUPS_KEEP`).

## Справочники для ИИ ⚑ (договорённость бэк ↔ ИИ)
Store на каждую правку строит `backend/app/refs.Refs` одним проходом и вызывает
`ai.enrich(incidents, now, rules, refs=refs)`, если у `enrich` есть параметр `refs` (иначе — старую сигнатуру).
ИИ берёт из refs только `refs.history.get(id)` (O(1), записи только читать). Когда GigaChat дописал ответы
(`ai.consume_dirty()`), Store вызывает только `ai.enrich` и пересобирает очередь действий — прогноз не пересчитывается.
`refs.changed` / `refs.removed` — id изменённых по `hash`; `refs.median_resolution(team, prio)`; `refs.history` — история
выше (формат совместим с `ai/dynamics.History.get()`). Справочник «кому передать / всплеск» — у ИИ (`ai/index.py`).
Замер: `python scripts/bench.py` (5 000 и 10 000), автотест `backend/tests/test_speed.py`.
