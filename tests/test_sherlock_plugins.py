from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import httpx
import pytest

from omoika import Registry, TransformPayload, load_plugins_fs
from omoika.ipc_worker import ObWorker
from omoika.output import set_progress_callback
from omoika.results import normalize_result


PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "paid_plugins"


def _reset_registry() -> None:
    Registry.labels.clear()
    Registry.plugins.clear()
    Registry.ui_labels.clear()
    Registry.transforms_map.clear()


def _element_value(entity: dict, label: str):
    for row in entity.get("elements", []):
        elements = row if isinstance(row, list) else [row]
        for element in elements:
            if element.get("label") == label:
                return element.get("value")
    raise AssertionError(f"Missing element label {label!r}")


async def _load_transform():
    _reset_registry()
    load_plugins_fs(str(PLUGIN_ROOT))
    plugin_cls = await Registry.get_entity("sherlock_username")
    mapping = Registry.find_transforms("sherlock_username", plugin_cls.version)
    return mapping["run_sherlock"]


async def _execute_transform(transform_fn, **kwargs):
    result = transform_fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    if inspect.isasyncgen(result):
        return [item async for item in result]
    if inspect.isgenerator(result):
        return list(result)
    return result


class DummyResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.mark.asyncio
async def test_sherlock_plugins_load_and_claimed_results(monkeypatch):
    transform_fn = await _load_transform()
    module = sys.modules[transform_fn.__module__]

    async def fake_manifest_loader(_source):
        return (
            {
                "ClaimedSite": {
                    "url": "https://claimed.example/{}",
                    "urlMain": "https://claimed.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                    "username_claimed": "taken",
                },
                "MessageSite": {
                    "url": "https://message.example/{}",
                    "urlMain": "https://message.example",
                    "errorType": "message",
                    "errorMsg": "User not found",
                    "username_claimed": "taken",
                },
                "RegexSite": {
                    "url": "https://regex.example/{}",
                    "urlMain": "https://regex.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                    "regexCheck": "^[a-z]+$",
                    "username_claimed": "taken",
                },
            },
            "bundled",
        )

    async def fake_request(self, method, url, **kwargs):
        if "claimed.example" in url:
            return DummyResponse(200, "profile")
        if "message.example" in url:
            return DummyResponse(200, "User not found")
        if "regex.example" in url:
            return DummyResponse(200, "profile")
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(module, "_load_manifest_payload", fake_manifest_loader)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    result = await _execute_transform(
        transform_fn,
        entity=TransformPayload(id="seed-1", label="Sherlock Username", username="bad!"),
        cfg={"include_report": True},
    )
    normalized = normalize_result(result, default_edge_label=getattr(transform_fn, "edge_label", ""))

    reports = [item for item in normalized if item.get("label") == "Sherlock Report"]
    findings = [item for item in normalized if item.get("label") == "Sherlock Finding"]

    assert len(reports) == 1
    assert len(findings) == 1

    report = reports[0]
    finding = findings[0]

    assert _element_value(report, "Claimed") == "1"
    assert _element_value(report, "Available") == "1"
    assert _element_value(report, "Illegal") == "1"
    assert _element_value(report, "Matched Sites") == "ClaimedSite"
    assert report["resolved_username"] == "bad!"

    assert _element_value(finding, "Profile Url") == "https://claimed.example/bad%21"
    assert _element_value(finding, "Site") == "ClaimedSite"
    assert _element_value(finding, "Resolved Username") == "bad!"
    assert finding["profile_url"] == "https://claimed.example/bad%21"
    assert finding["resolved_username"] == "bad!"


@pytest.mark.asyncio
async def test_sherlock_variants_and_optional_statuses(monkeypatch):
    transform_fn = await _load_transform()
    module = sys.modules[transform_fn.__module__]

    async def fake_manifest_loader(_source):
        return (
            {
                "AvailableSite": {
                    "url": "https://available.example/{}",
                    "urlMain": "https://available.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                    "username_claimed": "taken",
                },
                "RegexSite": {
                    "url": "https://regex.example/{}",
                    "urlMain": "https://regex.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                    "regexCheck": "^[a-z]+$",
                    "username_claimed": "taken",
                },
            },
            "bundled",
        )

    async def fake_request(self, method, url, **kwargs):
        if "available.example" in url:
            return DummyResponse(404, "missing")
        if "regex.example" in url:
            return DummyResponse(200, "profile")
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(module, "_load_manifest_payload", fake_manifest_loader)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    result = await _execute_transform(
        transform_fn,
        entity=TransformPayload(id="seed-2", label="Sherlock Username", username="alice{?}smith"),
        cfg={"include_available": True, "include_illegal": True, "include_report": True},
    )
    normalized = normalize_result(result, default_edge_label=getattr(transform_fn, "edge_label", ""))

    reports = [item for item in normalized if item.get("label") == "Sherlock Report"]
    findings = [item for item in normalized if item.get("label") == "Sherlock Finding"]

    assert len(reports) == 3
    assert len(findings) == 0

    report_usernames = {_element_value(report, "Resolved Username") for report in reports}
    assert report_usernames == {"alice_smith", "alice-smith", "alice.smith"}

    matched_sites = {_element_value(report, "Matched Sites") for report in reports}
    assert matched_sites == {""}


