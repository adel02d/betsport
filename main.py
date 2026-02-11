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

# ESTADOS DE CONVERSACIÓN
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
            await query.edit_message_text("No hay eventos activos. Pide al admin que cree uno.")
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
            await query.edit_message_text("No tienes saldo para retirar.")
            return ConversationHandler.END
        await query.edit_message_text(f"Tu saldo actual: ${balance}\n\nEscribe el monto que deseas retirar:")
        return AMOUNT

    elif data == 'my_balance':
        bal = db.get_user_balance(user_id)
        await query.edit_message_text(f"💰 Tu saldo actual es: ${bal}")
    
    elif data == 'back_menu':
        await query.edit_message_text("Menú Principal", reply_markup=InlineKeyboardMarkup(get_main_keyboard()))

    elif data.startswith('bet_'):
        parts = data.split('_')
        # Formato: bet_EVENTID_SELECTION_ODDS
        context.user_data['bet_info'] = {
            'id': int(parts[1]), 
            'sel': parts[2], 
            'odds': float(parts[3])
        }
        selection_text = parts[2].upper() # LOCAL, DRAW, AWAY
        await query.edit_message_text(
            f"Apuesta seleccionada: {selection_text} (Cuota: {parts[3]})\n\n"
            "¿Cuánto deseas apostar?"
        )
        return AMOUNT

# --- MANEJO DE TEXTOS (Monto Apuesta / Retiro) ---

async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Monto inválido. Por favor ingresa un número positivo.")
        return AMOUNT

    user_id = update.effective_user.id

    # CASO A: ES UNA APUESTA
    if 'bet_info' in context.user_data:
        info = context.user_data['bet_info']
        
        # Validar saldo
        if amount > db.get_user_balance(user_id):
            await update.message.reply_text("❌ Saldo insuficiente para realizar esta apuesta.")
            return ConversationHandler.END
        
        potential = amount * info['odds']
        success = db.place_bet(user_id, info['id'], info['sel'], info['odds'], amount, potential)
        
        if success:
            await update.message.reply_text(
                f"✅ ¡Apuesta realizada con éxito!\n"
                f"Monto: ${amount}\n"
                f"Ganancia Potencial: ${potential:.2f}"
            )
        else:
            await update.message.reply_text("❌ Hubo un error al procesar tu apuesta. Inténtalo de nuevo.")
        
        # Limpiar datos temporales
        if 'bet_info' in context.user_data:
            del context.user_data['bet_info']
        return ConversationHandler.END

    # CASO B: ES UN RETIRO
    else:
        if amount > db.get_user_balance(user_id):
            await update.message.reply_text("❌ Saldo insuficiente.")
            return ConversationHandler.END
        
        # 1. Descontar saldo inmediatamente (bloqueo de fondos)
        db.update_user_balance(user_id, -amount)
        
        # 2. Crear solicitud de retiro
        trans_id = db.create_transaction(user_id, 'WITHDRAW', amount)
        
        # 3. Notificar al Admin
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
            except Exception as e:
                print(f"Error notificando admin: {e}")
            
        await update.message.reply_text("✅ Solicitud de retiro enviada. Espera aprobación del administrador.")
        return ConversationHandler.END

# --- MANEJO DE FOTOS (DEPÓSITOS) - CAMBIO PRINCIPAL ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # 1. Obtener el objeto de la foto (la más grande/resolución)
    photo_obj = update.message.photo[-1]
    
    # 2. Obtener el File ID de Telegram
    # ESTO ES LO CLAVE: No descargamos el archivo. Usamos el ID que nos da Telegram.
    file_id = photo_obj.file_id
    
    # 3. Crear la transacción en la base de datos
    # No guardamos ruta de archivo (None) porque no la guardamos en disco.
    trans_id = db.create_transaction(user_id, 'DEPOSIT', 0, photo_path=None)
    
    # 4. Reenviar la foto directamente al Administrador usando el File ID
    caption = (
        f"🔔 **NUEVO DEPÓSITO**\n"
        f"👤 Usuario: {update.effective_user.first_name} (@{update.effective_user.username})\n"
        f"🆔 ID Transacción: {trans_id}\n\n"
        f"Verifica el monto en la imagen y apruébalo."
    )
    
    for admin_id in ADMIN_IDS:
        try:
            # Enviamos la foto usando su ID interno (sin descargar ni guardar en servidor)
            await context.bot.send_photo(
                chat_id=admin_id, 
                photo=file_id, 
                caption=caption, 
                parse_mode='Markdown'
            )
            # Enviamos instrucciones de comando al admin
            await context.bot.send_message(
                chat_id=admin_id, 
                text=f"Para acreditar saldo, usa:\n`/aprobar {trans_id} <MONTO_VISTO>`", 
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"Error enviando foto al admin {admin_id}: {e}")

    # 5. Confirmar al usuario
    await update.message.reply_text("📸 Comprobante recibido. Enviado al administrador para validación.")
    return ConversationHandler.END

