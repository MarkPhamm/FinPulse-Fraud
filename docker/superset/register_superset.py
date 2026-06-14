"""Register FinPulse database connections + datasets in Superset via its REST API.

Idempotent: skips databases/datasets that already exist by name. Run inside
the superset container so the in-network SQLAlchemy URIs resolve and the
drivers (pinotdb, pyhive) are importable:

    docker compose exec superset python /app/register_superset.py

This wires the two serving layers Superset queries:
  - Pinot  (hybrid table transactions_scored)        -> live, pre-aggregated
  - Presto (hive catalog: analytics.*, curated.*)     -> granular ad-hoc SQL

Dashboards themselves are built in the UI (Step 10c); this script only
creates the connections + datasets they sit on.
"""

import sys
import requests

BASE = "http://localhost:8088"
USER = "admin"
PASSWORD = "admin"

PINOT_URI = "pinot://pinot-broker:8099/query/sql?controller=http://pinot-controller:9000"
PRESTO_URI = "presto://presto-coordinator:8080/hive/default"

DATABASES = [
    {"database_name": "Pinot - finpulse", "sqlalchemy_uri": PINOT_URI},
    {"database_name": "Presto - finpulse", "sqlalchemy_uri": PRESTO_URI},
]

# (database_name, schema, table)
DATASETS = [
    ("Pinot - finpulse", None, "transactions_scored"),
    ("Presto - finpulse", "analytics", "transactions_enriched"),
    ("Presto - finpulse", "analytics", "scored"),
    ("Presto - finpulse", "curated", "merchant_directory"),
]


def session_and_headers():
    s = requests.Session()
    r = s.post(f"{BASE}/api/v1/security/login",
               json={"username": USER, "password": PASSWORD,
                     "provider": "db", "refresh": True})
    r.raise_for_status()
    token = r.json()["access_token"]
    csrf = s.get(f"{BASE}/api/v1/security/csrf_token/",
                 headers={"Authorization": f"Bearer {token}"}).json()["result"]
    headers = {
        "Authorization": f"Bearer {token}",
        "X-CSRFToken": csrf,
        "Referer": BASE,
        "Content-Type": "application/json",
    }
    return s, headers


def existing_databases(s, headers):
    r = s.get(f"{BASE}/api/v1/database/?q=(page_size:100)", headers=headers)
    r.raise_for_status()
    return {d["database_name"]: d["id"] for d in r.json()["result"]}


def ensure_databases(s, headers):
    have = existing_databases(s, headers)
    for db in DATABASES:
        name = db["database_name"]
        if name in have:
            print(f"  database exists: {name} (id={have[name]})")
            continue
        payload = {
            "database_name": name,
            "sqlalchemy_uri": db["sqlalchemy_uri"],
            "expose_in_sqllab": True,
        }
        r = s.post(f"{BASE}/api/v1/database/", headers=headers, json=payload)
        if r.status_code in (200, 201):
            print(f"  created database: {name} (id={r.json()['id']})")
        else:
            print(f"  FAILED database {name}: {r.status_code} {r.text}")
    return existing_databases(s, headers)


def existing_datasets(s, headers):
    r = s.get(f"{BASE}/api/v1/dataset/?q=(page_size:200)", headers=headers)
    r.raise_for_status()
    return {(d["database"]["database_name"], d["table_name"])
            for d in r.json()["result"]}


def ensure_datasets(s, headers, db_ids):
    have = existing_datasets(s, headers)
    for db_name, schema, table in DATASETS:
        if (db_name, table) in have:
            print(f"  dataset exists: {db_name}.{table}")
            continue
        payload = {"database": db_ids[db_name], "table_name": table}
        if schema:
            payload["schema"] = schema
        r = s.post(f"{BASE}/api/v1/dataset/", headers=headers, json=payload)
        if r.status_code in (200, 201):
            print(f"  created dataset: {db_name}.{table}")
        else:
            print(f"  WARN dataset {db_name}.{table}: {r.status_code} {r.text[:200]}")


def main() -> int:
    s, headers = session_and_headers()
    print("Registering databases:")
    db_ids = ensure_databases(s, headers)
    print("Registering datasets:")
    ensure_datasets(s, headers, db_ids)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
