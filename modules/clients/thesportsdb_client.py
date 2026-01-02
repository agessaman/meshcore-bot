import aiohttp
import asyncio
import time
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional
from .sports_mappings import (
    get_team_abbreviation_from_name, format_clean_date, 
    format_clean_date_time, is_soccer
)

class TheSportsDBClient:
    """Client for TheSportsDB API with rate limiting and parsing logic"""
    
    BASE_URL = "https://www.thesportsdb.com/api/v1/json"
    FREE_API_KEY = "123"  # Free public API key
    
    def __init__(self, logger=None, timeout: int = 10, session: Optional[aiohttp.ClientSession] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session = session
        self.last_request_time = 0
        self.min_request_interval = 2.1  # Slightly more than 2 seconds for safety

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self.session

    async def _rate_limit(self):
        """Enforce rate limiting asynchronously"""
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.min_request_interval:
            sleep_time = self.min_request_interval - time_since_last
            await asyncio.sleep(sleep_time)
        self.last_request_time = time.time()

    async def search_team(self, team_name: str) -> Optional[Dict]:
        """Search for a team by name"""
        await self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/searchteams.php"
        params = {'t': team_name}
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                teams = data.get('teams', [])
                return teams[0] if teams else None
        except Exception as e:
            self.logger.error(f"TheSportsDB search_team error: {e}")
            return None

    async def get_team_events_last(self, team_id: str, limit: int = 5) -> List[Dict]:
        """Get last N events for a team"""
        await self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventslast.php"
        params = {'id': team_id}
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                events = data.get('results', [])
                return events[:limit] if events else []
        except Exception as e:
            self.logger.error(f"TheSportsDB get_team_events_last error: {e}")
            return []

    async def get_team_events_next(self, team_id: str, limit: int = 5) -> List[Dict]:
        """Get next N events for a team"""
        await self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventsnext.php"
        params = {'id': team_id}
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                events = data.get('events', [])
                return events[:limit] if events else []
        except Exception as e:
            self.logger.error(f"TheSportsDB get_team_events_next error: {e}")
            return []

    async def fetch_team_games(self, sport: str, league: str, team_id: str) -> List[Dict]:
        """Fetch and parse team games (last and next)"""
        last_events_task = self.get_team_events_last(team_id, limit=5)
        next_events_task = self.get_team_events_next(team_id, limit=5)
        
        last_events, next_events = await asyncio.gather(last_events_task, next_events_task)
        
        all_games = []
        # Parse last events (completed games)
        for event in last_events:
            game_data = self.parse_event(event, team_id, sport, league)
            if game_data:
                all_games.append(game_data)
        
        # Parse next events (upcoming games)
        for event in next_events:
            game_data = self.parse_event(event, team_id, sport, league)
            if game_data:
                all_games.append(game_data)
        
        return all_games

    async def fetch_team_schedule(self, sport: str, league: str, team_id: str) -> List[Dict]:
        """Fetch upcoming scheduled games for a team"""
        next_events = await self.get_team_events_next(team_id, limit=10)
        
        upcoming_games = []
        for event in next_events:
            game_data = self.parse_event(event, team_id, sport, league)
            if game_data:
                upcoming_games.append(game_data)
        
        return upcoming_games

    def parse_event(self, event: Dict, team_id: str, sport: str, league: str) -> Optional[Dict]:
        """Parse a TheSportsDB event and return structured data with timestamp for sorting"""
        try:
            # Extract team info
            home_team = event.get('strHomeTeam', '')
            away_team = event.get('strAwayTeam', '')
            home_score = event.get('intHomeScore', '')
            away_score = event.get('intAwayScore', '')
            status = event.get('strStatus', 'UNKNOWN')
            timestamp_str = event.get('strTimestamp', '')
            date_str = event.get('dateEvent', '')
            time_str = event.get('strTime', '')
            
            # Determine if our team is home or away
            our_team_id = str(team_id)
            event_home_id = str(event.get('idHomeTeam', ''))
            is_home = (event_home_id == our_team_id)
            
            # Get team abbreviations
            home_abbr = get_team_abbreviation_from_name(home_team)
            away_abbr = get_team_abbreviation_from_name(away_team)
            
            # Get timestamp for sorting
            timestamp = 0
            event_timestamp = None
            if timestamp_str:
                try:
                    # fromisoformat handles 'Z' in Python 3.11+, but we force UTC if naive
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    event_timestamp = dt.timestamp()
                    timestamp = event_timestamp
                except:
                    if date_str and time_str:
                        try:
                            dt_str = f"{date_str} {time_str}"
                            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                            dt = dt.replace(tzinfo=timezone.utc)
                            event_timestamp = dt.timestamp()
                            timestamp = event_timestamp
                        except: pass
            
            formatted = ""
            if status == 'Match Finished':
                date_suffix = ""
                if event_timestamp:
                    dt = datetime.fromtimestamp(event_timestamp, tz=timezone.utc).astimezone()
                    if dt.date() != datetime.now().date():
                        date_suffix = f", {format_clean_date(dt)}"
                
                if home_score and away_score:
                    if is_soccer(sport):
                        formatted = f"@{home_abbr} {home_score}-{away_score} {away_abbr} (F{date_suffix})"
                    elif is_home:
                        formatted = f"{away_abbr} {away_score}-{home_score} @{home_abbr} (F{date_suffix})"
                    else:
                        formatted = f"@{home_abbr} {home_score}-{away_score} {away_abbr} (F{date_suffix})"
                else:
                    if is_soccer(sport):
                        formatted = f"@{home_abbr} vs. {away_abbr} (Final{date_suffix})"
                    else:
                        formatted = f"{away_abbr} vs. {home_abbr} (Final{date_suffix})"
                timestamp = 9999999998
            else:
                # Scheduled or TBD
                if event_timestamp:
                    dt = datetime.fromtimestamp(event_timestamp, tz=timezone.utc).astimezone()
                    time_str_formatted = format_clean_date_time(dt)
                    if is_soccer(sport):
                        formatted = f"@{home_abbr} vs. {away_abbr} ({time_str_formatted})"
                    else:
                        formatted = f"{away_abbr} @ {home_abbr} ({time_str_formatted})"
                else:
                    if is_soccer(sport):
                        formatted = f"@{home_abbr} vs. {away_abbr} (TBD)"
                    else:
                        formatted = f"{away_abbr} @ {home_abbr} (TBD)"
                    timestamp = 9999999999
            
            return {
                'id': event.get('idEvent'),
                'timestamp': timestamp,
                'event_timestamp': event_timestamp,
                'formatted': formatted,
                'sport': sport,
                'league': league,
                'status': status
            }
        except Exception as e:
            self.logger.error(f"Error parsing TheSportsDB event {event.get('idEvent')}: {e}")
            return None
    async def get_league_teams(self, league_id: str) -> List[Dict]:
        """Get all teams in a league"""
        await self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/lookup_all_teams.php"
        params = {'id': league_id}
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                teams = data.get('teams', [])
                return teams if teams else []
        except Exception as e:
            self.logger.error(f"TheSportsDB get_league_teams error: {e}")
            return []

    async def get_league_events_next(self, league_id: str, limit: int = 10) -> List[Dict]:
        """Get next N events for a league"""
        await self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventsnextleague.php"
        params = {'id': league_id}
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                events = data.get('events', [])
                return events[:limit] if events else []
        except Exception as e:
            self.logger.error(f"TheSportsDB get_league_events_next error: {e}")
            return []

    async def get_league_events_past(self, league_id: str, limit: int = 10) -> List[Dict]:
        """Get past N events for a league"""
        await self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventspastleague.php"
        params = {'id': league_id}
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                events = data.get('results', [])
                return events[:limit] if events else []
        except Exception as e:
            self.logger.error(f"TheSportsDB get_league_events_past error: {e}")
            return []

    async def get_events_by_day(self, date_str: str, league_id: str = None) -> List[Dict]:
        """Get events for a specific day"""
        await self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventsday.php"
        params = {'d': date_str}
        if league_id:
            params['l'] = league_id
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                events = data.get('events', [])
                if events is None: return []
                return events if isinstance(events, list) else []
        except Exception as e:
            self.logger.error(f"TheSportsDB get_events_by_day error: {e}")
            return []

    async def fetch_league_scores(self, sport: str, league_name: str, league_id: str) -> List[Dict]:
        """Fetch and parse league scores from multiple sources"""
        from datetime import timedelta
        
        # Get today's date and next few days
        today = datetime.now().date()
        date_strings = [today.strftime('%Y-%m-%d')]
        for i in range(1, 7):  # Next 6 days
            date_strings.append((today + timedelta(days=i)).strftime('%Y-%m-%d'))
        
        # Fetch events from multiple sources in parallel
        next_events_task = self.get_league_events_next(league_id, limit=15)
        past_events_task = self.get_league_events_past(league_id, limit=5)
        day_events_tasks = [self.get_events_by_day(d, league_id) for d in date_strings]
        
        # Wait for all requests
        results = await asyncio.gather(next_events_task, past_events_task, *day_events_tasks)
        next_events = results[0]
        past_events = results[1]
        day_events_list = results[2:]
        
        # Combine and parse
        all_event_data = []
        seen_event_ids = set()
        
        # Helper to add parsed events
        def add_parsed(events):
            for event in events:
                event_id = event.get('idEvent')
                if event_id and event_id not in seen_event_ids:
                    parsed = self.parse_event(event, "", sport, league_name)
                    if parsed:
                        all_event_data.append(parsed)
                        seen_event_ids.add(event_id)
        
        add_parsed(next_events)
        add_parsed(past_events)
        for day_events in day_events_list:
            add_parsed(day_events)
            
        return all_event_data
