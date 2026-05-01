# Emolink: Discord Emoji & Sticker CDN

Emolink is a high-performance Discord bot designed to bridge the gap between Discord's internal assets and external content delivery. It allows users to retrieve direct CDN links for emojis and stickers, providing various size options and handling complex format conversions (like APNG to GIF) for maximum compatibility.

## Features

- **Emoji CDN Delivery:** Get direct, high-quality links to any custom Discord emoji.
- **Sticker Support:** Full support for Discord stickers, including static (WebP) and animated (APNG/Lottie).
- **Dynamic Resizing:** Interactive UI buttons to request emojis/stickers in specific sizes (24px, 48px, 128px, etc.).
- **Format Conversion:** Automatically converts animated APNG stickers to GIF for better embed compatibility in external apps.
- **Persistence:** Uses Firebase and SQLite to ensure interactive menus remain functional even after bot restarts.
- **Graceful Handling:** Robust shutdown logic and rate-limit protection.

## Setup Instructions

### 1. Prerequisites
- Python 3.10 or higher
- A Discord Bot Token ([Discord Developer Portal](https://discord.com/developers/applications))
- A Firebase Project (for persistent view data)

### 2. Installation
```bash
# Clone the repository
git clone https://github.com/suaibulislam/emolink.git
cd emolink

# Install dependencies
pip install -r requirements.txt
```

### 3. Configuration
Copy the `.env.example` file to `.env` and fill in your credentials:
```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Your Discord bot token. |
| `GUILD_ID` | The primary server ID where the bot will operate. |
| `BACKEND_CHANNEL_ID` | A private channel ID where the bot stores processed stickers. |
| `FIREBASE_PROJECT_ID` | Your Firebase project identifier. |

### 4. Running the Bot
```bash
python bot.py
```

## Project Structure

- `bot.py`: The core bot logic (Monolithic for performance).
- `requirements.txt`: Project dependencies.
- `.env`: (Not tracked) Local environment configuration.
- `firebase-credentials.json`: (Not tracked) Firebase service account key.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
