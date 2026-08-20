import os
import requests

from fastapi import FastAPI, Request
from openai import OpenAI

app = FastAPI()


@app.get("/")
def health():
    return {"status": "Market Forge online"}


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    ticker = str(data.get("ticker", "N/A")).upper()
    action = str(data.get("action", "ALERT")).upper()
    price = data.get("price", "N/A")

    # ---------------------------------------------
    # Signal display
    # ---------------------------------------------
    signal_map = {
        "C": ("🟢", "CALL"),
        "MC": ("🚀", "MOMENTUM CALL"),
        "P": ("🔴", "PUT"),
        "MP": ("🔻", "MOMENTUM PUT"),
        "CMP": ("⚠️", "CRASH MOMENTUM"),
        "SCMP": ("🚨", "SHOCK CRASH"),
        "CTN UP": ("⬆️", "CONTINUATION UP"),
        "CTN↑": ("⬆️", "CONTINUATION UP"),
        "CTN DOWN": ("⬇️", "CONTINUATION DOWN"),
        "CTN↓": ("⬇️", "CONTINUATION DOWN"),
        "X": ("🟡", "EXIT"),
        "SX": ("🟠", "SHOCK EXIT"),
        "HX": ("🛑", "HARD EXIT"),
    }

    icon, signal_name = signal_map.get(
        action,
        ("🔔", action)
    )

    # ---------------------------------------------
    # Exit signals do not need AI analysis
    # ---------------------------------------------
    if action in {"X", "SX", "HX"}:
        exit_text = {
            "X": "Trend weakening",
            "SX": "Sharp reversal detected",
            "HX": "Trend structure broken",
        }.get(action, "Exit signal")

        msg = (
            f"{icon} {ticker} | {action} | ${price}\n"
            f"{exit_text}\n"
            f"Market Forge AI"
        )

    else:
        # -----------------------------------------
        # Very short AI classification
        # -----------------------------------------
        prompt = f"""
You are the Market Forge signal classifier.

TradingView signal:
Ticker: {ticker}
Signal: {action}
Price: {price}

Return ONLY exactly these 4 lines in Korean/English mixed format:

Momentum: HIGH, MED, or LOW
Risk: HIGH, MED, or LOW
Trend: BULLISH, BEARISH, or NEUTRAL
Note: maximum 8 Korean words

Do not give investment advice.
Do not write explanations.
Do not use markdown.
"""

        try:
            client = OpenAI(
                api_key=os.environ.get("OPENAI_API_KEY")
            )

            response = client.responses.create(
                model="gpt-5.6-luna",
                input=prompt
            )

            analysis = response.output_text.strip()

        except Exception as e:
            print("OpenAI error:", str(e))

            analysis = (
                "Momentum: MED\n"
                "Risk: MED\n"
                "Trend: NEUTRAL\n"
                "Note: 추가 확인 필요"
            )

        msg = (
            f"{icon} {ticker} | {action} | ${price}\n"
            f"{analysis}\n"
            f"Market Forge AI"
        )

    # ---------------------------------------------
    # Telegram
    # ---------------------------------------------
    telegram_response = requests.post(
        f"https://api.telegram.org/bot"
        f"{os.environ.get('TELEGRAM_BOT_TOKEN')}"
        f"/sendMessage",
        json={
            "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
            "text": msg,
        },
        timeout=20,
    )

    print(
        "Telegram status:",
        telegram_response.status_code
    )

    print(
        "Telegram response:",
        telegram_response.text
    )

    return {
        "status": "ok",
        "ticker": ticker,
        "action": action,
        "telegram_status":
            telegram_response.status_code,
    }
