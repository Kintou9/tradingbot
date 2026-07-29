"""
Fetches real OHLCV data from Twelve Data — this feeds everything else
(indicators.py, technical_scanner.py, backtesting).
"""

import os
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")


def fetch_ohlcv(ticker: str, interval: str = "1day", outputsize: int = 100) -> pd.DataFrame:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": ticker,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise ValueError(f"No data returned for {ticker}: {data}")

    df = pd.DataFrame(data["values"])
    df = df.rename(columns={"datetime": "date"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    return df


if __name__ == "__main__":
    df = fetch_ohlcv("AVPT")
    print(df.tail())
