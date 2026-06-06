"""Capital-city lookup for bare country / US-state inputs to the rain command.

Self-contained — neither ``pycountry`` nor ``us`` is installed in production, so
this carries its own data. When a user gives only a region name (e.g. ``!rain
france``), the rain command resolves to that region's capital and appends a
short heads-up. Keyed by lowercased name; the value is ``(capital, region)``
used to build a geocoder query like ``"Paris, France"`` or ``"Austin, TX"``.

Lives outside ``modules/commands/`` so the plugin loader (which globs
``commands/*.py`` for command classes) never tries to load it as a command.
"""
from typing import Optional

# Short warning appended when a bare region defaults to its capital. ~71 bytes —
# fits the 160-byte channel budget alongside any nowcast line (verified).
REGION_DEFAULT_NOTE = "⚠️ no city given — showing capital; try a city for detail"

# US state name -> (capital, abbreviation). Keyed by the full state name only;
# bare 2-letter abbreviations are too ambiguous ("in", "or", "la", "me") to map.
US_STATE_CAPITALS = {
    "alabama": ("Montgomery", "AL"), "alaska": ("Juneau", "AK"),
    "arizona": ("Phoenix", "AZ"), "arkansas": ("Little Rock", "AR"),
    "california": ("Sacramento", "CA"), "colorado": ("Denver", "CO"),
    "connecticut": ("Hartford", "CT"), "delaware": ("Dover", "DE"),
    "florida": ("Tallahassee", "FL"), "georgia": ("Atlanta", "GA"),
    "hawaii": ("Honolulu", "HI"), "idaho": ("Boise", "ID"),
    "illinois": ("Springfield", "IL"), "indiana": ("Indianapolis", "IN"),
    "iowa": ("Des Moines", "IA"), "kansas": ("Topeka", "KS"),
    "kentucky": ("Frankfort", "KY"), "louisiana": ("Baton Rouge", "LA"),
    "maine": ("Augusta", "ME"), "maryland": ("Annapolis", "MD"),
    "massachusetts": ("Boston", "MA"), "michigan": ("Lansing", "MI"),
    "minnesota": ("Saint Paul", "MN"), "mississippi": ("Jackson", "MS"),
    "missouri": ("Jefferson City", "MO"), "montana": ("Helena", "MT"),
    "nebraska": ("Lincoln", "NE"), "nevada": ("Carson City", "NV"),
    "new hampshire": ("Concord", "NH"), "new jersey": ("Trenton", "NJ"),
    "new mexico": ("Santa Fe", "NM"), "new york": ("Albany", "NY"),
    "north carolina": ("Raleigh", "NC"), "north dakota": ("Bismarck", "ND"),
    "ohio": ("Columbus", "OH"), "oklahoma": ("Oklahoma City", "OK"),
    "oregon": ("Salem", "OR"), "pennsylvania": ("Harrisburg", "PA"),
    "rhode island": ("Providence", "RI"), "south carolina": ("Columbia", "SC"),
    "south dakota": ("Pierre", "SD"), "tennessee": ("Nashville", "TN"),
    "texas": ("Austin", "TX"), "utah": ("Salt Lake City", "UT"),
    "vermont": ("Montpelier", "VT"), "virginia": ("Richmond", "VA"),
    "washington": ("Olympia", "WA"), "west virginia": ("Charleston", "WV"),
    "wisconsin": ("Madison", "WI"), "wyoming": ("Cheyenne", "WY"),
}

# Bare state names that almost always mean the city, not the state. Nominatim
# resolves these to the city (NYC, Washington DC) on its own, so let them.
STATE_AS_CITY = {"new york", "washington"}

