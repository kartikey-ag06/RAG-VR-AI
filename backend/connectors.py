"""
connectors.py - send reports out, and pull documents in.

OUTPUT connectors (a report / work order goes to):
  webhook     POST the report as JSON to any URL (Zapier, n8n, Make, Power Automate, Slack/Teams webhooks...)
  email       SMTP, report attached as PDF
  jira        creates an issue  (Jira Cloud REST v3: base URL, email, API token, project key)
  servicenow  creates a record  (Table API: instance URL, user, password, table, default "incident")
INPUT connectors (documents are re-indexed automatically when they change):
  folder      a folder on the server (e.g. a synced SharePoint / OneDrive / Google Drive folder) - checked every N minutes
  sharepoint, gdrive, confluence, notion - read through their official APIs (see cloudsync.py for the set-up steps)

Secrets are never sent back to the browser (shown as "••••").
"""
from net import HTTP, JSONB
import hashlib
import json
import secrets
import smtplib
import threading
import time
from email.message import EmailMessage

import cloudsync
from pathlib import Path

import requests
from fastapi import Request
from fastapi.responses import JSONResponse

TYPES = {
    "webhook": {"dir": "out", "label": "Webhook (any URL)", "fields": [
        ("url", "URL", False), ("header_name", "Header name (optional, e.g. Authorization)", False),
        ("header_value", "Header value", True)]},
    "email": {"dir": "out", "label": "Email (SMTP)", "fields": [
        ("smtp_host", "SMTP host", False), ("smtp_port", "Port (587)", False), ("username", "Username", False),
        ("password", "Password / app password", True), ("from_addr", "From", False), ("to_addr", "To (comma separated)", False)]},
    "jira": {"dir": "out", "label": "Jira Cloud", "fields": [
        ("base_url", "Base URL (https://you.atlassian.net)", False), ("email", "Account email", False),
        ("api_token", "API token", True), ("project_key", "Project key", False), ("issue_type", "Issue type (Task)", False)]},
    "servicenow": {"dir": "out", "label": "ServiceNow", "fields": [
        ("instance_url", "Instance URL (https://you.service-now.com)", False), ("username", "User", False),
        ("password", "Password", True), ("table", "Table (incident)", False)]},
    "folder": {"dir": "in", "label": "Folder sync (SharePoint / OneDrive / Drive synced folder)", "fields": [
        ("path", "Folder path on the server", False), ("interval_min", "Check every N minutes (10)", False),
        ("tags", "Machine ids / tags for its files (comma separated)", False)]},
    "sharepoint": {"dir": "in", "label": "SharePoint / OneDrive for Business (Microsoft Graph app)", "fields": [
        ("tenant_id", "Directory (tenant) id", False), ("client_id", "Application (client) id", False),
        ("client_secret", "Client secret", True), ("site_url", "Site URL (https://you.sharepoint.com/sites/Team)", False),
        ("folder", "Folder in the site's documents (optional, e.g. Manuals)", False),
        ("interval_min", "Check every N minutes (30)", False), ("tags", "Machine ids / tags (comma separated)", False)]},
    "gdrive": {"dir": "in", "label": "Google Drive (service account)", "fields": [
        ("service_account_json", "Service-account key (paste the whole JSON)", True),
        ("folder_id", "Folder id (from the folder's link, shared with the service account)", False),
        ("interval_min", "Check every N minutes (30)", False), ("tags", "Machine ids / tags (comma separated)", False)]},
    "confluence": {"dir": "in", "label": "Confluence Cloud (API token)", "fields": [
        ("base_url", "Site URL (https://you.atlassian.net)", False), ("email", "Account e-mail", False),
        ("api_token", "API token", True), ("space_key", "Space key (e.g. OPS)", False),
        ("interval_min", "Check every N minutes (30)", False), ("tags", "Machine ids / tags (comma separated)", False)]},
    "notion": {"dir": "in", "label": "Notion (integration token)", "fields": [
        ("token", "Internal integration token (secret_… / ntn_…)", True),
        ("root_id", "Database or page id (optional; empty = all pages shared with it)", False),
        ("interval_min", "Check every N minutes (30)", False), ("tags", "Machine ids / tags (comma separated)", False)]},
}
CLOUD = ("sharepoint", "gdrive", "confluence", "notion")
ALLOWED_EXT = {".pdf", ".docx", ".txt"}


