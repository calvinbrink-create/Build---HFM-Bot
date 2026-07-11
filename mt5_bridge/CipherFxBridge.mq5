#property strict

input string SymbolsCSV = "EURUSD,GBPUSD,USDJPY,USDCHF,USDCAD,AUDUSD,NZDUSD,USDZAR,XAUUSD,XAGUSD,BTCUSD,ETHUSD,NAS100,US30,GER40,UK100";
input string SymbolsFile = "cipherfx\\symbols.txt";
input int ExportBars = 220;
input int TimerSeconds = 1;
input int TimerMilliseconds = 250;
input int SlippagePoints = 20;
input int MaxRateSymbolsPerCycle = 25;
input bool ExportExtraTimeframes = true;
input double MinLotSize = 0.00;
input double TargetProfitPerTradeUSD = 0.00;
input double DailyProfitTargetUSD = 1000.00;
input double DailyLossLimitUSD = 1000.00;
input int MaxPyramidTrades = 10;
input int MaxPyramidTradesPerSignal = 10;
input bool AllowSameCandlePyramids = true;
input int MaxOpenTradesTotal = 30;
input int MaxTradesPerDay = 60;
input bool StopTradingAfterDailyTarget = false;
input bool StopTradingAfterDailyLossLimit = true;
input bool UseNetProfitTarget = false;
input int ExportLogSeconds = 60;

string BRIDGE_DIR = "cipherfx";
string COMMANDS_DIR = "cipherfx\\commands";
string RESULTS_DIR = "cipherfx\\results";
int RateCursor = 0;
string LastRateExportKeys[];
datetime LastRateExportBars[];
ulong LastStaticExportMs = 0;
ulong LastAccountExportMs = 0;
ulong LastPositionExportMs = 0;
ulong LastDealExportMs = 0;
string ActiveRiskSymbol = "";
ulong ActiveRiskMagic = 0;
string ActiveRiskComment = "";
string LastTradeBlockReason = "";
int LastPyramidCount = 0;
bool LastTradingBlocked = false;
datetime LastBridgeExportLogAt = 0;

bool ShouldLogBridgeExportCycle()
{
   if(ExportLogSeconds <= 0)
      return false;
   datetime now = TimeLocal();
   if(LastBridgeExportLogAt == 0 || (now - LastBridgeExportLogAt) >= ExportLogSeconds)
   {
      LastBridgeExportLogAt = now;
      return true;
   }
   return false;
}

int OnInit()
{
   FolderCreate(BRIDGE_DIR);
   FolderCreate(COMMANDS_DIR);
   FolderCreate(RESULTS_DIR);
   int timer_ms = MathMax(0, TimerMilliseconds);
   if(timer_ms > 0)
      EventSetMillisecondTimer(MathMax(100, timer_ms));
   else
      EventSetTimer(MathMax(1, TimerSeconds));
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTick()
{
   // The 250 ms timer owns bridge cadence; ticks must not duplicate full exports.
}

void OnTimer()
{
   RunBridge();
}

void RunBridge()
{
   ProcessCommands();
   ulong nowMs = GetTickCount64();
   if(LastAccountExportMs == 0 || (nowMs - LastAccountExportMs) >= 1000)
   {
      ExportAccount();
      LastAccountExportMs = nowMs;
   }
   if(LastPositionExportMs == 0 || (nowMs - LastPositionExportMs) >= 500)
   {
      ExportPositions();
      ExportOrders();
      LastPositionExportMs = nowMs;
   }
   if(LastDealExportMs == 0 || (nowMs - LastDealExportMs) >= 3000)
   {
      ExportDeals();
      LastDealExportMs = nowMs;
   }
   ExportSymbols();
   UpdateRiskComment(LastTradingBlocked, LastTradeBlockReason, LastPyramidCount);
   ProcessCommands();
}

string Trim(string value)
{
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
}

string Upper(string value)
{
   StringToUpper(value);
   return value;
}

bool AtomicReplaceFile(string temp_path, string final_path)
{
   ResetLastError();
   if(!FileMove(temp_path, 0, final_path, FILE_REWRITE))
   {
      int err = GetLastError();
      PrintFormat("CipherFX bridge atomic replace failed temp=%s final=%s err=%d", temp_path, final_path, err);
      FileDelete(temp_path);
      return false;
   }
   return true;
}

datetime BrokerDayStart()
{
   MqlDateTime stamp;
   TimeToStruct(TimeCurrent(), stamp);
   stamp.hour = 0;
   stamp.min = 0;
   stamp.sec = 0;
   return StructToTime(stamp);
}

int BrokerUtcOffsetSeconds()
{
   int raw_offset = (int)(TimeCurrent() - TimeGMT());
   // TimeCurrent and TimeGMT can straddle a second; normalize to whole minutes.
   return (int)MathRound((double)raw_offset / 60.0) * 60;
}

int EffectiveMaxPyramidTrades()
{
   int a = MathMax(1, MaxPyramidTrades);
   int b = MathMax(1, MaxPyramidTradesPerSignal);
   return MathMin(a, b);
}

bool MagicMatches(long deal_magic)
{
   return ActiveRiskMagic == 0 || (ulong)deal_magic == ActiveRiskMagic;
}

bool IsDealTradeType(ulong deal)
{
   ENUM_DEAL_TYPE type = (ENUM_DEAL_TYPE)HistoryDealGetInteger(deal, DEAL_TYPE);
   return type == DEAL_TYPE_BUY || type == DEAL_TYPE_SELL;
}

bool IsClosingDeal(ulong deal)
{
   ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal, DEAL_ENTRY);
   return entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_INOUT || entry == DEAL_ENTRY_OUT_BY;
}

bool IsOpeningDeal(ulong deal)
{
   ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal, DEAL_ENTRY);
   return entry == DEAL_ENTRY_IN || entry == DEAL_ENTRY_INOUT;
}

double GetTodayClosedProfit()
{
   double total = 0.0;
   if(!HistorySelect(BrokerDayStart(), TimeCurrent()))
      return 0.0;
   int total_deals = HistoryDealsTotal();
   for(int i = 0; i < total_deals; i++)
   {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0 || !IsDealTradeType(deal) || !IsClosingDeal(deal)) continue;
      if(!MagicMatches(HistoryDealGetInteger(deal, DEAL_MAGIC))) continue;
      total += HistoryDealGetDouble(deal, DEAL_PROFIT);
      total += HistoryDealGetDouble(deal, DEAL_SWAP);
      total += HistoryDealGetDouble(deal, DEAL_COMMISSION);
   }
   return total;
}

