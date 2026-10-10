"""Server-specific settings for the Staff Strikes ModMail plugin.

Role IDs and user IDs are integers. STAFF_RANKS must be ordered from lowest
rank to highest rank.
"""

STAFF_TEAM_ROLE_ID = 1461572126174875886
NOTIFICATION_USER_ID = 1272561419061297184
# The role assigned to Game Admin members; it does not grant strike authority.
GAME_ADMIN_ROLE_ID = 1490692440146051092
# Separate role whose holders can issue/remove Game Admin-scoped strikes.
GAME_ADMIN_MANAGER_ROLE_ID = 1555214047161618462

STAFF_RANKS = [
    {"name": "Trial Moderator", "role_id": 1457047936465633381},
    {"name": "Moderator", "role_id": 1457049978030653460},
    {"name": "Senior Moderator", "role_id": 1458421728718880791},
    {"name": "Staff Management", "role_id": 1457039931351367872},
    {"name": "Overseer", "role_id": 1546847847067025509},
    {"name": "Head of Staff", "role_id": 1458892950309441709},
    {"name": "Admin", "role_id": 1424785285782438089},
    {"name": "Head Admin", "role_id": 1484601981006774423},
]

# The lowest rank that can manage strikes for ranked staff.
STRIKE_AUTHORITY_RANK = "Staff Management"

# Only holders of one of these exact ranks may use `allstrikes`.
ALL_STRIKES_AUTHORITY_RANKS = {"Admin", "Head Admin"}

# Extra staff-only roles removed after a member reaches MAX_STRIKES.
EXTRA_STAFF_ROLE_IDS_TO_REMOVE = set()
MAX_STRIKES = 3

# A strike recipient can acknowledge a strike during this period.
# Increase or decrease this to match your server's policy.
ACKNOWLEDGEMENT_HOURS = 24

# How often the plugin checks for missed acknowledgements.
ACKNOWLEDGEMENT_CHECK_MINUTES = 5