def _secret_names(t):
    return {f for f, _, sec in TYPES[t]["fields"] if sec}


def masked(c):
    out = json.loads(json.dumps(c))
    for f in _secret_names(c["type"]):
        if out["settings"].get(f):
            out["settings"][f] = "••••"
    return out


# ------------------------------------------------------------------ sending a report
def send(c, report, pdf_bytes=None, timeout=20):
    """report: {title, summary, kind, fields...}. Returns a short text describing where it went."""
    s, t = c["settings"], c["type"]
    if t == "webhook":
        h = {"Content-Type": "application/json"}
        if s.get("header_name") and s.get("header_value"):
            h[s["header_name"]] = s["header_value"]
        r = HTTP.post(s["url"], json=report, headers=h, timeout=timeout)
        r.raise_for_status()
        return f"webhook HTTP {r.status_code}"
    if t == "email":
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = report.get("title", "VR_RAG report"), s.get("from_addr") or s.get("username"), s["to_addr"]
        m.set_content(report.get("text") or report.get("summary", ""))
        if pdf_bytes:
            m.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="report.pdf")
        with smtplib.SMTP(s["smtp_host"], int(s.get("smtp_port") or 587), timeout=timeout) as smtp:
            smtp.starttls()
            if s.get("username"):
                smtp.login(s["username"], s.get("password", ""))
            smtp.send_message(m)
        return f"email sent to {s['to_addr']}"
    if t == "jira":
        body = {"fields": {"project": {"key": s["project_key"]}, "summary": report.get("title", "VR_RAG report")[:250],
                           "issuetype": {"name": s.get("issue_type") or "Task"},
                           "description": {"type": "doc", "version": 1, "content": [
                               {"type": "paragraph", "content": [{"type": "text", "text": (report.get("text") or "")[:30000]}]}]}}}
        r = HTTP.post(s["base_url"].rstrip("/") + "/rest/api/3/issue", json=body, timeout=timeout,
                          auth=(s["email"], s["api_token"]), headers={"Accept": "application/json"})
        r.raise_for_status()
        return f"Jira issue {r.json().get('key', '')}"
    if t == "servicenow":
        table = s.get("table") or "incident"
        r = HTTP.post(s["instance_url"].rstrip("/") + f"/api/now/table/{table}", timeout=timeout,
                          auth=(s["username"], s["password"]), headers={"Accept": "application/json"},
                          json={"short_description": report.get("title", "VR_RAG report")[:160],
                                "description": (report.get("text") or "")[:30000]})
        r.raise_for_status()
        return f"ServiceNow {table} {r.json().get('result', {}).get('number', '')}"
    raise ValueError(f"{t} cannot send reports")


