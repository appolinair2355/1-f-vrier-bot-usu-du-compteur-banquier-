# Telegram Baccarat Prediction Bot

## Overview
A Telegram bot that monitors Telegram channels for game statistics and makes predictions based on card suit patterns. The bot uses the Telethon library to interact with the Telegram API and includes a simple web server for health checks.

## Tech Stack
- **Language**: Python 3.12
- **Telegram Library**: Telethon
- **Web Framework**: aiohttp (for health check server)
- **Dependencies**: telethon, aiohttp, python-dotenv, pyyaml, openpyxl

## Project Structure
```
.
├── main.py          # Main bot logic and web server
├── config.py        # Configuration (reads from environment variables)
├── requirements.txt # Python dependencies
└── .gitignore       # Git ignore rules
```

## Required Environment Variables
The bot requires the following secrets to be set:
- `API_ID` - Telegram API ID (from my.telegram.org)
- `API_HASH` - Telegram API Hash (from my.telegram.org)
- `BOT_TOKEN` - Bot token from @BotFather
- `ADMIN_ID` - Telegram user ID for admin access

Optional environment variables:
- `SOURCE_CHANNEL_ID` - Source channel 1 ID
- `SOURCE_CHANNEL_2_ID` - Source channel 2 ID (stats)
- `PREDICTION_CHANNEL_ID` - Channel where predictions are sent
- `PORT` - Web server port (default: 5000)
- `TELEGRAM_SESSION` - Session string for user authentication

## Running the Bot
The bot is configured to run via the "Telegram Bot" workflow which executes `python main.py`.

## Features
- Monitors Telegram channels for game statistics
- Predicts card suits based on statistical patterns
- Sends predictions to a designated channel
- Supports admin commands (/status, /help, /set_a)
- Includes a health check web server on port 5000
