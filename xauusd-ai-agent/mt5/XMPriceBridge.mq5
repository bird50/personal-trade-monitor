#property copyright "Local XAUUSD paper research"
#property version "1.01"
#property strict
#property description "Read-only demo price exporter. Contains no order execution functions."

// Endpoint is fixed to loopback. Credentials never go to a public endpoint.
input string ExpectedServer = "XMGlobal-MT5 7";
input string GoldSymbol = "GOLD"; // Empty also defaults to GOLD, independent of chart.
input int HistoryBars = 300;
input int PollSeconds = 15;

string token = "";
string symbol_name = "";
bool busy = false;

string Esc(string text)
{
   StringReplace(text, "\\", "\\\\");
   StringReplace(text, "\"", "\\\"");
   return text;
}

string IntegerText(long value)
{
   return StringFormat("%I64d", value);
}

bool DemoAccount()
{
   return AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO
          && AccountInfoString(ACCOUNT_SERVER) == ExpectedServer;
}

string SerializeBars(MqlRates &bars[], int count, int digits)
{
   string value = "[";
   for(int i = 0; i < count; i++)
   {
      if(i > 0) value += ",";
      value += "{\"open_server_s\":" + IntegerText((long)bars[i].time)
         + ",\"open\":" + DoubleToString(bars[i].open, digits)
         + ",\"high\":" + DoubleToString(bars[i].high, digits)
         + ",\"low\":" + DoubleToString(bars[i].low, digits)
         + ",\"close\":" + DoubleToString(bars[i].close, digits)
         + ",\"tick_volume\":" + IntegerText(bars[i].tick_volume)
         + ",\"spread_points\":" + IntegerToString(bars[i].spread) + "}";
   }
   return value + "]";
}

int OnInit()
{
   if(MQLInfoInteger(MQL_TESTER))
   {
      Print("Bridge requires a connected demo terminal; Strategy Tester is unsupported.");
      return INIT_FAILED;
   }
   if(!DemoAccount())
   {
      Print("Bridge requires a demo account on ExpectedServer.");
      return INIT_FAILED;
   }
   if(HistoryBars < 200 || HistoryBars > 1000 || PollSeconds < 15)
      return INIT_PARAMETERS_INCORRECT;
   int file = FileOpen("bridge-token.txt", FILE_READ | FILE_TXT | FILE_ANSI);
   if(file == INVALID_HANDLE)
   {
      Print("Place bridge-token.txt in this terminal's MQL5/Files folder.");
      return INIT_FAILED;
   }
   token = FileReadString(file);
   FileClose(file);
   StringTrimLeft(token);
   StringTrimRight(token);
   if(StringLen(token) < 32 || StringLen(token) > 128)
      return INIT_PARAMETERS_INCORRECT;
   for(int i = 0; i < StringLen(token); i++)
   {
      ushort c = StringGetCharacter(token, i);
      if(!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
           || (c >= '0' && c <= '9') || c == '_' || c == '-'))
         return INIT_PARAMETERS_INCORRECT;
   }
   symbol_name = GoldSymbol == "" ? "GOLD" : GoldSymbol;
   if(!SymbolSelect(symbol_name, true)) return INIT_FAILED;
   if(!EventSetTimer(PollSeconds)) return INIT_FAILED;
   Print("Read-only demo price bridge started. HTTP target: 127.0.0.1:8001.");
   Print("Price source symbol: ", symbol_name, "; chart symbol: ", _Symbol);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   token = "";
}

void OnTimer()
{
   if(busy || !TerminalInfoInteger(TERMINAL_CONNECTED) || !DemoAccount()) return;
   MqlRates m5[], h1[];
   ArraySetAsSeries(m5, false);
   ArraySetAsSeries(h1, false);
   // shift=1 excludes the candle currently forming. Oldest data is serialized first.
   int n5 = CopyRates(symbol_name, PERIOD_M5, 1, HistoryBars, m5);
   int n1 = CopyRates(symbol_name, PERIOD_H1, 1, HistoryBars, h1);
   if(n5 != HistoryBars || n1 != HistoryBars)
   {
      Print("Waiting for complete M5/H1 history.");
      return;
   }
   MqlTick quote;
   if(!SymbolInfoTick(symbol_name, quote) || quote.bid <= 0 || quote.ask < quote.bid) return;
   datetime utc = TimeGMT();
   datetime server_now = TimeTradeServer();
   int offset = (int)(MathRound((double)(server_now - utc) / 900.0) * 900);
   // Do not send quotes when the market feed is stale, e.g. a disconnected/weekend feed.
   if(MathAbs((double)(server_now - utc - offset)) > 60
      || server_now - quote.time > 120 || quote.time - server_now > 30) return;
   int digits = (int)SymbolInfoInteger(symbol_name, SYMBOL_DIGITS);
   string body = "{\"schema_version\":1,\"source\":\"mt5\",\"account_mode\":\"demo\""
      + ",\"server\":\"" + Esc(AccountInfoString(ACCOUNT_SERVER)) + "\""
      + ",\"broker_symbol\":\"" + Esc(symbol_name) + "\""
      + ",\"captured_at_utc_s\":" + IntegerText((long)utc)
      + ",\"server_time_s\":" + IntegerText((long)server_now)
      + ",\"observed_utc_offset_s\":" + IntegerToString(offset)
      + ",\"digits\":" + IntegerToString(digits)
      + ",\"point\":" + DoubleToString(SymbolInfoDouble(symbol_name, SYMBOL_POINT), 8)
      + ",\"tick_size\":" + DoubleToString(SymbolInfoDouble(symbol_name, SYMBOL_TRADE_TICK_SIZE), 8)
      + ",\"quote\":{\"time_server_ms\":" + IntegerText(quote.time_msc)
      + ",\"bid\":" + DoubleToString(quote.bid, digits)
      + ",\"ask\":" + DoubleToString(quote.ask, digits) + "}"
      + ",\"m5\":" + SerializeBars(m5, n5, digits)
      + ",\"h1\":" + SerializeBars(h1, n1, digits) + "}";
   char data[], result[];
   int bytes = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(bytes <= 1) return;
   ArrayResize(data, bytes - 1); // HTTP JSON must not contain the terminating NUL.
   string headers = "Content-Type: application/json\r\nAuthorization: Bearer " + token + "\r\n";
   string result_headers;
   busy = true;
   ResetLastError();
   int status = WebRequest("POST", "http://127.0.0.1:8001/mt5/snapshot", headers,
                           3000, data, result, result_headers);
   busy = false;
   // Never print credentials, headers, response body, account ID or balance.
   if(status == 200 || status == 201)
      Print("Price snapshot accepted: M5=", n5, ", H1=", n1);
   else if(status == -1)
      Print("Bridge connection failed. Error=", GetLastError(),
            "; check receiver and WebRequest allowlist http://127.0.0.1:8001");
   else
      Print("Bridge rejected snapshot. HTTP=", status);
}
