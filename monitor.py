"""Best-effort owner analytics check while ClipBot's local server is running.

This is NOT an Android background service: OS may stop Windows.
"""
import logging
import threading
import time
from datetime import date, timedelta
import youtube_api


def check_due():
    if not youtube_api.is_connected():
        return
    records = youtube_api.uploads()
    history = youtube_api._read(youtube_api.base_dir() / 'analytics_history.json', {})
    now = time.time()
    for video_id, row in list(records.items())[-12:]:
        try:
            if date.fromisoformat(row['date']) < date.today() - timedelta(days=29):
                continue
            existing = history.get(video_id, [])
            if existing:
                stamp = time.strptime(existing[-1]['checked_at'], '%Y-%m-%dT%H:%M:%S')
                if now - time.mktime(stamp) < 23*60*60:
                    continue
            metrics = youtube_api.performance(video_id, row.get('date'))
            youtube_api.remember_performance(video_id, metrics)
        except Exception as exc:
            logging.warning('Scheduled analytics check for %s skipped: %s', video_id, exc)


def start_monitor():
    def loop():
        while True:
            try:
                check_due()
            except Exception:
                logging.exception('Analytics monitor error')
            time.sleep(60*60)
    t = threading.Thread(target=loop, name='analytics-monitor', daemon=True)
    t.start()
