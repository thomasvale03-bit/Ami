"""Persona switch for the shared Slack worker.

SLACK_PERSONA=amy (default) is AMY Worker / AMYI: read-only AMY queue
lookups, booking-update posts, escalations to #amy-bookings.

SLACK_PERSONA=jacob is Jacob: new-hire promoter onboarding coach.
No AMY queue tools. Intro DM on team_join, #newhire joins, and a first
DM; homework and reason capture; escalations DMed to Tom.
"""
import os

AMY = 'amy'
JACOB = 'jacob'

# Intro copy lives in onboarding.INTRO_TEXT / intro_text (coach intro).


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
    """Coach intro in Tom's voice. Optional first name."""
    from .onboarding import intro_text
    return intro_text(user)
