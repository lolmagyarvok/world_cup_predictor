import requests
from typing import Dict, List, Any
from dotenv import load_dotenv
import os
import json


API_KEY = os.environ.get("API_KEY_ODDS_API", "")
SPORT = "soccer_fifa_world_cup"
MARKETS = "h2h"       # Head-to-head (Win/Draw/Loss)
REGIONS = "eu"         # European bookmakers (good for Decimal odds)
ODDS_FORMAT = "decimal"


TEAM_NAME_MAP = {
    "USA": "United States",
    "S. Korea": "South Korea",
    "UAE": "United Arab Emirates",
}

with open("simulation_predictions.json", "r", encoding="utf-8") as f:
    MY_PREDICTIONS = json.load(f)


def fetch_real_odds() -> List[Dict[str, Any]]:
    """Fetches upcoming or recent live odds from The Odds API."""
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds/"
    params = {
        "apiKey": API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT
    }
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching API data: {e}")
        return []

def get_market_odds(api_match: Dict[str, Any], bookmaker_name: str = "Betclic") -> Dict[str, float]:
    """
    Extracts home, away, and draw decimal odds for a specific bookmaker.
    Defaults to 'Betclic', but you can change it to any supported bookmaker (e.g., 'Bwin', 'Unibet').
    """
    odds_dict = {"home": 1.0, "away": 1.0, "draw": 1.0}
    
    # Locate the bookmaker
    bookmaker = next((b for b in api_match.get("bookmakers", []) if b["key"].lower() == bookmaker_name.lower()), None)
    if not bookmaker and api_match.get("bookmakers"):
        # Fallback to the first available bookmaker if preferred one isn't found
        bookmaker = api_match["bookmakers"][0]
        
    if bookmaker:
        market = next((m for m in bookmaker.get("markets", []) if m["key"] == "h2h"), None)
        if market:
            for outcome in market.get("outcomes", []):
                name = outcome["name"]
                price = outcome["price"]
                
                if name == api_match["home_team"]:
                    odds_dict["home"] = price
                elif name == api_match["away_team"]:
                    odds_dict["away"] = price
                elif name.lower() == "draw":
                    odds_dict["draw"] = price
                    
    return odds_dict

def evaluate_performance(predictions: List[Dict[str, Any]], odds_data: List[Dict[str, Any]]):
    """Matches predictions with odds data and prints performance metrics."""
    total_matches = len(predictions)
    correct_predictions = 0
    total_staked = 0.0
    total_returned = 0.0

    print("\n--- Match-by-Match Breakdown ---")
    
    for pred in predictions:
        # Standardize team names based on mapping rules
        home = TEAM_NAME_MAP.get(pred["home_team"], pred["home_team"])
        away = TEAM_NAME_MAP.get(pred["away_team"], pred["away_team"])
        
        # Look for the match in the API response payload
        match_odds = next((m for m in odds_data if m["home_team"] == home and m["away_team"] == away), None)
        
        # Default placeholder odds if match is missing or expired in the API cache
        odds = {"home": 2.0, "away": 2.0, "draw": 3.1} 
        if match_odds:
            odds = get_market_odds(match_odds)
            
        # Determine target odds based on what your model predicted
        if pred["predicted_winner"] == pred["home_team"]:
            target_odds = odds["home"]
        elif pred["predicted_winner"] == pred["away_team"]:
            target_odds = odds["away"]
        else:
            target_odds = odds["draw"]
            
        # Check prediction success
        is_correct = pred["predicted_winner"] == pred["actual_result"]
        stake = pred["stake"]
        total_staked += stake
        
        if is_correct:
            correct_predictions += 1
            returns = stake * target_odds
            total_returned += returns
            print(f"✅ {home} vs {away} | Predicted: {pred['predicted_winner']} | Odds: {target_odds} | Won: +${returns:.2f}")
        else:
            print(f"❌ {home} vs {away} | Predicted: {pred['predicted_winner']} | Actual: {pred['actual_result']} | Lost: -${stake:.2f}")

    # Financial & Accuracy Calculations
    accuracy = (correct_predictions / total_matches) * 100 if total_matches > 0 else 0
    net_profit = total_returned - total_staked
    roi = (net_profit / total_staked) * 100 if total_staked > 0 else 0

    print("\n==================================")
    print("      FINAL PERFORMANCE METRICS    ")
    print("==================================")
    print(f"Total Matches Simulated : {total_matches}")
    print(f"Prediction Accuracy     : {accuracy:.2f}% ({correct_predictions}/{total_matches})")
    print(f"Total Capital Staked    : ${total_staked:.2f}")
    print(f"Net Profit / Loss       : ${net_profit:.2f}")
    print(f"Return on Investment    : {roi:.2f}%")
    print("==================================")


if __name__ == "__main__":
    print("Fetching live tournament odds data...")
    raw_odds = fetch_real_odds()
    

    if not raw_odds:
        print("Proceeding with default backup odds for simulation evaluation.")
        
    evaluate_performance(MY_PREDICTIONS, raw_odds)