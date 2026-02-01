# =========================================
# Configuration du bot Telegram de prédiction Baccarat
# ⚠️ ATTENTION : Secrets en dur - Ne pas committer dans un dépôt public
# =========================================

# === CREDENTIALS TELEGRAM (secrets) ===
API_ID = 29177661
API_HASH = "a8639172fa8d35dbfd8ea46286d349ab"
BOT_TOKEN = "7663403310:AAHEmW-FzB1hvV9_FXTJxcdGt_hjrc3dJSk"
ADMIN_ID = 1190237801

# === MODE DEPLOIEMENT ===
RENDER_DEPLOYMENT = True  # Passer à True pour Render.com

# === IDs DES CANAUX TELEGRAM ===
# Source 1 : Canal principal avec les résultats
SOURCE_CHANNEL_ID = -1002682552255

# Source 2 : Canal des statistiques
SOURCE_CHANNEL_2_ID = -1003216148681

# Canal où envoyer les prédictions
PREDICTION_CHANNEL_ID = -1003554569009

# === CONFIGURATION SERVEUR ===
# Port pour Render.com (obligatoire)
PORT = 10000

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
