#!/usr/bin/env python3
"""
Bot webhook para WhatsApp Cloud API (Meta) + Telegram + Gemini Flash.

Pensado para desplegarse en Render.com (plan gratuito).
Recibe mensajes por WhatsApp o Telegram, consulta un JSON externo con
los datos de la paquetería, genera una respuesta con Gemini y la
devuelve por el mismo canal.

Variables de entorno requeridas (definirlas en Render, NO quemarlas en el código):
  - VERIFY_TOKEN     -> token de verificación del webhook de Meta
  - WHATSAPP_TOKEN   -> token de acceso de WhatsApp Cloud API
  - PHONE_NUMBER_ID  -> ID del número de teléfono remitente en Meta
  - GEMINI_API_KEY   -> API key de Google Gemini
  - GEMINI_MODEL     -> modelo de Gemini (opcional, por defecto gemini-flash-latest)
  - JSON_URL         -> URL pública del JSON con la base de datos de paquetería
  - TELEGRAM_TOKEN   -> token del bot de Telegram (de @BotFather)
  - PORT             -> puerto que asigna Render (por defecto 5000)
"""

import json
import os
import re
import sys
import time
import unicodedata

import requests
from flask import Flask, jsonify, request


app = Flask(__name__)

# ------------------------------------------------------------
# Configuración: todas las credenciales desde variables de entorno
# ------------------------------------------------------------
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
JSON_URL = os.environ.get("JSON_URL")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# Versión de la API de Graph de Meta indicada en la consigna
GRAPH_API_VERSION = "v17.0"


# ------------------------------------------------------------
# Funciones auxiliares
# ------------------------------------------------------------
def fetch_paqueteria_data():
    """Descarga el JSON de la paquetería desde JSON_URL y lo devuelve como texto.

    No rompe el webhook si la petición falla: devuelve "{}" para que
    Gemini pueda responder igualmente indicando que no hay datos.
    """
    if not JSON_URL:
        print("ERROR: JSON_URL no está definida.", file=sys.stderr)
        return "{}"
    try:
        response = requests.get(JSON_URL, timeout=10)
        response.raise_for_status()
        # Validamos que sea JSON válido y lo normalizamos.
        data = response.json()
        return json.dumps(data, ensure_ascii=False)
    except requests.RequestException as exc:
        print(f"ERROR al descargar JSON_URL: {exc}", file=sys.stderr)
        return "{}"
    except ValueError as exc:
        # response.json() falló -> no era JSON válido
        print(f"ERROR: JSON_URL no devolvió JSON válido: {exc}", file=sys.stderr)
        return "{}"


