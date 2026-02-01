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
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Vérifications minimales de la configuration
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

# Initialisation du client Telegram avec session string ou nouvelle session
session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# --- Variables Globales d'État ---
# Prédictions actives (déjà envoyées au canal de prédiction)
pending_predictions = {}
# Prédictions en attente (prêtes à être envoyées dès que la distance est bonne)
queued_predictions = {}
recent_games = {}
processed_messages = set()
last_transferred_game = None
current_game_number = 0
last_source_game_number = 0

# NOUVELLES VARIABLES POUR LA LOGIQUE DE BLOCAGE
suit_consecutive_counts = {}  # Compteur de prédictions consécutives par costume
suit_results_history = {}     # Historique des 3 derniers résultats par costume
suit_block_until = {}         # Timestamp de fin de blocage pour chaque costume
last_predicted_suit = None    # Dernier costume prédit (pour détecter les changements)

MAX_PENDING_PREDICTIONS = 5  # Augmenté pour gérer les rattrapages
PROXIMITY_THRESHOLD = 3      # Nombre de jeux avant l'envoi depuis la file d'attente
USER_A = 1                   # Valeur 'a' choisie par l'utilisateur (entier naturel)

source_channel_ok = False
prediction_channel_ok = False
transfer_enabled = True # Initialisé à True

