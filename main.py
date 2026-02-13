import os
import logging
import asyncio
import requests
import json
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from dotenv import load_dotenv
from aiohttp import web
import database as db

# --- CONFIGURACIÓN ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID").split(',')]
BANK_DETAILS = os.getenv("BANK_DETAILS")
ODDS_API_KEY = os.getenv("ODDS_API_KEY") # ASEGÚRATE DE TENER ESTA VARIABLE EN .ENV Y RENDER

# IDs de Ligas en Odds-API
LEAGUES = {
    "La Liga": "la_liga",
    "Premier League": "epl",
    "Bundesliga": "bundesliga",
    "Serie A": "serie_a",
    "Ligue 1": "ligue_1"
}

logging.basicConfig(level=logging.INFO)

# ESTADOS DE CONVERSACIÓN
UPLOAD_PHOTO, CONFIRM_DEPOSIT = range(2)
SELECT_LEAGUE, AMOUNT, CONFIRM_BET = range(3) # Estados simples/combo
COMBO_ADD, COMBO_FINISH = range(2)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_main_keyboard():
    keyboard = []
    # Botones de Ligas
    for name in LEAGUES.keys():
        keyboard.append([InlineKeyboardButton(f"⚽ {name}", callback_data=f'league_{name}')])
    
    # Botones de Funciones
    row1 = [InlineKeyboardButton("🎰 Apuesta Combinada", callback_data='start_combo')]
    row2 = [InlineKeyboardButton("💳 Depositar", callback_data='deposit_start')]
    row3 = [InlineKeyboardButton("💸 Retirar", callback_data='withdraw_start')]
    row4 = [InlineKeyboardButton("📊 Mis Apuestas", callback_data='my_bets')]
    row5 = [InlineKeyboardButton("💰 Mi Saldo", callback_data='my_balance')]
    
    keyboard.extend([row1, row2, row3, row4, row5])
    return keyboard

# --- FUNCIONES DE ODDS-API ---

def fetch_odds_api(sport_key):
    """Obtiene partidos y cuotas"""
    url = f"https://api.oddsapi.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu", 
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso"
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json()
        else:
            logging.error(f"Error API Odds: {response.status_code}")
            return []
    except Exception as e:
        logging.error(f"Excepción fetch_odds: {e}")
        return []

def fetch_scores_api():
    """Verifica resultados para pagar"""
    all_results = []
    for league_name, sport_key in LEAGUES.items():
        url = f"https://api.oddsapi.com/v4/sports/{sport_key}/scores"
        params = {
            "apiKey": ODDS_API_KEY,
            "daysFrom": 1 
        }
        try:
            response = requests.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list): all_results.extend(data)
        except: pass
    return all_results

# --- AUTOMATIZACIÓN ---

async def sync_events_job(context: ContextTypes.DEFAULT_TYPE):
    """Sincroniza eventos cada 1 hora"""
    logging.info("🔄 Sincronizando eventos con Odds-API...")
    
    for league_name, sport_key in LEAGUES.items():
        fixtures = fetch_odds_api(sport_key)
        
        for fix in fixtures:
            api_id = str(fix['id'])
            existing = db.get_event_by_api_id(api_id)
            
            bookmakers = fix.get('bookmakers', [])
            if not bookmakers: continue
            
            odds_data = None
            for bm in bookmakers:
                for m in bm.get('markets', []):
                    if m['key'] == 'h2h':
                        odds_data = m['outcomes']
                        break
                if odds_data: break
            
            if not odds_data: continue

            o_local, o_draw, o_away = 0.0, 0.0, 0.0
            for o in odds_data:
                if o['name'] == '1': o_local = o['price']
                if o['name'] == 'X': o_draw = o['price']
                if o['name'] == '2': o_away = o['price']

            home_team = fix.get('home_team')
            away_team = fix.get('away_team')
            commence_time = fix.get('commence_time', 'Unknown')

            if not existing:
                db.create_event_auto(
                    f"{home_team} vs {away_team}", 
                    o_local, o_draw, o_away, 
                    api_id, 
                    commence_time
                )
                logging.info(f"➕ Nuevo: {home_team} vs {away_team}")

