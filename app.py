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

import os
import re
import json
from flask import Flask, request, jsonify, url_for, abort, send_from_directory
from uuid import uuid4
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
import threading

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
    """
You translate a single user message into one function call:
addTask(description: string), viewTasks(), completeTask(task_id: int), deleteTask(task_id: int), or deleteAll().

Alternatively, for brief in-app conversational replies (greeting, short confirmations, or simple clarification)
return a JSON object with a single string field "assistant_message" instead of a function, for example:
{"assistant_message":"Hello! I can help manage your tasks."
}

Important rules:
- If the user asks anything outside the Task Manager domain (e.g., general knowledge like "Where is the USA located?"), do NOT attempt to answer with facts. Instead reply with a short assistant_message that politely declines and redirects to task-related help (e.g., "I can only help with managing tasks here.").
- For task actions, return only a JSON object like
  {"function":"<one>", "parameters":{...}}  — no prose, no code fences, no extra fields.
- For chit-chat allowed inside the app (greeting, thanks, short clarifications), return exactly:
  {"assistant_message": "<short reply>"}
  — keep the reply brief (<= 140 chars).

Rules for mapping language → functions:
- addTask when the user asks to add/create/make/“todo: …”. Description = quoted text if present, else the text after the verb (trimmed).
- viewTasks for show/list requests or whenever required info is missing/ambiguous (e.g., no numeric ID).
- completeTask only with an explicit numeric ID (e.g., task 3, #3, 3rd → 3).
- deleteTask only with an explicit numeric ID (same parsing as above).
- deleteAll only when the user explicitly asks to delete or clear ALL tasks (e.g., "delete all tasks", "clear my list"). Return deleteAll only if the user's intent is clearly to remove every task.

If multiple actions appear, pick the first clear action in reading order. Output exactly one call or a single assistant_message.

Examples
User: add 'buy milk'
You: {"function":"addTask","parameters":{"description":"buy milk"}}

User: show my tasks
You: {"function":"viewTasks","parameters":{}}

User: hi
You: {"assistant_message":"Hi — I can help with your tasks. Try: 'add buy milk'."}

User: where is the USA located?
You: {"assistant_message":"Sorry, I can only help with tasks in this app."}
"""
)

try:
    from flask_cors import CORS as _CORS
except Exception:
    _CORS = None

# Use Flask static handling: serve files from ./static at /ai-task-manager/static
app = Flask(__name__, static_folder='static', static_url_path='/ai-task-manager/static')
# Enable flask-cors if installed; otherwise rely on existing manual headers
if _CORS:
    _CORS(app)

# Serve the SPA index at root and /ai-task-manager
@app.route('/')
def index():
    base = os.path.dirname(__file__)
    return send_from_directory(base, 'index.html')

@app.route('/ai-task-manager')
@app.route('/ai-task-manager/')
def ai_index():
    base = os.path.dirname(__file__)
    return send_from_directory(base, 'index.html')

# Allow simple CORS for the SPA (handles preflight OPTIONS and adds CORS headers).
# Force CORS headers on every response (even when flask-cors is installed) to
# ensure browser clients never get blocked by missing headers on error paths.
@app.before_request
def _handle_options():
    if request.method == 'OPTIONS':
        resp = app.make_response(('', 200))
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return resp

@app.after_request
def _ensure_cors_headers(response):
    # Unconditionally set the common CORS headers so even exception handlers
    # and responses wrapped by flask-cors cannot omit them.
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# In-memory data store
TASKS: List[Dict[str, Any]] = []
NEXT_SHORT_ID: int = 1
# Reentrant lock to protect TASKS / NEXT_SHORT_ID across request threads
TASKS_LOCK = threading.RLock()

# --- Server-side undo buffer (ephemeral, in-memory) ---
UNDO_BUFFERS: Dict[str, Dict[str, Any]] = {}
UNDO_EXPIRY_SECONDS = 60 * 5  # 5 minutes expiry

def snapshot_undo_buffer(items: List[Dict[str, Any]]) -> str:
    """Store a deep-copy snapshot of items and return a token."""
    token = str(uuid4())
    UNDO_BUFFERS[token] = {'created': datetime.now(timezone.utc).timestamp(), 'items': json.loads(json.dumps(items))}
    return token

