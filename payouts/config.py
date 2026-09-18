"""Configuration for the staff strikes ModMail plugin.

Replace the placeholder values with the Discord role IDs from your server.
Role IDs must be integers. The order in STAFF_RANKS is lowest to highest.
"""

# The role applied to every member of the staff team.
STAFF_TEAM_ROLE_ID = 1461572126174875886

# A separate role that should count as staff for this plugin.
GAME_ADMIN_ROLE_ID = 1490692440146051092

# Put every rank that participates in the hierarchy here, from lowest to
# highest. The names are used in messages and in the authority checks below.
#
# Example:
# STAFF_RANKS = [
#     {"name": "Trial Moderator", "role_id": 111111111111111111},
#     {"name": "Moderator", "role_id": 222222222222222222},
#     {"name": "Staff Management", "role_id": 333333333333333333},
#     {"name": "Admin", "role_id": 444444444444444444},
#     {"name": "Head Admin", "role_id": 555555555555555555},
# ]
STAFF_RANKS = [
    {"name": "Trial Moderator", "role_id": 1457047936465633381},
    {"name": "Moderator", "role_id": 1457049978030653460},
    {"name": "Senior Moderator", "role_id": 1458421728718880791},
    {"name": "Staff Management", "role_id": 1457039931351367872},
    {"name": "Overseer", "role_id": 1546847847067025509},
    {"name": "Head of Staff", "role_id": 1458892950309441709},
    {"name": "Admin", "role_id": 1424785285782438089},
    {"name": "Head Admin", "role_id": 1272561419061297184},
]

# The lowest rank allowed to issue or remove strikes.
STRIKE_AUTHORITY_RANK = "Staff Management"

# Only these ranks may use .allstrikes.
ALL_STRIKES_AUTHORITY_RANKS = {"Admin", "Head Admin"}

# At three active strikes, all of these staff roles are removed. By default,
# this is every configured staff rank plus STAFF_TEAM_ROLE_ID and
# GAME_ADMIN_ROLE_ID. Add any extra staff-only roles here if needed.
EXTRA_STAFF_ROLE_IDS_TO_REMOVE = set()

# Maximum active strikes before the member loses their staff roles.
MAX_STRIKES = 3
