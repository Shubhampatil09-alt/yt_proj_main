def test_start_all_creates_one_real_worker_slot(client):
    response = client.get("/api/workers")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert [item["id"] for item in response.json()["workers"]] == [1]


def test_default_concurrency_limit_requires_explicit_increase(client):
    blocked = client.post("/api/workers/add")
    increased = client.post("/api/workers/increase-limit")
    added = client.post("/api/workers/add")

    assert blocked.status_code == 409
    assert increased.json()["max_workers"] == 2
    assert added.json()["count"] == 2


def test_remove_stops_highest_active_worker(client, worker_registry):
    response = client.post("/api/workers/remove")

    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert worker_registry[1].stop_calls == 1


def test_repeated_individual_stop_is_safe(client, worker_registry):
    first = client.post("/api/workers/1/stop")
    second = client.post("/api/workers/1/stop")

    assert first.status_code == 200
    assert second.status_code == 200
    assert worker_registry[1].stop_calls == 1


def test_worker_ids_are_not_reused(client):
    client.post("/api/workers/1/stop")
    response = client.post("/api/workers/add")

    assert {item["id"] for item in response.json()["workers"]} == {1, 2}


def test_stop_all_only_stops_managed_workers(client, worker_registry):
    response = client.post("/api/workers/stop-all")

    assert response.json()["count"] == 0
    assert worker_registry[1].stop_calls == 1

def test_playback_mode_is_saved_per_bot(client):
    bot = client.post("/api/bots", json={"display_name": "Repeat bot"}).json()
    bot_id = bot["bot_id"]
    response = client.patch(f"/api/bots/{bot_id}", json={"settings": {"playback_mode": "once"}})
    assert response.status_code == 200
    assert response.json()["settings"]["playback_mode"] == "once"
    saved = client.get("/api/bots").json()["bots"]
    assert next(item for item in saved if item["bot_id"] == bot_id)["settings"]["playback_mode"] == "once"
    assert client.patch(f"/api/bots/{bot_id}", json={"settings": {"playback_mode": "invalid"}}).status_code == 409


