from fastapi.testclient import TestClient


def test_jobs_endpoints_fail_closed_without_authentication(client: TestClient) -> None:
    """Until TASK-SEC-004 lands, scoped endpoints must refuse rather than guess a workspace."""
    for method, path in (("GET", "/api/v1/jobs/job_1"), ("POST", "/api/v1/jobs/job_1/cancel")):
        r = client.request(method, path)
        assert r.status_code == 401
        assert r.json()["error_code"] == "AUTH_ERROR"
