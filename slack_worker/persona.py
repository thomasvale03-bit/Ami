"""Persona switch for the shared Slack worker.

SLACK_PERSONA=amy (default) is AMY Worker / AMYI: read-only AMY queue
lookups, booking-update posts, escalations to #amy-bookings.

SLACK_PERSONA=jacob is Jacob: new-hire promoter onboarding knowledge bot.
No AMY queue tools, a welcome DM on team_join, escalations DMed to Tom.
"""
import os

AMY = 'amy'
JACOB = 'jacob'

WELCOME_TEXT = (
    "Hey, welcome to Playmaker. First things: get familiar with how we operate, "
    "review playmakerentertainment.com, and learn how guest lists, tickets, and "
    "tables work — especially how promoter attribution works. When you sign a "
    "client up, have them go through the Playmaker site, pick the right venue "
    "and date, and put your own first and last name in the Promoter fields on "
    "that same form. That's how you get counted for the guest list. Counted "
    "doesn't mean guaranteed check-in credit. If anything's unclear, ask me here."
)


def name():
    raw = (os.environ.get('SLACK_PERSONA') or 'amy').strip().lower()
    if raw in ('jacob', 'jac', 'onboarding'):
        return JACOB
    return AMY


def is_jacob():
    return name() == JACOB


def is_amy():
    return not is_jacob()


def welcome_text(user=None):
    """Short first-steps welcome in Tom's voice. Optional first name."""
    user = user if isinstance(user, dict) else {}
    profile = user.get('profile') if isinstance(user.get('profile'), dict) else {}
    first = (profile.get('first_name') or '').strip()
    if not first:
        real = (user.get('real_name') or user.get('display_name') or profile.get('display_name') or '').strip()
        first = real.split()[0] if real else ''
    if first and first.lower() not in ('slackbot', 'jacob'):
        rest = WELCOME_TEXT[len("Hey, welcome to Playmaker. "):]
        return f"Hey {first}, welcome to Playmaker. {rest}"
    return WELCOME_TEXT
