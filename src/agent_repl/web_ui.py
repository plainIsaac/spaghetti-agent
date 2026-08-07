"""Small dependency-free local web adapter for a single project session."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
from pathlib import Path
from threading import Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .ui import project_index, project_view


_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Spaghetti Agent · Project</title>
<style>
:root{color-scheme:light;--ink:#172033;--muted:#667085;--line:#e5e7eb;--panel:#fff;--soft:#f5f7fb;--accent:#4f46e5;--accent-dark:#3730a3;--ok:#087443;--warn:#9a6700;--danger:#b42318}*{box-sizing:border-box}body{margin:0;background:var(--soft);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1180px;margin:auto;padding:32px 24px 64px}header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:28px}h1,h2,p{margin:0}h1{font-size:clamp(24px,4vw,34px);letter-spacing:-.03em}h2{font-size:16px}.eyebrow{color:var(--accent);font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;margin-bottom:5px}.subtitle{color:var(--muted);margin-top:5px}.live{display:flex;align-items:center;gap:8px;color:var(--muted);white-space:nowrap}.dot{width:9px;height:9px;background:#16a36a;border-radius:50%;box-shadow:0 0 0 4px #d9f7e8}.layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(290px,.8fr);gap:20px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;box-shadow:0 5px 18px #1720330a}.panel-head{display:flex;justify-content:space-between;align-items:center;padding:18px 20px;border-bottom:1px solid var(--line)}.conversation{min-height:480px;display:flex;flex-direction:column}.messages{padding:18px 20px;flex:1;max-height:620px;overflow:auto}.message{padding:13px 15px;border:1px solid var(--line);border-radius:12px;margin-bottom:12px;white-space:pre-wrap;overflow-wrap:anywhere}.message:last-child{margin-bottom:0}.message small{display:block;color:var(--muted);font-size:12px;font-weight:700;margin-bottom:5px}.empty{color:var(--muted);padding:20px 0}.composer{padding:16px 20px;border-top:1px solid var(--line)}textarea{display:block;width:100%;min-height:92px;resize:vertical;border:1px solid #cfd4dc;border-radius:10px;padding:12px 13px;color:var(--ink);font:inherit;line-height:1.5}textarea:focus{outline:3px solid #c7d2fe;border-color:var(--accent)}.composer-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:10px}.hint{color:var(--muted);font-size:12px}button{border:0;border-radius:9px;background:var(--accent);color:white;cursor:pointer;font:600 14px inherit;padding:10px 15px;transition:background .15s,transform .15s}button:hover{background:var(--accent-dark)}button:active{transform:translateY(1px)}button:disabled{cursor:wait;opacity:.6}.secondary{background:#eef2ff;color:var(--accent-dark)}.secondary:hover{background:#e0e7ff}.stack{display:grid;gap:14px;align-content:start}.card{padding:16px 18px}.card strong{font-size:15px}.meta{color:var(--muted);font-size:13px}.status-row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px}.pill{border-radius:999px;background:#eef2ff;color:var(--accent-dark);font-size:12px;font-weight:700;padding:3px 9px}.pill.ok{background:#e8f7ef;color:var(--ok)}.pill.warn{background:#fff4d6;color:var(--warn)}details{background:var(--panel);border:1px solid var(--line);border-radius:16px;overflow:hidden}summary{cursor:pointer;list-style:none;padding:17px 18px;font-weight:700}summary::-webkit-details-marker{display:none}summary:after{content:'+';float:right;color:var(--muted);font-size:20px;line-height:16px}details[open] summary:after{content:'−'}details>section{border-top:1px solid var(--line);padding:12px 14px}.item{padding:12px 4px;border-bottom:1px solid var(--line)}.item:last-child{border-bottom:0}pre{background:#111827;color:#dbeafe;border-radius:9px;padding:12px;white-space:pre-wrap;overflow:auto;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;margin:10px 0 0}.toast{position:fixed;right:20px;bottom:20px;max-width:360px;background:#172033;color:white;border-radius:10px;padding:12px 15px;box-shadow:0 10px 30px #17203333}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}@media(max-width:800px){main{padding:22px 14px 44px}header{display:block}.live{margin-top:14px}.layout{grid-template-columns:1fr}.messages{max-height:none}.hint{display:none}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
</style></head><body><main><header><div><div class=eyebrow>Spaghetti Agent</div><h1>Project workspace</h1><p class=subtitle>Talk to your agent and keep an eye on its work.</p></div><div class=live><span class=dot aria-hidden=true></span><span id=connection>Live updates</span></div></header>
<div class=layout><section class="panel conversation" aria-labelledby=conversation-title><div class=panel-head><h2 id=conversation-title>Conversation</h2><span class=meta id=message-count>0 messages</span></div><div id=replies class=messages aria-live=polite></div><form id=send class=composer><label class=sr-only for=message>Message the project agent</label><textarea id=message name=text placeholder="Ask the agent to investigate, build, or explain something…" required></textarea><div class=composer-row><span class=hint>Press Ctrl + Enter to send</span><button id=send-button type=submit>Send message</button></div></form></section>
<aside class=stack><section class="panel card" aria-labelledby=inference-title><div class=status-row><h2 id=inference-title>Inference</h2><span id=inf-status class=pill>Idle</span></div><div id=inference class=meta>Waiting for the first turn.</div></section><details open><summary>Work status</summary><section><div id=agents></div><div id=tasks></div></section></details><details><summary>Verification</summary><section id=verification></section></details><details><summary>Submitted branches</summary><section id=branches></section></details><details><summary>Inspect work</summary><section><pre id=inspection></pre></section></details></aside></div><div id=toast class=toast role=status hidden></div></main>
<script>
const $=s=>document.querySelector(s),replies=$('#replies'),form=$('#send'),button=$('#send-button'),toast=$('#toast');
const text=(el,value)=>{el.textContent=value??'';return el}; const esc=(value)=>String(value??'');
function notify(message){text(toast,message);toast.hidden=false;clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>toast.hidden=true,3500)}
function empty(el,label){el.replaceChildren(text(document.createElement('p'),label));}
function item(title,detail,extra){const el=document.createElement('div');el.className='item';const strong=document.createElement('strong');text(strong,title);el.append(strong);if(detail){const meta=document.createElement('div');meta.className='meta';text(meta,detail);el.append(meta)}if(extra)el.append(extra);return el}
async function refresh(){try{const response=await fetch('/api/view');if(!response.ok)throw Error('Unable to load project state');const view=await response.json(),work=view.inspection,inf=view.default.inference,p=inf.provider;$('#connection').textContent='Live updates';const messages=view.default.replies||[];$('#message-count').textContent=messages.length+' message'+(messages.length===1?'':'s');replies.replaceChildren();if(!messages.length)empty(replies,'No messages yet. Your conversation will appear here.');messages.forEach(r=>{const el=document.createElement('article');el.className='message';const who=document.createElement('small');text(who,esc(r.sender));el.append(who,text(document.createTextNode(''),esc(r.text)));replies.append(el)});const status=inf.status||'idle';text($('#inf-status'),status);$('#inf-status').className='pill '+(status==='available'?'ok':status==='exhausted'?'warn':'');const details=p?p.provider+' · '+p.model:'No provider turn yet';text($('#inference'),details+'\\n'+(inf.used_tokens||0)+' tokens used'+(inf.remaining_tokens!==null&&inf.remaining_tokens!==undefined?' · '+inf.remaining_tokens+' remaining':'')+'\\n'+(inf.last_turn_accounting||inf.method||'Ready for a message'));const resume=document.createElement('button');resume.className='secondary';resume.type='button';resume.textContent='Resume pending work';resume.onclick=async()=>{resume.disabled=true;await fetch('/api/resume',{method:'POST'});resume.disabled=false;notify('Pending work resumed');refresh()};$('#inference').append(document.createElement('br'),resume);const agents=$('#agents');agents.replaceChildren();(work.agents||[]).length?(work.agents.forEach(a=>agents.append(item(a.name,a.role+' · '+a.phase+(a.pending_messages?' · '+a.pending_messages+' pending':'')))):empty(agents,'No active agents.');const tasks=$('#tasks');tasks.replaceChildren();(work.tasks||[]).length?(work.tasks.forEach(t=>tasks.append(item('Task #'+t.id+' · '+t.state,t.owner,t.title?text(document.createElement('div'),t.title):null)))):empty(tasks,'No active tasks.');const verification=$('#verification');verification.replaceChildren();(work.verification||[]).length?(work.verification.forEach(v=>{const extra=text(document.createElement('pre'),v.output);verification.append(item(v.exit_code===0?'Passed':v.timed_out?'Timed out':'Failed',v.command.join(' '),extra))})):empty(verification,'No verification commands have run.');const branches=$('#branches');branches.replaceChildren();const submitted=(work.branches||[]).filter(b=>b.state==='submitted');submitted.length?submitted.forEach(b=>{const merge=document.createElement('button');merge.className='secondary';merge.type='button';merge.textContent='Merge reviewed branch';merge.onclick=()=>mergeBranch(b.task_id,merge);branches.append(item('Task #'+b.task_id+' · '+b.agent,b.files+' file(s)',Object.assign(document.createElement('div'),{innerHTML:'<pre></pre>'})));branches.lastChild.querySelector('pre').textContent=b.diff||'No changed files';branches.lastChild.append(merge)}):empty(branches,'No submitted branches.');text($('#inspection'),JSON.stringify({state:view.default.state,inference:inf,...work},null,2))}catch(error){text($('#connection'),'Reconnecting…');notify(error.message)}}
async function mergeBranch(id,control){control.disabled=true;const r=await fetch('/api/branches/'+id+'/merge',{method:'POST'});if(!r.ok){notify((await r.json()).error||'Merge failed');control.disabled=false;return}notify('Branch merged');refresh()}
form.onsubmit=async e=>{e.preventDefault();const field=$('#message'),message=field.value.trim();if(!message)return;button.disabled=true;button.textContent='Sending…';try{const r=await fetch('/api/messages',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text:message})});if(!r.ok)throw Error('Message could not be sent');field.value='';notify('Message sent')}catch(error){notify(error.message)}finally{button.disabled=false;button.textContent='Send message';refresh()}};$('#message').onkeydown=e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();form.requestSubmit()}};refresh();setInterval(refresh,2000);
</script></body></html>"""

