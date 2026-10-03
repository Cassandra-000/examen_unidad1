
# PRACTICA: CONEXIÓN LOCAL CON MONGODB
# 11/09/2026
# ============================================

import tkinter as tk
from tkinter import messagebox
from pymongo import MongoClient


cliente = MongoClient("mongodb://localhost:27017")

db = cliente["practicas"]
alumnos = db["alumnos"]


def insertar_alumno():

    nombre = entrada_nombre.get().strip()
    edad = entrada_edad.get().strip()
    carrera = entrada_carrera.get().strip()

    # Verificar que no estén vacíos
    if nombre == "" or edad == "" or carrera == "":
        messagebox.showwarning(
            "Datos incompletos",
            "Por favor, completa todos los campos."
        )
        return

    # Verificar que la edad sea un número
    try:
        edad = int(edad)
    except ValueError:
        messagebox.showerror(
            "Error",
            "La edad debe ser un número."
        )
        return

    # Crear documento
    dato = {
        "nombre": nombre,
        "edad": edad,
        "carrera": carrera
    }

    try:

        resultado = alumnos.insert_one(dato)

        messagebox.showinfo(
            "Alumno registrado",
            f"Alumno insertado correctamente.\n\n"
            f"ID: {resultado.inserted_id}"
        )

        # Limpiar campos
        entrada_nombre.delete(0, tk.END)
        entrada_edad.delete(0, tk.END)
        entrada_carrera.delete(0, tk.END)

    except Exception as e:

        messagebox.showerror(
            "Error de MongoDB",
            f"No se pudo insertar el alumno.\n\n{e}"
        )

# ==== insertar alumnos ========================================

def insertar_ejemplo():

    datos = [
        {
            "nombre": "Anna",
            "edad": 21,
            "carrera": "chef"
        },
        {
            "nombre": "Carlos",
            "edad": 32,
            "carrera": "Sismologia"
        },
        {
            "nombre": "Maria",
            "edad": 30,
            "carrera": "Diseñadora"
        }
    ]

    try:

        resultado = alumnos.insert_many(datos)

        cantidad = len(resultado.inserted_ids)

        messagebox.showinfo(
            "Inserción exitosa",
            f"Documentos insertados: {cantidad}"
        )

    except Exception as e:

        messagebox.showerror(
            "Error de MongoDB",
            f"No se pudieron insertar los documentos.\n\n{e}"
        )

def cerrar_programa():

    cliente.close()
    ventana.destroy()

ventana = tk.Tk()

ventana.title("Práctica MongoDB Local")
ventana.geometry("600x600")
ventana.resizable(False, False)

titulo = tk.Label(
    ventana,
    text="Conexión local con MongoDB",
    font=("Arial", 22, "bold")
)

titulo.pack(pady=25)


informacion = tk.Label(
    ventana,
    text="Base de datos: practicas\nColección: alumnos",
    font=("Arial", 12)
)

informacion.pack(pady=10)

tk.Label(
    ventana,
    text="Nombre del alumno:",
    font=("Arial", 12)
).pack(pady=5)

entrada_nombre = tk.Entry(
    ventana,
    width=40,
    font=("Arial", 12)
)

entrada_nombre.pack(pady=5)

tk.Label(
    ventana,
    text="Edad:",
    font=("Arial", 12)
).pack(pady=5)

entrada_edad = tk.Entry(
    ventana,
    width=40,
    font=("Arial", 12)
)

entrada_edad.pack(pady=5)

tk.Label(
    ventana,
    text="Carrera:",
    font=("Arial", 12)
).pack(pady=5)

entrada_carrera = tk.Entry(
    ventana,
    width=40,
    font=("Arial", 12)
)

entrada_carrera.pack(pady=5)

tk.Button(
    ventana,
    text="Guardar alumno",
    command=insertar_alumno,
    font=("Arial", 12, "bold"),
    width=20
).pack(pady=20)

tk.Button(
    ventana,
    text="Insertar datos de ejemplo",
    command=insertar_ejemplo,
    font=("Arial", 11),
    width=25
).pack(pady=5)

tk.Button(
    ventana,
    text="Salir",
    command=cerrar_programa,
    font=("Arial", 11),
    width=15
).pack(pady=25)

ventana.protocol("WM_DELETE_WINDOW", cerrar_programa)

ventana.mainloop()