def _norm(text):
    """Minúsculas sin tildes para comparar nombres, guías y teléfonos."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


def _digits(text):
    return re.sub(r"\D", "", str(text or ""))


def filter_relevant_json(full_data, user_message, sender_id=None):
    """Devuelve solo los envíos/paquetes que coinciden con lo que pide el usuario.

    Prioridad:
    1. Si el remitente (WhatsApp) coincide con un teléfono del JSON, solo sus envíos.
    2. Si el mensaje menciona guía (ENV-2), nombre, teléfono, provincia o fecha, solo esos envíos.
    3. Si menciona tarifas/precios/estados en general, devuelve contexto general sin clientes.
    4. Si no filtra nada, devuelve todo para que Gemini responda lo que puede.

    Esto evita que un cliente vea datos de otros y reduce tokens.
    """
    try:
        envios = full_data.get("envios") or []

        # 1) Coincidencia por remitente (solo WhatsApp tiene phone_number)
        if sender_id:
            sender_digits = _digits(sender_id)
            if len(sender_digits) >= 7:
                por_remitente = [
                    e for e in envios
                    if any(
                        sender_digits in _digits(c.get("phone", "")) or sender_digits in _digits(c.get("recipient_phone", ""))
                        for c in e.get("customers", [])
                    )
                ]
                if por_remitente:
                    return {**full_data, "envios": por_remitente}

        # 2) Coincidencia por texto del mensaje
        mensaje_norm = " " + _norm(user_message) + " "
        mensaje_digits = _digits(user_message)
        filtrados = []
        for envio in envios:
            texto_envio = " ".join([
                str(envio.get("id", "")),
                str(envio.get("date", "")),
                str(envio.get("status", "")),
            ]) + " " + " ".join(
                f"{c.get('name','')} {c.get('recipient_name','')} {c.get('phone','')} {c.get('recipient_phone','')} {c.get('province','')} {c.get('origin_state','')}"
                for c in envio.get("customers", [])
            )
            envio_norm = _norm(texto_envio)
            # guía explícita ENV-2
            if f"env {envio.get('id','')}" in mensaje_norm or f"envio {envio.get('id','')}" in mensaje_norm:
                filtrados.append(envio)
                continue
            # coincidencia por teléfono mencionado
            if mensaje_digits and any(
                len(d) >= 7 and d in mensaje_digits
                for d in [_digits(c.get("phone", "")), _digits(c.get("recipient_phone", ""))]
                if d
            ):
                filtrados.append(envio)
                continue
            # coincidencia por nombre / provincia
            for c in envio.get("customers", []):
                for campo in [c.get("name", ""), c.get("recipient_name", ""), c.get("province", "")]:
                    nombre = _norm(campo).strip()
                    if len(nombre) >= 4 and nombre in mensaje_norm:
                        filtrados.append(envio)
                        break
                else:
                    continue
                break

        if filtrados:
            return {**full_data, "envios": filtrados}

        # 3) Pregunta general (tarifas, estados, categorías) -> contexto sin exponer todos los clientes
        if any(p in mensaje_norm for p in ["tarifa", "precio", "cuesta", "vale", "estado", "categoria", "categorias", "documento", "miscelanea", "aseo", "comida", "peso", "kilo", "envios", "cuando", "fecha"]):
            return {
                "paqueteria": full_data.get("paqueteria", ""),
                "precios": full_data.get("precios", {}),
                "estados_posibles": full_data.get("estados_posibles", []),
                "total_envios": len(envios),
            }
    except Exception as exc:
        print(f"Filtro de JSON omitido: {exc}", file=sys.stderr)
    return full_data


def generate_response(user_message, paqueteria_json_text, sender_id=None):
    """Genera la respuesta usando Gemini Flash.

    Si Gemini falla (sin API key, red, cuota, etc.), devuelve una
    respuesta de respaldo amigable en lugar de romper.
    """
    fallback = (
        "Hola, soy el asistente de la paquetería. "
        "En este momento no puedo consultar el estado de tu envío. "
        "Por favor, inténtalo de nuevo en unos minutos o contacta con nosotros."
    )
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY no está definida.", file=sys.stderr)
        return fallback
    try:
        # Filtrar el JSON según quién pregunta y qué pregunta.
        try:
            full_data = json.loads(paqueteria_json_text)
            relevant_data = filter_relevant_json(full_data, user_message, sender_id)
            relevant_text = json.dumps(relevant_data, ensure_ascii=False)
        except Exception:
            relevant_text = paqueteria_json_text
        # Prompt exacto exigido, con los datos del JSON insertados.
        prompt = (
            "Eres un asistente amable de una paquetería. "
            "El usuario pregunta sobre su envío. "
            "Responde basándote EXCLUSIVAMENTE en el siguiente JSON: "
            f"{relevant_text}\n\n"
            f"Mensaje del usuario: {user_message}\n\n"
            "Responde solo con la información relevante para ESTE usuario y esta pregunta. "
            "No muestres datos de otros clientes ni vuelques todo el JSON. "
            "Sé breve y directo."
        )
        # Llamada REST directa a Gemini (sin grpcio) para no agotar la memoria free.
        # Llave por header (no por URL) para que no se filtre en logs. Reintento ante 429.
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        response = None
        last_exc = None
        for attempt in range(3):
            try:
                response = requests.post(url, headers=headers, json=body, timeout=30)
                if response.status_code not in (429, 500, 503):
                    response.raise_for_status()
                    break
                if attempt == 2:
                    response.raise_for_status()
                wait = 2 * (attempt + 1) + (int(response.headers.get("Retry-After", "0") or 0) if response.headers.get("Retry-After") else 0)
                print(f"Gemini {response.status_code}, reintentando en {wait}s (intento {attempt+1}/3)", file=sys.stderr)
                time.sleep(wait)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        if response is None:
            raise last_exc or RuntimeError("Sin respuesta de Gemini")
        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return fallback
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts).strip()
        return text or fallback
    except Exception as exc:  # Gemini no puede romper el webhook
        print(f"ERROR en Gemini: {exc}", file=sys.stderr)
        return fallback


def send_whatsapp_message(to_number, message_text):
    """Envía un mensaje de texto por WhatsApp Cloud API (Graph de Meta)."""
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print(
            "ERROR: faltan WHATSAPP_TOKEN o PHONE_NUMBER_ID, no se envía el mensaje.",
            file=sys.stderr,
        )
        return

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message_text[:4096]},  # límite de WhatsApp
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        print(f"Mensaje WhatsApp enviado a {to_number}: {response.status_code}")
    except requests.RequestException as exc:
        print(f"ERROR al enviar mensaje por WhatsApp: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"ERROR inesperado al enviar WhatsApp: {exc}", file=sys.stderr)


def send_telegram_message(chat_id, message_text):
    """Envía un mensaje de texto por la API oficial de Telegram Bot."""
    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_TOKEN no está definida.", file=sys.stderr)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message_text[:4096],  # límite de Telegram
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"Mensaje Telegram enviado a {chat_id}: {response.status_code}")
    except requests.RequestException as exc:
        print(f"ERROR al enviar mensaje por Telegram: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"ERROR inesperado al enviar Telegram: {exc}", file=sys.stderr)


def extract_whatsapp_sender_and_message(payload):
    """Extrae (remitente, texto) del payload entrante de WhatsApp.

    Devuelve (None, None) si no hay mensaje de texto (ej. solo
    actualizaciones de estado, stickers, audios, etc.).
    """
    try:
        entry = (payload.get("entry") or [{}])[0]
        changes = entry.get("changes") or [{}]
        value = changes[0].get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return None, None

        msg = messages[0]
        sender = msg.get("from")
        # Solo respondemos a mensajes de texto.
        text_obj = msg.get("text") or {}
        user_text = (text_obj.get("body") or "").strip()

        if not sender or not user_text:
            return None, None
        return sender, user_text
    except (AttributeError, IndexError, TypeError) as exc:
        print(f"ERROR al extraer mensaje de WhatsApp: {exc}", file=sys.stderr)
        return None, None


def extract_telegram_sender_and_message(payload):
    """Extrae (chat_id, texto) del payload entrante de Telegram.

    Telegram envía updates con message.chat.id y message.text
    (también puede venir como edited_message).
    """
    try:
        message = payload.get("message") or payload.get("edited_message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        user_text = (message.get("text") or "").strip()

        if not chat_id or not user_text:
            return None, None
        return chat_id, user_text
    except (AttributeError, TypeError) as exc:
        print(f"ERROR al extraer mensaje de Telegram: {exc}", file=sys.stderr)
        return None, None


# ------------------------------------------------------------
# Rutas
# ------------------------------------------------------------
@app.route("/", methods=["GET"])
def home():
    """Ruta de salud para comprobar que el servicio está en línea en Render."""
    return jsonify({"status": "ok", "service": "paquetria-bot", "whatsapp": True, "telegram": True}), 200


# ----- WhatsApp (Meta) -----
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificación estándar del webhook de Meta."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return jsonify({"error": "Forbidden"}), 403


@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe mensajes de WhatsApp, responde con Gemini y devuelve.

    Siempre responde 200 EVENT_RECEIVED si el payload es válido, incluso
    si Gemini o las peticiones HTTP fallan, para que Meta no reintente
    en bucle y el webhook no se caiga.
    """
    try:
        data = request.get_json(silent=True) or {}
    except Exception as exc:
        print(f"ERROR: JSON inválido en webhook: {exc}", file=sys.stderr)
        return jsonify({"status": "EVENT_RECEIVED"}), 200

    # Ignora actualizaciones de estado y eventos sin mensaje de texto.
    sender, user_text = extract_whatsapp_sender_and_message(data)
    if sender is None or user_text is None:
        return jsonify({"status": "EVENT_RECEIVED"}), 200

    print(f"Mensaje WhatsApp de {sender}: {user_text}")

    # 1) Consultar la base de datos externa (JSON) y cargarla en memoria.
    paqueteria_data = fetch_paqueteria_data()

    # 2) Procesar con Gemini Flash (solo datos de este remitente / esta pregunta).
    reply = generate_response(user_text, paqueteria_data, sender)

    # 3) Enviar la respuesta de vuelta por WhatsApp.
    send_whatsapp_message(sender, reply)

    return jsonify({"status": "EVENT_RECEIVED"}), 200


# ----- Telegram -----
@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Recibe mensajes de Telegram, responde con Gemini y devuelve.

    Siempre responde 200 {"ok": true} aunque falle algo, para que
    Telegram no reintente en bucle.
    """
    try:
        data = request.get_json(silent=True) or {}
    except Exception as exc:
        print(f"ERROR: JSON inválido en telegram-webhook: {exc}", file=sys.stderr)
        return jsonify({"ok": True}), 200

    chat_id, user_text = extract_telegram_sender_and_message(data)
    if chat_id is None or user_text is None:
        return jsonify({"ok": True}), 200

    print(f"Mensaje Telegram de {chat_id}: {user_text}")

    # 1) Mismo JSON externo.
    paqueteria_data = fetch_paqueteria_data()

    # 2) Mismo Gemini Flash (solo datos de esta pregunta).
    reply = generate_response(user_text, paqueteria_data, None)

    # 3) Respuesta por Telegram.
    send_telegram_message(chat_id, reply)

    return jsonify({"ok": True}), 200


# ------------------------------------------------------------
# Arranque para Render
# ------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