async def auto_payouts_job(context: ContextTypes.DEFAULT_TYPE):
    """Paga apuestas simples automáticamente"""
    logging.info("💰 Verificando resultados...")
    results = fetch_scores_api()
    
    for res in results:
        api_id = str(res['id'])
        status = res.get('status')
        
        if status not in ['FT', 'Finished']: continue
        
        scores = res.get('scores', [])
        if not scores: continue
        
        home_score = 0; away_score = 0
        for s in scores:
            if s['name'] == 'Home': home_score = int(s['score'])
            if s['name'] == 'Away': away_score = int(s['score'])
        
        winner = None
        if home_score > away_score: winner = 'local'
        elif home_score < away_score: winner = 'away'
        else: winner = 'draw'
        
        conn = db.get_db_connection()
        cursor = conn.cursor()
        # Solo apuestas simples (is_combo=0)
        cursor.execute('SELECT * FROM bets WHERE event_id = (SELECT id FROM events WHERE api_event_id = ?) AND status="PENDING" AND is_combo=0', (api_id,))
        bets = cursor.fetchall()
        
        if not bets: 
            conn.close()
            continue
            
        for bet in bets:
            b = dict(bet)
            if b['selection'] == winner:
                db.update_user_balance(b['user_id'], b['potential_win'])
                cursor.execute('UPDATE bets SET status="WON" WHERE id=?', (b['id'],))
                try: await context.bot.send_message(chat_id=b['user_id'], text=f"🎉 GANASTE! Ganancia: ${b['potential_win']:.2f}")
                except: pass
            else:
                cursor.execute('UPDATE bets SET status="LOST" WHERE id=?', (b['id'],))
        
        cursor.execute('UPDATE events SET is_active=0 WHERE api_event_id=?', (api_id,))
        conn.commit()
        conn.close()

