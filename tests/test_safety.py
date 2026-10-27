from app.config import CPU_SUSTAINED_SAMPLES, RAM_SHUTDOWN_PERCENT, RAM_START_BLOCK_PERCENT


def test_ram_start_threshold_blocks_new_workers_without_stopping_active(client, fake_metrics):
    fake_metrics.ram = RAM_START_BLOCK_PERCENT

    payload = client.get("/api/metrics").json()
    add_response = client.post("/api/workers/add")

    assert payload["ram_warning"] is True
    assert payload["safety_shutdown"] is False
    assert payload["active_workers"] == 1
    assert add_response.status_code == 409


def test_workers_stop_at_ram_shutdown_threshold(client, fake_metrics):
    fake_metrics.ram = RAM_SHUTDOWN_PERCENT

    payload = client.get("/api/metrics").json()

    assert payload["safety_shutdown"] is True
    assert payload["active_workers"] == 0
    assert client.post("/api/workers/add").status_code == 409


def test_worker_starts_unlock_after_ram_recovers(client, fake_metrics):
    fake_metrics.ram = RAM_SHUTDOWN_PERCENT + 1
    client.get("/api/metrics")
    fake_metrics.ram = 50.0

    recovered = client.get("/api/metrics").json()
    add_response = client.post("/api/workers/add")

    assert recovered["start_blocked"] is False
    assert add_response.status_code == 200


def test_sustained_cpu_protection_stops_workers(client, fake_metrics):
    fake_metrics.cpu = 95.0

    payload = None
    for _ in range(CPU_SUSTAINED_SAMPLES):
        payload = client.get("/api/metrics").json()

    assert payload["cpu_sustained"] is True
    assert payload["cpu_high_samples"] == payload["thresholds"]["cpu_samples"]
    assert payload["safety_source"] == "cpu"
    assert payload["active_workers"] == 0
    assert payload["safety_shutdown"] is True