@pytest.mark.asyncio
async def test_sherlock_skips_claimed_urls_without_username(monkeypatch):
    transform_fn = await _load_transform()
    module = sys.modules[transform_fn.__module__]

    async def fake_manifest_loader(_source):
        return (
            {
                "ClaimedSite": {
                    "url": "https://claimed.example/profile",
                    "urlMain": "https://claimed.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                },
            },
            "bundled",
        )

    async def fake_request(self, method, url, **kwargs):
        if "claimed.example" in url:
            return DummyResponse(200, "profile")
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(module, "_load_manifest_payload", fake_manifest_loader)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    result = await _execute_transform(
        transform_fn,
        entity=TransformPayload(id="seed-4", label="Sherlock Username", username="alice"),
        cfg={},
    )
    normalized = normalize_result(result, default_edge_label=getattr(transform_fn, "edge_label", ""))

    assert normalized == []


@pytest.mark.asyncio
async def test_sherlock_emits_progress_events(monkeypatch):
    transform_fn = await _load_transform()
    module = sys.modules[transform_fn.__module__]

    async def fake_manifest_loader(_source):
        return (
            {
                "ClaimedSite": {
                    "url": "https://claimed.example/{}",
                    "urlMain": "https://claimed.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                },
                "AvailableSite": {
                    "url": "https://available.example/{}",
                    "urlMain": "https://available.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                },
            },
            "bundled",
        )

    async def fake_request(self, method, url, **kwargs):
        if "claimed.example" in url:
            return DummyResponse(200, "profile")
        if "available.example" in url:
            return DummyResponse(404, "missing")
        raise AssertionError(f"Unexpected URL {url}")

    progress_events: list[dict] = []

    monkeypatch.setattr(module, "_load_manifest_payload", fake_manifest_loader)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    set_progress_callback(progress_events.append)
    try:
        await _execute_transform(
            transform_fn,
            entity=TransformPayload(id="seed-3", label="Sherlock Username", username="alice"),
            cfg={},
        )
    finally:
        set_progress_callback(None)

    assert progress_events[0] == {"message": "Loading Sherlock site manifest", "percent": 5}
    assert [event["message"] for event in progress_events[1:-1]] == [
        "1/2 sites searched for alice",
        "2/2 sites searched for alice",
    ]
    assert [event["percent"] for event in progress_events[1:-1]] == [50, 95]
    assert progress_events[-1] == {"message": "Sherlock complete", "percent": 100}


@pytest.mark.asyncio
async def test_sherlock_streams_findings_as_sites_complete(monkeypatch):
    transform_fn = await _load_transform()
    module = sys.modules[transform_fn.__module__]

    async def fake_manifest_loader(_source):
        return (
            {
                "SlowSite": {
                    "url": "https://slow.example/{}",
                    "urlMain": "https://slow.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                },
                "FastSite": {
                    "url": "https://fast.example/{}",
                    "urlMain": "https://fast.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                },
            },
            "bundled",
        )

    async def fake_request(self, method, url, **kwargs):
        if "slow.example" in url:
            await asyncio.sleep(0.02)
            return DummyResponse(200, "profile")
        if "fast.example" in url:
            await asyncio.sleep(0.001)
            return DummyResponse(200, "profile")
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(module, "_load_manifest_payload", fake_manifest_loader)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    stream = transform_fn(
        entity=TransformPayload(id="seed-5", label="Sherlock Username", username="alice"),
        cfg={},
    )
    assert inspect.isasyncgen(stream)

    yielded = []
    async for item in stream:
        yielded.append(item)

    normalized = normalize_result(yielded, default_edge_label=getattr(transform_fn, "edge_label", ""))
    profile_urls = [_element_value(item, "Profile Url") for item in normalized]
    assert profile_urls == [
        "https://fast.example/alice",
        "https://slow.example/alice",
    ]


@pytest.mark.asyncio
async def test_sherlock_worker_runs_streaming_transform_without_self_error(monkeypatch):
    _reset_registry()
    worker = ObWorker()
    worker.ensure_plugins(str(PLUGIN_ROOT))
    module = sys.modules["plugins.transforms.sherlock"]

    async def fake_manifest_loader(_source):
        return (
            {
                "ClaimedSite": {
                    "url": "https://claimed.example/{}",
                    "urlMain": "https://claimed.example",
                    "errorType": "status_code",
                    "errorCode": 404,
                },
            },
            "bundled",
        )

    async def fake_request(self, method, url, **kwargs):
        if "claimed.example" in url:
            return DummyResponse(200, "profile")
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(module, "_load_manifest_payload", fake_manifest_loader)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    edge_label, result = await worker.run_transform(
        source={
            "entity": {
                "id": "seed-6",
                "label": "Sherlock Username",
                "transform": "Run Sherlock",
                "data": {
                    "label": "Sherlock Username",
                    "username": "alice",
                },
            }
        },
        plugins_path=str(PLUGIN_ROOT),
        cfg={},
    )

    normalized = normalize_result(
        [item async for item in result],
        default_edge_label=edge_label,
    )
    assert [_element_value(item, "Profile Url") for item in normalized] == [
        "https://claimed.example/alice"
    ]
