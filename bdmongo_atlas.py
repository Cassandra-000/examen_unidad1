import os
import tkinter as tk
from tkinter import messagebox
from dotenv import load_dotenv
from pymongo import MongoClient

# Cargar variables de entorno si cuentas con archivo .env
load_dotenv()


def guardar_en_atlas():
    # Obtener el texto ingresado por el usuario
    mensaje = entrada_mensaje.get().strip()

    if not mensaje:
        messagebox.showwarning(
            "Campo vacío", "Por favor ingresa un mensaje antes de continuar."
        )
        return

    # Usar la variable de entorno MONGO_URI si existe; de lo contrario, la URI directa
    uri = os.getenv(
        "MONGO_URI",
        "mongodb+srv://hector1985:Aime131985@utvt.qqqotrr.mongodb.net/?retryWrites=true&w=majority",
    )

    try:
        print("Conectando a MongoDB Atlas...")
        cliente = MongoClient(uri)

        # Probar que responde el servidor
        cliente.admin.command("ping")

        base_datos = cliente["cassandra"]
        coleccion = base_datos["gato"]

        dato = {"mensaje": mensaje}

        resultado = coleccion.insert_one(dato)

        print("¡Conexión exitosa a MongoDB Atlas!")
        print(f"Dato insertado con el ID: {resultado.inserted_id}")

        # Mostrar estado en la interfaz
        etiqueta_estado.config(
            text=(
                "¡Conexión exitosa a MongoDB Atlas!\n"
                f"ID asignado: {resultado.inserted_id}"
            ),
            fg="#2e7d32",
        )

        messagebox.showinfo(
            "Registro Exitoso",
            "El mensaje se guardó correctamente en MongoDB Atlas.",
        )

        # Limpiar el campo de texto
        entrada_mensaje.delete(0, tk.END)
        cliente.close()

    except Exception as e:
        print("Error al conectar:", e)
        etiqueta_estado.config(
            text=f"Error al conectar con Atlas:\n{e}", fg="#c62828"
        )
        messagebox.showerror(
            "Error de Conexión",
            f"No se pudo guardar el registro en la nube:\n\n{e}",
        )


# ================= Interfaz Gráfica (Tkinter) =================
ventana = tk.Tk()
ventana.title("Interfaz MongoDB Atlas - Gato")
ventana.geometry("520x420")
ventana.resizable(False, False)

# Encabezado
titulo = tk.Label(
    ventana,
    text="Conexión MongoDB Atlas",
    font=("Arial", 18, "bold"),
)
titulo.pack(pady=20)

subtitulo = tk.Label(
    ventana,
    text="Base de datos: cassabdra | Colección: gato",
    font=("Arial", 11),
    fg="#555555",
)
subtitulo.pack(pady=2)

# Campo para ingresar datos
etiqueta_campo = tk.Label(
    ventana, text="Ingresa el mensaje a guardar:", font=("Arial", 12)
)
etiqueta_campo.pack(pady=15)

entrada_mensaje = tk.Entry(ventana, width=42, font=("Arial", 12))
entrada_mensaje.pack(pady=5)
entrada_mensaje.insert(0, "blanco/negro")  # Valor inicial por defecto

# Botón de acción
boton_guardar = tk.Button(
    ventana,
    text="Guardar en MongoDB Atlas",
    command=guardar_en_atlas,
    font=("Arial", 12, "bold"),
    bg="#1b5e20",
    fg="white",
    padx=12,
    pady=6,
    cursor="hand2",
)
boton_guardar.pack(pady=25)

# Etiqueta para mensajes de estado y errores
etiqueta_estado = tk.Label(
    ventana,
    text="Esperando registro...",
    font=("Arial", 10),
    wraplength=460,
    justify="center",
)
etiqueta_estado.pack(pady=10)

ventana.mainloop()