int GetTodayClosedTradeCount()
{
   int count = 0;
   if(!HistorySelect(BrokerDayStart(), TimeCurrent()))
      return 0;
   int total_deals = HistoryDealsTotal();
   for(int i = 0; i < total_deals; i++)
   {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0 || !IsDealTradeType(deal) || !IsOpeningDeal(deal)) continue;
      if(!MagicMatches(HistoryDealGetInteger(deal, DEAL_MAGIC))) continue;
      count++;
   }
   return count;
}

bool SameSetupPosition(string symbol, ulong magic, string comment)
{
   if(PositionGetString(POSITION_SYMBOL) != symbol)
      return false;
   if(magic > 0 && (ulong)PositionGetInteger(POSITION_MAGIC) != magic)
      return false;
   if(comment != "" && PositionGetString(POSITION_COMMENT) != comment)
      return false;
   return true;
}

int GetActivePyramidCount(string direction, string symbol = "", ulong magic = 0, string comment = "")
{
   string dir = Upper(direction);
   string setup_symbol = symbol == "" ? ActiveRiskSymbol : symbol;
   ulong setup_magic = magic == 0 ? ActiveRiskMagic : magic;
   string setup_comment = comment == "" ? ActiveRiskComment : comment;
   int count = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(!SameSetupPosition(setup_symbol, setup_magic, setup_comment)) continue;
      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      if((dir == "BUY" && type == POSITION_TYPE_BUY) || (dir == "SELL" && type == POSITION_TYPE_SELL))
         count++;
   }
   return count;
}

bool HasOppositeSetupPosition(string symbol, string direction, ulong magic, string comment)
{
   string dir = Upper(direction);
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(!SameSetupPosition(symbol, magic, comment)) continue;
      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      if((dir == "BUY" && type == POSITION_TYPE_SELL) || (dir == "SELL" && type == POSITION_TYPE_BUY))
         return true;
   }
   return false;
}

bool HasSameCandleEntry(string symbol, string direction, ulong magic, string comment)
{
   datetime candle_start = iTime(symbol, PERIOD_M15, 0);
   if(candle_start <= 0)
      return false;
   string dir = Upper(direction);
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(!SameSetupPosition(symbol, magic, comment)) continue;
      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      bool same_dir = (dir == "BUY" && type == POSITION_TYPE_BUY) || (dir == "SELL" && type == POSITION_TYPE_SELL);
      if(same_dir && (datetime)PositionGetInteger(POSITION_TIME) >= candle_start)
         return true;
   }
   return false;
}

void UpdateRiskComment(bool blocked = false, string reason = "", int pyramid_count = -1)
{
   int daily_count = GetTodayClosedTradeCount();
   double daily_profit = GetTodayClosedProfit();
   int pyramid = pyramid_count >= 0 ? pyramid_count : LastPyramidCount;
   string block_text = blocked ? "YES" : "NO";
   string display_reason = reason == "" ? LastTradeBlockReason : reason;
   Comment(
      "Cipher FX MT5 Risk\n",
      "Daily closed profit: $", DoubleToString(daily_profit, 2), "\n",
      "Daily trade count: ", IntegerToString(daily_count), "/", IntegerToString(MaxTradesPerDay), "\n",
      "Current pyramid count: ", IntegerToString(pyramid), "/", IntegerToString(EffectiveMaxPyramidTrades()), "\n",
      "Trading blocked: ", block_text, "\n",
      "Reason: ", display_reason
   );
}

void LogTradeBlockReason(string reason)
{
   LastTradeBlockReason = reason;
   LastTradingBlocked = true;
   Print("CipherFX trade blocked: ", reason);
   UpdateRiskComment(true, reason, LastPyramidCount);
}

int VolumeDigits(double step)
{
   if(step <= 0.0) return 2;
   int digits = 0;
   double value = step;
   while(digits < 8 && MathAbs(value - MathRound(value)) > 0.00000001)
   {
      value *= 10.0;
      digits++;
   }
   return digits;
}

double NormalizeLotSize(double lot, string symbol = "")
{
   string use_symbol = symbol == "" ? _Symbol : symbol;
   if(lot <= 0.0) return 0.0;
   double broker_min = SymbolInfoDouble(use_symbol, SYMBOL_VOLUME_MIN);
   double broker_max = SymbolInfoDouble(use_symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(use_symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0) step = 0.01;
   if(broker_min <= 0.0) broker_min = step;
   if(broker_max <= 0.0) broker_max = MathMax(lot, broker_min);
   double min_required = broker_min;
   if(MinLotSize > 0.0)
      min_required = MathMax(broker_min, MinLotSize);
   if(broker_max < min_required)
      return 0.0;
   if(lot + step * 0.000001 < min_required)
      return 0.0;
   double requested = lot;
   requested = MathMin(requested, broker_max);
   double steps = MathFloor((requested + step * 0.000001) / step);
   double normalized = steps * step;
   if(normalized < min_required)
      return 0.0;
   normalized = MathMin(normalized, broker_max);
   return NormalizeDouble(normalized, VolumeDigits(step));
}

bool CheckFreeMargin(string symbol, ENUM_ORDER_TYPE order_type, double volume, double price, string &reason)
{
   double required_margin = 0.0;
   if(!OrderCalcMargin(order_type, symbol, volume, price, required_margin))
   {
      reason = "margin check failed for " + symbol + ": " + IntegerToString(GetLastError());
      return false;
   }
   double free_margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   if(required_margin > free_margin)
   {
      reason = "free margin too low: required=" + DoubleToString(required_margin, 2) +
               " free=" + DoubleToString(free_margin, 2);
      return false;
   }
   return true;
}

bool CanOpenNewTrade(string symbol, string direction, double volume, double price, ulong magic, string comment, string &reason)
{
   ActiveRiskSymbol = symbol;
   ActiveRiskMagic = magic;
   ActiveRiskComment = comment;
   LastTradingBlocked = false;
   LastTradeBlockReason = "";

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      reason = "terminal algo trading disabled";
      LogTradeBlockReason(reason);
      return false;
   }
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   {
      reason = "account trading disabled";
      LogTradeBlockReason(reason);
      return false;
   }
   if(PositionsTotal() >= MathMax(1, MaxOpenTradesTotal))
   {
      reason = "MaxOpenTradesTotal reached (" + IntegerToString(PositionsTotal()) + "/" + IntegerToString(MaxOpenTradesTotal) + ")";
      LogTradeBlockReason(reason);
      return false;
   }

   double daily_profit = GetTodayClosedProfit();
   int daily_count = GetTodayClosedTradeCount();
   int daily_limit = MathMax(1, MaxTradesPerDay);
   LastPyramidCount = GetActivePyramidCount(direction, symbol, magic, comment);

   Print("CipherFX risk check: daily_count=", daily_count, "/", daily_limit,
         " daily_closed_profit=", DoubleToString(daily_profit, 2),
         " pyramid_count=", LastPyramidCount, "/", EffectiveMaxPyramidTrades(),
         " symbol=", symbol, " direction=", direction);

   if(daily_count >= daily_limit)
   {
      reason = "MaxTradesPerDay reached (" + IntegerToString(daily_count) + "/" + IntegerToString(daily_limit) + ")";
      LogTradeBlockReason(reason);
      return false;
   }
   if(StopTradingAfterDailyTarget && DailyProfitTargetUSD > 0.0 && daily_profit >= DailyProfitTargetUSD)
   {
      reason = "DailyProfitTargetUSD reached (" + DoubleToString(daily_profit, 2) + "/" + DoubleToString(DailyProfitTargetUSD, 2) + ")";
      LogTradeBlockReason(reason);
      return false;
   }
   if(StopTradingAfterDailyLossLimit && DailyLossLimitUSD > 0.0 && daily_profit <= -MathAbs(DailyLossLimitUSD))
   {
      reason = "DailyLossLimitUSD reached (" + DoubleToString(daily_profit, 2) + "/-" + DoubleToString(MathAbs(DailyLossLimitUSD), 2) + ")";
      LogTradeBlockReason(reason);
      return false;
   }
   if(HasOppositeSetupPosition(symbol, direction, magic, comment))
   {
      reason = "opposite active setup exists; pyramids must match original direction";
      LogTradeBlockReason(reason);
      return false;
   }
   if(LastPyramidCount >= EffectiveMaxPyramidTrades())
   {
      reason = "MaxPyramidTradesPerSignal reached (" + IntegerToString(LastPyramidCount) + "/" + IntegerToString(EffectiveMaxPyramidTrades()) + ")";
      LogTradeBlockReason(reason);
      return false;
   }
   if(!AllowSameCandlePyramids && LastPyramidCount > 0 && HasSameCandleEntry(symbol, direction, magic, comment))
   {
      reason = "duplicate pyramid blocked on current M15 candle";
      LogTradeBlockReason(reason);
      return false;
   }

   ENUM_ORDER_TYPE order_type = Upper(direction) == "BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!CheckFreeMargin(symbol, order_type, volume, price, reason))
   {
      LogTradeBlockReason(reason);
      return false;
   }

   LastTradingBlocked = false;
   UpdateRiskComment(false, "", LastPyramidCount);
   return true;
}

