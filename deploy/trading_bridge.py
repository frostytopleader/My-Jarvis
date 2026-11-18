"""
Jarvis Trading Bridge — runs on the Windows trading desk machine.
Exposes the trading desk SQLite databases as a REST API so Jarvis (on Railway)
can call trading_desk_status, portfolio_snapshot, risk_dashboard, strategy_perf.

SETUP:
  1. pip install fastapi uvicorn
  2. Set DB paths below (or set env vars TRADING_DB, PORTFOLIO_DB, RISK_DB)
  3. python trading_bridge.py
  4. Install ngrok:   https://ngrok.com/download
     Expose it:       ngrok http 7001
  5. Add TRADING_DESK_URL=https://xxxx.ngrok.io to Railway Variables
  6. Jarvis can now call all trading desk tools.

The bridge reads your existing SQLite files — it never writes to them.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ── Configure paths to your trading desk SQLite files ─────────────────────────
# Override with environment variables or edit these defaults.
TRADING_DB   = Path(os.environ.get("TRADING_DB",   "trading_desk.db"))
PORTFOLIO_DB = Path(os.environ.get("PORTFOLIO_DB", "portfolio.db"))
RISK_DB      = Path(os.environ.get("RISK_DB",      "risk.db"))
STRATEGY_DB  = Path(os.environ.get("STRATEGY_DB",  "strategies.db"))

# Which DB to use if everything is in a single file
SINGLE_DB    = Path(os.environ.get("SINGLE_DB", ""))

START_TIME   = time.time()

app = FastAPI(title="Jarvis Trading Bridge", version="1.0.0")


def db_path(preferred: Path) -> Path:
    if SINGLE_DB and SINGLE_DB.exists():
        return SINGLE_DB
    return preferred


def run_query(db: Path, sql: str, params=()) -> list[dict]:
    if not db.exists():
        return []
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def scalar(db: Path, sql: str, params=(), default=None):
    if not db.exists():
        return default
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else default


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status")
def trading_status():
    """Overall system status — adapt table/column names to your schema."""
    uptime = (time.time() - START_TIME) / 3600

    # Try to read from common table names; adjust to match your schema
    tables_tried = []
    for db in [TRADING_DB, db_path(TRADING_DB)]:
        try:
            with sqlite3.connect(str(db)) as conn:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                tables_tried.extend(tables)
        except Exception:
            pass

    halt_active = False
    halt_reason = ""
    last_trade  = None
    stage       = "Stage14B"
    paper       = True
    strategies  = 0
    active      = 0

    # Try common column/table patterns
    for tbl in ["system_state", "trading_state", "risk_state", "config"]:
        if tbl in tables_tried:
            try:
                rows = run_query(db_path(TRADING_DB),
                                 f"SELECT * FROM {tbl} ORDER BY rowid DESC LIMIT 1")
                if rows:
                    r = rows[0]
                    halt_active = bool(r.get("halt_active", r.get("is_halted", False)))
                    halt_reason = r.get("halt_reason", r.get("halt_message", ""))
                    stage       = r.get("stage", stage)
                    paper       = bool(r.get("paper_trading", r.get("paper_mode", True)))
                    break
            except Exception:
                pass

    for tbl in ["trades", "trade_log", "executions", "orders"]:
        if tbl in tables_tried:
            try:
                rows = run_query(db_path(TRADING_DB),
                                 f"SELECT * FROM {tbl} ORDER BY rowid DESC LIMIT 1")
                if rows:
                    r = rows[0]
                    last_trade = r.get("timestamp", r.get("created_at", r.get("time", None)))
                    break
            except Exception:
                pass

    for tbl in ["strategies", "candidates", "strategy_pool"]:
        if tbl in tables_tried:
            try:
                strategies = scalar(db_path(STRATEGY_DB),
                                    f"SELECT COUNT(*) FROM {tbl}", default=0)
                active = scalar(db_path(STRATEGY_DB),
                                f"SELECT COUNT(*) FROM {tbl} WHERE active=1", default=0)
                break
            except Exception:
                pass

    return JSONResponse({
        "stage":              stage,
        "paper_trading":      paper,
        "halt_active":        halt_active,
        "halt_reason":        halt_reason,
        "strategies_loaded":  strategies,
        "active_strategies":  active,
        "last_trade":         str(last_trade) if last_trade else None,
        "uptime_hours":       round(uptime, 2),
        "last_updated":       datetime.now(timezone.utc).isoformat(),
        "available_tables":   list(set(tables_tried))[:20],
    })


@app.get("/portfolio")
def portfolio():
    """Current positions and P&L. Adapt table/column names to your schema."""
    tables_tried = []
    try:
        with sqlite3.connect(str(db_path(PORTFOLIO_DB))) as conn:
            tables_tried = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except Exception:
        pass

    positions   = []
    total_pnl   = 0.0
    pnl_pct     = 0.0
    equity      = 0.0
    cash        = 0.0

    for tbl in ["positions", "portfolio", "open_positions", "holdings"]:
        if tbl in tables_tried:
            try:
                rows = run_query(db_path(PORTFOLIO_DB), f"SELECT * FROM {tbl}")
                for r in rows:
                    pos = {
                        "symbol":        r.get("symbol", r.get("ticker", "?")),
                        "size":          float(r.get("size",   r.get("qty",   r.get("quantity", 0)))),
                        "pnl":           float(r.get("pnl",    r.get("unrealized_pnl", 0))),
                        "pnl_pct":       float(r.get("pnl_pct", r.get("return_pct", 0))),
                        "entry_price":   float(r.get("entry_price", r.get("avg_price", 0))),
                        "current_price": float(r.get("current_price", r.get("last_price", 0))),
                    }
                    positions.append(pos)
                    total_pnl += pos["pnl"]
                break
            except Exception:
                pass

    for tbl in ["account", "balance", "account_state"]:
        if tbl in tables_tried:
            try:
                rows = run_query(db_path(PORTFOLIO_DB), f"SELECT * FROM {tbl} LIMIT 1")
                if rows:
                    r      = rows[0]
                    equity = float(r.get("equity", r.get("total_value", r.get("account_value", 135.0))))
                    cash   = float(r.get("cash",   r.get("available_cash", 0)))
                    break
            except Exception:
                pass

    pnl_pct = (total_pnl / equity * 100) if equity else 0.0

    return JSONResponse({
        "account_value":  equity,
        "cash":           cash,
        "total_pnl":      round(total_pnl, 4),
        "total_pnl_pct":  round(pnl_pct, 4),
        "positions":      positions,
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    })


@app.get("/risk")
def risk():
    """Daily drawdown state and halt trigger. Adapt to your schema."""
    tables_tried = []
    try:
        with sqlite3.connect(str(db_path(RISK_DB))) as conn:
            tables_tried = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except Exception:
        pass

    dd_pct    = 0.0
    halt_thr  = 5.0
    halt_act  = False
    peak_eq   = 0.0
    cur_eq    = 0.0
    max_pos   = 0.10
    exposure  = 0.0
    violations = []

    for tbl in ["risk_state", "daily_risk", "drawdown_state", "risk"]:
        if tbl in tables_tried:
            try:
                rows = run_query(db_path(RISK_DB), f"SELECT * FROM {tbl} ORDER BY rowid DESC LIMIT 1")
                if rows:
                    r = rows[0]
                    dd_pct   = float(r.get("daily_drawdown_pct",    r.get("drawdown_pct", 0)))
                    halt_thr = float(r.get("halt_threshold",         r.get("max_drawdown_pct", 5.0)))
                    halt_act = bool(r.get("halt_active",             r.get("is_halted", False)))
                    peak_eq  = float(r.get("peak_equity",            r.get("peak_value", 0)))
                    cur_eq   = float(r.get("current_equity",         r.get("current_value", 0)))
                    max_pos  = float(r.get("max_position_size",      r.get("max_pos_pct", 0.10)))
                    exposure = float(r.get("total_exposure",         r.get("gross_exposure", 0)))
                    break
            except Exception:
                pass

    return JSONResponse({
        "daily_drawdown_pct":           round(dd_pct, 4),
        "daily_drawdown_halt_threshold": halt_thr,
        "halt_active":                  halt_act,
        "peak_equity":                  peak_eq,
        "current_equity":               cur_eq,
        "max_position_size":            max_pos,
        "total_exposure":               exposure,
        "violations":                   violations,
        "last_updated":                 datetime.now(timezone.utc).isoformat(),
    })


@app.get("/strategies")
def strategies(limit: int = 10, min_sharpe: float = 0.0):
    """Stage14B strategy performance. Adapt to your schema."""
    tables_tried = []
    try:
        with sqlite3.connect(str(db_path(STRATEGY_DB))) as conn:
            tables_tried = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    except Exception:
        pass

    strats = []
    total  = 0
    seed   = 20261026
    stage  = "Stage14B"

    for tbl in ["strategies", "candidates", "strategy_pool", "strategy_results"]:
        if tbl in tables_tried:
            try:
                total  = scalar(db_path(STRATEGY_DB), f"SELECT COUNT(*) FROM {tbl}", default=0)
                # Try to read Sharpe and other metrics
                rows = run_query(
                    db_path(STRATEGY_DB),
                    f"""SELECT * FROM {tbl}
                        WHERE (sharpe IS NULL OR sharpe >= ?)
                        ORDER BY sharpe DESC NULLS LAST
                        LIMIT ?""",
                    (min_sharpe, limit),
                )
                for r in rows:
                    strats.append({
                        "id":           str(r.get("id", r.get("strategy_id", "?"))),
                        "sharpe":       float(r.get("sharpe",        r.get("sharpe_ratio", 0))),
                        "win_rate":     float(r.get("win_rate",      r.get("win_pct", 0))),
                        "expectancy":   float(r.get("expectancy",    r.get("expected_value", 0))),
                        "max_drawdown": float(r.get("max_drawdown",  r.get("max_dd", 0))),
                        "trades":       int(r.get("trades",          r.get("trade_count", 0))),
                        "seed":         int(r.get("seed", seed)),
                    })
                break
            except Exception:
                pass

    # Dec 1 readiness gates — check for a gates table or config key
    gates = {}
    for tbl in ["readiness_gates", "gates", "milestones"]:
        if tbl in tables_tried:
            try:
                rows = run_query(db_path(STRATEGY_DB), f"SELECT * FROM {tbl}")
                for r in rows:
                    name   = r.get("gate", r.get("name", r.get("milestone", "?")))
                    status = bool(r.get("passed", r.get("complete", r.get("status", False))))
                    gates[name] = status
                break
            except Exception:
                pass

    return JSONResponse({
        "stage":           stage,
        "seed":            seed,
        "total_strategies": total,
        "strategies":      strats,
        "readiness_gates": gates,
        "last_updated":    datetime.now(timezone.utc).isoformat(),
    })


if __name__ == "__main__":
    print("=" * 60)
    print("Jarvis Trading Bridge — port 7001")
    print("=" * 60)
    for label, path in [
        ("TRADING_DB",   TRADING_DB),
        ("PORTFOLIO_DB", PORTFOLIO_DB),
        ("RISK_DB",      RISK_DB),
        ("STRATEGY_DB",  STRATEGY_DB),
        ("SINGLE_DB",    SINGLE_DB),
    ]:
        exists = "✅" if path.exists() else "⚠️  NOT FOUND"
        if path:
            print(f"  {label:12s}: {path}  {exists}")
    print()
    print("Once running, expose with:  ngrok http 7001")
    print("Then add TRADING_DESK_URL=https://xxxx.ngrok.io to Railway Variables.")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=7001, log_level="info")
