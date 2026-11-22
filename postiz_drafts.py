"""Optional draft handoff to an explicitly configured Postiz installation."""
from datetime import datetime, timezone
import json
import os
from threading import Lock
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

GUARD = Lock()
VIDEO_CHANNELS = {"youtube", "instagram", "instagram-standalone", "tiktok",
                  "facebook", "x", "threads", "bluesky", "linkedin", "linkedin-page"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def configured():
    return bool(os.getenv("CLIPBOT_POSTIZ_URL") and os.getenv("CLIPBOT_POSTIZ_KEY"))


def request(endpoint, body=None, content_type="application/json"):
    base = os.getenv("CLIPBOT_POSTIZ_URL", "").rstrip("/")
    key = os.getenv("CLIPBOT_POSTIZ_KEY")
    url = urlsplit(base)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not key or not url.hostname or url.username or url.password or url.query or url.fragment
            or (url.scheme != "https" and not (url.scheme == "http" and local))):
        raise RuntimeError("Configure the Postiz API address and key on the computer running ClipBot.")
    if not base.endswith("/public/v1"):
        base += "/public/v1"
    req = Request(base + endpoint, data=body,
                  headers={"Authorization": key, "Content-Type": content_type})
    try:
        with build_opener(NoRedirect()).open(req, timeout=90) as response:
            return json.loads(response.read(2_000_000))
    except HTTPError as error:
        raise RuntimeError(f"Postiz returned HTTP {error.code}. Check the Postiz dashboard before retrying.") from error
    except (OSError, ValueError) as error:
        raise RuntimeError("Postiz did not return a usable response. Check its dashboard before retrying.") from error


def channels():
    response = request("/integrations")
    if not isinstance(response, list):
        raise RuntimeError("Postiz did not return a channel list.")
    return [{"id": row["id"], "name": row.get("name") or row["identifier"],
             "platform": row["identifier"]}
            for row in response if isinstance(row, dict) and isinstance(row.get("id"), str)
            and row.get("identifier") in VIDEO_CHANNELS and not row.get("disabled")]


def drafts(video, selections, title):
    """Save drafts once per video/account. Keep unknown results pending for reconciliation."""
    accounts = {item["id"]: item for item in channels()}
    if not selections or any(key not in accounts for key in selections):
        raise ValueError("Choose connected Postiz destinations.")
    if video.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("This video is over 64 MB. Upload it directly in Postiz.")
    ledger_path = video.with_suffix(".postiz.json")

    def save():
        staged = ledger_path.with_suffix(".tmp")
        staged.write_text(json.dumps(ledger), encoding="utf-8")
        staged.replace(ledger_path)

    with GUARD:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else {}
        if any(key in ledger and not ledger[key] for key in selections):
            raise RuntimeError("A draft request needs reconciliation. Check Postiz before making another draft.")
        pending = [key for key in selections if key not in ledger]
        if pending:
            boundary = "ClipBot" + uuid4().hex
            header = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="video.mp4"\r\n'
                      'Content-Type: video/mp4\r\n\r\n').encode("ascii")
            tail = (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="adjust"\r\n\r\nfalse'
                    f'\r\n--{boundary}--\r\n').encode("ascii")
            media = request("/upload", header + video.read_bytes() + tail,
                            f"multipart/form-data; boundary={boundary}")
            if not isinstance(media, dict) or not media.get("id") or not media.get("path"):
                raise RuntimeError("Postiz upload did not return an attachment.")
            posts = []
            for key in pending:
                platform = accounts[key]["platform"]
                settings = {"__type": platform}
                if platform == "youtube":
                    settings["title"] = title
                posts.append({"integration": {"id": key}, "settings": settings,
                              "value": [{"content": selections[key],
                                         "image": [{"id": media["id"], "path": media["path"]}]}]})
                ledger[key] = None
            save()
            body = {"type": "draft", "date": datetime.now(timezone.utc).isoformat(),
                    "shortLink": False, "tags": [], "posts": posts}
            result = request("/posts", json.dumps(body).encode())
            if isinstance(result, list):
                for row in result:
                    if isinstance(row, dict) and row.get("integration") in pending and row.get("postId"):
                        ledger[row["integration"]] = row["postId"]
                save()
            if any(not ledger[key] for key in pending):
                raise RuntimeError("Some drafts could not be confirmed. Check the Postiz dashboard.")
        return [{"name": accounts[key]["name"], "id": ledger[key]} for key in selections]
