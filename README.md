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

### Сквозной сценарий «Сбой в нескольких продуктах»

Для отдельного прогона сгенерируйте временный файл и запустите сервер на другом порту. В PowerShell из корня репозитория:

```powershell
$demoOutageFile = Join-Path $env:TEMP "mts-outages-$PID.json"
.venv/Scripts/python scripts/make_demo.py --out $demoOutageFile
$env:INCIDENTS_FILE = $demoOutageFile
$env:TIME_MODE = 'real'
$env:LLM_PROVIDER = 'rules'
.venv/Scripts/python -m uvicorn backend.app.main:app --port 8010
```

В АРМ на `http://127.0.0.1:8010/` откройте вкладку «Сбои». У предложения должны быть три заявки из разных
продуктов на общем компоненте. Одновременная заявка с другим кодом остаётся отдельно. Подтвердите предложение
и откройте состав. На `http://127.0.0.1:8010/wall` должна появиться одна карточка сбоя, а четыре группы и их
счётчики продолжат учитывать отдельные инциденты. После перезапуска с тем же файлом подтверждение сохраняется.
Режим `rules` позволяет повторить сценарий без вызовов GigaChat. Для прогона с моделью используйте `gigachat`.

Тесты: `.venv/Scripts/python -m pytest backend/tests -q`

Документация: [CLAUDE.md](CLAUDE.md) · [docs/contract.md](docs/contract.md) · [docs/data-report.md](docs/data-report.md) · правила групп [config/rules.yaml](config/rules.yaml)
