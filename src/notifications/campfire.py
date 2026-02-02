"""
Campfire Chat Integration - Post trade alerts to ATAT Campfire

Usage:
    from src.notifications.campfire import CampfireNotifier
    
    notifier = CampfireNotifier(
        base_url="https://community.atradeortwo.com",
        room_id="5",
        bot_key="9-QiPGzPSpJBcB"
    )
    notifier.send_message("Hello from the bot!")
    notifier.send_trade_alert(side="SHORT", entry=7002.50, stop=7017.25, target=6950.00)
"""

import requests
from typing import Optional
from loguru import logger


class CampfireNotifier:
    """Send messages to Campfire chat rooms via bot API"""
    
    def __init__(self, base_url: str, room_id: str, bot_key: str):
        self.base_url = base_url.rstrip('/')
        self.room_id = room_id
        self.bot_key = bot_key
        self.enabled = bool(bot_key)
    
    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/rooms/{self.room_id}/{self.bot_key}/messages"
    
    def send_message(self, message: str) -> bool:
        """Send a plain text message to the room"""
        if not self.enabled:
            logger.debug("Campfire notifier disabled")
            return False
        
        try:
            response = requests.post(
                self.endpoint,
                data=message.encode('utf-8'),
                headers={"Content-Type": "text/plain; charset=utf-8"},
                timeout=10
            )
            
            if response.status_code == 201:
                logger.info(f"Campfire message sent successfully")
                return True
            else:
                logger.error(f"Campfire failed: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Campfire error: {e}")
            return False
    
    def send_attachment(self, file_path: str) -> bool:
        """Send a file attachment (image, chart, etc.) to the room"""
        if not self.enabled:
            logger.debug("Campfire notifier disabled")
            return False
        
        try:
            from pathlib import Path
            path = Path(file_path)
            
            if not path.exists():
                logger.error(f"File not found: {file_path}")
                return False
            
            with open(path, 'rb') as f:
                files = {'attachment': (path.name, f)}
                response = requests.post(
                    self.endpoint,
                    files=files,
                    timeout=30
                )
            
            if response.status_code == 201:
                logger.info(f"Campfire attachment sent: {path.name}")
                return True
            else:
                logger.error(f"Campfire attachment failed: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Campfire attachment error: {e}")
            return False
    
    def send_trade_alert(self, 
                         side: str,
                         entry: float, 
                         stop: float, 
                         target: float,
                         symbol: str = "/ES",
                         zone_type: str = "",
                         confirmation: str = "",
                         rr_ratio: float = 0,
                         chart_path: str = None) -> bool:
        """Send a formatted trade alert, optionally with a chart"""
        
        # Calculate R:R if not provided
        if rr_ratio == 0 and stop != entry:
            risk = abs(entry - stop)
            reward = abs(target - entry)
            rr_ratio = reward / risk if risk > 0 else 0
        
        emoji = "🔴" if side.upper() == "SHORT" else "🟢"
        
        message = f"""🚨 {symbol} TRADE ALERT

{emoji} {side.upper()} entry at {entry:.2f}
⏹️ Stop: {stop:.2f}
🎯 Target: {target:.2f}
📊 R:R: {rr_ratio:.1f}"""

        if zone_type:
            message += f"\n\n📍 Zone: {zone_type.upper()}"
        if confirmation:
            message += f"\n✅ Confirmation: {confirmation}"
        
        # Send message first
        result = self.send_message(message)
        
        # Then send chart if provided
        if chart_path:
            self.send_attachment(chart_path)
        
        return result
    
    def send_trade_exit(self,
                        side: str,
                        entry: float,
                        exit_price: float,
                        pnl: float,
                        exit_reason: str = "",
                        symbol: str = "/ES") -> bool:
        """Send a trade exit notification"""
        
        emoji = "✅" if pnl > 0 else "❌"
        pnl_emoji = "💰" if pnl > 0 else "📉"
        
        message = f"""📤 {symbol} TRADE CLOSED

{emoji} {side.upper()} exited at {exit_price:.2f}
📥 Entry was: {entry:.2f}
{pnl_emoji} P&L: ${pnl:+.2f}"""

        if exit_reason:
            message += f"\n📋 Reason: {exit_reason}"
        
        return self.send_message(message)
    
    def send_status(self, 
                    price: float,
                    daily_pnl: float,
                    positions: int,
                    zone_status: str = "",
                    symbol: str = "/ES") -> bool:
        """Send a status update"""
        
        message = f"""📊 {symbol} Status Update

💵 Current: {price:.2f}
📈 Daily P&L: ${daily_pnl:+.2f}
📦 Open Positions: {positions}"""

        if zone_status:
            message += f"\n📍 {zone_status}"
        
        return self.send_message(message)


# Default instance for ATAT Campfire
def get_campfire_notifier(enabled: bool = False) -> CampfireNotifier:
    """
    Get the default Campfire notifier for ATAT community.
    
    NOTE: Disabled by default as of 2026-02-02.
    The SPX Trendy Edge bot alerts are for Jon privately.
    ORB alerts (separate bot) go to Campfire.
    """
    if not enabled:
        # Return a disabled notifier (empty bot_key)
        return CampfireNotifier(
            base_url="https://community.atradeortwo.com",
            room_id="5",
            bot_key=""  # Disabled
        )
    
    return CampfireNotifier(
        base_url="https://community.atradeortwo.com",
        room_id="5",
        bot_key="9-QiPGzPSpJBcB"
    )


if __name__ == "__main__":
    # Test the notifier
    notifier = get_campfire_notifier()
    notifier.send_message("🧪 Test message from Trixie trading bot")
