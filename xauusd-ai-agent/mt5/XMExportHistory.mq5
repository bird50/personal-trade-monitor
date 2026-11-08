#property version "1.00"
#property strict
#property script_show_inputs
#property description "Read-only GOLD history exporter (M1 + M5 with spread) for local backtests. No orders."

input string ExpectedServer = "XMGlobal-MT5 7";
input string GoldSymbol = "GOLD";
input int MonthsBack = 36;

string I(long value) { return StringFormat("%I64d", value); }

bool Demo()
{
   return AccountInfoInteger(ACCOUNT_TRADE_MODE)==ACCOUNT_TRADE_MODE_DEMO
       && AccountInfoString(ACCOUNT_SERVER)==ExpectedServer;
}

// Copy one window, retrying while the terminal downloads history.
int CopyWindow(string symbol, ENUM_TIMEFRAMES tf, datetime from, datetime to, MqlRates &rates[])
{
   for(int attempt=0; attempt<20 && !IsStopped(); attempt++)
   {
      ResetLastError();
      int n=CopyRates(symbol,tf,from,to,rates);
      if(n>=0) return n;
      Sleep(500);
   }
   return -1;
}

// Writes oldest-first bars in broker server time. Times are not converted to UTC.
bool Export(string symbol, ENUM_TIMEFRAMES tf, string name, datetime start, datetime stop)
{
   string part=name+".part";
   int f=FileOpen(part,FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(f==INVALID_HANDLE) { Print("Cannot open ",part," error=",GetLastError()); return false; }
   long offset=(long)(TimeTradeServer()-TimeGMT());
   FileWriteString(f,"# source=xm_mt5_demo server="+AccountInfoString(ACCOUNT_SERVER)+" symbol="+symbol
      +" timeframe="+EnumToString(tf)+" point="+DoubleToString(SymbolInfoDouble(symbol,SYMBOL_POINT),8)
      +" digits="+IntegerToString((int)SymbolInfoInteger(symbol,SYMBOL_DIGITS))
      +" observed_utc_offset_s="+I(offset)+" exported_utc_s="+I((long)TimeGMT())
      +" time_basis=broker_server_wall_clock price=bid\r\n");
   FileWriteString(f,"time_server_s,open,high,low,close,tick_volume,spread_points\r\n");
   int digits=(int)SymbolInfoInteger(symbol,SYMBOL_DIGITS);
   long last=0, total=0, first=0;
   int failures=0;
   datetime from=start;
   while(from<stop && !IsStopped())
   {
      MqlDateTime d; TimeToStruct(from,d); d.mon+=1; if(d.mon>12) { d.mon=1; d.year+=1; }
      datetime to=StructToTime(d); if(to>stop) to=stop;
      MqlRates rates[];
      int n=CopyWindow(symbol,tf,from,to,rates);
      if(n<0) { failures++; Print("History window unavailable from ",TimeToString(from)," error=",GetLastError()); }
      for(int i=0;i<n;i++)
      {
         long t=(long)rates[i].time;
         // Skip the still-open bar and any overlap between windows.
         if(t<=last || rates[i].time+PeriodSeconds(tf)>TimeTradeServer()) continue;
         FileWriteString(f,I(t)+","+DoubleToString(rates[i].open,digits)+","+DoubleToString(rates[i].high,digits)+","
            +DoubleToString(rates[i].low,digits)+","+DoubleToString(rates[i].close,digits)+","
            +I(rates[i].tick_volume)+","+IntegerToString(rates[i].spread)+"\r\n");
         if(first==0) first=t;
         last=t; total++;
      }
      from=to;
   }
   FileClose(f);
   if(IsStopped() || total==0) { FileDelete(part); Print(name,": no bars written"); return false; }
   if(!FileMove(part,0,name,FILE_REWRITE)) { Print("Cannot finalize ",name," error=",GetLastError()); return false; }
   Print(name,": bars=",total," first=",TimeToString((datetime)first)," last=",TimeToString((datetime)last),
         " failed_windows=",failures," maxbars=",TerminalInfoInteger(TERMINAL_MAXBARS));
   return true;
}

void OnStart()
{
   if(MQLInfoInteger(MQL_TESTER) || !Demo()) { Print("History export requires the configured XM demo server"); return; }
   if(MonthsBack<1 || MonthsBack>36) { Print("MonthsBack must be 1-36"); return; }
   string symbol=GoldSymbol==""?"GOLD":GoldSymbol;
   if(!SymbolSelect(symbol,true)) { Print("Symbol not available: ",symbol); return; }
   datetime stop=TimeTradeServer();
   MqlDateTime d; TimeToStruct(stop,d); d.mon-=MonthsBack; while(d.mon<1) { d.mon+=12; d.year-=1; }
   datetime start=StructToTime(d);
   Print("History export started: ",symbol," from ",TimeToString(start)," (server time); read-only, no orders.");
   bool m1=Export(symbol,PERIOD_M1,"xm_gold_m1.csv",start,stop);
   bool m5=Export(symbol,PERIOD_M5,"xm_gold_m5.csv",start,stop);
   Print("History export finished: M1=",(m1?"ok":"failed")," M5=",(m5?"ok":"failed"));
}
