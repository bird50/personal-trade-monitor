#property version "1.02"
#property strict
#property description "Read-only contiguous GOLD tick exporter for local paper accounting."

input string ExpectedServer = "XMGlobal-MT5 7";
input string GoldSymbol = "GOLD";
input int PollSeconds = 15;
string token = "", symbol_name = "";
long cursor = -1;

string I(long value) { return StringFormat("%I64d", value); }
string Esc(string value)
{
   StringReplace(value,"\\","\\\\"); StringReplace(value,"\"","\\\""); return value;
}
bool Demo()
{
   return AccountInfoInteger(ACCOUNT_TRADE_MODE)==ACCOUNT_TRADE_MODE_DEMO
       && AccountInfoString(ACCOUNT_SERVER)==ExpectedServer;
}
int OnInit()
{
   if(MQLInfoInteger(MQL_TESTER) || !Demo() || PollSeconds<15) return INIT_PARAMETERS_INCORRECT;
   int f=FileOpen("bridge-token.txt",FILE_READ|FILE_TXT|FILE_ANSI);
   if(f==INVALID_HANDLE) { Print("Missing MQL5/Files/bridge-token.txt"); return INIT_FAILED; }
   token=FileReadString(f); FileClose(f); StringTrimLeft(token); StringTrimRight(token);
   if(StringLen(token)<32 || StringLen(token)>128) return INIT_PARAMETERS_INCORRECT;
   for(int i=0;i<StringLen(token);i++)
   {
      ushort c=StringGetCharacter(token,i);
      if(!((c>='A' && c<='Z') || (c>='a' && c<='z') || (c>='0' && c<='9') || c=='_' || c=='-'))
         return INIT_PARAMETERS_INCORRECT;
   }
   symbol_name=GoldSymbol==""?"GOLD":GoldSymbol;
   if(!SymbolSelect(symbol_name,true) || !EventSetTimer(PollSeconds)) return INIT_FAILED;
   Print("Paper tick exporter v1.02 started. Source: ",symbol_name,"; bounded catch-up; no order execution.");
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason) { EventKillTimer(); token=""; }

bool ReadCursor()
{
   string encoded=symbol_name; StringReplace(encoded,"#","%23");
   char empty[],result[]; string response_headers;
   int status=WebRequest("GET","http://127.0.0.1:8001/mt5/ticks/cursor?symbol="+encoded,
         "Authorization: Bearer "+token+"\r\n",3000,empty,result,response_headers);
   if(status!=200) { Print("Tick cursor unavailable. HTTP=",status); return false; }
   string value=CharArrayToString(result,0,-1,CP_UTF8); StringTrimLeft(value); StringTrimRight(value);
   for(int i=0;i<StringLen(value);i++)
      if(StringGetCharacter(value,i)<'0' || StringGetCharacter(value,i)>'9') return false;
   if(StringLen(value)==0) return false;
   cursor=StringToInteger(value);
   return true;
}

// Send one acknowledged page without skipping historical coverage.
// true means another page remains; errors and a live cursor stop the burst.
bool ExportPage()
{
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) || !Demo()) return false;
   if(cursor<0 && !ReadCursor()) return false;
   MqlTick quote;
   if(!SymbolInfoTick(symbol_name,quote) || quote.bid<=0 || quote.ask<quote.bid) return false;
   datetime utc=TimeGMT(), server_now=TimeTradeServer();
   int offset=(int)(MathRound((double)(server_now-utc)/900.0)*900);
   if(MathAbs((double)(server_now-utc-offset))>60) return false;
   if(cursor==0) cursor=quote.time_msc-60000;
   if(quote.time_msc<=cursor) return false;
   MqlTick ticks[];
   ResetLastError();
   int n=CopyTicks(symbol_name,ticks,COPY_TICKS_INFO,(ulong)(cursor+1),2048);
   int error=GetLastError();
   if(n<=0 || error!=0) { Print("Waiting for complete tick history. Error=",error); return false; }
   long through=quote.time_msc;
   // Never split a millisecond: re-fetch the entire last millisecond on next page.
   if(n==2048 && ticks[n-1].time_msc<=through) through=ticks[n-1].time_msc-1;
   int used=0;
   while(used<n && ticks[used].time_msc<=through) used++;
   if(used==0 || through<=cursor) return false;
   int digits=(int)SymbolInfoInteger(symbol_name,SYMBOL_DIGITS);
   string items="["; long previous=-1; int ordinal=-1;
   for(int i=0;i<used;i++)
   {
      if(ticks[i].bid<=0 || ticks[i].ask<ticks[i].bid)
      { Print("Tick batch has a non-tradable quote; coverage not advanced."); return false; }
      ordinal=ticks[i].time_msc==previous?ordinal+1:0; previous=ticks[i].time_msc;
      if(i>0) items+=",";
      items+="{\"time_server_ms\":"+I(ticks[i].time_msc)+",\"ordinal\":"+IntegerToString(ordinal)
          +",\"bid\":"+DoubleToString(ticks[i].bid,digits)+",\"ask\":"+DoubleToString(ticks[i].ask,digits)+"}";
   }
   items+="]";
   string body="{\"schema_version\":1,\"source\":\"mt5\",\"account_mode\":\"demo\""
      +",\"server\":\""+Esc(ExpectedServer)+"\",\"broker_symbol\":\""+Esc(symbol_name)+"\""
      +",\"captured_at_utc_s\":"+I((long)utc)+",\"server_time_s\":"+I((long)server_now)
      +",\"observed_utc_offset_s\":"+IntegerToString(offset)+",\"from_server_ms\":"+I(cursor+1)
      +",\"through_server_ms\":"+I(through)+",\"ticks\":"+items+"}";
   char data[],result[]; string response_headers;
   int bytes=StringToCharArray(body,data,0,WHOLE_ARRAY,CP_UTF8); ArrayResize(data,bytes-1);
   int status=WebRequest("POST","http://127.0.0.1:8001/mt5/ticks",
       "Content-Type: application/json\r\nAuthorization: Bearer "+token+"\r\n",3000,data,result,response_headers);
   if(status==200 || status==201)
   {
      cursor=through;
      Print("Paper ticks accepted: ",used,"; coverage through ",I(through));
      return through<quote.time_msc;
   }
   else
   {
      Print("Paper tick batch not accepted. HTTP=",status);
      // A lost acknowledgement or second exporter may have advanced the cursor.
      // Re-anchor a new stream if initial history synchronization made its capture too old.
      if(status==409 || status==422) cursor=-1;
   }
   return false;
}

void OnTimer()
{
   ulong started=GetTickCount64();
   // Live flow remains one page every PollSeconds. During recovery send up to
   // eight consecutive ACKed pages, bounded by a 2s soft time budget.
   // A slow CopyTicks or WebRequest may exceed the soft budget once; no new
   // request is started after it. Every failure backs off to the next timer.
   for(int page=0;page<8 && !IsStopped();page++)
   {
      if(page>0 && GetTickCount64()-started>=2000) break;
      if(!ExportPage()) break;
   }
}
