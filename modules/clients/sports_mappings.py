#!/usr/bin/env python3
"""
Sports Team and League Mappings
Contains team IDs and custom abbreviations for various sports APIs
"""

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
}

# Team mappings for common searches
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
    'arizona cardinals': {'sport': 'football', 'league': 'nfl', 'team_id': '22'},
    'az cardinals': {'sport': 'football', 'league': 'nfl', 'team_id': '22'},
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
    'carolina panthers': {'sport': 'football', 'league': 'nfl', 'team_id': '29'},
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
    
    # NBA Teams
    'hawks': {'sport': 'basketball', 'league': 'nba', 'team_id': '1'},
    'atlanta hawks': {'sport': 'basketball', 'league': 'nba', 'team_id': '1'},
    'celtics': {'sport': 'basketball', 'league': 'nba', 'team_id': '2'},
    'boston celtics': {'sport': 'basketball', 'league': 'nba', 'team_id': '2'},
    'nets': {'sport': 'basketball', 'league': 'nba', 'team_id': '17'},
    'brooklyn nets': {'sport': 'basketball', 'league': 'nba', 'team_id': '17'},
    'hornets': {'sport': 'basketball', 'league': 'nba', 'team_id': '30'},
    'charlotte hornets': {'sport': 'basketball', 'league': 'nba', 'team_id': '30'},
    'bulls': {'sport': 'basketball', 'league': 'nba', 'team_id': '4'},
    'chicago bulls': {'sport': 'basketball', 'league': 'nba', 'team_id': '4'},
    'cavaliers': {'sport': 'basketball', 'league': 'nba', 'team_id': '5'},
    'cavs': {'sport': 'basketball', 'league': 'nba', 'team_id': '5'},
    'cleveland cavaliers': {'sport': 'basketball', 'league': 'nba', 'team_id': '5'},
    'mavericks': {'sport': 'basketball', 'league': 'nba', 'team_id': '6'},
    'mavs': {'sport': 'basketball', 'league': 'nba', 'team_id': '6'},
    'dallas mavericks': {'sport': 'basketball', 'league': 'nba', 'team_id': '6'},
    'nuggets': {'sport': 'basketball', 'league': 'nba', 'team_id': '7'},
    'denver nuggets': {'sport': 'basketball', 'league': 'nba', 'team_id': '7'},
    'pistons': {'sport': 'basketball', 'league': 'nba', 'team_id': '8'},
    'detroit pistons': {'sport': 'basketball', 'league': 'nba', 'team_id': '8'},
    'warriors': {'sport': 'basketball', 'league': 'nba', 'team_id': '9'},
    'golden state warriors': {'sport': 'basketball', 'league': 'nba', 'team_id': '9'},
    'rockets': {'sport': 'basketball', 'league': 'nba', 'team_id': '10'},
    'houston rockets': {'sport': 'basketball', 'league': 'nba', 'team_id': '10'},
    'pacers': {'sport': 'basketball', 'league': 'nba', 'team_id': '11'},
    'indiana pacers': {'sport': 'basketball', 'league': 'nba', 'team_id': '11'},
    'clippers': {'sport': 'basketball', 'league': 'nba', 'team_id': '12'},
    'la clippers': {'sport': 'basketball', 'league': 'nba', 'team_id': '12'},
    'lakers': {'sport': 'basketball', 'league': 'nba', 'team_id': '13'},
    'la lakers': {'sport': 'basketball', 'league': 'nba', 'team_id': '13'},
    'heat': {'sport': 'basketball', 'league': 'nba', 'team_id': '14'},
    'miami heat': {'sport': 'basketball', 'league': 'nba', 'team_id': '14'},
    'bucks': {'sport': 'basketball', 'league': 'nba', 'team_id': '15'},
    'milwaukee bucks': {'sport': 'basketball', 'league': 'nba', 'team_id': '15'},
    'timberwolves': {'sport': 'basketball', 'league': 'nba', 'team_id': '16'},
    'twolves': {'sport': 'basketball', 'league': 'nba', 'team_id': '16'},
    'minnesota timberwolves': {'sport': 'basketball', 'league': 'nba', 'team_id': '16'},
    'pelicans': {'sport': 'basketball', 'league': 'nba', 'team_id': '3'},
    'new orleans pelicans': {'sport': 'basketball', 'league': 'nba', 'team_id': '3'},
    'knicks': {'sport': 'basketball', 'league': 'nba', 'team_id': '18'},
    'new york knicks': {'sport': 'basketball', 'league': 'nba', 'team_id': '18'},
    'magic': {'sport': 'basketball', 'league': 'nba', 'team_id': '19'},
    'orlando magic': {'sport': 'basketball', 'league': 'nba', 'team_id': '19'},
    '76ers': {'sport': 'basketball', 'league': 'nba', 'team_id': '20'},
    'sixers': {'sport': 'basketball', 'league': 'nba', 'team_id': '20'},
    'philadelphia 76ers': {'sport': 'basketball', 'league': 'nba', 'team_id': '20'},
    'suns': {'sport': 'basketball', 'league': 'nba', 'team_id': '21'},
    'phoenix suns': {'sport': 'basketball', 'league': 'nba', 'team_id': '21'},
    'trail blazers': {'sport': 'basketball', 'league': 'nba', 'team_id': '22'},
    'trailblazers': {'sport': 'basketball', 'league': 'nba', 'team_id': '22'},
    'blazers': {'sport': 'basketball', 'league': 'nba', 'team_id': '22'},
    'portland trail blazers': {'sport': 'basketball', 'league': 'nba', 'team_id': '22'},
    'kings': {'sport': 'basketball', 'league': 'nba', 'team_id': '23'},
    'sacramento kings': {'sport': 'basketball', 'league': 'nba', 'team_id': '23'},
    'spurs': {'sport': 'basketball', 'league': 'nba', 'team_id': '24'},
    'san antonio spurs': {'sport': 'basketball', 'league': 'nba', 'team_id': '24'},
    'thunder': {'sport': 'basketball', 'league': 'nba', 'team_id': '25'},
    'okc thunder': {'sport': 'basketball', 'league': 'nba', 'team_id': '25'},
    'oklahoma city thunder': {'sport': 'basketball', 'league': 'nba', 'team_id': '25'},
    'jazz': {'sport': 'basketball', 'league': 'nba', 'team_id': '26'},
    'utah jazz': {'sport': 'basketball', 'league': 'nba', 'team_id': '26'},
    'wizards': {'sport': 'basketball', 'league': 'nba', 'team_id': '27'},
    'washington wizards': {'sport': 'basketball', 'league': 'nba', 'team_id': '27'},
    'raptors': {'sport': 'basketball', 'league': 'nba', 'team_id': '28'},
    'toronto raptors': {'sport': 'basketball', 'league': 'nba', 'team_id': '28'},
    'grizzlies': {'sport': 'basketball', 'league': 'nba', 'team_id': '29'},
    'memphis grizzlies': {'sport': 'basketball', 'league': 'nba', 'team_id': '29'},
    
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