double ProfitAtPrice(string symbol, string direction, double volume, double open_price, double close_price)
{
   double profit = 0.0;
   ENUM_ORDER_TYPE order_type = Upper(direction) == "BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!OrderCalcProfit(order_type, symbol, volume, open_price, close_price, profit))
      return -1.0e100;
   return profit;
}

bool ApplyNetProfitTarget(string symbol, string direction, double volume, double open_price, double &tp, string &message)
{
   if(!UseNetProfitTarget || TargetProfitPerTradeUSD <= 0.0)
      return true;

   if(tp > 0.0 && ProfitAtPrice(symbol, direction, volume, open_price, tp) >= TargetProfitPerTradeUSD)
      return true;

   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(point <= 0.0)
   {
      message = "profit target blocked: invalid point size for " + symbol;
      return false;
   }

   string dir = Upper(direction);
   double low = 0.0;
   double high = point * 10.0;
   double max_distance = MathMax(open_price * 5.0, point * 1000000.0);
   bool found = false;

   for(int i = 0; i < 80; i++)
   {
      double candidate = dir == "BUY" ? open_price + high : open_price - high;
      if(candidate <= point)
         break;
      double profit = ProfitAtPrice(symbol, direction, volume, open_price, candidate);
      if(profit >= TargetProfitPerTradeUSD)
      {
         found = true;
         break;
      }
      high *= 2.0;
      if(high > max_distance)
         break;
   }

   if(!found)
   {
      message = "profit target blocked: cannot calculate $" + DoubleToString(TargetProfitPerTradeUSD, 2) + " TP for " + symbol;
      return false;
   }

   for(int i = 0; i < 50; i++)
   {
      double mid = (low + high) / 2.0;
      double candidate = dir == "BUY" ? open_price + mid : open_price - mid;
      double profit = ProfitAtPrice(symbol, direction, volume, open_price, candidate);
      if(profit >= TargetProfitPerTradeUSD)
         high = mid;
      else
         low = mid;
   }

   double target_price = dir == "BUY" ? open_price + high : open_price - high;
   tp = NormalizePriceForSymbol(symbol, target_price);
   Print("CipherFX TP adjusted for net target: ", symbol,
         " direction=", direction,
         " volume=", DoubleToString(volume, 4),
         " target_usd=", DoubleToString(TargetProfitPerTradeUSD, 2),
         " tp=", DoubleToString(tp, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)));
   return true;
}

void ExportAccount()
{
   string final_path = BRIDGE_DIR + "\\account.txt";
   string temp_path = final_path + ".tmp";
   int handle = FileOpen(temp_path, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE) return;
   FileWrite(handle, "login=" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN)));
   FileWrite(handle, "server=" + AccountInfoString(ACCOUNT_SERVER));
   FileWrite(handle, "name=" + AccountInfoString(ACCOUNT_NAME));
   FileWrite(handle, "balance=" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   FileWrite(handle, "equity=" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2));
   FileWrite(handle, "profit=" + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 2));
   FileWrite(handle, "leverage=" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LEVERAGE)));
   FileWrite(handle, "margin=" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2));
   FileWrite(handle, "free_margin=" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2));
   FileWrite(handle, "currency=" + AccountInfoString(ACCOUNT_CURRENCY));
   FileWrite(handle, "terminal_connected=" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "1" : "0"));
   FileWrite(handle, "trade_allowed=" + (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "1" : "0"));
   FileWrite(handle, "account_trade_allowed=" + (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "1" : "0"));
   FileWrite(handle, "broker_time_epoch=" + IntegerToString((int)TimeCurrent()));
   FileWrite(handle, "utc_time_epoch=" + IntegerToString((int)TimeGMT()));
   FileWrite(handle, "broker_utc_offset_seconds=" + IntegerToString(BrokerUtcOffsetSeconds()));
   FileWrite(handle, "export_receipt_local_epoch=" + IntegerToString((int)TimeLocal()));
   FileClose(handle);
   AtomicReplaceFile(temp_path, final_path);
}

void ExportPositions()
{
   string final_path = BRIDGE_DIR + "\\positions.csv";
   string temp_path = final_path + ".tmp";
   int handle = FileOpen(temp_path, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(handle == INVALID_HANDLE) return;
   FileWrite(handle, "ticket", "symbol", "direction", "volume", "price_open", "price_current", "sl", "tp", "profit", "time_broker", "time_utc", "broker_utc_offset_seconds");
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      string direction = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL";
      FileWrite(
         handle,
         (string)ticket,
         PositionGetString(POSITION_SYMBOL),
         direction,
         DoubleToString(PositionGetDouble(POSITION_VOLUME), 4),
         DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), 8),
         DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), 8),
         DoubleToString(PositionGetDouble(POSITION_SL), 8),
         DoubleToString(PositionGetDouble(POSITION_TP), 8),
         DoubleToString(PositionGetDouble(POSITION_PROFIT), 2),
         IntegerToString((int)PositionGetInteger(POSITION_TIME)),
         IntegerToString((int)PositionGetInteger(POSITION_TIME) - BrokerUtcOffsetSeconds()),
         IntegerToString(BrokerUtcOffsetSeconds())
      );
   }
   FileClose(handle);
   AtomicReplaceFile(temp_path, final_path);
}

