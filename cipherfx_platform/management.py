from __future__ import annotations
from mt5_xm_gateway import MT5Gateway
from .database import DatabaseLayer
class TradeManagementEngine:
    """Open-position management only; no market analysis."""
    def __init__(self,gateway:MT5Gateway,database:DatabaseLayer):self.gateway=gateway;self.database=database
    def snapshot(self):
        positions=self.gateway.positions()
        self.database.status("open_positions",[{"ticket":p.ticket,"symbol":p.symbol,"side":p.direction,"volume":p.volume,"profit":p.profit} for p in positions])
        return positions
    def close(self,ticket:int):
        for p in self.gateway.positions():
            if int(p.ticket)==int(ticket):return self.gateway.close_position(p)
        raise LookupError(f"open position not found: {ticket}")
    def reconcile(self):self.database.event("POSITION_RECONCILED",{"count":len(self.gateway.positions())})
