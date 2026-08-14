
import os, requests
from fastapi import FastAPI, Request
from google import genai

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    ticker = data.get("ticker", "N/A")
    action = data.get("action", "ALERT")
    price = data.get("price", "N/A")

    prompt = f"TradingView 신호 분석: 종목 {ticker}, 신호 {action}, 가격 {price}. GEX 데이터 및 기술적 관점 안전성 검증과 추천 Stop Loss를 3줄 요약해줘."

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)

    msg = f"🚨 [{action}] {ticker} (${price})\n\n🤖 **Gemini 분석:**\n{response.text}"
    requests.post(
        f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN')}/sendMessage",
        json={"chat_id": os.environ.get("TELEGRAM_CHAT_ID"), "text": msg, "parse_mode": "Markdown"}
    )
    return {"status": "ok"}