void ExportOrders()
{
   string final_path = BRIDGE_DIR + "\\orders.csv";
   string temp_path = final_path + ".tmp";
   int handle = FileOpen(temp_path, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(handle == INVALID_HANDLE) return;
   FileWrite(handle, "ticket", "symbol", "order_type", "side", "volume", "price_open", "sl", "tp", "state", "time_setup_broker", "time_setup_utc", "broker_utc_offset_seconds");
   for(int i = 0; i < OrdersTotal(); i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || !OrderSelect(ticket)) continue;
      ENUM_ORDER_TYPE order_type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      string order_type_text = EnumToString(order_type);
      string side = (order_type == ORDER_TYPE_SELL || order_type == ORDER_TYPE_SELL_LIMIT || order_type == ORDER_TYPE_SELL_STOP || order_type == ORDER_TYPE_SELL_STOP_LIMIT) ? "SELL" : "BUY";
      FileWrite(
         handle,
         (string)ticket,
         OrderGetString(ORDER_SYMBOL),
         order_type_text,
         side,
         DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 4),
         DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), 8),
         DoubleToString(OrderGetDouble(ORDER_SL), 8),
         DoubleToString(OrderGetDouble(ORDER_TP), 8),
         EnumToString((ENUM_ORDER_STATE)OrderGetInteger(ORDER_STATE)),
         IntegerToString((int)OrderGetInteger(ORDER_TIME_SETUP)),
         IntegerToString((int)OrderGetInteger(ORDER_TIME_SETUP) - BrokerUtcOffsetSeconds()),
         IntegerToString(BrokerUtcOffsetSeconds())
      );
   }
   FileClose(handle);
   AtomicReplaceFile(temp_path, final_path);
}

void ExportDeals()
{
   datetime from = TimeCurrent() - (7 * 24 * 60 * 60);
   datetime to = TimeCurrent();
   if(!HistorySelect(from, to)) return;
   string final_path = BRIDGE_DIR + "\\deals.csv";
   string temp_path = final_path + ".tmp";
   int handle = FileOpen(temp_path, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(handle == INVALID_HANDLE) return;
   FileWrite(handle, "deal", "position_id", "symbol", "price", "profit", "swap", "commission", "net_profit", "time_broker", "time_utc", "broker_utc_offset_seconds");
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0) continue;
      double profit = HistoryDealGetDouble(deal, DEAL_PROFIT);
      double swap = HistoryDealGetDouble(deal, DEAL_SWAP);
      double commission = HistoryDealGetDouble(deal, DEAL_COMMISSION);
      double net_profit = profit + swap + commission;
      FileWrite(
         handle,
         (string)deal,
         (string)HistoryDealGetInteger(deal, DEAL_POSITION_ID),
         HistoryDealGetString(deal, DEAL_SYMBOL),
         DoubleToString(HistoryDealGetDouble(deal, DEAL_PRICE), 8),
         DoubleToString(profit, 2),
         DoubleToString(swap, 2),
         DoubleToString(commission, 2),
         DoubleToString(net_profit, 2),
         IntegerToString((int)HistoryDealGetInteger(deal, DEAL_TIME)),
         IntegerToString((int)HistoryDealGetInteger(deal, DEAL_TIME) - BrokerUtcOffsetSeconds()),
         IntegerToString(BrokerUtcOffsetSeconds())
      );
   }
   FileClose(handle);
   AtomicReplaceFile(temp_path, final_path);
}

bool AddUniqueSymbol(string &symbols[], string symbol)
{
   symbol = Trim(symbol);
   if(symbol == "") return false;
   int total = ArraySize(symbols);
   string wanted = Upper(symbol);
   for(int i = 0; i < total; i++)
   {
      if(Upper(symbols[i]) == wanted)
         return false;
   }
   ArrayResize(symbols, total + 1);
   symbols[total] = symbol;
   return true;
}

int LoadSymbolsFile(string &symbols[])
{
   int before = ArraySize(symbols);
   int handle = FileOpen(SymbolsFile, FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE) return 0;
   while(!FileIsEnding(handle))
   {
      string symbol = Trim(FileReadString(handle));
      if(symbol == "") continue;
      AddUniqueSymbol(symbols, symbol);
   }
   FileClose(handle);
   return ArraySize(symbols) - before;
}

void LoadCsvSymbols(string &symbols[])
{
   string parts[];
   int count = StringSplit(SymbolsCSV, ',', parts);
   for(int i = 0; i < count; i++)
      AddUniqueSymbol(symbols, parts[i]);
}

void LoadConfiguredSymbols(string &symbols[])
{
   ArrayResize(symbols, 0);
   int loaded = LoadSymbolsFile(symbols);
   if(loaded <= 0)
      LoadCsvSymbols(symbols);
}

void SelectConfiguredSymbols(string &symbols[])
{
   LoadConfiguredSymbols(symbols);
   int total = ArraySize(symbols);
   for(int i = 0; i < total; i++)
      SymbolSelect(symbols[i], true);
}

string RateExportKey(string symbol, string label)
{
   return Upper(symbol) + "|" + Upper(label);
}

int RateExportIndex(string symbol, string label)
{
   int total = ArraySize(LastRateExportKeys);
   string wanted = RateExportKey(symbol, label);
   for(int i = 0; i < total; i++)
   {
      if(LastRateExportKeys[i] == wanted)
         return i;
   }
   return -1;
}

bool ShouldExportRates(string symbol, ENUM_TIMEFRAMES timeframe, string label)
{
   datetime current_bar = iTime(symbol, timeframe, 0);
   string file_name = BRIDGE_DIR + "\\rates_" + symbol + "_" + label + ".csv";
   int idx = RateExportIndex(symbol, label);
   if(idx < 0 || !FileIsExist(file_name))
      return true;
   return current_bar > 0 && LastRateExportBars[idx] != current_bar;
}

void RememberRateExport(string symbol, ENUM_TIMEFRAMES timeframe, string label)
{
   datetime current_bar = iTime(symbol, timeframe, 0);
   int idx = RateExportIndex(symbol, label);
   if(idx < 0)
   {
      int total = ArraySize(LastRateExportKeys);
      ArrayResize(LastRateExportKeys, total + 1);
      ArrayResize(LastRateExportBars, total + 1);
      LastRateExportKeys[total] = RateExportKey(symbol, label);
      LastRateExportBars[total] = current_bar;
      return;
   }
   LastRateExportBars[idx] = current_bar;
}

