"""Valid trip inputs for the shared storage, API and scheduler tests."""


def trip_request(**changes):
    return {"workflow": "hk_trip", "trip": {
        "arrival": "2026-11-21T09:00",
        "departure": "2026-11-22T20:00",
        "party_size": 2,
        "budget_hkd": "3500.00",
        **changes,
    }}