# Country / common-alias -> (capital, country name for the geocoder query).
WORLD_CAPITALS = {
    # North & Central America
    "united states": ("Washington", "United States"), "usa": ("Washington", "United States"),
    "america": ("Washington", "United States"), "canada": ("Ottawa", "Canada"),
    "mexico": ("Mexico City", "Mexico"), "guatemala": ("Guatemala City", "Guatemala"),
    "cuba": ("Havana", "Cuba"), "jamaica": ("Kingston", "Jamaica"),
    "haiti": ("Port-au-Prince", "Haiti"), "dominican republic": ("Santo Domingo", "Dominican Republic"),
    "panama": ("Panama City", "Panama"), "costa rica": ("San Jose", "Costa Rica"),
    "honduras": ("Tegucigalpa", "Honduras"), "el salvador": ("San Salvador", "El Salvador"),
    "nicaragua": ("Managua", "Nicaragua"), "belize": ("Belmopan", "Belize"),
    # South America
    "colombia": ("Bogota", "Colombia"), "venezuela": ("Caracas", "Venezuela"),
    "ecuador": ("Quito", "Ecuador"), "peru": ("Lima", "Peru"),
    "brazil": ("Brasilia", "Brazil"), "bolivia": ("La Paz", "Bolivia"),
    "chile": ("Santiago", "Chile"), "argentina": ("Buenos Aires", "Argentina"),
    "uruguay": ("Montevideo", "Uruguay"), "paraguay": ("Asuncion", "Paraguay"),
    # Europe
    "france": ("Paris", "France"), "germany": ("Berlin", "Germany"),
    "italy": ("Rome", "Italy"), "spain": ("Madrid", "Spain"),
    "portugal": ("Lisbon", "Portugal"), "united kingdom": ("London", "United Kingdom"),
    "uk": ("London", "United Kingdom"), "britain": ("London", "United Kingdom"),
    "great britain": ("London", "United Kingdom"), "england": ("London", "United Kingdom"),
    "scotland": ("Edinburgh", "United Kingdom"), "wales": ("Cardiff", "United Kingdom"),
    "ireland": ("Dublin", "Ireland"), "netherlands": ("Amsterdam", "Netherlands"),
    "holland": ("Amsterdam", "Netherlands"), "belgium": ("Brussels", "Belgium"),
    "luxembourg": ("Luxembourg", "Luxembourg"), "switzerland": ("Bern", "Switzerland"),
    "austria": ("Vienna", "Austria"), "poland": ("Warsaw", "Poland"),
    "czechia": ("Prague", "Czechia"), "czech republic": ("Prague", "Czechia"),
    "slovakia": ("Bratislava", "Slovakia"), "hungary": ("Budapest", "Hungary"),
    "romania": ("Bucharest", "Romania"), "bulgaria": ("Sofia", "Bulgaria"),
    "greece": ("Athens", "Greece"), "sweden": ("Stockholm", "Sweden"),
    "norway": ("Oslo", "Norway"), "denmark": ("Copenhagen", "Denmark"),
    "finland": ("Helsinki", "Finland"), "iceland": ("Reykjavik", "Iceland"),
    "russia": ("Moscow", "Russia"), "ukraine": ("Kyiv", "Ukraine"),
    "belarus": ("Minsk", "Belarus"), "croatia": ("Zagreb", "Croatia"),
    "serbia": ("Belgrade", "Serbia"), "slovenia": ("Ljubljana", "Slovenia"),
    "albania": ("Tirana", "Albania"), "estonia": ("Tallinn", "Estonia"),
    "latvia": ("Riga", "Latvia"), "lithuania": ("Vilnius", "Lithuania"),
    "monaco": ("Monaco", "Monaco"), "malta": ("Valletta", "Malta"),
    "cyprus": ("Nicosia", "Cyprus"),
    # Middle East & Central Asia
    "turkey": ("Ankara", "Turkey"), "turkiye": ("Ankara", "Turkey"),
    "israel": ("Jerusalem", "Israel"), "jordan": ("Amman", "Jordan"),
    "lebanon": ("Beirut", "Lebanon"), "syria": ("Damascus", "Syria"),
    "iraq": ("Baghdad", "Iraq"), "iran": ("Tehran", "Iran"),
    "saudi arabia": ("Riyadh", "Saudi Arabia"), "uae": ("Abu Dhabi", "United Arab Emirates"),
    "united arab emirates": ("Abu Dhabi", "United Arab Emirates"), "qatar": ("Doha", "Qatar"),
    "kuwait": ("Kuwait City", "Kuwait"), "oman": ("Muscat", "Oman"),
    "yemen": ("Sanaa", "Yemen"), "kazakhstan": ("Astana", "Kazakhstan"),
    "uzbekistan": ("Tashkent", "Uzbekistan"), "afghanistan": ("Kabul", "Afghanistan"),
    # South & East Asia
    "china": ("Beijing", "China"), "japan": ("Tokyo", "Japan"),
    "south korea": ("Seoul", "South Korea"), "korea": ("Seoul", "South Korea"),
    "north korea": ("Pyongyang", "North Korea"), "india": ("New Delhi", "India"),
    "pakistan": ("Islamabad", "Pakistan"), "bangladesh": ("Dhaka", "Bangladesh"),
    "sri lanka": ("Colombo", "Sri Lanka"), "nepal": ("Kathmandu", "Nepal"),
    "thailand": ("Bangkok", "Thailand"), "vietnam": ("Hanoi", "Vietnam"),
    "cambodia": ("Phnom Penh", "Cambodia"), "laos": ("Vientiane", "Laos"),
    "myanmar": ("Naypyidaw", "Myanmar"), "burma": ("Naypyidaw", "Myanmar"),
    "malaysia": ("Kuala Lumpur", "Malaysia"), "singapore": ("Singapore", "Singapore"),
    "indonesia": ("Jakarta", "Indonesia"), "philippines": ("Manila", "Philippines"),
    "mongolia": ("Ulaanbaatar", "Mongolia"), "taiwan": ("Taipei", "Taiwan"),
    # Africa
    "egypt": ("Cairo", "Egypt"), "morocco": ("Rabat", "Morocco"),
    "algeria": ("Algiers", "Algeria"), "tunisia": ("Tunis", "Tunisia"),
    "libya": ("Tripoli", "Libya"), "nigeria": ("Abuja", "Nigeria"),
    "ghana": ("Accra", "Ghana"), "kenya": ("Nairobi", "Kenya"),
    "ethiopia": ("Addis Ababa", "Ethiopia"), "tanzania": ("Dodoma", "Tanzania"),
    "uganda": ("Kampala", "Uganda"), "south africa": ("Pretoria", "South Africa"),
    "zimbabwe": ("Harare", "Zimbabwe"), "zambia": ("Lusaka", "Zambia"),
    "angola": ("Luanda", "Angola"), "senegal": ("Dakar", "Senegal"),
    "cameroon": ("Yaounde", "Cameroon"), "sudan": ("Khartoum", "Sudan"),
    # Oceania
    "australia": ("Canberra", "Australia"), "new zealand": ("Wellington", "New Zealand"),
    "fiji": ("Suva", "Fiji"), "papua new guinea": ("Port Moresby", "Papua New Guinea"),
}


def region_capital_query(location: Optional[str]) -> Optional[str]:
    """Return a ``"Capital, Region"`` geocoder query for a bare country or US
    state, else ``None``.

    Case-insensitive and whitespace-normalized. A comma in the input means the
    user already qualified a city ("Paris, France"), so it's not a bare region.
    City-dominant state names (New York, Washington) are excluded so they keep
    resolving to the city.
    """
    if not location:
        return None
    key = " ".join(location.strip().lower().split())
    if not key or "," in location:
        return None
    if key in US_STATE_CAPITALS and key not in STATE_AS_CITY:
        capital, abbr = US_STATE_CAPITALS[key]
        return f"{capital}, {abbr}"
    if key in WORLD_CAPITALS:
        capital, country = WORLD_CAPITALS[key]
        return f"{capital}, {country}"
    return None