bool ExportRatesIfChanged(string symbol, ENUM_TIMEFRAMES timeframe, string label)
{
   if(!ShouldExportRates(symbol, timeframe, label))
      return true;
   return ExportRates(symbol, timeframe, label);
}

void ExportSymbols()
{
   string configuredSymbols[];
   SelectConfiguredSymbols(configuredSymbols);

   string symbols_final_path = BRIDGE_DIR + "\\symbols.csv";
   string symbols_temp_path = symbols_final_path + ".tmp";
   ulong nowMs = GetTickCount64();
   bool exportStatic = LastStaticExportMs == 0 || (nowMs - LastStaticExportMs) >= 60000;
   int allHandle = exportStatic ? FileOpen(symbols_temp_path, FILE_WRITE | FILE_CSV | FILE_ANSI, ',') : INVALID_HANDLE;
   if(allHandle != INVALID_HANDLE)
      FileWrite(
         allHandle,
         "symbol", "visible", "description", "path", "digits", "point",
         "volume_min", "volume_max", "volume_step", "contract_size",
         "tick_value", "tick_value_profit", "tick_value_loss", "tick_size",
         "stops_level", "trade_mode", "filling_mode", "currency_profit"
      );
   int totalAll = exportStatic ? SymbolsTotal(false) : 0;
   for(int idx = 0; idx < totalAll; idx++)
   {
      string allSymbol = SymbolName(idx, false);
      if(allSymbol == "") continue;
      bool visible = SymbolInfoInteger(allSymbol, SYMBOL_VISIBLE);
      if(allHandle != INVALID_HANDLE)
      {
         FileWrite(
            allHandle,
            allSymbol,
            visible ? "1" : "0",
            SymbolInfoString(allSymbol, SYMBOL_DESCRIPTION),
            SymbolInfoString(allSymbol, SYMBOL_PATH),
            IntegerToString((int)SymbolInfoInteger(allSymbol, SYMBOL_DIGITS)),
            DoubleToString(SymbolInfoDouble(allSymbol, SYMBOL_POINT), 10),
            DoubleToString(SymbolInfoDouble(allSymbol, SYMBOL_VOLUME_MIN), 8),
            DoubleToString(SymbolInfoDouble(allSymbol, SYMBOL_VOLUME_MAX), 8),
            DoubleToString(SymbolInfoDouble(allSymbol, SYMBOL_VOLUME_STEP), 8),
            DoubleToString(SymbolInfoDouble(allSymbol, SYMBOL_TRADE_CONTRACT_SIZE), 8),
            DoubleToString(SymbolInfoDouble(allSymbol, SYMBOL_TRADE_TICK_VALUE), 8),
            DoubleToString(SymbolInfoDouble(allSymbol, SYMBOL_TRADE_TICK_VALUE_PROFIT), 8),
            DoubleToString(SymbolInfoDouble(allSymbol, SYMBOL_TRADE_TICK_VALUE_LOSS), 8),
            DoubleToString(SymbolInfoDouble(allSymbol, SYMBOL_TRADE_TICK_SIZE), 10),
            IntegerToString((int)SymbolInfoInteger(allSymbol, SYMBOL_TRADE_STOPS_LEVEL)),
            IntegerToString((int)SymbolInfoInteger(allSymbol, SYMBOL_TRADE_MODE)),
            IntegerToString((int)SymbolInfoInteger(allSymbol, SYMBOL_FILLING_MODE)),
            SymbolInfoString(allSymbol, SYMBOL_CURRENCY_PROFIT)
         );
      }
   }
   if(allHandle != INVALID_HANDLE)
   {
      FileClose(allHandle);
      AtomicReplaceFile(symbols_temp_path, symbols_final_path);
      LastStaticExportMs = nowMs;
   }

   int totalConfigured = ArraySize(configuredSymbols);
   int exported = 0;
   int maxPerCycle = MathMax(1, MaxRateSymbolsPerCycle);
   int start = RateCursor;
   bool logCycle = ShouldLogBridgeExportCycle();
   for(int n = 0; n < totalConfigured && exported < maxPerCycle; n++)
   {
      int j = (start + n) % totalConfigured;
      string visibleSymbol = configuredSymbols[j];
      if(visibleSymbol == "") continue;
      bool isVisible = (bool)SymbolInfoInteger(visibleSymbol, SYMBOL_VISIBLE);
      if(!isVisible)
      {
         if(logCycle)
            PrintFormat("CipherFX bridge export symbol=%s visible=0 tick=SKIP M1=SKIP M5=SKIP M15=SKIP broker_time=%s local_time=%s", visibleSymbol, TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS), TimeToString(TimeLocal(), TIME_DATE|TIME_SECONDS));
         continue;
      }
      bool tickOk = ExportTick(visibleSymbol);
      bool m15Ok = ExportRatesIfChanged(visibleSymbol, PERIOD_M15, "M15");
      bool m1Ok = ExportRatesIfChanged(visibleSymbol, PERIOD_M1, "M1");
      bool m5Ok = ExportRatesIfChanged(visibleSymbol, PERIOD_M5, "M5");
      if(logCycle)
         PrintFormat("CipherFX bridge export symbol=%s visible=1 tick=%s M1=%s M5=%s M15=%s broker_time=%s local_time=%s", visibleSymbol, tickOk ? "OK" : "FAIL", m1Ok ? "OK" : "FAIL", m5Ok ? "OK" : "FAIL", m15Ok ? "OK" : "FAIL", TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS), TimeToString(TimeLocal(), TIME_DATE|TIME_SECONDS));
      if(ExportExtraTimeframes)
      {
         ExportRatesIfChanged(visibleSymbol, PERIOD_M30, "M30");
         ExportRatesIfChanged(visibleSymbol, PERIOD_H1, "H1");
         ExportRatesIfChanged(visibleSymbol, PERIOD_H4, "H4");
         ExportRatesIfChanged(visibleSymbol, PERIOD_D1, "D1");
         ExportRatesIfChanged(visibleSymbol, PERIOD_W1, "W1");
         ExportRatesIfChanged(visibleSymbol, PERIOD_MN1, "MN");
      }
      exported++;
   }
   if(totalConfigured > 0)
      RateCursor = (start + MathMax(exported, 1)) % totalConfigured;
}

