import os
import logging
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from dotenv import load_dotenv
from aiohttp import web
import database as db

# CARGAR CONFIGURACIÓN
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID").split(',')]
BANK_DETAILS = os.getenv("BANK_DETAILS")

logging.basicConfig(level=logging.INFO)

# ESTADOS DE CONVERSACIÓN
# UPLOAD_PHOTO: Usuario sube captura
# CONFIRM_DEPOSIT: Usuario confirma si la foto es correcta
# AMOUNT: Usuario escribe monto
# CONFIRM_WITHDRAW: Usuario confirma si el monto de retiro es correcto
UPLOAD_PHOTO, CONFIRM_DEPOSIT, AMOUNT, CONFIRM_WITHDRAW = range(4)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_main_keyboard():
    return [
        [InlineKeyboardButton("⚽ Apostar", callback_data='bet_list')],
        [InlineKeyboardButton("💳 Depositar", callback_data='deposit_start')],
        [InlineKeyboardButton("💸 Retirar", callback_data='withdraw_start')],
        [InlineKeyboardButton("📊 Mi Saldo", callback_data='my_balance')]
    ]

# --- MANEJO DE FOTOS (DEPÓSITOS) ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Obtener la foto
    photo_obj = update.message.photo[-1]
    file_id = photo_obj.file_id
    
    # Guardar ID temporalmente para confirmar después
    context.user_data['pending_deposit_photo'] = file_id
    
    # Preguntar confirmación
    keyboard = [
        [InlineKeyboardButton("✅ Confirmar y Enviar", callback_data='confirm_deposit_yes')],
        [InlineKeyboardButton("❌ Cancelar", callback_data='cancel_deposit')]
    ]
    await update.message.reply_text(
        "¿Has enviado la transferencia y esta es la captura correcta?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_DEPOSIT

async def confirm_deposit_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == 'cancel_deposit':
        # Usuario canceló, borrar datos y volver
        if 'pending_deposit_photo' in context.user_data:
            del context.user_data['pending_deposit_photo']
        await query.edit_message_text("Operación cancelada.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
        return ConversationHandler.END

    if data == 'confirm_deposit_yes':
        # Usuario confirmó, procesar
        file_id = context.user_data['pending_deposit_photo']
        del context.user_data['pending_deposit_photo']
        
        trans_id = db.create_transaction(user_id, 'DEPOSIT', 0, photo_path=None)
        
        caption = (
            f"🔔 **NUEVO DEPÓSITO**\n"
            f"👤 Usuario: {query.from_user.first_name} (@{query.from_user.username})\n"
            f"🆔 ID Transacción: {trans_id}\n\n"
            f"Verifica el monto en la imagen y apruébalo."
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=caption, parse_mode='Markdown')
                await context.bot.send_message(chat_id=admin_id, text=f"Para acreditar saldo, usa:\n`/aprobar {trans_id} <MONTO_VISTO>`", parse_mode='Markdown')
            except Exception as e:
                print(f"Error enviando a admin: {e}")

        await query.edit_message_text("📸 Comprobante enviado al administrador. Espera validación.")
        
        # REDIRIGIR AL MENÚ
        await query.message.reply_text("Volviendo al menú principal...", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
        return ConversationHandler.END

# --- MANEJO DE TEXTOS (Monto Apuesta / Retiro) ---

async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Monto inválido. Por favor ingresa un número positivo.")
        return AMOUNT

    user_id = update.effective_user.id
    context.user_data['temp_amount'] = amount

    # CASO A: ES UNA APUESTA
    if 'bet_info' in context.user_data:
        info = context.user_data['bet_info']
        
        if amount > db.get_user_balance(user_id):
            await update.message.reply_text("❌ Saldo insuficiente para realizar esta apuesta.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
            return ConversationHandler.END
        
        potential = amount * info['odds']
        
        # Pedir confirmación de apuesta
        keyboard = [
            [InlineKeyboardButton("✅ Confirmar Apuesta", callback_data='confirm_bet_yes')],
            [InlineKeyboardButton("❌ Cancelar", callback_data='cancel_bet')]
        ]
        await update.message.reply_text(
            f"Resumen de Apuesta:\n"
            f"Evento: {info['sel'].upper()}\n"
            f"Cuota: {info['odds']}\n"
            f"Monto: ${amount}\n"
            f"Ganancia Potencial: ${potential:.2f}\n\n"
            f"¿Confirmar?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        # Usamos el mismo estado AMOUNT pero esperamos callback
        return AMOUNT 

    # CASO B: ES UN RETIRO
    else:
        if amount > db.get_user_balance(user_id):
            await update.message.reply_text("❌ Saldo insuficiente.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
            return ConversationHandler.END
        
        # Pedir confirmación de retiro
        keyboard = [
            [InlineKeyboardButton("✅ Confirmar Retiro", callback_data='confirm_withdraw_yes')],
            [InlineKeyboardButton("❌ Cancelar", callback_data='cancel_withdraw')]
        ]
        await update.message.reply_text(
            f"Vas a retirar: ${amount}\n\n"
            f"Esta acción descontará el dinero de tu saldo inmediatamente.\n"
            f"¿Confirmar?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CONFIRM_WITHDRAW

async def handle_confirmations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja las confirmaciones de Apuestas y Retiros"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # --- CANCELACIONES ---
    if data == 'cancel_bet':
        if 'bet_info' in context.user_data: del context.user_data['bet_info']
        if 'temp_amount' in context.user_data: del context.user_data['temp_amount']
        await query.edit_message_text("Apuesta cancelada.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
        return ConversationHandler.END

    if data == 'cancel_withdraw':
        if 'temp_amount' in context.user_data: del context.user_data['temp_amount']
        await query.edit_message_text("Retiro cancelado.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
        return ConversationHandler.END

    # --- CONFIRMACIONES ---
    
    # Confirmar Apuesta
    if data == 'confirm_bet_yes':
        info = context.user_data['bet_info']
        amount = context.user_data['temp_amount']
        potential = amount * info['odds']
        
        success = db.place_bet(user_id, info['id'], info['sel'], info['odds'], amount, potential)
        
        if success:
            await query.edit_message_text(f"✅ ¡Apuesta realizada!\nGanancia posible: ${potential:.2f}")
        else:
            await query.edit_message_text("❌ Error al realizar apuesta.")
        
        # Limpieza
        if 'bet_info' in context.user_data: del context.user_data['bet_info']
        if 'temp_amount' in context.user_data: del context.user_data['temp_amount']
        
        await query.message.reply_text("Volviendo al menú principal...", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
        return ConversationHandler.END

    # Confirmar Retiro
    if data == 'confirm_withdraw_yes':
        amount = context.user_data['temp_amount']
        
        # Descontar saldo
        db.update_user_balance(user_id, -amount)
        
        # Crear solicitud
        trans_id = db.create_transaction(user_id, 'WITHDRAW', amount)
        
        # Notificar admin
        msg = (
            f"🔔 **SOLICITUD DE RETIRO**\n"
            f"👤 User ID: {user_id}\n"
            f"💰 Monto: ${amount}\n"
            f"🆔 Transacción ID: {trans_id}\n\n"
            f"Para aprobar, responde: `/aprobar {trans_id} ok`"
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode='Markdown')
            except:
                pass
        
        await query.edit_message_text("✅ Solicitud enviada. Espera aprobación.")
        
        if 'temp_amount' in context.user_data: del context.user_data['temp_amount']
        await query.message.reply_text("Volviendo al menú principal...", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
        return ConversationHandler.END

# --- HANDLERS PRINCIPALES ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.register_or_update_user(user.id, user.username, user.first_name)
    await update.message.reply_text(
        f"👋 Hola {user.first_name}, bienvenido a la casa de apuestas.",
        reply_markup=InlineKeyboardMarkup(get_main_keyboard())
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == 'bet_list':
        events = db.get_active_events()
        if not events:
            await query.edit_message_text("No hay eventos activos.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
            return
        
        text = "🏆 **Eventos Disponibles:**\n\n"
        keyboard = []
        for ev in events:
            text += f"⚽ *{ev['name']}*\n"
            text += f"1️⃣ ({ev['odds_local']}) | X ({ev['odds_draw']}) | 2️⃣ ({ev['odds_away']})\n\n"
            keyboard.append([
                InlineKeyboardButton(f"1 ({ev['odds_local']})", callback_data=f"bet_{ev['id']}_local_{ev['odds_local']}"),
                InlineKeyboardButton(f"X ({ev['odds_draw']})", callback_data=f"bet_{ev['id']}_draw_{ev['odds_draw']}"),
                InlineKeyboardButton(f"2 ({ev['odds_away']})", callback_data=f"bet_{ev['id']}_away_{ev['odds_away']}")
            ])
        keyboard.append([InlineKeyboardButton("⬅️ Volver", callback_data='back_menu')])
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == 'deposit_start':
        await query.edit_message_text(
            f"💳 **Datos Bancarios:**\n\n{BANK_DETAILS}\n\n"
            "Por favor, realiza la transferencia y envíame la **CAPTURA** ahora mismo.",
            parse_mode='Markdown'
        )
        return UPLOAD_PHOTO

    elif data == 'withdraw_start':
        balance = db.get_user_balance(user_id)
        if balance <= 0:
            await query.edit_message_text("No tienes saldo para retirar.", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))
            return ConversationHandler.END
        await query.edit_message_text(f"Tu saldo actual: ${balance}\n\nEscribe el monto que deseas retirar:")
        return AMOUNT

    elif data == 'my_balance':
        bal = db.get_user_balance(user_id)
        keyboard = [[InlineKeyboardButton("⬅️ Volver", callback_data='back_menu')]]
        await query.edit_message_text(f"💰 Tu saldo actual es: ${bal}", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == 'back_menu':
        await query.edit_message_text("Menú Principal", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))

    elif data.startswith('bet_'):
        parts = data.split('_')
        context.user_data['bet_info'] = {
            'id': int(parts[1]), 
            'sel': parts[2], 
            'odds': float(parts[3])
        }
        selection_text = parts[2].upper()
        await query.edit_message_text(
            f"Apuesta seleccionada: {selection_text} (Cuota: {parts[3]})\n\n"
            "¿Cuánto deseas apostar?"
        )
        return AMOUNT

# --- COMANDOS DE ADMINISTRADOR ---

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    help_text = (
        "⚙️ **Panel Admin**\n"
        "/crear_evento <Nombre> <C1> <CX> <C2>\n"
        "/aprobar <ID> <MONTO> (deposito)\n"
        "/aprobar <ID> ok (retiro)"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def cmd_create_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        args = context.args
        if len(args) < 5: raise ValueError
        odds_away = float(args[-1]); odds_draw = float(args[-2]); odds_local = float(args[-3])
        event_name = " ".join(args[:-3])
        db.create_event(event_name, odds_local, odds_draw, odds_away)
        await update.message.reply_text(f"✅ Evento creado:\n*{event_name}*", parse_mode='Markdown')
    except: await update.message.reply_text("❌ Error en datos.")

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /aprobar <ID> <MONTO/u ok>")
        return
    try:
        trans_id = int(context.args[0]); val2 = context.args[1]
        trans = db.get_transaction(trans_id)
        if not trans: raise ValueError("Transacción no encontrada")

        if trans['type'] == 'DEPOSIT':
            try:
                amount = float(val2)
                db.update_user_balance(trans['user_id'], amount)
                db.update_transaction_status(trans_id, 'APPROVED')
                await update.message.reply_text(f"✅ Depósito ${amount} aprobado.")
                try: await context.bot.send_message(chat_id=trans['user_id'], text=f"✅ Tu depósito de ${amount} fue validado.")
                except: pass
            except: await update.message.reply_text("Monto inválido.")

        elif trans['type'] == 'WITHDRAW':
            if val2.lower() in ['ok', 'si']:
                db.update_transaction_status(trans_id, 'APPROVED')
                await update.message.reply_text("✅ Retiro aprobado.")
                try: await context.bot.send_message(chat_id=trans['user_id'], text="✅ Tu retiro fue procesado.")
                except: pass
            else: await update.message.reply_text("Para retiros escribe 'ok'")
    except Exception as e: await update.message.reply_text(f"Error: {e}")

# --- SERVIDOR WEB ---

async def handle_health(request): return web.Response(text="Bot is alive")
async def run_web_server(app):
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()
    print(f"Web server started on port {os.environ.get('PORT', 10000)}")

def main():
    application = Application.builder().token(TOKEN).build()

    # Handlers Comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("crear_evento", cmd_create_event))
    application.add_handler(CommandHandler("aprobar", cmd_approve))

    # Handlers Botones
    application.add_handler(CallbackQueryHandler(button_handler))

    # Conversación Depósito (Foto -> Confirmación -> Fin)
    dep_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^deposit_start$')],
        states={
            UPLOAD_PHOTO: [MessageHandler(filters.PHOTO, handle_photo)],
            CONFIRM_DEPOSIT: [CallbackQueryHandler(confirm_deposit_action)]
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: u.message.reply_text("Cancelado.") or ConversationHandler.END)]
    )
    application.add_handler(dep_handler)

    # Conversación Apuestas (Seleccionar -> Monto -> Confirmación -> Fin)
    bet_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^bet_')],
        states={
            AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount),
                CallbackQueryHandler(handle_confirmations, pattern='^(confirm_bet_yes|cancel_bet)$')
            ]
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: u.message.reply_text("Cancelado.") or ConversationHandler.END)]
    )
    application.add_handler(bet_handler)

    # Conversación Retiro (Monto -> Confirmación -> Fin)
    wit_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^withdraw_start$')],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)],
            CONFIRM_WITHDRAW: [CallbackQueryHandler(handle_confirmations, pattern='^(confirm_withdraw_yes|cancel_withdraw)$')]
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: u.message.reply_text("Cancelado.") or ConversationHandler.END)]
    )
    application.add_handler(wit_handler)

    print("Iniciando Bot y Servidor Web...")
    web_app = web.Application()
    web_app.add_routes([web.get('/', handle_health)])
    loop = asyncio.get_event_loop()
    loop.create_task(run_web_server(web_app))
    application.run_polling()

if __name__ == '__main__':
    main()