_PROJECT_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Spaghetti Agent · Projects</title><style>:root{font:15px system-ui;color:#172033;background:#f5f7fb}body{max-width:900px;margin:auto;padding:36px 20px}h1{margin:0 0 6px;letter-spacing:-.03em}p{color:#667085;margin:0 0 24px}.create,.card{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:18px;margin:14px 0;box-shadow:0 5px 18px #1720330a}.create{display:flex;gap:10px}input{flex:1;min-width:0;border:1px solid #cfd4dc;border-radius:9px;padding:10px 12px;font:inherit}input:focus{outline:3px solid #c7d2fe;border-color:#4f46e5}button{border:0;border-radius:9px;background:#4f46e5;color:#fff;cursor:pointer;font:600 14px inherit;padding:10px 14px}button:hover{background:#3730a3}.project{display:flex;align-items:center;justify-content:space-between;gap:16px}.project-name{font-weight:700}.meta{color:#667085;font-size:13px}.actions{display:flex;gap:8px;flex-wrap:wrap}.secondary{background:#eef2ff;color:#3730a3}.danger{background:#fff1f0;color:#b42318}@media(max-width:600px){body{padding:24px 14px}.create,.project{display:block}.create button{margin-top:10px;width:100%}.actions{margin-top:14px}}</style></head><body><div style="color:#4f46e5;font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase">Spaghetti Agent</div><h1>Projects</h1><p>Open a workspace to talk to an agent and review its work.</p><form id=create class=create><label for=name class=sr-only>Project name</label><input id=name name=name placeholder="New project name" required><button>Create project</button></form><section id=projects aria-live=polite></section><script>const root=document.querySelector('#projects'),form=document.querySelector('#create');const button=(label,kind,fn)=>{const b=document.createElement('button');b.textContent=label;b.className=kind||'';b.onclick=fn;return b};async function refresh(){const x=await(await fetch('/api/projects')).json();root.replaceChildren();if(!x.projects.length){const p=document.createElement('p');p.textContent='No projects yet.';root.append(p);return}x.projects.forEach(p=>{const card=document.createElement('article');card.className='card project';const info=document.createElement('div');const name=document.createElement('div');name.className='project-name';name.textContent=p.name;const meta=document.createElement('div');meta.className='meta';meta.textContent=p.state+(p.runtime_initialized?' · initialized':'');info.append(name,meta);const actions=document.createElement('div');actions.className='actions';if(p.state==='active'){actions.append(button('Open','',async()=>{const x=await(await fetch('/api/projects/'+p.id+'/open',{method:'POST'})).json();location.href=x.url}),button('Close runtime','secondary',async()=>{await fetch('/api/projects/'+p.id+'/close',{method:'POST'});refresh()}),button('Archive','danger',async()=>{await fetch('/api/projects/'+p.id+'/archive',{method:'POST'});refresh()}))}card.append(info,actions);root.append(card)})}form.onsubmit=async e=>{e.preventDefault();const name=new FormData(form).get('name').trim();if(!name)return;await fetch('/api/projects',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name})});form.reset();refresh()};refresh()</script></body></html>"""

_STATIC_DIR = Path(__file__).with_name("static")


def _static_asset(name: str) -> tuple[str, bytes]:
    """Read a packaged frontend asset and return its MIME type and bytes."""
    path = (_STATIC_DIR / name).resolve()
    if _STATIC_DIR.resolve() not in path.parents:
        raise ValueError("invalid static asset")
    mime = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}.get(path.suffix, "application/octet-stream")
    return mime, path.read_bytes()


