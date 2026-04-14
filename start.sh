#!/bin/bash
cd /Users/saicharantej/Documents/alfred
source venv/bin/activate
/Users/saicharantej/Documents/alfred/venv/bin/python -m uvicorn main:app --reload