# --- Fonctions d'Analyse ---

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message."""
    # Pattern plus flexible pour #N59 ou #N 59
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def parse_stats_message(message: str):
    """Extrait les statistiques du canal source 2."""
    stats = {}
    # Pattern pour extraire : ♠️ : 9 (23.7 %)
    patterns = {
        '♠': r'♠️?\s*:\s*(\d+)',
        '♥': r'♥️?\s*:\s*(\d+)',
        '♦': r'♦️?\s*:\s*(\d+)',
        '♣': r'♣️?\s*:\s*(\d+)'
    }
    for suit, pattern in patterns.items():
        match = re.search(pattern, message)
        if match:
            stats[suit] = int(match.group(1))
    return stats

def extract_parentheses_groups(message: str):
    """Extrait le contenu entre parenthèses."""
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(group_str: str) -> str:
    """Remplace les différentes variantes de symboles par un format unique (important pour la détection)."""
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_suits_in_group(group_str: str):
    """Liste toutes les couleurs (suits) présentes dans une chaîne."""
    normalized = normalize_suits(group_str)
    return [s for s in ALL_SUITS if s in normalized]

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    """Vérifie si la couleur cible est présente dans le deuxième groupe du résultat."""
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False

def get_predicted_suit(missing_suit: str) -> str:
    """Applique le mapping personnalisé (couleur manquante -> couleur prédite)."""
    return SUIT_MAPPING.get(missing_suit, missing_suit)

# --- Logique de Prédiction et File d'Attente ---

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None):
    """Envoie la prédiction au canal de prédiction et l'ajoute aux prédictions actives."""
    try:
        # Si c'est un rattrapage, on ne crée pas un nouveau message, on garde la trace
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

        # NOUVEAU FORMAT DE MESSAGE
        prediction_msg = f"""🎮 joueur №{target_game}
⚜️ Couleur de la carte:{SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
🎰 Poursuite deux jeux(🔰+3)
🗯️ Résultats :⏳"""
        msg_id = 0

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée au canal de prédiction {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction au canal: {e}")
        else:
            logger.warning(f"⚠️ Canal de prédiction non accessible, prédiction non envoyée")

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
    """Met une prédiction en file d'attente pour un envoi différé."""
    # Vérification d'unicité
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
    """Vérifie la file d'attente et envoie les prédictions."""
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
    """Met à jour le message de prédiction dans le canal."""
    global suit_consecutive_counts, suit_results_history, suit_block_until, last_predicted_suit
    
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['suit']

        # NOUVEAU FORMAT DE MESSAGE
        updated_msg = f"""🎮 joueur №{game_number}
⚜️ Couleur de la carte:{SUIT_DISPLAY.get(suit, suit)}
🎰 Poursuite deux jeux(🔰+3)
🗯️ Résultats :{new_status}"""

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour: {e}")

        # --- LOGIQUE DE GESTION DES RÉSULTATS (CORRIGÉE) ---
        
        # Initialiser l'historique pour ce costume si nécessaire
        if suit not in suit_results_history:
            suit_results_history[suit] = []
        
        # Ajouter le nouveau résultat à l'historique (garder les 3 derniers)
        suit_results_history[suit].append(new_status)
        if len(suit_results_history[suit]) > 3:
            suit_results_history[suit].pop(0)
        
        # Mettre à jour le statut de la prédiction
        pred['status'] = new_status
        
        # Supprimer si terminé
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣', '❌']:
            del pending_predictions[game_number]
            
            # --- VÉRIFICATION ET BLOCAGE APRÈS RÉSULTAT ---
            
            # Vérifier si on a 3 résultats pour ce costume
            if len(suit_results_history[suit]) == 3:
                logger.info(f"3 résultats consécutifs pour {suit}: {suit_results_history[suit]}")
                
                # CAS 1 : Si au moins un ❌ dans les 3 résultats
                if '❌' in suit_results_history[suit]:
                    logger.info(f"❌ détecté pour {suit} → Lancement immédiat au numéro suivant + blocage")
                    
                    # Lancer immédiatement une nouvelle prédiction pour le même costume
                    if current_game_number > 0:
                        # Utiliser le numéro actuel + 1 pour le re-lancement
                        target_game = current_game_number + 1
                        logger.info(f"Re-lancement immédiat de {suit} au jeu #{target_game}")
                        
                        # Bypass le blocage en envoyant directement sans vérifier
                        await send_prediction_to_channel(target_game, suit, current_game_number)
                    
                    # Puis bloquer ce costume pendant 5 minutes
                    block_until = datetime.now() + timedelta(minutes=5)
                    suit_block_until[suit] = block_until
                    suit_consecutive_counts[suit] = 0  # Réinitialiser le compteur
                    suit_results_history[suit] = []  # Réinitialiser l'historique
                    logger.info(f"{suit} bloqué jusqu'à {block_until}")
                
                # CAS 2 : Si 3 succès consécutifs (tous ✅)
                # Cette vérification est maintenant faite AVANT la prédiction, donc on ne devrait plus jamais passer ici
                # Mais on la garde comme sécurité
                elif all('✅' in result for result in suit_results_history[suit]):
                    logger.info(f"3 succès consécutifs pour {suit} → Blocage 5 minutes")
                    block_until = datetime.now() + timedelta(minutes=5)
                    suit_block_until[suit] = block_until
                    suit_consecutive_counts[suit] = 0  # Réinitialiser le compteur
                    suit_results_history[suit] = []  # Réinitialiser l'historique
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
    # On cherche dans pending_predictions si un jeu original correspond à un rattrapage
    for target_game, pred in list(pending_predictions.items()):
        if target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game', target_game - pred['rattrapage'])
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            
            if has_suit_in_group(second_group, target_suit):
                # Trouvé ! On met à jour le statut avec le bon numéro de rattrapage
                await update_prediction_status(original_game, f'✅{rattrapage_actuel}️⃣')
                # On supprime aussi l'entrée de rattrapage si elle est différente de l'originale
                if target_game != original_game:
                    del pending_predictions[target_game]
                return
            else:
                # Échec du rattrapage actuel
                if rattrapage_actuel < 3:
                    # Continuer la séquence
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    queue_prediction(next_target, target_suit, pred['base_game'], rattrapage=next_rattrapage, original_game=original_game)
                    logger.info(f"Échec rattrapage {rattrapage_actuel} sur #{game_number}, Rattrapage {next_rattrapage} planifié pour #{next_target}")
                    # Supprimer le rattrapage échoué pour laisser place au suivant
                    del pending_predictions[target_game]
                else:
                    # Échec final après 3 rattrapages
                    await update_prediction_status(original_game, '❌')
                    if target_game != original_game:
                        del pending_predictions[target_game]
                    logger.info(f"Échec final pour la prédiction originale #{original_game} après 3 rattrapages")
                return

async def process_stats_message(message_text: str):
    """Traite les statistiques du canal 2 selon les miroirs ♦️<->♠️ et ❤️<->♣️."""
    global last_source_game_number, last_predicted_suit, suit_consecutive_counts, suit_block_until
    
    stats = parse_stats_message(message_text)
    if not stats:
        return

    # Miroirs : ♦️<->♠️ et ❤️<->♣️
    pairs = [('♦', '♠'), ('♥', '♣')]
    
    for s1, s2 in pairs:
        if s1 in stats and s2 in stats:
            v1, v2 = stats[s1], stats[s2]
            diff = abs(v1 - v2)
            
            # MODIFIÉ : 6 changé à 10
            if diff >= 10:
                # Prédire le plus faible parmi les deux miroirs
                predicted_suit = s1 if v1 < v2 else s2
                
                # --- LOGIQUE DE BLOCAGE CORRECTE ---
                
                # Vérifier si ce costume est bloqué
                if predicted_suit in suit_block_until:
                    block_until = suit_block_until[predicted_suit]
                    if datetime.now() < block_until:
                        logger.info(f"{predicted_suit} est bloqué jusqu'à {block_until}, prédiction ignorée")
                        return False
                    else:
                        # Blocage expiré, nettoyer
                        del suit_block_until[predicted_suit]
                        suit_consecutive_counts[predicted_suit] = 0
                        suit_results_history[predicted_suit] = []  # Réinitialiser aussi l'historique
                
                # Vérifier si on a déjà 3 prédictions consécutives sans ❌
                # Si oui, bloquer avant de prédire
                if predicted_suit in suit_results_history and len(suit_results_history[predicted_suit]) == 3:
                    if '❌' not in suit_results_history[predicted_suit]:
                        logger.info(f"3 succès consécutifs pour {predicted_suit} → Blocage 5 minutes")
                        block_until = datetime.now() + timedelta(minutes=5)
                        suit_block_until[predicted_suit] = block_until
                        suit_consecutive_counts[predicted_suit] = 0
                        suit_results_history[predicted_suit] = []
                        return False
                
                # Réinitialiser le compteur si changement de costume
                if last_predicted_suit and last_predicted_suit != predicted_suit:
                    suit_consecutive_counts[last_predicted_suit] = 0
                    suit_results_history[last_predicted_suit] = []
                    logger.info(f"Changement de costume: {last_predicted_suit} -> {predicted_suit}, compteur réinitialisé")
                
                logger.info(f"Décalage détecté entre {s1} ({v1}) et {s2} ({v2}): {diff}. Plus faible: {predicted_suit}")
                
                if last_source_game_number > 0:
                    target_game = last_source_game_number + USER_A
                    
                    # Mettre en file d'attente et incrémenter le compteur
                    if queue_prediction(target_game, predicted_suit, last_source_game_number):
                        suit_consecutive_counts[predicted_suit] = suit_consecutive_counts.get(predicted_suit, 0) + 1
                        last_predicted_suit = predicted_suit
                        logger.info(f"Compteur {predicted_suit}: {suit_consecutive_counts[predicted_suit]}")
                    
                    return # Une seule prédiction par message de stats

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est un résultat final (non en cours)."""
    if '⏰' in message:
        return False
    # Accepter les messages qui ont un résultat (par exemple "▶️") ou les symboles de validation
    return '✅' in message or '🔰' in message or '▶️' in message

async def process_finalized_message(message_text: str, chat_id: int):
    """Traite les messages du canal source 1 ou 2."""
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
        
        # Hash pour éviter doublons
        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 2:  # MODIFIÉ : vérifier qu'on a au moins 2 groupes
            return
        second_group = groups[1]  # MODIFIÉ : prendre le deuxième groupe au lieu du premier

        # Vérification des résultats avec le deuxième groupe
        await check_prediction_result(game_number, second_group)
        # Envoi des files d'attente
        await check_and_send_queued_predictions(game_number)

    except Exception as e:
        logger.error(f"Erreur traitement: {e}")

async def handle_message(event):
    """Gère les nouveaux messages dans les canaux sources."""
    try:
        sender = await event.get_sender()
        sender_id = getattr(sender, 'id', event.sender_id)
        
        # LOG DE DÉBOGAGE POUR VOIR TOUS LES MESSAGES ENTRANTS
        chat = await event.get_chat()
        chat_id = chat.id
        # Convert internal ID to -100xxx format if it's a channel
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
            
        logger.info(f"DEBUG: Message reçu de chat_id={chat_id}: {event.message.message[:50]}...")

        if chat_id == SOURCE_CHANNEL_ID or chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_finalized_message(message_text, chat_id)
            # Après traitement, si c'est le canal 2, on force la vérification de l'envoi
            if chat_id == SOURCE_CHANNEL_2_ID:
                await check_and_send_queued_predictions(current_game_number)
            
        # Gérer les commandes admin même si elles ne viennent pas d'un canal
        if sender_id == ADMIN_ID:
            if event.message.message.startswith('/'):
                logger.info(f"DEBUG: Commande admin reçue: {event.message.message}")

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")

async def handle_edited_message(event):
    """Gère les messages édités dans les canaux sources."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id == SOURCE_CHANNEL_ID or chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_finalized_message(message_text, chat_id)
            # Après traitement, si c'est le canal 2, on force la vérification de l'envoi
            if chat_id == SOURCE_CHANNEL_2_ID:
                await check_and_send_queued_predictions(current_game_number)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

# --- Gestion des Messages (Hooks Telethon) ---

client.add_event_handler(handle_message, events.NewMessage())
client.add_event_handler(handle_edited_message, events.MessageEdited())

# --- Commandes Administrateur ---

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel: return
    await event.respond("🤖 **Bot de Prédiction Baccarat**\n\nCommandes: `/status`, `/help`, `/debug`, `/checkchannels`")

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

    status_msg = f"📊 **État du Bot:**\n\n"
    status_msg += f"🎮 Jeu actuel (Source 1): #{current_game_number}\n"
    status_msg += f"🔢 Paramètre 'a': {USER_A}\n\n"
    
    # Afficher les blocages actifs
    if suit_block_until:
        status_msg += f"**🔒 Blocages actifs:**\n"
        for suit, block_time in suit_block_until.items():
            if datetime.now() < block_time:
                remaining = block_time - datetime.now()
                status_msg += f"• {suit}: {remaining.seconds}s restantes\n"
    
    # Afficher les compteurs
    if suit_consecutive_counts:
        status_msg += f"\n**📊 Compteurs de costumes:**\n"
        for suit, count in suit_consecutive_counts.items():
            if count > 0:
                status_msg += f"• {suit}: {count}/3\n"
    
    if pending_predictions:
        status_msg += f"\n**🔮 Actives ({len(pending_predictions)}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            ratt = f" (R{pred['rattrapage']})" if pred.get('rattrapage', 0) > 0 else ""
            status_msg += f"• #{game_num}{ratt}: {pred['suit']} - {pred['status']} (dans {distance})\n"
    else: status_msg += "**🔮 Aucune prédiction active**\n"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel: return
    await event.respond(f"""📖 **Aide - Bot de Prédiction V3**

**Règles de prédiction :**
1. Surveille le **Canal Source 2** (Stats).
2. Si un décalage d'au moins **10 jeux** existe entre deux cartes :
   - Prédit la carte en avance.
   - Cible le jeu : **Dernier numéro Source 1 + a**.
3. **Rattrapages :** Si la carte ne sort pas au jeu cible, le bot retente sur les **3 jeux suivants** (3 rattrapages).
4. **Blocage :** 3 prédictions consécutives du même costume:
   - Si ❌ détecté → Re-lance immédiatement au numéro suivant puis bloque 5min
   - Si 3 succès → Bloque 5min
   - Si changement de costume → Réinitialise le compteur

**Commandes :**
- `/status` : Affiche l'état actuel.
- `/set_a <valeur>` : Modifie l'entier 'a' (par défaut 1).
- `/debug` : Infos techniques.
""")


# --- Serveur Web et Démarrage ---

async def index(request):
    html = f"""<!DOCTYPE html><html><head><title>Bot Prédiction Baccarat</title></head><body><h1>🎯 Bot de Prédiction Baccarat</h1><p>Le bot est en ligne et surveille les canaux.</p><p><strong>Jeu actuel:</strong> #{current_game_number}</p></body></html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    """Démarre le serveur web pour la vérification de l'état (health check)."""
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start() 
    logger.info(f"✅ Serveur web démarré sur le port {PORT}")

async def schedule_daily_reset():
    """Tâche planifiée pour la réinitialisation quotidienne des stocks de prédiction à 00h59 WAT."""
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
    """Démarre le client Telegram et les vérifications initiales."""
    global source_channel_ok, prediction_channel_ok
    try:
        await client.start(bot_token=BOT_TOKEN)
        
        source_channel_ok = True
        prediction_channel_ok = True 
        logger.info("Bot connecté et canaux marqués comme accessibles.")
        return True
    except Exception as e:
        logger.error(f"Erreur démarrage du client Telegram: {e}")
        return False

async def main():
    """Fonction principale pour lancer le serveur web, le bot et la tâche de reset."""
    try:
        # Démarrer le serveur web en premier pour que Render détecte le port ouvert
        await start_web_server()

        # Démarrer le bot Telegram
        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage du bot")
            return

        # Lancement de la tâche de reset en arrière-plan
        asyncio.create_task(schedule_daily_reset())
        
        logger.info("Bot complètement opérationnel - En attente de messages...")
        # Cette ligne bloque, mais le serveur web tourne en parallèle
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur dans main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if client.is_connected():
            await client.disconnect()

# --- Point d'entrée principal ---
if __name__ == '__main__':
    try:
        # Démarrer l'événement loop
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