_PAGE = _STATIC_DIR.joinpath("project.html").read_text(encoding="utf-8")
_PROJECT_PAGE = _STATIC_DIR.joinpath("projects.html").read_text(encoding="utf-8")


def make_handler(session: Any, on_message: Callable[[], None] | None = None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", _PAGE.encode())
            elif self.path.startswith("/static/"):
                try:
                    content_type, body = _static_asset(self.path.removeprefix("/static/"))
                    self._send(HTTPStatus.OK, content_type, body)
                except (OSError, ValueError):
                    self.send_error(HTTPStatus.NOT_FOUND)
            elif self.path == "/api/view":
                self._send(HTTPStatus.OK, "application/json", json.dumps(project_view(session), default=str).encode())
            elif urlsplit(self.path).path == "/api/events":
                query = parse_qs(urlsplit(self.path).query)
                after = int(query.get("after", ["0"])[0])
                limit = int(query.get("limit", ["200"])[0])
                body = json.dumps({"events": session.supervisor.events.recent(after, limit)}, default=str).encode()
                self._send(HTTPStatus.OK, "application/json", body)
            elif urlsplit(self.path).path == "/api/events/stream":
                self._stream_events(session)
            elif urlsplit(self.path).path == "/api/workspace":
                try:
                    workspace = session.supervisor.workspace
                    files = [path for path in workspace.list(".") if not any(part.startswith(".") for part in Path(path).parts)]
                    self._send(HTTPStatus.OK, "application/json", json.dumps({"files": files}, default=str).encode())
                except (OSError, ValueError, RuntimeError) as error:
                    self._send(HTTPStatus.CONFLICT, "application/json", json.dumps({"error": str(error)}).encode())
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path == "/api/resume":
                resumed = session.resume_pending() if hasattr(session, "resume_pending") else 0
                self._send(HTTPStatus.ACCEPTED, "application/json", json.dumps({"resumed": resumed}).encode())
                return
            if self.path.startswith("/api/branches/") and self.path.endswith("/merge"):
                try:
                    task_id = int(self.path.split("/")[3])
                    result = session.supervisor.workspace.merge(task_id)
                    self._send(HTTPStatus.OK, "application/json", json.dumps(result).encode())
                except (ValueError, RuntimeError, IndexError) as error:
                    self._send(HTTPStatus.CONFLICT, "application/json", json.dumps({"error": str(error)}).encode())
                return
            if self.path != "/api/messages":
                self.send_error(HTTPStatus.NOT_FOUND); return
            try:
                size = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(size))
                text = payload["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text must be non-empty")
            except (ValueError, KeyError, json.JSONDecodeError) as error:
                self._send(HTTPStatus.BAD_REQUEST, "application/json", json.dumps({"error": str(error)}).encode()); return
            message = session.send(text)
            if on_message is not None:
                on_message()
            self._send(HTTPStatus.ACCEPTED, "application/json", json.dumps({"id": message.id}).encode())

        def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            self.send_response(status); self.send_header("content-type", content_type); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def _stream_events(self, active_session: Any) -> None:
            query_values = parse_qs(urlsplit(self.path).query)
            after = int(query_values.get("after", ["0"])[0])
            subscriber = active_session.supervisor.events.subscribe()
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "keep-alive")
            self.end_headers()
            try:
                for event in active_session.supervisor.events.recent(after, 1_000):
                    self._write_event(event)
                while True:
                    try:
                        event = subscriber.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n"); self.wfile.flush()
                        continue
                    self._write_event(event.as_dict())
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                active_session.supervisor.events.unsubscribe(subscriber)

        def _write_event(self, event: dict[str, Any]) -> None:
            payload = json.dumps(event, ensure_ascii=False, default=str)
            self.wfile.write(f"id: {event['id']}\nevent: {event['kind']}\ndata: {payload}\n\n".encode())
            self.wfile.flush()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def make_project_manager_handler(manager: Any, open_project: Any = None, close_project: Any = None, restart_project: Any = None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                body = _PROJECT_PAGE.encode()
                self.send_response(HTTPStatus.OK); self.send_header("content-type", "text/html; charset=utf-8"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if self.path.startswith("/static/"):
                try:
                    content_type, body = _static_asset(self.path.removeprefix("/static/"))
                    self.send_response(HTTPStatus.OK); self.send_header("content-type", content_type); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
                except (OSError, ValueError):
                    self.send_error(HTTPStatus.NOT_FOUND)
                return
            if self.path == "/api/projects":
                self._send(HTTPStatus.OK, project_index(manager)); return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            try:
                if self.path == "/api/projects":
                    payload = self._json_body()
                    project = manager.create(payload["name"])
                    self._send(HTTPStatus.CREATED, {"id": project.id, "name": project.name, "state": project.state}); return
                if self.path.startswith("/api/projects/") and self.path.endswith("/open"):
                    if open_project is None:
                        raise ValueError("project opening is not configured")
                    project_id = int(self.path.split("/")[3])
                    self._send(HTTPStatus.OK, {"id": project_id, "url": open_project(project_id)}); return
                if self.path.startswith("/api/projects/") and self.path.endswith("/close"):
                    if close_project is None:
                        raise ValueError("project closing is not configured")
                    project_id = int(self.path.split("/")[3])
                    self._send(HTTPStatus.OK, {"id": project_id, "closed": close_project(project_id)}); return
                if self.path.startswith("/api/projects/") and self.path.endswith("/archive"):
                    project = manager.registry.archive(int(self.path.split("/")[3]))
                    self._send(HTTPStatus.OK, {"id": project.id, "state": project.state}); return
                if self.path.startswith("/api/projects/") and self.path.endswith("/inference-policy"):
                    project_id = int(self.path.split("/")[3])
                    policy = manager.set_inference_policy(project_id, self._json_body())
                    restarted = bool(restart_project(project_id)) if restart_project is not None and manager.is_open(project_id) else False
                    self._send(HTTPStatus.OK, {"id": project_id, "inference_policy": policy, "restarted": restarted}); return
            except (ValueError, KeyError) as error:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(error)}); return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _json_body(self) -> dict[str, Any]:
            return json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))

        def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(status); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