def cleanup_undo_buffers() -> None:
    nowt = datetime.now(timezone.utc).timestamp()
    expired = [k for k,v in list(UNDO_BUFFERS.items()) if nowt - v.get('created',0) > UNDO_EXPIRY_SECONDS]
    for k in expired:
        UNDO_BUFFERS.pop(k, None)

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"1", "true", "t", "yes", "y"}:
        return True
    if value in {"0", "false", "f", "no", "n"}:
        return False
    return None


def find_task_index(task_id: str) -> int:
    for i, t in enumerate(TASKS):
        if t["id"] == task_id:
            return i
    return -1


def find_task_index_by_short(short_id: int) -> int:
    for i, t in enumerate(TASKS):
        if t.get("short_id") == short_id:
            return i
    return -1


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(500)
def handle_error(err):
    status = getattr(err, "code", 500)
    code = {400: "bad_request", 404: "not_found"}.get(status, "server_error")
    message = getattr(err, "description", str(err))
    resp = jsonify({"error": {"code": code, "message": message}})
    resp.status_code = status
    # Ensure CORS headers are present on error responses (prevents browser blocking)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return resp


# Helpers

def build_task(title: str, description: str = "", due_date: Optional[str] = None) -> Dict[str, Any]:
    global NEXT_SHORT_ID
    # Protect NEXT_SHORT_ID allocation across concurrent requests
    with TASKS_LOCK:
        now = utcnow()
        task = {
            "id": str(uuid4()),
            "short_id": NEXT_SHORT_ID,
            "title": title.strip(),
            "description": description or "",
            "due_date": due_date,
            "completed": False,
            "created_at": now,
            "updated_at": now,
        }
        NEXT_SHORT_ID += 1
        return task


def renumber_short_ids() -> None:
    """Assign short_id = 1..N based on created_at (oldest first).
    Uses TASKS_LOCK to make the operation atomic if called externally.
    """
    global NEXT_SHORT_ID
    with TASKS_LOCK:
        TASKS.sort(key=lambda t: t.get('created_at') or '')
        for i, t in enumerate(TASKS, start=1):
            t['short_id'] = i
        NEXT_SHORT_ID = len(TASKS) + 1


@app.post("/v1/tasks")
def create_task():
    data = request.get_json(force=True, silent=True) or {}
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
    task = build_task(title, description, due_date)
    with TASKS_LOCK:
        TASKS.append(task)
    resp = jsonify(task)
    resp.status_code = 201
    resp.headers["Location"] = url_for("get_task", task_id=task['id'], _external=False)
    return resp


@app.get("/v1/tasks")
def get_tasks():
    items = list(TASKS)
    completed = parse_bool(request.args.get("completed"))
    if completed is not None:
        items = [t for t in items if t.get("completed") is completed]

    # --- Sorting (default by numeric short_id ascending so UI shows 1..N)
    sort = request.args.get("sort", "short_id")
    reverse = sort.startswith("-")
    key = sort.lstrip("-")
    if key not in {"created_at", "due_date", "short_id", "id"}:
        key = "short_id"

    # Use numeric sort for short_id, string sort for id/timestamps
    if key == "short_id":
        items.sort(key=lambda t: int(t.get("short_id") or 0), reverse=reverse)
    else:
        items.sort(key=lambda t: (t.get(key) or ""), reverse=reverse)

    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        abort(400, description="limit/offset must be integers")
    total = len(items)
    items = items[offset: offset + limit]
    return jsonify({"items": items, "page": {"limit": limit, "offset": offset, "total": total}})


@app.get("/v1/tasks/<task_id>")
def get_task(task_id: str):
    idx = find_task_index(task_id)
    if idx == -1:
        abort(404, description="task not found")
    return jsonify(TASKS[idx])


@app.patch("/v1/tasks/<task_id>")
def patch_task(task_id: str):
    idx = find_task_index(task_id)
    if idx == -1:
        abort(404, description="task not found")
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict) or "completed" not in data:
        abort(400, description="Body must include 'completed': true|false")
    TASKS[idx]["completed"] = bool(data["completed"])
    TASKS[idx]["updated_at"] = utcnow()
    return jsonify(TASKS[idx])


@app.delete("/v1/tasks/<task_id>")
def delete_task(task_id: str):
    idx = find_task_index(task_id)
    if idx == -1:
        abort(404, description="task not found")
    with TASKS_LOCK:
        removed = TASKS.pop(idx)
        renumber_short_ids()
        # snapshot removed item so client can request server-side restore
        token = snapshot_undo_buffer([removed])
    return jsonify({'deleted': 1, 'short_id': removed.get('short_id'), 'undo_token': token})


