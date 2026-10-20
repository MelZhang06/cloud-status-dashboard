"""Public AWS event dashboard. Feed availability is separate from app health."""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

AWS_STATUS_FEED = "https://status.aws.amazon.com/data.json"
REGION_FILTER = os.getenv("AWS_REGION", "ap-southeast-2")
# Exact public-feed service identifiers, not substring matches on display names.
MONITORED_SERVICES = {
    "EC2": f"ec2-{REGION_FILTER}",
    "S3": f"s3-{REGION_FILTER}",
    "RDS": f"rds-{REGION_FILTER}",
    "Lambda": f"lambda-{REGION_FILTER}",
    "ECS": f"ecs-{REGION_FILTER}",
    "DynamoDB": f"dynamodb-{REGION_FILTER}",
    "CloudFront": "cloudfront",
    "VPC": f"vpc-{REGION_FILTER}",
    "IAM": "iam",
    "CloudWatch": f"cloudwatch-{REGION_FILTER}",
}
CACHE_TTL_SECONDS = 60
logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="Cloud Status Dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_cache = {"data": None, "last_success_at": None, "attempted_at": None, "error": None}
_fetch_lock = asyncio.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_events(payload):
    """Reject a changed schema rather than silently interpreting it as no incidents."""
    if isinstance(payload, dict) and "current" in payload:
        payload = payload["current"]
    if not isinstance(payload, list):
        raise ValueError("Expected a list of current events")
    for event in payload:
        if not isinstance(event, dict):
            raise ValueError("Invalid event")
        service = event.get("service")
        impacted = event.get("impacted_services", {})
        if not isinstance(service, str) or not service:
            raise ValueError("Missing event service identifier")
        if not isinstance(event.get("summary", ""), str):
            raise ValueError("Invalid event summary")
        if not isinstance(impacted, dict):
            raise ValueError("Invalid impacted services")
        for key, detail in impacted.items():
            if not isinstance(key, str) or not isinstance(detail, dict):
                raise ValueError("Invalid impacted service")
    return payload


async def fetch_aws_events():
    """Cache successful results; keep last-good data but mark it stale on failure."""
    async with _fetch_lock:
        now = time.monotonic()
        attempted = _cache["attempted_at"]
        if attempted is not None and now - attempted < CACHE_TTL_SECONDS:
            return dict(_cache)
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(AWS_STATUS_FEED)
                response.raise_for_status()
                # json.loads(bytes) detects the UTF-16 BOM used by the current feed.
                events = validate_events(json.loads(response.content))
            _cache.update(data=events, last_success_at=utc_now(), error=None)
        except (httpx.HTTPError, ValueError, UnicodeError) as exc:
            logger.warning("AWS public feed unavailable: %s", exc)
            _cache["error"] = "AWS public feed unavailable or its format has changed"
        _cache["attempted_at"] = time.monotonic()
        return dict(_cache)


def event_status(code):
    # Observed public-feed codes: 0 resolved, 1/2 informational/degraded, 3 disrupted.
    # Unknown future codes stay unknown instead of being assumed healthy.
    return {"0": "yellow", "1": "yellow", "2": "yellow", "3": "red"}.get(str(code), "unknown")


def build_service_statuses(events, available=True):
    statuses = []
    for name, identifier in MONITORED_SERVICES.items():
        matches = []
        for event in events or []:
            impacted = event.get("impacted_services", {})
            if identifier in impacted:
                matches.append((event_status(impacted[identifier].get("current")), event))
            elif event["service"] == identifier:
                matches.append((event_status(event.get("status")), event))
        if not available:
            status, message = "unknown", "Current data unavailable"
        elif matches:
            # Do not let a resolved/older event hide another active disruption.
            priority = {"yellow": 1, "unknown": 2, "red": 3}
            status, event = max(matches, key=lambda item: priority[item[0]])
            message = event.get("summary") or "Reported public event"
        else:
            status, message = "green", "No matching public event"
        statuses.append({
            "service": name,
            "scope": "Global" if name in ("IAM", "CloudFront") else REGION_FILTER,
            "status": status,
            "message": message,
        })
    return statuses


async def status_snapshot():
    result = await fetch_aws_events()
    state = "unavailable" if result["data"] is None else "stale" if result["error"] else "ok"
    return {
        "region": REGION_FILTER,
        "checked_at": utc_now(),
        "last_success_at": result["last_success_at"],
        "data_state": state,
        "error": result["error"],
        "services": build_service_statuses(result["data"], available=state == "ok"),
    }


@app.get("/health")
async def health():
    # ALB liveness must not depend on the availability of a third-party feed.
    return {"status": "healthy"}


@app.get("/api/status")
async def api_status():
    return await status_snapshot()


@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context=await status_snapshot())
