# To‑Do SPA + Flask Chat Translator

Lightweight single-file Flask API (in-memory) with a small client-side Single Page App (`index.html`). The app demonstrates CRUD for tasks and a chat endpoint that translates natural language into function calls using an LLM.

Contents
- `app.py` — Flask backend (API + chat translator)
- `index.html` — Frontend SPA (pure HTML/CSS/JS)

Quick features
- Create, list, update, delete tasks via REST
- Chat endpoint (`/v1/chat`) that maps user text -> function call (`addTask`, `viewTasks`, `completeTask`, `deleteTask`) and executes it
- Responsive UI: task list and chat side-by-side on wide screens
- Local-friendly CORS so the SPA can be opened with `file://` or served separately
- In-memory storage (no DB). Numeric `short_id` is assigned and renumbered after deletions; primary resource id is a UUID.

Requirements
- Python 3.8+
- Recommended packages: `flask`, `python-dotenv`, optionally `openai` (if you want chat to call OpenAI)

Install (recommended)

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask python-dotenv
# If you want OpenAI chat: pip install openai
```

Environment
- Create a `.env` file in the project root (same folder as `app.py`) to provide credentials:

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini   # optional
```

Run the backend

```bash
# while virtualenv is active
python3 app.py
# Flask will listen by default on 127.0.0.1:5000
```

Open the frontend
- Development: open `index.html` in your browser (file:// works — the SPA will default to `http://127.0.0.1:5000` for API calls).
- Or serve the static file via a simple server:

```bash
python3 -m http.server 8000
# then open http://127.0.0.1:8000/index.html
```

API reference

- POST /v1/tasks
  - Create a task
  - Body JSON: `{ "title": "Buy milk" }`
  - Response: 201 with task JSON

- GET /v1/tasks
  - List tasks
  - Query parameters: `completed` (true/false), `sort`, `limit`, `offset`
  - Response: `{ items: [...], page: { limit, offset, total } }

- GET /v1/tasks/<task_id>
  - Get single task by UUID

- PATCH /v1/tasks/<task_id>
  - Toggle completion
  - Body JSON: `{ "completed": true }` (returns updated task)

- DELETE /v1/tasks/<task_id>
  - Delete task by UUID (server will renumber `short_id` values)

- POST /v1/chat
  - Translate a natural language message into a function call and execute it
  - Body JSON: `{ "message": "add buy milk" }`
  - Response JSON: `{ "tool_request": {...}, "result": ... }`
  - If OpenAI is NOT configured, server returns 501 with a helpful message

Notes & behavior
- Dual identifiers:
  - `id` — UUID used by REST endpoints (PATCH/DELETE)
  - `short_id` — small integer shown in the UI and used by the chat assistant
- In-memory: restarting the Flask process clears all tasks
- After deletions, `short_id` values are renumbered to keep them compact (1..N)

Troubleshooting
- "Could not add task" in the UI
  - Ensure backend is running on 127.0.0.1:5000
  - Check browser DevTools → Network / Console for the failing request and server response

- "Chat not configured"
  - Means `OPENAI_API_KEY` is not set or `openai` package not installed. Either set the key in `.env` and install the `openai` package, or use the chat-less UI.

Security & deployment
- This app is for demo/dev only: CORS is wide open and data is stored in memory.
- For production: use a real database, secure CORS (restrict origins), run behind a WSGI server (Gunicorn/uvicorn) and do not expose `debug=True`.

License
- MIT

Enjoy — if you want I can add a `requirements.txt`, simple Dockerfile, or example `curl` snippets for every endpoint.