@app.delete("/v1/tasks")
def delete_all_tasks():
    """Delete all tasks. Requires ?confirm=true to delete all tasks"""
    confirm = parse_bool(request.args.get("confirm"))
    if not confirm:
        abort(400, description="Missing confirm=true to delete all tasks")
    with TASKS_LOCK:
        deleted = len(TASKS)
        if deleted == 0:
            return jsonify({"deleted": 0})
        before = list(TASKS)
        TASKS.clear()
        renumber_short_ids()
        token = snapshot_undo_buffer(before)
    return jsonify({"deleted": deleted, 'undo_token': token})


@app.post('/v1/undo/restore')
def undo_restore():
    """Restore deleted items either by providing { token: <token> } (created by delete calls)
    or by posting { items: [...] } containing full task objects. Restored items will
    preserve their provided fields where possible. Returns the restored items.
    """
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('token')
    items = data.get('items')
    # garbage collect expired buffers
    cleanup_undo_buffers()
    if token:
        buf = UNDO_BUFFERS.pop(token, None)
        if not buf:
            return jsonify({'error': 'token not found or expired'}), 404
        items = buf.get('items', [])
    if not items:
        return jsonify({'error': 'no items to restore'}), 400
    restored = []
    with TASKS_LOCK:
        existing_ids = {t['id'] for t in TASKS}
        for it in items:
            # ensure unique id
            if not it.get('id') or it.get('id') in existing_ids:
                it['id'] = str(uuid4())
            # defensive defaults
            if not it.get('created_at'):
                it['created_at'] = utcnow()
            TASKS.append({
                'id': it['id'],
                'short_id': it.get('short_id'),
                'title': it.get('title') or it.get('description') or '',
                'description': it.get('description',''),
                'due_date': it.get('due_date'),
                'completed': bool(it.get('completed', False)),
                'created_at': it.get('created_at'),
                'updated_at': utcnow(),
            })
        renumber_short_ids()
        restored = TASKS[-len(items):]
    return jsonify({'restored': len(restored), 'items': restored})


