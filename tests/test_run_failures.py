"""Regression tests without network credentials or downloaded ASR models.

Run from the repository root: python -m unittest discover -s tests -v
Only external service / model dependencies are stubbed; orchestration,
the reporter and SQLite persistence are real.
"""

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from src.data.database import Database
from src.runtime import config
from src.runtime.reporter import Reporter


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_orchestration():
    dependencies = {
        "src.api.emailer": ("Emailer",),
        "src.api.icourse": ("ICourseClient",),
        "src.api.webvpn": ("WebVPNSession",),
        "src.ai.summarizer": ("Summarizer",),
        "src.ai.bucketer": ("assemble",),
        "src.ai.transcriber": ("Transcriber", "IncompleteAudioError", "NoAudioStreamError"),
        "src.pipeline.ppt_pipeline": ("PPTPipeline",),
        "src.runtime.scheduler": ("Scheduler",),
    }
    stubs = {}
    for name, attributes in dependencies.items():
        module = types.ModuleType(name)
        for attr in attributes:
            base = RuntimeError if attr.endswith("Error") else object
            setattr(module, attr, type(attr, (base,), {}))
        stubs[name] = module
    with patch.dict(sys.modules, stubs):
        runner = load_module("_runner_test", "src/pipeline/lecture_runner.py")
        sys.modules["src.pipeline.lecture_runner"] = runner
        main = load_module("_main_test", "main.py")
    return main, runner


main, runner = load_orchestration()


class FailureReportingTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.ExitStack()
        self.addCleanup(self.output.close)
        self.output.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.output.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.reporter = Reporter()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(str(Path(self.temp.name) / "test.db"))
        self.addCleanup(self.db.conn.close)

    def assertFailed(self, text):
        with self.assertRaisesRegex(RuntimeError, text):
            self.reporter.raise_if_failed()

    def add_summary(self, sub_id="lecture-1"):
        self.db.upsert_course("course-1", "Course", "Teacher")
        self.db.insert_lecture(sub_id, "course-1", "Lecture", "2026-10-28")
        self.db.update_summary_v2(sub_id, "Saved notes", "test/model")
        self.db.mark_processed(sub_id)

    def test_course_failure_does_not_hide_other_courses(self):
        client = Mock()
        client.get_course_detail.side_effect = [RuntimeError("unavailable"), {
            "title": "Course", "teacher": "Teacher", "lectures": [],
        }]
        with patch.object(config, "COURSE_IDS", ["bad", "good"]), \
                patch.object(main, "_check_session"):
            self.assertEqual(main._enumerate_lectures(client, self.db, self.reporter), [])
        self.assertEqual(client.get_course_detail.call_count, 2)
        self.assertFailed("course enumeration: bad")

    def test_lecture_failure_still_sends_successful_notes_before_run_fails(self):
        self.add_summary("good")
        lectures = [("course-1", "Course", {"sub_id": sid}) for sid in ("bad", "good")]
        scheduler = Mock()
        items = []
        with patch.object(main, "LectureRunner") as factory, \
                patch.object(main, "_check_session"):
            factory.return_value.run.side_effect = [RuntimeError("model failed"), "Saved notes"]
            main._drive_lectures(Mock(), self.db, scheduler, Mock(), Mock(),
                                 self.reporter, lectures, items)
        emailer = Mock()
        emailer.send.return_value = True
        main._send_email(emailer, self.db, self.reporter, items)
        self.assertIsNotNone(self.db.get_lecture("good")["emailed_at"])
        self.assertEqual(scheduler.audio_downloader.release.call_count, 2)
        self.assertFailed("lecture processing: bad")

    def test_failed_email_remains_unsent_for_next_run(self):
        for failure in (False, RuntimeError("SMTP unavailable")):
            with self.subTest(failure=failure):
                self.add_summary()
                self.reporter = Reporter()
                emailer = Mock()
                if isinstance(failure, Exception):
                    emailer.send.side_effect = failure
                else:
                    emailer.send.return_value = failure
                main._send_email(emailer, self.db, self.reporter, [])
                self.assertIsNone(self.db.get_lecture("lecture-1")["emailed_at"])
                self.assertFailed("email delivery")

    def test_optional_email_and_no_new_lectures_are_successful(self):
        main._send_email(None, self.db, self.reporter, [])
        self.reporter.raise_if_failed()

    def test_catalog_failure_continues_without_unbound_rows(self):
        client = Mock()
        client.discover_terms.return_value = [
            {"code": "bad", "name": "Bad term", "count": 1},
            {"code": "good", "name": "Good term", "count": 1},
        ]
        client.list_semester_courses.side_effect = [RuntimeError("API unavailable"), []]
        with patch.object(main, "_check_session"):
            main._crawl_semester_catalog(client, self.db, self.reporter)
        self.assertEqual(client.list_semester_courses.call_count, 2)
        self.assertFailed("catalog crawl: bad")

    def test_audio_wait_timeout_is_reported_as_failure(self):
        scheduler = Mock()
        scheduler.audio_downloader.get.side_effect = TimeoutError("audio wait timed out")
        lecture_runner = runner.LectureRunner.__new__(runner.LectureRunner)
        lecture_runner._scheduler = scheduler
        lecture_runner._client = Mock()
        lecture_runner._db = Mock()
        lecture_runner._reporter = self.reporter
        with self.assertRaises(TimeoutError):
            lecture_runner._get_transcript(None, "course-1", "lecture-1")
        lecture_runner._db.update_error.assert_called_once()

    def test_resummarize_failure_reaches_run_status(self):
        self.add_summary()
        self.db.update_transcript("lecture-1", "Existing transcript")
        self.db.conn.execute("UPDATE lectures SET summary_format_version = 0")
        self.db.conn.commit()
        summarizer = Mock()
        summarizer.summarize.side_effect = RuntimeError("all providers failed")
        with patch.object(runner.bucketer, "assemble", return_value=("prompt", "flat")):
            runner.resummarize_old_lectures(Mock(), self.db, summarizer, Mock(),
                                           self.reporter, [], ["course-1"])
        self.assertEqual(self.db.get_lecture("lecture-1")["summary"], "Saved notes")
        self.assertFailed("resummarize lecture: lecture-1")

    def test_run_marks_failure_after_email_and_shutdown(self):
        events = []
        scheduler = Mock()
        scheduler.shutdown.side_effect = lambda: events.append("shutdown")

        def failed_enumeration(*args):
            self.reporter.record_failure("course enumeration", "bad")
            return []

        with contextlib.ExitStack() as stack:
            for attr in ("Transcriber", "Summarizer", "ICourseClient", "login_with_retry"):
                stack.enter_context(patch.object(main, attr))
            stack.enter_context(patch.object(main, "Reporter", return_value=self.reporter))
            stack.enter_context(patch.object(main, "Database", return_value=self.db))
            stack.enter_context(patch.object(main, "Scheduler", return_value=scheduler))
            stack.enter_context(patch.object(main, "_crawl_semester_catalog"))
            stack.enter_context(patch.object(main, "_enumerate_lectures", side_effect=failed_enumeration))
            stack.enter_context(patch.object(main, "_send_email", side_effect=lambda *a: events.append("email")))
            stack.enter_context(patch.object(config, "COURSE_IDS", ["bad"]))
            stack.enter_context(patch.object(config, "RESUMMARIZE_OLD_ENABLED", False))
            stack.enter_context(patch.object(config, "SMTP_EMAIL", ""))
            with self.assertRaisesRegex(RuntimeError, "course enumeration"):
                main.run()
        self.assertEqual(events, ["shutdown", "email"])


if __name__ == "__main__":
    unittest.main()