bool ExportTick(string symbol)
{
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick)) return false;
   string final_path = BRIDGE_DIR + "\\tick_" + symbol + ".txt";
   string temp_path = final_path + ".tmp";
   int handle = FileOpen(temp_path, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE) return false;
   FileWrite(handle, "time_broker=" + IntegerToString((int)tick.time));
   FileWrite(handle, "time_utc=" + IntegerToString((int)tick.time - BrokerUtcOffsetSeconds()));
   FileWrite(handle, "broker_utc_offset_seconds=" + IntegerToString(BrokerUtcOffsetSeconds()));
   FileWrite(handle, "export_receipt_local_epoch=" + IntegerToString((int)TimeLocal()));
   FileWrite(handle, "bid=" + DoubleToString(tick.bid, 8));
   FileWrite(handle, "ask=" + DoubleToString(tick.ask, 8));
   FileWrite(handle, "last=" + DoubleToString(tick.last, 8));
   FileClose(handle);
   return AtomicReplaceFile(temp_path, final_path);
}

bool ExportRates(string symbol, ENUM_TIMEFRAMES timeframe, string label)
{
   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int copied = CopyRates(symbol, timeframe, 0, ExportBars, rates);
   if(copied <= 0) return false;
   string final_path = BRIDGE_DIR + "\\rates_" + symbol + "_" + label + ".csv";
   string temp_path = final_path + ".tmp";
   int handle = FileOpen(temp_path, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(handle == INVALID_HANDLE) return false;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   FileWrite(handle, "time", "open", "high", "low", "close", "volume", "spread_points", "spread_price");
   for(int i = 0; i < copied; i++)
   {
      FileWrite(
         handle,
         IntegerToString((int)rates[i].time),
         DoubleToString(rates[i].open, 8),
         DoubleToString(rates[i].high, 8),
         DoubleToString(rates[i].low, 8),
         DoubleToString(rates[i].close, 8),
         IntegerToString((int)rates[i].tick_volume),
         IntegerToString((int)rates[i].spread),
         DoubleToString((double)rates[i].spread * point, 10)
      );
   }
   FileClose(handle);
   if(!AtomicReplaceFile(temp_path, final_path))
      return false;
   RememberRateExport(symbol, timeframe, label);
   return true;
}

void ProcessCommands()
{
   string file_name;
   long search = FileFindFirst(COMMANDS_DIR + "\\command_*.txt", file_name);
   if(search == INVALID_HANDLE) return;
   do
   {
      ProcessSingleCommand(file_name);
   }
   while(FileFindNext(search, file_name));
   FileFindClose(search);
}

void ProcessSingleCommand(string file_name)
{
   string request_id = "";
   string action = "";
   string symbol = "";
   string direction = "";
   string ticket = "";
   string volume = "";
   string price = "";
   string open_price = "";
   string close_price = "";
   string sl = "";
   string tp = "";
   string comment = "";
   string magic = "";
   string pending_type = "";
   string enabled = "";

   int handle = FileOpen(COMMANDS_DIR + "\\" + file_name, FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE) return;
   while(!FileIsEnding(handle))
   {
      string line = FileReadString(handle);
      int pos = StringFind(line, "=");
      if(pos < 0) continue;
      string key = StringSubstr(line, 0, pos);
      string value = StringSubstr(line, pos + 1);
      if(key == "request_id") request_id = value;
      else if(key == "action") action = value;
      else if(key == "symbol") symbol = value;
      else if(key == "direction") direction = value;
      else if(key == "ticket") ticket = value;
      else if(key == "volume") volume = value;
      else if(key == "price") price = value;
      else if(key == "open_price") open_price = value;
      else if(key == "close_price") close_price = value;
      else if(key == "sl") sl = value;
      else if(key == "tp") tp = value;
      else if(key == "comment") comment = value;
      else if(key == "magic") magic = value;
      else if(key == "pending_type") pending_type = value;
      else if(key == "enabled") enabled = value;
   }
   FileClose(handle);

   bool ok = false;
   string message = "";
   ulong order_ticket = 0;
   ulong deal_ticket = 0;
   double fill_price = 0.0;
   double calc_value = 0.0;
   string calc_name = "";

   if(action == "OPEN")
      ok = ExecuteOpen(symbol, direction, volume, sl, tp, comment, magic, order_ticket, deal_ticket, fill_price, message);
   else if(action == "CLOSE")
      ok = ExecuteClose(ticket, symbol, volume, magic, order_ticket, deal_ticket, fill_price, message);
   else if(action == "PENDING")
      ok = ExecutePending(symbol, direction, pending_type, volume, price, sl, tp, comment, magic, order_ticket, deal_ticket, fill_price, message);
   else if(action == "MODIFY_POSITION")
      ok = ExecuteModifyPosition(ticket, symbol, sl, tp, magic, order_ticket, deal_ticket, fill_price, message);
   else if(action == "CANCEL_ORDER")
      ok = ExecuteCancelOrder(ticket, magic, order_ticket, deal_ticket, fill_price, message);
   else if(action == "SYMBOL_ENABLE")
      ok = ExecuteSymbolToggle(symbol, enabled, message);
   else if(action == "ORDER_CALC_PROFIT")
   {
      calc_name = "profit";
      ok = ExecuteOrderCalcProfit(symbol, direction, volume, open_price, close_price, calc_value, message);
   }
   else if(action == "ORDER_CALC_MARGIN")
   {
      calc_name = "margin";
      ok = ExecuteOrderCalcMargin(symbol, direction, volume, open_price, calc_value, message);
   }
   else
      message = "Unsupported action: " + action;

   WriteResult(request_id, ok, message, order_ticket, deal_ticket, fill_price, calc_name, calc_value);
   FileDelete(COMMANDS_DIR + "\\" + file_name);
}

bool IsSuccessRetcode(uint retcode)
{
   return retcode == TRADE_RETCODE_DONE ||
          retcode == TRADE_RETCODE_PLACED ||
          retcode == TRADE_RETCODE_DONE_PARTIAL;
}

double NormalizeVolumeForSymbol(string symbol, double requested)
{
   return NormalizeLotSize(requested, symbol);
}

double NormalizePriceForSymbol(string symbol, double price)
{
   if(price <= 0.0) return 0.0;
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   return NormalizeDouble(price, digits);
}

void EnforceStopDistance(string symbol, string direction, double entry_price, double &sl, double &tp)
{
   int stops_level = (int)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double min_dist = MathMax(stops_level * point, point);
   if(direction == "BUY")
   {
      if(sl > 0.0 && entry_price - sl < min_dist) sl = entry_price - min_dist;
      if(tp > 0.0 && tp - entry_price < min_dist) tp = entry_price + min_dist;
   }
   else
   {
      if(sl > 0.0 && sl - entry_price < min_dist) sl = entry_price + min_dist;
      if(tp > 0.0 && entry_price - tp < min_dist) tp = entry_price - min_dist;
   }
   sl = NormalizePriceForSymbol(symbol, sl);
   tp = NormalizePriceForSymbol(symbol, tp);
}

bool SendWithFillingFallback(MqlTradeRequest &request, MqlTradeResult &result, string &message)
{
   ENUM_ORDER_TYPE_FILLING fillings[3] = {ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN};
   for(int i = 0; i < 3; i++)
   {
      request.type_filling = fillings[i];
      ZeroMemory(result);
      ResetLastError();
      bool sent = OrderSend(request, result);
      if(sent && IsSuccessRetcode(result.retcode))
         return true;
      if(result.retcode == TRADE_RETCODE_INVALID_FILL)
         continue;
      int err = GetLastError();
      message = sent
         ? "retcode=" + IntegerToString((int)result.retcode) + " " + result.comment
         : "OrderSend failed: " + IntegerToString(err);
      return false;
   }
   message = "OrderSend failed: unsupported filling mode";
   return false;
}

bool ExecuteOpen(string symbol, string direction, string volume, string sl, string tp, string comment, string magic, ulong &order_ticket, ulong &deal_ticket, double &fill_price, string &message)
{
   MqlTradeRequest request;
   MqlTradeResult result;
   ZeroMemory(request);
   ZeroMemory(result);
   string dir = Upper(direction);

   if(!SymbolSelect(symbol, true))
   {
      message = "SymbolSelect failed for " + symbol;
      return false;
   }

   if(dir != "BUY" && dir != "SELL")
   {
      message = "Unsupported direction: " + direction;
      LogTradeBlockReason(message);
      return false;
   }

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
   {
      message = "No tick for symbol " + symbol;
      return false;
   }

   double order_volume = NormalizeVolumeForSymbol(symbol, StringToDouble(volume));
   if(order_volume <= 0.0)
   {
      message = "Invalid volume for " + symbol + ": " + volume;
      return false;
   }

   request.action = TRADE_ACTION_DEAL;
   request.symbol = symbol;
   request.volume = order_volume;
   request.sl = NormalizePriceForSymbol(symbol, StringToDouble(sl));
   request.tp = NormalizePriceForSymbol(symbol, StringToDouble(tp));
   request.magic = (ulong)StringToInteger(magic);
   request.comment = comment;
   request.deviation = SlippagePoints;
   request.type_time = ORDER_TIME_GTC;
   if(dir == "BUY")
   {
      request.type = ORDER_TYPE_BUY;
      request.price = tick.ask;
   }
   else
   {
      request.type = ORDER_TYPE_SELL;
      request.price = tick.bid;
   }
   EnforceStopDistance(symbol, dir, request.price, request.sl, request.tp);
   if(!ApplyNetProfitTarget(symbol, dir, request.volume, request.price, request.tp, message))
   {
      LogTradeBlockReason(message);
      return false;
   }
   EnforceStopDistance(symbol, dir, request.price, request.sl, request.tp);

   if(!CanOpenNewTrade(symbol, dir, request.volume, request.price, request.magic, request.comment, message))
      return false;

   if(!SendWithFillingFallback(request, result, message))
      return false;

   order_ticket = result.order;
   deal_ticket = result.deal;
   fill_price = result.price;
   return true;
}

bool ExecuteClose(string ticket, string symbol, string volume, string magic, ulong &order_ticket, ulong &deal_ticket, double &fill_price, string &message)
{
   ulong pos_ticket = (ulong)StringToInteger(ticket);
   if(pos_ticket == 0 || !PositionSelectByTicket(pos_ticket))
   {
      message = "Position not found: " + ticket;
      return false;
   }

   if(!SymbolSelect(symbol, true))
   {
      message = "SymbolSelect failed for " + symbol;
      return false;
   }

   MqlTradeRequest request;
   MqlTradeResult result;
   ZeroMemory(request);
   ZeroMemory(result);

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
   {
      message = "No tick for symbol " + symbol;
      return false;
   }

   ENUM_POSITION_TYPE pos_type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   double order_volume = NormalizeVolumeForSymbol(symbol, StringToDouble(volume));
   if(order_volume <= 0.0)
   {
      message = "Invalid close volume for " + symbol + ": " + volume;
      return false;
   }
   request.action = TRADE_ACTION_DEAL;
   request.position = pos_ticket;
   request.symbol = symbol;
   request.volume = order_volume;
   request.magic = (ulong)StringToInteger(magic);
   request.comment = "cipherfx-close";
   request.deviation = SlippagePoints;
   request.type_time = ORDER_TIME_GTC;
   if(pos_type == POSITION_TYPE_BUY)
   {
      request.type = ORDER_TYPE_SELL;
      request.price = tick.bid;
   }
   else
   {
      request.type = ORDER_TYPE_BUY;
      request.price = tick.ask;
   }

   if(!SendWithFillingFallback(request, result, message))
      return false;

   order_ticket = result.order;
   deal_ticket = result.deal;
   fill_price = result.price;
   return true;
}

bool ExecutePending(string symbol, string direction, string pending_type, string volume, string price, string sl, string tp, string comment, string magic, ulong &order_ticket, ulong &deal_ticket, double &fill_price, string &message)
{
   MqlTradeRequest request;
   MqlTradeResult result;
   ZeroMemory(request);
   ZeroMemory(result);
   string dir = Upper(direction);
   string pending = Upper(pending_type);

   if(!SymbolSelect(symbol, true))
   {
      message = "SymbolSelect failed for " + symbol;
      return false;
   }

   double order_volume = NormalizeVolumeForSymbol(symbol, StringToDouble(volume));
   if(order_volume <= 0.0)
   {
      message = "Invalid pending volume for " + symbol + ": " + volume;
      return false;
   }

   request.action = TRADE_ACTION_PENDING;
   request.symbol = symbol;
   request.volume = order_volume;
   request.price = NormalizePriceForSymbol(symbol, StringToDouble(price));
   request.sl = NormalizePriceForSymbol(symbol, StringToDouble(sl));
   request.tp = NormalizePriceForSymbol(symbol, StringToDouble(tp));
   request.magic = (ulong)StringToInteger(magic);
   request.comment = comment;
   request.deviation = SlippagePoints;
   request.type_filling = ORDER_FILLING_RETURN;
   request.type_time = ORDER_TIME_GTC;

   if(dir == "BUY" && pending == "LIMIT") request.type = ORDER_TYPE_BUY_LIMIT;
   else if(dir == "SELL" && pending == "LIMIT") request.type = ORDER_TYPE_SELL_LIMIT;
   else if(dir == "BUY" && pending == "STOP") request.type = ORDER_TYPE_BUY_STOP;
   else if(dir == "SELL" && pending == "STOP") request.type = ORDER_TYPE_SELL_STOP;
   else
   {
      message = "Unsupported pending type";
      LogTradeBlockReason(message);
      return false;
   }
   EnforceStopDistance(symbol, dir, request.price, request.sl, request.tp);
   if(!ApplyNetProfitTarget(symbol, dir, request.volume, request.price, request.tp, message))
   {
      LogTradeBlockReason(message);
      return false;
   }
   EnforceStopDistance(symbol, dir, request.price, request.sl, request.tp);

   if(!CanOpenNewTrade(symbol, dir, request.volume, request.price, request.magic, request.comment, message))
      return false;

   if(!OrderSend(request, result))
   {
      message = "Pending OrderSend failed: " + IntegerToString((int)GetLastError());
      return false;
   }

   order_ticket = result.order;
   deal_ticket = result.deal;
   fill_price = result.price;
   if(result.retcode != TRADE_RETCODE_DONE && result.retcode != TRADE_RETCODE_PLACED && result.retcode != TRADE_RETCODE_DONE_PARTIAL)
   {
      message = "retcode=" + IntegerToString((int)result.retcode);
      return false;
   }
   return true;
}

bool ExecuteModifyPosition(string ticket, string symbol, string sl, string tp, string magic, ulong &order_ticket, ulong &deal_ticket, double &fill_price, string &message)
{
   ulong pos_ticket = (ulong)StringToInteger(ticket);
   if(pos_ticket == 0 || !PositionSelectByTicket(pos_ticket))
   {
      message = "Position not found: " + ticket;
      return false;
   }

   MqlTradeRequest request;
   MqlTradeResult result;
   ZeroMemory(request);
   ZeroMemory(result);

   request.action = TRADE_ACTION_SLTP;
   request.position = pos_ticket;
   request.symbol = symbol;
   request.sl = NormalizePriceForSymbol(symbol, StringToDouble(sl));
   request.tp = NormalizePriceForSymbol(symbol, StringToDouble(tp));
   request.magic = (ulong)StringToInteger(magic);

   if(!OrderSend(request, result))
   {
      message = "Modify position failed: " + IntegerToString((int)GetLastError());
      return false;
   }

   order_ticket = result.order;
   deal_ticket = result.deal;
   fill_price = result.price;
   if(result.retcode != TRADE_RETCODE_DONE && result.retcode != TRADE_RETCODE_PLACED && result.retcode != TRADE_RETCODE_DONE_PARTIAL)
   {
      message = "retcode=" + IntegerToString((int)result.retcode);
      return false;
   }
   return true;
}

bool ExecuteCancelOrder(string ticket, string magic, ulong &order_ticket, ulong &deal_ticket, double &fill_price, string &message)
{
   ulong order = (ulong)StringToInteger(ticket);
   if(order == 0)
   {
      message = "Order not found: " + ticket;
      return false;
   }

   MqlTradeRequest request;
   MqlTradeResult result;
   ZeroMemory(request);
   ZeroMemory(result);

   request.action = TRADE_ACTION_REMOVE;
   request.order = order;
   request.magic = (ulong)StringToInteger(magic);

   if(!OrderSend(request, result))
   {
      message = "Cancel order failed: " + IntegerToString((int)GetLastError());
      return false;
   }

   order_ticket = result.order;
   deal_ticket = result.deal;
   fill_price = result.price;
   if(result.retcode != TRADE_RETCODE_DONE && result.retcode != TRADE_RETCODE_PLACED && result.retcode != TRADE_RETCODE_DONE_PARTIAL)
   {
      message = "retcode=" + IntegerToString((int)result.retcode);
      return false;
   }
   return true;
}

bool ExecuteOrderCalcProfit(string symbol, string direction, string volume, string open_price, string close_price, double &value, string &message)
{
   if(!SymbolSelect(symbol, true))
   {
      message = "SymbolSelect failed for " + symbol;
      return false;
   }
   string dir = Upper(direction);
   if(dir != "BUY" && dir != "SELL")
   {
      message = "Unsupported direction: " + direction;
      return false;
   }
   double lot = StringToDouble(volume);
   double entry = StringToDouble(open_price);
   double exit_price = StringToDouble(close_price);
   if(lot <= 0.0 || entry <= 0.0 || exit_price <= 0.0)
   {
      message = "invalid ORDER_CALC_PROFIT geometry";
      return false;
   }
   ENUM_ORDER_TYPE order_type = dir == "BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   ResetLastError();
   if(!OrderCalcProfit(order_type, symbol, lot, entry, exit_price, value))
   {
      message = "OrderCalcProfit failed: " + IntegerToString(GetLastError());
      return false;
   }
   message = "broker OrderCalcProfit";
   return true;
}

bool ExecuteOrderCalcMargin(string symbol, string direction, string volume, string open_price, double &value, string &message)
{
   if(!SymbolSelect(symbol, true))
   {
      message = "SymbolSelect failed for " + symbol;
      return false;
   }
   string dir = Upper(direction);
   if(dir != "BUY" && dir != "SELL")
   {
      message = "Unsupported direction: " + direction;
      return false;
   }
   double lot = StringToDouble(volume);
   double entry = StringToDouble(open_price);
   if(lot <= 0.0 || entry <= 0.0)
   {
      message = "invalid ORDER_CALC_MARGIN geometry";
      return false;
   }
   ENUM_ORDER_TYPE order_type = dir == "BUY" ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   ResetLastError();
   if(!OrderCalcMargin(order_type, symbol, lot, entry, value))
   {
      message = "OrderCalcMargin failed: " + IntegerToString(GetLastError());
      return false;
   }
   message = "broker OrderCalcMargin";
   return true;
}

bool ExecuteSymbolToggle(string symbol, string enabled, string &message)
{
   bool visible = (enabled == "1");
   if(!SymbolSelect(symbol, visible))
   {
      message = "SymbolSelect failed for " + symbol;
      return false;
   }
   message = visible ? "enabled" : "disabled";
   return true;
}

void WriteResult(string request_id, bool ok, string message, ulong order_ticket, ulong deal_ticket, double fill_price, string calc_name, double calc_value)
{
   if(request_id == "") return;
   string final_path = RESULTS_DIR + "\\result_" + request_id + ".txt";
   string temp_path = final_path + ".tmp";
   int handle = FileOpen(temp_path, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE) return;
   FileWrite(handle, "status=" + (ok ? "OK" : "ERROR"));
   FileWrite(handle, "message=" + message);
   FileWrite(handle, "retcode=" + IntegerToString(ok ? (int)TRADE_RETCODE_DONE : 0));
   FileWrite(handle, "order=" + (string)order_ticket);
   FileWrite(handle, "deal=" + (string)deal_ticket);
   FileWrite(handle, "price=" + DoubleToString(fill_price, 8));
   FileWrite(handle, "value_name=" + calc_name);
   FileWrite(handle, "value=" + DoubleToString(calc_value, 8));
   FileWrite(handle, "currency=" + AccountInfoString(ACCOUNT_CURRENCY));
   FileClose(handle);
   AtomicReplaceFile(temp_path, final_path);
}