# ------------------------------------------------------------------ folder sync (input connector)
class FolderSync:
    def __init__(self, store, kv):
        self.store, self.kv = store, kv
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            time.sleep(30)
            try:                                       # a database blip must not stop folder sync for good
                for c in self.kv.kv_list("connector"):
                    if c.get("type") in ("folder",) + CLOUD and c.get("enabled", True):
                        try:
                            mins = max(1, int(c["settings"].get("interval_min") or (10 if c["type"] == "folder" else 30)))
                            if time.time() - c.get("last_sync", 0) >= mins * 60:
                                self.sync(c) if c["type"] == "folder" else cloudsync.sync_source(c, self.store, self.kv)
                        except Exception as e:
                            c["last_result"] = f"error: {e}"[:300]
                            c["last_sync"] = time.time()
                            self.kv.kv_put("connector", c["id"], c)
            except Exception as e:
                print(f"[connectors] folder sync: {e}", flush=True)

    def sync(self, c):
        """New file -> added. Changed file (same name, new content) -> old version removed, new one added."""
        root = Path(c["settings"]["path"]).expanduser()
        if not root.is_dir():
            raise ValueError(f"folder not found: {root}")
        tags = [t.strip() for t in str(c["settings"].get("tags") or "").split(",") if t.strip()]
        known = c.setdefault("files", {})                     # name -> {sha, uid}
        added = changed = 0
        for f in sorted(root.rglob("*")):
            if f.suffix.lower() not in ALLOWED_EXT or not f.is_file() or f.stat().st_size > 50 * 1024 * 1024:
                continue
            rel = str(f.relative_to(root))
            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            k = known.get(rel)
            if k and k["sha"] == sha:
                continue
            old = self.store.doc_entry(k["uid"]) if k and k.get("uid") else None
            try:                                              # add the new version first, remove the old one after
                e = self.store.add(f.name, data, tags=tags or (old or {}).get("tags"), groups=(old or {}).get("groups"))
            except ValueError:                                # identical content already uploaded under another name
                known[rel] = {"sha": sha, "uid": k.get("uid", "") if k else ""}
                continue
            if old:
                self.store.delete(old["uid"])
            changed += 1 if k else 0
            added += 0 if k else 1
            known[rel] = {"sha": sha, "uid": e["uid"]}
        c["last_sync"] = time.time()
        c["last_result"] = f"{added} new, {changed} updated"
        self.kv.kv_put("connector", c["id"], c)
        return c["last_result"]


def register(app, kv, auth, store):
    folder = FolderSync(store, kv)

    def deny(req):
        return None if auth.is_admin(req) else JSONResponse({"error": "admin only"}, 403)

    @app.get("/api/connectors")
    def list_connectors(req: Request):
        admin = auth.is_admin(req)
        cs = [masked(c) for c in kv.kv_list("connector")]
        for c in cs:
            c.pop("files", None)
            if not admin:                       # webhook URLs, mail servers, folder paths are admin-only
                c.pop("settings", None); c.pop("last_result", None)
        return {"connectors": cs, "types": {k: {"label": v["label"], "dir": v["dir"], "planned": v.get("planned", False),
                                                "fields": [{"name": f, "label": l, "secret": sec} for f, l, sec in v["fields"]]}
                                            for k, v in TYPES.items()}, "admin": auth.is_admin(req)}

    @app.post("/api/connectors")
    def save_connector(req: Request, _body=JSONB):
        d = deny(req)
        if d:
            return d
        b = (_body if _body is not None else {})
        t = b.get("type")
        if t not in TYPES or TYPES[t].get("planned"):
            return JSONResponse({"error": "this connector type is not available yet"}, 400)
        old = kv.kv_get("connector", b.get("id")) if b.get("id") else None
        settings = {}
        for f, _, sec in TYPES[t]["fields"]:
            v = str((b.get("settings") or {}).get(f, "")).strip()[:6000 if f == "service_account_json" else 500]
            if sec and v == "••••" and old:
                v = old["settings"].get(f, "")              # unchanged secret
            settings[f] = v
        c = dict(old or {}, id=(old or {}).get("id") or secrets.token_hex(5), type=t,
                 name=str(b.get("name") or TYPES[t]["label"])[:80], enabled=bool(b.get("enabled", True)), settings=settings)
        kv.kv_put("connector", c["id"], c)
        return masked(c)

    @app.delete("/api/connectors/{cid}")
    def delete_connector(cid: str, req: Request):
        d = deny(req)
        if d:
            return d
        kv.kv_delete("connector", cid)
        return {"deleted": cid}

    @app.post("/api/connectors/{cid}/test")
    def test_connector(cid: str, req: Request):
        d = deny(req)
        if d:
            return d
        c = kv.kv_get("connector", cid)
        if not c:
            return JSONResponse({"error": "not found"}, 404)
        try:
            if c["type"] == "folder":
                return {"ok": True, "result": folder.sync(c)}
            if c["type"] in CLOUD:
                return {"ok": True, "result": cloudsync.sync_source(c, store, kv)}
            res = send(c, {"title": "VR_RAG test", "summary": "Connector test from VR_RAG", "kind": "test",
                           "text": "This is a test message from VR_RAG."})
            return {"ok": True, "result": res}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:400]}, 502)

    return {"send": send, "get": lambda cid: kv.kv_get("connector", cid)}
