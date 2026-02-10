import os
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from dotenv import load_dotenv
import database as db

# CARGAR CONFIGURACIÓN
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID").split(',')]
BANK_DETAILS = os.getenv("BANK_DETAILS")

logging.basicConfig(level=logging.INFO)

# ESTADOS
AMOUNT, UPLOAD_PHOTO = range(2)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_main_keyboard():
    return [
        [InlineKeyboardButton("⚽ Apostar", callback_data='bet_list')],
        [InlineKeyboardButton("💳 Depositar", callback_data='deposit_start')],
        [InlineKeyboardButton("💸 Retirar", callback_data='withdraw_start')],
        [InlineKeyboardButton("📊 Mi Saldo", callback_data='my_balance')]
    ]

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.register_or_update_user(user.id, user.username, user.first_name)
    await update.message.reply_text(f"👋 Hola {user.first_name}, bienvenido.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == 'bet_list':
        events = db.get_active_events()
        if not events:
            await query.edit_message_text("No hay eventos activos.")
            return
        
        text = "🏆 **Eventos:**\n\n"
        keyboard = []
        for ev in events:
            text += f"⚽ *{ev['name']}*\n"
            text += f"1️⃣ ({ev['odds_local']}) | X ({ev['odds_draw']}) | 2️⃣ ({ev['odds_away']})\n\n"
            keyboard.append([
                InlineKeyboardButton(f"1 ({ev['odds_local']})", callback_data=f"bet_{ev['id']}_local_{ev['odds_local']}"),
                InlineKeyboardButton(f"X ({ev['odds_draw']})", callback_data=f"bet_{ev['id']}_draw_{ev['odds_draw']}),
                InlineKeyboardButton(f"2 ({ev['odds_away']})", callback_data=f"bet_{ev['id']}_away_{ev['odds_away']}")
            ])
        keyboard.append([InlineKeyboardButton("⬅️ Volver", callback_data='back_menu')])
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == 'deposit_start':
        await query.edit_message_text(f"💳 **Datos Bancarios:**\n\n{BANK_DETAILS}\n\nEnvía la **CAPTURA** ahora.", parse_mode='Markdown')
        return UPLOAD_PHOTO

    elif data == 'withdraw_start':
        balance = db.get_user_balance(user_id)
        if balance <= 0:
            await query.edit_message_text("Sin saldo.")
            return ConversationHandler.END
        await query.edit_message_text(f"Saldo: ${balance}\n\nMonto a retirar:")
        return AMOUNT

    elif data == 'my_balance':
        bal = db.get_user_balance(user_id)
        await query.edit_message_text(f"💰 Saldo: ${bal}")
    
    elif data == 'back_menu':
        await query.edit_message_text("Menú", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))

    elif data.startswith('bet_'):
        parts = data.split('_')
        context.user_data['bet_info'] = {'id': int(parts[1]), 'sel': parts[2], 'odds': float(parts[3])}
        await query.edit_message_text(f"Apuesta: {parts[2].upper()} (Cuota: {parts[3]})\n\nEscribe monto:")
        return AMOUNT

async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        if amount <= 0: raise ValueError
    except:
        await update.message.reply_text("Número inválido.")
        return AMOUNT

    user_id = update.effective_user.id

    # Apuesta
    if 'bet_info' in context.user_data:
        info = context.user_data['bet_info']
        if amount > db.get_user_balance(user_id):
            await update.message.reply_text("Saldo insuficiente.")
            return ConversationHandler.END
        
        potential = amount * info['odds']
        if db.place_bet(user_id, info['id'], info['sel'], info['odds'], amount, potential):
            await update.message.reply_text(f"✅ Apuesta hecha. Ganancia pos: ${potential:.2f}")
        return ConversationHandler.END

    # Retiro
    else:
        if amount > db.get_user_balance(user_id):
            await update.message.reply_text("Saldo insuficiente.")
            return ConversationHandler.END
        
        db.update_user_balance(user_id, -amount)
        trans_id = db.create_transaction(user_id, 'WITHDRAW', amount)
        
        msg = f"🔔 **RETIRO**\nUser ID: {user_id}\nMonto: ${amount}\nID: {trans_id}\n\n/aprobar {trans_id} ok"
        for admin in ADMIN_IDS:
            await context.bot.send_message(chat_id=admin, text=msg, parse_mode='Markdown')
            
        await update.message.reply_text("Solicitud enviada.")
        return ConversationHandler.END

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Guardar Foto
    photo_file = await update.message.photo[-1].get_file()
    filename = f"deposit_{user_id}_{int(datetime.now().timestamp())}.jpg"
    # Usamos la ruta de UPLOAD_DIR definida en database.py
    filepath = os.path.join(database.UPLOAD_DIR, filename)
    await photo_file.download_to_drive(filepath)
    
    trans_id = db.create_transaction(user_id, 'DEPOSIT', 0, photo_path=filepath)
    
    caption = f"🔔 **DEPÓSITO**\nUser: {update.effective_user.first_name}\nID: {trans_id}"
    for admin in ADMIN_IDS:
        # IMPORTANTE: Enviar el archivo desde el disco
        with open(filepath, 'rb') as photo:
            await context.bot.send_photo(chat_id=admin, photo=photo, caption=caption, parse_mode='Markdown')
        await context.bot.send_message(chat_id=admin, text=f"Aprobar: /aprobar {trans_id} <MONTO>")

    await update.message.reply_text("Foto recibida. Validando...")
    return ConversationHandler.END

# --- COMANDOS ADMIN ---

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("Panel Admin:\n/crear_evento <Nombre> <C1> <CX> <C2>\n/aprobar <ID> <Monto/u ok>")

async def cmd_create_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        args = context.args
        if len(args) < 5: raise ValueError
        # Lógica simple: últimos 3 son cuotas
        odds_away = float(args[-1])
        odds_draw = float(args[-2])
        odds_local = float(args[-3])
        name = " ".join(args[:-3])
        db.create_event(name, odds_local, odds_draw, odds_away)
        await update.message.reply_text(f"✅ Evento creado: {name}")
    except:
        await update.message.reply_text("Formato: /crear_evento Equipo A vs Equipo B 1.5 3.0 5.0")

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /aprobar <ID> <MONTO> (depósito) o /aprobar <ID> ok (retiro)")
        return

    try:
        trans_id = int(context.args[0])
        val2 = context.args[1]
        trans = db.get_transaction(trans_id)
        if not trans: return

        if trans['type'] == 'DEPOSIT':
            amount = float(val2)
            db.update_user_balance(trans['user_id'], amount)
            db.update_transaction_status(trans_id, 'APPROVED')
            await update.message.reply_text(f"✅ Depósito ${amount} aprobado.")
            await context.bot.send_message(chat_id=trans['user_id'], text=f"✅ Tu depósito de ${amount} fue aceptado.")

        elif trans['type'] == 'WITHDRAW':
            if val2.lower() in ['ok', 'si']:
                db.update_transaction_status(trans_id, 'APPROVED')
                await update.message.reply_text("✅ Retiro aprobado.")
                await context.bot.send_message(chat_id=trans['user_id'], text="✅ Tu retiro fue procesado.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("crear_evento", cmd_create_event))
    app.add_handler(CommandHandler("aprobar", cmd_approve))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Conversaciones
    dep_handler = ConversationHandler(entry_points=[CallbackQueryHandler(button_handler, pattern='^deposit_start$')],
                                     states={UPLOAD_PHOTO: [MessageHandler(filters.PHOTO, handle_photo)]},
                                     fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)])
    app.add_handler(dep_handler)

    bet_handler = ConversationHandler(entry_points=[CallbackQueryHandler(button_handler, pattern='^bet_')],
                                     states={AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)]},
                                     fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)])
    app.add_handler(bet_handler)

    wit_handler = ConversationHandler(entry_points=[CallbackQueryHandler(button_handler, pattern='^withdraw_start$')],
                                     states={AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)]},
                                     fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)])
    app.add_handler(wit_handler)

    app.run_polling()

if __name__ == '__main__':
    main()
