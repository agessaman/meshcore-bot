#!/usr/bin/env python3
"""
Sports command for the MeshCore Bot
Provides sports scores and schedules using ESPN API
API description via https://github.com/zuplo/espn-openapi/

Team ID Stability:
ESPN team IDs are generally stable but can change in certain circumstances:
- Team relocation or renaming
- Expansion teams (new teams added to leagues)
- ESPN data system updates

If a team returns "No games found", verify the team_id using:
  python3 test_scripts/find_espn_team_id.py <sport> <league> <team_name>

Team IDs should be periodically verified, especially after:
- League expansion announcements
- Team relocations or rebranding
- When users report "no games found" for known active teams
"""

from datetime import datetime, timezone
from typing import List, Dict, Optional, TYPE_CHECKING
from .base_command import BaseCommand
from ..models import MeshMessage
from ..clients.espn_client import ESPNClient
from ..clients.thesportsdb_client import TheSportsDBClient
from ..clients.sports_mappings import (
    SPORT_EMOJIS, TEAM_MAPPINGS, LEAGUE_MAPPINGS
)

if TYPE_CHECKING:
    from ..core import MeshCoreBot


class SportsCommand(BaseCommand):
    """Handles sports commands with ESPN API integration"""
    
    # Plugin metadata
    name = "sports"
    keywords = ['sports', 'score', 'scores']
    description = "Get sports scores and schedules (usage: sports [team/league])"
    category = "sports"
    cooldown_seconds = 3  # 3 second cooldown per user to prevent API abuse
    requires_internet = True  # Requires internet access for ESPN API
    
    # Documentation
    short_description = "Get sports scores and schedules"
    usage = "sports [team|league]"
    examples = ["sports", "sports seahawks", "sports nfl"]
    parameters = [
        {"name": "team", "description": "Team name (e.g., seahawks, mariners)"},
        {"name": "league", "description": "League code (nfl, mlb, nba, nhl, mls)"}
    ]
    
    # ESPN client
    espn_client: Optional[ESPNClient] = None
    
    # TheSportsDB client for leagues not supported by ESPN
    thesportsdb_client: Optional[TheSportsDBClient] = None
    

    def __init__(self, bot: "MeshCoreBot"):
        """Initialize the sports command with API clients and configuration.

        Args:
            bot: The MeshCoreBot instance that owns this command.
        """
        super().__init__(bot)
        self.url_timeout = 10  # seconds

        # Load enabled (standard enabled; sports_enabled legacy)
        self.sports_enabled = self.get_config_value('Sports_Command', 'enabled', fallback=None, value_type='bool')
        if self.sports_enabled is None:
            self.sports_enabled = self.get_config_value('Sports_Command', 'sports_enabled', fallback=True, value_type='bool')

        # Initialize API clients
        self.espn_client = ESPNClient(logger=self.logger, timeout=self.url_timeout)
        self.thesportsdb_client = TheSportsDBClient(logger=self.logger)

        # Load default teams from config
        self.default_teams = self.load_default_teams()
        # Note: allowed_channels is now loaded by BaseCommand from config
        # Keep sports_channels for backward compatibility (used in execute() for channel-specific team defaults)
        self.sports_channels = self.load_sports_channels()
        self.channel_overrides = self.load_channel_overrides()
        
    def load_default_teams(self) -> List[str]:
        """Load default teams from config"""
        teams_str = self.get_config_value('Sports_Command', 'teams', fallback='seahawks,mariners,sounders,kraken', value_type='str')
        return [team.strip().lower() for team in teams_str.split(',') if team.strip()]
    
    def load_sports_channels(self) -> List[str]:
        """Load sports channels from config"""
        channels_str = self.get_config_value('Sports_Command', 'channels', fallback='', value_type='str')
        return [channel.strip() for channel in channels_str.split(',') if channel.strip()]
    
    def load_channel_overrides(self) -> Dict[str, str]:
        """Load channel overrides from config"""
        overrides_str = self.get_config_value('Sports_Command', 'channel_override', fallback='', value_type='str')
        overrides = {}
        if overrides_str:
            for override in overrides_str.split(','):
                if '=' in override:
                    channel, team = override.strip().split('=', 1)
                    overrides[channel.strip()] = team.strip().lower()
        return overrides
    
    
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if this command matches the message content - sports must be first word"""
        if not self.keywords:
            return False
        
        # Strip exclamation mark if present (for command-style messages)
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Split into words and check if first word matches any keyword
        words = content.split()
        if not words:
            return False
        
        first_word = words[0].lower()
        
        for keyword in self.keywords:
            if first_word == keyword.lower():
                return True
        
        return False
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can execute with the given message"""
        if not self.sports_enabled:
            return False
        
        # Channel access and cooldown are now handled by BaseCommand.can_execute()
        # Call parent can_execute() which includes channel checking and cooldown
        return super().can_execute(message)
    
    def get_help_text(self) -> str:
        return self.translate('commands.sports.help')
    
    
    async def get_default_teams_scores(self) -> str:
        """Get scores for default teams, sorted by game time"""
        if not self.default_teams:
            return self.translate('commands.sports.no_default_teams')
        
        game_data = []
        for team in self.default_teams:
            try:
                team_info = TEAM_MAPPINGS.get(team)
                if team_info:
                    # Get all relevant games for this team (live, past within 8 days, upcoming within 6 weeks)
                    games = await self.fetch_team_games(team_info)
                    if games:
                        game_data.extend(games)
            except Exception as e:
                self.logger.warning(f"Error fetching score for {team}: {e}")
        
        if not game_data:
            return self.translate('commands.sports.no_games_default')
        
        # Sort by game time (earliest first)
        game_data.sort(key=lambda x: x['timestamp'])
        
        # Format responses with sport emojis
        responses = []
        for game in game_data:
            sport_emoji = SPORT_EMOJIS.get(game['sport'], '🏆')
            responses.append(f"{sport_emoji} {game['formatted']}")
        
        # Join responses with newlines and ensure under 130 characters
        result = "\n".join(responses)
        if len(result) > 130:
            # If still too long, truncate the last response
            while len(result) > 130 and len(responses) > 1:
                responses.pop()
                result = "\n".join(responses)
            if len(result) > 130:
                result = result[:127] + "..."
        
        return result
    
    def get_league_info(self, league_name: str) -> Optional[Dict[str, str]]:
        """Get league information for league queries"""
        return LEAGUE_MAPPINGS.get(league_name.lower())
    
    def get_city_teams(self, city_name: str) -> List[Dict[str, str]]:
        """Get all teams for a given city"""
        city_name_lower = city_name.lower()
        
        # Define city mappings to team names
        city_mappings = {
            'seattle': ['seahawks', 'mariners', 'sounders', 'kraken', 'reign', 'storm', 'torrent'],
            'chicago': ['bears', 'cubs', 'white sox', 'fire', 'sky', 'blackhawks'],
            'new york': ['giants', 'jets', 'yankees', 'mets', 'knicks', 'nyc fc', 'red bulls', 'liberty', 'rangers', 'islanders'],  # Add PWHL New York when team_id verified
            'ny': ['giants', 'jets', 'yankees', 'mets', 'knicks', 'nyc fc', 'red bulls', 'liberty', 'rangers', 'islanders'],  # Add PWHL New York when team_id verified
            'los angeles': ['rams', 'dodgers', 'lakers', 'la galaxy', 'lafc', 'sparks'],
            'la': ['rams', 'dodgers', 'lakers', 'la galaxy', 'lafc', 'sparks'],
            'miami': ['dolphins', 'marlins', 'heat', 'inter miami'],
            'boston': ['patriots', 'red sox', 'celtics', 'revolution', 'bruins'],  # Add PWHL Boston when team_id verified
            'philadelphia': ['eagles', 'phillies', '76ers', 'union'],
            'atlanta': ['falcons', 'braves', 'hawks', 'atlanta united', 'dream'],
            'houston': ['texans', 'astros', 'dynamo'],
            'dallas': ['cowboys', 'rangers', 'stars', 'fc dallas', 'wings'],
            'denver': ['broncos', 'rockies', 'rapids'],
            'detroit': ['lions', 'tigers', 'pistons'],
            'minnesota': ['vikings', 'twins', 'timberwolves', 'minnesota united', 'lynx', 'wild'],  # Add PWHL Minnesota when team_id verified
            'minneapolis': ['vikings', 'twins', 'timberwolves', 'minnesota united', 'lynx'],  # Add PWHL Minnesota when team_id verified
            'cleveland': ['browns', 'guardians', 'cavaliers'],
            'cincinnati': ['bengals', 'reds', 'fc cincinnati'],
            'pittsburgh': ['steelers', 'pirates', 'penguins'],
            'baltimore': ['ravens', 'orioles'],
            'tampa': ['buccaneers', 'rays', 'lightning'],
            'tampa bay': ['buccaneers', 'rays', 'lightning'],
            'kansas city': ['chiefs', 'royals', 'sporting kc'],
            'kc': ['chiefs', 'royals', 'sporting kc'],
            'washington': ['commanders', 'nationals', 'wizards', 'dc united', 'mystics'],
            'dc': ['commanders', 'nationals', 'wizards', 'dc united', 'mystics'],
            'phoenix': ['cardinals', 'diamondbacks', 'suns', 'mercury'],
            'indiana': ['colts', 'pacers', 'fever'],
            'indianapolis': ['colts', 'pacers', 'fever'],
            'las vegas': ['raiders', 'aces', 'golden knights'],
            'connecticut': ['sun'],
            'arizona': ['cardinals', 'diamondbacks', 'coyotes'],
            'golden state': ['warriors', 'valkyries'],
            'san francisco': ['49ers', 'giants', 'warriors', 'earthquakes', 'valkyries'],
            'sf': ['49ers', 'giants', 'warriors', 'earthquakes', 'valkyries'],
            'san diego': ['chargers', 'padres', 'san diego fc'],
            'sd': ['chargers', 'padres', 'san diego fc'],
            'ind': ['colts', 'pacers'],
            'nashville': ['titans', 'predators', 'nashville sc'],
            'tennessee': ['titans', 'predators', 'nashville sc'],
            'ten': ['titans', 'predators', 'nashville sc'],
            'lv': ['raiders', 'golden knights'],
            'louisville': ['racing'],
            'carolina': ['panthers', 'hornets'],
            'charlotte': ['panthers', 'hornets', 'charlotte fc'],
            'new orleans': ['saints', 'pelicans'],
            'no': ['saints', 'pelicans'],
            'green bay': ['packers'],
            'gb': ['packers'],
            'buffalo': ['bills', 'sabres'],
            'buf': ['bills', 'sabres'],
            'milwaukee': ['bucks', 'brewers'],
            'mil': ['bucks', 'brewers'],
            'portland': ['trail blazers', 'timbers'],
            'por': ['trail blazers', 'timbers'],
            'pdx': ['trail blazers', 'timbers'],
            'salt lake': ['jazz', 'real salt lake'],
            'utah': ['jazz', 'real salt lake'],
            'orlando': ['magic', 'orlando city'],
            'orl': ['magic', 'orlando city'],
            'toronto': ['raptors', 'blue jays', 'toronto fc', 'maple leafs'],  # Add PWHL Toronto when team_id verified
            'tor': ['raptors', 'blue jays', 'toronto fc', 'maple leafs'],  # Add PWHL Toronto when team_id verified
            'vancouver': ['canucks', 'whitecaps'],
            'van': ['canucks', 'whitecaps'],
            'montreal': ['canadiens', 'cf montreal'],  # Add PWHL Montreal when team_id verified
            'mtl': ['canadiens', 'cf montreal'],  # Add PWHL Montreal when team_id verified
            'calgary': ['flames'],
            'edmonton': ['oilers'],
            'winnipeg': ['jets'],
            'ottawa': ['senators'],  # Add PWHL Ottawa when team_id verified
            'columbus': ['blue jackets', 'crew'],
            'clb': ['blue jackets', 'crew'],
            'st louis': ['blues', 'st louis city'],
            'stl': ['blues', 'st louis city'],
            'colorado': ['avalanche', 'rockies', 'rapids'],
            'col': ['avalanche', 'rockies', 'rapids'],
            'san jose': ['sharks', 'earthquakes'],
            'sj': ['sharks', 'earthquakes'],
            'anaheim': ['ducks', 'angels'],
            'austin': ['austin fc'],
            'atx': ['austin fc'],
        }
        
        # Get team names for this city
        team_names = city_mappings.get(city_name_lower, [])
        if not team_names:
            return []
        
        # Get team info for each team name
        city_teams = []
        for team_name in team_names:
            team_info = TEAM_MAPPINGS.get(team_name)
            if team_info:
                city_teams.append(team_info)
        
        return city_teams
    
    async def get_city_scores(self, city_teams: List[Dict[str, str]], city_name: str) -> str:
        """Get scores for all teams in a city"""
        if not city_teams:
            return self.translate('commands.sports.no_teams_city', city=city_name)
        
        game_data = []
        for team_info in city_teams:
            try:
                # Get all relevant games for this team (live, past within 8 days, upcoming within 6 weeks)
                games = await self.fetch_team_games(team_info)
                if games:
                    game_data.extend(games)
            except Exception as e:
                self.logger.warning(f"Error fetching score for {team_info}: {e}")
        
        if not game_data:
            return self.translate('commands.sports.no_games_city', city=city_name)
        
        # Sort by game time (earliest first)
        game_data.sort(key=lambda x: x['timestamp'])
        
        # Format responses with sport emojis
        responses = []
        for game in game_data:
            sport_emoji = SPORT_EMOJIS.get(game['sport'], '🏆')
            responses.append(f"{sport_emoji} {game['formatted']}")
        
        # Join responses with newlines and ensure under 130 characters
        result = "\n".join(responses)
        if len(result) > 130:
            # If still too long, truncate the last response
            while len(result) > 130 and len(responses) > 1:
                responses.pop()
                result = "\n".join(responses)
            if len(result) > 130:
                result = result[:127] + "..."
        
        return result
    
    async def get_league_scores(self, league_info: Dict[str, str]) -> str:
        """Get upcoming games for a league"""
        # Check if this league uses TheSportsDB
        if league_info.get('api_source') == 'thesportsdb':
            return await self.get_league_scores_thesportsdb(league_info)
        
        # Default to ESPN API
        try:
            # Fetch and parse scoreboard via client
            game_data = await self.espn_client.fetch_scoreboard(
                league_info['sport'], league_info['league']
            )
            
            if not game_data:
                return self.translate('commands.sports.no_games_league', sport=league_info['sport'])
            
            # Sort by game time (earliest first)
            game_data.sort(key=lambda x: x['timestamp'])
            
            # Format responses with sport emojis
            responses = []
            for game in game_data[:5]:  # Limit to 5 games to keep under 130 chars
                sport_emoji = SPORT_EMOJIS.get(game['sport'], '🏆')
                responses.append(f"{sport_emoji} {game['formatted']}")
            
            # Join responses with newlines and ensure under 130 characters
            result = "\n".join(responses)
            if len(result) > 130:
                # If still too long, truncate the last response
                while len(result) > 130 and len(responses) > 1:
                    responses.pop()
                    result = "\n".join(responses)
                if len(result) > 130:
                    result = result[:127] + "..."
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error fetching league scores: {e}")
            return self.translate('commands.sports.error_fetching_league', sport=league_info['sport'])
    
    async def get_league_scores_thesportsdb(self, league_info: Dict[str, str]) -> str:
        """Get upcoming games for a league from TheSportsDB"""
        if not self.thesportsdb_client:
            self.logger.error("TheSportsDB client not initialized")
            return self.translate('commands.sports.error_fetching_league', sport=league_info.get('sport', 'unknown'))
        
        league_id = league_info.get('league_id')
        if not league_id:
            league_name = league_info.get('league', 'unknown').upper()
            return f"League ID not configured for {league_name}. Please query specific teams instead."
        
        try:
            # Delegate to client
            all_event_data = await self.thesportsdb_client.fetch_league_scores(
                league_info['sport'], league_info['league'], league_id
            )
            
            if not all_event_data:
                return self.translate('commands.sports.no_recent_scores', sport=league_info['sport'])
            
            # Sort by timestamp
            all_event_data.sort(key=lambda x: x['timestamp'])
            
            # Format responses with sport emojis
            sport_emoji = SPORT_EMOJIS.get(league_info['sport'], '🏆')
            responses = []
            current_length = 0
            max_length = 130
            
            for game in all_event_data:
                game_str = f"{sport_emoji} {game['formatted']}"
                if responses:
                    test_length = current_length + len("\n") + len(game_str)
                else:
                    test_length = len(game_str)
                
                if test_length <= max_length:
                    responses.append(game_str)
                    current_length = test_length
                else:
                    break
            
            return "\n".join(responses)
            
        except Exception as e:
            self.logger.error(f"Error getting league scores from TheSportsDB: {e}")
            return self.translate('commands.sports.error_fetching_league', sport=league_info['sport'])
    
    
    
    
    async def get_team_scores(self, team_name: str) -> str:
        """Get scores for a specific team or league"""
        # Check if this is a schedule query (team_name ends with " schedule")
        is_schedule_query = team_name.endswith(' schedule')
        if is_schedule_query:
            team_name_clean = team_name[:-9].strip()  # Remove " schedule"
            
            # First check if it's a league query
            league_info = self.get_league_info(team_name_clean)
            if league_info:
                # For league schedule queries, we can return upcoming games
                # (which is essentially the schedule)
                return await self.get_league_scores(league_info)
            
            # Otherwise, treat as team query
            team_info = TEAM_MAPPINGS.get(team_name_clean)
            if not team_info:
                return self.translate('commands.sports.team_not_found', team=team_name_clean)
            
            try:
                schedule_info = await self.fetch_team_schedule_formatted(team_info)
                if schedule_info:
                    return schedule_info
                else:
                    return self.translate('commands.sports.no_games_team', team=team_name_clean)
            except Exception as e:
                self.logger.error(f"Error fetching schedule for {team_name_clean}: {e}")
                return self.translate('commands.sports.error_fetching_team', team=team_name_clean)
        
        # Check if this is a league query
        league_info = self.get_league_info(team_name)
        if league_info:
            return await self.get_league_scores(league_info)
        
        # Check if this is a city search that should return multiple teams
        city_teams = self.get_city_teams(team_name)
        if city_teams:
            return await self.get_city_scores(city_teams, team_name)
        
        # Otherwise, treat as single team query
        team_info = TEAM_MAPPINGS.get(team_name)
        if not team_info:
            return self.translate('commands.sports.team_not_found', team=team_name)
        
        try:
            score_info = await self.fetch_team_score(team_info)
            if score_info:
                # fetch_team_score already includes emojis, so return as-is
                return score_info
            else:
                return self.translate('commands.sports.no_games_team', team=team_name)
        except Exception as e:
            self.logger.error(f"Error fetching score for {team_name}: {e}")
            return self.translate('commands.sports.error_fetching_team', team=team_name)
    
    async def fetch_team_score(self, team_info: Dict[str, str]) -> Optional[str]:
        """Fetch score information for a team - returns current/next game plus past results"""
        games = await self.fetch_team_games(team_info)
        if not games:
            return None
        
        # Format games to fit within message limit (130 characters)
        # Use 125 as a buffer to avoid cutting off mid-game
        sport_emoji = SPORT_EMOJIS.get(team_info['sport'], '🏆')
        formatted_games = []
        current_length = 0
        max_length = 125  # Leave buffer to avoid cutoff
        
        for game in games:
            # Ensure game['formatted'] doesn't already have an emoji
            game_formatted = game['formatted'].strip()
            # Remove emoji if it's at the start (some games might have it)
            if game_formatted and game_formatted[0] in SPORT_EMOJIS.values():
                game_formatted = game_formatted[1:].strip()
            
            game_str = f"{sport_emoji} {game_formatted}"
            # Check if adding this game would exceed limit
            if formatted_games:
                # Account for newline separator
                test_length = current_length + len("\n") + len(game_str)
            else:
                test_length = len(game_str)
            
            if test_length <= max_length:
                formatted_games.append(game_str)
                current_length = test_length
            else:
                # Can't fit more games - stop before exceeding limit
                break
        
        if not formatted_games:
            # If even the first game doesn't fit, return it anyway (truncated)
            game_formatted = games[0]['formatted'].strip()
            if game_formatted and game_formatted[0] in SPORT_EMOJIS.values():
                game_formatted = game_formatted[1:].strip()
            return f"{sport_emoji} {game_formatted[:120]}"
        
        return "\n".join(formatted_games)
    
    async def fetch_team_games(self, team_info: Dict[str, str]) -> List[Dict]:
        """Fetch multiple games for a team: current/next game plus past results
        
        Uses the team schedule endpoint which returns both past and upcoming games
        in a single API call. Returns games sorted by relevance:
        - Live games first
        - Last completed game (if within last 8 days)
        - Next scheduled game (if known)
        
        Supports both ESPN API and TheSportsDB API based on team_info['api_source'].
        """
        # Check if this team uses TheSportsDB
        if team_info.get('api_source') == 'thesportsdb':
            return await self.fetch_team_games_thesportsdb(team_info)
        
        # Default to ESPN API
        try:
            # Use team schedule endpoint via client
            # The client already parses the events
            all_games = await self.espn_client.fetch_team_schedule(
                team_info['sport'], team_info['league'], team_info['team_id']
            )
            
            if not all_games:
                return []
            
            # Track event IDs for live games to fetch real-time scores
            live_event_ids = []
            for i, game_data in enumerate(all_games):
                if game_data['timestamp'] < 0:  # Negative timestamp indicates live game
                    event_id = game_data.get('id')
                    if event_id:
                        live_event_ids.append((event_id, i))
            
            # Fetch live event data for live games to get real-time scores
            for event_id, game_index in live_event_ids:
                try:
                    live_event_data = await self.espn_client.fetch_live_event_data(
                        event_id, team_info['sport'], team_info['league']
                    )
                    if live_event_data:
                        # Update the game data with live scores
                        updated_game = self.espn_client.parse_game_event_with_timestamp(
                            live_event_data, team_info['team_id'], team_info['sport'], team_info['league']
                        )
                        if updated_game:
                            all_games[game_index] = updated_game
                except Exception as e:
                    self.logger.warning(f"Error fetching live data for event {event_id}: {e}")
            
            # Sort by timestamp (negative for live games, then by actual timestamp)
            # This prioritizes: live games > upcoming games > recent past games
            all_games.sort(key=lambda x: x['timestamp'])
            
            # Get current time for comparison
            now = datetime.now(timezone.utc).timestamp()
            # 8 days in seconds
            eight_days_ago = now - (8 * 24 * 60 * 60)
            # 6 weeks in seconds (6 * 7 * 24 * 60 * 60)
            six_weeks_from_now = now + (6 * 7 * 24 * 60 * 60)
            
            # Separate into categories
            live_games = [g for g in all_games if g['timestamp'] < 0]  # Negative timestamps = live
            upcoming_games = []
            past_games = []
            
            # Categorize games with positive timestamps
            for game in all_games:
                if game['timestamp'] < 0:
                    continue  # Already in live_games
                
                game_event_ts = game.get('event_timestamp')
                effective_ts = game_event_ts if game_event_ts is not None else game['timestamp']
                
                if game['timestamp'] >= 9999999990 and game_event_ts is None:
                    # No real timestamp available, treat as past
                    past_games.append((effective_ts, game))
                elif effective_ts is None:
                    past_games.append((effective_ts, game))
                elif effective_ts > now:
                    # Future game - only include if within next 6 weeks
                    if effective_ts is not None and effective_ts <= six_weeks_from_now:
                        upcoming_games.append((effective_ts, game))
                else:
                    # Past game - only include if within last 8 days
                    if effective_ts is not None and effective_ts >= eight_days_ago:
                        past_games.append((effective_ts, game))
            
            # Sort upcoming games by soonest first, past games by most recent first
            upcoming_games.sort(key=lambda x: x[0] if x[0] is not None else float('inf'))
            past_games.sort(key=lambda x: x[0] if x[0] is not None else -float('inf'), reverse=True)
            
            # Build result with new priority:
            # 1. Live games (if any)
            # 2. Last completed game (if within last 8 days)
            # 3. Next scheduled game (if known and within 6 weeks)
            result = []
            
            # Add live games (if any)
            if live_games:
                result.extend(live_games)
            
            # Add last completed game (if within last 8 days)
            if past_games:
                result.append(past_games[0][1])  # Most recent past game
            
            # Add next scheduled game (if known and within 6 weeks)
            if upcoming_games:
                result.append(upcoming_games[0][1])
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error fetching team games: {e}")
            return []
    
    async def fetch_team_games_thesportsdb(self, team_info: Dict[str, str]) -> List[Dict]:
        """Fetch team games from TheSportsDB API via client"""
        if not self.thesportsdb_client:
            self.logger.error("TheSportsDB client not initialized")
            return []
        
        return await self.thesportsdb_client.fetch_team_games(
            team_info['sport'], team_info['league'], team_info['team_id']
        )
    
    async def fetch_team_game_data(self, team_info: Dict[str, str]) -> Optional[Dict]:
        """Fetch structured game data for a team with timestamp for sorting
        
        Uses the team schedule endpoint which returns both past and upcoming games
        in a single API call, eliminating the need for multiple scoreboard requests.
        Returns only the most relevant game (for backward compatibility).
        """
        games = await self.fetch_team_games(team_info)
        return games[0] if games else None
    
    async def fetch_team_schedule(self, team_info: Dict[str, str]) -> List[Dict]:
        """Fetch upcoming scheduled games for a team
        
        Returns as many upcoming games as available from the schedule endpoint.
        Used for 'sports <teamname> schedule' command.
        
        Supports both ESPN API and TheSportsDB API based on team_info['api_source'].
        """
        # Check if this team uses TheSportsDB
        if team_info.get('api_source') == 'thesportsdb':
            return await self.fetch_team_schedule_thesportsdb(team_info)
        
        # Default to ESPN API
        try:
            # The client already parses these events
            all_games = await self.espn_client.fetch_team_schedule(
                team_info['sport'], team_info['league'], team_info['team_id']
            )
            
            if not all_games:
                return []
            
            # Get current time for comparison
            now = datetime.now(timezone.utc).timestamp()
            # 1 hour buffer for ongoing games
            one_hour_ago = now - 3600
            
            parsed_games = []
            # We already have parsed games, but we need to filter/re-format them for schedule view
            for game_data in all_games:
                # Check if game is in the future or started very recently
                ts = game_data.get('event_timestamp') or game_data['timestamp']
                
                # If timestamp is negative (live), it's definitely something we could show
                # Otherwise check if it's in the future or within the last hour
                if game_data['timestamp'] < 0 or ts >= one_hour_ago:
                    parsed_games.append(game_data)
            
            # Sort by soonest first
            parsed_games.sort(key=lambda x: x.get('event_timestamp') or x['timestamp'])
            return parsed_games
            
        except Exception as e:
            self.logger.error(f"Error fetching team schedule: {e}")
            return []
    
    async def fetch_team_schedule_thesportsdb(self, team_info: Dict[str, str]) -> List[Dict]:
        """Fetch upcoming scheduled games for a team from TheSportsDB via client"""
        if not self.thesportsdb_client:
            return []
        
        return await self.thesportsdb_client.fetch_team_schedule(
            team_info['sport'], team_info['league'], team_info['team_id']
        )
    
    async def fetch_team_schedule_formatted(self, team_info: Dict[str, str]) -> Optional[str]:
        """Fetch and format upcoming scheduled games for a team
        
        Returns formatted schedule with as many games as fit in 130 characters.
        """
        games = await self.fetch_team_schedule(team_info)
        if not games:
            return None
        
        # Format games to fit within message limit (130 characters)
        # Use 125 as a buffer to avoid cutting off mid-game
        sport_emoji = SPORT_EMOJIS.get(team_info['sport'], '🏆')
        formatted_games = []
        current_length = 0
        max_length = 125  # Leave buffer to avoid cutoff
        
        for game in games:
            # Ensure game['formatted'] doesn't already have an emoji
            game_formatted = game['formatted'].strip()
            # Remove emoji if it's at the start (some games might have it)
            if game_formatted and game_formatted[0] in SPORT_EMOJIS.values():
                game_formatted = game_formatted[1:].strip()
            
            game_str = f"{sport_emoji} {game_formatted}"
            # Check if adding this game would exceed limit
            if formatted_games:
                # Account for newline separator
                test_length = current_length + len("\n") + len(game_str)
            else:
                test_length = len(game_str)
            
            if test_length <= max_length:
                formatted_games.append(game_str)
                current_length = test_length
            else:
                # Can't fit more games - stop before exceeding limit
                break
        
        if not formatted_games:
            # If even the first game doesn't fit, return it anyway (truncated)
            game_formatted = games[0]['formatted'].strip()
            if game_formatted and game_formatted[0] in SPORT_EMOJIS.values():
                game_formatted = game_formatted[1:].strip()
            return f"{sport_emoji} {game_formatted[:120]}"
        
        return "\n".join(formatted_games)

    async def execute(self, message: MeshMessage) -> bool:
        """Main entry point for command execution"""
        try:
            # Record execution for this user (handles cooldown)
            self.record_execution(message.sender_id)
            
            content = message.content.strip()
            if content.startswith('!'):
                content = content[1:].strip()
            
            # Parse command: !sports [query]
            parts = content.split(' ', 1)
            if len(parts) < 2:
                # Check if this channel has an override team
                if not message.is_dm and message.channel in self.channel_overrides:
                    override_team = self.channel_overrides[message.channel]
                    response = await self.get_team_scores(override_team)
                else:
                    response = await self.get_default_teams_scores()
                return await self.send_response(message, response)
            
            query = parts[1].strip()
            
            # Check if it's a league query (e.g., "nfl", "mlb", etc.)
            league_info = self.get_league_info(query)
            if league_info:
                scores = await self.get_league_scores(league_info)
                return await self.send_response(message, scores)
            
            # Check if it's a city query (e.g., "seattle")
            city_teams = self.get_city_teams(query)
            if city_teams:
                scores = await self.get_city_scores(city_teams, query)
                return await self.send_response(message, scores)
            
            # Treat as team score query
            scores = await self.get_team_scores(query)
            if not scores:
                return await self.send_response(message, f"No games found for {query}.")
                
            return await self.send_response(message, scores)
            
        except Exception as e:
            self.logger.error(f"Error in sports execute: {e}")
            return await self.send_response(message, "Error processing sports command.")
    
