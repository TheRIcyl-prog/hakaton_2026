# Mass Incident Coordinator Dashboard

The MVP gives a shift one shared incident list, individual coordinator workstations, and a read-only wall. Everything in the MVP works with `LLM_PROVIDER=off`.

## Run the MVP on Windows

From the repository root in a VS Code PowerShell terminal, install dependencies once:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Prepare the demo account in the same PowerShell terminal. Enter and repeat the password at the hidden prompts. Keep it with the presenter. The command stores only its verifier in the server environment.

```powershell
$demoVerifier = .\.venv\Scripts\python.exe -m scripts.hash_demo_password
if ($LASTEXITCODE -ne 0) { throw 'Demo password setup failed' }
$demoAccount = @{
    id = 'demo-1'
    login = 'demo.coordinator'
    display_name = 'Демо-координатор'
    password_hash = $demoVerifier
}
$env:DEMO_ACCOUNTS = ConvertTo-Json -InputObject @($demoAccount) -Compress
```

Generate a fresh synthetic demo file for rehearsal. Reuse the file between runs to keep the rehearsal changes.

```powershell
.\.venv\Scripts\python.exe scripts/make_demo.py --count 5000 --out cache/mvp-demo/incidents.json
```

Select the file and run the server. These shell variables also take precedence over an existing local `.env`.

```powershell
$env:INCIDENTS_FILE = 'cache/mvp-demo/incidents.json'
$env:CACHE_DIR = 'cache/mvp-demo/state'
$env:BACKUPS_DIR = 'cache/mvp-demo/backups'
$env:TIME_MODE = 'real'
$env:POLL_INTERVAL = '0.25'
$env:LLM_PROVIDER = 'off'
$env:ITSM_URL_TEMPLATE = '/itsm/{id}'
$env:DEMO_ORIGINS = 'http://127.0.0.1:8000,http://localhost:8000'
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --port 8000
```

- [Coordinator workstation](http://127.0.0.1:8000/)
- [Read-only wall](http://127.0.0.1:8000/wall)
- Local mini-ITSM opens from the incident panel.

The server keeps sessions in memory. They expire two hours after sign-in by default (`DEMO_SESSION_SECONDS=7200`). Logout or any server restart ends the session. Run one worker. Missing or invalid account configuration is reported in the server log and the login endpoint returns `503` for validly formed requests from a trusted origin.

For two workstations, generate a second password verifier and build `$demoAccount2` with its own `id`, `login`, `display_name` and `password_hash`. Set `$env:DEMO_ACCOUNTS = ConvertTo-Json -InputObject @($demoAccount, $demoAccount2) -Compress` and sign in from separate browser profiles or computers. Logins and display names must be unique ignoring case and surrounding spaces. To admit another computer on a trusted demo network, start with `--host 0.0.0.0`, add the server's exact LAN origin, such as `http://192.168.1.20:8000`, to `DEMO_ORIGINS` and use that URL. Add the corresponding `https://…` origin when serving via HTTPS. The session cookie is marked Secure on HTTPS.

## Rehearse individual tabs

On Windows, open [defense-demo.cmd](scripts/defense-demo.cmd) to choose an isolated scenario. Each run opens the corresponding screen and waits for Enter before applying each event. See the [scenario list](docs/defense-scenarios.md#отдельные-сценарии-по-вкладкам) for the direct launch commands.

## Run with AI

Set `GIGACHAT_CREDENTIALS` in the local `.env` and use `$env:LLM_PROVIDER = 'gigachat'` in the terminal before launching the server. The value in the terminal takes precedence over `.env`. The selected GigaChat account needs access to the configured `GIGACHAT_MODEL` and, if set, `GIGACHAT_MODEL_ADVANCED`.

The incident panel shows rule-based suggestions immediately and adds model output asynchronously. Check `/api/health` for the provider, last successful response, and retry status. If the machine cannot reach GigaChat, the same workflow works with `$env:LLM_PROVIDER = 'rules'`, which keeps AI analysis local.

## Workflow and data ownership

| Step | Behavior |
|---|---|
| Ingest | The server reads one JSON export, normalizes statuses and priorities, and refreshes a shared snapshot after changes. A partially written or invalid file leaves the last valid snapshot available. |
| Find work | Groups use configured priority, team, and status rules. Search covers the full export. The personal queue shows unclaimed tasks and tasks owned by the signed-in coordinator. Critical claims, handoffs, and outage reviews appear first. Deadline reminders and past cases can be expanded from the queue. |
| Coordinate | Taking a task records a coordinator on the incident. A colleague sees the owner and can explicitly take over. Releasing an active incident makes it available again. The coordinator owns the handoff; the assigned team owns technical work. |
| Transfer and update | Assignment to another team records the team and optional engineer, clears coordinator ownership, and writes the journal. The panel can change status via the allowed transitions, with a reason for terminal and investigation states. The mini-ITSM also edits priority and deadline. Escalation is recorded locally in the journal. |
| Review outages | An explicit cause link or at least three active incidents from at least two products sharing a code within six hours can produce a proposal with AI off. The coordinator can remove unrelated members before confirmation and correct a confirmed group later. |
| Keep screens in sync | The workstation, wall, and mini-ITSM read the same server state. Successful writes update the file, journal, and snapshot. The wall stays read-only. Completed incidents remain visible in groups for 30 minutes and closed incidents for up to 10 seconds. Search still finds them. |

The included mini-ITSM and workstation share the session, local JSON file and API. An external `ITSM_URL_TEMPLATE` can link to records in another ITSM using `{id}` or `{sysId}`. SimpleOne import, corporate SSO, and write-back are separate integrations. The wall and its public snapshot use synthetic demo data.

## Presentation check

1. Open the wall in a clean browser profile. In another profile, open the workstation and sign in as `demo.coordinator`.
2. Search for `INC000030000002`. Open it and change its status from «Назначен» to «Выполняется». The wall updates from the same demo file.
3. Open the incident in mini-ITSM. Verify the status and «Демо-координатор» in the journal. Try its direct link in a clean browser profile to verify the login return.
4. Use «Выйти» in the profile menu. The workstation returns to the login form and the wall keeps updating.
5. With a second account, take a critical incident in the first profile. Check «Мои» and the personal queue in both profiles.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

See [API contract](docs/contract.md) and [project decisions](docs/decisions.md).