@app.post("/v1/chat")
def chat_translate_and_execute():
    data = request.get_json(force=True, silent=True) or {}
    user_text = (data.get("message") or "").strip()
    if not user_text:
        abort(400, description="'message' is required")

    # --- Lightweight local parsing for common intents
    # Multi-add: prefer comma-separated lists; only split on 'and' when verbs repeat
    m_multi = re.match(r'^(?P<verb>add|buy|create|todo)\b[\s:\-]*(.+)$', user_text, flags=re.I)
    if m_multi:
        leading_verb = m_multi.group('verb')
        body = m_multi.group(2)
        parts = []
        # If commas present, use them as separators
        if ',' in body:
            parts = [p.strip() for p in body.split(',') if p.strip()]
        else:
            # count verb occurrences to safely decide whether 'and' separates items
            verbs = re.findall(r'\b(?:buy|add|get|purchase|grab)\b', body, flags=re.I)
            if len(verbs) > 1 and re.search(r'\band\b', body, flags=re.I):
                # Use inline (?i) flag to make the split case-insensitive
                parts = re.split(r'(?i)\s*(?:and)\s*', body)
            else:
                parts = [body]

        added = []
        for p in parts:
            # Only remove a leading verb in the part if it matches the original verb the user used.
            # This preserves descriptions like 'buy milk' when the user said 'add buy milk'.
            if re.match(fr'^\s*{leading_verb}\b', p, flags=re.I):
                item = re.sub(fr'^(?:{leading_verb})\b\s*', '', p, flags=re.I).strip()
            else:
                # keep the part verb if it differs from the command verb
                item = p.strip()
            item = item.strip(' "\'')
            if not item:
                continue
            task = build_task(title=item)
            with TASKS_LOCK:
                TASKS.append(task)
            added.append(task)
        if added:
            renumber_short_ids()
            resp_payload = {
                "tool_request": {"function": "addTask", "parameters": {"description": user_text}},
                "result": {"added": added},
                "assistant_message": f"Added {len(added)} tasks."
            }
            return jsonify(resp_payload)
    # Remove-by-name: "remove milk" or "delete milk"
    m_remove = re.match(r'^(?:remove|delete|clear)\b[\s:\-]*(.+)$', user_text, flags=re.I)
    if m_remove:
        name = m_remove.group(1).strip()

        # If the user explicitly asked to delete all tasks, perform the operation
        # and return an undo token. This avoids treating 'all' as a literal task name.
        if re.search(r'\b(all|every|everything)\b', name, flags=re.I):
            with TASKS_LOCK:
                deleted = len(TASKS)
                if deleted == 0:
                    return jsonify({"assistant_message": "No tasks to delete."})
                before = list(TASKS)
                TASKS.clear()
                renumber_short_ids()
                token = snapshot_undo_buffer(before)
            return jsonify({
                "tool_request": {"function": "deleteAll", "parameters": {}},
                "result": {"deleted": deleted},
                "undo_token": token,
                "assistant_message": f"Deleted {deleted} task(s)."
            })

        # If the user replied with a plain number like "4" or "#4" (allow trailing punctuation), treat it as a short_id
        # Require the entire name to be a numeric token (optionally with '#' and punctuation) to avoid accidental matches
        if re.match(r'^\s*#?\s*\d+\s*[.!?]?\s*$', name):
            m_num = re.search(r'(\d+)', name)
        else:
            m_num = None
        if m_num:
            sid = int(m_num.group(1))
            idx = find_task_index_by_short(sid)
            if idx == -1:
                return jsonify({"assistant_message": f"No task found with number {sid}."})
            with TASKS_LOCK:
                deleted = TASKS.pop(idx)
                renumber_short_ids()
                token = snapshot_undo_buffer([deleted])
            resp_payload = {
                "tool_request": {"function": "deleteTask", "parameters": {"task_id": sid}},
                "result": {"deleted": deleted, "short_id": deleted.get('short_id')},
                "undo_token": token,
                "assistant_message": f"Removed task '{deleted.get('title')}'."
            }
            return jsonify(resp_payload)

        # collect all matching tasks (case-insensitive substring match in title or description)
        matches = [t for t in list(TASKS) if name.lower() in (t.get('title','') + ' ' + t.get('description','')).lower()]
        if not matches:
            return jsonify({"assistant_message": f"No tasks found matching '{name}'."})
        if len(matches) == 1:
            # single exact/unique match — delete it
            to_del = matches[0]
            with TASKS_LOCK:
                # locate by id and remove
                for i, t in enumerate(TASKS):
                    if t['id'] == to_del['id']:
                        deleted = TASKS.pop(i)
                        renumber_short_ids()
                        token = snapshot_undo_buffer([deleted])
                        resp_payload = {
                            "tool_request": {"function": "deleteTask", "parameters": {"task_id": deleted.get("short_id")}},
                            "result": {"deleted": deleted, "short_id": deleted.get('short_id')},
                            "undo_token": token,
                            "assistant_message": f"Removed task '{deleted.get('title')}'."
                        }
                        return jsonify(resp_payload)
            # fallthrough if something odd happened
            return jsonify({"assistant_message": "Could not delete the task."}), 500
        # multiple matches -> ask user to clarify which one to delete
        options = [{"short_id": m.get('short_id'), "title": m.get('title')} for m in matches]
        # present numbered choices so the user can answer e.g. 'delete 3' or 'mark 3 as complete'
        return jsonify({
            "assistant_message": f"I found multiple tasks matching '{name}'. Which one should I delete? Reply with the task number (e.g. 'delete 3').",
            "choices": options
        })

    # --- Complete-by commands: numbers, ranges, 'all', or name-based with disambiguation
    # Examples handled: "complete 2,3,4", "mark all as complete", "mark 1 as complete", "mark milk as complete"
    m_complete = re.match(r'^(?:complete|mark|set)\b[\s:\-]*(.+?)\s*(?:as\s+complete|completed|done)?$', user_text, flags=re.I)
    if m_complete:
        target = m_complete.group(1).strip()
        # 'all' shortcut
        if re.search(r'\ball\b', target, flags=re.I):
            updated = []
            with TASKS_LOCK:
                for t in TASKS:
                    if not t.get('completed'):
                        t['completed'] = True
                        t['updated_at'] = utcnow()
                        updated.append(t)
            if not updated:
                return jsonify({"assistant_message": "All tasks are already marked complete."})
            return jsonify({
                "tool_request": {"function": "completeTask", "parameters": {"task_ids": "all"}},
                "result": {"updated": updated},
                "assistant_message": f"Marked {len(updated)} task(s) as complete." 
            })

        # parse numeric IDs and ranges (e.g. '2,3,5' or '2-4')
        ids = set()
        # expand ranges like 2-4
        for a,b in re.findall(r'(\d+)\s*-\s*(\d+)', target):
            try:
                a_i = int(a); b_i = int(b)
                if a_i <= b_i:
                    for n in range(a_i, b_i+1):
                        ids.add(n)
            except Exception:
                pass
        # individual digits
        for n in re.findall(r'\b(\d+)\b', target):
            try:
                ids.add(int(n))
            except Exception:
                pass
        if ids:
            updated = []
            not_found = []
            with TASKS_LOCK:
                for sid in sorted(ids):
                    idx = find_task_index_by_short(sid)
                    if idx == -1:
                        not_found.append(sid)
                        continue
                    TASKS[idx]['completed'] = True
                    TASKS[idx]['updated_at'] = utcnow()
                    updated.append(TASKS[idx])
            msg_parts = []
            if updated:
                msg_parts.append(f"Marked {len(updated)} task(s) as complete")
            if not_found:
                msg_parts.append(f"Could not find tasks: {', '.join(map(str, not_found))}")
            return jsonify({
                "tool_request": {"function": "completeTask", "parameters": {"task_ids": sorted(list(ids))}},
                "result": {"updated": updated, "not_found": not_found},
                "assistant_message": "; ".join(msg_parts) if msg_parts else "No changes made."
            })

        # otherwise treat target as a name/phrase and search
        name = target
        matches = [t for t in list(TASKS) if name.lower() in (t.get('title','') + ' ' + t.get('description','')).lower()]
        if not matches:
            return jsonify({"assistant_message": f"No tasks found matching '{name}'."})
        if len(matches) == 1:
            # single match -> mark it complete
            with TASKS_LOCK:
                for i, t in enumerate(TASKS):
                    if t['id'] == matches[0]['id']:
                        TASKS[i]['completed'] = True
                        TASKS[i]['updated_at'] = utcnow()
                        return jsonify({
                            "tool_request": {"function": "completeTask", "parameters": {"task_id": TASKS[i].get('short_id')}},
                            "result": {"updated": TASKS[i]},
                            "assistant_message": f"Marked '{TASKS[i].get('title')}' as complete."
                        })
            return jsonify({"assistant_message": "Could not update the task."}), 500
        # multiple matches -> ask which one to mark
        options = [{"short_id": m.get('short_id'), "title": m.get('title')} for m in matches]
        return jsonify({
            "assistant_message": f"I found multiple tasks matching '{name}'. Which one should I mark as complete? Reply with the task number (e.g. 'mark 3 as complete').",
            "choices": options
        })

    # Fallback: if no local parser matched, attempt to use the configured OpenAI translator
    # or return a short assistant_message directing the user to task commands.
    if client:
        try:
            # Call the OpenAI chat completion translator (SDK may vary; wrap defensively)
            resp = client.chat.completions.create(
                model=MODEL or "gpt-4o-mini",
                messages=[{"role": "system", "content": PROMPT6}, {"role": "user", "content": user_text}],
                max_tokens=400,
            )
            # Extract assistant content (SDK shape may vary); try common access patterns
            content = None
            try:
                # new-style: resp.choices[0].message.content
                content = resp.choices[0].message.content
            except Exception:
                try:
                    content = resp.choices[0]['message']['content']
                except Exception:
                    content = None
            if content:
                try:
                    parsed = json.loads(content)
                    return jsonify(parsed)
                except Exception:
                    return jsonify({"assistant_message": "Sorry, I couldn't interpret the assistant response. Try a simple task command like 'add buy milk'."})
            else:
                return jsonify({"assistant_message": "The AI translator returned an unexpected response. Try again or use direct commands like 'add buy milk'."})
        except Exception:
            # If the OpenAI call fails, fall through to a friendly static reply
            return jsonify({"assistant_message": "The AI translator is unavailable. Try direct commands like 'add buy milk'."}), 503

    # No AI client configured — return a simple helpful assistant message
    return jsonify({"assistant_message": "Hi — I can help manage your tasks. Try: 'add buy milk'."})

if __name__ == '__main__':
    # Default to localhost:5000 to match the SPA development expectation
    app.run(host='127.0.0.1', port=5000, debug=True)
