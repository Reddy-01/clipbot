"""Minimal local smoke tests. Running does not modify the user's phone data."""
import os
import tempfile
from subprocess import CompletedProcess
import unittest
from pathlib import Path

test_home = tempfile.TemporaryDirectory(prefix="clipbot-test-")
os.environ["CLIPBOT_HOME"] = test_home.name
import clipbot
import youtube_api
from pipeline_addons import fetch_authorized_video, validate_youtube_url, generate_hook
from unittest.mock import patch

class TestClipBot(unittest.TestCase):
    def test_offline_storyboard_and_validation(self):
        with patch.dict(os.environ, {"CLIPBOT_OLLAMA_MODEL": ""}), \
             patch("creator_studio.local_model", return_value=None):
            draft = clipbot.creator_studio.draft("Start with the setup.\nShow the finished result.")
        title, scenes = clipbot.creator_studio.validate(draft)
        self.assertEqual(title, "Start with the setup.")
        self.assertEqual(len(scenes), 2)
        with self.assertRaises(ValueError):
            clipbot.creator_studio.validate({"title": title, "scenes": [{"heading": "", "narration": " "}]})

    def test_postiz_drafts_reuse_saved_ids(self):
        integration = [{"id": "test", "name": "Test account", "platform": "tiktok"}]
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            video.write_bytes(b"video")
            with patch("postiz_drafts.channels", return_value=integration), \
                 patch("postiz_drafts.request", side_effect=[
                     {"id": "asset", "path": "/uploads/video.mp4"},
                     [{"integration": "test", "postId": "draft-1"}],
                 ]) as request:
                first = clipbot.postiz_drafts.drafts(video, {"test": "caption"}, "Example")
                second = clipbot.postiz_drafts.drafts(video, {"test": "caption"}, "Example")
            self.assertEqual(first, second)
            self.assertEqual(request.call_count, 2)
            self.assertIn(b'"type": "draft"', request.call_args.args[1])

    def test_cleanup_only_removes_processed_and_unchanged_export(self):
        from contextlib import ExitStack

        cases = (
            ("processed", True, False, False),
            ("uploaded", True, False, True),
            ("rejected", True, False, True),
            ("processed", False, False, True),
            ("processed", True, True, True),
            (RuntimeError("Offline"), True, False, True),
        )
        for status, enabled, changed, kept in cases:
            with self.subTest(status=status, enabled=enabled, changed=changed), ExitStack() as stack:
                directory = stack.enter_context(tempfile.TemporaryDirectory())
                output = Path(directory)
                stack.enter_context(patch.object(clipbot, "OUTPUT", output))
                stack.enter_context(patch.object(clipbot, "update"))
                stack.enter_context(patch.object(clipbot.time, "sleep"))
                clip = output / "test.mp4"
                clip.write_bytes(b"fake export")
                subtitles = clip.with_suffix(".srt")
                subtitles.write_text("transcript", encoding="utf-8")

                def upload(*args):
                    if changed:
                        clip.write_bytes(b"replacement")
                    return "video-id"

                stack.enter_context(patch.object(youtube_api, "yt_upload", side_effect=upload))
                record = stack.enter_context(patch.object(youtube_api, "record_upload"))
                verify = stack.enter_context(patch.object(
                    youtube_api, "upload_status",
                    side_effect=status if isinstance(status, Exception) else None,
                    return_value=status))
                video_id, _ = clipbot.upload_export(clip.name, "Test", "", "private", enabled)
                self.assertEqual(video_id, "video-id")
                self.assertEqual(clip.exists(), kept)
                self.assertTrue(subtitles.exists())
                record.assert_called_once()
                self.assertEqual(verify.called, enabled)

    def test_failed_upload_or_failed_record_keeps_export(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(clipbot, "OUTPUT", Path(directory)):
            clip = Path(directory) / "test.mp4"
            clip.write_bytes(b"fake export")
            with patch.object(youtube_api, "yt_upload", side_effect=RuntimeError("Upload failed")):
                with self.assertRaises(RuntimeError):
                    clipbot.upload_export(clip.name, "Test", "", "private", True)
                self.assertTrue(clip.exists())
            with patch.object(youtube_api, "yt_upload", return_value="video-id"), \
                 patch.object(youtube_api, "record_upload", side_effect=OSError("Could not record")):
                with self.assertRaises(OSError):
                    clipbot.upload_export(clip.name, "Test", "", "private", True)
                self.assertTrue(clip.exists())

    def test_manual_delete_requires_confirmation_and_an_idle_job(self):
        from io import BytesIO

        with tempfile.TemporaryDirectory() as directory, patch.object(clipbot, "OUTPUT", Path(directory)):
            clip = Path(directory) / "test.mp4"
            clip.write_bytes(b"test file")
            for confirm, busy, expected in ((False, False, 400), (True, True, 409), (True, False, 200)):
                with patch.dict(clipbot.state, {"busy": busy, "files": [clip.name]}):
                    handler = object.__new__(clipbot.Handler)
                    handler.path = "/api/exports/delete"
                    data = ('{"name":"test.mp4","confirm":' + str(confirm).lower() + '}').encode()
                    handler.rfile = BytesIO(data)
                    handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(data))}
                    responses = []
                    handler.fail = handler.send_json = lambda status, payload: responses.append((status, payload))
                    handler.do_POST()
                    self.assertEqual(responses[0][0], expected)
                    self.assertEqual(clip.exists(), expected != 200)
                    if expected == 200:
                        self.assertEqual(responses[0][1]["freed_bytes"], 9)

    def test_windows_stream_disconnect_does_not_raise_server_error(self):
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as directory, patch.object(clipbot, "OUTPUT", Path(directory)):
            (Path(directory) / "demo.mp4").write_bytes(b"test video")
            for endpoint in ("preview", "download"):
                with self.subTest(endpoint=endpoint):
                    handler = object.__new__(clipbot.Handler)
                    handler.path = f"/api/{endpoint}?name=demo.mp4"
                    handler.headers = {}
                    handler.send_response = Mock()
                    handler.send_header = Mock()
                    handler.end_headers = Mock()
                    handler.wfile = Mock()
                    handler.wfile.write.side_effect = ConnectionAbortedError("Client closed the stream")
                    handler.do_GET()
                    handler.send_response.assert_called_once_with(200)
                    handler.wfile.write.assert_called_once()

    def test_time_roundtrip(self):
        self.assertAlmostEqual(clipbot.srt_seconds(clipbot.srt_timestamp(83.45)),83.45,places=2)

    def test_srt_and_subclip(self):
        with tempfile.TemporaryDirectory() as d:
            src=Path(d)/"a.srt"
            src.write_text("1\n00:00:01,000 --> 00:00:04,000\nHello there.\n\n2\n00:00:05,200 --> 00:00:07,100\nHow are you?\n",encoding="utf-8")
            seg=clipbot.parse_srt(src)
            self.assertEqual(len(seg),2)
            dst=Path(d)/"clip.srt"
            clipbot.make_clip_subtitle(seg,3,6,dst)
            self.assertIn("00:00:00,000 --> 00:00:01,000",dst.read_text())

    def test_pick_clips_fallback(self):
        picks=clipbot.pick_clips([],150,count=3,length=30)
        self.assertEqual(len(picks),3)
        self.assertTrue(all(0<=a<b<=150 for a,b,_,_ in picks))

    def test_auto_all_and_unlimited_count(self):
        self.assertEqual(len(clipbot.pick_clips([], 600, count=0, length=30)), 20)
        self.assertEqual(len(clipbot.pick_clips([], 600, count=11, length=30)), 11)
        self.assertEqual(len(clipbot.pick_clips([], 600, count=9999, length=30)), 20)
        self.assertIn('Auto — all usable clips', clipbot.HTML)
        self.assertNotIn('count not in (1,2,3,4,5)', Path(clipbot.__file__).read_text())

    def test_scene_highlights_are_grounded_and_distinct(self):
        picks = clipbot.pick_clips([], 90, length=20, scenes=(12, 13, 55))
        self.assertEqual([p[0] for p in picks], [10, 53])
        self.assertEqual([p[2] for p in picks], ["Visual scene change"] * 2)

    def test_gameplay_can_find_distinct_events_without_dialogue_or_cuts(self):
        signals = [(12, .4, "visual motion"), (13, .9, "audio peak"),
                   (14, .7, "visual motion"), (55, .8, "visual motion")]
        clips = clipbot.pick_clips([], 90, length=20, gameplay=signals)
        self.assertEqual([clip[:2] for clip in clips], [(7, 27), (49, 69)])
        self.assertIn("audio peak + visual motion", clips[0][2])
        self.assertEqual(clipbot.pick_clips([], 90, count=1, length=20, gameplay=[])[0][2],
                         "Evenly spaced fallback")

    def test_speech_selection_respects_complete_segments(self):
        speech = [clipbot.Segment(1, 15, "How did this happen?"),
                  clipbot.Segment(17, 25, "The explanation continues.")]
        self.assertEqual(clipbot.pick_clips(speech, 60, length=20)[0][:2], (.85, 15))

    def test_analytics_selects_revision_experiment(self):
        self.assertEqual(youtube_api.revision_experiment(
            {"views": 120, "average_view_percentage": 45}), "shorter")
        self.assertEqual(youtube_api.revision_experiment(
            {"views": 12, "average_view_percentage": 45}), "hook")

    def test_pop_captions_use_real_word_timings(self):
        from shorts_captions import ACTIVE, write_pop_captions

        words = (clipbot.Segment(.6, .9, "Turn"), clipbot.Segment(1.1, 1.5, "left"))
        cue = clipbot.Segment(.6, 1.5, "Turn left", words=words)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "captions.ass"
            write_pop_captions([cue], .5, 1.4, path)
            result = path.read_text(encoding="utf-8")
        self.assertIn("0:00:00.10,0:00:00.60", result)
        self.assertIn("0:00:00.60,0:00:00.90", result)
        self.assertIn(ACTIVE + "Turn", result)
        self.assertIn(ACTIVE + "left", result)

    def test_source_validation(self):
        self.assertEqual(validate_youtube_url('https://www.youtube.com/watch?v=abc'), 'https://www.youtube.com/watch?v=abc')
        for url in ('file:///etc/passwd', 'http://127.0.0.1:8000/private', 'https://youtube.com.bad.site/vid',
                    'https://youtube.com@internal.local/vid', 'https://www.google.com/video'):
            with self.assertRaises(ValueError):
                validate_youtube_url(url)

    def test_downloader_runs_venv_and_verifies_export(self):
        with tempfile.TemporaryDirectory() as directory:
            exported = Path(directory) / "youtube_abc.mp4"
            exported.touch()
            with patch("pipeline_addons.importlib.util.find_spec", return_value=True), \
                 patch("pipeline_addons.subprocess.run", side_effect=[
                     CompletedProcess([], 0, "2026.10.26", ""),
                     CompletedProcess([], 0, str(exported), ""),
                 ]) as execute:
                result = fetch_authorized_video("https://www.youtube.com/watch?v=abc", directory, report=lambda _: None)
            self.assertEqual(result, exported.resolve())
            args = execute.call_args.args[0]
            self.assertEqual(args[1:3], ["-m", "yt_dlp"])
            self.assertIn("--no-simulate", args)
            self.assertIn("--ignore-config", args)
            self.assertIn("after_move:filepath", args)

    def test_auto_intro_uses_source_text(self):
        self.assertIn('Brilliant moment', generate_hook('Brilliant moment. A lot more happened later.'))
        self.assertEqual(generate_hook('   '), '')

    def test_advice_and_analytics_without_live_account(self):
        with patch('youtube_api._youtube_analytics', return_value={'rows': [[120, 17, 22.1, 45.0, 4, 1]]}):
            data = youtube_api.performance('abc', '2026-11-01')
            self.assertEqual(data['views'], 120)
            self.assertEqual(data['average_view_percentage'], 45.0)
            self.assertIn('shorter', youtube_api.advice(data).lower())


    def test_dashboard_real_status_and_no_fake_publishing(self):
        data = clipbot.dashboard_data()
        self.assertEqual(len(data['workflow']), 8)
        self.assertEqual(data['workflow'][5]['key'], 'publish')
        self.assertIn(data['workflow'][5]['status'], ('optional', 'waiting'))
        self.assertEqual(data['upload_count'], 0)
        self.assertIn('capabilities', data)
        self.assertIn('next_action', data)
        self.assertIn('source_count', data)


    def test_fallback_stages_are_not_reported_as_ai_success(self):
        sample = clipbot.OUTPUT / 'test_fallback.mp4'
        sample.write_bytes(b'placeholder for dashboard only')
        try:
            with patch.dict(clipbot.state, {'busy':False, 'error':None,
                     'mode':'evenly-spaced (Whisper unavailable)', 'stage':'Finished',
                     'progress':100}):
                flow = clipbot.dashboard_data()['workflow']
                self.assertEqual(flow[1]['status'], 'skipped')
                self.assertEqual(flow[2]['status'], 'skipped')
                self.assertEqual(flow[4]['status'], 'done')
        finally:
            sample.unlink(missing_ok=True)

    def test_web_dashboard_has_controls_and_live_status(self):
        self.assertIn('id="workflowGrid"', clipbot.HTML)
        self.assertIn('/api/dashboard', clipbot.HTML)
        self.assertIn('id="nextTitle"', clipbot.HTML)
        self.assertIn('id="progressPanel"', clipbot.HTML)
        self.assertIn('id="publishPanel"', clipbot.HTML)
        self.assertIn('id="insightsPanel"', clipbot.HTML)
        self.assertIn('clipbot_source_ack_v1', clipbot.HTML)
        self.assertIn('restoreSourcePermission()', clipbot.HTML)
        self.assertIn('No YouTube account connection is needed', clipbot.HTML)

    def test_path_safety(self):
        self.assertEqual(clipbot.safe_file("../../bad/name.mp4"),"name.mp4")

if __name__ == "__main__":
    unittest.main()
