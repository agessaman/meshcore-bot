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

import re
import json
import requests
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from .base_command import BaseCommand
from ..models import MeshMessage


class TheSportsDBClient:
    """Client for TheSportsDB API with rate limiting
    
    Free tier: 30 requests per minute (1 request every 2 seconds)
    """
    
    BASE_URL = "https://www.thesportsdb.com/api/v1/json"
    FREE_API_KEY = "123"  # Free public API key
    
    def __init__(self, logger=None):
        self.logger = logger
        self.last_request_time = 0
        self.min_request_interval = 2.1  # Slightly more than 2 seconds for safety
    
    def _rate_limit(self):
        """Enforce rate limiting"""
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.min_request_interval:
            sleep_time = self.min_request_interval - time_since_last
            time.sleep(sleep_time)
        self.last_request_time = time.time()
    
    def search_team(self, team_name: str) -> Optional[Dict]:
        """Search for a team by name"""
        self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/searchteams.php"
        params = {'t': team_name}
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            teams = data.get('teams', [])
            if teams:
                return teams[0]  # Return first match
            return None
        except Exception as e:
            if self.logger:
                self.logger.error(f"TheSportsDB search_team error: {e}")
            return None
    
    def get_team_events_last(self, team_id: str, limit: int = 5) -> List[Dict]:
        """Get last N events for a team"""
        self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventslast.php"
        params = {'id': team_id}
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            events = data.get('results', [])
            return events[:limit]
        except Exception as e:
            if self.logger:
                self.logger.error(f"TheSportsDB get_team_events_last error: {e}")
            return []
    
    def get_team_events_next(self, team_id: str, limit: int = 5) -> List[Dict]:
        """Get next N events for a team"""
        self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventsnext.php"
        params = {'id': team_id}
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            events = data.get('events', [])
            return events[:limit]
        except Exception as e:
            if self.logger:
                self.logger.error(f"TheSportsDB get_team_events_next error: {e}")
            return []
    
    def get_league_teams(self, league_id: str) -> List[Dict]:
        """Get all teams in a league"""
        self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/lookup_all_teams.php"
        params = {'id': league_id}
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            teams = data.get('teams', [])
            return teams
        except Exception as e:
            if self.logger:
                self.logger.error(f"TheSportsDB get_league_teams error: {e}")
            return []
    
    def get_league_events_next(self, league_id: str, limit: int = 10) -> List[Dict]:
        """Get next N events for a league"""
        self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventsnextleague.php"
        params = {'id': league_id}
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            events = data.get('events', [])
            return events[:limit]
        except Exception as e:
            if self.logger:
                self.logger.error(f"TheSportsDB get_league_events_next error: {e}")
            return []
    
    def get_league_events_past(self, league_id: str, limit: int = 10) -> List[Dict]:
        """Get past N events for a league"""
        self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventspastleague.php"
        params = {'id': league_id}
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            events = data.get('results', [])  # Note: past events use 'results' key
            return events[:limit]
        except Exception as e:
            if self.logger:
                self.logger.error(f"TheSportsDB get_league_events_past error: {e}")
            return []
    
    def get_events_by_day(self, date_str: str, league_id: str = None) -> List[Dict]:
        """Get events for a specific day
        
        Args:
            date_str: Date in YYYY-MM-DD format
            league_id: Optional league ID to filter by
        """
        self._rate_limit()
        url = f"{self.BASE_URL}/{self.FREE_API_KEY}/eventsday.php"
        params = {'d': date_str}
        if league_id:
            params['l'] = league_id
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            events = data.get('events', [])
            # Handle case where API returns None instead of empty list
            if events is None:
                return []
            return events if isinstance(events, list) else []
        except Exception as e:
            if self.logger:
                self.logger.error(f"TheSportsDB get_events_by_day error: {e}")
            return []


