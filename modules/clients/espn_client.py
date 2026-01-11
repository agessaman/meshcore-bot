import aiohttp
import logging
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Optional
from .sports_mappings import is_womens_league, get_team_abbreviation, format_clean_date, format_clean_date_time

class ESPNClient:
    """Client for ESPN API using aiohttp for asynchronous requests"""
    
    BASE_URL = "http://site.api.espn.com/apis/site/v2/sports"
    
    def __init__(self, logger: Optional[logging.Logger] = None, timeout: int = 10, session: Optional[aiohttp.ClientSession] = None):
        """Initialize the ESPN API client.

        Args:
            logger: Logger instance for error and info logging. If None, creates a default logger.
            timeout: Request timeout in seconds (default: 10)
            session: Optional existing aiohttp session to reuse. If None, creates new sessions as needed.
        """
        self.logger = logger or logging.getLogger(__name__)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session = session

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self.session

    async def fetch_scoreboard(self, sport: str, league: str) -> List[Dict]:
        """Fetch and parse scoreboard data for a league"""
        url = f"{self.BASE_URL}/{sport}/{league}/scoreboard"
        try:
            session = await self._get_session()
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                events = data.get('events', [])
                
                parsed_events = []
                for event in events:
                    parsed = self.parse_league_game_event(event, sport, league)
                    if parsed:
                        parsed_events.append(parsed)
                return parsed_events
        except Exception as e:
            self.logger.error(f"ESPN fetch_scoreboard error for {sport}/{league}: {e}")
            return []

    async def fetch_team_schedule(self, sport: str, league: str, team_id: str) -> List[Dict]:
        """Fetch and parse schedule data for a team

        For soccer teams, if the team schedule has no upcoming games, we fall back
        to searching the league scoreboard for games involving this team.
        """
        url = f"{self.BASE_URL}/{sport}/{league}/teams/{team_id}/schedule"
        try:
            session = await self._get_session()
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                events = data.get('events', [])

                parsed_events = []
                for event in events:
                    parsed = self.parse_game_event_with_timestamp(event, team_id, sport, league)
                    if parsed:
                        parsed_events.append(parsed)

                # For soccer, if no upcoming games found, check league scoreboard
                if sport == 'soccer':
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc).timestamp()
                    has_upcoming = any(
                        g.get('event_timestamp', 0) > now for g in parsed_events
                    )

                    if not has_upcoming:
                        # Fall back to league scoreboard to find this team's games
                        scoreboard_games = await self._find_team_in_scoreboard(sport, league, team_id)
                        if scoreboard_games:
                            parsed_events.extend(scoreboard_games)

                return parsed_events
        except Exception as e:
            self.logger.error(f"ESPN fetch_team_schedule error for {team_id}: {e}")
            return []

    async def _find_team_in_scoreboard(self, sport: str, league: str, team_id: str) -> List[Dict]:
        """Find games for a specific team in the league scoreboard"""
        url = f"{self.BASE_URL}/{sport}/{league}/scoreboard"
        try:
            session = await self._get_session()
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                events = data.get('events', [])

                team_games = []
                for event in events:
                    # Check if this team is in this event
                    competitions = event.get('competitions', [])
                    if not competitions:
                        continue

                    competition = competitions[0]
                    competitors = competition.get('competitors', [])

                    # Check if our team is in this game
                    team_in_game = False
                    for competitor in competitors:
                        if str(competitor.get('team', {}).get('id', '')) == str(team_id):
                            team_in_game = True
                            break

                    if team_in_game:
                        parsed = self.parse_game_event_with_timestamp(event, team_id, sport, league)
                        if parsed:
                            team_games.append(parsed)

                return team_games
        except Exception as e:
            self.logger.error(f"Error finding team in scoreboard: {e}")
            return []

    async def fetch_live_event_data(self, event_id: str, sport: str, league: str) -> Optional[Dict]:
        """Fetch live event data from the scoreboard endpoint for real-time scores

        The scoreboard endpoint provides more up-to-date scores for live games than the schedule endpoint.
        We fetch the scoreboard and find the matching event by ID.
        """
        url = f"{self.BASE_URL}/{sport}/{league}/scoreboard"
        try:
            session = await self._get_session()
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()

                # Find the event with matching ID in the scoreboard
                # Convert event_id to string for comparison (API may return IDs as strings or ints)
                event_id_str = str(event_id)
                events = data.get('events', [])
                for event in events:
                    event_id_from_api = str(event.get('id', ''))
                    if event_id_from_api == event_id_str:
                        return event

                # If not found in scoreboard, return None (event might not be live anymore)
                return None
        except Exception as e:
            self.logger.error(f"ESPN fetch_live_event_data error for {event_id}: {e}")
            return None

    def extract_score(self, competitor: Dict) -> str:
        """Extract score value from competitor data"""
        score = competitor.get('score', '0')
        if isinstance(score, dict):
            if 'displayValue' in score:
                return str(score['displayValue'])
            elif 'value' in score:
                value = score['value']
                if isinstance(value, float) and value.is_integer():
                    return str(int(value))
                return str(value)
            return '0'
        if isinstance(score, str):
            return score
        if isinstance(score, (int, float)):
            if isinstance(score, float) and score.is_integer():
                return str(int(score))
            return str(score)
        return '0'

    def extract_shootout_score(self, competitor: Dict) -> Optional[int]:
        """Extract penalty shootout score from competitor data"""
        score = competitor.get('score', {})
        if isinstance(score, dict) and 'shootoutScore' in score:
            shootout = score['shootoutScore']
            if isinstance(shootout, (int, float)):
                return int(shootout)
        return None

    def parse_game_event_with_timestamp(self, event: Dict, team_id: str, sport: str, league: str) -> Optional[Dict]:
        """Parse a game event and return structured data with timestamp for sorting"""
        try:
            competitions = event.get('competitions', [])
            if not competitions:
                return None
            
            competition = competitions[0]
            competitors = competition.get('competitors', [])
            
            if len(competitors) != 2:
                return None
            
            # Extract team info
            team1 = competitors[0]
            team2 = competitors[1]
            
            # Determine home/away
            home_team = team1 if team1.get('homeAway') == 'home' else team2
            away_team = team2 if team1.get('homeAway') == 'home' else team1
            
            home_id = home_team.get('team', {}).get('id', '')
            away_id = away_team.get('team', {}).get('id', '')
            home_abbr = home_team.get('team', {}).get('abbreviation', 'UNK')
            away_abbr = away_team.get('team', {}).get('abbreviation', 'UNK')
            
            home_name = get_team_abbreviation(home_id, home_abbr, sport, league)
            away_name = get_team_abbreviation(away_id, away_abbr, sport, league)
            
            home_score = self.extract_score(home_team)
            away_score = self.extract_score(away_team)
            
            # Get game status
            status_obj = competition.get('status', event.get('status', {}))
            status_type = status_obj.get('type', {})
            status_name = status_type.get('name', 'UNKNOWN')
            
            # Get timestamp for sorting
            date_str = event.get('date', '')
            timestamp = 0
            event_timestamp = None
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    event_timestamp = dt.timestamp()
                    timestamp = event_timestamp
                except:
                    pass
            
            # Format based on game status
            formatted = ""
            if status_name in ['STATUS_IN_PROGRESS', 'STATUS_FIRST_HALF', 'STATUS_SECOND_HALF', 'STATUS_END_PERIOD']:
                # Game is live
                clock = status_obj.get('displayClock', '')
                period = status_obj.get('period', 0)
                is_end_period = (status_name == 'STATUS_END_PERIOD')
                
                if sport == 'soccer':
                    # Soccer: @Home Score-Score Away (Clock)
                    period_str = clock if (clock and clock != '0:00' and clock != "0'") else f"{period}H"
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} ({period_str})"
                elif sport == 'baseball':
                    short_detail = status_type.get('shortDetail', '')
                    period_str = short_detail if ('Top' in short_detail or 'Bottom' in short_detail) else f"{period}I"
                    if is_end_period: period_str = f"End {period_str}"
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({period_str})"
                elif sport == 'football':
                    period_str = f"Q{period}"
                    if is_end_period: period_str = f"End {period_str}"
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({clock} {period_str})"
                else:
                    period_str = f"P{period}"
                    if is_end_period: period_str = f"End {period_str}"
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({clock} {period_str})"
                
                timestamp = -1 # Live games first
                
            elif status_name == 'STATUS_SCHEDULED':
                # Scheduled
                if event_timestamp:
                    dt = datetime.fromtimestamp(event_timestamp, tz=timezone.utc).astimezone()
                    time_str = format_clean_date_time(dt)
                    if sport == 'soccer':
                        formatted = f"@{home_name} vs. {away_name} ({time_str})"
                    else:
                        formatted = f"{away_abbr} @ {home_abbr} ({time_str})"
                else:
                    formatted = f"{away_abbr} @ {home_abbr} (TBD)" if sport != 'soccer' else f"@{home_name} vs. {away_name} (TBD)"
                    timestamp = 9999999999
                    
            elif status_name == 'STATUS_HALFTIME':
                if sport == 'soccer':
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} (HT)"
                else:
                    formatted = f"{away_abbr} {away_score}-{home_score} @{home_abbr} (HT)"
                timestamp = -2
                
            elif status_name in ['STATUS_FINAL', 'STATUS_FULL_TIME', 'STATUS_FINAL_PEN', 'STATUS_POSTPONED']:
                date_suffix = ""
                if event_timestamp:
                    dt = datetime.fromtimestamp(event_timestamp, tz=timezone.utc).astimezone()
                    if dt.date() != datetime.now().date():
                        date_suffix = f", {format_clean_date(dt)}"
                
                if status_name == 'STATUS_FINAL_PEN':
                    home_shootout = self.extract_shootout_score(home_team)
                    away_shootout = self.extract_shootout_score(away_team)
                    pen_str = f"FT-PEN {home_shootout}-{away_shootout}" if home_shootout is not None else "FT-PEN"
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} ({pen_str}{date_suffix})"
                elif status_name == 'STATUS_FULL_TIME':
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} (FT{date_suffix})"
                elif status_name == 'STATUS_POSTPONED':
                    formatted = f"{away_abbr} @ {home_abbr} (Postponed{date_suffix})"
                else:
                    formatted = f"{away_abbr} {away_score}-{home_score} @{home_abbr} (F{date_suffix})"
                
                timestamp = 9999999998
            else:
                prefix = "@" if sport == 'soccer' else ""
                suffix = " vs. " if sport == 'soccer' else " @ "
                if sport == 'soccer':
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} ({status_name})"
                else:
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({status_name})"
                timestamp = 9999999997

            return {
                'id': event.get('id'),
                'timestamp': timestamp,
                'event_timestamp': event_timestamp,
                'formatted': formatted,
                'sport': sport,
                'league': league,
                'status': status_name
            }
        except Exception as e:
            self.logger.error(f"Error parsing ESPN event {event.get('id')}: {e}")
            return None

    def parse_league_game_event(self, event: Dict, sport: str, league: str) -> Optional[Dict]:
        """Parse a league game event (scoreboard)"""
        return self.parse_game_event_with_timestamp(event, "", sport, league)
