# Дашборд координатора массовых инцидентов · хакатон МТС

## Запуск (Windows, Git Bash / PowerShell)
```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env
.venv/Scripts/python -m uvicorn backend.app.main:app --port 8000 --reload
```
- **http://127.0.0.1:8000/wall** — видеостена (общий экран, только просмотр)
- **http://127.0.0.1:8000/** — АРМ координатора: представиться → «Начать смену»; «Принял», «Взять в работу»,
  «Передать в команду», статусы, эскалация, комментарии, импорт/экспорт файла
- **http://127.0.0.1:8000/itsm/&lt;id&gt;** — мини-ITSM: назначение группы, статус, приоритет, срок, журнал работ

Правки `data/incidents.json` руками и действия из АРМ/ITSM видны на всех экранах за ~1 с.
Чтобы открыть АРМ с других компьютеров, запускайте сервер с `--host 0.0.0.0` и заходите по IP ноутбука.
Node/npm не нужны: фронт без сборки.

Демо-сценарий (запасной вариант ручной правки):
```bash
.venv/Scripts/python scripts/demo.py list     # шаги
.venv/Scripts/python scripts/demo.py step 1   # высокий → критический
.venv/Scripts/python scripts/demo.py reset    # вернуть оригинал
.venv/Scripts/python scripts/demo.py reset demo   # свой демо-файл со свежими датами
```

Тесты: `.venv/Scripts/python -m pytest backend/tests -q`

Документация: [CLAUDE.md](CLAUDE.md) · [docs/contract.md](docs/contract.md) · [docs/data-report.md](docs/data-report.md) · правила групп [config/rules.yaml](config/rules.yaml)
