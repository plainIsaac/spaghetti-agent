"""Small dependency-free local web adapter for a single project session."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from typing import Any

from .ui import project_index, project_view


_PAGE = """<!doctype html><meta charset=utf-8><title>Spaghetti Agent</title>
<style>body{font:16px system-ui;margin:0;background:#fafafa;color:#222}main{max-width:900px;margin:auto;padding:24px}#replies{white-space:pre-wrap}.reply{background:#fff;padding:12px;margin:8px 0;border-radius:8px}textarea{width:100%;min-height:84px;font:inherit}button{margin-top:8px;padding:8px 14px}details{background:#fff;margin-top:14px;padding:10px;border-radius:8px}pre{white-space:pre-wrap;overflow:auto}</style>
<main><h1>Project</h1><section id=replies></section><form id=send><textarea name=text placeholder="Message the project agent…"></textarea><br><button>Send</button></form><details open><summary>Inference</summary><section id=inference></section></details><details open><summary>Work status</summary><section id=agents></section><section id=tasks></section></details><details open><summary>Verification</summary><section id=verification></section></details><details open><summary>Submitted branches</summary><section id=branches></section></details><details><summary>Inspect work</summary><pre id=inspection></pre></details></main>
<script>const replies=document.querySelector('#replies'),inspection=document.querySelector('#inspection');
async function merge(id){let r=await fetch('/api/branches/'+id+'/merge',{method:'POST'});if(!r.ok)alert((await r.json()).error);refresh()}async function resume(){await fetch('/api/resume',{method:'POST'});refresh()}
async function refresh(){let view=await (await fetch('/api/view')).json(),work=view.inspection,inf=view.default.inference,p=inf.provider;replies.innerHTML=view.default.replies.map(r=>`<div class=reply><small>${r.sender}</small><br>${r.text}</div>`).join('')||'<p>No replies yet.</p>';document.querySelector('#inference').innerHTML=`<div class=reply><b>${p?p.provider:'No provider turn yet'}</b>${p?' · '+p.model:''}<br><b>${inf.status}</b> · ${inf.used_tokens} used${inf.reserved_tokens?' · '+inf.reserved_tokens+' reserved':''}${inf.remaining_tokens!==null?' · '+inf.remaining_tokens+' remaining':''}<br><small>${p&&p.fallback_failures&&p.fallback_failures.length?'Fallback: '+p.fallback_failures.map(x=>x.provider).join(', '):inf.last_turn_accounting||inf.method}</small><br><button onclick="resume()">Resume pending work</button></div>`;document.querySelector('#agents').innerHTML=work.agents.map(a=>`<div class=reply><b>${a.name}</b> · ${a.role} · ${a.phase}${a.pending_messages?' · '+a.pending_messages+' pending':''}</div>`).join('')||'<p>No active agents.</p>';document.querySelector('#tasks').innerHTML=work.tasks.map(t=>`<div class=reply>Task #${t.id} · ${t.owner} · ${t.state}<br>${t.title}</div>`).join('')||'<p>No active tasks.</p>';document.querySelector('#verification').innerHTML=(work.verification||[]).map(v=>`<div class=reply><b>${v.exit_code===0?'Passed':v.timed_out?'Timed out':'Failed'}</b> · ${v.command.join(' ')}<pre>${v.output}</pre></div>`).join('')||'<p>No verification commands have run.</p>';let branches=work.branches.filter(b=>b.state==='submitted');document.querySelector('#branches').innerHTML=branches.map(b=>`<div class=reply><b>Task #${b.task_id}</b> — ${b.agent}, ${b.files} file(s)<pre>${b.diff||'No changed files'}</pre><button onclick="merge(${b.task_id})">Merge reviewed branch</button></div>`).join('')||'<p>No submitted branches.</p>';inspection.textContent=JSON.stringify({state:view.default.state,inference:inf,...work},null,2)}
document.querySelector('#send').onsubmit=async e=>{e.preventDefault();let text=new FormData(e.target).get('text').trim();if(!text)return;await fetch('/api/messages',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text})});e.target.reset();refresh()};refresh();setInterval(refresh,1500);</script>"""

_PROJECT_PAGE = """<!doctype html><meta charset=utf-8><title>Spaghetti Agent Projects</title>
<style>body{font:16px system-ui;max-width:760px;margin:32px auto;background:#fafafa}.card{background:#fff;padding:14px;margin:10px 0;border-radius:8px}input,button{font:inherit;padding:8px}</style>
<h1>Projects</h1><form id=create><input name=name placeholder="New project name"><button>Create</button></form><section id=projects></section>
<script>async function refresh(){let x=await(await fetch('/api/projects')).json();document.querySelector('#projects').innerHTML=x.projects.map(p=>`<div class=card><b>${p.name}</b> · ${p.state}${p.runtime_initialized?' · initialized':''}${p.state==='active'?` <button onclick="openProject(${p.id})">Open</button> <button onclick="closeProject(${p.id})">Close runtime</button> <button onclick="archive(${p.id})">Archive</button>`:''}</div>`).join('')||'<p>No projects yet.</p>'}async function openProject(id){let x=await(await fetch('/api/projects/'+id+'/open',{method:'POST'})).json();location.href=x.url}async function closeProject(id){await fetch('/api/projects/'+id+'/close',{method:'POST'});refresh()}async function archive(id){await fetch('/api/projects/'+id+'/archive',{method:'POST'});refresh()}document.querySelector('#create').onsubmit=async e=>{e.preventDefault();let name=new FormData(e.target).get('name').trim();if(!name)return;await fetch('/api/projects',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name})});e.target.reset();refresh()};refresh()</script>"""


def make_handler(session: Any):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", _PAGE.encode())
            elif self.path == "/api/view":
                self._send(HTTPStatus.OK, "application/json", json.dumps(project_view(session), default=str).encode())
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
            self._send(HTTPStatus.ACCEPTED, "application/json", json.dumps({"id": message.id}).encode())

        def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            self.send_response(status); self.send_header("content-type", content_type); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def make_project_manager_handler(manager: Any, open_project: Any = None, close_project: Any = None, restart_project: Any = None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/":
                body = _PROJECT_PAGE.encode()
                self.send_response(HTTPStatus.OK); self.send_header("content-type", "text/html; charset=utf-8"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body); return
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


def serve(session: Any, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create a local-only project UI server; caller owns serve_forever/close."""
    return ThreadingHTTPServer((host, port), make_handler(session))


class LocalProjectUI:
    """Managed local server lifecycle, suitable for tests and embedded hosts."""

    def __init__(self, session: Any, host: str = "127.0.0.1", port: int = 0) -> None:
        self.server = serve(session, host, port)
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
