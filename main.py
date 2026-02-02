import os
import asyncio
import re
import logging
import sys
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, SOURCE_CHANNEL_2_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY
)

# --- Configuration et Initialisation ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Vérifications minimales
if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, SOURCE_CHANNEL_2={SOURCE_CHANNEL_2_ID}, PREDICTION_CHANNEL={PREDICTION_CHANNEL_ID}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# --- Variables Globales ---
pending_predictions = {}
queued_predictions = {}
recent_games = {}
processed_messages = set()
last_transferred_game = None
current_game_number = 0
last_source_game_number = 0

# Variables pour la logique de blocage
suit_consecutive_counts = {}
suit_results_history = {}
suit_block_until = {}
last_predicted_suit = None

MAX_PENDING_PREDICTIONS = 5
PROXIMITY_THRESHOLD = 3
USER_A = 1

source_channel_ok = False
prediction_channel_ok = False
transfer_enabled = True

# --- Fonctions d'Analyse ---
def extract_game_number(message: str):
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def parse_stats_message(message: str):
    stats = {}
    # Normalisation complète pour uniformiser les émojis
    text = message.replace('♠️', '♠').replace('♥️', '♥').replace('♦️', '♦').replace('♣️', '♣')
    text = text.replace('❤️', '♥').replace('❤', '♥')
    
    # Stratégie de secours : chercher le premier nombre qui suit chaque symbole
    # même s'il y a beaucoup de texte entre les deux (ex: dans un tableau)
    for suit in ['♠', '♥', '♦', '♣']:
        # On cherche le symbole, puis on scanne jusqu'au premier chiffre
        # re.S permet à '.' de matcher les retours à la ligne
        pattern = rf'{suit}.*?(\d+)'
        match = re.search(pattern, text, re.S)
        if match:
            stats[suit] = int(match.group(1))
            
    if stats and len(stats) == 4:
        logger.info(f"✅ Stats extraites avec succès: {stats}")
    elif stats:
        logger.warning(f"⚠️ Stats incomplètes: {stats}")
    else:
        logger.warning(f"❌ Échec total d'extraction. Message reçu: {text[:200]}...")
        
    return stats

def extract_parentheses_groups(message: str):
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(group_str: str) -> str:
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_suits_in_group(group_str: str):
    normalized = normalize_suits(group_str)
    return [s for s in ALL_SUITS if s in normalized]

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False

def get_predicted_suit(missing_suit: str) -> str:
    return SUIT_MAPPING.get(missing_suit, missing_suit)

