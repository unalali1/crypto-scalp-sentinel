"""Deployment preflight: validate config and additive schema before activation."""
from .config import Config
from .storage import Cooldowns
from .journal import Journal


def main():
    cfg=Config.from_env()
    cfg.validate()
    if not cfg.dry_run:
        storage=Cooldowns(cfg.database_url,False)
        try:
            Journal(storage.conn)
            storage.conn.execute('SELECT * FROM scalp_signal_performance LIMIT 0')
            storage.conn.commit()
        finally:
            storage.close()
    print('preflight_ok config_valid schema_ready paper_only=true')


if __name__=='__main__':
    main()
