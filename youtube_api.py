"""Optional YouTube upload + owner analytics. Google OAuth; no passwords or paid API.

Credentials are created by the user in Google Cloud and stay in the local ClipBot data directory.
Uploads ALWAYS require a separate explicit UI confirmation. No auto-republishing.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from threading import Lock
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/yt-analytics.readonly',
]
PENDING = {}
HISTORY_LOCK = Lock()


def base_dir():
    d = Path(os.environ.get('CLIPBOT_HOME', str(Path.home() / 'Videos' / 'ClipBot' if os.name == 'nt' else Path.home() / 'clipbot'))).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safewrite(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def _client():
    path = base_dir() / 'oauth_client.json'
    data = _read(path)
    mode = 'web' if os.environ.get('CLIPBOT_PUBLIC_ORIGIN', '').strip() else 'installed'
    if not isinstance(data, dict) or not isinstance(data.get(mode), dict):
        label = 'Web application' if mode == 'web' else 'Desktop app'
        raise RuntimeError(f'YouTube not configured. Store your {label} OAuth client as oauth_client.json in the ClipBot data folder.')
    data = data[mode]
    if not data.get('client_id'):
        raise RuntimeError('OAuth client JSON has no client_id')
    return data


def _post_form(url, data):
    req = Request(url, data=urlencode(data).encode('utf-8'), headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    return _json_request(req)


def _json_request(req):
    try:
        with urlopen(req, timeout=60) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except HTTPError as e:
        details = e.read(2048).decode('utf-8', errors='replace')
        raise RuntimeError(f'Google API returned HTTP {e.code}: {details}') from e
    except URLError as e:
        raise RuntimeError(f'Network connection failed: {e}') from e


def oauth_link(port, public_base=""):
    client = _client()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state = secrets.token_urlsafe(24)
    base = public_base or f'http://127.0.0.1:{port}'
    redirect_uri = base + '/api/oauth/callback'
    PENDING.clear()
    PENDING.update(state=state, verifier=verifier, redirect_uri=redirect_uri, created=time.time())
    params = {
        'client_id': client['client_id'], 'redirect_uri': redirect_uri,
        'response_type': 'code', 'scope': ' '.join(SCOPES),
        'access_type': 'offline', 'prompt': 'consent',
        'code_challenge': challenge, 'code_challenge_method': 'S256',
        'state': state,
    }
    return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params)


def oauth_finish(state, code):
    if not PENDING or not secrets.compare_digest(state, PENDING.get('state', '')) or time.time() - PENDING['created'] > 600:
        raise ValueError('OAuth session expired or state mismatched. Restart connection.')
    client = _client()
    pending = dict(PENDING)
    PENDING.clear()
    token = _post_form('https://oauth2.googleapis.com/token', {
        'code': code, 'client_id': client['client_id'], 'client_secret': client.get('client_secret', ''),
        'code_verifier': pending['verifier'], 'redirect_uri': pending['redirect_uri'],
        'grant_type': 'authorization_code',
    })
    if not token.get('access_token'):
        raise RuntimeError('Google did not return an access token.')
    token['expires_at'] = time.time() + int(token.get('expires_in', 3600)) - 90
    _safewrite(base_dir() / 'oauth_tokens.json', token)
    return True


def is_connected():
    token = _read(base_dir() / 'oauth_tokens.json', {})
    return bool(token.get('refresh_token') or (token.get('access_token') and token.get('expires_at', 0) > time.time()))


def access_token():
    token = _read(base_dir() / 'oauth_tokens.json', {})
    if token.get('access_token') and token.get('expires_at', 0) > time.time():
        return token['access_token']
    if not token.get('refresh_token'):
        raise RuntimeError('Connect your Google account to YouTube first.')
    client = _client()
    fresh = _post_form('https://oauth2.googleapis.com/token', {
        'client_id': client['client_id'], 'client_secret': client.get('client_secret', ''),
        'refresh_token': token['refresh_token'], 'grant_type': 'refresh_token',
    })
    token.update(fresh)
    token['expires_at'] = time.time() + int(fresh.get('expires_in', 3600)) - 90
    _safewrite(base_dir() / 'oauth_tokens.json', token)
    return token['access_token']


def disconnect():
    (base_dir() / 'oauth_tokens.json').unlink(missing_ok=True)


def yt_upload(video_path, title, description, privacy='private'):
    """Resumable upload, max 8 MiB RAM per chunk. Returns YouTube ID.

    Caller must obtain user confirmation. This function does not independently
    enable publication, and defaults to private.
    """
    video_path = Path(video_path)
    if not video_path.is_file() or video_path.suffix.lower() != '.mp4':
        raise ValueError('Only an existing exported MP4 may be uploaded')
    if privacy not in ('private', 'unlisted', 'public'):
        raise ValueError('Invalid YouTube privacy')
    total = video_path.stat().st_size
    token = access_token()
    metadata = {'snippet': {'title': title[:100], 'description': description[:4800], 'categoryId': '22'},
                'status': {'privacyStatus': privacy}}
    req = Request('https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status',
                  method='POST', data=json.dumps(metadata).encode('utf-8'),
                  headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8',
                           'X-Upload-Content-Type': 'video/mp4', 'X-Upload-Content-Length': str(total)})
    try:
        with urlopen(req, timeout=60) as resp:
            location = resp.headers.get('Location')
    except HTTPError as e:
        raise RuntimeError(f'YouTube upload session failed (HTTP {e.code}): {e.read(1200).decode(errors="replace")}') from e
    if not location or urlparse(location).scheme != 'https' or urlparse(location).hostname not in ('www.googleapis.com','youtube.googleapis.com'):
        raise RuntimeError('Google did not supply a valid resumable upload URL')
    chunk_size = 8 * 1024 * 1024
    with video_path.open('rb') as f:
        offset = 0
        stalled = 0
        while offset < total:
            f.seek(offset)
            piece = f.read(chunk_size)
            hi = offset + len(piece) - 1
            req = Request(location, method='PUT', data=piece,
                          headers={'Authorization': f'Bearer {access_token()}', 'Content-Type': 'video/mp4',
                                   'Content-Range': f'bytes {offset}-{hi}/{total}'})
            try:
                with urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read())
                    if not data.get('id'):
                        raise RuntimeError('YouTube did not return an uploaded video ID')
                    return data['id']
            except HTTPError as exc:
                if exc.code == 308:
                    last = exc.headers.get('Range')
                    next_offset = int(last.rsplit('-', 1)[-1]) + 1 if last else 0
                    if next_offset > hi + 1:
                        raise RuntimeError('YouTube reported an invalid upload offset')
                    stalled = stalled + 1 if next_offset <= offset else 0
                    if stalled >= 3:
                        raise RuntimeError('YouTube upload stopped making progress. Check the channel for a draft before retrying.')
                    offset = next_offset
                    continue
                raise RuntimeError(f'YouTube upload failed (HTTP {exc.code}): {exc.read(1200).decode(errors="replace")}') from exc
    raise RuntimeError('Resumable upload ended without YouTube confirmation')


def _youtube_analytics(video_id, from_date, to_date):
    token = access_token()
    params = {'ids': 'channel==MINE', 'startDate': from_date, 'endDate': to_date,
              'metrics': 'views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments',
              'filters': 'video==' + video_id}
    url = 'https://youtubeanalytics.googleapis.com/v2/reports?' + urlencode(params)
    return _json_request(Request(url, headers={'Authorization': f'Bearer {token}'}))


def performance(video_id, published_date=None):
    # Reports may lag and do not provide every YouTube Studio Shorts metric.
    today = date.today()
    since = date.fromisoformat(published_date) if published_date else today - timedelta(days=28)
    if since > today:
        since = today
    # Most recent day often still processing; request through yesterday.
    end = max(since, min(since + timedelta(days=6), today - timedelta(days=1)))
    raw = _youtube_analytics(video_id, str(since), str(end))
    row = raw.get('rows', [[0, 0, 0, 0, 0, 0]])
    values = row[0] if row else [0, 0, 0, 0, 0, 0]
    views, watch_minutes, duration, percent, likes, comments = (float(n) for n in values[:6])
    return {'video_id': video_id, 'views': int(views), 'watch_minutes': round(watch_minutes, 1),
            'average_view_duration_seconds': round(duration, 1), 'average_view_percentage': round(percent, 1),
            'likes': int(likes), 'comments': int(comments), 'start': str(since), 'end': str(end),
            'data_note': 'Owner analytics can lag. Viewed-vs-swiped-away is not provided here.'}


def advice(metrics):
    """Exploratory suggestions, not an algorithm or prediction of virality."""
    percent = metrics.get('average_view_percentage', 0)
    views = metrics.get('views', 0)
    if views < 100:
        return 'Not enough data for a strong conclusion. Keep this clip, wait for more views, and try one different hook or caption style.'
    if percent < 55:
        return 'Experiment: move the strongest moment into the first 2 seconds, shorten pauses, and generate a shorter alternate cut. Check audience retention in YouTube Studio.'
    if percent < 85:
        return 'Experiment: tighten the opening and remove any mid-clip detour. Keep the topic; test one shorter cut with clearer captions.'
    return 'View duration looks promising relative to the clip length. Keep the content structure; test a different title or opening while monitoring actual Shorts engagement.'


def upload_status(video_id):
    query = urlencode({"part": "status", "id": video_id, "fields": "items(id,status(uploadStatus))"})
    req = Request("https://www.googleapis.com/youtube/v3/videos?" + query,
                  headers={"Authorization": f"Bearer {access_token()}"})
    for item in _json_request(req).get("items", []):
        if item.get("id") == video_id:
            return item.get("status", {}).get("uploadStatus")
    return None


def record_upload(filename, video_id, title, privacy):
    path = base_dir() / 'youtube_uploads.json'
    entries = _read(path, {})
    entries[video_id] = {'filename': filename, 'title': title, 'privacy': privacy, 'date': str(date.today())}
    _safewrite(path, entries)


def uploads():
    return _read(base_dir() / 'youtube_uploads.json', {})


def remember_performance(video_id, metrics):
    path = base_dir() / 'analytics_history.json'
    with HISTORY_LOCK:
        hist = _read(path, {})
        hist.setdefault(video_id, []).append({'checked_at': time.strftime('%Y-%m-%dT%H:%M:%S'), **metrics})
        hist[video_id] = hist[video_id][-30:]
        _safewrite(path, hist)


def latest_performance(video_id):
    history = _read(base_dir() / 'analytics_history.json', {}).get(video_id, [])
    return history[-1] if history else None


def revision_experiment(metrics):
    if metrics.get('views', 0) >= 100 and metrics.get('average_view_percentage', 0) < 85:
        return 'shorter'
    return 'hook'


def compare_performance(original, revision):
    if not original or min(original.get("views", 0), revision.get("views", 0)) < 100:
        return "At least 100 views per edit are needed for a preliminary comparison."
    def report_days(metrics):
        return (date.fromisoformat(metrics["end"]) - date.fromisoformat(metrics["start"])).days + 1

    if report_days(original) != report_days(revision):
        return "Check both edits once reports cover the same number of days after upload."
    delta = revision["average_view_percentage"] - original["average_view_percentage"]
    return (f"Average percentage viewed changed by {delta:+.1f} percentage points over equal-length reporting windows. "
            "Audience and traffic differences may contribute.")
