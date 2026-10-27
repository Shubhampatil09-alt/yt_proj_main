from types import SimpleNamespace
from threading import Event

from app.browser_worker import BrowserWorker
from app.database import JobHistory


class FakeDriver:
    def __init__(self, states=None, error=None):
        self.service = SimpleNamespace(process=None)
        self.states = list(states or [])
        self.error = error
        self.quit_called = False
        self.url = None

    def get(self, url):
        self.url = url

    def execute_script(self, script):
        if "readyState >= 2" in script:
            return True
        if "return v.play" in script:
            return None
        if self.error:
            raise self.error
        if self.states:
            return self.states.pop(0)
        return {"currentTime": 1, "duration": 1, "paused": False, "ended": True, "error": None}

    def quit(self):
        self.quit_called = True


def test_browser_worker_completes_local_playback_and_quits(tmp_path):
    history = JobHistory(tmp_path / "worker.db")
    history.initialize()
    driver = FakeDriver([
        {"currentTime": .5, "duration": 1, "paused": False, "ended": False, "error": None},
        {"currentTime": 1, "duration": 1, "paused": False, "ended": True, "error": None},
    ])
    worker = BrowserWorker(1, "http://127.0.0.1:8000/playback-test", history, lambda _: driver)
    worker.POLL_SECONDS = 0.01

    worker.start()
    worker.join(2)
    state = worker.snapshot()

    assert state["status"] == "Stopped"
    assert state["playback_state"] == "completed"
    assert state["progress_percent"] == 100.0
    assert driver.quit_called is True
    assert history.counts()["completed"] == 1
    run = history.get_run(state["run_id"])
    assert run["final_status"] == "completed"
    assert [item["state"] for item in run["transitions"]] == ["Starting", "Running", "completed"]


def test_browser_failure_is_recorded_and_driver_is_closed(tmp_path):
    history = JobHistory(tmp_path / "failure.db")
    history.initialize()
    driver = FakeDriver(error=RuntimeError("browser vanished"))
    worker = BrowserWorker(4, "http://127.0.0.1:8000/playback-test", history, lambda _: driver)
    worker.POLL_SECONDS = 0.01

    worker.start()
    worker.join(2)

    assert worker.snapshot()["status"] == "Failed"
    assert worker.snapshot()["last_error"] == "browser vanished"
    assert driver.quit_called is True
    assert history.counts()["failed"] == 1
    assert history.get_run(worker.snapshot()["run_id"])["final_status"] == "failed"


def test_repeat_keeps_browser_alive_and_can_switch_to_once(tmp_path):
    history = JobHistory(tmp_path / "repeat.db")
    history.initialize()
    replayed = Event()

    class RepeatDriver(FakeDriver):
        def execute_script(self, script):
            if "v.currentTime=0" in script:
                replayed.set()
            return super().execute_script(script)

    driver = RepeatDriver()
    worker = BrowserWorker(2, "http://127.0.0.1:8000/playback-test", history,
                           lambda _: driver, playback_mode="repeat")
    worker.POLL_SECONDS = 0.01
    try:
        worker.start()
        assert replayed.wait(2)
        assert worker.snapshot()["status"] == "Running"
        assert not driver.quit_called
        worker.playback_mode = "once"
        worker.join(2)
        assert worker.snapshot()["playback_state"] == "completed"
        assert driver.quit_called
    finally:
        worker.stop()
        worker.join(2)


def test_repeat_can_be_stopped(tmp_path):
    history = JobHistory(tmp_path / "repeat-stop.db")
    history.initialize()
    worker = BrowserWorker(3, "http://127.0.0.1:8000/playback-test", history,
                           lambda _: FakeDriver(), playback_mode="repeat")
    worker.start()
    worker.stop()
    worker.join(2)
    assert worker.snapshot()["status"] == "Stopped"
    assert history.counts()["completed"] == 0
