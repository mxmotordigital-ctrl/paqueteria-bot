#!/usr/bin/env python3
"""
Bot webhook para WhatsApp Cloud API (Meta) + Gemini 1.5 Flash.

Pensado para desplegarse en Render.com (plan gratuito).
Recibe mensajes por WhatsApp, consulta un JSON externo con los datos
de la paquetería, genera una respuesta con Gemini y la devuelve
por WhatsApp usando la API de Graph de Meta.

Variables de entorno requeridas (definirlas en Render, NO quemarlas en el código):
  - VERIFY_TOKEN     -> token de verificación del webhook de Meta
  - WHATSAPP_TOKEN   -> token de acceso de WhatsApp Cloud API
  - PHONE_NUMBER_ID  -> ID del número de teléfono remitente en Meta
  - GEMINI_API_KEY   -> API key de Google Gemini
  - JSON_URL         -> URL pública del JSON con la base de datos de paquetería
  - PORT             -> puerto que asigna Render (por defecto 5000)
"""

import json
import os
import sys

import requests
from flask import Flask, jsonify, request

import google.generativeai as genai


app = Flask(__name__)

# ------------------------------------------------------------
# Configuración: todas las credenciales desde variables de entorno
# ------------------------------------------------------------
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
JSON_URL = os.environ.get("JSON_URL")

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


def generate_response(user_message, paqueteria_json_text):
    """Genera la respuesta usando Gemini 1.5 Flash.

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
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        # Prompt exacto exigido, con los datos del JSON insertados.
        prompt = (
            "Eres un asistente amable de una paquetería. "
            "El usuario pregunta sobre su envío. "
            "Responde basándote EXCLUSIVAMENTE en el siguiente JSON: "
            f"{paqueteria_json_text}\n\n"
            f"Mensaje del usuario: {user_message}"
        )

        result = model.generate_content(prompt)
        text = (getattr(result, "text", "") or "").strip()
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
        print(f"Mensaje enviado a {to_number}: {response.status_code}")
    except requests.RequestException as exc:
        print(f"ERROR al enviar mensaje por WhatsApp: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"ERROR inesperado al enviar WhatsApp: {exc}", file=sys.stderr)


def extract_sender_and_message(payload):
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
        print(f"ERROR al extraer mensaje: {exc}", file=sys.stderr)
        return None, None


# ------------------------------------------------------------
# Rutas
# ------------------------------------------------------------
@app.route("/", methods=["GET"])
def home():
    """Ruta de salud para comprobar que el servicio está en línea en Render."""
    return jsonify({"status": "ok", "service": "whatsapp-paquetria-bot"}), 200


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
    sender, user_text = extract_sender_and_message(data)
    if sender is None or user_text is None:
        return jsonify({"status": "EVENT_RECEIVED"}), 200

    print(f"Mensaje de {sender}: {user_text}")

    # 1) Consultar la base de datos externa (JSON) y cargarla en memoria.
    paqueteria_data = fetch_paqueteria_data()

    # 2) Procesar con Gemini 1.5 Flash.
    reply = generate_response(user_text, paqueteria_data)

    # 3) Enviar la respuesta de vuelta por WhatsApp.
    send_whatsapp_message(sender, reply)

    return jsonify({"status": "EVENT_RECEIVED"}), 200


# ------------------------------------------------------------
# Arranque para Render
# ------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
