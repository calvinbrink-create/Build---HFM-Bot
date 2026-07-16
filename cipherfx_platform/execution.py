from __future__ import annotations
import os, threading
from dataclasses import dataclass
from datetime import datetime, timezone
from mt5_xm_config import MT5RuntimeConfig
from mt5_xm_gateway import MT5Gateway
from .contracts import ExecutionResult, TradeProposal, SIDES
from .database import DatabaseLayer

@dataclass(frozen=True)
class ExecutionLimits:
    risk_pct: float=0.25
    max_risk_pct: float=1.0
    max_spread_points: float=0.0

class ExecutionEngine:
    """Broker, exposure, risk mechanics and order submission only."""
    def __init__(self,gateway:MT5Gateway,config:MT5RuntimeConfig,database:DatabaseLayer):
        self.gateway=gateway; self.config=config; self.database=database
        self.limits=ExecutionLimits(max(0.01,float(os.getenv("MT5_RISK_PCT","0.25"))),
            max(0.01,float(os.getenv("MT5_MAX_RISK_PCT","1.0"))),
            max(0.0,float(os.getenv("MT5_MAX_SPREAD_POINTS","0"))))
        self._lock=threading.RLock(); self._claimed=set()
    def _volume(self,p:TradeProposal,entry,spec,account):
        budget=max(0.0,float(getattr(account,"equity",0.0) or 0.0))*min(self.limits.risk_pct,self.limits.max_risk_pct)/100.0
        if budget<=0:return 0.0,0.0
        loss=abs(float(self.gateway.order_calc_profit(p.symbol,p.side,1.0,entry,p.stop_loss)))
        if loss<=0:return 0.0,0.0
        volume=self.gateway.normalize_volume(spec,min(float(spec.volume_max),max(float(spec.volume_min),budget/loss)))
        return volume,float(self.gateway.order_calc_margin(p.symbol,p.side,volume,entry))
    def _preflight(self,p):
        if p.side not in SIDES:return False,"INVALID_SIDE",0.0
        if datetime.now(timezone.utc)>=p.expires_at:return False,"PROPOSAL_EXPIRED",0.0
        account=self.gateway.account_info()
        if not getattr(account,"terminal_connected",True):return False,"BROKER_DISCONNECTED",0.0
        if not getattr(account,"trade_allowed",True) or not getattr(account,"account_trade_allowed",True):return False,"TRADING_NOT_ALLOWED",0.0
        if self.database.count_today()>=int(self.config.max_daily_trades):return False,"MAX_DAILY_TRADES",0.0
        positions=self.gateway.positions()
        if len(positions)>=int(self.config.max_open_trades):return False,"MAX_OPEN_TRADES",0.0
        if any(str(x.symbol).upper()==p.symbol.upper() and str(x.direction).upper()==p.side for x in positions):return False,"DUPLICATE_POSITION",0.0
        _,tick=self.gateway.symbol_tick(p.symbol); entry=float(getattr(tick,"ask" if p.side=="BUY" else "bid",0.0) or 0.0)
        if entry<=0:return False,"INVALID_TICK",0.0
        spec=self.gateway.symbol_info(p.symbol); spread=(float(tick.ask)-float(tick.bid))/max(float(spec.point),1e-12)
        if self.limits.max_spread_points and spread>self.limits.max_spread_points:return False,"SPREAD_LIMIT",0.0
        geometry=p.stop_loss<entry<p.take_profit if p.side=="BUY" else p.take_profit<entry<p.stop_loss
        if not geometry:return False,"INVALID_SL_TP_GEOMETRY",0.0
        volume,margin=self._volume(p,entry,spec,account)
        if volume<float(spec.volume_min):return False,"MIN_VOLUME",0.0
        free=float(getattr(account,"free_margin",0.0) or 0.0)
        if free>0 and margin>free:return False,"INSUFFICIENT_MARGIN",0.0
        return True,"PASS",volume
    def submit(self,p:TradeProposal)->ExecutionResult:
        with self._lock:
            if p.proposal_id in self._claimed or self.database.proposal_seen(p.proposal_id):
                return ExecutionResult(p.proposal_id,"DUPLICATE_SUPPRESSED",p.symbol,p.side,0.0,reason="IDEMPOTENCY_KEY_ALREADY_USED")
            self._claimed.add(p.proposal_id)
        try:
            ok,reason,volume=self._preflight(p)
            if not ok:
                r=ExecutionResult(p.proposal_id,"REJECTED",p.symbol,p.side,0.0,reason=reason)
                self.database.execution(p.proposal_id,p.symbol,p.side,r.status,0.0); self.database.event("EXECUTION_REJECTED",{"reason":reason},symbol=p.symbol,proposal_id=p.proposal_id); return r
            if self.config.dry_run or self.config.trade_mode=="paper":
                r=ExecutionResult(p.proposal_id,"DRY_RUN",p.symbol,p.side,volume,reason="PAPER_OR_DRY_RUN")
                self.database.execution(p.proposal_id,p.symbol,p.side,r.status,volume); self.database.event("EXECUTION_DRY_RUN",{"volume":volume},symbol=p.symbol,proposal_id=p.proposal_id); return r
            submitted=datetime.now(timezone.utc)
            resolved,price,broker=self.gateway.place_market_order(p.symbol,p.side,volume,p.stop_loss,p.take_profit,"cipherfx:"+p.proposal_id)
            retcode=int(getattr(broker,"retcode",0) or 0); order=int(getattr(broker,"order",0) or 0); deal=int(getattr(broker,"deal",0) or 0); position=int(getattr(broker,"position",0) or 0)
            status="FILLED" if deal or retcode in {10008,10009} else "REJECTED"
            r=ExecutionResult(p.proposal_id,status,resolved,p.side,volume,order_ticket=order,deal_ticket=deal,position_ticket=position,fill_price=float(price or 0.0),reason=str(getattr(broker,"comment","") or retcode),submitted_at=submitted,filled_at=datetime.now(timezone.utc) if status=="FILLED" else None)
            self.database.execution(p.proposal_id,resolved,p.side,status,volume); self.database.event("ORDER_FILLED" if status=="FILLED" else "ORDER_REJECTED",{"retcode":retcode,"order":order,"deal":deal},symbol=resolved,proposal_id=p.proposal_id); return r
        except Exception as exc:
            reason=f"BROKER_ERROR:{type(exc).__name__}:{str(exc)[:180]}"
            r=ExecutionResult(p.proposal_id,"REJECTED",p.symbol,p.side,0.0,reason=reason)
            self.database.execution(p.proposal_id,p.symbol,p.side,r.status,0.0)
            self.database.event("ORDER_REJECTED",{"reason":reason},symbol=p.symbol,proposal_id=p.proposal_id)
            return r
        finally:
            with self._lock:self._claimed.discard(p.proposal_id)
