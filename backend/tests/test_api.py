"""API surface: the contract the Bangla frontend codes against."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

CONTRACT_TEXT = """EMPLOYMENT CONTRACT
Employer: Desert Rose Trading Co.
Country: Saudi Arabia
Job Title: Driver
Monthly salary: SAR 1300
Contract duration: 24 months
Accommodation: provided
Return ticket: provided
Compensation for death or injury: as per law
Visa: tourist visa
Agency fee: SAR 9500 payable in cash.
"""


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["mode"] in ("offline", "llm")
    assert body["act_sections_indexed"] >= 49


def test_meta_carries_trust_signals(client):
    body = client.get("/api/meta").json()
    assert body["agency_data_date"]
    assert body["helpline_bn"]
    assert body["disclaimer_bn"]
    assert body["agency_data_is_official"] is False  # demo data until BMET export is loaded


def test_fixed_bangla_copy_endpoint(client):
    body = client.get("/api/texts").json()
    assert "এটি আইনি পরামর্শ নয়" in body["disclaimer_bn"]
    assert "ডেমো" in body["demo_banner_bn"]


def test_licence_endpoint_found(client):
    body = client.post("/api/licence", json={"query": "RL-1001"}).json()
    assert body["status"] == "found"
    assert body["matches"][0]["record"]["rl_number"] == "RL-1001"


def test_licence_endpoint_not_found(client):
    body = client.post("/api/licence", json={"query": "RL-4242"}).json()
    assert body["status"] == "not_found"
    assert "safe" not in json.dumps(body, ensure_ascii=False).lower()


def test_fee_check_endpoint(client):
    body = client.post(
        "/api/fee-check", json={"destination": "সৌদি আরব", "quoted_fee_bdt": 400000}
    ).json()
    assert body["exceeds_cap"] is True
    assert body["cap"]["country_code"] == "SA"


def test_fee_check_negative_amount_rejected(client):
    response = client.post("/api/fee-check", json={"destination": "SA", "quoted_fee_bdt": -5})
    assert response.status_code == 422


def test_ask_endpoint(client):
    body = client.post("/api/ask", json={"question": "প্রতারণার অভিযোগ কোথায় করব?"}).json()
    assert body["answer_bn"]
    assert body["citations"]


def test_ask_endpoint_refuses_out_of_scope(client):
    body = client.post("/api/ask", json={"question": "Best tourist places in Paris?"}).json()
    assert body["refused"] is True


def test_contract_endpoint_with_text_upload(client):
    files = {"file": ("contract.txt", CONTRACT_TEXT.encode(), "text/plain")}
    data = {"answers": json.dumps({"agency_rl": "RL-1001"})}
    body = client.post("/api/contract", files=files, data=data).json()

    assert body["extraction"]["monthly_wage"]["amount"] == 1300
    flag_ids = {flag["id"] for flag in body["flags"]}
    assert "tourist_visa_for_work" in flag_ids
    assert "fee_above_cap" in flag_ids
    assert body["summary_bn"]
    assert "এটি আইনি পরামর্শ নয়" in body["disclaimer_bn"]


def test_contract_endpoint_rejects_bad_type(client):
    files = {"file": ("evil.exe", b"MZ\x90\x00binary", "application/octet-stream")}
    response = client.post("/api/contract", files=files)
    assert response.status_code == 422
    assert "সাপোর্ট" in response.json()["message_bn"]


def test_frontend_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "প্রবাসী সুরক্ষা" in response.text
