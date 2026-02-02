import os

# =========================================
# Configuration du bot Telegram de prédiction Baccarat
# =========================================

# === CREDENTIALS TELEGRAM (from environment variables) ===
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))

# === IDs DES CANAUX TELEGRAM ===
# Source 1 : Canal principal avec les résultats
SOURCE_CHANNEL_ID = int(os.getenv('SOURCE_CHANNEL_ID', '-1002682552255'))

# Source 2 : Canal des statistiques
SOURCE_CHANNEL_2_ID = int(os.getenv('SOURCE_CHANNEL_2_ID', '-1003309666471'))

# Canal où envoyer les prédictions
PREDICTION_CHANNEL_ID = int(os.getenv('PREDICTION_CHANNEL_ID', '-1003554569009'))

# === CONFIGURATION SERVEUR ===
# Port for the web server
PORT = int(os.getenv('PORT', '5000'))

# === LOGIQUE DE PREDICTION ===
# Mapping des costumes miroirs : ♦️<->♠️ et ❤️<->♣️
SUIT_MAPPING = {
    '♦': '♠',  # Carreau ↔ Pique
    '♠': '♦',
    '♥': '♣',  # Cœur ↔ Trèfle
    '♣': '♥',
}

# Liste de tous les costumes
ALL_SUITS = ['♠', '♥', '♦', '♣']

# Affichage des emojis pour les messages
SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}
