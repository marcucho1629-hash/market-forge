import os
import requests
from fastapi import FastAPI, Request
from openai import OpenAI

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    ticker = data.get("ticker", "N/A")
    action = data.get("action", "ALERT")
    price = data.get("price", "N/A")

    prompt = (
        f"TradingView 신호 분석:\n"
        f"종목: {ticker}\n"
        f"신호: {action}\n"
        f"가격: {price}\n\n"
        f"Market Forge 관점에서 이 신호의 의미를 짧게 분석해줘. "
        f"추세 지속 가능성, 주의할 점, 리스크를 한국어로 간결하게 정리해줘. "
        f"개인 투자 조언이 아니라 교육용 분석으로 작성해줘."
    )

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    response = client.responses.create(
        model="gpt-5.5",
        input=prompt
    )

    analysis = response.output_text

    msg = (
        f"🚨 [{action}] {ticker} (${price})\n\n"
        f"🤖 **Market Forge AI 분석:**\n{analysis}"
    )

    requests.post(f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN')}/sendMessage",
    json={
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
        "text": msg
    }
)

print("Telegram status:", telegram_response.status_code)
print("Telegram response:", telegram_response.text)

return {"status": "ok"}
       
    return {"status": "ok"}
