from datetime import datetime
import httpx
from memory import load_memory, load_memory_async

DEFAULT_INTERESTS = [
    "entrepreneurship", "startups", "business", "finance", "fundraising", "venture capital", "India business"
]

def get_news(user_id: str, topic=None):
    try:
        from ddgs import DDGS
        memory = load_memory(user_id)
        interests = memory.get("interests", DEFAULT_INTERESTS)
        query = topic if topic else " OR ".join(interests[:3]) + " news today"
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        if not results:
            return "No news found."
        return "\n".join(["- " + r.get("title", "") + ": " + r.get("body", "")[:100] for r in results if r.get("title")])
    except Exception as e:
        return "Could not fetch news: " + str(e)

async def get_news_async(user_id: str, topic=None):
    try:
        from ddgs import DDGS
        memory = await load_memory_async(user_id)
        interests = memory.get("interests", DEFAULT_INTERESTS)
        query = topic if topic else " OR ".join(interests[:3]) + " news today"
        # DDGS is blocking, but we can run it in a thread
        import asyncio
        loop = asyncio.get_event_loop()
        def _fetch():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=5))
        results = await loop.run_in_executor(None, _fetch)
        if not results:
            return "No news found."
        return "\n".join(["- " + r.get("title", "") + ": " + r.get("body", "")[:100] for r in results if r.get("title")])
    except Exception as e:
        return "Could not fetch news: " + str(e)

def get_weather(user_id: str):
    try:
        memory = load_memory(user_id)
        city = memory.get("city", "Bengaluru")
        res = httpx.get(f"https://wttr.in/{city}?format=j1", timeout=5)
        data = res.json()
        current = data["current_condition"][0]
        return f"It is {current['temp_C']}°C in {city}, feels like {current['FeelsLikeC']}°C. {current['weatherDesc'][0]['value']}. Humidity is {current['humidity']}%."
    except Exception as e:
        return "Could not fetch weather."

async def get_weather_async(user_id: str):
    try:
        memory = await load_memory_async(user_id)
        city = memory.get("city", "Bengaluru")
        async with httpx.AsyncClient() as client:
            res = await client.get(f"https://wttr.in/{city}?format=j1", timeout=5)
            data = res.json()
            current = data["current_condition"][0]
            return f"It is {current['temp_C']}°C in {city}, feels like {current['FeelsLikeC']}°C. {current['weatherDesc'][0]['value']}. Humidity is {current['humidity']}%."
    except Exception as e:
        return "Could not fetch weather."

def get_stocks(user_id: str):
    try:
        memory = load_memory(user_id)
        symbols = memory.get("stocks", {"Sensex": "^BSESN", "Nifty": "^NSEI", "Apple": "AAPL", "Nvidia": "NVDA"})
        results = []
        for name, symbol in symbols.items():
            res = httpx.get("https://query1.finance.yahoo.com/v8/finance/chart/" + symbol, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            data = res.json()
            chart_result = data.get("chart", {}).get("result") or []
            if chart_result:
                price = chart_result[0]["meta"]["regularMarketPrice"]
                results.append(name + ": " + str(round(price, 2)))
        return " | ".join(results)
    except:
        return "Could not fetch stocks."

async def get_stocks_async(user_id: str):
    try:
        memory = await load_memory_async(user_id)
        symbols = memory.get("stocks", {"Sensex": "^BSESN", "Nifty": "^NSEI", "Apple": "AAPL", "Nvidia": "NVDA"})
        results = []
        async with httpx.AsyncClient() as client:
            for name, symbol in symbols.items():
                try:
                    res = await client.get("https://query1.finance.yahoo.com/v8/finance/chart/" + symbol, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    data = res.json()
                    chart_result = data.get("chart", {}).get("result") or []
                    if chart_result:
                        price = chart_result[0]["meta"]["regularMarketPrice"]
                        results.append(name + ": " + str(round(price, 2)))
                except:
                    continue
        return " | ".join(results)
    except:
        return "Could not fetch stocks."

def morning_briefing(user_id: str):
    greeting = _get_greeting()
    weather = get_weather(user_id)
    stocks = get_stocks(user_id)
    return f"{greeting}. Here is your briefing.\n\nWeather: {weather}\n\nMarkets: {stocks}"

async def morning_briefing_async(user_id: str):
    greeting = _get_greeting()
    weather = await get_weather_async(user_id)
    stocks = await get_stocks_async(user_id)
    return f"{greeting}. Here is your briefing.\n\nWeather: {weather}\n\nMarkets: {stocks}"

def _get_greeting():
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    else:
        return "Good evening"

def end_of_day():
    return "How was your day? What did you accomplish and what is pending for tomorrow?"
