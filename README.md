# Дашборд координатора массовых инцидентов · хакатон МТС

## Запуск (Windows, Git Bash / PowerShell)
```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env
.venv/Scripts/python -m uvicorn backend.app.main:app --port 8000 --reload
```
Открыть http://127.0.0.1:8000/api/snapshot. Править `data/incidents.json` — изменения подхватываются за ~0,5 с.

Тесты: `.venv/Scripts/python -m pytest backend/tests -q`

Документация: [CLAUDE.md](CLAUDE.md) · [docs/contract.md](docs/contract.md) · [docs/data-report.md](docs/data-report.md) · правила групп [config/rules.yaml](config/rules.yaml)
