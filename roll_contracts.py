"""Futures contract roll helper.

Updates the last_trade_date for one or more symbols in the contract_map table
so the bridge picks up the new front-month contract without a server restart.

Usage:
    # Roll MBT to June 2026:
    python roll_contracts.py MBT 202606

    # Roll multiple symbols at once:
    python roll_contracts.py MBT 202606 ES 202606

    # List current mappings:
    python roll_contracts.py --list

Run from the trading/ directory with .venv active.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from server.database import init_db, get_session, session_factory  # noqa: E402
from server.models import ContractMap  # noqa: E402
from sqlalchemy import select  # noqa: E402


async def list_mappings() -> None:
    await init_db()
    async with get_session() as sess:
        rows = (await sess.execute(select(ContractMap).order_by(ContractMap.tv_symbol))).scalars().all()
    if not rows:
        print("No contract mappings in DB. Run the server once to seed from CONTRACT_MAP env.")
        return
    print(f"{'TV_SYMBOL':<12} {'IB_SYMBOL':<12} {'SEC_TYPE':<12} {'EXCHANGE':<10} {'CURRENCY':<10} {'LAST_TRADE_DATE':<16} UPDATED_AT")
    print("-" * 90)
    for r in rows:
        print(f"{r.tv_symbol:<12} {r.ib_symbol:<12} {r.sec_type:<12} {r.exchange:<10} {r.currency:<10} {str(r.last_trade_date or ''):<16} {r.updated_at}")


async def roll(pairs: list[tuple[str, str]]) -> None:
    await init_db()
    async with get_session() as sess:
        for tv_sym, new_date in pairs:
            key = tv_sym.upper()
            row = (await sess.execute(select(ContractMap).where(ContractMap.tv_symbol == key))).scalar_one_or_none()
            if row is None:
                print(f"ERROR: No mapping found for {key}. Add it first via PUT /api/contracts/{key}.")
                continue
            old = row.last_trade_date
            row.last_trade_date = new_date
            print(f"Rolling {key}: {old} -> {new_date}")
        await sess.commit()
    print("Done. Bridge will use the new contract on the next signal (no restart needed).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Roll futures contract dates in the bridge DB.")
    parser.add_argument("--list", action="store_true", help="List current contract mappings")
    parser.add_argument("pairs", nargs="*", metavar="SYMBOL DATE",
                        help="Alternating symbol / YYYYMM pairs, e.g. MBT 202606 ES 202606")
    args = parser.parse_args()

    if args.list:
        asyncio.run(list_mappings())
        return

    if not args.pairs:
        parser.print_help()
        sys.exit(1)

    if len(args.pairs) % 2 != 0:
        print("ERROR: Arguments must be alternating SYMBOL DATE pairs.")
        sys.exit(1)

    pairs = [(args.pairs[i], args.pairs[i + 1]) for i in range(0, len(args.pairs), 2)]
    asyncio.run(roll(pairs))


if __name__ == "__main__":
    main()
