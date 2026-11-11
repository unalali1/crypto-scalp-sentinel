"""Persistent anti-spam state in PostgreSQL, in-memory in dry run."""
import sqlite3
import time


class Cooldowns:
    def __init__(self, database_url, dry_run):
        self.is_pg = database_url.startswith(("postgres://", "postgresql://"))
        if not dry_run and database_url and not self.is_pg:
            raise ValueError('DATABASE_URL must be a PostgreSQL URL')
        if not database_url and not dry_run:
            raise RuntimeError("DATABASE_URL (PostgreSQL) is required for live Telegram mode")
        if self.is_pg:
            import psycopg
            self.conn = psycopg.connect(database_url, connect_timeout=8)
            self.conn.execute("""CREATE TABLE IF NOT EXISTS scalp_signal_cooldowns (
                symbol TEXT NOT NULL, side TEXT NOT NULL, last_sent_epoch BIGINT NOT NULL,
                PRIMARY KEY (symbol, side))""")
            self.conn.commit()
            from .tracker import init_db
            init_db(self.conn)
        else:
            self.conn = sqlite3.connect(":memory:")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS scalp_signal_cooldowns (
                symbol TEXT NOT NULL, side TEXT NOT NULL, last_sent_epoch INTEGER NOT NULL,
                PRIMARY KEY (symbol, side))""")
            self.conn.commit()

    def allowed(self, symbol, side, cooldown_minutes):
        placeholder = "%s" if self.is_pg else "?"
        row = self.conn.execute(
            f"SELECT last_sent_epoch FROM scalp_signal_cooldowns WHERE symbol={placeholder} AND side={placeholder}",
            (symbol, side)).fetchone()
        return row is None or time.time() - int(row[0]) >= cooldown_minutes * 60

    def mark_sent(self, symbol, side, signal=None):
        if signal is not None and self.is_pg:
            from .tracker import record_sent
            record_sent(self.conn, signal, int(time.time() * 1000))
        placeholder = "%s" if self.is_pg else "?"
        self.conn.execute(
            f"INSERT INTO scalp_signal_cooldowns(symbol, side, last_sent_epoch) "
            f"VALUES({placeholder}, {placeholder}, {placeholder}) "
            "ON CONFLICT(symbol, side) DO UPDATE SET last_sent_epoch=excluded.last_sent_epoch",
            (symbol, side, int(time.time())))
        self.conn.commit()

    def close(self):
        self.conn.close()
