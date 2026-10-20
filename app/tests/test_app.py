import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import app as dashboard


@pytest.fixture(autouse=True)
def reset_cache():
    dashboard._cache.update(data=None, last_success_at=None, attempted_at=None, error=None)


def event(service="ec2-ap-southeast-2", status="3", **extra):
    return {"service": service, "status": status, "summary": "Test incident", **extra}


def service_status(events, name="EC2"):
    return next(s for s in dashboard.build_service_statuses(events) if s["service"] == name)


def run_fetch(handler):
    real_client = httpx.AsyncClient
    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    with patch.object(dashboard.httpx, "AsyncClient", side_effect=factory):
        return asyncio.run(dashboard.fetch_aws_events())


def test_real_feed_redirect_and_utf16_encoding():
    requests = []
    def handler(request):
        requests.append(request.url)
        if request.url.host == "status.aws.amazon.com":
            return httpx.Response(301, headers={"Location": "https://health.aws.amazon.com/public/currentevents"})
        return httpx.Response(200, content=json.dumps([event()]).encode("utf-16"))
    result = run_fetch(handler)
    assert len(requests) == 2
    assert result["error"] is None
    assert result["last_success_at"]
    assert service_status(result["data"])["status"] == "red"


def test_missing_region_field_uses_exact_service_identifier():
    assert service_status([event(service_name="Amazon Elastic Compute Cloud")])["status"] == "red"
    assert service_status([event("ec2-us-east-1")])["status"] == "green"


@pytest.mark.parametrize("identifier,name", [("iam", "IAM"), ("cloudfront", "CloudFront")])
def test_global_services(identifier, name):
    result = service_status([event(identifier)], name)
    assert result["status"] == "red"
    assert result["scope"] == "Global"


def test_multiple_services_use_individual_current_status():
    events = [event("multipleservices-me-central-1", impacted_services={
        "ec2-ap-southeast-2": {"current": "3"},
        "cloudfront": {"current": "0", "max": "3"},
        "ec2-us-east-1": {"current": "3"},
    })]
    assert service_status(events)["status"] == "red"
    assert service_status(events, "CloudFront")["status"] == "yellow"
    assert service_status(events, "RDS")["status"] == "green"


def test_active_incident_wins_over_resolved_regardless_of_order():
    for events in ([event(status="0"), event()], [event(), event(status="0")]):
        assert service_status(events)["status"] == "red"


def test_future_status_code_is_unknown():
    assert service_status([event(status="99")])["status"] == "unknown"


def test_empty_valid_feed_is_not_an_outage():
    result = run_fetch(lambda request: httpx.Response(200, json=[]))
    assert result["error"] is None
    assert all(s["status"] == "green" for s in dashboard.build_service_statuses(result["data"]))


@pytest.mark.parametrize("payload", [{"unexpected": []}, [None], [{}], [event(impacted_services=[])], [event(summary={})]])
def test_schema_change_is_not_interpreted_as_healthy(payload):
    result = run_fetch(lambda request: httpx.Response(200, json=payload))
    assert result["error"]
    assert result["data"] is None


def test_feed_failure_is_unknown_and_does_not_break_liveness():
    def handler(request):
        raise httpx.ConnectError("upstream unavailable", request=request)
    result = run_fetch(handler)
    assert result["error"]
    with TestClient(dashboard.app) as client:
        assert client.get("/health").json() == {"status": "healthy"}
        response = client.get("/api/status").json()
        assert response["data_state"] == "unavailable"
        assert response["last_success_at"] is None
        assert all(s["status"] == "unknown" for s in response["services"])
        page = client.get("/")
        assert page.status_code == 200
        assert 'role="alert"' in page.text
        assert "Current status is unknown" in page.text


def test_failure_preserves_last_success_time_and_marks_data_stale():
    initial = run_fetch(lambda request: httpx.Response(200, json=[event()]))
    dashboard._cache["attempted_at"] = None
    result = run_fetch(lambda request: httpx.Response(503))
    assert result["last_success_at"] == initial["last_success_at"]
    assert result["data"] == initial["data"]
    with TestClient(dashboard.app) as client:
        snapshot = client.get("/api/status").json()
        assert snapshot["data_state"] == "stale"
        assert all(s["status"] == "unknown" for s in snapshot["services"])


def test_successful_cache_does_not_refetch_or_change_timestamp():
    first = run_fetch(lambda request: httpx.Response(200, json=[]))
    def unexpected(request):
        pytest.fail("Should use cache")
    second = run_fetch(unexpected)
    assert second["last_success_at"] == first["last_success_at"]


def test_home_and_api_render_from_successful_cache():
    run_fetch(lambda request: httpx.Response(200, json=[event(summary="<script>alert(1)</script>")]))
    with TestClient(dashboard.app) as client:
        assert client.get("/api/status").json()["data_state"] == "ok"
        html = client.get("/").text
        assert "&lt;script&gt;" in html
        assert "<script>alert(1)</script>" not in html
        assert 'role="alert"' not in html


def test_lambda_uses_observed_public_feed_identifier():
    assert service_status([event("lambda-ap-southeast-2")], "Lambda")["status"] == "red"
