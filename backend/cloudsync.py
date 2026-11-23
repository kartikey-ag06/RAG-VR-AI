"""
cloudsync.py - pull documents from SharePoint, Google Drive, Confluence and Notion into the glasses' index.

Each source lists its items with a version stamp; only new or changed items are downloaded and re-indexed, and
items that disappear from the source are removed from the index too (a mirror). Pages (Confluence, Notion) are
indexed as plain-text files. Credentials stay on the server and are never sent back to the browser.

SharePoint   Microsoft Graph, app-only: tenant id, client (app) id, client secret, site URL, optional folder.
             Azure portal -> App registrations -> new app -> API permissions: Microsoft Graph "Sites.Read.All"
             (Application) + admin consent -> Certificates & secrets -> new client secret.
Google Drive A service account: Google Cloud console -> IAM -> Service accounts -> create -> Keys -> JSON.
             Enable the "Google Drive API", then share the Drive folder with the service account's e-mail.
             Google Docs are exported as Word files; PDFs, .docx and .txt are downloaded as they are.
Confluence   Cloud: site URL, account e-mail, API token (id.atlassian.com -> Security -> API tokens), space key.
Notion       Internal integration token (notion.so/my-integrations); share the pages / databases with it.
             Optional: a database id or page id to limit what is synced (else: everything shared with it).
"""
import base64
import hashlib
import html
import json
import re
import time
from urllib.parse import quote, urlparse

from net import HTTP

SUPPORTED = (".pdf", ".docx", ".txt")
MAX_BYTES = 50 * 1024 * 1024


def _safe(name, ext=".txt"):
    n = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "page"
    return n[:120] + ("" if n.lower().endswith(SUPPORTED) else ext)


def html_to_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr|table|ul|ol)>", "\n", h)
    h = re.sub(r"(?i)<li[^>]*>", "• ", h)
    h = re.sub(r"(?i)</t[dh]>", " | ", h)
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"\n\s*\n+", "\n\n", html.unescape(h)).strip()


# ------------------------------------------------------------------ SharePoint (Microsoft Graph)
def sharepoint_items(s):
    tok = HTTP.post(f"https://login.microsoftonline.com/{s['tenant_id'].strip()}/oauth2/v2.0/token", timeout=20, data={
        "client_id": s["client_id"].strip(), "client_secret": s["client_secret"], "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default"})
    if tok.status_code != 200:
        raise ValueError(f"Microsoft sign-in failed: {tok.text[:200]}")
    h = {"Authorization": "Bearer " + tok.json()["access_token"]}
    u = urlparse(s["site_url"].strip())
    site = HTTP.get(f"https://graph.microsoft.com/v1.0/sites/{u.hostname}:{u.path.rstrip('/') or '/'}", headers=h, timeout=20)
    site.raise_for_status()
    sid = site.json()["id"]
    folder = s.get("folder", "").strip().strip("/")
    start = f"https://graph.microsoft.com/v1.0/sites/{sid}/drive/" + (f"root:/{quote(folder)}:/children" if folder else "root/children")
    out, queue = [], [(start, "")]
    while queue and len(out) < 2000:
        url, prefix = queue.pop()
        while url:
            r = HTTP.get(url, headers=h, timeout=30)
            r.raise_for_status()
            j = r.json()
            for it in j.get("value", []):
                if "folder" in it:
                    queue.append((f"https://graph.microsoft.com/v1.0/sites/{sid}/drive/items/{it['id']}/children", prefix + it["name"] + "/"))
                elif it["name"].lower().endswith(SUPPORTED) and it.get("size", 0) <= MAX_BYTES:
                    dl = f"https://graph.microsoft.com/v1.0/sites/{sid}/drive/items/{it['id']}/content"
                    out.append({"key": "sp:" + it["id"], "name": it["name"], "path": prefix + it["name"],
                                "ver": it.get("cTag") or it.get("eTag") or it.get("lastModifiedDateTime", ""),
                                "fetch": (lambda dl=dl: _get_bytes(dl, h))})
            url = j.get("@odata.nextLink")
    return out


def _get_bytes(url, headers=None, params=None):
    r = HTTP.get(url, headers=headers or {}, params=params, timeout=90)
    r.raise_for_status()
    return r.content


