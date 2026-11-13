"""Agente de preguntas básicas de Red Salud Andina (ficticia).

Configuración (variables de entorno o secretos del Space):
  GEMINI_API_KEY               obligatoria
  GEMINI_MODEL                 opcional (por defecto gemini-3.6-flash)
  GEMINI_MODEL_RESPALDO        opcional (por defecto gemini-2.5-flash)
Las preguntas sin respuesta se guardan en registro.csv (fecha, pregunta, tipo).
En un Space gratuito ese disco es efímero: el archivo se borra al reiniciar.
"""
import csv
import datetime
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import gradio as gr
from google import genai
from google.genai import types
from pydantic import BaseModel

try:  # solo para pruebas locales con archivo .env
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE = Path(__file__).parent
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
MODEL_RESPALDO = os.environ.get("GEMINI_MODEL_RESPALDO", "gemini-2.5-flash")
ULTIMO_MODELO = None  # modelo que dio la última respuesta (lo usa eval/correr.py)

# Lista ordenada de modelos a probar. Puedes definirla completa con GEMINI_MODELOS
# (separados por coma); si no, se usan principal, respaldo y un tercero.
# Cada modelo tiene su PROPIA cuota diaria gratuita, así que más modelos = más capacidad.
_lista = [m.strip() for m in os.environ.get("GEMINI_MODELOS", "").split(",") if m.strip()]
MODELOS = list(dict.fromkeys(_lista or [MODEL, MODEL_RESPALDO, "gemini-3.5-flash-lite"]))
AGOTADOS = {}  # modelo -> momento hasta el cual se salta (cuota diaria agotada o no existe)

SYSTEM_PROMPT = (BASE / "prompt.md").read_text(encoding="utf-8").replace(
    "{{KB}}", (BASE / "kb.md").read_text(encoding="utf-8")
)

CANAL_RESPALDO = (
    "Puedes hablar con un asesor en el call center (601) 555 0100 "
    "(L–V 7:00–19:00, sáb 8:00–14:00) o escribir en el formulario del portal: "
    "citas.redsaludandina.example."
)

# timeout en milisegundos: si Gemini no responde en 20 s, falla y se reintenta
client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(timeout=20000),
)


class Salida(BaseModel):
    respuesta: str
    derivar: bool
    registrar: bool
    tipo: Optional[str] = None
    motivo: Optional[str] = None


# ---------- utilidades ----------
def _texto(contenido) -> str:
    """Gradio puede entregar el contenido como texto o como lista de partes."""
    if isinstance(contenido, str):
        return contenido
    if isinstance(contenido, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in contenido
        )
    return str(contenido or "")


def _normalizar_historial(history):
    """Devuelve una lista de (rol, texto) con rol 'user' o 'model'."""
    salida = []
    for item in history or []:
        if isinstance(item, dict):
            rol = "user" if item.get("role") == "user" else "model"
            salida.append((rol, _texto(item.get("content"))))
        elif isinstance(item, (list, tuple)) and len(item) == 2:  # formato antiguo
            salida.append(("user", _texto(item[0])))
            if item[1]:
                salida.append(("model", _texto(item[1])))
    return salida[-12:]  # limita el contexto


def limpiar_pregunta(texto: str) -> str:
    """Quita correos y números largos antes de guardar. No debe llegar nada personal."""
    texto = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[correo]", texto)
    texto = re.sub(r"\d[\d\s.\-]{4,}\d", "[número]", texto)
    # nombres declarados: "me llamo Ana Pérez", "mi nombre es Carlos", "soy María López"
    texto = re.sub(
        r"(?i:(\b(?:me llamo|mi nombre es|soy)\s+))(?:[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+\s?){1,3}",
        r"\1[nombre] ",
        texto,
    )
    return texto.strip()[:300]


def tiene_canal(texto: str) -> bool:
    return "555 0100" in texto or "citas.redsaludandina" in texto


