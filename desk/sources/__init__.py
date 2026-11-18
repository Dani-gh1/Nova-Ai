"""Offentlige, nøglefri datakilder."""
from . import polymarket, kalshi

SOURCES = {"polymarket": polymarket, "kalshi": kalshi}


def get(platform: str):
    return SOURCES[platform]