# ------------------------------------------------------------------ Google Drive (service account)
def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def google_token(sa_json, scope="https://www.googleapis.com/auth/drive.readonly"):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    sa = json.loads(sa_json)
    now = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": sa["client_email"], "scope": scope, "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"),
                              "iat": now, "exp": now + 3000}).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = _b64(key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    r = HTTP.post(sa.get("token_uri", "https://oauth2.googleapis.com/token"), timeout=20, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{head}.{claims}.{sig}"})
    if r.status_code != 200:
        raise ValueError(f"Google sign-in failed: {r.text[:200]}")
    return r.json()["access_token"]


GDOC = "application/vnd.google-apps.document"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def gdrive_items(s):
    h = {"Authorization": "Bearer " + google_token(s["service_account_json"])}
    api = "https://www.googleapis.com/drive/v3/files"
    out, queue = [], [(s["folder_id"].strip(), "")]
    seen = set()
    while queue and len(out) < 2000:
        fid, prefix = queue.pop()
        if fid in seen:
            continue
        seen.add(fid)
        token = None
        while True:
            r = HTTP.get(api, headers=h, timeout=30, params={
                "q": f"'{fid}' in parents and trashed = false", "pageSize": 200, "pageToken": token or "",
                "fields": "nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime, size)",
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
            r.raise_for_status()
            j = r.json()
            for f in j.get("files", []):
                mt = f["mimeType"]
                if mt == "application/vnd.google-apps.folder":
                    queue.append((f["id"], prefix + f["name"] + "/"))
                elif mt == GDOC:                                   # Google Doc -> Word file
                    out.append({"key": "gd:" + f["id"], "name": _safe(f["name"], ".docx"), "path": prefix + f["name"],
                                "ver": f.get("modifiedTime", ""),
                                "fetch": (lambda i=f["id"]: _get_bytes(f"{api}/{i}/export", h, {"mimeType": DOCX}))})
                elif f["name"].lower().endswith(SUPPORTED) and int(f.get("size") or 0) <= MAX_BYTES:
                    out.append({"key": "gd:" + f["id"], "name": f["name"], "path": prefix + f["name"],
                                "ver": f.get("md5Checksum") or f.get("modifiedTime", ""),
                                "fetch": (lambda i=f["id"]: _get_bytes(f"{api}/{i}", h, {"alt": "media", "supportsAllDrives": "true"}))})
            token = j.get("nextPageToken")
            if not token:
                break
    return out


# ------------------------------------------------------------------ Confluence Cloud
def confluence_items(s):
    base = s["base_url"].rstrip("/")
    if not base.endswith("/wiki"):
        base += "/wiki"
    auth = (s["email"].strip(), s["api_token"])
    out, start = [], 0
    while len(out) < 2000:
        r = HTTP.get(f"{base}/rest/api/content", auth=auth, timeout=30, headers={"Accept": "application/json"}, params={
            "spaceKey": s["space_key"].strip(), "type": "page", "status": "current", "limit": 50, "start": start,
            "expand": "version,body.storage"})
        if r.status_code == 401:
            raise ValueError("Confluence refused the e-mail / API token")
        r.raise_for_status()
        j = r.json()
        for p in j.get("results", []):
            text = p["title"] + "\n\n" + html_to_text(p.get("body", {}).get("storage", {}).get("value", ""))
            data = text.encode("utf-8")
            out.append({"key": "cf:" + p["id"], "name": _safe(p["title"]), "path": p["title"],
                        "ver": str(p.get("version", {}).get("number", "")), "fetch": (lambda d=data: d)})
        if j.get("size", 0) < 50:
            break
        start += 50
    return out


# ------------------------------------------------------------------ Notion
def _notion(path, tok, method="GET", body=None):
    h = {"Authorization": "Bearer " + tok, "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    url = "https://api.notion.com/v1" + path
    r = HTTP.post(url, headers=h, json=body or {}, timeout=30) if method == "POST" else HTTP.get(url, headers=h, timeout=30)
    if r.status_code == 401:
        raise ValueError("Notion refused the integration token")
    r.raise_for_status()
    return r.json()


def _rich(rt):
    return "".join(x.get("plain_text", "") for x in rt or [])


def notion_page_text(pid, tok, depth=0):
    lines, cursor = [], None
    while True:
        j = _notion(f"/blocks/{pid}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""), tok)
        for b in j.get("results", []):
            t = b.get("type")
            v = b.get(t, {}) if t else {}
            txt = _rich(v.get("rich_text"))
            if t and t.startswith("heading"):
                lines.append("\n" + txt)
            elif t in ("bulleted_list_item", "numbered_list_item", "to_do"):
                lines.append(("☐ " if t == "to_do" else "• ") + txt)
            elif t == "table_row":
                lines.append(" | ".join(_rich(c) for c in v.get("cells", [])))
            elif txt:
                lines.append(txt)
            if b.get("has_children") and depth < 3 and t != "child_page":
                lines.append(notion_page_text(b["id"], tok, depth + 1))
        if not j.get("has_more"):
            break
        cursor = j.get("next_cursor")
    return "\n".join(x for x in lines if x is not None)


def _title(page):
    for p in (page.get("properties") or {}).values():
        if p.get("type") == "title":
            return _rich(p.get("title")) or "Untitled"
    return "Untitled"


def notion_items(s):
    tok, root = s["token"].strip(), re.sub(r"[^0-9a-f]", "", (s.get("root_id") or "").lower())[-32:]
    pages = []
    if root:
        try:                                                   # a database: every row is a page
            cur = None
            while True:
                j = _notion(f"/databases/{root}/query", tok, "POST", {"page_size": 100, **({"start_cursor": cur} if cur else {})})
                pages += j.get("results", [])
                if not j.get("has_more"):
                    break
                cur = j.get("next_cursor")
        except Exception:
            pages = [_notion(f"/pages/{root}", tok)]            # a single page
    else:
        cur = None
        while len(pages) < 1000:
            j = _notion("/search", tok, "POST", {"filter": {"value": "page", "property": "object"}, "page_size": 100,
                                                 **({"start_cursor": cur} if cur else {})})
            pages += j.get("results", [])
            if not j.get("has_more"):
                break
            cur = j.get("next_cursor")
    out = []
    for p in pages:
        if p.get("archived") or p.get("in_trash"):
            continue
        title = _title(p)
        out.append({"key": "nt:" + p["id"], "name": _safe(title), "path": title, "ver": p.get("last_edited_time", ""),
                    "fetch": (lambda pid=p["id"], t=title: (t + "\n\n" + notion_page_text(pid, tok)).encode("utf-8"))})
    return out


FETCHERS = {"sharepoint": sharepoint_items, "gdrive": gdrive_items, "confluence": confluence_items, "notion": notion_items}


def sync_source(c, store, kv):
    """One run for one cloud connector. Returns a short result line and saves the connector."""
    s = c["settings"]
    tags = [t.strip() for t in str(s.get("tags") or "").split(",") if t.strip()]
    items = FETCHERS[c["type"]](s)
    known = c.setdefault("files", {})                          # key -> {ver, sha, uid, path}
    added = changed = removed = same = 0
    present = set()
    for it in items:
        present.add(it["key"])
        k = known.get(it["key"])
        if k and k.get("ver") == it["ver"] and k.get("uid") and store.doc_entry(k["uid"]):
            same += 1
            continue
        data = it["fetch"]()
        if not data or len(data) > MAX_BYTES:
            continue
        sha = hashlib.sha256(data).hexdigest()
        if k and k.get("sha") == sha and k.get("uid") and store.doc_entry(k["uid"]):
            known[it["key"]] = dict(k, ver=it["ver"])
            same += 1
            continue
        old = store.doc_entry(k["uid"]) if k and k.get("uid") else None
        try:
            e = store.add(it["name"], data, tags=tags or (old or {}).get("tags"), groups=(old or {}).get("groups"))
        except ValueError:                                     # same content already indexed
            known[it["key"]] = {"ver": it["ver"], "sha": sha, "uid": (k or {}).get("uid", ""), "path": it["path"]}
            continue
        if old:
            store.delete(old["uid"])
        changed += 1 if k else 0
        added += 0 if k else 1
        known[it["key"]] = {"ver": it["ver"], "sha": sha, "uid": e["uid"], "path": it["path"]}
    for key in [k for k in known if k not in present]:          # deleted at the source -> deleted here
        uid = known.pop(key).get("uid")
        if uid and store.doc_entry(uid):
            store.delete(uid)
            removed += 1
    c["last_sync"] = time.time()
    c["last_result"] = f"{added} new, {changed} updated, {removed} removed, {same} unchanged"
    kv.kv_put("connector", c["id"], c)
    return c["last_result"]
