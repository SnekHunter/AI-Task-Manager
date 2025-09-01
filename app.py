"""
Flask To‑Do List API (single file, in‑memory list) + Chat Translator
-------------------------------------------------------------------
Implements five endpoints:
  1) POST   /v1/tasks               → create a task
  2) GET    /v1/tasks               → list tasks (with optional filters)
  3) PATCH  /v1/tasks/<id>          → mark a task complete/incomplete (idempotent)
  4) DELETE /v1/tasks/<id>          → delete a task
  5) POST   /v1/chat                → *LLM translator* that converts natural language
                                     into function calls and executes them

Notes
- Data lives in an in‑memory Python list (no persistence).
- Timestamps are RFC3339 UTC ("...Z").
- OPENAI_API_KEY is read from .env (see README snippet below).
- Chat endpoint uses a strict system prompt so the model returns a JSON function call.
"""
from __future__ import annotations

import os, re, json
from flask import Flask, request, jsonify, url_for, abort
from uuid import uuid4
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

# -------- OpenAI client (env-driven) ---------------------------------------
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # optional import guard for environments without the SDK

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL")

client = None
if OPENAI_API_KEY and OpenAI:
    client = OpenAI(api_key=OPENAI_API_KEY)

# The system prompt used by the /v1/chat endpoint
PROMPT6 = (
    # Robust System Prompt — To-Do Function Router
"""
You translate a single user message into one function call:
addTask(description: string), viewTasks(), completeTask(task_id: int), deleteTask(task_id: int), or deleteAll().

Output: return only a JSON object like
{"function":"<one>", "parameters":{...}} — no prose, no code fences.

Rules:

addTask when the user asks to add/create/make/“todo: …”. Description = quoted text if present, else the text after the verb (trimmed).

viewTasks for show/list requests or whenever required info is missing/ambiguous (e.g., no numeric ID).

completeTask only with an explicit numeric ID (e.g., task 3, #3, 3rd → 3).

deleteTask only with an explicit numeric ID (same parsing as above).

deleteAll only when the user explicitly asks to delete or clear ALL tasks (e.g., "delete all tasks", "clear my list"). Return deleteAll only if the user's intent is clearly to remove every task.

If multiple actions appear, pick the first clear action in reading order. Output exactly one call.

Examples
User: add 'buy milk'
You: {"function":"addTask","parameters":{"description":"buy milk"}}

User: show my tasks
You: {"function":"viewTasks","parameters":{}}

User: mark task #3 done
You: {"function":"completeTask","parameters":{"task_id":3}}

User: delete all my tasks
You: {"function":"deleteAll","parameters":{}}
"""
)

app = Flask(__name__)

# Allow simple CORS for the SPA (handles preflight OPTIONS and adds CORS headers).
# This avoids adding an external dependency (flask-cors) and allows the index.html
# (served from file:// or a different origin) to call the API during local dev.
@app.before_request
def _handle_options():
    # Return a short-circuit response for CORS preflight requests
    if request.method == 'OPTIONS':
        resp = app.make_response(('', 200))
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return resp

@app.after_request
def _set_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# In‑memory data store: a simple list of task dicts.
# Each task has: id (uuid), short_id (int), title, description, due_date, completed, created_at, updated_at
TASKS: List[Dict[str, Any]] = []
NEXT_SHORT_ID: int = 1  # incremental numeric id for chat functions