class LocalProjectManagerUI:
    def __init__(self, manager: Any, host: str = "127.0.0.1", port: int = 0) -> None:
        self.manager, self.host = manager, host
        self._projects: dict[int, LocalProjectUI] = {}
        self.server = ThreadingHTTPServer((host, port), make_project_manager_handler(manager, self.open_project, self.close_project, self.restart_project))
        self._thread: Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "LocalProjectManagerUI":
        if self._thread is None:
            self._thread = Thread(target=self.server.serve_forever, name="spaghetti-agent-project-manager-ui", daemon=True)
            self._thread.start()
        return self

    def open_project(self, project_id: int) -> str:
        existing = self._projects.get(project_id)
        if existing is not None:
            return existing.url
        session = self.manager.open(project_id)
        ui = LocalProjectUI(session, host=self.host).start()
        self._projects[project_id] = ui
        return ui.url

    def close_project(self, project_id: int) -> bool:
        ui = self._projects.pop(project_id, None)
        if ui is None:
            return False
        ui.shutdown(); self.manager.close_project(project_id)
        return True

    def restart_project(self, project_id: int) -> bool:
        if project_id not in self._projects:
            return False
        self.close_project(project_id)
        self.open_project(project_id)
        return True

    def shutdown(self) -> None:
        self.server.shutdown(); self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2); self._thread = None
        for project_id in list(self._projects):
            self.close_project(project_id)


def serve(
    session: Any,
    host: str = "127.0.0.1",
    port: int = 8765,
    on_message: Callable[[], None] | None = None,
) -> ThreadingHTTPServer:
    """Create a local-only project UI server; caller owns serve_forever/close."""
    return ThreadingHTTPServer((host, port), make_handler(session, on_message))


class LocalProjectUI:
    """Managed local server lifecycle, suitable for tests and embedded hosts."""

    def __init__(self, session: Any, host: str = "127.0.0.1", port: int = 0, on_message: Callable[[], None] | None = None) -> None:
        self.server = serve(session, host, port, on_message)
        self._thread: Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "LocalProjectUI":
        if self._thread is None:
            self._thread = Thread(target=self.server.serve_forever, name="spaghetti-agent-web-ui", daemon=True)
            self._thread.start()
        return self

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
