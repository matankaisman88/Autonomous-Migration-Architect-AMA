"""
Kfar Supply synthetic dataset — DEV/TEST FIXTURE ONLY, not used in the live production flow.

This module generates a fake company database (DDL/DML + synthetic SQL logs) so developers
can spin up a local SQL Server and exercise AMA end-to-end without a real company database.
It is intentionally decoupled from the live API: ``/api/live/start`` only performs read-only
``real_extract`` against a real database and never imports anything from this package.

Reachable only from developer tooling and tests:
  - ``tools/generate_kfar_supply.py``  (regenerate the on-disk fixture)
  - ``tools/setup_dev_mssql.py``       (deploy the fixture into a local SQL Server)
  - the test suite

Do not wire this into any production API route.
"""

from __future__ import annotations