# ---------- registro de preguntas sin respuesta ----------
_BLOQUEO = threading.Lock()  # evita escrituras simultáneas al mismo archivo


def registrar_pregunta(pregunta: str, tipo: str) -> None:
    """Agrega una fila (fecha, pregunta, tipo) a registro.csv. Solo texto de la pregunta."""
    fila = [datetime.datetime.now().isoformat(timespec="seconds"), pregunta, tipo]
    ruta = BASE / "registro.csv"
    with _BLOQUEO:
        nuevo = not ruta.exists()
        # utf-8-sig para que Excel muestre bien las tildes
        with ruta.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if nuevo:
                w.writerow(["fecha", "pregunta", "tipo"])
            w.writerow(fila)


# ---------- modelo ----------
def llamar_modelo(mensaje: str, history) -> Optional[Salida]:
    global ULTIMO_MODELO
    ULTIMO_MODELO = None
    contenidos = [
        types.Content(role=rol, parts=[types.Part(text=txt)])
        for rol, txt in _normalizar_historial(history)
        if txt
    ]
    contenidos.append(types.Content(role="user", parts=[types.Part(text=mensaje)]))
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=Salida,
        temperature=0.2,
    )
    # Recorre los modelos en orden. Si la cuota diaria de uno se agotó (429 por día) o el
    # modelo no existe (404), se salta de inmediato; ante un 503 o un límite por minuto
    # se reintenta hasta 3 veces con espera creciente.
    for modelo in MODELOS:
        if AGOTADOS.get(modelo, 0) > time.time():
            continue
        for intento in range(3):
            try:
                r = client.models.generate_content(model=modelo, contents=contenidos, config=config)
                salida = Salida(**json.loads(r.text))
                ULTIMO_MODELO = modelo
                return salida
            except Exception as e:  # noqa: BLE001
                error = str(e)
                print(f"Error del modelo {modelo} (intento {intento + 1}):", error[:200])
                if "429" in error and ("PerDay" in error or "per day" in error.lower()):
                    AGOTADOS[modelo] = time.time() + 3600  # no insistir durante 1 hora
                    break
                if "404" in error or "NOT_FOUND" in error:
                    AGOTADOS[modelo] = time.time() + 86400
                    break
                time.sleep(1.5 * (intento + 1))
    return None


def hay_modelo_disponible() -> bool:
    return any(AGOTADOS.get(m, 0) <= time.time() for m in MODELOS)


def responder(mensaje: str, history):
    salida = llamar_modelo(mensaje, history)
    if salida is None:
        return "Tuve un problema técnico y no pude responderte. " + CANAL_RESPALDO

    texto = salida.respuesta.strip()
    if salida.derivar and not tiene_canal(texto):
        texto += "\n" + CANAL_RESPALDO

    if salida.registrar:
        tipo = salida.tipo if salida.tipo in ("brecha", "ambigua") else "brecha"
        previos = [t for r, t in _normalizar_historial(history) if r == "user"]
        pregunta = " | ".join((previos[-1:] if tipo == "ambigua" else []) + [mensaje])
        try:
            registrar_pregunta(limpiar_pregunta(pregunta), tipo)
        except Exception as e:  # el registro nunca debe romper la conversación
            print("Error al registrar:", repr(e))
    return texto


# ---------- interfaz ----------
demo = gr.ChatInterface(
    fn=responder,
    title="Asistente de Red Salud Andina",
    description=(
        "Resuelvo dudas sobre sedes, horarios, citas, requisitos y cuotas. "
        "No agendo citas ni doy consejo médico. Por favor, no escribas datos personales."
    ),
    examples=[
        "¿Qué necesito llevar a una cita de dermatología?",
        "¿Qué horario tiene el laboratorio en la sede Sur?",
        "¿Dónde puedo ver mis resultados?",
    ],
)

if __name__ == "__main__":
    # server_name="0.0.0.0" y PORT son necesarios en plataformas como Render;
    # en local, sin PORT, sigue usando el 7860 de siempre.
    demo.launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))