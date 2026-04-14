import httpx

def get_stock_price(symbol: str) -> str:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
        res = httpx.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        data = res.json()
        price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        name = data["chart"]["result"][0]["meta"]["symbol"]
        return f"{name} is currently trading at ${price:.2f}"
    except Exception as e:
        return f"Could not fetch stock price for {symbol}."

def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
    try:
        url = f"https://open.er-api.com/v6/latest/{from_currency.upper()}"
        res = httpx.get(url, timeout=5)
        data = res.json()
        rate = data["rates"][to_currency.upper()]
        converted = amount * rate
        return f"{amount} {from_currency.upper()} = {converted:.2f} {to_currency.upper()}"
    except Exception as e:
        return f"Could not convert {from_currency} to {to_currency}."
