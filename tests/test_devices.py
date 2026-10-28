from tests.conftest import first_home, login
from tests.fake_tibber import DATA_HOME, DEVICE, GQL_HOME


def test_homes_merge_and_ids_are_opaque(connected):
    body = connected.get("/v1/homes").json()
    assert len(body["homes"]) == 1
    home = body["homes"][0]
    assert home["name"] == "Test Home" and home["timeZone"] == "Europe/Stockholm"
    assert home["sources"] == {"prices": True, "devices": True} and home["realTimeConsumption"] is True
    assert home["id"] not in (GQL_HOME, DATA_HOME)
    assert GQL_HOME not in connected.get("/v1/homes").text


def test_devices_list_and_unknown_fields_tolerated(connected):
    hid = first_home(connected)
    body = connected.get(f"/v1/homes/{hid}/devices").json()
    assert body["available"] is True
    (dev,) = body["devices"]
    assert dev["name"] == "Garage charger" and dev["brand"] == "Acme"
    assert dev["historyResolutions"] == ["hour", "day"]  # unknown resolution dropped
    assert dev["id"] != DEVICE
    text = connected.get(f"/v1/homes/{hid}/devices").text
    assert DEVICE not in text and "SECRET-QR-CODE" not in text


def test_device_detail_generic_capabilities_and_privacy(connected, fake):
    hid = first_home(connected)
    did = connected.get(f"/v1/homes/{hid}/devices").json()["devices"][0]["id"]
    r = connected.get(f"/v1/homes/{hid}/devices/{did}")
    body = r.json()
    assert r.status_code == 200, body
    caps = {c["id"]: c for c in body["capabilities"]}
    assert caps["power"] == {
        "id": "power",
        "kind": "number",
        "description": "Charging power",
        "value": 7.4,
        "unit": "kW",
        "availableValues": None,
    }
    assert caps["sessions"]["kind"] == "integer" and caps["sessions"]["value"] == 12
    assert caps["state"]["kind"] == "enum" and caps["state"]["availableValues"] == ["idle", "charging"]
    assert caps["future"]["kind"] == "unknown"
    attrs = {a["id"]: a for a in body["attributes"]}
    assert attrs["connectivity.wifi"] == {
        "id": "connectivity.wifi",
        "kind": "connectivity",
        "description": None,
        "value": "connected",
        "availableValues": None,
    }
    assert attrs["something.new"]["kind"] == "unknown"
    assert body["maxRetentionDays"] == 90 and body["lastSeen"].startswith("2026-10-28")
    for secret in ("MyWifi", "aa:bb:cc", "192.0.2.10", "SECRET-QR-CODE", DEVICE):
        assert secret not in r.text


def test_device_state_uses_etag_revalidation(connected, fake):
    hid = first_home(connected)
    did = connected.get(f"/v1/homes/{hid}/devices").json()["devices"][0]["id"]
    connected.get(f"/v1/homes/{hid}/devices/{did}")
    db = connected.app.state.db
    db._x("UPDATE cache SET fetched_at = fetched_at - 600 WHERE key=?", (f"data:device:{did}",))
    r = connected.get(f"/v1/homes/{hid}/devices/{did}")
    assert r.status_code == 200 and r.json()["capabilities"]
    assert fake.calls[-1][1].endswith(f"/devices/{DEVICE}")  # revalidated upstream (fake answered 304)


def test_history_cursor_paging_is_opaque(connected, fake):
    hid = first_home(connected)
    did = connected.get(f"/v1/homes/{hid}/devices").json()["devices"][0]["id"]
    first = connected.get(
        f"/v1/homes/{hid}/devices/{did}/history", params={"resolution": "hour", "since": "2026-10-01T00:00:00+02:00"}
    )
    body = first.json()
    assert first.status_code == 200, body
    assert [i["data"]["energy"] for i in body["items"]] == [1.0, 2.0]
    assert body["next"] and body["prev"] is None and "page2" not in first.text
    second = connected.get(f"/v1/homes/{hid}/devices/{did}/history", params={"cursor": body["next"]}).json()
    assert [i["data"]["energy"] for i in second["items"]] == [3.0]
    assert second["next"] is None and second["prev"]


def test_history_validation(connected):
    hid = first_home(connected)
    did = connected.get(f"/v1/homes/{hid}/devices").json()["devices"][0]["id"]
    base = f"/v1/homes/{hid}/devices/{did}/history"
    assert connected.get(base, params={"resolution": "hour"}).status_code == 422
    assert connected.get(base, params={"cursor": "A" * 22}).status_code == 422  # unknown cursor
    assert connected.get(base, params={"cursor": "A" * 22, "resolution": "hour"}).status_code == 422
    assert connected.get(base, params={"resolution": "week", "since": "2026-10-01T00:00:00Z"}).status_code == 422


def test_object_level_authorisation_denies_unknown_ids(connected):
    hid = first_home(connected)
    assert connected.get("/v1/homes/" + "A" * 22 + "/devices").status_code == 404
    assert connected.get(f"/v1/homes/{hid}/devices/" + "B" * 22).status_code == 404
    # Upstream ids are not accepted in place of opaque ids.
    assert connected.get(f"/v1/homes/{GQL_HOME}/prices").status_code in (404, 422)


def test_devices_unavailable_without_oauth(client, fake):
    from tests.fake_tibber import PAT

    login(client)
    client.put("/v1/settings/personal-token", json={"token": PAT})
    hid = first_home(client)
    body = client.get(f"/v1/homes/{hid}/devices").json()
    assert body == {"available": False, "devices": [], "meta": body["meta"]}
