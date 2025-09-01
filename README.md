
---

# AI Task Manager — SPA + Flask + Chat Translator

Single-file Flask API (in-memory) + a minimal HTML/CSS/JS SPA. Manage to-dos through REST **and** via a natural-language chat endpoint that converts user text into function calls.

## TL;DR (Quick Start)

```bash
# 1) Setup
python3 -m venv venv
source venv/bin/activate
pip install flask python-dotenv            # + openai if you want chat: pip install openai

# 2) Optional: enable chat
echo "OPENAI_API_KEY=sk-REPLACE-ME" > .env
# optionally: echo "OPENAI_MODEL=gpt-4o-mini" >> .env

# 3) Run backend
python app.py     # -> http://127.0.0.1:5000

# 4) Open frontend
# EITHER: double-click index.html (file:// OK in dev)
# OR: python3 -m http.server 8000 && open http://127.0.0.1:8000/index.html
```

## Features

* CRUD tasks via REST (`/v1/tasks…`)
* Natural-language **chat** → function call → executed (`/v1/chat`)
* **short\_id** numbers (`#1`, `#2`, …) for easy chat references; UUIDs for API
* Works from `file://` or any static host; wide-open CORS in dev
* No DB; state resets on restart

## Project Structure

```
.
├─ app.py          # Flask API + chat translator
├─ index.html      # Single-page UI (tasks + chat panel)
└─ static/         # (optional assets)
```

## Configuration (.env)

```dotenv
OPENAI_API_KEY=sk-...      # required only if using /v1/chat with OpenAI
OPENAI_MODEL=gpt-4o-mini   # optional; defaults may vary
```

If no `OPENAI_API_KEY` is set (or `openai` isn’t installed), `/v1/chat` returns a helpful error and the SPA continues to function with REST only. ([GitHub][1])

---

## REST API

Base URL: `http://127.0.0.1:5000`

### Create task

**POST** `/v1/tasks`
Body:

```json
{ "title": "Buy milk" }
```

Response: `201 Created` + full task JSON (includes `id` (UUID) and `short_id`).

### List tasks

**GET** `/v1/tasks`
Query: `completed=true|false`, `sort`, `limit`, `offset`
Response:

```json
{ "items": [ { /* task */ } ], "page": { "limit": 50, "offset": 0, "total": 1 } }
```

### Get a task

**GET** `/v1/tasks/<id>`
Return a single task by UUID.

### Toggle complete

**PATCH** `/v1/tasks/<id>`
Body:

```json
{ "completed": true }
```

Response: updated task.

### Delete task

**DELETE** `/v1/tasks/<id>`
Response: `204 No Content`.

### Delete all (safety switch)

**DELETE** `/v1/tasks?confirm=true`
Response:

```json
{ "deleted": 3 }
```

> Notes
> • Primary identifier is `id` (UUID). The UI/Chat uses `short_id` integers for convenience.
> • After deletions, `short_id` values may be re-numbered (compact 1..N). ([GitHub][1])

---

## Chat Endpoint

**POST** `/v1/chat`
Body:

```json
{ "message": "add buy milk" }
```

**Response**:

```json
{
  "tool_request": { "function": "addTask", "parameters": { "description": "buy milk" } },
  "result": { /* object or summary of the action */ },
  "assistant_message": "Added: buy milk (#1)"
}
```

Behavior:

* The system prompt forces the model to return **one JSON function call**:

  * `addTask(description: string)`
  * `viewTasks()`
  * `completeTask(task_id: int)`
  * `deleteTask(task_id: int)`
  * (Optionally) `deleteAll()` if present in your version
* Server **executes** the requested action and returns a concise `assistant_message` when possible. ([GitHub][1])

### Example `curl`

```bash
# Add
curl -sX POST http://127.0.0.1:5000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"add buy milk"}' | jq

# View
curl -sX POST http://127.0.0.1:5000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"show my tasks"}' | jq

# Complete #1
curl -sX POST http://127.0.0.1:5000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"complete task 1"}' | jq

# Delete all (if implemented)
curl -s "http://127.0.0.1:5000/v1/tasks?confirm=true" -X DELETE | jq
```

---

## Frontend (index.html)

* **Composer**: add new tasks
* **Filters**: All / Active / Completed
* **Search**: client-side text filter
* **Actions**: complete, delete, clear completed
* **Chat panel**: send natural language; shows the raw tool call and a readable confirmation
* On wide screens, tasks and chat can render side-by-side (responsive). ([GitHub][1])

---

## Troubleshooting

* **UI shows “Could not add task”**
  Ensure backend is running on `127.0.0.1:5000`. Check DevTools → Network tab for errors. ([GitHub][1])

* **Chat says not configured**
  Add `OPENAI_API_KEY` to `.env` and `pip install openai`, then restart the server. ([GitHub][1])

* **CORS / Mixed content**
  In dev, the API enables permissive CORS to allow `file://` or a separate static host.

---

## Security & Deployment

* Dev/demo only: in-memory data, wide-open CORS, `debug=True`
* For production:

  * Use a real DB (SQLite/Postgres)
  * Lock CORS to trusted origins
  * Run behind Gunicorn/Uvicorn + a reverse proxy
  * Hide secrets in env/secret manager, not `.env` committed to git ([GitHub][1])

## License

MIT

---