def utcnow() -> str:
    """Return current time as RFC3339 UTC with trailing 'Z'."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_bool(value: Optional[str]) -> Optional[bool]:
    """Parse truthy/falsey query param strings to bool (or None if unspecified)."""
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"1", "true", "t", "yes", "y"}:
        return True
    if value in {"0", "false", "f", "no", "n"}:
        return False
    return None


def find_task_index(task_id: str) -> int:
    """Return the list index of the task with the given UUID id, or -1 if not found."""
    for i, t in enumerate(TASKS):
        if t["id"] == task_id:
            return i
    return -1


def find_task_index_by_short(short_id: int) -> int:
    """Return the list index of the task with the given short_id (int), or -1."""
    for i, t in enumerate(TASKS):
        if t.get("short_id") == short_id:
            return i
    return -1


# ---- Error handling -------------------------------------------------------
@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(500)
def handle_error(err):
    """Return a consistent JSON error envelope for 400/404/500."""
    status = getattr(err, "code", 500)
    code = {400: "bad_request", 404: "not_found"}.get(status, "server_error")
    message = getattr(err, "description", str(err))
    return jsonify({"error": {"code": code, "message": message}}), status


# ---- Helpers to create/update tasks (reused by chat) ----------------------

def build_task(title: str, description: str = "", due_date: Optional[str] = None) -> Dict[str, Any]:
    global NEXT_SHORT_ID
    now = utcnow()
    task = {
        "id": str(uuid4()),
        "short_id": NEXT_SHORT_ID,
        "title": title.strip(),
        "description": description or "",
        "due_date": due_date,   # accept as string; deeper validation is out of scope here
        "completed": False,
        "created_at": now,
        "updated_at": now,
    }
    NEXT_SHORT_ID += 1
    return task


# ---- Create a task --------------------------------------------------------
@app.post("/v1/tasks")
def create_task():
    """Create a new task.
    Request JSON (title required; description/due_date optional):
      { "title": "Buy milk", "description": "2L whole milk", "due_date": "2025-09-02T09:00:00Z" }
    Response: 201 Created with full task JSON, Location header to the resource.
    """
    data = request.get_json(force=True, silent=True) or {}

    # --- Validation
    if not isinstance(data, dict):
        abort(400, description="Body must be a JSON object")

    title = data.get("title")
    if not isinstance(title, str) or not (1 <= len(title.strip()) <= 200):
        abort(400, description="title is required (1..200 chars)")

    description = data.get("description") or ""
    if not isinstance(description, str):
        abort(400, description="description must be a string")

    due_date = data.get("due_date")
    if due_date is not None and not isinstance(due_date, str):
        abort(400, description="due_date must be an RFC3339 string or omitted")

    # --- Build + persist
    task = build_task(title, description, due_date)
    TASKS.append(task)

    resp = jsonify(task)
    resp.status_code = 201
    # Provide Location for the newly created resource (single-item GET implemented below)
    resp.headers["Location"] = url_for("get_task", task_id=task['id'], _external=False)
    return resp


# ---- List tasks -----------------------------------------------------------
@app.get("/v1/tasks")
def get_tasks():
    """Return tasks with optional filtering, sorting, and basic pagination."""
    items = list(TASKS)  # shallow copy for manipulation

    # --- Filtering by completion state (if provided)
    completed = parse_bool(request.args.get("completed"))
    if completed is not None:
        items = [t for t in items if t.get("completed") is completed]

    # --- Sorting (default newest first)
    sort = request.args.get("sort", "-created_at")
    reverse = sort.startswith("-")
    key = sort.lstrip("-")
    if key not in {"created_at", "due_date"}:
        key = "created_at"
    items.sort(key=lambda t: t.get(key) or "", reverse=reverse)

    # --- Pagination
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        abort(400, description="limit/offset must be integers")

    total = len(items)
    items = items[offset : offset + limit]

    return jsonify({"items": items, "page": {"limit": limit, "offset": offset, "total": total}})


# ---- Get single task -----------------------------------------------------
@app.get("/v1/tasks/<task_id>")
def get_task(task_id: str):
    idx = find_task_index(task_id)
    if idx == -1:
        abort(404, description="task not found")
    return jsonify(TASKS[idx])


# ---- Mark task as complete/incomplete (idempotent) ------------------------
@app.patch("/v1/tasks/<task_id>")
def patch_task(task_id: str):
    """Toggle completion via UUID path (primary API). Body must include 'completed'."""
    idx = find_task_index(task_id)
    if idx == -1:
        abort(404, description="task not found")

    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict) or "completed" not in data:
        abort(400, description="Body must include 'completed': true|false")

    TASKS[idx]["completed"] = bool(data["completed"])  # coercion to bool
    TASKS[idx]["updated_at"] = utcnow()
    return jsonify(TASKS[idx])


# ---- Delete a task --------------------------------------------------------
@app.delete("/v1/tasks/<task_id>")
def delete_task(task_id: str):
    idx = find_task_index(task_id)
    if idx == -1:
        abort(404, description="task not found")
    TASKS.pop(idx)
    # Re-number remaining tasks so short_id values remain compact (1..N)
    renumber_short_ids()
    return ("", 204)


def renumber_short_ids() -> None:
    """Ensure short_id values are sequential (1..N) and reset NEXT_SHORT_ID accordingly.

    Call this after any operation that removes or reorders tasks so chat numeric IDs remain
    compact and predictable.
    """
    global NEXT_SHORT_ID
    for i, t in enumerate(TASKS, start=1):
        t["short_id"] = i
    NEXT_SHORT_ID = len(TASKS) + 1


# ---- Chat translator endpoint --------------------------------------------
@app.post("/v1/chat")
def chat_translate_and_execute():
    """Translate a natural-language command into a function call, then execute it.
    Request JSON: { "message": "string" }
    Response JSON: { "tool_request": {..}, "result": <varies> }

    The model is instructed (via PROMPT6) to output ONLY a JSON object like:
      {"function": "addTask", "parameters": {"description": "buy milk"}}
    """
    if client is None:
        # Service not implemented without OpenAI configured
        abort(501, description="OpenAI client not initialized. Set OPENAI_API_KEY and install openai.")

    data = request.get_json(force=True, silent=True) or {}
    user_text = (data.get("message") or "").strip()
    if not user_text:
        abort(400, description="'message' is required")

    # Call the model
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": PROMPT6},
                {"role": "user", "content": user_text},
            ],
            temperature=0,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        abort(500, description=f"OpenAI error: {e}")

    # Extract JSON (strip optional code fences)
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"```\s*$", "", text)
    try:
        tool_req = json.loads(text)
    except Exception:
        abort(500, description="Model did not return valid JSON.")
    
    # Basic schema checks for safety
    if not isinstance(tool_req, dict):
        abort(500, description="Model output is not a JSON object.")
    if "function" not in tool_req or not isinstance(tool_req.get("function"), str):
        abort(500, description="Model JSON missing 'function' string.")
    params = tool_req.get("parameters", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        abort(500, description="Model 'parameters' must be an object.")

    # Validate tool request
    func = tool_req.get("function")
    params = params
    if func not in {"addTask", "viewTasks", "completeTask", "deleteTask", "deleteAll"}:
        abort(400, description="Unsupported function from model.")

    # Dispatch
    assistant_message = None
    if func == "addTask":
        desc = params.get("description")
        if not isinstance(desc, str) or not desc.strip():
            abort(400, description="addTask.description must be a non-empty string")
        task = build_task(title=desc.strip())  # map 'description' → title for this demo
        TASKS.append(task)
        result = task
        assistant_message = f"Added task #{task['short_id']}: {task['title']}"

    elif func == "viewTasks":
        result = {"items": list(TASKS), "total": len(TASKS)}
        # Compose a short human-friendly summary (up to 6 items)
        if not TASKS:
            assistant_message = "You have no tasks."
        else:
            lines = []
            for t in TASKS[:6]:
                status = "✓" if t.get("completed") else "·"
                lines.append(f"#{t.get('short_id')} {t.get('title')} {status}")
            more = "" if len(TASKS) <= 6 else f"\n...and {len(TASKS)-6} more tasks"
            assistant_message = "\n".join(lines) + more

    elif func == "completeTask":
        try:
            short_id = int(params.get("task_id"))
        except Exception:
            abort(400, description="completeTask.task_id must be an integer")
        idx = find_task_index_by_short(short_id)
        if idx == -1:
            abort(404, description="task not found (by short_id)")
        TASKS[idx]["completed"] = True
        TASKS[idx]["updated_at"] = utcnow()
        result = TASKS[idx]
        assistant_message = f"Marked task #{short_id} as completed."

    elif func == "deleteTask":
        try:
            short_id = int(params.get("task_id"))
        except Exception:
            abort(400, description="deleteTask.task_id must be an integer")
        idx = find_task_index_by_short(short_id)
        if idx == -1:
            abort(404, description="task not found (by short_id)")
        removed = TASKS.pop(idx)
        # Keep short_id values compact after removal
        renumber_short_ids()
        result = {"deleted": True, "short_id": removed.get("short_id")} 
        assistant_message = f"Deleted task #{removed.get('short_id')} ({removed.get('title')})."

    elif func == "deleteAll":
        # destructive operation — be explicit
        count = len(TASKS)
        TASKS.clear()
        renumber_short_ids()
        result = {"deleted": True, "count": count}
        assistant_message = f"Deleted all tasks ({count} removed)."

    # Return tool_request + result and a human-friendly assistant message to simplify frontend UX
    resp_payload = {"tool_request": tool_req, "result": result}
    if assistant_message is not None:
        resp_payload["assistant_message"] = assistant_message

    return jsonify(resp_payload)


# New: bulk-delete endpoint (DELETE /v1/tasks) with an explicit confirm query param
@app.delete("/v1/tasks")
def delete_all_tasks():
    """Delete all tasks. To avoid accidental mass-deletion require ?confirm=true.

    Returns JSON {"deleted": <count>} with HTTP 200 on success. If ?confirm is not set to
    a truthy value the request is rejected with 400 and a helpful message.
    """
    confirm = (request.args.get("confirm") or "").strip().lower()
    if confirm not in {"1", "true", "yes", "y"}:
        abort(400, description="To delete all tasks include ?confirm=true in the request")
    count = len(TASKS)
    TASKS.clear()
    renumber_short_ids()
    return jsonify({"deleted": count})


if __name__ == "__main__":
    # Debug is ON for developer convenience. Disable in production.
    app.run(debug=True)
