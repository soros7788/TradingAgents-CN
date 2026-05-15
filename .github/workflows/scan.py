import os, json, time, requests, pandas as pd, akshare as ak
from datetime import datetime
from zoneinfo import ZoneInfo

PERIOD = "30"
PERIOD_NAME = "30分钟"
WATCHLIST_FILE = "watchlist.json"

# 读取股票池
with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
    WATCHLIST = json.load(f)

# ==== Sina 30分钟K线 ====
def market_symbol(code):
    code = str(code).zfill(6)
    return "sh"+code if code.startswith(("600","601","603","605","688")) else "sz"+code

def get_30m_kline_sina(code):
    symbol = market_symbol(code)
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20KLC_ML=/CN_MinlineService.getMinlineData"
    params = {"symbol":symbol,"scale":"30","ma":"no","datalen":"120"}
    headers = {"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn/","Accept":"*/*","Connection":"close"}
    r=requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    text=r.text
    start=text.find("[")
    end=text.rfind("]")
    if start==-1 or end==-1: raise RuntimeError("新浪接口返回格式异常")
    data=json.loads(text[start:end+1])
    if not data: raise RuntimeError("新浪接口返回空K线")
    rows=[{"时间":x.get("day"),"开盘":x.get("open"),"最高":x.get("high"),
           "最低":x.get("low"),"收盘":x.get("close"),"成交额":x.get("volume",0)} for x in data]
    df=pd.DataFrame(rows)
    for col in ["开盘","收盘","最高","最低","成交额"]: df[col]=pd.to_numeric(df[col],errors="coerce")
    df=df.dropna(subset=["收盘","最高","最低"]).tail(90).reset_index(drop=True)
    if len(df)<60: raise RuntimeError(f"{PERIOD_NAME}K线不足60根，当前{len(df)}根")
    return df

# ==== akshare备用 ====
def get_30m_kline_akshare(code):
    df=ak.stock_zh_a_hist_min_em(symbol=str(code).zfill(6),period=PERIOD,adjust="qfq")
    df=df[["时间","开盘","收盘","最高","最低","成交额"]].copy()
    for col in ["开盘","收盘","最高","最低","成交额"]: df[col]=pd.to_numeric(df[col],errors="coerce")
    df=df.dropna(subset=["收盘","最高","最低"]).tail(90).reset_index(drop=True)
    if len(df)<60: raise RuntimeError(f"{PERIOD_NAME}K线不足60根，当前{len(df)}根")
    return df

# ==== 获取K线（新浪优先）====
def get_30m_kline(code):
    errors=[]
    for i in range(3):
        try: time.sleep(2+i); return get_30m_kline_sina(code)
        except Exception as e: errors.append(f"Sina{i+1}失败：{e}"); time.sleep(3+i*2)
    for i in range(3):
        try: time.sleep(2+i); return get_30m_kline_akshare(code)
        except Exception as e: errors.append(f"akshare{i+1}失败：{e}"); time.sleep(3+i*2)
    raise RuntimeError("30分钟K线获取失败："+"；".join(errors[-4:]))

# ==== 分析逻辑 ====
def near(a,b,tol=0.01): return a>0 and b>0 and abs(a-b)/max(a,b)<=tol
def analyze(code,name):
    df=get_30m_kline(code)
    current=float(df["收盘"].iloc[-1])
    last_time=str(df["时间"].iloc[-1])
    h5=float(df["最高"].tail(5).max())
    h20=float(df["最高"].tail(20).max())
    h60=float(df["最高"].tail(60).max())
    l2=float(df["最低"].tail(2).min())
    l5=float(df["最低"].tail(5).min())
    amount=float(df["成交额"].iloc[-1]) if "成交额" in df.columns else 0
    A=near(h5,h20) and near(h20,h60)
    B=near(l2,l5)
    D=current>h60*1.003
    F=current<l5*0.99
    if F: status="证伪"; reason=f"{PERIOD_NAME}收盘价 {current:.2f} 跌破支撑 {l5:.2f}"
    elif A and B and D: status="突破"; reason=f"{PERIOD_NAME}收盘价 {current:.2f} 突破结构高点 {h60:.2f}"
    elif A and B: status="等待"; reason=f"{PERIOD_NAME}结构有效，等待突破 {h60:.2f}"
    elif A: status="观察"; reason="高点压缩存在，但低点承接不足"
    else: status="未成型"; reason=f"不满足{PERIOD_NAME}K的5/20/60高点重合"
    return {"code":code,"name":name,"status":status,"reason":reason,"price":current,"last_time":last_time,"amount":amount/1e8 if amount else 0,"A":A,"B":B,"D":D,"F":F,"h5":h5,"h20":h20,"h60":h60,"l2":l2,"l5":l5}

# ==== 扫描并发送 Telegram ====
results=[]
for item in WATCHLIST:
    try: results.append(analyze(item["code"],item["name"]))
    except Exception as e: results.append({"code":item["code"],"name":item["name"],"status":"ERROR","reason":str(e)})

now=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
lines=[f"A股等突破扫描 ({PERIOD_NAME}K)","时间："+now]
for r in results:
    lines.append(f"{r['code']} {r['name']}｜{r['status']}  | {r['reason']}")

msg="\n".join(lines)
TELEGRAM_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")

requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
              data={"chat_id":TELEGRAM_CHAT_ID,"text":msg})
