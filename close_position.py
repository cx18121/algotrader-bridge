"""One-shot script: close open MBT long via IB Gateway."""
import asyncio

# eventkit reads the event loop at import time; create one first.
asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, ContFuture, MarketOrder

async def main():
    ib = IB()
    await ib.connectAsync("127.0.0.1", 7497, clientId=99)
    print("Connected")

    contract = ContFuture(symbol="MBT", exchange="CME", currency="USD")
    await ib.qualifyContractsAsync(contract)
    print(f"Contract: {contract.localSymbol}")

    order = MarketOrder("SELL", 1)
    trade = ib.placeOrder(contract, order)
    print(f"Order placed: {trade.order.orderId}")

    # Wait for fill (up to 30s)
    for _ in range(60):
        await asyncio.sleep(0.5)
        if trade.orderStatus.status == "Filled":
            print(f"Filled @ {trade.orderStatus.avgFillPrice}")
            break
        if trade.orderStatus.status in ("Cancelled", "Inactive"):
            print(f"Order failed: {trade.orderStatus.status}")
            break
    else:
        print(f"Timeout — status: {trade.orderStatus.status}")

    ib.disconnect()

asyncio.run(main())
