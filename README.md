# Mass Incident Coordinator Dashboard

The MVP gives a shift one shared incident list, individual coordinator workstations, and a read-only wall. Everything in the MVP works with `LLM_PROVIDER=off`.

## Run the MVP on Windows

From the repository root in a VS Code PowerShell terminal, install dependencies once:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Generate a fresh synthetic demo file when needed. Reuse the same file between runs to keep demo actions.

```powershell
.\.venv\Scripts\python.exe scripts/make_demo.py --count 5000 --out cache/mvp-demo/incidents.json
```

Select the file and run the server. These shell variables also take precedence over an existing local `.env`.

```powershell
$env:INCIDENTS_FILE = 'cache/mvp-demo/incidents.json'
$env:TIME_MODE = 'real'
$env:LLM_PROVIDER = 'off'
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --port 8000
```

- [Coordinator workstation](http://127.0.0.1:8000/)
- [Read-only wall](http://127.0.0.1:8000/wall)
- Local mini-ITSM opens from the incident panel.

For two workstations, use separate browser profiles or two computers connected to the same server. To admit another computer on a trusted demo network, start with `--host 0.0.0.0` and use the server computer's local address.

## Run with AI

Set `GIGACHAT_CREDENTIALS` in the local `.env` and use `$env:LLM_PROVIDER = 'gigachat'` in the terminal before launching the server. The value in the terminal takes precedence over `.env`. The selected GigaChat account needs access to the configured `GIGACHAT_MODEL` and, if set, `GIGACHAT_MODEL_ADVANCED`.

The incident panel shows rule-based suggestions immediately and adds model output asynchronously. Check `/api/health` for the provider, last successful response, and retry status. If the machine cannot reach GigaChat, the same workflow works with `$env:LLM_PROVIDER = 'rules'`, which keeps AI analysis local.

## Workflow and data ownership

| Step | Behavior |
|---|---|
| Ingest | The server reads one JSON export, normalizes statuses and priorities, and refreshes a shared snapshot after changes. A partially written or invalid file leaves the last valid snapshot available. |
| Find work | Groups use configured priority, team, and status rules. Search covers the full export. The personal queue shows unclaimed tasks and tasks owned by the selected coordinator. Critical claims, handoffs, and outage reviews appear first. Deadline reminders and past cases can be expanded from the queue. |
| Coordinate | Taking a task records a coordinator on the incident. A colleague sees the owner and can explicitly take over. Releasing an active incident makes it available again. The coordinator owns the handoff; the assigned team owns technical work. |
| Transfer and update | Assignment to another team records the team and optional engineer, clears coordinator ownership, and writes the journal. The panel can change status via the allowed transitions, with a reason for terminal and investigation states. The mini-ITSM also edits priority and deadline. Escalation is recorded locally in the journal. |
| Review outages | An explicit cause link or at least three active incidents from at least two products sharing a code within six hours can produce a proposal with AI off. The coordinator can remove unrelated members before confirmation and correct a confirmed group later. |
| Keep screens in sync | The workstation, wall, and mini-ITSM read the same server state. Successful writes update the file, journal, and snapshot. The wall stays read-only. Completed incidents remain visible for 30 minutes; closed incidents for 5 minutes. Search still finds them. |

The included mini-ITSM and workstation share a local JSON file and API. An external `ITSM_URL_TEMPLATE` can link to records in another ITSM using `{id}` or `{sysId}`. SimpleOne import, authentication, and write-back are separate integrations. The demo uses self-entered coordinator names, a single server process, and a trusted network.

## Presentation check

1. Open the wall and two coordinator profiles. Enter different coordinator names.
2. Find a critical or coordinator-queue incident, take it in one profile, and show the owner in the other. The other profile's personal queue no longer offers the task.
3. Transfer it to a team. Open the journal and verify the team, action, and author in the local mini-ITSM.
4. Change its status from the workstation. Show the history and the update on the shared wall if the incident belongs to a wall group.
5. Review a proposed outage, remove a member, and confirm the remaining eligible set. Open its composition on the workstation.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

See [API contract](docs/contract.md) and [project decisions](docs/decisions.md).
