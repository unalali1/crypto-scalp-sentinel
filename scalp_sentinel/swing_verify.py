"""Pre-deploy checks for the isolated paper swing service."""
import os
import unittest


def main():
    suite=unittest.defaultTestLoader.discover("tests")
    if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
        raise RuntimeError("swing_tests_failed")
    url=os.environ.get("DATABASE_URL","")
    if not url.startswith(("postgres://","postgresql://")):
        raise RuntimeError("swing_paper_requires_postgresql")
    import psycopg
    from .journal import Journal
    with psycopg.connect(url,connect_timeout=8) as conn:
        Journal(conn)
        conn.execute("SELECT symbol FROM scalp_swing_assessments LIMIT 0")
        conn.commit()
    print("swing_preflight_ok schema_ready paper_only=true")


if __name__=="__main__":
    main()
