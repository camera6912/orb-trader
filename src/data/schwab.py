"""
Schwab API Integration - Real-time market data and trading

Uses schwab-py library for OAuth and API access.
https://github.com/alexgolec/schwab-py
"""

import os
import json
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List
import pandas as pd
from loguru import logger

try:
    import schwab
    from schwab import auth
    from schwab.client import Client
    SCHWAB_AVAILABLE = True
except ImportError:
    SCHWAB_AVAILABLE = False
    logger.warning("schwab-py not installed. Run: pip install schwab-py")


class SchwabClient:
    """
    Schwab API client for market data and trading.
    
    Features:
    - Real-time SPX quotes
    - Options chain data
    - Historical price data
    - Paper and live trading
    """
    
    def __init__(self, config_path: str = 'config/secrets.yaml'):
        if not SCHWAB_AVAILABLE:
            raise ImportError("schwab-py required. Install with: pip install schwab-py")
        
        self.config = self._load_config(config_path)
        self.client: Optional[Client] = None
        self.token_path = Path(self.config.get('token_path', './config/schwab_token.json'))
    
    def _load_config(self, path: str) -> dict:
        """Load Schwab credentials from config file"""
        config_file = Path(path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        
        with open(config_file) as f:
            config = yaml.safe_load(f)
        
        return config.get('schwab', {})
    
    def authenticate(self, interactive: bool = True) -> bool:
        """
        Authenticate with Schwab API.
        
        First run requires interactive OAuth flow.
        Subsequent runs use cached token.
        
        Args:
            interactive: If True, opens browser for OAuth
            
        Returns:
            True if authenticated successfully
        """
        app_key = self.config['app_key']
        app_secret = self.config['app_secret']
        callback_url = self.config['callback_url']
        
        try:
            # Try to use existing token
            if self.token_path.exists():
                self.client = auth.client_from_token_file(
                    token_path=str(self.token_path),
                    api_key=app_key,
                    app_secret=app_secret
                )
                logger.info("Authenticated with cached token")
                return True
        except Exception as e:
            logger.warning(f"Cached token invalid: {e}")
        
        if not interactive:
            logger.error("No valid token and interactive=False")
            return False
        
        # Interactive OAuth flow
        logger.info("Starting OAuth flow - browser will open...")
        try:
            self.client = auth.client_from_manual_flow(
                api_key=app_key,
                app_secret=app_secret,
                callback_url=callback_url,
                token_path=str(self.token_path)
            )
            logger.info("Authentication successful!")
            return True
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return False
    
    def get_quote(self, symbol: str = '$SPX') -> dict:
        """
        Get real-time quote for a symbol.
        
        Args:
            symbol: Ticker symbol ($SPX for S&P 500 index)
            
        Returns:
            Quote data dict
        """
        if not self.client:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        
        response = self.client.get_quote(symbol)
        if response.status_code != 200:
            raise RuntimeError(f"Quote failed: {response.text}")
        
        return response.json()
    
    def get_quotes(self, symbols: List[str]) -> dict:
        """
        Get real-time quotes for multiple symbols.
        Required for futures symbols like /ES which need URL encoding.
        
        Args:
            symbols: List of ticker symbols
            
        Returns:
            Quote data dict keyed by resolved symbol
        """
        if not self.client:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        
        response = self.client.get_quotes(symbols)
        if response.status_code != 200:
            raise RuntimeError(f"Quotes failed: {response.text}")
        
        return response.json()
    
    def get_spx_price(self) -> float:
        """Get current SPX index price"""
        quote = self.get_quote('$SPX')
        return quote.get('$SPX', {}).get('quote', {}).get('lastPrice', 0.0)
    
    def get_es_price(self) -> float:
        """Get current /ES futures price (front month)"""
        quotes = self.get_quotes(['/ES'])
        # Schwab resolves /ES to the front-month contract (e.g., /ESH26)
        for symbol, data in quotes.items():
            if data.get('assetMainType') == 'FUTURE':
                return data.get('quote', {}).get('lastPrice', 0.0)
        return 0.0
    
    def get_es_quote(self) -> dict:
        """Get full /ES futures quote with all fields"""
        quotes = self.get_quotes(['/ES'])
        for symbol, data in quotes.items():
            if data.get('assetMainType') == 'FUTURE':
                return {
                    'symbol': symbol,
                    'price': data.get('quote', {}).get('lastPrice', 0.0),
                    'bid': data.get('quote', {}).get('bidPrice', 0.0),
                    'ask': data.get('quote', {}).get('askPrice', 0.0),
                    'high': data.get('quote', {}).get('highPrice', 0.0),
                    'low': data.get('quote', {}).get('lowPrice', 0.0),
                    'open': data.get('quote', {}).get('openPrice', 0.0),
                    'close': data.get('quote', {}).get('closePrice', 0.0),
                    'volume': data.get('quote', {}).get('totalVolume', 0),
                    'change': data.get('quote', {}).get('netChange', 0.0),
                    'change_pct': data.get('quote', {}).get('futurePercentChange', 0.0),
                    'multiplier': data.get('reference', {}).get('futureMultiplier', 50.0),
                    'tick_size': data.get('quote', {}).get('tick', 0.25),
                    'tick_value': data.get('quote', {}).get('tickAmount', 12.5),
                }
        return {}
    
    def get_price_history(self, symbol: str = '$SPX',
                         period_type: str = 'day',
                         period: int = 10,
                         frequency_type: str = 'minute',
                         frequency: int = 5) -> pd.DataFrame:
        """
        Get historical price data.
        
        Args:
            symbol: Ticker symbol
            period_type: day, month, year, ytd
            period: Number of periods
            frequency_type: minute, daily, weekly, monthly
            frequency: Candle size (1, 5, 15, 30 for minutes)
            
        Returns:
            DataFrame with OHLCV data
        """
        if not self.client:
            raise RuntimeError("Not authenticated")
        
        response = self.client.get_price_history(
            symbol=symbol,
            period_type=getattr(self.client.PriceHistory.PeriodType, period_type.upper()),
            period=self.client.PriceHistory.Period.ONE_DAY,  # Always use 1 day for intraday
            frequency_type=getattr(self.client.PriceHistory.FrequencyType, frequency_type.upper()),
            frequency=self.client.PriceHistory.Frequency.EVERY_MINUTE if frequency == 1 else self.client.PriceHistory.Frequency.EVERY_FIVE_MINUTES
        )
        
        if response.status_code != 200:
            raise RuntimeError(f"History failed: {response.text}")
        
        data = response.json()
        candles = data.get('candles', [])
        
        if not candles:
            return pd.DataFrame()
        
        df = pd.DataFrame(candles)
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df = df.set_index('datetime')
        df = df.rename(columns={'datetime': 'timestamp'})
        
        return df[['open', 'high', 'low', 'close', 'volume']]
    
    def get_options_chain(self, symbol: str = '$SPX',
                          contract_type: str = 'ALL',
                          strike_count: int = 10,
                          from_date: Optional[datetime] = None,
                          to_date: Optional[datetime] = None) -> dict:
        """
        Get options chain for a symbol.
        
        Args:
            symbol: Underlying symbol
            contract_type: CALL, PUT, or ALL
            strike_count: Number of strikes above/below ATM
            from_date: Start of expiration range
            to_date: End of expiration range
            
        Returns:
            Options chain data
        """
        if not self.client:
            raise RuntimeError("Not authenticated")
        
        if from_date is None:
            from_date = datetime.now()
        if to_date is None:
            to_date = from_date + timedelta(days=1)  # 0DTE/1DTE
        
        response = self.client.get_option_chain(
            symbol=symbol,
            contract_type=getattr(self.client.Options.ContractType, contract_type),
            strike_count=strike_count,
            from_date=from_date,
            to_date=to_date
        )
        
        if response.status_code != 200:
            raise RuntimeError(f"Options chain failed: {response.text}")
        
        return response.json()
    
    def find_option_strike(self, symbol: str = '$SPX',
                           option_type: str = 'CALL',
                           target_delta: float = 0.45,
                           expiration: str = '0DTE') -> dict:
        """
        Find option strike matching target delta.
        
        Args:
            symbol: Underlying
            option_type: CALL or PUT
            target_delta: Target delta (0.40-0.50 recommended)
            expiration: '0DTE' or '1DTE'
            
        Returns:
            Option contract details
        """
        # Get today's or tomorrow's expiration
        today = datetime.now().date()
        if expiration == '1DTE':
            exp_date = today + timedelta(days=1)
        else:
            exp_date = today
        
        chain = self.get_options_chain(
            symbol=symbol,
            contract_type=option_type,
            from_date=datetime.combine(exp_date, datetime.min.time()),
            to_date=datetime.combine(exp_date, datetime.max.time())
        )
        
        # Find strike closest to target delta
        options = chain.get('callExpDateMap' if option_type == 'CALL' else 'putExpDateMap', {})
        
        best_match = None
        best_delta_diff = float('inf')
        
        for exp_key, strikes in options.items():
            for strike, contracts in strikes.items():
                for contract in contracts:
                    delta = abs(contract.get('delta', 0))
                    diff = abs(delta - target_delta)
                    if diff < best_delta_diff:
                        best_delta_diff = diff
                        best_match = contract
        
        return best_match or {}


def setup_schwab_auth():
    """CLI helper to set up Schwab authentication"""
    print("=" * 50)
    print("Schwab API Authentication Setup")
    print("=" * 50)
    
    client = SchwabClient()
    
    print("\nThis will open a browser window for OAuth authentication.")
    print("After logging in to Schwab, you'll be redirected to a localhost URL.")
    print("Copy that URL and paste it when prompted.\n")
    
    input("Press Enter to continue...")
    
    if client.authenticate(interactive=True):
        print("\n✅ Authentication successful! Token saved.")
        print(f"Token file: {client.token_path}")
    else:
        print("\n❌ Authentication failed.")


if __name__ == '__main__':
    setup_schwab_auth()