# --- Logique de Prédiction ---
async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None):
    try:
        if rattrapage > 0:
            pending_predictions[target_game] = {
                'message_id': 0,
                'suit': predicted_suit,
                'base_game': base_game,
                'status': '🔮',
                'rattrapage': rattrapage,
                'original_game': original_game,
                'created_at': datetime.now().isoformat()
            }
            logger.info(f"Rattrapage {rattrapage} actif pour #{target_game} (Original #{original_game})")
            return 0

        # NOUVEAU FORMAT
        prediction_msg = f"""🎮 joueur №{target_game}
   125→⚜️ Couleur de la carte:{SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
   126→🎰 Poursuite deux jeux(🔰+3)
   127→🗯️ Résultats :⏳"""
        msg_id = 0

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée au canal {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction: {e}")
        else:
            logger.warning(f"⚠️ Canal de prédiction non accessible")

        pending_predictions[target_game] = {
            'message_id': msg_id,
            'suit': predicted_suit,
            'base_game': base_game,
            'status': '🔮',
            'check_count': 0,
            'rattrapage': 0,
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Prédiction active: Jeu #{target_game} - {predicted_suit}")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

def queue_prediction(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None):
    if target_game in queued_predictions or (target_game in pending_predictions and rattrapage == 0):
        return False

    queued_predictions[target_game] = {
        'target_game': target_game,
        'predicted_suit': predicted_suit,
        'base_game': base_game,
        'rattrapage': rattrapage,
        'original_game': original_game,
        'queued_at': datetime.now().isoformat()
    }
    logger.info(f"📋 Prédiction #{target_game} mise en file d'attente (Rattrapage {rattrapage})")
    return True

async def check_and_send_queued_predictions(current_game: int):
    global current_game_number
    current_game_number = current_game

    sorted_queued = sorted(queued_predictions.keys())

    for target_game in sorted_queued:
        pred_data = queued_predictions.pop(target_game)
        await send_prediction_to_channel(
            pred_data['target_game'],
            pred_data['predicted_suit'],
            pred_data['base_game'],
            pred_data.get('rattrapage', 0),
            pred_data.get('original_game')
        )

async def update_prediction_status(game_number: int, new_status: str):
    global suit_consecutive_counts, suit_results_history, suit_block_until, last_predicted_suit
    
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['suit']

        # NOUVEAU FORMAT
        updated_msg = f"""🎮 joueur №{game_number}
⚜️ Couleur de la carte:{SUIT_DISPLAY.get(suit, suit)}
🎰 Poursuite deux jeux(🔰+3)
🗯️ Résultats :{new_status}"""

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour: {e}")

        # --- LOGIQUE DE GESTION DES RÉSULTATS ---
        
        if suit not in suit_results_history:
            suit_results_history[suit] = []
        
        suit_results_history[suit].append(new_status)
        if len(suit_results_history[suit]) > 3:
            suit_results_history[suit].pop(0)
        
        pred['status'] = new_status
        
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣', '❌']:
            del pending_predictions[game_number]
            
            # Vérifier si on a 3 résultats
            if len(suit_results_history[suit]) == 3:
                logger.info(f"3 résultats pour {suit}: {suit_results_history[suit]}")
                
                # CAS 1 : ❌ détecté
                if '❌' in suit_results_history[suit]:
                    logger.info(f"❌ détecté pour {suit} → Re-lancement immédiat")
                    
                    # Lancer immédiatement
                    if current_game_number > 0:
                        target_game = current_game_number + 1
                        logger.info(f"Re-lancement {suit} au jeu #{target_game}")
                        # BYPASS le blocage
                        await send_prediction_to_channel(target_game, suit, current_game_number)
                    
                    # Puis bloquer
                    block_until = datetime.now() + timedelta(minutes=5)
                    suit_block_until[suit] = block_until
                    suit_consecutive_counts[suit] = 0
                    suit_results_history[suit] = []
                    logger.info(f"{suit} bloqué jusqu'à {block_until}")
                
                # CAS 2 : 3 succès
                elif all('✅' in result for result in suit_results_history[suit]):
                    logger.info(f"3 succès pour {suit} → Blocage 5min")
                    block_until = datetime.now() + timedelta(minutes=5)
                    suit_block_until[suit] = block_until
                    suit_consecutive_counts[suit] = 0
                    suit_results_history[suit] = []
                    logger.info(f"{suit} bloqué jusqu'à {block_until}")

        return True
    except Exception as e:
        logger.error(f"Erreur update_status: {e}")
        return False

async def check_prediction_result(game_number: int, second_group: str):
    """Vérifie les résultats selon la séquence ✅0️⃣, ✅1️⃣, ✅2️⃣, ✅3️⃣ ou ❌."""
    # 1. Vérification pour le jeu actuel (Cible N)
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            if has_suit_in_group(second_group, target_suit):
                await update_prediction_status(game_number, '✅0️⃣')
                return
            else:
                # Échec N, on lance le rattrapage 1 pour N+1
                next_target = game_number + 1
                queue_prediction(next_target, target_suit, pred['base_game'], rattrapage=1, original_game=game_number)
                logger.info(f"Échec # {game_number}, Rattrapage 1 planifié pour #{next_target}")

    # 2. Vérification pour les rattrapages (N-1, N-2, N-3)
    for target_game, pred in list(pending_predictions.items()):
        if target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game', target_game - pred['rattrapage'])
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            
            if has_suit_in_group(second_group, target_suit):
                await update_prediction_status(original_game, f'✅{rattrapage_actuel}️⃣')
                if target_game != original_game:
                    del pending_predictions[target_game]
                return
            else:
                if rattrapage_actuel < 3:
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    queue_prediction(next_target, target_suit, pred['base_game'], rattrapage=next_rattrapage, original_game=original_game)
                    logger.info(f"Échec rattrapage {rattrapage_actuel}, Rattrapage {next_rattrapage} planifié pour #{next_target}")
                    del pending_predictions[target_game]
                else:
                    await update_prediction_status(original_game, '❌')
                    if target_game != original_game:
                        del pending_predictions[target_game]
                    logger.info(f"Échec final pour #{original_game} après 3 rattrapages")
                return

async def process_stats_message(message_text: str):
    global last_source_game_number, last_predicted_suit, suit_consecutive_counts, suit_block_until
    
    logger.info(f"Analyse message stats: {message_text[:100]}...")
    stats = parse_stats_message(message_text)
    if not stats:
        logger.warning("Aucune statistique extraite du message")
        return

    # Miroirs: ♦<->♠ et ♥<->♣
    pairs = [('♦', '♠'), ('♥', '♣')]
    
    for s1, s2 in pairs:
        if s1 in stats and s2 in stats:
            v1, v2 = stats[s1], stats[s2]
            diff = abs(v1 - v2)
            
            if diff >= 10:
                predicted_suit = s1 if v1 < v2 else s2
                logger.info(f"Détection Pattern: {s1}({v1}) vs {s2}({v2}) | Diff={diff} | Prédit={predicted_suit}")
                
                # Vérifier blocage actif
                if predicted_suit in suit_block_until:
                    if datetime.now() < suit_block_until[predicted_suit]:
                        logger.info(f"{predicted_suit} bloqué jusqu'à {suit_block_until[predicted_suit]}, ignoré")
                        continue
                    else:
                        del suit_block_until[predicted_suit]
                        suit_consecutive_counts[predicted_suit] = 0
                        suit_results_history[predicted_suit] = []
                
                # Réinitialiser si changement de cible
                if last_predicted_suit and last_predicted_suit != predicted_suit:
                    suit_consecutive_counts[last_predicted_suit] = 0
                    suit_results_history[last_predicted_suit] = []
                    logger.info(f"Changement de cible: {last_predicted_suit} -> {predicted_suit}")
                
                if last_source_game_number > 0:
                    target_game = last_source_game_number + USER_A
                    if queue_prediction(target_game, predicted_suit, last_source_game_number):
                        suit_consecutive_counts[predicted_suit] = suit_consecutive_counts.get(predicted_suit, 0) + 1
                        last_predicted_suit = predicted_suit
                        logger.info(f"✅ Prédiction #{target_game} planifiée pour {predicted_suit}")
                    return
                else:
                    logger.warning("Impossible de prédire : Numéro de jeu source inconnu")

def is_message_finalized(message: str) -> bool:
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message or '▶️' in message

async def process_finalized_message(message_text: str, chat_id: int):
    global last_transferred_game, current_game_number, last_source_game_number
    try:
        if chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            return

        if not is_message_finalized(message_text):
            return

        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number
        last_source_game_number = game_number
        
        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 2:  # Deuxième groupe
            return
        second_group = groups[1]  # Deuxième groupe

        await check_prediction_result(game_number, second_group)
        await check_and_send_queued_predictions(game_number)

    except Exception as e:
        logger.error(f"Erreur traitement: {e}")

async def handle_message(event):
    try:
        sender = await event.get_sender()
        sender_id = getattr(sender, 'id', event.sender_id)
        
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
            
        logger.info(f"DEBUG: Message reçu de chat_id={chat_id}: {event.message.message[:50]}...")

        if chat_id == SOURCE_CHANNEL_ID or chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_finalized_message(message_text, chat_id)
            
        if sender_id == ADMIN_ID:
            if event.message.message.startswith('/'):
                logger.info(f"DEBUG: Commande admin: {event.message.message}")

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")

async def handle_edited_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id == SOURCE_CHANNEL_ID or chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_finalized_message(message_text, chat_id)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

client.add_event_handler(handle_message, events.NewMessage())
client.add_event_handler(handle_edited_message, events.MessageEdited())

# --- Commandes Administrateur ---
@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel: return
    await event.respond("🤖 Bot de Prédiction Baccarat\n\nCommandes: /status, /help, /debug, /checkchannels")

@client.on(events.NewMessage(pattern=r'^/a (\d+)$'))
async def cmd_set_a_shortcut(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: return
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ Valeur de 'a' mise à jour : {USER_A}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'^/set_a (\d+)$'))
async def cmd_set_a(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: return
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ Valeur de 'a' mise à jour : {USER_A}\nLes prochaines prédictions seront sur le jeu N+{USER_A}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return

    status_msg = f"📊 État du Bot:\n\n"
    status_msg += f"🎮 Jeu actuel (Source 1): #{current_game_number}\n"
    status_msg += f"🔢 Paramètre 'a': {USER_A}\n\n"
    
    if suit_block_until:
        status_msg += f"🔒 Blocages actifs:\n"
        for suit, block_time in suit_block_until.items():
            if datetime.now() < block_time:
                remaining = block_time - datetime.now()
                status_msg += f"• {suit}: {remaining.seconds//60}min {remaining.seconds%60}s\n"
    
    if suit_consecutive_counts:
        status_msg += f"\n📊 Compteurs:\n"
        for suit, count in suit_consecutive_counts.items():
            if count > 0:
                status_msg += f"• {suit}: {count}/3\n"
    
    if pending_predictions:
        status_msg += f"\n🔮 Actives ({len(pending_predictions)}):\n"
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            ratt = f" (R{pred['rattrapage']})" if pred.get('rattrapage', 0) > 0 else ""
            status_msg += f"• #{game_num}{ratt}: {pred['suit']} - {pred['status']} (dans {distance})\n"
    else:
        status_msg += "🔮 Aucune prédiction active\n"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel: return
    await event.respond(f"""📖 Aide - Bot de Prédiction V3

Règles:
1. Surveille le Canal Source 2 (Stats)
2. Si décalage ≥10 jeux entre deux cartes → Prédit la plus faible
3. Cible: Dernier numéro Source 1 + a
4. Rattrapages: 3 jeux suivants si échec
5. Blocage: 3 prédictions consécutives
   - Si ❌ → Re-lance + bloque 5min
   - Si 3 ✅ → Bloque 5min
   - Si changement → Réinitialise

Commandes:
/status - État du bot
/set_a <valeur> - Modifier 'a'
/help - Cette aide""")

# --- Serveur Web ---
async def index(request):
    html = f"""<!DOCTYPE html><html><head><title>Bot Prédiction Baccarat</title></head><body><h1>🎯 Bot de Prédiction Baccarat</h1><p>Le bot est en ligne et surveille les canaux.</p><p><strong>Jeu actuel:</strong> #{current_game_number}</p></body></html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start() 
    logger.info(f"✅ Serveur web démarré sur le port {PORT}")

async def schedule_daily_reset():
    wat_tz = timezone(timedelta(hours=1)) 
    reset_time = time(0, 59, tzinfo=wat_tz)
    logger.info(f"Tâche de reset planifiée pour {reset_time} WAT.")

    while True:
        now = datetime.now(wat_tz)
        target_datetime = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target_datetime:
            target_datetime += timedelta(days=1)
            
        time_to_wait = (target_datetime - now).total_seconds()
        logger.info(f"Prochain reset dans {timedelta(seconds=time_to_wait)}")
        await asyncio.sleep(time_to_wait)

        logger.warning("🚨 RESET QUOTIDIEN À 00h59 WAT DÉCLENCHÉ!")
        
        global pending_predictions, queued_predictions, recent_games, processed_messages, last_transferred_game, current_game_number, last_source_game_number
        global suit_consecutive_counts, suit_results_history, suit_block_until, last_predicted_suit

        pending_predictions.clear()
        queued_predictions.clear()
        recent_games.clear()
        processed_messages.clear()
        suit_consecutive_counts.clear()
        suit_results_history.clear()
        suit_block_until.clear()
        last_transferred_game = None
        current_game_number = 0
        last_source_game_number = 0
        last_predicted_suit = None
        
        logger.warning("✅ Toutes les données de prédiction ont été effacées.")

async def start_bot():
    global source_channel_ok, prediction_channel_ok
    while True:
        try:
            await client.start(bot_token=BOT_TOKEN)
            source_channel_ok = True
            prediction_channel_ok = True 
            logger.info("Bot connecté et canaux accessibles.")
            
            if PREDICTION_CHANNEL_ID:
                try:
                    await client.send_message(PREDICTION_CHANNEL_ID, "✅ Bot de prédiction opérationnel. En attente de statistiques...")
                    logger.info("Message de test envoyé au canal de prédiction.")
                except Exception as e:
                    logger.error(f"Erreur envoi message de test: {e}")
                    
            return True
        except Exception as e:
            if "A wait of" in str(e):
                import re
                seconds = int(re.search(r"(\d+) seconds", str(e)).group(1))
                logger.warning(f"🚨 FloodWait: Attente de {seconds} secondes...")
                await asyncio.sleep(seconds + 5)
            else:
                logger.error(f"Erreur démarrage du bot: {e}")
                return False

async def main():
    try:
        # Démarrer le serveur web EN PREMIER
        await start_web_server()

        # Puis le bot
        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage du bot")
            return

        asyncio.create_task(schedule_daily_reset())
        
        logger.info("Bot complètement opérationnel - En attente de messages...")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur dans main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