# Helper to check for women's leagues
WOMENS_LEAGUES = {
    ('basketball', 'wnba'),
    ('soccer', 'usa.nwsl'),
    ('hockey', 'pwhl')
}

# League mappings for league-wide queries
LEAGUE_MAPPINGS = {
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
    
    # NHL
    'nhl': {'sport': 'hockey', 'league': 'nhl'},
    'hockey': {'sport': 'hockey', 'league': 'nhl'},
    
    # PWHL
    'pwhl': {'sport': 'hockey', 'league': 'pwhl'},
    
    # WHL
    'whl': {'sport': 'hockey', 'league': 'whl', 'league_id': '5160', 'api_source': 'thesportsdb'},
    
    # MLS
    'mls': {'sport': 'soccer', 'league': 'usa.1'},
    'soccer': {'sport': 'soccer', 'league': 'usa.1'},
    
    # NWSL
    'nwsl': {'sport': 'soccer', 'league': 'usa.nwsl'},
    
    # Premier League
    'epl': {'sport': 'soccer', 'league': 'eng.1'},
    'premier league': {'sport': 'soccer', 'league': 'eng.1'}
}

def format_clean_date_time(dt) -> str:
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

def format_clean_date(dt) -> str:
    """Format date without leading zeros"""
    month = dt.month
    day = dt.day
    return f"{month}/{day}"

def get_team_abbreviation_from_name(team_name: str) -> str:
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
    
    # Try to extract from city name (first one or two words)
    words = team_name.lower().split()
    if len(words) >= 2:
        # Check for two-word cities first
        two_word_city = f"{words[0]} {words[1]}"
        # Use common city abbreviations
        city_abbr = {
            'seattle': 'SEA',
            'portland': 'POR',
            'everett': 'EVE',
            'spokane': 'SPO',
            'vancouver': 'VAN',
            'kamloops': 'KAM',
            'prince george': 'PG',
            'prince albert': 'PA',
            'kelowna': 'KEL',
            'tri-city': 'TC',
            'tri city': 'TC',
            'tricity': 'TC',
            'wenatchee': 'WEN',
            'victoria': 'VIC',
            'edmonton': 'EDM',
            'calgary': 'CGY',
            'red deer': 'RD',
            'medicine hat': 'MH',
            'lethbridge': 'LET',
            'swift current': 'SC',
            'moose jaw': 'MJ',
            'regina': 'REG',
            'saskatoon': 'SAS',
            'brandon': 'BDN',
            'winnipeg': 'WPG',
        }
        
        if two_word_city in city_abbr:
            return city_abbr[two_word_city]
            
        # Then check for one-word cities
        city = words[0]
        if city in city_abbr:
            return city_abbr[city]
        
        # Fallback for one-word city: use first 3 letters
        if len(city) >= 3:
            return city[:3].upper()
    
    # Final fallback: use first 3 letters of team name
    return team_name[:3].upper() if len(team_name) >= 3 else team_name.upper()

def is_womens_league(sport: str, league: str) -> bool:
    """Check if the league is a women's league"""
    return (sport, league) in WOMENS_LEAGUES

def is_soccer(sport: str) -> bool:
    """Check if the sport is soccer"""
    return sport.lower() == 'soccer'

def get_team_abbreviation(team_id: str, team_abbreviation: str, sport: str, league: str) -> str:
    """Get team abbreviation, using -W suffix only for women's leagues"""
    if is_womens_league(sport, league):
        return WOMENS_TEAM_ABBREVIATIONS.get(team_id, team_abbreviation)
    return team_abbreviation

# Export all public functions and constants
__all__ = [
    'SPORT_EMOJIS', 'WOMENS_TEAM_ABBREVIATIONS', 'TEAM_MAPPINGS',
    'WOMENS_LEAGUES', 'LEAGUE_MAPPINGS', 'is_womens_league', 'is_soccer',
    'get_team_abbreviation', 'get_team_abbreviation_from_name',
    'format_clean_date_time', 'format_clean_date'
]