# --- COMANDOS DE ADMINISTRADOR ---

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Acceso denegado.")
        return
    
    help_text = (
        "⚙️ **Panel de Administrador**\n\n"
        "Comandos disponibles:\n"
        "1. `/crear_evento <Nombre> <Cuota1> <CuotaX> <Cuota2>`\n"
        "   Ejemplo: /crear_evento Real Madrid vs Barca 1.90 3.40 4.00\n\n"
        "2. `/aprobar <ID> <MONTO>` (Para depósitos)\n"
        "   Ejemplo: /aprobar 1 500\n\n"
        "3. `/aprobar <ID> ok` (Para retiros)\n"
        "   Ejemplo: /aprobar 2 ok"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def cmd_create_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    
    try:
        args = context.args
        if len(args) < 5:
            await update.message.reply_text("❌ Faltan datos.\nUso: /crear_evento <Nombre> <C1> <CX> <C2>")
            return
        
        # Asumimos que las últimas 3 palabras son números (cuotas) y el resto es el nombre
        odds_away = float(args[-1])
        odds_draw = float(args[-2])
        odds_local = float(args[-3])
        event_name = " ".join(args[:-3]) # Unir el resto como nombre del evento

        db.create_event(event_name, odds_local, odds_draw, odds_away)
        await update.message.reply_text(f"✅ Evento creado exitosamente:\n*{event_name}*", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ Error: Las cuotas deben ser números.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error desconocido: {e}")

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    
    if len(context.args) < 2:
        await update.message.reply_text("❌ Uso incorrecto.\nDepósito: /aprobar <ID> <MONTO>\nRetiro: /aprobar <ID> ok")
        return

    try:
        trans_id = int(context.args[0])
        val2 = context.args[1]
        trans = db.get_transaction(trans_id)
        
        if not trans:
            await update.message.reply_text("❌ Transacción no encontrada.")
            return

        # LÓGICA DEPÓSITO
        if trans['type'] == 'DEPOSIT':
            try:
                amount = float(val2)
                # Aumentar saldo al usuario
                db.update_user_balance(trans['user_id'], amount)
                # Marcar transacción como aprobada
                db.update_transaction_status(trans_id, 'APPROVED')
                
                await update.message.reply_text(f"✅ Depósito de ${amount} aprobado. Saldo actualizado.")
                # Notificar al usuario
                try:
                    await context.bot.send_message(
                        chat_id=trans['user_id'], 
                        text=f"✅ ¡Tu depósito de ${amount} ha sido validado y acreditado!"
                    )
                except:
                    pass # El usuario puede haber bloqueado al bot
            except ValueError:
                await update.message.reply_text("❌ El monto debe ser un número válido.")

        # LÓGICA RETIRO
        elif trans['type'] == 'WITHDRAW':
            if val2.lower() in ['ok', 'si', 'aceptar']:
                # El saldo ya fue descontado al solicitar el retiro, solo marcamos como aprobado
                db.update_transaction_status(trans_id, 'APPROVED')
                
                await update.message.reply_text("✅ Retiro aprobado y marcado como pagado.")
                # Notificar al usuario
                try:
                    await context.bot.send_message(
                        chat_id=trans['user_id'], 
                        text="✅ Tu solicitud de retiro ha sido procesada exitosamente."
                    )
                except:
                    pass
            else:
                await update.message.reply_text("❌ Para retiros debes escribir 'ok' al final.\nEj: /aprobar 5 ok")

    except Exception as e:
        await update.message.reply_text(f"❌ Error procesando solicitud: {e}")

def main():
    app = Application.builder().token(TOKEN).build()

    # Registrar Comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("crear_evento", cmd_create_event))
    app.add_handler(CommandHandler("aprobar", cmd_approve))

    # Registrar Botones del Menú
    app.add_handler(CallbackQueryHandler(button_handler))

    # Conversación Depósito (Manejo de FOTO)
    dep_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^deposit_start$')],
        states={
            UPLOAD_PHOTO: [MessageHandler(filters.PHOTO, handle_photo)]
        },
        fallbacks=[CommandHandler('cancel', lambda u, c: u.message.reply_text("Cancelado.") or ConversationHandler.END)]
    )
    app.add_handler(dep_handler)

    # Conversación Apuestas (Manejo de MONTO)
    bet_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^bet_')],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)]
        },
        fallbacks=[CommandHandler('cancel', lambda u, c: u.message.reply_text("Cancelado.") or ConversationHandler.END)]
    )
    app.add_handler(bet_handler)

    # Conversación Retiro (Manejo de MONTO)
    wit_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^withdraw_start$')],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)]
        },
        fallbacks=[CommandHandler('cancel', lambda u, c: u.message.reply_text("Cancelado.") or ConversationHandler.END)]
    )
    app.add_handler(wit_handler)

    print("Bot iniciado y listo para apostar...")
    app.run_polling()

if __name__ == '__main__':
    main()
