from app.database import JobHistory


def test_job_history_counts_and_recent_results(tmp_path):
    history = JobHistory(tmp_path / "history.db")
    history.initialize()
    history.add("Local sample A", "completed")
    history.add("Local sample B", "failed", "Playback timeout")

    assert history.counts() == {"completed": 1, "failed": 1}
    assert history.recent()[0]["test_name"] == "Local sample B"