# --- HANDLERS DE USUARIO ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.register_or_update_user(user.id, user.username, user.first_name)
    await update.message.reply_text(
        f"👋 Hola {user.first_name}.\nSelecciona una liga:",
        reply_markup=InlineKeyboardMarkup(get_main_keyboard())
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # SELECCIÓN DE LIGA
    if data.startswith('league_'):
        league_name = data.split('_')[1]
        sport_key = LEAGUES[league_name]
        fixtures = fetch_odds_api(sport_key)
        if not fixtures:
            await query.edit_message_text(f"No hay partidos para {league_name}.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
            return
        
        text = f"⚽ **{league_name}**\n\n"
        keyboard = []
        
        for fix in fixtures[:10]:
            api_id = str(fix['id'])
            home = fix.get('home_team')
            away = fix.get('away_team')
            
            # Extraer cuotas
            bookmakers = fix.get('bookmakers', [])
            o1, ox, o2 = 2.0, 3.0, 3.5
            if bookmakers:
                for bm in bookmakers:
                    for m in bm.get('markets', []):
                        if m['key'] == 'h2h':
                            for o in m['outcomes']:
                                if o['name'] == '1': o1 = o['price']
                                if o['name'] == 'X': ox = o['price']
                                if o['name'] == '2': o2 = o['price']
                            break
                    if o1 != 2.0: break
            
            text += f"*{home} vs {away}*\n1️⃣ {o1} | X {ox} | 2️⃣ {o2}\n\n"
            
            keyboard.append([
                InlineKeyboardButton(f"1 ({o1})", callback_data=f'select_{api_id}_local_{o1}'),
                InlineKeyboardButton(f"X ({ox})", callback_data=f'select_{api_id}_draw_{ox}'),
                InlineKeyboardButton(f"2 ({o2})", callback_data=f'select_{api_id}_away_{o2}')
            ])
            
        keyboard.append([InlineKeyboardButton("⬅️ Menú", callback_data='back_menu')])
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    # SELECCIÓN DE APUESTA SIMPLE
    elif data.startswith('select_'):
        parts = data.split('_')
        api_id, selection, odds = parts[1], parts[2], float(parts[3])
        
        event = db.get_event_by_api_id(api_id)
        if not event: return

        context.user_data['pending_bet'] = {
            'event_id': event['id'],
            'name': event['name'],
            'selection': selection,
            'odds': odds
        }
        
        await query.edit_message_text(
            f"Selección: {selection.upper()} en {event['name']}\nCuota: {odds}\n\nMonto:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data='cancel_bet')]])
        )
        return AMOUNT

    # COMBINADAS
    elif data == 'start_combo':
        if 'combo_bets' not in context.user_data: context.user_data['combo_bets'] = []
        await show_leagues_for_combo(update, context)

    elif data.startswith('c_league_'):
        league_name = data.split('_', 2)[2]
        sport_key = LEAGUES[league_name]
        fixtures = fetch_odds_api(sport_key)
        if not fixtures: return

        text = f"Añadir a **Combinada** ({league_name}):\n\n"
        keyboard = []
        
        for fix in fixtures[:8]:
            api_id = str(fix['id'])
            home = fix.get('home_team')
            away = fix.get('away_team')
            bookmakers = fix.get('bookmakers', [])
            o1, ox, o2 = 2.0, 3.0, 3.5
            if bookmakers:
                for bm in bookmakers:
                    for m in bm.get('markets', []):
                        if m['key'] == 'h2h':
                            for o in m['outcomes']:
                                if o['name'] == '1': o1 = o['price']
                                if o['name'] == 'X': ox = o['price']
                                if o['name'] == '2': o2 = o['price']
                            break
                    if o1 != 2.0: break

            text += f"*{home} vs {away}*\n"
            keyboard.append([
                InlineKeyboardButton(f"1 ({o1})", callback_data=f'c_add_{api_id}_local_{o1}'),
                InlineKeyboardButton(f"X ({ox})", callback_data=f'c_add_{api_id}_draw_{ox}'),
                InlineKeyboardButton(f"2 ({o2})", callback_data=f'c_add_{api_id}_away_{o2}')
            ])
        
        keyboard.append([InlineKeyboardButton("⬅️ Volver", callback_data='start_combo')])
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('c_add_'):
        parts = data.split('_')
        api_id, selection, odds = parts[2], parts[3], float(parts[4])
        event = db.get_event_by_api_id(api_id)
        if not event: return
        
        context.user_data['combo_bets'].append({'id': event['id'], 'name': event['name'], 'selection': selection, 'odds': odds})
        await query.answer("Añadido")
        await show_combo_cart(update, context)

    elif data == 'c_finish':
        if not context.user_data.get('combo_bets'): return
        await query.edit_message_text("Combinada lista.\nMonto:")
        return AMOUNT

    elif data == 'c_cancel':
        del context.user_data['combo_bets']
        await query.edit_message_text("Cancelado.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))

    elif data == 'back_menu':
        await query.edit_message_text("Menú Principal", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))

    elif data == 'my_balance':
        bal = db.get_user_balance(user_id)
        await query.edit_message_text(f"💰 Saldo: ${bal}", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))

    elif data == 'my_bets':
        bets = db.get_bets_by_user(user_id)
        if not bets:
            await query.edit_message_text("Sin apuestas.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
            return
        text = "🎟️ **Tus Apuestas:**\n\n"
        for b in bets[:5]:
            status_icon = "⏳" if b['status'] == 'PENDING' else ("✅" if b['status'] == 'WON' else "❌")
            combo_tag = " 🎰" if b['is_combo'] else ""
            text += f"{status_icon} ${b['amount']} -> ${b['potential_win']:.2f}{combo_tag}\n"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(get_main_keyboard()))

# --- AYUDAS COMBO ---
async def show_leagues_for_combo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = []
    for name in LEAGUES.keys():
        keyboard.append([InlineKeyboardButton(name, callback_data=f'c_league_{name}')])
    keyboard.append([InlineKeyboardButton("⬅️ Volver", callback_data='back_menu')])
    await query.edit_message_text("Elige liga para añadir:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_combo_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bets = context.user_data['combo_bets']
    total_odds = 1.0
    text = "🛒 **Tu Combinada:**\n\n"
    for b in bets:
        total_odds *= b['odds']
        text += f"• {b['name']} ({b['selection'].upper()}) @{b['odds']}\n"
    text += f"\n🧮 Cuota Total: *{total_odds:.2f}*"
    keyboard = [
        [InlineKeyboardButton("➕ Añadir más", callback_data='start_combo')],
        [InlineKeyboardButton("✅ Apostar", callback_data='c_finish')],
        [InlineKeyboardButton("❌ Cancelar", callback_data='c_cancel')]
    ]
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

# --- MANEJO MONTO Y CONFIRMACIÓN ---
async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Monto inválido.")
        return AMOUNT

    user_id = update.effective_user.id
    context.user_data['temp_amount'] = amount
    
    is_combo = 'combo_bets' in context.user_data
    potential = 0.0
    details_text = ""

    if is_combo:
        bets = context.user_data['combo_bets']
        total_odds = 1.0
        for b in bets: total_odds *= b['odds']
        potential = amount * total_odds
        details_text = "🎰 **Combinada:**\n"
        for b in bets: details_text += f"- {b['name']} ({b['selection'].upper()})\n"
    else:
        info = context.user_data['pending_bet']
        potential = amount * info['odds']
        details_text = f"⚽ **Simple:**\n{info['name']} ({info['selection'].upper()})"

    keyboard = [
        [InlineKeyboardButton("✅ Confirmar", callback_data='confirm_yes')],
        [InlineKeyboardButton("❌ Cancelar", callback_data='confirm_no')]
    ]
    await update.message.reply_text(f"{details_text}\n\nMonto: ${amount}\nA ganar: ${potential:.2f}\n\n¿Confirmar?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRM_BET

async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    amount = context.user_data['temp_amount']
    
    if query.data == 'confirm_no':
        if 'combo_bets' in context.user_data: del context.user_data['combo_bets']
        if 'pending_bet' in context.user_data: del context.user_data['pending_bet']
        await query.edit_message_text("Cancelado.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
        return ConversationHandler.END

    if query.data == 'confirm_yes':
        potential = 0.0
        is_combo = 'combo_bets' in context.user_data
        
        if db.get_user_balance(user_id) < amount:
            await query.edit_message_text("Saldo insuficiente.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
            return ConversationHandler.END

        if is_combo:
            bets = context.user_data['combo_bets']
            total_odds = 1.0
            for b in bets: total_odds *= b['odds']
            potential = amount * total_odds
            
            conn = db.get_db_connection()
            cursor = conn.cursor()
            cursor.execute('INSERT INTO bets (user_id, amount, potential_win, is_combo, combo_details, status) VALUES (?, ?, ?, 1, ?, "PENDING")', (user_id, amount, potential, json.dumps(bets)))
            conn.commit()
            conn.close()
            del context.user_data['combo_bets']
        else:
            info = context.user_data['pending_bet']
            potential = amount * info['odds']
            db.place_bet(user_id, info['event_id'], info['selection'], info['odds'], amount, potential)
            del context.user_data['pending_bet']

        ticket = f"🎟️ **TICKET**\n\n💰 ${amount}\n🤑 ${potential:.2f}\n\n¡Suerte! 🍀"
        await query.edit_message_text("✅ ¡Hecho!", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
        await query.message.reply_text(ticket, parse_mode='Markdown')
        return ConversationHandler.END

# --- DEPÓSITOS Y RETIROS ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo_obj = update.message.photo[-1]
    file_id = photo_obj.file_id
    context.user_data['pending_deposit_photo'] = file_id
    keyboard = [[InlineKeyboardButton("✅ Enviar", callback_data='confirm_deposit_yes')], [InlineKeyboardButton("❌ Cancelar", callback_data='cancel_deposit')]]
    await update.message.reply_text("¿Es esta la captura correcta?", reply_markup=InlineKeyboardMarkup(keyboard))
    return UPLOAD_PHOTO

async def confirm_deposit_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'cancel_deposit':
        if 'pending_deposit_photo' in context.user_data: del context.user_data['pending_deposit_photo']
        await query.edit_message_text("Cancelado.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
        return ConversationHandler.END

    file_id = context.user_data['pending_deposit_photo']
    del context.user_data['pending_deposit_photo']
    trans_id = db.create_transaction(query.from_user.id, 'DEPOSIT', 0)
    caption = f"🔔 **DEPÓSITO**\n🆔 ID: {trans_id}"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=caption, parse_mode='Markdown')
            await context.bot.send_message(chat_id=admin_id, text=f"Aprobar: /aprobar {trans_id} <monto>")
        except: pass
    await query.edit_message_text("Enviado a admin.")
    await query.message.reply_text("Volviendo al menú...", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
    return ConversationHandler.END

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) < 2: return
    try:
        trans_id = int(context.args[0]); val2 = context.args[1]
        trans = db.get_transaction(trans_id)
        if not trans: return
        if trans['type'] == 'DEPOSIT':
            amount = float(val2)
            db.update_user_balance(trans['user_id'], amount)
            db.update_transaction_status(trans_id, 'APPROVED')
            await update.message.reply_text(f"✅ Aprobado ${amount}")
        elif trans['type'] == 'WITHDRAW':
            if val2 == 'ok': db.update_transaction_status(trans_id, 'APPROVED')
    except: pass

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Bot en modo automático. Solo gestiona dinero.")

# --- MAIN ---
async def handle_health(request): return web.Response(text="OK")
async def run_web_server(app):
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()

def main():
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("aprobar", cmd_approve))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Conversaciones
    bet_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^select_|^c_finish')],
        states={AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)], CONFIRM_BET: [CallbackQueryHandler(handle_confirm)]},
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)]
    )
    application.add_handler(bet_conv)

    dep_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^deposit_start$')],
        states={UPLOAD_PHOTO: [MessageHandler(filters.PHOTO, handle_photo)], CONFIRM_DEPOSIT: [CallbackQueryHandler(confirm_deposit_action)]},
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)]
    )
    application.add_handler(dep_conv)
    
    # Retiro Simple
    async def wit_start(u, c):
        if db.get_user_balance(u.effective_user.id) <= 0: await u.message.reply_text("Sin saldo.")
        else: await u.message.reply_text("Monto a retirar:"); return AMOUNT
    wit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(wit_start, pattern='^withdraw_start$')],
        states={AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: u.message.reply_text("Retiro solicitado.") or ConversationHandler.END)]},
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)]
    )
    application.add_handler(wit_conv)

    # CRON
    job_queue = application.job_queue
    job_queue.run_repeating(sync_events_job, interval=3600, first=10)
    job_queue.run_repeating(auto_payouts_job, interval=600, first=60)

    # WEB
    web_app = web.Application()
    web_app.add_routes([web.get('/', handle_health)])
    loop = asyncio.get_event_loop()
    loop.create_task(run_web_server(web_app))
    
    print("Bot listo con Odds-API...")
    application.run_polling()

if __name__ == '__main__':
    main()
