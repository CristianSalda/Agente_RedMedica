"""Ejecuta los casos de eval/casos.csv contra el agente y guarda eval/resultados.csv.

- No escribe en el registro real de preguntas: solo anota si el modelo pidió registrar.
- Las columnas de calificación (correcta, invento, ...) se llenan A MANO después.
- Los casos con dos turnos se separan con "||" en la columna "mensajes".
"""
import csv
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import app  # carga prompt, base de conocimiento y cliente; no lanza la interfaz

PAUSA = 4  # segundos entre llamadas, para no pasar el límite del nivel gratuito
ENTRADA = RAIZ / "eval" / "casos.csv"
SALIDA = RAIZ / "eval" / "resultados.csv"

CAMPOS = [
    "id", "categoria", "elegible_impacto", "mensajes", "conversacion", "n_turnos",
    "palabras_ultima", "lineas_ultima", "derivar_modelo", "registrar_modelo", "tipos",
    "motivo", "modelos_usados", "json_ok",
    "resultado_esperado", "debe_derivar", "debe_registrar",
    "derivar_coincide", "registrar_coincide",
    # --- calificación manual (0 o 1, salvo comentarios) ---
    "correcta", "invento", "max_4_lineas", "resolvio", "derivacion_ok",
    "registro_ok", "pidio_dato_personal", "mensajes_hasta_necesidad", "comentarios",
]


def si_no(valor: bool) -> str:
    return "si" if valor else "no"


def correr_caso(caso: dict) -> dict:
    historial, transcripcion = [], []
    derivar = registrar = False
    tipos, motivos, modelos = [], [], []
    json_ok = True
    texto = ""

    for mensaje in [m.strip() for m in caso["mensajes"].split("||")]:
        salida = app.llamar_modelo(mensaje, historial)
        if salida is None:
            json_ok = False
            texto = "[SIN RESPUESTA DEL MODELO]"
        else:
            texto = salida.respuesta.strip()
            if salida.derivar and not app.tiene_canal(texto):
                texto += "\n" + app.CANAL_RESPALDO  # misma lógica que app.responder
            derivar = derivar or salida.derivar
            registrar = registrar or salida.registrar
            if salida.registrar and salida.tipo:
                tipos.append(salida.tipo)
            if salida.motivo:
                motivos.append(salida.motivo)
            modelos.append(app.ULTIMO_MODELO or "?")

        historial += [
            {"role": "user", "content": mensaje},
            {"role": "assistant", "content": texto},
        ]
        transcripcion.append(f"USUARIO: {mensaje}\nAGENTE: {texto}")
        time.sleep(PAUSA)

    lineas = len([l for l in texto.split("\n") if l.strip()])
    fila = {c: "" for c in CAMPOS}
    fila.update({
        "id": caso["id"],
        "categoria": caso["categoria"],
        "elegible_impacto": caso["elegible_impacto"],
        "mensajes": caso["mensajes"],
        "conversacion": "\n\n".join(transcripcion),
        "n_turnos": len(transcripcion),
        "palabras_ultima": len(texto.split()),
        "lineas_ultima": lineas,
        "derivar_modelo": si_no(derivar),
        "registrar_modelo": si_no(registrar),
        "tipos": ",".join(tipos),
        "motivo": " | ".join(motivos),
        "modelos_usados": ",".join(sorted(set(modelos))),
        "json_ok": si_no(json_ok),
        "resultado_esperado": caso["resultado_esperado"],
        "debe_derivar": caso["debe_derivar"],
        "debe_registrar": caso["debe_registrar"],
        "derivar_coincide": si_no(si_no(derivar) == caso["debe_derivar"]),
        "registrar_coincide": si_no(si_no(registrar) == caso["debe_registrar"]),
    })
    return fila


def main():
    with ENTRADA.open(encoding="utf-8-sig", newline="") as f:
        casos = list(csv.DictReader(f))

    filas = []
    for caso in casos:
        print(f"Caso {caso['id']} ({caso['categoria']})...")
        filas.append(correr_caso(caso))

    with SALIDA.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        w.writerows(filas)

    n = len(filas)
    print(f"\nListo: {n} casos guardados en {SALIDA}")
    print("Sin respuesta del modelo:", sum(f["json_ok"] == "no" for f in filas))
    print("Derivación coincide:", sum(f["derivar_coincide"] == "si" for f in filas), "/", n)
    print("Registro coincide:", sum(f["registrar_coincide"] == "si" for f in filas), "/", n)
    usados = {}
    for f in filas:
        for m in f["modelos_usados"].split(","):
            if m:
                usados[m] = usados.get(m, 0) + 1
    print("Casos por modelo:", usados)
    print("Ahora califica a mano las columnas finales en Excel o Sheets.")


if __name__ == "__main__":
    main()