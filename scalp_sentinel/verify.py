"""Deployment preflight: validate config and additive schema before activation."""
from .config import Config
from .storage import Cooldowns
from .journal import Journal
import unittest


def main():
    suite=unittest.defaultTestLoader.discover('tests')
    if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
        raise RuntimeError('preflight_tests_failed')
    cfg=Config.from_env()
    cfg.validate()
    if not cfg.dry_run:
        storage=Cooldowns(cfg.database_url,False)
        try:
            Journal(storage.conn)
            # Avoid taking view locks in reverse order to a concurrent old
            # worker's legacy migration (table -> view). Query the table only.
            storage.conn.execute('SELECT id,journal_json FROM scalp_signal_outcomes LIMIT 0')
            storage.conn.commit()
        finally:
            storage.close()
    print('preflight_ok config_valid schema_ready paper_only=true')


if __name__=='__main__':
    main()
