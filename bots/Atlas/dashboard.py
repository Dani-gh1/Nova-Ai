#!/usr/bin/env python3
"""Start kun dashboardet (læser state.json og data/desk.sqlite)."""
import config
from desk.dashboard import serve

if __name__ == "__main__":
    serve(config)