class SportsCommand(BaseCommand):
    """Handles sports commands with ESPN API integration"""
    
    # Plugin metadata
    name = "sports"
    keywords = ['sports', 'score', 'scores']
    description = "Get sports scores and schedules (usage: sports [team/league])"
    category = "sports"
    cooldown_seconds = 3  # 3 second cooldown per user to prevent API abuse
    requires_internet = True  # Requires internet access for ESPN API
    
    # ESPN API base URL
    ESPN_BASE_URL = "http://site.api.espn.com/apis/site/v2/sports"
    
    # TheSportsDB client for leagues not supported by ESPN
    thesportsdb_client: Optional[TheSportsDBClient] = None
    
    # Sport emojis for easy identification
    SPORT_EMOJIS = {
        'football': '🏈',
        'baseball': '⚾',
        'basketball': '🏀',
        'hockey': '🏒',
        'soccer': '⚽'
    }
    
    # Custom team abbreviations to distinguish between leagues
    # Only use -W suffixes for women's leagues
    WOMENS_TEAM_ABBREVIATIONS = {
        # NWSL teams - use custom abbreviations to distinguish from MLS
        '21422': 'LA-W',   # Angel City FC (Women's)
        '22187': 'BAY-W',  # Bay FC (Women's)
        '15360': 'CHI-W',  # Chicago Stars FC (Women's)
        '15364': 'GFC-W',  # Gotham FC (Women's)
        '17346': 'HOU-W',  # Houston Dash (Women's)
        '20907': 'KC-W',   # Kansas City Current (Women's)
        '15366': 'NC-W',   # North Carolina Courage (Women's)
        '18206': 'ORL-W',  # Orlando Pride (Women's)
        '15362': 'POR-W',  # Portland Thorns FC (Women's)
        '20905': 'LOU-W',  # Racing Louisville FC (Women's)
        '21423': 'SD-W',   # San Diego Wave FC (Women's)
        '15363': 'SEA-W',  # Seattle Reign FC (Women's)
        '19141': 'UTA-W',  # Utah Royals (Women's)
        '15365': 'WAS-W',  # Washington Spirit (Women's)
        # WNBA teams - use custom abbreviations to distinguish from NBA
        '14': 'SEA-W',     # Seattle Storm (Women's)
        '9': 'NY-W',       # New York Liberty (Women's)
        '6': 'LA-W',       # Los Angeles Sparks (Women's)
        '19': 'CHI-W',     # Chicago Sky (Women's)
        '20': 'ATL-W',     # Atlanta Dream (Women's)
        '18': 'CON-W',     # Connecticut Sun (Women's)
        '3': 'DAL-W',      # Dallas Wings (Women's)
        '129689': 'GS-W',  # Golden State Valkyries (Women's)
        '5': 'IND-W',      # Indiana Fever (Women's)
        '17': 'LV-W',      # Las Vegas Aces (Women's)
        '8': 'MIN-W',      # Minnesota Lynx (Women's)
        '11': 'PHX-W',     # Phoenix Mercury (Women's)
        '16': 'WSH-W',     # Washington Mystics (Women's)
        # PWHL teams - use custom abbreviations to distinguish from NHL
        # NOTE: Team IDs need to be verified using test_scripts/find_espn_team_id.py hockey pwhl <team_name>
        # Once verified, uncomment and replace 'VERIFY_TEAM_ID' with the actual ESPN team ID
        # Format: 'ACTUAL_TEAM_ID': 'ABBREV-W',  # Team Name (Women's)
        # Example: '123456': 'BOS-W',  # Boston (Women's)
        # 'VERIFY_BOS': 'BOS-W',  # Boston (Women's) - verify team_id
        # 'VERIFY_MIN': 'MIN-W',  # Minnesota (Women's) - verify team_id
        # 'VERIFY_MTL': 'MTL-W',  # Montreal (Women's) - verify team_id
        # 'VERIFY_NY': 'NY-W',    # New York (Women's) - verify team_id
        # 'VERIFY_OTT': 'OTT-W',  # Ottawa (Women's) - verify team_id
        # 'VERIFY_TOR': 'TOR-W',  # Toronto (Women's) - verify team_id
        # 'VERIFY_SEA': 'SEA-W',  # Seattle Torrent (Women's) - verify team_id
    }
    
    # Team mappings for common searches
    # NOTE: Team IDs can change over time (see module docstring).
    # Use test_scripts/find_espn_team_id.py to verify/update team IDs.
    TEAM_MAPPINGS = {
        # NFL Teams
        'seahawks': {'sport': 'football', 'league': 'nfl', 'team_id': '26'},
        'hawks': {'sport': 'football', 'league': 'nfl', 'team_id': '26'},
        '49ers': {'sport': 'football', 'league': 'nfl', 'team_id': '25'},
        'niners': {'sport': 'football', 'league': 'nfl', 'team_id': '25'},
        'sf': {'sport': 'football', 'league': 'nfl', 'team_id': '25'},
        'bears': {'sport': 'football', 'league': 'nfl', 'team_id': '3'},
        'chicago': {'sport': 'football', 'league': 'nfl', 'team_id': '3'},
        'chi': {'sport': 'football', 'league': 'nfl', 'team_id': '3'},
        'bengals': {'sport': 'football', 'league': 'nfl', 'team_id': '4'},
        'cincinnati': {'sport': 'football', 'league': 'nfl', 'team_id': '4'},
        'cin': {'sport': 'football', 'league': 'nfl', 'team_id': '4'},
        'bills': {'sport': 'football', 'league': 'nfl', 'team_id': '2'},
        'buffalo': {'sport': 'football', 'league': 'nfl', 'team_id': '2'},
        'buf': {'sport': 'football', 'league': 'nfl', 'team_id': '2'},
        'broncos': {'sport': 'football', 'league': 'nfl', 'team_id': '7'},
        'denver': {'sport': 'football', 'league': 'nfl', 'team_id': '7'},
        'den': {'sport': 'football', 'league': 'nfl', 'team_id': '7'},
        'browns': {'sport': 'football', 'league': 'nfl', 'team_id': '5'},
        'cleveland': {'sport': 'football', 'league': 'nfl', 'team_id': '5'},
        'cle': {'sport': 'football', 'league': 'nfl', 'team_id': '5'},
        'buccaneers': {'sport': 'football', 'league': 'nfl', 'team_id': '27'},
        'bucs': {'sport': 'football', 'league': 'nfl', 'team_id': '27'},
        'tampa bay': {'sport': 'football', 'league': 'nfl', 'team_id': '27'},
        'tb': {'sport': 'football', 'league': 'nfl', 'team_id': '27'},
        'cardinals': {'sport': 'football', 'league': 'nfl', 'team_id': '22'},
        'arizona': {'sport': 'football', 'league': 'nfl', 'team_id': '22'},
        'ari': {'sport': 'football', 'league': 'nfl', 'team_id': '22'},
        'chargers': {'sport': 'football', 'league': 'nfl', 'team_id': '24'},
        'lac': {'sport': 'football', 'league': 'nfl', 'team_id': '24'},
        'la chargers': {'sport': 'football', 'league': 'nfl', 'team_id': '24'},
        'los angeles chargers': {'sport': 'football', 'league': 'nfl', 'team_id': '24'},
        'chiefs': {'sport': 'football', 'league': 'nfl', 'team_id': '12'},
        'kansas city': {'sport': 'football', 'league': 'nfl', 'team_id': '12'},
        'kc': {'sport': 'football', 'league': 'nfl', 'team_id': '12'},
        'colts': {'sport': 'football', 'league': 'nfl', 'team_id': '11'},
        'indianapolis': {'sport': 'football', 'league': 'nfl', 'team_id': '11'},
        'ind': {'sport': 'football', 'league': 'nfl', 'team_id': '11'},
        'commanders': {'sport': 'football', 'league': 'nfl', 'team_id': '28'},
        'washington': {'sport': 'football', 'league': 'nfl', 'team_id': '28'},
        'wsh': {'sport': 'football', 'league': 'nfl', 'team_id': '28'},
        'cowboys': {'sport': 'football', 'league': 'nfl', 'team_id': '6'},
        'dallas': {'sport': 'football', 'league': 'nfl', 'team_id': '6'},
        'dal': {'sport': 'football', 'league': 'nfl', 'team_id': '6'},
        'dolphins': {'sport': 'football', 'league': 'nfl', 'team_id': '15'},
        'miami': {'sport': 'football', 'league': 'nfl', 'team_id': '15'},
        'mia': {'sport': 'football', 'league': 'nfl', 'team_id': '15'},
        'eagles': {'sport': 'football', 'league': 'nfl', 'team_id': '21'},
        'philadelphia': {'sport': 'football', 'league': 'nfl', 'team_id': '21'},
        'phi': {'sport': 'football', 'league': 'nfl', 'team_id': '21'},
        'falcons': {'sport': 'football', 'league': 'nfl', 'team_id': '1'},
        'atlanta': {'sport': 'football', 'league': 'nfl', 'team_id': '1'},
        'atl': {'sport': 'football', 'league': 'nfl', 'team_id': '1'},
        'giants': {'sport': 'football', 'league': 'nfl', 'team_id': '19'},
        'nyg': {'sport': 'football', 'league': 'nfl', 'team_id': '19'},
        'jaguars': {'sport': 'football', 'league': 'nfl', 'team_id': '30'},
        'jax': {'sport': 'football', 'league': 'nfl', 'team_id': '30'},
        'jacksonville': {'sport': 'football', 'league': 'nfl', 'team_id': '30'},
        'jets': {'sport': 'football', 'league': 'nfl', 'team_id': '20'},
        'nyj': {'sport': 'football', 'league': 'nfl', 'team_id': '20'},
        'lions': {'sport': 'football', 'league': 'nfl', 'team_id': '8'},
        'detroit': {'sport': 'football', 'league': 'nfl', 'team_id': '8'},
        'det': {'sport': 'football', 'league': 'nfl', 'team_id': '8'},
        'packers': {'sport': 'football', 'league': 'nfl', 'team_id': '9'},
        'green bay': {'sport': 'football', 'league': 'nfl', 'team_id': '9'},
        'gb': {'sport': 'football', 'league': 'nfl', 'team_id': '9'},
        'panthers': {'sport': 'football', 'league': 'nfl', 'team_id': '29'},
        'carolina': {'sport': 'football', 'league': 'nfl', 'team_id': '29'},
        'car': {'sport': 'football', 'league': 'nfl', 'team_id': '29'},
        'patriots': {'sport': 'football', 'league': 'nfl', 'team_id': '17'},
        'new england': {'sport': 'football', 'league': 'nfl', 'team_id': '17'},
        'ne': {'sport': 'football', 'league': 'nfl', 'team_id': '17'},
        'raiders': {'sport': 'football', 'league': 'nfl', 'team_id': '13'},
        'las vegas': {'sport': 'football', 'league': 'nfl', 'team_id': '13'},
        'lv': {'sport': 'football', 'league': 'nfl', 'team_id': '13'},
        'rams': {'sport': 'football', 'league': 'nfl', 'team_id': '14'},
        'lar': {'sport': 'football', 'league': 'nfl', 'team_id': '14'},
        'la rams': {'sport': 'football', 'league': 'nfl', 'team_id': '14'},
        'los angeles rams': {'sport': 'football', 'league': 'nfl', 'team_id': '14'},
        'ravens': {'sport': 'football', 'league': 'nfl', 'team_id': '33'},
        'baltimore': {'sport': 'football', 'league': 'nfl', 'team_id': '33'},
        'bal': {'sport': 'football', 'league': 'nfl', 'team_id': '33'},
        'saints': {'sport': 'football', 'league': 'nfl', 'team_id': '18'},
        'new orleans': {'sport': 'football', 'league': 'nfl', 'team_id': '18'},
        'no': {'sport': 'football', 'league': 'nfl', 'team_id': '18'},
        'steelers': {'sport': 'football', 'league': 'nfl', 'team_id': '23'},
        'pittsburgh': {'sport': 'football', 'league': 'nfl', 'team_id': '23'},
        'pit': {'sport': 'football', 'league': 'nfl', 'team_id': '23'},
        'texans': {'sport': 'football', 'league': 'nfl', 'team_id': '34'},
        'houston': {'sport': 'football', 'league': 'nfl', 'team_id': '34'},
        'hou': {'sport': 'football', 'league': 'nfl', 'team_id': '34'},
        'titans': {'sport': 'football', 'league': 'nfl', 'team_id': '10'},
        'tennessee': {'sport': 'football', 'league': 'nfl', 'team_id': '10'},
        'ten': {'sport': 'football', 'league': 'nfl', 'team_id': '10'},
        'vikings': {'sport': 'football', 'league': 'nfl', 'team_id': '16'},
        'minnesota': {'sport': 'football', 'league': 'nfl', 'team_id': '16'},
        'min': {'sport': 'football', 'league': 'nfl', 'team_id': '16'},
        
        # CFL Teams (Canadian Football League)
        'bc lions': {'sport': 'football', 'league': 'cfl', 'team_id': '79'},
        'lions': {'sport': 'football', 'league': 'cfl', 'team_id': '79'},
        'bcl': {'sport': 'football', 'league': 'cfl', 'team_id': '79'},
        'calgary stampeders': {'sport': 'football', 'league': 'cfl', 'team_id': '80'},
        'stampeders': {'sport': 'football', 'league': 'cfl', 'team_id': '80'},
        'csp': {'sport': 'football', 'league': 'cfl', 'team_id': '80'},
        'edmonton elks': {'sport': 'football', 'league': 'cfl', 'team_id': '81'},
        'elks': {'sport': 'football', 'league': 'cfl', 'team_id': '81'},
        'ees': {'sport': 'football', 'league': 'cfl', 'team_id': '81'},
        'hamilton tiger-cats': {'sport': 'football', 'league': 'cfl', 'team_id': '82'},
        'tiger-cats': {'sport': 'football', 'league': 'cfl', 'team_id': '82'},
        'tigercats': {'sport': 'football', 'league': 'cfl', 'team_id': '82'},
        'htc': {'sport': 'football', 'league': 'cfl', 'team_id': '82'},
        'montreal alouettes': {'sport': 'football', 'league': 'cfl', 'team_id': '83'},
        'alouettes': {'sport': 'football', 'league': 'cfl', 'team_id': '83'},
        'mta': {'sport': 'football', 'league': 'cfl', 'team_id': '83'},
        'ottawa redblacks': {'sport': 'football', 'league': 'cfl', 'team_id': '87'},
        'redblacks': {'sport': 'football', 'league': 'cfl', 'team_id': '87'},
        'red blacks': {'sport': 'football', 'league': 'cfl', 'team_id': '87'},
        'orb': {'sport': 'football', 'league': 'cfl', 'team_id': '87'},
        'saskatchewan roughriders': {'sport': 'football', 'league': 'cfl', 'team_id': '84'},
        'roughriders': {'sport': 'football', 'league': 'cfl', 'team_id': '84'},
        'riders': {'sport': 'football', 'league': 'cfl', 'team_id': '84'},
        'srr': {'sport': 'football', 'league': 'cfl', 'team_id': '84'},
        'toronto argonauts': {'sport': 'football', 'league': 'cfl', 'team_id': '85'},
        'argonauts': {'sport': 'football', 'league': 'cfl', 'team_id': '85'},
        'argos': {'sport': 'football', 'league': 'cfl', 'team_id': '85'},
        'tat': {'sport': 'football', 'league': 'cfl', 'team_id': '85'},
        'winnipeg blue bombers': {'sport': 'football', 'league': 'cfl', 'team_id': '86'},
        'blue bombers': {'sport': 'football', 'league': 'cfl', 'team_id': '86'},
        'bombers': {'sport': 'football', 'league': 'cfl', 'team_id': '86'},
        'wbb': {'sport': 'football', 'league': 'cfl', 'team_id': '86'},
        
        # MLB Teams
        'mariners': {'sport': 'baseball', 'league': 'mlb', 'team_id': '12'},
        'seattle': {'sport': 'baseball', 'league': 'mlb', 'team_id': '12'},
        'sea': {'sport': 'baseball', 'league': 'mlb', 'team_id': '12'},
        'angels': {'sport': 'baseball', 'league': 'mlb', 'team_id': '3'},
        'laa': {'sport': 'baseball', 'league': 'mlb', 'team_id': '3'},
        'astros': {'sport': 'baseball', 'league': 'mlb', 'team_id': '18'},
        'houston': {'sport': 'baseball', 'league': 'mlb', 'team_id': '18'},
        'hou': {'sport': 'baseball', 'league': 'mlb', 'team_id': '18'},
        'athletics': {'sport': 'baseball', 'league': 'mlb', 'team_id': '11'},
        'a\'s': {'sport': 'baseball', 'league': 'mlb', 'team_id': '11'},
        'oakland': {'sport': 'baseball', 'league': 'mlb', 'team_id': '11'},
        'oak': {'sport': 'baseball', 'league': 'mlb', 'team_id': '11'},
        'blue jays': {'sport': 'baseball', 'league': 'mlb', 'team_id': '14'},
        'toronto': {'sport': 'baseball', 'league': 'mlb', 'team_id': '14'},
        'tor': {'sport': 'baseball', 'league': 'mlb', 'team_id': '14'},
        'braves': {'sport': 'baseball', 'league': 'mlb', 'team_id': '15'},
        'atlanta': {'sport': 'baseball', 'league': 'mlb', 'team_id': '15'},
        'atl': {'sport': 'baseball', 'league': 'mlb', 'team_id': '15'},
        'brewers': {'sport': 'baseball', 'league': 'mlb', 'team_id': '8'},
        'milwaukee': {'sport': 'baseball', 'league': 'mlb', 'team_id': '8'},
        'mil': {'sport': 'baseball', 'league': 'mlb', 'team_id': '8'},
        'cardinals': {'sport': 'baseball', 'league': 'mlb', 'team_id': '24'},
        'st louis': {'sport': 'baseball', 'league': 'mlb', 'team_id': '24'},
        'stl': {'sport': 'baseball', 'league': 'mlb', 'team_id': '24'},
        'cubs': {'sport': 'baseball', 'league': 'mlb', 'team_id': '16'},
        'chicago': {'sport': 'baseball', 'league': 'mlb', 'team_id': '16'},
        'chc': {'sport': 'baseball', 'league': 'mlb', 'team_id': '16'},
        'diamondbacks': {'sport': 'baseball', 'league': 'mlb', 'team_id': '29'},
        'arizona': {'sport': 'baseball', 'league': 'mlb', 'team_id': '29'},
        'ari': {'sport': 'baseball', 'league': 'mlb', 'team_id': '29'},
        'dodgers': {'sport': 'baseball', 'league': 'mlb', 'team_id': '19'},
        'lad': {'sport': 'baseball', 'league': 'mlb', 'team_id': '19'},
        'giants': {'sport': 'baseball', 'league': 'mlb', 'team_id': '26'},
        'san francisco': {'sport': 'baseball', 'league': 'mlb', 'team_id': '26'},
        'sf': {'sport': 'baseball', 'league': 'mlb', 'team_id': '26'},
        'guardians': {'sport': 'baseball', 'league': 'mlb', 'team_id': '5'},
        'cleveland': {'sport': 'baseball', 'league': 'mlb', 'team_id': '5'},
        'cle': {'sport': 'baseball', 'league': 'mlb', 'team_id': '5'},
        'marlins': {'sport': 'baseball', 'league': 'mlb', 'team_id': '28'},
        'miami': {'sport': 'baseball', 'league': 'mlb', 'team_id': '28'},
        'mia': {'sport': 'baseball', 'league': 'mlb', 'team_id': '28'},
        'mets': {'sport': 'baseball', 'league': 'mlb', 'team_id': '21'},
        'nym': {'sport': 'baseball', 'league': 'mlb', 'team_id': '21'},
        'nationals': {'sport': 'baseball', 'league': 'mlb', 'team_id': '20'},
        'washington': {'sport': 'baseball', 'league': 'mlb', 'team_id': '20'},
        'was': {'sport': 'baseball', 'league': 'mlb', 'team_id': '20'},
        'orioles': {'sport': 'baseball', 'league': 'mlb', 'team_id': '1'},
        'baltimore': {'sport': 'baseball', 'league': 'mlb', 'team_id': '1'},
        'bal': {'sport': 'baseball', 'league': 'mlb', 'team_id': '1'},
        'padres': {'sport': 'baseball', 'league': 'mlb', 'team_id': '25'},
        'san diego': {'sport': 'baseball', 'league': 'mlb', 'team_id': '25'},
        'sd': {'sport': 'baseball', 'league': 'mlb', 'team_id': '25'},
        'phillies': {'sport': 'baseball', 'league': 'mlb', 'team_id': '22'},
        'philadelphia': {'sport': 'baseball', 'league': 'mlb', 'team_id': '22'},
        'phi': {'sport': 'baseball', 'league': 'mlb', 'team_id': '22'},
        'pirates': {'sport': 'baseball', 'league': 'mlb', 'team_id': '23'},
        'pittsburgh': {'sport': 'baseball', 'league': 'mlb', 'team_id': '23'},
        'pit': {'sport': 'baseball', 'league': 'mlb', 'team_id': '23'},
        'rangers': {'sport': 'baseball', 'league': 'mlb', 'team_id': '13'},
        'texas': {'sport': 'baseball', 'league': 'mlb', 'team_id': '13'},
        'tex': {'sport': 'baseball', 'league': 'mlb', 'team_id': '13'},
        'rays': {'sport': 'baseball', 'league': 'mlb', 'team_id': '30'},
        'tampa bay': {'sport': 'baseball', 'league': 'mlb', 'team_id': '30'},
        'tb': {'sport': 'baseball', 'league': 'mlb', 'team_id': '30'},
        'red sox': {'sport': 'baseball', 'league': 'mlb', 'team_id': '2'},
        'boston': {'sport': 'baseball', 'league': 'mlb', 'team_id': '2'},
        'bos': {'sport': 'baseball', 'league': 'mlb', 'team_id': '2'},
        'reds': {'sport': 'baseball', 'league': 'mlb', 'team_id': '17'},
        'cincinnati': {'sport': 'baseball', 'league': 'mlb', 'team_id': '17'},
        'cin': {'sport': 'baseball', 'league': 'mlb', 'team_id': '17'},
        'rockies': {'sport': 'baseball', 'league': 'mlb', 'team_id': '27'},
        'colorado': {'sport': 'baseball', 'league': 'mlb', 'team_id': '27'},
        'col': {'sport': 'baseball', 'league': 'mlb', 'team_id': '27'},
        'royals': {'sport': 'baseball', 'league': 'mlb', 'team_id': '7'},
        'kansas city': {'sport': 'baseball', 'league': 'mlb', 'team_id': '7'},
        'kc': {'sport': 'baseball', 'league': 'mlb', 'team_id': '7'},
        'tigers': {'sport': 'baseball', 'league': 'mlb', 'team_id': '6'},
        'detroit': {'sport': 'baseball', 'league': 'mlb', 'team_id': '6'},
        'det': {'sport': 'baseball', 'league': 'mlb', 'team_id': '6'},
        'twins': {'sport': 'baseball', 'league': 'mlb', 'team_id': '9'},
        'minnesota': {'sport': 'baseball', 'league': 'mlb', 'team_id': '9'},
        'min': {'sport': 'baseball', 'league': 'mlb', 'team_id': '9'},
        'white sox': {'sport': 'baseball', 'league': 'mlb', 'team_id': '4'},
        'chw': {'sport': 'baseball', 'league': 'mlb', 'team_id': '4'},
        'yankees': {'sport': 'baseball', 'league': 'mlb', 'team_id': '10'},
        'new york': {'sport': 'baseball', 'league': 'mlb', 'team_id': '10'},
        'nyy': {'sport': 'baseball', 'league': 'mlb', 'team_id': '10'},
        
        # NBA Teams (limited data available from API)
        'lakers': {'sport': 'basketball', 'league': 'nba', 'team_id': '13'},
        'warriors': {'sport': 'basketball', 'league': 'nba', 'team_id': '9'},
        'celtics': {'sport': 'basketball', 'league': 'nba', 'team_id': '2'},
        'heat': {'sport': 'basketball', 'league': 'nba', 'team_id': '14'},
        '76ers': {'sport': 'basketball', 'league': 'nba', 'team_id': '20'},
        'knicks': {'sport': 'basketball', 'league': 'nba', 'team_id': '18'},
        'pelicans': {'sport': 'basketball', 'league': 'nba', 'team_id': '3'},
        'trail blazers': {'sport': 'basketball', 'league': 'nba', 'team_id': '22'},
        'blazers': {'sport': 'basketball', 'league': 'nba', 'team_id': '22'},
        
        # WNBA Teams
        'storm': {'sport': 'basketball', 'league': 'wnba', 'team_id': '14'},
        'seattle storm': {'sport': 'basketball', 'league': 'wnba', 'team_id': '14'},
        'liberty': {'sport': 'basketball', 'league': 'wnba', 'team_id': '9'},
        'new york liberty': {'sport': 'basketball', 'league': 'wnba', 'team_id': '9'},
        'sparks': {'sport': 'basketball', 'league': 'wnba', 'team_id': '6'},
        'los angeles sparks': {'sport': 'basketball', 'league': 'wnba', 'team_id': '6'},
        'sky': {'sport': 'basketball', 'league': 'wnba', 'team_id': '19'},
        'chicago sky': {'sport': 'basketball', 'league': 'wnba', 'team_id': '19'},
        'dream': {'sport': 'basketball', 'league': 'wnba', 'team_id': '20'},
        'atlanta dream': {'sport': 'basketball', 'league': 'wnba', 'team_id': '20'},
        'sun': {'sport': 'basketball', 'league': 'wnba', 'team_id': '18'},
        'connecticut sun': {'sport': 'basketball', 'league': 'wnba', 'team_id': '18'},
        'wings': {'sport': 'basketball', 'league': 'wnba', 'team_id': '3'},
        'dallas wings': {'sport': 'basketball', 'league': 'wnba', 'team_id': '3'},
        'valkyries': {'sport': 'basketball', 'league': 'wnba', 'team_id': '129689'},
        'golden state valkyries': {'sport': 'basketball', 'league': 'wnba', 'team_id': '129689'},
        'fever': {'sport': 'basketball', 'league': 'wnba', 'team_id': '5'},
        'indiana fever': {'sport': 'basketball', 'league': 'wnba', 'team_id': '5'},
        'aces': {'sport': 'basketball', 'league': 'wnba', 'team_id': '17'},
        'las vegas aces': {'sport': 'basketball', 'league': 'wnba', 'team_id': '17'},
        'lynx': {'sport': 'basketball', 'league': 'wnba', 'team_id': '8'},
        'minnesota lynx': {'sport': 'basketball', 'league': 'wnba', 'team_id': '8'},
        'mercury': {'sport': 'basketball', 'league': 'wnba', 'team_id': '11'},
        'phoenix mercury': {'sport': 'basketball', 'league': 'wnba', 'team_id': '11'},
        'mystics': {'sport': 'basketball', 'league': 'wnba', 'team_id': '16'},
        'washington mystics': {'sport': 'basketball', 'league': 'wnba', 'team_id': '16'},
        
        # NHL Teams
        'ducks': {'sport': 'hockey', 'league': 'nhl', 'team_id': '25'},
        'anaheim': {'sport': 'hockey', 'league': 'nhl', 'team_id': '25'},
        'ana': {'sport': 'hockey', 'league': 'nhl', 'team_id': '25'},
        'bruins': {'sport': 'hockey', 'league': 'nhl', 'team_id': '1'},
        'boston bruins': {'sport': 'hockey', 'league': 'nhl', 'team_id': '1'},
        'bos': {'sport': 'hockey', 'league': 'nhl', 'team_id': '1'},
        'sabres': {'sport': 'hockey', 'league': 'nhl', 'team_id': '2'},
        'buffalo': {'sport': 'hockey', 'league': 'nhl', 'team_id': '2'},
        'buf': {'sport': 'hockey', 'league': 'nhl', 'team_id': '2'},
        'flames': {'sport': 'hockey', 'league': 'nhl', 'team_id': '3'},
        'calgary': {'sport': 'hockey', 'league': 'nhl', 'team_id': '3'},
        'cgy': {'sport': 'hockey', 'league': 'nhl', 'team_id': '3'},
        'hurricanes': {'sport': 'hockey', 'league': 'nhl', 'team_id': '7'},
        'carolina': {'sport': 'hockey', 'league': 'nhl', 'team_id': '7'},
        'car': {'sport': 'hockey', 'league': 'nhl', 'team_id': '7'},
        'blackhawks': {'sport': 'hockey', 'league': 'nhl', 'team_id': '4'},
        'chicago blackhawks': {'sport': 'hockey', 'league': 'nhl', 'team_id': '4'},
        'chi': {'sport': 'hockey', 'league': 'nhl', 'team_id': '4'},
        'avalanche': {'sport': 'hockey', 'league': 'nhl', 'team_id': '17'},
        'colorado': {'sport': 'hockey', 'league': 'nhl', 'team_id': '17'},
        'col': {'sport': 'hockey', 'league': 'nhl', 'team_id': '17'},
        'blue jackets': {'sport': 'hockey', 'league': 'nhl', 'team_id': '29'},
        'columbus': {'sport': 'hockey', 'league': 'nhl', 'team_id': '29'},
        'cbj': {'sport': 'hockey', 'league': 'nhl', 'team_id': '29'},
        'stars': {'sport': 'hockey', 'league': 'nhl', 'team_id': '9'},
        'dallas': {'sport': 'hockey', 'league': 'nhl', 'team_id': '9'},
        'dal': {'sport': 'hockey', 'league': 'nhl', 'team_id': '9'},
        'red wings': {'sport': 'hockey', 'league': 'nhl', 'team_id': '5'},
        'detroit': {'sport': 'hockey', 'league': 'nhl', 'team_id': '5'},
        'det': {'sport': 'hockey', 'league': 'nhl', 'team_id': '5'},
        'oilers': {'sport': 'hockey', 'league': 'nhl', 'team_id': '6'},
        'edmonton': {'sport': 'hockey', 'league': 'nhl', 'team_id': '6'},
        'edm': {'sport': 'hockey', 'league': 'nhl', 'team_id': '6'},
        'panthers': {'sport': 'hockey', 'league': 'nhl', 'team_id': '26'},
        'florida': {'sport': 'hockey', 'league': 'nhl', 'team_id': '26'},
        'fla': {'sport': 'hockey', 'league': 'nhl', 'team_id': '26'},
        'kings': {'sport': 'hockey', 'league': 'nhl', 'team_id': '8'},
        'los angeles': {'sport': 'hockey', 'league': 'nhl', 'team_id': '8'},
        'la': {'sport': 'hockey', 'league': 'nhl', 'team_id': '8'},
        'wild': {'sport': 'hockey', 'league': 'nhl', 'team_id': '30'},
        'minnesota': {'sport': 'hockey', 'league': 'nhl', 'team_id': '30'},
        'min': {'sport': 'hockey', 'league': 'nhl', 'team_id': '30'},
        'canadiens': {'sport': 'hockey', 'league': 'nhl', 'team_id': '10'},
        'montreal': {'sport': 'hockey', 'league': 'nhl', 'team_id': '10'},
        'mtl': {'sport': 'hockey', 'league': 'nhl', 'team_id': '10'},
        'predators': {'sport': 'hockey', 'league': 'nhl', 'team_id': '27'},
        'nashville': {'sport': 'hockey', 'league': 'nhl', 'team_id': '27'},
        'nsh': {'sport': 'hockey', 'league': 'nhl', 'team_id': '27'},
        'devils': {'sport': 'hockey', 'league': 'nhl', 'team_id': '11'},
        'new jersey': {'sport': 'hockey', 'league': 'nhl', 'team_id': '11'},
        'nj': {'sport': 'hockey', 'league': 'nhl', 'team_id': '11'},
        'islanders': {'sport': 'hockey', 'league': 'nhl', 'team_id': '12'},
        'new york islanders': {'sport': 'hockey', 'league': 'nhl', 'team_id': '12'},
        'nyi': {'sport': 'hockey', 'league': 'nhl', 'team_id': '12'},
        'rangers': {'sport': 'hockey', 'league': 'nhl', 'team_id': '13'},
        'new york rangers': {'sport': 'hockey', 'league': 'nhl', 'team_id': '13'},
        'nyr': {'sport': 'hockey', 'league': 'nhl', 'team_id': '13'},
        'senators': {'sport': 'hockey', 'league': 'nhl', 'team_id': '14'},
        'ottawa': {'sport': 'hockey', 'league': 'nhl', 'team_id': '14'},
        'ott': {'sport': 'hockey', 'league': 'nhl', 'team_id': '14'},
        'flyers': {'sport': 'hockey', 'league': 'nhl', 'team_id': '15'},
        'philadelphia': {'sport': 'hockey', 'league': 'nhl', 'team_id': '15'},
        'phi': {'sport': 'hockey', 'league': 'nhl', 'team_id': '15'},
        'penguins': {'sport': 'hockey', 'league': 'nhl', 'team_id': '16'},
        'pittsburgh': {'sport': 'hockey', 'league': 'nhl', 'team_id': '16'},
        'pit': {'sport': 'hockey', 'league': 'nhl', 'team_id': '16'},
        'sharks': {'sport': 'hockey', 'league': 'nhl', 'team_id': '18'},
        'san jose': {'sport': 'hockey', 'league': 'nhl', 'team_id': '18'},
        'sj': {'sport': 'hockey', 'league': 'nhl', 'team_id': '18'},
        'kraken': {'sport': 'hockey', 'league': 'nhl', 'team_id': '124292'},
        'seattle kraken': {'sport': 'hockey', 'league': 'nhl', 'team_id': '124292'},
        'seattle': {'sport': 'hockey', 'league': 'nhl', 'team_id': '124292'},
        'blues': {'sport': 'hockey', 'league': 'nhl', 'team_id': '19'},
        'st louis': {'sport': 'hockey', 'league': 'nhl', 'team_id': '19'},
        'stl': {'sport': 'hockey', 'league': 'nhl', 'team_id': '19'},
        'lightning': {'sport': 'hockey', 'league': 'nhl', 'team_id': '20'},
        'tampa bay': {'sport': 'hockey', 'league': 'nhl', 'team_id': '20'},
        'tb': {'sport': 'hockey', 'league': 'nhl', 'team_id': '20'},
        'maple leafs': {'sport': 'hockey', 'league': 'nhl', 'team_id': '21'},
        'toronto': {'sport': 'hockey', 'league': 'nhl', 'team_id': '21'},
        'tor': {'sport': 'hockey', 'league': 'nhl', 'team_id': '21'},
        'mammoth': {'sport': 'hockey', 'league': 'nhl', 'team_id': '129764'},
        'utah': {'sport': 'hockey', 'league': 'nhl', 'team_id': '129764'},
        'utah mammoth': {'sport': 'hockey', 'league': 'nhl', 'team_id': '129764'},
        'canucks': {'sport': 'hockey', 'league': 'nhl', 'team_id': '22'},
        'vancouver': {'sport': 'hockey', 'league': 'nhl', 'team_id': '22'},
        'van': {'sport': 'hockey', 'league': 'nhl', 'team_id': '22'},
        'golden knights': {'sport': 'hockey', 'league': 'nhl', 'team_id': '37'},
        'vegas': {'sport': 'hockey', 'league': 'nhl', 'team_id': '37'},
        'vgk': {'sport': 'hockey', 'league': 'nhl', 'team_id': '37'},
        'capitals': {'sport': 'hockey', 'league': 'nhl', 'team_id': '23'},
        'washington': {'sport': 'hockey', 'league': 'nhl', 'team_id': '23'},
        'wsh': {'sport': 'hockey', 'league': 'nhl', 'team_id': '23'},
        'jets': {'sport': 'hockey', 'league': 'nhl', 'team_id': '28'},
        'winnipeg': {'sport': 'hockey', 'league': 'nhl', 'team_id': '28'},
        'wpg': {'sport': 'hockey', 'league': 'nhl', 'team_id': '28'},
        
        # PWHL Teams (Professional Women's Hockey League)
        # NOTE: As of now, PWHL data may not be available in ESPN's API yet.
        # ESPN may not have added PWHL teams to their API endpoints.
        # Team IDs need to be verified using test_scripts/find_espn_team_id.py hockey pwhl <team_name>
        # Once ESPN adds PWHL support and team IDs are verified, uncomment and update the team_id values below
        # 'torrent': {'sport': 'hockey', 'league': 'pwhl', 'team_id': 'VERIFY_TEAM_ID'},
        # 'seattle torrent': {'sport': 'hockey', 'league': 'pwhl', 'team_id': 'VERIFY_TEAM_ID'},
        # 'boston pwhl': {'sport': 'hockey', 'league': 'pwhl', 'team_id': 'VERIFY_TEAM_ID'},
        # 'minnesota pwhl': {'sport': 'hockey', 'league': 'pwhl', 'team_id': 'VERIFY_TEAM_ID'},
        # 'montreal pwhl': {'sport': 'hockey', 'league': 'pwhl', 'team_id': 'VERIFY_TEAM_ID'},
        # 'new york pwhl': {'sport': 'hockey', 'league': 'pwhl', 'team_id': 'VERIFY_TEAM_ID'},
        # 'ottawa pwhl': {'sport': 'hockey', 'league': 'pwhl', 'team_id': 'VERIFY_TEAM_ID'},
        # 'toronto pwhl': {'sport': 'hockey', 'league': 'pwhl', 'team_id': 'VERIFY_TEAM_ID'},
        
        # WHL Teams (Western Hockey League) - using TheSportsDB API
        'thunderbirds': {'sport': 'hockey', 'league': 'whl', 'team_id': '144380', 'api_source': 'thesportsdb'},
        'seattle thunderbirds': {'sport': 'hockey', 'league': 'whl', 'team_id': '144380', 'api_source': 'thesportsdb'},
        't-birds': {'sport': 'hockey', 'league': 'whl', 'team_id': '144380', 'api_source': 'thesportsdb'},
        'winterhawks': {'sport': 'hockey', 'league': 'whl', 'team_id': '144379', 'api_source': 'thesportsdb'},
        'portland winterhawks': {'sport': 'hockey', 'league': 'whl', 'team_id': '144379', 'api_source': 'thesportsdb'},
        'silvertips': {'sport': 'hockey', 'league': 'whl', 'team_id': '144378', 'api_source': 'thesportsdb'},
        'everett silvertips': {'sport': 'hockey', 'league': 'whl', 'team_id': '144378', 'api_source': 'thesportsdb'},
        'everett': {'sport': 'hockey', 'league': 'whl', 'team_id': '144378', 'api_source': 'thesportsdb'},
        'spokane chiefs': {'sport': 'hockey', 'league': 'whl', 'team_id': '144381', 'api_source': 'thesportsdb'},
        'spokane': {'sport': 'hockey', 'league': 'whl', 'team_id': '144381', 'api_source': 'thesportsdb'},
        'vancouver giants': {'sport': 'hockey', 'league': 'whl', 'team_id': '144376', 'api_source': 'thesportsdb'},
        'blazers': {'sport': 'hockey', 'league': 'whl', 'team_id': '144373', 'api_source': 'thesportsdb'},
        'kamloops blazers': {'sport': 'hockey', 'league': 'whl', 'team_id': '144373', 'api_source': 'thesportsdb'},
        'kamloops': {'sport': 'hockey', 'league': 'whl', 'team_id': '144373', 'api_source': 'thesportsdb'},
        'cougars': {'sport': 'hockey', 'league': 'whl', 'team_id': '144375', 'api_source': 'thesportsdb'},
        'prince george cougars': {'sport': 'hockey', 'league': 'whl', 'team_id': '144375', 'api_source': 'thesportsdb'},
        'prince george': {'sport': 'hockey', 'league': 'whl', 'team_id': '144375', 'api_source': 'thesportsdb'},
        'rockets': {'sport': 'hockey', 'league': 'whl', 'team_id': '144374', 'api_source': 'thesportsdb'},
        'kelowna rockets': {'sport': 'hockey', 'league': 'whl', 'team_id': '144374', 'api_source': 'thesportsdb'},
        'kelowna': {'sport': 'hockey', 'league': 'whl', 'team_id': '144374', 'api_source': 'thesportsdb'},
        'tri-city americans': {'sport': 'hockey', 'league': 'whl', 'team_id': '144382', 'api_source': 'thesportsdb'},
        'americans': {'sport': 'hockey', 'league': 'whl', 'team_id': '144382', 'api_source': 'thesportsdb'},
        'tri city': {'sport': 'hockey', 'league': 'whl', 'team_id': '144382', 'api_source': 'thesportsdb'},
        'tricity': {'sport': 'hockey', 'league': 'whl', 'team_id': '144382', 'api_source': 'thesportsdb'},
        'wenatchee wild': {'sport': 'hockey', 'league': 'whl', 'team_id': '144372', 'api_source': 'thesportsdb'},
        'wenatchee': {'sport': 'hockey', 'league': 'whl', 'team_id': '144372', 'api_source': 'thesportsdb'},
        'victoria royals': {'sport': 'hockey', 'league': 'whl', 'team_id': '144377', 'api_source': 'thesportsdb'},
        'victoria': {'sport': 'hockey', 'league': 'whl', 'team_id': '144377', 'api_source': 'thesportsdb'},
        'edmonton oil kings': {'sport': 'hockey', 'league': 'whl', 'team_id': '144362', 'api_source': 'thesportsdb'},
        'oil kings': {'sport': 'hockey', 'league': 'whl', 'team_id': '144362', 'api_source': 'thesportsdb'},
        'calgary hitmen': {'sport': 'hockey', 'league': 'whl', 'team_id': '144361', 'api_source': 'thesportsdb'},
        'hitmen': {'sport': 'hockey', 'league': 'whl', 'team_id': '144361', 'api_source': 'thesportsdb'},
        'red deer rebels': {'sport': 'hockey', 'league': 'whl', 'team_id': '144365', 'api_source': 'thesportsdb'},
        'red deer': {'sport': 'hockey', 'league': 'whl', 'team_id': '144365', 'api_source': 'thesportsdb'},
        'medicine hat tigers': {'sport': 'hockey', 'league': 'whl', 'team_id': '144364', 'api_source': 'thesportsdb'},
        'medicine hat': {'sport': 'hockey', 'league': 'whl', 'team_id': '144364', 'api_source': 'thesportsdb'},
        'lethbridge hurricanes': {'sport': 'hockey', 'league': 'whl', 'team_id': '144363', 'api_source': 'thesportsdb'},
        'lethbridge': {'sport': 'hockey', 'league': 'whl', 'team_id': '144363', 'api_source': 'thesportsdb'},
        'swift current broncos': {'sport': 'hockey', 'league': 'whl', 'team_id': '144366', 'api_source': 'thesportsdb'},
        'swift current': {'sport': 'hockey', 'league': 'whl', 'team_id': '144366', 'api_source': 'thesportsdb'},
        'moose jaw warriors': {'sport': 'hockey', 'league': 'whl', 'team_id': '144368', 'api_source': 'thesportsdb'},
        'moose jaw': {'sport': 'hockey', 'league': 'whl', 'team_id': '144368', 'api_source': 'thesportsdb'},
        'regina pats': {'sport': 'hockey', 'league': 'whl', 'team_id': '144370', 'api_source': 'thesportsdb'},
        'pats': {'sport': 'hockey', 'league': 'whl', 'team_id': '144370', 'api_source': 'thesportsdb'},
        'regina': {'sport': 'hockey', 'league': 'whl', 'team_id': '144370', 'api_source': 'thesportsdb'},
        'saskatoon blades': {'sport': 'hockey', 'league': 'whl', 'team_id': '144371', 'api_source': 'thesportsdb'},
        'blades': {'sport': 'hockey', 'league': 'whl', 'team_id': '144371', 'api_source': 'thesportsdb'},
        'saskatoon': {'sport': 'hockey', 'league': 'whl', 'team_id': '144371', 'api_source': 'thesportsdb'},
        'prince albert raiders': {'sport': 'hockey', 'league': 'whl', 'team_id': '144369', 'api_source': 'thesportsdb'},
        'prince albert': {'sport': 'hockey', 'league': 'whl', 'team_id': '144369', 'api_source': 'thesportsdb'},
        'brandon wheat kings': {'sport': 'hockey', 'league': 'whl', 'team_id': '144367', 'api_source': 'thesportsdb'},
        'wheat kings': {'sport': 'hockey', 'league': 'whl', 'team_id': '144367', 'api_source': 'thesportsdb'},
        'brandon': {'sport': 'hockey', 'league': 'whl', 'team_id': '144367', 'api_source': 'thesportsdb'},
        
        # MLS Teams
        'sounders': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9726'},
        'seattle sounders': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9726'},
        
        # NWSL Teams
        'reign': {'sport': 'soccer', 'league': 'usa.nwsl', 'team_id': '15363'},
        'seattle reign': {'sport': 'soccer', 'league': 'usa.nwsl', 'team_id': '15363'},
        'racing': {'sport': 'soccer', 'league': 'usa.nwsl', 'team_id': '20905'},
        'racing louisville': {'sport': 'soccer', 'league': 'usa.nwsl', 'team_id': '20905'},
        'louisville': {'sport': 'soccer', 'league': 'usa.nwsl', 'team_id': '20905'},
        'atlanta united': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '18418'},
        'atl': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '18418'},
        'austin fc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '20906'},
        'atx': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '20906'},
        'cf montreal': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9720'},
        'montreal': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9720'},
        'mtl': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9720'},
        'charlotte fc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '21300'},
        'clt': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '21300'},
        'chicago fire': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '182'},
        'fire': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '182'},
        'chi': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '182'},
        'rapids': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '184'},
        'colorado': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '184'},
        'col': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '184'},
        'crew': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '183'},
        'columbus': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '183'},
        'clb': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '183'},
        'dc united': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '193'},
        'dc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '193'},
        'fc cincinnati': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '18267'},
        'cincinnati': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '18267'},
        'cin': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '18267'},
        'fc dallas': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '185'},
        'dallas': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '185'},
        'dal': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '185'},
        'dynamo': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '6077'},
        'houston': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '6077'},
        'hou': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '6077'},
        'inter miami': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '20232'},
        'miami': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '20232'},
        'mia': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '20232'},
        'la galaxy': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '187'},
        'galaxy': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '187'},
        'la': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '187'},
        'lafc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '18966'},
        'minnesota united': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '17362'},
        'minnesota': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '17362'},
        'min': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '17362'},
        'nashville sc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '18986'},
        'nashville': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '18986'},
        'nsh': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '18986'},
        'revolution': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '189'},
        'new england': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '189'},
        'ne': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '189'},
        'nyc fc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '17606'},
        'nyc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '17606'},
        'red bulls': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '190'},
        'ny': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '190'},
        'orlando city': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '12011'},
        'orlando': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '12011'},
        'orl': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '12011'},
        'union': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '10739'},
        'philadelphia': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '10739'},
        'phi': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '10739'},
        'timbers': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9723'},
        'portland': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9723'},
        'por': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9723'},
        'real salt lake': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '4771'},
        'salt lake': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '4771'},
        'rsl': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '4771'},
        'san diego fc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '22529'},
        'san diego': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '22529'},
        'sd': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '22529'},
        'earthquakes': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '191'},
        'san jose': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '191'},
        'sj': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '191'},
        'sporting kc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '186'},
        'sporting kansas city': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '186'},
        'skc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '186'},
        'st louis city': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '21812'},
        'st louis': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '21812'},
        'stl': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '21812'},
        'toronto fc': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '7318'},
        'toronto': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '7318'},
        'tor': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '7318'},
        'whitecaps': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9727'},
        'vancouver': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9727'},
        'van': {'sport': 'soccer', 'league': 'usa.1', 'team_id': '9727'},
        
        # Premier League Teams
        'lfc': {'sport': 'soccer', 'league': 'eng.1', 'team_id': '364'},
        'liverpool': {'sport': 'soccer', 'league': 'eng.1', 'team_id': '364'},
        'manchester united': {'sport': 'soccer', 'league': 'eng.1', 'team_id': '360'},
        'man united': {'sport': 'soccer', 'league': 'eng.1', 'team_id': '360'},
        'arsenal': {'sport': 'soccer', 'league': 'eng.1', 'team_id': '359'},
        'chelsea': {'sport': 'soccer', 'league': 'eng.1', 'team_id': '363'},
        'manchester city': {'sport': 'soccer', 'league': 'eng.1', 'team_id': '382'},
        'man city': {'sport': 'soccer', 'league': 'eng.1', 'team_id': '382'},
    }
    
    def __init__(self, bot):
        super().__init__(bot)
        self.url_timeout = 10  # seconds
        
        # Per-user cooldown tracking
        self.user_cooldowns = {}  # user_id -> last_execution_time
        
        # Initialize TheSportsDB client
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
    
    def is_womens_league(self, sport: str, league: str) -> bool:
        """Check if the league is a women's league"""
        womens_leagues = {
            ('basketball', 'wnba'),
            ('soccer', 'usa.nwsl'),
            ('hockey', 'pwhl')
        }
        return (sport, league) in womens_leagues
    
    def get_team_abbreviation(self, team_id: str, team_abbreviation: str, sport: str, league: str) -> str:
        """Get team abbreviation, using -W suffix only for women's leagues"""
        if self.is_womens_league(sport, league):
            return self.WOMENS_TEAM_ABBREVIATIONS.get(team_id, team_abbreviation)
        else:
            return team_abbreviation
    
    def extract_score(self, competitor: Dict) -> str:
        """Extract score value from competitor data, handling both dict and string formats
        
        ESPN API returns scores in different formats:
        - Schedule endpoint: {'value': 13.0, 'displayValue': '13'}
        - Scoreboard endpoint: may be string or dict format
        
        Returns the score as a string for consistent formatting.
        """
        score = competitor.get('score', '0')
        
        # Handle dictionary format (from schedule endpoint)
        if isinstance(score, dict):
            # Prefer displayValue if available, otherwise use value
            if 'displayValue' in score:
                return str(score['displayValue'])
            elif 'value' in score:
                # Convert float to int if it's a whole number, otherwise keep as is
                value = score['value']
                if isinstance(value, float) and value.is_integer():
                    return str(int(value))
                return str(value)
            else:
                return '0'
        
        # Handle string format (from scoreboard endpoint or already processed)
        if isinstance(score, str):
            return score
        
        # Handle numeric format
        if isinstance(score, (int, float)):
            if isinstance(score, float) and score.is_integer():
                return str(int(score))
            return str(score)
        
        # Fallback
        return '0'
    
    def extract_shootout_score(self, competitor: Dict) -> Optional[int]:
        """Extract penalty shootout score from competitor data"""
        score = competitor.get('score', {})
        if isinstance(score, dict) and 'shootoutScore' in score:
            shootout = score['shootoutScore']
            if isinstance(shootout, (int, float)):
                return int(shootout) if isinstance(shootout, float) and shootout.is_integer() else int(shootout)
        return None
    
    def format_clean_date_time(self, dt) -> str:
        """Format date and time without leading zeros"""
        month = dt.month
        day = dt.day
        minute = dt.minute
        ampm = dt.strftime("%p")
        
        # Convert to 12-hour format
        hour_12 = dt.hour
        if hour_12 == 0:
            hour_12 = 12
        elif hour_12 > 12:
            hour_12 = hour_12 - 12
        
        # Remove leading zeros
        time_str = f"{month}/{day} {hour_12}:{minute:02d} {ampm}"
        return time_str
    
    def format_clean_date(self, dt) -> str:
        """Format date without leading zeros"""
        month = dt.month
        day = dt.day
        return f"{month}/{day}"
    
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
        # Check if sports command is enabled
        sports_enabled = self.get_config_value('Sports_Command', 'sports_enabled', fallback=True, value_type='bool')
        if not sports_enabled:
            return False
        
        # Channel access is now handled by BaseCommand.is_channel_allowed()
        # Call parent can_execute() which includes channel checking
        if not super().can_execute(message):
            return False
        
        # Check per-user cooldown (don't set it here, just check)
        if self.cooldown_seconds > 0:
            import time
            current_time = time.time()
            user_id = message.sender_id or "unknown"
            
            if user_id in self.user_cooldowns:
                time_since_last = current_time - self.user_cooldowns[user_id]
                if time_since_last < self.cooldown_seconds:
                    remaining = self.cooldown_seconds - time_since_last
                    self.logger.info(f"Sports command cooldown active for user {user_id}, {remaining:.1f}s remaining")
                    return False
        
        return True
    
    def get_help_text(self) -> str:
        return self.translate('commands.sports.help')
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the sports command"""
        try:
            # Set cooldown for this user
            if self.cooldown_seconds > 0:
                import time
                current_time = time.time()
                user_id = message.sender_id or "unknown"
                self.user_cooldowns[user_id] = current_time
            
            # Parse the command
            content = message.content.strip()
            if content.startswith('!'):
                content = content[1:].strip()
            
            # Extract team name if provided
            parts = content.split()
            if len(parts) > 1:
                # Join all parts after 'sports' keyword, preserving "schedule" if present
                team_name = ' '.join(parts[1:]).lower()
                response = await self.get_team_scores(team_name)
            else:
                # Check if this channel has an override team
                if not message.is_dm and message.channel in self.channel_overrides:
                    override_team = self.channel_overrides[message.channel]
                    response = await self.get_team_scores(override_team)
                else:
                    response = await self.get_default_teams_scores()
            
            # Send response
            return await self.send_response(message, response)
            
        except Exception as e:
            self.logger.error(f"Error in sports command: {e}")
            return await self.send_response(message, self.translate('commands.sports.error_fetching'))
    
    async def get_default_teams_scores(self) -> str:
        """Get scores for default teams, sorted by game time"""
        if not self.default_teams:
            return self.translate('commands.sports.no_default_teams')
        
        game_data = []
        for team in self.default_teams:
            try:
                team_info = self.TEAM_MAPPINGS.get(team)
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
            sport_emoji = self.SPORT_EMOJIS.get(game['sport'], '🏆')
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
        league_mappings = {
            # NFL
            'nfl': {'sport': 'football', 'league': 'nfl'},
            'football': {'sport': 'football', 'league': 'nfl'},
            
            # CFL
            'cfl': {'sport': 'football', 'league': 'cfl'},
            'canadian football': {'sport': 'football', 'league': 'cfl'},
            
            # MLB
            'mlb': {'sport': 'baseball', 'league': 'mlb'},
            'baseball': {'sport': 'baseball', 'league': 'mlb'},
            
            # NBA
            'nba': {'sport': 'basketball', 'league': 'nba'},
            'basketball': {'sport': 'basketball', 'league': 'nba'},
            
            # WNBA
            'wnba': {'sport': 'basketball', 'league': 'wnba'},
            'womens basketball': {'sport': 'basketball', 'league': 'wnba'},
            'womens': {'sport': 'basketball', 'league': 'wnba'},
            
            # NHL
            'nhl': {'sport': 'hockey', 'league': 'nhl'},
            'hockey': {'sport': 'hockey', 'league': 'nhl'},
            
            # PWHL
            'pwhl': {'sport': 'hockey', 'league': 'pwhl'},
            'womens hockey': {'sport': 'hockey', 'league': 'pwhl'},
            
            # WHL (Western Hockey League) - using TheSportsDB
            'whl': {'sport': 'hockey', 'league': 'whl', 'api_source': 'thesportsdb', 'league_id': '5160'},
            'western hockey league': {'sport': 'hockey', 'league': 'whl', 'api_source': 'thesportsdb', 'league_id': '5160'},
            
            # MLS
            'mls': {'sport': 'soccer', 'league': 'usa.1'},
            'soccer': {'sport': 'soccer', 'league': 'usa.1'},
            
            # NWSL
            'nwsl': {'sport': 'soccer', 'league': 'usa.nwsl'},
            'womens soccer': {'sport': 'soccer', 'league': 'usa.nwsl'},
            'womens': {'sport': 'soccer', 'league': 'usa.nwsl'},
            
            # Premier League
            'epl': {'sport': 'soccer', 'league': 'eng.1'},
            'premier league': {'sport': 'soccer', 'league': 'eng.1'},
            'premier': {'sport': 'soccer', 'league': 'eng.1'},
        }
        
        return league_mappings.get(league_name.lower())
    
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
            team_info = self.TEAM_MAPPINGS.get(team_name)
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
            sport_emoji = self.SPORT_EMOJIS.get(game['sport'], '🏆')
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
            # Construct API URL
            url = f"{self.ESPN_BASE_URL}/{league_info['sport']}/{league_info['league']}/scoreboard"
            
            # Make API request
            response = requests.get(url, timeout=self.url_timeout)
            response.raise_for_status()
            
            data = response.json()
            events = data.get('events', [])
            
            if not events:
                return self.translate('commands.sports.no_games_league', sport=league_info['sport'])
            
            # Parse all games and sort by time
            game_data = []
            for event in events:
                game_info = self.parse_league_game_event(event, league_info['sport'], league_info['league'])
                if game_info:
                    game_data.append(game_info)
            
            if not game_data:
                return self.translate('commands.sports.no_games_league', sport=league_info['sport'])
            
            # Sort by game time (earliest first)
            game_data.sort(key=lambda x: x['timestamp'])
            
            # Format responses with sport emojis
            responses = []
            for game in game_data[:5]:  # Limit to 5 games to keep under 130 chars
                sport_emoji = self.SPORT_EMOJIS.get(game['sport'], '🏆')
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
        """Get upcoming games for a league from TheSportsDB
        
        Fetches both upcoming and recent past events to provide a fuller response.
        """
        if not self.thesportsdb_client:
            self.logger.error("TheSportsDB client not initialized")
            return self.translate('commands.sports.error_fetching_league', sport=league_info.get('sport', 'unknown'))
        
        league_id = league_info.get('league_id')
        if not league_id:
            league_name = league_info.get('league', 'unknown').upper()
            return f"League ID not configured for {league_name}. Please query specific teams instead."
        
        try:
            # Fetch events from multiple sources to get more results
            import asyncio
            from datetime import timedelta
            loop = asyncio.get_event_loop()
            
            # Get today's date and next few days
            today = datetime.now().date()
            date_strings = [today.strftime('%Y-%m-%d')]
            for i in range(1, 7):  # Next 6 days
                date_strings.append((today + timedelta(days=i)).strftime('%Y-%m-%d'))
            
            # Fetch events from multiple sources in parallel
            next_events_task = loop.run_in_executor(
                None,
                lambda: self.thesportsdb_client.get_league_events_next(league_id, limit=15)
            )
            past_events_task = loop.run_in_executor(
                None,
                lambda: self.thesportsdb_client.get_league_events_past(league_id, limit=5)
            )
            
            # Fetch events for today and next few days
            def make_day_fetcher(date_str):
                return lambda: self.thesportsdb_client.get_events_by_day(date_str, league_id)
            
            day_events_tasks = [
                loop.run_in_executor(None, make_day_fetcher(d))
                for d in date_strings
            ]
            
            # Wait for all requests
            results = await asyncio.gather(next_events_task, past_events_task, *day_events_tasks)
            next_events = results[0]
            past_events = results[1]
            day_events_list = results[2:]  # List of lists from each day
            
            # Combine all day events into a single list
            all_day_events = []
            for day_events in day_events_list:
                all_day_events.extend(day_events)
            
            # Parse events - combine all sources and deduplicate by event ID
            now = datetime.now(timezone.utc).timestamp()
            eight_days_ago = now - (8 * 24 * 60 * 60)
            six_weeks_from_now = now + (6 * 7 * 24 * 60 * 60)
            
            # Combine all events and deduplicate by event ID
            all_events = []
            seen_event_ids = set()
            
            # Add past events
            for event in past_events:
                event_id = str(event.get('idEvent', ''))
                if event_id and event_id not in seen_event_ids:
                    all_events.append(event)
                    seen_event_ids.add(event_id)
            
            # Add next events
            for event in next_events:
                event_id = str(event.get('idEvent', ''))
                if event_id and event_id not in seen_event_ids:
                    all_events.append(event)
                    seen_event_ids.add(event_id)
            
            # Add day events
            for event in all_day_events:
                event_id = str(event.get('idEvent', ''))
                if event_id and event_id not in seen_event_ids:
                    all_events.append(event)
                    seen_event_ids.add(event_id)
            
            # Parse all events
            game_data = []
            for event in all_events:
                game_info = self.parse_thesportsdb_league_event(event, league_info['sport'], league_info['league'])
                if game_info:
                    event_ts = game_info.get('event_timestamp')
                    status = game_info.get('status', '')
                    
                    # Include:
                    # - Past games from last 8 days
                    # - Upcoming games within next 6 weeks
                    # - Live games (any status that's not NS/AP/FT/F)
                    if status not in ['NS', 'AP', 'FT', 'F', '']:
                        # Live or in-progress game
                        game_data.append(game_info)
                    elif event_ts:
                        if event_ts >= eight_days_ago and event_ts <= six_weeks_from_now:
                            game_data.append(game_info)
                    else:
                        # No timestamp but valid status - include it
                        game_data.append(game_info)
            
            if not game_data:
                return self.translate('commands.sports.no_games_league', sport=league_info.get('sport', 'unknown'))
            
            # Sort by game time (earliest first, but prioritize live games)
            game_data.sort(key=lambda x: x['timestamp'])
            
            # Format responses with sport emojis, building up to 130 characters
            sport_emoji = self.SPORT_EMOJIS.get(league_info['sport'], '🏆')
            responses = []
            current_length = 0
            max_length = 130
            
            for game in game_data:
                game_str = f"{sport_emoji} {game['formatted']}"
                
                # Check if adding this game would exceed limit
                if responses:
                    test_length = current_length + len("\n") + len(game_str)
                else:
                    test_length = len(game_str)
                
                if test_length <= max_length:
                    responses.append(game_str)
                    current_length = test_length
                else:
                    # Can't fit more games - stop before exceeding limit
                    break
            
            if not responses:
                # If even the first game doesn't fit, return it anyway (truncated)
                return f"{sport_emoji} {game_data[0]['formatted'][:120]}"
            
            return "\n".join(responses)
            
        except Exception as e:
            self.logger.error(f"Error fetching league scores from TheSportsDB: {e}")
            return self.translate('commands.sports.error_fetching_league', sport=league_info.get('sport', 'unknown'))
    
    def parse_thesportsdb_league_event(self, event: Dict, sport: str, league: str) -> Optional[Dict]:
        """Parse a TheSportsDB league event and return structured data with timestamp for sorting
        
        Similar to parse_thesportsdb_event but doesn't require a specific team_id.
        """
        try:
            # Extract team info
            home_team = event.get('strHomeTeam', '')
            away_team = event.get('strAwayTeam', '')
            home_score = event.get('intHomeScore', '')
            away_score = event.get('intAwayScore', '')
            status = event.get('strStatus', '')
            timestamp_str = event.get('strTimestamp', '')
            date_str = event.get('dateEvent', '')
            time_str = event.get('strTime', '')
            
            # Get team abbreviations
            home_abbr = self._get_team_abbreviation_from_name(home_team)
            away_abbr = self._get_team_abbreviation_from_name(away_team)
            
            # Get timestamp for sorting
            timestamp = 0
            event_timestamp = None
            if timestamp_str:
                try:
                    # Parse ISO format timestamp
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    event_timestamp = dt.timestamp()
                    timestamp = event_timestamp
                except:
                    # Try parsing date and time separately
                    if date_str and time_str:
                        try:
                            dt_str = f"{date_str} {time_str}"
                            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                            # Assume UTC if no timezone info
                            dt = dt.replace(tzinfo=timezone.utc)
                            event_timestamp = dt.timestamp()
                            timestamp = event_timestamp
                        except:
                            pass
            
            # Format based on status
            if status in ['FT', 'F']:  # Full Time / Final
                # Completed game
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.strptime(date_str, "%Y-%m-%d")
                        today = datetime.now().date()
                        game_date = dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(dt)}"
                    except:
                        pass
                
                formatted = f"{away_abbr} {away_score}-{home_score} @{home_abbr} (F{date_suffix})"
                timestamp = 9999999998  # Final games second to last
                
            elif status in ['NS', 'AP', '']:  # Not Started / Approved / Empty
                # Scheduled game
                if timestamp_str or (date_str and time_str):
                    try:
                        if timestamp_str:
                            dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        else:
                            dt_str = f"{date_str} {time_str}"
                            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                            dt = dt.replace(tzinfo=timezone.utc)
                        
                        local_dt = dt.astimezone()
                        time_str_formatted = self.format_clean_date_time(local_dt)
                        
                        formatted = f"{away_abbr} @ {home_abbr} ({time_str_formatted})"
                    except:
                        formatted = f"{away_abbr} @ {home_abbr} (TBD)"
                        timestamp = 9999999999  # Put TBD games last
                else:
                    formatted = f"{away_abbr} @ {home_abbr} (TBD)"
                    timestamp = 9999999999  # Put TBD games last
            else:
                # Other status (live game, postponed, etc.)
                formatted = f"{away_abbr} {away_score or '0'}-{home_score or '0'} @{home_abbr} ({status})"
                timestamp = -1 if status not in ['NS', 'AP'] else 9999999997
            
            return {
                'timestamp': timestamp,
                'event_timestamp': event_timestamp,
                'formatted': formatted,
                'sport': sport,
                'status': status
            }
            
        except Exception as e:
            self.logger.error(f"Error parsing TheSportsDB league event: {e}")
            return None
    
    def parse_league_game_event(self, event: Dict, sport: str, league: str) -> Optional[Dict]:
        """Parse a league game event and return structured data with timestamp for sorting"""
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
            
            # Determine home/away teams for all sports
            home_team = team1 if team1.get('homeAway') == 'home' else team2
            away_team = team2 if team1.get('homeAway') == 'home' else team1
            home_team_id = home_team.get('team', {}).get('id', '')
            away_team_id = away_team.get('team', {}).get('id', '')
            home_abbreviation = home_team.get('team', {}).get('abbreviation', 'UNK')
            away_abbreviation = away_team.get('team', {}).get('abbreviation', 'UNK')
            home_name = self.get_team_abbreviation(home_team_id, home_abbreviation, sport, league)
            away_name = self.get_team_abbreviation(away_team_id, away_abbreviation, sport, league)
            home_score = self.extract_score(home_team)
            away_score = self.extract_score(away_team)
            
            # Keep original variables for backward compatibility
            team1_name = away_name  # away team first
            team2_name = home_name  # home team second (gets @ symbol)
            team1_score = away_score
            team2_score = home_score
            
            # Get game status
            # In schedule endpoint, status is in competition, not event
            status = competition.get('status', event.get('status', {}))
            status_type = status.get('type', {})
            status_name = status_type.get('name', 'UNKNOWN')
            
            # Get timestamp for sorting
            date_str = event.get('date', '')
            timestamp = 0  # Default for sorting
            event_timestamp = None
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    event_timestamp = dt.timestamp()
                    timestamp = event_timestamp
                except:
                    pass
            
            # Format based on game status
            if status_name in ['STATUS_IN_PROGRESS', 'STATUS_FIRST_HALF', 'STATUS_SECOND_HALF', 'STATUS_END_PERIOD']:
                # Game is live - prioritize these (use negative timestamp)
                # STATUS_END_PERIOD means a period just ended but game is still ongoing
                clock = status.get('displayClock', '')
                period = status.get('period', 0)
                is_end_period = (status_name == 'STATUS_END_PERIOD')
                
                # Format period based on sport
                if sport == 'soccer':
                    # For soccer, use displayClock if available (e.g., "90'+5'"), otherwise use half
                    # For soccer, show home team first (traditional soccer format)
                    if clock and clock != '0:00' and clock != "0'":
                        period_str = clock  # Use displayClock directly (e.g., "90'+5'")
                        formatted = f"@{home_name} {home_score}-{away_score} {away_name} ({period_str})"
                    else:
                        period_str = f"{period}H"  # Fallback to half
                        formatted = f"@{home_name} {home_score}-{away_score} {away_name} ({clock} {period_str})"
                elif sport == 'baseball':
                    # Use shortDetail for ongoing baseball games to show top/bottom of inning
                    short_detail = status_type.get('shortDetail', '')
                    if short_detail and ('Top' in short_detail or 'Bottom' in short_detail):
                        period_str = short_detail  # e.g., "Top 14th", "Bottom 9th"
                    else:
                        period_str = f"{period}I"  # Fallback to inning number only
                    if is_end_period:
                        period_str = f"End {period_str}"
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({period_str})"
                elif sport == 'football':
                    period_str = f"Q{period}"  # Quarters
                    if is_end_period:
                        period_str = f"End {period_str}"
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({clock} {period_str})"
                else:
                    period_str = f"P{period}"  # Generic periods
                    if is_end_period:
                        period_str = f"End {period_str}"
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({clock} {period_str})"
                
                timestamp = -1  # Live games first
                
            elif status_name == 'STATUS_SCHEDULED':
                # Game is scheduled
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        time_str = self.format_clean_date_time(local_dt)
                        if sport == 'soccer':
                            formatted = f"@{home_name} vs. {away_name} ({time_str})"
                        else:
                            formatted = f"{away_name} @ {home_name} ({time_str})"
                    except:
                        if sport == 'soccer':
                            formatted = f"@{home_name} vs. {away_name} (TBD)"
                        else:
                            formatted = f"{away_name} @ {home_name} (TBD)"
                        timestamp = 9999999999  # Put TBD games last
                else:
                    if sport == 'soccer':
                        formatted = f"@{home_name} vs. {away_name} (TBD)"
                    else:
                        formatted = f"{away_name} @ {home_name} (TBD)"
                    timestamp = 9999999999  # Put TBD games last
                    
            elif status_name == 'STATUS_HALFTIME':
                # Game is at halftime
                if sport == 'soccer':
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} (HT)"
                else:
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} (HT)"
                timestamp = -2  # Halftime games second priority after live games
            elif status_name == 'STATUS_FULL_TIME':
                # Soccer game is finished - put these last
                # Check if game was played today or on a different day
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        today = datetime.now().date()
                        game_date = local_dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(local_dt)}"
                    except:
                        pass
                formatted = f"@{home_name} {home_score}-{away_score} {away_name} (FT{date_suffix})"
                timestamp = 9999999998  # Final games second to last
            elif status_name == 'STATUS_FINAL_PEN':
                # Soccer game finished in penalty shootout
                # Check if game was played today or on a different day
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        today = datetime.now().date()
                        game_date = local_dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(local_dt)}"
                    except:
                        pass
                
                # Get penalty shootout scores
                home_shootout = self.extract_shootout_score(home_team)
                away_shootout = self.extract_shootout_score(away_team)
                
                # Format with penalty shootout result
                if home_shootout is not None and away_shootout is not None:
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} (FT-PEN {home_shootout}-{away_shootout}{date_suffix})"
                else:
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} (FT-PEN{date_suffix})"
                
                timestamp = 9999999998  # Final games second to last
            elif status_name == 'STATUS_FINAL':
                # Other sports game is finished - put these last
                # Check if game was played today or on a different day
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        today = datetime.now().date()
                        game_date = local_dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(local_dt)}"
                    except:
                        pass
                formatted = f"{away_name} {away_score}-{home_score} @{home_name} (F{date_suffix})"
                timestamp = 9999999998  # Final games second to last
                
            else:
                # Other status
                if sport == 'soccer':
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} ({status_name})"
                else:
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({status_name})"
                timestamp = 9999999997  # Other statuses third to last
            
            return {
                'timestamp': timestamp,
                'event_timestamp': event_timestamp,
                'formatted': formatted,
                'sport': sport,
                'status': status_name
            }
                
        except Exception as e:
            self.logger.error(f"Error parsing league game event: {e}")
            return None
    
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
            team_info = self.TEAM_MAPPINGS.get(team_name_clean)
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
        team_info = self.TEAM_MAPPINGS.get(team_name)
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
        sport_emoji = self.SPORT_EMOJIS.get(team_info['sport'], '🏆')
        formatted_games = []
        current_length = 0
        max_length = 125  # Leave buffer to avoid cutoff
        
        for game in games:
            # Ensure game['formatted'] doesn't already have an emoji
            game_formatted = game['formatted'].strip()
            # Remove emoji if it's at the start (some games might have it)
            if game_formatted and game_formatted[0] in self.SPORT_EMOJIS.values():
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
            if game_formatted and game_formatted[0] in self.SPORT_EMOJIS.values():
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
            # Use team schedule endpoint - returns both past and upcoming games
            url = f"{self.ESPN_BASE_URL}/{team_info['sport']}/{team_info['league']}/teams/{team_info['team_id']}/schedule"
            
            # Make API request
            response = requests.get(url, timeout=self.url_timeout)
            response.raise_for_status()
            
            data = response.json()
            events = data.get('events', [])
            
            if not events:
                return []
            
            # Parse all games
            all_games = []
            live_event_ids = []  # Track event IDs for live games
            for event in events:
                game_data = self.parse_game_event_with_timestamp(event, team_info['team_id'], team_info['sport'], team_info['league'])
                if game_data:
                    all_games.append(game_data)
                    # If this is a live game, store the event ID to fetch live data
                    if game_data['timestamp'] < 0:  # Negative timestamp indicates live game
                        event_id = event.get('id')
                        if event_id:
                            live_event_ids.append((event_id, len(all_games) - 1))  # Store index too
            
            if not all_games:
                return []
            
            # Fetch live event data for live games to get real-time scores
            for event_id, game_index in live_event_ids:
                try:
                    live_event_data = await self.fetch_live_event_data(event_id, team_info['sport'], team_info['league'])
                    if live_event_data:
                        # The event endpoint returns the event directly (not in an array)
                        # Update the game data with live scores
                        updated_game = self.parse_game_event_with_timestamp(
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
        """Fetch team games from TheSportsDB API
        
        Returns games sorted by relevance:
        - Last completed game (if within last 8 days)
        - Next scheduled game (if known)
        """
        if not self.thesportsdb_client:
            self.logger.error("TheSportsDB client not initialized")
            return []
        
        try:
            team_id = team_info['team_id']
            
            # Fetch last events and next events
            # Run in executor to avoid blocking
            import asyncio
            loop = asyncio.get_event_loop()
            
            last_events_task = loop.run_in_executor(
                None,
                lambda: self.thesportsdb_client.get_team_events_last(team_id, limit=5)
            )
            next_events_task = loop.run_in_executor(
                None,
                lambda: self.thesportsdb_client.get_team_events_next(team_id, limit=5)
            )
            
            last_events, next_events = await asyncio.gather(last_events_task, next_events_task)
            
            # Parse events
            all_games = []
            
            # Parse last events (completed games)
            for event in last_events:
                game_data = self.parse_thesportsdb_event(event, team_id, team_info['sport'], team_info['league'])
                if game_data:
                    all_games.append(game_data)
            
            # Parse next events (upcoming games)
            for event in next_events:
                game_data = self.parse_thesportsdb_event(event, team_id, team_info['sport'], team_info['league'])
                if game_data:
                    all_games.append(game_data)
            
            if not all_games:
                return []
            
            # Get current time for comparison
            now = datetime.now(timezone.utc).timestamp()
            eight_days_ago = now - (8 * 24 * 60 * 60)
            six_weeks_from_now = now + (6 * 7 * 24 * 60 * 60)
            
            # Separate into categories
            upcoming_games = []
            past_games = []
            
            for game in all_games:
                game_event_ts = game.get('event_timestamp')
                effective_ts = game_event_ts if game_event_ts is not None else game['timestamp']
                
                if effective_ts is None:
                    # No timestamp, check status
                    if game.get('status') == 'FT' or game.get('status') == 'F':
                        past_games.append((now, game))
                    else:
                        upcoming_games.append((six_weeks_from_now, game))
                elif effective_ts > now:
                    # Future game - only include if within next 6 weeks
                    if effective_ts <= six_weeks_from_now:
                        upcoming_games.append((effective_ts, game))
                else:
                    # Past game - only include if within last 8 days
                    if effective_ts >= eight_days_ago:
                        past_games.append((effective_ts, game))
            
            # Sort upcoming games by soonest first, past games by most recent first
            upcoming_games.sort(key=lambda x: x[0] if x[0] is not None else float('inf'))
            past_games.sort(key=lambda x: x[0] if x[0] is not None else -float('inf'), reverse=True)
            
            # Build result:
            # 1. Last completed game (if within last 8 days)
            # 2. Next scheduled game (if known and within 6 weeks)
            result = []
            
            if past_games:
                result.append(past_games[0][1])  # Most recent past game
            
            if upcoming_games:
                result.append(upcoming_games[0][1])  # Next upcoming game
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error fetching team games from TheSportsDB: {e}")
            return []
    
    def parse_thesportsdb_event(self, event: Dict, team_id: str, sport: str, league: str) -> Optional[Dict]:
        """Parse a TheSportsDB event and return structured data with timestamp for sorting"""
        try:
            # Extract team info
            home_team = event.get('strHomeTeam', '')
            away_team = event.get('strAwayTeam', '')
            home_score = event.get('intHomeScore', '')
            away_score = event.get('intAwayScore', '')
            status = event.get('strStatus', '')
            timestamp_str = event.get('strTimestamp', '')
            date_str = event.get('dateEvent', '')
            time_str = event.get('strTime', '')
            
            # Determine if our team is home or away
            our_team_id = str(team_id)
            event_home_id = str(event.get('idHomeTeam', ''))
            event_away_id = str(event.get('idAwayTeam', ''))
            
            is_home = (event_home_id == our_team_id)
            
            # Get team abbreviations (use short names if available, otherwise use team names)
            # For now, use a simplified version of team names
            home_abbr = self._get_team_abbreviation_from_name(home_team)
            away_abbr = self._get_team_abbreviation_from_name(away_team)
            
            # Get timestamp for sorting
            timestamp = 0
            event_timestamp = None
            if timestamp_str:
                try:
                    # Parse ISO format timestamp
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    event_timestamp = dt.timestamp()
                    timestamp = event_timestamp
                except:
                    # Try parsing date and time separately
                    if date_str and time_str:
                        try:
                            dt_str = f"{date_str} {time_str}"
                            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                            # Assume UTC if no timezone info
                            dt = dt.replace(tzinfo=timezone.utc)
                            event_timestamp = dt.timestamp()
                            timestamp = event_timestamp
                        except:
                            pass
            
            # Format based on status
            if status in ['FT', 'F']:  # Full Time / Final
                # Completed game
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.strptime(date_str, "%Y-%m-%d")
                        today = datetime.now().date()
                        game_date = dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(dt)}"
                    except:
                        pass
                
                if is_home:
                    formatted = f"{away_abbr} {away_score}-{home_score} @{home_abbr} (F{date_suffix})"
                else:
                    formatted = f"{home_abbr} {home_score}-{away_score} @{away_abbr} (F{date_suffix})"
                
                timestamp = 9999999998  # Final games second to last
                
            elif status in ['NS', 'AP', '']:  # Not Started / Approved / Empty
                # Scheduled game
                if timestamp_str or (date_str and time_str):
                    try:
                        if timestamp_str:
                            dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        else:
                            dt_str = f"{date_str} {time_str}"
                            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                            dt = dt.replace(tzinfo=timezone.utc)
                        
                        local_dt = dt.astimezone()
                        time_str_formatted = self.format_clean_date_time(local_dt)
                        
                        if is_home:
                            formatted = f"{away_abbr} @ {home_abbr} ({time_str_formatted})"
                        else:
                            formatted = f"{home_abbr} @ {away_abbr} ({time_str_formatted})"
                    except:
                        if is_home:
                            formatted = f"{away_abbr} @ {home_abbr} (TBD)"
                        else:
                            formatted = f"{home_abbr} @ {away_abbr} (TBD)"
                        timestamp = 9999999999  # Put TBD games last
                else:
                    if is_home:
                        formatted = f"{away_abbr} @ {home_abbr} (TBD)"
                    else:
                        formatted = f"{home_abbr} @ {away_abbr} (TBD)"
                    timestamp = 9999999999  # Put TBD games last
            else:
                # Other status (live game, postponed, etc.)
                if is_home:
                    formatted = f"{away_abbr} {away_score or '0'}-{home_score or '0'} @{home_abbr} ({status})"
                else:
                    formatted = f"{home_abbr} {home_score or '0'}-{away_score or '0'} @{away_abbr} ({status})"
                timestamp = -1 if status not in ['NS', 'AP'] else 9999999997
            
            return {
                'timestamp': timestamp,
                'event_timestamp': event_timestamp,
                'formatted': formatted,
                'sport': sport,
                'status': status
            }
            
        except Exception as e:
            self.logger.error(f"Error parsing TheSportsDB event: {e}")
            return None
    
    def _get_team_abbreviation_from_name(self, team_name: str) -> str:
        """Extract a short abbreviation from a team name
        
        Uses common city abbreviations for WHL teams.
        """
        if not team_name:
            return 'UNK'
        
        # WHL team abbreviation mappings
        whl_abbreviations = {
            'seattle thunderbirds': 'SEA',
            'portland winterhawks': 'POR',
            'everett silvertips': 'EVE',
            'spokane chiefs': 'SPO',
            'vancouver giants': 'VAN',
            'kamloops blazers': 'KAM',
            'prince george cougars': 'PG',
            'kelowna rockets': 'KEL',
            'tri-city americans': 'TC',
            'wenatchee wild': 'WEN',
            'victoria royals': 'VIC',
            'edmonton oil kings': 'EDM',
            'calgary hitmen': 'CGY',
            'red deer rebels': 'RD',
            'medicine hat tigers': 'MH',
            'lethbridge hurricanes': 'LET',
            'swift current broncos': 'SC',
            'moose jaw warriors': 'MJ',
            'regina pats': 'REG',
            'saskatoon blades': 'SAS',
            'prince albert raiders': 'PA',
            'brandon wheat kings': 'BDN',
            'winnipeg ice': 'WPG',
        }
        
        team_lower = team_name.lower()
        if team_lower in whl_abbreviations:
            return whl_abbreviations[team_lower]
        
        # Try to extract from city name (first word)
        words = team_name.split()
        if len(words) >= 2:
            city = words[0]
            # Use common city abbreviations
            city_abbr = {
                'seattle': 'SEA',
                'portland': 'POR',
                'everett': 'EVE',
                'spokane': 'SPO',
                'vancouver': 'VAN',
                'kamloops': 'KAM',
                'prince': 'PG',  # Prince George (could also be Prince Albert, but PG is more common)
                'kelowna': 'KEL',
                'tri-city': 'TC',
                'tri city': 'TC',
                'tricity': 'TC',
                'wenatchee': 'WEN',
                'victoria': 'VIC',
                'edmonton': 'EDM',
                'calgary': 'CGY',
                'red': 'RD',  # Red Deer
                'medicine': 'MH',  # Medicine Hat
                'lethbridge': 'LET',
                'swift': 'SC',  # Swift Current
                'moose': 'MJ',  # Moose Jaw
                'regina': 'REG',
                'saskatoon': 'SAS',
                'prince albert': 'PA',
                'brandon': 'BDN',
            }
            city_lower = city.lower()
            if city_lower in city_abbr:
                return city_abbr[city_lower]
            
            # Fallback: use first 3 letters of city
            if len(city) >= 3:
                return city[:3].upper()
        
        # Final fallback: use first 3 letters of team name
        return team_name[:3].upper() if len(team_name) >= 3 else team_name.upper()
    
    async def fetch_live_event_data(self, event_id: str, sport: str, league: str) -> Optional[Dict]:
        """Fetch live event data from the scoreboard endpoint for real-time scores
        
        The scoreboard endpoint provides more up-to-date scores for live games than the schedule endpoint.
        We fetch the scoreboard and find the matching event by ID.
        """
        try:
            # Use scoreboard endpoint which has live scores
            url = f"{self.ESPN_BASE_URL}/{sport}/{league}/scoreboard"
            response = requests.get(url, timeout=self.url_timeout)
            response.raise_for_status()
            data = response.json()
            
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
            self.logger.warning(f"Error fetching live event data for {event_id}: {e}")
            return None
    
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
            # Use team schedule endpoint - returns both past and upcoming games
            url = f"{self.ESPN_BASE_URL}/{team_info['sport']}/{team_info['league']}/teams/{team_info['team_id']}/schedule"
            
            # Make API request
            response = requests.get(url, timeout=self.url_timeout)
            response.raise_for_status()
            
            data = response.json()
            events = data.get('events', [])
            
            if not events:
                return []
            
            # Parse all games
            all_games = []
            for event in events:
                game_data = self.parse_game_event_with_timestamp(event, team_info['team_id'], team_info['sport'], team_info['league'])
                if game_data:
                    all_games.append(game_data)
            
            if not all_games:
                return []
            
            # Get current time for comparison
            now = datetime.now(timezone.utc).timestamp()
            
            # Filter to only upcoming games
            upcoming_games = []
            for game in all_games:
                # Skip live games (negative timestamps)
                if game['timestamp'] < 0:
                    continue
                
                game_event_ts = game.get('event_timestamp')
                effective_ts = game_event_ts if game_event_ts is not None else game['timestamp']
                
                # Only include games with valid future timestamps
                if effective_ts is not None and effective_ts > now:
                    upcoming_games.append((effective_ts, game))
            
            # Sort by soonest first
            upcoming_games.sort(key=lambda x: x[0] if x[0] is not None else float('inf'))
            
            # Return all upcoming games (caller will limit by message length)
            return [g for _, g in upcoming_games]
            
        except Exception as e:
            self.logger.error(f"Error fetching team schedule: {e}")
            return []
    
    async def fetch_team_schedule_thesportsdb(self, team_info: Dict[str, str]) -> List[Dict]:
        """Fetch upcoming scheduled games for a team from TheSportsDB"""
        if not self.thesportsdb_client:
            self.logger.error("TheSportsDB client not initialized")
            return []
        
        try:
            team_id = team_info['team_id']
            
            # Fetch next events
            import asyncio
            loop = asyncio.get_event_loop()
            
            next_events = await loop.run_in_executor(
                None,
                lambda: self.thesportsdb_client.get_team_events_next(team_id, limit=10)
            )
            
            # Parse events
            upcoming_games = []
            now = datetime.now(timezone.utc).timestamp()
            
            for event in next_events:
                game_data = self.parse_thesportsdb_event(event, team_id, team_info['sport'], team_info['league'])
                if game_data:
                    game_event_ts = game_data.get('event_timestamp')
                    effective_ts = game_event_ts if game_event_ts is not None else game_data['timestamp']
                    
                    # Only include future games
                    if effective_ts is None or effective_ts > now:
                        upcoming_games.append((effective_ts or float('inf'), game_data))
            
            # Sort by soonest first
            upcoming_games.sort(key=lambda x: x[0] if x[0] is not None else float('inf'))
            
            return [g for _, g in upcoming_games]
            
        except Exception as e:
            self.logger.error(f"Error fetching team schedule from TheSportsDB: {e}")
            return []
    
    async def fetch_team_schedule_formatted(self, team_info: Dict[str, str]) -> Optional[str]:
        """Fetch and format upcoming scheduled games for a team
        
        Returns formatted schedule with as many games as fit in 130 characters.
        """
        games = await self.fetch_team_schedule(team_info)
        if not games:
            return None
        
        # Format games to fit within message limit (130 characters)
        # Use 125 as a buffer to avoid cutting off mid-game
        sport_emoji = self.SPORT_EMOJIS.get(team_info['sport'], '🏆')
        formatted_games = []
        current_length = 0
        max_length = 125  # Leave buffer to avoid cutoff
        
        for game in games:
            # Ensure game['formatted'] doesn't already have an emoji
            game_formatted = game['formatted'].strip()
            # Remove emoji if it's at the start (some games might have it)
            if game_formatted and game_formatted[0] in self.SPORT_EMOJIS.values():
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
            if game_formatted and game_formatted[0] in self.SPORT_EMOJIS.values():
                game_formatted = game_formatted[1:].strip()
            return f"{sport_emoji} {game_formatted[:120]}"
        
        return "\n".join(formatted_games)
    
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
            
            # Check if our team is in this game
            our_team = None
            other_team = None
            
            for competitor in competitors:
                if competitor.get('team', {}).get('id') == team_id:
                    our_team = competitor
                else:
                    other_team = competitor
            
            if not our_team or not other_team:
                return None
            
            # Determine home/away teams for all sports
            home_team = our_team if our_team.get('homeAway') == 'home' else other_team
            away_team = other_team if our_team.get('homeAway') == 'home' else our_team
            home_team_id = home_team.get('team', {}).get('id', '')
            away_team_id = away_team.get('team', {}).get('id', '')
            home_abbreviation = home_team.get('team', {}).get('abbreviation', 'UNK')
            away_abbreviation = away_team.get('team', {}).get('abbreviation', 'UNK')
            home_name = self.get_team_abbreviation(home_team_id, home_abbreviation, sport, league)
            away_name = self.get_team_abbreviation(away_team_id, away_abbreviation, sport, league)
            home_score = self.extract_score(home_team)
            away_score = self.extract_score(away_team)
            
            # For individual team queries, we still want to show our team first
            # but in the correct home/away order for each sport
            if our_team.get('homeAway') == 'home':
                our_team_name = home_name
                other_team_name = away_name
                our_score = home_score
                other_score = away_score
            else:
                our_team_name = away_name
                other_team_name = home_name
                our_score = away_score
                other_score = home_score
            
            # Get game status
            # In schedule endpoint, status is in competition, not event
            status = competition.get('status', event.get('status', {}))
            status_type = status.get('type', {})
            status_name = status_type.get('name', 'UNKNOWN')
            
            # Get timestamp for sorting
            date_str = event.get('date', '')
            timestamp = 0  # Default for sorting
            event_timestamp = None
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    event_timestamp = dt.timestamp()
                    timestamp = event_timestamp
                except:
                    pass
            
            # Format based on game status
            if status_name in ['STATUS_IN_PROGRESS', 'STATUS_FIRST_HALF', 'STATUS_SECOND_HALF', 'STATUS_END_PERIOD']:
                # Game is live - prioritize these (use negative timestamp)
                # STATUS_END_PERIOD means a period just ended but game is still ongoing
                clock = status.get('displayClock', '')
                period = status.get('period', 0)
                is_end_period = (status_name == 'STATUS_END_PERIOD')
                
                # Format period based on sport
                if sport == 'soccer':
                    # For soccer, use displayClock if available (e.g., "90'+5'"), otherwise use half
                    # For soccer, show home team first (traditional soccer format)
                    if clock and clock != '0:00' and clock != "0'":
                        period_str = clock  # Use displayClock directly (e.g., "90'+5'")
                        formatted = f"@{home_name} {home_score}-{away_score} {away_name} ({period_str})"
                    else:
                        period_str = f"{period}H"  # Fallback to half
                        formatted = f"@{home_name} {home_score}-{away_score} {away_name} ({clock} {period_str})"
                elif sport == 'baseball':
                    # Use shortDetail for ongoing baseball games to show top/bottom of inning
                    short_detail = status.get('type', {}).get('shortDetail', '')
                    if short_detail and ('Top' in short_detail or 'Bottom' in short_detail):
                        period_str = short_detail  # e.g., "Top 14th", "Bottom 9th"
                    else:
                        period_str = f"{period}I"  # Fallback to inning number only
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({period_str})"
                elif sport == 'football':
                    period_str = f"Q{period}"  # Quarters
                    if is_end_period:
                        period_str = f"End {period_str}"
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({clock} {period_str})"
                else:
                    period_str = f"P{period}"  # Generic periods (hockey, etc.)
                    if is_end_period:
                        period_str = f"End {period_str}"
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({clock} {period_str})"
                
                timestamp = -1  # Live games first
                
            elif status_name == 'STATUS_SCHEDULED':
                # Game is scheduled
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        time_str = self.format_clean_date_time(local_dt)
                        if sport == 'soccer':
                            formatted = f"@{home_name} vs. {away_name} ({time_str})"
                        else:
                            formatted = f"{away_name} @ {home_name} ({time_str})"
                    except:
                        if sport == 'soccer':
                            formatted = f"@{home_name} vs. {away_name} (TBD)"
                        else:
                            formatted = f"{away_name} @ {home_name} (TBD)"
                        timestamp = 9999999999  # Put TBD games last
                else:
                    if sport == 'soccer':
                        formatted = f"@{home_name} vs. {away_name} (TBD)"
                    else:
                        formatted = f"{away_name} @ {home_name} (TBD)"
                    timestamp = 9999999999  # Put TBD games last
                    
            elif status_name == 'STATUS_HALFTIME':
                # Game is at halftime
                if sport == 'soccer':
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} (HT)"
                else:
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} (HT)"
                timestamp = -2  # Halftime games second priority after live games
            elif status_name == 'STATUS_FULL_TIME':
                # Soccer game is finished - put these last
                # Check if game was played today or on a different day
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        today = datetime.now().date()
                        game_date = local_dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(local_dt)}"
                    except:
                        pass
                formatted = f"@{home_name} {home_score}-{away_score} {away_name} (FT{date_suffix})"
                timestamp = 9999999998  # Final games second to last
            elif status_name == 'STATUS_FINAL_PEN':
                # Soccer game finished in penalty shootout
                # Check if game was played today or on a different day
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        today = datetime.now().date()
                        game_date = local_dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(local_dt)}"
                    except:
                        pass
                
                # Get penalty shootout scores
                home_shootout = self.extract_shootout_score(home_team)
                away_shootout = self.extract_shootout_score(away_team)
                
                # Format with penalty shootout result
                if home_shootout is not None and away_shootout is not None:
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} (FT-PEN {home_shootout}-{away_shootout}{date_suffix})"
                else:
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} (FT-PEN{date_suffix})"
                
                timestamp = 9999999998  # Final games second to last
            elif status_name == 'STATUS_FINAL':
                # Other sports game is finished - put these last
                # Check if game was played today or on a different day
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        today = datetime.now().date()
                        game_date = local_dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(local_dt)}"
                    except:
                        pass
                formatted = f"{away_name} {away_score}-{home_score} @{home_name} (F{date_suffix})"
                timestamp = 9999999998  # Final games second to last
                
            else:
                # Other status
                if sport == 'soccer':
                    formatted = f"@{home_name} {home_score}-{away_score} {away_name} ({status_name})"
                else:
                    formatted = f"{away_name} {away_score}-{home_score} @{home_name} ({status_name})"
                timestamp = 9999999997  # Other statuses third to last
            
            return {
                'timestamp': timestamp,
                'event_timestamp': event_timestamp,
                'formatted': formatted,
                'sport': sport,
                'status': status_name
            }
                
        except Exception as e:
            self.logger.error(f"Error parsing game event with timestamp: {e}")
            return None

    def parse_game_event(self, event: Dict, team_id: str) -> Optional[str]:
        """Parse a game event and return formatted score info"""
        try:
            competitions = event.get('competitions', [])
            if not competitions:
                return None
            
            competition = competitions[0]
            competitors = competition.get('competitors', [])
            
            if len(competitors) != 2:
                return None
            
            # Check if our team is in this game
            our_team = None
            other_team = None
            
            for competitor in competitors:
                if competitor.get('team', {}).get('id') == team_id:
                    our_team = competitor
                else:
                    other_team = competitor
            
            if not our_team or not other_team:
                return None
            
            # Extract team info
            our_team_name = our_team.get('team', {}).get('abbreviation', 'UNK')
            other_team_name = other_team.get('team', {}).get('abbreviation', 'UNK')
            
            # Determine home/away teams
            our_home_away = our_team.get('homeAway', '')
            other_home_away = other_team.get('homeAway', '')
            
            if our_home_away == 'home':
                home_team_name = our_team_name
                away_team_name = other_team_name
            elif other_home_away == 'home':
                home_team_name = other_team_name
                away_team_name = our_team_name
            else:
                # Fallback if homeAway is not available
                home_team_name = other_team_name
                away_team_name = our_team_name
            
            # Get scores
            our_score = self.extract_score(our_team)
            other_score = self.extract_score(other_team)
            
            # Get game status
            status = event.get('status', {})
            status_type = status.get('type', {})
            status_name = status_type.get('name', 'UNKNOWN')
            
            # Format based on game status
            if status_name in ['STATUS_IN_PROGRESS', 'STATUS_FIRST_HALF', 'STATUS_SECOND_HALF']:
                # Game is live
                clock = status.get('displayClock', '')
                period = status.get('period', 0)
                
                # Format period based on sport (need to determine sport from team_info)
                # This is a legacy method, so we'll use a generic approach
                if period <= 2:
                    period_str = f"{period}H"  # Likely soccer (halves)
                elif period <= 4:
                    period_str = f"Q{period}"  # Likely football (quarters)
                else:
                    period_str = f"{period}I"  # Likely baseball (innings)
                
                return f"{our_team_name} {our_score}-{other_score} @{other_team_name} ({clock} {period_str})"
            
            elif status_name == 'STATUS_SCHEDULED':
                # Game is scheduled
                date_str = event.get('date', '')
                if date_str:
                    try:
                        # Parse date and format
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        # Convert to local time (assuming Pacific for Seattle teams)
                        local_dt = dt.astimezone()
                        time_str = self.format_clean_date_time(local_dt)
                        return f"{away_team_name} @ {home_team_name} ({time_str})"
                    except:
                        return f"{away_team_name} @ {home_team_name} (TBD)"
                else:
                    return f"{away_team_name} @ {home_team_name} (TBD)"
            
            elif status_name == 'STATUS_HALFTIME':
                # Game is at halftime
                return f"{our_team_name} {our_score}-{other_score} @{other_team_name} (HT)"
            elif status_name == 'STATUS_FULL_TIME':
                # Soccer game is finished
                # Check if game was played today or on a different day
                date_str = event.get('date', '')
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        today = datetime.now().date()
                        game_date = local_dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(local_dt)}"
                    except:
                        pass
                return f"{our_team_name} {our_score}-{other_score} @{other_team_name} (FT{date_suffix})"
            elif status_name == 'STATUS_FINAL':
                # Other sports game is finished
                # Check if game was played today or on a different day
                date_str = event.get('date', '')
                date_suffix = ""
                if date_str:
                    try:
                        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        local_dt = dt.astimezone()
                        today = datetime.now().date()
                        game_date = local_dt.date()
                        if game_date != today:
                            date_suffix = f", {self.format_clean_date(local_dt)}"
                    except:
                        pass
                return f"{our_team_name} {our_score}-{other_score} @{other_team_name} (F{date_suffix})"
            
            else:
                # Other status
                return f"{our_team_name} {our_score}-{other_score} {other_team_name} ({status_name})"
                
        except Exception as e:
            self.logger.error(f"Error parsing game event: {e}")
            return None
